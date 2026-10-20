"""
manual_positions.py — 手动仓位管理工具

任何在 manual_positions.json 中登记的仓位，
所有自动化脚本都应跳过对其的：
  - 自动止盈/止损挂单
  - 自动平仓
  - 自动补仓
  - 移动止损
  - 信号分析介入

使用方式：
    from manual_positions import is_manual, list_manual, add_manual, remove_manual

    # 在自动化脚本处理仓位前检查
    if is_manual(sym, pos_side):
        continue  # 跳过，不干预
"""

import json, os, datetime

MANUAL_FILE = os.path.join(os.path.dirname(__file__), "manual_positions.json")


def _load() -> dict:
    try:
        with open(MANUAL_FILE) as f:
            return json.load(f)
    except Exception:
        return {"positions": []}


def _save(data: dict):
    with open(MANUAL_FILE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def is_manual(symbol: str, position_side: str = None) -> bool:
    """
    检查某个仓位是否为手动仓位。
    symbol:        如 "ONUSDT"
    position_side: "LONG" | "SHORT" | None（None 表示任意方向均算）
    返回 True 表示该仓位由用户手动管理，自动化脚本应跳过。
    """
    data = _load()
    for p in data.get("positions", []):
        if p.get("symbol", "").upper() != symbol.upper():
            continue
        if position_side is None:
            return True
        if p.get("positionSide", "").upper() == position_side.upper():
            return True
    return False


def list_manual() -> list:
    """返回所有手动仓位列表"""
    return _load().get("positions", [])


def add_manual(symbol: str, position_side: str, reason: str = "手动建仓", note: str = "") -> bool:
    """将仓位添加到手动管理列表"""
    data = _load()
    # 避免重复添加
    for p in data["positions"]:
        if (p.get("symbol","").upper() == symbol.upper() and
                p.get("positionSide","").upper() == position_side.upper()):
            return False  # 已存在
    data["positions"].append({
        "symbol":       symbol.upper(),
        "positionSide": position_side.upper(),
        "reason":       reason,
        "added_at":     datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "note":         note
    })
    _save(data)
    return True


def remove_manual(symbol: str, position_side: str = None) -> bool:
    """从手动管理列表中移除仓位（平仓后调用）"""
    data = _load()
    before = len(data["positions"])
    data["positions"] = [
        p for p in data["positions"]
        if not (
            p.get("symbol","").upper() == symbol.upper() and
            (position_side is None or p.get("positionSide","").upper() == position_side.upper())
        )
    ]
    if len(data["positions"]) < before:
        _save(data)
        return True
    return False
