"""信号漏斗统计测试：全部 mock，不联网。"""
import json

import pytest

import scanner as sc


@pytest.mark.parametrize("reason,cat", [
    ("vol_ratio=0.3 < 0.5", "量能不足"),
    ("量比不足", "量能不足"),
    ("4H EMA20/EMA50 未多头排列", "EMA结构"),
    ("ATR 过低", "波动率"),
    ("波动不足", "波动率"),
    ("4H RSI=60.0 不在健康区间", "RSI条件"),
    ("OI 背离不成立", "OI条件"),
    ("形态未确认", "形态条件"),
    ("15M K线无反转", "形态条件"),
    ("chg24=10.0% > 8.0%", "其他"),
    (None, "其他"),
])
def test_classify(reason, cat):
    assert sc._funnel_classify(reason) == cat


def test_classify_first_match_wins():
    assert sc._funnel_classify("vol_ratio 低且 EMA 不满足") == "量能不足"


@pytest.fixture
def log_file(tmp_path, monkeypatch):
    p = tmp_path / "logs" / "funnel_log.jsonl"
    monkeypatch.setattr(sc, "_FUNNEL_LOG_FILE", p)
    return p


def test_one_round_writes_line(log_file, monkeypatch):
    import config
    monkeypatch.setattr(config, "TRIGGER_DETECTOR_ENABLED", False, raising=False)
    sc._funnel_begin()
    sc._apply_post_events(None, [
        {"kind": "analytics", "kwargs": {"judge_passed": True}},
        {"kind": "analytics", "kwargs": {"judge_passed": False, "reject_reason": "量比过低"}},
        {"kind": "analytics", "kwargs": {"judge_passed": False, "reject_reason": "chg24 过大"}},
    ])
    rec = sc._funnel_finish({"scanned": 40, "signals_final": 2})
    lines = log_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    d = json.loads(lines[0])
    assert d == rec
    assert set(d) == {"ts", "scan_round", "trigger_enabled", "triggered", "triggered_note",
                      "粗筛通过", "judge通过", "信号产出", "拒绝分布",
                      "coarse_by_route", "reject_by_route", "pass_by_route"}
    assert d["粗筛通过"] == 40 and d["judge通过"] == 1 and d["信号产出"] == 2
    assert d["拒绝分布"]["量能不足"] == 1 and d["拒绝分布"]["其他"] == 1
    assert set(d["拒绝分布"]) == set(sc._FUNNEL_CATS)


def test_trigger_disabled_note(log_file, monkeypatch):
    import config
    monkeypatch.setattr(config, "TRIGGER_DETECTOR_ENABLED", False, raising=False)
    sc._funnel_begin()
    rec = sc._funnel_finish({"scanned": 1, "signals_final": 0})
    assert rec["trigger_enabled"] is False and rec["triggered"] == 0
    assert "未启用" in rec["triggered_note"]


def test_trigger_enabled_delta(log_file, monkeypatch):
    import config
    monkeypatch.setattr(config, "TRIGGER_DETECTOR_ENABLED", True, raising=False)
    q = [5]
    monkeypatch.setattr(sc, "_funnel_queued", lambda: q[0])
    sc._funnel_begin()
    q[0] = 9
    rec = sc._funnel_finish({"scanned": 1, "signals_final": 0})
    assert rec["triggered"] == 4 and rec["triggered_note"] == ""


def test_round_increments(log_file):
    sc._funnel_begin(); a = sc._funnel_finish({})["scan_round"]
    sc._funnel_begin(); b = sc._funnel_finish({})["scan_round"]
    assert b == a + 1


def test_write_failure_only_warns(monkeypatch, tmp_path, caplog):
    bad = tmp_path / "f"
    bad.write_text("x")
    monkeypatch.setattr(sc, "_FUNNEL_LOG_FILE", bad / "sub" / "funnel_log.jsonl")
    sc._funnel_begin()
    with caplog.at_level("WARNING"):
        assert sc._funnel_finish({"scanned": 1}) is None
    assert "漏斗统计写入失败" in caplog.text


# ═══ 补充：分场景统计 ═══
def test_coarse_and_reject_by_route(log_file):
    sc._funnel_begin()
    sc._apply_post_events(None, [
        {"kind": "funnel_coarse", "routes": ["stop_hunt", "pullback"]},
        {"kind": "funnel_coarse", "routes": ["reversal", "pullback"]},
        {"kind": "analytics", "kwargs": {"mode": "pullback", "judge_passed": False, "reject_reason": "vol_ratio 低"}},
        {"kind": "analytics", "kwargs": {"mode": "pullback", "judge_passed": False, "reject_reason": "ATR 过低"}},
        {"kind": "analytics", "kwargs": {"mode": "reversal", "judge_passed": False, "reject_reason": "RSI 未到位"}},
        {"kind": "analytics_pass", "kwargs": {"mode": "pullback", "judge_passed": True}},
    ])
    rec = sc._funnel_finish({"scanned": 2, "signals_final": 1})
    assert rec["coarse_by_route"]["pullback"] == 2 and rec["coarse_by_route"]["stop_hunt"] == 1
    assert rec["coarse_by_route"]["reversal"] == 1 and rec["coarse_by_route"]["ema_trend"] == 0
    assert set(rec["coarse_by_route"]) == set(sc._FUNNEL_ROUTES)
    assert rec["reject_by_route"] == {"pullback": {"量能不足": 1, "波动率": 1}, "reversal": {"RSI条件": 1}}
    assert rec["pass_by_route"] == {"pullback": 1}
    assert rec["judge通过"] == 1                                   # analytics_pass 事件计入通过


def test_route_counters_reset_each_round(log_file):
    sc._funnel_begin()
    sc._apply_post_events(None, [{"kind": "funnel_coarse", "routes": ["pullback"]}])
    sc._funnel_finish({})
    sc._funnel_begin()
    rec = sc._funnel_finish({})
    assert rec["coarse_by_route"]["pullback"] == 0 and rec["reject_by_route"] == {}


def test_analyze_emits_coarse_event():
    import inspect
    src = inspect.getsource(sc._analyze_symbol_core) if hasattr(sc, "_analyze_symbol_core") else \
        open(sc.__file__, encoding="utf-8").read()
    assert 'events.append({"kind": "funnel_coarse", "routes": list(_candidate_routes)})' in src
