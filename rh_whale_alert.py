#!/usr/bin/env python3
"""
rh_whale_alert.py — Robinhood Chain KOL共振买入提醒 v3
触发条件: 3分钟内 >3个不同KOL买入同一个币
每次只推一个最强信号（按3min流入排序）
"""

import json, os, time, requests
from datetime import datetime, timezone

STATE_FILE = "/root/.openclaw/workspace/.rh_whale_state.json"
PROXY = {
    "https": "socks5://REDACTED_PROXY_CREDENTIALS",
    "http":  "socks5://REDACTED_PROXY_CREDENTIALS",
}
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://robinhoodtrenches.com/"}

WINDOW_SEC = 180   # 3分钟
MIN_BUYERS = 3     # >3个不同KOL
MIN_USD_IN = 3000


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"seen_tokens": []}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def fmt_usd(v):
    try:
        v = float(v)
        if v >= 1_000_000: return f"${v/1_000_000:.1f}M"
        if v >= 1_000:     return f"${v/1_000:.0f}K"
        return f"${v:.0f}"
    except: return "N/A"


def fmt_mc(v):
    if v is None: return "N/A"
    try:
        v = float(v)
        if v >= 1_000_000: return f"${v/1_000_000:.2f}M"
        if v >= 1_000:     return f"${v/1_000:.0f}K"
        return f"${v:.0f}"
    except: return "N/A"


def time_ago(ts):
    s = time.time() - ts
    if s < 60: return f"{s:.0f}秒前"
    if s < 3600: return f"{s/60:.0f}分钟前"
    return f"{s/3600:.1f}小时前"


def get_tokens():
    r = requests.get("https://robinhoodtrenches.com/api/tokens",
                     headers=HEADERS, proxies=PROXY, timeout=15)
    r.raise_for_status()
    return r.json()


def get_token_detail(addr):
    r = requests.get(f"https://robinhoodtrenches.com/api/token/{addr}",
                     headers=HEADERS, proxies=PROXY, timeout=15)
    r.raise_for_status()
    return r.json()


def check_3min_resonance(trades, window=WINDOW_SEC):
    now = time.time()
    cutoff = now - window
    recent = [t for t in trades if t.get("side") == "buy" and t.get("ts", 0) >= cutoff]
    seen_wallets = {}
    for t in sorted(recent, key=lambda x: x.get("ts", 0)):
        w = t.get("wallet", "").lower()
        if w and w not in seen_wallets:
            seen_wallets[w] = t
        elif w:
            seen_wallets[w]["usd"] = seen_wallets[w].get("usd", 0) + t.get("usd", 0)
    unique_buyers = list(seen_wallets.values())
    total_usd = sum(t.get("usd", 0) for t in recent)
    if len(unique_buyers) > MIN_BUYERS:
        return unique_buyers, total_usd
    return [], 0


def build_report(token, buyers_3min, total_3min):
    symbol  = token.get("symbol", "?")
    addr    = token.get("token", "N/A")
    mcap    = token.get("mcap")
    change24 = token.get("change24")
    pair_url = token.get("pair_url", "")
    n       = len(buyers_3min)

    tss = [b.get("ts", 0) for b in buyers_3min if b.get("ts")]
    first_ts = min(tss) if tss else 0
    last_ts  = max(tss) if tss else 0

    chg_str = f"{change24:+.0f}%" if change24 is not None else "—"

    lines = []
    lines.append(f"🐋 **{symbol}**  ·  {n}人/{int(WINDOW_SEC//60)}min共振")
    lines.append(f"")
    lines.append(f"📋 `{addr}`")
    lines.append(f"💰 流入 **{fmt_usd(total_3min)}**  |  MC **{fmt_mc(mcap)}**  |  {chg_str}")
    lines.append(f"⏱ {time_ago(first_ts)} ~ {time_ago(last_ts)}")
    if pair_url:
        lines.append(f"🔗 {pair_url}")
    return "\n".join(lines)


def main():
    state = load_state()
    seen  = set(state.get("seen_tokens", []))

    tokens = get_tokens()

    candidates = []

    for token in tokens:
        addr = token.get("token", "")
        if not addr: continue
        if token.get("honeypot") or token.get("drained"):
            seen.add(addr)
            continue
        if token.get("buyers", 0) <= MIN_BUYERS: continue
        if addr in seen: continue

        try:
            detail = get_token_detail(addr)
        except Exception:
            continue

        buyers_3min, total_3min = check_3min_resonance(detail.get("trades", []))

        if buyers_3min and total_3min >= MIN_USD_IN:
            candidates.append((token, buyers_3min, total_3min))

    # 所有扫描过的（未触发的）标为已见
    for token in tokens:
        a = token.get("token", "")
        if a and a not in [c[0].get("token") for c in candidates]:
            seen.add(a)

    if not candidates:
        print("NO_NEW_SIGNALS")
        state["seen_tokens"] = list(seen)[-500:]
        save_state(state)
        return

    # 只推最强一个（3min流入最大）
    candidates.sort(key=lambda x: x[2], reverse=True)
    token, buyers_3min, total_3min = candidates[0]

    print(build_report(token, buyers_3min, total_3min))

    # 标记已推送
    seen.add(token.get("token", ""))
    state["seen_tokens"] = list(seen)[-500:]
    save_state(state)


if __name__ == "__main__":
    main()
