"""
altcoin_monitor.py - 山寨币仓位高频维护(每15分钟)
只处理非主流币持仓,主流币留给 position_manager(30分钟)处理。
逻辑与 position_manager 的仓位维护部分相同。
"""
import sys, os, requests, json, time, datetime
sys.path.insert(0, "/root/.openclaw/workspace")
import binance_api as api
from manual_positions import is_manual
from entry_logic import (
    liquidation_map, calc_entry_mode,
    check_funding_settlement_window,
    rsi_calc, atr_calc, ema_calc, fmt_price, fmt_qty
)

BASE_F     = "https://fapi.binance.com"
STATE_FILE = "/root/.openclaw/workspace/ws_cache/scanner_state.json"

# ── 主流币白名单(这些交给 30min 的 position_manager 处理)──
MAJORS = {
    'BTCUSDT','ETHUSDT','BNBUSDT','SOLUSDT','XRPUSDT','ADAUSDT',
    'DOGEUSDT','AVAXUSDT','DOTUSDT','LINKUSDT','UNIUSDT','LTCUSDT',
    'BCHUSDT','NEARUSDT','APTUSDT','OPUSDT','ARBUSDT','MATICUSDT',
    'ATOMUSDT','FILUSDT','INJUSDT','SUIUSDT','SEIUSDT','TIAUSDT',
    'STXUSDT','RUNEUSDT','LDOUSDT','AAVEUSDT','MKRUSDT','SNXUSDT',
}
# 成交量超过 10 亿 U 才视为主流(避免 BANK/ENA 等中等体量被误判跳过)
# is_major 已迁移到 binance_api.py
def is_major(sym, vol_24h=0):
    return api.is_major(sym, vol_24h)

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

state = json.load(open(STATE_FILE)) if os.path.exists(STATE_FILE) else {"positions": {}}
if "positions" not in state: state["positions"] = {}

# 获取 24h 成交量用于分类
try:
    tickers = {t['symbol']: float(t.get('quoteVolume',0))
               for t in requests.get(f'{BASE_F}/fapi/v1/ticker/24hr', timeout=10).json()}
except:
    tickers = {}

# 当前真实持仓(同时包含 LONG 和 SHORT)
positions   = api.get_positions()
active_pos  = [
    p for p in positions
    if float(p.get('positionAmt', 0)) != 0
    and p.get('symbol','').endswith('USDT')
]
# 按 (symbol, positionSide) 索引,支持双向持仓
active_pos_map = {(p["symbol"], p.get("positionSide", "BOTH")): p for p in active_pos}
# 保持向下兼容:symbol → 任一持仓(用于清理逻辑)
active_pos_syms = {p["symbol"] for p in active_pos}

reports = []

def place_sl_order(symbol, side, qty, stop_price, position_side):
    """硬止损下单(市价):用于真实止损,确保一定成交,允许滑点"""
    try:
        r = api.place_algo_order(
            symbol=symbol, side=side,
            order_type="STOP_MARKET", trigger_price=stop_price,
            position_side=position_side, quantity=str(qty)
        )
        oid = r.get('algoId') or r.get('orderId')
        if oid:
            return str(oid), r
    except Exception:
        pass
    # fallback:普通 STOP_MARKET
    r2 = api.place_order(
        symbol=symbol, side=side, order_type="STOP_MARKET",
        quantity=str(qty), stopPrice=str(stop_price),
        positionSide=position_side
    )
    return str(r2.get('orderId', '')), r2


def place_trailing_sl_order(symbol, side, qty, stop_price, position_side, tick):
    """保本/移动止损下单(限价):触发后挂限价,减少滑点。
    用 STOP 类型 + limitPrice = stopPrice(触发即以该价格挂限价单)。
    若 algo 限价失败则 fallback 到普通限价 reduceOnly,价格相同。
    """
    limit_price = fmt_price(stop_price, tick)
    try:
        # algo STOP(触发价 = 限价,触发后挂限价单而非市价)
        r = api.place_algo_order(
            symbol=symbol, side=side,
            order_type="STOP", trigger_price=float(limit_price),
            position_side=position_side, quantity=str(qty)
        )
        oid = r.get('algoId') or r.get('orderId')
        if oid:
            return str(oid), r
    except Exception:
        pass
    # fallback:普通限价 reduceOnly(直接挂在触发价,等价格到了自然成交)
    r2 = api.place_order(
        symbol=symbol, side=side, order_type="LIMIT",
        quantity=str(qty), price=limit_price,
        positionSide=position_side, timeInForce="GTC"
    )
    return str(r2.get('orderId', '')), r2

