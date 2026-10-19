#!/usr/bin/env python3
"""
kol_exit_review.py — 系统性 KOL 触发卖出复盘 v3
-------------------------------------------------
v3 改进（2026-09-05）：
  以链上真实交易记录为准（baw wallet tx-history）
  日志只用于提取「触发原因」和「触发时间」
  确认状态 = on-chain confirmed，不依赖日志里的 SELL CONFIRMED/FAILED

流程:
  1. baw wallet tx-history → 所有 confirmed swap
  2. trade.log → 触发事件（EXECUTE EXIT reason）
  3. 两者对照：用 tx_time 和 log trigger_time 配对
  4. DexScreener 查当前价格对比决策质量

用法:
  python3 kol_exit_review.py           # 只输出到终端
  python3 kol_exit_review.py --push    # 同时推送到 Jarvis
"""

import re, sys, json, subprocess, requests, os
from datetime import datetime, timezone
from collections import defaultdict

LOG_FILE   = os.path.join(os.path.dirname(__file__), "trade.log")
STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")
PROXY = {
    "https": "socks5://REDACTED_PROXY_CREDENTIALS",
    "http":  "socks5://REDACTED_PROXY_CREDENTIALS",
}

TEST_SYMBOLS = {"TEST","T2","T3","T4","T5","MEME2","HIGH","LOW","microduck"}

# symbol → contract address（从 state.json 自动补全）
def _load_addr_map():
    static = {
        "AI":       "0x2e8c31162b855a2ffa90f6f8634643ad6f111e18",
        "SHROOM":   "0xab093def657f15df31b33922a95e047add645b29",
        "MEME":     "0x385f4f8ae47651ce5f58f5265395a669f8281e18",
        "CAP":      "0x2F219C706E052Dc25372A0c59Dcc2afe0CaB12f3",
        "ROBINCAT": "0xded852de9fe9ba9b6f27f39e8e81cf851a5c79cc",
    }
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
        for addr, pos in state.get("positions", {}).items():
            sym = pos.get("symbol")
            if sym and sym not in static:
                static[sym] = addr
    except Exception:
        pass
    return static

SYMBOL_TO_ADDR = _load_addr_map()


# ──────────────────────────────────────────────
# 1. 链上真实交易（baw wallet tx-history）
# ──────────────────────────────────────────────

def _fetch_onchain_swaps(size=100):
    """
    从 baw wallet tx-history 获取所有 swap，
    返回列表：每条包含 {time, status, symbol, direction, amount, eth_recv/sent, tx_hash}
    """
    # 最大支持 100，分页获取更多
    max_per_page = min(size, 100)
    r = subprocess.run(
        ["baw", "wallet", "tx-history", "--json", f"--size={max_per_page}"],
        capture_output=True, text=True, timeout=30
    )
    d = json.loads(r.stdout)
    if not d.get("success"):
        return []
    raw_txs = d.get("data", {}).get("transactions", [])

    swaps = []
    for tx in raw_txs:
        if tx.get("txType") != "swap":
            continue
        status   = tx.get("status", "")
        tx_time  = tx.get("txTime", "")
        tx_hash  = tx.get("txHash", "")

        for item in tx.get("txHashList", []):
            inst  = item.get("instructions", {})
            sends = inst.get("send", [])
            recvs = inst.get("receive", [])

            stable_syms = {"ETH","WETH","BNB","WBNB","USDG","USDT","USDC"}

            # 卖出：send 是目标 token，receive 是 ETH/stable
            for s in sends:
                si  = s.get("tokenInfo", {})
                sym = si.get("symbol", "")
                if sym in stable_syms:
                    continue
                eth_recv = 0
                usd_recv = 0
                for rv in recvs:
                    ri = rv.get("tokenInfo", {})
                    if ri.get("symbol") in ("ETH", "WETH"):
                        eth_recv += float(rv.get("amount", 0))
                    elif ri.get("symbol") in ("USDG","USDT","USDC"):
                        usd_recv += float(rv.get("amount", 0))

                swaps.append({
                    "time":      tx_time,
                    "status":    status,
                    "symbol":    sym,
                    "direction": "SELL",
                    "amount":    float(s.get("amount", 0)),
                    "eth_recv":  eth_recv,
                    "usd_recv":  usd_recv,
                    "tx_hash":   tx_hash,
                    "addr":      si.get("contractAddress", "").lower(),
                })

            # 买入：send 是 ETH/stable，receive 是目标 token
            # 只有当所有 send 都是 stable/ETH 时才算买入
            all_sends_stable = all(s.get("tokenInfo",{}).get("symbol","") in stable_syms for s in sends)
            if all_sends_stable:
                for rv in recvs:
                    ri  = rv.get("tokenInfo", {})
                    sym = ri.get("symbol", "")
                    if sym in stable_syms:
                        continue
                    eth_sent = 0
                    usd_sent = 0
                    for s in sends:
                        si = s.get("tokenInfo", {})
                        if si.get("symbol") in ("ETH", "WETH"):
                            eth_sent += float(s.get("amount", 0))
                        elif si.get("symbol") in ("USDG","USDT","USDC"):
                            usd_sent += float(s.get("amount", 0))

                    swaps.append({
                        "time":      tx_time,
                        "status":    status,
                        "symbol":    sym,
                        "direction": "BUY",
                        "amount":    float(rv.get("amount", 0)),
                        "eth_sent":  eth_sent,
                        "usd_sent":  usd_sent,
                        "tx_hash":   tx_hash,
                        "addr":      ri.get("contractAddress", "").lower(),
                    })

    return swaps


