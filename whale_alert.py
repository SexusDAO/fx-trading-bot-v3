#!/usr/bin/env python3
"""
巨鲸买入提醒脚本
- 每次运行抓取最新聪明钱信号（BSC + Solana）
- 对比上次状态，发现新信号时生成完整报告
- 报告包括：代币名/合约地址、买入价/当前价/最大涨幅、触发时间、
  聪明钱包数量、Top钱包胜率盈亏分析
"""

import json
import subprocess
import sys
import os
from datetime import datetime, timezone

STATE_FILE = "/root/.openclaw/workspace/.whale_alert_state.json"


def run_cli(cmd):
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
    try:
        return json.loads(result.stdout)
    except Exception:
        return None


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"seen_signal_ids": []}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def ts_to_str(ts_ms):
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def format_mc(mc_str):
    try:
        mc = float(mc_str)
        if mc >= 1_000_000:
            return f"${mc/1_000_000:.2f}M"
        elif mc >= 1000:
            return f"${mc/1000:.1f}K"
        else:
            return f"${mc:.2f}"
    except Exception:
        return str(mc_str)


def get_wallet_pnl_summary(address, chain="56"):
    """拿Top钱包7日PnL和胜率"""
    data = run_cli(f"binance-pro-cli web3 address {address} --chain {chain} 2>/dev/null")
    if not data or not data.get("success"):
        return None
    return data.get("data")


def get_pnl_leaderboard(chain="56"):
    """拿7日PnL排行榜中的聪明钱包"""
    data = run_cli(f"binance-pro-cli web3 rank pnl --chain {chain} --period 7d 2>/dev/null")
    if not data or not data.get("success"):
        return []
    inner = data.get("data", {})
    if isinstance(inner, dict):
        return inner.get("data", [])
    return inner or []


def build_report(signal, pnl_wallets):
    """生成单个信号的完整报告"""
    ticker = signal.get("ticker", "?")
    ca = signal.get("contractAddress", "N/A")
    chain_id = signal.get("chainId", "56")
    smart_count = signal.get("smartMoneyCount", 0)
    trigger_ts = signal.get("signalTriggerTime", 0)
    alert_price = float(signal.get("alertPrice", 0))
    current_price = float(signal.get("currentPrice", 0))
    max_gain = float(signal.get("maxGain", 0))
    status = signal.get("status", "valid")

    trigger_time = ts_to_str(trigger_ts) if trigger_ts else "N/A"

    # 价格变化
    if alert_price > 0:
        price_change_pct = (current_price - alert_price) / alert_price * 100
    else:
        price_change_pct = 0.0

    # 从PnL排行榜找相关聪明钱包（简单取前3个作为代表）
    top_wallets = pnl_wallets[:3] if pnl_wallets else []

    chain_name = "BSC" if chain_id == "56" else ("Solana" if chain_id == "CT_501" else f"Chain {chain_id}")

    status_emoji = "✅" if status == "valid" else "⏳"

    lines = []
    lines.append(f"🐋 **巨鲸买入提醒** {status_emoji}")
    lines.append(f"━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"🪙 **代币**: {ticker} ({chain_name})")
    lines.append(f"📋 **合约地址**: `{ca}`")
    lines.append(f"⏰ **触发时间**: {trigger_time}")
    lines.append(f"👥 **聪明钱包数**: {smart_count} 个同时买入")
    lines.append(f"")
    lines.append(f"💰 **价格信息**")
    lines.append(f"  • 买入价: ${alert_price:.8f}")
    lines.append(f"  • 当前价: ${current_price:.8f}")
    price_arrow = "🟢" if price_change_pct >= 0 else "🔴"
    lines.append(f"  • 涨跌幅: {price_arrow} {price_change_pct:+.2f}%")
    lines.append(f"  • 最大涨幅: 🚀 +{max_gain:.2f}%")
    lines.append(f"")

    if top_wallets:
        lines.append(f"📊 **聪明钱包胜率分析** (7日排行 Top 3)")
        lines.append(f"{'地址':<20} {'标签':<12} {'胜率':>6} {'7d PnL':>12} {'总交易':>6}")
        lines.append(f"{'─'*60}")
        for w in top_wallets:
            addr = w.get("address", "")
            short_addr = addr[:6] + "..." + addr[-4:] if addr else "N/A"
            label = w.get("addressLabel", "") or "匿名"
            label = label[:10]
            win_rate = float(w.get("winRate", 0)) * 100
            pnl = float(w.get("realizedPnl", 0))
            pnl_str = f"+${pnl/1000:.1f}K" if pnl >= 0 else f"-${abs(pnl)/1000:.1f}K"
            tx_cnt = w.get("totalTxCnt", 0)
            lines.append(f"`{short_addr}` {label:<12} {win_rate:>5.1f}% {pnl_str:>10} {tx_cnt:>6}笔")
        lines.append(f"")

    lines.append(f"⚠️ _仅供参考，不构成投资建议_")

    return "\n".join(lines)


def main():
    state = load_state()
    seen_ids = set(state.get("seen_signal_ids", []))

    # 获取BSC信号
    sig_data = run_cli("binance-pro-cli web3 signal --chain 56 2>/dev/null")
    signals = []
    if sig_data and sig_data.get("success"):
        signals = sig_data.get("data", [])

    # 获取Solana信号
    sol_data = run_cli("binance-pro-cli web3 signal --chain CT_501 2>/dev/null")
    if sol_data and sol_data.get("success"):
        signals += sol_data.get("data", [])

    if not signals:
        print("NO_NEW_SIGNALS")
        return

    # 只取新信号
    new_signals = [s for s in signals if s.get("signalId") not in seen_ids]

    if not new_signals:
        print("NO_NEW_SIGNALS")
        return

    # 获取PnL排行榜（一次性，供所有信号复用）
    pnl_wallets = get_pnl_leaderboard("56")

    reports = []
    new_ids = []
    for signal in new_signals[:5]:  # 每次最多5条，避免刷屏
        report = build_report(signal, pnl_wallets)
        reports.append(report)
        new_ids.append(signal.get("signalId"))

    # 更新状态（保留最近200个已见ID，防止文件无限增长）
    all_ids = list(seen_ids) + new_ids
    state["seen_signal_ids"] = all_ids[-200:]
    save_state(state)

    # 输出报告（每个报告之间加分隔符）
    print("\n\n".join(reports))


if __name__ == "__main__":
    main()
