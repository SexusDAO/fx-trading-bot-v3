#!/usr/bin/env python3
"""
paper_scanner_test.py — 胜率测试版（仓位无上限，固定100U，纯胜率统计）

★ 模拟模式说明：
  - 完整复刻 scanner.py 的所有评分、筛选、仓位计算逻辑
  - 仓位大小使用 calc_position_size_real.py 真实档位（不是固定100U）
  - 所有「开仓」「撤单」「止盈止损」仅记录于 paper_sim_state.json
  - 推送消息带 [模拟] 标记，发到当前 Jarvis thread
  - 持续运行（daemon），直到收到 SIGTERM/SIGINT
  - 止盈止损每轮扫描时自动检查，用实时 mark price 判断

运行方式：
  python3 paper_scanner.py          # 持续 daemon 模式
  python3 paper_scanner.py --once   # 单次扫描
  python3 paper_scanner.py --status # 查看模拟仓位状态
  python3 paper_scanner.py --reset  # 清空模拟状态
"""

import json
import os
import sys
import time
import subprocess
from datetime import datetime, timezone

sys.path.insert(0, "/root/.openclaw/workspace")
import binance_api as api
from notifier import silent as _notifier_normal  # 模拟模式：全部静默，不推送 Jarvis
from config_watcher import config as _cfg
from market_context import get_context as _get_market_ctx
from risk_manager import is_circuit_open as _is_circuit_open, check_market_shock as _check_market_shock
import coinglass_api as _cg

# ── 第9项：Coinglass 健康度监控 ──────────────────────────────────────────────
_CG_HEALTH: dict = {"ok": 0, "fail_404": 0, "fail_timeout": 0, "hour_start": 0.0}

def _cg_record(result: str):
    import time as _t
    now = _t.time()
    global _CG_HEALTH
    if now - _CG_HEALTH["hour_start"] >= 3600:
        total = _CG_HEALTH["ok"] + _CG_HEALTH["fail_404"] + _CG_HEALTH["fail_timeout"]
        if total > 0:
            rate = _CG_HEALTH["ok"] / total * 100
            msg = (f"📊 Coinglass 过去1H成功率: {rate:.0f}% "
                   f"(成功{_CG_HEALTH['ok']}/404:{_CG_HEALTH['fail_404']}/超时:{_CG_HEALTH['fail_timeout']})")
            log(msg)
            if rate < 50:
                notify(f"⚠️ Coinglass 可用性下降\n{msg}\n当前开仓止损精度受影响")
        _CG_HEALTH = {"ok": 0, "fail_404": 0, "fail_timeout": 0, "hour_start": now}
    _CG_HEALTH[result] = _CG_HEALTH.get(result, 0) + 1
# ─────────────────────────────────────────────────────────────────────────────
from calc_position_size_real import real_size as _real_size  # 模拟模式使用真实档位
from market_filter import apply as _market_filter      # P2: 环境过滤

# 模拟模式：固定 100U，只看信号方向和胜率
def _calc_pos_size(sig_type, score, direction, details):
    return 100.0

# 模拟模式： pre_order_gate 永远放行（不真实下单）
class _FakeGateResult:
    def __init__(self): self.reason = ""
    def __bool__(self): return True
def _pre_order_gate(**kwargs): return _FakeGateResult()

# ── Coinglass 调用缓存（优化一）─────────────────────────────────────────
# 目标：10次/min 限速下，每次扫描只对“观察区 + score>=70”标的调用，结果缓存 15 分钟
# 对同一标的 15 分钟内重复扫描直接返回缓存
_CG_CACHE: dict[str, dict] = {}   # sym → {"ts": float, "zones": ..., "bonus": int, "details": dict}
_CG_CACHE_TTL = 30 * 60           # 30 分钟缓存有效期（优化：从 15 分钟延长）
_CG_LOCK = __import__("threading").Lock()

# ── K 线缓存（优化二）─────────────────────────────────────────
# 避免重复拉取 K 线，5 分钟 TTL
_KLINE_CACHE: dict[str, dict] = {}  # {sym_interval_limit: {ts, klines}}
_KLINE_TTL = 5 * 60                 # 5 分钟缓存有效期
_KLINE_LOCK = __import__("threading").Lock()


def _get_klines_cached(sym: str, interval: str, limit: int) -> list:
    """
    带缓存的 K 线拉取。
    同一标的 + 时间周期 + 数量，5 分钟内直接返回缓存，不重复调用 API。
    """
    key = f"{sym}_{interval}_{limit}"
    now = time.time()
    with _KLINE_LOCK:
        cached = _KLINE_CACHE.get(key)
        if cached and (now - cached["ts"]) < _KLINE_TTL:
            log(f"[kline_cache_hit] {sym} {interval}x{limit} TTL 剩余{int(_KLINE_TTL-(now-cached['ts']))}s")
            return cached["klines"]
    
    # 未命中：真实调用 API
    klines = api.get_klines(sym, interval, limit)
    with _KLINE_LOCK:
        _KLINE_CACHE[key] = {"ts": now, "klines": klines}
    return klines



def _cg_get_bonus_cached(sym: str, direction: str, mark: float, details: dict) -> int:
    """
    带缓存的 Coinglass 清算区查询。
    命中率：直接返回缓存，不消耗 API 配额。
    未命中：不超过 8 次/min，留 2 次给开仓时的 get_signal_for_open。
    """
    now = time.time()
    with _CG_LOCK:
        cached = _CG_CACHE.get(sym)
        if cached and (now - cached["ts"]) < _CG_CACHE_TTL:
            # 命中缓存：把缓存的 zones 字段回写到 details
            details.update({k: v for k, v in cached.get("details", {}).items()
                            if k not in details})
            log(f"[cg_cache_hit] {sym} TTL剩余 {int(_CG_CACHE_TTL-(now-cached['ts']))}s")
            return cached["bonus"]

    # 未命中：真实调用 Coinglass API
    bonus = score_symbol_cg_bonus(sym, direction, mark, details)
    with _CG_LOCK:
        _CG_CACHE[sym] = {
            "ts":      now,
            "bonus":   bonus,
            "details": {k: details.get(k) for k in
                        ("cg_bonus", "cg_heatmap", "cg_deriv", "cg_zones",
                         "cg_bias", "cg_reason", "cg_deriv_sig")},
        }
    return bonus
import dual_net_orders as _dn
from entry_logic import (
    calc_score_trend,
    liquidation_map,
    calc_entry_mode,
    check_btc_filter,
    classify_signal,
    atr_calc,
    rsi_calc,
    ema_calc,
    _COND_ORDER_MIN_SCORE,
)

# 模拟模式： 仓位管理完全用模拟内常，不读取真实 pos_state.json
PAPER_SIM_STATE   = "/root/.openclaw/workspace/paper_test_state.json"
PAPER_CAPITAL_U   = 1000.0   # 模拟总资金

# 模拟占名义仓位计算（复用 scanner.py 预统计单口）
_paper_positions: dict = {}   # sym -> pos dict

def _load_paper_state() -> dict:
    import pathlib, json as _j
    f = pathlib.Path(PAPER_SIM_STATE)
    if f.exists():
        try:
            state = _j.loads(f.read_text())
            # 过滤旧数据：history 中 valid_from 不匹配当前 reset_at 的条目一律剔除
            reset_at = state.get("reset_at", "")
            if reset_at and state.get("history"):
                clean = [r for r in state["history"] if r.get("valid_from","") == reset_at]
                if len(clean) != len(state["history"]):
                    # 有脂数据，重算 stats
                    state["history"] = clean
                    wins   = sum(1 for r in clean if r.get("pnl_u",0) > 0)
                    losses = sum(1 for r in clean if r.get("pnl_u",0) <= 0)
                    pnl    = round(sum(r.get("pnl_u",0) for r in clean), 2)
                    state["stats"] = {"total": len(clean), "wins": wins, "losses": losses, "pnl": pnl}
            return state
        except Exception: pass
    return {"positions": {}, "pending_orders": {}, "history": [], "stats": {"total": 0, "wins": 0, "losses": 0, "pnl": 0.0}}

def _save_paper_state(state: dict):
    import pathlib, json as _j
    f = pathlib.Path(PAPER_SIM_STATE)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(_j.dumps(state, indent=2, ensure_ascii=False))
    tmp.replace(f)

def _paper_open(sym, direction, entry_price, sl_price, tp1_price, tp2_price, pos_usdt, sig_type, score, details):
    """模拟开仓，写入 paper_sim_state.json，推送通知"""
    state = _load_paper_state()
    if sym in state["positions"]:
        return False  # 已有模拟仓位
    # 检查仓位上限
    positions = state["positions"]
    long_cnt  = sum(1 for p in positions.values() if p["direction"] == "LONG")
    short_cnt = sum(1 for p in positions.values() if p["direction"] == "SHORT")
    if len(positions) >= MAX_POSITIONS:
        return False
    if direction == "LONG"  and long_cnt  >= MAX_LONG:  return False
    if direction == "SHORT" and short_cnt >= MAX_SHORT: return False

    now_str = __import__('datetime').datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    state["positions"][sym] = {
        "symbol":      sym,
        "direction":   direction,
        "entry":       entry_price,
        "sl_price":    sl_price,
        "tp1_price":   tp1_price,
        "tp2_price":   tp2_price,
        "pos_usdt":    pos_usdt,
        "sig_type":    sig_type,
        "score":       score,
        "trend_score": details.get("trend_score", 0),
        "timing_score": details.get("timing_score", 0),
        "opened_at":   now_str,
        "btc_trend":   details.get("btc_trend", ""),
        "sentiment":   details.get("sentiment", ""),
    }
    _save_paper_state(state)
    dir_zh = "多" if direction == "LONG" else "空"
    log(
        f"[测试] 📋 {sym} {dir_zh}单开仓 | {now_str}\n"
        f"评分：{score:.0f}  信号：{sig_type}\n"
        f"入场：{entry_price:.6g}\n"
        f"止损：{sl_price:.6g}  止盈TP1：{tp1_price:.6g}\n"
        f"仓位：{pos_usdt:.0f}U  ⚠️ 仅模拟，不动本金"
    )
    return True

def _paper_close(sym, reason, close_price=None):
    """模拟平仓，更新统计，推送通知"""
    state = _load_paper_state()
    if sym not in state["positions"]:
        return
    pos = state["positions"].pop(sym)
    if close_price is None or close_price == 0:
        # 三级 fallback：mark_price → ticker lastPrice → entry
        try:
            close_price = float(api.get_mark_price(sym)["markPrice"])
        except Exception:
            pass
        if not close_price:
            try:
                t = api.get_ticker_24h(sym)
                if isinstance(t, list): t = t[0]
                close_price = float(t["lastPrice"])
            except Exception:
                pass
        if not close_price:
            close_price = pos["entry"]
            log(f"[warn] {sym} 平仓价格全部获取失败，用入场价代替")

    entry    = pos["entry"]
    direction= pos["direction"]
    pos_usdt = pos["pos_usdt"]
    if direction == "LONG":
        pnl_pct = (close_price - entry) / entry * 100
    else:
        pnl_pct = (entry - close_price) / entry * 100
    # 使用名义仓位计算盈亏（杠杆隐含在 pos_usdt 里——这是名义，不乘杠杆）
    # 实际盈亏 = pos_usdt * pnl_pct / 100
    pnl_u = round(pos_usdt * pnl_pct / 100, 2)

    # valid_from: 当前重置的基准时间，写入每条平仓记录便于后续统计时过滤旧数据
    _valid_from = state.get("reset_at", "")

    rec = {**pos,
           "symbol":      sym,
           "close_price": close_price,
           "close_at":    __import__('datetime').datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
           "pnl_u":       pnl_u,
           "pnl_pct":     round(pnl_pct, 2),
           "reason":      reason,
           "valid_from":  _valid_from}
    state.setdefault("history", []).append(rec)
    stats = state.setdefault("stats", {"total": 0, "wins": 0, "losses": 0, "pnl": 0.0})
    stats["total"] += 1
    if pnl_u > 0: stats["wins"] += 1
    else:         stats["losses"] += 1
    stats["pnl"] = round(stats["pnl"] + pnl_u, 2)
    _save_paper_state(state)

    # ── IC 追踪：写入 ws_cache/signal_ic_log.jsonl ───────────────────────────
    # 记录 score / sig_type / pnl_pct 供后续 IC 统计分析
    try:
        import pathlib as _pl, json as _jj, time as _ti
        _ic_path = _pl.Path(WORKSPACE) / "ws_cache" / "signal_ic_log.jsonl"
        _ic_path.parent.mkdir(parents=True, exist_ok=True)
        _ic_rec = {
            "ts":       _ti.time(),
            "sym":      sym,
            "sig_type": pos.get("sig_type", "?"),
            "score":    pos.get("score", 0),
            "direction":direction,
            "pnl_pct":  round(pnl_pct, 4),
            "pnl_u":    pnl_u,
            "reason":   reason,
            "hold_h":   round((__import__('time').time() - pos.get("opened_ts", _ti.time())) / 3600, 2),
        }
        with open(_ic_path, "a") as _icf:
            _icf.write(_jj.dumps(_ic_rec) + "\n")
    except Exception:
        pass

    emoji = "✅" if pnl_u > 0 else "❌"
    dir_zh = "多" if direction == "LONG" else "空"
    log(
        f"[模拟] {emoji} {sym} {dir_zh}单平仓 ({reason})\n"
        f"入场={entry:.6g}  出场={close_price:.6g}  PnL={pnl_u:+.2f}U ({pnl_pct:+.2f}%)\n"
        f"历史胜率={stats['wins']}/{stats['total']}  总PnL={stats['pnl']:+.2f}U"
    )

def _paper_place_limit_order(sym, direction, limit_price, sl_price, tp1_price, tp2_price,
                              pos_usdt, sig_type, score, details):
    """模拟挂限价单（不立即成交），记录到 pending_orders，推送通知"""
    state = _load_paper_state()
    # 检查仓位上限（挂单也占槽位）
    positions = state.get("positions", {})
    pending   = state.get("pending_orders", {})
    if sym in positions or sym in pending:
        return False  # 已有仓位或挂单
    long_cnt  = sum(1 for p in positions.values() if p["direction"] == "LONG") +                 sum(1 for p in pending.values()   if p["direction"] == "LONG")
    short_cnt = sum(1 for p in positions.values() if p["direction"] == "SHORT") +                 sum(1 for p in pending.values()   if p["direction"] == "SHORT")
    if len(positions) + len(pending) >= MAX_POSITIONS: return False
    if direction == "LONG"  and long_cnt  >= MAX_LONG:  return False
    if direction == "SHORT" and short_cnt >= MAX_SHORT: return False

    import datetime as _dt
    now_str = _dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    now_ts  = time.time()
    state.setdefault("pending_orders", {})[sym] = {
        "symbol":      sym,
        "direction":   direction,
        "limit_price": limit_price,
        "sl_price":    sl_price,
        "tp1_price":   tp1_price,
        "tp2_price":   tp2_price,
        "pos_usdt":    pos_usdt,
        "sig_type":    sig_type,
        "score":       score,
        "placed_at":   now_str,
        "placed_ts":   now_ts,
        "expire_ts":   now_ts + 8 * 3600,  # 快照一：8小时内未成交则标记失效
        "snap1_ts":    now_ts + 8 * 3600,   # 第一次快照时间点（挂单8小时检查）
        "btc_trend":   details.get("btc_trend", ""),
        "sentiment":   details.get("sentiment", ""),
    }
    _save_paper_state(state)
    dir_zh = "多" if direction == "LONG" else "空"
    log(
        f"[测试] 📌 {sym} {dir_zh}单挂单 | {now_str}\n"
        f"评分：{score:.0f}  信号：{sig_type}\n"
        f"限价：{limit_price:.6g}（现价下方等回调）\n"
        f"止损：{sl_price:.6g}  止盈TP1：{tp1_price:.6g}\n"
        f"仓位：{pos_usdt:.0f}U  有效期：8小时  ⚠️ 仅模拟"
    )
    return True


