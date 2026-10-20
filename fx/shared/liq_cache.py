"""
shared/liq_cache.py — 清算区数据后台缓存

三级降级（v3.15.2 调整优先级）：
  1. Coinglass heatmap（权威来源，精度最高，支持 range_）
  2. ByKaranteli（备选，CG 失败时兜底）
  3. Coinglass liq_orders（最后兜底）

get_liq_zones_cached() 是 judges / calc_sl_tp 的唯一调用入口。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

logger = logging.getLogger("fx.liq_cache")

# ── 缓存存储 ───────────────────────────────────────────────────────────────
_CACHE: dict[str, dict] = {}
_CACHE_TTL = 15 * 60   # 15 分钟
_CACHE_LOCK = threading.Lock()

# ── 后台刷新线程状态 ───────────────────────────────────────────────────────
_bg_started = False
_bg_lock    = threading.Lock()

# 预热覆盖的 range_ 列表：确保所有场景均能命中缓存
_PREFETCH_RANGES = ("3d", "24h", "7d")


# ─────────────────────────────────────────────────────────────────────────────
# 内部工具
# ─────────────────────────────────────────────────────────────────────────────

def _liq_orders_to_zones(orders: list, direction: str, current_price: float) -> list:
    """把 liq_orders 原始列表转成 zone 格式（兜底路径用）。"""
    if not orders or current_price <= 0:
        return []
    bucket_pct = 0.05
    buckets: dict[int, float] = {}
    for o in orders:
        try:
            p   = float(o.get("price", 0) or o.get("liq_price", 0) or 0)
            usd = float(o.get("liqUsd", 0) or o.get("usd_size", 0) or 0)
            if p <= 0 or usd <= 0:
                continue
            if direction == "LONG"  and p >= current_price:
                continue
            if direction == "SHORT" and p <= current_price:
                continue
            idx = int(p / (current_price * bucket_pct))
            buckets[idx] = buckets.get(idx, 0.0) + usd
        except Exception:
            continue
    if not buckets:
        return []
    zones = []
    for idx, liq_usd in sorted(buckets.items(), key=lambda x: -x[1])[:10]:
        center = idx * current_price * bucket_pct
        zones.append({
            "price_low":  round(center * 0.975, 8),
            "price_high": round(center * 1.025, 8),
            "liq_usd":    round(liq_usd, 0),
        })
    return zones


def _fetch_from_bykaranteli(symbol: str, current_price: float) -> dict:
    """从 ByKaranteli 拉取清算区（备选路径，不区分 range）。"""
    try:
        from shared.bykaranteli_api import get_liq_zones
        long_zones  = get_liq_zones(symbol, "LONG",  current_price)
        short_zones = get_liq_zones(symbol, "SHORT", current_price)
        if long_zones or short_zones:
            logger.info(f"[liq_cache] {symbol} BYK liqmap: long={len(long_zones)} short={len(short_zones)} zones")
            return {"LONG": long_zones, "SHORT": short_zones, "ts": time.time(), "src_level": 2}
    except Exception as e:
        logger.debug(f"[liq_cache] {symbol} BYK 失败: {e}")
    return {}


def _fetch_from_clawby(symbol: str, current_price: float,
                        range_: str = "3d") -> dict:
    """
    清算区数据拉取，三级降级（v3.15.2：CG 升为主路径，BYK 降为备选）：
    1. Coinglass heatmap density（权威来源，支持 range_）
    2. ByKaranteli LiqMap（备选，CG 失败时兜底，不区分 range）
    3. Coinglass liq_orders 大额清算单（最后兜底）
    """
    # 主路径：Coinglass heatmap（权威来源，精度最高，支持 range_）
    try:
        from shared.clawby_adapter import get_liq_density
        density = get_liq_density(symbol, range_=range_)
        if density:
            long_zones  = []
            short_zones = []
            for price, val in sorted(density.items()):
                if val < 50_000:
                    continue
                zone = {"price_low": price * 0.998, "price_high": price * 1.002,
                        "amount": val}
                if price < current_price:
                    long_zones.append(zone)
                else:
                    short_zones.append(zone)
            if long_zones or short_zones:
                logger.info(f"[liq_cache] {symbol} CG heatmap: long={len(long_zones)} short={len(short_zones)} zones")
                return {"LONG": long_zones, "SHORT": short_zones, "ts": time.time(), "src_level": 1}
    except Exception as e:
        logger.debug(f"[liq_cache] {symbol} CG heatmap 失败: {e}")

    # 降级1：ByKaranteli（CG 失败时兜底）
    result = _fetch_from_bykaranteli(symbol, current_price)
    if result:
        logger.debug(f"[liq_cache] {symbol} CG失败，兜底用 BYK")
        return result

    # 降级2：Coinglass liq_orders
    try:
        from shared.coinglass_api import get_liq_orders
        raw = get_liq_orders(symbol, min_usd=50_000)
        if raw:
            long_zones  = _liq_orders_to_zones(raw, "LONG",  current_price)
            short_zones = _liq_orders_to_zones(raw, "SHORT", current_price)
            logger.debug(f"[liq_cache] {symbol} 降级到 CG liq_orders")
            return {"LONG": long_zones, "SHORT": short_zones, "ts": time.time(), "src_level": 3}
    except Exception as e:
        logger.debug(f"[liq_cache] {symbol} CG liq_orders 失败: {e}")

    logger.warning(f"[liq_cache] {symbol} 三级数据源全部失败，止损将降级为ATR公式")
    try:
        import subprocess as _sp
        _sp.run(
            ["openclaw", "message", "send", "--channel", "jarvis",
             "--message", f"⚠️ liq_cache {symbol} 三级数据源全部失败，清算图不可用，止损将降级为ATR公式"],
            timeout=5, capture_output=True,
        )
    except Exception:
        pass
    return {}


# ─────────────────────────────────────────────────────────────────────────────
# 公共接口
# ─────────────────────────────────────────────────────────────────────────────

def get_liq_zones_cached(symbol: str, direction: str,
                          current_price: float = 0.0,
                          range_: str = "3d") -> list:
    """
    返回指定方向的清算区列表。

    v3.64 逻辑调整：
    - 缓存未命中（包括重启后空缓存）→ 直接实时拉取，不走降级
    - 三级数据源全失败才返回空列表（对应 sl_degraded=True）
    - 这保证每次调用都能拿到最新清算图，不再因"缓存未预热"导致降级
    - 仓位维护场景（patrol/adjust_sl_tp）调用此函数时同样受益，
      若缓存已过期则自动刷新，确保止损动态跟随最新清算图结构
    """
    cache_key = f"{symbol}:{range_}"
    with _CACHE_LOCK:
        entry = _CACHE.get(cache_key)
        stale = (entry is None) or (time.time() - entry.get("ts", 0) > _CACHE_TTL)

    if stale:
        # 缓存过期或为空：实时拉取，而非直接返回空（v3.64 关键修改）
        price = current_price
        if price <= 0:
            try:
                from shared.binance_api import get_mark_price
                price = float(get_mark_price(symbol).get("markPrice", 0))
            except Exception:
                pass
        data = _fetch_from_clawby(symbol, price, range_=range_)
        if data:
            with _CACHE_LOCK:
                _CACHE[cache_key] = data
            entry = data
            logger.debug(f"[liq_cache] {symbol}:{range_} 实时拉取成功，写入缓存")
        else:
            # 三级数据源全部失败，才真正返回空（对应 sl_degraded）
            # 不写入缓存，下次调用继续尝试实时拉取
            logger.debug(f"[liq_cache] {symbol}:{range_} 三级数据源全失败，本次返回空")
            return []

    return (entry or {}).get(direction.upper(), [])


def prefetch_liq_cache(symbols: list) -> None:
    """
    后台批量预热缓存（每 15 分钟由 main.py 触发）。
    v3.15.1：BYK有数据时三个range共享；非主流币对每个range独立用CG拉取。
    """
    logger.info(f"[liq_cache] 开始预热 {len(symbols)} 个标的的清算区缓存")
    updated = 0
    for sym in symbols:
        try:
            from shared.binance_api import get_mark_price
            price = float(get_mark_price(sym).get("markPrice", 0))
            if price <= 0:
                continue

            # 判断是否需要刷新
            needs_refresh = False
            for r in _PREFETCH_RANGES:
                with _CACHE_LOCK:
                    entry = _CACHE.get(f"{sym}:{r}")
                    ts = entry.get("ts", 0) if entry else 0
                if time.time() - ts >= _CACHE_TTL * 0.8:
                    needs_refresh = True
                    break
            if not needs_refresh:
                continue

            wrote = 0
            for r in _PREFETCH_RANGES:
                data = _fetch_from_clawby(sym, price, range_=r)
                if data:
                    with _CACHE_LOCK:
                        _CACHE[f"{sym}:{r}"] = data
                    wrote += 1
                    # CG heatmap 拿到数据后，其他 range 共享同一份 BYK 结果
                    # （BYK 不区分 range，避免重复请求）
                    if data.get("LONG") and any(
                        z.get("source") == "byk_real_level"
                        for z in (data.get("LONG") or [])[:1]
                    ):
                        # BYK 数据，三个 range 共享
                        with _CACHE_LOCK:
                            for other_r in _PREFETCH_RANGES:
                                if other_r != r:
                                    _CACHE[f"{sym}:{other_r}"] = data
                        wrote = len(_PREFETCH_RANGES)
                        break

            if wrote > 0:
                updated += 1
                logger.debug(f"[liq_cache] {sym} 预热写入 {wrote}/3 个 range key")
            time.sleep(3)
        except Exception as e:
            logger.debug(f"[liq_cache] {sym} 预热失败: {e}")
    logger.info(f"[liq_cache] 预热完成，更新 {updated}/{len(symbols)} 个")


def start_bg_refresh(symbols_fn) -> None:
    """启动后台刷新线程（由 main.py 调用一次）。"""
    global _bg_started
    with _bg_lock:
        if _bg_started:
            return
        _bg_started = True

    def _loop():
        while True:
            try:
                syms = symbols_fn() or []
                if syms:
                    prefetch_liq_cache(syms)
            except Exception as e:
                logger.warning(f"[liq_cache] bg_refresh 异常: {e}")
            time.sleep(_CACHE_TTL)

    t = threading.Thread(target=_loop, name="liq-cache-bg", daemon=True)
    t.start()
    logger.info("[liq_cache] 后台刷新线程已启动（间隔15分钟）")


def get_liq_src_level(symbol: str) -> int:
    """[v3.88 OPT-14g] 返回该标的当前缓存中清算图的最差数据来源级别：
    1=Coinglass heatmap（主路径） / 2=ByKaranteli（二级降级） / 3=Coinglass liq_orders（三级降级）。
    无缓存或未记录来源时返回 0（视为未知，不触发降级回退）。只读，不触发网络请求。"""
    lvl = 0
    try:
        with _CACHE_LOCK:
            for k, v in _CACHE.items():
                if k.startswith(f"{symbol}:"):
                    lvl = max(lvl, int((v or {}).get("src_level", 0) or 0))
    except Exception:
        return 0
    return lvl
