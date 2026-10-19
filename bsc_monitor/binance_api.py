# ============================================================
# binance_api.py — Binance Web3 官方 API 封装
#
# 数据源（全部来自 Binance 官方，无需代理）：
#   1. Token Metadata    → 代币介绍 (description, links, creator)
#   2. Token Dynamic     → 实时行情 (price, mc, liq, holders, kol, smart money)
#   3. Token Audit       → 安全审计 (honeypot, tax, blacklist, risk level)
#   4. Smart Money Signal → 聪明钱信号 (buy/sell, trigger price, max gain, exit rate)
#   5. Meme Rush         → 新币列表 (Pulse launchpad, new/finalizing/migrated)
#   6. Smart Money Inflow → 聪明钱流入排名 (token inflow rank)
#   7. Social Hype       → 社交热度排名 (sentiment, social summary)
#
# 缓存策略：
#   - Metadata: 1h（静态数据）
#   - Dynamic: 30s（行情数据）
#   - Audit: 15min（安全数据）
#   - Signal/Rank: 60s（排名数据）
# ============================================================
import json
import logging
import threading
import time
import uuid
from typing import Optional

import requests

log = logging.getLogger("binance_api")

_BASE_HEADERS = {
    "Accept-Encoding": "identity",
    "User-Agent": "binance-web3/2.1 (Skill)",
}

# ── 缓存 ──
_cache_lock = threading.Lock()
_meta_cache: dict = {}       # addr → (ts, data)
_dynamic_cache: dict = {}    # addr → (ts, data)
_audit_cache: dict = {}      # addr → (ts, data)
_signal_cache: dict = {}     # "bsc" → (ts, data)
_inflow_cache: dict = {}     # "bsc_1h" → (ts, data)
_hype_cache: dict = {}       # "bsc" → (ts, data)
_meme_cache: dict = {}       # "bsc_30" → (ts, data)

# TTL
_META_TTL = 3600
_DYNAMIC_TTL = 30
_AUDIT_TTL = 900
_SIGNAL_TTL = 60
_INFLOW_TTL = 60
_HYPE_TTL = 120
_MEME_TTL = 60

_SESSION = requests.Session()
_SESSION.headers.update(_BASE_HEADERS)


# ═══════════════════════════════════════════════════════════════
# 1. Token Metadata — 代币介绍
# ═══════════════════════════════════════════════════════════════
def get_token_metadata(chain_id: str, contract_address: str) -> Optional[dict]:
    """获取代币元数据：name, symbol, description, links, creator, createTime"""
    addr = contract_address.lower()
    now = time.time()
    
    with _cache_lock:
        if addr in _meta_cache and now - _meta_cache[addr][0] < _META_TTL:
            return _meta_cache[addr][1]
    
    try:
        r = _SESSION.get(
            "https://web3.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/dex/market/token/meta/info/ai",
            params={"chainId": chain_id, "contractAddress": addr},
            timeout=10
        )
        data = r.json().get("data")
        if data:
            with _cache_lock:
                _meta_cache[addr] = (now, data)
            return data
    except Exception as e:
        log.debug(f"[BINANCE] metadata {addr[:10]} err: {e}")
    return None


def get_token_description(chain_id: str, contract_address: str) -> str:
    """获取代币简介（优先 description，其次 twitter）"""
    meta = get_token_metadata(chain_id, contract_address)
    if not meta:
        return ""
    
    desc = meta.get("description", "") or ""
    
    # 如果 description 太长，截取前 100 字符
    if len(desc) > 100:
        desc = desc[:100] + "..."
    
    # 如果没有 description，尝试从 links 获取 twitter
    if not desc:
        links = meta.get("links") or []
        for link in links:
            if link.get("label") == "x":
                twitter_url = link.get("link", "")
                if twitter_url:
                    # 提取 twitter username
                    username = twitter_url.rstrip("/").split("/")[-1]
                    if username:
                        return f"Twitter: @{username}"
    
    return desc


