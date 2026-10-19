"""
modules/sl_handler_liquidation.py — 止盈止损计算（集成清算图数据 v3.2）

核心原则：
  清算区 = 流动性池 = 价格磁铁
  这比 RSI/均线/ATR 更直接反映市场力量

清算图的本质：
  多头清算区（Long Liquidations）：
    - 大量多单的强平价格集中在某个价格下方
    - 做市商/大资金会主动打压到这里触发连锁清算
    - 清算完成后往往快速反弹（流动性被获取完毕）
  
  空头清算区（Short Liquidations）：
    - 大量空单的强平价格集中在某个价格上方
    - 价格会被推上去触发空单连锁清算
    - 清算完成后往往快速回落

止盈优先级：
  ① 清算区（量大 + 满足 min_rr）→ 最优先，磁性最强
  ② 清算区（量普通 + 满足 min_rr）→ 次优先
  ③ 市场结构位（阻力/支撑 + 满足 min_rr）→ 再次
  ④ 固定 RR 兜底（2×RR）→ 最后手段

止损优先级：
  ① 结构止损位（基础）
  ② 如果清算区在止损路径上（量大）→ 止损前移到清算区外侧
  ③ 动态缓冲（atr_std 决定缓冲大小）

禁止入场：
  entry 和 tp 之间存在反向巨大清算区（量>vol_baseline×10）
  → 该清算区会把价格打回来，止盈永远触不到
  → 直接返回 None，进观察池
"""

from __future__ import annotations
from typing import TypedDict, Optional, List
from modules.dynamic_baseline import DynamicBaseline
from modules.liquidation_data import LiquidationData, LiquidationZone


class TPResult(TypedDict):
    """止盈计算结果"""
    tp_price: float
    tp_source: str         # "liq_zone" / "structure" / "rr_fallback"
    tp_rr: float           # 实际盈亏比
    liq_size: float        # 清算量（如果是清算区）


class SLResult(TypedDict):
    """止损计算结果"""
    sl_price: float
    sl_source: str         # "structure" / "liq_adjusted"
    sl_buffer: float       # 缓冲大小
    adjusted_by_liq: bool  # 是否被清算区调整过


class BatchTPResult(TypedDict):
    """分批止盈结果"""
    tp1_price: float
    tp2_price: float
    tp1_source: str
    tp2_source: str
    tp1_rr: float
    tp2_rr: float


# ── 工具函数 ──────────────────────────────────────────────────────────────────

def _find_nearest_resistance(candles: list, current_price: float, atr: float) -> Optional[float]:
    """寻找当前价上方最近的阻力位"""
    if not candles or len(candles) < 5:
        return None
    
    highs = [float(c[2]) for c in candles]
    
    # 近 5 根最高价
    recent_high = max(highs[-5:])
    if recent_high > current_price:
        return recent_high
    
    # 前期高点（近 20 根排除近 5 根）
    if len(highs) >= 20:
        prev_high = max(highs[-20:-5])
        if prev_high > current_price:
            return prev_high
    
    return None


def _find_nearest_support(candles: list, current_price: float, atr: float) -> Optional[float]:
    """寻找当前价下方最近的支撑位"""
    if not candles or len(candles) < 5:
        return None
    
    lows = [float(c[3]) for c in candles]
    
    # 近 5 根最低价
    recent_low = min(lows[-5:])
    if recent_low < current_price:
        return recent_low
    
    # 前期低点（近 20 根排除近 5 根）
    if len(lows) >= 20:
        prev_low = min(lows[-20:-5])
        if prev_low < current_price:
            return prev_low
    
    return None


def _calc_sl_buffer(baseline: DynamicBaseline, scene: str) -> float:
    """
    动态计算止损缓冲。
    
    基于 atr_std 决定缓冲大小：
    - atr_std/at r_baseline > 0.5（波动率离散度高）→ 0.5×ATR
    - 否则 → 0.25×ATR（场景 C/G）或 0.3×ATR（场景 B/F）
    
    修复 1：加硬下限 buffer >= 0.5×ATR，避免止损过近
    """
    atr_ratio = baseline.atr_std / baseline.atr_baseline if baseline.atr_baseline > 0 else 0.3
    
    if atr_ratio > 0.5:
        buffer = 0.5 * baseline.atr_baseline
    elif scene in ("C_first_pull", "G_rally_short"):
        buffer = 0.25 * baseline.atr_baseline
    elif scene in ("B_breakout", "F_breakdown"):
        buffer = 0.3 * baseline.atr_baseline
    else:
        buffer = 0.5 * baseline.atr_baseline
    
    # 修复 1：硬下限 0.5×ATR
    buffer = max(buffer, baseline.atr_baseline * 0.5)
    return buffer


