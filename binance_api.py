"""
binance_api.py — Binance API 调用模块
使用 requests[socks] 走 SOCKS5 代理
"""
import hmac, hashlib, time, subprocess, sys
from urllib.parse import urlencode

# ── 自动确保 PySocks 已安装（防止环境重置后代理失效）──
try:
    import socks  # noqa: F401
except ImportError:
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "PySocks", "--break-system-packages", "-q"]
    )

import requests as _requests

API_KEY = "REDACTED_API_KEY"
SECRET  = "REDACTED_API_SECRET"

PROXY_URL = "socks5://REDACTED_PROXY_CREDENTIALS"
PROXIES   = {"http": PROXY_URL, "https": PROXY_URL}

BASE_FAPI = "https://fapi.binance.com"
BASE_API  = "https://api.binance.com"
HEADERS   = {"X-MBX-APIKEY": API_KEY}
TIMEOUT   = 15

def _sign(params: dict) -> dict:
    p = dict(params)
    p["timestamp"] = int(time.time() * 1000)
    qs  = urlencode(p)
    sig = hmac.new(SECRET.encode(), qs.encode(), hashlib.sha256).hexdigest()
    p["signature"] = sig
    return p

def request(method: str, url: str, params: dict = {}) -> dict:
    p = _sign(params)
    method = method.upper()
    try:
        if method == "GET":
            r = _requests.get(url, params=p, headers=HEADERS, proxies=PROXIES, timeout=TIMEOUT)
        elif method == "POST":
            r = _requests.post(url, data=p, headers=HEADERS, proxies=PROXIES, timeout=TIMEOUT)
        elif method == "DELETE":
            r = _requests.delete(url, params=p, headers=HEADERS, proxies=PROXIES, timeout=TIMEOUT)
        elif method == "PUT":
            r = _requests.put(url, data=p, headers=HEADERS, proxies=PROXIES, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    except _requests.exceptions.HTTPError as e:
        try: return e.response.json()
        except: return {"error": str(e)}
    except Exception as e:
        return {"error": str(e)}

def get_positions() -> list:
    r = request("GET", f"{BASE_FAPI}/fapi/v3/positionRisk")
    if isinstance(r, list):
        return [p for p in r if float(p.get("positionAmt", 0)) != 0]
    return []

def get_open_orders(symbol: str = None) -> list:
    p = {}
    if symbol: p["symbol"] = symbol
    r = request("GET", f"{BASE_FAPI}/fapi/v1/openOrders", p)
    return r if isinstance(r, list) else []

# ── 新 Algo Order 接口（直接走代理，不依赖 binance-cli）──────────────────────

def get_algo_open_orders(symbol: str = None) -> list:
    """查询当前 CONDITIONAL 挂单，使用正确端点 /fapi/v1/openAlgoOrders"""
    p = {}
    if symbol: p['symbol'] = symbol
    r = request('GET', f'{BASE_FAPI}/fapi/v1/openAlgoOrders', p)
    if isinstance(r, list): return r
    if isinstance(r, dict): return r.get('orders', r.get('algoOrders', []))
    return []

def cancel_algo_order(symbol: str, algo_id: int) -> dict:
    """DELETE /fapi/v1/algoOrder — 撤销 CONDITIONAL 单"""
    r = request('DELETE', f'{BASE_FAPI}/fapi/v1/algoOrder',
                {'symbol': symbol, 'algoId': int(algo_id)})
    if isinstance(r, dict) and 'algoId' in r:
        return {'code': '200', 'algoId': r['algoId']}
    return r

def place_algo_order(symbol: str, side: str, order_type: str, trigger_price,
                     position_side: str = "LONG", quantity=None) -> dict:
    """挂 CONDITIONAL 止损/止盈单（主入口，STOP_MARKET 用 MARK_PRICE，TP 用 CONTRACT_PRICE）"""
    if quantity is None:
        raise ValueError("quantity 必填")
    working_type = "MARK_PRICE" if order_type == "STOP_MARKET" else "CONTRACT_PRICE"
    return request('POST', f'{BASE_FAPI}/fapi/v1/algoOrder', {
        'symbol': symbol, 'side': side, 'positionSide': position_side,
        'type': order_type, 'triggerPrice': str(trigger_price),
        'quantity': str(quantity), 'workingType': working_type,
        'algoType': 'CONDITIONAL',
    })

def place_algo_order_v2(symbol: str, side: str, order_type: str, trigger_price,
                        position_side: str, quantity) -> dict:
    """place_algo_order 别名，向下兼容"""
    return place_algo_order(symbol, side, order_type, trigger_price, position_side, quantity)

def place_conditional_limit_sl(symbol: str, side: str, position_side: str,
                                trigger_price: float, limit_price: float,
                                quantity, tick_size: str = None) -> dict:
    """
    挂条件限价止损单（STOP 类型）。
    触发价到达后挂限价单，而非直接市价成交，给价格回调留空间。
    trigger_price: 触发价（通常略低于止损位）
    limit_price:   限价（通常比触发价再低0.1%~0.3%，防止挂上去直接吃掉）
    """
    if tick_size:
        trigger_price = round_by_tick(trigger_price, tick_size)
        limit_price   = round_by_tick(limit_price,   tick_size)
    return request('POST', f'{BASE_FAPI}/fapi/v1/order', {
        'symbol':       symbol,
        'side':         side,
        'type':         'STOP',
        'positionSide': position_side,
        'stopPrice':    str(trigger_price),
        'price':        str(limit_price),
        'quantity':     str(quantity),
        'reduceOnly':   'true',
        'timeInForce':  'GTC',
        'workingType':  'MARK_PRICE',
    })

# ── 本地 algo 缓存（解决 /fapi/v1/algoOpenOrders 404 问题）─────────────────────

import json as _json_mod
import os as _os_mod

_ALGO_CACHE = _os_mod.path.join(_os_mod.path.dirname(__file__), 'ws_cache', 'algo_orders.json')

def _load_algo_cache() -> dict:
    _os_mod.makedirs(_os_mod.path.dirname(_ALGO_CACHE), exist_ok=True)
    if not _os_mod.path.exists(_ALGO_CACHE): return {}
    try:
        with open(_ALGO_CACHE) as f: return _json_mod.load(f)
    except: return {}

def _save_algo_cache(data: dict):
    _os_mod.makedirs(_os_mod.path.dirname(_ALGO_CACHE), exist_ok=True)
    with open(_ALGO_CACHE, 'w') as f: _json_mod.dump(data, f, indent=2)

def algo_cache_save(symbol: str, order_type: str, algo_id: int, trigger: str, label: str = ""):
    """下单成功后调用，保存到本地缓存"""
    data = _load_algo_cache()
    if not label:
        label = 'SL' if order_type == 'STOP_MARKET' else 'TP'
    data.setdefault(symbol, {})[label] = {
        'algoId': algo_id, 'triggerPrice': trigger, 'orderType': order_type
    }
    _save_algo_cache(data)

def algo_cache_remove_symbol(symbol: str):
    """仓位关闭时调用，清除该 symbol 的所有 algo 记录"""
    data = _load_algo_cache()
    if symbol in data:
        del data[symbol]
        _save_algo_cache(data)

def algo_cache_remove_order(symbol: str, label: str):
    """撤单后调用，清除单条记录（label = 'SL' or 'TP'）"""
    data = _load_algo_cache()
    if symbol in data and label in data[symbol]:
        del data[symbol][label]
        if not data[symbol]: del data[symbol]
        _save_algo_cache(data)

def get_algo_open_orders_cached(symbol: str = None) -> list:
    """从本地缓存读取 algo 单列表（格式与 Binance API 兼容）"""
    data = _load_algo_cache()
    orders = []
    for sym, labels in data.items():
        if symbol and sym != symbol: continue
        for label, o in labels.items():
            orders.append({
                'symbol': sym, 'algoId': o['algoId'],
                'orderType': o['orderType'], 'triggerPrice': o['triggerPrice'],
                'algoStatus': 'NEW', 'positionSide': 'LONG',
            })
    return orders


# ── 风控仓位计算 ─────────────────────────────────────────────────────────────────

def calc_safe_notional(entry_price: float, sl_price: float,
                       circuit_loss_limit: float = 100.0,
                       daily_loss_so_far: float = 0.0,
                       max_notional: float = 300.0) -> float:
    """
    根据熔断线反推安全名义量。
    规则：单笔最大亏损 ≤ min(剩余熔断空间×40%, 20U)
    返回建议名义量（USDT），调用方再除以价格得到数量。
    """
    sl_pct    = abs(sl_price - entry_price) / entry_price
    remaining = max(0.0, circuit_loss_limit - daily_loss_so_far)
    max_loss  = min(remaining * 0.40, 20.0)
    if sl_pct <= 0 or max_loss <= 0:
        return 0.0
    notional = max_loss / sl_pct
    return max(50.0, min(notional, max_notional))


# ── 公共工具函数（供所有脚本共用）────────────────────────────────────────────────

import os as _os, json as _json, time as _time

_EXCHANGE_INFO_CACHE_FILE = "/root/.openclaw/workspace/ws_cache/exchange_info_cache.json"
_EXCHANGE_INFO_TTL = 3600  # 1小时刷新一次

def get_exchange_info(force_refresh=False) -> dict:
    """
    获取合约 exchangeInfo，带文件缓存（1小时TTL）。
    避免每次调用都下载 200KB 完整规则表。
    """
    now = _time.time()
    # 读缓存
    if not force_refresh and _os.path.exists(_EXCHANGE_INFO_CACHE_FILE):
        try:
            with open(_EXCHANGE_INFO_CACHE_FILE) as f:
                cached = _json.load(f)
            if now - cached.get("_cached_at", 0) < _EXCHANGE_INFO_TTL:
                return cached
        except: pass
    # 重新拉取
    r = _requests.get(f"{BASE_FAPI}/fapi/v1/exchangeInfo",
                      proxies=PROXIES, timeout=12).json()
    r["_cached_at"] = now
    try:
        with open(_EXCHANGE_INFO_CACHE_FILE, "w") as f:
            _json.dump(r, f)
    except: pass
    return r


def get_sym_info_cached(sym: str):
    """
    获取单个合约的 tickSize 和 stepSize，使用缓存版 exchangeInfo。
    返回 (tick_size: float, step_size: float)
    """
    info = get_exchange_info()
    s = next((x for x in info.get("symbols", []) if x["symbol"] == sym), None)
    if not s:
        return 0.0001, 1.0
    tick = float(next(f["tickSize"] for f in s["filters"] if f["filterType"] == "PRICE_FILTER"))
    step = float(next(f["stepSize"] for f in s["filters"] if f["filterType"] == "LOT_SIZE"))
    return tick, step


def get_klines(sym: str, interval: str, limit: int = 60) -> list:
    """get_klines — 兼容别名，等同于 get_klines_proxy"""
    return get_klines_proxy(sym, interval, limit)

def get_klines_proxy(sym: str, interval: str, limit: int = 60) -> list:
    """
    拉取合约K线，走代理。超时资动提至 15s + 自动重试一次。
    """
    last_err = None
    for attempt in range(2):  # 最多重试一次
        try:
            r = _requests.get(f"{BASE_FAPI}/fapi/v1/klines",
                              params={"symbol": sym, "interval": interval, "limit": limit},
                              proxies=PROXIES, timeout=15)
            return r.json()
        except Exception as e:
            last_err = e
            if attempt == 0:
                import time as _t; _t.sleep(1)
    raise last_err


# ── 主流币判断 ────────────────────────────────────────────────────────────────────

_MAJOR_SYMS = {
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT",
    "DOGEUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT", "MATICUSDT", "LTCUSDT",
    "UNIUSDT", "ATOMUSDT", "ETCUSDT", "FILUSDT", "APTUSDT", "NEARUSDT",
    "ARBUSDT", "OPUSDT",
}
_MAJOR_VOL_THRESHOLD = 1_000_000_000  # 24h成交量 > 10亿视为主流

def is_major(sym: str, vol_24h: float = 0) -> bool:
    """判断是否为主流币（固定名单 或 24h成交量 > 10亿）"""
    if sym in _MAJOR_SYMS:
        return True
    if vol_24h >= _MAJOR_VOL_THRESHOLD:
        return True
    return False

def classify_symbol(sym: str, vol_24h: float = 0, atr_pct: float = 0) -> str:
    """
    三档分类：
      'major'        — 主流币（BTC/ETH/SOL 等固定名单 或 24h成交量>10亿）
      'volatile_alt' — 高波动山寨币（非主流 且 ATR/price > 12%）
      'altcoin'      — 普通山寨币（非主流 且 ATR/price <= 12%）
    atr_pct: ATR/当前价格 * 100，由调用方传入；传0则不做高波动判断
    """
    if is_major(sym, vol_24h):
        return 'major'
    if atr_pct > 12:
        return 'volatile_alt'
    return 'altcoin'


# ── 被 market_context.py 引用的辅助函数（删除旧代码时误删，补回）────────────

def get_mark_price(symbol: str) -> dict:
    """GET /fapi/v1/premiumIndex — 返回 markPrice / lastFundingRate 等"""
    return request('GET', f'{BASE_FAPI}/fapi/v1/premiumIndex', {'symbol': symbol})


def get_ticker_24h(symbol: str = '') -> dict | list:
    """GET /fapi/v1/ticker/24hr"""
    params = {'symbol': symbol} if symbol else {}
    return request('GET', f'{BASE_FAPI}/fapi/v1/ticker/24hr', params)

# ── scanner.py 依赖的下单/撤单接口（补回缺失函数）─────────────────────────

def place_order(symbol: str, side: str, order_type: str,
                price: str = None, quantity=None,
                timeInForce: str = None, positionSide: str = None,
                stopPrice: str = None, closePosition: bool = False,
                **kwargs) -> dict:
    """POST /fapi/v1/order — 下合约限价/市价单"""
    p = {'symbol': symbol, 'side': side, 'type': order_type}
    if quantity      is not None: p['quantity']      = str(quantity)
    if price         is not None: p['price']         = str(price)
    if timeInForce   is not None: p['timeInForce']   = timeInForce
    if positionSide  is not None: p['positionSide']  = positionSide
    if stopPrice     is not None: p['stopPrice']     = str(stopPrice)
    if closePosition             : p['closePosition'] = 'true'
    p.update(kwargs)
    return request('POST', f'{BASE_FAPI}/fapi/v1/order', p)


def cancel_order(symbol: str, order_id: int) -> dict:
    """DELETE /fapi/v1/order — 撤销普通挂单"""
    return request('DELETE', f'{BASE_FAPI}/fapi/v1/order',
                   {'symbol': symbol, 'orderId': int(order_id)})


def get_ticker_price(symbol: str = '') -> dict | list:
    """GET /fapi/v1/ticker/price — 最新成交价"""
    params = {'symbol': symbol} if symbol else {}
    return request('GET', f'{BASE_FAPI}/fapi/v1/ticker/price', params)


def get_listen_key() -> str:
    """POST /fapi/v1/listenKey — 获取 user-data stream listenKey"""
    r = request('POST', f'{BASE_FAPI}/fapi/v1/listenKey', {})
    key = r.get('listenKey', '')
    if not key:
        raise RuntimeError(f'get_listen_key failed: {r}')
    return key


def keep_alive_listen_key(listen_key: str) -> dict:
    """PUT /fapi/v1/listenKey — 延续 listenKey（每30分钟调一次）
    代理不支持 PUT 时自动降级：重新 POST 获取新 key 并返回。
    """
    # 先尝试标准 PUT
    p = _sign({'listenKey': listen_key})
    try:
        r = _requests.put(
            f'{BASE_FAPI}/fapi/v1/listenKey',
            data=p, headers=HEADERS, proxies=PROXIES, timeout=TIMEOUT
        )
        r.raise_for_status()
        result = r.json() if r.text else {}
        if not result.get('error') and 'Unsupported' not in str(result):
            return {'ok': True, 'method': 'PUT'}
    except Exception:
        pass
    # PUT 失败——尝试重新获取新 listenKey（等效 keepalive）
    try:
        new_key = get_listen_key()
        if new_key:
            return {'ok': True, 'method': 'POST_renew', 'new_key': new_key}
    except Exception as e:
        return {'error': str(e)}
    return {'error': 'keepalive_failed'}


def get_order(symbol: str, order_id: int) -> dict:
    """GET /fapi/v1/order — 查单个订单状态"""
    return request('GET', f'{BASE_FAPI}/fapi/v1/order',
                   {'symbol': symbol, 'orderId': int(order_id)})


def get_top_gainers(limit: int = 20) -> list:
    """返回涨幅榜前 N 个 USDT 合约标的"""
    tickers = get_ticker_24h()
    if not isinstance(tickers, list):
        return []
    usdt = [t for t in tickers if t.get('symbol', '').endswith('USDT')]
    usdt.sort(key=lambda x: float(x.get('priceChangePercent', 0)), reverse=True)
    return usdt[:limit]


def get_top_losers(limit: int = 20) -> list:
    """返回跌幅榜前 N 个 USDT 合约标的"""
    tickers = get_ticker_24h()
    if not isinstance(tickers, list):
        return []
    usdt = [t for t in tickers if t.get('symbol', '').endswith('USDT')]
    usdt.sort(key=lambda x: float(x.get('priceChangePercent', 0)))
    return usdt[:limit]


def get_top_volume(limit: int = 60) -> list:
    """返回成交量榜前 N 个 USDT 合约标的"""
    tickers = get_ticker_24h()
    if not isinstance(tickers, list):
        return []
    usdt = [t for t in tickers if t.get('symbol', '').endswith('USDT')]
    usdt.sort(key=lambda x: float(x.get('quoteVolume', 0)), reverse=True)
    return usdt[:limit]
