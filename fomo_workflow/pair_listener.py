"""
pair_listener.py — Robinhood Chain 新 Pair 零延迟监听器

架构：
  WebSocket RPC → eth_subscribe logs → 实时推送新 Pair 地址
  → 塞进 asyncio.Queue → ws_monitor.py 的 handle_new_pair() 处理

支持 V2（PairCreated）和 V3（PoolCreated）双工厂事件。
断线后指数退避重连，最长 60s 间隔。
"""

import asyncio
import json
import logging
import time

import websockets

try:
    from config import WS_ERROR_RATE_WINDOW, WS_ERROR_RATE_THRESHOLD, WS_FALLBACK_POLL_SEC, JARVIS_TARGET
except ImportError:
    WS_ERROR_RATE_WINDOW    = 60
    WS_ERROR_RATE_THRESHOLD = 3
    WS_FALLBACK_POLL_SEC    = 15
    JARVIS_TARGET           = ""

logger = logging.getLogger("pair_listener")

# ── Era 2 工厂合约（当前主流，必须两个都监听）────────────────────────
UNISWAP_V2_FACTORY = "0x8bceaa40b9acdfaedf85adf4ff01f5ad6517937f"
UNISWAP_V3_FACTORY = "0x1f7d7550b1b028f7571e69a784071f0205fd2efa"

# 事件 topic（keccak256 of signature）
PAIR_CREATED_TOPIC = "0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9"
POOL_CREATED_TOPIC = "0x783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118"

# ── 断线重连参数 ─────────────────────────────────────────────────────
RECONNECT_BASE_SEC  = 2     # 初始等待
RECONNECT_MAX_SEC   = 60    # 最长等待
RECONNECT_MULTIPLIER = 2    # 指数退避倍数

# ── 限速检测状态 ────────────────────────────────────────────
_ws_error_timestamps: list[float] = []    # 记录最近断线时间戳
_ws_active:           bool         = True  # True=WSS活跃 False=已降级到轮询
_fallback_queue: asyncio.Queue | None = None  # 内部 pair_queue 引用

# ── 去重 TTL（防止 WSS replay 重复推送）──────────────────────────────
SEEN_PAIRS: dict[str, float] = {}  # tx_hash+log_index → timestamp
SEEN_TTL = 300   # 5 分钟


def _parse_v2_pair(log: dict) -> str | None:
    """
    解析 V2 PairCreated 事件，返回新 pair 的 token1 地址（最后一个 token）。
    事件 ABI: PairCreated(address indexed token0, address indexed token1, address pair, uint256)
    topics[0] = 事件 topic
    topics[1] = token0（indexed）
    topics[2] = token1（indexed）
    data = abi.encode(address pair, uint256 pairCount)
    """
    try:
        topics = log.get("topics", [])
        data   = log.get("data", "")
        if len(topics) < 3 or len(data) < 66:
            return None
        token0 = "0x" + topics[1][-40:]
        token1 = "0x" + topics[2][-40:]
        pair   = "0x" + data[26:66]   # 前 32 字节是 pair 地址（去掉 0x 和 24 个零填充）
        return pair, token0, token1
    except Exception:
        return None


def _parse_v3_pool(log: dict) -> tuple | None:
    """
    解析 V3 PoolCreated 事件，返回 (pool, token0, token1)。
    事件 ABI: PoolCreated(address indexed token0, address indexed token1,
                          uint24 indexed fee, int24 tickSpacing, address pool)
    topics[1] = token0, topics[2] = token1
    data = abi.encode(int24 tickSpacing, address pool)
    """
    try:
        topics = log.get("topics", [])
        data   = log.get("data", "")
        if len(topics) < 3 or len(data) < 130:
            return None
        token0 = "0x" + topics[1][-40:]
        token1 = "0x" + topics[2][-40:]
        # data[0:64] = tickSpacing（int24，左填充）, data[64:128] = pool address
        pool = "0x" + data[90:130]
        return pool, token0, token1
    except Exception:
        return None


def _dedup_key(log: dict) -> str:
    return f"{log.get('transactionHash','?')}:{log.get('logIndex','?')}"


def _is_dup(log: dict) -> bool:
    now = time.time()
    # 清理过期
    expired = [k for k, ts in SEEN_PAIRS.items() if now - ts > SEEN_TTL]
    for k in expired:
        del SEEN_PAIRS[k]
    key = _dedup_key(log)
    if key in SEEN_PAIRS:
        return True
    SEEN_PAIRS[key] = now
    return False


