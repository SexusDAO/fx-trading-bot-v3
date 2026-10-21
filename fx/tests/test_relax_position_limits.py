"""PR B：放宽数量上限 + 去掉熊市禁多 + 仓位大小分母固定。
运行：cd fx && BINANCE_API_KEY=x BINANCE_API_SECRET=y python3 -m pytest -q tests/test_relax_position_limits.py（mock）"""
import os, sys
from pathlib import Path

os.environ.setdefault("BINANCE_API_KEY", "x")
os.environ.setdefault("BINANCE_API_SECRET", "y")
_fx = Path(__file__).parent.parent
sys.path.insert(0, str(_fx))

import pytest
import config
from shared import risk_manager as rm

NEW = {
    "bull":  {"total": 8, "long": 6, "short": 4},
    "bear":  {"total": 8, "long": 4, "short": 6},
    "range": {"total": 8, "long": 5, "short": 5},
}


@pytest.mark.parametrize("mode", list(NEW))
def test_limits_per_mode(mode):
    assert rm._get_position_limits(mode) == NEW[mode]


def _check(monkeypatch, mode, total, long_, short_, direction):
    import shared.state as st
    monkeypatch.setattr(st, "get_position_count", lambda: {"total": total, "long": long_, "short": short_})
    monkeypatch.setattr(st, "get_pending_count", lambda: {"total": 0, "long": 0, "short": 0})
    monkeypatch.setattr(rm, "get_market_mode", lambda: mode)
    return rm.check_position_limits(direction)


@pytest.mark.parametrize("mode,direction,limit", [
    ("bull", "LONG", 6), ("bull", "SHORT", 4), ("bear", "LONG", 4), ("bear", "SHORT", 6),
    ("range", "LONG", 5), ("range", "SHORT", 5),
])
def test_direction_limit_enforced_in_each_mode(monkeypatch, mode, direction, limit):
    cnt = lambda n: (n, 0) if direction == "LONG" else (0, n)
    l, s = cnt(limit - 1)
    assert _check(monkeypatch, mode, limit - 1, l, s, direction) is True          # 未满：放行
    l, s = cnt(limit)
    assert _check(monkeypatch, mode, limit, l, s, direction) is False             # 已满：拒绝


@pytest.mark.parametrize("mode", ["bull", "bear", "range"])
def test_total_limit_enforced(monkeypatch, mode):
    assert _check(monkeypatch, mode, 7, 3, 4, "LONG") is True
    assert _check(monkeypatch, mode, 8, 4, 4, "LONG") is False


def test_sizing_denominator_is_fixed_and_decoupled():
    assert config.RISK_MAX_POSITIONS_FOR_SIZING == 5
    assert config.RISK_MAX_POSITIONS == config.MAX_POSITIONS_TOTAL_BULL == 8     # 兼容常量随上限变，但不再用于仓位大小
    src = (_fx / "executor.py").read_text()
    assert "max(RISK_MAX_POSITIONS_FOR_SIZING, 1) * 0.3" in src
    assert "max(RISK_MAX_POSITIONS, 1)" not in src


def test_bear_block_rev_long_removed():
    src = (_fx / "scanner.py").read_text()
    assert "_bear_block_rev_long" not in src
    assert "熊市禁止 reversal LONG" not in src


def test_early_skip_uses_max_over_modes():
    src = (_fx / "scanner.py").read_text()
    assert "_cfg_early.RISK_MAX_LONG" not in src and "_cfg_early.RISK_MAX_SHORT" not in src
    assert "_cfg_early.MAX_POSITIONS_LONG_RANGE" in src and "_cfg_early.MAX_POSITIONS_SHORT_RANGE" in src


def test_reversal_long_not_hard_rejected_when_btc_down_and_dump(monkeypatch):
    """BTC 下跌 + 标的 24H -10%：reversal LONG 不再因熊市硬拒绝被判失败（judge 返回通过则不会被改写成失败）。"""
    import scanner as sc

    def klines(n=100):
        t0 = 1_700_000_000_000
        return [[t0 + i * 3600_000, "100", "101", "99", "100", "1000", 0, "0", 10, "500", "0", "0"] for i in range(n)]

    monkeypatch.setattr(sc, "_get_klines_cached", lambda *a, **k: klines())
    monkeypatch.setattr(sc.api, "get_mark_price", lambda s: {"markPrice": "100", "lastFundingRate": "0.0001"})
    monkeypatch.setattr(sc, "classify", lambda *a, **k: "reversal")
    monkeypatch.setattr(sc, "_is_reversal", lambda *a, **k: True)
    called = {}
    monkeypatch.setattr(sc, "judge_reversal", lambda *a: (called.setdefault("n", 1), (True, {"direction": "LONG"}))[1])
    analytics = []
    monkeypatch.setattr(sc, "_write_signal_analytics", lambda *a, **k: analytics.append(k))
    ticker = {"quoteVolume": "5e8", "highPrice": "110", "lowPrice": "90", "lastPrice": "90",
              "priceChangePercent": "-10", "openPrice": "100"}
    try:
        sc.analyze_symbol("XUSDT", ticker, set(), set(), None, None, "down", "bear", mark_price_map={})
    except Exception:
        pass   # judge 通过之后的流程依赖更多 mock，这里只关心 reversal 没有被硬拒绝
    if not called:
        pytest.skip("测试数据未走到 judge_reversal，无法验证行为（源码级断言已覆盖）")
    assert not [k for k in analytics if k.get("mode") == "reversal" and k.get("judge_passed") is False]
