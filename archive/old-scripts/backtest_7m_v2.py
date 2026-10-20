"""
backtest_7m_v2.py — 优化版回测对比
对比四个版本：
  v_orig  : 原始（无任何优化）
  v_opt1  : 优化1：SL最小2.5%保护
  v_opt2  : 优化2：评分门槛75→78 + SL最小2.5%
  v_opt3  : 优化3：评分75~85取消85+高分单 + SL最小2.5% + 85+分TP收窄到2xATR

所有版本：含大K线冷却过滤，固定100U/单，无仓位上限，TP1全平简化版
"""
import sys, json, time, datetime as dt, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, "/root/.openclaw/workspace")
from entry_logic import rsi_calc, ema_calc, atr_calc

START_TS = int(dt.datetime(2026, 7, 1,  0, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)
END_TS   = int(dt.datetime(2026, 8, 22, 23, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)
POS_USDT = 100.0
DEV_MAX  = 3.0
ORDER_TTL= 4
SL_MULT  = 1.8

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

def score_candle(k1h_window, k4h_window, cfg):
    """
    cfg = {
      spike_filter: bool,
      score_min: int,
      score_max: int (85 to drop high-score),
      sl_min_pct: float,
      tp_mult_normal: float,
      tp_mult_high: float,   # for score>=85
      high_score_threshold: int,
    }
    """
    if len(k1h_window) < 20 or len(k4h_window) < 10:
        return 0, None, 0, 0, 0

    c1h  = [float(k[4]) for k in k1h_window]
    c4h  = [float(k[4]) for k in k4h_window]
    v1h  = [float(k[5]) for k in k1h_window]
    mark = c1h[-1]
    atr14 = atr_calc(k1h_window)
    if atr14 <= 0: return 0, None, 0, 0, 0

    # 大K线冷却
    if cfg.get("spike_filter", True):
        for ki in range(-12, 0):
            if abs(ki) > len(k1h_window): continue
            kl = k1h_window[ki]
            kh, kl2 = float(kl[2]), float(kl[3])
            kr = kh - kl2
            kc, ko = float(kl[4]), float(kl[1])
            range_pct = kr / kl2 * 100 if kl2 > 0 else 0
            kdown_pct = (ko - kc) / ko * 100 if ko > 0 else 0
            if atr14 > 0 and kr > atr14 * 3.5 and (range_pct > 10 or kdown_pct > 8):
                return 0, None, 0, 0, 0

    rsi1h = rsi_calc(c1h); rsi4h = rsi_calc(c4h)
    ema20_1h = ema_calc(c1h[-20:], 20)
    ema50_1h = ema_calc(c1h[-50:], 50) if len(c1h)>=50 else ema_calc(c1h, len(c1h))
    ema20_4h = ema_calc(c4h[-20:], 20) if len(c4h)>=20 else ema_calc(c4h, len(c4h))
    ema50_4h = ema_calc(c4h[-50:], 50) if len(c4h)>=50 else ema_calc(c4h, len(c4h))
    vb = sum(v1h[-20:-3])/17 if len(v1h)>=20 else (sum(v1h)/len(v1h) if v1h else 1)
    vr = (sum(v1h[-3:])/3)/vb if vb > 0 else 1

    lk = k1h_window[-1]
    o2, h2, lw2, c2 = float(lk[1]), float(lk[2]), float(lk[3]), float(lk[4])
    body = abs(c2-o2); lower = min(o2,c2)-lw2
    hammer = lower > body*2 and body > 0
    dir1h = '↑' if c1h[-1]>c1h[-2]>c1h[-3] else ('↓' if c1h[-1]<c1h[-2]<c1h[-3] else '~')
    chg24 = (c1h[-1]-c1h[-25])/c1h[-25]*100 if len(c1h)>=25 else 0

    sma20 = sum(c1h[-20:])/20
    std20 = (sum((x-sma20)**2 for x in c1h[-20:])/20)**0.5
    bb_u = sma20+2*std20; bb_l = sma20-2*std20
    bb_pct = (c1h[-1]-bb_l)/(bb_u-bb_l)*100 if (bb_u-bb_l)>0 else 50

    # 多头评分
    sl = 0
    both_long = ema20_1h>ema50_1h and ema20_4h>ema50_4h
    one_long  = ema20_1h>ema50_1h or  ema20_4h>ema50_4h
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
    if bb_pct>55:      score_l += 10
    if dir1h=='↑':     score_l += 8
    if hammer:         score_l += 7
    if chg24>3:        score_l += 10

    # 空头评分
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
    if bb_pct<45:       score_s += 10
    if dir1h=='↓':      score_s += 8
    if chg24<-3:        score_s += 10

    score_min = cfg.get("score_min", 75)
    score_max = cfg.get("score_max", 999)

    if score_l >= score_min and score_l >= score_s:
        score = score_l; direction = "LONG"
        entry = round(mark - atr14*0.4, 8)
        sl_   = round(entry - atr14*SL_MULT, 8)
        # SL最小距离保护
        sl_min_pct = cfg.get("sl_min_pct", 0)
        actual_sl_dist = (entry - sl_) / entry * 100
        if sl_min_pct > 0 and actual_sl_dist < sl_min_pct:
            return 0, None, 0, 0, 0
        # TP倍数（85+分收窄）
        ht = cfg.get("high_score_threshold", 85)
        tp_mult = cfg.get("tp_mult_high", 2.0) if score >= ht else cfg.get("tp_mult_normal", 3.0)
        tp1 = round(entry + atr14*tp_mult, 8)
    elif score_s >= score_min:
        score = score_s; direction = "SHORT"
        entry = round(mark + atr14*0.4, 8)
        sl_   = round(entry + atr14*SL_MULT, 8)
        sl_min_pct = cfg.get("sl_min_pct", 0)
        actual_sl_dist = (sl_ - entry) / entry * 100
        if sl_min_pct > 0 and actual_sl_dist < sl_min_pct:
            return 0, None, 0, 0, 0
        ht = cfg.get("high_score_threshold", 85)
        tp_mult = cfg.get("tp_mult_high", 2.0) if score >= ht else cfg.get("tp_mult_normal", 3.0)
        tp1 = round(entry - atr14*tp_mult, 8)
    else:
        return 0, None, 0, 0, 0

    if score > score_max:
        return 0, None, 0, 0, 0

    return score, direction, sl_, tp1, entry

def run_backtest(sym_klines_map, cfg, label):
    print(f"\n{'='*60}\n开始回测: {label}\n{'='*60}")
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
            print(f"  [{label}] {bar_idx/len(timeline)*100:.0f}%  {ts_str}  持仓={len(positions)}  成交={len(trades)}")

        for sym, klines in sym_klines_map.items():
            idx = sym_ts_idx[sym].get(ts)
            if idx is None or idx < 1: continue
            cur_k = klines[idx]
            cur_h, cur_l = float(cur_k[2]), float(cur_k[3])

            # 持仓SL/TP
            if sym in positions:
                pos = positions[sym]
                hit_sl = hit_tp = False
                if pos['dir']=='LONG':
                    if cur_l <= pos['sl']:  hit_sl = True
                    if cur_h >= pos['tp1']: hit_tp = True
                else:
                    if cur_h >= pos['sl']:  hit_sl = True
                    if cur_l <= pos['tp1']: hit_tp = True
                if hit_tp or hit_sl:
                    close_px = pos['tp1'] if hit_tp else pos['sl']
                    pnl = ((close_px-pos['entry'])/pos['entry'] if pos['dir']=='LONG'
                           else (pos['entry']-close_px)/pos['entry']) * pos['usdt']
                    trades.append({"sym":sym,"dir":pos['dir'],"entry":pos['entry'],
                        "close":close_px,"sl":pos['sl'],"tp1":pos['tp1'],
                        "usdt":pos['usdt'],"score":pos['score'],
                        "reason":"TP1" if hit_tp else "SL","pnl":round(pnl,4),
                        "open_ts":pos['open_ts'],"close_ts":ts})
                    del positions[sym]; continue

            # 挂单成交/超时
            if sym in pending:
                pend = pending[sym]
                if (ts - pend['placed_ts'])//3600000 >= ORDER_TTL:
                    del pending[sym]; continue
                triggered = ((pend['dir']=='LONG'  and cur_l <= pend['entry']) or
                             (pend['dir']=='SHORT' and cur_h >= pend['entry']))
                if triggered:
                    positions[sym] = {**pend, "open_ts": ts}
                    del pending[sym]; continue

            if sym in positions or sym in pending: continue

            # 评分扫描
            end_i = idx+1
            k1h_w = klines[max(0,end_i-55):end_i]
            if len(k1h_w) < 20: continue
            k1h_4h = klines[max(0,end_i-200):end_i]
            k4h_w = []; tmp = []
            for k in k1h_4h:
                tmp.append(k)
                if len(tmp)==4:
                    h4 = str(max(float(x[2]) for x in tmp))
                    l4 = str(min(float(x[3]) for x in tmp))
                    v4 = str(sum(float(x[5]) for x in tmp))
                    k4h_w.append([tmp[0][0],tmp[0][1],h4,l4,tmp[-1][4],v4]+tmp[-1][6:])
                    tmp = []
            if len(k4h_w) < 10: continue

            score, direction, sl, tp1, entry = score_candle(k1h_w, k4h_w, cfg)
            if score < cfg.get("score_min",75) or direction is None: continue
            if abs(entry-float(cur_k[4]))/float(cur_k[4])*100 > DEV_MAX: continue
            pending[sym] = {"entry":entry,"sl":sl,"tp1":tp1,"dir":direction,
                            "usdt":POS_USDT,"score":score,"placed_ts":ts}

    # 未平仓到期
    for sym, pos in positions.items():
        klines = sym_klines_map.get(sym,[])
        lc = float(klines[-1][4]) if klines else pos['entry']
        pnl = ((lc-pos['entry'])/pos['entry'] if pos['dir']=='LONG'
               else (pos['entry']-lc)/pos['entry']) * pos['usdt']
        trades.append({"sym":sym,"dir":pos['dir'],"entry":pos['entry'],"close":lc,
                       "sl":pos['sl'],"tp1":pos['tp1'],"usdt":pos['usdt'],"score":pos['score'],
                       "reason":"持有到期","pnl":round(pnl,4),
                       "open_ts":pos['open_ts'],"close_ts":END_TS})
    return trades

def report(trades, label):
    if not trades: print(f"[{label}] 无记录"); return None
    wins   = [t for t in trades if t['pnl']>0]
    losses = [t for t in trades if t['pnl']<=0]
    tp = sum(t['pnl'] for t in trades)
    aw = sum(t['pnl'] for t in wins)/len(wins) if wins else 0
    al = abs(sum(t['pnl'] for t in losses)/len(losses)) if losses else 1
    wr = len(wins)/len(trades)*100
    rr = aw/al if al else 0
    equity=1000; peak=1000; mdd=0
    for t in sorted(trades,key=lambda x:x['close_ts']):
        equity+=t['pnl']
        if equity>peak: peak=equity
        dd=(peak-equity)/peak*100
        if dd>mdd: mdd=dd
    longs=[t for t in trades if t['dir']=='LONG']
    shorts=[t for t in trades if t['dir']=='SHORT']
    lw=[t for t in longs if t['pnl']>0]
    sw=[t for t in shorts if t['pnl']>0]
    print(f"\n{'='*60}\n📊 {label}\n{'='*60}")
    print(f"总交易: {len(trades)}  W{len(wins)}/L{len(losses)}  胜率={wr:.1f}%")
    print(f"总盈亏: {tp:+.2f}U  avg_win=+{aw:.2f}U  avg_loss=-{al:.2f}U  RR=1:{rr:.2f}")
    print(f"最大回撤: -{mdd:.1f}%")
    print(f"多头胜率: {len(lw)/len(longs)*100:.1f}% ({len(lw)}/{len(longs)})")
    if shorts: print(f"空头胜率: {len(sw)/len(shorts)*100:.1f}% ({len(sw)}/{len(shorts)})")
    return {"label":label,"total":len(trades),"wins":len(wins),"losses":len(losses),
            "win_rate":wr,"total_pnl":tp,"avg_win":aw,"avg_loss":al,"rr":rr,"max_dd":mdd}

if __name__ == "__main__":
    print(f"[backtest_v2] 开始 {dt.datetime.utcnow().strftime('%H:%M UTC')}")

    # 拉数据
    print("\n[1/6] 拉取历史K线...")
    sym_klines = {}
    def fetch(sym):
        try: return sym, fetch_klines_full(sym, "1h", START_TS, END_TS)
        except: return sym, []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(fetch,s):s for s in SYMBOLS}
        done = 0
        for f in as_completed(futs):
            sym, klines = f.result(); done += 1
            if klines: sym_klines[sym] = klines
            if done % 20 == 0: print(f"  {done}/{len(SYMBOLS)}")
            time.sleep(0.05)
    print(f"  就绪: {len(sym_klines)}个币")

    # 四个版本配置
    configs = [
        ("v_orig",  {"spike_filter":True, "score_min":75,"score_max":999,"sl_min_pct":0,   "tp_mult_normal":3.0,"tp_mult_high":3.0,"high_score_threshold":85}),
        ("v_opt1",  {"spike_filter":True, "score_min":75,"score_max":999,"sl_min_pct":2.5, "tp_mult_normal":3.0,"tp_mult_high":3.0,"high_score_threshold":85}),
        ("v_opt2",  {"spike_filter":True, "score_min":78,"score_max":999,"sl_min_pct":2.5, "tp_mult_normal":3.0,"tp_mult_high":3.0,"high_score_threshold":85}),
        ("v_opt3",  {"spike_filter":True, "score_min":75,"score_max":999,"sl_min_pct":2.5, "tp_mult_normal":3.0,"tp_mult_high":2.0,"high_score_threshold":85}),
    ]

    results = []
    all_trades = {}
    for i, (label, cfg) in enumerate(configs):
        print(f"\n[{i+2}/6] 回测 {label}...")
        trades = run_backtest(sym_klines, cfg, label)
        all_trades[label] = trades
        r = report(trades, label)
        if r: results.append(r)

    # 对比摘要
    print(f"\n{'='*60}")
    print(f"📈 四版本对比摘要")
    print(f"{'='*60}")
    print(f"{'版本':<12} {'总笔数':>7} {'胜率%':>7} {'总盈亏U':>10} {'盈亏比':>7} {'最大回撤%':>10}")
    print(f"{'-'*55}")
    for r in results:
        print(f"{r['label']:<12} {r['total']:>7} {r['win_rate']:>7.1f} {r['total_pnl']:>+10.2f} {r['rr']:>7.2f} {r['max_dd']:>10.1f}")

    # 保存
    with open("backtest_7m_v2_result.json","w") as f:
        json.dump({"summaries":results,
                   "trades":{k:v for k,v in all_trades.items()}},
                  f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存: backtest_7m_v2_result.json")
    print(f"[backtest_v2] 完成 {dt.datetime.utcnow().strftime('%H:%M UTC')}")
