import os
#!/usr/bin/env python3
"""
Arc Universal Swap — Generic token buy via DEX calldata replay + modification

Key discovery:
  - DEX function 0xd984396a accepts modified parameters as long as:
    * amount >= original amount (can increase, not decrease)
    * amountOutMin >= 1 (not 0)
    * sqrtPriceLimit >= original (or MAX_UINT for no limit)
    * deadline (word[9]) must be a FUTURE timestamp (not expired)
  - We can replace sender address (all occurrences) with our wallet
  - Pool state errors (0x756688fe) happen when pool price moves too much
  - Best strategy: S10 (exact replay) + deadline=future + priceLim=MAX + outMin=1
  - Works on recent txs (within ~100 blocks) before pool state drifts

Usage:
    python3 universal_swap.py <token_address> <amount_usdc>
    python3 universal_swap.py 0xa163d7624da3b5d9182c50eab5b8cd247ae861bb 20
    python3 universal_swap.py --list         # list recent buyable tokens (with sim check)
    python3 universal_swap.py --monitor       # monitor DEX for new buyable tokens
    python3 universal_swap.py --buy-any <amount_usdc>  # buy cheapest available token
"""
import subprocess, json, time, sys, os, hashlib, re

# ════════════════════════════════════════════════════════════
# CONFIG
# ════════════════════════════════════════════════════════════
RPC = "https://rpc.mainnet.arc.io"
RPC_FALLBACK = "https://rpc.arc-scan.org"
PK = os.environ["ARC_PK"]  # 不接受默认值，必须在环境变量中设置
WALLET = "0x8A966Db06aD9624a0E41bb4adfD817c5e7e55F54"
WUSDC = "0x3600000000000000000000000000000000000000"  # wrapped USDC (6 decimals)
NATIVE_USDC = "0xfffffffffffffffffffffffffffffffffffffffffffe"
DEX = "0x114d574247342eafea95b7a9234e62984d48cD9E"  # Tolly DEX
CHAIN_ID = 5042
GAS_LIMIT = 1200000

# Event signatures
SWAP_EVENT = "0x89a885b6900024aaed2c0845aad74f2204445bf00ac135917c70f57540e557b3"
TRANSFER_EVENT = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
DEX_SELECTOR = "0xd984396a"
MAX_UINT_HEX = "f" * 64

LOG = "/tmp/arc_universal_swap.log"

# ════════════════════════════════════════════════════════════
# LOGGING
# ════════════════════════════════════════════════════════════
def log(msg, level="INFO"):
    ts = time.strftime("%H:%M:%S", time.gmtime())
    line = f"[{ts}] [{level}] {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")

# ════════════════════════════════════════════════════════════
# RPC LAYER
# ════════════════════════════════════════════════════════════
def curl_rpc(method, params=None, retries=3, timeout=15):
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

def is_rpc_up():
    resp = curl_rpc("eth_blockNumber", retries=1, timeout=8)
    if "result" in resp:
        return True, int(resp["result"], 16)
    return False, 0

def get_nonce():
    resp = curl_rpc("eth_getTransactionCount", [WALLET, "pending"])
    return int(resp.get("result", "0x0"), 16)

def get_gas_price():
    resp = curl_rpc("eth_gasPrice")
    return int(resp.get("result", "0x0"), 16) or 10**9

def get_native_balance():
    resp = curl_rpc("eth_getBalance", [WALLET, "latest"])
    return int(resp.get("result", "0x0"), 16)

def get_wusdc_balance():
    bal_data = "0x70a08231" + WALLET[2:].lower().zfill(64)
    resp = curl_rpc("eth_call", [{"to": WUSDC, "data": bal_data}, "latest"])
    return int(resp.get("result", "0x0"), 16)

