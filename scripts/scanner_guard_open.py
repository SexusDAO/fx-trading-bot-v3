#!/usr/bin/env python3
"""
scanner_guard_open.py — 观察区细筛开仓脚本
scanner-guard cron 调用此脚本，AI agent 不再自己写开仓代码。

流程：
1. 读 watchlist.json
2. 对每个候选币实时重算评分（calc_score_trend / calc_score_short）
3. 实时分 >= 75 且技术面无重大问题 → 按 _calc_position_usdt 计算仓位 → 开仓
4. 实时分 < 60 → 移出 watchlist
5. 输出结果（JSON），供 agent 播报
"""
import sys, os, json, time
sys.path.insert(0, '/root/.openclaw/workspace')
sys.path.insert(0, '/root/.openclaw/workspace/scripts')

import binance_api as api
from entry_logic import (
    calc_score_trend, calc_score_trend_short,
    calc_score_short, calc_score_reversal,
    liquidation_map, fmt_price, calc_entry_mode,
    classify_signal
)
from scanner import _calc_position_usdt

WL_FILE   = '/root/.openclaw/workspace/ws_cache/watchlist.json'
PS_FILE   = '/root/.openclaw/workspace/pos_state.json'
MAX_POS   = 5
SCORE_OPEN  = 75   # 实时分 >= 此值才开仓
SCORE_DROP  = 60   # 实时分 < 此值移出观察区
RSI_LONG_MAX  = 75  # 多单 RSI 不能超买
RSI_SHORT_MIN = 25  # 空单 RSI 不能超卖

def load_json(path, default={}):
    try:
        with open(path) as f: return json.load(f)
    except: return default

def save_json(path, data):
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)

results = {'opened': [], 'skipped': [], 'removed': []}

# ── 1. 检查当前持仓槽位 ──────────────────────────────────────────────────────
positions = api.get_positions()
active_syms = {p['symbol'] for p in positions if abs(float(p.get('positionAmt', 0))) > 0}
slots_used = len(active_syms)

if slots_used >= MAX_POS:
    print(json.dumps({'status': 'full', 'slots': f'{slots_used}/{MAX_POS}',
                      'msg': f'仓位已满 {slots_used}/{MAX_POS}，跳过细筛'}))
    sys.exit(0)

slots_left = MAX_POS - slots_used

# ── 2. 读 watchlist（统一标准格式 {sym: {target, direction, score, added_at}}）──
wl = load_json(WL_FILE, {})

# 兼容旧格式 {symbols:[], scores:{}} → 自动转换
if 'symbols' in wl:
    _syms = wl.get('symbols', [])
    _scrs = wl.get('scores', {})
    _now_iso = __import__('datetime').datetime.utcnow().isoformat() + 'Z'
    wl = {s: {'target': 0, 'direction': 'below', 'score': _scrs.get(s, 70),
               'added_at': _now_iso} for s in _syms}
    save_json(WL_FILE, wl)

candidates = [(sym, entry) for sym, entry in wl.items()
              if isinstance(entry, dict) and sym not in active_syms]

if not candidates:
    print(json.dumps({'status': 'empty', 'msg': 'watchlist 无候选'}))
    sys.exit(0)

# ── 3. 逐个实时重算评分 ──────────────────────────────────────────────────────
to_open   = []   # (score, sym, direction, details)
to_remove = []   # 低分移出

for sym, entry in candidates:
    # watchlist direction 格式：'below'=等回调入场(LONG)  'above'=等反弹入场(SHORT)
    wl_dir    = entry.get('direction', 'below')
    direction = 'SHORT' if wl_dir == 'above' else 'LONG'
    old_score = float(entry.get('score', 0))

    # 入池时间校验：记录旧分版本入池时间，超过 30 分钟必须强制重算
    try:
        from datetime import datetime as _dt, timezone as _tz
        _added_str = entry.get('added_at', '')
        if _added_str:
            _added_ts = _dt.fromisoformat(_added_str.replace('Z', '+00:00')).timestamp()
        else:
            _added_ts = 0
        _age_min = (time.time() - _added_ts) / 60 if _added_ts else 9999
    except Exception:
        _age_min = 9999
    try:
        k1h  = api.get_klines(sym, '1h', 100)
        k4h  = api.get_klines(sym, '4h', 50)
        k15m = api.get_klines(sym, '15m', 30)
        tk   = api.get_mark_price(sym) or {}
        fr   = float(tk.get('lastFundingRate', 0))
        mark = float(tk.get('markPrice', 0))
        if not mark:
            continue

        # 实时重算
        if direction == 'LONG':
            score_now, det = calc_score_trend(sym, k1h, k4h, k15m, tk, fr)
        else:
            score_now, det = calc_score_trend_short(sym, k1h, k4h, k15m, tk, fr)

        rsi_1h = det.get('rsi_1h', 50)
        ema_1h = det.get('ema_1h', '')
        ema_4h = det.get('ema_4h', '')

        # 低分移出
        if score_now < SCORE_DROP:
            to_remove.append(sym)
            results['removed'].append({'sym': sym, 'old': old_score, 'now': round(score_now, 1), 'reason': '评分跌破60'})
            continue

        # 不够格开仓
        if score_now < SCORE_OPEN:
            results['skipped'].append({'sym': sym, 'score': round(score_now, 1), 'reason': '实时分<75'})
            continue

        # RSI 过滤
        if direction == 'LONG' and rsi_1h > RSI_LONG_MAX:
            results['skipped'].append({'sym': sym, 'score': round(score_now, 1), 'reason': f'RSI1H={rsi_1h:.0f}>75超买'})
            continue
        if direction == 'SHORT' and rsi_1h < RSI_SHORT_MIN:
            results['skipped'].append({'sym': sym, 'score': round(score_now, 1), 'reason': f'RSI1H={rsi_1h:.0f}<25超卖'})
            continue

        # 近期大阴线/大阳线过滤（开仓前1H出现反向放量K线）
        c1h = [float(k[4]) for k in k1h]
        v1h = [float(k[5]) for k in k1h]
        avg_v = sum(v1h[-21:-1]) / 20 if len(v1h) >= 21 else 1
        last_candle_pct = (c1h[-1] - float(k1h[-1][1])) / float(k1h[-1][1]) * 100  # 当根涨跌幅
        last_vr = v1h[-1] / avg_v if avg_v > 0 else 1

        if direction == 'LONG' and last_candle_pct < -3 and last_vr > 2:
            results['skipped'].append({'sym': sym, 'score': round(score_now, 1),
                                       'reason': f'近期放量大阴线({last_candle_pct:.1f}% VR={last_vr:.1f}x)'})
            continue
        if direction == 'SHORT' and last_candle_pct > 3 and last_vr > 2:
            results['skipped'].append({'sym': sym, 'score': round(score_now, 1),
                                       'reason': f'近期放量大阳线({last_candle_pct:.1f}% VR={last_vr:.1f}x)'})
            continue

        to_open.append((score_now, sym, direction, det, mark, k1h, k4h, old_score))

    except Exception as e:
        results['skipped'].append({'sym': sym, 'score': old_score, 'reason': f'评分异常: {e}'})

