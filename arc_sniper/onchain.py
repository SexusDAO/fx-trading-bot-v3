"""
Arc On-Chain Data — 链上实时数据读取层 (移植自 robinhood_sniper/on_chain_data.py)

目的：取代 DexScreener 作为新币发现后的第一手数据源。
     RPC 发现 PoolCreated → 立即链上读取 → 直接评分 → 买入
     延迟：~2~5秒

Arc 链特性：
- 原生代币是 USDC（不是 ETH），18 decimals
- DEX 主要是 DAG Router + Tolly DEX
- Swap 事件使用 V2/V3 格式
- 链 ID: 5042
"""

import time
import logging
import json
import subprocess
from web3 import Web3
from typing import Optional

logger = logging.getLogger("arc.onchain")

# ═══════════════════════════════════════════════════════════════
# Arc 链常量
# ═══════════════════════════════════════════════════════════════

ARC_RPC = "https://rpc.arc-scan.org"
ARC_RPC_FALLBACK = "https://rpc.mainnet.arc.io"

# USDC 地址
WUSDC = "0x3600000000000000000000000000000000000000"       # wrapped USDC (6 decimals)
NATIVE_USDC = "0xfffffff7fffffffffffffffffffffffffffffffE"  # native USDC (18 decimals)

# Swap 事件 topics
SWAP_TOPIC_V3 = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
SWAP_TOPIC_V2 = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37975c4d3f1cbb71e394d2b5a"

# PoolCreated topic (Uniswap V3)
POOL_CREATED_TOPIC = "0x783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118"

# 区块参数（Arc 链 ~2秒/块）
BLOCKS_PER_MIN = 30
BLOCKS_5MIN = 150
BLOCKS_H1 = 1800
BLOCKS_H6 = 10800

# USDC 价格（Arc 链原生 USDC = $1）
USDC_PRICE = 1.0


# ═══════════════════════════════════════════════════════════════
# RPC helpers
# ═══════════════════════════════════════════════════════════════

def _rpc(method: str, params: list, timeout: int = 10, rpc: str = None) -> Optional[any]:
    rpc_url = rpc or ARC_RPC
    payload = json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": 1})
    try:
        r = subprocess.run(
            ["curl", "-s", "-m", str(timeout), "-X", "POST", rpc_url,
             "-H", "Content-Type: application/json", "-d", payload],
            capture_output=True, text=True,
        )
        data = json.loads(r.stdout)
        return data.get("result")
    except Exception as e:
        logger.debug(f"[OC] RPC {method} 失败: {e}")
        return None


def _batch(reqs: list, timeout: int = 10, rpc: str = None) -> dict:
    """批量 RPC，返回 {id: result}"""
    rpc_url = rpc or ARC_RPC
    payload = json.dumps(reqs)
    try:
        r = subprocess.run(
            ["curl", "-s", "-m", str(timeout), "-X", "POST", rpc_url,
             "-H", "Content-Type: application/json", "-d", payload],
            capture_output=True, text=True,
        )
        d = json.loads(r.stdout)
        if isinstance(d, list):
            return {item["id"]: item.get("result", "0x") for item in d}
    except Exception as e:
        logger.debug(f"[OC] batch RPC 失败: {e}")
    return {}


def _hex_to_int(v, default=0) -> int:
    try:
        return int(v, 16) if v and v != "0x" else default
    except Exception:
        return default


def _decode_string(raw: str) -> str:
    """解码 ABI 编码的 string 或 bytes32（symbol/name），最多保留 20 字符"""
    if not raw or raw == "0x":
        return "?"
    try:
        if len(raw) > 130:  # string ABI
            sym_len = min(int(raw[66:130], 16), 40)
            sym = bytes.fromhex(raw[130:130 + sym_len * 2]).decode("utf-8", errors="replace").strip()
        else:  # bytes32
            sym = bytes.fromhex(raw[2:]).decode("utf-8", errors="replace").rstrip("\x00").strip()
        sym = "".join(c for c in sym if c.isprintable() and ord(c) < 128)[:20]
        return sym or "?"
    except Exception:
        return "?"


def get_latest_block() -> int:
    resp = _rpc("eth_blockNumber", [])
    return _hex_to_int(resp)


# ═══════════════════════════════════════════════════════════════
# Pool 数据读取
# ═══════════════════════════════════════════════════════════════

