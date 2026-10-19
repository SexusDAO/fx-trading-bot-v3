"""
Security Checker — GoPlus + DexScreener 交叉验证
===================================================

GoPlus: 合约权限/税率/貔貅/持仓
DexScreener: 市值/流动性/官网交叉验证
"""
import logging

logger = logging.getLogger("arc.security")

GOPLUS_API = "https://api.gopluslabs.io/api/v1"
DEXSCREENER_API = "https://api.dexscreener.com"


def goplus_check(token_address: str, chain_id: int = 5042) -> dict:
    """GoPlus 合约安全检测"""
    import requests
    url = f"{GOPLUS_API}/token_security/{chain_id}"
    try:
        resp = requests.get(url, params={"contract_addresses": token_address}, timeout=10)
        if resp.status_code != 200:
            return _empty_goplus()
        data = resp.json()
        if data.get("code") != 1:
            return _empty_goplus()
        td = data.get("result", {}).get(token_address.lower(), {})
        if not td:
            return _empty_goplus()
        return _parse_goplus(td)
    except Exception as e:
        logger.debug(f"[Security] GoPlus: {e}")
        return _empty_goplus()


def _empty_goplus():
    return {"is_honeypot": False, "buy_tax": 0.0, "sell_tax": 0.0,
            "is_mintable": False, "is_proxy": False, "transfer_pausable": False,
            "owner_changeable": False, "hidden_owner": False, "cannot_sell_all": False,
            "top10_rate": 0.0, "creator_rate": 0.0, "lp_holders": 0,
            "is_open_source": False, "is_renounced": False, "available": False}


def _parse_goplus(d):
    def b(v): return str(v).lower() in ("1", "true", "yes") if v else False
    def f(v):
        try: return float(v) if v else 0.0
        except: return 0.0
    return {
        "is_honeypot": b(d.get("is_honeypot")),
        "buy_tax": f(d.get("buy_tax")) / 100, "sell_tax": f(d.get("sell_tax")) / 100,
        "is_mintable": b(d.get("is_mintable")), "is_proxy": b(d.get("is_proxy")),
        "transfer_pausable": b(d.get("transfer_pausable")),
        "owner_changeable": b(d.get("owner_changeable")),
        "hidden_owner": b(d.get("hidden_owner")),
        "cannot_sell_all": b(d.get("cannot_sell_all")),
        "top10_rate": f(d.get("top10_rate")) / 100 if d.get("top10_rate") else 0.0,
        "creator_rate": f(d.get("creator_rate")) / 100 if d.get("creator_rate") else 0.0,
        "lp_holders": int(f(d.get("lp_holder_count"))),
        "is_open_source": b(d.get("is_open_source")),
        "is_renounced": b(d.get("is_renounced")), "available": True,
    }


def dexscreener_check(token_address: str, chain_id: int = 5042) -> dict:
    """DexScreener 市值/流动性/官网交叉验证"""
    import requests
    try:
        url = f"{DEXSCREENER_API}/tokens/v1/{token_address}"
        resp = requests.get(url, timeout=10)
        if resp.status_code != 200:
            return _empty_dex()
        data = resp.json()
        if not data or not isinstance(data, list) or len(data) == 0:
            return _empty_dex()
        pair = data[0]
        total_liq = sum(float(p.get("liquidity", {}).get("usd", 0)) for p in data)
        total_vol = sum(float(p.get("volume", {}).get("h24", 0)) for p in data)
        website = twitter = telegram = ""
        for link in pair.get("links", []):
            lt = link.get("type", "").lower()
            if lt == "website": website = link.get("url", "")
            elif lt == "twitter": twitter = link.get("url", "")
            elif lt == "telegram": telegram = link.get("url", "")
        return {
            "price": float(pair.get("priceUsd", 0)),
            "market_cap": float(pair.get("fdv", 0)),
            "liquidity_usd": total_liq, "volume_24h": total_vol,
            "pairs": len(data), "website": website, "twitter": twitter,
            "telegram": telegram, "dex": pair.get("dexId", ""),
            "pair_address": pair.get("pairAddress", ""), "available": True,
        }
    except Exception as e:
        logger.debug(f"[Security] DexScreener: {e}")
        return _empty_dex()


def _empty_dex():
    return {"price": 0.0, "market_cap": 0.0, "liquidity_usd": 0.0, "volume_24h": 0.0,
            "pairs": 0, "website": "", "twitter": "", "telegram": "", "dex": "",
            "pair_address": "", "available": False}


def comprehensive_security_check(token_address: str, chain_id: int = 5042) -> dict:
    """综合安全检测：GoPlus + DexScreener，返回安全分和风险标记。"""
    gp = goplus_check(token_address, chain_id)
    dx = dexscreener_check(token_address, chain_id)
    score = 100
    flags = []
    if gp["available"]:
        if gp["is_honeypot"]: score = 0; flags.append("❌ 蜜罐")
        if gp["cannot_sell_all"]: score = min(score, 10); flags.append("❌ 不能全卖")
        if gp["buy_tax"] > 0.25: score -= 30; flags.append(f"⚠️ 买入税{gp['buy_tax']:.0%}")
        if gp["sell_tax"] > 0.25: score -= 30; flags.append(f"⚠️ 卖出税{gp['sell_tax']:.0%}")
        if gp["is_mintable"]: score -= 20; flags.append("⚠️ 可铸币")
        if gp["is_proxy"]: score -= 25; flags.append("⚠️ 代理合约")
        if gp["transfer_pausable"]: score -= 15; flags.append("⚠️ 可暂停")
        if gp["owner_changeable"]: score -= 15; flags.append("⚠️ owner可变")
        if gp["hidden_owner"]: score -= 20; flags.append("⚠️ 隐藏owner")
        if gp["top10_rate"] > 0.5: score -= 20; flags.append(f"⚠️ Top10 {gp['top10_rate']:.0%}")
        if gp["creator_rate"] > 0.3: score -= 15; flags.append(f"⚠️ 创建者{gp['creator_rate']:.0%}")
        if gp["is_open_source"]: score += 5
        if gp["is_renounced"]: score += 10
    if dx["available"]:
        if dx["liquidity_usd"] < 1000: score -= 20; flags.append(f"⚠️ 流动性${dx['liquidity_usd']:,.0f}")
        if 0 < dx["market_cap"] < 5000: score -= 10; flags.append(f"⚠️ 市值${dx['market_cap']:,.0f}")
    score = max(0, min(100, score))
    return {"goplus": gp, "dexscreener": dx, "security_score": score,
            "flags": flags, "is_safe": score >= 50 and not gp["is_honeypot"]}
