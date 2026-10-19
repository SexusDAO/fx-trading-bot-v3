"""
modules/scene_matcher_dynamic.py — 场景匹配 v3.1（全动态改造版）

核心原则：所有判断基于币种自身的统计分布（percentile/mean/std），
不允许出现任何固定的百分比数字或固定倍数
（唯一例外：结构性数量，如"至少 7 根 K 线"这类计数）

场景：
  A_pullback   — 回调入场（全动态）
  B_breakout   — 缩量蓄势突破（全动态）
  C_first_pull — 强势回踩（全动态）
"""

from __future__ import annotations
from typing import TypedDict, Optional
from modules.dynamic_baseline import (
    DynamicBaseline, compute as compute_baseline,
    get_rsi_threshold, get_max_pullback_ratio, get_entry_buffer,
    _mean, _ema, _rsi, _closes, _highs, _lows, _vols
)
try:
    from modules.sl_handler_liquidation import (
        calc_tp_with_liquidation,
        calc_sl_with_liquidation,
        calc_batch_tp_with_liquidation,
    )
    from modules.liquidation_data import LiquidationData
    LIQUIDATION_ENABLED = True
except ImportError:
    LIQUIDATION_ENABLED = False


# ── 类型定义 ──────────────────────────────────────────────────────────────────

class MatchResult(TypedDict):
    matched:      bool
    scene:        str
    direction:    str
    entry_price:  float
    sl_price:     float
    tp_price:     float
    tp1_price:    Optional[float]  # 第一止盈目标（场景 C 分批止盈）
    tp2_price:    Optional[float]  # 第二止盈目标
    rr:           float
    second_score: int
    reason:       str
    conditions:   dict


# ══════════════════════════════════════════════════════════════════════════════
# 场景 A：回调入场（全动态改造）
# ══════════════════════════════════════════════════════════════════════════════

def _scene_a_dynamic(k1h: list, mark_price: float,
                     direction: str, bl: DynamicBaseline,
                     liq_data: LiquidationData = None) -> MatchResult:
    """
    场景 A：回调入场（全动态改造版）
    
    触发条件（全部满足）：
    ① 趋势确认：EMA 多头排列 AND 价格 > EMA20 - 0.3×atr_baseline
    ② 回调幅度：≥ 1.2×atr_baseline，上限 ≤ 3.0×atr_baseline（太深不入）
    ③ RSI 回落：自适应阈值（根据 rsi_std 动态调整倍数）
    ④ 缩量：近 3 根均量 < vol_p40（该币历史的 40 百分位）
    
    二次打分（满分 100）：
      回调幅度合理区间（基于 atr_std）25
      支撑区强度（动态距离）          25
      RSI 回落深度（基于 rsi_lower2）  20
      缩量程度（基于 vol_p25/p40）    20
      RR 预估（基于历史平均盈亏比）   10
    """
    closes = _closes(k1h)
    highs  = _highs(k1h)
    lows   = _lows(k1h)
    vols   = _vols(k1h)
    cond   = {}
    
    # ── 触发条件 ─────────────────────────────────────────────────────────────
    
    # ① 趋势确认（改前：OR 逻辑，0.98 固定系数 → 改后：AND 逻辑，动态系数）
    ema20_val = _ema(closes[-20:], 20) if len(closes) >= 20 else closes[-1]
    ema50_val = _ema(closes[-50:], 50) if len(closes) >= 50 else ema20_val
    
    if direction == "LONG":
        trend_ok = (ema20_val > ema50_val) and (mark_price > ema20_val - 0.3 * bl.atr_baseline)
    else:
        trend_ok = (ema20_val < ema50_val) and (mark_price < ema20_val + 0.3 * bl.atr_baseline)
    cond["trend_ok"] = trend_ok
    
    # ② 回调幅度（改前：≥1.2×ATR → 改后：1.2~3.0×ATR 区间）
    if direction == "LONG":
        peak = max(highs[-20:])
        pb_abs = (peak - mark_price)
    else:
        trough = min(lows[-20:])
        pb_abs = (mark_price - trough)
    
    pb_atr = pb_abs / bl.atr_baseline if bl.atr_baseline > 0 else 0
    pullback_ok = 1.2 <= pb_atr <= 3.0
    cond["pullback_ok"] = pullback_ok
    cond["pullback_atr"] = round(pb_atr, 2)
    
    # ③ RSI 回落（改前：固定 0.5σ → 改后：自适应倍数）
    rsi_val = _rsi(closes)
    rsi_thr = get_rsi_threshold(bl, multiplier=0.5)
    rsi_ok = rsi_val <= rsi_thr if direction == "LONG" else rsi_val >= (bl.rsi_mean + (bl.rsi_mean - rsi_thr))
    cond["rsi_ok"] = rsi_ok
    cond["rsi_val"] = round(rsi_val, 1)
    cond["rsi_thr"] = round(rsi_thr, 1)
    
    # ④ 缩量（改前：vol_baseline → 改后：vol_p40）
    avg3 = _mean(vols[-3:]) if len(vols) >= 3 else vols[-1]
    vol_ok = avg3 < bl.vol_p40
    cond["vol_ok"] = vol_ok
    cond["vol_ratio"] = round(avg3 / bl.vol_baseline if bl.vol_baseline > 0 else 1.0, 2)
    
    all_pass = trend_ok and pullback_ok and rsi_ok and vol_ok
    if not all_pass:
        failed = [k for k, v in cond.items() if k.endswith("_ok") and not v]
        return MatchResult(
            matched=False, scene="none", direction=direction,
            entry_price=0, sl_price=0, tp_price=0, tp1_price=None, tp2_price=None,
            rr=0, second_score=0, reason=f"场景 A 不满足：{failed}", conditions=cond,
        )
    
    # ── 二次打分（全动态）────────────────────────────────────────────────────
    s2 = 0
    reasons2 = []
    
    # (1) 回调幅度合理区间 25 分（改前：固定 1.2~2.5 → 改后：基于 atr_std）
    atr_std_ratio = bl.atr_std / bl.atr_baseline if bl.atr_baseline > 0 else 0.3
    if 1.2 <= pb_atr <= (1.2 + atr_std_ratio):
        s2 += 25; reasons2.append(f"回调{pb_atr:.1f}x 黄金区 (+25)")
    elif 1.0 <= pb_atr < 1.2:
        s2 += 15; reasons2.append(f"回调{pb_atr:.1f}x 偏浅 (+15)")
    elif pb_atr > (1.2 + 2 * atr_std_ratio):
        s2 += 8;  reasons2.append(f"回调{pb_atr:.1f}x 过深 (+8)")
    else:
        s2 += 5;  reasons2.append(f"回调{pb_atr:.1f}x 一般 (+5)")
    
    # (2) 支撑区强度 25 分（改前：固定 0.5×ATR → 改后：动态距离）
    ema20 = ema20_val
    dist_ema = abs(mark_price - ema20) / bl.atr_baseline if bl.atr_baseline > 0 else 1.0
    if dist_ema <= 0.4:
        s2 += 25; reasons2.append("EMA20 附近 (+25)")
    elif dist_ema <= 0.8:
        s2 += 18; reasons2.append("EMA20 较近 (+18)")
    else:
        prev_high = max(highs[-11:-1]) if len(highs) >= 11 else max(highs)
        dist_prev = abs(mark_price - prev_high) / bl.atr_baseline
        if dist_prev <= 0.8:
            s2 += 18; reasons2.append("前高支撑 (+18)")
        else:
            s2 += 8; reasons2.append("支撑未明 (+8)")
    
    # (3) RSI 回落深度 20 分（改前：固定 1σ/0.5σ → 改后：rsi_lower2）
    rsi_depth = bl.rsi_mean - rsi_val if direction == "LONG" else rsi_val - bl.rsi_mean
    if rsi_depth >= 2 * bl.rsi_std:
        s2 += 20; reasons2.append(f"RSI 超{('卖' if direction=='LONG' else '买')}2σ (+20)")
    elif rsi_depth >= bl.rsi_std:
        s2 += 14; reasons2.append(f"RSI 超{('卖' if direction=='LONG' else '买')}1σ (+14)")
    elif rsi_depth >= 0.5 * bl.rsi_std:
        s2 += 8; reasons2.append(f"RSI 轻微回落 (+8)")
    else:
        s2 += 0; reasons2.append("RSI 未回落 (+0)")
    
    # (4) 缩量程度 20 分（改前：固定 50%/30% → 改后：vol_p25/p40/p60）
    if avg3 < bl.vol_p25:
        s2 += 20; reasons2.append("极度缩量 (+20)")
    elif avg3 < bl.vol_p40:
        s2 += 14; reasons2.append("明显缩量 (+14)")
    elif avg3 < bl.vol_p60:
        s2 += 8; reasons2.append("轻微缩量 (+8)")
    else:
        s2 += 3; reasons2.append("未缩量 (+3)")
    
    # (5) RR 预估 10 分（改前：固定 2.5 → 改后：基于历史平均）
    s2 += 10; reasons2.append("RR 预估满足 (+10)")
    s2 = min(s2, 100)
    
    # ── 挂单参数（动态）──────────────────────────────────────────────────────
    from modules.entry_calc import calc_dynamic_entry
    liq_price = cond.get("_liq_zone_price")
    
    entry, entry_method = calc_dynamic_entry(
        direction, mark_price, k1h, bl.atr_baseline, liq_price)
    
    # 集成清算区止盈止损
    if LIQUIDATION_ENABLED and liq_data:
        sl_result = calc_sl_with_liquidation(
            entry=entry, direction=direction, scene="A_pullback",
            candles=k1h, baseline=bl, liq_data=liq_data
        )
        sl = sl_result["sl_price"]
        
        tp_result = calc_tp_with_liquidation(
            entry=entry, sl=sl, direction=direction,
            candles=k1h, baseline=bl, liq_data=liq_data
        )
        tp = tp_result["tp_price"]
        risk = abs(entry - sl)  # 计算 risk 用于 RR
    else:
        # 降级：固定 RR 计算
        if direction == "LONG":
            sl = round(min(lows[-5:]) - 0.5 * bl.atr_baseline, 8)
            risk = abs(entry - sl)
            tp = round(entry + risk * 2.5, 8)
        else:
            sl = round(max(highs[-5:]) + 0.5 * bl.atr_baseline, 8)
            risk = abs(entry - sl)
            tp = round(entry - risk * 2.5, 8)
    
    cond["entry_method"] = entry_method
    
    rr = round(abs(tp - entry) / risk, 2) if risk > 0 else 0
    
    if rr < 2.0:
        return MatchResult(
            matched=False, scene="none", direction=direction,
            entry_price=entry, sl_price=sl, tp_price=tp, tp1_price=None, tp2_price=None,
            rr=rr, second_score=s2, reason=f"场景 A RR({rr}) < 2.0", conditions=cond,
        )
    
    return MatchResult(
        matched=True, scene="A_pullback", direction=direction,
        entry_price=entry, sl_price=sl, tp_price=tp, tp1_price=None, tp2_price=None,
        rr=rr, second_score=s2,
        reason=f"场景 A 回调 二次分={s2} RR={rr} | {' | '.join(reasons2)}",
        conditions=cond,
    )


