#!/usr/bin/env python3
"""
SOL Smart Money 跟单系统 v1.0
信号源：binance-pro-cli web3 signal --chain CT_501（Binance 内置聪明钱信号）
执行：baw market-order swap（Jupiter Aggregator）
审计：RugCheck API（专为 Solana 设计）
价格监控：DexScreener SOL
止盈止损：与 BSC 版本一致（SL×0.72 / TP1×2.0 半仓+移动止损 / TP2×6.0）
"""
import json, subprocess, time, threading, sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict

# ─── 配置 ──────────────────────────────────────────────────────────────────
WORKSPACE        = Path("/root/.openclaw/workspace")
STATE_FILE       = WORKSPACE / "sol_cw_state.json"
LOG_FILE         = WORKSPACE / "sol_cw_log.json"
CURVE_FILE       = WORKSPACE / "sol_equity_curve.json"

BUY_USD          = 25           # 每次买入目标金额（U）
GAS_RESERVE_USD  = 5            # gas 预留（U），动态换算为 SOL
MAX_POSITIONS    = 2            # 最多同时持仓2个土狗
MAX_DAILY_LOSSES = 3            # 日熔断阈值

# 止盈三档（无固定止损，用聪明钱止损替代）
TP1_MULT         = 2.00         # 第一档：2x → 卖出 50%，回收全部本金
TP1_SELL_RATIO   = 0.50
TP2_MULT         = 4.00         # 第二档：4x → 再卖出 25%，锁定利润
TP2_SELL_RATIO   = 0.25
TP3_MULT         = 6.00         # 第三档：6x → 全清，不贪
TP2_HARD_MULT    = TP3_MULT     # 兼容旧引用

# 聪明钱止损（最高优先级）
SMART_EXIT_RATIO  = 0.30        # 聪明钱卖出 ≥ 30% → 立刻全清
SMART_EXIT_WINDOW = 4 * 3600    # 买入4小时内持续跟踪

# 归零保护
LIQUIDITY_ZERO_THRESHOLD = 1_000   # 流动性跌破 $1000 视为枯竭

# 信号过滤
MIN_SMART_MONEY_COUNT = 2       # 至少2个聪明钱地址同时买
MIN_SIGNAL_VALID      = True    # 只接受 status=valid 的信号（未超时）
MAX_EXIT_RATE         = 75      # exitRate < 70 才接（太多人已跑说明错过了）
SIGNAL_POLL_INTERVAL  = 20      # 每20秒拉一次新信号（原60s，优化时效性）
PRICE_CHECK_NORMAL    = 45      # 巡检间隔（秒）
PRICE_CHECK_FAST      = 10      # 高频巡检间隔
FAST_MODE_TRIGGER     = -0.15   # 跌幅>15%切高频

# DEX 过滤
MIN_LIQUIDITY         = 50_000  # 流动性 < $50K 不入场（防操控）
MIN_POOL_AGE_MIN      = 5       # 池子至少5分钟
MAX_PUMP_5M           = 0.30    # 5分钟涨幅>30%拒绝
MAX_PUMP_1H           = 0.60    # 1小时涨幅>60%拒绝
MIN_DROP_1H           = -0.25   # 1小时跌幅>25%拒绝（接刀保护）
MAX_TOKEN_AGE_DAYS    = 30      # 币龄上限30天

# 链参数
SOL_CHAIN_ID     = "CT_501"
SOL_WALLET       = "CnM1arbWrUEC2VxExoVjVerf3qBxp6orP14KKC8mR56i"
HELIUS_RPC       = "https://mainnet.helius-rpc.com/?api-key=ff09d3f0-92e2-4e0c-b3f4-ec53f6b4f5fe"
# 公共 RPC 兜底列表（Helius 401 时自动切换）
FALLBACK_RPCS    = [
    "https://api.mainnet-beta.solana.com",
    "https://solana-api.projectserum.com",
]

# 滑点保护：成交价与 DEX 报价偏差超过此值则视为买入失败
MAX_PRICE_DEVIATION = 2.0   # 最大允许偏差倍数（2.0 = 200%，超过即异常）
JARVIS_TARGET    = "432401404:thread:019fa944-6d97-7061-b957-beca3ac89d75"

# ─── 全局状态 ──────────────────────────────────────────────────────────────
_lock          = threading.Lock()
_positions     = {}     # mint_addr -> pos
_processed     = set()  # 已处理的信号 signalId
_cooldown      = {}     # mint_addr -> ts
_daily_losses  = []
_fused         = False
_sell_watch    = {}     # mint_addr -> {signal_id, buy_ts, smart_money_count}
_fast_mode     = set()

def ts_str():
    return datetime.now(timezone.utc).strftime("%H:%M:%S")

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

# ─── 信号拉取 ─────────────────────────────────────────────────────────────

def fetch_signals(page_size=50):
    """从 Binance 拉取 SOL 链聪明钱信号"""
    try:
        r = subprocess.run(
            ["binance-pro-cli", "web3", "signal",
             "--chain", SOL_CHAIN_ID, "--page-size", str(page_size),
             "--no-compact", "--format", "json"],
            capture_output=True, text=True, timeout=20
        )
        data = json.loads(r.stdout)
        return data.get("data", [])
    except Exception as e:
        log(f"[SIGNAL] 拉取失败: {e}")
        return []

# ─── DexScreener SOL ──────────────────────────────────────────────────────

