"""
Arc Filter — 拒绝门 + 评分模型 (移植自 robinhood_sniper/filter.py)

=== Arc 链适配版 ===

设计原则（与罗宾汉一致）：
1. 先否决（拒绝门）：任何危险信号直接拒绝，不进入评分
2. 再评分（质量门）：评估真实健康度，而非热度
3. 聪明钱加分：叠加 smart_money.py 的共振信号

Arc 链特性适配：
- MC 天花板参考 Arc 链代币生态（$34M ARGUS, $25M TOLLY）
- R5 持有人检测：Arc 新链 holder 数据不全，放宽阈值
- 链上早期模式：Arc 链新币数据少，使用宽松评分
"""

import logging
from typing import Optional

logger = logging.getLogger("arc.filter")

# ═══════════════════════════════════════════════════════════════
# 参数 — 从 config.py 读取，不再硬编码
# ═══════════════════════════════════════════════════════════════

try:
    from . import config as _cfg
    MIN_LIQUIDITY_USD = getattr(_cfg, 'MIN_LIQUIDITY_USD', 2000)
    _TP1_MC = 100_000  # Arc 链 TP1 参考值
    MIN_SCORE = 5       # 基础评分满分 10，门槛 5
except ImportError:
    MIN_LIQUIDITY_USD = 2000
    _TP1_MC = 100_000
    MIN_SCORE = 5

# 链上早期模式参数
ONCHAIN_EARLY_AGE_MIN = 10  # 10 分钟内用宽松评分
ONCHAIN_EARLY_MIN_LIQ = 1_000  # 早期最低流动性 $1K


# ═══════════════════════════════════════════════════════════════
# 拒绝门（一票否决，在评分之前运行）
# ═══════════════════════════════════════════════════════════════

def rejection_gate(pair: dict, symbol: str = "") -> tuple[bool, str]:
    """
    拒绝门：检测庄家操控信号，任何一条命中直接拒绝。

    R1. 5min 暴涨 > 80% → 人为拉盘（Arc 新链波动大，阈值比 RH 更宽松）
    R2. 买卖单量接近（差距<15%）且总单数>30 → 机器刷单
    R2.5 卖压主导（买单<卖单×0.8）→ 出货信号
    R3. 5min 成交量脉冲（是1h均速的15倍+）→ 人为制造热度
    R4. MC 天花板太低（MC > TP1 × 0.95）→ 无止盈空间
    R5. 持有人极少（0 < holders < 3）→ 真实持有人不足

    Arc 链调整：
    - R1 阈值 50% → 80%（新链波动更大）
    - R2 总单数 50 → 30（Arc 链交易量更少）
    - R3 倍数 10x → 15x（新链正常波动范围更大）
    - R4 TP1 $30K → $100K（Arc 头部代币 MC 更高）
    """
    try:
        txns = pair.get("txns") or {}
        vol = pair.get("volume") or {}
        pc = pair.get("priceChange") or {}
        mc = float(pair.get("marketCap") or pair.get("fdv") or 0)

        pc_m5 = float(pc.get("m5", 0) or 0)
        buys_m5 = int((m5 := txns.get("m5") or {}).get("buys", 0) or 0)
        sells_m5 = int(m5.get("sells", 0) or 0)
        vol_m5 = float(vol.get("m5", 0) or 0)
        vol_h1 = float(vol.get("h1", 0) or 0)
        total_m5 = buys_m5 + sells_m5

        # R1: 5min 暴涨 > 80%
        if pc_m5 > 80:
            return False, f"🚫 R1-拉盘拒绝: 5min涨幅+{pc_m5:.0f}%>80%"

        # R2: 买卖单量接近（刷单特征）
        if total_m5 > 30 and max(buys_m5, sells_m5) > 0:
            diff_ratio = abs(buys_m5 - sells_m5) / max(buys_m5, sells_m5)
            if diff_ratio < 0.15:
                return False, (
                    f"🚫 R2-刷单拒绝: {buys_m5}买/{sells_m5}卖"
                    f" 差距仅{diff_ratio:.0%}"
                )

        # R2.5: 卖压主导
        if total_m5 >= 8 and sells_m5 > 0:
            buy_sell_ratio = buys_m5 / sells_m5
            if buy_sell_ratio < 0.8:
                return False, (
                    f"🚫 R2.5-卖压拒绝: {buys_m5}买/{sells_m5}卖"
                    f" 买卖比={buy_sell_ratio:.2f}<0.8"
                )

        # R3: 5min 成交量脉冲
        if vol_h1 > 0 and vol_m5 >= 30:
            h1_per_min = vol_h1 / 60
            if h1_per_min > 0 and vol_m5 > h1_per_min * 5 * 15:
                return False, (
                    f"🚫 R3-脉冲拒绝: 5min${vol_m5:.0f}"
                    f" 是1h均速的{vol_m5/(h1_per_min*5):.0f}倍"
                )

        # R4: MC 天花板
        if mc > 0 and mc > _TP1_MC * 0.95:
            return False, (
                f"🚫 R4-天花板拒绝: MC${mc/1000:.0f}k"
                f" 已接近TP1${_TP1_MC/1000:.0f}k"
            )

        # R5: 持有人极少（Arc 链放宽：holders 0 豁免，<3 才拒绝）
        holders = int(pair.get("holders") or 0)
        if 0 < holders < 3:
            return False, f"🚫 R5-持有人拒绝: holders={holders}"

    except Exception as e:
        logger.debug(f"[RejGate] {symbol} 拒绝门异常（非致命）: {e}")

    return True, "✅ 通过拒绝门"


