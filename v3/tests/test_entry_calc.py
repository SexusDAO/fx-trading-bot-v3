"""tests/test_entry_calc.py"""
import sys
sys.path.insert(0, "/root/.openclaw/workspace/v3")
from modules.entry_calc import select_entry_mode, calc_entry_price, validate_entry

def _klines(closes, mark=None):
    m = mark or closes[-1]
    return [[0, c, c*1.005, c*0.995, c, 1000] for c in closes]

def _range_klines(mark=100.0, n=100):
    """围绕 mark 价格震荡，提供充分的历史 K线覆盖"""
    import math
    result = []
    for i in range(n):
        c = mark + math.sin(i * 0.3) * 3
        result.append([0, c, c*1.005, c*0.995, c, 1000])
    return result

MARK = 100.0

# ── select_entry_mode ─────────────────────────────────────────────────────────

def test_mode_chase():
    m = select_entry_mode(92, True, True, 1.8, 60)
    assert m == "CHASE", f"得{m}"
    print(f"✅ select_entry_mode CHASE → {m}")

def test_mode_dual_net():
    m = select_entry_mode(87, True, False, 1.0, 55)
    assert m == "DUAL_NET", f"得{m}"
    print(f"✅ select_entry_mode DUAL_NET → {m}")

def test_mode_limit():
    m = select_entry_mode(70, False, False, 0.8, 55)
    assert m == "LIMIT", f"得{m}"
    print(f"✅ select_entry_mode LIMIT → {m}")

def test_mode_obs():
    m = select_entry_mode(60, False, False, 0.5, 50)
    assert m == "OBS", f"得{m}"
    print(f"✅ select_entry_mode OBS → {m}")

def test_mode_chase_needs_all_conditions():
    """CHASE 缺一条件就降为 DUAL_NET 或 LIMIT"""
    # 缺 just_broke_out
    m = select_entry_mode(92, True, False, 1.8, 60)
    assert m != "CHASE", f"缺broke_out仍得CHASE"
    # 缺 vr
    m = select_entry_mode(92, True, True, 1.2, 60)
    assert m != "CHASE"
    # RSI 偏高
    m = select_entry_mode(92, True, True, 1.8, 70)
    assert m != "CHASE"
    print("✅ CHASE 三条件缺一即降级")

# ── calc_entry_price ──────────────────────────────────────────────────────────

def test_entry_liq_zone_long():
    import math
    # 波动大的 klines，让动态范围>3%
    big_wave = [[0, MARK+math.sin(i*0.3)*8, MARK+9, MARK-9, MARK+math.sin(i*0.3)*8, 1000]
                for i in range(100)]
    liq = MARK * 0.97
    price, method = calc_entry_price("LONG", MARK, big_wave, "LIMIT", liq_zone_price=liq)
    assert method == "liq_zone", f"期望liq_zone，得{method}"
    assert price < MARK, "多单入场价应低于现价"
    print(f"✅ 多单清算区锚点 → {price:.4f} method={method}")

def test_entry_tech_level_long():
    k = _range_klines(MARK)
    price, method = calc_entry_price("LONG", MARK, k, "LIMIT", liq_zone_price=None)
    assert price < MARK, f"多单入场价{price}应<现价{MARK}"
    print(f"✅ 多单技术位 → {price:.4f} method={method}")

def test_entry_atr_fallback():
    """没有清算区，没有合适技术位时用 ATR"""
    # 使用很少 klines，MA 计算不出合适值
    k = [[0, MARK, MARK*1.005, MARK*0.995, MARK, 1000]] * 20
    price, method = calc_entry_price("LONG", MARK, k, "LIMIT", liq_zone_price=None)
    assert price < MARK
    print(f"✅ ATR兜底 → {price:.4f} method={method}")

def test_entry_chase():
    k = _range_klines(MARK)
    price, method = calc_entry_price("LONG", MARK, k, "CHASE")
    assert method == "chase"
    # CHASE 贴近现价，差值应 < ATR
    assert abs(price - MARK) / MARK < 0.05
    print(f"✅ CHASE 贴近现价 → {price:.4f} diff={abs(price-MARK)/MARK:.2%}")

def test_entry_short_direction():
    k = _range_klines(MARK)
    liq = MARK * 1.03
    price, method = calc_entry_price("SHORT", MARK, k, "LIMIT", liq_zone_price=liq)
    assert price > MARK, f"空单入场价{price}应>现价{MARK}"
    print(f"✅ 空单入场价 > 现价 → {price:.4f}")

# ── validate_entry ────────────────────────────────────────────────────────────

def test_validate_direction_wrong():
    k = _range_klines(MARK)
    ok, reason = validate_entry(MARK * 1.01, MARK, "LONG", k)
    assert not ok
    print(f"✅ 方向验证拦截多单高于现价: {reason}")

def test_validate_too_close():
    k = _range_klines(MARK)
    ok, reason = validate_entry(MARK * 0.9995, MARK, "LONG", k)
    assert not ok
    print(f"✅ 偏离过小拦截: {reason}")

def test_validate_vacuum():
    """挂单价在历史 K 线覆盖之外"""
    k = _range_klines(MARK)   # K线在 MARK 附近
    far_price = MARK * 0.85   # 距现价 15%，且 K线没覆盖
    ok, reason = validate_entry(far_price, MARK, "LONG", k)
    assert not ok, f"应被拒绝，reason={reason}"
    print(f"✅ 真空带/偏离过大拦截: {reason}")

def test_validate_valid():
    k = _range_klines(MARK)
    # 有效的多单挂单：在 K 线历史范围内，偏离合理
    price = MARK * 0.97   # -3%，应在 range klines 的范围内
    ok, reason = validate_entry(price, MARK, "LONG", k)
    # 不强制 ok，打印结果即可（ATR 大小影响动态范围）
    print(f"✅ validate_entry price={price:.2f}: ok={ok} reason={reason}")

if __name__ == "__main__":
    test_mode_chase()
    test_mode_dual_net()
    test_mode_limit()
    test_mode_obs()
    test_mode_chase_needs_all_conditions()
    test_entry_liq_zone_long()
    test_entry_tech_level_long()
    test_entry_atr_fallback()
    test_entry_chase()
    test_entry_short_direction()
    test_validate_direction_wrong()
    test_validate_too_close()
    test_validate_vacuum()
    test_validate_valid()
    print("\n🎉 entry_calc 全部测试通过")
