#!/usr/bin/env python3
"""
BANK/USDT 做空 Watcher
策略：限价分批做空，止盈3-5%，循环挂单
无止损，全仓5x，头仓500U，补仓250U
"""
import subprocess, json, time, os, datetime

LOG_FILE = "/root/.openclaw/workspace/bank_short_state.json"
TARGET_PRICE = 0.195
LEVERAGE = 5
INITIAL_MARGIN = 500
ADD_MARGIN = 250
TP_LOW = 0.03   # 3%
TP_HIGH = 0.05  # 5%
SYMBOL = "BANKUSDT"

def run(cmd):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    try:
        return json.loads(r.stdout)
    except:
        return r.stdout.strip()

def get_price():
    d = run(f"binance-cli futures-usds symbol-price-ticker --symbol {SYMBOL}")
    return float(d.get("price", 0)) if isinstance(d, dict) else 0

def get_positions():
    d = run(f"binance-cli futures-usds position-information-v3 --symbol {SYMBOL}")
    if isinstance(d, list):
        return [p for p in d if float(p.get("positionAmt", 0)) != 0]
    return []

def get_open_orders():
    d = run(f"binance-cli futures-usds all-open-orders --symbol {SYMBOL}")
    return d if isinstance(d, list) else []

def get_balance():
    d = run("binance-cli futures-usds futures-account-balance-v3")
    if isinstance(d, list):
        for b in d:
            if b.get("asset") == "USDT":
                return float(b.get("availableBalance", 0))
    return 0

def calc_qty(entry_price, margin_usdt):
    notional = margin_usdt * LEVERAGE
    return int(notional / entry_price)

def place_limit_short(entry_price, margin_usdt):
    qty = calc_qty(entry_price, margin_usdt)
    result = run(f"binance-cli futures-usds new-order "
                 f"--symbol {SYMBOL} --side SELL --positionSide SHORT "
                 f"--type LIMIT --price {entry_price:.7f} --quantity {qty} --timeInForce GTC")
    return result

def place_tp(entry_price, qty):
    tp = round(entry_price * (1 - TP_LOW - (TP_HIGH - TP_LOW) * 0.5), 7)  # ~4%
    result = run(f"binance-cli futures-usds new-order "
                 f"--symbol {SYMBOL} --side BUY --positionSide SHORT "
                 f"--type TAKE_PROFIT_MARKET --stopPrice {tp:.7f} "
                 f"--quantity {qty} --timeInForce GTC --workingType CONTRACT_PRICE")
    return result

def save_state(state):
    with open(LOG_FILE, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)

def load_state():
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE) as f:
            return json.load(f)
    return {"cycles": 0, "total_pnl": 0, "last_action": "init", "orders": [], "running": True}

def log(msg):
    ts = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