# ═══════════════════════════════════════════════════════════════
# 评分模型
# ═══════════════════════════════════════════════════════════════

def score_token(pair: dict, age_min: float) -> tuple[int, list[str]]:
    """
    基础评分（满分 10 分），评估稳健性而非热度。

    评分维度（与罗宾汉一致，适配 Arc 链参数）：
      流动性质量  2分  $3k→1分, $10k+→2分
      代币年龄    2分  5~15min 最优
      成交量连续  2分  h1/h6 均速比在合理范围
      买盘真实性  2分  买>卖 且差距合理
      价格趋势    1分  5min 上涨 5~50%（Arc 波动大，上限放宽）
      持有人      1分  holders > 10（Arc 新链阈值低）
    """
    score = 0
    reasons = []

    liq = float((pair.get("liquidity") or {}).get("usd", 0) or 0)
    txns = pair.get("txns") or {}
    m5 = txns.get("m5") or {}
    vol = pair.get("volume") or {}
    pc = pair.get("priceChange") or {}

    vol_h1 = float(vol.get("h1", 0) or 0)
    vol_h6 = float(vol.get("h6", 0) or 0)
    vol_m5 = float(vol.get("m5", 0) or 0)
    pc_m5 = float(pc.get("m5", 0) or 0)
    holders = int(pair.get("holders") or 0)

    buys_m5 = int(m5.get("buys", 0) or 0)
    sells_m5 = int(m5.get("sells", 0) or 0)
    total_m5 = buys_m5 + sells_m5

    # 1. 流动性质量（2分）
    if liq >= 10_000:
        score += 2
        reasons.append(f"✅ 流动性充足 ${liq:,.0f} +2")
    elif liq >= MIN_LIQUIDITY_USD:
        score += 1
        reasons.append(f"⚠️ 流动性偏低 ${liq:,.0f} +1")
    else:
        reasons.append(f"❌ 流动性不足 ${liq:,.0f}")

    # 2. 代币年龄（2分）
    if 5 <= age_min <= 15:
        score += 2
        reasons.append(f"⏰ 年龄优质 {age_min:.1f}min +2")
    elif age_min < 5:
        score += 1
        reasons.append(f"🚀 超早期 {age_min:.1f}min +1")
    elif age_min <= 30:
        score += 1
        reasons.append(f"⏰ 较新 {age_min:.1f}min +1")
    else:
        reasons.append(f"⌛ 偏老 {age_min:.1f}min")

    # 3. 成交量连续性（2分）
    avg_h6_per_h = vol_h6 / 6 if vol_h6 > 0 else 0
    if avg_h6_per_h > 0:
        ratio = vol_h1 / avg_h6_per_h
        if 1.0 <= ratio <= 8.0:  # Arc 链波动大，上限放宽到 8x
            score += 2
            reasons.append(f"📊 量健康 h1/h6={ratio:.1f}x +2")
        elif ratio > 8.0:
            score += 1
            reasons.append(f"📈 量加速 h1/h6={ratio:.1f}x +1")
        else:
            reasons.append(f"📉 量萎缩 h1/h6={ratio:.1f}x")
    elif vol_h1 > 500:
        score += 1
        reasons.append(f"💰 1h量 ${vol_h1:,.0f} +1")

    # 4. 买盘真实性（2分）
    if total_m5 > 2:
        buy_ratio = buys_m5 / total_m5 if total_m5 > 0 else 0
        if 0.55 <= buy_ratio <= 0.85:
            score += 2
            reasons.append(f"📈 买盘健康 {buys_m5}买/{sells_m5}卖({buy_ratio:.0%}) +2")
        elif buy_ratio > 0.85:
            score += 1
            reasons.append(f"📈 买盘集中 {buys_m5}买/{sells_m5}卖({buy_ratio:.0%}) +1")
        elif buy_ratio >= 0.50:
            score += 1
            reasons.append(f"⚖️ 买卖平衡 {buys_m5}买/{sells_m5}卖 +1")
        else:
            reasons.append(f"📉 卖压 {buys_m5}买/{sells_m5}卖")
    else:
        reasons.append("⚠️ 5min交易不足")

    # 5. 价格趋势（1分）— Arc 链波动大，上限 50%
    if 5 <= pc_m5 <= 50:
        score += 1
        reasons.append(f"📈 价格健康 +{pc_m5:.1f}% +1")
    elif pc_m5 > 0:
        reasons.append(f"➡️ 小幅 {pc_m5:+.1f}%")
    else:
        reasons.append(f"📉 下跌 {pc_m5:.1f}%")

    # 6. 持有人（1分）— Arc 新链阈值低
    if holders >= 50:
        score += 1
        reasons.append(f"👥 持有人 {holders} +1")
    elif holders >= 10:
        reasons.append(f"👥 持有人偏少 {holders}")
    else:
        reasons.append(f"⚠️ 持有人极少 {holders}")

    return score, reasons


