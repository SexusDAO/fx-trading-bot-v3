"""
short_logic.py — 做空核心逻辑模块（独立于多头）

函数列表：
  calc_score_short(sym, k1h, k4h, k15m, ticker, funding) -> (score, details)
  classify_signal_short(k1h, k4h) -> 'breakdown' | 'reversal' | 'none'
  calc_entry_short(sym, direction, mark, k1h, k4h, details) -> (mode, limit_price, wait_seconds)

返回格式与 entry_logic.calc_entry_mode() 完全一致，方便 scanner 统一调用。

评分维度（满分 ~115）：
  EMA 空头排列  30分  （EMA9 < EMA20 < EMA50，多周期叠加）
  RSI 高位      20分  （>70 超买 +20，55-70 +10）
  顶背离        15分  （价格新高但 RSI 不创新高）
  资金费率过高  10分  （>0.05% 多头拥挤）
  24h 涨幅确认  10分  （跌幅 >3%，趋势确认方向）
  BB 上轨附近   10分  （bb_pct > 85）
  OI 背离       10分  （价涨 OI 跌）
  上方清算密集  10分  （Coinglass 位上方清算集中）
"""

import requests
import math
import sys

sys.path.insert(0, "/root/.openclaw/workspace")
import binance_api as api

BASE_F = "https://fapi.binance.com"


# ─────────────────────────────────────────────
# 基础指标工具（独立，不依赖 entry_logic）
# ─────────────────────────────────────────────

def _ema(data, n):
    """指数移动平均"""
    if not data:
        return 0.0
    k = 2 / (n + 1)
    e = data[0]
    for x in data[1:]:
        e = x * k + e * (1 - k)
    return e


def _rsi(closes, n=14):
    """RSI 计算"""
    if len(closes) < n + 1:
        return 50.0
    diffs = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    g = [max(d, 0) for d in diffs[-n:]]
    l = [abs(min(d, 0)) for d in diffs[-n:]]
    ag = sum(g) / n
    al = sum(l) / n
    return 100 - 100 / (1 + ag / al) if al > 0 else 50.0


def _atr(klines, n=14):
    """ATR 计算"""
    trs = []
    for i in range(1, len(klines)):
        h  = float(klines[i][2])
        lo = float(klines[i][3])
        pc = float(klines[i - 1][4])
        trs.append(max(h - lo, abs(h - pc), abs(lo - pc)))
    return sum(trs[-n:]) / n if trs else 0.0


def _bb(closes, n=20):
    """布林带，返回 (upper, lower, pct)"""
    if len(closes) < n:
        return closes[-1], closes[-1], 50.0
    sma   = sum(closes[-n:]) / n
    std   = (sum((x - sma) ** 2 for x in closes[-n:]) / n) ** 0.5
    upper = sma + 2 * std
    lower = sma - 2 * std
    pct   = (closes[-1] - lower) / (upper - lower) * 100 if (upper - lower) > 0 else 50.0
    return upper, lower, pct


# ─────────────────────────────────────────────
# OI 背离检查（做空专用：价涨 OI 跌）
# ─────────────────────────────────────────────

def _check_oi_bearish_divergence(symbol):
    """
    价格接近近期高点，但 OI 同期下降 > 15% → 空头信号。
    返回 (is_diverging: bool, oi_chg_pct: float)
    """
    try:
        oi_data = requests.get(
            f"{BASE_F}/futures/data/openInterestHist",
            params={"symbol": symbol, "period": "1h", "limit": 24},
            timeout=8,
        ).json()
        if not isinstance(oi_data, list) or len(oi_data) < 6:
            return False, 0.0

        oi_vals   = [float(x["sumOpenInterest"]) for x in oi_data]
        oi_peak   = max(oi_vals[-24:])
        oi_recent = oi_vals[-1]
        oi_chg    = (oi_recent - oi_peak) / (oi_peak + 1e-9)

        kl = requests.get(
            f"{BASE_F}/fapi/v1/klines",
            params={"symbol": symbol, "interval": "1h", "limit": 24},
            timeout=8,
        ).json()
        if not isinstance(kl, list):
            return False, 0.0

        prices        = [float(k[4]) for k in kl]
        price_now     = prices[-1]
        price_24h_high = max(prices)

        near_high  = price_now >= price_24h_high * 0.97   # 接近近期高点
        oi_dropped = oi_chg < -0.15                        # OI 下降 > 15%

        return (near_high and oi_dropped), round(oi_chg * 100, 2)
    except Exception:
        return False, 0.0


