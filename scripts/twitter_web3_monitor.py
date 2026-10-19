#!/usr/bin/env python3
"""
twitter_web3_monitor.py — Web3 中文区热点监控
每次运行：搜索 Web3/Crypto 中文热点推文 → AI分析 → 有机会才推送

触发条件（任一）：
  - 发现新空投/白名单/活动
  - 发现套利机会（DEX价差/资金费率异常/新上币）
  - 发现高热度新项目（互动>500 且 Web3相关）

静默条件：
  - 纯价格讨论（"BTC要涨了"等）
  - 重复/已推送过的内容
  - 互动数低的普通帖子
"""

import json
import pathlib
import datetime
import sys
import re
import hashlib

import tweepy

WORKSPACE   = pathlib.Path("/root/.openclaw/workspace")
CFG_FILE    = WORKSPACE / "twitter_config.json"
CACHE_FILE  = WORKSPACE / "ws_cache" / "twitter_seen.json"

# 搜索关键词（中文 Web3 热点）
QUERIES = [
    "Web3 空投 lang:zh",
    "Crypto 白名单 mint lang:zh",
    "DeFi 套利 lang:zh",
    "链上 空投 领取 lang:zh",
    "新币 上线 Binance 欧易 lang:zh",
    "撸毛 攻略 lang:zh",
]

MAX_RESULTS_PER_QUERY = 10

# 机会关键词（命中即推送）
OPPORTUNITY_KEYWORDS = [
    "空投", "airdrop", "白名单", "whitelist", "mint", "铸造",
    "套利", "arb", "资金费率", "funding",
    "上线", "上币", "listing", "launchpool", "launchpad",
    "撸毛", "交互", "测试网", "testnet",
    "活动", "奖励", "reward", "earn", "质押",
    "IDO", "IEO", "presale", "预售",
]

# 排除词（降噪）
EXCLUDE_KEYWORDS = [
    "要涨", "要跌", "看涨", "看跌", "预测", "牛市", "熊市",
    "炒币", "梭哈", "抄底", "逃顶",
]


def load_config():
    return json.loads(CFG_FILE.read_text())


def load_seen() -> set:
    if CACHE_FILE.exists():
        d = json.loads(CACHE_FILE.read_text())
        return set(d.get("seen_ids", []))
    return set()


def save_seen(seen: set):
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps({"seen_ids": list(seen)[-2000:]}, ensure_ascii=False))


def tweet_hash(tweet_id: str) -> str:
    return hashlib.md5(str(tweet_id).encode()).hexdigest()[:12]


def score_tweet(text: str) -> tuple[int, list[str]]:
    """返回 (得分, 命中关键词列表)"""
    text_lower = text.lower()
    hits = [kw for kw in OPPORTUNITY_KEYWORDS if kw.lower() in text_lower]
    excl = [kw for kw in EXCLUDE_KEYWORDS if kw in text]
    score = len(hits) * 10 - len(excl) * 5
    return score, hits


def search_tweets(client: tweepy.Client) -> list[dict]:
    """搜索并返回候选推文列表"""
    results = []
    seen_texts = set()

    for query in QUERIES:
        try:
            resp = client.search_recent_tweets(
                query=f"{query} -is:retweet",
                max_results=MAX_RESULTS_PER_QUERY,
                tweet_fields=["public_metrics", "created_at", "author_id"],
                expansions=["author_id"],
                user_fields=["username", "name", "public_metrics"],
            )
            if not resp.data:
                continue

            user_map = {}
            if resp.includes and "users" in resp.includes:
                for u in resp.includes["users"]:
                    user_map[u.id] = u

            for tweet in resp.data:
                # 去重
                sig = tweet.text[:50]
                if sig in seen_texts:
                    continue
                seen_texts.add(sig)

                metrics = tweet.public_metrics or {}
                likes    = metrics.get("like_count", 0)
                retweets = metrics.get("retweet_count", 0)
                replies  = metrics.get("reply_count", 0)
                engagement = likes + retweets * 2 + replies

                score, hits = score_tweet(tweet.text)

                # 过滤：得分>0 或 互动量>200
                if score <= 0 and engagement < 200:
                    continue

                user = user_map.get(tweet.author_id)
                username = f"@{user.username}" if user else "unknown"
                followers = user.public_metrics.get("followers_count", 0) if user else 0

                results.append({
                    "id":         str(tweet.id),
                    "text":       tweet.text,
                    "username":   username,
                    "followers":  followers,
                    "likes":      likes,
                    "retweets":   retweets,
                    "engagement": engagement,
                    "score":      score,
                    "hits":       hits,
                    "created_at": str(tweet.created_at),
                })
        except Exception as e:
            print(f"[搜索失败] {query}: {e}", file=sys.stderr)

    # 按综合分排序
    results.sort(key=lambda x: -(x["score"] * 100 + x["engagement"]))
    return results


def format_alert(tweets: list[dict]) -> str:
    """格式化推送消息"""
    now = datetime.datetime.utcnow().strftime("%m-%d %H:%M UTC")
    lines = [f"🐦 Twitter Web3 热点 [{now}]"]
    lines.append(f"发现 {len(tweets)} 条有价值信息：\n")

    for i, t in enumerate(tweets[:5], 1):
        hits_str = " ".join(f"#{h}" for h in t["hits"][:3])
        text_preview = t["text"][:120].replace("\n", " ")
        lines.append(
            f"{i}. {t['username']} (粉丝{t['followers']//1000}k)\n"
            f"   {text_preview}...\n"
            f"   ❤️{t['likes']} 🔁{t['retweets']}  {hits_str}"
        )
        if i < len(tweets[:5]):
            lines.append("")

    return "\n".join(lines)


def main():
    cfg = load_config()
    client = tweepy.Client(
        consumer_key=cfg["consumer_key"],
        consumer_secret=cfg["consumer_secret"],
        access_token=cfg["access_token"],
        access_token_secret=cfg["access_token_secret"],
        wait_on_rate_limit=True,
    )

    seen = load_seen()
    tweets = search_tweets(client)

    # 过滤已推送
    new_tweets = [t for t in tweets if tweet_hash(t["id"]) not in seen]

    if not new_tweets:
        print("HEARTBEAT_OK")
        return

    # 更新已见集合
    for t in new_tweets:
        seen.add(tweet_hash(t["id"]))
    save_seen(seen)

    # 只取得分最高的
    top = [t for t in new_tweets if t["score"] >= 10 or t["engagement"] >= 300]
    if not top:
        print("HEARTBEAT_OK")
        return

    alert = format_alert(top)
    print(alert)


if __name__ == "__main__":
    main()