def _paper_check_pending():
    """每轮扫描时检查挂单是否成交或超时"""
    state = _load_paper_state()
    pending = state.get("pending_orders", {})
    if not pending:
        return
    now_ts = time.time()
    filled = []
    expired = []

    for sym, order in list(pending.items()):
        try:
            mk = float(api.get_mark_price(sym)["markPrice"])
        except Exception:
            continue
        direction   = order["direction"]
        limit_price = order["limit_price"]
        expire_ts   = order.get("expire_ts", now_ts + 1)

        # 检查是否成交：多单限价在现价下方，价格回落触碰即成交
        if direction == "LONG"  and mk <= limit_price:
            filled.append((sym, order, mk))
        elif direction == "SHORT" and mk >= limit_price:
            filled.append((sym, order, mk))
        # snap_fix: 挂单偏离>1%直接贴市价成交，降低miss率（与paper_scanner对齐）
        elif direction == "LONG" and mk > limit_price:
            _drift = abs(mk - limit_price) / (mk + 1e-9)
            if _drift > 0.01:
                log(f"[snap_fix] {sym} 多单挂单偏离{_drift*100:.1f}%>1%，贴市价成交 fill={mk:.6g}")
                filled.append((sym, order, mk))
                continue
            elif now_ts > expire_ts:
                expired.append((sym, order))
        elif direction == "SHORT" and mk < limit_price:
            _drift = abs(mk - limit_price) / (mk + 1e-9)
            if _drift > 0.01:
                log(f"[snap_fix] {sym} 空单挂单偏离{_drift*100:.1f}%>1%，贴市价成交 fill={mk:.6g}")
                filled.append((sym, order, mk))
                continue
            elif now_ts > expire_ts:
                expired.append((sym, order))
        # 快照一：8小时到期——标记未成交并写入 history
        elif now_ts > expire_ts:
            expired.append((sym, order))

    import datetime as _dt

    for sym, order, fill_price in filled:
        del pending[sym]
        # 转为持仓
        positions = state.setdefault("positions", {})
        long_cnt  = sum(1 for p in positions.values() if p["direction"] == "LONG")
        short_cnt = sum(1 for p in positions.values() if p["direction"] == "SHORT")
        dr = order["direction"]
        if len(positions) >= MAX_POSITIONS: continue
        if dr == "LONG"  and long_cnt  >= MAX_LONG:  continue
        if dr == "SHORT" and short_cnt >= MAX_SHORT: continue

        now_str = _dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        positions[sym] = {
            "symbol":           sym,
            "direction":        dr,
            "entry":            fill_price,
            "limit_price_orig": order["limit_price"],
            "sl_price":         order["sl_price"],
            "tp1_price":        order["tp1_price"],
            "tp2_price":        order["tp2_price"],
            "pos_usdt":         order["pos_usdt"],
            "sig_type":         order["sig_type"],
            "score":            order["score"],
            "opened_at":        now_str,
            "opened_ts":        now_ts,
            "placed_at":        order["placed_at"],
            "placed_ts":        order["placed_ts"],
            "intra_high":       fill_price,   # ATR移动止损：持仓最高价
            "intra_low":        fill_price,   # ATR移动止损：持仓最低价
            "btc_trend":        order.get("btc_trend", ""),
            "sentiment":        order.get("sentiment", ""),
            "snap8_done":       False,   # 8h 快照是否已记录
            "snap24_done":      False,   # 24h 快照是否已记录
            "snapshots":        [],      # [快照记录]
        }
        # ── 成交后校验SL方向：防止挂单期间价格偏移导致SL落在错误方向 ──
        _sl_now = positions[sym]["sl_price"]
        _sl_ok = (_sl_now < fill_price) if dr == "LONG" else (_sl_now > fill_price)
        if not _sl_ok:
            try:
                _k1h_fix = api.get_klines(sym, "1h", 30)
                _atr_fix = atr_calc(_k1h_fix)
                if dr == "LONG":
                    _sl_fixed = round(fill_price - _atr_fix * 1.5, 8)
                else:
                    _sl_fixed = round(fill_price + _atr_fix * 1.5, 8)
                # 上限8%
                _SL_MAX_FIX = 0.08
                if abs(_sl_fixed - fill_price) / fill_price > _SL_MAX_FIX:
                    _sl_fixed = round(fill_price * (1 - _SL_MAX_FIX) if dr == "LONG" else fill_price * (1 + _SL_MAX_FIX), 8)
                positions[sym]["sl_price"] = _sl_fixed
                log(f"[paper_sl_fix] {sym} {dr} SL方向错误({_sl_now:.5g})，成交后重算→{_sl_fixed:.5g} (entry={fill_price:.5g})")
            except Exception as _e:
                log(f"[paper_sl_fix] {sym} SL重算失败: {_e}")
        dir_zh = "多" if dr == "LONG" else "空"
        wait_min = int((now_ts - order["placed_ts"]) / 60)
        log(
            f"[测试] ✅ {sym} {dir_zh}单限价成交！| {now_str}\n"
            f"挂单价：{order['limit_price']:.6g}  成交价：{fill_price:.6g}\n"
            f"等待时间：约{wait_min}分钟\n"
            f"止损：{positions[sym]['sl_price']:.6g}  止盈TP1：{order['tp1_price']:.6g}\n"
            f"仓位：{order['pos_usdt']:.0f}U  将在 8h/24h 快照或止盈止损触发时结算"
        )
        log(f"[paper_filled] {sym} {dr} 限价成交 limit={order['limit_price']:.6g} fill={fill_price:.6g}")

    for sym, order in expired:
        del pending[sym]
        dr = order["direction"]
        dir_zh = "多" if dr == "LONG" else "空"
        placed = order.get("placed_at", "")
        # 快照一：记录8h未成交结果到 history
        try: mk_now = float(api.get_mark_price(sym)["markPrice"])
        except: mk_now = 0.0
        _valid_from = state.get("reset_at", "")
        snap_rec = {
            "symbol":       sym,
            "direction":    dr,
            "sig_type":     order.get("sig_type", ""),
            "score":        order.get("score", 0),
            "limit_price":  order["limit_price"],
            "market_at_expire": mk_now,
            "price_gap_pct":    round((mk_now - order["limit_price"]) / order["limit_price"] * 100, 2)
                                 if order["limit_price"] > 0 else 0,
            "placed_at":    placed,
            "result":       "snap1_miss",   # 快照一：8h内未触达挂单价
            "reason":       "8h内未成交",
            "btc_trend":    order.get("btc_trend", ""),
            "valid_from":   _valid_from,
            "close_at":     _dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        }
        state.setdefault("history", []).append(snap_rec)
        _s = state.setdefault("stats", {"total": 0, "wins": 0, "losses": 0, "pnl": 0.0})
        _s["total"] += 1
        # 未成交不计入胜败，但计入总数（方便统计挂单成交率）
        _save_paper_state(state)
        log(
            f"[测试] ⏰ {sym} {dir_zh}单快照一：8h内未成交\n"
            f"挂单价：{order['limit_price']:.6g}  到期现价：{mk_now:.6g}\n"
            f"价差：{snap_rec['price_gap_pct']:+.2f}%  挂单时间：{placed}"
        )
        log(f"[paper_snap1_miss] {sym} {dr} 8h未成交 limit={order['limit_price']:.6g} market={mk_now:.6g}")

    state["pending_orders"] = pending
    _save_paper_state(state)


def _paper_check_sl_tp():
    """每轮扫描时检查所有模拟仓位的止盈止损（含分批止盈+移动止损+时间止损）"""
    import time as _time
    state = _load_paper_state()
    now_ts = _time.time()

    for sym in list(state["positions"].keys()):
        pos   = state["positions"][sym]
        try:
            mark = float(api.get_mark_price(sym)["markPrice"])
        except Exception:
            continue

        direction = pos["direction"]
        sl   = pos["sl_price"]
        tp1  = pos["tp1_price"]
        ep   = pos["entry"]
        pu   = pos["pos_usdt"]

        # ── 优化①：移动止损（TP1触发后启动，跑趋势剩余50%仓）──────────
        # TP1 已触发过（trailing_sl 已设置）：用移动止损价检查
        trailing_sl = pos.get("trailing_sl", None)
        if trailing_sl is not None:
            # 更新移动止损：跟随最高/最低价
            if direction == "LONG":
                new_trail = round(mark * 0.95, 8)   # 当前价 -5% 做移动止损
                if new_trail > trailing_sl:
                    state["positions"][sym]["trailing_sl"] = new_trail
                    trailing_sl = new_trail
                if mark <= trailing_sl:
                    _paper_close(sym, "移动止损触发", mark)
                    continue
            else:
                new_trail = round(mark * 1.05, 8)
                if new_trail < trailing_sl:
                    state["positions"][sym]["trailing_sl"] = new_trail
                    trailing_sl = new_trail
                if mark >= trailing_sl:
                    _paper_close(sym, "移动止损触发", mark)
                    continue
            _save_paper_state(state)
            continue   # 剩余仓位只用移动止损，不再判断原始 TP1

        # ── vnpy式ATR移动止损：入场即启动 ────────────────────────────────────
        ATR_TRAIL_PCT = 0.04; ATR_TRAIL_MIN = 0.015
        if pos.get("trailing_sl") is None:
            _ih = max(float(pos.get("intra_high", ep)), mark)
            _il = min(float(pos.get("intra_low",  ep)), mark)
            state["positions"][sym]["intra_high"] = _ih
            state["positions"][sym]["intra_low"]  = _il
            if direction == "LONG":
                _atr_trail = round(_ih * (1 - ATR_TRAIL_PCT), 8)
                if _ih >= ep * (1 + ATR_TRAIL_MIN) and _atr_trail > sl:
                    if mark <= _atr_trail:
                        _paper_close(sym, f"ATR移动止损(ih={_ih:.4g})", mark); continue
                    if _atr_trail > state["positions"][sym]["sl_price"]:
                        state["positions"][sym]["sl_price"] = _atr_trail
                        sl = _atr_trail
            else:
                _atr_trail = round(_il * (1 + ATR_TRAIL_PCT), 8)
                if _il <= ep * (1 - ATR_TRAIL_MIN) and _atr_trail < sl:
                    if mark >= _atr_trail:
                        _paper_close(sym, f"ATR移动止损(il={_il:.4g})", mark); continue
                    if _atr_trail < state["positions"][sym]["sl_price"]:
                        state["positions"][sym]["sl_price"] = _atr_trail
                        sl = _atr_trail

        # ── 止损检查 ────────────────────────────────────────────────────
        if direction == "LONG":
            if mark <= sl:
                _paper_close(sym, "SL触发", mark); continue
        else:
            if mark >= sl:
                _paper_close(sym, "SL触发", mark); continue

        # ── 优化①：TP1触发时平50%，剩余启动移动止损 ──────────────────
        if direction == "LONG" and mark >= tp1:
            half_pnl = round(pu * 0.5 * (mark - ep) / ep, 2)
            state["positions"][sym]["pos_usdt"] = round(pu * 0.5, 2)    # 剩余50%
            # 移动止损初始化在当前价-2xATR，紧贴利润不回吐
            _atr_init = abs(ep - pos.get("sl_price", ep * 0.94)) / 1.8   # 用SL反推ATR（除数与 sl_mult 中间档匹配）
            state["positions"][sym]["trailing_sl"] = round(mark - _atr_init * 2.0, 8)
            _save_paper_state(state)
            log(
                f"[模拟] 🎯 {sym} 多 TP1触发（卉50%仓） "
                f"平仓价={mark:.5g}  卉50%PnL={half_pnl:+.2f}U "
                f"剩佐50%仓位启动移动止损 @{ep*1.05:.5g}，趪趋势"
            )
            log(f"[paper_tp1_half] {sym} TP1半仓平 mark={mark:.5g} half_pnl={half_pnl:+.2f}U")
            continue
        if direction == "SHORT" and mark <= tp1:
            half_pnl = round(pu * 0.5 * (ep - mark) / ep, 2)
            state["positions"][sym]["pos_usdt"] = round(pu * 0.5, 2)
            _atr_init_s = abs(pos.get("sl_price", ep * 1.06) - ep) / 1.8
            state["positions"][sym]["trailing_sl"] = round(mark + _atr_init_s * 2.0, 8)
            _save_paper_state(state)
            log(
                f"[模拟] 🎯 {sym} 空 TP1触发（卉50%仓） "
                f"平仓价={mark:.5g}  卉50%PnL={half_pnl:+.2f}U "
                f"剩佐50%仓位启动移动止损 @{ep*0.95:.5g}，趪趋势"
            )
            log(f"[paper_tp1_half] {sym} TP1半仓平 mark={mark:.5g} half_pnl={half_pnl:+.2f}U")
            continue


        # ── 8h/24h 快照 + 24h 底安强制平仓 ──────────────────────
        import datetime as _dtt
        opened_ts = pos.get("opened_ts", 0)
        if opened_ts == 0:
            try:
                _odt = _dtt.datetime.strptime(
                    pos.get("opened_at", "").replace(" UTC", ""), "%Y-%m-%d %H:%M"
                )
                opened_ts = _odt.replace(tzinfo=_dtt.timezone.utc).timestamp()
            except Exception:
                opened_ts = now_ts
        hold_h  = (now_ts - opened_ts) / 3600 if opened_ts else 0
        pnl_pct = (mark - ep)/ep*100 if direction=="LONG" else (ep - mark)/ep*100
        dir_zh  = "\u591a" if direction=="LONG" else "\u7a7a"

        # 快照二：8h 快照
        if hold_h >= 8 and not pos.get("snap8_done", False):
            snap8 = {
                "type":    "snap_8h",
                "hold_h":  round(hold_h, 1),
                "mark":    mark,
                "pnl_pct": round(pnl_pct, 2),
                "pnl_u":   round(pu * pnl_pct / 100, 2),
                "at":      _dtt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
            }
            state["positions"][sym].setdefault("snapshots", []).append(snap8)
            state["positions"][sym]["snap8_done"] = True
            _save_paper_state(state)
            emoji = "\u2705" if pnl_pct > 0 else "\U0001f534"
            log(
                f"[\u6d4b\u8bd5] {emoji} {sym} {dir_zh}\u5355 \u5feb\u7167\u4e8c(8h)\n"
                f"\u5165\u573a={ep:.6g}  \u73b0\u4ef7={mark:.6g}  \u6d6e\u52a8={pnl_pct:+.2f}%  PnL={pu*pnl_pct/100:+.2f}U\n"
                f"\u6b62\u635f={sl:.6g}  TP1={tp1:.6g}"
            )
            log(f"[paper_snap8] {sym} {direction} hold={hold_h:.1f}h pnl={pnl_pct:+.2f}%")

        # 快照三：24h 快照 + 底安强制平仓
        if hold_h >= 24:
            if not pos.get("snap24_done", False):
                snap24 = {
                    "type":    "snap_24h",
                    "hold_h":  round(hold_h, 1),
                    "mark":    mark,
                    "pnl_pct": round(pnl_pct, 2),
                    "pnl_u":   round(pu * pnl_pct / 100, 2),
                    "at":      _dtt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
                }
                state["positions"][sym].setdefault("snapshots", []).append(snap24)
                state["positions"][sym]["snap24_done"] = True
                _save_paper_state(state)
                emoji = "\u2705" if pnl_pct > 0 else "\U0001f534"
                log(
                    f"[\u6d4b\u8bd5] {emoji} {sym} {dir_zh}\u5355 \u5feb\u7167\u4e09(24h) \u2192 \u5e95\u5b89\u5f3a\u5236\u5e73\u4ed3\n"
                    f"\u5165\u573a={ep:.6g}  \u73b0\u4ef7={mark:.6g}  \u6d6e\u52a8={pnl_pct:+.2f}%  PnL={pu*pnl_pct/100:+.2f}U"
                )
                log(f"[paper_snap24] {sym} {direction} hold={hold_h:.1f}h pnl={pnl_pct:+.2f}%")
            _paper_close(sym, f"24h\u5e95\u5b89\u5e73\u4ed3({pnl_pct:+.2f}%)", mark)
            continue


def _get_position_state() -> dict:
    """从 paper_sim_state.json 读取模拟仓位状态（完全替代真实账户查询）"""
    state = _load_paper_state()
    positions_dict = state.get("positions", {})
    positions = list(positions_dict.values())
    long_pos  = [p for p in positions if p["direction"] == "LONG"]
    short_pos = [p for p in positions if p["direction"] == "SHORT"]

    used_notional = sum(p["pos_usdt"] for p in positions)
    remaining = max(0.0, PAPER_CAPITAL_U * 3 - used_notional)  # 模拟最大名义=资金*3

    return {
        "positions":          positions,
        "count":              len(positions),
        "long_count":         len(long_pos),
        "short_count":        len(short_pos),
        "active_syms":        set(positions_dict.keys()),
        "used_notional":      used_notional,
        "remaining_notional": remaining,
        "long_slots":         max(0, MAX_LONG  - len(long_pos)),
        "short_slots":        max(0, MAX_SHORT - len(short_pos)),
    }

# ─── 常量 ─────────────────────────────────────────────────────────────────────
WORKSPACE      = "/root/.openclaw/workspace"
WATCHLIST_FILE  = f"{WORKSPACE}/paper_watchlist.json"   # 模拟模式独立 watchlist
BLACKLIST_FILE  = f"{WORKSPACE}/daily_blacklist.json"


