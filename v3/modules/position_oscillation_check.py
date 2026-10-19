"""
modules/position_oscillation_check.py — TP1~TP2 区间震荡时间止损 (v2.0 新增)

来自新设计文档第四层「三阶段出场机制」：
  阶段2：TP1 触发后，剩余仓位在 TP1~TP2 之间
  阶段3：每根 K 线检查 —— 如果价格在 TP1~TP2 区间内震荡超过 N 根，则全平

设计原则（参考新文档）：
  不是等价格跌破才平，而是「时间到了，主动锁利」
  震荡 N 根 = 市场已经无法继续推进，动能衰竭信号

使用场景：
  ws_monitor.py 的持仓健康检查（每 30 分钟/每根 K 线）调用此模块
  仅适用于 TP1 已触发、剩余仓位处于 TP1~TP2 区间的持仓
"""

from __future__ import annotations


# ── 默认参数 ───────────────────────────────────────────────────────────────────

DEFAULT_OSCILLATION_BARS = 6    # 默认：震荡超过 6 根 1H K 线则全平
MIN_BARS_AFTER_TP1 = 2          # TP1 触发后至少等 2 根再检查（避免过早）


def check_tp1_tp2_oscillation(
    current_price: float,
    tp1_price: float,
    tp2_price: float,
    direction: str,
    bars_in_range: int,
    max_oscillation_bars: int = DEFAULT_OSCILLATION_BARS,
) -> dict:
    """
    检查是否触发 TP1~TP2 区间震荡止盈（时间止损）。

    Args:
        current_price:        当前标记价格
        tp1_price:            第一止盈价（已触发）
        tp2_price:            第二止盈价（目标）
        direction:            "LONG" | "SHORT"
        bars_in_range:        当前已在 TP1~TP2 区间内的 K 线根数
        max_oscillation_bars: 允许的最大震荡根数（默认 6）

    Returns:
        {
            "should_close": bool,
            "reason": str,
            "bars_in_range": int,
            "is_in_range": bool,
        }
    """
    if direction == "LONG":
        # 多头：TP1 < TP2，当前价在 TP1~TP2 之间
        in_range = tp1_price <= current_price <= tp2_price
    else:
        # 空头：TP2 < TP1，当前价在 TP2~TP1 之间
        in_range = tp2_price <= current_price <= tp1_price

    if not in_range:
        return {
            "should_close": False,
            "reason": "价格已突破区间（方向正确，继续持有）",
            "bars_in_range": 0,   # 重置计数
            "is_in_range": False,
        }

    # 在区间内，检查震荡根数
    if bars_in_range < MIN_BARS_AFTER_TP1:
        return {
            "should_close": False,
            "reason": f"TP1触发后仅{bars_in_range}根，等待确认",
            "bars_in_range": bars_in_range,
            "is_in_range": True,
        }

    if bars_in_range >= max_oscillation_bars:
        range_str = f"[{min(tp1_price, tp2_price):.4f}~{max(tp1_price, tp2_price):.4f}]"
        return {
            "should_close": True,
            "reason": (
                f"TP1~TP2区间{range_str}震荡{bars_in_range}根≥{max_oscillation_bars}根，"
                f"动能衰竭，主动锁利全平"
            ),
            "bars_in_range": bars_in_range,
            "is_in_range": True,
        }

    return {
        "should_close": False,
        "reason": f"区间内震荡{bars_in_range}/{max_oscillation_bars}根，继续观察",
        "bars_in_range": bars_in_range,
        "is_in_range": True,
    }


def update_bars_in_range(
    pos: dict,
    current_price: float,
) -> int:
    """
    根据当前价格更新 pos 字典中的 bars_in_range 计数。
    调用方负责将返回值写回 pos_state。

    pos 需要包含字段：
      tp1_price, tp2_price, direction
      (可选) bars_in_range → 初始化为 0

    Returns:
        更新后的 bars_in_range 值
    """
    tp1 = pos.get("tp1_price") or pos.get("tp1")
    tp2 = pos.get("tp2_price") or pos.get("tp2")
    direction = pos.get("direction", "LONG")

    if not tp1 or not tp2:
        return 0

    # 仅当 TP1 已触发（pos 中有 tp1_triggered=True 标记）才开始计数
    if not pos.get("tp1_triggered", False):
        return 0

    if direction == "LONG":
        in_range = float(tp1) <= current_price <= float(tp2)
    else:
        in_range = float(tp2) <= current_price <= float(tp1)

    current_count = int(pos.get("bars_in_range", 0))
    return current_count + 1 if in_range else 0


def get_oscillation_bar_limit(scene: str) -> int:
    """
    根据场景类型返回最大震荡根数（适应不同场景的持仓时间预期）。

    场景：
      A_pullback    → 8 根（回调场景，趋势较强，给更多空间）
      B_breakout    → 5 根（突破场景，动能衰竭更快）
      C_first_pull  → 10 根（强势场景，趋势延续性强）
      E / 其他      → 6 根（默认）
    """
    mapping = {
        "A_pullback":   8,
        "B_breakout":   5,
        "C_first_pull": 10,
    }
    return mapping.get(scene, DEFAULT_OSCILLATION_BARS)
