# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  ⚠️  LEGACY — scorer.py 旧版5维度评分系统（已被 quick_scorer.py 替代）   ║
# ║                                                                          ║
# ║  本文件保留作为参考，主流程已改用 quick_scorer.quick_score()。            ║
# ║  如需恢复旧评分，在 sim_week_v*.py 里重新 import score from scorer。     ║
# ║  旧逻辑：5维度（趋势/成交量/波动率/结构/市场同步）× 震荡/强势赛道         ║
# ║  新逻辑：3层过滤（方向×市场状态 / 技术结构 / RR） + 加分项               ║
# ╚══════════════════════════════════════════════════════════════════════════╝
"""
modules/scorer.py — 初筛评分系统 v3.1（动态基准版 - 修复版）

所有阈值均基于 DynamicBaseline，不使用全市场固定值。

两套赛道：
  震荡赛道（oscillation）— 默认
  强势赛道（momentum）   — 当日涨幅 > strong_threshold 且 放量 > baseline+2σ

修复内容 (v3.1)：
  1. 强势赛道支持多空双向（连续暴跌 + 放量 → 强势空头）
  2. 4H 周期冲突直接 DISCARD（不是扣 4 分）
  3. RSI 极端值在维度 5 惩罚（>85 或<15 扣分）
  4. 强势赛道触发条件改为"且"（不是"或"）

满分均为 100，5 个维度独立打分，输出结构化 JSON。
"""

from __future__ import annotations
from typing import TypedDict
from modules.dynamic_baseline import (
    DynamicBaseline, compute as compute_baseline,
)


# ── 工具 ──────────────────────────────────────────────────────────────────────

def _closes(k): return [float(x[4]) for x in k]
def _highs(k):  return [float(x[2]) for x in k]
def _lows(k):   return [float(x[3]) for x in k]
def _vols(k):   return [float(x[5]) for x in k]
def _opens(k):  return [float(x[1]) for x in k]

def _ema(data: list[float], n: int) -> float:
    k = 2 / (n + 1); e = data[0]
    for x in data[1:]: e = x * k + e * (1 - k)
    return e

def _rsi(closes: list[float], n: int = 14) -> float:
    if len(closes) < n + 1: return 50.0
    gains  = [max(closes[i] - closes[i-1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i-1] - closes[i], 0) for i in range(1, len(closes))]
    ag = sum(gains[-n:]) / n; al = sum(losses[-n:]) / n
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)

def _mean(data): return sum(data) / len(data) if data else 0.0


# ── 类型定义 ──────────────────────────────────────────────────────────────────

class DimScore(TypedDict):
    score:  int
    max:    int
    reason: str

class ScoreResult(TypedDict):
    total:          int
    track:          str          # "momentum" | "oscillation"
    trend_clarity:  DimScore     # 维度 1
    volume_health:  DimScore     # 维度 2
    volatility_fit: DimScore     # 维度 3
    structure:      DimScore     # 维度 4
    mkt_sync:       DimScore     # 维度 5
    direction:      str          # "LONG" | "SHORT" | "NONE" | "DISCARD"
    signal_type:    str          # "trend_long"|"trend_short"|"reversal_long"|...


# ══════════════════════════════════════════════════════════════════════════════
# 震荡赛道（Oscillation Track）
# ══════════════════════════════════════════════════════════════════════════════

