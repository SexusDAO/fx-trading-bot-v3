"""
市场扫描任务 - 严格按 position_rules.json 双轨评分体系执行
首仓 300U 名义仓位，最多补仓到 600U
"""
import sys, os, requests, json, math, time, subprocess
sys.path.insert(0, "/root/.openclaw/workspace")
import binance_api as api

base_f = "https://fapi.binance.com"
STATE_FILE = "/root/.openclaw/workspace/ws_cache/scanner_state.json"
FIRST_SIZE = 300   # 首仓名义 300U
MAX_SIZE   = 600   # 最大名义 600U

# ===== 工具函数 =====
def ema_calc(data, n):
    k=2/(n+1); e=data[0]
    for x in data[1:]: e=x*k+e*(1-k)
    return e

def rsi_calc(closes, n=14):
    diffs=[closes[i]-closes[i-1] for i in range(1,len(closes))]
    g=[max(d,0) for d in diffs[-n:]]; l=[abs(min(d,0)) for d in diffs[-n:]]
    ag=sum(g)/n; al=sum(l)/n
    return 100-100/(1+ag/al) if al>0 else 50

def atr_calc(klines, n=14):
    trs=[]
    for i in range(1,len(klines)):
        h=float(klines[i][2]); lo=float(klines[i][3]); pc=float(klines[i-1][4])
        trs.append(max(h-lo,abs(h-pc),abs(lo-pc)))
    return sum(trs[-n:])/n if trs else 0

def get_sym_info(sym):
    r=requests.get(f"{base_f}/fapi/v1/exchangeInfo",timeout=10).json()
    s=next((x for x in r.get("symbols",[]) if x["symbol"]==sym),None)
    if not s: return None,None
    tick=float(next((f["tickSize"] for f in s["filters"] if f["filterType"]=="PRICE_FILTER"),"0.001"))
    step=float(next((f["stepSize"] for f in s["filters"] if f["filterType"]=="LOT_SIZE"),"1"))
    return tick,step

def fmt_price(p, tick):
    dt=max(0,-int(math.floor(math.log10(tick)))) if tick<1 else 0
    return f"{round(round(p/tick)*tick,dt):.{dt}f}"

def fmt_qty(q, step):
    ds=max(0,-int(math.floor(math.log10(step)))) if step<1 else 0
    return f"{round(round(q/step)*step,ds):.{ds}f}"

# ===== 读状态 =====
os.makedirs("/root/.openclaw/workspace/ws_cache", exist_ok=True)
state = json.load(open(STATE_FILE)) if os.path.exists(STATE_FILE) else {}
has_pos = state.get("has_position", False)
pos_sym = state.get("symbol", None)

# ===== 有持仓：监控模式 =====
if has_pos and pos_sym:
    positions = api.get_positions()
    pos = next((p for p in positions if p["symbol"]==pos_sym and abs(float(p.get("positionAmt",0)))>0.01), None)

    if not pos:
        state = {"has_position": False, "symbol": None}
        with open(STATE_FILE,"w") as f: json.dump(state,f)
        print(f"✅ {pos_sym} 已平仓，恢复扫描模式")
    else:
        entry=float(pos["entryPrice"]); qty=abs(float(pos["positionAmt"]))
        mark_p=float(pos["markPrice"]); pnl=float(pos.get("unRealizedProfit",0))
        liq=float(pos.get("liquidationPrice",0)); pct=(mark_p-entry)/entry*100
        sig_type=state.get("sig_type","trend")
        sl=state.get("sl_price",0); tp=state.get("tp_price",0)
        fr=requests.get(f"{base_f}/fapi/v1/premiumIndex",params={"symbol":pos_sym},timeout=10).json()
        funding=float(fr.get("lastFundingRate",0))*100

        # 移动止损检查
        breakeven_pct = 0.5 if sig_type=="reversal" else 3.0
        alert=""
        if pnl <= -200:
            alert=f"🚨 浮亏{pnl:.1f}U 超过200U警戒！请决定是否平仓"
        elif pct > breakeven_pct and sl < entry:
            alert=f"💡 浮盈{pct:.1f}%，建议止损上移至入场价{entry:.4f}保本（{sig_type}单规则）"

        print(f"📊 持仓监控 | {pos_sym} [{sig_type}单]")
        print(f"入场={entry:.4f} 现价={mark_p:.4f} 浮动={pnl:+.2f}U ({pct:+.2f}%)")
        if liq>0: print(f"强平={liq:.4f} 距强平={(mark_p-liq)/mark_p*100:.1f}%")
        print(f"止盈={tp if tp else '?'} 止损={sl if sl else '?'} 费率={funding:.3f}%")
        if alert: print(alert)
        else: print("HEARTBEAT_OK")
    sys.exit()

