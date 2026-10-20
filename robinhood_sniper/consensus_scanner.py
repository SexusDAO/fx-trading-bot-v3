"""
consensus_scanner.py — 共识币扫描器

策略：不打新，找市场已有共识的币
目标：已存活 2h+，持续有买盘，流动性稳定，非貔貅盘

入场标准（全部满足）：
  C1. 年龄 >= 2h，< 72h（太新不稳定，太老动能衰减）
  C2. 流动性 >= $50,000（真实资金支撑）
  C3. 流动性/MC 比 >= 0.8%（防止流动性虚假）
  C4. h24 买盘比 >= 50%（长期买盘主导）
  C5. h6 买盘比 >= 48%（近期买盘持续）
  C6. h24 买单数 >= 200（真实交易深度）
  C7. 流动性稳定：h1 流动性变化 < ±20%（无大额撤单）
  C8. 成交量稳定：h1量 / (h6量/6) 在 0.3x~5x 之间（不是瞬间爆量）
  C9. 无大户集中持仓：前5地址持仓 < 50%（可用 DS holders 或估算）
  C10. 价格不处于极度超买：h1 涨幅 < +50%（不追高）

仓位：$20~$30（比打新略大，因为风险更低）
止损：-20%（比打新宽松，共识币波动更平稳）
止盈：按 MC 里程碑，$500K / $1M / $3M / $10M
"""

import time
import logging
import requests
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("sniper.consensus")

# ── 配置 ─────────────────────────────────────────────────────────────────────
PROXY_URL = "socks5://REDACTED_PROXY_CREDENTIALS"
_PROXIES  = {"http": PROXY_URL, "https": PROXY_URL}

# 入场条件（针对 RH 链规模校准：RH 链总流动性/成交量约为主流链 1/10）
MIN_AGE_HOURS        = 2.0     # 最少存活 2 小时
MAX_AGE_HOURS        = 72.0    # 最多 72 小时（太老的不碰）
MIN_LIQUIDITY_USD    = 30_000  # 最低流动性 $30K（原$50k，RH链规模校准）
MIN_LIQ_MC_RATIO     = 0.005   # 流动性/MC >= 0.5%（原0.8%，RH链池子偏薄）
MIN_H24_BUY_RATIO    = 0.50    # h24 买盘比 >= 50%（长期买盘主导）
MIN_H6_BUY_RATIO     = 0.48    # h6 买盘比 >= 48%（近期买盘持续）
MIN_H24_BUYS         = 100     # h24 买单数 >= 100 笔（原200，RH链交易量校准）
MAX_VOL_SPIKE        = 5.0     # h1量不超过 h6均 的 5 倍（非瞬爆）
MIN_VOL_FLOOR        = 0.2     # h1量至少是 h6均 的 0.2 倍（仍有交易，原0.3放宽）
MAX_H1_PRICE_CHANGE  = 50.0    # h1 涨幅不超过 +50%（不追高）
MAX_LIQ_DROP_H1      = 0.20    # h1 流动性下降不超过 20%

# 仓位（比打新大，风险更低）
CONSENSUS_POSITION_USD = 25.0   # 固定 $25 每笔
CONSENSUS_STOP_LOSS    = -0.20  # -20% 止损
CONSENSUS_TP_MILESTONES = [     # MC 止盈里程碑（卖出比例）
    (500_000,   0.30),   # MC $500K → 卖 30%
    (1_000_000, 0.25),   # MC $1M   → 卖 25%
    (3_000_000, 0.20),   # MC $3M   → 卖 20%
    (10_000_000,0.15),   # MC $10M  → 卖 15%
]

# 扫描间隔
SCAN_INTERVAL_SEC = 120  # 每 120 秒扫描一次（多搜索词需要更长时间，避免频繁429）
DS_TRENDING_URL   = "https://api.dexscreener.com/token-profiles/latest/v1"
DS_SEARCH_BASE    = "https://api.dexscreener.com/latest/dex/tokens/"

# 已检查过的地址（本次运行内去重）
_checked_cache: dict = {}   # addr -> ts
_CACHE_TTL = 1800           # 30min 内不重复检查同一地址


