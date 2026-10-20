#!/usr/bin/env python3
"""
sl_tp_manager.py — 止损止盈独立管理模块 v2
============================================
职责：
  1. 查真实仓位（通过代理 fapi/v3/positionRisk）
  2. 查真实 algo 单（binance-cli current-all-algo-open-orders）
  3. 缺止损/止盈则补挂（binance-cli new-algo-order）
  4. 有重复则撤多余（binance-cli cancel-algo-order）
  5. 打印每个仓位最终状态

用法：
  python3 sl_tp_manager.py            # 巡检 + 补挂
  python3 sl_tp_manager.py --dry-run  # 只打印，不实际挂单
  python3 sl_tp_manager.py --force    # 强制重新计算并替换现有 SL/TP

注意：这个账户开了多资产保证金模式，普通 /fapi/v1/order 的 STOP_MARKET 不支持，
只能使用 algo order API（binance-cli futures-usds new-algo-order）。
algo 单在 App 「持有仓位」列表中每个仓位旁边显示，不在「当前委托→条件委托」tab。
"""

import sys, os, time, math, subprocess, json

sys.path.insert(0, "/root/.openclaw/workspace")
import binance_api as _api

DRY_RUN = "--dry-run" in sys.argv
FORCE   = "--force"   in sys.argv


# ─── algo 单操作（只用 binance-cli，代理 API 无法撤 algo） ────────────────────

def get_algo_orders(symbol: str = None) -> list:
    """查询当前 CONDITIONAL 挂单，使用正确端点 /fapi/v1/openAlgoOrders"""
    p = {}
    if symbol: p['symbol'] = symbol
    # 正确端点（/fapi/v1/algoOpenOrders 已404）
    r = _api.request('GET', _api.BASE_FAPI + '/fapi/v1/openAlgoOrders', p)
    if isinstance(r, list): return r
    if isinstance(r, dict): return r.get('orders', r.get('algoOrders', []))
    return []


def place_algo(symbol, side, pos_side, order_type, trigger_price, qty_str, working_type) -> dict:
    """通过 /fapi/v1/algoOrder 直接走代理挂 CONDITIONAL 单（binance-cli 网络不通时的替代）"""
    if DRY_RUN:
        print(f"  [DRY] algoOrder {symbol} {order_type} trigger={trigger_price} qty={qty_str}")
        return {"algoId": "DRY"}
    return _api.request('POST', _api.BASE_FAPI + '/fapi/v1/algoOrder', {
        'symbol': symbol, 'side': side, 'positionSide': pos_side,
        'type': order_type, 'triggerPrice': str(trigger_price),
        'quantity': qty_str, 'workingType': working_type,
        'algoType': 'CONDITIONAL',
    })


def cancel_algo(symbol, algo_id) -> dict:
    """通过 /fapi/v1/algoOrder DELETE 撤销 CONDITIONAL 单"""
    if DRY_RUN:
        print(f"  [DRY] cancel algoOrder {symbol} algoId={algo_id}")
        return {"code": "200"}
    r = _api.request('DELETE', _api.BASE_FAPI + '/fapi/v1/algoOrder', {
        'symbol': symbol, 'algoId': int(algo_id)
    })
    # 成功时返回 {algoId: ...}，失败返回 {code: -xxxx}
    if 'algoId' in r:
        return {"code": "200", "algoId": r['algoId']}
    return r


# ─── 价格工具 ─────────────────────────────────────────────────────────────────

def _decimals(tick_str: str) -> int:
    tick = float(tick_str)
    if tick >= 1:
        return 0
    return max(0, -int(math.floor(math.log10(tick))))


def round_price(price: float, tick_str: str) -> str:
    tick = float(tick_str)
    if tick <= 0:
        return str(price)
    d = _decimals(tick_str)
    rounded = round(round(price / tick) * tick, d)
    return f"{rounded:.{d}f}"


def round_qty(qty: float, step_str: str) -> str:
    step = float(step_str)
    if step <= 0:
        return str(qty)
    d = _decimals(step_str)
    rounded = round(round(qty / step) * step, d)
    return f"{rounded:.{d}f}"


