#!/usr/bin/env python3
"""
fx_major/paper_scanner.py — 主流币模拟工作流

功能：
  - 每 5 分钟扫描市值前 100 主流币
  - 趋势/区间双模式信号，多空均做
  - 仓位动态计算（目标单次盈利 1%-5% 本金）
  - 复用清算图、恐惧贪婪、vol_regime 信号
  - 每日自动生成报告，接入 paper-daily-review
  - 模拟本金 1000U，独立状态文件，不影响山寨工作流

运行方式：
  python3 paper_scanner.py             # 持续 daemon
  python3 paper_scanner.py --once      # 单次扫描
  python3 paper_scanner.py --status    # 查看模拟仓位
  python3 paper_scanner.py --report    # 生成日报
  python3 paper_scanner.py --reset     # 清空状态
"""

import json
import logging
import math
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
# Insert in reverse priority: fx_major must shadow fx/ for 'config'
sys.path.insert(0, str(ROOT.parent / "fx"))   # lowest priority (fallback)
sys.path.insert(0, str(ROOT.parent))           # workspace root
sys.path.insert(0, str(ROOT))                  # fx_major/ — highest priority, shadows fx/config

# ── 日志配置 ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=[
        logging.FileHandler(ROOT / "logs" / "major_paper.log"),
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger("fx_major.paper")

# ── 常量 ──────────────────────────────────────────────────────────────────────
from config import (
    PAPER_CAPITAL, SCAN_INTERVAL_SEC, STATE_FILE, TRADE_LOG_FILE,
    MAX_POSITIONS_TOTAL, MAX_POSITIONS_LONG, MAX_POSITIONS_SHORT,
    CIRCUIT_DAILY_LOSS_U, COOLDOWN_HOURS_SL, TIME_STOP_HOURS, TIME_STOP_HARD_HOURS,
    ORDER_TIMEOUT_TREND_MIN, ORDER_TIMEOUT_RANGE_MIN,
)

_STATE_FILE = ROOT / STATE_FILE
_TRADE_LOG  = ROOT / TRADE_LOG_FILE
_STATE_LOCK = __import__("threading").Lock()

# ══════════════════════════════════════════════════════════════════════════════
# 通知（推送到 Jarvis）
# ══════════════════════════════════════════════════════════════════════════════
def _notify(msg: str):
    try:
        import subprocess
        subprocess.run(
            ["openclaw", "message", "send", "--channel", "jarvis",
             "--to", "432401404:thread:019fcac8-4a93-7bea-a930-7f0e59e4897c",
             "--message", f"[主流模拟] {msg}"],
            timeout=30, capture_output=True
        )
    except Exception as e:
        logger.warning(f"[notify] 推送失败: {e}")

# ══════════════════════════════════════════════════════════════════════════════
# 状态管理
# ══════════════════════════════════════════════════════════════════════════════
_DEFAULT_STATE = {
    "capital":       PAPER_CAPITAL,
    "positions":     {},   # symbol → position_dict
    "closed_trades": [],   # 已平仓记录
    "daily_loss":    0.0,
    "circuit_open":  False,
    "circuit_date":  "",
    "cooldowns":     {},   # symbol → expire_ts
    "stats": {
        "total_trades": 0,
        "wins": 0,
        "losses": 0,
        "total_pnl": 0.0,
        "max_drawdown": 0.0,
        "peak_capital": PAPER_CAPITAL,
    }
}

def _load_state() -> dict:
    try:
        if _STATE_FILE.exists():
            return json.loads(_STATE_FILE.read_text())
    except Exception:
        pass
    return json.loads(json.dumps(_DEFAULT_STATE))

def _save_state(state: dict):
    with _STATE_LOCK:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))

def _log_trade(trade: dict):
    _TRADE_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(_TRADE_LOG, "a") as f:
        f.write(json.dumps(trade, ensure_ascii=False) + "\n")

