"""
OKX REST API 客户端
支持：资金账户、合约账户、划转、充提币、永续合约开平仓
使用 HMAC-SHA256 签名 + Passphrase，通过 SOCKS5 代理访问
"""
import os
import hashlib, hmac, base64, time, json, urllib.parse
from typing import Optional
import requests

OKX_KEY        = os.environ.get("OKX_API_KEY", "")
OKX_SECRET     = os.environ.get("OKX_API_SECRET", "")
OKX_PASSPHRASE = os.environ.get("OKX_API_PASSPHRASE", "")
BASE_URL       = "https://www.okx.com"
PROXY_HOST     = os.environ.get("OKX_PROXY_HOST", "")
PROXY_PORT     = int(os.environ.get("OKX_PROXY_PORT", "0") or 0)
PROXY_USER     = os.environ.get("OKX_PROXY_USER", "")
PROXY_PASS     = os.environ.get("OKX_PROXY_PASS", "")

PROXIES = {
    "http":  f"socks5h://{PROXY_USER}:{PROXY_PASS}@{PROXY_HOST}:{PROXY_PORT}",
    "https": f"socks5h://{PROXY_USER}:{PROXY_PASS}@{PROXY_HOST}:{PROXY_PORT}",
}
HEADERS_BASE = {
    "Content-Type": "application/json",
    "Accept":       "application/json",
    "User-Agent":   "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36",
}

def _sign(ts: str, method: str, path: str, body: str = "") -> str:
    msg = ts + method.upper() + path + (body or "")
    return base64.b64encode(
        hmac.new(OKX_SECRET.encode(), msg.encode(), hashlib.sha256).digest()
    ).decode()

def request(method: str, endpoint: str,
            params: Optional[dict] = None,
            body: Optional[dict] = None,
            signed: bool = True) -> dict:
    params  = params or {}
    query   = urllib.parse.urlencode(params)
    body_s  = json.dumps(body) if body else ""
    ts      = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    path    = endpoint + ("?" + query if query else "")
    url     = BASE_URL + path

    headers = dict(HEADERS_BASE)
    if signed:
        headers["OK-ACCESS-KEY"]        = OKX_KEY
        headers["OK-ACCESS-SIGN"]       = _sign(ts, method, path, body_s)
        headers["OK-ACCESS-TIMESTAMP"]  = ts
        headers["OK-ACCESS-PASSPHRASE"] = OKX_PASSPHRASE

    resp = requests.request(
        method.upper(), url,
        headers=headers,
        data=body_s if body_s else None,
        proxies=PROXIES,
        timeout=20,
    )
    try:
        return resp.json()
    except Exception:
        return {"error": resp.text, "status": resp.status_code}

# ── 常用接口封装 ──────────────────────────────────────────────

def get_asset_balances(ccy: str = "USDT"):
    """资金账户余额（用于充提）"""
    return request("GET", "/api/v5/asset/balances", params={"ccy": ccy})

def get_trading_balance(ccy: str = "USDT"):
    """交易账户余额（现货/合约共用统一账户）"""
    return request("GET", "/api/v5/account/balance", params={"ccy": ccy})

def get_deposit_address(ccy: str, chain: str = ""):
    """获取充值地址"""
    params = {"ccy": ccy}
    if chain:
        params["chain"] = chain
    return request("GET", "/api/v5/asset/deposit-address", params=params)

def get_deposit_history(ccy: str = "USDT", limit: int = 5):
    """充值历史"""
    return request("GET", "/api/v5/asset/deposit-history",
                   params={"ccy": ccy, "limit": str(limit)})

def transfer(ccy: str, amt: str, from_: str, to: str):
    """
    账户间划转
    from/to: 6=资金账户 18=交易账户
    """
    return request("POST", "/api/v5/asset/transfer", body={
        "ccy": ccy, "amt": amt, "from": from_, "to": to, "type": "0"
    })

def swap_order(inst_id: str, side: str, sz: str,
               ord_type: str = "market", reduce_only: bool = False,
               pos_side: str = "net"):
    """
    永续合约下单
    inst_id: e.g. BTC-USDT-SWAP
    side: buy / sell
    sz: 合约张数（字符串）
    ord_type: market / limit
    pos_side: net（单向持仓）/ long / short（双向）
    """
    body = {
        "instId":    inst_id,
        "tdMode":    "cross",
        "side":      side,
        "ordType":   ord_type,
        "sz":        sz,
        "posSide":   pos_side,
    }
    if reduce_only:
        body["reduceOnly"] = "true"
    return request("POST", "/api/v5/trade/order", body=body)

def get_positions(inst_type: str = "SWAP"):
    """查询持仓"""
    return request("GET", "/api/v5/account/positions",
                   params={"instType": inst_type})

def close_position(inst_id: str, pos_side: str = "net", ccy: str = "USDT"):
    """一键平仓"""
    return request("POST", "/api/v5/trade/close-position", body={
        "instId":  inst_id,
        "mgnMode": "cross",
        "posSide": pos_side,
        "ccy":     ccy,
    })

def get_funding_rate(inst_id: str):
    """获取当前资金费率"""
    return request("GET", "/api/v5/public/funding-rate",
                   params={"instId": inst_id}, signed=False)

def get_tickers(inst_type: str = "SWAP"):
    """获取所有 SWAP 行情（含资金费率）"""
    return request("GET", "/api/v5/market/tickers",
                   params={"instType": inst_type}, signed=False)


if __name__ == "__main__":
    print("=== OKX API 连通性测试 ===\n")

    # 1. 资金账户
    r = get_asset_balances("USDT")
    if r.get("code") == "0":
        bal = r.get("data", [{}])[0]
        print(f"✅ 资金账户正常  USDT 可用: {bal.get('availBal','0')}")
    else:
        print(f"❌ 资金账户: {r.get('msg', r)}")

    # 2. 交易账户
    r2 = get_trading_balance("USDT")
    if r2.get("code") == "0":
        details = r2.get("data", [{}])[0].get("details", [])
        usdt = next((d for d in details if d.get("ccy") == "USDT"), None)
        print(f"✅ 交易账户正常  USDT 权益: {usdt.get('eq','0') if usdt else '0'}")
    else:
        print(f"❌ 交易账户: {r2.get('msg', r2)}")

    # 3. BTC 资金费率（公开接口）
    r3 = get_funding_rate("BTC-USDT-SWAP")
    if r3.get("code") == "0":
        fr = r3.get("data", [{}])[0]
        print(f"✅ 公开接口正常  BTC-USDT-SWAP 资金费率: {fr.get('fundingRate','N/A')}")
    else:
        print(f"❌ 公开接口: {r3.get('msg', r3)}")

    # 4. OKX BSC 充值地址
    r4 = get_deposit_address("USDT", chain="USDT-BSC")
    if r4.get("code") == "0":
        addr = r4.get("data", [{}])[0].get("addr", "N/A")
        print(f"✅ USDT BSC 充值地址: {addr}")
    else:
        print(f"❌ 充值地址查询: {r4.get('msg', r4)}")