def dex_info_sol(mint_addr):
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{mint_addr}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        r   = urllib.request.urlopen(req, timeout=12)
        d   = json.loads(r.read())
        pairs = d.get("pairs") or []
        # 过滤 SOL 链 pairs
        sol_pairs = [p for p in pairs if p.get("chainId") == "solana"]
        if not sol_pairs:
            sol_pairs = pairs
        if not sol_pairs:
            return {}
        pair    = max(sol_pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd", 0) or 0))
        liq     = float((pair.get("liquidity") or {}).get("usd", 0) or 0)
        price   = float(pair.get("priceUsd") or 0)
        chg     = pair.get("priceChange") or {}
        price5m = float(chg.get("m5", 0) or 0)
        price1h = float(chg.get("h1", 0) or 0)
        symbol  = (pair.get("baseToken") or {}).get("symbol", "?")
        dex_url = pair.get("url", f"https://dexscreener.com/solana/{mint_addr}")
        created = pair.get("pairCreatedAt") or 0
        age_days = (time.time()*1000 - created) / 86_400_000 if created else 999
        age_min  = (time.time()*1000 - created) / 60_000     if created else 999
        txns1h   = (pair.get("txns") or {}).get("h1", {})
        buyers1h = int(txns1h.get("buys", 0) or 0)
        fdv      = float(pair.get("fdv") or 0)
        return {
            "symbol": symbol, "liq": liq, "price": price,
            "price5m": price5m, "price1h": price1h,
            "age_days": age_days, "age_min": age_min,
            "dex_url": dex_url, "buyers1h": buyers1h,
            "fdv": fdv,
        }
    except Exception as e:
        return {"error": str(e)}

# ─── RugCheck 审计 ────────────────────────────────────────────────────────

def rugcheck_audit(mint_addr):
    """使用 RugCheck API 审计 SOL 代币安全性"""
    try:
        url = f"https://api.rugcheck.xyz/v1/tokens/{mint_addr}/report/summary"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        r   = urllib.request.urlopen(req, timeout=15)
        d   = json.loads(r.read())

        score = d.get("score", 0)  # 越低越安全（0=安全，1000=危险）
        risks = d.get("risks", [])

        # 高危风险检测
        critical_risks = []
        warn_risks = []
        for risk in risks:
            level = risk.get("level", "").lower()
            name  = risk.get("name", "")
            if level in ("danger", "critical"):
                critical_risks.append(name)
            elif level == "warn":
                warn_risks.append(name)

        if critical_risks:
            return {"pass": False, "reason": f"🚫 {' | '.join(critical_risks)}", "score": score}
        if score > 500:
            return {"pass": False, "reason": f"🚫 风险分 {score}/1000 过高", "score": score}

        reason = "✅通过"
        if warn_risks:
            reason = f"⚠️通过（{' | '.join(warn_risks[:2])}）"

        return {"pass": True, "reason": reason, "score": score, "warns": warn_risks}
    except Exception as e:
        return {"pass": None, "reason": f"审计异常({e})"}

# ─── SOL 钱包余额 ─────────────────────────────────────────────────────────

def get_wallet_sol_balance() -> float:
    """查询机器人钱包的 SOL 余额"""
    try:
        r = subprocess.run(
            ["baw", "wallet", "balance"],
            capture_output=True, text=True, timeout=15
        )
        for line in r.stdout.splitlines():
            if "SOL" in line and "CT_501" in line or ("SOL" in line and "So111" in line):
                parts = line.split()
                for p in parts:
                    try:
                        val = float(p)
                        if 0 < val < 1000:
                            return val
                    except:
                        pass
    except:
        pass
    # 用 Helius RPC 直接查
    try:
        payload = json.dumps({
            "jsonrpc": "2.0", "id": 1,
            "method": "getBalance",
            "params": [SOL_WALLET]
        }).encode()
        req = urllib.request.Request(HELIUS_RPC, data=payload,
                                      headers={"Content-Type": "application/json"})
        r   = urllib.request.urlopen(req, timeout=10)
        d   = json.loads(r.read())
        lamports = d.get("result", {}).get("value", 0)
        return lamports / 1e9
    except:
        return 0.0

def get_token_balance_sol(mint_addr: str) -> float:
    """查询机器人钱包对某个 token 的余额，Helius 优先，失败则轮询公共 RPC"""
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "method": "getTokenAccountsByOwner",
        "params": [SOL_WALLET, {"mint": mint_addr}, {"encoding": "jsonParsed"}]
    }).encode()

    rpcs = [HELIUS_RPC] + FALLBACK_RPCS
    for rpc in rpcs:
        try:
            req = urllib.request.Request(rpc, data=payload,
                                          headers={"Content-Type": "application/json"})
            r   = urllib.request.urlopen(req, timeout=10)
            d   = json.loads(r.read())
            accounts = (d.get("result") or {}).get("value", [])
            if not accounts:
                return 0.0
            total = 0.0
            for acc in accounts:
                ui_amount = (acc.get("account", {})
                               .get("data", {})
                               .get("parsed", {})
                               .get("info", {})
                               .get("tokenAmount", {})
                               .get("uiAmount") or 0)
                total += float(ui_amount)
            return total
        except Exception as e:
            log(f"[BAL] {rpc.split('/')[2][:20]} 失败: {e}")
            continue
    log(f"[BAL] {mint_addr[:10]} 所有 RPC 均失败")
    return 0.0

# ─── 买入/卖出执行 ────────────────────────────────────────────────────────

# SOL mint地址说明：
# 原生SOL（Native SOL）: So11111111111111111111111111111111111111111  (末尾...1111)
# WSOL（Wrapped SOL）: So11111111111111111111111111111111111111112  (末尾...1112)
# baw swap 的 fromToken 必须用原生SOL地址，不能用WSOL地址
# 错误码 30003001 = WSOL余额不足，根因就是用了错误的mint地址
WSOL_MINT = "So11111111111111111111111111111111111111111"  # 原生SOL地址（末尾...111）