def _load_daily_blacklist() -> dict:
    """加载当日黑名单，日期不同自动清空"""
    from datetime import datetime, timezone as _tz
    today = datetime.now(_tz.utc).strftime("%Y-%m-%d")
    try:
        import json as _j
        d = _j.loads(open(BLACKLIST_FILE).read())
        if d.get("date") != today:
            return {"date": today, "symbols": {}}
        return d
    except Exception:
        return {"date": today, "symbols": {}}


def _save_daily_blacklist(bl: dict):
    """原子写黑名单"""
    import json as _j, os as _os
    tmp = BLACKLIST_FILE + ".tmp"
    open(tmp, "w").write(_j.dumps(bl, indent=2, ensure_ascii=False))
    _os.replace(tmp, BLACKLIST_FILE)


def _add_to_cooldown(sym: str, direction: str, hours: float):
    """将币种加入冷静期（止损后调用）"""
    bl = _load_daily_blacklist()
    expire_ts = time.time() + hours * 3600
    from datetime import datetime, timezone as _tz
    expire_str = datetime.fromtimestamp(expire_ts, tz=_tz.utc).strftime("%H:%M UTC")
    bl["symbols"][sym] = {
        "direction":  direction,
        "expire_ts":  expire_ts,
        "expire_str": expire_str,
        "reason":     f"止损后{hours}h冷静期",
    }
    _save_daily_blacklist(bl)
    log(f"[cooldown] {sym} {direction} 进入冷静期，到期 {expire_str}")
POS_STATE      = f"{WORKSPACE}/paper_sim_state.json"   # 模拟模式独立状态文件
LOG_FILE       = f"{WORKSPACE}/paper_scanner_test.log"       # 模拟模式独立日志
JARVIS_TARGET  = "432401404:thread:019fcac8-4a93-7bea-a930-7f0e59e4897c"  # 模拟模式推送到当前 thread

SCORE_FIRE_HIGH = 80   # ≥80 → 直接挂单（强信号）
SCORE_AUTO      = 75   # ≥75 → 候选挂单（优化：从70提高到75，过滤低质信号）
SCORE_WATCH     = 60   # ≥60 → 进观察区
# < 60 → 跳过
SCAN_TOP_N = 60   # 成交量榜基础扫描 60 个（另外 + 涨幅/跌幅/热度榜）
MIN_VOL_USD = 5_000_000   # 优化：从 300 万 U 提至 500 万 U（过滤低流动性）   # 最低 24h 成交额（300万U）

# ─── 仓位规则（用户确认版）─────────────────────────────────────────────────────
MAX_POSITIONS  = 999   # 测试版：无上限（观察所有达标信号的真实结果）
MAX_LONG       = 999   # 测试版：多单无上限
MAX_SHORT      = 999   # 测试版：空单无上限
MAX_NEW_PER_SCAN = 3   # 每轮扫描最多新开仓数（防止同一时间窗口集中建仓）

# ─── 相关性过滤：同集群多单上限（P0优化）──────────────────────────────────────
# 数据显示 BTC/ETH/SOL/BNB 等主流币高度相关（0.7-0.9），同时持有等于一笔大仓位
# 每个集群方向最多持有 N 个多单，防止同向集中曝险
CLUSTER_LONG_MAX = 2   # 同集群多单上限（默认2个）
CLUSTER_SHORT_MAX = 3  # 同集群空单上限（空单相关性稍低，给多一个槽位）

# 集群分类：按市场相关性分组
CLUSTER_MAP = {
    # 主流大饼系（BTC相关性 >0.75）
    "major": {"BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT","AVAXUSDT",
              "LINKUSDT","DOGEUSDT","NEARUSDT","LTCUSDT","BCHUSDT","ADAUSDT",
              "DOTUSDT","MATICUSDT","UNIUSDT","ATOMUSDT","APTUSDT","OPUSDT",
              "ARBUSDT","SUIUSDT","SEIUSDT","STXUSDT","INJUSDT","TIAUSDT"},
    # AI/数据赛道
    "ai":    {"TAOUSDT","FETUSDT","RENDERUSDT","WLDUSDT","AGIXUSDT","OCEANUSDT"},
    # Meme赛道（相互相关性较低，但与BTC仍有中度相关）
    "meme":  {"DOGEUSDT","SHIBUSDT","PEPEUSDT","1000PEPEUSDT","FLOKIUSDT",
              "1000SHIBUSDT","TRUMPUSDT","MOODENGUSDT"},
    # DeFi赛道
    "defi":  {"AAVEUSDT","CRVUSDT","MKRUSDT","COMPUSDT","SNXUSDT","BALUSDT"},
}
WL_MAX_CANDIDATES = 100  # 观察区候选上限 100 个

# ─── 相关性过滤工具函数 ─────────────────────────────────────────────────
def _get_cluster(sym: str) -> str:
    """返回该币属于哪个集群，找不到返回币种本身（山寨币单独归组）"""
    for cluster_name, members in CLUSTER_MAP.items():
        if sym in members:
            return cluster_name
    return sym  # 未分组的币种自成一组

def _check_correlation(sym: str, direction: str, positions: dict, pending: dict) -> tuple:
    """
    检查同集群同方向持仓数量是否超限。
    返回 (ok: bool, reason: str)
    """
    if direction != "LONG":
        # 空单相关性较低，空单集群限制宽松一点
        limit = CLUSTER_SHORT_MAX
    else:
        limit = CLUSTER_LONG_MAX

    cluster = _get_cluster(sym)
    # 未分组的币种（山寨币）：每个币独立，不受集群限制
    if cluster == sym:
        return True, ""

    all_holdings = {**positions, **pending}
    count = sum(
        1 for s, p in all_holdings.items()
        if s != sym
        and p.get("direction") == direction
        and _get_cluster(s) == cluster
    )
    if count >= limit:
        return False, f"集群[{cluster}]{direction}已有{count}个，上限{limit}"
    return True, ""

POS_MIN_U      = 100   # 单笔名义仓位下限（U）【复盘模式固定100U】
POS_MAX_U      = 100   # 单笔名义仓位上限（U）【复盘模式固定100U】
CAPITAL        = 1236   # 本金（U），2026-07-27 ON止盈后更新
LEVERAGE_MAX   = 3      # 最大杠杆倍数
MAX_NOTIONAL   = 3710   # 总名义仓位上限（U），1236×3（可从 config 动态读：_cfg.get('max_notional', MAX_NOTIONAL)）
LOG_MAX_LINES  = 2000

# exchange_info 缓存（启动时加载一次，避免每次挂单都重复调用）
_exinfo_cache: dict = {}
_exinfo_loaded = False

def _get_sym_filters(sym: str) -> dict:
    """返回 {tick_size, step_size}，从缓存读取。"""
    global _exinfo_cache, _exinfo_loaded
    if not _exinfo_loaded:
        try:
            info = api.get_exchange_info()
            for s in info.get("symbols", []):
                tick = next((f["tickSize"] for f in s.get("filters", [])
                             if f["filterType"] == "PRICE_FILTER"), "0.0001")
                step = next((f["stepSize"] for f in s.get("filters", [])
                             if f["filterType"] == "LOT_SIZE"), "1")
                _exinfo_cache[s["symbol"]] = {"tick_size": tick, "step_size": step}
            _exinfo_loaded = True
            log(f"[exinfo] 缓存 {len(_exinfo_cache)} 个合约精度")
        except Exception as e:
            log(f"[exinfo_err] {e}")
    return _exinfo_cache.get(sym, {"tick_size": "0.0001", "step_size": "1"})


# ─── 工具函数 ──────────────────────────────────────────────────────────────────

def log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    line = f"[{ts}] {msg}\n"
    # 只写文件，不写 stdout；防止 nohup/守护进程将 stdout 重定向到同一文件导致双写
    try:
        with open(LOG_FILE, 'a', encoding='utf-8') as _lf:
            _lf.write(line)
    except Exception:
        pass


def notify(msg: str):
    """[模拟模式] 静默——只写日志，不推送 Jarvis 通知"""
    if not msg.startswith("[模拟]"):
        msg = f"[模拟] {msg}"
    log(msg)   # 仅记录到本地日志，不推送


def _load_pos_state() -> dict:
    try:
        if os.path.exists(POS_STATE):
            return json.load(open(POS_STATE))
    except Exception:
        pass
    return {}


WL_TTL_HOURS = 24   # watchlist 条目有效期：24小时，超时自动清理


def _load_watchlist() -> dict:
    try:
        if os.path.exists(WATCHLIST_FILE):
            return json.load(open(WATCHLIST_FILE))
    except Exception:
        pass
    return {}


def _purge_stale_watchlist(wl: dict) -> tuple[dict, list]:
    """
    清理超过 WL_TTL_HOURS 的过期条目。
    返回 (cleaned_wl, removed_syms)
    """
    now = time.time()
    cutoff = now - WL_TTL_HOURS * 3600
    removed = []
    cleaned = {}
    for k, v in wl.items():
        if k in ("candidates", "last_updated"):
            cleaned[k] = v
            continue
        if not isinstance(v, dict):
            cleaned[k] = v
            continue
        added_str = v.get("added_at", "")
        try:
            from datetime import datetime as _dt
            ts = _dt.fromisoformat(added_str.replace("Z", "+00:00")).timestamp()
            if ts < cutoff:
                removed.append(k)
                continue
        except Exception:
            pass  # 无法解析时间则保留
        cleaned[k] = v
    return cleaned, removed


def _save_watchlist(wl: dict):
    # 每次保存前自动清理过期条目
    cleaned, removed = _purge_stale_watchlist(wl)
    if removed:
        log(f"[watchlist_purge] 清理 {len(removed)} 个过期条目: {removed}")
    json.dump(cleaned, open(WATCHLIST_FILE, "w"), indent=2, ensure_ascii=False)


def _get_position_state() -> dict:
    """模拟模式：从 paper_sim_state.json 读取状态（不查真实账户）"""
    state = _load_paper_state()
    positions_dict = state.get("positions", {})
    positions = list(positions_dict.values())
    long_pos  = [p for p in positions if p["direction"] == "LONG"]
    short_pos = [p for p in positions if p["direction"] == "SHORT"]
    used_notional = sum(p.get("pos_usdt", 0) for p in positions)
    remaining = max(0.0, PAPER_CAPITAL_U * 3 - used_notional)
    return {
        "positions":          positions,
        "count":              len(positions),
        "long_count":         len(long_pos),
        "short_count":        len(short_pos),
        "active_syms":        set(positions_dict.keys()),
        "used_notional":      used_notional,
        "remaining_notional": remaining,
        "long_slots":         max(0, MAX_LONG  - len(long_pos)),
        "short_slots":        max(0, MAX_SHORT - len(short_pos)),
    }


def _cancel_all_entry_orders():
    """模拟模式：无真实挂单，直接返回 0"""
    return 0


def _current_position_count() -> int:
    """[模拟] 当前持仓数量"""
    return len(_load_paper_state().get("positions", {}))


def _get_active_symbols() -> set:
    """[模拟] 当前持仓的合约"""
    return set(_load_paper_state().get("positions", {}).keys())
def _tick_size(sym: str) -> str:
    """获取合约价格精度"""
    try:
        info = api.get_exchange_info()
        for s in info.get("symbols", []):
            if s["symbol"] == sym:
                for f in s.get("filters", []):
                    if f["filterType"] == "PRICE_FILTER":
                        return f["tickSize"]
    except Exception:
        pass
    return "0.0001"


# ─── 核心评分 ──────────────────────────────────────────────────────────────────

def score_symbol_fast(sym: str, ticker: dict, funding: float, btc_trend: str = "") -> tuple:
    """
    第一阶段：纯本地数据快速评分（不调 Coinglass），约 2-3 秒/个。
    btc_trend: 'up'|'down'|'' — BTC 环境评分加减分。
      LONG + down  → -8（逆大盘做多，降分）
      SHORT + up   → -8（逆大盘做空，降分）
      LONG + up    → +5（顺势加分）
      SHORT + down → +5（顺势加分）
    返回 (score, direction, details) 或 (0, None, {})
    """
    try:
        k1h  = api.get_klines(sym, "1h", 30)
        k4h  = api.get_klines(sym, "4h", 20)
        k15m = api.get_klines(sym, "15m", 30)

        closes_1h = [float(k[4]) for k in k1h]
        vols_1h   = [float(k[5]) for k in k1h]
        mark      = float(ticker.get("lastPrice") or closes_1h[-1])
        chg24     = float(ticker.get("priceChangePercent", 0))

        sig_type  = classify_signal(k1h, k4h, funding, chg24, symbol=sym)
        if sig_type == "skip_stock_closed":
            log(f"[scan] {sym} 股票代币休市期间，跳过")
            return (0, None, {})
        if sig_type in ("reversal", "reversal_l1"):
            direction = "LONG"
        elif sig_type in ("reversal_short", "reversal_short_l1"):
            direction = "SHORT"
        elif sig_type == "trend_short":
            direction = "SHORT"
        else:
            # trend 多单补充 EMA 过滤：防止弱势币错误进入趋势多单路径
            # 要求：1H EMA20多排（ema20>ema50）OR 价格在EMA20上方
            if chg24 > 0:
                _c1h_tmp  = [float(k[4]) for k in k1h]
                _ema20_1h = ema_calc(_c1h_tmp[-20:], 20) if len(_c1h_tmp)>=20 else _c1h_tmp[-1]
                _ema50_1h = ema_calc(_c1h_tmp[-50:], 50) if len(_c1h_tmp)>=50 else ema_calc(_c1h_tmp, len(_c1h_tmp))
                _ema_bull = _ema20_1h > _ema50_1h
                _above_ema20 = _c1h_tmp[-1] > _ema20_1h
                if not (_ema_bull or _above_ema20):
                    # 弱势币：涨幅>0但EMA空排+价格在EMA下方，直接跳过不做趋势多单
                    return 0, None, {"skip": "trend_long_ema_filter"}
            direction = "LONG" if chg24 > 0 else "SHORT"

        if not check_btc_filter(direction):
            return 0, None, {"skip": "BTC filter"}

        # 根据路径选择评分函数
        from entry_logic import (calc_score_short as _css,
                                  calc_score_reversal as _csr,
                                  calc_score_trend_short as _csts)
        if sig_type == "trend_short":
            # 趋势延续空单：专属评分（RSI中段+跌势结构）
            score, details = _csts(sym, k1h, k4h, k15m, ticker, funding)
        elif sig_type in ("reversal_short", "reversal_short_l1"):
            # 逆势超涨做空：超涨反转评分
            score, details = _css(sym, k1h, k4h, k15m, ticker, funding)
        elif sig_type in ("reversal", "reversal_l1"):
            # 逆势超跌做多
            score, details = _csr(sym, k1h, k4h, k15m, ticker, funding)
        else:
            score, details = calc_score_trend(sym, k1h, k4h, k15m, ticker, funding)

        # ── BTC 环境评分加减分 (v2.8: reversal_short 不受BTC上涨惩罚，trend_short 轻罚-3) ──
        btc_adj = 0
        if btc_trend == "down" and direction == "LONG":    btc_adj = -5
        elif btc_trend == "up"   and direction == "SHORT":
            # reversal_short（超涨反转）：逆BTC趋势开空本身就是反转操作，不额外惩罚
            if sig_type in ("reversal_short", "reversal_short_l1"):
                btc_adj = 0
            else:
                btc_adj = 0   # paper_test 去掉 trend_short 惩罚，观察空单真实胜率
        elif btc_trend == "up"   and direction == "LONG":  btc_adj = +5
        elif btc_trend == "down" and direction == "SHORT": btc_adj = +5
        if btc_adj != 0:
            score = max(0, score + btc_adj)
            log(f"[btc_adj] {sym} {direction} btc={btc_trend} adj={btc_adj:+d} → score={score}")

        details.update({"final_score": score, "direction": direction,
                        "sig_type": sig_type, "funding": funding, "chg24": chg24,
                        "btc_adj": btc_adj})
        return score, direction, details

    except Exception as e:
        log(f"[score_fast_err] {sym}: {e}")
        return 0, None, {}


def score_symbol_cg_bonus(sym: str, direction: str, mark: float, details: dict) -> int:
    """
    第二阶段：调 coinglass_api 综合评分（热力图 + 多空比/OI/资金费率/大单/爆仓）。
    """
    try:
        return _cg.score_symbol_cg_bonus(sym, direction, mark, details)
    except Exception as e:
        log(f"[cg_bonus_err] {sym}: {e}")
        return 0

def _calc_position_usdt(score: float = 70, details: dict = None) -> float:
    """
    【复盘模式】固定返回 100U。
    真实档位见 calc_position_size_real.py，复盘后乘对应倍数还原真实盈亏。
    """
    if details is None:
        details = {}
    # 连续亏损5笔仍然暂停开仓
    try:
        from risk_manager import load_circuit as _lc
        if _lc().get("consecutive_losses", 0) >= 5:
            return 0.0
    except Exception:
        pass
    return 100.0


