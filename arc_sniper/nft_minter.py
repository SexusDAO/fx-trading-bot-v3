"""
NFT Minter — Arc 链自动 NFT 公开 mint 模块。

流程：
    Twitter 监控 / Scanner 发现 NFT 合约地址
    → 验证 ERC-721 接口
    → 查 mint 状态、价格、剩余量
    → 公开 mint 开启 + 价格在预算内 → 自动 mint 1 个
    → 推送通知

限制：
    - 只处理公开 mint（publicMint / mint），白名单 mint 需要 merkle proof，不支持
    - 每个合约只 mint 一次（去重）
    - 频率控制：每小时最多 5 个 NFT
"""

import json
import logging
import os
import time
from typing import Optional
from web3 import Web3

from . import config
from .notify import _send_jarvis

logger = logging.getLogger("arc.nft_minter")

# ═══════════════════════════════════════════════════════════════
# NFT 配置
# ═══════════════════════════════════════════════════════════════

NFT_BUY_AMOUNT_USDC = 20       # 单个 NFT mint 预算上限 ($)
MAX_NFTS_PER_HOUR = 5           # 每小时最多 mint 5 个 NFT
MIN_NFT_MINT_INTERVAL_SEC = 30  # 两次 mint 之间最少 30 秒

# ERC-721 标准 ABI（只取我们需要的方法）
ERC721_ABI = [
    # 标准 ERC-165 / ERC-721 查询
    {
        "constant": True, "inputs": [{"name": "interfaceId", "type": "bytes4"}],
        "name": "supportsInterface", "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "view", "type": "function",
    },
    {
        "constant": True, "inputs": [],
        "name": "name", "outputs": [{"name": "", "type": "string"}],
        "stateMutability": "view", "type": "function",
    },
    {
        "constant": True, "inputs": [],
        "name": "symbol", "outputs": [{"name": "", "type": "string"}],
        "stateMutability": "view", "type": "function",
    },
    {
        "constant": True, "inputs": [],
        "name": "totalSupply", "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view", "type": "function",
    },
    {
        "constant": True, "inputs": [],
        "name": "maxSupply", "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view", "type": "function",
    },
    # 常见 mint 方法（不同项目命名不同，逐一尝试）
    {
        "constant": False,
        "inputs": [],
        "name": "mint",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "payable", "type": "function",
    },
    {
        "constant": False,
        "inputs": [{"name": "amount", "type": "uint256"}],
        "name": "mint",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "payable", "type": "function",
    },
    {
        "constant": False,
        "inputs": [],
        "name": "publicMint",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "payable", "type": "function",
    },
    {
        "constant": False,
        "inputs": [{"name": "amount", "type": "uint256"}],
        "name": "publicMint",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "payable", "type": "function",
    },
    # mintPrice (非标准，常见于 Arc/ETH NFT)
    {
        "constant": True, "inputs": [],
        "name": "mintPrice", "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view", "type": "function",
    },
    # price (备选)
    {
        "constant": True, "inputs": [],
        "name": "price", "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view", "type": "function",
    },
    # mintActive / isMintActive
    {
        "constant": True, "inputs": [],
        "name": "mintActive", "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "view", "type": "function",
    },
    {
        "constant": True, "inputs": [],
        "name": "isMintActive", "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "view", "type": "function",
    },
    # balanceOf（用于确认 mint 成功）
    {
        "constant": True,
        "inputs": [{"name": "owner", "type": "address"}],
        "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view", "type": "function",
    },
    # ownerOf（确认拥有哪个 token ID）
    {
        "constant": True,
        "inputs": [{"name": "tokenId", "type": "uint256"}],
        "name": "ownerOf", "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view", "type": "function",
    },
]

# ERC-721 接口 ID
ERC721_INTERFACE_ID = "0x80ac58cd"
# ERC-1155 接口 ID
ERC1155_INTERFACE_ID = "0xd9b67a26"

