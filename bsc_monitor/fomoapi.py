# ============================================================
# fomoapi.py — fomoapi REST 封装（BSC 链）
# 多 key 轮询池：每次请求轮换 key，402/401 自动切下一个
# （免费额度按月重置，耗尽的 key 留在池尾，次月自动复活）
# ============================================================
import threading

import requests

# 3 个新 key 轮询 + 1 个旧 key（额度耗尽但每月重置，留池尾备用）
FOMO_API_KEYS = [
    "fapi_53893b4d91d04b4fb486d8e71d02368027cc88d41c914c218376a4222e75e14d",
    "fapi_145a48559a2449fcaae09e37383ff39a9f1a37390e9e4f8cbbc13a4c9dc01ba7",
    "fapi_2f06fffa846b4f46ae63c72b272b87ab3ebb2ae720c94db4961cb36751d32d9b",
    "fapi_d7563942af9d4ba0928d662a257307bf4833a12b44c946b0a8bf477346fe5761",  # 旧 key，月重置后回用
]

FOMO_WS_URL = "wss://api.fomoapi.io/ws/alerts"

_PROXIES = {
    "http": "socks5://REDACTED_PROXY_CREDENTIALS",
    "https": "socks5://REDACTED_PROXY_CREDENTIALS",
}
_BASE = "https://api.fomoapi.io"

_key_idx = 0
_key_lock = threading.Lock()
_exhaust_warn_ts = 0.0   # 每小时最多警告一次全池耗尽


def _current_key() -> str:
    return FOMO_API_KEYS[_key_idx]


def current_key_hint() -> str:
    """调试用：返回当前 key 前 14 位"""
    return _current_key()[:14] + "…"


def _rotate(reason: str = ""):
    global _key_idx
    with _key_lock:
        _key_idx = (_key_idx + 1) % len(FOMO_API_KEYS)


def _warn_exhausted(status: int):
    global _exhaust_warn_ts
    import time
    now = time.time()
    if now - _exhaust_warn_ts > 3600:
        _exhaust_warn_ts = now
        print(f"[FOMO-API] ⚠ 所有 {len(FOMO_API_KEYS)} 个 key 均不可用（最后状态 {status}），本轮放弃", flush=True)


def _get(path: str, timeout: int = 12) -> dict:
    """带 key 轮询的 GET。轮换规则：
    - 成功（200）→ 轮换到下一个 key（均摊用量）
    - 401/402（无效/额度耗尽）→ 立即轮换并重试下一个
    - 网络异常 → 不轮换（避免代理抖动烧 key），直接返回空
    """
    url = f"{_BASE}{path}"
    last_status = 0
    for _ in range(len(FOMO_API_KEYS)):
        k = _current_key()
        try:
            r = requests.get(url, headers={"authorization": f"Bearer {k}"},
                             proxies=_PROXIES, timeout=timeout)
        except Exception:
            return {}
        if r.status_code in (401, 402):
            last_status = r.status_code
            _rotate(reason=f"{r.status_code}")
            continue
        if r.status_code == 200:
            try:
                d = r.json()
            except Exception:
                return {}
            _rotate()  # 均摊用量
            return d
        # 其他状态码（4xx/5xx）不轮换 key，视为接口问题
        return {}
    _warn_exhausted(last_status)
    return {}


def alerts_since(cursor: str = "", chain: str = "bsc") -> dict:
    """拉最新 alerts（50 条/次）。返回 {alerts, nextCursor}"""
    q = f"/v2/alerts?chain={chain}" + (f"&cursor={cursor}" if cursor else "")
    return _get(q)


def trade_detail(trade_id: str) -> dict:
    """拉取 trade 详情（含 trader.wallet、token.liquidityUsd、position 信息）"""
    return _get(f"/v2/trades/{trade_id}")


def handle_wallet(handle: str) -> str:
    """handle → evm wallet（走 /v2/trades 拿 wallet 字段，免费）"""
    # 实际上 trade detail 才带 wallet。alerts 不带，所以在 process_alert 里传 trade_id 进来
    return ""
