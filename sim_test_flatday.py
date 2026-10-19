"""
sim_test_flatday.py — 横盘日专项测试 (2026-08-01, BTC -0.11%)

专项关注点：
① 信号极少时系统行为（是否放宽条件）
② 5个候选"保底机制"有没有带来低质量信号
③ RANGING 状态下双向信号分布
④ 最终是否因无信号而"空仓一天"（这是正确行为）
"""

import sys, os, json, requests
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'v3'))

from datetime import datetime, timezone
from modules.dynamic_baseline import compute as compute_baseline, _closes, _ema, _atr_series, _mean
from modules.market_bias import judge_market_state, get_market_state_info, MARKET_STATE_HALT
from modules.scorer import score as do_score
from modules.scene_matcher import match as do_match
from modules.pre_order_gate import pre_order_gate
from modules.position_size import calc_position_size
from modules.time_stop import check_time_stop

SIM_DATE = '2026-08-01'
DATA_FILE = '/tmp/sim_kdata_0801.json'

# ── 拉数据（如果还没有）────────────────────────────────────────────────────────
def fetch_data():
    start_ts = int(datetime(2026,8,1,0,0,0,tzinfo=timezone.utc).timestamp()*1000)
    end_ts   = int(datetime(2026,8,2,0,0,0,tzinfo=timezone.utc).timestamp()*1000)

    SCAN_SYMBOLS = ['BTCUSDT','ETHUSDT','SOLUSDT','KOMAUSDT',
                    'SNDKUSDT','SKYAIUSDT','BICOUSDT',
                    '1000RATSUSDT','BEATUSDT','BANKUSDT','SOXLUSDT','MUUSDT']
    kdata = {}; fdata = {}
    for sym in SCAN_SYMBOLS:
        try:
            r1h = requests.get('https://fapi.binance.com/fapi/v1/klines',
                params={'symbol':sym,'interval':'1h','startTime':start_ts,'endTime':end_ts,'limit':24},timeout=10)
            r4h = requests.get('https://fapi.binance.com/fapi/v1/klines',
                params={'symbol':sym,'interval':'4h','startTime':start_ts-4*3600*1000*15,'endTime':end_ts,'limit':20},timeout=10)
            rf  = requests.get('https://fapi.binance.com/fapi/v1/fundingRate',
                params={'symbol':sym,'startTime':start_ts,'endTime':end_ts,'limit':10},timeout=10)
            k1h_d = r1h.json(); k4h_d = r4h.json()
            if not isinstance(k1h_d,list) or len(k1h_d)<5:
                print(f'{sym}: 数据异常'); continue
            kdata[sym]={'1h':k1h_d,'4h':k4h_d}
            fdata[sym]=rf.json()
            o=float(k1h_d[0][1]); c=float(k1h_d[-1][4])
            print(f'{sym}: {len(k1h_d)}×1h  全天{(c-o)/o*100:+.1f}%')
        except Exception as e:
            print(f'{sym}: {e}')
    with open(DATA_FILE,'w') as f:
        json.dump({'kdata':kdata,'fdata':fdata},f)
    print(f'数据保存至 {DATA_FILE}')
    return kdata, fdata

# ── 加载数据 ─────────────────────────────────────────────────────────────────
if not os.path.exists(DATA_FILE):
    print("拉取 2026-08-01 数据...")
    KDATA, FDATA = fetch_data()
else:
    with open(DATA_FILE) as f:
        raw = json.load(f)
    KDATA = raw['kdata']; FDATA = raw['fdata']
    print(f"已有数据: {list(KDATA.keys())}")

SCAN_SYMBOLS = list(KDATA.keys())

# ── 工具 ──────────────────────────────────────────────────────────────────────
def get_k1h_until(sym, hour_idx):
    all_k = KDATA.get(sym,{}).get('1h',[])
    if not isinstance(all_k, list): all_k = []
    hist_4h = KDATA.get(sym,{}).get('4h',[])
    if not isinstance(hist_4h, list): hist_4h = []  # 防御 API 错误响应
    hist_1h = []
    for bar in hist_4h[:13]:
        o,h,l,c,v = float(bar[1]),float(bar[2]),float(bar[3]),float(bar[4]),float(bar[5])
        ts=bar[0]
        for i in range(4):
            sub_c=o+(c-o)*(i+1)/4
            hist_1h.append([ts+i*3600000,str(o if i==0 else o+(c-o)*i/4),
                            str(h),str(l),str(sub_c),str(v/4),0,'0',0,'0','0','0'])
    combined=hist_1h+all_k[:hour_idx+1]
    return combined[-80:] if len(combined)>=80 else combined

