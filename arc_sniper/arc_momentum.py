import os
#!/usr/bin/env python3
"""
Arc Pre-Pump Scanner v2 — 基于历史回测优化的信号检测 + 自动买入

v2 优化（基于 8000 blocks / 6 个真实拉盘 token 回测）:
  1. 买入比例阈值 70%→50% (中位数 50%，很多金狗先砸后拉)
  2. 流动性变化 50%→10% (中位数仅 +9%，极不稳定)
  3. 交易者数 5→3 (P25=8，但门槛太高反而漏掉)
  4. 频率加速 2x→1.3x (实际中位数 0.4x，频率下降也能拉盘)
  5. 新增: 价格波动率 >=15% (拉盘前价格范围中位数 16.3%)
  6. 新增: 活跃度门槛 >=3 笔/30min (过滤死币)
  7. 自动买入门槛: ≥2 信号 + score≥50 (原 3 信号只覆盖 20%)

回测结果:
  原阈值 (70%/50%/5tr/3sig): 20% 检出率
  新阈值 (50%/10%/3tr/2sig): 60% 检出率
"""
import subprocess, json, time, sys, os, collections

# ════════════════════════════════════════════════════════════
# CONFIG
# ════════════════════════════════════════════════════════════
RPC = "https://rpc.mainnet.arc.io"
RPC_FALLBACK = "https://rpc.arc-scan.org"
PK = os.environ["ARC_PK"]  # 不接受默认值，必须在环境变量中设置
WALLET = "0x8A966Db06aD9624a0E41bb4adfD817c5e7e55F54"
NATIVE_USDC = "0xffffFFFfFFffffffffffffffFfFFFfffFFFfFFfE"
DEX = "0x114d574247342eafea95b7a9234e62984d48cD9E"
USDC = "0x3600000000000000000000000000000000000000"
CHAIN_ID = 5042
SWAP_TOPIC = "0x89a885b6900024aaed2c0845aad74f2204445bf00ac135917c70f57540e557b3"
DEX_SELECTOR = "0xd984396a"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df031b6ef"

# 策略参数
MIN_AGE_MINUTES = 30
MIN_PUMP_PCT = 30.0
PUMP_WINDOW_BLOCKS = 150
SCAN_BLOCKS = 8000
BLOCK_TIME_SEC = 2
BUY_AMOUNT = 20 * 10**18
MIN_SCORE_TO_BUY = 50
MAX_POSITIONS = 5
MIN_SELL_COUNT = 2          # 至少 2 笔卖出，否则跳过（防阴谋盘/单机盘）
MAX_BUY_RATIO = 0.95         # 买入占比 >95% 且卖出 <2 笔 → 跳过

# 预拉盘信号参数 (v2 — 基于回测优化)
PRE_PUMP_BUY_RATIO = 0.50       # Signal 1: 买入占比 >=50% (原 70%，中位数仅 50%)
PRE_PUMP_LIQ_INJECT = 0.10      # Signal 2: 流动性增加 >=10% (原 50%，中位数仅 +9%)
PRE_PUMP_BOUNCE_PCT = 5.0       # Signal 3: 从低点反弹 >=5%
PRE_PUMP_VOL_ACCEL = 1.3        # Signal 4: 频率 >=1.3x (原 2x，中位数 0.4x)
PRE_PUMP_NEW_TRADERS = 3        # Signal 5: 3+ unique traders (原 5，P25=8 但降低门槛提覆盖)
PRE_PUMP_PRICE_RANGE = 15.0     # Signal 6 (NEW): 价格波动率 >=15%
PRE_PUMP_MIN_SWAPS = 3          # Signal 7 (NEW): 最近 30min 至少 3 笔交易
MIN_PRE_PUMP_SIGNALS = 2        # ≥2 信号即触发 (原 3，检出率 20%→60%)

# Files
KNOWN_FILE = "/tmp/arc_known_tokens.json"
SCORES_FILE = "/tmp/arc_momentum_scores.json"
POSITIONS_FILE = "/tmp/arc_positions.json"
LOG = "/tmp/arc_momentum.log"
BUY_LOG = "/tmp/arc_buy.log"

# ════════════════════════════════════════════════════════════
# LOGGING
# ════════════════════════════════════════════════════════════
def log(msg, level="INFO"):
    ts = time.strftime("%H:%M:%S", time.gmtime())
    line = f"[{ts}] [{level}] {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")

