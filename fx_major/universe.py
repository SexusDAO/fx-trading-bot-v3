"""
fx_major/universe.py — 主流币候选池管理

功能：
  1. 每日通过 Binance /fapi/v1/ticker/24hr 拉取合约交易对
  2. 用 CoinGecko /coins/markets 获取市值排名（无需 key，每分钟30次）
  3. 取市值前 100 且在 Binance 合约市场存在的交易对
  4. 结果缓存 24 小时，失败时降级到白名单

对外接口：
  get_universe() → list[str]   # 返回当前候选池，如 ["BTCUSDT", "ETHUSDT", ...]
  get_market_mode(symbol) → str   # "trend_up" / "trend_down" / "range"
"""

import json
import logging
import math
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("fx_major.universe")

# ── 路径 ──────────────────────────────────────────────────────────────────────
_ROOT       = Path(__file__).parent
_CACHE_FILE = _ROOT / "logs" / "universe_cache.json"

# ── 代理 ──────────────────────────────────────────────────────────────────────
def _opener():
    try:
        import socks, socket as _sock
        p = "REDACTED_PROXY_CREDENTIALS"
        auth, hp = p.split("@"); user, pwd = auth.split(":"); host, port = hp.split(":")
        socks.set_default_proxy(socks.SOCKS5, host, int(port), username=user, password=pwd)
        _sock.socket = socks.socksocket
    except Exception:
        pass
    return urllib.request.build_opener()

