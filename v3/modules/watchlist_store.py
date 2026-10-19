"""
modules/watchlist_store.py — 观察池数据层

职责（单一）：
  - 读写 config/watchlist.json
  - TTL 管理（自动过期）
  - 评分趋势追踪（score_history → trend）
  - 状态流转：watching → upgraded / expired / dropped

数据结构（每条 item）：
  {
    "symbol":               "BLESSUSDT",
    "direction":            "LONG",
    "signal_type":          "trend_long",
    "entered_at":           "2026-08-03T08:00:00Z",
    "ttl_expires_at":       "2026-08-03T10:00:00Z",
    "entry_price_snapshot": 0.00412,   # 进池时价格（追高判断基准）
    "entry_score_snapshot": 61,        # 进池时评分
    "score_history":        [61, 63],  # 每次重新评分追加（最多保留 6 个）
    "latest_score":         63,
    "latest_price":         0.00418,
    "upgrade_attempts":     1,
    "status":               "watching"  # watching/upgraded/expired/dropped
  }

不包含任何业务逻辑，只做 CRUD。
"""

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

WORKSPACE      = Path(__file__).parent.parent
WATCHLIST_FILE = WORKSPACE / "config" / "watchlist.json"

# 默认 TTL（分钟）
DEFAULT_TTL_MIN = 120
# 评分历史最多保留几次
MAX_SCORE_HISTORY = 6


# ── 工具 ──────────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _is_expired(ttl_str: str) -> bool:
    try:
        dt = datetime.strptime(ttl_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) > dt
    except Exception:
        return False

def _load() -> dict:
    try:
        with open(WATCHLIST_FILE) as f:
            data = json.load(f)
            if not isinstance(data, dict):
                return {"updated_at": _now_iso(), "items": []}
            return data
    except Exception:
        return {"updated_at": _now_iso(), "items": []}

def _save(data: dict):
    WATCHLIST_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = WATCHLIST_FILE.with_suffix(".tmp")
    data["updated_at"] = _now_iso()
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    tmp.replace(WATCHLIST_FILE)


# ── 评分趋势计算 ──────────────────────────────────────────────────────────────

def calc_score_trend(history: list[int]) -> str:
    """
    根据评分历史判断趋势。
    连续 3 次上升 → "rising"
    连续 3 次下降 → "falling"
    其他           → "flat"
    """
    if len(history) < 3:
        return "flat"
    recent = history[-3:]
    if recent[0] < recent[1] < recent[2]:
        return "rising"
    if recent[0] > recent[1] > recent[2]:
        return "falling"
    return "flat"

def effective_score(item: dict) -> int:
    """
    有效评分 = latest_score + 趋势加成。
    连续 3 次上升 +3；连续 3 次下降 -3；其他不变。
    """
    base  = item.get("latest_score", 0)
    trend = calc_score_trend(item.get("score_history", []))
    if trend == "rising":
        return base + 3
    if trend == "falling":
        return base - 3
    return base


# ── 公开接口 ──────────────────────────────────────────────────────────────────

def add_or_update(symbol: str, direction: str,
                  score: int, price: float,
                  signal_type: str,
                  ttl_min: int = DEFAULT_TTL_MIN) -> dict:
    """
    将标的加入观察池（若已存在则更新评分/价格）。
    Returns: item dict
    """
    data  = _load()
    items = data.get("items", [])

    # 找已有条目
    existing = next(
        (it for it in items
         if it["symbol"] == symbol and it["direction"] == direction
         and it["status"] == "watching"),
        None
    )

    now = _now_iso()
    expires = (datetime.now(timezone.utc) + timedelta(minutes=ttl_min)
               ).strftime("%Y-%m-%dT%H:%M:%SZ")

    if existing:
        # 更新已有条目
        history = existing.get("score_history", [])
        history.append(score)
        existing["score_history"] = history[-MAX_SCORE_HISTORY:]
        existing["latest_score"]  = score
        existing["latest_price"]  = price
        existing["signal_type"]   = signal_type
        # 每次更新时续期 TTL
        existing["ttl_expires_at"] = expires
        item = existing
    else:
        # 新条目
        item = {
            "symbol":               symbol,
            "direction":            direction,
            "signal_type":          signal_type,
            "entered_at":           now,
            "ttl_expires_at":       expires,
            "entry_price_snapshot": price,
            "entry_score_snapshot": score,
            "score_history":        [score],
            "latest_score":         score,
            "latest_price":         price,
            "upgrade_attempts":     0,
            "status":               "watching",
        }
        items.append(item)

    data["items"] = items
    _save(data)
    return item


