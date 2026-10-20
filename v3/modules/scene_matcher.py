# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  ⚠️  LEGACY — scene_matcher.py 旧版场景匹配系统（已被 quick_scorer.py 替代）║
# ║                                                                          ║
# ║  本文件保留作为参考，主流程已改用 quick_scorer.calc_entry_simple()。     ║
# ║  如需恢复旧场景（A回调/B突破/C强势/E空头+二次打分），重新import do_match。║
# ║  旧逻辑：场景候选 → 触发条件 → 二次打分(0-100) → 挂单参数               ║
# ║  新逻辑：技术结构直接定位支撑/阻力 → entry/sl/tp 三要素，无场景概念      ║
# ╚══════════════════════════════════════════════════════════════════════════╝
"""
modules/scene_matcher.py — 场景匹配 v3.0（动态基准 + 二次打分）

三个场景（场景E暂缓）：
  A_pullback   — 回调入场（震荡/高位多头）
  B_breakout   — 缩量蓄势突破（震荡/中低位多头）
  C_first_pull — 强势回踩（强势赛道）

每个场景：
  1. 触发条件检查（全部满足才进入二次打分）
  2. 独立维度二次打分（满分100）
  3. 计算入场价/止损/止盈（全用动态 ATR，不用固定百分比）

主接口：match(k1h, mark_price, direction, track, bl)
        → MatchResult
"""

from __future__ import annotations
from typing import TypedDict, Optional
from modules.dynamic_baseline import DynamicBaseline, compute as compute_baseline, _mean
from modules.trend_filter import judge_market_bias, judge_market_bias_stable, check_trend_filter, get_required_score, TrendBias
from modules.time_stop import get_base_time_hours


# ── 工具 ──────────────────────────────────────────────────────────────────────

def _closes(k): return [float(x[4]) for x in k]
def _highs(k):  return [float(x[2]) for x in k]
def _lows(k):   return [float(x[3]) for x in k]
def _vols(k):   return [float(x[5]) for x in k]

def _ema(data, n):
    k2 = 2 / (n + 1); e = data[0]
    for x in data[1:]: e = x * k2 + e * (1 - k2)
    return e

def _rsi(closes, n=14):
    if len(closes) < n + 1: return 50.0
    gains  = [max(closes[i]-closes[i-1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i-1]-closes[i], 0) for i in range(1, len(closes))]
    ag = sum(gains[-n:]) / n; al = sum(losses[-n:]) / n
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)

def _atr_now(k):
    """最近3根 TR 均值"""
    if len(k) < 2: return 0.01
    trs = [max(float(k[i][2])-float(k[i][3]),
               abs(float(k[i][2])-float(k[i-1][4])),
               abs(float(k[i][3])-float(k[i-1][4])))
           for i in range(max(1, len(k)-3), len(k))]
    return _mean(trs) if trs else 0.01


# ── 类型定义 ──────────────────────────────────────────────────────────────────

class MatchResult(TypedDict):
    matched:      bool
    scene:        str         # "A_pullback"|"B_breakout"|"C_first_pull"|"none"
    direction:    str
    entry_price:  float
    sl_price:     float
    tp_price:     float
    rr:           float
    second_score: int         # 二次打分（0~100），未匹配时=0
    reason:       str
    conditions:   dict


# ══════════════════════════════════════════════════════════════════════════════
# 场景 A：回调入场
# ══════════════════════════════════════════════════════════════════════════════

