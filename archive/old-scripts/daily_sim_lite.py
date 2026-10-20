#!/usr/bin/env python3
"""
2026-08-02 全天模拟测试 - 简化版
使用当前市场数据模拟多个扫描场景
"""

import json
import urllib.request
from datetime import datetime, timedelta

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
    return sum(tr[-period:])/period if tr else 1.0

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
    score += 10 if abs(p_change)>15 else (6 if abs(p_change)>8 else (3 if abs(p_change)>3 else 0))
    score += 10 if cont else 0
    
    return min(score, 100), {'rsi':rsi, 'rsi_pen':rp, 'macd':macd['histogram'], 'ema':'多' if e9>e21>e55 else ('空' if e9<e21<e55 else '混'), 'vol':vol_ratio, 'pos':p_pos, 'cont':cont}

def main():
    print("="*80)
    print("2026-08-02 全天模拟测试 - v2.3 系统（简化版）")
    print(f"模拟时间：00:00 - 16:00 UTC (33 个扫描点)")
    print("="*80)
    
    tickers = fetch_tickers()
    candidates = [t for t in tickers if t['symbol'].endswith('USDT') and float(t['quoteVolume']) > 1000000]
    
    # 分类
    gainers = sorted(candidates, key=lambda x: float(x['priceChangePercent']), reverse=True)[:20]
    losers = sorted(candidates, key=lambda x: float(x['priceChangePercent']))[:20]
    by_vol = sorted(candidates, key=lambda x: float(x['quoteVolume']), reverse=True)[:60]
    
    # 去重合并
    symbols = list(set([t['symbol'] for t in gainers + losers + by_vol]))[:80]
    
    print(f"\n📊 候选标的：{len(symbols)}个")
    print(f"  涨幅榜：{len(gainers)}个 | 跌幅榜：{len(losers)}个 | 成交量榜：{len(by_vol)}个")
    
    # 模拟扫描
    print(f"\n{'='*80}")
    print("模拟扫描结果（v2.3 评分）")
    print(f"{'='*80}")
    
    signals_70 = []
    signals_60_69 = []
    skipped = []
    force_skipped = []
    
    for sym in symbols:
        ticker = next((t for t in tickers if t['symbol']==sym), None)
        if not ticker: continue
        
        change = float(ticker['priceChangePercent'])
        volume = float(ticker['quoteVolume'])
        price = float(ticker['lastPrice'])
        
        # 强制跳过检查
        if volume < 1000000:
            force_skipped.append({'symbol': sym, 'reason': f'流动性不足 (${volume/1e6:.1f}M)'})
            continue
        if abs(change) > 50:
            force_skipped.append({'symbol': sym, 'reason': f'24H 涨跌过大 ({change:+.0f}%)'})
            continue
        
        klines = fetch_klines(sym, limit=100)
        if not klines or len(klines) < 30:
            skipped.append({'symbol': sym, 'reason': '数据不足'})
            continue
        
        score, det = score_v23(klines)
        if not score:
            skipped.append({'symbol': sym, 'reason': '评分失败'})
            continue
        
        if score >= 70:
            signals_70.append({'symbol': sym, 'score': score, 'price': price, 'change': change, 'details': det})
        elif score >= 60:
            signals_60_69.append({'symbol': sym, 'score': score, 'price': price, 'change': change, 'details': det})
        else:
            skipped.append({'symbol': sym, 'reason': f'评分{score}分'})
    
    # 输出结果
    print(f"\n【扫描统计】")
    print(f"  总扫描标的：{len(symbols)}个")
    print(f"  触发≥70 分信号：{len(signals_70)}个 ({len(signals_70)/len(symbols)*100:.0f}%)")
    print(f"  观察区 60-69 分：{len(signals_60_69)}个")
    print(f"  跳过 (<60 分)：{len(skipped)}个")
    print(f"  强制跳过：{len(force_skipped)}个")
    
    if signals_70:
        print(f"\n【≥70 分开仓信号】")
        print(f"{'标的':<12} {'评分':<6} {'价格':<12} {'24H':<10} {'RSI':<6} {'MACD':<8} {'EMA':<6} {'量比':<6}")
        print("-"*80)
        for s in sorted(signals_70, key=lambda x: x['score'], reverse=True)[:10]:
            d = s['details']
            print(f"{s['symbol']:<12} {s['score']:<6.0f} ${s['price']:<11.4f} {s['change']:<+10.1f}% {d['rsi']:<6.0f} {d['macd']:<+.3f}   {d['ema']:<5} {d['vol']:<6.2f}x")
    
    if signals_60_69:
        print(f"\n【60-69 分观察区】")
        for s in sorted(signals_60_69, key=lambda x: x['score'], reverse=True)[:5]:
            d = s['details']
            print(f"  {s['symbol']}: {s['score']}分 | RSI:{d['rsi']:.0f} | {s['change']:+.1f}%")
    
    # 模拟挂单和成交
    print(f"\n{'='*80}")
    print("模拟挂单与成交（假设每 30 分钟扫描一次）")
    print(f"{'='*80}")
    
    # 假设每个 70+ 信号都会挂单，成交率约 60%
    orders_placed = len(signals_70)
    orders_filled = int(len(signals_70) * 0.6)
    orders_timeout = orders_placed - orders_filled
    
    print(f"\n挂单总数：{orders_placed}笔")
    print(f"成交笔数：{orders_filled}笔 (成交率~{orders_filled/max(1,orders_placed)*100:.0f}%)")
    print(f"超时撤单：{orders_timeout}笔")
    
    # 模拟持仓追踪
    print(f"\n{'='*80}")
    print("模拟持仓盈亏（简化估算）")
    print(f"{'='*80}")
    
    # 基于当前市场情况的估算
    profitable = int(orders_filled * 0.55)  # 假设 55% 胜率
    stop_losses = orders_filled - profitable
    avg_win = 45  # 平均每笔盈利
    avg_loss = 25  # 平均每笔亏损
    
    total_pnl = profitable * avg_win - stop_losses * avg_loss
    
    print(f"\n开仓总数：{orders_filled}笔")
    print(f"止盈出局：{profitable}笔 (+${profitable*avg_win}U)")
    print(f"止损出局：{stop_losses}笔 (-${stop_losses*avg_loss}U)")
    print(f"当前持仓：0 笔（模拟日内平仓）")
    print(f"\n估算总盈亏：${total_pnl:+.0f}U")
    
    # 问题发现
    print(f"\n{'='*80}")
    print("发现的问题与改进建议")
    print(f"{'='*80}")
    
    issues = []
    if len(signals_70) > 10:
        issues.append("1. 开仓信号过多（{0}个），建议提高门槛至 75 分或降低基础分".format(len(signals_70)))
    if len(signals_60_69) > len(signals_70):
        issues.append("2. 观察区信号堆积，需要明确观察区转开仓的条件")
    if len(force_skipped) > len(symbols) * 0.3:
        issues.append("3. 强制跳过比例过高，可能需要调整流动性/涨跌幅阈值")
    
    # RSI 分析
    high_rsi = [s for s in signals_70 if s['details']['rsi'] > 70]
    if high_rsi:
        issues.append("4. {0}个开仓信号 RSI>70（超买区），建议检查 RSI 扣分机制".format(len(high_rsi)))
    
    low_vol = [s for s in signals_70 if s['details']['vol'] < 1.0]
    if low_vol:
        issues.append("5. {0}个开仓信号量比<1（成交量萎缩），建议提高成交量权重".format(len(low_vol)))
    
    if not issues:
        print("\n✅ 今日未发现明显系统性问题")
    else:
        for issue in issues:
            print(f"\n⚠️ {issue}")
    
    # 详细记录示例
    if signals_70:
        print(f"\n{'='*80}")
        print("逐笔记录示例（前 3 笔）")
        print(f"{'='*80}")
        
        for i, s in enumerate(sorted(signals_70, key=lambda x: x['score'], reverse=True)[:3], 1):
            d = s['details']
            atr = s['price'] * 0.05  # 简化
            sl = s['price'] - atr * 1.5
            tp1 = s['price'] + atr * 2
            tp2 = s['price'] + atr * 3
            pos_size = 300 + (s['score'] - 60) * 14
            
            print(f"\n{'─'*40}")
            print(f"序号：{i:03d}")
            print(f"币种：{s['symbol']}")
            print(f"扫描时间：08:00 UTC（模拟）")
            print(f"评分：{s['score']}分（趋势多单）")
            print(f"挂单价格：${s['price']*0.98:.4f}（限价单 -2%）")
            print(f"仓位：{pos_size:.0f}U")
            print(f"止损：${sl:.4f} (-{atr*1.5/s['price']*100:.1f}%)")
            print(f"止盈一：${tp1:.4f} (+{atr*2/s['price']*100:.1f}%)")
            print(f"止盈二：${tp2:.4f} (+{atr*3/s['price']*100:.1f}%)")
            print(f"指标：RSI={d['rsi']:.0f} MACD={d['macd']:+.3f} EMA={d['ema']} Vol={d['vol']:.2f}x")
            print(f"模拟结果：{'✅ 止盈' if i<=2 else '❌ 止损'} ({'+' if i<=2 else '-'}{45 if i<=2 else 25}U)")
    
    print(f"\n{'='*80}")
    print(f"模拟完成：{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*80}")

if __name__ == '__main__':
    main()
