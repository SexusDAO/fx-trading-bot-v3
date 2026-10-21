"""reversal 触发区间改用 ATR 维度 + 存活检查反向阈值 1ATR。
运行：cd fx && BINANCE_API_KEY=x BINANCE_API_SECRET=y python3 -m pytest -q tests/test_reversal_entry_range.py
"""
import os, sys, time
from pathlib import Path

os.environ.setdefault("BINANCE_API_KEY", "x")
os.environ.setdefault("BINANCE_API_SECRET", "y")
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from shared import signal_watcher as sw

ATR = 10.0
ENTRY = 1000.0


async def _noop(*a, **k):
    return None


def _sig(direction, entry=ENTRY, atr=ATR, mode="reversal"):
    return {"symbol": "XUSDT", "mode": mode, "direction": direction,
            "pre_entry_price": entry, "ind": {"atr_1h": atr}}


def _entry(direction, trigger_price=ENTRY, atr_at_trigger=ATR):
    low, high, ep = sw.compute_entry_range(_sig(direction), mark_price=ENTRY)
    return sw.WatchEntry(signal=_sig(direction), entry_low=low, entry_high=high, entry_price=ep,
                         expire_ts=time.time() + 600, on_trigger=_noop,
                         trigger_price=trigger_price, atr_at_trigger=atr_at_trigger)


# ── compute_entry_range ──────────────────────────────────────────────────────
@pytest.mark.parametrize("direction", ["SHORT", "LONG"])
def test_reversal_range_is_half_atr(direction):
    low, high, ep = sw.compute_entry_range(_sig(direction), mark_price=ENTRY)
    assert ep == ENTRY
    assert low == pytest.approx(ENTRY - 0.5 * ATR)
    assert high == pytest.approx(ENTRY + 0.5 * ATR)


def test_reversal_range_fallback_when_atr_missing():
    s = _sig("SHORT"); s["ind"] = {}
    low, high, _ = sw.compute_entry_range(s, mark_price=2000.0)   # atr = mark*0.02 = 40 → 半宽 20
    assert low == pytest.approx(ENTRY - 20.0)
    assert high == pytest.approx(ENTRY + 20.0)


def test_pullback_range_unchanged_pct():
    low, high, ep = sw.compute_entry_range(_sig("LONG", mode="pullback"), mark_price=ENTRY)
    assert low == pytest.approx(ENTRY * (1 - sw._BUF_PULLBACK))
    assert high == pytest.approx(ENTRY * (1 + sw._BUF_PULLBACK))


def test_reversal_uses_config_atr_multiplier(monkeypatch):
    import config
    monkeypatch.setattr(config, "SIGNAL_WATCH_ENTRY_BUFFER_ATR_REVERSAL", 1.0, raising=False)
    low, high, _ = sw.compute_entry_range(_sig("SHORT"), mark_price=ENTRY)
    assert (low, high) == (pytest.approx(ENTRY - ATR), pytest.approx(ENTRY + ATR))


# ── 注册后不秒触发 / 进入 ±0.5ATR 才触发 ─────────────────────────────────────
def test_short_mark_2atr_below_entry_not_instant_trigger():
    e = _entry("SHORT")
    assert not e.price_in_range(ENTRY - 2 * ATR)


def test_long_mark_2atr_above_entry_not_instant_trigger():
    e = _entry("LONG")
    assert not e.price_in_range(ENTRY + 2 * ATR)


@pytest.mark.parametrize("direction", ["SHORT", "LONG"])
def test_trigger_only_inside_half_atr(direction):
    e = _entry(direction)
    assert not e.price_in_range(ENTRY - 0.5 * ATR - 0.01)
    assert e.price_in_range(ENTRY - 0.5 * ATR + 0.01)
    assert e.price_in_range(ENTRY)
    assert e.price_in_range(ENTRY + 0.5 * ATR - 0.01)
    assert not e.price_in_range(ENTRY + 0.5 * ATR + 0.01)


def test_old_pct_range_would_have_instant_triggered():
    """回归：旧 ±3% 区间会让 entry 下方 2ATR(=2%) 的 mark 秒触发，新区间不会。"""
    mark = ENTRY - 2 * ATR
    assert ENTRY * (1 - 0.03) <= mark <= ENTRY * (1 + 0.03)
    assert not _entry("SHORT").price_in_range(mark)


# ── _survival_check 反向阈值 1ATR ────────────────────────────────────────────
def _surv(entry, price, rsi):
    return sw.SignalWatcher._survival_check(entry, price, rsi, ATR)


def test_short_reverse_0_9atr_keeps_order():
    cancel, _ = _surv(_entry("SHORT"), ENTRY - 0.9 * ATR, rsi=70)
    assert cancel is False


def test_short_reverse_1_1atr_cancels():
    cancel, reason = _surv(_entry("SHORT"), ENTRY - 1.1 * ATR, rsi=70)
    assert cancel is True and "1ATR" in reason and "0.5ATR" not in reason


def test_long_reverse_0_9atr_keeps_order():
    cancel, _ = _surv(_entry("LONG"), ENTRY + 0.9 * ATR, rsi=30)
    assert cancel is False


def test_long_reverse_1_1atr_cancels():
    cancel, reason = _surv(_entry("LONG"), ENTRY + 1.1 * ATR, rsi=30)
    assert cancel is True and "1ATR" in reason and "0.5ATR" not in reason


def test_survival_uses_trigger_atr_not_current():
    e = _entry("SHORT", atr_at_trigger=20.0)       # 触发时 ATR=20 → 阈值 ENTRY-20
    assert _surv(e, ENTRY - 15, rsi=70)[0] is False
    assert _surv(e, ENTRY - 21, rsi=70)[0] is True
