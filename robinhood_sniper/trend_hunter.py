"""
trend_hunter.py — Twitter 热度追踪买入模块

逻辑：
  每 TREND_INTERVAL_SEC 秒轮询一次热门关键词（牛来、marscoin 等高频词）
  发现符合条件的代币 → 买入 $50 固定仓位
  止盈：+25% 立即卖出（快进快出）
  止损：-35% 止损（和打新共用 _emergency_exit）
  24h 去重：和打新共用 is_bought_24h / mark_bought_24h

触发条件：
  1. 推特 24h 内提及 >= TREND_MIN_MENTIONS 条
  2. 近 1h 提及 >= TREND_MIN_RECENT 条（说明正在爆发）
  3. 总互动（likes+RT）>= TREND_MIN_ENGAGEMENT
  4. DexScreener 能找到对应代币（在 Robinhood Chain 上）
  5. 流动性 >= $5000
  6. MC <= $500k（不追已经拉过的）
  7. 通过模拟卖出检测（防蜜罐）
  8. 24h 内未买过
"""
import time
import logging
from dataclasses import dataclass, field

logger = logging.getLogger("sniper.trend")

# ── 配置 ──────────────────────────────────────────────────────────────────────
TREND_USD              = 50.0     # 固定买入 $50
TREND_TP_PCT           = 0.25    # 止盈 +25%
TREND_SL_PCT           = -0.35   # 止损 -35%
TREND_MAX_MC_USD       = 500_000  # 入场 MC 上限 $50万
TREND_MIN_LIQ_USD      = 5_000   # 最低流动性 $5000
TREND_MIN_MENTIONS     = 10      # 24h 内最低提及数
TREND_MIN_RECENT       = 3       # 近 1h 最低提及数
TREND_MIN_ENGAGEMENT   = 50      # 最低总互动（likes+RT）
TREND_INTERVAL_SEC     = 120     # 每 120s 扫一次
TREND_KEYWORDS = [               # 搜索关键词列表
    # ── 发射平台直接信号（最高价值，新币几乎必提） ──
    "hood.fun",                   # 主流 bonding curve 发射平台
    "pons.fun",                   # 另一发射平台
    "hood.fun launched",
    "hoodfun new",
    # ── 链名 + 新币信号 ──
    "robinhood chain meme",
    "robinhood chain launch",
    "RobinhoodChain meme",
    "RobinhoodChain launch",
    "RH Chain meme",
    "RH Chain new",
    "on robinhood token",
    "on robinhood coin",
    # ── 链 ID 精准匹配 ──
    "4663 launch",
    "4663 meme",
    # ── 中文侧 ──
    "Robinhood链 土狗",
    "Robinhood链 新币",
    "RH链 土狗",
]


@dataclass
class TrendPosition:
    token_address:   str
    token_symbol:    str
    entry_price_usd: float
    entry_qty:       float
    entry_time:      float = field(default_factory=time.time)
    cost_usd:        float = TREND_USD
    peak_pnl:        float = 0.0
    closed:          bool  = False


