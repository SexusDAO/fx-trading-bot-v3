"""
sim_test_bigmove.py — 大波动日专项测试 (2026-06-02, BTC -6.53%)

专项关注点：
① HIGH_VOLATILITY 触发时刻（精确到小时）
② HALT 期间的持仓保护是否正常
③ HALT 解除后第一批信号的质量
④ 多头信号是否被趋势过滤全部拦住
"""

import sys, os, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'v3'))

from datetime import datetime, timezone
from modules.dynamic_baseline import compute as compute_baseline, _closes, _ema, _atr_series, _mean
from modules.market_bias import judge_market_state, get_market_state_info, MARKET_STATE_HALT
from modules.scorer import score as do_score
from modules.scene_matcher import match as do_match
from modules.pre_order_gate import pre_order_gate
from modules.position_size import calc_position_size
from modules.position_oscillation_check import check_tp1_tp2_oscillation
from modules.time_stop import check_time_stop

# ── 加载数据 ─────────────────────────────────────────────────────────────────
with open('/tmp/sim_kdata_0602.json') as f:
    RAW = json.load(f)

KDATA = RAW['kdata']
FDATA = RAW['fdata']

SIM_DATE = '2026-06-02'
SCAN_SYMBOLS = [s for s in KDATA.keys()]

# ── 工具 ──────────────────────────────────────────────────────────────────────
def get_k1h_until(sym, hour_idx):
    all_k = KDATA.get(sym, {}).get('1h', [])
    hist_4h = KDATA.get(sym, {}).get('4h', [])
    hist_1h = []
    for bar in hist_4h[:13]:
        o,h,l,c,v = float(bar[1]),float(bar[2]),float(bar[3]),float(bar[4]),float(bar[5])
        ts = bar[0]
        for i in range(4):
            sub_c = o + (c-o)*(i+1)/4
            hist_1h.append([ts+i*3600000,str(o if i==0 else o+(c-o)*i/4),
                            str(h),str(l),str(sub_c),str(v/4),0,'0',0,'0','0','0'])
    combined = hist_1h + all_k[:hour_idx+1]
    return combined[-80:] if len(combined)>=80 else combined

def get_k4h_recent(sym):
    return KDATA.get(sym, {}).get('4h', [])[-10:]

def get_price_at(sym, hour_idx):
    k = KDATA.get(sym,{}).get('1h',[])
    return float(k[hour_idx][4]) if hour_idx < len(k) else 0.0

def get_funding(sym, hour_idx):
    rates = FDATA.get(sym, [])
    if not rates: return 0.0001
    day_start = int(datetime(2026,6,2,0,0,0,tzinfo=timezone.utc).timestamp()*1000)
    target = day_start + hour_idx*3600000
    best = rates[0]
    for r in rates:
        if r.get('fundingTime',0) <= target: best = r
    return float(best.get('fundingRate',0.0001))

# ── 账户 ──────────────────────────────────────────────────────────────────────
ACCOUNT = {'equity':3000.0,'positions':{},'orders':[],'trade_log':[]}

def C(color, s):
    m={'green':'\033[92m','red':'\033[91m','yellow':'\033[93m',
       'cyan':'\033[96m','bold':'\033[1m','reset':'\033[0m','blue':'\033[94m','gray':'\033[90m'}
    return f"{m.get(color,'')}{s}{m['reset']}"

def hdr(t): print(f"\n{C('bold','─'*64)}\n{C('bold',t)}\n{C('bold','─'*64)}")
def log(m,lv='info'):
    lmap={'info':'gray','ok':'green','warn':'yellow','err':'red','signal':'cyan','trade':'bold','blue':'blue'}
    print(f"  {C(lmap.get(lv,'gray'),m)}")

# ── 专项跟踪变量 ──────────────────────────────────────────────────────────────
halt_events = []          # [(hour, state, 'enter'/'exit')]
post_halt_signals = []    # HALT 解除后第一轮信号
long_signals_blocked = [] # 被趋势过滤拦截的多头信号
prev_state = None

