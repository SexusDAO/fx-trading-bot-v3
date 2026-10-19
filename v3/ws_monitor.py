"""
ws_monitor.py — WebSocket 监控精简版 (P3)

职责（单一）：
  1. 每 5 秒轮询 pending_orders.json
     → status=pending  → 挂限价单
     → status=sent     → 检测是否已成交 → 成交则触发 on_open_filled
     → status=*        → 超时则标记 expired / 撤销交易所挂单
  2. on_open_filled：止损同步挂单 + gates 注册 + pos_state 写入
     tp1/tp2 若缺失则基于 ATR 自动推算
  3. check_gates：价格触达时移动止损（+3% 保本 / +5% 上移）
  4. patrol：每 3 分钟检查止损单是否丢失，缺失补挂，失败推 notify
  5. start()：主循环，传播 notify_fn，轮询 + 按持仓当前价触发 check_gates

不再内嵌扫描，不再决策。
"""

import json
import sys
import os
import time
import logging
import threading
from pathlib import Path
from datetime import datetime, timezone

WORKSPACE = Path(__file__).parent
sys.path.insert(0, str(WORKSPACE))

from modules.pos_state_store import write as ps_write, get_all as ps_get_all, remove as ps_remove
from modules.sl_handler      import place_sl, place_tp, calc_qty
from modules.trade_logger    import log_open, log_close

logger = logging.getLogger("ws_monitor")

# ── 路径 ──────────────────────────────────────────────────────────────────────
PENDING_FILE = WORKSPACE / "config" / "pending_orders.json"
GATES_FILE   = WORKSPACE / "config" / "price_gates.json"

POLL_INTERVAL   = 5    # 秒：pending_orders 轮询
PATROL_INTERVAL = 180  # 秒：止损/止盈巡逻

# ── 工具 ──────────────────────────────────────────────────────────────────────

def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _load_json(path: Path) -> dict | list:
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}

def _save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    tmp.replace(path)

def _is_expired(expire_at: str) -> bool:
    try:
        dt = datetime.strptime(expire_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) > dt
    except Exception:
        return False

# ── API 封装（可 mock）────────────────────────────────────────────────────────

def _get_api():
    try:
        from modules import binance_api as api
        return api
    except ImportError:
        return None

def _place_limit_order(symbol: str, direction: str,
                       price: float, notional: float,
                       api=None) -> dict | None:
    """挂限价开仓单"""
    if api is None:
        api = _get_api()
    if api is None:
        return {"orderId": "MOCK_ENTRY", "status": "NEW"}
    side = "BUY" if direction == "LONG" else "SELL"
    try:
        qty = calc_qty(notional, price)
        return api.place_order(
            symbol=symbol, side=side,
            order_type="LIMIT",
            price=price, quantity=qty,
            time_in_force="GTC",
        )
    except Exception as e:
        logger.error(f"[ws_monitor] place_limit_order error: {e}")
        return None

def _get_open_orders(symbol: str, api=None) -> list:
    """查询指定标的的当前挂单"""
    if api is None:
        api = _get_api()
    if api is None:
        return []
    try:
        return api.get_open_orders(symbol=symbol) or []
    except Exception:
        return []

def _get_order_status(symbol: str, order_id: str, api=None) -> str | None:
    """
    查询单笔订单的状态。
    返回 "FILLED" / "CANCELED" / "NEW" / "PARTIALLY_FILLED" / None（异常）。
    """
    if api is None:
        api = _get_api()
    if api is None:
        return None
    try:
        result = api.get_order(symbol=symbol, orderId=order_id)
        return result.get("status") if result else None
    except Exception as e:
        logger.debug(f"[ws_monitor] get_order {symbol}/{order_id}: {e}")
        return None

def _get_fill_price(symbol: str, order_id: str, api=None) -> float | None:
    """查询已成交订单的均价，返回 float 或 None"""
    if api is None:
        api = _get_api()
    if api is None:
        return None
    try:
        result = api.get_order(symbol=symbol, orderId=order_id)
        if result and result.get("status") == "FILLED":
            avg = float(result.get("avgPrice") or result.get("price") or 0)
            return avg if avg > 0 else None
    except Exception as e:
        logger.debug(f"[ws_monitor] get_fill_price {symbol}/{order_id}: {e}")
    return None

