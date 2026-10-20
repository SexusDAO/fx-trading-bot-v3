#!/usr/bin/env python3
"""
SNXXUSDT 逆市多单分析
检查 v2.3 在下跌过程中是否会触发抄底信号
"""

import json
import urllib.request
from datetime import datetime

def fetch_klines(symbol, limit=200):
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval=1h&limit={limit}"
    with urllib.request.urlopen(url, timeout=10) as response:
        data = json.loads(response.read())
        return [{
            'timestamp': k[0], 'open': float(k[1]), 'high': float(k[2]),
            'low': float(k[3]), 'close': float(k[4]), 'quote_volume': float(k[7])
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

def score_v23_long(klines):
    """v2.3 多单评分"""
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
    
    # Support (long: 越低分越高)
    score += 20 if p_pos<0.3 else (12 if p_pos<0.5 else (6 if p_pos<0.7 else 0))
    
    # Trend
    score += 10 if abs(p_change)>15 else (6 if abs(p_change)>8 else (3 if abs(p_change)>3 else 0))
    
    # Continuous
    score += 10 if cont else 0
    
    return min(score,100), {'rsi':rsi, 'rsi_pen':rp, 'macd':macd['histogram'], 
                            'ema':'多' if e9>e21>e55 else ('空' if e9<e21<e55 else '混'),
                            'vol':vol_ratio, 'pos':p_pos, 'cont':cont, 'e9':e9, 'e21':e21, 'e55':e55}

def main():
    print("="*80)
    print("SNXXUSDT 逆市多单分析 - v2.3 系统")
    print("问题：下跌过程中，系统会尝试抄底吗？")
    print("="*80)
    
    klines = fetch_klines('SNXXUSDT', 200)
    
    # 找到下跌阶段
    max_k = max(klines[:110], key=lambda k: k['high'])  # 前 110 根的最高点
    min_k = min(klines[:110], key=lambda k: k['low'])   # 前 110 根的最低点
    max_idx = klines.index(max_k)
    min_idx = klines.index(min_k)
    
    print(f"\n📊 下跌阶段：#{max_idx} → #{min_idx}")
    print(f"  起点：{datetime.utcfromtimestamp(max_k['timestamp']/1000)} ${max_k['high']:.2f}")
    print(f"  终点：{datetime.utcfromtimestamp(min_k['timestamp']/1000)} ${min_k['low']:.2f}")
    print(f"  跌幅：{(min_k['low']/max_k['high']-1)*100:-.1f}%")
    
    # 扫描下跌过程中的每个时点
    print(f"\n{'='*80}")
    print("下跌过程评分扫描（每 4 小时）")
    print(f"{'='*80}")
    
    print(f"\n{'时间':<12} {'价格':<8} {'RSI':<6} {'MACD':<8} {'EMA':<6} {'位置':<6} {'评分':<6} {'决策':<15}")
    print("-"*80)
    
    high_scores = []
    
    for i in range(max_idx + 10, min_idx, 4):  # 从下跌开始后 10 根 K 线开始
        snap = klines[:i+1]
        price = snap[-1]['close']
        ts = datetime.utcfromtimestamp(snap[-1]['timestamp']/1000)
        
        score, det = score_v23_long(snap)
        if not score: continue
        
        # 24H 涨跌检查
        skip = None
        if len(snap)>=24:
            chg24 = (snap[-1]['close']-snap[-24]['close'])/snap[-24]['close']*100
            if chg24 < -30: skip = f'暴跌 ({chg24:.0f}%)'
        else: chg24 = 0
        
        if score >= 60:
            high_scores.append({'idx':i, 'time':ts, 'price':price, 'score':score, 'det':det})
        
        decision = '开仓' if score>=70 else ('观察' if score>=60 else ('跳过' if score<50 else f'跳过({score}分)'))
        if skip: decision = f'跳过 ({skip})'
        
        # 打印关键点
        marker = '🔍' if score>=50 else '  '
        print(f"{marker} {ts.strftime('%m-%d %H:%M')} ${price:<7.2f} {det['rsi']:<6.0f} {det['macd']:<+.3f}   {det['ema']:<5} {det['pos']:<6.2f} {score:<6.0f} {decision:<15}")
    
    # 详细分析高分点
    print(f"\n{'='*80}")
    print("高分点详细分析（≥50 分）")
    print(f"{'='*80}")
    
    if not high_scores:
        print("\n❌ 下跌过程中没有任何时点评分≥60 分")
        print("\n原因:")
        print("1. EMA 空头排列（-20 分损失）")
        print("2. MACD 负值（-10 分损失）")
        print("3. 趋势分负向（下跌中为 0 分）")
        print("4. 连续 K 线确认不满足（下跌中为 0 分）")
    else:
        for hs in high_scores:
            print(f"\n#{hs['idx']} {hs['time'].strftime('%m-%d %H:%M')} ${hs['price']:.2f}")
            print(f"  评分：{hs['score']}分 ({'开仓' if hs['score']>=70 else '观察' if hs['score']>=60 else '接近'})")
            print(f"  RSI: {hs['det']['rsi']:.1f} (扣{hs['det']['rsi_pen']}分)")
            print(f"  MACD: {hs['det']['macd']:.3f}")
            print(f"  EMA: {hs['det']['ema']} (e9={hs['det']['e9']:.2f}, e21={hs['det']['e21']:.2f}, e55={hs['det']['e55']:.2f})")
            print(f"  成交量：{hs['det']['vol']:.2f}x")
            print(f"  价格位置：{hs['det']['pos']:.2f} (越低越接近底部)")
    
    # 关键问题解答
    print(f"\n{'='*80}")
    print("核心问题解答")
    print(f"{'='*80}")
    
    print("\n❓ 为什么 v2.3 不做逆市多单？")
    print("\n1️⃣ EMA 排列占 20 分权重")
    print("   下跌中 EMA 空头排列 → 只能拿 0-8 分（损失 12-20 分）")
    print("\n2️⃣ MACD 占 15 分权重")
    print("   下跌中 MACD 负值 → 只能拿 0-5 分（损失 10-15 分）")
    print("\n3️⃣ 连续 K 线确认占 10 分")
    print("   下跌中连续阴线 → 多单确认为 0 分")
    print("\n4️⃣ 趋势分占 10 分")
    print("   下跌中价格变化负向 → 多单趋势分为 0 分")
    print("\n📉 理论最高分（下跌中）:")
    print("   RSI(18) + MACD(5) + EMA(8) + Vol(20) + Support(20) + Trend(0) + Cont(0) = 71 分")
    print("   但实际 RSI 很难在 45-65 理想区间，通常<40 → 实际约 50-60 分")
    
    # 如果要支持逆市多单
    print(f"\n{'='*80}")
    print("如何支持逆市多单（左侧交易）？")
    print(f"{'='*80}")
    
    print("\n方案 1: 引入'超卖反弹'策略")
    print("  条件：RSI < 30 + 价格位置 < 0.2 + 成交量 > 2x")
    print("  评分：不要求 EMA 多头，改用 RSI 超卖分")
    print("  仓位：50%（左侧交易风险高）")
    
    print("\n方案 2: 降低 EMA 权重")
    print("  当前：EMA 20 分 → 调整为 10 分")
    print("  替代：成交量 20 分 → 25 分")
    
    print("\n方案 3: 双轨制")
    print("  趋势策略：≥70 分开仓（当前 v2.3）")
    print("  反转策略：RSI<30 + 支撑位强 → 60 分即可开仓（50% 仓位）")
    
    print(f"\n{'='*80}")
    print(f"分析完成：{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")

if __name__ == '__main__':
    main()
