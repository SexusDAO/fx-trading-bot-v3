#!/usr/bin/env python3
"""
v2.2 vs v2.3 评分系统对比回测
"""

import json
import urllib.request
from datetime import datetime
from pathlib import Path
# 内联 v2.3 评分函数（避免导入问题）
def check_continuous_confirmation(klines, direction):
    if len(klines) < 3:
        return False
    last_close = klines[-2]['close']
    prev_close = klines[-3]['close']
    prev_prev_close = klines[-4]['close'] if len(klines) > 4 else klines[-3]['open']
    if direction == 'long':
        return last_close > prev_close and prev_close > prev_prev_close
    else:
        return last_close < prev_close and prev_close < prev_prev_close

def score_symbol_v23(klines, ticker, position='long'):
    if not klines or len(klines) < 30:
        return None
    closed_klines = klines[:-1]
    prices = [k['close'] for k in closed_klines]
    quote_volumes = [k['quote_volume'] for k in closed_klines]
    rsi = calculate_rsi(prices)
    macd = calculate_macd(prices)
    ema9, ema21, ema55 = calculate_ema(prices, 9), calculate_ema(prices, 21), calculate_ema(prices, 55)
    if len(quote_volumes) >= 11:
        last_vol = quote_volumes[-2]
        avg_vol = sum(quote_volumes[-12:-2]) / 10
        vol_ratio = last_vol / avg_vol if avg_vol > 0 else 1
    else:
        vol_ratio = 1.0
    price_change = (prices[-1] - prices[-20]) / prices[-20] * 100
    divergence = detect_divergence(klines)
    continuous_confirmed = check_continuous_confirmation(klines, position)
    score = 0
    # RSI
    if position == 'long':
        if 45 <= rsi <= 65: rsi_score = 18
        elif 40 <= rsi < 45 or 65 < rsi <= 70: rsi_score = 12
        elif 30 <= rsi < 40 or 70 < rsi <= 75: rsi_score = 6
        else: rsi_score = 0
        rsi_penalty = 25 if rsi > 80 else (15 if rsi > 75 else (10 if rsi < 20 else 0))
    else:
        if 35 <= rsi <= 55: rsi_score = 18
        elif 30 <= rsi < 35 or 55 < rsi <= 60: rsi_score = 12
        elif 25 <= rsi < 30 or 60 < rsi <= 70: rsi_score = 6
        else: rsi_score = 0
        rsi_penalty = 25 if rsi < 20 else (15 if rsi < 25 else (10 if rsi > 80 else 0))
    score += rsi_score - rsi_penalty
    # MACD
    if position == 'long':
        macd_score = 15 if macd['histogram'] > 0.001 else (10 if macd['histogram'] > 0 else (5 if macd['histogram'] > -0.0005 else 0))
    else:
        macd_score = 15 if macd['histogram'] < -0.001 else (10 if macd['histogram'] < 0 else (5 if macd['histogram'] < 0.0005 else 0))
    score += macd_score
    # EMA
    if position == 'long':
        ema_score = 20 if ema9 > ema21 > ema55 and ema9 > prices[-1] else (15 if ema9 > ema21 > ema55 else (8 if ema9 > ema21 or ema21 > ema55 else 0))
    else:
        ema_score = 20 if ema9 < ema21 < ema55 and ema9 < prices[-1] else (15 if ema9 < ema21 < ema55 else (8 if ema9 < ema21 or ema21 < ema55 else 0))
    score += ema_score
    # Volume
    vol_score = 20 if vol_ratio > 2.0 else (15 if vol_ratio > 1.5 else (10 if vol_ratio > 1.0 else (5 if vol_ratio > 0.8 else 0)))
    score += vol_score
    # Support/Resistance
    high_20, low_20 = max(prices[-20:]), min(prices[-20:])
    price_pos = (prices[-1] - low_20) / (high_20 - low_20) if high_20 > low_20 else 0.5
    if position == 'long':
        support_score = 20 if price_pos < 0.3 else (12 if price_pos < 0.5 else (6 if price_pos < 0.7 else 0))
    else:
        support_score = 20 if price_pos > 0.7 else (12 if price_pos > 0.5 else (6 if price_pos > 0.3 else 0))
    score += support_score
    # Divergence
    div_score = 20 if (divergence == 'bullish' and position == 'long') or (divergence == 'bearish' and position == 'short') else (10 if divergence != 'none' else 5)
    score += div_score
    # Trend
    trend_score = 10 if abs(price_change) > 15 else (6 if abs(price_change) > 8 else (3 if abs(price_change) > 3 else 0))
    score += trend_score
    # Continuous confirmation
    score += 10 if continuous_confirmed else 0
    return {'raw_score': score, 'final_score': min(score, 100), 'details': {'rsi': rsi, 'rsi_penalty': rsi_penalty, 'vol_ratio': vol_ratio, 'continuous_confirmed': continuous_confirmed}}