# 清理已平仓,并撤销所有残留条件单
for sym in list(state["positions"].keys()):
    if sym not in active_pos_syms:
        try:
            all_algo = api.request("GET", f"{BASE_F}/fapi/v1/openAlgoOrders")
            if isinstance(all_algo, list):
                sym_algos = [o for o in all_algo if o["symbol"] == sym]
                for o in sym_algos:
                    api.cancel_algo_order(sym, o["algoId"])
                if sym_algos:
                    reports.append(f"🗑️ {sym} 平仓后撤销 {len(sym_algos)} 笔残留条件单")
        except Exception: pass  # 静默异常
        del state["positions"][sym]
save_state(state)

for pos in active_pos:
    sym = pos["symbol"]
    pos_side = pos.get("positionSide", "LONG")  # LONG or SHORT

    # ── 手动仓位跳过:用户自主管理,自动化不干预 ──
    if is_manual(sym, pos_side):
        continue

    state_key = f"{sym}_{pos_side}"  # 区分同币种多空

    # 只处理山寨币
    vol = tickers.get(sym, 0)
    if is_major(sym, vol):
        continue

    ps      = state["positions"].get(state_key, state["positions"].get(sym, {}))
    entry   = float(pos["entryPrice"])
    qty     = abs(float(pos["positionAmt"]))
    mark_p  = float(pos["markPrice"])
    pnl     = float(pos.get("unRealizedProfit", 0))
    is_long = pos_side == "LONG"
    # 多单:price > entry 盈利;空单:price < entry 盈利
    pct     = (mark_p - entry) / entry * 100 * (1 if is_long else -1) if entry > 0 else 0
    sl_price= ps.get("sl_price", 0)
    tp_price= ps.get("tp_price", 0)
    sig_type= ps.get("sig_type", "trend")

    try:
        k1h = requests.get(f"{BASE_F}/fapi/v1/klines",
            params={"symbol":sym,"interval":"1h","limit":55}, timeout=8).json()
        k4h = requests.get(f"{BASE_F}/fapi/v1/klines",
            params={"symbol":sym,"interval":"4h","limit":55}, timeout=8).json()
        c1h = [float(k[4]) for k in k1h]
        c4h = [float(k[4]) for k in k4h]
        rsi_1h  = rsi_calc(c1h)
        rsi_4h  = rsi_calc(c4h)
        atr     = atr_calc(k1h)
        ema20_1h = ema_calc(c1h[-20:], 20)
        ema50_1h = ema_calc(c1h[-50:], 50) if len(c1h)>=50 else ema_calc(c1h, len(c1h))
        ema20_4h = ema_calc(c4h[-20:], 20) if len(c4h)>=20 else ema_calc(c4h, len(c4h))
        ema50_4h = ema_calc(c4h[-50:], 50) if len(c4h)>=50 else ema_calc(c4h, len(c4h))
        trend_1h = "多排" if ema20_1h > ema50_1h else "空排"
        trend_4h = "多排" if ema20_4h > ema50_4h else "空排"
        fr_r    = requests.get(f"{BASE_F}/fapi/v1/premiumIndex",
            params={"symbol":sym}, timeout=8).json()
        funding = float(fr_r.get("lastFundingRate",0))*100
    except Exception as e:
        reports.append(f"⚠️ {sym} 指标拉取失败: {e}")
        continue

    info = requests.get(f"{BASE_F}/fapi/v1/exchangeInfo", timeout=10).json()
    s2   = next((x for x in info['symbols'] if x['symbol']==sym), None)
    if not s2: continue
    tick = float(next(f['tickSize'] for f in s2['filters'] if f['filterType']=='PRICE_FILTER'))
    step = float(next(f['stepSize'] for f in s2['filters'] if f['filterType']=='LOT_SIZE'))
    qty_str = fmt_qty(qty, step)
    actions = []

    # ── 资金费率窗口:仓位也要保护(窗口内不移动止损,避免误操作)──
    blocked, block_reason = check_funding_settlement_window(funding)
    if blocked:
        reports.append(f"⏰ {sym} 资金费率窗口({block_reason}),跳过本轮维护")
        continue

    # ── 提前平仓判断(山寨币更激进) ──
    # 阈值用 ATR 动态换算，避免对所有币用同一固定百分比
    atr_pct = atr / entry * 100 if entry > 0 else 1.0   # 代表该币"正常一根K线的波动%"
    EXIT_TREND_PCT = max(0.3, atr_pct * 0.5)   # 趋势反转保护：至少盈利 0.5 ATR
    EXIT_RSI_PCT   = max(0.8, atr_pct * 1.0)   # RSI 极端保护：至少盈利 1.0 ATR
    exit_now, exit_reason = False, ""
    if is_long:
        # 多单:1H+4H 双空排转势
        if trend_1h == "空排" and trend_4h == "空排" and pct > EXIT_TREND_PCT:
            exit_now = True; exit_reason = f"1H+4H 双空排转势,保护利润(阈值={EXIT_TREND_PCT:.2f}%)"
        elif rsi_1h > 80 and pct > EXIT_RSI_PCT:
            exit_now = True; exit_reason = f"RSI={rsi_1h:.0f} 极度超买,止盈出局(阈值={EXIT_RSI_PCT:.2f}%)"
    else:
        # 空单:1H+4H 双多排转势
        if trend_1h == "多排" and trend_4h == "多排" and pct > EXIT_TREND_PCT:
            exit_now = True; exit_reason = f"1H+4H 双多排反转,空单保护利润(阈值={EXIT_TREND_PCT:.2f}%)"
        elif rsi_1h < 20 and pct > EXIT_RSI_PCT:
            exit_now = True; exit_reason = f"RSI={rsi_1h:.0f} 极度超卖,空单止盈出局(阈值={EXIT_RSI_PCT:.2f}%)"

    if exit_now:
        close_side = "SELL" if is_long else "BUY"
        r_close = api.place_order(sym, close_side, "MARKET",
            quantity=qty_str, positionSide=pos_side, reduceOnly="true")
        try:
            api.request("DELETE", f"{BASE_F}/fapi/v1/allOpenOrders", {"symbol": sym})
        except Exception: pass  # 静默异常
        # 清理 state(兼容新旧两种 key)
        state["positions"].pop(state_key, None)
        state["positions"].pop(sym, None)
        save_state(state)
        reports.append(f"🔴 {sym}[{pos_side}] 提前平仓 | {exit_reason} | 盈亏={pnl:+.2f}U ({pct:+.1f}%)")
        continue

    # ── 移动止损 ──
    # 所有触发阈值均用 ATR 倍数动态换算（atr_pct 已在上方计算）
    # T1(保本)=0.5ATR  T2(+0.5ATR锁利)=1.5ATR  T3(+ATR锁利)=2.5ATR  T4(+1.5ATR锁利)=4ATR
    T1 = max(0.5, atr_pct * 0.5)
    T2 = max(1.5, atr_pct * 1.5)
    T3 = max(3.0, atr_pct * 2.5)
    T4 = max(5.0, atr_pct * 4.0)
    breakeven_ok = pct >= T1
    new_sl = None
    if is_long:
        # 多单：止损往上移
        if   pct >= T4 and sl_price < entry + atr*1.5:   new_sl = round(entry + atr*1.5, 8)
        elif pct >= T3 and sl_price < entry + atr*1.0:   new_sl = round(entry + atr*1.0, 8)
        elif pct >= T2 and sl_price < entry + atr*0.5:   new_sl = round(entry + atr*0.5, 8)
        elif breakeven_ok and sl_price < entry:           new_sl = round(entry, 8)  # 保本
    else:
        # 空单：止损往下移（止损价从上方往下追）
        cur_sl = sl_price if sl_price else entry * 1.06
        if   pct >= T4 and (cur_sl == 0 or cur_sl > entry - atr*1.5): new_sl = round(entry - atr*1.5, 8)
        elif pct >= T3 and (cur_sl == 0 or cur_sl > entry - atr*1.0): new_sl = round(entry - atr*1.0, 8)
        elif pct >= T2 and (cur_sl == 0 or cur_sl > entry - atr*0.5): new_sl = round(entry - atr*0.5, 8)
        elif breakeven_ok and (cur_sl == 0 or cur_sl > entry):         new_sl = round(entry, 8)  # 保本

    if new_sl:
        new_sl_str = fmt_price(new_sl, tick)
        # 撤旧止损,挂新止损
        try:
            api.request("DELETE", f"{BASE_F}/fapi/v1/allOpenOrders", {"symbol": sym})
            time.sleep(0.3)
            # 移动止损(限价单,减少滑点)
            sl_side = "SELL" if is_long else "BUY"
            _, _ = place_trailing_sl_order(sym, sl_side, qty_str, float(new_sl_str), pos_side, tick)
            # 重挂止盈(限价单,保持原止盈)
            if tp_price:
                tp_str_r = fmt_price(tp_price, tick)
                tp_side = "SELL" if is_long else "BUY"
                api.place_order(sym, tp_side, "LIMIT",
                    quantity=qty_str, price=tp_str_r,
                    positionSide=pos_side, timeInForce="GTC")
            if state_key not in state["positions"]:
                state["positions"][state_key] = {}
            state["positions"][state_key]["sl_price"] = float(new_sl_str)
            save_state(state)
            actions.append(f"📈 [{pos_side}] 移动止损→{new_sl_str} (浮盈{pct:.1f}%)")
        except Exception as e:
            actions.append(f"⚠️ 移动止损失败: {e}")

    # ── 止损/止盈缺失检查(成交后补挂)──
    # 如果 position_state.json 标记了 sl_tp_locked=True,说明该仓位止盈止损已由清算图手动设置,跳过自动补挂
    try:
        import json as _json
        with open('/root/.openclaw/workspace/position_state.json') as _f:
            _ps = _json.load(_f)
        _locked = _ps.get(sym, {}).get('sl_tp_locked', False)
    except Exception:
        _locked = False

    if not sl_price or not tp_price:
        if _locked:
            actions.append(f"✅ [{pos_side}] sl_tp_locked=True,跳过自动补挂止盈止损")
        else:
            lmap = liquidation_map(sym, k1h, k4h, pos_side)
            atr  = lmap['atr_14']
            sl_side = "SELL" if is_long else "BUY"
            tp_side = "SELL" if is_long else "BUY"

            # 优先用 Coinglass 真实清算图,失败则 fallback 到 liquidation_map
            _cg_sl, _cg_tp1, _cg_tp2, _cg_src = get_sl_tp_from_coinglass(
                sym, pos_side, entry, mark_p, atr, lmap
            )

            if not sl_price:
                sl_r   = _cg_sl
                sl_str = fmt_price(sl_r, tick)
                oid_sl, r_sl = place_sl_order(sym, sl_side, qty_str, float(sl_str), pos_side)
                if oid_sl:
                    if state_key not in state["positions"]:
                        state["positions"][state_key] = {}
                    state["positions"][state_key]["sl_price"] = float(sl_str)
                    actions.append(f"🛡️ [{pos_side}] 补挂止损={sl_str} (来源={_cg_src})")
            if not tp_price:
                tp_r   = _cg_tp1
                tp_str = fmt_price(tp_r, tick)
                # 止盈统一用限价单，避免 TAKE_PROFIT_MARKET 滑点
                try:
                    r_tp2 = api.place_order(sym, tp_side, "LIMIT",
                        quantity=qty_str, price=tp_str,
                        positionSide=pos_side, timeInForce="GTC")
                    tp_ok = isinstance(r_tp2, dict) and r_tp2.get('orderId')
                    if tp_ok:
                        actions.append(f"🎯 [{pos_side}] 补挂止盈={tp_str} 限价单 orderId={r_tp2['orderId']} (来源={_cg_src})")
                    else:
                        actions.append(f"⚠️ [{pos_side}] 止盈限价单下单失败: {r_tp2}")
                except Exception as _e:
                    tp_ok = False
                    actions.append(f"⚠️ [{pos_side}] 止盈下单失败: {_e}")
                if tp_ok:
                    if state_key not in state["positions"]:
                        state["positions"][state_key] = {}
                    state["positions"][state_key]["tp_price"] = float(tp_str)
            save_state(state)

    line = (f"📊 [{sym}][{pos_side}] 入场={entry:.4f} 现价={mark_p:.4f} "
            f"浮盈={pnl:+.2f}U({pct:+.1f}%) "
            f"SL={sl_price or '未设'} TP={tp_price or '未设'} "
            f"RSI={rsi_1h:.0f} 费率={funding:.3f}%")
    reports.append(line)
    for a in actions:
        reports.append(f"   {a}")

# ── 输出 ──
if not reports:
    print("HEARTBEAT_OK")
else:
    ts = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    print(f"⚡ 山寨仓位巡检 | {ts}")
    for r in reports:
        print(r)