# ── 熔断 ─────────────────────────────────────────────────────────────────────
def _check_circuit(state: dict) -> bool:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if state.get("circuit_date") != today:
        state["daily_loss"]   = 0.0
        state["circuit_open"] = False
        state["circuit_date"] = today
    return state.get("circuit_open", False)

def _record_loss(state: dict, loss_u: float):
    state["daily_loss"] = state.get("daily_loss", 0) + abs(loss_u)
    if state["daily_loss"] >= CIRCUIT_DAILY_LOSS_U:
        state["circuit_open"] = True
        logger.warning(f"[paper] 熔断触发！日亏损={state['daily_loss']:.1f}U")
        _notify(f"🔴 熔断触发！日亏损 {state['daily_loss']:.1f}U，今日停止开仓")

# ── 仓位统计 ──────────────────────────────────────────────────────────────────
def _position_counts(state: dict) -> dict:
    positions = state.get("positions", {})
    longs  = sum(1 for p in positions.values() if p.get("direction") == "LONG")
    shorts = sum(1 for p in positions.values() if p.get("direction") == "SHORT")
    return {"total": len(positions), "long": longs, "short": shorts}

# ══════════════════════════════════════════════════════════════════════════════
# 模拟开仓
# ══════════════════════════════════════════════════════════════════════════════
def _open_position(state: dict, sig: dict) -> bool:
    symbol    = sig["symbol"]
    direction = sig["direction"]
    now       = time.time()

    # 检查冷却
    cooldown_until = state.get("cooldowns", {}).get(symbol, 0)
    if now < cooldown_until:
        logger.debug(f"[paper] {symbol} 冷却中，跳过")
        return False

    # 检查重复持仓
    if symbol in state.get("positions", {}):
        logger.debug(f"[paper] {symbol} 已持仓，跳过")
        return False

    counts = _position_counts(state)
    if counts["total"] >= MAX_POSITIONS_TOTAL:
        logger.debug(f"[paper] 总槽位已满 ({counts['total']}/{MAX_POSITIONS_TOTAL})")
        return False
    if direction == "LONG"  and counts["long"]  >= MAX_POSITIONS_LONG:
        logger.debug(f"[paper] 多单槽位已满")
        return False
    if direction == "SHORT" and counts["short"] >= MAX_POSITIONS_SHORT:
        logger.debug(f"[paper] 空单槽位已满")
        return False

    notional = sig.get("notional", 0)
    if notional <= 0:
        return False

    pos = {
        "symbol":      symbol,
        "direction":   direction,
        "mode":        sig.get("mode", "unknown"),
        "entry_price": sig["entry_price"],
        "sl":          sig["sl"],
        "tp1":         sig["tp1"],
        "tp2":         sig["tp2"],
        "tp1_ratio":   sig.get("tp1_ratio", 0.6),
        "tp2_ratio":   sig.get("tp2_ratio", 0.4),
        "notional":    notional,
        "score":       sig.get("score", 0),
        "open_ts":     now,
        "open_time":   datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
        "tp1_closed":  False,
        "indicators":  sig.get("indicators", {}),
        "market_info": sig.get("market_info", {}),
        "btc_trend":   sig.get("btc_trend", ""),    # 开仓时 BTC 趋势
        "is_weekend":  sig.get("is_weekend", False), # 开仓时是否周末
    }

    state.setdefault("positions", {})[symbol] = pos
    logger.info(
        f"[paper] 开仓 {symbol} {direction} mode={pos['mode']} "
        f"entry={sig['entry_price']} sl={sig['sl']} tp1={sig['tp1']} "
        f"notional={notional:.0f}U score={sig.get('score',0)}"
    )
    # 开仓通知已静默（避免持仓推送刷屏），仅记录日志
    return True


