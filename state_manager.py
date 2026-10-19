#!/usr/bin/env python3
"""
state_manager.py — 统一持久化 & 重启恢复管理
==============================================
负责以下状态文件的读写，所有写操作原子化（tmp→replace）：

  watchlist_state.json   → 观察区标的 + 保护期状态
  daily_risk.json        → 当日累计亏损 + 熔断计数
  pending_orders_state.json → 当前挂单状态 + 互斥关系 + 有效期
  daily_blacklist.json   → 当日临时黑名单 + 冷静期

重启恢复流程（8步）由 recover_on_boot() 执行。
"""

import json, os, time, threading
from datetime import datetime, timezone
from pathlib import Path

WORKSPACE = Path("/root/.openclaw/workspace")

# ─── 文件路径 ─────────────────────────────────────────────────────────────────
F_WATCHLIST   = WORKSPACE / "watchlist_state.json"
F_DAILY_RISK  = WORKSPACE / "daily_risk.json"
F_PENDING_ORD = WORKSPACE / "pending_orders_state.json"
F_BLACKLIST   = WORKSPACE / "daily_blacklist.json"
F_POS_STATE   = WORKSPACE / "pos_state.json"
F_CIRCUIT     = WORKSPACE / "circuit_breaker.json"
F_GATES       = WORKSPACE / "price_gates.json"

_lock = threading.Lock()

