"""
robinhood_sniper/filter.py
第二层：AI 智能筛选 — 打分模型（规则引擎）v2.0
第三层：安全风控 — 一票否决（币安 audit API）

=== v2.0 核心改动（2026-09-02 复盘重设计）===

历史数据发现：
  - 评分越高反而亏损越严重（12分全亏，7分反而最赚）
  - 高分来自"买单多/成交量大/5min暴涨"——这些恰好是庄家拉盘砸盘的特征
  - 真正赚钱的币（NUDES/AAPLDOG/CASHBIRD）特征是成交量稳定、不极端

新设计原则：
  1. 先否决（拒绝门）：任何一个危险信号直接拒绝，不计入总分
  2. 再评分（质量门）：只评估真实健康度，而非热度
  3. 分数越高 = 越稳健，不等于越热

拒绝门（一票否决，和 security_gate / momentum_gate 并列）：
  R1. 5min 价格涨幅 > 50%  → 人为拉盘信号，拒绝
  R2. 买卖单数量相当（abs差距<20%）且总单数>50 → 机器刷单，拒绝
  R3. 5min 成交量 > 1h 均速的 10倍 → 人为制造脉冲热度，拒绝
  R4. MC × 5 < TP1($5万) → 天花板太低，止盈从未能触发，拒绝
  R5. RH 链 DexScreener 不索引 holders，永远返回 0，故完全禁用持有人拦截。
       仅当 holders > 0 但 < 5 时才拒绝（说明 DS 能索引到数据但真的没人买）。

评分维度（每项最多得1~2分，总分12分）：
  - 流动性质量（2分）：$5k→1分，$10k+→2分
  - 代币年龄（2分）：5~30min 最优（不太新不太老）
  - 成交量连续性（2分）：h1 vs h6 均速比值在合理范围
  - 买盘真实性（2分）：买单>卖单 且 买卖差距合理（非极端）
  - 社交热度（2分）：有真实 Twitter 热度
  - 持有人分散度（1分）：holders > 30
  - 价格趋势稳健（1分）：5min 上涨 5~30%（合理区间）
"""

import logging
import requests
import uuid
from typing import Optional
from .config import (
    PROXY_URL, CHAIN_ID, CHAIN_ID_DS,
    MIN_SCORE, MIN_LIQUIDITY_USD,
    MAX_BUY_TAX, MAX_SELL_TAX, BLOCK_RISK_LEVELS,
    MAX_TOKEN_AGE_SCORE_BONUS_MIN,
    PLATFORM_KEYWORDS, NARRATIVE_KEYWORDS,
    EARLY_TOKEN_AGE_MIN,
    MIN_REAL_HOLDERS, HOLDER_CHECK_ENABLED,
)
from .clawby_rpc import get_social_score, check_creator_risk

logger = logging.getLogger("sniper.filter")

_PROXIES = {"http": PROXY_URL, "https": PROXY_URL}
_AUDIT_URL = "https://web3.binance.com/bapi/defi/v1/public/wallet-direct/security/token/audit"
_AUDIT_HEADERS = {
    "Content-Type": "application/json",
    "Accept-Encoding": "identity",
    "User-Agent": "binance-web3/1.4 (Skill)",
}

# TP1 里程碑（与 config.py 保持一致）
# R4 检查：MC > TP1 × 0.95 拒绝，TP1 已降到 $3万
# → MC > $28.5k 才触发 R4，配合 MAX_ENTRY_MC_USD=$4.5万，有充足止盈空间
_TP1_MC = 30_000   # $3万（与 config.py TP_EARLY 第一档对齐）


# ─────────────────────────────────────────────────────────────────────────────
# 拒绝门（一票否决，在评分之前运行）
# ─────────────────────────────────────────────────────────────────────────────

