"""
Twitter 分析模块 — Clawby x_search 版（零 API 费用）
=====================================================
通过 Clawby x_search 端点搜索 X/Twitter 推文，用规则提取分析数据。

替代旧版 Grok API 方案：
    - 旧版：xAI Grok API ($1.25-2.50/1M tokens，每次鲸鱼信号 ~$0.002-0.005)
    - 新版：Clawby x_search ($0，已含在现有 Key 里)
    - 数据更全：直接拿到推文全文、浏览量、点赞、转发、用户粉丝数等

两个项目共用:
    1. arc_sniper/grok_monitor.py — Arc 链 Twitter 监控
    2. fomo_workflow/ws_monitor.py — 鲸鱼信号卡片 + 综合买入建议
"""

import os
import re
import json
import time
import logging
import requests
from typing import Optional

logger = logging.getLogger("grok_analyzer")

# ═══════════════════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════════════════

# Clawby 配置
CLAWBY_KEY = os.getenv("CLAWBY_KEY", "pk_xBgG9wi3q1ZnIe6peyFrBHQP_6v2hG2C")
CLAWBY_RELAY = "https://api.openclawby.com/api/relay"

# 代理（与 fomo_workflow 共用）
_PROXY = {
    "https": "socks5://REDACTED_PROXY_CREDENTIALS",
    "http":  "socks5://REDACTED_PROXY_CREDENTIALS",
}

# 缓存：同一 CA 24 小时内不重复查询
_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_TTL_SEC = 86400  # 24 小时

# ── Clawby x_search 熔断器 ──────────────────────────────────────────
# 连续 N 次失败后静默 COOLDOWN 秒，避免每次鲸鱼信号都刷大量 502 日志
_XSEARCH_FAIL_COUNT  = 0
_XSEARCH_FAIL_MAX    = 3       # 连续失败超过 3 次触发熔断
_XSEARCH_COOLDOWN_SEC = 600    # 熔断后静默 10 分钟
_XSEARCH_DISABLED_UNTIL: float = 0.0

# KOL 粉丝阈值：>1万 算 KOL
KOL_FOLLOWER_THRESHOLD = 10_000

# 热度阈值
HEAT_VIEW_THRESHOLD_HIGH = 50_000     # 单条浏览量 > 5万 = 超热
HEAT_VIEW_THRESHOLD_MEDIUM = 10_000   # > 1万 = 较高
HEAT_VIEW_THRESHOLD_LOW = 1_000       # > 1千 = 中等


# ═══════════════════════════════════════════════════════════════
# Clawby x_search 调用
# ═══════════════════════════════════════════════════════════════

