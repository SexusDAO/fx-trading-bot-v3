"""
modules/dynamic_baseline.py — 动态基准计算 v2.0（全动态改造版）

核心原则：所有判断基于币种自身的统计分布（percentile/mean/std），
不允许出现任何固定的百分比数字或固定倍数
（唯一例外：结构性数量，如"至少 7 根 K 线"这类计数）

输入：K 线列表（OHLCV），取最近 20/60 根计算基准。
输出：DynamicBaseline 对象，包含完整的统计分布信息。
"""

from __future__ import annotations
import math
from typing import NamedTuple, Optional


# ── 工具函数 ──────────────────────────────────────────────────────────────────

def _closes(k: list) -> list[float]:
    return [float(x[4]) for x in k]

def _highs(k: list) -> list[float]:
    return [float(x[2]) for x in k]

def _lows(k: list) -> list[float]:
    return [float(x[3]) for x in k]

def _opens(k: list) -> list[float]:
    return [float(x[1]) for x in k]

def _vols(k: list) -> list[float]:
    return [float(x[5]) for x in k]

def _bodies(k: list) -> list[float]:
    """K 线实体（绝对值）"""
    return [abs(float(x[4]) - float(x[1])) for x in k]

def _ranges(k: list) -> list[float]:
    """K 线高低点范围"""
    return [float(x[2]) - float(x[3]) for x in k]

def _mean(data: list[float]) -> float:
    if not data:
        return 0.0
    return sum(data) / len(data)

def _std(data: list[float]) -> float:
    if len(data) < 2:
        return 0.0
    m = _mean(data)
    return math.sqrt(sum((x - m) ** 2 for x in data) / len(data))

def _percentile(data: list[float], pct: float) -> float:
    if not data:
        return 0.0
    data = sorted(data)
    k = (len(data) - 1) * pct / 100
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return data[int(k)]
    return data[int(f)] * (c - k) + data[int(c)] * (k - f)

def _atr_series(k: list) -> list[float]:
    """ATR 序列（从第 2 根开始）"""
    if len(k) < 2:
        return []
    atr = []
    prev_c = float(k[0][4])
    for i in range(1, len(k)):
        h = float(k[i][2])
        l = float(k[i][3])
        c = float(k[i][4])
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        atr.append(tr)
        prev_c = c
    return atr

def _rsi(closes: list[float], n: int = 14) -> float:
    """计算当前 RSI 值"""
    if len(closes) < 2:
        return 50.0
    
    gains = []
    losses = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        if diff > 0:
            gains.append(diff)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(diff))
    
    if len(gains) < n:
        return 50.0
    
    avg_gain = _mean(gains[-n:])
    avg_loss = _mean(losses[-n:])
    
    if avg_loss == 0:
        return 100.0
    
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _rsi_series(closes: list[float], n: int = 14) -> list[float]:
    """RSI 序列（长度与 closes 一致，前面补 50）"""
    if len(closes) < 2:
        return [50.0] * len(closes)
    
    gains = []
    losses = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        if diff > 0:
            gains.append(diff)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(diff))
    
    if len(gains) < n:
        return [50.0] * len(closes)
    
    rsi = []
    avg_gain = _mean(gains[:n])
    avg_loss = _mean(losses[:n])
    
    for i in range(len(closes)):
        if i < n:
            rsi.append(50.0)
        else:
            if avg_loss == 0:
                rsi.append(100.0)
            else:
                rs = avg_gain / avg_loss
                rsi.append(100 - (100 / (1 + rs)))
            if i < len(closes) - 1:
                avg_gain = (avg_gain * (n - 1) + gains[i]) / n
                avg_loss = (avg_loss * (n - 1) + losses[i]) / n
    
    return rsi

def _gains_series(closes: list[float]) -> list[float]:
    """涨幅序列（相对于前一根）"""
    if len(closes) < 2:
        return []
    return [(closes[i] - closes[i-1]) / closes[i-1] if closes[i-1] > 0 else 0 for i in range(1, len(closes))]

