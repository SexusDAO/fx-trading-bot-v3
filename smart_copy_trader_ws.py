#!/usr/bin/env python3
"""
BSC Smart Money 跟单系统 v5.0
新增优化：
  1. 多维度反追高：5m涨幅>20% / 1h>50% / 1h<-20%（接刀保护）
  2. 聪明钱卖出检测：触发地址2小时内卖出同一代币 → 推送警报
  3. 双轨巡检：正常45秒 / 持仓跌幅>15%切换10秒高频模式
  4. 开盘狙击保护：流动性池创建<10分钟拒绝
  5. 熔断通知+次日UTC 00:00自动重置（无需人工确认）
  6. 每日收盘报告（23:59 UTC）+ 资金曲线记录
  + 手续费补偿：MIN_LIQUIDITY→$100k / 税率合计>8%拒绝 / SL调整到×0.72
"""
import json, subprocess, time, threading, sys
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import defaultdict

try:
    import websocket
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "websocket-client", "-q"])
    import websocket

# ─── 配置 ─────────────────────────────────────────────────────────
WORKSPACE        = Path("/root/.openclaw/workspace")
STATE_FILE       = WORKSPACE / "cw_state.json"
LOG_FILE         = WORKSPACE / "cw_log.json"
CURVE_FILE       = WORKSPACE / "cw_equity_curve.json"   # 资金曲线

BUY_AMOUNT_USD   = 25
MAX_POSITIONS    = 5
MAX_DAILY_LOSSES = 3
TOKEN_COOLDOWN   = 4 * 3600    # 同一代币冷却4小时

# 止损/止盈
SL_MULT          = 0.72        # ×0.72（补偿约2%手续费）
TP1_MULT         = 2.00        # ×2.0 出本：实际卖出50%保本，剩余转移动止损
TP1_SELL_RATIO   = 0.50        # 出本时卖出比例 50%
TRAIL_PCT        = 0.20        # 移动止损回撤20%
TP2_HARD_MULT    = 6.00        # 硬止盈×6.0（剩余50%硬上限）

# 聪明钱出场跟随（最高优先级）
SMART_EXIT_RATIO = 0.6         # 触发地址卖出比例≥60%认定为「清仓离场」
SMART_EXIT_WINDOW = 4 * 3600   # 买入后4小时内跟踪聪明钱出场（扩大窗口）
SMART_EXIT_MIN_HOLDERS = 1     # 至少1个触发地址离场才触发判断
SMART_EXIT_FOLLOW_THRESHOLD = -0.05  # 当前持仓跌幅>-5% 才跟随出场（避免止损时叠加）

# 巡检
PRICE_CHECK_NORMAL  = 45       # 正常巡检间隔（秒）
PRICE_CHECK_FAST    = 10       # 高频巡检间隔（秒）
FAST_MODE_TRIGGER   = -0.15    # 持仓跌幅超-15%触发高频模式

# 分层触发
TIER_TOP_MIN = 1   # 评分≥90，单独触发
TIER_MID_MIN = 2   # 评分70-89，≥2个
TIER_LOW_MIN = 3   # 评分<70，≥3个
MIN_TX_USD   = 100

# 过滤参数（含手续费优化）
MAX_TOKEN_AGE       = 30
MIN_LIQUIDITY       = 100_000   # 提高到$100k（降低滑点）
MIN_POOL_AGE_MIN    = 10        # 流动性池创建<10分钟拒绝（开盘狙击保护）
MAX_PUMP_5M         = 0.20      # 5分钟涨幅>20%拒绝
MAX_PUMP_1H         = 0.50      # 1小时涨幅>50%拒绝
MIN_DROP_1H         = -0.20     # 1小时跌幅>-20%拒绝（接刀保护）
MAX_TAX_TOTAL       = 0.08      # 买税+卖税合计>8%拒绝

# 耶明钱卖出检测
# BUG-D4 修复：统一用 SMART_EXIT_WINDOW，删除 SELL_WATCH_WINDOW 死变量
# SELL_WATCH_WINDOW = 2 * 3600  ← 已废弃，统一用下方的 SMART_EXIT_WINDOW
POLL_INTERVAL       = 300       # 轮询兜底间隔（秒），5分钟
POLL_TIERS          = {"TOP"}    # 只轮询 TOP 地址（减少API压力）