# ── 数据获取 ─────────────────────────────────────────────────────────────────

def _fetch_pair(token_addr: str) -> Optional[dict]:
    """获取 token 最高流动性的 pair 数据"""
    try:
        r = requests.get(
            f"https://api.dexscreener.com/tokens/v1/robinhood/{token_addr}",
            timeout=8, proxies=_PROXIES
        )
        data = r.json()
        pairs = data if isinstance(data, list) else data.get("pairs", [])
        rh_pairs = [p for p in pairs if p.get("chainId") == "robinhood"]
        if not rh_pairs:
            return None
        return max(rh_pairs, key=lambda x: float((x.get("liquidity") or {}).get("usd", 0) or 0))
    except Exception as e:
        logger.debug(f"[Consensus] fetch_pair {token_addr[:12]} 失败: {e}")
        return None


# 覆盖 RH 链常见 token 名字模式（实测 24 个词 → 160+ 地址）
_SEARCH_TERMS = [
    "dog", "cat", "meme", "ai", "pepe", "coin", "token", "inu",
    "moon", "rob", "hood", "chain", "rb", "rh", "bird", "frog",
    "ape", "wolf", "doge", "baby", "super", "chad", "based", "elon",
    "btc", "eth", "sol", "robin", "pump", "launch", "ceo",
]


def _fetch_rh_trending() -> list[str]:
    """
    获取 RH 链上活跃代币地址列表。
    策略：多搜索词覆盖 DexScreener search API，实测可获取 100-200 个地址
    （原 pairs/robinhood API 只返回 0 个，token-boosts 只有 14 个）
    """
    addrs = set()

    # 1. 多关键词搜索（主力数据源，覆盖大量 RH 链代币）
    for term in _SEARCH_TERMS:
        try:
            r = requests.get(
                f"https://api.dexscreener.com/latest/dex/search?q={term}",
                timeout=8, proxies=_PROXIES
            )
            pairs = r.json().get("pairs", [])
            for p in pairs:
                if p.get("chainId") == "robinhood":
                    addr = p.get("baseToken", {}).get("address", "")
                    if addr:
                        addrs.add(addr.lower())
            time.sleep(0.08)  # 限速，避免 429
        except Exception as e:
            logger.debug(f"[Consensus] search '{term}' 失败: {e}")

    # 2. token-profiles 补充（最近被 DexScreener 收录的代币）
    try:
        r2 = requests.get(
            "https://api.dexscreener.com/token-profiles/latest/v1",
            timeout=8, proxies=_PROXIES
        )
        items = r2.json() if isinstance(r2.json(), list) else []
        for item in items:
            if item.get("chainId") == "robinhood":
                addr = item.get("tokenAddress", "")
                if addr:
                    addrs.add(addr.lower())
    except Exception:
        pass

    # 3. boosted tokens 补充
    try:
        r3 = requests.get(
            "https://api.dexscreener.com/token-boosts/latest/v1",
            timeout=8, proxies=_PROXIES
        )
        items3 = r3.json() if isinstance(r3.json(), list) else []
        for item in items3:
            if item.get("chainId") == "robinhood":
                addr = item.get("tokenAddress", "")
                if addr:
                    addrs.add(addr.lower())
    except Exception:
        pass

    logger.debug(f"[Consensus] _fetch_rh_trending 合计 {len(addrs)} 个地址")
    return list(addrs)


# ── 共识评估 ─────────────────────────────────────────────────────────────────

