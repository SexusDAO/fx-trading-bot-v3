"""
fx/tools/market_relay.py — 行情中转站（单文件，部署在能正常收币安 WS 推送的服务器上）

作用：连接币安合约 `!markPrice@arr@1s`（全市场标记价格 + 资金费率 + 下次结算时间），
把收到的每一帧原样转发给所有已连接的客户端（例如交易程序里的 market_data_hub）。

特点：
  - 单文件，只依赖 websockets（pip3 install websockets）
  - 上游断线自动重连（3s 起指数退避，最大 60s）；30s 无消息主动重连；每 20s ping
  - 客户端必须带口令（环境变量 RELAY_TOKEN）：URL 查询参数 ?token=xxx 或请求头 Authorization: Bearer xxx
    未设置 RELAY_TOKEN 时拒绝启动（除非显式 --insecure）
  - 新客户端连上时立刻收到最近一帧（不用等下一秒）
  - 本程序不使用、不保存任何币安密钥，只处理公开行情

运行：
  RELAY_TOKEN=你的口令 python3 market_relay.py [--host 0.0.0.0] [--port 8765] [--upstream wss://...]
注意：ws:// 是明文传输，口令会在网络上明文出现（数据本身是公开行情）。
      建议在云服务器安全组里只放行交易程序的出口 IP 访问该端口。
"""
from __future__ import annotations

import argparse
import asyncio
import hmac
import logging
import os
import sys
import time
from http import HTTPStatus
from urllib.parse import parse_qs, urlsplit

import websockets
from websockets.asyncio.server import serve

logger = logging.getLogger("market_relay")

DEFAULT_UPSTREAM = "wss://fstream.binance.com/market/ws/!markPrice@arr@1s"
RECONNECT_BASE = 3
RECONNECT_MAX = 60
NO_MSG_TIMEOUT = 30
PING_INTERVAL = 20
STATS_INTERVAL = 60


class Relay:
    def __init__(self, upstream: str = DEFAULT_UPSTREAM, token: str | None = None,
                 reconnect_base: float = RECONNECT_BASE, no_msg_timeout: float = NO_MSG_TIMEOUT):
        self.upstream = upstream
        self.token = token
        self.reconnect_base = reconnect_base
        self.no_msg_timeout = no_msg_timeout
        self.clients: set = set()
        self.last_frame = None          # 最近一帧（新客户端连上时先发这个）
        self.frame_count = 0
        self.reconnect_count = 0
        self.upstream_connected = False
        self._stopping = False

    # ── 鉴权 ────────────────────────────────────────────────────────────
    def _authorized(self, request) -> bool:
        if not self.token:
            return True
        supplied = None
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            supplied = auth[7:].strip()
        if supplied is None:
            q = parse_qs(urlsplit(request.path).query)
            vals = q.get("token")
            supplied = vals[0] if vals else None
        return supplied is not None and hmac.compare_digest(supplied, self.token)

    async def _process_request(self, connection, request):
        if not self._authorized(request):
            return connection.respond(HTTPStatus.UNAUTHORIZED, "unauthorized\n")
        return None

    # ── 客户端 ──────────────────────────────────────────────────────────
    async def _handler(self, ws):
        self.clients.add(ws)
        logger.info(f"[relay] 客户端连入，当前 {len(self.clients)} 个")
        try:
            if self.last_frame is not None:
                await ws.send(self.last_frame)
            async for _ in ws:        # 忽略客户端发来的任何消息，只用来保持连接/检测断开
                pass
        except websockets.ConnectionClosed:
            pass
        finally:
            self.clients.discard(ws)
            logger.info(f"[relay] 客户端断开，当前 {len(self.clients)} 个")

    # ── 上游 ────────────────────────────────────────────────────────────
    async def _upstream_loop(self):
        delay = self.reconnect_base
        first = True
        while not self._stopping:
            if not first:
                self.reconnect_count += 1
                logger.warning(f"[relay] 上游重连第 {self.reconnect_count} 次（{delay}s 后）")
                await asyncio.sleep(delay)
                delay = min(delay * 2, RECONNECT_MAX)
            first = False
            try:
                async with websockets.connect(self.upstream, ping_interval=PING_INTERVAL,
                                              ping_timeout=10, max_queue=64) as up:
                    self.upstream_connected = True
                    delay = self.reconnect_base
                    logger.info(f"[relay] 上游已连接: {self.upstream}")
                    while not self._stopping:
                        msg = await asyncio.wait_for(up.recv(), timeout=self.no_msg_timeout)
                        self.last_frame = msg
                        self.frame_count += 1
                        if self.clients:
                            websockets.broadcast(self.clients, msg)
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                logger.warning(f"[relay] 上游 {self.no_msg_timeout}s 无消息，断开重连")
            except Exception as e:
                logger.warning(f"[relay] 上游断线: {e}")
            finally:
                self.upstream_connected = False

    async def _stats_loop(self):
        while True:
            await asyncio.sleep(STATS_INTERVAL)
            logger.info(f"[relay] frames={self.frame_count} clients={len(self.clients)} "
                        f"upstream={'up' if self.upstream_connected else 'down'} reconnects={self.reconnect_count}")

    async def run(self, host: str = "0.0.0.0", port: int = 8765, ready: asyncio.Future | None = None):
        async with serve(self._handler, host, port, process_request=self._process_request,
                         ping_interval=PING_INTERVAL, ping_timeout=20) as server:
            if ready is not None and not ready.done():
                ready.set_result(server.sockets[0].getsockname()[1])
            logger.info(f"[relay] 监听 ws://{host}:{port}（鉴权={'开' if self.token else '关'}）")
            tasks = [asyncio.ensure_future(self._upstream_loop()), asyncio.ensure_future(self._stats_loop())]
            try:
                await asyncio.gather(*tasks)
            finally:
                self._stopping = True
                for t in tasks:
                    t.cancel()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="币安合约行情中转站")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--upstream", default=DEFAULT_UPSTREAM)
    ap.add_argument("--insecure", action="store_true", help="不设口令（不推荐）")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    token = os.environ.get("RELAY_TOKEN", "").strip() or None
    if token is None and not args.insecure:
        print("未设置环境变量 RELAY_TOKEN，拒绝启动（如确需无口令运行，加 --insecure）", file=sys.stderr)
        return 2
    try:
        asyncio.run(Relay(args.upstream, token).run(args.host, args.port))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