# 已知 NFT 合约地址（项目方公布后写入）
KNOWN_NFT_ADDRS = {
    # ArclingsNFT / ArcadiansNFT 合约地址待确认
}

# 已 mint 的合约（去重）
_MINTED_FILE = os.path.join(config.BASE_DIR, ".nft_minted.json")
_minted_contracts: set = set()


def _load_minted():
    if os.path.exists(_MINTED_FILE):
        try:
            return set(json.load(open(_MINTED_FILE)))
        except Exception:
            pass
    return set()


def _save_minted():
    items = list(_minted_contracts)[-2000:]
    json.dump(items, open(_MINTED_FILE, "w"))


_minted_contracts = _load_minted()
logger.info(f"[NFT] 已加载 {len(_minted_contracts)} 个已 mint 合约")


# ═══════════════════════════════════════════════════════════════
# NFT Minter 主类
# ═══════════════════════════════════════════════════════════════

class NFTMinter:
    """Arc 链 NFT 自动 mint 模块。"""

    def __init__(self, w3: Web3, private_key: str):
        self.w3 = w3
        self.private_key = private_key
        self.account = w3.eth.account.from_key(private_key)
        self.wallet_address = self.account.address
        self.last_mint_time = 0.0
        self.mint_history: list[float] = []

        logger.info(f"[NFT] Minter 初始化，钱包: {self.wallet_address}")

    # ── 频率控制 ──────────────────────────────────────────────

    def _can_mint(self) -> tuple[bool, str]:
        now = time.time()
        if now - self.last_mint_time < MIN_NFT_MINT_INTERVAL_SEC:
            return False, f"频率限制：还需等待 {MIN_NFT_MINT_INTERVAL_SEC - (now - self.last_mint_time):.0f}s"

        self.mint_history = [t for t in self.mint_history if now - t < 3600]
        if len(self.mint_history) >= MAX_NFTS_PER_HOUR:
            return False, f"小时限额：已 mint {len(self.mint_history)}/{MAX_NFTS_PER_HOUR} 个"

        return True, ""

    def _record_mint(self):
        self.last_mint_time = time.time()
        self.mint_history.append(self.last_mint_time)

    # ── 合约验证 ─────────────────────────────────────────────

    def _is_erc721(self, contract) -> bool:
        """检查合约是否实现 ERC-721 接口"""
        try:
            result = contract.functions.supportsInterface(
                bytes.fromhex(ERC721_INTERFACE_ID[2:])
            ).call()
            return result
        except Exception:
            # 某些合约没实现 ERC-165，回退检查 name + symbol + balanceOf
            try:
                contract.functions.name().call()
                contract.functions.symbol().call()
                contract.functions.balanceOf(self.wallet_address).call()
                return True  # 可能是 ERC-721 但没实现 ERC-165
            except Exception:
                return False

    def _is_erc1155(self, contract) -> bool:
        """检查合约是否实现 ERC-1155 接口"""
        try:
            result = contract.functions.supportsInterface(
                bytes.fromhex(ERC1155_INTERFACE_ID[2:])
            ).call()
            return result
        except Exception:
            return False

    # ── 查询 mint 信息 ───────────────────────────────────────

    def _get_mint_info(self, contract) -> dict:
        """
        查询 NFT 合约的 mint 相关信息。

        返回:
            {
                "name": str,
                "symbol": str,
                "total_supply": int,
                "max_supply": int,       # 0 表示无限制或不可查
                "mint_price": float,     # USDC 价格，0 表示免费
                "mint_active": bool,
                "remaining": int,       # -1 表示无法计算
            }
        """
        info = {
            "name": "", "symbol": "",
            "total_supply": 0, "max_supply": 0,
            "mint_price": 0.0, "mint_active": True,
            "remaining": -1,
        }

        # name / symbol
        try:
            info["name"] = contract.functions.name().call()
        except Exception:
            pass
        try:
            info["symbol"] = contract.functions.symbol().call()
        except Exception:
            pass

        # totalSupply
        try:
            info["total_supply"] = contract.functions.totalSupply().call()
        except Exception:
            pass

        # maxSupply
        try:
            info["max_supply"] = contract.functions.maxSupply().call()
        except Exception:
            pass

        # 计算 remaining
        if info["max_supply"] > 0:
            info["remaining"] = info["max_supply"] - info["total_supply"]

        # mintPrice / price
        for price_fn in ["mintPrice", "price"]:
            try:
                raw_price = getattr(contract.functions, price_fn)().call()
                # Arc 链原生 USDC 用 18 位小数
                decimals = getattr(config, 'USDC_DECIMALS', 18)
                info["mint_price"] = raw_price / (10 ** decimals)
                break
            except Exception:
                continue

        # mintActive / isMintActive
        for active_fn in ["mintActive", "isMintActive"]:
            try:
                info["mint_active"] = getattr(contract.functions, active_fn)().call()
                break
            except Exception:
                continue

        return info

    # ── 执行 mint ────────────────────────────────────────────

    def try_mint(self, nft_address: str, source: str = "twitter",
                 tweet_url: str = "") -> dict:
        """
        尝试 mint 一个 NFT。

        参数:
            nft_address: NFT 合约地址
            source:      信号来源 ("twitter" / "scanner" / "manual")
            tweet_url:   推文链接（通知用）

        返回:
            {
                "success": bool,
                "tx_hash": str,
                "token_id": int,
                "error": str,
            }
        """
        addr = nft_address.lower().strip()

        # 去重检查
        if addr in _minted_contracts:
            logger.info(f"[NFT] 已 mint 过: {addr[:12]}...")
            return {"success": False, "error": "已 mint 过此合约"}
        if addr in KNOWN_NFT_ADDRS:
            return {"success": False, "error": "已知合约，跳过"}

        # 频率控制
        can_mint, reason = self._can_mint()
        if not can_mint:
            logger.info(f"[NFT] 跳过: {reason}")
            return {"success": False, "error": reason}

        # 创建合约实例
        try:
            contract = self.w3.eth.contract(
                address=Web3.to_checksum_address(addr),
                abi=ERC721_ABI,
            )
        except Exception as e:
            return {"success": False, "error": f"合约实例化失败: {e}"}

        # 验证 ERC-721
        if not self._is_erc721(contract):
            if self._is_erc1155(contract):
                logger.info(f"[NFT] {addr[:12]}... 是 ERC-1155，暂不支持")
                return {"success": False, "error": "ERC-1155 暂不支持"}
            logger.warning(f"[NFT] {addr[:12]}... 不是 ERC-721")
            return {"success": False, "error": "非 ERC-721 合约"}

        # 查询 mint 信息
        info = self._get_mint_info(contract)
        name = info["name"] or "?"
        symbol = info["symbol"] or "?"

        logger.info(
            f"[NFT] {name} ({symbol}) "
            f"供应: {info['total_supply']}/{info['max_supply'] or '∞'} "
            f"价格: ${info['mint_price']:.2f} "
            f"活跃: {info['mint_active']}"
        )

        # 检查是否可 mint
        if not info["mint_active"]:
            self._notify_skip(addr, name, symbol, "mint 未开启", source, tweet_url)
            return {"success": False, "error": "mint 未开启"}

        if info["remaining"] == 0:
            self._notify_skip(addr, name, symbol, "已 mint 完", source, tweet_url)
            return {"success": False, "error": "已 mint 完"}

        if info["mint_price"] > NFT_BUY_AMOUNT_USDC:
            self._notify_skip(
                addr, name, symbol,
                f"价格 ${info['mint_price']:.2f} > 预算 ${NFT_BUY_AMOUNT_USDC}",
                source, tweet_url,
            )
            return {"success": False, "error": f"价格超预算 ${info['mint_price']:.2f}"}

        # 检查 USDC 余额
        balance = self._get_usdc_balance()
        needed = info["mint_price"] + 5  # 留 $5 给 gas
        if balance < needed:
            return {"success": False, "error": f"USDC 余额不足: {balance:.2f} < {needed:.2f}"}

        # 尝试 mint
        logger.info(f"[NFT] 尝试 mint {name} ({symbol})...")

        # 依次尝试不同的 mint 方法
        mint_methods = [
            ("publicMint", []),       # publicMint() 无参
            ("mint", []),             # mint() 无参
        ]

        for method_name, args in mint_methods:
            try:
                mint_fn = getattr(contract.functions, method_name)
                # 构建交易
                nonce = self.w3.eth.get_transaction_count(self.wallet_address)

                # mintPrice 转为 wei（Arc 原生 USDC 18 位小数）
                decimals = getattr(config, 'USDC_DECIMALS', 18)
                value_wei = int(info["mint_price"] * 10**decimals) if info["mint_price"] > 0 else 0

                tx = mint_fn(*args).build_transaction({
                    "from": self.wallet_address,
                    "nonce": nonce,
                    "gas": 200000,
                    "gasPrice": self.w3.eth.gas_price,
                    "chainId": config.CHAIN_ID,
                    "value": value_wei,  # 如果 mint 需要支付 USDC
                })

                # 签名并广播
                signed = self.account.sign_transaction(tx)
                tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
                receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

                if receipt["status"] == 1:
                    # mint 成功
                    self._record_mint()
                    _minted_contracts.add(addr)
                    _save_minted()

                    # 查询 token ID（balanceOf 增加说明成功）
                    balance_after = contract.functions.balanceOf(self.wallet_address).call()

                    logger.info(
                        f"[NFT] ✅ mint 成功! "
                        f"tx={tx_hash.hex()[:18]}... "
                        f"持有数: {balance_after}"
                    )

                    self._notify_mint_success(
                        addr, name, symbol, info,
                        tx_hash.hex(), balance_after, source, tweet_url,
                    )

                    return {
                        "success": True,
                        "tx_hash": tx_hash.hex(),
                        "token_id": -1,  # 需要从事件解析具体 token ID
                    }
                else:
                    logger.error(f"[NFT] ❌ mint 交易回滚 tx={tx_hash.hex()[:18]}...")
                    # 继续尝试下一个方法
                    continue

            except Exception as e:
                logger.debug(f"[NFT] {method_name}() 失败: {e}")
                continue

        # 所有方法都失败
        self._notify_skip(addr, name, symbol, "所有 mint 方法均失败", source, tweet_url)
        return {"success": False, "error": "所有 mint 方法均失败"}

    # ── 余额查询 ─────────────────────────────────────────────

    def _get_usdc_balance(self) -> float:
        """查询钱包 USDC 余额"""
        if config.USDC_ADDRESS and config.USDC_ADDRESS != config.NATIVE_TOKEN:
            usdc_contract = self.w3.eth.contract(
                address=Web3.to_checksum_address(config.USDC_ADDRESS),
                abi=config.ERC20_ABI,
            )
            balance = usdc_contract.functions.balanceOf(self.wallet_address).call()
            decimals = usdc_contract.functions.decimals().call()
            return balance / (10 ** decimals)
        else:
            # 原生 USDC（Arc 链，18 位小数）
            balance = self.w3.eth.get_balance(self.wallet_address)
            return balance / (10 ** getattr(config, 'USDC_DECIMALS', 18))

    # ── 通知 ──────────────────────────────────────────────────

    def _notify_mint_success(
        self, addr: str, name: str, symbol: str, info: dict,
        tx_hash: str, balance_after: int, source: str, tweet_url: str,
    ):
        """推送 mint 成功通知"""
        lines = [
            "🎨 ARC NFT Mint 成功!",
            f"名称: {name} ({symbol})",
            f"合约: {addr}",
            f"价格: ${info['mint_price']:.2f} USDC",
            f"供应: {info['total_supply']}/{info['max_supply'] or '∞'}",
            f"持有数: {balance_after}",
            f"TX: {tx_hash[:20]}...",
            f"来源: {source}",
        ]
        if tweet_url:
            lines.append(f"推文: {tweet_url}")

        _send_jarvis("\n".join(lines))

    def _notify_skip(
        self, addr: str, name: str, symbol: str,
        reason: str, source: str, tweet_url: str,
    ):
        """推送跳过通知"""
        lines = [
            f"⏭️ ARC NFT 跳过: {name} ({symbol})",
            f"原因: {reason}",
            f"合约: {addr}",
            f"来源: {source}",
        ]
        if tweet_url:
            lines.append(f"推文: {tweet_url}")

        _send_jarvis("\n".join(lines))

    def _notify_signal(
        self, addr: str, name: str, symbol: str,
        source: str, tweet_url: str,
    ):
        """推送 NFT 信号通知（仅通知不 mint）"""
        lines = [
            "🎨 ARC NFT 信号发现",
            f"名称: {name} ({symbol})",
            f"合约: {addr}",
            f"来源: {source}",
        ]
        if tweet_url:
            lines.append(f"推文: {tweet_url}")

        _send_jarvis("\n".join(lines))