# ── 4. 按评分排序，只开槽位允许的数量 ───────────────────────────────────────
to_open.sort(key=lambda x: -x[0])
to_open = to_open[:slots_left]

# ── 5. 执行开仓 ──────────────────────────────────────────────────────────────
ps = load_json(PS_FILE, {})

for score_now, sym, direction, det, mark, k1h, k4h, old_score in to_open:
    try:
        # 仓位计算（严格按 _calc_position_usdt 规则，注入方向）
        det['direction'] = direction
        notional = _calc_position_usdt(score=score_now, details=det)

        # 精度
        sym_info = api.get_sym_info_cached(sym) or {}
        tick = float(next((f['tickSize'] for f in sym_info.get('filters', [])
                           if f['filterType'] == 'PRICE_FILTER'), 0.0001))
        qty_prec = int(sym_info.get('quantityPrecision', 2))

        qty = round(notional / mark, qty_prec)
        if qty <= 0:
            results['skipped'].append({'sym': sym, 'reason': 'qty=0'})
            continue

        # 清算图 SL/TP
        lmap = liquidation_map(sym, k1h, k4h, direction)
        sl_p  = lmap.get('hard_sl',  mark * (0.95 if direction == 'LONG' else 1.05))
        tp1_p = lmap.get('soft_tp1', mark * (1.04 if direction == 'LONG' else 0.96))

        import math as _m
        d = max(0, -int(_m.floor(_m.log10(tick))))
        def fp(p): return f"{round(round(p/tick)*tick, d):.{d}f}"

        order_side = 'BUY' if direction == 'LONG' else 'SELL'
        close_side = 'SELL' if direction == 'LONG' else 'BUY'

        # 市价开仓
        r = api.place_order(sym, order_side, 'MARKET', quantity=qty, positionSide=direction)
        if not r.get('orderId'):
            results['skipped'].append({'sym': sym, 'score': round(score_now, 1),
                                       'reason': f'开仓失败: {r}'})
            continue

        time.sleep(1)

        # 挂 TP（LIMIT）
        r_tp = api.place_order(sym, close_side, 'LIMIT',
                               quantity=qty, price=fp(tp1_p),
                               positionSide=direction, timeInForce='GTC')
        # 挂 SL（algo STOP_MARKET）
        r_sl = api.place_algo_order(sym, close_side, 'STOP_MARKET',
                                    fp(sl_p), direction, quantity=qty)

        # 写 pos_state
        ps[sym] = {
            'direction':   direction,
            'entry_price': mark,
            'score':       round(score_now, 1),
            'notional':    notional,
            'tp1_price':   float(fp(tp1_p)),
            'sl_price':    float(fp(sl_p)),
            'opened_at':   time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'source':      'scanner_guard_script',
        }
        save_json(PS_FILE, ps)

        # 移出 watchlist
        to_remove.append(sym)

        results['opened'].append({
            'sym': sym, 'direction': direction,
            'score_old': old_score, 'score_now': round(score_now, 1),
            'notional': notional, 'entry': mark,
            'tp': float(fp(tp1_p)), 'sl': float(fp(sl_p)),
            'order_id': r.get('orderId'),
        })

    except Exception as e:
        results['skipped'].append({'sym': sym, 'score': round(score_now, 1), 'reason': f'开仓异常: {e}'})

# ── 6. 更新 watchlist（移除低分和已开仓） ────────────────────────────────────
changed = False
for sym in set(to_remove):
    if sym in wl:
        del wl[sym]
        changed = True
if changed:
    save_json(WL_FILE, wl)

print(json.dumps(results, ensure_ascii=False, indent=2))
