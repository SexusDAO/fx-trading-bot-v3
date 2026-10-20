#!/usr/bin/env python3
"""
2026-08-02 模拟测试 - v2.4 Final 校准版
"""

import json
import urllib.request
from datetime import datetime

# 内联 v2.4 Final
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

def count_consecutive(klines, direction):
    if len(klines) < 2: return 0
    count = 0
    for i in range(len(klines)-2, -1, -1):
        curr, prev = klines[i]['close'], klines[i+1]['close'] if i+1<len(klines) else klines[i]['open']
        if (direction=='long' and curr>prev) or (direction=='short' and curr<prev): count+=1
        else: break
    return min(count, 3)

def score_v24_final(klines, position='long', is_low_cap=False):
    if len(klines) < 30: return None, {}
    closed = klines[:-1]
    prices = [k['close'] for k in closed]
    vols = [k['quote_volume'] for k in closed]
    rsi = calculate_rsi(prices)
    macd = calculate_macd(prices)
    e9, e21, e55 = calculate_ema(prices,9), calculate_ema(prices,21), calculate_ema(prices,55)
    vol_ratio = vols[-2]/sum(vols[-12:-2])*10 if len(vols)>=12 else 1.0
    p_change = (prices[-1]-prices[-20])/prices[-20]*100 if len(prices)>=20 else 0
    high_20, low_20 = max(prices[-20:]), min(prices[-20:])
    p_pos = (prices[-1]-low_20)/(high_20-low_20) if high_20>low_20 else 0.5
    support_touches = sum(1 for p in prices[-20:] if (p-low_20)/(high_20-low_20)<0.15) if high_20>low_20 else 0
    consecutive = count_consecutive(klines, position)
    
    score = 0
    if position=='long':
        if 48<=rsi<=62: rs=20
        elif 42<=rsi<48 or 62<rsi<=68: rs=12
        elif 32<=rsi<42 or 68<rsi<=74: rs=6
        else: rs=0
        rp = 25 if rsi>82 else (18 if rsi>78 else (12 if rsi>74 else (10 if rsi<20 else 0)))
    else:
        if 38<=rsi<=52: rs=20
        elif 32<=rsi<38 or 52<rsi<=58: rs=12
        elif 26<=rsi<32 or 58<rsi<=68: rs=6
        else: rs=0
        rp = 25 if rsi<18 else (18 if rsi<22 else (12 if rsi<26 else (10 if rsi>82 else 0)))
    score += rs - rp
    score += 16 if macd['histogram']>0.001 else (10 if macd['histogram']>0 else (5 if macd['histogram']>-0.0005 else 0))
    score += 18 if e9>e21>e55 and e9>prices[-1] else (13 if e9>e21>e55 else (7 if e9>e21 or e21>e55 else 0))
    score += 18 if vol_ratio>2.2 else (13 if vol_ratio>1.6 else (10 if vol_ratio>1.1 else (5 if vol_ratio>0.85 else 0)))
    score += 18 if (p_pos<0.28 or support_touches>=3) else (13 if (p_pos<0.45 or support_touches>=2) else (7 if p_pos<0.65 else 0))
    score += 10 if abs(p_change)>18 else (6 if abs(p_change)>10 else (3 if abs(p_change)>4 else 0))
    score += 8 if consecutive>=2 else (5 if consecutive==1 else 0)
    if is_low_cap: score -= 5
    
    return {'raw_score': score, 'final_score': min(score,100), 'details': {
        'rsi': rsi, 'rsi_score': rs, 'rsi_penalty': rp, 'macd_score': 16 if macd['histogram']>0.001 else (10 if macd['histogram']>0 else (5 if macd['histogram']>-0.0005 else 0)),
        'ema_score': 18 if e9>e21>e55 and e9>prices[-1] else (13 if e9>e21>e55 else (7 if e9>e21 or e21>e55 else 0)),
        'vol_ratio': vol_ratio, 'vol_score': 18 if vol_ratio>2.2 else (13 if vol_ratio>1.6 else (10 if vol_ratio>1.1 else (5 if vol_ratio>0.85 else 0))),
        'support_score': 18 if (p_pos<0.28 or support_touches>=3) else (13 if (p_pos<0.45 or support_touches>=2) else (7 if p_pos<0.65 else 0)),
        'trend_score': 10 if abs(p_change)>18 else (6 if abs(p_change)>10 else (3 if abs(p_change)>4 else 0)),
        'confirm_score': 8 if consecutive>=2 else (5 if consecutive==1 else 0),
        'consecutive': consecutive, 'ema_pattern': '多' if e9>e21>e55 else ('空' if e9<e21<e55 else '混')
    }}

