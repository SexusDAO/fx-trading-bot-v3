"""
backtest_7m.py — 7.1~8.22 历史回测
- 100个热门币种
- 逐小时回放，固定100U/单，无仓位数量限制
- TP1触发全平（简化版）
- 跑两遍：v_orig（无大K线冷却过滤）vs v_new（含大K线冷却过滤）
- 输出对比报告
"""
import sys, json, time, datetime as dt, math, traceback, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, "/root/.openclaw/workspace")
import binance_api as api
from entry_logic import rsi_calc, ema_calc, atr_calc

# ── 参数 ──────────────────────────────────────────────────────────────────────
START_TS   = int(dt.datetime(2026, 7, 1,  0, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)
END_TS     = int(dt.datetime(2026, 8, 22, 23, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)
INTERVAL   = "1h"
POS_USDT   = 100.0          # 固定每单100U
SCORE_GATE = 75             # 开单门槛
DEV_MAX    = 3.0            # 挂单偏差容忍 ±3%
ORDER_TTL  = 4              # 挂单有效期（小时）
SL_MULT    = 1.8            # 止损 ATR 倍数
TP_MULT    = 3.0            # 止盈 ATR 倍数（TP1全平）

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
    "MELANIAUSDT","MUUSDT","PUMPUSDT","XAUUSDT","XAGUSDT","BZUSDT","DASHUSDT",
    "MVLLUSDT","ZECUSDT",
]

# ── K线拉取 ───────────────────────────────────────────────────────────────────
def fetch_klines_full(sym, interval, start_ms, end_ms):
    """分批拉取完整历史K线（每次最多1500根）"""
    all_k = []
    cur = start_ms
    base = "https://fapi.binance.com"
    while cur < end_ms:
        url = (f"{base}/fapi/v1/klines?symbol={sym}&interval={interval}"
               f"&startTime={cur}&endTime={end_ms}&limit=1500")
        try:
            raw = urllib.request.urlopen(url, timeout=15).read()
            batch = json.loads(raw)
        except Exception as e:
            print(f"  [fetch] {sym} error: {e}")
            break
        if not batch:
            break
        all_k.extend(batch)
        last_ts = int(batch[-1][0])
        if last_ts <= cur:
            break
        cur = last_ts + 1
        if len(batch) < 1500:
            break
    # 去重+排序
    seen = set()
    result = []
    for k in all_k:
        ts = int(k[0])
        if ts not in seen and ts <= end_ms:
            seen.add(ts)
            result.append(k)
    result.sort(key=lambda x: int(x[0]))
    return result


# ── 评分逻辑（内联，避免依赖entry_logic的funding参数问题）────────────────────
def score_candle(k1h_window, k4h_window, funding=0.01, use_spike_filter=True):
    """
    简化版评分，和entry_logic.calc_score_trend对齐核心逻辑。
    k1h_window: 最近50根1H K线（list of kline）
    k4h_window: 最近50根4H K线
    返回 (score, direction, sl_price, tp1_price, entry_price)
    direction: 'LONG' or 'SHORT' or None
    """
    if len(k1h_window) < 20 or len(k4h_window) < 10:
        return 0, None, 0, 0, 0

    c1h = [float(k[4]) for k in k1h_window]
    c4h = [float(k[4]) for k in k4h_window]
    v1h = [float(k[5]) for k in k1h_window]
    mark = c1h[-1]

    atr14 = atr_calc(k1h_window)
    if atr14 <= 0:
        return 0, None, 0, 0, 0

    # ── 大K线冷却过滤（v_new专用）────────────────────────────────────────────
    if use_spike_filter:
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

    rsi1h = rsi_calc(c1h)
    rsi4h = rsi_calc(c4h)
    ema20_1h = ema_calc(c1h[-20:], 20)
    ema50_1h = ema_calc(c1h[-50:], 50) if len(c1h) >= 50 else ema_calc(c1h, len(c1h))
    ema20_4h = ema_calc(c4h[-20:], 20) if len(c4h) >= 20 else ema_calc(c4h, len(c4h))
    ema50_4h = ema_calc(c4h[-50:], 50) if len(c4h) >= 50 else ema_calc(c4h, len(c4h))

    vb  = sum(v1h[-20:-3]) / 17 if len(v1h) >= 20 else (sum(v1h) / len(v1h) if v1h else 1)
    vr  = (sum(v1h[-3:]) / 3) / vb if vb > 0 else 1

    # 方向判断
    trend_long  = ema20_1h > ema50_1h and ema20_4h > ema50_4h and mark > ema20_1h
    trend_short = ema20_1h < ema50_1h and ema20_4h < ema50_4h and mark < ema20_1h

    lk = k1h_window[-1]
    o, h, lw, c = float(lk[1]), float(lk[2]), float(lk[3]), float(lk[4])
    body  = abs(c - o)
    lower = min(o, c) - lw
    hammer = lower > body * 2 and body > 0
    dir1h = '↑' if c1h[-1] > c1h[-2] > c1h[-3] else ('↓' if c1h[-1] < c1h[-2] < c1h[-3] else '~')

    chg24_approx = (c1h[-1] - c1h[-25]) / c1h[-25] * 100 if len(c1h) >= 25 else 0

    # ── 多头评分 ─────────────────────────────────────────────────────────────
    score_long = 0
    both_long = ema20_1h > ema50_1h and ema20_4h > ema50_4h
    one_long  = ema20_1h > ema50_1h or  ema20_4h > ema50_4h
    if both_long:  score_long += 30
    elif one_long: score_long += 15

    if 50 <= rsi1h <= 65:   score_long += 15
    elif 40 <= rsi1h < 50:  score_long += 8
    if rsi4h > 75:          score_long -= 15
    elif rsi4h > 68:        score_long -= 8
    if 50 <= rsi4h <= 65:   score_long += 10
    elif 40 <= rsi4h < 50:  score_long += 5

    if vr > 1.5:   score_long += 20
    elif vr > 1.2: score_long += 12
    elif vr > 1.0: score_long += 6

    sma20 = sum(c1h[-20:]) / 20
    std20 = (sum((x - sma20)**2 for x in c1h[-20:]) / 20)**0.5
    bb_u = sma20 + 2*std20; bb_l = sma20 - 2*std20
    bb_pct = (c1h[-1] - bb_l) / (bb_u - bb_l) * 100 if (bb_u - bb_l) > 0 else 50
    if bb_pct > 55:     score_long += 10
    if dir1h == '↑':    score_long += 8
    if hammer:          score_long += 7
    if chg24_approx > 3: score_long += 10
    if funding < -0.1:  score_long += 5

    # ── 空头评分 ─────────────────────────────────────────────────────────────
    score_short = 0
    both_short = ema20_1h < ema50_1h and ema20_4h < ema50_4h
    one_short  = ema20_1h < ema50_1h or  ema20_4h < ema50_4h
    if both_short:  score_short += 30
    elif one_short: score_short += 15

    if 35 <= rsi1h <= 50:   score_short += 15
    elif 50 < rsi1h <= 60:  score_short += 8
    if rsi4h < 25:          score_short -= 15
    elif rsi4h < 32:        score_short -= 8
    if 35 <= rsi4h <= 50:   score_short += 10
    elif 50 < rsi4h <= 60:  score_short += 5

    if vr > 1.5:   score_short += 20
    elif vr > 1.2: score_short += 12
    elif vr > 1.0: score_short += 6

    if bb_pct < 45:      score_short += 10
    if dir1h == '↓':     score_short += 8
    if chg24_approx < -3: score_short += 10
    if funding > 0.1:    score_short += 5

    # 选择方向
    if score_long >= SCORE_GATE and score_long >= score_short:
        direction = "LONG"
        score = score_long
        entry  = round(mark - atr14 * 0.4, 8)
        sl     = round(entry - atr14 * SL_MULT, 8)
        tp1    = round(entry + atr14 * TP_MULT, 8)
    elif score_short >= SCORE_GATE:
        direction = "SHORT"
        score = score_short
        entry  = round(mark + atr14 * 0.4, 8)
        sl     = round(entry + atr14 * SL_MULT, 8)
        tp1    = round(entry - atr14 * TP_MULT, 8)
    else:
        return 0, None, 0, 0, 0

    return score, direction, sl, tp1, entry


# ── 回测引擎 ─────────────────────────────────────────────────────────────────
def run_backtest(sym_klines_map, use_spike_filter, label):
    """
    sym_klines_map: {sym: [kline, ...]}  已按时间排序的1H K线
    """
    print(f"\n{'='*60}")
    print(f"开始回测: {label}  spike_filter={use_spike_filter}")
    print(f"{'='*60}")

    trades  = []   # 已平仓交易
    pending = {}   # sym -> {entry, sl, tp1, dir, usdt, open_bar, score}
    positions = {} # sym -> {entry, sl, tp1, dir, usdt, open_bar, score}

    # 建立全局时间轴（所有币种union时间戳）
    all_ts = set()
    for klines in sym_klines_map.values():
        for k in klines:
            all_ts.add(int(k[0]))
    timeline = sorted(all_ts)

    # 每个币建立时间戳->index的快速查找
    sym_ts_idx = {}
    for sym, klines in sym_klines_map.items():
        sym_ts_idx[sym] = {int(k[0]): i for i, k in enumerate(klines)}

    bar_count = 0
    progress_step = max(1, len(timeline) // 20)

    for bar_idx, ts in enumerate(timeline):
        bar_count += 1
        if bar_idx % progress_step == 0:
            pct = bar_idx / len(timeline) * 100
            ts_str = dt.datetime.utcfromtimestamp(ts/1000).strftime("%m-%d %H:%M")
            print(f"  [{label}] {pct:.0f}%  {ts_str}  持仓={len(positions)}  挂单={len(pending)}  已成交={len(trades)}")

        for sym, klines in sym_klines_map.items():
            idx = sym_ts_idx[sym].get(ts)
            if idx is None or idx < 1:
                continue

            cur_k = klines[idx]
            cur_h = float(cur_k[2])
            cur_l = float(cur_k[3])
            cur_c = float(cur_k[4])
            cur_o = float(cur_k[1])

            # ── 1. 检查持仓SL/TP ─────────────────────────────────────────────
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
                        "sym":    sym,
                        "dir":    pos['dir'],
                        "entry":  pos['entry'],
                        "close":  close_px,
                        "sl":     pos['sl'],
                        "tp1":    pos['tp1'],
                        "usdt":   pos['usdt'],
                        "score":  pos['score'],
                        "reason": reason,
                        "pnl":    round(pnl, 4),
                        "open_ts":  pos['open_ts'],
                        "close_ts": ts,
                        "open_dt":  dt.datetime.utcfromtimestamp(pos['open_ts']/1000).strftime("%m-%d %H:%M"),
                        "close_dt": dt.datetime.utcfromtimestamp(ts/1000).strftime("%m-%d %H:%M"),
                    })
                    del positions[sym]
                    continue

            # ── 2. 检查挂单成交 ───────────────────────────────────────────────
            if sym in pending:
                pend = pending[sym]
                # 挂单有效期检查
                bars_elapsed = (ts - pend['placed_ts']) // 3600000
                if bars_elapsed >= ORDER_TTL:
                    del pending[sym]
                    continue
                # 成交判断：限价多单 cur_l<=entry，限价空单 cur_h>=entry
                triggered = False
                if pend['dir'] == 'LONG'  and cur_l <= pend['entry']: triggered = True
                if pend['dir'] == 'SHORT' and cur_h >= pend['entry']: triggered = True
                if triggered:
                    positions[sym] = {
                        "entry":    pend['entry'],
                        "sl":       pend['sl'],
                        "tp1":      pend['tp1'],
                        "dir":      pend['dir'],
                        "usdt":     pend['usdt'],
                        "score":    pend['score'],
                        "open_ts":  ts,
                    }
                    del pending[sym]
                    continue

            # ── 3. 扫描新信号（每根K线结束后） ──────────────────────────────
            if sym in positions or sym in pending:
                continue  # 已有仓位或挂单，不重复

            # 准备窗口数据（1H用当前及之前50根，4H用合并方式取近50根4H K线）
            win1h_end = idx + 1
            win1h_start = max(0, win1h_end - 55)
            k1h_win = klines[win1h_start:win1h_end]
            if len(k1h_win) < 20:
                continue

            # 4H窗口：从1H K线合并
            win4h_start = max(0, win1h_end - 200)
            k1h_for_4h  = klines[win4h_start:win1h_end]
            # 把1H K线合并成4H（每4根合一根）
            k4h_win = []
            tmp = []
            for k in k1h_for_4h:
                tmp.append(k)
                if len(tmp) == 4:
                    o4 = tmp[0][1]; h4 = str(max(float(x[2]) for x in tmp))
                    l4 = str(min(float(x[3]) for x in tmp)); c4 = tmp[-1][4]
                    v4 = str(sum(float(x[5]) for x in tmp))
                    k4h_win.append([tmp[0][0], o4, h4, l4, c4, v4] + tmp[-1][6:])
                    tmp = []
            if len(k4h_win) < 10:
                continue

            score, direction, sl, tp1, entry = score_candle(
                k1h_win, k4h_win, funding=0.01,
                use_spike_filter=use_spike_filter
            )
            if score < SCORE_GATE or direction is None:
                continue

            # 偏差检查
            dev = abs(entry - cur_c) / cur_c * 100
            if dev > DEV_MAX:
                continue

            pending[sym] = {
                "entry":     entry,
                "sl":        sl,
                "tp1":       tp1,
                "dir":       direction,
                "usdt":      POS_USDT,
                "score":     score,
                "placed_ts": ts,
            }

    # 未平仓视为持仓到最后一根K线按收盘价平
    last_ts = timeline[-1] if timeline else 0
    for sym, pos in positions.items():
        klines = sym_klines_map.get(sym, [])
        last_c = float(klines[-1][4]) if klines else pos['entry']
        if pos['dir'] == 'LONG':
            pnl = (last_c - pos['entry']) / pos['entry'] * pos['usdt']
        else:
            pnl = (pos['entry'] - last_c) / pos['entry'] * pos['usdt']
        trades.append({
            "sym":    sym, "dir": pos['dir'],
            "entry":  pos['entry'], "close": last_c,
            "sl":     pos['sl'],    "tp1":   pos['tp1'],
            "usdt":   pos['usdt'],  "score": pos['score'],
            "reason": "持有到期", "pnl": round(pnl, 4),
            "open_ts": pos['open_ts'], "close_ts": last_ts,
            "open_dt":  dt.datetime.utcfromtimestamp(pos['open_ts']/1000).strftime("%m-%d %H:%M"),
            "close_dt": "end",
        })

    return trades


