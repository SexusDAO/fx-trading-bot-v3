"""
position_size.py — 仓位大小统一计算 (P1)

规则（来自 SYSTEM_DESIGN_V3.md 第九章）：
  - 趋势多单：65~69→300U，70~79→500U，80~89→700U，≥90→1000U
  - 趋势空单：65~69→300U，70~79→450U，≥80→600U
  - 逆势 L2：250U；逆势 L1：125U
  - 场景四（65~69分 + 有清算区）：基础 × 50%
  - 动态系数：BTC顺势×1.2，逆势×0.8，连亏3笔×0.8
  - 单仓上限（含补仓）：1000U
"""

from pathlib import Path
import json


# ── 基础仓位表 ────────────────────────────────────────────────────────────────

_TREND_LONG = [
    (90, 1000),
    (80, 700),
    (70, 500),
    (65, 300),
]

_TREND_SHORT = [
    (80, 600),
    (70, 450),
    (65, 300),
]

_REVERSAL_L2 = 250
_REVERSAL_L1 = 125
_HARD_CAP    = 1000


def _base_notional(signal_type: str, score: int) -> int:
    """返回基础名义仓位（U），不含动态系数"""
    if signal_type in ("reversal",):
        return _REVERSAL_L2
    if signal_type == "reversal_l1":
        return _REVERSAL_L1
    if signal_type in ("reversal_short",):
        return _REVERSAL_L2

    if signal_type == "trend_short":
        for threshold, size in _TREND_SHORT:
            if score >= threshold:
                return size
        return 0

    # trend（默认）
    for threshold, size in _TREND_LONG:
        if score >= threshold:
            return size
    return 0


# ── 统一入口 ──────────────────────────────────────────────────────────────────

def calc_position_size(signal_type: str,
                       score: int,
                       btc_trend: str = "neutral",       # "bull" | "bear" | "neutral"
                       consecutive_losses: int = 0,
                       scene_four: bool = False) -> int:
    """
    返回本仓位建议名义仓位（U），已考虑所有系数。
    scene_four=True：评分65~69分且有清算区，仓位减半。
    返回 0 表示不应开仓（连续亏损≥5笔）。
    """
    # 连亏5笔：暂停
    if consecutive_losses >= 5:
        return 0

    base = _base_notional(signal_type, score)
    if base == 0:
        return 0

    size = float(base)

    # 场景四减半（只对 65~69 分区间）
    if scene_four and 65 <= score <= 69:
        size *= 0.5

    # BTC 趋势系数
    if btc_trend == "bull" and signal_type not in ("trend_short", "reversal_short"):
        size *= 1.2
    elif btc_trend == "bear" and signal_type in ("trend_short", "reversal_short"):
        size *= 1.2
    elif btc_trend == "bull" and signal_type in ("trend_short", "reversal_short"):
        size *= 0.8
    elif btc_trend == "bear" and signal_type not in ("trend_short", "reversal_short"):
        size *= 0.8

    # 连亏3笔缩仓
    if consecutive_losses >= 3:
        size *= 0.8

    return min(int(size), _HARD_CAP)