class TrendHunter:
    def __init__(self, monitor, notify_fn):
        self.monitor   = monitor
        self.notify    = notify_fn
        self.positions: list[TrendPosition] = []
        self._last_scan = 0.0

    # ── 主循环入口（由 main loop 每轮调用） ──────────────────────────────────
    def tick(self):
        now = time.time()
        # 监控已有热度仓位
        self._check_positions()
        # 定时扫描新热度信号
        if now - self._last_scan >= TREND_INTERVAL_SEC:
            self._last_scan = now
            self._scan_trends()

    # ── 扫描推特热度 ─────────────────────────────────────────────────────────
    def _scan_trends(self):
        from .signal import _clawby, is_bought_24h, mark_bought_24h
        from .executor import (get_eth_balance, get_quote,
                                buy_token, get_token_balance, check_sell_quote)
        from .blacklist import is_blacklisted

        logger.info("[Trend] 开始扫描推特热度信号...")

        for keyword in TREND_KEYWORDS:
            try:
                resp = _clawby("x_search", {"query": keyword, "limit": 50})
                if not resp:
                    continue
                posts = resp.get("data") or []
                if not posts:
                    continue

                now_ts   = time.time()
                count_24h = 0
                count_1h  = 0
                total_eng = 0
                # 从推文里提取代币名称（$符号后跟大写字母）
                import re
                symbols_mentioned: dict[str, int] = {}

                for p in posts:
                    # 时间过滤
                    try:
                        from email.utils import parsedate_to_datetime
                        ts = parsedate_to_datetime(p.get("created_at","")).timestamp()
                        if now_ts - ts < 86400:
                            count_24h += 1
                        if now_ts - ts < 3600:
                            count_1h  += 1
                    except Exception:
                        count_24h += 1
                        count_1h  += 1

                    likes = int(p.get("like_count") or 0)
                    rt    = int(p.get("retweet_count") or 0)
                    total_eng += likes + rt

                    # 提取 $SYMBOL
                    text = (p.get("text") or "") + " " + (p.get("full_text") or "")
                    for sym in re.findall(r'\$([A-Z]{2,10})\b', text.upper()):
                        symbols_mentioned[sym] = symbols_mentioned.get(sym, 0) + 1

                # 判断热度是否达标
                if count_24h < TREND_MIN_MENTIONS:
                    logger.info(f"[Trend] '{keyword}' 24h提及{count_24h}条 < 阈值{TREND_MIN_MENTIONS}，跳过")
                    continue
                if count_1h < TREND_MIN_RECENT:
                    logger.info(f"[Trend] '{keyword}' 1h提及{count_1h}条 < 阈值{TREND_MIN_RECENT}，跳过")
                    continue
                if total_eng < TREND_MIN_ENGAGEMENT:
                    logger.info(f"[Trend] '{keyword}' 互动{total_eng} < 阈值{TREND_MIN_ENGAGEMENT}，跳过")
                    continue

                logger.info(f"[Trend] 🔥 '{keyword}' 热度达标: 24h={count_24h} 1h={count_1h} 互动={total_eng}")
                logger.info(f"[Trend] 提及代币: {dict(sorted(symbols_mentioned.items(), key=lambda x:-x[1])[:5])}")

                # 尝试在 DexScreener 找到提及最多的代币
                top_syms = sorted(symbols_mentioned.items(), key=lambda x: -x[1])[:5]
                for sym, cnt in top_syms:
                    self._try_buy_trend(sym, keyword, count_24h, count_1h, total_eng)

            except Exception as e:
                logger.error(f"[Trend] 扫描 '{keyword}' 失败: {e}")

    def _try_buy_trend(self, symbol: str, keyword: str,
                       mentions_24h: int, mentions_1h: int, engagement: int):
        from .signal import _clawby, is_bought_24h, mark_bought_24h
        from .executor import (get_eth_balance, get_quote,
                                buy_token, get_token_balance, check_sell_quote)
        from .blacklist import is_blacklisted

        logger.info(f"[Trend] 尝试买入 ${symbol} (mentions={mentions_24h})")

        # 1. DexScreener 搜代币
        resp = _clawby("dexscreener_token_profiles", {"query": symbol})
        pairs = (resp.get("pairs") or []) if resp else []
        if not pairs:
            # 尝试通过搜索接口
            resp2 = _clawby("dexscreener_search", {"query": symbol, "chainId": "robinhood"})
            pairs = (resp2.get("pairs") or []) if resp2 else []

        if not pairs:
            logger.info(f"[Trend] {symbol} 在 DexScreener 找不到，跳过")
            return

        # 只取 Robinhood Chain 上的
        rh_pairs = [p for p in pairs if str(p.get("chainId","")).lower() in ("robinhood","4663")]
        if not rh_pairs:
            logger.info(f"[Trend] {symbol} 在 Robinhood Chain 上不存在，跳过")
            return

        # 按流动性取最强的
        pair = max(rh_pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd",0) or 0))
        addr = (pair.get("baseToken") or {}).get("address","").lower()
        if not addr:
            return

        mc  = float(pair.get("marketCap") or pair.get("fdv") or 0)
        liq = float((pair.get("liquidity") or {}).get("usd",0) or 0)

        # 2. 条件检查
        if mc > TREND_MAX_MC_USD:
            logger.info(f"[Trend] {symbol} MC=${mc/1000:.0f}k 超上限，跳过")
            return
        if liq < TREND_MIN_LIQ_USD:
            logger.info(f"[Trend] {symbol} 流动性${liq:.0f} 不足，跳过")
            return
        if is_bought_24h(addr):
            logger.info(f"[Trend] {symbol} 24h 内已买过，跳过")
            return
        if is_blacklisted(addr):
            logger.info(f"[Trend] {symbol} 创建者在黑名单，跳过")
            return

        # 3. 蜜罐检测
        sell_ok, sell_msg = check_sell_quote(addr)
        if not sell_ok:
            logger.warning(f"[Trend] {symbol} 模拟卖出失败（疑似蜜罐）: {sell_msg}")
            self.notify(f"🍯 [热度] {symbol} 疑似蜜罐，跳过")
            return

        # 4. 余额检查
        eth_price = 2420.0
        eth_bal   = get_eth_balance()
        buy_eth   = TREND_USD / eth_price
        if eth_bal < buy_eth * 1.1:
            logger.warning(f"[Trend] ETH 余额不足 ({eth_bal:.4f} ETH)")
            return

        # 5. 报价
        quote = get_quote(addr, buy_eth)
        if not quote:
            return
        token_qty = float(quote.get("toCoinAmount") or 0)
        if token_qty <= 0:
            return

        # 6. 执行买入
        logger.info(f"[Trend] 🚀 执行热度买入 ${symbol} ${TREND_USD} MC=${mc/1000:.0f}k liq=${liq/1000:.0f}k")
        ok, data = buy_token(addr, buy_eth)
        if not ok:
            logger.warning(f"[Trend] {symbol} 买入失败: {data}")
            return

        real_qty    = get_token_balance(addr)
        entry_price = (buy_eth * eth_price) / real_qty if real_qty > 0 else 0

        pos = TrendPosition(
            token_address   = addr,
            token_symbol    = symbol,
            entry_price_usd = entry_price,
            entry_qty       = real_qty,
            cost_usd        = TREND_USD,
        )
        self.positions.append(pos)
        mark_bought_24h(addr)

        msg = (f"🔥 [热度买入] ${symbol} ${TREND_USD} | "
               f"MC=${mc/1000:.0f}k 流动性=${liq/1000:.0f}k | "
               f"24h提及{mentions_24h}条 互动{engagement} | "
               f"止盈+25% 止损-35%")
        logger.info(f"[Trend] {msg}")
        self.notify(msg)

    # ── 监控热度仓位止盈止损 ─────────────────────────────────────────────────
    def _check_positions(self):
        from .signal import _clawby
        from .executor import get_token_balance, _baw
        from robinhood_sniper.config import (CHAIN_ID, NATIVE_TOKEN,
                                              SLIPPAGE_HOT, GAS_LEVEL)

        for pos in list(self.positions):
            if pos.closed:
                self.positions.remove(pos)
                continue

            try:
                resp = _clawby("dexscreener_token_pairs",
                               {"chainId": "robinhood", "tokenAddresses": pos.token_address})
                pairs = (resp.get("pairs") or []) if resp else []
                if not pairs:
                    continue

                pair  = pairs[0]
                price = float(pair.get("priceUsd") or 0)
                if price <= 0 or pos.entry_price_usd <= 0:
                    continue

                pnl = (price - pos.entry_price_usd) / pos.entry_price_usd
                if pnl > pos.peak_pnl:
                    pos.peak_pnl = pnl

                reason = None

                # 止盈 +25%
                if pnl >= TREND_TP_PCT:
                    reason = f"🎯 热度止盈 +{pnl:.0%}"

                # 止损 -35%
                elif pnl <= TREND_SL_PCT:
                    reason = f"🛑 热度止损 {pnl:.0%}"

                # 超时（4小时没到止盈就止损）
                elif (time.time() - pos.entry_time) >= 4 * 3600:
                    reason = f"⏰ 热度超时 PnL={pnl:.0%}"

                if reason:
                    qty = get_token_balance(pos.token_address)
                    if qty <= 0:
                        pos.closed = True
                        continue

                    ok, _ = _baw([
                        "market-order", "swap",
                        "--binanceChainId", CHAIN_ID,
                        "--fromToken", pos.token_address,
                        "--toToken", NATIVE_TOKEN,
                        "--fromTokenQty", str(round(qty, 6)),
                        "--slippage", str(SLIPPAGE_HOT),
                        "--mev", "false",
                        "--gasLevel", GAS_LEVEL,
                    ])
                    recv_usd = qty * price if ok else 0
                    pnl_usd  = recv_usd - pos.cost_usd

                    msg = (f"{'✅' if ok else '❌'} [热度出场] ${pos.token_symbol} "
                           f"{reason} | PnL ${pnl_usd:+.1f} ({pnl:+.0%})")
                    logger.info(f"[Trend] {msg}")
                    self.notify(msg)
                    pos.closed = True

            except Exception as e:
                logger.error(f"[Trend] 监控 {pos.token_symbol} 失败: {e}")