def _calc_limit_price(sym: str, direction: str, mark: float,
                      k1h: list, k4h: list, details: dict) -> float:
    """仅计算入场限价，不下单。供重复挂单优化比价用。
    使用多源锚点竞选，选距现价最近的合理支撑位，降低偏离度。"""
    import math
    try:
        lmap = liquidation_map(sym, k1h, k4h, direction)
        # 优先使用多源锚点竞选
        try:
            from entry_logic import calc_multi_source_support
            limit_price, src = calc_multi_source_support(sym, k1h, k4h, direction, lmap)
            log(f"[multi_source_support] {sym} {direction} 选中锚点={limit_price:.6f} 来源={src}")
        except Exception as _mse:
            log(f"[multi_source_support] {sym} 多源竞选异常({_mse})，降级到原有逻辑")
            score = details.get("final_score", 70)
            mode, limit_price, _, _ = calc_entry_mode(k1h, direction, lmap,
                                                       vol_threshold=1.0, final_score=score)
            if mode == 'WAIT':
                return 0.0
        # 对齐 tickSize
        tick_size = _get_sym_filters(sym)["tick_size"]
        t   = float(tick_size)
        dec = max(0, -int(math.floor(math.log10(t)))) if t < 1 else 0
        result = round(round(limit_price / t) * t, dec)
        # 安全检查：偏离超过 10% 则回退
        drift = abs(result - mark) / mark
        if drift > 0.10:
            log(f"[multi_source_support] {sym} 竞选结果偏离过大({drift:.1%})，回退")
            return 0.0
        return result
    except Exception:
        return 0.0


def place_entry_order(sym: str, direction: str, mark: float,
                      k1h: list, k4h: list, details: dict) -> dict:
    """Q6入场矩阵四场景 + entry_logic 计算入场价，对齐 tickSize/stepSize 后挂限价单。"""
    import math
    try:
        lmap    = liquidation_map(sym, k1h, k4h, direction)
        funding = details.get("funding", 0)
        score   = details.get("final_score", 0)

        # ── Q6: 入场矩阵四场景判断（利用 Q3 已检测的清算区字段）──────────────
        has_liq   = details.get("has_liq_zone", False)
        liq_price = float(details.get("liq_zone_price", 0))
        scenario  = details.get("scenario", 0)

        if score >= 80 and has_liq:
            # 场景二：高分+清算区 → 挂清算区边缘，4小时有效期
            _liq_entry = round(liq_price * 1.002, 8) if direction == "LONG" else round(liq_price * 0.998, 8)
            log(f"[Q6-场景二] {sym} score={score:.0f} has_liq @{liq_price:.6f}，入场价={_liq_entry:.6f}")
            mode, limit_price, wait_sec, anchor_src = 'PULLBACK', _liq_entry, 14400, 'liq_zone_edge'
        elif score >= 80 and not has_liq:
            # 场景一：高分+无清算区 → 短周期支撑位挂单，30min有效期
            mode, limit_price, wait_sec, anchor_src = calc_entry_mode(
                k1h, direction, lmap, vol_threshold=1.0, final_score=score)
            if mode == 'WAIT':
                # 找不到短周期支撑，贴近现价挂单（ATR×0.3偏移）
                _atr_s1 = lmap.get('atr_14', mark * 0.01)
                if direction == 'LONG':
                    limit_price = round(mark - _atr_s1 * 0.3, 8)
                else:
                    limit_price = round(mark + _atr_s1 * 0.3, 8)
                mode, wait_sec, anchor_src = 'PULLBACK', 1800, 'near_price_atr03'
                log(f"[Q6-场景一] {sym} 无支撑位，贴近现价 @{limit_price:.6f}")
            else:
                # 有短周期支撑，缩短有效期到 30min
                wait_sec = 1800
                log(f"[Q6-场景一] {sym} score={score:.0f} 短周期支撑 @{limit_price:.6f} mode={mode}")
        elif 60 <= score < 80 and has_liq:
            # 场景四：中分+清算区 → 挂单，仓位减半，2小时有效期
            _liq_entry = round(liq_price * 1.002, 8) if direction == "LONG" else round(liq_price * 0.998, 8)
            details["scenario"] = 4  # 标记场景四，供仓位计算用
            mode, limit_price, wait_sec, anchor_src = 'PULLBACK', _liq_entry, 7200, 'liq_zone_edge_s4'
            log(f"[Q6-场景四] {sym} score={score:.0f} has_liq @{liq_price:.6f}，仓位减半")
        else:
            # 场景三：中分+无清算区 → 只入观察区，不挂单
            if 60 <= score < 80:
                log(f"[Q6-场景三] {sym} score={score:.0f} 无清算区，入观察区不挂单")
                return {"ok": False, "mode": "WATCH", "reason": "scenario3_no_liq_zone"}
            # <60 已在上游 pre_order_gate 拦截，这里防层
            return {"ok": False, "mode": "SKIP", "reason": "score_too_low"}

        # P4-3: WAIT 模式改为挂条件限价单待价格回归，不再直接跳过
        if mode == 'WAIT':
            # 尝试用锡点价格挂条件单（限价单 + 4h有效期）
            _wait_price = limit_price if (limit_price > 0 and
                abs(limit_price - mark) / mark <= 0.30) else 0.0
            if _wait_price > 0:
                mode = 'COND_ORDER'
                log(f"[place_order] {sym} WAIT模式转为 COND_ORDER，等待价格回归 @{_wait_price:.6f}")
            else:
                log(f"[place_order_skip] {sym} mode=WAIT 且无合理锡点，跳过")
                return {"ok": False, "mode": "WAIT", "reason": anchor_src}

        # COND_ORDER 模式：偏差超阈值但在 30% 内，预埋条件限价单，跳过二次偏差拦战
        _is_cond = (mode == 'COND_ORDER')

        # 合理性校验：挂单价不能与市价偏差超过 dev_threshold（COND_ORDER 跳过此校验）
        from entry_logic import _dev_threshold
        _atr_check = lmap.get('atr_14', mark * 0.01)
        _dev_max = _dev_threshold(_atr_check, mark)
        _dev_actual = abs(limit_price - mark) / mark * 100 if mark > 0 else 0
        if not _is_cond and _dev_actual > _dev_max + 0.5:   # 留 0.5% 缓冲
            log(f"[place_order_skip] {sym} 偏差 {_dev_actual:.1f}% > 阈值 {_dev_max:.1f}%，跳过")
            return {"ok": False, "mode": mode, "reason": f"dev_{_dev_actual:.1f}%_exceeds_{_dev_max:.1f}%"}

        # 获取精度参数（缓存，避免重复调用）
        filters   = _get_sym_filters(sym)
        tick_size = filters["tick_size"]
        step_size = filters["step_size"]

        def _round_tick(p, tick):
            t = float(tick)
            dec = max(0, -int(math.floor(math.log10(t)))) if t < 1 else 0
            return round(round(p / t) * t, dec)

        def _round_step(q, step):
            s = float(step)
            dec = max(0, -int(math.floor(math.log10(s)))) if s < 1 else 0
            return round(math.floor(q / s) * s, dec)

        limit_price = _round_tick(limit_price, tick_size)
        pos_usdt    = float(details.get("pos_usdt", 0)) or _calc_position_usdt(score=details.get("final_score", 70), details=details)
        qty = _round_step(pos_usdt / limit_price, step_size)
        if qty <= 0:
            qty = _round_step(float(step_size), step_size)

        side = "BUY" if direction == "LONG" else "SELL"  # 提前定义，两个分支共用

        # ── COND_ORDER: 写入 cond_order_state.json，不占用持仓槽位 ──
        if _is_cond:
            import json as _jco, pathlib as _plco
            _co_file = _plco.Path(f"{WORKSPACE}/ws_cache/cond_order_state.json")
            _co_state = _jco.loads(_co_file.read_text()) if _co_file.exists() else {}
            # 容量检查
            _co_longs  = [v for v in _co_state.values() if v.get('direction')=='LONG']
            _co_shorts = [v for v in _co_state.values() if v.get('direction')=='SHORT']
            _co_limit  = 10 if direction == 'LONG' else 5
            _co_cur    = _co_longs if direction == 'LONG' else _co_shorts
            if sym in _co_state:
                log(f"[cond_order_skip] {sym} 已有条件单，跳过")
                return {"ok": False, "mode": "COND_ORDER", "reason": "already_exists"}
            if len(_co_cur) >= _co_limit:
                log(f"[cond_order_skip] {sym} {direction}条件单已达上限{_co_limit}，跳过")
                return {"ok": False, "mode": "COND_ORDER", "reason": "cap_reached"}
            # 直接挂限价单（条件单就是限价单）
            r_co = api.place_order(
                sym, side, "LIMIT",
                price=str(limit_price), quantity=qty,
                timeInForce="GTC", positionSide=direction,
            )
            _ok_co = bool(r_co.get("orderId"))
            _now_co = int(__import__('time').time())
            if _ok_co:
                _co_state[sym] = {
                    'direction':   direction,
                    'order_id':    r_co['orderId'],
                    'limit_price': float(limit_price),
                    'sl':          float(lmap.get('hard_sl', limit_price)),
                    'tp1':         float(lmap.get('soft_tp1', limit_price)),
                    'qty':         float(qty),
                    'placed_at':   _now_co,
                    'expire_at':   _now_co + 4 * 3600,
                    'score':       details.get('final_score', 0),
                    'sig_type':    details.get('sig_type', 'trend'),
                    'anchor_src':  anchor_src,
                    'notional':    pos_usdt,
                }
                _tmp_co = _co_file.with_suffix(".tmp")
                _tmp_co.write_text(_jco.dumps(_co_state, indent=2, ensure_ascii=False))
                _tmp_co.replace(_co_file)
                _dev_co = abs(limit_price - mark) / mark * 100 if mark > 0 else 0
                log(f"[cond_order] {sym} {direction} 条件限价单 @{limit_price}(离现价{_dev_co:.1f}%) {pos_usdt:.0f}U orderId={r_co['orderId']}")
            else:
                log(f"[cond_order_err] {sym} 条件单下单失败: {r_co}")
            return {"ok": _ok_co, "price": limit_price, "qty": qty,
                    "orderId": r_co.get("orderId"), "mode": "COND_ORDER",
                    "hard_sl": lmap.get('hard_sl', 0), "raw": r_co}

        r = api.place_order(
            sym, side, "LIMIT",
            price=str(limit_price),
            quantity=qty,
            timeInForce="GTC",
            positionSide=direction,
        )
        ok = bool(r.get("orderId"))
        hard_sl = lmap.get("hard_sl", 0)
        log(f"[place_order] {sym} {direction} {qty}张 @{limit_price} mode={mode} "
            f"hard_sl={hard_sl} ok={ok} err={r.get('msg','')}")

        # 模拟模式：不写入真实 pos_state，跳过 _ps_store.upsert
        pass  # [paper] pos_state write skipped

        return {"ok": ok, "price": limit_price, "qty": qty,
                "orderId": r.get("orderId"), "mode": mode,
                "wait_sec": wait_sec, "hard_sl": hard_sl, "raw": r}
    except Exception as e:
        log(f"[place_order_err] {sym}: {e}")
        return {"ok": False, "error": str(e)}

def add_to_watchlist(sym: str, direction: str, target_price: float,
                     score: float, note: str = ""):
    """
    将候选写入 watchlist.json，ws_monitor 会实时监控价格到达。
    direction: LONG → 等价格下探到 target_price 再触发；SHORT → 等价格上冲到 target_price
    watchlist direction 字段：LONG候选等 below（等价格跌到入场位），SHORT等 above
    """
    wl = _load_watchlist()
    wl_direction = "below" if direction == "LONG" else "above"
    existing_entry = wl.get(sym, {})
    # 保留 high80_ts（首次达到80分的时间戳），避免重写时清除保护期
    high80_ts = existing_entry.get("high80_ts") if isinstance(existing_entry, dict) else None
    # 本次评分 >=80 且没有记录过，写入时间戳
    if score >= 80 and not high80_ts:
        high80_ts = time.time()
        log(f"[protect] {sym} 首次达到 {score:.0f}≥80，保护期开始")
    entry = {
        "target":    target_price,
        "direction": wl_direction,
        "note":      note or f"scanner score={score:.0f} {direction}",
        "added_at":  datetime.now(timezone.utc).isoformat(),
        "score":     score,
    }
    if high80_ts:
        entry["high80_ts"] = high80_ts
    wl[sym] = entry
    _save_watchlist(wl)
    log(f"[watchlist] {sym} added target={target_price} dir={wl_direction} score={score:.0f}")


# ─── 主扫描流程 ────────────────────────────────────────────────────────────────

