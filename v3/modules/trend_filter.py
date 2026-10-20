"""
modules/trend_filter.py — 趋势过滤 v2.0（修复版）

修复内容：
1. market_bias 判断改为 4H EMA50 斜率（不用短期交叉）
2. 需要 BTC 和 ETH 的 4H EMA50 斜率同时满足才切换方向
3. 趋势过滤改为动态门槛，不是二元拒绝
"""

from __future__ import annotations
from typing import Optional, Literal, Tuple

TrendBias = Literal["BULLISH", "BEARISH", "NEUTRAL"]


def calc_ema(prices: list[float], period: int) -> float:
    """计算 EMA"""
    if len(prices) < period:
        return prices[-1] if prices else 0.0
    
    multiplier = 2 / (period + 1)
    ema = sum(prices[:period]) / period  # 初始 SMA
    
    for price in prices[period:]:
        ema = (price - ema) * multiplier + ema
    
    return ema


def calc_ema50_slope(closes: list[float], lookback: int = 20) -> float:
    """
    计算 EMA50 斜率
    
    Args:
        closes: 收盘价列表（从旧到新）
        lookback: 回看 K 线数（用于计算斜率）
    
    Returns:
        斜率（百分比变化）
    """
    if len(closes) < 50 + lookback:
        return 0.0
    
    # 计算当前 EMA50 和 lookback 根之前的 EMA50
    ema50_now = calc_ema(closes[-50:], 50)
    ema50_prev = calc_ema(closes[-(50+lookback):-lookback], 50)
    
    if ema50_prev == 0:
        return 0.0
    
    # 斜率 = 百分比变化
    slope = (ema50_now - ema50_prev) / ema50_prev
    
    return slope


def judge_market_bias(
    btc_closes_4h: list[float],
    eth_closes_4h: list[float],
    slope_threshold: float = 0.005  # 0.5% 斜率阈值
) -> TrendBias:
    """
    判断市场趋势方向
    
    规则：
    1. 使用 4H EMA50 斜率判断
    2. 需要 BTC 和 ETH 同时满足才切换方向
    3. 大多数震荡情况返回 NEUTRAL
    
    Args:
        btc_closes_4h: BTC 4H 收盘价
        eth_closes_4h: ETH 4H 收盘价
        slope_threshold: 斜率阈值（默认 0.5%）
    
    Returns:
        趋势方向：BULLISH | BEARISH | NEUTRAL
    """
    if len(btc_closes_4h) < 70 or len(eth_closes_4h) < 70:
        return "NEUTRAL"
    
    btc_slope = calc_ema50_slope(btc_closes_4h, lookback=20)
    eth_slope = calc_ema50_slope(eth_closes_4h, lookback=20)
    
    # 两者同时向上才判定为 BULLISH
    if btc_slope > slope_threshold and eth_slope > slope_threshold:
        return "BULLISH"
    
    # 两者同时向下才判定为 BEARISH
    if btc_slope < -slope_threshold and eth_slope < -slope_threshold:
        return "BEARISH"
    
    # 其他情况都是 NEUTRAL（震荡）
    return "NEUTRAL"


def get_required_score(
    signal_direction: str,
    trend_bias: TrendBias,
    base_threshold: int = 70,
    trend_penalty: int = 12
) -> int:
    """
    计算信号所需的最低分数（动态门槛）
    
    规则：
    - NEUTRAL：双向都是 base_threshold（70）
    - BULLISH：多头 base_threshold，空头 base_threshold + trend_penalty（82）
    - BEARISH：空头 base_threshold，多头 base_threshold + trend_penalty（82）
    
    Args:
        signal_direction: 信号方向 "LONG" 或 "SHORT"
        trend_bias: 市场趋势 "BULLISH" | "BEARISH" | "NEUTRAL"
        base_threshold: 基础门槛（默认 70）
        trend_penalty: 逆势惩罚（默认 12）
    
    Returns:
        所需最低分数
    """
    if trend_bias == "NEUTRAL":
        return base_threshold
    
    if signal_direction == "LONG":
        if trend_bias == "BULLISH":
            return base_threshold  # 顺势，不加分
        else:  # BEARISH
            return base_threshold + trend_penalty  # 逆势，+12 分
    
    elif signal_direction == "SHORT":
        if trend_bias == "BEARISH":
            return base_threshold  # 顺势，不加分
        else:  # BULLISH
            return base_threshold + trend_penalty  # 逆势，+12 分
    
    return base_threshold


