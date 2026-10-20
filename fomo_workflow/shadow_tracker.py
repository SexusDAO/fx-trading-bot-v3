# ============================================================
# shadow_tracker.py — 信号后价格影子跟踪
# 记录每条推送信号在 5m/30m/2h/24h 的价格变化
# 用于验证信号分类系统的准确性
# ============================================================
import time, json, os, asyncio, requests
from datetime import datetime, timezone, timedelta

_LOG_FILE = os.path.join(os.path.dirname(__file__), "trade.log")
_SHADOW_FILE = os.path.join(os.path.dirname(__file__), "shadow_records.json")
_PROXY = {"https": "socks5://REDACTED_PROXY_CREDENTIALS",
           "http":  "socks5://REDACTED_PROXY_CREDENTIALS"}

WINDOWS = {
    "5m":  5 * 60,
    "30m": 30 * 60,
    "2h":  2 * 3600,
    "24h": 24 * 3600,
}

MAX_RECORDS = 5000  # 最多保存 5000 条记录
_SAVE_INTERVAL = 60  # 每 60 秒落盘一次
_last_save = 0
_records: list = []
_dirty = False


def _log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with open(_LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _load():
    global _records, _last_save
    try:
        if os.path.exists(_SHADOW_FILE):
            with open(_SHADOW_FILE) as f:
                _records = json.load(f)
            _log(f"[SHADOW] 加载 {len(_records)} 条影子记录")
    except Exception as e:
        _log(f"[SHADOW] 加载失败: {e}")
        _records = []


def _save(force=False):
    global _last_save, _dirty
    now = time.time()
    if not force and now - _last_save < _SAVE_INTERVAL and not _dirty:
        return
    if not _dirty and not force:
        return
    try:
        # Trim to max records
        if len(_records) > MAX_RECORDS:
            _records[:] = _records[-MAX_RECORDS:]
        with open(_SHADOW_FILE, "w") as f:
            json.dump(_records, f, indent=2)
        _last_save = now
        _dirty = False
    except Exception as e:
        _log(f"[SHADOW] 保存失败: {e}")


def register_signal(token_addr: str, token_symbol: str, category: str,
                     priority: str, score: int, mc: float, entry_price: float,
                     source: str, trader: str = "", grade: str = ""):
    """信号推送时调用，记录基线价格。"""
    if entry_price <= 0:
        return
    record = {
        "token_addr": token_addr.lower(),
        "token_symbol": token_symbol,
        "category": category,
        "priority": priority,
        "score": score,
        "source": source,
        "trader": trader,
        "grade": grade,
        "mc": mc,
        "baseline_price": entry_price,
        "baseline_ts": time.time(),
        "samples": {},
    }
    _records.append(record)
    _dirty = True
    _log(f"[SHADOW] 注册 {token_symbol} cat={category} price=${entry_price:.10f}")
    _save(force=True)


def _get_current_price(token_addr: str) -> float:
    """通过 DexScreener 获取当前价格。"""
    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{token_addr}",
            timeout=8, proxies=_PROXY,
        )
        pairs = r.json().get("pairs", []) or []
        if not pairs:
            return 0.0
        pair = max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))
        return float(pair.get("priceUsd") or pair.get("price", "0") or 0)
    except Exception:
        return 0.0


async def check_samples():
    """定时调用：检查所有未完成记录的价格采样窗口。"""
    global _dirty
    now = time.time()
    checked = 0
    completed = 0

    for record in _records:
        if len(record["samples"]) >= len(WINDOWS):
            continue  # 已完成所有窗口

        elapsed = now - record["baseline_ts"]
        for window_name, window_sec in WINDOWS.items():
            if window_name in record["samples"]:
                continue
            if elapsed < window_sec:
                continue  # 窗口还未到

            # 窗口到了，采样价格
            price = await asyncio.to_thread(_get_current_price, record["token_addr"])
            if price <= 0:
                # 价格获取失败，标记为 null
                record["samples"][window_name] = {
                    "ts": now, "price": None, "return": None
                }
            else:
                baseline = record["baseline_price"]
                ret = (price / baseline - 1) if baseline > 0 else 0
                record["samples"][window_name] = {
                    "ts": now, "price": price, "return": ret
                }
            checked += 1
            if len(record["samples"]) >= len(WINDOWS):
                completed += 1

    if checked > 0:
        _dirty = True
        _log(f"[SHADOW] 检查 {checked} 个窗口，{completed} 条记录完成")
        _save(force=True)
    else:
        _save()