# ─── 原子写 ───────────────────────────────────────────────────────────────────
def _write(path: Path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    tmp.replace(path)

def _read(path: Path, default=None):
    if not path.exists():
        return default if default is not None else {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return default if default is not None else {}

# ─── 1. watchlist_state ───────────────────────────────────────────────────────
def load_watchlist_state() -> dict:
    return _read(F_WATCHLIST, {})

def save_watchlist_state(state: dict):
    """写入前自动丢弃超过 2 小时的观察区记录"""
    now = time.time()
    cleaned = {}
    for sym, v in state.items():
        if not isinstance(v, dict):
            continue
        added = v.get("added_at_ts", now)
        if now - added > 2 * 3600:
            continue
        cleaned[sym] = v
    with _lock:
        _write(F_WATCHLIST, cleaned)

def watchlist_set(sym: str, score: float, direction: str,
                  target: float, high80_ts: float = 0):
    state = load_watchlist_state()
    existing = state.get(sym, {})
    state[sym] = {
        "score":       score,
        "direction":   direction,
        "target":      target,
        "added_at_ts": existing.get("added_at_ts", time.time()),
        "high80_ts":   high80_ts or existing.get("high80_ts", 0),
        "updated_at":  int(time.time() * 1000),
    }
    save_watchlist_state(state)

def watchlist_remove(sym: str):
    state = load_watchlist_state()
    state.pop(sym, None)
    save_watchlist_state(state)

def get_protect_remaining(sym: str) -> float:
    """返回保护期剩余秒数（0 = 已过期或未开始）"""
    state = load_watchlist_state()
    h80 = (state.get(sym) or {}).get("high80_ts", 0)
    if not h80:
        return 0.0
    remaining = 2 * 3600 - (time.time() - h80)
    return max(0.0, remaining)

# ─── 2. daily_risk ────────────────────────────────────────────────────────────
def _today_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def load_daily_risk() -> dict:
    d = _read(F_DAILY_RISK, {})
    today = _today_key()
    if d.get("date") != today:
        # 新的一天，重置计数但保留历史
        d = {"date": today, "total_loss": 0.0, "loss_count": 0,
             "circuit_open": False, "history": d.get("history", [])}
        _write(F_DAILY_RISK, d)
    return d

def add_loss(amount_usdt: float):
    """记录一笔亏损（amount_usdt 为正数）"""
    with _lock:
        d = load_daily_risk()
        d["total_loss"]  = round(d.get("total_loss", 0) + abs(amount_usdt), 4)
        d["loss_count"]  = d.get("loss_count", 0) + 1
        d["last_loss_ts"] = int(time.time() * 1000)
        _write(F_DAILY_RISK, d)
    return d

def set_circuit(open_: bool):
    with _lock:
        d = load_daily_risk()
        d["circuit_open"] = open_
        _write(F_DAILY_RISK, d)

def is_circuit_open_sm() -> bool:
    return load_daily_risk().get("circuit_open", False)

# ─── 3. pending_orders_state ─────────────────────────────────────────────────
def load_pending_orders() -> dict:
    return _read(F_PENDING_ORD, {})

def save_pending_orders(state: dict):
    with _lock:
        _write(F_PENDING_ORD, state)

def upsert_pending_order(sym: str, order_id: int, direction: str,
                          price: float, qty: float, placed_at: float = 0,
                          expire_sec: int = 4 * 3600, pair_id: int = 0):
    state = load_pending_orders()
    state[str(order_id)] = {
        "symbol":     sym,
        "direction":  direction,
        "price":      price,
        "qty":        qty,
        "placed_at":  placed_at or time.time(),
        "expire_at":  (placed_at or time.time()) + expire_sec,
        "pair_id":    pair_id,   # 双网互斥 ID，同 pair_id 的为一组
        "active":     True,
    }
    save_pending_orders(state)

def remove_pending_order(order_id: int):
    state = load_pending_orders()
    state.pop(str(order_id), None)
    save_pending_orders(state)

def get_expired_orders(now: float = None) -> list:
    now = now or time.time()
    state = load_pending_orders()
    return [
        (int(k), v) for k, v in state.items()
        if v.get("active") and v.get("expire_at", 0) < now
    ]

# ─── 4. daily_blacklist ───────────────────────────────────────────────────────
def load_blacklist() -> dict:
    d = _read(F_BLACKLIST, {})
    today = _today_key()
    if d.get("date") != today:
        d = {"date": today, "symbols": {}}
        _write(F_BLACKLIST, d)
    return d

def blacklist_add(sym: str, reason: str, cooldown_sec: int = 4 * 3600):
    with _lock:
        d = load_blacklist()
        d["symbols"][sym] = {
            "reason":    reason,
            "added_at":  time.time(),
            "expire_at": time.time() + cooldown_sec,
        }
        _write(F_BLACKLIST, d)

def blacklist_remove(sym: str):
    with _lock:
        d = load_blacklist()
        d["symbols"].pop(sym, None)
        _write(F_BLACKLIST, d)

def is_blacklisted(sym: str) -> bool:
    d = load_blacklist()
    entry = d.get("symbols", {}).get(sym)
    if not entry:
        return False
    if entry.get("expire_at", 0) < time.time():
        blacklist_remove(sym)
        return False
    return True

def get_blacklist_syms() -> list:
    d = load_blacklist()
    now = time.time()
    return [s for s, v in d.get("symbols", {}).items()
            if v.get("expire_at", 0) > now]

# ─── 5. 重启恢复流程（8步）───────────────────────────────────────────────────
def recover_on_boot(api, notify_fn, log_fn) -> dict:
    """
    ws_monitor 启动时调用，严格按8步执行。
    返回 recovery_report dict，供调用方打印日志。
    """
    report = {}
    log_fn("[boot] ======== 重启恢复流程开始 ========")

    # ── 第一步：读取所有持久化文件 ──────────────────────────────────────────
    log_fn("[boot] Step1: 读取持久化文件")
    wl_state  = load_watchlist_state()
    risk      = load_daily_risk()
    pending   = load_pending_orders()
    blacklist = load_blacklist()
    report["watchlist_count"]  = len(wl_state)
    report["daily_loss"]       = risk.get("total_loss", 0)
    report["circuit_open"]     = risk.get("circuit_open", False)
    report["pending_count"]    = len(pending)
    report["blacklist_count"]  = len(blacklist.get("symbols", {}))
    log_fn(f"[boot] Step1 完成: wl={len(wl_state)} risk={risk.get('total_loss',0)}U "
           f"circuit={risk.get('circuit_open')} pending={len(pending)} bl={len(blacklist.get('symbols',{}))}")

    # ── 第二步：检查持仓 + 补挂止损止盈 ────────────────────────────────────
    log_fn("[boot] Step2: 检查持仓，补挂缺失止损止盈")
    filled_count = 0
    try:
        positions = api.get_positions() or []
        active    = [p for p in positions if abs(float(p.get("positionAmt", 0))) > 0]
        report["active_positions"] = len(active)
        if not active:
            log_fn("[boot] Step2: 无持仓，跳过")
        else:
            import json as _json
            ps = _json.loads(F_POS_STATE.read_text()) if F_POS_STATE.exists() else {}
            for pos in active:
                sym   = pos["symbol"]
                amt   = float(pos["positionAmt"])
                entry = float(pos.get("entryPrice", 0))
                side  = "LONG" if amt > 0 else "SHORT"
                qty   = abs(amt)
                if entry <= 0:
                    continue
                algos  = api.get_algo_open_orders(sym) or []
                has_sl = any("STOP" in a.get("orderType", "") for a in algos)
                has_tp_algo = any("TAKE_PROFIT" in a.get("orderType", "") for a in algos)
                # TP 可能是普通 LIMIT 减仓单（已改为限价止盈）
                try:
                    open_ords = api.get_open_orders(sym) or []
                    has_tp_limit = any(
                        o.get("type") == "LIMIT" and o.get("reduceOnly")
                        for o in (open_ords if isinstance(open_ords, list) else [])
                    )
                except Exception:
                    has_tp_limit = False
                has_tp = has_tp_algo or has_tp_limit
                if has_sl and has_tp:
                    log_fn(f"[boot] {sym} SL/TP完整，跳过")
                    continue
                pinfo      = ps.get(sym, {})
                sl  = float(pinfo.get("sl_price",  entry * (0.95 if side=="LONG" else 1.05)))
                tp1 = float(pinfo.get("tp1_price", entry * (1.04 if side=="LONG" else 0.96)))
                tp2 = float(pinfo.get("tp2_price", entry * (1.07 if side=="LONG" else 0.93)))
                close_side = "SELL" if side == "LONG" else "BUY"
                log_fn(f"[boot] {sym} 缺失 {'SL' if not has_sl else ''} {'TP' if not has_tp else ''}，补挂: sl={sl} tp1={tp1}")
                if not has_sl:
                    try:
                        r = api.place_algo_order(sym, close_side, "STOP_MARKET", sl, side, quantity=qty)
                        if r.get("algoId"):
                            log_fn(f"[boot] {sym} 止损补挂 @{sl} ✅")
                            filled_count += 1
                    except Exception as e:
                        log_fn(f"[boot] {sym} 止损补挂失败: {e}")
                if not has_tp:
                    import math as _math
                    def _fmt_boot(p, _sym=sym):
                        # 优先从交易所拉取真实 tickSize，避免精度超限
                        try:
                            import requests as _req
                            _ei = _req.get('https://fapi.binance.com/fapi/v1/exchangeInfo',
                                           timeout=5).json()
                            _tick_val = None
                            for _s in _ei.get('symbols', []):
                                if _s['symbol'] == _sym:
                                    for _f in _s['filters']:
                                        if _f['filterType'] == 'PRICE_FILTER':
                                            _tick_val = float(_f['tickSize'])
                                            break
                                    break
                            if _tick_val:
                                tick = _tick_val
                            else:
                                raise ValueError('tickSize not found')
                        except Exception:
                            mag = abs(p)
                            if mag < 1:      tick = 0.00001
                            elif mag < 10:   tick = 0.0001
                            elif mag < 100:  tick = 0.001
                            elif mag < 1000: tick = 0.01
                            else:            tick = 0.1
                        d = max(0, -int(_math.floor(_math.log10(tick))))
                        return f"{round(round(p/tick)*tick, d):.{d}f}"
                    half_q = round(qty / 2, 3)
                    rest_q = round(qty - half_q, 3)
                    try:
                        r1 = api.place_order(sym, close_side, "LIMIT",
                                             quantity=str(half_q), price=_fmt_boot(tp1),
                                             positionSide=side, timeInForce="GTC")
                        r2 = api.place_order(sym, close_side, "LIMIT",
                                             quantity=str(rest_q), price=_fmt_boot(tp2),
                                             positionSide=side, timeInForce="GTC")
                        if r1.get("orderId") and r2.get("orderId"):
                            log_fn(f"[boot] {sym} 止盈补挂 @{tp1}/{tp2} ✅")
                            filled_count += 2
                        else:
                            log_fn(f"[boot] {sym} 止盈补挂失败: {r1.get('msg','')} {r2.get('msg','')}")
                    except Exception as e:
                        log_fn(f"[boot] {sym} 止盈补挂失败: {e}")
    except Exception as e:
        log_fn(f"[boot] Step2 异常: {e}")
    report["filled_algo"] = filled_count
    log_fn(f"[boot] Step2 完成: 补挂 {filled_count} 个algo单")

    # ── 第三步：恢复熔断计数 ────────────────────────────────────────────────
    log_fn(f"[boot] Step3: 熔断状态 open={risk.get('circuit_open')} "
           f"累亏={risk.get('total_loss',0)}U/{risk.get('loss_count',0)}笔")

    # ── 第四步：恢复观察区（丢弃>2小时的） ──────────────────────────────────
    valid_wl = {s: v for s, v in wl_state.items()
                if time.time() - v.get("added_at_ts", 0) <= 2 * 3600}
    stale    = len(wl_state) - len(valid_wl)
    log_fn(f"[boot] Step4: 观察区恢复 {len(valid_wl)} 个（丢弃 {stale} 个>2h）")
    report["wl_valid"] = len(valid_wl)
    report["wl_stale"] = stale

    # ── 第五步：恢复挂单保护期状态（继续倒计时，不重置） ───────────────────
    protect_syms = [s for s, v in valid_wl.items() if v.get("high80_ts", 0) > 0]
    for sym in protect_syms:
        remain = get_protect_remaining(sym)
        log_fn(f"[boot] Step5: {sym} 保护期剩余 {remain/60:.0f}min")
    report["protect_count"] = len(protect_syms)
    log_fn(f"[boot] Step5: {len(protect_syms)} 个标的保护期继续倒计时")

    # ── 第六步：恢复当日黑名单 ──────────────────────────────────────────────
    bl_syms = get_blacklist_syms()
    log_fn(f"[boot] Step6: 黑名单恢复 {len(bl_syms)} 个: {bl_syms}")
    report["blacklist_syms"] = bl_syms

    # ── 第七步：重新连接 WebSocket（由调用方处理，这里只标记） ───────────────
    log_fn("[boot] Step7: WebSocket 将由 start_user_data_stream() 建立")

    # ── 第八步：恢复完成 ─────────────────────────────────────────────────────
    log_fn("[boot] Step8: 恢复完成，静默继续运行")
    log_fn("[boot] ======== 重启恢复流程结束 ========")
    return report


# ─── 6. 每5分钟自动持久化（后台线程）────────────────────────────────────────
def start_auto_persist(get_watchlist_fn, get_pending_fn, interval: int = 300):
    """
    启动后台线程，每 interval 秒把内存里的 watchlist / pending_orders
    刷新到磁盘，防止进程被杀时丢失最近的变更。
    """
    def _loop():
        while True:
            time.sleep(interval)
            try:
                wl = get_watchlist_fn()
                if wl is not None:
                    save_watchlist_state(wl)
                po = get_pending_fn()
                if po is not None:
                    save_pending_orders(po)
            except Exception:
                pass
    import threading
    t = threading.Thread(target=_loop, daemon=True, name="auto-persist")
    t.start()
    return t


# ─── 7. WS 断线状态跟踪 ──────────────────────────────────────────────────────
_ws_fail_count = 0
_ws_fail_lock  = threading.Lock()

def ws_on_fail(notify_fn, log_fn):
    global _ws_fail_count
    with _ws_fail_lock:
        _ws_fail_count += 1
        count = _ws_fail_count
    log_fn(f"[ws_fail] 第 {count} 次重连失败")
    if count >= 3:
        notify_fn(f"⚠️ WebSocket 断线且重连失败 {count} 次，请检查网络")

def ws_on_reconnect(log_fn):
    global _ws_fail_count
    with _ws_fail_lock:
        _ws_fail_count = 0
    log_fn("[ws_reconnect] WS 已重连，重置失败计数")


# ─── 8. 通知过滤（静默规则）─────────────────────────────────────────────────
# 以下事件静默，返回 False；需要通知的返回 True
_SILENT_EVENTS = {
    "restart_ok", "watchlist_add", "watchlist_remove", "watchlist_score_change",
    "order_place", "order_cancel", "order_adjust",
    "protect_start", "protect_end", "protect_remain",
    "patrol_ok", "persist_ok", "cache_update",
    "blacklist_add", "blacklist_remove",
    "tp_trigger",             # 止盈静默（截图里你要求的）
    "daily_report",
    "ws_reconnect_ok",        # WS 正常断线重连
    "boot_ok",                # 重启恢复过程静默
}

_NOTIFY_EVENTS = {
    "open_long", "open_short",       # 开仓通知
    "sl_trigger",                    # 止损触发
    "circuit_open",                  # 熔断触发
    "ws_fail_3times",                # WS断线重连失败>=3次
    "balance_below_floor",           # 账户余额跌破保本金
    "sl_fill_failed",                # 止损止盈补挂失败（高风险）
}

def should_notify(event: str) -> bool:
    if event in _SILENT_EVENTS:
        return False
    if event in _NOTIFY_EVENTS:
        return True
    return False  # 未知事件默认静默