# ══════════════════════════════════════════════════════════════════════════════
# 止盈计算（集成清算区）
# ══════════════════════════════════════════════════════════════════════════════

def calc_tp_with_liquidation(
    entry: float,
    sl: float,
    direction: str,
    candles: list,
    baseline: DynamicBaseline,
    liq_data: Optional[LiquidationData],
    min_rr: float = 2.0,
    scene: str = "",
) -> TPResult:
    """
    止盈计算（优先使用清算区）。
    
    决策逻辑：
    1. 找 entry 上方的所有清算区（空头清算）/下方的所有清算区（多头清算）
    2. 按距离排序（最近的先考虑）
    3. 找第一个满足 min_rr 的清算区
    4. 找市场结构目标位（阻力位/支撑位）
    5. 综合决策：
       - 两者都有：选更近的那个，但如果清算区量级远大于结构位，优先选清算区
       - 只有清算区：用清算区
       - 只有结构位：用结构位
       - 都没有：2×RR 兜底
    """
    risk = abs(entry - sl)
    
    # ── 多头止盈 ─────────────────────────────────────────────────────────────
    if direction == "LONG":
        # 第一步：找 entry 上方的所有清算区（空头清算）
        if liq_data:
            short_liq_zones = [
                z for z in liq_data.short_liquidations
                if z.price > entry
            ]
            # 按距离排序（最近的先考虑）
            short_liq_zones.sort(key=lambda z: z.price)
        else:
            short_liq_zones = []
        
        # 第二步：找第一个满足 min_rr 的清算区
        liq_tp = None
        liq_tp_rr = 0.0
        liq_tp_size = 0.0
        
        for zone in short_liq_zones:
            actual_rr = (zone.price - entry) / risk if risk > 0 else 0
            if actual_rr >= min_rr:
                liq_tp = zone.price
                liq_tp_rr = actual_rr
                liq_tp_size = zone.liquidation_amount
                break
        
        # 第三步：找市场结构目标位（阻力位/前高）
        structure_tp = _find_nearest_resistance(candles, entry, baseline.atr_baseline)
        
        # 第四步：综合决策
        tp = 0.0
        tp_source = ""
        
        if liq_tp and structure_tp:
            # 两者都有：选更近的那个（先拿到钱再说）
            # 但如果清算区量级远大于结构位，优先选清算区
            if liq_tp_size > baseline.vol_baseline * 5:
                # 清算量巨大，价格磁性极强，优先用清算区
                tp = liq_tp
                tp_source = "liq_zone"
            else:
                # 清算量普通，选两者中更近的
                if liq_tp < structure_tp:
                    tp = liq_tp
                    tp_source = "liq_zone"
                else:
                    tp = structure_tp
                    tp_source = "structure"
        elif liq_tp:
            tp = liq_tp
            tp_source = "liq_zone"
        elif structure_tp:
            tp = structure_tp
            tp_source = "structure"
        else:
            # 兜底：2×RR
            tp = entry + risk * 2.0
            tp_source = "rr_fallback"
        
        final_rr = (tp - entry) / risk if risk > 0 else 0
        
        return TPResult(
            tp_price=round(tp, 8),
            tp_source=tp_source,
            tp_rr=round(final_rr, 2),
            liq_size=round(liq_tp_size, 0),
        )
    
    # ── 空头止盈 ─────────────────────────────────────────────────────────────
    elif direction == "SHORT":
        # 找 entry 下方的多头清算区
        if liq_data:
            long_liq_zones = [
                z for z in liq_data.long_liquidations
                if z.price < entry
            ]
            # 按距离排序（最近的先考虑，从高到低）
            long_liq_zones.sort(key=lambda z: z.price, reverse=True)
        else:
            long_liq_zones = []
        
        liq_tp = None
        liq_tp_rr = 0.0
        liq_tp_size = 0.0
        
        for zone in long_liq_zones:
            actual_rr = (entry - zone.price) / risk if risk > 0 else 0
            if actual_rr >= min_rr:
                liq_tp = zone.price
                liq_tp_rr = actual_rr
                liq_tp_size = zone.liquidation_amount
                break
        
        structure_tp = _find_nearest_support(candles, entry, baseline.atr_baseline)
        
        # 综合决策
        tp = 0.0
        tp_source = ""
        
        if liq_tp and structure_tp:
            if liq_tp_size > baseline.vol_baseline * 5:
                tp = liq_tp
                tp_source = "liq_zone"
            else:
                # 空头选更近（更高）的
                if liq_tp > structure_tp:
                    tp = liq_tp
                    tp_source = "liq_zone"
                else:
                    tp = structure_tp
                    tp_source = "structure"
        elif liq_tp:
            tp = liq_tp
            tp_source = "liq_zone"
        elif structure_tp:
            tp = structure_tp
            tp_source = "structure"
        else:
            tp = entry - risk * 2.0
            tp_source = "rr_fallback"
        
        final_rr = (entry - tp) / risk if risk > 0 else 0
        
        return TPResult(
            tp_price=round(tp, 8),
            tp_source=tp_source,
            tp_rr=round(final_rr, 2),
            liq_size=round(liq_tp_size, 0),
        )
    
    else:
        return TPResult(
            tp_price=0.0,
            tp_source="none",
            tp_rr=0.0,
            liq_size=0.0,
        )


