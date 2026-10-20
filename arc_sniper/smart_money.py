"""
Arc Smart Money — 聪明钱共振监听层 (移植自 robinhood_sniper)

功能：
1. 实时监听 Arc 链上 Swap 事件
2. 统计时间窗口内每个 pool 被多少不同地址买入
3. 当某新代币出现 N 个不同买家（共振），触发加分信号
4. 识别大额买入 → 累计确认 → 加入聪明钱名单
5. 已知优质钱包白名单（FOMO KOL + 链上行为识别）

与 robinhood_sniper/smart_money.py 的区别：
- RPC 改为 Arc 链 (https://rpc.arc-scan.org)
- Swap topic 兼容 Uniswap V2/V3 + DAG Router
- USDC 是原生代币（不是 WETH），金额计算基于 USDC
- 持久化路径改为 arc_sniper 目录
"""

import time
import logging
import threading
import json
import os
from collections import defaultdict
from typing import Optional

logger = logging.getLogger("arc.smart_money")

# ═══════════════════════════════════════════════════════════════
# Arc 链常量
# ═══════════════════════════════════════════════════════════════

ARC_RPC = "https://rpc.arc-scan.org"
ARC_RPC_FALLBACK = "https://rpc.mainnet.arc.io"

# Swap 事件 topics
SWAP_TOPIC_V3 = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
SWAP_TOPIC_V2 = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37975c4d3f1cbb71e394d2b5a"

# Transfer 事件（用于追踪代币流入流出）
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# DAG Router 地址（主要 swap 路由）
DAG_ROUTER = "0x4e3bcce28caf98a143fd8bd9e4875ccab3e7bbe0"

# USDC 相关地址
WUSDC = "0x3600000000000000000000000000000000000000"       # wrapped USDC (6 decimals)
NATIVE_USDC = "0xfffffff7fffffffffffffffffffffffffffffffE"  # native USDC

# ── 已知优质钱包白名单 ────────────────────────────────────────────────────
# 从 FOMO 排行榜 + 链上行为识别 + Twitter 公开地址收集
# 持续更新：当发现新优质地址，加入此列表
SMART_WALLETS: dict[str, str] = {
    # Arc 链上识别的优质地址（通过链上行为识别后填入）
    # "0x...": "label (来源/备注)",
}

# ═══════════════════════════════════════════════════════════════
# 参数
# ═══════════════════════════════════════════════════════════════

WINDOW_SEC      = 10 * 60    # 10 分钟窗口内的共振
MIN_RESONANCE   = 2          # 至少 N 个不同钱包买入 → 共振
SMART_BONUS     = 2          # 有已知优质钱包买入时额外加分
RESONANCE_BONUS = 1          # 普通共振（N 个不同钱包）加分
MAX_PAIRS_CACHE = 500        # 缓存上限

# 聪明钱识别参数
SMART_MIN_USD    = 500.0     # 单笔买入门槛
SMART_MAX_USD    = 80_000   # 单笔上限（超大额≈合约/LP地址）
SMART_CONFIRM_N  = 3         # 同一钱包 N 笔大额买入才确认
SMART_MIN_TOTAL  = 1500.0   # N 笔合计最低金额
SMART_WINDOW_H   = 4         # 识别时间窗口（小时）

# ═══════════════════════════════════════════════════════════════
# 持久化路径
# ═══════════════════════════════════════════════════════════════

_PERSIST_DIR = os.path.dirname(os.path.abspath(__file__))
_SMART_PERSIST_FILE = os.path.join(_PERSIST_DIR, ".smart_wallets.json")

# ═══════════════════════════════════════════════════════════════
# 内存缓存
# ═══════════════════════════════════════════════════════════════

# {pool_addr: {wallet_addr: timestamp}} — 近期买入记录
_pair_buyers: dict[str, dict] = {}
_pair_to_token: dict[str, str] = {}
_lock = threading.Lock()

