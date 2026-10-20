"""
Arc Sniper — 直接买入脚本
=========================
绕过 scanner，直接在链上执行买入操作。

用法:
    python -m arc_sniper.direct_buy --token <address> --amount 20
    python -m arc_sniper.direct_buy --token <address> --amount 20 --via tolly
    python -m arc_sniper.direct_buy --list   # 列出已知代币
"""

import argparse
import logging
import time
import sys
import os
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web3 import Web3
from arc_sniper import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger("arc.buy")

# ═══════════════════════════════════════════════════════════════
# 已知代币列表 (从 Twitter/Square 获取 9/16)
# ═══════════════════════════════════════════════════════════════
KNOWN_TOKENS = {
    "TOLLY": {
        "address": "0xbc43ce8dec648ea298c4275559b81d6261c90b67",
        "dex": "tolly",
        "dex_address": "0x114d574247342eafea95b7a9234e62984d48cD9E",
        "mcap": "$25M",
        "desc": "Tolly 发射台代币",
    },
    "WARP": {
        "address": "0x384c60f98ecd4c26345499345c03d677e40f115e",
        "dex": "warp",
        "dex_address": "",
        "mcap": "unknown",
        "desc": "WARP 发射台代币",
    },
    "ARGUS": {
        "address": "",  # 需要从 GMGN 获取
        "dex": "unknown",
        "dex_address": "",
        "mcap": "$34M",
        "desc": "ArgusPad 平台币, 24h +890%",
    },
}


def get_wallet(w3):
    """获取钱包实例"""
    pk = config.PRIVATE_KEY
    if not pk:
        logger.error("❌ 未设置 ARC_PRIVATE_KEY")
        sys.exit(1)
    acct = w3.eth.account.from_key(pk)
    return acct


def check_balance(w3, wallet_addr):
    """检查 USDC 余额"""
    balance = w3.eth.get_balance(wallet_addr)
    usdc = balance / 10**18
    logger.info(f"💰 钱包: {wallet_addr}")
    logger.info(f"💰 USDC 余额: {usdc:.3f}")
    return usdc


def get_token_info(w3, token_addr):
    """获取代币信息"""
    contract = w3.eth.contract(
        address=Web3.to_checksum_address(token_addr),
        abi=config.ERC20_ABI,
    )
    try:
        name = contract.functions.name().call()
        symbol = contract.functions.symbol().call()
        decimals = contract.functions.decimals().call()
        supply = contract.functions.totalSupply().call()
        our_balance = contract.functions.balanceOf(
            Web3.to_checksum_address("0x8A966Db06aD9624a0E41bb4adfD817c5e7e55F54")
        ).call()
        return {
            "name": name,
            "symbol": symbol,
            "decimals": decimals,
            "total_supply": supply,
            "our_balance": our_balance,
        }
    except Exception as e:
        logger.error(f"获取代币信息失败: {e}")
        return None