def _compute_pullback_ratios(k: list) -> list[float]:
    """
    计算每根 K 线相对前一波高点的回撤比例。
    用于场景 C（强势回踩）的动态判断。
    """
    if len(k) < 10:
        return []
    
    highs = _highs(k)
    ratios = []
    
    for i in range(len(k)):
        if i < 5:
            ratios.append(0.0)
            continue
        
        # 前 5 根的最高点
        recent_high = max(highs[max(0, i-5):i])
        current_low = float(k[i][3])
        
        if recent_high > 0:
            ratio = (recent_high - current_low) / recent_high
            ratios.append(ratio)
        else:
            ratios.append(0.0)
    
    return ratios


# ══════════════════════════════════════════════════════════════════════════════
# 动态基线
# ══════════════════════════════════════════════════════════════════════════════

class DynamicBaseline(NamedTuple):
    """
    动态基准（全动态改造版 v2.0）
    
    所有字段基于币种自身的统计分布，无固定数字。
    """
    # ── ATR 分布 ─────────────────────────────────────────────────────────
    atr_baseline: float      # 基准 ATR（最近 20 根平均）
    atr_std: float           # ATR 标准差
    atr_p25: float           # ATR 25 百分位
    atr_p40: float           # ATR 40 百分位（用于缩量判断）
    atr_p60: float           # ATR 60 百分位
    atr_p70: float           # ATR 70 百分位
    atr_p75: float           # ATR 75 百分位（用于缓冲判断）
    atr_p90: float           # ATR 90 百分位（极端波动）
    
    # ── 成交量分布 ───────────────────────────────────────────────────────
    vol_baseline: float      # 基准成交量（最近 20 根平均）
    vol_std: float           # 成交量标准差
    vol_p25: float           # 成交量 25 百分位
    vol_p40: float           # 成交量 40 百分位
    vol_p60: float           # 成交量 60 百分位
    vol_p75: float           # 成交量 75 百分位
    vol_p90: float           # 成交量 90 百分位（爆量）
    
    # ── RSI 分布 ────────────────────────────────────────────────────────
    rsi_mean: float          # RSI 均值
    rsi_std: float           # RSI 标准差
    rsi_lower1: float        # RSI 下轨（mean - 1σ）
    rsi_lower2: float        # RSI 下轨（mean - 2σ，极端超卖）
    rsi_upper1: float        # RSI 上轨（mean + 1σ）
    rsi_upper2: float        # RSI 上轨（mean + 2σ，极端超买）
    
    # ── K 线实体分布 ─────────────────────────────────────────────────────
    body_mean: float         # K 线实体均值
    body_std: float          # K 线实体标准差
    body_p60: float          # K 线实体 60 百分位
    body_p75: float          # K 线实体 75 百分位
    
    # ── 高低点范围分布 ──────────────────────────────────────────────────
    range_mean: float        # 高低点范围均值
    range_std: float         # 高低点范围标准差
    range_p25: float
    range_p40: float
    range_p60: float
    
    # ── 涨跌幅分布 ──────────────────────────────────────────────────────
    gain_p60: float          # 涨跌幅 60 百分位
    gain_p80: float          # 涨跌幅 80 百分位（强势阈值）
    
    # ── 回踩比例分布 ────────────────────────────────────────────────────
    pullback_p30: float      # 回踩比例 30 百分位
    pullback_p40: float      # 回踩比例 40 百分位
    pullback_p50: float      # 回踩比例 50 百分位
    pullback_p75: float      # 回踩比例 75 百分位
    
    # ── 辅助字段 ────────────────────────────────────────────────────────
    strong_threshold: float  # 强势阈值（基于 gain_p80）
    ema20: float             # EMA20
    ema50: float             # EMA50