# 聪明钱识别缓存
# {wallet_addr: [(timestamp, usd_amount, pool_addr), ...]}
_big_buyers: dict = defaultdict(list)
_big_buyer_lock = threading.Lock()

# {pool_addr: {wallet_addr: total_usd}} — 当前轮聪明钱候选
_pool_smart_buyers: dict = defaultdict(lambda: defaultdict(float))

# 已确认聪明钱地址
_confirmed_smart: dict = {}

# ═══════════════════════════════════════════════════════════════
# RPC 工具
# ═══════════════════════════════════════════════════════════════

def _rpc(method, params, timeout=10, rpc=None):
    """低级 RPC 调用（带 fallback）"""
    import subprocess
    rpc_url = rpc or ARC_RPC
    payload = json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": 1})
    try:
        r = subprocess.run(
            ["curl", "-s", "-m", str(timeout), "-X", "POST", rpc_url,
             "-H", "Content-Type: application/json", "-d", payload],
            capture_output=True, text=True,
        )
        return json.loads(r.stdout)
    except Exception as e:
        logger.debug(f"[SM] RPC {method} 失败: {e}")
        return None


def _rpc_batch(requests_list, timeout=15, rpc=None):
    """批量 RPC"""
    import subprocess
    rpc_url = rpc or ARC_RPC
    payload = json.dumps(requests_list)
    try:
        r = subprocess.run(
            ["curl", "-s", "-m", str(timeout), "-X", "POST", rpc_url,
             "-H", "Content-Type: application/json", "-d", payload],
            capture_output=True, text=True,
        )
        result = json.loads(r.stdout)
        return result if isinstance(result, list) else []
    except Exception as e:
        logger.debug(f"[SM] RPC batch 失败: {e}")
        return []


# ═══════════════════════════════════════════════════════════════
# 持久化
# ═══════════════════════════════════════════════════════════════

def _load_smart_persist():
    """启动时加载已确认聪明钱地址"""
    global _confirmed_smart
    if os.path.exists(_SMART_PERSIST_FILE):
        try:
            data = json.load(open(_SMART_PERSIST_FILE))
            now = time.time()
            # 只保留 7 天内的记录
            _confirmed_smart = {
                k: v for k, v in data.items()
                if now - v.get("confirmed_at", 0) < 86400 * 7
            }
            logger.info(f"[SmartMoney] 加载已确认聪明钱 {len(_confirmed_smart)} 个地址")
        except Exception as e:
            logger.warning(f"[SmartMoney] 加载失败: {e}")
            _confirmed_smart = {}


def _save_smart_persist():
    """保存已确认聪明钱地址"""
    try:
        json.dump(_confirmed_smart, open(_SMART_PERSIST_FILE, "w"), ensure_ascii=False)
    except Exception as e:
        logger.debug(f"[SmartMoney] 保存失败: {e}")


# 启动时加载
_load_smart_persist()


# ═══════════════════════════════════════════════════════════════
# Swap 事件扫描
# ═══════════════════════════════════════════════════════════════

