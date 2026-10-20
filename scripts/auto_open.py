#!/usr/bin/env python3
"""
auto_open.py — 全自动扫描开仓脚本
规格：1000U 名义 / 50x 杠杆 / Coinglass 清算图对齐限价入场 + 单档止盈 + algo止损
每次运行：扫描 → 评分 → 达标则开仓 → 挂止盈止损 → 推送通知
"""
import sys, os, requests, json, datetime, time
sys.path.insert(0, "/root/.openclaw/workspace")
import binance_api as api
from order_registry import registry as _reg
import coinglass_api as cg
from entry_logic import (
    rsi_calc, atr_calc, ema_calc, fmt_price, fmt_qty,
    calc_score_trend, calc_score_reversal, classify_signal,
    check_funding_settlement_window, check_oi_divergence,
    liquidation_map, calc_entry_mode
)
from manual_positions import is_manual

NOTIFY_TO  = "432401404:thread:019fadbf-37d4-710e-bda4-d7816642e957"
PROXY      = {"http": api.PROXY_URL, "https": api.PROXY_URL}
BASE_F     = "https://fapi.binance.com"
NOTIONAL   = 1000          # U 名义仓位（单笔上限，按评分动态调整）
LEVERAGE   = 50
MAX_POS    = 5             # 最多同时持仓数（多+空合计）
MAX_LONG_POS = 3           # 多单最多3个
MIN_AVAIL  = 100           # 最低可用余额
SCORE_GATE = 75            # 多单开仓门槛（≥75才挂单）
SCORE_GATE_WEEKEND = 80    # 周末加强门槛（v3.0: 上限75，只能过周末80门槛才开单）
import datetime as _dt_tmp
_IS_WEEKEND = _dt_tmp.datetime.utcnow().weekday() >= 5
EFF_SCORE_GATE = SCORE_GATE_WEEKEND if _IS_WEEKEND else SCORE_GATE
SCORE_TREND    = 65
SCORE_REVERSAL = 65
STABLE     = {'USDT','BUSD','USDC','TUSD','FDUSD','USDP','DAI'}

STATE_FILE = "/root/.openclaw/workspace/ws_cache/auto_open_state.json"

now_str = datetime.datetime.utcnow().strftime('%H:%M UTC')

def notify(msg):
    print(f"__NOTIFY__:{msg}")

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except:
        return {"opened": {}}

def save_state(s):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(s, f, indent=2, ensure_ascii=False)

# ── 条件单状态管理 ──
COND_STATE_FILE = "/root/.openclaw/workspace/ws_cache/cond_order_state.json"
COND_MAX_LONG  = 10   # 多单条件单总数上限
COND_MAX_SHORT = 5    # 空单条件单总数上限
COND_TTL       = 4 * 3600  # 4小时超时自动撤单
_COND_ORDER_MIN_SCORE = 60  # 条件单预埋最低评分（低于直接开仓门槛）

def _load_cond_state():
    try:
        with open(COND_STATE_FILE) as f: return json.load(f)
    except: return {}

def _save_cond_state(s):
    os.makedirs(os.path.dirname(COND_STATE_FILE), exist_ok=True)
    tmp = COND_STATE_FILE + ".tmp"
    with open(tmp, "w") as f: json.dump(s, f, indent=2, ensure_ascii=False)
    os.replace(tmp, COND_STATE_FILE)

cond_state = _load_cond_state()
state = load_state()


balance   = api.request("GET", api.BASE_FAPI + "/fapi/v2/balance", {})
avail     = float([b for b in balance if b['asset']=='USDT'][0]['availableBalance'])
positions = api.get_positions()
active    = [p for p in positions if abs(float(p.get('positionAmt',0)))>0]
slots_used = len(active)

if slots_used >= MAX_POS:
    print(f"仓位已满 {slots_used}/{MAX_POS}，跳过扫描")
    sys.exit(0)

if avail < MIN_AVAIL:
    print(f"可用余额不足 {avail:.1f}U < {MIN_AVAIL}U，跳过")
    sys.exit(0)

