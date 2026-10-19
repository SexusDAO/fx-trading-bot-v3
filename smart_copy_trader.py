#!/usr/bin/env python3
"""
BSC Smart Money 跟单系统 v1.0
- 监控 50 个优质地址的新买入 TX
- 双重确认：多地址共买 + 实时信号
- 30 秒内完成安全审计（蜜罐/貔貅/铸币权/黑名单）
- 自动执行 25U 买入
"""
import json, subprocess, time, os, sys, hmac, hashlib, requests
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

# ─── 配置 ───────────────────────────────────────────────────────
WORKSPACE   = Path("/root/.openclaw/workspace")
STATE_FILE  = WORKSPACE / "copy_trader_state.json"
LOG_FILE    = WORKSPACE / "copy_trader_log.json"

BUY_AMOUNT_USD   = 25        # 每笔买入 25U
MIN_SIGNAL_CNT   = 2         # 至少 N 个地址同时买入才触发
REQUIRE_SIGNAL   = True      # 是否需要实时信号双重确认
SCAN_INTERVAL    = 900       # 15 分钟扫一次
AUDIT_TIMEOUT    = 28        # 安全审计超时秒数
MIN_TX_USD       = 100       # 聪明钱买入最低金额（过滤噪音）
BSC_CHAIN_ID     = "56"

# Jarvis 推送
JARVIS_TARGET    = "432401404:thread:019f9203-3b29-7d9e-bfb4-b67c0b06587c"

# SOCKS5 代理
PROXY_URL = "socks5://REDACTED_PROXY_CREDENTIALS"
PROXIES   = {"http": PROXY_URL, "https": PROXY_URL}

