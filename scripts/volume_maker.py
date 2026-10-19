#!/usr/bin/env python3
"""
volume_maker.py — 现货刷量脚本（网格补仓版 v5）

逻辑：
  1. 每次用 50U 限价买单（挂在 bid，等成交）
  2. 买入后立刻在「当前持仓均价 × (1 + SELL_PREMIUM)」挂一个限价卖单
  3. 如果价格继续下跌超过 GRID_DROP_PCT，再买一笔 50U，更新均价，重新挂卖单
  4. 卖单成交后，一轮结束，统计盈亏
  5. 全程只用限价单，不用市价

目标：低磨损刷量，争取接近平本
"""
import sys, os, time, math, argparse, json, datetime, requests
sys.path.insert(0, "/root/.openclaw/workspace")
import binance_api as api

SPOT_BASE = "https://api.binance.com"
PROXY     = {"http": api.PROXY_URL, "https": api.PROXY_URL}

# ── 参数 ──────────────────────────────────────────────────────────────────────
BUY_EACH       = 50.0    # 每次买入金额 U
GRID_DROP_PCT  = 0.20    # 价格跌超均价多少 % 时补仓（0.20 = 0.20%）
MAX_GRID_LAYERS = 6      # 最多补仓几次（单轮最大持仓 = 50×6 = 300U）
SELL_PREMIUM   = 0.15    # 卖出价比持仓均价高多少 %（覆盖手续费+微盈）
BUY_WAIT_SEC   = 30      # 买单最多等待秒数（bid 挂单，横盘时应该快速成交）
SELL_WAIT_SEC  = 120     # 卖单每次等待秒数
SELL_RETRIES   = 10      # 卖单超时后最多重新挂几次
SELL_STEP_TICKS = 1      # 卖单每次下移 tick 数（微调，不大幅让利）
MAX_TOTAL_LOSS = -10.0   # 整任务累计最大亏损 U，超过终止

# ── 行情过滤 ──────────────────────────────────────────────────────────────────
MAX_VOL_PCT    = 0.80    # 1m 振幅上限
MAX_RSI_UP     = 75
MIN_RSI_DN     = 25
MAX_SPREAD_PCT = 0.50

LOG_FILE = "/root/.openclaw/workspace/daily_task_log.json"


def notify(msg): print(f"__NOTIFY__:{msg}")


def log_result(sym, success, vol, pnl, reason=""):
    try:
        logs = []
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE) as f: logs = json.load(f)
        cutoff = (datetime.datetime.utcnow() - datetime.timedelta(days=30)).isoformat()
        logs = [l for l in logs if l.get("date", "") >= cutoff[:10]]
        logs.append({
            "date": datetime.datetime.utcnow().strftime("%Y-%m-%d"),
            "time": datetime.datetime.utcnow().strftime("%H:%M UTC"),
            "task": f"volume_maker_{sym}",
            "success": success, "vol_done": round(vol, 1),
            "pnl": round(pnl, 4), "reason": reason
        })
        with open(LOG_FILE, "w") as f:
            json.dump(logs, f, indent=2, ensure_ascii=False)
    except: pass


# ── 基础 API 封装 ─────────────────────────────────────────────────────────────
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


def spot_balance(asset="USDT"):
    acc = api.request("GET", f"{SPOT_BASE}/api/v3/account")
    b = next((x for x in acc.get("balances", []) if x["asset"] == asset), {})
    return float(b.get("free", 0))


def get_sym_info(sym):
    r = requests.get(f"{SPOT_BASE}/api/v3/exchangeInfo",
                     params={"symbol": sym}, timeout=8, proxies=PROXY)
    s = r.json()["symbols"][0]
    filters = {f["filterType"]: f for f in s["filters"]}
    tick_str = filters.get("PRICE_FILTER", {}).get("tickSize", "0.001")
    step_str = filters.get("LOT_SIZE", {}).get("stepSize", "1")
    min_qty  = float(filters.get("LOT_SIZE", {}).get("minQty", "1"))
    base     = s["baseAsset"]
    return tick_str, step_str, min_qty, base


def fmt_qty(qty, step_f, step_dec):
    v = math.floor(qty / step_f) * step_f
    return round(v, step_dec)


