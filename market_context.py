"""
market_context.py — 市场整体情绪与 BTC 趋势判断
缓存 15 分钟，避免频繁调用外部 API。

使用示例:
    import market_context
    ctx = market_context.get_context()
    print(ctx)
"""

import time
import requests

from binance_api import get_klines_proxy, get_mark_price, get_ticker_24h, PROXIES

# ─── 内存缓存 ─────────────────────────────────────────────────────────────────
_CACHE: dict = {}
_CACHE_TTL: int = 15 * 60  # 15 分钟


def _is_cache_valid() -> bool:
    """检查缓存是否仍在 15 分钟有效期内"""
    return (
        "data" in _CACHE
        and "ts" in _CACHE
        and (time.time() - _CACHE["ts"]) < _CACHE_TTL
    )


# ─── EMA 计算 ─────────────────────────────────────────────────────────────────
def _ema(prices: list, period: int) -> list:
    """计算 EMA，返回与 prices 等长的列表（前 period-1 个值为 None）"""
    if len(prices) < period:
        return [None] * len(prices)
    k = 2.0 / (period + 1)
    result = [None] * (period - 1)
    ema_val = sum(prices[:period]) / period  # 初始 SMA 作为种子
    result.append(ema_val)
    for price in prices[period:]:
        ema_val = price * k + ema_val * (1 - k)
        result.append(ema_val)
    return result


# ─── BTC 趋势判断 ─────────────────────────────────────────────────────────────
def get_btc_trend() -> str:
    """
    基于 BTC/USDT 4H EMA9/20/50 排列判断趋势。
    返回:
        "bull"    — EMA9 > EMA20 > EMA50（多头排列）
        "bear"    — EMA9 < EMA20 < EMA50（空头排列）
        "neutral" — 其他情况
    """
    try:
        klines = get_klines_proxy("BTCUSDT", "4h", limit=60)
        if not klines or len(klines) < 50:
            return "neutral"

        closes = [float(k[4]) for k in klines]  # k[4] = 收盘价

        ema9  = _ema(closes, 9)
        ema20 = _ema(closes, 20)
        ema50 = _ema(closes, 50)

        # 取最新一根完整 K 线（倒数第 2 根，最后一根可能尚未收盘）
        idx = -2
        e9  = ema9[idx]
        e20 = ema20[idx]
        e50 = ema50[idx]

        if e9 is None or e20 is None or e50 is None:
            return "neutral"

        if e9 > e20 > e50:
            return "bull"
        elif e9 < e20 < e50:
            return "bear"
        else:
            return "neutral"

    except Exception:
        return "neutral"


# ─── 恐慌贪婪指数 ─────────────────────────────────────────────────────────────
def get_fear_greed() -> dict | None:
    """
    从 alternative.me 获取恐慌贪婪指数。
    返回: {"value": int, "label": str}  或  None（请求失败时）
    """
    try:
        resp = requests.get(
            "https://api.alternative.me/fng/?limit=1",
            proxies=PROXIES,
            timeout=8,
        )
        resp.raise_for_status()
        data = resp.json()
        item = data["data"][0]
        return {
            "value": int(item["value"]),
            "label": item["value_classification"],
        }
    except Exception:
        return None


# ─── 综合市场情绪 ─────────────────────────────────────────────────────────────
def get_market_sentiment(btc_trend: str, fear_greed: dict | None) -> str:
    """
    综合 BTC 趋势 + 恐慌贪婪指数，返回五档情绪。
    返回值: "strong_bull" / "bull" / "neutral" / "bear" / "strong_bear"

    逻辑：
      - strong_bull: BTC 多头排列 且 贪婪（FGI >= 60）
      - bull:        BTC 多头排列，或 FGI >= 60
      - strong_bear: BTC 空头排列 且 恐慌（FGI <= 30）
      - bear:        BTC 空头排列，或 FGI <= 30
      - neutral:     其余情况
    """
    fgi = fear_greed["value"] if fear_greed else None

    if btc_trend == "bull" and fgi is not None and fgi >= 60:
        return "strong_bull"
    if btc_trend == "bear" and fgi is not None and fgi <= 30:
        return "strong_bear"
    if btc_trend == "bull" or (fgi is not None and fgi >= 60):
        return "bull"
    if btc_trend == "bear" or (fgi is not None and fgi <= 30):
        return "bear"
    return "neutral"


# ─── BTC 价格 & 24h 涨跌幅 ───────────────────────────────────────────────────
def _get_btc_price_and_chg() -> tuple[float, float]:
    """
    获取 BTC 当前标记价格 和 24h 涨跌幅百分比。
    返回 (btc_price, btc_24h_chg_pct)
    """
    try:
        ticker = get_ticker_24h("BTCUSDT")
        price = float(ticker.get("lastPrice", 0) or ticker.get("markPrice", 0))
        chg = float(ticker.get("priceChangePercent", 0))
        return price, chg
    except Exception:
        pass

    # 备用：使用 mark price
    try:
        mp = get_mark_price("BTCUSDT")
        price = float(mp.get("markPrice", 0))
        return price, 0.0
    except Exception:
        return 0.0, 0.0


# ─── 主接口 ───────────────────────────────────────────────────────────────────
def get_context(force: bool = False) -> dict:
    """
    返回完整市场上下文，缓存 15 分钟。
    force=True 时强制刷新缓存。

    返回格式:
    {
        "btc_price":    float,   # BTC 当前价格（USDT）
        "btc_trend":    str,     # "bull" / "bear" / "neutral"
        "fear_greed":   dict,    # {"value": int, "label": str} 或 None
        "sentiment":    str,     # "strong_bull"/"bull"/"neutral"/"bear"/"strong_bear"
        "btc_24h_chg":  float,   # 24h 涨跌幅（百分比，如 2.35 表示 +2.35%）
    }
    """
    if not force and _is_cache_valid():
        return _CACHE["data"]

    btc_trend   = get_btc_trend()
    fear_greed  = get_fear_greed()
    sentiment   = get_market_sentiment(btc_trend, fear_greed)
    btc_price, btc_24h_chg = _get_btc_price_and_chg()

    result = {
        "btc_price":   btc_price,
        "btc_trend":   btc_trend,
        "fear_greed":  fear_greed,
        "sentiment":   sentiment,
        "btc_24h_chg": btc_24h_chg,
    }

    _CACHE["data"] = result
    _CACHE["ts"]   = time.time()
    return result


# ─── 直接运行时打印结果 ───────────────────────────────────────────────────────
if __name__ == "__main__":
    import json
    ctx = get_context()
    print(json.dumps(ctx, ensure_ascii=False, indent=2))