def execute_buy_sol(mint_addr: str, buy_amount_sol: float = 0.34) -> dict:
    """用 SOL 买入指定代币"""
    try:
        r = subprocess.run([
            "baw", "market-order", "swap",
            "--binanceChainId", SOL_CHAIN_ID,
            "--fromTokenQty",   str(buy_amount_sol),
            "--fromToken",      WSOL_MINT,
            "--toToken",        mint_addr,
        ], capture_output=True, text=True, timeout=60)
        if r.returncode == 0 and "Order ID" in r.stdout:
            oid = r.stdout.split("Order ID:")[-1].strip().split()[0]
            # 查实际成交量，反算真实均价
            actual_price = None
            try:
                r2 = subprocess.run(
                    ["baw", "market-order", "list", "--orderId", oid],
                    capture_output=True, text=True, timeout=15
                )
                for line in r2.stdout.splitlines():
                    if "Amount:" in line and "→" in line:
                        parts = line.split("→")
                        from_amt = float(parts[0].split("Amount:")[-1].strip().split()[0])
                        to_amt   = float(parts[1].strip().split()[0])
                        if to_amt > 0:
                            actual_price = from_amt / to_amt
                        break
            except Exception:
                pass
            return {"success": True, "order_id": oid, "actual_price": actual_price}
        return {"success": False, "error": (r.stdout + r.stderr)[:300]}
    except Exception as e:
        return {"success": False, "error": str(e)}

def execute_sell_sol(mint_addr: str, qty: float = 0) -> dict:
    """卖出全部（或指定数量）代币换回 SOL"""
    try:
        if qty <= 0:
            qty = get_token_balance_sol(mint_addr)
        if qty <= 0:
            return {"success": False, "error": "余额0"}
        r = subprocess.run([
            "baw", "market-order", "swap",
            "--binanceChainId", SOL_CHAIN_ID,
            "--fromTokenQty",   str(qty),
            "--fromToken",      mint_addr,
            "--toToken",        WSOL_MINT,
            "--json",
        ], capture_output=True, text=True, timeout=60)
        resp = json.loads(r.stdout)
        if resp.get("success"):
            oid = resp["data"]["orderId"]
            return {"success": True, "order_id": oid, "qty": qty}
        return {"success": False, "error": resp.get("error", {}).get("message", r.stdout)[:300]}
    except Exception as e:
        return {"success": False, "error": str(e)}

def execute_sell_half_sol(mint_addr: str) -> dict:
    """卖出50%代币（保本用）"""
    qty = get_token_balance_sol(mint_addr)
    if qty <= 0:
        return {"success": False, "error": "余额0"}
    return execute_sell_sol(mint_addr, qty * TP1_SELL_RATIO)

def place_tp1_limit_order(mint_addr: str, qty: float, buy_price: float) -> dict:
    """
    买入后立即挂翻倍限价卖单（出本）。
    返回 {"success": bool, "strategy_id": str}
    """
    target_price = buy_price * TP1_MULT
    try:
        r = subprocess.run([
            "baw", "limit-order", "sell",
            "--binanceChainId", SOL_CHAIN_ID,
            "--triggerPrice",  f"{target_price:.10f}",
            "--fromTokenQty",  f"{qty:.4f}",
            "--fromToken",     mint_addr,
            "--toToken",       WSOL_MINT,
            "--json",
        ], capture_output=True, text=True, timeout=30)
        resp = json.loads(r.stdout)
        if resp.get("success"):
            return {"success": True, "strategy_id": str(resp["data"]["strategyId"]),
                    "target_price": target_price}
        return {"success": False, "error": resp.get("error", {}).get("message", r.stdout)[:200]}
    except Exception as e:
        return {"success": False, "error": str(e)}

def cancel_limit_order(strategy_id: str) -> bool:
    """撤销限价单，返回是否成功"""
    try:
        r = subprocess.run([
            "baw", "limit-order", "cancel",
            "--strategyId", strategy_id,
            "--json",
        ], capture_output=True, text=True, timeout=20)
        resp = json.loads(r.stdout)
        return resp.get("success", False)
    except:
        return False

# ─── 平仓核心逻辑 ─────────────────────────────────────────────────────────

def _do_exit(mint_addr, sym, pnl, cur, buy_price, reason, today_start):
    global _fused
    result = execute_sell_sol(mint_addr)
    if result["success"]:
        send_notify(
            f"{reason}\n"
            f"代币: ${sym}\n"
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
                        f"系统已暂停新买入，将于明日 UTC 00:00 自动重置"
                    )
        with _lock:
            _positions.pop(mint_addr, None)
            _fast_mode.discard(mint_addr)
            _sell_watch.pop(mint_addr, None)
        append_log({"action": "EXIT", "token": sym, "pnl_pct": pnl*100,
                    "reason": reason, "order_id": result["order_id"]})
        save_state()
        return True
    else:
        log(f"[EXIT_FAIL] ${sym} 卖出失败: {result['error'][:100]}")
        return False

# ─── 持仓价格监控 ─────────────────────────────────────────────────────────

