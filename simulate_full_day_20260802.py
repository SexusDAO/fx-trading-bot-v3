"""
simulate_full_day_20260802.py — 2026-08-02 全天完整工作流模拟

用真实K线数据重现整天交易系统的工作流：
  每小时触发一次扫描 (00:00~23:00 UTC, 共24轮)
  - 市场状态判断（v2.0 6种状态）
  - 候选标的评分
  - 场景匹配
  - pre_order_gate 检查
  - 虚拟挂单与成交模拟
  - 持仓健康监控（每30min）
  - TP1/TP2 震荡时间止损
  - 平仓

输出: 完整决策日志 + PnL 汇总
"""

import sys, os, json, random, math
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'v3'))

from datetime import datetime, timezone, timedelta
from modules.dynamic_baseline import compute as compute_baseline, _ema, _closes, _atr_series, _mean
from modules.market_bias      import judge_market_state, get_market_state_info, MARKET_STATE_HALT
from modules.scorer           import score as do_score
from modules.scene_matcher    import match as do_match
from modules.pre_order_gate   import pre_order_gate
from modules.position_size    import calc_position_size
from modules.position_oscillation_check import check_tp1_tp2_oscillation

random.seed(2026_08_02)

# ── 加载昨天真实数据 ──────────────────────────────────────────────────────────
with open('/tmp/sim_kdata.json') as f:
    RAW = json.load(f)

KDATA = RAW['kdata']
FDATA = RAW['fdata']

# 模拟账户
ACCOUNT = {
    'equity':    3000.0,
    'positions': {},    # sym_dir -> pos dict
    'orders':    [],    # pending limit orders
    'trade_log': [],
}

# 颜色输出
def C(color, s):
    colors = {'green':'\033[92m','red':'\033[91m','yellow':'\033[93m',
               'cyan':'\033[96m','bold':'\033[1m','reset':'\033[0m',
               'blue':'\033[94m','gray':'\033[90m'}
    return f"{colors.get(color,'')}{s}{colors['reset']}"

def hdr(title):
    print(f"\n{C('bold','─'*64)}")
    print(f"{C('bold', title)}")
    print(C('bold','─'*64))

def log(msg, level='info'):
    colors = {'info':'gray','ok':'green','warn':'yellow','err':'red','signal':'cyan','trade':'bold'}
    print(f"  {C(colors.get(level,'gray'), msg)}")


# ── 数据工具 ─────────────────────────────────────────────────────────────────

def get_k1h_until(sym: str, hour_idx: int) -> list:
    """返回截至当前小时的 1h K线（用最近 60 根做基准，避免只有24根）"""
    all_k = KDATA.get(sym, {}).get('1h', [])
    # 用历史+当天数据：4h bars 前13根作为历史背景（约 52 小时）
    hist_4h = KDATA.get(sym, {}).get('4h', [])
    # 把 4h 转成等效 1h
    hist_1h = []
    for bar in hist_4h[:13]:
        o,h,l,c,v = float(bar[1]),float(bar[2]),float(bar[3]),float(bar[4]),float(bar[5])
        ts = bar[0]
        for i in range(4):
            sub_c = o + (c-o)*(i+1)/4
            hist_1h.append([ts+i*3600000, str(o if i==0 else o+(c-o)*i/4),
                            str(h if i in (1,2) else sub_c),
                            str(l if i in (1,2) else sub_c*0.999),
                            str(sub_c), str(v/4), 0,'0',0,'0','0','0'])
    combined = hist_1h + all_k[:hour_idx+1]
    return combined[-80:] if len(combined) >= 80 else combined

def get_k4h_recent(sym: str) -> list:
    return KDATA.get(sym, {}).get('4h', [])[-10:]

def get_price_at_hour(sym: str, hour_idx: int) -> float:
    k1h = KDATA.get(sym, {}).get('1h', [])
    if hour_idx < len(k1h):
        return float(k1h[hour_idx][4])  # 收盘价
    return 0.0

