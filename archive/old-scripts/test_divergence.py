#!/usr/bin/env python3
"""
背离检测专项测试
"""
import sys
sys.path.insert(0, '/root/.openclaw/workspace')

from modules.scoring import DivergenceDetector

# 测试底背离
print("="*60)
print("背离检测测试")
print("="*60)

# 构造底背离场景
# 价格：第一低点 100，第二低点 95（新低）
# MACD: 第一低点 -0.5，第二低点 -0.3（未新低）
print("\n[测试 1] 底背离（价格新低，MACD 未新低）")

klines_bullish = []
base_time = 1700000000000

# 第一段下跌到 100
for i in range(10):
    low = 105 - i * 0.5
    klines_bullish.append([
        base_time + i * 3600000,
        low + 2, low + 5, low, low + 3, 1000
    ])

# 反弹
for i in range(5):
    low = 100 + i * 0.5
    klines_bullish.append([
        base_time + (10 + i) * 3600000,
        low + 2, low + 5, low, low + 3, 1000
    ])

# 第二段下跌到 95（新低）但 MACD 应该更高
for i in range(10):
    low = 102.5 - i * 0.75
    klines_bullish.append([
        base_time + (15 + i) * 3600000,
        low + 2, low + 5, low, low + 3, 1000
    ])

has_div, strength = DivergenceDetector.detect_bullish_divergence(klines_bullish)
print(f"底背离：{'✅ 检测到' if has_div else '❌ 未检测到'} (强度：{strength})")

# 构造顶背离场景
# 价格：第一高点 100，第二高点 105（新高）
# MACD: 第一高点 0.5，第二高点 0.3（未新高）
print("\n[测试 2] 顶背离（价格新高，MACD 未新高）")

klines_bearish = []

# 第一段上涨到 100
for i in range(10):
    high = 95 + i * 0.5
    klines_bearish.append([
        base_time + i * 3600000,
        high - 2, high + 2, high, high - 1, 1000
    ])

# 回调
for i in range(5):
    high = 100 - i * 0.5
    klines_bearish.append([
        base_time + (10 + i) * 3600000,
        high - 2, high + 2, high, high - 1, 1000
    ])

# 第二段上涨到 105（新高）但 MACD 应该更低
for i in range(10):
    high = 97.5 + i * 0.75
    klines_bearish.append([
        base_time + (15 + i) * 3600000,
        high - 2, high + 2, high, high - 1, 1000
    ])

has_div, strength = DivergenceDetector.detect_bearish_divergence(klines_bearish)
print(f"顶背离：{'✅ 检测到' if has_div else '❌ 未检测到'} (强度：{strength})")

# 测试无背离场景
print("\n[测试 3] 无背离（价格和 MACD 同步）")

klines_normal = []
for i in range(30):
    low = 100 - i * 0.3  # 持续下跌
    klines_normal.append([
        base_time + i * 3600000,
        low + 2, low + 5, low, low + 3, 1000
    ])

has_div, strength = DivergenceDetector.detect_bullish_divergence(klines_normal)
print(f"无背离：{'❌ 错误检测到' if has_div else '✅ 正确未检测到'} (强度：{strength})")

print("\n" + "="*60)
print("测试完成")
print("="*60)
