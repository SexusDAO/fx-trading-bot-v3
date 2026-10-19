#!/usr/bin/env python3
"""
Arc Swap v3 — Fixed version based on Grok analysis
Key fixes:
1. Gas price: copy from successful txs (46-48 Gwei), not eth_gasPrice (22 Gwei)
2. TX type: legacy (type 0), not EIP-1559
3. Replace ALL occurrences of original sender, not just word[5] and word[25]
4. Verify TX inclusion after sending
5. Use proper simulation with from=our_wallet
"""
import subprocess, json, time, sys, os, re

# ════════════════════════════════════════════════════════════
# CONFIG
# ════════════════════════════════════════════════════════════
RPC = "https://rpc.mainnet.arc.io"
RPC_FALLBACK = "https://rpc.arc-scan.org"
PK = os.environ.get("ARC_PK", "0xb1e6bc56444acb4f997bb7bd23ba707b258d6b7d8b889897fc4518afeb6be5c4")
WALLET = "0x8A966Db06aD9624a0E41bb4adfD817c5e7e55F54"
WUSDC = "0x3600000000000000000000000000000000000000"  # 6 decimals
DEX = "0x114d574247342eafea95b7a9234e62984d48cD9E"  # Tolly DEX
CHAIN_ID = 5042
GAS_PRICE_GWEI = 50  # 50 Gwei — above the 46-48 Gwei floor we see in successful txs
GAS_LIMIT = 600000   # based on successful txs (315K-914K)
LOG_FILE = "/tmp/arc_swap_fixed.log"

SWAP_EVENT = "0x89a885b6900024aaed2c0845aad74f2204445bf00ac135917c70f57540e557b3"
TRANSFER_EVENT = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df031b6ef"
DEX_SELECTOR = "0xd984396a"

def log(msg, level="INFO"):
    ts = time.strftime("%H:%M:%S", time.gmtime())
    line = f"[{ts}] [{level}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

# ════════════════════════════════════════════════════════════
# RPC LAYER
# ════════════════════════════════════════════════════════════
def curl_rpc(method, params=None, retries=3, timeout=30):
    if params is None: params = []
    payload = json.dumps({"jsonrpc":"2.0","method":method,"params":params,"id":1})
    for attempt in range(retries):
        for rpc in [RPC, RPC_FALLBACK]:
            r = subprocess.run(
                ["curl","-s","-m",str(timeout),"-X","POST",rpc,
                 "-H","Content-Type: application/json","-d",payload],
                capture_output=True, text=True
            )
            try:
                data = json.loads(r.stdout)
                if "error" in data and "unreachable" in str(data["error"]).lower():
                    if attempt < retries - 1:
                        time.sleep(2)
                    continue
                return data
            except:
                if attempt < retries - 1:
                    time.sleep(2)
                    continue
    return {"error": {"message": "curl failed after retries"}}

def get_nonce():
    resp = curl_rpc("eth_getTransactionCount", [WALLET, "pending"])
    return int(resp.get("result", "0x0"), 16)

def get_wusdc_balance():
    bal_data = "0x70a08231" + WALLET[2:].lower().zfill(64)
    resp = curl_rpc("eth_call", [{"to": WUSDC, "data": bal_data}, "latest"])
    return int(resp.get("result", "0x0"), 16)

def get_token_symbol(token_addr):
    try:
        resp = curl_rpc("eth_call", [{"to": token_addr, "data": "0x95d89b41"}, "latest"])
        r = resp.get("result", "0x")
        if r and r != "0x" and len(r) > 10:
            sym_hex = r[2:]
            offset = int(sym_hex[:64], 16) * 2
            length = int(sym_hex[64:128], 16) * 2
            return bytes.fromhex(sym_hex[128:128+length]).decode('utf-8', errors='replace')
    except:
        pass
    return token_addr[:8]