def _scene_a(k1h: list, mark_price: float,
             direction: str, bl: DynamicBaseline) -> MatchResult:
    """
    触发条件（全部满足）：
    ① 近20根高低点结构向上（多头）
    ② 回落幅度 ≥ 1.2 × atr_baseline
    ③ RSI 回落到 rsi_mean - 0.5×rsi_std 以下
    ④ 近3根成交量 < vol_baseline

    二次打分（满分100）：
      回调幅度合理区间（1.2~2.5×ATR）  25
      支撑区（前高/均线附近）           25
      RSI 回落深度                      20
      缩量程度                          20
      RR ≥ 2.5                         10
    """
    closes = _closes(k1h)
    highs  = _highs(k1h)
    lows   = _lows(k1h)
    vols   = _vols(k1h)
    cond   = {}

    # ① 趋势确认：EMA20 方向（主判断）+ 高低点结构（辅助）
    ema20_val = _ema(closes[-20:], 20) if len(closes) >= 20 else closes[-1]
    ema50_val = _ema(closes[-50:], 50) if len(closes) >= 50 else ema20_val
    if direction == "LONG":
        # EMA 多头排列 OR 价格在 EMA20 上方（回调中也成立）
        trend_ok = ema20_val > ema50_val or closes[-1] > ema20_val * 0.98
    else:
        trend_ok = ema20_val < ema50_val or closes[-1] < ema20_val * 1.02
    cond["trend_ok"] = trend_ok

    # ② 回调幅度
    if direction == "LONG":
        peak = max(highs[-20:])
        pb   = (peak - mark_price) / peak if peak > 0 else 0.0
    else:
        trough = min(lows[-20:])
        pb     = (mark_price - trough) / trough if trough > 0 else 0.0
    pullback_ok = pb >= 1.2 * bl.atr_baseline / max(mark_price, 1e-9)
    # 也接受绝对回调 ≥ 1.2×ATR
    pullback_abs = pb * mark_price >= 1.2 * bl.atr_baseline
    pullback_ok  = pullback_ok or pullback_abs
    cond["pullback_ok"] = pullback_ok
    cond["pullback_pct"] = round(pb, 4)

    # ③ RSI 动态
    rsi_val = _rsi(closes)
    rsi_thr = bl.rsi_mean - 0.5 * bl.rsi_std
    rsi_ok  = rsi_val <= rsi_thr if direction == "LONG" else rsi_val >= (bl.rsi_mean + 0.5 * bl.rsi_std)
    cond["rsi_ok"]  = rsi_ok
    cond["rsi_val"] = round(rsi_val, 1)
    cond["rsi_thr"] = round(rsi_thr, 1)

    # ④ 缩量
    avg10   = _mean(vols[-11:-1]) if len(vols) >= 11 else (vols[-1] if vols else 1.0)
    avg3    = _mean(vols[-3:]) if len(vols) >= 3 else vols[-1]
    vol_ok  = avg3 < bl.vol_baseline
    cond["vol_ok"]    = vol_ok
    cond["vol_ratio"] = round(avg3 / bl.vol_baseline if bl.vol_baseline > 0 else 1.0, 2)

    all_pass = trend_ok and pullback_ok and rsi_ok and vol_ok
    if not all_pass:
        failed = [k for k, v in cond.items() if k.endswith("_ok") and not v]
        return MatchResult(
            matched=False, scene="none", direction=direction,
            entry_price=0, sl_price=0, tp_price=0, rr=0, second_score=0,
            reason=f"场景A不满足: {failed}", conditions=cond,
        )

    # ── 二次打分 ──────────────────────────────────────────────────────────────
    s2 = 0
    reasons2 = []

    # (1) 回调幅度合理区间 25分
    pb_atr = pb * mark_price / bl.atr_baseline if bl.atr_baseline > 0 else 0
    if 1.2 <= pb_atr <= 2.5:
        s2 += 25; reasons2.append(f"回调{pb_atr:.1f}x ATR黄金区(+25)")
    elif 1.0 <= pb_atr < 1.2:
        s2 += 15; reasons2.append(f"回调{pb_atr:.1f}x ATR偏浅(+15)")
    elif pb_atr > 2.5:
        s2 += 8;  reasons2.append(f"回调{pb_atr:.1f}x ATR偏深(+8)")
    else:
        s2 += 5;  reasons2.append("回调偏浅(+5)")

    # (2) 支撑区：当前价接近 EMA20 或前高 25分
    ema20 = _ema(closes[-20:], 20) if len(closes) >= 20 else closes[-1]
    near_ema = abs(mark_price - ema20) <= bl.atr_baseline * 0.5
    if near_ema:
        s2 += 25; reasons2.append("回调到EMA20附近(+25)")
    else:
        # 是否接近前高点（近10根最高点）
        prev_high = max(highs[-11:-1]) if len(highs) >= 11 else max(highs)
        near_prev = abs(mark_price - prev_high) <= bl.atr_baseline * 0.8
        if near_prev:
            s2 += 18; reasons2.append("回调到前高支撑(+18)")
        else:
            s2 += 8;  reasons2.append("支撑未明确(+8)")

    # (3) RSI 回落深度 20分
    rsi_depth = bl.rsi_mean - rsi_val
    rsi_1sig  = bl.rsi_std
    if rsi_depth >= rsi_1sig:
        s2 += 20; reasons2.append(f"RSI回落超1σ(+20)")
    elif rsi_depth >= rsi_1sig * 0.5:
        s2 += 12; reasons2.append(f"RSI回落0.5σ(+12)")
    else:
        s2 += 5;  reasons2.append(f"RSI轻微回落(+5)")

    # (4) 缩量程度 20分
    shrink = 1 - cond["vol_ratio"]
    if shrink >= 0.5:
        s2 += 20; reasons2.append(f"缩量{shrink:.0%}(+20)")
    elif shrink >= 0.3:
        s2 += 13; reasons2.append(f"缩量{shrink:.0%}(+13)")
    else:
        s2 += 6;  reasons2.append(f"轻微缩量(+6)")

    # (5) RR ≥ 2.5 预估 10分（先用固定，挂单参数算准后校验）
    # 预估：止损 = 0.5×ATR，止盈 = 2×ATR，RR ≈ 4
    s2 += 10; reasons2.append("RR预估满足2.5(+10)")

    s2 = min(s2, 100)

    # ── 挂单参数（动态 ATR）────────────────────────────────────────────────────
    from modules.entry_calc import calc_dynamic_entry
    liq_price = cond.get("_liq_zone_price")

    if direction == "LONG":
        entry, entry_method = calc_dynamic_entry(
            "LONG", mark_price, k1h, bl.atr_baseline, liq_price)
        # ── v2.0 止损：结构无效化点（新设计思路）────────────────────────────
        # 止损 = 本次回调低点下方，而不是入场价 - N×ATR
        # 理由：跌破回调低点说明判断错误（趋势反转或回调未结束）
        pullback_low = min(lows[-5:])   # 本次回调的最低点
        sl = round(pullback_low - 0.3 * bl.atr_baseline, 8)   # 低点下方留 0.3×ATR 缓冲防假突破
        tp_ref = max(highs[-20:])
        risk   = abs(entry - sl)
        tp     = round(max(tp_ref, entry + risk * 2.5), 8)
    else:
        entry, entry_method = calc_dynamic_entry(
            "SHORT", mark_price, k1h, bl.atr_baseline, liq_price)
        # ── v2.0 止损：空头结构无效化点 ──────────────────────────────────────
        # 止损 = 本次反弹高点上方
        pullback_high = max(highs[-5:])  # 本次反弹的最高点
        sl     = round(pullback_high + 0.3 * bl.atr_baseline, 8)
        tp_ref = min(lows[-20:])
        risk   = abs(entry - sl)
        tp     = round(min(tp_ref, entry - risk * 2.5), 8)
    cond["entry_method"] = entry_method

    risk   = abs(entry - sl)
    reward = abs(tp - entry)
    rr     = round(reward / risk, 2) if risk > 0 else 0

    if rr < 2.0:
        return MatchResult(
            matched=False, scene="none", direction=direction,
            entry_price=entry, sl_price=sl, tp_price=tp, rr=rr, second_score=s2,
            reason=f"场景A RR({rr}) < 2.0", conditions=cond,
        )

    return MatchResult(
        matched=True, scene="A_pullback", direction=direction,
        entry_price=entry, sl_price=sl, tp_price=tp, rr=rr, second_score=s2,
        reason=f"场景A回调入场 二次分={s2} RR={rr} | {' | '.join(reasons2)}",
        conditions=cond,
    )


