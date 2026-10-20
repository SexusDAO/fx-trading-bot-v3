#!/usr/bin/env python3
"""
coindata_ws.py — CoinData WebSocket 实时推送守护进程

功能：
  1. /ws/stream  → 订阅 funding-rate + open-interest 实时推送
     → 写入 ws_cache/stream_latest.json（供 scanner 读取，替代部分轮询）
  2. /ws/kol     → 订阅 signals + long-short-rank + whale-position-events
     → 写入 ws_cache/kol_latest.json（实时巨鲸/KOL 方向，供评分层读取）

设计原则：
  - 守护进程模式，断线自动重连（指数退避，最长60s）
  - 写入原子操作（tmp文件 → rename），不影响 scanner 读取
  - 本脚本不调用 CG REST 接口，不占用限速配额
  - 异常静默（写 ws_cache/ws_errors.log），不推送通知

认证：{"type": "auth", "key": "<API_KEY>"}  ← 注意是 key 不是 api_key
"""

import os
import json
import pathlib
import datetime
import threading
import time
import logging
import signal
import sys
import socks
import socket

import websocket

# ── 配置 ─────────────────────────────────────────────────────────────────────
WS_BASE     = "wss://coinglass-standard.quant-store.com/ws"
API_KEY = os.environ.get("SCRIPTS_COINDATA_WS_API_KEY", "")
WORKSPACE   = pathlib.Path("/root/.openclaw/workspace")
CACHE_DIR   = WORKSPACE / "ws_cache"
LOG_FILE    = CACHE_DIR / "ws_errors.log"

# 代理凭证从环境变量读取，格式：socks5://user:pass@host:port
_proxy_raw = os.environ.get("BINANCE_PROXY", "")
def _parse_proxy(url):
    import urllib.parse; p = urllib.parse.urlparse(url)
    return p.hostname or "", p.port or 6688, p.username or "", p.password or ""
PROXY_HOST, PROXY_PORT, PROXY_USER, PROXY_PASS = _parse_proxy(_proxy_raw)

RECONNECT_INIT  = 5    # 初始重连间隔（秒）
RECONNECT_MAX   = 60   # 最长重连间隔（秒）
PING_INTERVAL   = 20   # WS 心跳间隔（秒）

# ── 日志 ─────────────────────────────────────────────────────────────────────
CACHE_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.WARNING,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)

def _ts():
    return datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

def _write_atomic(path: pathlib.Path, data: dict):
    """原子写入：先写 tmp 再 rename，避免 scanner 读到半截数据"""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False))
    tmp.rename(path)

# ── 代理设置 ─────────────────────────────────────────────────────────────────
def _apply_proxy():
    socks.set_default_proxy(
        socks.SOCKS5, PROXY_HOST, PROXY_PORT,
        username=PROXY_USER, password=PROXY_PASS
    )
    socket.socket = socks.socksocket

_apply_proxy()

# ═══════════════════════════════════════════════════════════════════════════════
# Stream WS（funding-rate + open-interest 实时推送）
# ═══════════════════════════════════════════════════════════════════════════════

_stream_state: dict = {}
_stream_lock = threading.Lock()

def _stream_on_open(ws):
    ws.send(json.dumps({"type": "auth", "key": API_KEY}))

def _stream_on_message(ws, message):
    try:
        d = json.loads(message)
    except Exception:
        return
    t = d.get("type", "")
    ch = d.get("channel", "")

    if t == "auth_ok":
        ws.send(json.dumps({"type": "subscribe", "channels": ["funding-rate", "open-interest"]}))
        print(f"[stream {_ts()}] auth OK, subscribing...", flush=True)
    elif t == "subscribed":
        print(f"[stream {_ts()}] subscribed: {d.get('channels')}", flush=True)
    elif ch == "funding-rate":
        data = d.get("data", {})
        with _stream_lock:
            sym = data.get("symbol", "")
            if sym:
                _stream_state.setdefault("funding_rate", {})[sym] = {
                    "rate": data.get("rate"),
                    "ts":   data.get("ts") or int(time.time() * 1000),
                }
        _flush_stream_cache()
    elif ch == "open-interest":
        data = d.get("data", {})
        with _stream_lock:
            sym = data.get("symbol", "")
            if sym:
                _stream_state.setdefault("open_interest", {})[sym] = {
                    "oi":  data.get("openInterest"),
                    "ts":  data.get("ts") or int(time.time() * 1000),
                }
        _flush_stream_cache()

def _flush_stream_cache():
    with _stream_lock:
        out = dict(_stream_state)
    out["_updated"] = _ts()
    try:
        _write_atomic(CACHE_DIR / "stream_latest.json", out)
    except Exception as e:
        logging.warning(f"stream flush error: {e}")

def _stream_on_error(ws, error):
    logging.warning(f"[stream] error: {error}")

def _stream_on_close(ws, code, msg):
    print(f"[stream {_ts()}] closed code={code}", flush=True)

# ═══════════════════════════════════════════════════════════════════════════════
# KOL WS（signals + long-short-rank + whale-position-events）
# ═══════════════════════════════════════════════════════════════════════════════

