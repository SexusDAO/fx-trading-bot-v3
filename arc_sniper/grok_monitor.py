"""
Twitter 监控 — Clawby x_search 版（零 API 费用）
===================================================
通过 Clawby x_search 端点搜索 Arc 生态账号的最近推文，
用规则提取合约地址（CA），发现新代币后自动买入。

替代旧版 Grok API 方案：
    - 旧版：Grok API 每 10 秒轮询，$1296/月
    - 新版：Clawby x_search 每 5 分钟搜一次，$0
    - 数据来源相同（X/Twitter），只是用规则代替 LLM 提取 CA

频率设计：
    - 3 组账号 × 1 次 API/组 = 3 次/轮
    - 每 5 分钟一轮 = 3 次/5分钟
    - 月消耗 = 3 × 12 × 24 × 30 = 2592 次/月（5000 限额内 ✅）
"""

import logging
import time
import json
import os
import re
from typing import Optional
from web3 import Web3

import requests

from . import config
from .executor import Executor
from .onchain import get_token_info, verify_contract_on_chain
from .notify import _send_jarvis
from .nft_minter import NFTMinter, extract_nft_signals_from_tweets, process_nft_signal
from .signal_aggregator import score_token_multi_dim

# 共享的 Twitter 分析模块
import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from grok_analyzer import analyze_token_twitter, format_for_arc_sniper, get_buy_recommendation

logger = logging.getLogger("arc.grok_monitor")

# 502 退避状态
_consecutive_502 = 0
_backoff_until = 0

# ═══════════════════════════════════════════════════════════════
# Clawby 配置
# ═══════════════════════════════════════════════════════════════
CLAWBY_KEY = os.getenv("CLAWBY_KEY", "pk_xBgG9wi3q1ZnIe6peyFrBHQP_6v2hG2C")
CLAWBY_RELAY = "https://api.openclawby.com/api/relay"
_PROXY = {
    "https": "socks5://REDACTED_PROXY_CREDENTIALS",
    "http":  "socks5://REDACTED_PROXY_CREDENTIALS",
}

# ═══════════════════════════════════════════════════════════════
# 监控参数
# ═══════════════════════════════════════════════════════════════
POLL_INTERVAL_SEC = 1800     # 每 30 分钟轮询一次（5000/月限额内: 3组×48轮/天×30天=4320）
MAX_BUYS_PER_HOUR = 5         # 每小时最多 5 笔
MIN_BUY_INTERVAL_SEC = 30    # 两笔买入之间最少 30 秒
BUY_AMOUNT_USDC = 50         # Twitter 信号每笔 $50 USDC

# ═══════════════════════════════════════════════════════════════
# 监控账号分组（每组 ≤3 个账号，用 OR 搜索）
# ═══════════════════════════════════════════════════════════════
ACCOUNT_GROUPS = [
    {
        "name": "official",
        # 官方账号 — 最可能发新代币公告
        "query": "from:arc",
        "accounts": ["arc"],
        "priority": 1,
    },
    {
        "name": "launchpads_core",
        # 核心发射台 — 会发 CA 的主要来源，合并成一组
        "query": "from:Arcanedotfi OR from:Zyoradotfun OR from:rwarcdotfun OR from:sashimidotfun OR from:arcpad_meme OR from:eve_dot_fun OR from:Ayooclub OR from:liftdotfun OR from:focidotfamily OR from:TollyLabs OR from:minarafun OR from:circlewarp OR from:Archemistdotfun OR from:PUMP_archi OR from:UBIdotFUN OR from:Arguspad OR from:onmidotfun OR from:Fliptfun OR from:arcfunxyz OR from:ArcadeSwap OR from:synthra_finance",
        "accounts": ["Arcanedotfi", "Zyoradotfun", "rwarcdotfun", "sashimidotfun", "arcpad_meme", "eve_dot_fun", "Ayooclub", "liftdotfun", "focidotfamily", "TollyLabs", "minarafun", "circlewarp", "Archemistdotfun", "PUMP_archi", "UBIdotFUN", "Arguspad", "onmidotfun", "Fliptfun", "arcfunxyz", "ArcadeSwap", "synthra_finance"],
        "priority": 2,
    },
    {
        "name": "ecosystem_watch",
        # 生态项目方 — 偶尔发 CA，低优先级
        "query": "from:Longdotsupply OR from:RadarDEXScan OR from:ACTFUN OR from:Orixaxyz OR from:gigawork_arc OR from:swaparc_app",
        "accounts": ["Longdotsupply", "RadarDEXScan", "ACTFUN", "Orixaxyz", "gigawork_arc", "swaparc_app"],
        "priority": 3,
    },
]