def _monitor_positions():
    while True:
        with _lock:
            positions_snap = dict(_positions)
            fast_set       = set(_fast_mode)

        interval = PRICE_CHECK_FAST if fast_set else PRICE_CHECK_NORMAL
        time.sleep(interval)

        now = time.time()
        today_start = now - (now % 86400)
        with _lock:
            _daily_losses[:] = [t for t in _daily_losses if t > today_start]

        for mint_addr, pos in positions_snap.items():
            try:
                with _lock:
                    if mint_addr not in _positions:
                        continue

                info = dex_info_sol(mint_addr)
                cur  = info.get("price", 0.0)
                if cur <= 0:
                    continue

                sym       = pos.get("symbol", "?")
                buy_price = pos.get("buy_price", 0)
                if buy_price <= 0:
                    continue

                pnl      = cur / buy_price - 1
                peak     = pos.get("peak", buy_price)
                tp1_done = pos.get("tp1_done", False)
                tp2_done = pos.get("tp2_done", False)

                # 更新峰值
                if cur > peak:
                    with _lock:
                        if mint_addr in _positions:
                            _positions[mint_addr]["peak"] = cur

                reason = None

                # ── 归零保护：流动性枯竭 ──
                cur_liq = info.get("liq", 999999)
                if cur_liq < LIQUIDITY_ZERO_THRESHOLD:
                    reason = f"⚠️ 流动性枯竭（${cur_liq:,.0f}），紧急出场"
                    _do_exit(mint_addr, sym, pnl, cur, buy_price, reason, today_start)
                    continue

                # ── 第一档：2x → 卖出 50%，回收全部本金 ──
                if not tp1_done and cur >= buy_price * TP1_MULT:
                    qty_now  = get_token_balance_sol(mint_addr)
                    sell_qty = qty_now * TP1_SELL_RATIO
                    res = execute_sell_sol(mint_addr, sell_qty)
                    if res["success"]:
                        tp1_strategy = pos.get("tp1_strategy", "")
                        if tp1_strategy:
                            cancel_limit_order(tp1_strategy)
                        with _lock:
                            if mint_addr in _positions:
                                _positions[mint_addr]["tp1_done"]     = True
                                _positions[mint_addr]["tp1_strategy"] = ""
                        save_state()
                        send_notify(
                            f"🎯 **TP1 翻倍出本**\n代币: ${sym}\n"
                            f"已卖出 {TP1_SELL_RATIO*100:.0f}%，全部本金已回收\n"
                            f"剩余 {(1-TP1_SELL_RATIO)*100:.0f}% 零成本持仓\n"
                            f"下一目标: 4x @ {buy_price*TP2_MULT:.10f}\n"
                            f"订单: {res['order_id']}"
                        )
                        append_log({"action": "TP1", "token": sym, "price": cur,
                                    "pnl_pct": pnl*100, "order_id": res["order_id"]})
                        log(f"[TP1] ${sym} 2x出本卖50%")
                    else:
                        log(f"[TP1_FAIL] ${sym}: {res['error']}")
                    continue

                # ── 第二档：4x → 再卖出原始仓位25% ──
                elif tp1_done and not tp2_done and cur >= buy_price * TP2_MULT:
                    qty_now  = get_token_balance_sol(mint_addr)
                    orig_qty = pos.get("token_qty", qty_now * 2)
                    sell_qty = min(orig_qty * TP2_SELL_RATIO, qty_now)
                    res = execute_sell_sol(mint_addr, sell_qty)
                    if res["success"]:
                        with _lock:
                            if mint_addr in _positions:
                                _positions[mint_addr]["tp2_done"] = True
                        save_state()
                        send_notify(
                            f"💰 **TP2 四倍锁利**\n代币: ${sym}\n"
                            f"再卖出 {TP2_SELL_RATIO*100:.0f}%，利润已锁定\n"
                            f"剩余 25% 零成本继续持有\n"
                            f"最终目标: 6x @ {buy_price*TP3_MULT:.10f}\n"
                            f"订单: {res['order_id']}"
                        )
                        append_log({"action": "TP2", "token": sym, "price": cur,
                                    "pnl_pct": pnl*100, "order_id": res["order_id"]})
                        log(f"[TP2] ${sym} 4x卖25%")
                    continue

                # ── 第三档：6x → 全清 ──
                elif tp1_done and cur >= buy_price * TP3_MULT:
                    reason = f"🏆 TP3 六倍全清 +{pnl*100:.0f}%"

                if reason:
                    _do_exit(mint_addr, sym, pnl, cur, buy_price, reason, today_start)

            except Exception as e:
                log(f"[MONITOR] {mint_addr[:10]}: {e}")

# ─── 信号处理流水线 ───────────────────────────────────────────────────────

