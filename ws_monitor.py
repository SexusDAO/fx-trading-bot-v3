#!/usr/bin/env python3
"""
ws_monitor_new.py - WebSocket 监控系统(精简重写版)
架构:log → notify → trigger_agent → gates → ws → patrol → main
目标:600~900 行,职责单一,线程安全
"""

import json, os, sys, time, threading, subprocess, importlib.util, pathlib
from datetime import datetime, timezone

sys.path.insert(0, "/root/.openclaw/workspace")
import binance_api as api
from pos_state_store import store as _ps_store  # P0: 统一 pos_state 写入
from risk_manager import RiskManager; _risk = RiskManager()
from notifier import notifier as _notifier
from config_watcher import config as _cfg
from trade_logger import log_open, log_close
import dual_net_orders as _dn  # 双网挂单策略 ─────────────────────────────────────────────────────────────────────
WORKSPACE     = "/root/.openclaw/workspace"
JARVIS_TARGET = "432401404:thread:019f9203-3b29-7d9e-bfb4-b67c0b06587c"
LOG_FILE      = f"{WORKSPACE}/ws_monitor.log"
GATES_FILE    = f"{WORKSPACE}/price_gates.json"
POS_STATE     = f"{WORKSPACE}/pos_state.json"
CIRCUIT_FILE  = f"{WORKSPACE}/circuit_breaker.json"
COUNTER_FILE  = f"{WORKSPACE}/add_position_counter.json"
CONFIG_FILE   = f"{WORKSPACE}/strategy-config.json"
SCAN_TRIGGER  = f"{WORKSPACE}/config/scan_trigger.json"

os.makedirs(f"{WORKSPACE}/ws_cache", exist_ok=True)

# ─── 1. log(msg) ─────────────────────────────────────────────────────────────

def log(msg: str):
    """只写文件,不 print,最多保留 500 行"""
    ts = datetime.now(timezone.utc).strftime("%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}\n"
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line)
        with open(LOG_FILE) as f:
            lines = f.readlines()
        if len(lines) > 600:
            with open(LOG_FILE, "w") as f:
                f.writelines(lines[-500:])
    except Exception:
        pass


# ─── 2. notify(msg) ──────────────────────────────────────────────────────────

def notify(msg: str):
    """推送到 JARVIS_TARGET(委托给 notifier 模块)"""
    _notifier.normal(msg)


# ─── 3. trigger_agent(prompt, label) ────────────────────────────────────────

