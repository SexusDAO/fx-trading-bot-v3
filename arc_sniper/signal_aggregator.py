"""
Signal Aggregator — Arc 链统一信号评分管道 (2026-09-16 重构)

管道流程:
    发现信号 (Twitter/GMGN/链上) → 安全拒绝门 → 多维评分 → 聪明钱叠加 → 决策

与旧版的区别:
    1. 评分前先过 filter.rejection_gate 拒绝门（之前两套系统不通话）
    2. 权重从 config 读取，不再硬编码
    3. 新增链上聪明钱维度 (smart_money.py)
    4. DEX 类型统一 "dag"（DAG Router 验证可用）
    5. 阈值全面适配 Arc 新链（holder/volume/liquidity 天然低）

评分维度 (总分 100):
    流动性     20 分  (WEIGHT_LIQUIDITY)
    持有人     15 分  (WEIGHT_HOLDERS)
    分布       15 分  (WEIGHT_DISTRIBUTION)
    叙事/KOL   25 分  (WEIGHT_NARRATIVE)  ← Arc 早期靠叙事
    年龄       10 分  (WEIGHT_AGE)
    聪明钱     15 分  (WEIGHT_SMART_MONEY) ← 新增
    安全否决    独立   (通过=0分加分，不通过=直接否决)
"""

import logging
import time
import json
from typing import Optional
from . import config

logger = logging.getLogger("arc.signal")

# ═══════════════════════════════════════════════════════════════
# 信号源枚举
# ═══════════════════════════════════════════════════════════════

SRC_TWITTER = "twitter"
SRC_GMGN = "gmgn"
SRC_ONCHAIN = "onchain"
SRC_WHALE = "whale"
SRC_MANUAL = "manual"


# ═══════════════════════════════════════════════════════════════
# 安全否决（一票否决，在评分之前）
# ═══════════════════════════════════════════════════════════════

def _security_gate(token_data: dict) -> tuple[bool, str]:
    """
    安全否决门：检测蜜罐/ rug / 砸盘风险。
    任何一条命中 → 直接否决，不进入评分。

    Arc 链适配：
    - holder_count 阈值降低（新链天然少）
    - top_10 持仓比阈值放宽（0.50 → 0.60）
    - rug_ratio 阈值放宽（0.30 → 0.40）
    """
    # 蜜罐
    if token_data.get("is_honeypot", 0):
        return False, "🚫 蜜罐"

    # 未开源 + 未放弃所有权 = 高风险
    # Arc 链新代币大多没有开源/放弃数据，放宽为 warning 而非 veto
    # 仅当两个都明确为 False（非 0/未知）时才否决
    is_os = token_data.get("is_open_source", None)
    is_rn = token_data.get("is_renounced", None)
    if is_os is False and is_rn is False:
        return False, "🚫 未开源且未放弃所有权"
    # 如果数据缺失（None/0），不否决，继续评分

    # 砸盘历史
    rug_ratio = float(token_data.get("rug_ratio", 0) or 0)
    if rug_ratio > 0.40:
        return False, f"🚫 砸盘率 {rug_ratio:.0%}"

    # Top10 持仓过度集中
    top10 = float(token_data.get("top_10_holder_rate", 1) or 0)
    if top10 > 0.60:
        return False, f"🚫 Top10 持仓 {top10:.0%}"

    # 流动性不足（Arc 新链适配）
    # 对极早期代币（< 10 分钟）放宽到 $200，低于此值滑点太大不值得买
    # 对稍晚代币（>= 10 分钟）使用 config.MIN_LIQUIDITY_USD ($1000)
    liq = float(token_data.get("liquidity_usd", 0) or token_data.get("liquidity", 0) or 0)
    creation_ts = int(token_data.get("creation_timestamp", 0) or 0)
    age_min = (int(time.time()) - creation_ts) / 60.0 if creation_ts > 0 else 999.0
    min_liq_for_age = 200 if age_min < 10 else config.MIN_LIQUIDITY_USD
    if 0 < liq < min_liq_for_age:
        return False, f"🚫 流动性 ${liq:.0f} < ${min_liq_for_age:.0f} (age={age_min:.1f}min)"
    # 流动性为 0 也拒绝（没有池子）
    if liq == 0:
        return False, f"🚫 无流动性 (age={age_min:.1f}min)"

    return True, "✅ 安全通过"


