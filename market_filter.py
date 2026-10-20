#!/usr/bin/env python3
"""
market_filter.py — 统一市场环境过滤模块 (P2-3)

所有扫描流程在评分后、挂单前调用此模块对评分进行环境修正。
返回修正后的评分；返回 -999 表示本轮完全禁止开仓。

接口：
    apply(sym, direction, score, funding_pct, fg_index=None) -> float
"""

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# ── 主流币名单（动态搜索范围用）─────────────────────────────────────────────
MAJOR_COINS = {"BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "AVAX", "DOT"}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _beijing_hour_minute() -> tuple:
    """返回 (hour, minute) in Asia/Shanghai (UTC+8)。"""
    now = _utc_now()
    bj_hour   = (now.hour + 8) % 24
    bj_minute = now.minute
    return bj_hour, bj_minute


def _get_btc_trend() -> str:
    """
    获取 BTC 大环境趋势。
    返回 'up' | 'down' | 'neutral'
    """
    try:
        import binance_api as api
        k4h = api.get_klines("BTCUSDT", "4h", 10)
        if len(k4h) < 3:
            return "neutral"
        closes = [float(k[4]) for k in k4h]
        # 简单判断：最近3根4H收盘价方向
        if closes[-1] > closes[-2] > closes[-3]:
            return "up"
        elif closes[-1] < closes[-2] < closes[-3]:
            return "down"
        return "neutral"
    except Exception:
        return "neutral"


def _get_fear_greed() -> int:
    """
    获取恐慌贪婪指数（0~100）。
    优先从 market_context 模块取，失败返回 50（中性）。
    """
    try:
        from market_context import get_context as _get_ctx
        ctx = _get_ctx()
        fg = ctx.get("fear_greed", {})
        if isinstance(fg, dict):
            return int(fg.get("value", 50))
        return int(fg) if fg else 50
    except Exception:
        return 50


# ── 主接口 ────────────────────────────────────────────────────────────────────

def apply(
    sym:         str,
    direction:   str,    # 'LONG' | 'SHORT'
    score:       float,
    funding_pct: float,  # 已单位化为百分比
    fg_index:    int = None,   # 可选：外部传入 FGI，避免重复查询
    btc_trend:   str = None,   # 可选：外部传入 BTC趋势，避免重复查询
) -> float:
    """
    对评分进行环境修正，返回修正后评分。
    -999 = 本轮禁止开仓（资金费率结算窗口等硬性限制）。
    """
    adjusted = score

    # ── 过滤一：BTC 大环境趋势 ────────────────────────────────────────────────
    trend = btc_trend or _get_btc_trend()
    if trend == "up":
        if direction == "LONG":
            adjusted += 5
        else:
            adjusted -= 5
    elif trend == "down":
        if direction == "SHORT":
            adjusted += 5
        else:
            adjusted -= 5

    # ── 过滤二：时段过滤 ──────────────────────────────────────────────────────
    bj_h, bj_m = _beijing_hour_minute()
    utc_h = _utc_now().hour

    # 美股开盘：北京时间 21:15~22:30（高波动，信号噪音大）
    us_open = (bj_h == 21 and bj_m >= 15) or (bj_h == 22 and bj_m <= 30)
    # 低流动性时段：UTC 02:00~06:00
    low_liq = 2 <= utc_h < 6

    if us_open or low_liq:
        adjusted -= 10

    # ── 过滤三：资金费率结算窗口（硬性拦截）────────────────────────────────
    # Binance 合约资金费率每8小时结算一次：00:00 / 08:00 / 16:00 UTC
    # 结算前后各5分钟禁止开仓，避免极端费率冲击
    _utc_snap = _utc_now()
    utc_h      = _utc_snap.hour
    minute     = _utc_snap.minute  # Fix: 明确定义 minute（原来漏写导致 NameError）
    is_extreme = abs(funding_pct) > 0.5
    is_settlement_hour = utc_h in (0, 8, 16)
    pre_block  = 10  # 结算前10分钟
    post_block = 10  # 结算后10分钟

    if is_settlement_hour and minute < post_block:
        return -999.0   # 结算后10分钟内
    if (utc_h + 1) % 24 in (0, 8, 16) and minute >= 60 - pre_block:
        return -999.0   # 结算前10分钟内

    # 极端费率（>0.5%）：保护窗口扩大到前后15分钟
    if is_extreme:
        if is_settlement_hour and minute < 15:
            return -999.0
        if (utc_h + 1) % 24 in (0, 8, 16) and minute >= 60 - 15:
            return -999.0

    # ── 过滤四：恐慌贪婪指数 ──────────────────────────────────────────────────
    # 数据复盘（2026-08-29~09-01）：贪婪情绪(FGI=60-84)下 LONG 胜率仅 26%，
    # 说明 FGI>85 的原门槛过宽，现改为：
    #   FGI > 75 → 禁止做多（贪婪区多单系统性失准）
    #   FGI > 85 → 禁止做多（保留极度贪婪原始逻辑，此分支实际被上面覆盖）
    #   FGI < 20 → 禁止做空（极度恐慌）
    fg = fg_index if fg_index is not None else _get_fear_greed()
    if fg < 20 and direction == "SHORT":
        return -999.0   # 极度恐慌，禁止做空
    if fg > 75 and direction == "LONG":
        return -999.0   # 贪婪区（FGI>75），禁止做多（原85→75，数据驱动收紧）

    return adjusted


def calc_dynamic_range(
    atr: float,
    price: float,
    bb_width_pct: float,
    vol_ratio: float,
    sym: str,
) -> float:
    """
    计算动态搜索范围（用于锚点竞选和入场偏差判断）。

    基础范围 = ATR(14) / 现价 × 100%
    × BB宽度系数 × 量能系数 × 币种系数

    返回：范围百分比（1% ~ 15%）
    """
    if price <= 0:
        return 5.0

    # 基础范围
    base_pct = (atr / price) * 100.0

    # BB宽度系数
    if bb_width_pct < 5:
        bb_coef = 0.8
    elif bb_width_pct > 15:
        bb_coef = 1.3
    else:
        bb_coef = 1.0

    # 量能系数
    if vol_ratio < 0.5:
        vol_coef = 0.9
    elif vol_ratio > 2.0:
        vol_coef = 1.4
    else:
        vol_coef = 1.0

    # 币种系数
    token = sym.replace("USDT", "").replace("PERP", "").replace("1000", "")
    if token in MAJOR_COINS:
        coin_coef = 0.7
    elif len(token) <= 4 and token.isupper():
        # 短符号通常是中等市值
        coin_coef = 1.0
    else:
        coin_coef = 1.3  # 长名字通常是小币/新币

    result = base_pct * bb_coef * vol_coef * coin_coef
    return max(1.0, min(15.0, result))


if __name__ == "__main__":
    print("market_filter 加载正常 ✅")
    # 简单验证动态范围
    r = calc_dynamic_range(atr=0.005, price=0.1, bb_width_pct=8.0, vol_ratio=1.2, sym="DOGEUSDT")
    print(f"DOGE 动态范围示例: {r:.2f}%")
    r2 = calc_dynamic_range(atr=500, price=65000, bb_width_pct=3.0, vol_ratio=0.8, sym="BTCUSDT")
    print(f"BTC  动态范围示例: {r2:.2f}%")
