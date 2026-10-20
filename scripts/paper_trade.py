#!/usr/bin/env python3
"""
paper_trade.py — 模拟开仓框架
=====================================
完全不调用真实下单接口，所有"成交"都写入 paper_state.json。
复用现有评分逻辑（entry_logic.py / short_scanner.py scoring），
但用模拟成交代替 place_order / place_algo_order。

用法:
  python3 scripts/paper_trade.py          # 单次扫描，输出信号 + 模拟开仓
  python3 scripts/paper_trade.py --status  # 查看当前模拟仓位盈亏
  python3 scripts/paper_trade.py --close <SYM>  # 模拟平仓
  python3 scripts/paper_trade.py --reset   # 清空所有模拟仓位
"""

import sys, os, json, time, datetime, argparse
sys.path.insert(0, "/root/.openclaw/workspace")

import binance_api as api
from entry_logic import (
    rsi_calc, atr_calc, ema_calc, fmt_price, fmt_qty,
    calc_score_trend, calc_score_reversal, classify_signal,
    liquidation_map, calc_entry_mode
)

# ── 配置 ──────────────────────────────────────────────────────────────────────
PAPER_STATE_FILE = "/root/.openclaw/workspace/paper_state.json"
NOTIFY_TO = "432401404:thread:019fadbf-37d4-710e-bda4-d7816642e957"
PAPER_NOTIONAL  = 1000   # 模拟名义仓位 U
PAPER_LEVERAGE  = 50
SCORE_THRESHOLD = 75     # 模拟开仓门槛
MAX_POSITIONS   = 5      # 最多同时持有模拟仓位数
RR_MIN          = 1.5    # 最低盈亏比

# 扫描标的（与原 short_scanner 保持一致）
SCAN_SYMBOLS = [
    "BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT",
    "DOGEUSDT","ADAUSDT","AVAXUSDT","DOTUSDT","LINKUSDT",
    "MATICUSDT","UNIUSDT","ATOMUSDT","LTCUSDT","ETCUSDT",
    "AAVEUSDT","SUSHIUSDT","COMPUSDT","MKRUSDT","YFIUSDT",
    "NEARUSDT","FTMUSDT","ALGOUSDT","HBARUSDT","XLMUSDT",
    "VETUSDT","ICPUSDT","FILUSDT","TRXUSDT","APEUSDT",
]

# ── State I/O ─────────────────────────────────────────────────────────────────
def load_state() -> dict:
    import pathlib
    f = pathlib.Path(PAPER_STATE_FILE)
    if f.exists():
        try:
            return json.loads(f.read_text())
        except Exception:
            pass
    return {"positions": {}, "history": [], "stats": {"total_trades": 0, "wins": 0, "losses": 0, "total_pnl": 0.0}}

def save_state(state: dict):
    import pathlib
    pathlib.Path(PAPER_STATE_FILE).write_text(json.dumps(state, indent=2, ensure_ascii=False))