def get_funding_at_hour(sym: str, hour_idx: int) -> float:
    """返回当前小时最近的资金费率"""
    rates = FDATA.get(sym, [])
    if not rates: return 0.0001
    # 找到在 hour_idx 之前的最近资金费率
    day_start_ms = int(datetime(2026,8,2,0,0,0,tzinfo=timezone.utc).timestamp() * 1000)
    target_ms = day_start_ms + hour_idx * 3600000
    best = rates[0]
    for r in rates:
        if r.get('fundingTime', 0) <= target_ms:
            best = r
    return float(best.get('fundingRate', 0.0001))

def get_high_at_hour(sym: str, hour_idx: int) -> float:
    k1h = KDATA.get(sym, {}).get('1h', [])
    if hour_idx < len(k1h): return float(k1h[hour_idx][2])
    return 0.0

def get_low_at_hour(sym: str, hour_idx: int) -> float:
    k1h = KDATA.get(sym, {}).get('1h', [])
    if hour_idx < len(k1h): return float(k1h[hour_idx][3])
    return 0.0


# ── 市场状态判断 ─────────────────────────────────────────────────────────────

def judge_state_at_hour(hour_idx: int) -> tuple[str, dict]:
    """用 BTC 数据判断当前市场状态"""
    k1h_btc = get_k1h_until('BTCUSDT', hour_idx)
    k4h_btc = get_k4h_recent('BTCUSDT')
    avg_funding = get_funding_at_hour('BTCUSDT', hour_idx)
    state = judge_market_state(k1h_btc, k4h_btc, avg_funding)
    info  = get_market_state_info(state)
    return state, info


# ── 候选标的评分 ──────────────────────────────────────────────────────────────

SCAN_SYMBOLS = ['BTCUSDT','ETHUSDT','SOLUSDT','KOMAUSDT','SNDKUSDT','SKYAIUSDT',
                'BICOUSDT','BTWUSDT','1000RATSUSDT','BEATUSDT','BANKUSDT',
                'SOXLUSDT','MUUSDT']

def scan_at_hour(hour_idx: int, market_state: str, state_info: dict) -> list:
    """对候选标的评分，返回达到开仓门槛的结果列表"""
    results = []
    btc_k1h = get_k1h_until('BTCUSDT', hour_idx)
    btc_closes = _closes(btc_k1h)
    ema20 = _ema(btc_closes[-20:], 20) if len(btc_closes) >= 20 else btc_closes[-1]
    ema50 = _ema(btc_closes[-50:], 50) if len(btc_closes) >= 50 else ema20
    btc_price = float(btc_k1h[-1][4]) if btc_k1h else 63000
    btc_trend = 'up' if (ema20 > ema50 and btc_price > ema20) else (
                'down' if (ema20 < ema50 and btc_price < ema20) else 'neutral')

    for sym in SCAN_SYMBOLS:
        k1h = get_k1h_until(sym, hour_idx)
        k4h = get_k4h_recent(sym)
        if len(k1h) < 20:
            continue

        mark_price = float(k1h[-1][4])
        if mark_price <= 0: continue

        funding = get_funding_at_hour(sym, hour_idx)
        prev_k  = k1h[:-2] if len(k1h) >= 2 else k1h
        prev_close = float(prev_k[-1][4]) if prev_k else mark_price
        chg24 = (mark_price - prev_close) / prev_close if prev_close > 0 else 0

        try:
            bl = compute_baseline(k1h)
            sr = do_score(k1h, k4h, mark_price, funding, chg24, btc_trend, baseline=bl)
        except Exception as e:
            continue

        direction = sr['direction']
        total = sr['total']

        if direction in ('NONE', 'DISCARD') or total < 55:
            continue

        # 市场状态方向过滤
        if state_info['preferred_direction'] == 'LONG' and direction == 'SHORT':
            total -= 20   # 逆势惩罚
        elif state_info['preferred_direction'] == 'SHORT' and direction == 'LONG':
            total -= 20

        if total < 55: continue

        results.append({
            'symbol':      sym,
            'direction':   direction,
            'signal_type': sr['signal_type'],
            'score':       total,
            'score_result': sr,
            'track':       sr.get('track', 'oscillation'),   # 修复：从 scorer 取正确 track
            'mark_price':  mark_price,
            'funding':     funding,
            'chg24':       chg24,
            'k1h':         k1h,
            'k4h':         k4h,
            'baseline':    bl,
        })

    results.sort(key=lambda x: x['score'], reverse=True)
    return results[:5]  # Top5


