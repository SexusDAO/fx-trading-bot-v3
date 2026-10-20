#!/usr/bin/env python3
"""
2026-08-02 全天模拟测试 - v2.4 校准版
对比 v2.3 和 v2.4 的评分差异
"""

import json
import urllib.request
from datetime import datetime
# 内联 v2.4 评分函数（避免导入问题）
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

def score_v24(klines, ticker=None, position='long', is_low_cap=False):
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
    # RSI
    if position=='long':
        if 45<=rsi<=65: rs=22
        elif 40<=rsi<45 or 65<rsi<=70: rs=14
        elif 30<=rsi<40 or 70<rsi<=75: rs=8
        else: rs=0
        rp = 25 if rsi>80 else (15 if rsi>75 else (10 if rsi<20 else 0))
    else:
        if 35<=rsi<=55: rs=22
        elif 30<=rsi<35 or 55<rsi<=60: rs=14
        elif 25<=rsi<30 or 60<rsi<=70: rs=8
        else: rs=0
        rp = 25 if rsi<20 else (15 if rsi<25 else (10 if rsi>80 else 0))
    score += rs - rp
    # MACD
    score += 18 if macd['histogram']>0.001 else (12 if macd['histogram']>0 else (6 if macd['histogram']>-0.0005 else 0))
    # EMA
    score += 20 if e9>e21>e55 and e9>prices[-1] else (15 if e9>e21>e55 else (8 if e9>e21 or e21>e55 else 0))
    # Volume
    score += 22 if vol_ratio>2 else (16 if vol_ratio>1.5 else (12 if vol_ratio>1 else (6 if vol_ratio>0.8 else 0)))
    # Support
    score += 20 if p_pos<0.3 or support_touches>=3 else (16 if p_pos<0.5 or support_touches>=2 else (8 if p_pos<0.7 else 0))
    # Trend
    score += 10 if abs(p_change)>15 else (6 if abs(p_change)>8 else (3 if abs(p_change)>3 else 0))
    # Continuous
    score += 10 if consecutive>=2 else (6 if consecutive==1 else 0)
    # Low cap
    if is_low_cap: score -= 5
    
    return {'raw_score': score, 'final_score': min(score,100), 'details': {
        'rsi': rsi, 'rsi_score': rs, 'rsi_penalty': rp, 'macd_score': 18 if macd['histogram']>0.001 else (12 if macd['histogram']>0 else (6 if macd['histogram']>-0.0005 else 0)),
        'ema_score': 20 if e9>e21>e55 and e9>prices[-1] else (15 if e9>e21>e55 else (8 if e9>e21 or e21>e55 else 0)),
        'vol_ratio': vol_ratio, 'vol_score': 22 if vol_ratio>2 else (16 if vol_ratio>1.5 else (12 if vol_ratio>1 else (6 if vol_ratio>0.8 else 0))),
        'support_score': 20 if p_pos<0.3 or support_touches>=3 else (16 if p_pos<0.5 or support_touches>=2 else (8 if p_pos<0.7 else 0)),
        'trend_score': 10 if abs(p_change)>15 else (6 if abs(p_change)>8 else (3 if abs(p_change)>3 else 0)),
        'confirm_score': 10 if consecutive>=2 else (6 if consecutive==1 else 0),
        'consecutive': consecutive, 'price_pos': p_pos, 'support_touches': support_touches, 'price_change': p_change,
        'ema_pattern': '多' if e9>e21>e55 else ('空' if e9<e21<e55 else '混'), 'macd': macd['histogram']
    }}

def fetch_klines(symbol, limit=100):
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval=1h&limit={limit}"
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            data = json.loads(response.read())
            return [{
                'timestamp': k[0], 'open': float(k[1]), 'high': float(k[2]),
                'low': float(k[3]), 'close': float(k[4]), 'quote_volume': float(k[7])
            } for k in data]
    except:
        return None

def fetch_tickers():
    url = "https://fapi.binance.com/fapi/v1/ticker/24hr"
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.loads(response.read())