async def _subscribe_once(wss_url: str, pair_queue: asyncio.Queue):
    """建立一次 WSS 连接并持续监听，断线则抛出异常由外层重连。"""
    # 优先从 config 读取工厂列表，兼容旧版仅双工厂场景
    try:
        from config import MONITORED_FACTORIES
        factories: dict = MONITORED_FACTORIES
    except ImportError:
        factories = {
            UNISWAP_V2_FACTORY: {"version": "V2", "event": "PAIR_CREATED"},
            UNISWAP_V3_FACTORY: {"version": "V3", "event": "POOL_CREATED"},
        }

    logger.info(f"[WS-PAIR] 连接 {wss_url[:60]}...")
    async with websockets.connect(
        wss_url,
        ping_interval=20,
        ping_timeout=10,
        open_timeout=15,
    ) as ws:
        # 为每个工厂地址建立订阅
        sub_meta: dict = {}   # sub_id → {version, event}
        req_id = 1
        for factory_addr, meta in factories.items():
            ev   = meta.get("event", "PAIR_CREATED")
            topic = PAIR_CREATED_TOPIC if ev == "PAIR_CREATED" else POOL_CREATED_TOPIC
            await ws.send(json.dumps({
                "jsonrpc": "2.0", "id": req_id,
                "method":  "eth_subscribe",
                "params":  ["logs", {
                    "address": factory_addr,
                    "topics":  [topic],
                }],
            }))
            r = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            sub_id = r.get("result")
            sub_meta[sub_id] = meta
            logger.info(f"[WS-PAIR] {meta['version']} {factory_addr[:12]}… 订阅 id={sub_id}")
            req_id += 1

        logger.info(f"[WS-PAIR] ✅ {len(factories)} 个工厂订阅成功，开始监听...")

        async for raw in ws:
            msg = json.loads(raw)
            if "params" not in msg:
                continue

            log    = msg["params"]["result"]
            topics = log.get("topics", [])
            if not topics:
                continue

            # 去重
            if _is_dup(log):
                continue

            event_topic = topics[0]
            parsed = None
            ver    = "?"

            if event_topic == PAIR_CREATED_TOPIC:
                parsed = _parse_v2_pair(log)
                ver    = "V2"
            elif event_topic == POOL_CREATED_TOPIC:
                parsed = _parse_v3_pool(log)
                # 尝试从 sub_meta 推断具体版本（V3/V4）
                _sub_id_raw = msg["params"].get("subscription")
                ver = sub_meta.get(_sub_id_raw, {}).get("version", "V3")
            else:
                continue

            if not parsed:
                continue

            pair_addr, token0, token1 = parsed
            logger.info(f"[WS-PAIR] 🆕 {ver} pair={pair_addr[:12]}… "
                        f"token0={token0[:10]}… token1={token1[:10]}…")

            await pair_queue.put({
                "source":    "ws_rpc",
                "version":   ver,
                "pair":      pair_addr,
                "token0":    token0,
                "token1":    token1,
                "block":     log.get("blockNumber"),
                "tx_hash":   log.get("transactionHash"),
                "timestamp": time.time(),
            })


async def run_pair_listener(wss_url: str, pair_queue: asyncio.Queue):
    """
    持久运行的监听协程，断线自动指数退避重连。
    内嵌限速检测：60s 内断线 ≥ 3 次 → 自动降级到 DexScreener 轮询。
    应由 asyncio.gather 在 main() 中启动。
    """
    global _ws_active, _fallback_queue
    _fallback_queue = pair_queue
    delay = RECONNECT_BASE_SEC

    while True:
        try:
            _ws_active = True
            await _subscribe_once(wss_url, pair_queue)
            # 正常退出（正常断开）重置 delay
            delay = RECONNECT_BASE_SEC
        except (websockets.exceptions.ConnectionClosed,
                websockets.exceptions.WebSocketException,
                OSError, asyncio.TimeoutError) as e:
            logger.warning(f"[WS-PAIR] 连接断开: {e}")
            await _on_ws_disconnect(pair_queue, wss_url, delay)
        except Exception as e:
            logger.error(f"[WS-PAIR] 未预期异常: {e}")
            await _on_ws_disconnect(pair_queue, wss_url, delay)

        if not _ws_active:
            # 已降级到轮询模式，停止重连循环
            return

        await asyncio.sleep(delay)
        delay = min(delay * RECONNECT_MULTIPLIER, RECONNECT_MAX_SEC)


