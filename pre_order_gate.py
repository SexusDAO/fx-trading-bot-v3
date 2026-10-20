#!/usr/bin/env python3
"""
pre_order_gate.py — 开仓前统一检查关卡 (P1)

所有开仓路径（scanner / auto_open / dual_net）在挂单前必须调用此模块。
按顺序检查7项，任意一项不通过即拒绝，并返回拒绝原因。

接口：
    check(sym, direction, score, funding_pct, pos_size_u) -> GateResult

GateResult:
    .ok      : bool
    .reason  : str（拒绝原因，通过时为 ''）
    .detail  : dict（各检查项状态，用于日志）
"""

import sys
import json
import time
from pathlib import Path
from dataclasses import dataclass, field

sys.path.insert(0, str(Path(__file__).parent))


@dataclass
class GateResult:
    ok:     bool
    reason: str = ""
    detail: dict = field(default_factory=dict)

    def __bool__(self):
        return self.ok


# ── 常量 ──────────────────────────────────────────────────────────────────────
WORKSPACE    = Path("/root/.openclaw/workspace")
MAX_LONG     = 3     # 多单持仓上限
MAX_SHORT    = 1     # 空单持仓上限
MAX_TOTAL    = 5     # 总持仓上限
CIRCUIT_LIMIT_U = 50.0   # 当日亏损熔断线（U）
NOTIONAL_RATIO  = 2.5    # 总名义仓位上限 = 余额 × 此系数
# 资金费率门槛（已单位化为百分比）
FUNDING_LONG_MAX  = +0.5   # 做多费率上限（%）
FUNDING_SHORT_MIN = -0.3   # 做空费率下限（%）


def _get_pos_counts() -> dict:
    """获取当前持仓数量。"""
    try:
        import binance_api as api
        positions = api.get_positions()
        longs  = [p for p in positions
                  if abs(float(p.get("positionAmt", 0))) > 0
                  and p.get("positionSide", "LONG") == "LONG"]
        shorts = [p for p in positions
                  if abs(float(p.get("positionAmt", 0))) > 0
                  and p.get("positionSide", "SHORT") == "SHORT"]
        return {
            "long":  len(longs),
            "short": len(shorts),
            "total": len(longs) + len(shorts),
            "syms":  {p["symbol"] for p in (longs + shorts)},
        }
    except Exception:
        return {"long": 0, "short": 0, "total": 0, "syms": set()}


def _is_circuit_open() -> tuple:
    """检查熔断状态，返回 (is_open: bool, daily_loss: float)。"""
    try:
        from risk_manager import load_circuit as _lc
        data = _lc()
        loss = abs(float(data.get("daily_loss", 0)))
        limit = float(data.get("daily_loss_limit", CIRCUIT_LIMIT_U))
        return loss >= limit, loss
    except Exception:
        return False, 0.0


def _in_blacklist(sym: str, direction: str) -> tuple:
    """检查黑名单（冷静期 + 动态黑名单）。"""
    bl_file = WORKSPACE / "daily_blacklist.json"
    try:
        bl = json.loads(bl_file.read_text()) if bl_file.exists() else {}
        entry = bl.get("symbols", {}).get(sym)
        if entry:
            expire_ts = float(entry.get("expire_ts", 0))
            bl_dir    = entry.get("direction", "")
            if time.time() < expire_ts:
                # 方向一致才拦截（多单止损不影响做空）
                if not bl_dir or bl_dir == direction:
                    return True, f"冷静期黑名单（到期：{entry.get('expire_str','')}）"
    except Exception:
        pass

    # 动态7天黑名单（P3实现后写入此文件）
    dyn_file = WORKSPACE / "ws_cache" / "dynamic_blacklist.json"
    try:
        dyn = json.loads(dyn_file.read_text()) if dyn_file.exists() else {}
        entry = dyn.get(sym)
        if entry and time.time() < float(entry.get("expire_ts", 0)):
            return True, f"动态黑名单（连续亏损{entry.get('losses',0)}次）"
    except Exception:
        pass

    return False, ""


def _has_open_order(sym: str) -> bool:
    """检查是否已有实际有效的开仓挂单（含双网 pending_orders，但过滤已取消的）。"""
    try:
        import binance_api as api
        orders = api.get_open_orders(sym) or []
        entry_orders = [o for o in orders if not o.get("reduceOnly")]
        if entry_orders:
            return True
    except Exception:
        pass
    # 双网 pending_orders：只有 A单实际存活才算有挂单
    pend_file = WORKSPACE / "pending_orders.json"
    try:
        import binance_api as api
        pend = json.loads(pend_file.read_text()) if pend_file.exists() else {}
        entry = pend.get(sym)
        if not entry:
            return False
        # 检查 A单是否真实存活（不信任 status 字段，直接查交易所）
        a_id = entry.get("a_order", {}).get("order_id")
        if a_id:
            try:
                real_orders = api.get_open_orders(sym) or []
                real_ids = {int(o.get("orderId", 0)) for o in real_orders}
                if int(a_id) in real_ids:
                    return True
                # A单已不在交易所，顺手清理 pending_orders
                del pend[sym]
                import tempfile, os
                tmp = pend_file.with_suffix(".tmp")
                tmp.write_text(json.dumps(pend, indent=2, ensure_ascii=False))
                tmp.replace(pend_file)
                return False
            except Exception:
                # 查询失败，保守返回 True（宁可误拦截也不重复开仓）
                a_status = entry.get("a_order", {}).get("status", "")
                return a_status not in ("canceled", "cancelled", "CANCELED", "filled", "FILLED")
    except Exception:
        pass
    return False


