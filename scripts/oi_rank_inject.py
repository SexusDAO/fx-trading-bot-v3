#!/usr/bin/env python3
"""
oi_rank_inject.py — OI 增幅榜注入（替代 Ru7 爬虫）

数据源：Binance fapi openInterestHist（公开接口，无需 API Key，无速率限制）
逻辑：
  1. 读取当前候选池（paper_watchlist.json）+ Binance 全市场 USDT 永续合约
  2. 并发拉取各标的最近 2 小时 OI 数据，计算 1h OI 增幅
  3. 筛选：OI 4h 增幅 > OI_MIN_CHG% 且绝对 OI > OI_MIN_ABS_M（百万 U）
  4. 按增幅降序，写入 ws_cache/ru7_inject.json（保持原有消费格式）
  5. 通知策略：全部静默，由 paper_scanner 主动通知

执行方式：
  python3 scripts/oi_rank_inject.py
  openclaw cron（每4小时，替代原 ru7-monitor）
"""

import json
import pathlib
import datetime
import concurrent.futures
import time
import sys

import requests

# ── 配置 ─────────────────────────────────────────────────────────────────────
WORKSPACE     = pathlib.Path("/root/.openclaw/workspace")
INJECT_FILE   = WORKSPACE / "ws_cache" / "ru7_inject.json"
WATCHLIST_FILE = WORKSPACE / "paper_watchlist.json"

PROXY         = "socks5://REDACTED_PROXY_CREDENTIALS"
FAPI_BASE     = "https://fapi.binance.com"

OI_PERIOD     = "4h"    # OI 窗口：4h 比 1h 更能反映主力建仓趋势
OI_MIN_CHG    = 3.0     # OI 4h 增幅阈值（%）：3% 以上视为明显流入
OI_MIN_ABS_M  = 5.0     # OI 绝对值下限（百万U）：5M 覆盖中小山寨
TOP_N         = 20      # 最多注入候选池条数
MAX_WORKERS   = 15      # 并发线程数
TIMEOUT_S     = 6       # 单个请求超时秒数

PROXIES = {
    "http":  PROXY,
    "https": PROXY,
}

# ── 工具函数 ──────────────────────────────────────────────────────────────────
def log(msg: str):
    ts = datetime.datetime.utcnow().strftime("%H:%M:%S")
    print(f"[oi_rank {ts}] {msg}", flush=True)


def _get_all_usdt_symbols() -> list[str]:
    """从 Binance fapi 获取所有 USDT 永续合约（排除季度合约）"""
    try:
        r = requests.get(
            f"{FAPI_BASE}/fapi/v1/exchangeInfo",
            proxies=PROXIES, timeout=10
        )
        r.raise_for_status()
        data = r.json()
        syms = [
            s["symbol"] for s in data.get("symbols", [])
            if s["symbol"].endswith("USDT")
            and s.get("contractType") == "PERPETUAL"
            and s.get("status") == "TRADING"
        ]
        return syms
    except Exception as e:
        log(f"获取全市场标的失败: {e}")
        return []


def _get_watchlist_symbols() -> list[str]:
    """读取 paper_watchlist.json 里的标的"""
    try:
        if WATCHLIST_FILE.exists():
            d = json.loads(WATCHLIST_FILE.read_text())
            return [k for k in d if k not in ("candidates", "last_updated") and isinstance(d[k], dict)]
    except Exception:
        pass
    return []


