"""
fx/shared/market_data_hub.py — 公共市场数据 WS 中心（阶段一：全市场标记价格 + 资金费率）

订阅 `!markPrice@arr@1s`（无需签名），本地缓存全市场最新标记价/资金费率/下次结算时间，
供 scanner 同步读取，减少扫描路径上的 REST 请求。

与 ws_listener 完全隔离：本模块自己持有一条独立的公共数据连接，不碰 userData / forceOrder / 持仓 markPrice。
默认关闭（config.MARKET_DATA_HUB_ENABLED = False）；关闭时本模块不会被 scanner / main 调用。

并发模型：WS 线程写、扫描线程读。缓存是 {symbol: (entry, recv_monotonic)} 的不可变快照字典，
写入时在锁内复制并整体替换引用；读取不加锁（拿到引用后只读）。
返回给调用方的字典与 REST premiumIndex 同字段名（symbol/markPrice/indexPrice/lastFundingRate/nextFundingTime/time），
额外带 age_sec。
"""
from __future__ import annotations

import asyncio
import json
import logging
import statistics
import threading
import time
from typing import Callable, Optional

import config

logger = logging.getLogger("market_data_hub")

try:
    _PROXY = config.PROXY_URL if getattr(config, "PROXY_URL", "") else None
except Exception:
    _PROXY = None

RECONNECT_BASE = getattr(config, "WS_RECONNECT_BASE", 3)
RECONNECT_MAX = getattr(config, "WS_RECONNECT_MAX", 60)
PING_INTERVAL = 20          # 秒：每 20 秒发 ping
NO_MSG_TIMEOUT = 30         # 秒：30 秒无任何消息 → 断开重连
SINGLE_MAX_AGE_SEC = 5.0    # 单标的读取：超过则返回 None
ALL_MAX_AGE_SEC = 10.0      # 全量快照：超过的条目被过滤
STATS_LOG_INTERVAL = 300    # 秒：5 分钟一条 info
STREAM_PATH = "/market/ws/!markPrice@arr@1s"   # 币安合约行情新路径（旧路径 /ws/ 已不推送行情）