# ═══════════════════════════════════════════════════════════════
# 2. Token Dynamic Data — 实时行情（补充 DexScreener）
# ═══════════════════════════════════════════════════════════════
def get_token_dynamic(chain_id: str, contract_address: str) -> Optional[dict]:
    """获取代币动态数据：price, mc, liq, volume, holders, kol, smart money 等"""
    addr = contract_address.lower()
    now = time.time()
    
    with _cache_lock:
        if addr in _dynamic_cache and now - _dynamic_cache[addr][0] < _DYNAMIC_TTL:
            return _dynamic_cache[addr][1]
    
    try:
        r = _SESSION.get(
            "https://web3.binance.com/bapi/defi/v4/public/wallet-direct/buw/wallet/market/token/dynamic/info/ai",
            params={"chainId": chain_id, "contractAddress": addr},
            timeout=10
        )
        data = r.json().get("data")
        if data:
            # 转换字符串为数字
            result = {
                "price": float(data.get("price", 0) or 0),
                "marketCap": float(data.get("marketCap", 0) or 0),
                "liquidity": float(data.get("liquidity", 0) or 0),
                "volume24h": float(data.get("volume24h", 0) or 0),
                "volume1h": float(data.get("volume1h", 0) or 0),
                "volume5m": float(data.get("volume5m", 0) or 0),
                "volume24hBuy": float(data.get("volume24hBuy", 0) or 0),
                "volume24hSell": float(data.get("volume24hSell", 0) or 0),
                "percentChange5m": float(data.get("percentChange5m", 0) or 0),
                "percentChange1h": float(data.get("percentChange1h", 0) or 0),
                "percentChange24h": float(data.get("percentChange24h", 0) or 0),
                "holders": int(float(data.get("holders", 0) or 0)),
                "kolHolders": int(float(data.get("kolHolders", 0) or 0)),
                "kolHoldingPercent": float(data.get("kolHoldingPercent", 0) or 0),
                "smartMoneyHolders": int(float(data.get("smartMoneyHolders", 0) or 0)),
                "smartMoneyHoldingPercent": float(data.get("smartMoneyHoldingPercent", 0) or 0),
                "proHolders": int(float(data.get("proHolders", 0) or 0)),
                "top10HoldersPercentage": float(data.get("top10HoldersPercentage", 0) or 0),
                "count24h": int(float(data.get("count24h", 0) or 0)),
                "count24hBuy": int(float(data.get("count24hBuy", 0) or 0)),
                "count24hSell": int(float(data.get("count24hSell", 0) or 0)),
            }
            with _cache_lock:
                _dynamic_cache[addr] = (now, result)
            return result
    except Exception as e:
        log.debug(f"[BINANCE] dynamic {addr[:10]} err: {e}")
    return None


# ═══════════════════════════════════════════════════════════════
# 3. Token Audit — 安全审计（替代/补充 GoPlus）
# ═══════════════════════════════════════════════════════════════
def get_token_audit(chain_id: str, contract_address: str) -> Optional[dict]:
    """获取代币安全审计：riskLevel, honeypot, tax, blacklist 等"""
    addr = contract_address.lower()
    now = time.time()
    
    with _cache_lock:
        if addr in _audit_cache and now - _audit_cache[addr][0] < _AUDIT_TTL:
            return _audit_cache[addr][1]
    
    try:
        r = _SESSION.post(
            "https://web3.binance.com/bapi/defi/v1/public/wallet-direct/security/token/audit",
            json={
                "binanceChainId": chain_id,
                "contractAddress": addr,
                "requestId": str(uuid.uuid4()),
            },
            headers={"Content-Type": "application/json"},
            timeout=10
        )
        data = r.json().get("data")
        if data and data.get("hasResult") and data.get("isSupported"):
            result = {
                "riskLevel": data.get("riskLevel", -1),
                "riskLevelEnum": data.get("riskLevelEnum", "UNKNOWN"),
                "buyTax": float(data.get("extraInfo", {}).get("buyTax", 0) or 0),
                "sellTax": float(data.get("extraInfo", {}).get("sellTax", 0) or 0),
                "isVerified": data.get("extraInfo", {}).get("isVerified", False),
                "risks": [],
            }
            
            # 提取风险项
            for item in data.get("riskItems", []):
                for detail in item.get("details", []):
                    if detail.get("isHit"):
                        risk_type = detail.get("riskType", "CAUTION")
                        title = detail.get("title", "")
                        result["risks"].append({
                            "type": risk_type,
                            "title": title,
                            "category": item.get("id", ""),
                        })
            
            with _cache_lock:
                _audit_cache[addr] = (now, result)
            return result
    except Exception as e:
        log.debug(f"[BINANCE] audit {addr[:10]} err: {e}")
    return None