def _clawby_x_search(query: str, count: int = 20) -> list[dict]:
    """
    调用 Clawby x_search 端点搜索 X/Twitter 推文。
    内置熔断器：连续失败 3 次后静默 10 分钟，避免刷屏报错。
    """
    global _XSEARCH_FAIL_COUNT, _XSEARCH_DISABLED_UNTIL
    import time as _t

    # 熔断期内直接返回空
    if _t.time() < _XSEARCH_DISABLED_UNTIL:
        return []

    try:
        resp = requests.post(
            CLAWBY_RELAY,
            headers={
                "X-API-Key": CLAWBY_KEY,
                "Content-Type": "application/json",
            },
            json={
                "name": "x_search",
                "params": {"query": query, "count": count},
            },
            proxies=_PROXY,
            timeout=15,
        )
        if resp.status_code in (401, 403):
            # key 失效 → 直接长时间熔断
            _XSEARCH_DISABLED_UNTIL = _t.time() + 3600
            _XSEARCH_FAIL_COUNT = _XSEARCH_FAIL_MAX
            logger.warning(f"[Twitter] Clawby key 失效({resp.status_code})，熔断 1h")
            return []
        if resp.status_code == 502 or resp.status_code >= 500:
            _XSEARCH_FAIL_COUNT += 1
            if _XSEARCH_FAIL_COUNT >= _XSEARCH_FAIL_MAX:
                _XSEARCH_DISABLED_UNTIL = _t.time() + _XSEARCH_COOLDOWN_SEC
                logger.warning(f"[Twitter] Clawby x_search 连续失败 {_XSEARCH_FAIL_COUNT} 次，熔断 {_XSEARCH_COOLDOWN_SEC}s")
            else:
                logger.warning(f"[Twitter] Clawby x_search HTTP {resp.status_code} (失败 {_XSEARCH_FAIL_COUNT}/{_XSEARCH_FAIL_MAX})")
            return []
        if resp.status_code != 200:
            _XSEARCH_FAIL_COUNT += 1
            logger.warning(f"[Twitter] Clawby x_search HTTP {resp.status_code}")
            return []

        # 成功 → 重置失败计数
        _XSEARCH_FAIL_COUNT = 0
        _XSEARCH_DISABLED_UNTIL = 0.0

        data = resp.json()
        if isinstance(data, dict) and data.get("error"):
            logger.warning(f"[Twitter] Clawby 返回错误: {data['error']}")
            return []
        tweets = data.get("data", [])
        if not isinstance(tweets, list):
            return []
        return tweets

    except Exception as e:
        _XSEARCH_FAIL_COUNT += 1
        if _XSEARCH_FAIL_COUNT >= _XSEARCH_FAIL_MAX:
            _XSEARCH_DISABLED_UNTIL = _t.time() + _XSEARCH_COOLDOWN_SEC
            logger.warning(f"[Twitter] Clawby x_search 异常: {e}，已熔断 {_XSEARCH_COOLDOWN_SEC}s")
        else:
            logger.warning(f"[Twitter] Clawby x_search 异常: {e} (失败 {_XSEARCH_FAIL_COUNT}/{_XSEARCH_FAIL_MAX})")
        return []


# ═══════════════════════════════════════════════════════════════
# 规则分析（替代 LLM）
# ═══════════════════════════════════════════════════════════════

