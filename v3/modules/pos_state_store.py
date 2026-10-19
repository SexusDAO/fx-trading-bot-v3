"""
pos_state_store.py — pos_state.json 唯一写入者 (P0)

规则：
  - 整个系统只有本模块写 pos_state.json
  - 读取不限制，任何模块可以直接 load_json(POS_STATE_FILE)
  - 所有写操作必须带文件锁，防止并发覆盖
"""

import json
import os
import threading
from pathlib import Path
from datetime import datetime, timezone

WORKSPACE     = Path(__file__).parent.parent
POS_STATE_FILE = WORKSPACE / "config" / "pos_state.json"

_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load() -> dict:
    try:
        with open(POS_STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(data: dict):
    POS_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = POS_STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    tmp.replace(POS_STATE_FILE)   # 原子替换，防止写一半


def get_all() -> dict:
    """读取全部持仓（任何模块均可调用）"""
    return _load()


def get(symbol: str, direction: str) -> dict | None:
    """读取单个持仓，不存在返回 None"""
    return _load().get(f"{symbol}_{direction}")


def write(symbol: str, direction: str, data: dict):
    """
    写入或更新一个持仓。
    data 里无需包含 symbol/direction，函数自动补充 updated_at。
    """
    with _lock:
        state = _load()
        key = f"{symbol}_{direction}"
        entry = state.get(key, {})
        entry.update(data)
        entry["symbol"]     = symbol
        entry["direction"]  = direction
        entry["updated_at"] = _now_iso()
        state[key] = entry
        _save(state)


def remove(symbol: str, direction: str):
    """平仓后移除持仓记录"""
    with _lock:
        state = _load()
        key = f"{symbol}_{direction}"
        if key in state:
            del state[key]
            _save(state)


def clear_all():
    """清空所有持仓（测试/紧急用）"""
    with _lock:
        _save({})


# ── 便捷别名 ──────────────────────────────────────────────────────────────────
store = type("store", (), {
    "get_all": staticmethod(get_all),
    "get":     staticmethod(get),
    "write":   staticmethod(write),
    "remove":  staticmethod(remove),
    "clear_all": staticmethod(clear_all),
})()
