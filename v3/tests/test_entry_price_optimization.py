#!/usr/bin/env python3
"""
test_entry_price_optimization.py — 测试动态入场价优化

验证三层逻辑：
  1. ATR 缓冲（解决噪音问题）
  2. 阻力位/支撑位（解决成交质量问题）
  3. 清算区锚点（解决套牢问题）
"""

import sys
from pathlib import Path

WORKSPACE = Path(__file__).parent.parent
sys.path.insert(0, str(WORKSPACE))

from modules.entry_calc import calc_dynamic_entry, _atr, _closes, _highs, _lows


def generate_realistic_klines(base_price: float, daily_volatility: float, count: int = 100) -> list:
    """
    生成更真实的 K 线数据（1 小时级别）。
    每日波动率转换为小时波动率：hourly = daily / sqrt(24)
    返回格式：[[open, high, low, close, volume], ...]
    """
    import random
    import math
    
    hourly_vol = daily_volatility / math.sqrt(24)
    klines = []
    price = base_price
    
    for i in range(count):
        # 小时波动通常在 0.2%~1% 之间
        change = random.uniform(-hourly_vol, hourly_vol)
        open_p = price
        close_p = price * (1 + change)
        # K 线高低点幅度约为收盘波动的 1.2~1.5 倍
        wick_factor = random.uniform(1.1, 1.3)
        high_p = max(open_p, close_p) * (1 + random.uniform(0, hourly_vol * wick_factor))
        low_p = min(open_p, close_p) * (1 - random.uniform(0, hourly_vol * wick_factor))
        volume = random.uniform(1000, 10000)
        klines.append([open_p, high_p, low_p, close_p, volume])
        price = close_p
    
    return klines


def test_btc_short_entry():
    """测试 BTC 空头入场价（高币价，低小时波动）"""
    print("\n=== 测试 1: BTC 空头入场价 ===")
    
    # BTC: 价格~65000，日波动~5%，小时波动~1%
    klines = generate_realistic_klines(65000, 0.05, 100)
    current_price = _closes(klines)[-1]
    atr_val = _atr(klines)
    
    print(f"当前价：{current_price:.2f}")
    print(f"ATR(14): {atr_val:.2f} ({atr_val/current_price*100:.3f}%)")
    
    # 计算动态入场价
    entry_price, method = calc_dynamic_entry("SHORT", current_price, klines, atr_val)
    
    offset_pct = (entry_price - current_price) / current_price * 100
    offset_atr = (entry_price - current_price) / atr_val
    
    print(f"入场价：{entry_price:.2f}")
    print(f"方法：{method}")
    print(f"偏离：+{offset_pct:.3f}% (+{offset_atr:.2f}×ATR)")
    
    # 验证：应该在 0.2~1.5×ATR 范围内
    assert 0.2 <= offset_atr <= 1.5, f"偏离 ATR 倍数不合理：{offset_atr}"
    assert entry_price > current_price, "空头入场价必须高于现价"
    
    print("✅ 验证通过")
    return True


def test_meme_short_entry():
    """测试 MEME 币空头入场价（低币价，高波动）"""
    print("\n=== 测试 2: MEME 币空头入场价 ===")
    
    # MEME: 价格~0.5，日波动~15%，小时波动~3%
    klines = generate_realistic_klines(0.5, 0.15, 100)
    current_price = _closes(klines)[-1]
    atr_val = _atr(klines)
    
    print(f"当前价：{current_price:.6f}")
    print(f"ATR(14): {atr_val:.6f} ({atr_val/current_price*100:.3f}%)")
    
    # 计算动态入场价
    entry_price, method = calc_dynamic_entry("SHORT", current_price, klines, atr_val)
    
    offset_pct = (entry_price - current_price) / current_price * 100
    offset_atr = (entry_price - current_price) / atr_val
    
    print(f"入场价：{entry_price:.6f}")
    print(f"方法：{method}")
    print(f"偏离：+{offset_pct:.3f}% (+{offset_atr:.2f}×ATR)")
    
    # 验证
    assert 0.2 <= offset_atr <= 1.5, f"偏离 ATR 倍数不合理：{offset_atr}"
    assert entry_price > current_price, "空头入场价必须高于现价"
    
    print("✅ 验证通过")
    return True


