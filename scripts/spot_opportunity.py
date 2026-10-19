#!/usr/bin/env python3
"""
spot_opportunity.py — 现货机会监控（顺带刷量版）

逻辑：
  每次运行扫描 EUL/MIRA/ERA 三个币的技术面
  若出现买入信号（超卖反弹 + 支撑确认），自动：
    1. 从合约转 X U 到现货
    2. 限价买入（挂在 ask，快速成交）
    3. 挂止盈限价卖单（买入均价 × TP_MULT）
    4. 通知用户

买入条件（同时满足）：
  - RSI 5m < 35（超卖区）
  - RSI 15m < 45（中周期不在高位）
  - 价格在近30根1m K线的低点附近（支撑位）
  - 1m 出现下影针或小阳线（反弹确认）
  - spread < 0.5%
  - 不在强趋势下跌中（近5根1m均不是连续新低）

止盈：买入均价 × TP_MULT（默认 1.5%，即 0.15% 覆盖手续费 + 1.35% 盈利）
止损：跌超买入均价 × SL_PCT 则限价清仓

运行方式：通过 cron 每 15 分钟触发一次，有信号才操作
"""
import sys, os, time, math, json, datetime, requests
sys.path.insert(0, "/root/.openclaw/workspace")
import binance_api as api

SPOT_BASE  = "https://api.binance.com"
FAPI_BASE  = "https://fapi.binance.com"
PROXY      = {"http": api.PROXY_URL, "https": api.PROXY_URL}

# ── 监控标的 ──────────────────────────────────────────────────────────────────
SYMBOLS = ["EULUSDT", "MIRAUSDT", "ERAUSDT"]

# ── 交易参数 ──────────────────────────────────────────────────────────────────
BUY_USDT   = 200.0   # 每次买入金额（U）
TP_MULT    = 1.015   # 止盈倍数（买入均价 × 1.015 = +1.5%）
SL_MULT    = 0.985   # 止损倍数（买入均价 × 0.985 = -1.5%）
MAX_HOLD_H = 6       # 最长持有小时，超过则挂均价卖出

# ── 信号阈值 ──────────────────────────────────────────────────────────────────
RSI_5M_BUY    = 35   # RSI 5m 低于此值才考虑买入
RSI_15M_MAX   = 50   # RSI 15m 不超过此值（不在大周期高位）
NEAR_LOW_PCT  = 1.5  # 价格在近30根1m低点上方不超过此%
SHADOW_RATIO  = 1.5  # 下影线长度 / 实体长度 >= 此值认为有插针反弹

STATE_FILE = "/root/.openclaw/workspace/spot_opportunity_state.json"
LOG_FILE   = "/root/.openclaw/workspace/daily_task_log.json"


def notify(msg):
    print(f"__NOTIFY__:{msg}")


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


def get_book(sym):
    r = requests.get(f"{SPOT_BASE}/api/v3/ticker/bookTicker",
                     params={"symbol": sym}, timeout=5, proxies=PROXY)
    d = r.json()
    return float(d["bidPrice"]), float(d["askPrice"])


def get_klines(sym, interval, limit):
    r = requests.get(f"{SPOT_BASE}/api/v3/klines",
                     params={"symbol": sym, "interval": interval, "limit": limit},
                     timeout=8, proxies=PROXY)
    return r.json()


def rsi(closes, n=14):
    if len(closes) <= n: return 50
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0)); losses.append(max(-d, 0))
    ag = sum(gains[-n:]) / n; al = sum(losses[-n:]) / n
    if al == 0: return 100
    return 100 - 100 / (1 + ag / al)


def get_sym_info(sym):
    r = requests.get(f"{SPOT_BASE}/api/v3/exchangeInfo",
                     params={"symbol": sym}, timeout=8, proxies=PROXY)
    s = r.json()["symbols"][0]
    filters = {f["filterType"]: f for f in s["filters"]}
    tick_str = filters.get("PRICE_FILTER", {}).get("tickSize", "0.001")
    step_str = filters.get("LOT_SIZE", {}).get("stepSize", "1")
    min_qty  = float(filters.get("LOT_SIZE", {}).get("minQty", "1"))
    return tick_str, step_str, min_qty, s["baseAsset"]


