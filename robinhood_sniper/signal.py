"""
robinhood_sniper/signal.py
第一层：信号捕捉 — 轮询 Clawby DexScreener token_profiles
返回 Robinhood Chain 上的新代币地址列表
"""

import time
import logging
import requests
from typing import Optional
from .config import (
    CLAWBY_API_KEY, CLAWBY_BASE_URL, PROXY_URL,
    CHAIN_ID_DS, MAX_TOKEN_AGE_MIN
)

from .clawby_rpc import get_new_pairs_from_rpc, get_latest_block, rpc_call
from . import smart_money as _sm
logger = logging.getLogger("sniper.signal")

_last_rpc_block: int = 0   # RPC 扫描进度

_HEADERS  = {"X-API-Key": CLAWBY_API_KEY, "Content-Type": "application/json"}
_PROXIES  = {"http": PROXY_URL, "https": PROXY_URL}
_SEEN_FILE = "/root/.openclaw/workspace/robinhood_sniper/.seen_tokens.json"

def _load_seen() -> set:
    """从文件加载已处理代币集合，重启不丢失"""
    import json as _json, os as _os
    if _os.path.exists(_SEEN_FILE):
        try:
            return set(_json.load(open(_SEEN_FILE)))
        except Exception:
            pass
    return set()

def _save_seen(seen: set):
    """持久化 _seen 到文件"""
    import json as _json
    # 只保留最近 5000 条，防止文件无限增长
    items = list(seen)[-5000:]
    _json.dump(items, open(_SEEN_FILE, "w"))

_seen = _load_seen()   # 启动时从文件加载，重启不重扫旧代币
logger.info(f"[Signal] 已加载 {len(_seen)} 个历史代币地址，避免重复处理")

# ── 24h 买入去重（合约地址级别，打新 + 热度追踪共用） ─────────────────────────
_BOUGHT_24H_FILE = "/root/.openclaw/workspace/robinhood_sniper/.bought_24h.json"

def _load_bought_24h() -> dict:
    """加载 24h 内已买入记录 {addr_lower: timestamp}"""
    import json as _j, os as _o
    if _o.path.exists(_BOUGHT_24H_FILE):
        try:
            raw = _j.load(open(_BOUGHT_24H_FILE))
            now = time.time()
            # 过滤 24h 以外的记录
            return {k: v for k, v in raw.items() if now - v < 86400}
        except Exception:
            pass
    return {}

def _save_bought_24h(d: dict):
    import json as _j
    _j.dump(d, open(_BOUGHT_24H_FILE, "w"))

def mark_bought_24h(addr: str):
    """记录买入，24h 内不重复买"""
    _bought_24h[addr.lower()] = time.time()
    _save_bought_24h(_bought_24h)

def is_bought_24h(addr: str) -> bool:
    """检查 24h 内是否已买过"""
    ts = _bought_24h.get(addr.lower(), 0)
    return time.time() - ts < 86400

_bought_24h: dict = _load_bought_24h()
logger.info(f"[Signal] 24h 去重记录: {len(_bought_24h)} 个地址")

# ── 止损冷却名单（止损后 24h 内不再重复买入，防止 people 式反复摊亏）──────────
_COOLDOWN_FILE = "/root/.openclaw/workspace/robinhood_sniper/.sl_cooldown.json"
_COOLDOWN_TTL  = 86400  # 24h


def _load_sl_cooldown() -> dict:
    import json as _j, os as _o
    if _o.path.exists(_COOLDOWN_FILE):
        try:
            raw = _j.load(open(_COOLDOWN_FILE))
            now = time.time()
            return {k: v for k, v in raw.items() if now - v["ts"] < _COOLDOWN_TTL}
        except Exception:
            pass
    return {}


def _save_sl_cooldown(d: dict):
    import json as _j
    _j.dump(d, open(_COOLDOWN_FILE, "w"), ensure_ascii=False)


def mark_sl_cooldown(addr: str, symbol: str, pnl: float):
    """止损出场后把该代币地址加入 24h 冷却名单，防止重复买入"""
    _sl_cooldown[addr.lower()] = {"symbol": symbol, "pnl": pnl, "ts": time.time()}
    _save_sl_cooldown(_sl_cooldown)
    logger.info(f"[Signal] 止损冷却 {symbol} 入库，24h 内不再买入 (PnL={pnl:+.1%})")


def is_sl_cooldown(addr: str) -> bool:
    """检查该代币是否在 24h 止损冷却期内"""
    entry = _sl_cooldown.get(addr.lower())
    if not entry:
        return False
    return time.time() - entry["ts"] < _COOLDOWN_TTL


_sl_cooldown: dict = _load_sl_cooldown()
logger.info(f"[Signal] 止损冷却名单: {len(_sl_cooldown)} 个地址")



def _clawby(name: str, params: dict = None, timeout: int = 15) -> Optional[dict]:
    try:
        r = requests.post(
            CLAWBY_BASE_URL,
            headers=_HEADERS,
            json={"name": name, "params": params or {}},
            timeout=timeout,
            proxies=_PROXIES,
        )
        if r.status_code in (401, 403):
            raise RuntimeError(f"Clawby 授权失败 {r.status_code}")
        if r.status_code == 429:
            logger.warning("Clawby 429，等待 15s")
            time.sleep(15)
            return None
        r.raise_for_status()
        d = r.json()
        return d.get("data")
    except requests.exceptions.Timeout:
        logger.warning(f"Clawby {name} 超时")
        return None
    except Exception as e:
        logger.error(f"Clawby {name} 异常: {e}")
        return None