# ══════════════════════════════════════════════════════════════════════════════
# 止损计算（集成清算区）
# ══════════════════════════════════════════════════════════════════════════════

def calc_sl_with_liquidation(
    entry: float,
    direction: str,
    scene: str,
    candles: list,
    baseline: DynamicBaseline,
    liq_data: Optional[LiquidationData],
    structure_sl: Optional[float] = None,
) -> SLResult:
    """
    止损计算（用清算区做保护盾）。
    
    逻辑：
    1. 先计算基础止损位（市场结构）
    2. 检查结构止损路径上是否有大清算区
    3. 有大清算区（量>vol_baseline×3）→ 止损前移到清算区外侧
    4. 没有 → 保持结构止损不变
    """
    # ── 基础止损位（市场结构）───────────────────────────────────────────────
    if structure_sl is None:
        highs = [float(c[2]) for c in candles]
        lows = [float(c[3]) for c in candles]
        
        if direction == "LONG":
            # 场景 A/E：近 5 根极值点
            # 场景 B/F：区间另一侧
            # 场景 C/G：近 5 根极值点（收紧版）
            if scene in ("C_first_pull", "G_rally_short"):
                base_sl = min(lows[-5:]) if len(lows) >= 5 else min(lows)
                buffer = 0.25 * baseline.atr_baseline
            elif scene in ("B_breakout", "F_breakdown"):
                # 区间另一侧（需要外部传入）
                base_sl = min(lows[-10:]) if len(lows) >= 10 else min(lows)
                buffer = 0.3 * baseline.atr_baseline
            else:
                base_sl = min(lows[-5:]) if len(lows) >= 5 else min(lows)
                buffer = 0.5 * baseline.atr_baseline
            
            structure_sl = round(base_sl - buffer, 8)
        else:  # SHORT
            if scene in ("C_first_pull", "G_rally_short"):
                base_sl = max(highs[-5:]) if len(highs) >= 5 else max(highs)
                buffer = 0.25 * baseline.atr_baseline
            elif scene in ("B_breakout", "F_breakdown"):
                base_sl = max(highs[-10:]) if len(highs) >= 10 else max(highs)
                buffer = 0.3 * baseline.atr_baseline
            else:
                base_sl = max(highs[-5:]) if len(highs) >= 5 else max(highs)
                buffer = 0.5 * baseline.atr_baseline
            
            structure_sl = round(base_sl + buffer, 8)
    
    # ── 清算区调整止损 ─────────────────────────────────────────────────────
    adjusted_by_liq = False
    sl = structure_sl
    sl_buffer = abs(structure_sl - entry) * 0.1 if entry != 0 else baseline.atr_baseline * 0.3
    
    if direction == "LONG":
        # 找 entry 下方的多头清算区（这些是危险区）
        if liq_data:
            long_liq_below = [
                z for z in liq_data.long_liquidations
                if z.price < entry
            ]
            # 按距离排序（离 entry 最近的优先）
            long_liq_below.sort(key=lambda z: z.price, reverse=True)
            
            if long_liq_below:
                nearest_liq = long_liq_below[0]
                liq_price = nearest_liq.price
                liq_size = nearest_liq.liquidation_amount
                
                # 情况 1：清算区在结构止损位上方
                # 说明止损会先穿过清算区才出场
                # 价格大概率会在清算区触发后反弹（我们白止损了）
                # → 止损上移到清算区上方，在清算区被触发前就出
                if structure_sl < liq_price < entry:
                    if liq_size > baseline.vol_baseline * 3:
                        # 大清算区：价格会在这里大幅波动，止损放清算区上方
                        buffer = _calc_sl_buffer(baseline, scene)
                        adjusted_sl = round(liq_price + buffer, 8)
                        # 但不能比结构止损更差（更远离 entry）
                        sl = max(adjusted_sl, structure_sl)
                        adjusted_by_liq = True
                        sl_buffer = buffer
    
    elif direction == "SHORT":
        # 找 entry 上方的空头清算区（危险区）
        if liq_data:
            short_liq_above = [
                z for z in liq_data.short_liquidations
                if z.price > entry
            ]
            # 按距离排序（离 entry 最近的优先）
            short_liq_above.sort(key=lambda z: z.price)
            
            if short_liq_above:
                nearest_liq = short_liq_above[0]
                liq_price = nearest_liq.price
                liq_size = nearest_liq.liquidation_amount
                
                if entry < liq_price < structure_sl:
                    if liq_size > baseline.vol_baseline * 3:
                        # 大清算区在止损路径上：止损移到清算区下方
                        buffer = _calc_sl_buffer(baseline, scene)
                        adjusted_sl = round(liq_price - buffer, 8)
                        sl = min(adjusted_sl, structure_sl)
                        adjusted_by_liq = True
                        sl_buffer = buffer
    
    return SLResult(
        sl_price=sl,
        sl_source="liq_adjusted" if adjusted_by_liq else "structure",
        sl_buffer=round(sl_buffer, 8),
        adjusted_by_liq=adjusted_by_liq,
    )


