#!/usr/bin/env python3
"""
pre_open_guard.py — 开仓前安全守卫
在 position_manager 真正下单前调用此模块，执行五项检查：
  1. 同标的反向仓位检查（实时拉取，不用缓存）
  2. active_symbols 专属监控锁定检查
  3. 同标的是否已有未成交开仓挂单
  4. 单笔名义金额硬上限检查
  5. 熔断状态检查

用法：
  from pre_open_guard import check_before_open
  ok, reason = check_before_open(sym, direction="LONG", notional=300)
  if not ok:
      reports.append(f"🚫 {sym} 开仓被拦截: {reason}")
      continue
"""
import sys, json, os
sys.path.insert(0, "/root/.openclaw/workspace")
import binance_api as api

ACTIVE_SYMBOL_FILE  = "/root/.openclaw/workspace/ws_cache/active_symbols.json"
CIRCUIT_FILE        = "/root/.openclaw/workspace/circuit_breaker.json"
STRATEGY_CONFIG     = "/root/.openclaw/workspace/strategy-config.json"
MAX_NOTIONAL        = 1000  # 单笔硬上限 U（动态由评分决定300~1000U）


def check_before_open(sym: str, direction: str = "LONG", notional: float = 0) -> tuple:
    """
    返回 (True, "") 表示可以开仓
    返回 (False, reason) 表示被拦截，附带原因
    """
    # ── 1. 熔断检查（动态熔断线：本金×20%）──
    try:
        import sys as _sys_pg; _sys_pg.path.insert(0, "/root/.openclaw/workspace/scripts")
        from circuit_limit import get_circuit_status
        cb = get_circuit_status()
        if cb.get("circuit_open"):
            return False, (f"熔断已触发，今日亏损={cb.get('daily_loss',0):.1f}U"
                           f" ≥ 动态限额={cb.get('limit',0):.1f}U（本金×20%）")
    except Exception:
        try:
            with open(CIRCUIT_FILE) as f:
                cb = json.load(f)
            if cb.get("circuit_open"):
                return False, f"熔断已触发，今日亏损={cb.get('daily_loss',0):.1f}U"
        except Exception:
            pass

    # ── 2. active_symbols 专属监控锁 ──
    try:
        with open(ACTIVE_SYMBOL_FILE) as f:
            active_syms = json.load(f)
        if sym in active_syms:
            role = active_syms[sym].get("role", "专属监控")
            return False, f"被 {role} 专属监控锁定，禁止新开仓"
    except Exception:
        pass

    # ── 3. 实时反向仓位检查 ──
    try:
        positions = api.get_positions()
        opposite_side = "SHORT" if direction == "LONG" else "LONG"
        reverse = [p for p in positions
                   if p.get("symbol") == sym
                   and p.get("positionSide") == opposite_side
                   and abs(float(p.get("positionAmt", 0))) > 0]
        if reverse:
            return False, f"已有 {opposite_side} 仓位，禁止开 {direction}，防止双向持仓"

        # 同方向仓位也检查（已持有就不重复开）
        same = [p for p in positions
                if p.get("symbol") == sym
                and p.get("positionSide") == direction
                and abs(float(p.get("positionAmt", 0))) > 0]
        if same:
            return False, f"已有 {direction} 仓位（持仓中），跳过重复开仓"
    except Exception as e:
        return False, f"实时仓位查询失败: {e}"

    # ── 4. 同标的已有开仓挂单检查 ──
    try:
        open_orders = api.request("GET", api.BASE_FAPI + "/fapi/v1/openOrders", {})
        if isinstance(open_orders, list):
            dup = [o for o in open_orders
                   if o.get("symbol") == sym
                   and o.get("positionSide") == direction
                   and o.get("side") == ("BUY" if direction == "LONG" else "SELL")
                   and not o.get("reduceOnly")]
            if dup:
                return False, f"已有 {direction} 开仓挂单 orderId={dup[0]['orderId']}，不重复挂"
    except Exception:
        pass

    # ── 5. 名义金额硬上限（含涨跌幅榜前五 500U 规则）──
    try:
        with open(STRATEGY_CONFIG) as f:
            cfg = json.load(f)
        hard_cap = cfg.get("notional_limits", {}).get("hard_cap_per_position", MAX_NOTIONAL)
        top5_cap = cfg.get("notional_limits", {}).get("gainers_losers_top5_max", 500)
    except Exception:
        hard_cap = MAX_NOTIONAL
        top5_cap = 500

    # 检查是否在涨跌幅榜前五
    try:
        import binance_api as _bapi2
        _tickers = _bapi2.request("GET", _bapi2.BASE_FAPI + "/fapi/v1/ticker/24hr", {})
        if isinstance(_tickers, list):
            _sorted = sorted(_tickers, key=lambda x: abs(float(x.get("priceChangePercent",0))), reverse=True)
            _top5 = {t["symbol"] for t in _sorted[:5]}
            if sym in _top5:
                hard_cap = min(hard_cap, top5_cap)
    except Exception:
        pass

    if notional > hard_cap:
        return False, f"名义金额 {notional:.0f}U 超过上限 {hard_cap}U"

    return True, ""


if __name__ == "__main__":
    # 测试
    ok, reason = check_before_open("ESPORTSUSDT", "LONG", 300)
    print(f"ESPORTSUSDT LONG: ok={ok} reason={reason}")
    ok, reason = check_before_open("ONUSDT", "LONG", 300)
    print(f"ONUSDT LONG: ok={ok} reason={reason}")