def _cancel_order(symbol: str, order_id: str, api=None):
    if api is None:
        api = _get_api()
    if api is None:
        return
    try:
        api.cancel_order(symbol=symbol, orderId=order_id)
    except Exception as e:
        logger.warning(f"[ws_monitor] cancel_order error: {e}")

def _calc_tp_prices(direction: str, entry: float, k1h: list | None) -> tuple[float, float]:
    """
    基于 ATR 推算 tp1 / tp2。
    ATR 来自最近 100 根 1h k 线（若无则用 entry * 1.5% 估算）。
    tp1 = entry ± 1.5 * ATR
    tp2 = entry ± 3.0 * ATR
    """
    atr = 0.0
    if k1h and len(k1h) >= 14:
        try:
            from modules.scoring_v3 import atr as _atr
            atr = _atr(k1h)
        except Exception:
            pass
    if atr <= 0:
        atr = entry * 0.015   # fallback: 1.5%

    if direction == "LONG":
        tp1 = round(entry + atr * 1.5, 6)
        tp2 = round(entry + atr * 3.0, 6)
    else:
        tp1 = round(entry - atr * 1.5, 6)
        tp2 = round(entry - atr * 3.0, 6)
    return tp1, tp2

# ── pending_orders 轮询 ────────────────────────────────────────────────────────

def poll_pending_orders(api=None, notify_fn=None):
    """
    读取 pending_orders.json：
      - status=pending  → 挂限价单，改为 sent
      - status=sent     → 查询交易所状态：
          FILLED   → 触发 on_open_filled，改为 filled
          CANCELED → 标记 canceled
          超时      → 撤销挂单，标记 expired
      - 其他状态        → 跳过
    """
    pending = _load_json(PENDING_FILE)
    if not isinstance(pending, dict):
        return

    orders = pending.get("orders", [])
    changed = False

    for o in orders:
        status = o.get("status")

        # ── pending → 挂单 ────────────────────────────────────────────────────
        if status == "pending":
            if _is_expired(o.get("expire_at", "")):
                o["status"] = "expired"
                changed = True
                logger.info(f"[poll] {o['symbol']} pending 超时，标记 expired")
                continue

            result = _place_limit_order(
                o["symbol"], o["direction"],
                o["entry_price"], o["notional"], api
            )
            if result and result.get("orderId"):
                o["status"] = "sent"
                o["exchange_order_id"] = str(result["orderId"])
                changed = True
                logger.info(f"[poll] {o['symbol']} 挂单成功 orderId={o['exchange_order_id']}")
            else:
                logger.warning(f"[poll] {o['symbol']} 挂单失败，下轮重试")

        # ── sent → 检测成交 ───────────────────────────────────────────────────
        elif status == "sent":
            exc_id = o.get("exchange_order_id", "")

            # 超时先撤单
            if _is_expired(o.get("expire_at", "")):
                if exc_id:
                    _cancel_order(o["symbol"], exc_id, api)
                o["status"] = "expired"
                changed = True
                logger.info(f"[poll] {o['symbol']} sent 超时，撤单 expired")
                continue

            order_status = _get_order_status(o["symbol"], exc_id, api)
            if order_status == "FILLED":
                fill_price = _get_fill_price(o["symbol"], exc_id, api) or o["entry_price"]
                fill_qty   = calc_qty(o.get("notional", 0), fill_price)
                ok = on_open_filled(
                    symbol        = o["symbol"],
                    direction     = o["direction"],
                    fill_price    = fill_price,
                    fill_qty      = fill_qty,
                    order_id      = exc_id,
                    pending_order = o,
                    api           = api,
                    notify_fn     = notify_fn,
                )
                o["status"] = "filled" if ok else "emergency_closed"
                o["filled_at"] = _now_iso()
                o["fill_price"] = fill_price
                changed = True
                logger.info(f"[poll] {o['symbol']} 成交 → {o['status']} price={fill_price}")

            elif order_status == "CANCELED":
                o["status"] = "canceled"
                changed = True
                logger.info(f"[poll] {o['symbol']} 交易所已撤单")

    if changed:
        pending["orders"] = orders
        _save_json(PENDING_FILE, pending)