# ══════════════════════════════════════════════════════════════════════════════
# 场景 B：缩量蓄势突破
# ══════════════════════════════════════════════════════════════════════════════

def _scene_b(k1h: list, mark_price: float,
             direction: str, bl: DynamicBaseline) -> MatchResult:
    """
    触发条件：
    ① 近8根在窄区间（价差 < 2×atr_baseline）
    ② 当前 ATR < atr_p40
    ③ 大趋势方向正确（EMA20 斜率）
    ④ 成交量持续低于 vol_baseline

    二次打分（满分100）：
      区间收窄程度  25
      ATR收窄深度  25
      震荡持续根数  20
      大趋势支撑    20
      RR ≥ 2.0     10
    """
    closes = _closes(k1h)
    highs  = _highs(k1h)
    lows   = _lows(k1h)
    vols   = _vols(k1h)
    cond   = {}

    # ① 窄区间：近8根高低点差 < 2×atr_baseline
    # 备用：近8根区间 ≤ 近20根区间的 45%（价格明显收窄）
    w = 8
    wh = highs[-w:]; wl = lows[-w:]
    rh = max(wh); rl = min(wl)
    range_w   = rh - rl
    highs_20 = _highs(k1h[-20:])
    lows_20 = _lows(k1h[-20:])
    range_20  = max(highs_20) - min(lows_20) if len(highs_20) >= 20 else range_w
    range_pct = range_w / rh if rh > 0 else 1.0
    range_ok  = (range_w < 2 * bl.atr_baseline) or (range_20 > 0 and range_w <= range_20 * 0.45)
    cond["range_ok"]  = range_ok
    cond["range_pct"] = round(range_pct, 4)

    # ② ATR 收窄：当前 ATR ≤ p70（宽松版：只要不处于高波动即可）
    atr_now = _atr_now(k1h[-9:]) if len(k1h) >= 9 else bl.atr_baseline
    atr_ok  = atr_now <= bl.atr_p70   # 放宽：p70 而非 p40，避免蓄势初期就被卡死
    cond["atr_shrink_ok"] = atr_ok
    cond["atr_ratio"]     = round(atr_now / bl.atr_baseline if bl.atr_baseline > 0 else 1.0, 2)

    # ③ 大趋势：EMA20 > EMA50（宽松版，适应蓄势阶段 EMA 平坦的情况）
    if len(closes) >= 50:
        ema20_val = _ema(closes[-20:], 20)
        ema50_val = _ema(closes[-50:], 50)
        trend_ok = ema20_val > ema50_val if direction == "LONG" else ema20_val < ema50_val
    elif len(closes) >= 20:
        ema20_val  = _ema(closes[-20:], 20)
        ema20_prev = _ema(closes[-25:-5], 20) if len(closes) >= 25 else ema20_val
        # 只要 EMA20 不明显向下即可
        trend_ok = (ema20_val >= ema20_prev * 0.997) if direction == "LONG" else (ema20_val <= ema20_prev * 1.003)
    else:
        trend_ok = True
    cond["trend_ok"] = trend_ok

    # ④ 持续缩量
    avg15  = _mean(vols[-16:-1]) if len(vols) >= 16 else (vols[-1] if vols else 1.0)
    avg5   = _mean(vols[-5:]) if len(vols) >= 5 else vols[-1]
    vol_ok = avg5 < bl.vol_baseline * 0.85
    cond["vol_ok"]    = vol_ok
    cond["vol_ratio"] = round(avg5 / bl.vol_baseline if bl.vol_baseline > 0 else 1.0, 2)

    all_pass = range_ok and atr_ok and trend_ok and vol_ok
    if not all_pass:
        failed = [k for k, v in cond.items() if k.endswith("_ok") and not v]
        return MatchResult(
            matched=False, scene="none", direction=direction,
            entry_price=0, sl_price=0, tp_price=0, rr=0, second_score=0,
            reason=f"场景B不满足: {failed}", conditions=cond,
        )

    # ── 二次打分 ──────────────────────────────────────────────────────────────
    s2 = 0; reasons2 = []

    # (1) 区间收窄程度 25分
    narrow_ratio = (rh - rl) / (2 * bl.atr_baseline) if bl.atr_baseline > 0 else 1.0
    if narrow_ratio <= 0.5:
        s2 += 25; reasons2.append(f"区间极窄{narrow_ratio:.1f}x(+25)")
    elif narrow_ratio <= 0.8:
        s2 += 18; reasons2.append(f"区间较窄{narrow_ratio:.1f}x(+18)")
    else:
        s2 += 10; reasons2.append(f"区间偏宽{narrow_ratio:.1f}x(+10)")

    # (2) ATR 收窄深度 25分（当前 ATR 在历史百分位）
    atr_rank = cond["atr_ratio"]
    if atr_rank <= 0.4:
        s2 += 25; reasons2.append(f"ATR{atr_rank:.1f}x,极度收窄(+25)")
    elif atr_rank <= 0.6:
        s2 += 18; reasons2.append(f"ATR{atr_rank:.1f}x,明显收窄(+18)")
    else:
        s2 += 10; reasons2.append(f"ATR{atr_rank:.1f}x,轻微收窄(+10)")

    # (3) 震荡持续根数 20分
    consol_bars = sum(1 for i in range(min(15, len(k1h)))
                      if (float(k1h[-1-i][2]) - float(k1h[-1-i][3])) < 1.5 * bl.atr_baseline)
    if consol_bars >= 10:
        s2 += 20; reasons2.append(f"{consol_bars}根蓄势(+20)")
    elif consol_bars >= 7:
        s2 += 14; reasons2.append(f"{consol_bars}根蓄势(+14)")
    elif consol_bars >= 5:
        s2 += 8;  reasons2.append(f"{consol_bars}根蓄势(+8)")
    else:
        s2 += 4;  reasons2.append(f"{consol_bars}根蓄势(+4)")

    # (4) 大趋势支撑 20分
    if len(closes) >= 50:
        ema50 = _ema(closes[-50:], 50)
        above = closes[-1] > ema50
        if (direction == "LONG" and above) or (direction == "SHORT" and not above):
            s2 += 20; reasons2.append("50均线顺势(+20)")
        else:
            s2 += 8; reasons2.append("50均线逆势(+8)")
    else:
        s2 += 12; reasons2.append("数据不足取中(+12)")

    # (5) RR 10分
    s2 += 10; reasons2.append("RR预估满足2.0(+10)")
    s2 = min(s2, 100)

    # ── 挂单参数 ────────────────────────────────────────────────────────────────
    from modules.entry_calc import calc_dynamic_entry, calc_entry_long_scene
    liq_price_b = cond.get("_liq_zone_price")

    if direction == "LONG":
        entry, entry_method_b = calc_entry_long_scene(
            "B_breakout", mark_price, k1h, bl.atr_baseline,
            consolidation_top=rh, liq_zone_price=liq_price_b)
        if entry is None:
            return MatchResult(
                matched=False, scene="none", direction=direction,
                entry_price=0, sl_price=0, tp_price=0, rr=0, second_score=s2,
                reason=f"场景B入场价安全检查不通过: {entry_method_b}",
                conditions=cond,
            )
        sl      = round(rl - 0.5 * bl.atr_baseline, 8)
        h_range = rh - rl
        tp      = round(entry + h_range + 0.5 * bl.atr_baseline, 8)
    else:
        entry, entry_method_b = calc_dynamic_entry(
            "SHORT", rl, k1h, bl.atr_baseline, liq_price_b)
        entry = min(entry, rl)
        sl    = round(rh + 0.5 * bl.atr_baseline, 8)
        h_range = rh - rl
        tp    = round(entry - h_range - 0.5 * bl.atr_baseline, 8)
    cond["entry_method"] = entry_method_b

    risk   = abs(entry - sl)
    reward = abs(tp - entry)
    rr     = round(reward / risk, 2) if risk > 0 else 0

    if rr < 1.8:
        return MatchResult(
            matched=False, scene="none", direction=direction,
            entry_price=entry, sl_price=sl, tp_price=tp, rr=rr, second_score=s2,
            reason=f"场景B RR({rr}) < 1.8", conditions=cond,
        )

    return MatchResult(
        matched=True, scene="B_breakout", direction=direction,
        entry_price=entry, sl_price=sl, tp_price=tp, rr=rr, second_score=s2,
        reason=f"场景B蓄势突破 二次分={s2} RR={rr} | {' | '.join(reasons2)}",
        conditions=cond,
    )


