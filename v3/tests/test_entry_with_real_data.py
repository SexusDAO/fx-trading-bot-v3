#!/usr/bin/env python3
"""
test_entry_with_real_data.py — 使用真实 API 数据验证入场价优化
"""

import sys
from pathlib import Path

WORKSPACE = Path(__file__).parent.parent
sys.path.insert(0, str(WORKSPACE))

from modules.binance_api import get_klines, get_ticker
from modules.entry_calc import calc_dynamic_entry, _atr, _closes


def test_with_real_symbol(symbol: str):
    """用真实标的测试入场价计算"""
    print(f"\n{'='*60}")
    print(f"测试：{symbol}")
    print('='*60)
    
    # 获取真实数据
    k1h = get_klines(symbol, "1h", limit=100)
    if not k1h or len(k1h) < 20:
        print(f"❌ {symbol} K 线数据不足")
        return False
    
    ticker = get_ticker(symbol)
    current_price = float(ticker.get("lastPrice", 0))
    atr_val = _atr(k1h)
    
    print(f"当前价：{current_price:.6g}")
    print(f"ATR(14): {atr_val:.6g} ({atr_val/current_price*100:.3f}%)")
    
    # 测试空头入场
    entry_short, method_short = calc_dynamic_entry("SHORT", current_price, k1h, atr_val)
    offset_short_pct = (entry_short - current_price) / current_price * 100
    offset_short_atr = (entry_short - current_price) / atr_val
    
    print(f"\n空头入场价：{entry_short:.6g} (+{offset_short_pct:.3f}% = +{offset_short_atr:.2f}×ATR) [方法：{method_short}]")
    
    # 测试多头入场
    entry_long, method_long = calc_dynamic_entry("LONG", current_price, k1h, atr_val)
    offset_long_pct = (current_price - entry_long) / current_price * 100
    offset_long_atr = (current_price - entry_long) / atr_val
    
    print(f"多头入场价：{entry_long:.6g} (-{offset_long_pct:.3f}% = -{offset_long_atr:.2f}×ATR) [方法：{method_long}]")
    
    # 验证
    errors = []
    
    if entry_short <= current_price:
        errors.append(f"空头入场价{entry_short} 必须高于现价{current_price}")
    
    if entry_long >= current_price:
        errors.append(f"多头入场价{entry_long} 必须低于现价{current_price}")
    
    if not (0.15 <= offset_short_atr <= 1.5):
        errors.append(f"空头偏离{offset_short_atr:.2f}×ATR 超出合理范围 (0.15~1.5)")
    
    if not (0.15 <= offset_long_atr <= 1.5):
        errors.append(f"多头偏离{offset_long_atr:.2f}×ATR 超出合理范围 (0.15~1.5)")
    
    if errors:
        print("\n❌ 验证失败:")
        for e in errors:
            print(f"  - {e}")
        return False
    else:
        print("\n✅ 验证通过")
        return True


def main():
    print("=" * 60)
    print("动态入场价优化验证（真实数据）v2.1")
    print("=" * 60)
    
    # 测试不同波动率的标的
    symbols = [
        ("BTCUSDT", "低波动 - 大盘"),
        ("ETHUSDT", "低波动 - 大盘"),
        ("SOLUSDT", "中波动 - 主流"),
        ("PEPEUSDT", "高波动 - MEME"),
    ]
    
    passed = 0
    failed = 0
    
    for symbol, desc in symbols:
        print(f"\n>> {symbol} ({desc})")
        try:
            if test_with_real_symbol(symbol):
                passed += 1
            else:
                failed += 1
        except Exception as e:
            print(f"❌ {symbol} 异常：{e}")
            failed += 1
    
    print("\n" + "=" * 60)
    print(f"测试结果：{passed} 通过 / {failed} 失败")
    print("=" * 60)
    
    # 总结
    print("\n📊 优化要点总结:")
    print("  1. ATR 缓冲：0.25×ATR（替代固定 0.3%）")
    print("  2. 阻力位/支撑位：在技术位附近挂单，提高成交质量")
    print("  3. 清算区：在清算区另一侧挂单，避免被密集止损扫掉")
    print("  4. 安全范围：±1.5×ATR（超过就不追）")
    
    return failed == 0


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