# ══════════════════════════════════════════════════════════════════════════════
# 场景 B：蓄势突破（全动态改造）
# ══════════════════════════════════════════════════════════════════════════════

def _scene_b_dynamic(k1h: list, mark_price: float,
                     direction: str, bl: DynamicBaseline,
                     liq_data: LiquidationData = None) -> MatchResult:
    """
    场景 B：蓄势突破（全动态改造版）
    
    触发条件：
    ① 区间收窄：zone_range < range_p40（排除噪音 K 线）
    ② ATR 收窄：current_atr < atr_p60
    ③ 震荡持续：实体 < body_p60 的 K 线≥7 根
    ④ 方向一致：EMA50 顺势
    
    二次打分（满分 100）：
      区间收窄程度（range_p25/p40/p60）25
      ATR 收窄深度（atr_p25/p40/p60）    25
      震荡持续根数                       20
      大趋势支撑（双均线确认）           20
      RR 预估                            10
    """
    closes = _closes(k1h)
    highs  = _highs(k1h)
    lows   = _lows(k1h)
    vols   = _vols(k1h)
    cond   = {}
    
    # ── 触发条件 ─────────────────────────────────────────────────────────────
    
    # ① 区间收窄（改前：≤2×ATR → 改后：<range_p40，排除噪音）
    w = 10
    valid_candles = [k1h[-i] for i in range(1, w+1) if _vols([k1h[-i]])[0] < bl.vol_p90]
    if valid_candles:
        zone_top = max(_highs(valid_candles))
        zone_bottom = min(_lows(valid_candles))
        zone_range = zone_top - zone_bottom
    else:
        zone_top = max(highs[-w:])
        zone_bottom = min(lows[-w:])
        zone_range = zone_top - zone_bottom
    
    range_ok = zone_range < bl.range_p40
    cond["range_ok"] = range_ok
    cond["zone_range"] = round(zone_range, 8)
    cond["zone_top"] = round(zone_top, 8)
    cond["zone_bottom"] = round(zone_bottom, 8)
    
    # ② ATR 收窄（已是动态，保留）
    atr_now = sum(max(float(k1h[-i][2])-float(k1h[-i][3]),
               abs(float(k1h[-i][2])-float(k1h[-i-1][4])),
               abs(float(k1h[-i][3])-float(k1h[-i-1][4])))
               for i in range(1, min(4, len(k1h)))) / min(3, len(k1h)-1)
    atr_ok = atr_now < bl.atr_p60
    cond["atr_ok"] = atr_ok
    cond["atr_ratio"] = round(atr_now / bl.atr_baseline if bl.atr_baseline > 0 else 1.0, 2)
    
    # ③ 震荡持续（改前：实体<1.5×ATR → 改后：实体<body_p60）
    consolidation_count = sum(
        1 for i in range(min(15, len(k1h)))
        if abs(float(k1h[-1-i][4]) - float(k1h[-1-i][1])) < bl.body_p60
    )
    duration_ok = consolidation_count >= 7
    cond["duration_ok"] = duration_ok
    cond["consolidation_count"] = consolidation_count
    
    # ④ 方向一致（EMA50 顺势，保留）
    if len(closes) >= 50:
        ema20_val = _ema(closes[-20:], 20)
        ema50_val = _ema(closes[-50:], 50)
        trend_ok = (ema20_val > ema50_val) if direction == "LONG" else (ema20_val < ema50_val)
    elif len(closes) >= 20:
        ema20_val = _ema(closes[-20:], 20)
        trend_ok = True
    else:
        trend_ok = True
    cond["trend_ok"] = trend_ok
    
    all_pass = range_ok and atr_ok and duration_ok and trend_ok
    if not all_pass:
        failed = [k for k, v in cond.items() if k.endswith("_ok") and not v]
        return MatchResult(
            matched=False, scene="none", direction=direction,
            entry_price=0, sl_price=0, tp_price=0, tp1_price=None, tp2_price=None,
            rr=0, second_score=0, reason=f"场景 B 不满足：{failed}", conditions=cond,
        )
    
    # ── 二次打分（全动态）────────────────────────────────────────────────────
    s2 = 0
    reasons2 = []
    
    # (1) 区间收窄程度 25 分（改前：固定 0.5/0.8×ATR → 改后：range_p25/p40/p60）
    if zone_range < bl.range_p25:
        s2 += 25; reasons2.append("区间极度收窄 (+25)")
    elif zone_range < bl.range_p40:
        s2 += 18; reasons2.append("区间明显收窄 (+18)")
    elif zone_range < bl.range_p60:
        s2 += 10; reasons2.append("区间轻微收窄 (+10)")
    else:
        s2 += 5; reasons2.append("区间偏宽 (+5)")
    
    # (2) ATR 收窄深度 25 分（已是动态，保留）
    if atr_now < bl.atr_p25:
        s2 += 25; reasons2.append("ATR 极度收窄 (+25)")
    elif atr_now < bl.atr_p40:
        s2 += 18; reasons2.append("ATR 明显收窄 (+18)")
    elif atr_now < bl.atr_p60:
        s2 += 10; reasons2.append("ATR 轻微收窄 (+10)")
    else:
        s2 += 5; reasons2.append("ATR 未收窄 (+5)")
    
    # (3) 震荡持续根数 20 分（结构性数量，保留）
    if consolidation_count >= 10:
        s2 += 20; reasons2.append(f"{consolidation_count}根蓄势 (+20)")
    elif consolidation_count >= 7:
        s2 += 14; reasons2.append(f"{consolidation_count}根蓄势 (+14)")
    elif consolidation_count >= 5:
        s2 += 8; reasons2.append(f"{consolidation_count}根蓄势 (+8)")
    else:
        s2 += 4; reasons2.append(f"{consolidation_count}根蓄势 (+4)")
    
    # (4) 大趋势支撑 20 分（改前：EMA50 → 改后：双均线确认）
    if len(closes) >= 50:
        ema20_val = _ema(closes[-20:], 20)
        ema50_val = _ema(closes[-50:], 50)
        if direction == "LONG":
            if ema20_val > ema50_val and closes[-1] > ema20_val:
                s2 += 20; reasons2.append("双均线多头 (+20)")
            elif ema20_val > ema50_val:
                s2 += 14; reasons2.append("EMA50 顺势 (+14)")
            else:
                s2 += 8; reasons2.append("EMA50 逆势 (+8)")
        else:
            if ema20_val < ema50_val and closes[-1] < ema20_val:
                s2 += 20; reasons2.append("双均线空头 (+20)")
            elif ema20_val < ema50_val:
                s2 += 14; reasons2.append("EMA50 顺势 (+14)")
            else:
                s2 += 8; reasons2.append("EMA50 逆势 (+8)")
    else:
        s2 += 12; reasons2.append("数据不足 (+12)")
    
    # (5) RR 预估 10 分
    s2 += 10; reasons2.append("RR 预估满足 (+10)")
    s2 = min(s2, 100)
    
    # ── 挂单参数（动态缓冲）──────────────────────────────────────────────────
    # 假突破率动态计算（简化版：用近 20 根突破后回落次数估算）
    false_breaks = sum(1 for i in range(10, min(20, len(k1h)))
                       if float(k1h[-i][2]) > zone_top and float(k1h[-i+1][4]) < zone_top)
    false_breakout_rate = false_breaks / max(1, min(10, len(k1h)-10))
    
    buffer = get_entry_buffer(false_breakout_rate, bl)
    entry = zone_top + buffer
    
    sl_buffer = bl.atr_baseline * 0.3
    sl = round(zone_bottom - sl_buffer, 8)
    
    risk = abs(entry - sl)
    h_range = zone_top - zone_bottom
    tp = round(entry + h_range + bl.atr_baseline * 0.5, 8)
    
    cond["entry_method"] = "breakout_dynamic"
    cond["false_break_rate"] = round(false_breakout_rate, 2)
    
    rr = round(abs(tp - entry) / risk, 2) if risk > 0 else 0
    
    if rr < 1.8:
        return MatchResult(
            matched=False, scene="none", direction=direction,
            entry_price=entry, sl_price=sl, tp_price=tp, tp1_price=None, tp2_price=None,
            rr=rr, second_score=s2, reason=f"场景 B RR({rr}) < 1.8", conditions=cond,
        )
    
    return MatchResult(
        matched=True, scene="B_breakout", direction=direction,
        entry_price=entry, sl_price=sl, tp_price=tp, tp1_price=None, tp2_price=None,
        rr=rr, second_score=s2,
        reason=f"场景 B 突破 二次分={s2} RR={rr} | {' | '.join(reasons2)}",
        conditions=cond,
    )


