"""
modules/binance_api.py — v3 Binance API 适配层

封装旧 workspace/binance_api.py 的接口，统一方法名供 v3 模块调用。
测试时用 MockAPI 替换，生产时自动引用真实实现。

注意：本模块不使用 import binance_api，而是直接加载 workspace 根目录的 binance_api.py 文件
以避免循环导入。
"""

import sys
import importlib.util
from pathlib import Path

# ── 加载真实 binance_api（workspace 根目录）────────────────────────────────────

_WS_ROOT = Path(__file__).parent.parent.parent  # /workspace
_REAL_API = None

try:
    _api_path = _WS_ROOT / "binance_api.py"
    if _api_path.exists():
        spec = importlib.util.spec_from_file_location("binance_api_real", _api_path)
        if spec and spec.loader:
            _REAL_API = importlib.util.module_from_spec(spec)
            sys.modules["binance_api_real"] = _REAL_API
            spec.loader.exec_module(_REAL_API)
except Exception as e:
    print(f"[v3/modules/binance_api] 加载真实 API 失败：{e}")


# ── 适配层函数 ───────────────────────────────────────────────────────────────

def get_klines(symbol: str, interval: str, limit: int = 100) -> list:
    if _REAL_API:
        return _REAL_API.get_klines(symbol, interval, limit=limit)
    return []


def get_ticker(symbol: str) -> dict:
    """
    返回统一格式：
    { lastPrice, lastFundingRate, priceChangePercent }
    """
    if _REAL_API:
        mark = _REAL_API.get_mark_price(symbol)
        t24  = _REAL_API.get_ticker_24h(symbol)
        return {
            "lastPrice":          str(mark.get("markPrice", 0)),
            "lastFundingRate":    str(mark.get("lastFundingRate", 0)),
            "priceChangePercent": str(t24.get("priceChangePercent", 0)),
        }
    return {"lastPrice": "0", "lastFundingRate": "0", "priceChangePercent": "0"}


def get_top_gainers(limit: int = 20) -> list[dict]:
    """返回 24h 涨幅前 N 的 USDT 永续合约标的"""
    if _REAL_API:
        try:
            tickers = _REAL_API.get_ticker_24h()  # 全量
            usdt = [t for t in tickers if str(t.get("symbol", "")).endswith("USDT")]
            usdt.sort(key=lambda x: float(x.get("priceChangePercent", 0)), reverse=True)
            return [{"symbol": t["symbol"]} for t in usdt[:limit]]
        except Exception:
            return []
    return []


def get_top_losers(limit: int = 20) -> list[dict]:
    if _REAL_API:
        try:
            tickers = _REAL_API.get_ticker_24h()
            usdt = [t for t in tickers if str(t.get("symbol", "")).endswith("USDT")]
            usdt.sort(key=lambda x: float(x.get("priceChangePercent", 0)))
            return [{"symbol": t["symbol"]} for t in usdt[:limit]]
        except Exception:
            return []
    return []


def get_top_volume(limit: int = 60) -> list[dict]:
    if _REAL_API:
        try:
            tickers = _REAL_API.get_ticker_24h()
            usdt = [t for t in tickers if str(t.get("symbol", "")).endswith("USDT")]
            usdt.sort(key=lambda x: float(x.get("quoteVolume", 0)), reverse=True)
            return [{"symbol": t["symbol"]} for t in usdt[:limit]]
        except Exception:
            return []
    return []


def get_all_tickers() -> list[dict]:
    """全量 ticker_24h，返回 list[{symbol, lastPrice, priceChangePercent, quoteVolume}]"""
    if _REAL_API:
        try:
            return _REAL_API.get_ticker_24h() or []
        except Exception:
            return []
    return []


def get_all_mark_prices() -> list[dict]:
    """全量 markPrice+fundingRate，返回 list[{symbol, markPrice, lastFundingRate}]"""
    if _REAL_API:
        try:
            result = _REAL_API.get_mark_price("")  # 空字符串=全量
            return result if isinstance(result, list) else []
        except Exception:
            return []
    return []


def get_futures_account() -> dict:
    """返回合约账户信息 {totalWalletBalance, availableBalance, ...}"""
    if _REAL_API:
        try:
            return _REAL_API.get_futures_account()
        except Exception:
            return {}
    return {"totalWalletBalance": "0", "availableBalance": "0"}


def get_futures_positions() -> list:
    if _REAL_API:
        return _REAL_API.get_positions()
    return []


def get_open_orders(symbol: str = None) -> list:
    if _REAL_API:
        return _REAL_API.get_open_orders(symbol=symbol) or []
    return []


def place_order(**kwargs) -> dict:
    if _REAL_API:
        return _REAL_API.place_order(**kwargs)
    return {"orderId": "MOCK", "status": "NEW"}


def cancel_order(symbol: str, orderId: str) -> dict:
    if _REAL_API:
        return _REAL_API.cancel_order(symbol=symbol, order_id=int(orderId))
    return {}


def get_order(symbol: str, orderId: str) -> dict:
    """查询单笔订单状态 {orderId, status, avgPrice, price, ...}"""
    if _REAL_API:
        try:
            return _REAL_API.get_order(symbol=symbol, order_id=int(orderId))
        except Exception:
            return {}
    return {}


def place_algo_order(symbol: str, side: str, order_type: str,
                     trigger_price: float, position_side: str,
                     quantity: float) -> dict:
    """挂 STOP_MARKET 止损单 (/fapi/v1/algoOrder)"""
    if _REAL_API:
        try:
            return _REAL_API.place_algo_order(
                symbol=symbol,
                side=side,
                order_type=order_type,
                trigger_price=trigger_price,
                position_side=position_side,
                quantity=quantity,
            )
        except Exception:
            return {}
    return {"algoId": "MOCK_ALGO", "orderId": "MOCK_ALGO", "status": "NEW"}


# ── Algo 订单缓存方法（委托给真实 API）────────────────────────────────────────

def get_algo_open_orders_cached() -> list:
    """查询当前 CONDITIONAL 挂单（使用真实 API 的缓存方法）"""
    if _REAL_API and hasattr(_REAL_API, 'get_algo_open_orders_cached'):
        return _REAL_API.get_algo_open_orders_cached()
    return []


def get_algo_open_orders(symbol: str = None) -> list:
    """查询当前 CONDITIONAL 挂单（带 symbol 过滤）"""
    if _REAL_API and hasattr(_REAL_API, 'get_algo_open_orders'):
        return _REAL_API.get_algo_open_orders(symbol=symbol)
    return []


def cancel_algo_order(symbol: str, algo_id: int) -> dict:
    """撤销 CONDITIONAL 订单"""
    if _REAL_API and hasattr(_REAL_API, 'cancel_algo_order'):
        return _REAL_API.cancel_algo_order(symbol=symbol, algo_id=int(algo_id))
    return {}


def algo_cache_remove_order(algo_id: int):
    """从本地缓存移除订单"""
    if _REAL_API and hasattr(_REAL_API, 'algo_cache_remove_order'):
        _REAL_API.algo_cache_remove_order(algo_id)