# ══════════════════════════════════════════════════════════════════════════════
# 模拟平仓
# ══════════════════════════════════════════════════════════════════════════════
def _close_position(state: dict, symbol: str, close_price: float,
                    reason: str, partial_ratio: float = 1.0) -> float:
    """返回本次平仓 PnL（U）"""
    pos = state.get("positions", {}).get(symbol)
    if not pos:
        return 0.0

    direction  = pos["direction"]
    entry      = pos["entry_price"]
    notional   = pos["notional"]
    ratio      = partial_ratio

    if direction == "LONG":
        pnl_pct = (close_price - entry) / entry
    else:
        pnl_pct = (entry - close_price) / entry

    pnl_u = pnl_pct * notional * ratio
    now   = time.time()
    hold_h = (now - pos["open_ts"]) / 3600

    trade = {
        "symbol":      symbol,
        "direction":   direction,
        "mode":        pos.get("mode"),
        "entry_price": entry,
        "close_price": close_price,
        "sl_price":    pos.get("sl", 0),          # 止损价（修复记录bug）
        "tp1_price":   pos.get("tp1", 0),         # 止盈1价
        "tp2_price":   pos.get("tp2", 0),         # 止盈2价
        "sl_dist_pct": round(abs(entry - pos.get("sl", entry)) / entry * 100, 3) if entry else 0,
        "notional":    notional,
        "ratio":       ratio,
        "pnl_u":       round(pnl_u, 4),
        "pnl_pct":     round(pnl_pct * ratio * 100, 3),
        "hold_hours":  round(hold_h, 2),
        "reason":      reason,
        "open_ts":     pos["open_ts"],
        "close_ts":    now,
        "close_time":  datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
        "score":       pos.get("score", 0),
        "btc_trend":   pos.get("btc_trend", ""),  # BTC趋势（信号时）
        "indicators":  pos.get("indicators", {}),
    }
    _log_trade(trade)

    # 更新统计
    stats = state.setdefault("stats", {})
    stats["total_trades"] = stats.get("total_trades", 0) + 1
    stats["total_pnl"]    = round(stats.get("total_pnl", 0) + pnl_u, 4)
    state["capital"]      = round(state.get("capital", PAPER_CAPITAL) + pnl_u, 4)

    if pnl_u >= 0:
        stats["wins"] = stats.get("wins", 0) + 1
    else:
        stats["losses"] = stats.get("losses", 0) + 1
        _record_loss(state, pnl_u)
        # 止损冷却
        state.setdefault("cooldowns", {})[symbol] = now + COOLDOWN_HOURS_SL * 3600

    # 峰值 & 最大回撤
    cap = state["capital"]
    peak = stats.get("peak_capital", PAPER_CAPITAL)
    if cap > peak:
        stats["peak_capital"] = cap
    else:
        dd = (peak - cap) / peak * 100
        if dd > stats.get("max_drawdown", 0):
            stats["max_drawdown"] = round(dd, 2)

    # 全平还是部分平仓
    if ratio >= 1.0 or pos.get("tp1_closed") and ratio >= pos.get("tp2_ratio", 0.4):
        del state["positions"][symbol]
        state.setdefault("closed_trades", []).append(trade)
        logger.info(f"[paper] 平仓 {symbol} {reason} pnl={pnl_u:.2f}U ({pnl_pct*ratio*100:.2f}%)")
        emoji = "✅" if pnl_u >= 0 else "❌"
        # 只在亏损时通知，避免频繁推送
        if pnl_u < 0:
            _notify(
                f"{emoji} 平仓 {symbol} [{reason}]\n"
                f"盈亏: {pnl_u:+.2f}U ({pnl_pct*ratio*100:+.2f}%)\n"
                f"持仓: {hold_h:.1f}h  当前本金: {state['capital']:.1f}U"
            )
    else:
        # TP1 部分平仓后标记
        pos["tp1_closed"] = True
        pos["notional"]   = round(notional * (1 - ratio), 2)
        logger.info(f"[paper] 部分平仓 {symbol} TP1 {ratio*100:.0f}% pnl={pnl_u:.2f}U")

    return pnl_u