def run_scan():
    start_ts = time.time()
    log("=" * 60)
    log(f"[scan_start] {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    # ── 心跳写入（供 health_check.py 活跃度检查使用）────────────────────────
    try:
        import json as _hb_json, time as _hb_time
        _hb_file = pathlib.Path(WORKSPACE) / "ws_cache" / "paper_scanner_test_heartbeat.json"
        _hb_file.write_text(_hb_json.dumps({"ts": _hb_time.time()}, indent=2))
    except Exception:
        pass

    # ── Fix P2: 动态资金利用率上限（余额 × 2.5）────────────────────────────
    global MAX_NOTIONAL
    try:
        _bal = api.request("GET", api.BASE_FAPI + "/fapi/v2/balance", {})
        _avail = float([b for b in _bal if b['asset'] == 'USDT'][0]['availableBalance'])
        MAX_NOTIONAL = max(500, _avail * 2.5)   # 最低 500U 兜底
        log(f"[scan] 动态MAX_NOTIONAL={MAX_NOTIONAL:.0f}U（余额{_avail:.0f}U×2.5）")
    except Exception as _e:
        log(f"[scan] 余额获取失败，用默认MAX_NOTIONAL={MAX_NOTIONAL}U: {_e}")

    # ── 熔断检查（优先，仓位获取之前）────────────────────────────────────────
    if _is_circuit_open():
        log("[scan] 熔断中，跳过本轮开仓扫描")
        notify("⚠️ 熔断中，本轮扫描跳过开仓")
        return

    # ── 大行情熔断检查（功能一 v3.1）────────────────────────────────────────
    if _check_market_shock(notifier_fn=notify):
        log("[scan] 大行情熔断触发，跳过本轮开仓扫描")
        return
    # ─────────────────────────────────────────────────────────────────────────

    # ── Fix P0-3: 加载冷静期黑名单（止损后1h多/2h空不重开同方向）────────────
    _bl = _load_daily_blacklist()
    _now_ts = time.time()
    _active_cooldown = {
        sym: info for sym, info in _bl.get("symbols", {}).items()
        if _now_ts < info.get("expire_ts", 0)
    }
    if _active_cooldown:
        log(f"[cooldown] 冷静期中的币种: {list(_active_cooldown.keys())}")

    # ── 仓位状态检查（新规则：多空分开 + 总名义仓位上限）──────────────────────
    ps          = _get_position_state()
    active_syms = ps["active_syms"]
    log(
        f"[scan] 持仓 {ps['count']}/{MAX_POSITIONS} "
        f"（多{ps['long_count']}/{MAX_LONG} 空{ps['short_count']}/{MAX_SHORT}）"
        f" | 已用名义={ps['used_notional']:.0f}U / {MAX_NOTIONAL}U"
        f" | 剩余={ps['remaining_notional']:.0f}U"
    )

    # 总仓位满 → 撤所有开仓挂单，等待释放
    if ps["count"] >= MAX_POSITIONS or ps["remaining_notional"] < POS_MIN_U:
        cancelled = _cancel_all_entry_orders()
        msg = (
            f"⏸ Scanner: 仓位已满（{ps['count']}/{MAX_POSITIONS}）"
            f"或余额不足（剩余{ps['remaining_notional']:.0f}U < {POS_MIN_U}U），"
            f"已撤销 {cancelled} 个开仓挂单，等待仓位释放后再开。"
        )
        log(msg)
        notify(msg)
        return

    # ── 保证金使用率检查：使用率≥ 75% 硬止开仓，60-75% 缩小仓位 ──
    _margin_scale = 1.0   # 默认不缩
    try:
        import sys as _msys, pathlib as _mpl
        _msys.path.insert(0, str(_mpl.Path(__file__).parent))
        from risk_manager import check_margin_safety as _cms
        _mok, _mreason, _mstats = _cms(notifier_fn=notify)
        if not _mok:
            log(f"[scan] 保证金使用率过高，本轮跳过开仓: {_mreason}")
            return
        _margin_scale = _mstats.get("position_scale", 1.0)
        if _margin_scale < 1.0:
            log(f"[scan] 保证金偏紧 {_mstats.get('margin_usage_pct',0):.1f}%，新开仓仓位缩小至 {_margin_scale*100:.0f}%")
    except Exception as _me:
        log(f"[scan] 保证金检查失败，跳过: {_me}")

    # ── 市场环境获取 + 字段标准化 ──────────────────────────────────────────
    try:
        _mctx = _get_market_ctx()
        # market_context 返回 btc_trend="bull"/"bear"/"neutral"
        # _calc_position_usdt 期望 "up"/"down"/"" → 统一在此转换
        _raw_trend = _mctx.get("btc_trend", "")
        _raw_sent  = _mctx.get("sentiment", "")
        _mctx["btc_trend"] = "up" if _raw_trend in ("bull", "strong_bull") else (
                              "down" if _raw_trend in ("bear", "strong_bear") else "")
        # sentiment 值对齐：strong_bull/bull→greedy, bear/strong_bear→fearful
        _mctx["sentiment"] = "greedy" if _raw_sent in ("strong_bull", "bull") else (
                              "fearful" if _raw_sent in ("bear", "strong_bear") else "neutral")
        log(f"[scan] 市场环境: btc_trend={_mctx['btc_trend']}({_raw_trend}) "
            f"sentiment={_mctx['sentiment']}({_raw_sent}) "
            f"fg={_mctx.get('fear_greed',{}).get('value','?')}")
    except Exception as _e:
        log(f"[scan] market_context 获取失败: {_e}")
        _mctx = {}

    # ── 批量获取全市场 24h Ticker + 资金费率（一次请求）─────────────────────
    try:
        all_tickers_raw = api.request(
            "GET", f"{api.BASE_FAPI}/fapi/v1/ticker/24hr", {}
        )
        if not isinstance(all_tickers_raw, list):
            raise ValueError(f"ticker list: {type(all_tickers_raw)}")
        tickers = {
            t["symbol"]: t for t in all_tickers_raw
            if t["symbol"].endswith("USDT")
        }
    except Exception as e:
        log(f"[scan_err] ticker获取失败: {e}，5s后重试")
        try:
            import time as _t; _t.sleep(5)
            all_tickers_raw = api.get_ticker_24h()
            if not isinstance(all_tickers_raw, list):
                raise ValueError(f"ticker list: {type(all_tickers_raw)}")
            tickers = {
                t["symbol"]: t for t in all_tickers_raw
                if t["symbol"].endswith("USDT")
            }
            log("[scan_retry] ticker重试成功")
        except Exception as e2:
            log(f"[scan_err] ticker重试失败: {e2}")
            notify(f"⚠️ Scanner 行情获取连续失败，本轮扫描已跳过\n  原因: {e2}")
            return

    # 批量资金费率（premiumIndex 不传 symbol = 全市场）
    funding_map = {}
    try:
        marks = api.get_mark_price()   # list
        if isinstance(marks, list):
            for m in marks:
                funding_map[m["symbol"]] = float(m.get("lastFundingRate", 0))
    except Exception:
        pass

    # ── 扫描池构建：成交量 Top60 + 涨幅 Top20 + 跌幅 Top20 + 热度榜 Top10，合并去重 ──────────
    _all_usdt = [
        s for s in tickers.keys()
        if float(tickers[s].get("quoteVolume", 0)) >= MIN_VOL_USD
    ]
    # 池一：成交量前100（原有）
    _vol_top = sorted(_all_usdt,
        key=lambda s: float(tickers[s].get("quoteVolume", 0)), reverse=True
    )[:SCAN_TOP_N]
    # 池二：涨幅前20（捕捉趋势延续做多 + 超涨做空机会）
    _gain_top20 = sorted(_all_usdt,
        key=lambda s: float(tickers[s].get("priceChangePercent", 0)), reverse=True
    )[:20]
    # 池三：跌幅前20（捕捉趋势延续做空 + 超跌反弹做多机会）
    _drop_top20 = sorted(_all_usdt,
        key=lambda s: float(tickers[s].get("priceChangePercent", 0))
    )[:20]
    # 池四：热度榜 Top10（Binance Pro hotrank，捕捉社区关注标的）
    _hot_syms = []
    try:
        import subprocess as _sub
        _hotrank = _sub.run(
            ["binance-pro-cli", "search", "hotrank", "futures", "--limit", "10"],
            capture_output=True, text=True, timeout=10
        )
        if _hotrank.returncode == 0:
            import json as _j
            _hot_data = _j.loads(_hotrank.stdout)
            _hot_syms = [
                item.get("symbol", "").upper()
                for item in (_hot_data.get("data", []) if isinstance(_hot_data, dict) else [])
                if item.get("symbol", "").endswith("USDT")
            ]
            log(f"[hotrank] 热度榜：{_hot_syms}")
        else:
            log(f"[hotrank] 调用失败：{_hotrank.stderr[:100]}")
    except Exception as _he:
        log(f"[hotrank_err] {_he}")
        _hot_syms = []

    # 合并去重，保持顺序（成交量池优先，然后涨幅/跌幅/热度）
    _seen = set(_vol_top)
    _extra = []
    for s in (_gain_top20 + _drop_top20 + _hot_syms):
        if s not in _seen:
            _seen.add(s)
            _extra.append(s)
    top_syms = _vol_top + _extra
    log(f"[scan] 扫描池: 成交量{len(_vol_top)} + 涨跌幅新增{len(_extra)}个（涨幅20+跌幅20去重后）= {len(top_syms)}个")
    top_syms = [s for s in top_syms if s not in active_syms]
    log(f"[scan] Top候选（去持仓+流动性过滤）共 {len(top_syms)} 个")

    # ── 模拟模式：无真实挂单，existing_orders 始终为空 ─────────────────────────────
    existing_orders: dict = {}   # 模拟模式：永远为空，不查真实挂单
    log("[paper] 模拟模式：跳过真实挂单查询")

    # ── 第7项补充二：资金费率极端强制空单候选 ──────────────────────────────
    _forced_short_syms = []
    for _fs in list(tickers.keys()):
        _fr_val = funding_map.get(_fs, 0.0)
        _fvol = float(tickers[_fs].get("quoteVolume", 0))
        if _fr_val > 0.003 and _fvol >= MIN_VOL_USD and _fs not in active_syms:
            _forced_short_syms.append(_fs)
    if _forced_short_syms:
        log(f"[funding_force] 资金费率极端标的: {_forced_short_syms[:10]}")
        _forced_new = [s for s in _forced_short_syms if s not in top_syms]
        top_syms = _forced_new + top_syms
    # ─────────────────────────────────────────────────────────────────────────

    # ── 追涨模块（与 paper_scanner 对齐）────────────────────────────────────
    SURGE_MIN_GAIN = 20.0; SURGE_POS_USDT = 100.0  # 固定100U（纯信号测试工作流统一100U）
    SURGE_SL_PCT   = 0.05; SURGE_TRAIL_PCT = 0.10
    SURGE_RSI_MIN  = 50;   SURGE_RSI_MAX   = 78
    SURGE_VOL_RATIO = 1.8
    _btc_now = _mctx.get("btc_trend", "")
    if _btc_now != "down":
        _surge_state = _load_paper_state()
        _surge_candidates = sorted(
            [s for s, t in tickers.items()
             if float(t.get("priceChangePercent", 0)) >= SURGE_MIN_GAIN
             and s not in active_syms
             and s not in _surge_state.get("positions", {})
             and s not in _surge_state.get("pending_orders", {})
             and s.endswith("USDT")],
            key=lambda s: float(tickers[s].get("priceChangePercent", 0)), reverse=True
        )
        _surge_opened = 0
        for _ssym in _surge_candidates[:5]:
            if _surge_opened >= 1: break
            if len(_surge_state.get("positions", {})) + len(_surge_state.get("pending_orders", {})) >= MAX_POSITIONS: break
            try:
                _sk1h  = api.get_klines(_ssym, "1h", 25)
                _smark = float(api.get_mark_price(_ssym)["markPrice"])
                _sc    = [float(k[4]) for k in _sk1h]
                _srsi  = rsi_calc(_sc, 14)
                if not (SURGE_RSI_MIN <= _srsi <= SURGE_RSI_MAX): continue
                if not (float(_sk1h[-1][4]) > float(_sk1h[-1][1])): continue  # 1h仍涨
                _svols = [float(k[5]) for k in _sk1h]
                _svol_avg = sum(_svols[-21:-1]) / 20 if len(_svols) >= 21 else sum(_svols[:-1]) / max(1, len(_svols)-1)
                _svol_ratio = _svols[-1] / (_svol_avg + 1e-9)
                if _svol_ratio < SURGE_VOL_RATIO: continue
                _schg = float(tickers[_ssym].get("priceChangePercent", 0))
                if _schg > 50 and _svol_ratio < 5.0: continue
                _ssl  = round(_smark * (1 - SURGE_SL_PCT), 6)
                _stp1 = round(_smark * (1 + SURGE_TRAIL_PCT), 6)
                _stp2 = round(_smark * 1.25, 6)
                _surge_state["positions"][_ssym] = {
                    "direction": "LONG", "entry": _smark, "entry_price": _smark,
                    "pos_usdt": SURGE_POS_USDT, "sl_price": _ssl,
                    "tp1_price": _stp1, "tp2_price": _stp2,
                    "trailing_sl": None, "tp1_hit": False,
                    "open_ts": time.time(), "score": 999,
                    "sig_type": "surge_chase", "btc_trend": _btc_now,
                    "surge_gain": _schg, "surge_vol_ratio": round(_svol_ratio, 2),
                    "surge_rsi": round(_srsi, 1), "order_mode": "market",
                }
                _save_paper_state(_surge_state)
                _surge_opened += 1
                log(f"[surge_open] ✅ {_ssym} 涨幅+{_schg:.1f}% RSI={_srsi:.1f} vol={_svol_ratio:.1f}x entry={_smark:.6g}")
            except Exception as _se:
                log(f"[surge_err] {_ssym}: {_se}")
    # ── 追涨模块结束 ─────────────────────────────────────────────────────────

    # ── 第一阶段：快速评分（不调 Coinglass）── 并发化优化 ─────────────────
    # 原来串行 80 个 × 2.3s/个 ≈ 3min，并发后 ~10s
    from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed

    def _score_one(sym):
        funding = funding_map.get(sym, 0.0)
        return sym, *score_symbol_fast(sym, tickers[sym], funding,
                                       btc_trend=_mctx.get("btc_trend", ""))

    pre_results = []
    _stage1_start = time.time()
    with ThreadPoolExecutor(max_workers=20) as _ex:
        _futures = {_ex.submit(_score_one, sym): sym for sym in top_syms}
        for _fut in _as_completed(_futures):
            try:
                sym, fs, direction, details = _fut.result()
                if fs >= 50:   # 初筛阈值：低于 50 直接跳过，减少无效 CG API 调用
                    pre_results.append((sym, fs, direction, details))
            except Exception as _fe:
                log(f"[score_fast_err] {_futures[_fut]}: {_fe}")

    pre_results.sort(key=lambda x: x[1], reverse=True)
    log(f"[scan] 第一阶段通过 {len(pre_results)} 个（>=50分），耗时 {time.time()-_stage1_start:.1f}s")

    # ── 第二阶段：Top10 调 CG 深度评分，并发化（原来串行 10次×1-2s=10-20s）─
    CG_CALL_LIMIT = 10  # 每轮最多对 Top10 做新 CG 请求，其余用缓存
    _stage2_start = time.time()

    # 先区分哪些走真实 CG，哪些只用缓存
    _now_ts = time.time()
    _cg_new   = []   # 需要新请求 CG 的 (sym, fs, dir, det)
    _cg_cache = []   # 命中缓存的
    _cg_skip  = []   # 超出限制跳过的

    for item in pre_results:
        sym_i, fs_i, dir_i, det_i = item
        if sym_i in _CG_CACHE and (_now_ts - _CG_CACHE[sym_i]["ts"]) < _CG_CACHE_TTL:
            _cg_cache.append(item)
        elif len(_cg_new) < CG_CALL_LIMIT:
            _cg_new.append(item)
        else:
            _cg_skip.append(item)

    # 并发请求 CG（最多 CG_CALL_LIMIT 个，5 workers 避免触发限速）
    _cg_bonus_map: dict = {}

    def _fetch_cg(item):
        sym_i, fs_i, dir_i, det_i = item
        mark_i = float(tickers[sym_i].get("lastPrice", 0))
        return sym_i, _cg_get_bonus_cached(sym_i, dir_i, mark_i, det_i)

    with ThreadPoolExecutor(max_workers=5) as _ex2:
        for sym_i, bonus_i in _ex2.map(_fetch_cg, _cg_new + _cg_cache):
            _cg_bonus_map[sym_i] = bonus_i

    results = []
    for sym, fs, direction, details in pre_results:
        mark = float(tickers[sym].get("lastPrice", 0))
        bonus = _cg_bonus_map.get(sym, 0)
        final = min(100, fs + bonus)
        details["final_score"] = final
        results.append((sym, final, direction, details))
        if bonus:
            log(f"[score_cg] {sym} {fs:.0f}+{bonus} => {final:.0f}")

    log(f"[scan] 第二阶段 CG 完成，耗时 {time.time()-_stage2_start:.1f}s（新请求{len(_cg_new)}个，缓存{len(_cg_cache)}个，跳过{len(_cg_skip)}个）")

    results.sort(key=lambda x: x[1], reverse=True)

    # ── Q3: 清算区检测（只对评分>=60的标的，节省API配额）────────────────────
    _liq_start = time.time()
    _liq_count = 0
    for sym, final, direction, details in results:
        if final < SCORE_WATCH:
            break  # 已按分数降序排列，<60就全部跳过
        mark = float(tickers[sym].get("lastPrice", 0))
        if mark <= 0:
            continue
        try:
            # 动态搜索范围
            from market_filter import calc_dynamic_range as _cdr
            atr_tmp = float(details.get("atr", mark * 0.02))
            bb_w    = float(details.get("bb_width_pct", 8.0))
            vr      = float(details.get("vol_ratio", 1.0))
            dyn_range = _cdr(atr_tmp, mark, bb_w, vr, sym)
            # 查清算区
            liq_info = _cg.get_liquidation_zone(
                symbol=sym, price=mark, direction=direction,
                search_range_pct=dyn_range, min_volume=500_000,
            )
            details.update(liq_info)  # 写入 has_liq_zone / liq_zone_price / liq_zone_volume
            _liq_count += 1
        except Exception as _le:
            details["has_liq_zone"] = False
    log(f"[scan] Q3 清算区检测完成，共 {_liq_count} 个标的，耗时 {time.time()-_liq_start:.1f}s")

    qualified = [(s, f, d, det) for s, f, d, det in results if f >= SCORE_WATCH]
    log(f"[scan] 第二阶段 >=60分(WATCH)共 {len(qualified)} 个，其中>=70(AUTO)={sum(1 for _,f,_,_ in results if f>=SCORE_AUTO)} 个，>=80(FIRE)={sum(1 for _,f,_,_ in results if f>=SCORE_FIRE_HIGH)} 个")

    # ── 低量比潜力标的：进观察区等放量 ─────────────────────────────────────────
    # 条件：初筛分 ≥50 但量比 < 0.8（无量拉升但有技术结构），且接近关键阻力位（dist_high < 3%）
    # 策略：写入观察区但不挂单，等量比 > 1.2 时下一轮扫描自动触发
    _vol_watch_added = 0
    for sym, fs, direction, det in results:
        if fs < 50 or fs >= SCORE_WATCH:
            continue   # 已经进 qualified 或分太低，跳过
        vr_det   = det.get("vr", 1.0)
        abs_vol  = det.get("abs_vol_usdt", 0)  # calc_score_trend 填入
        chg_det  = det.get("chg24", 0)
        if abs_vol > 50_000_000:
            continue  # 绝对量大的不需要等放量
        if vr_det >= 0.8:
            continue  # 量比不够低，不是无量拉升
        if abs(chg_det) < 3:
            continue  # 涨跌幅不够，没有值得等待的动能
        # 接近高点（可能突破）：近3根1H最高价距现价 < 5%
        _wl_now = _load_watchlist()
        if sym in _wl_now:
            continue  # 已在观察区
        if sym in active_syms:
            continue  # 已有持仓
        add_to_watchlist(sym, direction, 0,  # target=0 表示等量比触发而非价格触发
                         fs, note=f"低量比等放量 vr={vr_det:.2f}x score={fs:.0f} {direction}")
        log(f"[vol_watch] {sym} 低量比({vr_det:.2f}x)进观察区等放量 score={fs:.0f}")
        _vol_watch_added += 1
        if _vol_watch_added >= 5:
            break  # 每轮最多加5个，避免观察区膨胀
    if _vol_watch_added:
        log(f"[scan] 低量比等放量观察区新增 {_vol_watch_added} 个")

    # ── WAIT 主动触发 + 超时清理 ─────────────────────────────────────────────
    # 观察区条目主动检查：量比突然放大 → 触发挂单；超时2h且评分跌破60 → 移出
    _wl_now = _load_watchlist()
    _wl_changed = False
    _wl_to_remove = []
    _wait_scan_now = int(time.time())

    for _ws, _we in list(_wl_now.items()):
        if not isinstance(_we, dict):
            continue
        _sig_type_wl = _we.get("sig_type", "")
        if _sig_type_wl.startswith("pin"):
            continue   # PIN信号由 ws_monitor 单独处理，跳过
        if _ws in active_syms:
            continue   # 已有持仓，跳过

        _added_ts = _we.get("added_at_ts", 0)
        if not _added_ts:
            try:
                from datetime import datetime as _dt, timezone as _tz
                _added_ts = _dt.fromisoformat(_we.get("added_at","")).timestamp()
            except Exception:
                _added_ts = _wait_scan_now - 3600  # 无法解析时默认1h前

        _age_sec  = _wait_scan_now - _added_ts
        _wl_score = float(_we.get("score", 0))
        _wl_dir   = "LONG" if _we.get("direction","") == "below" else "SHORT"

        # 超时清理：观察区超2h且重评分数跌破60，移出
        if _age_sec > 7200:
            _tk_wl = tickers.get(_ws)
            if _tk_wl:
                _fund_wl = funding_map.get(_ws, 0.0)
                _fs_new, _dir_new, _det_new = score_symbol_fast(_ws, _tk_wl, _fund_wl,
                                                                  btc_trend=_mctx.get("btc_trend",""))
                if _fs_new < SCORE_WATCH:
                    _wl_to_remove.append(_ws)
                    log(f"[wl_expire] {_ws} 观察区超时2h且重评{_fs_new:.0f}<60，移出")
                    continue
                else:
                    # 更新分数，延续等待
                    _wl_now[_ws]["score"] = _fs_new
                    _wl_now[_ws]["added_at_ts"] = _wait_scan_now  # 重置计时
                    _wl_changed = True
                    log(f"[wl_renew] {_ws} 超时重评{_fs_new:.0f}≥60，继续等待")

        # 多单WAIT主动触发：量比从低位突然放大 + 价格突破近期高点
        if _wl_dir == "LONG" and _wl_score < SCORE_AUTO:
            _tk_wl = tickers.get(_ws)
            if _tk_wl:
                _k1h_wl = api.get_klines(_ws, "1h", 25)
                if _k1h_wl and len(_k1h_wl) >= 5:
                    _v1h_wl = [float(k[5]) for k in _k1h_wl]
                    _vb_wl  = sum(_v1h_wl[-20:-3])/17 if len(_v1h_wl)>=20 else sum(_v1h_wl)/len(_v1h_wl)
                    _vr_now = (sum(_v1h_wl[-3:])/3) / _vb_wl if _vb_wl > 0 else 1
                    _vr_prev= sum(_v1h_wl[-6:-3])/3 / _vb_wl if _vb_wl > 0 else 1
                    _c1h_wl = [float(k[4]) for k in _k1h_wl]
                    _h20_wl = max(float(k[2]) for k in _k1h_wl[-20:])
                    _price_wl = float(_tk_wl.get("lastPrice", _c1h_wl[-1]))
                    vol_surge = _vr_prev < 0.8 and _vr_now > 1.5
                    price_break = _price_wl >= _h20_wl * 0.998
                    # 检查是否有多单槽位（has_slot在外层循环中定义，这里实时判断当前槽位）
                    _ps_tmp = _get_position_state()
                    _has_long_slot_wl = _ps_tmp["long_slots"] > 0
                    if vol_surge and price_break and _has_long_slot_wl:
                        log(f"[wait_trigger] {_ws} 量比放大({_vr_prev:.2f}→{_vr_now:.2f})且突破高点，触发CHASE挂单")
                        _wl_to_remove.append(_ws)  # 移出观察区，交给下轮完整流程处理
                        _wl_changed = True

    for _rm in _wl_to_remove:
        _wl_now.pop(_rm, None)
        _wl_changed = True

    if _wl_changed:
        try:
            import pathlib as _pathlib
            _wl_file = _pathlib.Path(f"{WORKSPACE}/watchlist.json")
            _tmp_wl  = _wl_file.with_suffix(".tmp")
            import json as _jj
            _tmp_wl.write_text(_jj.dumps(_wl_now, indent=2, ensure_ascii=False))
            _tmp_wl.replace(_wl_file)
        except Exception as _e_wl:
            log(f"[wl_update_err] {_e_wl}")

    # ── 分数下降撤单：保护期逻辑 ────────────────────────────────────────────────
    # 撤单规则：
    #   1. 评分跌破 60（保护期内 或 保护期外）→ 撤单  [Fix P0-1: 统一为60]
    #   2. BTC 5分钟跌幅 > 3% → 全部强制撤单
    #   3. 挂单超过 4 小时未成交 → 撤单（过期信号）
    #   保护期：首次 >=80 分后 2 小时内，60~79 分不撤单（对齐 dual_net）
    PROTECT_WINDOW      = 2 * 3600   # 2小时保护期
    PROTECT_MIN_SCORE  = 60         # 保护期内最低分
    SCORE_80           = 80         # 触发保护期的门槛
    ORDER_EXPIRE_SEC   = 4 * 3600   # 挂单有效期 4 小时
    BTC_MOVE_THRESHOLD = 0.03       # BTC 波动 >=3% 触发方向性撤单
    # P4-2: 撤单冷静期设计
    # 挂单后30分钟内撤单门槛降至50（不因短期评分波动撤单）
    CANCEL_QUIET_MIN    = 30 * 60   # 30分钟冷静期
    CANCEL_QUIET_SCORE  = 50        # 冷静期内撤单门槛：评分<50才撤
    CANCEL_NORMAL_SCORE = 60        # 冷静期外撤单门槛：评分<60才撤

    score_map = {s: f for s, f, d, det in results}
    dir_map   = {s: d for s, f, d, det in results}
    high_score_syms = {s for s, f, d, det in qualified if f >= SCORE_AUTO}
    cancelled_orders = []

    # 获取 BTC 5m 涨跌幅（正为涨，负为跌）
    btc_chg_5m = 0.0
    try:
        btc_klines = api.get_klines("BTCUSDT", "5m", 2)
        if len(btc_klines) >= 2:
            btc_prev   = float(btc_klines[-2][4])
            btc_cur    = float(btc_klines[-1][4])
            btc_chg_5m = (btc_cur - btc_prev) / btc_prev if btc_prev > 0 else 0
            if abs(btc_chg_5m) >= BTC_MOVE_THRESHOLD:
                log(f"[protect] BTC 5m 波动 {btc_chg_5m*100:.1f}%，启动方向性撤单模式")
    except Exception as e:
        log(f"[protect] BTC 波动检测异常: {e}")

    wl_state = _load_watchlist()

    for sym_ex, orders_ex in existing_orders.items():
        if sym_ex in active_syms:
            continue
        cur_score = score_map.get(sym_ex, 0)

        # 更新 high80_ts
        if cur_score >= SCORE_80:
            if sym_ex not in wl_state or not isinstance(wl_state.get(sym_ex), dict):
                wl_state[sym_ex] = {}
            if not wl_state[sym_ex].get("high80_ts"):
                wl_state[sym_ex]["high80_ts"] = time.time()
                log(f"[protect] {sym_ex} 首次达到 {cur_score:.0f}≥80，保护期开始")

        high80_ts  = (wl_state.get(sym_ex) or {}).get("high80_ts", 0)
        in_protect = bool(high80_ts and (time.time() - high80_ts) < PROTECT_WINDOW)

        # 检查挂单是否超时（取最早一笔挂单的时间）
        order_expired = False
        if orders_ex:
            oldest_ms = min(int(o.get("time", time.time()*1000)) for o in orders_ex)
            order_age  = time.time() - oldest_ms / 1000
            if order_age > ORDER_EXPIRE_SEC:
                order_expired = True
                log(f"[protect] {sym_ex} 挂单已超时 {order_age/3600:.1f}h > {ORDER_EXPIRE_SEC//3600}h")

        # 获取本标的挂单方向（从 orders_ex 的 side 字段判断）
        # BUY 挂单 = LONG 方向；SELL 挂单 = SHORT 方向
        if orders_ex:
            first_side = orders_ex[0].get("side", "")
            order_dir  = "LONG" if first_side == "BUY" else "SHORT"
        else:
            order_dir  = dir_map.get(sym_ex, "")

        # BTC 波动 >=3%：只撤不利方向的挂单，保留有利方向
        btc_cancel = False
        if abs(btc_chg_5m) >= BTC_MOVE_THRESHOLD:
            if btc_chg_5m < 0 and order_dir == "LONG":
                btc_cancel = True
                log(f"[protect] {sym_ex} BTC下跌{btc_chg_5m*100:.1f}%，撤多单")
            elif btc_chg_5m > 0 and order_dir == "SHORT":
                btc_cancel = True
                log(f"[protect] {sym_ex} BTC上涨{btc_chg_5m*100:.1f}%，撤空单")
            # BTC 走势与挂单方向一致 → 不撤，保留有利挂单

        force_reason = ""
        if btc_cancel:
            force_reason = f"BTC波动{btc_chg_5m*100:.1f}%不利{order_dir}"
        elif order_expired:
            force_reason = f"挂单超时{order_age/3600:.1f}h"

        if force_reason:
            for o in orders_ex:
                try:
                    # 模拟模式：不真实撤单，只记录
                    cancelled_orders.append((sym_ex, force_reason, cur_score))
                    log(f"[paper] 模拟撤单 {sym_ex}（{force_reason}）")
                    if sym_ex in wl_state and isinstance(wl_state[sym_ex], dict):
                        wl_state[sym_ex].pop("high80_ts", None)
                except Exception as e:
                    log(f"[scan] 撤单失败 {sym_ex}: {e}")
            continue

        # Fix P0-1: 保护期内外统一用 60 分作为保留线（原保护期外用70，现改为60）
        # P4-2: 冷静期内降低撤单门槛
        oldest_ms   = min(int(o.get("time", time.time()*1000)) for o in orders_ex) if orders_ex else 0
        order_age   = time.time() - oldest_ms / 1000 if oldest_ms else 0
        in_quiet    = order_age < CANCEL_QUIET_MIN  # 才挂单将30分钟
        cancel_thr  = CANCEL_QUIET_SCORE if in_quiet else CANCEL_NORMAL_SCORE

        # Fix P0-1: 评分 >= cancel_thr，保留挂单
        if cur_score >= cancel_thr:
            if sym_ex not in high_score_syms:
                _period = '冷静期' if in_quiet else '常规'
                log(f"[protect] {sym_ex} score={cur_score:.0f}>={cancel_thr}({_period})，保留挂单")
                high_score_syms.add(sym_ex)
            continue

        if in_protect and cur_score >= PROTECT_MIN_SCORE:
            log(f"[protect] {sym_ex} 保护期内 score={cur_score:.0f}，保留挂单")
            high_score_syms.add(sym_ex)
            continue

        # 常规撤单
        _cp = '冷静期' if in_quiet else '常规'
        reason = f"{_cp}分数{cur_score:.0f}<{cancel_thr}"
        for o in orders_ex:
            try:
                # 模拟模式：不真实撤单，只记录
                cancelled_orders.append((sym_ex, reason, cur_score))
                log(f"[paper] 模拟撤单 {sym_ex}（{reason}，score={cur_score:.0f}）")
                if sym_ex in wl_state and isinstance(wl_state[sym_ex], dict):
                    wl_state[sym_ex].pop("high80_ts", None)
            except Exception as e:
                log(f"[scan] 撤单失败 {sym_ex}: {e}")

    _save_watchlist(wl_state)

    # ── 决策：高分(>=70) 判断是否挂单，其余只写观察区不播报 ──────────────────
    auto_entries  = []   # 真正挂了单的
    watch_high    = []   # >=70 但无槽位，播报给你参考
    cur_long_slots   = ps["long_slots"]
    cur_short_slots  = ps["short_slots"]
    remaining_budget = ps["remaining_notional"]
    pending_usdt     = 0.0
    new_opened_this_scan = 0   # 本轮已新开仓计数

    for sym, fs, direction, details in qualified:
        try:
            mark = float(tickers[sym].get("lastPrice", 0))
            # ── 注入市场环境字段，供 _calc_position_usdt 的乘数逻辑使用 ──
            details.setdefault("btc_trend", _mctx.get("btc_trend", ""))
            details.setdefault("sentiment", _mctx.get("sentiment", "neutral"))
            details.setdefault("sig_type",  details.get("signal_type", "trend"))

            # P2: 环境过滤（修正评分）
            funding_pct = float(details.get("funding", 0)) * 100
            fs_filtered = _market_filter(
                sym=sym, direction=direction, score=fs,
                funding_pct=funding_pct,
                btc_trend=details.get("btc_trend", ""),
            )
            if fs_filtered <= -999:
                log(f"[scan] {sym} 环境过滤拦截（资金费率结算窗口/FGI极端）")
                continue
            fs = fs_filtered  # 使用修正后评分

            # P1: 统一仓位计算
            scenario = 4 if (fs < 80 and details.get("has_liq_zone")) else 0
            details["scenario"] = scenario
            # 注入当前净值供动态仓位上限计算
            try:
                _sim_realized = sum(t.get("pnl_u", t.get("pnl", 0)) for t in state.get("history", []))
                _sim_open_pnl = sum(
                    (float(__import__('binance_api').get_mark_price(s)["markPrice"]) - p.get("entry", 0))
                    * p.get("pos_usdt", 100) / p.get("entry", 1)
                    for s, p in state.get("positions", {}).items()
                    if p.get("entry", 0) > 0
                ) if state.get("positions") else 0
                details["net_value"] = max(200.0, 1000 + _sim_realized + _sim_open_pnl)
            except Exception:
                details["net_value"] = 1000.0

            # 固定 100U 模拟盘：直接开仓
            pos_usdt = _calc_pos_size(
                sig_type=details.get("sig_type", "trend"),
                score=fs,
                direction=direction,
                details=details,
            )
            # 保证金偏紧时自动缩小仓位
            if _margin_scale < 1.0:
                pos_usdt = round(pos_usdt * _margin_scale, 1)
            pos_usdt = min(pos_usdt, remaining_budget - pending_usdt)
            if pos_usdt < POS_MIN_U:
                pos_usdt = 0

            has_long_slot  = cur_long_slots > 0  and direction == "LONG"
            has_short_slot = cur_short_slots > 0 and direction == "SHORT"
            has_slot       = has_long_slot or has_short_slot
            # Fix P0-3: 冷静期过滤（止损后同方向禁止重开）
            _in_cooldown = sym in _active_cooldown and _active_cooldown[sym].get("direction") == direction
            if _in_cooldown:
                log(f"[cooldown] {sym} {direction} 冷静期中（到 {_active_cooldown[sym].get('expire_str','?')}），跳过")
            # ≥80: 强信号直接开; ≥70: 候选信号也尝试开（calc_entry_mode会判断WAIT拦截偏差过大的）
            can_open       = fs >= SCORE_AUTO and has_slot and pos_usdt >= POS_MIN_U and not _in_cooldown
            high_no_slot   = fs >= SCORE_AUTO and not has_slot

            # 写入观察区（低于开仓标准的不播报，只静默记录）
            try:
                k1h_wl = api.get_klines(sym, "1h", 30)
                atr_wl = atr_calc(k1h_wl)
                target_px = round(mark - atr_wl * 0.3, 6) if direction == "LONG" else round(mark + atr_wl * 0.3, 6)
                add_to_watchlist(sym, direction, target_px, fs,
                                 f"score={fs:.0f} {direction}")
            except Exception:
                target_px = 0

            if can_open:
                # ═══ 模拟模式：挂限价单（与真实工作流一致），不直接市价成交 ═══
                # 每轮扫描新开仓节流
                if new_opened_this_scan >= MAX_NEW_PER_SCAN:
                    log(f"[paper] 本轮已开 {new_opened_this_scan} 笔，达到上限 MAX_NEW_PER_SCAN={MAX_NEW_PER_SCAN}，跳过 {sym}")
                    continue
                # ── 优化②：BTC趋势明显时，反向开仓门槛提高到80 ───────────
                # BTC上涨明显(up) → 空单需>=80；BTC下跌明显(down) → 多单需>=80
                btc_trend_now = str(details.get("btc_trend", "")).lower()
                _btc_up   = "up"   in btc_trend_now
                _btc_down = "down" in btc_trend_now
                # P0优化：btc=up 时完全禁止 SHORT（数据显示 btc=up 时 SHORT 胜率 0%）
                if direction == "SHORT" and _btc_up:
                    log(f"[paper] {sym} btc=up 期间禁止开空，跳过")
                    continue
                # btc=down 时 LONG 门槛提至90（之前80不够，数据显示高分多单仍严重亏损）
                COUNTER_TREND_MIN = 90
                if direction == "LONG" and _btc_down and fs < COUNTER_TREND_MIN:
                    log(f"[paper] {sym} 多单评分{fs:.0f}<{COUNTER_TREND_MIN}（btc=down 逆势过滤），跳过")
                    continue

                # ── P0优化③：主流币禁止 trend LONG（数据显示全部以24h底安出局）──
                # 主流币低波动率 + 宽SL → 震荡磨损，没有趋势动能，不适合 trend 多单
                # 只保留空单（可做trend_short）和 reversal 信号
                _sig_type_now = str(details.get("sig_type", "") or details.get("signal_type", "")).lower()
                if direction == "LONG" and sym in CLUSTER_MAP.get("major", set()) and "trend" in _sig_type_now and "reversal" not in _sig_type_now:
                    log(f"[paper] {sym} 主流币 trend LONG 过滤（数据显示100%以24h底安出局），跳过")
                    continue

                # ── P0优化④：surge_chase 贪婪情绪禁止追涨 ──────────────────
                # 数据显示 surge_chase LONG 在贪婪情绪下胜率25%，全部亏损
                # 贪婪情绪下 surge = 末端拉升，追涨即入陷阱
                _surge_sig = "surge" in _sig_type_now
                _cur_sentiment = str(details.get("sentiment", "")).lower()
                if direction == "LONG" and _surge_sig and _cur_sentiment == "greedy":
                    log(f"[paper] {sym} surge_chase 贪婪情绪禁止追涨多单，跳过")
                    continue

                # ── P0 相关性过滤：同集群多单不超过 CLUSTER_LONG_MAX ──
                _ps_state      = _load_paper_state()
                _cur_positions = _ps_state.get("positions", {})
                _cur_pending   = _ps_state.get("pending_orders", {})
                _corr_ok, _corr_reason = _check_correlation(sym, direction, _cur_positions, _cur_pending)
                if not _corr_ok:
                    log(f"[paper] {sym} 相关性过滤：{_corr_reason}，跳过")
                    continue

                # ── 合约安全审计：高风险/貔貅盘币一票否决 ──
                try:
                    from entry_logic import issuer_risk_check as _irc
                    _audit_veto, _audit_reason = _irc(sym)
                    if _audit_veto:
                        log(f"[paper] {sym} 安全审计一票否决: {_audit_reason}，跳过")
                        continue
                except Exception as _ae:
                    log(f"[paper] {sym} 安全审计异常({_ae})，放行")

                gate = _pre_order_gate(
                    sym=sym, direction=direction, score=fs,
                    funding_pct=funding_pct, pos_size_u=pos_usdt,
                    skip_smart_money=("token_report_score" in details),
                )
                if not gate:
                    log(f"[paper] {sym} pre_order_gate 拦截: {gate.reason}")
                    continue

                # 计算止盈止损
                try:
                    liq = liquidation_map(sym, direction)
                    sl_price  = liq.get("hard_sl",   mark * (0.94 if direction == "LONG" else 1.06))
                    tp1_price = liq.get("soft_tp1",  mark * (1.10 if direction == "LONG" else 0.90))
                    tp2_price = liq.get("soft_tp2",  mark * (1.18 if direction == "LONG" else 0.84))
                except Exception:
                    sl_price  = round(mark * (0.94 if direction == "LONG" else 1.06), 6)
                    tp1_price = round(mark * (1.10 if direction == "LONG" else 0.90), 6)
                    tp2_price = round(mark * (1.18 if direction == "LONG" else 0.84), 6)

                # ── SL计算：取清算图 和 ATR×1.5 中更宽的一个 ──────────────
                # 逻辑：先用清算图得到一个SL；然后算出1.5xATR的SL位；取更远的那个。
                # 这样确保SL永远不会小于1.5×ATR，不会被市场噪音轻易扫掉。
                try:
                    k1h_atr = api.get_klines(sym, "1h", 30)
                    atr_val  = atr_calc(k1h_atr)
                    atr_sl   = atr_val * 1.5
                    if direction == "LONG":
                        sl_atr   = mark - atr_sl              # ATR对应的SL位
                        sl_price = min(sl_price, sl_atr)      # 取更低（更宽）的SL
                    else:
                        sl_atr   = mark + atr_sl
                        sl_price = max(sl_price, sl_atr)      # 取更高（更宽）的SL
                    _sl_dist_pct_new = abs(mark - sl_price) / mark * 100
                    log(f"[paper_sl] {sym} sl={sl_price:.5g} dist={_sl_dist_pct_new:.2f}% atr={atr_val:.5g}")
                except Exception:
                    atr_val = None

                # ── TP1验证：清算图TP1为主，仅当盈亏比<1:1时才兑底 ──
                # 之前错误：_tp1_dist < 8%就覆盖，导致清算图TP1完全失效
                # 现在：只要盈亏比>=1:1就保留清算图TP1，不覆盖
                try:
                    _tp1_dist = abs(tp1_price - mark) / mark
                    _sl_dist  = abs(sl_price  - mark) / mark
                    # 只有清算图TP1 < SL距离时（盈亏比<1:1）才兑底调整
                    if _tp1_dist < _sl_dist:
                        _tp1_target = _sl_dist * 1.5  # 至少要到1.5倍盈亏比
                        if direction == "LONG":
                            tp1_price = round(mark * (1 + _tp1_target), 8)
                        else:
                            tp1_price = round(mark * (1 - _tp1_target), 8)
                        log(f"[paper_tp1_adj] {sym} 清算图TP1盈亏比不足，兑底调整 tp1={tp1_price:.5g}(盈亏比{_tp1_target/_sl_dist:.1f}:1)")
                except Exception:
                    pass

                # ── SL上限保护8%（防低价币异常宽止损）──────────────────────
                _SL_MAX = 0.08
                _sl_dist_now = abs(mark - sl_price) / (mark + 1e-9)
                if sl_price <= 0 or _sl_dist_now > _SL_MAX:
                    log(f"[paper] {sym} SL距离{_sl_dist_now*100:.1f}%>{_SL_MAX*100:.0f}%，压到上限")
                    sl_price = round(mark * (1 - _SL_MAX) if direction == "LONG" else mark * (1 + _SL_MAX), 6)

                # 计算限价入场位（与真实工作流完全一致）
                try:
                    k1h_lp = api.get_klines(sym, "1h", 30)
                    k4h_lp = api.get_klines(sym, "4h", 20)
                    details["pos_usdt"] = pos_usdt
                    limit_price = _calc_limit_price(sym, direction, mark, k1h_lp, k4h_lp, details)
                    if not limit_price or limit_price <= 0:
                        # 降级：用 ATR 回调法
                        try:
                            atr_fb = atr_calc(k1h_lp)
                            limit_price = round(mark - atr_fb * 0.5, 6) if direction == "LONG"                                           else round(mark + atr_fb * 0.5, 6)
                        except Exception:
                            limit_price = round(mark * (0.997 if direction == "LONG" else 1.003), 6)
                    log(f"[paper] {sym} 限价挂单 limit={limit_price:.6g} mark={mark:.6g}")
                except Exception as _lpe:
                    limit_price = round(mark * (0.997 if direction == "LONG" else 1.003), 6)
                    log(f"[paper] {sym} 限价计算失败降级: {_lpe}")

                # [v2.1] 信号过期过滤：市价比挂单价高超过3%说明入场时机已过，丢弃
                # 方向一致：LONG看市价>挂单价多少；SHORT看市价<挂单价多少
                _gap_pct_chk = (mark - limit_price) / limit_price * 100 if direction == "LONG"                                 else (limit_price - mark) / limit_price * 100
                if _gap_pct_chk > 3.0:
                    log(f"[skip_stale] {sym} 信号已过期: 市价{mark:.6g}比挂单价{limit_price:.6g}"
                        f"高{_gap_pct_chk:.2f}%>3%，放弃")
                    continue

                placed = _paper_place_limit_order(
                    sym=sym, direction=direction,
                    limit_price=limit_price, sl_price=sl_price,
                    tp1_price=tp1_price, tp2_price=tp2_price,
                    pos_usdt=pos_usdt,
                    sig_type=details.get("sig_type", details.get("signal_type", "trend")),
                    score=fs, details=details,
                )
                if placed:
                    pending_usdt += pos_usdt
                    new_opened_this_scan += 1
                    auto_entries.append({"sym": sym, "score": fs, "direction": direction,
                                         "limit_price": limit_price, "tp": tp1_price, "sl": sl_price,
                                         "notional": pos_usdt, "action": "paper_limit"})
                    cur_long_slots  -= (1 if direction == "LONG"  else 0)
                    cur_short_slots -= (1 if direction == "SHORT" else 0)
                else:
                    log(f"[paper] {sym} 模拟挂单被拒（仓位已满或重复）")


            elif high_no_slot:
                # 高分但无槽位，记录供播报
                watch_high.append({"sym": sym, "score": fs, "direction": direction,
                                    "target": target_px, "details": details})
        except Exception as _iter_e:
            log(f"[scan_iter_err] {sym} 决策层异常: {_iter_e}")
            import traceback as _tb
            log(_tb.format_exc()[:400])

    # ══════════════════════════════════════════════════════════════
    # 全流程结束后，发送一条汇总报告（不在流程中途发任何消息）
    # ══════════════════════════════════════════════════════════════
    elapsed = time.time() - start_ts

    # ── [模拟] 持仓简报 ──────────────────────────────────────────
    pos_section = []
    _paper_check_pending()  # 检查限价挂单是否成交或超时
    _paper_check_sl_tp()   # 检查持仓止盈止损
    try:
        _pstate = _load_paper_state()
        _ppos   = _pstate.get("positions", {})
        if _ppos:
            for pos_idx, (sym_p, p) in enumerate(_ppos.items(), 1):
                ep  = p["entry"]
                sl  = p.get("sl_price", 0)
                tp1 = p.get("tp1_price", 0)
                drx = p["direction"]
                pu  = p.get("pos_usdt", 0)
                try:
                    mk = float(api.get_mark_price(sym_p)["markPrice"])
                except Exception:
                    mk = ep
                if drx == "LONG":
                    pnl_pct = (mk - ep) / ep * 100
                else:
                    pnl_pct = (ep - mk) / ep * 100
                pnl_u = pu * pnl_pct / 100
                d   = "🟢 多头[模拟]" if drx == "LONG" else "🔴 空头[模拟]"
                pnl_s   = f"+{pnl_pct:.2f}%" if pnl_pct >= 0 else f"{pnl_pct:.2f}%"
                pnl_u_s = f"+{pnl_u:.1f}U"   if pnl_u   >= 0 else f"{pnl_u:.1f}U"
                pos_section += [
                    f"{pos_idx}. {d} {sym_p}",
                    f"入场价：{ep:.6g}",
                    f"现价：{mk:.6g}",
                    f"浮盈：{pnl_s}（{pnl_u_s}）",
                    f"名义：{pu:.0f}U  止损：{sl:.6g}  止盈TP1：{tp1:.6g}",
                ]
                if pos_idx < len(_ppos):
                    pos_section.append("")
        else:
            pos_section.append("  📭 无模拟持仓")
        # 追加挂单中的订单显示
        _ppending = _pstate.get("pending_orders", {})
        if _ppending:
            pos_section.append("")
            pos_section.append("📌 限价挂单中：")
            for sym_pd, pd in _ppending.items():
                try:
                    mk_pd = float(api.get_mark_price(sym_pd)["markPrice"])
                    dist  = (mk_pd - pd["limit_price"]) / mk_pd * 100  # 多单：正=还没触发
                except Exception:
                    mk_pd = 0; dist = 0
                dr_pd = "多" if pd["direction"] == "LONG" else "空"
                import datetime as _dt
                expire_left = max(0, int((pd.get("expire_ts", 0) - time.time()) / 60))
                pos_section.append(
                    f"  {sym_pd} {dr_pd} | 限价={pd['limit_price']:.6g} 现价={mk_pd:.6g}"
                    f" 距触发{dist:+.1f}% | 剩余{expire_left}min"
                )
    except Exception as e:
        pos_section.append(f"  持仓查询失败: {e}")

    # ── 挂单简报（高分，≥70分）────────────────────────────────────
    order_section = []
    # 模拟模式：paper_open 的记录在 auto_entries 里 action="paper_open"
    new_orders  = [e for e in auto_entries if e.get("action") in ("paper_open", "paper_limit")]
    keep_orders = []   # 模拟模式无持续挂单

    def _build_reason(e: dict) -> str:
        d   = e["details"]
        fr  = d.get("funding", 0)
        chg = d.get("chg24", 0)
        sig = d.get("sig_type", "")
        rsi1h = d.get("rsi_1h", 0)
        rsi4h = d.get("rsi_4h", 0)
        vr    = d.get("vr", 1.0)
        bb    = d.get("bb_pct", 50)
        ema1h = d.get("ema_1h", "")
        ema4h = d.get("ema_4h", "")
        oi_div = d.get("oi_divergence", False)
        top_div = d.get("top_divergence", False)
        trend_s  = d.get("trend_score", 0)
        moment_s = d.get("momentum_score", 0)
        vol_s    = d.get("volume_score", 0)
        emot_s   = d.get("emotion_score", 0)
        direction = e.get("direction", "LONG")

        parts: list[str] = []

        # 信号类型
        sig_label = {"trend": "趋势", "reversal": "反转"}.get(sig, sig)
        if sig_label: parts.append(f"信号类型: {sig_label}")

        # EMA 排列
        if ema4h or ema1h:
            ema_str = "/".join(x for x in [ema4h, ema1h] if x)
            parts.append(f"EMA排列: {ema_str}")

        # RSI
        rsi_parts = []
        if rsi4h:  rsi_parts.append(f"4H={rsi4h:.0f}")
        if rsi1h:  rsi_parts.append(f"1H={rsi1h:.0f}")
        if rsi_parts: parts.append(f"RSI {' '.join(rsi_parts)}")

        # 资金费率
        if abs(fr) >= 0.0003: parts.append(f"资金费率 {fr*100:+.3f}%")

        # 24h 涨跌
        if abs(chg) >= 1: parts.append(f"24h {chg:+.1f}%")

        # BB 位置
        if bb >= 85:   parts.append(f"布林带 {bb:.0f}%（顶部）")
        elif bb <= 15: parts.append(f"布林带 {bb:.0f}%（底部）")
        else:          parts.append(f"布林带 {bb:.0f}%")

        # 成交量
        if vr >= 1.5:    parts.append(f"量能放大 {vr:.1f}x")
        elif vr <= 0.6:  parts.append(f"量能萎缩 {vr:.1f}x")
        else:            parts.append(f"量能正常 {vr:.1f}x")

        # 背离
        if top_div: parts.append("顶背离")
        if oi_div:  parts.append("OI背离")

        # 分项得分（让用户看到哪项贡献最多）
        score_line = f"趋势{trend_s}+动量{moment_s}+量能{vol_s}+情绪{emot_s}"
        parts.append(f"分项: {score_line}")

        return "\n    ".join(parts) if parts else f"综合评分{e['score']:.0f}分"

    for e in new_orders:
        d  = "🟢多" if e.get("direction") == "LONG" else "🔴空"
        action_label = "限价挂单中" if e.get("action") == "paper_limit" else "模拟开仓"
        lp = e.get('limit_price', 0)
        sl = e.get('sl', 0)
        tp = e.get('tp', 0)
        order_section.append(
            f"{d} {e.get('sym','?')}｜[{action_label}]\n"
            f"   限价：{lp:.6g}｜仓位：{e.get('notional',0):.0f}U｜评分：{e.get('score',0):.0f}分\n"
            f"   止损：{sl:.6g}  止盈TP1：{tp:.6g}"
        )
    # 模拟模式无 keep_orders
    if not order_section:
        order_section.append("  无高分挂单（本轮最高分未达70）")

    # ── 模拟模式：无真实挂单，跳过实际挂单补充 ─────────────────────────
    # 模拟仓位已在上方 pos_section 展示，此处不需要补充真实挂单

    # ── 高分观察区 top3（本轮 watch_high + watchlist 历史）────────────────
    slot_section = []
    try:
        import json as _json
        wl_raw = _json.load(open(WATCHLIST_FILE)) if os.path.exists(WATCHLIST_FILE) else {}
        pending_set = set(existing_orders.keys())
        pos_set = set(_load_paper_state().get("positions", {}).keys())  # 模拟模式：从 paper_sim_state 取

        # 收集所有达标的（本轮扫描 + watchlist 历史）
        all_cands = {}  # sym -> (score, dir, target, hit_pending)
        for e in watch_high:
            all_cands[e["sym"]] = (e["score"], e["direction"], e.get("target",""), False)

        # watchlist 历史入池：对每个历史候选重新评分（防止分数冻结）
        WL_TTL = 24 * 3600  # watchlist 有效期 24小时
        _now_wl = time.time()
        _wl_stale = []
        _wl_updated = False
        for sym_w, v_w in list(wl_raw.items()):
            if sym_w in ("candidates","last_updated") or not isinstance(v_w, dict):
                continue
            if sym_w in pending_set or sym_w in pos_set:
                continue
            if sym_w in all_cands:  # 本轮已有新评分，跳过
                continue
            # TTL 检查
            added_ts = 0
            try:
                from datetime import datetime as _dt2
                added_ts = _dt2.fromisoformat(v_w["added_at"]).timestamp() if v_w.get("added_at") else 0
            except Exception:
                pass
            if added_ts > 0 and (_now_wl - added_ts) > WL_TTL:
                _wl_stale.append(sym_w)
                continue
            # 重新评分
            _new_score = 0
            _new_dir = "LONG" if "LONG" in v_w.get("note","") else "SHORT"
            if sym_w in tickers:
                try:
                    _fs_w, _dir_w, _ = score_symbol_fast(sym_w, tickers[sym_w], funding_map.get(sym_w, 0.0))
                    if _dir_w:
                        _new_dir = _dir_w
                    _new_score = _fs_w
                except Exception:
                    pass
            if _new_score < 10:  # 未扫到或异常，用旧分
                note_w = v_w.get("note","")
                for part in note_w.split():
                    if part.startswith("score="):
                        try: _new_score = float(part.split("=")[1])
                        except: pass
            if _new_score >= 70:
                all_cands[sym_w] = (_new_score, _new_dir, v_w.get("target","?"), False)
                # 分数变化 >=3 才回写，避免小波动不断触发写文件
                old_score = v_w.get("score", 0)
                if abs(_new_score - old_score) >= 3:
                    v_w["score"] = _new_score
                    v_w["note"] = f"score={_new_score:.0f} {_new_dir}"
                    v_w["added_at"] = datetime.now(timezone.utc).isoformat()
                    wl_raw[sym_w] = v_w
                    _wl_updated = True
        # 清除过期条目，加入撤单通知
        if _wl_stale:
            for _s in _wl_stale:
                wl_raw.pop(_s, None)
                cancelled_orders.append((_s, f"观察超过{WL_TTL//3600}小时无信号刷新，自动移除", 0))
            _save_watchlist(wl_raw)
            log(f"[watchlist_ttl] 清除过期({WL_TTL//3600}h): {_wl_stale}")
        elif _wl_updated:
            _save_watchlist(wl_raw)

        # 标记本轮触达目标价的
        try:
            import ws_monitor as _wsm
            hit_map = getattr(_wsm, "_wl_hit_pending", {})
            for sym_h in list(hit_map.keys()):
                if sym_h in all_cands:
                    sc, dr, tg, _ = all_cands[sym_h]
                    all_cands[sym_h] = (sc, dr, tg, True)
        except Exception:
            hit_map = {}

        # 排序取 top3
        sorted_cands = sorted(all_cands.items(), key=lambda x: -x[1][0])
        for idx, (sym_w, (score_w, dir_w, target_w, hit)) in enumerate(sorted_cands[:3], 1):
            d_icon = "🟢 多头" if dir_w == "LONG" else "🔴 空头"
            hit_tag = "｜❗已触达目标价" if hit else ""
            v_note = wl_raw.get(sym_w, {}).get("note","") if isinstance(wl_raw.get(sym_w), dict) else ""
            reason_parts = [p for p in v_note.split() if not p.startswith("score=") and p not in ("LONG","SHORT")]
            reason_str = " ".join(reason_parts[:8]) if reason_parts else f"综合评分{score_w:.0f}分"
            if not reason_str.strip():
                reason_str = f"综合评分{score_w:.0f}分"

            # ── 计算当前 WAIT 原因，告知为何尚未挂单 ──
            wait_reason = ""
            try:
                import requests as _req
                _mark = float(_req.get(
                    f"https://fapi.binance.com/fapi/v1/ticker/price",
                    params={"symbol": sym_w},
                    proxies={"http": api.PROXY_URL, "https": api.PROXY_URL},
                    timeout=4
                ).json()["price"])
                _k1h = api.get_klines(sym_w, "1h", 60)
                _k4h = api.get_klines(sym_w, "4h", 30)
                from entry_logic import liquidation_map as _lmap_fn, calc_entry_mode as _cem, _dev_threshold
                _lmap = _lmap_fn(sym_w, _k1h, _k4h, dir_w)
                _mode, _lp, _, _anchor = _cem(_k1h, dir_w, _lmap, 1.0, final_score=int(score_w))
                if _mode == "WAIT":
                    if "no_clear_condition" in _anchor:
                        wait_reason = "⏳ 等待最佳入场时机（EMA方向未确认，暂不开仓）"
                    elif "pullback_dev" in _anchor:
                        # 解析偏差数字
                        import re as _re
                        _m = _re.search(r'pullback_dev_([\d.]+)%', _anchor)
                        _dev_str = _m.group(1) if _m else "?"
                        _th_m = _re.search(r'threshold=([\d.]+)', _anchor)
                        _th_str = _th_m.group(1) if _th_m else "3.0"
                        wait_reason = f"⏳ 等待最佳入场时机（理想入场价距现价{_dev_str}%，超过{_th_str}%阈值，等价格回落）"
                    else:
                        wait_reason = f"⏳ 等待最佳入场时机（{_anchor}）"
                elif _mode in ("PULLBACK", "CHASE", "CHASE_ALT", "FADE"):
                    _dev = abs(_lp - _mark) / _mark * 100
                    wait_reason = f"🎯 挂单价 {_lp:.4g}，距现价 {_dev:.1f}%，等价格回落触发"
            except Exception:
                pass

            line = f"{idx}. {sym_w}｜{d_icon}｜{score_w:.0f}分{hit_tag}｜目标价位 {float(target_w):.4g}｜{reason_str}"
            if wait_reason:
                line += f"\n   {wait_reason}"
            slot_section.append(line)

        # 消费已展示的 hit_pending
        try:
            for sym_h in list(hit_map.keys()):
                if sym_h in all_cands:
                    del hit_map[sym_h]
        except Exception:
            pass
    except Exception as _we:
        log(f"[scan_report] watchlist slot read err: {_we}")

    # ── 本轮撤单 ─────────────────────────────────────────────────
    cancel_section = []
    _seen_cancel = set()
    for _cancel_item in cancelled_orders:
        if isinstance(_cancel_item, tuple):
            sym_c, reason_c, score_c = _cancel_item
        else:
            sym_c, reason_c, score_c = _cancel_item, "分数下降", 0
        if sym_c in _seen_cancel:
            continue
        _seen_cancel.add(sym_c)
        _score_str = f"（当前{score_c:.0f}分）" if score_c > 0 else ""
        if "BTC" in reason_c or "btc" in reason_c.lower():
            _reason_zh = f"BTC大幅波动引发强制撤单{_score_str}"
        elif "超时" in reason_c or "expire" in reason_c.lower() or "4小时" in reason_c:
            _reason_zh = f"挂单超时未成交（4小时过期）{_score_str}"
        elif "保护期" in reason_c:
            _reason_zh = f"保护期内评分跌破60{_score_str}，信号减弱"
        else:
            _reason_zh = f"评分下降{_score_str}，信号减弱"
        cancel_section.append(f"  🗑 {sym_c}  [已撤单]  {_reason_zh}")

    # ── [模拟] 组装报告 ─────────────────────────────────────────
    _pstats = _load_paper_state().get("stats", {})
    _realized = _pstats.get("pnl", 0.0)
    _t = _pstats.get("total", 0)
    _w = _pstats.get("wins", 0)
    _wr = f"{_w/_t*100:.1f}%" if _t > 0 else "N/A"
    log(f"[test_stats] 已结{_t}笔 胜{_w}败{_pstats.get('losses',0)} 胜率={_wr} PnL={_realized:+.2f}U")
    _float_pnl = sum(
        (float(api.get_mark_price(s)["markPrice"]) - p["entry"]) / p["entry"] * p["pos_usdt"]
        if p["direction"] == "LONG" else
        (p["entry"] - float(api.get_mark_price(s)["markPrice"])) / p["entry"] * p["pos_usdt"]
        for s, p in _load_paper_state().get("positions", {}).items()
        if (lambda mk: True)(float(api.get_mark_price(s).get("markPrice", p["entry"])))
    ) if _load_paper_state().get("positions") else 0.0
    try:
        _float_pnl = 0.0
        for _s, _p in _load_paper_state().get("positions", {}).items():
            try:
                _mk = float(api.get_mark_price(_s)["markPrice"])
                _dr = _p["direction"]
                _pu = _p["pos_usdt"]
                _ep = _p["entry"]
                _float_pnl += (_mk - _ep) / _ep * _pu if _dr == "LONG" else (_ep - _mk) / _ep * _pu
            except Exception:
                pass
    except Exception:
        _float_pnl = 0.0
    _net_val = PAPER_CAPITAL_U + _realized + _float_pnl
    lines = [
        f"[测试] 📊 扫描完成｜耗时 {int(elapsed)}s｜固定100U·无仓位上限",
        f"[测试] 历史已结：{_t}笔 胜{_w}败{_pstats.get('losses',0)} 胜率={_wr}  PnL={_realized:+.2f}U  浮动={_float_pnl:+.2f}U",
        f"模拟净值：{_net_val:.2f}U / {PAPER_CAPITAL_U:.0f}U  总收益：{_realized+_float_pnl:+.2f}U",
        "",
        "一、模拟仓位总览",
        f"1. 总仓位槽位：5个，当前已占用：{ps['count']}个",
        f"2. 细分占用：多头 {ps['long_count']} 槽，空头 {ps['short_count']} 槽",
        f"3. 已用名义资金：{ps['used_notional']:.0f}U（模拟）",
        "",
        "二、当前模拟持仓",
    ] + pos_section + [
        "",
        "三、本轮新开仓",
    ]
    if order_section:
        for idx, item in enumerate(order_section, 1):
            lines.append(f"{idx}. {item.lstrip()}")
    else:
        lines.append("本轮无新开仓")

    if slot_section:
        lines += ["", "四、高分观察池（评分≥70，等待空余槽位）"] + slot_section

    if cancel_section:
        lines += ["", "本轮撤单："] + cancel_section

    lines += [
        "",
        
    ]

    log(f"[scan_done] new={len(new_orders)} keep={len(keep_orders)} "
        f"watch_high={len(watch_high)} cancelled={len(cancelled_orders)} elapsed={elapsed:.0f}s")

    # [模拟] 有新开仓才推送简报，止盈止损触发已由 _paper_check_sl_tp 单独推送
    has_new_order   = len(new_orders) > 0
    has_cancellation = len(cancelled_orders) > 0
    if not has_new_order and not has_cancellation:
        log("[paper_done] 本轮无新模拟开仓，静默")
        return

    brief_lines = [f"[模拟] 📊 扫描完成｜{__import__('datetime').datetime.utcnow().strftime('%H:%M')} UTC"]
    if has_new_order:
        brief_lines.append("")
        brief_lines.append("**本轮新开仓（模拟）**")
        for e in new_orders:
            sym_n = e.get("sym", "")
            dir_n = "多" if e.get("direction","LONG") == "LONG" else "空"
            sc_n  = e.get("score", 0)
            pr_n  = e.get("limit_price", 0)
            nm_n  = e.get("notional", 0)
            tp_n  = e.get("tp", "?")
            sl_n  = e.get("sl", "?")
            act_n = "挂单" if e.get("action") == "paper_limit" else "开仓"
            brief_lines.append(f"• {sym_n} {dir_n} {act_n} | {sc_n:.0f}分 | 限价{pr_n:.6g} | {nm_n:.0f}U | TP {tp_n:.6g} SL {sl_n:.6g}")
    notify("\n".join(brief_lines))


SCAN_INTERVAL = 10 * 60   # 10 分钟扫描一轮（CPU优化：原5分钟→10分钟）

if __name__ == "__main__":
    import signal as _signal
    import sys as _sys

    # ── 命令行参数 ──────────────────────────────────────────────────
    # --once   : 单次扫描后退出
    # --status : 查看当前模拟仓位
    # --reset  : 清空模拟状态
    if "--status" in _sys.argv:
        _st = _load_paper_state()
        _pp = _st.get("positions", {})
        _hs = _st.get("stats", {})
        print(f"模拟仓位数: {len(_pp)}")
        for sym_s, p_s in _pp.items():
            try:
                mk_s = float(api.get_mark_price(sym_s)["markPrice"])
            except Exception:
                mk_s = p_s["entry"]
            ep_s = p_s["entry"]
            dr_s = p_s["direction"]
            pu_s = p_s.get("pos_usdt", 0)
            if dr_s == "LONG":
                pnl_pct_s = (mk_s - ep_s) / ep_s * 100
            else:
                pnl_pct_s = (ep_s - mk_s) / ep_s * 100
            pnl_u_s = pu_s * pnl_pct_s / 100
            print(f"  {sym_s} {dr_s}: 入场={ep_s:.6g} 现价={mk_s:.6g} PnL={pnl_u_s:+.2f}U ({pnl_pct_s:+.2f}%)")
            print(f"    SL={p_s.get('sl_price',0):.6g} TP1={p_s.get('tp1_price',0):.6g} 仓位={pu_s:.0f}U")
        print(f"历史: {_hs.get('total',0)}笔 胜{_hs.get('wins',0)} 败{_hs.get('losses',0)} 总PnL={_hs.get('pnl',0.0):+.2f}U")
        sys.exit(0)

    if "--reset" in _sys.argv:
        _save_paper_state({"positions": {}, "history": [], "stats": {"total": 0, "wins": 0, "losses": 0, "pnl": 0.0}})
        print("模拟状态已清空")
        sys.exit(0)

    # --once 参数：只跑一轮扫描就退出
    # 默认 daemon 模式：持续运行，每 SCAN_INTERVAL 秒循环
    _once_mode = "--once" in _sys.argv

    _running = True

    def _handle_term(sig, frame):
        global _running
        log("[main] 收到退出信号，下一轮扫描前优雅退出")
        _running = False

    import threading as _sig_threading
    _stop_event = _sig_threading.Event()

    def _handle_term_new(sig, frame):
        global _running
        log("[main] 收到退出信号，下一轮扫描前优雅退出")
        _running = False
        _stop_event.set()   # 立即唤醒 wait，不用等满 5 分钟

    _signal.signal(_signal.SIGTERM, _handle_term_new)
    _signal.signal(_signal.SIGINT,  _handle_term_new)

    if _once_mode:
        # ── 单次模式：cron/外部调用，跑完即退 ──
        log("[main] 单次模式（--once）")
        try:
            run_scan()
        except Exception as _e:
            log(f"[main] run_scan 异常: {_e}")
            import traceback as _tb
            log(_tb.format_exc()[:500])
        log("[main] 单次扫描完成，退出")
    else:
        # ── Daemon 模式：持续运行 ──
        # ── 启动时立即写入心跳，防 health_check 报"心跳文件不存在" ──────────
        try:
            import json as _hj, time as _ht, pathlib as _hp
            (_hp.Path(WORKSPACE) / "ws_cache" / "paper_scanner_test_heartbeat.json").write_text(
                _hj.dumps({"ts": _ht.time(), "status": "starting"}, indent=2))
        except Exception:
            pass
        log(f"[main] scanner 启动（daemon），扫描间隔 {SCAN_INTERVAL//60} 分钟")

        # ── 启动时立即执行一次全量 SL 检查（修复进程崩溃期间的脏数据）──
        log("[main] 启动时全量 SL 修复检查（修复进程崩溃期间未触发的止损）")
        try:
            _paper_check_sl_tp()
            log("[main] 启动 SL 修复检查完成")
        except Exception as _startup_e:
            log(f"[main] 启动 SL 修复异常: {_startup_e}")

        # ── 独立止损心跳线程：每 30 秒检查一次 SL/TP，不依赖主扫描轮次 ──
        import threading as _threading
        def _sl_heartbeat():
            import time as _t
            while _running:
                try:
                    _paper_check_sl_tp()
                except Exception as _he:
                    log(f"[sl_heartbeat] 异常: {_he}")
                _t.sleep(30)
        _hb_thread = _threading.Thread(target=_sl_heartbeat, daemon=True, name="sl-heartbeat")
        _hb_thread.start()
        log("[main] 止损心跳启动，每30秒检查一次 SL/TP")

        while _running:
            try:
                run_scan()
            except Exception as _e:
                log(f"[main] run_scan 异常: {_e}")
                import traceback as _tb
                log(_tb.format_exc()[:800])
            if not _running:
                break
            log(f"[main] 下一轮扫描在 {SCAN_INTERVAL//60} 分钟后")
            # 整段 sleep，用 Event 响应 SIGTERM，CPU 友好
            _stop_event.wait(timeout=SCAN_INTERVAL)