# ══════════════════════════════════════════════════════════════════════════════
# 场景 C：强势回踩（First Pullback）
# ══════════════════════════════════════════════════════════════════════════════

def _scene_c(k1h: list, mark_price: float,
             bl: DynamicBaseline) -> MatchResult:
    """
    触发条件：
    ① 当日涨幅 > strong_threshold（已由 track=momentum 确认）
    ② 回踩幅度 < 本次涨幅的38%
    ③ 未跌破前突破位（用近10根低点均值近似）
    ④ 回踩成交量萎缩（< vol_baseline）

    二次打分（满分100）：
      回踩幅度控制（20%~38%区间）  30
      守住前突破位                  25
      回踩缩量程度                  20
      动能仍在（均线多头）          15
      RR ≥ 2.0                    10
    """
    closes = _closes(k1h)
    highs  = _highs(k1h)
    lows   = _lows(k1h)
    vols   = _vols(k1h)
    cond   = {}

    # 计算本次涨幅（近20根低点 → 近5根高点）
    swing_low  = min(lows[-20:]) if len(lows) >= 20 else min(lows)
    swing_high = max(highs[-5:]) if len(highs) >= 5  else max(highs)
    rise = swing_high - swing_low

    # ① 涨幅（momentum 已确认，此处只验证量化）
    gain_ok = rise / swing_low >= bl.strong_threshold if swing_low > 0 else False
    cond["gain_ok"] = gain_ok
    cond["gain_pct"] = round(rise / swing_low if swing_low > 0 else 0, 4)

    # ② 回踩幅度
    pullback = swing_high - mark_price
    pb_of_rise = pullback / rise if rise > 0 else 0
    pb_ok = pb_of_rise < 0.38
    cond["pb_ok"]       = pb_ok
    cond["pb_of_rise"]  = round(pb_of_rise, 3)

    # ③ 守住前突破位（用近10根低点 avg + 0.5×ATR 作支撑）
    breakout_level = _mean(lows[-11:-1]) if len(lows) >= 11 else min(lows)
    hold_ok = mark_price >= breakout_level - 0.5 * bl.atr_baseline
    cond["hold_ok"] = hold_ok

    # ④ 回踩缩量：修复 3 - 按涨幅大小分级
    recent5     = vols[-5:] if len(vols) >= 5 else vols
    gain_pct = rise / swing_low if swing_low > 0 else 0
    is_large_gain = gain_pct > (bl.gain_p80 if hasattr(bl, "gain_p80") else 0.05)
    vol_threshold = bl.vol_p60 if is_large_gain else (bl.vol_p40 if hasattr(bl, "vol_p40") else bl.vol_baseline)
    shrink_cnt  = sum(1 for v in recent5 if v < vol_threshold)
    vol_ok      = shrink_cnt >= 3
    cond["vol_ok"]     = vol_ok
    cond["shrink_cnt"] = shrink_cnt
    cond["vol_ratio"]  = round(_mean(recent5) / bl.vol_baseline if bl.vol_baseline > 0 else 1.0, 2)
    cond["vol_threshold"] = "vol_p60" if is_large_gain else "vol_p40"

    all_pass = gain_ok and pb_ok and hold_ok and vol_ok
    if not all_pass:
        failed = [k for k, v in cond.items() if k.endswith("_ok") and not v]
        return MatchResult(
            matched=False, scene="none", direction="LONG",
            entry_price=0, sl_price=0, tp_price=0, rr=0, second_score=0,
            reason=f"场景C不满足: {failed}", conditions=cond,
        )

    # ── 二次打分 ──────────────────────────────────────────────────────────────
    s2 = 0; reasons2 = []

    # (1) 回踩幅度 30分
    if 0.20 <= pb_of_rise <= 0.38:
        s2 += 30; reasons2.append(f"回踩{pb_of_rise:.0%}在黄金区(+30)")
    elif pb_of_rise < 0.20:
        s2 += 18; reasons2.append(f"回踩{pb_of_rise:.0%}极浅,可能继续(+18)")
    else:
        s2 += 10; reasons2.append(f"回踩{pb_of_rise:.0%}接近50%(+10)")

    # (2) 守住突破位 25分
    if hold_ok:
        margin = mark_price - (breakout_level - 0.5 * bl.atr_baseline)
        if margin > bl.atr_baseline:
            s2 += 25; reasons2.append("守住突破位且有余量(+25)")
        else:
            s2 += 18; reasons2.append("刚好守住突破位(+18)")
    else:
        s2 += 0  # 触发条件已过滤，不会到这里

    # (3) 缩量 20分
    shrink = 1 - cond["vol_ratio"]
    if shrink >= 0.5:
        s2 += 20; reasons2.append(f"缩量{shrink:.0%}(+20)")
    elif shrink >= 0.3:
        s2 += 13; reasons2.append(f"缩量{shrink:.0%}(+13)")
    else:
        s2 += 7;  reasons2.append(f"轻微缩量(+7)")

    # (4) 均线多头 15分
    if len(closes) >= 20:
        ema20 = _ema(closes[-20:], 20)
        if closes[-1] > ema20:
            s2 += 15; reasons2.append("收盘上EMA20(+15)")
        else:
            s2 += 6;  reasons2.append("收盘下EMA20(+6)")
    else:
        s2 += 10; reasons2.append("数据不足取中(+10)")

    # (5) RR 10分
    s2 += 10; reasons2.append("RR预估≥2.0(+10)")
    s2 = min(s2, 100)

    # ── 挂单参数 ────────────────────────────────────────────────────────────────
    from modules.entry_calc import calc_entry_long_scene
    liq_price_c = cond.get("_liq_zone_price")
    entry, entry_method_c = calc_entry_long_scene(
        "C_first_pull", mark_price, k1h, bl.atr_baseline,
        liq_zone_price=liq_price_c)
    if entry is None:
        return MatchResult(
            matched=False, scene="none", direction="LONG",
            entry_price=0, sl_price=0, tp_price=0, rr=0, second_score=s2,
            reason=f"场景C入场价安全检查不通过: {entry_method_c}",
            conditions=cond,
        )
    cond["entry_method"] = entry_method_c
    sl   = round(min(lows[-5:]) - 0.3 * bl.atr_baseline, 8)
    risk = abs(entry - sl)

    # ── 修改1：高ATR百分位动态止盈 ─────────────────────────────────────────
    # 计算当前ATR在历史分布中的百分位
    from modules.dynamic_baseline import _atr_series as _atr_s
    _atrs = _atr_s(k1h)
    _cur_atr = _mean(_atrs[-3:]) if len(_atrs) >= 3 else _atrs[-1]
    _atr_window = _atrs[-60:] if len(_atrs) >= 60 else _atrs
    _atr_pct = sum(1 for a in _atr_window if a <= _cur_atr) / len(_atr_window) * 100

    if _atr_pct > 80:
        # 高波动状态：TP1 收近，TP2 保留 2.5×risk 供 40% 仓位继续持有
        # TP1 = min(最近清算区附近, entry + 1.5×risk)
        tp1_cap = round(entry + risk * 1.5, 8)
        # 若有清算区价格且在 TP1_cap 以内，以清算区为准
        liq_p = cond.get("_liq_zone_price")
        if liq_p and entry < liq_p <= tp1_cap:
            tp = round(liq_p - 0.1 * _cur_atr, 8)   # 略低于清算区，确保触及
        else:
            tp = tp1_cap
        tp2 = round(entry + risk * 2.5, 8)           # 40% 仓位的更远目标
        cond["tp2_high_vol"]  = tp2
        cond["atr_pct"]       = round(_atr_pct, 1)
        cond["is_high_vol_tp"] = True
    else:
        # 正常状态：原有逻辑不变
        tp_swing = round(swing_high + risk * 0.5, 8)
        tp_rr    = round(entry + 2.5 * risk, 8)
        tp       = tp_swing if tp_swing >= entry + 2.0 * risk else tp_rr
        cond["atr_pct"]        = round(_atr_pct, 1)
        cond["is_high_vol_tp"] = False

    # ── 移动止损触发点（不等TP1，价格涨 1×ATR 即移止损到成本）─────────────
    # 写入 cond 供 ws_monitor 读取，不改变挂单本身的 SL 位置
    cond["trail_trigger_price"] = round(entry + _cur_atr, 8)   # 多头：涨1ATR后移SL到成本
    cond["trail_to_cost"]       = entry                         # 移到的位置 = 入场价

    reward   = abs(tp - entry)
    rr       = round(reward / risk, 2) if risk > 0 else 0

    if rr < 1.8:
        return MatchResult(
            matched=False, scene="none", direction="LONG",
            entry_price=entry, sl_price=sl, tp_price=tp, rr=rr, second_score=s2,
            reason=f"场景C RR({rr}) < 1.8", conditions=cond,
        )

    return MatchResult(
        matched=True, scene="C_first_pull", direction="LONG",
        entry_price=entry, sl_price=sl, tp_price=tp, rr=rr, second_score=s2,
        reason=f"场景C强势回踩 二次分={s2} RR={rr} | {' | '.join(reasons2)}",
        conditions=cond,
    )