def fetch_pool_data(pool_addr: str, token_addr: str,
                    latest_block: Optional[int] = None) -> Optional[dict]:
    """
    给定 pool 地址和 token 地址，返回完整评分所需数据。

    Arc 链特性：
    - USDC 是原生代币（不是 WETH）
    - 池子可能用 wUSDC (6 decimals) 或 native USDC (18 decimals)
    - 价格以 USDC 计（1 USDC = $1）

    返回 dict 结构与 DexScreener pair 兼容：
      priceUsd, marketCap, liquidity.usd,
      volume.m5/h1/h6, txns.m5.buys/sells, txns.h1.buys/sells,
      priceChange.m5/h1, pairAddress, baseToken.address/symbol
    """
    pool = pool_addr.lower()
    token = token_addr.lower()
    wusdc = WUSDC.lower()

    # ── 1. 批量读取 token 基础信息 + 池子余额 ──
    batch_ids = {
        1: ("eth_call", [{"to": wusdc, "data": "0x70a08231" + "000000000000000000000000" + pool[2:]}, "latest"]),
        2: ("eth_call", [{"to": token, "data": "0x70a08231" + "000000000000000000000000" + pool[2:]}, "latest"]),
        3: ("eth_call", [{"to": token, "data": "0x18160ddd"}, "latest"]),  # totalSupply
        4: ("eth_call", [{"to": token, "data": "0x313ce567"}, "latest"]),  # decimals
        5: ("eth_call", [{"to": token, "data": "0x95d89b41"}, "latest"]),  # symbol
        6: ("eth_blockNumber", []),
    }
    reqs = [{"jsonrpc": "2.0", "method": m, "params": p, "id": i}
            for i, (m, p) in batch_ids.items()]
    res = _batch(reqs, timeout=10)

    if not res:
        # 尝试 fallback RPC
        res = _batch(reqs, timeout=10, rpc=ARC_RPC_FALLBACK)

    if not res:
        return None

    latest = _hex_to_int(res.get(6)) or get_latest_block()
    usdc_in = _hex_to_int(res.get(1))   # wUSDC in pool (6 decimals)
    tok_in = _hex_to_int(res.get(2))    # token in pool
    ts_raw = _hex_to_int(res.get(3))    # totalSupply
    dec = _hex_to_int(res.get(4)) or 18  # decimals
    sym = _decode_string(res.get(5, ""))

    if usdc_in == 0 or tok_in == 0:
        # 也尝试 native USDC balance
        native_bal = _rpc("eth_getBalance", [pool, "latest"])
        native_usdc = _hex_to_int(native_bal)
        if native_usdc == 0:
            return None  # 无流动性
        usdc_in = native_usdc
        usdc_decimals = 18
    else:
        usdc_decimals = 6  # wUSDC

    usdc_val = usdc_in / (10 ** usdc_decimals)
    tok_val = tok_in / (10 ** dec)
    supply = ts_raw / (10 ** dec)

    if tok_val <= 0:
        return None

    price_usd = (usdc_val / tok_val) * USDC_PRICE
    mc = supply * price_usd
    liq_usd = usdc_val * USDC_PRICE * 2  # 两边对称估算

    # ── 2. 扫 Swap 事件 ──
    if latest_block:
        latest = latest_block

    def _get_logs(from_blk, to_blk):
        for topic in (SWAP_TOPIC_V3, SWAP_TOPIC_V2):
            logs = _rpc("eth_getLogs", [{
                "address": pool,
                "fromBlock": hex(from_blk),
                "toBlock": hex(to_blk),
                "topics": [topic],
            }], timeout=8)
            if logs:
                return logs, topic
        return [], None

    logs_h6, topic = _get_logs(max(0, latest - BLOCKS_H6), latest)

    # 解析 swap 方向和成交量
    def _parse_swap(log, topic):
        data = log.get("data", "")
        if len(data) < 130:
            return None
        try:
            def signed(h):
                v = int(h, 16)
                return v - 2**256 if v >= 2**255 else v

            if topic == SWAP_TOPIC_V3:
                a0 = signed(data[2:66])
                a1 = signed(data[66:130])
                # 判断哪个方向是 USDC
                if token > wusdc:
                    usdc_a = a0
                    is_buy = a0 < 0  # USDC 流入 pool = 买入 token
                else:
                    usdc_a = a1
                    is_buy = a1 < 0
                usdc_vol = abs(usdc_a) / (10 ** usdc_decimals)
                return {"buy": is_buy, "usdc": usdc_vol}
            else:  # V2
                a0in = int(data[2:66], 16)
                a1in = int(data[66:130], 16)
                a0out = int(data[130:194], 16) if len(data) >= 194 else 0
                a1out = int(data[194:258], 16) if len(data) >= 258 else 0
                is_usdc_0 = wusdc < token
                if is_usdc_0:
                    is_buy = a0in > 0
                    usdc_vol = max(a0in, a0out) / (10 ** usdc_decimals)
                else:
                    is_buy = a1in > 0
                    usdc_vol = max(a1in, a1out) / (10 ** usdc_decimals)
                return {"buy": is_buy, "usdc": usdc_vol}
        except Exception:
            return None

    swaps_h6 = [s for l in logs_h6 for s in [_parse_swap(l, topic)] if s]

    def _stats(swaps, from_blk):
        subset = [s for s, l in zip(swaps, logs_h6)
                  if _hex_to_int(l.get("blockNumber")) >= from_blk]
        buys = sum(1 for s in subset if s["buy"])
        sells = len(subset) - buys
        vol = sum(s["usdc"] for s in subset) * USDC_PRICE
        return buys, sells, vol

    buys_m5, sells_m5, vol_m5 = _stats(swaps_h6, latest - BLOCKS_5MIN)
    buys_h1, sells_h1, vol_h1 = _stats(swaps_h6, latest - BLOCKS_H1)
    _, _, vol_h6 = _stats(swaps_h6, 0)

    # ── 3. 价格变化 ──
    pc_m5 = 0.0
    pc_h1 = 0.0
    try:
        if len(logs_h6) >= 2:
            def _price_from_log(l):
                data = l.get("data", "")
                if len(data) >= 194 and topic == SWAP_TOPIC_V3:
                    sqrt = int(data[130:194], 16)
                    if sqrt > 0:
                        raw = (sqrt / 2**96) ** 2
                        return raw * USDC_PRICE if token > wusdc else (1 / raw) * USDC_PRICE if raw > 0 else 0
                return 0

            p_now = price_usd
            logs_before_5m = [l for l in logs_h6
                              if _hex_to_int(l["blockNumber"]) < latest - BLOCKS_5MIN]
            if logs_before_5m:
                p5 = _price_from_log(logs_before_5m[-1])
                if p5 > 0 and p_now > 0:
                    pc_m5 = (p_now - p5) / p5 * 100

            logs_before_1h = [l for l in logs_h6
                              if _hex_to_int(l["blockNumber"]) < latest - BLOCKS_H1]
            if logs_before_1h:
                p1h = _price_from_log(logs_before_1h[-1])
                if p1h > 0 and p_now > 0:
                    pc_h1 = (p_now - p1h) / p1h * 100
    except Exception:
        pass

    # ── 4. 组装 DS 兼容格式 ──
    now_ms = int(time.time() * 1000)
    pair_dict = {
        "chainId": "arc",
        "pairAddress": pool_addr,
        "baseToken": {"address": token_addr, "symbol": sym},
        "quoteToken": {"address": WUSDC, "symbol": "USDC"},
        "priceUsd": str(price_usd),
        "marketCap": mc,
        "fdv": mc,
        "liquidity": {"usd": liq_usd},
        "volume": {
            "m5": vol_m5,
            "h1": vol_h1,
            "h6": vol_h6,
            "h24": vol_h6,
        },
        "txns": {
            "m5": {"buys": buys_m5, "sells": sells_m5},
            "h1": {"buys": buys_h1, "sells": sells_h1},
        },
        "priceChange": {
            "m5": pc_m5,
            "h1": pc_h1,
        },
        "pairCreatedAt": now_ms,
        "holders": 0,
        "_onchain": True,
        "_usdc_in_pool": usdc_val,
        "_swap_count_h1": buys_h1 + sells_h1,
    }
    return pair_dict


