"""
sim_week_20260728.py — 2026-07-28 ~ 2026-08-03 全周完整工作流模拟

持仓跨天延续：仓位不在收盘强平，而是真实持有到止损/止盈/时间止损
账户初始 3000U，全程不补仓
"""

import sys, os, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'v3'))

from datetime import datetime, timezone, timedelta
from collections import defaultdict

from modules.dynamic_baseline import compute as compute_baseline, _closes, _ema, _atr_series, _mean
from modules.market_bias      import judge_market_state, get_market_state_info, MARKET_STATE_HALT
from modules.scorer           import score as do_score
from modules.scene_matcher    import match as do_match
from modules.pre_order_gate   import pre_order_gate
from modules.position_size    import calc_position_size
from modules.position_oscillation_check import check_tp1_tp2_oscillation
from modules.time_stop        import check_time_stop

# ── 加载数据 ──────────────────────────────────────────────────────────────────
with open('/tmp/sim_week_data.json') as f:
    RAW = json.load(f)

KDATA    = RAW['kdata']
FDATA    = RAW['fdata']
DAY_KEYS = RAW['days']   # ['0728','0729',...,'0803']

SCAN_SYMBOLS = list(KDATA.keys())

BASE_DATE = datetime(2026, 7, 28, tzinfo=timezone.utc)

# ── 账户（跨天持续）──────────────────────────────────────────────────────────
ACCOUNT = {
    'equity':     3000.0,
    'positions':  {},   # key=f"{sym}_{dir}", val=pos dict
    'orders':     [],   # pending limit orders
    'trade_log':  [],
}

# ── 全周跟踪 ─────────────────────────────────────────────────────────────────
WEEK_LOG = []   # 每小时一条摘要

# ── 工具 ──────────────────────────────────────────────────────────────────────
def global_hour(day_idx: int, hour: int) -> int:
    """返回全局小时编号（0=07-28 00:00, 167=08-03 23:00）"""
    return day_idx * 24 + hour

def get_k1h_until(sym: str, day_idx: int, hour: int) -> list:
    """
    返回截至当前时刻的 1H K线历史，最多 80 根。
    历史来源：4H 转换 + 之前所有天 1H + 今天前 hour+1 根。
    """
    # 4H 历史背景（最多取前 12 根 4H，约 48H）
    hist_4h = KDATA.get(sym, {}).get('4h', [])
    if not isinstance(hist_4h, list): hist_4h = []
    # 取到当天 00:00 之前的 4H bars
    day_cutoff_ms = int((BASE_DATE + timedelta(days=day_idx)).timestamp() * 1000)
    hist_4h_before = [k for k in hist_4h if k[0] < day_cutoff_ms][-12:]

    hist_1h = []
    for bar in hist_4h_before:
        o, h, l, c, v = float(bar[1]), float(bar[2]), float(bar[3]), float(bar[4]), float(bar[5])
        ts = bar[0]
        for i in range(4):
            sub_c = o + (c - o) * (i + 1) / 4
            hist_1h.append([ts + i*3600000,
                             str(o if i == 0 else o + (c-o)*i/4),
                             str(h), str(l), str(sub_c),
                             str(v / 4), 0, '0', 0, '0', '0', '0'])

    # 之前所有天的完整 1H
    day_labels_before = DAY_KEYS[:day_idx]
    for dl in day_labels_before:
        bars = KDATA.get(sym, {}).get('1h_by_day', {}).get(dl, [])
        if isinstance(bars, list):
            hist_1h.extend(bars)

    # 今天截至 hour
    today_bars = KDATA.get(sym, {}).get('1h_by_day', {}).get(DAY_KEYS[day_idx], [])
    if isinstance(today_bars, list):
        hist_1h.extend(today_bars[:hour + 1])

    combined = hist_1h[-80:] if len(hist_1h) >= 80 else hist_1h
    return combined

def get_k4h_recent(sym: str) -> list:
    v = KDATA.get(sym, {}).get('4h', [])
    return v[-10:] if isinstance(v, list) else []

