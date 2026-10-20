#!/usr/bin/env python3
"""
新系统评分对比测试
对比最近 5 个开仓币种在新旧系统下的评分和决策
"""
import sys
sys.path.insert(0, '/root/.openclaw/workspace')

import json
import requests
from datetime import datetime

# 导入新系统模块
from modules.scoring import ScoringEngine
from config.market_rules import MarketFilter

# 最近 5 笔开仓记录
RECENT_TRADES = [
    {'symbol': 'MSTRUSDT', 'direction': 'long', 'entry_price': 97.15, 'open_time': '2026-07-29T14:12:29Z'},
    {'symbol': 'UBUSDT', 'direction': 'long', 'entry_price': 0.13214, 'open_time': '2026-07-29T12:55:33Z'},
    {'symbol': 'BTWUSDT', 'direction': 'long', 'entry_price': 0.08985, 'open_time': '2026-07-29T08:13:18Z'},
    {'symbol': 'SOLUSDT', 'direction': 'long', 'entry_price': 76.1, 'open_time': '2026-07-27T14:26:07Z'},
    {'symbol': 'ONDOUSDT', 'direction': 'long', 'entry_price': 0.4094, 'open_time': '2026-07-27T14:21:07Z'},
]

def get_klines(symbol, interval='1h', limit=100):
    """获取 K 线数据"""
    try:
        url = "https://fapi.binance.com/fapi/v1/klines"
        params = {'symbol': symbol, 'interval': interval, 'limit': limit}
        r = requests.get(url, params=params, timeout=10)
        return r.json()
    except Exception as e:
        print(f"  获取 {symbol} K 线失败：{e}")
        return None

def calc_rsi(closes, period=14):
    """计算 RSI"""
    if len(closes) < period + 1:
        return 50
    
    gains = []
    losses = []
    
    for i in range(1, len(closes)):
        change = closes[i] - closes[i-1]
        if change > 0:
            gains.append(change)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(change))
    
    if len(gains) < period:
        return 50
    
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    
    if avg_loss == 0:
        return 100
    
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    
    return round(rsi, 2)

def calc_macd_hist(closes):
    """计算 MACD 柱状图（简化版）"""
    if len(closes) < 26:
        return 0
    
    # 简化：用价格变化率模拟
    recent = closes[-12:]
    prev = closes[-26:-12]
    
    recent_slope = (recent[-1] - recent[0]) / len(recent)
    prev_slope = (prev[-1] - prev[0]) / len(prev)
    
    macd_hist = (recent_slope - prev_slope) / prev[-1] * 1000 if prev[-1] else 0
    
    return round(macd_hist, 4)

def calc_ema_alignment(closes):
    """计算 EMA 排列得分（简化版）"""
    if len(closes) < 55:
        return 50
    
    # 计算 EMA9, EMA21, EMA55
    def ema(data, period):
        multiplier = 2 / (period + 1)
        ema_val = sum(data[:period]) / period
        for val in data[period:]:
            ema_val = (val - ema_val) * multiplier + ema_val
        return ema_val
    
    ema9 = ema(closes, 9)
    ema21 = ema(closes, 21)
    ema55 = ema(closes, 55)
    
    # 多排：EMA9 > EMA21 > EMA55
    if ema9 > ema21 > ema55:
        return 85
    # 空排：EMA9 < EMA21 < EMA55
    elif ema9 < ema21 < ema55:
        return 15
    # 混乱排列
    else:
        return 50

