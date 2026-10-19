"""
策略 A：复制一笔已有的 Tolly DEX 买单，从我们钱包发出。

原理：
- Tolly DEX (0x114d...) 使用 selector 0xd984396a 的 multicall 函数
- 所有 tx 的 value=0，说明 DEX 通过 transferFrom 拉取 wUSDC
- 我们需要先 wrap native USDC -> wUSDC (0x3600...)，然后 approve DEX，最后复制 calldata

步骤：
1. 等 RPC 恢复
2. 获取一笔成功的小额 TOLLY 买单
3. 解析 calldata 中的关键参数（买入金额、token 地址）
4. 修改 recipient 为我们的钱包地址
5. wrap USDC -> wUSDC
6. approve DEX 使用 wUSDC
7. 发送修改后的 calldata
8. 确认买入结果
"""

import sys, os, time, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web3 import Web3
from arc_sniper import config

w3 = Web3(Web3.HTTPProvider(config.RPC_URL))

PK = config.PRIVATE_KEY
WALLET = Web3.to_checksum_address("0x8A966Db06aD9624a0E41bb4adfD817c5e7e55F54")
TOLLY_DEX = Web3.to_checksum_address("0x114d574247342eafea95b7a9234e62984d48cD9E")
TOLLY_TOKEN = Web3.to_checksum_address("0xbc43ce8dec648ea298c4275559b81d6261c90b67")
WUSDC = Web3.to_checksum_address("0x3600000000000000000000000000000000000000")