# ── 成交事件处理（P0 严格顺序）─────────────────────────────────────────────────

def on_open_filled(symbol: str, direction: str,
                   fill_price: float, fill_qty: float,
                   order_id: str, pending_order: dict,
                   api=None, notify_fn=None) -> bool:
    """
    开仓成交后严格顺序执行：
      Step1: 止损（同步，P0，失败→市价平仓）
      Step2: 注册 gates
      Step3: 写入 pos_state
      Step4: 止盈（tp 若为 0 则基于 ATR 自动推算）
      Step5: 记录日志
      Step6: notify 播报

    Returns: True=全部成功, False=止损失败已平仓
    """
    hard_sl  = pending_order.get("hard_sl", 0)
    notional = pending_order.get("notional", fill_price * fill_qty)

    # ── Step 1: 止损（同步，P0） ───────────────────────────────────────────────
    sl_ok, sl_order_id = place_sl(symbol, direction, hard_sl, fill_qty, api=api)
    if not sl_ok:
        logger.error(f"[on_open_filled] {symbol} 止损挂单失败，市价平仓")
        _emergency_close(symbol, direction, fill_qty, api)
        if notify_fn:
            notify_fn(
                f"🚨 {symbol} {direction} 止损挂单失败！已市价平仓。\n"
                f"entry={fill_price} hard_sl={hard_sl}"
            )
        return False

    # ── Step 2: 注册 gates ────────────────────────────────────────────────────
    tp1_raw = pending_order.get("tp1_price", 0)
    _register_gates(symbol, direction, fill_price, hard_sl, tp1_raw)

    # ── Step 3: 写入 pos_state ────────────────────────────────────────────────
    ps_write(symbol, direction, {
        "entry_price":     fill_price,
        "avg_price":       fill_price,
        "notional":        notional,
        "total_notional":  notional,
        "first_notional":  notional,
        "sl_price":        hard_sl,
        "sl_order_id":     sl_order_id,
        "signal_type":     pending_order.get("signal_type", ""),
        "score_at_open":   pending_order.get("score", 0),
        "score_details":   pending_order.get("score_details", {}),
        "entry_mode":      pending_order.get("entry_mode", ""),
        "flatten_count":   0,
        "entry_time":      _now_iso(),
    })

    # ── Step 4: 止盈（若未设置则 ATR 推算）───────────────────────────────────
    tp1 = pending_order.get("tp1_price", 0)
    tp2 = pending_order.get("tp2_price", 0)
    if tp1 <= 0:
        k1h = pending_order.get("k1h")   # scanner 写入时可保留
        tp1, tp2 = _calc_tp_prices(direction, fill_price, k1h)
        logger.info(f"[on_open_filled] {symbol} tp 自动推算: tp1={tp1} tp2={tp2}")

    tp_result = place_tp(
        symbol, direction,
        tp1_price=tp1, tp1_qty=round(fill_qty * 0.6, 6),
        tp2_price=tp2 if tp2 > 0 else None,
        tp2_qty=round(fill_qty * 0.4, 6) if tp2 > 0 else None,
        api=api,
    )
    ps_write(symbol, direction, {
        "tp1_price":    tp1,
        "tp1_order_id": tp_result.get("tp1_order_id"),
        "tp2_price":    tp2,
        "tp2_order_id": tp_result.get("tp2_order_id"),
    })

    # ── Step 5: 日志 ──────────────────────────────────────────────────────────
    log_open(
        symbol=symbol, direction=direction,
        entry_price=fill_price, notional=notional,
        signal_type=pending_order.get("signal_type", ""),
        score=pending_order.get("score", 0),
        score_details=pending_order.get("score_details", {}),
        entry_mode=pending_order.get("entry_mode", "LIMIT"),
        funding_rate=pending_order.get("funding_rate", 0),
        sl_price=hard_sl, tp1_price=tp1, tp2_price=tp2,
    )

    logger.info(f"[on_open_filled] {symbol} {direction} 开仓完成 "
                f"price={fill_price} sl={hard_sl} tp1={tp1} sl_order={sl_order_id}")

    # ── Step 6: 播报 ──────────────────────────────────────────────────────────
    if notify_fn:
        score = pending_order.get("score", 0)
        sig   = pending_order.get("signal_type", "")
        notify_fn(
            f"✅ 开仓成交\n"
            f"{direction} {symbol}  成交价 {fill_price}  {notional}U\n"
            f"止损 {hard_sl}  止盈 {tp1}/{tp2}\n"
            f"信号 {sig}  评分 {score}"
        )

    return True


