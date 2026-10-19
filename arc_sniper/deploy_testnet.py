#!/usr/bin/env python3
"""
Testnet 全流程测试：部署代币 + DEX → 买入 → 查余额 → 卖出 → 验证
"""
import json
import os
import time
from web3 import Web3
from solcx import compile_source

# 连接 testnet
w3 = Web3(Web3.HTTPProvider('https://rpc.testnet.arc.io'))
pk = os.environ.get('ARC_PRIVATE_KEY', '0xb1e6bc56444acb4f997bb7bd23ba707b258d6b7d8b889897fc4518afeb6be5c4')
acct = w3.eth.account.from_key(pk)
addr = acct.address
print(f'钱包: {addr}')
print(f'余额: {w3.eth.get_balance(addr) / 10**18:.6f} USDC')
print(f'Chain ID: {w3.eth.chain_id}')
print()

# 编译合约
with open('arc_sniper/contracts/TestToken.sol') as f:
    source = f.read()

print('🔨 编译合约...')
compiled = compile_source(source, output_values=['abi', 'bin'])
token_interface = compiled['<stdin>:TestToken']
dex_interface = compiled['<stdin>:SimpleDEX']

token_abi = token_interface['abi']
token_bin = token_interface['bin']
dex_abi = dex_interface['abi']
dex_bin = dex_interface['bin']

# ── 1. 部署 TestToken ──
print('\n1️⃣ 部署 TestToken...')
Token = w3.eth.contract(abi=token_abi, bytecode=token_bin)
nonce = w3.eth.get_transaction_count(addr)
tx = Token.constructor().build_transaction({
    'from': addr,
    'nonce': nonce,
    'gas': 500000,
    'gasPrice': w3.eth.gas_price,
    'chainId': w3.eth.chain_id,
})
signed = acct.sign_transaction(tx)
tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
token_addr = receipt['contractAddress']
print(f'   ✅ Token 部署成功: {token_addr}')
print(f'   TX: {tx_hash.hex()[:20]}...')
print(f'   Gas: {receipt["gasUsed"]}')

token_contract = w3.eth.contract(address=token_addr, abi=token_abi)
name = token_contract.functions.name().call()
symbol = token_contract.functions.symbol().call()
supply = token_contract.functions.totalSupply().call()
print(f'   名称: {name} (${symbol})')
print(f'   总供应: {supply / 10**18:,.0f}')

# ── 2. 部署 SimpleDEX ──
print('\n2️⃣ 部署 SimpleDEX...')
nonce = w3.eth.get_transaction_count(addr)
DEX = w3.eth.contract(abi=dex_abi, bytecode=dex_bin)
tx = DEX.constructor(token_contract.address).build_transaction({
    'from': addr,
    'nonce': nonce,
    'gas': 500000,
    'gasPrice': w3.eth.gas_price,
    'chainId': w3.eth.chain_id,
})
signed = acct.sign_transaction(tx)
tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
dex_addr = receipt['contractAddress']
print(f'   ✅ DEX 部署成功: {dex_addr}')
print(f'   Gas: {receipt["gasUsed"]}')

dex_contract = w3.eth.contract(address=dex_addr, abi=dex_abi)

# ── 3. 给 DEX 注入代币流动性 + USDC 流动性 ──
print('\n3️⃣ 注入流动性...')
# 转 100,000 ATT 到 DEX
nonce = w3.eth.get_transaction_count(addr)
tx = token_contract.functions.transfer(
    dex_addr, 100000 * 10**18
).build_transaction({
    'from': addr, 'nonce': nonce, 'gas': 100000,
    'gasPrice': w3.eth.gas_price, 'chainId': w3.eth.chain_id,
})
signed = acct.sign_transaction(tx)
tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
print(f'   ✅ 100,000 ATT → DEX (gas: {receipt["gasUsed"]})')

# 注入 5 USDC 到 DEX（支持卖出）
nonce = w3.eth.get_transaction_count(addr)
tx = {
    'from': addr, 'to': dex_addr, 'value': 5 * 10**18,
    'nonce': nonce, 'gas': 100000,
    'gasPrice': w3.eth.gas_price, 'chainId': w3.eth.chain_id,
}
signed = acct.sign_transaction(tx)
tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
print(f'   ✅ 5 USDC → DEX (gas: {receipt["gasUsed"]})')