# ── 挂单模拟 ──────────────────────────────────────────────────────────────────

def try_open_orders(hour_idx: int, candidates: list, market_state: str, state_info: dict):
    """对 Top 候选运行场景匹配 + gate + 挂单"""
    opened = []

    for item in candidates:
        sym = item['symbol']
        direction = item['direction']
        score = item['score']
        k1h = item['k1h']
        bl  = item['baseline']
        mark_price = item['mark_price']
        funding = item['funding']
        signal_type = item['signal_type']

        # 已有持仓/挂单则跳过
        pos_key = f"{sym}_{direction}"
        if pos_key in ACCOUNT['positions']:
            continue
        if any(o['symbol'] == sym for o in ACCOUNT['orders']):
            continue

        # 持仓上限检查
        long_cnt  = sum(1 for k in ACCOUNT['positions'] if k.endswith('_LONG'))
        short_cnt = sum(1 for k in ACCOUNT['positions'] if k.endswith('_SHORT'))
        total_cnt = long_cnt + short_cnt
        if total_cnt >= 5: continue
        if direction == 'LONG' and long_cnt >= 3: continue
        if direction == 'SHORT' and short_cnt >= 2: continue

        # 场景匹配
        try:
            track = item.get('track', 'oscillation')
            sm = do_match(k1h, mark_price, direction, track=track, bl=bl, symbol=sym)
        except Exception as e:
            continue

        if not sm['matched'] or sm.get('second_score', 0) < 65:
            continue

        entry = sm['entry_price']
        sl    = sm['sl_price']
        tp1   = sm['tp_price']
        risk  = abs(entry - sl)
        tp2   = round(entry + risk * 3.0 if direction == 'LONG' else entry - risk * 3.0, 6)

        # 仓位大小
        notional = calc_position_size(signal_type, score,
                                      btc_trend='up' if state_info['preferred_direction']=='LONG' else 'neutral')
        # 市场状态仓位调整
        notional = int(notional * state_info['size_multiplier'])
        notional = max(250, min(1000, round(notional / 50) * 50))

        sl_dist_pct = abs(entry - sl) / entry if entry > 0 else 0.02

        # pre_order_gate（含新的 market_state 和 sl_distance_pct）
        gate = pre_order_gate(
            symbol=sym, direction=direction, score=score,
            funding=funding, notional=notional,
            equity=ACCOUNT['equity'],
            sl_distance_pct=sl_dist_pct,
            market_state=market_state,
        )

        if not gate['ok']:
            log(f"  [GATE拒绝] {sym} {direction}: {gate['reason']}", 'warn')
            if gate.get('check') == 9:
                # 建议缩仓而非拒绝
                suggested = gate.get('suggested_notional', notional)
                notional = suggested
                log(f"  [缩仓] {sym} 仓位调整为 {notional}U", 'warn')
                # 重新检查
                gate2 = pre_order_gate(sym, direction, score, funding, notional,
                                       equity=ACCOUNT['equity'],
                                       sl_distance_pct=sl_dist_pct,
                                       market_state=market_state)
                if not gate2['ok']:
                    continue
            else:
                continue

        order = {
            'symbol':    sym,
            'direction': direction,
            'entry':     entry,
            'sl':        sl,
            'tp1':       tp1,
            'tp2':       tp2,
            'notional':  notional,
            'score':     score,
            'scene':     sm['scene'],
            'rr':        sm.get('rr', 0),
            'signal_type': signal_type,
            'placed_hour': hour_idx,
            'expire_hour': hour_idx + 4,  # 4小时有效期
            'tp1_triggered': False,
            'bars_in_range': 0,
            'conditions': sm.get('conditions', {}),   # 含 trail_trigger_price/tp2_high_vol
        }
        ACCOUNT['orders'].append(order)
        opened.append(order)
        log(f"✅ 挂单 {direction} {sym}  评分:{score}  "
            f"场景:{sm['scene']}  入场:{entry:.4f}  SL:{sl:.4f}  "
            f"TP1:{tp1:.4f}  RR:{sm.get('rr',0):.1f}  仓位:{notional}U", 'signal')

    return opened


