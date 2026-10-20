#!/usr/bin/env python3
"""
完整历史情景模拟回测 - v2.2
使用 Binance API 直接获取数据
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
            # 转换为字典格式
            klines = []
            for k in data:
                klines.append({
                    'timestamp': k[0],
                    'open': float(k[1]),
                    'high': float(k[2]),
                    'low': float(k[3]),
                    'close': float(k[4]),
                    'volume': float(k[5])
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
    """计算 RSI"""
    if len(prices) < period + 1:
        return 50.0
    
    gains = []
    losses = []
    for i in range(1, len(prices)):
        diff = prices[i] - prices[i-1]
        gains.append(max(0, diff))
        losses.append(max(0, -diff))
    
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    
    if avg_loss == 0:
        return 100.0
    
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def calculate_ema(prices, period):
    """计算 EMA"""
    if len(prices) < period:
        return prices[-1] if prices else 0
    
    multiplier = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    
    for price in prices[period:]:
        ema = (price - ema) * multiplier + ema
    
    return ema

def calculate_macd(prices):
    """计算 MACD"""
    if len(prices) < 26:
        return {'macd': 0, 'signal': 0, 'histogram': 0}
    
    ema12 = calculate_ema(prices, 12)
    ema26 = calculate_ema(prices, 26)
    macd_line = ema12 - ema26
    
    # 简化信号线计算
    signal_line = macd_line * 0.9
    histogram = macd_line - signal_line
    
    return {'macd': macd_line, 'signal': signal_line, 'histogram': histogram}

def detect_divergence(klines):
    """检测背离"""
    if len(klines) < 20:
        return 'none'
    
    prices = [k['close'] for k in klines[-20:]]
    
    # 计算每个点的 MACD 柱
    macd_hist = []
    for i in range(len(prices)):
        macd = calculate_macd(prices[:i+1])
        macd_hist.append(macd['histogram'])
    
    # 寻找底背离
    for i in range(5, len(prices) - 3):
        if (prices[i] < min(prices[i-3:i]) and 
            prices[i] < min(prices[i+1:i+4]) and
            macd_hist[i] > macd_hist[i-3]):
            return 'bullish'
    
    # 寻找顶背离
    for i in range(5, len(prices) - 3):
        if (prices[i] > max(prices[i-3:i]) and 
            prices[i] > max(prices[i+1:i+4]) and
            macd_hist[i] < macd_hist[i-3]):
            return 'bearish'
    
    return 'none'

def find_trend_start(klines, is_gainer):
    """找到行情起点"""
    if len(klines) < 10:
        return len(klines) - 1, klines[-1]
    
    if is_gainer:
        # 涨幅榜：找上涨起点（低点）
        min_price = float('inf')
        min_idx = 0
        for i in range(len(klines) - 10, len(klines)):
            if klines[i]['low'] < min_price:
                min_price = klines[i]['low']
                min_idx = i
        return min_idx, klines[min_idx]
    else:
        # 跌幅榜：找下跌起点（高点）
        max_price = 0
        max_idx = 0
        for i in range(len(klines) - 10, len(klines)):
            if klines[i]['high'] > max_price:
                max_price = klines[i]['high']
                max_idx = i
        return max_idx, klines[max_idx]

def score_symbol(klines, ticker):
    """v2.2 评分系统"""
    if not klines or len(klines) < 20:
        return None
    
    prices = [k['close'] for k in klines]
    volumes = [k['volume'] for k in klines]
    
    # 计算指标
    rsi = calculate_rsi(prices)
    macd = calculate_macd(prices)
    
    ema9 = calculate_ema(prices, 9)
    ema21 = calculate_ema(prices, 21)
    ema55 = calculate_ema(prices, 55)
    
    avg_vol = sum(volumes[-10:]) / 10
    vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 1
    
    # 价格变化
    price_change = (prices[-1] - prices[-20]) / prices[-20] * 100
    
    # 检测背离
    divergence = detect_divergence(klines)
    
    # EMA 排列
    if ema9 > ema21 > ema55:
        ema_score = 15
        ema_pattern = "多头排列"
    elif ema9 < ema21 < ema55:
        ema_score = 15
        ema_pattern = "空头排列"
    else:
        ema_score = 5
        ema_pattern = "混乱"
    
    # 评分
    score = 50  # 基础分
    
    # RSI 评分（0-15 分）
    if 40 <= rsi <= 60:
        rsi_score = 10
    elif 30 <= rsi < 40 or 60 < rsi <= 70:
        rsi_score = 5
    else:
        rsi_score = 0
    score += rsi_score
    
    # MACD 评分（0-15 分）
    if macd['histogram'] > 0:
        macd_score = 15
    elif macd['histogram'] > -0.001:
        macd_score = 5
    else:
        macd_score = 0
    score += macd_score
    
    # 成交量评分（0-15 分）
    if vol_ratio > 2.0:
        vol_score = 15
    elif vol_ratio > 1.5:
        vol_score = 10
    elif vol_ratio > 1.0:
        vol_score = 5
    else:
        vol_score = 0
    score += vol_score
    
    # 背离评分（0-20 分）
    if divergence == 'bullish' or divergence == 'bearish':
        div_score = 20
    else:
        div_score = 5
    score += div_score
    
    # 趋势强度（0-10 分）
    if abs(price_change) > 10:
        trend_score = 10
    elif abs(price_change) > 5:
        trend_score = 5
    else:
        trend_score = 0
    score += trend_score
    
    # EMA 排列（0-15 分）
    score += ema_score
    
    # 市值调整
    change_pct = float(ticker.get('priceChangePercent', 0)) / 100
    market_cap_adj = 1.0
    if abs(change_pct) > 0.3:
        market_cap_adj = 1.05
    elif abs(change_pct) > 0.1:
        market_cap_adj = 1.02
    
    final_score = score * market_cap_adj
    
    return {
        'rsi': rsi,
        'macd': macd,
        'ema9': ema9,
        'ema21': ema21,
        'ema55': ema55,
        'ema_pattern': ema_pattern,
        'vol_ratio': vol_ratio,
        'divergence': divergence,
        'price_change': price_change,
        'rsi_score': rsi_score,
        'macd_score': macd_score,
        'vol_score': vol_score,
        'div_score': div_score,
        'trend_score': trend_score,
        'ema_score': ema_score,
        'raw_score': score,
        'final_score': final_score
    }

def main():
    print("=" * 70)
    print("历史情景模拟回测报告 - v2.2")
    print(f"生成时间：{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 70)
    
    # 测试标的
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
        
        # 获取数据
        ticker = fetch_ticker(symbol)
        klines = fetch_klines(symbol)
        
        if not ticker or not klines:
            print("⚠️ 数据获取失败，跳过")
            continue
        
        # 24H 涨跌幅
        change_pct = float(ticker.get('priceChangePercent', 0))
        current_price = float(ticker.get('lastPrice', 0))
        
        print(f"当前价格：${current_price}")
        print(f"24H 涨跌：{change_pct:+.2f}%")
        
        # 找到行情起点
        is_gainer = sym_type == 'gainer'
        start_idx, start_kline = find_trend_start(klines, is_gainer)
        print(f"行情起点：{start_idx} 根 K 线前 | 起点价格：${start_kline['close']:.6f}")
        
        # 强制跳过检查
        skip_reason = None
        avg_vol = sum(k['volume'] for k in klines[-10:]) / 10
        if klines[-1]['volume'] < avg_vol * 0.1:
            skip_reason = f"成交量过低 ({klines[-1]['volume']/avg_vol:.2f}x)"
        
        if abs(change_pct) > 30:
            skip_reason = f"24H 涨跌幅过大 ({change_pct:+.2f}%)"
        
        if skip_reason:
            print(f"❌ 强制跳过：{skip_reason}")
            results.append({
                'symbol': symbol,
                'type': sym_type,
                'decision': '跳过',
                'reason': skip_reason,
                'score': 0
            })
            continue
        
        # 评分
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
        
        # 开仓决策
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
            'symbol': symbol,
            'type': sym_type,
            'price': current_price,
            'change_pct': change_pct,
            'score': score_result['final_score'],
            'decision': decision,
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
    
    # 系统性问题
    print(f"\n📋 发现的系统性问题:")
    high_vol_count = sum(1 for r in results if r.get('change_pct', 0) > 30)
    if high_vol_count > 0:
        print(f"  - {high_vol_count} 个标的因 24H 涨跌>30% 被跳过（高波动风险）")
    
    low_score_count = sum(1 for r in results if r['decision'] == '跳过' and r['score'] < 63)
    if low_score_count > 0:
        print(f"  - {low_score_count} 个标的评分<63 分（信号质量不足）")
    
    # 保存报告
    report = {
        'timestamp': datetime.utcnow().isoformat(),
        'version': 'v2.2',
        'parameters': {
            'open_threshold': 70,
            'observe_threshold': 63
        },
        'results': results,
        'summary': {
            'total': total,
            'open': open_count,
            'observe': observe_count,
            'skip': skip_count,
            'open_rate': open_count/total*100 if total > 0 else 0
        }
    }
    
    report_path = Path('backtest_report_v2.2_full.json')
    with open(report_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    
    print(f"\n✅ 报告已保存：{report_path}")

if __name__ == '__main__':
    main()
