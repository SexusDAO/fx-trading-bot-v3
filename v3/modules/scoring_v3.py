"""
scoring_v3.py — 统一评分引擎 (P1)

支持三路径：
  calc_score_trend(k1h, k4h, funding, token_report=0, smart_money=0) → (score, details)
  calc_score_reversal(k1h, k4h, funding, chg24, token_report=0, smart_money=0) → (score, details)
  calc_score_trend_short(k1h, k4h, funding, token_report=0, smart_money=0) → (score, details)

所有函数返回归一化 0~100 分，details 是每个维度的原始分。
"""

import math


# ── 技术指标工具 ─────────────────────────────────────────────────────────────

def _closes(klines: list) -> list[float]:
    return [float(k[4]) for k in klines]

def _highs(klines: list) -> list[float]:
    return [float(k[2]) for k in klines]

def _lows(klines: list) -> list[float]:
    return [float(k[3]) for k in klines]

def _vols(klines: list) -> list[float]:
    return [float(k[5]) for k in klines]


def ema(data: list[float], n: int) -> float:
    if len(data) < n:
        return data[-1] if data else 0
    k = 2 / (n + 1)
    e = data[0]
    for x in data[1:]:
        e = x * k + e * (1 - k)
    return e


def rsi(closes: list[float], n: int = 14) -> float:
    if len(closes) < n + 1:
        return 50.0
    diffs = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    g = [max(d, 0) for d in diffs[-n:]]
    l = [abs(min(d, 0)) for d in diffs[-n:]]
    ag = sum(g) / n
    al = sum(l) / n
    if al == 0:
        return 100.0 if ag > 0 else 50.0  # 全部上涨→RSI=100
    return 100 - 100 / (1 + ag / al)


def atr(klines: list, n: int = 14) -> float:
    trs = []
    for i in range(1, len(klines)):
        h = float(klines[i][2]); lo = float(klines[i][3]); pc = float(klines[i-1][4])
        trs.append(max(h - lo, abs(h - pc), abs(lo - pc)))
    return sum(trs[-n:]) / n if trs else 0


def vol_ratio(klines: list, n: int = 10) -> float:
    """当前成交量 / 前 n 根均量"""
    vols = _vols(klines)
    if len(vols) < n + 1:
        return 1.0
    avg = sum(vols[-n-1:-1]) / n
    return vols[-1] / avg if avg > 0 else 1.0


def ema_alignment(closes: list[float]) -> float:
    """EMA 多排程度 0~100（100=强多，0=强空）"""
    if len(closes) < 50:
        return 50.0
    e9  = ema(closes[-50:], 9)
    e20 = ema(closes[-50:], 20)
    e50 = ema(closes[-50:], 50)
    e99 = ema(closes, 99) if len(closes) >= 99 else e50

    score = 50.0
    if e9 > e20:   score += 15
    if e20 > e50:  score += 15
    if e50 > e99:  score += 10
    if e9 < e20:   score -= 15
    if e20 < e50:  score -= 15
    if e50 < e99:  score -= 10
    return max(0.0, min(100.0, score))


def bb_pct(closes: list[float], n: int = 20) -> float:
    """布林带 %B：0=下轨，1=上轨"""
    if len(closes) < n:
        return 0.5
    window = closes[-n:]
    mid = sum(window) / n
    std = math.sqrt(sum((x - mid)**2 for x in window) / n)
    if std == 0:
        return 0.5
    upper = mid + 2 * std
    lower = mid - 2 * std
    c = closes[-1]
    return (c - lower) / (upper - lower) if upper != lower else 0.5


def detect_hammer(k1h: list) -> bool:
    """最近一根1H K线是否出现锤头/下影针"""
    if len(k1h) < 1:
        return False
    k = k1h[-1]
    o, h, lo, c = float(k[1]), float(k[2]), float(k[3]), float(k[4])
    body  = abs(c - o)
    lower = min(o, c) - lo
    return lower > body * 2 and body > 0


def detect_divergence_bullish(k1h: list) -> bool:
    """简化底背离：价格新低但 RSI 未新低"""
    if len(k1h) < 30:
        return False
    closes = _closes(k1h[-30:])
    lows_  = _lows(k1h[-30:])
    mid = len(closes) // 2
    price_new_low = min(lows_[mid:]) < min(lows_[:mid])
    rsi_new_low   = rsi(closes[mid:]) < rsi(closes[:mid]) - 5
    return price_new_low and not rsi_new_low


# ── 评分函数 ──────────────────────────────────────────────────────────────────

