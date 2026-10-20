"""
guards/circuit_daily_reset.py — 熔断器每日重置 (UTC 00:00 cron 触发)

职责：
  - 重置 circuit_breaker.json 的 open=False
  - 清空 daily_loss / daily_cancel_counter
  - 静默执行，HEARTBEAT_OK
"""

import sys, json, logging
from pathlib import Path
from datetime import datetime, timezone

WORKSPACE = Path(__file__).parent.parent
sys.path.insert(0, str(WORKSPACE))

CIRCUIT_FILE = WORKSPACE / "config" / "circuit_breaker.json"
CANCEL_FILE  = WORKSPACE / "config" / "daily_cancel_counter.json"
RISK_FILE    = WORKSPACE / "config" / "daily_risk.json"

LOG_FILE = WORKSPACE / "logs" / "circuit_reset.log"
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(message)s",
                    handlers=[logging.FileHandler(LOG_FILE),
                               logging.StreamHandler()])
logger = logging.getLogger("circuit_reset")


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _save_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    tmp.replace(path)


def run() -> dict:
    """重置熔断器和每日计数器，返回操作摘要。"""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    result = {"reset_at": _now_iso(), "date": today, "actions": []}

    # 重置熔断器
    _save_json(CIRCUIT_FILE, {
        "open":       False,
        "daily_loss": 0.0,
        "reset_at":   _now_iso(),
    })
    result["actions"].append("circuit_breaker reset")
    logger.info(f"[circuit_reset] 熔断器重置 open=False")

    # 重置每日撤单计数
    _save_json(CANCEL_FILE, {"date": today, "count": 0})
    result["actions"].append("cancel_counter reset")

    # 重置每日风控
    _save_json(RISK_FILE, {"date": today, "daily_loss": 0.0, "daily_pnl": 0.0})
    result["actions"].append("daily_risk reset")

    logger.info(f"[circuit_reset] 每日重置完成: {result['actions']}")
    return result


if __name__ == "__main__":
    r = run()
    print("HEARTBEAT_OK")