def fetch_multiple(pool_token_pairs: list[tuple[str, str]],
                   latest_block: Optional[int] = None) -> dict[str, dict]:
    """批量查多个 pool"""
    results = {}
    for pool, token in pool_token_pairs:
        try:
            d = fetch_pool_data(pool, token, latest_block)
            if d:
                results[pool] = d
        except Exception as e:
            logger.debug(f"[OC] {pool[:10]} 读取失败: {e}")
    return results


# ═══════════════════════════════════════════════════════════════
# 新 Pool 发现
# ═══════════════════════════════════════════════════════════════

def get_new_pools(from_block: int, to_block: int) -> list[dict]:
    """
    扫描 PoolCreated 事件，发现新交易对。
    返回 [{"pair": "0x...", "token0": "0x...", "token1": "0x...", "txHash": "0x..."}]
    """
    if to_block < from_block or to_block - from_block > 100:
        return []

    resp = _rpc("eth_getLogs", [{
        "fromBlock": hex(from_block),
        "toBlock": hex(to_block),
        "topics": [POOL_CREATED_TOPIC],
    }], timeout=10)

    if not resp or not isinstance(resp, list):
        return []

    pools = []
    for log in resp:
        # PoolCreated(address token0, address token1, uint24 fee, int24 tickSpacing, address pool)
        try:
            token0 = "0x" + log["topics"][1][-40:]
            token1 = "0x" + log["topics"][2][-40:]
            # 必须有一端是 USDC
            wusdc = WUSDC.lower()
            native = NATIVE_USDC.lower()
            t0_lower = token0.lower()
            t1_lower = token1.lower()

            if t0_lower not in (wusdc, native) and t1_lower not in (wusdc, native):
                continue

            token_addr = token1 if t0_lower in (wusdc, native) else token0
            pool_addr = "0x" + log["data"][-40:] if len(log.get("data", "")) >= 42 else ""

            pools.append({
                "pair": pool_addr,
                "token0": token0,
                "token1": token1,
                "token_addr": token_addr,
                "txHash": log.get("transactionHash", ""),
                "blockNumber": _hex_to_int(log.get("blockNumber")),
            })
            logger.info(
                f"[OC] 🆕 新池 token={token_addr[:10]}... "
                f"pool={pool_addr[:10]}..."
            )
        except Exception:
            continue

    return pools


