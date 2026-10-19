"""
robinhood_sniper/smart_money.py
聪明钱共振监听层 v1.0

功能：
1. 实时监听 RH 链上所有 Swap 事件
2. 统计指定时间窗口内，每个 pair 被多少个不同地址买入
3. 当某个新代币出现 N 个不同买家（共振），触发加分信号
4. 可选：维护一个「已知优质地址」白名单（从 FOMO KOL 等收集）

用途：在 filter.py 评分基础上叠加「聪明钱共振」加分。
买入决策 = 评分 >= MIN_SCORE  AND  (聪明钱共振 >= 1 OR 纯评分 >= 10)
"""

import time
import logging
import requests
import threading
from typing import Optional

logger = logging.getLogger("sniper.smart_money")

RH_RPC = "https://rpc.mainnet.chain.robinhood.com"
SWAP_TOPIC = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"

# ── 已知优质钱包白名单（来自 @oragnes FOMO 排行榜 + 公开地址）────────────
# 注：FOMO 账户地址未全部公开，这里收录了能确认的链上地址
# 持续更新：当发现新优质地址（通过链上溯源），加入此列表
SMART_WALLETS: dict[str, str] = {
    # 地址: 来源/备注
    # 以下为从 Twitter bio 公开信息收集到的地址（ETH主网，需验证是否同地址用于 RH 链）
    # "0x07f5b6823751c2e2cd4560f28af75ff887102241": "bluntz_capital",
    # "0x232ce3bd40fcd6f80f3d55a522d03f25df784ee2": "econoar",
    # "0x56ab53b77f07da3af732150e8aec4783eb5bba7d": "ether_monk",
    # 注：上述地址在 RH 链上无活动，可能使用不同地址
    # 通过链上行为识别优质地址（大量盈利、早期买入、持有时间长）
}

# ── 内存缓存：pair → 近期买入地址集合 ─────────────────────────────────────
_pair_buyers: dict[str, dict] = {}  # {pair: {addr: ts, ...}}
_pair_to_token: dict[str, str] = {}  # {pair_addr: token_addr}
_lock = threading.Lock()

# ── 参数 ────────────────────────────────────────────────────────────────────
WINDOW_SEC      = 10 * 60   # 10 分钟窗口内的共振
MIN_RESONANCE   = 2         # 至少 N 个不同钱包买入 → 共振
SMART_BONUS     = 2         # 有已知优质钱包买入时额外加分
RESONANCE_BONUS = 1         # 普通共振（N 个不同钱包）加分
MAX_PAIRS_CACHE = 500       # 缓存上限，防止内存膨胀

_PROXIES = {
    "http":  "socks5://REDACTED_PROXY_CREDENTIALS",
    "https": "socks5://REDACTED_PROXY_CREDENTIALS",
}
_session = requests.Session()
_session.proxies = _PROXIES


# ─────────────────────────────────────────────────────────────────────────────
def _rpc(method, params, timeout=10):
    try:
        r = _session.post(RH_RPC,
            json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1},
            timeout=timeout)
        return r.json()
    except Exception as e:
        logger.debug(f"[SM] RPC {method} 失败: {e}")
        return None


def _rpc_batch(requests_list, timeout=15):
    try:
        r = _session.post(RH_RPC, json=requests_list, timeout=timeout)
        return r.json() if isinstance(r.json(), list) else []
    except Exception as e:
        logger.debug(f"[SM] RPC batch 失败: {e}")
        return []


