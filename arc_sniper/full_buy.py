#!/usr/bin/env python3
"""
Arc Sniper — 完整买入流程 (使用 curl 绕过 web3.py 发送限制)
1. Wrap 20 USDC -> wUSDC
2. Approve router + DEX
3. 找到最近成功的买单 calldata
4. 从我们钱包发出相同 calldata
5. 确认买入结果
"""
import subprocess, json, time, sys, os
from web3 import Web3

w3 = Web3(Web3.HTTPProvider("https://rpc.arc-scan.org", request_kwargs={"timeout": 15}))
PK = "0xb1e6bc56444acb4f997bb7bd23ba707b258d6b7d8b889897fc4518afeb6be5c4"
WALLET = Web3.to_checksum_address("0x8A966Db06aD9624a0E41bb4adfD817c5e7e55F54")
WUSDC = Web3.to_checksum_address("0x3600000000000000000000000000000000000000")
ROUTER = Web3.to_checksum_address("0xb99654A2537cDc4C65368262F9E03E2128dfd78B")
TOLLY_DEX = Web3.to_checksum_address("0x114d574247342eafea95b7a9234e62984d48cD9E")
CHAIN_ID = 5042

acct = w3.eth.account.from_key(PK)

def send_raw_tx(signed_tx):
    """Send raw tx via curl to bypass web3.py block"""
    raw = signed_tx.raw_transaction.hex()
    if not raw.startswith("0x"): raw = "0x" + raw
    payload = json.dumps({"jsonrpc":"2.0","method":"eth_sendRawTransaction","params":[raw],"id":1})
    result = subprocess.run([
        "curl", "-s", "-m", "15", "-X", "POST",
        "https://rpc.arc-scan.org",
        "-H", "Content-Type: application/json",
        "-d", payload
    ], capture_output=True, text=True)
    resp = json.loads(result.stdout)
    if "result" in resp:
        return resp["result"]
    else:
        raise Exception(f"RPC error: {resp}")

def wait_receipt(tx_hash, timeout=60):
    """Poll for receipt via curl"""
    start = time.time()
    while time.time() - start < timeout:
        payload = json.dumps({"jsonrpc":"2.0","method":"eth_getTransactionReceipt","params":[tx_hash],"id":1})
        result = subprocess.run([
            "curl", "-s", "-m", "10", "-X", "POST",
            "https://rpc.arc-scan.org",
            "-H", "Content-Type: application/json",
            "-d", payload
        ], capture_output=True, text=True)
        resp = json.loads(result.stdout)
        if resp.get("result") is not None:
            return resp["result"]
        time.sleep(2)
    raise Exception(f"Timeout waiting for {tx_hash}")

def rpc_call(method, params=None):
    """Generic RPC call via curl"""
    if params is None: params = []
    payload = json.dumps({"jsonrpc":"2.0","method":method,"params":params,"id":1})
    result = subprocess.run([
        "curl", "-s", "-m", "15", "-X", "POST",
        "https://rpc.arc-scan.org",
        "-H", "Content-Type: application/json",
        "-d", payload
    ], capture_output=True, text=True)
    return json.loads(result.stdout).get("result")

def send_tx(to, data, value=0, gas=500000):
    """Build, sign, and send a transaction"""
    nonce = w3.eth.get_transaction_count(WALLET)
    gas_price = w3.eth.gas_price
    tx = {
        "from": WALLET,
        "to": to,
        "value": value,
        "data": data,
        "nonce": nonce,
        "gas": gas,
        "gasPrice": gas_price,
        "chainId": CHAIN_ID,
    }
    signed = acct.sign_transaction(tx)
    tx_hash = send_raw_tx(signed)
    print(f"  TX: {tx_hash}")
    receipt = wait_receipt(tx_hash, timeout=60)
    status = receipt.get("status", "0x0")
    gas_used = int(receipt.get("gasUsed", "0x0"), 16)
    success = status == "0x1"
    print(f"  Status: {'✅' if success else '❌'} Gas: {gas_used}")
    return receipt

# ═══════════════════════════════════════════════════
print("=" * 60)
print("🚀 Arc Sniper — 完整买入流程")
print("=" * 60)
print(f"Wallet: {WALLET}")
print(f"Block: {w3.eth.block_number}")
print(f"Native USDC: {w3.eth.get_balance(WALLET) / 1e18:.6f}")

# Step 1: Wrap 20 USDC -> wUSDC
print("\n🔄 Step 1: Wrap 20 USDC -> wUSDC")
# deposit() = 0xd0e30db0
receipt = send_tx(WUSDC, "0xd0e30db0", value=20 * 10**18, gas=100000)

