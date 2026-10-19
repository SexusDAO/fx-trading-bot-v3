#!/usr/bin/env python3
"""
BANK 仓位专用 5 分钟巡检脚本
- 检查止盈单是否存活，丢失则重挂
- 根据当前价格和 ATR 动态调整止盈位（只上移，不下移，锁住浮盈）
- 判断是否满足二次补仓条件（最大仓位 1500U）
- 输出简洁通知
"""
import sys, json, time
sys.path.insert(0, "/root/.openclaw/workspace")
import binance_api as api

SYM = "BANKUSDT"
MAX_NOTIONAL = 1500.0  # 最大仓位 U
TP_FILE = "/root/.openclaw/workspace/tp_targets.json"

# ── 工具函数 ──────────────────────────────────────────────────────────────────
def load_tp():
    try:
        with open(TP_FILE) as f:
            return json.load(f).get(SYM, {})
    except:
        return {}

def save_tp(data: dict):
    try:
        with open(TP_FILE) as f:
            td = json.load(f)
    except:
        td = {}
    td[SYM] = data
    with open(TP_FILE, "w") as f:
        json.dump(td, f, ensure_ascii=False, indent=2)

def round_tick(price, tick_str):
    tick = float(tick_str)
    dec = len(tick_str.rstrip("0").split(".")[-1]) if "." in tick_str else 0
    return round(round(price / tick) * tick, dec)

def get_tick_size():
    info = api._signed_request("GET", "https://fapi.binance.com/fapi/v1/exchangeInfo", {})
    for s in info.get("symbols", []):
        if s["symbol"] == SYM:
            for f in s["filters"]:
                if f["filterType"] == "PRICE_FILTER":
                    return f["tickSize"]
    return "0.00001"

def get_atr(period=14):
    klines = api.get_klines(SYM, "1h", period + 5)
    trs = [abs(float(k[2]) - float(k[3])) for k in klines]
    return sum(trs[-period:]) / period

def get_position():
    pos = api.get_positions()
    for p in pos:
        if p["symbol"] == SYM and float(p.get("positionAmt", 0)) != 0:
            return p
    return None

def get_rsi(period=14):
    klines = api.get_klines(SYM, "1h", period + 5)
    closes = [float(k[4]) for k in klines]
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_g = sum(gains[-period:]) / period
    avg_l = sum(losses[-period:]) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return round(100 - 100 / (1 + rs), 1)

# ── 主逻辑 ────────────────────────────────────────────────────────────────────
def main():
    pos = get_position()
    if not pos:
        print("BANK 仓位已平，巡检结束")
        return

    entry   = float(pos["entryPrice"])
    qty     = abs(float(pos["positionAmt"]))
    notional = abs(float(pos["notional"]))
    mark    = float(pos["markPrice"])
    pnl     = float(pos["unRealizedProfit"])
    pnl_pct = pnl / notional * 100

    tick  = get_tick_size()
    atr   = get_atr(14)
    rsi1h = get_rsi(14)

    td = load_tp()
    cur_tp     = td.get("tp", 0)
    tp_algo_id = td.get("algo_tp_id")

    # ── 1. 检查止盈单是否存活 ────────────────────────────────────────────────
    algos = api.get_algo_open_orders(SYM)
    alive_ids = {a.get("algoId") for a in algos}
    tp_alive = tp_algo_id and tp_algo_id in alive_ids

    # ── 2. 动态止盈：只向有利方向移动（空单：价格下跌则上移止盈） ────────────
    new_tp = round_tick(entry - atr * 3.0, tick)

    # 若价格已下跌超过 ATR，可以把止盈上移锁住浮盈
    # 空单止盈越高越保守，越低越激进；我们允许上移（收紧）但不允许下移（放宽）
    if cur_tp > 0 and new_tp > cur_tp:
        # new_tp 更高 = 更保守 = 锁住了更多浮盈 → 上移
        should_update_tp = True
        reason = f"ATR×3 上移止盈 {cur_tp:.5f}→{new_tp:.5f}"
    else:
        should_update_tp = not tp_alive  # 只有止盈单丢失时才重挂
        reason = "止盈单丢失，重挂" if not tp_alive else "止盈单存活，无需调整"
        if tp_alive:
            new_tp = cur_tp  # 保持原位

    tp_pnl = round((entry - new_tp) / entry * notional, 1)

    # ── 3. 执行止盈更新 ──────────────────────────────────────────────────────
    update_msg = ""
    if should_update_tp:
        # 先撤旧单
        if tp_algo_id:
            try:
                api.cancel_algo_order(SYM, tp_algo_id)
                time.sleep(0.5)
            except:
                pass
        r = api.place_algo_order(SYM, "BUY", "TAKE_PROFIT_MARKET", new_tp, "SHORT")
        new_id = r.get("algoId")
        update_msg = f"✅ 止盈更新: {new_tp:.5f}  预估+{tp_pnl}U  ({reason})"
        td.update({
            "tp": new_tp, "tp_pnl": tp_pnl,
            "algo_tp_id": new_id, "entry": entry,
            "qty": qty, "notional": round(notional, 2),
            "atr_1h": round(atr, 6),
            "side": "SHORT"
        })
        save_tp(td)
    else:
        update_msg = f"✅ 止盈单正常 @ {cur_tp:.5f}  预估+{td.get('tp_pnl','?')}U"

    # ── 4. 补仓判断 ──────────────────────────────────────────────────────────
    add_pos_msg = ""
    remaining_room = MAX_NOTIONAL - notional
    price_vs_entry = (mark - entry) / entry * 100  # 正=价格比均价高(空单亏损)

    if notional >= MAX_NOTIONAL * 0.95:
        add_pos_msg = "🚫 仓位已达上限 1500U，不补仓"
    elif price_vs_entry > 5:
        # 还在亏损扩大区，看 RSI 是否极端超卖
        if rsi1h <= 30:
            add_pos_msg = (
                f"⚠️ 1H RSI={rsi1h} 已超卖，可考虑补仓约 {remaining_room:.0f}U\n"
                f"  建议补仓价：市价 {mark:.5f}（均价将拉高至约 {(entry*notional+mark*remaining_room)/(notional+remaining_room):.5f}）"
            )
        else:
            add_pos_msg = (
                f"⏸ 暂不补仓：现价 {mark:.5f} 比均价高 {price_vs_entry:.1f}%，"
                f"RSI={rsi1h} 未到超卖，等信号"
            )
    elif price_vs_entry <= -2:
        # 价格已回落到均价附近或以下 → 顺势补仓机会
        add_pos_msg = (
            f"💡 价格回落至均价附近（偏差 {price_vs_entry:.1f}%），RSI={rsi1h}，"
            f"可补仓 {remaining_room:.0f}U 摊低成本"
        )
    else:
        add_pos_msg = f"⏸ 暂不补仓：价格偏离均价 {price_vs_entry:.1f}%，RSI={rsi1h}，继续观察"

    # ── 5. 输出通知 ──────────────────────────────────────────────────────────
    direction = "亏" if pnl < 0 else "盈"
    print(
        f"📊 BANK 巡检\n"
        f"  现价 {mark:.5f}  均价 {entry:.5f}  浮{direction} {pnl:+.2f}U ({pnl_pct:.1f}%)\n"
        f"  仓位名义 {notional:.1f}U  ATR={atr:.5f}  RSI1H={rsi1h}\n"
        f"  {update_msg}\n"
        f"  {add_pos_msg}"
    )

if __name__ == "__main__":
    main()