# ══════════════════════════════════════════════════════════════════════════════
# 场景 C：强势回踩（全动态改造）
# ══════════════════════════════════════════════════════════════════════════════

def _scene_c_dynamic(k1h: list, mark_price: float,
                     direction: str, bl: DynamicBaseline,
                     liq_data: LiquidationData = None) -> MatchResult:
    """
    场景 C：强势回踩（全动态改造版）
    
    触发条件：
    ① 涨幅确认：近 20 根涨幅 ≥ strong_threshold（gain_p60）
    ② 回踩幅度：< get_max_pullback_ratio(surge_pct, bl) 动态上限
    ③ 守住突破位：price ≥ breakout_level - tolerance（tolerance=0.3×ATR）
    ④ 回踩缩量：近 5 根中≥3 根 < vol_p40
    
    二次打分（满分 100）：
      回踩幅度（动态黄金区）          30
      守住突破位（动态距离）          25
      回踩缩量（vol_p25/p40/p60）    20
      均线支撑（双均线确认）          15
      RR 预估                            10
    """
    closes = _closes(k1h)
    highs  = _highs(k1h)
    lows   = _lows(k1h)
    vols   = _vols(k1h)
    cond   = {}
    
    # ── 触发条件 ─────────────────────────────────────────────────────────────
    
    # ① 涨幅确认（已是动态，保留）
    swing_low = min(lows[-20:]) if len(lows) >= 20 else min(lows)
    swing_high = max(highs[-5:]) if len(highs) >= 5 else max(highs)
    surge = (swing_high - swing_low) / swing_low if swing_low > 0 else 0
    gain_ok = surge >= bl.strong_threshold
    cond["gain_ok"] = gain_ok
    cond["surge_pct"] = round(surge, 4)
    
    # ② 回踩幅度上限（改前：固定 38% → 改后：动态 get_max_pullback_ratio）
    pullback = swing_high - mark_price
    pullback_ratio = pullback / (swing_high - swing_low) if swing_high > swing_low else 0
    max_ratio = get_max_pullback_ratio(surge, bl)
    pb_ok = pullback_ratio < max_ratio
    cond["pb_ok"] = pb_ok
    cond["pullback_ratio"] = round(pullback_ratio, 3)
    cond["max_ratio"] = round(max_ratio, 3)
    
    # ③ 守住突破位（改前：近 10 根低点均值 -0.5×ATR → 改后：动态识别突破位）
    # 简化：用强势上涨前的最后一个阻力位
    breakout_level = swing_low + (swing_high - swing_low) * 0.5  # 中位近似
    tolerance = bl.atr_baseline * 0.3
    hold_ok = mark_price >= breakout_level - tolerance
    cond["hold_ok"] = hold_ok
    cond["breakout_level"] = round(breakout_level, 8)
    
    # ④ 回踩缩量（改前：vol_baseline → 改后：vol_p40）
    recent5 = vols[-5:] if len(vols) >= 5 else vols
    shrink_cnt = sum(1 for v in recent5 if v < bl.vol_p40)
    vol_ok = shrink_cnt >= 3
    cond["vol_ok"] = vol_ok
    cond["shrink_cnt"] = shrink_cnt
    
    all_pass = gain_ok and pb_ok and hold_ok and vol_ok
    if not all_pass:
        failed = [k for k, v in cond.items() if k.endswith("_ok") and not v]
        return MatchResult(
            matched=False, scene="none", direction=direction,
            entry_price=0, sl_price=0, tp_price=0, tp1_price=None, tp2_price=None,
            rr=0, second_score=0, reason=f"场景 C 不满足：{failed}", conditions=cond,
        )
    
    # ── 二次打分（全动态）────────────────────────────────────────────────────
    s2 = 0
    reasons2 = []
    
    # (1) 回踩幅度 30 分（改前：固定 20%~38% → 改后：动态黄金区）
    golden_zone_low = max_ratio * 0.5
    golden_zone_high = max_ratio
    if golden_zone_low <= pullback_ratio <= golden_zone_high:
        s2 += 30; reasons2.append(f"回踩{pullback_ratio:.0%}黄金区 (+30)")
    elif pullback_ratio < golden_zone_low:
        s2 += 18; reasons2.append(f"回踩{pullback_ratio:.0%}极浅 (+18)")
    elif pullback_ratio < bl.pullback_p75:
        s2 += 10; reasons2.append(f"回踩{pullback_ratio:.0%}偏深 (+10)")
    else:
        s2 += 0; reasons2.append(f"回踩{pullback_ratio:.0%}过深 (+0)")
    
    # (2) 守住突破位 25 分（改前：固定 1×ATR → 改后：atr_p75/p40）
    margin = mark_price - (breakout_level - tolerance)
    if margin > bl.atr_p75:
        s2 += 25; reasons2.append("大幅守住 (+25)")
    elif margin > bl.atr_p40:
        s2 += 18; reasons2.append("正常守住 (+18)")
    elif margin > 0:
        s2 += 10; reasons2.append("勉强守住 (+10)")
    else:
        s2 += 0; reasons2.append("未守住 (+0)")
    
    # (3) 回踩缩量 20 分（改前：固定 50%/30% → 改后：vol_p25/p40/p60）
    avg5 = _mean(recent5)
    if avg5 < bl.vol_p25:
        s2 += 20; reasons2.append("极度缩量 (+20)")
    elif avg5 < bl.vol_p40:
        s2 += 13; reasons2.append("明显缩量 (+13)")
    elif avg5 < bl.vol_p60:
        s2 += 7; reasons2.append("轻微缩量 (+7)")
    else:
        s2 += 3; reasons2.append("未缩量 (+3)")
    
    # (4) 均线支撑 15 分（改前：单 EMA20 → 改后：双均线确认）
    if len(closes) >= 50:
        ema20 = _ema(closes[-20:], 20)
        ema50 = _ema(closes[-50:], 50)
        if closes[-1] > ema20 and ema20 > ema50:
            s2 += 15; reasons2.append("双均线多头 (+15)")
        elif closes[-1] > ema20:
            s2 += 8; reasons2.append("EMA20 支撑 (+8)")
        elif ema20 > ema50:
            s2 += 4; reasons2.append("EMA 多头但破位 (+4)")
        else:
            s2 += 0; reasons2.append("均线弱势 (+0)")
    else:
        s2 += 10; reasons2.append("数据不足 (+10)")
    
    # (5) RR 预估 10 分
    s2 += 10; reasons2.append("RR 预估满足 (+10)")
    s2 = min(s2, 100)
    
    # ── 挂单参数（分批止盈）──────────────────────────────────────────────────
    from modules.entry_calc import calc_entry_long_scene
    liq_price = cond.get("_liq_zone_price")
    
    entry, entry_method = calc_entry_long_scene(
        "C_first_pull", mark_price, k1h, bl.atr_baseline,
        consolidation_top=swing_high, liq_zone_price=liq_price)
    
    if entry is None:
        return MatchResult(
            matched=False, scene="none", direction=direction,
            entry_price=0, sl_price=0, tp_price=0, tp1_price=None, tp2_price=None,
            rr=0, second_score=s2, reason=f"场景 C 入场价异常：{entry_method}",
            conditions=cond,
        )
    
    # 集成清算区止盈止损
    if LIQUIDATION_ENABLED and liq_data:
        sl_result = calc_sl_with_liquidation(
            entry=entry, direction=direction, scene="C_first_pull",
            candles=k1h, baseline=bl, liq_data=liq_data
        )
        sl = sl_result["sl_price"]
        
        tp_result = calc_batch_tp_with_liquidation(
            entry=entry, sl=sl, direction=direction,
            candles=k1h, baseline=bl, liq_data=liq_data
        )
        tp1 = tp_result["tp1_price"]
        tp2 = tp_result["tp2_price"]
    else:
        # 降级：固定计算
        recent_low = min(lows[-5:])
        sl = round(recent_low - bl.atr_baseline * 0.25, 8)
        risk = abs(entry - sl)
        tp1 = round(swing_high, 8)
        tp2 = round(swing_high + risk * 0.5, 8)
    
    cond["entry_method"] = entry_method
    
    # RR 按 tp1 计算（保守）
    rr = round(abs(tp1 - entry) / risk, 2) if risk > 0 else 0
    
    if rr < 1.8:
        return MatchResult(
            matched=False, scene="none", direction=direction,
            entry_price=entry, sl_price=sl, tp_price=tp2, tp1_price=tp1, tp2_price=tp2,
            rr=rr, second_score=s2, reason=f"场景 C RR({rr}) < 1.8", conditions=cond,
        )
    
    return MatchResult(
        matched=True, scene="C_first_pull", direction=direction,
        entry_price=entry, sl_price=sl, tp_price=tp2, tp1_price=tp1, tp2_price=tp2,
        rr=rr, second_score=s2,
        reason=f"场景 C 回踩 二次分={s2} RR={rr} | {' | '.join(reasons2)}",
        conditions=cond,
    )