def get_price_at(sym: str, day_idx: int, hour: int) -> float:
    bars = KDATA.get(sym, {}).get('1h_by_day', {}).get(DAY_KEYS[day_idx], [])
    if isinstance(bars, list) and hour < len(bars):
        return float(bars[hour][4])
    return 0.0

def get_high_at(sym: str, day_idx: int, hour: int) -> float:
    bars = KDATA.get(sym, {}).get('1h_by_day', {}).get(DAY_KEYS[day_idx], [])
    if isinstance(bars, list) and hour < len(bars):
        return float(bars[hour][2])
    return 0.0

def get_low_at(sym: str, day_idx: int, hour: int) -> float:
    bars = KDATA.get(sym, {}).get('1h_by_day', {}).get(DAY_KEYS[day_idx], [])
    if isinstance(bars, list) and hour < len(bars):
        return float(bars[hour][3])
    return 0.0

def get_funding(sym: str, day_idx: int, hour: int) -> float:
    rates = FDATA.get(sym, [])
    if not isinstance(rates, list) or not rates: return 0.0001
    target_ms = int((BASE_DATE + timedelta(days=day_idx, hours=hour)).timestamp() * 1000)
    best = rates[0]
    for r in rates:
        if isinstance(r, dict) and r.get('fundingTime', 0) <= target_ms:
            best = r
    return float(best.get('fundingRate', 0.0001)) if isinstance(best, dict) else 0.0001


# ── 输出 ──────────────────────────────────────────────────────────────────────
def C(color, s):
    m = {'g':'\033[92m','r':'\033[91m','y':'\033[93m','c':'\033[96m',
         'b':'\033[1m','x':'\033[0m','u':'\033[94m','z':'\033[90m'}
    return f"{m.get(color,'')}{s}{m['x']}"

def hdr(t):
    print(f"\n{C('b','═'*64)}\n{C('b',t)}\n{C('b','═'*64)}")

def log(msg, lv='z'):
    lmap = {'z':'\033[90m','g':'\033[92m','r':'\033[91m','y':'\033[93m',
            'c':'\033[96m','u':'\033[94m','b':'\033[1m'}
    print(f"  {lmap.get(lv,'')}{msg}\033[0m")


# ── 扫描 ──────────────────────────────────────────────────────────────────────
def scan(day_idx: int, hour: int, state: str, state_info: dict) -> list:
    k1h_btc   = get_k1h_until('BTCUSDT', day_idx, hour)
    btc_closes = _closes(k1h_btc) if k1h_btc else []
    if len(btc_closes) >= 50:
        ema20 = _ema(btc_closes[-20:], 20)
        ema50 = _ema(btc_closes[-50:], 50)
        bp    = float(k1h_btc[-1][4])
        btc_trend = ('up'   if ema20 > ema50 and bp > ema20 else
                     'down' if ema20 < ema50 and bp < ema20 else 'neutral')
    else:
        btc_trend = 'neutral'

    results = []
    for sym in SCAN_SYMBOLS:
        k1h = get_k1h_until(sym, day_idx, hour)
        k4h = get_k4h_recent(sym)
        if len(k1h) < 20: continue
        mp  = float(k1h[-1][4])
        if mp <= 0: continue
        fd  = get_funding(sym, day_idx, hour)
        pc  = float(k1h[-2][4]) if len(k1h) >= 2 else mp
        chg = (mp - pc) / pc if pc > 0 else 0
        try:
            bl = compute_baseline(k1h)
            sr = do_score(k1h, k4h, mp, fd, chg, btc_trend, baseline=bl)
        except:
            continue
        direction = sr['direction']
        total     = sr['total']
        if direction in ('NONE', 'DISCARD') or total < 55:
            continue
        # 趋势方向惩罚
        if state_info['preferred_direction'] == 'LONG'  and direction == 'SHORT': total -= 20
        if state_info['preferred_direction'] == 'SHORT' and direction == 'LONG':  total -= 20
        if total < 55: continue

        results.append({
            'symbol': sym, 'direction': direction, 'score': total,
            'track': sr.get('track', 'oscillation'), 'mark_price': mp,
            'funding': fd, 'chg24': chg, 'k1h': k1h, 'k4h': k4h,
            'baseline': bl, 'score_result': sr,
        })

    results.sort(key=lambda x: x['score'], reverse=True)
    return results[:5]


