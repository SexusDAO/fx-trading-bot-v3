"""
fx_major/scanner.py — 主流币扫描器

核心逻辑：趋势模式 / 区间模式自动切换
  - trend_up:   回踩 EMA20 做多，顺势入场
  - trend_down: 反弹 EMA20 做空，顺势入场
  - range:      区间下沿做多 / 区间上沿做空，双向均做

复用 fx/ 的：
  - coinglass_api（清算图 + 恐惧贪婪）
  - clawby_adapter（清算热力图）
  - vol_regime（BTC/ETH DVOL 信号）
  - binance_api（K 线 / 标记价格 / 资金费率）

输出：信号列表 [{"symbol", "direction", "mode", "score", "entry_price", "sl", "tp1", "tp2", ...}]
"""

import json
import logging
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))                   # workspace/
sys.path.insert(0, str(ROOT.parent / "fx"))            # 复用 fx/ 模块

logger = logging.getLogger("fx_major.scanner")

# ── 指标工具 ──────────────────────────────────────────────────────────────────
def _ema(closes, n):
    if len(closes) < n: return closes[-1] if closes else 0.0
    k = 2 / (n + 1); e = sum(closes[:n]) / n
    for c in closes[n:]: e = c * k + e * (1 - k)
    return e

def _rsi(closes, n=14):
    if len(closes) < n + 1: return 50.0
    g, l_ = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]; g.append(max(d,0)); l_.append(max(-d,0))
    ag = sum(g[-n:])/n; al = sum(l_[-n:])/n
    return 100.0 if al == 0 else 100.0 - 100.0 / (1 + ag/al)

def _atr(klines, n=14):
    trs = []
    for i in range(1, len(klines)):
        h, lo, pc = float(klines[i][2]), float(klines[i][3]), float(klines[i-1][4])
        trs.append(max(h-lo, abs(h-pc), abs(lo-pc)))
    return sum(trs[-n:])/n if trs else 0.0

def _vol_ma(klines, n=20):
    vols = [float(k[5]) for k in klines]
    return sum(vols[-n:])/n if len(vols) >= n else (sum(vols)/len(vols) if vols else 1)


# ══════════════════════════════════════════════════════════════════════════════
# 仓位计算
# ══════════════════════════════════════════════════════════════════════════════
def calc_position_size(symbol: str, sl_distance: float, entry_price: float,
                       paper_capital: float, score: int) -> float:
    """
    动态仓位计算，目标单次盈利 1%-5% 本金。

    逻辑：
      1. 基础名义仓位 = 用户愿意承受的最大亏损 / 止损距离%
         最大亏损 = 本金 × risk_pct（由 score 动态决定）
      2. 乘以 vol_regime 乘数
      3. 限制在标的硬顶以内

    risk_pct 范围：score 60→80 对应 risk 1.5%→3.0%
    TP/SL 盈亏比约 2:1，所以 risk 1.5% → 单次盈利约 3%
    """
    from fx_major.config import (
        PAPER_CAPITAL, LEVERAGE, MAX_MARGIN_PCT,
        CAP_BTC_ETH, CAP_MAJOR, CAP_DEFAULT, TARGET_PROFIT_MIN_PCT,
    )

    if sl_distance <= 0 or entry_price <= 0:
        return 0.0

    # 根据分数动态 risk
    score = max(50, min(score, 90))
    risk_pct = 0.015 + (score - 50) / 40 * 0.025   # 50分:1.5%, 90分:4.0%

    max_loss_u  = paper_capital * risk_pct
    sl_pct      = sl_distance / entry_price
    notional    = max_loss_u / sl_pct if sl_pct > 0 else 0

    # Vol regime 乘数（BTC/ETH 适用）
    try:
        sys.path.insert(0, str(ROOT.parent / "fx"))
        from shared.vol_regime import get_vol_multipliers
        vm = get_vol_multipliers(symbol)
        notional *= vm["size"]
    except Exception:
        pass

    # 标的硬顶
    cap = CAP_BTC_ETH if symbol in ("BTCUSDT", "ETHUSDT") else \
          CAP_MAJOR   if any(s in symbol for s in ("SOL","BNB","XRP","ADA","DOGE")) else \
          CAP_DEFAULT
    notional = min(notional, cap)

    # 保证金上限检查
    margin = notional / LEVERAGE
    if margin > paper_capital * MAX_MARGIN_PCT:
        notional = paper_capital * MAX_MARGIN_PCT * LEVERAGE

    return round(notional, 2)


