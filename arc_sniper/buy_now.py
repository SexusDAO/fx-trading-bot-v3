import os
#!/usr/bin/env python3
"""Arc Buy — all in one, using curl for all tx sending"""
import subprocess, json, time, sys
from web3 import Web3

RPC = "https://rpc.arc-scan.org"
PK = os.environ["ARC_PK"]  # 不接受默认值，必须在环境变量中设置
WALLET = Web3.to_checksum_address("0x8A966Db06aD9624a0E41bb4adfD817c5e7e55F54")
WUSDC = Web3.to_checksum_address("0x3600000000000000000000000000000000000000")
TOLLY_DEX = Web3.to_checksum_address("0x114d574247342eafea95b7a9234e62984d48cD9E")
CHAIN_ID = 5042

w3 = Web3(Web3.HTTPProvider(RPC, request_kwargs={"timeout": 15}))
acct = w3.eth.account.from_key(PK)

def curl_rpc(method, params=None):
    if params is None: params = []
    payload = json.dumps({"jsonrpc":"2.0","method":method,"params":params,"id":1})
    r = subprocess.run(["curl","-s","-m","15","-X","POST",RPC,"-H","Content-Type: application/json","-d",payload], capture_output=True, text=True)
    return json.loads(r.stdout)

def send_signed(signed_tx):
    raw = signed_tx.raw_transaction.hex()
    if not raw.startswith("0x"): raw = "0x" + raw
    resp = curl_rpc("eth_sendRawTransaction", [raw])
    if "error" in resp:
        return None, resp["error"]
    return resp.get("result"), None

def wait_receipt(tx_hash, timeout=120):
    for _ in range(40):
        time.sleep(3)
        resp = curl_rpc("eth_getTransactionReceipt", [tx_hash])
        if resp.get("result"):
            return resp["result"]
    return None

def send_tx(to, data, value=0, gas=500000):
    nonce = w3.eth.get_transaction_count(WALLET)
    tx = {"from": WALLET, "to": to, "value": value, "data": data,
          "nonce": nonce, "gas": gas, "gasPrice": w3.eth.gas_price, "chainId": CHAIN_ID}
    signed = acct.sign_transaction(tx)
    tx_hash, err = send_signed(signed)
    if err:
        print(f"  ❌ Send error: {err}")
        return None
    print(f"  TX: {tx_hash}")
    rcpt = wait_receipt(tx_hash)
    if rcpt:
        status = int(rcpt.get("status","0x0"), 16)
        gas_used = int(rcpt.get("gasUsed","0x0"), 16)
        print(f"  Status: {'✅' if status==1 else '❌'} Gas: {gas_used}")
    return rcpt

def find_calldata():
    latest = w3.eth.block_number
    logs = w3.eth.get_logs({"fromBlock": latest-200, "toBlock": latest, "address": TOLLY_DEX})
    tx_hashes = list(set(l["transactionHash"] for l in logs))
    best = None
    for txh in tx_hashes:
        try:
            tx = w3.eth.get_transaction(txh)
            data = tx["input"].hex() if isinstance(tx["input"], bytes) else tx["input"]
            if not data.startswith("0x"): data = "0x" + data
            if data[:10] == "0x9265bb9d":
                rcpt = w3.eth.get_transaction_receipt(txh)
                if rcpt.status == 1:
                    if best is None or rcpt.gasUsed < best[1]:
                        best = (tx, rcpt.gasUsed, data)
        except:
            pass
    return best

def analyze(rcpt):
    TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df031b6ef"
    received = []
    spent = []
    for log in rcpt.get("logs", []):
        topics = log.get("topics", [])
        if topics and topics[0] == TRANSFER:
            from_a = "0x" + topics[1][24:64]
            to_a = "0x" + topics[2][24:64]
            amount = int(log.get("data","0x"), 16)
            token = log.get("address","")
            if to_a.lower() == WALLET[2:].lower():
                received.append((token, amount))
            if from_a.lower() == WALLET[2:].lower():
                spent.append((token, amount))
    return received, spent

# ═══════════════════════════════════════
print(f"Block: {w3.eth.block_number}")
print(f"Native USDC: {w3.eth.get_balance(WALLET)/1e18:.6f}")