def buy_via_tolly_dex(w3, acct, token_addr, amount_usdc):
    """
    通过 Tolly DEX (0x114d...) 买入。
    
    Tolly DEX 使用自定义 swap 函数 (selector 0xd984396a)。
    大部分交易通过这个函数完成，参数是 ABI 编码的 multicall 数据。
    
    由于 DEX 接口非标准，我们采用另一种方式：
    直接发送 native USDC 到 DEX，附带 token 地址作为 calldata。
    如果 DEX 支持 payable swap，这应该能工作。
    
    如果不行，我们回退到直接通过 Uniswap V3 Router 买入。
    """
    dex_addr = Web3.to_checksum_address(config.TOLLY_DEX)
    token_addr = Web3.to_checksum_address(token_addr)
    wallet = acct.address
    
    logger.info(f"🚀 尝试通过 Tolly DEX 买入")
    logger.info(f"  Token: {token_addr}")
    logger.info(f"  Amount: ${amount_usdc} USDC")
    logger.info(f"  DEX: {dex_addr}")
    
    # 方法1: 尝试 swap(uint256 amount, address token) = 0xd3986f08
    # 发送 native USDC 作为 value
    amount_wei = int(amount_usdc * 10**18)
    
    # 构建交易 - 发送 native USDC + calldata 指定要买的 token
    # 尝试几种常见的 swap 函数签名
    
    # swap(address) = 0x1f0464d1 + padded address (payable)
    # buy(address) = 0xcce7ec13 + padded address (payable)
    # swap(uint256, address) = 0xd3986f08
    
    swap_attempts = [
        # (selector, description, value, data_suffix)
        ("0xcce7ec13", "buy(address)", amount_wei, token_addr[2:].lower().zfill(64)),
        ("0xd004f0f7", "swap(address,uint256)", 0, token_addr[2:].lower().zfill(64) + hex(amount_wei)[2:].zfill(64)),
        ("0x1f0464d1", "swap(address) payable", amount_wei, token_addr[2:].lower().zfill(64)),
        ("0xd3986f08", "swap(uint256,address)", 0, hex(amount_wei)[2:].zfill(64) + token_addr[2:].lower().zfill(64)),
    ]
    
    for selector, desc, value, suffix in swap_attempts:
        data = selector + suffix
        logger.info(f"  尝试 {desc}...")
        
        try:
            # Static call first to check if it reverts
            w3.eth.call({
                "to": dex_addr,
                "from": wallet,
                "value": value,
                "data": data,
            })
            logger.info(f"  ✅ Static call succeeded!")
        except Exception as e:
            err = str(e)
            if "revert" in err.lower() or "0xe1368a84" in err:
                logger.debug(f"  ❌ {desc} reverted")
                continue
            else:
                logger.debug(f"  ❌ {desc}: {err[:60]}")
                continue
        
        # Send the actual transaction
        try:
            nonce = w3.eth.get_transaction_count(wallet)
            tx = {
                "from": wallet,
                "to": dex_addr,
                "value": value,
                "data": bytes.fromhex(data[2:]),
                "nonce": nonce,
                "gas": config.GAS_LIMIT,
                "gasPrice": w3.eth.gas_price,
                "chainId": config.CHAIN_ID,
            }
            signed = acct.sign_transaction(tx)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            logger.info(f"  📤 TX 发送: {tx_hash.hex()}")
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
            
            if receipt["status"] == 1:
                logger.info(f"  ✅ 买入成功! TX: {tx_hash.hex()}")
                return {
                    "success": True,
                    "tx_hash": tx_hash.hex(),
                    "method": desc,
                }
            else:
                logger.error(f"  ❌ TX 回滚")
                continue
        except Exception as e:
            logger.error(f"  ❌ 发送失败: {str(e)[:80]}")
            continue
    
    return {"success": False, "error": "所有 Tolly DEX swap 方法都失败了"}