def ingest_blocks(from_block: int, to_block: int):
    """
    扫描 from_block ~ to_block 的 Swap 事件，更新 _pair_buyers 缓存。
    由主信号循环定期调用（每次 RPC 轮询后调用）。

    Arc 链特性：
    - Swap 事件可能来自 Tolly DEX、DAG Router、或 Uniswap V3
    - 同时扫描 V2 和 V3 swap topics
    - USDC 是原生代币，金额以 USDC 计
    """
    if to_block < from_block or to_block - from_block > 100:
        return

    # 扫描多个 swap topic
    all_logs = []
    for topic in [SWAP_TOPIC_V3, SWAP_TOPIC_V2]:
        resp = _rpc("eth_getLogs", [{
            "fromBlock": hex(from_block),
            "toBlock": hex(to_block),
            "topics": [topic],
        }], timeout=12)
        if resp and "result" in resp:
            all_logs.extend(resp["result"] or [])

    if not all_logs:
        return

    # 批量查 tx，获取 from 地址
    tx_hashes = list({l["transactionHash"] for l in all_logs})
    batch = [
        {"jsonrpc": "2.0", "method": "eth_getTransactionByHash",
         "params": [h], "id": i + 1}
        for i, h in enumerate(tx_hashes[:60])
    ]
    results = _rpc_batch(batch, timeout=15)

    tx_map = {}
    for res in results:
        tx = (res or {}).get("result") or {}
        if tx:
            tx_map[tx["hash"].lower()] = tx

    now = time.time()

    # 处理大额买入识别
    logs_with_tx = []
    for log in all_logs:
        txh = log["transactionHash"].lower()
        tx = tx_map.get(txh, {})
        if tx:
            logs_with_tx.append({"log": log, "tx": tx})

    if logs_with_tx:
        ingest_big_buys(logs_with_tx)

    # 更新 pair_buyers 缓存
    with _lock:
        for log in all_logs:
            pair = log["address"].lower()
            txh = log["transactionHash"].lower()
            tx = tx_map.get(txh, {})
            trader = (tx.get("from") or "").lower()
            if not trader:
                continue

            if pair not in _pair_buyers:
                if len(_pair_buyers) >= MAX_PAIRS_CACHE:
                    # 清掉最旧的
                    oldest = min(
                        _pair_buyers,
                        key=lambda p: max(_pair_buyers[p].values(), default=0),
                    )
                    del _pair_buyers[oldest]
                _pair_buyers[pair] = {}

            _pair_buyers[pair][trader] = now

        # 清理过期记录
        cutoff = now - WINDOW_SEC
        for pair in list(_pair_buyers):
            _pair_buyers[pair] = {
                a: t for a, t in _pair_buyers[pair].items() if t > cutoff
            }
            if not _pair_buyers[pair]:
                del _pair_buyers[pair]

    logger.debug(
        f"[SM] ingest {from_block}-{to_block}: {len(all_logs)} swaps, "
        f"cache={len(_pair_buyers)} pairs"
    )


def ingest_big_buys(logs_with_tx: list[dict], usdc_price: float = 1.0):
    """
    解析 Swap 事件列表（含 tx.from 信息），识别单笔 > $500 的真实买入。
    同一钱包累计 3 笔大额买入 + 总额 > $1500 → 加入已确认聪明钱名单。

    Arc 链特性：
    - USDC 是原生代币，通过 tx.value 获取金额（18 decimals）
    - 也检查 ERC20 Transfer 事件来获取 swap 金额
    """
    now = time.time()
    _wusdc = WUSDC.lower()
    _native = NATIVE_USDC.lower()

    with _big_buyer_lock:
        for item in logs_with_tx:
            log = item.get("log", {})
            tx = item.get("tx", {})
            trader = (tx.get("from") or "").lower()
            pool = (log.get("address") or "").lower()
            if not trader or not pool:
                continue

            topic = (log.get("topics") or [""])[0]
            data = log.get("data", "")

            # 计算买入金额（USD）
            usd_amount = 0.0
            is_buy = False

            try:
                # 方法1: 从 tx.value 获取（如果 USDC 是原生代币）
                tx_value = int(tx.get("value", "0x0"), 16) / 1e18
                if tx_value > 0.01:
                    usd_amount = tx_value  # 原生 USDC，1:1
                    is_buy = True

                # 方法2: 从 Swap event data 解析
                if not is_buy and topic == SWAP_TOPIC_V3 and len(data) >= 130:
                    def _signed(h):
                        v = int(h, 16)
                        return v - 2**256 if v >= 2**255 else v
                    a0 = _signed(data[2:66])
                    a1 = _signed(data[66:130])

                    # 判断 USDC 流入 pool = 买入
                    # USDC 地址比较来判断方向
                    if pool < _wusdc:
                        is_buy = a1 > 0  # wUSDC 是 token1
                    else:
                        is_buy = a0 > 0  # wUSDC 是 token0

                    usd_amount = max(abs(a0), abs(a1)) / 1e6  # wUSDC 6 decimals

                elif not is_buy and topic == SWAP_TOPIC_V2 and len(data) >= 130:
                    a0in = int(data[2:66], 16)
                    a1in = int(data[66:130], 16)
                    is_wusdc_0 = _wusdc < pool
                    is_buy = (a0in > 0) if is_wusdc_0 else (a1in > 0)
                    usd_amount = max(a0in, a1in) / 1e6

            except Exception:
                continue

            if not is_buy or usd_amount < SMART_MIN_USD or usd_amount > SMART_MAX_USD:
                continue

            # 记录大额买入
            _big_buyers[trader].append((now, usd_amount, pool))
            _pool_smart_buyers[pool][trader] += usd_amount

            # 检查是否达到「确认」条件
            window = SMART_WINDOW_H * 3600
            recent = [x for x in _big_buyers[trader] if now - x[0] < window]
            if len(recent) >= SMART_CONFIRM_N and trader not in _confirmed_smart:
                total = sum(x[1] for x in recent)
                if total >= SMART_MIN_TOTAL:
                    avg = total / len(recent)
                    _confirmed_smart[trader] = {
                        "label": f"大额买家(${total:,.0f}/{len(recent)}笔均${avg:,.0f})",
                        "total_usd": total,
                        "tx_count": len(recent),
                        "avg_usd": avg,
                        "confirmed_at": now,
                    }
                    logger.info(
                        f"[SmartMoney] 🧠 新确认聪明钱: {trader[:10]}... "
                        f"共{len(recent)}笔 总额${total:,.0f} 均${avg:,.0f}"
                    )
                    _save_smart_persist()


