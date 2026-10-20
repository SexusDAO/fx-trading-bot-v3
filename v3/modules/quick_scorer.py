"""
modules/quick_scorer.py — 极简评分系统 v1.0

设计原则：
  - 3层过滤（不是10层）：方向 + 结构 + RR
  - 门槛55分（不是70分）
  - 有结构支撑就入场，其余全是加分项
  - 入场价永远有兜底，不允许返回 None

核心函数：
  quick_score(k1h, k4h, mark_price, funding, bl, market_state)
      → (score, direction, signal_type)

  calc_entry_simple(direction, mark_price, k1h, bl)
      → (entry, sl, tp, source, rr)

原 scorer.py 的5维度评分逻辑已全部注释，保留在旧文件中作为参考。
原 scene_matcher.py 的 A/B/C/E 场景匹配已全部注释，保留在旧文件中作为参考。
"""

from __future__ import annotations
from modules.dynamic_baseline import DynamicBaseline, compute as compute_baseline


# ── 工具 ──────────────────────────────────────────────────────────────────────

def _c(k):  return [float(x[4]) for x in k]
def _h(k):  return [float(x[2]) for x in k]
def _l(k):  return [float(x[3]) for x in k]
def _v(k):  return [float(x[5]) for x in k]

def _ema(data: list, n: int) -> float:
    k = 2 / (n + 1); e = data[0]
    for x in data[1:]: e = x * k + e * (1 - k)
    return e

def _rsi(closes: list, n: int = 14) -> float:
    if len(closes) < n + 1: return 50.0
    g = [max(closes[i]-closes[i-1], 0) for i in range(1, len(closes))]
    l = [max(closes[i-1]-closes[i], 0) for i in range(1, len(closes))]
    ag = sum(g[-n:])/n; al = sum(l[-n:])/n
    return 100.0 if al == 0 else 100 - 100/(1 + ag/al)

def _mean(d): return sum(d)/len(d) if d else 0.0

def _find_support(k1h: list, mark_price: float, bl: DynamicBaseline) -> float | None:
    """
    找最近的支撑位（多头）：
    1. EMA20（最常用）
    2. 近20根最低点
    3. 近10根局部低点（swing low）
    返回离 mark_price 最近且在其下方的支撑价，无则返回 None。
    """
    closes = _c(k1h); lows = _l(k1h)
    candidates = []

    # EMA20
    if len(closes) >= 20:
        ema20 = _ema(closes[-20:], 20)
        if ema20 < mark_price:
            candidates.append(ema20)

    # EMA50
    if len(closes) >= 50:
        ema50 = _ema(closes[-50:], 50)
        if ema50 < mark_price:
            candidates.append(ema50)

    # 近20根最低点
    low20 = min(lows[-20:]) if len(lows) >= 20 else min(lows)
    if low20 < mark_price:
        candidates.append(low20)

    # swing low：近10根中，有两侧都比它高的低点
    if len(lows) >= 5:
        for i in range(-8, -2):
            idx = len(lows) + i
            if 0 < idx < len(lows) - 1:
                if lows[idx] < lows[idx-1] and lows[idx] < lows[idx+1]:
                    if lows[idx] < mark_price:
                        candidates.append(lows[idx])

    if not candidates:
        return None
    # 取离当前价最近的（最大的那个，因为都在下方）
    return max(candidates)


def _find_resistance(k1h: list, mark_price: float, bl: DynamicBaseline) -> float | None:
    """
    找最近的阻力位（空头）：EMA20/EMA50/近20根最高点/swing high
    返回离 mark_price 最近且在其上方的阻力价，无则返回 None。
    """
    closes = _c(k1h); highs = _h(k1h)
    candidates = []

    if len(closes) >= 20:
        ema20 = _ema(closes[-20:], 20)
        if ema20 > mark_price:
            candidates.append(ema20)

    if len(closes) >= 50:
        ema50 = _ema(closes[-50:], 50)
        if ema50 > mark_price:
            candidates.append(ema50)

    high20 = max(highs[-20:]) if len(highs) >= 20 else max(highs)
    if high20 > mark_price:
        candidates.append(high20)

    if len(highs) >= 5:
        for i in range(-8, -2):
            idx = len(highs) + i
            if 0 < idx < len(highs) - 1:
                if highs[idx] > highs[idx-1] and highs[idx] > highs[idx+1]:
                    if highs[idx] > mark_price:
                        candidates.append(highs[idx])

    if not candidates:
        return None
    return min(candidates)