def main():
    state = load_state()
    state["running"] = True
    save_state(state)
    log("=== BANK短策略 Watcher 启动 ===")
    log(f"目标价: {TARGET_PRICE}, 杠杆: {LEVERAGE}x, 头仓: {INITIAL_MARGIN}U, 补仓: {ADD_MARGIN}U")

    consecutive_errors = 0

    while True:
        try:
            state = load_state()
            if not state.get("running", True):
                log("收到停止信号，退出")
                break

            price = get_price()
            if price == 0:
                log("获取价格失败，重试...")
                time.sleep(10)
                continue

            positions = get_positions()
            open_orders = get_open_orders()
            balance = get_balance()
            has_short = any(float(p.get("positionAmt", 0)) < 0 for p in positions)
            short_pos = next((p for p in positions if float(p.get("positionAmt", 0)) < 0), None)
            has_open_limit = any(o.get("side") == "SELL" and o.get("type") == "LIMIT" for o in open_orders)

            state.update({
                "price": price,
                "balance": balance,
                "has_short": has_short,
                "has_open_limit": has_open_limit,
                "position_amt": float(short_pos.get("positionAmt", 0)) if short_pos else 0,
                "entry_price": float(short_pos.get("entryPrice", 0)) if short_pos else 0,
                "unrealized_pnl": float(short_pos.get("unRealizedProfit", 0)) if short_pos else 0,
                "updated_at": datetime.datetime.utcnow().isoformat(),
            })

            # 情况1：无持仓 + 无挂单 → 找反弹位挂新空单
            if not has_short and not has_open_limit:
                # 挂在当前价上方 +0.2~0.4% 做反弹空
                entry = round(price * 1.003, 7)
                # 判断用头仓还是补仓
                margin = INITIAL_MARGIN if state.get("cycles", 0) == 0 or balance >= INITIAL_MARGIN else ADD_MARGIN
                if balance < 50:
                    log(f"⚠️ 余额不足({balance:.2f}U)，暂停挂单")
                    state["last_action"] = f"余额不足暂停 balance={balance:.2f}"
                else:
                    log(f"无持仓，挂限价空 @ {entry} 数量={calc_qty(entry, margin)} margin={margin}U balance={balance:.2f}U")
                    result = place_limit_short(entry, margin)
                    order_id = result.get("orderId") if isinstance(result, dict) else None
                    state["last_action"] = f"挂单 @ {entry} orderId={order_id}"
                    if order_id:
                        state.setdefault("orders", []).append({
                            "orderId": order_id, "entry": entry, "margin": margin, "time": datetime.datetime.utcnow().isoformat()
                        })

            # 情况2：有持仓 → 检查是否达到止盈
            elif has_short and short_pos:
                entry_p = float(short_pos.get("entryPrice", price))
                pnl_pct = (entry_p - price) / entry_p * 100
                qty_abs = abs(float(short_pos.get("positionAmt", 0)))
                has_tp_order = any(o.get("type") in ("TAKE_PROFIT_MARKET", "TAKE_PROFIT") for o in open_orders)

                log(f"持仓: {qty_abs} BANK SHORT @ {entry_p:.7f} 当前={price:.7f} PnL={pnl_pct:.2f}%")

                # 若还没有止盈单，挂止盈
                if not has_tp_order:
                    tp_result = place_tp(entry_p, int(qty_abs))
                    tp_id = tp_result.get("orderId") if isinstance(tp_result, dict) else None
                    log(f"挂止盈单 orderId={tp_id}")
                    state["last_action"] = f"挂止盈 @ {round(entry_p*(1-0.04),7)} orderId={tp_id}"

                # 检测止盈触发（价格低于entry的3%以上且无持仓时，在下次循环无持仓流程里会重挂）
                if pnl_pct >= TP_HIGH * 100:
                    log(f"✅ 达到止盈{pnl_pct:.1f}%，等止盈单触发平仓")

            # 情况3：有挂单但还没成交
            elif not has_short and has_open_limit:
                pending_orders = [o for o in open_orders if o.get("side") == "SELL" and o.get("type") == "LIMIT"]
                if pending_orders:
                    o = pending_orders[0]
                    limit_price = float(o.get("price", 0))
                    # 如果价格已经远离挂单价（超过1%），取消并重挂
                    diff_pct = abs(price - limit_price) / limit_price * 100
                    if diff_pct > 1.5 and price < limit_price:
                        # 价格已下跌远离挂单价，取消重挂
                        log(f"价格({price:.6f}) 低于挂单({limit_price:.6f}) {diff_pct:.1f}%，取消重挂")
                        run(f"binance-cli futures-usds cancel-order --symbol {SYMBOL} --orderId {o['orderId']}")
                        state["last_action"] = f"取消重挂 旧价={limit_price} 新价待确认"
                    else:
                        log(f"挂单等待成交: {o.get('origQty')} BANK @ {limit_price:.7f} (当前={price:.7f})")
                        state["last_action"] = f"等待成交 @ {limit_price}"

            state["cycles"] = state.get("cycles", 0)
            save_state(state)
            consecutive_errors = 0
            time.sleep(15)

        except KeyboardInterrupt:
            log("手动中断，退出")
            break
        except Exception as e:
            log(f"错误: {e}")
            consecutive_errors += 1
            if consecutive_errors > 10:
                log("连续错误过多，退出")
                break
            time.sleep(30)

    state = load_state()
    state["running"] = False
    save_state(state)
    log("Watcher 已停止")

if __name__ == "__main__":
    main()