def _emergency_close(symbol: str, direction: str, qty: float, api=None):
    """止损挂单失败时市价平仓（紧急）"""
    if api is None:
        api = _get_api()
    if api is None:
        logger.error(f"[emergency] {symbol} 无 API，无法平仓！")
        return
    side = "SELL" if direction == "LONG" else "BUY"
    try:
        api.place_order(symbol=symbol, side=side,
                        order_type="MARKET", quantity=qty,
                        reduce_only=True)
        logger.info(f"[emergency] {symbol} 市价平仓成功")
    except Exception as e:
        logger.error(f"[emergency] {symbol} 市价平仓失败: {e}")

# ── Gates（移动止损）─────────────────────────────────────────────────────────

def _register_gates(symbol: str, direction: str,
                    entry: float, sl: float, tp1: float):
    """注册移动止损门控到 price_gates.json"""
    gates = _load_json(GATES_FILE) if GATES_FILE.exists() else {}
    key = f"{symbol}_{direction}"
    gates[key] = {
        "symbol":    symbol,
        "direction": direction,
        "entry":     entry,
        "sl":        sl,
        "tp1":       tp1,
        "breakeven_triggered": False,
        "gate5_triggered":     False,
        "gate8_triggered":     False,
        "registered_at":       _now_iso(),
    }
    _save_json(GATES_FILE, gates)


def check_gates(symbol: str, current_price: float, api=None, notify_fn=None):
    """
    检查 gates 是否触发，触发则移动止损。
    由主循环按持仓当前价定期调用（也可由 WS 实时触发）。
    """
    gates = _load_json(GATES_FILE) if GATES_FILE.exists() else {}
    pos_all = ps_get_all()
    changed = False

    for key, g in gates.items():
        if g["symbol"] != symbol:
            continue
        direction = g["direction"]
        entry = g["entry"]
        pos = pos_all.get(key, {})
        if not pos:
            continue

        if direction == "LONG":
            pnl_pct = (current_price - entry) / entry
        else:
            pnl_pct = (entry - current_price) / entry

        sl_new = None
        gate_label = ""

        # 保本门（+3%）
        if not g["breakeven_triggered"] and pnl_pct >= 0.03:
            sl_new = entry
            g["breakeven_triggered"] = True
            gate_label = "+3% 保本"
            logger.info(f"[gates] {symbol} +3% 止损移至保本 {entry}")

        # +5% 门
        elif g["breakeven_triggered"] and not g["gate5_triggered"] and pnl_pct >= 0.05:
            atr_est = entry * 0.01
            sl_new = round(entry + atr_est if direction == "LONG" else entry - atr_est, 6)
            g["gate5_triggered"] = True
            gate_label = "+5% 上移"
            logger.info(f"[gates] {symbol} +5% 止损上移 {sl_new:.4f}")

        if sl_new is not None:
            qty = calc_qty(pos.get("notional", 0), entry)
            old_sl_id = pos.get("sl_order_id")
            if old_sl_id:
                _cancel_order(symbol, old_sl_id, api)
            ok, new_sl_id = place_sl(symbol, direction, sl_new, qty, api=api)
            if ok:
                ps_write(symbol, direction, {
                    "sl_price":    sl_new,
                    "sl_order_id": new_sl_id,
                })
                g["sl"] = sl_new
                changed = True
                if notify_fn:
                    notify_fn(
                        f"🔒 止损移动 [{gate_label}]\n"
                        f"{direction} {symbol}  新止损 {sl_new}  当前价 {current_price}"
                    )

    if changed:
        _save_json(GATES_FILE, gates)

    # ── 摔平型补仓检测（浮亏 >3%）─────────────────────────────
    pos_all_fresh = ps_get_all()
    for key, pos in pos_all_fresh.items():
        if pos.get("symbol") == symbol:
            _check_flatten_and_add(symbol, pos.get("direction", ""),
                                   pos, current_price, api, notify_fn)