def audit_to_safety_score(audit: Optional[dict]) -> dict:
    """将 Binance Audit 转换为与 GoPlus 兼容的安全评分格式"""
    if not audit:
        return {"score": None, "label": "unknown", "issues": []}
    
    risk_level = audit.get("riskLevel", -1)
    risks = audit.get("risks", [])
    
    # 转换风险等级为分数
    if risk_level <= 1:
        score = 85  # LOW
    elif risk_level <= 3:
        score = 60  # MID
    elif risk_level == 4:
        score = 30  # HIGH
    else:
        score = 0   # BLOCK
    
    # 检查蜜罐（在 risks 中查找）
    is_honeypot = any("honeypot" in r.get("title", "").lower() for r in risks)
    if is_honeypot:
        score = 0
    
    # 检查税率
    buy_tax = audit.get("buyTax", 0)
    sell_tax = audit.get("sellTax", 0)
    if buy_tax > 10 or sell_tax > 10:
        score = min(score, 20)
    elif buy_tax > 5 or sell_tax > 5:
        score = min(score, 50)
    
    # 提取问题列表
    issues = []
    for r in risks[:5]:
        title = r.get("title", "")
        # 简化标题
        if "Honeypot" in title:
            issues.append("蜜罐")
        elif "Blacklist" in title:
            issues.append("黑名单")
        elif "Mintable" in title:
            issues.append("可增发")
        elif "Owner" in title or "owner" in title:
            issues.append("owner权限")
        elif "Proxy" in title:
            issues.append("代理合约")
        else:
            issues.append(title[:10])
    
    return {
        "score": score,
        "label": audit.get("riskLevelEnum", "UNKNOWN"),
        "issues": issues,
        "buyTax": buy_tax,
        "sellTax": sell_tax,
        "source": "binance",
    }


# ═══════════════════════════════════════════════════════════════
# 4. Smart Money Signal — 聪明钱信号
# ═══════════════════════════════════════════════════════════════
def get_smart_money_signals(chain_id: str, page: int = 1, page_size: int = 50) -> list:
    """获取聪明钱交易信号列表"""
    cache_key = f"{chain_id}_{page}"
    now = time.time()
    
    with _cache_lock:
        if cache_key in _signal_cache and now - _signal_cache[cache_key][0] < _SIGNAL_TTL:
            return _signal_cache[cache_key][1]
    
    try:
        r = _SESSION.post(
            "https://web3.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/web/signal/smart-money/ai",
            json={
                "smartSignalType": "",
                "page": page,
                "pageSize": page_size,
                "chainId": chain_id,
            },
            headers={"Content-Type": "application/json"},
            timeout=10
        )
        data = r.json().get("data", [])
        
        # 解析信号
        signals = []
        for s in data:
            signal = {
                "signalId": s.get("signalId"),
                "ticker": s.get("ticker", ""),
                "contractAddress": s.get("contractAddress", ""),
                "direction": s.get("direction", ""),  # buy/sell
                "smartMoneyCount": s.get("smartMoneyCount", 0),
                "totalValue": float(s.get("totalTokenValue", 0) or 0),
                "alertPrice": float(s.get("alertPrice", 0) or 0),
                "alertMarketCap": float(s.get("alertMarketCap", 0) or 0),
                "currentPrice": float(s.get("currentPrice", 0) or 0),
                "currentMarketCap": float(s.get("currentMarketCap", 0) or 0),
                "highestPrice": float(s.get("highestPrice", 0) or 0),
                "maxGain": float(s.get("maxGain", 0) or 0),
                "exitRate": s.get("exitRate", 0),
                "status": s.get("status", ""),
                "triggerTime": s.get("signalTriggerTime", 0),
            }
            signals.append(signal)
        
        with _cache_lock:
            _signal_cache[cache_key] = (now, signals)
        return signals
    except Exception as e:
        log.debug(f"[BINANCE] signals err: {e}")
    return []


