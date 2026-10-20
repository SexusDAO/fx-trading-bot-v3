#!/usr/bin/env python3
"""
完整历史情景模拟回测 - v2.2 修正版
修正成交量检查逻辑，使用 quoteVolume
"""

import json
import urllib.request
from datetime import datetime
from pathlib import Path

def fetch_klines(symbol, interval="1h", limit=100):
    """从 Binance API 获取 K 线数据"""
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval={interval}&limit={limit}"
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            data = json.loads(response.read())
            klines = []
            for k in data:
                klines.append({
                    'timestamp': k[0],
                    'open': float(k[1]),
                    'high': float(k[2]),
                    'low': float(k[3]),
                    'close': float(k[4]),
                    'volume': float(k[5]),
                    'quote_volume': float(k[7])  # USDT 成交额
                })
            return klines
    except Exception as e:
        print(f"获取 {symbol} K 线失败：{e}")
        return None

def fetch_ticker(symbol):
    """获取 24H 行情"""
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
    signal_line = macd_line * 0.9
    return {'macd': macd_line, 'signal': signal_line, 'histogram': macd_line - signal_line}

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

def score_symbol(klines, ticker):
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
    
    if ema9 > ema21 > ema55:
        ema_score, ema_pattern = 15, "多头排列"
    elif ema9 < ema21 < ema55:
        ema_score, ema_pattern = 15, "空头排列"
    else:
        ema_score, ema_pattern = 5, "混乱"
    
    score = 50
    rsi_score = 10 if 40 <= rsi <= 60 else (5 if 30 <= rsi < 40 or 60 < rsi <= 70 else 0)
    macd_score = 15 if macd['histogram'] > 0 else (5 if macd['histogram'] > -0.001 else 0)
    vol_score = 15 if vol_ratio > 2.0 else (10 if vol_ratio > 1.5 else (5 if vol_ratio > 1.0 else 0))
    div_score = 20 if divergence in ['bullish', 'bearish'] else 5
    trend_score = 10 if abs(price_change) > 10 else (5 if abs(price_change) > 5 else 0)
    
    score += rsi_score + macd_score + vol_score + div_score + trend_score + ema_score
    
    change_pct = float(ticker.get('priceChangePercent', 0)) / 100
    market_cap_adj = 1.05 if abs(change_pct) > 0.3 else (1.02 if abs(change_pct) > 0.1 else 1.0)
    
    return {
        'rsi': rsi, 'macd': macd, 'ema_pattern': ema_pattern, 'vol_ratio': vol_ratio,
        'divergence': divergence, 'price_change': price_change,
        'rsi_score': rsi_score, 'macd_score': macd_score, 'vol_score': vol_score,
        'div_score': div_score, 'trend_score': trend_score, 'ema_score': ema_score,
        'raw_score': score, 'final_score': score * market_cap_adj
    }

