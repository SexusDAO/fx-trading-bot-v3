#!/usr/bin/env python3
"""
notifier.py — 统一推送通知封装

分级：
  CRITICAL  紧急，必推（失败时仍尝试 fallback）
  NORMAL    普通，正常推送
  SILENT    静默，只写日志不推送

用法：
  from notifier import send, critical, normal, silent, CRITICAL, NORMAL, SILENT
  send("消息内容")                  # 默认 NORMAL
  send("紧急消息", level=CRITICAL)
  critical("紧急消息")
  normal("普通消息")
  silent("只写日志，不推送")
"""

import os
import subprocess
import sys
from datetime import datetime, timezone

# ─── 通知级别常量 ──────────────────────────────────────────────────────────────
CRITICAL = "CRITICAL"
NORMAL   = "NORMAL"
SILENT   = "SILENT"

# ─── 常量（与 ws_monitor.py 保持一致） ────────────────────────────────────────
WORKSPACE     = "/root/.openclaw/workspace"
LOG_FILE      = os.path.join(WORKSPACE, "ws_monitor.log")
JARVIS_TARGET = "432401404:thread:019f9203-3b29-7d9e-bfb4-b67c0b06587c"

# ─── 尝试从 ws_monitor 复用 log / notify ──────────────────────────────────────
_ws_log    = None
_ws_notify = None

try:
    sys.path.insert(0, WORKSPACE)
    import ws_monitor as _wsm
    _ws_log    = _wsm.log
    _ws_notify = _wsm.notify
except Exception:
    pass  # fallback 实现见下方


# ─── 内部：写日志 ──────────────────────────────────────────────────────────────
def _log(msg: str) -> None:
    """写日志（复用 ws_monitor.log，最多 600 行）。"""
    if _ws_log is not None:
        try:
            _ws_log(msg)
            return
        except Exception:
            pass
    # fallback：直接写文件
    ts   = datetime.now(timezone.utc).strftime("%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}\n"
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line)
        with open(LOG_FILE) as f:
            lines = f.readlines()
        if len(lines) > 600:
            with open(LOG_FILE, "w") as f:
                f.writelines(lines[-500:])
    except Exception:
        pass


# ─── 内部：底层推送 ────────────────────────────────────────────────────────────
def _push(msg: str) -> None:
    """实际推送：优先复用 ws_monitor.notify，失败时 fallback openclaw CLI。"""
    if _ws_notify is not None:
        try:
            _ws_notify(msg)
            return
        except Exception as e:
            _log(f"[notifier_ws_notify_err] {e}")
    # fallback：直接调用 openclaw CLI
    try:
        subprocess.Popen(
            [
                "openclaw", "message", "send",
                "--target", JARVIS_TARGET,
                "--channel", "jarvis",
                "--message", msg,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        _log(f"[notifier_push_err] {e}")


# ─── 公开 API ──────────────────────────────────────────────────────────────────
def send(msg: str, level: str = NORMAL) -> None:
    """
    发送通知。

    level=SILENT  → 只写日志，不推送
    level=NORMAL  → 写日志 + 推送
    level=CRITICAL → 写日志 + 推送（优先级最高）
    """
    ts       = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    tagged   = f"[{level}] [{ts}] {msg}"
    log_line = f"[notifier] {tagged}"

    _log(log_line)

    if level == SILENT:
        return  # 静默：只写日志

    _push(tagged)


def critical(msg: str) -> None:
    """紧急通知，必推。"""
    send(msg, level=CRITICAL)


def normal(msg: str) -> None:
    """普通通知。"""
    send(msg, level=NORMAL)


def silent(msg: str) -> None:
    """静默：只写日志，不推送。"""
    send(msg, level=SILENT)


# ─── Module-level singleton so callers can do: from notifier import notifier ──
class _NotifierSingleton:
    """Thin object so callers can do: from notifier import notifier as _notifier"""
    def send(self, msg: str, level: str = NORMAL) -> None:
        send(msg, level=level)

    def critical(self, msg: str) -> None:
        critical(msg)

    def normal(self, msg: str) -> None:
        normal(msg)

    def silent(self, msg: str) -> None:
        silent(msg)


notifier = _NotifierSingleton()
