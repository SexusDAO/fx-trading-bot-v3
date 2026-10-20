"""
Watchlist — 延迟复查机制

当代币流动性太低被跳过时，存入 watchlist。
5 分钟后复查：如果流动性增长或持有人增长，重新评估。
"""

import json
import time
import logging
import os

logger = logging.getLogger("arc.watchlist")

WATCHLIST_FILE = os.path.join(os.path.dirname(__file__), ".watchlist.json")
RECHECK_DELAY = 300  # 5 分钟后复查


def load_watchlist():
    if os.path.exists(WATCHLIST_FILE):
        try:
            with open(WATCHLIST_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def save_watchlist(data):
    try:
        with open(WATCHLIST_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.debug(f"[Watchlist] 保存失败: {e}")


def add_token(token_info):
    """添加代币到 watchlist，等待复查。"""
    addr = token_info.get("token_address", "")
    if not addr:
        return
    wl = load_watchlist()
    entry = {
        "symbol": token_info.get("symbol", ""),
        "token_address": addr,
        "pair_address": token_info.get("pair_address", ""),
        "added_at": time.time(),
        "initial_liquidity": token_info.get("liquidity_usd", 0),
        "initial_holders": token_info.get("holder_count", 0),
        "block": token_info.get("creation_block", 0),
    }
    wl[addr.lower()] = entry
    save_watchlist(wl)
    logger.info(f"[Watchlist] 📌 添加 {entry['symbol']} (${entry['initial_liquidity']:.0f} liq, {entry['initial_holders']} holders) → 5min后复查")


def get_ready_tokens():
    """返回已到复查时间的代币列表。"""
    wl = load_watchlist()
    now = time.time()
    ready = []
    for addr, entry in list(wl.items()):
        if now - entry["added_at"] >= RECHECK_DELAY:
            ready.append(entry)
            del wl[addr]
    if ready:
        save_watchlist(wl)
    return ready


def remove_token(addr):
    """手动移除代币。"""
    wl = load_watchlist()
    wl.pop(addr.lower(), None)
    save_watchlist(wl)
