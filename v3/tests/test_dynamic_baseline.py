"""
tests/test_dynamic_baseline.py — DynamicBaseline 单元测试
"""
import sys, math
sys.path.insert(0, "/root/.openclaw/workspace/v3")


def _make_k(n=30, base=100.0, trend=0.002, vol=1000.0):
    k = []
    price = base
    for i in range(n):
        price = price * (1 + trend)
        o  = price * 0.999
        hi = price * 1.005
        lo = price * 0.995
        c  = price
        k.append([i * 3600, str(o), str(hi), str(lo), str(c), str(vol + i * 10)])
    return k


def _make_k_volatile(n=30, base=1.0):
    """模拟 MEME 币高波动"""
    import random; random.seed(42)
    k = []
    price = base
    for i in range(n):
        chg = random.uniform(-0.05, 0.08)
        price = max(price * (1 + chg), 0.0001)
        o  = price * 0.99
        hi = price * 1.08
        lo = price * 0.92
        c  = price
        vol = random.uniform(50000, 500000)
        k.append([i * 3600, str(o), str(hi), str(lo), str(c), str(vol)])
    return k


# ── 基础测试 ──────────────────────────────────────────────────────────────────

def test_baseline_fields_present():
    """返回对象包含所有必需字段"""
    from modules.dynamic_baseline import compute
    k  = _make_k(30)
    bl = compute(k)
    for field in ["high_20", "low_20", "price_range",
                  "atr_baseline", "atr_p40", "atr_p70",
                  "vol_baseline", "vol_std",
                  "rsi_mean", "rsi_std",
                  "strong_threshold"]:
        assert hasattr(bl, field), f"缺少字段: {field}"
    print(f"✅ 字段完整: atr={bl.atr_baseline:.4f} vol={bl.vol_baseline:.0f} "
          f"rsi_mean={bl.rsi_mean:.1f}")


def test_baseline_price_range():
    """price_range = high_20 - low_20 > 0"""
    from modules.dynamic_baseline import compute
    k  = _make_k(30, trend=0.003)
    bl = compute(k)
    assert bl.price_range > 0, "price_range 应 > 0"
    assert bl.high_20 >= bl.low_20, "high_20 应 ≥ low_20"
    assert abs(bl.price_range - (bl.high_20 - bl.low_20)) < 1e-9
    print(f"✅ price_range={bl.price_range:.4f} [{bl.low_20:.4f}~{bl.high_20:.4f}]")


def test_baseline_atr_ordering():
    """atr_p40 ≤ atr_baseline ≤ atr_p70（不严格，但 p40 ≤ p70）"""
    from modules.dynamic_baseline import compute
    k  = _make_k(40, trend=0.001)
    bl = compute(k)
    assert bl.atr_p40 <= bl.atr_p70, \
        f"atr_p40({bl.atr_p40:.5f}) 应 ≤ atr_p70({bl.atr_p70:.5f})"
    assert bl.atr_baseline > 0
    print(f"✅ ATR: baseline={bl.atr_baseline:.5f} p40={bl.atr_p40:.5f} p70={bl.atr_p70:.5f}")


def test_baseline_vol_stats():
    """vol_baseline > 0, vol_std ≥ 0"""
    from modules.dynamic_baseline import compute
    k  = _make_k(25, vol=5000.0)
    bl = compute(k)
    assert bl.vol_baseline > 0
    assert bl.vol_std >= 0
    print(f"✅ vol: baseline={bl.vol_baseline:.0f} std={bl.vol_std:.0f}")


def test_baseline_rsi_range():
    """rsi_mean ∈ [0, 100], rsi_std ≥ 1"""
    from modules.dynamic_baseline import compute
    k  = _make_k(30, trend=0.002)
    bl = compute(k)
    assert 0 <= bl.rsi_mean <= 100
    assert bl.rsi_std >= 1.0   # 至少1，防除零
    print(f"✅ RSI: mean={bl.rsi_mean:.1f} std={bl.rsi_std:.1f}")


def test_baseline_strong_threshold_minimum():
    """strong_threshold ≥ 3%（最低下限）"""
    from modules.dynamic_baseline import compute
    # 极端平稳行情
    k = [[i*3600, "100", "100.001", "99.999", "100", "1000"] for i in range(25)]
    bl = compute(k)
    assert bl.strong_threshold >= 0.03, \
        f"strong_threshold 最低 3%，得 {bl.strong_threshold:.2%}"
    print(f"✅ strong_threshold: {bl.strong_threshold:.2%}")