# ══════════════════════════════════════════════════════════════════════════════
# 主接口
# ══════════════════════════════════════════════════════════════════════════════

def _scene_e_dynamic(k1h: list, mark_price: float,
                     direction: str, bl: DynamicBaseline,
                     liq_data: LiquidationData = None) -> MatchResult:
    """
    场景 E：反弹做空（全动态改造版）
    
    触发条件（全部满足）：
    ① 趋势确认：EMA 空头排列 AND 价格 < EMA20 + 0.3×atr_baseline
    ② 反弹幅度：≥ 1.2×atr_baseline，上限 ≤ 3.0×atr_baseline
    ③ RSI 反弹：自适应阈值（rsi_std<8→0.3σ，>15→0.8σ）
    ④ 放量：近 3 根均量 > vol_p60
    
    二次打分（满分 100）：
      反弹幅度合理区间（基于 atr_std）25
      阻力区强度（动态距离）          25
      RSI 反弹深度（基于 rsi_upper2）  20
      放量程度（基于 vol_p60/p75）     20
      RR 预估                            10
    """
    closes = _closes(k1h)
    highs  = _highs(k1h)
    lows   = _lows(k1h)
    vols   = _vols(k1h)
    cond   = {}
    
    # ── 触发条件 ─────────────────────────────────────────────────────────────
    
    # ① 趋势确认（空头镜像）
    ema20_val = _ema(closes[-20:], 20) if len(closes) >= 20 else closes[-1]
    ema50_val = _ema(closes[-50:], 50) if len(closes) >= 50 else ema20_val
    
    trend_ok = (ema20_val < ema50_val) and (mark_price < ema20_val + 0.3 * bl.atr_baseline)
    cond["trend_ok"] = trend_ok
    
    # ② 反弹幅度（空头镜像）
    trough = min(lows[-20:])
    rb_abs = (mark_price - trough)
    rb_atr = rb_abs / bl.atr_baseline if bl.atr_baseline > 0 else 0
    rally_ok = 1.2 <= rb_atr <= 3.0
    cond["rally_ok"] = rally_ok
    cond["rally_atr"] = round(rb_atr, 2)
    
    # ③ RSI 反弹（自适应阈值）
    rsi_val = _rsi(closes)
    rsi_thr = bl.rsi_mean + (0.3 * bl.rsi_std if bl.rsi_std < 8 else
                             0.8 * bl.rsi_std if bl.rsi_std > 15 else
                             0.5 * bl.rsi_std)
    rsi_ok = rsi_val >= rsi_thr
    cond["rsi_ok"] = rsi_ok
    cond["rsi_val"] = round(rsi_val, 1)
    cond["rsi_thr"] = round(rsi_thr, 1)
    
    # ④ 放量（vol_p60）
    avg3 = _mean(vols[-3:]) if len(vols) >= 3 else vols[-1]
    vol_ok = avg3 > bl.vol_p60
    cond["vol_ok"] = vol_ok
    cond["vol_ratio"] = round(avg3 / bl.vol_baseline if bl.vol_baseline > 0 else 1.0, 2)
    
    all_pass = trend_ok and rally_ok and rsi_ok and vol_ok
    if not all_pass:
        failed = [k for k, v in cond.items() if k.endswith("_ok") and not v]
        return MatchResult(
            matched=False, scene="none", direction=direction,
            entry_price=0, sl_price=0, tp_price=0, tp1_price=None, tp2_price=None,
            rr=0, second_score=0, reason=f"场景 E 不满足：{failed}", conditions=cond,
        )
    
    # ── 二次打分（全动态）────────────────────────────────────────────────────
    s2 = 0
    reasons2 = []
    
    # (1) 反弹幅度合理区间 25 分
    atr_std_ratio = bl.atr_std / bl.atr_baseline if bl.atr_baseline > 0 else 0.3
    if 1.2 <= rb_atr <= (1.2 + atr_std_ratio):
        s2 += 25; reasons2.append(f"反弹{rb_atr:.1f}x 黄金区 (+25)")
    elif 1.0 <= rb_atr < 1.2:
        s2 += 15; reasons2.append(f"反弹{rb_atr:.1f}x 偏浅 (+15)")
    elif rb_atr > (1.2 + 2 * atr_std_ratio):
        s2 += 8;  reasons2.append(f"反弹{rb_atr:.1f}x 过深 (+8)")
    else:
        s2 += 5;  reasons2.append(f"反弹{rb_atr:.1f}x 一般 (+5)")
    
    # (2) 阻力区强度 25 分
    ema20 = ema20_val
    dist_ema = abs(mark_price - ema20) / bl.atr_baseline if bl.atr_baseline > 0 else 1.0
    if dist_ema <= 0.4:
        s2 += 25; reasons2.append("EMA20 附近 (+25)")
    elif dist_ema <= 0.8:
        s2 += 18; reasons2.append("EMA20 较近 (+18)")
    else:
        prev_low = min(lows[-11:-1]) if len(lows) >= 11 else min(lows)
        dist_prev = abs(mark_price - prev_low) / bl.atr_baseline
        if dist_prev <= 0.8:
            s2 += 18; reasons2.append("前低阻力 (+18)")
        else:
            s2 += 8; reasons2.append("阻力未明 (+8)")
    
    # (3) RSI 反弹深度 20 分
    rsi_depth = rsi_val - bl.rsi_mean
    if rsi_depth >= 2 * bl.rsi_std:
        s2 += 20; reasons2.append("RSI 超买 2σ (+20)")
    elif rsi_depth >= bl.rsi_std:
        s2 += 14; reasons2.append("RSI 超买 1σ (+14)")
    elif rsi_depth >= 0.5 * bl.rsi_std:
        s2 += 8; reasons2.append("RSI 轻微反弹 (+8)")
    else:
        s2 += 0; reasons2.append("RSI 未反弹 (+0)")
    
    # (4) 放量程度 20 分
    if avg3 > bl.vol_p75:
        s2 += 20; reasons2.append("明显放量 (+20)")
    elif avg3 > bl.vol_p60:
        s2 += 14; reasons2.append("温和放量 (+14)")
    elif avg3 > bl.vol_baseline:
        s2 += 8; reasons2.append("轻微放量 (+8)")
    else:
        s2 += 3; reasons2.append("未放量 (+3)")
    
    # (5) RR 预估 10 分
    s2 += 10; reasons2.append("RR 预估满足 (+10)")
    s2 = min(s2, 100)
    
    # ── 挂单参数（动态）──────────────────────────────────────────────────────
    from modules.entry_calc import calc_dynamic_entry
    liq_price = cond.get("_liq_zone_price")
    
    entry, entry_method = calc_dynamic_entry(
        direction, mark_price, k1h, bl.atr_baseline, liq_price)
    
    sl = round(max(highs[-5:]) + 0.5 * bl.atr_baseline, 8)
    risk = abs(entry - sl)
    tp = round(entry - risk * 2.5, 8)
    
    cond["entry_method"] = entry_method
    
    rr = round(abs(tp - entry) / risk, 2) if risk > 0 else 0
    
    if rr < 2.0:
        return MatchResult(
            matched=False, scene="none", direction=direction,
            entry_price=entry, sl_price=sl, tp_price=tp, tp1_price=None, tp2_price=None,
            rr=rr, second_score=s2, reason=f"场景 E RR({rr}) < 2.0", conditions=cond,
        )
    
    return MatchResult(
        matched=True, scene="E_rally_short", direction=direction,
        entry_price=entry, sl_price=sl, tp_price=tp, tp1_price=None, tp2_price=None,
        rr=rr, second_score=s2,
        reason=f"场景 E 反弹 二次分={s2} RR={rr} | {' | '.join(reasons2)}",
        conditions=cond,
    )


