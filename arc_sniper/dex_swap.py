#!/usr/bin/env python3
"""Arc Buy — approve DEX + swapExactTokensForTokens directly on DEX"""
import os
from web3 import Web3
import json, time, subprocess

RPC = "https://rpc.arc-scan.org"
PK = os.environ["ARC_PK"]  # 不接受默认值，必须在环境变量中设置
WALLET = Web3.to_checksum_address("0x8A966Db06aD9624a0E41bb4adfD817c5e7e55F54")
TOLLY_DEX = Web3.to_checksum_address("0x114d574247342eafea95b7a9234e62984d48cD9E")
NATIVE_USDC = Web3.to_checksum_address("0xffffFFFfFFffffffffffffffFfFFFfffFFFfFFfE")
TOLLY_TOKEN = os.environ.get("ARC_SNIPER_DEX_SWAP_TOLLY_TOKEN", "")
CHAIN_ID = 5042

w3 = Web3(Web3.HTTPProvider(RPC, request_kwargs={"timeout": 20}))
acct = w3.eth.account.from_key(PK)

def curl_rpc(method, params=None):
    if params is None: params = []
    payload = json.dumps({"jsonrpc":"2.0","method":method,"params":params,"id":1})
    r = subprocess.run(["curl","-s","-m","15","-X","POST",RPC,"-H","Content-Type: application/json","-d",payload], capture_output=True, text=True)
    return json.loads(r.stdout)

def send_tx(to, data, value=0, gas=500000):
    nonce = w3.eth.get_transaction_count(WALLET)
    tx = {"from": WALLET, "to": to, "value": value, "data": data,
          "nonce": nonce, "gas": gas, "gasPrice": w3.eth.gas_price, "chainId": CHAIN_ID}
    signed = acct.sign_transaction(tx)
    raw = signed.raw_transaction.hex()
    if not raw.startswith("0x"): raw = "0x" + raw
    resp = curl_rpc("eth_sendRawTransaction", [raw])
    if "error" in resp:
        print(f"  ❌ Send error: {resp['error']}")
        return None
    tx_hash = resp["result"]
    print(f"  TX: {tx_hash}")
    for _ in range(40):
        time.sleep(3)
        r = curl_rpc("eth_getTransactionReceipt", [tx_hash])
        if r.get("result"):
            return r["result"]
    return None

print(f"Block: {w3.eth.block_number}")
print(f"Native USDC: {w3.eth.get_balance(WALLET)/1e18:.6f}")

# Step 1: Approve DEX on native USDC
print("\n🔐 Step 1: Approve DEX on native USDC")
erc20_abi = [{"constant":True,"inputs":[{"name":"o","type":"address"},{"name":"s","type":"address"}],"name":"allowance","outputs":[{"name":"","type":"uint256"}],"type":"function"}]
nc = w3.eth.contract(address=NATIVE_USDC, abi=erc20_abi)
current_allow = nc.functions.allowance(WALLET, TOLLY_DEX).call()
print(f"  Current allowance: {current_allow/1e18:.6f}")

if current_allow < 100 * 10**18:
    approve_data = "0x095ea7b3" + TOLLY_DEX[2:].lower().zfill(64) + hex(10000*10**18)[2:].zfill(64)
    rcpt = send_tx(NATIVE_USDC, approve_data, gas=100000)
    if rcpt:
        status = int(rcpt.get("status","0x0"), 16)
        print(f"  Approve: {'✅' if status==1 else '❌'}")
    else:
        print("  ❌ Approve failed")
        exit(1)
else:
    print("  Already approved")

# Step 2: Try multiple swap functions on DEX
print("\n🚀 Step 2: Try swap on DEX")
deadline = hex(int(time.time()) + 1200)
amt_in = hex(20 * 10**18)[2:].zfill(64)
amt_out_min = hex(0)[2:].zfill(64)
wallet = WALLET[2:].lower().zfill(64)
native = NATIVE_USDC[2:].lower().zfill(64)
tolly = TOLLY_TOKEN[2:].lower().zfill(64)

# swapExactTokensForTokens(uint256 amountIn, uint256 amountOutMin, address[] path, address to, uint256 deadline)
# offset to path = 0x60 (96 bytes = 3 words after selector)
swap_v2 = "0x38ed1739" + amt_in + amt_out_min + hex(96)[2:].zfill(64) + wallet + deadline[2:].zfill(64) + hex(2)[2:].zfill(64) + native + tolly

# swapExactTokensForTokensSupportingFeeOnTransferTokens
swap_fee = "0x5c11d795" + amt_in + amt_out_min + hex(96)[2:].zfill(64) + wallet + deadline[2:].zfill(64) + hex(2)[2:].zfill(64) + native + tolly