def check_trend_filter(
    signal_direction: str,
    second_score: int,
    trend_bias: TrendBias,
    base_threshold: int = 70,
    trend_penalty: int = 12
) -> tuple[bool, int, str]:
    """
    检查信号是否通过趋势过滤（动态门槛版）
    
    Args:
        signal_direction: 信号方向 "LONG" 或 "SHORT"
        second_score: 二次打分
        trend_bias: 市场趋势 "BULLISH" | "BEARISH" | "NEUTRAL"
        base_threshold: 基础门槛（默认 70）
        trend_penalty: 逆势惩罚（默认 12）
    
    Returns:
        (是否通过，所需分数，原因)
    """
    required_score = get_required_score(signal_direction, trend_bias, base_threshold, trend_penalty)
    
    if second_score >= required_score:
        if trend_bias == "NEUTRAL":
            return True, required_score, "neutral_trend"
        elif (signal_direction == "LONG" and trend_bias == "BULLISH") or \
             (signal_direction == "SHORT" and trend_bias == "BEARISH"):
            return True, required_score, "trend_aligned"
        else:
            return True, required_score, "counter_trend_high_score"
    else:
        if trend_bias == "NEUTRAL":
            return False, required_score, f"neutral_trend_score_{second_score}_lt_{required_score}"
        elif (signal_direction == "LONG" and trend_bias == "BULLISH") or \
             (signal_direction == "SHORT" and trend_bias == "BEARISH"):
            return False, required_score, f"trend_aligned_score_{second_score}_lt_{required_score}"
        else:
            return False, required_score, f"counter_trend_score_{second_score}_lt_{required_score}"


def get_trend_strength(
    btc_slope: float,
    eth_slope: float
) -> float:
    """
    计算趋势强度（BTC 和 ETH 斜率平均值）
    
    Args:
        btc_slope: BTC EMA50 斜率
        eth_slope: ETH EMA50 斜率
    
    Returns:
        趋势强度（绝对值）
    """
    return (abs(btc_slope) + abs(eth_slope)) / 2


def judge_market_bias_stable(
    symbol: str,
    btc_closes_4h: list[float],
    eth_closes_4h: list[float] = None,
    own_closes_4h: list[float] = None,
    slope_threshold: float = 0.005
) -> TrendBias:
    """
    判断市场趋势方向（修复 2：区分 BTC 和其他币）
    
    规则：
    - BTC：用自身 EMA50 斜率判断
    - 其他币：BTC+ETH 双确认逻辑
    
    Args:
        symbol: 币种符号（如 "BTCUSDT", "ETHUSDT"）
        btc_closes_4h: BTC 4H 收盘价
        eth_closes_4h: ETH 4H 收盘价
        own_closes_4h: 自身 4H 收盘价（非 BTC 时使用）
        slope_threshold: 斜率阈值（默认 0.5%）
    
    Returns:
        趋势方向：BULLISH | BEARISH | NEUTRAL
    """
    # BTC 直接用自身斜率判断
    if symbol == "BTCUSDT":
        if len(btc_closes_4h) < 70:
            return "NEUTRAL"
        
        btc_slope = calc_ema50_slope(btc_closes_4h, lookback=20)
        
        if btc_slope > slope_threshold:
            return "BULLISH"
        elif btc_slope < -slope_threshold:
            return "BEARISH"
        else:
            return "NEUTRAL"
    
    # 其他币用 BTC+ETH 双确认
    if eth_closes_4h is None or len(eth_closes_4h) < 70:
        return "NEUTRAL"
    if len(btc_closes_4h) < 70:
        return "NEUTRAL"
    
    btc_slope = calc_ema50_slope(btc_closes_4h, lookback=20)
    eth_slope = calc_ema50_slope(eth_closes_4h, lookback=20)
    
    # 两者同时向上才判定为 BULLISH
    if btc_slope > slope_threshold and eth_slope > slope_threshold:
        return "BULLISH"
    
    # 两者同时向下才判定为 BEARISH
    if btc_slope < -slope_threshold and eth_slope < -slope_threshold:
        return "BEARISH"
    
    # 其他情况都是 NEUTRAL（震荡）
    return "NEUTRAL"