# Step 1: Wrap 20 USDC
print("\n🔄 Step 1: Wrap 20 USDC -> wUSDC")
rcpt = send_tx(WUSDC, "0xd0e30db0", value=20*10**18, gas=100000)
if not rcpt:
    print("❌ Wrap send failed")
    sys.exit(1)

wrap_status = int(rcpt.get("status","0x0"), 16)
abi = [{"constant":True,"inputs":[{"name":"","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"type":"function"}]
c = w3.eth.contract(address=WUSDC, abi=abi)
wbal = c.functions.balanceOf(WALLET).call()
print(f"  wUSDC balance: {wbal/1e18:.6f}")

if wbal == 0:
    print("❌ No wUSDC! Trying alternative: send value+calldata to router...")
    best = find_calldata()
    if not best:
        print("❌ No calldata found")
        sys.exit(1)
    orig_tx, gas, calldata = best
    router = orig_tx["to"]
    print(f"  Router: {router[:12]}...")
    
    # Try with value
    rcpt2 = send_tx(router, calldata, value=20*10**18, gas=500000)
    if rcpt2:
        status2 = int(rcpt2.get("status","0x0"), 16)
        if status2 == 1:
            received, spent = analyze(rcpt2)
            if received:
                print(f"\n🎉 BUY SUCCESS (via value)!")
                for token, amount in received:
                    print(f"  📦 {amount/1e18:.6f} {token[:12]}")
            else:
                print("TX ok but no tokens received directly")
        else:
            print("❌ TX reverted")
    sys.exit(0 if rcpt2 and int(rcpt2.get("status","0x0"),16)==1 else 1)

# Step 2: Find calldata
print("\n📋 Step 2: Find buy calldata")
best = find_calldata()
if not best:
    print("❌ No tx found")
    sys.exit(1)
orig_tx, gas, calldata = best
router = orig_tx["to"]
print(f"  Router: {router[:12]}... gas={gas} len={len(calldata)}")

# Step 3: Approve router
print(f"\n🔐 Step 3: Approve router")
approve_data = "0x095ea7b3" + router[2:].lower().zfill(64) + hex(1000*10**18)[2:].zfill(64)
send_tx(WUSDC, approve_data, gas=100000)
time.sleep(3)

# Step 4: Approve DEX
print(f"\n🔐 Step 4: Approve DEX")
approve_data2 = "0x095ea7b3" + TOLLY_DEX[2:].lower().zfill(64) + hex(1000*10**18)[2:].zfill(64)
send_tx(WUSDC, approve_data2, gas=100000)
time.sleep(3)

# Step 5: BUY!
print(f"\n🚀 Step 5: SENDING BUY TX!!!")
rcpt = send_tx(router, calldata, gas=500000)
if rcpt:
    status = int(rcpt.get("status","0x0"), 16)
    received, spent = analyze(rcpt)
    print(f"\n{'='*50}")
    if received:
        print(f"🎉🎉🎉 BUY SUCCESS! 🎉🎉🎉")
        for token, amount in received:
            try:
                erc20_abi = [
                    {"constant":True,"inputs":[],"name":"symbol","outputs":[{"name":"","type":"string"}],"type":"function"},
                    {"constant":True,"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"type":"function"},
                ]
                tc = w3.eth.contract(address=Web3.to_checksum_address(token), abi=erc20_abi)
                sym = tc.functions.symbol().call()
                dec = tc.functions.decimals().call()
                print(f"  📦 {amount/10**dec} {sym}")
            except:
                print(f"  📦 {amount/1e18:.6f} {token[:12]}")
    elif status == 1:
        print("✅ TX success, no direct transfer")
    else:
        print("❌ TX failed")
    
    if spent:
        for token, amount in spent:
            print(f"  💸 {amount/1e18:.6f} {token[:12]}")
    
    print(f"  TX: {rcpt.get('transactionHash','?')}")
    print(f"{'='*50}")
    
    # Save result
    with open("/tmp/arc_buy_result.json", "w") as f:
        json.dump({"success": status==1, "received": received, "tx": rcpt.get("transactionHash")}, f, indent=2)