# ══════════════════════════════════════════════════════════════════════════════
# 分批止盈（场景 C/G，集成清算区）
# ══════════════════════════════════════════════════════════════════════════════

def calc_batch_tp_with_liquidation(
    entry: float,
    sl: float,
    direction: str,
    candles: list,
    baseline: DynamicBaseline,
    liq_data: Optional[LiquidationData],
) -> BatchTPResult:
    """
    分批止盈计算（场景 C/G，集成清算区）。
    
    tp1：第一个满足 RR≥1.2 的清算区（先锁利润）
    tp2：量最大的清算区（终极目标，价格磁性最强）
    无清算区数据时降级用 swing_high/low
    """
    risk = abs(entry - sl)
    
    if direction == "LONG":
        # 找 entry 上方的所有空头清算区，按大小排序
        if liq_data:
            short_liq_zones = sorted(
                [z for z in liq_data.short_liquidations if z.price > entry],
                key=lambda z: z.liquidation_amount,
                reverse=True  # 量最大的优先
            )
        else:
            short_liq_zones = []
        
        # tp1：第一个满足 RR≥1.2 的清算区（按距离排序）
        tp1 = None
        tp1_rr = 0.0
        
        for zone in sorted(short_liq_zones, key=lambda z: z.price):
            rr = (zone.price - entry) / risk if risk > 0 else 0
            if rr >= 1.2:
                tp1 = zone.price
                tp1_rr = rr
                break
        
        # tp2：量最大的清算区（终极目标）
        tp2 = None
        tp2_rr = 0.0
        
        for zone in short_liq_zones:
            rr = (zone.price - entry) / risk if risk > 0 else 0
            if rr >= 1.8:
                tp2 = zone.price
                tp2_rr = rr
                break
        
        # 兜底：没有清算区数据时用 swing_high
        if tp1 is None:
            highs = [float(c[2]) for c in candles]
            tp1 = max(highs[-5:]) if len(highs) >= 5 else max(highs)
            tp1_rr = (tp1 - entry) / risk if risk > 0 else 1.2
        
        if tp2 is None:
            tp2 = tp1 + risk * 0.5
            tp2_rr = tp1_rr + 0.5
        
        return BatchTPResult(
            tp1_price=round(tp1, 8),
            tp2_price=round(tp2, 8),
            tp1_source="liq_zone" if liq_data and any(z.price == tp1 for z in liq_data.short_liquidations) else "structure",
            tp2_source="liq_zone" if liq_data and any(z.price == tp2 for z in liq_data.short_liquidations) else "structure",
            tp1_rr=round(tp1_rr, 2),
            tp2_rr=round(tp2_rr, 2),
        )
    
    elif direction == "SHORT":
        # 找 entry 下方的多头清算区，按大小排序
        if liq_data:
            long_liq_zones = sorted(
                [z for z in liq_data.long_liquidations if z.price < entry],
                key=lambda z: z.liquidation_amount,
                reverse=True
            )
        else:
            long_liq_zones = []
        
        # tp1：第一个满足 RR≥1.2 的清算区（按距离排序，从高到低）
        tp1 = None
        tp1_rr = 0.0
        
        for zone in sorted(long_liq_zones, key=lambda z: z.price, reverse=True):
            rr = (entry - zone.price) / risk if risk > 0 else 0
            if rr >= 1.2:
                tp1 = zone.price
                tp1_rr = rr
                break
        
        # tp2：量最大的清算区（终极目标）
        tp2 = None
        tp2_rr = 0.0
        
        for zone in long_liq_zones:
            rr = (entry - zone.price) / risk if risk > 0 else 0
            if rr >= 1.8:
                tp2 = zone.price
                tp2_rr = rr
                break
        
        # 兜底
        if tp1 is None:
            lows = [float(c[3]) for c in candles]
            tp1 = min(lows[-5:]) if len(lows) >= 5 else min(lows)
            tp1_rr = (entry - tp1) / risk if risk > 0 else 1.2
        
        if tp2 is None:
            tp2 = tp1 - risk * 0.5
            tp2_rr = tp1_rr + 0.5
        
        return BatchTPResult(
            tp1_price=round(tp1, 8),
            tp2_price=round(tp2, 8),
            tp1_source="liq_zone" if liq_data and any(z.price == tp1 for z in liq_data.long_liquidations) else "structure",
            tp2_source="liq_zone" if liq_data and any(z.price == tp2 for z in liq_data.long_liquidations) else "structure",
            tp1_rr=round(tp1_rr, 2),
            tp2_rr=round(tp2_rr, 2),
        )
    
    else:
        return BatchTPResult(
            tp1_price=0.0,
            tp2_price=0.0,
            tp1_source="none",
            tp2_source="none",
            tp1_rr=0.0,
            tp2_rr=0.0,
        )