# ─────────────────────────────────────────────
# 上方清算密集检查
# ─────────────────────────────────────────────

def _check_liq_above(symbol, mark_price):
    """
    检查当前价格上方是否存在清算密集区（空头挤压区）。
    用最近 24h 1H K 线最高点范围 + OI 结构推断。
    返回 (has_liq_above: bool, liq_price_estimate: float)
    """
    try:
        kl = requests.get(
            f"{BASE_F}/fapi/v1/klines",
            params={"symbol": symbol, "interval": "1h", "limit": 48},
            timeout=8,
        ).json()
        if not isinstance(kl, list) or len(kl) < 6:
            return False, mark_price

        highs  = [float(k[2]) for k in kl[-24:]]
        recent_high = max(highs)

        # 若标记价已高于近24h高点 → 上方清算概率高
        if mark_price >= recent_high * 0.995:
            # 估算清算密集位：高于当前价 1~3%
            atr_est = sum(float(k[2]) - float(k[3]) for k in kl[-14:]) / 14
            liq_est = mark_price + atr_est * 1.5
            return True, round(liq_est, 8)

        return False, recent_high
    except Exception:
        return False, mark_price


# ─────────────────────────────────────────────
# 1. 做空评分
# ─────────────────────────────────────────────

def calc_score_short(sym, k1h, k4h, k15m, ticker, funding, token_report_score=0):
    """
    做空信号评分，满分 ~115（含聪明钱加分）。
    独立于多头逻辑。

    参数：
      sym       - 交易对符号，如 'BTCUSDT'
      k1h       - 1H K 线列表（每条：[open_time, o, h, l, c, v, ...]）
      k4h       - 4H K 线列表
      k15m      - 15m K 线列表（可为空，退回到 k1h）
      ticker    - REST /fapi/v1/ticker/24hr 响应字典
      funding   - 当前资金费率（float，如 0.001 = 0.1%）
      token_report_score - 聪明钱加减分（正负整数，由调用方传入）

    返回：(score: int, details: dict)

    评分维度：
      EMA 空头排列（EMA9 < EMA20 < EMA50）：最多 +30
        - 1H + 4H + 15m 三周期全空排：+30
        - 1H + 4H 双空排：+20
        - 仅 1H 空排：+10
      RSI 高位：
        - RSI_1H > 70（超买）：+20
        - RSI_1H 55-70：+10
      顶背离（价格新高但 RSI 不创新高）：+15
      资金费率过高（> 0.05% 多头拥挤）：+10
      24h 跌幅 > 3%（趋势确认）：+10
      布林带上轨附近（bb_pct > 85）：+10
      OI 背离（价涨 OI 跌）：+10
      上方清算密集：+10
      Token Report 加减分（聪明钱）：± token_report_score
    """
    c1h  = [float(k[4]) for k in k1h]
    c4h  = [float(k[4]) for k in k4h]
    c15m = [float(k[4]) for k in k15m] if k15m else c1h

    rsi_1h  = _rsi(c1h)
    rsi_4h  = _rsi(c4h)

    # EMA（空排用 9/20/50）
    ema9_1h   = _ema(c1h[-9:],   9)
    ema20_1h  = _ema(c1h[-20:], 20)
    ema50_1h  = _ema(c1h[-50:], 50) if len(c1h) >= 50 else _ema(c1h, len(c1h))

    ema9_4h   = _ema(c4h[-9:],   9) if len(c4h) >= 9  else _ema(c4h, len(c4h))
    ema20_4h  = _ema(c4h[-20:], 20) if len(c4h) >= 20 else _ema(c4h, len(c4h))
    ema50_4h  = _ema(c4h[-50:], 50) if len(c4h) >= 50 else _ema(c4h, len(c4h))

    ema9_15m  = _ema(c15m[-9:],   9) if len(c15m) >= 9  else _ema(c15m, len(c15m))
    ema20_15m = _ema(c15m[-20:], 20) if len(c15m) >= 20 else _ema(c15m, len(c15m))
    ema50_15m = _ema(c15m[-50:], 50) if len(c15m) >= 50 else _ema(c15m, len(c15m))

    bear_1h  = ema9_1h  < ema20_1h  < ema50_1h
    bear_4h  = ema9_4h  < ema20_4h  < ema50_4h
    bear_15m = ema9_15m < ema20_15m < ema50_15m

    # 布林带
    _, _, bb_pct = _bb(c1h)

    # 顶背离：近3根K线价格创新高，但 RSI 低于前期高点时的 RSI
    price_new_high = len(c1h) >= 10 and c1h[-1] >= max(c1h[-10:-1])
    rsi_prev       = _rsi(c1h[:-3]) if len(c1h) > 17 else rsi_1h
    top_divergence = price_new_high and rsi_1h < rsi_prev - 2  # 差值 > 2 才算有效背离

    chg24 = float(ticker.get("priceChangePercent", 0))
    mark  = float(ticker.get("lastPrice", c1h[-1]))

    # OI 背离
    oi_div, oi_chg = _check_oi_bearish_divergence(sym)

    # 上方清算密集
    liq_above, liq_above_price = _check_liq_above(sym, mark)

    score = 0

    # ── 1. EMA 空头排列（最多 30 分）──
    ema_score = 0
    if bear_4h and bear_1h and bear_15m:
        ema_score = 30
    elif bear_4h and bear_1h:
        ema_score = 20
    elif bear_1h:
        ema_score = 10
    score += ema_score

    # ── 2. RSI 高位（最多 20 分）──
    rsi_score = 0
    if rsi_1h > 70:
        rsi_score = 20
    elif rsi_1h >= 55:
        rsi_score = 10
    score += rsi_score

    # ── 3. 顶背离（15 分）──
    div_score = 15 if top_divergence else 0
    score += div_score

    # ── 4. 资金费率过高（10 分）──
    funding_pct = funding * 100  # 转为百分比
    fr_score = 10 if funding_pct > 0.05 else 0
    score += fr_score

    # ── 5. 24h 跌幅 > 3% 趋势确认（10 分）──
    trend_score = 10 if chg24 < -3 else 0
    score += trend_score

    # ── 6. 布林带上轨附近（10 分）──
    bb_score = 0
    if bb_pct > 95:
        bb_score = 10
    elif bb_pct > 85:
        bb_score = 6
    score += bb_score

    # ── 7. OI 背离（10 分）──
    oi_score = 10 if oi_div else 0
    score += oi_score

    # ── 8. 上方清算密集（10 分）──
    liq_score = 10 if liq_above else 0
    score += liq_score

    # ── 9. Token Report 加减分（聪明钱）──
    score += token_report_score

    details = {
        # EMA
        "ema_1h":       "空排" if bear_1h  else "非空排",
        "ema_4h":       "空排" if bear_4h  else "非空排",
        "ema_15m":      "空排" if bear_15m else "非空排",
        "ema_score":    ema_score,
        # RSI
        "rsi_1h":       round(rsi_1h, 2),
        "rsi_4h":       round(rsi_4h, 2),
        "rsi_score":    rsi_score,
        # 顶背离
        "top_divergence":   top_divergence,
        "price_new_high":   price_new_high,
        "rsi_prev":         round(rsi_prev, 2),
        "div_score":        div_score,
        # 资金费率
        "funding_pct":  round(funding_pct, 4),
        "fr_score":     fr_score,
        # 24h 变化
        "chg24":        chg24,
        "trend_score":  trend_score,
        # BB
        "bb_pct":       round(bb_pct, 2),
        "bb_score":     bb_score,
        # OI
        "oi_divergence":    oi_div,
        "oi_chg_pct":       oi_chg,
        "oi_score":         oi_score,
        # 上方清算
        "liq_above":        liq_above,
        "liq_above_price":  liq_above_price,
        "liq_score":        liq_score,
        # Token Report
        "token_report_score": token_report_score,
    }

    return score, details