# ═══════════════════════════════════════════════════════════════
# 从推文信号处理 NFT
# ═══════════════════════════════════════════════════════════════

# NFT 相关关键词（推文包含这些词时触发 NFT 检查）
NFT_KEYWORDS = [
    "nft", "mint", "minting", "drop", "collection",
    "arclings", "arcadians", "open sea", "opensea",
]

# NFT 合约地址正则（和代币相同，0x + 40 hex）
import re
NFT_CA_PATTERN = re.compile(r"0x[0-9a-fA-F]{40}")


def extract_nft_signals_from_tweets(tweets: list[dict], group: dict) -> list[dict]:
    """
    从推文中提取 NFT mint 信号。

    和代币信号提取类似，但增加了 NFT 关键词过滤。
    """
    signals = []
    accounts = set(a.lower() for a in group["accounts"])

    for tweet in tweets:
        text = tweet.get("text", "")
        text_lower = text.lower()

        # 必须包含 NFT 相关关键词
        if not any(kw in text_lower for kw in NFT_KEYWORDS):
            continue

        # 提取合约地址
        matches = NFT_CA_PATTERN.findall(text)
        if not matches:
            continue

        tweet_id = tweet.get("id", "")
        user = tweet.get("user", {})
        username = user.get("username", "")
        tweet_url = tweet.get("url", "")

        for addr in matches:
            addr_lower = addr.lower()
            # 排除已知代币地址
            from .grok_monitor import KNOWN_TOKEN_ADDRS
            if addr_lower in KNOWN_TOKEN_ADDRS:
                continue
            # 排除已知 NFT
            if addr_lower in KNOWN_NFT_ADDRS:
                continue
            # 排除已 mint 的
            if addr_lower in _minted_contracts:
                continue

            signals.append({
                "account": username,
                "tweet_url": tweet_url,
                "contract_address": addr_lower,
                "tweet_text": text[:200],
                "tweet_id": tweet_id,
            })

    return signals


# ═══════════════════════════════════════════════════════════════
# 集成入口：从 GrokMonitor 调用
# ═══════════════════════════════════════════════════════════════

def process_nft_signal(signal: dict, minter: Optional[NFTMinter], group: dict):
    """
    处理一个 NFT 信号。可被 GrokMonitor._check_group 调用。

    参数:
        signal:  extract_nft_signals_from_tweets 返回的信号
        minter:  NFTMinter 实例（None 时只通知）
        group:   监控组信息
    """
    addr = signal["contract_address"]
    account = signal.get("account", "")
    tweet_url = signal.get("tweet_url", "")

    if minter is None:
        logger.info(f"[NFT] 无 minter，仅记录 NFT 信号: {addr[:12]}...")
        return

    minter.try_mint(addr, source="twitter", tweet_url=tweet_url)
