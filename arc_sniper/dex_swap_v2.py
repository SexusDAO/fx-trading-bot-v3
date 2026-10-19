#!/usr/bin/env python3
"""Arc Buy v4 — all-curl, approve DEX + direct swap"""
import os
from web3 import Web3
import json, time, subprocess, sys

RPC = "https://rpc.arc-scan.org"
PK = "0xb1e6bc56444acb4f997bb7bd23ba707b258d6b7d8b889897fc4518afeb6be5c4"
WALLET = Web3.to_checksum_address("0x8A966Db06aD9624a0E41bb4adfD817c5e7e55F54")
TOLLY_DEX = Web3.to_checksum_address("0x114d574247342eafea95b7a9234e62984d48cD9E")
NATIVE_USDC = "0xffffFFFfFFffffffffffffffFfFFFfffFFFfFFfE"
TOLLY_TOKEN = os.environ.get("ARC_SNIPER_DEX_SWAP_V2_TOLLY_TOKEN", "")
CHAIN_ID = 5042

w3 = Web3(Web3.HTTPProvider(RPC, request_kwargs={"timeout": 20}))
acct = w3.eth.account.from_key(PK)

def curl_rpc(method, params=None):
    if params is None: params = []
    payload = json.dumps({"jsonrpc":"2.0","method":method,"params":params,"id":1})
    r = subprocess.run(["curl","-s","-m","15","-X","POST",RPC,"-H","Content-Type: application/json","-d",payload], capture_output=True, text=True)
    try:
        return json.loads(r.stdout)
    except:
        return {"error": {"message": r.stdout[:100]}}