def score_token_full(pair: dict, age_min: float,
                     symbol: str = "",
                     pair_address: str = "") -> tuple[int, list[str]]:
    """
    完整评分：基础分(10) + 聪明钱加分(+3)

    链上早期模式（age < 10min）：使用宽松评分
    """
    is_onchain_early = (
        pair.get("_onchain") is True
        and age_min < ONCHAIN_EARLY_AGE_MIN
    )

    if is_onchain_early:
        # 链上早期宽松评分
        score = 0
        reasons = [f"⚡ 链上早期 age={age_min:.1f}min"]

        liq = float((pair.get("liquidity") or {}).get("usd", 0) or 0)
        if liq >= 20_000:
            score += 3
            reasons.append(f"✅ 流动性强 ${liq:,.0f} +3")
        elif liq >= ONCHAIN_EARLY_MIN_LIQ:
            score += 2
            reasons.append(f"✅ 流动性 ${liq:,.0f} +2")
        else:
            score += 1
            reasons.append(f"⚠️ 流动性 ${liq:,.0f} +1")

        m5 = (pair.get("txns") or {}).get("m5") or {}
        buys_m5 = int(m5.get("buys", 0) or 0)
        sells_m5 = int(m5.get("sells", 0) or 0)
        total_m5 = buys_m5 + sells_m5
        if total_m5 >= 2:
            br = buys_m5 / total_m5
            if br >= 0.60:
                score += 2
                reasons.append(f"📈 买盘 {buys_m5}买/{sells_m5}卖({br:.0%}) +2")
            elif br >= 0.45:
                score += 1
                reasons.append(f"⚖️ 买卖平衡 {buys_m5}买/{sells_m5}卖 +1")
        elif total_m5 == 1:
            reasons.append("⚠️ 5min仅1笔")
        else:
            reasons.append("⚠️ 5min无交易")

        vol_m5 = float((pair.get("volume") or {}).get("m5", 0) or 0)
        vol_h1 = float((pair.get("volume") or {}).get("h1", 0) or 0)
        if vol_m5 >= 200 or vol_h1 >= 100:
            score += 1
            reasons.append(f"💰 初始量 ${max(vol_m5, vol_h1):,.0f} +1")

        mc = float(pair.get("marketCap") or pair.get("fdv") or 0)
        if 0 < mc <= 5_000:
            score += 1
            reasons.append(f"🎯 MC极小 ${mc/1000:.1f}k +1")
        elif 0 < mc <= 50_000:
            score += 1
            reasons.append(f"🎯 MC小 ${mc/1000:.1f}k +1")
    else:
        score, reasons = score_token(pair, age_min)

    # 聪明钱加分（最多 +3）
    pair_addr = pair_address or pair.get("pairAddress", "")
    if pair_addr:
        try:
            from . import smart_money as _sm
            sm_bonus, sm_msg = _sm.get_signal(pair_addr)
            if sm_bonus > 0:
                score += sm_bonus
                reasons.append(sm_msg)
        except Exception:
            pass

    return score, reasons


# ═══════════════════════════════════════════════════════════════
# 综合决策
# ═══════════════════════════════════════════════════════════════

def evaluate_token(pair: dict, age_min: float,
                    symbol: str = "",
                    pair_address: str = "") -> dict:
    """
    综合评估代币：拒绝门 + 评分 + 聪明钱。

    返回:
        {
            "should_buy": bool,
            "score": int,
            "reasons": list[str],
            "rejection": str (拒绝原因，通过时为空),
        }
    """
    # 1. 拒绝门
    passed, reason = rejection_gate(pair, symbol)
    if not passed:
        return {
            "should_buy": False,
            "score": 0,
            "reasons": [reason],
            "rejection": reason,
        }

    # 2. 评分
    score, reasons = score_token_full(pair, age_min, symbol, pair_address)

    # 3. 决策
    should_buy = score >= MIN_SCORE

    return {
        "should_buy": should_buy,
        "score": score,
        "reasons": reasons,
        "rejection": "" if should_buy else f"评分 {score} < {MIN_SCORE}",
    }
