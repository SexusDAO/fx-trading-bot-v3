#!/usr/bin/env python3
"""
Arc Monitor v2 — Position tracking + new token discovery + auto-buy
Silent by default; only notifies on: new token found, buy success, PnL milestone, or error.

Modes (via argv):
  monitor   — track positions, discover new tokens (default)
  buy       — buy a specific token (args: token_addr amount_usdc)
  status    — print one-line summary and exit
"""
import subprocess, json, time, sys, os, hashlib

# ════════════════════════════════════════════════════════════
# CONFIG
# ════════════════════════════════════════════════════════════
RPC = "https://rpc.mainnet.arc.io"
RPC_FALLBACK = "https://rpc.arc-scan.org"
PK = os.environ.get("ARC_PK", "0xb1e6bc56444acb4f997bb7bd23ba707b258d6b7d8b889897fc4518afeb6be5c4")
WALLET = "0x8A966Db06aD9624a0E41bb4adfD817c5e7e55F54"
NATIVE_USDC = "0xffffFFFfFFffffffffffffffFfFFFfffFFFfFFfE"
TOLLY_DEX = "0x114d574247342eafea95b7a9234e62984d48cD9E"
TOLLY_TOKEN = os.environ.get("ARC_SNIPER_ARC_MONITOR_TOLLY_TOKEN", "")
CHAIN_ID = 5042
LOG = "/tmp/arc_monitor.log"
STATE_FILE = "/tmp/arc_state.json"
KNOWN_TOKENS_FILE = os.environ.get("ARC_SNIPER_ARC_MONITOR_KNOWN_TOKENS_FILE", "")
BUY_AMOUNT_DEFAULT = 20 * 10**18  # 20 USDC default for new buys
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df031b6ef"
DEX_SELECTOR = "0xd984396a"

# DEX addresses to watch (can add more as discovered)
DEX_CONTRACTS = [TOLLY_DEX]

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
# RPC LAYER — with retry
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
    return "result" in resp, resp

def get_nonce():
    resp = curl_rpc("eth_getTransactionCount", [WALLET, "latest"])
    return int(resp.get("result", "0x0"), 16)

def get_gas_price():
    resp = curl_rpc("eth_gasPrice")
    return int(resp.get("result", "0x0"), 16) or 10**9

def get_balance():
    resp = curl_rpc("eth_getBalance", [WALLET, "latest"])
    if "error" in resp or not resp.get("result"):
        return -1  # signal error
    return int(resp.get("result", "0x0"), 16)

def get_block():
    resp = curl_rpc("eth_blockNumber")
    return int(resp.get("result", "0x0"), 16) if "result" in resp else None

# ════════════════════════════════════════════════════════════
# TOKEN QUERIES
# ════════════════════════════════════════════════════════════
def token_balance(token, addr):
    data = "0x70a08231" + addr[2:].lower().zfill(64)
    resp = curl_rpc("eth_call", [{"to": token, "data": data}, "latest"])
    r = resp.get("result", "0x")
    return int(r, 16) if r and r != "0x" and len(r) > 4 else 0

def token_symbol(token):
    resp = curl_rpc("eth_call", [{"to": token, "data": "0x95d89b41"}, "latest"])
    r = resp.get("result", "0x")
    if not r or r == "0x" or len(r) < 130:
        return None
    try:
        hex_str = r[2:]
        offset = int(hex_str[:64], 16) * 2
        length = int(hex_str[64:128], 16) * 2
        return bytes.fromhex(hex_str[128:128+length]).decode('utf-8', errors='replace')
    except:
        return None

def token_name(token):
    resp = curl_rpc("eth_call", [{"to": token, "data": "0x06fdde03"}, "latest"])
    r = resp.get("result", "0x")
    if not r or r == "0x" or len(r) < 130:
        return None
    try:
        hex_str = r[2:]
        offset = int(hex_str[:64], 16) * 2
        length = int(hex_str[64:128], 16) * 2
        return bytes.fromhex(hex_str[128:128+length]).decode('utf-8', errors='replace')
    except:
        return None