# ── 挂单 ──────────────────────────────────────────────────────────────────────
def try_open(day_idx: int, hour: int, candidates: list, state: str, state_info: dict):
    gh = global_hour(day_idx, hour)
    for item in candidates:
        sym       = item['symbol']
        direction = item['direction']
        score     = item['score']
        k1h, k4h  = item['k1h'], item['k4h']
        bl        = item['baseline']
        mp        = item['mark_price']
        fd        = item['funding']

        pos_key = f"{sym}_{direction}"
        if pos_key in ACCOUNT['positions']: continue
        if any(o['symbol'] == sym for o in ACCOUNT['orders']): continue

        lc = sum(1 for k in ACCOUNT['positions'] if k.endswith('_LONG'))
        sc = sum(1 for k in ACCOUNT['positions'] if k.endswith('_SHORT'))
        if lc + sc >= 5: continue
        if direction == 'LONG'  and lc >= 3: continue
        if direction == 'SHORT' and sc >= 2: continue

        try:
            sm = do_match(k1h, mp, direction, track=item['track'], bl=bl, symbol=sym)
        except:
            continue
        if not sm['matched'] or sm.get('second_score', 0) < 65: continue

        entry = sm['entry_price']
        sl    = sm['sl_price']
        tp1   = sm['tp_price']
        risk  = abs(entry - sl)
        tp2   = round(entry + risk * 3.0 if direction == 'LONG' else entry - risk * 3.0, 8)

        notional = calc_position_size(item['score_result']['signal_type'], score, btc_trend='neutral')
        notional = int(notional * state_info['size_multiplier'])
        notional = max(250, min(1000, round(notional / 50) * 50))

        sl_pct = abs(entry - sl) / entry if entry > 0 else 0.02
        gate = pre_order_gate(sym, direction, score, fd, notional,
                              equity=ACCOUNT['equity'],
                              sl_distance_pct=sl_pct,
                              market_state=state)
        if not gate['ok']:
            if gate.get('check') == 9:
                sug = gate.get('suggested_notional', notional)
                notional = max(150, round(sug / 50) * 50)
                g2 = pre_order_gate(sym, direction, score, fd, notional,
                                    equity=ACCOUNT['equity'],
                                    sl_distance_pct=sl_pct, market_state=state)
                if not g2['ok']: continue
            else:
                continue

        order = {
            'symbol': sym, 'direction': direction,
            'entry': entry, 'sl': sl, 'tp1': tp1, 'tp2': tp2,
            'notional': notional, 'score': score,
            'scene': sm['scene'], 'rr': sm.get('rr', 0),
            'signal_type': item['score_result']['signal_type'],
            'placed_gh': gh,
            'expire_gh': gh + 4,
            'tp1_triggered': False, 'bars_in_range': 0,
            'conditions': sm.get('conditions', {}),
        }
        ACCOUNT['orders'].append(order)
        log(f"✅ 挂单 {direction} {sym}  {score}分  {sm['scene']}  "
            f"入:{entry:.4f}  SL:{sl:.4f}  TP1:{tp1:.4f}  RR:{sm.get('rr',0):.1f}  {notional}U", 'c')


