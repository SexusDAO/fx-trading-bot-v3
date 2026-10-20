"""tests/test_market_filter.py"""
import sys
from datetime import datetime, timezone
sys.path.insert(0, "/root/.openclaw/workspace/v3")
from modules.market_filter import market_filter, BLOCKED, _minutes_from_funding_settlement

def _utc(h, m=0):
    return datetime(2026, 8, 3, h, m, tzinfo=timezone.utc)

# ── 资金费率窗口 ──────────────────────────────────────────────────────────────

def test_funding_window_blocked():
    # UTC 07:52 = 结算前 8 分钟（< 15）→ 阻断
    r = market_filter("LONG", 75, funding_rate=0.001, now=_utc(7, 52))
    assert r == BLOCKED, f"期望BLOCKED，得{r}"
    print("✅ 资金费率结算前阻断")

def test_funding_window_after():
    # UTC 08:03 = 结算后 3 分钟（≤ 5）→ 阻断
    r = market_filter("LONG", 75, funding_rate=0.001, now=_utc(8, 3))
    assert r == BLOCKED, f"期望BLOCKED，得{r}"
    print("✅ 资金费率结算后阻断")

def test_funding_window_clear():
    # UTC 08:10 = 结算后 10 分钟 → 正常（普通费率后5min才解封）
    r = market_filter("LONG", 75, funding_rate=0.001, now=_utc(8, 10))
    assert r != BLOCKED, f"应放行，得{r}"
    print(f"✅ 资金费率窗口外放行 → {r}")

def test_funding_extreme_wider_window():
    # 极端费率：|rate|>0.5%，窗口扩展到前20/后10
    # UTC 07:45 = 结算前 15 分钟，极端费率应阻断
    r = market_filter("LONG", 75, funding_rate=0.006, now=_utc(7, 45))
    assert r == BLOCKED, f"极端费率前15min应阻断，得{r}"
    # UTC 07:43 = 结算前 17 分钟，普通费率应放行（>15min）
    r2 = market_filter("LONG", 75, funding_rate=0.001, now=_utc(7, 43))
    assert r2 != BLOCKED, f"普通费率前17min应放行，得{r2}"
    print("✅ 极端费率窗口更宽")

# ── BTC 趋势调整 ──────────────────────────────────────────────────────────────

def test_btc_bull_long():
    r = market_filter("LONG", 70, btc_trend="bull", now=_utc(10))
    assert r == 75, f"牛市多单+5，期望75，得{r}"
    print(f"✅ 牛市多单 +5 → {r}")

def test_btc_bull_short():
    r = market_filter("SHORT", 70, btc_trend="bull", now=_utc(10))
    assert r == 60, f"牛市空单-10，期望60，得{r}"
    print(f"✅ 牛市空单 -10 → {r}")

def test_btc_bear_short():
    r = market_filter("SHORT", 70, btc_trend="bear", now=_utc(10))
    assert r == 75, f"熊市空单+5，期望75，得{r}"
    print(f"✅ 熊市空单 +5 → {r}")

def test_btc_neutral():
    r = market_filter("LONG", 70, btc_trend="neutral", now=_utc(10))
    assert r == 70, f"中性不调整，期望70，得{r}"
    print(f"✅ BTC 中性 不调整 → {r}")

# ── 时段调整 ─────────────────────────────────────────────────────────────────

def test_session_us_open():
    # UTC 13:20 = 北京时间 21:20 美股开盘 → -10
    r = market_filter("LONG", 75, now=_utc(13, 20))
    assert r == 65, f"美股开盘-10，期望65，得{r}"
    print(f"✅ 美股开盘 -10 → {r}")

def test_session_low_liquidity():
    # UTC 03:00 低流动性 → -10
    r = market_filter("LONG", 75, now=_utc(3))
    assert r == 65, f"低流动性-10，期望65，得{r}"
    print(f"✅ 低流动性 -10 → {r}")

def test_session_normal():
    r = market_filter("LONG", 75, now=_utc(10))
    assert r == 75, f"正常时段不调整，得{r}"
    print(f"✅ 正常时段 无调整 → {r}")

# ── FGI ──────────────────────────────────────────────────────────────────────

def test_fgi_extreme_fear_no_short():
    r = market_filter("SHORT", 70, fgi=15, now=_utc(10))
    assert r == BLOCKED, f"极度恐慌禁空，得{r}"
    print("✅ FGI=15 禁止做空")

def test_fgi_extreme_greed_no_long():
    r = market_filter("LONG", 70, fgi=90, now=_utc(10))
    assert r == BLOCKED, f"极度贪婪禁多，得{r}"
    print("✅ FGI=90 禁止做多")

def test_fgi_normal():
    r = market_filter("LONG", 70, fgi=50, now=_utc(10))
    assert r == 70
    print(f"✅ FGI=50 正常 → {r}")

# ── 叠加效果 ──────────────────────────────────────────────────────────────────

def test_combined_adj():
    # 牛市(+5) + 低流动性(-10) = -5
    r = market_filter("LONG", 70, btc_trend="bull", now=_utc(4))
    assert r == 65, f"期望65，得{r}"
    print(f"✅ 叠加调整 牛市+5 低流动性-10 = {r}")

if __name__ == "__main__":
    test_funding_window_blocked()
    test_funding_window_after()
    test_funding_window_clear()
    test_funding_extreme_wider_window()
    test_btc_bull_long()
    test_btc_bull_short()
    test_btc_bear_short()
    test_btc_neutral()
    test_session_us_open()
    test_session_low_liquidity()
    test_session_normal()
    test_fgi_extreme_fear_no_short()
    test_fgi_extreme_greed_no_long()
    test_fgi_normal()
    test_combined_adj()
    print("\n🎉 market_filter 全部测试通过")