def fmt_qty(qty, step_f, step_dec):
    return round(math.floor(qty / step_f) * step_f, step_dec)


def fmt_price(price, tick_f, tick_dec):
    return round(round(price / tick_f) * tick_f, tick_dec)


def spot_balance(asset="USDT"):
    acc = api.request("GET", f"{SPOT_BASE}/api/v3/account")
    b = next((x for x in acc.get("balances", []) if x["asset"] == asset), {})
    return float(b.get("free", 0))


def futures_available():
    bal = api.request("GET", f"{FAPI_BASE}/fapi/v2/balance")
    b = next((x for x in bal if x["asset"] == "USDT"), {})
    return float(b.get("availableBalance", 0))


def transfer_to_spot(amount):
    r = api.request("POST", f"{SPOT_BASE}/sapi/v1/futures/transfer",
                    {"asset": "USDT", "amount": str(amount), "type": 2})
    return r.get("tranId")


def transfer_to_futures(amount):
    r = api.request("POST", f"{SPOT_BASE}/sapi/v1/futures/transfer",
                    {"asset": "USDT", "amount": str(amount), "type": 1})
    return r.get("tranId")


def place_limit(sym, side, qty, price):
    return api.request("POST", f"{SPOT_BASE}/api/v3/order", {
        "symbol": sym, "side": side, "type": "LIMIT",
        "timeInForce": "GTC", "quantity": str(qty), "price": str(price)
    })


def cancel_order(sym, oid):
    try:
        return api.request("DELETE", f"{SPOT_BASE}/api/v3/order",
                           {"symbol": sym, "orderId": oid})
    except: return {}


def get_order(sym, oid):
    try:
        return api.request("GET", f"{SPOT_BASE}/api/v3/order",
                           {"symbol": sym, "orderId": oid})
    except: return {}


# ── 技术面分析 ────────────────────────────────────────────────────────────────
def analyze(sym):
    """
    返回 (signal, reason, cur_price)
    signal: "BUY" / "SKIP" / "ERROR"
    """
    try:
        k1m  = get_klines(sym, "1m",  35)
        k5m  = get_klines(sym, "5m",  25)
        k15m = get_klines(sym, "15m", 20)

        c1m = [float(k[4]) for k in k1m]
        o1m = [float(k[1]) for k in k1m]
        h1m = [float(k[2]) for k in k1m]
        l1m = [float(k[3]) for k in k1m]
        c5m  = [float(k[4]) for k in k5m]
        c15m = [float(k[4]) for k in k15m]

        bid, ask = get_book(sym)
        spread   = (ask - bid) / bid * 100

        rsi_5m  = rsi(c5m)
        rsi_15m = rsi(c15m)
        cur     = c1m[-1]

        # 近30根低点
        low_30  = min(l1m[-30:])
        near_low = (cur - low_30) / low_30 * 100  # 当前价比低点高多少%

        # 最后一根 K 线形态（下影针 or 小阳线）
        last_o = o1m[-1]; last_c = c1m[-1]
        last_h = h1m[-1]; last_l = l1m[-1]
        body   = abs(last_c - last_o)
        lower_shadow = min(last_o, last_c) - last_l
        has_pin = (body > 0 and lower_shadow / body >= SHADOW_RATIO) or \
                  (last_c > last_o and body / (last_h - last_l + 1e-10) < 0.5)

        # 连续下跌检测（近5根1m连续创新低）
        strong_down = all(l1m[-i-1] < l1m[-i-2] for i in range(5))

        reasons = []

        # ── 买入条件 ──
        ok = True
        if spread > 0.5:
            ok = False; reasons.append(f"spread={spread:.3f}%过大")
        if rsi_5m > RSI_5M_BUY:
            ok = False; reasons.append(f"RSI5m={rsi_5m:.1f}>{RSI_5M_BUY}不够超卖")
        if rsi_15m > RSI_15M_MAX:
            ok = False; reasons.append(f"RSI15m={rsi_15m:.1f}>{RSI_15M_MAX}中周期偏高")
        if near_low > NEAR_LOW_PCT:
            ok = False; reasons.append(f"距低点{near_low:.2f}%>阈值{NEAR_LOW_PCT}%")
        if not has_pin:
            ok = False; reasons.append("无反弹K线确认（需下影针或小阳线）")
        if strong_down:
            ok = False; reasons.append("仍在连续下跌中")

        if ok:
            return "BUY", (f"✅ 买入信号 RSI5m={rsi_5m:.1f} RSI15m={rsi_15m:.1f} "
                           f"距低点{near_low:.2f}% 有插针"), cur
        else:
            return "SKIP", f"SKIP({'; '.join(reasons)}) RSI5m={rsi_5m:.1f} 价格={cur}", cur

    except Exception as e:
        return "ERROR", f"分析异常:{e}", 0