# ── 成交检查（限价单是否被触碰）─────────────────────────────────────────────

def check_fills(hour_idx: int):
    """检查本小时的价格区间是否触碰挂单价"""
    filled = []
    still_pending = []

    for order in ACCOUNT['orders']:
        sym = order['symbol']
        entry = order['entry']
        direction = order['direction']

        # 检查有效期
        if hour_idx > order['expire_hour']:
            log(f"⏰ 挂单过期撤销 {direction} {sym}  入场:{entry:.4f}", 'gray')
            continue

        high = get_high_at_hour(sym, hour_idx)
        low  = get_low_at_hour(sym, hour_idx)

        if high <= 0 or low <= 0:
            still_pending.append(order)
            continue

        # 多单：价格回调到挂单价
        if direction == 'LONG' and low <= entry:
            filled.append(order)
            pos_key = f"{sym}_{direction}"
            ACCOUNT['positions'][pos_key] = {
                **order,
                'open_hour': hour_idx,
                'open_price': entry,
                'status': 'open',
                'tp1_triggered': False,
                'remaining_pct': 1.0,
                'bars_in_range': 0,
                'trail_activated': False,
                'conditions': order.get('conditions', {}),
            }
            log(f"🎯 多单成交 {sym}  价格:{entry:.4f}  SL:{order['sl']:.4f}  "
                f"TP1:{order['tp1']:.4f}  仓位:{order['notional']}U", 'ok')
        # 空单：价格反弹到挂单价
        elif direction == 'SHORT' and high >= entry:
            filled.append(order)
            pos_key = f"{sym}_{direction}"
            ACCOUNT['positions'][pos_key] = {
                **order,
                'open_hour': hour_idx,
                'open_price': entry,
                'status': 'open',
                'tp1_triggered': False,
                'remaining_pct': 1.0,
                'bars_in_range': 0,
                'trail_activated': False,
                'conditions': order.get('conditions', {}),
            }
            log(f"🎯 空单成交 {sym}  价格:{entry:.4f}  SL:{order['sl']:.4f}  "
                f"TP1:{order['tp1']:.4f}  仓位:{order['notional']}U", 'ok')
        else:
            still_pending.append(order)

    ACCOUNT['orders'] = still_pending
    return filled


# ── 持仓管理（止损/止盈/时间止损）────────────────────────────────────────────

