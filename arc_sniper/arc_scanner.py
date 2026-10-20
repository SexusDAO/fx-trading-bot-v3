#!/usr/bin/env python3
"""
Arc Token Scanner — Multi-source discovery + scoring engine
Discovers tokens from DEX events, scores them by quality signals, and alerts on high-potential new tokens.

Scoring dimensions:
1. Liquidity depth (USDC in pool)
2. Volume (swap count + USDC volume)
3. Code quality (proxy vs full contract)
4. Price action (trend, volatility)
5. Age (newer = higher risk/opportunity)
6. Holder diversity (unique addresses in DEX events)

Discovery sources:
1. DEX Swap events (0x89a885...) — all tokens currently trading
2. New token detection — tokens that appear in DEX events but aren't in known_tokens
3. Reserve tracking — monitor liquidity changes over time
"""
import subprocess, json, time, sys, os

RPC = "https://rpc.mainnet.arc.io"
RPC_FALLBACK = "https://rpc.arc-scan.org"
DEX = "0x114d574247342eafea95b7a9234e62984d48cD9E"
USDC = "0x3600000000000000000000000000000000000000"
SWAP_TOPIC = "0x89a885b6900024aaed2c0845aad74f2204445bf00ac135917c70f57540e557b3"
KNOWN_FILE = "/tmp/arc_known_tokens.json"
SCORES_FILE = "/tmp/arc_token_scores.json"
LOG = "/tmp/arc_scanner.log"

def log(msg, level="INFO"):
    ts = time.strftime("%H:%M:%S", time.gmtime())
    line = f"[{ts}] [{level}] {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")

def curl_rpc(method, params=None, retries=3):
    if params is None: params = []
    p = json.dumps({"jsonrpc":"2.0","method":method,"params":params,"id":1})
    for attempt in range(retries):
        for rpc in [RPC, RPC_FALLBACK]:
            r = subprocess.run(["curl","-s","-m","12","-X","POST",rpc,
                "-H","Content-Type: application/json","-d",p],
                capture_output=True, text=True)
            try:
                data = json.loads(r.stdout)
                if "error" in data and "unreachable" in str(data["error"]).lower():
                    continue
                return data
            except:
                continue
    return {"error": "failed"}

def token_string(addr, selector="0x95d89b41"):
    resp = curl_rpc("eth_call", [{"to": addr, "data": selector}, "latest"])
    r = resp.get("result", "0x")
    if not r or r == "0x" or len(r) < 130:
        return "?"
    try:
        h = r[2:]
        length = int(h[64:128], 16) * 2
        return bytes.fromhex(h[128:128+length]).decode('utf-8', errors='replace')
    except:
        return "?"

def token_int(addr, selector):
    resp = curl_rpc("eth_call", [{"to": addr, "data": selector}, "latest"])
    r = resp.get("result", "0x")
    return int(r, 16) if r and r != "0x" and len(r) > 4 else 0

def code_size(addr):
    resp = curl_rpc("eth_getCode", [addr, "latest"])
    r = resp.get("result", "0x")
    return (len(r) - 2) // 2

def get_block():
    resp = curl_rpc("eth_blockNumber")
    return int(resp.get("result", "0x0"), 16) if "result" in resp else None