# ── 报告生成 ──────────────────────────────────────────────────────────────────
def print_report(trades, label):
    if not trades:
        print(f"\n[{label}] 无交易记录")
        return
    wins   = [t for t in trades if t['pnl'] > 0]
    losses = [t for t in trades if t['pnl'] <= 0]
    total_pnl = sum(t['pnl'] for t in trades)
    win_pnl   = sum(t['pnl'] for t in wins)
    loss_pnl  = sum(t['pnl'] for t in losses)
    win_rate  = len(wins) / len(trades) * 100

    # 盈亏比
    avg_win  = win_pnl  / len(wins)   if wins   else 0
    avg_loss = abs(loss_pnl / len(losses)) if losses else 1
    rr_ratio = avg_win / avg_loss if avg_loss > 0 else 0

    # 最大回撤
    equity = 1000.0
    peak = equity
    max_dd = 0
    for t in sorted(trades, key=lambda x: x['close_ts']):
        equity += t['pnl']
        if equity > peak: peak = equity
        dd = (peak - equity) / peak * 100
        if dd > max_dd: max_dd = dd

    # 按方向分
    longs  = [t for t in trades if t['dir'] == 'LONG']
    shorts = [t for t in trades if t['dir'] == 'SHORT']
    l_wins = [t for t in longs  if t['pnl'] > 0]
    s_wins = [t for t in shorts if t['pnl'] > 0]

    print(f"\n{'='*60}")
    print(f"📊 {label} 回测报告")
    print(f"{'='*60}")
    print(f"总交易:    {len(trades)}笔   多={len(longs)}  空={len(shorts)}")
    print(f"胜率:      {win_rate:.1f}%   W{len(wins)}/L{len(losses)}")
    print(f"总盈亏:    {total_pnl:+.2f}U   (基于每单100U)")
    print(f"盈利单均值: +{avg_win:.2f}U   亏损单均值: -{avg_loss:.2f}U")
    print(f"盈亏比:    1:{rr_ratio:.2f}")
    print(f"最大回撤:  -{max_dd:.1f}%")
    print(f"多头胜率:  {len(l_wins)/len(longs)*100:.1f}%  ({len(l_wins)}/{len(longs)})")
    if shorts:
        print(f"空头胜率:  {len(s_wins)/len(shorts)*100:.1f}%  ({len(s_wins)}/{len(shorts)})")

    # Top 10 盈利 / Top 10 亏损
    print(f"\n--- Top10 盈利 ---")
    for t in sorted(trades, key=lambda x: -x['pnl'])[:10]:
        print(f"  {t['sym']:<14} {t['dir']}  {t['open_dt']}→{t['close_dt']}  {t['pnl']:+.2f}U  {t['reason']}")
    print(f"\n--- Top10 亏损 ---")
    for t in sorted(trades, key=lambda x: x['pnl'])[:10]:
        print(f"  {t['sym']:<14} {t['dir']}  {t['open_dt']}→{t['close_dt']}  {t['pnl']:+.2f}U  {t['reason']}")

    return {
        "label": label, "total": len(trades), "wins": len(wins), "losses": len(losses),
        "win_rate": win_rate, "total_pnl": total_pnl,
        "avg_win": avg_win, "avg_loss": avg_loss, "rr": rr_ratio, "max_dd": max_dd,
        "trades": trades
    }