# ── 持仓检查 & 止盈止损 ───────────────────────────────────────────────────────
def check_positions(state):
    """检查已有仓位的止盈/止损情况，返回更新后的 state"""
    changed = False
    now = time.time()

    for sym in list(state.get("positions", {}).keys()):
        pos = state["positions"][sym]
        tp_oid  = pos.get("tp_order_id")
        avg_buy = pos.get("avg_buy", 0)
        qty     = pos.get("qty", 0)
        buy_ts  = pos.get("buy_ts", 0)
        base    = pos.get("base_asset", "")

        # 1. 检查止盈单是否成交
        if tp_oid:
            o = get_order(sym, tp_oid)
            st = o.get("status", "")
            if st == "FILLED":
                rev = float(o.get("cummulativeQuoteQty", 0))
                pnl = rev - pos["buy_cost"] - pos["buy_cost"]*0.001 - rev*0.001
                msg = (f"🎯 **{sym} 止盈成交**\n"
                       f"卖出: {o.get('executedQty')}枚 @ {o.get('price')}\n"
                       f"收入: {rev:.3f}U  盈利: {pnl:+.3f}U")
                notify(msg)
                del state["positions"][sym]
                changed = True
                continue
            elif st in ("CANCELED", "EXPIRED"):
                # 止盈单被撤，重新挂
                pos["tp_order_id"] = None
                changed = True

        # 2. 止损检查
        try:
            bid, ask = get_book(sym)
            cur = bid
            sl_price = avg_buy * SL_MULT
            if cur < sl_price:
                # 触发止损，限价清仓
                print(f"[止损] {sym} 当前{cur:.6f} < 止损{sl_price:.6f}，挂限价清仓")
                if tp_oid:
                    cancel_order(sym, tp_oid)
                tick_str, step_str, min_qty, base_a = get_sym_info(sym)
                tick_f = float(tick_str); tick_dec = max(0,-int(math.floor(math.log10(tick_f)))) if tick_f<1 else 0
                step_f = float(step_str); step_dec = max(0,-int(math.floor(math.log10(step_f)))) if step_f<1 else 0
                token_free = spot_balance(base_a)
                sl_qty = fmt_qty(min(qty, token_free), step_f, step_dec)
                sl_p = fmt_price(bid, tick_f, tick_dec)
                r = place_limit(sym, "SELL", sl_qty, sl_p)
                sl_oid = r.get("orderId")
                msg = (f"🛑 **{sym} 触发止损**\n"
                       f"均价{avg_buy:.6f} → 止损{sl_p:.6f}（-{(1-SL_MULT)*100:.1f}%）\n"
                       f"限价清仓单已挂 orderId={sl_oid}")
                notify(msg)
                del state["positions"][sym]
                changed = True
                continue
        except: pass

        # 3. 超时持仓（超过 MAX_HOLD_H 小时），挂均价卖单
        if now - buy_ts > MAX_HOLD_H * 3600 and not tp_oid:
            print(f"[超时] {sym} 持仓超{MAX_HOLD_H}h，挂均价卖出")
            tick_str, step_str, min_qty, base_a = get_sym_info(sym)
            tick_f = float(tick_str); tick_dec = max(0,-int(math.floor(math.log10(tick_f)))) if tick_f<1 else 0
            step_f = float(step_str); step_dec = max(0,-int(math.floor(math.log10(step_f)))) if step_f<1 else 0
            token_free = spot_balance(base_a)
            sell_qty = fmt_qty(min(qty, token_free), step_f, step_dec)
            _, ask = get_book(sym)
            sell_p  = fmt_price(max(avg_buy * 1.001, ask), tick_f, tick_dec)
            r = place_limit(sym, "SELL", sell_qty, sell_p)
            pos["tp_order_id"] = r.get("orderId")
            pos["tp_price"]    = sell_p
            changed = True
            notify(f"⏰ {sym} 超时挂均价卖单 @ {sell_p}")
            continue

        # 4. 若止盈单不存在，重新挂
        if not tp_oid and qty > 0:
            tick_str, step_str, min_qty, base_a = get_sym_info(sym)
            tick_f = float(tick_str); tick_dec = max(0,-int(math.floor(math.log10(tick_f)))) if tick_f<1 else 0
            step_f = float(step_str); step_dec = max(0,-int(math.floor(math.log10(step_f)))) if step_f<1 else 0
            token_free = spot_balance(base_a)
            sell_qty = fmt_qty(min(qty, token_free), step_f, step_dec)
            _, ask = get_book(sym)
            tp_price = fmt_price(max(avg_buy * TP_MULT, ask), tick_f, tick_dec)
            r = place_limit(sym, "SELL", sell_qty, tp_price)
            pos["tp_order_id"] = r.get("orderId")
            pos["tp_price"]    = tp_price
            changed = True
            print(f"[重挂止盈] {sym} @ {tp_price}")

    if changed:
        save_state(state)
    return state