def rejection_gate(pair: dict, symbol: str) -> tuple[bool, str]:
    """
    拒绝门：检测庄家操控信号，任何一条命中直接拒绝，不进入评分。
    返回 (passed, reason)  passed=False 表示拒绝

    R1. 5min 暴涨 > 50%   → 人为拉盘，随后砸盘概率极高
    R2. 买卖单数量接近（差距<20%）且总单数>50 → 机器刷单制造虚假买盘
    R3. 5min 成交量是 1h 均速的 10倍+ → 人为制造脉冲热度
    R4. 当前 MC × 5 < TP1 → 天花板太低，止盈里程碑无法触发
    """
    try:
        txns = pair.get("txns") or {}
        vol  = pair.get("volume") or {}
        m5   = txns.get("m5") or {}
        pc   = pair.get("priceChange") or {}
        mc   = float(pair.get("marketCap") or pair.get("fdv") or 0)

        pc_m5    = float(pc.get("m5", 0) or 0)
        buys_m5  = int(m5.get("buys",  0) or 0)
        sells_m5 = int(m5.get("sells", 0) or 0)
        vol_m5   = float(vol.get("m5", 0) or 0)
        vol_h1   = float(vol.get("h1", 0) or 0)
        total_m5 = buys_m5 + sells_m5

        # R1: 5min 暴涨 > 50%
        if pc_m5 > 50:
            return False, f"🚫 R1-拉盘拒绝: 5min涨幅+{pc_m5:.0f}%>50%（典型拉盘砸盘信号）"

        # R2: 买卖单数量接近（刷单特征）
        # 门槛从 20% 收紧到 10%：MEME 早期 116买/118卖(差距1%) 被误杀
        # 真正刷单是机械1:1，10%以内才是可靠信号，10-20%区间是正常价格发现
        # 使用 abs(buys-sells)/max(buys,sells) 计算，更直观
        if total_m5 > 50 and max(buys_m5, sells_m5) > 0:
            diff_ratio = abs(buys_m5 - sells_m5) / max(buys_m5, sells_m5)
            # 阈值 15%：真正机械刷单是完全1:1（差距<5%），10-15% 仍是正常价格发现。
            # 历史误杀案例：BITCAT 374买/256卖差距19%（正常买盘），放宽到15%后不会误杀。
            # 注意：R2.5 作为补充，买卖比<0.9 的净卖压情形已单独处理。
            if diff_ratio < 0.15:  # 差距小于多方的 15%（机械刷单特征）
                return False, (f"🚫 R2-刷单拒绝: 买卖单量相当 {buys_m5}买/{sells_m5}卖"
                               f" 差距仅{diff_ratio:.0%}（机器刷单特征）")

        # R2.5: 卖压主导一票否决
        # 5min 买单 < 卖单 × 0.8（即卖单超买单 25%+）且总单数 >= 10 → 拒绝
        # 证据：CATMAN(9买/19卖)和 BOGLE(18买/22卖) 买入时均为卖压，合计亏 $58
        # 买压不足说明当前有人在主动出货，不是好入场时机
        if total_m5 >= 10 and sells_m5 > 0:
            buy_sell_ratio = buys_m5 / sells_m5
            if buy_sell_ratio < 0.9:  # 买单不足卖单的 90%（即卖单超买单 >11%）
                return False, (
                    f"🚫 R2.5-卖压主导拒绝: {buys_m5}买/{sells_m5}卖"
                    f" 买卖比={buy_sell_ratio:.2f}<0.9（出货信号）"
                )

        # R3: 5min 成交量脉冲（是1h均速的10倍+）
        # 注意：链上早期数据绝对值可能极小（<$50），此时比值无意义，跳过
        if vol_h1 > 0 and vol_m5 >= 50:   # 最低 $50 才有实际意义
            h1_per_min = vol_h1 / 60
            if h1_per_min > 0 and vol_m5 > h1_per_min * 5 * 10:  # 5min正常量=h1均速*5
                return False, (f"🚫 R3-脉冲拒绝: 5min成交量${vol_m5:.0f}"
                               f" 是1h均速的{vol_m5/(h1_per_min*5):.0f}倍（人为脉冲）")

        # R4: MC 天花板太低（当前MC已超过TP1的95%）
        # 修改：阈值从80%→95%，避免 $40k-$50k 区间被误杀
        # 逻辑：入场MC通常$2-20k，TP1=$50k；只有MC已接近TP1（>$47.5k）才真正无空间
        if mc > 0 and mc > _TP1_MC * 0.95:
            return False, f"🚫 R4-天花板拒绝: MC${mc/1000:.0f}k 已接近TP1${_TP1_MC/1000:.0f}k，无止盈空间"

        # R5: 持有人数检测（RH 链专用修订版）
        # RH 链的 DexScreener 持有人字段几乎永远返回 0，不可依赖年龄门槛判断。
        # 新策略：只有当 holders > 0（DS 真的索引到了）但 < 5 时才拒绝，
        # 代表 DS 能看到数据且真的没人买；holders = 0 一律豁免（无法判断）。
        holders = int(pair.get("holders") or 0)
        if 0 < holders < 5:
            return False, f"🚫 R5-持有人拒绝: holders={holders}（DS已索引，真实持有人极少）"

        # R6: 链上真实持有人检测（去关联地址后 >= 50 人才允许买入）
        if HOLDER_CHECK_ENABLED:
            try:
                from .holder_analysis import analyze_real_holders
                token_addr = (pair.get("baseToken") or {}).get("address", "")
                if token_addr:
                    raw_h, real_h, passed = analyze_real_holders(
                        token_addr,
                        min_real_holders=MIN_REAL_HOLDERS,
                    )
                    if not passed and real_h > 0:
                        return False, (
                            f"🚫 R6-真实持有人不足: 总{raw_h}人 去关联后{real_h}人 < {MIN_REAL_HOLDERS}人"
                        )
                    logger.debug(f"[RejGate] {symbol} 持有人总={raw_h} 真实={real_h} ✅")
            except Exception as _he:
                logger.debug(f"[RejGate] {symbol} R6 跳过: {_he}")

    except Exception as e:
        logger.debug(f"[RejGate] {symbol} 拒绝门检测异常（非致命）: {e}")

    return True, "✅ 通过拒绝门"