def _get(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "fx-major/1.0"})
    with _opener().open(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

# ── 拉取 Binance 合约交易对 ───────────────────────────────────────────────────
def _fetch_binance_futures() -> set:
    """返回 Binance 永续合约 USDT 交易对集合"""
    data = _get("https://fapi.binance.com/fapi/v1/exchangeInfo")
    symbols = set()
    for s in data.get("symbols", []):
        if s.get("contractType") == "PERPETUAL" and s.get("quoteAsset") == "USDT" \
                and s.get("status") == "TRADING":
            symbols.add(s["symbol"])
    return symbols

# ── 拉取 CoinGecko 市值排名 ───────────────────────────────────────────────────
def _fetch_coingecko_top100() -> list[str]:
    """
    返回市值前 100 的 coin symbol（大写），通过 CoinGecko 公开 API。
    分两页拉取，每页 50 条。
    """
    base = "https://api.coingecko.com/api/v3/coins/markets"
    params = "vs_currency=usd&order=market_cap_desc&per_page=50&sparkline=false"
    coins = []
    for page in [1, 2]:
        try:
            data = _get(f"{base}?{params}&page={page}", timeout=20)
            coins.extend(data)
            time.sleep(0.5)  # 限速：免费版 30次/min
        except Exception as e:
            logger.warning(f"[universe] CoinGecko page={page} 失败: {e}")
    return [c["symbol"].upper() for c in coins]

# ── 构建候选池 ────────────────────────────────────────────────────────────────
def _build_universe() -> list[str]:
    """
    取 CoinGecko 市值前100 ∩ Binance 合约市场，按市值排序返回。
    失败时降级到白名单。
    """
    from fx_major.config import UNIVERSE_WHITELIST, UNIVERSE_MIN_VOL_24H

    try:
        # 1. Binance 合约交易对
        futures_set = _fetch_binance_futures()
        logger.info(f"[universe] Binance 合约: {len(futures_set)} 个")

        # 2. CoinGecko 市值前 100
        cg_top100 = _fetch_coingecko_top100()
        logger.info(f"[universe] CoinGecko top100: {len(cg_top100)} 个")

        # 3. 交集：CoinGecko symbol + "USDT" 是否在 Binance 合约里
        result = []
        for sym in cg_top100:
            pair = sym + "USDT"
            if pair in futures_set:
                result.append(pair)

        # 4. 过滤掉稳定币
        stables = {"USDCUSDT", "BUSDUSDT", "TUSDUSDT", "USDTUSDT", "DAIUSDT", "FDUSDUSDT"}
        result  = [s for s in result if s not in stables]

        logger.info(f"[universe] 最终候选池: {len(result)} 个")
        return result

    except Exception as e:
        logger.error(f"[universe] 候选池构建失败，降级到白名单: {e}")
        return list(UNIVERSE_WHITELIST)

# ── 缓存管理 ──────────────────────────────────────────────────────────────────
def _load_cache():
    try:
        if _CACHE_FILE.exists():
            d = json.loads(_CACHE_FILE.read_text())
            return d
    except Exception:
        pass
    return None

def _save_cache(pool: list):
    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_text(json.dumps({
            "pool": pool,
            "ts": time.time(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "count": len(pool),
        }, ensure_ascii=False, indent=2))
    except Exception as e:
        logger.warning(f"[universe] 缓存写入失败: {e}")

# ══════════════════════════════════════════════════════════════════════════════
# 公开接口
# ══════════════════════════════════════════════════════════════════════════════

def get_universe(force_refresh: bool = False) -> list[str]:
    """
    返回当前主流币候选池（市值前100 ∩ Binance 合约）。
    每 24 小时自动刷新，失败时返回缓存或白名单。
    """
    from fx_major.config import UNIVERSE_REFRESH_HOURS, UNIVERSE_WHITELIST
    ttl = UNIVERSE_REFRESH_HOURS * 3600

    if not force_refresh:
        cached = _load_cache()
        if cached and (time.time() - cached.get("ts", 0)) < ttl:
            return cached.get("pool", [])

    pool = _build_universe()
    if pool:
        _save_cache(pool)
        return pool

    # 最终降级
    return list(UNIVERSE_WHITELIST)


# ── 市场模式判断 ──────────────────────────────────────────────────────────────
def _ema(closes: list, n: int) -> float:
    if len(closes) < n: return closes[-1] if closes else 0.0
    k = 2 / (n + 1); e = sum(closes[:n]) / n
    for c in closes[n:]: e = c * k + e * (1 - k)
    return e

def _atr(klines: list, n: int = 14) -> float:
    trs = []
    for i in range(1, len(klines)):
        h, lo, pc = float(klines[i][2]), float(klines[i][3]), float(klines[i-1][4])
        trs.append(max(h - lo, abs(h - pc), abs(lo - pc)))
    return sum(trs[-n:]) / n if trs else 0.0

def get_market_mode(symbol: str, klines_1d: list) -> dict:
    """
    分析单个标的的市场模式。

    Parameters
    ----------
    symbol : str
    klines_1d : list  最近 N 根日线 K 线 [open_time, open, high, low, close, volume, ...]

    Returns
    -------
    dict:
        {
          "mode": "trend_up" | "trend_down" | "range",
          "atr_1d": float,
          "atr_pct": float,       # ATR / 当前价格
          "ema9": float,
          "ema20": float,
          "ema50": float,
          "day_high": float,      # 当日最高
          "day_low": float,       # 当日最低
          "range_pct": float,     # 当日区间宽度 / 价格
        }
    """
    from fx_major.config import (
        TREND_CONFIRM_DAYS, TREND_ATR_EXPAND_RATIO,
        RANGE_ATR_SHRINK_RATIO, RANGE_MAX_BODY_RATIO,
        TREND_EMA_FAST, TREND_EMA_MID, TREND_EMA_SLOW,
    )

    if len(klines_1d) < 10:
        return {"mode": "range", "atr_1d": 0, "atr_pct": 0,
                "ema9": 0, "ema20": 0, "ema50": 0,
                "day_high": 0, "day_low": 0, "range_pct": 0}

    closes  = [float(k[4]) for k in klines_1d]
    opens   = [float(k[1]) for k in klines_1d]
    highs   = [float(k[2]) for k in klines_1d]
    lows    = [float(k[3]) for k in klines_1d]
    price   = closes[-1]

    atr_now = _atr(klines_1d, 14)
    # ATR 5日均值（用于判断扩张/收缩）
    atr_5d  = sum(_atr(klines_1d[:max(i, 15)], 14) for i in range(-5, 0)) / 5
    atr_pct = atr_now / price if price > 0 else 0

    ema9  = _ema(closes, TREND_EMA_FAST)
    ema20 = _ema(closes, TREND_EMA_MID)
    ema50 = _ema(closes, TREND_EMA_SLOW)

    # 当日高低（用最新日线）
    day_high  = highs[-1]
    day_low   = lows[-1]
    range_pct = (day_high - day_low) / price if price > 0 else 0

    # ── 趋势判断 ──────────────────────────────────────────────────────────────
    # 条件1：最近 TREND_CONFIRM_DAYS 根日线同向
    recent = list(zip(opens[-TREND_CONFIRM_DAYS:], closes[-TREND_CONFIRM_DAYS:]))
    all_up   = all(c > o for o, c in recent)
    all_down = all(c < o for o, c in recent)

    # 条件2：ATR 扩张（相较5日均值）
    atr_expanding = (atr_now > atr_5d * TREND_ATR_EXPAND_RATIO) if atr_5d > 0 else False

    # 条件3：EMA 排列
    ema_bull = ema9 > ema20 > ema50 and price > ema20
    ema_bear = ema9 < ema20 < ema50 and price < ema20

    if all_up and (atr_expanding or ema_bull):
        mode = "trend_up"
    elif all_down and (atr_expanding or ema_bear):
        mode = "trend_down"
    else:
        mode = "range"

    return {
        "mode":      mode,
        "atr_1d":    round(atr_now, 4),
        "atr_pct":   round(atr_pct * 100, 2),
        "ema9":      round(ema9, 4),
        "ema20":     round(ema20, 4),
        "ema50":     round(ema50, 4),
        "day_high":  day_high,
        "day_low":   day_low,
        "range_pct": round(range_pct * 100, 2),
    }