# ── 获取行情数据 ──
try:
    ticker_all = requests.get(f'{BASE_F}/fapi/v1/ticker/24hr', timeout=10, proxies=PROXY).json()
    ticker_map = {t['symbol']: t for t in ticker_all if isinstance(t, dict)}
except Exception as e:
    print(f"获取行情失败: {e}")
    sys.exit(1)

# ── 获取交易规格（lot size step + tick size）──
try:
    _exch_info = requests.get(f'{BASE_F}/fapi/v1/exchangeInfo', timeout=10, proxies=PROXY).json()
    _step_map = {}
    _tick_size_map = {}
    _precision_map = {}
    for _s in _exch_info.get('symbols', []):
        for _f in _s.get('filters', []):
            if _f['filterType'] == 'LOT_SIZE':
                _step_map[_s['symbol']] = float(_f['stepSize'])
            elif _f['filterType'] == 'PRICE_FILTER':
                _tick_size_map[_s['symbol']] = float(_f['tickSize'])
        _precision_map[_s['symbol']] = _s.get('pricePrecision', 2)
except Exception as _e:
    print(f"获取 exchangeInfo 失败，使用默认 step=1.0: {_e}")
    _step_map = {}
    _tick_size_map = {}
    _precision_map = {}

# ── 筛选候选标的 ──
existing_syms = {p['symbol'] for p in active}

candidates = []
for sym, tk in ticker_map.items():
    if not sym.endswith('USDT'): continue
    if sym.replace('USDT','') in STABLE: continue
    vol24 = float(tk.get('quoteVolume', 0))
    if vol24 < 30_000_000: continue
    if sym in existing_syms: continue          # 已有持仓跳过
    if is_manual(sym): continue                # 手动管理跳过
    candidates.append((sym, tk, vol24))

candidates.sort(key=lambda x: -x[2])
candidates = candidates[:60]

# ── 扫描评分 ──
from concurrent.futures import ThreadPoolExecutor, as_completed

def scan_one(item):
    sym, tk, vol24 = item
    try:
        mark  = float(tk.get('lastPrice', 0))
        chg24 = float(tk.get('priceChangePercent', 0))

        k1h  = requests.get(f'{BASE_F}/fapi/v1/klines',
            params={'symbol': sym, 'interval': '1h', 'limit': 50},
            timeout=6, proxies=PROXY).json()
        k4h  = requests.get(f'{BASE_F}/fapi/v1/klines',
            params={'symbol': sym, 'interval': '4h', 'limit': 20},
            timeout=6, proxies=PROXY).json()
        k15m = requests.get(f'{BASE_F}/fapi/v1/klines',
            params={'symbol': sym, 'interval': '15m', 'limit': 30},
            timeout=6, proxies=PROXY).json()

        fr_raw = float(requests.get(f'{BASE_F}/fapi/v1/premiumIndex',
            params={'symbol': sym}, timeout=5, proxies=PROXY).json().get('lastFundingRate', 0))
        fr_pct = fr_raw * 100

        c1h  = [float(k[4]) for k in k1h]
        c4h  = [float(k[4]) for k in k4h]
        c15m = [float(k[4]) for k in k15m]

        rsi1h  = rsi_calc(c1h)
        rsi4h  = rsi_calc(c4h)
        rsi15m = rsi_calc(c15m)
        atr    = atr_calc(k1h, 14)
        ema20_1h = ema_calc(c1h[-20:], 20)
        ema50_1h = ema_calc(c1h[-50:], 50)
        ema20_4h = ema_calc(c4h[-20:], 20)
        ema50_4h = ema_calc(c4h[-50:], 50)

        p4   = [float(k[4]) for k in k15m[-4:]]
        mom4 = (p4[-1]-p4[0])/p4[0]*100

        blocked, _ = check_funding_settlement_window(fr_pct)
        oi_div, _  = check_oi_divergence(sym)

        sig_type = classify_signal(k1h, k4h, fr_raw, chg24)
        if sig_type == 'trend':
            score, _ = calc_score_trend(sym, k1h, k4h, k15m, tk, fr_raw)
            threshold = SCORE_TREND
        else:
            score, _ = calc_score_reversal(sym, k1h, k4h, k15m, tk, fr_raw)
            threshold = SCORE_REVERSAL

        # 方向判断
        short_ok = (rsi1h > 62 or rsi4h > 60) and not blocked and not oi_div
        long_ok  = (rsi1h < 38 or rsi4h < 40) and not blocked and not oi_div
        direction = 'SHORT' if short_ok else ('LONG' if long_ok else 'NEUTRAL')

        return dict(sym=sym, mark=mark, score=score, threshold=threshold,
                    direction=direction, sig_type=sig_type,
                    rsi1h=rsi1h, rsi4h=rsi4h, rsi15m=rsi15m,
                    mom4=mom4, fr=fr_pct, atr=atr, atr_pct=atr/mark*100,
                    ema1h='多' if ema20_1h>ema50_1h else '空',
                    ema4h='多' if ema20_4h>ema50_4h else '空',
                    chg24=chg24, vol24=vol24,
                    blocked=blocked, oi_div=oi_div,
                    k1h=k1h, k4h=k4h)
    except:
        return None