# ──────────────────────────────────────────────
# 2. 从日志提取 KOL 触发事件
# ──────────────────────────────────────────────

def _parse_kol_triggers():
    """
    从 trade.log 提取 KOL 触发的 EXECUTE EXIT 记录，
    返回 [{ts, symbol, reason, kol, kol_usd}]
    """
    triggers = []
    with open(LOG_FILE) as f:
        for line in f:
            ts_m = re.match(r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]', line)
            if not ts_m:
                continue
            ts = ts_m.group(1)

            m = re.search(r'\[EXECUTE EXIT\]\s+(\w+)\s*\|\s*full_sell 100%\s*\|\s*(.+)', line)
            if not m:
                continue
            sym, reason = m.groups()
            reason = reason.strip()
            if sym in TEST_SYMBOLS:
                continue

            # 只要 KOL 触发
            is_kol = bool(re.search(r'大\s*KOL|KOL\s*卖出', reason))
            if not is_kol:
                continue

            kol_m   = re.search(r'@(\w+)', reason)
            usd_m   = re.search(r'\$([\d,]+)', reason)
            kol     = kol_m.group(1) if kol_m else "?"
            kol_usd = float(usd_m.group(1).replace(",","")) if usd_m else 0

            triggers.append({
                "ts":      ts,
                "symbol":  sym,
                "reason":  reason,
                "kol":     kol,
                "kol_usd": kol_usd,
            })
    return triggers


def _parse_open_trades():
    """从日志提取买入记录（OPEN + entry_mc）"""
    opens = {}  # symbol → [{ts, strategy, size, mc}]
    with open(LOG_FILE) as f:
        for line in f:
            ts_m = re.match(r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]', line)
            if not ts_m:
                continue
            ts = ts_m.group(1)
            m  = re.search(r'\[OPEN\]\s+(\w+)\s*\|\s*(\w+)\s*\|\s*([\d.]+)U\s*\|\s*mc=([\d,]+)', line)
            if not m:
                continue
            sym, strat, size, mc = m.groups()
            if sym in TEST_SYMBOLS:
                continue
            opens.setdefault(sym, []).append({
                "ts": ts, "strategy": strat,
                "size": float(size), "mc": float(mc.replace(",",""))
            })
    return opens


# ──────────────────────────────────────────────
# 3. 价格查询
# ──────────────────────────────────────────────

def _get_price_data(addr):
    if not addr:
        return None, {}
    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{addr}",
            proxies=PROXY, timeout=10
        )
        pairs = r.json().get("pairs") or []
        if not pairs:
            return None, {}
        p = sorted(pairs, key=lambda x: x.get("liquidity", {}).get("usd", 0) or 0, reverse=True)[0]
        price   = float(p.get("priceUsd") or 0)
        changes = {
            "1h":  float(p.get("priceChange", {}).get("h1") or 0),
            "6h":  float(p.get("priceChange", {}).get("h6") or 0),
            "24h": float(p.get("priceChange", {}).get("h24") or 0),
        }
        return price, changes
    except Exception:
        return None, {}


def _hours_since(ts_str):
    if not ts_str or ts_str in ("—","?"):
        return None
    try:
        # 支持 ISO 格式和日志格式
        if "T" in ts_str:
            dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        else:
            dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 3600
    except Exception:
        return None


# ──────────────────────────────────────────────
# 4. 配对：trigger → onchain sell
# ──────────────────────────────────────────────

def _match_trigger_to_sell(trigger, onchain_sells, tolerance_min=15):
    """
    在链上卖出记录里找匹配：
    trigger ts 附近 tolerance_min 分钟内、同 symbol 的 confirmed sell
    """
    sym = trigger["symbol"]
    ts  = trigger["ts"]  # "YYYY-MM-DD HH:MM:SS"

    def parse_ts(s):
        if "T" in s:
            s = s[:19].replace("T", " ")
        # 统一为 naive UTC datetime 再比较
        return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)

    t_dt = parse_ts(ts)
    best = None
    best_gap = float("inf")

    for sell in onchain_sells:
        if sell["symbol"] != sym:
            continue
        if sell["direction"] != "SELL":
            continue
        s_dt  = parse_ts(sell["time"])
        gap   = (s_dt - t_dt).total_seconds() / 60  # 分钟
        if -5 <= gap <= tolerance_min:  # 允许 trigger 之前 5 分钟（日志延迟）
            if abs(gap) < best_gap:
                best_gap = abs(gap)
                best     = sell

    return best