def _check_flatten_and_add(symbol: str, direction: str, pos: dict,
                           current_price: float, api=None, notify_fn=None):
    """
    检查摔平型补仓条件，满足则提交市价单进行摔平。
    健康评分简化版：仅检查浮亏 + 次数，不调 LLM。
    """
    try:
        from modules.add_position import check_flatten, apply_flatten
    except ImportError:
        return

    result = check_flatten(pos, current_price)
    if not result.get("should_add"):
        return

    add_notional = result["add_notional"]
    logger.info(f"[add_pos] {symbol} {direction} 补仓触发: {result['reason']}")

    # 提交市价补仓单
    side = "BUY" if direction == "LONG" else "SELL"
    try:
        qty = calc_qty(add_notional, current_price)
        if api is None:
            api = _get_api()
        result_order = None
        if api:
            result_order = api.place_order(
                symbol=symbol, side=side,
                order_type="MARKET", quantity=qty,
                reduce_only=False,
            )
        if result_order is None:
            result_order = {"orderId": "MOCK_ADD", "status": "FILLED"}

        # 更新 pos_state
        new_pos = apply_flatten(pos, current_price, add_notional)
        ps_write(symbol, direction, {
            "avg_price":      new_pos["avg_price"],
            "notional":       new_pos["notional"],
            "total_notional": new_pos["total_notional"],
            "flatten_count":  new_pos["flatten_count"],
            "last_flatten_at": new_pos["last_flatten_at"],
        })
        logger.info(f"[add_pos] {symbol} 摔平补仓成功 "
                    f"price={current_price} add={add_notional}U "
                    f"new_avg={new_pos['avg_price']:.4f}")
        if notify_fn:
            notify_fn(
                f"🔄 摔平补仓\n"
                f"{direction} {symbol}  市价 {current_price}  +{add_notional}U\n"
                f"新均价 {new_pos['avg_price']:.4f}  "
                f"补仓次数 {new_pos['flatten_count']}/2"
            )
    except Exception as e:
        logger.error(f"[add_pos] {symbol} 摔平补仓失败: {e}")


# ── 平仓成交处理 ────────────────────────────────────────────────────────────────