def check_allowance(token, spender):
    data = "0xdd62ed3e" + WALLET[2:].lower().zfill(64) + spender[2:].lower().zfill(64)
    resp = curl_rpc("eth_call", [{"from": WALLET, "to": token, "data": data}, "latest"])
    r = resp.get("result", "0x")
    return int(r, 16) if r and r != "0x" and len(r) > 4 else 0

# ════════════════════════════════════════════════════════════
# TX LAYER
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

def send_tx(to, data, value=0, gas=800000):
    nonce = get_nonce()
    gas_price = get_gas_price()
    tx = {"from": WALLET, "to": to, "value": value, "data": data,
          "nonce": nonce, "gas": gas, "gasPrice": gas_price}
    tx_hash, err = sign_and_send(tx)
    if err:
        log(f"  TX send error: {err}", "ERROR")
        return None
    log(f"  TX: {tx_hash}")
    return wait_receipt(tx_hash)

def approve(token, spender, amount):
    data = "0x095ea7b3" + spender[2:].lower().zfill(64) + hex(amount)[2:].zfill(64)
    rcpt = send_tx(token, data, gas=100000)
    if rcpt:
        status = int(rcpt.get("status", "0x0"), 16)
        return status == 1
    return False

# ════════════════════════════════════════════════════════════
# STATE MANAGEMENT
# ════════════════════════════════════════════════════════════
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"positions": {}, "known_tokens": {}, "last_block": 0, "pnl_high": 0, "pnl_low": 0}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def load_known_tokens():
    if os.path.exists(KNOWN_TOKENS_FILE):
        with open(KNOWN_TOKENS_FILE) as f:
            return json.load(f)
    return {}

def save_known_tokens(tokens):
    with open(KNOWN_TOKENS_FILE, "w") as f:
        json.dump(tokens, f, indent=2)

# ════════════════════════════════════════════════════════════
# DEX EVENT SCANNING — discover new tokens
# ════════════════════════════════════════════════════════════
def scan_dex_events(from_block, to_block, dex_addr):
    """Scan DEX Swap events to discover traded tokens"""
    resp = curl_rpc("eth_getLogs", [{
        "fromBlock": hex(from_block),
        "toBlock": hex(to_block),
        "address": dex_addr
    }])
    if resp.get("error") or not resp.get("result"):
        return {}
    
    # DEX Swap event: topic[0]=0x89a885..., topic[1]=tokenIn, topic[2]=tokenOut
    SWAP_TOPIC = "0x89a885b6900024aaed2c0845aad74f2204445bf00ac135917c70f57540e557b3"
    USDC_WRAPPED = "0x3600000000000000000000000000000000000000"
    
    tokens = {}
    for entry in resp["result"]:
        topics = entry.get("topics", [])
        if not topics or topics[0].lower() != SWAP_TOPIC.lower():
            continue
        if len(topics) < 3:
            continue
        # Extract addresses from topic[1] and topic[2] (last 40 chars of 64-char topic)
        for i in [1, 2]:
            addr = "0x" + topics[i][-40:]
            if addr.lower() == USDC_WRAPPED.lower():
                continue
            tokens[addr.lower()] = tokens.get(addr.lower(), 0) + 1
    return tokens

def get_recent_swaps(dex_addr, count=5):
    """Get recent successful swap txs from DEX"""
    block = get_block()
    if not block:
        return []
    
    resp = curl_rpc("eth_getLogs", [{
        "fromBlock": hex(block - 100),
        "toBlock": hex(block),
        "address": dex_addr
    }])
    if resp.get("error") or not resp.get("result"):
        return []
    
    tx_hashes = list(set(l["transactionHash"] for l in resp["result"] if l.get("transactionHash")))
    swaps = []
    for txh in tx_hashes[:count]:
        tx_resp = curl_rpc("eth_getTransactionByHash", [txh])
        if tx_resp.get("error") or not tx_resp.get("result"):
            continue
        tx = tx_resp["result"]
        if tx.get("input", "")[:10] == DEX_SELECTOR:
            rcpt_resp = curl_rpc("eth_getTransactionReceipt", [txh])
            if rcpt_resp.get("result"):
                rcpt = rcpt_resp["result"]
                if int(rcpt.get("status", "0x0"), 16) == 1:
                    swaps.append((tx, rcpt))
    return swaps

