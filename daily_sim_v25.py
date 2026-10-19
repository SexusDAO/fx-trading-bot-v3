#!/usr/bin/env python3
"""
2026-08-02 全天模拟测试 - v2.5 折中优化版
对比 v2.2 / v2.4 / v2.5 三个版本
"""

import json
import urllib.request
from datetime import datetime

# 内联 v2.5 评分函数
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

def score_v25(klines, position='long', is_low_cap=False):
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
    
    score = 20  # v2.5 基础分
    # RSI
    if position=='long':
        if 45<=rsi<=65: rs=18
        elif 40<=rsi<45 or 65<rsi<=70: rs=12
        elif 30<=rsi<40 or 70<rsi<=75: rs=6
        else: rs=0
        rp = 15 if rsi>75 else (10 if rsi>70 else (8 if rsi<20 else 0))
    else:
        if 35<=rsi<=55: rs=18
        elif 30<=rsi<35 or 55<rsi<=60: rs=12
        elif 25<=rsi<30 or 60<rsi<=70: rs=6
        else: rs=0
        rp = 15 if rsi<25 else (10 if rsi<30 else (8 if rsi>75 else 0))
    score += rs - rp
    # MACD
    score += 15 if macd['histogram']>0.001 else (10 if macd['histogram']>0 else (5 if macd['histogram']>-0.0005 else 0))
    # EMA
    score += 18 if e9>e21>e55 and e9>prices[-1] else (13 if e9>e21>e55 else (7 if e9>e21 or e21>e55 else 0))
    # Volume
    score += 18 if vol_ratio>2.0 else (13 if vol_ratio>1.5 else (10 if vol_ratio>1.0 else (5 if vol_ratio>0.8 else 0)))
    # Support
    score += 15 if (p_pos<0.3 or support_touches>=3) else (10 if (p_pos<0.5 or support_touches>=2) else (5 if p_pos<0.7 else 0))
    # Trend
    score += 10 if abs(p_change)>15 else (6 if abs(p_change)>8 else (3 if abs(p_change)>3 else 0))
    # Continuous
    score += 7 if consecutive>=2 else (4 if consecutive==1 else 0)
    # Low cap
    if is_low_cap: score -= 3
    
    return {'raw_score': score, 'final_score': min(score,100), 'details': {
        'rsi': rsi, 'rsi_score': rs, 'rsi_penalty': rp,
        'macd_score': 15 if macd['histogram']>0.001 else (10 if macd['histogram']>0 else (5 if macd['histogram']>-0.0005 else 0)),
        'ema_score': 18 if e9>e21>e55 and e9>prices[-1] else (13 if e9>e21>e55 else (7 if e9>e21 or e21>e55 else 0)),
        'ema_pattern': '多' if e9>e21>e55 else ('空' if e9<e21<e55 else '混'),
        'vol_ratio': vol_ratio, 'vol_score': 18 if vol_ratio>2.0 else (13 if vol_ratio>1.5 else (10 if vol_ratio>1.0 else (5 if vol_ratio>0.8 else 0))),
        'support_score': 15 if (p_pos<0.3 or support_touches>=3) else (10 if (p_pos<0.5 or support_touches>=2) else (5 if p_pos<0.7 else 0)),
        'trend_score': 10 if abs(p_change)>15 else (6 if abs(p_change)>8 else (3 if abs(p_change)>3 else 0)),
        'confirm_score': 7 if consecutive>=2 else (4 if consecutive==1 else 0),
        'consecutive': consecutive, 'price_pos': p_pos, 'price_change': p_change, 'macd': macd['histogram']
    }}