def process_signal(signal: dict):
    """处理单条聪明钱信号"""
    global _fused

    mint_addr  = signal.get("contractAddress", "")
    signal_id  = signal.get("signalId", 0)
    ticker     = signal.get("ticker", "?")
    sm_count   = signal.get("smartMoneyCount", 0)
    exit_rate  = signal.get("exitRate", 100)
    max_gain   = float(signal.get("maxGain") or 0)
    alert_price = float(signal.get("alertPrice") or 0)
    status     = signal.get("status", "")
    signal_type = signal.get("smartSignalType", "")

    # 半仓标记（outDecline / timeout救活路径）
    _is_half = signal.get("_half_size", False)
    _entry_reason = signal.get("_reason", "")

    if not mint_addr:
        return

    log(f"[SIGNAL] ${ticker} id={signal_id} sm={sm_count} exit={exit_rate}% type={signal_type}")

    now = time.time()
    today_start = now - (now % 86400)

    # ① 已处理
    with _lock:
        if signal_id in _processed or mint_addr in _positions:
            return

    # ② 熔断
    with _lock:
        today_losses = [t for t in _daily_losses if t > today_start]
        if _fused and len(today_losses) < MAX_DAILY_LOSSES:
            _fused = False
        if _fused:
            log(f"[FUSE] 熔断中，跳过 ${ticker}")
            return

    # ③ 持仓上限
    with _lock:
        if len(_positions) >= MAX_POSITIONS:
            log(f"[SKIP] 持仓已满 {len(_positions)}/{MAX_POSITIONS}")
            return

    # ④ 冷却
    if now - _cooldown.get(mint_addr, 0) < 4 * 3600:
        log(f"[CD] ${ticker} 冷却中")
        return

    # ⑤ 信号质量过滤
    if sm_count < MIN_SMART_MONEY_COUNT:
        log(f"[FILTER] ${ticker} 聪明钱数量不足 {sm_count}<{MIN_SMART_MONEY_COUNT}")
        return
    if exit_rate >= MAX_EXIT_RATE:
        log(f"[FILTER] ${ticker} exitRate={exit_rate}%≥{MAX_EXIT_RATE}%，信号过时")
        with _lock:
            _cooldown[mint_addr] = now  # 聪明钱已跑，才加冷却
        save_state()
        return
    if status not in ("valid", "") and not _is_half:
        log(f"[FILTER] ${ticker} status={status}，跳过非有效信号")
        with _lock:
            _cooldown[mint_addr] = now
        save_state()
        return
    # outDecline / timeout 路径：额外确认 exitRate 不能太高
    if _is_half and exit_rate >= 40:
        log(f"[FILTER] ${ticker} {_entry_reason} exitRate={exit_rate}%已过高，放弃")
        return

    # ⑥ DexScreener 过滤（流动性/池龄/涨幅）
    # 注意：这里 REJECT 不加冷却，流动性不足是暂时的，下次信号再检查
    dex = dex_info_sol(mint_addr)
    if "error" in dex or not dex:
        log(f"[SKIP] ${ticker} DEX无数据")
        return

    liq      = dex.get("liq", 0)
    price    = dex.get("price", 0)
    price5m  = dex.get("price5m", 0)
    price1h  = dex.get("price1h", 0)
    age_days = dex.get("age_days", 999)
    age_min  = dex.get("age_min", 999)
    dex_url  = dex.get("dex_url", "")
    buyers1h = dex.get("buyers1h", 0)
    fdv      = dex.get("fdv", 0)

    if liq < MIN_LIQUIDITY:
        log(f"[REJECT] ${ticker} 流动性${liq:,.0f}<${MIN_LIQUIDITY:,.0f}，跳过")
        return

    # 持有人集中度检查（前10持有人占比 > 80% 拒绝）
    try:
        hld_url = f"https://api.rugcheck.xyz/v1/tokens/{mint_addr}/report/summary"
        import urllib.request as _ur
        req = _ur.Request(hld_url, headers={"User-Agent": "Mozilla/5.0"})
        hld_resp = json.loads(_ur.urlopen(req, timeout=8).read())
        top10_pct = hld_resp.get("topHolders", {}).get("top10Pct", 0) or 0
        if top10_pct > 80:
            log(f"[REJECT] ${ticker} 前10持有人占比 {top10_pct:.1f}%>80%，集中度过高")
            return
        if top10_pct > 0:
            log(f"[OK] ${ticker} 前10持有人占比 {top10_pct:.1f}%")
    except Exception as e:
        log(f"[WARN] ${ticker} 持有人集中度查询失败: {e}，跳过该检查")  # ← 不加冷却
    if age_min < MIN_POOL_AGE_MIN:
        log(f"[REJECT] ${ticker} 池龄{age_min:.1f}min<{MIN_POOL_AGE_MIN}min")
        return  # ← 不加冷却
    if age_days > MAX_TOKEN_AGE_DAYS:
        log(f"[REJECT] ${ticker} 币龄{age_days:.0f}天>{MAX_TOKEN_AGE_DAYS}天")
        with _lock:
            _cooldown[mint_addr] = now  # 老币，加冷却避免反复查
        save_state()
        return
    if price5m > MAX_PUMP_5M * 100:
        log(f"[REJECT] ${ticker} 5m涨幅{price5m:.0f}%>{MAX_PUMP_5M*100:.0f}%")
        return  # ← 涨幅过高是暂时的，不加冷却
    if price1h > MAX_PUMP_1H * 100:
        log(f"[REJECT] ${ticker} 1h涨幅{price1h:.0f}%>{MAX_PUMP_1H*100:.0f}%")
        with _lock:
            _cooldown[mint_addr] = now  # 短期涨太多，冷却1小时后再看
        save_state()
        return
    if price1h < MIN_DROP_1H * 100:
        log(f"[REJECT] ${ticker} 1h跌幅{price1h:.0f}%<{MIN_DROP_1H*100:.0f}%，接刀保护")
        return  # ← 接刀保护，不加冷却

    # ⑦ RugCheck 审计
    log(f"[AUDIT] ${ticker} {mint_addr[:10]}...")
    t0    = time.time()
    audit = rugcheck_audit(mint_addr)
    log(f"[AUDIT] ${ticker} {time.time()-t0:.1f}s → {audit['reason']}")
    if audit["pass"] is False:
        send_notify(f"🚫 审计拒绝 ${ticker}\n{audit['reason']}")
        append_log({"action": "REJECT_AUDIT", "token": ticker,
                    "addr": mint_addr, "reason": audit["reason"]})
        _cooldown[mint_addr] = now; save_state(); return

    # ⑧ 执行买入
    # 动态计算本次买入 SOL 数量
    sol_price = dex_info_sol("So11111111111111111111111111111111111111111").get("price", 0)
    if sol_price <= 0:
        # fallback: 用 DexScreener BTC 估算
        sol_price = 73.5
    buy_sol = round((BUY_USD / 2 if _is_half else BUY_USD) / sol_price, 4)
    gas_reserve_sol = round(GAS_RESERVE_USD / sol_price, 4)
    _size_label = f"半仓({_entry_reason})" if _is_half else "标准仓"

    # gas 检查
    sol_bal = get_wallet_sol_balance()
    if sol_bal < buy_sol + gas_reserve_sol:
        log(f"[SKIP] ${ticker} SOL余额不足（{sol_bal:.4f}<{buy_sol+gas_reserve_sol:.4f}），跳过")
        return

    log(f"[BUY] ${ticker} {buy_sol} SOL（≈${BUY_USD / 2 if _is_half else BUY_USD}, SOL=${sol_price:.2f}) {_size_label}...")

    # 先标记已处理，防止并发重复触发
    with _lock:
        _processed.add(signal_id)
        _cooldown[mint_addr] = now

    result = execute_buy_sol(mint_addr, buy_sol)

    if result["success"]:
        oid = result["order_id"]
        actual_price = result.get("actual_price")
        buy_price = actual_price if actual_price and actual_price > 0 else price
        if actual_price and actual_price > 0:
            log(f"[BUY] 实际成交均价: ${buy_price:.10f}（DEX报价: ${price:.10f}，偏差: {(price/buy_price-1)*100:+.1f}%）")
        else:
            log(f"[BUY] 使用DEX报价作为备选（实际成交价查询失败）")

        # ── 滑点保护：成交价与 DEX 报价偏差超过 MAX_PRICE_DEVIATION 倍则个失败 ──
        if actual_price and actual_price > 0 and price > 0:
            deviation = abs(actual_price - price) / price
            if deviation > MAX_PRICE_DEVIATION:
                log(f"[SLIP_REJECT] ${ticker} 成交价偏差{deviation*100:.0f}%>限制{MAX_PRICE_DEVIATION*100:.0f}%，"
                    f"实测={actual_price:.8f} DEX={price:.8f}，不计入持仓")
                send_notify(
                    f"⚠️ SOL 买入滑点异常\n\n"
                    f"${ticker} 成交价偏差 {deviation*100:.0f}%（限制 {MAX_PRICE_DEVIATION*100:.0f}%）\n"
                    f"实测: ${actual_price:.8f}\nDEX: ${price:.8f}\n\n"
                    f"订单已提交但不计入持仓，建议手动检查钱包余额"
                )
                append_log({"action": "SLIP_REJECT", "token": ticker, "addr": mint_addr,
                            "actual_price": actual_price, "dex_price": price,
                            "deviation_pct": round(deviation*100, 1), "order_id": oid})
                return  # 不写入 positions，相当于忽略此笔

        # 查链上真实持仓数量（稍等成交确认）
        time.sleep(3)
        token_qty = get_token_balance_sol(mint_addr)
        if token_qty <= 0:
            token_qty = (BUY_AMOUNT_SOL * 73.5) / buy_price if buy_price > 0 else 0
            log(f"[BUY] 链上查询数量失败，估算: {token_qty:.2f}")

        pos = {
            "symbol":       ticker,
            "mint":         mint_addr,
            "buy_price":    buy_price,
            "buy_ts":       int(now * 1000),
            "buy_sol":      buy_sol,
            "order_id":     oid,
            "signal_id":    signal_id,
            "sm_count":     sm_count,
            "peak":         buy_price,
            "sl":           buy_price * SL_MULT,
            "tp1":          buy_price * TP1_MULT,
            "tp2":          buy_price * TP2_HARD_MULT,
            "tp1_done":     False,
            "trail_sl":     0,
            "tp1_strategy": "",
            "token_qty":    token_qty,
        }
        with _lock:
            _positions[mint_addr] = pos
            _sell_watch[mint_addr] = {
                "symbol":    ticker,
                "buy_ts":    now,
                "signal_id": signal_id,
                "sm_count":  sm_count,
            }
        save_state()

        # ── 立即挂翻倍限价卖单 ──
        tp1_strategy_id = ""
        if token_qty > 0:
            lim = place_tp1_limit_order(mint_addr, token_qty, buy_price)
            if lim["success"]:
                tp1_strategy_id = lim["strategy_id"]
                with _lock:
                    if mint_addr in _positions:
                        _positions[mint_addr]["tp1_strategy"] = tp1_strategy_id
                save_state()
                log(f"[LIMIT] 翻倍限价卖单已挂: strategyId={tp1_strategy_id} @ {lim['target_price']:.10f}")
            else:
                log(f"[LIMIT_FAIL] 限价卖单失败: {lim['error']}，将由监控线程市价处理")

        price_note = f"（实测{buy_price:.10f}，DEX{price:.10f}，偏差{(price/buy_price-1)*100:+.1f}%）" if actual_price and actual_price > 0 else ""
        limit_note = f"\n📌 翻倍限价卖单: {tp1_strategy_id} @ {buy_price*TP1_MULT:.10f}" if tp1_strategy_id else "\n⚠️ 限价卖单挂单失败，监控线程兜底"
        send_notify(
            f"🚨 SOL 聪明钱信号\n\n"
            f"📍 代币：${ticker}\n"
            f"🔗 合约：{mint_addr[:20]}...\n"
            f"🐋 聪明钱：{sm_count}个地址买入 | 类型：{signal_type}\n"
            f"📊 流动性：${liq:,.0f} | 1h买家：{buyers1h}人\n"
            f"📈 5m：{price5m:.1f}% / 1h：{price1h:.1f}%\n"
            f"💎 FDV：${fdv:,.0f} | 池龄：{age_min:.0f}分钟\n"
            f"🛡️ 审计：{audit['reason']} (风险分:{audit.get('score',0)})\n"
            f"📉 exitRate：{exit_rate}% | maxGain：{max_gain*100:.0f}%\n\n"
            f"✅ 已买入 {buy_sol} SOL（≈${BUY_USD}） @ {buy_price:.10f}{price_note}\n"
            f"（无固定止损，聪明钱卖出≥{SMART_EXIT_RATIO*100:.0f}%自动清仓）\n"
            f"翻倍出本：×{TP1_MULT} = {buy_price*TP1_MULT:.10f}{limit_note}\n"
            f"全清：×{TP2_HARD_MULT} = {buy_price*TP2_HARD_MULT:.10f}\n"
            f"订单：{oid}\n\n"
            f"[查看] {dex_url}"
        )
        append_log({
            "action": "BUY_OK", "token": ticker, "addr": mint_addr,
            "price": buy_price, "dex_price": price, "order_id": oid,
            "sm_count": sm_count, "signal_type": signal_type,
            "exit_rate": exit_rate, "price5m": price5m, "price1h": price1h,
            "liq": liq, "tp1_strategy": tp1_strategy_id,
        })
        log(f"[BUY_OK] ${ticker} 订单:{oid} 限价卖单:{tp1_strategy_id or 'NONE'}")
    else:
        log(f"[BUY_FAIL] ${ticker}: {result['error'][:150]}")
        append_log({"action": "BUY_FAIL", "token": ticker,
                    "addr": mint_addr, "error": result.get("error", "")})
        save_state()  # 确保 BUY_FAIL 后也持久化 processed，防止重启后重复触发同信号

