#!/usr/bin/env python3
"""
历史情景模拟回测脚本 - v2.2 评分系统
全程模拟，不执行真实下单
"""

import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

# 测试标的（涨幅榜前 5 + 手动选择的波动标的）
TEST_SYMBOLS = [
    {"symbol": "BLESSUSDT", "type": "gainer", "change": 0.758},
    {"symbol": "HOMEUSDT", "type": "gainer", "change": 0.410},
    {"symbol": "UAIUSDT", "type": "gainer", "change": 0.339},
    {"symbol": "STARUSDT", "type": "gainer", "change": 0.281},
    {"symbol": "ICNTUSDT", "type": "gainer", "change": 0.221},
    {"symbol": "BTCUSDT", "type": "loser", "change": -0.02},  # 假设下跌
    {"symbol": "ETHUSDT", "type": "loser", "change": -0.03},
    {"symbol": "SOLUSDT", "type": "loser", "change": -0.05},
    {"symbol": "BNBUSDT", "type": "loser", "change": -0.01},
    {"symbol": "XRPUSDT", "type": "loser", "change": -0.04},
]

def run_command(cmd):
    """执行命令行并返回 JSON 结果"""
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            return json.loads(result.stdout)
        else:
            print(f"命令失败：{cmd}\n错误：{result.stderr}")
            return None
    except Exception as e:
        print(f"执行错误：{e}")
        return None

def get_klines(symbol, interval="1h", limit=100):
    """获取 K 线数据"""
    cmd = f'binance-pro-cli search indicators --symbol {symbol} --interval {interval} --limit {limit} --format json 2>&1'
    return run_command(cmd)

def calculate_rsi(prices, period=14):
    """计算 RSI"""
    if len(prices) < period + 1:
        return 50.0
    
    gains = []
    losses = []
    for i in range(1, len(prices)):
        diff = prices[i] - prices[i-1]
        if diff > 0:
            gains.append(diff)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(diff))
    
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    
    if avg_loss == 0:
        return 100.0
    
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

def calculate_macd(prices, fast=12, slow=26, signal=9):
    """计算 MACD"""
    if len(prices) < slow + signal:
        return {"macd": 0, "signal": 0, "histogram": 0}
    
    # 简化计算：使用价格变化率代替 EMA
    ema_fast = sum(prices[-fast:]) / fast
    ema_slow = sum(prices[-slow:]) / slow
    macd_line = ema_fast - ema_slow
    
    # 简化信号线
    signal_line = macd_line * 0.9
    histogram = macd_line - signal_line
    
    return {"macd": macd_line, "signal": signal_line, "histogram": histogram}

def detect_divergence(klines):
    """
    检测背离（简化版）
    返回：'bullish', 'bearish', 'none'
    """
    if len(klines) < 20:
        return 'none'
    
    # 提取价格和 MACD
    prices = [k['close'] for k in klines[-20:]]
    macd_hist = []
    for i in range(len(prices)):
        macd = calculate_macd(prices[:i+1])
        macd_hist.append(macd['histogram'])
    
    # 寻找局部低点（底背离）
    for i in range(5, len(prices) - 3):
        if (prices[i] < min(prices[i-3:i]) and 
            prices[i] < min(prices[i+1:i+4]) and
            macd_hist[i] > macd_hist[i-3]):
            return 'bullish'
    
    # 寻找局部高点（顶背离）
    for i in range(5, len(prices) - 3):
        if (prices[i] > max(prices[i-3:i]) and 
            prices[i] > max(prices[i+1:i+4]) and
            macd_hist[i] < macd_hist[i-3]):
            return 'bearish'
    
    return 'none'