def fetch_klines(symbol, interval="1h", limit=100):
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval={interval}&limit={limit}"
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            data = json.loads(response.read())
            return [{
                'timestamp': k[0], 'open': float(k[1]), 'high': float(k[2]),
                'low': float(k[3]), 'close': float(k[4]), 'volume': float(k[5]),
                'quote_volume': float(k[7])
            } for k in data]
    except Exception as e:
        return None

def fetch_ticker(symbol):
    url = f"https://fapi.binance.com/fapi/v1/ticker/24hr?symbol={symbol}"
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return json.loads(response.read())
    except:
        return None

def calculate_rsi(prices, period=14):
    if len(prices) < period + 1:
        return 50.0
    gains = [max(0, prices[i] - prices[i-1]) for i in range(1, len(prices))]
    losses = [max(0, prices[i-1] - prices[i]) for i in range(1, len(prices))]
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    return 100 - (100 / (1 + avg_gain / avg_loss))

def calculate_ema(prices, period):
    if len(prices) < period:
        return prices[-1] if prices else 0
    multiplier = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for price in prices[period:]:
        ema = (price - ema) * multiplier + ema
    return ema

def calculate_macd(prices):
    if len(prices) < 26:
        return {'macd': 0, 'signal': 0, 'histogram': 0}
    ema12 = calculate_ema(prices, 12)
    ema26 = calculate_ema(prices, 26)
    macd_line = ema12 - ema26
    return {'macd': macd_line, 'signal': macd_line * 0.9, 'histogram': macd_line * 0.1}

def detect_divergence(klines):
    if len(klines) < 20:
        return 'none'
    prices = [k['close'] for k in klines[-20:]]
    macd_hist = [calculate_macd(prices[:i+1])['histogram'] for i in range(len(prices))]
    for i in range(5, len(prices) - 3):
        if prices[i] < min(prices[i-3:i]) and prices[i] < min(prices[i+1:i+4]) and macd_hist[i] > macd_hist[i-3]:
            return 'bullish'
        if prices[i] > max(prices[i-3:i]) and prices[i] > max(prices[i+1:i+4]) and macd_hist[i] < macd_hist[i-3]:
            return 'bearish'
    return 'none'

def score_symbol_v22(klines, ticker):
    """v2.2 评分（基础分 50）"""
    if not klines or len(klines) < 20:
        return None
    prices = [k['close'] for k in klines]
    quote_volumes = [k['quote_volume'] for k in klines]
    rsi = calculate_rsi(prices)
    macd = calculate_macd(prices)
    ema9, ema21, ema55 = calculate_ema(prices, 9), calculate_ema(prices, 21), calculate_ema(prices, 55)
    avg_vol = sum(quote_volumes[-10:]) / 10
    vol_ratio = quote_volumes[-1] / avg_vol if avg_vol > 0 else 1
    price_change = (prices[-1] - prices[-20]) / prices[-20] * 100
    divergence = detect_divergence(klines)
    
    score = 50  # 基础分
    score += 10 if 40 <= rsi <= 60 else (5 if 30 <= rsi < 40 or 60 < rsi <= 70 else 0)
    score += 15 if macd['histogram'] > 0 else (5 if macd['histogram'] > -0.001 else 0)
    score += 15 if ema9 > ema21 > ema55 or ema9 < ema21 < ema55 else 5
    score += 15 if vol_ratio > 2.0 else (10 if vol_ratio > 1.5 else (5 if vol_ratio > 1.0 else 0))
    score += 20 if divergence in ['bullish', 'bearish'] else 5
    score += 10 if abs(price_change) > 10 else (5 if abs(price_change) > 5 else 0)
    
    change_pct = float(ticker.get('priceChangePercent', 0)) / 100
    market_cap_adj = 1.05 if abs(change_pct) > 0.3 else (1.02 if abs(change_pct) > 0.1 else 1.0)
    
    return {'raw_score': score, 'final_score': min(score * market_cap_adj, 100), 'rsi': rsi, 'vol_ratio': vol_ratio}

