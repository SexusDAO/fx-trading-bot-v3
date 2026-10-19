"""
dual_net_orders.py — 双网挂单策略核心模块
═══════════════════════════════════════════════════════════════════
触发条件：scanner 评分 >= 80 时，同时部署 A单（回调限价）+ B单（突破条件）
互斥执行：任意一单成交后，另一单在 3 秒内强制撤销
保护期：挂单后 4 小时内，评分 60~100 不撤单，仅 <60 或触发例外才撤

与现有系统融合：
- 仓位上限沿用 MAX_LONG=3 / MAX_SHORT=2（空单实际控制在 1 个）
- 评分档位独立（不干扰主流程的 _calc_position_usdt）
- 状态文件：pending_orders.json（独立于 watchlist.json）
═══════════════════════════════════════════════════════════════════
"""

import os, json, time, threading, math
from datetime import datetime, timezone
from pathlib import Path
import sys as _sys
_sys.path.insert(0, str(Path(__file__).parent))
from pos_state_store import store as _ps_store  # P0: 统一 pos_state 写入

WORKSPACE         = Path(__file__).parent
PENDING_FILE      = WORKSPACE / "pending_orders.json"
LOG_FILE          = WORKSPACE / "scanner.log"

# ── 双网专用仓位档位（轻量，不覆盖主流程档位）──────────────────────────────
DUAL_POS_LOW  = 300   # 80~84分 A单仓位 (U)
DUAL_POS_HIGH = 500   # 85+分  A单仓位 (U)
B_QTY_RATIO   = 0.70  # B单基础仓位比 = A单的70%（实际按突破强度动态调整）
B_SL_PCT      = 0.05  # B单止损备用（ATR计算失败时的兜底固定比例）
B_SL_ATR_MULT = 1.5   # B单ATR止损倍数：入场价 ± ATR × 1.5
B_QTY_BOOST_MAX = 1.30  # 突破强度最大仓位加成上限（最多+30%）
A_EXPIRE_H    = 8     # A单有效期（小时）—— 8h，给信号较强的币足够时间等回调/突破
A_DRIFT_MAX   = 0.08  # A单挂价与现价偏离超过此比例则不部署（避免无效挂单）
A_DAILY_CANCEL_MAX = 3  # 同标的当日撤单次数上限，超过则当天冷却不再挂
B_EXPIRE_MIN  = 30    # B单信号有效期（分钟）
PROTECT_H     = 4     # 保护期（小时）—— 调整为4h（A单总时长的一半）
SCORE_MIN     = 80    # 触发双网的最低评分
SCORE_CANCEL  = 60    # 低于此分强制撤单
MUTUAL_CANCEL_SEC = 3 # 互斥撤单超时（秒）

# ── B单突破判断参数 ────────────────────────────────────────────────────────────
# 量比门槛根据标的 24h 成交额动态调整（市值越小流动性越差，门槛越低）
# 成交额 > 5万U亿（50000万U）: 大市值  -> 1.5x
# 成交额 5000~50000万U:        中市值  -> 1.2x
# 成交额 500~5000万U:          小市值  -> 0.8x
# 成交额 50~500万U:            微市值  -> 0.5x
# 成交额 < 50万U:              极小    -> 0.3x
B_VOL_RATIO_TIERS = [
    (500_000_000, 1.5),   # > 5亿U
    (50_000_000,  1.2),   # > 5000万U
    (5_000_000,   0.8),   # > 500万U
    (500_000,     0.5),   # > 50万U
    (0,           0.3),   # 其余
]
B_VOL_RATIO_MIN      = 1.5   # 兜底默认值（查不到成交额时用）
B_VOL_RATIO_PUMP     = 2.5   # 标的过去6h涨幅>15%时，量比门槛提高至此
_vol_ratio_cache: dict = {}   # sym -> (threshold, ts)，缓存动态门槛避免频繁查询
B_PUMP_COOLDOWN_H    = 6     # 过去几小时内涨幅超过此门槛则触发提高量比
B_PUMP_THRESHOLD_PCT = 15.0  # 涨幅超过此%视为暴拉标的
B_RSI_MAX            = 75    # 1H RSI 超买阈值
PRICE_BREAK_PCT      = 0.002 # 站稳阻力位判断：收盘需高于阻力位 0.2%

_lock = threading.Lock()
_b_sl_tp_fired: set = set()   # Fix-2 防重入：已触发止盈止损兜底的 sym
_vol_log_suppress: dict = {}  # sym -> last_log_ts，量比不足日志冷却（60s/sym）
_pump_log_suppress: dict = {}  # sym -> last_log_ts，暴拉冷却日志抑制（300s/sym）

# ─────────────────────────────────────────────────────────────────────────────
# 状态 I/O
# ─────────────────────────────────────────────────────────────────────────────

def _fmt_price(sym: str, price: float, api=None) -> str:
    """把价格按交易所 tickSize 精度对齐，返回字符串。避免精度超限报错。"""
    import math as _m
    tick = 0.00001  # 默认
    try:
        if api:
            info = api.get_exchange_info()
            for s in info.get("symbols", []):
                if s["symbol"] == sym:
                    for f in s.get("filters", []):
                        if f["filterType"] == "PRICE_FILTER":
                            tick = float(f["tickSize"])
                            break
                    break
    except Exception:
        pass
    d = max(0, -int(_m.floor(_m.log10(tick)))) if tick > 0 else 5
    aligned = round(round(price / tick) * tick, d)
    return f"{aligned:.{d}f}"


def _log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    line = f"[{ts}][dual_net] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_pending() -> dict:
    """读取 pending_orders.json，结构：{sym: {a_order, b_order, meta}}"""
    if PENDING_FILE.exists():
        try:
            raw = json.loads(PENDING_FILE.read_text())
            # 兼容旧格式 {"orders": [...]}
            if isinstance(raw, dict) and "orders" in raw and isinstance(raw["orders"], list):
                return {}
            return raw if isinstance(raw, dict) else {}
        except Exception:
            pass
    return {}


def save_pending(data: dict):
    """原子写 pending_orders.json"""
    tmp = PENDING_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tmp.replace(PENDING_FILE)


# ─────────────────────────────────────────────────────────────────────────────
# 仓位计算（双网专用档位）
# ─────────────────────────────────────────────────────────────────────────────

def _dual_pos_usdt(score: float) -> float:
    """双网模式下 A单的名义仓位（轻量档位）"""
    return float(DUAL_POS_HIGH if score >= 85 else DUAL_POS_LOW)


# ─────────────────────────────────────────────────────────────────────────────
# 核心：部署双网挂单
# ─────────────────────────────────────────────────────────────────────────────