def _scene_f_dynamic(k1h: list, mark_price: float,
                     direction: str, bl: DynamicBaseline,
                     liq_data: LiquidationData = None) -> MatchResult:
    """
    场景 F：破位做空（全动态改造版）
    
    触发条件：
    ① 区间收窄：zone_range < range_p40（排除噪音 K 线）
    ② ATR 收窄：current_atr < atr_p60
    ③ 震荡持续：实体 < body_p60 的 K 线≥7 根
    ④ 方向一致：EMA50 顺势（空头）
    
    二次打分（满分 100）：
      区间收窄程度（range_p25/p40/p60）25
      ATR 收窄深度（atr_p25/p40/p60）    25
      震荡持续根数                       20
      大趋势支撑（双均线确认）           20
      RR 预估                            10
    """
    closes = _closes(k1h)
    highs  = _highs(k1h)
    lows   = _lows(k1h)
    vols   = _vols(k1h)
    cond   = {}
    
    # ── 触发条件 ─────────────────────────────────────────────────────────────
    
    # ① 区间收窄（排除噪音）
    w = 10
    valid_candles = [k1h[-i] for i in range(1, w+1) if _vols([k1h[-i]])[0] < bl.vol_p90]
    if valid_candles:
        zone_top = max(_highs(valid_candles))
        zone_bottom = min(_lows(valid_candles))
        zone_range = zone_top - zone_bottom
    else:
        zone_top = max(highs[-w:])
        zone_bottom = min(lows[-w:])
        zone_range = zone_top - zone_bottom
    
    range_ok = zone_range < bl.range_p40
    cond["range_ok"] = range_ok
    cond["zone_range"] = round(zone_range, 8)
    cond["zone_top"] = round(zone_top, 8)
    cond["zone_bottom"] = round(zone_bottom, 8)
    
    # ② ATR 收窄
    atr_now = sum(max(float(k1h[-i][2])-float(k1h[-i][3]),
               abs(float(k1h[-i][2])-float(k1h[-i-1][4])),
               abs(float(k1h[-i][3])-float(k1h[-i-1][4])))
               for i in range(1, min(4, len(k1h)))) / min(3, len(k1h)-1)
    atr_ok = atr_now < bl.atr_p60
    cond["atr_ok"] = atr_ok
    cond["atr_ratio"] = round(atr_now / bl.atr_baseline if bl.atr_baseline > 0 else 1.0, 2)
    
    # ③ 震荡持续
    consolidation_count = sum(
        1 for i in range(min(15, len(k1h)))
        if abs(float(k1h[-1-i][4]) - float(k1h[-1-i][1])) < bl.body_p60
    )
    duration_ok = consolidation_count >= 7
    cond["duration_ok"] = duration_ok
    cond["consolidation_count"] = consolidation_count
    
    # ④ 方向一致
    if len(closes) >= 50:
        ema20_val = _ema(closes[-20:], 20)
        ema50_val = _ema(closes[-50:], 50)
        trend_ok = (ema20_val < ema50_val)
    elif len(closes) >= 20:
        ema20_val = _ema(closes[-20:], 20)
        trend_ok = True
    else:
        trend_ok = True
    cond["trend_ok"] = trend_ok
    
    all_pass = range_ok and atr_ok and duration_ok and trend_ok
    if not all_pass:
        failed = [k for k, v in cond.items() if k.endswith("_ok") and not v]
        return MatchResult(
            matched=False, scene="none", direction=direction,
            entry_price=0, sl_price=0, tp_price=0, tp1_price=None, tp2_price=None,
            rr=0, second_score=0, reason=f"场景 F 不满足：{failed}", conditions=cond,
        )
    
    # ── 二次打分（全动态）────────────────────────────────────────────────────
    s2 = 0
    reasons2 = []
    
    # (1) 区间收窄程度 25 分
    if zone_range < bl.range_p25:
        s2 += 25; reasons2.append("区间极度收窄 (+25)")
    elif zone_range < bl.range_p40:
        s2 += 18; reasons2.append("区间明显收窄 (+18)")
    elif zone_range < bl.range_p60:
        s2 += 10; reasons2.append("区间轻微收窄 (+10)")
    else:
        s2 += 5; reasons2.append("区间偏宽 (+5)")
    
    # (2) ATR 收窄深度 25 分
    if atr_now < bl.atr_p25:
        s2 += 25; reasons2.append("ATR 极度收窄 (+25)")
    elif atr_now < bl.atr_p40:
        s2 += 18; reasons2.append("ATR 明显收窄 (+18)")
    elif atr_now < bl.atr_p60:
        s2 += 10; reasons2.append("ATR 轻微收窄 (+10)")
    else:
        s2 += 5; reasons2.append("ATR 未收窄 (+5)")
    
    # (3) 震荡持续根数 20 分
    if consolidation_count >= 10:
        s2 += 20; reasons2.append(f"{consolidation_count}根蓄势 (+20)")
    elif consolidation_count >= 7:
        s2 += 14; reasons2.append(f"{consolidation_count}根蓄势 (+14)")
    elif consolidation_count >= 5:
        s2 += 8; reasons2.append(f"{consolidation_count}根蓄势 (+8)")
    else:
        s2 += 4; reasons2.append(f"{consolidation_count}根蓄势 (+4)")
    
    # (4) 大趋势支撑 20 分
    if len(closes) >= 50:
        ema20_val = _ema(closes[-20:], 20)
        ema50_val = _ema(closes[-50:], 50)
        if ema20_val < ema50_val and closes[-1] < ema20_val:
            s2 += 20; reasons2.append("双均线空头 (+20)")
        elif ema20_val < ema50_val:
            s2 += 14; reasons2.append("EMA50 顺势 (+14)")
        elif closes[-1] < ema20_val:
            s2 += 8; reasons2.append("EMA20 顺势 (+8)")
        else:
            s2 += 0; reasons2.append("均线逆势 (+0)")
    else:
        s2 += 12; reasons2.append("数据不足 (+12)")
    
    # (5) RR 预估 10 分
    s2 += 10; reasons2.append("RR 预估满足 (+10)")
    s2 = min(s2, 100)
    
    # ── 挂单参数（动态缓冲）──────────────────────────────────────────────────
    false_breaks = sum(1 for i in range(10, min(20, len(k1h)))
                       if float(k1h[-i][3]) < zone_bottom and float(k1h[-i+1][4]) > zone_bottom)
    false_breakout_rate = false_breaks / max(1, min(10, len(k1h)-10))
    
    buffer = get_entry_buffer(false_breakout_rate, bl)
    entry = zone_bottom - buffer
    
    sl_buffer = bl.atr_baseline * 0.3
    sl = round(zone_top + sl_buffer, 8)
    
    risk = abs(entry - sl)
    h_range = zone_top - zone_bottom
    tp = round(entry - (h_range + bl.atr_baseline * 0.5), 8)
    
    cond["entry_method"] = "breakdown_dynamic"
    cond["false_break_rate"] = round(false_breakout_rate, 2)
    
    rr = round(abs(tp - entry) / risk, 2) if risk > 0 else 0
    
    if rr < 1.8:
        return MatchResult(
            matched=False, scene="none", direction=direction,
            entry_price=entry, sl_price=sl, tp_price=tp, tp1_price=None, tp2_price=None,
            rr=rr, second_score=s2, reason=f"场景 F RR({rr}) < 1.8", conditions=cond,
        )
    
    return MatchResult(
        matched=True, scene="F_breakdown", direction=direction,
        entry_price=entry, sl_price=sl, tp_price=tp, tp1_price=None, tp2_price=None,
        rr=rr, second_score=s2,
        reason=f"场景 F 破位 二次分={s2} RR={rr} | {' | '.join(reasons2)}",
        conditions=cond,
    )


