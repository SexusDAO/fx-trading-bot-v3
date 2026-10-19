#!/usr/bin/env python3
"""Fetch smart money / whale signals from Binance Web3 and format for chat push."""
import json
import urllib.request
import time
from datetime import datetime, timezone

API_URL = "https://web3.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/web/signal/smart-money/ai"
HEADERS = {
    "Content-Type": "application/json",
    "Accept-Encoding": "identity",
    "User-Agent": "binance-web3/1.1 (Skill)",
}

CHAINS = [
    ("CT_501", "Solana"),
    ("56", "BSC"),
]

def fetch_signals(chain_id, page=1, page_size=30):
    body = json.dumps({
        "smartSignalType": "",
        "page": page,
        "pageSize": page_size,
        "chainId": chain_id
    }).encode()
    req = urllib.request.Request(API_URL, data=body, headers=HEADERS, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        result = json.loads(resp.read())
    return result.get("data", []) or []

def format_price(p):
    try:
        f = float(p)
        if f == 0:
            return "0"
        if f < 0.0001:
            return f"{f:.10f}".rstrip('0').rstrip('.')
        return f"{f:.6f}".rstrip('0').rstrip('.')
    except Exception:
        return str(p)

def format_pct(p):
    try:
        f = float(p)
        if abs(f) >= 100:
            return f"{f:.1f}%"
        return f"{f:.2f}%"
    except Exception:
        return str(p) + "%"

def format_mc(mc):
    try:
        f = float(mc)
        if f >= 1e6:
            return f"${f/1e6:.2f}M"
        if f >= 1e3:
            return f"${f/1e3:.1f}K"
        return f"${f:.0f}"
    except Exception:
        return str(mc)

def time_ago(ts_ms):
    now = int(time.time() * 1000)
    diff = now - ts_ms
    if diff < 0:
        return "just now"
    mins = diff // 60000
    if mins < 1:
        return "just now"
    if mins < 60:
        return f"{mins}m ago"
    hours = mins // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"

def get_tags(signal):
    tags = []
    token_tag = signal.get("tokenTag", {}) or {}
    for category, tag_list in token_tag.items():
        for t in (tag_list or []):
            name = t.get("tagName", "")
            if name:
                tags.append(name)
    return tags

def format_signals():
    all_lines = []
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    all_lines.append(f"🐋 **Smart Money / Whale Signals** ({now_str})")
    all_lines.append("")

    for chain_id, chain_name in CHAINS:
        try:
            signals = fetch_signals(chain_id)
        except Exception as e:
            all_lines.append(f"**{chain_name}** — fetch failed: {e}")
            all_lines.append("")
            continue

        # Filter: only active/valid signals, prioritize buy signals
        active = [s for s in signals if s.get("status") in ("valid", "active")]
        # Sort by smartMoneyCount desc, then by signalTriggerTime desc
        active.sort(key=lambda s: (s.get("smartMoneyCount", 0), s.get("signalTriggerTime", 0)), reverse=True)

        all_lines.append(f"**{chain_name}** ({len(active)} active signals)")
        if not active:
            all_lines.append("  No active signals right now.")
            all_lines.append("")
            continue

        for s in active[:8]:  # Top 8 per chain
            ticker = s.get("ticker", "?")
            direction = s.get("direction", "?")
            sm_count = s.get("smartMoneyCount", 0)
            alert_price = format_price(s.get("alertPrice", "0"))
            cur_price = format_price(s.get("currentPrice", "0"))
            max_gain = format_pct(s.get("maxGain", "0"))
            cur_mc = format_mc(s.get("currentMarketCap", "0"))
            exit_rate = s.get("exitRate", 0)
            sig_type = s.get("smartSignalType", "")
            trigger_time = time_ago(s.get("signalTriggerTime", 0))
            tags = get_tags(s)
            mark = s.get("mark", "")

            # Direction emoji
            dir_emoji = "🟢" if direction == "buy" else "🔴"

            # Signal type label
            type_label = "Smart Money" if sig_type == "SMART_MONEY" else "KOL" if sig_type == "SMART_KOL" else sig_type

            line = f"  {dir_emoji} **{ticker}** | {type_label} x{sm_count} | {direction.upper()} | {trigger_time}"
            line += f"\n    Trigger: ${alert_price} → Now: ${cur_price} | Max Gain: {max_gain} | MCap: {cur_mc}"
            line += f"\n    Exit Rate: {exit_rate}% | Signal Count: {s.get('signalCount', 0)}"

            if mark == "risk":
                line += " | ⚠️ RISK"

            tag_str = ", ".join(tags[:4])
            if tag_str:
                line += f"\n    Tags: {tag_str}"

            all_lines.append(line)
            all_lines.append("")

        all_lines.append("")

    all_lines.append("Source: Binance Web3 Smart Money Signals (Solana + BSC)")
    return "\n".join(all_lines)

if __name__ == "__main__":
    output = format_signals()
    print(output)