def test_long_entry_with_liq_zone():
    """测试多头入场价（带清算区）"""
    print("\n=== 测试 3: 多头入场价（带清算区） ===")
    
    # ETH: 价格~3500，日波动~6%，小时波动~1.2%
    klines = generate_realistic_klines(3500, 0.06, 100)
    current_price = _closes(klines)[-1]
    atr_val = _atr(klines)
    
    # 模拟清算区在现价下方 0.5% 处（合理范围）
    liq_zone = current_price * 0.995
    
    print(f"当前价：{current_price:.2f}")
    print(f"ATR(14): {atr_val:.2f} ({atr_val/current_price*100:.3f}%)")
    print(f"清算区：{liq_zone:.2f} (下方{((current_price-liq_zone)/current_price)*100:.2f}%)")
    
    # 计算动态入场价（带清算区）
    entry_price, method = calc_dynamic_entry("LONG", current_price, klines, atr_val, liq_zone)
    
    offset_pct = (current_price - entry_price) / current_price * 100
    offset_atr = (current_price - entry_price) / atr_val
    
    print(f"入场价：{entry_price:.2f}")
    print(f"方法：{method}")
    print(f"偏离：-{offset_pct:.3f}% (-{offset_atr:.2f}×ATR)")
    
    # 验证：应该在 0.2~1.5×ATR 范围内
    assert 0.2 <= offset_atr <= 1.5, f"偏离 ATR 倍数不合理：{offset_atr}"
    assert entry_price < current_price, "多头入场价必须低于现价"
    
    print("✅ 验证通过")
    return True


def test_no_liq_zone_fallback():
    """测试无清算区时的 fallback 逻辑"""
    print("\n=== 测试 4: 无清算区 fallback ===")
    
    klines = generate_realistic_klines(100, 0.08, 100)
    current_price = _closes(klines)[-1]
    atr_val = _atr(klines)
    
    print(f"当前价：{current_price:.2f}")
    print(f"ATR(14): {atr_val:.2f} ({atr_val/current_price*100:.3f}%)")
    
    # 不带清算区
    entry_price, method = calc_dynamic_entry("SHORT", current_price, klines, atr_val, None)
    
    print(f"入场价：{entry_price:.2f} (方法：{method})")
    assert entry_price > current_price, "空头入场价必须高于现价"
    assert method in ("atr_buffer", "resistance", "combined"), f"未知方法：{method}"
    
    print("✅ 验证通过")
    return True


def test_resistance_level_detection():
    """测试阻力位检测逻辑"""
    print("\n=== 测试 5: 阻力位检测 ===")
    
    # 创建一个有明显阻力位的 K 线序列
    klines = []
    base = 100
    
    # 前 80 根：震荡上行
    for i in range(80):
        price = base + i * 0.1
        klines.append([price, price * 1.005, price * 0.995, price, 5000])
    
    # 近 20 根：在 108 附近形成阻力（多次试探不过）
    for i in range(20):
        high = 108 + random.uniform(-0.2, 0.1)  # 阻力位在 108
        low = 107 + random.uniform(-0.1, 0.2)
        close = 107.5 + random.uniform(-0.3, 0.3)
        klines.append([close, high, low, close, 5000])
    
    import random
    current_price = 107.5
    atr_val = _atr(klines)
    
    print(f"当前价：{current_price:.2f}")
    print(f"ATR(14): {atr_val:.2f}")
    
    # 计算空头入场价
    entry_price, method = calc_dynamic_entry("SHORT", current_price, klines, atr_val)
    
    print(f"入场价：{entry_price:.2f} (方法：{method})")
    
    # 如果检测到阻力位，入场价应该接近阻力位下方
    if method in ("resistance", "combined"):
        print(f"✅ 检测到阻力位，入场价在阻力位附近")
    else:
        print(f"ℹ️  使用 ATR 缓冲（未检测到有效阻力位）")
    
    assert entry_price > current_price, "空头入场价必须高于现价"
    return True


def main():
    import random
    random.seed(42)  # 固定随机种子，保证可重复性
    
    print("=" * 60)
    print("动态入场价优化测试 v2.1")
    print("=" * 60)
    
    tests = [
        ("BTC 空头", test_btc_short_entry),
        ("MEME 空头", test_meme_short_entry),
        ("多头 + 清算区", test_long_entry_with_liq_zone),
        ("无清算区 fallback", test_no_liq_zone_fallback),
        ("阻力位检测", test_resistance_level_detection),
    ]
    
    passed = 0
    failed = 0
    
    for name, test_fn in tests:
        try:
            if test_fn():
                passed += 1
        except AssertionError as e:
            print(f"❌ {name} 失败：{e}")
            failed += 1
        except Exception as e:
            print(f"❌ {name} 异常：{e}")
            failed += 1
    
    print("\n" + "=" * 60)
    print(f"测试结果：{passed} 通过 / {failed} 失败")
    print("=" * 60)
    
    return failed == 0


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
