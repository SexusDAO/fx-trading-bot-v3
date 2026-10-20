import os
#!/usr/bin/env python3
"""
Arc Buy v4 — Direct DEX call with calldata replay
Key insight: users call DEX (0x114d...) directly with 0xd984396a, not through router proxies.
The sender address is at word[5] in the calldata. We replace it with ours.

Flow:
1. Check RPC + balance
2. Approve DEX on native USDC (0xFFFF...FFFE) if needed
3. Find recent successful DEX tx with 0xd984396a
4. Copy calldata, replace sender (word[5]) with our address
5. Adjust amount_in (word[6]) to small amount, amount_out_min (word[7]) to 0
6. Send to DEX directly
7. Check receipt for token received
"""
import subprocess, json, time, sys, os, hashlib

RPC = "https://rpc.arc-scan.org"
PK = os.environ["ARC_PK"]  # 不接受默认值，必须在环境变量中设置
WALLET = "0x8A966Db06aD9624a0E41bb4adfD817c5e7e55F54"
NATIVE_USDC = "0xffffFFFfFFffffffffffffffFfFFFfffFFFfFFfE"
TOLLY_DEX = "0x114d574247342eafea95b7a9234e62984d48cD9E"
TOLLY_TOKEN = os.environ.get("ARC_SNIPER_BUY_V4_TOLLY_TOKEN", "")
CHAIN_ID = 5042
LOG = "/tmp/arc_buy_v4.log"
BUY_AMOUNT = 5 * 10**18  # 5 USDC (native, 18 decimals on Arc)

def log(msg):
    ts = time.strftime("%H:%M:%S", time.gmtime())
    line = f"[{ts}] {msg}"
    print(line, flush=True)

def curl_rpc(method, params=None):
    if params is None: params = []
    p = json.dumps({"jsonrpc":"2.0","method":method,"params":params,"id":1})
    r = subprocess.run(["curl","-s","-m","15","-X","POST",RPC,"-H","Content-Type: application/json","-d",p],
                       capture_output=True, text=True)
    try:
        return json.loads(r.stdout)
    except:
        return {"error": {"message": f"curl failed: {r.stdout[:100]}"}}

def get_nonce():
    resp = curl_rpc("eth_getTransactionCount", [WALLET, "pending"])
    return int(resp.get("result", "0x0"), 16)

def get_gas_price():
    resp = curl_rpc("eth_gasPrice")
    return int(resp.get("result", "0x0"), 16) or 10**9

def get_balance():
    resp = curl_rpc("eth_getBalance", [WALLET, "latest"])
    return int(resp.get("result", "0x0"), 16)

def sign_and_send(tx_dict):
    """Sign tx locally and send raw via curl"""
    from web3 import Web3
    from eth_account import Account
    acct = Account.from_key(PK)
    # Ensure addresses are checksummed
    tx_dict['to'] = Web3.to_checksum_address(tx_dict['to'])
    tx_dict['from'] = Web3.to_checksum_address(tx_dict['from'])
    tx_dict['chainId'] = CHAIN_ID
    signed = acct.sign_transaction(tx_dict)
    raw = signed.raw_transaction.hex()
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

def send_tx(to, data, value=0, gas=800000):
    nonce = get_nonce()
    gas_price = get_gas_price()
    tx = {
        "from": WALLET,
        "to": to,
        "value": value,
        "data": data,
        "nonce": nonce,
        "gas": gas,
        "gasPrice": gas_price,
    }
    tx_hash, err = sign_and_send(tx)
    if err:
        log(f"  ❌ Send error: {err}")
        return None
    log(f"  TX: {tx_hash}")
    return wait_receipt(tx_hash)

def check_allowance(token, spender):
    allow_data = "0xdd62ed3e" + WALLET[2:].lower().zfill(64) + spender[2:].lower().zfill(64)
    resp = curl_rpc("eth_call", [{"from": WALLET, "to": token, "data": allow_data}, "latest"])
    r = resp.get("result", "0x")
    if r and r != "0x" and len(r) > 4:
        return int(r, 16)
    return 0

def approve(token, spender, amount):
    approve_data = "0x095ea7b3" + spender[2:].lower().zfill(64) + hex(amount)[2:].zfill(64)
    log(f"  Approving {spender[:12]}... for {amount/1e18:.2f} USDC")
    rcpt = send_tx(token, approve_data, gas=100000)
    if rcpt:
        status = int(rcpt.get("status", "0x0"), 16)
        log(f"  Approve: {'✅' if status==1 else '❌'}")
        return status == 1
    return False

