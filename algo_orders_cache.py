"""
algo_orders_cache.py — 本地缓存 algo 订单 ID，解决查询 endpoint 404 问题
"""
import json, os, time

CACHE_FILE = os.path.join(os.path.dirname(__file__), 'ws_cache', 'algo_orders.json')

def _load():
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    if not os.path.exists(CACHE_FILE):
        return {}
    try:
        with open(CACHE_FILE) as f:
            return json.load(f)
    except:
        return {}

def _save(data):
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    with open(CACHE_FILE, 'w') as f:
        json.dump(data, f, indent=2)

def save_order(symbol: str, order_type: str, algo_id: int, trigger_price: str,
               position_side: str, quantity: str):
    """保存一个新挂的 algo 单"""
    data = _load()
    key  = f"{symbol}_{order_type}_{position_side}"
    data[key] = {
        'symbol': symbol, 'orderType': order_type, 'positionSide': position_side,
        'algoId': algo_id, 'triggerPrice': trigger_price, 'quantity': quantity,
        'algoStatus': 'NEW', 'createTime': int(time.time() * 1000)
    }
    _save(data)

def remove_order(symbol: str, order_type: str, position_side: str):
    """删除一个 algo 单记录（撤单或触发后）"""
    data = _load()
    key = f"{symbol}_{order_type}_{position_side}"
    data.pop(key, None)
    _save(data)

def get_orders(symbol: str = None) -> list:
    """获取当前缓存的 algo 单列表"""
    data = _load()
    orders = list(data.values())
    if symbol:
        orders = [o for o in orders if o['symbol'] == symbol]
    return orders

def get_algo_id(symbol: str, order_type: str, position_side: str = 'LONG'):
    """获取指定仓位的 algo 单 ID"""
    data = _load()
    key = f"{symbol}_{order_type}_{position_side}"
    entry = data.get(key)
    return entry['algoId'] if entry else None