def evaluate_consensus(pair: dict) -> tuple[bool, str, float]:
    """
    评估一个 pair 是否满足共识币标准
    返回: (passed, reason, score)
    score 0-10，越高越优质
    """
    now_ts  = datetime.now(tz=timezone.utc).timestamp()
    created = pair.get("pairCreatedAt", 0)
    age_h   = (now_ts - created / 1000) / 3600 if created else 0

    liq  = float((pair.get("liquidity") or {}).get("usd", 0) or 0)
    mc   = float(pair.get("marketCap") or pair.get("fdv") or 0)
    v_h1 = float((pair.get("volume") or {}).get("h1", 0) or 0)
    v_h6 = float((pair.get("volume") or {}).get("h6", 0) or 0)
    v_h24= float((pair.get("volume") or {}).get("h24", 0) or 0)

    pc_h1 = float((pair.get("priceChange") or {}).get("h1", 0) or 0)
    pc_h6 = float((pair.get("priceChange") or {}).get("h6", 0) or 0)

    txns = pair.get("txns") or {}
    h1t  = txns.get("h1") or {}
    h6t  = txns.get("h6") or {}
    h24t = txns.get("h24") or {}

    h1_buys  = h1t.get("buys", 0)
    h1_sells = h1t.get("sells", 0)
    h6_buys  = h6t.get("buys", 0)
    h6_sells = h6t.get("sells", 0)
    h24_buys = h24t.get("buys", 0)
    h24_sells= h24t.get("sells", 0)

    h24_total    = h24_buys + h24_sells
    h6_total     = h6_buys  + h6_sells
    h24_buy_ratio= h24_buys / h24_total if h24_total > 0 else 0
    h6_buy_ratio = h6_buys  / h6_total  if h6_total  > 0 else 0

    liq_mc_ratio = liq / mc if mc > 0 else 0
    h6_avg_vol   = v_h6 / 6 if v_h6 > 0 else 0
    vol_spike    = v_h1 / h6_avg_vol if h6_avg_vol > 0 else 0

    sym = pair.get("baseToken", {}).get("symbol", "?")

    # ── 硬门槛检查（一票否决）────────────────────────────────
    if age_h < MIN_AGE_HOURS:
        return False, f"C1 年龄不足{age_h:.1f}h<{MIN_AGE_HOURS}h", 0
    if age_h > MAX_AGE_HOURS:
        return False, f"C1 太老{age_h:.0f}h>{MAX_AGE_HOURS}h", 0
    if liq < MIN_LIQUIDITY_USD:
        return False, f"C2 流动性不足${liq:,.0f}<${MIN_LIQUIDITY_USD:,.0f}", 0
    if mc > 0 and liq_mc_ratio < MIN_LIQ_MC_RATIO:
        return False, f"C3 流动性/MC={liq_mc_ratio:.2%}<{MIN_LIQ_MC_RATIO:.1%}(流动性虚薄)", 0
    if h24_buy_ratio < MIN_H24_BUY_RATIO:
        return False, f"C4 h24买盘比{h24_buy_ratio:.0%}<{MIN_H24_BUY_RATIO:.0%}(长期卖压)", 0
    if h6_buy_ratio < MIN_H6_BUY_RATIO:
        return False, f"C5 h6买盘比{h6_buy_ratio:.0%}<{MIN_H6_BUY_RATIO:.0%}(近期卖压)", 0
    if h24_buys < MIN_H24_BUYS:
        return False, f"C6 h24买单{h24_buys}<{MIN_H24_BUYS}(交易深度不足)", 0
    if h6_avg_vol > 0 and vol_spike > MAX_VOL_SPIKE:
        return False, f"C8 成交量异常暴增{vol_spike:.1f}x(可能是拉盘)", 0
    if h6_avg_vol > 0 and vol_spike < MIN_VOL_FLOOR:
        return False, f"C8 成交量枯竭{vol_spike:.2f}x(动能衰减)", 0
    if pc_h1 > MAX_H1_PRICE_CHANGE:
        return False, f"C10 h1涨幅+{pc_h1:.0f}%过高(不追高)", 0

    # ── 评分（软指标，越高越好）──────────────────────────────
    score = 0.0

    # 年龄分：2-12h 最佳
    if 2 <= age_h <= 12:
        score += 2.0
    elif age_h <= 24:
        score += 1.5
    else:
        score += 1.0

    # 流动性分（RH链规模校准：$30k-$200k 是主力区间）
    if liq >= 200_000:
        score += 2.0
    elif liq >= 100_000:
        score += 1.5
    elif liq >= 50_000:
        score += 1.2
    elif liq >= 30_000:
        score += 1.0

    # 买盘持续性
    if h24_buy_ratio >= 0.55 and h6_buy_ratio >= 0.55:
        score += 2.0
    elif h24_buy_ratio >= 0.52 and h6_buy_ratio >= 0.50:
        score += 1.5
    else:
        score += 1.0

    # 成交量健康度
    if 0.8 <= vol_spike <= 2.0:
        score += 1.5   # 最健康：与均值基本一致
    elif 0.5 <= vol_spike <= 3.0:
        score += 1.0
    else:
        score += 0.5

    # 流动性深度
    if liq_mc_ratio >= 0.03:
        score += 1.5   # 流动性超充足
    elif liq_mc_ratio >= 0.015:
        score += 1.0
    else:
        score += 0.5

    # h24 买单量
    if h24_buys >= 1000:
        score += 1.0
    elif h24_buys >= 500:
        score += 0.7
    else:
        score += 0.3

    return True, (
        f"✅ 共识币 age={age_h:.1f}h liq=${liq:,.0f} "
        f"h24买盘{h24_buy_ratio:.0%} h6买盘{h6_buy_ratio:.0%} "
        f"vol_spike={vol_spike:.1f}x score={score:.1f}"
    ), round(score, 1)