dex_token_bal = token_contract.functions.balanceOf(dex_addr).call()
dex_usdc_bal = w3.eth.get_balance(dex_addr)
print(f'   DEX 持有: {dex_token_bal/10**18:,.0f} ATT + {dex_usdc_bal/10**18:.2f} USDC')

# ── 4. 测试买入 ──
print('\n4️⃣ 测试买入 (1 USDC → ATT)...')
buy_amount_wei = 1 * 10**18  # 1 USDC
nonce = w3.eth.get_transaction_count(addr)
tx = dex_contract.functions.buy().build_transaction({
    'from': addr,
    'nonce': nonce,
    'gas': 200000,
    'gasPrice': w3.eth.gas_price,
    'chainId': w3.eth.chain_id,
    'value': buy_amount_wei,
})
signed = acct.sign_transaction(tx)
tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
print(f'   ✅ 买入成功! TX: {tx_hash.hex()[:20]}...')
print(f'   Gas: {receipt["gasUsed"]}')

# 查余额
my_token_bal = token_contract.functions.balanceOf(addr).call()
my_usdc_bal = w3.eth.get_balance(addr)
print(f'   我的 ATT 余额: {my_token_bal / 10**18:,.2f}')
print(f'   我的 USDC 余额: {my_usdc_bal / 10**18:.6f}')

# ── 5. 测试卖出 ──
print('\n5️⃣ 测试卖出 (50 ATT → USDC)...')
sell_amount = 50 * 10**18

# 先 approve DEX
nonce = w3.eth.get_transaction_count(addr)
tx = token_contract.functions.approve(dex_addr, sell_amount).build_transaction({
    'from': addr, 'nonce': nonce, 'gas': 100000,
    'gasPrice': w3.eth.gas_price, 'chainId': w3.eth.chain_id,
})
signed = acct.sign_transaction(tx)
tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
print(f'   ✅ Approve 成功 (gas: {receipt["gasUsed"]})')

# 执行卖出
nonce = w3.eth.get_transaction_count(addr)
tx = dex_contract.functions.sell(sell_amount).build_transaction({
    'from': addr, 'nonce': nonce, 'gas': 200000,
    'gasPrice': w3.eth.gas_price, 'chainId': w3.eth.chain_id,
})
signed = acct.sign_transaction(tx)
tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
print(f'   ✅ 卖出成功! TX: {tx_hash.hex()[:20]}...')
print(f'   Gas: {receipt["gasUsed"]}')

# 最终余额
final_token = token_contract.functions.balanceOf(addr).call()
final_usdc = w3.eth.get_balance(addr)
print(f'\n📊 最终余额:')
print(f'   ATT: {final_token / 10**18:,.2f}')
print(f'   USDC: {final_usdc / 10**18:.6f}')

# ── 6. 验证 executor 能读取余额 ──
print('\n6️⃣ 验证 Executor 读取余额...')
from arc_sniper.executor import Executor
from arc_sniper import config
config.CHAIN_ID = 5042002
executor = Executor(w3)
usdc_bal = executor.get_usdc_balance()
token_bal = executor.get_token_balance(token_addr)
print(f'   Executor USDC: ${usdc_bal:.6f}')
print(f'   Executor ATT: {token_bal:.2f}')

# ── 保存部署地址 ──
result = {
    'token_address': token_addr,
    'dex_address': dex_addr,
    'wallet': addr,
    'token_name': name,
    'token_symbol': symbol,
}
with open('arc_sniper/.testnet_contracts.json', 'w') as f:
    json.dump(result, f, indent=2)
print(f'\n✅ 全部测试通过! 合约地址已保存到 .testnet_contracts.json')
print(f'   Token: {token_addr}')
print(f'   DEX: {dex_addr}')
print(f'\n💡 9/16 主网上线时，把 token/DEX 地址替换为真实合约即可流畅运行')