# ═══════════════════════════════════════════════════════════════
# 信号查询
# ═══════════════════════════════════════════════════════════════

def get_signal(pool_address: str) -> tuple[int, str]:
    """
    查询某个交易对的聪明钱信号。
    返回 (bonus_score, description)
      0 → 无信号
      1 → 普通共振或大额买入
      2 → 确认聪明钱买入
      3 → 聪明钱共振（多个确认聪明钱）
    """
    pool = pool_address.lower()
    now = time.time()
    cutoff = now - WINDOW_SEC

    # 1. 短期共振信号（10分钟窗口）
    with _lock:
        buyers = {a: t for a, t in _pair_buyers.get(pool, {}).items() if t > cutoff}

    # 2. 大额买入信号
    with _big_buyer_lock:
        big_buyers = dict(_pool_smart_buyers.get(pool, {}))

    smart_hits = [a for a in buyers if a in SMART_WALLETS] if buyers else []
    confirmed = {w: _confirmed_smart[w] for w in big_buyers if w in _confirmed_smart}
    n_buyers = len(buyers)
    n_big = len(big_buyers)
    n_confirmed = len(confirmed)
    total_usd = sum(big_buyers.values()) if big_buyers else 0

    # 优先级：确认聪明钱共振 > 确认聪明钱 > 普通共振 + 大额 > 普通共振 > 大额 > 无
    if smart_hits and n_buyers >= MIN_RESONANCE:
        names = [SMART_WALLETS[a] for a in smart_hits]
        return SMART_BONUS + RESONANCE_BONUS, (
            f"🧠 聪明钱共振! {n_buyers}个钱包买入, "
            f"已知优质: {', '.join(names[:2])}"
        )
    elif n_confirmed >= 2:
        labels = [v["label"] for v in list(confirmed.values())[:2]]
        return 3, f"🧠 {n_confirmed}个聪明钱共振 总额${total_usd:,.0f} | {', '.join(labels)}"
    elif n_confirmed >= 1:
        label = list(confirmed.values())[0]["label"]
        return 2, f"🧠 确认聪明钱买入 ${total_usd:,.0f} | {label}"
    elif smart_hits:
        names = [SMART_WALLETS[a] for a in smart_hits]
        return SMART_BONUS, f"🧠 已知优质钱包买入: {', '.join(names[:2])}"
    elif n_buyers >= MIN_RESONANCE and n_big >= 2 and total_usd >= 1000:
        return 2, f"👥 {n_buyers}个钱包共振 + {n_big}笔大额买入 总额${total_usd:,.0f}"
    elif n_buyers >= MIN_RESONANCE:
        return RESONANCE_BONUS, f"👥 多地址共振: {n_buyers}个不同钱包买入"
    elif n_big >= 2 and total_usd >= 1000:
        return 1, f"👀 {n_big}笔大额买入 总额${total_usd:,.0f}（未确认聪明钱）"
    elif n_big >= 1 and total_usd >= 500:
        return 1, f"💰 大额买入 ${total_usd:,.0f}（观察中）"

    return 0, ""


