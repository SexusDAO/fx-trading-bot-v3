"""
entry_calc.py — 入场价计算模块 (P1)

三级优先 + 四项验证：

  select_entry_mode(score, has_liq_zone, just_broke_out, vol_ratio, rsi) → mode
    → CHASE / DUAL_NET / LIMIT / OBS

  calc_entry_price(symbol, direction, mark_price, k1h, liq_zone=None) → (price, method)

  validate_entry(entry_price, mark_price, k1h, direction, liq_zones=None) → (ok, reason)
"""

import math
from typing import Optional

# ── 工具函数 ──────────────────────────────────────────────────────────────────

def _closes(klines): return [float(k[4]) for k in klines]
def _highs(klines):  return [float(k[2]) for k in klines]
def _lows(klines):   return [float(k[3]) for k in klines]

def _ema(data, n):
    k = 2 / (n + 1); e = data[0]
    for x in data[1:]: e = x * k + e * (1 - k)
    return e

def _atr(klines, n=14):
    trs = []
    for i in range(1, len(klines)):
        h = float(klines[i][2]); lo = float(klines[i][3]); pc = float(klines[i-1][4])
        trs.append(max(h-lo, abs(h-pc), abs(lo-pc)))
    return sum(trs[-n:]) / n if trs else 0

def _vol_ratio(klines, n=10):
    vols = [float(k[5]) for k in klines]
    if len(vols) < n + 1: return 1.0
    avg = sum(vols[-n-1:-1]) / n
    return vols[-1] / avg if avg > 0 else 1.0

def _bb_width_pct(closes, n=20):
    if len(closes) < n: return 10.0
    window = closes[-n:]
    mid = sum(window) / n
    std = math.sqrt(sum((x-mid)**2 for x in window) / n)
    return (std * 4) / mid * 100  # BB宽度%


# ── 模式选择（四路互斥）────────────────────────────────────────────────────────

def select_entry_mode(score: int,
                      has_liq_zone: bool,
                      just_broke_out: bool,
                      vr: float,
                      rsi_val: float) -> str:
    """
    返回：CHASE / DUAL_NET / LIMIT / OBS
    按优先级顺序判断，第一个满足的执行。
    """
    # 路径 A：CHASE
    if score >= 90 and just_broke_out and vr >= 1.5 and rsi_val < 65:
        return "CHASE"

    # 路径 B：DUAL_NET
    if score >= 85 and has_liq_zone:
        return "DUAL_NET"

    # 路径 C/D：LIMIT（有/无清算区都挂单，只是锚点不同）
    if score >= 65:
        return "LIMIT"

    # 路径 E：观察区
    return "OBS"


# ── 动态搜索范围 ─────────────────────────────────────────────────────────────

def calc_dynamic_range(mark_price: float, k1h: list) -> float:
    """
    返回最大偏移比例（0.01 ~ 0.15）
    """
    closes = _closes(k1h)
    atr_val = _atr(k1h)
    base = atr_val / mark_price if mark_price > 0 else 0.02

    bb_w = _bb_width_pct(closes)
    bb_coef = 0.8 if bb_w < 5 else (1.3 if bb_w > 15 else 1.0)

    vr = _vol_ratio(k1h)
    vol_coef = 0.9 if vr < 0.5 else (1.4 if vr > 2.0 else 1.0)

    dynamic = base * bb_coef * vol_coef
    return max(0.01, min(0.15, dynamic))


# ── 技术位竞选 ────────────────────────────────────────────────────────────────

def _find_tech_level(direction: str, mark_price: float, k1h: list,
                     max_offset: float) -> Optional[float]:
    """
    多源技术位竞选，返回距现价最近且在动态范围内的锚点。
    direction: LONG → 找支撑（低于现价）；SHORT → 找压力（高于现价）
    """
    closes = _closes(k1h)
    lows_   = _lows(k1h)
    highs_  = _highs(k1h)

    candidates = []

    # 来源1：近100根K线前低/前高
    window_lows  = sorted(lows_[-100:])
    window_highs = sorted(highs_[-100:], reverse=True)

    if direction == "LONG":
        for lo in window_lows[:5]:
            if lo < mark_price:
                candidates.append(lo)
    else:
        for hi in window_highs[:5]:
            if hi > mark_price:
                candidates.append(hi)

    # 来源2：MA25 / MA99
    if len(closes) >= 25:
        ma25 = sum(closes[-25:]) / 25
        candidates.append(ma25)
    if len(closes) >= 99:
        ma99 = sum(closes[-99:]) / 99
        candidates.append(ma99)

    # 过滤：在动态范围内，方向合理
    valid = []
    for c in candidates:
        offset = abs(c - mark_price) / mark_price
        if offset > max_offset:
            continue
        if direction == "LONG" and c < mark_price:
            valid.append(c)
        elif direction == "SHORT" and c > mark_price:
            valid.append(c)

    if not valid:
        return None

    # 选距现价最近的
    return min(valid, key=lambda x: abs(x - mark_price))