def buy_log(msg):
    ts = time.strftime("%H:%M:%S", time.gmtime())
    line = f"[{ts}] {msg}"
    with open(BUY_LOG, "a") as f:
        f.write(line + "\n")
    print(line, flush=True)

# ════════════════════════════════════════════════════════════
# RPC
# ════════════════════════════════════════════════════════════
def curl_rpc(method, params=None, retries=3):
    if params is None: params = []
    p = json.dumps({"jsonrpc":"2.0","method":method,"params":params,"id":1})
    for attempt in range(retries):
        for rpc in [RPC, RPC_FALLBACK]:
            r = subprocess.run(["curl","-s","-m","15","-X","POST",rpc,
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

def get_block():
    resp = curl_rpc("eth_blockNumber")
    return int(resp.get("result", "0x0"), 16) if "result" in resp else None

def get_balance():
    resp = curl_rpc("eth_getBalance", [WALLET, "latest"])
    if "error" in resp or not resp.get("result"):
        return -1
    return int(resp.get("result", "0x0"), 16)

def get_nonce():
    resp = curl_rpc("eth_getTransactionCount", [WALLET, "latest"])
    return int(resp.get("result", "0x0"), 16)

def get_gas_price():
    resp = curl_rpc("eth_gasPrice")
    return int(resp.get("result", "0x0"), 16) or 10**9

# ════════════════════════════════════════════════════════════
# TOKEN QUERIES
# ════════════════════════════════════════════════════════════
_SYMBOL_CACHE = {}
_NAME_CACHE = {}
_CODE_CACHE = {}

def token_string(addr, selector="0x95d89b41"):
    cache = _NAME_CACHE if selector == "0x06fdde03" else _SYMBOL_CACHE
    key = addr.lower()
    if key in cache:
        return cache[key]
    resp = curl_rpc("eth_call", [{"to": addr, "data": selector}, "latest"])
    r = resp.get("result", "0x")
    if not r or r == "0x" or len(r) < 130:
        cache[key] = "?"
        return "?"
    try:
        h = r[2:]
        length = int(h[64:128], 16) * 2
        sym = bytes.fromhex(h[128:128+length]).decode('utf-8', errors='replace')
        cache[key] = sym
        return sym
    except:
        cache[key] = "?"
        return "?"

def code_size(addr):
    key = addr.lower()
    if key in _CODE_CACHE:
        return _CODE_CACHE[key]
    resp = curl_rpc("eth_getCode", [addr, "latest"])
    r = resp.get("result", "0x")
    sz = (len(r) - 2) // 2
    _CODE_CACHE[key] = sz
    return sz

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
        return int(rcpt.get("status", "0x0"), 16) == 1
    return False

# ════════════════════════════════════════════════════════════
# DEX EVENT PARSING
# ════════════════════════════════════════════════════════════
def get_dex_swaps(from_block, to_block):
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
        
        block_num = int(l["blockNumber"], 16)
        data = l.get("data", "0x")[2:]
        words = [data[i:i+64] for i in range(0, len(data), 64)]
        
        token_in = "0x" + topics[1][-40:]
        token_out = "0x" + topics[2][-40:]
        trader = "0x" + topics[3][-40:] if len(topics) >= 4 else "0x?"
        
        if token_in.lower() != USDC.lower():
            token_addr = token_in.lower()
            direction = "SELL"
        elif token_out.lower() != USDC.lower():
            token_addr = token_out.lower()
            direction = "BUY"
        else:
            continue
        
        res_tok = int(words[3], 16) if len(words) > 3 else 0
        res_usdc = int(words[4], 16) if len(words) > 4 else 0
        price = res_usdc / res_tok if res_tok > 0 else 0
        amount = int(words[1], 16) if len(words) > 1 else 0
        
        if token_addr not in tokens:
            tokens[token_addr] = {
                "swaps": [],
                "first_block": block_num,
                "last_block": block_num,
                "swap_count": 0,
                "unique_traders": set(),
                "reserve_token": 0,
                "reserve_usdc": 0,
                "directions": collections.Counter()
            }
        
        t = tokens[token_addr]
        t["swaps"].append({
            "block": block_num, "price": price,
            "direction": direction, "trader": trader.lower(),
            "amount": amount, "res_tok": res_tok, "res_usdc": res_usdc
        })
        t["swap_count"] += 1
        t["last_block"] = max(t["last_block"], block_num)
        t["first_block"] = min(t["first_block"], block_num)
        t["unique_traders"].add(trader.lower())
        t["directions"][direction] += 1
        if res_tok > 0: t["reserve_token"] = res_tok
        if res_usdc > 0: t["reserve_usdc"] = res_usdc
    
    return tokens

# ════════════════════════════════════════════════════════════
# PUMP DETECTION (已确认突破)
# ════════════════════════════════════════════════════════════
def detect_pump(swaps, window_blocks=PUMP_WINDOW_BLOCKS):
    if len(swaps) < 2:
        return 0, 0, 0, 0, 0, False
    sorted_swaps = sorted(swaps, key=lambda x: x["block"])
    max_pump = 0
    best_start = sorted_swaps[0]["block"]
    best_end = sorted_swaps[-1]["block"]
    best_p1 = sorted_swaps[0]["price"]
    best_p2 = sorted_swaps[-1]["price"]
    
    for i in range(len(sorted_swaps)):
        for j in range(i + 1, len(sorted_swaps)):
            block_diff = sorted_swaps[j]["block"] - sorted_swaps[i]["block"]
            if block_diff > window_blocks:
                break
            p1 = sorted_swaps[i]["price"]
            p2 = sorted_swaps[j]["price"]
            if p1 <= 0 or p2 <= 0:
                continue
            pump_pct = ((p2 - p1) / p1) * 100
            if pump_pct > max_pump:
                max_pump = pump_pct
                best_start = sorted_swaps[i]["block"]
                best_end = sorted_swaps[j]["block"]
                best_p1 = p1
                best_p2 = p2
    
    buy_count = sum(1 for s in sorted_swaps if s["direction"] == "BUY")
    sell_count = sum(1 for s in sorted_swaps if s["direction"] == "SELL")
    is_buy_pressure = buy_count > sell_count * 1.5
    return max_pump, best_start, best_end, best_p1, best_p2, is_buy_pressure

# ════════════════════════════════════════════════════════════
# PRE-PUMP SIGNAL DETECTION (拉盘前信号)
# ════════════════════════════════════════════════════════════
def detect_pre_pump_signals(swaps, current_block):
    """
    检测拉盘前信号 (v2 — 基于回测优化)。
    8 个信号，阈值根据 6 个真实拉盘 token 的 P25 值设定。
    返回 (signal_count, signals[])
    """
    if len(swaps) < 3:
        return 0, []
    
    sorted_swaps = sorted(swaps, key=lambda x: x["block"])
    signals = []
    
    # ─── Signal 1: 买入占比 (阈值 50%, 原 70%) ──────────────
    # 回测: 中位数 50%, 很多金狗先砸后拉
    recent_10 = sorted_swaps[-10:] if len(sorted_swaps) >= 10 else sorted_swaps
    buy_count = sum(1 for s in recent_10 if s["direction"] == "BUY")
    sell_count = sum(1 for s in recent_10 if s["direction"] == "SELL")
    total = buy_count + sell_count
    if total > 0:
        buy_ratio = buy_count / total
        if buy_ratio >= PRE_PUMP_BUY_RATIO:
            signals.append(f"买入占比: {buy_count}/{total} ({buy_ratio*100:.0f}% BUY)")
    
    # ─── Signal 2: 流动性增加 (阈值 +10%, 原 +50%) ──────────
    # 回测: 中位数仅 +9%, 极不稳定
    recent_20_blocks = [s for s in sorted_swaps if s["block"] >= current_block - 20]
    if len(recent_20_blocks) >= 2:
        usdc_values = [s["res_usdc"] for s in recent_20_blocks if s["res_usdc"] > 0]
        if len(usdc_values) >= 2:
            early_usdc = usdc_values[0]
            late_usdc = usdc_values[-1]
            if early_usdc > 0:
                liq_change = (late_usdc - early_usdc) / early_usdc
                if liq_change >= PRE_PUMP_LIQ_INJECT:
                    signals.append(f"流动性增加: +{liq_change*100:.0f}% ({early_usdc/1e18:.0f}→{late_usdc/1e18:.0f}U)")
    
    # ─── Signal 3: 价格底部反弹 (阈值 5%) ────────────────────
    prices = [s["price"] for s in sorted_swaps if s["price"] > 0]
    if len(prices) >= 3:
        recent_prices = prices[-20:]
        min_price = min(recent_prices)
        current_price = recent_prices[-1]
        if min_price > 0:
            bounce = ((current_price - min_price) / min_price) * 100
            if bounce >= PRE_PUMP_BOUNCE_PCT:
                signals.append(f"底部反弹: +{bounce:.1f}% (low={min_price:.4f}→now={current_price:.4f})")
    
    # ─── Signal 4: 交易频率变化 (阈值 1.3x, 原 2x) ────────────
    # 回测: 中位数 0.4x, 频率下降也能拉盘
    recent_15min = [s for s in sorted_swaps if s["block"] >= current_block - 450]
    prev_30min = [s for s in sorted_swaps if current_block - 900 <= s["block"] < current_block - 450]
    
    if len(prev_30min) > 0:
        recent_rate = len(recent_15min) / 15
        prev_rate = len(prev_30min) / 30
        if prev_rate > 0 and recent_rate >= prev_rate * PRE_PUMP_VOL_ACCEL:
            signals.append(f"频率加快: {recent_rate:.1f}/min vs {prev_rate:.1f}/min ({recent_rate/prev_rate:.1f}x)")
    elif len(recent_15min) >= 5:
        signals.append(f"频率加快: {len(recent_15min)} 笔/15min (之前无交易)")
    
    # ─── Signal 5: 交易者多样性 (阈值 3+, 原 5+) ──────────────
    # 回测: P25=8, 但降低门槛提高检出率
    recent_10_swaps = sorted_swaps[-10:] if len(sorted_swaps) >= 10 else sorted_swaps
    unique_traders = set(s["trader"] for s in recent_10_swaps)
    if len(unique_traders) >= PRE_PUMP_NEW_TRADERS:
        signals.append(f"交易者多样: {len(unique_traders)} 个独立地址")
    
    # ─── Signal 6 (NEW): 价格波动率 (阈值 15%) ───────────────
    # 回测: 拉盘前价格范围中位数 16.3%, 是重要前兆信号
    if len(prices) >= 3:
        recent_prices_20 = [s["price"] for s in sorted_swaps[-20:] if s["price"] > 0]
        if len(recent_prices_20) >= 2 and min(recent_prices_20) > 0:
            price_range_pct = ((max(recent_prices_20) - min(recent_prices_20)) / min(recent_prices_20)) * 100
            if price_range_pct >= PRE_PUMP_PRICE_RANGE:
                signals.append(f"价格波动: {price_range_pct:.1f}% (min={min(recent_prices_20):.4f} max={max(recent_prices_20):.4f})")
    
    # ─── Signal 7 (NEW): 活跃度门槛 (>=3 笔/30min) ───────────
    # 过滤长时间无交易的死币
    recent_30min = [s for s in sorted_swaps if s["block"] >= current_block - 900]
    if len(recent_30min) >= PRE_PUMP_MIN_SWAPS:
        signals.append(f"活跃交易: 30min内 {len(recent_30min)} 笔")
    
    # ─── Signal 8: 池子创建 ────────────────────────────
    if len(sorted_swaps) >= 2:
        early_res = sorted_swaps[0]["res_usdc"]
        late_res = sorted_swaps[-1]["res_usdc"]
        if early_res == 0 and late_res > 0:
            signals.append(f"池子创建: 流动性从 0 -> {late_res/1e18:.0f}U")
    
    return len(signals), signals

# ════════════════════════════════════════════════════════════
# SCORING
# ════════════════════════════════════════════════════════════
def score_token(token_data, pump_pct, is_buy_pressure, code_sz, pre_pump_count, pre_pump_signals):
    score = 0
    reasons = list(pre_pump_signals)  # 优先展示预拉盘信号
    
    # 预拉盘信号分数 (0-40, 每个信号 5 分, 8 信号满分 40)
    score += pre_pump_count * 5
    if pre_pump_count >= MIN_PRE_PUMP_SIGNALS:
        reasons.append(f"🔥 {pre_pump_count}/8 预拉盘信号触发")
    
    # 已确认拉盘 (0-15)
    if pump_pct >= MIN_PUMP_PCT:
        score += 15
        reasons.append(f"已确认拉盘 {pump_pct:.1f}%")
    elif pump_pct >= 15:
        score += 8
        reasons.append(f"温和拉盘 {pump_pct:.1f}%")
    
    # 买盘压力 (0-10)
    buy_count = token_data["directions"].get("BUY", 0)
    sell_count = token_data["directions"].get("SELL", 0)
    total = buy_count + sell_count
    if total > 0:
        buy_ratio = buy_count / total
        if is_buy_pressure:
            score += 10
        elif buy_ratio > 0.6:
            score += 7
        elif buy_ratio > 0.4:
            score += 3
    
    # 流动性 (0-10)
    liq = token_data["reserve_usdc"] / 1e18
    if liq > 10000: score += 10; reasons.append(f"流动性充足 {liq:.0f}U")
    elif liq > 1000: score += 7
    elif liq > 100: score += 3
    
    # 合约质量 (0-10)
    if code_sz > 5000: score += 10
    elif code_sz > 1000: score += 7
    elif code_sz > 100: score += 3
    
    # 交易者多样性 (0-10)
    traders = len(token_data["unique_traders"])
    if traders > 15: score += 10
    elif traders > 8: score += 7
    elif traders > 3: score += 3
    
    # 年龄 (0-5)
    age_min = (token_data["last_block"] - token_data["first_block"]) * BLOCK_TIME_SEC / 60
    if 30 < age_min < 180: score += 5
    elif age_min < 30: score += 2
    
    return min(score, 100), reasons

# ════════════════════════════════════════════════════════════
# AUTO-BUY
# ════════════════════════════════════════════════════════════
def find_dex_tx_for_token(token_addr):
    block = get_block()
    if not block: return None
    resp = curl_rpc("eth_getLogs", [{
        "fromBlock": hex(block - 500), "toBlock": hex(block), "address": DEX
    }])
    if resp.get("error") or not resp.get("result"): return None
    
    tx_hashes = set()
    for l in resp["result"]:
        topics = l.get("topics", [])
        if not topics or topics[0].lower() != SWAP_TOPIC.lower() or len(topics) < 3:
            continue
        for i in [1, 2]:
            addr = "0x" + topics[i][-40:]
            if addr.lower() == token_addr.lower():
                tx_hashes.add(l.get("transactionHash"))
                break
    
    for txh in list(tx_hashes)[:10]:
        tx_resp = curl_rpc("eth_getTransactionByHash", [txh])
        if tx_resp.get("error") or not tx_resp.get("result"): continue
        tx = tx_resp["result"]
        if tx.get("input", "")[:10] != DEX_SELECTOR: continue
        rcpt_resp = curl_rpc("eth_getTransactionReceipt", [txh])
        if rcpt_resp.get("result"):
            rcpt = rcpt_resp["result"]
            if int(rcpt.get("status", "0x0"), 16) == 1:
                return tx, rcpt
    return None

def auto_buy(token_addr, symbol, amount_usdc_wei=BUY_AMOUNT):
    buy_log(f"{'='*60}")
    buy_log(f"🚀 AUTO-BUY: {symbol} @ {token_addr[:12]}...")
    buy_log(f"   金额: {amount_usdc_wei/1e18:.2f} USDC")
    
    balance = get_balance()
    if balance < 0:
        buy_log(f"❌ RPC 查询余额失败")
        return False, "rpc_error"
    if balance < amount_usdc_wei:
        buy_log(f"❌ 余额不足: {balance/1e18:.2f} < {amount_usdc_wei/1e18:.2f}")
        return False, "insufficient_balance"
    buy_log(f"   余额: {balance/1e18:.2f} USDC")
    
    positions = load_positions()
    if len(positions) >= MAX_POSITIONS:
        buy_log(f"❌ 持仓数已达上限 {MAX_POSITIONS}")
        return False, "max_positions"
    if token_addr.lower() in positions:
        buy_log(f"⚠️ 已持有 {symbol}，跳过")
        return False, "already_holding"
    
    allowance = check_allowance(NATIVE_USDC, DEX)
    if allowance < amount_usdc_wei:
        buy_log(f"   Approving DEX...")
        if not approve(NATIVE_USDC, DEX, 10000 * 10**18):
            buy_log(f"❌ Approve 失败")
            return False, "approve_failed"
    else:
        buy_log(f"   Allowance OK: {allowance/1e18:.0f}U")
    
    buy_log(f"   搜索 {symbol} 的 DEX 交易...")
    result = find_dex_tx_for_token(token_addr)
    if not result:
        buy_log(f"❌ 未找到 {symbol} 的 DEX 交易")
        return False, "no_dex_tx"
    
    tx, rcpt = result
    original_sender = tx["from"]
    original_data = tx["input"]
    buy_log(f"   找到 tx: from={original_sender[:12]}...")
    
    # Modify calldata
    selector = original_data[:10]
    params = original_data[10:]
    words = [params[i:i+64] for i in range(0, len(params), 64)]
    sender_replaced = False
    for i, w in enumerate(words):
        addr = "0x" + w[24:]
        if addr.lower() == original_sender.lower():
            words[i] = WALLET[2:].lower().zfill(64)
            sender_replaced = True
    if len(words) > 7:
        words[7] = "0" * 64
    new_data = selector + "".join(words)
    
    # Simulate
    buy_log(f"   模拟交易...")
    sim_resp = curl_rpc("eth_call", [
        {"from": WALLET, "to": DEX, "data": new_data, "value": "0x0"}, "latest"
    ])
    if sim_resp.get("error") or not sim_resp.get("result"):
        buy_log(f"❌ 模拟失败")
        return False, "sim_failed"
    buy_log(f"   模拟成功 ✅")
    
    # Send buy tx!
    buy_log(f"   🚀 发送买入 TX!!!")
    buy_rcpt = send_tx(DEX, new_data, gas=1200000)
    if not buy_rcpt:
        buy_log(f"❌ TX 发送失败")
        return False, "tx_failed"
    
    status = int(buy_rcpt.get("status", "0x0"), 16)
    gas_used = int(buy_rcpt.get("gasUsed", "0x0"), 16)
    tx_hash = buy_rcpt.get("transactionHash", "")
    
    if status != 1:
        buy_log(f"❌ TX 失败, gas={gas_used}")
        return False, "tx_reverted"
    
    # Parse received tokens
    received_amount = 0
    for log_entry in buy_rcpt.get("logs", []):
        topics = log_entry.get("topics", [])
        if not topics or topics[0] != TRANSFER_TOPIC or len(topics) < 3:
            continue
        to_a = "0x" + topics[2][24:64]
        if to_a.lower() == WALLET[2:].lower():
            token = log_entry.get("address", "")
            amount = int(log_entry.get("data", "0x"), 16)
            if token.lower() == token_addr.lower():
                received_amount = amount
                buy_log(f"   📦 收到: {amount/1e18:.6f} {symbol}")
    
    cost_usdc = amount_usdc_wei / 1e18
    save_position(token_addr, symbol, received_amount, cost_usdc, tx_hash)
    buy_log(f"   ✅ 买入成功! TX: {tx_hash[:20]}...")
    buy_log(f"   💰 花费: {cost_usdc:.2f} USDC, Gas: {gas_used}")
    
    return True, {
        "symbol": symbol, "amount": received_amount, "cost_usdc": cost_usdc,
        "tx_hash": tx_hash, "gas_used": gas_used
    }

# ════════════════════════════════════════════════════════════
# POSITION MANAGEMENT
# ════════════════════════════════════════════════════════════
def load_positions():
    if os.path.exists(POSITIONS_FILE):
        with open(POSITIONS_FILE) as f:
            return json.load(f)
    return {}

def save_position(token_addr, symbol, amount, cost_usdc, tx_hash):
    positions = load_positions()
    positions[token_addr.lower()] = {
        "symbol": symbol, "amount": amount, "cost_usdc": cost_usdc,
        "buy_tx": tx_hash, "buy_time": time.time(), "buy_block": get_block() or 0
    }
    with open(POSITIONS_FILE, "w") as f:
        json.dump(positions, f, indent=2)

def load_known():
    if os.path.exists(KNOWN_FILE):
        with open(KNOWN_FILE) as f:
            return json.load(f)
    return {}

def save_known(known):
    with open(KNOWN_FILE, "w") as f:
        json.dump(known, f, indent=2)

# ════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════
def main():
    log("=" * 70)
    log("🚀 Arc Pre-Pump Scanner + Auto-Buy")
    log(f"   策略: 8信号预拉盘检测 (≥{MIN_PRE_PUMP_SIGNALS}信号触发) + 5min>30%确认突破")
    log(f"   买入: score≥{MIN_SCORE_TO_BUY} → 自动买 {BUY_AMOUNT/1e18:.0f}U | 卖出: 用户自管")
    log("=" * 70)
    
    block = get_block()
    if not block:
        print("ERROR:RPC_DOWN")
        return 1
    
    log(f"当前 block: {block}")
    
    # Scan DEX
    scan_from = max(1, block - SCAN_BLOCKS)
    log(f"扫描范围: {scan_from}-{block} ({block - scan_from} blocks, ~{(block - scan_from)*BLOCK_TIME_SEC/60:.0f} min)")
    
    all_tokens = {}
    chunk_size = 500
    for start in range(scan_from, block, chunk_size):
        end = min(start + chunk_size, block)
        chunk_tokens = get_dex_swaps(start, end)
        for addr, data in chunk_tokens.items():
            if addr not in all_tokens:
                all_tokens[addr] = {
                    "swaps": [], "first_block": data["first_block"],
                    "last_block": data["last_block"], "swap_count": 0,
                    "unique_traders": set(), "reserve_token": 0,
                    "reserve_usdc": 0, "directions": collections.Counter()
                }
            t = all_tokens[addr]
            t["swaps"].extend(data["swaps"])
            t["swap_count"] += data["swap_count"]
            t["first_block"] = min(t["first_block"], data["first_block"])
            t["last_block"] = max(t["last_block"], data["last_block"])
            t["unique_traders"].update(data["unique_traders"])
            t["directions"].update(data["directions"])
            if data["reserve_token"] > 0: t["reserve_token"] = data["reserve_token"]
            if data["reserve_usdc"] > 0: t["reserve_usdc"] = data["reserve_usdc"]
    
    log(f"发现 {len(all_tokens)} 个 token")
    if not all_tokens:
        print("HEARTBEAT_OK:no_tokens")
        return 0
    
    # Analyze each token
    results = []
    for addr, data in all_tokens.items():
        age_min = (block - data["first_block"]) * BLOCK_TIME_SEC / 60
        if age_min < 5:  # 太新，跳过
            continue
        
        # 1. 已确认拉盘检测
        pump_pct, ps, pe, p1, p2, is_buy = detect_pump(data["swaps"], PUMP_WINDOW_BLOCKS)
        pump_age_min = (block - pe) * BLOCK_TIME_SEC / 60  # 拉盘发生到现在的时间
        
        # 2. 预拉盘信号检测
        signal_count, signals = detect_pre_pump_signals(data["swaps"], block)
        
        # 3. 打分
        sym = token_string(addr)
        cs = code_size(addr)
        score, reasons = score_token(data, pump_pct, is_buy, cs, signal_count, signals)
        
        is_confirmed_pump = pump_pct >= MIN_PUMP_PCT
        is_pre_pump = signal_count >= MIN_PRE_PUMP_SIGNALS
        is_near_pump = pump_pct >= MIN_PUMP_PCT * 0.3 or signal_count >= 2
        
        if is_confirmed_pump or is_pre_pump or is_near_pump:
            results.append({
                "address": addr, "symbol": sym, "score": score,
                "pump_pct": pump_pct, "signal_count": signal_count,
                "signals": signals, "reasons": reasons,
                "age_minutes": age_min,
                "swap_count": data["swap_count"],
                "liquidity_usdc": data["reserve_usdc"] / 1e18,
                "code_size": cs, "traders": len(data["unique_traders"]),
                "buy_count": data["directions"].get("BUY", 0),
                "sell_count": data["directions"].get("SELL", 0),
                "is_confirmed_pump": is_confirmed_pump,
                "is_pre_pump": is_pre_pump,
                "pump_age_min": pump_age_min,
            })
    
    # Sort by score
    results.sort(key=lambda x: -x["score"])
    
    if not results:
        log("无信号 token，静默")
        # Update known_tokens
        known = load_known()
        for addr, data in all_tokens.items():
            if addr not in known:
                known[addr] = {"symbol": token_string(addr), "first_seen": block, "first_seen_block": data["first_block"]}
        save_known(known)
        print("HEARTBEAT_OK:no_signal")
        return 0
    
    # Print results
    print(f"\n{'='*120}")
    print(f"{'#':>2} {'Score':>4} {'Symbol':<10} {'Pump%':>6} {'Sig':>3} {'Age':>5} {'PAge':>5} {'Swaps':>5} {'Liq$':>8} {'B/S':>5} {'Status':<10} {'Top Signal'}")
    print(f"{'='*130}")
    
    for i, r in enumerate(results[:20]):
        if r["is_confirmed_pump"]:
            status = "🔥 突破"
        elif r["is_pre_pump"]:
            status = "⚡ 预警"
        else:
            status = "📋 观察"
        
        page_str = f"{r.get('pump_age_min',0):.0f}m" if r.get('pump_age_min',0) < 1000 else "--"
        top_sig = r["signals"][0] if r["signals"] else (r["reasons"][0] if r["reasons"] else "")
        print(f"{i+1:>2} {r['score']:>4} {r['symbol']:<10} {r['pump_pct']:>5.1f}% {r['signal_count']:>3} {r['age_minutes']:>4.0f}m {page_str:>5} {r['swap_count']:>5} {r['liquidity_usdc']:>8.0f} {r['buy_count']:>2}/{r['sell_count']:<2} {status:<10} {top_sig[:50]}")
        print(f"{'='*120}")
        
        for sig in r["signals"][1:3]:
            print(f"{'':>60} {sig[:50]}")
    
    # ════════════════════════════════════════════════════════════
    # AUTO-BUY LOGIC
    # ════════════════════════════════════════════════════════════
    bought = []
    MAX_CHASE_MINUTES = 15  # 拉盘超过 15 分钟不追
    
    for r in results:
        should_buy = False
        buy_reason = ""
        
        # ─── 防阴谋盘/单机盘过滤 ──────────────────────────
        # 买入占比 >95% 且卖出 <2 笔 → 大概率自嗨盘，跳过
        bc = r.get("buy_count", 0)
        sc = r.get("sell_count", 0)
        total_swaps = bc + sc
        if total_swaps > 0 and sc < MIN_SELL_COUNT and bc / total_swaps > MAX_BUY_RATIO:
            log(f"⛔ {r['symbol']} 跳过: {bc}/{sc} 买卖比过高 ({bc/total_swaps*100:.0f}%), 疑似单机盘")
            continue
        
        if r["is_confirmed_pump"] and r["score"] >= MIN_SCORE_TO_BUY:
            # 保护: 拉盘已超过 15 分钟，不追顶
            if r.get("pump_age_min", 0) > MAX_CHASE_MINUTES:
                log(f"⛔ {r['symbol']} 拉盘 {r['pump_pct']:.1f}% 但已过去 {r['pump_age_min']:.0f}min, 不追顶")
                continue
            should_buy = True
            buy_reason = f"🔥 确认突破: {r['symbol']} pump={r['pump_pct']:.1f}% score={r['score']} age={r['pump_age_min']:.0f}min"
        elif r["is_pre_pump"] and r["score"] >= MIN_SCORE_TO_BUY:
            should_buy = True
            buy_reason = f"⚡ 预拉盘信号: {r['symbol']} {r['signal_count']}/8 signals score={r['score']}"
        
        if should_buy:
            log(f"{buy_reason} → 自动买入!")
            print(f"NOTIFY:AUTO_BUY:{r['symbol']}:{r['score']}:{r['pump_pct']:.1f}%:{r['signal_count']}sig:{r['address']}")
            
            success, info = auto_buy(r["address"], r["symbol"], BUY_AMOUNT)
            if success:
                bought.append(r["symbol"])
                print(f"NOTIFY:BUY_SUCCESS:{r['symbol']}:{info['amount']/1e18:.6f}:{info['cost_usdc']:.2f}:{info['tx_hash'][:20]}")
                log(f"✅ 买入成功: {r['symbol']}", "BUY")
            else:
                print(f"NOTIFY:BUY_FAILED:{r['symbol']}:{info}")
                log(f"❌ 买入失败: {r['symbol']} ({info})", "ERROR")
    
    # Watchlist notifications
    for r in results:
        if not r["is_confirmed_pump"] and not r["is_pre_pump"] and r["score"] >= 35:
            print(f"NOTIFY:WATCHLIST:{r['symbol']}:{r['score']}:{r['pump_pct']:.1f}%:{r['signal_count']}sig:{r['address']}")
    
    # Save results
    with open(SCORES_FILE, "w") as f:
        json.dump(results, f, indent=2, default=str)
    
    # Update known_tokens
    known = load_known()
    for addr, data in all_tokens.items():
        if addr not in known:
            known[addr] = {"symbol": token_string(addr), "first_seen": block, "first_seen_block": data["first_block"]}
    save_known(known)
    
    # Print positions
    positions = load_positions()
    if positions:
        print(f"\n当前持仓: {len(positions)}")
        for addr, pos in positions.items():
            print(f"  {pos['symbol']}: {pos['amount']/1e18:.6f} (cost {pos['cost_usdc']:.2f}U)")
    
    print(f"\n已知token: {len(known)} | 候选: {len(results)} | 买入: {len(bought)}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