# ── 主循环 ────────────────────────────────────────────────────────────────────
def run():
    global prev_state
    hdr(f"🔥 大波动日专项测试 — {SIM_DATE} (BTC -6.53%)")
    print(f"  专项关注: HIGH_VOLATILITY 触发/解除时刻、多头拦截情况")

    just_exited_halt = False

    for hour in range(24):
        btc_price = get_price_at('BTCUSDT', hour)
        k1h_btc = get_k1h_until('BTCUSDT', hour)
        k4h_btc = get_k4h_recent('BTCUSDT')
        avg_funding = get_funding('BTCUSDT', hour)

        state, state_info = judge_market_state(k1h_btc, k4h_btc, avg_funding), {}
        state_info = get_market_state_info(state)

        # 跟踪状态变化
        if prev_state != state:
            if state in MARKET_STATE_HALT:
                halt_events.append((hour, state, 'enter'))
            elif prev_state in MARKET_STATE_HALT:
                halt_events.append((hour, state, 'exit'))
                just_exited_halt = True
        prev_state = state

        # ATR 百分位（用于判断 HIGH_VOLATILITY）
        atrs = _atr_series(k1h_btc)
        cur_atr = _mean(atrs[-3:]) if len(atrs)>=3 else atrs[-1]
        atr_window = atrs[-60:] if len(atrs)>=60 else atrs
        atr_pct = sum(1 for a in atr_window if a<=cur_atr)/len(atr_window)*100 if atr_window else 50

        emoji = '🔴' if state in MARKET_STATE_HALT else ('🟠' if 'DOWN' in state else '🟢')
        log(f"{hour:02d}:00  BTC:{btc_price:.0f}  [{emoji} {state}]  ATR百分位:{atr_pct:.0f}%  可开:{state_info['can_open']}", 'info')

        # HALT 状态
        if state in MARKET_STATE_HALT:
            log(f"  🛑 HALT - 不扫描，仅监控持仓", 'warn')
            _manage(hour)
            continue

        # 扫描
        btc_closes = _closes(k1h_btc)
        ema20 = _ema(btc_closes[-20:],20) if len(btc_closes)>=20 else btc_closes[-1]
        ema50 = _ema(btc_closes[-50:],50) if len(btc_closes)>=50 else ema20
        bp = float(k1h_btc[-1][4]) if k1h_btc else btc_price
        btc_trend = 'up' if (ema20>ema50 and bp>ema20) else ('down' if (ema20<ema50 and bp<ema20) else 'neutral')

        scan_results = []
        for sym in SCAN_SYMBOLS:
            k1h = get_k1h_until(sym, hour)
            k4h = get_k4h_recent(sym)
            if len(k1h)<20: continue
            mp = float(k1h[-1][4]); fd = get_funding(sym, hour)
            prev_c = float(k1h[-2][4]) if len(k1h)>=2 else mp
            chg24 = (mp-prev_c)/prev_c if prev_c>0 else 0
            try:
                bl = compute_baseline(k1h)
                sr = do_score(k1h, k4h, mp, fd, chg24, btc_trend, baseline=bl)
            except: continue

            direction = sr['direction']
            total = sr['total']
            if direction in ('NONE','DISCARD') or total < 55: continue

            orig_total = total
            # 趋势过滤
            if state_info['preferred_direction']=='SHORT' and direction=='LONG':
                total -= 20
                if total < 55:
                    long_signals_blocked.append({'hour':hour,'sym':sym,'orig_score':orig_total,'blocked_by':'TRENDING_DOWN_penalty'})
                    log(f"  🚫 多头拦截 {sym} 原分{orig_total}→{total}（下跌日逆势-20）", 'warn')
                    continue

            scan_results.append({'symbol':sym,'direction':direction,'score':total,'score_result':sr,
                                  'track':sr.get('track','oscillation'),'mark_price':mp,'funding':fd,
                                  'chg24':chg24,'k1h':k1h,'k4h':k4h,'baseline':bl})

        scan_results.sort(key=lambda x:x['score'],reverse=True)
        scan_results = scan_results[:5]

        # HALT 解除后第一批信号记录
        if just_exited_halt and scan_results:
            log(f"  📡 HALT解除后首批信号 (共{len(scan_results)}个):", 'cyan')
            for s in scan_results:
                post_halt_signals.append({'hour':hour,'sym':s['symbol'],'dir':s['direction'],'score':s['score']})
                log(f"    {s['direction']:5} {s['symbol']:15} {s['score']}分  {s['score_result']['signal_type']}", 'cyan')
            just_exited_halt = False
        elif scan_results:
            for s in scan_results[:3]:
                log(f"  → {s['direction']:5} {s['symbol']:15} {s['score']}分", 'gray')

        # 挂单
        _try_open(hour, scan_results, state, state_info)
        _check_fills(hour)
        _manage(hour)

    # 收盘
    for pk, pos in list(ACCOUNT['positions'].items()):
        sym=pos['symbol']; cp=get_price_at(sym,23)
        if cp<=0: continue
        rem=pos.get('remaining_pct',1.0)
        if pos['direction']=='LONG': pnl=(cp-pos['open_price'])/pos['open_price']*pos['notional']*rem
        else: pnl=(pos['open_price']-cp)/pos['open_price']*pos['notional']*rem
        ACCOUNT['trade_log'].append({'symbol':sym,'direction':pos['direction'],'open_price':pos['open_price'],
            'close_price':cp,'pnl':pnl,'reason':'EOD','hour':23,'notional':pos['notional']*rem})
        log(f"  {'💰' if pnl>0 else '🔴'} EOD平仓 {pos['direction']} {sym} @{cp:.4f}  PnL:{pnl:+.1f}U",
            'ok' if pnl>0 else 'err')

    # 报告
    hdr("📊 大波动日专项分析报告")

    print(f"\n  【HALT触发/解除记录】")
    if halt_events:
        for hour, state, evt in halt_events:
            arrow = '⬆️ 进入' if evt=='enter' else '⬇️ 解除'
            print(f"    {hour:02d}:00  {arrow} {state}")
        # 计算HALT持续时间
        halt_hours = sum(1 for h in range(24) if any(
            e[0]<=h<(halt_events[i+1][0] if i+1<len(halt_events) else 24)
            and e[2]=='enter'
            for i,e in enumerate(halt_events) if e[2]=='enter'
        ))
        print(f"    HALT 总时长: ~{len([e for e in halt_events if e[2]=='enter'])*2}小时")
    else:
        print(f"    全天未触发 HIGH_VOLATILITY HALT")

    print(f"\n  【HALT解除后首批信号质量】")
    if post_halt_signals:
        for s in post_halt_signals:
            print(f"    {s['hour']:02d}:00  {s['dir']:5} {s['sym']:15}  {s['score']}分")
        avg_score = sum(s['score'] for s in post_halt_signals)/len(post_halt_signals)
        print(f"    平均评分: {avg_score:.1f}分  {'✅ 质量合格(≥65)' if avg_score>=65 else '⚠️ 质量偏低(<65)'}")
    else:
        print(f"    HALT未触发或解除后无有效信号")

    print(f"\n  【多头信号拦截统计】")
    print(f"    被趋势过滤拦截的多头信号: {len(long_signals_blocked)} 个")
    if long_signals_blocked:
        for s in long_signals_blocked[:8]:
            print(f"    {s['hour']:02d}:00  {s['sym']:15}  原分:{s['orig_score']} → 拦截")

    trades = ACCOUNT['trade_log']
    total_pnl = sum(t['pnl'] for t in trades)
    wins = [t for t in trades if t['pnl']>0]
    print(f"\n  【交易结果】")
    print(f"    成交: {len(trades)}笔  盈利:{len(wins)}笔  总PnL:{total_pnl:+.1f}U")
    for t in sorted(trades, key=lambda x:x['hour']):
        print(f"    {t['hour']:02d}:xx  {t['direction']:5} {t['symbol']:15}  {t['reason']:15}  {t['pnl']:+.1f}U")


