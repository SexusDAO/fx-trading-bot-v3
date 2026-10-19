"""
market_filter.py — 环境过滤统一入口 (P2)

四项过滤，返回调整后评分。
  过滤一：BTC 趋势（多单+5/-10，空单+5/-10）
  过滤二：时段（美股开盘 -10，低流动性 -10）
  过滤三：资金费率结算窗口（返回 BLOCKED=-999）
  过滤四：恐慌贪婪指数（极度恐慌禁空，极度贪婪禁多）

返回：
  -999  → 本轮完全禁止开仓（资金费率窗口）
  负数  → 信号降分，可能跌破门槛
  原分或加分 → 正常
"""

from datetime import datetime, timezone
from typing import Optional

BLOCKED = -999

# 资金费率结算时刻（UTC 小时:分）：0:00 / 8:00 / 16:00
_FUNDING_HOURS_UTC = [0, 8, 16]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _minutes_from_funding_settlement(now: Optional[datetime] = None) -> int:
    """
    返回距最近一次资金费率结算的分钟数（正=结算后，负=结算前）。
    例：结算前 10 分钟 → -10；结算后 5 分钟 → +5
    """
    if now is None:
        now = _utc_now()
    total_minutes = now.hour * 60 + now.minute
    closest = min(_FUNDING_HOURS_UTC, key=lambda h: abs(total_minutes - h * 60))
    return total_minutes - closest * 60


def _is_in_funding_window(funding_rate: float,
                          now: Optional[datetime] = None) -> bool:
    """
    是否在资金费率结算保护窗口内。
    普通费率：结算前 15 分钟 ~ 结算后 5 分钟
    极端费率（|rate| > 0.5%）：结算前 20 分钟 ~ 结算后 10 分钟
    """
    delta = _minutes_from_funding_settlement(now)
    extreme = abs(funding_rate) > 0.005
    pre  = -20 if extreme else -15
    post =  10 if extreme else   5
    return pre <= delta <= post


def _btc_score_adj(direction: str, btc_trend: str) -> int:
    """BTC 趋势对评分的调整量"""
    if btc_trend == "bull":
        return 5 if direction == "LONG" else -10
    if btc_trend == "bear":
        return 5 if direction == "SHORT" else -10
    return 0  # neutral / range


def _session_adj(now: Optional[datetime] = None) -> int:
    """时段调整：美股开盘 / 低流动性"""
    if now is None:
        now = _utc_now()
    h = now.hour
    m = now.minute
    total = h * 60 + m

    # 北京时间 21:15~22:30 = UTC 13:15~14:30
    if 13 * 60 + 15 <= total <= 14 * 60 + 30:
        return -10

    # UTC 02:00~06:00 低流动性
    if 2 * 60 <= total <= 6 * 60:
        return -10

    return 0


def market_filter(direction: str,
                  score: int,
                  btc_trend: str = "neutral",
                  funding_rate: float = 0.0,
                  fgi: Optional[int] = None,
                  now: Optional[datetime] = None) -> int:
    """
    统一环境过滤。

    Args:
        direction:    "LONG" | "SHORT"
        score:        当前技术评分
        btc_trend:    "bull" | "bear" | "neutral"
        funding_rate: 资金费率（如 -0.001）
        fgi:          恐慌贪婪指数 0~100，None 表示不检查
        now:          当前时间（UTC），None 则取实时

    Returns:
        调整后评分（BLOCKED=-999 表示本轮禁止开仓）
    """
    # 过滤三：资金费率结算窗口（最高优先级，直接阻断）
    if _is_in_funding_window(funding_rate, now):
        return BLOCKED

    adj = 0

    # 过滤一：BTC 趋势
    adj += _btc_score_adj(direction, btc_trend)

    # 过滤二：时段
    adj += _session_adj(now)

    # 过滤四：恐慌贪婪指数
    if fgi is not None:
        if fgi < 20 and direction == "SHORT":
            return BLOCKED   # 极度恐慌禁做空
        if fgi > 85 and direction == "LONG":
            return BLOCKED   # 极度贪婪禁做多

    return score + adj