# ─────────────────────────────────────────────
# 2. 做空信号分类
# ─────────────────────────────────────────────

def classify_signal_short(k1h, k4h):
    """
    做空信号分类。

    参数：
      k1h - 1H K 线列表
      k4h - 4H K 线列表

    返回：
      'breakdown'  — 跌破关键支撑，趋势性做空（EMA 空排 + 量能配合）
      'reversal'   — 高位顶部反转做空（RSI 高位超买 + 顶背离）
      'none'       — 无明确信号，不建议做空
    """
    c1h = [float(k[4]) for k in k1h]
    c4h = [float(k[4]) for k in k4h]
    l1h = [float(k[3]) for k in k1h]
    v1h = [float(k[5]) for k in k1h]

    rsi_1h = _rsi(c1h)
    rsi_4h = _rsi(c4h)

    # EMA
    ema9_1h  = _ema(c1h[-9:],   9)
    ema20_1h = _ema(c1h[-20:], 20)
    ema50_1h = _ema(c1h[-50:], 50) if len(c1h) >= 50 else _ema(c1h, len(c1h))
    ema9_4h  = _ema(c4h[-9:],   9) if len(c4h) >= 9  else _ema(c4h, len(c4h))
    ema20_4h = _ema(c4h[-20:], 20) if len(c4h) >= 20 else _ema(c4h, len(c4h))
    ema50_4h = _ema(c4h[-50:], 50) if len(c4h) >= 50 else _ema(c4h, len(c4h))

    bear_1h = ema9_1h  < ema20_1h  < ema50_1h
    bear_4h = ema9_4h  < ema20_4h  < ema50_4h

    # 量比（最近3根 vs 近17根均值）
    vb  = sum(v1h[-20:-3]) / 17 if len(v1h) >= 20 else sum(v1h) / max(len(v1h), 1)
    vr  = (sum(v1h[-3:]) / 3) / vb if vb > 0 else 1.0

    # 跌破支撑：最近2根K线有K收在近20根低点min下方
    support = min(l1h[-22:-2]) if len(l1h) >= 22 else min(l1h)
    broke_support = c1h[-1] < support or (len(c1h) >= 2 and c1h[-2] < support)

    # 顶背离
    price_new_high = len(c1h) >= 10 and c1h[-1] >= max(c1h[-10:-1])
    rsi_prev       = _rsi(c1h[:-3]) if len(c1h) > 17 else rsi_1h
    top_divergence = price_new_high and rsi_1h < rsi_prev - 2

    # ── 判断 breakdown ──
    # 跌破支撑 + EMA 至少 1H 空排 + 量能放大
    breakdown_conditions = sum([
        broke_support,
        bear_1h,
        bear_4h,
        vr >= 1.3,
        rsi_1h < 50,
    ])
    if breakdown_conditions >= 3:
        return "breakdown"

    # ── 判断 reversal ──
    # RSI 超买 + 顶背离，或 EMA 空排 + RSI 高位
    reversal_conditions = sum([
        rsi_1h > 70,
        rsi_4h > 65,
        top_divergence,
        (bear_1h and rsi_1h > 55),
    ])
    if reversal_conditions >= 2:
        return "reversal"

    return "none"