# ═══════════════════════════════════════════════════════════════
# 去重
# ═══════════════════════════════════════════════════════════════
_PROCESSED_FILE = os.path.join(config.BASE_DIR, ".grok_processed.json")
_processed_cas: set = set()
# 记录已处理的 tweet_id，防止同一推文重复提取
_seen_tweet_ids: set = set()


def _load_processed():
    if os.path.exists(_PROCESSED_FILE):
        try:
            return set(json.load(open(_PROCESSED_FILE)))
        except Exception:
            pass
    return set()


def _save_processed():
    items = list(_processed_cas)[-5000:]
    json.dump(items, open(_PROCESSED_FILE, "w"))


_processed_cas = _load_processed()
logger.info(f"[Monitor] 已加载 {len(_processed_cas)} 个已处理 CA")


# ═══════════════════════════════════════════════════════════════
# Clawby x_search 调用
# ═══════════════════════════════════════════════════════════════

def _clawby_search(query: str, count: int = 20) -> list[dict]:
    """调用 Clawby x_search 搜索推文"""
    global _consecutive_502, _backoff_until
    try:
        # 退避机制：连续 502 后等待 5 分钟再重试
        if _consecutive_502 >= 3 and time.time() < _backoff_until:
            return []

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
            timeout=20,
        )
        if resp.status_code != 200:
            _consecutive_502 += 1
            if _consecutive_502 == 1 or _consecutive_502 % 10 == 0:
                logger.warning(f"[Monitor] Clawby x_search HTTP {resp.status_code} (连续第{_consecutive_502}次，退避中)")
            if _consecutive_502 >= 3:
                _backoff_until = time.time() + 300  # 5 分钟退避
            return []

        _consecutive_502 = 0
        _backoff_until = 0

        data = resp.json()
        tweets = data.get("data", [])
        if not isinstance(tweets, list):
            return []
        return tweets
    except Exception as e:
        _consecutive_502 += 1
        if _consecutive_502 == 1 or _consecutive_502 % 10 == 0:
            logger.warning(f"[Monitor] Clawby x_search 异常: {e} (连续第{_consecutive_502}次)")
        if _consecutive_502 >= 3:
            _backoff_until = time.time() + 300
        return []


# ═══════════════════════════════════════════════════════════════
# CA 提取（规则替代 LLM）
# ═══════════════════════════════════════════════════════════════

CA_PATTERN = re.compile(r"0x[0-9a-fA-F]{40}")

# 已知代币地址（排除列表，防止返回已存在的老币）
KNOWN_TOKEN_ADDRS = {
    "0xbc43ce8dec648ea298c4275559b81d6261c90b67",  # TOLLY
    "0x384c60f98ecd4c26345499345c03d677e40f115e",  # WARP
    "0xeb64987643db71c76b2a2be7e723decc995e5b37",  # COOL
    "0x8bcb94279fc2c984ec34e0c1f2192df8c69ea4f0",  # Architects
    "0x07704b06981ea962b87296362a1281484d160000",  # ARCAT
    "0x41c8a71f630c636294009fa4fb0cc4c3bbe674fe",  # BEANCAT
}

# 发射台已知合约（排除列表）
LAUNCHPAD_CONTRACTS = {
    # 已知的 factory/router 地址，不是新代币
}