def score_v22(klines):
    """简化版 v2.2（基础分 50）"""
    if len(klines) < 30: return None, {}
    closed = klines[:-1]
    prices = [k['close'] for k in closed]
    vols = [k['quote_volume'] for k in closed]
    rsi = calculate_rsi(prices)
    macd = calculate_macd(prices)
    e9, e21, e55 = calculate_ema(prices,9), calculate_ema(prices,21), calculate_ema(prices,55)
    vol_ratio = vols[-1]/sum(vols[-10:])*10 if len(vols)>=10 else 1.0
    p_change = (prices[-1]-prices[-20])/prices[-20]*100 if len(prices)>=20 else 0
    
    score = 50  # v2.2 基础分
    score += 10 if 40<=rsi<=60 else (5 if 30<=rsi<40 or 60<rsi<=70 else 0)
    score += 10 if macd['histogram']>0 else (5 if macd['histogram']>-0.001 else 0)
    score += 15 if e9>e21>e55 else (5 if e9<e21<e55 else 5)
    score += 15 if vol_ratio>2.0 else (10 if vol_ratio>1.5 else (5 if vol_ratio>1.0 else 0))
    score += 10 if abs(p_change)>10 else (5 if abs(p_change)>5 else 0)
    return min(score, 100), {'rsi': rsi, 'vol_ratio': vol_ratio}

