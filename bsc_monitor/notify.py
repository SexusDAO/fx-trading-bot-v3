# ============================================================
# notify.py — 钉钉推送（复用 fomo_workflow 的机器人）
# 支持 markdown 卡片模式
# ============================================================
import time
import hmac
import base64
import hashlib
import urllib.parse
import requests

from config import DINGTALK_ACCESS_TOKEN, DINGTALK_SECRET, PROXY

_PROXIES = {"http": PROXY, "https": PROXY}


def _sign_url() -> str:
    ts = str(round(time.time() * 1000))
    string_to_sign = f"{ts}\n{DINGTALK_SECRET}"
    hmac_code = hmac.new(
        DINGTALK_SECRET.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
    return (
        f"https://oapi.dingtalk.com/robot/send?access_token={DINGTALK_ACCESS_TOKEN}"
        f"&timestamp={ts}&sign={sign}"
    )


def _post(body: dict):
    try:
        r = requests.post(_sign_url(), json=body, proxies=_PROXIES, timeout=10)
        if r.status_code != 200 or r.json().get("errcode") != 0:
            print(f"[DINGTALK FAIL] {r.status_code} {r.text[:120]}")
    except Exception as e:
        print(f"[DINGTALK ERR] {e}")


def notify(msg: str):
    """纯文本推送（保留兼容）"""
    print(f"\n🔔 {msg}\n", flush=True)
    if len(msg) > 4000:
        msg = msg[:4000] + "..."
    _post({"msgtype": "text", "text": {"content": msg}})


def notify_md(title: str, md: str):
    """markdown 卡片推送"""
    print(f"\n🔔 {title}\n{md}\n", flush=True)
    if len(md) > 18000:
        md = md[:18000] + "..."
    _post({"msgtype": "markdown", "markdown": {"title": title, "text": md}})


def notify_multi(messages: list):
    """多条纯文本合并为一条推送"""
    if not messages:
        return
    notify("\n\n".join(messages))