def _osc_trend_clarity(k1h: list, k4h: list, bl: DynamicBaseline,
                       chg24: float) -> tuple[DimScore, str]:
    """
    维度 1：趋势方向明确性（25 分）
    看高低点结构是否单调，不看绝对涨跌幅。
    
    修复：4H 与 1H 周期冲突 → 返回 DISCARD
    """
    closes = _closes(k1h)
    highs  = _highs(k1h)
    lows   = _lows(k1h)

    # EMA 排列
    ema20 = _ema(closes[-20:], 20) if len(closes) >= 20 else closes[-1]
    ema50 = _ema(closes[-50:], 50) if len(closes) >= 50 else ema20
    ema99 = _ema(closes[-99:], 99) if len(closes) >= 99 else ema50

    # EMA 斜率
    if len(closes) >= 25:
        ema20_prev = _ema(closes[-25:-5], 20)
        ema20_slope = (ema20 - ema20_prev) / max(abs(ema20_prev), 1e-9)
    else:
        ema20_slope = 0.0

    long_aligned  = ema20 > ema50 and ema50 > ema99
    short_aligned = ema20 < ema50 and ema50 < ema99

    # 高低点结构
    n_half = max(1, len(highs[-20:]) // 2)
    hw = highs[-20:]
    lw = lows[-20:]
    hh = max(hw[-n_half:]) > max(hw[:n_half])
    hl = min(lw[-n_half:]) > min(lw[:n_half])
    lh = max(hw[-n_half:]) < max(hw[:n_half])
    ll = min(lw[-n_half:]) < min(lw[:n_half])

    # 4H 确认
    if k4h and len(k4h) >= 20:
        c4h = _closes(k4h)
        ema4h_20 = _ema(c4h[-20:], 20)
        trend_4h = "up" if c4h[-1] > ema4h_20 else "down"
    else:
        trend_4h = "neutral"

    score = 0; reasons = []; direction = "NONE"

    if long_aligned and ema20_slope > -0.002:
        score += 12; reasons.append(f"EMA 多头排列 (+12)")
        if hh and hl: score += 7; reasons.append("HH+HL(+7)")
        elif hh or hl: score += 3; reasons.append("部分多头结构 (+3)")
        if trend_4h == "up":
            score += 4; reasons.append("4H 顺势 (+4)")
        elif trend_4h == "down":
            # 4H 与 1H 冲突 → DISCARD
            return DimScore(score=0, max=25, reason="4H 周期冲突，DISCARD"), "DISCARD"
        else:
            score += 1; reasons.append("4H 中性 (+1)")
        direction = "LONG"
    elif short_aligned and ema20_slope < 0.002:
        score += 12; reasons.append(f"EMA 空头排列 (+12)")
        if lh and ll: score += 7; reasons.append("LH+LL(+7)")
        elif lh or ll: score += 3; reasons.append("部分空头结构 (+3)")
        if trend_4h == "down":
            score += 4; reasons.append("4H 顺势 (+4)")
        elif trend_4h == "up":
            # 4H 与 1H 冲突 → DISCARD
            return DimScore(score=0, max=25, reason="4H 周期冲突，DISCARD"), "DISCARD"
        else:
            score += 1; reasons.append("4H 中性 (+1)")
        direction = "SHORT"
    elif ema20 > ema50:
        score += 6; reasons.append("EMA20>50，排列未完全 (+6)"); direction = "LONG"
    elif ema20 < ema50:
        score += 6; reasons.append("EMA20<50，排列未完全 (+6)"); direction = "SHORT"
    else:
        score += 2; reasons.append("EMA 粘合 (+2)")

    # 24h 与方向一致
    if direction == "LONG"  and chg24 > 0.01: score += 2; reasons.append("24h 顺多 (+2)")
    if direction == "SHORT" and chg24 < -0.01: score += 2; reasons.append("24h 顺空 (+2)")

    score = min(score, 25)
    return DimScore(score=score, max=25, reason=" | ".join(reasons)), direction


def _osc_volume_health(k1h: list, bl: DynamicBaseline, direction: str) -> DimScore:
    """
    维度 2：成交量健康度（20 分）
    基于 vol_baseline ± σ，不用固定比例。
    """
    vols   = _vols(k1h)
    closes = _closes(k1h)
    if len(vols) < 5:
        return DimScore(score=8, max=20, reason="数据不足 (8)")

    last_vol  = vols[-1]
    avg_5     = _mean(vols[-6:-1]) if len(vols) >= 6 else vols[-1]
    price_up  = closes[-1] > closes[-2] if len(closes) >= 2 else True

    vol_high  = bl.vol_baseline + bl.vol_std
    vol_low   = max(bl.vol_baseline - bl.vol_std, bl.vol_baseline * 0.3)
    vol_spike = bl.vol_baseline + 2 * bl.vol_std

    score = 0; reasons = []

    if last_vol > vol_spike:
        score += 3; reasons.append(f"暴量{last_vol/bl.vol_baseline:.1f}x，警惕 (+3)")
    elif direction == "LONG":
        if price_up and last_vol >= vol_high:
            score += 14; reasons.append("价涨量增 (+14)")
        elif price_up and vol_low <= last_vol < vol_high:
            score += 9;  reasons.append("价涨量平稳 (+9)")
        elif not price_up and last_vol < vol_low:
            score += 12; reasons.append("回调缩量健康 (+12)")
        elif not price_up and last_vol >= vol_high:
            score += 2;  reasons.append("价跌量增 (+2)")
        else:
            score += 6;  reasons.append("量能一般 (+6)")
    else:  # SHORT
        if not price_up and last_vol >= vol_high:
            score += 14; reasons.append("价跌量增 (+14)")
        elif not price_up and vol_low <= last_vol < vol_high:
            score += 9;  reasons.append("价跌量平稳 (+9)")
        elif price_up and last_vol < vol_low:
            score += 12; reasons.append("反弹缩量，空头健康 (+12)")
        else:
            score += 6;  reasons.append("量能一般 (+6)")

    if len(vols) >= 6:
        recent = vols[-5:]
        shrink_cnt = sum(1 for v in recent if v < bl.vol_baseline * 0.7)
        if shrink_cnt >= 3:
            score += 4; reasons.append(f"持续缩量{shrink_cnt}根 (+4)")
        elif shrink_cnt >= 2:
            score += 2; reasons.append(f"缩量{shrink_cnt}根 (+2)")

    return DimScore(score=min(score, 20), max=20, reason=" | ".join(reasons))


def _osc_volatility_fit(k1h: list, bl: DynamicBaseline, mark_price: float) -> DimScore:
    """维度 3：波动率适中性（20 分）"""
    from modules.dynamic_baseline import _atr_series, _percentile
    trs = _atr_series(k1h[-21:]) if len(k1h) >= 2 else [bl.atr_baseline]
    cur_atr = _mean(trs[-3:]) if trs else bl.atr_baseline

    cur_pct = sum(1 for t in trs if t <= cur_atr) / max(len(trs), 1) * 100

    score = 0; reasons = []

    if 30 <= cur_pct <= 75:
        score += 16; reasons.append(f"ATR 在{cur_pct:.0f}百分位，适中 (+16)")
    elif 20 <= cur_pct < 30:
        score += 10; reasons.append(f"ATR 在{cur_pct:.0f}百分位，偏低 (+10)")
    elif 75 < cur_pct <= 90:
        score += 8;  reasons.append(f"ATR 在{cur_pct:.0f}百分位，偏高 (+8)")
    elif cur_pct < 20:
        score += 3;  reasons.append(f"ATR 在{cur_pct:.0f}百分位，死气 (+3)")
    else:
        score += 2;  reasons.append(f"ATR 在{cur_pct:.0f}百分位，过高 (+2)")

    if cur_atr <= bl.atr_p40:
        score += 4; reasons.append("ATR 收窄蓄势 (+4)")
    elif cur_atr <= bl.atr_p70:
        score += 2; reasons.append("ATR 适中 (+2)")

    return DimScore(score=min(score, 20), max=20, reason=" | ".join(reasons))


def _osc_structure(k1h: list, bl: DynamicBaseline, direction: str) -> DimScore:
    """维度 4：近期 K 线结构（20 分）"""
    closes = _closes(k1h)
    highs  = _highs(k1h)
    lows   = _lows(k1h)

    if len(k1h) < 8:
        return DimScore(score=8, max=20, reason="数据不足 (8)")

    score = 0; reasons = []

    n = min(10, len(highs))
    n_h = max(1, n // 2)
    rh = highs[-n:]; rl = lows[-n:]
    hh = max(rh[-n_h:]) > max(rh[:n_h])
    hl = min(rl[-n_h:]) > min(rl[:n_h])
    lh = max(rh[-n_h:]) < max(rh[:n_h])
    ll = min(rl[-n_h:]) < min(rl[:n_h])

    if direction == "LONG":
        if hh and hl: score += 12; reasons.append("HH+HL 多头结构 (+12)")
        elif hl:      score += 7;  reasons.append("HL 支撑 (+7)")
        elif hh:      score += 4;  reasons.append("新高低点未跟上 (+4)")
        elif ll:      score += 0;  reasons.append("创新低，结构破坏 (0)")
        else:         score += 3;  reasons.append("中性结构 (+3)")
    else:
        if lh and ll: score += 12; reasons.append("LH+LL 空头结构 (+12)")
        elif lh:      score += 7;  reasons.append("LH 压力 (+7)")
        elif ll:      score += 4;  reasons.append("新低高点未跟下 (+4)")
        elif hh:      score += 0;  reasons.append("创新高，结构破坏 (0)")
        else:         score += 3;  reasons.append("中性结构 (+3)")

    last = k1h[-1]
    o, h, lo, c = float(last[1]), float(last[2]), float(last[3]), float(last[4])
    body  = abs(c - o)
    uw    = h - max(o, c)
    lw    = min(o, c) - lo
    atr = bl.atr_baseline

    if direction == "LONG":
        if lw >= body * 2 and uw <= atr * 0.3 and body > 0:
            score += 7; reasons.append("锤子线 (+7)")
        elif c > o and body / (h-lo if h>lo else 1) > 0.6:
            score += 5; reasons.append("强阳线 (+5)")
        elif c > o:
            score += 2; reasons.append("阳线 (+2)")
    else:
        if uw >= body * 2 and lw <= atr * 0.3 and body > 0:
            score += 7; reasons.append("射击之星 (+7)")
        elif c < o and body / (h-lo if h>lo else 1) > 0.6:
            score += 5; reasons.append("强阴线 (+5)")
        elif c < o:
            score += 2; reasons.append("阴线 (+2)")

    return DimScore(score=min(score, 20), max=20, reason=" | ".join(reasons))


def _osc_mkt_sync(bl: DynamicBaseline, funding: float, chg24: float,
                  direction: str, btc_trend: str = "neutral",
                  rsi_val: float = 50.0,
                  prev_funding=None) -> DimScore:
    """
    维度 5：市场情绪同步性（15 分）
    
    修复：RSI 极端值惩罚（>85 或<15 扣分）
    """
    score = 0; reasons = []

    # BTC 趋势（5 分）
    if btc_trend == "up" and direction == "LONG":
        score += 5; reasons.append("BTC 顺多 (+5)")
    elif btc_trend == "down" and direction == "SHORT":
        score += 5; reasons.append("BTC 顺空 (+5)")
    elif btc_trend == "neutral":
        score += 2; reasons.append("BTC 中性 (+2)")
    else:
        reasons.append("逆 BTC(+0)")

    # 资金费率（6 分 + v2.0 回归信号额外最多 +3 分）
    # v2.0 新增：资金费率快速回归均值 → 说明极端仓位在平仓 → 逆势时机加分
    funding_reversion_bonus = 0
    if prev_funding is not None:
        delta = funding - prev_funding
        # 多头逻辑：费率极高(>0.003) 且正在快速下降（多头在平仓）→ 适合做空/空头时机成熟
        if direction == "LONG" and prev_funding > 0.003 and delta < -0.0005:
            funding_reversion_bonus = 3
            reasons.append(f"费率强势回归{prev_funding:.4f}→{funding:.4f}，多单时机(+3)")
        # 空头逻辑：费率极低(<-0.003) 且正在快速回升（空头在平仓）→ 适合做多/多头时机成熟
        elif direction == "SHORT" and prev_funding < -0.003 and delta > 0.0005:
            funding_reversion_bonus = 3
            reasons.append(f"费率强势回归{prev_funding:.4f}→{funding:.4f}，空单时机(+3)")
    score += funding_reversion_bonus

    if direction == "LONG":
        if -0.001 <= funding <= 0.001:
            score += 6; reasons.append(f"费率中性{funding:.4f}(+6)")
        elif funding < -0.001:
            score += 4; reasons.append(f"负费率利多{funding:.4f}(+4)")
        elif funding <= 0.003:
            score += 2; reasons.append(f"费率偏高{funding:.4f}(+2)")
        else:
            reasons.append(f"费率过高{funding:.4f}(+0)")
    else:
        if -0.001 <= funding <= 0.001:
            score += 6; reasons.append(f"费率中性{funding:.4f}(+6)")
        elif funding > 0.001:
            score += 4; reasons.append(f"正费率利空{funding:.4f}(+4)")
        elif funding >= -0.003:
            score += 2; reasons.append(f"费率偏低{funding:.4f}(+2)")
        else:
            reasons.append(f"费率过低{funding:.4f}(+0)")

    # 24h 涨跌（4 分）
    if direction == "LONG" and 0.01 < chg24 < 0.20:
        score += 4; reasons.append(f"24h 温和上涨{chg24:.1%}(+4)")
    elif direction == "SHORT" and -0.20 < chg24 < -0.01:
        score += 4; reasons.append(f"24h 温和下跌{chg24:.1%}(+4)")
    elif direction == "LONG" and chg24 >= 0.20:
        score += 1; reasons.append(f"24h 过热{chg24:.1%}(+1)")
    elif direction == "SHORT" and chg24 <= -0.20:
        score += 1; reasons.append(f"24h 过度下跌{chg24:.1%}(+1)")
    else:
        score += 2; reasons.append(f"24h 方向弱 (+2)")

    # RSI 极端值惩罚（修复）
    if direction == "LONG" and rsi_val > 85:
        penalty = min(10, int((rsi_val - 85) / 2))
        score -= penalty
        reasons.append(f"RSI 超买{rsi_val:.0f} 惩罚-{penalty}")
    elif direction == "SHORT" and rsi_val < 15:
        penalty = min(10, int((15 - rsi_val) / 2))
        score -= penalty
        reasons.append(f"RSI 超卖{rsi_val:.0f} 惩罚-{penalty}")

    return DimScore(score=max(0, min(score, 15)), max=15, reason=" | ".join(reasons))


# ══════════════════════════════════════════════════════════════════════════════
# 强势赛道（Momentum Track）- 修复版
# ══════════════════════════════════════════════════════════════════════════════

def _is_momentum_track(current_price: float, prev_close: float,
                       bl: DynamicBaseline, current_vol: float,
                       k1h: list | None = None) -> tuple[bool, str]:
    """
    判断是否走强势赛道（v3.3 累积型强势扩展）。

    触发路径 A（爆发型）：当根量价齐升/跌
      vol_spike AND gain >= strong_threshold
    触发路径 B（累积型）：EMA 偏离 > strong_threshold AND ATR 活跃
      适用于已经涨/跌了很多、当前根不爆量的持续强势标的（如 KOMA 这波）

    返回：(is_momentum, direction)
    """
    gain = (current_price - prev_close) / prev_close if prev_close > 0 else 0.0
    vol_spike = current_vol > (bl.vol_baseline + 2 * bl.vol_std)

    # 路径A：爆发型（原逻辑）
    if vol_spike and gain >= bl.strong_threshold:
        return True, "LONG"
    elif vol_spike and gain <= -bl.strong_threshold:
        return True, "SHORT"

    # 路径B：累积型强势（新增）
    # EMA20 偏离 > strong_threshold 且 ATR 高于历史中位（有实际波动支撑）
    if k1h and len(k1h) >= 20:
        from modules.dynamic_baseline import _closes as _c, _ema as _e, _atr_series as _as, _mean as _m
        closes = _c(k1h)
        ema20   = _e(closes[-20:], 20)
        ema_gap = (current_price - ema20) / ema20 if ema20 > 0 else 0.0
        if abs(ema_gap) >= bl.strong_threshold:
            atrs = _as(k1h)
            if len(atrs) >= 10:
                atr_p50 = sorted(atrs)[len(atrs) // 2]
                cur_atr = _m(atrs[-3:]) if len(atrs) >= 3 else atrs[-1]
                atr_p25 = sorted(atrs)[len(atrs) // 4]  # 25百分位，只排除真正萎缩
                if cur_atr >= atr_p25:  # 回踩期ATR略低也允许，不萎缩即可
                    return True, ("LONG" if ema_gap > 0 else "SHORT")
    
    return False, "NONE"


def _mom_momentum(k1h: list, bl: DynamicBaseline, direction: str) -> DimScore:
    """维度 1：动能持续性（25 分）- 支持多空双向"""
    closes = _closes(k1h)
    highs  = _highs(k1h)
    opens  = _opens(k1h)
    n = min(8, len(k1h))
    recent = k1h[-n:]

    # 连续阳线/阴线数
    if direction == "LONG":
        streak = 0
        for bar in reversed(recent):
            if float(bar[4]) > float(bar[1]): streak += 1
            else: break
    else:  # SHORT
        streak = 0
        for bar in reversed(recent):
            if float(bar[4]) < float(bar[1]): streak += 1
            else: break

    ema20 = _ema(closes[-20:], 20) if len(closes) >= 20 else closes[-1]
    ema_aligned = (closes[-1] > ema20) if direction == "LONG" else (closes[-1] < ema20)

    score = 0; reasons = []
    if streak >= 4:   score += 15; reasons.append(f"连续{streak}根{'阳线' if direction=='LONG' else '阴线'}(+15)")
    elif streak == 3: score += 10; reasons.append(f"连续 3 根{'阳线' if direction=='LONG' else '阴线'}(+10)")
    elif streak == 2: score += 6;  reasons.append(f"连续 2 根{'阳线' if direction=='LONG' else '阴线'}(+6)")
    else:             score += 2;  reasons.append(f"{'阳线' if direction=='LONG' else '阴线'}不足 (+2)")

    if ema_aligned: score += 7; reasons.append("均线顺向 (+7)")
    else:           score += 2; reasons.append("均线未顺 (+2)")

    if len(k1h) >= 5:
        avg5 = _mean(closes[-5:])
        if direction == "LONG":
            min_low3 = min(float(k1h[-i][3]) for i in range(1, 4))
            if min_low3 >= avg5 * 0.95:
                score += 3; reasons.append("无大幅回撤 (+3)")
        else:
            max_high3 = max(float(k1h[-i][2]) for i in range(1, 4))
            if max_high3 <= avg5 * 1.05:
                score += 3; reasons.append("无反弹 (+3)")

    return DimScore(score=min(score, 25), max=25, reason=" | ".join(reasons))


def _mom_volume_accel(k1h: list, bl: DynamicBaseline) -> DimScore:
    """维度 2：成交量加速度（20 分）"""
    vols = _vols(k1h)
    if len(vols) < 5:
        return DimScore(score=8, max=20, reason="数据不足 (8)")

    last_vol   = vols[-1]
    spike_thr  = bl.vol_baseline + 2 * bl.vol_std
    score = 0; reasons = []

    if last_vol >= spike_thr:
        score += 14; reasons.append(f"放量{last_vol/bl.vol_baseline:.1f}x(+14)")
    elif last_vol >= bl.vol_baseline * 1.5:
        score += 9;  reasons.append(f"量增 1.5x(+9)")
    elif last_vol >= bl.vol_baseline:
        score += 5;  reasons.append(f"量平稳 (+5)")
    else:
        score += 2;  reasons.append(f"量偏低 (+2)")

    if len(vols) >= 4:
        if vols[-1] > vols[-2] > vols[-3]:
            score += 6; reasons.append("量能递增 (+6)")
        elif vols[-1] > vols[-2]:
            score += 3; reasons.append("量在增加 (+3)")

    return DimScore(score=min(score, 20), max=20, reason=" | ".join(reasons))


def _mom_pullback_ctrl(k1h: list, bl: DynamicBaseline, direction: str) -> DimScore:
    """维度 3：回调/反弹幅度控制（20 分）"""
    closes = _closes(k1h)
    highs  = _highs(k1h)
    lows   = _lows(k1h)
    if len(closes) < 5:
        return DimScore(score=10, max=20, reason="数据不足 (10)")

    if direction == "LONG":
        peak  = max(highs[-20:]) if len(highs) >= 20 else max(highs)
        base  = min(lows[-20:]) if len(lows) >= 20 else min(lows)
        rise  = peak - base
        cur   = closes[-1]
        pb_of_rise = (peak - cur) / rise if rise > 0 else 0
    else:  # SHORT
        base  = min(lows[-20:]) if len(lows) >= 20 else min(lows)
        peak  = max(highs[-20:]) if len(highs) >= 20 else max(highs)
        fall  = peak - base
        cur   = closes[-1]
        pb_of_rise = (cur - base) / fall if fall > 0 else 0

    score = 0; reasons = []
    if pb_of_rise <= 0.20:
        score += 20; reasons.append(f"回调{pb_of_rise:.0%}，极强 (+20)")
    elif pb_of_rise <= 0.38:
        score += 15; reasons.append(f"回调{pb_of_rise:.0%}≤38%，黄金区 (+15)")
    elif pb_of_rise <= 0.50:
        score += 8;  reasons.append(f"回调{pb_of_rise:.0%}≤50%，偏深 (+8)")
    else:
        score += 2;  reasons.append(f"回调{pb_of_rise:.0%}>50%，动能弱 (+2)")

    return DimScore(score=min(score, 20), max=20, reason=" | ".join(reasons))


def _mom_candle_quality(k1h: list, bl: DynamicBaseline, direction: str) -> DimScore:
    """维度 4：强势 K 线特征（20 分）"""
    if not k1h:
        return DimScore(score=8, max=20, reason="数据不足 (8)")

    last = k1h[-1]
    o, h, lo, c = float(last[1]), float(last[2]), float(last[3]), float(last[4])
    body     = abs(c - o)
    uw       = h - max(o, c)
    lw       = min(o, c) - lo
    rng      = h - lo if h > lo else 1e-9

    score = 0; reasons = []
    if direction == "LONG":
        close_pct = (c - lo) / rng
        if close_pct >= 0.80:
            score += 12; reasons.append(f"收盘在上 80%({close_pct:.0%})(+12)")
        elif close_pct >= 0.65:
            score += 8;  reasons.append(f"收盘在上 65%({close_pct:.0%})(+8)")
        elif close_pct >= 0.50:
            score += 4;  reasons.append(f"收盘中位 ({close_pct:.0%})(+4)")
        else:
            score += 0;  reasons.append(f"收盘在下 ({close_pct:.0%})(+0)")

        if uw < bl.atr_baseline * 0.20:
            score += 8; reasons.append("上影短，无卖压 (+8)")
        elif uw < bl.atr_baseline * 0.50:
            score += 4; reasons.append("上影适中 (+4)")
        else:
            reasons.append("上影长，卖压重 (+0)")
    else:  # SHORT
        close_pct = (h - c) / rng
        if close_pct >= 0.80:
            score += 12; reasons.append(f"收盘在下 80%({close_pct:.0%})(+12)")
        elif close_pct >= 0.65:
            score += 8;  reasons.append(f"收盘在下 65%({close_pct:.0%})(+8)")
        elif close_pct >= 0.50:
            score += 4;  reasons.append(f"收盘中位 ({close_pct:.0%})(+4)")
        else:
            score += 0;  reasons.append(f"收盘在上 ({close_pct:.0%})(+0)")

        if lw < bl.atr_baseline * 0.20:
            score += 8; reasons.append("下影短，无买盘 (+8)")
        elif lw < bl.atr_baseline * 0.50:
            score += 4; reasons.append("下影适中 (+4)")
        else:
            reasons.append("下影长，买盘重 (+0)")

    return DimScore(score=min(score, 20), max=20, reason=" | ".join(reasons))


def _mom_mkt_sync(bl: DynamicBaseline, funding: float, chg24: float,
                  btc_trend: str, direction: str,
                  rsi_val: float = 50.0) -> DimScore:
    """
    维度 5：资金集中度（15 分）- 支持多空双向
    
    修复：RSI 极端值惩罚
    """
    score = 0; reasons = []

    # BTC 趋势（6 分）
    if direction == "LONG":
        if btc_trend == "up":
            score += 6; reasons.append("BTC 顺多 (+6)")
        elif btc_trend == "neutral":
            score += 3; reasons.append("BTC 中性 (+3)")
        else:
            score += 0; reasons.append("BTC 逆向 (+0)")
    else:  # SHORT
        if btc_trend == "down":
            score += 6; reasons.append("BTC 顺空 (+6)")
        elif btc_trend == "neutral":
            score += 3; reasons.append("BTC 中性 (+3)")
        else:
            score += 0; reasons.append("BTC 逆向 (+0)")

    # 资金费率（5 分）
    if direction == "LONG":
        if 0.0 < funding <= 0.003:
            score += 5; reasons.append(f"正费率{funding:.4f}(+5)")
        elif -0.001 <= funding <= 0.0:
            score += 3; reasons.append(f"费率近零{funding:.4f}(+3)")
        else:
            score += 1; reasons.append(f"费率异常{funding:.4f}(+1)")
    else:  # SHORT
        if -0.003 <= funding < 0.0:
            score += 5; reasons.append(f"负费率{funding:.4f}(+5)")
        elif 0.0 <= funding <= 0.001:
            score += 3; reasons.append(f"费率近零{funding:.4f}(+3)")
        else:
            score += 1; reasons.append(f"费率异常{funding:.4f}(+1)")

    # 24h 涨跌幅（4 分）
    if direction == "LONG":
        if chg24 > bl.strong_threshold:
            score += 4; reasons.append(f"24h{chg24:.1%}超强势 (+4)")
        elif chg24 > 0.05:
            score += 2; reasons.append(f"24h{chg24:.1%}强 (+2)")
    else:  # SHORT
        if chg24 < -bl.strong_threshold:
            score += 4; reasons.append(f"24h{chg24:.1%}超弱势 (+4)")
        elif chg24 < -0.05:
            score += 2; reasons.append(f"24h{chg24:.1%}弱 (+2)")

    # RSI 极端值惩罚（修复）
    if direction == "LONG" and rsi_val > 85:
        penalty = min(10, int((rsi_val - 85) / 2))
        score -= penalty
        reasons.append(f"RSI 超买{rsi_val:.0f} 惩罚-{penalty}")
    elif direction == "SHORT" and rsi_val < 15:
        penalty = min(10, int((15 - rsi_val) / 2))
        score -= penalty
        reasons.append(f"RSI 超卖{rsi_val:.0f} 惩罚-{penalty}")

    return DimScore(score=max(0, min(score, 15)), max=15, reason=" | ".join(reasons))


# ══════════════════════════════════════════════════════════════════════════════
# 主接口 - 修复版
# ══════════════════════════════════════════════════════════════════════════════

def score(k1h: list, k4h: list,
          mark_price: float,
          funding: float,
          chg24: float,
          btc_trend: str = "neutral",
          baseline: DynamicBaseline | None = None,
          prev_funding: float | None = None) -> ScoreResult:  # v2.0: 上一期资金费率（用于回归信号）
    """
    对单标的进行初筛评分（v3.2）。

    修复内容：
      1. 强势赛道支持多空双向
      2. 4H 周期冲突直接 DISCARD
      3. RSI 极端值惩罚
      4. 强势赛道触发条件改为"且"
    v2.0 新增：
      5. 资金费率快速回归均值信号（prev_funding 可选传入）
    """
    if len(k1h) < 10:
        return ScoreResult(
            total=0, track="oscillation",
            trend_clarity=DimScore(score=0, max=25, reason="数据不足"),
            volume_health=DimScore(score=0, max=20, reason="数据不足"),
            volatility_fit=DimScore(score=0, max=20, reason="数据不足"),
            structure=DimScore(score=0, max=20, reason="数据不足"),
            mkt_sync=DimScore(score=0, max=15, reason="数据不足"),
            direction="NONE", signal_type="none",
        )

    bl = baseline or compute_baseline(k1h)

    # 判断赛道（修复：支持多空双向）
    prev_close = float(k1h[-2][4]) if len(k1h) >= 2 else mark_price
    cur_vol    = float(k1h[-1][5])
    is_momentum, mom_direction = _is_momentum_track(mark_price, prev_close, bl, cur_vol, k1h)
    
    rsi_val = _rsi(_closes(k1h))

    if is_momentum:
        track = "momentum"
        direction = mom_direction  # 可能是 LONG 或 SHORT
        
        dim1 = _mom_momentum(k1h, bl, direction)
        dim2 = _mom_volume_accel(k1h, bl)
        dim3 = _mom_pullback_ctrl(k1h, bl, direction)
        dim4 = _mom_candle_quality(k1h, bl, direction)
        dim5 = _mom_mkt_sync(bl, funding, chg24, btc_trend, direction, rsi_val)
    else:
        track = "oscillation"
        dim1, direction = _osc_trend_clarity(k1h, k4h, bl, chg24)
        
        # 检查是否 DISCARD（4H 周期冲突）
        if direction == "DISCARD":
            return ScoreResult(
                total=0, track="oscillation",
                trend_clarity=dim1,
                volume_health=DimScore(score=0, max=20, reason="周期冲突"),
                volatility_fit=DimScore(score=0, max=20, reason="周期冲突"),
                structure=DimScore(score=0, max=20, reason="周期冲突"),
                mkt_sync=DimScore(score=0, max=15, reason="周期冲突"),
                direction="DISCARD", signal_type="none",
            )
        
        dim2 = _osc_volume_health(k1h, bl, direction)
        dim3 = _osc_volatility_fit(k1h, bl, mark_price)
        dim4 = _osc_structure(k1h, bl, direction)
        dim5 = _osc_mkt_sync(bl, funding, chg24, direction, btc_trend, rsi_val, prev_funding)

    if direction == "NONE":
        total = dim1["score"]
        return ScoreResult(
            total=total, track=track,
            trend_clarity=dim1,
            volume_health=DimScore(score=0, max=20, reason="方向不明"),
            volatility_fit=DimScore(score=0, max=20, reason="方向不明"),
            structure=DimScore(score=0, max=20, reason="方向不明"),
            mkt_sync=DimScore(score=0, max=15, reason="方向不明"),
            direction="NONE", signal_type="none",
        )

    total = dim1["score"] + dim2["score"] + dim3["score"] + dim4["score"] + dim5["score"]

    # 信号类型判定
    if direction == "LONG":
        sig = "reversal_long" if rsi_val < bl.rsi_mean - bl.rsi_std else "trend_long"
    else:
        sig = "reversal_short" if rsi_val > bl.rsi_mean + bl.rsi_std else "trend_short"

    return ScoreResult(
        total=total, track=track,
        trend_clarity=dim1,
        volume_health=dim2,
        volatility_fit=dim3,
        structure=dim4,
        mkt_sync=dim5,
        direction=direction,
        signal_type=sig,
    )
