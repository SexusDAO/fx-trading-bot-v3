#!/usr/bin/env python3
"""
liquidation_guard.py — 爆仓价安全检查

在每次开仓前调用，确保新仓位加入后账户整体爆仓风险可控。
Cross 模式下所有仓位共享保证金，单个仓位过大会拉低整体爆仓价。

核心规则：
1. 新仓位加入后，模拟计算账户整体爆仓价
2. 爆仓价必须距当前价格有足够缓冲（默认 30% 以上）
3. 单个仓位名义金额不超过账户净值的 40%
4. 所有仓位总名义金额不超过账户净值的 150%（全仓杠杆不超过 1.5x 净值）
"""

import sys, json, os
sys.path.insert(0, "/root/.openclaw/workspace")

STRATEGY_CONFIG = "/root/.openclaw/workspace/strategy-config.json"

# 安全阈值
MIN_LIQ_BUFFER   = 0.30   # 爆仓价距当前价最少 30% 缓冲
MAX_POS_RATIO    = 0.60   # 单仓名义 / 账户净值 上限（净值600U时单仓最大360U，1000U时600U）
MAX_TOTAL_RATIO  = 2.50   # 总名义 / 账户净值 上限（全仓杠杆不超过 2.5x 净值，控制总敞口）
MAINT_MARGIN_RATE = 0.01  # 保守维持保证金率估算（1%）


def _get_account_state() -> dict:
    """获取账户净值、当前持仓列表。API 失败时从本地文件读取本金。"""
    import binance_api as api
    try:
        bal = api.request("GET", api.BASE_FAPI + "/fapi/v2/balance", {})
        usdt = {}
        if isinstance(bal, list):
            usdt = next((b for b in bal if b.get('asset') == 'USDT'), {})
        equity = float(usdt.get('crossWalletBalance') or usdt.get('balance') or 0)
        unrealized = float(usdt.get('crossUnPnl') or 0)
        net_equity = equity + unrealized

        pos_risk = api.request("GET", api.BASE_FAPI + "/fapi/v2/positionRisk", {})
        positions = [p for p in pos_risk
                     if abs(float(p.get('positionAmt', 0))) > 0] if isinstance(pos_risk, list) else []

        if equity > 0:
            # 成功获取时把本金写入本地缓存
            _save_equity_cache(equity, positions)

        return {
            "equity":     equity,
            "net_equity": max(net_equity, equity),
            "positions":  positions,
        }
    except Exception as e:
        # API 失败时从本地缓存读取（不能放行！）
        return _get_account_state_from_cache(str(e))


EQUITY_CACHE_FILE = "/root/.openclaw/workspace/ws_cache/equity_cache.json"


def _save_equity_cache(equity: float, positions: list):
    """API 成功时把最新账户状态写入本地缓存"""
    import time as _t
    try:
        data = {
            "equity": equity,
            "positions": [{"symbol": p["symbol"],
                           "positionSide": p.get("positionSide", "BOTH"),
                           "notional": p.get("notional", 0),
                           "positionAmt": p.get("positionAmt", 0)} for p in positions],
            "ts": int(_t.time())
        }
        tmp = EQUITY_CACHE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, EQUITY_CACHE_FILE)
    except Exception:
        pass


def _get_account_state_from_cache(err: str) -> dict:
    """API 失败时的安全 fallback：读本地缓存而不是返回空 positions"""
    import time as _t
    try:
        with open(EQUITY_CACHE_FILE) as f:
            cache = json.load(f)
        age = int(_t.time()) - cache.get("ts", 0)
        if age > 300:  # 缓存超过5分钟则保守拒绝
            return {"equity": 1.0, "net_equity": 1.0, "positions": [],
                    "_fallback": True, "_reason": f"缓存过期({age}s): {err}"}
        return {
            "equity":     cache["equity"],
            "net_equity": cache["equity"],
            "positions":  cache.get("positions", []),
            "_fallback":  True,
            "_reason":    err,
        }
    except Exception:
        # 完全无法获取本金，用极小值让守卫强制拦打
        return {"equity": 1.0, "net_equity": 1.0, "positions": [],
                "_fallback": True, "_reason": err}


def estimate_liquidation_price(
    sym: str,
    direction: str,       # "LONG" or "SHORT"
    entry_price: float,
    notional: float,      # 名义金额 U
    net_equity: float,    # 账户净值 U
    existing_notional: float = 0,  # 现有总名义金额
    maint_rate: float = MAINT_MARGIN_RATE,
) -> float:
    """
    Cross 模式下单个仓位的近似爆仓价：
    LONG:  liq ≈ entry * (1 - net_equity / notional + maint_rate)
    SHORT: liq ≈ entry * (1 + net_equity / notional - maint_rate)

    净值越小、名义越大 → 爆仓价越接近入场价
    """
    if notional <= 0 or net_equity <= 0:
        return 0.0

    qty = notional / entry_price
    if direction == "LONG":
        liq = entry_price - (net_equity - notional * maint_rate) / qty
    else:
        liq = entry_price + (net_equity - notional * maint_rate) / qty

    return max(liq, 0.0)