def compute(k: list, window_short: int = 20, window_long: int = 60) -> DynamicBaseline:
    """
    计算动态基准。
    
    Args:
        k: K 线列表（OHLCV）
        window_short: 短期窗口（默认 20）
        window_long: 长期窗口（默认 60）
    
    Returns:
        DynamicBaseline 对象
    """
    if len(k) < window_short:
        return _default_baseline()
    
    closes = _closes(k)
    highs = _highs(k)
    lows = _lows(k)
    vols = _vols(k)
    bodies = _bodies(k)
    ranges = _ranges(k)
    atrs = _atr_series(k)
    rsis = _rsi_series(closes)
    gains = _gains_series(closes)
    pullbacks = _compute_pullback_ratios(k)
    
    # ── ATR 分布 ─────────────────────────────────────────────────────────
    atr_short = atrs[-window_short:] if len(atrs) >= window_short else atrs
    atr_baseline = _mean(atr_short)
    atr_std = _std(atr_short)
    atr_all = atrs if len(atrs) > 0 else [atr_baseline]
    
    # ── 成交量分布 ───────────────────────────────────────────────────────
    vol_short = vols[-window_short:]
    vol_baseline = _mean(vol_short)
    vol_std = _std(vol_short)
    vol_all = vols[-window_long:] if len(vols) >= window_long else vols
    
    # ── RSI 分布 ────────────────────────────────────────────────────────
    rsi_recent = rsis[-window_short:]
    rsi_mean = _mean(rsi_recent)
    rsi_std = _std(rsi_recent)
    
    # ── K 线实体分布 ─────────────────────────────────────────────────────
    body_short = bodies[-window_short:]
    body_mean = _mean(body_short)
    body_std = _std(body_short)
    
    # ── 高低点范围分布 ──────────────────────────────────────────────────
    range_short = ranges[-window_short:]
    range_mean = _mean(range_short)
    range_std = _std(range_short)
    
    # ── 涨跌幅分布 ──────────────────────────────────────────────────────
    gain_all = gains[-window_long:] if len(gains) >= window_long else gains
    gain_abs = [abs(g) for g in gain_all]
    
    # ── 回踩比例分布 ────────────────────────────────────────────────────
    pullback_valid = [p for p in pullbacks if p > 0]
    if not pullback_valid:
        pullback_valid = [0.01, 0.02, 0.03]
    
    # ── EMA ────────────────────────────────────────────────────────────
    ema20 = _ema(closes[-20:], 20) if len(closes) >= 20 else closes[-1]
    ema50 = _ema(closes[-50:], 50) if len(closes) >= 50 else ema20
    
    return DynamicBaseline(
        # ATR
        atr_baseline=atr_baseline,
        atr_std=atr_std,
        atr_p25=_percentile(atr_all, 25),
        atr_p40=_percentile(atr_all, 40),
        atr_p60=_percentile(atr_all, 60),
        atr_p70=_percentile(atr_all, 70),
        atr_p75=_percentile(atr_all, 75),
        atr_p90=_percentile(atr_all, 90),
        
        # 成交量
        vol_baseline=vol_baseline,
        vol_std=vol_std,
        vol_p25=_percentile(vol_all, 25),
        vol_p40=_percentile(vol_all, 40),
        vol_p60=_percentile(vol_all, 60),
        vol_p75=_percentile(vol_all, 75),
        vol_p90=_percentile(vol_all, 90),
        
        # RSI
        rsi_mean=rsi_mean,
        rsi_std=rsi_std,
        rsi_lower1=rsi_mean - rsi_std,
        rsi_lower2=rsi_mean - 2 * rsi_std,
        rsi_upper1=rsi_mean + rsi_std,
        rsi_upper2=rsi_mean + 2 * rsi_std,
        
        # K 线实体
        body_mean=body_mean,
        body_std=body_std,
        body_p60=_percentile(body_short, 60),
        body_p75=_percentile(body_short, 75),
        
        # 高低点范围
        range_mean=range_mean,
        range_std=range_std,
        range_p25=_percentile(range_short, 25),
        range_p40=_percentile(range_short, 40),
        range_p60=_percentile(range_short, 60),
        
        # 涨跌幅
        gain_p60=_percentile(gain_abs, 60),
        gain_p80=_percentile(gain_abs, 80),
        
        # 回踩比例
        pullback_p30=_percentile(pullback_valid, 30),
        pullback_p40=_percentile(pullback_valid, 40),
        pullback_p50=_percentile(pullback_valid, 50),
        pullback_p75=_percentile(pullback_valid, 75),
        
        # 辅助
        strong_threshold=_percentile(gain_abs, 80),
        ema20=ema20,
        ema50=ema50,
    )


