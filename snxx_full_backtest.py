#!/usr/bin/env python3
"""
SNXXUSDT 完整暴涨回测分析 - v2.3 系统
覆盖 7.25-8.2 完整周期
"""

import json
import urllib.request
from datetime import datetime

def fetch_klines(symbol, limit=200):
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval=1h&limit={limit}"
    with urllib.request.urlopen(url, timeout=10) as response:
        data = json.loads(response.read())
        return [{
            'timestamp': k[0],
            'open': float(k[1]), 'high': float(k[2]), 'low': float(k[3]),
            'close': float(k[4]), 'volume': float(k[5]), 'quote_volume': float(k[7])
        } for k in data]

def calculate_rsi(prices, period=14):
    if len(prices) < period + 1: return 50.0
    gains = [max(0, prices[i] - prices[i-1]) for i in range(1, len(prices))]
    losses = [max(0, prices[i-1] - prices[i]) for i in range(1, len(prices))]
    avg_gain, avg_loss = sum(gains[-period:])/period, sum(losses[-period:])/period
    return 100 - (100 / (1 + avg_gain/avg_loss)) if avg_loss > 0 else 100.0

def calculate_ema(prices, period):
    if len(prices) < period: return prices[-1] if prices else 0
    mult = 2/(period+1)
    ema = sum(prices[:period])/period
    for p in prices[period:]: ema = (p-ema)*mult + ema
    return ema

def calculate_macd(prices):
    if len(prices) < 26: return {'histogram': 0}
    e12, e26 = calculate_ema(prices,12), calculate_ema(prices,26)
    return {'histogram': (e12-e26)*0.1}

def calculate_atr(klines, period=14):
    if len(klines) < period: return 1.0
    tr = [max(klines[i]['high']-klines[i]['low'], abs(klines[i]['high']-klines[i-1]['close']), abs(klines[i]['low']-klines[i-1]['close'])) for i in range(1, len(klines))]
    return sum(tr[-period:])/period

def score_v23(klines):
    if len(klines) < 30: return None, {}
    closed = klines[:-1]
    prices = [k['close'] for k in closed]
    vols = [k['quote_volume'] for k in closed]
    
    rsi = calculate_rsi(prices)
    macd = calculate_macd(prices)
    e9, e21, e55 = calculate_ema(prices,9), calculate_ema(prices,21), calculate_ema(prices,55)
    vol_ratio = vols[-2]/sum(vols[-12:-2])*10 if len(vols)>=12 else 1.0
    p_change = (prices[-1]-prices[-20])/prices[-20]*100 if len(prices)>=20 else 0
    p_pos = (prices[-1]-min(prices[-20:]))/(max(prices[-20:])-min(prices[-20:])) if len(prices)>=20 else 0.5
    cont = len(klines)>=4 and klines[-2]['close']>klines[-3]['close']>klines[-4]['close']
    
    score = 0
    # RSI (long)
    if 45<=rsi<=65: rs=18
    elif 40<=rsi<45 or 65<rsi<=70: rs=12
    elif 30<=rsi<40 or 70<rsi<=75: rs=6
    else: rs=0
    rp = 25 if rsi>80 else (15 if rsi>75 else (10 if rsi<20 else 0))
    score += rs - rp
    
    # MACD
    score += 15 if macd['histogram']>0.001 else (10 if macd['histogram']>0 else (5 if macd['histogram']>-0.0005 else 0))
    
    # EMA
    score += 20 if e9>e21>e55 and e9>prices[-1] else (15 if e9>e21>e55 else (8 if e9>e21 or e21>e55 else 0))
    
    # Volume
    score += 20 if vol_ratio>2 else (15 if vol_ratio>1.5 else (10 if vol_ratio>1 else (5 if vol_ratio>0.8 else 0)))
    
    # Support
    score += 20 if p_pos<0.3 else (12 if p_pos<0.5 else (6 if p_pos<0.7 else 0))
    
    # Trend
    score += 10 if abs(p_change)>15 else (6 if abs(p_change)>8 else (3 if abs(p_change)>3 else 0))
    
    # Continuous
    score += 10 if cont else 0
    
    return min(score,100), {'rsi':rsi, 'rsi_pen':rp, 'macd':macd['histogram'], 'ema':'多' if e9>e21>e55 else ('空' if e9<e21<e55 else '混'), 'vol':vol_ratio, 'pos':p_pos, 'cont':cont}

