"""
modules/market_bias.py — 市场趋势判断 v2.0（6种状态扩展版）

v1.0：3种状态（up/down/neutral），仅 1H EMA 判断
v2.0：6种精细状态 + 多级别联合判断 + ATR 百分位识别极端状态

新增状态：
  BREAKOUT_PENDING  — 低波动蓄势，等待方向（禁止开新仓）
  HIGH_VOLATILITY   — 极端波动（禁止开新仓，已有仓位考虑减仓）
  ACCUMULATION      — 低波动积累中（最佳入场窗口）

行为映射：
  TRENDING_UP/DOWN   → 顺势方向允许，逆势门槛+20分，仓位×1.2
  RANGING            → 双向允许，仓位×0.8
  BREAKOUT_PENDING   → 禁止开新仓（返回 HALT）
  HIGH_VOLATILITY    → 禁止开新仓（返回 HALT）
  ACCUMULATION       → 积极入场，仓位×0.9（小仓位建仓）
"""

from __future__ import annotations
from typing import Literal
from modules.dynamic_baseline import _ema, _closes, _atr_series, _percentile, _mean


# ── 类型 ──────────────────────────────────────────────────────────────────────

MarketBias   = Literal["up", "down", "neutral"]          # v1.0 兼容
MarketState  = Literal[
    "TRENDING_UP",
    "TRENDING_DOWN",
    "RANGING",
    "BREAKOUT_PENDING",
    "HIGH_VOLATILITY",
    "ACCUMULATION",
]

# MarketState → 是否允许开新仓
MARKET_STATE_HALT = {"BREAKOUT_PENDING", "HIGH_VOLATILITY"}

# MarketState → 仓位系数
MARKET_STATE_SIZE_MULTIPLIER: dict[str, float] = {
    "TRENDING_UP":      1.2,
    "TRENDING_DOWN":    1.2,
    "RANGING":          0.8,
    "BREAKOUT_PENDING": 0.0,   # 不开仓
    "HIGH_VOLATILITY":  0.0,   # 不开仓
    "ACCUMULATION":     0.9,
}

# MarketState → 逆势信号额外门槛（分）
MARKET_STATE_COUNTER_PENALTY: dict[str, int] = {
    "TRENDING_UP":      20,   # 上涨中做空需要额外20分
    "TRENDING_DOWN":    20,   # 下跌中做多需要额外20分
    "RANGING":          0,
    "BREAKOUT_PENDING": 999,  # 不开
    "HIGH_VOLATILITY":  999,  # 不开
    "ACCUMULATION":     5,    # 轻度限制逆势
}

# MarketState → v1 兼容 bias 映射
STATE_TO_BIAS: dict[MarketState, MarketBias] = {
    "TRENDING_UP":      "up",
    "TRENDING_DOWN":    "down",
    "RANGING":          "neutral",
    "BREAKOUT_PENDING": "neutral",
    "HIGH_VOLATILITY":  "neutral",
    "ACCUMULATION":     "neutral",
}


# ── 辅助：多级别趋势判断 ──────────────────────────────────────────────────────

def _trend_single(closes: list[float]) -> Literal["up", "down", "neutral"]:
    """用 EMA20/EMA50 + 价格位置判断单一级别趋势"""
    if len(closes) < 50:
        return "neutral"
    ema20 = _ema(closes[-20:], 20)
    ema50 = _ema(closes[-50:], 50) if len(closes) >= 50 else ema20
    price = closes[-1]
    if ema20 > ema50 and price > ema20:
        return "up"
    if ema20 < ema50 and price < ema20:
        return "down"
    return "neutral"


def _atr_percentile_rank(k: list) -> float:
    """
    返回当前 ATR 在最近 60 根 K 线历史 ATR 中的百分位（0~100）。
    用于识别波动率状态：
      < 20  → 极低（ACCUMULATION / BREAKOUT_PENDING 候选）
      > 85  → 极高（HIGH_VOLATILITY 候选）
    """
    atrs = _atr_series(k)
    if len(atrs) < 5:
        return 50.0
    window = atrs[-60:] if len(atrs) >= 60 else atrs
    cur = _mean(atrs[-3:]) if len(atrs) >= 3 else atrs[-1]
    rank = sum(1 for a in window if a <= cur) / len(window) * 100
    return round(rank, 1)