# ── 主扫描循环 ────────────────────────────────────────────────────────────────

class ConsensusScanner:
    """
    独立运行的共识币扫描器
    发现候选币后通过 on_signal 回调通知主流程
    """

    def __init__(self, on_signal, notify_fn=None):
        """
        on_signal(pair, score): 发现合格候选时调用
        notify_fn(msg):         发送通知
        """
        self.on_signal  = on_signal
        self.notify_fn  = notify_fn
        self._bought: set = set()   # 已买入的地址，不重复买

    def mark_bought(self, token_addr: str):
        self._bought.add(token_addr.lower())

    def scan_once(self) -> list[dict]:
        """执行一次全量扫描，返回合格候选列表"""
        candidates = []

        # 获取待扫描地址列表
        addrs = _fetch_rh_trending()
        logger.info(f"[Consensus] 扫描 {len(addrs)} 个地址...")

        for addr in addrs:
            addr_lower = addr.lower()

            # 去重：已买入 / 最近检查过
            if addr_lower in self._bought:
                continue
            if addr_lower in _checked_cache:
                if time.time() - _checked_cache[addr_lower] < _CACHE_TTL:
                    continue
            _checked_cache[addr_lower] = time.time()

            pair = _fetch_pair(addr)
            if not pair:
                continue

            passed, reason, score = evaluate_consensus(pair)
            sym = pair.get("baseToken", {}).get("symbol", "?")

            if passed:
                logger.info(f"[Consensus] 🎯 {sym} {reason}")
                candidates.append({"pair": pair, "score": score, "reason": reason,
                                    "token_addr": addr_lower})
            else:
                logger.debug(f"[Consensus] ✗ {sym}: {reason}")

            time.sleep(0.05)   # 限速

        # 按评分排序，只取前3（避免仓位过多）
        candidates.sort(key=lambda x: -x["score"])
        return candidates[:3]

    def run(self):
        """阻塞式主循环（在独立线程里跑）"""
        logger.info("[Consensus] 共识币扫描器启动 ✅")
        if self.notify_fn:
            self.notify_fn("🔍 共识币扫描器已启动（每60s扫描，寻找 2h+ 稳定买盘的代币）")

        while True:
            try:
                candidates = self.scan_once()
                for c in candidates:
                    pair  = c["pair"]
                    score = c["score"]
                    sym   = pair.get("baseToken", {}).get("symbol", "?")
                    mc    = float(pair.get("marketCap") or 0)
                    liq   = float((pair.get("liquidity") or {}).get("usd", 0) or 0)
                    msg = (f"🎯 共识币信号: {sym} | score={score} "
                           f"| MC=${mc:,.0f} | 流动性=${liq:,.0f}"
                           f"\n{c['reason']}")
                    logger.info(f"[Consensus] {msg}")
                    if self.on_signal:
                        self.on_signal(c)

                if not candidates:
                    logger.debug("[Consensus] 本轮无合格候选")

            except Exception as e:
                logger.error(f"[Consensus] 扫描异常: {e}", exc_info=True)

            time.sleep(SCAN_INTERVAL_SEC)