def on_close_filled(symbol: str, direction: str,
                   close_price: float,
                   close_reason: str,
                   api=None, notify_fn=None):
    """
    止损/止盈/手动平仓成交后执行：
      1. log_close（PnL 计算 + 写 trade_log）
      2. ps_remove（清除持仓状态）
      3. 清除 gates
      4. circuit breaker 联动（亏损累计超阈值则触发熔断）
      5. notify 播报

    close_reason: 'sl' | 'tp1' | 'tp2' | 'manual' | 'circuit'
    """
    pos = None
    try:
        from modules.pos_state_store import get as ps_get
        pos = ps_get(symbol, direction)
    except Exception:
        pass

    if not pos:
        logger.warning(f"[on_close_filled] {symbol} {direction} pos_state 不存在，跳过")
        return

    entry_price = pos.get("entry_price", close_price)
    avg_price   = pos.get("avg_price", entry_price)
    notional    = pos.get("notional", 0)
    add_count   = pos.get("flatten_count", 0)
    signal_type = pos.get("signal_type", "")

    # ── 1. 记录平仓日志 ───────────────────────────────────────────────────────
    rec = log_close(
        symbol=symbol,
        direction=direction,
        close_price=close_price,
        close_reason=close_reason,
        entry_price=entry_price,
        notional=notional,
        add_count=add_count,
        avg_price=avg_price,
        extra={"signal_type": signal_type},
    )
    pnl_usdt = rec.get("pnl_usdt", 0)
    pnl_pct  = rec.get("pnl_pct", 0)

    # ── 2. 清除持仓状态 ───────────────────────────────────────────────────────
    ps_remove(symbol, direction)
    logger.info(f"[on_close_filled] {symbol} {direction} 已平仓 "
                f"close={close_price} pnl={pnl_usdt}U ({pnl_pct}%)")

    # ── 3. 清除 gates ─────────────────────────────────────────────────────────
    gates = _load_json(GATES_FILE) if GATES_FILE.exists() else {}
    key = f"{symbol}_{direction}"
    if key in gates:
        del gates[key]
        _save_json(GATES_FILE, gates)

    # ── 4. circuit breaker 联动 ───────────────────────────────────────────────
    _update_circuit(pnl_usdt)

    # ── 5. 播报 ───────────────────────────────────────────────────────────────
    if notify_fn:
        emoji = "🟢" if pnl_usdt > 0 else "🔴"
        reason_map = {"sl": "止损", "tp1": "止盈TP1", "tp2": "止盈TP2",
                      "manual": "手动", "circuit": "熔断平仓"}
        reason_label = reason_map.get(close_reason, close_reason)
        notify_fn(
            f"{emoji} 平仓 [{reason_label}]\n"
            f"{direction} {symbol}  平仓价 {close_price}\n"
            f"PnL {'+' if pnl_usdt >= 0 else ''}{pnl_usdt}U  ({'+' if pnl_pct >= 0 else ''}{pnl_pct}%)"
        )


def _update_circuit(pnl_usdt: float):
    """
    每次平仓后累计 daily_loss，超过阈值自动触发熔断。
    阈值来自 strategy-config.json risk.daily_loss_pct（默认 20% 权益）。
    """
    RISK_FILE    = WORKSPACE / "config" / "daily_risk.json"
    CIRCUIT_FILE = WORKSPACE / "config" / "circuit_breaker.json"
    STRATEGY_FILE = WORKSPACE / "config" / "strategy-config.json"

    # 读取每日风控状态
    risk = _load_json(RISK_FILE)
    if not isinstance(risk, dict):
        risk = {}
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if risk.get("date") != today:
        risk = {"date": today, "daily_loss": 0.0, "daily_pnl": 0.0}

    risk["daily_pnl"] = round(risk.get("daily_pnl", 0) + pnl_usdt, 2)
    if pnl_usdt < 0:
        risk["daily_loss"] = round(risk.get("daily_loss", 0) + abs(pnl_usdt), 2)
    _save_json(RISK_FILE, risk)

    # 检查是否超阈值
    cfg = _load_json(STRATEGY_FILE)
    daily_loss_pct = float(cfg.get("risk", {}).get("daily_loss_pct", 0.20))

    # 用权益上限估算（简化：用 daily_loss > 200U 作为绝对兜底）
    circuit = _load_json(CIRCUIT_FILE)
    if isinstance(circuit, dict) and not circuit.get("open", False):
        # 从 trade_log 拿今日总亏损与权益比
        try:
            from modules.trade_logger import daily_summary
            summary    = daily_summary(today)
            total_loss = risk["daily_loss"]
            consec     = summary.get("consecutive_losses", 0)
            # 连亏 ≥5 笔 或 当日亏损超 200U → 熔断
            should_trip = (consec >= 5) or (total_loss >= 200)
            if should_trip:
                circuit["open"] = True
                circuit["tripped_at"] = _now_iso()
                circuit["reason"] = (
                    f"consecutive_losses={consec}" if consec >= 5
                    else f"daily_loss={total_loss}U"
                )
                _save_json(CIRCUIT_FILE, circuit)
                logger.warning(f"[circuit] 熔断触发: {circuit['reason']}")
        except Exception as e:
            logger.debug(f"[circuit] 检查失败: {e}")


