"""
classify.py — 信号分类模块 (P1)

五路径：
  trend          趋势做多
  trend_short    趋势做空
  reversal       逆势做多 L2（高确定性超卖）
  reversal_l1    逆势做多 L1（中等超卖）
  reversal_short 逆势做空（超买反转）

输入：k1h / k4h 是 OHLCV kline 列表（每条 [ts,o,h,l,c,vol,...]）
      funding: float（资金费率，如 -0.001）
      chg24: float（24h 涨跌幅，如 -0.18 = -18%）
"""


def _ema(data: list[float], n: int) -> float:
    k = 2 / (n + 1)
    e = data[0]
    for x in data[1:]:
        e = x * k + e * (1 - k)
    return e


def _rsi(closes: list[float], n: int = 14) -> float:
    diffs = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    g = [max(d, 0) for d in diffs[-n:]]
    l = [abs(min(d, 0)) for d in diffs[-n:]]
    ag = sum(g) / n
    al = sum(l) / n
    return 100 - 100 / (1 + ag / al) if al > 0 else 50.0


def _ema_alignment(closes: list[float]) -> float:
    """
    EMA 排列分数 0~100
    100 = 强多排（EMA9 > EMA20 > EMA50），0 = 强空排
    """
    if len(closes) < 50:
        return 50.0
    e9  = _ema(closes[-50:], 9)
    e20 = _ema(closes[-50:], 20)
    e50 = _ema(closes[-50:], 50)
    if e9 > e20 > e50:
        return 90.0   # 强多排
    if e9 < e20 < e50:
        return 10.0   # 强空排
    if e9 > e20:
        return 65.0   # 弱多
    if e9 < e20:
        return 35.0   # 弱空
    return 50.0


def classify_signal(k1h: list, k4h: list, funding: float, chg24: float) -> str:
    """
    返回五路径之一：
      trend | trend_short | reversal | reversal_l1 | reversal_short
    """
    if len(k1h) < 30 or len(k4h) < 30:
        return "trend"   # 数据不足，默认趋势

    closes_1h = [float(k[4]) for k in k1h]
    closes_4h = [float(k[4]) for k in k4h]

    rsi_1h = _rsi(closes_1h)
    rsi_4h = _rsi(closes_4h)
    ema_score = _ema_alignment(closes_1h)

    # ── 逆势做多：超卖 ────────────────────────────────────────────────────────
    reversal_base = chg24 < -0.15 and funding < -0.001

    if reversal_base and rsi_4h < 10 and rsi_1h < 25:
        return "reversal"        # L2：极度超卖，高确定性

    if reversal_base and rsi_4h < 20 and rsi_1h < 35:
        return "reversal_l1"     # L1：中等超卖

    # ── 逆势做空：超买 ────────────────────────────────────────────────────────
    if chg24 > 0.20 and funding > 0.003 and rsi_4h > 75 and rsi_1h > 70:
        return "reversal_short"

    # ── 趋势做空 ──────────────────────────────────────────────────────────────
    if ema_score <= 20:
        return "trend_short"

    # ── 趋势做多（默认） ──────────────────────────────────────────────────────
    return "trend"
