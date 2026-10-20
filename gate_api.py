"""
Gate.io REST API 客户端
支持：现货账户、永续合约、划转、充提币
使用 HMAC-SHA512 签名，通过 SOCKS5 代理访问
"""
import os
import hashlib, hmac, time, json, urllib.parse
import urllib.request, socks, socket
from typing import Optional

GATE_KEY    = "31fa24489c3c7d862575696fa7daf2fa"
GATE_SECRET = os.environ.get("GATE_API_GATE_SECRET", "")
BASE_URL    = "https://api.gateio.ws/api/v4"
# 代理凭证从环境变量读取
_proxy_raw2 = os.environ.get("BINANCE_PROXY", "")
def _parse_proxy2(url):
    import urllib.parse; p = urllib.parse.urlparse(url)
    return p.hostname or "", p.port or 6688, p.username or "", p.password or ""
PROXY_HOST, PROXY_PORT, PROXY_USER, PROXY_PASS = _parse_proxy2(_proxy_raw2)

def _proxy_opener():
    """创建走 SOCKS5 代理的 opener"""
    import socks, socket
    socks.set_default_proxy(socks.SOCKS5, PROXY_HOST, PROXY_PORT,
                            username=PROXY_USER, password=PROXY_PASS)
    socket.socket = socks.socksocket
    return urllib.request.build_opener()

def _sign(method: str, path: str, query: str, body: str, ts: str) -> str:
    hashed_body = hashlib.sha512(body.encode()).hexdigest()
    msg = f"{method}\n{path}\n{query}\n{hashed_body}\n{ts}"
    return hmac.new(GATE_SECRET.encode(), msg.encode(), hashlib.sha512).hexdigest()

def request(method: str, endpoint: str,
            params: Optional[dict] = None,
            body: Optional[dict] = None,
            signed: bool = True) -> dict:
    params = params or {}
    query  = urllib.parse.urlencode(params)
    body_s = json.dumps(body) if body else ""
    ts     = str(int(time.time()))
    path   = f"/api/v4{endpoint}"
    url    = f"{BASE_URL}{endpoint}"
    if query:
        url += "?" + query

    headers = {
        "Content-Type": "application/json",
        "Accept":       "application/json",
    }
    if signed:
        headers["KEY"]       = GATE_KEY
        headers["Timestamp"] = ts
        headers["SIGN"]      = _sign(method.upper(), path, query, body_s, ts)

    data = body_s.encode() if body_s else None
    req  = urllib.request.Request(url, data=data, headers=headers, method=method.upper())

    opener = _proxy_opener()
    try:
        with opener.open(req, timeout=20) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        err = e.read().decode()
        try:    return json.loads(err)
        except: return {"error": err, "status": e.code}

# ── 常用接口封装 ──────────────────────────────────────────────

def get_spot_accounts():
    """现货账户余额列表"""
    return request("GET", "/spot/accounts")

def get_futures_accounts(settle="usdt"):
    """永续合约账户（settle=usdt）"""
    return request("GET", f"/futures/{settle}/accounts")

def get_deposit_address(currency: str, chain: str):
    """获取充值地址"""
    return request("GET", "/withdrawals/deposit-address",
                   params={"currency": currency, "chain": chain})

def transfer(currency: str, from_: str, to: str, amount: str, settle: str = "usdt"):
    """账户间划转 (spot ↔ futures)"""
    return request("POST", "/wallet/transfers", body={
        "currency": currency,
        "from":     from_,
        "to":       to,
        "amount":   amount,
        "settle":   settle,
    })

def futures_order(settle: str, contract: str, size: int,
                  price: str = "0", tif: str = "ioc",
                  reduce_only: bool = False):
    """
    永续合约下单
    size > 0 = 做多, size < 0 = 做空
    price=0 + tif=ioc = 市价单
    """
    return request("POST", f"/futures/{settle}/orders", body={
        "contract":    contract,
        "size":        size,
        "price":       price,
        "tif":         tif,
        "reduce_only": reduce_only,
    })

def futures_cancel_order(settle: str, order_id: str):
    return request("DELETE", f"/futures/{settle}/orders/{order_id}")

def futures_positions(settle="usdt"):
    return request("GET", f"/futures/{settle}/positions")

def get_futures_contract(settle: str, contract: str):
    return request("GET", f"/futures/{settle}/contracts/{contract}", signed=False)

if __name__ == "__main__":
    print("=== Gate API 连通性测试 ===\n")
    # 1. 现货余额
    acct = get_spot_accounts()
    if isinstance(acct, list):
        print("✅ 现货账户连接正常")
        bal_usdt = next((a for a in acct if a.get('currency') == 'USDT'), None)
        if bal_usdt:
            print(f"   USDT 可用: {bal_usdt.get('available','0')}  冻结: {bal_usdt.get('locked','0')}")
        else:
            print("   暂无 USDT 余额")
    else:
        print(f"❌ 现货账户: {acct}")

    # 2. 合约账户
    fut = get_futures_accounts()
    if isinstance(fut, dict) and 'total' in fut:
        print(f"✅ 合约账户连接正常")
        print(f"   总权益: {fut.get('total','0')} USDT  可用: {fut.get('available','0')}")
    else:
        print(f"❌ 合约账户: {fut}")