def get_k4h_recent(sym):
    v = KDATA.get(sym,{}).get('4h',[])
    return v[-10:] if isinstance(v,list) else []

def get_price_at(sym, hour_idx):
    k=KDATA.get(sym,{}).get('1h',[])
    return float(k[hour_idx][4]) if hour_idx<len(k) else 0.0

def get_funding(sym, hour_idx):
    rates=FDATA.get(sym,[])
    if not rates: return 0.0001
    day_start=int(datetime(2026,8,1,0,0,0,tzinfo=timezone.utc).timestamp()*1000)
    target=day_start+hour_idx*3600000
    best=rates[0]
    for r in rates:
        if r.get('fundingTime',0)<=target: best=r
    return float(best.get('fundingRate',0.0001))

# ── 账户 ──────────────────────────────────────────────────────────────────────
ACCOUNT={'equity':3000.0,'positions':{},'orders':[],'trade_log':[]}

def C(color,s):
    m={'green':'\033[92m','red':'\033[91m','yellow':'\033[93m','cyan':'\033[96m',
       'bold':'\033[1m','reset':'\033[0m','blue':'\033[94m','gray':'\033[90m'}
    return f"{m.get(color,'')}{s}{m['reset']}"
def hdr(t): print(f"\n{C('bold','─'*64)}\n{C('bold',t)}\n{C('bold','─'*64)}")
def log(m,lv='info'):
    lmap={'info':'gray','ok':'green','warn':'yellow','err':'red','signal':'cyan','blue':'blue'}
    print(f"  {C(lmap.get(lv,'gray'),m)}")

# ── 专项跟踪 ──────────────────────────────────────────────────────────────────
hourly_signal_count = []     # [(hour, n_raw_signals, n_after_filter, n_matched)]
low_quality_opens   = []     # 评分<65 但仍被开仓的情况（保底机制滥用检测）
zero_signal_hours   = []     # 完全无信号的小时