# ── 主入口：计算入场价 ────────────────────────────────────────────────────────

def calc_entry_price(direction: str,
                     mark_price: float,
                     k1h: list,
                     mode: str = "LIMIT",
                     liq_zone_price: Optional[float] = None) -> tuple[float, str]:
    """
    根据模式和方向计算入场限价。

    Returns:
        (entry_price, method_used)
        method_used: "liq_zone" / "tech_level" / "atr_offset" / "chase"
    """
    atr_val = _atr(k1h) or mark_price * 0.01
    max_offset = calc_dynamic_range(mark_price, k1h)

    # CHASE：贴近现价
    if mode == "CHASE":
        if direction == "LONG":
            price = mark_price + atr_val * 0.1
        else:
            price = mark_price - atr_val * 0.1
        return round(price, 8), "chase"

    # 第一优先：Coinglass 清算区锚点
    if liq_zone_price is not None:
        offset = abs(liq_zone_price - mark_price) / mark_price
        if offset <= max_offset:
            return round(liq_zone_price, 8), "liq_zone"

    # 第二优先：技术位竞选
    tech = _find_tech_level(direction, mark_price, k1h, max_offset)
    if tech is not None:
        return round(tech, 8), "tech_level"

    # 第三优先：ATR 动态偏移
    min_offset = max(0.005, atr_val * 0.3 / mark_price)
    actual_offset = max(min_offset, atr_val * 0.5 / mark_price)
    actual_offset = min(actual_offset, max_offset)

    if direction == "LONG":
        price = mark_price * (1 - actual_offset)
    else:
        price = mark_price * (1 + actual_offset)

    return round(price, 8), "atr_offset"


# ── 四项有效性验证 ────────────────────────────────────────────────────────────

def validate_entry(entry_price: float,
                   mark_price: float,
                   direction: str,
                   k1h: list,
                   liq_zones: Optional[list] = None) -> tuple[bool, str]:
    """
    验证挂单价是否有效。
    Returns: (ok, reason)
    """
    # 验证四：方向合理性
    if direction == "LONG" and entry_price >= mark_price:
        return False, f"多单挂单价({entry_price})必须低于现价({mark_price})"
    if direction == "SHORT" and entry_price <= mark_price:
        return False, f"空单挂单价({entry_price})必须高于现价({mark_price})"

    # 验证一：偏离合理性
    max_offset = calc_dynamic_range(mark_price, k1h)
    actual_offset = abs(entry_price - mark_price) / mark_price
    if actual_offset > max_offset:
        return False, f"偏离过大({actual_offset:.2%} > {max_offset:.2%})，需改为条件限价单"

    min_offset = 0.005
    if actual_offset < min_offset:
        return False, f"偏离过小({actual_offset:.2%} < {min_offset:.2%})，容易被扫"

    # 验证三：非真空带（简化：检查 entry_price ±1% 内有无 K线实体覆盖）
    tolerance = entry_price * 0.01
    covered = False
    for k in k1h[-100:]:
        lo = float(k[3]); hi = float(k[2])
        if lo <= entry_price + tolerance and hi >= entry_price - tolerance:
            covered = True
            break
    if not covered:
        return False, f"挂单价({entry_price})处于真空带，无历史成交覆盖"

    return True, "ok"


# ══════════════════════════════════════════════════════════════════════════════
# 动态入场价 v2.0（三层优先级）
# 替换 scene_matcher 里的固定 current_price×1.003 逻辑
# ══════════════════════════════════════════════════════════════════════════════