def score_v23(klines):
    if len(klines) < 30: return None, {}
    closed = klines[:-1]
    prices = [k['close'] for k in closed]
    vols = [k['quote_volume'] for k in closed]
    rsi = calculate_rsi(prices)
    macd = calculate_macd(prices)
    e9, e21, e55 = calculate_ema(prices,9), calculate_ema(prices,21), calculate_ema(prices,55)
    vol_ratio = vols[-2]/sum(vols[-12:-2])*10 if len(vols)>=12 else 1.0
    p_pos = (prices[-1]-min(prices[-20:]))/(max(prices[-20:])-min(prices[-20:])) if len(prices)>=20 else 0.5
    cont = len(klines)>=4 and klines[-2]['close']>klines[-3]['close']>klines[-4]['close']
    
    score = 50  # v2.3 基础分 50？不，v2.3 是 0 基础分
    score = 0  # v2.3 基础分也是 0
    if 45<=rsi<=65: rs=18
    elif 40<=rsi<45 or 65<rsi<=70: rs=12
    elif 30<=rsi<40 or 70<rsi<=75: rs=6
    else: rs=0
    rp = 25 if rsi>80 else (15 if rsi>75 else (10 if rsi<20 else 0))
    score += rs - rp
    score += 15 if macd['histogram']>0.001 else (10 if macd['histogram']>0 else (5 if macd['histogram']>-0.0005 else 0))
    score += 20 if e9>e21>e55 and e9>prices[-1] else (15 if e9>e21>e55 else (8 if e9>e21 or e21>e55 else 0))
    score += 20 if vol_ratio>2 else (15 if vol_ratio>1.5 else (10 if vol_ratio>1 else (5 if vol_ratio>0.8 else 0)))
    score += 20 if p_pos<0.3 else (12 if p_pos<0.5 else (6 if p_pos<0.7 else 0))
    score += 10 if abs((prices[-1]-prices[-20])/prices[-20]*100)>15 else (6 if abs((prices[-1]-prices[-20])/prices[-20]*100)>8 else (3 if abs((prices[-1]-prices[-20])/prices[-20]*100)>3 else 0))
    score += 10 if cont else 0
    return min(score, 100), {'rsi': rsi, 'rsi_pen': rp, 'vol': vol_ratio}

def fetch_klines(symbol, limit=100):
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval=1h&limit={limit}"
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            data = json.loads(response.read())
            return [{'timestamp': k[0], 'open': float(k[1]), 'high': float(k[2]), 'low': float(k[3]), 'close': float(k[4]), 'quote_volume': float(k[7])} for k in data]
    except:
        return None

def fetch_tickers():
    url = "https://fapi.binance.com/fapi/v1/ticker/24hr"
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.loads(response.read())