# ══════════════════════════════════════════════════════════════════════════════
# 止盈止损巡检
# ══════════════════════════════════════════════════════════════════════════════
def _check_positions(state: dict, mark_prices: dict):
    for symbol in list(state.get("positions", {}).keys()):
        pos = state["positions"].get(symbol)
        if not pos:
            continue

        price = mark_prices.get(symbol)
        if not price:
            continue

        direction = pos["direction"]
        now       = time.time()
        hold_h    = (now - pos["open_ts"]) / 3600

        # ── 时间止损 ─────────────────────────────────────────────────────────
        if hold_h >= TIME_STOP_HARD_HOURS:
            _close_position(state, symbol, price, f"时间止损(硬){hold_h:.1f}h")
            continue

        # ── 止损 ─────────────────────────────────────────────────────────────
        sl = pos["sl"]
        if direction == "LONG"  and price <= sl:
            _close_position(state, symbol, price, "止损SL")
            continue
        if direction == "SHORT" and price >= sl:
            _close_position(state, symbol, price, "止损SL")
            continue

        # ── TP1（部分平仓）───────────────────────────────────────────────────
        if not pos.get("tp1_closed"):
            tp1 = pos["tp1"]
            tp1_ratio = pos.get("tp1_ratio", 0.6)
            if direction == "LONG"  and price >= tp1:
                _close_position(state, symbol, price, "TP1", partial_ratio=tp1_ratio)
                continue
            if direction == "SHORT" and price <= tp1:
                _close_position(state, symbol, price, "TP1", partial_ratio=tp1_ratio)
                continue

        # ── TP2（全平）──────────────────────────────────────────────────────
        tp2 = pos["tp2"]
        if direction == "LONG"  and price >= tp2:
            _close_position(state, symbol, price, "TP2")
        if direction == "SHORT" and price <= tp2:
            _close_position(state, symbol, price, "TP2")


# ══════════════════════════════════════════════════════════════════════════════
# 批量拉取 mark price
# ══════════════════════════════════════════════════════════════════════════════
def _fetch_mark_prices(symbols: list) -> dict:
    try:
        import urllib.request as _ur
        data = json.loads(_ur.urlopen(
            "https://fapi.binance.com/fapi/v1/premiumIndex", timeout=10).read())
        mp = {item["symbol"]: float(item.get("markPrice", 0)) for item in data}
        return {s: mp[s] for s in symbols if s in mp}
    except Exception as e:
        logger.warning(f"[paper] mark price 拉取失败: {e}")
        return {}


# ══════════════════════════════════════════════════════════════════════════════
# 日报生成
# ══════════════════════════════════════════════════════════════════════════════
def generate_report(state: dict = None) -> str:
    if state is None:
        state = _load_state()

    stats    = state.get("stats", {})
    capital  = state.get("capital", PAPER_CAPITAL)
    pnl      = stats.get("total_pnl", 0)
    trades   = stats.get("total_trades", 0)
    wins     = stats.get("wins", 0)
    losses   = stats.get("losses", 0)
    wr       = wins / trades * 100 if trades > 0 else 0
    dd       = stats.get("max_drawdown", 0)
    peak     = stats.get("peak_capital", PAPER_CAPITAL)
    positions = state.get("positions", {})

    # Vol regime 当前状态
    vol_line = ""
    try:
        sys.path.insert(0, str(ROOT.parent / "fx"))
        from shared.vol_regime import get_vol_regime
        btc_vol = get_vol_regime("BTCUSDT")
        eth_vol = get_vol_regime("ETHUSDT")
        vol_line = (
            f"\n📊 波动率状态\n"
            f"  BTC: IV={btc_vol['iv']}% RV={btc_vol['rv']}% → {btc_vol['regime'].upper()}\n"
            f"  ETH: IV={eth_vol['iv']}% RV={eth_vol['rv']}% → {eth_vol['regime'].upper()}"
        )
    except Exception:
        pass

    # 候选池状态
    try:
        from universe import get_universe
        pool = get_universe()
        pool_line = f"\n🌐 候选池: {len(pool)} 个主流币"
    except Exception:
        pool_line = ""

    # 当前持仓
    pos_lines = ""
    if positions:
        pos_lines = "\n📂 当前持仓:\n"
        for sym, pos in positions.items():
            hold_h = (time.time() - pos["open_ts"]) / 3600
            pos_lines += (
                f"  {sym} {'🟢多' if pos['direction']=='LONG' else '🔴空'} "
                f"mode={pos.get('mode','?')} "
                f"entry={pos['entry_price']} "
                f"持仓{hold_h:.1f}h\n"
            )

    # 最近5笔交易
    recent = state.get("closed_trades", [])[-5:]
    recent_lines = ""
    if recent:
        recent_lines = "\n📋 最近5笔:\n"
        for t in reversed(recent):
            emoji = "✅" if t["pnl_u"] >= 0 else "❌"
            recent_lines += (
                f"  {emoji} {t['symbol']} {t['direction']} "
                f"{t['pnl_u']:+.1f}U [{t['reason']}] "
                f"持{t['hold_hours']:.1f}h\n"
            )

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    report  = (
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 主流币模拟工作流日报\n"
        f"{now_str}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 本金: {capital:.1f}U  (初始 {PAPER_CAPITAL}U)\n"
        f"📈 总盈亏: {pnl:+.1f}U ({pnl/PAPER_CAPITAL*100:+.2f}%)\n"
        f"🏆 胜率: {wr:.1f}% ({wins}胜/{losses}负, 共{trades}笔)\n"
        f"📉 最大回撤: {dd:.2f}%\n"
        f"{pool_line}{vol_line}{pos_lines}{recent_lines}"
        f"━━━━━━━━━━━━━━━━━━━━━━━"
    )
    return report