# ════════════════════════════════════════════════════════════
# TX SIGNING & SENDING (legacy type 0)
# ════════════════════════════════════════════════════════════
def sign_and_send(tx_dict):
    from web3 import Web3
    from eth_account import Account
    acct = Account.from_key(PK)
    tx_dict['to'] = Web3.to_checksum_address(tx_dict['to'])
    tx_dict['from'] = Web3.to_checksum_address(tx_dict['from'])
    tx_dict['chainId'] = CHAIN_ID
    # Force legacy type 0 (matching successful txs)
    tx_dict.pop('type', None)
    tx_dict.pop('maxFeePerGas', None)
    tx_dict.pop('maxPriorityFeePerGas', None)
    tx_dict.pop('accessList', None)
    
    signed = acct.sign_transaction(tx_dict)
    raw = signed.raw_transaction.hex()
    if not raw.startswith("0x"): raw = "0x" + raw
    
    # Verify signature
    recovered = Account.recover_transaction(signed.raw_transaction)
    if recovered.lower() != WALLET.lower():
        log(f"  ❌ Signature mismatch! recovered={recovered}", "ERROR")
        return None, {"message": "signature mismatch"}
    
    log(f"  ✅ Signature OK, recovered={recovered[:12]}...")
    
    # Send via both RPCs
    resp = curl_rpc("eth_sendRawTransaction", [raw])
    if "error" in resp:
        return None, resp["error"]
    
    tx_hash = resp.get("result")
    if not tx_hash:
        return None, {"message": "no tx hash returned"}
    
    log(f"  TX hash: {tx_hash}")
    
    # CRITICAL: Verify TX was actually accepted by checking getTransaction
    time.sleep(3)
    verify = curl_rpc("eth_getTransactionByHash", [tx_hash])
    if not verify.get("result"):
        log(f"  ⚠️ TX not found after 3s — may not be in mempool. Trying fallback RPC...")
        # Try sending via fallback RPC
        payload = json.dumps({"jsonrpc":"2.0","method":"eth_sendRawTransaction","params":[raw],"id":1})
        r = subprocess.run(
            ["curl","-s","-m","30","-X","POST",RPC_FALLBACK,
             "-H","Content-Type: application/json","-d",payload],
            capture_output=True, text=True
        )
        try:
            resp2 = json.loads(r.stdout)
            if resp2.get("result"):
                tx_hash = resp2["result"]
                log(f"  TX hash (fallback): {tx_hash}")
            elif resp2.get("error"):
                return None, resp2["error"]
        except:
            return None, {"message": "fallback RPC also failed"}
    
    return tx_hash, None

def wait_receipt(tx_hash, timeout=180):
    """Wait for receipt with block-based polling"""
    start_block = curl_rpc("eth_blockNumber")
    start_block = int(start_block.get("result", "0x0"), 16) if start_block.get("result") else 0
    
    for i in range(60):  # 60 * 3s = 180s
        time.sleep(3)
        resp = curl_rpc("eth_getTransactionReceipt", [tx_hash])
        if resp.get("result"):
            return resp["result"]
        
        # Also check if TX is at least in mempool
        tx_resp = curl_rpc("eth_getTransactionByHash", [tx_hash])
        if not tx_resp.get("result") and i == 0:
            log(f"  ⚠️ TX not in mempool after first poll!")
        elif not tx_resp.get("result") and i > 0 and i % 10 == 0:
            log(f"  ⚠️ TX still not found after {i*3}s...")
    
    return None

def send_tx(to, data, value=0, gas=GAS_LIMIT, gas_price_gwei=GAS_PRICE_GWEI):
    nonce = get_nonce()
    gas_price = gas_price_gwei * 10**9  # Convert Gwei to Wei
    tx = {
        "from": WALLET,
        "to": to,
        "value": value,
        "data": data,
        "nonce": nonce,
        "gas": gas,
        "gasPrice": gas_price,
    }
    log(f"  nonce={nonce} gasPrice={gas_price_gwei}Gwei gas={gas}")
    
    tx_hash, err = sign_and_send(tx)
    if err:
        log(f"  ❌ Send error: {err}")
        return None
    return wait_receipt(tx_hash)

# ════════════════════════════════════════════════════════════
# DEX CALDATA REPLAY (FIXED)
# ════════════════════════════════════════════════════════════
def find_buy_tx_for_token(token_addr, scan_blocks=500, max_orig_amount=None):
    """Find a recent successful BUY tx for the target token"""
    resp = curl_rpc("eth_blockNumber")
    if "result" not in resp:
        return None
    block = int(resp["result"], 16)
    
    logs = curl_rpc("eth_getLogs", [{
        "fromBlock": hex(max(0, block - scan_blocks)),
        "toBlock": hex(block),
        "address": DEX
    }])
    if logs.get("error") or not logs.get("result"):
        return None
    
    tx_hashes = list(set(l["transactionHash"] for l in logs["result"]))
    log(f"  Scanning {len(tx_hashes)} DEX txs in last {scan_blocks} blocks...")
    
    candidates = []
    for txh in tx_hashes:
        tx_resp = curl_rpc("eth_getTransactionByHash", [txh])
        if not tx_resp.get("result"):
            continue
        tx = tx_resp["result"]
        data = tx.get("input", "0x")
        if data[:10] != DEX_SELECTOR:
            continue
        
        rcpt_resp = curl_rpc("eth_getTransactionReceipt", [txh])
        if not rcpt_resp.get("result"):
            continue
        rcpt = rcpt_resp["result"]
        if int(rcpt.get("status", "0x0"), 16) != 1:
            continue
        
        for l in rcpt.get("logs", []):
            topics = l.get("topics", [])
            if not topics or topics[0].lower() != SWAP_EVENT.lower():
                continue
            token_in = "0x" + topics[1][-40:]
            token_out = "0x" + topics[2][-40:]
            
            if token_out.lower() == token_addr.lower() and token_in.lower() == WUSDC.lower():
                raw = data[10:]
                words = [raw[i:i+64] for i in range(0, len(raw), 64)]
                orig_amount = int(words[6], 16) if len(words) > 6 else 0
                
                if max_orig_amount and orig_amount > max_orig_amount:
                    continue
                
                # Find ALL occurrences of sender address in calldata
                sender_hex = tx['from'][2:].lower()
                sender_positions = []
                for i, w in enumerate(words):
                    if w[-40:].lower() == sender_hex:
                        sender_positions.append(i)
                
                candidates.append({
                    'tx': txh,
                    'block': int(tx['blockNumber'], 16),
                    'from': tx['from'],
                    'calldata': data,
                    'orig_amount': orig_amount,
                    'orig_amount_usdc': orig_amount / 1e6,
                    'words': words,
                    'sender_positions': sender_positions,
                    'gas': int(tx.get('gas', '0x0'), 16),
                    'gas_price': int(tx.get('gasPrice', '0x0'), 16),
                })
            break
    
    if not candidates:
        return None
    
    # Sort by: most recent first, then smallest original amount
    candidates.sort(key=lambda x: (-x['block'], x['orig_amount']))
    return candidates[0]