def _fetch_oi_chg(symbol: str) -> dict | None:
    """
    拉取单个标的最近 2 根 1h OI，计算增幅。
    返回 {"sym": ..., "oi_chg_pct": ..., "oi_usd_m": ...} 或 None
    """
    try:
        r = requests.get(
            f"{FAPI_BASE}/futures/data/openInterestHist",
            params={"symbol": symbol, "period": OI_PERIOD, "limit": 2},
            proxies=PROXIES,
            timeout=TIMEOUT_S,
        )
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list) or len(data) < 2:
            return None
        oi_old = float(data[0]["sumOpenInterestValue"])
        oi_new = float(data[1]["sumOpenInterestValue"])
        if oi_old < 1e-6:
            return None
        chg_pct = (oi_new - oi_old) / oi_old * 100
        oi_usd_m = oi_new / 1_000_000
        return {"sym": symbol, "oi_chg_pct": round(chg_pct, 2), "oi_usd_m": round(oi_usd_m, 2)}
    except Exception:
        return None


def _build_candidates(symbols: list[str]) -> list[dict]:
    """
    并发拉取所有标的 OI，筛选并排序。
    返回按增幅降序的列表。
    """
    log(f"并发拉取 {len(symbols)} 个标的 OI …")
    t0 = time.time()
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(_fetch_oi_chg, sym): sym for sym in symbols}
        for f in concurrent.futures.as_completed(futures):
            r = f.result()
            if r and r["oi_chg_pct"] >= OI_MIN_CHG and r["oi_usd_m"] >= OI_MIN_ABS_M:
                results.append(r)
    elapsed = time.time() - t0
    results.sort(key=lambda x: -x["oi_chg_pct"])
    log(f"拉取完成，耗时 {elapsed:.1f}s，筛出 {len(results)} 个标的（{OI_PERIOD} 增幅≥{OI_MIN_CHG}% 且 OI≥{OI_MIN_ABS_M}M）")
    return results


def main():
    # 1. 获取候选池（watchlist 优先，补全全市场）
    wl_syms   = _get_watchlist_symbols()
    all_syms  = _get_all_usdt_symbols()
    # 合并去重，watchlist 优先（已知活跃标的）
    symbol_set = list(dict.fromkeys(wl_syms + all_syms))
    log(f"候选池: watchlist {len(wl_syms)} + fapi全市场 {len(all_syms)} → 去重后 {len(symbol_set)} 个")

    if not symbol_set:
        log("候选池为空，退出")
        return

    # 2. 批量拉 OI
    candidates = _build_candidates(symbol_set)

    # 3. 截取 Top N
    top = candidates[:TOP_N]
    ts_str = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    # 4. 读取现有 inject 文件（保留未消费条目）
    existing = {}
    try:
        if INJECT_FILE.exists():
            existing = json.loads(INJECT_FILE.read_text())
    except Exception:
        pass

    # 保留之前未消费的条目（避免覆盖尚未被 scanner 处理的信号）
    prev_unconsumed = [e for e in existing.get("oi_gainers", []) if not e.get("consumed")]

    # 新条目去重（不重复添加已有未消费的标的）
    prev_syms = {e["sym"] for e in prev_unconsumed}
    new_entries = [
        {
            "sym":        r["sym"],
            "oi_chg_pct": r["oi_chg_pct"],
            "oi_usd_m":   r["oi_usd_m"],
            "source":     f"binance_fapi_{OI_PERIOD}",
            "consumed":   False,
        }
        for r in top if r["sym"] not in prev_syms
    ]

    merged = prev_unconsumed + new_entries

    # 5. 写入
    INJECT_FILE.parent.mkdir(parents=True, exist_ok=True)
    out = {
        "updated":     ts_str,
        "oi_gainers":  merged,
        "market_maker": existing.get("market_maker", []),   # 保留原字段（现为空）
    }
    INJECT_FILE.write_text(json.dumps(out, indent=2, ensure_ascii=False))

    if new_entries:
        log(f"注入 {len(new_entries)} 个新标的: {[e['sym'] for e in new_entries]}")
        for e in new_entries[:5]:
            log(f"  {e['sym']:12s}  OI={e['oi_usd_m']:7.1f}M  chg={e['oi_chg_pct']:+.1f}%")
    else:
        log("无新增 OI 异动标的（保留 prev_unconsumed）")


if __name__ == "__main__":
    main()
