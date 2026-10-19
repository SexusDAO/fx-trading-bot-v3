"""
modules/position_judge.py — 位置判断 + 场景候选映射 v1.2

市场状态过滤（v1.2 新增）：
  RANGING状态：
    position_pct > 0.75 → 只允许 E 场景（高位做空）
    position_pct < 0.25 → 只允许 A 场景（低位回调多头）
    position_pct 25%~75% → 返回空列表，不开仓（中间地带不确定）
    禁用 C 场景（震荡中强势回踩花所 = 高点追多）
  TRENDING_UP：允许 A/B/C（顺势多头全开）
  TRENDING_DOWN：禁用所有多头场景，只允许 E 场景
  其他状态（ACCUMULATION/BREAKOUT_PENDING/未知）：保持原有逻辑

多头：position_pct 位置逻辑不变。
空头（v1.1）：移除"低位不追空"的绝对拦截，改为三条件判断：
  ① is_downtrend：近20根高低点持续降低
  ② near_recent_high：当前价 >= 近5根最高价 × 0.97
  ③ not_oversold：RSI > rsi_mean - 2×rsi_std
  三条全满足才允许进入场景匹配。

# ── v1.1 旧逻辑（已注释保留）────────────────────────────────────────────────
# 旧版 judge() 无 market_state 参数，所有状态走同一套位置逻辑：
#   track==momentum → C 场景
#   direction==LONG:
#     pct>0.80 → A；0.20~0.80 → B；else → A
#   direction==SHORT:
#     无K线 → pct>0.20则E；有K线 → 三条件判断
# 问题：RANGING状态下 C 场景会频繁触发（震荡高点追多），本次修改禁用。
# ─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations
from modules.dynamic_baseline import DynamicBaseline, position_pct

SCENE_A = "A_pullback"
SCENE_B = "B_breakout"
SCENE_C = "C_first_pull"
SCENE_E = "E_short"


def _rsi_simple(closes: list[float], n: int = 14) -> float:
    if len(closes) < n + 1:
        return 50.0
    gains  = [max(closes[i] - closes[i-1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i-1] - closes[i], 0) for i in range(1, len(closes))]
    ag = sum(gains[-n:]) / n
    al = sum(losses[-n:]) / n
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def _short_conditions(k1h: list, mark_price: float,
                      bl: DynamicBaseline) -> tuple[bool, dict]:
    """
    空头三条件判断，返回 (all_pass, detail_dict)。
    """
    if len(k1h) < 20:
        return False, {"reason": "数据不足"}

    highs  = [float(x[2]) for x in k1h]
    lows   = [float(x[3]) for x in k1h]
    closes = [float(x[4]) for x in k1h]

    # ① is_downtrend：近20根分前后两半，高低点均降低
    hw20 = highs[-20:]; lw20 = lows[-20:]
    n_h  = max(1, len(hw20) // 2)
    is_downtrend = (max(hw20[-n_h:]) < max(hw20[:n_h]) and
                    min(lw20[-n_h:]) < min(lw20[:n_h]))

    # ② near_recent_high：当前价 >= 近5根最高价 × 0.97
    recent_high  = max(highs[-5:])
    near_high    = mark_price >= recent_high * 0.97

    # ③ not_oversold：RSI > rsi_mean - 2×rsi_std
    rsi_val      = _rsi_simple(closes)
    rsi_floor    = bl.rsi_mean - 2 * bl.rsi_std
    not_oversold = rsi_val > rsi_floor

    detail = {
        "is_downtrend":     is_downtrend,
        "near_recent_high": near_high,
        "not_oversold":     not_oversold,
        "rsi_val":          round(rsi_val, 1),
        "rsi_floor":        round(rsi_floor, 1),
        "recent_high":      round(recent_high, 8),
    }
    return (is_downtrend and near_high and not_oversold), detail


def judge(mark_price: float,
          direction: str,
          track: str,
          bl: DynamicBaseline,
          k1h: list | None = None,
          market_state: str = "") -> list[str]:
    """
    参数：
        mark_price   — 当前价
        direction    — "LONG" | "SHORT"
        track        — "momentum" | "oscillation"
        bl           — DynamicBaseline
        k1h          — K线列表（空头三条件判断需要）
        market_state — 市场状态字符串，由 judge_market_state() 返回
    返回：
        候选场景列表（空列表 = 不入场）
    """
    pct = position_pct(mark_price, bl)

    # ════════════════════════════════════════════════════════
    # v1.2 市场状态过滤（优先于默认位置逻辑）
    # ════════════════════════════════════════════════════════

    # ── RANGING：中间地带不开仓，禁用 C 场景 ──────────────────────────────────
    if market_state == "RANGING":
        if direction == "SHORT":
            # 高位空头：pct > 0.75 才允许做空
            if pct > 0.75:
                if k1h is None or len(k1h) < 20:
                    return [SCENE_E]
                pass_cond, _ = _short_conditions(k1h, mark_price, bl)
                return [SCENE_E] if pass_cond else []
            else:
                return []  # RANGING 中低位不追空
        else:  # LONG
            if pct < 0.25:
                return [SCENE_A]  # 低位回调：只允许 A 场景
            elif pct > 0.75:
                return []  # RANGING 高位不开多
            else:
                return []  # 25%~75% 中间地带，不确定，不开仓

    # ── TRENDING_DOWN：禁止所有多头场景 ──────────────────────────────────────
    if market_state == "TRENDING_DOWN":
        if direction == "LONG":
            return []
        else:
            if k1h is None or len(k1h) < 20:
                return [SCENE_E]
            pass_cond, _ = _short_conditions(k1h, mark_price, bl)
            return [SCENE_E] if pass_cond else []

    # ── TRENDING_UP：顺势多头全开（走默认逻辑，不额外限制）──────────────────
    # （直接落入下方默认逻辑）

    # ════════════════════════════════════════════════════════
    # 默认逻辑（ACCUMULATION / BREAKOUT_PENDING / TRENDING_UP / 未知）
    # ════════════════════════════════════════════════════════

    # ── 强势赛道：场景 C（momentum track）────────────────────────────────────
    if track == "momentum" and direction == "LONG":
        return [SCENE_C]

    # ── 多头位置逻辑 ──────────────────────────────────────────────────────────
    if direction == "LONG":
        if pct > 0.80:
            return [SCENE_A]
        elif 0.20 <= pct <= 0.80:
            return [SCENE_B]
        else:
            return [SCENE_A]

    # ── 空头三条件判断（v1.1）────────────────────────────────────────────────
    elif direction == "SHORT":
        if k1h is None or len(k1h) < 20:
            return [SCENE_E] if pct > 0.20 else []
        pass_cond, _ = _short_conditions(k1h, mark_price, bl)
        return [SCENE_E] if pass_cond else []

    return []


def describe(mark_price: float, bl: DynamicBaseline,
             direction: str = "", k1h: list | None = None,
             market_state: str = "") -> str:
    """位置描述（日志/trace 用）"""
    pct = position_pct(mark_price, bl)
    if pct > 0.80:    label = "高位(>80%)"
    elif pct > 0.20:  label = f"中位({pct:.0%})"
    else:             label = "低位(<20%)"
    base = f"position_pct={pct:.2f} [{label}] state={market_state or 'unknown'}"
    if direction == "SHORT" and k1h and len(k1h) >= 20:
        _, det = _short_conditions(k1h, mark_price, bl)
        base += (f" | 空头: 趋势↓={det['is_downtrend']} "
                 f"靠近高点={det['near_recent_high']} "
                 f"未超卖={det['not_oversold']}(RSI={det['rsi_val']}>{det['rsi_floor']})")
    return base