def get_all_dex_tokens(from_block, to_block):
    """Get all tokens and their swap data from DEX events"""
    resp = curl_rpc("eth_getLogs", [{
        "fromBlock": hex(from_block),
        "toBlock": hex(to_block),
        "address": DEX
    }])
    if resp.get("error") or not resp.get("result"):
        return {}
    
    tokens = {}
    for l in resp["result"]:
        topics = l.get("topics", [])
        if not topics or topics[0].lower() != SWAP_TOPIC.lower():
            continue
        if len(topics) < 3:
            continue
        
        block = int(l["blockNumber"], 16)
        data = l.get("data", "0x")[2:]
        words = [data[i:i+64] for i in range(0, len(data), 64)]
        
        # Extract token address from topic[1] and topic[2]
        for i in [1, 2]:
            addr = "0x" + topics[i][-40:]
            if addr.lower() == USDC.lower():
                continue
            token_addr = addr.lower()
            
            if token_addr not in tokens:
                tokens[token_addr] = {
                    "swap_count": 0,
                    "first_block": block,
                    "last_block": block,
                    "amounts": [],
                    "reserve_token": 0,
                    "reserve_usdc": 0,
                    "unique_traders": set()
                }
            
            t = tokens[token_addr]
            t["swap_count"] += 1
            t["last_block"] = max(t["last_block"], block)
            t["first_block"] = min(t["first_block"], block)
            
            # Extract swap data
            if len(words) > 5:
                amount = int(words[1], 16)
                res_tok = int(words[3], 16)
                res_usdc = int(words[4], 16)
                t["amounts"].append(amount)
                if res_tok > 0:
                    t["reserve_token"] = res_tok
                if res_usdc > 0:
                    t["reserve_usdc"] = res_usdc
            
            # Extract trader from topic[3]
            if len(topics) >= 4:
                trader = "0x" + topics[3][-40:]
                t["unique_traders"].add(trader.lower())
    
    return tokens

def score_token(token_data, sym, code_sz, total_supply, decimals):
    """
    Score a token 0-100 based on quality signals.
    Returns (score, reasons[])
    """
    score = 0
    reasons = []
    
    # 1. Liquidity depth (0-25 pts)
    liq_usdc = token_data["reserve_usdc"] / 1e18
    if liq_usdc > 50000:
        score += 25
        reasons.append(f"Deep liquidity: {liq_usdc:.0f} USDC")
    elif liq_usdc > 10000:
        score += 20
        reasons.append(f"Good liquidity: {liq_usdc:.0f} USDC")
    elif liq_usdc > 1000:
        score += 12
        reasons.append(f"Moderate liquidity: {liq_usdc:.0f} USDC")
    elif liq_usdc > 100:
        score += 5
        reasons.append(f"Low liquidity: {liq_usdc:.0f} USDC")
    else:
        reasons.append(f"Very low liquidity: {liq_usdc:.1f} USDC")
    
    # 2. Volume / swap count (0-20 pts)
    swaps = token_data["swap_count"]
    if swaps > 15:
        score += 20
        reasons.append(f"High volume: {swaps} swaps")
    elif swaps > 8:
        score += 15
        reasons.append(f"Decent volume: {swaps} swaps")
    elif swaps > 3:
        score += 8
        reasons.append(f"Some volume: {swaps} swaps")
    else:
        reasons.append(f"Low volume: {swaps} swaps")
    
    # 3. Code quality (0-15 pts)
    if code_sz > 5000:
        score += 15
        reasons.append(f"Full contract ({code_sz}B)")
    elif code_sz > 1000:
        score += 12
        reasons.append(f"Standard contract ({code_sz}B)")
    elif code_sz > 100:
        score += 5
        reasons.append(f"Small contract ({code_sz}B)")
    else:
        score += 2
        reasons.append(f"Proxy/minimal ({code_sz}B)")
    
    # 4. Trader diversity (0-15 pts)
    traders = len(token_data["unique_traders"])
    if traders > 20:
        score += 15
        reasons.append(f"High diversity: {traders} unique traders")
    elif traders > 10:
        score += 10
        reasons.append(f"Good diversity: {traders} traders")
    elif traders > 3:
        score += 5
        reasons.append(f"Some diversity: {traders} traders")
    else:
        reasons.append(f"Low diversity: {traders} traders")
    
    # 5. Age factor (0-15 pts) — newer tokens with good metrics = higher opportunity
    block_range = token_data["last_block"] - token_data["first_block"]
    if block_range < 50 and swaps > 3:
        score += 15
        reasons.append(f"Brand new (appeared {block_range} blocks ago, already {swaps} swaps)")
    elif block_range < 200:
        score += 10
        reasons.append(f"Recent ({block_range} blocks span)")
    else:
        score += 5
        reasons.append(f"Established ({block_range} blocks span)")
    
    # 6. Reserve balance (0-10 pts)
    if token_data["reserve_token"] > 0 and token_data["reserve_usdc"] > 0:
        ratio = token_data["reserve_usdc"] / token_data["reserve_token"]
        if 0.8 < ratio < 1.2:
            score += 10
            reasons.append(f"Balanced pool (ratio {ratio:.3f})")
        elif 0.5 < ratio < 2.0:
            score += 5
            reasons.append(f"Skewed pool (ratio {ratio:.3f})")
        else:
            reasons.append(f"Unbalanced pool (ratio {ratio:.3f})")
    
    return score, reasons