# ═══════════════════════════════════════════════════════════════
# 单维评分函数
# ═══════════════════════════════════════════════════════════════

def _score_liquidity(token_data: dict) -> tuple[int, str]:
    """流动性 (WEIGHT_LIQUIDITY=20)"""
    liq = float(token_data.get("liquidity_usd", 0) or token_data.get("liquidity", 0) or 0)
    w = config.WEIGHT_LIQUIDITY

    if liq >= 50_000:
        s = w
    elif liq >= 20_000:
        s = int(w * 0.80)
    elif liq >= 10_000:
        s = int(w * 0.60)
    elif liq >= 5_000:
        s = int(w * 0.40)
    elif liq >= config.MIN_LIQUIDITY_USD:
        s = int(w * 0.25)
    elif liq > 0:
        s = int(w * 0.10)  # 有但很低
    else:
        s = 0

    return s, f"{s}/{w} (liq=${liq:,.0f})"


def _score_holders(token_data: dict) -> tuple[int, str]:
    """持有人 (WEIGHT_HOLDERS=15)"""
    h = int(token_data.get("holder_count", 0) or token_data.get("holders", 0) or 0)
    w = config.WEIGHT_HOLDERS

    # Arc 新链阈值大幅降低
    if h >= 500:
        s = w
    elif h >= 200:
        s = int(w * 0.85)
    elif h >= 100:
        s = int(w * 0.70)
    elif h >= 50:
        s = int(w * 0.50)
    elif h >= 20:
        s = int(w * 0.35)
    elif h >= config.MIN_HOLDERS:
        s = int(w * 0.20)
    elif h > 0:
        s = int(w * 0.10)
    else:
        s = 0

    return s, f"{s}/{w} (holders={h})"


def _score_distribution(token_data: dict) -> tuple[int, str]:
    """持仓分布 (WEIGHT_DISTRIBUTION=15)"""
    top10 = float(token_data.get("top_10_holder_rate", 1) or 0)
    lock = float(token_data.get("lock_percent", 0) or 0)
    w = config.WEIGHT_DISTRIBUTION

    # Top10 占比越低越好
    if top10 <= 0.15:
        top_score = w
    elif top10 <= 0.25:
        top_score = int(w * 0.85)
    elif top10 <= 0.35:
        top_score = int(w * 0.65)
    elif top10 <= 0.45:
        top_score = int(w * 0.45)
    elif top10 <= 0.55:
        top_score = int(w * 0.25)
    else:
        top_score = int(w * 0.10)

    # LP 锁定加分
    lock_bonus = 0
    if lock >= 0.80:
        lock_bonus = 3
    elif lock >= 0.50:
        lock_bonus = 2
    elif lock >= 0.20:
        lock_bonus = 1

    s = min(top_score + lock_bonus, w)
    return s, f"{s}/{w} (top10={top10:.0%}, lock={lock:.0%}+{lock_bonus})"