# swapExactETHForTokens(uint256 amountOutMin, address[] path, address to, uint256 deadline)
# offset to path = 0x40
swap_eth = "0x7ff36ab5" + amt_out_min + hex(64)[2:].zfill(64) + wallet + deadline[2:].zfill(64) + hex(2)[2:].zfill(64) + native + tolly

# swapExactETHForTokensSupportingFeeOnTransferTokens
swap_eth_fee = "0xc9567bf9" + amt_out_min + hex(64)[2:].zfill(64) + wallet + deadline[2:].zfill(64) + hex(2)[2:].zfill(64) + native + tolly

swaps = [
    ("swapExactTokensForTokens", swap_v2, 0),
    ("swapExactTokensForTokensSupportingFee", swap_fee, 0),
    ("swapExactETHForTokens", swap_eth, 20*10**18),
    ("swapExactETHForTokensSupportingFee", swap_eth_fee, 20*10**18),
]

for name, data, val in swaps:
    print(f"\n  Trying {name} (value={val/1e18})...")
    try:
        result = w3.eth.call({"from": WALLET, "to": TOLLY_DEX, "data": data, "value": val}, "latest")
        print(f"  Sim: ✅ {result.hex()[:60]}")
        
        # Send it!
        rcpt = send_tx(TOLLY_DEX, data, value=val, gas=500000)
        if rcpt:
            status = int(rcpt.get("status","0x0"), 16)
            gas_used = int(rcpt.get("gasUsed","0x0"), 16)
            print(f"  Status: {'✅ SUCCESS 🎉' if status==1 else '❌ FAILED'} Gas: {gas_used}")
            
            TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df031b6ef"
            received = []
            for log in rcpt.get("logs", []):
                topic0 = log.get("topics",[""])[0] if log.get("topics") else ""
                addr = log.get("address","")
                if topic0 == TRANSFER:
                    from_a = "0x" + log["topics"][1][24:64]
                    to_a = "0x" + log["topics"][2][24:64]
                    amount = int(log.get("data","0x"), 16)
                    if to_a.lower() == WALLET[2:].lower():
                        received.append((addr, amount))
                        print(f"    📥 IN: {addr[:12]}... = {amount/1e18:.6f}")
                    if from_a.lower() == WALLET[2:].lower():
                        print(f"    📤 OUT: {addr[:12]}... = {amount/1e18:.6f}")
            
            if received:
                print(f"\n🎉🎉🎉 BUY SUCCESS! 🎉🎉🎉")
                with open("/tmp/arc_buy_result.json", "w") as f:
                    json.dump({"success": True, "received": received, "tx": rcpt.get("transactionHash")}, f, indent=2)
            
            if status == 1:
                break  # Success, stop trying
    except Exception as e:
        print(f"  Sim: ❌ {str(e)[:60]}")

# Also try: call DEX multicall (0xd984396a) with swap data inside
print(f"\n  Trying multicall with swap inside...")
inner_swap = swap_v2[2:]  # remove 0x
inner_len = hex(len(inner_swap) // 2)[2:].zfill(64)
multicall = "0xd984396a" + \
    hex(32)[2:].zfill(64) + \
    hex(1)[2:].zfill(64) + \
    hex(32)[2:].zfill(64) + \
    inner_len + \
    inner_swap
try:
    result = w3.eth.call({"from": WALLET, "to": TOLLY_DEX, "data": multicall, "value": 0}, "latest")
    print(f"  Sim: ✅ {result.hex()[:60]}")
    rcpt = send_tx(TOLLY_DEX, multicall, gas=500000)
    if rcpt:
        status = int(rcpt.get("status","0x0"), 16)
        print(f"  Status: {'✅ SUCCESS 🎉' if status==1 else '❌ FAILED'}")
except Exception as e:
    print(f"  Sim: ❌ {str(e)[:60]}")

# Try the original 0x9265bb9d selector but on DEX directly (not router)
print(f"\n  Trying 0x9265bb9d on DEX directly...")
latest = w3.eth.block_number
logs = w3.eth.get_logs({"fromBlock": latest-50, "toBlock": latest, "address": TOLLY_DEX})
tx_hashes = list(set(l["transactionHash"] for l in logs))
for txh in tx_hashes[:3]:
    tx = w3.eth.get_transaction(txh)
    data = tx["input"].hex() if isinstance(tx["input"], bytes) else tx["input"]
    if not data.startswith("0x"): data = "0x" + data
    if data[:10] == "0x9265bb9d":
        try:
            result = w3.eth.call({"from": WALLET, "to": TOLLY_DEX, "data": data, "value": 0}, "latest")
            print(f"  Sim on DEX: ✅ {result.hex()[:40]}")
        except Exception as e:
            print(f"  Sim on DEX: ❌ {str(e)[:60]}")
        break

print(f"\n--- Final balances ---")
print(f"Native USDC: {w3.eth.get_balance(WALLET)/1e18:.6f}")