BSC_CHAIN_ID  = "56"
BSC_WS_URL    = "wss://bsc-rpc.publicnode.com"
BSC_RPC_URL   = "https://bsc-dataseed.binance.org/"
USDT_BSC      = "0x55d398326f99059ff775485246999027b3197955"
JARVIS_TARGET = "432401404:thread:019f9203-3b29-7d9e-bfb4-b67c0b06587c"
TRANSFER_SIG  = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# ─── 50个地址库（2026-07-28 更新，优先使用24h内活跃地址）────────────
_RAW_ADDRS = [
    # ★ 今日活跃新增（7天排行 + 24h内活跃）
    {"addr": "0xbf004bff64725914ee36d03b87d6965b0ced4903", "label": "阿峰_Afeng",       "score": 115},  # 1.5h前 胜率78%
    {"addr": "0xd00cfb791b0aaaefb2cf61c97b43a6c49daf2f79", "label": "新地址7d#1",      "score": 112},  # 刚活跃 7d收益248%
    {"addr": "0x0bbc493ac927e05cd9de8f716b1237272c42bce5", "label": "新地址7d#2",      "score": 110},  # 1h前   7d收益91%
    {"addr": "0xfabbf3b95f2c0c2c247f520270c2b92d004988c2", "label": "新地址7d#3",      "score": 108},  # 刚活跃
    {"addr": "0x3391a39a1b508e54a361924a26056c01c1c2c07d", "label": "Reljoo",          "score": 105},  # 4h前活跃
    {"addr": "0x02545c226146d7b26949bd9168e4433af1e53bba", "label": "新地址7d#4",      "score": 102},  # 2.7h前 7d收益115%
    {"addr": "0x5b6211081130165ef3ce7841fe69f8714d0ecde7", "label": "新地址7d#5",      "score": 100},  # 1h前   7d收益91%
    {"addr": "0x4132d406fa587039503cc7b3bb9fe3645bcc72e6", "label": "新地址7d#6",      "score": 95},   # 1h前   7d收益296%
    # 原库保留（30天稳定高分）
    {"addr": "0x2ce9d43d1cba6ae31d7f07bfe0098dfa2d833373", "label": "枯坐p小将",      "score": 107},
    {"addr": "0x1bff8f0ade179dfa1beddb54fa5d7d6dc814191a", "label": "SmartMoney#1",   "score": 96},
    {"addr": "0xd7828f0aa42c863ef2dabfe01832111ed21034c8", "label": "SmartMoney#2",      "score": 89},
    {"addr": "0x3c672bb3f5e16cac12953fd67b9f8ef6bd98f3bb", "label": "SmartMoney#3",      "score": 86},
    {"addr": "0x6a900ee716a579a6ea984015697de9fbf98989fc", "label": "SmartMoney#4",      "score": 85},
    {"addr": "0xbac453b9b7f53b35ac906b641925b2f5f2567a89", "label": "Dani",              "score": 83},
    {"addr": "0xfeccd1d6e453e44cdf7466644bc6ae1d70c354e5", "label": "SmartMoney#5",      "score": 83},
    {"addr": "0x7a2363a401b2340c7941dd2eeff0196a5078d2e6", "label": "金狗挖掘机0xDavid",  "score": 82},
    {"addr": "0xe14f5204244593df7d07e615590411baf3816ec4", "label": "SmartMoney#6",      "score": 80},
    {"addr": "0xa8a03e1c4154451c6d5d5f20b15aec8e8a45a517", "label": "SmartMoney#7",      "score": 75},
    {"addr": "0x81d5d84039f973584755a4805282ef94e2123302", "label": "SmartMoney#8",      "score": 75},
    {"addr": "0xcfe30db196c9a99fc80c72ad44fb4a135a53491f", "label": "旭旭宝宝",           "score": 75},
    {"addr": "0xbd0a401d7a41b15f25c95224bc87d9a85852b0a1", "label": "SmartMoney#9",      "score": 73},
    {"addr": "0xeeefff8ce2710fa490e0fcb794235e873c252d2e", "label": "Shenron🐉",          "score": 72},
    {"addr": "0xa7d4ffc4eca3c71af150ce302560a9d04a1d2b9f", "label": "MIRRO BNB",         "score": 71},
    {"addr": "0x5dbce812228bd08bfd4f08a2b6a1dbb613046afe", "label": "SmartMoney#10",     "score": 71},
    {"addr": "0x9a2a1daca07556faca114b8e8485d4b1d9ab873a", "label": "SmartMoney#11",     "score": 71},
    {"addr": "0x170332b75c0859a39bf7288f6cbf0db94bb1f567", "label": "TOM🦞",             "score": 71},
    {"addr": "0x051a53d095d277e22e3d905b10d3bbe75f0ac508", "label": "bruce",             "score": 70},
    {"addr": "0x55976c6818e4794f3e2e7179eea2cc2202811e11", "label": "九条命",            "score": 70},
    {"addr": "0xb2cd29be62895e84a6555f47de27e0926d63285a", "label": "SmartMoney#12",     "score": 70},
    {"addr": "0x5b6211081130165ef3ce7841fe69f8714d0ecde7", "label": "SmartMoney#13",     "score": 69},
    {"addr": "0x968e3059c22476dd953b38affb2cff6b37622979", "label": "SmartMoney#14",     "score": 69},
    {"addr": "0xad440ea22e75d24cc80f595cbc5a617ae67da7ac", "label": "SmartMoney#15",     "score": 69},
    {"addr": "0xbe1961f690b07f315699537f2dd81ea5dd974255", "label": "SmartMoney#16",     "score": 69},
    {"addr": "0x5388668c4fa8da7f756ef8498ff24d7999999999", "label": "西瓜BNB",           "score": 68},
    {"addr": "0x94cf7ed19901eb7d487c779e6d1baa87157b2d01", "label": "888UP",             "score": 68},
    {"addr": "0xacd7793d92cd870e56db2372b36e41c59ae4fb40", "label": "SmartMoney#17",     "score": 67},
    {"addr": "0x0fa3a520273545effec692de886683609fd0cf01", "label": "SmartMoney#18",     "score": 67},
    {"addr": "0x4d9644d05fe2123b4eafa8d7fd31b0ea430726f3", "label": "降雨幾率",          "score": 66},
    {"addr": "0x29935ef1417e1c3b1fec68dd666ea9baac31d4ed", "label": "SmartMoney#19",     "score": 65},
    {"addr": "0xddac928a240bdace3994c2cc0783d4e29a002127", "label": "o大扫链版",         "score": 65},
    {"addr": "0x3391a39a1b508e54a361924a26056c01c1c2c07d", "label": "Reljoo",            "score": 63},
    {"addr": "0x70d94aa3ad899b5456e8b1199049f98e2ab36a55", "label": "SmartMoney#20",     "score": 63},
    {"addr": "0x76a280376c5332abbbae1786a73c70116906e757", "label": "Jerry",             "score": 63},
    {"addr": "0xd41feaa24dede516a501862cf1f376defb811772", "label": "milito",            "score": 61},
    {"addr": "0x77a3b5c87d7f4b03c84f7b33ec82bacdc4d512a0", "label": "SmartMoney#21",    "score": 60},
    {"addr": "0x5b6560900ab57368c5127536e9ac9dbcbd61e942", "label": "SmartMoney#22",    "score": 60},
    {"addr": "0x02545c226146d7b26949bd9168e4433af1e53bba", "label": "SmartMoney#23",    "score": 59},
    {"addr": "0xecd1f3e99caa4cdbf174ad12ce79d89a8ba54fca", "label": "SmartMoney#24",    "score": 59},
    {"addr": "0x3097ad08546ec879d67c84c898458993424e05c5", "label": "SmartMoney#25",    "score": 59},
    {"addr": "0xa83b73f5644cde337b61da79589f10ea15548811", "label": "AntPositions",     "score": 58},
    {"addr": "0xb2d1af0746c410e146272e804b1741f07f83b851", "label": "Zephyr",           "score": 58},
    {"addr": "0x8ade93ba431a2ce19fc62a9ce97626e69a4a333f", "label": "0xLuck",           "score": 58},
    {"addr": "0xa1426c6f65fe804e05ce3c07a42412d735ef78bc", "label": "打狗大师",         "score": 56},
    {"addr": "0xd171ebe9d3c1e55d918563107b0b7782948dd52c", "label": "YYYzzy",           "score": 54},
    {"addr": "0x82e159d63e585067e9fa3a4bba7d992fbc667751", "label": "SmartMoney#26",    "score": 54},
    {"addr": "0xaaee1f3bb3583e360ebc1e5d8230428527e7341a", "label": "SmartMoney#27",    "score": 53},
    {"addr": "0x9c701dec4120629207b519cfc4fca620b8b7de22", "label": "SmartMoney#28",    "score": 52},
    {"addr": "0x877af245c61289b24f0c619a4346cdbc68f1aaac", "label": "古月月",           "score": 52},
]
# BUG-D6 修复：重复地址保留 score 最高的版本，防止低分覆盖高分导致门槛降级
WATCH_ADDRS: dict = {}
for _a in _RAW_ADDRS:
    _k = _a["addr"].lower()
    if _k not in WATCH_ADDRS or _a["score"] > WATCH_ADDRS[_k]["score"]:
        WATCH_ADDRS[_k] = _a

def _tier(score):
    if score >= 90: return "TOP"
    if score >= 70: return "MID"
    return "LOW"

def _min_buyers(tier):
    return {"TOP": TIER_TOP_MIN, "MID": TIER_MID_MIN, "LOW": TIER_LOW_MIN}[tier]