def run():
    hdr(f"😴 横盘日专项测试 — {SIM_DATE} (BTC -0.11%)")
    print(f"  专项关注: 信号稀少时系统是否放宽条件、保底机制质量")

    for hour in range(24):
        btc_price=get_price_at('BTCUSDT',hour)
        # 用 ETHUSDT 作为市场基准（0801 无 BTC 数据）
        anchor_sym = 'BTCUSDT' if 'BTCUSDT' in KDATA else 'ETHUSDT'
        k1h_btc=get_k1h_until(anchor_sym,hour)
        k4h_btc=get_k4h_recent(anchor_sym)
        avg_funding=get_funding(anchor_sym,hour)
        state=judge_market_state(k1h_btc,k4h_btc,avg_funding)
        state_info=get_market_state_info(state)

        atrs=_atr_series(k1h_btc) if k1h_btc else []
        if not atrs:
            cur_atr=0.0; atr_pct=50.0
        else:
            cur_atr=_mean(atrs[-3:]) if len(atrs)>=3 else atrs[-1]
            atr_window=atrs[-60:] if len(atrs)>=60 else atrs
            atr_pct=sum(1 for a in atr_window if a<=cur_atr)/len(atr_window)*100

        emoji='🔴' if state in MARKET_STATE_HALT else '🟡' if 'RANGING' in state else '🟢'
        log(f"{hour:02d}:00  BTC:{btc_price:.0f}  [{emoji} {state}]  ATR%:{atr_pct:.0f}  可开:{state_info['can_open']}",'info')

        if state in MARKET_STATE_HALT:
            log(f"  🛑 HALT",'warn')
            _manage(hour); continue

        btc_closes=_closes(k1h_btc) if k1h_btc else []
        if len(btc_closes)>=50:
            ema20=_ema(btc_closes[-20:],20); ema50=_ema(btc_closes[-50:],50)
            bp=float(k1h_btc[-1][4])
            btc_trend='up' if (ema20>ema50 and bp>ema20) else ('down' if (ema20<ema50 and bp<ema20) else 'neutral')
        else:
            btc_trend='neutral' 

        # 扫描
        raw_count=0; filtered_count=0; matched_count=0
        scan_results=[]
        for sym in SCAN_SYMBOLS:
            k1h=get_k1h_until(sym,hour)
            k4h=get_k4h_recent(sym)
            if len(k1h)<20: continue
            mp=float(k1h[-1][4]); fd=get_funding(sym,hour)
            prev_c=float(k1h[-2][4]) if len(k1h)>=2 else mp
            chg24=(mp-prev_c)/prev_c if prev_c>0 else 0
            try:
                bl=compute_baseline(k1h)
                sr=do_score(k1h,k4h,mp,fd,chg24,btc_trend,baseline=bl)
            except: continue
            direction=sr['direction']; total=sr['total']
            raw_count+=1
            if direction in ('NONE','DISCARD') or total<55: continue
            filtered_count+=1

            # 趋势过滤
            if state_info['preferred_direction']=='LONG' and direction=='SHORT': total-=20
            elif state_info['preferred_direction']=='SHORT' and direction=='LONG': total-=20
            if total<55: continue

            scan_results.append({'symbol':sym,'direction':direction,'score':total,'score_result':sr,
                'track':sr.get('track','oscillation'),'mark_price':mp,'funding':fd,
                'chg24':chg24,'k1h':k1h,'k4h':k4h,'baseline':bl})

        # 【关键检查】排序后取 Top5，记录最低分
        scan_results.sort(key=lambda x:x['score'],reverse=True)
        top5=scan_results[:5]

        if not top5:
            zero_signal_hours.append(hour)
            log(f"  ○ 无信号（原始{raw_count}个标的，过滤后0个）",'gray')
        else:
            min_score=min(s['score'] for s in top5)
            log(f"  扫描: {len(top5)}个候选  最低分:{min_score}  (原始{raw_count}→过滤后{filtered_count})",'gray')
            for s in top5:
                quality="✅" if s['score']>=65 else "⚠️低质"
                log(f"    {quality} {s['direction']:5} {s['symbol']:15} {s['score']}分  {s['score_result']['signal_type']}",'gray')
                if s['score']<65:
                    low_quality_opens.append({'hour':hour,'sym':s['symbol'],'score':s['score'],'dir':s['direction']})

        # 场景匹配（只计数，不关心具体内容）
        for item in top5:
            try:
                sm=do_match(item['k1h'],item['mark_price'],item['direction'],
                            track=item['track'],bl=item['baseline'],symbol=item['symbol'])
                if sm['matched'] and sm.get('second_score',0)>=65:
                    matched_count+=1
            except: pass

        hourly_signal_count.append((hour, raw_count, len(top5), matched_count))

        _try_open(hour,top5,state,state_info)
        _check_fills(hour)
        _manage(hour)

    # EOD
    for pk,pos in list(ACCOUNT['positions'].items()):
        sym=pos['symbol']; cp=get_price_at(sym,23)
        if cp<=0: continue
        rem=pos.get('remaining_pct',1.0)
        pnl=(cp-pos['open_price'])/pos['open_price']*pos['notional']*rem if pos['direction']=='LONG' \
            else (pos['open_price']-cp)/pos['open_price']*pos['notional']*rem
        ACCOUNT['trade_log'].append({'symbol':sym,'direction':pos['direction'],'open_price':pos['open_price'],
            'close_price':cp,'pnl':pnl,'reason':'EOD','hour':23,'notional':pos['notional']*rem})

    # ── 报告 ───────────────────────────────────────────────────────────────────
    hdr("📊 横盘日专项分析报告")

    print(f"\n  BTC 全天: {get_price_at('BTCUSDT',0):.0f} → {get_price_at('BTCUSDT',23):.0f}")

    print(f"\n  【每小时信号统计】")
    print(f"  {'时间':>5} {'扫标的':>6} {'Top5':>5} {'场景匹配':>6}")
    for h,raw,f5,m in hourly_signal_count:
        bar = '█'*f5 if f5>0 else '·'
        print(f"  {h:02d}:00  {raw:>5}  {f5:>4}  {m:>5}  {bar}")

    total_sig=sum(f5 for _,_,f5,_ in hourly_signal_count)
    total_match=sum(m for _,_,_,m in hourly_signal_count)
    print(f"\n  全天累计Top5候选: {total_sig}个  场景匹配: {total_match}个")
    print(f"  无信号小时: {len(zero_signal_hours)}h → {zero_signal_hours}")

    print(f"\n  【保底机制质量检查（<65分进入候选）】")
    if low_quality_opens:
        print(f"  ⚠️ 发现 {len(low_quality_opens)} 个低质量候选进入了Top5列表:")
        for s in low_quality_opens:
            print(f"    {s['hour']:02d}:00  {s['dir']:5} {s['sym']:15}  {s['score']}分")
        print(f"  → 这些候选虽然进了Top5，但场景匹配门槛65分会再次过滤")
        print(f"  → 实际开仓需要二次评分≥65，不会因为进了候选就放宽")
    else:
        print(f"  ✅ 全天 Top5 候选最低分均 ≥ 55 分，无异常低质量信号混入")

    trades=ACCOUNT['trade_log']
    total_pnl=sum(t['pnl'] for t in trades)
    print(f"\n  【交易结果】")
    print(f"  成交: {len(trades)}笔  总PnL: {total_pnl:+.1f}U")
    if trades:
        for t in sorted(trades, key=lambda x:x['hour']):
            print(f"  {t['hour']:02d}:xx  {t['direction']:5} {t['symbol']:15}  {t['reason']:15}  {t['pnl']:+.1f}U")
    else:
        print(f"  ✅ 全天0成交（横盘日正确行为——无合格结构，不开仓）")