def get_token_balance(token_addr):
    bal_data = "0x70a08231" + WALLET[2:].lower().zfill(64)
    resp = curl_rpc("eth_call", [{"to": token_addr, "data": bal_data}, "latest"])
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
# TRANSACTION SIGNING & SENDING
# ════════════════════════════════════════════════════════════
def sign_and_send(tx_dict):
    from web3 import Web3
    from eth_account import Account
    acct = Account.from_key(PK)
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

def send_tx(to, data, value=0, gas=GAS_LIMIT):
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

# ════════════════════════════════════════════════════════════
# DEX CALldATA ANALYSIS & MODIFICATION
# ════════════════════════════════════════════════════════════
def find_buy_tx_for_token(token_addr, scan_blocks=500, max_orig_amount=None):
    """
    Scan recent DEX direct calls (0xd984396a) for BUY txs of the target token.
    BUY = wUSDC (tokenIn) -> token (tokenOut)
    Returns the most recent successful tx with the smallest original amount
    (so we can buy with our desired amount >= original).
    """
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
        
        # Check swap event for token direction
        for l in rcpt.get("logs", []):
            topics = l.get("topics", [])
            if not topics or topics[0].lower() != SWAP_EVENT.lower():
                continue
            token_in = "0x" + topics[1][-40:]
            token_out = "0x" + topics[2][-40:]
            
            # BUY = wUSDC in, token out
            if token_out.lower() == token_addr.lower() and token_in.lower() == WUSDC.lower():
                raw = data[10:]
                words = [raw[i:i+64] for i in range(0, len(raw), 64)]
                orig_amount = int(words[6], 16)  # in 6-decimal wUSDC
                
                # Skip if original amount > our max (we need amount >= original)
                if max_orig_amount and orig_amount > max_orig_amount:
                    continue
                
                candidates.append({
                    'tx': txh,
                    'block': int(tx['blockNumber'], 16),
                    'from': tx['from'],
                    'calldata': data,
                    'orig_amount': orig_amount,
                    'orig_amount_usdc': orig_amount / 1e6,
                    'words': words,
                    'sender_positions': [i for i, w in enumerate(words) if w[-40:].lower() == tx['from'][2:].lower()],
                })
            break
    
    if not candidates:
        return None
    
    # Sort by: most recent first, then smallest original amount
    candidates.sort(key=lambda x: (-x['block'], x['orig_amount']))
    return candidates[0]

def find_any_buy_tx(scan_blocks=500, max_orig_amount=None):
    """
    Find any recent BUY tx (wUSDC -> any token) on DEX.
    """
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
            
            if token_in.lower() == WUSDC.lower():
                raw = data[10:]
                words = [raw[i:i+64] for i in range(0, len(raw), 64)]
                orig_amount = int(words[6], 16)
                
                if max_orig_amount and orig_amount > max_orig_amount:
                    continue
                
                candidates.append({
                    'tx': txh,
                    'block': int(tx['blockNumber'], 16),
                    'from': tx['from'],
                    'calldata': data,
                    'orig_amount': orig_amount,
                    'orig_amount_usdc': orig_amount / 1e6,
                    'words': words,
                    'sender_positions': [i for i, w in enumerate(words) if w[-40:].lower() == tx['from'][2:].lower()],
                    'token_out': token_out,
                })
            break
    
    if not candidates:
        return None
    
    candidates.sort(key=lambda x: (-x['block'], x['orig_amount']))
    return candidates

