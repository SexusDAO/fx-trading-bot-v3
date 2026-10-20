import os
#!/usr/bin/env python3
"""
Arc Auto Buy v2 — 多策略自动重试
策略1: 直接 wrap + replay (标准路径)
策略2: 发送 value 给 router + calldata (让 router 内部 wrap)
策略3: 发送 value 给其他合约地址
策略4: 等待 RPC 限制解除后重试策略1
"""
import subprocess, json, time, sys, os
from web3 import Web3

RPC = "https://rpc.arc-scan.org"
PK = os.environ["ARC_PK"]  # 不接受默认值，必须在环境变量中设置
WALLET = Web3.to_checksum_address("0x8A966Db06aD9624a0E41bb4adfD817c5e7e55F54")
WUSDC = Web3.to_checksum_address("0x3600000000000000000000000000000000000000")
TOLLY_DEX = Web3.to_checksum_address("0x114d574247342eafea95b7a9234e62984d48cD9E")
CHAIN_ID = 5042
LOG = "/tmp/arc_autobuy_v2.log"
RESULT_FILE = "/tmp/arc_buy_result.json"

def log(msg):
    ts = time.strftime("%H:%M:%S", time.gmtime())
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")

def rpc_curl(method, params=None):
    if params is None: params = []
    payload = json.dumps({"jsonrpc":"2.0","method":method,"params":params,"id":1})
    r = subprocess.run(["curl","-s","-m","15","-X","POST",RPC,"-H","Content-Type: application/json","-d",payload], capture_output=True, text=True)
    try:
        resp = json.loads(r.stdout)
        if "error" in resp:
            return None, resp["error"]
        return resp.get("result"), None
    except:
        return None, {"message": f"curl failed: {r.stdout[:100]}"}

def get_w3():
    return Web3(Web3.HTTPProvider(RPC, request_kwargs={"timeout": 10}))

def send_raw_tx(signed_tx):
    raw = signed_tx.raw_transaction.hex()
    if not raw.startswith("0x"): raw = "0x" + raw
    result, err = rpc_curl("eth_sendRawTransaction", [raw])
    if err:
        raise Exception(f"RPC: {err}")
    return result

def wait_receipt_curl(tx_hash, timeout=120):
    start = time.time()
    while time.time() - start < timeout:
        result, _ = rpc_curl("eth_getTransactionReceipt", [tx_hash])
        if result is not None:
            return result
        time.sleep(3)
    raise Exception(f"Timeout: {tx_hash}")

def send_tx(w3, acct, to, data, value=0, gas=500000):
    nonce = w3.eth.get_transaction_count(WALLET)
    tx = {
        "from": WALLET, "to": to, "value": value, "data": data,
        "nonce": nonce, "gas": gas, "gasPrice": w3.eth.gas_price, "chainId": CHAIN_ID,
    }
    signed = acct.sign_transaction(tx)
    tx_hash = send_raw_tx(signed)
    log(f"  TX: {tx_hash}")
    receipt = wait_receipt_curl(tx_hash, timeout=120)
    status = int(receipt.get("status", "0x0"), 16)
    gas_used = int(receipt.get("gasUsed", "0x0"), 16)
    success = status == 1
    log(f"  Status: {'✅' if success else '❌'} Gas: {gas_used}")
    return receipt

def find_buy_calldata(w3):
    """Find a recent successful buy tx calldata"""
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