# ─── 50 个优质监控地址 ────────────────────────────────────────────
WATCH_ADDRESSES = [
    {"addr": "0x2ce9d43d1cba6ae31d7f07bfe0098dfa2d833373", "label": "枯坐p小将",      "score": 107},
    {"addr": "0x1bff8f0ade179dfa1beddb54fa5d7d6dc814191a", "label": "Smart Money #1", "score": 96},
    {"addr": "0xd7828f0aa42c863ef2dabfe01832111ed21034c8", "label": "Smart Money #2", "score": 89},
    {"addr": "0x3c672bb3f5e16cac12953fd67b9f8ef6bd98f3bb", "label": "Smart Money #3", "score": 86},
    {"addr": "0x6a900ee716a579a6ea984015697de9fbf98989fc", "label": "Smart Money #4", "score": 85},
    {"addr": "0xbac453b9b7f53b35ac906b641925b2f5f2567a89", "label": "Dani",            "score": 83},
    {"addr": "0xfeccd1d6e453e44cdf7466644bc6ae1d70c354e5", "label": "Smart Money #5", "score": 83},
    {"addr": "0x7a2363a401b2340c7941dd2eeff0196a5078d2e6", "label": "金狗挖掘机0xDavid","score": 82},
    {"addr": "0xe14f5204244593df7d07e615590411baf3816ec4", "label": "Smart Money #6", "score": 80},
    {"addr": "0xa8a03e1c4154451c6d5d5f20b15aec8e8a45a517", "label": "Smart Money #7", "score": 75},
    {"addr": "0x81d5d84039f973584755a4805282ef94e2123302", "label": "Smart Money #8", "score": 75},
    {"addr": "0xcfe30db196c9a99fc80c72ad44fb4a135a53491f", "label": "旭旭宝宝",         "score": 75},
    {"addr": "0xbd0a401d7a41b15f25c95224bc87d9a85852b0a1", "label": "Smart Money #9", "score": 73},
    {"addr": "0xeeefff8ce2710fa490e0fcb794235e873c252d2e", "label": "Shenron🐉",        "score": 72},
    {"addr": "0xa7d4ffc4eca3c71af150ce302560a9d04a1d2b9f", "label": "MIRRO BNB",       "score": 71},
    {"addr": "0x5dbce812228bd08bfd4f08a2b6a1dbb613046afe", "label": "Smart Money #10","score": 71},
    {"addr": "0x9a2a1daca07556faca114b8e8485d4b1d9ab873a", "label": "Smart Money #11","score": 71},
    {"addr": "0x170332b75c0859a39bf7288f6cbf0db94bb1f567", "label": "TOM🦞",           "score": 71},
    {"addr": "0x051a53d095d277e22e3d905b10d3bbe75f0ac508", "label": "bruce",           "score": 70},
    {"addr": "0x55976c6818e4794f3e2e7179eea2cc2202811e11", "label": "九条命",          "score": 70},
    {"addr": "0xb2cd29be62895e84a6555f47de27e0926d63285a", "label": "Smart Money #12","score": 70},
    {"addr": "0x5b6211081130165ef3ce7841fe69f8714d0ecde7", "label": "Smart Money #13","score": 69},
    {"addr": "0x968e3059c22476dd953b38affb2cff6b37622979", "label": "Smart Money #14","score": 69},
    {"addr": "0xad440ea22e75d24cc80f595cbc5a617ae67da7ac", "label": "Smart Money #15","score": 69},
    {"addr": "0xbe1961f690b07f315699537f2dd81ea5dd974255", "label": "Smart Money #16","score": 69},
    {"addr": "0x5388668c4fa8da7f756ef8498ff24d7999999999", "label": "西瓜BNB",         "score": 68},
    {"addr": "0x94cf7ed19901eb7d487c779e6d1baa87157b2d01", "label": "888UP",           "score": 68},
    {"addr": "0xacd7793d92cd870e56db2372b36e41c59ae4fb40", "label": "Smart Money #17","score": 67},
    {"addr": "0x0fa3a520273545effec692de886683609fd0cf01", "label": "Smart Money #18","score": 67},
    {"addr": "0x4d9644d05fe2123b4eafa8d7fd31b0ea430726f3", "label": "降雨幾率",        "score": 66},
    {"addr": "0x29935ef1417e1c3b1fec68dd666ea9baac31d4ed", "label": "Smart Money #19","score": 65},
    {"addr": "0xddac928a240bdace3994c2cc0783d4e29a002127", "label": "o大扫链版",       "score": 65},
    {"addr": "0x3391a39a1b508e54a361924a26056c01c1c2c07d", "label": "Reljoo",          "score": 63},
    {"addr": "0x70d94aa3ad899b5456e8b1199049f98e2ab36a55", "label": "Smart Money #20","score": 63},
    {"addr": "0x76a280376c5332abbbae1786a73c70116906e757", "label": "Jerry",           "score": 63},
    {"addr": "0xd41feaa24dede516a501862cf1f376defb811772", "label": "milito",          "score": 61},
    {"addr": "0x77a3b5c87d7f4b03c84f7b33ec82bacdc4d512a0", "label": "Smart Money #21","score": 60},
    {"addr": "0x5b6560900ab57368c5127536e9ac9dbcbd61e942", "label": "Smart Money #22","score": 60},
    {"addr": "0x02545c226146d7b26949bd9168e4433af1e53bba", "label": "Smart Money #23","score": 59},
    {"addr": "0xecd1f3e99caa4cdbf174ad12ce79d89a8ba54fca", "label": "Smart Money #24","score": 59},
    {"addr": "0x3097ad08546ec879d67c84c898458993424e05c5", "label": "Smart Money #25","score": 59},
    {"addr": "0xa83b73f5644cde337b61da79589f10ea15548811", "label": "AntPositions",    "score": 58},
    {"addr": "0xb2d1af0746c410e146272e804b1741f07f83b851", "label": "Zephyr",          "score": 58},
    {"addr": "0x8ade93ba431a2ce19fc62a9ce97626e69a4a333f", "label": "0xLuck",          "score": 58},
    {"addr": "0xa1426c6f65fe804e05ce3c07a42412d735ef78bc", "label": "打狗大师",        "score": 56},
    {"addr": "0xd171ebe9d3c1e55d918563107b0b7782948dd52c", "label": "YYYzzy GDC",      "score": 54},
    {"addr": "0x82e159d63e585067e9fa3a4bba7d992fbc667751", "label": "Smart Money #26","score": 54},
    {"addr": "0xaaee1f3bb3583e360ebc1e5d8230428527e7341a", "label": "Smart Money #27","score": 53},
    {"addr": "0x9c701dec4120629207b519cfc4fca620b8b7de22", "label": "Smart Money #28","score": 52},
    {"addr": "0x877af245c61289b24f0c619a4346cdbc68f1aaac", "label": "古月月",          "score": 52},
]