def modify_calldata(words, sender_positions, new_amount_6dec, out_min=1, price_limit=MAX_UINT_HEX, deadline=None):
    """
    Modify DEX calldata for our wallet:
    - Replace all sender addresses with our wallet
    - Set amount to our desired amount (must be >= original)
    - Set amountOutMin to 1 (must be >= 1, not 0)
    - Set sqrtPriceLimit to MAX_UINT (no limit, must be >= original)
    - Set deadline (word[9]) to future timestamp (critical! expired deadline = revert)
    """
    import time as _time
    WALLET_HEX = WALLET[2:].lower().zfill(64)
    new_words = words.copy()
    
    # Replace all sender occurrences
    for i in sender_positions:
        new_words[i] = WALLET_HEX
    
    # Set amount (word[6]) - must be >= original
    new_words[6] = hex(new_amount_6dec)[2:].zfill(64)
    
    # Set amountOutMin (word[7]) - must be >= 1
    new_words[7] = hex(out_min)[2:].zfill(64)
    
    # Set sqrtPriceLimit (word[8]) - must be >= original or MAX_UINT
    new_words[8] = price_limit
    
    # Set deadline (word[9]) - must be a FUTURE timestamp or it reverts with DeadlineExpired()
    if deadline is None:
        deadline = int(_time.time()) + 3600  # 1 hour from now
    new_words[9] = hex(deadline)[2:].zfill(64)
    
    return DEX_SELECTOR + "".join(new_words)

def simulate_buy(calldata):
    """Simulate the buy call via eth_call"""
    resp = curl_rpc("eth_call", [
        {"from": WALLET, "to": DEX, "data": calldata, "value": "0x0"},
        "latest"
    ])
    if resp.get("result"):
        return True, resp["result"]
    elif resp.get("error"):
        return False, resp["error"].get("data", "") or resp["error"].get("message", "")
    return False, "unknown error"