def _analyze_tweets(tweets: list[dict], token_symbol: str, ca: str) -> dict:
    """
    从推文列表中用规则提取分析数据，无需 LLM。

    提取维度：
    1. 讨论热度 — 推文数量 + 总互动量
    2. KOL 情况 — 是否有粉丝 >1万 的用户发帖
    3. 话题热度 — 总浏览量 + 传播力
    4. 叙事质量 — 从推文文本提取关键叙事
    5. 综合评分 — 加权计算
    """
    result = {
        "raw_text": "",
        "ca": ca,
        "token_name": token_symbol,
        "tweet_count": 0,
        "total_views": 0,
        "total_likes": 0,
        "total_rt": 0,
        "total_bookmarks": 0,
        "total_replies": 0,
        "kol_accounts": [],
        "kol_count": 0,
        "top_tweet": None,
        # 评分维度
        "discussion_score": 0,
        "discussion_level": "",
        "kol_score": 0,
        "has_kol": False,
        "topic_score": 0,
        "topic_level": "",
        "narrative_score": 0,
        "narrative_summary": "",
        # 综合
        "composite_score": 0,
        "buy_signal": "",
        "position_advice": "",
        "cost_usd": 0.0,
    }

    if not tweets:
        return result

    # ── 统计 ──
    total_views = sum(int(t.get("view_count", 0) or 0) for t in tweets)
    total_likes = sum(int(t.get("like_count", 0) or 0) for t in tweets)
    total_rt = sum(int(t.get("retweet_count", 0) or 0) for t in tweets)
    total_bookmarks = sum(int(t.get("bookmark_count", 0) or 0) for t in tweets)
    total_replies = sum(int(t.get("reply_count", 0) or 0) for t in tweets)

    result["tweet_count"] = len(tweets)
    result["total_views"] = total_views
    result["total_likes"] = total_likes
    result["total_rt"] = total_rt
    result["total_bookmarks"] = total_bookmarks
    result["total_replies"] = total_replies

    # ── 1. 讨论热度 ──
    # 评分依据：推文数量 + 总互动（likes+RT+replies）
    total_engagement = total_likes + total_rt + total_replies
    if len(tweets) >= 15 and total_engagement >= 200:
        result["discussion_score"] = 9
        result["discussion_level"] = "超热"
    elif len(tweets) >= 10 and total_engagement >= 80:
        result["discussion_score"] = 7
        result["discussion_level"] = "较高"
    elif len(tweets) >= 5 and total_engagement >= 20:
        result["discussion_score"] = 5
        result["discussion_level"] = "中等"
    elif len(tweets) >= 2:
        result["discussion_score"] = 3
        result["discussion_level"] = "低"
    else:
        result["discussion_score"] = 1
        result["discussion_level"] = "低"

    # ── 2. KOL 情况 ──
    kol_users = []
    for t in tweets:
        user = t.get("user", {})
        followers = int(user.get("followers_count", 0) or 0)
        username = user.get("username", "")
        verified = user.get("is_blue_verified", False)
        if followers >= KOL_FOLLOWER_THRESHOLD:
            kol_users.append({
                "username": username,
                "followers": followers,
                "verified": verified,
                "views": int(t.get("view_count", 0) or 0),
                "text": t.get("text", "")[:100],
            })

    # 去重（同一用户可能发多条）
    seen_users = set()
    unique_kol = []
    for k in kol_users:
        if k["username"] not in seen_users:
            seen_users.add(k["username"])
            unique_kol.append(k)

    result["kol_accounts"] = unique_kol
    result["kol_count"] = len(unique_kol)
    result["has_kol"] = len(unique_kol) > 0

    if len(unique_kol) >= 3:
        result["kol_score"] = 9
    elif len(unique_kol) >= 2:
        result["kol_score"] = 7
    elif len(unique_kol) >= 1:
        # 有 KOL 但只有 1 个，看粉丝量
        if unique_kol[0]["followers"] >= 50_000:
            result["kol_score"] = 7
        else:
            result["kol_score"] = 5
    else:
        result["kol_score"] = 2

    # ── 3. 话题热度 ──
    # 评分依据：总浏览量 + 平均浏览量
    avg_views = total_views / len(tweets) if tweets else 0
    if total_views >= HEAT_VIEW_THRESHOLD_HIGH * 10 or avg_views >= HEAT_VIEW_THRESHOLD_HIGH:
        result["topic_score"] = 9
        result["topic_level"] = "超热"
    elif total_views >= HEAT_VIEW_THRESHOLD_MEDIUM * 5 or avg_views >= HEAT_VIEW_THRESHOLD_MEDIUM:
        result["topic_score"] = 7
        result["topic_level"] = "较高"
    elif total_views >= HEAT_VIEW_THRESHOLD_LOW * 5 or avg_views >= HEAT_VIEW_THRESHOLD_LOW:
        result["topic_score"] = 5
        result["topic_level"] = "中等"
    elif total_views >= 100:
        result["topic_score"] = 3
        result["topic_level"] = "低"
    else:
        result["topic_score"] = 1
        result["topic_level"] = "低"

    # ── 4. 叙事质量 ──
    # 从推文文本提取关键叙事
    all_text = " ".join(t.get("text", "") for t in tweets).lower()
    narrative_bits = []

    # 检测叙事关键词
    narrative_keywords = {
        "ai": ("AI/人工智能叙事", ["ai", "artificial intelligence", "agent", "llm"]),
        "meme": ("Meme/社区叙事", ["meme", "dog", "cat", "inu", "pepe", "frog"]),
        "gaming": ("GameFi叙事", ["game", "gaming", "play", "metaverse"]),
        "defi": ("DeFi叙事", ["defi", "yield", "lp", "staking", "swap"]),
        "layer2": ("L2/链叙事", ["layer2", "l2", "rollup", "bridge"]),
        "robinhood": ("Robinhood 链生态", ["robinhood", "rh chain", "robinhood chain"]),
        "ath": ("突破ATH", ["ath", "all time high", "new high"]),
        "whale": ("鲸鱼跟踪", ["whale", "鲸鱼", "big buy", "smart money"]),
        "early": ("早期机会", ["early", "gem", "alpha", "first", "new launch"]),
        "community": ("社区共识", ["community", "holder", "diamond hand", "hold", "strong"]),
    }

    for label, keywords in narrative_keywords.values():
        if any(kw in all_text for kw in keywords):
            narrative_bits.append(label)

    # 热度趋势
    if total_likes > 50 and total_rt > 10:
        narrative_bits.append("互动活跃，有传播力")
    elif total_likes > 10:
        narrative_bits.append("有一定互动")
    else:
        narrative_bits.append("互动偏少，传播力弱")

    # 新鲜度（推文时间跨度）
    if len(tweets) >= 5:
        narrative_bits.append("推文数量充足，讨论正在展开")
    elif len(tweets) >= 2:
        narrative_bits.append("讨论刚起步")
    else:
        narrative_bits.append("讨论极少")

    result["narrative_summary"] = "；".join(narrative_bits)

    # 叙事评分
    narrative_keyword_count = len(narrative_bits) - 2  # 减去互动和新鲜度两项
    if narrative_keyword_count >= 3 and total_likes > 50:
        result["narrative_score"] = 8
    elif narrative_keyword_count >= 2:
        result["narrative_score"] = 6
    elif narrative_keyword_count >= 1:
        result["narrative_score"] = 4
    else:
        result["narrative_score"] = 2

    # ── 5. 综合评分 ──
    # 讨论×0.3 + KOL×0.25 + 话题×0.25 + 叙事×0.2
    composite = (
        result["discussion_score"] * 0.3
        + result["kol_score"] * 0.25
        + result["topic_score"] * 0.25
        + result["narrative_score"] * 0.2
    )
    result["composite_score"] = round(composite)

    # 买入信号强度
    if result["composite_score"] >= 8:
        result["buy_signal"] = "极强"
    elif result["composite_score"] >= 7:
        result["buy_signal"] = "强"
    elif result["composite_score"] >= 5:
        result["buy_signal"] = "中等"
    elif result["composite_score"] >= 3:
        result["buy_signal"] = "弱"
    else:
        result["buy_signal"] = "无"

    # 仓位建议
    if result["composite_score"] >= 9:
        result["position_advice"] = "正常仓3-5%"
    elif result["composite_score"] >= 7:
        result["position_advice"] = "中仓1.5-3%"
    elif result["composite_score"] >= 5:
        result["position_advice"] = "小仓0.5-1.5%"
    else:
        result["position_advice"] = "≤4不买"

    # 保存 top tweet（浏览量最高的）
    if tweets:
        top = max(tweets, key=lambda t: int(t.get("view_count", 0) or 0))
        result["top_tweet"] = {
            "username": top.get("user", {}).get("username", ""),
            "followers": int(top.get("user", {}).get("followers_count", 0) or 0),
            "text": top.get("text", "")[:150],
            "views": int(top.get("view_count", 0) or 0),
            "likes": int(top.get("like_count", 0) or 0),
            "rt": int(top.get("retweet_count", 0) or 0),
        }

    # 构建 raw_text 供兼容旧代码
    result["raw_text"] = _build_raw_text(result)

    return result


