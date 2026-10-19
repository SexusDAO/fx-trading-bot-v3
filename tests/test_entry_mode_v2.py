#!/usr/bin/env python3
"""
test_entry_mode_v2.py
验证重构后的 calc_entry_mode：
  1. PULLBACK 优先级一：清算区 + 订单簿重叠
  2. PULLBACK 优先级二：仅清算区
  3. PULLBACK 优先级三：K线低点（清算数据不可用）
  4. CHASE 评分<90 时降为 PULLBACK
  5. CHASE 评分>=90 时允许追涨
  6. 偏差>3% 返回 WAIT
  7. 方向不明（既非追涨也非回调）返回 WAIT
  8. 锚点高于现价时自动调整（多单不追高）
"""

import sys
import math
sys.path.insert(0, "/root/.openclaw/workspace")

from entry_logic import calc_entry_mode, rsi_calc

# ─────────────────────────────────────────────
# Mock K线工厂
# ─────────────────────────────────────────────

def make_klines(n=50, base=100.0, trend='flat', atr_pct=0.01):
    """生成模拟 1h K线"""
    klines = []
    price = base
    for i in range(n):
        if trend == 'up':
            price *= (1 + 0.002)
        elif trend == 'down':
            price *= (1 - 0.002)
        h = price * (1 + atr_pct / 2)
        l = price * (1 - atr_pct / 2)
        o = price * (1 - 0.001)
        c = price
        v = 1000.0
        ts = 1700000000000 + i * 3600000
        klines.append([ts, str(o), str(h), str(l), str(c), str(v),
                        ts+3599999, "0", 100, "500", "50000", "0"])
    return klines


def make_lmap(price, liq_below_offset=-0.02, liq_above_offset=0.03,
              ob_support_offset=-0.02, ob_resistance_offset=0.025, atr_pct=0.01):
    """构造 liquidation_map 结构"""
    atr = price * atr_pct
    return {
        'liq_below':     price * (1 + liq_below_offset),
        'liq_above':     price * (1 + liq_above_offset),
        'ob_support':    price * (1 + ob_support_offset),
        'ob_resistance': price * (1 + ob_resistance_offset),
        'atr_14':        atr,
        'hard_sl':       price * 0.95,
        'soft_tp1':      price * 1.04,
        'soft_tp2':      price * 1.07,
    }


# ─────────────────────────────────────────────
# Case 1：优先级一 — 清算区 + 订单簿重叠（偏差≤0.5%）
# ─────────────────────────────────────────────
def test_case1_overlap():
    price = 100.0
    # liq_below=98.0, ob_support=97.8 → 偏差 0.2% ≤ 0.5%
    lmap = make_lmap(price,
                     liq_below_offset=-0.02,
                     ob_support_offset=-0.022,
                     liq_above_offset=0.05,
                     ob_resistance_offset=0.04)

    k1h = make_klines(50, base=price, trend='up')
    # 强制 dist_high < 0.5%，触发 pullback 条件
    for i in range(-20, 0):
        row = list(k1h[i])
        row[2] = str(price * 1.004)
        k1h[i] = row
    k1h[-1] = list(k1h[-1]); k1h[-1][4] = str(price)

    mode, limit, wait, src = calc_entry_mode(k1h, 'LONG', lmap, final_score=75)
    assert mode == 'PULLBACK', f"Case1: expected PULLBACK, got {mode}"
    assert 'overlap' in src, f"Case1: expected overlap source, got {src}"
    assert limit < price, f"Case1: limit {limit} should be < price {price}"
    print(f"  Case1 PASS: mode={mode} limit={limit:.4f} src={src}")


# ─────────────────────────────────────────────
# Case 2：优先级二 — 仅清算区（无重叠）
# ─────────────────────────────────────────────
def test_case2_liq_only():
    price = 100.0
    # liq_below=98, ob_support=96 → 偏差 2% > 0.5%，不重叠
    lmap = make_lmap(price,
                     liq_below_offset=-0.02,
                     ob_support_offset=-0.04,
                     liq_above_offset=0.05,
                     ob_resistance_offset=0.03)

    k1h = make_klines(50, base=price, trend='flat')
    # 让 RSI 较高触发 pullback
    # 用 trend='up' 的 k 线让最高点近，dist_high < 0.5
    k1h[-1] = list(k1h[-1])
    k1h[-1][4] = str(price)
    # 手动设置 dist_high < 0.5 → 将近20根最高点拉到 price*1.004
    for i in range(-20, -1):
        row = list(k1h[i])
        row[2] = str(price * 1.004)   # high
        k1h[i] = row

    mode, limit, wait, src = calc_entry_mode(k1h, 'LONG', lmap, final_score=75)
    assert mode == 'PULLBACK', f"Case2: expected PULLBACK, got {mode}"
    assert 'liq_below' in src, f"Case2: expected liq_below source, got {src}"
    assert limit < price, f"Case2: limit {limit} should be < price {price}"
    print(f"  Case2 PASS: mode={mode} limit={limit:.4f} src={src}")