def send_tx(to, data, value=0, gas=500000):
    nonce = w3.eth.get_transaction_count(WALLET)
    to_cs = Web3.to_checksum_address(to)
    tx = {"from": WALLET, "to": to_cs, "value": value, "data": data,
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
    print("  ⏰ Receipt timeout")
    return None

print(f"Block: {w3.eth.block_number}")
print(f"Native USDC: {w3.eth.get_balance(WALLET)/1e18:.6f}")

# Step 1: Approve DEX on native USDC (just do it, don't check)
print("\n🔐 Approving DEX on native USDC...")
approve_data = "0x095ea7b3" + TOLLY_DEX[2:].lower().zfill(64) + hex(10000*10**18)[2:].zfill(64)
rcpt = send_tx(NATIVE_USDC, approve_data, gas=100000)
if rcpt:
    status = int(rcpt.get("status","0x0"), 16)
    print(f"  Approve: {'✅' if status==1 else '❌'}")
else:
    print("  Approve failed, continuing anyway...")

# Step 2: Try all swap approaches
deadline = hex(int(time.time()) + 1200)[2:].zfill(64)
amt_in = hex(20 * 10**18)[2:].zfill(64)
amt_out_min = hex(0)[2:].zfill(64)
wallet = WALLET[2:].lower().zfill(64)
native = NATIVE_USDC[2:].lower().zfill(64)
tolly = TOLLY_TOKEN[2:].lower().zfill(64)

swaps = [
    # swapExactTokensForTokens(uint256, uint256, address[], address, uint256)
    ("swapExactTokensForTokens", "0x38ed1739" + amt_in + amt_out_min + hex(96)[2:].zfill(64) + wallet + deadline + hex(2)[2:].zfill(64) + native + tolly, 0),
    # swapExactTokensForTokensSupportingFeeOnTransferTokens
    ("swapExactTokensForTokensSupportingFee", "0x5c11d795" + amt_in + amt_out_min + hex(96)[2:].zfill(64) + wallet + deadline + hex(2)[2:].zfill(64) + native + tolly, 0),
    # swapExactETHForTokens(uint256, address[], address, uint256)
    ("swapExactETHForTokens", "0x7ff36ab5" + amt_out_min + hex(64)[2:].zfill(64) + wallet + deadline + hex(2)[2:].zfill(64) + native + tolly, 20*10**18),
    # swapExactETHForTokensSupportingFeeOnTransferTokens
    ("swapExactETHForTokensSupportingFee", "0xc9567bf9" + amt_out_min + hex(64)[2:].zfill(64) + wallet + deadline + hex(2)[2:].zfill(64) + native + tolly, 20*10**18),
    # Try with path = [WUSDC, TOLLY] where WUSDC = 0x3600...
    ("swapExactTokensForTokens (wUSDC path)", "0x38ed1739" + amt_in + amt_out_min + hex(96)[2:].zfill(64) + wallet + deadline + hex(2)[2:].zfill(64) + "3600000000000000000000000000000000000000".zfill(64) + tolly, 0),
]

TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df031b6ef"

for name, data, val in swaps:
    print(f"\n🚀 Trying {name} (value={val/1e18})...")
    
    # Simulate
    try:
        sim_resp = curl_rpc("eth_call", [{"from": WALLET, "to": TOLLY_DEX, "data": data, "value": hex(val)}, "latest"])
        if sim_resp.get("result"):
            print(f"  Sim: ✅ {sim_resp['result'][:40]}")
        elif sim_resp.get("error"):
            print(f"  Sim: ❌ {str(sim_resp['error'])[:60]}")
            continue
    except Exception as e:
        print(f"  Sim: ❌ {str(e)[:60]}")
        continue
    
    # Send
    rcpt = send_tx(TOLLY_DEX, data, value=val, gas=500000)
    if not rcpt:
        continue
        
    status = int(rcpt.get("status","0x0"), 16)
    gas_used = int(rcpt.get("gasUsed","0x0"), 16)
    print(f"  Status: {'✅ SUCCESS 🎉' if status==1 else '❌ FAILED'} Gas: {gas_used}")
    
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
        for token, amount in received:
            # Get symbol
            sym_resp = curl_rpc("eth_call", [{"from": WALLET, "to": token, "data": "0x95d89b41"}, "latest"])
            if sym_resp.get("result") and sym_resp["result"] != "0x":
                try:
                    sym_hex = sym_resp["result"][2:]
                    sym_offset = int(sym_hex[0:64], 16) * 2
                    sym_len = int(sym_hex[64:128], 16) * 2
                    sym = bytes.fromhex(sym_hex[128:128+sym_len]).decode('utf-8', errors='replace')
                    print(f"  📦 {amount/1e18:.6f} {sym}")
                except:
                    print(f"  📦 {amount/1e18:.6f} {token[:12]}")
            else:
                print(f"  📦 {amount/1e18:.6f} {token[:12]}")
        
        with open("/tmp/arc_buy_result.json", "w") as f:
            json.dump({"success": True, "received": received, "tx": rcpt.get("transactionHash")}, f, indent=2)
        break
    
    if status == 1 and not received:
        print("  TX ok but no tokens received directly")
        # Still might have worked via internal balance

# Step 3: Try multicall on DEX (0xd984396a)
print(f"\n📋 Trying multicall on DEX...")
# Use the swapExactTokensForTokens data as inner call
inner = swaps[0][1][2:]  # remove 0x
inner_len = hex(len(inner) // 2)[2:].zfill(64)
multicall = "0xd984396a" + hex(32)[2:].zfill(64) + hex(1)[2:].zfill(64) + hex(32)[2:].zfill(64) + inner_len + inner

sim_resp = curl_rpc("eth_call", [{"from": WALLET, "to": TOLLY_DEX, "data": multicall, "value": "0x0"}, "latest"])
if sim_resp.get("result"):
    print(f"  Sim: ✅ {sim_resp['result'][:40]}")
    rcpt = send_tx(TOLLY_DEX, multicall, gas=500000)
    if rcpt:
        status = int(rcpt.get("status","0x0"), 16)
        print(f"  Status: {'✅ SUCCESS 🎉' if status==1 else '❌ FAILED'}")
        for log in rcpt.get("logs", []):
            topic0 = log.get("topics",[""])[0] if log.get("topics") else ""
            if topic0 == TRANSFER:
                to_a = "0x" + log["topics"][2][24:64]
                amount = int(log.get("data","0x"), 16)
                if to_a.lower() == WALLET[2:].lower():
                    print(f"    📦 IN: {log.get('address','')[:12]}... = {amount/1e18:.6f}")
elif sim_resp.get("error"):
    print(f"  Sim: ❌ {str(sim_resp['error'])[:60]}")

# Step 4: Try the original 0x9265bb9d on DEX directly
print(f"\n📋 Trying 0x9265bb9d on DEX directly...")
latest = w3.eth.block_number
logs = w3.eth.get_logs({"fromBlock": latest-50, "toBlock": latest, "address": TOLLY_DEX})
tx_hashes = list(set(l["transactionHash"] for l in logs))
for txh in tx_hashes[:5]:
    tx = w3.eth.get_transaction(txh)
    data = tx["input"].hex() if isinstance(tx["input"], bytes) else tx["input"]
    if not data.startswith("0x"): data = "0x" + data
    if data[:10] == "0x9265bb9d":
        sim_resp = curl_rpc("eth_call", [{"from": WALLET, "to": TOLLY_DEX, "data": data, "value": "0x0"}, "latest"])
        if sim_resp.get("result"):
            print(f"  Sim on DEX: ✅")
            rcpt = send_tx(TOLLY_DEX, data, gas=500000)
            if rcpt:
                status = int(rcpt.get("status","0x0"), 16)
                print(f"  Status: {'✅ SUCCESS 🎉' if status==1 else '❌ FAILED'}")
        else:
            print(f"  Sim on DEX: ❌")
        break

print(f"\n--- Final ---")
print(f"Native USDC: {w3.eth.get_balance(WALLET)/1e18:.6f}")
