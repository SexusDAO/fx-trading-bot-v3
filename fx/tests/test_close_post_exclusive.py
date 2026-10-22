"""remove_position_exclusive 三态返回 + _close_position_post_static 的并发平仓互斥。
- True 删到 / None 找不到 / False 文件损坏
- 只有真删到（或文件异常记账继续）的那一路才记盈亏；None 的一路直接跳过
运行：cd fx && python3 -m pytest tests/test_close_post_exclusive.py
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import position_manager as pm
from shared import state as st


def _patch_store(monkeypatch, data):
    store = {"d": list(data)}
    monkeypatch.setattr(st, "_load_pos", lambda: list(store["d"]))
    monkeypatch.setattr(st, "_save_pos", lambda d: store.__setitem__("d", list(d)))
    monkeypatch.setattr(st, "_POS_STATE_CORRUPTED", False)
    return store


def test_exclusive_true_then_none(monkeypatch):
    store = _patch_store(monkeypatch, [{"pos_id": "a"}, {"pos_id": "b"}])
    assert st.remove_position_exclusive("a") is True
    assert store["d"] == [{"pos_id": "b"}]
    assert st.remove_position_exclusive("a") is None       # 已被删：找不到
    assert store["d"] == [{"pos_id": "b"}]


def test_exclusive_corrupted_returns_false_and_keeps_data(monkeypatch):
    store = _patch_store(monkeypatch, [{"pos_id": "a"}])
    monkeypatch.setattr(st, "_POS_STATE_CORRUPTED", True)
    assert st.remove_position_exclusive("a") is False
    assert store["d"] == [{"pos_id": "a"}]


def test_remove_position_semantics_unchanged(monkeypatch):
    store = _patch_store(monkeypatch, [{"pos_id": "a"}])
    assert st.remove_position("zzz") is True               # 不存在仍算成功（原语义）
    assert st.remove_position("a") is True
    assert store["d"] == []
    monkeypatch.setattr(st, "_POS_STATE_CORRUPTED", True)
    assert st.remove_position("a") is False


class _Stop(BaseException):
    """_record_pnl 的替身抛它，用来证明流程越过了清仓闸门（BaseException 不会被 except Exception 吞掉）。"""


def _setup(monkeypatch, remove_results):
    calls = []
    pos = {"pos_id": "p1", "symbol": "XUSDT", "direction": "long", "entry_price": 10.0,
           "quantity": 1.0, "leverage": 1, "open_time": 0}
    monkeypatch.setattr(pm, "get_all_positions", lambda: [pos])
    monkeypatch.setattr(pm, "set_cooldown", lambda *a, **k: calls.append("cooldown"))
    results = list(remove_results)

    def fake_remove(pid):
        calls.append("remove")
        return results.pop(0)
    monkeypatch.setattr(pm, "remove_position_exclusive", fake_remove)

    def fake_pnl(*a, **k):
        calls.append("pnl")
        raise _Stop()
    monkeypatch.setattr(pm, "_record_pnl", fake_pnl)
    return calls


def _run():
    return asyncio.run(pm._close_position_post_static("p1", "XUSDT", "sl", {}))


def test_close_post_true_continues_after_cooldown_and_remove(monkeypatch):
    calls = _setup(monkeypatch, [True])
    with pytest.raises(_Stop):
        _run()
    assert calls == ["cooldown", "remove", "pnl"]           # 冷却先于清仓，清仓先于记账


def test_close_post_none_skips_accounting(monkeypatch):
    calls = _setup(monkeypatch, [None])
    assert _run() is None
    assert calls == ["cooldown", "remove"]                  # 没有 pnl


def test_close_post_false_continues_accounting(monkeypatch):
    calls = _setup(monkeypatch, [False])
    with pytest.raises(_Stop):
        _run()
    assert calls == ["cooldown", "remove", "pnl"]           # 文件异常：维持原行为，记账继续


def test_close_post_two_paths_account_once(monkeypatch):
    calls = _setup(monkeypatch, [True, None])               # 先到的删到，后到的找不到
    with pytest.raises(_Stop):
        _run()
    assert _run() is None
    assert calls.count("pnl") == 1