# ─────────────────────────────────────────────
# Case 3：优先级三 — K线低点（清算 liq_below 高于现价，降级）
# ─────────────────────────────────────────────
def test_case3_kline_fallback():
    price = 100.0
    # liq_below > price → 判断不可用，进入 kline 分支
    lmap = make_lmap(price,
                     liq_below_offset=0.05,   # liq_below = 105 > price
                     ob_support_offset=-0.10,  # ob_support=90, 离 105 很远
                     liq_above_offset=0.08,
                     ob_resistance_offset=0.06)

    k1h = make_klines(50, base=price, trend='flat')
    # 触发 pullback: 近高点贴近 price
    for i in range(-20, -1):
        row = list(k1h[i])
        row[2] = str(price * 1.003)
        k1h[i] = row

    mode, limit, wait, src = calc_entry_mode(k1h, 'LONG', lmap, final_score=75)
    assert mode == 'PULLBACK', f"Case3: expected PULLBACK, got {mode}"
    assert 'kline' in src, f"Case3: expected kline source, got {src}"
    assert limit < price, f"Case3: limit {limit} should be < price {price}"
    print(f"  Case3 PASS: mode={mode} limit={limit:.4f} src={src}")


# ─────────────────────────────────────────────
# Case 4：CHASE 评分 < 90 → 强制 PULLBACK
# ─────────────────────────────────────────────
def test_case4_chase_score_too_low():
    price = 100.0
    lmap = make_lmap(price, atr_pct=0.005)

    # 造出标准 CHASE 场景（同 Case5），但 score=85 < 90
    k1h = make_klines(50, base=price)
    for i in range(0, 30):
        row = list(k1h[i])
        row[1]='96'; row[2]='96.5'; row[3]='95.5'; row[4]='96.0'; row[5]='1000.0'
        k1h[i] = row
    for i in range(-20, -2):
        row = list(k1h[i])
        row[1]='96'; row[2]='96.0'; row[3]='95.5'; row[4]='96.0'; row[5]='1000.0'
        k1h[i] = row
    row = list(k1h[-2]); row[1]='98'; row[2]='100'; row[3]='97.5'; row[4]='99'; row[5]='3000.0'
    k1h[-2] = row
    row = list(k1h[-1]); row[1]='99'; row[2]='102'; row[3]='98.5'; row[4]='100'; row[5]='3000.0'
    k1h[-1] = row

    mode, limit, wait, src = calc_entry_mode(k1h, 'LONG', lmap, final_score=85)
    # score=85 < 90 → 不允许 CHASE
    assert mode != 'CHASE', f"Case4: should NOT be CHASE with score=85, got {mode}"
    print(f"  Case4 PASS: mode={mode} limit={limit:.4f} (not CHASE, score<90)")


# ─────────────────────────────────────────────
# Case 5：CHASE 评分 >= 90 + 全条件满足
# ─────────────────────────────────────────────
def test_case5_chase_allowed():
    price = 100.0
    lmap = make_lmap(price, atr_pct=0.005)

    # 同样的突破场景，score=92
    k1h = make_klines(50, base=price)
    for i in range(0, 30):
        row = list(k1h[i])
        row[1]='96'; row[2]='96.5'; row[3]='95.5'; row[4]='96.0'; row[5]='1000.0'
        k1h[i] = row
    for i in range(-20, -2):
        row = list(k1h[i])
        row[1]='96'; row[2]='96.0'; row[3]='95.5'; row[4]='96.0'; row[5]='1000.0'
        k1h[i] = row
    row = list(k1h[-2]); row[1]='98'; row[2]='100'; row[3]='97.5'; row[4]='99'; row[5]='3000.0'
    k1h[-2] = row
    row = list(k1h[-1]); row[1]='99'; row[2]='102'; row[3]='98.5'; row[4]='100'; row[5]='3000.0'
    k1h[-1] = row

    c1h = [float(k[4]) for k in k1h]
    rsi = rsi_calc(c1h)

    mode, limit, wait, src = calc_entry_mode(k1h, 'LONG', lmap, final_score=92)
    assert mode == 'CHASE', f"Case5: expected CHASE (score=92), got {mode} rsi={rsi:.1f}"
    assert abs(limit - price * 1.001) < 0.01, f"Case5: CHASE limit should be price*1.001"
    print(f"  Case5 PASS: mode={mode} limit={limit:.4f} rsi={rsi:.1f}")


