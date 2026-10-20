#!/usr/bin/env python3
"""
test_scorer_fixes.py - 验证评分器 4 个关键修复

修复内容:
  1. 强势赛道支持多空双向（连续暴跌 + 放量 → 强势空头）
  2. 4H 周期冲突直接 DISCARD（不是扣 4 分）
  3. RSI 极端值在维度 5 惩罚（>85 或<15 扣分）
  4. 强势赛道触发条件改为且（不是或）
"""

import sys
import random
from pathlib import Path

WORKSPACE = Path(__file__).parent.parent
sys.path.insert(0, str(WORKSPACE))

from modules.scorer import score
from modules.dynamic_baseline import compute as compute_baseline


def generate_klines(direction: str, count: int = 50, volatility: float = 0.02) -> list:
    """生成模拟 K 线数据 [open, high, low, close, volume]"""
    klines = []
    price = 100
    
    for i in range(count):
        if direction == "up":
            change = random.uniform(0, volatility)
        elif direction == "down":
            change = random.uniform(-volatility, 0)
        else:
            change = random.uniform(-volatility, volatility)
        
        open_p = price
        close_p = price * (1 + change)
        high_p = max(open_p, close_p) * (1 + random.uniform(0, 0.01))
        low_p = min(open_p, close_p) * (1 - random.uniform(0, 0.01))
        volume = random.uniform(1000, 5000)
        
        # 格式：[open, high, low, close, unused, volume]
        klines.append([open_p, high_p, low_p, close_p, 0, volume])
        price = close_p
    
    return klines


def test_1_momentum_supports_short():
    """测试 1：强势赛道支持做空方向"""
    print("\n=== 测试 1: 强势赛道支持做空 ===")
    
    k1h = generate_klines("down", 50, 0.05)
    k4h = generate_klines("down", 30, 0.03)
    
    last = k1h[-1]
    k1h[-1] = [last[0], last[1], last[2], last[3] * 0.95, last[4], last[5] * 3]
    
    mark_price = k1h[-1][4]
    funding = 0.001
    chg24 = -0.08
    
    result = score(k1h, k4h, mark_price, funding, chg24, "down")
    
    print(f"方向：{result['direction']}")
    print(f"赛道：{result['track']}")
    print(f"总分：{result['total']}")
    
    if result['direction'] == 'SHORT' and result['track'] == 'momentum':
        print("✅ 强势赛道正确支持做空方向")
        return True
    else:
        print(f"❌ 预期 SHORT+momentum，实际{result['direction']}+{result['track']}")
        return False


def test_2_4h_conflict_discard():
    """测试 2:4H 周期冲突直接 DISCARD"""
    print("\n=== 测试 2:4H 周期冲突 DISCARD ===")
    
    k1h = generate_klines("up", 50, 0.02)
    k4h = generate_klines("down", 30, 0.03)
    
    mark_price = k1h[-1][4]
    funding = 0.0005
    chg24 = 0.03
    
    result = score(k1h, k4h, mark_price, funding, chg24, "neutral")
    
    print(f"方向：{result['direction']}")
    print(f"趋势维度：{result['trend_clarity']['reason']}")
    
    if result['direction'] == 'DISCARD':
        print("✅ 4H 周期冲突正确触发 DISCARD")
        return True
    else:
        print(f"❌ 预期 DISCARD，实际{result['direction']}")
        return False


def test_3_rsi_extreme_penalty():
    """测试 3:RSI 极端值惩罚"""
    print("\n=== 测试 3:RSI 极端值惩罚 ===")
    
    k1h = generate_klines("up", 30, 0.03)
    k4h = generate_klines("up", 20, 0.02)
    
    mark_price = k1h[-1][4]
    funding = 0.002
    chg24 = 0.15
    
    result = score(k1h, k4h, mark_price, funding, chg24, "up")
    
    print(f"方向：{result['direction']}")
    print(f"维度 5 得分：{result['mkt_sync']['score']}/{result['mkt_sync']['max']}")
    print(f"维度 5 原因：{result['mkt_sync']['reason']}")
    
    if "RSI 超买" in result['mkt_sync']['reason'] or result['mkt_sync']['score'] < 10:
        print("✅ RSI 极端值惩罚已生效")
        return True
    else:
        print(f"❌ 未检测到 RSI 惩罚（维度 5 得分{result['mkt_sync']['score']}偏高）")
        return False