# ── 买入执行 ──────────────────────────────────────────────────────────────────
def do_buy(sym, cur_price, state):
    tick_str, step_str, min_qty, base_asset = get_sym_info(sym)
    tick_f   = float(tick_str)
    step_f   = float(step_str)
    step_dec = max(0, -int(math.floor(math.log10(step_f)))) if step_f < 1 else 0
    tick_dec = max(0, -int(math.floor(math.log10(tick_f)))) if tick_f < 1 else 0

    # 从合约转资金
    fapi_avail = futures_available()
    pos = api.get_positions()
    maint = sum(float(p.get("maintMargin", 0)) for p in pos
                if abs(float(p.get("positionAmt", 0))) > 0)
    safe_keep = maint * 3
    transferable = fapi_avail - safe_keep

    if transferable < BUY_USDT:
        notify(f"⚠️ {sym} 合约可转{transferable:.1f}U < 买入需求{BUY_USDT}U，跳过")
        return False

    tid = transfer_to_spot(round(BUY_USDT + 2, 2))
    if not tid:
        notify(f"⚠️ {sym} 划转失败，跳过")
        return False
    time.sleep(3)

    # 限价买入（挂 ask 价，快速成交）
    bid, ask = get_book(sym)
    buy_price = fmt_price(ask, tick_f, tick_dec)
    qty_raw = BUY_USDT / buy_price
    qty = fmt_qty(qty_raw, step_f, step_dec)
    if qty < min_qty: qty = min_qty

    r_buy = place_limit(sym, "BUY", qty, buy_price)
    buy_id = r_buy.get("orderId")
    if not buy_id:
        notify(f"⚠️ {sym} 买单失败: {r_buy.get('msg','?')}")
        # 转回合约
        u = spot_balance("USDT")
        if u > 1: transfer_to_futures(round(u - 0.5, 2))
        return False

    # 等待成交（最多 15s）
    filled_qty = 0.0; buy_cost = 0.0
    for _ in range(15):
        time.sleep(1)
        o = get_order(sym, buy_id)
        if o.get("status") == "FILLED":
            filled_qty = float(o.get("executedQty", 0))
            buy_cost   = float(o.get("cummulativeQuoteQty", 0))
            break
    if filled_qty == 0:
        cancel_order(sym, buy_id)
        time.sleep(0.5)
        o = get_order(sym, buy_id)
        filled_qty = float(o.get("executedQty", 0))
        buy_cost   = float(o.get("cummulativeQuoteQty", 0))

    if filled_qty <= 0:
        notify(f"⚠️ {sym} 买单未成交，资金转回")
        u = spot_balance("USDT")
        if u > 1: transfer_to_futures(round(u - 0.5, 2))
        return False

    avg_buy = buy_cost / filled_qty
    time.sleep(2)  # 等 token settle

    # 挂止盈限价单
    _, ask2   = get_book(sym)
    tp_price  = fmt_price(max(avg_buy * TP_MULT, ask2 + tick_f), tick_f, tick_dec)
    sell_qty  = fmt_qty(filled_qty, step_f, step_dec)
    r_tp      = place_limit(sym, "SELL", sell_qty, tp_price)
    tp_oid    = r_tp.get("orderId")

    # 剩余 USDT 转回合约
    u = spot_balance("USDT")
    if u > 1:
        transfer_to_futures(round(u - 0.5, 2))

    # 保存仓位
    state.setdefault("positions", {})[sym] = {
        "avg_buy":     avg_buy,
        "qty":         filled_qty,
        "buy_cost":    buy_cost,
        "tp_price":    tp_price,
        "tp_order_id": tp_oid,
        "sl_price":    avg_buy * SL_MULT,
        "base_asset":  base_asset,
        "buy_ts":      time.time(),
    }
    save_state(state)

    pct_tp = (TP_MULT - 1) * 100
    pct_sl = (1 - SL_MULT) * 100
    msg = (f"🟢 **{sym} 买入成功**\n"
           f"均价: {avg_buy:.6f}  数量: {filled_qty:.2f}\n"
           f"止盈: {tp_price:.6f} (+{pct_tp:.2f}%)\n"
           f"止损: {avg_buy*SL_MULT:.6f} (-{pct_sl:.2f}%)\n"
           f"已挂止盈单 orderId={tp_oid}")
    notify(msg)
    return True