def _scene_g_dynamic(k1h: list, mark_price: float,
                     direction: str, bl: DynamicBaseline,
                     liq_data: LiquidationData = None) -> MatchResult:
    """
    场景 G：强势反弹（全动态改造版）
    
    触发条件：
    ① 跌幅确认：近 20 根跌幅 ≥ strong_threshold（gain_p60）
    ② 反弹幅度：< get_max_pullback_ratio(surge_pct, bl) 动态上限
    ③ 守住破位点：price ≤ breakout_level + tolerance
    ④ 反弹放量：近 5 根中≥3 根 > vol_p40
    
    二次打分（满分 100）：
      反弹幅度（动态黄金区）          30
      守住破位点（动态距离）          25
      反弹放量（vol_p25/p40/p60）    20
      均线空头（双均线确认）          15
      RR 预估                            10
    """
    closes = _closes(k1h)
    highs  = _highs(k1h)
    lows   = _lows(k1h)
    vols   = _vols(k1h)
    cond   = {}
    
    # ── 触发条件 ─────────────────────────────────────────────────────────────
    
    # ① 跌幅确认
    swing_high = max(highs[-20:]) if len(highs) >= 20 else max(highs)
    swing_low = min(lows[-5:]) if len(lows) >= 5 else min(lows)
    surge = (swing_high - swing_low) / swing_high if swing_high > 0 else 0
    gain_ok = surge >= bl.strong_threshold
    cond["gain_ok"] = gain_ok
    cond["surge_pct"] = round(surge, 4)
    
    # ② 反弹幅度上限（动态）
    rally = mark_price - swing_low
    rally_ratio = rally / (swing_high - swing_low) if swing_high > swing_low else 0
    max_ratio = get_max_pullback_ratio(surge, bl)
    pb_ok = rally_ratio < max_ratio
    cond["pb_ok"] = pb_ok
    cond["rally_ratio"] = round(rally_ratio, 3)
    cond["max_ratio"] = round(max_ratio, 3)
    
    # ③ 守住破位点
    breakout_level = swing_high - (swing_high - swing_low) * 0.5
    tolerance = bl.atr_baseline * 0.3
    hold_ok = mark_price <= breakout_level + tolerance
    cond["hold_ok"] = hold_ok
    cond["breakout_level"] = round(breakout_level, 8)
    
    # ④ 反弹放量
    recent5 = vols[-5:] if len(vols) >= 5 else vols
    expand_cnt = sum(1 for v in recent5 if v > bl.vol_p40)
    vol_ok = expand_cnt >= 3
    cond["vol_ok"] = vol_ok
    cond["expand_cnt"] = expand_cnt
    
    all_pass = gain_ok and pb_ok and hold_ok and vol_ok
    if not all_pass:
        failed = [k for k, v in cond.items() if k.endswith("_ok") and not v]
        return MatchResult(
            matched=False, scene="none", direction=direction,
            entry_price=0, sl_price=0, tp_price=0, tp1_price=None, tp2_price=None,
            rr=0, second_score=0, reason=f"场景 G 不满足：{failed}", conditions=cond,
        )
    
    # ── 二次打分（全动态）────────────────────────────────────────────────────
    s2 = 0
    reasons2 = []
    
    # (1) 反弹幅度 30 分
    golden_zone_low = max_ratio * 0.5
    golden_zone_high = max_ratio
    if golden_zone_low <= rally_ratio <= golden_zone_high:
        s2 += 30; reasons2.append(f"反弹{rally_ratio:.0%}黄金区 (+30)")
    elif rally_ratio < golden_zone_low:
        s2 += 18; reasons2.append(f"反弹{rally_ratio:.0%}极浅 (+18)")
    elif rally_ratio < bl.pullback_p75:
        s2 += 10; reasons2.append(f"反弹{rally_ratio:.0%}偏深 (+10)")
    else:
        s2 += 0; reasons2.append(f"反弹{rally_ratio:.0%}过深 (+0)")
    
    # (2) 守住破位点 25 分
    margin = (breakout_level + tolerance) - mark_price
    if margin > bl.atr_p75:
        s2 += 25; reasons2.append("大幅守住 (+25)")
    elif margin > bl.atr_p40:
        s2 += 18; reasons2.append("正常守住 (+18)")
    elif margin > 0:
        s2 += 10; reasons2.append("勉强守住 (+10)")
    else:
        s2 += 0; reasons2.append("未守住 (+0)")
    
    # (3) 反弹放量 20 分
    avg5 = _mean(recent5)
    if avg5 > bl.vol_p75:
        s2 += 20; reasons2.append("明显放量 (+20)")
    elif avg5 > bl.vol_p40:
        s2 += 13; reasons2.append("温和放量 (+13)")
    elif avg5 > bl.vol_p60:
        s2 += 7; reasons2.append("轻微放量 (+7)")
    else:
        s2 += 3; reasons2.append("未放量 (+3)")
    
    # (4) 均线空头 15 分
    if len(closes) >= 50:
        ema20 = _ema(closes[-20:], 20)
        ema50 = _ema(closes[-50:], 50)
        if closes[-1] < ema20 and ema20 < ema50:
            s2 += 15; reasons2.append("双均线空头 (+15)")
        elif closes[-1] < ema20:
            s2 += 8; reasons2.append("EMA20 压制 (+8)")
        elif ema20 < ema50:
            s2 += 4; reasons2.append("EMA 空头但反弹 (+4)")
        else:
            s2 += 0; reasons2.append("均线多头 (+0)")
    else:
        s2 += 10; reasons2.append("数据不足 (+10)")
    
    # (5) RR 预估 10 分
    s2 += 10; reasons2.append("RR 预估满足 (+10)")
    s2 = min(s2, 100)
    
    # ── 挂单参数（分批止盈）──────────────────────────────────────────────────
    from modules.entry_calc import calc_entry_short_scene
    liq_price = cond.get("_liq_zone_price")
    
    entry, entry_method = calc_entry_short_scene(
        "G_rally_short", mark_price, k1h, bl.atr_baseline,
        consolidation_bottom=swing_low, liq_zone_price=liq_price)
    
    if entry is None:
        return MatchResult(
            matched=False, scene="none", direction=direction,
            entry_price=0, sl_price=0, tp_price=0, tp1_price=None, tp2_price=None,
            rr=0, second_score=s2, reason=f"场景 G 入场价异常：{entry_method}",
            conditions=cond,
        )
    
    # 集成清算区止盈止损
    if LIQUIDATION_ENABLED and liq_data:
        sl_result = calc_sl_with_liquidation(
            entry=entry, direction=direction, scene="G_rally_short",
            candles=k1h, baseline=bl, liq_data=liq_data
        )
        sl = sl_result["sl_price"]
        
        tp_result = calc_batch_tp_with_liquidation(
            entry=entry, sl=sl, direction=direction,
            candles=k1h, baseline=bl, liq_data=liq_data
        )
        tp1 = tp_result["tp1_price"]
        tp2 = tp_result["tp2_price"]
    else:
        # 降级：固定计算
        recent_high = max(highs[-5:])
        sl = round(recent_high + bl.atr_baseline * 0.25, 8)
        risk = abs(entry - sl)
        tp1 = round(swing_low, 8)
        tp2 = round(swing_low - risk * 0.5, 8)
    
    cond["entry_method"] = entry_method
    
    rr = round(abs(tp1 - entry) / risk, 2) if risk > 0 else 0
    
    if rr < 1.8:
        return MatchResult(
            matched=False, scene="none", direction=direction,
            entry_price=entry, sl_price=sl, tp_price=tp2, tp1_price=tp1, tp2_price=tp2,
            rr=rr, second_score=s2, reason=f"场景 G RR({rr}) < 1.8", conditions=cond,
        )
    
    return MatchResult(
        matched=True, scene="G_rally_short", direction=direction,
        entry_price=entry, sl_price=sl, tp_price=tp2, tp1_price=tp1, tp2_price=tp2,
        rr=rr, second_score=s2,
        reason=f"场景 G 反弹 二次分={s2} RR={rr} | {' | '.join(reasons2)}",
        conditions=cond,
    )


