"""
guards/patrol_sl_check.py — 止损守卫 (每 3 分钟 cron 触发)

职责：
  - 读取 pos_state.json
  - 对每个持仓查询交易所当前挂单
  - 发现止损单丢失 → 补挂 STOP_MARKET
  - 静默执行，无行动不推送；有补挂才写日志

这是 patrol 双层的第一层（纯脚本，不走 LLM）。
"""

import sys, json, logging
from pathlib import Path
from datetime import datetime, timezone

WORKSPACE = Path(__file__).parent.parent
sys.path.insert(0, str(WORKSPACE))
sys.path.insert(0, str(WORKSPACE.parent))

from modules.pos_state_store import get_all as ps_get_all, write as ps_write, remove as ps_remove
from modules.sl_handler      import place_sl, calc_qty

LOG_FILE = WORKSPACE / "logs" / "patrol_sl.log"
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(message)s",
                    handlers=[logging.FileHandler(LOG_FILE),
                               logging.StreamHandler()])
logger = logging.getLogger("patrol_sl")


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _get_api():
    try:
        import binance_api as api
        return api
    except ImportError:
        return None


def _get_open_order_ids(symbol: str, api) -> set[str]:
    """
    返回该 symbol 所有挂单的 id 集合（同时检查 algo 订单和普通订单）。
    止损单通过 place_algo_order 挂出，id 是 algoId（字符串）。
    """
    ids: set[str] = set()
    # 1. Algo 订单（止损/止盈通过 /fapi/v1/algoOrder 挂出）
    try:
        # 尝试两种可能的 API 方法名
        algo_orders = None
        if hasattr(api, 'get_algo_open_orders_cached'):
            algo_orders = api.get_algo_open_orders_cached()
        elif hasattr(api, 'get_algo_open_orders'):
            algo_orders = api.get_algo_open_orders(symbol=symbol)
        
        if algo_orders:
            for o in algo_orders:
                if o.get("symbol") == symbol:
                    algo_id = str(o.get("algoId", ""))
                    # 过滤测试 ID 和空值
                    if algo_id and algo_id not in ('', 'None', 'MOCK_ALGO', 'MOCK'):
                        ids.add(algo_id)
    except Exception as e:
        logger.debug(f"[patrol_sl] get_algo_open_orders {symbol} 失败：{e}")
    
    # 2. 普通订单（兜底，防止未来用 place_order 挂的止损）
    try:
        orders = api.get_open_orders(symbol=symbol) or []
        for o in orders:
            order_id = str(o.get("orderId", ""))
            if order_id and order_id not in ('', 'None', 'MOCK'):
                ids.add(order_id)
    except Exception as e:
        logger.debug(f"[patrol_sl] get_open_orders {symbol} 失败：{e}")
    
    return ids


def run(api=None) -> dict:
    """
    执行一轮止损巡逻。
    Returns: {"checked": N, "reattached": N, "errors": N}
    """
    if api is None:
        api = _get_api()

    pos_all = ps_get_all()
    result  = {"checked": 0, "reattached": 0, "errors": 0}

    if not pos_all:
        return result

    for key, pos in pos_all.items():
        symbol    = pos.get("symbol", "")
        direction = pos.get("direction", "")
        sl_price  = pos.get("sl_price", 0)
        sl_id     = pos.get("sl_order_id", "")
        entry     = pos.get("entry_price", 1)
        notional  = pos.get("notional", 0)

        if not symbol or not direction or not sl_price:
            continue

        result["checked"] += 1

        # 同时检查交易所是否有真实持仓；若已平仓则清理 pos_state 后跳过
        try:
            exchange_positions = api.get_positions() if api else []
            has_position = any(
                p.get("symbol") == symbol and float(p.get("positionAmt", 0)) != 0
                for p in exchange_positions
            )
        except Exception as e:
            logger.warning(f"[patrol_sl] get_positions {symbol} 失败：{e}")
            has_position = True  # 查询失败时保守处理，继续检查止损

        if not has_position:
            logger.info(f"[patrol_sl] {symbol} 实际无持仓，清理 pos_state 残留")
            ps_remove(symbol, direction)
            # 撤销该 symbol 残余 algo 订单（止损/止盈）
            if api:
                try:
                    stale = api.get_algo_open_orders_cached() or [] if hasattr(api, 'get_algo_open_orders_cached') else []
                    for o in stale:
                        if o.get("symbol") == symbol:
                            algo_id = o.get("algoId")
                            if algo_id:
                                api.cancel_algo_order(symbol, algo_id)
                                if hasattr(api, 'algo_cache_remove_order'):
                                    api.algo_cache_remove_order(algo_id)
                                logger.info(f"[patrol_sl] 撤销残余 algoId={algo_id}")
                except Exception as e:
                    logger.debug(f"[patrol_sl] 撤销残余订单失败：{e}")
            result["checked"] -= 1  # 不算作有效检查
            continue

        open_ids = _get_open_order_ids(symbol, api) if api else set()

        # 止损单存在 → 无需操作
        if sl_id and sl_id in open_ids:
            continue

        # 止损单丢失 → 补挂
        logger.warning(f"[patrol_sl] {symbol} 止损单丢失 (id={sl_id})，补挂 sl={sl_price}")
        qty = calc_qty(notional, entry)
        if qty <= 0:
            logger.error(f"[patrol_sl] {symbol} qty={qty} 无效，跳过")
            result["errors"] += 1
            continue

        ok, new_id = place_sl(symbol, direction, sl_price, qty, api=api)
        if ok:
            ps_write(symbol, direction, {
                "sl_order_id": new_id,
                "sl_reattached_at": _now_iso(),
            })
            logger.info(f"[patrol_sl] {symbol} 补挂成功 new_id={new_id}")
            result["reattached"] += 1
        else:
            logger.error(f"[patrol_sl] {symbol} 补挂失败！需要人工干预")
            result["errors"] += 1

    return result


if __name__ == "__main__":
    r = run()
    if r["reattached"] == 0 and r["errors"] == 0:
        print("HEARTBEAT_OK")
    else:
        print(f"patrol_sl: checked={r['checked']} reattached={r['reattached']} errors={r['errors']}")