# ─── 全局状态 ─────────────────────────────────────────────────────
_lock          = threading.Lock()
_pending       = defaultdict(lambda: {"buyers": [], "total_usd": 0.0, "best_tier": "LOW"})
_positions     = {}    # token_addr -> pos
_processed     = set()
_cooldown      = {}    # token_addr -> ts
_daily_losses  = []    # 今日止损时间戳列表
_fused         = False # 熔断状态
_sell_watch    = {}    # token_addr -> {buyers:[addr], buy_ts, symbol}
_fast_mode     = set() # 高频巡检的 token_addr 集合
_last_block    = 0
_req_id        = 1

# 资金曲线（每日快照）
_equity_curve  = []    # [{date, start_usdt, end_usdt, trades, wins, losses}]

def ts_str():
    return datetime.now(timezone.utc).strftime("%H:%M:%S")

def today_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def log(msg):
    print(f"[{ts_str()}] {msg}", flush=True)

def send_notify(msg: str):
    try:
        subprocess.run(
            ["/usr/local/bin/openclaw", "message", "send",
             "--target", JARVIS_TARGET, "--channel", "jarvis", "--message", msg],
            capture_output=True, text=True, timeout=30
        )
    except Exception as e:
        log(f"推送失败: {e}")

def save_state():
    # BUG-D1 修复：锁+原子写，防止并发写坏 JSON
    with _lock:
        data = {
            "positions":    _positions,
            "processed":    list(_processed),
            "cooldown":     _cooldown,
            "daily_losses": _daily_losses,
            "fused":        _fused,
            "sell_watch":   _sell_watch,
        }
    tmp = STATE_FILE.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    tmp.replace(STATE_FILE)

def load_state():
    global _positions, _processed, _cooldown, _daily_losses, _fused, _sell_watch
    if not STATE_FILE.exists():
        return
    d = json.loads(STATE_FILE.read_text())
    _positions    = d.get("positions", {})
    _processed    = set(d.get("processed", []))
    _cooldown     = d.get("cooldown", {})
    _daily_losses = d.get("daily_losses", [])
    _fused        = d.get("fused", False)
    _sell_watch   = d.get("sell_watch", {})

def append_log(entry):
    logs = []
    if LOG_FILE.exists():
        try: logs = json.loads(LOG_FILE.read_text())
        except: pass
    logs.append({"ts": int(time.time()*1000), **entry})
    LOG_FILE.write_text(json.dumps(logs[-1000:], ensure_ascii=False, indent=2))

def save_equity(entry):
    curve = []
    if CURVE_FILE.exists():
        try: curve = json.loads(CURVE_FILE.read_text())
        except: pass
    curve.append(entry)
    CURVE_FILE.write_text(json.dumps(curve, ensure_ascii=False, indent=2))

# ─── RPC ─────────────────────────────────────────────────────────

def rpc(method, params):
    payload = json.dumps({"jsonrpc":"2.0","method":method,"params":params,"id":1}).encode()
    req = urllib.request.Request(BSC_RPC_URL, data=payload,
                                  headers={"Content-Type":"application/json"})
    r = urllib.request.urlopen(req, timeout=10)
    return json.loads(r.read()).get("result")

# ─── Transfer 事件解析 ────────────────────────────────────────────

def get_block_transfers(block_hex):
    """读取区块所有 ERC20 Transfer，区分买入（to=监控地址）和卖出（from=监控地址）"""
    try:
        logs = rpc("eth_getLogs", [{"fromBlock": block_hex, "toBlock": block_hex,
                                     "topics": [TRANSFER_SIG]}])
        if not logs:
            return [], []
        buys, sells = [], []
        for entry in logs:
            topics = entry.get("topics", [])
            if len(topics) < 3:
                continue
            from_addr  = "0x" + topics[1][-40:].lower()
            to_addr    = "0x" + topics[2][-40:].lower()
            token_addr = entry.get("address", "").lower()
            if token_addr == USDT_BSC.lower():
                continue
            data = entry.get("data", "0x")
            try:    amount_raw = int(data, 16)
            except: amount_raw = 0
            tx_hash = entry.get("transactionHash", "")

            if to_addr in WATCH_ADDRS:
                buys.append({"buyer_addr": to_addr, "buyer_info": WATCH_ADDRS[to_addr],
                              "token_addr": token_addr, "amount_raw": amount_raw, "tx_hash": tx_hash})
            if from_addr in WATCH_ADDRS:
                sells.append({"seller_addr": from_addr, "seller_info": WATCH_ADDRS[from_addr],
                               "token_addr": token_addr, "tx_hash": tx_hash})
        return buys, sells
    except Exception as e:
        log(f"get_block_transfers: {e}")
        return [], []

# ─── DEX Screener ─────────────────────────────────────────────────

def dex_info(token_addr):
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{token_addr}"
        req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0","Accept":"application/json"})
        r   = urllib.request.urlopen(req, timeout=12)
        d   = json.loads(r.read())
        pairs = d.get("pairs") or []
        if not pairs:
            return {}
        pair     = max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd",0) or 0))
        liq      = float((pair.get("liquidity") or {}).get("usd",0) or 0)
        price    = float(pair.get("priceUsd") or 0)
        chg      = pair.get("priceChange") or {}
        price5m  = float(chg.get("m5",  0) or 0)   # 5分钟涨幅%
        price1h  = float(chg.get("h1",  0) or 0)   # 1小时涨幅%
        symbol   = (pair.get("baseToken") or {}).get("symbol","?")
        dex_url  = pair.get("url", f"https://dexscreener.com/bsc/{token_addr}")
        created  = pair.get("pairCreatedAt") or 0
        age_days = (time.time()*1000 - created) / 86_400_000 if created else 999
        age_min  = (time.time()*1000 - created) / 60_000     if created else 999
        txns1h   = (pair.get("txns") or {}).get("h1", {})
        buyers1h = int(txns1h.get("buys", 0) or 0)
        return {"symbol": symbol, "liq": liq, "price": price,
                "price5m": price5m, "price1h": price1h,
                "age_days": age_days, "age_min": age_min,
                "dex_url": dex_url, "buyers1h": buyers1h}
    except Exception as e:
        return {"error": str(e)}

# ─── GoPlus 审计 ──────────────────────────────────────────────────

def goplus_audit(token_addr):
    try:
        url = f"https://api.gopluslabs.io/api/v1/token_security/{BSC_CHAIN_ID}?contract_addresses={token_addr}"
        r   = urllib.request.urlopen(url, timeout=20)
        res = (json.loads(r.read()).get("result") or {}).get(token_addr.lower(), {})
        if not res:
            return {"pass": None, "reason": "无数据"}
        fails = []
        if res.get("is_honeypot")         == "1": fails.append("🍯蜜罐")
        if res.get("cannot_sell_all")      == "1": fails.append("🐷貔貅")
        if res.get("owner_change_balance") == "1": fails.append("⚠️铸币权")
        if res.get("is_blacklisted")       == "1": fails.append("🚫黑名单")
        if res.get("is_open_source")       == "0": fails.append("未开源")
        sell_tax = float(res.get("sell_tax") or 0)
        buy_tax  = float(res.get("buy_tax")  or 0)
        # 合计税率>8%拒绝
        if buy_tax + sell_tax > MAX_TAX_TOTAL:
            fails.append(f"税率合计{(buy_tax+sell_tax)*100:.0f}%>{MAX_TAX_TOTAL*100:.0f}%")
        holders = int(res.get("holder_count") or 0)
        if fails:
            return {"pass": False, "reason": " | ".join(fails), "holders": holders}
        return {"pass": True, "reason": "✅通过", "holders": holders,
                "buy_tax": buy_tax, "sell_tax": sell_tax}
    except Exception as e:
        return {"pass": None, "reason": f"审计异常({e})"}