# ── 成交检查 ──────────────────────────────────────────────────────────────────
def check_fills(day_idx: int, hour: int):
    gh = global_hour(day_idx, hour)
    still = []
    for order in ACCOUNT['orders']:
        sym       = order['symbol']
        entry     = order['entry']
        direction = order['direction']
        if gh > order['expire_gh']:
            log(f"⏰ 过期 {direction} {sym}  入:{entry:.4f}", 'z')
            continue
        hi = get_high_at(sym, day_idx, hour)
        lo = get_low_at(sym, day_idx, hour)
        if hi <= 0:
            still.append(order); continue
        filled = (direction == 'LONG' and lo <= entry) or \
                 (direction == 'SHORT' and hi >= entry)
        if filled:
            ACCOUNT['positions'][f"{sym}_{direction}"] = {
                **order,
                'open_gh': gh, 'open_price': entry, 'status': 'open',
                'tp1_triggered': False, 'remaining_pct': 1.0,
                'bars_in_range': 0, 'trail_activated': False,
                'conditions': order.get('conditions', {}),
            }
            log(f"🎯 成交 {direction} {sym} @{entry:.4f}  SL:{order['sl']:.4f}  "
                f"TP1:{order['tp1']:.4f}  {order['notional']}U", 'g')
        else:
            still.append(order)
    ACCOUNT['orders'] = still