# ─────────────────────────────────────────────────────────────────────────────
# 第二层：打分筛选（v2.0 稳健性评分）
# ─────────────────────────────────────────────────────────────────────────────

def score_token(pair: dict, profile: dict, age_min: float) -> tuple[int, list[str]]:
    """
    对代币打分（满分 10 分基础分），返回 (score, reasons)
    v2.0：评估稳健性，而非热度

    评分维度：
      流动性质量  2分  $5k→1分, $10k+→2分
      代币年龄    2分  5~30min最优
      成交量连续  2分  h1/h6比值在合理范围（非脉冲）
      买盘真实性  2分  买>卖 且差距合理
      价格趋势    1分  5min上涨5~30%（健康区间）
      持有人      1分  holders > 30
    """
    score   = 0
    reasons = []

    liq   = float((pair.get("liquidity") or {}).get("usd", 0) or 0)
    txns  = pair.get("txns") or {}
    m5    = txns.get("m5") or {}
    h1_tx = txns.get("h1") or {}
    vol   = pair.get("volume") or {}
    pc    = pair.get("priceChange") or {}

    vol_h1  = float(vol.get("h1",  0) or 0)
    vol_h6  = float(vol.get("h6",  0) or 0)
    vol_m5  = float(vol.get("m5",  0) or 0)
    pc_m5   = float(pc.get("m5",   0) or 0)
    holders = int(pair.get("holders") or 0)

    buys_m5  = int(m5.get("buys",  0) or 0)
    sells_m5 = int(m5.get("sells", 0) or 0)
    total_m5 = buys_m5 + sells_m5

    # ── 1. 流动性质量（2分）─────────────────────────────────────────────────
    if liq >= 10_000:
        score += 2
        reasons.append(f"✅ 流动性充足 ${liq:,.0f} +2")
    elif liq >= MIN_LIQUIDITY_USD:
        score += 1
        reasons.append(f"⚠️ 流动性偏低 ${liq:,.0f} +1")
    else:
        reasons.append(f"❌ 流动性不足 ${liq:,.0f}")

    # ── 2. 代币年龄（2分）——5~30min 窗口最优 ────────────────────────────────
    if 5 <= age_min <= 15:
        score += 2
        reasons.append(f"⏰ 年龄优质 {age_min:.1f}min(5~15min窗口) +2")
    elif age_min < 5:
        score += 1
        reasons.append(f"🚀 超早期 {age_min:.1f}min(太新,庄家未完成布局) +1")
    elif age_min <= 30:
        score += 1
        reasons.append(f"⏰ 较新代币 {age_min:.1f}min +1")
    else:
        reasons.append(f"⌛ 代币偏老 {age_min:.1f}min")

    # ── 3. 成交量连续性（2分）——非脉冲式增长 ───────────────────────────────
    # h1 成交量 vs h6 平均速率，比值在 1x~6x 之间为健康增长
    avg_h6_per_h = vol_h6 / 6 if vol_h6 > 0 else 0
    if avg_h6_per_h > 0:
        ratio = vol_h1 / avg_h6_per_h
        if 1.0 <= ratio <= 6.0:
            score += 2
            reasons.append(f"📊 成交量健康增长 h1/h6均速={ratio:.1f}x +2")
        elif ratio > 6.0:
            score += 1  # 增速快但不算极端
            reasons.append(f"📈 成交量加速 h1/h6均速={ratio:.1f}x(偏快) +1")
        else:
            reasons.append(f"📉 成交量萎缩 h1/h6均速={ratio:.1f}x")
    elif vol_h1 > 1000:
        score += 1
        reasons.append(f"💰 1h成交量 ${vol_h1:,.0f} +1")

    # ── 4. 买盘真实性（2分）────────────────────────────────────────────────
    # 买单 > 卖单，且差距在 20%~80% 之间（太悬殊说明刷单，已在拒绝门处理>50%情形）
    if total_m5 > 3:
        buy_ratio = buys_m5 / total_m5 if total_m5 > 0 else 0
        if 0.55 <= buy_ratio <= 0.85:
            score += 2
            reasons.append(f"📈 买盘主导健康 {buys_m5}买/{sells_m5}卖({buy_ratio:.0%}) +2")
        elif buy_ratio > 0.85:
            score += 1
            reasons.append(f"📈 买盘过度集中 {buys_m5}买/{sells_m5}卖({buy_ratio:.0%}) +1")
        elif buy_ratio >= 0.50:
            score += 1
            reasons.append(f"⚖️ 买卖基本平衡 {buys_m5}买/{sells_m5}卖 +1")
        else:
            reasons.append(f"📉 卖压主导 {buys_m5}买/{sells_m5}卖")
    else:
        reasons.append(f"⚠️ 5min交易数据不足")

    # ── 5. 价格趋势（1分）——健康区间 5~30%（不是暴涨不是下跌）──────────────
    if 5 <= pc_m5 <= 30:
        score += 1
        reasons.append(f"📈 价格趋势健康 +{pc_m5:.1f}% +1")
    elif pc_m5 > 0:
        reasons.append(f"➡️ 价格小幅变化 {pc_m5:+.1f}%")
    else:
        reasons.append(f"📉 价格下跌 {pc_m5:.1f}%")

    # ── 6. 持有人分散度（1分）───────────────────────────────────────────────
    if holders >= 50:
        score += 1
        reasons.append(f"👥 持有人 {holders} +1")
    elif holders >= 20:
        reasons.append(f"👥 持有人偏少 {holders}")
    else:
        reasons.append(f"⚠️ 持有人极少 {holders}（可能未开放交易）")

    return score, reasons