def execute_buy(token_addr, amount_usdc):
    """Execute a buy of token_addr with amount_usdc worth of USDC"""
    log("=" * 60)
    log(f"🚀 UNIVERSAL SWAP: Buy {get_token_symbol(token_addr)} with {amount_usdc} USDC")
    log(f"   Token: {token_addr}")
    log(f"   Amount: {amount_usdc} USDC")
    log("=" * 60)
    
    # Check RPC
    ok, block = is_rpc_up()
    if not ok:
        log("❌ RPC is down")
        return {"success": False, "error": "RPC down"}
    log(f"✅ RPC online, block={block}")
    
    # Check balances
    wusdc_bal = get_wusdc_balance()
    native_bal = get_native_balance()
    amount_6dec = int(amount_usdc * 10**6)  # Convert to wUSDC 6-decimal format
    
    log(f"💰 Native USDC: {native_bal/1e18:.4f}")
    log(f"💰 wUSDC: {wusdc_bal/1e6:.4f} (6 dec)")
    log(f"💰 Need: {amount_usdc} USDC = {amount_6dec} wUSDC (6 dec)")
    
    if wusdc_bal < amount_6dec:
        log(f"❌ Insufficient wUSDC balance: {wusdc_bal/1e6:.4f} < {amount_usdc}")
        # Check if we can wrap
        if native_bal >= amount_usdc * 10**18:
            log("💡 Have native USDC, need to wrap to wUSDC first...")
            # TODO: implement wrapping
            # For now, the DEX might handle native USDC internally
            # Let's check our wUSDC allowance
            log("⚠️ DEX needs wUSDC, not native USDC. Wrapping not implemented yet.")
            return {"success": False, "error": "Insufficient wUSDC (need wrapping)"}
        else:
            return {"success": False, "error": "Insufficient balance"}
    
    # Check wUSDC allowance to DEX
    allow_data = "0xdd62ed3e" + WALLET[2:].lower().zfill(64) + DEX[2:].lower().zfill(64)
    allow_resp = curl_rpc("eth_call", [{"to": WUSDC, "data": allow_data}, "latest"])
    allowance = int(allow_resp.get("result", "0x0"), 16)
    log(f"🔐 wUSDC allowance to DEX: {allowance/1e6:.2f}")
    
    if allowance < amount_6dec:
        log("🔐 Need to approve DEX on wUSDC...")
        approve_data = "0x095ea7b3" + DEX[2:].lower().zfill(64) + hex(2**256-1)[2:].zfill(64)
        rcpt = send_tx(WUSDC, approve_data, gas=100000)
        if not rcpt or int(rcpt.get("status", "0x0"), 16) != 1:
            log("❌ Approve failed")
            return {"success": False, "error": "Approve failed"}
        log("✅ Approved")
    
    # Find a suitable DEX tx to replay
    log(f"🔍 Finding DEX tx for {get_token_symbol(token_addr)}...")
    candidate = find_buy_tx_for_token(token_addr, scan_blocks=500, max_orig_amount=amount_6dec)
    
    if not candidate:
        log(f"❌ No suitable DEX tx found for {token_addr[:12]}... with orig_amount <= {amount_usdc} USDC")
        log("💡 Try a larger amount, or wait for a matching tx to appear")
        return {"success": False, "error": "No suitable tx found"}
    
    log(f"📋 Found tx: block {candidate['block']} ({block - candidate['block']} blocks ago)")
    log(f"   Original: {candidate['orig_amount_usdc']:.6f} USDC -> {get_token_symbol(token_addr)}")
    log(f"   Sender positions: {candidate['sender_positions']}")
    
    # Check: do we already have this token?
    existing_bal = get_token_balance(token_addr)
    if existing_bal > 0:
        log(f"📦 Already holding: {existing_bal/1e18:.6f}")
    
    # Construct modified calldata
    new_calldata = modify_calldata(
        candidate['words'],
        candidate['sender_positions'],
        amount_6dec,  # our amount
        out_min=1,    # minimal outMin (0 fails)
        price_limit=MAX_UINT_HEX,  # no price limit
    )
    
    # Simulate
    log("🧪 Simulating buy...")
    sim_ok, sim_result = simulate_buy(new_calldata)
    
    if not sim_ok:
        log(f"❌ Simulation failed: {sim_result}")
        return {"success": False, "error": f"Sim failed: {sim_result}"}
    
    log(f"✅ Simulation passed! Result: {str(sim_result)[:60]}...")
    
    # Execute
    log("🚀 SENDING BUY TX!!!")
    rcpt = send_tx(DEX, new_calldata, gas=GAS_LIMIT)
    
    if not rcpt:
        log("❌ Failed to get receipt")
        return {"success": False, "error": "No receipt"}
    
    status = int(rcpt.get("status", "0x0"), 16)
    gas_used = int(rcpt.get("gasUsed", "0x0"), 16)
    
    # Parse Transfer events from receipt
    received = []
    sent = []
    for l in rcpt.get("logs", []):
        topics = l.get("topics", [])
        if not topics or topics[0].lower() != TRANSFER_EVENT.lower():
            continue
        frm = "0x" + topics[1][-40:]
        to = "0x" + topics[2][-40:]
        amt = int(l.get("data", "0x"), 16)
        addr = l.get("address", "")
        if to.lower() == WALLET[2:].lower():
            received.append((addr, amt))
        if frm.lower() == WALLET[2:].lower():
            sent.append((addr, amt))
    
    log(f"\n{'='*60}")
    log(f"Status: {'✅ SUCCESS 🎉' if status == 1 else '❌ FAILED'}")
    log(f"Gas: {gas_used}")
    
    if received:
        log("\n🎉🎉🎉 BOUGHT TOKENS! 🎉🎉🎉")
        for token, amount in received:
            sym = get_token_symbol(token)
            # Check decimals
            dec_resp = curl_rpc("eth_call", [{"to": token, "data": "0x313ce567"}, "latest"])
            dec = int(dec_resp.get("result", "0x12"), 16) if dec_resp.get("result") and dec_resp["result"] != "0x" else 18
            log(f"  📦 {amount / 10**dec:.6f} {sym}")
        log(f"  TX: {rcpt.get('transactionHash', '?')}")
    
    for token, amount in sent:
        if token.lower() == WUSDC.lower():
            log(f"  💸 Spent: {amount/1e6:.6f} wUSDC")
        else:
            log(f"  📤 Out: {token[:12]}... {amount}")
    
    log(f"{'='*60}")
    
    # Save result
    result = {
        "success": status == 1,
        "received": [(t, a) for t, a in received],
        "sent": [(t, a) for t, a in sent],
        "tx": rcpt.get("transactionHash"),
        "gas_used": gas_used,
    }
    with open("/tmp/arc_swap_result.json", "w") as f:
        json.dump(result, f, indent=2, default=str)
    
    return result

