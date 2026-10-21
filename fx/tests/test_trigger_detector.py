"""阶段3：影子触发器（全 mock，不联网）。"""
import json
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import pytest

import shared.trigger_detector as td

H = 3600_000
M15 = 900_000
WEEKDAY = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)  # 周三


def _rows(closes, iv_ms, t0=0, highs=None, lows=None, vols=None):
    out = []
    for i, c in enumerate(closes):
        h = highs[i] if highs else c * 1.01
        l = lows[i] if lows else c * 0.99
        v = vols[i] if vols else 100.0
        out.append([t0 + i * iv_ms, c, h, l, c, v, t0 + (i + 1) * iv_ms - 1])
    return out


class FakeHub:
    def __init__(self):
        self._kl_lock = threading.Lock()
        self.k = {}
        self.mp = {}
        self.tk = {}

    @property
    def _series(self):
        return {key: None for key in self.k}

    def set(self, sym, k1h, k15=None, k4h=None, price=100.0, funding=0.0, chg24=0.0,
            hi24=200.0, lo24=1.0, nft=0):
        self.k[(sym, "1h")] = k1h
        self.k[(sym, "15m")] = k15 or _rows([100.0] * 30, M15)
        self.k[(sym, "4h")] = k4h or _rows([100.0] * 60, 4 * H)
        self.mp[sym] = {"markPrice": price, "lastFundingRate": funding, "nextFundingTime": nft}
        self.tk[sym] = {"priceChangePercent": chg24, "highPrice": hi24, "lowPrice": lo24}

    def get_klines(self, sym, iv, limit):
        r = self.k.get((sym, iv))
        return [list(x) for x in r[-limit:]] if r else None

    def get_mark_price(self, sym):
        return dict(self.mp[sym]) if sym in self.mp else None

    def get_ticker(self, sym):
        return dict(self.tk[sym]) if sym in self.tk else None


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def env(tmp_path, monkeypatch):
    ind = {"rsi": 50.0}
    monkeypatch.setattr(td, "_rsi", lambda c: ind["rsi"])
    hub, clk = FakeHub(), Clock()
    oi = {"v": (0.0, 0.0)}
    det = td.TriggerDetector(hub=hub, clock=clk, oi_reader=lambda s: oi["v"], now_utc=lambda: WEEKDAY,
                             trigger_log=tmp_path / "trigger_log.jsonl", scan_log=tmp_path / "scan_signals.jsonl")
    return det, hub, clk, ind, oi, tmp_path


def _strats(fired, sym="AAAUSDT"):
    return {(r["strategy"], r["direction"]) for r in fired if r["symbol"] == sym}


def _flat1h(n=60, c=100.0):
    return _rows([c] * n, H)


def _advance(hub, sym, iv, ms):
    rows = hub.k[(sym, iv)]
    last = rows[-1]
    rows.append([last[0] + ms] + last[1:6] + [last[6] + ms])
    rows.pop(0)


# ── 8 种策略各一个触发用例 ──────────────────────────────────────────────────
def test_pullback_on_15m_close(env):
    det, hub, clk, *_ = env
    hub.set("AAAUSDT", _flat1h(), price=100.0)
    assert det.poll_once() == []                      # 首轮只初始化
    clk.t += 1
    assert ("pullback", "LONG") not in _strats(det.poll_once())   # 无 15M 收盘
    _advance(hub, "AAAUSDT", "15m", M15)
    clk.t += 1
    assert ("pullback", "LONG") in _strats(det.poll_once())


def test_pullback_zone_is_asymmetric(env):
    det, hub, clk, *_ = env
    # flat: ema20=ema50=100, atr≈2 → LONG 区间 [98, 100.6]；101 在 +0.3ATR 之外
    hub.set("AAAUSDT", _flat1h(), price=101.0)
    det.poll_once()
    _advance(hub, "AAAUSDT", "15m", M15)
    clk.t += 1
    assert "pullback" not in {s for s, _ in _strats(det.poll_once())}


def test_reversal_chg24(env):
    det, hub, clk, *_ = env
    hub.set("AAAUSDT", _flat1h(), chg24=-20.0)
    hub.tk["AAAUSDT"]["priceChangePercent"] = 0.0
    det.poll_once()
    hub.tk["AAAUSDT"]["priceChangePercent"] = -20.0
    clk.t += 1
    assert ("reversal", "LONG") in _strats(det.poll_once())


def test_breakout_up_requires_positive_chg24(env):
    det, hub, clk, *_ = env
    hub.set("AAAUSDT", _flat1h(), price=100.0, chg24=6.0)
    det.poll_once()
    hub.mp["AAAUSDT"]["markPrice"] = 102.0            # >= high_20(101)
    clk.t += 1
    assert ("breakout", "LONG") in _strats(det.poll_once())
    # chg24<0 时向上突破不算
    det2, hub2, clk2 = td.TriggerDetector(hub=FakeHub(), clock=Clock(), oi_reader=lambda s: (0, 0),
                                          trigger_log=det._tlog, scan_log=det._slog), None, None
    h2 = det2._hub
    h2.set("AAAUSDT", _flat1h(), price=100.0, chg24=-6.0)
    det2.poll_once()
    h2.mp["AAAUSDT"]["markPrice"] = 102.0
    det2._clock.t += 1
    assert "breakout" not in {s for s, _ in _strats(det2.poll_once())}


