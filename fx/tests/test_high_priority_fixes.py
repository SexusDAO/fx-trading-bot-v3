"""高优先级 4 处修复测试：全部 mock，不联网。"""
import asyncio
import logging
import time
from unittest.mock import MagicMock

import pytest

import executor
from shared import signal_watcher as sw
from shared import binance_api as api
from shared import state
from shared import btc_guard


def run(c):
    return asyncio.new_event_loop().run_until_complete(c)


@pytest.fixture(autouse=True)
def _iso(monkeypatch):
    btc_guard.get_guard().reset()
    monkeypatch.setattr(api, "get_position", MagicMock(return_value=[]))
    monkeypatch.setattr(api, "get_open_orders", MagicMock(return_value=[]))
    monkeypatch.setattr(state, "is_in_cooldown", lambda s: (False, ""))
    yield


# ── 【修复1】 ─────────────────────────────────────────────────────────────
def _trig(monkeypatch, d, mark, anchor=100.0):
    monkeypatch.setattr(sw, "_get_hub_or_rest", lambda *a, **k: {"markPrice": str(mark)})
    hit = []

    async def on_trig(sig):
        hit.append(1)
    sig = {"symbol": "ETHUSDT", "direction": d, "mode": "pullback", "ind": {"atr_1h": 100.0}}
    e = sw.WatchEntry(signal=sig, entry_low=anchor * 0.99, entry_high=anchor * 1.01, entry_price=anchor,
                      expire_ts=time.time() + 3600, on_trigger=on_trig)
    w = sw.SignalWatcher()
    w._watched["ETHUSDT"] = e
    run(w._trigger("ETHUSDT", mark, e))
    return hit


@pytest.mark.parametrize("d,mark", [("LONG", 99.4), ("SHORT", 100.6)])
def test_anchor_crossed_skips(monkeypatch, caplog, d, mark):
    with caplog.at_level(logging.INFO):
        assert not _trig(monkeypatch, d, mark)
    assert "[trigger-skip-anchor]" in caplog.text


@pytest.mark.parametrize("d,mark", [("LONG", 99.9), ("SHORT", 100.1)])
def test_anchor_within_tolerance_triggers(monkeypatch, d, mark):
    assert _trig(monkeypatch, d, mark)


# ── 【修复2】 ─────────────────────────────────────────────────────────────
def _gate(monkeypatch, notional, oi_contracts, depth_u):
    monkeypatch.setattr(api, "get_mark_price", lambda s: {"markPrice": "1"})
    monkeypatch.setattr(api, "get_order_book", lambda s, limit=20: {"asks": [["1", str(depth_u)]], "bids": []})
    return executor._check_liquidity_gate("XUSDT", "LONG", notional=notional, oi_now=oi_contracts)[0]


def test_threshold_uses_notional_x3(monkeypatch):
    # notional=10000 → 30000；OI×0.05%=5000；旧 OI×0.1%=10000
    assert not _gate(monkeypatch, 10_000, 10_000_000, 29_000)
    assert _gate(monkeypatch, 10_000, 10_000_000, 31_000)


def test_small_notional_uses_oi_floor(monkeypatch):
    # notional=100 → 300；OI×0.05% = 20000×… 用 OI=20_000_000 → 10000
    assert not _gate(monkeypatch, 100, 20_000_000, 9_000)
    assert _gate(monkeypatch, 100, 20_000_000, 10_500)


def test_hard_floor_3000_kept(monkeypatch):
    assert not _gate(monkeypatch, 100, 1_000, 2_900)


def test_signal_notional_sources():
    assert executor._signal_notional({"pre_params": {"notional": 500}}) == 500
    assert executor._signal_notional({"pre_params": {"position_size": 2}, "ind": {"mark_price": 50}}) == 100
    assert executor._signal_notional({}) == 100.0


# ── 【修复3】【修复4】 ────────────────────────────────────────────────────
def _add(monkeypatch, d, depth_u=1e9, cd=(False, "")):
    monkeypatch.setattr(state, "is_in_cooldown", lambda s: cd)
    monkeypatch.setattr(sw, "_get_hub_or_rest", lambda *a, **k: {"markPrice": "1"})
    side = {"asks": [["1", str(depth_u)]], "bids": [["1", str(depth_u)]]}
    monkeypatch.setattr(api, "get_order_book", lambda s, limit=20: side)
    w = sw.SignalWatcher()
    sig = {"symbol": "ETHUSDT", "direction": d, "mode": "pullback", "sig_type": "pullback",
           "ind": {"atr_1h": 1}, "pre_params": {"notional": 1000}}
    run(w.add_signal(sig, 1, 2, 1, time.time() + 3600, MagicMock()))
    return "ETHUSDT" in w._watched


def test_depth_insufficient_not_registered(monkeypatch):
    assert not _add(monkeypatch, "LONG", depth_u=1500)   # < 1000×2
    assert _add(monkeypatch, "LONG", depth_u=2500)


@pytest.mark.parametrize("reason,d,ok", [
    ("crash_guard_short", "LONG", True), ("crash_guard_short", "SHORT", False),
    ("crash_guard", "SHORT", True), ("crash_guard", "LONG", False),
    ("stop_loss_cooldown", "LONG", False), ("stop_loss_cooldown", "SHORT", False),
])
def test_cooldown_precheck(monkeypatch, reason, d, ok):
    assert _add(monkeypatch, d, cd=(True, reason)) is ok
