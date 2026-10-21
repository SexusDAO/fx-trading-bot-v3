"""
fx/shared/kline_cache.py — K 线 REST 缓存（按收盘时间失效）

只缓存「已收盘」K 线；最后一根未收盘 K 线每次由调用方用 limit=1 重取（fetch_live），
因此调用方拿到的数据格式与 api.get_klines 完全一致（最后一根是未收盘的）。

失效判断（不依赖本机时钟，以交易所返回的 K 线时间为准）：
  缓存记录最新已收盘 K 线的收盘时间 T（= 缓存数据 k[-2][6]，毫秒）。
  下一根 K 线的开盘时间必然 = T + 1。
    · 重取的未收盘 K 线开盘时间 == T + 1 → 仍在同一收盘周期 → 命中
    · 开盘时间 >  T + 1                    → 有新 K 线收盘了 → 失效（调用方重拉全量并 set）
    · 开盘时间 <  T + 1                    → 数据异常 → 视为未命中
线程安全：所有对内部字典与计数器的读写都加锁；网络请求（fetch_live）在锁外执行。
返回列表里的行对象与缓存共享（只读使用，调用方不得原地修改 K 线行）。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)

_INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "6h": 21_600_000,
    "8h": 28_800_000, "12h": 43_200_000, "1d": 86_400_000,
}


class _Entry:
    __slots__ = ("closed", "last_close", "live", "access_ts")

    def __init__(self, closed, last_close, live):
        self.closed = closed            # 已收盘 K 线（旧→新）
        self.last_close = last_close    # 最新已收盘 K 线的收盘时间（毫秒）
        self.live = live                # set 当时的未收盘那根（仅在调用方不提供 fetch_live 时作快照使用）
        self.access_ts = time.time()


class KlineCache:
    def __init__(self, max_entries: int = 600):
        self._lock = threading.Lock()
        self._d: dict = {}
        self._max = max_entries
        self._hit = 0
        self._miss = 0
        self._expired = 0
        self._reasons: dict = {}      # miss 原因 -> 次数（诊断用）

    # ── 写入 ──────────────────────────────────────────────────────────────
    def set(self, symbol: str, interval: str, data: list) -> None:
        """写入缓存：data 为 api.get_klines 的完整返回（最后一根是未收盘的）。"""
        if not isinstance(data, list) or len(data) < 3:
            logger.debug(f"[kline_cache] set 忽略 {symbol} {interval}: 数据无效 len={len(data) if isinstance(data, list) else type(data).__name__}")
            return
        try:
            last_close = int(data[-2][6])
        except (TypeError, ValueError, IndexError):
            logger.debug(f"[kline_cache] set 忽略 {symbol} {interval}: 收盘时间无法解析")
            return
        entry = _Entry(list(data[:-1]), last_close, data[-1])
        with self._lock:
            self._d[(symbol, interval)] = entry
            if len(self._d) > self._max:
                oldest = min(self._d, key=lambda k: self._d[k].access_ts)
                self._d.pop(oldest, None)
        logger.debug(f"[kline_cache] set 成功 {symbol} {interval} closed={len(entry.closed)} last_close={last_close}")

    # ── 读取 ──────────────────────────────────────────────────────────────
    def get(self, symbol: str, interval: str, limit: int,
            fetch_live: Optional[Callable[[], list]] = None) -> Optional[list]:
        """命中且未失效：返回与 api.get_klines 同格式的列表（最后一根是未收盘的）；未命中/已失效返回 None。

        fetch_live：返回最新 1 根（未收盘）K 线的可调用对象（通常是 limit=1 的 REST 请求）。
                    不提供时退化为按本机时钟判断，并使用 set 时保存的未收盘快照（可能略旧）。
        """
        key = (symbol, interval)
        with self._lock:
            e = self._d.get(key)
            if e is None:
                self._miss += 1
                self._reasons["key_not_found"] = self._reasons.get("key_not_found", 0) + 1
                logger.debug(f"[kline_cache] miss {symbol} {interval}: key 不存在 (size={len(self._d)})")
                return None
            if len(e.closed) < max(limit - 1, 0):
                self._miss += 1
                self._reasons["closed_too_short"] = self._reasons.get("closed_too_short", 0) + 1
                logger.debug(f"[kline_cache] miss {symbol} {interval}: closed 不够长 have={len(e.closed)} need={max(limit - 1, 0)}")
                return None
            e.access_ts = time.time()
            closed, last_close, snap = e.closed, e.last_close, e.live

        if fetch_live is None:
            iv = _INTERVAL_MS.get(interval)
            if iv and time.time() * 1000 > last_close + iv:
                return self._expire(key)
            live = snap
        else:
            try:
                got = fetch_live()
            except Exception:
                got = None
            if not got:
                return self._count_miss("live_fetch_empty_or_failed", key)
            live = got[-1]

        try:
            live_open = int(live[0])
        except (TypeError, ValueError, IndexError):
            return self._count_miss("live_open_unparsable", key)
        expected = last_close + 1
        if live_open == expected:
            with self._lock:
                self._hit += 1
            n = limit - 1
            return (closed[-n:] if n > 0 else []) + [live]
        if live_open > expected:
            return self._expire(key)
        return self._count_miss(f"live_open_lt_expected(live={live_open},expected={expected})", key)

    def _count_miss(self, reason: str = "unknown", key=None):
        with self._lock:
            self._miss += 1
            short = reason.split("(")[0]
            self._reasons[short] = self._reasons.get(short, 0) + 1
        logger.debug(f"[kline_cache] miss {key}: {reason}")
        return None

    def _expire(self, key):
        with self._lock:
            self._expired += 1
            self._d.pop(key, None)
        logger.debug(f"[kline_cache] expired {key}: 新 K 线已收盘，条目移除")
        return None

    def invalidate(self, symbol: str, interval: str) -> None:
        with self._lock:
            self._d.pop((symbol, interval), None)

    def stats(self) -> dict:
        with self._lock:
            return {"hit": self._hit, "miss": self._miss, "expired": self._expired}

    def miss_reasons(self) -> dict:
        with self._lock:
            return dict(self._reasons)

    def size(self) -> int:
        with self._lock:
            return len(self._d)