def calc_atr(klines, period=14) -> float:
    if not klines or len(klines) < period:
        return 0.0
    trs = [abs(float(k[2]) - float(k[3])) for k in klines[-period:]]
    return sum(trs) / len(trs)


def calc_sl_tp(pos_side, entry, klines_1h, tick_str):
    """
    计算合理的止损/止盈价：
      止损：距入场价 ~6%（或 2×ATR，取较宽），防噪音
      止盈：距入场价 ~12%（或 4×ATR，取较宽）
    """
    atr = calc_atr(klines_1h) or (entry * 0.02)
    if pos_side == "LONG":
        sl_raw = min(entry * 0.94, entry - atr * 2)
        tp_raw = max(entry * 1.12, entry + atr * 4)
    else:
        sl_raw = max(entry * 1.06, entry + atr * 2)
        tp_raw = min(entry * 0.88, entry - atr * 4)
    return round_price(sl_raw, tick_str), round_price(tp_raw, tick_str)


def validate_prices(pos_side, entry, sl_str, tp_str, tick_str):
    """确保 SL/TP 方向正确"""
    sl = float(sl_str)
    tp = float(tp_str)
    if pos_side == "LONG":
        if sl >= entry:
            sl_str = round_price(entry * 0.94, tick_str)
            print(f"    [修正] SL {sl} >= entry {entry}，改为 {sl_str}")
        if tp <= entry:
            tp_str = round_price(entry * 1.12, tick_str)
            print(f"    [修正] TP {tp} <= entry {entry}，改为 {tp_str}")
    else:
        if sl <= entry:
            sl_str = round_price(entry * 1.06, tick_str)
            print(f"    [修正] SL {sl} <= entry {entry}（空单），改为 {sl_str}")
        if tp >= entry:
            tp_str = round_price(entry * 0.88, tick_str)
            print(f"    [修正] TP {tp} >= entry {entry}（空单），改为 {tp_str}")
    return sl_str, tp_str


# ─── 主流程 ───────────────────────────────────────────────────────────────────

