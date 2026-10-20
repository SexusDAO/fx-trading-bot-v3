#!/usr/bin/env python3
"""
优化版评分系统 - 基于 ONDO 回测数据
"""
import sys
sys.path.insert(0, '/root/.openclaw/workspace')

import json
from modules.scoring import ScoringEngine

# 加载 ONDO 回测数据
with open('config/ondo_backtest.json') as f:
    data = json.load(f)

spots = data['spots']

print("="*70)
print("优化版评分系统回测 - ONDO 数据")
print("="*70)

# 方案对比
schemes = {
    '原方案': {'threshold': 70, 'name': '原方案 (≥70 分)'},
    '方案 A': {'threshold': 65, 'name': '方案 A (≥65 分)'},
    '方案 B': {'threshold': 63, 'name': '方案 B (≥63 分)'},
    '方案 C': {'threshold': 60, 'name': '方案 C (≥60 分)'},
}

print("\n【方案对比】")
print("-"*70)

for scheme_id, scheme in schemes.items():
    open_count = sum(1 for s in spots if s['score'] >= scheme['threshold'])
    skip_count = len(spots) - open_count
    open_rate = open_count / len(spots) * 100
    
    print(f"{scheme['name']}: 开仓 {open_count}/{len(spots)} ({open_rate:.0f}%), 跳过 {skip_count}/{len(spots)} ({100-open_rate:.0f}%)")

# 优化版评分（调整权重）
def optimized_score_trend(details):
    """
    优化版趋势评分
    基于 ONDO 回测数据调整
    """
    score = 0
    breakdown = {}
    
    # RSI 评分 (0-25 分) - 提高超卖加分
    rsi = details.get('rsi', 50)
    if 30 <= rsi <= 45:
        rsi_score = 22  # 底部区域，加分
    elif 45 < rsi <= 60:
        rsi_score = 18  # 中性
    elif rsi < 30:
        rsi_score = 20  # 超卖，但可能继续跌
    elif 60 < rsi <= 70:
        rsi_score = 15  # 偏高
    else:
        rsi_score = 10  # 超买风险
    breakdown['rsi'] = rsi_score
    score += rsi_score
    
    # MACD 评分 (0-20 分) - 不 penalize 负值
    macd_hist = details.get('macd_hist', 0)
    if macd_hist > 0:
        macd_score = min(20, 15 + macd_hist * 100)
    else:
        # 负值但不太低，可能是底部
        if macd_hist > -0.005:
            macd_score = 14  # 轻微负值，不 penalize 太多
        else:
            macd_score = 8  # 大幅负值
    breakdown['macd'] = macd_score
    score += macd_score
    
    # EMA 排列评分 (0-20 分) - 降低权重，混乱不 penalize 太多
    ema_score_raw = details.get('ema_alignment', 50)
    if ema_score_raw >= 85:  # 多排
        ema_score = 20
    elif ema_score_raw <= 15:  # 空排
        ema_score = 8  # 降低 penalize
    else:  # 混乱
        ema_score = 14  # 混乱是常态，不 penalize 太多
    breakdown['ema'] = ema_score
    score += ema_score
    
    # 成交量评分 (0-15 分) - 缩量不 penalize 太多
    vol_ratio = details.get('volume_ratio', 1.0)
    if vol_ratio > 2.0:
        vol_score = 15
    elif vol_ratio > 1.5:
        vol_score = 13
    elif vol_ratio > 0.8:
        vol_score = 11  # 正常或略低
    else:
        vol_score = 8  # 缩量，但不 penalize 太多（底部常态）
    breakdown['volume'] = vol_score
    score += vol_score
    
    # 支撑位强度 (0-20 分)
    support = details.get('support_strength', 50)
    support_score = support / 100 * 20
    breakdown['support'] = support_score
    score += support_score
    
    return round(score, 2), breakdown

# 用优化版评分重新测试
print("\n【优化版评分测试】")
print("-"*70)

optimized_spots = []
for spot in spots:
    details = {
        'rsi': spot['rsi'],
        'macd_hist': spot['macd_hist'],
        'ema_alignment': spot['ema_score'],
        'volume_ratio': spot['vol_ratio'],
        'support_strength': 50,
        'risk_reward_ratio': spot['gain'] / 3,
        'confidence': 0.6
    }
    
    opt_score, breakdown = optimized_score_trend(details)
    spot['opt_score'] = opt_score
    spot['breakdown'] = breakdown
    optimized_spots.append(spot)

# 统计不同门槛的开仓率
thresholds = [70, 68, 65, 63, 60, 58]