def test_funding_arb_every_5min(env):
    det, hub, clk, *_ = env
    nft = int((clk.t + 3600) * 1000)
    hub.set("AAAUSDT", _flat1h(), funding=0.0, nft=nft)
    det.poll_once()
    hub.mp["AAAUSDT"]["lastFundingRate"] = -0.006
    clk.t += 10
    assert "funding_arb" not in {s for s, _ in _strats(det.poll_once())}   # 未到 5 分钟
    clk.t += 300
    assert ("funding_arb", "LONG") in _strats(det.poll_once())


def test_range_bounce_on_1h_close(env):
    det, hub, clk, *_ = env
    closes = [100.0] * 45 + [100, 104, 108, 112, 116, 122, 121, 118, 115, 113, 112, 111, 112, 111, 111]
    # 高波动以满足 ATR>=2%
    k1h = _rows(closes, H, highs=[c * 1.03 for c in closes], lows=[c * 0.97 for c in closes])
    hub.set("AAAUSDT", k1h, price=113.0)
    det.poll_once()
    _advance(hub, "AAAUSDT", "1h", H)
    clk.t += 1
    assert ("range_bounce", "LONG") in _strats(det.poll_once())


def test_stop_hunt_pierce_low(env):
    det, hub, clk, *_ = env
    c1 = [100.0] * 40 + [105, 104, 103, 102, 95, 101, 102, 103, 104, 105, 104, 105, 104, 105, 104, 105, 104, 105, 104, 105]
    k1h = _rows(c1, H)
    vols = [100.0] * 30
    lows = [99.0] * 30
    hub.set("AAAUSDT", k1h, k15=_rows([100.0] * 30, M15, lows=lows, vols=vols))
    det.poll_once()
    k15 = hub.k[("AAAUSDT", "15m")]
    k15[-3][3] = 94.0      # 刺穿 pivot_low(95) 0.3%+
    k15[-3][5] = 300.0     # 量比 3x
    clk.t += 1
    assert ("stop_hunt", "LONG") in _strats(det.poll_once())


def test_oi_divergence_from_cache(env):
    det, hub, clk, ind, oi, _ = env
    k1h = _rows([100.0] * 60, H)
    hub.set("AAAUSDT", k1h, price=100.0, hi24=101.0, lo24=80.0)
    det.poll_once()
    oi["v"] = (90.0, 100.0)     # 下降 10% + 接近高点 → SHORT
    clk.t += 1
    fired = det.poll_once()
    assert ("oi_divergence", "SHORT") in _strats(fired)
    rec = [r for r in fired if r["strategy"] == "oi_divergence"][0]
    assert "oi_src=cache" in rec["reason"]


def test_ema_trend_on_4h_close(env):
    det, hub, clk, ind, *_ = env
    c4 = [80.0 + i * 0.4 for i in range(60)]           # 多头排列
    hub.set("AAAUSDT", _flat1h(c=c4[-2]), k4h=_rows(c4, 4 * H), price=c4[-2], chg24=1.0)
    ind["rsi"] = 55.0
    det.poll_once()
    _advance(hub, "AAAUSDT", "4h", 4 * H)
    e20 = td._ema([float(k[4]) for k in hub.k[("AAAUSDT", "4h")][:-1]], 20)
    hub.mp["AAAUSDT"]["markPrice"] = e20 * 1.01          # 在 EMA20 的 -2.5%~+3% 内
    clk.t += 1
    assert ("ema_trend", "LONG") in _strats(det.poll_once())


# ── 边沿 / 迟滞 / 冷却 / 首轮 ──────────────────────────────────────────────
def test_edge_no_repeat_while_condition_holds(env):
    det, hub, clk, *_ = env
    hub.set("AAAUSDT", _flat1h(), chg24=0.0)
    det.poll_once()
    hub.tk["AAAUSDT"]["priceChangePercent"] = -20.0
    clk.t += 1
    assert "reversal" in {s for s, _ in _strats(det.poll_once())}
    clk.t += 5000                                       # 冷却已过，但条件持续成立
    assert "reversal" not in {s for s, _ in _strats(det.poll_once())}


def test_rsi_hysteresis(env):
    det, hub, clk, ind, *_ = env
    hub.set("AAAUSDT", _flat1h())
    det.poll_once()

    def step(r):
        ind["rsi"] = r
        clk.t += 1000
        return "reversal" in {s for s, _ in _strats(det.poll_once())}

    assert step(27.0) is True       # <28 触发
    assert step(30.0) is False      # 30 不复位，也不重复触发
    assert step(27.0) is False      # 仍未复位 → 不触发
    assert step(33.0) is False      # >32 复位
    assert step(27.0) is True       # 复位后再次触发