def score_v24(klines, is_low_cap=False):
    """简化版 v2.4 Final"""
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
    consecutive = count_consecutive(klines, 'long')
    
    score = 0
    if 48<=rsi<=62: rs=20
    elif 42<=rsi<48 or 62<rsi<=68: rs=12
    elif 32<=rsi<42 or 68<rsi<=74: rs=6
    else: rs=0
    rp = 25 if rsi>82 else (18 if rsi>78 else (12 if rsi>74 else (10 if rsi<20 else 0)))
    score += rs - rp
    score += 16 if macd['histogram']>0.001 else (10 if macd['histogram']>0 else (5 if macd['histogram']>-0.0005 else 0))
    score += 18 if e9>e21>e55 and e9>prices[-1] else (13 if e9>e21>e55 else (7 if e9>e21 or e21>e55 else 0))
    score += 18 if vol_ratio>2.2 else (13 if vol_ratio>1.6 else (10 if vol_ratio>1.1 else (5 if vol_ratio>0.85 else 0)))
    score += 18 if (p_pos<0.28) else (13 if p_pos<0.45 else (7 if p_pos<0.65 else 0))
    score += 10 if abs(p_change)>18 else (6 if abs(p_change)>10 else (3 if abs(p_change)>4 else 0))
    score += 8 if consecutive>=2 else (5 if consecutive==1 else 0)
    if is_low_cap: score -= 5
    return min(score, 100), {'rsi': rsi, 'rsi_pen': rp, 'vol_ratio': vol_ratio}

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
    print("="*100)
    print("2026-08-02 模拟测试 - v2.5 折中优化版")
    print("对比：v2.2 vs v2.4 vs v2.5")
    print("="*100)
    
    tickers = fetch_tickers()
    candidates = [t for t in tickers if t['symbol'].endswith('USDT') and float(t['quoteVolume']) > 1000000]
    gainers = sorted(candidates, key=lambda x: float(x['priceChangePercent']), reverse=True)[:20]
    losers = sorted(candidates, key=lambda x: float(x['priceChangePercent']))[:20]
    by_vol = sorted(candidates, key=lambda x: float(x['quoteVolume']), reverse=True)[:60]
    symbols = list(set([t['symbol'] for t in gainers + losers + by_vol]))[:80]
    
    print(f"\n📊 候选标的：{len(symbols)}个")
    
    comparison = {'v22': [], 'v24': [], 'v25': []}
    
    for sym in symbols:
        ticker = next((t for t in tickers if t['symbol']==sym), None)
        if not ticker: continue
        change = float(ticker['priceChangePercent'])
        volume = float(ticker['quoteVolume'])
        if volume < 1000000 or abs(change) > 70: continue
        klines = fetch_klines(sym, limit=100)
        if not klines or len(klines) < 30: continue
        
        is_low_cap = volume < 10000000
        
        s22, _ = score_v22(klines)
        s24, _ = score_v24(klines, is_low_cap)
        res25 = score_v25(klines, 'long', is_low_cap)
        s25 = res25['final_score'] if res25 else None
        
        if s22 and s24 and s25:
            d25 = res25['details']
            comparison['v22'].append({'symbol': sym, 'score': s22, 'change': change})
            comparison['v24'].append({'symbol': sym, 'score': s24, 'change': change, 'details': d25})
            comparison['v25'].append({'symbol': sym, 'score': s25, 'change': change, 'details': d25, 'is_low_cap': is_low_cap})
    
    # 统计对比
    print(f"\n{'='*100}")
    print("三版本对比")
    print(f"{'='*100}")
    
    for ver in ['v22', 'v24', 'v25']:
        data = comparison[ver]
        avg_score = sum(c['score'] for c in data) / len(data)
        open_count = sum(1 for c in data if c['score'] >= 70 or (ver=='v25' and c['is_low_cap'] and c['score']>=67))
        observe_count = sum(1 for c in data if (55 if ver=='v25' else 60) <= c['score'] < 70)
        
        print(f"\n{ver.upper()}:")
        print(f"  平均分：{avg_score:.1f}分")
        print(f"  开仓数（≥70 分）：{open_count}个 ({open_count/len(data)*100:.1f}%)")
        print(f"  观察区：{observe_count}个 ({observe_count/len(data)*100:.1f}%)")
    
    # v2.5 详细分析
    print(f"\n{'='*100}")
    print("v2.5 详细分析")
    print(f"{'='*100}")
    
    v25_data = comparison['v25']
    open_signals = sorted([c for c in v25_data if c['score'] >= 70 or (c['is_low_cap'] and c['score'] >= 67)], key=lambda x: x['score'], reverse=True)
    observe_signals = [c for c in v25_data if 55 <= c['score'] < 70 and not (c['is_low_cap'] and c['score'] >= 67)]
    
    print(f"\n【开仓信号】{len(open_signals)}个")
    for c in open_signals[:15]:
        d = c['details']
        flag = ' [低]' if c['is_low_cap'] else ''
        print(f"  {c['symbol']}: {c['score']:.0f}分 | RSI={d['rsi']:.0f} Vol={d['vol_ratio']:.2f}x EMA={d['ema_pattern']}{flag}")
    
    # RSI 超买检查
    high_rsi = [c for c in v25_data if c['details']['rsi'] > 70]
    high_rsi_open = [c for c in high_rsi if c['score'] >= 70 or (c['is_low_cap'] and c['score'] >= 67)]
    
    print(f"\n【RSI 超买拦截】")
    print(f"  RSI>70 的标的：{len(high_rsi)}个")
    print(f"  被错误开仓：{len(high_rsi_open)}个")
    for c in high_rsi[:5]:
        d = c['details']
        status = '⚠️ 开仓' if c['score']>=70 or (c['is_low_cap'] and c['score']>=67) else '✅ 拦截'
        print(f"    {c['symbol']}: RSI={d['rsi']:.0f} 评分={c['score']:.0f}分 {status}")
    
    # 评分分布
    print(f"\n【评分分布】")
    v25_scores = [c['score'] for c in v25_data]
    for low, high in [(0,40), (40,50), (50,60), (60,70), (70,80), (80,100)]:
        count = sum(1 for s in v25_scores if low <= s < high)
        print(f"  {low}-{high}分：{count}个")
    
    # 目标达成检查
    print(f"\n{'='*100}")
    print("v2.5 目标达成检查")
    print(f"{'='*100}")
    
    open_rate = len(open_signals)/len(v25_data)*100
    avg_score = sum(c['score'] for c in v25_data) / len(v25_data)
    neutral = [c for c in v25_data if 55 <= c['score'] <= 65]
    avg_neutral = sum(c['score'] for c in neutral)/len(neutral) if neutral else 0
    
    print(f"""
目标                        实际结果              状态
─────────────────────────────────────────────────────────
开仓率 20-30%             {open_rate:.1f}%              {'✅' if 20<=open_rate<=30 else '⚠️'}
平均分 60-70 分           {avg_score:.1f}分              {'✅' if 60<=avg_score<=70 else '⚠️'}
中性信号 60-65 分         {avg_neutral:.0f}分              {'✅' if 60<=avg_neutral<=65 else '⚠️'}
RSI 超买拦截              {len(high_rsi)-len(high_rsi_open)}/{len(high_rsi)}个             {'✅' if len(high_rsi_open)==0 else '⚠️'}
""")
    
    # 结论
    all_pass = (20<=open_rate<=35) and (len(high_rsi_open)==0)
    print(f"\n{'='*100}")
    if all_pass:
        print("✅ v2.5 折中方案成功！达到预期目标")
    else:
        print("⚠️ v2.5 基本可用，可能需要微调")
    print(f"{'='*100}")
    print(f"\n报告生成：{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")

if __name__ == '__main__':
    main()
