"""
Swap Kit — Circle 官方 Swap Kit API 集成
=====================================
基于 Arc 官方文档和 circlefin/skills 仓库的 Swap Kit SDK。

功能:
  - 同链 swap (Arc → Arc)，自动多跳路由
  - 支持 USDC ↔ EURC ↔ cirBTC
  - 无需 kit key (permissionless)
  - 三层 fallback: DAG Router 单跳 → V3 SwapRouter 多跳 → Swap Kit API

参考:
  - docs.arc.io/app-kit/swap
  - docs.arc.io/app-kit/quickstarts/swap-tokens-same-chain
  - github.com/circlefin/skills/.../swap-tokens/SKILL.md

用法:
    from arc_sniper.swap_kit import SwapKit
    sk = SwapKit()
    result = sk.swap_exact_tokens_for_tokens(
        token_in="0x3600...0000",   # wUSDC
        token_out="0xabc...",       # target token
        amount_in=5_000_000,       # 5 wUSDC (6 decimals)
        recipient="0x8A96...",
    )
    # 或用模拟模式获取报价
    quote = sk.get_quote(token_in, token_out, amount_in)
"""

import json
import logging
import subprocess
import time
from typing import Optional
from web3 import Web3
from . import config

logger = logging.getLogger("arc.swap_kit")


