"""
shared/notifier.py — 推送通知（Jarvis）

设计：动态卡片（一个信号 = 一条消息，后续事件追加行，不新发）
  - card_create(sig_id, header)   → 创建卡片，返回 message_id
  - card_append(sig_id, line)     → 向已有卡片追加一行
  - notify(text)                  → 发一条全局独立消息
  - notify_async(text)            → 异步版 notify
"""
import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("fx.notifier")

# ── Jarvis 推送目标（userId:thread:threadId）──────────────────────────────────
_JARVIS_TARGET = "432401404:thread:01a14004-35fe-7932-ae7e-fd9ae23f767b"

# ── 卡片状态持久化路径（进程重启后能续接同一张卡片）────────────────────────────
_CARD_STATE_PATH = Path(__file__).parent.parent / "logs" / "card_state.json"

# ── 内存缓存：sig_id → {"message_id": str, "text": str} ─────────────────────
_card_cache: dict = {}
_card_lock = threading.Lock()


# =============================================================================
# 底层：发送 / 编辑 消息
# =============================================================================

def _send_via_socket(payload: dict) -> Optional[str]:
    """
    通过 IPC Socket 发送请求到 OpenClaw gateway。
    成功返回 message_id（字符串），失败返回 None。
    """
    try:
        import socket as _sock
        sock_path = os.path.expanduser("~/.openclaw/gateway.sock")
        if not os.path.exists(sock_path):
            return None
        raw = json.dumps(payload).encode() + b"\n"
        with _sock.socket(_sock.AF_UNIX, _sock.SOCK_STREAM) as s:
            s.settimeout(5)
            s.connect(sock_path)
            s.sendall(raw)
            # 读取响应（最多 4KB）
            resp_raw = b""
            try:
                s.settimeout(3)
                while True:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    resp_raw += chunk
            except Exception:
                pass
        if resp_raw:
            try:
                resp = json.loads(resp_raw.decode())
                return resp.get("message_id") or resp.get("id") or resp.get("messageId")
            except Exception:
                pass
        return "ok"   # socket 成功但无 message_id 时也标记为成功
    except Exception:
        return None


def _send_via_cli(text: str) -> Optional[str]:
    """用 subprocess 调 openclaw CLI，返回 stdout 中的 message_id（如有）。"""
    try:
        r = subprocess.run(
            ["openclaw", "message", "send",
             "--channel", "jarvis",
             "--target", _JARVIS_TARGET,
             "--message", text],
            timeout=30, capture_output=True, text=True
        )
        # 尝试从 stdout 解析 message_id
        out = r.stdout.strip()
        if out:
            try:
                d = json.loads(out)
                return d.get("message_id") or d.get("id") or d.get("messageId")
            except Exception:
                pass
        return "ok"  # CLI 成功但无结构化输出
    except Exception:
        return None


def _edit_via_socket(message_id: str, new_text: str) -> bool:
    """通过 IPC Socket 编辑已有消息。"""
    try:
        import socket as _sock
        sock_path = os.path.expanduser("~/.openclaw/gateway.sock")
        if not os.path.exists(sock_path):
            return False
        payload = {
            "action":     "edit",
            "channel":    "jarvis",
            "target":     _JARVIS_TARGET,
            "message_id": message_id,
            "message":    new_text,
        }
        raw = json.dumps(payload).encode() + b"\n"
        with _sock.socket(_sock.AF_UNIX, _sock.SOCK_STREAM) as s:
            s.settimeout(5)
            s.connect(sock_path)
            s.sendall(raw)
            # 消费响应（不解析）
            try:
                s.settimeout(2)
                s.recv(4096)
            except Exception:
                pass
        return True
    except Exception:
        return False


def _edit_via_cli(message_id: str, new_text: str) -> bool:
    """用 subprocess 调 CLI 编辑消息。"""
    try:
        r = subprocess.run(
            ["openclaw", "message", "edit",
             "--channel", "jarvis",
             "--target", _JARVIS_TARGET,
             "--message-id", message_id,
             "--message", new_text],
            timeout=30, capture_output=True
        )
        return r.returncode == 0
    except Exception:
        return False


# =============================================================================
# 卡片持久化（进程重启续接）
# =============================================================================

def _load_card_state() -> dict:
    try:
        if _CARD_STATE_PATH.exists():
            data = json.loads(_CARD_STATE_PATH.read_text(encoding="utf-8"))
            # 清理超过 24h 的旧卡片
            cutoff = time.time() - 86400
            return {k: v for k, v in data.items() if v.get("ts", 0) > cutoff}
    except Exception:
        pass
    return {}


def _save_card_state(state: dict) -> None:
    try:
        _CARD_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CARD_STATE_PATH.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def _init_card_cache() -> None:
    """进程启动时从磁盘加载卡片状态到内存。"""
    global _card_cache
    with _card_lock:
        _card_cache = _load_card_state()


# 启动时初始化
_init_card_cache()


# =============================================================================
# 公共 API
# =============================================================================

def notify(text: str) -> None:
    """发一条全局独立消息（不属于任何卡片）。失败则只记日志。"""
    try:
        mid = _send_via_socket({"action": "send", "channel": "jarvis",
                                 "target": _JARVIS_TARGET, "message": text})
        if mid:
            return
        _send_via_cli(text)
    except Exception as e:
        logger.info(f"[notify] 推送失败: {e} | 内容: {text[:80]}")


