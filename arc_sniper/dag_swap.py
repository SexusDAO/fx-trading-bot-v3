#!/usr/bin/env python3
"""
DAG Swap — Arc 链通用代币 swap 模块
=====================================
通过 DAG Router (0x4e3bcce2...) 执行 swap，支持任意 token 买入。

成功验证（2026-09-16）:
    - TX: 0x542984fc8796a26e573b8271313ffcae2cd2c65344a519afb7df31bbe2579e3b
    - 5 USDC → 564.09 TOLLY ✅
    - Gas: 188,955 @ 120 Gwei

核心发现:
    1. DAG Router 是 Arc 链上的主要 swap 路由，不是 Tolly DEX
    2. 最常用函数: uniswapV3SwapTo (0x0d5f0e3b)
    3. Gas price 必须 ≥ 100 Gwei（之前 22 Gwei 全部失败）
    4. Pool path 编码从最近成功交易提取，不是 calldata replay

用法:
    from dag_swap import DagSwap
    swap = DagSwap(rpc, private_key)
    result = swap.buy(token_address, amount_usdc)
    # → {"success": True, "tx_hash": "0x...", "token_received": 123.45, "usdc_spent": 5.0}

    # 查询 pool path
    path = swap.find_pool_path(token_address)
    # → {"pool": "0x162df51c...", "path_data": [...]}

    # 模拟 swap
    sim = swap.simulate(token_address, amount_usdc)
    # → {"success": True, "expected_output": 564.09}
"""

import json
import logging
import subprocess
import time
from typing import Optional, Union
from web3 import Web3

logger = logging.getLogger("arc.dag_swap")

# ═══════════════════════════════════════════════════════════════
# 常量
# ═══════════════════════════════════════════════════════════════

DAG_ROUTER = "0x4e3bcce28caf98a143fd8bd9e4875ccab3e7bbe0"
WUSDC = "0x3600000000000000000000000000000000000000"       # wrapped USDC (6 decimals)
NATIVE_USDC = "0xffffFFFfFFffffffffffffffFfFFFfffFFFfFFfE"  # native USDC (18 decimals on Arc)
CHAIN_ID = 5042
UNISWAP_V3_SWAP_TO_SELECTOR = "0d5f0e3b"

# Gas 配置
MIN_GAS_PRICE_GWEI = 100
DEFAULT_GAS_LIMIT = 500_000
GAS_PRICE_MULTIPLIER = 3  # eth_gasPrice × 3，至少 100 Gwei

# RPC
DEFAULT_RPC = "https://rpc.arc-scan.org"
FALLBACK_RPC = "https://rpc.mainnet.arc.io"

# 已知 pool path 缓存（token_address → pool path data）
# 从链上成功交易提取，避免每次都搜索
_POOL_PATH_CACHE: dict[str, dict] = {}

# 已知代币的 pool path（硬编码常用代币）
KNOWN_POOL_PATHS = {
    # TOLLY: 从 TX 0x03eddd... 提取
    "0xbc43ce8dec648ea298c4275559b81d6261c90b67": {
        "pool": "0x162df51c504e7b8321e07387932f333d9be16a72",
        "path": [
            "7777777711118000000000000000000000000000000007e5a06d378d9ced52a4",
            "777777771111000000000064fa00a9ed787f3793db668bff3e6e6e7db0f92a1b",
        ],
    },
}