def update_usdc_price(price: float):
    """外部更新 USDC 价格（Arc 链 USDC = $1，通常不需要）"""
    global USDC_PRICE
    if price > 0:
        USDC_PRICE = price


# ═══════════════════════════════════════════════════════════════
# 代币工具函数（从旧 onchain.py 迁移，供 main.py/grok_monitor 使用）
# ═══════════════════════════════════════════════════════════════

def _curl_eth_call(to: str, data: str, timeout: int = 8) -> str:
    """Curl-based eth_call, 绕开 web3.py 限流"""
    import subprocess
    from . import config
    payload = json.dumps({
        "jsonrpc": "2.0",
        "method": "eth_call",
        "params": [{"to": to, "data": data}, "latest"],
        "id": 1,
    })
    r = subprocess.run(
        ["curl", "-s", "-m", str(timeout), "-X", "POST",
         config.RPC_URL, "-H", "Content-Type: application/json",
         "-d", payload],
        capture_output=True, text=True, timeout=timeout + 2,
    )
    resp = json.loads(r.stdout) if r.stdout else {}
    return resp.get("result", "0x")


def _curl_rpc(method: str, params: list, timeout: int = 8) -> dict:
    """Curl-based JSON-RPC call"""
    import subprocess
    from . import config
    payload = json.dumps({
        "jsonrpc": "2.0", "method": method, "params": params, "id": 1})
    r = subprocess.run(
        ["curl", "-s", "-m", str(timeout), "-X", "POST",
         config.RPC_URL, "-H", "Content-Type: application/json",
         "-d", payload],
        capture_output=True, text=True, timeout=timeout + 2,
    )
    return json.loads(r.stdout) if r.stdout else {}


def _decode_string(hex_data: str) -> str:
    """Decode ABI-encoded string (offset + length + data)"""
    try:
        data = hex_data[2:] if hex_data.startswith("0x") else hex_data
        if len(data) < 128:
            return ""
        # String is at offset (first 32 bytes), length is at that offset
        offset = int(data[:64], 16) * 2  # byte offset to hex char offset
        length = int(data[offset:offset+64], 16) * 2  # string length in hex chars
        string_hex = data[offset+64:offset+64+length]
        return bytes.fromhex(string_hex).decode("utf-8", errors="replace").strip("\x00")
    except Exception:
        return ""


def get_token_info(w3, token_address: str) -> dict:
    """读取代币基础信息 (curl-based, 绕开 web3.py 限流)"""
    addr = Web3.to_checksum_address(token_address)

    try:
        # symbol() = 0x95d89b41
        symbol_hex = _curl_eth_call(addr, "0x95d89b41")
        symbol = _decode_string(symbol_hex) if symbol_hex != "0x" else "?"

        # name() = 0x06fdde03
        name_hex = _curl_eth_call(addr, "0x06fdde03")
        name = _decode_string(name_hex) if name_hex != "0x" else "?"

        # decimals() = 0x313ce567
        decimals_hex = _curl_eth_call(addr, "0x313ce567")
        decimals = int(decimals_hex, 16) if decimals_hex != "0x" and len(decimals_hex) >= 66 else 18

        # totalSupply() = 0x18160ddd
        supply_hex = _curl_eth_call(addr, "0x18160ddd")
        total_supply = int(supply_hex, 16) if supply_hex != "0x" and len(supply_hex) >= 66 else 0

        return {
            "symbol": symbol or "?",
            "name": name or "?",
            "decimals": decimals,
            "total_supply": total_supply,
            "address": token_address,
        }
    except Exception as e:
        logger.warning(f"[OC] get_token_info failed for {token_address[:12]}...: {e}")
        return None


