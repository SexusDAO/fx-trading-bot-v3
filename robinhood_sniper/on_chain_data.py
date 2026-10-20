"""
on_chain_data.py  — 链上实时数据读取层 v1.1

目的：取代 DexScreener 作为新币发现后的第一手数据源。
     RPC 发现 PoolCreated → 立即链上读取 → 直接评分 → 买入
     延迟：~2~5秒（vs DS 的 15~25分钟）

支持：
  - Uniswap V3 / PONS 格式 pool（slot0 + balanceOf）
  - token symbol / decimals / totalSupply
  - 最近 N 块 Swap 事件 → 买卖单数量、成交量
  - 流动性、MC、价格计算
  - 聪明钱识别（v1.1 改进：3笔确认 + 总金额门槛 + 持久化 + 买入方向判断）
"""

import time
import logging
import requests
from typing import Optional

logger = logging.getLogger("sniper.onchain")

# ── 常量 ──────────────────────────────────────────────────────────────────────
RH_RPC    = "https://rpc.mainnet.chain.robinhood.com"
WETH      = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
ETH_PRICE = 2388.0           # 运行时会被动态更新

SWAP_TOPIC_V3 = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
SWAP_TOPIC_V2 = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37975c4d3f1cbb71e394d2b5a"

# 每块 ~2 秒；评分窗口
BLOCKS_PER_MIN  = 30          # 1min  = 30 块
BLOCKS_5MIN     = 150         # 5min
BLOCKS_H1       = 1800        # 1h
BLOCKS_H6       = 10800       # 6h（用于 vol 连续性）

_PROXIES = {
    "http":  "socks5://REDACTED_PROXY_CREDENTIALS",
    "https": "socks5://REDACTED_PROXY_CREDENTIALS",
}
_sess = requests.Session()
_sess.proxies = _PROXIES


# ── RPC helpers ───────────────────────────────────────────────────────────────
def _rpc(method: str, params: list, timeout: int = 5):
    try:
        r = _sess.post(RH_RPC,
                       json={"jsonrpc": "2.0", "method": method,
                             "params": params, "id": 1},
                       timeout=timeout)
        return r.json().get("result")
    except Exception as e:
        logger.debug(f"[OC] RPC {method} 失败: {e}")
        return None


def _batch(reqs: list, timeout: int = 5) -> dict:
    """批量 RPC，返回 {id: result}"""
    try:
        r = _sess.post(RH_RPC, json=reqs, timeout=timeout)
        d = r.json()
        if isinstance(d, list):
            return {item["id"]: item.get("result", "0x") for item in d}
    except Exception as e:
        logger.debug(f"[OC] batch RPC 失败: {e}")
    return {}


def _hex_to_int(v, default=0) -> int:
    try:
        return int(v, 16) if v and v != "0x" else default
    except Exception:
        return default


def _decode_symbol(raw: str) -> str:
    """解码 ABI 编码的 string 或 bytes32 symbol，最多保留 20 字符"""
    if not raw or raw == "0x":
        return "?"
    try:
        if len(raw) > 130:                         # string ABI
            sym_len = min(int(raw[66:130], 16), 40)  # 限制最多读 40 字节
            sym = bytes.fromhex(raw[130: 130 + sym_len * 2]).decode("utf-8", errors="replace").strip()
        else:                                       # bytes32
            sym = bytes.fromhex(raw[2:]).decode("utf-8", errors="replace").rstrip("\x00").strip()
        # 只保留 ASCII 可打印字符，最多 20 个
        sym = "".join(c for c in sym if c.isprintable() and ord(c) < 128)[:20]
        return sym or "?"
    except Exception:
        return "?"


