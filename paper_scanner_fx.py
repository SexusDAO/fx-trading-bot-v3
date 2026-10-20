#!/usr/bin/env python3
"""
paper_scanner_fx.py — 与主工作流 fx/ 信号体系完全对齐的模拟盘 scanner

★ 设计原则：
  - 信号逻辑完全复用 fx/scanner.py（classify + judge_reversal/breakout/pullback）
  - 止盈止损计算复用 fx/shared/calc_sl_tp.py
  - 仓位大小固定 100U（模拟模式），持仓无上限（测试用）
  - 开仓/平仓/状态全部写 paper_fx_state.json（独立，不干扰 paper_test_state.json）
  - 推送消息带 [模拟FX] 标记
  - 守护进程模式，每 10 分钟扫描一次

运行方式：
  python3 paper_scanner_fx.py          # daemon 模式
  python3 paper_scanner_fx.py --once   # 单次扫描
  python3 paper_scanner_fx.py --status # 查看当前模拟持仓
  python3 paper_scanner_fx.py --reset  # 清空模拟状态
"""

import sys
import json
import os
import time
import pathlib
import threading
import signal
from datetime import datetime, timezone

WORKSPACE = "/root/.openclaw/workspace"
# fx/ 必须在最前，否则 scanner 会误导入根目录的 scanner.py
sys.path = [f"{WORKSPACE}/fx", WORKSPACE] + [
    p for p in sys.path if p not in (f"{WORKSPACE}/fx", WORKSPACE)
]

# ── 导入 fx/ 的信号逻辑（与主工作流完全一致）──────────────────────────────────
from scanner import (
    analyze_symbol,   # 完整的单标的分析（classify + judge + 方向判断）
    refresh_pool,
    _load_pool,
    _in_funding_window,
    _flush_oi_cache,
    calc_indicators,
)
from shared import binance_api as api
from shared.state import get_all_positions as _get_fx_positions, get_oi_24h_ago, update_oi_cache
from shared.calc_sl_tp import calc_sl_tp
from shared.coinglass_api import get_liq_zones_list as _get_liq_zones
from config import (
    POOL_VOLUME_TOP_N, POOL_GAINERS_N, POOL_LOSERS_N,
    POOL_MIN_VOLUME_USD,
    SIGNAL_PRIORITY,
    FUNDING_SETTLE_HOURS,
    SL_MULT_LOW, SL_MULT_MID, SL_MULT_HIGH,
)

# ── 模拟盘独立状态文件 ─────────────────────────────────────────────────────────
PAPER_FX_STATE  = f"{WORKSPACE}/paper_fx_state.json"
LOG_FILE        = f"{WORKSPACE}/logs/paper_scanner_fx.log"
HEARTBEAT_FILE  = f"{WORKSPACE}/ws_cache/paper_scanner_fx_heartbeat.json"
JARVIS_TARGET   = "432401404:thread:01a05aaa-7d26-752b-b179-181ce62bb767"

# ── 模拟仓位规则（与 paper_test 对齐，只改信号体系）──────────────────────────
POS_SIZE_U      = 100.0   # 固定 100U 名义仓位
MAX_POSITIONS   = 999     # 测试版：无上限
MAX_LONG        = 999
MAX_SHORT       = 999
SCAN_INTERVAL   = 600     # 10 分钟

# ── 线程锁（防并发写 state 文件）─────────────────────────────────────────────
_STATE_LOCK = threading.Lock()
_RUNNING    = True