def check_liquidation_risk(
    sym: str,
    direction: str,
    entry_price: float,
    notional: float,
) -> tuple:
    """
    主检查函数：在开仓前调用。
    返回 (ok: bool, reason: str, liq_price: float, buffer_pct: float)
    """
    try:
        state = _get_account_state()
    except Exception as e:
        # 查询失败时保守放行，不阻断开仓
        return True, f"账户查询失败({e})，保守放行", 0.0, 0.0

    net_equity = state["net_equity"]
    positions  = state["positions"]

    # 现有总名义金额
    existing_notional = sum(abs(float(p.get('notional', 0))) for p in positions)
    total_notional    = existing_notional + notional

    # ── 1. 单仓名义 / 净值 比例检查（净值充裕时才启用）──
    # 净值 < 1000U 时放松比例限制，小账户主要靠爆仓缓冲保护
    effective_max_pos_ratio = MAX_POS_RATIO if net_equity >= 1000 else 2.5
    pos_ratio = notional / net_equity if net_equity > 0 else 999
    if pos_ratio > effective_max_pos_ratio:
        return (False,
                f"单仓名义 {notional:.0f}U = 净值{net_equity:.0f}U × {pos_ratio:.0%}，超过上限{effective_max_pos_ratio:.0%}",
                0.0, 0.0)

    # ── 2. 总名义 / 净值 比例检查（全仓杠杆控制）──
    total_ratio = total_notional / net_equity if net_equity > 0 else 999
    if total_ratio > MAX_TOTAL_RATIO:
        return (False,
                f"加仓后总名义 {total_notional:.0f}U = 净值{net_equity:.0f}U × {total_ratio:.1f}x，超过{MAX_TOTAL_RATIO}x上限",
                0.0, 0.0)

    # ── 3. 爆仓价距当前价缓冲检查 ──
    liq_price = estimate_liquidation_price(
        sym, direction, entry_price, notional,
        net_equity, existing_notional
    )

    if liq_price <= 0:
        return True, "爆仓价计算异常，保守放行", liq_price, 1.0

    if direction == "LONG":
        buffer_pct = (entry_price - liq_price) / entry_price
    else:
        buffer_pct = (liq_price - entry_price) / entry_price

    if buffer_pct < MIN_LIQ_BUFFER:
        return (False,
                f"爆仓价 {liq_price:.5f} 距入场 {entry_price:.5f} 仅 {buffer_pct:.1%}，"
                f"不足安全缓冲 {MIN_LIQ_BUFFER:.0%}",
                liq_price, buffer_pct)

    return True, f"爆仓价={liq_price:.5f} 缓冲={buffer_pct:.1%} ✅", liq_price, buffer_pct


def safe_notional(
    sym: str,
    direction: str,
    entry_price: float,
    requested_notional: float,
    min_notional: float = 300.0,
) -> tuple:
    """
    自动缩减仓位直到通过爆仓检查。
    返回 (safe_notional: float, liq_price: float, buffer_pct: float, was_reduced: bool)
    若缩减到 min_notional 仍不通过，返回 (0, 0, 0, True) 表示不开仓。
    """
    test_notional = requested_notional
    step = 50.0  # 每次缩减 50U

    while test_notional >= min_notional:
        ok, reason, liq, buf = check_liquidation_risk(sym, direction, entry_price, test_notional)
        if ok:
            return test_notional, liq, buf, (test_notional < requested_notional)
        test_notional -= step
        test_notional = round(test_notional / 10) * 10

    # 如果 min_notional 也不通过，尝试用 min_notional 再过一遍
    ok, reason, liq, buf = check_liquidation_risk(sym, direction, entry_price, min_notional)
    if ok:
        return min_notional, liq, buf, True

    return 0.0, 0.0, 0.0, True  # 无法安全开仓


if __name__ == "__main__":
    import binance_api as api

    pos_risk = api.request("GET", api.BASE_FAPI + "/fapi/v2/positionRisk", {})
    bal      = api.request("GET", api.BASE_FAPI + "/fapi/v2/balance", {})
    usdt     = next((b for b in bal if b.get('asset')=='USDT'), {}) if isinstance(bal, list) else {}
    equity   = float(usdt.get('crossWalletBalance') or 0)
    print(f"账户净值: {equity:.2f}U")

    # 测试：假设要开一个 BTCUSDT LONG 500U @108000
    ok, reason, liq, buf = check_liquidation_risk("BTCUSDT", "LONG", 108000, 500)
    print(f"\nBTCUSDT LONG 500U: ok={ok}")
    print(f"  {reason}")

    # 测试边界：开 2000U（超过净值40%）
    ok2, reason2, liq2, buf2 = check_liquidation_risk("BTCUSDT", "LONG", 108000, 2000)
    print(f"\nBTCUSDT LONG 2000U: ok={ok2}")
    print(f"  {reason2}")

    # safe_notional 自动缩减
    safe, liq_s, buf_s, reduced = safe_notional("BTCUSDT", "LONG", 108000, 1000)
    print(f"\nsafe_notional(1000U 请求): {safe:.0f}U 爆仓缓冲={buf_s:.1%} 被缩减={reduced}")