# ── 辅助函数（与大波动日脚本完全一致）────────────────────────────────────────
def _try_open(hour, candidates, state, state_info):
    for item in candidates:
        sym=item['symbol']; direction=item['direction']; score=item['score']
        k1h=item['k1h']; bl=item['baseline']; mp=item['mark_price']; fd=item['funding']
        track=item['track']
        pos_key=f"{sym}_{direction}"
        if pos_key in ACCOUNT['positions']: continue
        if any(o['symbol']==sym for o in ACCOUNT['orders']): continue
        lc=sum(1 for k in ACCOUNT['positions'] if k.endswith('_LONG'))
        sc=sum(1 for k in ACCOUNT['positions'] if k.endswith('_SHORT'))
        if lc+sc>=5: continue
        if direction=='LONG' and lc>=3: continue
        if direction=='SHORT' and sc>=2: continue
        try:
            sm=do_match(k1h,mp,direction,track=track,bl=bl,symbol=sym)
        except: continue
        if not sm['matched'] or sm.get('second_score',0)<65: continue
        entry=sm['entry_price']; sl=sm['sl_price']; tp1=sm['tp_price']
        risk=abs(entry-sl)
        tp2=round(entry+risk*3.0 if direction=='LONG' else entry-risk*3.0,6)
        notional=calc_position_size(item['score_result']['signal_type'],score,'neutral')
        notional=int(notional*state_info['size_multiplier'])
        notional=max(250,min(1000,round(notional/50)*50))
        sl_pct=abs(entry-sl)/entry if entry>0 else 0.02
        gate=pre_order_gate(sym,direction,score,fd,notional,equity=ACCOUNT['equity'],
                            sl_distance_pct=sl_pct,market_state=state)
        if not gate['ok']:
            if gate.get('check')==9:
                sug=gate.get('suggested_notional',notional)
                notional=max(150,round(sug/50)*50)
                gate2=pre_order_gate(sym,direction,score,fd,notional,equity=ACCOUNT['equity'],
                                     sl_distance_pct=sl_pct,market_state=state)
                if not gate2['ok']: continue
            else: continue
        order={'symbol':sym,'direction':direction,'entry':entry,'sl':sl,'tp1':tp1,'tp2':tp2,
               'notional':notional,'score':score,'scene':sm['scene'],'rr':sm.get('rr',0),
               'signal_type':item['score_result']['signal_type'],
               'placed_hour':hour,'expire_hour':hour+4,'tp1_triggered':False,'bars_in_range':0,
               'conditions':sm.get('conditions',{})}
        ACCOUNT['orders'].append(order)
        log(f"✅ 挂单 {direction} {sym}  {score}分  {sm['scene']}  "
            f"入场:{entry:.4f}  SL:{sl:.4f}  TP1:{tp1:.4f}  {notional}U",'signal')

def _check_fills(hour):
    still=[]
    for order in ACCOUNT['orders']:
        sym=order['symbol']; entry=order['entry']; direction=order['direction']
        if hour>order['expire_hour']:
            log(f"⏰ 过期 {direction} {sym}",'gray'); continue
        k1h=KDATA.get(sym,{}).get('1h',[])
        if hour>=len(k1h): still.append(order); continue
        high=float(k1h[hour][2]); low=float(k1h[hour][3])
        if (direction=='LONG' and low<=entry) or (direction=='SHORT' and high>=entry):
            ACCOUNT['positions'][f"{sym}_{direction}"]={
                **order,'open_hour':hour,'open_price':entry,'status':'open',
                'tp1_triggered':False,'remaining_pct':1.0,'bars_in_range':0,
                'trail_activated':False,'conditions':order.get('conditions',{})}
            log(f"🎯 成交 {direction} {sym} @{entry:.4f}  {order['notional']}U",'ok')
        else: still.append(order)
    ACCOUNT['orders']=still