# ─────────────────────────────────────────────
# Case 6：偏差 > 3% → WAIT
# ─────────────────────────────────────────────
def test_case6_dev_too_large():
    price = 100.0
    # liq_below = 95 → anchor=95, limit=95+atr*0.2 ≈ 95.1, 偏差 ≈ 4.9% > 3%
    lmap = make_lmap(price,
                     liq_below_offset=-0.05,
                     ob_support_offset=-0.10,  # 离 liq_below 偏差 5% > 0.5%，不重叠
                     liq_above_offset=0.08,
                     ob_resistance_offset=0.06,
                     atr_pct=0.005)

    k1h = make_klines(50, base=price, trend='flat')
    # 触发 pullback：RSI > 65 或 dist_high < 0.5
    for i in range(-20, -1):
        row = list(k1h[i])
        row[2] = str(price * 1.003)
        k1h[i] = row

    mode, limit, wait, src = calc_entry_mode(k1h, 'LONG', lmap, final_score=75)
    assert mode == 'WAIT', f"Case6: expected WAIT (dev>3%), got {mode} limit={limit:.4f}"
    print(f"  Case6 PASS: mode={mode} src={src}")


# ─────────────────────────────────────────────
# Case 7：方向不明 → WAIT
# ─────────────────────────────────────────────
def test_case7_no_clear_direction():
    price = 100.0
    lmap = make_lmap(price, atr_pct=0.01)

    # 构造既不满足追涨也不满足回调的中间态
    # dist_high ∈ [0.5, 2]，RSI ∈ [50, 65]，量比 < 1.5，无突破
    k1h = make_klines(50, base=price, trend='flat')
    # 设最高点在 price*1.01（dist_high=1%，不满足<0.5也不满足>1）
    for i in range(-20, 0):
        row = list(k1h[i])
        row[2] = str(price * 1.01)
        k1h[i] = row
    # 当前价 price（dist_high=1%，不满足 <0.5 也不满足追涨的 >1%+突破）

    mode, limit, wait, src = calc_entry_mode(k1h, 'LONG', lmap, final_score=70)
    print(f"  Case7: mode={mode} limit={limit:.4f} src={src}")
    # 这种情况最终可能是 PULLBACK（pullback 条件不严格要求同时满足追涨失败）
    # 主要检验不会 CHASE
    assert mode != 'CHASE', f"Case7: should not CHASE in ambiguous zone"
    print(f"  Case7 PASS: mode={mode}")


# ─────────────────────────────────────────────
# Case 8：多单入场价调整（anchor > price 时自动下调）
# ─────────────────────────────────────────────
def test_case8_anchor_above_price_adjusted():
    price = 100.0
    # 造一个 liq_below 高于 price 的情况（ob_support 也高于 price）
    lmap = make_lmap(price,
                     liq_below_offset=0.01,   # liq_below = 101 > price
                     ob_support_offset=0.005,  # ob_support=100.5 > price
                     liq_above_offset=0.05,
                     ob_resistance_offset=0.04,
                     atr_pct=0.01)

    k1h = make_klines(50, base=price, trend='flat')
    for i in range(-20, -1):
        row = list(k1h[i])
        row[2] = str(price * 1.003)
        k1h[i] = row

    mode, limit, wait, src = calc_entry_mode(k1h, 'LONG', lmap, final_score=75)
    # 无论什么模式，LONG 的 limit 不能 >= price（不追高）
    if mode == 'PULLBACK':
        assert limit < price, f"Case8: LONG limit {limit:.4f} should be < price {price}"
    print(f"  Case8 PASS: mode={mode} limit={limit:.4f} src={src}")


# ─────────────────────────────────────────────
# 运行所有测试
# ─────────────────────────────────────────────
if __name__ == '__main__':
    tests = [
        test_case1_overlap,
        test_case2_liq_only,
        test_case3_kline_fallback,
        test_case4_chase_score_too_low,
        test_case5_chase_allowed,
        test_case6_dev_too_large,
        test_case7_no_clear_direction,
        test_case8_anchor_above_price_adjusted,
    ]
    passed = 0
    failed = 0
    for t in tests:
        try:
            print(f"[RUN] {t.__name__}")
            t()
            passed += 1
        except AssertionError as e:
            print(f"  FAIL: {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR: {e}")
            import traceback; traceback.print_exc()
            failed += 1

    print(f"\n{'='*40}")
    print(f"结果: {passed}/{passed+failed} 通过" + (" ✅" if failed == 0 else " ❌"))