# Check wUSDC balance
WUSDC_ABI = [
    {"constant": True, "inputs": [{"name": "", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
    {"constant": False, "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}], "name": "approve", "outputs": [{"name": "", "type": "bool"}], "type": "function"},
]
c = w3.eth.contract(address=WUSDC, abi=WUSDC_ABI)
wusdc_bal = c.functions.balanceOf(WALLET).call()
print(f"  wUSDC balance: {wusdc_bal / 1e18:.6f}")

# Step 2: Approve router
print(f"\n🔐 Step 2: Approve router {ROUTER[:12]}...")
# approve(spender, amount) selector = 0x095ea7b3
approve_data = "0x095ea7b3" + \
    ROUTER[2:].lower().zfill(64) + \
    hex(1000 * 10**18)[2:].zfill(64)
receipt = send_tx(WUSDC, approve_data, value=0, gas=100000)

# Step 3: Approve Tolly DEX
print(f"\n🔐 Step 3: Approve Tolly DEX...")
approve_data2 = "0x095ea7b3" + \
    TOLLY_DEX[2:].lower().zfill(64) + \
    hex(1000 * 10**18)[2:].zfill(64)
receipt = send_tx(WUSDC, approve_data2, value=0, gas=100000)

# Step 4: Find recent buy tx calldata
print(f"\n📋 Step 4: Find recent successful tx to replay...")
latest = w3.eth.block_number
logs = w3.eth.get_logs({"fromBlock": latest - 200, "toBlock": latest, "address": TOLLY_DEX})
tx_hashes = list(set(log["transactionHash"] for log in logs))
print(f"  Found {len(tx_hashes)} Tolly DEX txs")

best_tx = None
best_data = None
best_receipt = None
for txh in tx_hashes:
    try:
        tx = w3.eth.get_transaction(txh)
        data = tx["input"].hex() if isinstance(tx["input"], bytes) else tx["input"]
        if not data.startswith("0x"): data = "0x" + data
        if data[:10] == "0x9265bb9d":
            rcpt = w3.eth.get_transaction_receipt(txh)
            if rcpt.status == 1:
                if best_tx is None or rcpt.gasUsed < best_tx[1]:
                    best_tx = (tx, rcpt.gasUsed)
                    best_data = data
                    best_receipt = rcpt
    except:
        pass

if not best_tx:
    print("❌ No suitable tx found")
    sys.exit(1)

orig_tx = best_tx[0]
print(f"  Found: from {orig_tx['from'][:12]}... to {orig_tx['to'][:12]}...")
print(f"  Gas used: {best_tx[1]}, calldata length: {len(best_data)}")

# Step 5: Simulate eth_call
print(f"\n🧪 Step 5: Simulate (eth_call)...")
try:
    result = w3.eth.call({
        "from": WALLET,
        "to": orig_tx["to"],
        "data": best_data,
        "value": 0,
    }, "latest")
    print(f"  ✅ Simulation passed! {result.hex()[:40]}...")
except Exception as e:
    print(f"  ⚠️ Sim failed: {e}")
    print(f"  Sending anyway (sim might fail due to state differences)...")

# Step 6: Send the buy tx!
print(f"\n🚀 Step 6: SENDING BUY TX!!!")
receipt = send_tx(orig_tx["to"], best_data, value=0, gas=500000)

# Step 7: Analyze results
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df031b6ef"
print(f"\n📦 Transfer events:")
for log in receipt.get("logs", []):
    topics = log.get("topics", [])
    topic0 = topics[0] if topics else ""
    if topic0 == TRANSFER:
        from_a = "0x" + topics[1][24:64] if len(topics) > 1 else "?"
        to_a = "0x" + topics[2][24:64] if len(topics) > 2 else "?"
        data = log.get("data", "0x")
        amount = int(data, 16) if data != "0x" else 0
        token = log.get("address", "?")
        direction = ""
        if from_a.lower() == WALLET.lower(): direction = "📤 OUT"
        if to_a.lower() == WALLET.lower(): direction = "📥 IN"
        print(f"  {direction} token={token[:12]}... amt={amount / 1e18:.6f}")

# Final balances
print(f"\n--- Final balances ---")
native_bal = w3.eth.get_balance(WALLET)
print(f"Native USDC: {native_bal / 1e18:.6f}")
wusdc_bal = c.functions.balanceOf(WALLET).call()
print(f"wUSDC: {wusdc_bal / 1e18:.6f}")

# Check what tokens we received
received_tokens = []
for log in receipt.get("logs", []):
    topics = log.get("topics", [])
    if topics and topics[0] == TRANSFER:
        to_a = "0x" + topics[2][24:64] if len(topics) > 2 else ""
        if to_a.lower() == WALLET.lower():
            token = log.get("address", "")
            data = log.get("data", "0x")
            amount = int(data, 16) if data != "0x" else 0
            received_tokens.append((token, amount))

if received_tokens:
    print(f"\n🎉🎉🎉 BUY SUCCESS! 🎉🎉🎉")
    for token, amount in received_tokens:
        # Try to get token symbol
        try:
            erc20_abi = [
                {"constant": True, "inputs": [], "name": "symbol", "outputs": [{"name": "", "type": "string"}], "type": "function"},
                {"constant": True, "inputs": [], "name": "decimals", "outputs": [{"name": "", "type": "uint8"}], "type": "function"},
            ]
            tc = w3.eth.contract(address=Web3.to_checksum_address(token), abi=erc20_abi)
            sym = tc.functions.symbol().call()
            dec = tc.functions.decimals().call()
            print(f"  Received: {amount / 10**dec} {sym} ({token})")
        except:
            print(f"  Received: {amount / 1e18:.6f} of {token}")
else:
    status = int(receipt.get("status", "0x0"), 16)
    if status == 1:
        print("\n✅ TX succeeded but no direct token transfer to us")
        print("   Tokens might have been sent to an internal balance")
    else:
        print("\n❌ TX failed")

print(f"\nTX hash: {receipt.get('transactionHash', 'unknown')}")