class DagSwap:
    """通过 DAG Router 执行 swap 的通用模块。"""

    def __init__(
        self,
        rpc_url: str = DEFAULT_RPC,
        private_key: str = "",
        wallet_address: str = "",
    ):
        self.rpc = rpc_url
        self.pk = private_key
        self.wallet = Web3.to_checksum_address(wallet_address) if wallet_address else None

        if private_key:
            self.w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 20}))
            self.acct = self.w3.eth.account.from_key(private_key)
            self.wallet = self.acct.address
        else:
            self.w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 20}))

        # 验证连接
        try:
            self.chain_id = self.w3.eth.chain_id
        except Exception as e:
            raise ConnectionError(f"无法连接 RPC {rpc_url}: {e}")

    # ═══════════════════════════════════════════════════════════
    # RPC 工具
    # ═══════════════════════════════════════════════════════════

    def _curl_rpc(self, method: str, params=None, retries=2):
        """低级 RPC 调用（带 fallback）"""
        if params is None:
            params = []
        payload = json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": 1})
        for attempt in range(retries):
            for rpc in [self.rpc, FALLBACK_RPC]:
                try:
                    r = subprocess.run(
                        ["curl", "-s", "-m", "15", "-X", "POST", rpc,
                         "-H", "Content-Type: application/json", "-d", payload],
                        capture_output=True, text=True,
                    )
                    data = json.loads(r.stdout)
                    if "error" in data and "unreachable" in str(data["error"]).lower():
                        continue
                    return data
                except Exception:
                    continue
        return {"error": {"message": "RPC failed after retries"}}

    def _get_nonce(self) -> int:
        return self.w3.eth.get_transaction_count(self.wallet, "pending")

    def _get_gas_price(self) -> int:
        """获取 gas price，至少 MIN_GAS_PRICE_GWEI Gwei"""
        try:
            price = self.w3.eth.gas_price
            price = max(price * GAS_PRICE_MULTIPLIER, MIN_GAS_PRICE_GWEI * 10**9)
        except Exception:
            price = MIN_GAS_PRICE_GWEI * 10**9
        return price

    def _get_block(self) -> int:
        return self.w3.eth.block_number

    # ═══════════════════════════════════════════════════════════
    # Pool Path 发现
    # ═══════════════════════════════════════════════════════════

    def find_pool_path(self, token_address: str) -> Optional[dict]:
        """
        查找代币的 pool path。

        策略:
            1. 检查硬编码缓存 (KNOWN_POOL_PATHS)
            2. 检查运行时缓存 (_POOL_PATH_CACHE)
            3. 搜索最近成功交易提取 pool path
        """
        addr = token_address.lower().strip()

        # 1. 硬编码
        if addr in KNOWN_POOL_PATHS:
            return KNOWN_POOL_PATHS[addr]

        # 2. 运行时缓存
        if addr in _POOL_PATH_CACHE:
            return _POOL_PATH_CACHE[addr]

        # 3. 搜索最近成功交易
        path = self._discover_pool_path(addr)
        if path:
            _POOL_PATH_CACHE[addr] = path
            return path

        logger.warning(f"[DagSwap] 未找到 {addr[:12]}... 的 pool path")
        return None

    def _discover_pool_path(self, token_address: str) -> Optional[dict]:
        """
        从最近 20 个区块搜索包含该代币的成功 swap 交易，
        提取 uniswapV3SwapTo 的 pool path 参数。
        """
        latest = self._get_block()

        for block_num in range(latest, max(latest - 30, 0), -1):
            # 搜索该代币的 Transfer 事件
            resp = self._curl_rpc("eth_getLogs", [{
                "fromBlock": hex(block_num),
                "toBlock": hex(block_num),
                "address": token_address,
                "topics": [
                    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
                ],
            }])

            logs = resp.get("result", [])
            for log in logs:
                tx_hash = log.get("transactionHash")
                if not tx_hash:
                    continue

                # 检查这笔 TX 是否调用了 DAG Router
                tx_resp = self._curl_rpc("eth_getTransactionByHash", [tx_hash])
                tx = tx_resp.get("result", {})
                if not tx:
                    continue

                if tx.get("to", "").lower() != DAG_ROUTER.lower():
                    continue

                input_data = tx.get("input", "0x")
                selector = input_data[:10]

                # 只处理 uniswapV3SwapTo
                if selector != "0x" + UNISWAP_V3_SWAP_TO_SELECTOR:
                    continue

                # 解析参数
                data = input_data[10:]
                words = [data[i * 64:(i + 1) * 64] for i in range(len(data) // 64)]
                if len(words) < 8:
                    continue

                # Param 5 = pool address, Param 6-7 = path data
                pool_addr = "0x" + words[5][-40:]
                path_data = [words[6], words[7]]

                # 验证这笔交易成功
                rcpt = self._curl_rpc("eth_getTransactionReceipt", [tx_hash])
                receipt = rcpt.get("result", {})
                if receipt.get("status") == "0x1":
                    logger.info(
                        f"[DagSwap] 发现 pool path: pool={pool_addr[:12]}... "
                        f"from TX {tx_hash[:16]}..."
                    )
                    return {
                        "pool": pool_addr,
                        "path": path_data,
                    }

        return None

    # ═══════════════════════════════════════════════════════════
    # Swap 构建
    # ═══════════════════════════════════════════════════════════

    def _build_swap_calldata(
        self,
        token_address: str,
        amount_usdc: float,
        pool_path: dict,
        amount_out_min: int = 1,
    ) -> str:
        """
        构建 uniswapV3SwapTo calldata。

        参数结构 (从成功交易逆向):
            Param 0: 调用者地址 (uint256 编码)
            Param 1: amount_in (wUSDC 6 decimals)
            Param 2: amount_out_min
            Param 3: flag = 128
            Param 4: array length = 1
            Param 5: pool address
            Param 6: path data 1
            Param 7: path data 2
        """
        # wUSDC 使用 6 decimals
        amount_in = int(amount_usdc * 10**6)

        # 调用者地址作为 uint256
        sender_int = int(self.wallet, 16)

        # 构建 calldata
        parts = [
            UNISWAP_V3_SWAP_TO_SELECTOR,
            hex(sender_int)[2:].zfill(64),       # Param 0: sender
            hex(amount_in)[2:].zfill(64),         # Param 1: amount in
            hex(amount_out_min)[2:].zfill(64),    # Param 2: amount out min
            hex(128)[2:].zfill(64),               # Param 3: flag
            hex(1)[2:].zfill(64),                 # Param 4: array length
            pool_path["pool"][2:].lower().zfill(64),  # Param 5: pool address
            pool_path["path"][0],                 # Param 6: path data 1
            pool_path["path"][1],                 # Param 7: path data 2
        ]

        return "0x" + "".join(parts)

    # ═══════════════════════════════════════════════════════════
    # 模拟
    # ═══════════════════════════════════════════════════════════

    def simulate(
        self,
        token_address: str,
        amount_usdc: float,
        pool_path: Optional[dict] = None,
    ) -> dict:
        """
        模拟 swap，返回预期输出量。

        返回:
            {"success": True, "expected_output": float (token amount, 18 decimals)}
            {"success": False, "error": str}
        """
        if pool_path is None:
            pool_path = self.find_pool_path(token_address)
        if not pool_path:
            return {"success": False, "error": "未找到 pool path"}

        calldata = self._build_swap_calldata(token_address, amount_usdc, pool_path, amount_out_min=0)

        resp = self._curl_rpc("eth_call", [
            {
                "from": self.wallet,
                "to": DAG_ROUTER,
                "data": calldata,
                "value": "0x0",
            },
            "latest",
        ])

        result = resp.get("result", "")
        error = resp.get("error", "")

        if result and result != "0x":
            output = int(result, 16)
            return {
                "success": True,
                "expected_output": output / 1e18,  # 大多数 Arc 代币 18 decimals
                "expected_output_raw": output,
            }
        elif error:
            return {"success": False, "error": json.dumps(error)[:200]}
        else:
            return {"success": False, "error": f"RPC 返回空结果: {result}"}

    # ═══════════════════════════════════════════════════════════
    # 执行 Swap
    # ═══════════════════════════════════════════════════════════

    def buy(
        self,
        token_address: str,
        amount_usdc: float,
        pool_path: Optional[dict] = None,
        slippage_pct: float = 15.0,
        gas_limit: int = DEFAULT_GAS_LIMIT,
        wait_for_receipt: bool = True,
        timeout_sec: int = 120,
    ) -> dict:
        """
        执行买入 swap。

        参数:
            token_address: 目标代币合约地址
            amount_usdc:   花费 USDC 金额（美元）
            pool_path:     可选，预查询的 pool path。None 则自动查找
            slippage_pct:  最大滑点百分比（默认 15%）
            gas_limit:     Gas limit
            wait_for_receipt: 是否等待交易确认
            timeout_sec:   等待超时秒数

        返回:
            {
                "success": bool,
                "tx_hash": str,
                "token_received": float,
                "usdc_spent": float,
                "gas_used": int,
                "error": str (失败时),
            }
        """
        if not self.pk:
            return {"success": False, "error": "未设置私钥"}
        if amount_usdc <= 0:
            return {"success": False, "error": "金额必须 > 0"}

        # 1. 查找 pool path
        if pool_path is None:
            pool_path = self.find_pool_path(token_address)
        if not pool_path:
            return {"success": False, "error": f"未找到 {token_address[:12]}... 的 pool path"}

        # 2. 检查余额
        balance = self.get_usdc_balance()
        if balance < amount_usdc + 2:  # 留 $2 给 gas
            return {
                "success": False,
                "error": f"USDC 余额不足: {balance:.4f} < {amount_usdc + 2:.1f}",
            }

        # 3. 模拟 swap，获取预期输出
        sim = self.simulate(token_address, amount_usdc, pool_path)
        if not sim["success"]:
            return {"success": False, "error": f"模拟失败: {sim.get('error', '?')}"}

        expected_out = sim["expected_output_raw"]
        logger.info(
            f"[DagSwap] 模拟通过: {amount_usdc} USDC → {sim['expected_output']:.4f} token"
        )

        # 4. 计算最小输出（滑点保护）
        amount_out_min = max(1, int(expected_out * (100 - slippage_pct) / 100))

        # 5. 构建 calldata
        calldata = self._build_swap_calldata(token_address, amount_usdc, pool_path, amount_out_min)

        # 6. 获取 gas price 和 nonce
        gas_price = self._get_gas_price()
        nonce = self._get_nonce()

        logger.info(
            f"[DagSwap] 发送 swap: nonce={nonce} gas={gas_limit} "
            f"gasPrice={gas_price / 1e9:.1f} Gwei amount={amount_usdc} USDC"
        )

        # 7. 签名并发送
        try:
            tx = {
                "from": self.wallet,
                "to": Web3.to_checksum_address(DAG_ROUTER),
                "value": 0,
                "data": bytes.fromhex(calldata[2:]),
                "nonce": nonce,
                "gas": gas_limit,
                "gasPrice": gas_price,
                "chainId": self.chain_id,
            }
            signed = self.acct.sign_transaction(tx)
            raw = "0x" + signed.raw_transaction.hex()

            resp = self._curl_rpc("eth_sendRawTransaction", [raw])
            if resp.get("error"):
                return {"success": False, "error": f"发送失败: {json.dumps(resp['error'])[:150]}"}

            tx_hash = resp["result"]
            logger.info(f"[DagSwap] TX 已发送: {tx_hash}")

            if not wait_for_receipt:
                return {
                    "success": True,
                    "tx_hash": tx_hash,
                    "token_received": 0,
                    "usdc_spent": amount_usdc,
                }

            # 8. 等待 receipt
            for i in range(timeout_sec // 3):
                time.sleep(3)
                rcpt = self._curl_rpc("eth_getTransactionReceipt", [tx_hash])
                receipt = rcpt.get("result")
                if receipt:
                    status = receipt.get("status", "0x0")
                    gas_used = int(receipt.get("gasUsed", "0x0"), 16)

                    if status == "0x1":
                        # 查询收到的代币数量
                        token_bal = self.get_token_balance(token_address)
                        logger.info(
                            f"[DagSwap] ✅ Swap 成功: tx={tx_hash[:18]}... "
                            f"gas={gas_used} received={token_bal:.4f}"
                        )
                        return {
                            "success": True,
                            "tx_hash": tx_hash,
                            "token_received": token_bal,
                            "usdc_spent": amount_usdc,
                            "gas_used": gas_used,
                        }
                    else:
                        logger.error(f"[DagSwap] ❌ 交易回滚: {tx_hash[:18]}...")
                        return {
                            "success": False,
                            "error": "交易回滚",
                            "tx_hash": tx_hash,
                            "gas_used": gas_used,
                        }

            logger.warning(f"[DagSwap] ⏰ 等待 receipt 超时 ({timeout_sec}s)")
            return {
                "success": False,
                "error": f"receipt 超时 ({timeout_sec}s)",
                "tx_hash": tx_hash,
            }

        except Exception as e:
            logger.error(f"[DagSwap] 发送异常: {e}")
            return {"success": False, "error": str(e)}

    # ═══════════════════════════════════════════════════════════
    # 余额查询
    # ═══════════════════════════════════════════════════════════

    def get_usdc_balance(self) -> float:
        """查询钱包 native USDC 余额（18 decimals）"""
        bal = self.w3.eth.get_balance(self.wallet)
        return bal / 1e18

    def get_token_balance(self, token_address: str, decimals: int = 18) -> float:
        """查询代币余额"""
        bal_data = "0x70a08231" + self.wallet[2:].lower().zfill(64)
        resp = self._curl_rpc("eth_call", [
            {"to": token_address, "data": bal_data},
            "latest",
        ])
        result = resp.get("result", "0x0")
        if result == "0x" or not result:
            return 0.0
        return int(result, 16) / (10 ** decimals)

    # ═══════════════════════════════════════════════════════════
    # 卖出 Swap (Token → USDC)
    # ═══════════════════════════════════════════════════════════

    def _build_sell_calldata(
        self,
        token_address: str,
        amount_token_raw: int,
        pool_path: dict,
        amount_out_min: int = 1,
    ) -> str:
        """
        构建 卖出 uniswapV3SwapTo calldata (Token → wUSDC)。

        卖出格式 (从 TOLLY 卖出 TX 逆向):
            Param 0: sender (uint256)
            Param 1: amount_in (token, 18 decimals)
            Param 2: amount_out_min (wUSDC, 6 decimals)
            Param 3: flag = 128
            Param 4: array length = 1
            Param 5: pool address with 0x80 prefix
            Param 6: wUSDC address with 0x80 prefix
        """
        sender_int = int(self.wallet, 16)
        pool_addr = pool_path["pool"][2:].lower().zfill(64)

        parts = [
            UNISWAP_V3_SWAP_TO_SELECTOR,
            hex(sender_int)[2:].zfill(64),
            hex(amount_token_raw)[2:].zfill(64),
            hex(amount_out_min)[2:].zfill(64),
            hex(128)[2:].zfill(64),
            hex(1)[2:].zfill(64),
            "800000000000000000000000" + pool_addr[-40:],   # pool with 0x80 prefix
            "800000000000000000000000" + WUSDC[2:],          # wUSDC with 0x80 prefix
        ]
        return "0x" + "".join(parts)

    def simulate_sell(
        self,
        token_address: str,
        amount_token: float,
        pool_path: Optional[dict] = None,
        decimals: int = 18,
    ) -> dict:
        """
        模拟卖出 swap，返回预期 USDC 输出量。

        返回:
            {"success": True, "expected_usdc": float}
            {"success": False, "error": str}
        """
        if pool_path is None:
            pool_path = self.find_pool_path(token_address)
        if not pool_path:
            return {"success": False, "error": "未找到 pool path"}

        amount_raw = int(amount_token * 10**decimals)
        calldata = self._build_sell_calldata(token_address, amount_raw, pool_path)

        resp = self._curl_rpc("eth_call", [
            {"from": self.wallet, "to": DAG_ROUTER, "data": calldata, "value": "0x0"},
            "latest",
        ])
        result = resp.get("result", "")
        error = resp.get("error", "")

        if result and result != "0x":
            output = int(result, 16)
            return {
                "success": True,
                "expected_usdc": output / 1e6,   # wUSDC is 6 decimals
                "expected_usdc_raw": output,
            }
        elif error:
            return {"success": False, "error": json.dumps(error)[:200]}
        else:
            return {"success": False, "error": f"RPC 返回空: {result}"}

    def sell(
        self,
        token_address: str,
        amount_token: float,
        pool_path: Optional[dict] = None,
        slippage_pct: float = 20.0,
        decimals: int = 18,
        gas_limit: int = DEFAULT_GAS_LIMIT,
        wait_for_receipt: bool = True,
        timeout_sec: int = 120,
    ) -> dict:
        """
        执行卖出 swap (Token → USDC)。

        参数:
            token_address: 代币合约地址
            amount_token:  卖出数量 (人类可读)
            pool_path:    预查询的 pool path
            slippage_pct: 最大滑点 (默认 20%，卖出允许更大滑点)
            decimals:     代币精度 (默认 18)

        返回:
            {
                "success": bool,
                "tx_hash": str,
                "usdc_received": float,
                "token_sold": float,
                "gas_used": int,
                "error": str (失败时),
            }
        """
        if not self.pk:
            return {"success": False, "error": "未设置私钥"}
        if amount_token <= 0:
            return {"success": False, "error": "数量必须 > 0"}

        # 1. 查找 pool path
        if pool_path is None:
            pool_path = self.find_pool_path(token_address)
        if not pool_path:
            return {"success": False, "error": f"未找到 {token_address[:12]}... 的 pool path"}

        # 2. 检查代币余额
        balance = self.get_token_balance(token_address, decimals)
        if balance < amount_token:
            return {"success": False, "error": f"代币余额不足: {balance} < {amount_token}"}

        amount_raw = int(amount_token * 10**decimals)

        # 3. 模拟卖出
        sim = self.simulate_sell(token_address, amount_token, pool_path, decimals)
        if not sim["success"]:
            return {"success": False, "error": f"卖出模拟失败: {sim.get('error', '?')}"}

        expected_usdc_raw = sim["expected_usdc_raw"]
        logger.info(
            f"[DagSwap] 卖出模拟通过: {amount_token:.4f} token → {sim['expected_usdc']:.6f} USDC"
        )

        # 4. 滑点保护
        amount_out_min = max(1, int(expected_usdc_raw * (100 - slippage_pct) / 100))

        # 5. 构建 calldata
        calldata = self._build_sell_calldata(token_address, amount_raw, pool_path, amount_out_min)

        # 6. 发送交易
        gas_price = self._get_gas_price()
        nonce = self._get_nonce()

        logger.info(
            f"[DagSwap] 发送卖出: nonce={nonce} gas={gas_limit} "
            f"gasPrice={gas_price / 1e9:.1f} Gwei amount={amount_token:.4f}"
        )

        try:
            tx = {
                "from": self.wallet,
                "to": Web3.to_checksum_address(DAG_ROUTER),
                "value": 0,
                "data": bytes.fromhex(calldata[2:]),
                "nonce": nonce,
                "gas": gas_limit,
                "gasPrice": gas_price,
                "chainId": self.chain_id,
            }
            signed = self.acct.sign_transaction(tx)
            raw = "0x" + signed.raw_transaction.hex()

            resp = self._curl_rpc("eth_sendRawTransaction", [raw])
            if resp.get("error"):
                return {"success": False, "error": f"发送失败: {json.dumps(resp['error'])[:150]}"}

            tx_hash = resp["result"]
            logger.info(f"[DagSwap] 卖出 TX 已发送: {tx_hash}")

            if not wait_for_receipt:
                return {
                    "success": True, "tx_hash": tx_hash,
                    "usdc_received": 0, "token_sold": amount_token,
                }

            # 7. 等待 receipt
            for i in range(timeout_sec // 3):
                time.sleep(3)
                rcpt = self._curl_rpc("eth_getTransactionReceipt", [tx_hash])
                receipt = rcpt.get("result")
                if receipt:
                    status = receipt.get("status", "0x0")
                    gas_used = int(receipt.get("gasUsed", "0x0"), 16)

                    if status == "0x1":
                        new_usdc = self.get_usdc_balance()
                        logger.info(
                            f"[DagSwap] ✅ 卖出成功: tx={tx_hash[:18]}... gas={gas_used}"
                        )
                        return {
                            "success": True,
                            "tx_hash": tx_hash,
                            "usdc_received": sim["expected_usdc"],
                            "token_sold": amount_token,
                            "gas_used": gas_used,
                        }
                    else:
                        logger.error(f"[DagSwap] ❌ 卖出回滚: {tx_hash[:18]}...")
                        return {
                            "success": False, "error": "卖出交易回滚",
                            "tx_hash": tx_hash, "gas_used": gas_used,
                        }

            return {"success": False, "error": f"receipt 超时 ({timeout_sec}s)", "tx_hash": tx_hash}

        except Exception as e:
            logger.error(f"[DagSwap] 卖出异常: {e}")
            return {"success": False, "error": str(e)}

    # ═══════════════════════════════════════════════════════════
    # Pool path 管理
    # ═══════════════════════════════════════════════════════════

    @staticmethod
    def add_known_pool_path(token_address: str, pool: str, path: list[str]):
        """手动添加已知的 pool path"""
        KNOWN_POOL_PATHS[token_address.lower().strip()] = {
            "pool": pool,
            "path": path,
        }

    def refresh_pool_path(self, token_address: str) -> Optional[dict]:
        """强制重新搜索 pool path"""
        addr = token_address.lower().strip()
        _POOL_PATH_CACHE.pop(addr, None)
        return self._discover_pool_path(addr)


# ═══════════════════════════════════════════════════════════════
# CLI 入口
# ═══════════════════════════════════════════════════════════════

def main():
    """命令行: python3 dag_swap.py <token_address> <amount_usdc>"""
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
    )

    if len(sys.argv) < 3:
        print("用法: python3 dag_swap.py <token_address> <amount_usdc>")
        print("示例: python3 dag_swap.py 0xbc43ce8dec648ea298c4275559b81d6261c90b67 5")
        sys.exit(1)

    token = sys.argv[1]
    amount = float(sys.argv[2])

    import os
    pk = os.environ.get("ARC_PRIVATE_KEY", "0xb1e6bc56444acb4f997bb7bd23ba707b258d6b7d8b889897fc4518afeb6be5c4")

    swap = DagSwap(DEFAULT_RPC, pk)

    print(f"\n钱包: {swap.wallet}")
    print(f"USDC 余额: {swap.get_usdc_balance():.4f}")
    print(f"区块: {swap._get_block()}")

    # 查找 pool path
    print(f"\n查找 {token[:12]}... 的 pool path...")
    path = swap.find_pool_path(token)
    if path:
        print(f"  Pool: {path['pool']}")
    else:
        print("  ❌ 未找到 pool path")
        sys.exit(1)

    # 模拟
    print(f"\n模拟 {amount} USDC → {token[:12]}...")
    sim = swap.simulate(token, amount, path)
    if sim["success"]:
        print(f"  ✅ 预期输出: {sim['expected_output']:.4f} token")
    else:
        print(f"  ❌ 模拟失败: {sim['error']}")
        sys.exit(1)

    # 执行
    print(f"\n执行 swap...")
    result = swap.buy(token, amount, path)
    if result["success"]:
        print(f"\n🎉 成功!")
        print(f"  TX: {result['tx_hash']}")
        print(f"  收到: {result['token_received']:.4f} token")
        print(f"  花费: {result['usdc_spent']} USDC")
        print(f"  Gas: {result['gas_used']}")
    else:
        print(f"\n❌ 失败: {result['error']}")


if __name__ == "__main__":
    main()
