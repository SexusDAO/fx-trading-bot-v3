# ============================================================
# market_utils.py — 市值查询 + 高市值趋势过滤
# ============================================================
import time, requests

from config import (
    FOMO_API_KEY, FOMO_API_KEYS,
    HIGH_MC_FILTER_THRESHOLD,
    WHALE_MC_RATIO_TABLE,
    WHALE_C_THRESHOLD_USD, WHALE_B_THRESHOLD_USD, WHALE_A_THRESHOLD_USD,
    WHALE_SELL_WARN_USD, WHALE_SELL_SOFT_USD, WHALE_SELL_HARD_USD,
    WHALE_MIN_USD,
)

_log_fn = None

def init_market_utils(log_fn):
    global _log_fn
    _log_fn = log_fn

def _log(msg):
    if _log_fn:
        _log_fn(msg)


# ── 鲸鱼判断：双重门槛（绝对金额 + MC 占比）────────────────────────────────

def is_whale(amount_usd: float, current_mc: float) -> bool:
    """
    判断一笔交易是否达到鲸鱼门槛。
    分层条件：低 MC 时绝对金额门槛降低，占比门槛略高。
    current_mc <= 0 时仅用绝对金额判断（防止无MC数据时全部漏报）。
    """
    if current_mc <= 0:
        # 无 MC 数据：用最低档门槛 ($2K)，防止新币 MC=0 时全部漏报
        return amount_usd >= WHALE_MIN_USD
    for mc_cap, min_usd, ratio in WHALE_MC_RATIO_TABLE:
        if current_mc < mc_cap:
            return amount_usd >= min_usd and (amount_usd / current_mc) >= ratio
    # 超过所有档位：用最后一档
    _last = WHALE_MC_RATIO_TABLE[-1]
    return amount_usd >= _last[1] and (amount_usd / current_mc) >= _last[2]


def get_whale_grade(amount_usd: float, current_mc: float) -> str | None:
    """
    返回鲸鱼等级：'A' / 'B' / 'C' / None（不是鲸鱼）。
    A级：>= $100k 且占比满足 → S级策略W。
    B级：>= $50k  且占比满足 → 策略W。
    C级：>= $10k  且占比满足 → signal_score 加分，>= $20k 才进验证队列。
    """
    if not is_whale(amount_usd, current_mc):
        return None
    if amount_usd >= WHALE_A_THRESHOLD_USD:
        return 'A'
    elif amount_usd >= WHALE_B_THRESHOLD_USD:
        return 'B'
    else:
        return 'C'


def get_whale_sell_level(amount_usd: float, current_mc: float) -> str | None:
    """
    鲸鱼卖出分级：'hard' / 'soft' / 'warn' / None。
    hard：>= $100k 且占比满足 → 策略B/W/S级硬止损全清。
    soft：>= $50k  且占比满足 → 策略W全清，策略B signal -15。
    warn：>= $10k  且占比满足 → 推送预警，不操作。
    None：不是鲸鱼卖出信号。
    """
    if not is_whale(amount_usd, current_mc):
        return None
    if amount_usd >= WHALE_SELL_HARD_USD:
        return 'hard'
    elif amount_usd >= WHALE_SELL_SOFT_USD:
        return 'soft'
    else:
        return 'warn'


# ── 高市值趋势过滤 ────────────────────────────────────────────────

_TREND_CACHE: dict = {}
TREND_CACHE_TTL = 120