def _notify_pair_listener(msg: str):
    """内部推送工具（防止循环 import）"""
    try:
        import subprocess
        subprocess.Popen(
            ["openclaw", "message", "send",
             "--channel", "jarvis", "-t", JARVIS_TARGET, "-m", msg],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


async def _on_ws_disconnect(pair_queue: asyncio.Queue, wss_url: str, delay: float):
    """
    断线时调用：记录时间戳并判断是否触发限速降级。
    - 如果 WS_ERROR_RATE_WINDOW 秒内断线次数 >= WS_ERROR_RATE_THRESHOLD → 降级
    - 否则 → 带退避重连（和旧逻辑相同）
    """
    global _ws_active
    now = time.time()
    _ws_error_timestamps.append(now)
    # 清理窗口外过期记录
    _ws_error_timestamps[:] = [
        ts for ts in _ws_error_timestamps
        if now - ts < WS_ERROR_RATE_WINDOW
    ]
    recent_errors = len(_ws_error_timestamps)
    logger.warning(f"[WS-PAIR] 断线次数 {recent_errors}/{WS_ERROR_RATE_THRESHOLD}"
                   f"（窗口 {WS_ERROR_RATE_WINDOW}s）")

    if recent_errors >= WS_ERROR_RATE_THRESHOLD:
        logger.warning("[WS-PAIR] ❗ 超过限速阈值，启动降级模式")
        await _activate_fallback_mode(pair_queue)
    else:
        # 正常退避延迟
        await asyncio.sleep(delay)


async def _activate_fallback_mode(pair_queue: asyncio.Queue):
    """停止 WSS，启动 DexScreener 轮询兄底，推送 Jarvis 告警。"""
    global _ws_active
    _ws_active = False
    logger.error("[WS-PAIR] 🔴 已降级到 DexScreener 轮询模式")
    _notify_pair_listener(
        f"🔴 QuickNode WSS 痑似限速，已降级到 DexScreener 轮询兄底\n"
        f"轮询间隔 {WS_FALLBACK_POLL_SEC}s，请检查 QuickNode 配额。"
    )
    asyncio.create_task(_dexscreener_fallback_poller(pair_queue))


async def _dexscreener_fallback_poller(pair_queue: asyncio.Queue):
    """
    降级后定期从 DexScreener 拉取新 pair，推入 pair_queue。
    与现有 handle_new_pair_loop 公用同一队列，不需额外拓展。
    """
    import requests
    logger.info(f"[WS-PAIR-FALLBACK] 启动 DexScreener 轮询，间隔 {WS_FALLBACK_POLL_SEC}s")
    seen: set[str] = set()

    while not _ws_active:   # 一旦 WSS 恢复则停止（当前设计不自动恢复，需运维手动重启）
        try:
            r = requests.get(
                "https://api.dexscreener.com/token-profiles/latest/v1",
                timeout=8,
                proxies={"https": "socks5://REDACTED_PROXY_CREDENTIALS"},
            )
            profiles = r.json() if isinstance(r.json(), list) else []
            for item in profiles:
                addr = (item.get("tokenAddress") or "").lower()
                if not addr or addr in seen:
                    continue
                seen.add(addr)
                # 封装为与 WSS 兄容的格式
                await pair_queue.put({
                    "source":    "dex_fallback",
                    "version":   "fallback",
                    "pair":      addr,
                    "token0":    addr,
                    "token1":    "",
                    "block":     None,
                    "tx_hash":   None,
                    "timestamp": time.time(),
                })
                logger.info(f"[WS-PAIR-FALLBACK] 推入 pair {addr[:12]}…")
        except Exception as e:
            logger.warning(f"[WS-PAIR-FALLBACK] DexScreener 获取失败: {e}")

        await asyncio.sleep(WS_FALLBACK_POLL_SEC)

    logger.info("[WS-PAIR-FALLBACK] WSS 已恢复，将轮询崭本轮询已停止")


# ── 独立测试入口 ──────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    wss_url = sys.argv[1] if len(sys.argv) > 1 else \
        "wss://robinhood-mainnet.g.alchemy.com/v2/YOUR_ALCHEMY_KEY"

    q = asyncio.Queue()
    async def _main():
        await asyncio.gather(
            run_pair_listener(wss_url, q),
            _printer(q),
        )
    async def _printer(q):
        while True:
            item = await q.get()
            print(f">>> 新 pair: {json.dumps(item, indent=2)}")
    asyncio.run(_main())