# ════════════════════════════════════════════════════════════
# BUY ENGINE — calldata replay
# ════════════════════════════════════════════════════════════
def estimate_price_from_swaps(token_addr, swaps):
    """Estimate token price from recent swap data"""
    for tx, rcpt in swaps:
        usdc_out = 0
        token_in = 0
        for log_entry in rcpt.get("logs", []):
            topics = log_entry.get("topics", [])
            if not topics or topics[0] != TRANSFER_TOPIC:
                continue
            if len(topics) < 3:
                continue
            from_a = "0x" + topics[1][24:64]
            to_a = "0x" + topics[2][24:64]
            amount = int(log_entry.get("data", "0x"), 16)
            token = log_entry.get("address", "")
            if token.lower() == NATIVE_USDC.lower() and from_a.lower() == TOLLY_DEX[2:].lower():
                usdc_out += amount
            if token.lower() == token_addr.lower() and to_a.lower() != TOLLY_DEX[2:].lower():
                token_in += amount
        if token_in > 0 and usdc_out > 0:
            return usdc_out / token_in  # USDC per token
    return None

def buy_token(dex_addr, token_addr, amount_usdc_wei, mode="minimal"):
    """
    Buy any token from DEX by replaying a recent successful swap.
    Returns (success, received_amount, tx_hash)
    """
    # Find recent successful DEX tx with our selector
    swaps = get_recent_swaps(dex_addr, count=10)
    if not swaps:
        log("No recent DEX swaps found", "WARN")
        return False, 0, None
    
    for tx, rcpt in swaps:
        original_sender = tx["from"]
        original_data = tx["input"]
        
        # Check if this swap involves our target token
        involves_token = False
        for log_entry in rcpt.get("logs", []):
            if log_entry.get("address", "").lower() == token_addr.lower():
                involves_token = True
                break
        if not involves_token:
            continue
        
        # Modify calldata: replace sender
        selector = original_data[:10]
        params = original_data[10:]
        words = [params[i:i+64] for i in range(0, len(params), 64)]
        
        sender_replaced = False
        for i, w in enumerate(words):
            addr = "0x" + w[24:]
            if addr.lower() == original_sender.lower():
                words[i] = WALLET[2:].lower().zfill(64)
                sender_replaced = True
        
        if not sender_replaced:
            continue
        
        # Set amount_out_min to 0 (word[7] typically)
        if mode == "adjust" and len(words) > 7:
            words[7] = "0" * 64
        
        new_data = selector + "".join(words)
        
        # Simulate
        sim_resp = curl_rpc("eth_call", [
            {"from": WALLET, "to": dex_addr, "data": new_data, "value": "0x0"},
            "latest"
        ])
        if sim_resp.get("error"):
            log(f"Sim failed: {str(sim_resp['error'])[:60]}", "WARN")
            continue
        if not sim_resp.get("result"):
            continue
        
        # Send the buy tx
        log(f"🚀 Buying token {token_addr[:12]}...")
        buy_rcpt = send_tx(dex_addr, new_data, gas=1200000)
        if not buy_rcpt:
            return False, 0, None
        
        status = int(buy_rcpt.get("status", "0x0"), 16)
        gas_used = int(buy_rcpt.get("gasUsed", "0x0"), 16)
        
        # Parse received tokens
        received = []
        for log_entry in buy_rcpt.get("logs", []):
            topics = log_entry.get("topics", [])
            if not topics or topics[0] != TRANSFER_TOPIC or len(topics) < 3:
                continue
            to_a = "0x" + topics[2][24:64]
            amount = int(log_entry.get("data", "0x"), 16)
            if to_a.lower() == WALLET[2:].lower():
                received.append((log_entry.get("address", ""), amount))
        
        if status == 1 and received:
            tx_hash = buy_rcpt.get("transactionHash", "")
            log(f"✅ Bought! Gas: {gas_used}", "BUY")
            for token, amount in received:
                log(f"  📦 {amount/1e18:.6f} {token[:12]}...", "BUY")
            return True, received, tx_hash
        elif status != 1:
            log(f"❌ TX failed, trying next swap...", "WARN")
            continue
    
    return False, 0, None