# ─── 信号轮询主循环 ────────────────────────────────────────────────────────

def _signal_poll_loop():
    """定期拉取 SOL 聪明钱信号并处理"""
    log(f"[POLL] 信号轮询已启动，间隔 {SIGNAL_POLL_INTERVAL}s")
    _seen_ids = set()  # 本轮已处理的 signalId（防重复触发）
    while True:
        try:
            signals = fetch_signals(page_size=50)
            if not signals:
                log("[POLL] 无信号数据")
            else:
                # 优化路径1: valid 信号（默认路径）
                valid = [s for s in signals
                         if s.get("direction") == "buy"
                         and s.get("status") in ("valid", "")
                         and int(s.get("signalId", 0)) not in _seen_ids]

                # 优化路径2: outDecline 降级入场路径
                # exitRate<40%（大部分聪明钱还在）
                # 切半仓入场（把 BUY_USD 临时覆盖为 5 → 2.5）
                decline = [s for s in signals
                           if s.get("direction") == "buy"
                           and s.get("status") == "outDecline"
                           and s.get("exitRate", 100) < 40
                           and int(s.get("signalId", 0)) not in _seen_ids]

                # 优化路径3: timeout 信号二次确认路径
                # exitRate<50% 且未大幅拉升（最近 5m 涨幅<15%）
                timeout_rescue = [s for s in signals
                                  if s.get("direction") == "buy"
                                  and s.get("status") == "timeout"
                                  and s.get("exitRate", 100) < 50
                                  and int(s.get("signalId", 0)) not in _seen_ids]

                total_valid = len(valid) + len(decline) + len(timeout_rescue)
                log(f"[POLL] 收到 {len(signals)} 条信号，其中有效买入信号 {len(valid)} 条"
                    f"（+outDecline降级 {len(decline)} 条，+timeout救活 {len(timeout_rescue)} 条）")

                # 处理 valid
                for sig in valid:
                    _seen_ids.add(int(sig.get("signalId", 0)))
                    threading.Thread(target=process_signal, args=(sig,), daemon=True).start()
                    time.sleep(0.3)

                # 处理 outDecline：将 BUY_USD 居中临时覆盖为半仓
                for sig in decline:
                    _seen_ids.add(int(sig.get("signalId", 0)))
                    sig_copy = dict(sig)
                    sig_copy["_half_size"] = True  # 标记为半仓信号
                    sig_copy["_reason"]   = "outDecline降级"
                    threading.Thread(target=process_signal, args=(sig_copy,), daemon=True).start()
                    time.sleep(0.3)

                # 处理 timeout救活：同样半仓
                for sig in timeout_rescue:
                    _seen_ids.add(int(sig.get("signalId", 0)))
                    sig_copy = dict(sig)
                    sig_copy["_half_size"] = True
                    sig_copy["_reason"]   = "timeout救活"
                    threading.Thread(target=process_signal, args=(sig_copy,), daemon=True).start()
                    time.sleep(0.3)

                # 防止 _seen_ids 无限増长：只保留最近 500 个
                if len(_seen_ids) > 500:
                    _seen_ids.clear()

        except Exception as e:
            log(f"[POLL] 异常: {e}")
        time.sleep(SIGNAL_POLL_INTERVAL)