# ─────────────────────────────────────────────
# 3. 做空入场价计算
# ─────────────────────────────────────────────

def calc_entry_short(sym, direction, mark, k1h, k4h, details):
    """
    做空入场价计算，格式与 entry_logic.calc_entry_mode() 完全一致。

    参数：
      sym       - 交易对符号
      direction - 应为 'SHORT'（预留接口兼容性）
      mark      - 当前标记价（float）
      k1h       - 1H K 线列表
      k4h       - 4H K 线列表
      details   - calc_score_short() 返回的 details 字典

    返回：(mode, limit_price, wait_seconds)
      mode: 'CHASE' | 'PULLBACK' | 'WAIT'
        - CHASE   : 评分 ≥ 90，追空，在卖单墙下方或刚突破位入场
        - PULLBACK: 等反弹，在 1H MA25 附近或关键阻力位做空
        - WAIT    : 信号不足，观望

    wait_seconds（参考等待时长）：
      CHASE   → 3600  (1H)
      PULLBACK→ 14400 (4H)
      WAIT    → 0
    """
    c1h = [float(k[4]) for k in k1h]
    h1h = [float(k[2]) for k in k1h]
    l1h = [float(k[3]) for k in k1h]
    v1h = [float(k[5]) for k in k1h]

    atr = _atr(k1h)
    rsi_1h = _rsi(c1h)

    # MA25（1H）
    ma25_1h = sum(c1h[-25:]) / 25 if len(c1h) >= 25 else sum(c1h) / max(len(c1h), 1)

    # 量比
    vb  = sum(v1h[-10:-3]) / 7 if len(v1h) >= 10 else sum(v1h) / max(len(v1h), 1)
    vr  = (sum(v1h[-3:]) / 3) / vb if vb > 0 else 1.0

    # 关键阻力位（近12根1H高点最高值）
    resistance = max(h1h[-12:]) if len(h1h) >= 12 else max(h1h)

    # 卖单墙（从 details 取；若无则用近期高点估算）
    ob_resistance = details.get("liq_above_price", resistance)

    # ── 追空条件（CHASE）──
    # 需要：信号分类为 breakdown（跌破位）+ 量比 ≥ 1.3 + RSI 未超卖（> 35）
    signal = classify_signal_short(k1h, k4h)
    chase_ok = (
        signal == "breakdown"
        and vr >= 1.3
        and rsi_1h > 35
    )

    # ── 入场逻辑 ──
    # 先取 score 判断是否达到追空门槛
    # （调用方应在 score ≥ 90 时才传入此函数；函数内也做二次校验）
    # 从 details 反推总分（各子分加总）
    score_fields = [
        "ema_score", "rsi_score", "div_score", "fr_score",
        "trend_score", "bb_score", "oi_score", "liq_score",
        "token_report_score",
    ]
    total_score = sum(details.get(f, 0) for f in score_fields)

    if total_score >= 90 and chase_ok:
        # 追空：在卖单墙下方 ATR*0.05 入场，不低于现价*0.999
        limit_price = max(
            ob_resistance - atr * 0.05,
            mark * 0.999,
        )
        return "CHASE", round(limit_price, 8), 3600

    # PULLBACK：等反弹至 MA25 或阻力位做空
    # 取 MA25 和 resistance 中较低者（更保守），但不能低于现价 +0.5%
    pullback_target = min(ma25_1h, resistance - atr * 0.2)
    # 若目标价低于现价，则直接用现价 + ATR*0.3（等小幅反弹）
    if pullback_target <= mark:
        pullback_target = mark + atr * 0.3
    # 不能超过 resistance（防止挂得太高被扫）
    pullback_target = min(pullback_target, resistance)

    # 若信号为 none，直接 WAIT
    if signal == "none" and total_score < 75:
        return "WAIT", round(mark, 8), 0

    return "PULLBACK", round(pullback_target, 8), 14400