# ════════════════════════════════════════════════════════════
# MONITOR MODE
# ════════════════════════════════════════════════════════════
def monitor_single():
    """Run one monitoring cycle and exit. Cron handles periodicity."""
    state = load_state()
    known_tokens = load_known_tokens()
    
    up, resp = is_rpc_up()
    if not up:
        log("RPC down", "WARN")
        return 0
    
    block = int(resp["result"], 16)
    balance = get_balance()
    if balance < 0:
        log("Balance query failed", "WARN")
        return 0
    
    log(f"Block={block} Balance={balance/1e18:.4f} USDC")
    
    # 1. Track existing positions
    for token_addr, pos in list(state.get("positions", {}).items()):
        bal = token_balance(token_addr, WALLET)
        old_bal = pos.get("balance", 0)
        if bal != old_bal:
            pos["balance"] = bal
            log(f"📊 {pos.get('symbol', token_addr[:8])}: {bal/1e18:.6f} (was {old_bal/1e18:.6f})")
        
        # Estimate price from recent swaps
        swaps = get_recent_swaps(TOLLY_DEX, count=5)
        price = estimate_price_from_swaps(token_addr, swaps)
        if price:
            pos["last_price"] = price
            value_usdc = bal * price / 1e18
            pos["value_usdc"] = value_usdc
            cost = pos.get("cost_usdc", 0)
            if cost > 0:
                pnl = value_usdc - cost
                pnl_pct = (pnl / cost) * 100
                pos["pnl"] = pnl
                pos["pnl_pct"] = pnl_pct
                
                high = state.get("pnl_high", 0)
                low = state.get("pnl_low", 0)
                if pnl_pct > high:
                    state["pnl_high"] = pnl_pct
                    log(f"📈 {pos.get('symbol','?')} PnL: {pnl_pct:+.1f}% (NEW HIGH)", "PNL")
                    print(f"NOTIFY:PNL_HIGH:{pos.get('symbol','?')}:{pnl_pct:.1f}%")
                elif pnl_pct < low:
                    state["pnl_low"] = pnl_pct
                    log(f"📉 {pos.get('symbol','?')} PnL: {pnl_pct:+.1f}% (NEW LOW)", "PNL")
                    print(f"NOTIFY:PNL_LOW:{pos.get('symbol','?')}:{pnl_pct:.1f}%")
                else:
                    log(f"📊 {pos.get('symbol','?')} PnL: {pnl_pct:+.1f}% (${pnl:+.2f})")
    
    # 2. Discover new tokens on DEX
    last_block = state.get("last_block", 0)
    if last_block == 0:
        last_block = block - 50
    if block - last_block > 20:
        scan_to = min(block, last_block + 100)
        log(f"🔎 Scanning blocks {last_block+1}-{scan_to} for new tokens...")
        for dex in DEX_CONTRACTS:
            tokens = scan_dex_events(last_block + 1, scan_to, dex)
            for token_addr, count in tokens.items():
                if token_addr not in known_tokens:
                    sym = token_symbol(token_addr) or "?"
                    name = token_name(token_addr) or "?"
                    known_tokens[token_addr] = {
                        "symbol": sym, "name": name,
                        "first_seen_block": scan_to, "transfer_count": count
                    }
                    log(f"🆕 NEW TOKEN: {sym} ({name}) @ {token_addr[:12]}... ({count} transfers)", "DISCOVER")
                    print(f"NOTIFY:NEW_TOKEN:{sym}:{name}:{token_addr}")
        
        save_known_tokens(known_tokens)
        state["last_block"] = scan_to
    
    # 3. Check allowance
    allowance = check_allowance(NATIVE_USDC, TOLLY_DEX)
    if allowance > 0 and allowance < 100 * 10**18:
        log(f"Allowance low: {allowance/1e18:.2f}, re-approving...", "INFO")
        approve(NATIVE_USDC, TOLLY_DEX, 10000 * 10**18)
    elif allowance == 0 and balance > 10 * 10**18:
        log("No allowance, approving DEX...", "INFO")
        approve(NATIVE_USDC, TOLLY_DEX, 10000 * 10**18)
    
    save_state(state)
    return 0