def extract_signals_from_tweets(tweets: list[dict], group: dict) -> list[dict]:
    """
    从推文列表中用正则提取合约地址（替代 Grok LLM）。

    返回信号列表，每个信号包含 CA、推文信息、置信度。
    """
    signals = []
    accounts = set(a.lower() for a in group["accounts"])

    for tweet in tweets:
        tweet_id = tweet.get("id", "")
        if tweet_id and tweet_id in _seen_tweet_ids:
            continue

        user = tweet.get("user", {})
        username = user.get("username", "")
        text = tweet.get("text", "")
        tweet_url = tweet.get("url", "")
        created_at = tweet.get("created_at", "")

        # 提取所有 0x 开头的 40 位 hex 地址
        matches = CA_PATTERN.findall(text)

        if not matches:
            continue

        # 去重已知地址
        new_addresses = []
        for addr in matches:
            addr_lower = addr.lower()
            if addr_lower not in KNOWN_TOKEN_ADDRS and addr_lower not in LAUNCHPAD_CONTRACTS:
                new_addresses.append(addr_lower)

        if not new_addresses:
            continue

        # 标记 tweet 为已处理
        if tweet_id:
            _seen_tweet_ids.add(tweet_id)

        # 判断置信度
        text_lower = text.lower()
        high_confidence_keywords = [
            "launch", "new token", "ca:", "contract", "just launched",
            "live", "now live", "deploying", "deployed",
            "token address", "0x", "add lp", "pool",
        ]
        confidence = "high" if any(kw in text_lower for kw in high_confidence_keywords) else "medium"

        # 推断 token name 和 symbol
        token_name = ""
        token_symbol = ""

        # 尝试从推文提取 $SYMBOL
        symbol_match = re.search(r"\$([A-Z]{2,10})\b", text)
        if symbol_match:
            token_symbol = symbol_match.group(1)

        # 尝试从推文提取代币名
        name_patterns = [
            r"(?:token|coin|project)\s+(?:is\s+)?called\s+([A-Z][a-zA-Z0-9]+)",
            r"(?:introducing|announcing|presenting)\s+([A-Z][a-zA-Z0-9]+)",
        ]
        for pat in name_patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                token_name = m.group(1)
                break

        for addr in new_addresses:
            signals.append({
                "account": username,
                "tweet_url": tweet_url,
                "contract_address": addr,
                "token_name": token_name,
                "token_symbol": token_symbol,
                "tweet_text": text[:200],
                "confidence": confidence,
                "tweet_created_at": created_at,
            })

    return signals


# ═══════════════════════════════════════════════════════════════
# CA 验证
# ═══════════════════════════════════════════════════════════════

def validate_ca(address: str) -> bool:
    """验证地址格式 + 不在已知列表中 + 不在已处理列表中"""
    addr = address.lower().strip()
    if not CA_PATTERN.fullmatch(addr):
        return False
    if addr in KNOWN_TOKEN_ADDRS:
        return False
    if addr in _processed_cas:
        return False
    return True


# ═══════════════════════════════════════════════════════════════
# 监控主类
# ═══════════════════════════════════════════════════════════════