def test_4_momentum_trigger_and_condition():
    """测试 4：强势赛道触发条件改为且"""
    print("\n=== 测试 4：强势赛道触发条件（且） ===")
    
    k1h_vol_only = generate_klines("up", 50, 0.01)
    k1h_vol_only[-1][5] *= 5  # 索引 5 是 volume
    
    k1h_gain_only = generate_klines("up", 50, 0.01)
    k1h_gain_only[-1][3] *= 1.08  # 索引 3 是 close
    k1h_gain_only[-1][5] *= 0.8
    
    k1h_both = generate_klines("up", 50, 0.01)
    k1h_both[-1][3] *= 1.06  # 索引 3 是 close
    k1h_both[-1][5] *= 3     # 索引 5 是 volume
    
    k4h = generate_klines("up", 20, 0.02)
    
    result_a = score(k1h_vol_only, k4h, k1h_vol_only[-1][4], 0.001, 0.01, "up")
    print(f"场景 A（仅放量）：赛道={result_a['track']}")
    
    result_b = score(k1h_gain_only, k4h, k1h_gain_only[-1][4], 0.001, 0.08, "up")
    print(f"场景 B（仅涨幅）：赛道={result_b['track']}")
    
    result_c = score(k1h_both, k4h, k1h_both[-1][4], 0.002, 0.06, "up")
    print(f"场景 C（涨幅 + 放量）：赛道={result_c['track']}")
    
    passed = True
    if result_a['track'] != 'oscillation':
        print(f"❌ 场景 A 应该退回震荡赛道，实际{result_a['track']}")
        passed = False
    else:
        print("✅ 场景 A 正确（仅放量→震荡赛道）")
    
    if result_b['track'] != 'oscillation':
        print(f"❌ 场景 B 应该退回震荡赛道，实际{result_b['track']}")
        passed = False
    else:
        print("✅ 场景 B 正确（仅涨幅→震荡赛道）")
    
    if result_c['track'] != 'momentum':
        print(f"❌ 场景 C 应该进强势赛道，实际{result_c['track']}")
        passed = False
    else:
        print("✅ 场景 C 正确（涨幅 + 放量→强势赛道）")
    
    return passed


def main():
    print("=" * 60)
    print("评分器 4 个关键修复验证")
    print("=" * 60)
    
    random.seed(42)
    
    tests = [
        ("强势赛道支持做空", test_1_momentum_supports_short),
        ("4H 周期冲突 DISCARD", test_2_4h_conflict_discard),
        ("RSI 极端值惩罚", test_3_rsi_extreme_penalty),
        ("强势赛道触发条件（且）", test_4_momentum_trigger_and_condition),
    ]
    
    passed = 0
    failed = 0
    
    for name, test_fn in tests:
        try:
            if test_fn():
                passed += 1
            else:
                failed += 1
        except Exception as e:
            print(f"❌ {name} 异常：{e}")
            import traceback
            traceback.print_exc()
            failed += 1
    
    print("\n" + "=" * 60)
    print(f"测试结果：{passed} 通过 / {failed} 失败")
    print("=" * 60)
    
    if failed == 0:
        print("\n✅ 所有修复已正确应用！")
        print("\n修复总结:")
        print("  1. ✅ 强势赛道支持多空双向")
        print("  2. ✅ 4H 周期冲突直接 DISCARD")
        print("  3. ✅ RSI 极端值在维度 5 惩罚")
        print("  4. ✅ 强势赛道触发条件改为且")
    else:
        print(f"\n⚠️ 有{failed}个测试未通过，需要进一步检查")
    
    return failed == 0


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