def _build_raw_text(r: dict) -> str:
    """构建兼容旧格式的 raw_text"""
    lines = [
        f"**仅基于 Twitter/X 数据的分析**",
        f"**CA：** {r['ca'][:12]}...",
        f"**代币名：** {r['token_name']}",
        "",
        f"### 1. 讨论热度",
        f"**级别：{r['discussion_level']}**",
        f"**评分：{r['discussion_score']}/10**",
        f"- 推文 {r['tweet_count']} 条，总互动 {r['total_likes'] + r['total_rt'] + r['total_replies']}",
        "",
        f"### 2. KOL情况",
        f"**是否有粉丝>1万的KOL讨论：{'有' if r['has_kol'] else '没有'}**",
        f"**评分：{r['kol_score']}/10**",
    ]
    if r["kol_accounts"]:
        for k in r["kol_accounts"][:3]:
            lines.append(f"- @{k['username']} ({k['followers']:,} 粉丝, 浏览 {k['views']:,})")
    lines += [
        "",
        f"### 3. 话题热度",
        f"**级别：{r['topic_level']}**",
        f"**评分：{r['topic_score']}/10**",
        f"- 总浏览 {r['total_views']:,}，平均 {r['total_views'] // max(r['tweet_count'], 1):,}",
        "",
        f"### 4. 叙事质量",
        f"**评分：{r['narrative_score']}/10**",
        f"- {r['narrative_summary']}",
        "",
        f"### 5. 综合决策数据",
        f"**Twitter综合分：{r['composite_score']}/10**",
        f"**买入信号强度：{r['buy_signal']}**",
        f"**建议仓位参考：** {r['position_advice']}",
    ]
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# 公开接口
# ═══════════════════════════════════════════════════════════════

