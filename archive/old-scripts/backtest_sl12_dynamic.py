"""
backtest_sl12_dynamic.py — 固定12%SL + 动态仓位（按评分分档）
仓位规则：
  75~80分  →  50U
  80~85分  →  100U
  85~90分  →  150U
  90+分    →  200U
其他配置和sl12完全一样（TP正常清算图+ATR，大K线冷却，移动止损修复版）
"""
import sys, json, time, datetime as dt, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
sys.path.insert(0, "/root/.openclaw/workspace")
from entry_logic import rsi_calc, ema_calc, atr_calc

START_TS = int(dt.datetime(2026, 7,  1,  0, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)
END_TS   = int(dt.datetime(2026, 8, 22, 23, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)
SCORE_MIN  = 75
SL_FIXED   = 0.12
SL_MULT    = 1.8
ORDER_TTL  = 4
DEV_MAX    = 3.0

def pos_size(score):
    """动态仓位：评分越高仓位越大"""
    if score >= 90: return 200.0
    if score >= 85: return 150.0
    if score >= 80: return 100.0
    return 50.0

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
        try: batch = json.loads(urllib.request.urlopen(url, timeout=15).read())
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

def sim_sl_tp(k1h_window, direction, atr14):
    if len(k1h_window) < 20: return 0
    recent = k1h_window[-48:] if len(k1h_window)>=48 else k1h_window
    highs = [float(k[2]) for k in recent]; lows = [float(k[3]) for k in recent]
    vols  = [float(k[5]) for k in recent]
    vavg  = sum(vols)/len(vols) if vols else 1
    hvb   = [i for i,v in enumerate(vols) if v > vavg*1.5]
    ob_res = max(highs[i] for i in hvb) if hvb else max(highs)
    ob_sup = min(lows[i]  for i in hvb) if hvb else min(lows)
    key_high = max(float(k[2]) for k in k1h_window[-20:])
    key_low  = min(float(k[3]) for k in k1h_window[-20:])
    liq_above = max(ob_res, key_high); liq_below = min(ob_sup, key_low)
    if direction == "LONG": return round(liq_above - atr14*0.5, 8)
    else:                   return round(liq_below + atr14*0.5, 8)

def score_and_entry(k1h_w, k4h_w):
    if len(k1h_w)<20 or len(k4h_w)<10: return 0,None,0,0,0
    c1h=[float(k[4]) for k in k1h_w]; c4h=[float(k[4]) for k in k4h_w]
    v1h=[float(k[5]) for k in k1h_w]; mark=c1h[-1]
    atr14=atr_calc(k1h_w)
    if atr14<=0: return 0,None,0,0,0
    for ki in range(-12,0):
        if abs(ki)>len(k1h_w): continue
        kl=k1h_w[ki]; kh,kl2=float(kl[2]),float(kl[3])
        kr=kh-kl2; ko,kc=float(kl[1]),float(kl[4])
        rp=kr/kl2*100 if kl2>0 else 0; kd=(ko-kc)/ko*100 if ko>0 else 0
        if kr>atr14*3.5 and (rp>10 or kd>8): return 0,None,0,0,0
    rsi1h=rsi_calc(c1h); rsi4h=rsi_calc(c4h)
    ema20_1h=ema_calc(c1h[-20:],20)
    ema50_1h=ema_calc(c1h[-50:],50) if len(c1h)>=50 else ema_calc(c1h,len(c1h))
    ema20_4h=ema_calc(c4h[-20:],20) if len(c4h)>=20 else ema_calc(c4h,len(c4h))
    ema50_4h=ema_calc(c4h[-50:],50) if len(c4h)>=50 else ema_calc(c4h,len(c4h))
    vb=sum(v1h[-20:-3])/17 if len(v1h)>=20 else (sum(v1h)/len(v1h) if v1h else 1)
    vr=(sum(v1h[-3:])/3)/vb if vb>0 else 1
    sma20=sum(c1h[-20:])/20; std20=(sum((x-sma20)**2 for x in c1h[-20:])/20)**0.5
    bb_u=sma20+2*std20; bb_l=sma20-2*std20
    bb_pct=(c1h[-1]-bb_l)/(bb_u-bb_l)*100 if (bb_u-bb_l)>0 else 50
    lk=k1h_w[-1]; o2,h2,lw2,c2=float(lk[1]),float(lk[2]),float(lk[3]),float(lk[4])
    body=abs(c2-o2); lower=min(o2,c2)-lw2
    hammer=lower>body*2 and body>0
    dir1h='↑' if c1h[-1]>c1h[-2]>c1h[-3] else('↓' if c1h[-1]<c1h[-2]<c1h[-3] else '~')
    chg24=(c1h[-1]-c1h[-25])/c1h[-25]*100 if len(c1h)>=25 else 0
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
    entry=round(mark-atr14*0.4,8) if direction=="LONG" else round(mark+atr14*0.4,8)
    # SL固定12%
    sl_price=round(entry*(1-SL_FIXED),8) if direction=="LONG" else round(entry*(1+SL_FIXED),8)
    # TP清算图+ATR，85+分收窄2xATR
    tp_mult=2.0 if score>=85 else 3.0
    lmap_tp=sim_sl_tp(k1h_w, direction, atr14)
    if direction=="LONG":
        tp_atr=entry+atr14*tp_mult
        tp1=round(min(lmap_tp,tp_atr) if lmap_tp>entry+atr14*1.5 else tp_atr, 8)
    else:
        tp_atr=entry-atr14*tp_mult
        tp1=round(max(lmap_tp,tp_atr) if lmap_tp<entry-atr14*1.5 else tp_atr, 8)
    return score, direction, sl_price, tp1, entry

def run_backtest(sym_klines_map):
    print(f"\n{'='*60}\n固定12%SL + 动态仓位回测\n{'='*60}")
    trades=[]; pending={}; positions={}
    all_ts=set()
    for klines in sym_klines_map.values():
        for k in klines: all_ts.add(int(k[0]))
    timeline=sorted(all_ts)
    sym_ts_idx={sym:{int(k[0]):i for i,k in enumerate(klines)} for sym,klines in sym_klines_map.items()}
    step=max(1,len(timeline)//20)

    for bar_idx,ts in enumerate(timeline):
        if bar_idx%step==0:
            print(f"  {bar_idx/len(timeline)*100:.0f}%  {dt.datetime.utcfromtimestamp(ts/1000).strftime('%m-%d %H:%M')}  持仓={len(positions)}  成交={len(trades)}")
        for sym,klines in sym_klines_map.items():
            idx=sym_ts_idx[sym].get(ts)
            if idx is None or idx<1: continue
            cur_k=klines[idx]
            cur_h,cur_l,cur_c=float(cur_k[2]),float(cur_k[3]),float(cur_k[4])

            if sym in positions:
                pos=positions[sym]; ep=pos['entry']; dir_=pos['dir']
                trail=pos.get('trailing_sl')
                if trail is not None:
                    atr_t=pos.get('atr_init',ep*0.02)
                    if dir_=='LONG':
                        new_t=round(cur_l-atr_t*0.5,8)
                        if new_t>trail: positions[sym]['trailing_sl']=new_t; trail=new_t
                        if cur_l<=trail:
                            pnl=(trail-ep)/ep*pos['usdt']
                            trades.append({**pos,"close":trail,"reason":"移动止损","pnl":round(pnl,4),"close_ts":ts}); del positions[sym]; continue
                    else:
                        new_t=round(cur_h+atr_t*0.5,8)
                        if new_t<trail: positions[sym]['trailing_sl']=new_t; trail=new_t
                        if cur_h>=trail:
                            pnl=(ep-trail)/ep*pos['usdt']
                            trades.append({**pos,"close":trail,"reason":"移动止损","pnl":round(pnl,4),"close_ts":ts}); del positions[sym]; continue
                    continue
                hit_sl=hit_tp=False
                if dir_=='LONG':
                    if cur_l<=pos['sl']:  hit_sl=True
                    if cur_h>=pos['tp1']: hit_tp=True
                else:
                    if cur_h>=pos['sl']:  hit_sl=True
                    if cur_l<=pos['tp1']: hit_tp=True
                if hit_tp:
                    atr_now=pos.get('atr_init',abs(ep-pos['sl'])/SL_MULT)
                    half_pnl=((pos['tp1']-ep)/ep if dir_=='LONG' else (ep-pos['tp1'])/ep)*pos['usdt']*0.5
                    trades.append({**pos,"close":pos['tp1'],"reason":"TP1半仓","pnl":round(half_pnl,4),"close_ts":ts})
                    trail_init=(round(cur_h-atr_now*2.0,8) if dir_=='LONG' else round(cur_l+atr_now*2.0,8))
                    positions[sym]={**pos,'usdt':pos['usdt']*0.5,'trailing_sl':trail_init,'atr_init':atr_now}
                    continue
                elif hit_sl:
                    pnl=((pos['sl']-ep)/ep if dir_=='LONG' else (ep-pos['sl'])/ep)*pos['usdt']
                    trades.append({**pos,"close":pos['sl'],"reason":"SL","pnl":round(pnl,4),"close_ts":ts}); del positions[sym]; continue

            if sym in pending:
                pend=pending[sym]
                if (ts-pend['placed_ts'])//3600000>=ORDER_TTL: del pending[sym]; continue
                if ((pend['dir']=='LONG' and cur_l<=pend['entry']) or (pend['dir']=='SHORT' and cur_h>=pend['entry'])):
                    positions[sym]={**pend,'open_ts':ts}; del pending[sym]; continue

            if sym in positions or sym in pending: continue
            end_i=idx+1
            k1h_w=klines[max(0,end_i-55):end_i]
            if len(k1h_w)<20: continue
            k4h_w=[]; tmp=[]
            for k in klines[max(0,end_i-200):end_i]:
                tmp.append(k)
                if len(tmp)==4:
                    k4h_w.append([tmp[0][0],tmp[0][1],str(max(float(x[2]) for x in tmp)),
                                  str(min(float(x[3]) for x in tmp)),tmp[-1][4],
                                  str(sum(float(x[5]) for x in tmp))]+tmp[-1][6:]); tmp=[]
            if len(k4h_w)<10: continue
            score,direction,sl,tp1,entry=score_and_entry(k1h_w,k4h_w)
            if score<SCORE_MIN or direction is None: continue
            if abs(entry-cur_c)/cur_c*100>DEV_MAX: continue
            usdt=pos_size(score)
            atr_now=atr_calc(k1h_w)
            pending[sym]={'sym':sym,'entry':entry,'sl':sl,'tp1':tp1,'dir':direction,
                          'usdt':usdt,'score':score,'placed_ts':ts,'atr_init':atr_now}

    for sym,pos in positions.items():
        klines=sym_klines_map.get(sym,[])
        lc=float(klines[-1][4]) if klines else pos['entry']
        pnl=((lc-pos['entry'])/pos['entry'] if pos['dir']=='LONG' else (pos['entry']-lc)/pos['entry'])*pos['usdt']
        trades.append({**pos,"close":lc,"reason":"持有到期","pnl":round(pnl,4),"close_ts":END_TS})
    return trades

def report(trades):
    import collections
    if not trades: print("无记录"); return
    wins=[t for t in trades if t['pnl']>0]; losses=[t for t in trades if t['pnl']<=0]
    tp=sum(t['pnl'] for t in trades)
    aw=sum(t['pnl'] for t in wins)/len(wins) if wins else 0
    al=abs(sum(t['pnl'] for t in losses)/len(losses)) if losses else 1
    wr=len(wins)/len(trades)*100; rr=aw/al if al else 0
    equity=1000; peak=1000; mdd=0; monthly=collections.defaultdict(float)
    for t in sorted(trades,key=lambda x:x['close_ts']):
        equity+=t['pnl']
        if equity>peak: peak=equity
        dd=(peak-equity)/peak*100
        if dd>mdd: mdd=dd
        mo=dt.datetime.utcfromtimestamp(t['close_ts']/1000).strftime("%Y-%m")
        monthly[mo]+=t['pnl']
    longs=[t for t in trades if t['dir']=='LONG']; shorts=[t for t in trades if t['dir']=='SHORT']
    lw=[t for t in longs if t['pnl']>0]; sw=[t for t in shorts if t['pnl']>0]

    print(f"\n{'='*65}")
    print(f"📊 固定12%SL + 动态仓位 | 7.1~8.22 | 99个币")
    print(f"{'='*65}")
    print(f"仓位规则: 75~80分→50U  80~85分→100U  85~90分→150U  90+分→200U")
    print(f"总记录: {len(trades)}条  胜率={wr:.1f}%  W{len(wins)}/L{len(losses)}")
    print(f"总盈亏: {tp:+.2f}U   均胜=+{aw:.2f}U  均亏=-{al:.2f}U  盈亏比=1:{rr:.2f}")
    print(f"最大回撤: -{mdd:.1f}%   终态净值: {1000+tp:.0f}U")
    print(f"多头胜率: {len(lw)/len(longs)*100:.1f}%  空头胜率: {len(sw)/len(shorts)*100:.1f}%")

    print(f"\n月度盈亏:")
    for mo,p in sorted(monthly.items()):
        grp=[t for t in trades if dt.datetime.utcfromtimestamp(t['close_ts']/1000).strftime("%Y-%m")==mo]
        gw=[t for t in grp if t['pnl']>0]
        print(f"  {mo}  {p:+.2f}U   {len(grp)}条  胜率={len(gw)/len(grp)*100:.1f}%")

    print(f"\n评分分档（动态仓位效果）:")
    for lo,hi,usdt in [(75,80,50),(80,85,100),(85,90,150),(90,200,200)]:
        grp=[t for t in trades if lo<=t['score']<hi]
        if not grp: continue
        gw=[t for t in grp if t['pnl']>0]
        gp=sum(t['pnl'] for t in grp)
        print(f"  [{lo}-{hi}) {usdt}U  n={len(grp):4}  胜率={len(gw)/len(grp)*100:.1f}%  总盈亏={gp:+.1f}U  avg={gp/len(grp):+.2f}U")

    print(f"\n平仓原因:")
    rc=collections.Counter(t['reason'] for t in trades)
    for r,n in rc.most_common():
        avg=sum(t['pnl'] for t in trades if t['reason']==r)/n
        print(f"  {r:<12} n={n:4}  avg={avg:+.2f}U")

    print(f"\n{'='*65}")
    print(f"对比（均为7.1~8.22）:")
    print(f"  固定100U（sl12）       胜率86.1%   +278U  盈亏比0.18  回撤-25.5%")
    print(f"  动态仓位（本次）       胜率{wr:.1f}%   {tp:+.0f}U  盈亏比{rr:.2f}  回撤-{mdd:.1f}%")

if __name__=="__main__":
    print(f"[backtest_sl12_dynamic] 开始 {dt.datetime.utcnow().strftime('%H:%M UTC')}")
    print(f"仓位: 75~80→50U  80~85→100U  85~90→150U  90+→200U")
    sym_klines={}
    def fetch(sym):
        try: return sym,fetch_klines_full(sym,"1h",START_TS,END_TS)
        except: return sym,[]
    print(f"\n[1/3] 拉K线...")
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
    with open("backtest_sl12_dynamic_result.json","w") as f:
        json.dump({"trades":trades},f,ensure_ascii=False,indent=2)
    print(f"\n已保存: backtest_sl12_dynamic_result.json")
    print(f"[backtest_sl12_dynamic] 完成 {dt.datetime.utcnow().strftime('%H:%M UTC')}")
