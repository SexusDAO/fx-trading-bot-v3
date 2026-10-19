"""
Notify — 推送通知到 Jarvis。

通知格式：
    🎯 ARC 新币买入
    代币: COOL ($COOL)
    合约: 0xeb64...
    DEX: TOLLY
    买入: $20 USDC
    得分: 72/100
      流动性: $8.5k ✅
      持有人: 45 ✅
      Top10: 38% ✅
      叙事: "cool" 匹配 ✅
    TX: 0xabc123...

    📊 ARC 信号（未买入）
    代币: XXX ($XXX)
    得分: 32/100 — 流动性不足、持有人偏少
"""

import logging
import subprocess
import json
from . import config

logger = logging.getLogger("arc.notify")


def _send_jarvis(message: str):
    """通过 openclaw message 发送 Jarvis 通知"""
    try:
        payload = json.dumps({
            "action": "send",
            "target": config.JARVIS_TARGET,
            "message": message,
            "channel": "jarvis",
        })
        subprocess.run(
            ["openclaw", "message", "--json", payload],
            capture_output=True,
            timeout=10,
        )
        logger.info("[Notify] Jarvis 通知已发送")
    except Exception as e:
        logger.warning(f"[Notify] Jarvis 通知失败: {e}")


def notify_buy(
    token_info: dict,
    score: int,
    details: dict,
    buy_result: dict,
):
    """买入通知 — 已静默（用户通过 Web3 钱包自行查看）"""
    symbol = token_info.get("symbol", "?")
    if buy_result.get("success"):
        logger.info(
            f"[Notify] [静默] 买入成功 {symbol} "
            f"${buy_result.get('usdc_spent', 0):.0f} "
            f"tx={buy_result.get('tx_hash', '')[:18]}..."
        )
    else:
        logger.error(
            f"[Notify] 买入失败 {symbol}: {buy_result.get('error', '未知')}"
        )
    # 不发送 Jarvis 通知


def notify_skip(token_info: dict, score: int, details: dict, reason: str):
    """跳过通知 — 已静默，只记日志"""
    symbol = token_info.get("symbol", "?")
    name   = token_info.get("name", "?")
    addr   = token_info.get("address", "")[:12]

    if "veto" in details:
        veto_reasons = "; ".join(details["veto"])
        logger.info(f"[Notify] 跳过 {name} (${symbol}) {addr}... 得分={score} 原因: {veto_reasons}")
    else:
        logger.info(f"[Notify] 跳过 {name} (${symbol}) {addr}... 得分={score} < {config.MIN_SCORE_TO_BUY}")
    # 不发送 Jarvis 通知