def calc_score_trend(k1h, k4h, funding: float,
                     token_report: int = 0,
                     smart_money: int = 0) -> tuple[int, dict]:
    """
    趋势多单评分（满分 ~115，归一化到 100）
    返回 (score_0_100, details_dict)
    """
    d = {}

    c1h = _closes(k1h);  c4h = _closes(k4h)
    r1  = rsi(c1h);      r4  = rsi(c4h)
    ea  = ema_alignment(c1h)

    # 1. EMA 多周期趋势 (0~30)
    if ea >= 80:   d["ema"] = 30
    elif ea >= 65: d["ema"] = 22
    elif ea >= 50: d["ema"] = 15
    elif ea >= 35: d["ema"] = 8
    else:          d["ema"] = 0

    # 2. RSI 位置 (0~25)，RSI 超买扣分
    if 50 <= r1 <= 65:   d["rsi"] = 15
    elif 40 <= r1 < 50:  d["rsi"] = 10
    elif r1 < 40:        d["rsi"] = 8
    elif 65 < r1 <= 70:  d["rsi"] = 8
    elif r1 > 75:        d["rsi"] = -20
    else:                d["rsi"] = -15      # 70~75

    # 3. 量能 (0~20)
    vr = vol_ratio(k1h)
    if vr >= 1.5:    d["vol"] = 20
    elif vr >= 1.2:  d["vol"] = 12
    elif vr >= 1.0:  d["vol"] = 8
    else:            d["vol"] = 0

    # 4. 结构/情绪 (0~40)
    struct = 0
    bb = bb_pct(c1h)
    if bb > 0.55:   struct += 10
    if detect_hammer(k1h): struct += 7
    if funding < -0.001:  struct += 5
    if token_report > 0:  struct += min(token_report, 15)
    if token_report < 0:  struct += max(token_report, -20)
    if smart_money > 0:   struct += min(smart_money, 10)
    d["struct"] = max(-20, min(40, struct))

    raw = sum(d.values())
    # 归一化：满分 115 → 100
    score = max(0, min(100, round(raw / 115 * 100)))
    return score, d


def calc_score_reversal(k1h, k4h, funding: float, chg24: float,
                        token_report: int = 0,
                        smart_money: int = 0) -> tuple[int, dict]:
    """逆势多单评分（满分 ~115，归一化 100）"""
    d = {}

    c1h = _closes(k1h); c4h = _closes(k4h)
    r1  = rsi(c1h);     r4  = rsi(c4h)

    # 1. 超卖深度 (0~35)
    if r4 < 10:        d["oversold"] = 20
    elif r4 < 15:      d["oversold"] = 15
    elif r4 < 25:      d["oversold"] = 8
    else:              d["oversold"] = 0
    if r1 < 30:        d["oversold"] += 8
    elif r1 < 40:      d["oversold"] += 3
    d["oversold"] = min(d["oversold"], 35)

    # 2. 资金费率 (0~25)
    if funding < -0.005:   d["funding"] = 15
    elif funding < -0.003: d["funding"] = 10
    elif funding < -0.001: d["funding"] = 5
    else:                  d["funding"] = 0

    # 3. 反弹动能 (0~35)
    rebound = 0
    if detect_hammer(k1h):                rebound += 10
    if vol_ratio(k1h) >= 1.2:             rebound += 6
    if detect_divergence_bullish(k1h):    rebound += 4
    if token_report > 0:  rebound += min(token_report, 15)
    if token_report < 0:  rebound += max(token_report, -20)
    if smart_money > 0:   rebound += min(smart_money, 10)
    d["rebound"] = max(-20, min(35, rebound))

    # 4. 跌幅 (0~20)
    drop = abs(chg24)
    if drop >= 0.20:   d["drop"] = 12
    elif drop >= 0.15: d["drop"] = 8
    elif drop >= 0.10: d["drop"] = 4
    else:              d["drop"] = 0

    raw = sum(d.values())
    score = max(0, min(100, round(raw / 115 * 100)))
    return score, d


def calc_score_trend_short(k1h, k4h, funding: float,
                           token_report: int = 0,
                           smart_money: int = 0) -> tuple[int, dict]:
    """趋势空单评分（满分 ~100，归一化）"""
    d = {}

    c1h = _closes(k1h); c4h = _closes(k4h)
    r1  = rsi(c1h);     r4  = rsi(c4h)
    ea  = ema_alignment(c1h)

    # EMA 空排 (0~30)
    if ea <= 20:        d["ema"] = 30
    elif ea <= 35:      d["ema"] = 22
    elif ea <= 50:      d["ema"] = 8
    else:               d["ema"] = 0

    # RSI（做空时偏高更好）(0~25)
    if 55 <= r1 <= 70:  d["rsi"] = 22
    elif r1 > 75:       d["rsi"] = 20
    elif 40 <= r1 < 55: d["rsi"] = 12
    elif r1 < 30:       d["rsi"] = -15
    else:               d["rsi"] = 8

    # 量能 (0~20)
    vr = vol_ratio(k1h)
    if vr >= 1.5:   d["vol"] = 20
    elif vr >= 1.2: d["vol"] = 12
    elif vr >= 1.0: d["vol"] = 8
    else:           d["vol"] = 0

    # 结构/情绪 (0~25)
    struct = 0
    if funding > 0.003:  struct += 5
    if token_report < 0: struct += min(abs(token_report), 10)
    if smart_money < 0:  struct += min(abs(smart_money), 10)
    d["struct"] = min(25, struct)

    raw = sum(d.values())
    score = max(0, min(100, round(raw / 100 * 100)))
    return score, d


# ── 统一入口 ──────────────────────────────────────────────────────────────────

def calc_score(signal_type: str, k1h, k4h, funding: float,
               chg24: float = 0.0,
               token_report: int = 0,
               smart_money: int = 0) -> tuple[int, dict]:
    """
    统一入口，根据 signal_type 路由到对应评分函数。
    signal_type: trend | trend_short | reversal | reversal_l1 | reversal_short
    """
    if signal_type in ("reversal", "reversal_l1"):
        return calc_score_reversal(k1h, k4h, funding, chg24, token_report, smart_money)
    if signal_type in ("trend_short", "reversal_short"):
        return calc_score_trend_short(k1h, k4h, funding, token_report, smart_money)
    return calc_score_trend(k1h, k4h, funding, token_report, smart_money)