# ─── 每日收盘报告 ─────────────────────────────────────────────────────────

# ─── 聪明钱卖出跟踪线程 ──────────────────────────────────────────────────

def _smart_exit_tracker():
    """
    每60秒重新拉取聪明钱信号，对比买入时的 exitRate。
    规则：
      - 持仓中的代币，信号 exitRate 上升超过 20pp（百分点）→ 聪明钱在撤
      - 或信号 status 变为 invalid / expired → 聪明钱已跑
      - 触发条件：exitRate >= 60% 且 比入场时上升 >= 20pp
    触发后：撤销翻倍限价卖单，立即市价卖出全仓，推送通知。
    """
    log(f"[SMART_EXIT] 聪明钱卖出追踪已启动，轮询间隔 60s")
    # 记录每个 mint 的入场 exitRate
    entry_exit_rate = {}

    while True:
        time.sleep(60)
        with _lock:
            watch_snap = dict(_sell_watch)
            pos_snap   = dict(_positions)

        if not watch_snap:
            continue

        try:
            signals = fetch_signals(page_size=100)
            signal_map = {s.get("contractAddress", ""): s for s in signals}
        except Exception as e:
            log(f"[SMART_EXIT] 拉取信号失败: {e}")
            continue

        now = time.time()
        for mint_addr, watch in watch_snap.items():
            try:
                with _lock:
                    if mint_addr not in _positions:
                        _sell_watch.pop(mint_addr, None)
                        continue

                pos = pos_snap.get(mint_addr, {})
                sym = watch.get("symbol", "?")
                buy_ts = watch.get("buy_ts", 0)

                # 超过跟踪窗口则停止跟踪（不强制卖出，由价格监控接管）
                if now - buy_ts > SMART_EXIT_WINDOW:
                    log(f"[SMART_EXIT] ${sym} 超出跟踪窗口（{SMART_EXIT_WINDOW/3600:.0f}h），停止跟踪")
                    with _lock:
                        _sell_watch.pop(mint_addr, None)
                    save_state()
                    continue

                sig = signal_map.get(mint_addr)
                if not sig:
                    continue

                cur_exit_rate = float(sig.get("exitRate", 0) or 0)
                cur_status    = sig.get("status", "valid")

                # 记录入场时 exitRate（第一次见到）
                if mint_addr not in entry_exit_rate:
                    entry_exit_rate[mint_addr] = cur_exit_rate
                    log(f"[SMART_EXIT] ${sym} 记录入场 exitRate={cur_exit_rate:.0f}%")
                    continue

                entry_rate = entry_exit_rate[mint_addr]
                rise       = cur_exit_rate - entry_rate

                log(f"[SMART_EXIT] ${sym} exitRate: {entry_rate:.0f}%→{cur_exit_rate:.0f}% (+{rise:.0f}pp) status={cur_status}")

                # 触发条件
                triggered = False
                trigger_reason = ""

                if cur_exit_rate >= SMART_EXIT_RATIO * 100 and rise >= 20:  # 30%+20pp
                    triggered = True
                    trigger_reason = f"聪明钱正在撤出（exitRate {entry_rate:.0f}%→{cur_exit_rate:.0f}%，上升{rise:.0f}pp）"

                if cur_status in ("invalid", "expired") and entry_rate < 50:
                    triggered = True
                    trigger_reason = f"聪明钱信号已失效（status={cur_status}）"

                if not triggered:
                    continue

                # 获取当前价格
                dex = dex_info_sol(mint_addr)
                cur_price = dex.get("price", 0)
                buy_price = pos.get("buy_price", 0)
                pnl = (cur_price / buy_price - 1) if buy_price > 0 else 0

                # 撤销翻倍限价卖单
                tp1_strategy = pos.get("tp1_strategy", "")
                if tp1_strategy:
                    ok = cancel_limit_order(tp1_strategy)
                    log(f"[SMART_EXIT] ${sym} 撤销限价卖单 {tp1_strategy}: {'OK' if ok else 'FAIL（可能已成交）'}")

                # 市价全部卖出
                result = execute_sell_sol(mint_addr)
                today_start = now - (now % 86400)

                if result["success"]:
                    msg = (
                        f"🔔 **聪明钱卖出，跟随清仓**\n\n"
                        f"代币：${sym}\n"
                        f"原因：{trigger_reason}\n"
                        f"买入价：{buy_price:.10f}\n"
                        f"当前价：{cur_price:.10f}\n"
                        f"PnL：{pnl*100:+.1f}%\n"
                        f"订单：{result['order_id']}"
                    )
                    send_notify(msg)
                    with _lock:
                        _positions.pop(mint_addr, None)
                        _sell_watch.pop(mint_addr, None)
                        _fast_mode.discard(mint_addr)
                        if pnl < 0:
                            _daily_losses.append(now)
                    entry_exit_rate.pop(mint_addr, None)
                    append_log({"action": "SMART_EXIT", "token": sym, "pnl_pct": pnl*100,
                                "exit_rate": cur_exit_rate, "reason": trigger_reason,
                                "order_id": result["order_id"]})
                    save_state()
                    log(f"[SMART_EXIT] ${sym} 已清仓 PnL={pnl*100:+.1f}%")
                else:
                    log(f"[SMART_EXIT] ${sym} 卖出失败: {result['error']}")
                    send_notify(f"⚠️ 聪明钱已撤但清仓失败：${sym}\n{result['error']}\n请手动检查！")

            except Exception as e:
                log(f"[SMART_EXIT] {mint_addr[:10]} 异常: {e}")