# ── 主流程 ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"[backtest_7m] 开始  {dt.datetime.utcnow().strftime('%H:%M UTC')}")
    print(f"时间段: 2026-07-01 ~ 2026-08-22  |  {len(SYMBOLS)}个币种")

    # 1. 拉K线数据（并发）
    print(f"\n[1/4] 拉取历史K线数据...")
    sym_klines = {}

    def fetch(sym):
        try:
            klines = fetch_klines_full(sym, INTERVAL, START_TS, END_TS)
            return sym, klines
        except Exception as e:
            print(f"  {sym} 拉取失败: {e}")
            return sym, []

    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(fetch, s): s for s in SYMBOLS}
        done = 0
        for f in as_completed(futs):
            sym, klines = f.result()
            done += 1
            if klines:
                sym_klines[sym] = klines
                if done % 10 == 0:
                    print(f"  已拉取 {done}/{len(SYMBOLS)}  {sym} {len(klines)}根")
            else:
                print(f"  {sym} 无数据，跳过")
            time.sleep(0.05)  # 避免rate limit

    print(f"  数据就绪: {len(sym_klines)}个币  耗时完成")

    # 2. 跑原始版（无大K线冷却）
    print(f"\n[2/4] 回测原始版（无大K线冷却过滤）...")
    trades_orig = run_backtest(sym_klines, use_spike_filter=False, label="v_orig")

    # 3. 跑当前版（含大K线冷却）
    print(f"\n[3/4] 回测当前版（含大K线冷却过滤）...")
    trades_new  = run_backtest(sym_klines, use_spike_filter=True, label="v_new")

    # 4. 输出报告
    print(f"\n[4/4] 生成报告...")
    r_orig = print_report(trades_orig, "v_orig（原始版）")
    r_new  = print_report(trades_new,  "v_new（含大K线冷却）")

    # 对比摘要
    print(f"\n{'='*60}")
    print(f"📈 对比摘要")
    print(f"{'='*60}")
    if r_orig and r_new:
        print(f"{'指标':<16} {'v_orig':>12} {'v_new':>12} {'变化':>12}")
        print(f"{'-'*52}")
        metrics = [
            ("总交易笔数", "total", "{:.0f}"),
            ("胜率%",      "win_rate", "{:.1f}"),
            ("总盈亏U",    "total_pnl", "{:+.2f}"),
            ("盈亏比",     "rr", "{:.2f}"),
            ("最大回撤%",  "max_dd", "{:.1f}"),
        ]
        for name, key, fmt in metrics:
            v1 = r_orig[key]; v2 = r_new[key]
            delta = v2 - v1
            print(f"{name:<16} {fmt.format(v1):>12} {fmt.format(v2):>12} {('+' if delta>=0 else '')+fmt.format(delta):>12}")

    # 保存JSON
    out = {"orig": r_orig, "new": r_new} if r_orig and r_new else {}
    if out:
        out['orig'].pop('trades', None)
        out['new'].pop('trades', None)
    with open("backtest_7m_result.json", "w") as f:
        json.dump({
            "orig_summary": {k:v for k,v in (r_orig or {}).items() if k!='trades'},
            "new_summary":  {k:v for k,v in (r_new  or {}).items() if k!='trades'},
            "orig_trades":  trades_orig,
            "new_trades":   trades_new,
        }, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存: backtest_7m_result.json")
    print(f"[backtest_7m] 完成  {dt.datetime.utcnow().strftime('%H:%M UTC')}")
