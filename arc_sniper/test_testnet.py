#!/usr/bin/env python3
"""
Arc 链 Testnet 测试脚本
======================
在 Arc testnet 上测试 NFT mint + 代币买入流程。

使用:
    export ARC_PRIVATE_KEY="你的测试网私钥"
    python -m arc_sniper.test_testnet

或指定 RPC:
    python -m arc_sniper.test_testnet --rpc https://rpc.testnet.arc.io
"""

import argparse
import logging
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web3 import Web3
from arc_sniper import config
from arc_sniper.nft_minter import NFTMinter
from arc_sniper.executor import Executor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger("arc.test")


def main():
    parser = argparse.ArgumentParser(description="Arc Testnet 测试")
    parser.add_argument("--rpc", default=None, help="Testnet RPC URL")
    parser.add_argument("--nft", default=None, help="NFT 合约地址（测试 mint）")
    parser.add_argument("--token", default=None, help="代币合约地址（测试买入）")
    parser.add_argument("--dry-run", action="store_true", default=True,
                         help="只查不买（默认开启）")
    parser.add_argument("--execute", action="store_true",
                         help="实际执行 mint/buy（覆盖 --dry-run）")
    args = parser.parse_args()

    rpc_url = args.rpc or config.RPC_URL
    logger.info(f"🧪 Arc Testnet 测试")
    logger.info(f"  RPC: {rpc_url}")
    logger.info(f"  Chain ID: {config.CHAIN_ID}")
    logger.info(f"  Execute: {args.execute}")

    # 连接
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    try:
        connected = w3.is_connected()
    except Exception:
        connected = False

    if not connected:
        logger.error(f"❌ 无法连接 RPC: {rpc_url}")
        logger.info("提示: Arc testnet RPC 可能还没有公开")
        logger.info("尝试: https://rpc.testnet.arc.io 或 https://testnet.rpc.arc-scan.org")
        sys.exit(1)

    chain_id = w3.eth.chain_id
    block = w3.eth.block_number
    logger.info(f"✅ RPC 连接成功!")
    logger.info(f"  Chain ID: {chain_id}")
    logger.info(f"  当前区块: {block}")

    # 检查私钥
    if not config.PRIVATE_KEY:
        logger.error("❌ 未设置 ARC_PRIVATE_KEY")
        logger.info("export ARC_PRIVATE_KEY='你的测试网私钥'")
        sys.exit(1)

    wallet = w3.eth.account.from_key(config.PRIVATE_KEY)
    logger.info(f"  钱包地址: {wallet.address}")

    # 查余额
    balance_wei = w3.eth.get_balance(wallet.address)
    decimals = getattr(config, 'USDC_DECIMALS', 18)
    balance_usdc = balance_wei / (10 ** decimals)
    logger.info(f"  原生余额: {balance_wei} wei = ${balance_usdc:.6f} USDC")

    if balance_usdc < 1:
        logger.warning("⚠️ 余额接近 0，需要先去 faucet 领测试 USDC")
        logger.info("  Arc testnet faucet: https://faucet.testnet.arc.io")
        logger.info("  或 Circle faucet")

    # ── NFT 测试 ──
    if args.nft:
        logger.info(f"\n🎨 NFT 测试: {args.nft}")
        minter = NFTMinter(w3, config.PRIVATE_KEY)

        if args.execute:
            result = minter.try_mint(args.nft, source="manual_test")
            logger.info(f"  结果: {result}")
        else:
            # 只查询不 mint
            try:
                contract = w3.eth.contract(
                    address=Web3.to_checksum_address(args.nft),
                    abi=minter.ERC721_ABI if hasattr(minter, 'ERC721_ABI') else None,
                )
                # 这里需要用 nft_minter 的 ABI
                from arc_sniper.nft_minter import ERC721_ABI
                contract = w3.eth.contract(
                    address=Web3.to_checksum_address(args.nft),
                    abi=ERC721_ABI,
                )
                info = minter._get_mint_info(contract)
                logger.info(f"  名称: {info['name']} ({info['symbol']})")
                logger.info(f"  供应: {info['total_supply']}/{info['max_supply'] or '∞'}")
                logger.info(f"  价格: ${info['mint_price']:.2f}")
                logger.info(f"  活跃: {info['mint_active']}")
                logger.info(f"  剩余: {info['remaining']}")
                logger.info("  (dry-run 模式，不执行 mint)")
            except Exception as e:
                logger.error(f"  查询失败: {e}")

    # ── 代币买入测试 ──
    if args.token:
        logger.info(f"\n🪙 代币测试: {args.token}")
        executor = Executor(w3)
        token_balance = executor.get_token_balance(args.token)
        logger.info(f"  当前持仓: {token_balance}")

        if args.execute:
            result = executor.buy_token(
                args.token, config.BUY_AMOUNT_USDC,
                dex_type="uniswap_v3", pair_address="",
            )
            logger.info(f"  结果: {result}")
        else:
            logger.info("  (dry-run 模式，不执行买入)")

    # ── 链上基础信息 ──
    if not args.nft and not args.token:
        logger.info(f"\n📊 链上基础信息:")
        logger.info(f"  Chain ID: {chain_id}")
        logger.info(f"  当前区块: {block}")
        logger.info(f"  Gas Price: {w3.eth.gas_price}")
        logger.info(f"  钱包: {wallet.address}")
        logger.info(f"  余额: ${balance_usdc:.6f} USDC")
        logger.info(f"\n提示:")
        logger.info(f"  测试 NFT mint: python -m arc_sniper.test_testnet --nft <合约地址> --execute")
        logger.info(f"  测试代币买入: python -m arc_sniper.test_testnet --token <合约地址> --execute")
        logger.info(f"  先领测试币: https://faucet.testnet.arc.io")

    logger.info("\n✅ 测试完成")


if __name__ == "__main__":
    main()
