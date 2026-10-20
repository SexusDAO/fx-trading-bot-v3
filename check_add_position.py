#!/usr/bin/env python3
"""
check_add_position.py — 补仓逻辑模块 (P2-c / Q2修订版)

触发条件（全部满足）：
  多单：
    - 价格回踩到入场时支撑位附近（支撑位上方0.5%以内）
    - 该支撑位结构未被破坏（当前价 > 支撑位）
    - 1H RSI 未超卖（>30）
    - 距上次补仓 >30 分钟
  空单：
    - 价格反弹到入场时压力位附近（压力位下方0.5%以内）
    - 该压力位结构未被突破（当前价 < 压力位）
    - 1H RSI 未超买（<70）
    - 距上次补仓 >30 分钟

额外保护：
  - 补仓后总保证金占用 ≤ 账户余额80%

接口：
    check_and_add(sym, notify_fn=None) -> bool
"""

import sys, json, time, math
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import binance_api as api
from pos_state_store import store as _ps_store

WORKSPACE      = Path("/root/.openclaw/workspace")
ADD_STATE_FILE = WORKSPACE / "add_position_counter.json"
MAX_ADD_TIMES  = 2
ADD_COOLDOWN   = 30 * 60     # 30分钟冷静期
SUPPORT_TOL    = 0.005       # 支撑位容差 0.5%
MARGIN_LIMIT   = 0.80        # 补仓后保证金占用上限 80%


def _load_add_state() -> dict:
    try:
        return json.loads(ADD_STATE_FILE.read_text()) if ADD_STATE_FILE.exists() else {}
    except Exception:
        return {}


def _save_add_state(data: dict):
    ADD_STATE_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def _get_rsi1h(sym: str) -> float:
    try:
        from entry_logic import rsi_calc
        k = api.get_klines(sym, "1h", 30)
        return rsi_calc([float(c[4]) for c in k])
    except Exception:
        return 50.0


def _get_support_resistance(sym: str, direction: str, entry_price: float) -> float:
    """
    从 pos_state 读取入场时的支撑/压力位（scanner 挂单时已存入 liq_zone_price）。
    若无记录则用 ATR 动态计算。
    """
    pos = _ps_store.read(sym)
    # 优先用 scanner 挂单时记录的清算区价格
    liq_price = float(pos.get("liq_zone_price", 0))
    if liq_price > 0:
        return liq_price
    # 退而求其次：入场时的 hard_sl 作为支撑
    hard_sl = float(pos.get("hard_sl", 0))
    if hard_sl > 0:
        return hard_sl
    # 最后兜底：ATR计算
    try:
        from entry_logic import atr_calc
        k15m = api.get_klines(sym, "15m", 30)
        atr  = atr_calc(k15m)
        return entry_price - atr * 1.5 if direction == "LONG" else entry_price + atr * 1.5
    except Exception:
        return entry_price * 0.97 if direction == "LONG" else entry_price * 1.03


def _check_margin_safe(add_notional: float) -> bool:
    """检查补仓后保证金是否仍在安全范围内。"""
    try:
        positions = api.get_positions()
        used = sum(
            abs(float(p["positionAmt"])) * float(p.get("markPrice", 0))
            for p in positions if abs(float(p.get("positionAmt", 0))) > 0
        )
        bal_data = api.request("GET", api.BASE_FAPI + "/fapi/v2/balance", {})
        balance  = float([b for b in bal_data if b["asset"] == "USDT"][0]["availableBalance"])
        if balance <= 0:
            return False
        # 补仓后占用 <= 余额80%（名义仓位/杠杆 = 保证金）
        leverage = 50
        new_margin_used = (used + add_notional) / leverage
        return new_margin_used <= balance * MARGIN_LIMIT
    except Exception:
        return True  # 查询失败时保守放行


def _round_qty(qty: float, step_str: str) -> str:
    step = float(step_str)
    if step <= 0:
        return str(qty)
    d = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
    return f"{round(math.floor(qty / step) * step, d):.{d}f}"