def get_watching() -> list[dict]:
    """返回所有 status=watching 且未过期的条目"""
    data  = _load()
    items = data.get("items", [])
    return [
        it for it in items
        if it.get("status") == "watching"
        and not _is_expired(it.get("ttl_expires_at", ""))
    ]


def update_item(symbol: str, direction: str, fields: dict):
    """更新指定条目的字段"""
    data  = _load()
    items = data.get("items", [])
    for it in items:
        if it["symbol"] == symbol and it["direction"] == direction:
            it.update(fields)
    data["items"] = items
    _save(data)


def mark_upgraded(symbol: str, direction: str):
    update_item(symbol, direction, {"status": "upgraded"})

def mark_dropped(symbol: str, direction: str):
    update_item(symbol, direction, {"status": "dropped"})


def purge_expired(drop_score_threshold: int = 45):
    """
    清理过期 + 评分持续低于阈值的条目。
    保留 upgraded/dropped 记录（供复盘），只移除 watching。
    """
    data  = _load()
    items = data.get("items", [])
    kept  = []
    for it in items:
        if it.get("status") != "watching":
            kept.append(it)
            continue
        if _is_expired(it.get("ttl_expires_at", "")):
            it["status"] = "expired"
            kept.append(it)
            continue
        if effective_score(it) < drop_score_threshold:
            it["status"] = "dropped"
            kept.append(it)
            continue
        kept.append(it)
    data["items"] = kept
    _save(data)


def clear_all():
    """清空（仅用于测试）"""
    _save({"items": []})


def snapshot() -> dict:
    """返回当前观察池状态摘要（用于播报/调试）"""
    data  = _load()
    items = data.get("items", [])
    watching  = [it for it in items if it["status"] == "watching"]
    upgraded  = [it for it in items if it["status"] == "upgraded"]
    expired   = [it for it in items if it["status"] == "expired"]
    dropped   = [it for it in items if it["status"] == "dropped"]
    return {
        "watching": len(watching),
        "upgraded": len(upgraded),
        "expired":  len(expired),
        "dropped":  len(dropped),
        "items":    watching,   # 仅返回 watching 的完整信息
    }


def add_volume_warning(symbol: str, direction: str,
                       price: float, signal_type: str,
                       ttl_min: int = 360) -> dict:
    """
    将标的加入预警池（成交量异常）。
    
    预警池特点：
    - TTL = 6 小时（默认 360 分钟）
    - source = "volume_warning"
    - 下次扫描跳过 Top10 竞争，直接评分
    - 不直接开仓，必须走完评分 + 场景匹配
    
    Returns: item dict
    """
    data  = _load()
    items = data.get("items", [])
    
    # 检查是否已存在预警条目
    existing = next(
        (it for it in items
         if it["symbol"] == symbol and it.get("source") == "volume_warning"),
        None
    )
    
    if existing:
        # 更新已有预警
        existing["latest_price"] = price
        existing["ttl_expires_at"] = (
            datetime.now(timezone.utc) + timedelta(minutes=ttl_min)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        _save(data)
        return existing
    
    # 新建预警条目
    item = {
        "symbol": symbol,
        "direction": direction,
        "signal_type": signal_type,
        "entered_at": _now_iso(),
        "ttl_expires_at": (
            datetime.now(timezone.utc) + timedelta(minutes=ttl_min)
        ).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "entry_price_snapshot": price,
        "entry_score_snapshot": 0,
        "score_history": [],
        "latest_score": 0,
        "latest_price": price,
        "upgrade_attempts": 0,
        "status": "watching",
        "source": "volume_warning",
    }
    
    items.append(item)
    data["items"] = items
    _save(data)
    return item


def get_volume_warnings() -> list:
    """获取所有预警池条目"""
    data  = _load()
    items = data.get("items", [])
    return [
        it for it in items
        if it.get("source") == "volume_warning" and it.get("status") == "watching"
    ]