# ── 辅助函数（复用主模拟逻辑）────────────────────────────────────────────────
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
            sm=do_match(k1h, mp, direction, track=track, bl=bl, symbol=sym)
        except: continue
        if not sm['matched'] or sm.get('second_score',0)<65: continue

        entry=sm['entry_price']; sl=sm['sl_price']; tp1=sm['tp_price']
        risk=abs(entry-sl)
        tp2=round(entry+risk*3.0 if direction=='LONG' else entry-risk*3.0,6)
        notional=calc_position_size(item['score_result']['signal_type'],score,'neutral')
        notional=int(notional*state_info['size_multiplier'])
        notional=max(250,min(1000,round(notional/50)*50))
        sl_pct=abs(entry-sl)/entry if entry>0 else 0.02

        gate=pre_order_gate(sym,direction,score,fd,notional,
                            equity=ACCOUNT['equity'],sl_distance_pct=sl_pct,market_state=state)
        if not gate['ok']:
            if gate.get('check')==9:
                sug=gate.get('suggested_notional',notional)
                notional=max(150,round(sug/50)*50)
                gate2=pre_order_gate(sym,direction,score,fd,notional,
                                     equity=ACCOUNT['equity'],sl_distance_pct=sl_pct,market_state=state)
                if not gate2['ok']: continue
            else: continue

        order={'symbol':sym,'direction':direction,'entry':entry,'sl':sl,'tp1':tp1,'tp2':tp2,
               'notional':notional,'score':score,'scene':sm['scene'],'rr':sm.get('rr',0),
               'signal_type':item['score_result']['signal_type'],
               'placed_hour':hour,'expire_hour':hour+4,'tp1_triggered':False,'bars_in_range':0,
               'conditions':sm.get('conditions',{})}
        ACCOUNT['orders'].append(order)
        log(f"✅ 挂单 {direction} {sym}  {score}分  场景:{sm['scene']}  "
            f"入场:{entry:.4f}  SL:{sl:.4f}  TP1:{tp1:.4f}  RR:{sm.get('rr',0):.1f}  {notional}U",'signal')

