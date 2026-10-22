"""_order_event_loop / _补挂保护单 不得在事件循环线程里直接做同步磁盘/网络调用。
- 静态检查：函数体内不再有对 get_all_positions / update_position 等的直接调用（只能作为参数交给 run_in_executor）
- 运行检查：这些函数确实在非事件循环线程里执行
运行：cd fx && python3 -m pytest tests/test_order_event_loop_executor.py
"""
import ast
import asyncio
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import position_manager as pm

_SRC = Path(pm.__file__).read_text(encoding="utf-8")


def _direct_calls(func_name, names):
    tree = ast.parse(_SRC)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == func_name)
    bad = []
    for n in ast.walk(fn):
        if isinstance(n, ast.Call):
            f = n.func
            nm = f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else None)
            if nm in names:
                bad.append((nm, n.lineno))
    return bad


def test_order_event_loop_has_no_direct_sync_state_calls():
    assert _direct_calls("_order_event_loop", {"get_all_positions", "update_position"}) == []


def test_buhua_baohudan_has_no_direct_sync_calls():
    names = {"update_position", "_remove_position_checked", "_gap", "_gmm_pm", "_rm_pos",
             "_get_precision", "get_klines"}
    assert _direct_calls("_补挂保护单", names) == []


class _FakeWS:
    def __init__(self, event=None):
        self.event = event

    async def wait_any_order_event(self, ids, timeout=5.0):
        return self.event


def test_get_all_positions_runs_in_executor_thread(monkeypatch):
    main_tid = threading.get_ident()   # asyncio.run 的事件循环就跑在当前线程
    seen = []
    mgr = pm.PositionManager(ws_listener=_FakeWS())
    mgr._running = True

    def fake_gap():
        seen.append(threading.get_ident())
        mgr._running = False
        return []
    monkeypatch.setattr(pm, "get_all_positions", fake_gap)
    asyncio.run(mgr._order_event_loop())
    assert seen and all(t != main_tid for t in seen)


def _run_pending_fill(monkeypatch, guard_raises):
    main_tid = threading.get_ident()
    calls = []
    mgr = pm.PositionManager(ws_listener=_FakeWS({"X": "FILLED", "ap": "10", "z": "1", "i": 123}))
    mgr._running = True
    pos = {"symbol": "XUSDT", "status": "pending", "open_order_id": 123, "pos_id": "p1"}

    def fake_gap():
        mgr._running = False
        return [pos]

    def fake_update(pid, upd):
        calls.append((upd.get("status"), threading.get_ident()))
        return True

    async def fake_guard(*a, **k):
        if guard_raises:
            raise RuntimeError("boom")

    async def fake_notify(*a, **k):
        return None
    monkeypatch.setattr(pm, "get_all_positions", fake_gap)
    monkeypatch.setattr(pm, "update_position", fake_update)
    monkeypatch.setattr(pm, "mark_system_order_update", lambda s: None)
    monkeypatch.setattr(pm, "_notify_async", fake_notify)
    monkeypatch.setattr(pm, "_补挂保护单", fake_guard)
    asyncio.run(mgr._order_event_loop())
    return main_tid, calls


def test_update_position_patching_runs_in_executor_thread(monkeypatch):
    main_tid, calls = _run_pending_fill(monkeypatch, guard_raises=False)
    assert [c[0] for c in calls] == ["patching"]
    assert all(t != main_tid for _, t in calls)


def test_update_position_rollback_runs_in_executor_thread(monkeypatch):
    main_tid, calls = _run_pending_fill(monkeypatch, guard_raises=True)
    assert [c[0] for c in calls] == ["patching", "pending"]
    assert all(t != main_tid for _, t in calls)


def test_buhua_already_closed_branch_runs_in_executor_thread(monkeypatch):
    import shared.state as st
    from shared import binance_api as api
    main_tid = threading.get_ident()
    seen = []
    monkeypatch.setattr(st, "update_position", lambda pid, u: (seen.append(("update", threading.get_ident())) or True))
    monkeypatch.setattr(pm, "_remove_position_checked",
                        lambda pid, symbol="": (seen.append(("remove", threading.get_ident())) or True))
    monkeypatch.setattr(api, "get_position", lambda s: [])
    asyncio.run(getattr(pm, "_补挂保护单")({"direction": "LONG"}, 10.0, 1.0, "pid_x", "XUSDT"))
    assert [k for k, _ in seen] == ["update", "remove"]
    assert all(t != main_tid for _, t in seen)