def check_high_mc_trend(token_addr: str, current_mc: float) -> tuple[bool, str]:
    if current_mc < HIGH_MC_FILTER_THRESHOLD:
        return True, "市值 < 5M，无需质量过滤"

    now = time.time()
    key = token_addr.lower()
    if key in _TREND_CACHE:
        result, ts = _TREND_CACHE[key]
        if now - ts < TREND_CACHE_TTL:
            return result

    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{token_addr}",
            timeout=8,
            proxies={"https": "socks5://REDACTED_PROXY_CREDENTIALS"},
        )
        pairs = r.json().get("pairs", [])
        if not pairs:
            _log(f"[TREND] {token_addr[:10]}… DexScreener 无数据，放行")
            _TREND_CACHE[key] = ((True, "无数据放行"), now)
            return True, "DexScreener 无数据，放行"

        p = sorted(pairs, key=lambda x: x.get("liquidity", {}).get("usd", 0) or 0, reverse=True)[0]
        sym      = p.get("baseToken", {}).get("symbol", "?")
        chg_1h   = float(p.get("priceChange", {}).get("h1",  0) or 0)
        chg_6h   = float(p.get("priceChange", {}).get("h6",  0) or 0)
        chg_24h  = float(p.get("priceChange", {}).get("h24", 0) or 0)
        liq_usd  = float(p.get("liquidity",  {}).get("usd",  0) or 0)
        top10_pct = float((p.get("holders", {}) or {}).get("top10Percentage") or 0)

        _log(f"[TREND] {sym} | 1h={chg_1h:+.1f}% | 6h={chg_6h:+.1f}% | 24h={chg_24h:+.1f}%"
             f" | liq=${liq_usd:,.0f} | top10={top10_pct:.0f}%")

        checks = [
            (chg_1h < 0 and chg_6h < 0,
             f"{sym} 1h={chg_1h:+.1f}% 6h={chg_6h:+.1f}% 双向下跌，拦截"),
            (chg_24h > 500 and chg_1h < -3,
             f"{sym} 24h={chg_24h:+.1f}% 炸升后 1h={chg_1h:+.1f}% 回落，拦截"),
            (liq_usd < 30_000,
             f"{sym} 高MC但流动性仅 ${liq_usd:,.0f} < $30k，拦截"),
            (top10_pct > 60,
             f"{sym} top10持仓={top10_pct:.0f}% > 60%，筹码集中，拦截"),
        ]
        for cond, reason in checks:
            if cond:
                _TREND_CACHE[key] = ((False, reason), now)
                return False, reason

        reason = (f"{sym} 质量通过: 1h={chg_1h:+.1f}% 6h={chg_6h:+.1f}% 24h={chg_24h:+.1f}%"
                  f" liq=${liq_usd:,.0f} top10={top10_pct:.0f}%")
        _TREND_CACHE[key] = ((True, reason), now)
        return True, reason

    except Exception as e:
        _log(f"[TREND ERROR] {token_addr[:10]}… {e}，放行")
        return True, f"趋势检测失败({e})，放行"


# ── 市值查询 ──────────────────────────────────────────────────────

_MC_CACHE: dict = {}
MC_CACHE_TTL      = 30   # 实时数据（DexScreener）缓存 30s
MC_CACHE_TTL_STALE = 5   # fomoapi 榜单来源缓存仅 5s，防止旧数据污染


def fetch_market_cap(token_addr: str) -> float:
    now = time.time()
    key = token_addr.lower()
    if key in _MC_CACHE:
        mc, ts, source = _MC_CACHE[key]
        ttl = MC_CACHE_TTL_STALE if source == "fomo" else MC_CACHE_TTL
        if now - ts < ttl and mc > 0:
            return mc

    # ① DexScreener 优先（实时数据，比 fomoapi 榜单准确）
    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{token_addr}",
            timeout=8,
            proxies={"https": "socks5://REDACTED_PROXY_CREDENTIALS",
                     "http":  "socks5://REDACTED_PROXY_CREDENTIALS"},
        )
        pairs = r.json().get("pairs", [])
        rh_pairs = [p for p in pairs if p.get("chainId", "").lower() in ("robinhood", "robinhood-mainnet")]
        pool = (rh_pairs or pairs)
        if pool:
            p = sorted(pool, key=lambda x: x.get("liquidity", {}).get("usd", 0), reverse=True)[0]
            mc = float(p.get("marketCap") or p.get("fdv") or 0)
            if mc > 0:
                _MC_CACHE[key] = (mc, now, "dex")
                return mc
    except Exception as e:
        _log(f"[MC] DexScreener 查询失败: {e}")

    # ② fomoapi 热门榜兜底（仅更新没有实时数据的 token）
    try:
        _keys = FOMO_API_KEYS or [FOMO_API_KEY]
        headers = {"authorization": f"Bearer {_keys[0]}"}
        for endpoint in ["trending", "most-held"]:
            r = requests.get(
                f"https://api.fomoapi.io/v2/leaderboard/tokens/{endpoint}?limit=50",
                headers=headers, timeout=8,
            )
            for tok in r.json().get("tokens", []):
                addr = tok.get("token", {}).get("address", "").lower()
                mc   = tok.get("marketCapUsd", 0) or 0
                if mc > 0:
                    # 只更新当前没有实时数据的 token
                    if addr not in _MC_CACHE or _MC_CACHE[addr][2] != "dex":
                        _MC_CACHE[addr] = (mc, now, "fomo")
        if key in _MC_CACHE and _MC_CACHE[key][0] > 0:
            return _MC_CACHE[key][0]
    except Exception as e:
        _log(f"[MC] fomoapi 查询失败: {e}")

    return _MC_CACHE.get(key, (0, 0, ""))[0]
