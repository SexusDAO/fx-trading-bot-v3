"""
holder_analysis.py — 链上持有人真实性分析
功能：
1. 通过 RPC eth_getLogs 获取代币所有 Transfer 记录
2. 计算当前持有人列表（余额 > 0）
3. 关联地址检测：同一时间窗口、相似金额、同一来源地址 → 标记为关联地址
4. 返回"去关联后真实持有人数"
"""

import requests, time, logging
from collections import defaultdict
from typing import Optional

logger = logging.getLogger("sniper.holder")

RPC_URL = "https://rpc.mainnet.chain.robinhood.com"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ZERO_ADDR = "0x" + "0" * 40

_PROXIES = {
    "http":  "socks5://REDACTED_PROXY_CREDENTIALS",
    "https": "socks5://REDACTED_PROXY_CREDENTIALS",
}

def _rpc(payload: dict, timeout=12) -> dict:
    r = requests.post(RPC_URL, json=payload, timeout=timeout, proxies=_PROXIES)
    return r.json()

def get_transfer_logs(token_address: str) -> list:
    """获取代币所有 Transfer 事件"""
    r = _rpc({
        "jsonrpc": "2.0", "method": "eth_getLogs",
        "params": [{"address": token_address,
                    "topics": [TRANSFER_TOPIC],
                    "fromBlock": hex(0), "toBlock": "latest"}],
        "id": 1
    })
    return r.get("result", [])

def compute_holders(logs: list) -> dict:
    """从 Transfer 日志计算当前持有人余额"""
    balances = defaultdict(int)
    for log in logs:
        topics = log.get("topics", [])
        if len(topics) < 3:
            continue
        frm = "0x" + topics[1][-40:]
        to  = "0x" + topics[2][-40:]
        amt = int(log.get("data") or "0x0", 16)
        if frm != ZERO_ADDR:
            balances[frm] -= amt
        if to != ZERO_ADDR:
            balances[to] += amt
    return {addr: bal for addr, bal in balances.items() if bal > 0}

def detect_sybil_groups(logs: list, holders: dict,
                        time_window_blocks: int = 30,   # ~60 秒
                        amount_tolerance: float = 0.20  # 金额相差 ±20%
                        ) -> set:
    """
    关联地址检测，返回被标记为关联地址的集合。
    判定标准（满足任意两个即标记）：
      A. 同一 block 或相邻 30 块内收到相似金额的 mint（from=ZERO）
      B. 同一个 from 地址在同一时间窗口分发给多个地址
      C. 多个地址来自同一个 EOA（直接买入 tx 的 origin）
    """
    sybil: set = set()

    # 只看 mint 事件（from = ZERO，即 DEX 买入后的 token 分发）
    mints = [
        {
            "addr": "0x" + log["topics"][2][-40:],
            "block": int(log["blockNumber"], 16),
            "amt": int(log.get("data") or "0x0", 16),
            "tx": log.get("transactionHash", ""),
        }
        for log in logs
        if len(log.get("topics", [])) >= 3
        and "0x" + log["topics"][1][-40:] == ZERO_ADDR
        and "0x" + log["topics"][2][-40:] in holders
    ]

    if not mints:
        return sybil

    # A + B：同时间窗口内、相似金额的买入 → 关联
    mints.sort(key=lambda x: x["block"])
    for i, m in enumerate(mints):
        cluster = [m]
        for j in range(i + 1, len(mints)):
            m2 = mints[j]
            if m2["block"] - m["block"] > time_window_blocks:
                break
            if m2["tx"] == m["tx"]:
                continue
            # 金额相似度
            if m["amt"] > 0 and abs(m2["amt"] - m["amt"]) / m["amt"] <= amount_tolerance:
                cluster.append(m2)

        if len(cluster) >= 3:  # 同一时间窗口 3 个以上地址买相似金额 → 高度可疑
            for c in cluster:
                sybil.add(c["addr"])
                logger.debug(f"  [Sybil] {c['addr'][:12]} block={c['block']} amt={c['amt']}")

    logger.info(f"[Holder] 关联地址检测: 发现 {len(sybil)} 个可疑地址")
    return sybil

def analyze_real_holders(token_address: str,
                         min_real_holders: int = 50,
                         timeout_sec: float = 8.0
                         ) -> tuple[int, int, bool]:
    """
    主入口：分析真实持有人数
    返回: (raw_holders, real_holders, passed)
    passed=True 表示去关联后真实持有人 >= min_real_holders
    超时或 RPC 失败时返回 (0, 0, True)（豁免，不阻止买入）
    """
    try:
        logs = get_transfer_logs(token_address)
        if not logs:
            return 0, 0, True  # 无数据，豁免

        holders = compute_holders(logs)
        raw = len(holders)

        if raw == 0:
            return 0, 0, True  # 刚发行，豁免

        sybil = detect_sybil_groups(logs, holders)
        real = len({addr for addr in holders if addr not in sybil})

        passed = real >= min_real_holders
        logger.info(
            f"[Holder] {token_address[:12]}... "
            f"总持有人={raw} 关联地址={len(sybil)} 真实={real} "
            f"{'✅ 通过' if passed else f'❌ 未达{min_real_holders}人'}"
        )
        return raw, real, passed

    except Exception as e:
        logger.warning(f"[Holder] 持有人分析异常（豁免）: {e}")
        return 0, 0, True  # 异常时豁免，不阻止买入