# ── 主入口 ────────────────────────────────────────────────────────────────────
def main():
    ts = datetime.datetime.utcnow().strftime("%H:%M UTC")
    print(f"[{ts}] spot_opportunity 扫描 {SYMBOLS}")

    state = load_state()

    # 1. 先检查已有持仓的止盈/止损
    state = check_positions(state)

    # 2. 扫描新买入机会
    for sym in SYMBOLS:
        # 已有仓位则跳过
        if sym in state.get("positions", {}):
            print(f"  {sym}: 已有持仓，跳过扫描")
            continue

        sig, reason, cur_price = analyze(sym)
        print(f"  {sym}: {reason}")

        if sig == "BUY":
            print(f"  ➡️ 执行买入 {sym} @ ~{cur_price:.6f}")
            do_buy(sym, cur_price, state)

    # 3. 打印持仓摘要
    positions = state.get("positions", {})
    if positions:
        print(f"\n当前持仓 ({len(positions)}个):")
        for sym, pos in positions.items():
            bid, _ = get_book(sym)
            pnl_pct = (bid / pos['avg_buy'] - 1) * 100
            print(f"  {sym}: 均价={pos['avg_buy']:.6f} 现价={bid:.6f} "
                  f"盈亏={pnl_pct:+.2f}% 止盈={pos['tp_price']:.6f}")
    else:
        print("当前无持仓")
        print("HEARTBEAT_OK")

    # 4. 清扫现货多余 USDT → 转回合约（每次扫描结束执行）
    try:
        leftover = spot_balance("USDT")
        if leftover > 5:
            back = round(leftover - 0.5, 2)
            r = transfer_to_futures(back)
            if r:
                print(f"[清扫] 现货多余 {back}U 已转回合约")
    except Exception as e:
        print(f"[清扫] 转回合约失败: {e}")


if __name__ == "__main__":
    main()