def check_wusdc_balance(w3):
    abi = [{"constant":True,"inputs":[{"name":"","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"type":"function"}]
    c = w3.eth.contract(address=WUSDC, abi=abi)
    return c.functions.balanceOf(WALLET).call()

def analyze_receipt(receipt, w3=None):
    """Analyze receipt for transfer events"""
    TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df031b6ef"
    logs = receipt.get("logs", [])
    received = []
    spent = []
    for log in logs:
        topics = log.get("topics", [])
        if topics and topics[0] == TRANSFER:
            from_a = "0x" + topics[1][24:64] if len(topics) > 1 else ""
            to_a = "0x" + topics[2][24:64] if len(topics) > 2 else ""
            data = log.get("data", "0x")
            amount = int(data, 16) if data != "0x" else 0
            token = log.get("address", "")
            if to_a.lower() == WALLET.lower():
                received.append((token, amount))
            if from_a.lower() == WALLET.lower():
                spent.append((token, amount))
    return received, spent

def try_wrap(w3, acct):
    """Try to wrap USDC to wUSDC"""
    try:
        receipt = send_tx(w3, acct, WUSDC, "0xd0e30db0", value=20*10**18, gas=100000)
        status = int(receipt.get("status", "0x0"), 16)
        if status == 1:
            wbal = check_wusdc_balance(w3)
            if wbal > 0:
                log(f"  ✅ Wrapped! wUSDC: {wbal/1e18:.6f}")
                return True
        return False
    except Exception as e:
        log(f"  Wrap error: {e}")
        return False

def try_approve(w3, acct, spender):
    """Approve spender for wUSDC"""
    approve_data = "0x095ea7b3" + spender[2:].lower().zfill(64) + hex(1000*10**18)[2:].zfill(64)
    try:
        receipt = send_tx(w3, acct, WUSDC, approve_data, gas=100000)
        return int(receipt.get("status", "0x0"), 16) == 1
    except Exception as e:
        log(f"  Approve error: {e}")
        return False

def try_buy(w3, acct, orig_tx, calldata):
    """Send the buy tx"""
    try:
        # Simulate first
        try:
            w3.eth.call({"from": WALLET, "to": orig_tx["to"], "data": calldata, "value": 0}, "latest")
            log("  Sim: ✅")
        except Exception as e:
            log(f"  Sim: ⚠️ {str(e)[:60]}")
        
        receipt = send_tx(w3, acct, orig_tx["to"], calldata, gas=500000)
        return receipt
    except Exception as e:
        log(f"  Buy error: {e}")
        return None

def execute_full_buy():
    """Execute the full buy flow"""
    w3 = get_w3()
    if not w3.is_connected():
        log("❌ RPC down")
        return False
    
    acct = w3.eth.account.from_key(PK)
    log(f"Block: {w3.eth.block_number}, Native: {w3.eth.get_balance(WALLET)/1e18:.3f}")
    
    # Check if we already have wUSDC
    wbal = check_wusdc_balance(w3)
    log(f"wUSDC: {wbal/1e18:.6f}")
    
    # Step 1: Wrap (if needed)
    if wbal < 20 * 10**18:
        log("🔄 Wrapping 20 USDC...")
        if not try_wrap(w3, acct):
            log("❌ Wrap failed — trying alternative strategies...")
            
            # Strategy 2: Send value + calldata to router directly
            best = find_buy_calldata(w3)
            if best:
                orig_tx, gas, calldata = best
                log(f"Trying with value=20 USDC to router {orig_tx['to'][:12]}...")
                try:
                    receipt = send_tx(w3, acct, orig_tx["to"], calldata, value=20*10**18, gas=500000)
                    status = int(receipt.get("status", "0x0"), 16)
                    if status == 1:
                        received, spent = analyze_receipt(receipt)
                        if received:
                            log("🎉 BUY SUCCESS (with value)!")
                            for token, amount in received:
                                log(f"  📦 Received: {amount/1e18:.6f} {token[:12]}")
                            return True
                except Exception as e:
                    log(f"  Value to router failed: {str(e)[:80]}")
            
            return False
    else:
        log("✅ Already have wUSDC")
    
    # Step 2: Approve
    best = find_buy_calldata(w3)
    if not best:
        log("❌ No buy tx found to replay")
        return False
    
    orig_tx, gas, calldata = best
    router_addr = orig_tx["to"]
    
    log(f"🔐 Approving {router_addr[:12]}...")
    if not try_approve(w3, acct, router_addr):
        log("❌ Approve failed")
        return False
    
    log(f"🔐 Approving DEX...")
    if not try_approve(w3, acct, TOLLY_DEX):
        log("❌ DEX approve failed")
        return False
    
    # Step 3: Buy
    log("🚀 SENDING BUY TX!")
    receipt = try_buy(w3, acct, orig_tx, calldata)
    if receipt:
        status = int(receipt.get("status", "0x0"), 16)
        received, spent = analyze_receipt(receipt)
        
        if received:
            log("\n🎉🎉🎉 BUY SUCCESS! 🎉🎉🎉")
            for token, amount in received:
                # Try to get symbol
                try:
                    erc20_abi = [
                        {"constant":True,"inputs":[],"name":"symbol","outputs":[{"name":"","type":"string"}],"type":"function"},
                        {"constant":True,"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"type":"function"},
                    ]
                    tc = w3.eth.contract(address=Web3.to_checksum_address(token), abi=erc20_abi)
                    sym = tc.functions.symbol().call()
                    dec = tc.functions.decimals().call()
                    log(f"  📦 Received: {amount/10**dec} {sym}")
                except:
                    log(f"  📦 Received: {amount/1e18:.6f} {token[:12]}")
            
            with open(RESULT_FILE, "w") as f:
                json.dump({"success": True, "received": [(t, a) for t, a in received], "tx": receipt.get("transactionHash")}, f, indent=2)
            return True
        elif status == 1:
            log("✅ TX success but no direct transfer")
            with open(RESULT_FILE, "w") as f:
                json.dump({"success": True, "note": "no direct transfer", "tx": receipt.get("transactionHash")}, f, indent=2)
            return True
        else:
            log("❌ TX failed")
    
    return False

def main():
    log("=" * 60)
    log("🤖 Arc Auto Buy v2 — Multi-strategy")
    log("=" * 60)
    
    for i in range(240):  # 2 hours
        log(f"--- Attempt {i+1}/240 ---")
        try:
            if execute_full_buy():
                log("🎉 DONE! Exiting.")
                # Kill the background monitor
                os.system("pkill -f auto_monitor.py 2>/dev/null")
                return 0
        except Exception as e:
            log(f"❌ Error: {str(e)[:100]}")
        
        time.sleep(30)
    
    log("⏰ Timed out")
    return 1

if __name__ == "__main__":
    sys.exit(main())