# ── 主接口：读取 pool 完整数据 ─────────────────────────────────────────────────
def fetch_pool_data(pool_addr: str, token_addr: str,
                   latest_block: Optional[int] = None) -> Optional[dict]:
    """
    给定 pool 地址和 token 地址，返回完整评分所需数据（对标 DexScreener pair dict）。
    返回 None 表示数据不可用（流动性太低 / 读取失败）。

    返回 dict 结构与 DexScreener pair 兼容：
      priceUsd, marketCap, liquidity.usd,
      volume.m5/h1/h6, txns.m5.buys/sells, txns.h1.buys/sells,
      priceChange.m5/h1, pairAddress, baseToken.address/symbol,
      pairCreatedAt (从 pool 创建块推算)
    """
    pool  = pool_addr.lower()
    token = token_addr.lower()
    weth  = WETH.lower()

    # ── 1. 批量读取 token 基础信息 + 池子余额 ──────────────────────────────
    batch_ids = {
        1: ("eth_call", [{"to": weth,  "data": "0x70a08231" + "000000000000000000000000" + pool[2:]}, "latest"]),   # WETH in pool
        2: ("eth_call", [{"to": token, "data": "0x70a08231" + "000000000000000000000000" + pool[2:]}, "latest"]),   # token in pool
        3: ("eth_call", [{"to": token, "data": "0x18160ddd"}, "latest"]),                                           # totalSupply
        4: ("eth_call", [{"to": token, "data": "0x313ce567"}, "latest"]),                                           # decimals
        5: ("eth_call", [{"to": token, "data": "0x95d89b41"}, "latest"]),                                           # symbol
        6: ("eth_blockNumber", []),                                                                                   # latest block
    }
    reqs = [{"jsonrpc": "2.0", "method": m, "params": p, "id": i}
            for i, (m, p) in batch_ids.items()]
    res = _batch(reqs, timeout=5)

    if not res:
        return None

    latest  = _hex_to_int(res.get(6)) or int(_rpc("eth_blockNumber", []) or "0x0", 16)
    weth_in = _hex_to_int(res.get(1))
    tok_in  = _hex_to_int(res.get(2))
    ts_raw  = _hex_to_int(res.get(3))
    dec     = _hex_to_int(res.get(4)) or 18
    sym     = _decode_symbol(res.get(5, ""))

    if weth_in == 0 or tok_in == 0:
        return None   # 无流动性

    weth_val = weth_in  / 1e18
    tok_val  = tok_in   / (10 ** dec)
    supply   = ts_raw   / (10 ** dec)

    price_usd = (weth_val / tok_val) * ETH_PRICE
    mc        = supply * price_usd
    liq_usd   = weth_val * ETH_PRICE * 2   # 两边对称估算

    # ── 2. 扫 Swap 事件（批量，拿 m5 / h1 / h6）────────────────────────────
    if latest_block:
        latest = latest_block

    # 优先扫 V3，再试 V2
    def _get_logs(from_blk, to_blk):
        for topic in (SWAP_TOPIC_V3, SWAP_TOPIC_V2):
            logs = _rpc("eth_getLogs", [{
                "address":   pool,
                "fromBlock": hex(from_blk),
                "toBlock":   hex(to_blk),
                "topics":    [topic],
            }], timeout=5)
            if logs:
                return logs, topic
        return [], None

    logs_h6, topic = _get_logs(max(0, latest - BLOCKS_H6), latest)

    # 解析 swap 方向和成交量（ETH 计）
    def _parse_swap(log, topic):
        data = log.get("data", "")
        if len(data) < 130:
            return None
        try:
            def signed(h):
                v = int(h, 16)
                return v - 2**256 if v >= 2**255 else v

            if topic == SWAP_TOPIC_V3:
                a0 = signed(data[2:66])    # token0 amount
                a1 = signed(data[66:130])  # token1 amount
                # 判断哪个方向是 ETH：哪个 token 是 WETH
                # 买 = ETH 流入 pool（a_weth < 0）
                # 我们只关心方向和成交量
                eth_a = a0 if token > weth else a1  # 近似：按地址排序
                is_buy = eth_a < 0   # ETH 流入 = 买 token
                eth_vol = abs(a0 if a0 != 0 else a1) / 1e18
                return {"buy": is_buy, "eth": eth_vol}
            else:   # V2
                a0in  = int(data[2:66],    16)
                a1in  = int(data[66:130],  16)
                a0out = int(data[130:194], 16) if len(data) >= 194 else 0
                a1out = int(data[194:258], 16) if len(data) >= 258 else 0
                # 买 = ETH in，token out
                is_weth_0 = weth < token  # 地址排序
                if is_weth_0:
                    is_buy = a0in > 0
                    eth_vol = max(a0in, a0out) / 1e18
                else:
                    is_buy = a1in > 0
                    eth_vol = max(a1in, a1out) / 1e18
                return {"buy": is_buy, "eth": eth_vol}
        except Exception:
            return None

    swaps_h6 = [s for l in logs_h6 for s in [_parse_swap(l, topic)] if s]

    def _stats(swaps, from_blk):
        subset = [s for s, l in zip(swaps, logs_h6)
                  if _hex_to_int(l.get("blockNumber")) >= from_blk]
        buys  = sum(1 for s in subset if s["buy"])
        sells = len(subset) - buys
        vol   = sum(s["eth"] for s in subset) * ETH_PRICE
        return buys, sells, vol

    buys_m5,  sells_m5,  vol_m5  = _stats(swaps_h6, latest - BLOCKS_5MIN)
    buys_h1,  sells_h1,  vol_h1  = _stats(swaps_h6, latest - BLOCKS_H1)
    _,        _,          vol_h6  = _stats(swaps_h6, 0)

    # ── 3. 价格变化（用 Swap 事件推算）─────────────────────────────────────
    # 找 5min 前、1h 前的 sqrtPrice（V3）或 reserve 比（V2），简化：用成交量代替
    # 精确价格变化需要历史状态，暂用 ±0（后续可用 eth_call blockTag 补充）
    pc_m5 = 0.0
    pc_h1 = 0.0
    try:
        if len(logs_h6) >= 2:
            def _price_from_log(l):
                data = l.get("data", "")
                if len(data) >= 194 and topic == SWAP_TOPIC_V3:
                    sqrt = int(data[130:194], 16)
                    if sqrt > 0:
                        raw = (sqrt / 2**96) ** 2
                        # 调整方向
                        return raw * ETH_PRICE if token > weth else (1/raw) * ETH_PRICE if raw > 0 else 0
                return 0

            p_now = price_usd
            # 5min 前价格：取 5min 前最近一条 swap
            logs_before_5m = [l for l in logs_h6
                               if _hex_to_int(l["blockNumber"]) < latest - BLOCKS_5MIN]
            if logs_before_5m:
                p5 = _price_from_log(logs_before_5m[-1])
                if p5 > 0 and p_now > 0:
                    pc_m5 = (p_now - p5) / p5 * 100

            logs_before_1h = [l for l in logs_h6
                               if _hex_to_int(l["blockNumber"]) < latest - BLOCKS_H1]
            if logs_before_1h:
                p1h = _price_from_log(logs_before_1h[-1])
                if p1h > 0 and p_now > 0:
                    pc_h1 = (p_now - p1h) / p1h * 100
    except Exception:
        pass

    # ── 4. 组装成 DS 兼容格式 ───────────────────────────────────────────────
    now_ms = int(time.time() * 1000)
    pair_dict = {
        # DS 标准字段
        "chainId":     "robinhood",
        "pairAddress": pool_addr,
        "baseToken":   {"address": token_addr, "symbol": sym},
        "quoteToken":  {"address": WETH, "symbol": "WETH"},
        "priceUsd":    str(price_usd),
        "marketCap":   mc,
        "fdv":         mc,
        "liquidity":   {"usd": liq_usd},
        "volume": {
            "m5":  vol_m5,
            "h1":  vol_h1,
            "h6":  vol_h6,
            "h24": vol_h6,   # 暂用 h6 代替
        },
        "txns": {
            "m5": {"buys": buys_m5, "sells": sells_m5},
            "h1": {"buys": buys_h1, "sells": sells_h1},
        },
        "priceChange": {
            "m5": pc_m5,
            "h1": pc_h1,
        },
        "pairCreatedAt": now_ms,   # 调用方会用 RPC 事件时间覆盖
        "holders": 0,              # 链上读取成本高，保持豁免（R5 不拦 holders=0）
        # 扩展字段（评分用）
        "_onchain": True,
        "_weth_in_pool": weth_val,
        "_swap_count_h1": buys_h1 + sells_h1,
    }
    return pair_dict