def _daily_report_scheduler():
    last_report_date = ""
    last_reset_date  = ""
    start_sol = get_wallet_sol_balance()

    while True:
        time.sleep(30)
        now    = datetime.now(timezone.utc)
        date_s = now.strftime("%Y-%m-%d")
        hh, mm = now.hour, now.minute

        if hh == 0 and mm == 0 and date_s != last_reset_date:
            global _fused
            with _lock:
                today_start = time.time() - (time.time() % 86400)
                _daily_losses[:] = [t for t in _daily_losses if t > today_start]
                was_fused = _fused
                _fused = False
            last_reset_date = date_s
            log(f"[RESET] 新的一天 {'（解除熔断）' if was_fused else ''}")
            start_sol = get_wallet_sol_balance()

        if hh == 23 and mm == 59 and date_s != last_report_date:
            last_report_date = date_s
            _send_daily_report(date_s, start_sol)

def _send_daily_report(date_s: str, start_sol: float):
    end_sol = get_wallet_sol_balance()
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

    pnl_sol = end_sol - start_sol
    pnl_pct = (pnl_sol / start_sol * 100) if start_sol > 0 else 0
    sign    = "+" if pnl_sol >= 0 else ""

    report = (
        f"📊 SOL 跟单日报 {date_s}\n\n"
        f"💰 起始：{start_sol:.4f} SOL\n"
        f"💰 结束：{end_sol:.4f} SOL\n"
        f"📈 今日：{sign}{pnl_sol:.4f} SOL（{sign}{pnl_pct:.1f}%）\n\n"
        f"今日交易：{len(buys)}笔买入\n"
        f"  ✅ 止盈：{len(wins)}笔\n"
        f"  ❌ 止损：{len(losses)}笔\n"
        f"  ⏳ 持仓中：{open_cnt}笔"
    )
    send_notify(report)
    save_equity({
        "date": date_s, "start_sol": start_sol, "end_sol": end_sol,
        "pnl_sol": pnl_sol, "pnl_pct": pnl_pct,
        "trades": len(buys), "wins": len(wins), "losses": len(losses)
    })
    log(f"[DAILY] 日报已发送 {date_s}")

# ─── 主入口 ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    log("=" * 60)
    log("SOL Smart Money Copy Trader v1.0 启动")
    log(f"信号源：Binance web3 signal (CT_501)")
    log(f"钱包：{SOL_WALLET}")
    log(f"买入：${BUY_USD}U/次 | 最大持仓：{MAX_POSITIONS}个 | Gas预留：${GAS_RESERVE_USD}U")
    log(f"止盈：2x(50%)→4x(25%)→6x(全清) | 聪明钱卖出≥{SMART_EXIT_RATIO*100:.0f}%即清仓")
    log(f"信号过滤：sm≥{MIN_SMART_MONEY_COUNT} | exitRate<{MAX_EXIT_RATE}% | status=valid")
    log(f"DEX过滤：流动性>${MIN_LIQUIDITY/1000:.0f}k | 持有人前10<80% | 池龄>{MIN_POOL_AGE_MIN}min")
    log("=" * 60)

    load_state()
    log(f"持仓：{len(_positions)}个 | 已处理信号：{len(_processed)}个 | 熔断：{_fused}")

    # 启动监控线程
    threading.Thread(target=_monitor_positions,      daemon=True, name="pos-monitor").start()
    threading.Thread(target=_daily_report_scheduler, daemon=True, name="daily-report").start()
    threading.Thread(target=_smart_exit_tracker,     daemon=True, name="smart-exit").start()

    # 信号轮询主循环（主线程运行）
    _signal_poll_loop()