# ── 通知 ──────────────────────────────────────────────────────────────────────
def notify(msg: str):
    try:
        import subprocess
        subprocess.Popen([
            "openclaw", "message", "send",
            "--channel", "jarvis",
            "--to", NOTIFY_TO,
            "--message", msg,
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        print(msg)

# ── 扫描 + 评分 ───────────────────────────────────────────────────────────────
def scan_symbol(sym: str) -> dict | None:
    """对单个币评分，返回信号 dict 或 None"""
    try:
        # K线
        klines1h = api.get_klines(sym, "1h", 50)
        klines4h = api.get_klines(sym, "4h", 50)
        if not klines1h or not klines4h:
            return None

        closes1h = [float(k[4]) for k in klines1h]
        closes4h = [float(k[4]) for k in klines4h]
        highs1h  = [float(k[2]) for k in klines1h]
        lows1h   = [float(k[3]) for k in klines1h]

        rsi1h = rsi_calc(closes1h, 14)
        rsi4h = rsi_calc(closes4h, 14)
        atr1h = atr_calc(highs1h, lows1h, closes1h, 14)
        ema20 = ema_calc(closes1h, 20)[-1]

        mark_data = api.get_mark_price(sym)
        mark = float(mark_data["markPrice"])
        fr   = float(mark_data.get("lastFundingRate", 0)) * 100

        # 7日涨幅（强趋势保护）
        klines1d = api.get_klines(sym, "1d", 8)
        low7d  = min(float(k[3]) for k in klines1d[-7:])
        high7d = max(float(k[2]) for k in klines1d[-7:])
        change7d = (mark - low7d) / low7d * 100

        # 强趋势过滤：7日涨幅 > 80% 且价格在 EMA20 上方 > 10% → 禁止做空
        ema_dev = (mark - ema20) / ema20 * 100
        meme_protect = change7d > 80 and ema_dev > 10

        # 评分
        sig = classify_signal(rsi1h, rsi4h, fr)
        if sig == "reversal_short":
            score, details = calc_score_reversal(rsi1h, rsi4h, fr, "SHORT")
        elif sig == "trend_short":
            score, details = calc_score_trend(rsi1h, rsi4h, fr, "SHORT")
        else:
            return None

        if score < SCORE_THRESHOLD:
            return None

        if meme_protect:
            return None   # 强趋势保护，跳过做空

        # 入场价 / 止损 / 止盈
        try:
            liq = liquidation_map(sym, "SHORT")
            sl_price  = liq.get("sl_price",  mark * 1.06)
            tp1_price = liq.get("tp1_price", mark * 0.94)
        except Exception:
            sl_price  = round(mark * 1.06, 6)
            tp1_price = round(mark * 0.94, 6)

        # ATR 止损保护：止损不得小于 ATR×1.5
        atr_sl = mark + atr1h * 1.5
        sl_price = max(sl_price, atr_sl)

        entry = mark
        sl_dist  = (sl_price - entry) / entry * 100
        tp_dist  = (entry - tp1_price) / entry * 100
        rr = tp_dist / sl_dist if sl_dist > 0 else 0

        if rr < RR_MIN:
            return None   # RR 不足，跳过

        return {
            "symbol":    sym,
            "direction": "SHORT",
            "score":     score,
            "sig_type":  sig,
            "entry":     entry,
            "sl_price":  round(sl_price, 6),
            "tp1_price": round(tp1_price, 6),
            "rsi1h":     rsi1h,
            "rsi4h":     rsi4h,
            "fr":        fr,
            "rr":        round(rr, 2),
            "ema_dev":   round(ema_dev, 2),
            "change7d":  round(change7d, 2),
            "atr1h":     round(atr1h, 4),
        }
    except Exception as e:
        return None


# ── 模拟开仓 ──────────────────────────────────────────────────────────────────
def paper_open(signal: dict, state: dict) -> bool:
    sym = signal["symbol"]
    if sym in state["positions"]:
        return False  # 已有模拟仓位
    if len(state["positions"]) >= MAX_POSITIONS:
        return False

    now_str = datetime.datetime.utcnow().strftime("%m-%d %H:%M UTC")
    state["positions"][sym] = {
        "direction":  signal["direction"],
        "entry":      signal["entry"],
        "sl_price":   signal["sl_price"],
        "tp1_price":  signal["tp1_price"],
        "score":      signal["score"],
        "sig_type":   signal["sig_type"],
        "rr":         signal["rr"],
        "notional":   PAPER_NOTIONAL,
        "leverage":   PAPER_LEVERAGE,
        "opened_at":  now_str,
        "rsi1h":      signal["rsi1h"],
        "rsi4h":      signal["rsi4h"],
        "fr":         signal["fr"],
        "change7d":   signal["change7d"],
        "atr1h":      signal["atr1h"],
    }
    save_state(state)

    notify(
        f"📋 **[模拟] {sym} SHORT 已开仓** | {now_str}\n\n"
        f"评分：{signal['score']} ({signal['sig_type']})\n"
        f"入场：{signal['entry']:.6g}\n"
        f"止盈：{signal['tp1_price']:.6g}  止损：{signal['sl_price']:.6g}\n"
        f"RR=1:{signal['rr']}  ATR={signal['atr1h']}  EMA偏离={signal['ema_dev']:+.1f}%\n"
        f"7日涨幅={signal['change7d']:+.1f}%  FR={signal['fr']:+.3f}%\n"
        f"名义 {PAPER_NOTIONAL}U × {PAPER_LEVERAGE}x  ⚠️ 仅模拟，不动本金"
    )
    return True


# ── 查看模拟仓位状态 ──────────────────────────────────────────────────────────
def show_status():
    state = load_state()
    positions = state.get("positions", {})
    stats = state.get("stats", {})

    if not positions:
        print("📋 当前无模拟仓位")
    else:
        print(f"📋 模拟仓位 ({len(positions)} 个):\n")
        total_upnl = 0.0
        for sym, pos in positions.items():
            try:
                mark = float(api.get_mark_price(sym)["markPrice"])
            except Exception:
                mark = pos["entry"]
            entry = pos["entry"]
            direction = pos["direction"]
            notional = pos["notional"]

            pnl_pct = (entry - mark) / entry * 100 if direction == "SHORT" else (mark - entry) / entry * 100
            pnl_u   = notional * PAPER_LEVERAGE * pnl_pct / 100

            sl_hit = (direction == "SHORT" and mark >= pos["sl_price"]) or \
                     (direction == "LONG"  and mark <= pos["sl_price"])
            tp_hit = (direction == "SHORT" and mark <= pos["tp1_price"]) or \
                     (direction == "LONG"  and mark >= pos["tp1_price"])

            status = "🔴 SL触发" if sl_hit else ("🟢 TP触发" if tp_hit else "🟡 持仓中")
            total_upnl += pnl_u

            print(f"  {sym} {direction}: entry={entry:.6g}  now={mark:.6g}  "
                  f"PnL={pnl_u:+.2f}U ({pnl_pct:+.2f}%)  {status}")
            print(f"    SL={pos['sl_price']:.6g}  TP={pos['tp1_price']:.6g}  "
                  f"score={pos['score']}  RR=1:{pos['rr']}  开仓={pos['opened_at']}")
        print(f"\n  模拟总浮盈: {total_upnl:+.2f}U")

    print(f"\n历史统计: 共{stats.get('total_trades',0)}笔  "
          f"胜{stats.get('wins',0)}负{stats.get('losses',0)}  "
          f"总PnL={stats.get('total_pnl',0.0):+.2f}U")


# ── 模拟平仓 ──────────────────────────────────────────────────────────────────
def paper_close(sym: str, reason: str = "manual"):
    state = load_state()
    if sym not in state["positions"]:
        print(f"{sym} 无模拟仓位")
        return

    pos = state["positions"].pop(sym)
    try:
        mark = float(api.get_mark_price(sym)["markPrice"])
    except Exception:
        mark = pos["entry"]

    entry = pos["entry"]
    direction = pos["direction"]
    notional = pos["notional"]
    pnl_pct = (entry - mark) / entry * 100 if direction == "SHORT" else (mark - entry) / entry * 100
    pnl_u   = notional * PAPER_LEVERAGE * pnl_pct / 100

    rec = {**pos, "close_price": mark, "close_at": datetime.datetime.utcnow().strftime("%m-%d %H:%M UTC"),
           "pnl_u": round(pnl_u, 2), "pnl_pct": round(pnl_pct, 2), "reason": reason}
    state.setdefault("history", []).append(rec)

    stats = state.setdefault("stats", {"total_trades": 0, "wins": 0, "losses": 0, "total_pnl": 0.0})
    stats["total_trades"] += 1
    if pnl_u > 0:
        stats["wins"] += 1
    else:
        stats["losses"] += 1
    stats["total_pnl"] = round(stats["total_pnl"] + pnl_u, 2)

    save_state(state)
    emoji = "✅" if pnl_u > 0 else "❌"
    notify(
        f"{emoji} **[模拟] {sym} 平仓** ({reason})\n"
        f"入场={entry:.6g}  出场={mark:.6g}  "
        f"PnL={pnl_u:+.2f}U ({pnl_pct:+.2f}%)\n"
        f"历史胜率={stats['wins']}/{stats['total_trades']}  总PnL={stats['total_pnl']:+.2f}U"
    )
    print(f"{sym} 已模拟平仓 PnL={pnl_u:+.2f}U")


# ── 主流程 ────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--close", type=str, default=None)
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args()

    if args.status:
        show_status()
        return

    if args.close:
        paper_close(args.close.upper())
        return

    if args.reset:
        save_state({"positions": {}, "history": [], "stats": {"total_trades": 0, "wins": 0, "losses": 0, "total_pnl": 0.0}})
        print("模拟仓位已重置")
        return

    # 扫描
    state = load_state()
    now_str = datetime.datetime.utcnow().strftime("%m-%d %H:%M UTC")
    print(f"[{now_str}] 开始模拟扫描 ({len(SCAN_SYMBOLS)} 个标的)...")

    signals = []
    for sym in SCAN_SYMBOLS:
        sig = scan_symbol(sym)
        if sig:
            signals.append(sig)

    signals.sort(key=lambda x: x["score"], reverse=True)
    print(f"发现信号: {len(signals)} 个")

    opened = 0
    for sig in signals:
        if paper_open(sig, state):
            print(f"  [模拟开仓] {sig['symbol']} score={sig['score']} RR=1:{sig['rr']}")
            opened += 1

    if opened == 0 and signals:
        print("  已有足够仓位或无新信号")
    elif not signals:
        print("  本轮无达标信号")

    # 自动检查止盈止损触发
    for sym in list(state["positions"].keys()):
        pos = state["positions"][sym]
        try:
            mark = float(api.get_mark_price(sym)["markPrice"])
        except Exception:
            continue
        direction = pos["direction"]
        if direction == "SHORT":
            if mark >= pos["sl_price"]:
                paper_close(sym, reason="SL触发")
            elif mark <= pos["tp1_price"]:
                paper_close(sym, reason="TP1触发")
        else:
            if mark <= pos["sl_price"]:
                paper_close(sym, reason="SL触发")
            elif mark >= pos["tp1_price"]:
                paper_close(sym, reason="TP1触发")

    show_status()


if __name__ == "__main__":
    main()