def fetch_new_tokens() -> list[dict]:
    """
    轮询 dexscreener_token_profiles，返回 Robinhood Chain 上
    尚未处理过的新代币列表。
    每条记录包含：
      address, description, links, icon, header (来自 DexScreener profile)
    """
    data = _clawby("dexscreener_token_profiles")
    if not data:
        return []

    profiles = data if isinstance(data, list) else []
    now_ms   = time.time() * 1000
    new_tokens = []

    for p in profiles:
        if p.get("chainId") != CHAIN_ID_DS:
            continue

        addr = (p.get("tokenAddress") or "").lower()
        if not addr or addr in _seen:
            continue

        # 时间过滤：token_profiles 没有 createTime，跳过时间检查
        # 后续在筛选层通过 dexscreener_token_pairs 拿到 pairCreatedAt 再判断
        _seen.add(addr)
        _save_seen(_seen)
        new_tokens.append({
            "address":     addr,
            "description": p.get("description", ""),
            "links":       p.get("links", []),
            "icon":        p.get("icon", ""),
            "header":      p.get("header", ""),
            "source":      "dexscreener_token_profiles",
        })
        logger.info(f"[Signal] 发现新代币: {addr[:10]}... desc={p.get('description','')[:40]}")

    return new_tokens


def fetch_pair_data(token_address: str) -> Optional[dict]:
    """
    通过 dexscreener_token_pairs 拿到完整交易对数据
    返回流动性最大的那个 pair 的原始数据
    
    安全卤：如果同一代币有多个 pair，且最高流动性 pair 与次高 pair 价格相差 > 3x，
    说明项目方在用小池误导入入，将小池标记为低流动性使其被 R规则拦截。
    """
    data = _clawby("dexscreener_token_pairs", {
        "chainId": CHAIN_ID_DS,
        "tokenAddresses": token_address,
    })
    if not data:
        return None

    pairs = data.get("pairs") or []
    # 过滤 robinhood 链，按流动性排序
    rh_pairs = [p for p in pairs if p.get("chainId") == CHAIN_ID_DS]
    if not rh_pairs:
        return None

    rh_pairs.sort(key=lambda x: float((x.get("liquidity") or {}).get("usd", 0) or 0), reverse=True)
    best = rh_pairs[0]

    # —— 多池陷阱检测 ———————————————————————————
    # 如果有 ≥2 个 pair，检查最高流动性 pair 与次高流动性 pair 的价格差异。
    # 价格差 > 5x 说明最高流动性的池很可能是废弃小池，主力在其他 pair。
    if len(rh_pairs) >= 2:
        best_price  = float(best.get("priceUsd") or 0)
        second_liq  = float((rh_pairs[1].get("liquidity") or {}).get("usd", 0) or 0)
        second_price = float(rh_pairs[1].get("priceUsd") or 0)
        best_liq    = float((best.get("liquidity") or {}).get("usd", 0) or 0)

        # 如果最高流动性 pair 与次高 pair 的价格相差 > 5x，取价格最高的那个（真正的主力池）
        if best_price > 0 and second_price > 0:
            price_ratio = max(best_price, second_price) / min(best_price, second_price)
            if price_ratio > 5:
                # 取价格最高的 pair 作为主力池（即使流动性较小）
                best_by_price = max(rh_pairs, key=lambda x: float(x.get("priceUsd") or 0))
                best_price_liq = float((best_by_price.get("liquidity") or {}).get("usd", 0) or 0)
                if best_price_liq >= 3000:  # 主力池至少要有 $3k 流动性
                    logger.info(
                        f"[Signal] 多池检测: 价格差异 {price_ratio:.1f}x，切换主力池 "
                        f"liq=${best_price_liq:,.0f} 价格=${float(best_by_price.get('priceUsd') or 0):.8f}"
                    )
                    best = best_by_price

    return best


def fetch_pair_age_min(pair: dict) -> float:
    """从 pair 数据中计算代币年龄（分钟）"""
    created_at = pair.get("pairCreatedAt")  # ms timestamp
    if not created_at:
        return 9999.0
    return (time.time() * 1000 - created_at) / 60_000


def fetch_new_tokens_rpc() -> list[dict]:
    """
    主要发现通道：直连 RPC 监听 PairCreated 事件（零延迟）
    发现后转成与 fetch_new_tokens 相同格式，统一进入筛选层
    """
    global _last_rpc_block

    latest = get_latest_block()
    if latest == 0:
        return []

    if _last_rpc_block == 0:
        _last_rpc_block = latest - 5   # 首次运行，扫最近 5 个区块

    pairs = get_new_pairs_from_rpc(_last_rpc_block + 1, latest)
    # 同步喂入 smart_money 聪明钱监听层（收集 Swap 买入数据）
    try:
        _sm.ingest_blocks(_last_rpc_block + 1, latest)
    except Exception:
        pass
    _last_rpc_block = latest

    NATIVE = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
    WETH   = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
    native_set = {NATIVE, WETH}

    new_tokens = []
    for p in pairs:
        t0, t1 = p["token0"], p["token1"]
        # 必须有一端是原生 ETH/WETH
        if t0 not in native_set and t1 not in native_set:
            continue
        token_addr = t1 if t0 in native_set else t0

        if token_addr in _seen:
            continue
        _seen.add(token_addr)
        _save_seen(_seen)

        new_tokens.append({
            "address":     token_addr,
            "description": "",
            "links":       [],
            "icon":        "",
            "header":      "",
            "source":      "rpc_pair_created",
            "pair_addr":   p.get("pair", ""),
            "tx_hash":     p.get("txHash", ""),
        })
        logger.info(f"[RPC] 🆕 发现新交易对 token={token_addr[:10]}... "
                    f"pair={p.get('pair','')[:10]}... tx={p.get('txHash','')[:12]}...")

    return new_tokens