# ─── 查询钱包 USDT 余额 ───────────────────────────────────────────

def get_wallet_usdt() -> float:
    try:
        r = subprocess.run(["baw","wallet","balance","--json"],
                           capture_output=True, text=True, timeout=15)
        d = json.loads(r.stdout)
        items = d if isinstance(d, list) else (d.get("data") or [])
        for item in items:
            addr = (item.get("contractAddress") or item.get("address") or "").lower()
            sym  = (item.get("symbol") or "").upper()
            if addr == USDT_BSC.lower() or sym == "USDT":
                return float(item.get("balance") or item.get("amount") or 0)
    except:
        pass
    return 0.0

# ─── 半仓卖出（保本用）────────────────────────────────────────────

def execute_sell_half(token_addr, symbol) -> dict:
    """卖出持仓的 TP1_SELL_RATIO（50%），保本用"""
    try:
        r = subprocess.run(["baw","wallet","balance","--json"],
                           capture_output=True, text=True, timeout=15)
        d = json.loads(r.stdout)
        items = d if isinstance(d, list) else (d.get("data") or [])
        token_bal = 0.0
        for item in items:
            addr = (item.get("contractAddress") or item.get("address") or "").lower()
            if addr == token_addr.lower():
                token_bal = float(item.get("balance") or item.get("amount") or 0)
                break
        if token_bal <= 0:
            return {"success": False, "error": "余额0"}
        sell_qty = token_bal * TP1_SELL_RATIO
        r2 = subprocess.run([
            "baw","market-order","swap",
            "--binanceChainId", BSC_CHAIN_ID,
            "--fromTokenQty",   str(sell_qty),
            "--fromToken",      token_addr,
            "--toToken",        USDT_BSC,
        ], capture_output=True, text=True, timeout=60)
        if r2.returncode == 0 and "Order ID" in r2.stdout:
            oid = r2.stdout.split("Order ID:")[-1].strip().split()[0]
            return {"success": True, "order_id": oid, "qty": sell_qty, "remaining": token_bal - sell_qty}
        return {"success": False, "error": (r2.stdout+r2.stderr)[:200]}
    except Exception as e:
        return {"success": False, "error": str(e)}

# ─── 全仓卖出 ─────────────────────────────────────────────────────

def execute_sell(token_addr, symbol) -> dict:
    try:
        r = subprocess.run(["baw","wallet","balance","--json"],
                           capture_output=True, text=True, timeout=15)
        d = json.loads(r.stdout)
        items = d if isinstance(d, list) else (d.get("data") or [])
        token_bal = 0.0
        for item in items:
            addr = (item.get("contractAddress") or item.get("address") or "").lower()
            if addr == token_addr.lower():
                token_bal = float(item.get("balance") or item.get("amount") or 0)
                break
        if token_bal <= 0:
            return {"success": False, "error": "余额0"}
        r2 = subprocess.run([
            "baw","market-order","swap",
            "--binanceChainId", BSC_CHAIN_ID,
            "--fromTokenQty",   str(token_bal),
            "--fromToken",      token_addr,
            "--toToken",        USDT_BSC,
        ], capture_output=True, text=True, timeout=60)
        if r2.returncode == 0 and "Order ID" in r2.stdout:
            oid = r2.stdout.split("Order ID:")[-1].strip().split()[0]
            return {"success": True, "order_id": oid, "qty": token_bal}
        return {"success": False, "error": (r2.stdout+r2.stderr)[:200]}
    except Exception as e:
        return {"success": False, "error": str(e)}

# ─── 双轨巡检 + 移动止损（优化3）─────────────────────────────────

def _do_exit(ta, sym, pnl, cur, buy_price, reason, today_start):
    """执行全仓平仓并处理后续状态，可由价格监控和聪明钱跟随共同调用"""
    global _fused
    result = execute_sell(ta, sym)
    if result["success"]:
        send_notify(
            f"{reason}\n代币: ${sym}\n"
            f"买入价: {buy_price:.8f}\n卖出价: {cur:.8f}\n"
            f"PnL: {pnl*100:.1f}%\n订单: {result['order_id']}"
        )
        if pnl < 0:
            with _lock:
                _daily_losses.append(time.time())
                today_losses = [t for t in _daily_losses if t > today_start]
                if len(today_losses) >= MAX_DAILY_LOSSES and not _fused:
                    _fused = True
                    send_notify(
                        f"⛔ 熔断触发！今日已止损 {len(today_losses)} 笔\n"
                        f"系统已暂停新买入\n"
                        f"将于明日 UTC 00:00 自动重置"
                    )
        with _lock:
            _positions.pop(ta, None)
            _fast_mode.discard(ta)
            _sell_watch.pop(ta, None)
        append_log({"action":"EXIT","token":sym,"pnl_pct":pnl*100,
                    "reason":reason,"order_id":result["order_id"]})
        save_state()
        return True
    return False


