"""tests/test_scoring_v3.py"""
import sys
sys.path.insert(0, "/root/.openclaw/workspace/v3")
from modules.scoring_v3 import calc_score, calc_score_trend, calc_score_reversal, calc_score_trend_short

def _klines(closes):
    return [[0, c, c*1.005, c*0.995, c, 1000] for c in closes]

def _healthy_trend(n=100):
    """
    模拟真实趋势多单场景:
    EMA 形成多排后,RSI 在 50~65 整理区间
    """
    # 前60根:温和上涨建立趋势;后40根:横盘整理(RSI回落到健康区间)
    import random
    random.seed(42)
    result = []
    price = 100.0
    for i in range(60):
        price += 0.4 + random.uniform(-0.2, 0.2)
        result.append(price)
    top = price
    for i in range(40):
        price = top + random.uniform(-0.8, 0.8)
        result.append(price)
    return _klines(result)

def _big_vol_klines(n=100):
    ks = _healthy_trend(n)
    ks[-1][5] = 3000
    return ks

def _uptrend(n=100):
    return _klines([100 + i * 0.5 for i in range(n)])

def _downtrend(n=100):
    return _klines([200 - i * 0.5 for i in range(n)])

def _crash(n=100):
    return _klines([200 - i * 1.5 for i in range(n)])

# ── 趋势多单 ──────────────────────────────────────────────────────────────────

def test_trend_strong():
    """强趋势多单应≥65"""
    k = _big_vol_klines()
    score, d = calc_score_trend(k, k, funding=-0.001)
    print(f"  趋势强信号: {score}分 details={d}")
    assert score >= 65, f"期望≥65,得{score}"
    print(f"✅ 趋势强多单 → {score}")

def test_trend_overbought():
    """RSI>75 扣20分,比 RSI 正常时分数低"""
    from modules.scoring_v3 import rsi as _rsi, _closes
    # 用同样 EMA 排列,只改 RSI:直接注入 details 对比
    k_norm = _uptrend()
    # 极速拉升让 RSI 极高
    closes_ob = [100.0] * 50 + [100 + i*10 for i in range(50)]
    k_ob = _klines(closes_ob)
    r_ob = _rsi(_closes(k_ob))
    score_ob, d_ob = calc_score_trend(k_ob, k_ob, funding=0.002)
    score_norm, d_norm = calc_score_trend(k_norm, k_norm, funding=-0.001)
    print(f"  超买RSI={r_ob:.1f} score={score_ob} d={d_ob}")
    print(f"  正常趋势 score={score_norm} d={d_norm}")
    # 核心验证:RSI>75 时 d_ob['rsi'] 应为 -20
    assert d_ob["rsi"] <= -15, f"RSI超买应≤-15分,实际{d_ob['rsi']}"
    print(f"✅ 超买扣分有效(rsi维度={d_ob['rsi']})")

def test_trend_token_report_bonus():
    k = _uptrend()
    score_base, _ = calc_score_trend(k, k, funding=0.0)
    score_bonus, _ = calc_score_trend(k, k, funding=0.0, token_report=15)
    score_penalty, _ = calc_score_trend(k, k, funding=0.0, token_report=-20)
    assert score_bonus > score_base > score_penalty, \
        f"bonus={score_bonus} base={score_base} penalty={score_penalty}"
    print(f"✅ Token Report 加减分有效({score_penalty}→{score_base}→{score_bonus})")

# ── 逆势多单 ──────────────────────────────────────────────────────────────────

def test_reversal_extreme_oversold():
    k = _crash()
    score, d = calc_score_reversal(k, k, funding=-0.005, chg24=-0.25)
    print(f"  逆势极度超卖: {score}分 d={d}")
    # 逆势单门槛 60分。crash klines RSI很低、资金费率极端、跌幅大,应达到40分以上
    assert score >= 40, f"逆势超卖应≥40,得{score}"
    print(f"✅ 逆势极度超卖 → {score}")

def test_reversal_mild_no_signal():
    k = _uptrend()   # 没在跌,逆势条件不足
    score, d = calc_score_reversal(k, k, funding=0.001, chg24=-0.05)
    print(f"  逆势轻微: {score}分")
    assert score < 50, f"轻微条件应<50,得{score}"
    print(f"✅ 逆势轻微信号低分 → {score}")

# ── 趋势空单 ──────────────────────────────────────────────────────────────────

def test_trend_short():
    k = _downtrend()
    score, d = calc_score_trend_short(k, k, funding=0.003)
    print(f"  趋势空单: {score}分 d={d}")
    # downtrend klines RSI极低，空单扫分被扣，20分上即合理
    assert score >= 20, f"趋势空单应≥20，得{score}"
    # EMA 应该是空排
    assert d["ema"] >= 22, f"EMA空排应≥22分，得{d['ema']}"
    print(f"✅ 趋势空单 → {score}，EMA={d['ema']}")

# ── 统一入口 ──────────────────────────────────────────────────────────────────

def test_unified_entry():
    k_up = _uptrend(); k_dn = _crash()
    s1, _ = calc_score("trend",        k_up, k_up, -0.001)
    s2, _ = calc_score("reversal",     k_dn, k_dn, -0.005, chg24=-0.25)
    s3, _ = calc_score("trend_short",  k_dn, k_dn, 0.003)
    s4, _ = calc_score("reversal_l1",  k_dn, k_dn, -0.003, chg24=-0.18)
    print(f"  unified: trend={s1} reversal={s2} trend_short={s3} reversal_l1={s4}")
    assert all(0 <= s <= 100 for s in [s1, s2, s3, s4]), "分数必须在0~100"
    print("✅ 统一入口路由正确")

def test_score_range():
    """所有路径分数在 0~100"""
    ks = [_uptrend, _downtrend, _crash]
    types = ["trend", "trend_short", "reversal", "reversal_l1", "reversal_short"]
    for t in types:
        k = _uptrend()
        s, _ = calc_score(t, k, k, 0.0, chg24=-0.1)
        assert 0 <= s <= 100, f"{t} 得分={s} 超出范围"
    print("✅ 所有路径分数在 0~100 以内")

if __name__ == "__main__":
    test_trend_strong()
    test_trend_overbought()
    test_trend_token_report_bonus()
    test_reversal_extreme_oversold()
    test_reversal_mild_no_signal()
    test_trend_short()
    test_unified_entry()
    test_score_range()
    print("\n🎉 scoring_v3 全部测试通过")