# ══════════════════════════════════════════════════════════════════════════════
# 清算区加分（复用 fx/ clawby_adapter）
# ══════════════════════════════════════════════════════════════════════════════
def _liq_bonus(symbol: str, direction: str, entry_price: float, atr_1h: float) -> tuple[int, str]:
    """
    检查入场价附近是否有清算区支撑/阻力。
    返回 (bonus_score, detail_str)
    """
    try:
        sys.path.insert(0, str(ROOT.parent / "fx"))
        from shared.coinglass_api import get_liq_orders
        from fx_major.config import LIQ_ZONE_MIN_USD, LIQ_ZONE_MAX_ATR_MULT, LIQ_BONUS_SCORE

        orders = get_liq_orders(symbol) or []
        best_dist = float("inf")
        best_usd  = 0

        for o in orders:
            price = float(o.get("price", 0))
            usd   = float(o.get("usd", 0))
            if usd < LIQ_ZONE_MIN_USD:
                continue
            dist  = abs(price - entry_price)
            # 多单找下方清算区，空单找上方清算区
            if direction == "LONG"  and price < entry_price and dist < best_dist:
                best_dist = dist; best_usd = usd
            elif direction == "SHORT" and price > entry_price and dist < best_dist:
                best_dist = dist; best_usd = usd

        if best_dist <= atr_1h * LIQ_ZONE_MAX_ATR_MULT and best_usd >= LIQ_ZONE_MIN_USD:
            detail = f"清算区${best_usd/1e6:.1f}M距离{best_dist/entry_price*100:.1f}%"
            return LIQ_BONUS_SCORE, detail

    except Exception as e:
        logger.debug(f"[scanner] liq_bonus 失败: {e}")

    return 0, ""


# ══════════════════════════════════════════════════════════════════════════════
# 恐惧贪婪加分
# ══════════════════════════════════════════════════════════════════════════════
def _fear_greed_bonus(direction: str) -> tuple[int, str]:
    try:
        sys.path.insert(0, str(ROOT.parent / "fx"))
        from shared.coinglass_api import get_fear_greed
        from fx_major.config import FEAR_GREED_EXTREME_FEAR, FEAR_GREED_EXTREME_GREED, FEAR_GREED_BONUS_SCORE

        fg = get_fear_greed()
        if not fg:
            return 0, ""
        val = int(fg.get("value", 50))

        if direction == "LONG"  and val <= FEAR_GREED_EXTREME_FEAR:
            return FEAR_GREED_BONUS_SCORE, f"极度恐惧({val})"
        if direction == "SHORT" and val >= FEAR_GREED_EXTREME_GREED:
            return FEAR_GREED_BONUS_SCORE, f"极度贪婪({val})"
    except Exception as e:
        logger.debug(f"[scanner] fear_greed 失败: {e}")

    return 0, ""


# ══════════════════════════════════════════════════════════════════════════════
# BTC 大行情检查（复用逻辑）
# ══════════════════════════════════════════════════════════════════════════════
_btc_shock_ts = 0.0
_btc_shock_until = 0.0