def score_token_full(pair: dict, profile: dict, age_min: float,
                     symbol: str = "") -> tuple[int, list[str]]:
    """
    完整评分：基础分(10) + 社交热度(+2) + 链上聪明钱(+3)

    链上早期模式（_onchain=True 且 age < ONCHAIN_EARLY_AGE_MIN）：
      h1/h6 数据不足，改用宽松早期评分（流动性 + 初始买盘 + 聪明钱）
      门槛降至 ONCHAIN_EARLY_MIN_SCORE，弥补数据不足的评分缺口
    """
    from robinhood_sniper.config import (
        ONCHAIN_EARLY_AGE_MIN, ONCHAIN_EARLY_MIN_LIQ
    )

    is_onchain_early = (
        pair.get("_onchain") is True
        and age_min < ONCHAIN_EARLY_AGE_MIN
    )

    if is_onchain_early:
        # ── 链上早期宽松评分 ──────────────────────────────────────────
        score   = 0
        reasons = [f"⚡ 链上早期 age={age_min:.1f}min（宽松模式）"]

        liq = float((pair.get("liquidity") or {}).get("usd", 0) or 0)
        if liq >= 20_000:
            score += 3; reasons.append(f"✅ 流动性强 ${liq:,.0f} +3")
        elif liq >= ONCHAIN_EARLY_MIN_LIQ:
            score += 2; reasons.append(f"✅ 流动性 ${liq:,.0f} +2")
        else:
            score += 1; reasons.append(f"⚠️ 流动性 ${liq:,.0f} +1")

        m5 = (pair.get("txns") or {}).get("m5") or {}
        buys_m5  = int(m5.get("buys",  0) or 0)
        sells_m5 = int(m5.get("sells", 0) or 0)
        total_m5 = buys_m5 + sells_m5
        if total_m5 >= 2:
            br = buys_m5 / total_m5
            if br >= 0.60:
                score += 2; reasons.append(f"📈 买盘 {buys_m5}买/{sells_m5}卖({br:.0%}) +2")
            elif br >= 0.45:
                score += 1; reasons.append(f"⚖️ 买卖平衡 {buys_m5}买/{sells_m5}卖 +1")
        elif total_m5 == 1:
            reasons.append(f"⚠️ 5min 仅 1 笔交易（极早期）")
        else:
            reasons.append("⚠️ 5min 暂无交易")

        # 初始成交量（哪怕极少也说明有人在买）
        vol_m5 = float((pair.get("volume") or {}).get("m5", 0) or 0)
        vol_h1 = float((pair.get("volume") or {}).get("h1", 0) or 0)
        if vol_m5 >= 500 or vol_h1 >= 200:
            score += 1
            reasons.append(f"💰 初始交易量 ${max(vol_m5, vol_h1):,.0f} +1")

        # MC 小胡同套利：MC 越小 = 潜在空间越大
        mc = float(pair.get("marketCap") or pair.get("fdv") or 0)
        if 0 < mc <= 10_000:
            score += 1; reasons.append(f"🎯 MC极小 ${mc/1000:.1f}k 超早入场 +1")
        elif 0 < mc <= 30_000:
            score += 1; reasons.append(f"🎯 MC小 ${mc/1000:.1f}k 早期入场 +1")
    else:
        score, reasons = score_token(pair, profile, age_min)

    # ── 聪明钱加分（最多 +3，链上直读优先）────────────────────────────
    pair_addr = pair.get("pairAddress", "")
    if pair_addr:
        try:
            if pair.get("_onchain"):
                from robinhood_sniper.on_chain_data import get_smart_money_signal
                sm_bonus, sm_msg = get_smart_money_signal(pair_addr)
            else:
                from . import smart_money as _sm
                sm_bonus, sm_msg = _sm.get_signal(pair_addr)
            if sm_bonus > 0:
                score += sm_bonus
                reasons.append(sm_msg)
        except Exception:
            pass

    # ── 社交热度加分（DS 路径才用，链上早期无社交数据）─────────────────
    if symbol and not is_onchain_early:
        token_addr = (pair.get("baseToken") or {}).get("address", "")
        social_score, social_msg = get_social_score(symbol, token_addr)
        social_score = min(social_score, 2)
        score += social_score
        reasons.append(social_msg)

    return score, reasons