def _find_nearest_resistance(k1h: list, current_price: float,
                              atr: float) -> Optional[float]:
    """
    寻找当前价上方最近的阻力位（空头用）。
    来源：近5根最高价 / EMA20 / EMA50 压制位。
    只返回在 current_price ~ current_price+1.5×ATR 范围内的阻力位。
    """
    highs  = _highs(k1h)
    closes = _closes(k1h)
    candidates = []

    # 来源1：近5根最高价
    for h in highs[-5:]:
        if h > current_price:
            candidates.append(h)

    # 来源2：前期高点（近20根排除近5根）
    prev_high = max(highs[-20:-5]) if len(highs) >= 20 else max(highs)
    if prev_high > current_price:
        candidates.append(prev_high)

    # 来源3：EMA20 / EMA50 压制位
    if len(closes) >= 20:
        e20 = _ema(closes[-20:], 20)
        if e20 > current_price:
            candidates.append(e20)
    if len(closes) >= 50:
        e50 = _ema(closes[-50:], 50)
        if e50 > current_price:
            candidates.append(e50)

    # 过滤：必须在 current_price ~ current_price+1.5×ATR 之内
    upper_bound = current_price + 1.5 * atr
    valid = [c for c in candidates if current_price < c <= upper_bound]
    if not valid:
        return None
    # 取最近的那个（最低的阻力位）
    return min(valid)


def _find_nearest_support(k1h: list, current_price: float,
                           atr: float) -> Optional[float]:
    """
    寻找当前价下方最近的支撑位（多头用）。
    来源：近5根最低价 / EMA20 / EMA50 支撑位。
    """
    lows   = _lows(k1h)
    closes = _closes(k1h)
    candidates = []

    for lo in lows[-5:]:
        if lo < current_price:
            candidates.append(lo)

    prev_low = min(lows[-20:-5]) if len(lows) >= 20 else min(lows)
    if prev_low < current_price:
        candidates.append(prev_low)

    if len(closes) >= 20:
        e20 = _ema(closes[-20:], 20)
        if e20 < current_price:
            candidates.append(e20)
    if len(closes) >= 50:
        e50 = _ema(closes[-50:], 50)
        if e50 < current_price:
            candidates.append(e50)

    lower_bound = current_price - 1.5 * atr
    valid = [c for c in candidates if lower_bound <= c < current_price]
    if not valid:
        return None
    return max(valid)  # 最近的支撑（最高的）