def main():
    print("=" * 80)
    print("v2.2 vs v2.3 评分系统对比回测")
    print(f"生成时间：{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 80)
    
    symbols = [
        {"symbol": "BLESSUSDT", "type": "gainer"},
        {"symbol": "HOMEUSDT", "type": "gainer"},
        {"symbol": "UAIUSDT", "type": "gainer"},
        {"symbol": "STARUSDT", "type": "gainer"},
        {"symbol": "ICNTUSDT", "type": "gainer"},
        {"symbol": "BTCUSDT", "type": "neutral"},
        {"symbol": "ETHUSDT", "type": "neutral"},
        {"symbol": "SOLUSDT", "type": "neutral"},
        {"symbol": "BNBUSDT", "type": "neutral"},
        {"symbol": "XRPUSDT", "type": "neutral"},
    ]
    
    results_v22 = []
    results_v23 = []
    
    for sym_info in symbols:
        symbol = sym_info['symbol']
        ticker = fetch_ticker(symbol)
        klines = fetch_klines(symbol)
        
        if not ticker or not klines:
            continue
        
        change_pct = float(ticker.get('priceChangePercent', 0))
        quote_vol = float(ticker.get('quoteVolume', 0))
        
        # 强制跳过检查
        skip = False
        if quote_vol < 1000000 or abs(change_pct) > 50:
            skip = True
        
        if skip:
            results_v22.append({'symbol': symbol, 'decision': '跳过', 'score': 0})
            results_v23.append({'symbol': symbol, 'decision': '跳过', 'score': 0})
            continue
        
        # v2.2 评分
        score_v22 = score_symbol_v22(klines, ticker)
        if score_v22:
            decision_v22 = '开仓' if score_v22['final_score'] >= 70 else ('观察区' if score_v22['final_score'] >= 63 else '跳过')
            results_v22.append({
                'symbol': symbol, 'score': score_v22['final_score'],
                'decision': decision_v22, 'rsi': score_v22['rsi']
            })
        
        # v2.3 评分（多单）
        score_v23 = score_symbol_v23(klines, ticker, 'long')
        if score_v23:
            decision_v23 = '开仓' if score_v23['final_score'] >= 70 else ('观察区' if score_v23['final_score'] >= 63 else '跳过')
            results_v23.append({
                'symbol': symbol, 'score': score_v23['final_score'],
                'decision': decision_v23, 'rsi': score_v23['details']['rsi'],
                'rsi_penalty': score_v23['details']['rsi_penalty']
            })
    
    # 汇总对比
    print("\n" + "=" * 80)
    print("对比结果汇总")
    print("=" * 80)
    
    total = len(results_v22)
    open_v22 = sum(1 for r in results_v22 if r['decision'] == '开仓')
    observe_v22 = sum(1 for r in results_v22 if r['decision'] == '观察区')
    skip_v22 = sum(1 for r in results_v22 if r['decision'] == '跳过')
    
    open_v23 = sum(1 for r in results_v23 if r['decision'] == '开仓')
    observe_v23 = sum(1 for r in results_v23 if r['decision'] == '观察区')
    skip_v23 = sum(1 for r in results_v23 if r['decision'] == '跳过')
    
    print(f"\n{'指标':<15} {'v2.2':<15} {'v2.3':<15} {'变化':<15}")
    print("-" * 60)
    print(f"{'总标的数':<15} {total:<15} {total:<15} {'-':<15}")
    print(f"{'触发开仓':<15} {open_v22} ({open_v22/total*100:.0f}%)  {open_v23} ({open_v23/total*100:.0f}%)  {open_v23-open_v22:+d}")
    print(f"{'观察区':<15} {observe_v22} ({observe_v22/total*100:.0f}%)  {observe_v23} ({observe_v23/total*100:.0f}%)  {observe_v23-observe_v22:+d}")
    print(f"{'跳过':<15} {skip_v22} ({skip_v22/total*100:.0f}%)  {skip_v23} ({skip_v23/total*100:.0f}%)  {skip_v23-skip_v22:+d}")
    
    avg_score_v22 = sum(r['score'] for r in results_v22 if r['score'] > 0) / max(1, len([r for r in results_v22 if r['score'] > 0]))
    avg_score_v23 = sum(r['score'] for r in results_v23 if r['score'] > 0) / max(1, len([r for r in results_v23 if r['score'] > 0]))
    print(f"{'平均评分':<15} {avg_score_v22:.1f}          {avg_score_v23:.1f}          {avg_score_v23-avg_score_v22:+.1f}")
    
    # 详细对比
    print("\n" + "=" * 80)
    print("详细对比（每个标的）")
    print("=" * 80)
    print(f"{'标的':<12} {'v2.2 评分':<12} {'v2.2 决策':<10} {'v2.3 评分':<12} {'v2.3 决策':<10} {'RSI':<8} {'RSI 扣分':<8}")
    print("-" * 80)
    
    for i in range(len(results_v22)):
        r22 = results_v22[i]
        r23 = results_v23[i]
        score_22 = f"{r22['score']:.1f}" if r22['score'] > 0 else '-'
        score_23 = f"{r23['score']:.1f}" if r23['score'] > 0 else '-'
        rsi_23 = f"{r23.get('rsi', 0):.1f}" if r23['score'] > 0 else '-'
        penalty = f"-{r23.get('rsi_penalty', 0)}" if r23.get('rsi_penalty', 0) > 0 else '-'
        print(f"{r22['symbol']:<12} {score_22:<12} {r22['decision']:<10} {score_23:<12} {r23['decision']:<10} {rsi_23:<8} {penalty:<8}")
    
    # 保存报告
    report = {
        'timestamp': datetime.utcnow().isoformat(),
        'comparison': {
            'v2.2': {'open': open_v22, 'observe': observe_v22, 'skip': skip_v22, 'avg_score': avg_score_v22},
            'v2.3': {'open': open_v23, 'observe': observe_v23, 'skip': skip_v23, 'avg_score': avg_score_v23}
        },
        'details_v22': results_v22,
        'details_v23': results_v23
    }
    
    with open('backtest_v22_v23_compare.json', 'w', encoding='utf-8') as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    
    print(f"\n✅ 报告已保存：backtest_v22_v23_compare.json")

if __name__ == '__main__':
    main()