# ════════════════════════════════════════════════════════════
# STATUS MODE — one line summary
# ════════════════════════════════════════════════════════════
def status():
    up, resp = is_rpc_up()
    if not up:
        print("RPC DOWN")
        return
    
    block = int(resp["result"], 16)
    balance = get_balance()
    
    state = load_state()
    positions = state.get("positions", {})
    
    parts = [f"Block={block}", f"USDC={balance/1e18:.2f}"]
    for addr, pos in positions.items():
        sym = pos.get("symbol", addr[:8])
        bal = pos.get("balance", 0) / 1e18
        pnl = pos.get("pnl_pct", 0)
        parts.append(f"{sym}={bal:.2f}({pnl:+.1f}%)")
    
    print(" | ".join(parts))

# ════════════════════════════════════════════════════════════
# BUY MODE
# ════════════════════════════════════════════════════════════
def buy(token_addr, amount_usdc):
    amount_wei = int(amount_usdc * 10**18)
    
    up, _ = is_rpc_up()
    if not up:
        print("ERROR:RPC_DOWN")
        return 1
    
    balance = get_balance()
    if balance < amount_wei:
        print(f"ERROR:INSUFFICIENT_BALANCE:{balance/1e18:.4f}<{amount_usdc}")
        return 1
    
    # Check/set allowance
    allowance = check_allowance(NATIVE_USDC, TOLLY_DEX)
    if allowance < amount_wei:
        log("Approving DEX...", "INFO")
        if not approve(NATIVE_USDC, TOLLY_DEX, 10000 * 10**18):
            print("ERROR:APPROVE_FAILED")
            return 1
    
    # Buy
    success, received, tx_hash = buy_token(TOLLY_DEX, token_addr, amount_wei)
    if success:
        # Update state
        state = load_state()
        sym = token_symbol(token_addr) or token_addr[:8]
        state["positions"][token_addr.lower()] = {
            "symbol": sym,
            "balance": received[0][1] if received else 0,
            "cost_usdc": amount_usdc,
            "buy_tx": tx_hash,
            "buy_time": time.time()
        }
        save_state(state)
        print(f"SUCCESS:{sym}:{received[0][1]/1e18:.6f}:{tx_hash}")
        return 0
    else:
        print("ERROR:BUY_FAILED")
        return 1

# ════════════════════════════════════════════════════════════
# INIT — record existing TOLLY position
# ════════════════════════════════════════════════════════════
def init_state():
    """Initialize state with existing TOLLY position"""
    state = load_state()
    if not state.get("positions"):
        tolly_bal = token_balance(TOLLY_TOKEN, WALLET)
        if tolly_bal > 0:
            state["positions"][TOLLY_TOKEN.lower()] = {
                "symbol": "TOLLY",
                "balance": tolly_bal,
                "cost_usdc": 196.66,  # approximate cost
                "buy_tx": "0xde7cbeb626e065a14b819c6fce3cf735279f9256225c691fb3c9ac92948b2533",
                "buy_time": time.time() - 3600
            }
            log(f"Initialized TOLLY position: {tolly_bal/1e18:.2f}", "INIT")
    
    # Set last_block to current - 50 if it's 0 or very old
    block = get_block()
    if block and state.get("last_block", 0) < block - 100:
        state["last_block"] = block - 50
        log(f"Set last_block to {block - 50}", "INIT")
    
    if not state.get("known_tokens") and not os.path.exists(KNOWN_TOKENS_FILE):
        known_tokens = load_known_tokens()
        known_tokens[TOLLY_TOKEN.lower()] = {
            "symbol": "TOLLY",
            "name": "Tolly",
            "first_seen_block": 0,
            "transfer_count": 100
        }
        save_known_tokens(known_tokens)
    
    save_state(state)
    return state

# ════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════
def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "monitor"
    
    if mode == "status":
        status()
        return 0
    elif mode == "buy":
        if len(sys.argv) < 4:
            print("Usage: buy <token_addr> <amount_usdc>")
            return 1
        return buy(sys.argv[2], float(sys.argv[3]))
    elif mode == "init":
        init_state()
        print("State initialized")
        return 0
    elif mode == "monitor":
        init_state()
        monitor_single()
        return 0
    else:
        print(f"Unknown mode: {mode}. Use: monitor, buy, status, init")
        return 1

if __name__ == "__main__":
    sys.exit(main())
