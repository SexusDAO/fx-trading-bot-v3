"""
pre_order_gate.py — 开仓前置检查 (P1 + v2.0 新增)

7 项原有检查 + 2 项新增：
  检查八（新）：市场状态 HALT 拦截（BREAKOUT_PENDING / HIGH_VOLATILITY）
  检查九（新）：单笔风险敞口 ≤ 账户余额 × 1%

所有检查同步执行，任一失败返回 {"ok": False, "reason": "..."}。
调用方收到 ok=False 后：静默跳过（写日志），不挂单。
"""

import json, os
from pathlib import Path
from datetime import datetime, timezone

WORKSPACE = Path(__file__).parent.parent


def _load_json(path: Path) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def _load_pos_state() -> dict:
    return _load_json(WORKSPACE / "config" / "pos_state.json")


def _load_circuit() -> dict:
    return _load_json(WORKSPACE / "config" / "circuit_breaker.json")


def _load_blacklist() -> list:
    data = _load_json(WORKSPACE / "config" / "blacklist.json")
    return data.get("symbols", [])


def _load_strategy() -> dict:
    return _load_json(WORKSPACE / "config" / "strategy-config.json")


def _load_market_regime() -> str:
    """返回当前市场模式：bull / bear / range"""
    data = _load_json(WORKSPACE / "config" / "market_regime.json")
    return data.get("mode", "range")


def _count_positions(pos_state: dict) -> tuple[int, int]:
    """返回 (long_count, short_count)"""
    longs  = sum(1 for k in pos_state if k.endswith("_LONG"))
    shorts = sum(1 for k in pos_state if k.endswith("_SHORT"))
    return longs, shorts


# ── 公开接口 ──────────────────────────────────────────────────────────────────

def pre_order_gate(symbol: str,
                   direction: str,
                   score: int,
                   funding: float,
                   notional: float,
                   smart_money_direction: str = "neutral",   # "buy"|"sell"|"neutral"
                   equity: float = 0.0,
                   sl_distance_pct: float = 0.0,             # 新增：止损距离 % (如 0.02 = 2%)
                   market_state: str = "") -> dict:           # 新增：市场状态（空字符串=不检查）
    """
    9 项前置检查（7原有 + 2新增）。
    Returns:
        {"ok": True}
        {"ok": False, "reason": "...", "check": N}
    """
    cfg       = _load_strategy()
    pos       = _load_pos_state()
    circuit   = _load_circuit()
    blacklist = _load_blacklist()
    regime    = _load_market_regime()

    limits = cfg.get("position", {}).get("market_mode_limits", {})
    mode_limits = limits.get(regime, {"max_long": 2, "max_short": 2, "max_total": 4})
    max_total = mode_limits.get("max_total", 5)
    max_long  = mode_limits.get("max_long", 2)
    max_short = mode_limits.get("max_short", 2)

    long_cnt, short_cnt = _count_positions(pos)
    total_cnt = long_cnt + short_cnt

    # ── 检查一：持仓上限 ──────────────────────────────────────────────────────
    if total_cnt >= max_total:
        return {"ok": False, "reason": f"持仓已满({total_cnt}/{max_total})", "check": 1}
    if direction == "LONG" and long_cnt >= max_long:
        return {"ok": False, "reason": f"多单已满({long_cnt}/{max_long})", "check": 1}
    if direction == "SHORT" and short_cnt >= max_short:
        return {"ok": False, "reason": f"空单已满({short_cnt}/{max_short})", "check": 1}

    # ── 检查二：熔断状态 ──────────────────────────────────────────────────────
    cb_open = circuit.get("open", False)
    if cb_open:
        return {"ok": False, "reason": "熔断已触发，暂停开仓", "check": 2}

    # ── 检查三：黑名单 ────────────────────────────────────────────────────────
    if symbol in blacklist:
        return {"ok": False, "reason": f"{symbol} 在黑名单中", "check": 3}

    # ── 检查四：同币种持仓/挂单 ──────────────────────────────────────────────
    key = f"{symbol}_{direction}"
    if key in pos:
        return {"ok": False, "reason": f"{symbol} 已有 {direction} 持仓", "check": 4}

    # ── 检查五：总名义仓位上限 ───────────────────────────────────────────────
    if equity > 0:
        current_notional = sum(
            p.get("notional", 0) for p in pos.values()
        )
        notional_cap = equity * 2.5
        if current_notional + notional >= notional_cap:
            return {"ok": False, "reason": f"名义仓位超限({current_notional+notional:.0f}>{notional_cap:.0f})", "check": 5}

    # ── 检查六：聪明钱方向 ───────────────────────────────────────────────────
    if smart_money_direction == "buy" and direction == "SHORT":
        return {"ok": False, "reason": "聪明钱明确做多，禁止做空", "check": 6}
    if smart_money_direction == "sell" and direction == "LONG":
        return {"ok": False, "reason": "聪明钱明确出货，禁止做多", "check": 6}

    # ── 检查七：资金费率 ─────────────────────────────────────────────────────
    if direction == "LONG" and funding > 0.005:
        return {"ok": False, "reason": f"多单资金费率过高({funding:.4f}>0.005)", "check": 7}
    if direction == "SHORT" and funding < -0.003:
        return {"ok": False, "reason": f"空单资金费率过低({funding:.4f}<-0.003)", "check": 7}

    # ── 检查八（新）：市场状态 HALT 拦截 ──────────────────────────────────────
    if market_state in ("BREAKOUT_PENDING", "HIGH_VOLATILITY"):
        return {
            "ok": False,
            "reason": f"市场状态 {market_state}，暂停开新仓",
            "check": 8,
        }

    # ── 检查九（新）：单笔风险敞口 ≤ 账户1% ─────────────────────────────────
    # sl_distance_pct > 0 才检查；equity > 0 才检查
    if sl_distance_pct > 0 and equity > 0:
        risk_u = notional * sl_distance_pct          # 本次最大亏损 U
        max_risk_u = equity * 0.01                   # 账户 1% 上限
        if risk_u > max_risk_u:
            # 不直接拒绝，而是建议缩仓（调用方可选择按比例缩小 notional）
            suggested = max_risk_u / sl_distance_pct
            return {
                "ok": False,
                "reason": (
                    f"单笔风险{risk_u:.1f}U > 账户1%上限{max_risk_u:.1f}U，"
                    f"建议仓位缩至 {suggested:.0f}U"
                ),
                "check": 9,
                "suggested_notional": round(suggested / 50) * 50,   # 取整到50倍数
            }

    return {"ok": True}