def run():
    print("=" * 60)
    print(f"sl_tp_manager v2  {'[DRY RUN]' if DRY_RUN else '[实盘]'}  {'[FORCE]' if FORCE else ''}")
    print("=" * 60)

    # 1. 持仓
    positions = _api.get_positions()
    if not positions:
        print("无持仓。")
        return
    print(f"持仓 {len(positions)} 个\n")

    # 2. 全量 algo 单
    all_algos = get_algo_orders()
    algo_map = {}
    for a in all_algos:
        s = a["symbol"]
        algo_map.setdefault(s, {"SL": [], "TP": []})
        ot = a.get("orderType", "")
        if ot == "STOP_MARKET":
            algo_map[s]["SL"].append(a)
        elif ot == "TAKE_PROFIT_MARKET":
            algo_map[s]["TP"].append(a)

    summary = []

    for pos in positions:
        symbol   = pos["symbol"]
        pos_side = pos.get("positionSide", "LONG")
        amt      = float(pos.get("positionAmt", 0))
        entry    = float(pos.get("entryPrice", 0))
        mark     = float(pos.get("markPrice", entry))
        qty_abs  = abs(amt)

        if entry <= 0 or qty_abs <= 0:
            continue

        pct = ((entry - mark) / entry * 100) if pos_side == "SHORT" \
              else ((mark - entry) / entry * 100)
        sl_side = "SELL" if pos_side == "LONG" else "BUY"

        cur_sl = algo_map.get(symbol, {}).get("SL", [])
        cur_tp = algo_map.get(symbol, {}).get("TP", [])

        print(f"{'─'*50}")
        print(f"{symbol} {pos_side}  entry={entry}  mark={mark:.5g}  {pct:+.2f}%  qty={qty_abs}")
        print(f"  现有 algo: SL×{len(cur_sl)}  TP×{len(cur_tp)}")

        # 3. 去重（保留最新的）
        def dedup(orders, label):
            if len(orders) <= 1:
                return list(orders)
            orders = sorted(orders, key=lambda x: x.get("createTime", 0), reverse=True)
            for dup in orders[1:]:
                print(f"  撤重复 {label} algoId={dup['algoId']} trigger={dup['triggerPrice']}")
                r = cancel_algo(symbol, dup["algoId"])
                msg = "✅" if isinstance(r, dict) and str(r.get("code")) == "200" else f"⚠️{r}"
                print(f"    {msg}")
                time.sleep(0.5)
            return orders[:1]

        cur_sl = dedup(cur_sl, "SL")
        cur_tp = dedup(cur_tp, "TP")

        # FORCE 模式：撤所有已有的
        if FORCE and (cur_sl or cur_tp):
            for o in cur_sl + cur_tp:
                cancel_algo(symbol, o["algoId"])
                time.sleep(0.3)
            cur_sl, cur_tp = [], []
            print("  [FORCE] 已清除，重新计算")

        has_sl = len(cur_sl) > 0
        has_tp = len(cur_tp) > 0

        if has_sl and has_tp:
            sl_p = cur_sl[0]["triggerPrice"]
            tp_p = cur_tp[0]["triggerPrice"]
            print(f"  ✅ 已就绪: SL@{sl_p}  TP@{tp_p}")
            summary.append((symbol, "✅", sl_p, tp_p))
            continue

        # 4. 计算价格
        sinfo    = _api.get_symbol_info(symbol)
        tick_str = sinfo.get("tick_size", "0.001")
        step_str = sinfo.get("step_size", "0.001")
        qty_str  = round_qty(qty_abs, step_str)

        klines = _api.get_klines(symbol, "1h", 50)
        sl_str, tp_str = calc_sl_tp(pos_side, entry, klines, tick_str)
        sl_str, tp_str = validate_prices(pos_side, entry, sl_str, tp_str, tick_str)
        print(f"  计算: SL={sl_str}  TP={tp_str}  qty={qty_str}")

        ok_sl = has_sl
        ok_tp = has_tp

        # 5. 挂止损
        if not has_sl:
            r = place_algo(symbol, sl_side, pos_side, "STOP_MARKET",
                           sl_str, qty_str, "MARK_PRICE")
            if r.get("algoId"):
                print(f"  ✅ 止损 algoId={r['algoId']}")
                _api.algo_cache_save(symbol, "STOP_MARKET", r["algoId"], sl_str)
                ok_sl = True
                time.sleep(1)
            else:
                print(f"  ❌ 止损失败: {r}")

        # 6. 挂止盈
        if not has_tp:
            r = place_algo(symbol, sl_side, pos_side, "TAKE_PROFIT_MARKET",
                           tp_str, qty_str, "CONTRACT_PRICE")
            if r.get("algoId"):
                print(f"  ✅ 止盈 algoId={r['algoId']}")
                _api.algo_cache_save(symbol, "TAKE_PROFIT_MARKET", r["algoId"], tp_str)
                ok_tp = True
                time.sleep(1)
            else:
                print(f"  ❌ 止盈失败: {r}")

        # 7. 最终验证 — 用本地缓存判断（API 查询接口 404 不可用）
        time.sleep(1.0)
        cached = _api.get_algo_open_orders_cached(symbol)
        v_sl = [o for o in cached if o["orderType"] == "STOP_MARKET"]
        v_tp = [o for o in cached if o["orderType"] == "TAKE_PROFIT_MARKET"]
        # 只要下单时没报错（ok_sl/ok_tp=True），就认为成功
        final_ok = ok_sl and ok_tp
        if final_ok:
            sl_p = v_sl[0]["triggerPrice"] if v_sl else sl_str
            tp_p = v_tp[0]["triggerPrice"] if v_tp else tp_str
            print(f"  ✅ 完成: SL@{sl_p}  TP@{tp_p}")
            summary.append((symbol, "✅", sl_p, tp_p))
        else:
            print(f"  ❌ 失败: ok_sl={ok_sl} ok_tp={ok_tp}")
            summary.append((symbol, "❌", "?", "?"))

    print(f"\n{'='*60}")
    print("汇总结果")
    print(f"{'='*60}")
    for sym, status, sl, tp in summary:
        print(f"  {status} {sym:12}  止损@{sl:12}  止盈@{tp}")
    print()


if __name__ == "__main__":
    run()
