"""
backtest_tp12.py — 固定12% TP回测
配置：
  - 评分门槛75，SL最小2.5%，大K线冷却过滤
  - TP1统一固定12%（不区分评分高低，不用清算图TP）
  - SL用清算图锚定（模拟），但最小2.5%保护
  - 移动止损修复版：TP1触发后从当前价-2xATR开始（不再是ep*1.05）
  - 固定100U/单，无仓位上限，7.1~8.22
"""
import sys, json, time, datetime as dt, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
sys.path.insert(0, "/root/.openclaw/workspace")
from entry_logic import rsi_calc, ema_calc, atr_calc

START_TS = int(dt.datetime(2026, 7,  1,  0, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)
END_TS   = int(dt.datetime(2026, 8, 22, 23, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)
POS_USDT   = 100.0
SCORE_MIN  = 75
SL_MIN_PCT = 2.5
TP_FIXED   = 0.12      # 固定12%
SL_MULT    = 1.8
ORDER_TTL  = 4
DEV_MAX    = 3.0

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
    while cur < end_ms:
        url = (f"https://fapi.binance.com/fapi/v1/klines?symbol={sym}"
               f"&interval={interval}&startTime={cur}&endTime={end_ms}&limit=1500")
        try:
            batch = json.loads(urllib.request.urlopen(url, timeout=15).read())
        except: break
        if not batch: break
        all_k.extend(batch)
        last = int(batch[-1][0])
        if last <= cur: break
        cur = last + 1
        if len(batch) < 1500: break
    seen = set(); res = []
    for k in all_k:
        ts = int(k[0])
        if ts not in seen and ts <= end_ms:
            seen.add(ts); res.append(k)
    res.sort(key=lambda x: int(x[0]))
    return res

def sim_sl(k1h_window, direction, atr14):
    """模拟清算图SL锚定（K线结构）"""
    if len(k1h_window) < 20: return None
    recent = k1h_window[-48:] if len(k1h_window)>=48 else k1h_window
    lows  = [float(k[3]) for k in recent]
    highs = [float(k[2]) for k in recent]
    vols  = [float(k[5]) for k in recent]
    vavg  = sum(vols)/len(vols) if vols else 1
    hvb   = [i for i,v in enumerate(vols) if v > vavg*1.5]
    ob_sup = min(lows[i] for i in hvb) if hvb else min(lows)
    ob_res = max(highs[i] for i in hvb) if hvb else max(highs)
    key_low  = min(float(k[3]) for k in k1h_window[-20:])
    key_high = max(float(k[2]) for k in k1h_window[-20:])
    liq_below = min(ob_sup, key_low)
    liq_above = max(ob_res, key_high)
    if direction == "LONG":
        return round(liq_below - atr14*0.3, 8)
    else:
        return round(liq_above + atr14*0.3, 8)

def score_and_entry(k1h_w, k4h_w):
    if len(k1h_w)<20 or len(k4h_w)<10: return 0,None,0,0,0
    c1h=[float(k[4]) for k in k1h_w]; c4h=[float(k[4]) for k in k4h_w]
    v1h=[float(k[5]) for k in k1h_w]; mark=c1h[-1]
    atr14=atr_calc(k1h_w)
    if atr14<=0: return 0,None,0,0,0

    # 大K线冷却过滤
    for ki in range(-12,0):
        if abs(ki)>len(k1h_w): continue
        kl=k1h_w[ki]; kh,kl2=float(kl[2]),float(kl[3])
        kr=kh-kl2; ko,kc=float(kl[1]),float(kl[4])
        rp=kr/kl2*100 if kl2>0 else 0
        kd=(ko-kc)/ko*100 if ko>0 else 0
        if kr>atr14*3.5 and (rp>10 or kd>8): return 0,None,0,0,0

    rsi1h=rsi_calc(c1h); rsi4h=rsi_calc(c4h)
    ema20_1h=ema_calc(c1h[-20:],20)
    ema50_1h=ema_calc(c1h[-50:],50) if len(c1h)>=50 else ema_calc(c1h,len(c1h))
    ema20_4h=ema_calc(c4h[-20:],20) if len(c4h)>=20 else ema_calc(c4h,len(c4h))
    ema50_4h=ema_calc(c4h[-50:],50) if len(c4h)>=50 else ema_calc(c4h,len(c4h))
    vb=sum(v1h[-20:-3])/17 if len(v1h)>=20 else (sum(v1h)/len(v1h) if v1h else 1)
    vr=(sum(v1h[-3:])/3)/vb if vb>0 else 1
    sma20=sum(c1h[-20:])/20
    std20=(sum((x-sma20)**2 for x in c1h[-20:])/20)**0.5
    bb_u=sma20+2*std20; bb_l=sma20-2*std20
    bb_pct=(c1h[-1]-bb_l)/(bb_u-bb_l)*100 if (bb_u-bb_l)>0 else 50
    lk=k1h_w[-1]; o2,h2,lw2,c2=float(lk[1]),float(lk[2]),float(lk[3]),float(lk[4])
    body=abs(c2-o2); lower=min(o2,c2)-lw2
    hammer=lower>body*2 and body>0
    dir1h='↑' if c1h[-1]>c1h[-2]>c1h[-3] else('↓' if c1h[-1]<c1h[-2]<c1h[-3] else '~')
    chg24=(c1h[-1]-c1h[-25])/c1h[-25]*100 if len(c1h)>=25 else 0

    # 多头评分
    bl=ema20_1h>ema50_1h and ema20_4h>ema50_4h; ol=ema20_1h>ema50_1h or ema20_4h>ema50_4h
    sl=0
    if bl: sl+=30
    elif ol: sl+=15
    if 50<=rsi1h<=65: sl+=15
    elif 40<=rsi1h<50: sl+=8
    if rsi4h>75: sl-=15
    elif rsi4h>68: sl-=8
    if 50<=rsi4h<=65: sl+=10
    elif 40<=rsi4h<50: sl+=5
    if vr>1.5: sl+=20
    elif vr>1.2: sl+=12
    elif vr>1.0: sl+=6
    if bb_pct>55: sl+=10
    if dir1h=='↑': sl+=8
    if hammer: sl+=7
    if chg24>3: sl+=10

    # 空头评分
    bs=ema20_1h<ema50_1h and ema20_4h<ema50_4h; os_=ema20_1h<ema50_1h or ema20_4h<ema50_4h
    ss=0
    if bs: ss+=30
    elif os_: ss+=15
    if 35<=rsi1h<=50: ss+=15
    elif 50<rsi1h<=60: ss+=8
    if rsi4h<25: ss-=15
    elif rsi4h<32: ss-=8
    if 35<=rsi4h<=50: ss+=10
    elif 50<rsi4h<=60: ss+=5
    if vr>1.5: ss+=20
    elif vr>1.2: ss+=12
    elif vr>1.0: ss+=6
    if bb_pct<45: ss+=10
    if dir1h=='↓': ss+=8
    if chg24<-3: ss+=10

    if sl>=SCORE_MIN and sl>=ss: score=sl; direction="LONG"
    elif ss>=SCORE_MIN: score=ss; direction="SHORT"
    else: return 0,None,0,0,0

    # 入场价
    entry = round(mark-atr14*0.4,8) if direction=="LONG" else round(mark+atr14*0.4,8)

    # SL：清算图模拟锚定，最小2.5%保护
    sl_raw = sim_sl(k1h_w, direction, atr14)
    if sl_raw is None:
        sl_raw = round(entry-atr14*SL_MULT,8) if direction=="LONG" else round(entry+atr14*SL_MULT,8)
    sl_dist = abs(entry-sl_raw)/entry*100
    if sl_dist < SL_MIN_PCT:
        sl_raw = round(entry*(1-SL_MIN_PCT/100),8) if direction=="LONG" else round(entry*(1+SL_MIN_PCT/100),8)

    # TP1：固定12%
    tp1 = round(entry*(1+TP_FIXED),8) if direction=="LONG" else round(entry*(1-TP_FIXED),8)

    return score, direction, sl_raw, tp1, entry


def run_backtest(sym_klines_map):
    print(f"\n{'='*60}\n开始回测: 固定12%TP\n{'='*60}")
    trades=[]; pending={}; positions={}
    all_ts=set()
    for klines in sym_klines_map.values():
        for k in klines: all_ts.add(int(k[0]))
    timeline=sorted(all_ts)
    sym_ts_idx={sym:{int(k[0]):i for i,k in enumerate(klines)}
                for sym,klines in sym_klines_map.items()}
    step=max(1,len(timeline)//20)

    for bar_idx,ts in enumerate(timeline):
        if bar_idx%step==0:
            ts_str=dt.datetime.utcfromtimestamp(ts/1000).strftime("%m-%d %H:%M")
            print(f"  {bar_idx/len(timeline)*100:.0f}%  {ts_str}  持仓={len(positions)}  成交={len(trades)}")

        for sym,klines in sym_klines_map.items():
            idx=sym_ts_idx[sym].get(ts)
            if idx is None or idx<1: continue
            cur_k=klines[idx]
            cur_h,cur_l,cur_c=float(cur_k[2]),float(cur_k[3]),float(cur_k[4])

            # 持仓SL/TP + 移动止损
            if sym in positions:
                pos=positions[sym]
                ep=pos['entry']; dir_=pos['dir']
                trail=pos.get('trailing_sl')

                # 移动止损阶段（TP1触发后）
                if trail is not None:
                    if dir_=='LONG':
                        atr_t=pos.get('atr_init',ep*0.02)
                        new_t=round(cur_l-atr_t*0.5,8)   # 跟随最低价-0.5xATR
                        if new_t>trail:
                            positions[sym]['trailing_sl']=new_t; trail=new_t
                        if cur_l<=trail:
                            pnl=(trail-ep)/ep*pos['usdt']
                            trades.append({**pos,"close":trail,"reason":"移动止损","pnl":round(pnl,4),"close_ts":ts})
                            del positions[sym]; continue
                    else:
                        atr_t=pos.get('atr_init',ep*0.02)
                        new_t=round(cur_h+atr_t*0.5,8)
                        if new_t<trail:
                            positions[sym]['trailing_sl']=new_t; trail=new_t
                        if cur_h>=trail:
                            pnl=(ep-trail)/ep*pos['usdt']
                            trades.append({**pos,"close":trail,"reason":"移动止损","pnl":round(pnl,4),"close_ts":ts})
                            del positions[sym]; continue
                    continue

                # 常规SL/TP
                hit_sl=hit_tp=False
                if dir_=='LONG':
                    if cur_l<=pos['sl']:  hit_sl=True
                    if cur_h>=pos['tp1']: hit_tp=True
                else:
                    if cur_h>=pos['sl']:  hit_sl=True
                    if cur_l<=pos['tp1']: hit_tp=True

                if hit_tp:
                    # TP1触发：平50%，剩余启动移动止损（从当前价-2xATR开始）
                    atr_now=pos.get('atr_init', abs(ep-pos['sl'])/SL_MULT)
                    half_pnl=((pos['tp1']-ep)/ep if dir_=='LONG' else (ep-pos['tp1'])/ep)*pos['usdt']*0.5
                    trades.append({**pos,"close":pos['tp1'],"reason":"TP1半仓","pnl":round(half_pnl,4),"close_ts":ts})
                    if dir_=='LONG':
                        trail_init=round(cur_h-atr_now*2.0,8)  # 当前高点-2xATR（修复版）
                    else:
                        trail_init=round(cur_l+atr_now*2.0,8)  # 当前低点+2xATR（修复版）
                    positions[sym]={**pos,'usdt':pos['usdt']*0.5,'trailing_sl':trail_init,'atr_init':atr_now}
                    continue
                elif hit_sl:
                    close_px=pos['sl']
                    pnl=((close_px-ep)/ep if dir_=='LONG' else (ep-close_px)/ep)*pos['usdt']
                    trades.append({**pos,"close":close_px,"reason":"SL","pnl":round(pnl,4),"close_ts":ts})
                    del positions[sym]; continue

            # 挂单检查
            if sym in pending:
                pend=pending[sym]
                if (ts-pend['placed_ts'])//3600000>=ORDER_TTL:
                    del pending[sym]; continue
                triggered=((pend['dir']=='LONG' and cur_l<=pend['entry']) or
                           (pend['dir']=='SHORT' and cur_h>=pend['entry']))
                if triggered:
                    positions[sym]={**pend,'open_ts':ts}
                    del pending[sym]; continue

            if sym in positions or sym in pending: continue

            # 扫描信号
            end_i=idx+1
            k1h_w=klines[max(0,end_i-55):end_i]
            if len(k1h_w)<20: continue
            k1h_4h=klines[max(0,end_i-200):end_i]
            k4h_w=[]; tmp=[]
            for k in k1h_4h:
                tmp.append(k)
                if len(tmp)==4:
                    k4h_w.append([tmp[0][0],tmp[0][1],
                                  str(max(float(x[2]) for x in tmp)),
                                  str(min(float(x[3]) for x in tmp)),
                                  tmp[-1][4],str(sum(float(x[5]) for x in tmp))]+tmp[-1][6:])
                    tmp=[]
            if len(k4h_w)<10: continue

            score,direction,sl,tp1,entry=score_and_entry(k1h_w,k4h_w)
            if score<SCORE_MIN or direction is None: continue
            if abs(entry-cur_c)/cur_c*100>DEV_MAX: continue
            atr_now=atr_calc(k1h_w)
            pending[sym]={'entry':entry,'sl':sl,'tp1':tp1,'dir':direction,
                         'usdt':POS_USDT,'score':score,'placed_ts':ts,'atr_init':atr_now}

    # 未平仓到期
    for sym,pos in positions.items():
        klines=sym_klines_map.get(sym,[])
        lc=float(klines[-1][4]) if klines else pos['entry']
        pnl=((lc-pos['entry'])/pos['entry'] if pos['dir']=='LONG'
             else (pos['entry']-lc)/pos['entry'])*pos['usdt']
        trades.append({**pos,"close":lc,"reason":"持有到期","pnl":round(pnl,4),"close_ts":END_TS})
    return trades


def report(trades):
    import collections, statistics
    if not trades: print("无记录"); return

    # 合并同一笔单的TP1半仓和移动止损（简化：直接统计所有子记录）
    wins=[t for t in trades if t['pnl']>0]
    losses=[t for t in trades if t['pnl']<=0]
    tp=sum(t['pnl'] for t in trades)
    aw=sum(t['pnl'] for t in wins)/len(wins) if wins else 0
    al=abs(sum(t['pnl'] for t in losses)/len(losses)) if losses else 1
    wr=len(wins)/len(trades)*100; rr=aw/al if al else 0

    equity=1000; peak=1000; mdd=0
    monthly=collections.defaultdict(float)
    for t in sorted(trades,key=lambda x:x['close_ts']):
        equity+=t['pnl']
        if equity>peak: peak=equity
        dd=(peak-equity)/peak*100
        if dd>mdd: mdd=dd
        mo=dt.datetime.utcfromtimestamp(t['close_ts']/1000).strftime("%Y-%m")
        monthly[mo]+=t['pnl']

    longs=[t for t in trades if t['dir']=='LONG']
    shorts=[t for t in trades if t['dir']=='SHORT']
    lw=[t for t in longs if t['pnl']>0]
    sw=[t for t in shorts if t['pnl']>0]

    print(f"\n{'='*65}")
    print(f"📊 固定12%TP 回测报告 | 7.1~8.22 | 99个币")
    print(f"{'='*65}")
    print(f"总记录:    {len(trades)}条  (含TP1半仓+移动止损独立记录)")
    print(f"胜率:      {wr:.1f}%  W{len(wins)}/L{len(losses)}")
    print(f"总盈亏:    {tp:+.2f}U   (每单100U本金)")
    print(f"均胜:     +{aw:.2f}U   均亏: -{al:.2f}U   盈亏比: 1:{rr:.2f}")
    print(f"最大回撤:  -{mdd:.1f}%   终态净值: {1000+tp:.0f}U")
    print(f"多头胜率:  {len(lw)/len(longs)*100:.1f}%  ({len(lw)}/{len(longs)})")
    if shorts: print(f"空头胜率:  {len(sw)/len(shorts)*100:.1f}%  ({len(sw)}/{len(shorts)})")

    print(f"\n月度盈亏:")
    for mo,p in sorted(monthly.items()):
        grp=[t for t in trades if dt.datetime.utcfromtimestamp(t['close_ts']/1000).strftime("%Y-%m")==mo]
        gw=[t for t in grp if t['pnl']>0]
        print(f"  {mo}  {p:+.2f}U   {len(grp)}条  胜率={len(gw)/len(grp)*100:.1f}%")

    print(f"\n评分分档:")
    for lo,hi in [(75,80),(80,85),(85,90),(90,200)]:
        grp=[t for t in trades if lo<=t['score']<hi]
        if not grp: continue
        gw=[t for t in grp if t['pnl']>0]
        gp=sum(t['pnl'] for t in grp)
        print(f"  [{lo}-{hi})  n={len(grp):4}  胜率={len(gw)/len(grp)*100:.1f}%  总盈亏={gp:+.1f}U  avg={gp/len(grp):+.2f}U")

    print(f"\n平仓原因:")
    rc=collections.Counter(t['reason'] for t in trades)
    for r,n in rc.most_common():
        grp=[t for t in trades if t['reason']==r]
        avg=sum(t['pnl'] for t in grp)/n
        print(f"  {r:<12} n={n:4}  avg={avg:+.2f}U")

    print(f"\n盈利Top10:")
    for t in sorted(trades,key=lambda x:-x['pnl'])[:10]:
        od=dt.datetime.utcfromtimestamp(t['open_ts']/1000).strftime("%m-%d") if 'open_ts' in t else '?'
        cd=dt.datetime.utcfromtimestamp(t['close_ts']/1000).strftime("%m-%d")
        print(f"  {t['sym']:<14} {t['dir']}  {od}→{cd}  {t['pnl']:+.2f}U  {t['reason']}")

    print(f"\n亏损Top10:")
    for t in sorted(trades,key=lambda x:x['pnl'])[:10]:
        od=dt.datetime.utcfromtimestamp(t.get('open_ts',t['close_ts'])/1000).strftime("%m-%d")
        cd=dt.datetime.utcfromtimestamp(t['close_ts']/1000).strftime("%m-%d")
        print(f"  {t['sym']:<14} {t['dir']}  {od}→{cd}  {t['pnl']:+.2f}U  {t['reason']}")

    # 和之前版本对比
    print(f"\n{'='*65}")
    print(f"对比之前版本（每单100U）:")
    print(f"  v_orig（无优化）     2218笔  胜率37.1%  +165U  RR=1.78  回撤-28.1%")
    print(f"  v_opt1（SL≥2.5%）    810笔  胜率40.0%  +301U  RR=1.73  回撤-15.9%")
    print(f"  v_opt3（SL+TP收窄）  837笔  胜率42.5%  +286U  RR=1.54  回撤-17.1%")
    print(f"  v_full（清算图）    1189笔  胜率62.6%  +135U  RR=0.62  回撤-31.6%")
    print(f"  v_tp12（本次）       ---笔  胜率--.--%  {tp:+.0f}U  RR={rr:.2f}  回撤-{mdd:.1f}%")

if __name__=="__main__":
    print(f"[backtest_tp12] 开始 {dt.datetime.utcnow().strftime('%H:%M UTC')}")
    print(f"配置: 评分≥{SCORE_MIN}  SL≥{SL_MIN_PCT}%  TP固定{TP_FIXED*100:.0f}%  大K线冷却✅  移动止损修复版✅")

    print(f"\n[1/3] 拉K线数据...")
    sym_klines={}
    def fetch(sym):
        try: return sym,fetch_klines_full(sym,"1h",START_TS,END_TS)
        except: return sym,[]
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs={ex.submit(fetch,s):s for s in SYMBOLS}
        done=0
        for f in as_completed(futs):
            sym,klines=f.result(); done+=1
            if klines: sym_klines[sym]=klines
            if done%20==0: print(f"  {done}/{len(SYMBOLS)}")
            time.sleep(0.05)
    print(f"  就绪: {len(sym_klines)}个币")

    print(f"\n[2/3] 回测...")
    trades=run_backtest(sym_klines)

    print(f"\n[3/3] 报告...")
    report(trades)

    with open("backtest_tp12_result.json","w") as f:
        json.dump({"trades":trades},f,ensure_ascii=False,indent=2)
    print(f"\n已保存: backtest_tp12_result.json")
    print(f"[backtest_tp12] 完成 {dt.datetime.utcnow().strftime('%H:%M UTC')}")