# ─────────────────────────────────────────────────────────────────────────────
# 日志 & 通知
# ─────────────────────────────────────────────────────────────────────────────
def log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    # stdout 已被守护进程重定向到文件时，不再额外写 LOG_FILE（防止重复）
    import sys as _sys
    try:
        _stdout_is_file = not _sys.stdout.isatty()
    except Exception:
        _stdout_is_file = False
    if not _stdout_is_file:
        try:
            pathlib.Path(LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


def notify(msg: str):
    """推送到 Jarvis，带 [模拟FX] 标记，异步不阻塞。"""
    def _send():
        try:
            import subprocess
            tagged = f"[模拟FX] {msg}"
            subprocess.run(
                ["openclaw", "msg", "send", "--to", JARVIS_TARGET,
                 "--message", tagged, "--channel", "jarvis"],
                timeout=10, capture_output=True,
            )
        except Exception:
            pass
    threading.Thread(target=_send, daemon=True).start()


# ─────────────────────────────────────────────────────────────────────────────
# 状态文件 读/写
# ─────────────────────────────────────────────────────────────────────────────
_DEFAULT_STATE = lambda: {
    "positions": {},
    "history":   [],
    "stats":     {"total": 0, "wins": 0, "losses": 0, "pnl": 0.0},
    "reset_at":  datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
}


def _load_state() -> dict:
    try:
        f = pathlib.Path(PAPER_FX_STATE)
        if f.exists():
            return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        pass
    return _DEFAULT_STATE()


def _save_state(state: dict):
    with _STATE_LOCK:
        try:
            pathlib.Path(PAPER_FX_STATE).write_text(
                json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as e:
            log(f"[state] 保存失败: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# 模拟开仓 / 平仓
# ─────────────────────────────────────────────────────────────────────────────
def _paper_open(sym: str, sig: dict, entry_price: float,
                sl_price: float, tp1_price: float, tp2_price: float):
    """记录模拟开仓。"""
    state = _load_state()
    positions = state["positions"]

    if sym in positions:
        log(f"[paper] {sym} 已有模拟持仓，跳过")
        return False

    direction = sig["direction"]
    sig_type  = sig["sig_type"]

    pos = {
        "symbol":     sym,
        "direction":  direction,
        "sig_type":   sig_type,
        "entry":      entry_price,
        "sl_price":   sl_price,
        "tp1_price":  tp1_price,
        "tp2_price":  tp2_price,
        "pos_usdt":   POS_SIZE_U,
        "opened_at":  datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "opened_ts":  time.time(),
        "tp1_hit":    False,
        "trailing_sl": None,
        "intra_high": entry_price,
        "intra_low":  entry_price,
        # 用于复盘的上下文
        "chg24":      sig.get("chg24", 0),
        "funding":    sig.get("funding", 0),
        "ind_summary": {
            "rsi_1h":   sig["ind"].get("rsi_1h", 0),
            "rsi_4h":   sig["ind"].get("rsi_4h", 0),
            "vol_ratio": sig["ind"].get("vol_ratio", 0),
            "atr_ratio": sig["ind"].get("atr_ratio", 0),
            "final_score": sig["ind"].get("final_score", 0),
        },
        "snapshots":  [],
    }
    positions[sym] = pos
    _save_state(state)

    score = sig["ind"].get("final_score", 0)
    log(f"[paper] ✅ 开仓 {sym} {direction} {sig_type} "
        f"entry={entry_price:.6g} sl={sl_price:.6g} tp1={tp1_price:.6g} "
        f"score={score:.0f} pos=100U")
    notify(
        f"{'🟢' if direction == 'LONG' else '🔴'} {sym} {direction} [{sig_type}]\n"
        f"入场: {entry_price:.6g}  止损: {sl_price:.6g}  TP1: {tp1_price:.6g}\n"
        f"评分: {score:.0f}  chg24={sig.get('chg24',0):+.1f}%  仓位: 100U  ⚠️ 仅模拟"
    )
    return True


def _paper_close(sym: str, reason: str, close_price: float | None = None):
    """平仓并写入历史。"""
    state = _load_state()
    positions = state.get("positions", {})
    pos = positions.get(sym)
    if not pos:
        return

    # 用实时价格（若未传入）
    if close_price is None:
        try:
            mp = api.get_mark_price(sym)
            close_price = float(mp.get("markPrice", 0))
        except Exception:
            close_price = pos["entry"]

    direction  = pos["direction"]
    entry      = pos["entry"]
    pos_usdt   = pos.get("pos_usdt", POS_SIZE_U)

    if direction == "LONG":
        pnl_pct = (close_price - entry) / entry * 100
    else:
        pnl_pct = (entry - close_price) / entry * 100

    pnl_u = round(pos_usdt * pnl_pct / 100, 2)

    record = {**pos, "close_price": close_price, "close_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
              "pnl_u": pnl_u, "pnl_pct": round(pnl_pct, 2), "reason": reason}
    state["history"].append(record)

    stats = state.setdefault("stats", {"total": 0, "wins": 0, "losses": 0, "pnl": 0.0})
    stats["total"] += 1
    stats["pnl"]    = round(stats["pnl"] + pnl_u, 2)
    if pnl_u > 0:
        stats["wins"] += 1
    else:
        stats["losses"] += 1

    del positions[sym]
    _save_state(state)

    emoji = "✅" if pnl_u > 0 else "❌"
    log(f"[paper] {emoji} 平仓 {sym} {reason} pnl={pnl_u:+.2f}U ({pnl_pct:+.2f}%)")
    notify(
        f"{emoji} 平仓 {sym} {pos['direction']} [{pos['sig_type']}]\n"
        f"原因: {reason}\n"
        f"盈亏: {pnl_u:+.2f}U ({pnl_pct:+.2f}%)"
    )


# ─────────────────────────────────────────────────────────────────────────────
# SL / TP 心跳检查（每 30s 一次）
# ─────────────────────────────────────────────────────────────────────────────
def _check_sl_tp():
    """检查所有模拟持仓的止盈止损，含移动止损和 24h 底安逻辑。"""
    state = _load_state()
    positions = state.get("positions", {})
    if not positions:
        return

    for sym in list(positions.keys()):
        pos = positions.get(sym)
        if not pos:
            continue
        try:
            mp = api.get_mark_price(sym)
            mark = float(mp.get("markPrice", 0))
        except Exception:
            continue
        if mark <= 0:
            continue

        direction  = pos["direction"]
        entry      = pos["entry"]
        sl_price   = pos["sl_price"]
        tp1_price  = pos["tp1_price"]
        tp2_price  = pos.get("tp2_price", 0)
        pos_usdt   = pos.get("pos_usdt", POS_SIZE_U)
        tp1_hit    = pos.get("tp1_hit", False)
        trailing   = pos.get("trailing_sl")
        opened_ts  = pos.get("opened_ts", 0)

        # 更新日内极值
        if direction == "LONG":
            pos["intra_high"] = max(pos.get("intra_high", mark), mark)
        else:
            pos["intra_low"]  = min(pos.get("intra_low", mark), mark)

        # 8h 快照
        hold_h = (time.time() - opened_ts) / 3600 if opened_ts else 0
        if hold_h >= 8 and not pos.get("snap8_done"):
            pnl_pct = (mark - entry) / entry * 100 if direction == "LONG" else (entry - mark) / entry * 100
            pnl_u   = round(pos_usdt * pnl_pct / 100, 2)
            pos.setdefault("snapshots", []).append({"type": "snap_8h", "hold_h": round(hold_h, 1), "mark": mark,
                                                     "pnl_pct": round(pnl_pct, 2), "pnl_u": pnl_u,
                                                     "at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")})
            pos["snap8_done"] = True

        # TP1 触达 → 启动移动止损
        if not tp1_hit:
            if (direction == "LONG" and mark >= tp1_price) or (direction == "SHORT" and mark <= tp1_price):
                pos["tp1_hit"] = True
                # 移动止损 = TP1 × 0.97（多）或 TP1 × 1.03（空）
                pos["trailing_sl"] = round(tp1_price * (0.97 if direction == "LONG" else 1.03), 8)
                log(f"[sl_tp] {sym} TP1触达 mark={mark:.6g}，启动移动止损={pos['trailing_sl']:.6g}")
                trailing = pos["trailing_sl"]

        # 更新移动止损
        if trailing:
            if direction == "LONG":
                new_trail = round(mark * 0.97, 8)
                if new_trail > trailing:
                    pos["trailing_sl"] = new_trail
                    trailing = new_trail
                # 移动止损触发
                if mark <= trailing:
                    _save_state(state)
                    _paper_close(sym, "移动止损触发", mark)
                    state = _load_state()
                    positions = state.get("positions", {})
                    continue
            else:
                new_trail = round(mark * 1.03, 8)
                if new_trail < trailing:
                    pos["trailing_sl"] = new_trail
                    trailing = new_trail
                if mark >= trailing:
                    _save_state(state)
                    _paper_close(sym, "移动止损触发", mark)
                    state = _load_state()
                    positions = state.get("positions", {})
                    continue

        # 原始 SL
        if (direction == "LONG" and mark <= sl_price) or (direction == "SHORT" and mark >= sl_price):
            _save_state(state)
            _paper_close(sym, "SL触发", mark)
            state = _load_state()
            positions = state.get("positions", {})
            continue

        # 24h 底安平仓
        if opened_ts and (time.time() - opened_ts) >= 86400:
            pnl_pct = (mark - entry) / entry * 100 if direction == "LONG" else (entry - mark) / entry * 100
            pnl_u   = round(pos_usdt * pnl_pct / 100, 2)
            reason  = f"24h底安平仓({pnl_pct:+.2f}%)"
            _save_state(state)
            _paper_close(sym, reason, mark)
            state = _load_state()
            positions = state.get("positions", {})
            continue

        # 状态已被修改（trailing更新），写回
        state["positions"][sym] = pos

    _save_state(state)


def _sl_tp_loop():
    """独立线程，每 30s 检查一次止盈止损。"""
    log("[main] 止损心跳启动，每30秒检查一次 SL/TP")
    while _RUNNING:
        try:
            _check_sl_tp()
        except Exception as e:
            log(f"[sl_tp] 心跳异常: {e}")
        time.sleep(30)


# ─────────────────────────────────────────────────────────────────────────────
# 主扫描（直接调用 fx/scanner.analyze_symbol）
# ─────────────────────────────────────────────────────────────────────────────
def run_scan():
    log("=" * 60)
    log(f"[scan_start] {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")

    # 心跳
    try:
        pathlib.Path(HEARTBEAT_FILE).parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(HEARTBEAT_FILE).write_text(json.dumps({"ts": time.time()}))
    except Exception:
        pass

    state     = _load_state()
    positions = state.get("positions", {})
    active_syms  = set(positions.keys())
    pending_syms: set = set()   # 模拟盘无挂单概念

    log(f"[scan] 当前持仓 {len(positions)}/{MAX_POSITIONS} "
        f"（多{sum(1 for p in positions.values() if p.get('direction')=='LONG')} "
        f"空{sum(1 for p in positions.values() if p.get('direction')=='SHORT')}）")

    # 模拟盘测信号准确度：资金费率结算窗口仅记录，不跳过（实盘才跳过）
    try:
        btc_mp = api.get_mark_price("BTCUSDT")
        btc_fr = abs(float(btc_mp.get("lastFundingRate", 0)))
        in_win, win_reason = _in_funding_window(btc_fr)
        if in_win:
            log(f"[scan] 注意: {win_reason}（模拟盘不跳过，继续扫描）")
    except Exception:
        pass

    # 加载候选池（若过期则刷新）
    try:
        pool_data = json.loads(pathlib.Path(f"{WORKSPACE}/fx/logs/candidate_pool.json").read_text())
        pool_age  = time.time() - pool_data.get("ts", 0)
        pool      = pool_data.get("pool", [])
        if pool_age > 1800 or not pool:   # 超过30分钟则刷新
            log("[scan] 候选池过期，刷新中...")
            pool = refresh_pool()
    except Exception:
        log("[scan] 候选池读取失败，刷新中...")
        pool = refresh_pool()

    if not pool:
        log("[scan] 候选池为空，跳过")
        return

    # 排除已持仓
    scan_pool = [s for s in pool if s not in active_syms]
    log(f"[scan] 扫描池: {len(scan_pool)} 个标的")

    # ── 并行分析（直接调用 fx/scanner.analyze_symbol，信号逻辑完全一致）──────
    import concurrent.futures
    signals_raw = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as exe:
        futures = {
            exe.submit(analyze_symbol, sym, {}, active_syms, pending_syms): sym
            for sym in scan_pool
        }
        for fut in concurrent.futures.as_completed(futures, timeout=240):
            try:
                res = fut.result(timeout=30)
                if res:
                    signals_raw.append(res)
            except Exception:
                pass

    log(f"[scan] 原始信号: {len(signals_raw)} 个")

    # ── BTC 趋势加减分（与 fx/scanner.run_scan 对齐）─────────────────────
    try:
        _btc_k = api.get_klines("BTCUSDT", "4h", 60)
        _c = [float(k[4]) for k in _btc_k]
        def _ema_s(p, n):
            k = 2.0 / (n + 1); v = sum(p[:n]) / n
            for x in p[n:]: v = x * k + v * (1 - k)
            return v
        _e9, _e20, _e50 = _ema_s(_c, 9), _ema_s(_c, 20), _ema_s(_c, 50)
        btc_trend = "up" if _e9 > _e20 > _e50 else ("down" if _e9 < _e20 < _e50 else "")
    except Exception:
        btc_trend = ""
    log(f"[scan] BTC 趋势: {btc_trend or 'neutral'}")

    for sig in signals_raw:
        direction  = sig["direction"]
        ind        = sig["ind"]
        # 基础分：用 judge 输出的结构分，如无则 70 起跳
        base = ind.get("final_score") or ind.get("score", 70)
        btc_adj = 0
        if btc_trend == "up"   and direction == "LONG":  btc_adj = +5
        elif btc_trend == "up"   and direction == "SHORT": btc_adj = -5
        elif btc_trend == "down" and direction == "LONG":  btc_adj = -5
        elif btc_trend == "down" and direction == "SHORT": btc_adj = +5
        ind["final_score"] = min(100, max(0, base + btc_adj))
        ind["btc_trend"]   = btc_trend

    # 按优先级排序（reversal > breakout > pullback）
    signals_raw.sort(key=lambda x: (
        SIGNAL_PRIORITY.get(x["sig_type"], 99),
        -x["ind"].get("vol_ratio", 0)
    ))

    # ── 计算 SL/TP 并模拟开仓 ─────────────────────────────────────────────────
    opened = 0
    for sig in signals_raw:
        sym = sig["symbol"]
        if sym in active_syms:
            continue   # 已在本轮新开过

        # 拉取 Coinglass 清算图（格式对齐 executor._fetch_liq）
        try:
            liq_long  = _get_liq_zones(sym, "LONG")
            liq_short = _get_liq_zones(sym, "SHORT")
            liq_zones = {"long": liq_long, "short": liq_short}
        except Exception:
            liq_zones = {"long": [], "short": []}

        # 拉取 ATR、EMA、K线（fx/executor 需要）
        try:
            k1h  = api.get_klines(sym, "1h", 50)
            mark = float(api.get_mark_price(sym).get("markPrice", 0))
            ind  = sig["ind"]
            atr  = ind.get("atr_1h", mark * 0.02)
            ema20 = ind.get("ema20_1h", mark)
        except Exception as e:
            log(f"[scan] {sym} 市场数据获取失败: {e}")
            continue

        # 计算入场参数（与实盘完全相同的 calc_sl_tp）
        try:
            params = calc_sl_tp(
                signal=sig,
                mark_price=mark,
                atr=atr,
                liq_zones=liq_zones,
                k1h=k1h,
                ema20=ema20,
            )
            if "error" in params:
                log(f"[scan] {sym} calc_sl_tp 拒绝: {params['error']}")
                continue
        except Exception as e:
            log(f"[scan] {sym} calc_sl_tp 异常: {e}")
            continue

        entry_price = params["entry_price"]
        sl_price    = params["sl_price"]
        tp1_price   = params["tp1_price"]
        tp2_price   = params["tp2_price"]

        ok = _paper_open(sym, sig, entry_price, sl_price, tp1_price, tp2_price)
        if ok:
            active_syms.add(sym)
            opened += 1

    log(f"[scan] 本轮新开仓: {opened} 笔 | 信号总计: {len(signals_raw)}")


# ─────────────────────────────────────────────────────────────────────────────
# 状态打印
# ─────────────────────────────────────────────────────────────────────────────
def print_status():
    state     = _load_state()
    positions = state.get("positions", {})
    stats     = state.get("stats", {})
    history   = state.get("history", [])

    print(f"\n=== 模拟FX持仓状态 ===")
    print(f"运行起始: {state.get('reset_at','?')}")
    print(f"总计: {stats.get('total',0)}笔 | 胜: {stats.get('wins',0)} | 负: {stats.get('losses',0)} | PnL: {stats.get('pnl',0):+.2f}U")
    if stats.get('total', 0) > 0:
        wr = stats['wins'] / stats['total'] * 100
        print(f"胜率: {wr:.1f}%")

    print(f"\n当前持仓 ({len(positions)}个):")
    for sym, pos in positions.items():
        print(f"  {sym} {pos['direction']} [{pos['sig_type']}] entry={pos['entry']:.6g} "
              f"sl={pos['sl_price']:.6g} 开仓@{pos.get('opened_at','?')}")

    if history:
        recent = history[-5:]
        print(f"\n最近5笔已结算:")
        for h in recent:
            emoji = "✅" if h.get('pnl_u', 0) > 0 else "❌"
            print(f"  {emoji} {h.get('symbol')} {h.get('direction')} pnl={h.get('pnl_u',0):+.2f}U {h.get('reason','?')}")


# ─────────────────────────────────────────────────────────────────────────────
# 入口
# ─────────────────────────────────────────────────────────────────────────────
def _signal_handler(sig, frame):
    global _RUNNING
    log("[main] 收到退出信号，正在停止...")
    _RUNNING = False


def main():
    global _RUNNING
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--once",   action="store_true", help="单次扫描后退出")
    parser.add_argument("--status", action="store_true", help="查看当前模拟持仓")
    parser.add_argument("--reset",  action="store_true", help="清空模拟状态")
    args = parser.parse_args()

    if args.status:
        print_status()
        return

    if args.reset:
        state = _DEFAULT_STATE()
        _save_state(state)
        print("模拟FX状态已重置")
        return

    log("[main] paper_scanner_fx 启动（信号体系与主工作流 fx/ 完全对齐）")
    log(f"[main] 状态文件: {PAPER_FX_STATE}")
    log(f"[main] 信号逻辑: fx/scanner.analyze_symbol → judge_reversal/breakout/pullback")
    log(f"[main] SL/TP: fx/shared/calc_sl_tp.calc_sl_tp")
    log(f"[main] 仓位: 固定 {POS_SIZE_U}U，无持仓上限（测试模式）")

    if args.once:
        run_scan()
        return

    # daemon 模式
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    # 启动 SL/TP 心跳线程
    sl_thread = threading.Thread(target=_sl_tp_loop, daemon=True)
    sl_thread.start()

    # 主扫描循环
    while _RUNNING:
        try:
            run_scan()
        except Exception as e:
            log(f"[main] 扫描异常: {e}")
        if not _RUNNING:
            break
        log(f"[main] 下次扫描约 {SCAN_INTERVAL//60} 分钟后")
        for _ in range(SCAN_INTERVAL):
            if not _RUNNING:
                break
            time.sleep(1)

    log("[main] paper_scanner_fx 已停止")


if __name__ == "__main__":
    main()