def _monitor_positions():
    """持仓价格监控：正常45s / 跌幅>15%切10s高频"""
    while True:
        with _lock:
            positions_snap = dict(_positions)
            fast_set       = set(_fast_mode)

        # 有高频持仓用10s，否则45s
        interval = PRICE_CHECK_FAST if fast_set else PRICE_CHECK_NORMAL
        time.sleep(interval)

        now = time.time()
        today_start = now - (now % 86400)
        with _lock:
            _daily_losses[:] = [t for t in _daily_losses if t > today_start]

        for ta, pos in positions_snap.items():
            try:
                # 如果聪明钱已经触发平仓，这个仓位可能已不存在，跳过
                with _lock:
                    if ta not in _positions:
                        continue

                info = dex_info(ta)
                cur  = info.get("price", 0.0)
                if cur <= 0:
                    continue

                sym       = pos.get("symbol", "?")
                buy_price = pos.get("buy_price", 0)
                if buy_price <= 0:
                    continue

                pnl       = (cur / buy_price - 1)
                peak      = pos.get("peak", buy_price)
                tp1_done  = pos.get("tp1_done", False)
                trail_sl  = pos.get("trail_sl", 0)
                sl        = pos.get("sl", buy_price * SL_MULT)

                # 高频模式判断（跌幅>15%）
                with _lock:
                    if pnl < FAST_MODE_TRIGGER:
                        _fast_mode.add(ta)
                        if ta not in fast_set:
                            log(f"[FAST] ${sym} 跌幅{pnl*100:.1f}% 启动高频巡检")
                    elif ta in _fast_mode and pnl > FAST_MODE_TRIGGER + 0.05:
                        _fast_mode.discard(ta)

                # 更新历史最高价 & 移动止损线
                if cur > peak:
                    peak = cur
                    if tp1_done:
                        trail_sl = peak * (1 - TRAIL_PCT)
                    with _lock:
                        if ta in _positions:
                            _positions[ta]["peak"]     = peak
                            _positions[ta]["trail_sl"] = trail_sl

                reason = None

                # TP1：实际卖出50%保本，剩余50%转移动止损
                if not tp1_done and cur >= buy_price * TP1_MULT:
                    result_half = execute_sell_half(ta, sym)
                    new_trail_sl = cur * (1 - TRAIL_PCT)
                    if result_half["success"]:
                        with _lock:
                            if ta in _positions:
                                _positions[ta]["tp1_done"]  = True
                                _positions[ta]["trail_sl"]  = new_trail_sl
                                _positions[ta]["sl"]        = new_trail_sl
                                _positions[ta]["peak"]      = cur
                        send_notify(
                            f"✅ 保本平仓完成 +{pnl*100:.0f}%\n代币: ${sym}\n"
                            f"已卖出 {TP1_SELL_RATIO*100:.0f}%，回收本金\n"
                            f"剩余 {(1-TP1_SELL_RATIO)*100:.0f}% 转移动止损\n"
                            f"移动止损保护位: {new_trail_sl:.8f}（峰值回撤{TRAIL_PCT*100:.0f}%）\n"
                            f"硬止盈上限: ×{TP2_HARD_MULT}\n"
                            f"订单: {result_half['order_id']}"
                        )
                        append_log({"action":"TP1_HALF","token":sym,"pnl_pct":pnl*100,
                                    "order_id":result_half["order_id"]})
                        save_state()
                    else:
                        log(f"[TP1_FAIL] ${sym} 半仓卖出失败: {result_half['error']}")
                    continue

                # 硬止损
                if not tp1_done and cur <= sl:
                    reason = f"🔴 止损 {pnl*100:.1f}%"
                # 移动止损（剩余50%）
                elif tp1_done and trail_sl > 0 and cur <= trail_sl:
                    reason = f"📉 移动止损 {pnl*100:.1f}%（峰值回撤{TRAIL_PCT*100:.0f}%）"
                # 硬止盈（剩余50%）
                elif cur >= buy_price * TP2_HARD_MULT:
                    reason = f"🟢 全清止盈 +{pnl*100:.0f}%"

                if reason:
                    log(f"[EXIT] ${sym} {reason}")
                    _do_exit(ta, sym, pnl, cur, buy_price, reason, today_start)
            except Exception as e:
                log(f"[MONITOR] {ta[:10]}: {e}")

# ─── 聪明钱卖出检测（最高优先级）────────────────────────────────

def _check_sell_watch(sells: list):
    """
    检测触发地址在买入后窗口期内卖出同一代币。
    优先级最高：检测到离场 → 立即拉取实时价判断，自动跟随平仓。
    """
    now = time.time()
    today_start = now - (now % 86400)

    with _lock:
        sw_snap = dict(_sell_watch)

    for sell in sells:
        ta     = sell["token_addr"]
        seller = sell["seller_addr"]
        if ta not in sw_snap:
            continue
        watch = sw_snap[ta]
        if now - watch["buy_ts"] > SMART_EXIT_WINDOW:
            continue
        if seller not in watch["buyers"]:
            continue

        sym     = watch.get("symbol", ta[:8])
        lbl     = WATCH_ADDRS.get(seller, {}).get("label", seller[:10])
        elapsed = int((now - watch["buy_ts"]) / 60)

        log(f"[SMART_EXIT] ${sym} 触发地址 {lbl} 在买入后 {elapsed}分钟 开始卖出")

        # 拉取当前实时价
        with _lock:
            pos = _positions.get(ta)
        if not pos:
            # 仓位不存在（可能已平），清理监控
            with _lock:
                _sell_watch.pop(ta, None)
            continue

        buy_price = pos.get("buy_price", 0)
        info = dex_info(ta)
        cur  = info.get("price", 0.0) if info else 0.0

        if cur > 0 and buy_price > 0:
            pnl = cur / buy_price - 1
        else:
            pnl = 0.0

        # 判断是否跟随出场
        # 规则：若当前仍盈利或亏损在阈值以内（>-5%），直接跟随平仓
        # 若已重亏（<-5%），正常止损流程处理，避免双重触发
        if pnl >= SMART_EXIT_FOLLOW_THRESHOLD:
            log(f"[SMART_EXIT] ${sym} PnL={pnl*100:.1f}%，跟随聪明钱清仓")
            reason = (
                f"🧠 跟随聪明钱出场 {'+' if pnl>=0 else ''}{pnl*100:.1f}%\n"
                f"触发：{lbl}（持仓{elapsed}分钟后离场）"
            )
            _do_exit(ta, sym, pnl, cur if cur > 0 else buy_price,
                     buy_price, reason, today_start)
        else:
            # 已重亏，推送预警但不强制平，等正常止损
            log(f"[SMART_EXIT] ${sym} PnL={pnl*100:.1f}% 重亏中，预警不强制平")
            send_notify(
                f"⚠️ 聪明钱出场预警\n\n"
                f"代币: ${sym}\n"
                f"离场地址: {lbl}（买入后{elapsed}分钟）\n"
                f"当前PnL: {pnl*100:.1f}%（已重亏，等止损触发）\n"
                f"止损位: {pos.get('sl', 0):.8f}"
            )
            # 仍然移出窗口防重复触发
            with _lock:
                _sell_watch.pop(ta, None)

# ─── 买入执行 ────────────────────────────────────────────────────

def execute_buy(token_addr) -> dict:
    try:
        r = subprocess.run([
            "baw","market-order","swap",
            "--binanceChainId", BSC_CHAIN_ID,
            "--fromTokenQty",   str(BUY_AMOUNT_USD),
            "--fromToken",      USDT_BSC,
            "--toToken",        token_addr,
        ], capture_output=True, text=True, timeout=60)
        if r.returncode == 0 and "Order ID" in r.stdout:
            oid = r.stdout.split("Order ID:")[-1].strip().split()[0]
            # 查实际成交量，反算真实均价
            actual_price = None
            try:
                r2 = subprocess.run(
                    ["baw","market-order","list","--orderId", oid],
                    capture_output=True, text=True, timeout=15
                )
                # 解析 "Amount: X USDT → Y Token"
                for line in r2.stdout.splitlines():
                    if "Amount:" in line and "→" in line:
                        parts = line.split("→")
                        from_part = parts[0].split("Amount:")[-1].strip()
                        to_part   = parts[1].strip()
                        from_amt  = float(from_part.split()[0])
                        to_amt    = float(to_part.split()[0])
                        if to_amt > 0:
                            actual_price = from_amt / to_amt
                        break
            except Exception:
                pass
            return {"success": True, "order_id": oid, "actual_price": actual_price}
        return {"success": False, "error": (r.stdout+r.stderr)[:200]}
    except Exception as e:
        return {"success": False, "error": str(e)}