def _is_range_contracting(k: list, window: int = 8) -> bool:
    """
    检测近 window 根 K 线是否处于收窄区间（横盘蓄势）：
    区间宽度 < 2×ATR_baseline 且 近 window 根成交量低于均线
    """
    if len(k) < max(window, 20):
        return False
    highs = [float(x[2]) for x in k[-window:]]
    lows  = [float(x[3]) for x in k[-window:]]
    atrs  = _atr_series(k[-21:])
    atr_base = _mean(atrs[-10:]) if len(atrs) >= 10 else _mean(atrs)
    range_w = max(highs) - min(lows)
    return range_w < 2 * atr_base


# ── 核心：6种状态判断 ─────────────────────────────────────────────────────────

def judge_market_state(
    k1h: list,
    k4h: list | None = None,
    avg_funding_rate: float = 0.0,   # 全市场平均资金费率
) -> MarketState:
    """
    6种市场状态判断（v2.0 核心入口）。

    输入：
      k1h：BTC 1H K 线（至少 60 根）
      k4h：BTC 4H K 线（可选，提供则做双周期确认）
      avg_funding_rate：全市场平均资金费率（可选）

    输出：
      MarketState 之一

    判断逻辑（优先级从高到低）：
      1. HIGH_VOLATILITY：ATR 百分位 > 85
      2. BREAKOUT_PENDING：ATR 百分位 < 20 + 区间收窄 + K线数≥6根
      3. ACCUMULATION：ATR 百分位 < 25 + 价格在 EMA 上方（多头积累）
      4. TRENDING_UP：1H + 4H 趋势均向上
      5. TRENDING_DOWN：1H + 4H 趋势均向下
      6. RANGING：其余
    """
    closes_1h = _closes(k1h)
    atr_rank  = _atr_percentile_rank(k1h)
    trend_1h  = _trend_single(closes_1h)

    # 4H 级别趋势（可选）
    if k4h and len(k4h) >= 50:
        closes_4h = _closes(k4h)
        trend_4h  = _trend_single(closes_4h)
    else:
        trend_4h = trend_1h   # 无 4H 数据则退化为 1H

    # ── 1. HIGH_VOLATILITY（最高优先级，必须先检查）─────────────────────────
    # ── 1. HIGH_VOLATILITY（分级响应 v1.2）────────────────────────────────────
    # 旧逻辑（已注释）: atr_rank > 85 → HALT，完全停止开仓
    # if atr_rank > 85:
    #     return "HIGH_VOLATILITY"
    #
    # 新逻辑（v1.2）三档分级，状态字符串保持 HIGH_VOLATILITY 不变，
    # 分级参数通过 get_market_state_info(atr_rank=) 动态生成：
    #   ATR 70~85%  → can_open=True,  禁止方向=无,   size×0.6
    #   ATR 85~95%  → can_open=True,  禁止方向=LONG, size×0.4（只允许做空）
    #   ATR >95%    → can_open=False  HALT（完全停止）
    if atr_rank > 70:
        return "HIGH_VOLATILITY"

    # ── 2. BREAKOUT_PENDING（低波动横盘蓄势）────────────────────────────────
    if atr_rank < 20 and _is_range_contracting(k1h, window=8):
        return "BREAKOUT_PENDING"

    # ── 3. ACCUMULATION（低波动积累 + 多头结构）─────────────────────────────
    if atr_rank < 25 and trend_1h == "up":
        # 额外确认：资金费率不极端（极端说明已追高，不是积累）
        if abs(avg_funding_rate) < 0.002:
            return "ACCUMULATION"

    # ── 4. TRENDING_UP（双级别确认多头趋势）────────────────────────────────
    if trend_1h == "up" and trend_4h == "up":
        return "TRENDING_UP"

    # ── 5. TRENDING_DOWN（双级别确认空头趋势）──────────────────────────────
    if trend_1h == "down" and trend_4h == "down":
        return "TRENDING_DOWN"

    # ── 6. RANGING（其余情况）───────────────────────────────────────────────
    return "RANGING"


