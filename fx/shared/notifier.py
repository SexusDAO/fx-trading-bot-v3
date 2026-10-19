"""
shared/notifier.py — 推送通知（Jarvis）
"""
import logging
import os
import threading

logger = logging.getLogger("fx.notifier")

# Jarvis 推送目标（userId:thread:threadId）
_JARVIS_TARGET = "432401404:thread:01a14004-35fe-7932-ae7e-fd9ae23f767b"

# 异步内部：用 requests 直接调 openclaw 内部 API
def _send_via_socket(text: str) -> bool:
    """Try openclaw internal messaging via IPC socket first."""
    try:
        import socket, json
        sock_path = os.path.expanduser("~/.openclaw/gateway.sock")
        if not os.path.exists(sock_path):
            return False
        payload = json.dumps({
            "action": "send",
            "channel": "jarvis",
            "target": _JARVIS_TARGET,
            "message": text,
        })
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(5)
            s.connect(sock_path)
            s.sendall(payload.encode() + b"\n")
        return True
    except Exception:
        return False


def notify(text: str) -> None:
    """\u53d1\u9001\u901a\u77e5\u5230 Jarvis\uff0c\u5931\u8d25\u5219\u53ea\u8bb0\u65e5\u5fd7"""
    try:
        # 方\u6cd5\u4e00\uff1a\u5c1d\u8bd5 socket IPC
        if _send_via_socket(text):
            return
        # \u65b9\u6cd5\u4e8c\uff1a subprocess\uff08\u8d85\u65f6\u653e\u5bbd\u5230 30s\uff09
        import subprocess
        subprocess.run(
            ["openclaw", "message", "send",
             "--channel", "jarvis",
             "--target", _JARVIS_TARGET,
             "--message", text],
            timeout=30, capture_output=True
        )
    except Exception as e:
        logger.info(f"[notify] {text[:100]}... (\u63a8\u9001\u5931\u8d25: {e})")


async def notify_async(text: str) -> None:
    """\u5f02\u6b65\u7248\u672c\u7684 notify\uff0c\u53ef\u5728 async \u51fd\u6570\u4e2d await\uff0c\u5e95\u5c42\u4e0d\u963b\u585e\u4e8b\u4ef6\u5faa\u73af\u3002"""
    import asyncio
    loop = asyncio.get_event_loop()
    # fire-and-forget \u5728\u72ec\u7acb\u7ebf\u7a0b\u8fd0\u884c\uff0c\u907f\u514d\u963b\u585e\u4e3b\u5faa\u73af
    await loop.run_in_executor(None, lambda: notify(text))