async def shadow_loop():
    """后台循环：每 5 分钟检查一次影子跟踪采样。"""
    _load()
    _log(f"[SHADOW] 影子跟踪启动，{len(_records)} 条记录")
    while True:
        try:
            await check_samples()
        except Exception as e:
            _log(f"[SHADOW] 检查异常: {e}")
        await asyncio.sleep(300)  # 5 分钟


def get_summary(hours: int = 24) -> dict:
    """
    获取最近 N 小时内已完成所有窗口的信号分类统计。
    返回各分类的胜率、平均收益、样本数。
    """
    cutoff = time.time() - hours * 3600
    by_category = {}

    for record in _records:
        if record["baseline_ts"] < cutoff:
            continue
        if len(record["samples"]) < len(WINDOWS):
            continue  # 未完成所有窗口

        cat = record["category"]
        if cat not in by_category:
            by_category[cat] = {
                "count": 0,
                "wins": 0,  # 24h return > 0
                "losses": 0,
                "returns_5m": [], "returns_30m": [],
                "returns_2h": [], "returns_24h": [],
                "best_24h": -999, "worst_24h": 999,
            }

        entry = by_category[cat]
        entry["count"] += 1

        r5 = record["samples"].get("5m", {}).get("return")
        r30 = record["samples"].get("30m", {}).get("return")
        r2h = record["samples"].get("2h", {}).get("return")
        r24 = record["samples"].get("24h", {}).get("return")

        if r5 is not None: entry["returns_5m"].append(r5)
        if r30 is not None: entry["returns_30m"].append(r30)
        if r2h is not None: entry["returns_2h"].append(r2h)
        if r24 is not None:
            entry["returns_24h"].append(r24)
            if r24 > 0: entry["wins"] += 1
            else: entry["losses"] += 1
            entry["best_24h"] = max(entry["best_24h"], r24)
            entry["worst_24h"] = min(entry["worst_24h"], r24)

    # 计算平均收益
    summary = {}
    for cat, data in by_category.items():
        n = data["count"]
        if n == 0:
            continue
        win_rate = data["wins"] / n if n > 0 else 0
        avg = lambda lst: sum(lst) / len(lst) if lst else None
        summary[cat] = {
            "count": n,
            "win_rate": f"{win_rate:.0%}",
            "avg_5m": f"{avg(data['returns_5m']):+.1%}" if avg(data['returns_5m']) is not None else "N/A",
            "avg_30m": f"{avg(data['returns_30m']):+.1%}" if avg(data['returns_30m']) is not None else "N/A",
            "avg_2h": f"{avg(data['returns_2h']):+.1%}" if avg(data['returns_2h']) is not None else "N/A",
            "avg_24h": f"{avg(data['returns_24h']):+.1%}" if avg(data['returns_24h']) is not None else "N/A",
            "best_24h": f"+{data['best_24h']:.0%}" if data['best_24h'] != -999 else "N/A",
            "worst_24h": f"{data['worst_24h']:.0%}" if data['worst_24h'] != 999 else "N/A",
        }

    return summary


def get_daily_report() -> str:
    """生成每日复盘报告文本。"""
    summary = get_summary(hours=24)
    if not summary:
        return "📊 影子跟踪日报\n过去24h无已完成样本，等待信号积累。"

    lines = ["📊 影子跟踪日报（最近24h）", "─" * 20]
    for cat, data in sorted(summary.items(), key=lambda x: -x[1]["count"]):
        lines.append(
            f"{cat} | {data['count']}样本 | 胜率{data['win_rate']}\n"
            f"  5m {data['avg_5m']} | 30m {data['avg_30m']} | "
            f"2h {data['avg_2h']} | 24h {data['avg_24h']}\n"
            f"  最佳24h {data['best_24h']} | 最差24h {data['worst_24h']}"
        )
    lines.append("─" * 20)
    lines.append("💡 用于验证分类系统准确性，非交易收益")
    return "\n".join(lines)