# ══════════════════════════════════════════════════════════════════════════════
# 入场过滤（清算区阻挡检查）
# ══════════════════════════════════════════════════════════════════════════════

def check_liquidation_block(
    entry: float,
    tp: float,
    direction: str,
    liq_data: Optional[LiquidationData],
    baseline: DynamicBaseline,
) -> tuple[bool, str]:
    """
    检查 entry 和 tp 之间是否存在反向巨大清算区。
    
    如果存在（量>vol_baseline×10）：
    → 该清算区会把价格打回来
    → 止盈永远触不到
    → 返回 (True, "blocked")，信号应进观察池
    
    返回：(is_blocked, reason)
    """
    if not liq_data:
        return False, "no_data"
    
    if direction == "LONG":
        # 找 entry 和 tp 之间的多头清算区（反向清算）
        long_liq_between = [
            z for z in liq_data.long_liquidations
            if entry < z.price < tp
        ]
        
        # 检查是否有巨大清算区
        for zone in long_liq_between:
            if zone.liquidation_amount > baseline.vol_baseline * 10:
                return True, f"liq_block_long_{zone.price}"
    
    elif direction == "SHORT":
        # 找 entry 和 tp 之间的空头清算区（反向清算）
        short_liq_between = [
            z for z in liq_data.short_liquidations
            if tp < z.price < entry
        ]
        
        for zone in short_liq_between:
            if zone.liquidation_amount > baseline.vol_baseline * 10:
                return True, f"liq_block_short_{zone.price}"
    
    return False, "clear"