def find_recent_dex_tx():
    """Find a recent successful tx that calls DEX directly with 0xd984396a"""
    blk_resp = curl_rpc("eth_blockNumber")
    if "result" not in blk_resp:
        return None
    block = int(blk_resp["result"], 16)
    
    # Search last 200 blocks
    logs_resp = curl_rpc("eth_getLogs", [{
        "fromBlock": hex(block - 200),
        "toBlock": hex(block),
        "address": TOLLY_DEX
    }])
    if logs_resp.get("error") or not logs_resp.get("result"):
        return None
    
    tx_hashes = list(set(l["transactionHash"] for l in logs_resp["result"]))
    log(f"  Found {len(tx_hashes)} DEX txs in last 200 blocks")
    
    for txh in tx_hashes:
        tx_resp = curl_rpc("eth_getTransactionByHash", [txh])
        if tx_resp.get("error") or not tx_resp.get("result"):
            continue
        tx = tx_resp["result"]
        data = tx.get("input", "0x")
        if not data or data[:10] != "0xd984396a":
            continue
        
        # Check receipt for success
        rcpt_resp = curl_rpc("eth_getTransactionReceipt", [txh])
        if rcpt_resp.get("error") or not rcpt_resp.get("result"):
            continue
        rcpt = rcpt_resp["result"]
        if int(rcpt.get("status", "0x0"), 16) != 1:
            continue
        
        # Found a successful direct DEX call
        return tx, rcpt
    
    return None

def modify_calldata(original_data, original_sender, mode="minimal"):
    """
    Replace the sender address in the calldata with our address.
    mode='minimal': only replace sender, keep everything else
    mode='adjust': also set amount_out_min to 0
    """
    selector = original_data[:10]
    params = original_data[10:]
    words = [params[i:i+64] for i in range(0, len(params), 64)]
    
    # Find and replace sender address in all words
    sender_replaced = False
    for i, w in enumerate(words):
        addr = "0x" + w[24:]
        if addr.lower() == original_sender.lower():
            words[i] = WALLET[2:].lower().zfill(64)
            log(f"  Replaced sender at word[{i}]: {original_sender[:12]}... -> {WALLET[:12]}...")
            sender_replaced = True
    
    if not sender_replaced:
        log(f"  ⚠️ Sender not found in calldata, using as-is")
    
    if mode == "adjust":
        # Try to set amount_out_min to 0
        # word[7] is likely amount_out_min based on analysis
        if len(words) > 7:
            orig_out_min = int(words[7], 16)
            if orig_out_min > 0:
                log(f"  Setting amount_out_min (word[7]): {orig_out_min} -> 0")
                words[7] = "0" * 64
    
    return selector + "".join(words)

def check_token_balance(token, addr):
    bal_data = "0x70a08231" + addr[2:].lower().zfill(64)
    resp = curl_rpc("eth_call", [{"to": token, "data": bal_data}, "latest"])
    r = resp.get("result", "0x")
    if r and r != "0x" and len(r) > 4:
        return int(r, 16)
    return 0

