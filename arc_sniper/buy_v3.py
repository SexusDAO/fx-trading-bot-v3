import os
#!/usr/bin/env python3
"""
Arc Buy v3 — All-curl, approve native USDC + buy
1. approve(router) on 0xFFFF...FFFE (native USDC ERC20)
2. Send buy calldata
"""
import subprocess, json, time, sys, os

RPC = "https://rpc.arc-scan.org"
PK = os.environ["ARC_PK"]  # 不接受默认值，必须在环境变量中设置
WALLET = "0x8A966Db06aD9624a0E41bb4adfD817c5e7e55F54"
NATIVE_USDC = "0xffffFFFfFFffffffffffffffFfFFFfffFFFfFFfE"
TOLLY_DEX = "0x114d574247342eafea95b7a9234e62984d48cD9E"
CHAIN_ID = 5042
LOG = "/tmp/arc_buy_v3.log"

from web3 import Web3
w3 = Web3(Web3.HTTPProvider(RPC, request_kwargs={"timeout": 20}))
acct = w3.eth.account.from_key(PK)

def log(msg):
    ts = time.strftime("%H:%M:%S", time.gmtime())
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")

def curl_rpc(method, params=None):
    if params is None: params = []
    payload = json.dumps({"jsonrpc":"2.0","method":method,"params":params,"id":1})
    r = subprocess.run(["curl","-s","-m","15","-X","POST",RPC,"-H","Content-Type: application/json","-d",payload], capture_output=True, text=True)
    try:
        return json.loads(r.stdout)
    except:
        return {"error": {"message": f"curl failed: {r.stdout[:100]}"}}

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
        log(f"  ❌ Send error: {err}")
        return None
    log(f"  TX: {tx_hash}")
    return wait_receipt(tx_hash)