# ──────────────────────────────────────────────
# 5. 完整复盘
# ──────────────────────────────────────────────

def _get_eth_price():
    """获取当前 ETH 价格"""
    try:
        r = requests.get(
            "https://api.dexscreener.com/latest/dex/tokens/0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
            proxies=PROXY, timeout=8
        )
        pairs = r.json().get("pairs") or []
        eth_pairs = [p for p in pairs if p.get("baseToken", {}).get("symbol") == "ETH"
                     or "ETH" in p.get("baseToken", {}).get("symbol", "")]
        if eth_pairs:
            return float(eth_pairs[0].get("priceUsd") or 2450)
    except Exception:
        pass
    # fallback: 查 WETH on opBNB
    try:
        r = requests.get(
            "https://api.dexscreener.com/latest/dex/tokens/0x4200000000000000000000000000000000000006",
            proxies=PROXY, timeout=8
        )
        pairs = r.json().get("pairs") or []
        if pairs:
            p = sorted(pairs, key=lambda x: x.get("liquidity",{}).get("usd",0) or 0, reverse=True)[0]
            return float(p.get("priceUsd") or 2450)
    except Exception:
        pass
    return 2450  # last-resort


def review(push=False):
    print("📡 正在获取链上交易记录…")
    onchain = _fetch_onchain_swaps(size=100)
    onchain_sells = [s for s in onchain if s["direction"] == "SELL" and s["status"] == "confirmed"]

    print("📋 解析日志触发事件…")
    triggers = _parse_kol_triggers()
    opens    = _parse_open_trades()

    print("💱 获取 ETH 价格…")
    eth_price = _get_eth_price()

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = []
    lines.append("=" * 64)
    lines.append("📊 KOL 触发卖出系统复盘报告 v3（链上数据）")
    lines.append(f"生成时间: {now_str}")
    lines.append(f"ETH 参考价: ${eth_price:,.0f}")
    lines.append(f"KOL 触发事件: {len(triggers)} 笔")
    lines.append("=" * 64)

    correct = wrong = unclear = 0

    for trig in triggers:
        sym      = trig["symbol"]
        kol      = trig["kol"]
        kol_usd  = trig["kol_usd"]
        trig_ts  = trig["ts"]

        # 找对应的链上卖出
        sell_tx = _match_trigger_to_sell(trig, onchain_sells)

        # 找买入记录
        sym_opens = opens.get(sym, [])
        # 找触发之前最近的 OPEN
        before_opens = [o for o in sym_opens if o["ts"] <= trig_ts]
        open_rec = before_opens[-1] if before_opens else (sym_opens[-1] if sym_opens else None)

        # 当前价格
        addr = SYMBOL_TO_ADDR.get(sym)
        cur_price, changes = _get_price_data(addr)

        # 链上卖出信息
        if sell_tx:
            sell_eth_amt = sell_tx.get("eth_recv", 0)
            sell_usd     = sell_eth_amt * eth_price
            sell_status  = "✅ 链上确认成交"
            sell_hash    = sell_tx["tx_hash"][:20]
            sell_time    = sell_tx["time"][:16]

            # 计算真实 PnL
            if open_rec:
                cost_usd = open_rec["size"]
                pnl_usd  = sell_usd - cost_usd
                pnl_pct  = pnl_usd / cost_usd * 100
                pnl_str  = f"${pnl_usd:+.2f} ({pnl_pct:+.1f}%)"
                # ★ Rug 检测：亏损 >= 85% → 自动将 deployer 加入黑名单
                if pnl_pct <= -85:
                    try:
                        from token_filter import add_rug_deployer, get_token_deployer
                        token_ca = open_rec.get("token_addr", "")
                        if token_ca:
                            deployer = get_token_deployer(token_ca)
                            if deployer:
                                add_rug_deployer(
                                    deployer,
                                    reason=f"kol_exit_review: {open_rec.get('symbol','?')} 亏损 {pnl_pct:.1f}%"
                                )
                    except Exception:
                        pass
            else:
                pnl_str = "—（无买入记录）"
        else:
            sell_status = "❌ 链上未找到对应卖出（可能尚未成交或时间窗口外）"
            sell_hash   = "—"
            sell_time   = "—"
            pnl_str     = "—"

        # 决策评判
        hours = _hours_since(trig_ts)
        if hours is None:
            hours = 0

        if hours <= 2:
            chg = changes.get("1h", 0)
            window = "1h"
        elif hours <= 7:
            chg = changes.get("6h", 0)
            window = "6h"
        else:
            chg = changes.get("24h", 0)
            window = "24h"

        verdict = ""
        note    = ""
        if cur_price is None:
            verdict = "❓ 无价格数据"
            unclear += 1
        elif sell_tx:
            # 已成功卖出，判断是否正确
            if chg <= -5:
                verdict = "✅ 正确止损"
                note    = f"卖出后{window}跌 {chg:.1f}%，离场时机准确"
                correct += 1
            elif chg >= 8:
                verdict = "❌ 过早离场"
                note    = f"卖出后{window}涨 {chg:.1f}%，可能过早"
                wrong += 1
            else:
                verdict = "➖ 方向持平"
                note    = f"{window}变化 {chg:.1f}%，趋势未明"
                unclear += 1
        else:
            verdict = "❓ 无链上成交"
            unclear += 1

        hours_ago = f"{hours:.1f}h 前" if hours else "刚刚"
        lines.append(f"\n🪙 {sym}  [{hours_ago}]")
        if open_rec:
            lines.append(f"   入场: {open_rec['ts']}  策略={open_rec['strategy']}  mc=${open_rec['mc']:,.0f}  成本=${open_rec['size']:.1f}")
        else:
            lines.append(f"   入场: 日志无记录")
        lines.append(f"   触发: @{kol} 卖出 ${kol_usd:,.0f}  ({trig_ts})")
        lines.append(f"   链上: {sell_status}")
        if sell_tx:
            lines.append(f"   卖出: {sell_time}  ETH={sell_tx.get('eth_recv',0):.6f}  ≈${sell_tx.get('eth_recv',0)*eth_price:.2f}")
            lines.append(f"   TxHash: {sell_tx['tx_hash']}")
            lines.append(f"   实际 PnL: {pnl_str}")
        if cur_price:
            chg_str = "  ".join(f"{k}:{v:+.1f}%" for k,v in changes.items() if v != 0)
            lines.append(f"   当前价: ${cur_price:.8f}  {chg_str}")
        lines.append(f"   决策评价: {verdict}{'  ' + note if note else ''}")

    # 统计
    total = len(triggers)
    lines.append("\n" + "=" * 64)
    lines.append("📈 KOL 跟单止损 — 决策质量统计")
    if total > 0:
        lines.append(f"   ✅ 正确止损: {correct}/{total} ({correct/total:.0%})")
        lines.append(f"   ❌ 过早离场: {wrong}/{total}  ({wrong/total:.0%})")
        lines.append(f"   ❓ 不确定:   {unclear}/{total} ({unclear/total:.0%})")

        eff = correct + wrong
        if eff >= 2:
            lines.append(f"\n   有效胜率（已判断样本）: {correct}/{eff} = {correct/eff:.0%}")
        if total >= 3:
            if correct / total >= 0.6:
                lines.append("   → ✅ KOL 卖出信号整体有效，建议保留跟单逻辑")
            elif wrong / total >= 0.5:
                lines.append("   → ⚠️ 过早离场偏多，建议提高 KOL 卖出金额阈值")
            else:
                lines.append("   → 📊 样本不足，继续积累（目标 ≥10 笔）")
        else:
            lines.append("   → 📊 样本偏少（<3笔），继续积累数据")

    # 附：其他链上卖出汇总
    non_kol_symbols = set()
    for trig in triggers:
        non_kol_symbols.add(trig["symbol"])

    other_sells = [s for s in onchain_sells
                   if s["symbol"] not in non_kol_symbols
                   and s["symbol"] not in TEST_SYMBOLS
                   and s.get("eth_recv", 0) > 0]

    if other_sells:
        lines.append("\n" + "=" * 64)
        lines.append("📋 其他链上卖出记录（非 KOL 触发）")
        sym_groups = defaultdict(list)
        for s in other_sells:
            sym_groups[s["symbol"]].append(s)
        for sym, sells in sorted(sym_groups.items()):
            total_eth = sum(s.get("eth_recv",0) for s in sells)
            total_usd = total_eth * eth_price
            times     = [s["time"][:16] for s in sells]
            lines.append(f"   {sym:<10} {len(sells)}笔  合计 ETH={total_eth:.6f}  ≈${total_usd:.2f}  最近={times[0]}")

    lines.append("=" * 64)

    report = "\n".join(lines)
    print(report)

    if push:
        summary = "\n".join(lines[:50]) + ("\n…（更多见终端）" if len(lines) > 50 else "")
        subprocess.run([
            "openclaw", "message", "send",
            "--channel", "jarvis",
            "--to", "432401404",
            "--message", summary,
        ], capture_output=True)

    return report


if __name__ == "__main__":
    push = "--push" in sys.argv
    review(push=push)