def _check_btc_shock(klines_btc_1h: list) -> bool:
    """BTC 1H 涨跌超过阈值 → 暂停开仓"""
    global _btc_shock_until
    from fx_major.config import BTC_SHOCK_PCT

    now = time.time()
    if now < _btc_shock_until:
        return True  # 仍在熔断期

    if len(klines_btc_1h) >= 2:
        k = klines_btc_1h[-2]
        chg = abs((float(k[4]) - float(k[1])) / float(k[1]))
        if chg >= BTC_SHOCK_PCT:
            _btc_shock_until = now + 1800  # 熔断30分钟
            logger.warning(f"[scanner] BTC 大行情熔断: 1H涨跌{chg*100:.1f}%，暂停30分钟")
            return True
    return False


# ══════════════════════════════════════════════════════════════════════════════
# 单标的分析
# ══════════════════════════════════════════════════════════════════════════════
def analyze_symbol(symbol: str, klines_1h: list, klines_15m: list,
                   klines_1d: list, mark_price: float) -> list[dict]:
    """
    对单个标的进行分析，返回信号列表（可能0个或多个）。
    """
    from fx_major.config import (
        RSI_OVERSOLD, RSI_OVERBOUGHT, RSI_TREND_MID_LO, RSI_TREND_MID_HI,
        RANGE_LONG_UPPER_PCT, RANGE_SHORT_LOWER_PCT, RANGE_MIN_SPREAD_PCT,
        RANGE_SL_ATR_MULT, RANGE_TP_RATIO, TREND_SL_ATR_MULT,
        TREND_TP1_ATR_MULT, TREND_TP2_ATR_MULT, TREND_TP1_RATIO, TREND_TP2_RATIO,
        SL_MAX_PCT, EMA_PULLBACK_ATR, RANGE_VOL_SHRINK_MAX,
        TREND_PULLBACK_MAX_BODY_RATIO, TREND_VOL_CONFIRM_MIN,
        PAPER_CAPITAL,
    )
    from fx_major.universe import get_market_mode

    if len(klines_1h) < 50 or len(klines_1d) < 5:
        return []

    closes_1h = [float(k[4]) for k in klines_1h]
    opens_1h  = [float(k[1]) for k in klines_1h]
    highs_1h  = [float(k[2]) for k in klines_1h]
    lows_1h   = [float(k[3]) for k in klines_1h]
    price     = mark_price or closes_1h[-1]

    atr_1h    = _atr(klines_1h, 14)
    rsi_1h    = _rsi(closes_1h, 14)
    ema20_1h  = _ema(closes_1h, 20)
    vol_ma_1h = _vol_ma(klines_1h, 20)
    vol_last3 = [float(k[5]) for k in klines_1h[-3:]]
    vol_ratio = (sum(vol_last3)/3 / vol_ma_1h) if vol_ma_1h > 0 else 1.0

    # 日线模式判断
    minfo = get_market_mode(symbol, klines_1d)
    mode  = minfo["mode"]

    signals = []

    # ── 区间模式 ──────────────────────────────────────────────────────────────
    if mode == "range":
        day_high = minfo["day_high"]
        day_low  = minfo["day_low"]
        spread   = day_high - day_low

        if spread / price < RANGE_MIN_SPREAD_PCT:
            return []  # 区间太窄

        pos_pct = (price - day_low) / spread  # 0 = 下沿，1 = 上沿

        # 做多：在区间下 30% 分位
        if pos_pct <= RANGE_LONG_UPPER_PCT and rsi_1h < RSI_OVERSOLD \
                and vol_ratio < RANGE_VOL_SHRINK_MAX:
            sl      = day_low - atr_1h * RANGE_SL_ATR_MULT
            tp1     = price + spread * RANGE_TP_RATIO
            tp2     = day_high - atr_1h * 0.2   # 接近上沿
            sl_dist = price - sl
            if sl_dist / price <= SL_MAX_PCT and tp1 > price:
                score = 60
                score += 10 if rsi_1h < 30 else 0
                score += 5  if pos_pct < 0.15 else 0   # 越靠下越好
                liq_b, liq_d = _liq_bonus(symbol, "LONG", price, atr_1h)
                fg_b,  fg_d  = _fear_greed_bonus("LONG")
                score += liq_b + fg_b
                notional = calc_position_size(symbol, sl_dist, price, PAPER_CAPITAL, score)
                signals.append({
                    "symbol": symbol, "direction": "LONG", "mode": "range",
                    "entry_price": round(price, 6),
                    "sl": round(sl, 6), "tp1": round(tp1, 6), "tp2": round(tp2, 6),
                    "tp1_ratio": TREND_TP1_RATIO, "tp2_ratio": TREND_TP2_RATIO,
                    "notional": notional, "score": score,
                    "indicators": {
                        "rsi_1h": round(rsi_1h, 1), "pos_pct": round(pos_pct, 3),
                        "vol_ratio": round(vol_ratio, 2), "atr_1h": round(atr_1h, 4),
                        "day_high": day_high, "day_low": day_low,
                        "liq_detail": liq_d, "fg_detail": fg_d,
                    },
                    "market_info": minfo,
                })

        # 做空：在区间上 70% 分位
        elif pos_pct >= RANGE_SHORT_LOWER_PCT and rsi_1h > RSI_OVERBOUGHT \
                and vol_ratio < RANGE_VOL_SHRINK_MAX:
            sl      = day_high + atr_1h * RANGE_SL_ATR_MULT
            tp1     = price - spread * RANGE_TP_RATIO
            tp2     = day_low + atr_1h * 0.2
            sl_dist = sl - price
            if sl_dist / price <= SL_MAX_PCT and tp1 < price:
                score = 60
                score += 10 if rsi_1h > 70 else 0
                score += 5  if pos_pct > 0.85 else 0
                liq_b, liq_d = _liq_bonus(symbol, "SHORT", price, atr_1h)
                fg_b,  fg_d  = _fear_greed_bonus("SHORT")
                score += liq_b + fg_b
                notional = calc_position_size(symbol, sl_dist, price, PAPER_CAPITAL, score)
                signals.append({
                    "symbol": symbol, "direction": "SHORT", "mode": "range",
                    "entry_price": round(price, 6),
                    "sl": round(sl, 6), "tp1": round(tp1, 6), "tp2": round(tp2, 6),
                    "tp1_ratio": TREND_TP1_RATIO, "tp2_ratio": TREND_TP2_RATIO,
                    "notional": notional, "score": score,
                    "indicators": {
                        "rsi_1h": round(rsi_1h, 1), "pos_pct": round(pos_pct, 3),
                        "vol_ratio": round(vol_ratio, 2), "atr_1h": round(atr_1h, 4),
                        "day_high": day_high, "day_low": day_low,
                        "liq_detail": liq_d, "fg_detail": fg_d,
                    },
                    "market_info": minfo,
                })

    # ── 趋势模式 ──────────────────────────────────────────────────────────────
    elif mode in ("trend_up", "trend_down"):
        ema20    = minfo["ema20"]
        closes_d = [float(k[4]) for k in klines_1d]
        opens_d  = [float(k[1]) for k in klines_1d]

        # 前一日线实体（用于判断回踩深度）
        prev_body = abs(closes_d[-2] - opens_d[-2]) if len(closes_d) >= 2 else 0

        direction = "LONG" if mode == "trend_up" else "SHORT"
        rsi_ok    = (RSI_TREND_MID_LO <= rsi_1h <= RSI_TREND_MID_HI)

        # 回踩判断：价格靠近 EMA20
        pullback_dist = abs(price - ema20)
        near_ema20    = pullback_dist < atr_1h * EMA_PULLBACK_ATR

        # 回踩深度限制
        pullback_depth = abs(price - closes_d[-2])
        depth_ok = pullback_depth <= prev_body * TREND_PULLBACK_MAX_BODY_RATIO \
                   if prev_body > 0 else True

        # 入场K线量比
        vol_ok = vol_ratio >= TREND_VOL_CONFIRM_MIN

        if rsi_ok and near_ema20 and depth_ok and vol_ok:
            if direction == "LONG":
                sl   = min(lows_1h[-3:]) - atr_1h * TREND_SL_ATR_MULT
                tp1  = price + atr_1h * TREND_TP1_ATR_MULT
                tp2  = price + atr_1h * TREND_TP2_ATR_MULT
            else:
                sl   = max(highs_1h[-3:]) + atr_1h * TREND_SL_ATR_MULT
                tp1  = price - atr_1h * TREND_TP1_ATR_MULT
                tp2  = price - atr_1h * TREND_TP2_ATR_MULT

            sl_dist = abs(price - sl)
            if sl_dist / price <= SL_MAX_PCT:
                score = 65
                score += 10 if rsi_ok and 45 <= rsi_1h <= 55 else 0  # RSI 越中性越好
                score += 5  if near_ema20 and pullback_dist < atr_1h * 0.3 else 0
                liq_b, liq_d = _liq_bonus(symbol, direction, price, atr_1h)
                fg_b,  fg_d  = _fear_greed_bonus(direction)
                score += liq_b + fg_b
                notional = calc_position_size(symbol, sl_dist, price, PAPER_CAPITAL, score)
                signals.append({
                    "symbol": symbol, "direction": direction, "mode": mode,
                    "entry_price": round(price, 6),
                    "sl": round(sl, 6), "tp1": round(tp1, 6), "tp2": round(tp2, 6),
                    "tp1_ratio": TREND_TP1_RATIO, "tp2_ratio": TREND_TP2_RATIO,
                    "notional": notional, "score": score,
                    "indicators": {
                        "rsi_1h": round(rsi_1h, 1),
                        "ema20": round(ema20, 4),
                        "pullback_pct": round(pullback_dist/price*100, 2),
                        "vol_ratio": round(vol_ratio, 2),
                        "atr_1h": round(atr_1h, 4),
                        "liq_detail": liq_d, "fg_detail": fg_d,
                    },
                    "market_info": minfo,
                })

    return signals