def register_pair(pool_address: str, token_address: str):
    """注册 pool → token 映射"""
    with _lock:
        _pair_to_token[pool_address.lower()] = token_address.lower()


def add_smart_wallet(address: str, label: str):
    """动态添加已知优质钱包（运行时可调用）"""
    SMART_WALLETS[address.lower()] = label
    logger.info(f"[SM] 添加聪明钱: {address[:10]}... ({label})")


def get_confirmed_smart_wallets() -> dict:
    """返回所有已确认聪明钱地址"""
    with _big_buyer_lock:
        return dict(_confirmed_smart)


def get_stats() -> dict:
    """返回当前缓存统计"""
    with _lock:
        pairs = len(_pair_buyers)
    with _big_buyer_lock:
        total_buyers = sum(len(v) for v in _pool_smart_buyers.values())
    return {
        "pairs_tracked": pairs,
        "smart_wallets_known": len(SMART_WALLETS),
        "smart_wallets_confirmed": len(_confirmed_smart),
        "total_big_buyers": total_buyers,
    }


# ═══════════════════════════════════════════════════════════════
# FOMO API 集成
# ═══════════════════════════════════════════════════════════════

def fetch_fomo_smart_wallets(chain: str = "arc", limit: int = 50) -> list[dict]:
    """
    从 FOMO API 获取聪明钱排行榜地址。

    FOMO API 文档: https://api.fomo.ai/docs
    端点: GET /api/v1/smart-money/leaderboard

    返回格式: [{"address": "0x...", "label": "trader_name", "pnl_pct": 123.4, ...}]

    注意: FOMO API 可能需要 API key，暂时使用公开端点。
    如果 API 不可用，返回空列表，不影响主流程。
    """
    import subprocess

    # 尝试公开端点
    fomo_urls = [
        f"https://api.fomo.ai/api/v1/smart-money/leaderboard?chain={chain}&limit={limit}",
        f"https://fomo.app/api/smart-money?chain={chain}&limit={limit}",
    ]

    for url in fomo_urls:
        try:
            r = subprocess.run(
                ["curl", "-s", "-m", "10", url],
                capture_output=True, text=True,
            )
            if r.returncode == 0 and r.stdout:
                data = json.loads(r.stdout)
                if isinstance(data, list):
                    return data
                elif isinstance(data, dict) and "data" in data:
                    return data["data"]
        except Exception:
            continue

    logger.debug("[SmartMoney] FOMO API 不可用，使用链上行为识别")
    return []


def sync_fomo_wallets():
    """
    从 FOMO API 同步聪明钱地址到本地白名单。
    定期调用（每小时一次），保持白名单更新。
    """
    wallets = fetch_fomo_smart_wallets()
    added = 0
    for w in wallets:
        addr = (w.get("address") or "").lower()
        label = w.get("label") or w.get("name") or f"FOMO #{w.get('rank', '?')}"
        pnl = w.get("pnl_pct", 0)
        if addr and addr not in SMART_WALLETS and pnl > 0:
            SMART_WALLETS[addr] = f"FOMO: {label} (PnL {pnl:+.1f}%)"
            added += 1

    if added:
        logger.info(f"[SmartMoney] 从 FOMO 同步 {added} 个聪明钱地址")
    return added