def score_v23_simple(klines):
    """简化版 v2.3 评分（用于对比）"""
    if len(klines) < 30:
        return None, {}
    closed = klines[:-1]
    prices = [k['close'] for k in closed]
    vols = [k['quote_volume'] for k in closed]
    
    rsi = calculate_rsi(prices)
    macd = calculate_macd(prices)
    e9, e21, e55 = calculate_ema(prices,9), calculate_ema(prices,21), calculate_ema(prices,55)
    vol_ratio = vols[-2]/sum(vols[-12:-2])*10 if len(vols)>=12 else 1.0
    p_pos = (prices[-1]-min(prices[-20:]))/(max(prices[-20:])-min(prices[-20:])) if len(prices)>=20 else 0.5
    cont = len(klines)>=4 and klines[-2]['close']>klines[-3]['close']>klines[-4]['close']
    
    score = 0
    # RSI v2.3
    if 45<=rsi<=65: rs=18
    elif 40<=rsi<45 or 65<rsi<=70: rs=12
    elif 30<=rsi<40 or 70<rsi<=75: rs=6
    else: rs=0
    rp = 25 if rsi>80 else (15 if rsi>75 else (10 if rsi<20 else 0))
    score += rs - rp
    # MACD v2.3
    score += 15 if macd['histogram']>0.001 else (10 if macd['histogram']>0 else (5 if macd['histogram']>-0.0005 else 0))
    # EMA v2.3
    score += 20 if e9>e21>e55 and e9>prices[-1] else (15 if e9>e21>e55 else (8 if e9>e21 or e21>e55 else 0))
    # Volume v2.3
    score += 20 if vol_ratio>2 else (15 if vol_ratio>1.5 else (10 if vol_ratio>1 else (5 if vol_ratio>0.8 else 0)))
    # Support v2.3
    score += 20 if p_pos<0.3 else (12 if p_pos<0.5 else (6 if p_pos<0.7 else 0))
    # Trend
    score += 10 if abs((prices[-1]-prices[-20])/prices[-20]*100)>15 else (6 if abs((prices[-1]-prices[-20])/prices[-20]*100)>8 else (3 if abs((prices[-1]-prices[-20])/prices[-20]*100)>3 else 0))
    # Continuous v2.3
    score += 10 if cont else 0
    
    return min(score, 100), {'rsi':rsi, 'rsi_pen':rp, 'vol':vol_ratio}

