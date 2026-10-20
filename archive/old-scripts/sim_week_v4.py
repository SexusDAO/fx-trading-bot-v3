"""
sim_week_v4.py — quick_scorer 重构版全周模拟

改动说明：
  - scorer.py 5维度评分 → quick_scorer.quick_score()（旧逻辑已注释）
  - scene_matcher.py A/B/C/E场景 → quick_scorer.calc_entry_simple()（旧逻辑已注释）
  - 评分门槛：55分（旧版65分）
  - 过滤层：3层（方向×市场状态 / 技术结构 / RR≥1.2）
  - 持仓管理保持不变（time_stop / position_oscillation_check）
"""

import sys, os, json, time as _time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'v3'))

from collections import defaultdict

print("Loading data...", flush=True)
t0 = _time.time()
with open('/tmp/sim_week_data.json') as f:
    RAW = json.load(f)

KDATA    = RAW['kdata']
FDATA    = RAW['fdata']
DAY_KEYS = RAW['days']

SCAN_SYMBOLS = [s for s in KDATA if all(KDATA[s]['1h_by_day'].get(dk) for dk in DAY_KEYS)]
print(f"  完整7天标的: {len(SCAN_SYMBOLS)} 个  {_time.time()-t0:.1f}s", flush=True)

DAY_START_MS = {
    '0728': 1753660800000, '0729': 1753747200000,
    '0730': 1753833600000, '0731': 1753920000000,
    '0801': 1754006400000, '0802': 1754092800000,
    '0803': 1754179200000,
}

# ── 新模块（替代旧 scorer + scene_matcher）────────────────────────────────────
from modules.quick_scorer import quick_score, calc_entry_simple

# ── 旧模块（仍然使用的部分）──────────────────────────────────────────────────
from modules.dynamic_baseline      import compute as compute_baseline
from modules.dynamic_baseline      import _closes, _ema, _atr_series, _mean
from modules.market_bias           import judge_market_state, get_market_state_info
from modules.pre_order_gate        import pre_order_gate
from modules.position_oscillation_check import check_tp1_tp2_oscillation
from modules.time_stop             import check_time_stop

print(f"  模块加载完  {_time.time()-t0:.1f}s", flush=True)

# ── 预处理 ────────────────────────────────────────────────────────────────────
def build_full_1h(sym: str) -> list:
    bars_4h = KDATA[sym].get('4h', [])
    bg_1h = []
    cut_ms = DAY_START_MS['0728']
    for bar in bars_4h:
        ts = int(bar[0])
        if ts >= cut_ms:
            break
        o, h, l, c, v = float(bar[1]), float(bar[2]), float(bar[3]), float(bar[4]), float(bar[5])
        for i in range(4):
            sub_o = o + (c - o) * i / 4
            sub_c = o + (c - o) * (i + 1) / 4
            bg_1h.append([ts + i*3600000,
                           str(sub_o), str(h), str(l), str(sub_c), str(v/4),
                           0, '0', 0, '0', '0', '0'])
    week_1h = []
    for dk in DAY_KEYS:
        bars = KDATA[sym]['1h_by_day'].get(dk, [])
        if isinstance(bars, list):
            week_1h.extend([[b[0],b[1],b[2],b[3],b[4],b[5],0,'0',0,'0','0','0'] for b in bars])
    combined = bg_1h + week_1h
    combined.sort(key=lambda x: x[0])
    return combined

print("Preprocessing 1H series...", flush=True)
FULL_1H = {sym: build_full_1h(sym) for sym in SCAN_SYMBOLS}
print(f"  预处理完  {_time.time()-t0:.1f}s", flush=True)

TOTAL_HOURS = 7 * 24