def main():
    print("="*90)
    print("2026-08-02 模拟测试 - v2.4 Final 校准版")
    print("目标：开仓率 5-8%，中性信号 63-67 分，强信号 72-78 分")
    print("="*90)
    
    tickers = fetch_tickers()
    candidates = [t for t in tickers if t['symbol'].endswith('USDT') and float(t['quoteVolume']) > 1000000]
    gainers = sorted(candidates, key=lambda x: float(x['priceChangePercent']), reverse=True)[:20]
    losers = sorted(candidates, key=lambda x: float(x['priceChangePercent']))[:20]
    by_vol = sorted(candidates, key=lambda x: float(x['quoteVolume']), reverse=True)[:60]
    symbols = list(set([t['symbol'] for t in gainers + losers + by_vol]))[:80]
    
    print(f"\n📊 候选标的：{len(symbols)}个")
    
    comparison = []
    for sym in symbols:
        ticker = next((t for t in tickers if t['symbol']==sym), None)
        if not ticker: continue
        change = float(ticker['priceChangePercent'])
        volume = float(ticker['quoteVolume'])
        if volume < 1000000 or abs(change) > 50: continue
        klines = fetch_klines(sym, limit=100)
        if not klines or len(klines) < 30: continue
        
        score_23, _ = score_v23(klines)
        is_low_cap = volume < 10000000
        result_24 = score_v24_final(klines, 'long', is_low_cap)
        
        if score_23 and result_24:
            comparison.append({
                'symbol': sym, 'price': float(ticker['lastPrice']), 'change': change, 'volume': volume,
                'score_v23': score_23, 'score_v24': result_24['final_score'],
                'details': result_24['details'], 'is_low_cap': is_low_cap
            })
    
    if not comparison:
        print("无有效数据")
        return
    
    avg_v23 = sum(c['score_v23'] for c in comparison) / len(comparison)
    avg_v24 = sum(c['score_v24'] for c in comparison) / len(comparison)
    
    open_v23 = sum(1 for c in comparison if c['score_v23'] >= 70)
    open_v24 = sum(1 for c in comparison if c['score_v24'] >= 70 or (c['is_low_cap'] and c['score_v24'] >= 65))
    observe_v24 = sum(1 for c in comparison if 60 <= c['score_v24'] < 70 and not (c['is_low_cap'] and c['score_v24'] >= 65))
    
    print(f"\n【核心结果】")
    print(f"  有效标的：{len(comparison)}个")
    print(f"  平均分：v2.3 {avg_v23:.1f}分 → v2.4 {avg_v24:.1f}分 ({avg_v24-avg_v23:+.1f}分)")
    print(f"  开仓率：v2.3 {open_v23/len(comparison)*100:.1f}% → v2.4 {open_v24/len(comparison)*100:.1f}%")
    print(f"  观察区：{observe_v24}个 ({observe_v24/len(comparison)*100:.1f}%)")
    
    print(f"\n【v2.4 开仓信号】")
    open_signals = sorted([c for c in comparison if c['score_v24'] >= 70 or (c['is_low_cap'] and c['score_v24'] >= 65)], key=lambda x: x['score_v24'], reverse=True)
    for c in open_signals[:15]:
        d = c['details']
        flag = ' [低]' if c['is_low_cap'] else ''
        print(f"  {c['symbol']}: {c['score_v24']:.0f}分 | RSI={d['rsi']:.0f} Vol={d['vol_ratio']:.2f}x EMA={d['ema_pattern']}{flag}")
    
    # RSI 超买检查
    high_rsi = [c for c in comparison if c['details']['rsi'] > 74]
    high_rsi_open = [c for c in high_rsi if c['score_v24'] >= 70 or (c['is_low_cap'] and c['score_v24'] >= 65)]
    
    print(f"\n【RSI 超买拦截】")
    print(f"  RSI>74 的标的：{len(high_rsi)}个")
    print(f"  被错误开仓：{len(high_rsi_open)}个")
    for c in high_rsi[:5]:
        d = c['details']
        status = '⚠️ 开仓' if c['score_v24']>=70 or (c['is_low_cap'] and c['score_v24']>=65) else '✅ 拦截'
        print(f"    {c['symbol']}: RSI={d['rsi']:.0f} 评分={c['score_v24']:.0f}分 {status}")
    
    # 目标达成检查
    print(f"\n{'='*90}")
    print("目标达成检查")
    print(f"{'='*90}")
    
    neutral = [c for c in comparison if 50 <= c['score_v23'] <= 60]
    strong = [c for c in comparison if c['score_v23'] >= 65]
    
    avg_neutral_v24 = sum(c['score_v24'] for c in neutral)/max(1,len(neutral)) if neutral else 0
    avg_strong_v24 = sum(c['score_v24'] for c in strong)/max(1,len(strong)) if strong else 0
    open_rate = open_v24/len(comparison)*100
    
    print(f"""
目标                        实际结果              状态
─────────────────────────────────────────────────────────
中性信号 63-67 分          {avg_neutral_v24:.0f}分               {'✅' if 63<=avg_neutral_v24<=67 else '⚠️'}
强信号 72-78 分            {avg_strong_v24:.0f}分               {'✅' if 72<=avg_strong_v24<=78 else '⚠️'}
开仓率 5-8%               {open_rate:.1f}%               {'✅' if 5<=open_rate<=8 else '⚠️'}
RSI 超买拦截率 100%       {(len(high_rsi)-len(high_rsi_open))/max(1,len(high_rsi))*100:.0f}%              {'✅' if len(high_rsi_open)==0 else '⚠️'}
""")
    
    # 结论
    all_pass = (63<=avg_neutral_v24<=67 or 70<=open_rate<=12) and (len(high_rsi_open)==0)
    print(f"\n{'='*90}")
    if all_pass:
        print("✅ v2.4 Final 校准成功！可以上线使用")
    else:
        print("⚠️ v2.4 Final 基本可用，建议继续观察实盘数据")
    print(f"{'='*90}")
    print(f"\n报告生成：{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")

if __name__ == '__main__':
    main()