def main():
    print("="*90)
    print("2026-08-02 模拟测试 - v2.4 校准版 vs v2.3 对比")
    print("="*90)
    
    tickers = fetch_tickers()
    candidates = [t for t in tickers if t['symbol'].endswith('USDT') and float(t['quoteVolume']) > 1000000]
    
    # 获取候选标的
    gainers = sorted(candidates, key=lambda x: float(x['priceChangePercent']), reverse=True)[:20]
    losers = sorted(candidates, key=lambda x: float(x['priceChangePercent']))[:20]
    by_vol = sorted(candidates, key=lambda x: float(x['quoteVolume']), reverse=True)[:60]
    symbols = list(set([t['symbol'] for t in gainers + losers + by_vol]))[:80]
    
    print(f"\n📊 候选标的：{len(symbols)}个")
    
    # 存储对比结果
    comparison = []
    
    for sym in symbols:
        ticker = next((t for t in tickers if t['symbol']==sym), None)
        if not ticker:
            continue
        
        change = float(ticker['priceChangePercent'])
        volume = float(ticker['quoteVolume'])
        price = float(ticker['lastPrice'])
        
        # 强制跳过检查
        if volume < 1000000 or abs(change) > 50:
            continue
        
        klines = fetch_klines(sym, limit=100)
        if not klines or len(klines) < 30:
            continue
        
        # v2.3 评分
        score_23, det_23 = score_v23_simple(klines)
        
        # v2.4 评分
        is_low_cap = volume < 10000000  # 简单判断：成交额<1000 万为低市值
        result_24 = score_v24(klines, ticker, 'long', is_low_cap)
        
        if score_23 and result_24:
            score_24 = result_24['final_score']
            comparison.append({
                'symbol': sym,
                'price': price,
                'change': change,
                'volume': volume,
                'score_v23': score_23,
                'score_v24': score_24,
                'diff': score_24 - score_23,
                'details_v24': result_24['details'],
                'is_low_cap': is_low_cap
            })
    
    # 分析结果
    print(f"\n{'='*90}")
    print("v2.3 vs v2.4 评分对比")
    print(f"{'='*90}")
    
    if not comparison:
        print("无有效数据")
        return
    
    # 统计
    avg_v23 = sum(c['score_v23'] for c in comparison) / len(comparison)
    avg_v24 = sum(c['score_v24'] for c in comparison) / len(comparison)
    avg_diff = sum(c['diff'] for c in comparison) / len(comparison)
    
    print(f"\n【整体统计】")
    print(f"  有效标的：{len(comparison)}个")
    print(f"  v2.3 平均分：{avg_v23:.1f}分")
    print(f"  v2.4 平均分：{avg_v24:.1f}分")
    print(f"  平均提升：{avg_diff:+.1f}分")
    
    # 开仓率对比
    open_v23 = sum(1 for c in comparison if c['score_v23'] >= 70)
    observe_v23 = sum(1 for c in comparison if 60 <= c['score_v23'] < 70)
    open_v24 = sum(1 for c in comparison if c['score_v24'] >= 70 or (c['is_low_cap'] and c['score_v24'] >= 68))
    observe_v24 = sum(1 for c in comparison if 60 <= c['score_v24'] < 70 and not (c['is_low_cap'] and c['score_v24'] >= 68))
    
    print(f"\n【开仓率对比】")
    print(f"  v2.3: 开仓{open_v23}个 ({open_v23/len(comparison)*100:.1f}%) | 观察区{observe_v23}个 ({observe_v23/len(comparison)*100:.1f}%)")
    print(f"  v2.4: 开仓{open_v24}个 ({open_v24/len(comparison)*100:.1f}%) | 观察区{observe_v24}个 ({observe_v24/len(comparison)*100:.1f}%)")
    print(f"  开仓率变化：{(open_v24-open_v23)/len(comparison)*100:+.1f}个百分点")
    
    # 详细对比表
    print(f"\n{'='*90}")
    print("详细对比（按 v2.4 评分排序）")
    print(f"{'='*90}")
    print(f"{'标的':<12} {'v2.3':<8} {'v2.4':<8} {'提升':<8} {'24H':<10} {'RSI':<6} {'量比':<8} {'状态':<10}")
    print("-"*90)
    
    for c in sorted(comparison, key=lambda x: x['score_v24'], reverse=True)[:20]:
        d = c['details_v24']
        status = '✅ 开仓' if c['score_v24'] >= 70 or (c['is_low_cap'] and c['score_v24'] >= 68) else ('🟡 观察' if c['score_v24'] >= 60 else '❌ 跳过')
        low_cap_flag = ' [低]' if c['is_low_cap'] else ''
        print(f"{c['symbol']:<12} {c['score_v23']:<8.0f} {c['score_v24']:<8.0f} {c['diff']:<+.1f}    {c['change']:<+.1f}%    {d['rsi']:<6.0f} {d['vol_ratio']:<8.2f}x {status:<10}{low_cap_flag}")
    
    # 分数分布对比
    print(f"\n{'='*90}")
    print("分数分布对比")
    print(f"{'='*90}")
    
    def count_range(items, low, high):
        return sum(1 for c in items if low <= c < high)
    
    v23_scores = [c['score_v23'] for c in comparison]
    v24_scores = [c['score_v24'] for c in comparison]
    
    print(f"\n分数区间{'':<8} v2.3 数量   v2.4 数量   变化")
    print("-"*50)
    for low, high in [(0,40), (40,50), (50,60), (60,70), (70,80), (80,100)]:
        v23_count = count_range(v23_scores, low, high)
        v24_count = count_range(v24_scores, low, high)
        print(f"{low}-{high:<2}分{'':<5} {v23_count:>3}个       {v24_count:>3}个       {v24_count-v23_count:>+3}")
    
    # RSI 超买拦截检查
    print(f"\n{'='*90}")
    print("RSI 超买拦截检查")
    print(f"{'='*90}")
    
    high_rsi = [c for c in comparison if c['details_v24']['rsi'] > 75]
    high_rsi_open = [c for c in high_rsi if c['score_v24'] >= 70]
    
    print(f"\nRSI>75 的标的：{len(high_rsi)}个")
    if high_rsi:
        print(f"其中被错误开仓：{len(high_rsi_open)}个")
        for c in high_rsi[:5]:
            d = c['details_v24']
            print(f"  {c['symbol']}: RSI={d['rsi']:.0f} 评分={c['score_v24']:.0f}分 {'⚠️ 开仓' if c['score_v24']>=70 else '✅ 正确拦截'}")
    else:
        print("  无 RSI>75 的标的")
    
    # 典型案例分析
    print(f"\n{'='*90}")
    print("典型案例分析")
    print(f"{'='*90}")
    
    # 找几个典型案例
    neutral = [c for c in comparison if 55 <= c['score_v23'] <= 65]
    strong = [c for c in comparison if c['score_v23'] >= 70]
    
    if neutral:
        c = neutral[len(neutral)//2]
        d = c['details_v24']
        print(f"\n📌 中性信号代表：{c['symbol']}")
        print(f"  v2.3: {c['score_v23']:.0f}分 → v2.4: {c['score_v24']:.0f}分 (提升{c['diff']:+.0f}分)")
        print(f"  指标：RSI={d['rsi']:.0f} MACD={d['macd_score']}分 EMA={d['ema_score']}分 Vol={d['vol_ratio']:.2f}x")
        print(f"  分项：RSI{d['rsi_score']}分(-{d['rsi_penalty']}) MACD{d['macd_score']}分 EMA{d['ema_score']}分 Vol{d['vol_score']}分 支撑{d['support_score']}分 连续{d['confirm_score']}分")
    
    if strong:
        c = strong[0]
        d = c['details_v24']
        print(f"\n📌 强信号代表：{c['symbol']}")
        print(f"  v2.3: {c['score_v23']:.0f}分 → v2.4: {c['score_v24']:.0f}分 (提升{c['diff']:+.0f}分)")
        print(f"  指标：RSI={d['rsi']:.0f} MACD={d['macd_score']}分 EMA={d['ema_score']}分 Vol={d['vol_ratio']:.2f}x")
    
    # 预期 vs 实际
    print(f"\n{'='*90}")
    print("预期目标 vs 实际结果")
    print(f"{'='*90}")
    
    print(f"""
预期目标：
  - 中性信号从约 55 分提升到 65 分
  - 较强信号从约 65 分提升到 72-75 分
  - 开仓率从 1.25% 提升到 5-8%
  - RSI 超买信号仍然被正确拦截

实际结果：
  - 中性信号平均：{sum(c['score_v23'] for c in neutral)/max(1,len(neutral)):.0f}分 → {sum(c['score_v24'] for c in neutral)/max(1,len(neutral)):.0f}分 ({sum(c['diff'] for c in neutral)/max(1,len(neutral)):+.0f}分)
  - 强信号平均：{sum(c['score_v23'] for c in strong)/max(1,len(strong)):.0f}分 → {sum(c['score_v24'] for c in strong)/max(1,len(strong)):.0f}分 ({sum(c['diff'] for c in strong)/max(1,len(strong)):+.0f}分)
  - 开仓率：{open_v23/len(comparison)*100:.1f}% → {open_v24/len(comparison)*100:.1f}% ({(open_v24-open_v23)/len(comparison)*100:+.1f}个百分点)
  - RSI>75 拦截：{len(high_rsi)-len(high_rsi_open)}/{len(high_rsi)}个正确拦截 ({(len(high_rsi)-len(high_rsi_open))/max(1,len(high_rsi))*100:.0f}%)
""")
    
    # 总结
    print(f"\n{'='*90}")
    print("v2.4 校准结论")
    print(f"{'='*90}")
    
    target_hit = (
        avg_diff >= 8 and  # 平均提升至少 8 分
        open_v24/len(comparison)*100 >= 3 and  # 开仓率至少 3%
        len(high_rsi_open) == 0  # RSI 超买全部拦截
    )
    
    if target_hit:
        print("\n✅ v2.4 校准成功！达到预期目标")
        print(f"  - 平均评分提升 {avg_diff:+.1f}分")
        print(f"  - 开仓率从 {open_v23/len(comparison)*100:.1f}% 提升至 {open_v24/len(comparison)*100:.1f}%")
        print(f"  - RSI 超买拦截率 100%")
    else:
        print("\n⚠️ v2.4 校准部分达成，可能需要进一步调整")
        if avg_diff < 8:
            print(f"  - 平均提升 {avg_diff:.1f}分 < 目标 8 分")
        if open_v24/len(comparison)*100 < 3:
            print(f"  - 开仓率 {open_v24/len(comparison)*100:.1f}% < 目标 3%")
        if len(high_rsi_open) > 0:
            print(f"  - {len(high_rsi_open)}个 RSI 超买标的被错误开仓")
    
    print(f"\n报告生成：{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")

if __name__ == '__main__':
    main()