_kol_state: dict = {
    "signals":        {},  # symbol → {long_count, short_count, net_bias, ...}
    "long_short_rank": [],
    "whale_events":   [],  # 最近20条巨鲸事件
}
_kol_lock = threading.Lock()

def _kol_on_open(ws):
    ws.send(json.dumps({"type": "auth", "key": API_KEY}))

def _kol_on_message(ws, message):
    try:
        d = json.loads(message)
    except Exception:
        return
    t  = d.get("type", "")
    ch = d.get("channel", "")

    if t == "auth_ok":
        ws.send(json.dumps({
            "type": "subscribe",
            "channels": ["signals", "long-short-rank", "whale-position-events"]
        }))
        print(f"[kol {_ts()}] auth OK, subscribing...", flush=True)
    elif t == "subscribed":
        print(f"[kol {_ts()}] subscribed: {d.get('channels')}", flush=True)
    elif ch == "signals":
        rows = d.get("data", [])
        with _kol_lock:
            for row in rows:
                sym = row.get("symbol", "")
                if sym:
                    _kol_state["signals"][sym] = {
                        "exchange":    row.get("exchange"),
                        "long_count":  row.get("long_count", 0),
                        "short_count": row.get("short_count", 0),
                        "net_bias":    row.get("net_bias", "neutral"),
                        "bias_score":  row.get("bias_score", 0),
                        "total_pnl":   row.get("total_pnl", 0),
                        "updated_at":  row.get("updated_at", ""),
                    }
        _flush_kol_cache()
    elif ch == "long-short-rank":
        rows = d.get("data", [])
        with _kol_lock:
            _kol_state["long_short_rank"] = rows
        _flush_kol_cache()
    elif ch == "whale-position-events":
        event = d.get("data", {})
        with _kol_lock:
            _kol_state["whale_events"].insert(0, {
                **event, "_recv_at": _ts()
            })
            _kol_state["whale_events"] = _kol_state["whale_events"][:50]  # 保留最近50条
        _flush_kol_cache()
        # 打印大额事件（>100万U）
        val = float(event.get("value", 0) or 0)
        if val >= 1_000_000:
            print(
                f"[kol {_ts()}] 🐳 WHALE EVENT "
                f"{event.get('symbol','')} {event.get('direction','')} "
                f"${val/1e6:.1f}M {event.get('action','')}",
                flush=True
            )

def _flush_kol_cache():
    with _kol_lock:
        out = {k: v for k, v in _kol_state.items()}
    out["_updated"] = _ts()
    try:
        _write_atomic(CACHE_DIR / "kol_latest.json", out)
    except Exception as e:
        logging.warning(f"kol flush error: {e}")

def _kol_on_error(ws, error):
    logging.warning(f"[kol] error: {error}")

def _kol_on_close(ws, code, msg):
    print(f"[kol {_ts()}] closed code={code}", flush=True)

# ═══════════════════════════════════════════════════════════════════════════════
# 守护线程（带指数退避重连）
# ═══════════════════════════════════════════════════════════════════════════════

def _run_ws_forever(ws_type: str, url: str, on_open, on_message, on_error, on_close):
    delay = RECONNECT_INIT
    while True:
        try:
            ws = websocket.WebSocketApp(
                url,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )
            ws.run_forever(ping_interval=PING_INTERVAL, ping_timeout=10)
        except Exception as e:
            logging.warning(f"[{ws_type}] run_forever exception: {e}")
        print(f"[{ws_type} {_ts()}] 重连中，{delay}s 后...", flush=True)
        time.sleep(delay)
        delay = min(delay * 2, RECONNECT_MAX)


def main():
    print(f"[coindata_ws {_ts()}] 启动 WebSocket 守护进程...", flush=True)

    t_stream = threading.Thread(
        target=_run_ws_forever,
        args=("stream", f"{WS_BASE}/stream",
              _stream_on_open, _stream_on_message,
              _stream_on_error, _stream_on_close),
        daemon=True,
        name="ws-stream",
    )
    t_kol = threading.Thread(
        target=_run_ws_forever,
        args=("kol", f"{WS_BASE}/kol",
              _kol_on_open, _kol_on_message,
              _kol_on_error, _kol_on_close),
        daemon=True,
        name="ws-kol",
    )

    t_stream.start()
    time.sleep(1)   # 错开启动，避免同时抢代理连接
    t_kol.start()

    # 主线程等待 SIGTERM / SIGINT
    def _shutdown(sig, frame):
        print(f"\n[coindata_ws {_ts()}] 收到信号 {sig}，退出", flush=True)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    print(f"[coindata_ws {_ts()}] 两路 WS 已启动，按 Ctrl-C 停止", flush=True)
    while True:
        time.sleep(30)
        # 定期打印心跳
        s_syms = len(_stream_state.get("funding_rate", {}))
        k_syms = len(_kol_state.get("signals", {}))
        print(f"[coindata_ws {_ts()}] 心跳 stream={s_syms}个FR kol_signals={k_syms}个", flush=True)


if __name__ == "__main__":
    main()