def analyze_token_twitter(ca: str, chain_hint: str = "") -> Optional[dict]:
    """
    分析代币的 Twitter 数据。同一 CA 24h 内只查一次。

    返回分析结果 dict，失败返回 None。
    """
    ca = ca.lower().strip()

    # 缓存检查（24h）
    now = time.time()
    if ca in _CACHE:
        cached_time, cached_result = _CACHE[ca]
        if now - cached_time < _CACHE_TTL_SEC:
            logger.debug(f"[Twitter] 缓存命中 {ca[:12]}... (age={now-cached_time:.0f}s)")
            return cached_result

    # 从已知的 token symbol 构建搜索 query
    # 尝试多种 query 组合以提高命中率
    token_symbol = ""
    try:
        # 尝试从 Clawby dex_token_info 获取 symbol
        resp = requests.post(
            CLAWBY_RELAY,
            headers={"X-API-Key": CLAWBY_KEY, "Content-Type": "application/json"},
            json={"name": "dex_token_info", "params": {"chain": "robinhood", "address": ca}},
            proxies=_PROXY,
            timeout=10,
        )
        if resp.status_code == 200:
            info = resp.json().get("data", {}).get("data", {})
            if isinstance(info, dict):
                token_symbol = info.get("symbol", "")
    except Exception:
        pass

    # 构建搜索 query
    queries = []
    if token_symbol:
        queries.append(f"${token_symbol} {chain_hint or ''}".strip())
        queries.append(f"${token_symbol} crypto")
    queries.append(ca)  # 最后用 CA 搜

    all_tweets = []
    for query in queries[:2]:  # 最多搜 2 次
        tweets = _clawby_x_search(query, count=20)
        if tweets:
            all_tweets.extend(tweets)
            break  # 有结果就不继续

    # 去重（同一 tweet id）
    seen_ids = set()
    unique_tweets = []
    for t in all_tweets:
        tid = t.get("id", "")
        if tid and tid not in seen_ids:
            seen_ids.add(tid)
            unique_tweets.append(t)

    if not unique_tweets:
        logger.info(f"[Twitter] {ca[:12]}... 无推文")
        # 缓存空结果 1 小时（避免频繁重查）
        empty_result = {
            "raw_text": "", "ca": ca, "token_name": token_symbol,
            "tweet_count": 0, "total_views": 0, "total_likes": 0,
            "total_rt": 0, "total_bookmarks": 0, "total_replies": 0,
            "kol_accounts": [], "kol_count": 0, "top_tweet": None,
            "discussion_score": 0, "discussion_level": "",
            "kol_score": 0, "has_kol": False,
            "topic_score": 0, "topic_level": "",
            "narrative_score": 0, "narrative_summary": "",
            "composite_score": 0, "buy_signal": "",
            "position_advice": "", "cost_usd": 0.0,
        }
        _CACHE[ca] = (now, empty_result)
        # 1 小时后过期（不是 24h）
        _CACHE[ca] = (now, empty_result)
        return empty_result

    # 分析
    result = _analyze_tweets(unique_tweets, token_symbol or "UNKNOWN", ca)
    result["cost_usd"] = 0.0  # Clawby 不额外收费

    # 缓存（24h）
    _CACHE[ca] = (now, result)
    # 清理过期缓存
    expired = [k for k, (t, _) in _CACHE.items() if now - t > _CACHE_TTL_SEC * 2]
    for k in expired:
        del _CACHE[k]

    logger.info(
        f"[Twitter] {ca[:12]}... {token_symbol or '?'} "
        f"综合={result['composite_score']}/10 信号={result['buy_signal']} "
        f"推文={result['tweet_count']} KOL={result['kol_count']} "
        f"浏览={result['total_views']:,}"
    )

    return result