def fmt_price(price, tick_f, tick_dec):
    return round(round(price / tick_f) * tick_f, tick_dec)


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


def cancel_all_spot(sym):
    try:
        orders = api.request("GET", f"{SPOT_BASE}/api/v3/openOrders", {"symbol": sym})
        for o in orders:
            cancel_order(sym, o["orderId"])
    except: pass


def rsi_calc(closes):
    if len(closes) < 15: return 50
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0)); losses.append(max(-d, 0))
    ag = sum(gains[-14:]) / 14; al = sum(losses[-14:]) / 14
    if al == 0: return 100
    return 100 - 100 / (1 + ag / al)


def check_market(sym):
    try:
        k1m = get_klines(sym, "1m", 15)
        k5m = get_klines(sym, "5m", 20)
        c1m = [float(k[4]) for k in k1m]
        h1m = [float(k[2]) for k in k1m]
        l1m = [float(k[3]) for k in k1m]
        c5m = [float(k[4]) for k in k5m]
        price = c1m[-1]
        avg_rng = sum(h1m[i] - l1m[i] for i in range(-10, 0)) / 10
        vol_pct = avg_rng / price * 100
        rsi5    = rsi_calc(c5m)
        bid, ask = get_book(sym)
        spread  = (ask - bid) / bid * 100
        # 单向趋势检测（4根连续同向）
        strong_up   = all(c1m[-i-1] > c1m[-i-2] for i in range(4))
        strong_down = all(c1m[-i-1] < c1m[-i-2] for i in range(4))
        if vol_pct   > MAX_VOL_PCT:    return False, f"波动过大{vol_pct:.3f}%"
        if rsi5      > MAX_RSI_UP:     return False, f"RSI={rsi5:.1f}偏高（追涨风险）"
        if rsi5      < MIN_RSI_DN:     return False, f"RSI={rsi5:.1f}偏低（跌势风险）"
        if spread    > MAX_SPREAD_PCT: return False, f"spread={spread:.4f}%过大"
        if strong_up:                   return False, "强势上涨，不追"
        if strong_down:                 return False, "连续下跌，暂停"
        return True, f"OK vol={vol_pct:.3f}% RSI={rsi5:.1f} spread={spread:.4f}%"
    except Exception as e:
        return False, f"检测异常:{e}"