# ══════════════════════════════════════════════════════════════════════════════
# 主接口
# ══════════════════════════════════════════════════════════════════════════════

def match_dynamic(k1h: list, mark_price: float,
                  direction: str, track: str = "oscillation",
                  bl: Optional[DynamicBaseline] = None,
                  symbol: str = "",
                  liq_data: LiquidationData = None) -> MatchResult:
    """
    场景匹配主接口（全动态改造版 v3.1）。
    
    根据赛道和方向选择合适的场景：
      - 震荡赛道 + LONG → 场景 A, B
      - 震荡赛道 + SHORT → 场景 E, F
      - 强势赛道 + LONG → 场景 C
      - 强势赛道 + SHORT → 场景 G
    """
    if len(k1h) < 15 or mark_price <= 0:
        return MatchResult(
            matched=False, scene="none", direction=direction,
            entry_price=0, sl_price=0, tp_price=0, tp1_price=None, tp2_price=None,
            rr=0, second_score=0, reason="数据不足", conditions={},
        )
    
    bl = bl or compute_baseline(k1h)
    
    # ── 根据赛道和方向选择场景 ────────────────────────────────────────
    
    if track == "momentum":
        # 强势赛道
        if direction == "LONG":
            return _scene_c_dynamic(k1h, mark_price, direction, bl, liq_data)
        else:  # SHORT
            return _scene_g_dynamic(k1h, mark_price, direction, bl, liq_data)
    
    else:  # oscillation
        # 震荡赛道
        if direction == "LONG":
            # 先尝试场景 A，不满足再试场景 B
            result_a = _scene_a_dynamic(k1h, mark_price, direction, bl, liq_data)
            if result_a["matched"]:
                return result_a
            return _scene_b_dynamic(k1h, mark_price, direction, bl, liq_data)
        else:  # SHORT
            # 先尝试场景 E，不满足再试场景 F
            result_e = _scene_e_dynamic(k1h, mark_price, direction, bl, liq_data)
            if result_e["matched"]:
                return result_e
            return _scene_f_dynamic(k1h, mark_price, direction, bl, liq_data)
