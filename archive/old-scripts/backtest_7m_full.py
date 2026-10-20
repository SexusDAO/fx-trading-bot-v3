"""
backtest_7m_full.py — 完整体工作流回测
配置：评分门槛75，SL最小2.5%，85+分TP收窄2xATR，加入清算图模拟锚定
清算图说明：真实工作流用Coinglass实时数据，回测用K线结构模拟：
  - liq_below: 过去48H最大量能K线低点（多头爆仓密集区）
  - liq_above: 过去48H最大量能K线高点（空头爆仓密集区）
  - hard_sl:   清算密集区外侧0.5xATR（比纯ATR乘数更合理）
  - soft_tp1:  对向清算密集区前沿（比固定TP倍数更接近真实工作流）
  - 订单簿墙:  用近20根K线最大成交量K线的高/低点模拟
"""
import sys, json, time, datetime as dt, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, "/root/.openclaw/workspace")
from entry_logic import rsi_calc, ema_calc, atr_calc

START_TS = int(dt.datetime(2026, 7, 1,  0, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)
END_TS   = int(dt.datetime(2026, 8, 22, 23, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)
POS_USDT   = 100.0
SCORE_MIN  = 75
SL_MIN_PCT = 2.5       # SL最小距离保护
TP_HIGH_MULT    = 2.0  # 85+分TP倍数
TP_NORMAL_MULT  = 3.0  # 普通TP倍数
HIGH_SCORE_GATE = 85
ORDER_TTL  = 4         # 挂单有效期（小时）
DEV_MAX    = 3.0       # 挂单偏差容忍

SYMBOLS = [
    "BTCUSDT","ETHUSDT","XRPUSDT","SOLUSDT","HYPEUSDT","TRUMPUSDT","DOGEUSDT",
    "SNDKUSDT","ENAUSDT","SPCXUSDT","1000PEPEUSDT","SUIUSDT","ADAUSDT","BCHUSDT",
    "LINKUSDT","SKHYNIXUSDT","SNXXUSDT","SKHYUSDT","WLDUSDT","KORUUSDT","NEARUSDT",
    "AAVEUSDT","ONDOUSDT","CLUSDT","AVAXUSDT","UNIUSDT","XLMUSDT","PENGUUSDT",
    "LTCUSDT","SOXSUSDT","GALAUSDT","FILUSDT","ACEUSDT","ETCUSDT","POLUSDT",
    "BOMEUSDT","TSLAUSDT","FARTCOINUSDT","TRXUSDT","MRVLUSDT","XMRUSDT",
    "CRVUSDT","1000BONKUSDT","HBARUSDT","INTCUSDT","NEIROUSDT","WIFUSDT",
    "ENSUSDT","SANDUSDT","COINUSDT","PEOPLEUSDT","NVDAUSDT","OPUSDT",
    "INJUSDT","ORDIUSDT","ICPUSDT","VIRTUALUSDT","MUBARAKUSDT","STXUSDT",
    "SAMSUNGUSDT","TIAUSDT","MOVEUSDT","TAOUSDT","WLFIUSDT","HEMIUSDT",
    "ONGUSDT","DRAMUSDT","BTWUSDT","COTIUSDT","ZAMAUSDT","QQQUSDT","REUSDT",
    "PAXGUSDT","MAGMAUSDT","ROBOUSDT","ZORAUSDT","ETHFIUSDT","NBISUSDT",
    "GPSUSDT","KAITOUSDT","GOOGLUSDT","MUUUSDT","ZENUSDT","ASTERUSDT",
    "CRCLUSDT","MSTRUSDT","XPLUSDT","BLESSUSDT","PROMUSDT","AAOIUSDT",
    "MELANIAUSDT","MUUSDT","BZUSDT","DASHUSDT","MVLLUSDT","ZECUSDT",
    "PUMPUSDT","XAUUSDT","XAGUSDT",
]

# ── K线拉取 ───────────────────────────────────────────────────────────────────
def fetch_klines_full(sym, interval, start_ms, end_ms):
    all_k = []; cur = start_ms
    base = "https://fapi.binance.com"
    while cur < end_ms:
        url = (f"{base}/fapi/v1/klines?symbol={sym}&interval={interval}"
               f"&startTime={cur}&endTime={end_ms}&limit=1500")
        try:
            batch = json.loads(urllib.request.urlopen(url, timeout=15).read())
        except Exception as e:
            print(f"  [fetch] {sym} err: {e}"); break
        if not batch: break
        all_k.extend(batch)
        last_ts = int(batch[-1][0])
        if last_ts <= cur: break
        cur = last_ts + 1
        if len(batch) < 1500: break
    seen = set(); result = []
    for k in all_k:
        ts = int(k[0])
        if ts not in seen and ts <= end_ms:
            seen.add(ts); result.append(k)
    result.sort(key=lambda x: int(x[0]))
    return result

# ── 清算图模拟（核心，替代Coinglass实时数据）──────────────────────────────────
def sim_liquidation_map(k1h_window, direction, atr14):
    """
    模拟 liquidation_map 输出，基于K线结构：
    - 多头：SL锚定近48H最低支撑区外侧，TP1锚定近48H压力区前沿
    - 空头：SL锚定近48H最高阻力区外侧，TP1锚定近48H支撑区前沿
    额外修正：
    - 用成交量最大的K线高/低点模拟订单簿挂单墙
    - SL必须在主锚外侧 0.3xATR（模拟清算密集区边缘保护）
    """
    if len(k1h_window) < 24:
        return None

    recent = k1h_window[-48:] if len(k1h_window) >= 48 else k1h_window
    c_now  = float(k1h_window[-1][4])

    highs = [float(k[2]) for k in recent]
    lows  = [float(k[3]) for k in recent]
    vols  = [float(k[5]) for k in recent]

    # 成交量加权：找高量K线作为"订单簿大墙"模拟
    vol_avg = sum(vols) / len(vols) if vols else 1
    high_vol_bars = [i for i, v in enumerate(vols) if v > vol_avg * 1.5]

    if high_vol_bars:
        ob_support    = min(lows[i]  for i in high_vol_bars)
        ob_resistance = max(highs[i] for i in high_vol_bars)
    else:
        ob_support    = min(lows)
        ob_resistance = max(highs)

    # 关键结构位（近20根波动高低点）
    recent20 = k1h_window[-20:]
    key_low  = min(float(k[3]) for k in recent20)
    key_high = max(float(k[2]) for k in recent20)

    # 混合取最合理的锚点
    liq_below = min(ob_support,  key_low)   # 下方清算密集区
    liq_above = max(ob_resistance, key_high) # 上方清算密集区

    if direction == "LONG":
        # SL：下方清算密集区外侧 0.3xATR
        hard_sl = liq_below - atr14 * 0.3
        # 入场：贴近下方清算密集区上沿（回调等待）
        safe_entry = c_now - atr14 * 0.4
        # TP1：上方清算密集区前沿（不穿越大阻力）
        soft_tp1 = liq_above - atr14 * 0.5
    else:
        # SHORT
        hard_sl  = liq_above + atr14 * 0.3
        safe_entry = c_now + atr14 * 0.4
        soft_tp1 = liq_below + atr14 * 0.5

    return {
        "hard_sl":    hard_sl,
        "soft_tp1":   soft_tp1,
        "safe_entry": safe_entry,
        "liq_below":  liq_below,
        "liq_above":  liq_above,
        "ob_support":    ob_support,
        "ob_resistance": ob_resistance,
    }

# ── 评分 + 完整体开单逻辑 ────────────────────────────────────────────────────
def score_and_entry(k1h_window, k4h_window):
    """
    完整体：评分 + 清算图锚定 + SL最小距离 + 高分TP收窄
    返回 (score, direction, sl, tp1, entry) 或 (0, None, ...)
    """
    if len(k1h_window) < 20 or len(k4h_window) < 10:
        return 0, None, 0, 0, 0

    c1h  = [float(k[4]) for k in k1h_window]
    c4h  = [float(k[4]) for k in k4h_window]
    v1h  = [float(k[5]) for k in k1h_window]
    mark = c1h[-1]
    atr14 = atr_calc(k1h_window)
    if atr14 <= 0: return 0, None, 0, 0, 0

    # ── 1. 大K线冷却过滤 ────────────────────────────────────────────────────
    for ki in range(-12, 0):
        if abs(ki) > len(k1h_window): continue
        kl = k1h_window[ki]
        kh, kl2 = float(kl[2]), float(kl[3])
        kr = kh - kl2
        ko, kc = float(kl[1]), float(kl[4])
        range_pct  = kr / kl2 * 100 if kl2 > 0 else 0
        kdown_pct  = (ko - kc) / ko * 100 if ko > 0 else 0
        if kr > atr14 * 3.5 and (range_pct > 10 or kdown_pct > 8):
            return 0, None, 0, 0, 0

    # ── 2. 技术指标 ──────────────────────────────────────────────────────────
    rsi1h = rsi_calc(c1h); rsi4h = rsi_calc(c4h)
    ema20_1h = ema_calc(c1h[-20:], 20)
    ema50_1h = ema_calc(c1h[-50:], 50) if len(c1h)>=50 else ema_calc(c1h, len(c1h))
    ema20_4h = ema_calc(c4h[-20:], 20) if len(c4h)>=20 else ema_calc(c4h, len(c4h))
    ema50_4h = ema_calc(c4h[-50:], 50) if len(c4h)>=50 else ema_calc(c4h, len(c4h))
    vb  = sum(v1h[-20:-3])/17 if len(v1h)>=20 else (sum(v1h)/len(v1h) if v1h else 1)
    vr  = (sum(v1h[-3:])/3)/vb if vb > 0 else 1
    sma20 = sum(c1h[-20:])/20
    std20 = (sum((x-sma20)**2 for x in c1h[-20:])/20)**0.5
    bb_u = sma20+2*std20; bb_l = sma20-2*std20
    bb_pct = (c1h[-1]-bb_l)/(bb_u-bb_l)*100 if (bb_u-bb_l)>0 else 50
    lk = k1h_window[-1]
    o2, h2, lw2, c2 = float(lk[1]), float(lk[2]), float(lk[3]), float(lk[4])
    body = abs(c2-o2); lower = min(o2,c2)-lw2
    hammer   = lower > body*2 and body > 0
    dir1h    = '↑' if c1h[-1]>c1h[-2]>c1h[-3] else ('↓' if c1h[-1]<c1h[-2]<c1h[-3] else '~')
    chg24    = (c1h[-1]-c1h[-25])/c1h[-25]*100 if len(c1h)>=25 else 0

    # ── 3. 评分（多头）───────────────────────────────────────────────────────
    both_long = ema20_1h>ema50_1h and ema20_4h>ema50_4h
    one_long  = ema20_1h>ema50_1h or  ema20_4h>ema50_4h
    sl = 0
    score_l = 0
    if both_long:  score_l += 30
    elif one_long: score_l += 15
    if 50<=rsi1h<=65:   score_l += 15
    elif 40<=rsi1h<50:  score_l += 8
    if rsi4h>75:        score_l -= 15
    elif rsi4h>68:      score_l -= 8
    if 50<=rsi4h<=65:   score_l += 10
    elif 40<=rsi4h<50:  score_l += 5
    if vr>1.5:   score_l += 20
    elif vr>1.2: score_l += 12
    elif vr>1.0: score_l += 6
    if bb_pct>55:   score_l += 10
    if dir1h=='↑':  score_l += 8
    if hammer:      score_l += 7
    if chg24>3:     score_l += 10

    # ── 4. 评分（空头）───────────────────────────────────────────────────────
    both_short = ema20_1h<ema50_1h and ema20_4h<ema50_4h
    one_short  = ema20_1h<ema50_1h or  ema20_4h<ema50_4h
    score_s = 0
    if both_short:  score_s += 30
    elif one_short: score_s += 15
    if 35<=rsi1h<=50:   score_s += 15
    elif 50<rsi1h<=60:  score_s += 8
    if rsi4h<25:        score_s -= 15
    elif rsi4h<32:      score_s -= 8
    if 35<=rsi4h<=50:   score_s += 10
    elif 50<rsi4h<=60:  score_s += 5
    if vr>1.5:   score_s += 20
    elif vr>1.2: score_s += 12
    elif vr>1.0: score_s += 6
    if bb_pct<45:   score_s += 10
    if dir1h=='↓':  score_s += 8
    if chg24<-3:    score_s += 10

    # ── 5. 方向选择 ──────────────────────────────────────────────────────────
    if score_l >= SCORE_MIN and score_l >= score_s:
        score = score_l; direction = "LONG"
    elif score_s >= SCORE_MIN:
        score = score_s; direction = "SHORT"
    else:
        return 0, None, 0, 0, 0

    # ── 6. 清算图锚定止损/止盈/入场 ─────────────────────────────────────────
    lmap = sim_liquidation_map(k1h_window, direction, atr14)

    if lmap:
        entry = round(lmap["safe_entry"], 8)
        sl    = round(lmap["hard_sl"], 8)
        # TP1：ATR倍数为主，清算图前沿作为天花板约束
        # 真实工作流：SL锚定清算密集区外侧，TP用ATR倍数但不穿越对向清算密集区
        tp_mult = TP_NORMAL_MULT  # 统一3xATR，不区分高低评分
        if direction == "LONG":
            tp_from_atr  = entry + atr14 * tp_mult
            tp_from_lmap = lmap["soft_tp1"]
            # soft_tp1是对向清算区前沿：若比ATR目标更近则用ATR；若更远则取ATR（不追涨）
            if tp_from_lmap > entry and tp_from_lmap < tp_from_atr:
                # 清算区压力在ATR目标前面，TP提前在压力区前 0.5xATR
                tp1 = round(tp_from_lmap - atr14 * 0.5, 8)
                if tp1 < entry + atr14 * 1.5:  # 最小保证1.5xATR
                    tp1 = round(tp_from_atr, 8)
            else:
                tp1 = round(tp_from_atr, 8)
        else:
            tp_from_atr  = entry - atr14 * tp_mult
            tp_from_lmap = lmap["soft_tp1"]
            if tp_from_lmap < entry and tp_from_lmap > tp_from_atr:
                tp1 = round(tp_from_lmap + atr14 * 0.5, 8)
                if tp1 > entry - atr14 * 1.5:
                    tp1 = round(tp_from_atr, 8)
            else:
                tp1 = round(tp_from_atr, 8)
    else:
        # fallback: 纯ATR
        tp_mult = TP_NORMAL_MULT  # 统一3xATR，不区分高低评分
        if direction == "LONG":
            entry = round(mark - atr14*0.4, 8)
            sl    = round(entry - atr14*1.8, 8)
            tp1   = round(entry + atr14*tp_mult, 8)
        else:
            entry = round(mark + atr14*0.4, 8)
            sl    = round(entry + atr14*1.8, 8)
            tp1   = round(entry - atr14*tp_mult, 8)

    # ── 7. SL最小距离保护（2.5%）────────────────────────────────────────────
    if direction == "LONG":
        sl_dist_pct = (entry - sl) / entry * 100
    else:
        sl_dist_pct = (sl - entry) / entry * 100

    if sl_dist_pct < SL_MIN_PCT:
        return 0, None, 0, 0, 0

    # ── 8. TP必须合理（方向正确）────────────────────────────────────────────
    if direction == "LONG" and tp1 <= entry:
        return 0, None, 0, 0, 0
    if direction == "SHORT" and tp1 >= entry:
        return 0, None, 0, 0, 0

    return score, direction, sl, tp1, entry


# ── 回测引擎 ─────────────────────────────────────────────────────────────────
def run_backtest(sym_klines_map):
    label = "v_full"
    print(f"\n{'='*60}\n开始完整体回测: {label}\n{'='*60}")
    trades = []; pending = {}; positions = {}
    all_ts = set()
    for klines in sym_klines_map.values():
        for k in klines: all_ts.add(int(k[0]))
    timeline = sorted(all_ts)
    sym_ts_idx = {sym: {int(k[0]): i for i, k in enumerate(klines)}
                  for sym, klines in sym_klines_map.items()}
    step = max(1, len(timeline)//20)

    for bar_idx, ts in enumerate(timeline):
        if bar_idx % step == 0:
            ts_str = dt.datetime.utcfromtimestamp(ts/1000).strftime("%m-%d %H:%M")
            print(f"  [{label}] {bar_idx/len(timeline)*100:.0f}%  {ts_str}  "
                  f"持仓={len(positions)}  挂单={len(pending)}  成交={len(trades)}")

        for sym, klines in sym_klines_map.items():
            idx = sym_ts_idx[sym].get(ts)
            if idx is None or idx < 1: continue
            cur_k = klines[idx]
            cur_h, cur_l, cur_c = float(cur_k[2]), float(cur_k[3]), float(cur_k[4])

            # 持仓SL/TP检查
            if sym in positions:
                pos = positions[sym]
                hit_sl = hit_tp = False
                if pos['dir'] == 'LONG':
                    if cur_l <= pos['sl']:  hit_sl = True
                    if cur_h >= pos['tp1']: hit_tp = True
                else:
                    if cur_h >= pos['sl']:  hit_sl = True
                    if cur_l <= pos['tp1']: hit_tp = True
                if hit_tp or hit_sl:
                    close_px = pos['tp1'] if hit_tp else pos['sl']
                    reason   = "TP1" if hit_tp else "SL"
                    if pos['dir'] == 'LONG':
                        pnl = (close_px - pos['entry']) / pos['entry'] * pos['usdt']
                    else:
                        pnl = (pos['entry'] - close_px) / pos['entry'] * pos['usdt']
                    trades.append({
                        "sym":sym, "dir":pos['dir'], "entry":pos['entry'],
                        "close":close_px, "sl":pos['sl'], "tp1":pos['tp1'],
                        "usdt":pos['usdt'], "score":pos['score'],
                        "reason":reason, "pnl":round(pnl, 4),
                        "open_ts":pos['open_ts'], "close_ts":ts,
                        "open_dt":dt.datetime.utcfromtimestamp(pos['open_ts']/1000).strftime("%m-%d %H:%M"),
                        "close_dt":dt.datetime.utcfromtimestamp(ts/1000).strftime("%m-%d %H:%M"),
                        "sl_pct": abs(pos['entry']-pos['sl'])/pos['entry']*100,
                        "tp_pct": abs(pos['tp1']-pos['entry'])/pos['entry']*100,
                    })
                    del positions[sym]; continue

            # 挂单检查
            if sym in pending:
                pend = pending[sym]
                if (ts - pend['placed_ts']) // 3600000 >= ORDER_TTL:
                    del pending[sym]; continue
                triggered = ((pend['dir']=='LONG'  and cur_l <= pend['entry']) or
                             (pend['dir']=='SHORT' and cur_h >= pend['entry']))
                if triggered:
                    positions[sym] = {**pend, "open_ts": ts}
                    del pending[sym]; continue

            if sym in positions or sym in pending: continue

            # 扫描新信号
            end_i = idx + 1
            k1h_w = klines[max(0, end_i-55):end_i]
            if len(k1h_w) < 20: continue
            # 4H合并
            k1h_4h = klines[max(0, end_i-200):end_i]
            k4h_w = []; tmp = []
            for k in k1h_4h:
                tmp.append(k)
                if len(tmp) == 4:
                    k4h_w.append([
                        tmp[0][0], tmp[0][1],
                        str(max(float(x[2]) for x in tmp)),
                        str(min(float(x[3]) for x in tmp)),
                        tmp[-1][4],
                        str(sum(float(x[5]) for x in tmp)),
                    ] + tmp[-1][6:])
                    tmp = []
            if len(k4h_w) < 10: continue

            score, direction, sl, tp1, entry = score_and_entry(k1h_w, k4h_w)
            if score < SCORE_MIN or direction is None: continue
            if abs(entry - cur_c) / cur_c * 100 > DEV_MAX: continue

            pending[sym] = {
                "entry":entry, "sl":sl, "tp1":tp1, "dir":direction,
                "usdt":POS_USDT, "score":score, "placed_ts":ts
            }

    # 未平仓到期
    for sym, pos in positions.items():
        klines = sym_klines_map.get(sym, [])
        lc = float(klines[-1][4]) if klines else pos['entry']
        if pos['dir'] == 'LONG':
            pnl = (lc - pos['entry']) / pos['entry'] * pos['usdt']
        else:
            pnl = (pos['entry'] - lc) / pos['entry'] * pos['usdt']
        trades.append({
            "sym":sym, "dir":pos['dir'], "entry":pos['entry'], "close":lc,
            "sl":pos['sl'], "tp1":pos['tp1'], "usdt":pos['usdt'], "score":pos['score'],
            "reason":"持有到期", "pnl":round(pnl, 4),
            "open_ts":pos['open_ts'], "close_ts":END_TS,
            "open_dt":dt.datetime.utcfromtimestamp(pos['open_ts']/1000).strftime("%m-%d %H:%M"),
            "close_dt":"end",
            "sl_pct": abs(pos['entry']-pos['sl'])/pos['entry']*100,
            "tp_pct": abs(pos['tp1']-pos['entry'])/pos['entry']*100,
        })
    return trades


# ── 详细报告 ─────────────────────────────────────────────────────────────────
def full_report(trades):
    import collections, statistics
    if not trades:
        print("无交易记录"); return

    wins   = [t for t in trades if t['pnl'] > 0]
    losses = [t for t in trades if t['pnl'] <= 0]
    tp_sum  = sum(t['pnl'] for t in trades)
    aw = sum(t['pnl'] for t in wins)/len(wins) if wins else 0
    al = abs(sum(t['pnl'] for t in losses)/len(losses)) if losses else 1
    wr = len(wins)/len(trades)*100
    rr = aw/al if al else 0

    # 最大回撤 + 净值曲线
    equity = 1000.0; peak = 1000.0; mdd = 0.0
    monthly_pnl = collections.defaultdict(float)
    for t in sorted(trades, key=lambda x: x['close_ts']):
        equity += t['pnl']
        if equity > peak: peak = equity
        dd = (peak - equity) / peak * 100
        if dd > mdd: mdd = dd
        mo = dt.datetime.utcfromtimestamp(t['close_ts']/1000).strftime("%Y-%m")
        monthly_pnl[mo] += t['pnl']

    longs  = [t for t in trades if t['dir']=='LONG']
    shorts = [t for t in trades if t['dir']=='SHORT']
    lw = [t for t in longs  if t['pnl']>0]
    sw = [t for t in shorts if t['pnl']>0]

    print(f"\n{'='*65}")
    print(f"📊 完整体工作流回测报告 | v_full | 7.1~8.22 | {len(set(t['sym'] for t in trades))}个币种")
    print(f"{'='*65}")
    print(f"总交易:    {len(trades)}笔   多={len(longs)}笔  空={len(shorts)}笔")
    print(f"胜率:      {wr:.1f}%   W{len(wins)}/L{len(losses)}")
    print(f"总盈亏:    {tp_sum:+.2f}U   (每单固定100U)")
    print(f"均胜:     +{aw:.2f}U   均亏: -{al:.2f}U   盈亏比: 1:{rr:.2f}")
    print(f"最大回撤:  -{mdd:.1f}%   估算终态净值: {1000+tp_sum:.0f}U")
    print(f"多头胜率:  {len(lw)/len(longs)*100:.1f}%  ({len(lw)}/{len(longs)})")
    if shorts:
        print(f"空头胜率:  {len(sw)/len(shorts)*100:.1f}%  ({len(sw)}/{len(shorts)})")

    print(f"\n--- 月度盈亏 ---")
    for mo, pnl in sorted(monthly_pnl.items()):
        grp = [t for t in trades if dt.datetime.utcfromtimestamp(t['close_ts']/1000).strftime("%Y-%m")==mo]
        gw  = [t for t in grp if t['pnl']>0]
        print(f"  {mo}  {pnl:+.2f}U   {len(grp)}笔  胜率={len(gw)/len(grp)*100:.1f}%")

    print(f"\n--- 评分分档 ---")
    for lo, hi in [(75,80),(80,85),(85,90),(90,200)]:
        grp = [t for t in trades if lo <= t['score'] < hi]
        if not grp: continue
        gw  = [t for t in grp if t['pnl']>0]
        gp  = sum(t['pnl'] for t in grp)
        print(f"  [{lo}-{hi})  n={len(grp):4}  胜率={len(gw)/len(grp)*100:.1f}%  总盈亏={gp:+.1f}U  avg={gp/len(grp):+.2f}U")

    print(f"\n--- SL距离分档 ---")
    for lo, hi in [(0,3),(3,5),(5,8),(8,15),(15,50)]:
        grp = [t for t in trades if lo <= t.get('sl_pct',0) < hi]
        if not grp: continue
        gw  = [t for t in grp if t['pnl']>0]
        gp  = sum(t['pnl'] for t in grp)
        print(f"  SL {lo}-{hi}%  n={len(grp):4}  胜率={len(gw)/len(grp)*100:.1f}%  总盈亏={gp:+.1f}U  avg={gp/len(grp):+.2f}U")

    print(f"\n--- TP距离分档（清算图效果）---")
    for lo, hi in [(0,5),(5,10),(10,20),(20,50)]:
        grp = [t for t in trades if lo <= t.get('tp_pct',0) < hi]
        if not grp: continue
        gw  = [t for t in grp if t['pnl']>0]
        gp  = sum(t['pnl'] for t in grp)
        print(f"  TP {lo}-{hi}%  n={len(grp):4}  胜率={len(gw)/len(grp)*100:.1f}%  总盈亏={gp:+.1f}U  avg={gp/len(grp):+.2f}U")

    print(f"\n--- 持仓时长 ---")
    for lo, hi in [(0,4),(4,12),(12,24),(24,72),(72,999)]:
        grp = [t for t in trades if lo <= (t['close_ts']-t['open_ts'])//3600000 < hi]
        if not grp: continue
        gw  = [t for t in grp if t['pnl']>0]
        gp  = sum(t['pnl'] for t in grp)
        print(f"  {lo}-{hi}h  n={len(grp):4}  胜率={len(gw)/len(grp)*100:.1f}%  总盈亏={gp:+.1f}U")

    print(f"\n--- 盈利Top10 ---")
    for t in sorted(trades, key=lambda x: -x['pnl'])[:10]:
        print(f"  {t['sym']:<14} {t['dir']}  {t['open_dt']}→{t['close_dt']}  {t['pnl']:+.2f}U  sl={t.get('sl_pct',0):.1f}%  tp={t.get('tp_pct',0):.1f}%  {t['reason']}")

    print(f"\n--- 亏损Top10 ---")
    for t in sorted(trades, key=lambda x: x['pnl'])[:10]:
        print(f"  {t['sym']:<14} {t['dir']}  {t['open_dt']}→{t['close_dt']}  {t['pnl']:+.2f}U  sl={t.get('sl_pct',0):.1f}%  tp={t.get('tp_pct',0):.1f}%  {t['reason']}")

    print(f"\n--- 币种盈亏排行（Top/Bottom 10）---")
    sym_pnl = collections.defaultdict(lambda: {"pnl":0,"n":0,"w":0})
    for t in trades:
        sym_pnl[t['sym']]["pnl"] += t['pnl']
        sym_pnl[t['sym']]["n"]   += 1
        if t['pnl'] > 0: sym_pnl[t['sym']]["w"] += 1
    ranked = sorted(sym_pnl.items(), key=lambda x: -x[1]["pnl"])
    print("  盈利Top10:")
    for sym, s in ranked[:10]:
        print(f"    {sym:<16} {s['pnl']:+.1f}U  {s['n']}笔  胜率={s['w']/s['n']*100:.0f}%")
    print("  亏损Top10:")
    for sym, s in ranked[-10:]:
        print(f"    {sym:<16} {s['pnl']:+.1f}U  {s['n']}笔  胜率={s['w']/s['n']*100:.0f}%")

    return {
        "total":len(trades), "wins":len(wins), "losses":len(losses),
        "win_rate":wr, "total_pnl":tp_sum, "avg_win":aw, "avg_loss":al,
        "rr":rr, "max_dd":mdd, "trades":trades
    }


# ── 主流程 ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"[backtest_full] 开始 {dt.datetime.utcnow().strftime('%H:%M UTC')}")
    print(f"配置: 评分门槛={SCORE_MIN}  SL最小={SL_MIN_PCT}%  "
          f"TP高分倍数={TP_HIGH_MULT}x  TP普通倍数={TP_NORMAL_MULT}x  清算图=模拟")

    print(f"\n[1/3] 拉取历史K线数据 ({len(SYMBOLS)}个币)...")
    sym_klines = {}
    def fetch(sym):
        try: return sym, fetch_klines_full(sym, "1h", START_TS, END_TS)
        except: return sym, []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(fetch, s): s for s in SYMBOLS}
        done = 0
        for f in as_completed(futs):
            sym, klines = f.result(); done += 1
            if klines: sym_klines[sym] = klines
            if done % 20 == 0:
                print(f"  {done}/{len(SYMBOLS)}  就绪={len(sym_klines)}")
            time.sleep(0.05)
    print(f"  数据就绪: {len(sym_klines)}个币")

    print(f"\n[2/3] 执行完整体回测...")
    trades = run_backtest(sym_klines)

    print(f"\n[3/3] 生成详细报告...")
    result = full_report(trades)

    # 对比之前的版本
    try:
        with open("backtest_7m_v2_result.json") as f:
            prev = json.load(f)
        prev_summaries = {r['label']: r for r in prev.get('summaries', [])}
        print(f"\n{'='*65}")
        print(f"📈 与之前版本对比")
        print(f"{'='*65}")
        print(f"{'版本':<14} {'笔数':>6} {'胜率%':>7} {'总盈亏U':>10} {'盈亏比':>7} {'最大回撤%':>10}")
        print(f"{'-'*58}")
        for label, r in prev_summaries.items():
            print(f"{label:<14} {r['total']:>6} {r['win_rate']:>7.1f} {r['total_pnl']:>+10.2f} {r['rr']:>7.2f} {r['max_dd']:>10.1f}")
        if result:
            print(f"{'v_full(完整体)':<14} {result['total']:>6} {result['win_rate']:>7.1f} "
                  f"{result['total_pnl']:>+10.2f} {result['rr']:>7.2f} {result['max_dd']:>10.1f}")
    except Exception as e:
        print(f"对比加载失败: {e}")

    # 保存
    if result:
        result_save = {k: v for k, v in result.items() if k != 'trades'}
        result_save['trades'] = trades
        with open("backtest_7m_full_result.json", "w") as f:
            json.dump(result_save, f, ensure_ascii=False, indent=2)
        print(f"\n结果已保存: backtest_7m_full_result.json")
    print(f"[backtest_full] 完成 {dt.datetime.utcnow().strftime('%H:%M UTC')}")
