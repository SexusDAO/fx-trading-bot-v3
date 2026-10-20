#!/usr/bin/env python3
"""
on_long_monitor.py — ONUSDT 多单动态监控
功能：
1. 空头急跌 → 市价平仓保护
2. 盈利扩大 → 动态上移止损（锁住利润）
3. 价格上涨 → 动态上移止盈（追踪上方清算区）
4. 平仓后 → 自动分析再入场方向
5. 无异常 → HEARTBEAT_OK 静默
"""
import sys, json, datetime, requests, time
sys.path.insert(0, "/root/.openclaw/workspace")
import binance_api as api
import entry_logic as el
import coinglass_api as cg
from order_registry import registry

SYM           = 'ONUSDT'
POSITION_SIDE = 'LONG'
STATE_FILE    = '/root/.openclaw/workspace/on_long_state.json'
CRON_JOB_ID   = 'LONG_MONITOR_CRON_ID'   # 启动后更新
NOTIFY_TO     = '432401404:thread:019f9203-3b29-7d9e-bfb4-b67c0b06587c'
ACTIVE_SYMBOL_FILE = '/root/.openclaw/workspace/ws_cache/active_symbols.json'

# 启动时锤定当前标的 —— position_manager 看到后不自行开单或撤单
def _lock_active_symbol(sym, role='on_long_monitor'):
    try:
        try:
            with open(ACTIVE_SYMBOL_FILE) as f:
                data = json.load(f)
        except Exception:
            data = {}
        data[sym] = {'role': role, 'ts': int(time.time())}
        with open(ACTIVE_SYMBOL_FILE, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass

_lock_active_symbol(SYM)

# 急跌平仓阈值（多单：价格下跌）
BEAR_CRASH_A_MOM = -5.0   # 15m近4根跌幅 < -5% 且 RSI<45
BEAR_CRASH_B_MOM = -8.0   # 15m近4根跌幅 < -8%（急跌，无视RSI）
LOSS_PROTECT_PCT = 8.0    # 浮亏超8%触发保护

# 止盈上移触发阈值
TP_ADJUST_RISE = 1.0      # 价格上涨 >1% 且盈利区，触发止盈上移

# 动态止损档位（多单：止损在均价下方/上方锁利润）
SL_TIERS = [
    (18.0, 1.08),   # 浮盈≥18% → 止损移到均价×1.08
    (12.0, 1.05),   # 浮盈≥12% → 止损移到均价×1.05
    ( 8.0, 1.03),   # 浮盈≥8%  → 止损移到均价×1.03
    ( 5.0, 1.02),   # 浮盈≥5%  → 止损移到均价×1.02
    ( 3.0, 1.00),   # 浮盈≥3%  → 止损移到均价（保本）
]

# 再入场冷却期（秒）
REENTRY_COOLDOWN = 900    # 平仓后15分钟内不再入场

# 保本止损：开仓后立即挂，挂在均价×BREAKEVEN_FACTOR（多单=1.00即保本）
# 等价格站稳 SL_ACTIVATE_THRESHOLD 后才真正挂追踪止损
BREAKEVEN_BUFFER = 0.005  # 保本止损在均价下方0.5%（给插针留空间）

now_str = datetime.datetime.utcnow().strftime('%H:%M UTC')
TICK = 0.00001

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except:
        return {}

def save_state(s):
    with open(STATE_FILE, 'w') as f:
        json.dump(s, f, indent=2, ensure_ascii=False)

def notify(msg):
    print(f'__NOTIFY__:{msg}')

def close_cron():
    import subprocess
    cron_id = state.get('cron_job_id', CRON_JOB_ID)
    subprocess.run(['openclaw', 'cron', 'rm', cron_id], capture_output=True)

def _cancel_sl(sl_id):
    try:
        api.cancel_algo_order(SYM, int(sl_id))
    except Exception:
        try:
            api.cancel_order(SYM, sl_id)
        except Exception:
            pass

def _place_sl(stop_price_fmt):
    """挂止损，返回 algoId 或 None，并登记注册表"""
    try:
        r = api.place_algo_order(
            symbol=SYM, side='SELL', order_type='STOP_MARKET',
            trigger_price=stop_price_fmt, position_side=POSITION_SIDE,
            quantity=str(abs(int(amt)))
        )
        oid = r.get('algoId') or r.get('orderId')
        if oid:
            registry.register(str(oid), SYM, 'SELL', 'sl', 'on_long_monitor',
                               price=float(stop_price_fmt), qty=abs(int(amt)))
            return str(oid)
    except Exception:
        return None

def _place_tp(price_fmt, qty, role):
    """挂止盈限价单，返回 orderId，并登记注册表"""
    try:
        r = api.place_order(
            symbol=SYM, side='SELL', order_type='LIMIT',
            quantity=str(qty), price=price_fmt,
            positionSide=POSITION_SIDE, timeInForce='GTC'
        )
        oid = r.get('orderId')
        if oid:
            registry.register(str(oid), SYM, 'SELL', role, 'on_long_monitor',
                               price=float(price_fmt), qty=float(qty))
        return r
    except Exception as e:
        return {}

# ── 读取 state ──
state = load_state()

# ── 检查仓位（三重确认防抖）──
positions = api.get_positions()
on_pos = [p for p in positions
          if p.get('symbol') == SYM
          and p.get('positionSide', '').upper() == 'LONG'
          and float(p.get('positionAmt', 0)) > 0]

if not on_pos:
    time.sleep(2)
    positions2 = api.get_positions()
    on_pos2 = [p for p in positions2
               if p.get('symbol') == SYM
               and p.get('positionSide', '').upper() == 'LONG'
               and float(p.get('positionAmt', 0)) > 0]
    if not on_pos2:
        time.sleep(5)
        positions3 = api.get_positions()
        on_pos3 = [p for p in positions3
                   if p.get('symbol') == SYM
                   and p.get('positionSide', '').upper() == 'LONG'
                   and float(p.get('positionAmt', 0)) > 0]
        if not on_pos3:
            close_cron()
            notify(f'✅ **{SYM} LONG 仓位已关闭，监控自动停止** | {now_str}')
            # 冷却期15分钟后再入场
            time.sleep(REENTRY_COOLDOWN)
            try:
                _analyze_reentry(SYM)
            except Exception as e:
                notify(f'⚠️ 再入场分析失败: {e}')
            sys.exit(0)
        on_pos = on_pos3
    else:
        on_pos = on_pos2

p     = on_pos[0]
amt   = float(p['positionAmt'])
entry = float(p['entryPrice'])
mark  = float(p['markPrice'])
pnl   = float(p['unRealizedProfit'])

pct_move  = (mark - entry) / entry * 100
is_profit = mark > entry
profit_pct = abs(pct_move) if is_profit else -abs(pct_move)

# ── 行情数据 ──
k1h  = requests.get('https://fapi.binance.com/fapi/v1/klines',
    params={'symbol': SYM, 'interval': '1h', 'limit': 50}, timeout=8).json()
k4h  = requests.get('https://fapi.binance.com/fapi/v1/klines',
    params={'symbol': SYM, 'interval': '4h', 'limit': 50}, timeout=8).json()
k15m = requests.get('https://fapi.binance.com/fapi/v1/klines',
    params={'symbol': SYM, 'interval': '15m', 'limit': 20}, timeout=8).json()

c1h   = [float(k[4]) for k in k1h]
rsi_1h = el.rsi_calc(c1h)

c15m_cl = [float(k[4]) for k in k15m]
p15m_4  = [float(k[4]) for k in k15m[-4:]]
mom_15m = (p15m_4[-1] - p15m_4[0]) / p15m_4[0] * 100 if p15m_4[0] > 0 else 0

last_price = state.get('last_price', mark)

# ── 挂单 ──
open_orders = api.get_open_orders()
on_orders   = [o for o in open_orders if o.get('symbol') == SYM]
tp_orders   = [o for o in on_orders
               if o.get('side') == 'SELL' and o.get('type') == 'LIMIT']

# ══════════════════════════════════════════
# 情况1：急跌 / 反向保护 → 市价平仓
# ══════════════════════════════════════════
bear_crash_A = mom_15m < BEAR_CRASH_A_MOM and rsi_1h < 45
bear_crash_B = mom_15m < BEAR_CRASH_B_MOM
loss_protect = pct_move < -LOSS_PROTECT_PCT

if bear_crash_A or bear_crash_B or loss_protect:
    if loss_protect:
        reason = f'浮亏超{LOSS_PROTECT_PCT}%（当前{pct_move:.1f}%），触发保护性平仓'
    elif bear_crash_B:
        reason = f'15m急跌 {mom_15m:.1f}%，多头崩盘风险'
    else:
        reason = f'15m跌 {mom_15m:.1f}% + RSI 1H={rsi_1h:.0f}，空头动能转强'

    for o in on_orders:
        api.cancel_order(SYM, o['orderId'])

    r = api.place_order(
        symbol=SYM, side='SELL', order_type='MARKET',
        quantity=str(abs(int(amt))), positionSide=POSITION_SIDE
    )
    notify(
        f'⚡ **{SYM} LONG 已市价平仓** | {now_str}\n\n'
        f'触发：{reason}\n'
        f'平仓均价：约 {mark:.5f}\n'
        f'仓位盈亏：{pnl:+.2f}U（{profit_pct:.1f}%）\n'
        f'订单号：{r.get("orderId","?")}'
    )
    close_cron()
    try:
        _analyze_reentry(SYM)
    except Exception:
        pass
    sys.exit(0)

# ══════════════════════════════════════════
# 情况2：动态止损（随盈利扩大上移）
# 前提：价格必须先站稳 0.131 以上才开始挂止损
# ══════════════════════════════════════════
SL_ACTIVATE_THRESHOLD = 0.130   # 低于此价不挂止损，站稳0.130后开始保护

def _target_sl_price():
    for threshold, factor in SL_TIERS:
        if profit_pct >= threshold:
            return entry * factor
    return None

target_sl = _target_sl_price()

# 从 state 读取当前止损
current_sl_price = state.get('sl_price')
sl_order_id      = state.get('sl_order_id') if state.get('sl_activated') else None

# 价格未站稳 0.131 → 不挂止损
if mark < SL_ACTIVATE_THRESHOLD:
    # 清空 state 里的止损记录（用户已手动移除）
    if state.get('sl_activated') and not sl_order_id:
        state['sl_activated'] = False
        state['sl_price']     = None
        state['sl_order_id']  = None
        save_state(state)
elif target_sl is not None:
    target_sl_fmt = el.fmt_price(target_sl, TICK)
    target_sl_f   = float(target_sl_fmt)

    need_place  = current_sl_price is None
    # 多单止损只能往上移（新止损 > 旧止损）
    need_update = (current_sl_price is not None) and (target_sl_f > current_sl_price + TICK)

    if need_place or need_update:
        if sl_order_id:
            _cancel_sl(sl_order_id)

        new_oid = _place_sl(target_sl_fmt)

        for thr, fac in SL_TIERS:
            if profit_pct >= thr:
                tier_desc = f'浮盈 {profit_pct:.1f}% ≥{thr}%，止损→均价×{fac}'
                break

        if new_oid:
            action = '首次挂止损' if need_place else f'止损上移 {current_sl_price:.5f}→{target_sl_fmt}'
            notify(
                f'🛡️ **{SYM} {action}** | {now_str}\n\n'
                f'触发：{tier_desc}\n'
                f'止损价：{target_sl_fmt}\n'
                f'当前浮盈：{profit_pct:.2f}%（{pnl:+.2f}U）\n'
                f'订单号：#{new_oid}'
            )
            state['sl_activated'] = True
            state['sl_price']     = target_sl_f
            state['sl_order_id']  = new_oid
            save_state(state)

# ══════════════════════════════════════════
# 情况3：价格上涨 → 上移止盈
# ══════════════════════════════════════════
price_rise = (mark - last_price) / last_price * 100 if last_price > 0 else 0
bull_move  = price_rise > TP_ADJUST_RISE and is_profit

if bull_move:
    try:
        zones   = cg.get_liq_zones(SYM, mark, range_='3d', top_n=5)
        above   = zones.get('above', [])
        # TP2 追踪：锚定 Coinglass 第一道清算密集区（介于 TP1 和 TP3 之间）
        # TP3 固定在 0.16302，不追踪
        TP3_FIXED = state.get('tp3_price', 0.16302)
        TP1_FIXED = state.get('tp1_price', 0.14252)
        # 找第一个 > TP1 且 < TP3 的清算区作为 TP2 新锚点
        tp2_candidates = [z[0] for z in above if z[0] > TP1_FIXED * 1.005 and z[0] < TP3_FIXED]
        if tp2_candidates:
            tp2_new = min(tp2_candidates)   # 最近的那个
            tp2_fmt = el.fmt_price(tp2_new, TICK)

            # 只追踪 TP2，TP1 和 TP3 固定不动
            tp2_order = state.get('tp2_order')
            tp2_orders = [o for o in tp_orders if str(o.get('orderId')) == str(tp2_order)]
            cur_tp2_price = float(tp2_orders[0]['price']) if tp2_orders else None

            if cur_tp2_price is None or float(tp2_fmt) > cur_tp2_price + TICK:
                tp2_qty = state.get('tp2_qty', abs(int(amt)) // 3)
                if tp2_orders:
                    api.cancel_order(SYM, tp2_orders[0]['orderId'])
                r_tp2 = api.place_order(
                    symbol=SYM, side='SELL', order_type='LIMIT',
                    quantity=str(tp2_qty), price=tp2_fmt,
                    positionSide=POSITION_SIDE, timeInForce='GTC'
                )
                if r_tp2.get('orderId'):
                    registry.register(str(r_tp2['orderId']), SYM, 'SELL', 'tp2',
                                      'on_long_monitor', price=float(tp2_fmt), qty=tp2_qty)
                tp2_pnl = (float(tp2_fmt) - entry) * tp2_qty
                notify(
                    f'📈 **{SYM} TP2 上移（Coinglass锚定）** | {now_str}\n\n'
                    f'价格从 {last_price:.5f} 涨至 {mark:.5f}（+{price_rise:.1f}%）\n'
                    f'新 TP2：{tp2_fmt}（清算密集区锚定）→ 预计+{tp2_pnl:.1f}U\n'
                    f'TP1 {TP1_FIXED:.5f} 固定 | TP3 {TP3_FIXED:.5f} 固定\n'
                    f'当前浮盈：{pnl:+.2f}U'
                )
                state['tp2_order'] = r_tp2.get('orderId')
                state['tp2_price'] = float(tp2_fmt)
    except Exception:
        pass

# ══════════════════════════════════════════
# 情况5：价格下跌时→推送持仓信心分析
# ══════════════════════════════════════════
price_drop_pct = (last_price - mark) / last_price * 100 if last_price > 0 else 0
is_dropping    = price_drop_pct > 0.3   # 相比上一个5分钟周期下跌超过0.3%

if is_dropping:
    try:
        # 拉富政指标辅证
        k15m_full = requests.get('https://fapi.binance.com/fapi/v1/klines',
            params={'symbol': SYM, 'interval': '15m', 'limit': 30}, timeout=8).json()
        c15m_full = [float(k[4]) for k in k15m_full]
        rsi15m_v  = el.rsi_calc(c15m_full)

        p4_v  = [float(k[4]) for k in k15m_full[-4:]]
        p8_v  = [float(k[4]) for k in k15m_full[-8:]]
        mom4_v = (p4_v[-1]-p4_v[0])/p4_v[0]*100
        mom8_v = (p8_v[-1]-p8_v[0])/p8_v[0]*100

        vols_v = [float(k[5]) for k in k15m_full]
        vr_v   = sum(vols_v[-3:])/3 / (sum(vols_v[-10:-3])/7) if sum(vols_v[-10:-3])>0 else 1

        # 订单簿
        ob_v     = requests.get('https://fapi.binance.com/fapi/v1/depth',
            params={'symbol': SYM, 'limit': 20}, timeout=5).json()
        bid_wall = max([(float(b[0]),float(b[1])) for b in ob_v['bids'][:10]], key=lambda x:x[1])
        ask_wall = max([(float(a[0]),float(a[1])) for a in ob_v['asks'][:10]], key=lambda x:x[1])

        # Coinglass 下方支撑
        zones_v = cg.get_liq_zones(SYM, mark, range_='3d', top_n=3)
        below_v = zones_v.get('below', [])[:2]

        # 判断回落性质
        is_weak_drop   = vr_v < 1.0 and mom4_v > -3.0           # 缩量小跌
        has_bid_support = bid_wall[1] > 2000                     # 买单墙厕厉
        rsi_ok         = rsi15m_v > 30 and rsi15m_v < 55        # RSI 在合理区间
        tp1_dist       = (0.14252 - mark) / mark * 100
        tp1_pnl_left   = (0.14252 - entry) * abs(int(amt)) - pnl

        # 构建信心分析
        signals = []
        warnings = []

        if is_weak_drop:
            signals.append(f'缩量回调（量比={vr_v:.1f}x），卖压不重')
        else:
            warnings.append(f'放量下跌（量比={vr_v:.1f}x），需要小心')

        if has_bid_support:
            signals.append(f'买单墙 {bid_wall[0]:.5f} x{bid_wall[1]:.0f}，下方承接力强')
        else:
            warnings.append(f'买单墙薄弱（x{bid_wall[1]:.0f}），注意支撑')

        if rsi_ok:
            signals.append(f'RSI 15m={rsi15m_v:.0f}，未达超卖，结构未破坏')
        else:
            warnings.append(f'RSI 15m={rsi15m_v:.0f}，进入超卖区')

        if below_v:
            nearest = below_v[0]
            signals.append(f'Coinglass 下方最近支撑 {nearest[0]:.5f}（密度={nearest[1]/1e4:.0f}万，-{(mark-nearest[0])/mark*100:.1f}%）')

        # 持仓评分：信号多 = 持仓
        hold_score = len(signals)
        hold_judge = '✅ 建议继续持仓' if hold_score >= 2 else '⚠️ 需要关注下方支撑'

        signal_text  = '\n'.join(f'✔ {s}' for s in signals)  if signals  else '暂无明显信号'
        warning_text = '\n'.join(f'⚠ {w}' for w in warnings) if warnings else ''

        msg = (
            f'📊 **ON 多单回调分析** | {now_str}\n\n'
            f'现价：{mark:.5f}（相比上一周期 -{price_drop_pct:.1f}%）\n'
            f'均价：{entry:.5f} | 浮盈：{pnl:+.2f}U（{profit_pct:+.1f}%）\n\n'
            f'**持仓信号：**\n{signal_text}\n'
        )
        if warning_text:
            msg += f'\n**需注意：**\n{warning_text}\n'
        msg += (
            f'\n**TP1 {0.14252:.5f}** 距现价 +{tp1_dist:.1f}%，'
            f'触达还有 +{tp1_pnl_left:.0f}U 空间\n'
            f'{hold_judge}'
        )
        notify(msg)
    except Exception:
        pass

# ══════════════════════════════════════════
# 更新 last_price，保存 state
# ══════════════════════════════════════════
state['last_price'] = mark
save_state(state)

# ══════════════════════════════════════════
# 再入场分析函数（平仓后调用）
# ══════════════════════════════════════════
def _analyze_reentry(sym):
    """平仓后1分钟，多空双向评分，择优挂1000U限价单"""
    import coinglass_api as _cg
    now_r    = datetime.datetime.utcnow().strftime('%H:%M UTC')
    NOTIONAL = 1000
    tick_r   = 0.00001
    step_r   = 1.0

    k1h_r  = requests.get('https://fapi.binance.com/fapi/v1/klines',
        params={'symbol': sym, 'interval': '1h',  'limit': 100}, timeout=8).json()
    k4h_r  = requests.get('https://fapi.binance.com/fapi/v1/klines',
        params={'symbol': sym, 'interval': '4h',  'limit': 100}, timeout=8).json()
    k15m_r = requests.get('https://fapi.binance.com/fapi/v1/klines',
        params={'symbol': sym, 'interval': '15m', 'limit': 60},  timeout=8).json()
    ticker_r = api.get_ticker_24h(sym)
    fr_r     = requests.get(f'https://fapi.binance.com/fapi/v1/premiumIndex?symbol={sym}', timeout=5).json()
    fr_raw_r = float(fr_r.get('lastFundingRate', 0))
    fr_pct_r = fr_raw_r * 100
    price_r  = float(k1h_r[-1][4])
    chg24_r  = float(ticker_r.get('priceChangePercent', 0))

    c1h_r  = [float(k[4]) for k in k1h_r]
    c4h_r  = [float(k[4]) for k in k4h_r]
    rsi_1h_r = el.rsi_calc(c1h_r)
    rsi_4h_r = el.rsi_calc(c4h_r)
    ema20_4h_r = el.ema_calc(c4h_r[-20:], 20)
    ema50_4h_r = el.ema_calc(c4h_r[-50:], 50)
    ema4h_dir_r = '多排' if ema20_4h_r > ema50_4h_r else '空排'

    from entry_logic import classify_signal, calc_score_trend, calc_score_reversal
    from entry_logic import check_funding_settlement_window, check_oi_divergence

    sig_type_r = classify_signal(k1h_r, k4h_r, fr_raw_r, chg24_r)
    if sig_type_r == 'trend':
        score_r, _ = calc_score_trend(sym, k1h_r, k4h_r, k15m_r, ticker_r, fr_raw_r)
        threshold_r = 75
    else:
        score_r, _ = calc_score_reversal(sym, k1h_r, k4h_r, k15m_r, ticker_r, fr_raw_r)
        threshold_r = 65

    blocked_r, _ = check_funding_settlement_window(fr_pct_r)
    oi_div_r, _  = check_oi_divergence(sym)

    can_short = score_r >= threshold_r and not blocked_r and not oi_div_r and (rsi_1h_r > 65 or rsi_4h_r > 60)
    can_long  = score_r >= threshold_r and not blocked_r and not oi_div_r and (rsi_1h_r < 35 or rsi_4h_r < 40)

    lmap_s = el.liquidation_map(sym, k1h_r, k4h_r, 'SHORT')
    lmap_l = el.liquidation_map(sym, k1h_r, k4h_r, 'LONG')

    def _place_entry(side, lmap):
        order_side = 'SELL' if side == 'SHORT' else 'BUY'
        tp_side    = 'BUY'  if side == 'SHORT' else 'SELL'
        limit_p    = lmap.get('safe_entry', price_r * (1.005 if side=='SHORT' else 0.995))
        sl_p       = lmap.get('hard_sl',   price_r * (1.03  if side=='SHORT' else 0.97))

        try:
            zones_r = _cg.get_liq_zones(sym, price_r, range_='3d', top_n=3)
            if side == 'SHORT' and zones_r.get('below'):
                tp1_p = zones_r['below'][0][0]
            elif side == 'LONG' and zones_r.get('above'):
                tp1_p = zones_r['above'][0][0]
            else:
                tp1_p = lmap.get('soft_tp1', price_r * (0.95 if side=='SHORT' else 1.05))
        except Exception:
            tp1_p = lmap.get('soft_tp1', price_r * (0.95 if side=='SHORT' else 1.05))

        limit_fmt = el.fmt_price(limit_p, tick_r)
        sl_fmt    = el.fmt_price(sl_p,    tick_r)
        tp1_fmt   = el.fmt_price(tp1_p,   tick_r)
        qty_fmt   = el.fmt_qty(NOTIONAL / float(limit_fmt), step_r)

        entry_pct = (float(limit_fmt) - price_r) / price_r * 100
        sl_pct    = abs(float(sl_fmt)  - float(limit_fmt)) / float(limit_fmt) * 100
        tp_pct    = abs(float(tp1_fmt) - float(limit_fmt)) / float(limit_fmt) * 100
        tp_pnl    = abs(float(tp1_fmt) - float(limit_fmt)) * float(qty_fmt)
        rr        = tp_pct / sl_pct if sl_pct > 0 else 0

        bal   = api.request("GET", api.BASE_FAPI + "/fapi/v2/balance", {})
        avail = float([b for b in bal if b['asset']=='USDT'][0]['availableBalance'])
        slots = len([p2 for p2 in api.get_positions() if abs(float(p2.get('positionAmt',0)))>0])
        if slots >= 5 or avail < 50:
            notify(f'🟡 {sym} {side} 评分达标但仓位满或余额不足（{slots}/5, {avail:.0f}U）\n参考入场：{limit_fmt} TP：{tp1_fmt} SL：{sl_fmt}')
            return

        api.request('POST', 'https://fapi.binance.com/fapi/v1/leverage', {'symbol': sym, 'leverage': 50})
        r_e = api.place_order(symbol=sym, side=order_side, order_type='LIMIT',
            quantity=qty_fmt, price=limit_fmt, positionSide=side, timeInForce='GTC')
        if not r_e.get('orderId'):
            notify(f'⚠️ {sym} {side} 入场挂单失败')
            return
        r_tp = api.place_order(symbol=sym, side=tp_side, order_type='LIMIT',
            quantity=qty_fmt, price=tp1_fmt, positionSide=side, timeInForce='GTC')
        r_sl = api.place_algo_order(symbol=sym, side=tp_side, order_type='STOP_MARKET',
            trigger_price=sl_fmt, position_side=side, quantity=qty_fmt)
        notify(
            f'🟢 **{sym} 再入场 {side} 已挂单** | {now_r}\n\n'
            f'评分：{score_r}/{threshold_r} | RSI 1H={rsi_1h_r:.0f} 4H={rsi_4h_r:.0f}\n'
            f'入场：{limit_fmt}（{entry_pct:+.1f}%）| TP：{tp1_fmt}（+{tp_pct:.1f}%，+{tp_pnl:.1f}U）\n'
            f'SL：{sl_fmt}（-{sl_pct:.1f}%）| 盈亏比 1:{rr:.1f} | 全仓单档止盈\n'
            f'入场#{r_e.get("orderId")} TP#{r_tp.get("orderId")}'
        )

    if can_short and can_long:
        can_short = (rsi_1h_r - 65) >= (35 - rsi_1h_r)
        can_long  = not can_short

    if can_short:
        _place_entry('SHORT', lmap_s)
    elif can_long:
        _place_entry('LONG', lmap_l)
    else:
        reasons = []
        if score_r < threshold_r:            reasons.append(f'评分{score_r}<{threshold_r}')
        if blocked_r:                        reasons.append('资金费率窗口')
        if oi_div_r:                         reasons.append('OI背离')
        if not (rsi_1h_r > 65 or rsi_4h_r > 60 or rsi_1h_r < 35 or rsi_4h_r < 40):
            reasons.append(f'RSI中性({rsi_1h_r:.0f}/{rsi_4h_r:.0f})')
        notify(
            f'🟡 **{sym} 暂无清晰再入场信号** | {now_r}\n\n'
            f'原因：{", ".join(reasons)}\n'
            f'评分：{score_r} | RSI 1H={rsi_1h_r:.0f} 4H={rsi_4h_r:.0f} | EMA 4H={ema4h_dir_r}'
        )

# ══════════════════════════════════════════
# 情况6：检测 TP 成交 → 自动触发全市场扫描
# ══════════════════════════════════════════
def _check_tp_filled_and_scan():
    """检查 TP1/TP2/TP3 是否已成交，成交后立即触发短扫+多币扫"""
    tp_map = {
        'tp1': state.get('tp1_order'),
        'tp2': state.get('tp2_order'),
        'tp3': state.get('tp3_order'),
    }
    filled_tps = []
    try:
        # 获取当前所有挂单（只有未成交的单才在列表里）
        open_oids = {str(o['orderId']) for o in open_orders}
        for role, oid in tp_map.items():
            if not oid:
                continue
            # 如果之前 state 里有这个订单，但现在挂单列表里没了 → 可能已成交
            if str(oid) not in open_oids:
                # 查历史订单确认是 FILLED
                try:
                    hist = api.get_order(SYM, int(oid))
                    if hist.get('status') == 'FILLED':
                        filled_tps.append((role, oid, hist))
                except Exception:
                    pass
    except Exception:
        return

    if not filled_tps:
        return

    for role, oid, hist in filled_tps:
        qty_filled = float(hist.get('executedQty', 0))
        price_filled = float(hist.get('avgPrice', hist.get('price', 0)))
        pnl_this = (price_filled - entry) * qty_filled
        notify(
            f'✅ **{SYM} {role.upper()} 止盈成交** | {now_str}\n\n'
            f'成交价：{price_filled:.5f} | 数量：{qty_filled:.0f}\n'
            f'本档盈利：+{pnl_this:.1f}U\n'
            f'立即触发全市场扫描...'
        )
        # 清理 state 里的 TP 订单记录
        state[f'{role}_order'] = None

    save_state(state)

    # 解除 active_symbol_set 锁定（如果全部 TP 都成交了）
    remaining_tps = [v for k, v in {
        'tp1': state.get('tp1_order'),
        'tp2': state.get('tp2_order'),
        'tp3': state.get('tp3_order'),
    }.items() if v is not None]
    if not remaining_tps:
        _release_active_symbol(SYM)

    # 立即触发扫描（不等下一个 10/40 分钟周期）
    _trigger_immediate_scan()


ACTIVE_SYMBOL_FILE = '/root/.openclaw/workspace/ws_cache/active_symbols.json'

def _release_active_symbol(sym):
    """从 active_symbol_set 移除，允许 position_manager 接管"""
    try:
        import json as _j
        try:
            with open(ACTIVE_SYMBOL_FILE) as f:
                data = _j.load(f)
        except Exception:
            data = {}
        if sym in data:
            del data[sym]
        with open(ACTIVE_SYMBOL_FILE, 'w') as f:
            _j.dump(data, f, indent=2)
    except Exception:
        pass


def _trigger_immediate_scan():
    """TP 成交后立即触发一次 short-scanner + altcoin-monitor，不等 cron 周期"""
    import subprocess
    # 读取 cron job ids
    try:
        result = subprocess.run(['openclaw', 'cron', 'list', '--json'],
                                capture_output=True, text=True, timeout=10)
        jobs = json.loads(result.stdout) if result.stdout.strip() else []
        for j in jobs:
            if j.get('name') in ('short-scanner', 'altcoin-monitor'):
                jid = j.get('id', '')
                subprocess.run(['openclaw', 'cron', 'trigger', jid],
                               capture_output=True, timeout=10)
    except Exception:
        pass
    notify(
        f'🔍 **市场扫描已触发** | {now_str}\n'
        f'{SYM} 止盈成交后，short-scanner + altcoin-monitor 立即扫描中'
    )


_check_tp_filled_and_scan()

# 静默退出