def list_buyable_tokens(scan_blocks=500):
    """List all tokens that have recent BUY txs on DEX"""
    ok, block = is_rpc_up()
    if not ok:
        print("RPC is down")
        return
    
    print(f"Current block: {block}")
    print(f"Scanning last {scan_blocks} blocks for BUY txs...\n")
    
    candidates = find_any_buy_tx(scan_blocks=scan_blocks)
    if not candidates:
        print("No buy txs found")
        return
    
    # Deduplicate by token
    by_token = {}
    for c in candidates:
        token = c['token_out']
        if token not in by_token or c['block'] > by_token[token]['block']:
            by_token[token] = c
    
    print(f"Found {len(by_token)} buyable tokens:\n")
    print(f"{'Symbol':<12} {'Token':<44} {'Min Buy':<12} {'Blocks Ago':<10}")
    print("-" * 80)
    
    for token, c in sorted(by_token.items(), key=lambda x: -x[1]['block']):
        sym = get_token_symbol(token)
        min_buy = c['orig_amount_usdc']
        blocks_ago = block - c['block']
        print(f"{sym:<12} {token:<44} {min_buy:<12.4f} {blocks_ago:<10}")

def monitor_dex(interval_sec=30):
    """Monitor DEX for new buy txs and report"""
    log("🔍 DEX Monitor started")
    log(f"   Polling every {interval_sec}s")
    log(f"   Looking for new BUY txs (wUSDC -> token)")
    
    seen_txs = set()
    
    while True:
        try:
            ok, block = is_rpc_up()
            if not ok:
                log("RPC down, retrying...")
                time.sleep(interval_sec)
                continue
            
            logs = curl_rpc("eth_getLogs", [{
                "fromBlock": hex(block - 5),
                "toBlock": hex(block),
                "address": DEX
            }])
            
            if logs.get("error") or not logs.get("result"):
                time.sleep(interval_sec)
                continue
            
            tx_hashes = list(set(l["transactionHash"] for l in logs["result"]))
            
            for txh in tx_hashes:
                if txh in seen_txs:
                    continue
                seen_txs.add(txh)
                
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
                    
                    if token_in.lower() == WUSDC.lower():
                        raw = data[10:]
                        words = [raw[i:i+64] for i in range(0, len(raw), 64)]
                        orig_amount = int(words[6], 16) / 1e6
                        sym = get_token_symbol(token_out)
                        blk = int(tx['blockNumber'], 16)
                        log(f"🟢 NEW BUY: {sym} | {orig_amount:.4f} USDC | block {blk} | tx {txh[:16]}...")
                    break
            
            # Keep seen set from growing too large
            if len(seen_txs) > 1000:
                seen_txs = set(list(seen_txs)[-500:])
            
            time.sleep(interval_sec)
            
        except KeyboardInterrupt:
            log("Monitor stopped")
            break
        except Exception as e:
            log(f"Error: {e}", "ERROR")
            time.sleep(interval_sec)

# ════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════
def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    
    if sys.argv[1] == "--list":
        list_buyable_tokens()
        return
    
    if sys.argv[1] == "--monitor":
        monitor_dex()
        return
    
    token_addr = sys.argv[1]
    if not token_addr.startswith("0x"):
        print(f"Invalid token address: {token_addr}")
        return
    
    if len(sys.argv) < 3:
        print("Usage: python3 universal_swap.py <token_address> <amount_usdc>")
        return
    
    amount = float(sys.argv[2])
    
    result = execute_buy(token_addr, amount)
    
    if result.get("success"):
        print(f"\n✅ Buy successful!")
        sys.exit(0)
    else:
        print(f"\n❌ Buy failed: {result.get('error')}")
        sys.exit(1)

if __name__ == "__main__":
    main()