# ══════════════════════════════════════════════════════════════════════════════
# 主扫描入口
# ══════════════════════════════════════════════════════════════════════════════
def run_scan(paper_capital: float = 1000.0, dry_run: bool = False) -> dict:
    """
    主扫描函数，返回本轮信号。
    dry_run=True 时只返回信号，不触发任何执行。
    """
    from fx_major.universe import get_universe
    from fx_major.config import (
        KLINE_1H_LIMIT, KLINE_1D_LIMIT,
        MAX_POSITIONS_TOTAL, MAX_POSITIONS_LONG, MAX_POSITIONS_SHORT,
    )

    t0 = time.time()
    result = {"signals": [], "scanned": 0, "skipped": 0, "ts": t0}

    universe = get_universe()
    if not universe:
        logger.error("[scanner] 候选池为空，终止扫描")
        return result

    # ── 修复①：周末评分上限 ─────────────────────────────────────────────────
    from datetime import datetime, timezone as _tz
    _now_utc   = datetime.now(_tz.utc)
    _is_weekend = _now_utc.isoweekday() >= 6   # 6=周六 7=周日
    WEEKEND_SCORE_CAP = 75   # 周末流动性差，评分 cap 到 75（与 fx/scanner 对齐）
    if _is_weekend:
        logger.info(f"[scanner] 周末模式 ({_now_utc.strftime('%A')})，信号评分上限 {WEEKEND_SCORE_CAP}")

    # ── BTC 1H K线（大行情熔断 + 趋势判断）───────────────────────────────────
    _btc_trend = "neutral"   # up / down / neutral
    try:
        import urllib.request as _ur
        btc_1h = json.loads(_ur.urlopen(
            "https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval=1h&limit=24",
            timeout=8).read())
        if _check_btc_shock(btc_1h):
            logger.warning("[scanner] BTC 大行情熔断，跳过本轮")
            result["skipped_reason"] = "btc_shock"
            return result
        # BTC 趋势：用最近 24h 涨跌幅判断
        _btc_open  = float(btc_1h[0][1])
        _btc_close = float(btc_1h[-1][4])
        _btc_chg   = (_btc_close - _btc_open) / _btc_open
        if   _btc_chg >  0.015:  _btc_trend = "up"    # 24h 涨幅 > 1.5%
        elif _btc_chg < -0.015:  _btc_trend = "down"  # 24h 跌幅 > 1.5%
        logger.info(f"[scanner] BTC 24h 趋势={_btc_trend} chg={_btc_chg*100:.2f}%")
    except Exception as e:
        logger.warning(f"[scanner] BTC K线检查失败: {e}")

    # 拉取所有标的的 mark price（批量）
    mark_prices = {}
    try:
        import urllib.request as _ur
        mp_data = json.loads(_ur.urlopen(
            "https://fapi.binance.com/fapi/v1/premiumIndex", timeout=10).read())
        for item in mp_data:
            mark_prices[item["symbol"]] = float(item.get("markPrice", 0))
    except Exception as e:
        logger.warning(f"[scanner] 批量 mark price 失败: {e}")

    signals_all = []

    for symbol in universe:
        try:
            # 拉 K 线
            def _klines(interval, limit):
                url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval={interval}&limit={limit}"
                import urllib.request as _ur2
                return json.loads(_ur2.urlopen(url, timeout=10).read())

            k1h  = _klines("1h",  KLINE_1H_LIMIT)
            k1d  = _klines("1d",  KLINE_1D_LIMIT)
            k15m = _klines("15m", 50)

            mp = mark_prices.get(symbol, float(k1h[-1][4]))
            sigs = analyze_symbol(symbol, k1h, k15m, k1d, mp)
            # 将 BTC 趋势写入每个信号（供 paper_scanner 记录和复盘）
            for s in sigs:
                s["btc_trend"] = _btc_trend
                s["is_weekend"] = _is_weekend
            signals_all.extend(sigs)
            result["scanned"] += 1

        except Exception as e:
            logger.warning(f"[scanner] {symbol} 分析失败: {e}")
            result["skipped"] += 1
        finally:
            time.sleep(0.05)   # 轻微限速

    # ── 修复②：BTC 趋势协同过滤 + 周末评分上限 + 空头强制门槛 ─────────────────
    filtered = []
    rejected = []
    for sig in signals_all:
        direction = sig["direction"]
        score     = sig.get("score", 0)
        sym       = sig["symbol"]

        # A) 周末：评分超过上限的直接压到上限（不拒绝，降低仓位）
        if _is_weekend and score > WEEKEND_SCORE_CAP:
            sig["score"] = WEEKEND_SCORE_CAP
            sig["weekend_capped"] = True
            score = WEEKEND_SCORE_CAP

        # B) BTC 趋势协同：逆势信号拒绝
        #    btc_up   → 禁止开空（顺势只做多）
        #    btc_down → 禁止开多（顺势只做空）
        if _btc_trend == "up" and direction == "SHORT":
            rejected.append(f"{sym}(btc_up禁空)")
            continue
        if _btc_trend == "down" and direction == "LONG":
            rejected.append(f"{sym}(btc_down禁多)")
            continue

        # C) 空头强制门槛：空单评分必须 >= 70（比多单更严）
        if direction == "SHORT" and score < 70:
            rejected.append(f"{sym}(空头分数{score}<70)")
            continue

        filtered.append(sig)

    if rejected:
        logger.info(f"[scanner] 协同过滤拒绝 {len(rejected)} 个: {rejected[:5]}")

    # 按 score 排序
    filtered.sort(key=lambda x: -x.get("score", 0))
    result["signals"] = filtered
    result["rejected"] = rejected
    result["elapsed"] = round(time.time() - t0, 2)

    logger.info(
        f"[scanner] 完成 scanned={result['scanned']} "
        f"signals={len(signals_all)} elapsed={result['elapsed']}s"
    )
    return result