def test_cooldown(env):
    det, hub, clk, ind, *_ = env
    hub.set("AAAUSDT", _flat1h())
    det.poll_once()

    def step(r, dt):
        ind["rsi"] = r
        clk.t += dt
        return "reversal" in {s for s, _ in _strats(det.poll_once())}

    assert step(27.0, 1) is True
    assert step(40.0, 1) is False   # 复位
    assert step(27.0, 100) is False  # 冷却 900s 内
    assert step(40.0, 1) is False
    assert step(27.0, 900) is True   # 冷却结束恢复


def test_first_round_only_initializes(env):
    det, hub, clk, ind, *_ = env
    ind["rsi"] = 20.0
    hub.set("AAAUSDT", _flat1h(), chg24=-30.0, funding=-0.01, nft=int((clk.t + 60) * 1000))
    assert det.poll_once() == []
    assert not det._tlog.exists()
    clk.t += 1
    assert "reversal" not in {s for s, _ in _strats(det.poll_once())}   # 首轮已在状态里，边沿不再触发


def test_symbol_not_covered_by_hub_skipped(env):
    det, hub, clk, *_ = env
    hub.k[("BBBUSDT", "1h")] = _flat1h()        # 有 1h 但无标记价
    hub.k[("CCCUSDT", "1h")] = _flat1h()[:5]    # K 线不足
    hub.mp["CCCUSDT"] = {"markPrice": 100.0}
    det.poll_once()
    clk.t += 1
    assert det.poll_once() == []


# ── 输出 ──────────────────────────────────────────────────────────────────
def test_trigger_log_format_and_size_warning(env, caplog, monkeypatch):
    det, hub, clk, ind, *_ = env
    monkeypatch.setattr(td, "LOG_WARN_BYTES", 10)
    hub.set("AAAUSDT", _flat1h())
    det.poll_once()
    ind["rsi"] = 20.0
    clk.t += 1
    with caplog.at_level("INFO", logger="fx.trigger"):
        det.poll_once()
    rec = json.loads(det._tlog.read_text().splitlines()[0])
    assert set(rec) == {"ts", "symbol", "strategy", "direction", "reason", "price", "hub_snapshot"}
    assert set(rec["hub_snapshot"]) == {"chg24", "rsi_1h", "ema20_1h", "atr_ratio", "funding"}
    assert any("[trigger] AAAUSDT reversal LONG" in m for m in caplog.messages)
    assert any("超过 10MB" in m for m in caplog.messages)


def test_write_scan_signals(tmp_path):
    p = tmp_path / "s.jsonl"
    td.write_scan_signals([], path=p, ts_ms=1)
    td.write_scan_signals([{"symbol": "A", "sig_type": "pullback", "direction": "LONG", "route_quality": 70}], path=p, ts_ms=2)
    lines = [json.loads(x) for x in p.read_text().splitlines()]
    assert lines[0] == {"ts": 1, "signals": []}
    assert lines[1] == {"ts": 2, "symbol": "A", "strategy": "pullback", "direction": "LONG", "route_quality": 70}


# ── 对账 ──────────────────────────────────────────────────────────────────
def test_reconcile_miss_and_fp(env, caplog):
    det, hub, clk, ind, oi, tmp = env
    hub.set("AAAUSDT", _flat1h())
    det.poll_once()
    now_ms = int(clk.t * 1000)
    td.write_scan_signals([
        {"symbol": "ZZZUSDT", "sig_type": "breakout", "direction": "LONG", "route_quality": 60},
        {"symbol": "FFFUSDT", "sig_type": "funding_arb", "direction": "LONG", "route_quality": 50},
    ], path=det._slog, ts_ms=now_ms)
    ind["rsi"] = 20.0                    # AAA 触发 reversal，但扫描里没有 AAA
    clk.t += 1
    with caplog.at_level("INFO", logger="fx.trigger"):
        det.poll_once()
        assert det.stats["miss"] == 1    # ZZZ 记 miss；funding_arb 不参与
        clk.t += 901
        det.poll_once()
    assert det.stats["fp"] == 1
    assert any("[trigger-miss] ZZZUSDT breakout" in m for m in caplog.messages)
    assert any("[trigger-fp] AAAUSDT reversal" in m for m in caplog.messages)
    assert not any("FFFUSDT" in m for m in caplog.messages)


def test_reconcile_symbol_level_match(env):
    det, hub, clk, ind, *_ = env
    hub.set("AAAUSDT", _flat1h())
    det.poll_once()
    ind["rsi"] = 20.0
    clk.t += 1
    det.poll_once()                      # AAA reversal 触发
    td.write_scan_signals([{"symbol": "AAAUSDT", "sig_type": "pullback", "direction": "LONG",
                            "route_quality": 70}], path=det._slog, ts_ms=int(clk.t * 1000))
    clk.t += 1
    det.poll_once()
    clk.t += 2000
    det.poll_once()
    assert det.stats["miss"] == 0 and det.stats["fp"] == 0


def test_disabled_by_default(monkeypatch):
    import config
    assert config.TRIGGER_DETECTOR_ENABLED is False
    td._default = None
    assert td.start() is None
