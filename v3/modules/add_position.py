"""
add_position.py — 补仓逻辑 (P2)

两种补仓独立处理，互不影响：

  摊平型 (flatten)：
    触发：浮亏 >3%，结构健康评分 ≥60，补仓次数 <2，距上次 >30min
    仓位：≤首仓大小，LIMIT_ONLY
    成交后：重新计算均价，更新止损

  信号增强型 (signal_enhance)：
    触发：场景四（初始65~69分减半）成交后评分升至 ≥70
    仓位：补足至正常档位差额
    成交后：均价更新

两种补仓各自计次，单仓合计不超过 hard_cap（1000U）。
"""

from datetime import datetime, timezone
from typing import Optional


HARD_CAP = 1000  # 单仓含所有补仓的名义上限

# ── 工具 ──────────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _minutes_since(iso_str: Optional[str]) -> float:
    if not iso_str:
        return 9999.0
    try:
        dt = datetime.strptime(iso_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 60
    except Exception:
        return 9999.0

def calc_avg_price(old_notional: float, old_avg: float,
                   add_notional: float, add_price: float) -> float:
    """加权平均入场价"""
    total = old_notional + add_notional
    if total <= 0:
        return old_avg
    return (old_notional * old_avg + add_notional * add_price) / total


# ── 摊平型补仓 ────────────────────────────────────────────────────────────────

def check_flatten(pos: dict, current_price: float,
                  health_score: int = 100) -> dict:
    """
    检查摊平型补仓是否触发。

    Args:
        pos:           pos_state 中该仓位的 dict
        current_price: 当前市价
        health_score:  持仓健康评分（0~100），来自 LLM-3 或重新评分

    Returns:
        {"should_add": True/False, "reason": str, "add_notional": int}
    """
    entry     = pos.get("entry_price", 0)
    notional  = pos.get("notional", 0)
    avg_price = pos.get("avg_price", entry)
    direction = pos.get("direction", "LONG")
    flatten_count = pos.get("flatten_count", 0)
    last_add_at   = pos.get("last_flatten_at")

    # 计算浮亏
    if direction == "LONG":
        pnl_pct = (current_price - avg_price) / avg_price if avg_price else 0
    else:
        pnl_pct = (avg_price - current_price) / avg_price if avg_price else 0

    # 条件检查
    if pnl_pct >= -0.03:
        return {"should_add": False, "reason": f"浮亏不足3%（当前{pnl_pct:.2%}）"}
    if health_score < 60:
        return {"should_add": False, "reason": f"健康评分不足60（{health_score}）"}
    if flatten_count >= 2:
        return {"should_add": False, "reason": f"补仓次数已达上限（{flatten_count}/2）"}
    if _minutes_since(last_add_at) < 30:
        return {"should_add": False, "reason": f"距上次补仓不足30分钟"}

    # 计算补仓量（≤首仓大小，且不超过硬上限）
    first_notional = pos.get("first_notional", notional)
    current_total  = pos.get("total_notional", notional)
    add_notional   = min(first_notional, HARD_CAP - current_total)

    if add_notional <= 0:
        return {"should_add": False, "reason": "已达单仓上限1000U"}

    return {
        "should_add":   True,
        "reason":       f"浮亏{pnl_pct:.2%}，触发摊平补仓",
        "add_notional": int(add_notional),
        "add_type":     "flatten",
    }


def apply_flatten(pos: dict, add_price: float, add_notional: int) -> dict:
    """
    补仓成交后更新仓位 dict（只更新内存，写入由 pos_state_store 负责）。
    返回更新后的 pos dict。
    """
    old_notional  = pos.get("total_notional", pos.get("notional", 0))
    old_avg       = pos.get("avg_price", pos.get("entry_price", add_price))

    new_avg       = calc_avg_price(old_notional, old_avg, add_notional, add_price)
    new_total     = old_notional + add_notional
    flatten_count = pos.get("flatten_count", 0) + 1

    pos = dict(pos)
    pos["avg_price"]      = round(new_avg, 8)
    pos["total_notional"] = new_total
    pos["flatten_count"]  = flatten_count
    pos["last_flatten_at"] = _now_iso()
    return pos


# ── 信号增强型补仓 ────────────────────────────────────────────────────────────

def check_signal_enhance(pos: dict, new_score: int) -> dict:
    """
    检查信号增强型补仓是否触发（场景四专用）。

    场景四：初始 65~69 分减半开仓，成交后评分升至 ≥70 → 补足至正常档位。
    """
    if not pos.get("scene_four", False):
        return {"should_add": False, "reason": "非场景四仓位"}

    if pos.get("enhance_done", False):
        return {"should_add": False, "reason": "信号增强型补仓已完成"}

    if new_score < 70:
        return {"should_add": False, "reason": f"评分未达70（当前{new_score}）"}

    # 正常档位 vs 当前减半仓位
    full_notional = pos.get("full_notional", 0)
    current_total = pos.get("total_notional", pos.get("notional", 0))
    add_notional  = full_notional - current_total

    if add_notional <= 0:
        return {"should_add": False, "reason": "仓位已是正常档位，无需补仓"}

    return {
        "should_add":   True,
        "reason":       f"场景四评分升至{new_score}，补足至正常档位",
        "add_notional": int(add_notional),
        "add_type":     "signal_enhance",
    }


def apply_signal_enhance(pos: dict, add_price: float, add_notional: int) -> dict:
    """信号增强型补仓成交后更新 pos dict"""
    old_notional = pos.get("total_notional", pos.get("notional", 0))
    old_avg      = pos.get("avg_price", pos.get("entry_price", add_price))

    new_avg   = calc_avg_price(old_notional, old_avg, add_notional, add_price)
    new_total = old_notional + add_notional

    pos = dict(pos)
    pos["avg_price"]      = round(new_avg, 8)
    pos["total_notional"] = new_total
    pos["enhance_done"]   = True
    return pos