# ══════════════════════════════════════════════════════════════════════════════
# 主循环
# ══════════════════════════════════════════════════════════════════════════════
_running = True

def _handle_signal(sig, frame):
    global _running
    logger.info("[paper] 收到退出信号，准备停止...")
    _running = False

signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT,  _handle_signal)


def run_once(state: dict) -> dict:
    """执行一轮扫描 + 止盈止损检查"""
    from scanner import run_scan

    # 1. 止盈止损巡检（先做，避免错过价格）
    all_syms = list(state.get("positions", {}).keys())
    if all_syms:
        mps = _fetch_mark_prices(all_syms)
        _check_positions(state, mps)

    # 2. 熔断检查
    if _check_circuit(state):
        logger.info("[paper] 熔断激活，跳过开仓")
        _save_state(state)
        return state

    # 3. 扫描
    scan_result = run_scan(paper_capital=state.get("capital", PAPER_CAPITAL), dry_run=False)
    signals     = scan_result.get("signals", [])

    # 4. 执行信号
    placed = 0
    for sig in signals:
        if _open_position(state, sig):
            placed += 1

    _save_state(state)

    if placed > 0 or signals:
        logger.info(
            f"[paper] 本轮: 扫描={scan_result.get('scanned',0)} "
            f"信号={len(signals)} 开仓={placed}"
        )
    return state


def main():
    logger.info(f"[paper] 主流币模拟工作流启动 本金={PAPER_CAPITAL}U")
    state = _load_state()

    once   = "--once"   in sys.argv
    status = "--status" in sys.argv
    report = "--report" in sys.argv
    reset  = "--reset"  in sys.argv

    if reset:
        _STATE_FILE.unlink(missing_ok=True)
        _TRADE_LOG.unlink(missing_ok=True)
        logger.info("[paper] 状态已重置")
        return

    if status:
        s = _load_state()
        print(generate_report(s))
        return

    if report:
        print(generate_report())
        return

    if once:
        run_once(state)
        print(generate_report(state))
        return

    # ── daemon 模式 ───────────────────────────────────────────────────────────
    _notify("🚀 主流币模拟工作流启动")
    interval = SCAN_INTERVAL_SEC

    while _running:
        try:
            state = run_once(state)
        except Exception as e:
            logger.error(f"[paper] 主循环异常: {e}", exc_info=True)

        if not _running:
            break
        time.sleep(interval)

    _save_state(state)
    logger.info("[paper] 工作流已停止")
    _notify("⏹️ 主流币模拟工作流已停止")


if __name__ == "__main__":
    main()
