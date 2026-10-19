#!/usr/bin/env python3
"""
清算区止盈止损集成测试 v3.2

测试场景：
1. 止盈计算：清算区优先于结构位
2. 止损计算：清算区调整止损位置
3. 分批止盈：场景 C/G 使用清算区
4. 入场过滤：阻挡清算区过滤信号
5. 降级处理：API 失败时优雅降级
"""

import sys
from pathlib import Path

# 添加模块路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from modules.liquidation_data import LiquidationData, LiquidationZone
from modules.sl_handler_liquidation import (
    calc_tp_with_liquidation,
    calc_sl_with_liquidation,
    calc_batch_tp_with_liquidation,
    check_liquidation_block,
)
from modules.dynamic_baseline import compute as compute_baseline


def generate_test_klines(base_price: float = 50000, count: int = 200):
    """生成模拟 K 线数据用于测试（列表格式：[time, open, high, low, close, vol]）"""
    import random
    klines = []
    price = base_price
    base_time = 1700000000000
    for i in range(count):
        change = random.uniform(-0.02, 0.02)
        close = price * (1 + change)
        high = max(price, close) * (1 + random.uniform(0, 0.01))
        low = min(price, close) * (1 - random.uniform(0, 0.01))
        open_p = price
        volume = random.uniform(1000, 5000)
        # 列表格式：[time, open, high, low, close, vol]
        klines.append([
            base_time + i * 3600000,  # 1 小时间隔
            open_p,
            high,
            low,
            close,
            volume,
        ])
        price = close
    return klines


def test_tp_with_liquidation_long():
    """测试多头止盈计算（清算区优先）"""
    print("\n📈 测试 1：多头止盈计算")
    
    klines = generate_test_klines(50000)
    bl = compute_baseline(klines)
    entry = 50000
    sl = 49000
    risk = entry - sl
    
    # 创建清算数据：上方有空头清算区
    # 使用巨大的清算量确保优先选择清算区
    liq_data = LiquidationData(
        symbol="BTCUSDT",
        long_liquidations=[],
        short_liquidations=[
            LiquidationZone(price=51200, liquidation_amount=500000000, side="short", symbol="BTCUSDT"),
            LiquidationZone(price=52000, liquidation_amount=800000000, side="short", symbol="BTCUSDT"),
        ],
        total_long_amount=0,
        total_short_amount=1300000000,
        timestamp=0
    )
    
    result = calc_tp_with_liquidation(
        entry=entry, sl=sl, direction="LONG",
        candles=klines, baseline=bl, liq_data=liq_data
    )
    
    tp = result["tp_price"]
    tp_source = result["tp_source"]
    
    print(f"  entry={entry}, sl={sl}, risk={risk}")
    print(f"  tp={tp}, tp_source={tp_source}")
    print(f"  清算区：51200 (500M), 52000 (800M)")
    
    # 验证：应该使用清算区（具体价格可能受结构位影响）
    assert tp_source == "liq_zone", f"预期 tp_source=liq_zone，实际={tp_source}"
    assert tp in [51200, 52000], f"预期 tp=51200 或 52000，实际={tp}"
    print("  ✅ 通过：止盈目标为第一个清算区")


def test_tp_without_liquidation():
    """测试降级：无清算数据时使用结构位"""
    print("\n📉 测试 2：止盈降级（无清算数据）")
    
    klines = generate_test_klines(50000)
    bl = compute_baseline(klines)
    entry = 50000
    sl = 49000
    
    # 空清算数据
    liq_data = LiquidationData(
        symbol="BTCUSDT",
        long_liquidations=[],
        short_liquidations=[],
        total_long_amount=0,
        total_short_amount=0,
        timestamp=0
    )
    
    result = calc_tp_with_liquidation(
        entry=entry, sl=sl, direction="LONG",
        candles=klines, baseline=bl, liq_data=liq_data
    )
    
    tp = result["tp_price"]
    tp_source = result["tp_source"]
    
    print(f"  tp={tp}, tp_source={tp_source}")
    assert tp_source in ["structure", "rr_fallback"], f"预期 structure/rr_fallback，实际={tp_source}"
    print("  ✅ 通过：降级到结构位或固定 RR")


def test_sl_with_liquidation_adjustment():
    """测试止损调整：清算区在止损路径上时前移止损"""
    print("\n🛡️  测试 3：止损调整（清算区保护）")
    
    klines = generate_test_klines(50000)
    bl = compute_baseline(klines)
    entry = 50000
    
    # 创建清算数据：下方有多头清算区
    liq_data = LiquidationData(
        symbol="BTCUSDT",
        long_liquidations=[
            LiquidationZone(price=49300, liquidation_amount=60000000, side="long", symbol="BTCUSDT"),
        ],
        short_liquidations=[],
        total_long_amount=60000000,
        total_short_amount=0,
        timestamp=0
    )
    
    result = calc_sl_with_liquidation(
        entry=entry, direction="LONG", scene="A_pullback",
        candles=klines, baseline=bl, liq_data=liq_data
    )
    
    sl = result["sl_price"]
    
    print(f"  entry={entry}")
    print(f"  sl={sl}")
    print(f"  清算区：49300 (60M)")
    
    # 验证：止损应该在清算区上方（避免被扫）
    assert sl > 49300, f"预期 sl > 49300（清算区上方），实际={sl}"
    print(f"  ✅ 通过：止损上移到清算区上方 ({sl})")


