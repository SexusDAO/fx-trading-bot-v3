"""get_liq_zones_cached fetch_if_missing 测试：全部 mock，不联网。"""
import re
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from shared import liq_cache as lc


@pytest.fixture(autouse=True)
def _iso(monkeypatch):
    with lc._CACHE_LOCK:
        saved = dict(lc._CACHE)
        lc._CACHE.clear()
    fetch = MagicMock(return_value={"LONG": [{"p": 1}], "SHORT": [{"p": 2}], "ts": time.time(), "src_level": 1})
    monkeypatch.setattr(lc, "_fetch_dedup", fetch)
    from shared import binance_api
    mp = MagicMock(return_value={"markPrice": "100"})
    monkeypatch.setattr(binance_api, "get_mark_price", mp)
    yield fetch, mp
    with lc._CACHE_LOCK:
        lc._CACHE.clear()
        lc._CACHE.update(saved)


def test_no_fetch_on_miss_returns_empty(_iso):
    fetch, mp = _iso
    assert lc.get_liq_zones_cached("XUSDT", "LONG", 0.0, range_="3d", fetch_if_missing=False) == []
    fetch.assert_not_called()
    mp.assert_not_called()


def test_no_fetch_on_stale_returns_empty(_iso):
    fetch, _ = _iso
    with lc._CACHE_LOCK:
        lc._CACHE["XUSDT:7d"] = {"LONG": [{"p": 9}], "SHORT": [], "ts": time.time() - lc._CACHE_TTL - 5}
    assert lc.get_liq_zones_cached("XUSDT", "LONG", 100.0, range_="7d", fetch_if_missing=False) == []
    fetch.assert_not_called()


def test_no_fetch_hit_returns_cached(_iso):
    fetch, _ = _iso
    with lc._CACHE_LOCK:
        lc._CACHE["XUSDT:3d"] = {"LONG": [{"p": 7}], "SHORT": [], "ts": time.time()}
    assert lc.get_liq_zones_cached("XUSDT", "LONG", 100.0, fetch_if_missing=False) == [{"p": 7}]
    fetch.assert_not_called()


def test_default_still_fetches_on_miss(_iso):
    fetch, mp = _iso
    assert lc.get_liq_zones_cached("XUSDT", "SHORT", 0.0) == [{"p": 2}]
    fetch.assert_called_once()
    mp.assert_called_once()


def test_fetch_true_explicit_same_as_default(_iso):
    fetch, _ = _iso
    assert lc.get_liq_zones_cached("XUSDT", "LONG", 100.0, fetch_if_missing=True) == [{"p": 1}]
    fetch.assert_called_once()


def test_scanner_call_sites():
    src = (Path(lc.__file__).resolve().parent.parent / "scanner.py").read_text(encoding="utf-8")
    calls = re.findall(r"_liq_zones\(sym,[^)]*\)", src)
    assert len(calls) == 4 and all("fetch_if_missing=False" in c for c in calls)
    pre = src[src.index("def _prefetch_liq"):src.index("def _prefetch_symbol")]
    assert "fetch_if_missing" not in pre          # 分析前预取保持实时拉取