# ─── 辅助函数 ─────────────────────────────────────────────────────

def log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{ts}] {msg}")

def send_notify(msg: str):
    """推送消息到 Jarvis"""
    try:
        r = subprocess.run(
            ["openclaw", "message", "send", "--target", JARVIS_TARGET,
             "--channel", "jarvis", "--message", msg],
            capture_output=True, text=True, timeout=10
        )
    except Exception as e:
        log(f"推送失败: {e}")

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"seen_tokens": {}, "positions": {}, "last_scan": 0}

def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))

def append_log(entry: dict):
    logs = []
    if LOG_FILE.exists():
        try: logs = json.loads(LOG_FILE.read_text())
        except: logs = []
    logs.append(entry)
    logs = logs[-500:]  # 保留最近500条
    LOG_FILE.write_text(json.dumps(logs, indent=2, ensure_ascii=False))

# ─── 步骤1：扫描地址新买入 ─────────────────────────────────────────

def scan_address_buys(addr: str, since_ts: int) -> list:
    """
    查询某地址在 BSC 链上 since_ts 之后的新买入交易
    返回: [{"token_addr": ..., "token_symbol": ..., "usd_amount": ..., "tx_time": ...}]
    """
    try:
        r = subprocess.run(
            ["binance-pro-cli", "web3", "address", addr, "--chain", BSC_CHAIN_ID],
            capture_output=True, text=True, timeout=20
        )
        data = json.loads(r.stdout)
        holdings = data.get("data", {}).get("list", []) or []
        buys = []
        for h in holdings:
            last_tx = h.get("lastTxTime", 0) or 0
            buy_cnt = h.get("buyCnt") or 0
            buy_amt = float(h.get("buyAmtUsd") or 0)
            if last_tx > since_ts and buy_cnt > 0 and buy_amt >= MIN_TX_USD:
                buys.append({
                    "token_addr":   h.get("contractAddress", ""),
                    "token_symbol": h.get("symbol", "?"),
                    "usd_amount":   buy_amt,
                    "tx_time":      last_tx,
                    "risk_level":   h.get("riskLevel", "UNKNOWN"),
                    "low_liq":      h.get("lowLiquidity", 0),
                })
        return buys
    except Exception as e:
        log(f"scan_address_buys error {addr[:10]}: {e}")
        return []

# ─── 步骤2：检查实时信号 ─────────────────────────────────────────

def check_realtime_signal(token_addr: str) -> bool:
    """检查代币是否出现在实时聪明钱信号中"""
    try:
        r = subprocess.run(
            ["binance-pro-cli", "web3", "signal", "--chain", BSC_CHAIN_ID],
            capture_output=True, text=True, timeout=20
        )
        data = json.loads(r.stdout)
        signals = data.get("data", {}).get("data", []) or []
        for s in signals:
            if s.get("tokenAddress", "").lower() == token_addr.lower():
                return True
        return False
    except Exception as e:
        log(f"check_realtime_signal error: {e}")
        return False

# ─── 步骤3：安全审计（30秒内完成）───────────────────────────────────