def update_eth_price(price: float):
    """外部更新 ETH 价格（定期从 Binance 拉）"""
    global ETH_PRICE
    if price > 100:
        ETH_PRICE = price


# ── 便捷函数：批量查多个 pool ──────────────────────────────────────────────────
def fetch_multiple(pool_token_pairs: list[tuple[str, str]],
                   latest_block: Optional[int] = None) -> dict[str, dict]:
    """
    pool_token_pairs: [(pool_addr, token_addr), ...]
    返回 {pool_addr: pair_dict}，失败的 pool 不包含在结果里
    """
    results = {}
    for pool, token in pool_token_pairs:
        try:
            d = fetch_pool_data(pool, token, latest_block)
            if d:
                results[pool] = d
        except Exception as e:
            logger.debug(f"[OC] {pool[:10]} 读取失败: {e}")
    return results


# ═══════════════════════════════════════════════════════════════
# 聪明钱识别 v1.1
# 改进点：
#   1. 识别买入方向（V3/V2），只记录真正的买入
#   2. 确认门槛：3 笔 + 累计 $1500+（避免误识别散户高频小额）
#   3. 持久化到 .smart_wallets.json，重启不丢失
# ═══════════════════════════════════════════════════════════════
import threading as _threading
from collections import defaultdict as _defaultdict
import json as _json
import os as _os