def buy_via_uniswap_v3(w3, acct, token_addr, amount_usdc, fee=3000):
    """
    通过 Uniswap V3 SwapRouter 买入。
    
    使用 exactInputSingle：
      tokenIn = native USDC (0x0)
      tokenOut = token_addr
      fee = 3000 (0.3%)
      recipient = wallet
      amountIn = amount_usdc
      amountOutMin = 0 (临时)
      sqrtPriceLimitX96 = 0
    """
    router_addr = Web3.to_checksum_address(config.UNISWAP_V3_ROUTER)
    token_addr = Web3.to_checksum_address(token_addr)
    wallet = acct.address
    native = Web3.to_checksum_address(config.NATIVE_TOKEN)
    
    logger.info(f"🚀 尝试通过 Uniswap V3 Router 买入")
    logger.info(f"  Router: {router_addr}")
    logger.info(f"  Token: {token_addr}")
    logger.info(f"  Amount: ${amount_usdc} USDC (native)")
    logger.info(f"  Fee: {fee}")
    
    amount_in = int(amount_usdc * 10**18)
    deadline = int(time.time()) + 120
    
    # exactInputSingle with native USDC as tokenIn
    # Function: exactInputSingle((address,address,uint24,address,uint256,uint256,uint256,uint160))
    # Selector: 0x414bf389
    # Params are ABI-encoded as a tuple
    
    from eth_abi import encode
    # Encode the tuple
    params = (
        native,           # tokenIn (native USDC)
        token_addr,       # tokenOut
        fee,              # fee
        wallet,           # recipient
        deadline,         # deadline
        amount_in,        # amountIn
        0,                # amountOutMin (accept any, will improve later)
        0,                # sqrtPriceLimitX96
    )
    
    # ABI encode as a single tuple
    encoded_params = encode(
        ["(address,address,uint24,address,uint256,uint256,uint256,uint160)"],
        [params]
    )
    data = "0x414bf389" + encoded_params.hex()
    
    # Static call to check
    try:
        result = w3.eth.call({
            "to": router_addr,
            "from": wallet,
            "value": amount_in,  # Send native USDC as value
            "data": bytes.fromhex(data[2:]),
        })
        logger.info(f"  ✅ Static call succeeded! Output: {result.hex()[:40]}")
    except Exception as e:
        err = str(e)
        logger.error(f"  ❌ Static call failed: {err[:100]}")
        return {"success": False, "error": f"V3 router call failed: {err[:100]}"}
    
    # Send the transaction
    try:
        nonce = w3.eth.get_transaction_count(wallet)
        tx = {
            "from": wallet,
            "to": router_addr,
            "value": amount_in,
            "data": bytes.fromhex(data[2:]),
            "nonce": nonce,
            "gas": config.GAS_LIMIT,
            "gasPrice": w3.eth.gas_price,
            "chainId": config.CHAIN_ID,
        }
        signed = acct.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        logger.info(f"  📤 TX 发送: {tx_hash.hex()}")
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
        
        if receipt["status"] == 1:
            logger.info(f"  ✅ 买入成功! TX: {tx_hash.hex()}")
            # Check token balance
            token_contract = w3.eth.contract(address=token_addr, abi=config.ERC20_ABI)
            balance = token_contract.functions.balanceOf(wallet).call()
            decimals = token_contract.functions.decimals().call()
            human_balance = balance / 10**decimals
            logger.info(f"  📦 收到 {human_balance} {token_contract.functions.symbol().call()}")
            return {
                "success": True,
                "tx_hash": tx_hash.hex(),
                "token_balance": human_balance,
            }
        else:
            logger.error(f"  ❌ TX 回滚")
            return {"success": False, "error": "V3 router tx reverted"}
    except Exception as e:
        logger.error(f"  ❌ 发送失败: {str(e)[:100]}")
        return {"success": False, "error": str(e)}


def buy_via_direct_transfer(w3, acct, token_addr, amount_usdc):
    """
    最后回退方案：如果 DEX 有简单的 buy 函数，直接发送 USDC 并附带 calldata。
    """
    # Try sending USDC to the Tolly DEX with the token address as data
    dex_addr = Web3.to_checksum_address(config.TOLLY_DEX)
    token_addr = Web3.to_checksum_address(token_addr)
    wallet = acct.address
    amount_wei = int(amount_usdc * 10**18)
    
    logger.info(f"🚀 尝试直接发送 USDC + token 地址 calldata")
    
    # Just send USDC with token address as data
    data = bytes.fromhex(token_addr[2:].lower().zfill(64))
    
    try:
        nonce = w3.eth.get_transaction_count(wallet)
        tx = {
            "from": wallet,
            "to": dex_addr,
            "value": amount_wei,
            "data": data,
            "nonce": nonce,
            "gas": config.GAS_LIMIT,
            "gasPrice": w3.eth.gas_price,
            "chainId": config.CHAIN_ID,
        }
        signed = acct.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        logger.info(f"  📤 TX: {tx_hash.hex()}")
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
        
        if receipt["status"] == 1:
            logger.info(f"  ✅ 成功!")
            return {"success": True, "tx_hash": tx_hash.hex()}
        else:
            logger.error(f"  ❌ 回滚")
            return {"success": False, "error": "direct transfer reverted"}
    except Exception as e:
        logger.error(f"  ❌ {e}")
        return {"success": False, "error": str(e)}


