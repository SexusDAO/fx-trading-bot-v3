#!/usr/bin/env python3
"""
sl_handler.py — 止损止盈挂单单一负责人 (P1)

全系统唯一的止损/止盈挂单入口。
所有开仓成交后的止损挂单都通过 sl_handler.place() 执行。

职责：
  1. 计算合理止损价（从 pos_state 读 hard_sl，或根据 ATR 计算）
  2. 挂 Algo STOP_MARKET（交易所服务端执行，重启也有效）
  3. 挂止盈限价单（TP1 + TP2，各50%仓位）
  4. 写入 pos_state（通过 pos_state_store）
  5. 防重复挂：先查现有 algo 单，有则跳过

不负责：
  - price_gates 门控（浮盈移动止损，由 ws_monitor 负责）
  - 平仓后的清理（由 ws_monitor._on_close_filled 负责）
  - 手动修复（sl_tp_manager.py 作为独立人工工具）
"""

import sys
import time
import math
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import binance_api as api
from pos_state_store import store as _ps_store


# ─── 价格工具 ─────────────────────────────────────────────────────────────────

def _round_price(price: float, tick_str: str) -> str:
    tick = float(tick_str)
    if tick <= 0:
        return str(price)
    d = max(0, -int(math.floor(math.log10(tick)))) if tick < 1 else 0
    return f"{round(round(price / tick) * tick, d):.{d}f}"


def _round_qty(qty: float, step_str: str) -> str:
    step = float(step_str)
    if step <= 0:
        return str(qty)
    d = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
    return f"{round(math.floor(qty / step) * step, d):.{d}f}"


def _has_stop(sym: str) -> bool:
    """检查是否已有 algo 止损单（防重复挂）。"""
    try:
        orders = api.get_algo_open_orders(sym) or []
        return any("STOP" in o.get("orderType", "") for o in orders)
    except Exception:
        return False


def _has_tp(sym: str) -> bool:
    """检查是否已有止盈单（algo TP 或普通限价减仓单）。"""
    try:
        # Algo TP
        orders = api.get_algo_open_orders(sym) or []
        if any("TAKE_PROFIT" in o.get("orderType", "") for o in orders):
            return True
        # 普通限价减仓单
        open_orders = api.get_open_orders(sym) or []
        if any(o.get("reduceOnly") for o in open_orders):
            return True
    except Exception:
        pass
    return False


# ─── 主接口 ───────────────────────────────────────────────────────────────────