# ─── 信号处理流水线 ───────────────────────────────────────────────

def process_candidate(token_addr, buyers, total_usd):
    buyer_lbls  = [b["label"] for b in buyers]
    best_score  = max(WATCH_ADDRS[b["addr"]]["score"] for b in buyers)
    buyer_addrs = [b["addr"] for b in buyers]
    log(f"[PIPELINE] {token_addr[:10]}... 买家:{buyer_lbls} 最高评分:{best_score}")

    now = time.time()
    today_start = now - (now % 86400)

    # ① 熔断检查（自动次日重置）
    with _lock:
        global _fused
        today_losses = [t for t in _daily_losses if t > today_start]
        if _fused and len(today_losses) < MAX_DAILY_LOSSES:
            _fused = False  # 新的一天，自动解除
        if _fused:
            log(f"[FUSE] 熔断中，跳过")
            return

    # ② 持仓上限
    with _lock:
        if len(_positions) >= MAX_POSITIONS:
            log(f"[SKIP] 持仓已满 {len(_positions)}/{MAX_POSITIONS}")
            _processed.discard(token_addr)
            return

    # ③ 冷却期
    if now - _cooldown.get(token_addr, 0) < TOKEN_COOLDOWN:
        remaining = (TOKEN_COOLDOWN - (now - _cooldown.get(token_addr, 0))) / 3600
        log(f"[CD] {token_addr[:10]} 冷却中 {remaining:.1f}h")
        return

    # ④ DEX Screener 多维度过滤
    dex = dex_info(token_addr)
    if "error" in dex or not dex:
        log(f"[SKIP] DEX无数据")
        return

    symbol   = dex["symbol"]
    liq      = dex["liq"]
    age_days = dex["age_days"]
    age_min  = dex["age_min"]
    price    = dex["price"]
    price5m  = dex["price5m"]
    price1h  = dex["price1h"]
    dex_url  = dex["dex_url"]
    buyers1h = dex["buyers1h"]

    # 流动性
    if liq < MIN_LIQUIDITY:
        log(f"[REJECT] ${symbol} 流动性${liq:,.0f}")
        _cooldown[token_addr] = now; save_state(); return

    # 开盘狙击保护（优化4）
    if age_min < MIN_POOL_AGE_MIN:
        log(f"[REJECT] ${symbol} 池子仅{age_min:.1f}分钟，等待稳定")
        _cooldown[token_addr] = now; save_state(); return

    # 币龄
    if age_days > MAX_TOKEN_AGE:
        log(f"[REJECT] ${symbol} 币龄{age_days:.0f}天")
        _cooldown[token_addr] = now; save_state(); return

    # 多维度反追高（优化1）
    if price5m > MAX_PUMP_5M * 100:
        log(f"[REJECT] ${symbol} 5m涨幅{price5m:.0f}%>{MAX_PUMP_5M*100:.0f}%，刚被拉盘")
        _cooldown[token_addr] = now; save_state(); return
    if price1h > MAX_PUMP_1H * 100:
        log(f"[REJECT] ${symbol} 1h涨幅{price1h:.0f}%>{MAX_PUMP_1H*100:.0f}%，追高风险")
        _cooldown[token_addr] = now; save_state(); return
    if price1h < MIN_DROP_1H * 100:
        log(f"[REJECT] ${symbol} 1h跌幅{price1h:.0f}%，接刀保护")
        _cooldown[token_addr] = now; save_state(); return

    # ⑤ GoPlus 审计
    log(f"[AUDIT] ${symbol}...")
    t0    = time.time()
    audit = goplus_audit(token_addr)
    log(f"[AUDIT] ${symbol} {time.time()-t0:.1f}s → {audit['reason']}")
    if audit["pass"] is False:
        send_notify(f"🚫 审计拒绝 ${symbol}\n{audit['reason']}")
        append_log({"action":"REJECT_AUDIT","token":symbol,"addr":token_addr,"reason":audit["reason"]})
        _cooldown[token_addr] = now; save_state(); return

    holders  = audit.get("holders", 0)
    buy_tax  = audit.get("buy_tax", 0)
    sell_tax = audit.get("sell_tax", 0)

    # ⑥ 执行买入
    log(f"[BUY] ${symbol} {BUY_AMOUNT_USD}U...")
    result = execute_buy(token_addr)
    _cooldown[token_addr] = now

    if result["success"]:
        oid = result["order_id"]
        # 优先用实际成交均价，回退到 DexScreener 报价
        actual_price = result.get("actual_price")
        buy_price = actual_price if actual_price and actual_price > 0 else price
        if actual_price and actual_price > 0:
            log(f"[BUY] 实际成交均价: ${buy_price:.8f}（DEX报价: ${price:.8f}，偏差: {(price/buy_price-1)*100:+.1f}%）")
        else:
            log(f"[BUY] 使用DEX报价: ${buy_price:.8f}（实际成交价查询失败，将在下次巡检修正）")

        pos = {
            "symbol":    symbol,
            "buy_price": buy_price,
            "buy_ts":    int(now*1000),
            "buy_usd":   BUY_AMOUNT_USD,
            "order_id":  oid,
            "buyers":    buyer_lbls,
            "peak":      buy_price,
            "sl":        buy_price * SL_MULT,
            "tp1":       buy_price * TP1_MULT,
            "tp2":       buy_price * TP2_HARD_MULT,
            "tp1_done":  False,
            "trail_sl":  0,
        }
        with _lock:
            _positions[token_addr] = pos
            # 注册卖出监控（优化2）
            _sell_watch[token_addr] = {
                "symbol":  symbol,
                "buy_ts":  now,
                "buyers":  buyer_addrs,
            }
        save_state()

        price_note = f"（实测{buy_price:.8f}，DEX报价{price:.8f}，偏差{(price/buy_price-1)*100:+.1f}%）" if actual_price and actual_price > 0 else ""
        send_notify(
            f"🚨 聪明钱信号 · BNB Chain\n\n"
            f"📍 代币：${symbol}\n"
            f"🔗 合约：{token_addr[:20]}...（{age_days:.0f}天 / 池龄{age_min:.0f}分钟）\n"
            f"🐋 触发：{', '.join(buyer_lbls[:3])} (评分{best_score})\n"
            f"💰 鲸鱼买入：${total_usd:,.0f} | 1h买家:{buyers1h}人\n"
            f"📊 流动性：${liq:,.0f} | 持有人：{holders}\n"
            f"📈 5m:{price5m:.1f}% / 1h:{price1h:.1f}%\n"
            f"🛡️ 审计：{audit['reason']} 买税{buy_tax*100:.0f}%卖税{sell_tax*100:.0f}%\n\n"
            f"✅ 已买入 {BUY_AMOUNT_USD}U @ {buy_price:.8f}{price_note}\n"
            f"止损：×{SL_MULT}={buy_price*SL_MULT:.8f}\n"
            f"出本：×{TP1_MULT}={buy_price*TP1_MULT:.8f}（转移动止损）\n"
            f"全清：×{TP2_HARD_MULT}={buy_price*TP2_HARD_MULT:.8f}\n"
            f"订单：{oid}\n\n"
            f"[查看] {dex_url}"
        )
        append_log({"action":"BUY_OK","token":symbol,"addr":token_addr,
                    "price":buy_price,"dex_price":price,"order_id":oid,"buyers":buyer_lbls,"score":best_score,
                    "price5m":price5m,"price1h":price1h,"liq":liq})
        log(f"[BUY_OK] ${symbol} 订单:{oid}")
    else:
        log(f"[BUY_FAIL] ${symbol}: {result['error'][:100]}")
        append_log({"action":"BUY_FAIL","token":symbol,"addr":token_addr,"error":result.get("error","")})