print(f"\n{'门槛':<10} {'开仓数':<10} {'开仓率':<10} {'错过数':<10}")
print("-"*45)

for threshold in thresholds:
    open_count = sum(1 for s in optimized_spots if s['opt_score'] >= threshold)
    skip_count = len(optimized_spots) - open_count
    open_rate = open_count / len(optimized_spots) * 100
    
    print(f"{threshold}分{'':<7} {open_count:<10} {open_rate:.0f}%{'':<7} {skip_count:<10}")

# 找到最佳门槛（平衡开仓率与质量）
print("\n【最佳门槛分析】")
print("-"*70)

# 计算每个点位的"质量"（涨幅）
for s in optimized_spots:
    s['quality'] = s['gain']  # 涨幅作为质量指标

# 按优化评分分组
score_ranges = [
    (70, 100, '≥70 分'),
    (63, 69, '63-69 分'),
    (60, 62, '60-62 分'),
    (0, 59, '<60 分')
]

print(f"\n{'评分区间':<15} {'数量':<10} {'平均涨幅':<15} {'最大涨幅':<15}")
print("-"*55)

for low, high, label in score_ranges:
    range_spots = [s for s in optimized_spots if low <= s['opt_score'] <= high]
    if range_spots:
        avg_gain = sum(s['gain'] for s in range_spots) / len(range_spots)
        max_gain = max(s['gain'] for s in range_spots)
        print(f"{label:<15} {len(range_spots):<10} {avg_gain:.2f}%{'':<10} {max_gain:.1f}%{'':<10}")
    else:
        print(f"{label:<15} 0{'':<10} N/A{'':<15} N/A{'':<15}")

# 推荐方案
print("\n【推荐方案】")
print("-"*70)

# 计算各方案的期望收益
scenarios = [
    {'name': '保守 (≥70 分)', 'threshold': 70},
    {'name': '平衡 (≥63 分)', 'threshold': 63},
    {'name': '激进 (≥60 分)', 'threshold': 60},
]

print(f"\n{'方案':<20} {'开仓数':<10} {'平均涨幅':<15} {'总涨幅':<15} {'错过机会':<10}")
print("-"*70)

for scenario in scenarios:
    threshold = scenario['threshold']
    open_spots = [s for s in optimized_spots if s['opt_score'] >= threshold]
    skip_spots = [s for s in optimized_spots if s['opt_score'] < threshold]
    
    if open_spots:
        avg_gain = sum(s['gain'] for s in open_spots) / len(open_spots)
        total_gain = sum(s['gain'] for s in open_spots)
    else:
        avg_gain = 0
        total_gain = 0
    
    missed_opportunities = len([s for s in skip_spots if s['gain'] > 5])  # 错过涨幅>5% 的机会
    
    scenario['open_count'] = len(open_spots)
    scenario['avg_gain'] = avg_gain
    scenario['total_gain'] = total_gain
    scenario['missed'] = missed_opportunities
    
    print(f"{scenario['name']:<20} {len(open_spots):<10} {avg_gain:.2f}%{'':<10} {total_gain:.1f}%{'':<10} {missed_opportunities:<10}")

# 最佳方案
best_scenario = max(scenarios, key=lambda x: x['total_gain'] - x['missed'] * 3)  # 权衡总涨幅与错过机会
print(f"\n✅ 推荐方案：{best_scenario['name']}")
print(f"   理由：在开仓机会和错过风险之间取得最佳平衡")

# 保存优化后的数据
output = {
    'optimized_spots': optimized_spots,
    'recommended_threshold': 63,
    'scenarios': scenarios
}

with open('config/optimized_scoring.json', 'w') as f:
    json.dump(output, f, indent=2)

print(f"\n详细数据已保存：config/optimized_scoring.json")

# 代码示例
print("\n【代码修改建议】")
print("-"*70)
print("""
在 modules/scoring.py 中调整 calc_score_trend 函数：

1. RSI 评分调整:
   - 30-45 分：15 分 → 22 分（底部区域加分）
   - <30 分：12 分 → 20 分（超卖但不 penalize）

2. MACD 评分调整:
   - 负值但>-0.005: 5 分 → 14 分（底部常态）
   - 大幅负值：5 分 → 8 分

3. EMA 排列调整:
   - 多排：25 分 → 20 分（降低权重）
   - 空排：15 分 → 8 分
   - 混乱：50 分 → 14 分（不 penalize 太多）

4. 成交量调整:
   - 0.8-1.0: 8 分 → 11 分（底部缩量正常）
   - <0.8: 5 分 → 8 分

5. 开仓门槛:
   - 70 分 → 63 分
""")
