"""
sl_handler.py — 止损/止盈挂单唯一入口 (P0)

规则：
  - place_sl：同步，失败重试3次，3次全败→返回 False（调用方市价平仓）
  - place_tp：异步安全，失败只记日志不中断主流程
  - 所有真实 API 调用委托给 binance_api（可 mock）
  - 开仓顺序：place_sl → confirm → register gates → write pos_state → place_tp
"""

import sys, os, time, json, logging
from pathlib import Path

WORKSPACE = Path(__file__).parent.parent
sys.path.insert(0, str(WORKSPACE))
sys.path.insert(0, str(WORKSPACE.parent))  # workspace root，访问 binance_api

logger = logging.getLogger("sl_handler")

# ── 重试配置 ──────────────────────────────────────────────────────────────────
SL_MAX_RETRIES  = 3
SL_RETRY_DELAY  = 1.0   # 秒


def _api():
    """懒加载 binance_api，允许测试时 mock"""
    try:
        import binance_api as api
        return api
    except ImportError:
        return None


# ── 内部：向交易所挂止损单 ────────────────────────────────────────────────────

def _place_stop_market(symbol: str, direction: str, sl_price: float, qty: float,
                       api=None) -> dict | None:
    """
    挂 STOP_MARKET 止损单。
    direction: LONG → side=SELL；SHORT → side=BUY
    使用 place_algo_order (/fapi/v1/algoOrder)，而非 place_order (/fapi/v1/order)
    返回统一格式 dict（含 orderId），失败返回 None
    """
    if api is None:
        api = _api()
    if api is None:
        logger.warning("[sl_handler] binance_api 未加载，跳过真实挂单")
        return {"orderId": "MOCK_SL", "status": "NEW"}

    side = "SELL" if direction == "LONG" else "BUY"
    position_side = "LONG" if direction == "LONG" else "SHORT"
    try:
        result = api.place_algo_order(
            symbol=symbol,
            side=side,
            order_type="STOP_MARKET",
            trigger_price=sl_price,
            position_side=position_side,
            quantity=qty,
        )
        # place_algo_order 返回 algoId，统一包装成 orderId 供上层使用
        if result and result.get("algoId"):
            result["orderId"] = str(result["algoId"])
        return result
    except Exception as e:
        logger.error(f"[sl_handler] _place_stop_market error: {e}")
        return None


def _place_limit_tp(symbol: str, direction: str, tp_price: float, qty: float,
                    api=None) -> dict | None:
    """挂限价止盈单（TAKE_PROFIT）"""
    if api is None:
        api = _api()
    if api is None:
        return {"orderId": "MOCK_TP", "status": "NEW"}

    side = "SELL" if direction == "LONG" else "BUY"
    try:
        result = api.place_order(
            symbol=symbol,
            side=side,
            order_type="TAKE_PROFIT",
            stop_price=tp_price,
            price=tp_price,
            quantity=qty,
            reduce_only=True,
        )
        return result
    except Exception as e:
        logger.error(f"[sl_handler] _place_limit_tp error: {e}")
        return None


# ── 公开接口 ──────────────────────────────────────────────────────────────────

def place_sl(symbol: str, direction: str, sl_price: float, qty: float,
             retries: int = SL_MAX_RETRIES, api=None) -> tuple[bool, str | None]:
    """
    同步挂止损单，带重试。

    Returns:
        (True,  order_id)  成功
        (False, None)      失败（调用方应执行市价平仓）
    """
    for attempt in range(1, retries + 1):
        result = _place_stop_market(symbol, direction, sl_price, qty, api)
        if result and result.get("orderId"):
            order_id = str(result["orderId"])
            logger.info(f"[sl_handler] SL placed: {symbol} {direction} sl={sl_price} orderId={order_id}")
            return True, order_id
        logger.warning(f"[sl_handler] SL attempt {attempt}/{retries} failed: {symbol}")
        if attempt < retries:
            time.sleep(SL_RETRY_DELAY)

    logger.error(f"[sl_handler] SL FAILED after {retries} retries: {symbol} {direction}")
    return False, None


def place_tp(symbol: str, direction: str,
             tp1_price: float, tp1_qty: float,
             tp2_price: float | None = None,
             tp2_qty: float | None = None,
             api=None) -> dict:
    """
    挂止盈单（异步安全，失败不中断主流程）。

    Returns:
        {"tp1_order_id": ..., "tp2_order_id": ..., "ok": True/False}
    """
    out = {"tp1_order_id": None, "tp2_order_id": None, "ok": False}

    r1 = _place_limit_tp(symbol, direction, tp1_price, tp1_qty, api)
    if r1 and r1.get("orderId"):
        out["tp1_order_id"] = str(r1["orderId"])
        out["ok"] = True
        logger.info(f"[sl_handler] TP1 placed: {symbol} tp1={tp1_price} orderId={out['tp1_order_id']}")

    if tp2_price and tp2_qty:
        r2 = _place_limit_tp(symbol, direction, tp2_price, tp2_qty, api)
        if r2 and r2.get("orderId"):
            out["tp2_order_id"] = str(r2["orderId"])
            logger.info(f"[sl_handler] TP2 placed: {symbol} tp2={tp2_price} orderId={out['tp2_order_id']}")

    return out


def calc_qty(notional: float, entry_price: float) -> float:
    """根据名义仓位和入场价反算合约张数（简化版，不含 tick 精度）"""
    if entry_price <= 0:
        return 0.0
    return round(notional / entry_price, 4)
