#!/usr/bin/env python3
"""
circuit_limit.py — 动态熔断线计算
每次调用实时查询账户本金，返回当日允许的最大亏损额（本金 × 20%）。
所有涉及熔断的模块统一调用此函数，确保熔断标准和账户余额同步。
"""
import sys, json, os
sys.path.insert(0, "/root/.openclaw/workspace")

CIRCUIT_FILE    = "/root/.openclaw/workspace/circuit_breaker.json"
STRATEGY_CONFIG = "/root/.openclaw/workspace/strategy-config.json"
LOSS_RATIO      = 0.20   # 每日最大亏损比例：本金的 20%
FALLBACK_LIMIT  = 80.0   # API 查询失败时的保底值（U）


def get_daily_loss_limit() -> float:
    """
    实时查询合约账户本金（crossWalletBalance），返回 本金 × 20%。
    失败时返回 FALLBACK_LIMIT。
    """
    try:
        import binance_api as api
        r = api.request("GET", api.BASE_FAPI + "/fapi/v2/balance", {})
        if isinstance(r, list):
            usdt = next((b for b in r if b.get('asset') == 'USDT'), {})
            wallet = float(usdt.get('crossWalletBalance') or usdt.get('balance') or 0)
            if wallet > 0:
                limit = round(wallet * LOSS_RATIO, 2)
                # 顺带把最新本金写入 circuit_breaker.json，供 liquidation_guard 离线使用
                try:
                    import os as _os
                    _cb_path = CIRCUIT_FILE
                    try:
                        with open(_cb_path) as _f: _cb = json.load(_f)
                    except Exception: _cb = {}
                    _cb['last_known_equity'] = wallet
                    _tmp = _cb_path + '.tmp'
                    with open(_tmp,'w') as _f: json.dump(_cb, _f, indent=2)
                    _os.replace(_tmp, _cb_path)
                except Exception: pass
                return max(limit, 10.0)   # 最低不低于 10U（防止本金极小时熔断太灵敏）
    except Exception:
        pass
    return FALLBACK_LIMIT


def get_circuit_status() -> dict:
    """读取当前熔断状态，同时刷新动态熔断线。"""
    import datetime
    today = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    try:
        with open(CIRCUIT_FILE) as f:
            cb = json.load(f)
    except Exception:
        cb = {}

    # 日期切换自动重置
    if cb.get("date") != today:
        cb = {"date": today, "daily_loss": 0.0, "circuit_open": False, "ts": 0}

    # 动态刷新熔断线
    cb["limit"] = get_daily_loss_limit()
    cb["daily_loss_limit"] = cb["limit"]

    # 重新判断熔断状态（本金变化可能导致限额变化）
    if cb.get("daily_loss", 0) >= cb["limit"]:
        cb["circuit_open"] = True

    return cb


def update_circuit_loss(loss_u: float) -> dict:
    """
    累加今日亏损，自动判断是否触发熔断。
    loss_u 传入正数（亏损绝对值）或负数（已是负值时取绝对值）。
    返回更新后的熔断状态。
    """
    import datetime, time
    today = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    cb = get_circuit_status()
    cb["daily_loss"] = cb.get("daily_loss", 0) + abs(loss_u)
    cb["limit"]      = get_daily_loss_limit()
    cb["daily_loss_limit"] = cb["limit"]
    if cb["daily_loss"] >= cb["limit"]:
        cb["circuit_open"] = True
    cb["date"] = today
    cb["ts"]   = int(time.time())
    _save(cb)
    return cb


def _save(cb: dict):
    tmp = CIRCUIT_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cb, f, indent=2, ensure_ascii=False)
    os.replace(tmp, CIRCUIT_FILE)


if __name__ == "__main__":
    limit = get_daily_loss_limit()
    status = get_circuit_status()
    print(f"当前本金动态熔断线: {limit:.2f} U/天（本金×20%）")
    print(f"今日已亏: {status.get('daily_loss',0):.2f} U")
    print(f"熔断状态: {'🔴 已触发' if status.get('circuit_open') else '✅ 正常'}")