class GrokMonitor:
    """通过 Clawby x_search 监控 Arc 生态 Twitter，发现 CA 后自动买入。"""

    def __init__(self, w3: Web3, executor: Optional[Executor] = None, nft_minter: Optional[NFTMinter] = None):
        self.w3 = w3
        self.executor = executor
        self.nft_minter = nft_minter
        self.last_buy_time = 0.0
        self.buy_history: list[float] = []
        self._poll_counter = 0

        total_accounts = sum(len(g["accounts"]) for g in ACCOUNT_GROUPS)
        logger.info(
            f"[Monitor] 监控 {total_accounts} 个账号，"
            f"每 {POLL_INTERVAL_SEC}s 轮询一次（Clawby x_search）"
        )

    # ── 频率控制 ──────────────────────────────────────────────

    def _can_buy(self) -> tuple[bool, str]:
        now = time.time()
        if now - self.last_buy_time < MIN_BUY_INTERVAL_SEC:
            return False, f"频率限制：还需等待 {MIN_BUY_INTERVAL_SEC - (now - self.last_buy_time):.0f}s"

        self.buy_history = [t for t in self.buy_history if now - t < 3600]
        if len(self.buy_history) >= MAX_BUYS_PER_HOUR:
            return False, f"小时限额：已买 {len(self.buy_history)}/{MAX_BUYS_PER_HOUR} 笔"

        return True, ""

    def _record_buy(self):
        self.last_buy_time = time.time()
        self.buy_history.append(self.last_buy_time)

    # ── 主循环 ────────────────────────────────────────────────

    def run_forever(self):
        """主循环 — 每 5 分钟用 Clawby x_search 搜索推文"""
        logger.info("[Monitor] 开始监控（Clawby x_search）...")

        while True:
            try:
                self._poll_once()
                time.sleep(POLL_INTERVAL_SEC)
            except KeyboardInterrupt:
                logger.info("[Monitor] 收到退出信号")
                break
            except Exception as e:
                logger.error(f"[Monitor] 主循环异常: {e}")
                time.sleep(POLL_INTERVAL_SEC)

    def _poll_once(self):
        """一次轮询：搜索所有组的最近推文"""
        for group in ACCOUNT_GROUPS:
            try:
                self._check_group(group)
            except Exception as e:
                logger.error(f"[Monitor] 组 {group['name']} 异常: {e}")

        self._poll_counter += 1

    def _check_group(self, group: dict):
        """检查一组账号的最近推文"""
        query = group["query"]
        priority = group["priority"]
        group_name = group["name"]

        tweets = _clawby_search(query, count=20)
        if not tweets:
            return

        # 过滤：只看最近 10 分钟的推文
        now = time.time()
        recent_tweets = []
        for t in tweets:
            # Clawby 返回 created_at 字符串如 "Fri Sep 11 18:29:31 +0000 2026"
            created_str = t.get("created_at", "")
            if created_str:
                try:
                    from email.utils import parsedate_to_datetime
                    dt = parsedate_to_datetime(created_str)
                    tweet_ts = dt.timestamp()
                    if now - tweet_ts > 600:  # 超过 10 分钟
                        continue
                except Exception:
                    pass  # 解析失败就不过滤时间
            recent_tweets.append(t)

        if not recent_tweets:
            return

        # 提取 CA 信号（代币）
        signals = extract_signals_from_tweets(recent_tweets, group)

        if signals:
            logger.info(f"[Monitor] {group_name} 组发现 {len(signals)} 个代币信号!")

        for signal in signals:
            self._process_signal(signal, group)

        # 提取 NFT 信号
        nft_signals = extract_nft_signals_from_tweets(recent_tweets, group)

        if nft_signals:
            logger.info(f"[Monitor] {group_name} 组发现 {len(nft_signals)} 个 NFT 信号!")

        for nft_signal in nft_signals:
            try:
                process_nft_signal(nft_signal, self.nft_minter, group)
            except Exception as e:
                logger.error(f"[Monitor] NFT 信号处理异常: {e}")

    def _process_signal(self, signal: dict, group: dict):
        """处理一个 Twitter 信号 — 走统一管道 process_gmgn_signal"""
        account = signal.get("account", "").lstrip("@")
        tweet_url = signal.get("tweet_url", "")
        ca = signal.get("contract_address", "").lower().strip()
        token_name = signal.get("token_name", "")
        token_symbol = signal.get("token_symbol", "")
        confidence = signal.get("confidence", "medium")
        priority = group["priority"]

        # 1. 验证 CA 格式
        if not validate_ca(ca):
            logger.debug(f"[Monitor] CA 格式无效或已处理: {ca[:12]}...")
            return

        # 2. 标记为已处理
        _processed_cas.add(ca)
        _save_processed()

        # 3. 链上验证
        if not verify_contract_on_chain(self.w3, ca):
            logger.info(f"[Monitor] 链上无合约: {ca[:12]}... (可能不在 Arc 链上或未部署)")
            return

        logger.info(
            f"[Monitor] 链上验证通过: {ca[:12]}... "
            f"来自 @{account} confidence={confidence}"
        )

        # 4. 构建统一格式的信号数据，走 process_gmgn_signal 管道
        signal_data = {
            "token_address": ca,
            "signal_type": 999,  # Twitter 信号
            "trigger_at": int(time.time()),
            "id": f"twitter_{ca}_{int(time.time())}",
            # 补充 Twitter 特有信息
            "account": account,
            "tweet_url": tweet_url,
            "confidence": confidence,
            "priority": priority,
            "token_name": token_name,
            "token_symbol": token_symbol,
        }

        # 5. 走统一管道：安全门 + 评分 + 买入
        result = score_token_multi_dim(signal_data, signal_source="twitter")

        if result.get("should_buy") and self.executor:
            buy_result = self.executor.buy_token(
                ca, config.BUY_AMOUNT_USDC, "dag", ""
            )
            result["buy_result"] = buy_result
            result["action"] = "buy" if buy_result.get("success") else "buy_failed"
        else:
            result["action"] = "skip"

        # 6. 频率控制（Twitter 通道独立计频）
        if result.get("action") == "buy" and result.get("buy_result", {}).get("success"):
            self._record_buy()
            logger.info(f"[Monitor] Twitter 信号买入成功: {ca[:12]}...")

        logger.info(f"[Monitor] Twitter 信号处理完成: {result.get('action', '?')} score={result.get('score', 0)}")

    def _infer_dex_type(self, account: str) -> str:
        """所有 swap 统一走 DAG Router"""
        return "dag"

    # ── 通知 ──────────────────────────────────────────────────

    def _notify_buy(
        self, token_info: dict, account: str, tweet_url: str,
        confidence: str, priority: int, buy_result: dict, twitter_analysis: str = "",
    ):
        """推送买入通知"""
        symbol = token_info.get("symbol", "?")
        name = token_info.get("name", "?")
        addr = token_info.get("address", "")

        emoji = "🔥" if priority == 1 else "🚀" if priority == 2 else "💡"
        conf_emoji = {"high": "✅", "medium": "⚠️", "low": "❓"}.get(confidence, "⚠️")

        lines = [
            f"{emoji} ARC Twitter 信号买入",
            f"来源: @{account} (置信度 {conf_emoji} {confidence})",
            f"推文: {tweet_url}",
            "",
            f"代币: {name} (${symbol})",
            f"合约: {addr}",
            f"买入: ${BUY_AMOUNT_USDC} USDC",
        ]

        if buy_result.get("success"):
            lines.append(f"收到: {buy_result.get('token_received', 0):.2f} {symbol}")
            lines.append(f"TX: {buy_result.get('tx_hash', '')[:20]}...")
        else:
            lines.append(f"❌ 买入失败: {buy_result.get('error', '未知错误')}")

        if twitter_analysis:
            lines.append("")
            lines.append(twitter_analysis)

        _send_jarvis("\n".join(lines))

    def _notify_skip_twitter(
        self, token_info: dict, account: str, tweet_url: str, analysis: dict,
    ):
        """Twitter 分析建议不买入时推送通知"""
        symbol = token_info.get("symbol", "?")
        name = token_info.get("name", "?")
        addr = token_info.get("address", "")
        score = analysis.get("composite_score", 0)
        signal = analysis.get("buy_signal", "")

        lines = [
            f"⏭️ ARC 信号跳过（Twitter 分析不足）",
            f"来源: @{account}",
            f"代币: {name} (${symbol})",
            f"合约: {addr}",
            f"Twitter 综合: {score}/10  信号: {signal}",
            f"推文: {tweet_url}",
        ]

        _send_jarvis("\n".join(lines))

    def _notify_signal_only(
        self, token_info: dict, account: str, tweet_url: str,
        confidence: str, priority: int,
    ):
        """只通知不买入（无 executor 模式）"""
        symbol = token_info.get("symbol", "?")
        name = token_info.get("name", "?")
        addr = token_info.get("address", "")

        emoji = "🔥" if priority == 1 else "🚀" if priority == 2 else "💡"

        lines = [
            f"{emoji} ARC Twitter 信号（仅通知）",
            f"来源: @{account} (置信度 {confidence})",
            f"推文: {tweet_url}",
            f"代币: {name} (${symbol})",
            f"合约: {addr}",
        ]

        _send_jarvis("\n".join(lines))


# ═══════════════════════════════════════════════════════════════
# 独立运行入口
# ═══════════════════════════════════════════════════════════════

def main():
    """独立启动监控"""
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(config.LOG_FILE, encoding="utf-8"),
        ],
    )

    w3 = Web3(Web3.HTTPProvider(config.RPC_URL))
    if not w3.is_connected():
        logger.error(f"[Monitor] 无法连接 RPC: {config.RPC_URL}")
        sys.exit(1)

    executor = None
    nft_minter = None
    if config.PRIVATE_KEY:
        executor = Executor(w3)
        balance = executor.get_usdc_balance()
        logger.info(f"[Monitor] 钱包 USDC 余额: ${balance:.2f}")

        # NFT Minter 初始化
        nft_minter = NFTMinter(w3, config.PRIVATE_KEY)
        nft_balance = nft_minter._get_usdc_balance()
        logger.info(f"[Monitor] NFT Minter 就绪，余额: ${nft_balance:.2f}")
    else:
        logger.info("[Monitor] 未设置私钥，仅通知模式")

    monitor = GrokMonitor(w3, executor, nft_minter)
    monitor.run_forever()


if __name__ == "__main__":
    main()