def test_baseline_meme_vs_btc_different():
    """MEME 币（高波动）和 BTC（低波动）的基准应显著不同"""
    from modules.dynamic_baseline import compute
    k_btc  = _make_k(30, base=65000.0, trend=0.001, vol=1e9)
    k_meme = _make_k_volatile(30, base=0.001)
    bl_btc  = compute(k_btc)
    bl_meme = compute(k_meme)
    # MEME 的 atr/price 比 BTC 高
    atr_pct_btc  = bl_btc.atr_baseline  / bl_btc.high_20
    atr_pct_meme = bl_meme.atr_baseline / bl_meme.high_20
    assert atr_pct_meme > atr_pct_btc * 2, \
        f"MEME ATR% 应远高于 BTC: meme={atr_pct_meme:.2%} btc={atr_pct_btc:.2%}"
    print(f"✅ 差异化: BTC ATR%={atr_pct_btc:.3%}  MEME ATR%={atr_pct_meme:.3%}")


def test_baseline_insufficient_data():
    """不足5根 K 线也不崩溃"""
    from modules.dynamic_baseline import compute
    k  = _make_k(3)
    bl = compute(k)
    assert bl.atr_baseline > 0
    assert bl.price_range > 0
    print(f"✅ 3根K线不崩溃: atr={bl.atr_baseline:.4f}")


def test_baseline_empty_data():
    """空数据返回安全默认值"""
    from modules.dynamic_baseline import compute
    bl = compute([])
    assert bl.atr_baseline > 0
    assert bl.price_range > 0
    print("✅ 空数据返回安全默认值")


def test_position_pct():
    """position_pct: 最低点→0, 中间→0.5, 最高点→1"""
    from modules.dynamic_baseline import compute, position_pct
    k  = _make_k(20, base=100.0, trend=0.01)
    bl = compute(k)

    pct_low  = position_pct(bl.low_20,  bl)
    pct_high = position_pct(bl.high_20, bl)
    pct_mid  = position_pct((bl.high_20 + bl.low_20) / 2, bl)

    assert pct_low  == 0.0, f"最低点应=0，得 {pct_low}"
    assert pct_high == 1.0, f"最高点应=1，得 {pct_high}"
    assert 0.4 <= pct_mid <= 0.6, f"中间应~0.5，得 {pct_mid:.2f}"
    print(f"✅ position_pct: low={pct_low} mid={pct_mid:.2f} high={pct_high}")


def test_position_pct_clamps():
    """超出范围的价格被截断到 [0, 1]"""
    from modules.dynamic_baseline import compute, position_pct
    k  = _make_k(20)
    bl = compute(k)
    assert position_pct(0.0,   bl) == 0.0
    assert position_pct(1e9,   bl) == 1.0
    print("✅ position_pct 边界截断正常")


def test_is_strong_track_gain():
    """单日涨幅超过 strong_threshold → 强势赛道"""
    from modules.dynamic_baseline import compute, is_strong_track
    k  = _make_k(25, trend=0.001)
    bl = compute(k)
    price_now  = bl.high_20 * (1 + bl.strong_threshold + 0.01)
    price_prev = bl.high_20
    assert is_strong_track(price_now, price_prev, bl, bl.vol_baseline)
    print(f"✅ 涨幅触发强势赛道: threshold={bl.strong_threshold:.2%}")


def test_is_strong_track_vol():
    """成交量 > vol_baseline + 2σ → 强势赛道"""
    from modules.dynamic_baseline import compute, is_strong_track
    k  = _make_k(25)
    bl = compute(k)
    big_vol = bl.vol_baseline + 3 * bl.vol_std + 1
    assert is_strong_track(bl.high_20, bl.high_20 * 0.99, bl, big_vol)
    print(f"✅ 放量触发强势赛道: big_vol={big_vol:.0f} baseline={bl.vol_baseline:.0f}")


def test_is_strong_track_negative():
    """正常行情不触发强势赛道"""
    from modules.dynamic_baseline import compute, is_strong_track
    k  = _make_k(25, trend=0.001)
    bl = compute(k)
    assert not is_strong_track(bl.high_20, bl.high_20 * 0.999,
                                bl, bl.vol_baseline * 0.8)
    print("✅ 正常行情不触发强势赛道")


if __name__ == "__main__":
    tests = [
        test_baseline_fields_present,
        test_baseline_price_range,
        test_baseline_atr_ordering,
        test_baseline_vol_stats,
        test_baseline_rsi_range,
        test_baseline_strong_threshold_minimum,
        test_baseline_meme_vs_btc_different,
        test_baseline_insufficient_data,
        test_baseline_empty_data,
        test_position_pct,
        test_position_pct_clamps,
        test_is_strong_track_gain,
        test_is_strong_track_vol,
        test_is_strong_track_negative,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t(); passed += 1
        except Exception as e:
            import traceback
            print(f"❌ {t.__name__}: {e}"); traceback.print_exc(); failed += 1
    print(f"\n{'='*50}\n{passed} passed / {failed} failed")
    if failed: import sys as _s; _s.exit(1)
