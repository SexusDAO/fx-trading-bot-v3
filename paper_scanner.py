#!/usr/bin/env python3
"""
paper_scanner.py — 模拟扫描工作流（完整复刻 scanner.py，不真实下单）

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
    """记录 Coinglass 请求结果：ok / 404 / timeout"""
    import time as _t
    now = _t.time()
    global _CG_HEALTH
    if now - _CG_HEALTH["hour_start"] >= 3600:
        # 新一小时，重置统计
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

# 模拟模式：替换 calc_position_size 为 real_size 包装
def _calc_pos_size(sig_type, score, direction, details):
    return _real_size(sig_type=sig_type, score=score, direction=direction, details=details)

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
_CG_LOCK    = __import__("threading").Lock()
_STATE_LOCK = __import__("threading").Lock()   # 防止 sl_heartbeat 线程与主线程并发写 state

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
PAPER_SIM_STATE   = "/root/.openclaw/workspace/paper_sim_state.json"
PAPER_CAPITAL_U   = 1000.0   # 模拟总资金

# 模拟占名义仓位计算（复用 scanner.py 预统计单口）
_paper_positions: dict = {}   # sym -> pos dict

def _load_paper_state() -> dict:
    import pathlib, json as _j
    f = pathlib.Path(PAPER_SIM_STATE)
    if f.exists():
        try: return _j.loads(f.read_text())
        except Exception: pass
    return {"positions": {}, "pending_orders": {}, "history": [], "stats": {"total": 0, "wins": 0, "losses": 0, "pnl": 0.0}}

def _save_paper_state(state: dict):
    """原子写入 + 线程锁保护（防止 sl_heartbeat 与主线程并发写）"""
    import pathlib, json as _j
    with _STATE_LOCK:
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

    # ── 功能一：相关性过滤（paper 轻量版）────────────────────────────────────
    try:
        import numpy as _np
        _CORR_THRESH = 0.85
        def _get_closes(s, n=25):
            raw = api.get_klines(s, "1h", n)
            return [float(k[4]) for k in raw[:-1]]
        _closes_new = _get_closes(sym)
        _max_corr = 0.0
        _block_corr = False
        for _psym, _ppos in positions.items():
            if _ppos["direction"] != direction: continue
            try:
                _closes_p = _get_closes(_psym)
                _n = min(len(_closes_new), len(_closes_p), 24)
                if _n < 8: continue
                _c = float(_np.corrcoef(_closes_new[-_n:], _closes_p[-_n:])[0,1])
                if abs(_c) > _max_corr: _max_corr = abs(_c)
                if abs(_c) > _CORR_THRESH: _block_corr = True; break
            except Exception: pass
        if _block_corr:
            log(f"[JUDGE_REJECT] {sym} 相关性过高 max_corr={_max_corr:.3f}>{_CORR_THRESH}，跳过")
            return False
        if details is not None:
            details["max_correlation"] = round(_max_corr, 4)
    except Exception as _corr_e:
        log(f"[corr_warn] {sym}: {_corr_e}")
    # ─────────────────────────────────────────────────────────────────────────

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
        "intra_high":  entry_price,   # vnpy式ATR移动止损：持仓期最高价
        "intra_low":   entry_price,   # vnpy式ATR移动止损：持仓期最低价
        "trailing_sl": None,          # ATR移动止损价（入场后立刻启动）
        "tp1_hit":     False,
    }
    _save_paper_state(state)
    dir_zh = "多" if direction == "LONG" else "空"
    _open_msg = (
        f"[模拟] 📋 {sym} {dir_zh}单开仓 | {now_str}\n"
        f"评分：{score:.0f}  信号：{sig_type}\n"
        f"入场：{entry_price:.6g}\n"
        f"止损：{sl_price:.6g}  止盈TP1：{tp1_price:.6g}\n"
        f"仓位：{pos_usdt:.0f}U  ⚠️ 仅模拟，不动本金"
    )
    log(_open_msg)
    notify(_open_msg, force=True)   # 开仓成功 → 真正推送
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

    rec = {**pos,
           "symbol":      sym,
           "close_price": close_price,
           "close_at":    __import__('datetime').datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
           "pnl_u":       pnl_u,
           "pnl_pct":     round(pnl_pct, 2),
           "reason":      reason}
    state.setdefault("history", []).append(rec)
    stats = state.setdefault("stats", {"total": 0, "wins": 0, "losses": 0, "pnl": 0.0})
    # 复盘优化：stats.total 仅统计系统自动成交（过滤 MANUAL_CLOSE，避免胜率失真）
    if reason != "MANUAL_CLOSE":
        stats["total"] += 1
        if pnl_u > 0: stats["wins"] += 1
        else:         stats["losses"] += 1
    stats["pnl"] = round(stats["pnl"] + pnl_u, 2)
    _save_paper_state(state)

    # ── IC 追踪：平仓后写入 ws_cache/signal_ic_log.jsonl ──
    try:
        import pathlib as _pl, json as _jj, time as _ti
        _ic_path = _pl.Path(WORKSPACE) / "ws_cache" / "signal_ic_log.jsonl"
        _ic_path.parent.mkdir(parents=True, exist_ok=True)
        _ic_rec = {
            "ts":        _ti.time(),
            "sym":       sym,
            "sig_type":  pos.get("sig_type", "?"),
            "score":     pos.get("score", 0),
            "direction": direction,
            "pnl_pct":   round(pnl_pct, 4),
            "pnl_u":     pnl_u,
            "reason":    reason,
            "hold_h":    round((_ti.time() - pos.get("opened_ts", _ti.time())) / 3600, 2),
            "version":   "sim",
        }
        with open(_ic_path, "a") as _icf:
            _icf.write(_jj.dumps(_ic_rec) + "\n")
    except Exception:
        pass

    emoji = "✅" if pnl_u > 0 else "❌"
    dir_zh = "多" if direction == "LONG" else "空"
    _close_msg = (
        f"[模拟] {emoji} {sym} {dir_zh}单平仓 ({reason})\n"
        f"入场={entry:.6g}  出场={close_price:.6g}  PnL={pnl_u:+.2f}U ({pnl_pct:+.2f}%)\n"
        f"历史胜率={stats['wins']}/{stats['total']}  总PnL={stats['pnl']:+.2f}U"
    )
    log(_close_msg)
    notify(_close_msg, force=True)   # 止损/止盈结果 → 真正推送

    # ── 第8项：平仓后检查信号等待队列 ────────────────────────────────────────
    _flush_signal_queue()
    # ─────────────────────────────────────────────────────────────────────────

def _flush_signal_queue():
    """
    冲突二修复(v3.3)：出队时完整重新预检 + 价格合理性验证
    1. 超时清除
    2. 重新检查持仓数量（槽位）
    3. 重新执行相关性过滤
    4. 重新检查冷却期
    5. 价格偏差不超过 ATR×1.0
    全部通过才执行，否则丢弃
    """
    import time as _t
    import numpy as _np
    now = _t.time()
    if not _SIGNAL_WAIT_QUEUE:
        return

    state = _get_position_state()
    positions = state.get("positions", {})

    for q in list(_SIGNAL_WAIT_QUEUE):
        sym = q["sym"]; direction = q["direction"]

        # 1. 超时清除
        if now - q["ts"] > _SIGNAL_QUEUE_TTL:
            log(f"[wait_queue] {sym} 超时移除")
            _SIGNAL_WAIT_QUEUE.remove(q)
            continue

        # 2. 槽位检查（重新读最新状态）
        long_cnt  = sum(1 for p in positions.values() if p.get("direction")=="LONG")
        short_cnt = sum(1 for p in positions.values() if p.get("direction")=="SHORT")
        max_pos, max_long, max_short = _get_dynamic_limits()
        if direction == "LONG"  and long_cnt  >= max_long:  continue
        if direction == "SHORT" and short_cnt >= max_short: continue

        # 3. 冷却期检查
        if _is_in_cooldown_paper(sym):
            log(f"[wait_queue] {sym} 冷却期，丢弃")
            _SIGNAL_WAIT_QUEUE.remove(q)
            continue

        # 4. 相关性过滤（重新检查，持仓已变）
        try:
            _CORR_THRESH = 0.85
            def _get_closes(s, n=25):
                raw = api.get_klines(s, "1h", n)
                return [float(k[4]) for k in raw[:-1]]
            _closes_new = _get_closes(sym)
            _block = False
            for _psym, _ppos in positions.items():
                if _ppos.get("direction") != direction: continue
                try:
                    _closes_p = _get_closes(_psym)
                    _n = min(len(_closes_new), len(_closes_p), 24)
                    if _n < 8: continue
                    _c = float(_np.corrcoef(_closes_new[-_n:], _closes_p[-_n:])[0,1])
                    if abs(_c) > _CORR_THRESH: _block = True; break
                except Exception: pass
            if _block:
                log(f"[wait_queue] {sym} 出队相关性过高，丢弃")
                _SIGNAL_WAIT_QUEUE.remove(q)
                continue
        except Exception: pass

        # 5. 价格合理性验证（偏差 <= ATR×1.0）
        try:
            mark = float(api.get_mark_price(sym)["markPrice"])
            atr_q = q.get("atr", 0)
            if atr_q <= 0:
                atr_q = atr_calc(api.get_klines(sym, "1h", 20))
            dev = abs(mark - q["entry_price"])
            if atr_q > 0 and dev > atr_q * 1.0:
                log(f"[wait_queue] {sym} 偏差{dev:.4g}>ATR×1.0={atr_q:.4g}，价格失效，丢弃")
                _SIGNAL_WAIT_QUEUE.remove(q)
                continue
        except Exception:
            continue

        # 全部通过 → 触发开仓
        placed = _paper_place_limit_order(
            sym=sym, direction=direction,
            limit_price=q["entry_price"], sl_price=q["sl_price"],
            tp1_price=q["tp1_price"], tp2_price=q["tp2_price"],
            pos_usdt=q["pos_usdt"], sig_type=q["sig_type"],
            score=q["score"], details=q["details"],
        )
        if placed:
            log(f"[wait_queue] {sym} 等待队列信号触发成功(完整预检通过) score={q['score']}")
            _SIGNAL_WAIT_QUEUE.remove(q)
            break


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
    now_str = _dt.datetime.utcnow().strftime("%m-%d %H:%M UTC")
    now_ts  = time.time()
    state.setdefault("pending_orders", {})[sym] = {
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
        "expire_ts":   now_ts + 4 * 3600,  # 4小时有效期
        "btc_trend":   details.get("btc_trend", ""),
        "sentiment":   details.get("sentiment", ""),
    }
    _save_paper_state(state)
    dir_zh = "多" if direction == "LONG" else "空"
    log(
        f"[模拟] 📌 {sym} {dir_zh}单挂单 | {now_str}\n"
        f"评分：{score:.0f}  信号：{sig_type}\n"
        f"限价：{limit_price:.6g}（现价下方等回调）\n"
        f"止损：{sl_price:.6g}  止盈TP1：{tp1_price:.6g}\n"
        f"仓位：{pos_usdt:.0f}U  有效期：4小时  ⚠️ 仅模拟"
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
        # 超时撤单
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
        # 再检查一次槽位（其他仓位可能已开满）
        if len(positions) >= MAX_POSITIONS: continue
        if dr == "LONG"  and long_cnt  >= MAX_LONG:  continue
        if dr == "SHORT" and short_cnt >= MAX_SHORT: continue

        now_str = _dt.datetime.utcnow().strftime("%m-%d %H:%M UTC")
        positions[sym] = {
            "direction": dr,
            "entry":     fill_price,           # 实际成交价=触碰时mark price
            "limit_price_orig": order["limit_price"],  # 原始挂单价记录
            "sl_price":  order["sl_price"],
            "tp1_price": order["tp1_price"],
            "tp2_price": order["tp2_price"],
            "pos_usdt":  order["pos_usdt"],
            "sig_type":  order["sig_type"],
            "score":     order["score"],
            "opened_at": now_str,
            "placed_at": order["placed_at"],
            "btc_trend": order.get("btc_trend", ""),
            "sentiment": order.get("sentiment", ""),
        }
        # ── 成交后校验SL方向：防止挂单期间价格偏移导致SL方向错 ──
        _sl_now = positions[sym]["sl_price"]
        _sl_ok  = (_sl_now < fill_price) if dr == "LONG" else (_sl_now > fill_price)
        if not _sl_ok:
            try:
                _k1h_fix = api.get_klines(sym, "1h", 30)
                _atr_fix = atr_calc(_k1h_fix)
                _sl_fixed = round(fill_price - _atr_fix * 1.5, 8) if dr == "LONG" \
                            else round(fill_price + _atr_fix * 1.5, 8)
                if abs(_sl_fixed - fill_price) / fill_price > 0.08:
                    _sl_fixed = round(fill_price * (0.92 if dr == "LONG" else 1.08), 8)
                positions[sym]["sl_price"] = _sl_fixed
                log(f"[paper_sl_fix] {sym} {dr} SL方向错误，成交后重算 {_sl_now:.5g}→{_sl_fixed:.5g}")
            except Exception as _e:
                log(f"[paper_sl_fix] {sym} SL重算失败: {_e}")
        dir_zh = "多" if dr == "LONG" else "空"
        wait_min = int((now_ts - order["placed_ts"]) / 60)
        log(
            f"[模拟] ✅ {sym} {dir_zh}单限价成交！| {now_str}\n"
            f"挂单价：{order['limit_price']:.6g}  成交价：{fill_price:.6g}\n"
            f"等待时间：约{wait_min}分钟\n"
            f"止损：{order['sl_price']:.6g}  止盈TP1：{order['tp1_price']:.6g}\n"
            f"仓位：{order['pos_usdt']:.0f}U"
        )
        log(f"[paper_filled] {sym} {dr} 限价成交 limit={order['limit_price']:.6g} fill={fill_price:.6g}")

    for sym, order in expired:
        del pending[sym]
        dr = order["direction"]
        dir_zh = "多" if dr == "LONG" else "空"
        placed = order.get("placed_at", "")
        log(
            f"[模拟] ⏰ {sym} {dir_zh}单挂单超时撤销\n"
            f"挂单价：{order['limit_price']:.6g}  挂单时间：{placed}\n"
            f"4小时内价格未回调到限价位，已撤单"
        )
        log(f"[paper_expired] {sym} {dr} 挂单超时撤销 limit={order['limit_price']:.6g}")

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
        # 复盘修复：间距从 -5% 收窄到 -2%，减少利润回吐
        trailing_sl = pos.get("trailing_sl", None)
        if trailing_sl is not None:
            if direction == "LONG":
                new_trail = round(mark * 0.98, 8)   # 当前价 -2%（原-5%）
                if new_trail > trailing_sl:
                    state["positions"][sym]["trailing_sl"] = new_trail
                    trailing_sl = new_trail
                if mark <= trailing_sl * 1.0003:
                    log(f"[paper_trailing_sl] {sym} LONG 移动止损触发 mark={mark:.5g} tsl={trailing_sl:.5g}")
                    _paper_close(sym, "移动止损触发", mark)
                    continue
            else:  # SHORT
                new_trail = round(mark * 1.02, 8)   # 当前价 +2%（原+5%）
                if new_trail < trailing_sl:
                    state["positions"][sym]["trailing_sl"] = new_trail
                    trailing_sl = new_trail
                if mark >= trailing_sl * 0.9997:
                    log(f"[paper_trailing_sl] {sym} SHORT 移动止损触发 mark={mark:.5g} tsl={trailing_sl:.5g}")
                    _paper_close(sym, "移动止损触发", mark)
                    continue
            _save_paper_state(state)
            continue   # 剩余仓位只用移动止损，不再判断原始 TP1

        # ── vnpy式ATR移动止损：入场后立刻启动，不等TP1 ────────────────────
        # 参考 vnpy AtrRsiStrategy: long_stop = intra_trade_high * (1 - trailing_pct)
        # 持续更新 intra_high/low，ATR移动止损 = intra_high × (1 - ATR_TRAIL_PCT)
        ATR_TRAIL_PCT  = 0.04   # 移动止损距离：高点-4%（比TP1后的2%宽，给行情空间）
        ATR_TRAIL_MIN  = 0.015  # 最小浮盈才启动：高点需超入场价1.5%才开始追踪
        if pos.get("trailing_sl") is None:   # 只在TP1未触发时用ATR追踪
            # 更新最高/最低价
            _ih = max(float(pos.get("intra_high", ep)), mark)
            _il = min(float(pos.get("intra_low",  ep)), mark)
            state["positions"][sym]["intra_high"] = _ih
            state["positions"][sym]["intra_low"]  = _il

            if direction == "LONG":
                _atr_trail = round(_ih * (1 - ATR_TRAIL_PCT), 8)
                _min_activated = _ih >= ep * (1 + ATR_TRAIL_MIN)
                if _min_activated and _atr_trail > sl:
                    # ATR移动止损比固定SL更优，用ATR止损替换
                    if mark <= _atr_trail:
                        log(f"[atr_trail] {sym} LONG ATR移动止损触发 mark={mark:.5g} ih={_ih:.5g} trail={_atr_trail:.5g}")
                        _paper_close(sym, f"ATR移动止损(ih={_ih:.4g})", mark)
                        continue
                    # 更新 sl 为 ATR 移动止损（只能上移，不能下移）
                    if _atr_trail > state["positions"][sym]["sl_price"]:
                        state["positions"][sym]["sl_price"] = _atr_trail
                        sl = _atr_trail
            else:  # SHORT
                _atr_trail = round(_il * (1 + ATR_TRAIL_PCT), 8)
                _min_activated = _il <= ep * (1 - ATR_TRAIL_MIN)
                if _min_activated and _atr_trail < sl:
                    if mark >= _atr_trail:
                        log(f"[atr_trail] {sym} SHORT ATR移动止损触发 mark={mark:.5g} il={_il:.5g} trail={_atr_trail:.5g}")
                        _paper_close(sym, f"ATR移动止损(il={_il:.4g})", mark)
                        continue
                    if _atr_trail < state["positions"][sym]["sl_price"]:
                        state["positions"][sym]["sl_price"] = _atr_trail
                        sl = _atr_trail

        # ── 固定止损检查 ────────────────────────────────────────────────────
        if direction == "LONG":
            if mark <= sl:
                _paper_close(sym, "SL触发", mark); continue
        else:
            if mark >= sl:
                _paper_close(sym, "SL触发", mark); continue

        # ── 优化①：TP1触发时平50%，剩余启动移动止损 ──────────────────
        # 复盘修复：写入 tp1_hit=True；空头TP1方向修正（must < tp1）；移动止损初始化用 -2%
        # 空头TP1方向修正：tp1 必须 < ep（向下），若 tp1 >= ep 说明计算异常，跳过
        _tp1_valid_short = tp1 > 0 and tp1 < ep
        _tp1_valid_long  = tp1 > 0 and tp1 > ep

        if direction == "LONG" and _tp1_valid_long and mark >= tp1:
            half_pnl = round(pu * 0.5 * (mark - ep) / ep, 2)
            state["positions"][sym]["pos_usdt"]  = round(pu * 0.5, 2)
            state["positions"][sym]["tp1_hit"]   = True   # ✅ 记录tp1已触发
            _atr_init = abs(ep - pos.get("sl_price", ep * 0.94)) / 1.8
            state["positions"][sym]["trailing_sl"] = round(mark * 0.98, 8)  # -2%
            _save_paper_state(state)
            log(f"[模拟] 🎯 {sym} 多 TP1触发 平仓50% mark={mark:.5g} half_pnl={half_pnl:+.2f}U 移动止损@{mark*0.98:.5g}")
            log(f"[paper_tp1_half] {sym} TP1半仓平 mark={mark:.5g} half_pnl={half_pnl:+.2f}U")
            continue

        if direction == "SHORT" and _tp1_valid_short and mark <= tp1:
            half_pnl = round(pu * 0.5 * (ep - mark) / ep, 2)
            state["positions"][sym]["pos_usdt"]  = round(pu * 0.5, 2)
            state["positions"][sym]["tp1_hit"]   = True   # ✅ 记录tp1已触发
            _atr_init_s = abs(pos.get("sl_price", ep * 1.06) - ep) / 1.8
            state["positions"][sym]["trailing_sl"] = round(mark * 1.02, 8)  # +2%
            _save_paper_state(state)
            log(f"[模拟] 🎯 {sym} 空 TP1触发 平仓50% mark={mark:.5g} half_pnl={half_pnl:+.2f}U 移动止损@{mark*1.02:.5g}")
            log(f"[paper_tp1_half] {sym} TP1半仓平 mark={mark:.5g} half_pnl={half_pnl:+.2f}U")
            continue

        # 空头TP1方向异常保护：tp1算反了（>ep），直接跳过不平仓，等SL或时间止损
        if direction == "SHORT" and tp1 >= ep and tp1 > 0:
            log(f"[paper_tp1_warn] {sym} 空头TP1={tp1:.5g} >= entry={ep:.5g} 方向异常，跳过TP1检查")

        # ── 优化③ v2：时间止损（持仓>24H浮亏>1%收紧，持仓>20H浮亏>3%也收紧）──
        opened_at_str = pos.get("opened_at", "")
        try:
            import datetime as _dtt
            opened_dt = _dtt.datetime.strptime(
                "2026-" + opened_at_str.replace(" UTC",""), "%Y-%m-%d %H:%M"
            )
            hold_h = (now_ts - opened_dt.replace(tzinfo=_dtt.timezone.utc).timestamp()) / 3600
        except Exception:
            hold_h = 0

        if hold_h > 0:
            pnl_pct = (mark - ep)/ep*100 if direction=="LONG" else (ep - mark)/ep*100
            current_sl = pos["sl_price"]
            tight_sl = None

            if hold_h > 24 and pnl_pct < -1.0:
                # 持仓超24H且浮亏超1%：收紧到保本附近（-1.5%）防止小亏变大亏
                tight_sl = round(mark * (1 - 0.015) if direction=="LONG"
                                 else mark * (1 + 0.015), 8)
                log(f"[paper_time_sl] {sym} 持仓{hold_h:.1f}h 浮亏{pnl_pct:.1f}% >24H触发收紧")
            elif hold_h > 20 and pnl_pct < -3.0:
                # 持仓超20H且浮亏超3%：收紧到当前价-3%
                tight_sl = round(mark * 0.97 if direction=="LONG" else mark * 1.03, 8)

            if tight_sl is not None:
                # ★ 修复：时间止损的 should_tighten 判断逻辑错误
                # LONG： tight_sl 应在当前 SL 以上（更高才是收紧）
                # SHORT： tight_sl 应在当前 SL 以下（更低才是收紧）
                # 另：如果 tight_sl 已超越当前价，直接平仓（止损已穿越）
                if direction == "LONG":
                    if tight_sl >= mark:   # 止损收紧到当前价以上，直接平仓
                        log(f"[paper_time_sl] {sym} 时间止损已穿越当前价，直接平仓")
                        _paper_close(sym, "时间止损平仓", mark)
                        continue
                    elif tight_sl > current_sl:   # 正常收紧
                        state["positions"][sym]["sl_price"] = tight_sl
                        _save_paper_state(state)
                        log(f"[paper_time_sl] {sym} 持仓{hold_h:.1f}h 浮亏{pnl_pct:.1f}% 止损收紧 {current_sl:.5g}→{tight_sl:.5g}")
                else:  # SHORT
                    if tight_sl <= mark:   # 止损收紧到当前价以下，直接平仓
                        log(f"[paper_time_sl] {sym} 时间止损已穿越当前价，直接平仓")
                        _paper_close(sym, "时间止损平仓", mark)
                        continue
                    elif tight_sl < current_sl:   # 正常收紧
                        state["positions"][sym]["sl_price"] = tight_sl
                        _save_paper_state(state)
                        log(f"[paper_time_sl] {sym} 持仓{hold_h:.1f}h 浮亏{pnl_pct:.1f}% 止损收紧 {current_sl:.5g}→{tight_sl:.5g}")

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
LOG_FILE       = f"{WORKSPACE}/paper_scanner.log"       # 模拟模式独立日志
JARVIS_TARGET  = "432401404:thread:019fcac8-4a93-7bea-a930-7f0e59e4897c"  # 模拟模式推送到当前 thread

SCORE_FIRE_HIGH = 80   # ≥80 → 直接挂单（强信号）
SCORE_AUTO      = 75   # ≥75 → 候选挂单（优化：从70提高到75，过滤低质信号）
SCORE_WATCH     = 60   # ≥60 → 进观察区
# < 60 → 跳过
SCAN_TOP_N = 60   # 成交量榜基础扫描 60 个（另外 + 涨幅/跌幅/热度榜）
MIN_VOL_USD = 5_000_000   # 优化：从 300 万 U 提至 500 万 U（过滤低流动性）   # 最低 24h 成交额（300万U）

# ─── 仓位规则（用户确认版）─────────────────────────────────────────────────────
MAX_POSITIONS  = 5     # 最多同时持仓数（动态上限基准）
MAX_LONG       = 3     # 多单上限（动态基准）
MAX_SHORT      = 1     # 空单上限（动态基准）
MAX_POSITIONS_HARD_MAX = 7   # 硬性上限
MAX_POSITIONS_HARD_MIN = 2   # 硬性下限

# ── 第8项：信号等待队列（最多3个，内存态，重启清空）──
import collections as _collections
_SIGNAL_WAIT_QUEUE: list = []   # [{"sym","dir","entry","score_sum","ts","details",...}, ...]
_SIGNAL_QUEUE_MAX  = 3
_SIGNAL_QUEUE_TTL  = 30 * 60   # 30分钟有效期

def _get_dynamic_limits():
    """
    冲突三修复(v3.3)：动态调整仅在BTC市场模式判断的基础上 ±1 微调，取较小值。
    先用BTC EMA判断市场基础上限，再用ATR中位数做微调，不覆盖市场判断。
    """
    # ── Step1: BTC市场模式基础上限 ──────────────────────────────────────────
    btc_base_pos = MAX_POSITIONS; btc_base_long = MAX_LONG; btc_base_short = MAX_SHORT
    try:
        import urllib.request as _ur, json as _jj
        _url = "https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval=4h&limit=55"
        _kl  = _jj.loads(_ur.urlopen(_url, timeout=6).read())
        if len(_kl) >= 50:
            _c = [float(k[4]) for k in _kl]
            from entry_logic import ema_calc
            _e20 = ema_calc(_c[-20:], 20); _e50 = ema_calc(_c[-50:], 50)
            _chg4h = (_c[-1] - _c[-2]) / _c[-2] * 100 if len(_c) >= 2 else 0
            if _e20 > _e50 and _chg4h > 0:          # 多头市场
                btc_base_pos = 5; btc_base_long = 3; btc_base_short = 2
            elif _e20 < _e50 and _chg4h < 0:        # 空头市场
                btc_base_pos = 5; btc_base_long = 2; btc_base_short = 3
            else:                                     # 震荡
                btc_base_pos = 4; btc_base_long = 2; btc_base_short = 2
    except Exception:
        pass

    # ── Step2: ATR中位数微调（±1，不超过BTC基础±1的范围）───────────────────
    delta = 0
    try:
        import pathlib, json as _j
        _f = pathlib.Path("/root/.openclaw/workspace/ws_cache/market_volatility.json")
        if _f.exists():
            _v = _j.loads(_f.read_text())
            atr_median = float(_v.get("atr_median_pct", 5.0))
            if atr_median < 3.0:
                delta = +1    # 低波动：可多开一个
            elif atr_median > 10.0:
                delta = -1    # 极端波动：少开一个
    except Exception:
        pass

    # ── Step3: 取较小值（保守优先），硬性边界保底
    final_pos   = max(MAX_POSITIONS_HARD_MIN, min(btc_base_pos + delta, MAX_POSITIONS_HARD_MAX))
    final_long  = max(1, min(btc_base_long + max(0, delta), MAX_POSITIONS_HARD_MAX - 1))
    final_short = max(0, min(btc_base_short + max(0, delta), MAX_POSITIONS_HARD_MAX - 1))
    return final_pos, final_long, final_short
WL_MAX_CANDIDATES = 100  # 观察区候选上限 100 个
MAX_NEW_PER_SCAN  = 3    # 每轮最多新开仓数

# ─── 相关性过滤（与固定100U版完全一致）────────────────────────────────────────
CLUSTER_LONG_MAX  = 2
CLUSTER_SHORT_MAX = 3
CLUSTER_MAP = {
    "major": {"BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT","AVAXUSDT",
              "LINKUSDT","DOGEUSDT","NEARUSDT","LTCUSDT","BCHUSDT","ADAUSDT",
              "DOTUSDT","MATICUSDT","UNIUSDT","ATOMUSDT","APTUSDT","OPUSDT",
              "ARBUSDT","SUIUSDT","SEIUSDT","STXUSDT","INJUSDT","TIAUSDT"},
    "ai":    {"TAOUSDT","FETUSDT","RENDERUSDT","WLDUSDT","AGIXUSDT","OCEANUSDT"},
    "meme":  {"DOGEUSDT","SHIBUSDT","PEPEUSDT","1000PEPEUSDT","FLOKIUSDT",
              "1000SHIBUSDT","TRUMPUSDT","MOODENGUSDT"},
    "defi":  {"AAVEUSDT","CRVUSDT","MKRUSDT","COMPUSDT","SNXUSDT","BALUSDT"},
}

def _get_cluster(sym: str) -> str:
    for name, members in CLUSTER_MAP.items():
        if sym in members:
            return name
    return sym

def _check_correlation(sym: str, direction: str, positions: dict, pending: dict) -> tuple:
    limit = CLUSTER_SHORT_MAX if direction == "SHORT" else CLUSTER_LONG_MAX
    cluster = _get_cluster(sym)
    if cluster == sym:
        return True, ""
    count = sum(
        1 for s, p in {**positions, **pending}.items()
        if s != sym and p.get("direction") == direction and _get_cluster(s) == cluster
    )
    if count >= limit:
        return False, f"集群[{cluster}]{direction}已有{count}个，上限{limit}"
    return True, ""

POS_MIN_U      = 100   # 单笔名义仓位下限（U）
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
    try:
        lines = open(LOG_FILE, encoding='utf-8', errors='replace').readlines() if os.path.exists(LOG_FILE) else []
        lines.append(line)
        open(LOG_FILE, "w").writelines(lines[-LOG_MAX_LINES:])
    except Exception:
        pass


# Jarvis 推送目标（从 session_status 获取）
_JARVIS_TARGET = "432401404:thread:019fcac8-4a93-7bea-a930-7f0e59e4897c"

def notify(msg: str, force: bool = False):
    """
    通知策略：
    - force=True（开仓成功/止损触发）：真正推送到 Jarvis
    - force=False（其他状态）：静默，仅写日志
    """
    if not msg.startswith("[模拟]"):
        msg = f"[模拟] {msg}"
    log(msg)
    if force:
        # 真正推送到 Jarvis
        try:
            import subprocess as _sp
            _sp.Popen([
                "openclaw", "message", "send",
                "--channel", "jarvis",
                "--to", _JARVIS_TARGET,
                "--message", msg
            ], stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
        except Exception as _ne:
            log(f"[notify_err] {_ne}")


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

        sig_type  = classify_signal(k1h, k4h, funding, chg24)
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

        # ── BTC 环境评分加减分 ──
        btc_adj = 0
        if btc_trend == "down" and direction == "LONG":    btc_adj = -5
        elif btc_trend == "up"   and direction == "SHORT": btc_adj = -5
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
        _hb_file = pathlib.Path(WORKSPACE) / "ws_cache" / "paper_scanner_heartbeat.json"
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

    # ── Ru7 OI增幅榜 + 做市商预警 注入候选池 ────────────────────────────────
    # OI 增幅 >30% 且 OI>$20M：主力建仓信号，现有量/涨幅池可能遗漏
    # 做市商预警 ≥8 分：异常放量/阶梯拉盘等主力行为特征
    import pathlib as pathlib  # noqa: PLC0414 确保局部可用
    _RU7_INJECT = pathlib.Path(WORKSPACE) / "ws_cache" / "ru7_inject.json"
    _ru7_added  = []
    try:
        if _RU7_INJECT.exists():
            _rdata = json.loads(_RU7_INJECT.read_text())
            # 读取所有未消费的注入条目（oi_gainers + market_maker）
            _all_entries = (
                _rdata.get("oi_gainers", []) +
                _rdata.get("market_maker", []) +
                _rdata.get("hot_contracts", [])  # 向后兼容旧字段
            )
            _changed = False
            for _entry in _all_entries:
                if _entry.get("consumed"): continue
                _esym = _entry.get("sym","")
                if _esym and _esym not in top_syms and _esym not in active_syms:
                    top_syms.insert(0, _esym)   # 最前面优先扫描
                    _ru7_added.append(_esym)
                    _entry["consumed"] = True
                    _changed = True
            if _ru7_added:
                log(f"[ru7_inject] 注入候选池 {len(_ru7_added)} 个: {_ru7_added}")
                if _changed:
                    _RU7_INJECT.write_text(json.dumps(_rdata, indent=2, ensure_ascii=False))
    except Exception as _re:
        log(f"[ru7_inject] 读取失败: {_re}")
    # ─────────────────────────────────────────────────────────────────────────

    # ── 第7项补充二：资金费率极端强制空单候选 ──────────────────────────────
    # 资金费率 > +0.3%（0.003）的标的多头极度拥挤，强制归入 reversal 空单扫描
    _forced_short_syms = []
    for _fs in list(tickers.keys()):
        _fr = abs(funding_map.get(_fs, 0.0))
        _fr_pct = _fr * 100
        _fval = float(tickers[_fs].get("quoteVolume", 0))
        if funding_map.get(_fs, 0.0) > 0.003 and _fval >= MIN_VOL_USD and _fs not in active_syms:
            _forced_short_syms.append(_fs)
    if _forced_short_syms:
        log(f"[funding_force] 资金费率极端标的（强制空单候选）: {_forced_short_syms[:10]}")
        # 把这些标的加入扫描池最前面，并标记为 forced_short
        _forced_new = [s for s in _forced_short_syms if s not in top_syms]
        top_syms = _forced_new + top_syms
    # ─────────────────────────────────────────────────────────────────────────

    # ── 追涨模块：24h涨幅>20% 的突破行情，独立评估是否追进 ──────────────────
    # 规则：涨幅>20% + 1h仍在涨 + 成交量放大(>2x) + RSI55-75 + BTC非down
    # 特点：无市值下限限制，小仓50U，止损-5%，移动止损+10%跑趋势
    SURGE_MIN_GAIN    = 20.0   # 触发门槛：24h涨幅%
    SURGE_POS_USDT    = 500.0  # 仓位：固定500U（止损5%=最大亏25U）
    SURGE_SL_PCT      = 0.05   # 止损：-5%
    SURGE_TRAIL_PCT   = 0.10   # 移动止损追踪：盈利超10%后启动
    SURGE_RSI_MIN     = 50     # RSI最低（不追过冷的反弹）
    SURGE_RSI_MAX     = 78     # RSI最高（不追超买）
    SURGE_VOL_RATIO   = 1.8    # 成交量放大倍数（当前1h成交量/近20h均量）

    _btc_now = _mctx.get("btc_trend", "")
    _max_surge_per_round = 1   # 每轮最多追1个，防连续追高

    if _btc_now != "down":  # BTC 下跌时不追涨
        _surge_state = _load_paper_state()  # 追涨模块独立加载state
        # 找涨幅>20%且不在持仓里的币（全市场，不限成交量）
        _surge_candidates = [
            s for s, t in tickers.items()
            if float(t.get("priceChangePercent", 0)) >= SURGE_MIN_GAIN
            and s not in active_syms
            and s not in _surge_state.get("positions", {})
            and s not in _surge_state.get("pending_orders", {})
            and s.endswith("USDT")
        ]
        _surge_candidates.sort(
            key=lambda s: float(tickers[s].get("priceChangePercent", 0)), reverse=True
        )
        log(f"[surge] 涨幅>{SURGE_MIN_GAIN}%候选: {len(_surge_candidates)}个 {_surge_candidates[:5]}")

        _surge_opened = 0
        for _ssym in _surge_candidates[:5]:  # 最多看前5个
            if _surge_opened >= _max_surge_per_round:
                break
            if len(_surge_state.get("positions", {})) + len(_surge_state.get("pending_orders", {})) >= MAX_POSITIONS:
                break
            try:
                _sk1h = api.get_klines(_ssym, "1h", 25)
                _smark = float(api.get_mark_price(_ssym)["markPrice"])

                # RSI 检查
                _sc = [float(k[4]) for k in _sk1h]
                _srsi = rsi_calc(_sc, 14)
                if not (SURGE_RSI_MIN <= _srsi <= SURGE_RSI_MAX):
                    log(f"[surge] {_ssym} RSI={_srsi:.1f} 不在{SURGE_RSI_MIN}-{SURGE_RSI_MAX}，跳过")
                    continue

                # 1h 仍在上涨检查（最近一根 1h K线收盘>开盘）
                _slast_k = _sk1h[-1]
                _s_still_up = float(_slast_k[4]) > float(_slast_k[1])  # close > open
                if not _s_still_up:
                    log(f"[surge] {_ssym} 最新1h K线已转阴，跳过")
                    continue

                # 成交量放大检查
                _svols = [float(k[5]) for k in _sk1h]
                _svol_avg = sum(_svols[-21:-1]) / 20 if len(_svols) >= 21 else sum(_svols[:-1]) / max(1, len(_svols)-1)
                _svol_ratio = _svols[-1] / (_svol_avg + 1e-9)
                if _svol_ratio < SURGE_VOL_RATIO:
                    log(f"[surge] {_ssym} 成交量倍数={_svol_ratio:.1f}<{SURGE_VOL_RATIO}，量能不足跳过")
                    continue

                # 价格合理性：不追已经涨了50%以上（除非成交量极端放大）
                _schg = float(tickers[_ssym].get("priceChangePercent", 0))
                if _schg > 50 and _svol_ratio < 5.0:
                    log(f"[surge] {_ssym} 涨幅{_schg:.0f}%>50%且量能倍数{_svol_ratio:.1f}<5，风险过高跳过")
                    continue

                # 止盈止损计算
                _sentry    = _smark
                _ssl_price = round(_sentry * (1 - SURGE_SL_PCT), 6)
                _stp1      = round(_sentry * (1 + SURGE_TRAIL_PCT), 6)   # 移动止损启动线
                _stp2      = round(_sentry * 1.25, 6)                    # TP2: +25%

                # 以市价立即开仓（追涨不等回调）
                _spos_id   = f"surge_{_ssym}_{int(time.time())}"
                _snow      = time.time()
                _surge_state["positions"][_ssym] = {
                    "direction":   "LONG",
                    "entry":       _sentry,
                    "entry_price": _sentry,
                    "pos_usdt":    SURGE_POS_USDT,
                    "sl_price":    _ssl_price,
                    "tp1_price":   _stp1,
                    "tp2_price":   _stp2,
                    "trailing_sl": None,
                    "tp1_hit":     False,
                    "open_ts":     _snow,
                    "score":       999,        # 标识追涨信号
                    "sig_type":    "surge_chase",
                    "btc_trend":   _btc_now,
                    "surge_gain":  _schg,      # 追涨时的涨幅
                    "surge_vol_ratio": round(_svol_ratio, 2),
                    "surge_rsi":   round(_srsi, 1),
                    "order_mode":  "market",   # 市价单
                }
                _save_paper_state(_surge_state)
                _surge_opened += 1
                chg24_str = f"+{_schg:.1f}%"
                log(f"[surge_open] ✅ 追涨开仓 {_ssym} 涨幅{chg24_str} RSI={_srsi:.1f} "
                    f"vol_ratio={_svol_ratio:.1f}x entry={_sentry:.6g} SL={_ssl_price:.6g} TP1={_stp1:.6g}")
            except Exception as _se:
                log(f"[surge_err] {_ssym}: {_se}")
    # ── 追涨模块结束 ─────────────────────────────────────────────────────────

    # ── 第一阶段：快速评分（不调 Coinglass）── 并发化优化 ─────────────────
    # 原来串行 80 个 × 2.3s/个 ≈ 3min，并发后 ~10s
    from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed

    def _score_one(sym):
        """第6项：单标的超时保护 8s"""
        import signal as _sig
        funding = funding_map.get(sym, 0.0)
        try:
            result = score_symbol_fast(sym, tickers[sym], funding,
                                       btc_trend=_mctx.get("btc_trend", ""))
            return sym, *result
        except Exception as _se:
            log(f"[score_timeout_or_err] {sym}: {_se}")
            return sym, 0, "NEUTRAL", {}

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

    for sym, fs, direction, details in qualified:
        try:
            # ── 防复发：每轮循环先初始化所有可能的分支变量 ─────────
            # 避免 can_open/high_no_slot 分支跨越导致 UnboundLocalError
            sl_price = tp1_price = tp2_price = None
            limit_price = target_px = atr_val = pos_usdt = None
            # ────────────────────────────────────────────
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
            pos_usdt = _calc_pos_size(
                sig_type=details.get("sig_type", "trend"),
                score=fs,
                direction=direction,
                details=details,
            )
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
                # ── Bug1修复：空单在btc=up+greedy市场下加强门控 ─────────────
                # BTC上涨明显(up) → 空单需>=80；BTC下跌明显(down) → 多单需>=80
                # 新增：btc=up AND sentiment=greedy → 空单需>=85（双重强势市场拒绝逆势）
                btc_trend_now = str(details.get("btc_trend", "")).lower()
                sentiment_now = str(details.get("sentiment", _mctx.get("sentiment", ""))).lower()
                _btc_up   = "up"   in btc_trend_now
                _btc_down = "down" in btc_trend_now
                _greedy   = "greedy" in sentiment_now
                _fearful  = "fearful" in sentiment_now
                # P0优化：空单BTC协同过滤（与 fx/scanner.py v3.4.2 对齐）
                # 复盘：空头全负(7/7) — 单纯trend在任何BTC趋势下开空均亏损
                _funding_now = float(details.get("funding", 0))
                _rsi4h_now   = float(details.get("rsi_4h", 50))
                _sig_type_now = str(details.get("sig_type", "")).lower()
                _btc_is_neutral = not _btc_up and not _btc_down
                if direction == "SHORT" and _btc_up:
                    # BTC上涨：funding>=0.005% 且 rsi4h>=80
                    if not (_funding_now >= 0.00005 and _rsi4h_now >= 80):
                        log(f"[paper] {sym} btc=up 空单协同拒绝: funding={_funding_now:.5f} rsi4h={_rsi4h_now:.1f}")
                        continue
                elif direction == "SHORT" and _btc_is_neutral:
                    # BTC中性：必须是 trend_short 且 funding>=0.003%
                    if not (_sig_type_now == "trend_short" and _funding_now >= 0.00003):
                        log(f"[paper] {sym} btc=neutral 空单拒绝: sig_type={_sig_type_now} funding={_funding_now:.5f}")
                        continue
                # 其余逆势过滤逻辑保持不变
                if direction == "SHORT" and _btc_up and _greedy:
                    COUNTER_TREND_MIN = 85
                    log(f"[paper] {sym} 空单双重强势市场(btc=up+greedy)门槛提至{COUNTER_TREND_MIN}")
                elif direction == "SHORT" and _btc_up:
                    COUNTER_TREND_MIN = 80
                elif direction == "LONG" and _btc_down and _fearful:
                    COUNTER_TREND_MIN = 92
                    log(f"[paper] {sym} 多单双重弱势市场(btc=down+fearful)门槛提至{COUNTER_TREND_MIN}")
                elif direction == "LONG" and _btc_down:
                    COUNTER_TREND_MIN = 90  # 与固定100U版对齐
                else:
                    COUNTER_TREND_MIN = 75   # 默认门槛
                if direction == "SHORT" and _btc_up and fs < COUNTER_TREND_MIN:
                    log(f"[paper] {sym} 空单评分{fs:.0f}<{COUNTER_TREND_MIN}（btc=up{'+ greedy' if _greedy else ''} 逆势过滤），跳过")
                    continue
                if direction == "LONG" and _btc_down and fs < COUNTER_TREND_MIN:
                    log(f"[paper] {sym} 多单评分{fs:.0f}<{COUNTER_TREND_MIN}（btc=down{'+ fearful' if _fearful else ''} 逆势过滤），跳过")
                    continue

                # ── 相关性过滤：同集群同方向不超过上限 ──
                _ps_raw    = _load_paper_state()
                _corr_ok, _corr_reason = _check_correlation(
                    sym, direction,
                    _ps_raw.get("positions", {}),
                    _ps_raw.get("pending_orders", {}),
                )
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

                # ── SL计算：取清算图和 ATR×1.5 中更宽的一个 ──
                # 防止SL被噪音扫掉：必须至少是1.5xATR，不够宽的就放宽
                try:
                    k1h_atr = api.get_klines(sym, "1h", 30)
                    atr_val  = atr_calc(k1h_atr)
                    atr_sl   = atr_val * 1.5
                    if direction == "LONG":
                        sl_atr   = mark - atr_sl
                        sl_price = min(sl_price, sl_atr)   # 取更低（更宽）
                    else:
                        sl_atr   = mark + atr_sl
                        sl_price = max(sl_price, sl_atr)   # 取更高（更宽）
                    _sl_dist_new = abs(mark - sl_price) / mark * 100
                    log(f"[paper_sl] {sym} sl={sl_price:.5g} dist={_sl_dist_new:.2f}% atr={atr_val:.5g}")
                except Exception:
                    atr_val = None

                # ── Bug2修复：sl_price 负值/异常保护（复盘优化：上限收紧到 SL_MAX_PCT=8%）──
                # TACUSDT -44% 根因：28% 上限过宽，低价币清算图异常时SL跑飞
                from config import SL_MAX_PCT as _SL_MAX
                _sl_dist_check = abs(mark - sl_price) / (mark + 1e-9)
                if sl_price <= 0 or _sl_dist_check > _SL_MAX:
                    log(f"[paper] {sym} sl_price={sl_price:.6g} 异常(距离{_sl_dist_check*100:.1f}%>{_SL_MAX*100:.0f}%)，"
                        f"回退到ATR兜底")
                    try:
                        _atr_fb = atr_val if 'atr_val' in dir() else atr_calc(api.get_klines(sym, "1h", 30))
                        sl_price = round(mark - _atr_fb * 2.0, 8) if direction == "LONG" \
                                   else round(mark + _atr_fb * 2.0, 8)
                    except Exception:
                        sl_price = round(mark * (1 - _SL_MAX) if direction == "LONG" else mark * (1 + _SL_MAX), 6)
                    # 二次检查：兜底后进一步限制
                    _sl_dist2 = abs(mark - sl_price) / (mark + 1e-9)
                    if sl_price <= 0 or _sl_dist2 > _SL_MAX:
                        sl_price = round(mark * (1 - _SL_MAX) if direction == "LONG" else mark * (1 + _SL_MAX), 6)
                    if sl_price <= 0:
                        log(f"[paper] {sym} sl_price兜底后仍<=0，跳过")
                        continue



                # ── TP1验证：清算图TP1为主，仅当盈亏比<1:1时才兑底 ──
                try:
                    _tp1_dist = abs(tp1_price - mark) / mark
                    _sl_dist  = abs(sl_price  - mark) / mark
                    if _tp1_dist < _sl_dist:
                        _tp1_target = _sl_dist * 1.5
                        if direction == "LONG":
                            tp1_price = round(mark * (1 + _tp1_target), 8)
                        else:
                            tp1_price = round(mark * (1 - _tp1_target), 8)
                        log(f"[paper_tp1_adj] {sym} 清算图TP1盈亏比不足，兑底调整 tp1={tp1_price:.5g}(盈亏比{_tp1_target/_sl_dist:.1f}:1)")
                except Exception:
                    pass

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

                # [v2.1] 信号过期过滤：如果当前市价比挂单价高超过 3%，说明该信号已经错过入场时机
                # 继续挂单只会白占守候槽位，直接丢弃
                _gap_pct_check = (mark - limit_price) / limit_price * 100 if direction == "LONG" \
                                  else (limit_price - mark) / limit_price * 100
                if _gap_pct_check > 3.0:
                    log(f"[skip_stale] {sym} 信号已过期: 市价{mark:.6g} 比挂单价{limit_price:.6g} "
                        f"高{_gap_pct_check:.2f}%>3%，放弃")
                    continue
                # 复盘优化：snap1_miss 43% → 扩大贴近市价触发条件
                # 原：vol_ratio>2.0 且方向顺势（条件太窄）
                # 新：限价偏离现价 >1% 时直接贴近市价（0.05% 内），不管成交量
                _snap_adjusted = False
                try:
                    _vol_ratio_now = details.get("vol_ratio", 0)
                    _snap_drift = abs(mark - limit_price) / (mark + 1e-9)
                    _is_fast_market = (
                        _snap_drift > 0.01 or  # 偏离>1% 直接追价
                        (_vol_ratio_now > 2.0 and
                         ((direction == "LONG" and _btc_up) or
                          (direction == "SHORT" and _btc_down)))
                    )
                    if _is_fast_market:
                        try:
                            _tick_s = _get_sym_filters(sym).get("tick_size", 0.0001)
                        except Exception:
                            _tick_s = 0.0001
                        import math as _ms
                        _dec_s = max(0, -int(_ms.floor(_ms.log10(_tick_s)))) if _tick_s < 1 else 0
                        _agg_px = mark * (1 - 0.0005) if direction == "LONG" else mark * (1 + 0.0005)
                        _agg_px = round(round(_agg_px / _tick_s) * _tick_s, _dec_s)
                        log(f"[snap_fix] {sym} 强势行情 vol_ratio={_vol_ratio_now:.1f}"
                            f" 追价限价 {limit_price:.6g}→{_agg_px:.6g}")
                        limit_price = _agg_px
                        _snap_adjusted = True
                        details["snap_adjusted"] = True
                except Exception as _sne:
                    log(f"[snap_fix_err] {sym}: {_sne}")

                # ── 第5项：高分信号追价逻辑（v3.2）──────────────────────────
                # 触发条件：trend_score > 45 且 偏差在 ATR×0.5 ~ ATR×1.5 之间
                # 效果：限价向市价方向调整 ATR×0.3，有效期缩短到15分钟
                _entry_chased = False
                _chase_expire_min = None
                try:
                    _ts_score = details.get("trend_score", 0)
                    _atr_c = atr_calc(api.get_klines(sym, "1h", 20))
                    _dev   = abs(mark - limit_price)
                    if _ts_score > 45 and _atr_c > 0 and 0.5 * _atr_c < _dev < 1.5 * _atr_c:
                        # 追价：向市价方向移动 ATR×0.3
                        if direction == "LONG":
                            limit_price = round(limit_price + _atr_c * 0.3, 8)
                        else:
                            limit_price = round(limit_price - _atr_c * 0.3, 8)
                        _entry_chased = True
                        _chase_expire_min = 15   # 有效期15分钟
                        log(f"[chase] {sym} trend_score={_ts_score} dev={_dev:.4g} "
                            f"ATR={_atr_c:.4g} → 追价 limit={limit_price:.6g} TTL=15min")
                    elif _dev >= 1.5 * _atr_c:
                        log(f"[chase_skip] {sym} 偏差={_dev:.4g} >= 1.5×ATR={1.5*_atr_c:.4g}，放弃")
                        continue
                except Exception as _ce:
                    log(f"[chase_err] {sym}: {_ce}")
                if _entry_chased:
                    details["entry_chased"] = True
                # ─────────────────────────────────────────────────────────────

                # ── Bug4修复：重复挂单去重 — symbol+direction+价位 (v3.3) ─────────
                # 问题：QQQUSDT同价位同方向重复进入，添加本轮扫描级去重
                _dedup_key = f"{sym}_{direction}_{round(limit_price, 4)}"
                if not hasattr(_paper_place_limit_order, '_placed_keys'):
                    _paper_place_limit_order._placed_keys = set()
                if _dedup_key in _paper_place_limit_order._placed_keys:
                    log(f"[dedup] {sym} {direction} 价位{limit_price:.6g} 本轮已挂，跳过")
                    continue
                _paper_place_limit_order._placed_keys.add(_dedup_key)
                if len(_paper_place_limit_order._placed_keys) > 200:
                    _paper_place_limit_order._placed_keys = set(
                        list(_paper_place_limit_order._placed_keys)[-200:]
                    )

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
                    auto_entries.append({"sym": sym, "score": fs, "direction": direction,
                                         "limit_price": limit_price, "tp": tp1_price, "sl": sl_price,
                                         "notional": pos_usdt, "action": "paper_limit"})
                    cur_long_slots  -= (1 if direction == "LONG"  else 0)
                    cur_short_slots -= (1 if direction == "SHORT" else 0)
                else:
                    log(f"[paper] {sym} 模拟挂单被拒（仓位已满或重复）")


            elif high_no_slot:
                # 高分但无槽位：存入等待队列（第8项 v3.2）
                # ── Bug修复：sl_price/tp1_price/tp2_price 在此分支可能未赋值 ──
                # 入口已初始化 sl_price=None，若 can_open 未执行则用 ATR 兑底默认值
                _sl_safe  = sl_price  if sl_price  is not None else round(mark * (0.94 if direction=="LONG" else 1.06), 6)
                _tp1_safe = tp1_price if tp1_price is not None else round(mark * (1.10 if direction=="LONG" else 0.90), 6)
                _tp2_safe = tp2_price if tp2_price is not None else round(mark * (1.18 if direction=="LONG" else 0.84), 6)
                _tpx_safe = target_px if target_px is not None else mark
                _pos_safe = pos_usdt  if pos_usdt  is not None else MIN_POS_USDT
                _now_q = time.time()
                _q_entry = {
                    "sym": sym, "direction": direction,
                    "score": fs, "score_sum": details.get("trend_score", 0) + details.get("timing_score", 0),
                    "entry_price": _tpx_safe, "sl_price": _sl_safe,
                    "tp1_price": _tp1_safe, "tp2_price": _tp2_safe,
                    "pos_usdt": _pos_safe, "sig_type": details.get("sig_type", "trend"),
                    "details": details, "ts": _now_q,
                    "atr": atr_val if atr_val is not None else 0,
                }
                # 去重 + 按 score_sum 降序，只保留最多3个
                _SIGNAL_WAIT_QUEUE[:] = [q for q in _SIGNAL_WAIT_QUEUE
                                          if q["sym"] != sym and _now_q - q["ts"] < _SIGNAL_QUEUE_TTL]
                _SIGNAL_WAIT_QUEUE.append(_q_entry)
                _SIGNAL_WAIT_QUEUE.sort(key=lambda x: -x["score_sum"])
                if len(_SIGNAL_WAIT_QUEUE) > _SIGNAL_QUEUE_MAX:
                    _SIGNAL_WAIT_QUEUE[:] = _SIGNAL_WAIT_QUEUE[:_SIGNAL_QUEUE_MAX]
                log(f"[wait_queue] {sym} {direction} 加入等待队列 score={fs} 队列长度={len(_SIGNAL_WAIT_QUEUE)}")
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
        f"[模拟] 📊 扫描完成｜耗时 {int(elapsed)}s",
        f"历史已实现：{_realized:+.2f}U（胜{_pstats.get('wins',0)}败{_pstats.get('losses',0)}）  当前浮动：{_float_pnl:+.2f}U",
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
    log("\n".join(brief_lines))   # 轮报静默，只写日志


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
            (_hp.Path(WORKSPACE) / "ws_cache" / "paper_scanner_heartbeat.json").write_text(
                _hj.dumps({"ts": _ht.time(), "status": "starting"}, indent=2))
        except Exception:
            pass
        log(f"[main] scanner 启动（daemon），扫描间隔 {SCAN_INTERVAL//60} 分钟")

        # ── 独立止损心跳：每 30 秒检查一次 SL/TP ──
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
                _tb_str = _tb.format_exc()
                log(_tb_str[:800])
                # 同时写入独立错误文件，避免被 LOG_MAX_LINES 截断
                try:
                    with open(f"{WORKSPACE}/paper_scanner_errors.log", "a") as _ef:
                        import datetime as _edt
                        _ef.write(f"\n[{_edt.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}]\n{_tb_str}\n")
                except Exception:
                    pass
            if not _running:
                break
            log(f"[main] 下一轮扫描在 {SCAN_INTERVAL//60} 分钟后")
            # 整段 sleep，用 Event 响应 SIGTERM，CPU 友好
            _stop_event.wait(timeout=SCAN_INTERVAL)