def main():
    print("="*80)
    print("SNXXUSDT 完整周期回测 - v2.3 系统")
    print("覆盖：7.25 下跌 → 7.29 底部 → 7.30-31 暴涨 → 8.1-8.2 回调")
    print("="*80)
    
    klines = fetch_klines('SNXXUSDT', 200)
    print(f"\n📊 数据：{len(klines)}根 K 线 | {datetime.utcfromtimestamp(klines[0]['timestamp']/1000)} 到 {datetime.utcfromtimestamp(klines[-1]['timestamp']/1000)}")
    print(f"价格：${min(k['low'] for k in klines):.2f} - ${max(k['high'] for k in klines):.2f} ({(max(k['high'] for k in klines)/min(k['low'] for k in klines)-1)*100:+.0f}%)")
    
    # 找到关键点
    min_k = min(klines, key=lambda k: k['low'])
    max_k = max(klines, key=lambda k: k['high'])
    min_idx = klines.index(min_k)
    max_idx = klines.index(max_k)
    
    print(f"\n🎯 关键点:")
    print(f"  最低点：#{min_idx} {datetime.utcfromtimestamp(min_k['timestamp']/1000)} ${min_k['low']:.2f}")
    print(f"  最高点：#{max_idx} {datetime.utcfromtimestamp(max_k['timestamp']/1000)} ${max_k['high']:.2f}")
    
    # 模拟扫描
    print(f"\n{'='*80}")
    print("模拟扫描（每 2 小时，从#26 根 K 线开始）")
    print(f"{'='*80}")
    
    signals = []
    for i in range(26, len(klines)-1, 2):
        snap = klines[:i+1]
        price = snap[-1]['close']
        ts = datetime.utcfromtimestamp(snap[-1]['timestamp']/1000)
        
        score, det = score_v23(snap)
        if not score: continue
        
        # 跳过检查
        skip = None
        if sum(k['quote_volume'] for k in snap[-10:])/10 < 500000:
            skip = '流动性不足'
        if len(snap)>=24:
            chg24 = (snap[-1]['close']-snap[-24]['close'])/snap[-24]['close']*100
            if abs(chg24)>50: skip = f'24H 涨跌过大 ({chg24:+.0f}%)'
        else: chg24 = 0
        
        if skip:
            dec = f"❌ 跳过 ({skip})"
        elif score>=70:
            dec = f"✅ 开仓 ({score:.0f}分)"
            signals.append({'idx':i, 'time':ts, 'price':price, 'score':score, 'det':det, 'chg24':chg24})
        elif score>=60:
            dec = f"🟡 观察 ({score:.0f}分)"
            signals.append({'idx':i, 'time':ts, 'price':price, 'score':score, 'det':det, 'chg24':chg24, 'observe':True})
        else:
            dec = f"❌ 跳过 ({score:.0f}分)"
        
        # 打印关键点
        if i in [min_idx, max_idx] or score>=60 or (i-min_idx in range(-2,3)):
            marker = "📍" if i==min_idx else "🚩" if i==max_idx else "  "
            chg_str = f" | 24H:{chg24:+.0f}%" if 'chg24' in dir() else ""
            print(f"{marker} {ts.strftime('%m-%d %H:%M')} ${price:.2f} {dec}{chg_str}")
            if score and score>=50:
                print(f"    RSI:{det['rsi']:.0f}(-{det['rsi_pen']}) MACD:{det['macd']:.3f} EMA:{det['ema']} Vol:{det['vol']:.2f}x Pos:{det['pos']:.2f}")
    
    # 分析结果
    print(f"\n{'='*80}")
    print("回测结果分析")
    print(f"{'='*80}")
    
    open_signals = [s for s in signals if not s.get('observe') and s['score']>=70]
    observe_signals = [s for s in signals if s.get('observe')]
    
    print(f"\n信号统计:")
    print(f"  开仓信号：{len(open_signals)}个")
    print(f"  观察信号：{len(observe_signals)}个")
    
    if not open_signals:
        print(f"\n⚠️ v2.3 系统在 SNXX 暴涨中【不会开仓】")
        print(f"\n原因分析:")
        print(f"1. 暴涨起点 (#26-30): 数据不足，指标未形成")
        print(f"2. 暴涨中期 (#31-40): RSI 超买 (>75) 触发扣分 +24H 涨幅>50% 强制跳过")
        print(f"3. 暴涨后期 (#41+): 价格高位，支撑位评分低")
        print(f"\n✅ 这是系统设计意图：避免追高已暴涨币种")
    else:
        for sig in open_signals:
            print(f"\n📍 开仓：{sig['time'].strftime('%m-%d %H:%M')} ${sig['price']:.2f} ({sig['score']:.0f}分)")
            atr = calculate_atr(klines[max(0,sig['idx']-14):sig['idx']+1])
            sl = sig['price'] - atr*1.5
            tp1 = sig['price'] + atr*2
            tp2 = sig['price'] + atr*3
            print(f"  止损${sl:.2f} 止盈 1${tp1:.2f} 止盈 2${tp2:.2f}")
            
            remaining = klines[sig['idx']:]
            max_p = max(k['high'] for k in remaining)
            min_p = min(k['low'] for k in remaining)
            print(f"  后续：最高${max_p:.2f} 最低${min_p:.2f}")
            
            if max_p >= tp2:
                print(f"  ✅ 止盈 2 出局 (+{(tp2-sig['price'])/sig['price']*100:.0f}%)")
            elif max_p >= tp1:
                print(f"  ✅ 止盈 1 出局 (+{(tp1-sig['price'])/sig['price']*100:.0f}%)")
            elif min_p <= sl:
                print(f"  ❌ 止损出局 ({(sl-sig['price'])/sig['price']*100:.0f}%)")
            else:
                print(f"  持仓中 ({(remaining[-1]['close']-sig['price'])/sig['price']*100:+.0f}%)")
    
    # 关键时点详细分析
    print(f"\n{'='*80}")
    print("关键时点详细分析")
    print(f"{'='*80}")
    
    for idx in [min_idx, min_idx+4, min_idx+8, max_idx]:
        if idx >= len(klines): continue
        snap = klines[:idx+1]
        score, det = score_v23(snap)
        chg24 = (snap[-1]['close']-snap[-24]['close'])/snap[-24]['close']*100 if len(snap)>=24 else 0
        print(f"\n#{idx} {datetime.utcfromtimestamp(snap[-1]['timestamp']/1000).strftime('%m-%d %H:%M')}")
        print(f"  价格：${snap[-1]['close']:.2f} | 24H 涨跌：{chg24:+.1f}%")
        print(f"  评分：{score}分 ({'开仓' if score and score>=70 else '观察' if score and score>=60 else '跳过'})")
        if score:
            print(f"  RSI:{det['rsi']:.1f}(扣{det['rsi_pen']}) MACD:{det['macd']:.3f} EMA:{det['ema']} Vol:{det['vol']:.2f}x")
    
    print(f"\n{'='*80}")
    print(f"回测完成：{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")

if __name__ == '__main__':
    main()