async def notify_async(text: str) -> None:
    """异步版 notify，在协程中 await 使用，不阻塞事件循环。"""
    import asyncio
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, lambda: notify(text))


# ─────────────────────────────────────────────────────────────────────────────
# 卡片接口
# ─────────────────────────────────────────────────────────────────────────────

def card_create(sig_id: str, header: str) -> Optional[str]:
    """
    创建一张新信号卡片，发出第一条消息。
    header = 标题行，如 "📋 BTCUSDT · 做多 · 回踩 · 强信号"
    返回 message_id（后续 card_append 用），失败返回 None。
    """
    text = header
    mid = _send_via_socket({"action": "send", "channel": "jarvis",
                             "target": _JARVIS_TARGET, "message": text})
    if not mid:
        mid = _send_via_cli(text)
    if not mid:
        logger.warning(f"[card] 创建卡片失败: sig_id={sig_id}")
        return None

    with _card_lock:
        _card_cache[sig_id] = {"message_id": mid, "text": text, "ts": time.time()}
        _save_card_state(dict(_card_cache))

    logger.debug(f"[card] 创建卡片 sig_id={sig_id} mid={mid}")
    return mid


def card_append(sig_id: str, line: str) -> bool:
    """
    向已有卡片追加一行。通过编辑原消息实现（text += \\n + line）。
    如果卡片不存在（进程重启丢失），降级为发一条新消息。
    返回是否成功。
    """
    with _card_lock:
        entry = _card_cache.get(sig_id)

    if entry is None:
        # 卡片不存在，降级为独立消息（追加内容前缀标注）
        logger.debug(f"[card] sig_id={sig_id} 无卡片，降级独立推送")
        notify(line)
        return False

    message_id = entry["message_id"]
    new_text = entry["text"] + "\n" + line

    # 尝试编辑
    ok = _edit_via_socket(message_id, new_text)
    if not ok:
        ok = _edit_via_cli(message_id, new_text)

    if ok:
        with _card_lock:
            if sig_id in _card_cache:
                _card_cache[sig_id]["text"] = new_text
                _card_cache[sig_id]["ts"] = time.time()
                _save_card_state(dict(_card_cache))
        logger.debug(f"[card] 追加成功 sig_id={sig_id} mid={message_id}")
        return True
    else:
        # 编辑失败（可能消息太旧或 API 不支持），降级为新消息
        logger.warning(f"[card] 编辑失败 sig_id={sig_id} mid={message_id}，降级独立推送")
        notify(line)
        return False


async def card_create_async(sig_id: str, header: str) -> Optional[str]:
    """异步版 card_create。"""
    import asyncio
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: card_create(sig_id, header))


async def card_append_async(sig_id: str, line: str) -> bool:
    """异步版 card_append。"""
    import asyncio
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: card_append(sig_id, line))


def card_remove(sig_id: str) -> None:
    """从缓存中移除卡片（信号结束后可选清理，减少内存占用）。"""
    with _card_lock:
        _card_cache.pop(sig_id, None)
        _save_card_state(dict(_card_cache))


# =============================================================================
# 格式化工具（供各模块使用）
# =============================================================================

def _hhmm() -> str:
    """当前 UTC 时间 HH:MM。"""
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M")


_DIR_CN = {"LONG": "做多", "SHORT": "做空"}
_MODE_CN = {
    "pullback":    "回踩",
    "breakout":    "突破",
    "reversal":    "反转",
    "oi_divergence": "OI背离",
    "ema_trend":   "EMA趋势",
    "stop_hunt":   "猎杀止损",
    "funding_arb": "资金费套利",
}
_STRENGTH_CN = {"strong": "强信号", "medium": "中等信号", "weak": "弱信号"}
_CLOSE_REASON_CN = {
    "tp2":           "TP2止盈",
    "sl":            "止损",
    "sl_triggered":  "止损",
    "manual_close":  "手动平仓",
    "time_stop":     "时间止损",
    "time_stop_no_progress": "时间止损",
    "time_stop_loss": "时间止损",
    "time_stop_hard": "时间止损",
    "time_stop_hard_72h": "时间止损",
    "trailing_stop": "移动止损锁利",
    "pnl_lock":      "浮盈保护平仓",
    "early_stagnation": "时间止损",
    "pnl_protection": "浮盈保护平仓",
    "tp1_protect_exit": "浮盈保护平仓",
    "partial_time_stop": "时间止损",
    "breakout_fail": "突破失败",
    "exit_signal_reversal": "反向信号平仓",
}


def make_card_header(symbol: str, direction: str, mode: str, signal_strength: str) -> str:
    dir_cn  = _DIR_CN.get(direction.upper(), direction)
    mode_cn = _MODE_CN.get(mode, mode)
    str_cn  = _STRENGTH_CN.get(signal_strength, signal_strength)
    return f"📋 {symbol} · {dir_cn} · {mode_cn} · {str_cn}"


def fmt_hold_time(hold_minutes: float) -> str:
    h = int(hold_minutes // 60)
    m = int(hold_minutes % 60)
    if h > 0:
        return f"{h}h {m}min"
    return f"{m}min"