def modify_calldata_fixed(original_data, original_sender, our_wallet, new_amount, deadline_ts):
    """
    Fixed calldata modification:
    1. Replace ALL occurrences of original sender (not just word[5] and word[25])
    2. Set amount (word[6]) to our amount
    3. Set amountOutMin (word[7]) to 1 (for testing)
    4. Set sqrtPriceLimit (word[8]) to MAX_UINT160 (not MAX_UINT256)
    5. Set deadline (word[9]) to future timestamp
    """
    selector = original_data[:10]
    raw = original_data[10:]
    
    # Replace ALL occurrences of sender address
    sender_hex = original_sender[2:].lower()
    wallet_hex = our_wallet[2:].lower()
    
    # Replace in the raw hex (catches both padded and unpadded occurrences)
    modified = raw.replace(sender_hex, wallet_hex)
    
    words = [modified[i:i+64] for i in range(0, len(modified), 64)]
    
    # Modify amount (word[6])
    if len(words) > 6:
        words[6] = hex(new_amount)[2:].zfill(64)
        log(f"  Set word[6] (amount): {new_amount} ({new_amount/1e6:.6f} USDC)")
    
    # Set amountOutMin (word[7]) to 1
    if len(words) > 7:
        words[7] = hex(1)[2:].zfill(64)
        log(f"  Set word[7] (amountOutMin): 1")
    
    # Set sqrtPriceLimit (word[8]) to MAX_UINT160 (V3 style)
    if len(words) > 8:
        # MAX_UINT160 = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF
        words[8] = "f" * 40  # 160 bits max
        log(f"  Set word[8] (sqrtPriceLimit): MAX_UINT160")
    
    # Set deadline (word[9]) to future timestamp
    if len(words) > 9:
        words[9] = hex(deadline_ts)[2:].zfill(64)
        log(f"  Set word[9] (deadline): {deadline_ts} ({time.strftime('%H:%M:%S', time.gmtime(deadline_ts))})")
    
    result = selector + "".join(words)
    
    # Count replacements
    replace_count = raw.count(sender_hex)
    log(f"  Replaced {replace_count} occurrences of sender {original_sender[:12]}...")
    
    return result

def simulate_buy(calldata):
    """Simulate with from=our_wallet (critical!)"""
    resp = curl_rpc("eth_call", [
        {"from": WALLET, "to": DEX, "data": calldata, "value": "0x0"},
        "latest"
    ])
    if resp.get("result"):
        return True, resp["result"]
    elif resp.get("error"):
        return False, str(resp["error"])
    return False, "unknown error"