# ─────────────────────────────────────────────────────────────────────────────
# 动量门（已有，保留）
# ─────────────────────────────────────────────────────────────────────────────

def momentum_gate(pair: dict, symbol: str) -> tuple[bool, str]:
    """
    价格动量方向过滤：拒绝在下跌中或卖压主导时买入。
    注意：拒绝门(rejection_gate) 已处理更严格的情况，
    momentum_gate 作为补充兜底，处理拒绝门未覆盖的边界情况。
    """
    try:
        price_change_m5 = float((pair.get('priceChange') or {}).get('m5', 0) or 0)
        if price_change_m5 < -10:
            return False, f"❌ 动量过滤: 5min价格{price_change_m5:.1f}%，正在下跌"

        txns_m5  = (pair.get('txns') or {}).get('m5') or {}
        m5_buys  = int(txns_m5.get('buys',  0) or 0)
        m5_sells = int(txns_m5.get('sells', 0) or 0)
        if m5_sells > 0 and m5_buys > 0:
            if m5_sells > m5_buys * 1.5 and m5_sells > 20:
                return False, f"❌ 动量过滤: 卖压主导 {m5_sells}卖/{m5_buys}买"
    except Exception:
        pass
    return True, "✅ 动量正常"


# ─────────────────────────────────────────────────────────────────────────────
# 第三层：安全风控
# ─────────────────────────────────────────────────────────────────────────────