# ═══════════════════════════════════════════════════════════════
# 5. Meme Rush — 新币列表（Pulse launchpad）
# ═══════════════════════════════════════════════════════════════
def get_meme_rush(chain_id: str, rank_type: int = 30, limit: int = 50) -> list:
    """获取 Meme 代币列表
    rank_type: 10=New, 20=Finalizing, 30=Migrated
    """
    cache_key = f"{chain_id}_{rank_type}"
    now = time.time()
    
    with _cache_lock:
        if cache_key in _meme_cache and now - _meme_cache[cache_key][0] < _MEME_TTL:
            return _meme_cache[cache_key][1]
    
    try:
        r = _SESSION.post(
            "https://web3.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/market/token/pulse/rank/list/ai",
            json={
                "chainId": chain_id,
                "rankType": rank_type,
                "limit": limit,
            },
            headers={"Content-Type": "application/json"},
            timeout=10
        )
        data = r.json().get("data", [])
        
        tokens = []
        for t in data:
            token = {
                "symbol": t.get("symbol", ""),
                "name": t.get("name", ""),
                "contractAddress": t.get("contractAddress", ""),
                "price": float(t.get("price", 0) or 0),
                "marketCap": float(t.get("marketCap", 0) or 0),
                "liquidity": float(t.get("liquidity", 0) or 0),
                "volume": float(t.get("volume", 0) or 0),
                "holders": t.get("holders", 0),
                "kolHolders": t.get("kolHolders", 0),
                "createTime": t.get("createTime", 0),
                "migrateTime": t.get("migrateTime", 0),
                "progress": t.get("progress", ""),
                "devAddress": t.get("devAddress", ""),
                "devMigrateCount": t.get("devMigrateCount", 0),
                "devSellPercent": t.get("devSellPercent", ""),
                "holdersTop10Percent": t.get("holdersTop10Percent", ""),
                "tagDevBurnedToken": t.get("tagDevBurnedToken", 0),
                "tagDevWashTrading": t.get("tagDevWashTrading", 0),
            }
            tokens.append(token)
        
        with _cache_lock:
            _meme_cache[cache_key] = (now, tokens)
        return tokens
    except Exception as e:
        log.debug(f"[BINANCE] meme rush err: {e}")
    return []


# ═══════════════════════════════════════════════════════════════
# 6. Smart Money Inflow Rank — 聪明钱流入排名
# ═══════════════════════════════════════════════════════════════
def get_smart_money_inflow(chain_id: str, period: str = "1h") -> list:
    """获取聪明钱流入排名
    period: 5m, 1h, 4h, 24h
    """
    cache_key = f"{chain_id}_{period}"
    now = time.time()
    
    with _cache_lock:
        if cache_key in _inflow_cache and now - _inflow_cache[cache_key][0] < _INFLOW_TTL:
            return _inflow_cache[cache_key][1]
    
    try:
        r = _SESSION.post(
            "https://web3.binance.com/bapi/defi/v1/public/wallet-direct/tracker/wallet/token/inflow/rank/query/ai",
            json={
                "chainId": chain_id,
                "period": period,
                "tagType": 2,
            },
            headers={"Content-Type": "application/json"},
            timeout=10
        )
        data = r.json().get("data", [])
        
        tokens = []
        for t in data:
            token = {
                "tokenName": t.get("tokenName", ""),
                "contractAddress": t.get("ca", ""),
                "price": float(t.get("price", 0) or 0),
                "marketCap": float(t.get("marketCap", 0) or 0),
                "liquidity": float(t.get("liquidity", 0) or 0),
                "volume": float(t.get("volume", 0) or 0),
                "priceChangeRate": float(t.get("priceChangeRate", 0) or 0),
                "holders": int(float(t.get("holders", 0) or 0)),
                "inflow": t.get("inflow", 0),
                "traders": t.get("traders", 0),
                "tokenRiskLevel": t.get("tokenRiskLevel", -1),
            }
            tokens.append(token)
        
        with _cache_lock:
            _inflow_cache[cache_key] = (now, tokens)
        return tokens
    except Exception as e:
        log.debug(f"[BINANCE] inflow rank err: {e}")
    return []


