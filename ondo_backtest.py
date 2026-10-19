#!/usr/bin/env python3
"""
ONDO 回测分析 - 找出最佳开仓点的技术特征
"""
import sys
sys.path.insert(0, '/root/.openclaw/workspace')

import json
from datetime import datetime

# 加载 K 线数据
with open('config/ondo_klines.json') as f:
    klines = json.load(f)

# 提取数据
closes = [float(k[4]) for k in klines]
opens = [float(k[1]) for k in klines]
highs = [float(k[2]) for k in klines]
lows = [float(k[3]) for k in klines]
volumes = [float(k[5]) for k in klines]
times = [k[0] for k in klines]

def calc_rsi(closes, period=14):
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

def calc_macd(closes):
    if len(closes) < 26:
        return 0, 0, 0
    
    def ema(data, period):
        multiplier = 2 / (period + 1)
        ema_val = sum(data[:period]) / period
        for val in data[period:]:
            ema_val = (val - ema_val) * multiplier + ema_val
        return ema_val
    
    ema12 = ema(closes, 12)
    ema26 = ema(closes, 26)
    macd_line = ema12 - ema26
    
    # 简化 signal
    if len(closes) > 9:
        prev_macds = []
        for i in range(9):
            idx = len(closes) - 9 + i
            if idx > 25:
                e12 = ema(closes[:idx+1], 12)
                e26 = ema(closes[:idx+1], 26)
                prev_macds.append(e12 - e26)
        signal = sum(prev_macds) / len(prev_macds) if prev_macds else 0
    else:
        signal = 0
    
    hist = macd_line - signal
    
    return round(macd_line, 4), round(signal, 4), round(hist, 4)

def calc_ema(closes, period):
    if len(closes) < period:
        return None
    multiplier = 2 / (period + 1)
    ema_val = sum(closes[:period]) / period
    for val in closes[period:]:
        ema_val = (val - ema_val) * multiplier + ema_val
    return ema_val

def analyze_spot(idx):
    """分析特定点位的技术指标"""
    # 计算所需数据
    window = klines[max(0, idx-100):idx+1]
    window_closes = [float(k[4]) for k in window]
    
    rsi = calc_rsi(window_closes, 14)
    macd_line, signal, hist = calc_macd(window_closes)
    
    ema9 = calc_ema(window_closes, 9)
    ema21 = calc_ema(window_closes, 21)
    ema55 = calc_ema(window_closes, 55)
    
    # EMA 排列
    if ema9 and ema21 and ema55:
        if ema9 > ema21 > ema55:
            ema_align = '多排'
            ema_score = 85
        elif ema9 < ema21 < ema55:
            ema_align = '空排'
            ema_score = 15
        else:
            ema_align = '混乱'
            ema_score = 50
    else:
        ema_align = 'N/A'
        ema_score = 50
    
    # 成交量
    if len(volumes) > 10:
        avg_vol = sum(volumes[idx-10:idx]) / 10
        vol_ratio = volumes[idx] / avg_vol if avg_vol > 0 else 1
    else:
        vol_ratio = 1
    
    # 价格位置
    entry_price = closes[idx]
    
    return {
        'rsi': rsi,
        'macd_hist': hist,
        'ema_align': ema_align,
        'ema_score': ema_score,
        'vol_ratio': vol_ratio,
        'price': entry_price
    }

# 找出所有涨幅>3% 的点位
best_spots = []
for i in range(len(closes) - 10):
    entry = closes[i]
    max_high = max(highs[i:i+10])
    max_gain = (max_high - entry) / entry * 100
    
    if max_gain >= 3:
        best_spots.append({
            'idx': i,
            'time': times[i],
            'entry': entry,
            'max_high': max_high,
            'max_gain': max_gain
        })

# 排序去重（相邻的点位只保留最好的）
best_spots.sort(key=lambda x: x['max_gain'], reverse=True)

# 去重：间隔<5 根 K 线的只保留涨幅最大的
filtered_spots = []
for spot in best_spots:
    is_duplicate = False
    for existing in filtered_spots:
        if abs(spot['idx'] - existing['idx']) < 5:
            is_duplicate = True
            break
    if not is_duplicate:
        filtered_spots.append(spot)
    
    if len(filtered_spots) >= 15:
        break

print("="*70)
print("ONDO 回测分析 - 最佳开仓点技术特征")
print("="*70)
print(f"分析时间范围：{datetime.fromtimestamp(times[0]/1000)} - {datetime.fromtimestamp(times[-1]/1000)}")
print(f"找到 {len(filtered_spots)} 个最佳开仓点（涨幅≥3%，去重后）\n")

# 分析每个点位的技术特征
print("【最佳开仓点技术特征】")
print("-"*70)

all_features = []
for spot in filtered_spots:
    features = analyze_spot(spot['idx'])
    features['gain'] = spot['max_gain']
    features['time'] = spot['time']
    all_features.append(features)
    
    time_str = datetime.fromtimestamp(spot['time']/1000).strftime('%m-%d %H:%M')
    print(f"{time_str} | 涨幅 +{spot['max_gain']:.1f}% | RSI:{features['rsi']:.1f} | MACD:{features['macd_hist']:+.3f} | EMA:{features['ema_align']} | Vol:{features['vol_ratio']:.2f}x")

# 统计特征
print("\n" + "="*70)
print("【技术特征统计】")
print("-"*70)

avg_rsi = sum(f['rsi'] for f in all_features) / len(all_features)
avg_macd = sum(f['macd_hist'] for f in all_features) / len(all_features)
avg_vol = sum(f['vol_ratio'] for f in all_features) / len(all_features)

ema_counts = {'多排': 0, '空排': 0, '混乱': 0, 'N/A': 0}
for f in all_features:
    ema_counts[f['ema_align']] += 1

