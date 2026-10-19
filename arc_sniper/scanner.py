"""
Scanner — 监听 DEX 工厂合约的新币创建事件。

核心逻辑：
    1. 每 2 秒轮询一次 eth_getLogs
    2. 检查 TOLLY / WARP / Uniswap V3 工厂合约
    3. 解析事件日志，提取新代币地址
    4. 返回 (token_address, dex_type, creation_block, tx_hash)

设计原则：
    - 简单轮询，不依赖 WebSocket（新链 RPC 不一定支持 WS）
    - 支持多个工厂合约并行监控
    - 去重：已发现的代币不会重复返回
"""

import logging
import time
import subprocess
from web3 import Web3
from typing import Optional
from . import config

logger = logging.getLogger("arc.scanner")

# 已发现的代币集合（内存 + 文件持久化）
_seen: set = set()


def _load_seen():
    """从文件加载已发现代币列表"""
    import json
    import os
    if os.path.exists(config.SEEN_TOKENS_FILE):
        try:
            return set(json.load(open(config.SEEN_TOKENS_FILE)))
        except Exception:
            pass
    return set()


def _save_seen():
    """持久化已发现代币列表"""
    import json
    items = list(_seen)[-5000:]  # 最多保留 5000 条
    json.dump(items, open(config.SEEN_TOKENS_FILE, "w"))


_seen = _load_seen()
logger.info(f"[Scanner] 已加载 {len(_seen)} 个历史代币")


class Scanner:
    """轮询多个工厂合约，发现新代币创建事件。"""

    def __init__(self, w3: Web3):
        self.w3 = w3
        self.last_block = 0
        # 工厂监控配置
        # 统一走 Uniswap V3 PoolCreated + onchain.py 新池发现
        self.factories = []

        if config.UNISWAP_V3_FACTORY:
            self.factories.append({
                "address": config.UNISWAP_V3_FACTORY,
                "topic": config.POOL_CREATED_TOPIC,
                "type": "uniswap_v3",
            })
            logger.info("[Scanner] Uniswap V3 工厂已注册")

        if not self.factories:
            logger.warning("[Scanner] ⚠️ 没有配置工厂合约")

    def _get_latest_block(self) -> int:
        """获取最新区块号"""
        try:
            return self.w3.eth.block_number
        except Exception as e:
            logger.warning(f"[Scanner] 获取区块失败: {e}")
            return 0

    def scan(self) -> list[dict]:
        """
        扫描所有工厂合约，返回新代币列表。

        每条返回：
        {
            "token_address": str,    # 新代币合约地址
            "dex_type": str,          # "tolly" / "warp" / "uniswap_v3"
            "block_number": int,     # 创建区块
            "tx_hash": str,          # 创建交易哈希
            "pair_address": str,     # 交易对/池地址（如果事件中包含）
        }
        """
        if not self.factories:
            return []

        latest = self._get_latest_block()
        if latest == 0:
            return []

        if self.last_block == 0:
            self.last_block = latest - 5  # 首次运行，扫最近 5 个区块
        if latest <= self.last_block:
            return []

        from_block = self.last_block + 1
        to_block = min(latest, from_block + config.SCAN_BLOCK_RANGE - 1)

        new_tokens = []

        for factory in self.factories:
            try:
                # 使用 curl-based RPC 避免 web3.py 的连接池限流问题
                import json as _json
                payload = _json.dumps({
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
                     config.RPC_URL, "-H", "Content-Type: application/json",
                     "-d", payload],
                    capture_output=True, text=True, timeout=12,
                )
                resp = _json.loads(r.stdout) if r.stdout else {}
                logs_raw = resp.get("result", [])
                error = resp.get("error")

                if error:
                    logger.warning(f"[Scanner] RPC error ({factory['type']}) blocks {from_block}-{to_block}: {error.get('message','?')[:80]}")
                    break  # RPC 限流，不要推进 last_block，下次重试

                # 直接用 raw JSON dict，_parse_log 已改为支持 dict access
                for log_entry in logs_raw:
                    token_info = self._parse_log(log_entry, factory["type"])
                    if token_info and token_info["token_address"] not in _seen:
                        _seen.add(token_info["token_address"])
                        _save_seen()
                        new_tokens.append(token_info)
                        logger.info(
                            f"[Scanner] 🆕 新币: {token_info['token_address'][:10]}... "
                            f"DEX={token_info['dex_type']} "
                            f"block={token_info['block_number']}"
                        )

            except Exception as e:
                logger.warning(f"[Scanner] 扫描 {factory['type']} 失败 (blocks {from_block}-{to_block}): {e}")
                # 不 break，继续下一个 factory（如果有）
                continue

        # 始终推进 last_block，避免卡死
        # 如果 RPC 限流，最多丢失这几个 block 的信号，不影响整体运行
        self.last_block = to_block
        return new_tokens

    def _parse_log(self, log, dex_type: str) -> Optional[dict]:
        """
        解析工厂事件日志，提取代币地址。

        Uniswap V3 PoolCreated:
            topics[1] = token0, topics[2] = token1
            data 包含 fee, tickSpacing, pool 地址

        TOLLY / WARP:
            TODO: 确认事件格式后实现解析逻辑
        """
        topics = log.get("topics", [])
        data = log.get("data", "0x")

        try:
            if dex_type == "uniswap_v3":
                # PoolCreated(address indexed token0, address indexed token1, ...)
                # topics from JSON RPC are hex strings like "0x0000...1234"
                if len(topics) < 3:
                    return None
                # topics[1] = token0 (padded to 32 bytes), topics[2] = token1
                t0_hex = topics[1] if isinstance(topics[1], str) else topics[1].hex()
                t1_hex = topics[2] if isinstance(topics[2], str) else topics[2].hex()
                token0 = "0x" + t0_hex[-40:]
                token1 = "0x" + t1_hex[-40:]

                # 判断哪个是 USDC，另一个就是新代币
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
                    # 没有 USDC 端，跳过（不是 USDC 交易对）
                    return None

                # 池地址在 data 最后 20 bytes (40 hex chars)
                data_hex = data[2:] if data.startswith("0x") else data
                pool_addr = "0x" + data_hex[-40:] if len(data_hex) >= 40 else ""

                return {
                    "token_address": token_addr.lower(),
                    "dex_type": "dag",  # 统一走 DAG Router
                    "block_number": int(log.get("blockNumber"), 16) if isinstance(log.get("blockNumber"), str) else log.get("blockNumber", 0),
                    "tx_hash": log.get("transactionHash", ""),
                    "pair_address": pool_addr.lower(),
                }

            else:
                # Tolly/Warp 事件格式已废弃，统一走 V3 PoolCreated
                return None

        except Exception as e:
            logger.debug(f"[Scanner] 解析日志失败: {e}")

        return None

    def is_seen(self, token_address: str) -> bool:
        """检查代币是否已发现过"""
        return token_address.lower() in _seen

    def mark_seen(self, token_address: str):
        """标记代币为已发现"""
        _seen.add(token_address.lower())
        _save_seen()