def analyze_symbol(symbol, direction):
    """分析单个标的"""
    print(f"\n{'='*60}")
    print(f"{symbol} ({direction})")
    print(f"{'='*60}")
    
    # 获取 K 线
    klines = get_klines(symbol, interval='1h', limit=100)
    if not klines:
        return None
    
    closes = [float(k[4]) for k in klines]
    volumes = [float(k[5]) for k in klines]
    
    current_price = closes[-1]
    
    # 计算技术指标
    rsi = calc_rsi(closes, 14)
    macd_hist = calc_macd_hist(closes)
    ema_alignment = calc_ema_alignment(closes)
    
    # 成交量比率
    if len(volumes) > 10:
        volume_ratio = volumes[-1] / (sum(volumes[-10:-1]) / 9)
    else:
        volume_ratio = 1.0
    
    print(f"当前价格：{current_price}")
    print(f"RSI(14): {rsi}")
    print(f"MACD 柱：{macd_hist}")
    print(f"EMA 排列：{ema_alignment}")
    print(f"成交量比率：{volume_ratio:.2f}")
    
    # 构建评分详情
    details = {
        'rsi': rsi,
        'macd_hist': macd_hist,
        'ema_alignment': ema_alignment,
        'volume_ratio': volume_ratio,
        'support_strength': 60,  # 简化假设
        'resistance_strength': 60,
        'risk_reward_ratio': 2.0,
        'confidence': 0.7
    }
    
    # 新系统评分
    if direction == 'long':
        score, breakdown = ScoringEngine.calc_score_trend(details)
    else:
        score, breakdown = ScoringEngine.calc_score_trend_short(details)
    
    print(f"\n【新系统评分】")
    print(f"  总分：{score}")
    print(f"  明细：RSI={breakdown['rsi']}, MACD={breakdown['macd']}, EMA={breakdown['ema']:.1f}, Vol={breakdown['volume']}, Support={breakdown['support']:.1f}")
    
    # 新系统决策
    if score >= 85:
        new_decision = "开仓（双网）"
    elif score >= 70:
        new_decision = "开仓（限价单）"
    else:
        new_decision = "跳过"
    
    # 老系统决策（基于原始评分）
    old_score = 0  # 老系统实际评分
    if old_score > 0:
        old_decision = "开仓"
    else:
        old_decision = "开仓（评分缺失）"
    
    print(f"\n【决策对比】")
    print(f"  老系统：{old_decision} (评分：{old_score})")
    print(f"  新系统：{new_decision} (评分：{score})")
    
    # 仓位计算
    position = ScoringEngine.calc_position_usdt(
        score=score,
        details=details,
        balance_usdt=10000,
        current_positions_count=2,
        max_positions=5
    )
    print(f"\n【建议仓位】")
    print(f"  新系统：{position} USDT")
    
    return {
        'symbol': symbol,
        'old_score': old_score,
        'new_score': score,
        'old_decision': old_decision,
        'new_decision': new_decision,
        'position': position,
        'details': details
    }

def main():
    print("="*60)
    print("新系统评分对比测试")
    print("="*60)
    print(f"测试时间：{datetime.utcnow().isoformat()}")
    print(f"测试标的：最近 5 笔开仓记录")
    
    # 检查市场环境
    mf = MarketFilter()
    regime = mf.check_btc_filter()
    limits = mf.get_position_limits()
    
    print(f"\n当前市场环境：{regime}")
    print(f"持仓上限：多单{limits[0]}, 空单{limits[1]}, 总计{limits[2]}")
    
    results = []
    for trade in RECENT_TRADES:
        result = analyze_symbol(trade['symbol'], trade['direction'])
        if result:
            results.append(result)
    
    # 汇总对比
    print("\n" + "="*60)
    print("汇总对比")
    print("="*60)
    
    print(f"\n{'标的':<15} {'老系统':<12} {'新系统':<12} {'差异':<10} {'新决策':<15}")
    print("-"*60)
    
    for r in results:
        diff = r['new_score'] - r['old_score']
        diff_str = f"+{diff}" if diff > 0 else str(diff)
        print(f"{r['symbol']:<15} {r['old_score']:<12} {r['new_score']:<12} {diff_str:<10} {r['new_decision']:<15}")
    
    # 统计分析
    avg_new_score = sum(r['new_score'] for r in results) / len(results)
    would_open = sum(1 for r in results if r['new_decision'] != '跳过')
    
    print(f"\n【统计分析】")
    print(f"  平均评分：{avg_new_score:.1f}")
    print(f"  会开仓：{would_open}/{len(results)} ({would_open/len(results)*100:.0f}%)")
    print(f"  会跳过：{len(results) - would_open}/{len(results)} ({(len(results)-would_open)/len(results)*100:.0f}%)")
    
    # 保存结果
    output = {
        'timestamp': datetime.utcnow().isoformat(),
        'market_regime': regime,
        'results': results,
        'summary': {
            'avg_score': avg_new_score,
            'would_open': would_open,
            'total': len(results)
        }
    }
    
    with open('config/scoring_comparison.json', 'w') as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    
    print(f"\n结果已保存：config/scoring_comparison.json")

if __name__ == '__main__':
    main()