def manage_positions(hour_idx: int):
    """检查所有持仓的止损/止盈触发"""
    closed = []
    still_open = {}

    for pos_key, pos in ACCOUNT['positions'].items():
        sym = pos['symbol']
        direction = pos['direction']
        sl = pos['sl']
        tp1 = pos['tp1']
        tp2 = pos['tp2']
        notional = pos['notional']
        remaining = pos.get('remaining_pct', 1.0)
        tp1_triggered = pos.get('tp1_triggered', False)

        high = get_high_at_hour(sym, hour_idx)
        low  = get_low_at_hour(sym, hour_idx)
        cur_price = get_price_at_hour(sym, hour_idx)

        if high <= 0 or cur_price <= 0:
            still_open[pos_key] = pos
            continue

        close_reason = None
        close_price  = 0.0
        close_pct    = 0.0

        if direction == 'LONG':
            # ── 修改2：时间止损检查（每小时触发）─────────────────────────────
            entry_ts  = pos.get('open_hour', 0) * 3600 * 1000
            current_ts= hour_idx * 3600 * 1000
            scene_name= pos.get('scene', 'A_pullback')
            s2_score  = pos.get('score', 70)
            pos_risk  = abs(pos['open_price'] - pos['sl'])
            from modules.time_stop import check_time_stop as _ts_check
            ts_hit, ts_reason = _ts_check(
                entry_ts, current_ts, scene_name, s2_score,
                pos['open_price'], cur_price, pos['tp1'],
                direction,
                sl_price=pos['sl'], risk=pos_risk,
            )
            if ts_hit and not pos.get('tp1_triggered'):
                close_reason = f'TIME_SL({ts_reason})'
                close_price  = cur_price
                close_pct    = (cur_price - pos['open_price']) / pos['open_price']
            # ── 修改1：移动止损到成本（涨1×ATR后）────────────────────────────
            elif not pos.get('trail_activated') and not pos.get('tp1_triggered'):
                trail_trig = pos.get('conditions', {}).get('trail_trigger_price', 0)
                if trail_trig and cur_price >= trail_trig:
                    pos['sl'] = pos['open_price']   # 止损移到成本
                    pos['trail_activated'] = True
                    log(f"📈 移动止损 LONG {sym}  价格{cur_price:.4f}≥触发{trail_trig:.4f}  SL→成本{pos['open_price']:.4f}", 'cyan')

            # 止损
            if not close_reason and low <= sl:
                close_reason = 'SL'
                close_price  = sl
                close_pct    = (sl - pos['open_price']) / pos['open_price']
            # TP1
            elif not tp1_triggered and high >= tp1:
                # 触发 TP1：平60%，剩余40%止损移到成本价
                partial_pnl = (tp1 - pos['open_price']) / pos['open_price'] * notional * 0.6
                ACCOUNT['trade_log'].append({
                    'symbol': sym, 'direction': direction,
                    'open_price': pos['open_price'], 'close_price': tp1,
                    'pnl': partial_pnl, 'reason': 'TP1_PARTIAL(60%)',
                    'hour': hour_idx, 'notional': notional * 0.6
                })
                pos['tp1_triggered'] = True
                pos['remaining_pct'] = 0.4
                pos['sl'] = pos['open_price']  # 止损移到成本价
                still_open[pos_key] = pos
                log(f"✨ TP1触发 LONG {sym}  平60% @{tp1:.4f}  "
                    f"PnL:+{partial_pnl:.1f}U  止损移至成本", 'ok')
                continue
            # TP2
            elif tp1_triggered and high >= tp2:
                close_reason = 'TP2'
                close_price  = tp2
                close_pct    = (tp2 - pos['open_price']) / pos['open_price'] * remaining
            # 震荡时间止损（TP1已触发，剩余仓位在TP1~TP2区间内震荡）
            elif tp1_triggered:
                # 更新 bars_in_range
                if tp1 <= cur_price <= tp2:
                    pos['bars_in_range'] = pos.get('bars_in_range', 0) + 1
                else:
                    pos['bars_in_range'] = 0
                osc_check = check_tp1_tp2_oscillation(
                    cur_price, tp1, tp2, direction,
                    pos['bars_in_range'],
                    max_oscillation_bars=6
                )
                if osc_check['should_close']:
                    close_reason = 'OSC_TIME_STOP'
                    close_price  = cur_price
                    close_pct    = (cur_price - pos['open_price']) / pos['open_price'] * remaining
                    log(f"⏱️ 震荡时间止损 LONG {sym}  @{cur_price:.4f}  {osc_check['reason']}", 'warn')
                else:
                    still_open[pos_key] = pos
                    continue
        else:  # SHORT
            # ── 修改2：时间止损检查 ───────────────────────────────────────────
            entry_ts  = pos.get('open_hour', 0) * 3600 * 1000
            current_ts= hour_idx * 3600 * 1000
            scene_name= pos.get('scene', 'A_pullback')
            s2_score  = pos.get('score', 70)
            pos_risk  = abs(pos['open_price'] - pos['sl'])
            from modules.time_stop import check_time_stop as _ts_check
            ts_hit, ts_reason = _ts_check(
                entry_ts, current_ts, scene_name, s2_score,
                pos['open_price'], cur_price, pos['tp1'],
                direction,
                sl_price=pos['sl'], risk=pos_risk,
            )
            if ts_hit and not pos.get('tp1_triggered'):
                close_reason = f'TIME_SL({ts_reason})'
                close_price  = cur_price
                close_pct    = (pos['open_price'] - cur_price) / pos['open_price']
            # ── 修改1：移动止损到成本（跌1×ATR后）───────────────────────────
            elif not pos.get('trail_activated') and not pos.get('tp1_triggered'):
                trail_trig = pos.get('conditions', {}).get('trail_trigger_price', 0)
                if trail_trig and cur_price <= trail_trig:
                    pos['sl'] = pos['open_price']
                    pos['trail_activated'] = True
                    log(f"📉 移动止损 SHORT {sym}  价格{cur_price:.4f}≤触发{trail_trig:.4f}  SL→成本{pos['open_price']:.4f}", 'cyan')

            # 止损
            if not close_reason and high >= sl:
                close_reason = 'SL'
                close_price  = sl
                close_pct    = (pos['open_price'] - sl) / pos['open_price']
            # TP1
            elif not tp1_triggered and low <= tp1:
                partial_pnl = (pos['open_price'] - tp1) / pos['open_price'] * notional * 0.6
                ACCOUNT['trade_log'].append({
                    'symbol': sym, 'direction': direction,
                    'open_price': pos['open_price'], 'close_price': tp1,
                    'pnl': partial_pnl, 'reason': 'TP1_PARTIAL(60%)',
                    'hour': hour_idx, 'notional': notional * 0.6
                })
                pos['tp1_triggered'] = True
                pos['remaining_pct'] = 0.4
                pos['sl'] = pos['open_price']
                still_open[pos_key] = pos
                log(f"✨ TP1触发 SHORT {sym}  平60% @{tp1:.4f}  "
                    f"PnL:+{partial_pnl:.1f}U  止损移至成本", 'ok')
                continue
            # TP2
            elif tp1_triggered and low <= tp2:
                close_reason = 'TP2'
                close_price  = tp2
                close_pct    = (pos['open_price'] - tp2) / pos['open_price'] * remaining
            # 震荡时间止损
            elif tp1_triggered:
                if tp2 <= cur_price <= tp1:
                    pos['bars_in_range'] = pos.get('bars_in_range', 0) + 1
                else:
                    pos['bars_in_range'] = 0
                osc_check = check_tp1_tp2_oscillation(
                    cur_price, tp1, tp2, direction,
                    pos['bars_in_range'],
                    max_oscillation_bars=6
                )
                if osc_check['should_close']:
                    close_reason = 'OSC_TIME_STOP'
                    close_price  = cur_price
                    close_pct    = (pos['open_price'] - cur_price) / pos['open_price'] * remaining
                    log(f"⏱️ 震荡时间止损 SHORT {sym}  @{cur_price:.4f}", 'warn')
                else:
                    still_open[pos_key] = pos
                    continue

        if close_reason:
            pnl = close_pct * notional * remaining if remaining < 1.0 else close_pct * notional
            ACCOUNT['trade_log'].append({
                'symbol': sym, 'direction': direction,
                'open_price': pos['open_price'], 'close_price': close_price,
                'pnl': pnl, 'reason': close_reason,
                'hour': hour_idx, 'notional': notional * remaining,
                'scene': pos.get('scene','?'), 'score': pos.get('score',0)
            })
            closed.append(pos_key)
            color = 'ok' if pnl > 0 else 'err'
            emoji = '💰' if pnl > 0 else '🔴'
            log(f"{emoji} 平仓 {direction} {sym}  原因:{close_reason}  "
                f"@{close_price:.4f}  PnL:{pnl:+.1f}U", color)
        else:
            still_open[pos_key] = pos

    ACCOUNT['positions'] = still_open
    return closed


