"""
Executor — 通过 DAG Router 执行买入操作。

核心功能：
    1. 检查 USDC 余额
    2. 通过 DAG Router swap 买入代币
    3. 返回交易哈希和买入数量

设计原则：
    - 只买不卖 — 离场由用户自行操作
    - 私钥从环境变量读取，本地签名，通过 RPC 广播
    - Gas 用 USDC 支付（Arc 链特性）
    - 所有 DEX 类型统一走 DAG Router（唯一验证可用的路径）

历史:
    2026-09-16: DAG Router swap 首次成功 (TX 0x542984fc...)
                5 USDC → 564.09 TOLLY，Gas 188,955 @ 120 Gwei
    旧路径 (Tolly DEX / WARP / Uniswap V3) 全部废弃，
    统一走 DAG Router uniswapV3SwapTo 函数。
"""

import logging
import time
from web3 import Web3
from typing import Optional
from . import config

logger = logging.getLogger("arc.executor")

MAX_UINT256 = 2**256 - 1


class Executor:
    """web3.py 买入执行器 — DAG Router 统一路径。"""

    def __init__(self, w3: Web3):
        self.w3 = w3
        self.private_key = config.PRIVATE_KEY
        if not self.private_key:
            raise ValueError("ARC_PRIVATE_KEY 环境变量未设置")

        self.account = w3.eth.account.from_key(self.private_key)
        self.wallet_address = self.account.address
        logger.info(f"[Executor] 钱包地址: {self.wallet_address}")

        # USDC 合约实例（如果 USDC 是 ERC20）
        if config.USDC_ADDRESS and config.USDC_ADDRESS != config.NATIVE_TOKEN:
            self.usdc_contract = w3.eth.contract(
                address=Web3.to_checksum_address(config.USDC_ADDRESS),
                abi=config.ERC20_ABI,
            )
        else:
            self.usdc_contract = None  # USDC 是原生代币，不需要合约实例

    # ═══════════════════════════════════════════════════
    # 余额查询
    # ═══════════════════════════════════════════════════

    def get_usdc_balance(self) -> float:
        """查询钱包 USDC 余额（人类可读）"""
        if self.usdc_contract:
            # ERC20 USDC
            balance = self.usdc_contract.functions.balanceOf(self.wallet_address).call()
            decimals = self.usdc_contract.functions.decimals().call()
            return balance / (10 ** decimals)
        else:
            # 原生 USDC（Arc 链原生代币，18 位小数）
            balance = self.w3.eth.get_balance(self.wallet_address)
            return balance / (10 ** getattr(config, 'USDC_DECIMALS', 18))

    def get_token_balance(self, token_address: str) -> float:
        """查询某代币持仓数量"""
        contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(token_address),
            abi=config.ERC20_ABI,
        )
        balance = contract.functions.balanceOf(self.wallet_address).call()
        decimals = contract.functions.decimals().call()
        return balance / (10 ** decimals)

    # ═══════════════════════════════════════════════════
    # 授权（保留用于 ERC20 USDC，原生 USDC 不需要）
    # ═══════════════════════════════════════════════════

    def ensure_approval(self, spender_address: str) -> bool:
        """确保 DEX 合约有足够的 USDC 授权额度。"""
        if not self.usdc_contract:
            return True  # 原生 USDC 不需要 approve

        current = self.usdc_contract.functions.allowance(
            self.wallet_address, spender_address
        ).call()

        if current > config.BUY_AMOUNT_USDC * (10 ** 6):
            return True

        logger.info(f"[Executor] 授权 USDC 给 {spender_address[:10]}...")
        try:
            nonce = self.w3.eth.get_transaction_count(self.wallet_address)
            tx = self.usdc_contract.functions.approve(
                spender_address, MAX_UINT256
            ).build_transaction({
                "from": self.wallet_address,
                "nonce": nonce,
                "gas": 100000,
                "gasPrice": self.w3.eth.gas_price,
                "chainId": config.CHAIN_ID,
            })
            signed = self.account.sign_transaction(tx)
            tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
            self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
            logger.info(f"[Executor] ✅ 授权成功 tx={tx_hash.hex()[:18]}...")
            return True
        except Exception as e:
            logger.error(f"[Executor] 授权失败: {e}")
            return False

    # ═══════════════════════════════════════════════════
    # 买入 — 统一走 DAG Router
    # ═══════════════════════════════════════════════════

    def buy_token(self, token_address: str, usdc_amount: float,
                  dex_type: str = "dag", pair_address: str = "") -> dict:
        """
        买入代币 — 统一通过 DAG Router 执行。

        参数:
            token_address: 要买入的代币合约地址
            usdc_amount:   花费 USDC 金额（美元）
            dex_type:      DEX 类型（保留接口兼容，实际忽略，统一走 DAG）
            pair_address:  交易对/池地址（可选，DAG 自动发现 pool path）

        返回:
            {
                "success": bool,
                "tx_hash": str,
                "token_received": float,
                "usdc_spent": float,
                "error": str,
            }
        """
        if usdc_amount <= 0:
            return {"success": False, "error": "买入金额必须 > 0"}

        # 检查余额
        balance = self.get_usdc_balance()
        if balance < usdc_amount + 5:
            return {
                "success": False,
                "error": f"USDC 余额不足: {balance:.2f} < {usdc_amount + 5:.2f}",
            }

        logger.info(
            f"[Executor] 买入 {token_address[:10]}... "
            f"金额=${usdc_amount} (DAG Router)"
        )

        return self._buy_via_dag(token_address, usdc_amount, pair_address)

    def _buy_via_dag(self, token_address: str, usdc_amount: float,
                     pair_address: str = "") -> dict:
        """
        通过 DAG Router (0x4e3bcce2...) 买入 — 唯一验证可用的 swap 路径。

        使用 uniswapV3SwapTo 函数，pool path 从链上成功交易动态提取。
        成功验证: TX 0x542984fc... (5 USDC → 564.09 TOLLY, 2026-09-16)
        """
        try:
            from .dag_swap import DagSwap

            swap = DagSwap(
                rpc_url=config.RPC_URL,
                private_key=self.private_key,
                wallet_address=self.wallet_address,
            )

            result = swap.buy(token_address, usdc_amount)

            if result.get("success"):
                logger.info(
                    f"[Executor] ✅ DAG swap 成功 "
                    f"tx={result.get('tx_hash', '')[:18]}... "
                    f"收到 {result.get('token_received', 0):.4f}"
                )

            return result

        except Exception as e:
            logger.error(f"[Executor] DAG swap 异常: {e}")
            return {"success": False, "error": str(e)}