# ── 核心：一轮网格买卖 ────────────────────────────────────────────────────────
def do_grid_round(sym, base_asset, tick_f, tick_dec, step_f, step_dec, min_qty, rnd_id):
    """
    一轮：
      - 用 50U 限价单买入（挂 bid）
      - 持有后挂卖单在均价+SELL_PREMIUM
      - 若价格跌超 GRID_DROP_PCT 则补仓，更新均价，重挂卖单
      - 卖单成交后结束
    返回 (vol_done, pnl, ok)
    """
    # ── 持仓状态 ──
    total_qty  = 0.0   # 总持有量
    total_cost = 0.0   # 总成本
    layers     = 0     # 已买层数
    sell_id    = None
    round_vol  = 0.0

    def avg_price():
        return total_cost / total_qty if total_qty > 0 else 0

    def do_buy(label=""):
        """挂一笔限价买单，等待成交，返回 (filled_qty, filled_cost) 或 (0,0)"""
        nonlocal total_qty, total_cost, layers, round_vol

        usdt_free = spot_balance("USDT")
        buy_usdt = min(BUY_EACH, usdt_free * 0.98)
        if buy_usdt < 5:
            print(f"    [买入{label}] USDT不足({usdt_free:.1f}U)，跳过")
            return 0, 0

        bid, ask = get_book(sym)
        # 挂在 ask 价格，立即成交（taker），确保快速买入
        buy_price = fmt_price(ask, tick_f, tick_dec)
        qty_raw = buy_usdt / buy_price
        qty = fmt_qty(qty_raw, step_f, step_dec)
        if qty < min_qty:
            qty = min_qty

        print(f"    [买入{label}] {qty} @ {buy_price}(ask) (≈{buy_usdt:.1f}U)")
        r = place_limit(sym, "BUY", qty, buy_price)
        oid = r.get("orderId")
        if not oid:
            print(f"    [买入{label}] 挂单失败: {r.get('msg','?')}")
            return 0, 0

        # ask 价格的限价单通常立刻成交，等最多5秒
        filled = 0.0; cost = 0.0
        for _ in range(10):
            time.sleep(1)
            o = get_order(sym, oid)
            st = o.get("status", "")
            if st == "FILLED":
                filled = float(o.get("executedQty", 0))
                cost   = float(o.get("cummulativeQuoteQty", 0))
                break
            elif st in ("CANCELED", "EXPIRED", "REJECTED"):
                filled = float(o.get("executedQty", 0))
                cost   = float(o.get("cummulativeQuoteQty", 0))
                break
        else:
            # 10s 还没成交（行情快速上涨导致 ask 变了），撤单重试
            cancel_order(sym, oid)
            time.sleep(0.5)
            o = get_order(sym, oid)
            filled = float(o.get("executedQty", 0))
            cost   = float(o.get("cummulativeQuoteQty", 0))

        if filled > 0:
            total_qty  += filled
            total_cost += cost
            layers     += 1
            round_vol  += cost
            print(f"    [买入{label}] ✅ {filled:.4f}枚 @ 均{cost/filled:.6f} cost={cost:.3f}U | 持仓均价={avg_price():.6f}")
        else:
            print(f"    [买入{label}] 未成交，跳过")

        return filled, cost

    def place_sell():
        """在当前持仓均价 + SELL_PREMIUM 挂卖单"""
        nonlocal sell_id
        if sell_id:
            cancel_order(sym, sell_id)
            sell_id = None

        time.sleep(1)  # 等 token settle

        # 用账户实际 free 余额
        token_free = spot_balance(base_asset)
        sell_qty = fmt_qty(min(total_qty, token_free), step_f, step_dec)
        if sell_qty < min_qty:
            print(f"    [卖单] token free={token_free:.4f} 不足 min_qty={min_qty}")
            return None

        avg = avg_price()
        sell_price = fmt_price(avg * (1 + SELL_PREMIUM / 100), tick_f, tick_dec)

        # 卖价不能低于 ask（否则会立即成交，变成 taker）
        bid, ask = get_book(sym)
        sell_price = fmt_price(max(sell_price, ask), tick_f, tick_dec)

        r = place_limit(sym, "SELL", sell_qty, sell_price)
        oid = r.get("orderId")
        if oid:
            print(f"    [卖单] ✅ 挂出 {sell_qty} @ {sell_price:.6f} (均价={avg:.6f} 溢价={SELL_PREMIUM}%)")
            sell_id = oid
        else:
            print(f"    [卖单] 挂单失败: {r.get('msg','?')}")
        return oid

    # ── 第一层买入 ────────────────────────────────────────────────────────────
    filled, cost = do_buy()
    if filled <= 0:
        return 0, 0, False

    # 挂初始卖单
    place_sell()

    # ── 持仓监控循环 ──────────────────────────────────────────────────────────

    for tick in range(3600):   # 最多等 1 小时
        time.sleep(2)

        # 1. 检查卖单是否成交
        if sell_id:
            o = get_order(sym, sell_id)
            st = o.get("status", "")
            if st == "FILLED":
                sell_rev  = float(o.get("cummulativeQuoteQty", 0))
                sell_qty  = float(o.get("executedQty", 0))
                sell_fee  = sell_rev * 0.001
                buy_fee   = total_cost * 0.001
                round_vol += sell_rev
                pnl = sell_rev - total_cost - buy_fee - sell_fee
                print(f"    [卖单] ✅ 成交 rev={sell_rev:.3f}U pnl={pnl:+.4f}U")
                sell_id = None
                return round_vol, pnl, True
            elif st in ("CANCELED", "EXPIRED"):
                print(f"    [卖单] 被取消，重新挂")
                sell_id = None
                place_sell()
                continue

        # 2. 检查是否需要补仓
        bid, ask = get_book(sym)
        cur_price = bid

        if layers < MAX_GRID_LAYERS:
            drop_from_avg = (avg_price() - cur_price) / avg_price() * 100
            if drop_from_avg >= GRID_DROP_PCT:
                print(f"    [补仓] 当前价 {cur_price:.6f} 跌离均价 {drop_from_avg:.3f}% >= {GRID_DROP_PCT}%")
                # 先撤现有卖单
                if sell_id:
                    cancel_order(sym, sell_id)
                    sell_id = None
                # 补仓
                f2, c2 = do_buy(f"补{layers}")
                if f2 > 0:
                    last_buy_price = avg_price()
                    # 重新挂卖单（基于新均价）
                    place_sell()

        # 3. 每 10 分钟重挂一次卖单（价格可能已经变动）
        if tick > 0 and tick % 300 == 0 and sell_id:
            o = get_order(sym, sell_id)
            if o.get("status") == "NEW":
                # 检查当前 ask 是否已经超过卖单价格（可能应该提高）
                _, ask_now = get_book(sym)
                current_sell_price = float(o.get("price", 0))
                target = avg_price() * (1 + SELL_PREMIUM / 100)
                if ask_now > current_sell_price * 1.001:
                    print(f"    [卖单] 价格上移，重挂更优位置")
                    cancel_order(sym, sell_id)
                    sell_id = None
                    place_sell()

    # 超时，强制清仓
    print(f"    [超时] 1小时未完成，强制限价清仓")
    if sell_id:
        cancel_order(sym, sell_id)
        sell_id = None

    # 用当前 ask-1tick 挂限价清仓
    token_free = spot_balance(base_asset)
    sell_qty_f = fmt_qty(token_free, step_f, step_dec)
    if sell_qty_f >= min_qty:
        bid, ask = get_book(sym)
        # 强制清仓：挂在 bid（确保成交）
        clear_price = fmt_price(bid, tick_f, tick_dec)
        r_c = place_limit(sym, "SELL", sell_qty_f, clear_price)
        clear_id = r_c.get("orderId")
        if clear_id:
            for _ in range(60):
                time.sleep(1)
                o = get_order(sym, clear_id)
                if o.get("status") == "FILLED":
                    rev = float(o.get("cummulativeQuoteQty", 0))
                    round_vol += rev
                    pnl = rev - total_cost - total_cost * 0.001 - rev * 0.001
                    print(f"    [强清] ✅ rev={rev:.3f}U pnl={pnl:+.4f}U")
                    return round_vol, pnl, True
                bid2, _ = get_book(sym)
                if float(o.get("price", 0)) > bid2 * 1.002:
                    cancel_order(sym, clear_id)
                    clear_price = fmt_price(bid2, tick_f, tick_dec)
                    r_c = place_limit(sym, "SELL", sell_qty_f, clear_price)
                    clear_id = r_c.get("orderId")
                    if not clear_id: break

    buy_fee = total_cost * 0.001
    return round_vol, -total_cost - buy_fee, False  # 未能清仓