# ─── 每日收盘报告（优化6）────────────────────────────────────────

def _daily_report_scheduler():
    """每日 UTC 23:59 发送收盘报告，00:00 重置熔断"""
    last_report_date = ""
    last_reset_date  = ""
    start_usdt       = get_wallet_usdt()

    while True:
        time.sleep(30)
        now     = datetime.now(timezone.utc)
        date_s  = now.strftime("%Y-%m-%d")
        hh, mm  = now.hour, now.minute

        # UTC 00:00 自动重置熔断计数器
        if hh == 0 and mm == 0 and date_s != last_reset_date:
            global _fused
            with _lock:
                today_start = time.time() - (time.time() % 86400)
                _daily_losses[:] = [t for t in _daily_losses if t > today_start]
                was_fused = _fused
                _fused = False
            last_reset_date = date_s
            log(f"[RESET] 新的一天，熔断计数器重置 {'（解除熔断）' if was_fused else ''}")
            start_usdt = get_wallet_usdt()  # 记录今日起始资金

        # UTC 23:59 发送日报
        if hh == 23 and mm == 59 and date_s != last_report_date:
            last_report_date = date_s
            _send_daily_report(date_s, start_usdt)

def _send_daily_report(date_s: str, start_usdt: float):
    end_usdt = get_wallet_usdt()

    # 从日志读今日交易记录
    today_logs = []
    if LOG_FILE.exists():
        try:
            all_logs = json.loads(LOG_FILE.read_text())
            today_start_ms = int((time.time() - time.time() % 86400) * 1000)
            today_logs = [l for l in all_logs if l.get("ts", 0) > today_start_ms]
        except: pass

    buys   = [l for l in today_logs if l.get("action") == "BUY_OK"]
    exits  = [l for l in today_logs if l.get("action") == "EXIT"]
    wins   = [l for l in exits if l.get("pnl_pct", 0) > 0]
    losses = [l for l in exits if l.get("pnl_pct", 0) <= 0]

    with _lock:
        open_cnt = len(_positions)

    pnl_usdt = end_usdt - start_usdt
    pnl_pct  = (pnl_usdt / start_usdt * 100) if start_usdt > 0 else 0

    # 读取资金曲线
    curve = []
    if CURVE_FILE.exists():
        try: curve = json.loads(CURVE_FILE.read_text())
        except: pass

    # 累计收益
    if curve:
        total_start = curve[0].get("start_usdt", start_usdt)
        total_pnl   = (end_usdt - total_start) / total_start * 100 if total_start > 0 else 0
        days        = len(curve) + 1
    else:
        total_pnl   = pnl_pct
        days        = 1

    # 保存今日数据
    save_equity({
        "date": date_s, "start_usdt": start_usdt, "end_usdt": end_usdt,
        "pnl_usdt": pnl_usdt, "pnl_pct": pnl_pct,
        "trades": len(buys), "wins": len(wins), "losses": len(losses)
    })

    # 资金曲线文本
    curve_lines = ""
    if curve:
        recent = curve[-5:]  # 最近5天
        curve_lines = "\n📈 近期资金曲线：\n"
        for c in recent:
            sign = "+" if c["pnl_pct"] >= 0 else ""
            curve_lines += f"  {c['date']}: {c['end_usdt']:.1f}U ({sign}{c['pnl_pct']:.1f}%)\n"
        curve_lines += f"  {date_s}: {end_usdt:.1f}U ({'+' if pnl_pct>=0 else ''}{pnl_pct:.1f}%) ← 今日\n"

    sign = "+" if pnl_usdt >= 0 else ""
    report = (
        f"📊 每日收盘报告 {date_s}\n\n"
        f"💰 起始资金：{start_usdt:.2f} U\n"
        f"💰 结束资金：{end_usdt:.2f} U\n"
        f"📈 今日盈亏：{sign}{pnl_usdt:.2f}U（{sign}{pnl_pct:.1f}%）\n\n"
        f"今日交易：{len(buys)}笔买入\n"
        f"  ✅ 止盈：{len(wins)}笔\n"
        f"  ❌ 止损：{len(losses)}笔\n"
        f"  ⏳ 持仓中：{open_cnt}笔\n"
        f"{curve_lines}\n"
        f"📊 累计收益：{'+' if total_pnl>=0 else ''}{total_pnl:.1f}%（运行{days}天）"
    )
    send_notify(report)
    log(f"[DAILY] 日报已发送 {date_s}")

# ─── 区块处理 ────────────────────────────────────────────────────

def on_new_block(block_hex):
    global _last_block
    block_num = int(block_hex, 16)
    with _lock:
        if block_num <= _last_block:
            return
        _last_block = block_num
    log(f"[BLOCK] #{block_num}")

    buys, sells = get_block_transfers(block_hex)

    # 卖出检测（优化2）
    if sells:
        threading.Thread(target=_check_sell_watch, args=(sells,), daemon=True).start()

    to_process = []
    with _lock:
        for t in buys:
            ta    = t["token_addr"]
            binfo = t["buyer_info"]
            tier  = _tier(binfo["score"])
            if ta in _processed or ta in _positions:
                continue
            buyers = _pending[ta]["buyers"]
            if not any(b["addr"] == t["buyer_addr"] for b in buyers):
                buyers.append({"addr": t["buyer_addr"], "label": binfo["label"], "score": binfo["score"]})
                tr = {"TOP":0,"MID":1,"LOW":2}
                if tr[tier] < tr[_pending[ta].get("best_tier","LOW")]:
                    _pending[ta]["best_tier"] = tier

        for ta, info in list(_pending.items()):
            if ta in _processed or ta in _positions:
                continue
            best_tier = info.get("best_tier","LOW")
            if len(info["buyers"]) >= _min_buyers(best_tier):
                to_process.append((ta, list(info["buyers"]), info["total_usd"]))
                _processed.add(ta)
                del _pending[ta]

    for ta, buyers, total_usd in to_process:
        threading.Thread(target=process_candidate,
                          args=(ta, buyers, total_usd), daemon=True).start()