# ── 向后兼容：v1 接口保留 ────────────────────────────────────────────────────

def judge_market_bias(k1h: list, lookback_periods: int = 50) -> MarketBias:
    """
    v1.0 兼容接口（返回 up/down/neutral）。
    内部调用 judge_market_state 并映射。
    """
    state = judge_market_state(k1h)
    return STATE_TO_BIAS[state]


def get_market_state_info(state: MarketState, atr_rank: float = -1.0) -> dict:
    """
    返回当前状态的完整行为配置（供 scanner/pre_order_gate 使用）。

    v1.2 新增 atr_rank 参数用于 HIGH_VOLATILITY 分级响应：
      ATR 70~85%  → can_open=True,  preferred=BOTH,  size×0.6  （减仓但可开）
      ATR 85~95%  → can_open=True,  preferred=SHORT, size×0.4  （只允许做空）
      ATR  >95%   → can_open=False  HALT              size×0.0  （完全停止）
    旧逻辑（已注释）: HIGH_VOLATILITY 一律 HALT (can_open=False, size=0.0)
    """
    descriptions = {
        "TRENDING_UP":      "单边上涨，多头为主",
        "TRENDING_DOWN":    "单边下跌，空头为主",
        "RANGING":          "横盘震荡，双向机会",
        "BREAKOUT_PENDING": "蓄势待发，等待方向选择（暂停开仓）",
        "HIGH_VOLATILITY":  "极端波动，谨慎减仓（暂停开仓）",
        "ACCUMULATION":     "低波动积累，强势启动前兆（小仓建仓）",
    }

    # HIGH_VOLATILITY 分级响应（v1.2）
    if state == "HIGH_VOLATILITY" and atr_rank >= 0:
        if atr_rank > 95:
            # 旧：所有 >85 都是这种情况；新：只有 >95 才 HALT
            return {
                "state": state,
                "description": "极端波动(ATR>95%)，全面暂停开仓",
                "can_open": False,
                "size_multiplier": 0.0,
                "counter_penalty": 999,
                "preferred_direction": "BOTH",
                "atr_rank": round(atr_rank, 1),
                "hv_tier": "halt",
            }
        elif atr_rank > 85:
            return {
                "state": state,
                "description": "高波动(ATR 85~95%)，禁止多头，空头小仓",
                "can_open": True,
                "size_multiplier": 0.4,
                "counter_penalty": 999,          # 多头被 counter_penalty 直接封锁
                "preferred_direction": "SHORT",  # 只允许做空
                "atr_rank": round(atr_rank, 1),
                "hv_tier": "short_only",
            }
        else:  # 70 < atr_rank <= 85
            return {
                "state": state,
                "description": "波动偏高(ATR 70~85%)，双向可开但减仓",
                "can_open": True,
                "size_multiplier": 0.6,
                "counter_penalty": 15,
                "preferred_direction": "BOTH",
                "atr_rank": round(atr_rank, 1),
                "hv_tier": "reduced",
            }

    return {
        "state":              state,
        "description":        descriptions[state],
        "can_open":           state not in MARKET_STATE_HALT,
        "size_multiplier":    MARKET_STATE_SIZE_MULTIPLIER[state],
        "counter_penalty":    MARKET_STATE_COUNTER_PENALTY[state],
        "preferred_direction": (
            "LONG"  if state == "TRENDING_UP"   else
            "SHORT" if state == "TRENDING_DOWN"  else
            "BOTH"
        ),
    }


# ── 原有兼容接口 ──────────────────────────────────────────────────────────────

def check_position_gate(second_score: int, direction: str,
                        market_bias: MarketBias) -> int:
    """
    v1 兼容：根据趋势调整有效分数。
    新代码建议改用 get_market_state_info() + counter_penalty。
    """
    if market_bias == "up" and direction == "SHORT":
        return second_score - 10
    elif market_bias == "down" and direction == "LONG":
        return second_score - 10
    return second_score


def get_trend_alignment_score(direction: str, market_bias: MarketBias) -> int:
    """v1 兼容：顺势加分（0 或 5）"""
    if market_bias == "up" and direction == "LONG":
        return 5
    elif market_bias == "down" and direction == "SHORT":
        return 5
    return 0
