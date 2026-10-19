"""
shared/opt_v388.py — v3.88 清算区「相对强度」：用 24H 成交额做分母，替代按固定金额衡量清算区。

同样 500 万 U 的清算区，对日成交 30 亿 U 的 BTC 是噪声，对日成交 500 万 U 的山寨币却能推动整个市场。
相对强度 = 清算区金额 / 24H 成交额（USDT 计）。
"""
from __future__ import annotations


def liq_relative_strength(liq_amount: float, vol24_usd: float) -> float | None:
    """返回相对强度；金额或成交额缺失 / 非正时返回 None（调用方应回退到原逻辑）。"""
    try:
        a = float(liq_amount or 0)
        v = float(vol24_usd or 0)
    except (TypeError, ValueError):
        return None
    if a <= 0 or v <= 0:
        return None
    return a / v


def liq_weight(rel: float | None, ref_ratio: float = 0.10,
               w_min: float = 0.5, w_max: float = 3.0, base: float = 2.0) -> float:
    """清算区锚点权重 = base × (相对强度 / 参考相对强度)，截断到 [w_min, w_max]；相对强度未知时返回 base（原权重）。"""
    if rel is None or ref_ratio <= 0:
        return base
    return max(w_min, min(w_max, base * rel / ref_ratio))


def tp1_buffer_atr(rel: float | None, strong: float = 0.20, mid: float = 0.05,
                   buf_strong: float = 0.20, buf_mid: float = 0.35, buf_weak: float = 0.50,
                   legacy: float = 0.30) -> float:
    """TP1 距清算区前沿的缓冲（ATR 倍数）。相对强度越大，价格越可能打到甚至穿过清算区，缓冲越小。未知时返回原值。"""
    if rel is None:
        return legacy
    if rel >= strong:
        return buf_strong
    if rel >= mid:
        return buf_mid
    return buf_weak