def _get_total_notional_and_balance() -> tuple:
    """返回 (used_notional: float, avail_balance: float)。"""
    try:
        import binance_api as api
        positions = api.get_positions()
        used = sum(
            abs(float(p["positionAmt"])) * float(
                p.get("markPrice") or
                api.get_mark_price(p["symbol"]).get("markPrice", 0)
            )
            for p in positions if abs(float(p.get("positionAmt", 0))) > 0
        )
        bal_data = api.request("GET", api.BASE_FAPI + "/fapi/v2/balance", {})
        avail = float([b for b in bal_data if b["asset"] == "USDT"][0]["availableBalance"])
        return used, avail
    except Exception:
        return 0.0, 500.0  # 无法获取时保守处理


def _get_token_report_direction(sym: str) -> str:
    """
    获取聪明钱方向（基于 token_report_score）。
    返回 'positive' | 'negative' | 'neutral'
    """
    try:
        from entry_logic import get_token_report_score
        token = sym.replace("USDT", "").replace("PERP", "")
        score, veto, _ = get_token_report_score(sym)
        if veto or score < -10:
            return "negative"
        elif score > 5:
            return "positive"
        return "neutral"
    except Exception:
        return "neutral"


# ── 主接口 ────────────────────────────────────────────────────────────────────

def check(
    sym:         str,
    direction:   str,   # 'LONG' | 'SHORT'
    score:       float,
    funding_pct: float, # 已单位化为百分比，如 0.1 表示 0.1%
    pos_size_u:  float,
    skip_smart_money: bool = False,  # 跳过聪明钱检查（避免重复调用）
) -> GateResult:
    """
    开仓前7项检查。返回 GateResult。

    检查一：持仓上限
    检查二：熔断状态
    检查三：黑名单
    检查四：同币种限制
    检查五：总仓位上限
    检查六：聪明钱方向（一票否决）
    检查七：资金费率
    """
    detail = {}

    # ── 检查一：持仓上限 ──────────────────────────────────────────────────────
    pos = _get_pos_counts()
    detail["pos"] = pos
    if direction == "LONG" and pos["long"] >= MAX_LONG:
        return GateResult(False, f"多单持仓已满 {pos['long']}/{MAX_LONG}", detail)
    if direction == "SHORT" and pos["short"] >= MAX_SHORT:
        return GateResult(False, f"空单持仓已满 {pos['short']}/{MAX_SHORT}", detail)
    if pos["total"] >= MAX_TOTAL:
        return GateResult(False, f"总持仓已满 {pos['total']}/{MAX_TOTAL}", detail)

    # ── 检查二：熔断 ──────────────────────────────────────────────────────────
    circuit_open, daily_loss = _is_circuit_open()
    detail["daily_loss"] = daily_loss
    if circuit_open:
        return GateResult(False, f"熔断中，当日亏损 {daily_loss:.1f}U", detail)

    # ── 检查三：黑名单 ────────────────────────────────────────────────────────
    in_bl, bl_reason = _in_blacklist(sym, direction)
    detail["blacklist"] = bl_reason
    if in_bl:
        return GateResult(False, f"{sym} 在黑名单：{bl_reason}", detail)

    # ── 检查四：同币种限制 ────────────────────────────────────────────────────
    if sym in pos["syms"]:
        return GateResult(False, f"{sym} 已有持仓，禁止重复开仓", detail)
    if _has_open_order(sym):
        return GateResult(False, f"{sym} 已有开仓挂单，跳过", detail)

    # ── 检查五：总仓位上限 ────────────────────────────────────────────────────
    used_notional, avail_balance = _get_total_notional_and_balance()
    max_notional = avail_balance * NOTIONAL_RATIO
    detail["used_notional"]  = round(used_notional, 1)
    detail["avail_balance"]  = round(avail_balance, 1)
    detail["max_notional"]   = round(max_notional, 1)
    if used_notional + pos_size_u > max_notional:
        return GateResult(
            False,
            f"总仓位超限：已用{used_notional:.0f}U + 新{pos_size_u:.0f}U > 上限{max_notional:.0f}U",
            detail
        )

    # ── 检查六：聪明钱方向（一票否决）────────────────────────────────────────
    if not skip_smart_money:
        smart = _get_token_report_direction(sym)
        detail["smart_money"] = smart
        if direction == "LONG"  and smart == "negative":
            return GateResult(False, f"{sym} 聪明钱净流出，禁止做多", detail)
        if direction == "SHORT" and smart == "positive":
            return GateResult(False, f"{sym} 聪明钱净流入，禁止做空", detail)

    # ── 检查七：资金费率 ──────────────────────────────────────────────────────
    detail["funding_pct"] = funding_pct
    if direction == "LONG"  and funding_pct > FUNDING_LONG_MAX:
        return GateResult(False, f"资金费率过高 {funding_pct:.3f}% > {FUNDING_LONG_MAX}%，不做多", detail)
    if direction == "SHORT" and funding_pct < FUNDING_SHORT_MIN:
        return GateResult(False, f"资金费率过低 {funding_pct:.3f}% < {FUNDING_SHORT_MIN}%，不做空", detail)

    return GateResult(True, "", detail)


if __name__ == "__main__":
    print("pre_order_gate 加载正常 ✅")
    print("接口: pre_order_gate.check(sym, direction, score, funding_pct, pos_size_u)")