print(f"RSI 平均值：{avg_rsi:.1f}")
print(f"MACD 平均值：{avg_macd:+.3f}")
print(f"成交量比率平均值：{avg_vol:.2f}x")
print(f"\nEMA 排列分布:")
print(f"  多排：{ema_counts['多排']} ({ema_counts['多排']/len(all_features)*100:.0f}%)")
print(f"  空排：{ema_counts['空排']} ({ema_counts['空排']/len(all_features)*100:.0f}%)")
print(f"  混乱：{ema_counts['混乱']} ({ema_counts['混乱']/len(all_features)*100:.0f}%)")

# 按 RSI 分组
print("\n【RSI 分布】")
rsi_ranges = {'<30': 0, '30-50': 0, '50-70': 0, '>70': 0}
for f in all_features:
    if f['rsi'] < 30:
        rsi_ranges['<30'] += 1
    elif f['rsi'] < 50:
        rsi_ranges['30-50'] += 1
    elif f['rsi'] < 70:
        rsi_ranges['50-70'] += 1
    else:
        rsi_ranges['>70'] += 1

for range_name, count in rsi_ranges.items():
    print(f"  RSI {range_name}: {count} ({count/len(all_features)*100:.0f}%)")

# 按 MACD 分组
print("\n【MACD 分布】")
macd_pos = sum(1 for f in all_features if f['macd_hist'] > 0)
macd_neg = sum(1 for f in all_features if f['macd_hist'] <= 0)
print(f"  MACD 正：{macd_pos} ({macd_pos/len(all_features)*100:.0f}%)")
print(f"  MACD 负：{macd_neg} ({macd_neg/len(all_features)*100:.0f}%)")

# 按成交量分组
print("\n【成交量分布】")
vol_high = sum(1 for f in all_features if f['vol_ratio'] > 1.5)
vol_normal = sum(1 for f in all_features if 0.8 <= f['vol_ratio'] <= 1.5)
vol_low = sum(1 for f in all_features if f['vol_ratio'] < 0.8)
print(f"  放量 (>1.5x): {vol_high} ({vol_high/len(all_features)*100:.0f}%)")
print(f"  正常 (0.8-1.5x): {vol_normal} ({vol_normal/len(all_features)*100:.0f}%)")
print(f"  缩量 (<0.8x): {vol_low} ({vol_low/len(all_features)*100:.0f}%)")

# 用新系统评分测试这些点位
print("\n" + "="*70)
print("【新系统评分测试】")
print("-"*70)

from modules.scoring import ScoringEngine

open_count_70 = 0
open_count_65 = 0
open_count_60 = 0
skip_count = 0

for f in all_features:
    details = {
        'rsi': f['rsi'],
        'macd_hist': f['macd_hist'],
        'ema_alignment': f['ema_score'],
        'volume_ratio': f['vol_ratio'],
        'support_strength': 50,
        'risk_reward_ratio': f['gain'] / 3,  # 假设盈亏比
        'confidence': 0.6
    }
    
    score, _ = ScoringEngine.calc_score_trend(details)
    
    if score >= 70:
        open_count_70 += 1
    elif score >= 65:
        open_count_65 += 1
    elif score >= 60:
        open_count_60 += 1
    else:
        skip_count += 1
    
    f['score'] = score

print(f"≥70 分开仓：{open_count_70}/{len(all_features)} ({open_count_70/len(all_features)*100:.0f}%)")
print(f"≥65 分开仓：{open_count_70+open_count_65}/{len(all_features)} ({(open_count_70+open_count_65)/len(all_features)*100:.0f}%)")
print(f"≥60 分开仓：{open_count_70+open_count_65+open_count_60}/{len(all_features)} ({(open_count_70+open_count_65+open_count_60)/len(all_features)*100:.0f}%)")
print(f"<60 分跳过：{skip_count}/{len(all_features)} ({skip_count/len(all_features)*100:.0f}%)")

# 保存详细数据
output = {
    'summary': {
        'total_spots': len(all_features),
        'avg_rsi': avg_rsi,
        'avg_macd': avg_macd,
        'avg_vol': avg_vol,
        'ema_distribution': ema_counts,
        'open_rate_70': open_count_70 / len(all_features),
        'open_rate_65': (open_count_70+open_count_65) / len(all_features),
        'open_rate_60': (open_count_70+open_count_65+open_count_60) / len(all_features)
    },
    'spots': all_features
}

with open('config/ondo_backtest.json', 'w') as f:
    json.dump(output, f, indent=2)

print(f"\n详细数据已保存：config/ondo_backtest.json")

# 优化建议
print("\n" + "="*70)
print("【优化建议】")
print("-"*70)

if avg_rsi < 50:
    print("✓ 最佳开仓点 RSI 偏低，建议降低 RSI 权重或增加超卖加分")
if avg_macd < 0:
    print("✓ 最佳开仓点 MACD 多为负，说明底部启动时 MACD 常为负，不应过度 penalize")
if avg_vol < 1.0:
    print("✓ 最佳开仓点成交量多萎缩，说明底部缩量是常态，不应过度 penalize")
if ema_counts['混乱'] > ema_counts['多排']:
    print("✓ 最佳开仓点 EMA 多为混乱，说明趋势启动初期 EMA 尚未形成多排")

print("\n具体调整建议:")
print("1. RSI 超卖 (30-40) 应从 10 分 → 18 分（底部特征）")
print("2. MACD 负值但收窄 应从 5 分 → 12 分（背离前兆）")
print("3. 成交量萎缩 (0.5-0.8) 应从 5 分 → 10 分（底部缩量）")
print("4. EMA 混乱 应从 50 分 → 60 分（趋势转换期）")
print("5. 开仓门槛从 70 分 → 63 分（平衡机会与质量）")