def audit_token(token_address: str) -> Optional[dict]:
    """调用币安 Web3 audit API，返回审计结果"""
    try:
        r = requests.post(
            _AUDIT_URL,
            headers=_AUDIT_HEADERS,
            json={
                "binanceChainId": CHAIN_ID,
                "contractAddress": token_address,
                "requestId": str(uuid.uuid4()),
            },
            timeout=15,
            proxies=_PROXIES,
        )
        r.raise_for_status()
        d = r.json()
        if d.get("code") == "000000":
            return d.get("data")
        return None
    except Exception as e:
        logger.warning(f"[Audit] {token_address[:10]}... 审计失败: {e}")
        return None


def security_gate(token_address: str) -> tuple[bool, str]:
    """安全风控一票否决，返回 (passed, reason)"""
    audit = audit_token(token_address)

    if audit is None:
        return True, "⚠️ 审计API无响应，未经安全验证"

    risk_level = audit.get("riskLevelEnum", "UNKNOWN")
    extra      = audit.get("extraInfo") or {}
    buy_tax    = float(extra.get("buyTax",  0) or 0)
    sell_tax   = float(extra.get("sellTax", 0) or 0)
    flagged    = extra.get("isFlaggedByVendor", False)
    blacklist  = extra.get("isBlacklist", False)

    if risk_level in BLOCK_RISK_LEVELS:
        return False, f"❌ 风险等级 {risk_level}"
    if flagged:
        return False, "❌ 被第三方安全供应商标记"
    if blacklist:
        return False, "❌ 在黑名单中"
    if buy_tax > MAX_BUY_TAX:
        return False, f"❌ 买税过高 {buy_tax}%"
    if sell_tax > MAX_SELL_TAX:
        return False, f"❌ 卖税过高 {sell_tax}%"

    for item in (audit.get("riskItems") or []):
        for detail in (item.get("details") or []):
            if detail.get("isHit") and detail.get("riskType") == "HIGH":
                return False, f"❌ 高危风险: {detail.get('title', '')}"

    return True, f"✅ 安全通过 | 风险:{risk_level} 买税:{buy_tax}% 卖税:{sell_tax}%"


# ─────────────────────────────────────────────────────────────────────────────
# Token 自动分类
# ─────────────────────────────────────────────────────────────────────────────

def classify_token(pair: dict, profile: dict, age_min: float = 999) -> str:
    """
    自动判断代币类型：early > platform > narrative > normal
    动态 narrative 升级：链上热度指标达标时自动升级，不依赖关键词
    """
    name  = (pair.get("baseToken") or {}).get("name",   "").lower()
    sym   = (pair.get("baseToken") or {}).get("symbol", "").lower()
    desc  = (profile.get("description") or "").lower()
    combined = f"{name} {sym} {desc}"

    if age_min <= EARLY_TOKEN_AGE_MIN:
        return "early"

    if any(kw in combined for kw in PLATFORM_KEYWORDS):
        return "platform"

    if any(kw in combined for kw in NARRATIVE_KEYWORDS):
        return "narrative"

    # 动态 narrative 升级
    try:
        txns    = pair.get("txns") or {}
        vol     = pair.get("volume") or {}
        m5_buys = int((txns.get("m5") or {}).get("buys", 0) or 0)
        vol_h1  = float(vol.get("h1", 0) or 0)
        vol_h6  = float(vol.get("h6", 0) or 0)
        vol_m5  = float(vol.get("m5", 0) or 0)
        holders = int(pair.get("holders") or 0)

        if m5_buys >= 50:
            logger.info(f"[Classify] {sym} 动态升级 narrative: m5_buys={m5_buys}>=50")
            return "narrative"
        if vol_h1 >= 50_000 and vol_m5 >= 5_000:
            logger.info(f"[Classify] {sym} 动态升级 narrative: vol_h1=${vol_h1:.0f}")
            return "narrative"
        avg_h6_per_h = vol_h6 / 6 if vol_h6 > 0 else 0
        if avg_h6_per_h > 0 and vol_h1 >= avg_h6_per_h * 4:
            logger.info(f"[Classify] {sym} 动态升级 narrative: vol增速{vol_h1/avg_h6_per_h:.1f}x")
            return "narrative"
        if holders >= 200:
            logger.info(f"[Classify] {sym} 动态升级 narrative: holders={holders}>=200")
            return "narrative"
    except Exception as e:
        logger.debug(f"[Classify] 动态 narrative 检测异常: {e}")

    return "normal"