# {wallet_addr: [(timestamp, usd_amount, pool_addr), ...]}
_big_buyers: dict = _defaultdict(list)
_big_buyer_lock = _threading.Lock()

# {pool_addr: {wallet_addr: total_usd}}  当前轮聪明钱候选
_pool_smart_buyers: dict = _defaultdict(lambda: _defaultdict(float))

# 已确认的聪明钱地址 {wallet: {'label': str, 'total_usd': float, 'tx_count': int, 'avg_usd': float}}
_confirmed_smart: dict = {}

# 已确认聪明钱持久化路径
_SMART_PERSIST_FILE = "/root/.openclaw/workspace/robinhood_sniper/.smart_wallets.json"

# 聪明钱识别参数（可覆盖）
SMART_MIN_USD    = 500.0    # 单笔买入门槛
_SMART_MAX_USD   = 80_000   # 单笔上限（超大额≈合约/LP地址）
SMART_CONFIRM_N  = 3        # 同一钱包 N 笔大额买入才确认（避免散户误识别）
SMART_MIN_TOTAL  = 1500.0   # N 笔合计最低金额（质量门）
SMART_WINDOW_H   = 4        # 属于同一钱包的识别时间窗口（小时）


def _load_smart_persist():
    """启动时加载已确认聪明钱地址（重启不丢失）"""
    global _confirmed_smart
    if _os.path.exists(_SMART_PERSIST_FILE):
        try:
            data = _json.load(open(_SMART_PERSIST_FILE))
            now = time.time()
            # 只保留 7 天内的记录
            _confirmed_smart = {k: v for k, v in data.items()
                                if now - v.get("confirmed_at", 0) < 86400 * 7}
            logger.info(f"[SmartMoney] 加载已确认聪明钱 {len(_confirmed_smart)} 个地址")
        except Exception as e:
            logger.warning(f"[SmartMoney] 加载失败: {e}")
            _confirmed_smart = {}


def _save_smart_persist():
    """保存已确认聪明钱地址"""
    try:
        _json.dump(_confirmed_smart, open(_SMART_PERSIST_FILE, "w"), ensure_ascii=False)
    except Exception as e:
        logger.debug(f"[SmartMoney] 保存失败: {e}")


# 启动时加载
_load_smart_persist()