# ===== 无持仓：扫描模式 =====
symbols = [
    "BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT",
    "DOGEUSDT","ADAUSDT","AVAXUSDT","LINKUSDT","DOTUSDT",
    "LTCUSDT","ATOMUSDT","UNIUSDT","APTUSDT","ARBUSDT",
    "OPUSDT","SUIUSDT","INJUSDT","SEIUSDT","TIAUSDT",
    "WIFUSDT","PENDLEUSDT","JUPUSDT","NEARUSDT","FTMUSDT"
]

tickers = requests.get(f"{base_f}/fapi/v1/ticker/24hr", timeout=15).json()
tm = {t["symbol"]:t for t in tickers}

results = []
for sym in symbols:
    tk = tm.get(sym)
    if not tk: continue
    mark=float(tk.get("lastPrice",0)); chg24=float(tk.get("priceChangePercent",0))
    if float(tk.get("quoteVolume",0)) < 30_000_000: continue

    k1h  = requests.get(f"{base_f}/fapi/v1/klines",params={"symbol":sym,"interval":"1h","limit":55},timeout=10).json()
    k4h  = requests.get(f"{base_f}/fapi/v1/klines",params={"symbol":sym,"interval":"4h","limit":55},timeout=10).json()
    k15m = requests.get(f"{base_f}/fapi/v1/klines",params={"symbol":sym,"interval":"15m","limit":55},timeout=10).json()
    if not k1h or not k4h: continue

    c1h=[float(k[4]) for k in k1h]; v1h=[float(k[5]) for k in k1h]
    c4h=[float(k[4]) for k in k4h]
    c15m=[float(k[4]) for k in k15m] if k15m else c1h

    ema20_1h=ema_calc(c1h[-20:],20); ema50_1h=ema_calc(c1h[-50:],50)
    ema20_4h=ema_calc(c4h[-20:],20); ema50_4h=ema_calc(c4h[-50:],50) if len(c4h)>=50 else ema_calc(c4h,len(c4h))
    rsi_1h=rsi_calc(c1h); rsi_4h=rsi_calc(c4h); rsi_15m=rsi_calc(c15m)
    atr_1h=atr_calc(k1h)

    vb1h=sum(v1h[-20:-3])/17 if len(v1h)>=20 else sum(v1h)/len(v1h)
    vr1h=(sum(v1h[-3:])/3)/vb1h if vb1h>0 else 1

    sma20=sum(c1h[-20:])/20
    std20=(sum((x-sma20)**2 for x in c1h[-20:])/20)**0.5
    bb_u=sma20+2*std20; bb_l=sma20-2*std20
    bb_pct_1h=(c1h[-1]-bb_l)/(bb_u-bb_l)*100 if (bb_u-bb_l)>0 else 50

    lk=k1h[-1]; o=float(lk[1]); h=float(lk[2]); l2=float(lk[3]); c=float(lk[4])
    body=abs(c-o); lower=min(o,c)-l2
    hammer=lower>body*2 and body>0
    dir1h="↑" if c1h[-1]>c1h[-2]>c1h[-3] else("↓" if c1h[-1]<c1h[-2]<c1h[-3] else "~")
    rsi_div=c1h[-1]<c1h[-6] and rsi_1h>rsi_calc(c1h[:-5])

    fr=requests.get(f"{base_f}/fapi/v1/premiumIndex",params={"symbol":sym},timeout=10).json()
    funding=float(fr.get("lastFundingRate",0))*100

    # ===== 信号类型判断 =====
    rev_conds=sum([rsi_4h<15, funding<-0.5, chg24<-15, bb_pct_1h<5])
    is_reversal = rev_conds >= 3

    if is_reversal:
        # 逆势型评分（position_rules reversal_scoring）
        score=0
        if rsi_4h<10: score+=20
        elif rsi_4h<15: score+=15
        if rsi_1h<30: score+=8
        if rsi_15m<30 or rsi_1h<30: score+=4
        if bb_pct_1h<5: score+=3
        if funding<-0.5: score+=15
        elif funding<-0.3: score+=10
        elif funding<-0.1: score+=5
        if hammer: score+=10
        if vr1h>1.2: score+=6
        if rsi_div: score+=4
        if chg24<-20: score+=12
        elif chg24<-15: score+=8
        elif chg24<-10: score+=4
        sig_type="reversal"; threshold=65
    else:
        # 趋势型评分（position_rules trend_scoring）
        score=0
        # 趋势维度（max 30）
        if ema20_1h>ema50_1h and ema20_4h>ema50_4h: score+=30
        elif ema20_1h>ema50_1h or ema20_4h>ema50_4h: score+=15
        # 动量维度（max 25）
        if 50<=rsi_1h<=65: score+=15
        elif 40<=rsi_1h<50: score+=8
        if 50<=rsi_4h<=65: score+=10
        # 量能维度（max 20）
        if vr1h>1.5: score+=20
        elif vr1h>1.2: score+=12
        elif vr1h>1.0: score+=6
        # 结构情绪维度（max 40，含聪明钱）
        if bb_pct_1h>55: score+=10
        if dir1h=="↑": score+=8
        if hammer: score+=7
        if funding<-0.1: score+=5
        if chg24>3: score+=10
        sig_type="trend"; threshold=75

    results.append({
        "sym":sym,"mark":mark,"chg24":chg24,"score":score,"sig_type":sig_type,
        "threshold":threshold,"rsi_1h":rsi_1h,"rsi_4h":rsi_4h,"rsi_15m":rsi_15m,
        "vr1h":vr1h,"bb_pct":bb_pct_1h,"funding":funding,"dir":dir1h,
        "hammer":hammer,"div":rsi_div,"atr":atr_1h,
        "ema_1h":"多排" if ema20_1h>ema50_1h else "空排",
        "ema_4h":"多排" if ema20_4h>ema50_4h else "空排",
    })

