#!/usr/bin/env python3
"""
pos_state_store.py — 持仓状态单一写入者

全系统唯一的 pos_state 读写入口。
所有模块通过 store.upsert / store.delete / store.read 操作，
禁止直接读写 pos_state.json 文件。

接口：
  store.read(sym=None)          → dict  （无 sym 返回全量）
  store.upsert(sym, data)       → None  （merge，不覆盖整个 dict）
  store.delete(sym)             → None
  store.clear_stale(live_syms)  → list  （返回被清理的 sym 列表）
  store.set_sl(sym, sl_price)   → None  （快捷：只更新止损价）
  store.set_tp_hit(sym, level)  → None  （快捷：标记 tp1/tp2 已触达）
"""

import json
import os
import threading
from pathlib import Path

_WORKSPACE  = Path("/root/.openclaw/workspace")
_STATE_FILE = _WORKSPACE / "pos_state.json"
_LOCK       = threading.Lock()


def _atomic_write(data: dict):
    """原子写：先写 tmp，再 replace，防崩溃时文件损坏。"""
    tmp = _STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tmp.replace(_STATE_FILE)


def _raw_read() -> dict:
    """直接读文件，不加锁（调用方自己持锁）。"""
    try:
        if _STATE_FILE.exists():
            return json.loads(_STATE_FILE.read_text())
    except Exception:
        pass
    return {}


# ── 公共接口 ──────────────────────────────────────────────────────────────────

def read(sym: str = None):
    """
    读取持仓状态。
    sym=None → 返回全量 dict
    sym='BTCUSDT' → 返回该标的 dict，不存在返回 {}
    """
    with _LOCK:
        data = _raw_read()
    if sym is None:
        return data
    return data.get(sym, {})


def upsert(sym: str, fields: dict):
    """
    新增或更新持仓记录。
    fields 与现有记录 merge（不覆盖整个 dict），fields 中的 key 优先。

    示例：
        store.upsert('BTCUSDT', {
            'direction': 'LONG',
            'entry_price': 65000.0,
            'qty': 0.015,
            'sl_price': 63000.0,
            'opened_at': time.time(),
        })
    """
    with _LOCK:
        data = _raw_read()
        existing = data.get(sym, {})
        existing.update(fields)
        data[sym] = existing
        _atomic_write(data)


def delete(sym: str):
    """删除持仓记录（平仓成交后调用）。"""
    with _LOCK:
        data = _raw_read()
        if sym in data:
            del data[sym]
            _atomic_write(data)


def clear_stale(live_syms: set) -> list:
    """
    清理 pos_state 里没有实际仓位的残留记录。
    live_syms：当前真实有仓位的标的集合（从 API get_positions 获取）。
    返回被清理的 sym 列表。
    """
    with _LOCK:
        data = _raw_read()
        stale = [s for s in data if s not in live_syms]
        if stale:
            for s in stale:
                del data[s]
            _atomic_write(data)
    return stale


def set_sl(sym: str, sl_price: float):
    """快捷接口：只更新止损价。"""
    upsert(sym, {"sl_price": sl_price})


def set_tp_hit(sym: str, level: int):
    """快捷接口：标记止盈档位已触达（level=1 or 2）。"""
    upsert(sym, {f"tp{level}_hit": True})


# ── 模块级单例（import 即可用）─────────────────────────────────────────────────
# 用法：
#   from pos_state_store import store
#   store.upsert('BTCUSDT', {...})
#   pos = store.read('BTCUSDT')

class _Store:
    read        = staticmethod(read)
    upsert      = staticmethod(upsert)
    delete      = staticmethod(delete)
    clear_stale = staticmethod(clear_stale)
    set_sl      = staticmethod(set_sl)
    set_tp_hit  = staticmethod(set_tp_hit)


store = _Store()


if __name__ == "__main__":
    # 简单自测
    import time
    print("当前 pos_state:", json.dumps(read(), indent=2, ensure_ascii=False))
    print("pos_state_store 加载正常 ✅")