def test_batch_tp_with_liquidation():
    """测试分批止盈（场景 C/G）"""
    print("\n🎯 测试 4：分批止盈（清算区）")
    
    klines = generate_test_klines(50000)
    bl = compute_baseline(klines)
    entry = 50000
    sl = 49000
    
    # 创建多个清算区
    liq_data = LiquidationData(
        symbol="BTCUSDT",
        long_liquidations=[],
        short_liquidations=[
            LiquidationZone(price=51000, liquidation_amount=30000000, side="short", symbol="BTCUSDT"),
            LiquidationZone(price=51500, liquidation_amount=50000000, side="short", symbol="BTCUSDT"),
            LiquidationZone(price=52000, liquidation_amount=80000000, side="short", symbol="BTCUSDT"),
        ],
        total_long_amount=0,
        total_short_amount=160000000,
        timestamp=0
    )
    
    result = calc_batch_tp_with_liquidation(
        entry=entry, sl=sl, direction="LONG",
        candles=klines, baseline=bl, liq_data=liq_data
    )
    
    tp1 = result["tp1_price"]
    tp2 = result["tp2_price"]
    
    print(f"  entry={entry}, sl={sl}")
    print(f"  tp1={tp1} (第一个满足 RR≥1.2 的清算区)")
    print(f"  tp2={tp2} (量最大的清算区)")
    
    # 验证：tp1 应该是第一个满足 RR 的清算区，tp2 应该是量最大的
    assert tp1 in [51000, 51500], f"tp1 应该是 51000 或 51500，实际={tp1}"
    assert tp2 == 52000, f"tp2 应该是 52000（量最大），实际={tp2}"
    print("  ✅ 通过：分批止盈目标正确")


def test_liquidation_block():
    """测试入场过滤：阻挡清算区"""
    print("\n🚫 测试 5：入场过滤（清算区阻挡）")
    
    entry = 50000
    tp = 52000
    
    # 创建巨大的反向清算区（在 entry 和 tp 之间）
    liq_data = LiquidationData(
        symbol="BTCUSDT",
        long_liquidations=[
            LiquidationZone(price=51000, liquidation_amount=500000000, side="long", symbol="BTCUSDT"),
        ],
        short_liquidations=[],
        total_long_amount=500000000,
        total_short_amount=0,
        timestamp=0
    )
    
    bl = compute_baseline(generate_test_klines(50000))
    
    result = check_liquidation_block(
        entry=entry, tp=tp, direction="LONG",
        liq_data=liq_data, baseline=bl
    )
    
    is_blocked = result[0]
    reason = result[1]
    
    print(f"  entry={entry}, tp={tp}")
    print(f"  阻挡清算区：51000 (500M)")
    print(f"  is_blocked={is_blocked}, reason={reason}")
    
    assert is_blocked == True, f"预期被阻挡，实际={is_blocked}"
    print("  ✅ 通过：信号被过滤（进观察池）")


def test_none_liquidation_data():
    """测试 None 清算数据的降级处理"""
    print("\n🔄 测试 6：None 清算数据降级")
    
    klines = generate_test_klines(50000)
    bl = compute_baseline(klines)
    entry = 50000
    sl = 49000
    
    # liq_data=None
    result = calc_tp_with_liquidation(
        entry=entry, sl=sl, direction="LONG",
        candles=klines, baseline=bl, liq_data=None
    )
    
    tp = result["tp_price"]
    tp_source = result["tp_source"]
    
    print(f"  liq_data=None")
    print(f"  tp={tp}, tp_source={tp_source}")
    assert tp_source in ["structure", "rr_fallback"], f"预期降级，实际={tp_source}"
    print("  ✅ 通过：None 数据优雅降级")


def main():
    print("=" * 60)
    print("🦞 清算区止盈止损集成测试 v3.2")
    print("=" * 60)
    
    try:
        test_tp_with_liquidation_long()
        test_tp_without_liquidation()
        test_sl_with_liquidation_adjustment()
        test_batch_tp_with_liquidation()
        test_liquidation_block()
        test_none_liquidation_data()
        
        print("\n" + "=" * 60)
        print("✅ 所有测试通过！")
        print("=" * 60)
        return 0
        
    except AssertionError as e:
        print(f"\n❌ 测试失败：{e}")
        return 1
    except Exception as e:
        print(f"\n💥 测试异常：{e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
