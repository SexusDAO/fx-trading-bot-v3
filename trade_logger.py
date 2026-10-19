#!/usr/bin/env python3
"""
trade_logger.py — 结构化交易日志 (P3-1)

每笔交易完整记录，支持按信号类型/时段/币种统计胜率。
动态黑名单逻辑也集成在此（P3-2）。

接口：
    log_open(sym, direction, sig_type, entry, qty, score, details)
    log_close(sym, exit_price, reason)
    get_stats(days=7) -> dict
    check_dynamic_blacklist(sym, direction) -> (bool, str)
    add_dynamic_blacklist(sym, direction, reason)
"""

import json
import time
import math
from pathlib import Path
from datetime import datetime, timezone

WORKSPACE       = Path("/root/.openclaw/workspace")
LOG_FILE        = WORKSPACE / "trade_log.json"
DYN_BL_FILE     = WORKSPACE / "ws_cache" / "dynamic_blacklist.json"
BL_LOSS_THRESH  = 2     # 同一币种连续亏损2次加入黑名单
BL_DAYS         = 7     # 黑名单时长（天）


def _load_log() -> list:
    try:
        return json.loads(LOG_FILE.read_text()) if LOG_FILE.exists() else []
    except Exception:
        return []


def _save_log(data: list):
    LOG_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def _load_bl() -> dict:
    try:
        DYN_BL_FILE.parent.mkdir(parents=True, exist_ok=True)
        return json.loads(DYN_BL_FILE.read_text()) if DYN_BL_FILE.exists() else {}
    except Exception:
        return {}


def _save_bl(data: dict):
    DYN_BL_FILE.parent.mkdir(parents=True, exist_ok=True)
    DYN_BL_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))


# ── 开仓记录 ──────────────────────────────────────────────────────────────────

def log_open(
    sym:       str,
    direction: str,
    sig_type:  str,
    entry:     float,
    qty:       float,
    score:     float,
    details:   dict = None,
):
    """记录开仓信息。"""
    if details is None:
        details = {}
    records = _load_log()
    record = {
        "id":          f"{sym}_{int(time.time())}",
        "sym":         sym,
        "direction":   direction,
        "sig_type":    sig_type,
        "entry_price": entry,
        "qty":         qty,
        "notional":    round(entry * qty, 2),
        "score":       round(score, 1),
        "btc_trend":   details.get("btc_trend", ""),
        "funding_pct": round(float(details.get("funding", 0)) * 100, 4),
        "scenario":    details.get("scenario", 0),
        "open_ts":     time.time(),
        "open_dt":     datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        # 平仓信息（后续填写）
        "close_price": None,
        "close_ts":    None,
        "close_dt":    None,
        "pnl_u":       None,
        "pnl_pct":     None,
        "close_reason": None,
        "add_count":   0,
        "status":      "open",
    }
    records.append(record)
    # 只保留最近500笔
    if len(records) > 500:
        records = records[-500:]
    _save_log(records)
    return record["id"]


# ── 平仓记录 ──────────────────────────────────────────────────────────────────

def log_close(
    sym:        str,
    exit_price: float,
    reason:     str,   # 'sl' | 'tp1' | 'tp2' | 'health' | 'manual' | 'circuit'
    add_count:  int = 0,
):
    """
    更新对应开仓记录的平仓信息。
    同时检查是否需要加入动态黑名单。
    """
    records = _load_log()
    # 找最近一条 open 状态的同币种记录
    target = None
    for r in reversed(records):
        if r["sym"] == sym and r["status"] == "open":
            target = r
            break
    if target is None:
        return

    entry      = target["entry_price"]
    qty        = target["qty"]
    direction  = target["direction"]
    pnl_u      = round((exit_price - entry) * qty * (1 if direction == "LONG" else -1), 2)
    pnl_pct    = round(pnl_u / (entry * qty) * 100, 2) if entry * qty > 0 else 0

    target["close_price"]  = exit_price
    target["close_ts"]     = time.time()
    target["close_dt"]     = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    target["pnl_u"]        = pnl_u
    target["pnl_pct"]      = pnl_pct
    target["close_reason"] = reason
    target["add_count"]    = add_count
    target["status"]       = "closed"

    _save_log(records)

    # P3-2: 连续亏损检查 → 动态黑名单
    if pnl_u < 0:
        _check_add_dynamic_bl(sym, direction, records)


def _check_add_dynamic_bl(sym: str, direction: str, records: list):
    """检查最近几笔是否连续亏损，是则加入7天黑名单。"""
    closed = [r for r in records if r["sym"] == sym and r["status"] == "closed"]
    if len(closed) < BL_LOSS_THRESH:
        return
    recent = closed[-BL_LOSS_THRESH:]
    if all(r.get("pnl_u", 0) < 0 for r in recent):
        add_dynamic_blacklist(sym, direction, f"连续亏损{BL_LOSS_THRESH}次")