# ── 持仓管理 ──────────────────────────────────────────────────────────────────
def manage(day_idx: int, hour: int):
    gh    = global_hour(day_idx, hour)
    still = {}

    for pk, pos in ACCOUNT['positions'].items():
        sym       = pos['symbol']
        direction = pos['direction']
        sl        = pos['sl']
        tp1       = pos['tp1']
        tp2       = pos['tp2']
        notional  = pos['notional']
        rem       = pos.get('remaining_pct', 1.0)
        tp1t      = pos.get('tp1_triggered', False)

        hi  = get_high_at(sym, day_idx, hour)
        lo  = get_low_at(sym, day_idx, hour)
        cur = get_price_at(sym, day_idx, hour)
        if hi <= 0 or cur <= 0:
            still[pk] = pos; continue

        close_reason = None
        close_price  = 0.0

        pos_risk  = abs(pos['open_price'] - pos['sl'])
        entry_ts  = pos['open_gh']  * 3600000
        cur_ts    = gh              * 3600000

        if direction == 'LONG':
            # 时间止损
            ts_hit, _ = check_time_stop(entry_ts, cur_ts,
                pos.get('scene','A'), pos.get('score', 70),
                pos['open_price'], cur, tp1, direction,
                sl_price=sl, risk=pos_risk)
            if ts_hit and not tp1t:
                close_reason, close_price = 'TIME_SL', cur

            # 移动止损
            if not close_reason and not pos.get('trail_activated') and not tp1t:
                tt = pos.get('conditions', {}).get('trail_trigger_price', 0)
                if tt and cur >= float(tt):
                    pos['sl'] = pos['open_price']
                    pos['trail_activated'] = True
                    log(f"📈 移动止损 {sym} SL→成本 {pos['open_price']:.4f}", 'u')

            # 止损触碰
            if not close_reason and lo <= pos['sl']:
                close_reason, close_price = 'SL', pos['sl']

            # TP1
            elif not close_reason and not tp1t and hi >= tp1:
                pnl = (tp1 - pos['open_price']) / pos['open_price'] * notional * 0.6
                ACCOUNT['trade_log'].append({
                    'symbol': sym, 'direction': direction,
                    'open_price': pos['open_price'], 'close_price': tp1,
                    'pnl': pnl, 'reason': 'TP1_PARTIAL',
                    'open_gh': pos['open_gh'], 'close_gh': gh,
                    'notional': notional * 0.6, 'scene': pos.get('scene', '?'),
                })
                pos['tp1_triggered'] = True
                pos['remaining_pct'] = 0.4
                pos['sl'] = pos['open_price']
                still[pk] = pos
                log(f"✨ TP1 LONG {sym} @{tp1:.4f}  +{pnl:.1f}U  止损→成本", 'g')
                continue

            # TP2
            elif not close_reason and tp1t and hi >= tp2:
                close_reason = 'TP2'
                close_price  = tp2

            # 震荡时间止损（TP1后）
            elif not close_reason and tp1t:
                if tp1 <= cur <= tp2:
                    pos['bars_in_range'] = pos.get('bars_in_range', 0) + 1
                else:
                    pos['bars_in_range'] = 0
                osc = check_tp1_tp2_oscillation(cur, tp1, tp2, direction,
                                                 pos['bars_in_range'])
                if osc['should_close']:
                    close_reason = 'OSC_STOP'
                    close_price  = cur

        else:  # SHORT
            ts_hit, _ = check_time_stop(entry_ts, cur_ts,
                pos.get('scene','A'), pos.get('score', 70),
                pos['open_price'], cur, tp1, direction,
                sl_price=sl, risk=pos_risk)
            if ts_hit and not tp1t:
                close_reason, close_price = 'TIME_SL', cur

            if not close_reason and not pos.get('trail_activated') and not tp1t:
                tt = pos.get('conditions', {}).get('trail_trigger_price', 0)
                if tt and cur <= float(tt):
                    pos['sl'] = pos['open_price']
                    pos['trail_activated'] = True
                    log(f"📉 移动止损 {sym} SL→成本 {pos['open_price']:.4f}", 'u')

            if not close_reason and hi >= pos['sl']:
                close_reason, close_price = 'SL', pos['sl']

            elif not close_reason and not tp1t and lo <= tp1:
                pnl = (pos['open_price'] - tp1) / pos['open_price'] * notional * 0.6
                ACCOUNT['trade_log'].append({
                    'symbol': sym, 'direction': direction,
                    'open_price': pos['open_price'], 'close_price': tp1,
                    'pnl': pnl, 'reason': 'TP1_PARTIAL',
                    'open_gh': pos['open_gh'], 'close_gh': gh,
                    'notional': notional * 0.6, 'scene': pos.get('scene', '?'),
                })
                pos['tp1_triggered'] = True
                pos['remaining_pct'] = 0.4
                pos['sl'] = pos['open_price']
                still[pk] = pos
                log(f"✨ TP1 SHORT {sym} @{tp1:.4f}  +{pnl:.1f}U  止损→成本", 'g')
                continue

            elif not close_reason and tp1t and lo <= tp2:
                close_reason = 'TP2'
                close_price  = tp2

            elif not close_reason and tp1t:
                if tp2 <= cur <= tp1:
                    pos['bars_in_range'] = pos.get('bars_in_range', 0) + 1
                else:
                    pos['bars_in_range'] = 0
                osc = check_tp1_tp2_oscillation(cur, tp1, tp2, direction,
                                                 pos['bars_in_range'])
                if osc['should_close']:
                    close_reason = 'OSC_STOP'
                    close_price  = cur

        if close_reason:
            if rem < 1.0:
                chg = ((close_price - pos['open_price']) / pos['open_price']
                       if direction == 'LONG'
                       else (pos['open_price'] - close_price) / pos['open_price'])
                pnl = chg * notional * rem
            else:
                chg = ((close_price - pos['open_price']) / pos['open_price']
                       if direction == 'LONG'
                       else (pos['open_price'] - close_price) / pos['open_price'])
                pnl = chg * notional

            ACCOUNT['trade_log'].append({
                'symbol': sym, 'direction': direction,
                'open_price': pos['open_price'], 'close_price': close_price,
                'pnl': pnl, 'reason': close_reason,
                'open_gh': pos['open_gh'], 'close_gh': gh,
                'notional': notional * rem, 'scene': pos.get('scene', '?'),
                'score': pos.get('score', 0),
            })
            col = 'g' if pnl > 0 else 'r'
            log(f"{'💰' if pnl>0 else '🔴'} 平仓 {direction} {sym}  "
                f"{close_reason}  @{close_price:.4f}  {pnl:+.1f}U", col)
        else:
            still[pk] = pos

    ACCOUNT['positions'] = still


