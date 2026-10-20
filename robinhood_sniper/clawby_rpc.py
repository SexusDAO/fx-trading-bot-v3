"""
robinhood_sniper/clawby_rpc.py
Clawby 扩展能力层：
1. RPC — 直连 Robinhood Chain RPC，监听 PairCreated 事件（零延迟）
2. x_search — 推特社交热度评分
3. address_labels — 创建者钱包风险检测（ETH 主网历史）
"""

import time
import logging
import requests
from typing import Optional

from .config import (
    CLAWBY_API_KEY, CLAWBY_BASE_URL, PROXY_URL,
    CHAIN_ID_DS,
)

logger = logging.getLogger("sniper.clawby_rpc")

# Robinhood Chain 原生 RPC（已验证可用，无需 Clawby 代理）
RH_RPC_URL = "https://rpc.mainnet.chain.robinhood.com"

# Pons DEX Factory（Robinhood Chain，从链上 pair 合约 factory() 读取验证）
PONS_FACTORY = "0x1f7d7550b1b028f7571e69a784071f0205fd2efa"
# Uniswap V3 PoolCreated topic（链上实测确认）
PAIR_CREATED_TOPIC = "0x783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118"

_HEADERS_CLAWBY = {
    "X-API-Key": CLAWBY_API_KEY,
    "Content-Type": "application/json",
}
_PROXIES = {"http": PROXY_URL, "https": PROXY_URL}

_rpc_session = requests.Session()
_rpc_session.proxies = _PROXIES


# ─────────────────────────────────────────────────────────────────────────────
# 1. 直连 RPC：获取最新区块 PairCreated 事件
# ─────────────────────────────────────────────────────────────────────────────

def rpc_call(method: str, params: list, timeout: int = 10) -> Optional[dict]:
    """直连 Robinhood Chain RPC"""
    try:
        r = _rpc_session.post(
            RH_RPC_URL,
            json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1},
            timeout=timeout,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.debug(f"[RPC] {method} 失败: {e}")
        return None


def get_latest_block() -> int:
    """获取当前最新区块号"""
    resp = rpc_call("eth_blockNumber", [])
    if resp and "result" in resp:
        return int(resp["result"], 16)
    return 0


def get_new_pairs_from_rpc(from_block: int, to_block: int) -> list[dict]:
    """
    通过 eth_getLogs 监听 PairCreated 事件
    返回新交易对列表，每条含 token0/token1/pair 地址
    """
    if to_block < from_block:
        return []

    # 限制最大扫描范围防止超时
    if to_block - from_block > 50:
        from_block = to_block - 50

    params = [{
        "fromBlock": hex(from_block),
        "toBlock":   hex(to_block),
        "address":   PONS_FACTORY,
        "topics":    [PAIR_CREATED_TOPIC],
    }]
    resp = rpc_call("eth_getLogs", params, timeout=15)
    if not resp or "result" not in resp:
        return []

    pairs = []
    for log in (resp["result"] or []):
        topics = log.get("topics", [])
        data   = log.get("data", "")
        # Uniswap V3 PoolCreated:
        #   topics[1] = token0, topics[2] = token1
        #   data: fee(32) + tickSpacing(32) + pool_address(32)
        if len(topics) < 3:
            continue
        try:
            token0 = "0x" + topics[1][-40:]
            token1 = "0x" + topics[2][-40:]
            # V3 PoolCreated data: tickSpacing(32) + pool_address(32)
            # pool 地址在 data 最后 20 bytes
            pool   = "0x" + data[-40:] if len(data) >= 42 else ""
            pairs.append({
                "token0":    token0.lower(),
                "token1":    token1.lower(),
                "pair":      pool.lower(),
                "blockNumber": int(log.get("blockNumber", "0x0"), 16),
                "txHash":    log.get("transactionHash", ""),
                "source":    "rpc_pool_created",
            })
        except Exception as e:
            logger.debug(f"[RPC] 解析 PoolCreated log 失败: {e}")

    if pairs:
        logger.info(f"[RPC] 区块 {from_block}-{to_block} 发现 {len(pairs)} 个新交易对")
    return pairs


# ─────────────────────────────────────────────────────────────────────────────
# 2. x_search：Twitter 社交热度评分
# ─────────────────────────────────────────────────────────────────────────────

def _clawby_post(name: str, params: dict, timeout: int = 12) -> Optional[dict]:
    try:
        r = requests.post(
            CLAWBY_BASE_URL,
            headers=_HEADERS_CLAWBY,
            json={"name": name, "params": params},
            timeout=timeout,
            proxies=_PROXIES,
        )
        if r.status_code == 429:
            time.sleep(10)
            return None
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.debug(f"[Clawby] {name} 失败: {e}")
        return None


def get_social_score(symbol: str, token_address: str) -> tuple[int, str]:
    """
    搜索 Twitter 上该代币的提及情况，返回 (social_score, summary)
    social_score: 0-3 分
      0 = 无提及
      1 = 有少量提及（<5条）
      2 = 活跃提及（5-20条，互动一般）
      3 = 热门话题（20+条，或高互动）
    """
    query = f"{symbol} robinhood chain"
    resp  = _clawby_post("x_search", {"query": query, "limit": 20})
    if not resp:
        return 0, "Twitter 查询失败"

    posts = resp.get("data", [])
    if not posts:
        return 0, "无 Twitter 提及"

    count      = len(posts)
    total_likes = sum(int(p.get("like_count") or 0) for p in posts)
    total_rt    = sum(int(p.get("retweet_count") or 0) for p in posts)

    # 近24h内的推文数量
    recent_24h = 0
    now_ts = time.time()
    for p in posts:
        try:
            import time as _t
            from email.utils import parsedate_to_datetime
            ts = parsedate_to_datetime(p["created_at"]).timestamp()
            if now_ts - ts < 86400:
                recent_24h += 1
        except Exception:
            recent_24h += 1  # 无法解析时计入

    if recent_24h >= 20 or total_likes > 500:
        score = 3
        summary = f"🔥 Twitter 热门 {recent_24h}条/24h 总赞{total_likes}"
    elif recent_24h >= 5:
        score = 2
        summary = f"📣 Twitter 活跃 {recent_24h}条/24h 转推{total_rt}"
    elif count > 0:
        score = 1
        summary = f"📱 Twitter 少量提及 {count}条"
    else:
        score = 0
        summary = "无 Twitter 热度"

    return score, summary


# ─────────────────────────────────────────────────────────────────────────────
# 3. address_labels：创建者钱包风险检测
# ─────────────────────────────────────────────────────────────────────────────

def check_creator_risk(creator_address: str) -> tuple[bool, str]:
    """
    检测创建者钱包是否有已知风险标签（ETH 主网历史）
    返回 (is_safe, reason)
    """
    resp = _clawby_post("address_labels", {
        "coin":    "ETH",
        "address": creator_address,
    })

    if not resp or "error" in resp:
        # API 不支持或查不到 → 降级处理，不阻断
        return True, "⚠️ 创建者钱包无历史记录（新地址）"

    data   = resp.get("data", {})
    labels = data.get("label_list", []) if isinstance(data, dict) else []

    # 高危标签
    danger_labels = {"scam", "phishing", "hack", "rug", "exploit",
                     "mixer", "tornado", "sanctioned", "fraud"}
    found_danger  = [l for l in labels if any(d in str(l).lower() for d in danger_labels)]

    if found_danger:
        return False, f"❌ 创建者钱包有高危标签: {found_danger}"

    label_str = ", ".join(str(l) for l in labels[:3]) if labels else "无标签"
    return True, f"✅ 创建者钱包: {label_str}"