def deploy_dual_net(
    sym: str,
    direction: str,   # "LONG" / "SHORT"
    score: float,
    support_price: float,    # A单：技术支撑位（LONG） / 压力位（SHORT）
    breakout_price: float,   # B单：阻力位（LONG突破） / 支撑跌破（SHORT追空）
    tp_price: float,         # 止盈目标（Coinglass清算区）
    details: dict,
    api,
    notify_fn,
) -> dict:
    """
    同时部署 A单（回调限价）+ B单（突破条件单保存触发参数，不直接挂限价）
    B单在 ws_monitor handle_ticker 里实时监控突破后市价追入。
    """
    now = time.time()
    pos_a = _dual_pos_usdt(score)
    pos_b = round(pos_a * B_QTY_RATIO, 1)

    # 检查已有该 sym 的挂单，避免重复部署
    pending = load_pending()
    if sym in pending:
        existing = pending[sym]
        if existing.get("meta", {}).get("status") == "active":
            _log(f"{sym} 已有活跃双网挂单，跳过重复部署")
            return {"ok": False, "reason": "already_active"}

    # ── 优化1：偏离度检查——现价与 A单价偏离过大则不挂 ──────────────────────
    try:
        cur_ticker = api.request("GET", "https://fapi.binance.com/fapi/v1/ticker/price",
                                  {"symbol": sym})
        cur_price = float(cur_ticker.get("price", 0))
        if cur_price > 0:
            drift = abs(cur_price - support_price) / support_price
            if drift > A_DRIFT_MAX:
                _log(f"{sym} 跳过部署：现价{cur_price:.6f} 与A单{support_price:.6f} "
                     f"偏离{drift:.1%} > {A_DRIFT_MAX:.0%} 阈值")
                return {"ok": False, "reason": f"drift_too_large_{drift:.1%}"}
    except Exception as e:
        _log(f"{sym} 偏离检查异常（忽略）: {e}")

    # ── 优化2：每日撤单次数冷却检查 ──────────────────────────────────────────
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cancel_counter_file = Path(__file__).parent / "daily_cancel_counter.json"
    try:
        cancel_counter = json.loads(cancel_counter_file.read_text()) \
            if cancel_counter_file.exists() else {}
        # 清理昨天的数据
        cancel_counter = {k: v for k, v in cancel_counter.items()
                          if k.startswith(today_str)}
        key = f"{today_str}:{sym}"
        daily_cancels = cancel_counter.get(key, 0)
        if daily_cancels >= A_DAILY_CANCEL_MAX:
            _log(f"{sym} 跳过部署：今日已撤单 {daily_cancels} 次 >= 上限 {A_DAILY_CANCEL_MAX}，当天冷却")
            return {"ok": False, "reason": f"daily_cancel_limit_{daily_cancels}"}
    except Exception as e:
        _log(f"{sym} 撤单计数检查异常（忽略）: {e}")

    # ── 自动设置最大杠杆（节省保证金，仓位大小由pos_a控制）────────────────────
    try:
        lev_info = api.request("GET", "https://fapi.binance.com/fapi/v1/leverageBracket",
                               {"symbol": sym})
        brackets = lev_info[0]["brackets"] if isinstance(lev_info, list) else lev_info.get("brackets", [])
        max_lev = int(brackets[0].get("initialLeverage", 20)) if brackets else 20
        api.request("POST", "https://fapi.binance.com/fapi/v1/leverage",
                    {"symbol": sym, "leverage": max_lev})
        _log(f"{sym} 杠杆已设为最大 {max_lev}x")
    except Exception as _le:
        _log(f"{sym} 杠杆设置失败（继续）: {_le}")

    # ── A单：回调限价单 ────────────────────────────────────────────────────────
    a_qty  = _safe_qty(sym, pos_a, support_price, api)
    a_side = "BUY" if direction == "LONG" else "SELL"
    a_pos_side = direction

    r_a = api.place_order(
        sym, a_side, "LIMIT",
        price=str(support_price),
        quantity=a_qty,
        timeInForce="GTX",   # Post-only（回调接单，不主动扫单）
        positionSide=a_pos_side,
    )
    # GTX 可能被拒绝（价格超出范围），fallback 到 GTC
    if not r_a.get("orderId") and "GTX" in str(r_a.get("msg", "")):
        r_a = api.place_order(
            sym, a_side, "LIMIT",
            price=str(support_price),
            quantity=a_qty,
            timeInForce="GTC",
            positionSide=a_pos_side,
        )
    a_ok = bool(r_a.get("orderId"))
    a_order_id = r_a.get("orderId")

    # ── B单：保存突破参数，不挂单（由 ws_monitor 实时触发）──────────────────
    # 优化2: ATR止损 — 用 ATR×1.5 代替固定5%
    try:
        _k_atr = api.get_klines(sym, "1h", 20)
        _highs = [float(k[2]) for k in _k_atr]
        _lows  = [float(k[3]) for k in _k_atr]
        _cls   = [float(k[4]) for k in _k_atr]
        _b_atr = sum(max(_highs[i] - _lows[i],
                         abs(_highs[i] - _cls[i-1]) if i > 0 else 0,
                         abs(_lows[i]  - _cls[i-1]) if i > 0 else 0)
                     for i in range(len(_k_atr))) / max(len(_k_atr), 1)
    except Exception:
        _b_atr = breakout_price * 0.03  # 查不到时备用 3%
    b_sl_atr = _b_atr * B_SL_ATR_MULT
    if direction == "LONG":
        b_sl = round(breakout_price - b_sl_atr, 8)
    else:
        b_sl = round(breakout_price + b_sl_atr, 8)
    _log(f"{sym} B单ATR止损: atr={_b_atr:.6f} ×{B_SL_ATR_MULT} => sl={b_sl:.6f}")

    # 优化3: 突破强度仓位 — 突破幅度越大B单越重
    # 基础 = A单×0.70；每多突破0.5%加5%，上限+30%
    try:
        _cur_px = float(api.request("GET", "https://fapi.binance.com/fapi/v1/ticker/price",
                                     {"symbol": sym}).get("price", breakout_price))
        _break_pct = abs(_cur_px - breakout_price) / breakout_price * 100 if breakout_price > 0 else 0
        _strength_boost = min(1.0 + (_break_pct / 0.5) * 0.05, B_QTY_BOOST_MAX)
    except Exception:
        _strength_boost = 1.0
    _log(f"{sym} B单突破强度乘数: {_strength_boost:.2f}x (pre-breakout)")
    pos_b = round(pos_a * B_QTY_RATIO * _strength_boost, 1)
    b_qty = _safe_qty(sym, pos_b, breakout_price, api)

    # ── 有效期按信号类型动态设置 ───────────────────────────────────────────
    sig_type = details.get("signal_type", "")  # trend/counter/pin/preorder/b_order
    if "pin" in sig_type.lower():
        _expire_h = 0.5          # 插针信号：30分钟
        _recheck_interval = 5    # 插针复查频率：5分钟
    elif "trend" in sig_type.lower():
        _expire_h = 4            # 趋势信号：4小时
        _recheck_interval = 30
    elif "counter" in sig_type.lower():
        _expire_h = 2            # 逆势信号：2小时
        _recheck_interval = 30
    elif "preorder" in sig_type.lower():
        _expire_h = 4            # 条件单：4小时
        _recheck_interval = 30
    else:
        _expire_h = A_EXPIRE_H   # 默认：8小时
        _recheck_interval = 30
    expire_ts    = now + _expire_h * 3600
    protect_end  = now + PROTECT_H * 3600

    entry = {
        "sym":       sym,
        "direction": direction,
        "score":     score,
        "tp":        tp_price,
        "a_order": {
            "order_id":   a_order_id,
            "price":      support_price,
            "qty":        a_qty,
            "status":     "open" if a_ok else "failed",
            "placed_at":  now,
        },
        "b_order": {
            "trigger_price":   breakout_price,
            "qty":             b_qty,
            "sl":              b_sl,
            "status":          "watching",   # waiting for breakout signal
            "placed_at":       None,
            "order_id":        None,
            "signal_valid_until": None,      # 突破后 30min 内有效
        },
        "meta": {
            "status":          "active",     # active / a_filled / b_filled / expired / cancelled
            "placed_at":       now,
            "expire_at":       expire_ts,
            "protect_end":     protect_end,
            "recheck_interval": _recheck_interval,  # 评分复查间隔（分钟）
            "last_recheck_ts": now,                 # 上次复查时间戳
            "score_history":   [{"ts": now, "score": score}],
            "adjust_count_h":  0,            # 本小时调价次数
            "adjust_hour":     int(now // 3600),
            "last_adjust_ts":  0,
        },
    }

    with _lock:
        pending[sym] = entry
        save_pending(pending)

    _log(f"{'✅' if a_ok else '⚠️'} {sym} 双网已部署 | "
         f"A单@{support_price}({pos_a}U) B单触发@{breakout_price}({pos_b}U) "
         f"TP@{tp_price} 保护期至 {_fmt_ts(protect_end)}")

    # ── 推送通知（挂单/撤单静默，只记录日志）──────────────────────────────
    reason_short = details.get("reason", "双网信号")
    _log(f"[部署静默] {sym} 双网已挂 A@{support_price} B触发@{breakout_price} 评分={score:.0f}")

    return {"ok": a_ok, "entry": entry}


# ─────────────────────────────────────────────────────────────────────────────
# 互斥撤单：A成交→撤B  /  B成交→撤A
# ─────────────────────────────────────────────────────────────────────────────

def on_order_filled(sym: str, filled_side: str, filled_order_id, api, notify_fn):
    """
    ws_monitor open_filled 成交事件时调用。
    filled_side: "A" 或 "B"
    自动撤销另一侧挂单，并更新状态。
    Fix-1: B单成交时先查实时挂单获取真实 orderId 再撤 A单（防过期ID）
    Fix-2: B单成交后触发 _schedule_b_order_sl_tp 兜底止盈止损
    """
    with _lock:
        pending = load_pending()
        entry = pending.get(sym)
        if not entry or entry["meta"]["status"] not in ("active",):
            return  # 无活跃双网或已处理

        cancel_side = "B" if filled_side == "A" else "A"
        cancel_order = entry[f"{cancel_side.lower()}_order"]
        cancel_id = cancel_order.get("order_id")

        entry["meta"]["status"] = f"{filled_side.lower()}_filled"
        entry[f"{filled_side.lower()}_order"]["status"] = "filled"
        entry[f"{cancel_side.lower()}_order"]["status"] = "cancelling"
        # 捕获 B单实际均价和数量，供 Fix-2 使用
        b_avg_price = float(entry["b_order"].get("avg_price", 0))
        b_qty       = float(entry["b_order"].get("qty", 0))
        direction   = entry["direction"]
        save_pending(pending)

    # ── 互斥撤单 ─────────────────────────────────────────────────────────────
    # B单 watching 状态无实际挂单，优先判断，不调 API
    if cancel_side == "B" and cancel_order.get("status") in ("watching", "signal_pending"):
        cancelled = True
        _log(f"[mutual_cancel] {sym} B单为监控状态，无需撤单")
    elif cancel_side == "A":
        # Fix-1: 先实时查挂单，用当前真实 orderId 而非 pending 里可能过期的 ID
        cancelled = False
        try:
            live_orders = api.get_open_orders(sym)
            buy_orders  = [o for o in (live_orders or [])
                           if o.get("side") == "BUY" and not o.get("reduceOnly")]
            if not buy_orders:
                # 挂单已不存在（可能已成交/已撤），视为成功
                _log(f"[mutual_cancel] {sym} A单已不在挂单列表，视为撤单成功")
                cancelled = True
            else:
                live_id = buy_orders[0]["orderId"]
                if str(live_id) != str(cancel_id):
                    _log(f"[mutual_cancel] {sym} A单ID已更新 {cancel_id}→{live_id}，用新ID撤单")
                r = api.cancel_order(sym, int(live_id))
                cancelled = bool(r.get("orderId") or r.get("status") == "CANCELED")
                if not cancelled:
                    code = int(r.get("code", 0)) if r.get("code") else 0
                    if code in (-2011, -2013):   # 已成交/已撤
                        cancelled = True
                        _log(f"[mutual_cancel] {sym} A单 code={code}，视为已处理")
        except Exception as e:
            _log(f"[mutual_cancel] {sym} 撤A单异常: {e}")
    elif cancel_id:
        cancelled = False
        try:
            r = api.cancel_order(sym, int(cancel_id))
            cancelled = bool(r.get("orderId") or r.get("status") == "CANCELED")
        except Exception as e:
            _log(f"[mutual_cancel] {sym} 撤{cancel_side}单异常: {e}")
    else:
        cancelled = True  # 无 orderId，视为已撤

    with _lock:
        pending = load_pending()
        if sym in pending:
            pending[sym][f"{cancel_side.lower()}_order"]["status"] = "cancelled" if cancelled else "cancel_failed"
            save_pending(pending)

    side_name   = "回调单" if filled_side == "A" else "突破单"
    cancel_name = "突破单" if filled_side == "A" else "回调单"
    notify_fn(
        f"✅ 双网成交 [{sym}]\n"
        f"{side_name}已成交，{cancel_name}{'已撤销' if cancelled else '撤销失败⚠️'}\n"
        f"成交方式: {'回调接入' if filled_side == 'A' else '突破追入'}"
    )
    _log(f"{sym} {filled_side}单成交 → {cancel_side}单{'已撤' if cancelled else '撤销失败'}")

    # Fix-2: B单成交 → 异步触发止盈止损兜底（不依赖 ws_monitor 的 _on_open_filled）
    # _b_sl_tp_fired 防重入：check_breakout 和 ws_monitor 两条路径可能都调用 on_order_filled
    if filled_side == "B":
        # Fix-3: b_avg_price=0 时（ws event ap字段未填充）查询实际均价
        _actual_avg = b_avg_price
        if _actual_avg <= 0:
            try:
                _pos_list = api.get_positions()
                _pos_match = [p for p in _pos_list
                              if p.get("symbol") == sym
                              and abs(float(p.get("positionAmt", 0))) > 0]
                if _pos_match:
                    _actual_avg = float(_pos_match[0].get("entryPrice", 0))
                    _log(f"[b_sl_tp] {sym} b_avg_price=0，从持仓查到实际入场价={_actual_avg}")
            except Exception as _ae:
                _log(f"[b_sl_tp] {sym} 查询实际均价失败: {_ae}")
            # 再兜底：用 mark price
            if _actual_avg <= 0:
                try:
                    _actual_avg = float(api.get_mark_price(sym).get("markPrice", 0))
                    _log(f"[b_sl_tp] {sym} 用mark price兜底: {_actual_avg}")
                except Exception:
                    pass
        if _actual_avg > 0 and b_qty > 0:
            with _lock:
                if sym in _b_sl_tp_fired:
                    _log(f"[b_sl_tp] {sym} 兜底已在运行，跳过重复触发")
                    return
                _b_sl_tp_fired.add(sym)
            def _run_and_clear():
                try:
                    _schedule_b_order_sl_tp(sym, _actual_avg, b_qty, direction, api, notify_fn)
                finally:
                    with _lock:
                        _b_sl_tp_fired.discard(sym)
            threading.Thread(target=_run_and_clear, daemon=True).start()
        else:
            _log(f"[b_sl_tp_skip] {sym} avg_price和b_qty均无效(avg={_actual_avg} qty={b_qty})，兜底跳过")


def on_both_filled(sym: str, api, notify_fn):
    """
    两单同时成交的异常处理：立刻平掉后成交的那单（B单，因为追高风险大）
    """
    _log(f"[WARN] {sym} 双单同时成交，紧急平掉 B单仓位")
    try:
        pos = [p for p in api.get_positions()
               if p["symbol"] == sym and abs(float(p.get("positionAmt", 0))) > 0]
        if pos:
            p = pos[0]
            amt = abs(float(p["positionAmt"]))
            side = "SELL" if float(p["positionAmt"]) > 0 else "BUY"
            api.place_order(sym, side, "MARKET", quantity=amt,
                            positionSide=p.get("positionSide", "LONG"))
    except Exception as e:
        _log(f"[both_filled_err] {sym}: {e}")
    notify_fn(
        f"⚠️ 双网异常 [{sym}]\n"
        f"A单和B单同时成交！已紧急平掉B单仓位\n"
        f"请检查仓位状态"
    )


# ─────────────────────────────────────────────────────────────────────────────
def _get_dynamic_vol_threshold(sym: str, api) -> float:
    """
    根据标的 24h 合约成交额动态返回量比门槛。
    结果缓存 5 分钟，避免频繁请求。
    """
    now = time.time()
    cached = _vol_ratio_cache.get(sym)
    if cached and now - cached[1] < 300:  # 5分钟缓存
        return cached[0]
    try:
        ticker = api.request("GET", "https://fapi.binance.com/fapi/v1/ticker/24hr",
                             {"symbol": sym})
        quote_vol = float(ticker.get("quoteVolume", 0))
        threshold = B_VOL_RATIO_MIN  # 默认山底
        for min_vol, thresh in B_VOL_RATIO_TIERS:
            if quote_vol >= min_vol:
                threshold = thresh
                break
        _vol_ratio_cache[sym] = (threshold, now)
        _log(f"{sym} 动态量比门槛: 24h成交额={quote_vol/10000:.0f}万U -> 门槛={threshold}x")
        return threshold
    except Exception as e:
        _log(f"{sym} 动态量比检索异常({e})，使用默认{B_VOL_RATIO_MIN}")
        return B_VOL_RATIO_MIN


# B单突破检测（由 ws_monitor handle_ticker 实时调用）
# ─────────────────────────────────────────────────────────────────────────────

def check_breakout(sym: str, price: float, vol_ratio: float,
                   rsi_1h: float, kline_close: float,
                   api, notify_fn,
                   is_kline_closed: bool = False) -> bool:
    """
    每次 ticker 更新时调用，检查 B单突破条件是否满足。
    vol_ratio: 当前成交量 / 过去N根K线平均量
    rsi_1h: 1H RSI 当前值
    kline_close: 当前1H K线收盘价（优化1: 应传已收盘K线的收盘价）
    is_kline_closed: True = kline_close 是真实已收盘K线价，False = 降级为ticker价

    优化1: 收盘确认 — 触发后如果当前K线未收盘，仅标记 signal_pending，
           等K线收盘站稳后再市价追入，避免插针假突破。

    返回 True = 已触发 B单市价追入
    """
    pending = load_pending()
    entry = pending.get(sym)
    if not entry or entry["meta"]["status"] != "active":
        return False

    b = entry["b_order"]
    if b["status"] not in ("watching", "signal_pending"):
        return False

    trigger = b["trigger_price"]
    direction = entry["direction"]

    # 检查突破条件
    if direction == "LONG":
        price_break = price >= trigger and kline_close >= trigger * (1 + PRICE_BREAK_PCT)
    else:
        price_break = price <= trigger and kline_close <= trigger * (1 - PRICE_BREAK_PCT)

    if not price_break:
        return False

    # ── 优化1: 5分钟K线收盘确认 ──────────────────────────────────────────────
    # 价格触及触发位后，等最近一根5分钟K线收盘价也站稳，再市价追入
    # 比1H确认快得多（最多等5分钟），又能过滤插针假突破
    try:
        _k5m = api.get_klines(sym, "5m", 2)
        if _k5m and len(_k5m) >= 1:
            # 取已收盘的那根（倒数第2根，最新根可能未收盘）
            _5m_close = float(_k5m[-2][4])  # 已收盘5m K线收盘价
            if direction == "LONG":
                _5m_ok = _5m_close >= trigger * (1 + PRICE_BREAK_PCT)
            else:
                _5m_ok = _5m_close <= trigger * (1 - PRICE_BREAK_PCT)
        else:
            _5m_ok = True  # 拉不到K线时不阻塞
    except Exception:
        _5m_ok = True  # 异常时不阻塞

    if not _5m_ok:
        if b["status"] == "watching":
            with _lock:
                pending2 = load_pending()
                if sym in pending2:
                    pending2[sym]["b_order"]["status"] = "signal_pending"
                    save_pending(pending2)
            _log(f"{sym} B单价格触及触发位但5m K线未收盘站稳，等5m收盘确认 (signal_pending)")
        return False
    # 5分钟K线已收盘站稳 → 正式触发
    if b["status"] == "signal_pending":
        _log(f"{sym} B单5m收盘确认站稳 (signal_pending→触发)")

    # ── 动态量比门槛：根据标的 24h 成交额自动调整 ──────────────────
    # 大市值(>5亿U)=1.5x  中市值(>5000万)=1.2x  小市值(>500万)=0.8x  微市值(>50万)=0.5x  极小=0.3x
    _effective_vol_min = _get_dynamic_vol_threshold(sym, api)

    # ── 暴拉冷却：若标的短期暴拉，在动态门槛基础上进一步提高至 PUMP 默认值 ──
    try:
        _k6h = api.get_klines(sym, "1h", B_PUMP_COOLDOWN_H + 2)
        if len(_k6h) >= B_PUMP_COOLDOWN_H:
            _lo6 = min(float(k[3]) for k in _k6h[-B_PUMP_COOLDOWN_H:])
            _hi6 = max(float(k[2]) for k in _k6h[-B_PUMP_COOLDOWN_H:])
            _pump_pct = (_hi6 - _lo6) / _lo6 * 100 if _lo6 > 0 else 0
            if _pump_pct >= B_PUMP_THRESHOLD_PCT:
                _effective_vol_min = max(_effective_vol_min, B_VOL_RATIO_PUMP)
                _now_t = time.time()
                if _now_t - _pump_log_suppress.get(sym, 0) >= 300:  # 5分钟打一次
                    _log(f"{sym} B单暴拉冷却: 过去{B_PUMP_COOLDOWN_H}h涨幅={_pump_pct:.1f}%超过{B_PUMP_THRESHOLD_PCT}%，量比门槛提高至{B_VOL_RATIO_PUMP}x")
                    _pump_log_suppress[sym] = _now_t
    except Exception as _pe:
        _log(f"{sym} 暴拉冷却检查异常(非致命): {_pe}")
    # ─────────────────────────────────────────────────────────

    if vol_ratio < _effective_vol_min:
        # ── 绝对量兜底：相对量比不足时，用绝对成交额补充判断 ────────────────
        abs_vol_ok = False
        try:
            # 条件1：当前1H绝对成交额 > 2亿U
            _k1h_vol = api.get_klines(sym, "1h", 2)
            cur_1h_vol = float(_k1h_vol[-1][7]) if _k1h_vol else 0
            if cur_1h_vol > 200_000_000:
                abs_vol_ok = True
                _log(f"{sym} B单量比免局: 1H绝对量={cur_1h_vol/1e6:.0f}M > 200M")
            # 条件2：5min成交额 > 5000万U 且较前5min增加>50%
            if not abs_vol_ok:
                _k5m_vol = api.get_klines(sym, "5m", 3)
                if len(_k5m_vol) >= 2:
                    cur_5m  = float(_k5m_vol[-1][7])
                    prev_5m = float(_k5m_vol[-2][7])
                    if cur_5m > 50_000_000 and prev_5m > 0 and (cur_5m - prev_5m) / prev_5m > 0.5:
                        abs_vol_ok = True
                        _log(f"{sym} B单量比免局: 5min量={cur_5m/1e6:.0f}M 增{(cur_5m-prev_5m)/prev_5m:.0%}")
        except Exception as _ve:
            _log(f"{sym} B单绝对量检查异常: {_ve}")

        if not abs_vol_ok:
            _now = time.time()
            if _now - _vol_log_suppress.get(sym, 0) >= 60:
                _log(f"{sym} B单: 量比不足 {vol_ratio:.2f} < {_effective_vol_min}，绝对量也未达标")
                _vol_log_suppress[sym] = _now
            return False
    if rsi_1h > B_RSI_MAX:
        _log(f"{sym} B单: 价格突破但 RSI 超买 {rsi_1h:.1f} > {B_RSI_MAX}")
        return False

    # ── 执行 B单市价追入 ──────────────────────────────────────────────────────
    # 优化3 (运行时补充): 根据实际突破幅度再次调整仓位
    # deploy时用的是挂单前的预估幅度，这里用K线收盘价算真实突破幅度
    b_qty = b["qty"]
    try:
        _actual_break_pct = abs(kline_close - trigger) / trigger * 100 if trigger > 0 else 0
        _rt_boost = min(1.0 + (_actual_break_pct / 0.5) * 0.05, B_QTY_BOOST_MAX)
        # 如果运行时幅度比部署时更大，适当增加；否则保持不变（不减少）
        _rt_qty_raw = b["qty"] * _rt_boost
        if _rt_qty_raw > b["qty"] * 1.05:  # 只在增加>5%时才调整，避免频繁微调
            from entry_logic import atr_calc as _atr_calc
            b_qty_new = _safe_qty(sym, entry.get("meta", {}).get("pos_b_usdt", 0) or _rt_qty_raw * trigger, trigger, api)
            if b_qty_new > 0:
                b_qty = b_qty_new
                _log(f"{sym} B单运行时仓位调整: 突破{_actual_break_pct:.1f}% boost={_rt_boost:.2f}x qty={b_qty}")
    except Exception:
        pass  # 调整失败不影响主流程
    b_side = "BUY" if direction == "LONG" else "SELL"
    r = api.place_order(sym, b_side, "MARKET", quantity=b_qty,
                        positionSide=direction)
    b_ok = bool(r.get("orderId"))

    if b_ok:
        avg_price = float(r.get("avgPrice") or r.get("price") or price)
        with _lock:
            pending = load_pending()
            if sym in pending:
                pending[sym]["b_order"]["status"] = "filled"
                pending[sym]["b_order"]["order_id"] = r.get("orderId")
                pending[sym]["b_order"]["placed_at"] = time.time()
                pending[sym]["b_order"]["avg_price"] = avg_price
                # 30分钟信号有效期标记
                pending[sym]["b_order"]["signal_valid_until"] = time.time() + B_EXPIRE_MIN * 60
                save_pending(pending)

        _log(f"{sym} B单突破成交 qty={b_qty} @market")

        # Fix-3: B单成交后立刻写 pos_state，让 _on_open_filled 能读到完整信息
        _write_pos_state_for_b_order(sym, direction, avg_price, float(b_qty), entry)

        # 互斥撤 A单
        on_order_filled(sym, "B", r.get("orderId"), api, notify_fn)
        return True
    else:
        _log(f"{sym} B单市价下单失败: {r}")
        return False


def _write_pos_state_for_b_order(
    sym: str, direction: str, avg_price: float, qty: float, entry: dict
):
    """
    Fix-3: B单市价成交后立刻将开仓信息写入 pos_state.json。
    这样 ws_monitor._on_open_filled 能读到 entry_price/qty/direction，
    避免 Coinglass 查询时因 pos_state 为空而导致止损止盈失败。
    """
    try:
        _ps_store.upsert(sym, {
            'entry_price': avg_price,
            'qty':         qty,
            'direction':   direction,
            'source':      'dual_net_b',
            'sl_price':    _ps_store.read(sym).get('sl_price',  0),
            'tp1_price':   _ps_store.read(sym).get('tp1_price', 0),
            'tp2_price':   _ps_store.read(sym).get('tp2_price', 0),
        })
        _log(f"{sym} pos_state 已写入 entry={avg_price} qty={qty} dir={direction}")
    except Exception as e:
        _log(f"{sym} pos_state 写入失败（非致命）: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# 保护期评分检查（scanner_loop 每次重评分后调用）
# ─────────────────────────────────────────────────────────────────────────────

def check_protection(
    sym: str,
    new_score: float,
    btc_drop_15m: float,    # BTC 15分钟跌幅（负值 = 下跌）
    funding_rate: float,
    has_reversal: bool,     # 是否出现反转信号
    support_broken: bool,   # 支撑/压力位是否被突破
    new_support: float,     # 新的支撑/压力位（用于调价）
    api,
    notify_fn,
) -> str:
    """
    评分复查 + 保护期逻辑。
    返回：'protected' | 'adjusted' | 'cancelled' | 'expired' | 'downgraded' | 'extended'
    """
    pending = load_pending()
    entry = pending.get(sym)
    if not entry or entry["meta"]["status"] not in ("active",):
        return "not_found"

    meta = entry["meta"]
    direction = entry["direction"]
    now = time.time()

    # ── 评分复查频率控制：未到复查时间则跳过（强制撤单和到期不受限） ─────
    _recheck_min = meta.get("recheck_interval", 30)  # 默认 30 分钟
    _last_recheck = meta.get("last_recheck_ts", 0)
    _skip_recheck = (now - _last_recheck) < _recheck_min * 60

    # ── 到期检查（优先，不受复查频率限制）───────────────────────────────
    if now > meta["expire_at"]:
        _cancel_both(sym, entry, "超过有效期", api, notify_fn, silent=False)
        return "expired"

    # ── 更新评分历史 ──────────────────────────────────────────────────────────

    in_protect = now < meta["protect_end"]

    # ── 强制撤单例外（无论保护期，立刻撤）───────────────────────────────────
    force_cancel_reason = _check_force_cancel(
        direction, btc_drop_15m, funding_rate, has_reversal, support_broken
    )
    if force_cancel_reason:
        _cancel_both(sym, entry, force_cancel_reason, api, notify_fn, silent=False)
        with _lock:
            pending = load_pending()
            if sym in pending:
                del pending[sym]
                save_pending(pending)
        return "cancelled"

    # ── 分级撤单逻辑（按挂单存活时长动态调整门槛）────────────────────────
    age_min = (now - meta["placed_at"]) / 60  # 挂单存活分钟数

    if age_min < 30:
        # 冷静期 0~30min：只有评分崩塌才撤（50分），不因短期波动撤单
        score_threshold = 45  # 强制撤线
        cancel_reason = f"冷静期评分崩塌 {new_score:.0f} < 45" if new_score < score_threshold else ""
    elif age_min < 120:
        # 30min~2h：维持原有 60 分门槛
        score_threshold = SCORE_CANCEL  # 60
        cancel_reason = f"评分跌破 {score_threshold} (now={new_score:.0f})" if new_score < score_threshold else ""
    else:
        # 2h 以上：提高到 65 分，说明信号已衰减明显才撤
        score_threshold = 65
        cancel_reason = f"挂单>2h评分跌破65 (now={new_score:.0f})" if new_score < score_threshold else ""

    # 绝对底线：任何阶段评分跌破 45 立刻撤
    if new_score < 45:
        cancel_reason = f"评分跌破绝对底线45 (now={new_score:.0f})"

    if cancel_reason:
        _cancel_both(sym, entry, cancel_reason, api, notify_fn, silent=False)
        with _lock:
            pending = load_pending()
            if sym in pending:
                del pending[sym]
                save_pending(pending)
        return "cancelled"

    # ── 评分复查频率屏蔽：尊重 recheck_interval，跳过不必要的频繁检查 ────────
    if _skip_recheck:
        return "skip_recheck"

    # 更新评分历史 + 复查时间戳
    meta["score_history"].append({"ts": now, "score": new_score})
    meta["score_history"] = meta["score_history"][-20:]
    meta["last_recheck_ts"] = now

    # ── 保护期内：评分在 60~100，考虑调价 ───────────────────────────────────
    if in_protect:
        if support_broken and new_support > 0:
            _try_adjust_price(sym, entry, new_support, direction, api, notify_fn)
            return "adjusted"
        with _lock:
            pending[sym] = entry
            save_pending(pending)
        return "protected"

    # ── 保护期结束，评分复查 ──────────────────────────────────────────────────
    if new_score >= SCORE_MIN:
        # 续期 1 小时
        meta["protect_end"] = now + 3600
        meta["expire_at"] = now + A_EXPIRE_H * 3600  # 有效期重置
        with _lock:
            pending[sym] = entry
            save_pending(pending)
        _log(f"{sym} 保护期续期 1h（评分 {new_score:.0f} >= {SCORE_MIN}）")
        return "extended"

    elif new_score >= SCORE_CANCEL:
        # 降级为普通观察状态
        meta["status"] = "downgraded"
        with _lock:
            pending[sym] = entry
            save_pending(pending)
        _log(f"[降级静默] {sym} 评分{new_score:.0f} 已降为普通观察状态")
        return "downgraded"

    else:
        _cancel_both(sym, entry, f"保护期结束后评分 {new_score:.0f} < {SCORE_CANCEL}",
                     api, notify_fn, silent=False)
        return "cancelled"


def _check_force_cancel(
    direction, btc_drop_15m, funding_rate,
    has_reversal, support_broken,
    api=None,                     # Fix P0-4: 需要查实时仓位/熔断
    oi_change_1h: float = 0.0,    # Fix P0-5: OI 1小时变化率，正值=净流入
) -> str:
    """
    检查强制撤单例外条件，返回原因字符串，无异常返回空字符串。
    Fix P0-4: 增加熔断检查 + 仓位上限检查（LONG≤5 / SHORT≤1）
    Fix P0-5: 空单增加 OI 急增（聪明钱大幅净流入）例外
    """
    # ── Fix P0-4: 熔断中 → 无论方向都强制撤 ──────────────────────────────────
    try:
        from risk_manager import load_circuit as _lc
        if _lc().get("fused", False):
            return "熔断触发，强制撤单"
    except Exception:
        pass

    # ── Fix P0-4: 仓位上限检查 ────────────────────────────────────────────────
    if api is not None:
        try:
            _positions = api.get_positions()
            _active    = [p for p in _positions if abs(float(p.get("positionAmt", 0))) > 0]
            _long_cnt  = sum(1 for p in _active if float(p.get("positionAmt", 0)) > 0)
            _short_cnt = sum(1 for p in _active if float(p.get("positionAmt", 0)) < 0)
            if direction == "LONG"  and _long_cnt  >= 5:
                return f"多单仓位已达上限（{_long_cnt}/5），撤挂单释放资金"
            if direction == "SHORT" and _short_cnt >= 1:
                return f"空单仓位已达上限（{_short_cnt}/1），撤挂单"
        except Exception:
            pass

    if direction == "LONG":
        if btc_drop_15m < -0.03:
            return f"BTC 15min 下跌 {btc_drop_15m*100:.1f}%（>3%）"
        if funding_rate > 0.005:
            return f"资金费率极度正值 {funding_rate*100:.3f}%（>+0.5%）"
        if has_reversal:
            return "出现下跌反转信号"
        if support_broken:
            return "关键支撑位跌破"
    else:  # SHORT
        if btc_drop_15m > 0.03:
            return f"BTC 15min 上涨 {btc_drop_15m*100:.1f}%（>3%）"
        if funding_rate < -0.003:
            return f"资金费率极度负值 {funding_rate*100:.3f}%（<-0.3%）"
        if has_reversal:
            return "出现上涨反转信号"
        # Fix P0-5: OI 1h 急增 >20% → 聪明钱大幅净流入，空单风险极大
        if oi_change_1h > 0.20:
            return f"OI 1h 急增 {oi_change_1h*100:.0f}%（>20%，聪明钱净流入）"
    return ""


def _try_adjust_price(sym: str, entry: dict, new_price: float,
                      direction: str, api, notify_fn):
    """保护期内调整 A单挂单价格（最多 2%，每小时最多 2 次，静默）"""
    meta = entry["meta"]
    now = time.time()
    cur_hour = int(now // 3600)

    # 重置小时计数器
    if meta["adjust_hour"] != cur_hour:
        meta["adjust_count_h"] = 0
        meta["adjust_hour"] = cur_hour

    if meta["adjust_count_h"] >= 2:
        _log(f"{sym} 本小时调价次数已达上限（2次），跳过")
        return

    a_order = entry["a_order"]
    old_price = a_order["price"]
    max_chg = old_price * 0.02
    if abs(new_price - old_price) > max_chg:
        # 限制调幅
        new_price = (old_price + max_chg) if new_price > old_price else (old_price - max_chg)
        new_price = round(new_price, 6)

    # 撤旧单
    if a_order.get("order_id"):
        try:
            api.cancel_order(sym, int(a_order["order_id"]))
        except Exception as e:
            _log(f"{sym} 调价撤旧单失败: {e}")
            return

    # 挂新单
    a_qty = a_order["qty"]
    a_side = "BUY" if direction == "LONG" else "SELL"
    r = api.place_order(sym, a_side, "LIMIT", price=str(new_price),
                        quantity=a_qty, timeInForce="GTC",
                        positionSide=direction)
    if r.get("orderId"):
        a_order["order_id"] = r["orderId"]
        a_order["price"]    = new_price
        meta["adjust_count_h"] += 1
        meta["last_adjust_ts"]  = now
        meta["expire_at"]       = now + A_EXPIRE_H * 3600  # 有效期重置
        _log(f"{sym} A单调价: {old_price} → {new_price}（静默）")
    else:
        _log(f"{sym} A单调价挂单失败: {r}")


def _cancel_both(sym: str, entry: dict, reason: str, api, notify_fn, silent: bool = False):
    """撤销 A单和 B单（B单如是watching则直接标记）"""
    a = entry.get("a_order", {})
    b = entry.get("b_order", {})

    a_cancelled = False
    if a.get("order_id") and a.get("status") == "open":
        try:
            api.cancel_order(sym, int(a["order_id"]))
            a_cancelled = True
        except Exception as e:
            _log(f"{sym} 撤A单失败: {e}")

    b_cancelled = b.get("status") in ("watching", "signal_pending") or bool(b.get("order_id") is None)
    if not b_cancelled and b.get("order_id"):
        try:
            api.cancel_order(sym, int(b["order_id"]))
            b_cancelled = True
        except Exception as e:
            _log(f"{sym} 撤B单失败: {e}")

    score_now = entry["meta"]["score_history"][-1]["score"] if entry["meta"]["score_history"] else "?"

    # ── 优化2：记录该标的当日撤单次数 ───────────────────────────────────────
    try:
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        cancel_counter_file = Path(__file__).parent / "daily_cancel_counter.json"
        counter = json.loads(cancel_counter_file.read_text()) \
            if cancel_counter_file.exists() else {}
        counter = {k: v for k, v in counter.items() if k.startswith(today_str)}
        key = f"{today_str}:{sym}"
        counter[key] = counter.get(key, 0) + 1
        cancel_counter_file.write_text(json.dumps(counter, indent=2))
    except Exception:
        pass

    if not silent:
        _log(f"{sym} 双网撤单[静默]: {reason} 评分={score_now}")
    _log(f"{sym} 双网撤单: {reason}")


# ─────────────────────────────────────────────────────────────────────────────
# 定期巡检（scanner_loop 每 30min 调用）
# ─────────────────────────────────────────────────────────────────────────────

def patrol_pending(api, notify_fn):
    """清理超期、检查 A单是否还在交易所挂单上（防漏单）"""
    pending = load_pending()
    changed = False
    now = time.time()

    for sym, entry in list(pending.items()):
        meta = entry["meta"]
        if meta["status"] not in ("active", "downgraded"):
            continue

        # 到期清理
        if now > meta["expire_at"]:
            _cancel_both(sym, entry, "巡检：A单超过8小时未成交", api, notify_fn, silent=False)
            del pending[sym]
            changed = True
            continue

        # 确认 A单还在 REST 挂单上
        a = entry["a_order"]
        if a.get("order_id") and a.get("status") == "open":
            try:
                r = api.get_order(sym, int(a["order_id"]))
                if r.get("status") in ("CANCELED", "EXPIRED", "FILLED"):
                    _log(f"{sym} A单 REST 确认已 {r['status']}，更新本地状态")
                    a["status"] = r["status"].lower()
                    if r["status"] == "FILLED":
                        on_order_filled(sym, "A", a["order_id"], api, notify_fn)
                    changed = True
            except Exception as e:
                _log(f"{sym} A单巡检查询失败: {e}")

    if changed:
        save_pending(pending)


# ─────────────────────────────────────────────────────────────────────────────
# 辅助函数
# ─────────────────────────────────────────────────────────────────────────────

def _safe_qty(sym: str, notional: float, price: float, api) -> float:
    """根据名义金额和价格计算合规数量"""
    import math
    try:
        info = api.request("GET", f"{api.BASE_API}/fapi/v1/exchangeInfo",
                           {"symbol": sym})
        filters = {}
        for f in (info.get("symbols") or [{}])[0].get("filters", []):
            if f["filterType"] == "LOT_SIZE":
                filters["step"] = float(f["stepSize"])
        step = filters.get("step", 1.0)
        qty = math.floor((notional / price) / step) * step
        dec = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
        return round(qty, dec)
    except Exception:
        return round(notional / price, 3)


def _fmt_ts(ts: float) -> str:
    return datetime.utcfromtimestamp(ts).strftime("%H:%M UTC")


def reset_failed_pending(sym: str):
    """
    A单已失效且当前无法重挂时（WAIT模式），清除 pending 状态让该币重新进入 watchlist 流程。
    """
    with _lock:
        pending = load_pending()
        if sym in pending:
            pending.pop(sym)
            save_pending(pending)
            _log(f"[reset_pending] {sym} A单已失效且无法重挂，已清除 pending 状态")


def get_active_syms() -> list:
    """返回当前有活跃双网挂单的 symbol 列表。
    A单已 canceled/failed/None 的视为无效，不包含在内。
    """
    result = []
    for s, e in load_pending().items():
        if not isinstance(e, dict): continue
        if e.get("meta", {}).get("status") not in ("active", "downgraded"): continue
        a = e.get("a_order", {})
        a_status = a.get("status", "")
        a_id = a.get("order_id")
        # A单已失效→不算 active，让 scanner 重新评估
        if a_status in ("canceled", "failed", "cancel_failed") or not a_id:
            continue
        result.append(s)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Fix-1: scanner 撤旧重挂后同步更新 pending_orders 里的 a_order.order_id
# ─────────────────────────────────────────────────────────────────────────────

def sync_a_order_id(sym: str, new_order_id, new_price: float = 0):
    """
    scanner 每次撤旧重挂 A单后调用。
    更新 pending_orders.json 里该 sym 的 a_order.order_id，
    防止 B单成交时互斥撤单使用过期 orderId 导致撤单失败。
    """
    with _lock:
        pending = load_pending()
        entry = pending.get(sym)
        if not entry or entry["meta"]["status"] not in ("active", "downgraded"):
            return  # 该 sym 无活跃双网，无需同步
        old_id = entry["a_order"].get("order_id")
        entry["a_order"]["order_id"] = new_order_id
        if new_price > 0:
            entry["a_order"]["price"] = new_price
        entry["a_order"]["status"] = "open"
        save_pending(pending)
    _log(f"[sync_a_order_id] {sym} A单ID更新: {old_id} → {new_order_id}")


# ─────────────────────────────────────────────────────────────────────────────
# Fix-2: B单成交后兜底止盈止损（独立于 ws_monitor._on_open_filled）
# ─────────────────────────────────────────────────────────────────────────────

def _schedule_b_order_sl_tp(
    sym: str, avg_price: float, qty: float, direction: str, api, notify_fn
):
    """
    B单市价成交后 5 秒延迟触发，给 ws_monitor._on_open_filled 一个先跑的窗口。
    若 5 秒后仍无止损单 / 止盈单，则用 Coinglass 独立补挂，兜底保护。
    """
    import json as _json, coinglass_api as _cg
    POS_STATE_FILE = WORKSPACE / "pos_state.json"

    time.sleep(5)   # 让 _on_open_filled 先跑

    # 检查是否已有止损单
    try:
        algo_orders = api.get_algo_open_orders(sym) or []
    except Exception:
        algo_orders = []
    has_sl = any("STOP" in o.get("orderType", "") for o in algo_orders)
    # TP 可能是 algo TAKE_PROFIT 或普通 LIMIT 减仓单，两种都要检查
    has_tp_algo = any("TAKE_PROFIT" in o.get("orderType", "") for o in algo_orders)
    try:
        open_orders = api.get_open_orders(sym) or []
    except Exception:
        open_orders = []
    has_tp_limit = any(
        o.get("type") == "LIMIT" and o.get("reduceOnly")
        for o in (open_orders if isinstance(open_orders, list) else [])
    )
    has_tp = has_tp_algo or has_tp_limit

    if has_sl and has_tp:
        _log(f"[b_sl_tp] {sym} 止损止盈已就位，兜底跳过")
        return

    _log(f"[b_sl_tp] {sym} 检测到缺少 {'SL' if not has_sl else ''} {'TP' if not has_tp else ''}，开始兜底补挂")

    close_side = "SELL" if direction == "LONG" else "BUY"
    mark = float(api.get_mark_price(sym).get("markPrice", avg_price))

    # ── ATR 计算 ──────────────────────────────────────────────────────────────
    try:
        k1h   = api.get_klines(sym, "1h", 20)
        highs = [float(k[2]) for k in k1h]
        lows  = [float(k[3]) for k in k1h]
        cls   = [float(k[4]) for k in k1h]
        atr   = sum(max(highs[i] - lows[i],
                        abs(highs[i] - cls[i-1]) if i > 0 else 0,
                        abs(lows[i]  - cls[i-1]) if i > 0 else 0)
                    for i in range(len(k1h))) / max(len(k1h), 1)
    except Exception:
        atr = avg_price * 0.03

    # ── Coinglass 锚定 SL/TP ──────────────────────────────────────────────────
    try:
        liq   = _cg.get_liq_zones(sym, mark)
        below = liq.get("below", [])
        above = liq.get("above", [])

        # TP 最小距离保护（入场价的百分比）
        TP1_MIN_PCT = 0.02   # TP1 至少离入场价 2%
        TP2_MIN_PCT = 0.04   # TP2 至少离入场价 4%

        def _pick_best_liq(zones, min_vol=0):
            """按量排序，取前两个满足条件的区。无门槛时取最大量的两个。"""
            filtered = sorted(
                [z for z in zones if float(z[1]) >= min_vol],
                key=lambda z: float(z[1]), reverse=True
            )
            if len(filtered) < 2 and min_vol > 0:
                # 门槛太高没数据，降级为按量排序取最大的两个
                filtered = sorted(zones, key=lambda z: float(z[1]), reverse=True)
            return filtered

        if direction == "LONG":
            # SL: 下方最大量清算区外侧
            sl_candidates = _pick_best_liq(below, 1_000_000)
            best_sl  = sl_candidates[0] if sl_candidates else None
            sl_raw   = float(best_sl[0]) - atr * 0.5 if best_sl else avg_price * (1 - TP1_MIN_PCT * 1.5)
            sl_price = min(sl_raw, avg_price * 0.999)

            # TP1/TP2: 上方清算区，按量排序取最大两个，但必须高于现价且满足最小距离
            tp_candidates = _pick_best_liq([z for z in above if float(z[0]) > mark], 0)
            tp_min1 = avg_price * (1 + TP1_MIN_PCT)
            tp_min2 = avg_price * (1 + TP2_MIN_PCT)
            tp1_zone = next((z for z in sorted(tp_candidates, key=lambda z: float(z[0]))
                             if float(z[0]) * 0.999 >= tp_min1), None)
            tp2_zone = next((z for z in sorted(tp_candidates, key=lambda z: float(z[0]))
                             if float(z[0]) * 0.999 >= tp_min2 and (tp1_zone is None or float(z[0]) > float(tp1_zone[0]))), None)
            tp1 = float(tp1_zone[0]) * 0.999 if tp1_zone else round(avg_price * (1 + TP1_MIN_PCT), 6)
            tp2 = float(tp2_zone[0]) * 0.999 if tp2_zone else round(avg_price * (1 + TP2_MIN_PCT), 6)
        else:
            # SL: 上方最大量清算区外侧
            sl_candidates = _pick_best_liq(above, 1_000_000)
            best_sl  = sl_candidates[0] if sl_candidates else None
            sl_raw   = float(best_sl[0]) + atr * 0.5 if best_sl else avg_price * (1 + TP1_MIN_PCT * 1.5)
            sl_price = max(sl_raw, avg_price * 1.001)

            # TP1/TP2: 下方清算区，必须低于现价且满足最小距离（空单TP是价格下跌）
            tp_candidates = _pick_best_liq([z for z in below if float(z[0]) < mark], 0)
            tp_max1 = avg_price * (1 - TP1_MIN_PCT)
            tp_max2 = avg_price * (1 - TP2_MIN_PCT)
            tp1_zone = next((z for z in sorted(tp_candidates, key=lambda z: float(z[0]), reverse=True)
                             if float(z[0]) * 1.001 <= tp_max1), None)
            tp2_zone = next((z for z in sorted(tp_candidates, key=lambda z: float(z[0]), reverse=True)
                             if float(z[0]) * 1.001 <= tp_max2 and (tp1_zone is None or float(z[0]) < float(tp1_zone[0]))), None)
            tp1 = float(tp1_zone[0]) * 1.001 if tp1_zone else round(avg_price * (1 - TP1_MIN_PCT), 6)
            tp2 = float(tp2_zone[0]) * 1.001 if tp2_zone else round(avg_price * (1 - TP2_MIN_PCT), 6)

        sl_price = round(sl_price, 6)
        tp1      = round(tp1, 6)
        tp2      = round(tp2, 6)
    except Exception as e:
        _log(f"[b_sl_tp] {sym} Coinglass失败，用ATR兜底: {e}")
        # ATR兜底：SL=ATR×2，TP1=ATR×2，TP2=ATR×4（同时保证至少2%/4%距离）
        sl_atr  = max(atr * 2, avg_price * 0.03)
        tp1_atr = max(atr * 2, avg_price * 0.02)
        tp2_atr = max(atr * 4, avg_price * 0.04)
        if direction == "LONG":
            sl_price = round(avg_price - sl_atr,  6)
            tp1      = round(avg_price + tp1_atr, 6)
            tp2      = round(avg_price + tp2_atr, 6)
        else:
            sl_price = round(avg_price + sl_atr,  6)
            tp1      = round(avg_price - tp1_atr, 6)
            tp2      = round(avg_price - tp2_atr, 6)

    half_qty = int(qty / 2)
    rest_qty = int(qty) - half_qty

    results = {}
    # ── 补挂止损 ──────────────────────────────────────────────────────────────
    if not has_sl:
        try:
            sl_price_fmt = float(_fmt_price(sym, sl_price, api))
            r = api.place_algo_order(sym, close_side, "STOP_MARKET",
                                     sl_price_fmt, direction, quantity=int(qty))
            results["sl"] = r.get("algoId") or "fail"
            if r.get("algoId"):
                # P0: 统一写入路径
                try:
                    _ps_store.upsert(sym, {
                        'sl_price':   sl_price,
                        'sl_algo_id': r['algoId'],
                    })
                except Exception: pass
        except Exception as e:
            results["sl"] = f"err:{e}"

    # ── 补挂止盈（限价单，不用条件单）──────────────────────────────────────
    if not has_tp:
        import math as _math
        def _fmt_tp(p):
            return _fmt_price(sym, p, api)
        try:
            r1 = api.place_order(sym, close_side, "LIMIT",
                                 quantity=str(half_qty), price=_fmt_tp(tp1),
                                 positionSide=direction, timeInForce="GTC")
            results["tp1"] = r1.get("orderId") or "fail"
        except Exception as e:
            results["tp1"] = f"err:{e}"
        try:
            r2 = api.place_order(sym, close_side, "LIMIT",
                                 quantity=str(rest_qty), price=_fmt_tp(tp2),
                                 positionSide=direction, timeInForce="GTC")
            results["tp2"] = r2.get("orderId") or "fail"
        except Exception as e:
            results["tp2"] = f"err:{e}"

        # P0: 统一写入路径
        try:
            _ps_store.upsert(sym, {
                'tp1_price': tp1,
                'tp2_price': tp2,
            })
        except Exception: pass

    sl_pnl  = round((sl_price  - avg_price) * qty, 2)
    tp1_pnl = round((tp1 - avg_price) * half_qty, 2)
    _log(f"[b_sl_tp] {sym} 兜底完成 SL={sl_price} TP1={tp1} TP2={tp2} results={results}")
    notify_fn(
        f"🛡 B单止损止盈已补挂 [{sym}]\n"
        f"入场 @{avg_price}  方向: {direction}\n"
        f"SL @{sl_price} ({sl_pnl:+.1f}U)  TP1 @{tp1} (+{tp1_pnl:.1f}U)  TP2 @{tp2}\n"
        f"来源: dual_net_b_fallback  结果: {results}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Fix-3: B单下单成功后立刻写 pos_state（在 check_breakout 里调用）
# ─────────────────────────────────────────────────────────────────────────────

def _write_pos_state_for_b_order(
    sym: str, direction: str, avg_price: float, qty: float, entry: dict
):
    """
    B单市价成交后立刻写入 pos_state.json 基础字段，
    让 ws_monitor._on_open_filled 能读到 entry_price/qty/direction，
    避免 Coinglass 第一次调用时 pos_state 为空导致参数错误。
    """
    try:
        _ps_store.upsert(sym, {
            'entry_price': avg_price,
            'qty':         qty,
            'direction':   direction,
            'source':      'dual_net_b',
        })
        _log(f"[pos_state] {sym} B单写入 entry={avg_price} qty={qty} dir={direction}")
    except Exception as e:
        _log(f"[pos_state] {sym} 写入失败（非致命）: {e}")