# ── 主循环 ───────────────────────────────────────────────────────────────────

def run_simulation():
    hdr("🚀 2026-08-02 全天交易工作流模拟")
    print(f"  账户余额: {ACCOUNT['equity']}U  |  扫描标的: {len(SCAN_SYMBOLS)} 个")
    print(f"  系统版本: v2.0 (6种市场状态 + 风险敞口控制 + 震荡时间止损)")

    hourly_states = []
    total_signals = 0
    total_halted  = 0
    gate_rejections = 0

    for hour in range(24):
        dt_str = f"2026-08-02 {hour:02d}:00 UTC"
        btc_price = get_price_at_hour('BTCUSDT', hour)

        hdr(f"⏰ {dt_str}  |  BTC: {btc_price:.0f}U")

        # 1. 市场状态判断
        state, state_info = judge_state_at_hour(hour)
        hourly_states.append(state)
        can_open = state_info['can_open']
        emoji = '🟢' if can_open else '🔴'
        log(f"市场状态: {emoji} {state} — {state_info['description']}", 
            'ok' if can_open else 'warn')
        log(f"可开仓: {can_open}  仓位系数: {state_info['size_multiplier']}  "
            f"逆势惩罚: {state_info['counter_penalty']}分", 'gray')

        # HALT 状态：跳过开仓，但继续管理持仓
        if state in MARKET_STATE_HALT:
            total_halted += 1
            log(f"🛑 {state} 状态，跳过本轮扫描", 'warn')
        else:
            # 2. 候选标的评分
            candidates = scan_at_hour(hour, state, state_info)
            if candidates:
                log(f"扫描结果: {len(candidates)} 个候选", 'cyan')
                for c in candidates:
                    log(f"  → {c['direction']:5} {c['symbol']:15} {c['score']:3}分  "
                        f"{c['signal_type']}", 'gray')
                total_signals += len(candidates)
            else:
                log("无有效信号", 'gray')

            # 3. 场景匹配 + gate + 挂单
            if candidates:
                opened = try_open_orders(hour, candidates, state, state_info)

        # 4. 检查上一轮挂单是否成交
        filled = check_fills(hour)

        # 5. 持仓管理
        if ACCOUNT['positions']:
            log(f"当前持仓: {len(ACCOUNT['positions'])} 个", 'cyan')
            closed_keys = manage_positions(hour)
        
        # 6. 每小时持仓状态摘要
        if ACCOUNT['positions']:
            for pk, pos in ACCOUNT['positions'].items():
                cur = get_price_at_hour(pos['symbol'], hour)
                if cur <= 0: continue
                if pos['direction'] == 'LONG':
                    unreal = (cur - pos['open_price']) / pos['open_price'] * pos['notional'] * pos.get('remaining_pct',1.0)
                else:
                    unreal = (pos['open_price'] - cur) / pos['open_price'] * pos['notional'] * pos.get('remaining_pct',1.0)
                tp1t = '✅' if pos.get('tp1_triggered') else '  '
                log(f"  持仓 {pos['direction']} {pos['symbol']}  "
                    f"入场:{pos['open_price']:.4f}  现价:{cur:.4f}  "
                    f"浮盈:{unreal:+.1f}U {tp1t}", 'blue')

    # ── 强制平仓（收盘）──────────────────────────────────────────────────────
    if ACCOUNT['positions']:
        hdr("🔔 23:59 UTC — 收盘强制平仓")
        for pk, pos in list(ACCOUNT['positions'].items()):
            sym = pos['symbol']
            close_price = get_price_at_hour(sym, 23)
            if close_price <= 0: continue
            remaining = pos.get('remaining_pct', 1.0)
            if pos['direction'] == 'LONG':
                pnl = (close_price - pos['open_price']) / pos['open_price'] * pos['notional'] * remaining
            else:
                pnl = (pos['open_price'] - close_price) / pos['open_price'] * pos['notional'] * remaining
            ACCOUNT['trade_log'].append({
                'symbol': sym, 'direction': pos['direction'],
                'open_price': pos['open_price'], 'close_price': close_price,
                'pnl': pnl, 'reason': 'EOD_CLOSE',
                'hour': 23, 'notional': pos['notional'] * remaining,
                'scene': pos.get('scene','?'), 'score': pos.get('score',0)
            })
            color = 'ok' if pnl > 0 else 'err'
            log(f"  {'💰' if pnl>0 else '🔴'} 收盘平 {pos['direction']} {sym}  "
                f"@{close_price:.4f}  PnL:{pnl:+.1f}U", color)

    # ── 最终报告 ─────────────────────────────────────────────────────────────
    hdr("📊 全天模拟汇总报告")

    all_trades = ACCOUNT['trade_log']
    total_pnl = sum(t['pnl'] for t in all_trades)
    wins   = [t for t in all_trades if t['pnl'] > 0]
    losses = [t for t in all_trades if t['pnl'] <= 0]
    by_reason = {}
    for t in all_trades:
        r = t['reason']
        by_reason.setdefault(r, []).append(t['pnl'])

    print(f"\n  {'BTC行情':>20}: {get_price_at_hour('BTCUSDT',0):.0f} → {get_price_at_hour('BTCUSDT',23):.0f}")
    print(f"  {'市场状态分布':>20}: ", end='')
    from collections import Counter
    state_cnt = Counter(hourly_states)
    for s, n in state_cnt.items():
        print(f"{s}×{n}  ", end='')
    print()
    print(f"  {'HALT小时数':>20}: {total_halted}小时（{total_halted/24*100:.0f}%）")
    print(f"  {'总候选信号':>20}: {total_signals} 个")
    print(f"  {'实际成交笔数':>20}: {len([t for t in all_trades if 'PARTIAL' not in t['reason']])} 笔（含分批平仓）")
    print()
    
    # 按原因分类
    print(f"  平仓原因分布:")
    for reason, pnls in sorted(by_reason.items()):
        total_r = sum(pnls)
        print(f"    {reason:20s}: {len(pnls):3d}笔  合计:{total_r:+7.1f}U  "
              f"均值:{total_r/len(pnls):+5.1f}U")
    
    print()
    print(f"  ┌─────────────────────────────────────────┐")
    print(f"  │  总交易笔数: {len(all_trades):>3} 笔  (含TP1分批)      │")
    print(f"  │  盈利笔数: {len(wins):>3}  亏损笔数: {len(losses):>3}          │")
    win_rate = len(wins)/len(all_trades)*100 if all_trades else 0
    print(f"  │  胜率: {win_rate:.0f}%                              │")
    print(f"  │  总 PnL: {total_pnl:>+8.1f} U                       │")
    print(f"  │  账户变化: {ACCOUNT['equity']:.0f} → {ACCOUNT['equity']+total_pnl:.0f} U      │")
    print(f"  └─────────────────────────────────────────┘")

    # 每笔交易明细
    print(f"\n  交易明细:")
    print(f"  {'时间':>5} {'方向':>6} {'标的':>15} {'原因':>15} {'入场':>10} {'出场':>10} {'PnL':>8}")
    print(f"  {'─'*75}")
    for t in sorted(all_trades, key=lambda x: x['hour']):
        pnl_str = C('green', f"{t['pnl']:>+7.1f}U") if t['pnl'] > 0 else C('red', f"{t['pnl']:>+7.1f}U")
        print(f"  {t['hour']:02d}:xx  {t['direction']:>6}  {t['symbol']:>15}  "
              f"{t['reason']:>15}  {t['open_price']:>10.4f}  "
              f"{t['close_price']:>10.4f}  {t['pnl']:>+7.1f}U")

    return total_pnl, all_trades


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.WARNING)  # 安静模式
    total_pnl, trades = run_simulation()