# ─── WebSocket ───────────────────────────────────────────────────

def ws_on_open(ws):
    global _req_id, _ws_reconnect_delay
    log(f"[WS] 已连接 {BSC_WS_URL}")
    # BUG-C09 修复：连接成功时重置退避延迟
    _ws_reconnect_delay = 5
    # 清理过期 pending（超过10分钟未处理的条目）
    now = time.time()
    stale = [k for k, v in list(_pending.items())
             if now - v.get("first_seen", now) > 600]
    for k in stale:
        del _pending[k]
    ws.send(json.dumps({"jsonrpc":"2.0","id":_req_id,
                         "method":"eth_subscribe","params":["newHeads"]}))
    _req_id += 1

def ws_on_message(ws, message):
    try:
        data = json.loads(message)
        params = data.get("params")
        if params:
            bh = (params.get("result") or {}).get("number")
            if bh:
                threading.Thread(target=on_new_block, args=(bh,), daemon=True).start()
    except Exception as e:
        log(f"[WS] 解析错误: {e}")

def ws_on_error(ws, e): log(f"[WS] 错误: {e}")
def ws_on_close(ws, code, msg): log(f"[WS] 断开 code={code}")

_ws_reconnect_delay = 5

def run_ws():
    global _ws_reconnect_delay
    while True:
        try:
            ws = websocket.WebSocketApp(BSC_WS_URL,
                on_open=ws_on_open, on_message=ws_on_message,
                on_error=ws_on_error, on_close=ws_on_close)
            ws.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as e:
            log(f"[WS] 异常: {e}")
        time.sleep(_ws_reconnect_delay)
        _ws_reconnect_delay = min(_ws_reconnect_delay * 2, 60)

# ─── 主入口 ──────────────────────────────────────────────────────

# ─── 轮询兜底（捕获非标准Transfer）────────────────────────────────
def _poll_top_addresses():
    """
    每 POLL_INTERVAL 秒轮询一次 TOP 地址持仓列表，
    对比上一轮持仓快照，发现新增买入则进入流水线。
    专门应对代理合约或非标准 Transfer 导致 WS 漏掉的情况。
    """
    # 每个地址的小快照: token_addr -> last_buy_ts
    snapshots: dict[str, dict[str, int]] = {}
    now_ms = int(time.time() * 1000)

    while True:
        time.sleep(POLL_INTERVAL)
        try:
            top_addrs = [
                (addr, info) for addr, info in WATCH_ADDRS.items()
                if _tier(info["score"]) in POLL_TIERS
            ]
            for addr, info in top_addrs:
                try:
                    r = subprocess.run(
                        ["binance-pro-cli", "web3", "address", addr, "--chain", BSC_CHAIN_ID],
                        capture_output=True, text=True, timeout=15
                    )
                    data     = json.loads(r.stdout)
                    holdings = data.get("data", {}).get("list", []) or []
                    prev     = snapshots.get(addr, {})
                    new_snap = {}

                    for h in holdings:
                        ta       = (h.get("contractAddress") or "").lower()
                        last_tx  = h.get("lastTxTime", 0) or 0
                        buy_cnt  = h.get("buyCnt", 0) or 0
                        buy_usd  = float(h.get("buyAmtUsd") or 0)
                        if not ta: continue
                        new_snap[ta] = last_tx

                        # 新入：上一轮快照没有这个 token，或 last_tx 比快照新
                        is_new_buy = (
                            ta not in prev and buy_cnt > 0 and buy_usd >= MIN_TX_USD
                        ) or (
                            ta in prev and last_tx > prev[ta] and buy_cnt > 0
                        )

                        if is_new_buy:
                            with _lock:
                                if ta in _processed or ta in _positions:
                                    continue
                                already = [b["addr"] for b in _pending[ta]["buyers"]]
                                if addr not in already:
                                    _pending[ta]["buyers"].append({
                                        "addr":  addr,
                                        "label": info["label"],
                                        "score": info["score"],
                                    })
                                    _pending[ta]["total_usd"] += buy_usd
                                    # TOP 地址直接达到触发门槛
                                    best_tier = _tier(info["score"])
                                    tr = {"TOP":0,"MID":1,"LOW":2}
                                    if tr[best_tier] < tr[_pending[ta].get("best_tier","LOW")]:
                                        _pending[ta]["best_tier"] = best_tier

                                    log(f"[POLL] 捕获新买入 ${h.get('symbol','?')} 来自 {info['label']} ${buy_usd:,.0f}")

                                    # 检查是否达到触发条件
                                    info_pending = _pending[ta]
                                    needed = _min_buyers(_tier(info["score"]))
                                    if len(info_pending["buyers"]) >= needed:
                                        buyers_snap  = list(info_pending["buyers"])
                                        total_usd    = info_pending["total_usd"]
                                        _processed.add(ta)
                                        del _pending[ta]
                                        threading.Thread(
                                            target=process_candidate,
                                            args=(ta, buyers_snap, total_usd),
                                            daemon=True
                                        ).start()

                    snapshots[addr] = new_snap
                    time.sleep(0.4)  # 避免连续请求限速
                except Exception as e:
                    log(f"[POLL] {info['label']} 异常: {e}")
        except Exception as e:
            log(f"[POLL] 循环异常: {e}")


if __name__ == "__main__":
    log("Smart Copy Trader v5.0 启动")
    log(f"分层触发: TOP(≥90)×{TIER_TOP_MIN} / MID(70-89)×{TIER_MID_MIN} / LOW(<70)×{TIER_LOW_MIN}")
    log(f"止损:×{SL_MULT} | 出本:×{TP1_MULT}→移动止损(回撤{TRAIL_PCT*100:.0f}%) | 全清:×{TP2_HARD_MULT}")
    log(f"过滤: 流动性>${MIN_LIQUIDITY/1000:.0f}k | 池龄>{MIN_POOL_AGE_MIN}min | 5m<{MAX_PUMP_5M*100:.0f}% | 1h<{MAX_PUMP_1H*100:.0f}% | 1h>-{abs(MIN_DROP_1H)*100:.0f}%")
    log(f"税率合计<{MAX_TAX_TOTAL*100:.0f}% | 日熔断{MAX_DAILY_LOSSES}笔 | 次日UTC00:00自动重置")

    load_state()
    log(f"持仓:{len(_positions)}个 | 已处理:{len(_processed)}个 | 熔断:{_fused}")

    threading.Thread(target=_monitor_positions,     daemon=True, name="pos-monitor").start()
    threading.Thread(target=_daily_report_scheduler, daemon=True, name="daily-report").start()
    threading.Thread(target=_poll_top_addresses,     daemon=True, name="poll-fallback").start()
    log(f"轮询兜底已启动：TOP地址每{POLL_INTERVAL//60}分钟扫一次（共{sum(1 for _,i in WATCH_ADDRS.items() if _tier(i['score'])=='咤TOP'[0])}... 个）")

    run_ws()