results = []
with ThreadPoolExecutor(max_workers=8) as ex:
    for r in as_completed({ex.submit(scan_one, c): c for c in candidates}):
        res = r.result()
        if res: results.append(res)

results.sort(key=lambda x: (-x['score'], x['sym']))

# ── 达标信号（周末用加强门槛 EFF_SCORE_GATE=80，工作日75）──
fired = [r for r in results
         if r['score'] >= EFF_SCORE_GATE
         and r['direction'] != 'NEUTRAL']

if not fired:
    # 打印前5供参考，然后静默退出
    top5 = [r for r in results[:5]]
    summary = "  ".join(f"{r['sym']} {r['score']}/{r['threshold']}" for r in top5)
    print(f"无达标信号，前5: {summary}")
    sys.exit(0)

# ── 当前条件单数量统计 ──
now_ts_co = int(time.time())
_cond_longs  = [v for v in cond_state.values() if v.get('direction') == 'LONG'  and now_ts_co - v.get('placed_at', 0) < COND_TTL]
_cond_shorts = [v for v in cond_state.values() if v.get('direction') == 'SHORT' and now_ts_co - v.get('placed_at', 0) < COND_TTL]

# ── 开仓执行 ──
slots_left = MAX_POS - slots_used

# 将达标信号拆为：直接执行开仓 vs 条件单预埋
# 条件单候选：评分足够但 COND_ORDER，且不占用持仓槽位
cond_candidates = [r for r in results
                   if r['score'] >= _COND_ORDER_MIN_SCORE
                   and r['direction'] != 'NEUTRAL'
                   and r['sym'] not in cond_state  # 同一币种不重复挂
                   and r['sym'] not in existing_syms]  # 已持仓跳过

