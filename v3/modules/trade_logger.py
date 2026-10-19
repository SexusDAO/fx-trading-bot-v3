"""
trade_logger.py — 结构化交易日志 (P2)

每笔交易完整记录开仓和平仓，支持每日统计。
日志格式：JSONL（每行一条），append-only。

接口：
  log_open(symbol, direction, ...)   记录开仓
  log_close(symbol, direction, ...)  记录平仓，自动关联开仓记录
  daily_summary(date_str=None)       返回当日统计 dict
"""

import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

WORKSPACE = Path(__file__).parent.parent
LOG_FILE  = WORKSPACE / "logs" / "trade_log.jsonl"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _append(record: dict):
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _load_all() -> list[dict]:
    if not LOG_FILE.exists():
        return []
    records = []
    with open(LOG_FILE) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return records


# ── 公开接口 ──────────────────────────────────────────────────────────────────

def log_open(symbol: str,
             direction: str,
             entry_price: float,
             notional: float,
             signal_type: str,
             score: int,
             score_details: dict,
             entry_mode: str,
             funding_rate: float = 0.0,
             btc_trend: str = "neutral",
             sl_price: float = 0.0,
             tp1_price: float = 0.0,
             tp2_price: float = 0.0,
             extra: dict | None = None):
    """记录开仓"""
    record = {
        "event":        "open",
        "symbol":       symbol,
        "direction":    direction,
        "entry_price":  entry_price,
        "notional":     notional,
        "signal_type":  signal_type,
        "score":        score,
        "score_details": score_details,
        "entry_mode":   entry_mode,
        "funding_rate": funding_rate,
        "btc_trend":    btc_trend,
        "sl_price":     sl_price,
        "tp1_price":    tp1_price,
        "tp2_price":    tp2_price,
        "open_time":    _now_iso(),
        "key":          f"{symbol}_{direction}",
    }
    if extra:
        record.update(extra)
    _append(record)
    return record


def log_close(symbol: str,
              direction: str,
              close_price: float,
              close_reason: str,
              entry_price: float,
              notional: float,
              add_count: int = 0,
              avg_price: float = 0.0,
              extra: dict | None = None) -> dict:
    """
    记录平仓，计算 PnL。
    close_reason: "sl" | "tp1" | "tp2" | "manual" | "health" | "circuit"
    """
    avg = avg_price if avg_price else entry_price
    if direction == "LONG":
        pnl_pct = (close_price - avg) / avg if avg else 0
    else:
        pnl_pct = (avg - close_price) / avg if avg else 0

    pnl_usdt = round(pnl_pct * notional, 2)

    record = {
        "event":        "close",
        "symbol":       symbol,
        "direction":    direction,
        "entry_price":  entry_price,
        "avg_price":    avg,
        "close_price":  close_price,
        "notional":     notional,
        "pnl_pct":      round(pnl_pct * 100, 3),   # %
        "pnl_usdt":     pnl_usdt,
        "close_reason": close_reason,
        "add_count":    add_count,
        "close_time":   _now_iso(),
        "key":          f"{symbol}_{direction}",
    }
    if extra:
        record.update(extra)
    _append(record)
    return record


def daily_summary(date_str: str | None = None) -> dict:
    """
    返回指定日期（默认今天）的统计：
    {
      "date": "2026-08-03",
      "total_trades": N,
      "wins": N, "losses": N,
      "win_rate": 0.xx,
      "total_pnl_usdt": xx.x,
      "by_signal_type": { "trend": {...}, ... },
      "by_reason": { "sl": N, "tp1": N, ... },
      "consecutive_losses": N,    # 当前连续亏损笔数
    }
    """
    date_str = date_str or _today_str()
    records = _load_all()
    closes = [r for r in records
              if r.get("event") == "close"
              and r.get("close_time", "").startswith(date_str)]

    wins   = [r for r in closes if r.get("pnl_usdt", 0) > 0]
    losses = [r for r in closes if r.get("pnl_usdt", 0) <= 0]
    total  = len(closes)

    by_type = {}
    for r in closes:
        st = r.get("signal_type", "unknown")
        if st not in by_type:
            by_type[st] = {"trades": 0, "wins": 0, "pnl": 0.0}
        by_type[st]["trades"] += 1
        if r.get("pnl_usdt", 0) > 0:
            by_type[st]["wins"] += 1
        by_type[st]["pnl"] += r.get("pnl_usdt", 0)

    by_reason: dict[str, int] = {}
    for r in closes:
        reason = r.get("close_reason", "unknown")
        by_reason[reason] = by_reason.get(reason, 0) + 1

    # 连续亏损（从所有历史记录末尾往前数）
    all_closes = sorted(
        [r for r in records if r.get("event") == "close"],
        key=lambda r: r.get("close_time", "")
    )
    consec = 0
    for r in reversed(all_closes):
        if r.get("pnl_usdt", 0) <= 0:
            consec += 1
        else:
            break

    return {
        "date":               date_str,
        "total_trades":       total,
        "wins":               len(wins),
        "losses":             len(losses),
        "win_rate":           round(len(wins) / total, 3) if total else 0,
        "total_pnl_usdt":     round(sum(r.get("pnl_usdt", 0) for r in closes), 2),
        "by_signal_type":     by_type,
        "by_reason":          by_reason,
        "consecutive_losses": consec,
    }
