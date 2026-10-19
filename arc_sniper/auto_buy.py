#!/usr/bin/env python3
"""
Arc Auto Buy — 后台自动重试，RPC 恢复后自动执行完整买入流程
每 30 秒检测一次，最多 2 小时
"""
import subprocess, json, time, sys, os
from web3 import Web3

RPC = "https://rpc.arc-scan.org"
PK = "0xb1e6bc56444acb4f997bb7bd23ba707b258d6b7d8b889897fc4518afeb6be5c4"
WALLET = Web3.to_checksum_address("0x8A966Db06aD9624a0E41bb4adfD817c5e7e55F54")
WUSDC = Web3.to_checksum_address("0x3600000000000000000000000000000000000000")
ROUTER = Web3.to_checksum_address("0xb99654A2537cDc4C65368262F9E03E2128dfd78B")
TOLLY_DEX = Web3.to_checksum_address("0x114d574247342eafea95b7a9234e62984d48cD9E")
CHAIN_ID = 5042
LOG = "/tmp/arc_autobuy.log"

def log(msg):
    ts = time.strftime("%H:%M:%S", time.gmtime())
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")

def get_w3():
    return Web3(Web3.HTTPProvider(RPC, request_kwargs={"timeout": 10}))

def send_raw_tx(signed_tx):
    raw = signed_tx.raw_transaction.hex()
    if not raw.startswith("0x"): raw = "0x" + raw
    payload = json.dumps({"jsonrpc":"2.0","method":"eth_sendRawTransaction","params":[raw],"id":1})
    r = subprocess.run(["curl","-s","-m","15","-X","POST",RPC,"-H","Content-Type: application/json","-d",payload], capture_output=True, text=True)
    resp = json.loads(r.stdout)
    if "error" in resp:
        raise Exception(f"RPC: {resp['error']}")
    return resp["result"]

def wait_receipt(w3, tx_hash, timeout=60):
    start = time.time()
    while time.time() - start < timeout:
        try:
            return w3.eth.get_transaction_receipt(tx_hash)
        except:
            time.sleep(2)
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
    receipt = wait_receipt(w3, tx_hash, timeout=60)
    status = receipt.status if hasattr(receipt, 'status') else int(receipt.get('status','0x0'),16)
    gas_used = receipt.gasUsed if hasattr(receipt, 'gasUsed') else int(receipt.get('gasUsed','0x0'),16)
    success = status == 1
    log(f"  Status: {'✅' if success else '❌'} Gas: {gas_used}")
    return receipt

def execute_buy():
    w3 = get_w3()
    if not w3.is_connected():
        log("❌ RPC down")
        return False
    
    acct = w3.eth.account.from_key(PK)
    log(f"Block: {w3.eth.block_number}, Native: {w3.eth.get_balance(WALLET)/1e18:.3f}")
    
    try:
        # Step 1: Wrap 20 USDC
        log("🔄 Wrap 20 USDC -> wUSDC...")
        send_tx(w3, acct, WUSDC, "0xd0e30db0", value=20*10**18, gas=100000)
        
        # Verify wUSDC balance
        abi = [{"constant":True,"inputs":[{"name":"","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"type":"function"}]
        c = w3.eth.contract(address=WUSDC, abi=abi)
        wbal = c.functions.balanceOf(WALLET).call()
        log(f"  wUSDC: {wbal/1e18:.6f}")
        if wbal == 0:
            log("❌ Wrap failed - no wUSDC")
            return False
        
        # Step 2: Approve router
        log("🔐 Approve router...")
        approve_data = "0x095ea7b3" + ROUTER[2:].lower().zfill(64) + hex(1000*10**18)[2:].zfill(64)
        send_tx(w3, acct, WUSDC, approve_data, gas=100000)
        
        # Step 3: Approve DEX
        log("🔐 Approve DEX...")
        approve_data2 = "0x095ea7b3" + TOLLY_DEX[2:].lower().zfill(64) + hex(1000*10**18)[2:].zfill(64)
        send_tx(w3, acct, WUSDC, approve_data2, gas=100000)
        
        # Step 4: Find calldata to replay
        log("📋 Finding recent buy tx...")
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
        
        if not best:
            log("❌ No suitable tx found")
            return False
        
        orig_tx, gas, calldata = best
        log(f"  Found tx to {orig_tx['to'][:12]}... gas={gas} len={len(calldata)}")
        
        # Step 5: Send buy tx
        log("🚀 SENDING BUY TX!!!")
        receipt = send_tx(w3, acct, orig_tx["to"], calldata, gas=500000)
        
        # Step 6: Analyze
        TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df031b6ef"
        received = []
        spent = []
        for log_entry in receipt.logs:
            topic0 = log_entry["topics"][0].hex() if log_entry["topics"] else ""
            if topic0 == TRANSFER:
                from_a = "0x" + log_entry["topics"][1].hex()[24:64]
                to_a = "0x" + log_entry["topics"][2].hex()[24:64]
                amount = int(log_entry["data"].hex(), 16) if log_entry["data"] != b"" else 0
                token = log_entry["address"]
                if to_a.lower() == WALLET.lower():
                    received.append((token, amount))
                if from_a.lower() == WALLET.lower():
                    spent.append((token, amount))
        
        log(f"\n{'='*50}")
        if received:
            log("🎉🎉🎉 BUY SUCCESS! 🎉🎉🎉")
            for token, amount in received:
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
                    log(f"  📦 Received: {amount/1e18:.6f} {token[:10]}")
        elif receipt.status == 1:
            log("✅ TX success but no direct transfer to us")
        else:
            log("❌ TX failed")
        
        if spent:
            for token, amount in spent:
                log(f"  💸 Spent: {amount/1e18:.6f} {token[:10]}")
        
        log(f"  TX: {receipt.transactionHash.hex()}")
        log(f"{'='*50}")
        
        # Write result
        with open("/tmp/arc_buy_result.json", "w") as f:
            json.dump({
                "success": receipt.status == 1,
                "received": [(t, a) for t, a in received],
                "tx_hash": receipt.transactionHash.hex(),
                "time": time.strftime("%Y-%m-%d %H:%M:%S UTC"),
            }, f, indent=2)
        
        return True
        
    except Exception as e:
        log(f"❌ Error: {e}")
        return False

def main():
    log("=" * 50)
    log("🤖 Arc Auto Buy — 后台自动重试")
    log(f"Max retries: 240 (2 hours)")
    log("=" * 50)
    
    for i in range(240):
        log(f"--- Attempt {i+1}/240 ---")
        try:
            w3 = get_w3()
            if w3.is_connected() and w3.eth.chain_id == CHAIN_ID:
                log(f"✅ RPC online! Block={w3.eth.block_number}")
                if execute_buy():
                    log("🎉 Done! Exiting.")
                    return 0
                else:
                    log("Buy failed, waiting 60s...")
                    time.sleep(60)
                    continue
            else:
                log("❌ RPC down")
        except Exception as e:
            log(f"❌ Error: {e}")
        time.sleep(30)
    
    log("⏰ Timed out after 2 hours")
    return 1

if __name__ == "__main__":
    sys.exit(main())