def gh_to_ms(gh: int) -> int:
    return DAY_START_MS[DAY_KEYS[gh // 24]] + (gh % 24) * 3600000

def price_at(sym: str, gh: int) -> float:
    bars = KDATA[sym]['1h_by_day'].get(DAY_KEYS[gh // 24], [])
    h = gh % 24
    return float(bars[h][4]) if isinstance(bars, list) and h < len(bars) else 0.0

def high_at(sym: str, gh: int) -> float:
    bars = KDATA[sym]['1h_by_day'].get(DAY_KEYS[gh // 24], [])
    h = gh % 24
    return float(bars[h][2]) if isinstance(bars, list) and h < len(bars) else 0.0

def low_at(sym: str, gh: int) -> float:
    bars = KDATA[sym]['1h_by_day'].get(DAY_KEYS[gh // 24], [])
    h = gh % 24
    return float(bars[h][3]) if isinstance(bars, list) and h < len(bars) else 0.0

def funding_at(sym: str, gh: int) -> float:
    ms = gh_to_ms(gh)
    rates = FDATA.get(sym, [])
    if not isinstance(rates, list) or not rates: return 0.0001
    best = None
    for r in rates:
        if isinstance(r, dict) and int(r.get('fundingTime', 0)) <= ms:
            best = r
    return float(best['fundingRate']) if best else 0.0001

def k1h_until(sym: str, gh: int) -> list:
    target_ms = gh_to_ms(gh) + 3600000
    bars = FULL_1H.get(sym, [])
    lo, hi = 0, len(bars)
    while lo < hi:
        mid = (lo + hi) // 2
        if int(bars[mid][0]) < target_ms: lo = mid + 1
        else: hi = mid
    result = bars[:lo]
    return result[-80:] if len(result) > 80 else result

# ── 预算 BTC 市场状态 ─────────────────────────────────────────────────────────
print("Precomputing BTC market states...", flush=True)
BTC_STATE = {}
for gh in range(TOTAL_HOURS):
    k1h = k1h_until('BTCUSDT', gh)
    k4h = KDATA['BTCUSDT'].get('4h', [])
    fr  = funding_at('BTCUSDT', gh)
    try:
        state = judge_market_state(k1h, k4h, fr)
        from modules.dynamic_baseline import _atr_series as _atrs, _mean as _mn
        _a = _atrs(k1h)
        _ca = _mn(_a[-3:]) if len(_a) >= 3 else (_a[-1] if _a else 0)
        _w = _a[-60:] if len(_a) >= 60 else _a
        _ar = sum(1 for a in _w if a <= _ca) / len(_w) * 100 if _w else 50
        si = get_market_state_info(state, atr_rank=_ar)
    except:
        state = 'RANGING'; si = get_market_state_info(state)
    BTC_STATE[gh] = (state, si)
print(f"  BTC状态预算完  {_time.time()-t0:.1f}s", flush=True)

# ── 账户 ──────────────────────────────────────────────────────────────────────
ACCOUNT = {'equity': 3000.0, 'positions': {}, 'orders': [], 'trade_log': []}

SCAN_HOURS = {0, 4, 8, 12, 16, 20}
SCORE_THRESHOLD = 55   # 门槛（旧版65，新版55）
MIN_RR = 1.2

# ── 扫描 ──────────────────────────────────────────────────────────────────────
def scan(gh: int) -> list:
    state, si = BTC_STATE[gh]
    if not si.get('can_open', True): return []

    results = []
    for sym in SCAN_SYMBOLS:
        already_long  = f"{sym}_LONG"  in ACCOUNT['positions']
        already_short = f"{sym}_SHORT" in ACCOUNT['positions']
        has_order = any(o['symbol'] == sym for o in ACCOUNT['orders'])
        if (already_long and already_short) or has_order: continue

        k1h = k1h_until(sym, gh)
        k4h = KDATA[sym].get('4h', [])[-10:]
        if len(k1h) < 20: continue
        mp  = float(k1h[-1][4])
        if mp <= 0: continue
        fd  = funding_at(sym, gh)

        try:
            bl = compute_baseline(k1h)
            # ── 新评分（替代旧 scorer.score()）──────────────────────────────
            score, direction, signal_type = quick_score(
                k1h, k4h, mp, fd, bl, market_state=state)
        except:
            continue

        if direction in ('NONE',) or score < SCORE_THRESHOLD: continue
        if already_long  and direction == 'LONG':  continue
        if already_short and direction == 'SHORT': continue

        # ── 新入场计算（替代旧 scene_matcher.match()）───────────────────────
        try:
            entry, sl, tp, source, rr = calc_entry_simple(direction, mp, k1h, bl)
        except:
            continue

        if rr < MIN_RR: continue

        results.append({
            'symbol': sym, 'direction': direction,
            'score': score, 'signal_type': signal_type,
            'mark_price': mp, 'funding': fd,
            'entry': entry, 'sl': sl, 'tp1': tp,
            'tp2': round(entry + abs(entry-sl)*2.5 if direction=='LONG'
                         else entry - abs(entry-sl)*2.5, 8),
            'rr': rr, 'source': source,
            'k1h': k1h, 'bl': bl,
        })

    results.sort(key=lambda x: x['score'], reverse=True)
    return results[:5]


def try_open(gh: int, candidates: list):
    state, si = BTC_STATE[gh]
    for item in candidates:
        sym, direction = item['symbol'], item['direction']
        pos_key = f"{sym}_{direction}"
        if pos_key in ACCOUNT['positions']: continue
        if any(o['symbol']==sym and o['direction']==direction for o in ACCOUNT['orders']): continue
        total_pos = len(ACCOUNT['positions'])
        if total_pos >= 5: continue
        lc = sum(1 for k in ACCOUNT['positions'] if k.endswith('_LONG'))
        sc = sum(1 for k in ACCOUNT['positions'] if k.endswith('_SHORT'))
        if direction == 'LONG'  and lc >= 3: continue
        if direction == 'SHORT' and sc >= 2: continue

        entry, sl, tp1 = item['entry'], item['sl'], item['tp1']
        risk = abs(entry - sl)
        if risk <= 0: continue
        tp2 = item['tp2']

        # 仓位大小：按分数线性映射 250~700U
        notional = int(250 + (item['score'] - SCORE_THRESHOLD) / (100 - SCORE_THRESHOLD) * 450)
        notional = max(200, min(700, round(notional / 50) * 50))
        notional = int(notional * si.get('size_multiplier', 1.0))
        notional = max(200, min(700, round(notional / 50) * 50))

        sl_pct = risk / entry if entry > 0 else 0.02
        gate = pre_order_gate(sym, direction, item['score'], item['funding'], notional,
                              equity=ACCOUNT['equity'], sl_distance_pct=sl_pct, market_state=state)
        if not gate['ok']:
            if gate.get('check') == 9:
                notional = max(150, round(gate.get('suggested_notional', notional) / 50) * 50)
                g2 = pre_order_gate(sym, direction, item['score'], item['funding'], notional,
                                    equity=ACCOUNT['equity'], sl_distance_pct=sl_pct, market_state=state)
                if not g2['ok']: continue
            else:
                continue

        order = {
            'symbol': sym, 'direction': direction,
            'entry': entry, 'sl': sl, 'tp1': tp1, 'tp2': tp2,
            'notional': notional, 'score': item['score'],
            'scene': item['source'],   # 用 source 替代旧 scene 字段
            'rr': item['rr'],
            'signal_type': item['signal_type'],
            'placed_gh': gh, 'expire_gh': gh + 4,
        }
        ACCOUNT['orders'].append(order)
        day_idx, hour = gh // 24, gh % 24
        print(f"      ✅ 挂单 {direction:5s} {sym:15s} {item['score']}分 [{item['source']:12s}] "
              f"入:{entry:.4f} SL:{sl:.4f} TP1:{tp1:.4f} RR:{item['rr']:.1f} {notional}U "
              f"[D{day_idx+1} {hour:02d}:00]", flush=True)


def check_fills(gh: int):
    still = []
    for order in ACCOUNT['orders']:
        if gh > order['expire_gh']:
            d, h = gh//24, gh%24
            print(f"      ⏰ 过期 {order['direction']:5s} {order['symbol']:15s} "
                  f"入:{order['entry']:.4f} [D{d+1} {h:02d}:00]", flush=True)
            continue
        hi = high_at(order['symbol'], gh)
        lo = low_at(order['symbol'], gh)
        if hi <= 0: still.append(order); continue
        d = order['direction']
        filled = (d == 'LONG' and lo <= order['entry']) or \
                 (d == 'SHORT' and hi >= order['entry'])
        if filled:
            ACCOUNT['positions'][f"{order['symbol']}_{d}"] = {
                **order, 'open_gh': gh, 'open_price': order['entry'],
                'tp1_triggered': False, 'remaining_pct': 1.0,
                'bars_in_range': 0, 'trail_activated': False,
            }
            day_idx, hour = gh//24, gh%24
            print(f"      🎯 成交 {d:5s} {order['symbol']:15s} @{order['entry']:.4f} "
                  f"SL:{order['sl']:.4f} TP1:{order['tp1']:.4f} {order['notional']}U "
                  f"[D{day_idx+1} {hour:02d}:00]", flush=True)
        else:
            still.append(order)
    ACCOUNT['orders'] = still


def manage_positions(gh: int):
    still = {}
    for pk, pos in ACCOUNT['positions'].items():
        sym, direction = pos['symbol'], pos['direction']
        sl, tp1, tp2   = pos['sl'], pos['tp1'], pos['tp2']
        notional       = pos['notional']
        rem            = pos.get('remaining_pct', 1.0)
        tp1t           = pos.get('tp1_triggered', False)

        hi  = high_at(sym, gh)
        lo  = low_at(sym, gh)
        cur = price_at(sym, gh)
        if hi <= 0 or cur <= 0: still[pk] = pos; continue

        close_reason = None; close_price = 0.0
        pos_risk     = abs(pos['open_price'] - pos['sl'])
        entry_ms     = gh_to_ms(pos['open_gh'])
        cur_ms       = gh_to_ms(gh)

        if direction == 'LONG':
            try:
                ts_hit, _ = check_time_stop(entry_ms, cur_ms,
                    pos.get('scene','A'), pos.get('score',60),
                    pos['open_price'], cur, tp1, direction,
                    sl_price=sl, risk=pos_risk)
            except: ts_hit = False
            if ts_hit and not tp1t:
                close_reason, close_price = 'TIME_SL', cur

            if not close_reason and lo <= pos['sl']:
                close_reason, close_price = 'SL', pos['sl']
            elif not close_reason and not tp1t and hi >= tp1:
                pnl = (tp1 - pos['open_price']) / pos['open_price'] * notional * 0.6
                ACCOUNT['trade_log'].append({
                    'symbol': sym, 'direction': direction,
                    'open_price': pos['open_price'], 'close_price': tp1,
                    'pnl': pnl, 'reason': 'TP1_PARTIAL',
                    'open_gh': pos['open_gh'], 'close_gh': gh,
                    'notional': notional*0.6, 'scene': pos.get('scene','?'),
                    'score': pos.get('score', 0),
                })
                pos['tp1_triggered'] = True
                pos['remaining_pct'] = 0.4
                pos['sl'] = pos['open_price']
                still[pk] = pos
                d, h = gh//24, gh%24
                print(f"      ✨ TP1   LONG  {sym:15s} @{tp1:.4f} +{pnl:.1f}U [D{d+1} {h:02d}:00]", flush=True)
                continue
            elif not close_reason and tp1t and hi >= tp2:
                close_reason, close_price = 'TP2', tp2
            elif not close_reason and tp1t:
                if tp1 <= cur <= tp2: pos['bars_in_range'] = pos.get('bars_in_range',0)+1
                else: pos['bars_in_range'] = 0
                try:
                    osc = check_tp1_tp2_oscillation(cur, tp1, tp2, direction, pos['bars_in_range'])
                    if osc.get('should_close'): close_reason, close_price = 'OSC_STOP', cur
                except: pass

        else:  # SHORT
            try:
                ts_hit, _ = check_time_stop(entry_ms, cur_ms,
                    pos.get('scene','E'), pos.get('score',60),
                    pos['open_price'], cur, tp1, direction,
                    sl_price=sl, risk=pos_risk)
            except: ts_hit = False
            if ts_hit and not tp1t:
                close_reason, close_price = 'TIME_SL', cur

            if not close_reason and hi >= pos['sl']:
                close_reason, close_price = 'SL', pos['sl']
            elif not close_reason and not tp1t and lo <= tp1:
                pnl = (pos['open_price'] - tp1) / pos['open_price'] * notional * 0.6
                ACCOUNT['trade_log'].append({
                    'symbol': sym, 'direction': direction,
                    'open_price': pos['open_price'], 'close_price': tp1,
                    'pnl': pnl, 'reason': 'TP1_PARTIAL',
                    'open_gh': pos['open_gh'], 'close_gh': gh,
                    'notional': notional*0.6, 'scene': pos.get('scene','?'),
                    'score': pos.get('score', 0),
                })
                pos['tp1_triggered'] = True
                pos['remaining_pct'] = 0.4
                pos['sl'] = pos['open_price']
                still[pk] = pos
                d, h = gh//24, gh%24
                print(f"      ✨ TP1   SHORT {sym:15s} @{tp1:.4f} +{pnl:.1f}U [D{d+1} {h:02d}:00]", flush=True)
                continue
            elif not close_reason and tp1t and lo <= tp2:
                close_reason, close_price = 'TP2', tp2
            elif not close_reason and tp1t:
                if tp2 <= cur <= tp1: pos['bars_in_range'] = pos.get('bars_in_range',0)+1
                else: pos['bars_in_range'] = 0
                try:
                    osc = check_tp1_tp2_oscillation(cur, tp1, tp2, direction, pos['bars_in_range'])
                    if osc.get('should_close'): close_reason, close_price = 'OSC_STOP', cur
                except: pass

        if close_reason:
            chg = ((close_price - pos['open_price']) / pos['open_price']
                   if direction == 'LONG'
                   else (pos['open_price'] - close_price) / pos['open_price'])
            pnl = chg * notional * rem
            ACCOUNT['trade_log'].append({
                'symbol': sym, 'direction': direction,
                'open_price': pos['open_price'], 'close_price': close_price,
                'pnl': pnl, 'reason': close_reason,
                'open_gh': pos['open_gh'], 'close_gh': gh,
                'notional': notional*rem, 'scene': pos.get('scene','?'),
                'score': pos.get('score', 0),
            })
            d, h = gh//24, gh%24
            emoji = '💰' if pnl > 0 else '🔴'
            print(f"      {emoji} 平仓 {direction:5s} {sym:15s} {close_reason:10s} "
                  f"@{close_price:.4f} {pnl:+.1f}U [D{d+1} {h:02d}:00]", flush=True)
        else:
            still[pk] = pos
    ACCOUNT['positions'] = still


# ── 主循环 ────────────────────────────────────────────────────────────────────
DAY_NAMES = ['Mon 07-28','Tue 07-29','Wed 07-30','Thu 07-31',
             'Fri 08-01','Sat 08-02','Sun 08-03']

print(f"\n{'='*72}", flush=True)
print(f"  📅 全周模拟 v4 (quick_scorer)  2026-07-28 ~ 2026-08-03", flush=True)
print(f"  账户:{ACCOUNT['equity']}U  标的:{len(SCAN_SYMBOLS)}个  门槛:{SCORE_THRESHOLD}分  "
      f"RR≥{MIN_RR}  扫描:每天6次", flush=True)
print(f"  评分模块: quick_scorer（旧scorer/scene_matcher已注释）", flush=True)
print(f"{'='*72}", flush=True)

day_pnl_prev = 0.0

for gh in range(TOTAL_HOURS):
    day_idx, hour = gh // 24, gh % 24

    if hour == 0:
        print(f"\n── {DAY_NAMES[day_idx]}  持仓:{len(ACCOUNT['positions'])}  挂单:{len(ACCOUNT['orders'])}", flush=True)
        btc_o = price_at('BTCUSDT', gh)
        btc_c = price_at('BTCUSDT', day_idx*24 + 23)
        btc_chg = (btc_c - btc_o) / btc_o * 100 if btc_o > 0 else 0
        print(f"   BTC: {btc_o:.0f} → {btc_c:.0f}  ({btc_chg:+.2f}%)", flush=True)

    check_fills(gh)
    manage_positions(gh)

    if hour in SCAN_HOURS:
        state, si = BTC_STATE[gh]
        emoji = {'TRENDING_UP':'🟢','TRENDING_DOWN':'🔴','RANGING':'🟡',
                 'BREAKOUT_PENDING':'🟠','HIGH_VOLATILITY':'🔥','ACCUMULATION':'💎'}.get(state,'⚪')
        print(f"   [{hour:02d}:00] {emoji}{state:20s} 持仓:{len(ACCOUNT['positions'])} "
              f"挂单:{len(ACCOUNT['orders'])}", flush=True)
        if si.get('can_open', True):
            t_scan = _time.time()
            candidates = scan(gh)
            if candidates:
                try_open(gh, candidates)
            print(f"         扫描完 {_time.time()-t_scan:.1f}s  候选:{len(candidates)}", flush=True)

    if hour == 23:
        cur_pnl = sum(t['pnl'] for t in ACCOUNT['trade_log'])
        day_pnl = cur_pnl - day_pnl_prev
        day_pnl_prev = cur_pnl
        unrealized = 0.0
        for pk, pos in ACCOUNT['positions'].items():
            cp = price_at(pos['symbol'], gh)
            rem = pos.get('remaining_pct', 1.0)
            if cp > 0:
                chg = ((cp-pos['open_price'])/pos['open_price'] if pos['direction']=='LONG'
                       else (pos['open_price']-cp)/pos['open_price'])
                unrealized += chg * pos['notional'] * rem
        print(f"   [23:00] 当日已实现:{day_pnl:+.1f}U  浮盈:{unrealized:+.1f}U  "
              f"累计:{cur_pnl:+.1f}U", flush=True)

# ── 汇总 ──────────────────────────────────────────────────────────────────────
print(f"\n{'='*72}", flush=True)
print(f"  📊 全周汇总 (v4 quick_scorer)", flush=True)
print(f"{'='*72}", flush=True)

trades   = ACCOUNT['trade_log']
total_pnl = sum(t['pnl'] for t in trades)
wins     = [t for t in trades if t['pnl'] > 0]
losses   = [t for t in trades if t['pnl'] <= 0]
wr = len(wins)/len(trades)*100 if trades else 0

by_reason = defaultdict(list)
by_sym    = defaultdict(list)
by_source = defaultdict(list)
for t in trades:
    by_reason[t['reason']].append(t['pnl'])
    by_sym[t['symbol']].append(t['pnl'])
    if t['reason'] not in ('TP1_PARTIAL',):
        by_source[t.get('scene','?')].append(t['pnl'])

print(f"\n  【每日 PnL】")
running = 0.0
for day_idx in range(7):
    day_trades = [t for t in trades
                  if day_idx*24 <= t.get('close_gh', day_idx*24) < (day_idx+1)*24]
    dp = sum(t['pnl'] for t in day_trades)
    running += dp
    bar = ('█' * int(abs(dp)/5)) if abs(dp)>=5 else '·'
    print(f"  {DAY_NAMES[day_idx]}  {dp:+7.1f}U  累计:{running:+7.1f}U  {bar}")

print(f"\n  【平仓原因分布】")
for reason, pnls in sorted(by_reason.items(), key=lambda x: sum(x[1]), reverse=True):
    tr = sum(pnls)
    print(f"  {reason:20s}: {len(pnls):3d}笔  合计:{tr:+7.1f}U  均:{tr/len(pnls):+5.1f}U")

print(f"\n  【标的 PnL Top10】")
for sym, pnls in sorted(by_sym.items(), key=lambda x: sum(x[1]), reverse=True)[:10]:
    print(f"  {sym:20s}: {len(pnls):3d}笔  合计:{sum(pnls):+7.1f}U")

print(f"\n  【入场来源分布（排除TP1分批）】")
for src, pnls in sorted(by_source.items(), key=lambda x: sum(x[1]), reverse=True):
    wr_s = sum(1 for p in pnls if p>0)/len(pnls)*100
    print(f"  {src:20s}: {len(pnls):3d}笔  合计:{sum(pnls):+7.1f}U  胜率:{wr_s:.0f}%")

print(f"\n  ╔{'═'*50}╗")
print(f"  ║  评分模块:   quick_scorer v1.0 (旧scorer已注释)  ║")
print(f"  ║  总笔数:    {len(trades):>4} 笔  (含TP1分批)               ║")
print(f"  ║  盈利: {len(wins):>4}  亏损: {len(losses):>4}  胜率: {wr:>5.1f}%           ║")
print(f"  ║  总 PnL:  {total_pnl:>+8.1f} U                            ║")
print(f"  ║  账户:   {ACCOUNT['equity']:.0f} → {ACCOUNT['equity']+total_pnl:.0f} U                    ║")
print(f"  ╚{'═'*50}╝")

print(f"\n  【完整交易明细】")
print(f"  {'日':>5} {'时':>3}  {'方向':>5} {'标的':>15} {'来源':>12} {'原因':>12} {'入场':>9} {'出场':>9} {'PnL':>8} {'分':>4}")
print(f"  {'─'*98}")
for t in sorted(trades, key=lambda x: x.get('close_gh',0)):
    gh = t.get('close_gh', 0)
    d = gh//24; h = gh%24
    print(f"  {DAY_NAMES[d][-5:]:>5} {h:02d}  "
          f"{t['direction']:>5} {t['symbol']:>15} {t.get('scene','?'):>12} "
          f"{t['reason']:>12} {t['open_price']:>9.4f} {t['close_price']:>9.4f} "
          f"{t['pnl']:>+8.1f}U {t.get('score',0):>4}")

print(f"\n  总耗时: {_time.time()-t0:.1f}s", flush=True)