# ─────────────────────────────────────────────────────────────────────────────
def ingest_blocks(from_block: int, to_block: int):
    """
    扫描 from_block ~ to_block 的 Swap 事件，更新 _pair_buyers 缓存。
    由主信号循环定期调用（每次 RPC 轮询后调用）。
    """
    if to_block < from_block or to_block - from_block > 100:
        return

    resp = _rpc("eth_getLogs", [{
        "fromBlock": hex(from_block),
        "toBlock":   hex(to_block),
        "topics":    [SWAP_TOPIC],
    }], timeout=12)

    if not resp or "result" not in resp:
        return

    logs = resp["result"] or []
    if not logs:
        return

    # 批量查 tx，获取 from 地址
    tx_hashes = list({l["transactionHash"] for l in logs})
    batch = [{"jsonrpc": "2.0", "method": "eth_getTransactionByHash",
              "params": [h], "id": i + 1}
             for i, h in enumerate(tx_hashes[:60])]
    results = _rpc_batch(batch, timeout=15)

    tx_map = {}
    for res in results:
        tx = (res or {}).get("result") or {}
        if tx:
            tx_map[tx["hash"].lower()] = tx

    now = time.time()
    # 传给 on_chain_data 做大额买入识别
    logs_with_tx = []
    for log in logs:
        txh = log["transactionHash"].lower()
        tx  = tx_map.get(txh, {})
        if tx:
            logs_with_tx.append({"log": log, "tx": tx})
    if logs_with_tx:
        try:
            from robinhood_sniper.on_chain_data import ingest_big_buys, ETH_PRICE
            ingest_big_buys(logs_with_tx, ETH_PRICE)
        except Exception:
            pass

    with _lock:
        for log in logs:
            pair   = log["address"].lower()
            txh    = log["transactionHash"].lower()
            tx     = tx_map.get(txh, {})
            trader = tx.get("from", "").lower()
            if not trader:
                continue

            if pair not in _pair_buyers:
                if len(_pair_buyers) >= MAX_PAIRS_CACHE:
                    # 清掉最旧的
                    oldest = min(_pair_buyers,
                                 key=lambda p: max(_pair_buyers[p].values(), default=0))
                    del _pair_buyers[oldest]
                _pair_buyers[pair] = {}

            _pair_buyers[pair][trader] = now

        # 清理过期记录
        cutoff = now - WINDOW_SEC
        for pair in list(_pair_buyers):
            _pair_buyers[pair] = {a: t for a, t in _pair_buyers[pair].items() if t > cutoff}
            if not _pair_buyers[pair]:
                del _pair_buyers[pair]

    logger.debug(f"[SM] ingest {from_block}-{to_block}: {len(logs)} swaps, "
                 f"cache={len(_pair_buyers)} pairs")


def get_signal(pair_address: str) -> tuple[int, str]:
    """
    查询某个交易对的聪明钱信号。
    返回 (bonus_score, description)
    bonus_score = 0 → 无信号
    bonus_score = 1 → 普通共振（≥2 不同地址买入）
    bonus_score = 2 → 有已知优质钱包买入
    bonus_score = 3 → 优质钱包 + 多人共振
    """
    pair = pair_address.lower()
    now = time.time()
    cutoff = now - WINDOW_SEC

    with _lock:
        buyers = {a: t for a, t in _pair_buyers.get(pair, {}).items() if t > cutoff}

    if not buyers:
        return 0, "📊 无聪明钱数据"

    n_buyers = len(buyers)
    smart_hits = [a for a in buyers if a in SMART_WALLETS]

    if smart_hits and n_buyers >= MIN_RESONANCE:
        names = [SMART_WALLETS[a] for a in smart_hits]
        return SMART_BONUS + RESONANCE_BONUS, (
            f"🧠 聪明钱共振! {n_buyers}个钱包买入, "
            f"已知优质: {', '.join(names[:2])}"
        )
    elif smart_hits:
        names = [SMART_WALLETS[a] for a in smart_hits]
        return SMART_BONUS, f"🧠 已知优质钱包买入: {', '.join(names[:2])}"
    elif n_buyers >= MIN_RESONANCE:
        return RESONANCE_BONUS, f"👥 多地址共振: {n_buyers}个不同钱包买入"

    return 0, f"👤 {n_buyers}个地址买入（未达共振阈值{MIN_RESONANCE}）"


def register_pair(pair_address: str, token_address: str):
    """注册 pair → token 映射"""
    with _lock:
        _pair_to_token[pair_address.lower()] = token_address.lower()


def add_smart_wallet(address: str, label: str):
    """动态添加已知优质钱包（运行时可调用）"""
    SMART_WALLETS[address.lower()] = label
    logger.info(f"[SM] 添加聪明钱: {address[:10]}... ({label})")


def get_stats() -> dict:
    """返回当前缓存统计"""
    with _lock:
        return {
            "pairs_tracked": len(_pair_buyers),
            "smart_wallets": len(SMART_WALLETS),
            "total_buyers": sum(len(v) for v in _pair_buyers.values()),
        }
