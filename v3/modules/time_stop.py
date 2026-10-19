"""
modules/time_stop.py — 时间止损 v2.0（修复版）

修复内容：
1. 时间阈值改为小时制（不是根数）
2. 触发条件加入进度判断
"""

from __future__ import annotations
from typing import Optional, Tuple


def get_base_time_hours(scene: str) -> int:
    """
    获取场景对应的基础时间阈值（小时）
    
    规则：
    - A/E 场景（回调/反弹）：48 小时
    - B/F 场景（突破/跌破）：36 小时
    - C/G 场景（强势回踩/反弹）：24 小时
    
    Args:
        scene: 场景名称
    
    Returns:
        基础时间阈值（小时）
    """
    scene_lower = scene.lower()

    # 修改2：1H K线下阈值全部减半（原48/36/24 → 24/16/12）
    # 用前缀精确匹配，避免场景名内含字母误命中
    if scene_lower.startswith("a") or scene_lower.startswith("e"):
        return 24   # A/E 场景：原48 → 24小时
    elif scene_lower.startswith("b") or scene_lower.startswith("f"):
        return 16   # B/F 场景：原36 → 16小时
    elif scene_lower.startswith("c") or scene_lower.startswith("g"):
        return 12   # C/G 场景：原24 → 12小时
    else:
        return 16   # 默认 16 小时


def calc_time_sl_hours(scene: str, second_score: int) -> int:
    """
    计算时间止损阈值（小时）
    
    基于场景和打分动态调整：
    - 基础时间由场景决定
    - 高分（≥84）：基础时间 × 1.2
    - 低分（<77）：基础时间 × 0.8
    
    Args:
        scene: 场景名称
        second_score: 二次打分
    
    Returns:
        时间止损阈值（小时）
    """
    base_hours = get_base_time_hours(scene)
    
    if second_score >= 84:
        return int(base_hours * 1.2)
    elif second_score < 77:
        return int(base_hours * 0.8)
    else:
        return base_hours


def calc_price_progress(
    entry_price: float,
    current_price: float,
    tp_price: float,
    direction: str
) -> float:
    """
    计算价格运动进度（相对于止盈目标）
    
    Args:
        entry_price: 入场价
        current_price: 当前价
        tp_price: 止盈价
        direction: "LONG" 或 "SHORT"
    
    Returns:
        进度（0.0~1.0+，1.0 表示已到达止盈）
    """
    if direction == "LONG":
        total_distance = tp_price - entry_price
        current_distance = current_price - entry_price
    else:
        total_distance = entry_price - tp_price
        current_distance = entry_price - current_price
    
    if total_distance == 0:
        return 0.0
    
    progress = current_distance / total_distance
    
    # 处理反向运动（负进度）
    return max(0.0, progress)


def check_time_stop(
    entry_time: int,
    current_time: int,
    scene: str,
    second_score: int,
    entry_price: float,
    current_price: float,
    tp_price: float,
    direction: str,
    sl_price: Optional[float] = None,   # 修改2新增：用于计算 risk
    risk: Optional[float] = None,        # 修改2新增：入场-止损距离
) -> tuple[bool, Optional[str]]:
    """
    检查是否触发时间止损（修改2）

    触发条件：
    1. 持仓时间超过基础时间的 50%
    2. 价格运动进度 < 30%
    3a. 修改2新增：持仓超过 max_hours×50% 且浮亏 > 0.1×risk → 提前触发
    3b. 持仓时间超过完整阈值
    """
    # 计算持仓时间（小时）
    hours_held = (current_time - entry_time) / (1000 * 3600)
    
    # 获取时间止损阈值
    time_sl_threshold = calc_time_sl_hours(scene, second_score)
    base_hours = get_base_time_hours(scene)
    
    # 计算价格运动进度
    progress = calc_price_progress(entry_price, current_price, tp_price, direction)
    
    # 触发条件检查
    # 条件 1：持仓时间超过基础时间的 50%
    if hours_held < base_hours * 0.5:
        return False, None

    # 条件 2：价格运动进度 < 30%
    if progress >= 0.3:
        return False, None

    # 修改2 新增：半程浮亏提前触发
    # 持仓超过 max_hours×50% 且当前浮亏 > 0.1×risk → 不等满时间直接触发
    if sl_price is not None and risk is not None and risk > 0:
        if direction == "LONG":
            float_loss = entry_price - current_price   # 正值=亏损
        else:
            float_loss = current_price - entry_price
        if float_loss > 0.1 * risk:
            return True, (
                f"time_sl_early_{int(hours_held)}h"
                f"_float_loss_{float_loss:.4f}_gt_10pct_risk"
            )

    # 条件 3：持仓时间超过完整阈值
    if hours_held >= time_sl_threshold:
        return True, f"time_sl_{int(hours_held)}h_ge_{time_sl_threshold}h_prog_{progress:.0%}"

    return False, None


def get_time_stop_reason(hours_held: int, threshold: int, progress: float) -> str:
    """
    生成时间止损原因描述
    
    Args:
        hours_held: 实际持仓小时数
        threshold: 时间止损阈值
        progress: 价格运动进度
    
    Returns:
        原因描述
    """
    return f"time_sl_exceeded_{hours_held}h_ge_{threshold}h_prog_{progress:.0%}"