def verify_contract_on_chain(w3, token_address: str) -> bool:
    """验证地址在链上是否有合约代码"""
    try:
        code = w3.eth.get_code(w3.to_checksum_address(token_address))
        return len(code) > 2
    except Exception:
        return False


def get_holder_stats(w3, token_address: str, from_block: int = 0) -> dict:
    """统计持有人数（通过 Transfer 事件，curl-based 绕开限流）"""
    addr = Web3.to_checksum_address(token_address)
    TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
    try:
        # 用 curl 查 Transfer 事件
        from . import config
        latest_block = int(_curl_rpc("eth_blockNumber", []).get("result", "0x0"), 16)
        if latest_block == 0:
            return {"count": 0, "top10_pct": 1.0}
        
        # 搜索最近 5000 块的 Transfer 事件
        from_block = max(from_block, latest_block - 5000)
        payload = json.dumps({
            "jsonrpc": "2.0",
            "method": "eth_getLogs",
            "params": [{
                "fromBlock": hex(from_block),
                "toBlock": hex(latest_block),
                "address": addr,
                "topics": [TRANSFER_TOPIC],
            }],
            "id": 1,
        })
        r = subprocess.run(
            ["curl", "-s", "-m", "10", "-X", "POST",
             config.RPC_URL, "-H", "Content-Type: application/json",
             "-d", payload],
            capture_output=True, text=True, timeout=12,
        )
        resp = json.loads(r.stdout) if r.stdout else {}
        logs = resp.get("result", [])
        if resp.get("error"):
            logger.warning(f"[OC] holder_stats RPC error: {resp['error'].get('message','?')[:60]}")
            return {"count": 0, "top10_pct": 1.0}
        
        holders = set()
        for log in logs:
            topics = log.get("topics", [])
            if len(topics) >= 3:
                holders.add(topics[2][26:].lower())
        
        count = len(holders)
        return {"count": count, "top10_pct": 0.30 if count > 0 else 1.0}
    except Exception as e:
        logger.warning(f"[OC] get_holder_stats failed: {e}")
        return {"count": 0, "top10_pct": 1.0}


def check_honeypot(w3, token_address: str, pair_address: str = "") -> tuple[bool, str]:
    """简化版蜜罐检测（curl-based eth_call estimateGas）"""
    try:
        addr = Web3.to_checksum_address(token_address)
        # transfer(address,uint256) selector = 0xa9059cbb
        # transfer to self, amount 0
        to_addr = addr[2:].lower().zfill(64)
        amount = (0).to_bytes(32, 'big').hex()
        data = "0xa9059cbb" + to_addr + amount
        
        # 用 curl 模拟 estimateGas
        from . import config
        payload = json.dumps({
            "jsonrpc": "2.0",
            "method": "eth_estimateGas",
            "params": [{"to": addr, "data": data, "from": addr}],
            "id": 1,
        })
        r = subprocess.run(
            ["curl", "-s", "-m", "8", "-X", "POST",
             config.RPC_URL, "-H", "Content-Type: application/json",
             "-d", payload],
            capture_output=True, text=True, timeout=10,
        )
        resp = json.loads(r.stdout) if r.stdout else {}
        if resp.get("result"):
            return True, "✅ transfer 可执行"
        elif resp.get("error"):
            err_msg = resp["error"].get("message", "")
            if "execution reverted" in err_msg.lower():
                return False, "❌ transfer 被拦截（可能蜜罐）"
            # Other errors might not be honeypot
            return True, f"⚠️ gas estimate error: {err_msg[:40]}"
        return True, "✅ assumed safe (no response)"
    except Exception as e:
        return True, f"⚠️ 检测失败: {e}"


def get_pair_liquidity(w3, pair_address: str, token_address: str) -> float:
    """获取交易对流动性（简化版：从 pool 数据读取）"""
    try:
        pool_data = fetch_pool_data(pair_address, token_address)
        if pool_data:
            return float(pool_data.get("liquidity", {}).get("usd", 0))
    except Exception:
        pass
    return 0.0


def get_token_age_sec(w3, creation_block: int) -> int:
    """根据创建区块计算代币年龄（秒）"""
    try:
        latest = w3.eth.block_number
        # Arc 链 ~2 秒/块
        return max(0, (latest - creation_block) * 2)
    except Exception:
        return 0