def _manage(hour):
    still={}
    for pk,pos in ACCOUNT['positions'].items():
        sym=pos['symbol']; direction=pos['direction']
        sl=pos['sl']; tp1=pos['tp1']; tp2=pos['tp2']
        notional=pos['notional']; rem=pos.get('remaining_pct',1.0)
        tp1t=pos.get('tp1_triggered',False)
        k1h=KDATA.get(sym,{}).get('1h',[])
        if hour>=len(k1h): still[pk]=pos; continue
        high=float(k1h[hour][2]); low=float(k1h[hour][3]); cur=float(k1h[hour][4])
        close_reason=None; close_price=0.0; close_pct=0.0
        pos_risk=abs(pos['open_price']-pos['sl'])
        entry_ts=pos.get('open_hour',0)*3600000; cur_ts=hour*3600000
        if direction=='LONG':
            ts_hit,_=check_time_stop(entry_ts,cur_ts,pos.get('scene','A'),pos.get('score',70),
                pos['open_price'],cur,tp1,direction,sl_price=sl,risk=pos_risk)
            if ts_hit and not tp1t: close_reason='TIME_SL'; close_price=cur; close_pct=(cur-pos['open_price'])/pos['open_price']
            elif not pos.get('trail_activated') and not tp1t:
                tt=pos.get('conditions',{}).get('trail_trigger_price',0)
                if tt and cur>=tt: pos['sl']=pos['open_price']; pos['trail_activated']=True
            if not close_reason and low<=sl: close_reason='SL'; close_price=sl; close_pct=(sl-pos['open_price'])/pos['open_price']
            elif not close_reason and not tp1t and high>=tp1:
                pnl=(tp1-pos['open_price'])/pos['open_price']*notional*0.6
                ACCOUNT['trade_log'].append({'symbol':sym,'direction':direction,'open_price':pos['open_price'],
                    'close_price':tp1,'pnl':pnl,'reason':'TP1_PARTIAL','hour':hour,'notional':notional*0.6})
                pos['tp1_triggered']=True; pos['remaining_pct']=0.4; pos['sl']=pos['open_price']
                still[pk]=pos; log(f"✨ TP1 {sym} +{pnl:.1f}U",'ok'); continue
            elif not close_reason and tp1t and high>=tp2: close_reason='TP2'; close_price=tp2; close_pct=(tp2-pos['open_price'])/pos['open_price']*rem
        else:
            ts_hit,_=check_time_stop(entry_ts,cur_ts,pos.get('scene','A'),pos.get('score',70),
                pos['open_price'],cur,tp1,direction,sl_price=sl,risk=pos_risk)
            if ts_hit and not tp1t: close_reason='TIME_SL'; close_price=cur; close_pct=(pos['open_price']-cur)/pos['open_price']
            elif not pos.get('trail_activated') and not tp1t:
                tt=pos.get('conditions',{}).get('trail_trigger_price',0)
                if tt and cur<=tt: pos['sl']=pos['open_price']; pos['trail_activated']=True
            if not close_reason and high>=sl: close_reason='SL'; close_price=sl; close_pct=(pos['open_price']-sl)/pos['open_price']
            elif not close_reason and not tp1t and low<=tp1:
                pnl=(pos['open_price']-tp1)/pos['open_price']*notional*0.6
                ACCOUNT['trade_log'].append({'symbol':sym,'direction':direction,'open_price':pos['open_price'],
                    'close_price':tp1,'pnl':pnl,'reason':'TP1_PARTIAL','hour':hour,'notional':notional*0.6})
                pos['tp1_triggered']=True; pos['remaining_pct']=0.4; pos['sl']=pos['open_price']
                still[pk]=pos; log(f"✨ TP1 {sym} +{pnl:.1f}U",'ok'); continue
            elif not close_reason and tp1t and low<=tp2: close_reason='TP2'; close_price=tp2; close_pct=(pos['open_price']-tp2)/pos['open_price']*rem
        if close_reason:
            pnl=close_pct*notional*(rem if rem<1 else 1)
            ACCOUNT['trade_log'].append({'symbol':sym,'direction':direction,'open_price':pos['open_price'],
                'close_price':close_price,'pnl':pnl,'reason':close_reason,'hour':hour,'notional':notional*rem})
            log(f"{'💰' if pnl>0 else '🔴'} {direction} {sym}  {close_reason}  {pnl:+.1f}U",'ok' if pnl>0 else 'err')
        else: still[pk]=pos
    ACCOUNT['positions']=still


if __name__=='__main__':
    import logging; logging.basicConfig(level=logging.WARNING)
    run()
