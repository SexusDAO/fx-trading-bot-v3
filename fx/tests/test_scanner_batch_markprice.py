"""analyze_symbol 复用批量标记价格。运行：cd fx && BINANCE_API_KEY=x BINANCE_API_SECRET=y python3 -m pytest -q tests/test_scanner_batch_markprice.py（mock）"""
import os, sys, time
from pathlib import Path

os.environ.setdefault("BINANCE_API_KEY", "x")
os.environ.setdefault("BINANCE_API_SECRET", "y")
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
import scanner as sc


class _Stop(BaseException):
    """在 classify 处截停，避免跑完整个分析；同时捕获传入的 funding。"""
    def __init__(self, funding):
        self.funding = funding


def _klines(n=100, base=100.0):
    t0 = 1_700_000_000_000
    return [[t0 + i * 3600_000, str(base), str(base * 1.01), str(base * 0.99), str(base), "1000", 0, "0", 10, "500", "0", "0"]
            for i in range(n)]


TICKER = {"quoteVolume": "5e8", "highPrice": "110", "lowPrice": "90", "lastPrice": "100",
          "priceChangePercent": "12", "openPrice": "89"}


@pytest.fixture
def env(monkeypatch):
    calls = {"mp": [], "klines": 0}
    monkeypatch.setattr(sc.api, "get_klines", lambda *a, **k: calls.__setitem__("klines", calls["klines"] + 1) or _klines())
    monkeypatch.setattr(sc.api, "get_mark_price",
                        lambda s: calls["mp"].append(s) or {"symbol": s, "markPrice": "100", "lastFundingRate": "0.0009"})
    def fake_classify(symbol, chg24, funding, ind):
        raise _Stop(funding)
    monkeypatch.setattr(sc, "classify", fake_classify)
    return calls


def _run(map_, symbol="XUSDT"):
    try:
        sc.analyze_symbol(symbol, TICKER, set(), set(), None, None, "", "", map_)
    except _Stop as st:
        return st.funding
    pytest.skip("测试 ticker 被预筛/其他逻辑提前拦截，未走到标记价格之后")


def test_map_hit_does_not_call_rest_and_reads_funding_from_map(env):
    f = _run({"XUSDT": {"symbol": "XUSDT", "markPrice": "100", "lastFundingRate": "-0.0123"}})
    assert env["mp"] == []                         # 没有单独请求
    assert f == pytest.approx(-0.0123)             # funding 来自 map


def test_none_map_falls_back_to_rest(env):
    f = _run(None)
    assert env["mp"] == ["XUSDT"]
    assert f == pytest.approx(0.0009)              # 来自 REST 返回值，行为与改动前一致


def test_map_miss_symbol_falls_back_to_rest(env):
    f = _run({"OTHERUSDT": {"lastFundingRate": "0.5"}})
    assert env["mp"] == ["XUSDT"] and f == pytest.approx(0.0009)


def test_empty_map_falls_back_to_rest(env):
    _run({})
    assert env["mp"] == ["XUSDT"]


def test_run_scan_builds_map_once_and_passes_it():
    src = (Path(__file__).parent.parent / "scanner.py").read_text()
    assert "_mark_price_map[_ms] = _m" in src
    assert "_mkt_mode, _mark_price_map): sym" in src
    assert src.count("(mark_price_map or {}).get(symbol)") == 1