def main():
    log("=" * 60)
    log("🚀 Arc Buy v3 — Approve + Buy (all-curl)")
    log("=" * 60)

    for attempt in range(120):
        log(f"--- Attempt {attempt+1}/120 ---")
        try:
            # Check RPC
            resp = curl_rpc("eth_blockNumber")
            if "error" in resp:
                log(f"❌ RPC down: {resp['error'].get('message','')[:50]}")
                time.sleep(30)
                continue

            block = int(resp["result"], 16)
            log(f"✅ RPC online, block={block}")

            # Step 1: Approve router on native USDC
            # Find router from recent txs
            TOLLY = TOLLY_DEX.lower().replace("0x","")
            resp2 = curl_rpc("eth_getLogs", [{
                "fromBlock": hex(block - 50),
                "toBlock": hex(block),
                "address": TOLLY_DEX
            }])
            if resp2.get("error"):
                log(f"❌ getLogs failed: {resp2['error']}")
                time.sleep(30)
                continue

            logs = resp2["result"]
            tx_hashes = list(set(l["transactionHash"] for l in logs))
            log(f"Found {len(tx_hashes)} DEX txs")

            # Find the best buy calldata
            best = None
            for txh in tx_hashes:
                resp3 = curl_rpc("eth_getTransactionByHash", [txh])
                if resp3.get("error") or not resp3.get("result"):
                    continue
                tx = resp3["result"]
                data = tx["input"]
                if not data: continue
                if data[:10] != "0x9265bb9d":
                    continue

                # Get receipt to check status
                resp4 = curl_rpc("eth_getTransactionReceipt", [txh])
                if resp4.get("error") or not resp4.get("result"):
                    continue
                rcpt = resp4["result"]
                if int(rcpt.get("status","0x0"), 16) != 1:
                    continue
                gas_used = int(rcpt.get("gasUsed","0x0"), 16)
                if best is None or gas_used < best[1]:
                    best = (tx, gas_used, data)

            if not best:
                log("❌ No suitable buy tx found")
                time.sleep(30)
                continue

            orig_tx, gas, calldata = best
            router = orig_tx["to"]
            log(f"Router: {router[:12]}... gas={gas} len={len(calldata)}")

            # Step 1: Check current allowance
            # allowance(owner, spender) = 0xdd62ed3e
            allow_data = "0xdd62ed3e" + \
                WALLET[2:].lower().zfill(64) + \
                router[2:].lower().zfill(64)
            resp5 = curl_rpc("eth_call", [{"from": WALLET, "to": NATIVE_USDC, "data": allow_data}, "latest"])
            if resp5.get("result") and resp5["result"] != "0x":
                current_allow = int(resp5["result"], 16)
                log(f"Current allowance: {current_allow/1e18:.6f}")
            else:
                current_allow = 0
                log(f"Allowance: 0 (need to approve)")

            # Step 2: Approve if needed
            if current_allow < 100 * 10**18:
                log(f"🔐 Approving router on native USDC...")
                approve_data = "0x095ea7b3" + \
                    router[2:].lower().zfill(64) + \
                    hex(10000*10**18)[2:].zfill(64)
                rcpt = send_tx(NATIVE_USDC, approve_data, gas=100000)
                if rcpt:
                    status = int(rcpt.get("status","0x0"), 16)
                    log(f"Approve: {'✅' if status==1 else '❌'}")
                    if status != 1:
                        log("❌ Approve failed, retrying later")
                        time.sleep(30)
                        continue
                else:
                    log("❌ Approve tx failed to send")
                    time.sleep(30)
                    continue
            else:
                log("✅ Already approved")

            # Step 3: Simulate buy
            log("🧪 Simulating buy...")
            resp6 = curl_rpc("eth_call", [{"from": WALLET, "to": router, "data": calldata, "value": "0x0"}, "latest"])
            if resp6.get("result"):
                log(f"  Sim: ✅ {resp6['result'][:40]}")
            elif resp6.get("error"):
                log(f"  Sim: ❌ {str(resp6['error'])[:60]}")

            # Step 4: SEND BUY!
            log("🚀 SENDING BUY TX!!!")
            rcpt2 = send_tx(router, calldata, gas=500000)
            if rcpt2:
                status2 = int(rcpt2.get("status","0x0"), 16)
                gas2 = int(rcpt2.get("gasUsed","0x0"), 16)
                log(f"\n{'='*60}")
                log(f"Status: {'✅ SUCCESS 🎉' if status2==1 else '❌ FAILED'}")
                log(f"Gas: {gas2}")

                TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df031b6ef"
                received = []
                for log_entry in rcpt2.get("logs", []):
                    topic0 = log_entry.get("topics",[""])[0] if log_entry.get("topics") else ""
                    addr = log_entry.get("address","")
                    if topic0 == TRANSFER:
                        from_a = "0x" + log_entry["topics"][1][24:64]
                        to_a = "0x" + log_entry["topics"][2][24:64]
                        amount = int(log_entry.get("data","0x"), 16)
                        direction = ""
                        if to_a.lower() == WALLET[2:].lower():
                            direction = "📥 IN"
                            received.append((addr, amount))
                        if from_a.lower() == WALLET[2:].lower():
                            direction = "📤 OUT"
                        if direction:
                            log(f"  {direction} {addr[:12]}... = {amount/1e18:.6f}")
                    else:
                        log(f"  Event: {topic0[:20]}... on {addr[:12]}...")

                if received:
                    log("\n🎉🎉🎉 BOUGHT TOKENS! 🎉🎉🎉")
                    for token, amount in received:
                        # Try to get symbol
                        try:
                            sym_resp = curl_rpc("eth_call", [{"from": WALLET, "to": token, "data": "0x95d89b41"}, "latest"])
                            dec_resp = curl_rpc("eth_call", [{"from": WALLET, "to": token, "data": "0x313ce567"}, "latest"])
                            if sym_resp.get("result"):
                                sym_hex = sym_resp["result"][2:]
                                sym_bytes = bytes.fromhex(sym_hex)
                                # Decode string (offset 64, then length, then data)
                                # ABI encoded string: 32 bytes offset + 32 bytes length + data
                                sym_str = sym_bytes[64:64+int(sym_hex[64:128],16)*2].decode('utf-8', errors='replace')
                                dec_val = int(dec_resp.get("result","0x12"), 16)
                                log(f"  📦 {amount/10**dec_val} {sym_str}")
                            else:
                                log(f"  📦 {amount/1e18:.6f} {token[:12]}")
                        except:
                            log(f"  📦 {amount/1e18:.6f} {token[:12]}")

                log(f"  TX: {rcpt2.get('transactionHash','?')}")
                log(f"{'='*60}")

                with open("/tmp/arc_buy_result.json", "w") as f:
                    json.dump({"success": status2==1, "received": received, "tx": rcpt2.get("transactionHash")}, f, indent=2)

                if status2 == 1:
                    log("🎉 DONE!")
                    return 0
                else:
                    log("❌ TX failed, retrying...")
                    time.sleep(60)
                    continue
            else:
                log("❌ Buy tx failed to send")
                time.sleep(30)
                continue

        except Exception as e:
            log(f"❌ Error: {str(e)[:100]}")
            time.sleep(30)

    log("⏰ Timed out after 1 hour")
    return 1

if __name__ == "__main__":
    sys.exit(main())