def load_known():
    if os.path.exists(KNOWN_FILE):
        with open(KNOWN_FILE) as f:
            return json.load(f)
    return {}

def save_known(known):
    with open(KNOWN_FILE, "w") as f:
        json.dump(known, f, indent=2)

def main():
    log("=" * 60)
    log("🔍 Arc Token Scanner — Discovery + Scoring")
    log("=" * 60)
    
    block = get_block()
    if not block:
        print("ERROR:RPC_DOWN")
        return 1
    
    log(f"Current block: {block}")
    
    # Scan last 500 blocks for DEX events
    scan_from = block - 500
    log(f"Scanning blocks {scan_from}-{block}...")
    
    tokens = get_all_dex_tokens(scan_from, block)
    if not tokens:
        print("ERROR:NO_TOKENS")
        return 1
    
    log(f"Found {len(tokens)} unique tokens on DEX")
    
    # Check for new tokens
    known = load_known()
    new_tokens = []
    for addr in tokens:
        if addr not in known:
            new_tokens.append(addr)
    
    if new_tokens:
        log(f"🆕 {len(new_tokens)} NEW tokens discovered!")
    else:
        log(f"All {len(tokens)} tokens already known")
    
    # Score each token
    results = []
    for addr, data in tokens.items():
        sym = token_string(addr)
        name = token_string(addr, "0x06fdde03")
        cs = code_size(addr)
        supply = token_int(addr, "0x18160ddd")
        dec = token_int(addr, "0x313ce567") or 18
        
        score, reasons = score_token(data, sym, cs, supply, dec)
        
        results.append({
            "address": addr,
            "symbol": sym,
            "name": name,
            "score": score,
            "swap_count": data["swap_count"],
            "liquidity_usdc": data["reserve_usdc"] / 1e18,
            "code_size": cs,
            "traders": len(data["unique_traders"]),
            "first_block": data["first_block"],
            "last_block": data["last_block"],
            "is_new": addr in new_tokens,
            "reasons": reasons
        })
        
        # Add to known tokens
        if addr not in known:
            known[addr] = {
                "symbol": sym,
                "name": name,
                "first_seen": block,
                "first_seen_block": data["first_block"]
            }
    
    save_known(known)
    
    # Sort by score
    results.sort(key=lambda x: -x["score"])
    
    # Print results
    print("\n" + "=" * 100)
    print(f"{'#':<3} {'Score':>5} {'Symbol':<12} {'Swaps':>5} {'Liq$':>10} {'Code':>5} {'Trade':>5} {'New':>3} {'Reasons'}")
    print("=" * 100)
    
    for i, r in enumerate(results[:25]):
        new_flag = "🆕" if r["is_new"] else ""
        top_reason = r["reasons"][0] if r["reasons"] else ""
        print(f"{i+1:<3} {r['score']:>5} {r['symbol']:<12} {r['swap_count']:>5} {r['liquidity_usdc']:>10.1f} {r['code_size']:>5} {r['traders']:>5} {new_flag:>3} {top_reason}")
    
    # Alert on high-scoring new tokens
    for r in results:
        if r["is_new"] and r["score"] >= 30:
            print(f"\nNOTIFY:NEW_HIGH_SCORE:{r['symbol']}:{r['score']}:{r['address']}")
            log(f"🚨 HIGH-SCORE NEW TOKEN: {r['symbol']} (score {r['score']}) @ {r['address'][:12]}...", "ALERT")
    
    # Save scores
    with open(SCORES_FILE, "w") as f:
        json.dump(results, f, indent=2, default=str)
    
    print(f"\nScores saved to {SCORES_FILE}")
    print(f"Known tokens: {len(known)}")
    
    return 0

if __name__ == "__main__":
    sys.exit(main())