# ── 动态黑名单 ────────────────────────────────────────────────────────────────

def add_dynamic_blacklist(sym: str, direction: str, reason: str):
    bl = _load_bl()
    expire_ts  = time.time() + BL_DAYS * 86400
    expire_str = datetime.fromtimestamp(expire_ts, tz=timezone.utc).strftime("%Y-%m-%d UTC")
    bl[sym] = {
        "direction":  direction,
        "reason":     reason,
        "expire_ts":  expire_ts,
        "expire_str": expire_str,
        "losses":     bl.get(sym, {}).get("losses", 0) + 1,
        "added_dt":   datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }
    _save_bl(bl)


def check_dynamic_blacklist(sym: str, direction: str) -> tuple:
    """返回 (in_blacklist: bool, reason: str)。"""
    bl = _load_bl()
    entry = bl.get(sym)
    if not entry:
        return False, ""
    if time.time() >= float(entry.get("expire_ts", 0)):
        # 已过期，清理
        del bl[sym]
        _save_bl(bl)
        return False, ""
    bl_dir = entry.get("direction", "")
    if bl_dir and bl_dir != direction:
        return False, ""
    return True, entry.get("reason", "动态黑名单")


# ── 统计 ──────────────────────────────────────────────────────────────────────

def get_stats(days: int = 7) -> dict:
    """返回最近 N 天的交易统计。"""
    records = _load_log()
    cutoff  = time.time() - days * 86400
    closed  = [r for r in records
               if r["status"] == "closed" and r.get("close_ts", 0) >= cutoff]

    if not closed:
        return {"total": 0, "message": f"最近{days}天无已平仓记录"}

    total    = len(closed)
    wins     = [r for r in closed if r.get("pnl_u", 0) > 0]
    losses   = [r for r in closed if r.get("pnl_u", 0) <= 0]
    win_rate = len(wins) / total * 100
    total_pnl = sum(r.get("pnl_u", 0) for r in closed)

    # 按信号类型
    by_type = {}
    for r in closed:
        st = r.get("sig_type", "unknown")
        if st not in by_type:
            by_type[st] = {"total": 0, "wins": 0, "pnl": 0}
        by_type[st]["total"] += 1
        by_type[st]["pnl"]   += r.get("pnl_u", 0)
        if r.get("pnl_u", 0) > 0:
            by_type[st]["wins"] += 1
    for st in by_type:
        t = by_type[st]["total"]
        by_type[st]["win_rate"] = round(by_type[st]["wins"] / t * 100, 1) if t else 0
        by_type[st]["pnl"]     = round(by_type[st]["pnl"], 2)

    # 按平仓原因
    by_reason = {}
    for r in closed:
        rs = r.get("close_reason", "unknown")
        by_reason[rs] = by_reason.get(rs, 0) + 1

    # 连续亏损数
    consecutive = 0
    for r in reversed(closed):
        if r.get("pnl_u", 0) <= 0:
            consecutive += 1
        else:
            break

    return {
        "days":         days,
        "total":        total,
        "wins":         len(wins),
        "losses":       len(losses),
        "win_rate":     round(win_rate, 1),
        "total_pnl":    round(total_pnl, 2),
        "avg_win":      round(sum(r["pnl_u"] for r in wins) / len(wins), 2) if wins else 0,
        "avg_loss":     round(sum(r["pnl_u"] for r in losses) / len(losses), 2) if losses else 0,
        "by_sig_type":  by_type,
        "by_reason":    by_reason,
        "consecutive_losses": consecutive,
    }


def daily_summary() -> str:
    """生成每日汇总文本（用于推送通知）。"""
    stats = get_stats(days=1)
    if stats.get("total", 0) == 0:
        return "今日暂无平仓记录"
    lines = [
        f"📊 今日交易汇总",
        f"共 {stats['total']} 笔  胜率 {stats['win_rate']}%  盈亏 {stats['total_pnl']:+.2f}U",
    ]
    for st, d in stats.get("by_sig_type", {}).items():
        lines.append(f"  {st}: {d['total']}笔 胜率{d['win_rate']}% {d['pnl']:+.2f}U")
    if stats["consecutive_losses"] >= 3:
        lines.append(f"⚠️ 连续亏损 {stats['consecutive_losses']} 笔，已触发降仓保护")
    return "\n".join(lines)


if __name__ == "__main__":
    print("trade_logger 加载正常 ✅")
    print(daily_summary())