# ══════════════════════════════════════════════════════════════════════════════
# 主接口
# ══════════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════════
# 场景 E：做空入场（Short Entry）
# ══════════════════════════════════════════════════════════════════════════════

def _scene_e(k1h: list, mark_price: float,
             bl: "DynamicBaseline") -> "MatchResult":
    """
    触发条件（全部满足）：
    ① 近20根高低点持续降低（下降趋势）
    ② RSI > rsi_mean + 0.5×rsi_std（动态超买）
    ③ 当前价靠近近5根最高价（≥ recent_high × 0.97，在局部高点附近）
    ④ 近3根成交量中至少1根 > vol_baseline（有量能配合）

    二次打分（满分100）：
      阻力位强度（叠加程度）  30
      趋势反转确认程度         25
      RSI 超买程度             20
      成交量配合               15
      RR ≥ 2.0                10
    """
    closes = _closes(k1h)
    highs  = _highs(k1h)
    lows   = _lows(k1h)
    vols   = _vols(k1h)
    cond   = {}

    # ① 下降趋势
    hw20 = highs[-20:]; lw20 = lows[-20:]
    n_h  = max(1, len(hw20) // 2)
    is_downtrend = (max(hw20[-n_h:]) < max(hw20[:n_h]) and
                    min(lw20[-n_h:]) < min(lw20[:n_h]))
    cond["downtrend_ok"] = is_downtrend

    # ② 动态超买
    rsi_val  = _rsi(closes)
    rsi_thr  = bl.rsi_mean + 0.5 * bl.rsi_std
    rsi_ok   = rsi_val >= rsi_thr
    cond["rsi_ok"]  = rsi_ok
    cond["rsi_val"] = round(rsi_val, 1)
    cond["rsi_thr"] = round(rsi_thr, 1)

    # ③ 靠近局部高点
    recent_high = max(highs[-5:])
    near_high   = mark_price >= recent_high * 0.97
    cond["near_high_ok"] = near_high
    cond["recent_high"]  = round(recent_high, 8)

    # ④ 有量能配合（近3根中至少1根放量）
    recent3  = vols[-3:] if len(vols) >= 3 else vols
    vol_ok   = any(v > bl.vol_baseline for v in recent3)
    cond["vol_ok"]    = vol_ok
    cond["vol_ratio"] = round(max(recent3) / bl.vol_baseline if bl.vol_baseline > 0 else 1.0, 2)

    all_pass = is_downtrend and rsi_ok and near_high and vol_ok
    if not all_pass:
        failed = [k for k, v in cond.items() if k.endswith("_ok") and not v]
        return MatchResult(
            matched=False, scene="none", direction="SHORT",
            entry_price=0, sl_price=0, tp_price=0, rr=0, second_score=0,
            reason=f"场景E不满足: {failed}", conditions=cond,
        )

    # ── 二次打分 ──────────────────────────────────────────────────────────────
    s2 = 0; reasons2 = []

    # (1) 阻力位强度：EMA20/50叠加 + 前高叠加 30分
    resistance_count = 0
    if len(closes) >= 20:
        e20 = _ema(closes[-20:], 20)
        if abs(mark_price - e20) <= bl.atr_baseline * 0.5:
            resistance_count += 1
    if len(closes) >= 50:
        e50 = _ema(closes[-50:], 50)
        if abs(mark_price - e50) <= bl.atr_baseline * 0.8:
            resistance_count += 1
    prev_high = max(highs[-11:-1]) if len(highs) >= 11 else max(highs)
    if abs(mark_price - prev_high) <= bl.atr_baseline:
        resistance_count += 1

    if resistance_count >= 3:
        s2 += 30; reasons2.append("三重阻力叠加(+30)")
    elif resistance_count == 2:
        s2 += 22; reasons2.append("双重阻力(+22)")
    elif resistance_count == 1:
        s2 += 12; reasons2.append("单一阻力(+12)")
    else:
        s2 += 4;  reasons2.append("无明显阻力位(+4)")

    # (2) 趋势反转确认 25分
    # 最后1根是否为阴线（卖压）
    last_bear = float(k1h[-1][4]) < float(k1h[-1][1])
    # 近5根中阴线数量
    bear_count = sum(1 for i in range(-5, 0) if float(k1h[i][4]) < float(k1h[i][1]))
    if last_bear and bear_count >= 3:
        s2 += 25; reasons2.append(f"近5根{bear_count}阴+末阴线(+25)")
    elif last_bear:
        s2 += 15; reasons2.append(f"末阴线(+15)")
    elif bear_count >= 3:
        s2 += 12; reasons2.append(f"近5根{bear_count}阴(+12)")
    else:
        s2 += 5;  reasons2.append("卖压不明确(+5)")

    # (3) RSI 超买程度 20分
    rsi_excess = rsi_val - rsi_thr
    rsi_1sig   = bl.rsi_std
    if rsi_excess >= rsi_1sig:
        s2 += 20; reasons2.append(f"RSI超买超1σ({rsi_val:.0f})(+20)")
    elif rsi_excess >= rsi_1sig * 0.5:
        s2 += 13; reasons2.append(f"RSI超买0.5σ({rsi_val:.0f})(+13)")
    else:
        s2 += 6;  reasons2.append(f"RSI轻微超买({rsi_val:.0f})(+6)")

    # (4) 成交量配合 15分
    max_vol3 = max(recent3)
    if max_vol3 >= bl.vol_baseline + bl.vol_std:
        s2 += 15; reasons2.append("放量确认(+15)")
    elif max_vol3 >= bl.vol_baseline:
        s2 += 9;  reasons2.append("量平稳(+9)")
    else:
        s2 += 3;  reasons2.append("量偏低(+3)")

    # (5) RR 10分（预估）
    s2 += 10; reasons2.append("RR预估≥2.0(+10)")
    s2 = min(s2, 100)

    # ── 挂单参数（动态入场价）─────────────────────────────────────────────────
    from modules.entry_calc import calc_dynamic_entry
    liq_price_e = cond.get("_liq_zone_price")
    entry, entry_method_e = calc_dynamic_entry(
        "SHORT", mark_price, k1h, bl.atr_baseline, liq_price_e)
    cond["entry_method"] = entry_method_e

    sl   = round(recent_high + 0.5 * bl.atr_baseline, 8)
    risk = abs(entry - sl)
    if risk <= 0:
        return MatchResult(
            matched=False, scene="none", direction="SHORT",
            entry_price=entry, sl_price=sl, tp_price=0, rr=0, second_score=s2,
            reason="场景E止损计算异常(risk=0)", conditions=cond,
        )
    # 止盈：下方支撑位 or RR≥2.0
    support_level = min(lows[-20:]) if len(lows) >= 20 else min(lows)
    tp_by_support = support_level + bl.atr_baseline * 0.3
    tp_by_rr      = entry - risk * 2.0
    tp = round(min(tp_by_support, tp_by_rr), 8)   # 取更近的（保守）

    reward = abs(tp - entry)
    rr     = round(reward / risk, 2) if risk > 0 else 0

    if rr < 1.8:
        return MatchResult(
            matched=False, scene="none", direction="SHORT",
            entry_price=entry, sl_price=sl, tp_price=tp, rr=rr, second_score=s2,
            reason=f"场景E RR({rr}) < 1.8", conditions=cond,
        )

    return MatchResult(
        matched=True, scene="E_short", direction="SHORT",
        entry_price=entry, sl_price=sl, tp_price=tp, rr=rr, second_score=s2,
        reason=f"场景E做空入场 二次分={s2} RR={rr} | {' | '.join(reasons2)}",
        conditions=cond,
    )


def match(k1h: list, mark_price: float,
          direction: str,
          track: str = "oscillation",
          bl: Optional[DynamicBaseline] = None,
          symbol: str = "",
          enable_trend_filter: bool = True,
          btc_closes_4h: Optional[list] = None,
          eth_closes_4h: Optional[list] = None,
          market_state: str = "") -> MatchResult:
    """
    根据场景候选列表运行触发条件 + 二次打分。

    参数：
        k1h          — 1小时K线
        mark_price   — 当前价
        direction    — "LONG" | "SHORT"
        track        — "momentum" | "oscillation"
        bl           — 已计算的基准（None 则自动计算）
        market_state — 市场状态（传给 position_judge.judge()）

    返回：
        MatchResult，second_score 是二次打分（0~100）
        matched=False + scene="none" → 进观察池
    """
    if len(k1h) < 15 or mark_price <= 0:
        return MatchResult(
            matched=False, scene="none", direction=direction,
            entry_price=0, sl_price=0, tp_price=0, rr=0, second_score=0,
            reason="数据不足", conditions={},
        )

    bl = bl or compute_baseline(k1h)

    # ── 趋势过滤（修复 2：区分 BTC 和其他币）──────────────────────────────────
    trend_bias: TrendBias = "NEUTRAL"
    if enable_trend_filter and btc_closes_4h:
        trend_bias = judge_market_bias_stable(symbol, btc_closes_4h, eth_closes_4h, slope_threshold=0.005)
    # 注意：趋势过滤改为动态门槛，在场景函数中检查 second_score >= required_score

    # ── 清算区查询（第三层入场价优化，可选）──────────────────────────────────
    _liq_zone_price = None
    if symbol:
        try:
            import sys
            sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent.parent))
            from coinglass_api import get_liquidation_zone
            liq_info = get_liquidation_zone(
                symbol=symbol,
                price=mark_price, direction=direction,
                search_range_pct=3.0, min_volume=500_000,
            )
            if liq_info.get("has_liq_zone"):
                _liq_zone_price = liq_info["liq_zone_price"]
        except Exception:
            pass  # 清算区查询失败不影响主流程

    from modules.position_judge import judge
    # v1.2: 传入 market_state 供位置判断过滤（RANGING禁C，TRENDING_DOWN禁多头）
    scenes = judge(mark_price, direction, track, bl, k1h=k1h, market_state=market_state)

    if not scenes:
        return MatchResult(
            matched=False, scene="none", direction=direction,
            entry_price=0, sl_price=0, tp_price=0, rr=0, second_score=0,
            reason="位置判断：低位空头不追空", conditions={},
        )

    # 把清算区价格注入 cond 供场景函数读取（通过临时全局变量传递）
    _SCENE_LIQ_PRICE = _liq_zone_price  # noqa

    results = []
    for scene_id in scenes:
        if scene_id == "A_pullback":
            r = _scene_a(k1h, mark_price, direction, bl)
            if _liq_zone_price: r["conditions"]["_liq_zone_price"] = _liq_zone_price
        elif scene_id == "B_breakout":
            r = _scene_b(k1h, mark_price, direction, bl)
            if _liq_zone_price: r["conditions"]["_liq_zone_price"] = _liq_zone_price
        elif scene_id == "C_first_pull":
            r = _scene_c(k1h, mark_price, bl)
            if _liq_zone_price: r["conditions"]["_liq_zone_price"] = _liq_zone_price
        elif scene_id == "E_short":
            r = _scene_e(k1h, mark_price, bl)
            if _liq_zone_price: r["conditions"]["_liq_zone_price"] = _liq_zone_price
        else:
            continue
        if r["matched"]:
            # 趋势过滤检查（动态门槛）
            if enable_trend_filter:
                required_score = get_required_score(direction, trend_bias, base_threshold=70, trend_penalty=12)
                if r["second_score"] < required_score:
                    r["matched"] = False
                    r["reason"] = f"趋势过滤：分数{r['second_score']} < 要求{required_score} (trend={trend_bias})"
                    r["conditions"]["trend_bias"] = trend_bias
                    r["conditions"]["required_score"] = required_score
            if r["matched"]:
                results.append(r)

    if not results:
        # 汇总未匹配原因
        a = _scene_a(k1h, mark_price, direction, bl) if "A_pullback" in scenes else None
        b = _scene_b(k1h, mark_price, direction, bl) if "B_breakout" in scenes else None
        c = _scene_c(k1h, mark_price, bl)            if "C_first_pull" in scenes else None
        e = _scene_e(k1h, mark_price, bl)            if "E_short" in scenes else None
        msgs = []
        for r in [a, b, c, e]:
            if r: msgs.append(r["reason"])
        return MatchResult(
            matched=False, scene="none", direction=direction,
            entry_price=0, sl_price=0, tp_price=0, rr=0, second_score=0,
            reason=" || ".join(msgs),
            conditions={"tried": scenes},
        )

    # 多个场景均满足 → 选二次打分最高的
    best = max(results, key=lambda r: r["second_score"])
    if len(results) > 1:
        best["reason"] = (f"多场景满足({[r['scene'] for r in results]})，"
                          f"选二次分最高 {best['scene']}={best['second_score']}: "
                          + best["reason"])

    # ══════════════════════════════════════════════════════════════════════
    # 修刹2（v1.2）：SL 最小距离验证
    # abs(entry - sl) < 0.3 × atr_baseline → 拒绝信号，进观察池
    # 原因：SL贴近入场价 = 保证金清零，无法提供任何风険保护
    # 已该修改塑造了某些场景中 SL=entry 的情况（详见模拟周分析）
    # ══════════════════════════════════════════════════════════════════════
    _min_sl_dist = 0.3 * bl.atr_baseline
    _sl_dist = abs(best["entry_price"] - best["sl_price"])
    if best["entry_price"] > 0 and _sl_dist < _min_sl_dist:
        best["matched"] = False
        best["reason"] = (
            f"[SL最小距离拒绝] entry={best['entry_price']:.6f} "
            f"sl={best['sl_price']:.6f} 距离={_sl_dist:.6f} "
            f"< 0.3×ATR={_min_sl_dist:.6f} → 进观察池"
        )
        best["conditions"]["sl_dist_rejected"] = True
        best["conditions"]["sl_dist"] = round(_sl_dist, 8)
        best["conditions"]["min_sl_dist"] = round(_min_sl_dist, 8)
        return best

    # ══════════════════════════════════════════════════════════════════════
    # 修刹3（v1.2）：TP1 改动态距离
    # TP1 = min(swing_high, entry + 1.5×atr_baseline)
    # 同时保证 TP1 RR ≥ 1.2，取三者中满足 RR≥1.2 的最近目标
    # 废弃静态 TP1 = entry + N×risk 的方式
    # ══════════════════════════════════════════════════════════════════════
    _entry = best["entry_price"]
    _sl    = best["sl_price"]
    _risk  = abs(_entry - _sl)
    if best["direction"] == "LONG" and _risk > 0:
        _swing_high = max([float(x[2]) for x in k1h[-20:]])
        _tp1_atr    = round(_entry + 1.5 * bl.atr_baseline, 8)
        _tp1_swing  = _swing_high
        _min_tp1    = round(_entry + 1.2 * _risk, 8)   # RR≥1.2 山线
        # 取 swing_high 和 atr×1.5 中较近的（更容易触及）
        _tp1_candidate = min(_tp1_atr, _tp1_swing)
        _tp1_final = max(_tp1_candidate, _min_tp1)  # 至少满足 RR≥1.2
        if _tp1_final > _entry:
            old_tp = best["tp_price"]
            best["tp_price"] = _tp1_final
            best["rr"] = round(abs(_tp1_final - _entry) / _risk, 2)
            best["conditions"]["tp1_dynamic"] = True
            best["conditions"]["tp1_old"] = round(old_tp, 8)
            best["conditions"]["tp1_new"] = round(_tp1_final, 8)
            best["conditions"]["tp1_method"] = (
                "atr1.5" if _tp1_candidate == _tp1_atr else "swing"
            )
    elif best["direction"] == "SHORT" and _risk > 0:
        _swing_low  = min([float(x[3]) for x in k1h[-20:]])
        _tp1_atr    = round(_entry - 1.5 * bl.atr_baseline, 8)
        _tp1_swing  = _swing_low
        _min_tp1    = round(_entry - 1.2 * _risk, 8)
        _tp1_candidate = max(_tp1_atr, _tp1_swing)  # 空头取较高的（较近入场价）
        _tp1_final = min(_tp1_candidate, _min_tp1)
        if _tp1_final < _entry:
            old_tp = best["tp_price"]
            best["tp_price"] = _tp1_final
            best["rr"] = round(abs(_tp1_final - _entry) / _risk, 2)
            best["conditions"]["tp1_dynamic"] = True
            best["conditions"]["tp1_old"] = round(old_tp, 8)
            best["conditions"]["tp1_new"] = round(_tp1_final, 8)

    return best