# ── Patrol（止损/止盈守卫）───────────────────────────────────────────────────

def patrol(api=None, notify_fn=None):
    """
    每 3 分钟执行：
      A. 检查止损单是否存在，缺失则补挂。补挂失败推 notify。
      B. 检查止损/止盈单是否已成交——若已成交则触发 on_close_filled。
    无操作时静默。
    """
    result = {"checked": 0, "reattached": 0, "errors": 0, "closed": 0}
    pos_all = ps_get_all()

    for key, pos in pos_all.items():
        symbol    = pos.get("symbol", "")
        direction = pos.get("direction", "")
        sl_price  = pos.get("sl_price", 0)
        sl_id     = pos.get("sl_order_id", "")
        tp1_id    = pos.get("tp1_order_id", "")
        tp2_id    = pos.get("tp2_order_id", "")

        if not symbol or not sl_price:
            continue

        result["checked"] += 1
        open_orders = _get_open_orders(symbol, api)
        open_ids    = {str(o.get("orderId", "")) for o in open_orders}

        # ── B. 止盈单成交检测 ──────────────────────────────────
        for tp_id, reason in [(tp1_id, "tp1"), (tp2_id, "tp2")]:
            if not tp_id:
                continue
            # 如果 止盈单 不在 open_orders 中，可能已成交或已撤销
            if tp_id not in open_ids:
                status = _get_order_status(symbol, tp_id, api)
                if status == "FILLED":
                    close_price = _get_fill_price(symbol, tp_id, api) or pos.get("sl_price", 0)
                    logger.info(f"[patrol] {symbol} {reason} 已成交 price={close_price}")
                    on_close_filled(symbol, direction, close_price, reason,
                                    api=api, notify_fn=notify_fn)
                    result["closed"] += 1
                    break  # 平仓后该持仓已删除，跳出内层循环

        # 平仓后 pos 已被删除，跳过后续检查
        if not ps_get_all().get(key):
            continue

        # ── B2. 止损单成交检测 ─────────────────────────────────
        if sl_id and sl_id not in open_ids:
            status = _get_order_status(symbol, sl_id, api)
            if status == "FILLED":
                close_price = _get_fill_price(symbol, sl_id, api) or sl_price
                logger.warning(f"[patrol] {symbol} 止损已触发 price={close_price}")
                on_close_filled(symbol, direction, close_price, "sl",
                                api=api, notify_fn=notify_fn)
                result["closed"] += 1
                continue

        # ── A. 止损单守卫（仅当仓位还存在时）──────────────────
        if sl_id and sl_id not in open_ids:
            logger.warning(f"[patrol] {symbol} 止损单丢失(id={sl_id})，补挂 sl={sl_price}")
            qty = calc_qty(pos.get("notional", 0), pos.get("entry_price", 1))
            ok, new_id = place_sl(symbol, direction, sl_price, qty, api=api)
            if ok:
                ps_write(symbol, direction, {"sl_order_id": new_id})
                result["reattached"] += 1
                logger.info(f"[patrol] {symbol} 补挂成功 new_id={new_id}")
            else:
                result["errors"] += 1
                logger.error(f"[patrol] {symbol} 补挂失败！需要人工干预")
                if notify_fn:
                    notify_fn(
                        f"🚨 止损守卫告警\n"
                        f"{direction} {symbol}  止损单丢失且补挂失败！\n"
                        f"止损价 {sl_price}  需要立即人工检查"
                    )

    return result

# ── 主循环（生产用）──────────────────────────────────────────────────────────