results.sort(key=lambda x: x["score"], reverse=True)
top = results[0] if results else None

# 无达标信号
if not top or top["score"] < top["threshold"]:
    best3=[f"{r['sym']} {r['score']}分({r['sig_type']})" for r in results[:3]]
    print(f"📡 扫描完成 | 无开仓信号（最高: {best3[0] if best3 else '-'}）")
    if len(best3)>1: print(f"次高: {' | '.join(best3[1:])}")
    sys.exit()

# ===== 有达标信号，执行开仓前完整检查清单 =====
sym=top["sym"]; mark=top["mark"]; sig_type=top["sig_type"]
token=sym.replace("USDT","")

print(f"🎯 发现信号: {sym} | 评分={top['score']}/{top['threshold']} | 类型={sig_type}")
print(f"现价={mark:.4f} 涨跌={top['chg24']:+.1f}% RSI1H={top['rsi_1h']:.0f} RSI4H={top['rsi_4h']:.0f}")
print(f"量比={top['vr1h']:.2f}x BB%={top['bb_pct']:.0f} 费率={top['funding']:.3f}%")
print(f"EMA: 1H={top['ema_1h']} 4H={top['ema_4h']} 方向={top['dir']} 锤头={top['hammer']}")
print()

# 检查1：Token Report（聪明钱 + 一票否决）
print("🔍 检查1: Token Report...")
veto=False; tr_score=0; tr_note=""
try:
    tr=subprocess.run(["binance-pro-cli","search","report","--symbol",token],
        capture_output=True,text=True,timeout=30)
    rtext=tr.stdout.lower()
    veto_kws=["treasury transfer","team wallet","exploit","hack","rug","exit scam",
               "treasury multisig","project wallet","selling pressure from team"]
    hit=[kw for kw in veto_kws if kw in rtext]
    if hit:
        veto=True; tr_note=f"一票否决触发: {hit}"
        print(f"  ❌ {tr_note}")
    else:
        pos_kws=["smart money","whale","institutional","accumulation","inflow","bullish"]
        pos_hit=[kw for kw in pos_kws if kw in rtext]
        neg_kws=["selling pressure","bearish","significant decline","community concern"]
        neg_hit=[kw for kw in neg_kws if kw in rtext]
        if pos_hit: tr_score=15; tr_note=f"正面+15: {pos_hit[:2]}"
        elif neg_hit: tr_score=-10; tr_note=f"负面-10: {neg_hit[:2]}"
        else: tr_note="中性，0分"
        print(f"  {'✅' if tr_score>=0 else '⚠️'} {tr_note} | 评分调整: {tr_score:+d}")
        top["score"] += tr_score