# ── 主循环 ────────────────────────────────────────────────────────────────────
def do_volume(sym, target_usdt, base_asset, tick_str, step_str, min_qty):
    tick_f   = float(tick_str)
    step_f   = float(step_str)
    step_dec = max(0, -int(math.floor(math.log10(step_f)))) if step_f < 1 else 0
    tick_dec = max(0, -int(math.floor(math.log10(tick_f)))) if tick_f < 1 else 0

    total_vol   = 0.0
    total_pnl   = 0.0
    skip_streak = 0
    rnd         = 0

    print(f"\n[{sym}] 网格补仓刷量 目标={target_usdt:.0f}U")
    print(f"  每次买 {BUY_EACH}U | 网格间距 {GRID_DROP_PCT}% | 最多补仓 {MAX_GRID_LAYERS}层")
    print(f"  卖出溢价 {SELL_PREMIUM}% | 最大亏损保护 {MAX_TOTAL_LOSS}U")

    while total_vol < target_usdt * 1.9:
        rnd += 1
        ok, reason = check_market(sym)
        if not ok:
            print(f"  轮{rnd} 跳过: {reason}")
            skip_streak += 1
            if skip_streak >= 5:
                print("  连续5轮跳过，终止"); break
            time.sleep(30); continue
        skip_streak = 0

        ts = datetime.datetime.utcnow().strftime("%H:%M:%S")
        print(f"\n  ── 轮{rnd} [{ts}] {reason} ──")

        vol, pnl, ok2 = do_grid_round(
            sym, base_asset, tick_f, tick_dec, step_f, step_dec, min_qty, rnd
        )
        if not ok2 and vol == 0:
            skip_streak += 1; time.sleep(15); continue
        skip_streak = 0

        total_vol += vol
        total_pnl += pnl
        pnl_s = f"{pnl:+.4f}U"
        print(f"  轮{rnd}结: vol={vol:.0f}U pnl={pnl_s} | 累计 vol={total_vol:.0f}U pnl={total_pnl:+.4f}U")

        if total_pnl < MAX_TOTAL_LOSS:
            print(f"  🛑 累计亏损 {total_pnl:+.3f}U 超限，终止")
            break

        time.sleep(3)

    return total_vol, total_pnl