# wUSDC ABI (deposit = wrap native USDC)
WUSDC_ABI = [
    {"constant": False, "inputs": [], "name": "deposit", "outputs": [], "stateMutability": "payable", "type": "function"},
    {"constant": False, "inputs": [{"name": "amount", "type": "uint256"}], "name": "withdraw", "outputs": [], "type": "function"},
    {"constant": True, "inputs": [{"name": "", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "decimals", "outputs": [{"name": "", "type": "uint8"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "symbol", "outputs": [{"name": "", "type": "string"}], "type": "function"},
    # approve
    {"constant": False, "inputs": [
        {"name": "spender", "type": "address"},
        {"name": "amount", "type": "uint256"}
    ], "name": "approve", "outputs": [{"name": "", "type": "bool"}], "type": "function"},
    {"constant": True, "inputs": [
        {"name": "owner", "type": "address"},
        {"name": "spender", "type": "address"}
    ], "name": "allowance", "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
]

TOLLY_DEX_ABI = [
    {"constant": True, "inputs": [], "name": "owner", "outputs": [{"name": "", "type": "address"}], "type": "function"},
]

def wait_rpc(max_retries=10):
    for i in range(max_retries):
        try:
            if w3.is_connected():
                return True
        except:
            pass
        print(f"RPC retry {i+1}/{max_retries}...")
        time.sleep(5)
    return False

def get_wallet():
    acct = w3.eth.account.from_key(PK)
    return acct

def check_balance(addr):
    bal = w3.eth.get_balance(addr)
    print(f"Native USDC: {bal / 10**18:.3f}")
    return bal

def check_wusdc(addr):
    c = w3.eth.contract(address=WUSDC, abi=WUSDC_ABI)
    bal = c.functions.balanceOf(addr).call()
    print(f"wUSDC: {bal / 10**18:.3f}")
    return bal

def wrap_usdc(acct, amount_wei):
    """Wrap native USDC to wUSDC (0x3600...)"""
    c = w3.eth.contract(address=WUSDC, abi=WUSDC_ABI)
    nonce = w3.eth.get_transaction_count(acct.address)
    tx = c.functions.deposit().build_transaction({
        "from": acct.address,
        "value": amount_wei,
        "nonce": nonce,
        "gas": 100000,
        "gasPrice": w3.eth.gas_price,
        "chainId": config.CHAIN_ID,
    })
    signed = acct.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
    print(f"Wrap tx: {tx_hash.hex()} status={'✅' if receipt.status==1 else '❌'}")
    return receipt.status == 1

def approve_wusdc(acct, spender, amount):
    """Approve DEX to spend our wUSDC"""
    c = w3.eth.contract(address=WUSDC, abi=WUSDC_ABI)
    # Check current allowance
    current = c.functions.allowance(acct.address, spender).call()
    if current >= amount:
        print(f"Already approved: {current/10**18:.3f}")
        return True
    
    nonce = w3.eth.get_transaction_count(acct.address)
    tx = c.functions.approve(spender, amount).build_transaction({
        "from": acct.address,
        "nonce": nonce,
        "gas": 100000,
        "gasPrice": w3.eth.gas_price,
        "chainId": config.CHAIN_ID,
    })
    signed = acct.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
    print(f"Approve tx: {tx_hash.hex()} status={'✅' if receipt.status==1 else '❌'}")
    return receipt.status == 1

def find_simple_buy_tx():
    """Find a simple TOLLY buy transaction to copy"""
    latest = w3.eth.block_number
    logs = w3.eth.get_logs({"fromBlock": latest - 100, "toBlock": latest, "address": TOLLY_DEX})
    tx_hashes = list(set(log["transactionHash"] for log in logs))
    
    best = None
    for tx_hash in tx_hashes[:20]:
        try:
            tx = w3.eth.get_transaction(tx_hash)
            receipt = w3.eth.get_transaction_receipt(tx_hash)
            data = tx["input"]
            if isinstance(data, bytes):
                data = data.hex()
            if receipt.status == 1 and data[:10] == "0xd984396a" and receipt.gasUsed < 270000:
                if best is None or receipt.gasUsed < best[1]:
                    best = (tx_hash, receipt.gasUsed, tx, receipt, data)
        except:
            pass
    
    return best

def replay_tx(acct, original_tx, original_data):
    """
    Copy the calldata from a successful buy tx and send from our wallet.
    
    The key question: does the calldata contain the buyer's address (msg.sender)?
    If yes, we need to replace it. If the DEX uses msg.sender internally,
    then just sending the same calldata from our wallet should work.
    """
    # The calldata is a multicall. The DEX (0x114d) uses msg.sender to:
    # 1. Pull wUSDC from the caller via transferFrom
    # 2. Send bought tokens to the caller
    # So if we send the same calldata, the DEX should pull from our wallet
    # and send tokens to us.
    
    # BUT: the calldata might contain hardcoded addresses (like the token to buy)
    # and amounts. We need to check if our wallet address is anywhere in the data.
    
    wallet_hex = acct.address[2:].lower()
    if wallet_hex in original_data.lower():
        print(f"⚠️  Wallet address found in calldata - need to replace")
        # Replace the original buyer's address with ours
        original_from = original_tx["from"][2:].lower()
        new_data = original_data.lower().replace(original_from, wallet_hex)
        print(f"  Replaced {original_from[:12]}... -> {wallet_hex[:12]}...")
    else:
        print(f"✅ Wallet not in calldata - DEX uses msg.sender")
        new_data = original_data
    
    # Send the transaction
    nonce = w3.eth.get_transaction_count(acct.address)
    tx = {
        "from": acct.address,
        "to": TOLLY_DEX,
        "value": 0,  # All txs have value=0
        "data": bytes.fromhex(new_data[2:]),
        "nonce": nonce,
        "gas": 500000,
        "gasPrice": w3.eth.gas_price,
        "chainId": config.CHAIN_ID,
    }
    
    signed = acct.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"📤 Replay tx: {tx_hash.hex()}")
    
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
    print(f"Status: {'✅ SUCCESS' if receipt.status == 1 else '❌ FAILED'}")
    print(f"Gas used: {receipt.gasUsed}")
    
    # Check what tokens we received
    for log in receipt.logs:
        topic0 = log["topics"][0].hex() if log["topics"] else ""
        if topic0 == "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df031b6ef":
            from_a = "0x" + log["topics"][1].hex()[24:64]
            to_a = "0x" + log["topics"][2].hex()[24:64]
            amount = int(log["data"], 16) if log["data"] != "0x" else 0
            token = log["address"]
            if to_a.lower() == acct.address.lower():
                token_name = "TOLLY" if token.lower() == TOLLY_TOKEN.lower() else token[:10]
                print(f"  📦 Received: {amount / 10**18:.6f} {token_name}")
            elif from_a.lower() == acct.address.lower():
                token_name = "wUSDC" if token.lower() == WUSDC.lower() else token[:10]
                print(f"  💸 Spent: {amount / 10**18:.6f} {token_name}")
    
    return receipt.status == 1

def main():
    print("=" * 60)
    print("🚀 Arc Sniper — 策略 A: 复制买单")
    print("=" * 60)
    
    # Wait for RPC
    if not wait_rpc():
        print("❌ RPC 不可用")
        return
    
    print(f"✅ RPC 连接, block={w3.eth.block_number}")
    
    # Get wallet
    acct = get_wallet()
    print(f"Wallet: {acct.address}")
    check_balance(acct.address)
    
    # Step 1: Find a simple buy tx to copy
    print("\n📋 寻找简单的买单...")
    result = find_simple_buy_tx()
    if not result:
        print("❌ 没找到合适的买单")
        return
    
    tx_hash, gas, tx, receipt, data = result
    print(f"找到: {tx_hash.hex()[:20]}... gas={gas} datalen={len(data)}")
    
    # Step 2: Check what token was bought
    print("\n📋 交易详情:")
    for log in receipt.logs:
        topic0 = log["topics"][0].hex() if log["topics"] else ""
        if topic0 == "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df031b6ef":
            from_a = "0x" + log["topics"][1].hex()[24:64]
            to_a = "0x" + log["topics"][2].hex()[24:64]
            amount = int(log["data"], 16) if log["data"] != "0x" else 0
            token = log["address"]
            if token.lower() == TOLLY_TOKEN.lower():
                token_name = "TOLLY"
            elif token.lower() == WUSDC.lower():
                token_name = "wUSDC"
            else:
                token_name = token[:10]
            print(f"  {token_name}: {from_a[:12]}... -> {to_a[:12]}... amt={amount/10**18:.6f}")
    
    # Step 3: We need wUSDC for the DEX to pull via transferFrom
    # First check if we already have wUSDC
    wusdc_bal = check_wusdc(acct.address)
    
    # The original tx spent some wUSDC. Let's estimate how much we need
    # Look at the transfer events to find how much wUSDC was spent
    wusdc_spent = 0
    for log in receipt.logs:
        topic0 = log["topics"][0].hex() if log["topics"] else ""
        if topic0 == "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df031b6ef":
            from_a = "0x" + log["topics"][1].hex()[24:64]
            token = log["address"]
            if from_a.lower() == tx["from"].lower() and token.lower() == WUSDC.lower():
                wusdc_spent = int(log["data"], 16)
    
    if wusdc_spent > 0:
        print(f"\n💰 原始交易花费: {wusdc_spent / 10**18:.6f} wUSDC")
    
    # We'll only spend a small amount. Let's pick a tx that spends a small amount
    # If the original tx spent too much, we might need to find a smaller one
    # For now, let's wrap 20 USDC and try
    
    wrap_amount = 20 * 10**18  # 20 USDC
    
    # Actually, we can't just copy the calldata if the amount is hardcoded
    # The calldata contains the swap amount in the encoded params
    # Let me check: word[6] in the first tx was 1280000000 (1.28 USDC)
    # That might be the amount in wUSDC (with 18 decimals = 1.28 USDC)
    
    # Let's extract the amount from the calldata
    # Based on the pattern: word[6] seems to be the swap amount
    params = data[10:]
    words = [params[i:i+64] for i in range(0, len(params), 64)]
    if len(words) > 6:
        word6 = int(words[6], 16)
        print(f"  word[6] (possible amount): {word6} ({word6/10**18:.6f} USDC)")
        
        # Use this amount for wrapping
        if word6 > 0 and word6 < 100 * 10**18:
            wrap_amount = word6 + int(word6 * 0.1)  # 10% extra for gas + slippage
            print(f"  Will wrap: {wrap_amount/10**18:.6f} USDC")
    
    # Step 4: Wrap USDC -> wUSDC
    if wusdc_bal < wrap_amount:
        print(f"\n🔄 Wrapping {wrap_amount/10**18:.6f} USDC -> wUSDC...")
        if not wrap_usdc(acct, wrap_amount):
            print("❌ Wrap failed")
            return
        wusdc_bal = check_wusdc(acct.address)
    else:
        print(f"\n✅ 已有足够 wUSDC: {wusdc_bal/10**18:.6f}")
    
    # Step 5: Approve DEX to spend wUSDC
    print(f"\n🔐 Approving DEX to spend wUSDC...")
    approve_amount = 1000 * 10**18  # Approve 1000 USDC
    if not approve_wusdc(acct, TOLLY_DEX, approve_amount):
        print("❌ Approve failed")
        return
    
    # Step 6: Replay the transaction
    print(f"\n🚀 发送复制交易...")
    success = replay_tx(acct, tx, data)
    
    if success:
        print("\n🎉 买入成功！")
        # Check our TOLLY balance
        erc20 = [{"constant": True, "inputs": [{"name": "", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
                 {"constant": True, "inputs": [], "name": "symbol", "outputs": [{"name": "", "type": "string"}], "type": "function"},
                 {"constant": True, "inputs": [], "name": "decimals", "outputs": [{"name": "", "type": "uint8"}], "type": "function"}]
        tolly_c = w3.eth.contract(address=TOLLY_TOKEN, abi=erc20)
        bal = tolly_c.functions.balanceOf(acct.address).call()
        dec = tolly_c.functions.decimals().call()
        sym = tolly_c.functions.symbol().call()
        print(f"📦 {sym} 余额: {bal / 10**dec}")
    else:
        print("\n❌ 买入失败，可能需要调整 calldata")

if __name__ == "__main__":
    main()
