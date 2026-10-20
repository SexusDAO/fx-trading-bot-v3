#!/usr/bin/env python3
"""
refresh_watchlist.py
从币安 PnL 排行榜自动刷新 evm_watchlist.json 里的聪明钱地址

筛选条件：30天 PnL > 50,000 且 胜率 > 35%
每次运行只追加新地址，不删除现有地址（避免误删手动维护的地址）
"""
import json, subprocess, sys, os
from datetime import datetime, timezone

WATCHLIST_PATH = os.path.join(os.path.dirname(__file__), "evm_watchlist.json")
MIN_PNL = 50_000
MIN_WIN_RATE = 0.35
PERIOD = "30d"
SIZE = 50  # 多拉一些

def fetch_pnl_leaderboard(period=PERIOD):
    result = subprocess.run(
        ["binance-pro-cli", "web3", "rank", "pnl",
         "--chain", "204", "--period", period,
         "--sort-by", "0", "--size", str(SIZE),
         "--no-compact", "--format", "json"],
        capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0:
        raise RuntimeError(f"CLI error: {result.stderr}")
    return json.loads(result.stdout)

def main():
    # 加载现有 watchlist
    with open(WATCHLIST_PATH) as f:
        existing = json.load(f)
    existing_addrs = {x["evm"].lower() for x in existing}

    # 拉取排行榜（7d + 30d 合并去重）
    data7 = fetch_pnl_leaderboard("7d")
    data30 = fetch_pnl_leaderboard("30d")
    traders_7d = {t["address"].lower(): t for t in data7["data"]["data"]}
    traders_30d = {t["address"].lower(): t for t in data30["data"]["data"]}

    # 合并候选：7d PnL>30k WR>38% 或 30d PnL>80k WR>40%
    candidates = {}
    for addr, t in traders_7d.items():
        if float(t["realizedPnl"]) > 30000 and float(t["winRate"]) > 0.38:
            candidates[addr] = t
    for addr, t in traders_30d.items():
        if float(t["realizedPnl"]) > MIN_PNL and float(t["winRate"]) > MIN_WIN_RATE:
            if addr not in candidates:
                candidates[addr] = t
    traders = list(candidates.values())

    # 筛选
    added = []
    for t in traders:
        addr = t["address"].lower()
        pnl = float(t["realizedPnl"])
        win_rate = float(t["winRate"])
        label = t.get("addressLabel") or ""
        tags = t.get("tags") or []

        if addr in existing_addrs:
            continue

        # 生成 handle
        if label:
            handle = f"RH_{label[:18]}"
        else:
            tag = "KOL" if "KOL" in tags else "SM"
            handle = f"RH_{tag}_{addr[2:8]}"

        existing.append({"evm": addr, "handle": handle})
        existing_addrs.add(addr)
        added.append((addr, handle, pnl, win_rate))

    # 保存
    with open(WATCHLIST_PATH, "w") as f:
        json.dump(existing, f, indent=2, ensure_ascii=False)

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if added:
        lines = [f"✅ [{ts}] 聪明钱名单更新 +{len(added)} 个地址 (总计 {len(existing)} 个)"]
        for addr, handle, pnl, wr in added:
            lines.append(f"  + {addr[:16]}... {handle} PnL=${pnl:,.0f} WR={wr:.1%}")
        print("\n".join(lines))
    else:
        print(f"[{ts}] 聪明钱名单无新增（当前 {len(existing)} 个地址）")

if __name__ == "__main__":
    main()