except Exception as e:
    print(f"  ⚠️ Token Report异常({e})，跳过")

if veto:
    print(f"\n🚫 Token Report 一票否决，{sym} 本次不开仓")
    sys.exit()

# 检查2：OI背离（趋势做多时）
if sig_type == "trend":
    print("🔍 检查2: OI趋势背离...")
    try:
        oi_r=requests.get(f"{base_f}/futures/data/openInterestHist",
            params={"symbol":sym,"period":"1h","limit":6},timeout=10).json()
        if isinstance(oi_r,list) and len(oi_r)>=4:
            oi_old=float(oi_r[0].get("sumOpenInterestValue",0))
            oi_new=float(oi_r[-1].get("sumOpenInterestValue",0))
            oi_chg=(oi_new-oi_old)/oi_old*100 if oi_old>0 else 0
            if top["chg24"]>3 and oi_chg<-20:
                print(f"  ❌ OI背离: 价格{top['chg24']:+.1f}% 但OI{oi_chg:+.1f}% → 禁止做多")
                sys.exit()
            else:
                print(f"  ✅ OI变化={oi_chg:+.1f}%，无背离")
        else:
            print("  ⚠️ OI数据不足，跳过")
    except Exception as e:
        print(f"  ⚠️ OI检查异常({e})，跳过")
else:
    print("🔍 检查2: OI（逆势单跳过）✅")

# 检查3：高控盘（逆势单额外检查）
if sig_type == "reversal":
    print("🔍 检查3: 高控盘结构（逆势单）...")
    try:
        tr2=subprocess.run(["binance-pro-cli","search","report","--symbol",token],
            capture_output=True,text=True,timeout=30)
        rtext2=tr2.stdout.lower()
        conc_kws=["concentrated","top holders","whale concentration","high concentration"]
        conc_hit=[kw for kw in conc_kws if kw in rtext2]
        if conc_hit:
            print(f"  ❌ 高控盘+逆势单 → 禁止开仓: {conc_hit}")
            sys.exit()
        else:
            print("  ✅ 无高控盘警报")
    except Exception as e:
        print(f"  ⚠️ 跳过({e})")
else:
    print("🔍 检查3: 高控盘（趋势单跳过）✅")

# 检查4：最终评分确认
print(f"🔍 检查4: 最终评分={top['score']} 门槛={top['threshold']}")
if top["score"] < top["threshold"]:
    print(f"  ❌ Token Report调整后未达门槛，不开仓")
    sys.exit()
print("  ✅ 全部检查通过，执行开仓")

# ===== 【扫描模式】全部检查通过，输出方案但不执行开仓 =====
tick,step=get_sym_info(sym)
if not tick:
    print(f"❌ 无法获取合约信息"); sys.exit()

qty_raw=FIRST_SIZE/mark
qty_str=fmt_qty(qty_raw,step); qty_f=float(qty_str)
atr=top["atr"]

if sig_type=="reversal":
    sl_price=mark-atr*1.5; tp_price=mark+atr*3.0
else:
    sl_price=mark-atr*1.0; tp_price=mark+atr*4.0

sl_str=fmt_price(sl_price,tick); tp_str=fmt_price(tp_price,tick)
sl_loss=(mark-float(sl_str))*qty_f; tp_gain=(float(tp_str)-mark)*qty_f

print(f"\n📋 [仅扫描，不开仓] 信号方案")
print(f"  {sym} {sig_type}单 | 市价≈{mark:.4f} 名义={FIRST_SIZE}U")
print(f"  数量={qty_str} 止损={sl_str}(-{sl_loss:.1f}U) 止盈={tp_str}(+{tp_gain:.1f}U)")
print(f"  盈亏比=1:{tp_gain/sl_loss:.1f}")
print(f"  ⏸️ 当前为扫描模式，需手动确认后才开仓")