for r in fired[:slots_left]:
    sym       = r['sym']
    direction = r['direction']
    mark      = r['mark']
    k1h       = r['k1h']
    k4h       = r['k4h']
    tick      = _tick_size_map.get(sym, 0.00001 if mark < 1 else (0.0001 if mark < 10 else 0.001))
    step      = _step_map.get(sym, 1.0)

    try:
        lmap = liquidation_map(sym, k1h, k4h, direction)

        # ── 用新的 calc_entry_mode 计算入场价 ──
        entry_mode, limit_p, _wait, anchor_src = calc_entry_mode(
            k1h, direction, lmap, final_score=r['score']
        )

        if entry_mode == 'WAIT':
            print(f"  {sym} {direction} 入场位无效（{anchor_src}），跳过")
            continue

        # ── COND_ORDER：预埋条件限价单，不占用持仓槽位 ──
        if entry_mode == 'COND_ORDER':
            # 容量检查
            _dir_key = 'LONG' if direction == 'LONG' else 'SHORT'
            _cur_longs  = len([v for v in cond_state.values() if v.get('direction') == 'LONG'  and now_ts_co - v.get('placed_at',0) < COND_TTL])
            _cur_shorts = len([v for v in cond_state.values() if v.get('direction') == 'SHORT' and now_ts_co - v.get('placed_at',0) < COND_TTL])
            _over_cap = (_dir_key == 'LONG' and _cur_longs >= COND_MAX_LONG) or \
                        (_dir_key == 'SHORT' and _cur_shorts >= COND_MAX_SHORT)
            if _over_cap:
                print(f"  {sym} {direction} 条件单已达上限，跳过")
                continue
            # 限价单就是条件单的执行价：计算仓位
            _sig_type_co = r.get('sig_type', 'trend')
            if _sig_type_co == 'trend':
                _sc_co = r['score']
                if _sc_co >= 90:    _notional_co = 1000
                elif _sc_co >= 80:  _notional_co = 700
                elif _sc_co >= 70:  _notional_co = 500
                else:               _notional_co = 300
            elif _sig_type_co == 'trend_short':
                _sc_co = r['score']
                if _sc_co >= 80:    _notional_co = 600
                elif _sc_co >= 70:  _notional_co = 450
                else:               _notional_co = 300
            else:
                _notional_co = 250   # reversal类 固定250U

            _pos_side_co = 'LONG' if direction == 'LONG' else 'SHORT'
            _order_side_co = 'BUY' if direction == 'LONG' else 'SELL'
            _tp_side_co    = 'SELL' if direction == 'LONG' else 'BUY'
            _tick_co = _tick_size_map.get(sym, 0.00001 if mark < 1 else (0.0001 if mark < 10 else 0.001))
            _step_co = _step_map.get(sym, 1.0)
            _lp_fmt  = fmt_price(limit_p, _tick_co)
            _qty_co  = fmt_qty(_notional_co / float(_lp_fmt), _step_co)
            _sl_fmt  = fmt_price(lmap['hard_sl'], _tick_co)
            _tp_fmt  = fmt_price(lmap['soft_tp1'], _tick_co)
            _dev_pct_co = abs(float(_lp_fmt) - mark) / mark * 100

            # 未设杯信权
            api.request("POST", f"{api.BASE_FAPI}/fapi/v1/leverage", {"symbol": sym, "leverage": LEVERAGE})

            r_co = api.place_order(
                symbol=sym, side=_order_side_co, order_type="LIMIT",
                quantity=_qty_co, price=_lp_fmt,
                positionSide=_pos_side_co, timeInForce="GTC"
            )
            if not isinstance(r_co, dict) or not r_co.get('orderId'):
                print(f"  {sym} {direction} 条件单下单失败: {r_co}")
                continue

            # 写入条件单状态
            cond_state[sym] = {
                'direction':    direction,
                'order_id':     r_co['orderId'],
                'limit_price':  float(_lp_fmt),
                'sl':           float(_sl_fmt),
                'tp1':          float(_tp_fmt),
                'qty':          float(_qty_co),
                'placed_at':    now_ts_co,
                'expire_at':    now_ts_co + COND_TTL,
                'score':        r['score'],
                'sig_type':     _sig_type_co,
                'anchor_src':   anchor_src,
                'notional':     _notional_co,
            }
            _save_cond_state(cond_state)
            # 静默不推送（条件单挂出时不需通知）
            print(f"  🟡 {sym} {direction} 条件限价单已挂 限价={_lp_fmt}（离现价{_dev_pct_co:.1f}%） {_notional_co}U 钉点={anchor_src} SL={_sl_fmt}")
            continue

        if direction == 'SHORT':
            order_side = 'SELL'
            tp_side    = 'BUY'
            pos_side   = 'SHORT'
            # TP1 / 止损 来自 liquidation_map
            tp1_p = lmap['soft_tp1']
            sl_p  = lmap['hard_sl']
        else:
            order_side = 'BUY'
            tp_side    = 'SELL'
            pos_side   = 'LONG'
            tp1_p = lmap['soft_tp1']
            sl_p  = lmap['hard_sl']

        limit_fmt = fmt_price(limit_p, tick)
        tp1_fmt   = fmt_price(tp1_p,   tick)
        sl_fmt    = fmt_price(sl_p,     tick)

        # ── 条目7：仓位计算（复盘模式固定100U）──
        sig_type = r.get('sig_type', 'trend')
        try:
            from calc_position_size import calc as _cps
            _notional = _cps(sig_type, r['score'], r.get('direction', 'LONG'), r)
        except Exception:
            _notional = 100  # fallback 固定100U

        qty      = _notional / float(limit_fmt)
        qty_fmt  = fmt_qty(qty, step)

        entry_pct = (float(limit_fmt) - mark) / mark * 100
        tp_pct    = abs(float(tp1_fmt) - float(limit_fmt)) / float(limit_fmt) * 100
        sl_pct    = abs(float(sl_fmt)  - float(limit_fmt)) / float(limit_fmt) * 100
        tp_pnl    = abs(float(tp1_fmt) - float(limit_fmt)) * float(qty_fmt)
        rr        = tp_pct / sl_pct if sl_pct > 0 else 0

        # 盈亏比检查（最低 1:1.5）
        if rr < 1.5:
            notify(f"⚠️ {sym} {direction} 盈亏比 1:{rr:.1f} 不足 1:1.5，跳过")
            continue

        # 设杠杆
        api.request("POST", f"{api.BASE_FAPI}/fapi/v1/leverage",
            {"symbol": sym, "leverage": LEVERAGE})

        # 入场限价单
        r_entry = api.place_order(
            symbol=sym, side=order_side, order_type="LIMIT",
            quantity=qty_fmt, price=limit_fmt,
            positionSide=pos_side, timeInForce="GTC"
        )
        if not r_entry.get('orderId'):
            notify(f"⚠️ {sym} {direction} 入场挂单失败: {r_entry}")
            continue

        # ⚠️ 止盈止损不在此处挂单——入场限价单尚未成交，仓位不存在
        # ws_monitor.on_order_filled 会在入场单成交后自动挂止盈止损
        # 在 pos_state.json 中预存止盈止损价，供 on_order_filled 参考
        _POS_STATE_FILE = "/root/.openclaw/workspace/pos_state.json"
        try:
            import pathlib as _pl
            _ps_path = _pl.Path(_POS_STATE_FILE)
            _ps_now = json.loads(_ps_path.read_text()) if _ps_path.exists() else {}
            _ps_now.setdefault(sym, {})
            _ps_now[sym].update({
                'sl_price':         float(sl_fmt),
                'hard_sl':          float(sl_fmt),
                'tp1_price':        float(tp1_fmt),
                'direction':        direction,
                'entry_price_hint': float(limit_fmt),
                'qty':              float(qty_fmt),
                'source':           'auto_open',
                'pending_order_id': r_entry.get('orderId'),
                'opened_at':        int(time.time()),
            })
            _ps_path.write_text(json.dumps(_ps_now, indent=2, ensure_ascii=False))
        except Exception as _ps_err:
            notify(f"⚠️ {sym} pos_state 写入失败: {_ps_err}")

        # 更新 state
        state['opened'][sym] = {
            'direction': direction,
            'limit_price': float(limit_fmt),
            'tp1': float(tp1_fmt),
            'sl': float(sl_fmt),
            'qty': float(qty_fmt),
            'entry_order': r_entry.get('orderId'),
            'opened_at': now_str,
            'score': r['score'],
        }
        save_state(state)

        notify(
            f"🟢 **{sym} {direction} 已挂单** | {now_str}\n\n"
            f"评分：{r['score']}/{r['threshold']}（{r['sig_type']} | {entry_mode} 锚点:{anchor_src}）\n"
            f"RSI 1H={r['rsi1h']:.0f} 4H={r['rsi4h']:.0f} | FR={r['fr']:+.3f}%\n"
            f"入场限价：{limit_fmt}（距现价 {entry_pct:+.1f}%）\n"
            f"止盈：{tp1_fmt}（+{tp_pct:.1f}%，预计+{tp_pnl:.1f}U）\n"
            f"止损：{sl_fmt}（-{sl_pct:.1f}%）| 盈亏比 1:{rr:.1f}\n"
            f"名义 {_notional}U × {LEVERAGE}x | 入场#{r_entry.get('orderId')}（止盈止损将在成交后自动挂）"
        )

    except Exception as e:
        notify(f"⚠️ {sym} {direction} 开仓异常: {e}")