# ═══════════════════════════════════════════════════════════════
# 7. Social Hype — 社交热度排名
# ═══════════════════════════════════════════════════════════════
def get_social_hype(chain_id: str, sentiment: str = "All", target_language: str = "en") -> list:
    """获取社交热度排名
    sentiment: All, Positive, Negative, Neutral
    """
    cache_key = f"{chain_id}_{sentiment}"
    now = time.time()
    
    with _cache_lock:
        if cache_key in _hype_cache and now - _hype_cache[cache_key][0] < _HYPE_TTL:
            return _hype_cache[cache_key][1]
    
    try:
        r = _SESSION.get(
            "https://web3.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/market/token/pulse/social/hype/rank/leaderboard/ai",
            params={
                "chainId": chain_id,
                "sentiment": sentiment,
                "socialLanguage": "ALL",
                "targetLanguage": target_language,
                "timeRange": 1,
            },
            timeout=10
        )
        data = r.json().get("data", {}).get("leaderBoardList", [])
        
        tokens = []
        for t in data:
            meta = t.get("metaInfo", {})
            social = t.get("socialHypeInfo", {})
            market = t.get("marketInfo", {})
            token = {
                "symbol": meta.get("symbol", ""),
                "contractAddress": meta.get("contractAddress", ""),
                "marketCap": market.get("marketCap", 0),
                "priceChange": market.get("priceChange", 0),
                "socialHype": social.get("socialHype", 0),
                "sentiment": social.get("sentiment", ""),
                "socialSummary": social.get("socialSummaryBriefTranslated", "") or social.get("socialSummaryBrief", ""),
            }
            tokens.append(token)
        
        with _cache_lock:
            _hype_cache[cache_key] = (now, tokens)
        return tokens
    except Exception as e:
        log.debug(f"[BINANCE] social hype err: {e}")
    return []


# ═══════════════════════════════════════════════════════════════
# 便捷函数
# ═══════════════════════════════════════════════════════════════
def get_token_full_info(chain_id: str, contract_address: str) -> dict:
    """获取代币完整信息（metadata + dynamic + audit）"""
    addr = contract_address.lower()
    result = {
        "address": addr,
        "chain_id": chain_id,
    }
    
    # Metadata
    meta = get_token_metadata(chain_id, addr)
    if meta:
        result["name"] = meta.get("name", "")
        result["symbol"] = meta.get("symbol", "")
        result["description"] = meta.get("description", "")
        result["links"] = meta.get("links", [])
        result["creator"] = meta.get("creatorAddress", "")
        result["createTime"] = meta.get("createTime", 0)
    
    # Dynamic
    dynamic = get_token_dynamic(chain_id, addr)
    if dynamic:
        result.update(dynamic)
    
    # Audit
    audit = get_token_audit(chain_id, addr)
    if audit:
        result["audit"] = audit
        result["safety"] = audit_to_safety_score(audit)
    
    return result


# 测试
if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    
    # 测试 USDT
    print("=== Token Metadata ===")
    meta = get_token_metadata("56", "0x55d398326f99059ff775485246999027b3197955")
    if meta:
        print(f"Name: {meta.get('name')}")
        print(f"Description: {meta.get('description', '')[:100]}...")
    
    print("\n=== Token Dynamic ===")
    dynamic = get_token_dynamic("56", "0x55d398326f99059ff775485246999027b3197955")
    if dynamic:
        print(f"Price: ${dynamic.get('price')}")
        print(f"MC: ${dynamic.get('marketCap'):,.0f}")
        print(f"Holders: {dynamic.get('holders')}")
        print(f"KOL: {dynamic.get('kolHolders')}")
    
    print("\n=== Token Audit ===")
    audit = get_token_audit("56", "0x55d398326f99059ff775485246999027b3197955")
    if audit:
        print(f"Risk Level: {audit.get('riskLevelEnum')}")
        print(f"Buy Tax: {audit.get('buyTax')}%")
        print(f"Sell Tax: {audit.get('sellTax')}%")
    
    print("\n=== Smart Money Signals ===")
    signals = get_smart_money_signals("56", page_size=5)
    for s in signals[:3]:
        print(f"{s['ticker']:10} | {s['direction']:4} | ${s['totalValue']:,.0f}")
    
    print("\n=== Meme Rush (Migrated) ===")
    memes = get_meme_rush("56", rank_type=30, limit=5)
    for t in memes[:3]:
        print(f"{t['symbol']:10} | MC=${t['marketCap']:,.0f} | Holders={t['holders']}")
    
    print("\n=== Smart Money Inflow ===")
    inflow = get_smart_money_inflow("56", period="1h")
    for t in inflow[:3]:
        print(f"{t['tokenName']:15} | Inflow=${t['inflow']:,.0f} | Traders={t['traders']}")