def start(api=None, notify_fn=None):
    """
    启动主循环（阻塞）。
    生产环境调用；测试时直接调用 poll_pending_orders / on_open_filled / patrol。

    每轮：
      1. poll_pending_orders（挂单 + 成交检测）
      2. 按持仓当前价触发 check_gates（每次取最新 mark price）
      3. 每 3 分钟 patrol（止损守卫） + 持仓一致性核对

    告警机制：
      A. API 连续失败 3 次 → notify_fn 推送告警
      B. 本地持仓与交易所持仓不一致 → notify_fn 推送告警
    """
    logger.info("[ws_monitor] 启动")
    last_patrol  = 0.0
    _api_fail_count = 0          # 连续 API 失败计数（告警A）
    _API_FAIL_THRESHOLD = 3
    _last_pos_mismatch_alert = 0.0  # 防止重复告警（告警B，最多每5分钟一次）

    while True:
        try:
            # Step 1: 轮询 pending
            poll_pending_orders(api, notify_fn)
            _api_fail_count = 0  # 成功则重置计数

            # Step 2: 对每个持仓触发 check_gates
            pos_all = ps_get_all()
            if pos_all:
                real_api = api or _get_api()
                for key, pos in pos_all.items():
                    sym = pos.get("symbol", "")
                    if not sym:
                        continue
                    try:
                        if real_api and hasattr(real_api, "get_ticker"):
                            ticker = real_api.get_ticker(sym)
                            price  = float(ticker.get("lastPrice", 0))
                            if price > 0:
                                check_gates(sym, price, api, notify_fn)
                    except Exception as e:
                        logger.debug(f"[start] check_gates {sym}: {e}")

            # Step 3: patrol + 持仓一致性核对
            now = time.time()
            if now - last_patrol >= PATROL_INTERVAL:
                patrol(api, notify_fn)

                # ── 告警B：持仓状态一致性核对 ─────────────────────────────────
                # 对比本地 pos_state 与交易所实际持仓
                try:
                    real_api = api or _get_api()
                    if real_api:
                        exchange_pos = real_api.get_positions()  # 交易所持仓列表
                        exchange_map = {
                            f"{p['symbol']}_LONG":  float(p.get('positionAmt', 0))
                            for p in exchange_pos if float(p.get('positionAmt', 0)) != 0
                        }
                        exchange_map.update({
                            f"{p['symbol']}_SHORT": abs(float(p.get('positionAmt', 0)))
                            for p in exchange_pos if float(p.get('positionAmt', 0)) < 0
                        })
                        local_keys = set(ps_get_all().keys())
                        exchange_keys = set(exchange_map.keys())

                        # 本地有但交易所没有（本地多记了）
                        ghost = local_keys - exchange_keys
                        # 交易所有但本地没有（本地漏记了）
                        missing = exchange_keys - local_keys

                        if (ghost or missing) and now - _last_pos_mismatch_alert > 300:
                            _last_pos_mismatch_alert = now
                            msg_lines = ["⚠️ 持仓状态不一致，需人工核查"]
                            if ghost:
                                msg_lines.append(f"本地多记（交易所无持仓）: {list(ghost)}")
                            if missing:
                                msg_lines.append(f"本地漏记（交易所有持仓）: {list(missing)}")
                            msg = "\n".join(msg_lines)
                            logger.error(f"[mismatch] {msg}")
                            if notify_fn:
                                notify_fn(msg)
                except Exception as e:
                    logger.debug(f"[start] 持仓核对异常: {e}")

                last_patrol = now

        except Exception as e:
            # ── 告警A：API 连续失败计数 ──────────────────────────────────────
            _api_fail_count += 1
            logger.error(f"[ws_monitor] 主循环异常({_api_fail_count}次): {e}")
            if _api_fail_count >= _API_FAIL_THRESHOLD and notify_fn:
                notify_fn(
                    f"🚨 ws_monitor API连续失败 {_api_fail_count} 次\n"
                    f"最近错误: {type(e).__name__}: {str(e)[:120]}\n"
                    f"请检查网络/API Key/币安服务状态"
                )
                _api_fail_count = 0  # 告警后重置，避免每轮都告

        time.sleep(POLL_INTERVAL)