def _score_narrative(token_data: dict, signal_source: str, signal_text: str) -> tuple[int, str]:
    """叙事/KOL/社交信号 (WEIGHT_NARRATIVE=25) — Arc 早期最重要维度"""
    w = config.WEIGHT_NARRATIVE
    score = 0
    parts = []

    # GMGN 聪明钱
    smart_degen = int(token_data.get("smart_degen_count", 0) or 0)
    if smart_degen >= 5:
        score += 8
        parts.append(f"smart_degen={smart_degen}+8")
    elif smart_degen >= 3:
        score += 6
        parts.append(f"smart_degen={smart_degen}+6")
    elif smart_degen >= 1:
        score += 3
        parts.append(f"smart_degen={smart_degen}+3")

    # KOL/知名地址
    renowned = int(token_data.get("renowned_count", 0) or 0)
    if renowned >= 3:
        score += 7
        parts.append(f"renowned={renowned}+7")
    elif renowned >= 1:
        score += 4
        parts.append(f"renowned={renowned}+4")

    # 信号源加权
    if signal_source == SRC_TWITTER:
        # Twitter 信号需要 Grok 确认才可信
        score += 3
        parts.append("twitter+3")
    elif signal_source == SRC_GMGN:
        score += 2
        parts.append("gmgn+2")
    elif signal_source == SRC_ONCHAIN:
        score += 4  # 链上信号最可信
        parts.append("onchain+4")

    # 有网站/Twitter = 有叙事基础
    if token_data.get("website"):
        score += 1
        parts.append("website+1")
    if token_data.get("twitter_username"):
        score += 1
        parts.append("twitter_acct+1")

    # 社区活跃度
    tg = token_data.get("telegram", "")
    if tg:
        score += 1
        parts.append("telegram+1")

    score = min(score, w)
    return score, f"{score}/{w} ({', '.join(parts)})"


def _score_age(token_data: dict, current_time: int) -> tuple[int, str]:
    """代币年龄 (WEIGHT_AGE=10) — 越新越有机会但也越危险"""
    w = config.WEIGHT_AGE
    created = int(token_data.get("creation_timestamp", 0) or 0)
    if created == 0:
        return 0, f"0/{w} (unknown age)"

    age_sec = current_time - created
    age_min = age_sec / 60

    if age_min < 5:
        s = int(w * 0.60)  # 超早期：机会大但风险
    elif age_min < 15:
        s = w  # 黄金期
    elif age_min < 30:
        s = int(w * 0.80)
    elif age_min < 60:
        s = int(w * 0.50)
    elif age_min < 180:
        s = int(w * 0.30)
    else:
        s = int(w * 0.10)

    return s, f"{s}/{w} (age={age_min:.0f}min)"