def calc_dynamic_entry(
    direction: str,
    current_price: float,
    k1h: list,
    atr_baseline: float,
    liq_zone_price: Optional[float] = None,
) -> tuple[float, str]:
    """
    三层动态入场价计算（v2.1 - 优化版）。

    核心思想：解决固定比例（如 0.3%）在不同币种上表现差异巨大的问题。
    - BTC atr=800U → 0.3%=200U → 完全在噪音范围内，极易被扫掉
    - MEME 币 atr=0.3% → 0.3%=刚好 1 个 ATR → 又太激进

    层级优先级（三层递进）：
      第一层（base）：ATR 缓冲（解决噪音问题）
        空头：current_price + 0.25×ATR（挂在上方等反弹）
        多头：current_price - 0.25×ATR（挂在下方等回调）
        目的：不被噪音扫掉，入场后立刻反向触发止损

      第二层（resistance/support）：技术位（解决成交质量问题）
        空头：阻力位 - 0.1×ATR（在阻力位下方挂，等反弹触阻回落）
        多头：支撑位 + 0.1×ATR（在支撑位上方挂，等回调触支撑企稳）
        条件：技术位必须在 1.5×ATR 范围内才采用
        目的：尽量在真实卖压/买盘聚集的地方成交

      第三层（liq_zone）：清算区锚点（解决套牢问题）
        空头：清算区顶 + 0.15×ATR（在清算区上方挂，等多单清算被消化后入）
        多头：清算区底 - 0.15×ATR（在清算区下方挂，等空单清算被消化后入）
        条件：清算区在 base_entry 的 2% 范围内才采用
        目的：避免在密集清算区被反向扫掉

    综合决策：
      空头：取所有候选中最低值（不追高），但不得低于 current_price
      多头：取所有候选中最高值（不追低），但不得高于 current_price

    安全检查：
      入场价不得超过 current_price ± 1.5×ATR（追太高/太低就不追）

    返回：
      (entry_price, method)  method: 'atr_buffer'/'resistance'/'support'/'liq_zone'/'combined'
    """
    atr = atr_baseline if atr_baseline > 0 else current_price * 0.01
    
    # 动态范围检查：所有候选必须在 ±1.5×ATR 内
    max_allowed_up = current_price + 1.5 * atr
    max_allowed_down = current_price - 1.5 * atr

    if direction == "SHORT":
        candidates = []
        
        # ── 第一层：ATR 缓冲（必选 base）─────────────────────────────────────
        # 空头：挂在现价上方 0.25×ATR，等反弹入场
        base_entry = current_price + atr * 0.25
        candidates.append((base_entry, "atr_buffer"))
        
        # ── 第二层：阻力位（优先级最高）─────────────────────────────────────
        # 空头应该挂在阻力位下方，等反弹触阻回落时成交
        resistance = _find_nearest_resistance(k1h, current_price, atr)
        if resistance is not None:
            # 阻力位必须在合理范围内（不超过 1.5×ATR）
            if resistance <= max_allowed_up:
                # 入场价 = 阻力位下方 0.1×ATR（在阻力位下方挂单，等反弹触及阻力受阻）
                entry_resist = resistance - atr * 0.1
                # 必须仍在现价上方才是有效的空头入场
                if entry_resist > current_price:
                    candidates.append((entry_resist, "resistance"))
        
        # ── 第三层：清算区（覆盖前两层）─────────────────────────────────────
        # 如果当前价到入场价之间有密集清算区（大量多单集中止损位）
        # 说明这个区域会有强烈反弹，入场价应该挪到清算区上方
        if liq_zone_price is not None and liq_zone_price > current_price:
            # 清算区必须在 base_entry 的 2% 范围内
            if liq_zone_price <= base_entry * 1.02:
                # 入场价 = 清算区顶 + 0.15×ATR（在清算区上方挂，等多单清算被消化）
                entry_liq = liq_zone_price + atr * 0.15
                if entry_liq <= max_allowed_up:
                    candidates.append((entry_liq, "liq_zone"))
        
        # ── 综合决策：取最优入场价 ────────────────────────────────────────
        # 空头：取所有候选里最低的（尽量不追高，更接近当前价）
        # 但不能低于 current_price（空头入场价必须在当前价上方或等于）
        best_price, best_method = min(candidates, key=lambda x: x[0])
        final = max(current_price, best_price)
        final = min(final, max_allowed_up)
        
    else:  # LONG
        candidates = []
        
        # ── 第一层：ATR 缓冲（必选 base）─────────────────────────────────────
        # 多头：挂在现价下方 0.25×ATR，等回调入场
        base_entry = current_price - atr * 0.25
        candidates.append((base_entry, "atr_buffer"))
        
        # ── 第二层：支撑位（优先级最高）─────────────────────────────────────
        # 多头应该挂在支撑位上方，等回调触支撑企稳时成交
        support = _find_nearest_support(k1h, current_price, atr)
        if support is not None:
            # 支撑位必须在合理范围内（不低于 1.5×ATR）
            if support >= max_allowed_down:
                # 入场价 = 支撑位上方 0.1×ATR（在支撑位上方挂单，等回调触及支撑企稳）
                entry_support = support + atr * 0.1
                # 必须仍在现价下方才是有效的多头入场
                # 且必须低于 base_entry（支撑位入场应该比 ATR 缓冲更优）
                if entry_support < current_price and entry_support <= base_entry:
                    candidates.append((entry_support, "support"))
        
        # ── 第三层：清算区（覆盖前两层）─────────────────────────────────────
        # 如果当前价到入场价之间有密集清算区（大量空单集中止损位）
        # 说明这个区域会有强烈反弹，入场价应该挪到清算区下方
        if liq_zone_price is not None and liq_zone_price < current_price:
            # 清算区必须在 base_entry 的 2% 范围内
            if liq_zone_price >= base_entry * 0.98:
                # 入场价 = 清算区底 - 0.15×ATR（在清算区下方挂，等空单清算被消化）
                entry_liq = liq_zone_price - atr * 0.15
                if entry_liq >= max_allowed_down:
                    candidates.append((entry_liq, "liq_zone"))
        
        # ── 综合决策：取最优入场价 ────────────────────────────────────────
        # 多头：取所有候选里最高的（尽量不追低，更接近当前价）
        # 但不能高于 current_price（多头入场价必须在当前价下方或等于）
        best_price, best_method = max(candidates, key=lambda x: x[0])
        final = min(current_price, best_price)
        final = max(final, max_allowed_down)
        
        # 最小偏移检查：必须至少有 0.15×ATR 的缓冲（防止入场价太接近现价）
        min_offset = current_price - atr * 0.15
        if final > min_offset:
            final = min_offset
    
    # 方法判定：单一候选用该候选的方法，多候选竞争用 combined
    method = best_method if len(candidates) == 1 else "combined"
    return round(final, 8), method


# ══════════════════════════════════════════════════════════════════════════════
# 场景化多头入场价 v2.0
# ══════════════════════════════════════════════════════════════════════════════