def _check_fills(hour):
    still=[]
    for order in ACCOUNT['orders']:
        sym=order['symbol']; entry=order['entry']; direction=order['direction']
        if hour>order['expire_hour']:
            log(f"⏰ 过期 {direction} {sym}  @{entry:.4f}",'gray'); continue
        k1h=KDATA.get(sym,{}).get('1h',[])
        if hour>=len(k1h): still.append(order); continue
        high=float(k1h[hour][2]); low=float(k1h[hour][3])
        if (direction=='LONG' and low<=entry) or (direction=='SHORT' and high>=entry):
            pos_key=f"{sym}_{direction}"
            ACCOUNT['positions'][pos_key]={**order,'open_hour':hour,'open_price':entry,'status':'open',
                'tp1_triggered':False,'remaining_pct':1.0,'bars_in_range':0,
                'trail_activated':False,'conditions':order.get('conditions',{})}
            log(f"🎯 成交 {direction} {sym} @{entry:.4f}  SL:{order['sl']:.4f}  TP1:{order['tp1']:.4f}  {order['notional']}U",'ok')
        else:
            still.append(order)
    ACCOUNT['orders']=still

def _manage(hour):
    still={}
    for pk,pos in ACCOUNT['positions'].items():
        sym=pos['symbol']; direction=pos['direction']
        sl=pos['sl']; tp1=pos['tp1']; tp2=pos['tp2']
        notional=pos['notional']; rem=pos.get('remaining_pct',1.0)
        tp1_triggered=pos.get('tp1_triggered',False)
        k1h=KDATA.get(sym,{}).get('1h',[])
        if hour>=len(k1h): still[pk]=pos; continue
        high=float(k1h[hour][2]); low=float(k1h[hour][3]); cur=float(k1h[hour][4])
        close_reason=None; close_price=0.0; close_pct=0.0

        pos_risk=abs(pos['open_price']-pos['sl'])
        entry_ts=pos.get('open_hour',0)*3600000; cur_ts=hour*3600000

        if direction=='LONG':
            ts_hit,ts_r=check_time_stop(entry_ts,cur_ts,pos.get('scene','A'),pos.get('score',70),
                pos['open_price'],cur,tp1,direction,sl_price=sl,risk=pos_risk)
            if ts_hit and not tp1_triggered:
                close_reason=f'TIME_SL'; close_price=cur; close_pct=(cur-pos['open_price'])/pos['open_price']
            elif not pos.get('trail_activated') and not tp1_triggered:
                tt=pos.get('conditions',{}).get('trail_trigger_price',0)
                if tt and cur>=tt:
                    pos['sl']=pos['open_price']; pos['trail_activated']=True
                    log(f"📈 移动止损 LONG {sym} SL→成本{pos['open_price']:.4f}",'blue')
            if not close_reason and low<=sl:
                close_reason='SL'; close_price=sl; close_pct=(sl-pos['open_price'])/pos['open_price']
            elif not close_reason and not tp1_triggered and high>=tp1:
                pnl=(tp1-pos['open_price'])/pos['open_price']*notional*0.6
                ACCOUNT['trade_log'].append({'symbol':sym,'direction':direction,'open_price':pos['open_price'],
                    'close_price':tp1,'pnl':pnl,'reason':'TP1_PARTIAL','hour':hour,'notional':notional*0.6})
                pos['tp1_triggered']=True; pos['remaining_pct']=0.4; pos['sl']=pos['open_price']
                still[pk]=pos
                log(f"✨ TP1 LONG {sym} @{tp1:.4f}  +{pnl:.1f}U",'ok'); continue
            elif not close_reason and tp1_triggered and high>=tp2:
                close_reason='TP2'; close_price=tp2; close_pct=(tp2-pos['open_price'])/pos['open_price']*rem
        else:
            ts_hit,ts_r=check_time_stop(entry_ts,cur_ts,pos.get('scene','A'),pos.get('score',70),
                pos['open_price'],cur,tp1,direction,sl_price=sl,risk=pos_risk)
            if ts_hit and not tp1_triggered:
                close_reason='TIME_SL'; close_price=cur; close_pct=(pos['open_price']-cur)/pos['open_price']
            elif not pos.get('trail_activated') and not tp1_triggered:
                tt=pos.get('conditions',{}).get('trail_trigger_price',0)
                if tt and cur<=tt:
                    pos['sl']=pos['open_price']; pos['trail_activated']=True
                    log(f"📉 移动止损 SHORT {sym} SL→成本{pos['open_price']:.4f}",'blue')
            if not close_reason and high>=sl:
                close_reason='SL'; close_price=sl; close_pct=(pos['open_price']-sl)/pos['open_price']
            elif not close_reason and not tp1_triggered and low<=tp1:
                pnl=(pos['open_price']-tp1)/pos['open_price']*notional*0.6
                ACCOUNT['trade_log'].append({'symbol':sym,'direction':direction,'open_price':pos['open_price'],
                    'close_price':tp1,'pnl':pnl,'reason':'TP1_PARTIAL','hour':hour,'notional':notional*0.6})
                pos['tp1_triggered']=True; pos['remaining_pct']=0.4; pos['sl']=pos['open_price']
                still[pk]=pos
                log(f"✨ TP1 SHORT {sym} @{tp1:.4f}  +{pnl:.1f}U",'ok'); continue
            elif not close_reason and tp1_triggered and low<=tp2:
                close_reason='TP2'; close_price=tp2; close_pct=(pos['open_price']-tp2)/pos['open_price']*rem

        if close_reason:
            pnl=close_pct*notional*(rem if rem<1 else 1)
            ACCOUNT['trade_log'].append({'symbol':sym,'direction':direction,'open_price':pos['open_price'],
                'close_price':close_price,'pnl':pnl,'reason':close_reason,'hour':hour,'notional':notional*rem})
            color='ok' if pnl>0 else 'err'
            log(f"{'💰' if pnl>0 else '🔴'} 平仓 {direction} {sym}  {close_reason}  @{close_price:.4f}  {pnl:+.1f}U",color)
        else:
            still[pk]=pos
    ACCOUNT['positions']=still


if __name__=='__main__':
    import logging; logging.basicConfig(level=logging.WARNING)
    run()
