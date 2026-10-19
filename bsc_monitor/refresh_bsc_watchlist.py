# ============================================================
# refresh_bsc_watchlist.py — 构建 BSC 聪明钱名单
# 复用 fomo_workflow/refresh_watchlist.py 的逻辑，改 chain 56
# 输出: bsc_monitor/smart_watchlist.json
# 每日由 cron 调用（bsc-smart-watchlist-refresh）
# ============================================================
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

OUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "smart_watchlist.json")
CHAIN = "56"          # BSC
SIZE = 150            # 拉更多（80→150）
MIN_PNL = 20_000      # 降低门槛（50k→20k）
MIN_WIN_RATE = 0.30   # 降低门槛（0.35→0.30）


def fetch_pnl_leaderboard(period: str):
    result = subprocess.run(
        ["binance-pro-cli", "web3", "rank", "pnl",
         "--chain", CHAIN, "--period", period,
         "--sort-by", "0", "--size", str(SIZE),
         "--no-compact", "--format", "json"],
        capture_output=True, text=True, timeout=40,
    )
    if result.returncode != 0:
        raise RuntimeError(f"CLI error: {result.stderr}")
    return json.loads(result.stdout)


def main():
    data1 = fetch_pnl_leaderboard("1d")
    data7 = fetch_pnl_leaderboard("7d")
    data30 = fetch_pnl_leaderboard("30d")
    traders_1d = {t["address"].lower(): t for t in (data1.get("data", {}).get("data") or [])}
    traders_7d = {t["address"].lower(): t for t in (data7.get("data", {}).get("data") or [])}
    traders_30d = {t["address"].lower(): t for t in (data30.get("data", {}).get("data") or [])}

    # 合并候选：1d PnL>5k WR>35% 或 7d PnL>15k WR>35% 或 30d PnL>20k WR>30%
    candidates = {}
    for addr, t in traders_1d.items():
        try:
            if float(t["realizedPnl"]) > 5_000 and float(t["winRate"]) > 0.35:
                candidates[addr] = t
        except (KeyError, ValueError, TypeError):
            continue
    for addr, t in traders_7d.items():
        try:
            if float(t["realizedPnl"]) > 15_000 and float(t["winRate"]) > 0.35:
                candidates[addr] = t
        except (KeyError, ValueError, TypeError):
            continue
    for addr, t in traders_30d.items():
        try:
            if float(t["realizedPnl"]) > MIN_PNL and float(t["winRate"]) > MIN_WIN_RATE:
                if addr not in candidates:
                    candidates[addr] = t
        except (KeyError, ValueError, TypeError):
            continue

    out = []
    for addr, t in candidates.items():
        out.append({
            "evm": addr,
            "pnl_30d": float(t.get("realizedPnl") or 0),
            "win_rate": float(t.get("winRate") or 0),
            "label": t.get("addressLabel") or "",
            "tags": t.get("tags") or [],
            "updated": datetime.now(timezone.utc).isoformat(),
        })

    json.dump(out, open(OUT_PATH, "w"), ensure_ascii=False, indent=1)
    print(f"[OK] BSC smart watchlist: {len(out)} traders → {OUT_PATH}")


if __name__ == "__main__":
    main()