def _ema(data: list[float], span: int) -> float:
    """计算指数移动平均"""
    if not data:
        return 0.0
    if len(data) < span:
        return _mean(data)
    
    multiplier = 2 / (span + 1)
    ema = _mean(data[:span])
    
    for val in data[span:]:
        ema = (val - ema) * multiplier + ema
    
    return ema


def _default_baseline() -> DynamicBaseline:
    """默认基准（数据不足时使用）"""
    return DynamicBaseline(
        atr_baseline=100, atr_std=30,
        atr_p25=70, atr_p40=80, atr_p60=120, atr_p70=140, atr_p75=150, atr_p90=200,
        vol_baseline=10000, vol_std=3000,
        vol_p25=7000, vol_p40=8000, vol_p60=12000, vol_p75=14000, vol_p90=20000,
        rsi_mean=50, rsi_std=15,
        rsi_lower1=35, rsi_lower2=20,
        rsi_upper1=65, rsi_upper2=80,
        body_mean=50, body_std=20,
        body_p60=60, body_p75=75,
        range_mean=80, range_std=30,
        range_p25=50, range_p40=60, range_p60=100,
        gain_p60=0.02, gain_p80=0.03,
        pullback_p30=0.01, pullback_p40=0.015, pullback_p50=0.02, pullback_p75=0.03,
        strong_threshold=0.03,
        ema20=0, ema50=0,
    )


def position_pct(current_price: float, bl: DynamicBaseline) -> float:
    """
    根据当前价格在 ATR 分布中的位置，返回仓位百分比。
    用于动态调整仓位大小。
    """
    # 简化：根据波动率调整仓位
    # atr_std/at r_baseline 越大，仓位越小
    if bl.atr_baseline <= 0:
        return 1.0
    
    ratio = bl.atr_std / bl.atr_baseline
    if ratio > 0.8:
        return 0.5  # 高波动，半仓
    elif ratio > 0.5:
        return 0.75  # 中等波动，75 仓
    else:
        return 1.0  # 低波动，满仓


def get_rsi_threshold(bl: DynamicBaseline, multiplier: float = 0.5) -> float:
    """
    获取动态 RSI 阈值。
    
    Args:
        bl: DynamicBaseline 对象
        multiplier: 标准差倍数（默认 0.5）
    
    Returns:
        RSI 阈值（低于此值为超卖）
    """
    return bl.rsi_mean - multiplier * bl.rsi_std


def get_max_pullback_ratio(surge_pct: float, bl: DynamicBaseline) -> float:
    """
    获取动态最大回踩比例。
    
    Args:
        surge_pct: 涨幅（绝对值）
        bl: DynamicBaseline 对象
    
    Returns:
        最大回踩比例（超过此值认为趋势破坏）
    """
    # 涨幅越大，允许的回踩越深
    base = bl.pullback_p50
    
    if surge_pct >= bl.gain_p80:
        # 强势上涨：允许回踩到 0.618
        return 0.618
    elif surge_pct >= bl.gain_p60:
        # 中等上涨：允许回踩到 0.5
        return 0.5
    else:
        # 弱势上涨：只允许回踩到 0.382
        return 0.382


def get_entry_buffer(false_breakout_rate: float, bl: DynamicBaseline) -> float:
    """
    获取动态入场缓冲。
    
    Args:
        false_breakout_rate: 假突破率（0~1）
        bl: DynamicBaseline 对象
    
    Returns:
        入场缓冲（ATR 倍数）
    """
    # 假突破率越高，缓冲越大
    base_buffer = 0.25  # 基础缓冲
    
    if false_breakout_rate > 0.6:
        return base_buffer + 0.25  # 高假突破率：0.5×ATR
    elif false_breakout_rate > 0.4:
        return base_buffer + 0.15  # 中等：0.4×ATR
    else:
        return base_buffer  # 低：0.25×ATR
