import requests, hmac, hashlib, time, urllib.parse, json, math

PROXY = "socks5://REDACTED_PROXY_CREDENTIALS"
proxies = {"http": PROXY, "https": PROXY}
API_KEY = 'REDACTED_API_KEY'
SECRET  = 'REDACTED_API_SECRET'

def signed_post(path, params={}, base='https://api.binance.com'):
    p = dict(params)
    p['timestamp'] = int(time.time()*1000)
    qs = urllib.parse.urlencode(p)
    sig = hmac.new(SECRET.encode(), qs.encode(), hashlib.sha256).hexdigest()
    r = requests.post(f'{base}{path}?{qs}&signature={sig}',
                      headers={'X-MBX-APIKEY': API_KEY}, proxies=proxies, timeout=15)
    return r.json()

def signed_get(path, params={}, base='https://api.binance.com'):
    p = dict(params)
    p['timestamp'] = int(time.time()*1000)
    qs = urllib.parse.urlencode(p)
    sig = hmac.new(SECRET.encode(), qs.encode(), hashlib.sha256).hexdigest()
    r = requests.get(f'{base}{path}?{qs}&signature={sig}',
                     headers={'X-MBX-APIKEY': API_KEY}, proxies=proxies, timeout=15)
    return r.json()

def transfer(asset, amount, type_):
    # type 1=现货→合约, 2=合约→现货
    r = signed_post('/sapi/v1/futures/transfer',
                    {'asset': asset, 'amount': str(amount), 'type': str(type_)})
    return r

def get_depth(symbol):
    r = requests.get(f'https://api.binance.com/api/v3/depth?symbol={symbol}&limit=1',
                     proxies=proxies, timeout=10).json()
    return float(r['bids'][0][0]), float(r['asks'][0][0])

def buy_and_sell(symbol, usdc_amount):
    """买入后立即卖出，返回实际磨损(USDC)"""
    # 获取精度
    ei = requests.get('https://api.binance.com/api/v3/exchangeInfo',
                      proxies=proxies, timeout=10).json()
    sym_info = next((s for s in ei['symbols'] if s['symbol'] == symbol), None)
    if not sym_info:
        return None, f'{symbol} not found'
    filters = {f['filterType']: f for f in sym_info['filters']}
    step = float(filters['LOT_SIZE']['stepSize'])
    decimals = len(str(step).rstrip('0').split('.')[-1]) if '.' in str(step) else 0

    # 买入
    bid, ask = get_depth(symbol)
    qty = math.floor((usdc_amount / ask) / step) * step
    qty = round(qty, decimals)
    print(f'  买入 {qty} 份 @ ask={ask}，预计花费 {qty*ask:.4f} USDC')

    buy_order = signed_post('/api/v3/order', {
        'symbol': symbol, 'side': 'BUY', 'type': 'MARKET', 'quantity': str(qty)
    })
    print(f'  BUY 结果: orderId={buy_order.get("orderId")} status={buy_order.get("status")}')
    if 'orderId' not in buy_order:
        return None, buy_order

    buy_cost = float(buy_order.get('cummulativeQuoteQty', usdc_amount))
    buy_qty  = float(buy_order.get('executedQty', qty))
    buy_fee  = sum(float(f['commission']) for f in buy_order.get('fills', []))

    time.sleep(1)

    # 卖出
    sell_order = signed_post('/api/v3/order', {
        'symbol': symbol, 'side': 'SELL', 'type': 'MARKET', 'quantity': str(buy_qty)
    })
    print(f'  SELL 结果: orderId={sell_order.get("orderId")} status={sell_order.get("status")}')

    sell_revenue = float(sell_order.get('cummulativeQuoteQty', 0))
    sell_fee     = sum(float(f['commission']) for f in sell_order.get('fills', []))

    total_loss = buy_cost - sell_revenue + buy_fee + sell_fee
    return round(total_loss, 4), {'buy': buy_order, 'sell': sell_order,
                                   'buy_cost': buy_cost, 'sell_revenue': sell_revenue,
                                   'buy_fee': buy_fee, 'sell_fee': sell_fee}

# ===== 主流程 =====
print("=== 股票任务第一次执行 ===")
print(f"时间: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")

# Step 1: 划转 505 USDC 合约→现货
print("\n[1] 划转 505 USDC 合约→现货...")
r = transfer('USDC', 505, 2)
print(f"划转结果: {r}")
if 'tranId' not in r:
    print("❌ 划转失败，停止")
    exit(1)
time.sleep(3)

# Step 2: 第一次买卖 SPYB/USDC
print("\n[2] 第一次买卖 SPYBUSDC（标普500，磨损最小）...")
# 优先用 SPYBUSDC，如果没有则用 SPYBUSDT
symbol = 'SPYBUSDC'
# 检查是否有 SPYBUSDC，没有则用 SPYBUSDT
ei_check = requests.get('https://api.binance.com/api/v3/exchangeInfo',
                         proxies=proxies, timeout=10).json()
available = [s['symbol'] for s in ei_check['symbols']
             if 'SPY' in s['symbol'] and s['quoteAsset'] in ['USDC','USDT']]
print(f"可用 SPY 交易对: {available}")
if 'SPYBUSDC' in available:
    symbol = 'SPYBUSDC'
elif 'SPYBUSDT' in available:
    symbol = 'SPYBUSDT'
    print("⚠️ 使用 SPYBUSDT（USDC 对不存在）")

loss, detail = buy_and_sell(symbol, 500)

if loss is None:
    print(f"❌ 下单失败: {detail}")
    print("\n[X] 划回 USDC...")
    transfer('USDC', 505, 1)
    exit(1)

print(f"\n{'='*40}")
print(f"✅ 第一次买卖完成！")
print(f"   交易对: {symbol}")
print(f"   买入花费: {detail['buy_cost']:.4f} USDC")
print(f"   卖出收回: {detail['sell_revenue']:.4f} USDC")
print(f"   手续费:   {detail['buy_fee']+detail['sell_fee']:.4f} USDC")
print(f"   实际磨损: {loss} USDC")
print(f"{'='*40}")

result = {
    'loss': loss,
    'symbol': symbol,
    'status': 'first_done',
    'buy_cost': detail['buy_cost'],
    'sell_revenue': detail['sell_revenue'],
    'fee': detail['buy_fee'] + detail['sell_fee']
}
with open('/tmp/stock_task_result.json', 'w') as f:
    json.dump(result, f)

print(f"\nRESULT_JSON:{json.dumps(result)}")