def place(
    sym: str,
    pos_side: str,       # 'LONG' | 'SHORT'
    entry: float,
    qty: float,
    sl_price: float,
    tp1: float,
    tp2: float,
    notify_fn=None,
    force: bool = False,  # True = 忽略重复检查，强制重挂
) -> dict:
    """
    统一挂止损 + 止盈。

    参数：
        sym       - 合约标的，如 'BTCUSDT'
        pos_side  - 仓位方向：'LONG' or 'SHORT'
        entry     - 开仓均价
        qty       - 仓位数量（张）
        sl_price  - 止损价（由调用方计算，通常来自 liquidation_map 的 hard_sl）
        tp1       - 止盈1价（50%仓位）
        tp2       - 止盈2价（50%仓位）
        notify_fn - 通知函数，可选
        force     - 是否强制重新挂（忽略重复检查）

    返回：
        {
          "sl_ok": bool,
          "tp1_ok": bool,
          "tp2_ok": bool,
          "sl_algo_id": int or None,
          "errors": list
        }
    """
    result = {"sl_ok": False, "tp1_ok": False, "tp2_ok": False,
              "sl_algo_id": None, "errors": []}

    close_side = "SELL" if pos_side == "LONG" else "BUY"

    # 获取精度参数
    try:
        sinfo     = api.get_symbol_info(sym)
        tick_str  = sinfo.get("tick_size", "0.0001")
        step_str  = sinfo.get("step_size", "0.001")
    except Exception as e:
        result["errors"].append(f"get_symbol_info: {e}")
        tick_str = "0.0001"
        step_str = "0.001"

    half_qty = float(_round_qty(qty / 2, step_str))
    rest_qty = float(_round_qty(qty - half_qty, step_str))
    qty_str  = _round_qty(qty, step_str)

    # 安全检查：止损价方向正确，fallback 用 ATR×2 动态计算
    try:
        from entry_logic import atr_calc
        _k_sl = api.get_klines(sym, "1h", 20)
        _atr_sl = atr_calc(_k_sl) if _k_sl else entry * 0.02
    except Exception:
        _atr_sl = entry * 0.02
    if pos_side == "LONG" and sl_price >= entry:
        sl_price = entry - _atr_sl * 2.0
    if pos_side == "SHORT" and sl_price <= entry:
        sl_price = entry + _atr_sl * 2.0

    sl_str  = _round_price(sl_price, tick_str)
    tp1_str = _round_price(tp1, tick_str)
    tp2_str = _round_price(tp2, tick_str)

    # ── 挂止损 ───────────────────────────────────────────────────────────────
    if force or not _has_stop(sym):
        try:
            r = api.place_algo_order(
                sym, close_side, "STOP_MARKET",
                sl_str, pos_side, quantity=qty
            )
            if r.get("algoId"):
                result["sl_ok"]      = True
                result["sl_algo_id"] = int(r["algoId"])
                api.algo_cache_save(sym, "STOP_MARKET", int(r["algoId"]), sl_str)
                _ps_store.upsert(sym, {
                    "sl_price":   float(sl_str),
                    "sl_algo_id": int(r["algoId"]),
                })
            else:
                result["errors"].append(f"sl_place_fail: {r}")
        except Exception as e:
            result["errors"].append(f"sl_exception: {e}")
    else:
        result["sl_ok"] = True  # 已有止损，视为成功

    # ── 挂止盈 ───────────────────────────────────────────────────────────────
    if force or not _has_tp(sym):
        try:
            r1 = api.place_order(
                sym, close_side, "LIMIT",
                quantity=str(half_qty), price=tp1_str,
                positionSide=pos_side, timeInForce="GTC"
            )
            if r1.get("orderId"):
                result["tp1_ok"] = True
                api.algo_cache_save(sym, "LIMIT_TP1", int(r1["orderId"]), tp1_str)
            else:
                result["errors"].append(f"tp1_place_fail: {r1}")
        except Exception as e:
            result["errors"].append(f"tp1_exception: {e}")

        try:
            r2 = api.place_order(
                sym, close_side, "LIMIT",
                quantity=str(rest_qty), price=tp2_str,
                positionSide=pos_side, timeInForce="GTC"
            )
            if r2.get("orderId"):
                result["tp2_ok"] = True
                api.algo_cache_save(sym, "LIMIT_TP2", int(r2["orderId"]), tp2_str)
            else:
                result["errors"].append(f"tp2_place_fail: {r2}")
        except Exception as e:
            result["errors"].append(f"tp2_exception: {e}")

        if result["tp1_ok"] or result["tp2_ok"]:
            _ps_store.upsert(sym, {
                "tp1_price": float(tp1_str),
                "tp2_price": float(tp2_str),
            })
    else:
        result["tp1_ok"] = result["tp2_ok"] = True  # 已有止盈，视为成功

    # ── 通知 ─────────────────────────────────────────────────────────────────
    if notify_fn:
        status = "✅" if (result["sl_ok"] and result["tp1_ok"]) else "⚠️"
        err_str = ("  错误: " + " / ".join(result["errors"])) if result["errors"] else ""
        pnl_sl  = round((float(sl_str)  - entry) * qty  * (1 if pos_side=="LONG" else -1), 2)
        pnl_tp1 = round((float(tp1_str) - entry) * half_qty * (1 if pos_side=="LONG" else -1), 2)
        pnl_tp2 = round((float(tp2_str) - entry) * rest_qty * (1 if pos_side=="LONG" else -1), 2)
        notify_fn(
            f"{status} [{sym}] 止损止盈已挂\n"
            f"SL @{sl_str} ({pnl_sl:+.1f}U)  "
            f"TP1 @{tp1_str} (+{pnl_tp1:.1f}U)  "
            f"TP2 @{tp2_str} (+{pnl_tp2:.1f}U){err_str}"
        )

    return result


def cancel_sl(sym: str) -> bool:
    """SL 触发/TP全部成交时调用，撤掉剩余 SL（OCO 互斥）。"""
    try:
        algo_orders = api.get_algo_open_orders(sym) or []
        sl_orders = [o for o in algo_orders if "STOP" in o.get("orderType", "")]
        for o in sl_orders:
            api.cancel_algo_order(sym, int(o["algoId"]))
        return bool(sl_orders)
    except Exception:
        return False


def cancel_tp(sym: str) -> bool:
    """SL 触发时调用，撤掉剩余 TP 限价单（OCO 互斥）。"""
    cancelled = False
    try:
        open_orders = api.get_open_orders(sym) or []
        for o in open_orders:
            if o.get("reduceOnly") and o.get("type") == "LIMIT":
                api.cancel_order(sym, int(o["orderId"]))
                cancelled = True
    except Exception:
        pass
    return cancelled


# ─── 语法自测 ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("sl_handler 加载正常 ✅")
    print("接口: sl_handler.place(sym, pos_side, entry, qty, sl_price, tp1, tp2)")