def main():
    print("=" * 70)
    print("历史情景模拟回测报告 - v2.2 (修正版)")
    print(f"生成时间：{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 70)
    
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
    
    results = []
    
    for sym_info in symbols:
        symbol = sym_info['symbol']
        sym_type = sym_info['type']
        
        print(f"\n{'='*70}")
        print(f"标的：{symbol} | 类型：{sym_type}")
        print(f"{'='*70}")
        
        ticker = fetch_ticker(symbol)
        klines = fetch_klines(symbol)
        
        if not ticker or not klines:
            print("⚠️ 数据获取失败，跳过")
            continue
        
        change_pct = float(ticker.get('priceChangePercent', 0))
        current_price = float(ticker.get('lastPrice', 0))
        quote_vol = float(ticker.get('quoteVolume', 0))
        
        print(f"当前价格：${current_price:.6f}")
        print(f"24H 涨跌：{change_pct:+.2f}%")
        print(f"24H 成交额：${quote_vol:,.0f}")
        
        # 强制跳过检查（修正版）
        skip_reason = None
        
        # 使用 quoteVolume 检查，阈值改为 100 万 USDT
        if quote_vol < 1000000:
            skip_reason = f"24H 成交额过低 (${quote_vol:,.0f} < $1M)"
        
        # 24H 涨跌>50% 跳过（过高波动）
        if abs(change_pct) > 50:
            skip_reason = f"24H 涨跌幅过大 ({change_pct:+.2f}%)"
        
        if skip_reason:
            print(f"❌ 强制跳过：{skip_reason}")
            results.append({'symbol': symbol, 'type': sym_type, 'decision': '跳过', 'reason': skip_reason, 'score': 0})
            continue
        
        score_result = score_symbol(klines, ticker)
        if not score_result:
            print("⚠️ 评分失败")
            continue
        
        print(f"\n评分明细:")
        print(f"  RSI({score_result['rsi']:.1f}) → {score_result['rsi_score']}分")
        print(f"  MACD 柱 ({score_result['macd']['histogram']:.6f}) → {score_result['macd_score']}分")
        print(f"  EMA 排列 ({score_result['ema_pattern']}) → {score_result['ema_score']}分")
        print(f"  成交量 ({score_result['vol_ratio']:.2f}x) → {score_result['vol_score']}分")
        print(f"  背离 ({score_result['divergence']}) → {score_result['div_score']}分")
        print(f"  趋势 ({score_result['price_change']:+.1f}%) → {score_result['trend_score']}分")
        print(f"  原始总分：{score_result['raw_score']:.1f}")
        print(f"  最终评分：{score_result['final_score']:.1f}")
        
        if score_result['final_score'] >= 70:
            decision = "开仓"
            direction = "多" if change_pct > 0 else "空"
            position = 300 + (score_result['final_score'] - 60) * 14
            print(f"\n✅ 决策：{decision} | 方向：{direction} | 仓位：{position:.0f}U")
        elif score_result['final_score'] >= 63:
            decision = "观察区"
            position = (300 + (score_result['final_score'] - 60) * 14) * 0.5
            print(f"\n🟡 决策：{decision} | 仓位减半：{position:.0f}U")
        else:
            decision = "跳过"
            print(f"\n❌ 决策：{decision} (评分<63)")
        
        results.append({
            'symbol': symbol, 'type': sym_type, 'price': current_price,
            'change_pct': change_pct, 'quote_vol': quote_vol,
            'score': score_result['final_score'], 'decision': decision,
            'position': position if decision in ['开仓', '观察区'] else 0,
            'divergence': score_result['divergence']
        })
    
    # 汇总
    print(f"\n{'='*70}")
    print("汇总报告")
    print(f"{'='*70}")
    
    total = len(results)
    open_count = sum(1 for r in results if r['decision'] == '开仓')
    observe_count = sum(1 for r in results if r['decision'] == '观察区')
    skip_count = sum(1 for r in results if r['decision'] == '跳过')
    
    print(f"\n总标的数：{total}")
    print(f"触发开仓：{open_count} ({open_count/total*100:.1f}%)")
    print(f"观察区：{observe_count} ({observe_count/total*100:.1f}%)")
    print(f"跳过：{skip_count} ({skip_count/total*100:.1f}%)")
    
    if open_count > 0:
        avg_score = sum(r['score'] for r in results if r['decision'] == '开仓') / open_count
        print(f"开仓标的平均评分：{avg_score:.1f}")
    
    print(f"\n📋 发现的系统性问题:")
    high_vol = sum(1 for r in results if abs(r.get('change_pct', 0)) > 50)
    if high_vol > 0:
        print(f"  - {high_vol} 个标的因 24H 涨跌>50% 被跳过（高波动风险）")
    
    low_liq = sum(1 for r in results if r.get('quote_vol', 0) < 1000000)
    if low_liq > 0:
        print(f"  - {low_liq} 个标的因 24H 成交额<$1M 被跳过（流动性不足）")
    
    low_score = sum(1 for r in results if r['decision'] == '跳过' and r.get('score', 0) < 63 and 'reason' not in r)
    if low_score > 0:
        print(f"  - {low_score} 个标的评分<63 分（信号质量不足）")
    
    # 保存报告
    report = {
        'timestamp': datetime.utcnow().isoformat(),
        'version': 'v2.2-fixed',
        'parameters': {'open_threshold': 70, 'observe_threshold': 63},
        'results': results,
        'summary': {'total': total, 'open': open_count, 'observe': observe_count, 'skip': skip_count}
    }
    
    with open('backtest_report_v2.2_fixed.json', 'w', encoding='utf-8') as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    
    print(f"\n✅ 报告已保存：backtest_report_v2.2_fixed.json")

if __name__ == '__main__':
    main()