# ── 额外扫描：对未进 fired 但评分≥60 的信号，尝试挂条件限价单 ──
_cond_longs_now  = [v for v in cond_state.values() if v.get('direction') == 'LONG'  and now_ts_co - v.get('placed_at',0) < COND_TTL]
_cond_shorts_now = [v for v in cond_state.values() if v.get('direction') == 'SHORT' and now_ts_co - v.get('placed_at',0) < COND_TTL]

for r in cond_candidates:
    sym = r['sym']
    if sym in cond_state: continue      # 已有条件单
    if sym in existing_syms: continue   # 已持仓
    direction = r['direction']
    mark      = r['mark']
    k1h       = r['k1h']
    k4h       = r['k4h']

    # 容量检查
    _cl = len([v for v in cond_state.values() if v.get('direction')=='LONG'  and now_ts_co-v.get('placed_at',0)<COND_TTL])
    _cs = len([v for v in cond_state.values() if v.get('direction')=='SHORT' and now_ts_co-v.get('placed_at',0)<COND_TTL])
    if direction == 'LONG'  and _cl >= COND_MAX_LONG:  break
    if direction == 'SHORT' and _cs >= COND_MAX_SHORT: break

    try:
        lmap = liquidation_map(sym, k1h, k4h, direction)
        entry_mode, limit_p, _, anchor_src = calc_entry_mode(k1h, direction, lmap, final_score=r['score'])
        if entry_mode != 'COND_ORDER': continue   # 只处理 COND_ORDER

        tick = _tick_size_map.get(sym, 0.00001 if mark < 1 else (0.0001 if mark < 10 else 0.001))
        step = _step_map.get(sym, 1.0)
        sig_type = r.get('sig_type', 'trend')
        try:
            from calc_position_size import calc as _cps
            notional = _cps(sig_type, r['score'], r.get('direction', 'LONG'), r)
        except Exception:
            notional = 100  # fallback 固定100U（复盘模式）

        pos_side   = 'LONG' if direction == 'LONG' else 'SHORT'
        order_side = 'BUY'  if direction == 'LONG' else 'SELL'
        lp_fmt = fmt_price(limit_p, tick)
        qty_co = fmt_qty(notional / float(lp_fmt), step)
        sl_fmt = fmt_price(lmap['hard_sl'], tick)
        tp_fmt = fmt_price(lmap['soft_tp1'], tick)
        dev_pct = abs(float(lp_fmt) - mark) / mark * 100

        api.request("POST", f"{api.BASE_FAPI}/fapi/v1/leverage", {"symbol": sym, "leverage": LEVERAGE})
        r_co = api.place_order(symbol=sym, side=order_side, order_type="LIMIT",
                               quantity=qty_co, price=lp_fmt,
                               positionSide=pos_side, timeInForce="GTC")
        if not isinstance(r_co, dict) or not r_co.get('orderId'):
            continue
        now_ts_co2 = int(time.time())
        cond_state[sym] = {
            'direction': direction, 'order_id': r_co['orderId'],
            'limit_price': float(lp_fmt), 'sl': float(sl_fmt),
            'tp1': float(tp_fmt), 'qty': float(qty_co),
            'placed_at': now_ts_co2, 'expire_at': now_ts_co2 + COND_TTL,
            'score': r['score'], 'sig_type': sig_type,
            'anchor_src': anchor_src, 'notional': notional,
        }
        _save_cond_state(cond_state)
        print(f"  🟡 {sym} {direction} 条件限价单已挂 限价={lp_fmt}（离现价{dev_pct:.1f}%）{notional}U 锚={anchor_src}")
    except Exception as _ce:
        print(f"  ⚠️ {sym} 条件单尝试异常: {_ce}")