def _get_direction(k1h: list, mark_price: float,
                   bl: DynamicBaseline) -> tuple[str, str]:
    """
    快速方向判断 + signal_type。
    返回 (direction, signal_type)
    direction: "LONG" | "SHORT" | "NONE"
    """
    closes = _c(k1h)
    if len(closes) < 20:
        return "NONE", "none"

    ema20 = _ema(closes[-20:], 20)
    ema50 = _ema(closes[-50:], 50) if len(closes) >= 50 else ema20
    rsi   = _rsi(closes)

    # 强势多头：价格 > EMA20 > EMA50
    if mark_price > ema20 and ema20 > ema50 * 0.998:
        return "LONG", "trend_long"

    # 强势空头：价格 < EMA20 < EMA50
    if mark_price < ema20 and ema20 < ema50 * 1.002:
        return "SHORT", "trend_short"

    # 超卖反弹：RSI < rsi_lower2 且价格接近支撑
    if rsi < bl.rsi_lower2 and mark_price < ema50:
        return "LONG", "reversal_long"

    # 超买反转：RSI > rsi_upper2 且价格接近阻力
    if rsi > bl.rsi_upper2 and mark_price > ema50:
        return "SHORT", "reversal_short"

    # 中间地带：用 EMA 斜率决定
    if ema20 > ema50:
        return "LONG", "trend_long"
    elif ema20 < ema50:
        return "SHORT", "trend_short"

    return "NONE", "none"


# ══════════════════════════════════════════════════════════════════════════════
# quick_score — 极简评分（3层核心 + 加分项）
# ══════════════════════════════════════════════════════════════════════════════

def quick_score(k1h: list, k4h: list,
                mark_price: float,
                funding: float,
                bl: DynamicBaseline,
                market_state: str = "") -> tuple[int, str, str]:
    """
    极简评分，返回 (score, direction, signal_type)。

    评分结构（满分约 90+）：
      核心层（决定能不能进）：
        技术结构支撑/阻力     +25    有 EMA/swing 支撑则给分
        方向顺市场状态         -20   逆势惩罚
      加分层（提高质量）：
        成交量异常放大         +15
        RSI 在合理区间         +10
        资金费率顺方向         +10
        BTC 趋势同向           +10
        ATR 适中（不过热）     +10
        近3根K线方向一致       +10

      旧的5维度评分已全部注释，保留在 scorer.py 中。
      旧的场景二次打分已全部注释，保留在 scene_matcher.py 中。
    """
    if len(k1h) < 15 or mark_price <= 0:
        return 0, "NONE", "none"

    closes = _c(k1h)
    highs  = _h(k1h)
    lows   = _l(k1h)
    vols   = _v(k1h)

    direction, signal_type = _get_direction(k1h, mark_price, bl)
    if direction == "NONE":
        return 0, "NONE", "none"

    score = 0

    # ── 层1：方向 × 市场状态 ────────────────────────────────────────────────
    if market_state in ("TRENDING_UP", "ACCUMULATION") and direction == "SHORT":
        score -= 20
    elif market_state == "TRENDING_DOWN" and direction == "LONG":
        score -= 20
    # RANGING / BREAKOUT_PENDING / HIGH_VOLATILITY: 不额外惩罚，让结构自己过滤

    # ── 层2：技术结构 ────────────────────────────────────────────────────────
    if direction == "LONG":
        struct = _find_support(k1h, mark_price, bl)
        if struct is not None:
            dist = mark_price - struct
            if dist <= bl.atr_baseline * 1.5:
                score += 25   # 贴近支撑，强结构
            elif dist <= bl.atr_baseline * 3.0:
                score += 15   # 距支撑1.5~3×ATR
            else:
                score += 8    # 支撑存在但偏远
        else:
            score += 0        # 无支撑：不加分也不扣分，让 RR 过滤
    else:  # SHORT
        struct = _find_resistance(k1h, mark_price, bl)
        if struct is not None:
            dist = struct - mark_price
            if dist <= bl.atr_baseline * 1.5:
                score += 25
            elif dist <= bl.atr_baseline * 3.0:
                score += 15
            else:
                score += 8
        else:
            score += 0

    # ── 加分项 ───────────────────────────────────────────────────────────────

    # 成交量异常（近3根均量 > vol_p75）
    avg3_vol = _mean(vols[-3:]) if len(vols) >= 3 else vols[-1]
    if avg3_vol > bl.vol_p75:
        score += 15
    elif avg3_vol > bl.vol_baseline:
        score += 8

    # RSI 在合理区间
    rsi_val = _rsi(closes)
    if direction == "LONG":
        if bl.rsi_lower1 <= rsi_val <= bl.rsi_mean:
            score += 10   # 回调到均值附近
        elif rsi_val < bl.rsi_lower1:
            score += 6    # 超卖但可能继续跌
    else:
        if bl.rsi_mean <= rsi_val <= bl.rsi_upper1:
            score += 10
        elif rsi_val > bl.rsi_upper1:
            score += 6

    # 资金费率顺方向（正费率 → 做多不利，做空有利）
    if direction == "LONG" and funding < -0.0001:
        score += 10   # 负费率，空头付费，多头占优
    elif direction == "LONG" and funding < 0.0003:
        score += 5    # 费率适中
    elif direction == "SHORT" and funding > 0.0003:
        score += 10
    elif direction == "SHORT" and funding > 0.0001:
        score += 5

    # 近3根K线方向一致
    if len(closes) >= 4:
        last3 = [(closes[-i] - closes[-i-1]) for i in range(1, 4)]
        if direction == "LONG" and all(x > 0 for x in last3):
            score += 10
        elif direction == "SHORT" and all(x < 0 for x in last3):
            score += 10
        elif direction == "LONG" and sum(1 for x in last3 if x > 0) >= 2:
            score += 5
        elif direction == "SHORT" and sum(1 for x in last3 if x < 0) >= 2:
            score += 5

    # ATR 适中（不处于极度高波动）
    from modules.dynamic_baseline import _atr_series as _atrs, _mean as _mn
    atrs = _atrs(k1h)
    if atrs:
        cur_atr = _mn(atrs[-3:]) if len(atrs) >= 3 else atrs[-1]
        win = atrs[-60:] if len(atrs) >= 60 else atrs
        atr_pct = sum(1 for a in win if a <= cur_atr) / len(win) * 100
        if atr_pct < 70:
            score += 10   # 波动正常
        elif atr_pct < 85:
            score += 5    # 波动偏高但可接受

    return score, direction, signal_type