def check_and_add(sym: str, notify_fn=None) -> bool:
    """
    检查是否满足技术位补仓条件，满足则挂补仓限价单。
    返回 True = 已挂单，False = 不满足或已跳过。
    """
    pos_data = _ps_store.read(sym)
    if not pos_data:
        return False

    direction   = pos_data.get("direction", "LONG")
    entry_price = float(pos_data.get("entry_price", 0))
    orig_qty    = float(pos_data.get("qty", 0))
    sig_type    = pos_data.get("sig_type", "trend")
    scenario    = int(pos_data.get("scenario", 0))
    if entry_price <= 0 or orig_qty <= 0:
        return False

    # 获取实时行情
    try:
        live = api.get_positions()
        pos_live = next((p for p in live if p["symbol"] == sym and
                         abs(float(p.get("positionAmt", 0))) > 0), None)
        if not pos_live:
            return False
        mark_price  = float(pos_live.get("markPrice", entry_price))
        current_qty = abs(float(pos_live.get("positionAmt", 0)))
    except Exception:
        return False

    # ── 条件一：距上次补仓 >30分钟 ────────────────────────────────────────────
    add_state = _load_add_state()
    sym_state = add_state.get(sym, {})
    add_count  = int(sym_state.get("count", 0))
    last_add_ts = float(sym_state.get("last_ts", 0))

    if add_count >= MAX_ADD_TIMES:
        return False
    if time.time() - last_add_ts < ADD_COOLDOWN:
        return False

    # ── 条件二：获取支撑/压力位 ───────────────────────────────────────────────
    key_price = _get_support_resistance(sym, direction, entry_price)

    # ── 条件三：价格回踩/反弹到位 ────────────────────────────────────────────
    if direction == "LONG":
        # 价格在支撑位上方0.5%以内，且支撑位结构未被破坏（当前价 > 支撑位）
        near_support = (
            mark_price > key_price and
            (mark_price - key_price) / key_price <= SUPPORT_TOL
        )
        if not near_support:
            return False
        # RSI 未超卖（>30 说明还有下行空间但未崩溃）
        rsi = _get_rsi1h(sym)
        if rsi <= 30:
            return False
    else:
        # 价格在压力位下方0.5%以内，且压力位结构未被突破（当前价 < 压力位）
        near_resist = (
            mark_price < key_price and
            (key_price - mark_price) / key_price <= SUPPORT_TOL
        )
        if not near_resist:
            return False
        rsi = _get_rsi1h(sym)
        if rsi >= 70:
            return False

    # ── 条件四：保证金安全检查 ────────────────────────────────────────────────
    # 补仓金额：场景四且评分升到>=80补全，否则首仓大小
    add_notional = entry_price * orig_qty  # 不超过首仓大小
    if scenario == 4:
        try:
            from calc_position_size import calc as _cp
            from check_add_position import _get_health_score_local
            hs = _get_health_score_local(sym, direction, sig_type)
            if hs >= 80:
                full = _cp(sig_type=sig_type, score=hs, direction=direction)
                add_notional = max(0, full - entry_price * current_qty)
        except Exception:
            pass

    if not _check_margin_safe(add_notional):
        if notify_fn:
            notify_fn(f"⚠️ [{sym}] 补仓跳过：保证金占用超过{MARGIN_LIMIT*100:.0f}%上限")
        return False

    # ── 挂补仓限价单 ──────────────────────────────────────────────────────────
    add_price = key_price  # 在支撑/压力位精确挂单
    try:
        sinfo    = api.get_symbol_info(sym)
        step_str = sinfo.get("step_size", "0.001")
        tick_str = sinfo.get("tick_size", "0.0001")
    except Exception:
        step_str, tick_str = "0.001", "0.0001"

    add_qty_str = _round_qty(add_notional / add_price, step_str)
    add_qty     = float(add_qty_str)
    if add_qty <= 0:
        return False

    tick = float(tick_str)
    d_tick = max(0, -int(math.floor(math.log10(tick)))) if 0 < tick < 1 else 0
    price_str = f"{add_price:.{d_tick}f}"

    try:
        r = api.place_order(
            sym, "BUY" if direction == "LONG" else "SELL", "LIMIT",
            quantity=add_qty_str, price=price_str,
            positionSide=direction, timeInForce="GTC",
        )
        if not r.get("orderId"):
            if notify_fn:
                notify_fn(f"⚠️ [{sym}] 补仓挂单失败: {r}")
            return False
    except Exception as e:
        if notify_fn:
            notify_fn(f"⚠️ [{sym}] 补仓挂单异常: {e}")
        return False

    # 更新状态
    add_state[sym] = {"count": add_count + 1, "last_ts": time.time(),
                      "last_price": add_price, "last_qty": add_qty}
    _save_add_state(add_state)
    _ps_store.upsert(sym, {"add_count": add_count + 1, "last_add_price": add_price,
                           "last_add_ts": time.time()})

    if notify_fn:
        notify_fn(
            f"➕ [{sym}] 技术位补仓挂单\n"
            f"方向: {direction}  支撑/压力位: {price_str}  数量: {add_qty_str}\n"
            f"RSI={rsi:.1f}  已补仓: {add_count+1}次"
        )
    return True


def _get_health_score_local(sym: str, direction: str, sig_type: str) -> float:
    """简单重新评分，供补仓场景四使用。"""
    try:
        k1h  = api.get_klines(sym, "1h", 100)
        k4h  = api.get_klines(sym, "4h", 50)
        k15m = api.get_klines(sym, "15m", 20)
        tk   = api.get_ticker(sym) or {}
        fr   = float(tk.get("lastFundingRate", 0))
        from entry_logic import (calc_score_trend, calc_score_reversal,
                                  calc_score_trend_short, calc_score_short)
        if direction == "LONG":
            s, _ = (calc_score_reversal if "reversal" in sig_type
                    else calc_score_trend)(sym, k1h, k4h, k15m, tk, fr)
        else:
            s, _ = (calc_score_short if "reversal" in sig_type
                    else calc_score_trend_short)(sym, k1h, k4h, k15m, tk, fr)
        return s
    except Exception:
        return 0.0


if __name__ == "__main__":
    print("check_add_position (Q2技术位版) 加载正常 ✅")