def calc_entry_long_scene(
    scene: str,
    current_price: float,
    k1h: list,
    atr_baseline: float,
    consolidation_top: float | None = None,
    liq_zone_price: float | None = None,
) -> tuple[float | None, str]:
    """
    多头三场景分化入场价计算。

    场景A（A_pullback）— 回调入场：
      找到最近支撑位，在支撑位上方 0.1×ATR 挂单，
      等回调触支撑企稳后成交。
      fallback：current_price - 0.2×ATR
      安全检查：偏离当前价 > 1.5×ATR → 返回 None，进观察池

    场景B（B_breakout）— 缩量突破：
      区间上沿（consolidation_top）+ 0.15×ATR 缓冲，防假突破。
      fallback：current_price + 0.15×ATR
      安全检查：超过 current_price + 1.5×ATR → None

    场景C（C_first_pull）— 强势回踩：
      前突破位（近20根低点均值，近似EMA支撑）+ 0.1×ATR
      fallback：current_price - 0.15×ATR
      安全检查：低于 current_price - 1.5×ATR → None

    返回：(entry_price, method)  |  (None, reason_str) 若安全检查不通过
    """
    atr = atr_baseline if atr_baseline > 0 else current_price * 0.01
    highs  = _highs(k1h)
    lows   = _lows(k1h)
    closes = _closes(k1h)

    if scene == "A_pullback":
        support = _find_nearest_support(k1h, current_price, atr)
        if support is not None:
            entry  = support + atr * 0.1
            method = "support"
        else:
            entry  = current_price - atr * 0.20
            method = "atr_buffer"

        # 清算区优化（多头：在清算区下方等空单被清算）
        if liq_zone_price is not None and liq_zone_price < current_price:
            liq_entry = liq_zone_price - atr * 0.15
            if liq_entry > entry:          # 取更高的（不追低）
                entry  = liq_entry
                method = "liq_zone"

        # 安全检查
        min_allowed = current_price - 1.5 * atr
        if entry < min_allowed:
            return None, f"场景A入场价{entry:.6f}偏离过深(>{1.5:.1f}×ATR)"
        entry = min(entry, current_price)  # 不超过当前价
        return round(entry, 8), method

    elif scene == "B_breakout":
        # 区间上沿：优先用传入的 consolidation_top，否则用近8根最高价
        if consolidation_top is not None:
            zone_top = consolidation_top
        else:
            zone_top = max(highs[-8:]) if len(highs) >= 8 else current_price
        entry  = zone_top + atr * 0.15
        method = "zone_top"

        # 安全检查：突破入场不能追太高
        max_allowed = current_price + 1.5 * atr
        if entry > max_allowed:
            return None, f"场景B入场价{entry:.6f}追高超过1.5×ATR"
        return round(entry, 8), method

    elif scene == "C_first_pull":
        # 强势回踩场景：入场价优先级
        # 第一优先：ATR 浅回调入场（现价附近 0.2~0.3×ATR）
        # 第二优先：清算区优化
        # 第三优先：EMA20 支撑（仅当 EMA20 在现价 0.8×ATR 之内才采用）
        # 设计原则：强势行情不会深回，不等深度回踩

        # 基础：浅回调（现价附近 0.2~0.3×ATR）
        base_entry = current_price - atr * 0.25
        entry  = base_entry
        method = "atr_shallow"

        # EMA20 支撑（仅当 EMA20 在现价 0.8×ATR 之内）
        if len(closes) >= 20:
            ema20 = _ema(closes[-20:], 20)
            ema20_offset = abs(current_price - ema20)
            if ema20 < current_price and ema20_offset <= 0.8 * atr:
                # EMA20 在近处，用 EMA20 上方小缓冲入场
                entry  = ema20 + atr * 0.05
                method = "ema20_near"
            # 若 EMA20 偏远，坚持 ATR 浅回调，不等深度支撑

        # 清算区优化
        if liq_zone_price is not None and liq_zone_price < current_price:
            liq_entry = liq_zone_price - atr * 0.15
            if liq_entry > entry:
                entry  = liq_entry
                method = "liq_zone"

        # 安全检查：强势回踩场景，允许最多 2×ATR 的回踩深度
        min_allowed = current_price - 2.0 * atr
        if entry < min_allowed:
            entry  = current_price - atr * 0.25
            method = "atr_buffer_fallback"
        entry = min(entry, current_price)
        return round(entry, 8), method

    else:
        # 未知场景，退回通用逻辑
        entry, m = calc_dynamic_entry("LONG", current_price, k1h, atr_baseline, liq_zone_price)
        return entry, m