def ingest_big_buys(logs_with_tx: list[dict], eth_price: float = None):
    """
    解析 Swap 事件列表（含 tx.from 信息），识别单笔 > $500 的真实买入。
    同一钱包累计 3 笔大额买入 + 总额 > $1500 → 加入已确认聪明钱名单。
    由 smart_money.ingest_blocks 调用（已有 tx_map）。
    """
    if eth_price is None:
        eth_price = ETH_PRICE

    now = time.time()
    _weth = WETH.lower()

    with _big_buyer_lock:
        for item in logs_with_tx:
            log     = item.get("log", {})
            tx      = item.get("tx", {})
            trader  = (tx.get("from") or "").lower()
            pool    = (log.get("address") or "").lower()
            if not trader or not pool:
                continue

            topic = (log.get("topics") or [""])[0]
            data  = log.get("data", "")

            eth_amount = 0.0
            is_buy = False
            try:
                if topic == SWAP_TOPIC_V3 and len(data) >= 130:
                    def _s(h):
                        v = int(h, 16)
                        return v - 2**256 if v >= 2**255 else v
                    a0 = _s(data[2:66])
                    a1 = _s(data[66:130])
                    # V3: a_weth > 0 = WETH 流入 pool = 买入 token
                    # 判断 WETH 是 token0 还是 token1（按地址大小排序）
                    # topics[1]=sender, topics[2]=recipient；通过检查 log.topics 更准确
                    # 简化：取 a0/a1 的正值侧作为买入方向（weth 地址小则是 token0）
                    if pool < _weth:
                        # weth = token1, a1 > 0 → weth 流入 → 买入
                        is_buy = a1 > 0
                    else:
                        # weth = token0, a0 > 0 → weth 流入 → 买入
                        is_buy = a0 > 0
                    eth_amount = max(abs(a0), abs(a1)) / 1e18

                elif topic == SWAP_TOPIC_V2 and len(data) >= 130:
                    a0in  = int(data[2:66],    16)
                    a1in  = int(data[66:130],  16)
                    a0out = int(data[130:194], 16) if len(data) >= 194 else 0
                    # V2: weth_in > 0 = ETH 流入 = 买入
                    is_weth_0 = _weth < pool
                    is_buy = (a0in > 0) if is_weth_0 else (a1in > 0)
                    eth_amount = max(a0in, a1in, a0out) / 1e18

            except Exception:
                continue

            # 只统计买入方向
            if not is_buy:
                continue

            usd = eth_amount * eth_price
            if usd < SMART_MIN_USD:
                continue
            if usd > _SMART_MAX_USD:
                continue

            # 记录大额买入
            _big_buyers[trader].append((now, usd, pool))
            _pool_smart_buyers[pool][trader] += usd

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
                    logger.info(f"[SmartMoney] 🧠 新确认聪明钱: {trader[:10]}... "
                                f"共{len(recent)}笔 总额${total:,.0f} 均${avg:,.0f}")
                    _save_smart_persist()  # 立即持久化


def get_smart_money_signal(pool_addr: str) -> tuple[int, str]:
    """
    查询某 pool 的聪明钱信号。
    返回 (bonus, description)

    加分逻辑（最高 +3）：
      +3: ≥2 个确认聪明钱共振
      +2: 1 个确认聪明钱买入
      +1: ≥2 笔大额买入（未确认，但总额 ≥ $1000）
      +1: 1 笔大额买入（总额 ≥ $500）
    """
    pool = pool_addr.lower()
    with _big_buyer_lock:
        buyers = dict(_pool_smart_buyers.get(pool, {}))

    if not buyers:
        return 0, ""

    confirmed = {w: _confirmed_smart[w] for w in buyers if w in _confirmed_smart}
    n_big = len(buyers)
    n_confirmed = len(confirmed)
    total_usd = sum(buyers.values())

    if n_confirmed >= 2:
        labels = [v["label"] for v in list(confirmed.values())[:2]]
        return 3, f"🧠 {n_confirmed}个聪明钱共振 总额${total_usd:,.0f} | {', '.join(labels)}"
    elif n_confirmed >= 1:
        label = list(confirmed.values())[0]["label"]
        return 2, f"🧠 确认聪明钱买入 ${total_usd:,.0f} | {label}"
    elif n_big >= 2 and total_usd >= 1000:
        return 1, f"👀 {n_big}笔大额买入 总额${total_usd:,.0f}（未确认聪明钱）"
    elif n_big >= 1 and total_usd >= 500:
        return 1, f"💰 大额买入 ${total_usd:,.0f}（观察中）"

    return 0, ""


def get_confirmed_smart_wallets() -> dict:
    """返回所有已确认聪明钱地址"""
    with _big_buyer_lock:
        return dict(_confirmed_smart)