def main():
    log("=" * 60)
    log("🚀 Arc Buy v4 — Direct DEX call with calldata replay")
    log("=" * 60)
    
    for attempt in range(200):
        log(f"--- Attempt {attempt+1}/200 ---")
        try:
            # 1. Check RPC
            blk_resp = curl_rpc("eth_blockNumber")
            if "error" in blk_resp:
                log(f"❌ RPC down: {str(blk_resp['error'])[:50]}")
                time.sleep(30)
                continue
            
            block = int(blk_resp["result"], 16)
            log(f"✅ RPC online, block={block}")
            
            # 2. Check balance
            balance = get_balance()
            log(f"💰 Balance: {balance/1e18:.6f} USDC")
            if balance < BUY_AMOUNT:
                log(f"❌ Insufficient balance (need {BUY_AMOUNT/1e18} USDC)")
                time.sleep(60)
                continue
            
            # 3. Check/set DEX allowance
            allowance = check_allowance(NATIVE_USDC, TOLLY_DEX)
            if allowance < BUY_AMOUNT * 2:
                log(f"🔐 Approving DEX on native USDC...")
                if not approve(NATIVE_USDC, TOLLY_DEX, 10000 * 10**18):
                    log("❌ Approve failed, retrying...")
                    time.sleep(30)
                    continue
            else:
                log(f"✅ DEX already approved: {allowance/1e18:.2f} USDC")
            
            # 4. Find a recent successful DEX tx
            result = find_recent_dex_tx()
            if not result:
                log("❌ No recent successful DEX tx found")
                time.sleep(30)
                continue
            
            tx, rcpt = result
            original_sender = tx["from"]
            original_data = tx["input"]
            log(f"📋 Found tx: from={original_sender[:12]}... data_len={len(original_data)}")
            
            # 5. Check if we already have TOLLY balance
            tolly_bal = check_token_balance(TOLLY_TOKEN, WALLET)
            if tolly_bal > 0:
                log(f"🎉 Already have TOLLY: {tolly_bal/1e18:.6f}")
                log("🎉 DONE!")
                return 0
            
            # 6. Modify calldata - try minimal first, then adjust
            new_data = modify_calldata(original_data, original_sender, mode="minimal")
            log(f"  New calldata len: {len(new_data)} chars")
            
            # 7. Simulate the call
            log("🧪 Simulating buy (minimal mode)...")
            sim_resp = curl_rpc("eth_call", [
                {"from": WALLET, "to": TOLLY_DEX, "data": new_data, "value": "0x0"},
                "latest"
            ])
            sim_ok = False
            if sim_resp.get("result"):
                log(f"  Sim: ✅ {sim_resp['result'][:40]}...")
                sim_ok = True
            elif sim_resp.get("error"):
                err_msg = str(sim_resp['error'])
                log(f"  Sim: ❌ {err_msg[:80]}")
                # Try adjust mode
                new_data2 = modify_calldata(original_data, original_sender, mode="adjust")
                log("🧪 Simulating buy (adjust mode)...")
                sim_resp2 = curl_rpc("eth_call", [
                    {"from": WALLET, "to": TOLLY_DEX, "data": new_data2, "value": "0x0"},
                    "latest"
                ])
                if sim_resp2.get("result"):
                    log(f"  Sim2: ✅ {sim_resp2['result'][:40]}...")
                    new_data = new_data2
                    sim_ok = True
                elif sim_resp2.get("error"):
                    log(f"  Sim2: ❌ {str(sim_resp2['error'])[:80]}")
            
            # 8. Send the buy tx!
            log("🚀 SENDING BUY TX!!!")
            buy_rcpt = send_tx(TOLLY_DEX, new_data, gas=1200000)
            
            if buy_rcpt:
                status = int(buy_rcpt.get("status", "0x0"), 16)
                gas_used = int(buy_rcpt.get("gasUsed", "0x0"), 16)
                log(f"\n{'='*60}")
                log(f"Status: {'✅ SUCCESS 🎉' if status==1 else '❌ FAILED'}")
                log(f"Gas: {gas_used}")
                
                TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df031b6ef"
                received = []
                sent = []
                for log_entry in buy_rcpt.get("logs", []):
                    topic0 = log_entry.get("topics", [""])[0] if log_entry.get("topics") else ""
                    addr = log_entry.get("address", "")
                    if topic0 == TRANSFER:
                        from_a = "0x" + log_entry["topics"][1][24:64]
                        to_a = "0x" + log_entry["topics"][2][24:64]
                        amount = int(log_entry.get("data", "0x"), 16)
                        if to_a.lower() == WALLET[2:].lower():
                            received.append((addr, amount))
                            log(f"  📥 IN: {addr[:12]}... = {amount/1e18:.6f}")
                        if from_a.lower() == WALLET[2:].lower():
                            sent.append((addr, amount))
                            log(f"  📤 OUT: {addr[:12]}... = {amount/1e18:.6f}")
                    else:
                        log(f"  Event: {topic0[:20]}... on {addr[:12]}...")
                
                if received:
                    log("\n🎉🎉🎉 BOUGHT TOKENS! 🎉🎉🎉")
                    for token, amount in received:
                        # Get token symbol
                        sym_resp = curl_rpc("eth_call", [{"to": token, "data": "0x95d89b41"}, "latest"])
                        if sym_resp.get("result") and sym_resp["result"] != "0x":
                            sym_hex = sym_resp["result"][2:]
                            try:
                                offset = int(sym_hex[:64], 16)
                                length = int(sym_hex[64:128], 16)
                                sym = bytes.fromhex(sym_hex[128:128+length*2]).decode('utf-8', errors='replace')
                            except:
                                sym = token[:10]
                        else:
                            sym = token[:10]
                        log(f"  📦 {amount/1e18:.6f} {sym}")
                    
                    log(f"  TX: {buy_rcpt.get('transactionHash', '?')}")
                    log(f"{'='*60}")
                    
                    # Save result
                    with open("/tmp/arc_buy_result.json", "w") as f:
                        json.dump({
                            "success": status==1,
                            "received": [(t, a) for t, a in received],
                            "tx": buy_rcpt.get("transactionHash"),
                            "gas_used": gas_used
                        }, f, indent=2)
                    
                    if status == 1:
                        log("🎉 DONE!")
                        return 0
                
                if status != 1:
                    log("❌ TX failed, will retry with different calldata...")
                    time.sleep(60)
                    continue
            else:
                log("❌ Buy tx failed to send")
                time.sleep(30)
                continue
        
        except Exception as e:
            log(f"❌ Error: {str(e)[:150]}")
            import traceback
            traceback.print_exc()
            time.sleep(30)
    
    log("⏰ Timed out")
    return 1

if __name__ == "__main__":
    sys.exit(main())
