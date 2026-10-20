import os
#!/usr/bin/env python3
"""Check wUSDC mechanism and try alternative buy strategies"""
from web3 import Web3
import subprocess, json, time

RPC = "https://rpc.arc-scan.org"
PK = os.environ["ARC_PK"]  # 不接受默认值，必须在环境变量中设置
WALLET = Web3.to_checksum_address("0x8A966Db06aD9624a0E41bb4adfD817c5e7e55F54")
WUSDC = Web3.to_checksum_address("0x3600000000000000000000000000000000000000")
TOLLY_DEX = Web3.to_checksum_address("0x114d574247342eafea95b7a9234e62984d48cD9E")
CHAIN_ID = 5042

w3 = Web3(Web3.HTTPProvider(RPC, request_kwargs={"timeout": 15}))
acct = w3.eth.account.from_key(PK)

abi = [
    {"constant":True,"inputs":[],"name":"totalSupply","outputs":[{"name":"","type":"uint256"}],"type":"function"},
    {"constant":True,"inputs":[],"name":"symbol","outputs":[{"name":"","type":"string"}],"type":"function"},
    {"constant":True,"inputs":[],"name":"name","outputs":[{"name":"","type":"string"}],"type":"function"},
    {"constant":True,"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"type":"function"},
    {"constant":True,"inputs":[{"name":"","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"type":"function"},
]
c = w3.eth.contract(address=WUSDC, abi=abi)
print(f"wUSDC: {c.functions.name().call()} ({c.functions.symbol().call()})")
print(f"Decimals: {c.functions.decimals().call()}")
print(f"Total Supply: {c.functions.totalSupply().call() / 1e18:.6f}")

# Check if the DEX has wUSDC
dex_bal = c.functions.balanceOf(TOLLY_DEX).call()
print(f"DEX wUSDC: {dex_bal / 1e18:.6f}")

# Check top wUSDC holders from recent DEX interactions
latest = w3.eth.block_number
logs = w3.eth.get_logs({"fromBlock": latest-100, "toBlock": latest, "address": TOLLY_DEX})
tx_hashes = list(set(l["transactionHash"] for l in logs))[:10]
for txh in tx_hashes:
    tx = w3.eth.get_transaction(txh)
    buyer = tx["from"]
    bal = c.functions.balanceOf(buyer).call()
    print(f"  Buyer {buyer[:12]}... wUSDC={bal/1e18:.6f}")

# Now try: deploy a wrapper contract that wraps USDC and returns wUSDC
# Solidity: contract Wrap { receive() external payable { (bool s,) = WUSDC.call{value: msg.value}(""); require(s); uint256 bal = IERC20(WUSDC).balanceOf(address(this)); IERC20(WUSDC).transfer(msg.sender, bal); } }
# But we can't compile Solidity here. Let me write raw EVM bytecode.

# Actually, let me try a different approach:
# Deploy a contract that just holds funds, using a minimal bytecode
# Then we can call it to wrap for us

# Minimal contract bytecode that on receive calls wUSDC.deposit and transfers back
# This is complex to write in raw bytecode. Let me try a simpler approach.

# Alternative: Can we call wUSDC.deposit() from another contract?
# The router contracts might have this capability. Let me try calling
# the router with just the deposit() selector + value

print("\n=== Try deposit via router ===")
routers = [
    Web3.to_checksum_address("0xb99654A2537cDc4C65368262F9E03E2128dfd78B"),
    Web3.to_checksum_address("0xDe76a089263627Cc6AC658953474Dc87bA96447c"),
    Web3.to_checksum_address("0x6aAFD2F727684D560638f60124ff6d3e7651262f"),
]

for r in routers:
    # Try calling deposit() on router with value
    try:
        result = w3.eth.call({
            "from": WALLET, "to": r, "data": "0xd0e30db0", "value": 10**18
        }, "latest")
        print(f"  {r[:12]} deposit(): ✅ {result.hex()[:40]}")
    except Exception as e:
        print(f"  {r[:12]} deposit(): ❌ {str(e)[:60]}")
    
    # Try calling with empty data + value (fallback)
    try:
        result = w3.eth.call({
            "from": WALLET, "to": r, "data": "0x", "value": 10**18
        }, "latest")
        print(f"  {r[:12]} fallback(): ✅ {result.hex()[:40]}")
    except Exception as e:
        print(f"  {r[:12]} fallback(): ❌ {str(e)[:60]}")

# Try: send value directly to DEX with buy calldata
print("\n=== Try buy calldata with value on different routers ===")
best = None
logs2 = w3.eth.get_logs({"fromBlock": latest-200, "toBlock": latest, "address": TOLLY_DEX})
tx_hashes2 = list(set(l["transactionHash"] for l in logs2))
for txh in tx_hashes2:
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

if best:
    orig_tx, gas, calldata = best
    router = orig_tx["to"]
    print(f"  Original router: {router[:12]}...")
    
    # Try simulating with different values on different routers
    for r in routers + [TOLLY_DEX]:
        for val in [0, 10**18, 20*10**18]:
            try:
                result = w3.eth.call({
                    "from": WALLET, "to": r, "data": calldata, "value": val
                }, "latest")
                print(f"  {r[:12]} val={val/1e18}: ✅ {result.hex()[:40]}")
            except Exception as e:
                err = str(e)[:50]
                if val == 0:
                    print(f"  {r[:12]} val=0: ❌ {err}")

# Check the wUSDC contract's bytecode to understand its deposit mechanism
print("\n=== wUSDC contract bytecode ===")
wusdc_code = w3.eth.get_code(WUSDC)
print(f"Code size: {len(wusdc_code)} bytes")
print(f"Code (first 200 chars): {wusdc_code.hex()[:200]}")
# Check if it has standard WETH9 functions
# deposit() = 0xd0e30db0
# withdraw() = 0x2e1a7d4d
# balanceOf = 0x70a08231

# Check if wUSDC is behind a proxy
# ERC1967 proxy: implementation slot = 0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc
impl_slot = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
impl_result, _ = (None, None)
try:
    impl_data = w3.eth.get_storage_at(WUSDC, impl_slot)
    impl_addr = "0x" + impl_data.hex()[24:64]
    print(f"Implementation slot: {impl_addr}")
except:
    print("Not a proxy (no implementation slot)")

# Actually, just check the first 4 bytes of the code
# If it starts with 0x363d3d37 it's a minimal proxy
code_hex = wusdc_code.hex()
if code_hex.startswith("363d3d373d3d3d363d73"):
    impl = "0x" + code_hex[20:60]
    print(f"Minimal proxy -> {impl}")
elif len(wusdc_code) > 1000:
    print("Full contract (not a proxy)")