# ── 主循环 ────────────────────────────────────────────────────────────────────
def run():
    hdr("📅 2026-07-28 ~ 08-03 全周工作流模拟")
    print(f"  账户: {ACCOUNT['equity']}U  标的池: {len(SCAN_SYMBOLS)} 个  持仓跨天延续（不强制收盘平仓）")

    day_names = ['Mon 07-28','Tue 07-29','Wed 07-30','Thu 07-31',
                 'Fri 08-01','Sat 08-02','Sun 08-03']

    for day_idx, (day_key, day_name) in enumerate(zip(DAY_KEYS, day_names)):
        btc_bars = KDATA.get('BTCUSDT',{}).get('1h_by_day',{}).get(day_key,[])
        if not btc_bars:
            print(f"\n  [{day_name}] 无BTC数据，跳过")
            continue
        btc_o = float(btc_bars[0][1]); btc_c = float(btc_bars[-1][4])
        btc_chg = (btc_c - btc_o) / btc_o * 100

        hdr(f"📆 {day_name}  BTC {btc_o:.0f}→{btc_c:.0f}  ({btc_chg:+.2f}%)  "
            f"持仓:{len(ACCOUNT['positions'])}  挂单:{len(ACCOUNT['orders'])}")

        day_pnl_start = sum(t['pnl'] for t in ACCOUNT['trade_log'])

        for hour in range(24):
            gh = global_hour(day_idx, hour)

            # 市场状态
            k1h_btc = get_k1h_until('BTCUSDT', day_idx, hour)
            k4h_btc = get_k4h_recent('BTCUSDT')
            avg_fr  = get_funding('BTCUSDT', day_idx, hour)
            state   = judge_market_state(k1h_btc, k4h_btc, avg_fr)
            si      = get_market_state_info(state)

            btc_cur = get_price_at('BTCUSDT', day_idx, hour)

            # ATR 百分位（用于状态标注）
            atrs = _atr_series(k1h_btc) if k1h_btc else []
            if atrs:
                ca = _mean(atrs[-3:]) if len(atrs)>=3 else atrs[-1]
                aw = atrs[-60:] if len(atrs)>=60 else atrs
                atr_pct = round(sum(1 for a in aw if a<=ca)/len(aw)*100)
            else:
                atr_pct = 50

            state_emoji = {'TRENDING_UP':'🟢','TRENDING_DOWN':'🔴',
                           'RANGING':'🟡','BREAKOUT_PENDING':'🟠',
                           'HIGH_VOLATILITY':'🔥','ACCUMULATION':'💎'}.get(state,'⚪')

            log(f"{hour:02d}:00  BTC:{btc_cur:.0f}  {state_emoji}{state}  "
                f"ATR%:{atr_pct}  开仓:{si['can_open']}  "
                f"持仓:{len(ACCOUNT['positions'])}  挂单:{len(ACCOUNT['orders'])}", 'z')

            # 成交检查（先检查前一轮挂单）
            check_fills(day_idx, hour)

            # 持仓管理
            manage(day_idx, hour)

            # 扫描 & 挂单（HALT 状态跳过）
            if si['can_open']:
                candidates = scan(day_idx, hour, state, si)
                if candidates:
                    try_open(day_idx, hour, candidates, state, si)

        # 当天小结
        day_pnl = sum(t['pnl'] for t in ACCOUNT['trade_log']) - day_pnl_start
        unrealized = 0.0
        for pk, pos in ACCOUNT['positions'].items():
            cp = get_price_at(pos['symbol'], day_idx, 23)
            if cp <= 0: continue
            rem = pos.get('remaining_pct', 1.0)
            if pos['direction'] == 'LONG':
                unrealized += (cp - pos['open_price']) / pos['open_price'] * pos['notional'] * rem
            else:
                unrealized += (pos['open_price'] - cp) / pos['open_price'] * pos['notional'] * rem

        log(f"── 当日已实现 PnL: {day_pnl:+.1f}U  "
            f"持仓浮盈: {unrealized:+.1f}U  "
            f"持仓数: {len(ACCOUNT['positions'])}", 'b')

    # ── 全周汇总 ─────────────────────────────────────────────────────────────
    hdr("📊 全周模拟汇总报告")

    trades = ACCOUNT['trade_log']
    total_pnl = sum(t['pnl'] for t in trades)
    wins   = [t for t in trades if t['pnl'] > 0]
    losses = [t for t in trades if t['pnl'] <= 0]

    # 按原因分类
    by_reason = defaultdict(list)
    for t in trades:
        by_reason[t['reason']].append(t['pnl'])

    # 按标的分类
    by_sym = defaultdict(list)
    for t in trades:
        by_sym[t['symbol']].append(t['pnl'])

    # 按场景分类
    by_scene = defaultdict(list)
    for t in trades:
        if t.get('scene') and t['reason'] not in ('TP1_PARTIAL',):
            by_scene[t.get('scene','?')].append(t['pnl'])

    print(f"\n  BTC 全周: {get_price_at('BTCUSDT',0,0):.0f} → {get_price_at('BTCUSDT',6,23):.0f}")

    print(f"\n  【每日 PnL】")
    running = 0.0
    for day_idx, (day_key, day_name) in enumerate(zip(DAY_KEYS, day_names)):
        day_trades = [t for t in trades
                      if day_idx*24 <= t.get('close_gh', day_idx*24) < (day_idx+1)*24]
        dp = sum(t['pnl'] for t in day_trades)
        running += dp
        bar = '█' * int(abs(dp)/5) if abs(dp) >= 5 else '·'
        sign = '+' if dp >= 0 else ''
        print(f"  {day_name}  {sign}{dp:.1f}U  累计:{running:+.1f}U  {bar}")

    print(f"\n  【平仓原因分布】")
    for reason, pnls in sorted(by_reason.items(), key=lambda x: sum(x[1]), reverse=True):
        total_r = sum(pnls)
        print(f"  {reason:20s}: {len(pnls):3d}笔  合计:{total_r:+7.1f}U  均:{total_r/len(pnls):+5.1f}U")

    print(f"\n  【各标的 PnL】")
    for sym, pnls in sorted(by_sym.items(), key=lambda x: sum(x[1]), reverse=True):
        total_s = sum(pnls)
        print(f"  {sym:20s}: {len(pnls):3d}笔  合计:{total_s:+7.1f}U")

    print(f"\n  【场景 PnL（排除TP1分批）】")
    for scene, pnls in sorted(by_scene.items(), key=lambda x: sum(x[1]), reverse=True):
        total_s = sum(pnls)
        wr = sum(1 for p in pnls if p > 0) / len(pnls) * 100
        print(f"  {scene:20s}: {len(pnls):3d}笔  合计:{total_s:+7.1f}U  胜率:{wr:.0f}%")

    print(f"\n  ┌{'─'*45}┐")
    print(f"  │  总笔数:    {len(trades):>4} 笔  (含TP1分批)            │")
    print(f"  │  盈利笔数:  {len(wins):>4}     亏损笔数: {len(losses):>4}            │")
    wr = len(wins)/len(trades)*100 if trades else 0
    print(f"  │  胜率:      {wr:>5.1f}%                              │")
    print(f"  │  总 PnL:  {total_pnl:>+8.1f} U                         │")
    print(f"  │  账户:   {ACCOUNT['equity']:>7.0f} → {ACCOUNT['equity']+total_pnl:>7.0f} U              │")
    print(f"  └{'─'*45}┘")

    print(f"\n  【完整交易明细】")
    print(f"  {'天':>4} {'时':>3} {'方向':>6} {'标的':>15} {'场景':>12} {'原因':>12} {'入场':>9} {'出场':>9} {'PnL':>8}")
    print(f"  {'─'*90}")
    for t in sorted(trades, key=lambda x: x.get('close_gh', 0)):
        gh   = t.get('close_gh', 0)
        d_idx = gh // 24; h = gh % 24
        day_label = day_names[d_idx] if d_idx < len(day_names) else f"D{d_idx}"
        pnl_str = f"{t['pnl']:+.1f}U"
        print(f"  {day_label[-5:]:>5} {h:02d}  "
              f"{t['direction']:>6}  {t['symbol']:>15}  "
              f"{t.get('scene','?'):>12}  {t['reason']:>12}  "
              f"{t['open_price']:>9.4f}  {t['close_price']:>9.4f}  {pnl_str:>8}")


if __name__ == '__main__':
    import logging
    logging.basicConfig(level=logging.WARNING)
    run()