def trigger_agent(prompt: str, label: str):
    """启动隔离 Agent 执行 prompt,推送结果到 JARVIS_TARGET"""
    try:
        subprocess.Popen(
            [
                "openclaw", "run",
                "--label", label,
                "--to", JARVIS_TARGET,
                "--announce", prompt,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        log(f"[trigger_agent] label={label}")
    except Exception as e:
        log(f"[trigger_agent_err] {e}")


# ─── 4. binance_api 直接 import,无额外封装 ──────────────────────────────────
# 使用:api.get_mark_price(), api.get_klines(), api.get_algo_open_orders_cached(), 等

# ─── 5. 熔断模块(已迁移至 risk_manager,以下为向下兼容别名)────────────────────
# 内部逻辑已移至 risk_manager.py,ws_monitor 通过 _risk 实例调用。
# 以下函数保留作向下兼容别名,避免其他脚本直接 import 时报错。


def _get_daily_loss_limit() -> float:
    """向下兼容别名 → 委托 config_watcher 读取"""
    return _cfg.get("risk.daily_loss_circuit_breaker_usdt",
                    _cfg.get("daily_loss_limit", 100.0))


def load_circuit() -> dict:
    """向下兼容别名 → _risk.load_circuit()"""
    return _risk.load_circuit()


def save_circuit(data: dict):
    """向下兼容别名 → _risk.save_circuit()"""
    _risk.save_circuit(data)


def add_loss(pnl_u: float):
    """向下兼容别名 → _risk.add_loss()"""
    _risk.add_loss(pnl_u)


def write_scan_trigger(reason: str):
    """
    写入 scan_trigger.json,通知 scanner 在下次运行时立即执行完整扫描。
    reason 示例: 'position_closed', 'tp1_triggered', 'time_sl'
    """
    try:
        import json as _json
        tmp = SCAN_TRIGGER + ".tmp"
        with open(tmp, "w") as _f:
            _json.dump({"trigger": True, "reason": reason,
                        "ts": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")}, _f)
        os.replace(tmp, SCAN_TRIGGER)
        log(f"[scan_trigger] 写入 flag reason={reason}")
    except Exception as _e:
        log(f"[scan_trigger] 写入失败: {_e}")


def is_circuit_open() -> bool:
    """向下兼容别名 → _risk.is_circuit_open()"""
    return _risk.is_circuit_open()



# ─── 6. Price Gates 模块 ─────────────────────────────────────────────────────

_gates_lock     = threading.Lock()
_gates_init     = set()    # 已注册的 gate id,防重复
_fired_lock     = threading.Lock()
_gates_mem: dict = {}      # 内存级 gates 缓存,避免 handle_ticker 频繁读文件

def _load_gates() -> dict:
    """优先返回内存缓存,没有时从文件读。"""
    global _gates_mem
    if _gates_mem:
        return _gates_mem
    try:
        with open(GATES_FILE) as f:
            _gates_mem = json.load(f)
            return _gates_mem
    except Exception:
        return {}


def _save_gates(data: dict):
    global _gates_mem
    _gates_mem = data   # 同步内存缓存
    try:
        tmp = GATES_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, GATES_FILE)
    except Exception as e:
        log(f"[gates_save_err] {e}")


def _invalidate_gates_cache():
    """gates 文件被外部修改时强制刷新内存缓存。"""
    global _gates_mem
    _gates_mem = {}


def _load_pos_state() -> dict:
    try:
        with open(POS_STATE) as f:
            return json.load(f)
    except Exception:
        return {}


def _build_move_sl_prompt(sym: str, side: str, qty, entry: float,
                          sl: float, price: float,
                          sl_target: float = 0.0,
                          tier_str: str = "保本") -> str:
    """
    生成止损上移指令。
    sl_target: 已由门控规则确定的目标止损价,直接使用。
    tier_str: '保本' | '锁利'
    """
    if sl_target <= 0:
        sl_target = round(entry * 1.001, 6) if side == "LONG" else round(entry * 0.999, 6)
    close_side = "SELL" if side == "LONG" else "BUY"
    return (
        f"[{sym} 止损上移,{tier_str}门控]\n"
        f"价格已到达 {price},触发{tier_str}门控,执行止损上移。\n\n"
        f"持仓: {side} {qty}张 @ 均价{entry} | 当前SL:{sl} | 目标SL:{sl_target}\n\n"
        f"执行步骤(直接执行,无需确认):\n"
        f"1. import sys,json,os; sys.path.insert(0,'/root/.openclaw/workspace'); import binance_api as api\n\n"
        f"2. new_sl = {sl_target}  # 已由门控规则写死,直接使用\n"
        f"   # 安全校验\n"
        f"   if '{side}' == 'LONG' and new_sl <= {sl}:\n"
        f"       print('安全检查失败:目标SL未向盈利方向移动,退出'); exit()\n"
        f"   if '{side}' == 'SHORT' and new_sl >= {sl}:\n"
        f"       print('安全检查失败:目标SL未向盈利方向移动,退出'); exit()\n\n"
        f"3. algos = api.get_algo_open_orders('{sym}')\n"
        f"   for a in algos:\n"
        f"       if 'STOP' in a.get('orderType',''):\n"
        f"           api.cancel_algo_order('{sym}', int(a['algoId']))\n\n"
        f"4. r = api.place_algo_order('{sym}', '{close_side}', 'STOP_MARKET', new_sl, '{side}', quantity={qty})\n"
        f"   if not r.get('algoId'):\n"
        f"       print('挂单失败:', r); exit()\n"
        f"   print('成功: algoId=', r['algoId'])\n\n"
        f"5. PS_FILE = '/root/.openclaw/workspace/pos_state.json'\n"
        f"   ps = json.load(open(PS_FILE)) if os.path.exists(PS_FILE) else {{}}\n"
        f"   if '{sym}' not in ps: ps['{sym}'] = {{}}\n"
        f"   ps['{sym}']['sl_price']   = new_sl\n"
        f"   ps['{sym}']['sl_algo_id'] = r.get('algoId')\n"
        f"   json.dump(ps, open(PS_FILE,'w'), indent=2)\n\n"
        f"6. print(f'播报: ✅ {sym} 止损已上移 @{{new_sl}}({tier_str}门控)')\n\n"
        f"播报格式:✅ {sym} 止损已上移 @{{new_sl}}({tier_str}),{sl} → {{new_sl}}\n"
    )

def register_gates(symbol: str, entry: float, qty, side: str,
                   sl: float, tp1: float, tp2: float,
                   atr1h: float = 0.0):
    """
    开仓成交后或启动恢复时调用;用 _gates_init 防重复注册。

    两档门控规则:
    ┌─ 第一档(保本)────────────────────────────────────────────────┐
    │  触发:浮盈 +5%  OR  TP1 成交                                 │
    │  动作:止损上移至 entry * 1.001(手续费缓冲保本)              │
    └────────────────────────────────────────────────────────────────┘
    ┌─ 第二档(锁利)────────────────────────────────────────────────┐
    │  触发:浮盈 +10% OR  TP2 成交                                 │
    │  动作:止损上移至 entry + ATR(1h)*1.5(写死,开仓时计算)      │
    │        TP2 成交时额外规则:止损移到 TP1 价格                   │
    └────────────────────────────────────────────────────────────────┘
    原则:止损只能向盈利方向移动,不得反向放宽。
    """
    with _gates_lock:
        gates     = _load_gates()
        sym_gates = gates.get(symbol, [])
        existing_ids = {g["id"] for g in sym_gates}

        # 第一档止损目标(保本):优先用 Coinglass 最近清算区作为保本锚点
        # 逻辑:开仓后下方(多单)/ 上方(空单)最近清算区,比固定 entry*1.001 更有意义
        try:
            import coinglass_api as _cg_gate
            _cg_zones_gate = _cg_gate.get_liq_zones(symbol, entry)
            if side == "LONG":
                # 多单保本:下方最近清算区(但不能低于 entry,否则还是亏)
                # 取在 entry*0.995 ~ entry 之间的清算区,即"刚刚保本区域"
                _be_candidates = [z for z in _cg_zones_gate.get("below", [])
                                   if entry * 0.990 < z[0] < entry]
                sl_breakeven = round(_be_candidates[0][0] * 0.999, 6) if _be_candidates                                else round(entry + atr1h * 0.1 if atr1h > 0 else entry * 1.001, 6)
                # 第二档锁利:下方第二个显著清算区(或 ATR×1.5)
                _lock_candidates = [z for z in _cg_zones_gate.get("above", [])
                                     if z[0] > entry * 1.01]
                sl_lock = round(max(_lock_candidates, key=lambda z: z[1])[0] * 0.999, 6)                           if _lock_candidates else                           (round(entry + atr1h * 1.5, 6) if atr1h > 0 else round(entry * 1.03, 6))
            else:  # SHORT
                _be_candidates = [z for z in _cg_zones_gate.get("above", [])
                                   if entry < z[0] < entry * 1.010]
                sl_breakeven = round(_be_candidates[0][0] * 1.001, 6) if _be_candidates                                else round(entry - atr1h * 0.1 if atr1h > 0 else entry * 0.999, 6)
                _lock_candidates = [z for z in _cg_zones_gate.get("below", [])
                                     if z[0] < entry * 0.99]
                sl_lock = round(min(_lock_candidates, key=lambda z: z[0])[0] * 1.001, 6)                           if _lock_candidates else                           (round(entry - atr1h * 1.5, 6) if atr1h > 0 else round(entry * 0.97, 6))
        except Exception as _cg_gate_err:
            # fallback: ATR 动态保本/锁利
            log(f"[gate_reg] {symbol} CG查询失败,使用ATR fallback: {_cg_gate_err}")
            if side == "LONG":
                sl_breakeven = round(entry + (atr1h * 0.1 if atr1h > 0 else entry * 0.001), 6)
                sl_lock = round(entry + (atr1h * 1.5 if atr1h > 0 else entry * 0.03), 6)
            else:
                sl_breakeven = round(entry - (atr1h * 0.1 if atr1h > 0 else entry * 0.001), 6)
                sl_lock = round(entry - (atr1h * 1.5 if atr1h > 0 else entry * 0.03), 6)

        def _add(gate_id, price, direction, action, **extra):
            if gate_id in _gates_init or gate_id in existing_ids:
                return
            gate_obj = {
                "id":        gate_id,
                "symbol":    symbol,
                "price":     price,
                "direction": direction,
                "action":    action,
                "fired":     False,
                "entry":     entry,
                "qty":       str(qty),
                "side":      side,
                "sl":        sl,
                "sl_target": extra.get("sl_target", sl_breakeven),
                "tp1":       tp1,
                "tp2":       tp2,
            }
            gate_obj.update(extra)
            sym_gates.append(gate_obj)
            _gates_init.add(gate_id)
            log(f"[gate_reg] {gate_id} @ {price} dir={direction} sl_target={gate_obj['sl_target']}")

        # gate1/gate2 触发价：用 ATR 动态计算（atr1h>0时），fallback 固定百分比
        # v2.9修复: gate1 ATR倍数从1.5提高到2.0
        # 原因: GIGGLEUSDT案例—倒滞等到浮盈ATR×1.5才移保本，主升行情第一波可能小于ATR×1.5
        # 加宽后给行情更多呼吸空间，不要过早被微小回调扫出
        # gate2 = entry ± ATR×3.5（原3.0）一并拖宽
        if atr1h > 0:
            gate1_price_l = round(entry + atr1h * 2.0, 6)
            gate2_price_l = round(entry + atr1h * 3.5, 6)
            gate1_price_s = round(entry - atr1h * 2.0, 6)
            gate2_price_s = round(entry - atr1h * 3.5, 6)
        else:
            gate1_price_l = round(entry * 1.05, 6)
            gate2_price_l = round(entry * 1.10, 6)
            gate1_price_s = round(entry * 0.95, 6)
            gate2_price_s = round(entry * 0.90, 6)

        if side == "LONG":
            # 第一档:浮盈 1.5 ATR 触发保本
            _add(f"{symbol}_gate1_pnl",
                 gate1_price_l, "above", "move_sl",
                 sl_target=sl_breakeven, gate_tier=1)
            # 第二档:浮盈 3.0 ATR 触发锁利
            _add(f"{symbol}_gate2_pnl",
                 gate2_price_l, "above", "move_sl",
                 sl_target=sl_lock, gate_tier=2)
            # TP 接近通知(提前 1.5% 预警)-- BUG-D2 修复:tp=0 时不注册,防止门控价格为 0 立即触发
            if tp1 > 0:
                _add(f"{symbol}_tp1_near", round(tp1 * 0.985, 6), "above", "notify")
            if tp2 > 0:
                _add(f"{symbol}_tp2_near", round(tp2 * 0.985, 6), "above", "notify")
        else:  # SHORT
            _add(f"{symbol}_gate1_pnl",
                 gate1_price_s, "below", "move_sl",
                 sl_target=sl_breakeven, gate_tier=1)
            _add(f"{symbol}_gate2_pnl",
                 gate2_price_s, "below", "move_sl",
                 sl_target=sl_lock, gate_tier=2)
            if tp1 > 0:
                _add(f"{symbol}_tp1_near", round(tp1 * 1.015, 6), "below", "notify")
            if tp2 > 0:
                _add(f"{symbol}_tp2_near", round(tp2 * 1.015, 6), "below", "notify")

        gates[symbol] = sym_gates
        _save_gates(gates)
        log(f"[gate_reg] {symbol} done | sl_breakeven={sl_breakeven} sl_lock={sl_lock} atr1h={atr1h:.6f}")

def clear_gates(symbol: str):
    """平仓成交后调用"""
    with _gates_lock:
        gates = _load_gates()
        if symbol in gates:
            del gates[symbol]
            _save_gates(gates)
        # 清理 _gates_init 中该 symbol 的记录
        to_remove = {gid for gid in _gates_init if gid.startswith(f"{symbol}_")}
        for gid in to_remove:
            _gates_init.discard(gid)
    log(f"[gates] cleared for {symbol}")


def _execute_move_sl(sym: str, side: str, qty, sl_old: float,
                     sl_target: float, tier_str: str, trigger_price: float):
    """
    直接执行止损上移,不走 agent。
    撤旧 STOP 单 → 挂新止损 → 更新 pos_state → 播报。
    """
    import os as _os
    close_side = "SELL" if side == "LONG" else "BUY"
    try:
        # 1. 安全校验
        if side == "LONG" and sl_target <= sl_old:
            log(f"[move_sl] 安全检查失败: sl_target={sl_target} <= sl_old={sl_old},跳过")
            return
        if side == "SHORT" and sl_target >= sl_old:
            log(f"[move_sl] 安全检查失败: sl_target={sl_target} >= sl_old={sl_old},跳过")
            return

        # 2. 撤除旧止损单
        algos = api.get_algo_open_orders(sym)
        for a in algos:
            if "STOP" in a.get("orderType", ""):
                api.cancel_algo_order(sym, int(a["algoId"]))
                log(f"[move_sl] 撤除旧止损 algoId={a['algoId']}")

        # 3. 挂新止损单
        r = api.place_algo_order(sym, close_side, "STOP_MARKET",
                                 sl_target, side, quantity=qty)
        if not r.get("algoId"):
            raise ValueError(f"挂单失败: {r}")
        log(f"[move_sl] 新止损已挂 @{sl_target} algoId={r['algoId']}")

        # 4. 更新 pos_state(原子写,防止崩溃时文件损坏)
        PS = POS_STATE
        ps = json.load(open(PS)) if _os.path.exists(PS) else {}
        ps.setdefault(sym, {})
        ps[sym]["sl_price"]   = sl_target
        ps[sym]["sl_algo_id"] = r.get("algoId")
        _tmp = PS + ".tmp"
        json.dump(ps, open(_tmp, "w"), indent=2)
        _os.replace(_tmp, PS)

        # 4b. 更新 algo_cache(BUG-C06 修复:move_sl 后同步缓存,防止 patrol 用旧 algoId)
        try:
            api.algo_cache_save(sym, "STOP_MARKET", int(r["algoId"]), str(sl_target))
        except Exception as _ce:
            log(f"[move_sl] algo_cache_save 失败: {_ce}")

        # 5. 将新 sl 同步到内存中的门控记录(防止后续门控反向放宽)
        with _gates_lock:
            gates = _load_gates()
            for g in gates.get(sym, []):
                if g.get("action") == "move_sl" and not g.get("fired"):
                    g["sl"] = sl_target  # 更新未触发门控的基准 sl
            _save_gates(gates)

        # 6. 播报
        notify(
            f"✅ [{sym}] 止损已上移({tier_str}门控)\n"
            f"  {sl_old:.6f} → {sl_target:.6f}\n"
            f"  触发价 {trigger_price:.6f}"
        )
        log(f"[move_sl] 完成 {sym} {sl_old} → {sl_target} ({tier_str})")

    except Exception as e:
        log(f"[move_sl_err] {sym}: {e}")
        # 自动重试一次(网络抖动/限频等可恢复错误)
        try:
            import time as _time; _time.sleep(3)
            r2 = api.place_algo_order(sym, close_side, "STOP_MARKET",
                                      sl_target, side, quantity=qty)
            if r2.get("algoId"):
                log(f"[move_sl_retry] {sym} 重试成功 @{sl_target} algoId={r2['algoId']}")
                return
        except Exception as e2:
            log(f"[move_sl_retry_err] {sym}: {e2}")
        # 两次均失败 → 止损关键操作,需要人工介入
        notify(f"⚠️ [{sym}] 止损上移连续失败,需手动处理\n  目标: {sl_target:.6f}\n  原因: {e}")


def _fire_gate(gate: dict, price: float):
    """执行门控动作"""
    gid       = gate["id"]
    sym       = gate["symbol"]
    action    = gate["action"]
    entry     = float(gate.get("entry", 0))
    qty       = gate.get("qty", 0)
    side      = gate.get("side", "LONG")
    sl_old    = float(gate.get("sl", 0))
    sl_target = float(gate.get("sl_target", entry * 1.001))
    gate_tier = gate.get("gate_tier", 1)

    if action == "notify":
        notify(f"🔔 [{sym}] 接近止盈位 {gid} @ {price:.6f}")

    elif action == "move_sl":
        tier_str = "保本" if gate_tier == 1 else "锁利"
        # 直接执行,不走 agent,由 threading 防止阻塞 patrol loop
        threading.Thread(
            target=_execute_move_sl,
            args=(sym, side, qty, sl_old, sl_target, tier_str, price),
            daemon=True
        ).start()

    log(f"[gate_fire] {gid} action={action} price={price} sl_target={sl_target}")


def check_gates(sym: str, price: float):
    """每个 ticker 更新时调用,O(n) 扫描,命中则触发(线程安全)"""
    with _gates_lock:
        gates = _load_gates()
        sym_gates = gates.get(sym, [])
        changed = False
        for gate in sym_gates:
            if gate.get("fired"):
                continue
            g_price = float(gate["price"])
            direction = gate["direction"]
            triggered = (
                (direction == "above" and price >= g_price) or
                (direction == "below" and price <= g_price)
            )
            if triggered:
                gate["fired"] = True
                changed = True
                threading.Thread(
                    target=_fire_gate, args=(dict(gate), price), daemon=True
                ).start()
        if changed:
            gates[sym] = sym_gates
            _save_gates(gates)


def restore_gates_from_pos_state():
    """启动时从 POS_STATE 恢复 gates,并清洗无实际仓位的残留记录"""
    ps = _load_pos_state()

    # Fix-A: 启动时比对实际仓位,清理 pos_state 里的幽灵记录
    # 防止 preempt_sl 对无仓位标的反复触发市价平仓
    try:
        live_positions = api.get_positions()
        live_syms = {p["symbol"] for p in live_positions
                     if abs(float(p.get("positionAmt", 0))) > 0}
        stale = _ps_store.clear_stale(live_syms)
        if stale:
            log(f"[gates] 清理无仓位残留 pos_state: {stale}")
    except Exception as _ge:
        log(f"[gates] pos_state 清洗失败(非致命): {_ge}")

    for sym, pos in ps.items():
        entry = float(pos.get("entry_price", 0))
        qty   = pos.get("qty", 0)
        side  = pos.get("direction", "LONG")
        atr1h = float(pos.get("atr1h", 0.0))  # 开仓时写入的 ATR
        # fallback sl/tp: ATR动态(如果 atr1h=0 则用保守3%)
        _atr_fb = atr1h if atr1h > 0 else entry * 0.03
        _side_f = pos.get("direction", "LONG")
        sl    = float(pos.get("sl_price",   entry - _atr_fb * 2.0 if _side_f == 'LONG' else entry + _atr_fb * 2.0))
        tp1   = float(pos.get("tp1_price",  entry + _atr_fb * 2.0 if _side_f == 'LONG' else entry - _atr_fb * 2.0))
        tp2   = float(pos.get("tp2_price",  entry + _atr_fb * 3.5 if _side_f == 'LONG' else entry - _atr_fb * 3.5))
        if entry > 0 and qty:
            register_gates(sym, entry, qty, side, sl, tp1, tp2, atr1h=atr1h)
    log(f"[gates] restored from pos_state: {list(ps.keys())}")


# ─── 7. WebSocket 模块 ───────────────────────────────────────────────────────

_ticker_cache     = {}   # sym → price
_ticker_cache_raw = {}   # sym → raw ticker dict
_dn_vol_avg       = {}   # sym → 近20根1H K线平均量(由 scanner_loop 每15min更新)
_dn_kline_vol_cur = {}   # sym → 最新一根1H K线量(由 scanner_loop 每15min更新)
_rsi_cache        = {}   # sym → 最新1H RSI(由 scanner_loop 更新)
_ticker_lock    = threading.Lock()

# ─── 急跌/急涨异动检测 ────────────────────────────────────────────────────────
_price_snapshots: dict = {}   # sym → [(timestamp, price), ...] 保留近30分钟
_surge_cooldown:  dict = {}   # sym → cooldown过期时间戳(2小时)
_surge_hourly:    list = []   # 本小时已触发标的时间戳列表(限3个/小时)
_surge_lock             = threading.Lock()
SURGE_WINDOW_SEC  = 15 * 60   # 15 分钟窗口
SURGE_THRESHOLD   = 0.05      # 5% 涨跌触发
SURGE_COOLDOWN    = 2 * 3600  # 2 小时冷却
SURGE_HOURLY_MAX  = 3         # 每小时最多触发3个标的
SURGE_SCORE_AUTO  = 65        # ≥65 自动触发评分+挂单
SURGE_SCORE_NOTIFY = 60       # 60~65 仅通知
_SURGE_MONITOR_SYMS: set = set()  # 主动监控的标的(ws 全量)
_streams_started = False
_stop_event     = threading.Event()

WS_FAPI_BASE = "wss://fstream.binance.com"
RECONNECT_DELAY = 5


def run_stream(name: str, handler, url: str = ""):
    """
    用 binance-cli 子进程接收 WS 数据,完全绕开 OpenClaw MITM 代理。
    旧版本验证过可行,新版本统一改回这个方式。
    url 参数保留兼容性,实际用 name 决定 CLI 命令。
    """
    STREAM_CMDS = {
        "ticker":    "binance-cli futures-usds-streams all-market-tickers-streams --json 2>/dev/null",
        "markprice": "binance-cli futures-usds-streams mark-price-stream-for-all-market --json 2>/dev/null",
        # 优化二:1H kline实时推送,仅订阅双网活跃标的,动态由 _dn_get_kline_symbols() 提供
        "kline_1h":  "_dynamic_kline_1h",
    }
    cmd = STREAM_CMDS.get(name)
    if not cmd:
        log(f"[{name}] 未知 stream 类型,跳过")
        return

    # 优化二:kline_1h 动态构建订阅列表
    if cmd == "_dynamic_kline_1h":
        _kl_last_syms: list = []
        _kl_retry_delay = 3    # 初始重试间隔(秒)
        _kl_max_delay   = 120  # 最大重试间隔(秒),网络不通时避免刷屏
        while not _stop_event.is_set():
            syms = _dn.get_active_syms()
            if not syms:
                time.sleep(30)
                continue
            # 构建订阅 URL:<sym>@kline_1h/...
            streams = "/".join(f"{s.lower()}@kline_1h" for s in syms)
            cmd_dyn = (
                f'python3 -c "'
                f'import websocket,json,sys; '
                f'url=\"wss://fstream.binance.com/stream?streams={streams}\"; '
                f'ws=websocket.create_connection(url,timeout=60); ws.settimeout(90); '
                f'[print(json.dumps(json.loads(ws.recv())[\"data\"])) for _ in range(86400)]" '
                f'2>/dev/null'
            )
            # 只在首次或标的变化时打印订阅日志(避免连接失败时刷屏)
            if set(syms) != set(_kl_last_syms):
                log(f"[kline_1h] 订阅: {syms}")
                _kl_last_syms = list(syms)
            try:
                proc = subprocess.Popen(
                    cmd_dyn, shell=True, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, text=True, bufsize=1
                )
                last_msg = time.time()
                def _kl_watchdog():
                    while not _stop_event.is_set():
                        time.sleep(60)
                        if time.time() - last_msg > 120:
                            try: proc.kill()
                            except Exception: pass
                            return
                threading.Thread(target=_kl_watchdog, daemon=True).start()
                for raw in proc.stdout:
                    if _stop_event.is_set():
                        break
                    raw = raw.strip()
                    if not raw:
                        continue
                    last_msg = time.time()
                    try:
                        handle_kline_1h(json.loads(raw))
                    except Exception as e:
                        log(f"[kline_1h_handler_err] {e}")
                    # 若活跃标已变化,重建订阅
                    new_syms = _dn.get_active_syms()
                    if set(new_syms) != set(syms):
                        log(f"[kline_1h] 活跃标变化 {syms}->{new_syms},重建订阅")
                        try: proc.kill()
                        except Exception: pass
                        break
            except Exception as e:
                log(f"[kline_1h_err] {e}")
            time.sleep(3)
        return

    log(f"[{name}] 启动(binance-cli 子进程)")
    while not _stop_event.is_set():
        try:
            proc = subprocess.Popen(
                cmd, shell=True, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, bufsize=1
            )
            for raw in proc.stdout:
                if _stop_event.is_set():
                    break
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    msg = json.loads(raw)
                    handler(msg)
                except Exception as e:
                    log(f"[{name}_handler_err] {e}")
        except Exception as e:
            log(f"[{name}_proc_err] {e}")
        finally:
            try:
                proc.terminate()
            except Exception:
                pass

        if not _stop_event.is_set():
            # BUG-C07 修复:指数退避(最大 60s),防止连续重连触发 API 限流
            reconnect_delay = getattr(run_stream, f'_delay_{name}', RECONNECT_DELAY)
            log(f"[{name}] 子进程退出,{reconnect_delay}s 后重启...")
            time.sleep(reconnect_delay)
            setattr(run_stream, f'_delay_{name}', min(reconnect_delay * 2, 60))


def run_user_data_stream(listen_key: str, handler):
    """
    user-data stream 单独走 binance-cli,需要 listenKey。
    独立线程每 20 分钟 keepalive,主循环监测子进程健康并自动重连。
    """
    cmd = f'binance-cli futures-usds-streams user-data --listen-key "{listen_key}" --json 2>/dev/null'
    log(f"[user_data] 启动(listenKey={listen_key[:8]}...)")

    # 独立 keepalive 线程(每 20 分钟,不依赖数据轮转)
    _lk_box = [listen_key]   # 用列表包装,规避 Python 闭包赋值限制
    def _keepalive_loop():
        while not _stop_event.is_set():
            time.sleep(1200)  # 20 分钟
            if _stop_event.is_set():
                break
            try:
                r = api.keep_alive_listen_key(_lk_box[0])
                if r.get('ok'):
                    # 如果代理返回了新 key,更新并重启子进程
                    if r.get('new_key') and r['new_key'] != _lk_box[0]:
                        _lk_box[0] = r['new_key']
                        cmd = f'binance-cli futures-usds-streams user-data --listen-key "{_lk_box[0]}" --json 2>/dev/null'
                        log(f"[user_data] listenKey 已更新,正在重连 ({_lk_box[0][:8]}...)")
                        try: proc.kill()
                        except Exception: pass
                    else:
                        log("[user_data] keepalive OK")
                else:
                    log(f"[user_data_keepalive_err] {r}")
            except Exception as e:
                log(f"[user_data_keepalive_err] {e}")
    threading.Thread(target=_keepalive_loop, daemon=True).start()

    while not _stop_event.is_set():
        try:
            proc = subprocess.Popen(
                cmd, shell=True, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, bufsize=1
            )
            last_msg_time = time.time()

            # BUG-C01 修复 + 优化:独立守护线程检测 2min 无消息则强杀子进程(原 10min,过长导致成交事件错过)
            def _watchdog():
                while not _stop_event.is_set():
                    time.sleep(15)
                    if time.time() - last_msg_time > 120:
                        log("[user_data] 超过 2min 无消息,主动重连")
                        try: proc.kill()
                        except Exception: pass
                        return
            threading.Thread(target=_watchdog, daemon=True).start()

            for raw in proc.stdout:
                if _stop_event.is_set():
                    break
                raw = raw.strip()
                if not raw:
                    continue
                last_msg_time = time.time()
                try:
                    msg = json.loads(raw)
                    handler(msg)
                except Exception as e:
                    log(f"[user_data_handler_err] {e}")
        except Exception as e:
            log(f"[user_data_proc_err] {e}")
        finally:
            try:
                proc.terminate()
            except Exception:
                pass

        if not _stop_event.is_set():
            log(f"[user_data] 子进程退出,{RECONNECT_DELAY}s 后重启...")
            time.sleep(RECONNECT_DELAY)

# ── 观察区实时监控 ────────────────────────────────────────────────────────────
_wl_fired: dict = {}
_wl_hit_pending: dict = {}   # 观察区触达目标价但尚未播报的记录,等下轮扫描入报
_WL_COOLDOWN  = 3600
_WL_FIRED_FILE = f"{WORKSPACE}/wl_fired.json"
_WL_SCORE_MIN  = 70   # 低于此分不推送

def _load_wl_fired():
    global _wl_fired
    try:
        import json as _j
        if os.path.exists(_WL_FIRED_FILE):
            data = _j.load(open(_WL_FIRED_FILE))
            _wl_fired = {k: float(v) for k, v in data.items()}
    except Exception:
        _wl_fired = {}

def _save_wl_fired():
    try:
        import json as _j
        _j.dump(_wl_fired, open(_WL_FIRED_FILE, "w"), indent=2)
    except Exception:
        pass

WL_TTL_HOURS = 24  # watchlist 有效期(小时)


def _purge_stale_watchlist_ws(wl: dict) -> tuple[dict, list]:
    """ws_monitor 内部清理函数,逻辑与 scanner 一致"""
    now = time.time()
    cutoff = now - WL_TTL_HOURS * 3600
    removed, cleaned = [], {}
    for k, v in wl.items():
        if k in ("candidates", "last_updated") or not isinstance(v, dict):
            cleaned[k] = v; continue
        added_str = v.get("added_at", "")
        try:
            from datetime import datetime as _dt2
            ts = _dt2.fromisoformat(added_str.replace("Z", "+00:00")).timestamp()
            if ts < cutoff:
                removed.append(k); continue
        except Exception:
            pass
        cleaned[k] = v
    return cleaned, removed


# ── PIN 信号已挂单记录(防重复,key=sym,value=过期时间)────────────────────────
_pin_orders: dict = {}   # sym -> {"order_id": int, "expire_at": float, "direction": str}

def _handle_pin_signal(sym: str, price: float, entry: dict, wl: dict, wl_file: str):
    """
    PIN_LONG / PIN_SHORT 插针信号专属处理。
    - 立即挂限价单,不等常规扫描周期
    - PIN_LONG : 挂单价 = 插针低点上方 1%,止损 = 低点下方 0.5%,仓位 250U
    - PIN_SHORT: 挂单价 = 插针高点下方 1%,止损 = 高点上方 0.5%,仓位 250U
    - 有效期 30 分钟,过期自动撤单(静默)
    """
    import json as _j2
    import math as _math

    sig_type   = entry.get("sig_type", "")
    expire_at  = float(entry.get("expire_at", 0))
    now_ts     = time.time()

    # 已过期 → 移出观察区(静默)
    if now_ts > expire_at:
        wl.pop(sym, None)
        try:
            _j2.dump(wl, open(wl_file, "w"), indent=2, ensure_ascii=False)
        except Exception:
            pass
        log(f"[pin] {sym} 插针信号已过期,静默移出观察区")
        return

    # 已有挂单且未过期 → 跳过
    pin_rec = _pin_orders.get(sym)
    if pin_rec and now_ts < pin_rec.get("expire_at", 0):
        # 检查挂单是否还存在
        try:
            open_orders = api.request("GET", api.BASE_FAPI + "/fapi/v1/openOrders", {})
            still_open = any(int(o.get("orderId", 0)) == pin_rec["order_id"]
                             for o in (open_orders if isinstance(open_orders, list) else []))
        except Exception:
            still_open = True
        if still_open:
            return
        # 挂单已消失(成交或手动撤销)→ 清理记录
        _pin_orders.pop(sym, None)

    # 过期撤单检查(对上一轮留下的旧PIN单)
    if pin_rec and now_ts >= pin_rec.get("expire_at", 0):
        old_oid = pin_rec.get("order_id")
        if old_oid:
            try:
                api.request("DELETE", api.BASE_FAPI + "/fapi/v1/order",
                            {"symbol": sym, "orderId": old_oid})
                log(f"[pin] {sym} 旧插针挂单 #{old_oid} 已过期,静默撤销")
            except Exception:
                pass
        _pin_orders.pop(sym, None)
        wl.pop(sym, None)
        try:
            _j2.dump(wl, open(wl_file, "w"), indent=2, ensure_ascii=False)
        except Exception:
            pass
        return

    # ── 仓位、方向、价格 ──
    is_long    = (sig_type == "pin_long")
    direction  = "LONG" if is_long else "SHORT"
    side       = "BUY"  if is_long else "SELL"
    close_side = "SELL" if is_long else "BUY"
    pos_side   = "LONG" if is_long else "SHORT"
    notional   = 250.0

    # 获取精度信息
    try:
        tick, step = api.get_sym_info_cached(sym)
        if not tick or not step:
            tick, step = 0.001, 0.001
    except Exception:
        tick, step = 0.001, 0.001

    def _round_tick(p, t):
        dec = max(0, -int(_math.floor(_math.log10(t)))) if t < 1 else 0
        return float(f"{round(round(p/t)*t, dec):.{dec}f}")
    def _round_step(q, s):
        dec = max(0, -int(_math.floor(_math.log10(s)))) if s < 1 else 0
        return float(f"{round(round(q/s)*s, dec):.{dec}f}")

    # 挂单价:当前价上方/下方1%
    if is_long:
        limit_price = _round_tick(price * 1.01, tick)
        sl_price    = _round_tick(price * 0.995, tick)  # 低点下方0.5%(以当前价近似)
    else:
        limit_price = _round_tick(price * 0.99, tick)
        sl_price    = _round_tick(price * 1.005, tick)  # 高点上方0.5%

    qty = _round_step(notional / limit_price, step)
    if qty <= 0:
        log(f"[pin] {sym} 数量计算为0,跳过")
        return

    # ── 开仓前守卫检查 ──
    try:
        import sys as _sys_pin; _sys_pin.path.insert(0, f"{WORKSPACE}/scripts")
        from pre_open_guard import check_before_open as _pg
        ok_pg, reason_pg = _pg(sym, direction, notional)
        if not ok_pg:
            log(f"[pin] {sym} 守卫拦截: {reason_pg}")
            return
    except Exception as _e_pg:
        log(f"[pin] {sym} 守卫检查异常: {_e_pg}")

    # ── 下限价单 ──
    try:
        r = api.place_order(sym, side, "LIMIT",
                            price=str(limit_price), quantity=str(qty),
                            positionSide=pos_side)
        if not r.get("orderId"):
            log(f"[pin] {sym} 挂单失败: {r}")
            return
        order_id = int(r["orderId"])
        log(f"[pin] {sym} {direction} 插针挂单 #{order_id} @{limit_price} qty={qty} sl={sl_price}")
    except Exception as e:
        log(f"[pin] {sym} 挂单异常: {e}")
        return

    # ── 记录(含过期时间,供下次轮询撤单用)──
    _pin_orders[sym] = {
        "order_id":  order_id,
        "expire_at": expire_at,
        "direction": direction,
        "sl_price":  sl_price,
        "notional":  notional,
    }

    # ── 推送通知 ──
    arrow  = "📈" if is_long else "📉"
    p_type = "下影插针多单" if is_long else "上影插针空单"
    notify(
        f"{arrow} [{sym}] 插针信号挂单\n"
        f"方向: {p_type}  仓位: {notional:.0f}U\n"
        f"挂单价: {limit_price}  止损: {sl_price}\n"
        f"有效期: 30分钟(到期自动撤单)"
    )


def _check_watchlist(sym: str, price: float):
    import json as _j
    wl_file = f"{WORKSPACE}/watchlist.json"
    if not os.path.exists(wl_file):
        return
    try:
        wl = _j.load(open(wl_file))
    except Exception:
        return
    # 顺手清理过期条目
    cleaned, removed = _purge_stale_watchlist_ws(wl)
    if removed:
        try:
            _j.dump(cleaned, open(wl_file, "w"), indent=2, ensure_ascii=False)
            log(f"[wl_purge] 清理 {len(removed)} 个过期条目: {removed}")
        except Exception:
            pass
        wl = cleaned
    entry = wl.get(sym)
    if not entry:
        return

    # ── PIN 信号专属路径(优先判断,不走普通 score 门槛)──
    sig_type = entry.get("sig_type", "")
    if sig_type in ("pin_long", "pin_short"):
        _handle_pin_signal(sym, price, entry, wl, wl_file)
        return

    target    = float(entry.get("target", 0))
    direction = entry.get("direction", "above")
    score     = float(entry.get("score", 0))
    note      = entry.get("note", "")
    if target <= 0:
        return
    hit = (direction == "above" and price >= target) or           (direction == "below" and price <= target)
    if not hit:
        return
    now = time.time()
    if now - _wl_fired.get(sym, 0) < _WL_COOLDOWN:
        return

    # ── 触达时实时重新评分(核心修复:不用加入时的旧分)──────────────────────
    # 加入时评分可能已过时(几小时前),必须用实时评分决策
    try:
        import threading as _th_wl
        def _rescore_and_fire():
            try:
                k1h  = api.get_klines(sym, "1h", 100)
                k4h  = api.get_klines(sym, "4h", 50)
                k15m = api.get_klines(sym, "15m", 20)
                tk   = api.get_mark_price(sym) or {}
                fr   = float(tk.get("lastFundingRate", 0))
                from entry_logic import (
                    calc_score_trend, calc_score_reversal,
                    calc_score_trend_short, calc_score_short,
                )
                trade_dir = "LONG" if direction == "below" else "SHORT"
                if trade_dir == "LONG":
                    st = calc_score_trend(sym, k1h, k4h, k15m, tk, fr)[0]
                    sr = calc_score_reversal(sym, k1h, k4h, k15m, tk, fr)[0]
                    live_score = max(st, sr)
                else:
                    ss  = calc_score_short(sym, k1h, k4h, k15m, tk, fr)[0]
                    sts = calc_score_trend_short(sym, k1h, k4h, k15m, tk, fr)[0]
                    live_score = max(ss, sts)

                stale_score = score  # 加入时的旧评分
                gate = 75  # 开仓门槛

                log(f"[watchlist] {sym} 触达目标价 旧分={stale_score:.0f} 实时分={live_score:.0f} 方向={trade_dir}")

                if live_score < gate:
                    # 实时评分不达标,不开仓,仅通知
                    notify(
                        f"⚠️ [{sym}] watchlist触达但实时评分不足\n"
                        f"  加入时={stale_score:.0f} → 实时={live_score:.0f}(门槛{gate})\n"
                        f"  市场条件已变化,跳过开仓"
                    )
                    log(f"[watchlist] {sym} 实时评分{live_score:.0f}<{gate},不开仓")
                    return

                # 实时评分达标,写入待汇总队列
                _wl_fired[sym] = now
                _save_wl_fired()
                arrow = "📈" if direction == "below" else "📉"
                _wl_hit_pending[sym] = {
                    "arrow": arrow, "price": price, "target": target,
                    "score": live_score,   # 用实时评分
                    "stale_score": stale_score,
                    "note": note, "ts": now
                }
                log(f"[watchlist] {sym} 实时评分{live_score:.0f}达标,加入待播报队列")
            except Exception as _e_rs:
                log(f"[watchlist_rescore_err] {sym}: {_e_rs}")
                # fallback:用旧分(总比不处理好)
                if score >= _WL_SCORE_MIN:
                    _wl_fired[sym] = now
                    _save_wl_fired()
                    arrow = "📈" if direction == "above" else "📉"
                    _wl_hit_pending[sym] = {
                        "arrow": arrow, "price": price, "target": target,
                        "score": score, "note": note, "ts": now,
                        "rescore_failed": True,
                    }

        _th_wl.Thread(target=_rescore_and_fire, daemon=True).start()
    except Exception as _te:
        log(f"[watchlist_thread_err] {sym}: {_te}")


# ─── 急跌/急涨异动检测 ────────────────────────────────────────────────────────

def _record_price_snapshot(sym: str, price: float):
    """每次 ticker 更新时记录价格快照,保留最近 30 分钟"""
    now = time.time()
    with _surge_lock:
        if sym not in _price_snapshots:
            _price_snapshots[sym] = []
        _price_snapshots[sym].append((now, price))
        # 只保留最近 30 分钟的快照(节省内存)
        cutoff = now - 30 * 60
        _price_snapshots[sym] = [(t, p) for t, p in _price_snapshots[sym] if t >= cutoff]


def _check_surge(sym: str, price: float):
    """
    检测 15 分钟内急跌/急涨 ≥ 5%。
    - 评分 ≥ 65 → 触发 trigger_agent 走完整评分+挂单
    - 评分 60~65 → 只通知用户
    - 其他 → 静默
    冷却:同一标的 2 小时内不重复触发;每小时最多触发 3 个标的。
    """
    now = time.time()

    # 检查冷却
    with _surge_lock:
        cd_expire = _surge_cooldown.get(sym, 0)
        if now < cd_expire:
            return  # 仍在冷却期

        # 检查每小时上限(清理过期记录)
        _surge_hourly[:] = [t for t in _surge_hourly if now - t < 3600]
        if len(_surge_hourly) >= SURGE_HOURLY_MAX:
            return  # 本小时已满

        # 取 15 分钟前的快照
        snaps = _price_snapshots.get(sym, [])
        cutoff_15m = now - SURGE_WINDOW_SEC
        old_snaps = [(t, p) for t, p in snaps if t <= cutoff_15m]
        if not old_snaps:
            return  # 快照不足 15 分钟,无法判断

        # 用最早的 15 分钟前快照作为基准
        base_price = old_snaps[0][1]
        change_pct = (price - base_price) / base_price  # 正=涨,负=跌

        if abs(change_pct) < SURGE_THRESHOLD:
            return  # 变化幅度不足 5%

        # 触发!标记冷却
        _surge_cooldown[sym] = now + SURGE_COOLDOWN
        _surge_hourly.append(now)

    # ── 走出 lock 后执行(避免阻塞 ticker 流)
    direction = "急涨" if change_pct > 0 else "急跌"
    emoji = "🚀" if change_pct > 0 else "📉"
    pct_str = f"{change_pct*100:+.1f}%"
    log(f"[surge] {sym} 15m{direction} {pct_str} base={base_price:.6f} now={price:.6f}")

    # 根据评分缓存决定动作(使用已有的 _rsi_cache 快速判断,避免 API 调用)
    cached_rsi = _rsi_cache.get(sym)

    if change_pct < 0:  # 急跌 → 做多候选
        rsi_ok = cached_rsi is None or cached_rsi < 45  # RSI 未过热时才考虑做多
        if not rsi_ok:
            log(f"[surge] {sym} 急跌但 RSI={cached_rsi:.1f} 偏高,跳过")
            return
        prompt = (
            f"{sym} 在过去 15 分钟内急跌 {pct_str}(当前价 {price:.6f})。"
            f"请立即对 {sym} 做一次完整评分(趋势分 + 反转分),"
            f"若评分 ≥ 65 且方向确认做多,则按标准双网策略挂单(A 单 = 当前价 -1%,B 单触发价 = 当前价 +3%);"
            f"若评分 60~65 则只通知用户;评分 < 60 静默忽略。"
            f"推送时包含:评分、RSI、ATR、理由、挂单详情(如有)。"
        )
        label = f"surge-long-{sym}-{int(now)}"
    else:  # 急涨 → 追多候选
        rsi_ok = cached_rsi is None or cached_rsi < 72  # RSI 未过热时才考虑追多
        if not rsi_ok:
            log(f"[surge] {sym} 急涨但 RSI={cached_rsi:.1f} 过热,跳过")
            return
        prompt = (
            f"{sym} 在过去 15 分钟内急涨 {pct_str}(当前价 {price:.6f})。"
            f"请立即检查:1) 1H RSI 是否 < 72;2) 成交量是否是近1H均量 2x 以上;3) OI 是否同步增加(排除空头平仓拉盘)。"
            f"若三项均满足且评分 ≥ 65,则挂 B 单(突破追多,触发价 = 当前价 +1%);"
            f"否则只通知用户说明原因。"
            f"推送时包含:评分、量比、OI 变化、挂单详情(如有)。"
        )
        label = f"surge-short-{sym}-{int(now)}"

    # ── 直接在本进程执行评分+决策,不依赖外部 agent ──────────────────────
    notify(f"{emoji} [{sym}] 异动警报:15m{direction} {pct_str}  当前价 {price:.6f}")
    _surge_evaluate_and_place(sym, price, change_pct, direction)


def _surge_evaluate_and_place(sym: str, price: float, change_pct: float, direction: str):
    """
    异动检测后:本进程内直接完成评分->gate->挂单->推送结果,不依赖外部agent。
    在独立线程执行,不阻塞 ticker 流。
    """
    import threading

    def _run():
        try:
            k1h  = api.get_klines(sym, "1h", 100)
            k4h  = api.get_klines(sym, "4h", 50)
            k15m = api.get_klines(sym, "15m", 20)
            tk   = api.get_mark_price(sym) or {}
            fr   = float(tk.get("lastFundingRate", 0))
            mark = float(tk.get("markPrice", price))
            funding_pct = fr * 100

            from entry_logic import (
                calc_score_trend, calc_score_reversal,
                calc_score_trend_short, calc_score_short,
            )
            from market_filter import apply as _mf
            from pre_order_gate import check as _gate
            from calc_position_size import calc as _calc_pos

            scores = {
                "trend_long":     calc_score_trend(sym, k1h, k4h, k15m, tk, fr)[0],
                "reversal_long":  calc_score_reversal(sym, k1h, k4h, k15m, tk, fr)[0],
                "trend_short":    calc_score_trend_short(sym, k1h, k4h, k15m, tk, fr)[0],
                "reversal_short": calc_score_short(sym, k1h, k4h, k15m, tk, fr)[0],
            }
            best_sig   = max(scores, key=scores.get)
            best_score = scores[best_sig]
            trade_dir  = "LONG" if "long" in best_sig else "SHORT"

            score_summary = (
                "TL=%.0f RL=%.0f TS=%.0f RS=%.0f" % (
                    scores["trend_long"], scores["reversal_long"],
                    scores["trend_short"], scores["reversal_short"],
                )
            )

            # 环境过滤
            adj_score = _mf(sym=sym, direction=trade_dir, score=best_score,
                            funding_pct=funding_pct)
            if adj_score <= -999:
                notify("[%s] 异动评分=%d 环境过滤拦截(结算窗口/FGI极端)" % (sym, best_score))
                return

            header = "[%s] 异动评分: 最优=%s %.0f分  %s" % (sym, best_sig, adj_score, score_summary)

            if adj_score < 60:
                notify("%s\n-> 评分不足60,不开仓" % header)
                return

            if adj_score < 80:
                notify("%s\n-> 评分60~79,进观察区,等清算区出现后入场" % header)
                return

            # 评分>=80,走完整开仓流程
            pos_u = _calc_pos(best_sig, adj_score, trade_dir, {})
            gate  = _gate(sym=sym, direction=trade_dir, score=adj_score,
                          funding_pct=funding_pct, pos_size_u=pos_u)
            if not gate.ok:
                notify("%s\n-> 风控拦截: %s" % (header, gate.reason))
                return

            from scanner import place_entry_order as _place
            details = {
                "final_score": adj_score, "sig_type": best_sig,
                "funding": fr, "btc_trend": "",
                "has_liq_zone": False,
            }
            result = _place(sym, trade_dir, mark, k1h, k4h, details)

            if result.get("ok"):
                notify(
                    "%s\n-> 已挂单 方向=%s 仓位=%.0fU 入场价=%s 模式=%s" % (
                        header, trade_dir, pos_u,
                        result.get("limit_price", "?"),
                        result.get("mode", "?"),
                    )
                )
            elif result.get("mode") == "WATCH":
                notify("%s\n-> 进观察区(%s)" % (header, result.get("reason", "")))
            else:
                notify("%s\n-> 挂单失败: %s" % (header, result.get("reason", str(result))))

        except Exception as e:
            log("[surge_eval_err] %s: %s" % (sym, e))
            notify("[%s] 异动评分异常: %s" % (sym, e))

    t = threading.Thread(target=_run, daemon=True, name="surge-%s" % sym)
    t.start()


def handle_kline_1h(msg: dict):
    """
    优化二: 处理 1H kline WS 实时推送。
    格式:{"e":"kline","s":"ONDOUSDT","k":{"t":...,"v":"1234.5","c":"0.41",...}}
    更新 _dn_kline_vol_cur(当前k线成交量)和 _dn_vol_avg(动态平均量)。
    """
    try:
        k = msg.get("k") or msg.get("data", {}).get("k")
        if not k:
            return
        sym     = msg.get("s") or msg.get("data", {}).get("s", "")
        is_done = k.get("x", False)   # True = 该根K线已收盘
        vol     = float(k.get("v", 0))
        close   = float(k.get("c", 0))

        if not sym:
            return

        # 实时更新当前k线成交量(未收盘也更新,当前累计量)
        _dn_kline_vol_cur[sym] = vol

        # 收盘后更新平均量(连续20根滚动均值)
        if is_done:
            _dn_kline_close[sym] = close   # 优化1: 缓存已收盘K线的收盘价
            _vol_history = _kline_vol_history.setdefault(sym, [])
            _vol_history.append(vol)
            if len(_vol_history) > 60:          # 最多保留 60 根
                _vol_history.pop(0)
            if len(_vol_history) >= 5:          # 至少 5 根才计算均量
                window = _vol_history[-20:] if len(_vol_history) >= 20 else _vol_history
                _dn_vol_avg[sym] = sum(window) / len(window)
            log(f"[kline_1h] {sym} 收盘 vol={vol:.0f} avg20={_dn_vol_avg.get(sym,0):.0f} "
                f"ratio={vol/_dn_vol_avg.get(sym,vol):.2f}x")
    except Exception as e:
        log(f"[handle_kline_1h_err] {e}")


_kline_vol_history: dict[str, list] = {}   # sym → 近20根K线成交量历史
_dn_kline_close: dict = {}   # sym → 最近一根已收盘的1H K线收盘价(优化1: 收盘确认)


def handle_ticker(msg):
    """解析 ticker list,更新 _ticker 缓存,调用 check_gates + watchlist 监控"""
    data = msg if isinstance(msg, list) else [msg]

    # 快速 early-exit:无门控且无 watchlist 时跳过全部标识查询
    gates_empty = not _gates_mem
    try:
        wl_exists = os.path.exists(f"{WORKSPACE}/watchlist.json")
    except Exception:
        wl_exists = False

    with _ticker_lock:
        for item in data:
            sym = item.get("s")
            price_str = item.get("c") or item.get("p")
            if sym and price_str:
                try:
                    _ticker_cache[sym] = float(price_str)
                except Exception:
                    pass

    if gates_empty and not wl_exists:
        return   # 无任何监控任务,直接返回

    for item in data:
        sym = item.get("s")
        if sym:
            price = _ticker_cache.get(sym)
            if price:
                if not gates_empty:
                    try:
                        check_gates(sym, price)
                    except Exception as e:
                        log(f"[gates_check_err] {sym}: {e}")
                if wl_exists:
                    try:
                        _check_watchlist(sym, price)
                    except Exception as e:
                        log(f"[watchlist_err] {sym}: {e}")

                # ── 优化三:Algo单主动平仓保护层 ────────────────────────────────
                # 层次一:价格已穿越止损价 → 直接市价平仓(Algo单可能延迟触发)
                # 层次二:价格很接近止损价(距离<=0.5%)→ REST 检查 Algo单是否还在
                try:
                    _ps = _load_pos_state()
                    _pe = _ps.get(sym)
                    if _pe and abs(float(_pe.get("qty", 0))) > 0:
                        _sl  = float(_pe.get("sl_price", 0))
                        _dir = _pe.get("direction", "LONG")
                        _qty = abs(float(_pe.get("qty", 0)))
                        if _sl > 0 and _qty > 0:
                            # 层次一:价格已穿越--尝试直接平仓
                            _breached = ((_dir == "LONG" and price <= _sl * 0.999) or
                                         (_dir == "SHORT" and price >= _sl * 1.001))
                            if _breached:
                                _preempt_key = f"_preempt_{sym}"
                                if not globals().get(_preempt_key):
                                    # Fix-B: 先确认真的有仓位,防止 pos_state 幽灵记录导致反复触发
                                    try:
                                        _live_pos = [p for p in api.get_positions()
                                                     if p["symbol"] == sym
                                                     and abs(float(p.get("positionAmt", 0))) > 0]
                                    except Exception:
                                        _live_pos = [True]  # 查询失败保守处理,继续执行
                                    if not _live_pos:
                                        # 无仓位但 pos_state 有记录 → 清理残留,跳过平仓
                                        log(f"[preempt_sl] {sym} pos_state 有记录但无实际仓位,清理残留")
                                        _ps_store.delete(sym)
                                    else:
                                        globals()[_preempt_key] = True
                                        log(f"[preempt_sl] {sym} 价格 {price} 已穿越止损 {_sl},主动平仓")
                                        _close_side = "SELL" if _dir == "LONG" else "BUY"
                                        _r = api.place_order(sym, _close_side, "MARKET",
                                                             quantity=_qty, positionSide=_dir)
                                        if _r.get("orderId"):
                                            notify(f"🛡 [{sym}] 主动平仓触发\n"
                                                   f"价格 {price:.6f} 已穿越止损价 {_sl:.6f}\n"
                                                   f"Algo单延迟保护层生效")
                                        else:
                                            globals()[_preempt_key] = False  # 平仓失败,下次再试
                            # 层次二:价格接近止损(距离 <= 0.5%)--REST 验证 Algo单存在性
                            else:
                                _near_threshold = 0.005
                                _near = ((_dir == "LONG" and 0 < (_sl - price) / price <= _near_threshold) or
                                         (_dir == "SHORT" and 0 < (price - _sl) / price <= _near_threshold))
                                if _near:
                                    # 限制频率:同一标的 REST 检查每 30 秒不超过一次
                                    _check_key = f"_sl_check_ts_{sym}"
                                    _last_check = globals().get(_check_key, 0)
                                    if time.time() - _last_check >= 30:
                                        globals()[_check_key] = time.time()
                                        try:
                                            _real = api.get_algo_open_orders(sym) or []
                                            _has_sl = any("STOP" in o.get("orderType","") for o in _real)
                                            if not _has_sl:
                                                log(f"[preempt_near] {sym} 接近止损 {_sl}, REST确认无 Algo止损单,补挂")
                                                # 直接补挂止损单
                                                _close_side = "SELL" if _dir == "LONG" else "BUY"
                                                _rr = api.place_algo_order(sym, _close_side, "STOP_MARKET",
                                                                           _sl, _dir, quantity=_qty)
                                                if _rr.get("algoId"):
                                                    api.algo_cache_save(sym, "STOP_MARKET",
                                                                        int(_rr["algoId"]), str(_sl))
                                                    notify(f"⚠️ [{sym}] 接近止损位且 Algo单丢失,已补挂 @{_sl}")
                                        except Exception as _re:
                                            log(f"[preempt_rest_err] {sym}: {_re}")
                except Exception as _pe_err:
                    log(f"[preempt_err] {sym}: {_pe_err}")
                # ── 双网 B单突破检测(评分>=80挂单后实时监控)───────────────────
                _dn_pending = _dn.load_pending()
                if sym in _dn_pending and isinstance(_dn_pending[sym], dict) and _dn_pending[sym].get("meta", {}).get("status") == "active":
                    try:
                        _cur_vol = _dn_kline_vol_cur.get(sym, 0)
                        _avg_vol = _dn_vol_avg.get(sym, 0)
                        # Bug修复:缓存未就绪时(scanner_loop还未跑完)立即拉一次K线初始化
                        if _avg_vol == 0 or _cur_vol == 0:
                            try:
                                _init_k = api.get_klines(sym, "1h", 22)
                                _init_vols = [float(k[5]) for k in _init_k]
                                _init_closes = [float(k[4]) for k in _init_k]
                                _avg_vol = sum(_init_vols[-21:-1]) / 20 if len(_init_vols) >= 21 else sum(_init_vols[:-1]) / max(len(_init_vols)-1, 1)
                                _cur_vol = _init_vols[-1]
                                _dn_vol_avg[sym]       = _avg_vol
                                _dn_kline_vol_cur[sym] = _cur_vol
                                # 同步初始化 RSI
                                if sym not in _rsi_cache:
                                    _gains  = [max(_init_closes[i]-_init_closes[i-1], 0) for i in range(1, len(_init_closes))]
                                    _losses = [max(_init_closes[i-1]-_init_closes[i], 0) for i in range(1, len(_init_closes))]
                                    _ag = sum(_gains[-14:]) / 14; _al = sum(_losses[-14:]) / 14
                                    _rsi_cache[sym] = 100 - (100 / (1 + _ag / _al)) if _al > 0 else 50
                                log(f"[dn_init] {sym} 首次初始化 vol_avg={_avg_vol:.0f} cur={_cur_vol:.0f} rsi={_rsi_cache.get(sym,50):.1f}")
                            except Exception as _ie:
                                log(f"[dn_init_err] {sym}: {_ie}")
                        vol_ratio = (_cur_vol / _avg_vol) if _avg_vol > 0 and _cur_vol > 0 else 1.0
                        rsi_1h = _rsi_cache.get(sym, 50.0)
                        # B单5m收盘确认:check_breakout内部自己拉5m K线判断,
                        # is_kline_closed=True让它走5m确认分支(不再用1H收盘等待)
                        _dn.check_breakout(
                            sym=sym, price=price,
                            vol_ratio=vol_ratio, rsi_1h=rsi_1h,
                            kline_close=price,
                            is_kline_closed=True,
                            api=api, notify_fn=notify,
                        )
                    except Exception as e:
                        log(f"[dn_breakout_err] {sym}: {e}")

                # ── 急跌/急涨异动检测层(已关闭,减少噪音推送)───────────
                # _check_surge 已禁用:评分 API 不兼容 + 用户不需要异动提醒
                # _record_price_snapshot(sym, price)
                # _check_surge(sym, price)


def handle_user_data(msg):
    """解析 ORDER_TRADE_UPDATE 和 ACCOUNT_UPDATE"""
    try:
        event = msg.get("e")
        if event == "ORDER_TRADE_UPDATE":
            _process_order_update(msg.get("o", {}))
        elif event == "ACCOUNT_UPDATE":
            _process_account_update(msg.get("a", {}))
    except Exception as e:
        log(f"[user_data_err] {e}")


def _process_order_update(order: dict):
    """处理订单更新事件"""
    status     = order.get("X")   # order status
    if status != "FILLED":
        return
    symbol     = order.get("s", "")
    side       = order.get("S", "")         # BUY / SELL
    order_type = order.get("o", "")
    reduce_only = order.get("R", False)
    avg_price  = float(order.get("ap", 0) or order.get("p", 0))
    qty        = float(order.get("q", 0))
    realized_pnl = float(order.get("rp", 0))

    on_order_filled(symbol, side, reduce_only, order_type, avg_price, qty, realized_pnl)

    # ── 双网互斥:检查该订单是否属于双网 A单,如是则马上撤 B单 ────────────
    if not reduce_only:
        _pending_data = _dn.load_pending()
        _dn_entry = _pending_data.get(symbol)
        if _dn_entry and _dn_entry["meta"]["status"] == "active":
            a_id = _dn_entry["a_order"].get("order_id")
            b_id = _dn_entry["b_order"].get("order_id")
            order_id = order.get("i")  # orderId from event
            if str(a_id) == str(order_id):
                # A单成交 → 撤 B单
                threading.Thread(
                    target=_dn.on_order_filled,
                    args=(symbol, "A", order_id, api, notify),
                    daemon=True
                ).start()
            elif str(b_id) == str(order_id):
                # B单成交 → 撤 A单
                threading.Thread(
                    target=_dn.on_order_filled,
                    args=(symbol, "B", order_id, api, notify),
                    daemon=True
                ).start()
        def _check_cancel_pending():
            time.sleep(3)  # 等仓位更新
            try:
                CAPITAL    = 619
                LEVERAGE   = 3
                MAX_NOTIONAL = CAPITAL * LEVERAGE  # 1857U
                BUFFER_U   = 50  # 「接近上限 50U 内就撤单」

                positions = [p for p in api.get_positions()
                             if abs(float(p.get("positionAmt", 0))) > 0]
                used = sum(
                    abs(float(p["positionAmt"])) *
                    float(api.get_mark_price(p["symbol"]).get("markPrice",
                          p.get("entryPrice", 0)))
                    for p in positions
                )
                remaining = MAX_NOTIONAL - used
                log(f"[fill_check] {symbol} 成交后 used={used:.0f}U remaining={remaining:.0f}U")

                if remaining <= BUFFER_U:
                    # 剩余小于缓冲区,撤除所有开仓挂单
                    all_orders = api.get_open_orders()
                    entry_orders = [o for o in (all_orders if isinstance(all_orders, list) else [])
                                    if not o.get("reduceOnly")]
                    cancelled = 0
                    for o in entry_orders:
                        try:
                            api.cancel_order(o["symbol"], int(o["orderId"]))
                            cancelled += 1
                        except Exception as ce:
                            log(f"[cancel_err] {o['symbol']}: {ce}")
                    if cancelled:
                        notify(
                            f"⏸ [{symbol}] 开仓成交,剩余名义{remaining:.0f}U ≤ 缓冲{BUFFER_U}U,"
                            f"已撤除 {cancelled} 个开仓挂单,等待仓位释放后再扫描。"
                        )
                        log(f"[fill_check] 已撤 {cancelled} 个开仓挂单")
            except Exception as e:
                log(f"[fill_check_err] {e}")

        threading.Thread(target=_check_cancel_pending, daemon=True).start()


def _process_account_update(account: dict):
    """处理账户余额更新"""
    for asset in account.get("B", []):
        if asset.get("a") == "USDT":
            log(f"[acct_update] USDT balance={asset.get('wb')}")


def get_listen_key() -> str:
    """获取 listenKey,走代理"""
    try:
        result = api.get_listen_key()
        log(f"[listen_key] obtained: {result[:8]}...")
        return result
    except Exception as e:
        log(f"[listen_key_err] {e}")
        return ""


# BUG-C02 已删除孤立函数 _keepalive_listen_key_loop(死代码,未被任何地方调用)
# keepalive 逻辑在 run_user_data_stream 内部的 _keepalive_loop 线程里,连续可靠

def start_streams():
    """启动 ticker 和 markprice 流(binance-cli 子进程,可绕过 MITM 代理)"""
    global _streams_started
    if _streams_started:
        return
    _streams_started = True

    # ticker 全量 miniTicker(用于门控检查 + watchlist 监控)
    threading.Thread(
        target=run_stream, args=("ticker", handle_ticker), daemon=True
    ).start()
    log("[ws] ticker stream started (binance-cli)")

    # ── 优化二:1H kline WS实时推送,替代 scanner_loop 30min 轮询的量比缓存 ──────
    threading.Thread(
        target=run_stream, args=("kline_1h", handle_kline_1h), daemon=True
    ).start()
    log("[ws] kline_1h stream started")


def start_user_data_stream():
    """启动 user-data stream(用于成交回调)"""
    listen_key = get_listen_key()
    if not listen_key:
        log("[ws] 获取 listenKey 失败,user-data stream 未启动")
        return

    threading.Thread(
        target=run_user_data_stream, args=(listen_key, handle_user_data), daemon=True
    ).start()
    log("[ws] user-data stream started (binance-cli)")

# ─── 8. on_order_filled ──────────────────────────────────────────────────────

def on_order_filled(symbol: str, side: str, reduce_only: bool,
                    order_type: str, avg_price: float, qty: float, realized_pnl: float):
    """统一处理成交事件"""
    if not reduce_only:
        _on_open_filled(symbol, side, avg_price, qty)
    else:
        _on_close_filled(symbol, side, avg_price, qty, realized_pnl)


def _on_open_filled(symbol: str, side: str, avg_price: float, qty: float):
    """
    开仓成交处理:
    P0 强制执行顺序(不可打乱):
    1. 即时挂止损(fast path,不等Coinglass,失败→重试3次→市价平仓)
    2. 注册 price_gates
    3. 写入 pos_state
    4. 异步挂止盈(不阻塞主流程)
    5. 推送成交通知
    6. 其他后续逻辑
    """
    _increment_addon_counter(symbol)

    def _delayed_setup():
        # P0: 止损前置,不再延迟等待
        import coinglass_api as _cg

        ps       = _load_pos_state()
        pos      = ps.get(symbol, {})
        _entry_raw = float(pos.get("entry_price", 0) or 0)
        entry    = _entry_raw if _entry_raw > 0 else avg_price
        s_qty    = float(pos.get("qty", qty) or qty)
        # 修复:entry_price=0时补写真实成交价
        if _entry_raw <= 0:
            ps = _load_pos_state()
            ps.setdefault(symbol, {})
            ps[symbol]["entry_price"] = avg_price
            ps[symbol]["qty"]        = s_qty
            _save_pos_state(ps)
            log(f"[open_filled] {symbol} pos_state entry_price=0,已补写实际成交价={avg_price}")
        pos_side = pos.get("direction") or ("LONG" if side == "BUY" else "SHORT")
        close_side = "SELL" if pos_side == "LONG" else "BUY"

        # ─── ATR(1h) ──────────────────────────────────────────────────────────
        try:
            k1h   = api.get_klines(symbol, "1h", 20)
            highs = [float(k[2]) for k in k1h]
            lows  = [float(k[3]) for k in k1h]
            cls   = [float(k[4]) for k in k1h]
            atr   = sum(max(highs[i] - lows[i],
                            abs(highs[i] - cls[i-1]) if i > 0 else 0,
                            abs(lows[i]  - cls[i-1]) if i > 0 else 0)
                        for i in range(len(k1h))) / max(len(k1h), 1)
        except Exception as e:
            log(f"[open_filled] {symbol} ATR计算异常: {e}")
            atr = entry * 0.03   # fallback 3%

        mark = float(api.get_mark_price(symbol)["markPrice"])

        # ─── 1 自动挂止损(技术债二:统一走 sl_handler,P0 fast path 保留)────
        sl_price = float(pos.get("sl_price", 0))
        existing_sl = []
        try:
            existing_sl = [a for a in api.get_algo_open_orders(symbol)
                           if "STOP" in a.get("orderType", "")]
        except Exception:
            pass

        if existing_sl:
            sl_price = float(existing_sl[0].get("triggerPrice", sl_price))
            log(f"[open_filled] {symbol} 已有止损单 @{sl_price},跳过补挂")
        else:
            # 计算止损价(fast path,不等 Coinglass)
            hard_sl  = float(pos.get("hard_sl", 0))
            atr_sl   = round(entry - atr * 2.0, 6) if pos_side == "LONG" else round(entry + atr * 2.0, 6)
            fixed_sl = round(entry - atr * 2.0, 6) if pos_side == "LONG" else round(entry + atr * 2.0, 6)  # ATR×2 替代固定6%

            if hard_sl > 0 and ((pos_side == "LONG" and hard_sl < entry) or
                                 (pos_side == "SHORT" and hard_sl > entry)):
                sl_raw = hard_sl
                log(f"[open_filled] {symbol} 止损 fast path hard_sl={hard_sl}")
            elif atr_sl > 0 and ((pos_side == "LONG" and atr_sl < entry) or
                                  (pos_side == "SHORT" and atr_sl > entry)):
                sl_raw = atr_sl
                log(f"[open_filled] {symbol} 止损 fast path atr_sl={atr_sl:.6f}")
            else:
                sl_raw = fixed_sl
                log(f"[open_filled] {symbol} 止损固定比例兜底={fixed_sl:.6f}")

            # Coinglass 非阻塞优化(挂单后异步,更好就更新)
            try:
                liq  = _cg.get_liq_zones(symbol, mark)
                below = liq.get("below", [])
                sig   = [z for z in below if float(z[1]) >= 2_000_000]
                if sig:
                    cg_sl = float(max(sig, key=lambda z: float(z[1]))[0]) - atr * 0.5
                    if ((pos_side == "LONG"  and 0 < cg_sl < sl_raw) or
                        (pos_side == "SHORT" and cg_sl > sl_raw > 0)):
                        sl_raw = cg_sl
                        log(f"[open_filled] {symbol} CG 止损更优,更新 {cg_sl:.6f}")
            except Exception as _cge:
                log(f"[open_filled] {symbol} CG 查询(非阻塞): {_cge}")

            # 安全夹:止损必须在入场价 ATR×0.1 外侧(不能紧贴入场价)
            _sl_min_gap = atr * 0.1
            if pos_side == "LONG":
                sl_price = round(min(sl_raw, entry - _sl_min_gap), 6)
            else:
                sl_price = round(max(sl_raw, entry + _sl_min_gap), 6)

            # ── 统一走 sl_handler(技术债二核心)────────────────────────────
            import sl_handler as _slh
            _sl_result = _slh.place(
                sym=symbol, pos_side=pos_side,
                entry=entry, qty=s_qty,
                sl_price=sl_price,
                tp1=0.0, tp2=0.0,   # TP 由下方原有逻辑挂,sl_handler 只负责止损
                notify_fn=None,     # 通知由下方统一推送,避免重复
                force=False,
            )
            if _sl_result["sl_ok"]:
                sl_price = _sl_result.get("sl_algo_id") and sl_price or sl_price
                log(f"[open_filled] {symbol} sl_handler 止损成功 @{sl_price} algoId={_sl_result.get('sl_algo_id')}")
                notify(f"✅ [{symbol}] 开仓成交 {side} {qty} @ {avg_price:.6f}")
                notify(f"🔒 [{symbol}] 止损 @{sl_price}(sl_handler 确认)")
                ps.setdefault(symbol, {})
                ps[symbol]["sl_price"]     = sl_price
                ps[symbol]["sl_algo_id"]   = _sl_result.get("sl_algo_id")
                ps[symbol]["sl_activated"] = True
                _ps_store.upsert(symbol, {
                    "sl_price":     sl_price,
                    "sl_algo_id":   _sl_result.get("sl_algo_id"),
                    "sl_activated": True,
                })
                # P3-3: 持仓监控确认
                _gate_ok = symbol in _load_gates()
                _ps_ok   = bool(_ps_store.read(symbol))
                notify(
                    f"[开仓确认] {symbol} {pos_side}\n"
                    f"  ✅ 止损单: algo #{_sl_result.get('sl_algo_id')} @{sl_price}\n"
                    f"  {'✅' if _gate_ok else '⚠️'} gates: {'已注册' if _gate_ok else '未注册'}\n"
                    f"  ✅ patrol: 已加入监控队列\n"
                    f"  {'✅' if _ps_ok else '⚠️'} pos_state: {'已写入' if _ps_ok else '未写入'}"
                )
            else:
                errs = _sl_result.get("errors", [])
                notify(f"🚨 [{symbol}] sl_handler 止损失败(3次重试+市价平仓已触发)\n  错误: {errs}")

        # ─── 2 自动挂止盈 TP1+TP2(各 50%)─────────────────────────────────
        # 无论 sl_handler 成功或失败,都要挂止盈单
        tp1 = float(pos.get("tp1_price", 0))
        tp2 = float(pos.get("tp2_price", 0))
        try:
            existing_tp = [o for o in api.get_open_orders(symbol)
                           if o.get("reduceOnly") and o.get("type") in
                           ("LIMIT_TP", "TAKE_PROFIT", "LIMIT")]
        except Exception:
            existing_tp = []

        if existing_tp:
            log(f"[open_filled] {symbol} 已有止盈单,跳过自动挂")
            if tp1 <= 0 and existing_tp:
                tp1 = float(existing_tp[0].get("stopPrice") or existing_tp[0].get("price") or 0)
            if tp2 <= 0 and len(existing_tp) > 1:
                tp2 = float(existing_tp[1].get("stopPrice") or existing_tp[1].get("price") or 0)
        else:
            try:
                liq   = _cg.get_liq_zones(symbol, mark)
                above = liq.get("above", []) if pos_side == "LONG" else liq.get("below", [])
                sig_above = sorted(
                    [z for z in above if float(z[1]) >= 1_500_000],
                    key=lambda z: float(z[0])
                )
                if pos_side == "LONG":
                    # 筛选在当前价格以上的清算区
                    sig_above = [z for z in sig_above if float(z[0]) > mark]
                else:
                    sig_above = [z for z in sig_above if float(z[0]) < mark]

                # ── TP 最优选取:TP2 与 TP1 间距 < 0.5% 则跳过,找下一个有效清算区 ──
                MIN_TP_GAP = 0.005  # TP2 至少比 TP1 高 0.5%

                if len(sig_above) >= 1:
                    tp1 = round(float(sig_above[0][0]) * 0.999, 6)
                    # 寻找与 TP1 间距 >= MIN_TP_GAP 的下一个清算区作为 TP2
                    tp2_candidates = [
                        z for z in sig_above[1:]
                        if abs(float(z[0]) - float(sig_above[0][0])) / float(sig_above[0][0]) >= MIN_TP_GAP
                    ] if len(sig_above) >= 2 else []

                    if tp2_candidates:
                        tp2 = round(float(tp2_candidates[0][0]) * 0.999, 6)
                        log(f"[open_filled] {symbol} TP2 跳过间距过小清算区,锚定 {tp2} (+{(float(tp2_candidates[0][0])-entry)/entry*100:.2f}%)")
                    else:
                        # 所有清算区都挨得太近,TP2 用 ATR×3.5 兜底
                        tp2 = round(entry + atr * 3.5, 6) if pos_side == "LONG" else round(entry - atr * 3.5, 6)
                        log(f"[open_filled] {symbol} 上方清算区间距均<{MIN_TP_GAP*100:.1f}%,TP2 改用 ATR×3.5={tp2}")
                else:
                    # 无清算区数据,用 ATR 倍数
                    tp1 = round(entry + atr * 2.0, 6) if pos_side == "LONG" else round(entry - atr * 2.0, 6)
                    tp2 = round(entry + atr * 3.5, 6) if pos_side == "LONG" else round(entry - atr * 3.5, 6)

                # ── 最小止盈距离检查(仅在 TP 距离 ≤ 2% 时才口预)────────
                MIN_TP_TRIGGER = 0.02  # 只有 TP 距离 ≤ 2% 才介入
                MIN_TP_DIST    = 0.04  # 介入后上调到至少 4%

                def _tp_dist(tp_price):
                    if pos_side == "LONG":
                        return (tp_price - mark) / mark
                    else:
                        return (mark - tp_price) / mark

                # 判断是否具备反弹潜力:RSI 未超买(多)/未超卖(空) + 量比 ≥ 0.8
                try:
                    from entry_logic import rsi_calc
                    k1h_check = api.get_klines(symbol, "1h", limit=30)
                    c1h_check = [float(k[4]) for k in k1h_check]
                    v1h_check = [float(k[5]) for k in k1h_check]
                    rsi_now   = rsi_calc(c1h_check)
                    vr_now    = sum(v1h_check[-3:]) / 3 / (sum(v1h_check[-23:-3]) / 20 + 1e-9)
                    has_rebound = (
                        (pos_side == "LONG"  and rsi_now < 75 and vr_now >= 0.8) or
                        (pos_side == "SHORT" and rsi_now > 25 and vr_now >= 0.8)
                    )
                except Exception:
                    has_rebound = True  # 获取失败则保守挂单

                # TP1 距离 > 2%:按原来逻辑挂,不介入
                if _tp_dist(tp1) > MIN_TP_TRIGGER:
                    pass  # 正常路径,直接挂单
                elif has_rebound:
                    # TP1 距离 ≤ 2% 且有反弹潜力:上调到 4%
                    tp1_fallback = round(entry * (1 + MIN_TP_DIST) if pos_side == "LONG"
                                         else entry * (1 - MIN_TP_DIST), 6)
                    if len(sig_above) >= 2:
                        tp1 = max(tp1_fallback,
                                  round(float(sig_above[1][0]) * 0.999, 6)) if pos_side == "LONG" \
                              else min(tp1_fallback,
                                       round(float(sig_above[1][0]) * 1.001, 6))
                    else:
                        tp1 = tp1_fallback
                    log(f"[open_filled] {symbol} TP1 距离仅 {_tp_dist(round(float(sig_above[0][0])*0.999,6))*100:.1f}%,上调至 {tp1}")
                else:
                    # TP1 距离 ≤ 2% 且无反弹潜力:不挂 TP,靠止损保护
                    log(f"[open_filled] {symbol} RSI={rsi_now:.1f} VR={vr_now:.2f} 无反弹潜力且TP紧,跳过挂TP")
                    notify(f"⚠️ [{symbol}] TP 距离仅≤2%且方向风险,未挂止盈,止损已就位")
                    tp1 = 0.0
                    tp2 = 0.0

                # Fix P1-8: 空单三档止盈 40%/35%/25%,多单保持 50/50
                if pos_side == "SHORT":
                    qty1 = round(s_qty * 0.40, 0)
                    qty2 = round(s_qty * 0.35, 0)
                    qty3 = round(s_qty - qty1 - qty2, 0)
                    # 需要 tp3(比 tp2 更深一档)
                    if tp2 > 0 and entry > 0:
                        tp3 = round(entry - (entry - tp2) * 1.5, 6)   # tp2 基础上再延伸50%
                    else:
                        tp3 = tp2
                else:
                    qty1 = round(s_qty / 2, 0)
                    qty2 = round(s_qty - qty1, 0)
                    qty3 = 0
                    tp3  = 0

                # 多单:挂 TP1(50%)+ TP2(50%)
                # 空单:挂 TP1(40%)+ TP2(35%)+ TP3(25%)
                from entry_logic import fmt_price as _fmt_price
                # 使用交易所真实 tickSize,避免精度错误
                try:
                    _tick_real, _step_real = api.get_sym_info_cached(symbol)
                    _tick = float(_tick_real)
                except Exception:
                    _tick = 0.00001 if entry < 1 else (0.0001 if entry < 10 else (0.001 if entry < 100 else 0.01))
                tp1_fmt = _fmt_price(tp1, _tick)
                tp2_fmt = _fmt_price(tp2, _tick)
                r1 = api.place_order(symbol, close_side, "LIMIT",
                                     quantity=str(int(qty1)), price=tp1_fmt,
                                     positionSide=pos_side, timeInForce="GTC")
                r2 = api.place_order(symbol, close_side, "LIMIT",
                                     quantity=str(int(qty2)), price=tp2_fmt,
                                     positionSide=pos_side, timeInForce="GTC")
                r3 = None
                if pos_side == "SHORT" and tp3 > 0 and qty3 > 0:
                    tp3_fmt = _fmt_price(tp3, _tick)
                    r3 = api.place_order(symbol, close_side, "LIMIT",
                                         quantity=str(int(qty3)), price=tp3_fmt,
                                         positionSide=pos_side, timeInForce="GTC")

                tp1_ok = bool(r1.get("orderId"))
                tp2_ok = bool(r2.get("orderId"))
                tp3_ok = bool(r3.get("orderId")) if r3 else None
                log(f"[open_filled] {symbol} TP1={tp1_fmt}({qty1}张) ok={tp1_ok} "
                    f"TP2={tp2_fmt}({qty2}张) ok={tp2_ok}"
                    + (f" TP3={tp3_fmt}({qty3}张) ok={tp3_ok}" if tp3_ok is not None else ""))
                try:
                    if tp1_ok: api.algo_cache_save(symbol, "LIMIT_TP1", int(r1["orderId"]), str(tp1))
                    if tp2_ok: api.algo_cache_save(symbol, "LIMIT_TP2", int(r2["orderId"]), str(tp2))
                    if tp3_ok: api.algo_cache_save(symbol, "LIMIT_TP3", int(r3["orderId"]), str(tp3))
                except Exception: pass
                # P0: 统一写入路径
                try:
                    _ps_store.upsert(symbol, {
                        'tp1_price': tp1,
                        'tp2_price': tp2,
                        'tp3_price': tp3 if pos_side == 'SHORT' else 0,
                        'opened_at': time.time(),
                        'tp1_hit':   False,
                    })
                except Exception: pass
                # 盈亏比计算
                dir_emoji = "🟢 多" if pos_side == "LONG" else "🔴 空"
                try:
                    if pos_side == "LONG":
                        tp1_pnl = round((tp1 - entry) * qty1, 2)
                        tp2_pnl = round((tp2 - entry) * qty2, 2)
                        sl_pnl  = round((sl_price - entry) * s_qty, 2)
                        tp3_line = ""
                    else:
                        tp1_pnl = round((entry - tp1) * qty1, 2)
                        tp2_pnl = round((entry - tp2) * qty2, 2)
                        tp3_pnl = round((entry - tp3) * qty3, 2) if qty3 > 0 else 0
                        sl_pnl  = round((entry - sl_price) * s_qty, 2)
                        tp3_line = f"  🎯 TP3 @{tp3}({qty3}张)→ +{tp3_pnl}U\n" if qty3 > 0 else ""
                    rr1 = round(abs(tp1_pnl / sl_pnl), 2) if sl_pnl != 0 else 0
                    rr2 = round(abs(tp2_pnl / sl_pnl), 2) if sl_pnl != 0 else 0
                    pnl_str = (
                        f"  🎯 TP1 @{tp1}({qty1}张)→ +{tp1_pnl}U  RR≈1:{rr1}\n"
                        f"  🎯 TP2 @{tp2}({qty2}张)→ +{tp2_pnl}U  RR≈1:{rr2}\n"
                        + (tp3_line if pos_side == "SHORT" else "")
                        + f"  🛡 SL  @{sl_price} → {sl_pnl}U"
                    )
                except Exception:
                    pnl_str = (
                        f"  TP1 @{tp1}({qty1}张){'✅' if tp1_ok else '❌'}\n"
                        f"  TP2 @{tp2}({qty2}张){'✅' if tp2_ok else '❌'}"
                    )
                notify(
                    f"🔔 止盈止损已挂 · {symbol}\n"
                    f"{dir_emoji} 入场 @{entry}\n"
                    f"{pnl_str}"
                )
                # 写入 pos_state
                ps.setdefault(symbol, {})
                ps[symbol]["tp1_price"] = tp1
                ps[symbol]["tp2_price"] = tp2
            except Exception as e:
                log(f"[open_filled] {symbol} 止盈挂单异常: {e},3s后重试")
                try:
                    import time as _t; _t.sleep(3)
                    # 重试整个止盈挂单流程(简化版:仅挂 TP1)
                    tp1_retry = float(pos.get("tp1_price", 0))
                    if tp1_retry:
                        qty_half = round(abs(float(pos.get("qty", 0))) / 2, 3)
                        from entry_logic import fmt_price as _fp2
                        _tk2 = 0.00001 if tp1_retry < 1 else (0.0001 if tp1_retry < 10 else (0.001 if tp1_retry < 100 else 0.01))
                        r_tp = api.place_order(symbol, close_side, "LIMIT",
                                               quantity=str(qty_half),
                                               price=_fp2(tp1_retry, _tk2),
                                               positionSide=pos_side, timeInForce="GTC")
                        if r_tp.get("orderId"):
                            log(f"[open_filled] {symbol} 止盈TP1重试成功 orderId={r_tp['orderId']}")
                        else:
                            notify(f"⚠️ [{symbol}] 止盈重试失败,需手动挂止盈\n  TP1: {tp1_retry:.6f}")
                    else:
                        notify(f"⚠️ [{symbol}] 止盈挂单异常且无法重试(tp1未知),需手动处理\n  原因: {e}")
                except Exception as e2:
                    notify(f"⚠️ [{symbol}] 止盈挂单重试失败,需手动处理\n  原因: {e2}")
                # fallback TP: ATR×2/3.5 动态,不用固定5%/10%
                tp1 = tp1 or round(entry + atr * 2.0, 6) if pos_side == 'LONG' else (tp1 or round(entry - atr * 2.0, 6))
                tp2 = tp2 or round(entry + atr * 3.5, 6) if pos_side == 'LONG' else (tp2 or round(entry - atr * 3.5, 6))

        # ─── 3 保存 pos_state ────────────────────────────────────────────────
        ps.setdefault(symbol, {})
        ps[symbol].update({
            "entry_price": entry,
            "qty":         s_qty,
            "direction":   pos_side,
            "atr1h":       atr,
        })
        # P0: 统一写入路径
        _ps_store.upsert(symbol, {
            "entry_price": entry,
            "qty":         s_qty,
            "direction":   pos_side,
            "atr1h":       atr,
        })

        # ─── 4 注册门控(传入 ATR)──────────────────────────────────────────
        register_gates(symbol, entry, s_qty, pos_side,
                       sl_price, tp1, tp2, atr1h=atr)

        # ─── 5 记录开仓日志 ──────────────────────────────────────────────────
        notional = round(entry * s_qty, 4)
        _score   = pos.get("score", 0)
        _details = pos.get("details", {})
        try:
            log_open(
                sym=symbol,
                direction=pos_side,
                sig_type=pos.get("sig_type", pos.get("source", "trend")),
                entry=entry,
                qty=s_qty,
                score=_score,
                details=_details,
            )
        except Exception as _le:
            log(f"[trade_logger_open_err] {_le}")

        # ─── 条目3: 逆势空单 L1→L2 补仓监测线程 ──────────────────────────────
        # 触发条件:sig_type == 'reversal_short_l1'(L1空单,125U已成交)
        #   价格继续上涨(浮亏<3%)且出现见顶K线→触发L2补仓125U,合计250U
        #   直接大幅下跌(浮盈>5%)→不补仓,按L1管理
        _sig_type_check = pos.get("sig_type", pos.get("source", ""))
        if (pos_side == "SHORT" and
                _sig_type_check == "reversal_short_l1" and
                pos.get("addon_l2_done", False) is False):
            def _l2_addon_monitor(sym_=symbol, entry_=entry, qty_=s_qty):
                import time as _ti
                MAX_WATCH_MINUTES = 120   # 最多监测2小时
                CHECK_INTERVAL    = 120   # 每2分钟检查一次
                for _ in range(MAX_WATCH_MINUTES * 60 // CHECK_INTERVAL):
                    _ti.sleep(CHECK_INTERVAL)
                    try:
                        # 检查仓位是否还在
                        _cur_pos = [p for p in api.get_positions()
                                    if p.get("symbol") == sym_
                                    and p.get("positionSide", "") == "SHORT"
                                    and abs(float(p.get("positionAmt", 0))) > 0]
                        if not _cur_pos:
                            log(f"[l2_addon] {sym_} 仓位已关闭,唅止L2监测")
                            break
                        _mark = float(api.get_mark_price(sym_).get("markPrice", entry_))
                        _pnl_pct = (entry_ - _mark) / entry_ * 100  # 空单盈利
                        # 直接大幅盈利 > 5% → 不补仓
                        if _pnl_pct > 5.0:
                            log(f"[l2_addon] {sym_} 浮盈{_pnl_pct:.1f}% > 5%,不需补仓,唅止监测")
                            break
                        # 浮亏 < 3%(价格小幅上涨)且出现见顶K线 → 补L2
                        if _pnl_pct < 0 and abs(_pnl_pct) < 3.0:
                            _k1h_l2 = api.get_klines(sym_, "1h", 5)
                            # 见顶K线:上影线>实伓2倍 或 阴包阳
                            _has_top = False
                            if len(_k1h_l2) >= 2:
                                _k = _k1h_l2[-1]
                                _kp= _k1h_l2[-2]
                                _ko=float(_k[1]);_kh=float(_k[2]);_kc=float(_k[4])
                                _kop=float(_kp[1]);_kcp=float(_kp[4])
                                _body=abs(_kc-_ko); _upper=_kh-max(_ko,_kc)
                                _ss = _upper > _body*2 and _body > 0
                                _be = _kc < _kop and _ko > _kcp and _kc < _ko
                                _has_top = _ss or _be
                            if _has_top:
                                # 补L2:125U同向空单
                                _lmap2 = None
                                try:
                                    from entry_logic import liquidation_map as _lm2, calc_entry_mode as _cem2
                                    _lmap2 = _lm2(sym_, _k1h_l2, _k1h_l2, "SHORT")
                                    _em2, _lp2, _, _as2 = _cem2(_k1h_l2, "SHORT", _lmap2, final_score=70)
                                except Exception:
                                    _lp2 = _mark * 1.001  # 现价微上方挂单
                                    _as2 = "fallback"
                                try:
                                    from entry_logic import get_sym_info as _gsi2, fmt_price as _fp2, fmt_qty as _fq2
                                except Exception:
                                    _fp2 = lambda p, t: str(round(p, 6))
                                    _fq2 = lambda q, s: str(round(q, 3))
                                _tick2 = 0.00001 if _mark < 1 else (0.0001 if _mark < 10 else 0.001)
                                _step2 = 1.0
                                _qty2  = 125.0 / max(_lp2, 1e-9)
                                _lp2_str = _fp2(_lp2, _tick2)
                                _qty2_str= _fq2(_qty2, _step2)
                                r_l2 = api.place_order(sym_, "SELL", "LIMIT",
                                                       quantity=_qty2_str, price=_lp2_str,
                                                       positionSide="SHORT", timeInForce="GTC")
                                if r_l2.get("orderId"):
                                    # 更新 pos_state 标记 L2 已触发
                                    _ps_l2 = _load_pos_state()
                                    _ps_l2.setdefault(sym_, {})
                                    _ps_l2[sym_]["addon_l2_done"] = True
                                    _save_pos_state(_ps_l2)
                                    notify(f"🔴 [{sym_}] 逆势空单L1→L2 补仓125U | 出现见顶K线 | 浮亏{abs(_pnl_pct):.1f}% | 限价={_lp2_str} 锚点={_as2} 数量={_qty2_str} | 合计空单250U")
                                    log(f"[l2_addon] {sym_} L2补仓成功 orderId={r_l2['orderId']}")
                                else:
                                    notify(f"⚠️ [{sym_}] L2补仓挟单失败: {r_l2}")
                                break  # 无论成与否均唅止监测
                    except Exception as _le2:
                        log(f"[l2_addon_err] {sym_}: {_le2}")
                log(f"[l2_addon] {sym_} L2监测已结束")
            threading.Thread(target=_l2_addon_monitor, daemon=True).start()
            log(f"[l2_addon] {symbol} reversal_short_l1 L2监测已展开")

        # ─── 条目3 end ────────────────────────────────────────────────

    threading.Thread(target=_delayed_setup, daemon=True).start()

def _on_close_filled(symbol: str, side: str, avg_price: float,
                     qty: float, realized_pnl: float,
                     order_type: str = "", trigger_price: float = 0.0):
    """
    平仓成交处理。
    - 止盈单成交(LIMIT 限价):识别 TP1/TP2,触发对应门控,spawn TP followup
    - 止损单成交或完全平仓:clear_gates
    """
    pnl_str = f"+{realized_pnl:.2f}U" if realized_pnl >= 0 else f"{realized_pnl:.2f}U"
    notify(f"{'✅' if realized_pnl >= 0 else '❌'} [{symbol}] 平仓成交 {side} {qty} @ {avg_price:.6f} | PnL: {pnl_str}")

    if realized_pnl < 0:
        add_loss(realized_pnl)

    # ── 记录平仓日志 ──────────────────────────────────────────────────────────
    _close_reason = order_type.lower() if order_type else (
        "tp" if realized_pnl >= 0 else "sl"
    )
    try:
        _add_cnt = _ps_store.read(symbol).get("add_count", 0)
        log_close(symbol, avg_price, _close_reason, add_count=_add_cnt)
    except Exception as _le:
        log(f"[trade_logger_close_err] {_le}")

    # ── 检查是否还有剩余仓位 ─────────────────────────────────────────────────
    time.sleep(2)
    try:
        remaining = _get_remaining_position(symbol)
        has_remaining = remaining and remaining.get("qty", 0) > 0

        if not has_remaining:
            # 完全平仓:OCO 互斥 - 撤掉所有剩余 TP 限价单 + SL algo 单,避免占用保证金
            # SL 触发 → 撤剩余 TP;TP 全部成交 → 撤剩余 SL
            try:
                import sl_handler as _slh_oco
                _slh_oco.cancel_tp(symbol)   # 撤剩余 TP 限价单
                _slh_oco.cancel_sl(symbol)   # 撤剩余 SL algo 单
                log(f"[close_filled] {symbol} OCO互斥:剩余SL/TP单已全部撤销")
            except Exception as _oco_e:
                log(f"[close_filled] {symbol} OCO撤单异常: {_oco_e}")
            # 兼容:保留原 algo 撤销逻辑作为兜底
            try:
                remaining_algos = api.get_algo_open_orders(symbol)
                for a in remaining_algos:
                    try:
                        api.cancel_algo_order(symbol, int(a["algoId"]))
                        log(f"[close_filled] {symbol} 平仓后撤销 algo algoId={a['algoId']} type={a.get('orderType')}")
                    except Exception as _ce:
                        log(f"[close_filled] {symbol} 撤销 algo 失败: {_ce}")
            except Exception as _ae:
                log(f"[close_filled] {symbol} 获取 algo 失败: {_ae}")
            # 清缓存
            try:
                from binance_api import _load_algo_cache, _save_algo_cache
                _ac = _load_algo_cache()
                if symbol in _ac:
                    del _ac[symbol]
                    _save_algo_cache(_ac)
            except Exception:
                pass
            clear_gates(symbol)
            _reset_addon_counter(symbol)

            # 平仓后写 scan_trigger,通知 scanner 尽快扫描
            write_scan_trigger("position_closed")

            # Fix P0-3: 止损出局写冷静期(止盈不限)
            if realized_pnl < 0:
                try:
                    import importlib.util as _ilu
                    _spec = _ilu.spec_from_file_location("scanner", f"{WORKSPACE}/scanner.py")
                    _sm = _ilu.module_from_spec(_spec)
                    _spec.loader.exec_module(_sm)
                    # LONG止损→1h冷静, SHORT止损→2h冷静
                    _cool_h = 2.0 if side == "SELL" else 1.0   # SELL=平多=LONG止损
                    _cool_dir = "LONG" if side == "SELL" else "SHORT"
                    _sm._add_to_cooldown(symbol, _cool_dir, _cool_h)
                except Exception as _ce:
                    log(f"[cooldown_write_err] {_ce}")
            return

        rem_qty   = remaining["qty"]
        rem_entry = remaining.get("entry", avg_price)
        rem_side  = remaining.get("positionSide", "LONG")

        # ── TP 成交识别:触发对应门控 ─────────────────────────────────────────
        ps = _load_pos_state()
        pos = ps.get(symbol, {})
        tp1 = float(pos.get("tp1_price", 0))
        tp2 = float(pos.get("tp2_price", 0))
        sl  = float(pos.get("sl_price", 0))

        is_tp1 = tp1 > 0 and abs(avg_price - tp1) / max(tp1, 1e-9) < 0.005
        is_tp2 = tp2 > 0 and abs(avg_price - tp2) / max(tp2, 1e-9) < 0.005

        if is_tp1:
            log(f"[close_filled] {symbol} 识别为 TP1 成交 @{avg_price}")
            # 触发第一档门控:止损目标优先用 Coinglass 最近清算区(比固定保本价更有意义)
            # 逻辑:TP1 成交后,下方(多单)/ 上方(空单)最近清算区就是新的止损锚点
            try:
                _cg_gate = cg.get_liq_zones(symbol, avg_price)
                if rem_side == "LONG":
                    _gate_zones = [z for z in _cg_gate.get("below", []) if z[0] < avg_price]
                    # 取离成交价最近的下方清算区(按量排序取最大量,再比距离取更近的)
                    _near = [z for z in _gate_zones if z[0] > avg_price * 0.985]  # 成交价 -1.5% 内
                    sl_target = round(_near[0][0] * 0.999, 6) if _near else round(rem_entry * 1.001, 6)
                else:
                    _gate_zones = [z for z in _cg_gate.get("above", []) if z[0] > avg_price]
                    _near = [z for z in _gate_zones if z[0] < avg_price * 1.015]
                    sl_target = round(_near[0][0] * 1.001, 6) if _near else round(rem_entry * 0.999, 6)
                log(f"[close_filled] {symbol} TP1门控 sl_target={sl_target} (来源=CG)")
            except Exception as _cg_gate_e:
                # fallback: entry ± ATR×0.1(贴近保本,比固定0.001更合理)
                try:
                    _k_gate = api.get_klines(symbol, "1h", 14)
                    _atr_gate = sum(abs(float(k[2])-float(k[3])) for k in _k_gate) / 14
                except Exception:
                    _atr_gate = rem_entry * 0.01
                sl_target = round(rem_entry + _atr_gate * 0.1, 6) if rem_side == "LONG" else round(rem_entry - _atr_gate * 0.1, 6)
                log(f"[close_filled] {symbol} TP1门控 sl_target={sl_target} (来源=ATR_fallback: {_cg_gate_e})")
            if sl > 0 and (
                (rem_side == "LONG"  and sl_target > sl) or
                (rem_side == "SHORT" and sl_target < sl)
            ):
                threading.Thread(
                    target=_execute_move_sl,
                    args=(symbol, rem_side, rem_qty, sl, sl_target, "保本(TP1触发)", avg_price),
                    daemon=True
                ).start()
            else:
                log(f"[close_filled] {symbol} TP1门控:sl_target={sl_target} 未超过现有sl={sl},跳过")

        elif is_tp2:
            log(f"[close_filled] {symbol} 识别为 TP2 成交 @{avg_price}")
            # 触发第二档门控:止损目标优先用 TP1 价格作为锁利点
            # (TP1 已成交意味着那个价格之前已经套利,止损锁在 TP1 价格之上是合理的)
            # 若能找到更好的清算区锚点(在 TP1 和成交价之间),使用清算区
            try:
                _cg_gate2 = cg.get_liq_zones(symbol, avg_price)
                if rem_side == "LONG":
                    # 找在 tp1 和 avg_price 之间的清算区
                    _mid_zones = [z for z in _cg_gate2.get("below", [])
                                  if tp1 < z[0] < avg_price]
                    sl_target = round(max(_mid_zones, key=lambda z: z[1])[0] * 0.999, 6)                                 if _mid_zones else (tp1 if tp1 > 0 else round(rem_entry * 1.03, 6))
                else:
                    _mid_zones = [z for z in _cg_gate2.get("above", [])
                                  if avg_price < z[0] < tp1]
                    sl_target = round(min(_mid_zones, key=lambda z: z[0])[0] * 1.001, 6)                                 if _mid_zones else (tp1 if tp1 > 0 else round(rem_entry * 0.97, 6))
                log(f"[close_filled] {symbol} TP2门控 sl_target={sl_target} (来源=CG/TP1)")
            except Exception:
                sl_target = tp1 if tp1 > 0 else round(rem_entry * 1.03, 6)
            if sl > 0 and (
                (rem_side == "LONG"  and sl_target > sl) or
                (rem_side == "SHORT" and sl_target < sl)
            ):
                threading.Thread(
                    target=_execute_move_sl,
                    args=(symbol, rem_side, rem_qty, sl, sl_target, "锁利(TP2触发)", avg_price),
                    daemon=True
                ).start()

        # ── TP followup 分析(仅 TP1 成交时触发) ────────────────────────────
        if is_tp1:
            # TP1 平恡3/5,仓位数不变但有更多槽位,主动触发扫描
            write_scan_trigger("tp1_triggered")
            # P-TP2: TP1 触发后立即重查 Coinglass,把 TP2 移到最新最大清算区前沿
            try:
                _fresh_zones = cg.get_liq_zones(symbol, avg_price)
                _fresh_below  = _fresh_zones.get('below', []) if rem_side == 'SHORT' else []
                _fresh_above  = _fresh_zones.get('above', []) if rem_side == 'LONG'  else []
                _fresh_zones2 = _fresh_below if rem_side == 'SHORT' else _fresh_above
                _tp1_used  = avg_price  # TP1 刚成交的价格就是新的参考基准
                _MIN_TP2_R = 0.03       # TP2 至少还要离 TP1 成交价 3%
                if rem_side == 'SHORT':
                    _tp2_candidates_r = sorted(
                        [z for z in _fresh_zones2 if float(z[0]) <= _tp1_used * (1 - _MIN_TP2_R)],
                        key=lambda z: z[1] if len(z) > 1 else 0, reverse=True
                    )
                    _new_tp2 = (_tp2_candidates_r[0][0] * 1.001) if _tp2_candidates_r else None
                else:
                    _tp2_candidates_r = sorted(
                        [z for z in _fresh_zones2 if float(z[0]) >= _tp1_used * (1 + _MIN_TP2_R)],
                        key=lambda z: z[1] if len(z) > 1 else 0, reverse=True
                    )
                    _new_tp2 = (_tp2_candidates_r[0][0] * 0.999) if _tp2_candidates_r else None

                if _new_tp2 and abs(_new_tp2 - tp2) / max(tp2, 1e-9) > 0.005:
                    # 重查结果与原 TP2 有 > 0.5% 差异,更新 TP2 挂单
                    try:
                        _tick_r, _ = api.get_sym_info_cached(symbol)
                        from entry_logic import fmt_price as _fmtp
                        _tp2_fmt_r = _fmtp(_new_tp2, float(_tick_r))
                        # 撤旧 TP2 限价单(保留 TP3 不动)
                        _open_now = api.get_open_orders(symbol)
                        _old_tp2_orders = [
                            o for o in _open_now
                            if o.get('reduceOnly') and o.get('type') == 'LIMIT'
                            and abs(float(o.get('price', 0)) - tp2) / max(tp2, 1e-9) < 0.01
                        ]
                        for _o in _old_tp2_orders:
                            api.cancel_order(symbol, _o['orderId'])
                        _close_side_r = 'BUY' if rem_side == 'SHORT' else 'SELL'
                        _r_tp2_new = api.place_order(
                            symbol, _close_side_r, 'LIMIT',
                            quantity=str(int(rem_qty * 0.5)),
                            price=_tp2_fmt_r,
                            positionSide=rem_side, timeInForce='GTC'
                        )
                        if _r_tp2_new.get('orderId'):
                            _ps_store.upsert(symbol, {'tp2_price': float(_tp2_fmt_r)})
                            log(f'[tp1_hit] {symbol} TP2 重查更新: {tp2} → {_tp2_fmt_r} (来源=coinglass_refresh)')
                            notify(f'🔄 [{symbol}] TP1成交后TP2重锚定 → {_tp2_fmt_r} (原{tp2})')
                        else:
                            log(f'[tp1_hit] {symbol} TP2 重挂失败: {_r_tp2_new}')
                    except Exception as _e_tp2_upd:
                        log(f'[tp1_hit] {symbol} TP2 更新异常: {_e_tp2_upd}')
                else:
                    log(f'[tp1_hit] {symbol} TP2 位置合理(偏差<0.5%),不调整 tp2={tp2} new_tp2={_new_tp2}')
            except Exception as _e_tp2_refresh:
                log(f'[tp1_hit] {symbol} Coinglass重查异常,跳过TP2更新: {_e_tp2_refresh}')

            open_orders = api.get_open_orders(symbol)
            tp_orders = [o for o in open_orders
                         if o.get("reduceOnly") and
                         o.get("type") == "LIMIT" and o.get("reduceOnly")]
            if tp_orders:
                _spawn_tp_followup_agent(symbol, rem_qty, rem_entry, rem_side, tp_orders)

        # ── 检查止损单是否缺失或数量不匹配(参考老版本 _adjust_sl_qty 逻辑)────
        algo_orders = api.get_algo_open_orders_cached(symbol)
        sl_orders = [o for o in algo_orders if "STOP" in o.get("orderType", "")]
        if not sl_orders:
            _spawn_sl_adj_agent(symbol, rem_qty, rem_side, rem_entry)
        else:
            # 止损单存在,但检查数量是否匹配剩余仓位(TP 部分成交后止损数量要同步下调)
            total_sl_qty = sum(float(a.get("quantity", 0)) for a in sl_orders)
            if abs(total_sl_qty - rem_qty) > rem_qty * 0.02:  # 偏差 > 2%
                log(f"[close_filled] {symbol} 止损数量不匹配: sl={total_sl_qty} rem={rem_qty},触发重挂")
                _spawn_sl_adj_agent(symbol, rem_qty, rem_side, rem_entry)

    except Exception as e:
        log(f"[close_follow_err] {symbol}: {e}")

def _get_remaining_position(symbol: str) -> dict:
    """查询剩余仓位"""
    try:
        positions = api.get_positions()
        for p in positions:
            if p.get("symbol") == symbol:
                amt = float(p.get("positionAmt", 0))
                if abs(amt) > 0:
                    return {
                        "qty": abs(amt),
                        "entry": float(p.get("entryPrice", 0)),
                        "positionSide": "LONG" if amt > 0 else "SHORT",
                    }
    except Exception as e:
        log(f"[remaining_pos_err] {e}")
    return {}


def _spawn_tp_followup_agent(symbol: str, rem_qty, entry: float,
                              side: str, tp_orders: list):
    """
    TP 成交后跟进分析。
    原则:保住当前寻利是首要目标,能多拿是加分,
    直接执行决策,决策后向用户播报。
    """
    tp_list_str = json.dumps([
        {"orderId": o.get("orderId"), "price": o.get("stopPrice") or o.get("price"),
         "qty": o.get("origQty")}
        for o in tp_orders
    ], ensure_ascii=False)

    prompt = f"""[{symbol} TP内线分析 - 直接执行]
剩余仓位: {side} {rem_qty}张 @ 均价{entry}
剩余止盈单: {tp_list_str}

核心原则:保住当前寻利是首要目标。能多拿是加分项。不产生新亏损。

执行步骤(必须严格按顺序):
1. import sys,json; sys.path.insert(0,'/root/.openclaw/workspace'); import binance_api as api, coinglass_api

2. # 拉取全量市场数据
   mark = float(api.get_mark_price('{symbol}')['markPrice'])
   k1h  = api.get_klines('{symbol}','1h',20)
   k4h  = api.get_klines('{symbol}','4h',10)
   closes_1h = [float(k[4]) for k in k1h]
   vols_1h   = [float(k[5]) for k in k1h]
   closes_4h = [float(k[4]) for k in k4h]

3. # 计算指标
   gains =[max(closes_1h[i]-closes_1h[i-1],0) for i in range(1,len(closes_1h))]
   losses=[max(closes_1h[i-1]-closes_1h[i],0) for i in range(1,len(closes_1h))]
   avg_g =sum(gains[-13:])/13; avg_l=sum(losses[-13:])/13
   rsi14 = 100-(100/(1+(avg_g/(avg_l+1e-9))))
   vol_ratio = vols_1h[-1]/(sum(vols_1h[-6:-1])/5+1e-9)  # 最新5根均量之比
   trend_4h  = 'up' if closes_4h[-1] > closes_4h[-4] else 'down'
   liq       = coinglass_api.get_liq_zones('{symbol}', mark)
   above     = liq.get('above', [])
   # 当前所有剥盈单(用于情况B判断)
   existing_tp_orders = [o for o in api.get_open_orders('{symbol}')
                         if o.get('reduceOnly') and o.get('type') == 'LIMIT']
   # 如果是空单, above 用下方清算区
   if '{side}' == 'SHORT':
       above = liq.get('below', [])

4. # 决策逻辑
   # 情况A - 直接平仓(首要:锁住现有盈利)
   # 触发条件: RSI>72 或 4H趋势已反转(down) 或 量莳缩且RSI>65
   do_close  = rsi14 > 72 or trend_4h == 'down' or (vol_ratio < 0.7 and rsi14 > 65)

   # 情况B - 止盈单移到最密集清算区前沿(小幅度优化)
   # 触发条件: above 有清算密集区 且 现有止盈单不在最佳清算带前沿(偏差>1%)
   best_above = max(above, key=lambda z: float(z[1]))[0] if above else None
   sig_above  = [z for z in above if float(z[1]) >= 2_000_000]
   _existing_prices = [float(o['price']) for o in existing_tp_orders]
   do_adjust  = (not do_close and sig_above
                 and _existing_prices
                 and all(abs(p - float(sig_above[0][0])) / float(sig_above[0][0]) > 0.01
                         for p in _existing_prices))
   # 情况C - 维持不动
   do_hold = not do_close and not do_adjust

5. # 执行
   if do_close:
     r = api.place_order('{symbol}','SELL','MARKET',quantity={rem_qty},
                         reduceOnly='true',positionSide='{side}')
     report = f'平仓锁住寻利 @{{mark}}(RSI={{rsi14:.1f}}, 4H={{trend_4h}}, 量比={{vol_ratio:.2f}})'
   elif do_adjust and sig_above:
     new_tp = round(float(sig_above[0][0]) * 0.999, 6)  # 清算密集区前沿 *0.999
     # 合并所有剩余止盈单为一个
     cur_orders = api.get_open_orders('{symbol}')
     for o in cur_orders:
       if o.get('reduceOnly') and o.get('side') == 'SELL':
         api.cancel_order('{symbol}', int(o['orderId']))
     r = api.place_order('{symbol}','SELL','LIMIT',price=str(new_tp),
                         quantity={rem_qty},timeInForce='GTC',positionSide='{side}')
     report = f'止盈单已调整至最密集清算区前沿 @{{new_tp}}(合并{rem_qty}张)'
   else:
     report = f'止盈单维持不动(RSI={{rsi14:.1f}}, 4H={{trend_4h}}, 量比={{vol_ratio:.2f}})'

6. print(report)

播报格式(必须包含):
📊 {symbol} TP跟进 | 决策: [[平仓/调整/维持]] | RSI={{rsi14:.1f}} | 4H={{trend_4h}} | 量比={{vol_ratio:.2f}} | 操作: {{report}}
"""
    trigger_agent(prompt, label=f"tp-followup-{symbol}")


def _spawn_sl_adj_agent(symbol: str, rem_qty, side: str, entry: float):
    """止损缺失时直接在进程内补挂(废弃 trigger_agent 子进程,不可靠)。"""
    try:
        # 二次检查:确认真的没有止损
        algos = api.get_algo_open_orders(symbol) or []
        existing_sl = [a for a in algos if "STOP" in a.get("orderType", "")]
        if existing_sl:
            log(f"[sl_adj] {symbol} 已有止损 @{existing_sl[0].get('triggerPrice')},跳过")
            return

        # 计算止损价
        mark = float(api.get_mark_price(symbol).get("markPrice", entry))
        k1h  = api.get_klines(symbol, "1h", 20)
        from entry_logic import atr_calc as _atr_calc
        atr  = _atr_calc(k1h)

        try:
            import coinglass_api as _cg_sl
            zones = _cg_sl.get_liq_zones(symbol, mark)
            if side == "LONG":
                below = [z for z in zones.get("below", []) if z[0] < mark]
                best  = max(below, key=lambda z: z[1] if len(z)>1 else 0) if below else None
                sl    = (best[0] - atr * 0.5) if best else (mark - atr * 2.0)
                sl    = min(sl, entry * 0.97)   # 不超过入场价3%亏损
            else:
                above = [z for z in zones.get("above", []) if z[0] > mark]
                best  = max(above, key=lambda z: z[1] if len(z)>1 else 0) if above else None
                sl    = (best[0] + atr * 0.5) if best else (mark + atr * 2.0)
                sl    = max(sl, entry * 1.03)
        except Exception as _cg_e:
            log(f"[sl_adj] {symbol} CG失败用ATR fallback: {_cg_e}")
            sl = (mark - atr * 2.0) if side == "LONG" else (mark + atr * 2.0)

        # tick 精度
        try:
            _tick, _ = api.get_sym_info_cached(symbol)
            from entry_logic import fmt_price as _fmtp
            sl = float(_fmtp(sl, float(_tick)))
        except Exception:
            sl = round(sl, 6)

        close_side = "SELL" if side == "LONG" else "BUY"
        r = api.place_algo_order(symbol, close_side, "STOP_MARKET", sl, side, rem_qty)
        if isinstance(r, dict) and r.get("algoId"):
            log(f"[sl_adj] {symbol} 止损补挂成功 @{sl} algoId={r['algoId']}")
            notify(f"🛡️ [{symbol}] 止损已补挂 @{sl}")
            _ps_store.upsert(symbol, {"sl_price": sl})
        else:
            log(f"[sl_adj] {symbol} 止损补挂失败: {r}")
            notify(f"⚠️ [{symbol}] 止损补挂失败: {r}")
    except Exception as e:
        log(f"[sl_adj_err] {symbol}: {e}")
        notify(f"⚠️ [{symbol}] 止损补挂异常: {e}")


def _increment_addon_counter(symbol: str):
    """补仓计数器 +1"""
    try:
        data = {}
        if os.path.exists(COUNTER_FILE):
            with open(COUNTER_FILE) as f:
                data = json.load(f)
        data[symbol] = data.get(symbol, 0) + 1
        with open(COUNTER_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log(f"[counter_err] {e}")


def _reset_addon_counter(symbol: str):
    """平仓后清零补仓计数器"""
    try:
        data = {}
        if os.path.exists(COUNTER_FILE):
            with open(COUNTER_FILE) as f:
                data = json.load(f)
        if symbol in data:
            del data[symbol]
            with open(COUNTER_FILE, "w") as f:
                json.dump(data, f, indent=2)
    except Exception as e:
        log(f"[counter_reset_err] {e}")


# ─── 9. sl_tp_patrol_loop() ──────────────────────────────────────────────────

PATROL_INTERVAL    = 3 * 60    # 3分钟:止损单已有检查
PATROL_GATE_EVERY  = 10 * 60   # 10分钟:WS已实时触发门控,patrol仅作备份
PATROL_HEALTH_EVERY = 30 * 60  # P2: 30分钟:结构健康检查


def sl_tp_patrol_loop():
    """
    双层巡检:
    第一层(每3分钟):止损单存在性检查 + 门控备份
    第二层(每30分钟,P2新增):结构健康检查,重新评分持仓
    """
    last_gate_check   = 0.0
    last_short_check  = 0.0
    last_health_check = 0.0
    last_add_check    = 0.0   # P2-c: 补仓检查
    while not _stop_event.is_set():
        try:
            _patrol_check_sl()
        except Exception as e:
            log(f"[patrol_err] {e}")
        now = time.time()
        if now - last_gate_check >= PATROL_GATE_EVERY:
            try:
                _patrol_check_gates()
                last_gate_check = now
            except Exception as e:
                log(f"[patrol_gates_err] {e}")
        if now - last_short_check >= 300:
            try:
                _patrol_short_protection()
                last_short_check = now
            except Exception as e:
                log(f"[patrol_short_err] {e}")
        if now - last_health_check >= PATROL_HEALTH_EVERY:
            try:
                # 新版仓位维护:重新评分 + 横盘收紧 SL/TP(替代旧的 _patrol_health_check)
                _patrol_position_maintain()
                last_health_check = now
            except Exception as e:
                log(f"[patrol_maintain_err] {e}")
        # P2-c: 补仓检查(每5分钟)
        if now - last_add_check >= 300:
            try:
                ps_snap = _load_pos_state()
                for _sym in list(ps_snap.keys()):
                    from check_add_position import check_and_add as _chk_add
                    _chk_add(_sym, notify_fn=notify)
                last_add_check = now
            except Exception as e:
                log(f"[patrol_add_err] {e}")
        _stop_event.wait(PATROL_INTERVAL)

# ─────────────────────────────────────────────────────────────────────────────
# 第五步:仓位维护(每30分钟,与扫描同周期)
# 功能1: 重新评分 → AI 判断是否市价平仓
# 功能2: 横盘震荡识别 → 收紧 SL/TP
# ─────────────────────────────────────────────────────────────────────────────

def _detect_sideways(k1h: list, atr: float) -> tuple:
    """
    横盘震荡识别。
    判断依据(三维综合):
      1. BB 带宽:(BB上轨-BB下轨) / 中轨 < 0.06(6%),说明波动收窄
      2. ATR 比率:当前 ATR < 近20根平均 ATR × 0.6(波动率萎缩)
      3. 方向性:近10根K线,涨跌各占40~60%(没有明确方向)
    三个条件满足2个以上 → 横盘震荡
    返回 (is_sideways: bool, reason: str)
    """
    if len(k1h) < 20:
        return False, "数据不足"
    c1h = [float(k[4]) for k in k1h]
    h1h = [float(k[2]) for k in k1h]
    l1h = [float(k[3]) for k in k1h]

    # BB 带宽
    sma20 = sum(c1h[-20:]) / 20
    std20 = (sum((x - sma20) ** 2 for x in c1h[-20:]) / 20) ** 0.5
    bb_u  = sma20 + 2 * std20
    bb_l  = sma20 - 2 * std20
    bb_width_pct = (bb_u - bb_l) / sma20 if sma20 > 0 else 0.1
    cond_bb = bb_width_pct < 0.06

    # ATR 比率:当前 ATR vs 近20根历史平均
    atr_hist = [abs(h1h[i] - l1h[i]) for i in range(-20, 0)]
    atr_avg  = sum(atr_hist) / 20 if atr_hist else atr
    cond_atr = atr < atr_avg * 0.6

    # 方向性:近10根涨跌比
    up_count   = sum(1 for i in range(-10, 0) if c1h[i] > c1h[i-1])
    down_count = 10 - up_count
    cond_dir   = 4 <= up_count <= 6  # 涨跌各占 40~60%,无明确方向

    hit = sum([cond_bb, cond_atr, cond_dir])
    reasons = []
    if cond_bb:  reasons.append(f"BB带宽{bb_width_pct*100:.1f}%<6%")
    if cond_atr: reasons.append(f"ATR萎缩{atr/atr_avg:.2f}x")
    if cond_dir: reasons.append(f"方向性弱(涨{up_count}/跌{down_count})")
    is_sideways = hit >= 2
    return is_sideways, ",".join(reasons) if reasons else "趋势明确"


def _tighten_sl_tp(sym: str, pos_side: str, entry: float, qty: float,
                   mark: float, atr: float, sideways_reason: str):
    """
    横盘震荡时收紧止盈止损。
    收紧逻辑:
      SL: 收到现价 ± ATR×0.8(原来可能是 ±ATR×2.0,收紧到 0.8)
          方向:多单 SL 只能上移(不能往亏损方向动),空单 SL 只能下移
      TP: TP1 收到现价 ± ATR×1.0(压缩目标,在震荡中尽快锁利)
          只有 TP1 > 现价(多)/ TP1 < 现价(空)才收紧,否则已成交或无意义
    """
    close_side = "SELL" if pos_side == "LONG" else "BUY"

    # ── 计算新 SL/TP ──────────────────────────────────────────────────────
    if pos_side == "LONG":
        new_sl  = round(mark - atr * 0.8, 8)
        new_tp1 = round(mark + atr * 1.0, 8)
    else:
        new_sl  = round(mark + atr * 0.8, 8)
        new_tp1 = round(mark - atr * 1.0, 8)

    # 安全检查:SL 不能穿越入场价(盈利中才收紧,亏损不动)
    in_profit = (pos_side == "LONG" and mark > entry) or (pos_side == "SHORT" and mark < entry)
    if not in_profit:
        log(f"[tighten] {sym} 亏损状态({mark:.4g} vs entry={entry:.4g}),不收紧止盈止损")
        return

    changes = []

    # ── 收紧 SL ───────────────────────────────────────────────────────────
    try:
        algo_orders = api.get_algo_open_orders(sym) or []
        sl_orders = [a for a in algo_orders if "STOP" in a.get("orderType", "")]
        if sl_orders:
            cur_sl = float(sl_orders[0].get("triggerPrice", 0))
            # SL 只能向盈利方向移动
            sl_ok = (pos_side == "LONG" and new_sl > cur_sl) or                     (pos_side == "SHORT" and new_sl < cur_sl)
            # 安全距离:至少离现价 ATR×0.3
            safe = abs(mark - new_sl) >= atr * 0.3
            if sl_ok and safe:
                old_id = int(sl_orders[0]["algoId"])
                api.cancel_algo_order(sym, old_id)
                r_sl = api.place_algo_order(sym, close_side, "STOP_MARKET",
                                            new_sl, pos_side, qty)
                if isinstance(r_sl, dict) and r_sl.get("algoId"):
                    changes.append(f"SL收紧 {cur_sl:.6g}→{new_sl:.6g}")
                    _ps_store.upsert(sym, {"sl_price": new_sl})
    except Exception as e:
        log(f"[tighten] {sym} SL收紧异常: {e}")

    # ── 收紧 TP1 ──────────────────────────────────────────────────────────
    try:
        open_orders = api.get_open_orders(sym) or []
        tp_orders = sorted(
            [o for o in open_orders if o.get("reduceOnly") and o.get("type") == "LIMIT"],
            key=lambda o: float(o.get("price", 0)),
            reverse=(pos_side == "SHORT")
        )
        if tp_orders:
            cur_tp1 = float(tp_orders[0]["price"])
            # TP1 只向更快盈利方向收紧(更近)
            tp_closer = (pos_side == "LONG" and new_tp1 < cur_tp1 and new_tp1 > mark) or                         (pos_side == "SHORT" and new_tp1 > cur_tp1 and new_tp1 < mark)
            if tp_closer:
                tp1_qty = int(float(tp_orders[0].get("origQty", qty / 2)))
                try:
                    from entry_logic import fmt_price as _fmtp_t
                    _tick_t, _ = api.get_sym_info_cached(sym)
                    tp1_fmt = _fmtp_t(new_tp1, float(_tick_t))
                    api.cancel_order(sym, int(tp_orders[0]["orderId"]))
                    r_tp = api.place_order(sym, close_side, "LIMIT",
                                           quantity=str(tp1_qty), price=tp1_fmt,
                                           positionSide=pos_side, timeInForce="GTC")
                    if isinstance(r_tp, dict) and r_tp.get("orderId"):
                        changes.append(f"TP1收紧 {cur_tp1:.6g}→{tp1_fmt}")
                        _ps_store.upsert(sym, {"tp1_price": new_tp1})
                except Exception as e2:
                    log(f"[tighten] {sym} TP1收紧下单异常: {e2}")
    except Exception as e:
        log(f"[tighten] {sym} TP1收紧异常: {e}")

    if changes:
        notify(f"📐 [{sym}] 横盘震荡({sideways_reason}),止盈止损收紧: {' | '.join(changes)}")
        log(f"[tighten] {sym} 收紧完成: {changes}")
    else:
        log(f"[tighten] {sym} 横盘识别但无需调整(已是最优位置)")


def _patrol_position_maintain():
    """
    仓位维护(每30分钟,与扫描同步执行)。

    功能1 - 重新评分:
      ≥60:健康,继续持有,清除预警
      40~59:推送预警,准备离场(首次标记,30分钟后若仍<40则平仓)
      <40:进入双次确认机制,30分钟后再检查;仍<40 → AI 判断是否市价平仓
      <20 或 反向信号+<35:立即平仓(无需二次确认)

    功能2 - SL/TP 维护:
      横盘震荡识别 → 收紧 TP1 和 SL 到 ATR×1.0 / ATR×0.8
      趋势强劲(评分≥70)→ 调用 _dynamic_update_sl_tp() 更新到最新清算区
    """
    ps = _load_pos_state()
    if not ps:
        return

    try:
        live_positions = api.get_positions()
        live_map = {p["symbol"]: p for p in live_positions
                    if abs(float(p.get("positionAmt", 0))) > 0}
    except Exception as e:
        log(f"[maintain] get_positions失败: {e}")
        return

    now = time.time()
    WARN_KEY = "health_warn_ts"

    for sym, pos_data in ps.items():
        if sym not in live_map:
            if pos_data.get(WARN_KEY):
                _ps_store.upsert(sym, {WARN_KEY: 0})
            continue

        live_p     = live_map[sym]
        direction  = pos_data.get("direction", "LONG")
        entry      = float(pos_data.get("entry_price", 0))
        sig_type   = pos_data.get("sig_type", "trend")
        warn_ts    = float(pos_data.get(WARN_KEY, 0))
        qty        = abs(float(live_p.get("positionAmt", 0)))
        mark       = float(live_p.get("markPrice", 0))
        if entry <= 0 or qty <= 0 or mark <= 0:
            continue

        # 新仓保护期已移除:严格按评分系统执行,不设保护窗口
        # 若评分骤跌说明被诱多/诱空,下一轮维护直接处理

        try:
            # ── 拉 K 线 ──────────────────────────────────────────────────
            k1h  = api.get_klines(sym, "1h", 100)
            k4h  = api.get_klines(sym, "4h", 50)
            k15m = api.get_klines(sym, "15m", 20)
            tk   = api.get_mark_price(sym) or {}
            fr_raw  = float(tk.get("lastFundingRate", 0))
            chg24   = float(tk.get("priceChangePercent", 0) or
                            (live_p.get("priceChangePercent", 0)))
            from entry_logic import (
                calc_score_trend, calc_score_reversal,
                calc_score_trend_short, calc_score_short,
                classify_signal, atr_calc,
            )
            atr = atr_calc(k1h)

            # ── 功能1:重新评分 ──────────────────────────────────────────
            if direction == "LONG":
                score_now, details = (
                    calc_score_reversal if "reversal" in sig_type
                    else calc_score_trend
                )(sym, k1h, k4h, k15m, tk, fr_raw)
            else:
                score_now, details = (
                    calc_score_short if "reversal" in sig_type
                    else calc_score_trend_short
                )(sym, k1h, k4h, k15m, tk, fr_raw)

            # 反向信号检测
            rev_sig     = classify_signal(k1h, k4h, fr_raw, chg24)
            has_reverse = (
                (direction == "LONG"  and "short" in rev_sig) or
                (direction == "SHORT" and "short" not in rev_sig and "trend" in rev_sig)
            )
            pnl_pct = (mark - entry) / entry * (1 if direction == "LONG" else -1) * 100

            def _do_close(reason: str):
                close_side = "SELL" if direction == "LONG" else "BUY"
                try:
                    r = api.place_order(sym, close_side, "MARKET",
                                        quantity=str(qty), positionSide=direction)
                    if isinstance(r, dict) and r.get("orderId"):
                        notify(f"✅ [{sym}] 仓位维护平仓 #{r['orderId']}\n原因: {reason}\n评分={score_now:.0f} PnL={pnl_pct:+.2f}%")
                        _ps_store.upsert(sym, {WARN_KEY: 0})
                    else:
                        notify(f"⚠️ [{sym}] 维护平仓失败: {r}")
                except Exception as ce:
                    notify(f"⚠️ [{sym}] 维护平仓异常: {ce}")

            log(f"[maintain] {sym} {direction} score={score_now:.0f} pnl={pnl_pct:+.2f}% rev={has_reverse}")

            # ── 直接平仓(无需二次确认)────────────────────────────────────
            if score_now < 20:
                notify(f"🚨 [{sym}] 评分崩溃({score_now:.0f}<20),直接平仓")
                _do_close(f"评分崩溃{score_now:.0f}")
                continue

            if has_reverse and score_now < 35:
                notify(f"🚨 [{sym}] 反向信号+评分{score_now:.0f},直接平仓")
                _do_close(f"反向信号+评分{score_now:.0f}")
                continue

            # ── 评分<40:直接按盈亏决策(无保护期,无双次确认)─────────────────
            # 评分骤跌<40 = 信号失效/被诱多诱空;亏损直接平,释放保证金给后续信号
            if score_now < 40:
                pnl_abs = round((mark - entry) * qty * (1 if direction == "LONG" else -1), 2)
                rsi_str = f"RSI1H={details.get('rsi_1h', 0):.0f} RSI4H={details.get('rsi_4h', 0):.0f}"
                if pnl_pct < 0:
                    _do_close(f"评分{score_now:.0f}<40+亏损{pnl_pct:.2f}%,释放保证金")
                else:
                    notify(
                        f"⚠️ [{sym}] 评分跌破40({score_now:.0f})有浮盈{pnl_pct:+.2f}%({pnl_abs:+.1f}U)\n"
                        f"  {rsi_str} 反向={has_reverse}  不强制平,下一轮继续评估"
                    )
                    _ps_store.upsert(sym, {WARN_KEY: now})
                # 评分<40 不做 SL/TP 调整
                continue

            # ── 评分40~59:轻预警 + 执行 SL/TP 维护 ────────────────────────
            if score_now < 60:
                if warn_ts > 0:
                    _ps_store.upsert(sym, {WARN_KEY: 0})
                log(f"[maintain] {sym} 评分弱{score_now:.0f},推送预警并维护止盈止损")

            # ── 评分≥60:清除预警 ──────────────────────────────────────────
            if score_now >= 60 and warn_ts > 0:
                _ps_store.upsert(sym, {WARN_KEY: 0})
                log(f"[maintain] {sym} 评分回升{score_now:.0f},解除预警")

            # ── 功能2:SL/TP 维护 ──────────────────────────────────────────
            is_sideways, sideways_reason = _detect_sideways(k1h, atr)

            if is_sideways:
                # 横盘震荡:收紧 SL/TP
                log(f"[maintain] {sym} 横盘识别({sideways_reason}),收紧止盈止损")
                _tighten_sl_tp(sym, direction, entry, qty, mark, atr, sideways_reason)
                if score_now < 60:
                    notify(f"⚠️ [{sym}] 趋势弱化+横盘震荡 评分={score_now:.0f} {sideways_reason}\n  已收紧止盈止损,建议关注")
            elif score_now >= 70:
                # 趋势强劲:更新 SL/TP 到最新 Coinglass 清算区
                log(f"[maintain] {sym} 趋势强劲(score={score_now:.0f}),更新SL/TP到最新清算区")
                try:
                    import coinglass_api as _cg_m
                    zones = _cg_m.get_liq_zones(sym, mark)
                    if zones:
                        _update_sl_tp_from_zones(sym, direction, qty, mark, entry, atr, zones)
                except Exception as _cg_e:
                    log(f"[maintain] {sym} CG更新异常: {_cg_e}")
            else:
                log(f"[maintain] {sym} 评分{score_now:.0f},趋势一般,维持当前SL/TP")

        except Exception as e:
            log(f"[maintain] {sym} 检查异常: {e}")


def _update_sl_tp_from_zones(sym: str, side: str, qty: float,
                              mark: float, entry: float, atr: float, zones: dict):
    """
    趋势强劲时,把 SL/TP 更新到最新 Coinglass 最大量清算区。
    SL 只能向盈利方向移动;TP 只向更远盈利目标移动。
    """
    close_side = "SELL" if side == "LONG" else "BUY"
    _MIN_MOVE = 0.003   # 至少移动 0.3% 才执行

    # ── 计算新 SL(最大量清算区)───────────────────────────────────────────
    if side == "LONG":
        below = [z for z in zones.get("below", []) if z[0] < mark]
        new_sl = max(below, key=lambda z: z[1] if len(z)>1 else 0)[0] - atr * 0.1                  if below else None
    else:
        above = [z for z in zones.get("above", []) if z[0] > mark]
        new_sl = min(above, key=lambda z: z[1] if len(z)>1 else 0)[0] + atr * 0.1                  if above else None

    # ── 计算新 TP(最大量清算区,要求最小距离)────────────────────────────
    if side == "LONG":
        above = [z for z in zones.get("above", []) if z[0] > mark * 1.02]
        new_tp1 = max(above, key=lambda z: z[1] if len(z)>1 else 0)[0] * 0.999                   if above else None
    else:
        below = [z for z in zones.get("below", []) if z[0] < mark * 0.98]
        new_tp1 = max(below, key=lambda z: z[1] if len(z)>1 else 0)[0] * 1.001                   if below else None

    changes = []

    # ── 更新 SL ───────────────────────────────────────────────────────────
    if new_sl:
        try:
            algo_orders = api.get_algo_open_orders(sym) or []
            sl_orders = [a for a in algo_orders if "STOP" in a.get("orderType", "")]
            if sl_orders:
                cur_sl = float(sl_orders[0].get("triggerPrice", 0))
                move_ok  = (side == "LONG" and new_sl > cur_sl) or                             (side == "SHORT" and new_sl < cur_sl)
                move_big = abs(new_sl - cur_sl) / max(cur_sl, 1e-9) >= _MIN_MOVE
                safe_gap = abs(mark - new_sl) >= atr * 0.5
                if move_ok and move_big and safe_gap:
                    old_id = int(sl_orders[0]["algoId"])
                    api.cancel_algo_order(sym, old_id)
                    r = api.place_algo_order(sym, close_side, "STOP_MARKET",
                                             new_sl, side, qty)
                    if isinstance(r, dict) and r.get("algoId"):
                        changes.append(f"SL {cur_sl:.6g}→{new_sl:.6g}")
                        _ps_store.upsert(sym, {"sl_price": new_sl})
        except Exception as e:
            log(f"[update_zones] {sym} SL更新异常: {e}")

    # ── 更新 TP1 ──────────────────────────────────────────────────────────
    if new_tp1:
        try:
            open_orders = api.get_open_orders(sym) or []
            tp_orders = sorted(
                [o for o in open_orders if o.get("reduceOnly") and o.get("type") == "LIMIT"],
                key=lambda o: float(o.get("price", 0)),
                reverse=(side == "SHORT")
            )
            if tp_orders:
                cur_tp1 = float(tp_orders[0]["price"])
                tp_better = (side == "LONG" and new_tp1 > cur_tp1) or                             (side == "SHORT" and new_tp1 < cur_tp1)
                move_big  = abs(new_tp1 - cur_tp1) / max(cur_tp1, 1e-9) >= _MIN_MOVE
                if tp_better and move_big:
                    tp1_qty = int(float(tp_orders[0].get("origQty", qty / 2)))
                    try:
                        from entry_logic import fmt_price as _fmtp2
                        _tick2, _ = api.get_sym_info_cached(sym)
                        tp1_fmt2 = _fmtp2(new_tp1, float(_tick2))
                        api.cancel_order(sym, int(tp_orders[0]["orderId"]))
                        r2 = api.place_order(sym, close_side, "LIMIT",
                                             quantity=str(tp1_qty), price=tp1_fmt2,
                                             positionSide=side, timeInForce="GTC")
                        if isinstance(r2, dict) and r2.get("orderId"):
                            changes.append(f"TP1 {cur_tp1:.6g}→{tp1_fmt2}")
                            _ps_store.upsert(sym, {"tp1_price": new_tp1})
                    except Exception as e2:
                        log(f"[update_zones] {sym} TP1更新下单异常: {e2}")
        except Exception as e:
            log(f"[update_zones] {sym} TP1更新异常: {e}")

    if changes:
        log(f"[update_zones] {sym} 趋势更新: {changes}")
    else:
        log(f"[update_zones] {sym} 无需更新(已是最优)")


def _patrol_health_check():
    """
    Q1: 结构健康检查(每30分钟)双次确认机制。

    评分≥60       → 继续持有,清除预警
    评分40~59     → 推送警告,准备离场
    评分<40       → 第一次:标记预警;第二次(30min后仍<40)→ 执行平仓
    直接平仓条件(不需二次确认):
      - 评分<20(信号完全崩溃)
      - 反向信号 + 评分<40
      - 聪明钱大幅反向(暂留空位,待 trade_logger 统计后启用)
    """
    ps = _load_pos_state()
    if not ps:
        return
    try:
        live_positions = api.get_positions()
        live_syms = {p["symbol"] for p in live_positions
                     if abs(float(p.get("positionAmt", 0))) > 0}
    except Exception as e:
        log(f"[health] get_positions失败: {e}")
        return

    now = time.time()
    HEALTH_WARN_KEY = "health_warn_ts"  # pos_state 里的预警时间戳字段

    for sym, pos_data in ps.items():
        if sym not in live_syms:
            # 清理预警(已无仓位)
            if pos_data.get(HEALTH_WARN_KEY):
                _ps_store.upsert(sym, {HEALTH_WARN_KEY: 0})
            continue

        direction   = pos_data.get("direction", "LONG")
        entry_price = float(pos_data.get("entry_price", 0))
        sig_type    = pos_data.get("sig_type", "trend")
        warn_ts     = float(pos_data.get(HEALTH_WARN_KEY, 0))
        if entry_price <= 0:
            continue

        # 新仓保护期已移除:严格按评分执行

        try:
            k1h  = api.get_klines(sym, "1h", 100)
            k4h  = api.get_klines(sym, "4h", 50)
            k15m = api.get_klines(sym, "15m", 20)
            tk   = api.get_mark_price(sym) or {}
            fr_raw = float(tk.get("lastFundingRate", 0))
            from entry_logic import (
                calc_score_trend, calc_score_reversal,
                calc_score_trend_short, calc_score_short,
                classify_signal,
            )
            if direction == "LONG":
                score_now, _ = (calc_score_reversal if "reversal" in sig_type
                                else calc_score_trend)(sym, k1h, k4h, k15m, tk, fr_raw)
            else:
                score_now, _ = (calc_score_short if "reversal" in sig_type
                                else calc_score_trend_short)(sym, k1h, k4h, k15m, tk, fr_raw)

            # 反向信号
            chg24 = float(tk.get("priceChangePercent", 0))
            rev_sig = classify_signal(k1h, k4h, fr_raw, chg24)
            has_reverse = (
                (direction == "LONG"  and "short" in rev_sig) or
                (direction == "SHORT" and "short" not in rev_sig and "trend" in rev_sig)
            )

            def _do_close(reason: str):
                close_side = "SELL" if direction == "LONG" else "BUY"
                qty = abs(float([p for p in live_positions if p["symbol"] == sym][0].get("positionAmt", 0)))
                try:
                    r = api.place_order(sym, close_side, "MARKET", quantity=qty, positionSide=direction)
                    if r.get("orderId"):
                        notify(f"✅ [{sym}] 健康平仓 #{r['orderId']}({reason})")
                        _ps_store.upsert(sym, {HEALTH_WARN_KEY: 0})
                    else:
                        notify(f"⚠️ [{sym}] 健康平仓失败: {r}")
                except Exception as ce:
                    notify(f"⚠️ [{sym}] 健康平仓异常: {ce}")

            # ── 直接平仓(不需二次确认)────────────────────────────────────
            if score_now < 20:
                log(f"[health] {sym} 评分崩溃={score_now:.0f}<20,直接平仓")
                notify(f"🚨 [{sym}] 健康检查:评分{score_now:.0f}崩溃,直接平仓")
                _do_close(f"评分崩溃{score_now:.0f}")
                continue

            if has_reverse and score_now < 35:  # 原40,收紧避免正常波动触发
                log(f"[health] {sym} 反向信号+评分{score_now:.0f},直接平仓")
                notify(f"🚨 [{sym}] 健康检查:反向信号+评分{score_now:.0f},直接平仓")
                _do_close(f"反向信号+评分{score_now:.0f}")
                continue

            # ── 评分<40:双次确认 ────────────────────────────────────────────
            if score_now < 40:
                if warn_ts == 0:
                    # 第一次:标记预警,30分钟后再确认
                    _ps_store.upsert(sym, {HEALTH_WARN_KEY: now})
                    notify(
                        f"⚠️ [{sym}] 健康预警(第1次):评分{score_now:.0f}<40\n"
                        f"  30分钟后第二次检查,若仍<40则执行平仓"
                    )
                    log(f"[health] {sym} 第一次预警 score={score_now:.0f},等待二次确认")
                elif now - warn_ts >= 30 * 60:
                    # 第二次确认:已超过30分钟
                    log(f"[health] {sym} 第二次确认 score={score_now:.0f},执行平仓")
                    notify(f"🚨 [{sym}] 健康检查(第2次确认):评分{score_now:.0f}<40,执行平仓")
                    _do_close(f"二次确认评分{score_now:.0f}")
                else:
                    # 仍在30分钟等待中
                    remain = int((warn_ts + 30*60 - now) / 60)
                    log(f"[health] {sym} 预警中,{remain}min后第二次确认 score={score_now:.0f}")
                continue

            # ── 评分40~59:推送警告,准备离场,清除预警 ─────────────────────
            if score_now < 60:
                if warn_ts > 0:
                    _ps_store.upsert(sym, {HEALTH_WARN_KEY: 0})  # 回升到40以上,解除预警
                notify(
                    f"⚠️ [{sym}] 健康检查:评分{score_now:.0f}(40~59),入场逻辑弱化\n"
                    f"  建议手动评估或紧缩止盈"
                )
                continue

            # ── 评分≥60:健康,清除预警 ─────────────────────────────────────
            if warn_ts > 0:
                _ps_store.upsert(sym, {HEALTH_WARN_KEY: 0})
                log(f"[health] {sym} 评分回升至{score_now:.0f},解除预警")
            else:
                log(f"[health] {sym} 评分{score_now:.0f},健康,继续持有")

        except Exception as e:
            log(f"[health] {sym} 检查异常: {e}")


def _patrol_short_protection():
    """
    Fix P1-7: 轧空保护 - SHORT仓浮亏>4% 且当前价格仍高于入场价 → 市价平仓
    Fix P1-8: 空单分批止盈三档(已成交第一档才跳过)
    Fix P1-9: 48h 未达第一档止盈 → 强制评估推送
    """
    ps = _load_pos_state()
    short_positions = {
        sym: pos for sym, pos in ps.items()
        if pos.get("direction") == "SHORT"
    }
    if not short_positions:
        return

    try:
        live_positions = api.get_positions()
        live_map = {p["symbol"]: p for p in live_positions
                    if float(p.get("positionAmt", 0)) < 0}
    except Exception as e:
        log(f"[patrol_short] get_positions failed: {e}")
        return

    for sym, pos in short_positions.items():
        if sym not in live_map:
            continue
        live = live_map[sym]
        entry_price  = float(pos.get("entry_price", 0))
        tp1_price    = float(pos.get("tp1_price", 0))
        opened_at    = float(pos.get("opened_at", 0))
        tp1_hit      = pos.get("tp1_hit", False)
        qty          = abs(float(live.get("positionAmt", 0)))
        mark_price   = float(live.get("markPrice", entry_price))
        unrealized   = float(live.get("unRealizedProfit", 0))

        if entry_price <= 0 or qty <= 0:
            continue

        # ── Fix P1-7: 轧空保护 ─────────────────────────────────────────────────
        pnl_pct = unrealized / (entry_price * qty) if entry_price * qty > 0 else 0
        still_rising = mark_price > entry_price * 1.005   # 价格仍高于入场价
        if pnl_pct <= -0.04 and still_rising:  # Fix: <=,浮亏达到4%即触发
            log(f"[squeeze_protect] {sym} 浮亏{pnl_pct*100:.1f}%且价格仍在涨,主动平仓")
            try:
                r = api.place_order(sym, "BUY", "MARKET", quantity=qty, positionSide="SHORT")
                if r.get("orderId"):
                    notify(f"🛡 [{sym}] 轧空保护触发\n浮亏{pnl_pct*100:.1f}%,价格仍在涨\n已市价平仓,订单#{r['orderId']}")
                    # 写冷静期2h
                    try:
                        import importlib.util as _ilu
                        _spec = _ilu.spec_from_file_location("scanner", f"{WORKSPACE}/scanner.py")
                        _sm = _ilu.module_from_spec(_spec); _spec.loader.exec_module(_sm)
                        _sm._add_to_cooldown(sym, "SHORT", 2.0)
                    except Exception: pass
            except Exception as e:
                log(f"[squeeze_protect] {sym} 平仓失败: {e}")
            continue

        # ── Fix P1-9: 48h 未达第一档止盈强制评估 ──────────────────────────────
        if not tp1_hit and opened_at > 0:
            hours_held = (time.time() - opened_at) / 3600
            if hours_held >= 48:
                notify(
                    f"⚠️ 空单 [{sym}] 持仓已 {hours_held:.0f}h,未达第一档止盈 {tp1_price}\n"
                    f"当前浮亏/盈: {unrealized:+.2f}U  入场: {entry_price}  现价: {mark_price}\n"
                    f"建议:手动评估是否继续持有或止损出局"
                )
                log(f"[48h_alert] {sym} 持仓{hours_held:.0f}h未达TP1,已推送评估通知")


def _patrol_check_gates():
    """
    用 REST API 获取当前价格,主动检查门控是否应触发。
    解决 WebSocket 不通时门控永远不触发的问题。
    每 3 分钟由 patrol_loop 调用一次。
    """
    try:
        gates = _load_gates()
    except Exception:
        return
    for sym, sym_gates in gates.items():
        unfired = [g for g in sym_gates if not g.get("fired")]
        if not unfired:
            continue
        try:
            mark = float(api.get_mark_price(sym)["markPrice"])
            check_gates(sym, mark)
        except Exception as e:
            log(f"[patrol_gates] {sym} 获取价格失败: {e}")




def _patrol_check_sl():
    """检查所有当前仓位的止损单,缺失则补挂"""
    try:
        positions = api.get_positions()
    except Exception as e:
        log(f"[patrol_positions_err] {e}")
        return

    for pos in positions:
        sym = pos.get("symbol", "")
        amt = float(pos.get("positionAmt", 0))
        if abs(amt) == 0:
            continue
        side = "LONG" if amt > 0 else "SHORT"
        qty  = abs(amt)
        entry = float(pos.get("entryPrice", 0))

        # 直接用 REST 查询止损单,不依赖缓存(缓存不可靠,会导致重复补挂)
        try:
            real_algos = api.get_algo_open_orders(sym) or []
            has_sl = any("STOP" in a.get("orderType", "") for a in real_algos)
        except Exception as _e:
            log(f"[patrol_algo_err] {sym}: {_e}")
            has_sl = True  # 查询失败保守:不补挂
        if not has_sl:
            log(f"[patrol] {sym} 缺少止损单,触发补挂 Agent")
            _spawn_sl_adj_agent(sym, qty, side, entry)

        # -- Patrol 备份补挂 TP(open_filled 漏捕时的安全网)--------------
        # 只在有 pos_state 记录且知道 tp1_price 时才补挂,避免玲挂无效价格
        try:
            _ps_data = _load_pos_state().get(sym, {})
            _tp1 = float(_ps_data.get("tp1_price", 0))
            _tp2 = float(_ps_data.get("tp2_price", 0))
            if _tp1 > 0:
                _existing_tp = [o for o in api.get_open_orders(sym)
                                if o.get("type") in ("LIMIT", "TAKE_PROFIT", "LIMIT_TP")
                                and o.get("reduceOnly")]
                if not _existing_tp:
                    import math as _math
                    # 拉取真实 tickSize/quantityPrecision
                    try:
                        _sym_info = api.get_sym_info_cached(sym) or {}
                        _tick = float(next(
                            (f["tickSize"] for f in _sym_info.get("filters", [])
                             if f["filterType"] == "PRICE_FILTER"), 0.0001))
                        _qty_prec = int(_sym_info.get("quantityPrecision", 2))
                    except Exception:
                        _tick = 0.0001; _qty_prec = 2
                    def _fp(p, _t=_tick):
                        d = max(0, -int(_math.floor(_math.log10(_t))))
                        return f"{round(round(p/_t)*_t, d):.{d}f}"
                    _close_side = "SELL" if side == "LONG" else "BUY"
                    _qty1 = round(qty / 2, _qty_prec)
                    _qty2 = round(qty - _qty1, _qty_prec)
                    _r1 = api.place_order(sym, _close_side, "LIMIT",
                                         quantity=str(_qty1), price=_fp(_tp1),
                                         positionSide=side, timeInForce="GTC")
                    if _tp2 > 0:
                        _r2 = api.place_order(sym, _close_side, "LIMIT",
                                             quantity=str(_qty2), price=_fp(_tp2),
                                             positionSide=side, timeInForce="GTC")
                    if _r1.get("orderId"):
                        log(f"[patrol_tp] {sym} 止盈备份补挂 TP1@{_fp(_tp1)} TP2@{_fp(_tp2) if _tp2 else '-'}")
                        notify(f"⚠️ [{sym}] patrol 止盈补挂 TP1@{_fp(_tp1)}(open_filled 遗漏)")
        except Exception as _tpe:
            log(f"[patrol_tp_err] {sym}: {_tpe}")


# ─── 10. main() ──────────────────────────────────────────────────────────────

# ── 动态 SL/TP 更新(每次 scanner_loop 触发后执行)──────────────────────────
_SL_MIN_MOVE_PCT  = 0.005   # SL 变化 < 0.5% 不更新(避免频繁撤单)
_TP_MIN_MOVE_PCT  = 0.008   # TP 变化 < 0.8% 不更新
_SL_SAFETY_BUFFER = 0.003   # SL 与现价保持至少 0.3% 安全距离


def _dynamic_update_sl_tp():
    """
    每次扫描后对所有持仓执行动态 SL/TP 更新。
    原理:
      - SL:取 CG 热力图下方(做多)/ 上方(做空)最近清算密集区,
            只允许往盈利方向移动,不得放宽。
      - TP:取 CG 热力图上方(做多)/ 下方(做空)最近清算密集区,
            若新 TP 比现有高(多头)则更新,否则保持不变。
    """
    try:
        positions = api.get_positions()
        if not positions:
            return
        import coinglass_api as _cg_dyn
        for p in positions:
            sym   = p["symbol"]
            amt   = float(p["positionAmt"])
            ep    = float(p["entryPrice"])
            mp    = float(p["markPrice"])
            side  = "LONG" if amt > 0 else "SHORT"
            qty   = abs(amt)
            pos_side = "LONG" if side == "LONG" else "SHORT"
            close_side = "SELL" if side == "LONG" else "BUY"

            try:
                # ── 拉 CG 热力图 ──────────────────────────────────────────
                zones = _cg_dyn.get_liq_zones(sym, mp)
                if not zones or (not zones.get("above") and not zones.get("below")):
                    continue

                # ── 计算新 SL ─────────────────────────────────────────────
                if side == "LONG":
                    # 做多 SL:下方最近清算密集区(第1个)
                    new_sl = zones["below"][0][0] if zones["below"] else None
                else:
                    # 做空 SL:上方最近清算密集区(第1个)
                    new_sl = zones["above"][0][0] if zones["above"] else None

                # ── 计算新 TP ─────────────────────────────────────────────
                if side == "LONG":
                    new_tp1 = zones["above"][0][0] if zones["above"] else None
                    new_tp2 = zones["above"][1][0] if len(zones["above"]) > 1 else None
                else:
                    new_tp1 = zones["below"][0][0] if zones["below"] else None
                    new_tp2 = zones["below"][1][0] if len(zones["below"]) > 1 else None

                # ── 获取当前 Algo 单 ───────────────────────────────────────
                algo_orders = api.get_algo_open_orders_cached()
                if not isinstance(algo_orders, list):
                    algo_orders = []
                sym_algos = [a for a in algo_orders if a.get("symbol") == sym]

                cur_sl  = next((float(a["triggerPrice"]) for a in sym_algos
                                if a.get("orderType") == "STOP_MARKET"), None)
                # 止盈已改为 LIMIT 限价单,从普通挂单里找
                sym_orders = [o for o in api.get_open_orders(sym) if o.get("reduceOnly")]
                tp_orders  = sorted([o for o in sym_orders if o.get("type") == "LIMIT"],
                                    key=lambda o: float(o.get("price", 0)),
                                    reverse=(side == "SHORT"))  # SHORT: 止盈价从高到低
                cur_tp1 = float(tp_orders[0]["price"]) if len(tp_orders) > 0 else None
                cur_tp2 = float(tp_orders[1]["price"]) if len(tp_orders) > 1 else None

                changes = []

                # ── 更新 SL ───────────────────────────────────────────────
                if new_sl and cur_sl:
                    move_pct = abs(new_sl - cur_sl) / cur_sl
                    # 只向盈利方向移动:多头 SL 只能上移,空头 SL 只能下移
                    direction_ok = (side == "LONG" and new_sl > cur_sl) or \
                                   (side == "SHORT" and new_sl < cur_sl)
                    # 安全距离:SL 不能太接近现价
                    safe_dist = abs(mp - new_sl) / mp > _SL_SAFETY_BUFFER
                    if direction_ok and safe_dist and move_pct >= _SL_MIN_MOVE_PCT:
                        # 撤旧 SL,挂新 SL
                        old_algo_id = next((int(a["algoId"]) for a in sym_algos
                                            if a.get("orderType") == "STOP_MARKET"), None)
                        if old_algo_id:
                            api.cancel_algo_order(sym, old_algo_id)
                            api.algo_cache_remove_order(sym, old_algo_id)
                        r = api.place_algo_order(sym, close_side, "STOP_MARKET",
                                                 new_sl, pos_side, qty)
                        if isinstance(r, dict) and r.get("algoId"):
                            api.algo_cache_save(sym, "STOP_MARKET",
                                                int(r["algoId"]), str(new_sl))
                            changes.append(f"SL {cur_sl:.6g}→{new_sl:.6g}")
                            log(f"[dyn_sl] {sym} SL更新 {cur_sl:.6g}→{new_sl:.6g}")

                elif new_sl and not cur_sl:
                    # 没有止损单 → 直接补挂
                    # 先 REST 验证,再补挂(防缓存不同步导致重复挂单)
                    try:
                        rest_algos = api.get_algo_open_orders(sym) or []
                        existing_sl = [a for a in rest_algos if a.get("orderType") == "STOP_MARKET"]
                        if existing_sl:
                            # REST 显示已有止损,取消旧的再挂新的(避免重复)
                            for old_sl in existing_sl:
                                api.cancel_algo_order(sym, int(old_sl["algoId"]))
                                log(f"[dyn_sl] {sym} 取消旧止损 @{old_sl['triggerPrice']} 后重挂")
                    except Exception as _rest_e:
                        log(f"[dyn_sl] {sym} REST 验证失败:{_rest_e}")

                    safe_dist = abs(mp - new_sl) / mp > _SL_SAFETY_BUFFER
                    if safe_dist:
                        r = api.place_algo_order(sym, close_side, "STOP_MARKET",
                                                 new_sl, pos_side, qty)
                        if isinstance(r, dict) and r.get("algoId"):
                            api.algo_cache_save(sym, "STOP_MARKET",
                                                int(r["algoId"]), str(new_sl))
                            changes.append(f"SL补挂@{new_sl:.6g}")
                            log(f"[dyn_sl] {sym} SL补挂 @{new_sl:.6g}")

                # ── 更新 TP2(主力止盈,TP1 一般较快成交不频繁调整)────────
                if new_tp2 and cur_tp2:
                    move_pct = abs(new_tp2 - cur_tp2) / cur_tp2
                    # TP2 只向更高盈利目标移动
                    direction_ok = (side == "LONG" and new_tp2 > cur_tp2) or \
                                   (side == "SHORT" and new_tp2 < cur_tp2)
                    if direction_ok and move_pct >= _TP_MIN_MOVE_PCT:
                        # 撤旧 TP2 限价单
                        old_tp2_order = next((o for o in tp_orders[1:2]), None)
                        if old_tp2_order:
                            api.cancel_order(sym, int(old_tp2_order["orderId"]))
                        from entry_logic import fmt_price as _fp3
                        _tk3 = 0.00001 if new_tp2 < 1 else (0.0001 if new_tp2 < 10 else (0.001 if new_tp2 < 100 else 0.01))
                        r = api.place_order(sym, close_side, "LIMIT",
                                            quantity=str(qty), price=_fp3(new_tp2, _tk3),
                                            positionSide=pos_side, timeInForce="GTC")
                        if isinstance(r, dict) and r.get("orderId"):
                            changes.append(f"TP2 {cur_tp2:.6g}→{new_tp2:.6g}")
                            log(f"[dyn_tp] {sym} TP2限价单更新 {cur_tp2:.6g}→{new_tp2:.6g}")

                # ── 提前止盈:评分下滑 or 热力图反转 ──────────────────────────────
                # 只在有浮盈的情况下触发,避免亏损状态下的错误平仓
                ep_price = float(p.get("entryPrice", mp))
                upnl     = float(p.get("unRealizedProfit", 0))
                pct_profit = (mp - ep_price) / ep_price * (1 if side == "LONG" else -1)

                if upnl > 0 and pct_profit > 0.005:   # 有效浮盈 > 0.5% 才考虑
                    try:
                        full_sig   = _cg_dyn.get_signal_for_open(sym, side, mp)
                        score_now  = full_sig.get("score_adjust", 0)
                        bias_now   = full_sig.get("bias", "neutral")
                        # 从 pos_state 拿开仓时评分(如无则跳过)
                        _pstate = {}
                        if os.path.exists(f"{WORKSPACE}/pos_state.json"):
                            import json as _js2
                            _pstate = _js2.load(open(f"{WORKSPACE}/pos_state.json"))
                        entry_score = _pstate.get(sym, {}).get("entry_score", None)

                        early_exit = False
                        early_reason = ""

                        # 条件1:评分从开仓时高分跌到低分
                        if entry_score is not None and entry_score >= 5:
                            score_drop = entry_score - score_now
                            if score_drop >= 15 and score_now <= -10:
                                early_exit = True
                                early_reason = f"评分大幅下滑 开仓{entry_score:+d}→现在{score_now:+d}"

                        # 条件2:热力图方向反转(对持仓不利)
                        bias_hostile = (
                            (side == "LONG"  and bias_now == "short") or
                            (side == "SHORT" and bias_now == "long")
                        )
                        if bias_hostile and score_now <= -5:
                            early_exit = True
                            early_reason = (early_reason + " | " if early_reason else "") + \
                                           f"热力图反转({bias_now})且评分{score_now:+d}"

                        if early_exit:
                            log(f"[early_tp] {sym} 触发提前止盈: {early_reason} 浮盈={upnl:+.2f}U({pct_profit:+.2%})")
                            # 撤掉现有 TP Algo 单,改为立即市价平仓
                            for _a in sym_algos:
                                if _a.get("type") == "LIMIT" and _a.get("reduceOnly"):
                                    try:
                                        api.cancel_algo_order(sym, int(_a["algoId"]))
                                        api.algo_cache_remove_order(sym, int(_a["algoId"]))
                                    except Exception:
                                        pass
                            # 市价平仓
                            try:
                                close_result = api.request(
                                    "POST",
                                    f"{api.BASE_FAPI}/fapi/v1/order",
                                    {
                                        "symbol":       sym,
                                        "side":         close_side,
                                        "type":         "MARKET",
                                        "quantity":     str(qty),
                                        "reduceOnly":   "true",
                                        "positionSide": pos_side,
                                    },
                                    signed=True
                                )
                                order_ok = isinstance(close_result, dict) and "orderId" in close_result
                            except Exception as _ce:
                                close_result = None; order_ok = False
                                log(f"[early_tp_order_err] {sym}: {_ce}")
                            if order_ok:
                                notify(
                                    f"💰 [{sym}] 提前止盈已执行 +{upnl:.2f}U ({pct_profit:+.2%})\n"
                                    f"原因: {early_reason}"
                                )
                                changes.append(f"提前止盈+{upnl:.1f}U")
                            else:
                                notify(f"⚠️ [{sym}] 提前止盈下单失败,请手动检查!")
                    except Exception as _ep_e:
                        log(f"[early_tp_err] {sym}: {_ep_e}")

                if changes:
                    notify(f"🔄 [{sym}] 动态调整: {' | '.join(changes)}")

            except Exception as _e:
                log(f"[dyn_sltp_err] {sym}: {_e}")

    except Exception as e:
        log(f"[dyn_sltp_outer_err] {e}")


def scanner_loop():
    """
    内嵌扫描线程:每15分钟触发一次(:00/:15/:30/:45)。
    不走 cron + LLM,不消耗积分,每次扫描约 30s。
    """
    import importlib
    from datetime import datetime, timezone
    # 第一次启动延迟 2 分钟(等 ws 和门控初始化完)
    _stop_event.wait(120)
    while not _stop_event.is_set():
        now = datetime.now(timezone.utc)
        minute = now.minute
        second = now.second
        # 等到下一个 :00/:30(每30分钟,与cron :10/:40 错开,避免重复扫描)
        slot = (minute // 30 + 1) * 30
        if slot >= 60:
            wait_sec = (60 - minute) * 60 - second
        else:
            wait_sec = (slot - minute) * 60 - second
        log(f"[scanner_loop] 下次扫描在 {wait_sec//60}分{wait_sec%60}秒后(每30分钟 :00/:30)")
        _stop_event.wait(max(wait_sec, 1))
        if _stop_event.is_set():
            break
        try:
            # ── 使用新版多空扫描替代旧版 scanner.py ──────────────────────────
            # 旧 scanner.py 使用独立简单评分体系,与 entry_logic.py 四向评分脱节
            # 改为直接调用 short_scanner + auto_open,评分体系统一
            import subprocess as _subp
            _py = sys.executable
            _ws = WORKSPACE

            _r_short = _subp.run(
                [_py, f"{_ws}/scripts/short_scanner.py"],
                capture_output=True, text=True, timeout=120, cwd=_ws
            )
            if _r_short.returncode != 0:
                log(f"[scanner_loop] short_scanner 异常: {_r_short.stderr[-300:]}")
            else:
                log(f"[scanner_loop] short_scanner OK ({len(_r_short.stdout)}chars)")

            _r_long = _subp.run(
                [_py, f"{_ws}/scripts/auto_open.py"],
                capture_output=True, text=True, timeout=120, cwd=_ws
            )
            if _r_long.returncode != 0:
                log(f"[scanner_loop] auto_open 异常: {_r_long.stderr[-300:]}")
            else:
                log(f"[scanner_loop] auto_open OK ({len(_r_long.stdout)}chars)")

            # ── 动态 SL/TP 更新(每次扫描后跟着跟)─────────────────────────
            try:
                _dynamic_update_sl_tp()
            except Exception as _dyn_e:
                log(f"[dyn_sltp_call_err] {_dyn_e}")

            # ── 双网保护期已小执行:每次扫描后对活跃双网评分复查 ────────
            try:
                import coinglass_api as _cg_check
                import requests as _req_check
                _btc_price_now = float(_req_check.get(
                    "https://fapi.binance.com/fapi/v1/ticker/price?symbol=BTCUSDT",
                    proxies=api.PROXIES, timeout=5).json()["price"])
                _btc_price_15m = _ticker_cache.get("BTCUSDT_15m", _btc_price_now)
                _btc_chg_15m = (_btc_price_now - _btc_price_15m) / _btc_price_15m if _btc_price_15m else 0
                # 每次更新 BTC 15m 基准价
                _ticker_cache["BTCUSDT_15m"] = _btc_price_now

                for _dn_sym in _dn.get_active_syms():
                    try:
                        _dn_k = api.get_klines(_dn_sym, "1h", 22)  # 取22根,前20根算均量,最新1根算当前量
                        _dn_closes = [float(k[4]) for k in _dn_k]
                        _dn_vols   = [float(k[5]) for k in _dn_k]
                        # 计算 RSI14
                        _gains  = [max(_dn_closes[i]-_dn_closes[i-1], 0) for i in range(1, len(_dn_closes))]
                        _losses = [max(_dn_closes[i-1]-_dn_closes[i], 0) for i in range(1, len(_dn_closes))]
                        _ag = sum(_gains[-14:]) / 14; _al = sum(_losses[-14:]) / 14
                        _rsi = 100 - (100 / (1 + _ag / _al)) if _al > 0 else 50
                        _rsi_cache[_dn_sym] = _rsi
                        # BUG-4 修复:精确量比 = 最新根量 / 近20根均量(不含最新根)
                        _avg_vol_20 = sum(_dn_vols[-21:-1]) / 20 if len(_dn_vols) >= 21 else (sum(_dn_vols[:-1]) / max(len(_dn_vols)-1, 1))
                        _dn_vol_avg[_dn_sym]       = _avg_vol_20       # 20根均量
                        _dn_kline_vol_cur[_dn_sym] = _dn_vols[-1]      # 最新1H K线量

                        _dn_score_info = _dn.load_pending().get(_dn_sym, {})
                        _dn_new_score = _dn_score_info.get("score", 80)  # 简化:尚无重新评分,用已存score
                        _dn_fr = api.get_mark_price(_dn_sym).get("lastFundingRate", 0)
                        _dn.check_protection(
                            sym=_dn_sym, new_score=float(_dn_new_score),
                            btc_drop_15m=_btc_chg_15m,
                            funding_rate=float(_dn_fr),
                            has_reversal=False, support_broken=False,
                            new_support=0.0, api=api, notify_fn=notify,
                        )
                    except Exception as _dn_e:
                        log(f"[dn_protect_err] {_dn_sym}: {_dn_e}")
                # 双网挂单到期已小执行
                _dn.patrol_pending(api, notify)
            except Exception as _dn_patrol_e:
                log(f"[dn_patrol_err] {_dn_patrol_e}")
        except Exception as e:
            log(f"[scanner_loop_err] {e}")


def main():
    # BUG-C10 修复:PID 文件锁,防止 ws-monitor-guard 误触发时两个进程并发(双挂单/重复平仓)
    import fcntl
    _pid_lock_fd = open("/tmp/ws_monitor.lock", "w")
    try:
        fcntl.flock(_pid_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        log("[main] 另一个 ws_monitor 实例已在运行,退出")
        import sys; sys.exit(0)

    log("=" * 60)
    log("[main] ws_monitor_new 启动")

    # ── 统一重启恢复流程(state_manager 8步)─────────────────────────────
    import state_manager as _sm
    _sm.recover_on_boot(api=api, notify_fn=notify, log_fn=log)

    # 加载熔断状态(兼容旧接口)
    circuit = load_circuit()
    log(f"[main] 熔断状态: open={circuit.get('open')} daily_loss={circuit.get('daily_loss')}U")

    # 从 pos_state 恢复 gates(内存结构)
    _load_wl_fired()
    restore_gates_from_pos_state()

    # 启动自动持久化(每5分钟)
    _sm.start_auto_persist(
        get_watchlist_fn=lambda: None,
        get_pending_fn=lambda: None,
        interval=300
    )

    # 启动 user-data WebSocket
    start_user_data_stream()

    # 启动 ticker WebSocket
    start_streams()

    # 启动 sl_tp_patrol_loop 线程
    patrol_thread = threading.Thread(target=sl_tp_patrol_loop, daemon=True)
    patrol_thread.start()
    log(f"[main] patrol loop started (interval={PATROL_INTERVAL//60}min)")

    # 启动内嵌扫描线程(替代 cron scanner-30m)
    scanner_thread = threading.Thread(target=scanner_loop, daemon=True)
    scanner_thread.start()
    log("[main] scanner loop started (internal, no LLM, 15min interval)")

    log("[main] ws_monitor started (binance-cli WS + internal scanner)")

    # 主线程等待
    try:
        while not _stop_event.is_set():
            time.sleep(60)
    except KeyboardInterrupt:
        log("[main] 收到退出信号")
        _stop_event.set()
        log("[main] ws_monitor stopped")


if __name__ == "__main__":
    main()
