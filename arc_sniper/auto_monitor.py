#!/usr/bin/env python3
"""
Auto-monitor: 检测 RPC 恢复后自动执行 replay_buy.py
每次检测间隔 30 秒，最多尝试 120 次（1 小时）
成功买入后自动退出
"""
import subprocess, sys, os, time, json
from datetime import datetime

from web3 import Web3

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import RPC_URL, CHAIN_ID

LOG_FILE = "/tmp/arc_auto_monitor.log"

def log(msg):
    ts = datetime.utcnow().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

def check_rpc():
    """Try connecting to RPC"""
    try:
        w3 = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": 10}))
        if w3.is_connected() and w3.eth.chain_id == CHAIN_ID:
            block = w3.eth.block_number
            log(f"✅ RPC ONLINE — block={block} chain={w3.eth.chain_id}")
            return True
        else:
            log(f"❌ RPC connected but wrong chain or no response")
            return False
    except Exception as e:
        log(f"❌ RPC down: {e}")
        return False

def check_balance():
    """Check wallet has USDC"""
    try:
        w3 = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": 10}))
        addr = "0x8A966Db06aD9624a0E41bb4adfD817c5e7e55F54"
        bal = w3.eth.get_balance(addr)
        log(f"💰 Wallet balance: {bal / 1e18:.3f} USDC")
        return bal > 0
    except Exception as e:
        log(f"❌ Balance check failed: {e}")
        return False

def run_buy():
    """Execute replay_buy.py"""
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "replay_buy.py")
    log(f"🚀 Launching {script} ...")
    result = subprocess.run(
        [sys.executable, script],
        capture_output=True, text=True, timeout=120, cwd=os.path.dirname(script)
    )
    log(f"stdout: {result.stdout}")
    if result.stderr:
        log(f"stderr: {result.stderr}")
    log(f"return code: {result.returncode}")
    return result.returncode == 0

def main():
    log("=" * 50)
    log("🤖 Arc Auto Monitor 启动")
    log(f"RPC: {RPC_URL}")
    log(f"Max retries: 120 (1 hour)")
    log("=" * 50)

    for i in range(120):
        log(f"--- Check {i+1}/120 ---")
        if check_rpc():
            if check_balance():
                # RPC is back and we have balance, execute buy
                success = run_buy()
                if success:
                    log("🎉 BUY SUCCESS! Exiting.")
                    # Write success marker
                    with open("/tmp/arc_buy_success.json", "w") as f:
                        json.dump({"success": True, "time": datetime.utcnow().isoformat()}, f)
                    return 0
                else:
                    log("❌ Buy failed, retrying in 60s...")
                    time.sleep(60)
                    continue
            else:
                log("⚠️ RPC back but no balance, waiting 30s...")
        time.sleep(30)

    log("⏰ Timed out after 1 hour. RPC never recovered.")
    with open("/tmp/arc_buy_success.json", "w") as f:
        json.dump({"success": False, "reason": "rpc_timeout", "time": datetime.utcnow().isoformat()}, f)
    return 1

if __name__ == "__main__":
    sys.exit(main())