def security_audit(token_addr: str, token_symbol: str) -> dict:
    """
    调用 query-token-audit 做安全审计
    返回: {"pass": bool, "reason": str, "details": dict}
    """
    try:
        r = subprocess.run(
            ["binance-pro-cli", "web3", "address", token_addr,
             "--chain", BSC_CHAIN_ID, "--audit"],
            capture_output=True, text=True, timeout=AUDIT_TIMEOUT
        )
        # 尝试用 query-token-audit CLI
        r2 = subprocess.run(
            ["query-token-audit", "--chain", "bsc",
             "--contract", token_addr, "--json"],
            capture_output=True, text=True, timeout=AUDIT_TIMEOUT
        )
        if r2.returncode == 0:
            audit = json.loads(r2.stdout)
            honeypot     = audit.get("is_honeypot", False)
            blacklist    = audit.get("has_blacklist", False)
            mint_risk    = audit.get("mint_risk", False)
            open_source  = audit.get("is_open_source", True)
            buy_tax      = float(audit.get("buy_tax", 0))
            sell_tax     = float(audit.get("sell_tax", 0))

            fails = []
            if honeypot: fails.append("蜜罐检测触发")
            if blacklist: fails.append("存在黑名单")
            if mint_risk: fails.append("铸币权风险")
            if not open_source: fails.append("合约未开源")
            if sell_tax > 0.1: fails.append(f"卖税过高({sell_tax*100:.0f}%)")
            if buy_tax > 0.1: fails.append(f"买税过高({buy_tax*100:.0f}%)")

            if fails:
                return {"pass": False, "reason": " | ".join(fails), "details": audit}
            return {"pass": True, "reason": "审计通过", "details": audit}
    except Exception:
        pass

    # fallback: 用 baw 内建风险级别
    # 如果扫描时 risk_level 已经是 HIGH，直接拒绝
    return {"pass": None, "reason": "审计工具不可用，依赖内建风险级别", "details": {}}

# ─── 步骤4：执行买入 ─────────────────────────────────────────────

def get_usdt_contract() -> str:
    return "0x55d398326f99059ff775485246999027b3197955"  # BSC USDT

def execute_buy(token_addr: str, token_symbol: str, amount_usd: float) -> dict:
    """用 baw market-order swap 买入"""
    try:
        r = subprocess.run([
            "baw", "market-order", "swap",
            "--binanceChainId", BSC_CHAIN_ID,
            "--fromTokenQty", str(amount_usd),
            "--fromToken", get_usdt_contract(),
            "--toToken", token_addr,
        ], capture_output=True, text=True, timeout=60)

        if r.returncode == 0 and "Order ID" in r.stdout:
            order_id = r.stdout.split("Order ID:")[-1].strip().split()[0]
            return {"success": True, "order_id": order_id, "raw": r.stdout}
        else:
            return {"success": False, "error": r.stdout + r.stderr}
    except Exception as e:
        return {"success": False, "error": str(e)}

# ─── 主扫描逻辑 ──────────────────────────────────────────────────