def format_for_signal_card(analysis: dict) -> str:
    """
    鲸鱼信号卡片格式：附加在原有信号后面，包含 Twitter 分析。
    """
    if not analysis or analysis.get("composite_score", 0) == 0:
        return ""

    score = analysis["composite_score"]
    signal = analysis.get("buy_signal", "")
    tweet_count = analysis.get("tweet_count", 0)
    total_views = analysis.get("total_views", 0)
    kol_count = analysis.get("kol_count", 0)

    lines = [
        f"🐦 Twitter 分析（Clawby）",
        f"综合分: {score}/10  买入信号: {signal}",
    ]

    # 讨论热度
    if analysis.get("discussion_level"):
        lines.append(
            f"讨论{analysis['discussion_score']}({analysis['discussion_level']}) "
            f"KOL{analysis['kol_score']} "
            f"话题{analysis['topic_score']}({analysis['topic_level'] or '?'}) "
            f"叙事{analysis['narrative_score']}"
        )

    # 数据明细
    data_parts = [f"推文{tweet_count}"]
    if total_views > 0:
        if total_views >= 1_000_000:
            data_parts.append(f"浏览{total_views/1e6:.1f}M")
        elif total_views >= 1_000:
            data_parts.append(f"浏览{total_views/1e3:.0f}K")
        else:
            data_parts.append(f"浏览{total_views}")
    if kol_count > 0:
        data_parts.append(f"KOL{kol_count}")
    lines.append("  ".join(data_parts))

    # KOL 列表
    if analysis.get("kol_accounts"):
        for k in analysis["kol_accounts"][:3]:
            lines.append(f"  👤 @{k['username']} ({k['followers']:,}粉) 浏览{k['views']:,}")

    # 叙事简述
    if analysis.get("narrative_summary"):
        lines.append(f"📝 {analysis['narrative_summary']}")

    # Top tweet
    top = analysis.get("top_tweet")
    if top and top.get("views", 0) >= 1000:
        lines.append(
            f"🔥 热帖 @{top['username']}: \"{top['text'][:80]}...\" "
            f"浏览{top['views']:,} 赞{top['likes']}"
        )

    return "\n".join(lines)


def format_for_arc_sniper(analysis: dict) -> str:
    """Arc Sniper 买入通知格式"""
    if not analysis or analysis.get("composite_score", 0) == 0:
        return "Twitter 分析: 不可用"

    score = analysis["composite_score"]
    signal = analysis.get("buy_signal", "")

    parts = [f"🐦 Twitter: 综合{score}/10  信号{signal}"]
    if analysis.get("discussion_level"):
        parts.append(
            f"   讨论{analysis['discussion_score']}({analysis['discussion_level']}) "
            f"KOL{analysis['kol_score']} "
            f"话题{analysis['topic_score']}({analysis['topic_level'] or '?'}) "
            f"叙事{analysis['narrative_score']}"
        )

    return "\n".join(parts)