class MarketDataHub:
    def __init__(self, url: Optional[str] = None, clock: Callable[[], float] = time.monotonic):
        self._url = url or (config.BINANCE_WS_URL + STREAM_PATH)
        self._clock = clock
        self._lock = threading.Lock()            # 仅保护写入与计数器
        self._prices: dict = {}                  # {symbol: (entry_dict, recv_ts)}，整体替换，不原地改
        self._ready = False
        self._connected = False
        # 可观测字段
        self.last_msg_ts: float = 0.0            # 最近一次收到消息的 monotonic 时间
        self.reconnect_count: int = 0
        self.frame_count: int = 0
        # 线程 / 事件循环
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._stopping = False

    # ── 对外读取接口（不加锁） ─────────────────────────────────────────────
    def is_ready(self) -> bool:
        """至少收到一帧全市场数据，且当前连接有效（断线后为 False，直到重连后收到新帧）。"""
        return self._ready

    def get_mark_price(self, symbol: str) -> Optional[dict]:
        """返回 {symbol, markPrice, lastFundingRate, nextFundingTime, ..., age_sec}；未就绪/缺失/超过 5 秒返回 None。"""
        if not self._ready:
            return None
        item = self._prices.get(symbol)
        if item is None:
            return None
        entry, recv = item
        age = self._clock() - recv
        if age > SINGLE_MAX_AGE_SEC:
            return None
        out = dict(entry)
        out["age_sec"] = round(age, 3)
        return out

    def get_all_mark_prices(self) -> dict:
        """全量快照 {symbol: dict(同上)}；age_sec 超过 10 秒的条目被过滤；未就绪返回 {}。"""
        if not self._ready:
            return {}
        now = self._clock()
        snap = self._prices                       # 拿引用即可，写入方只会替换不会原地改
        out = {}
        for sym, (entry, recv) in snap.items():
            age = now - recv
            if age <= ALL_MAX_AGE_SEC:
                d = dict(entry)
                d["age_sec"] = round(age, 3)
                out[sym] = d
        return out

    def stats(self) -> dict:
        snap = self._prices
        now = self._clock()
        ages = [now - r for _, r in snap.values()]
        return {
            "ready": self._ready, "connected": self._connected,
            "frame_count": self.frame_count, "reconnect_count": self.reconnect_count,
            "symbols": len(snap),
            "last_msg_age_sec": (now - self.last_msg_ts) if self.last_msg_ts else None,
            "age_median_sec": statistics.median(ages) if ages else None,
            "age_max_sec": max(ages) if ages else None,
        }

    # ── 帧解析 / 断线处理（同步，可直接测试） ───────────────────────────────
    def _handle_frame(self, raw) -> int:
        """解析一帧 `!markPrice@arr`（list[dict]），更新缓存；返回更新的标的数。非法帧返回 0。"""
        try:
            data = json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else raw
        except Exception:
            return 0
        if isinstance(data, dict):                # 兼容组合 stream 的 {"stream":..., "data":[...]}
            data = data.get("data")
        if not isinstance(data, list):
            return 0
        now = self._clock()
        updates = {}
        for it in data:
            if not isinstance(it, dict) or it.get("e") != "markPriceUpdate":
                continue
            sym = it.get("s")
            if not sym or it.get("p") is None:
                continue
            updates[sym] = ({
                "symbol": sym,
                "markPrice": it.get("p"),
                "indexPrice": it.get("i"),
                "lastFundingRate": it.get("r"),
                "nextFundingTime": it.get("T"),
                "time": it.get("E"),
            }, now)
        with self._lock:
            self.last_msg_ts = now
            self.frame_count += 1
            if updates:
                new = dict(self._prices)          # 复制 + 整体替换，读端不受影响
                new.update(updates)
                self._prices = new
                self._ready = True
        return len(updates)

    def _on_disconnect(self) -> None:
        """断线：清空缓存并置未就绪，重连期间读取一律返回 None/{}（不返回旧数据）。"""
        with self._lock:
            self._ready = False
            self._connected = False
            self._prices = {}

    # ── 后台线程 / 事件循环 ────────────────────────────────────────────────
    def start(self) -> None:
        """在后台线程启动 WS 连接，非阻塞；并注册为模块级默认实例。"""
        global _default
        if self._thread is not None and self._thread.is_alive():
            return
        self._stopping = False
        _default = self
        self._thread = threading.Thread(target=self._thread_main, name="market-data-hub", daemon=True)
        self._thread.start()
        logger.info(f"[hub] 已启动: {self._url}")

    def stop(self, timeout: float = 5.0) -> None:
        """优雅关闭：通知循环退出并等待线程结束。"""
        global _default
        self._stopping = True
        loop, ev = self._loop, self._stop_event
        if loop is not None and ev is not None:
            try:
                loop.call_soon_threadsafe(ev.set)
            except RuntimeError:
                pass
        if self._thread is not None:
            self._thread.join(timeout)
        self._on_disconnect()
        if _default is self:
            _default = None
        logger.info("[hub] 已停止")

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._run())
        except Exception as e:
            logger.error(f"[hub] 线程异常退出: {e}")
        finally:
            try:
                loop.close()
            except Exception:
                pass

    async def _run(self) -> None:
        self._stop_event = asyncio.Event()
        stats_task = asyncio.ensure_future(self._stats_loop())
        try:
            await self._connect_loop()
        finally:
            stats_task.cancel()

    async def _stats_loop(self) -> None:
        while True:
            await asyncio.sleep(STATS_LOG_INTERVAL)
            s = self.stats()
            logger.info(
                f"[hub] 5min统计: frame_count={s['frame_count']} 覆盖标的={s['symbols']} "
                f"age_median={_fmt(s['age_median_sec'])}s age_max={_fmt(s['age_max_sec'])}s "
                f"reconnect_count={s['reconnect_count']} ready={s['ready']}"
            )

    async def _connect_loop(self) -> None:
        import websockets
        delay = RECONNECT_BASE
        first = True
        while not self._stopping:
            if not first:
                self.reconnect_count += 1
                logger.warning(f"[hub] 重连第 {self.reconnect_count} 次（等待 {delay}s 后连接）")
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                    return
                except asyncio.TimeoutError:
                    pass
                delay = min(delay * 2, RECONNECT_MAX)
            first = False
            try:
                async with websockets.connect(
                    self._url, ping_interval=PING_INTERVAL, ping_timeout=10, proxy=_PROXY, max_queue=64,
                ) as ws:
                    self._connected = True
                    delay = RECONNECT_BASE
                    logger.info("[hub] WS 已连接")
                    while not self._stopping:
                        # 30 秒无消息 → 超时异常 → 走断线重连
                        raw = await asyncio.wait_for(ws.recv(), timeout=NO_MSG_TIMEOUT)
                        self._handle_frame(raw)
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                logger.warning(f"[hub] {NO_MSG_TIMEOUT}s 无消息，断开重连")
            except Exception as e:
                logger.warning(f"[hub] 断线: {e}")
            finally:
                self._on_disconnect()


def _fmt(v) -> str:
    return "n/a" if v is None else f"{v:.1f}"


# ── 模块级便捷接口（读取 start() 注册的默认实例；未启动时返回空） ──────────────
_default: Optional[MarketDataHub] = None


def get_hub() -> Optional[MarketDataHub]:
    return _default


def start() -> None:
    global _default
    if _default is None:
        _default = MarketDataHub()
    _default.start()


def stop() -> None:
    if _default is not None:
        _default.stop()


def is_ready() -> bool:
    return _default is not None and _default.is_ready()


def get_mark_price(symbol: str) -> Optional[dict]:
    return _default.get_mark_price(symbol) if _default is not None else None


def get_all_mark_prices() -> dict:
    return _default.get_all_mark_prices() if _default is not None else {}