# ════════════════════════════════════════════════════════════
# MAIN EXECUTION
# ════════════════════════════════════════════════════════════
def execute_buy(token_addr, amount_usdc):
    """Execute a buy: wUSDC -> token"""
    amount_wei = int(amount_usdc * 1e6)  # 6 decimal wUSDC
    
    log("=" * 60)
    log(f"🚀 SWAP: Buy {token_addr[:12]}... with {amount_usdc} USDC")
    log("=" * 60)
    
    # 1. Check RPC
    blk_resp = curl_rpc("eth_blockNumber")
    if "result" not in blk_resp:
        log("❌ RPC down")
        return {"success": False, "error": "RPC down"}
    block = int(blk_resp["result"], 16)
    log(f"✅ RPC online, block={block}")
    
    # 2. Check balance
    wusdc_bal = get_wusdc_balance()
    log(f"💰 wUSDC: {wusdc_bal/1e6:.6f}")
    if wusdc_bal < amount_wei:
        log(f"❌ Insufficient wUSDC: {wusdc_bal/1e6} < {amount_usdc}")
        return {"success": False, "error": "insufficient balance"}
    
    # 3. Check allowance
    allow_data = "0xdd62ed3e" + WALLET[2:].lower().zfill(64) + DEX[2:].lower().zfill(64)
    allow_resp = curl_rpc("eth_call", [{"to": WUSDC, "data": allow_data}, "latest"])
    allowance = int(allow_resp.get("result", "0x0"), 16)
    log(f"🔐 Allowance: {allowance/1e6:.2f} USDC")
    if allowance < amount_wei:
        log("❌ Insufficient allowance, need to approve first")
        return {"success": False, "error": "insufficient allowance"}
    
    # 4. Find a recent BUY tx for this token
    log(f"🔍 Finding DEX tx for token {token_addr[:12]}...")
    candidate = find_buy_tx_for_token(token_addr, scan_blocks=500, max_orig_amount=amount_wei)
    if not candidate:
        log(f"❌ No recent BUY tx found for this token with amount <= {amount_usdc}")
        return {"success": False, "error": "no suitable template tx"}
    
    blocks_ago = block - candidate['block']
    log(f"📋 Found tx: block {candidate['block']} ({blocks_ago} blocks ago)")
    log(f"   Original: {candidate['orig_amount_usdc']:.6f} USDC")
    log(f"   Sender positions: {candidate['sender_positions']}")
    log(f"   Gas used by template: {candidate['gas']}")
    
    # 5. Modify calldata
    deadline_ts = int(time.time()) + 600  # 10 min deadline
    new_calldata = modify_calldata_fixed(
        candidate['calldata'],
        candidate['from'],
        WALLET,
        amount_wei,
        deadline_ts
    )
    
    # 6. Simulate with our wallet as from
    log("🧪 Simulating buy (from=our wallet)...")
    sim_ok, sim_result = simulate_buy(new_calldata)
    if sim_ok:
        log(f"✅ Simulation passed! {sim_result[:40]}...")
    else:
        log(f"❌ Simulation failed: {sim_result[:100]}")
        return {"success": False, "error": f"simulation failed: {sim_result[:100]}"}
    
    # 7. Send with fixed gas price (50 Gwei, above 46-48 floor)
    log(f"🚀 SENDING BUY TX (gasPrice={GAS_PRICE_GWEI} Gwei, gas={GAS_LIMIT})...")
    rcpt = send_tx(DEX, new_calldata, gas=GAS_LIMIT, gas_price_gwei=GAS_PRICE_GWEI)
    
    if not rcpt:
        log("❌ TX not mined after 180s")
        return {"success": False, "error": "TX not mined"}
    
    status = int(rcpt.get("status", "0x0"), 16)
    gas_used = int(rcpt.get("gasUsed", "0x0"), 16)
    log(f"📋 Receipt: status={'✅' if status==1 else '❌'} gas_used={gas_used}")
    
    if status != 1:
        log("❌ TX reverted")
        return {"success": False, "error": "TX reverted", "receipt": rcpt}
    
    # 8. Check for token received
    received = []
    for l in rcpt.get("logs", []):
        topics = l.get("topics", [])
        if not topics:
            continue
        t0 = topics[0].lower()
        if t0 == TRANSFER_EVENT.lower():
            to_addr = "0x" + topics[2][-40:]
            if to_addr.lower() == WALLET[2:].lower():
                token = l.get("address", "")
                amount = int(l.get("data", "0x"), 16)
                received.append((token, amount))
                sym = get_token_symbol(token)
                log(f"  📥 Received: {amount} of {sym} ({token[:12]}...)")
    
    if received:
        log("🎉🎉🎉 BUY SUCCESSFUL! 🎉🎉🎉")
        return {"success": True, "received": received, "tx": rcpt.get("transactionHash")}
    else:
        log("⚠️ TX succeeded but no tokens received directly")
        return {"success": True, "warning": "no direct transfer", "tx": rcpt.get("transactionHash")}

def main():
    if len(sys.argv) < 3:
        print("Usage: python3 swap_fixed.py <token_address> <amount_usdc>")
        print("Example: python3 swap_fixed.py 0x5ce58c2a1f3f9b828206396b4e60921af35470d9 2")
        return
    
    token_addr = sys.argv[1]
    amount = float(sys.argv[2])
    
    result = execute_buy(token_addr, amount)
    
    if result.get("success"):
        print(f"\n✅ SUCCESS!")
        sys.exit(0)
    else:
        print(f"\n❌ FAILED: {result.get('error')}")
        sys.exit(1)

if __name__ == "__main__":
    main()