# ── 转账封装 ──────────────────────────────────────────────────────────────────
def transfer(amount, to_spot=True):
    t = 2 if to_spot else 1
    return api.request("POST", "https://api.binance.com/sapi/v1/futures/transfer",
                       {"asset": "USDT", "amount": str(amount), "type": t})


# ── 入口 ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol",      default="EULUSDT")
    parser.add_argument("--target",      type=float, default=5000)
    parser.add_argument("--no-transfer", action="store_true",
                        help="不自动从合约划转，直接用现货余额")
    args = parser.parse_args()
    sym = args.symbol.upper(); target = args.target
    ts  = datetime.datetime.utcnow().strftime("%H:%M UTC")
    print(f"[{ts}] volume_maker v5(网格补仓) sym={sym} target={target}U")

    ok, reason = check_market(sym)
    if not ok:
        print(f"⏸ 跳过: {reason}")
        log_result(sym, False, 0, 0, reason); return
    print(f"行情: {reason}")

    tick_str, step_str, min_qty, base_asset = get_sym_info(sym)
    print(f"交易精度: tick={tick_str} step={step_str} base={base_asset}")

    # 转账
    actual_target = target
    if not args.no_transfer:
        bal   = api.request("GET", api.BASE_FAPI + "/fapi/v2/balance")
        avail = float(next((b for b in bal if b["asset"] == "USDT"), {}).get("availableBalance", 0))
        pos   = api.get_positions()
        maint = sum(float(p.get("maintMargin", 0)) for p in pos
                    if abs(float(p.get("positionAmt", 0))) > 0)
        transferable = max(0, avail - maint * 3) * 0.40
        transfer_amt = round(min(target + 20, transferable), 2)
        if transfer_amt < 20:
            log_result(sym, False, 0, 0, "余额不足"); return
        actual_target = min(target, transfer_amt * 0.9)
        print(f"划转 {transfer_amt}U 合约→现货")
        r_tf = transfer(transfer_amt, to_spot=True)
        if not r_tf.get("tranId"):
            notify(f"⚠️ {sym} 转账失败: {r_tf}")
            log_result(sym, False, 0, 0, "转账失败"); return
        time.sleep(4)

    # 执行
    vol, pnl = do_volume(sym, actual_target, base_asset, tick_str, step_str, min_qty)

    # 转回
    if not args.no_transfer:
        u = spot_balance("USDT")
        if u > 1:
            transfer(round(u - 0.5, 2), to_spot=False)
            time.sleep(2)

    # 通知
    ts_end = datetime.datetime.utcnow().strftime("%H:%M UTC")
    success = vol >= actual_target * 1.5
    status  = "✅" if success else "⚠️"
    if pnl > 0.05:    pnl_str = f"+{pnl:.3f}U 盈利 🎉"
    elif pnl < -0.5:  pnl_str = f"{pnl:.3f}U 亏损"
    else:             pnl_str = f"{pnl:.3f}U 约平本"
    msg = (f"{status} {sym} 刷量完成 | {ts_end}\n"
           f"成交量: {vol:.0f}U  {pnl_str}\n"
           f"{'完成目标' if success else '未达目标，下次补刷'}")
    print(msg); notify(msg)
    log_result(sym, success, vol, pnl, "ok" if success else "partial")


if __name__ == "__main__":
    main()