def _score_smart_money(token_data: dict, pair_address: str) -> tuple[int, str]:
    """链上聪明钱共振 (WEIGHT_SMART_MONEY=15) — 从 smart_money.py 获取实时信号"""
    w = config.WEIGHT_SMART_MONEY

    try:
        from . import smart_money as _sm
        pair = pair_address or token_data.get("pair_address", "")
        if not pair:
            return 0, f"0/{w} (no pair addr)"

        sm_bonus, sm_msg = _sm.get_signal(pair)
        # sm_bonus 范围 0-3，映射到权重
        s = min(sm_bonus * (w // 3), w) if sm_bonus > 0 else 0
        return s, f"{s}/{w} ({sm_msg})" if sm_msg else f"{s}/{w}"
    except Exception:
        return 0, f"0/{w} (unavailable)"


# ═══════════════════════════════════════════════════════════════
# 统一评分入口
# ═══════════════════════════════════════════════════════════════

def score_token_multi_dim(
    token_data: dict,
    signal_source: str = "",
    signal_text: str = "",
    chain_id: int = 5042,
) -> dict:
    """
    统一评分入口：安全门 → 拒绝门 → 6 维评分 → 决策。

    参数:
        token_data:   代币数据 dict（含链上/GMGN/Twitter 字段）
        signal_source: 信号来源 ("twitter" / "gmgn" / "onchain" / "whale" / "manual")
        signal_text:  信号文本（如推文内容）
        chain_id:     链 ID

    返回:
        {
            "score": int,         # 0-100
            "should_buy": bool,
            "reason": str,        # 决策原因
            "dex_type": str,      # 始终 "dag"
            "details": dict,      # 各维度详情
        }
    """
    current_time = int(time.time())
    details = {}
    score = 0

    # ── 1. 安全否决 ──
    safe, safe_msg = _security_gate(token_data)
    if not safe:
        details["blocked"] = True
        details["block_reason"] = safe_msg
        return {
            "score": 0,
            "should_buy": False,
            "reason": safe_msg,
            "dex_type": "dag",
            "details": details,
        }

    details["security"] = safe_msg

    # ── 2. filter.py 拒绝门（如果提供了 pair 数据）──
    pair_data = token_data.get("_pair_data")
    if pair_data:
        try:
            from .filter import rejection_gate
            passed, rej_msg = rejection_gate(pair_data, token_data.get("symbol", ""))
            if not passed:
                details["blocked"] = True
                details["block_reason"] = rej_msg
                return {
                    "score": 0,
                    "should_buy": False,
                    "reason": rej_msg,
                    "dex_type": "dag",
                    "details": details,
                }
            details["rejection_gate"] = rej_msg
        except Exception:
            pass

    # ── 3. 六维评分 ──
    s_liq, d_liq = _score_liquidity(token_data)
    s_hold, d_hold = _score_holders(token_data)
    s_dist, d_dist = _score_distribution(token_data)
    s_narr, d_narr = _score_narrative(token_data, signal_source, signal_text)
    s_age, d_age = _score_age(token_data, current_time)
    s_smart, d_smart = _score_smart_money(token_data, token_data.get("pair_address", ""))

    score = s_liq + s_hold + s_dist + s_narr + s_age + s_smart

    details["liquidity"] = d_liq
    details["holders"] = d_hold
    details["distribution"] = d_dist
    details["narrative"] = d_narr
    details["age"] = d_age
    details["smart_money"] = d_smart

    # ── 4. 额外数据 ──
    token_data.setdefault("dex_type", "dag")
    details["volume_24h"] = token_data.get("volume_24h", 0)
    details["signal_source"] = signal_source

    # ── 5. 决策 ──
    min_score = config.MIN_SCORE_TO_BUY
    should_buy = score >= min_score

    reason = f"总分 {score}/100"
    if not should_buy:
        reason += f" (需 ≥{min_score})"

    return {
        "score": score,
        "should_buy": should_buy,
        "reason": reason,
        "dex_type": "dag",
        "details": details,
    }


# ═══════════════════════════════════════════════════════════════
# 快速预检（轻量版，不调用外部模块）
# ═══════════════════════════════════════════════════════════════

def quick_prescreen(token_data: dict) -> tuple[bool, str]:
    """
    快速预检：只做安全否决 + 基础阈值检查。
    用于高频扫描的第一层过滤，避免每个代币都走完整评分。
    """
    safe, msg = _security_gate(token_data)
    if not safe:
        return False, msg

    # 基础阈值
    h = int(token_data.get("holder_count", 0) or 0)
    if 0 < h < config.MIN_HOLDERS:
        return False, f"holders={h}<{config.MIN_HOLDERS}"

    age = int(time.time()) - int(token_data.get("creation_timestamp", 0) or 0)
    if 0 < age > config.MAX_TOKEN_AGE_SEC:
        return False, f"age={age}s>{config.MAX_TOKEN_AGE_SEC}s"

    return True, "prescreen passed"


# ═══════════════════════════════════════════════════════════════
# 批量评分
# ═══════════════════════════════════════════════════════════════

def score_batch(tokens: list[dict], signal_source: str = "") -> list[dict]:
    """
    批量评分：先 quick_prescreen 过滤，再对通过的做完整评分。
    返回 [{"token": ..., "result": ...}, ...]（只包含通过预检的）。
    """
    results = []
    for token in tokens:
        passed, msg = quick_prescreen(token)
        if not passed:
            logger.debug(f"[Signal] 预检淘汰 {token.get('symbol','?')}: {msg}")
            continue
        result = score_token_multi_dim(token, signal_source)
        results.append({"token": token, "result": result})
    return results