def score_symbol(symbol_data, klines):
    """
    评分系统 v2.2
    返回评分详情
    """
    if not klines or len(klines) < 20:
        return None
    
    prices = [k['close'] for k in klines]
    volumes = [k['volume'] for k in klines]
    
    # 计算指标
    rsi = calculate_rsi(prices)
    macd = calculate_macd(prices)
    avg_vol = sum(volumes[-10:]) / 10
    vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 1
    
    # 检测背离
    divergence = detect_divergence(klines)
    
    # 评分
    score = 50  # 基础分
    
    # RSI 评分（0-15 分）
    if 40 <= rsi <= 60:
        score += 10
    elif 30 <= rsi < 40 or 60 < rsi <= 70:
        score += 5
    
    # MACD 评分（0-15 分）
    if macd['histogram'] > 0:
        score += 10
    elif macd['histogram'] > -0.001:
        score += 5
    
    # 成交量评分（0-15 分）
    if vol_ratio > 2.0:
        score += 15
    elif vol_ratio > 1.5:
        score += 10
    elif vol_ratio > 1.0:
        score += 5
    
    # 背离评分（0-20 分）
    if divergence == 'bullish' or divergence == 'bearish':
        score += 20
    
    # 趋势强度评分（0-10 分）
    price_change = (prices[-1] - prices[-20]) / prices[-20] * 100
    if abs(price_change) > 10:
        score += 10
    elif abs(price_change) > 5:
        score += 5
    
    # 市值调整（简化：高波动小市值加分）
    market_cap_adj = 1.0
    if symbol_data['change'] > 0.5:
        market_cap_adj = 1.1
    
    final_score = score * market_cap_adj
    
    return {
        'rsi': rsi,
        'macd': macd,
        'vol_ratio': vol_ratio,
        'divergence': divergence,
        'price_change': price_change,
        'raw_score': score,
        'final_score': final_score
    }

def main():
    print("=" * 60)
    print("历史情景模拟回测报告 - v2.2")
    print("=" * 60)
    print()
    
    results = []
    
    for symbol_info in TEST_SYMBOLS:
        symbol = symbol_info['symbol']
        print(f"\n{'='*50}")
        print(f"标的：{symbol}")
        print(f"类型：{symbol_info['type']} (24H 涨跌：{symbol_info['change']*100:+.1f}%)")
        print(f"{'='*50}")
        
        # 获取 K 线数据
        klines_data = get_klines(symbol)
        
        if not klines_data or 'klines' not in klines_data:
            print("⚠️ 无法获取 K 线数据，跳过")
            continue
        
        klines = klines_data['klines']
        
        # 评分
        score_result = score_symbol(symbol_info, klines)
        
        if not score_result:
            print("⚠️ 评分失败，跳过")
            continue
        
        # 开仓决策
        decision = "跳过"
        if score_result['final_score'] >= 70:
            decision = "开仓"
        elif score_result['final_score'] >= 63:
            decision = "观察区"
        
        print(f"RSI: {score_result['rsi']:.1f}")
        print(f"MACD 柱：{score_result['macd']['histogram']:.6f}")
        print(f"成交量比：{score_result['vol_ratio']:.2f}x")
        print(f"背离：{score_result['divergence']}")
        print(f"原始评分：{score_result['raw_score']:.1f}")
        print(f"最终评分：{score_result['final_score']:.1f}")
        print(f"决策：{decision}")
        
        results.append({
            'symbol': symbol,
            'type': symbol_info['type'],
            'score': score_result['final_score'],
            'decision': decision
        })
    
    # 汇总
    print("\n" + "=" * 60)
    print("汇总")
    print("=" * 60)
    
    total = len(results)
    open_count = sum(1 for r in results if r['decision'] == '开仓')
    observe_count = sum(1 for r in results if r['decision'] == '观察区')
    skip_count = sum(1 for r in results if r['decision'] == '跳过')
    
    print(f"总标的数：{total}")
    print(f"触发开仓：{open_count} ({open_count/total*100:.1f}%)")
    print(f"观察区：{observe_count} ({observe_count/total*100:.1f}%)")
    print(f"跳过：{skip_count} ({skip_count/total*100:.1f}%)")
    
    # 保存报告
    report = {
        'timestamp': datetime.utcnow().isoformat(),
        'version': 'v2.2',
        'results': results,
        'summary': {
            'total': total,
            'open': open_count,
            'observe': observe_count,
            'skip': skip_count
        }
    }
    
    report_path = Path('backtest_report_v2.2.json')
    with open(report_path, 'w') as f:
        json.dump(report, f, indent=2)
    
    print(f"\n报告已保存：{report_path}")

if __name__ == '__main__':
    main()