# ══════════════════════════════════════════════════════════════════════════════
# calc_entry_simple — 极简入场三要素计算
# ══════════════════════════════════════════════════════════════════════════════

def calc_entry_simple(direction: str,
                      mark_price: float,
                      k1h: list,
                      bl: DynamicBaseline) -> tuple[float, float, float, str, float]:
    """
    计算 (entry, sl, tp, source, rr)。
    永远有兜底，不返回 None。

    优先级：技术结构 > 当前价折价/溢价
    止盈：swing high/low，没有则 1.5×ATR
    RR = abs(tp - entry) / abs(entry - sl)

    原 scene_matcher.py 里的 A/B/C/E 场景入场计算已全部注释，
    本函数是其精简替代版。
    """
    highs  = _h(k1h)
    lows   = _l(k1h)
    atr    = bl.atr_baseline

    MIN_SL_DIST = 0.3 * atr   # SL 最小距离（防止 SL=entry）

    if direction == "LONG":
        support = _find_support(k1h, mark_price, bl)

        if support is not None and (mark_price - support) <= atr * 2.5:
            # 有支撑：挂在支撑上方 0.1×ATR
            entry = round(support + atr * 0.1, 8)
            sl    = round(support - atr * 0.3, 8)
            source = "structure"
        else:
            # 兜底：当前价小幅折价（0.2%）
            entry  = round(mark_price * 0.998, 8)
            sl     = round(mark_price - max(atr * 1.5, mark_price * 0.015), 8)
            source = "current_price"

        # 保证 SL 最小距离
        if abs(entry - sl) < MIN_SL_DIST:
            sl = round(entry - MIN_SL_DIST, 8)

        # 止盈：swing high，没有则 1.5×ATR
        swing_high = max(highs[-20:]) if len(highs) >= 20 else max(highs)
        risk = abs(entry - sl)
        tp_swing = swing_high if swing_high > entry else round(entry + atr * 1.5, 8)
        tp_atr   = round(entry + atr * 1.5, 8)
        # 取 swing 和 ATR 中较近的（更容易触发）
        tp_candidate = min(tp_swing, tp_atr) if tp_swing > entry else tp_atr
        # 保证 RR ≥ 1.2
        tp = max(tp_candidate, round(entry + risk * 1.2, 8))

    else:  # SHORT
        resistance = _find_resistance(k1h, mark_price, bl)

        if resistance is not None and (resistance - mark_price) <= atr * 2.5:
            entry  = round(resistance - atr * 0.1, 8)
            sl     = round(resistance + atr * 0.3, 8)
            source = "structure"
        else:
            entry  = round(mark_price * 1.002, 8)
            sl     = round(mark_price + max(atr * 1.5, mark_price * 0.015), 8)
            source = "current_price"

        if abs(entry - sl) < MIN_SL_DIST:
            sl = round(entry + MIN_SL_DIST, 8)

        swing_low = min(lows[-20:]) if len(lows) >= 20 else min(lows)
        risk = abs(entry - sl)
        tp_swing = swing_low if swing_low < entry else round(entry - atr * 1.5, 8)
        tp_atr   = round(entry - atr * 1.5, 8)
        tp_candidate = max(tp_swing, tp_atr) if tp_swing < entry else tp_atr
        tp = min(tp_candidate, round(entry - risk * 1.2, 8))

    risk   = abs(entry - sl)
    reward = abs(tp - entry)
    rr     = round(reward / risk, 2) if risk > 0 else 0.0

    return entry, sl, tp, source, rr
