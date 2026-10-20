"""
Scanner WS — WebSocket 订阅版 Scanner
=====================================
基于 Arc 官方文档推荐：用 eth_subscribe("newHeads") WebSocket 订阅新块，
有新块才查一次 eth_getLogs（只查 1 个块），彻底绕开 RPC HTTP 限流。

参考: docs.arc.io/integrate/infrastructure/indexing-events
       docs.arc.io/arc/tutorials/monitor-contract-events

用法:
    from arc_sniper.scanner_ws import ScannerWS
    scanner = ScannerWS()
    await scanner.start(on_new_token=callback)
"""

import asyncio
import json
import logging
import subprocess
import time
from typing import Callable, Optional
from web3 import Web3
from . import config

logger = logging.getLogger("arc.scanner_ws")

# 复用 _seen 集合
from .scanner import _seen, _save_seen, _load_seen

# WebSocket endpoint (官方文档)
WS_URL = "wss://rpc.mainnet.arc.io"
# HTTP fallback (curl-based, 绕开 web3.py 连接池限流)
HTTP_URL = config.RPC_URL


class ScannerWS:
    """WebSocket 订阅版 Scanner — 实时监听新块，精准查日志。"""

    def __init__(self, w3: Optional[Web3] = None):
        self.w3 = w3 or Web3(Web3.HTTPProvider(HTTP_URL, request_kwargs={"timeout": 15}))
        self.last_block = 0
        self._running = False
        self._reconnect_delay = 1  # 断线重连延迟（秒），指数退避

        # 监听的工厂合约
        self.factories = []
        if config.UNISWAP_V3_FACTORY:
            self.factories.append({
                "address": config.UNISWAP_V3_FACTORY,
                "topic": config.POOL_CREATED_TOPIC,
                "type": "uniswap_v3",
            })
            logger.info("[ScannerWS] Uniswap V3 工厂已注册")

        # System Emitter (EIP-7708) — 监听所有 USDC Transfer 事件
        # 官方文档: 0xffffFFFfFFffffffffffffffFfFFFfffFFFfFFfE
        self.system_emitter = "0xffffFFFfFFffffffffffffffFfFFFfffFFFfFFfE"
        self.monitor_emitter = False  # 可选增强，默认关闭

    async def start(self, on_new_token: Callable[[dict], None]):
        """
        启动 WebSocket 订阅循环。
        on_new_token: 回调函数，参数为 token_info dict。
        """
        self._running = True
        logger.info(f"[ScannerWS] 启动 WebSocket 订阅, endpoint={WS_URL}")

        while self._running:
            try:
                await self._run_loop(on_new_token)
            except Exception as e:
                logger.warning(f"[ScannerWS] WebSocket 断开: {e}, {self._reconnect_delay}s 后重连...")
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, 30)  # 指数退避，最大 30s

    async def stop(self):
        """停止订阅"""
        self._running = False
        logger.info("[ScannerWS] 停止订阅")

    async def _run_loop(self, on_new_token: Callable[[dict], None]):
        """
        核心 WebSocket 循环：
        1. 连接 WebSocket
        2. 发送 eth_subscribe("newHeads")
        3. 收到新块 → 查该块的 PoolCreated 事件
        4. 解析 → 回调
        """
        import websockets

        async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=60, close_timeout=10) as ws:
            self._reconnect_delay = 1  # 重连成功，重置退避

            # 订阅 newHeads
            sub_msg = json.dumps({
                "jsonrpc": "2.0",
                "method": "eth_subscribe",
                "params": ["newHeads"],
                "id": 1,
            })
            await ws.send(sub_msg)
            logger.info("[ScannerWS] 已订阅 newHeads")

            # 等待订阅确认
            ack = await ws.recv()
            ack_data = json.loads(ack)
            sub_id = ack_data.get("result", "")
            logger.info(f"[ScannerWS] 订阅 ID: {sub_id}")

            # 初始化 last_block
            if self.last_block == 0:
                latest = self._get_latest_block_http()
                if latest:
                    self.last_block = latest - 1
                    logger.info(f"[ScannerWS] 初始化 last_block={self.last_block}")

            # 消息循环
            while self._running:
                raw = await ws.recv()
                msg = json.loads(raw)

                # newHeads 通知
                if msg.get("method") == "eth_subscription":
                    result = msg.get("params", {}).get("result", {})
                    block_num = int(result.get("number", "0x0"), 16)
                    if block_num > self.last_block:
                        await self._scan_block(block_num, on_new_token)

                # 忽略其他消息（如 log subscriptions 的回显）

    async def _scan_block(self, block_num: int, on_new_token: Callable[[dict], None]):
        """
        查询指定区块的所有工厂 PoolCreated 事件。
        只查 1 个块，不会触发 RPC 限流。
        """
        # 扫描 last_block+1 到 block_num 的所有区块（通常只有 1-2 个）
        from_block = self.last_block + 1
        to_block = block_num

        for factory in self.factories:
            try:
                # 用 curl-based HTTP 查日志（单块查询，不会限流）
                payload = json.dumps({
                    "jsonrpc": "2.0",
                    "method": "eth_getLogs",
                    "params": [{
                        "fromBlock": hex(from_block),
                        "toBlock": hex(to_block),
                        "address": Web3.to_checksum_address(factory["address"]),
                        "topics": [factory["topic"]] if factory["topic"] else [],
                    }],
                    "id": 1,
                })
                r = subprocess.run(
                    ["curl", "-s", "-m", "10", "-X", "POST",
                     HTTP_URL, "-H", "Content-Type: application/json",
                     "-d", payload],
                    capture_output=True, text=True, timeout=12,
                )
                resp = json.loads(r.stdout) if r.stdout else {}
                logs_raw = resp.get("result", [])
                error = resp.get("error")

                if error:
                    logger.warning(f"[ScannerWS] RPC error block {block_num}: {error.get('message','?')[:80]}")
                    continue

                for log_entry in logs_raw:
                    token_info = self._parse_log(log_entry, factory["type"])
                    if token_info and token_info["token_address"] not in _seen:
                        _seen.add(token_info["token_address"])
                        _save_seen()
                        logger.info(
                            f"[ScannerWS] 🆕 新币: {token_info['token_address'][:10]}... "
                            f"DEX={token_info['dex_type']} block={token_info['block_number']}"
                        )
                        # 异步回调
                        if asyncio.iscoroutinefunction(on_new_token):
                            await on_new_token(token_info)
                        else:
                            on_new_token(token_info)

            except Exception as e:
                logger.warning(f"[ScannerWS] 扫描 block {block_num} 失败: {e}")

        # 推进 last_block
        self.last_block = block_num

    def _get_latest_block(self) -> int:
        """获取最新区块号 (HTTP curl)"""
        return self._get_latest_block_http()

    def _get_latest_block_http(self) -> int:
        """用 curl 获取最新区块号"""
        try:
            r = subprocess.run(
                ["curl", "-s", "-m", "5", "-X", "POST",
                 HTTP_URL, "-H", "Content-Type: application/json",
                 "-d", json.dumps({"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1})],
                capture_output=True, text=True, timeout=7,
            )
            resp = json.loads(r.stdout) if r.stdout else {}
            return int(resp.get("result", "0x0"), 16)
        except Exception as e:
            logger.warning(f"[ScannerWS] 获取区块号失败: {e}")
            return 0

    def _parse_log(self, log, dex_type: str) -> Optional[dict]:
        """
        解析工厂事件日志 — 复用 scanner.py 的逻辑。
        """
        topics = log.get("topics", [])
        data = log.get("data", "0x")

        try:
            if dex_type == "uniswap_v3":
                if len(topics) < 3:
                    return None
                t0_hex = topics[1] if isinstance(topics[1], str) else topics[1].hex()
                t1_hex = topics[2] if isinstance(topics[2], str) else topics[2].hex()
                token0 = "0x" + t0_hex[-40:]
                token1 = "0x" + t1_hex[-40:]

                # 判断哪个是 USDC/wUSDC，另一个是新代币
                wusdc = (config.WUSDC_ADDRESS or "").lower()
                native = (config.NATIVE_USDC_ADDR or "").lower()
                token0_lower = token0.lower()
                token1_lower = token1.lower()

                if wusdc and token0_lower == wusdc:
                    token_addr = token1
                elif wusdc and token1_lower == wusdc:
                    token_addr = token0
                elif native and token0_lower == native:
                    token_addr = token1
                elif native and token1_lower == native:
                    token_addr = token0
                else:
                    # 不是 USDC 交易对，也记录（可能是 TOKEN-TOKEN 池）
                    # 选非 USDC 的那个作为新代币
                    # 但如果两个都是未知代币，选第一个
                    token_addr = token1 if token0_lower in (wusdc, native) else token0

                # 池地址在 data 最后 20 bytes
                data_hex = data[2:] if data.startswith("0x") else data
                pool_addr = "0x" + data_hex[-40:] if len(data_hex) >= 40 else ""

                return {
                    "token_address": token_addr.lower(),
                    "dex_type": "dag",
                    "block_number": int(log.get("blockNumber"), 16) if isinstance(log.get("blockNumber"), str) else log.get("blockNumber", 0),
                    "tx_hash": log.get("transactionHash", ""),
                    "pair_address": pool_addr.lower(),
                }
            else:
                return None
        except Exception as e:
            logger.debug(f"[ScannerWS] 解析日志失败: {e}")
            return None

    def scan_sync(self) -> list[dict]:
        """
        同步扫描方法 — 兼容旧版 Scanner 接口。
        用于 HTTP 轮询 fallback 模式。
        """
        latest = self._get_latest_block_http()
        if latest == 0:
            return []

        if self.last_block == 0:
            self.last_block = latest - 5

        if latest <= self.last_block:
            return []

        # 如果落后太多，跳到最新
        if latest - self.last_block > 50:
            logger.info(f"[ScannerWS] 落后 {latest - self.last_block} blocks, 跳到 latest-5")
            self.last_block = latest - 5

        from_block = self.last_block + 1
        to_block = min(latest, from_block + config.SCAN_BLOCK_RANGE - 1)

        new_tokens = []
        for factory in self.factories:
            try:
                payload = json.dumps({
                    "jsonrpc": "2.0",
                    "method": "eth_getLogs",
                    "params": [{
                        "fromBlock": hex(from_block),
                        "toBlock": hex(to_block),
                        "address": Web3.to_checksum_address(factory["address"]),
                        "topics": [factory["topic"]] if factory["topic"] else [],
                    }],
                    "id": 1,
                })
                r = subprocess.run(
                    ["curl", "-s", "-m", "10", "-X", "POST",
                     HTTP_URL, "-H", "Content-Type: application/json",
                     "-d", payload],
                    capture_output=True, text=True, timeout=12,
                )
                resp = json.loads(r.stdout) if r.stdout else {}
                logs_raw = resp.get("result", [])
                error = resp.get("error")

                if error:
                    logger.warning(f"[ScannerWS] RPC error: {error.get('message','?')[:80]}")
                    continue

                for log_entry in logs_raw:
                    token_info = self._parse_log(log_entry, factory["type"])
                    if token_info and token_info["token_address"] not in _seen:
                        _seen.add(token_info["token_address"])
                        _save_seen()
                        new_tokens.append(token_info)
                        logger.info(
                            f"[ScannerWS] 🆕 新币: {token_info['token_address'][:10]}... "
                            f"DEX={token_info['dex_type']} block={token_info['block_number']}"
                        )
            except Exception as e:
                logger.warning(f"[ScannerWS] 扫描失败: {e}")
                continue

        self.last_block = to_block
        return new_tokens

    def is_seen(self, token_address: str) -> bool:
        return token_address.lower() in _seen

    def mark_seen(self, token_address: str):
        _seen.add(token_address.lower())
        _save_seen()