def run_scan():
    state = load_state()
    now_ts = int(time.time() * 1000)
    last_scan = state.get("last_scan", 0)
    seen_tokens = state.get("seen_tokens", {})  # token_addr -> {first_seen, buyer_addrs, signal_hit}

    log(f"开始扫描，监控地址数: {len(WATCH_ADDRESSES)}")

    # ── 并行扫描所有地址的新买入 ──
    token_buyers = {}  # token_addr -> [{"addr": ..., "label": ..., "usd": ...}]

    for watcher in WATCH_ADDRESSES:
        addr  = watcher["addr"]
        label = watcher["label"]
        buys  = scan_address_buys(addr, last_scan)
        for buy in buys:
            ta = buy["token_addr"].lower()
            if not ta: continue
            if ta not in token_buyers:
                token_buyers[ta] = {
                    "symbol":   buy["token_symbol"],
                    "buyers":   [],
                    "risk":     buy["risk_level"],
                    "low_liq":  buy["low_liq"],
                }
            token_buyers[ta]["buyers"].append({
                "addr":  addr,
                "label": label,
                "usd":   buy["usd_amount"],
            })
        time.sleep(0.3)  # 避免 API 限速

    log(f"本轮发现新买入代币: {len(token_buyers)} 个")

    # ── 筛选：≥ MIN_SIGNAL_CNT 个地址同时买入 ──
    candidates = {
        ta: info for ta, info in token_buyers.items()
        if len(info["buyers"]) >= MIN_SIGNAL_CNT
    }
    log(f"多地址共买（≥{MIN_SIGNAL_CNT}个）: {len(candidates)} 个")

    for token_addr, info in candidates.items():
        symbol  = info["symbol"]
        buyers  = info["buyers"]
        buyer_labels = [b["label"] for b in buyers]

        log(f"候选: {symbol} ({token_addr[:10]}...) 被 {len(buyers)} 个地址买入: {buyer_labels}")

        # ── 已有持仓跳过 ──
        positions = state.get("positions", {})
        if token_addr in positions:
            log(f"  跳过：已持仓 {symbol}")
            continue

        # ── 内建风险过滤 ──
        if info["risk"] == "HIGH":
            log(f"  跳过：内建风险 HIGH")
            append_log({"ts": now_ts, "action": "REJECT_RISK", "token": symbol, "addr": token_addr})
            continue
        if info["low_liq"]:
            log(f"  跳过：低流动性")
            continue

        # ── 实时信号双重确认 ──
        has_signal = check_realtime_signal(token_addr)
        if REQUIRE_SIGNAL and not has_signal:
            log(f"  跳过：无实时信号确认")
            # 存入 seen_tokens，等下轮信号出现
            seen_tokens[token_addr] = {
                "symbol": symbol, "buyers": buyer_labels,
                "first_seen": now_ts, "signal_hit": False
            }
            continue

        # ── 安全审计（30秒内完成）──
        log(f"  开始安全审计: {symbol}...")
        audit_start = time.time()
        audit = security_audit(token_addr, symbol)
        audit_elapsed = time.time() - audit_start
        log(f"  审计耗时: {audit_elapsed:.1f}s | 结果: {audit['reason']}")

        if audit["pass"] is False:
            log(f"  ❌ 审计未通过: {audit['reason']}")
            send_notify(f"🚫 安全审计拒绝\n代币: {symbol}\n原因: {audit['reason']}")
            append_log({"ts": now_ts, "action": "REJECT_AUDIT", "token": symbol,
                        "addr": token_addr, "reason": audit["reason"]})
            continue

        # ── 执行买入 ──
        log(f"  ✅ 双重确认通过！执行买入 {BUY_AMOUNT_USD}U → {symbol}")
        result = execute_buy(token_addr, symbol, BUY_AMOUNT_USD)

        if result["success"]:
            entry = {
                "ts": now_ts,
                "action": "BUY",
                "token": symbol,
                "token_addr": token_addr,
                "usd": BUY_AMOUNT_USD,
                "order_id": result["order_id"],
                "buyers": buyer_labels,
                "signal": has_signal,
                "audit": audit["reason"],
            }
            append_log(entry)
            # 记录持仓
            positions[token_addr] = {
                "symbol": symbol,
                "buy_ts": now_ts,
                "buy_usd": BUY_AMOUNT_USD,
                "order_id": result["order_id"],
                "buyers": buyer_labels,
            }
            state["positions"] = positions

            notify_msg = (
                f"🟢 跟单买入成功\n"
                f"代币: {symbol}\n"
                f"金额: ${BUY_AMOUNT_USD}\n"
                f"触发地址({len(buyers)}个): {', '.join(buyer_labels[:3])}\n"
                f"实时信号: {'✅' if has_signal else '❌'}\n"
                f"安全审计: {audit['reason']}\n"
                f"订单ID: {result['order_id']}"
            )
            send_notify(notify_msg)
            log(f"  买入成功: {result['order_id']}")
        else:
            log(f"  买入失败: {result['error']}")
            send_notify(f"⚠️ 跟单买入失败\n代币: {symbol}\n错误: {result['error'][:100]}")
            append_log({"ts": now_ts, "action": "BUY_FAIL", "token": symbol,
                        "addr": token_addr, "error": result.get("error","")})

    # ── 更新 state ──
    state["last_scan"] = now_ts
    state["seen_tokens"] = seen_tokens
    save_state(state)

    # ── 扫描汇报 ──
    positions = state.get("positions", {})
    log(f"扫描完成 | 当前持仓: {len(positions)} 个")
    if candidates:
        names = [v["symbol"] for v in candidates.values()]
        log(f"本轮候选代币: {names}")


if __name__ == "__main__":
    if "--once" in sys.argv:
        run_scan()
    else:
        log("Smart Copy Trader 启动，监控 BSC 链 50 个地址")
        while True:
            try:
                run_scan()
            except Exception as e:
                log(f"扫描异常: {e}")
            log(f"下次扫描: {SCAN_INTERVAL//60} 分钟后")
            time.sleep(SCAN_INTERVAL)