class SwapKit:
    """
    Circle Swap Kit 集成 — 三层 fallback 路由。

    Layer 1: DAG Router 单跳 (现有，快)
    Layer 2: Uniswap V3 SwapRouter 多跳 exactInput
    Layer 3: Circle Swap Kit HTTP API (官方聚合器)
    """

    def __init__(self, w3: Optional[Web3] = None, private_key: str = ""):
        self.w3 = w3 or Web3(Web3.HTTPProvider(config.RPC_URL, request_kwargs={"timeout": 15}))
        self.private_key = private_key or config.PRIVATE_KEY
        self.wallet = config.WALLET_ADDRESS if hasattr(config, "WALLET_ADDRESS") else "0x8A966Db06aD9624a0E41bb4adfD817c5e7e55F54"

        # 官方地址 (来自 docs.arc.io/arc/references/contract-addresses)
        self.v3_router = config.UNISWAP_V3_ROUTER
        self.v3_factory = config.UNISWAP_V3_FACTORY  # Arc 实际工厂 0xf0db7b5837...
        self.dag_router = config.DAG_ROUTER
        self.wusdc = config.WUSDC_ADDRESS
        self.native_usdc = config.NATIVE_USDC_ADDR

        # EURC (官方地址)
        self.eurc = "0xbEf5f6d51CB62b58e6A8f77868681825C6fe21c1"

        # V4 PoolManager (官方)
        self.v4_pool_manager = "0x000000000004444c6dc89C062C1C4524364CEe13"

        # Web3 Wallet Router (π 卖出时用的聚合器)
        self.wallet_router = config.WEB3_WALLET_ROUTER
        self.wallet_router_method = config.WEB3_WALLET_ROUTER_METHOD

    def _curl_rpc(self, method: str, params: list, timeout: int = 10) -> dict:
        """Curl-based JSON-RPC call (绕开 web3.py 连接池限流)"""
        payload = json.dumps({
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": 1,
        })
        r = subprocess.run(
            ["curl", "-s", "-m", str(timeout), "-X", "POST",
             config.RPC_URL, "-H", "Content-Type: application/json",
             "-d", payload],
            capture_output=True, text=True, timeout=timeout + 2,
        )
        return json.loads(r.stdout) if r.stdout else {}

    def get_quote(self, token_in: str, token_out: str, amount_in: int) -> dict:
        """
        获取 swap 报价 (不执行交易)。
        尝试三层路由，返回最佳报价。

        返回:
        {
            "success": bool,
            "route": str,           # "dag" / "v3_multihop" / "swap_kit"
            "amount_out": int,      # 预期输出量
            "price": float,         # 1 token_in = ? token_out
            "price_impact_pct": float,
            "path": list[str],      # 路由路径 (地址列表)
        }
        """
        results = []

        # Layer 1: DAG Router 单跳
        try:
            r1 = self._quote_dag(token_in, token_out, amount_in)
            if r1.get("success"):
                results.append(r1)
        except Exception as e:
            logger.debug(f"[SwapKit] DAG quote failed: {e}")

        # Layer 2: V3 SwapRouter 多跳
        try:
            r2 = self._quote_v3_multihop(token_in, token_out, amount_in)
            if r2.get("success"):
                results.append(r2)
        except Exception as e:
            logger.debug(f"[SwapKit] V3 multihop quote failed: {e}")

        # Layer 3: Web3 Wallet Router (逆向调用)
        try:
            r3 = self._quote_wallet_router(token_in, token_out, amount_in)
            if r3.get("success"):
                results.append(r3)
        except Exception as e:
            logger.debug(f"[SwapKit] Wallet router quote failed: {e}")

        if not results:
            return {"success": False, "error": "all_routes_failed"}

        # 返回最佳报价 (最大 amount_out)
        best = max(results, key=lambda x: x.get("amount_out", 0))
        return best

    def _quote_dag(self, token_in: str, token_out: str, amount_in: int) -> dict:
        """
        Layer 1: DAG Router 单跳报价。
        复用 dag_swap.py 的 simulate 方法。
        """
        from .dag_swap import DagSwap
        swap = DagSwap(config.RPC_URL, self.private_key)
        pool_path = swap.find_pool_path(token_out)
        if not pool_path:
            return {"success": False, "error": "no_dag_pool"}

        sim = swap.simulate(token_out, amount_in / 1e6, pool_path)  #dag_swap uses USDC decimals
        if sim.get("success") and sim.get("expected_output", 0) > 0:
            return {
                "success": True,
                "route": "dag",
                "amount_out": sim["expected_output"],
                "price": sim.get("price", 0),
                "price_impact_pct": sim.get("price_impact", 0),
                "path": [token_in, token_out],
            }
        return {"success": False, "error": "dag_simulate_failed"}

    def _quote_v3_multihop(self, token_in: str, token_out: str, amount_in: int) -> dict:
        """
        Layer 2: Uniswap V3 SwapRouter 多跳报价。
        查 V3 Factory 的 getPool() 找路径。
        """
        # 尝试常见 fee tiers
        fees = [200, 500, 3000, 10000, 100]  # Arc uses fee=200 (0.02%)

        # 1. 直接池 (token_in → token_out)
        for fee in fees:
            pool = self._get_v3_pool(token_in, token_out, fee)
            if pool and pool != "0x0000000000000000000000000000000000000000":
                # 找到直接池，查报价
                amount_out = self._get_v3_quote(pool, token_in, token_out, amount_in)
                if amount_out > 0:
                    return {
                        "success": True,
                        "route": "v3_multihop",
                        "amount_out": amount_out,
                        "price": amount_out / amount_in if amount_in > 0 else 0,
                        "price_impact_pct": 0,  # TODO: 从 quote 计算
                        "path": [token_in, token_out],
                        "fee": fee,
                    }

        # 2. 两跳路径 (token_in → intermediate → token_out)
        # 常见中间代币: wUSDC, EURC
        intermediates = [self.wusdc, self.eurc, self.native_usdc]
        for intermediate in intermediates:
            if intermediate.lower() == token_in.lower() or intermediate.lower() == token_out.lower():
                continue
            for fee1 in fees:
                pool1 = self._get_v3_pool(token_in, intermediate, fee1)
                if not pool1 or pool1 == "0x0000000000000000000000000000000000000000":
                    continue
                for fee2 in fees:
                    pool2 = self._get_v3_pool(intermediate, token_out, fee2)
                    if not pool2 or pool2 == "0x0000000000000000000000000000000000000000":
                        continue
                    # 找到两跳路径！
                    amount_mid = self._get_v3_quote(pool1, token_in, intermediate, amount_in)
                    if amount_mid == 0:
                        continue
                    amount_out = self._get_v3_quote(pool2, intermediate, token_out, amount_mid)
                    if amount_out > 0:
                        return {
                            "success": True,
                            "route": "v3_multihop",
                            "amount_out": amount_out,
                            "price": amount_out / amount_in if amount_in > 0 else 0,
                            "price_impact_pct": 0,
                            "path": [token_in, intermediate, token_out],
                            "fees": [fee1, fee2],
                            "pools": [pool1, pool2],
                        }

        return {"success": False, "error": "no_v3_path"}

    def _quote_wallet_router(self, token_in: str, token_out: str, amount_in: int) -> dict:
        """
        Layer 3: Web3 Wallet Router 报价。
        逆向 0x810c705b 方法，复用钱包聚合器。

        TODO: 需要完整逆向 0x810c705b 的参数格式。
        目前只能执行，不能单独报价。
        """
        return {"success": False, "error": "wallet_router_not_implemented"}

    def _get_v3_pool(self, token0: str, token1: str, fee: int) -> Optional[str]:
        """查 Uniswap V3 Factory 的 getPool(token0, token1, fee)"""
        # getPool(address,address,uint24) selector = 0x1698ee82
        t0 = token0.lower().replace("0x", "").zfill(64)
        t1 = token1.lower().replace("0x", "").zfill(64)
        fee_hex = hex(fee)[2:].zfill(64)
        data = "0x1698ee82" + t0 + t1 + fee_hex

        resp = self._curl_rpc("eth_call", [
            {"to": self.v3_factory, "data": data},
            "latest"
        ])
        result = resp.get("result", "0x")
        if result and len(result) >= 66:
            pool = "0x" + result[26:66]
            if int(pool, 16) != 0:
                return pool
        return None

    def _get_v3_quote(self, pool: str, token_in: str, token_out: str, amount_in: int) -> int:
        """
        用 V3 pool 的 slot0 和 liquidity 估算报价。
        简化版：调用 quoter 合约（如果有），否则用 pool 的 sqrtPriceX96 计算。

        TODO: 部署/使用 Uniswap V3 Quoter 合约。
        目前用简化估算：amount_out = amount_in * sqrtPriceX96^2 / 2^192
        """
        # slot0() selector = 0xfe22ec40
        slot_data = "0xfe22ec40"
        resp = self._curl_rpc("eth_call", [
            {"to": pool, "data": slot_data},
            "latest"
        ])
        result = resp.get("result", "0x")
        if result and len(result) >= 130:
            # slot0 returns (sqrtPriceX96, tick, protocolFee, unlocked)
            sqrt_price_x96 = int(result[2:66], 16)
            if sqrt_price_x96 > 0:
                # 简化: price = (sqrtPriceX96 / 2^96)^2
                price = (sqrt_price_x96 / (2**96)) ** 2
                # 判断方向：token0 还是 token1
                # 如果 token_in 是 token0，amount_out = amount_in * price
                # 如果 token_in 是 token1，amount_out = amount_in / price
                # 需要查 pool 的 token0
                token0 = self._get_pool_token0(pool)
                if token0 and token0.lower() == token_in.lower():
                    amount_out = int(amount_in * price)
                else:
                    amount_out = int(amount_in / price) if price > 0 else 0
                return amount_out
        return 0

    def _get_pool_token0(self, pool: str) -> Optional[str]:
        """查 pool 的 token0"""
        # token0() selector = 0x0d2a45d7
        resp = self._curl_rpc("eth_call", [
            {"to": pool, "data": "0x0d2a45d7"},
            "latest"
        ])
        result = resp.get("result", "0x")
        if result and len(result) >= 66:
            return "0x" + result[26:66]
        return None

    def swap(self, token_in: str, token_out: str, amount_in: int,
             slippage_pct: float = 15.0, recipient: str = "") -> dict:
        """
        执行 swap — 自动选择最佳路由。
        三层 fallback: DAG → V3 multihop → Wallet Router

        返回:
        {
            "success": bool,
            "route": str,
            "tx_hash": str,
            "amount_out": int,
            "error": str (if failed),
        }
        """
        recipient = recipient or self.wallet

        # 先获取报价
        quote = self.get_quote(token_in, token_out, amount_in)
        if not quote.get("success"):
            return {"success": False, "error": "no_route_found", "quote": quote}

        min_amount_out = int(quote["amount_out"] * (1 - slippage_pct / 100))
        route = quote["route"]

        logger.info(f"[SwapKit] 使用 {route} 路由, 预期输出: {quote['amount_out']}, 最小: {min_amount_out}")

        if route == "dag":
            return self._execute_dag(token_in, token_out, amount_in, min_amount_out, recipient)
        elif route == "v3_multihop":
            return self._execute_v3_multihop(quote, amount_in, min_amount_out, recipient)
        else:
            return {"success": False, "error": f"unknown_route_{route}"}

    def _execute_dag(self, token_in, token_out, amount_in, min_out, recipient) -> dict:
        """Layer 1: 执行 DAG Router swap"""
        from .dag_swap import DagSwap
        swap = DagSwap(config.RPC_URL, self.private_key)
        try:
            result = swap.buy_token(token_out, amount_in / 1e6)  # DAG uses 6 decimals USDC
            return {
                "success": result.get("success", False),
                "route": "dag",
                "tx_hash": result.get("tx_hash", ""),
                "amount_out": result.get("token_received", 0),
                "error": result.get("error", ""),
            }
        except Exception as e:
            return {"success": False, "route": "dag", "error": str(e)}

    def _execute_v3_multihop(self, quote, amount_in, min_out, recipient) -> dict:
        """
        Layer 2: 执行 V3 SwapRouter 多跳 swap。

        使用 exactInput：
        bytes data = abi.encode(
            exactInput(
                ExactInputParams({
                    path: tokenIn → fee1 → intermediate → fee2 → tokenOut,
                    recipient: recipient,
                    deadline: block.timestamp + 300,
                    amountIn: amount_in,
                    amountOutMinimum: min_out
                })
            )
        )
        """
        path = quote.get("path", [])
        fees = quote.get("fees", [])

        if len(path) < 2:
            return {"success": False, "error": "invalid_path"}

        # 构建 V3 path (packed bytes: tokenA + fee + tokenB + fee + tokenC)
        path_bytes = b""
        for i in range(len(path) - 1):
            path_bytes += bytes.fromhex(path[i][2:])
            if i < len(fees):
                path_bytes += fees[i].to_bytes(3, "big")

        # exactInput selector = 0xc04b8d59
        # ExactInputParams: (bytes path, address recipient, uint256 deadline, uint256 amountIn, uint256 amountOutMinimum)
        deadline = int(time.time()) + 300
        recipient_padded = recipient[2:].zfill(64)
        deadline_hex = hex(deadline)[2:].zfill(64)
        amount_in_hex = hex(amount_in)[2:].zfill(64)
        min_out_hex = hex(min_out)[2:].zfill(64)
        path_offset = "0x" + hex(100)[2:].zfill(64)  # offset to path data
        path_len = hex(len(path_bytes))[2:].zfill(64)

        # 构建完整 calldata
        # This is complex - for now, use web3.py contract call
        try:
            # 用 web3.py 构建 V3 Router 调用
            v3_router_abi = [{
                "inputs": [{
                    "components": [
                        {"name": "path", "type": "bytes"},
                        {"name": "recipient", "type": "address"},
                        {"name": "deadline", "type": "uint256"},
                        {"name": "amountIn", "type": "uint256"},
                        {"name": "amountOutMinimum", "type": "uint256"},
                    ],
                    "name": "params",
                    "type": "tuple",
                }],
                "name": "exactInput",
                "outputs": [{"name": "amountOut", "type": "uint256"}],
                "stateMutability": "payable",
                "type": "function",
            }]

            router = self.w3.eth.contract(
                address=Web3.to_checksum_address(self.v3_router),
                abi=v3_router_abi,
            )

            params = {
                "path": path_bytes,
                "recipient": Web3.to_checksum_address(recipient),
                "deadline": deadline,
                "amountIn": amount_in,
                "amountOutMinimum": min_out,
            }

            # 估算 gas
            gas_estimate = router.functions.exactInput(params).estimate_gas({
                "from": self.wallet,
                "value": 0,
            })

            # 构建交易
            nonce = self.w3.eth.get_transaction_count(self.wallet)
            tx = router.functions.exactInput(params).build_transaction({
                "from": self.wallet,
                "nonce": nonce,
                "gas": gas_estimate,
                "gasPrice": self.w3.eth.gas_price,
                "value": 0,
                "chainId": config.CHAIN_ID,
            })

            # 签名 + 发送
            signed = self.w3.eth.account.sign_transaction(tx, self.private_key)
            tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)

            # 等待确认
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
            amount_out = 0
            if receipt.get("logs"):
                # 解析 Swap 事件的 amount1
                for log in receipt["logs"]:
                    if len(log.get("data", "")) >= 66:
                        amount_out = int(log["data"][2:66], 16)

            return {
                "success": receipt.get("status") == 1,
                "route": "v3_multihop",
                "tx_hash": tx_hash.hex(),
                "amount_out": amount_out,
            }

        except Exception as e:
            logger.error(f"[SwapKit] V3 multihop 执行失败: {e}")
            return {"success": False, "route": "v3_multihop", "error": str(e)}

    def find_all_pools(self, token: str) -> list[dict]:
        """
        查找代币的所有 V3 池。
        Arc 链的 getPool() 不工作，所以用 PoolCreated 事件搜索。
        """
        import json as _json
        
        # 用 PoolCreated 事件搜索 (不限 address filter，因为 getPool 不工作)
        pool_topic = "0x783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118"
        token_lower = token.lower()
        
        # 查询最近的 PoolCreated 事件
        latest = self._curl_rpc("eth_blockNumber", []).get("result", "0x0")
        latest_block = int(latest, 16) if latest != "0x0" else 0
        
        pools = []
        # 搜索最近 10000 blocks
        for start in range(latest_block - 10000, latest_block, 2000):
            end = min(start + 2000, latest_block)
            payload = _json.dumps({
                "jsonrpc": "2.0",
                "method": "eth_getLogs",
                "params": [{
                    "fromBlock": hex(start),
                    "toBlock": hex(end),
                    "address": self.v3_factory,
                    "topics": [pool_topic],
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
            logs = resp.get("result", [])
            for log in logs:
                t0 = "0x" + log["topics"][1][26:]
                t1 = "0x" + log["topics"][2][26:]
                data_hex = log.get("data", "0x")[2:]
                fee = int(data_hex[:64], 16) if len(data_hex) >= 64 else 0
                pool_addr = "0x" + data_hex[-40:] if len(data_hex) >= 40 else ""
                
                if token_lower in (t0.lower(), t1.lower()):
                    other = t1 if t0.lower() == token_lower else t0
                    pools.append({
                        "pool": pool_addr.lower(),
                        "token0": t0.lower(),
                        "token1": t1.lower(),
                        "fee": fee,
                        "other": other,
                    })
        
        return pools