def main():
    parser = argparse.ArgumentParser(description="Arc Sniper — 直接买入")
    parser.add_argument("--token", help="代币合约地址")
    parser.add_argument("--symbol", help="已知代币符号 (TOLLY/WARP/ARGUS)")
    parser.add_argument("--amount", type=float, default=20, help="买入金额 (USDC)")
    parser.add_argument("--via", choices=["tolly", "uniswap", "auto"], default="auto",
                       help="DEX 路径")
    parser.add_argument("--list", action="store_true", help="列出已知代币")
    args = parser.parse_args()
    
    if args.list:
        print("已知代币:")
        for sym, info in KNOWN_TOKENS.items():
            print(f"  {sym}: {info['address'][:12]}... mcap={info['mcap']} ({info['desc']})")
        return
    
    # 确定代币地址
    token_addr = args.token
    if not token_addr and args.symbol:
        info = KNOWN_TOKENS.get(args.symbol.upper())
        if info:
            token_addr = info["address"]
            if not token_addr:
                logger.error(f"❌ {args.symbol} 地址未知，需要从 GMGN 获取")
                return
        else:
            logger.error(f"❌ 未知代币: {args.symbol}")
            return
    
    if not token_addr:
        logger.error("❌ 请指定 --token <address> 或 --symbol <TOLLY|WARP|ARGUS>")
        return
    
    # 连接 RPC
    w3 = Web3(Web3.HTTPProvider(config.RPC_URL))
    if not w3.is_connected():
        logger.error(f"❌ RPC 连接失败: {config.RPC_URL}")
        logger.info("Arc 主网 RPC 不稳定，稍后重试")
        return
    
    logger.info(f"✅ RPC 连接成功, block={w3.eth.block_number}")
    
    # 获取钱包
    acct = get_wallet(w3)
    balance = check_balance(w3, acct.address)
    
    if balance < args.amount + 5:
        logger.error(f"❌ 余额不足: {balance:.2f} < {args.amount + 5:.2f}")
        return
    
    # 获取代币信息
    info = get_token_info(w3, token_addr)
    if info:
        logger.info(f"📋 代币: {info['name']} (${info['symbol']})")
        logger.info(f"  Decimals: {info['decimals']}")
        logger.info(f"  Total supply: {info['total_supply'] / 10**info['decimals']:,.0f}")
        if info["our_balance"] > 0:
            logger.info(f"  当前持仓: {info['our_balance'] / 10**info['decimals']}")
    
    # 执行买入
    if args.via == "tolly":
        result = buy_via_tolly_dex(w3, acct, token_addr, args.amount)
    elif args.via == "uniswap":
        result = buy_via_uniswap_v3(w3, acct, token_addr, args.amount)
    elif args.via == "auto":
        # 先试 Tolly DEX，再试 Uniswap V3
        logger.info("🔄 自动模式: 先试 Tolly DEX...")
        result = buy_via_tolly_dex(w3, acct, token_addr, args.amount)
        if not result.get("success"):
            logger.info("🔄 Tolly DEX 失败，尝试 Uniswap V3...")
            result = buy_via_uniswap_v3(w3, acct, token_addr, args.amount)
            if not result.get("success"):
                logger.info("🔄 V3 失败，尝试直接转账...")
                result = buy_via_direct_transfer(w3, acct, token_addr, args.amount)
    
    # 结果
    if result.get("success"):
        logger.info(f"\n🎉 买入成功!")
        logger.info(f"  TX: {result.get('tx_hash', '')}")
        if result.get("token_balance"):
            logger.info(f"  收到: {result['token_balance']}")
    else:
        logger.error(f"\n❌ 买入失败: {result.get('error', 'unknown')}")


if __name__ == "__main__":
    main()