def get_buy_recommendation(analysis: Optional[dict]) -> tuple[bool, str]:
    """
    根据 Twitter 分析判断是否建议买入。
    综合分 ≤4 → 不建议，≥5 → 建议
    """
    if not analysis:
        return True, "Twitter 分析不可用，按原逻辑继续"

    score = analysis.get("composite_score", 0)
    signal = analysis.get("buy_signal", "")

    if score >= 5:
        return True, f"Twitter 综合{score}/10，{signal}"
    elif score > 0:
        return False, f"Twitter 综合{score}/10 ≤4，不建议买入"
    else:
        return True, "Twitter 分析失败，按原逻辑继续"


def combined_recommendation(
    whale_grade: str,
    whale_rating: str,
    whale_score: int,
    twitter_analysis: Optional[dict],
) -> str:
    """
    综合鲸鱼信号 + Twitter 分析，给出一个明确的买入建议。

    参数:
        whale_grade: 鲸鱼等级 ("S" / "A" / "B" / "C")
        whale_rating: 鲸鱼信号评级 ("🟢 建议跟单" / "🟡 可以考虑" / "🟠 谨慎观望" / "🔴 不建议")
        whale_score: 鲸鱼信号评分 (int)
        twitter_analysis: Twitter 分析结果 (None 表示不可用)

    返回: 一行综合建议文本
    """
    # 鲸鱼信号权重：S/A 级 + 评分高 = 强信号
    whale_strong = whale_grade in ("S", "A") and whale_score >= 4
    whale_ok = whale_score >= 1
    whale_weak = whale_score < 0

    # Twitter 信号
    tw = twitter_analysis or {}
    tw_score = tw.get("composite_score", 0)
    tw_signal = tw.get("buy_signal", "")
    tw_strong = tw_score >= 7
    tw_ok = tw_score >= 5
    tw_weak = tw_score > 0 and tw_score <= 4
    tw_unavailable = tw_score == 0

    # 综合判断
    if whale_strong and tw_strong:
        return f"✅ 综合建议买入 — 鲸鱼{whale_grade}级强信号 + Twitter {tw_score}/10 高热度"
    elif whale_strong and tw_ok:
        return f"✅ 综合建议买入 — 鲸鱼{whale_grade}级强信号 + Twitter {tw_score}/10 适中"
    elif whale_strong and tw_weak:
        return f"⚠️ 综合谨慎 — 鲸鱼强但 Twitter {tw_score}/10 偏弱，注意流动性"
    elif whale_ok and tw_strong:
        return f"✅ 综合建议买入 — Twitter {tw_score}/10 高热度弥补鲸鱼信号一般"
    elif whale_ok and tw_ok:
        return f"🟡 综合可以考虑 — 鲸鱼{whale_grade}级 + Twitter {tw_score}/10，中等信号"
    elif whale_ok and tw_weak:
        return f"🟠 综合观望 — 鲸鱼一般 + Twitter {tw_score}/10 偏弱"
    elif whale_weak and tw_strong:
        return f"🟠 综合观望 — 鲸鱼信号弱但 Twitter {tw_score}/10 热度高，看个人风险偏好"
    elif whale_weak and tw_weak:
        return f"🔴 综合不建议 — 鲸鱼信号弱 + Twitter {tw_score}/10 偏弱"
    elif tw_unavailable:
        # Twitter 不可用时退回鲸鱼信号判断
        if whale_strong:
            return f"✅ 综合建议买入 — 鲸鱼{whale_grade}级强信号（Twitter 数据暂不可用）"
        elif whale_ok:
            return f"🟡 综合可以考虑 — 鲸鱼{whale_grade}级信号（Twitter 数据暂不可用）"
        else:
            return f"🔴 综合不建议 — 鲸鱼信号弱（Twitter 数据暂不可用）"
    else:
        return f"🟠 综合观望 — 信号混合，需更多信息"