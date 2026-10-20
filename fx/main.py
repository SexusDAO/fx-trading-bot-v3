"""
fx/main.py — 异步入口（V3.0）

职责：
  1. 启动 WsOrderListener（userData + markPrice）
  2. 初始化 PositionManager，接入 ws_listener
  3. 定时调度：
       T1: 每 5 分钟   → scanner.run_scan()
       T2: 每 5 分钟   → position_manager.patrol_once()（与 T1 错开 30 秒）
       T3: 每 30 分钟  → scanner.refresh_pool()
       T4: 每天 UTC 00:00 → risk_manager.reset_circuit()
  4. 优雅退出：捕获 SIGINT / SIGTERM，停止所有任务再退出
"""

import asyncio
import logging
import os
import signal
import sys
import shutil
import subprocess
from pathlib import Path

# ── 启动前依赖检查：只检查，不在运行时安装 ───────────────────────────────────
def _ensure_deps():
    """
    [v3.89 #24] 只做依赖健康检查：缺少关键依赖时 logger.critical 并退出，不在启动阶段修改运行环境。
    原写法在模块加载时运行未固定版本的 pip install（无超时），重启恢复时间取决于外网，
    且同一提交可能得到不同依赖。依赖应在构建/部署阶段安装（requests[socks]、PySocks）。
    """
    missing = []
    try:
        import socks  # noqa: F401
    except ImportError:
        missing.append("PySocks（pip install 'requests[socks]' PySocks）")
    if missing:
        msg = "[startup] 缺少必需依赖: " + "; ".join(missing) + "。请在部署阶段安装后重新启动，系统不会在启动时自动安装。"
        logging.getLogger("fx.startup").critical(msg)
        print(msg, file=sys.stderr, flush=True)
        sys.exit(1)

_ensure_deps()
from datetime import datetime, timezone

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))

# 启动时自动清理 __pycache__，防止旧的 .pyc 导致修复不生效
for _d in ROOT.rglob("__pycache__"):
    shutil.rmtree(_d, ignore_errors=True)

LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# ── 自写 main.pid，供 daemon_start.py 精确管理 ──────────────────────────────
_MAIN_PID_FILE = LOG_DIR / "main.pid"
try:
    _MAIN_PID_FILE.write_text(str(os.getpid()))
except Exception:
    pass

import atexit
@atexit.register
def _cleanup_main_pid():
    try:
        _MAIN_PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass

# v3.32: FileHandler → RotatingFileHandler，防止 system.log 无限增长
# 10MB × 5 份 = 最多保留 50MB 历史日志，长期 24/7 运转安全
from logging.handlers import RotatingFileHandler as _RotatingFileHandler
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=[
        _RotatingFileHandler(
            str(LOG_DIR / "system.log"),
            maxBytes=10 * 1024 * 1024,  # 10MB
            backupCount=5,
            encoding="utf-8",
        ),
        # v3.14: 去掉 StreamHandler，daemon 已将 stdout 重定向到 system.log，
        # 保留 StreamHandler 会导致每条日志重复写两次
    ],
    force=True,  # v3.14: 强制覆盖已有 handler，防止如果子模块提前初始化了 root logger
)
logger = logging.getLogger("fx.main")
logging.getLogger("websockets").setLevel(logging.WARNING)  # 屏蔽 ws 心跳 DEBUG 日志
logging.getLogger("urllib3").setLevel(logging.WARNING)


# ───────────────────────────────────────────────────────────────────────────────
# v3.33 共享线程池：扫描线程不再每轮新建，避免卡住时线程多次累积
import concurrent.futures as _cfu
_SCAN_POOL = _cfu.ThreadPoolExecutor(max_workers=1, thread_name_prefix="scan-worker")

# ───────────────────────────────────────────────────────────────────────────────
# 定时任务（异步版）
# ───────────────────────────────────────────────────────────────────────────────

async def _loop_scan(interval: int = 300, signal_watcher=None) -> None:
    """每 interval 秒执行一次扫描，扫到信号后注册到 signal_watcher（若提供）。
    v3.42: 周末自动降频至 900s（15 分钟），工作日维持 interval（默认 300s）。
    周末价格几乎不动，5 分钟和 15 分钟的信号捕捉能力完全相同，但资源消耗减少 2/3。
    """
    await asyncio.sleep(5)
    # ── 6小时心跳计数器 ────────────────────────────────────────────────
    import time as _hb_time, datetime as _hb_dt
    _hb_interval   = 6 * 3600          # 6 小时一次
    _hb_last_ts    = _hb_time.time()   # 启动时对齐，第一次心跳在启动 6h 后
    _hb_scans      = 0    # 计数：扫描轮次
    _hb_raw        = 0    # 计数：原始信号
    _hb_watched    = 0    # 计数：进入监控
    _hb_placed     = 0    # 计数：成交开仓
    while True:
        try:
            from scanner import run_scan
            # ── 心跳写入（供 health_check 活跃度检查）───────────────────────
            try:
                import json as _hb_j, time as _hb_t, pathlib as _hb_p
                _hb_f = _hb_p.Path(__file__).parent.parent / "ws_cache" / "fx_main_heartbeat.json"
                _hb_f.write_text(_hb_j.dumps({"ts": _hb_t.time()}))
            except Exception:
                pass
            loop = asyncio.get_event_loop()
            # v3.33 复用共享线程池（替代每轮 with ThreadPoolExecutor）
            import functools
            _scan_fn = functools.partial(run_scan, signal_watcher=signal_watcher)
            future = loop.run_in_executor(_SCAN_POOL, _scan_fn)
            try:
                result = await asyncio.wait_for(future, timeout=300)
                placed = (result or {}).get("placed", 0)
                # ── 心跳计数器更新 ────────────────────────────────────────────
                _r = result or {}
                _hb_scans   += 1
                _hb_raw     += _r.get("signals_raw", 0)
                _hb_placed  += placed
                # 计入监控的信号数（signals_final 是进入 watcher 的）
                _hb_watched += _r.get("signals_final", 0)
                if placed:
                    logger.info(f"[main] 扫描完成，注册信号/开仓 {placed} 笔")
                else:
                    logger.debug("[main] 扫描完成，无信号")
                # 打印 signal_watcher 当前监控状态
                if signal_watcher is not None:
                    pending = signal_watcher.pending_count()
                    if pending:
                        logger.info(f"[main] SignalWatcher 监控中: {pending} 个信号 {signal_watcher.pending_symbols()}")
            except asyncio.TimeoutError:
                logger.error("[main] task_scan 超时（300s），跳过本轮")
            except Exception as e:
                logger.error(f"[main] task_scan 子线程异常: {e}", exc_info=True)
        except Exception as e:
            logger.error(f"[main] task_scan 外层异常: {e}", exc_info=True)
        # v3.42: 周末降频 300s → 900s，工作日保持原 interval
        from scanner import _is_weekend_period as _iwp
        from datetime import datetime, timezone as _tz
        _eff_interval = 900 if _iwp(datetime.now(_tz.utc)) else interval
        if _eff_interval != interval:
            logger.debug(f"[main] 周末模式：扫描间隔降频 {interval}s → {_eff_interval}s")
        # ── 6小时心跳推送 ────────────────────────────────────────────
        _now_ts = _hb_time.time()
        if _now_ts - _hb_last_ts >= _hb_interval:
            try:
                from shared.notifier import notify_async as _nb_ntf
                import shared.ws_listener as _ws_mod
                _ws_ref   = getattr(_ws_mod, "_ws_instance", None)
                _ws_ok    = "✅ 全部连接"
                if _ws_ref is not None:
                    _disconn = getattr(_ws_ref, "disconnected_streams", None)
                    if callable(_disconn):
                        _bad = _disconn()
                        _ws_ok = f"❌ {','.join(_bad)}断开" if _bad else "✅ 全部连接"
                _pending_n = signal_watcher.pending_count() if signal_watcher else 0
                _hb_utc    = _hb_dt.datetime.now(_hb_dt.timezone.utc).strftime("%H:%M UTC")
                await _nb_ntf(
                    f"💓 系统心跳 · {_hb_utc}\n"
                    f"扫描正常 | {_pending_n}个标的 | 每5分钟一轮\n"
                    f"过去6小时：原始信号{_hb_raw}个 | 进入监控{_hb_watched}个 | 成交{_hb_placed}笔\n"
                    f"WS：{_ws_ok}"
                )
            except Exception as _hb_e:
                logger.debug(f"[main] 心跳推送失败: {_hb_e}")
            # 重置计数器
            _hb_last_ts = _now_ts
            _hb_scans = _hb_raw = _hb_watched = _hb_placed = 0
        await asyncio.sleep(_eff_interval)


async def _loop_patrol(interval: int = 300, offset: int = 30) -> None:
    """每 interval 秒执行一次巡检，与 scan 错开 offset 秒。"""
    await asyncio.sleep(offset)
    while True:
        try:
            from position_manager import patrol_once as _patrol_once
            await _patrol_once()
        except Exception as e:
            logger.error(f"[main] task_patrol 异常: {e}", exc_info=True)
        await asyncio.sleep(interval)


async def _loop_refresh_pool(interval: int = 1800) -> None:
    """每 interval 秒刷新一次候选池。
    v3.42: 工作日缩短至 900s（15 分钟），更快捕捉新进涨跌幅榜的标的。
    周末保持 1800s（30 分钟），候选池变化慢，高频刷新无意义。
    """
    while True:
        try:
            from scanner import refresh_pool
            await asyncio.get_event_loop().run_in_executor(None, refresh_pool)
            logger.info("[main] 候选池已刷新")
        except Exception as e:
            logger.error(f"[main] task_refresh_pool 异常: {e}", exc_info=True)
        # v3.42: 工作日候选池刷新缩短到 15 分钟，周末保持 30 分钟
        from scanner import _is_weekend_period as _iwp
        from datetime import datetime, timezone as _tz
        _eff_pool_interval = interval if _iwp(datetime.now(_tz.utc)) else 900
        if _eff_pool_interval != interval:
            logger.debug(f"[main] 工作日模式：候选池刷新间隔 {interval}s → {_eff_pool_interval}s")
        await asyncio.sleep(_eff_pool_interval)


async def _loop_funding_arb(interval_sec: int = 1800, signal_watcher=None) -> None:
    """每30分钟运行一次 funding_arb 独立扫描"""
    import asyncio as _aio
    await _aio.sleep(120)  # 启动偏移：错开主扫描
    while True:
        try:
            from scanner import run_funding_arb_scan
            loop = _aio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: run_funding_arb_scan(dry_run=False, signal_watcher=signal_watcher)
            )
        except Exception as _e:
            logger.error(f"[main] funding_arb_scan 异常: {_e}", exc_info=True)
        await _aio.sleep(interval_sec)


async def _loop_reset_circuit() -> None:
    """每天 UTC 00:00 重置熔断器。"""
    while True:
        now = datetime.now(timezone.utc)
        # 计算到下一个 00:00 的秒数
        seconds_until_midnight = (
            (23 - now.hour) * 3600
            + (59 - now.minute) * 60
            + (60 - now.second)
        )
        await asyncio.sleep(seconds_until_midnight)
        try:
            from shared.risk_manager import reset_circuit
            reset_circuit()
            logger.info("[main] 熔断器日重置完成")
        except Exception as e:
            logger.error(f"[main] task_reset_circuit 异常: {e}", exc_info=True)
        await asyncio.sleep(60)  # 防止同秒多次触发


async def _loop_track_outcomes(interval_sec: int = 3600) -> None:
    """每小时运行一次事后追踪，对已通过 judge 的信号填充 outcome。"""
    await asyncio.sleep(600)  # 启动偏移 10 分钟，错开主扫描
    while True:
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: __import__('tools.track_outcomes', fromlist=['run']).run()
            )
        except Exception as _e:
            logger.warning(f"[main] track_outcomes 异常（非致命）: {_e}")
        await asyncio.sleep(interval_sec)


# ═══════════════════════════════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════════════════════════════

async def _sync_positions_on_startup() -> None:
    """
    启动时对齐持仓状态：
    1. 拉取交易所真实持仓和开放挂单
    2. 清除 pos_state 中已无持仓的僵尸记录
    3. 检查有持仓但缺 tp1/tp2 挂单的仓位，自动补挂
    4. 推送对齐摘要到 Jarvis
    """
    import math
    import time as _time

    try:
        sys.path.insert(0, str(ROOT))
        sys.path.insert(0, str(ROOT / "shared"))
        from shared import binance_api as _bapi
        from shared.state import get_all_positions, update_position, remove_position, cleanup_orphan_patching
        from executor import _place_guards_async, _get_precision
    except Exception as e:
        logger.error(f"[startup_sync] 模块导入失败: {e}")
        return

    # ── 圆山 孤儿 patching 清理（问题34）——进程崩溃/网络异常可能导致 patching 永远不恢复，
    # 造成该标的无法再开仓。开机时一次性强制清理。
    try:
        orphans = cleanup_orphan_patching()
        if orphans:
            logger.warning(f"[startup_sync] 广清孤儿 patching: {orphans}")
            try:
                from shared.notifier import notify_async
                await notify_async(
                    f"⚠️ 开机自检：广清孤儿 patching 记录\n"
                    f"局标的: {', '.join(orphans)}\n"
                    f"已删除，该标的下轮扫描可正常开仓"
                )
            except Exception:
                pass
    except Exception as _op_e:
        logger.error(f"[startup_sync] orphan patching 清理失败（非致命）: {_op_e}")

    try:
        # ── 拉取真实持仓 ──────────────────────────────────────────────────────
        pos_risk = _bapi.get_position()  # 走 binance_api 封装：_retry + Session + key 轮询
        # HEDGE 模式：多单 positionSide=LONG，空单 positionSide=SHORT，两者都要对齐
        # 用 (symbol, positionSide) 作为 key，避免同一标的多空各一条记录时互相覆盖
        real = {(p["symbol"], p["positionSide"]): p for p in pos_risk
                if p.get("positionSide") in ("LONG", "SHORT")
                and abs(float(p.get("positionAmt", 0))) > 0}

        # ── 拉取开放挂单（普通限价单：TP）──────────────────────────────────────
        open_orders = _bapi.get_open_orders()  # 走 binance_api 封装
        live_ids = {int(o["orderId"]) for o in open_orders}
        order_by_sym = {}
        for o in open_orders:
            order_by_sym.setdefault(o["symbol"], []).append(o)

        # ── v3.56: 拉取 Algo 止损单（SL），检查 SL 是否仍在交易所 ──────────────
        try:
            _algo_orders = _bapi.get_algo_open_orders() or []
        except Exception as _ae:
            logger.warning(f"[startup_sync] 拉取 algo 止损单失败: {_ae}")
            _algo_orders = []
        live_algo_ids = {int(o.get("algoId", 0)) for o in _algo_orders if o.get("algoId")}

        positions = get_all_positions()
        ps_map = {p["symbol"]: p for p in positions}

        removed,補挂, errors = [], [], []

        # ── 1. 清僵尸记录 ─────────────────────────────────────────────────────
        for sym, ps in ps_map.items():
            if ps.get("status") != "open":
                continue
            ps_side = ps.get("direction", "long").upper()  # pos_state 里存的是 direction
            real_key = (sym, ps_side)
            if real_key not in real:
                # 交易所已无此方向持仓，用 state.py 的加锁原子接口清除
                pid = ps.get("pos_id", sym)
                remove_position(pid)
                removed.append(sym)
                logger.info(f"[startup_sync] 清除僵尸记录: {sym} ({ps_side})")

        # ── 2. 检查有持仓但缺 TP 挂单的仓位，自动补挂 ───────────────────────
        for (sym, pos_side), rp in real.items():
            entry = float(rp["entryPrice"])
            qty   = abs(float(rp["positionAmt"]))
            ps    = ps_map.get(sym, {})
            pid   = ps.get("pos_id", sym)
            tp1_id = ps.get("tp1_order_id")
            tp2_id = ps.get("tp2_order_id")

            # 单TP模式：tp2_price=None 表示本仓位不需要 TP2
            single_tp_mode = (ps.get("tp2_price") is None)

            tp1_alive = bool(tp1_id and int(tp1_id) in live_ids)
            tp2_alive = bool(tp2_id and int(tp2_id) in live_ids) if not single_tp_mode else True

            # v3.56: 检查 SL 是否仍在交易所（重启期间可能被交易所自动撤销）
            sl_id_ps  = ps.get("sl_order_id")
            sl_alive  = bool(sl_id_ps and int(sl_id_ps) in live_algo_ids)
            if sl_id_ps and not sl_alive:
                logger.warning(f"[startup_sync] {sym}: SL单 {sl_id_ps} 不在交易所，需补挂")
                try:
                    from executor import _get_precision
                    from shared.calc_sl_tp import calc_sl_tp as _csltp
                    _direction_sl = ps.get("direction", "LONG").upper()
                    _sl_price_sv  = float(ps.get("sl_price", 0))
                    _qty_sv       = abs(float(rp.get("positionAmt", 0)))
                    _prec_sv      = _get_precision(sym)
                    if _sl_price_sv > 0 and _qty_sv > 0:
                        _resp_sv = await _place_guards_async.__wrapped__(
                            sym, _direction_sl, _qty_sv,
                            _sl_price_sv, ps.get("tp1_price", 0), ps.get("tp2_price"), _prec_sv
                        ) if False else None  # 只补SL，不重复挂TP
                        # 直接调 api.place_sl
                        from shared import binance_api as _bapi2
                        _sl_resp = await __import__('asyncio').get_event_loop().run_in_executor(
                            None, lambda: _bapi2.place_sl(
                                symbol=sym,
                                side="SELL" if _direction_sl=="LONG" else "BUY",
                                position_side=_direction_sl,
                                trigger_price=round(_sl_price_sv / _prec_sv["tick"]) * _prec_sv["tick"],
                                quantity=round(_qty_sv / _prec_sv["step"]) * _prec_sv["step"],
                            )
                        )
                        _new_sl_id_sv = _sl_resp.get("orderId") or _sl_resp.get("algoId")
                        if _new_sl_id_sv:
                            from shared.state import update_position as _upd
                            _upd(ps.get("pos_id", sym), {"sl_order_id": int(_new_sl_id_sv)})
                            logger.info(f"[startup_sync] {sym}: SL补挂成功 id={_new_sl_id_sv}")
                            補挂.append(f"{sym} sl={_new_sl_id_sv}")
                        else:
                            errors.append(f"{sym} SL补挂失败: {_sl_resp}")
                except Exception as _sv_e:
                    errors.append(f"{sym} SL补挂异常: {_sv_e}")
                    logger.error(f"[startup_sync] {sym} SL补挂失败: {_sv_e}")

            if tp1_alive and tp2_alive and sl_alive:
                logger.info(f"[startup_sync] {sym}: SL✅ TP1✅ {'(单TP)' if single_tp_mode else 'TP2✅'} 无需补挂")
                continue

            # TP1 缺失才需补挂
            if tp1_alive and single_tp_mode and sl_alive:
                logger.info(f"[startup_sync] {sym}: SL✅ TP1✅ 单TP模式 无需补挂")
                continue

            logger.info(f"[startup_sync] {sym}: tp1_alive={tp1_alive} tp2_alive={tp2_alive} single_tp={single_tp_mode}，开始补挂")
            try:
                klines = _bapi.get_klines(sym, "1h", 22)
                now_ms = _time.time() * 1000
                closed = [k for k in klines if float(k[6]) < now_ms]
                trs = [max(float(closed[i][2]) - float(closed[i][3]),
                           abs(float(closed[i][2]) - float(closed[i-1][4])),
                           abs(float(closed[i][3]) - float(closed[i-1][4])))
                       for i in range(1, len(closed))]
                atr = sum(trs[-14:]) / 14 if trs else entry * 0.01
                tier = ps.get("volatility_tier", "mid")
                from config import SL_MULT_LOW, SL_MULT_MID, SL_MULT_HIGH
                sl_mult = {"low": SL_MULT_LOW, "mid": SL_MULT_MID, "high": SL_MULT_HIGH}.get(tier, SL_MULT_MID)

                direction_ps = ps.get("direction", "long").upper()
                sl_price  = ps.get("sl_price") or (
                    entry - atr * sl_mult if direction_ps == "LONG" else entry + atr * sl_mult)
                tp1_price = ps.get("tp1_price") or (
                    entry + atr * 1.5 if direction_ps == "LONG" else entry - atr * 1.5)
                # tp2_price 严格从 pos_state 取，None 表示单TP模式
                tp2_price = ps.get("tp2_price")  # None → 不挂 TP2

                precision = _get_precision(sym)
                guard = await _place_guards_async(
                    sym, direction_ps, qty, sl_price, tp1_price, tp2_price, precision
                )

                update_position(pid, {
                    "status": "open",
                    "fill_price": entry,
                    "quantity": qty,
                    "sl_price": sl_price,
                    "tp1_price": tp1_price,
                    "tp2_price": tp2_price,
                    "tp1_order_id": guard.get("tp1_id"),
                    "tp2_order_id": guard.get("tp2_id"),
                })
                補挂.append(f"{sym} tp1={guard.get('tp1_id')}")
                logger.info(f"[startup_sync] {sym} 补挂完成: {guard}")
            except Exception as e:
                errors.append(f"{sym}: {e}")
                logger.error(f"[startup_sync] {sym} 补挂失败: {e}")

        # ── 3. 推送摘要 ───────────────────────────────────────────────────────
        lines = [f"🔄 重启持仓对齐完成"]
        lines.append(f"  真实持仓: {len(real)} 笔 | pos_state: {len(positions)} 条")
        for (sym, pos_side), rp in real.items():
            entry = float(rp["entryPrice"])
            qty   = abs(float(rp["positionAmt"]))
            pnl   = float(rp["unRealizedProfit"])
            ps    = ps_map.get(sym, {})
            tp1_id = ps.get("tp1_order_id")
            tp2_id = ps.get("tp2_order_id")
            sl_p   = ps.get("sl_price")
            sl_str = f"{float(sl_p):.6g}" if sl_p is not None else "?"
            t1_ok  = "✅" if (tp1_id and int(tp1_id) in live_ids) else "❌"
            t2_ok  = "✅" if (tp2_id and int(tp2_id) in live_ids) else "❌"
            lines.append(f"  {sym}({pos_side}): entry={entry:.6g} qty={qty} PnL={pnl:+.2f}U")
            lines.append(f"    SL={sl_str}(patrol) TP1{t1_ok} TP2{t2_ok}")
        if removed:
            lines.append(f"  🗑 清除僵尸: {', '.join(removed)}")
        if 補挂:
            lines.append(f"  🔧 补挂TP: {', '.join(補挂)}")
        if errors:
            lines.append(f"  ⚠️ 补挂失败: {', '.join(errors)}")

        msg = "\n".join(lines)
        logger.info(f"[startup_sync] 摘要: {msg}")

        # 只在补挂失败（errors）时才推送，正常对齐和补挂全部静默
        if not errors:
            return
        try:
            from shared.notifier import notify_async
            err_msg = "⚠️ 重启对齐异常\n" + "\n".join(f"  {e}" for e in errors)
            await notify_async(err_msg)
        except Exception as e:
            logger.warning(f"[startup_sync] Jarvis 推送失败: {e}")

    except Exception as e:
        logger.error(f"[startup_sync] 对齐失败: {e}", exc_info=True)


def _handle_task_exception(loop, context):
    """
    事件循环未捕获异常处理器。
    [v3.89 #106] 原写法 context.get("exception", context["message"]) 会提前求值 context["message"]，
    自定义上下文缺少 message 时处理器自己抛 KeyError，掩盖原始故障。现安全读取两个可选字段并保留兜底。
    """
    try:
        exc = context.get("exception")
        msg = context.get("message")
        detail = exc if exc is not None else (msg if msg is not None else f"<无 exception/message，上下文键: {sorted(context)}>")
        logger.error(f"[main] 未捕获的协程异常: {detail}", exc_info=exc if isinstance(exc, BaseException) else None)
    except Exception as _he:
        logger.error(f"[main] 异常处理器自身出错: {_he!r}")


async def _shutdown_components(steps, total_timeout: float = 30.0, per_timeout: float = 10.0) -> dict:
    """
    [v3.89 #105] 依次停止各组件：每项独立 try/except，单项最多 per_timeout 秒，整体最多 total_timeout 秒。
    steps: [(name, callable)]，callable 可返回协程或普通值。
    返回 {name: "ok" | "timeout" | "skipped(deadline)" | "error: ..."}。
    """
    import inspect
    results: dict = {}
    t0 = asyncio.get_event_loop().time()
    for name, fn in steps:
        remaining = total_timeout - (asyncio.get_event_loop().time() - t0)
        if remaining <= 0:
            results[name] = "skipped(deadline)"
            logger.error(f"[main] 退出整体超时（{total_timeout:.0f}s），跳过 {name}")
            continue
        try:
            r = fn()
            if inspect.isawaitable(r):
                await asyncio.wait_for(r, timeout=min(per_timeout, remaining))
            results[name] = "ok"
        except asyncio.TimeoutError:
            results[name] = "timeout"
            logger.error(f"[main] 停止 {name} 超时")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            results[name] = f"error: {e!r}"
            logger.error(f"[main] 停止 {name} 异常: {e!r}", exc_info=True)
    return results


async def main() -> None:
    logger.info("[main] fx 交易系统 V3.0 启动")

    # ── 1. 初始化 WebSocket 监听器 ──────────────────────────────────────────
    from shared.ws_listener import WsOrderListener
    ws = WsOrderListener()
    await ws.start()

    # ── 1.5 启动时对齐持仓状态 ─────────────────────────────────────────────
    await _sync_positions_on_startup()

    # v3.33 注册 WS 重连回调：断线期间可能漏掉 TP/SL 成交事件，重连后主动对齐
    # [v3.89 #3] 持仓对齐加锁：断线期间的 REST 补偿任务与重连回调不会同时对齐
    _sync_lock = asyncio.Lock()

    async def _on_ws_reconnect():
        logger.info("[main] WS 重连，主动触发持仓对齐...")
        async with _sync_lock:   # 重连对齐必须执行：若补偿任务正在对齐则等它结束
            await _sync_positions_on_startup()
    ws.on_reconnect(_on_ws_reconnect)

    async def _on_ws_disconnect():
        # [v3.89 #3] userData 断线期间的有界 REST 补偿（最长 30s，每 10s 一轮）；已有对齐在跑则跳过本轮
        if _sync_lock.locked():
            return
        logger.warning("[main] WS 断线，REST 补偿对齐持仓...")
        async with _sync_lock:
            await _sync_positions_on_startup()
    ws.on_disconnect(_on_ws_disconnect)

    # ── 2. 初始化 PositionManager，注入 ws ────────────────────────────────
    from position_manager import PositionManager, _set_manager, _adjust_sl_tp_loop_static
    pm = PositionManager(ws_listener=ws)
    _set_manager(pm)
    await pm.start()

    # ── 3. 注册 executor 使用相同的 ws 实例 ───────────────────────────────
    #    executor 的 WsOrderListener 引用通过模块级 _LISTENER 变量注入
    try:
        import executor as _executor
        _executor._LISTENER = ws
        logger.info("[main] executor ws_listener 注入完成")
    except Exception as e:
        logger.warning(f"[main] executor ws_listener 注入失败（非致命）: {e}")

    # ── 3.5. 初始化 SignalWatcher（信号入场价格监控）─────────────────────────
    from shared.signal_watcher import SignalWatcher
    signal_watcher = SignalWatcher()
    signal_watcher._main_loop   = asyncio.get_event_loop()   # 主 loop，供子线程 run_coroutine_threadsafe 用
    signal_watcher._ws_listener = ws                          # 供 cancel_all 的 unsubscribe 用
    # 注册价格回调（WS 模式）同时也启动 REST 轮询（WS 不透传时作为保底）
    signal_watcher.register_to_ws(ws)
    await signal_watcher.start_gc()           # 后台 GC
    await signal_watcher.start_polling(1.5)   # REST 轮询（1.5s间隔，与 WS 互补）
    await signal_watcher.start_recheck(900)   # 每15min 复核 pending 挂单指标
    await signal_watcher.start_btc_guard(300) # 每5min 检查 BTC 大跌护卫
    logger.info("[main] SignalWatcher 初始化完成（WS + REST + recheck + btc_guard）")

    # ── 4. 启动定时任务 ────────────────────────────────────────────────────
    # ── 3.5 启动 liq_cache 后台刷新线程（每15分钟批量预热清算区缓存）──────────────
    try:
        from shared.liq_cache import start_bg_refresh
        import scanner as _scanner_mod
        # 从 candidate_pool.json 读取当前候选池列表
        import json as _json
        from pathlib import Path as _Path
        _pool_file = _Path(__file__).parent / "logs" / "candidate_pool.json"
        def _get_pool():
            try:
                return _json.loads(_pool_file.read_text()).get("pool", [])
            except Exception:
                return []
        start_bg_refresh(_get_pool)
        logger.info("[main] liq_cache 后台刷新线程已启动")
    except Exception as _lce:
        logger.warning(f"[main] liq_cache 启动失败（非致命）: {_lce}")

    tasks = [
        # ── 优先级 1：持仓维护（止损/止盈/巡检） ───────────────────────────────
        asyncio.create_task(_adjust_sl_tp_loop_static(),                       name="adjust_sl_tp"),
        asyncio.create_task(_loop_patrol(300, 30),                             name="patrol"),
        # ── 优先级 2：挂单维护（recheck/btc_guard 已在 signal_watcher 内部 task） ─
        # （signal_watcher.start_recheck / start_btc_guard 已在上方启动）
        # ── 优先级 3：市场信号扫描 ────────────────────────────────────────────────
        asyncio.create_task(_loop_scan(300, signal_watcher=signal_watcher),    name="scan"),
        asyncio.create_task(_loop_funding_arb(1800, signal_watcher=signal_watcher), name="funding_arb"),
        # ── 基础支撑 ─────────────────────────────────────────────────────────────
        asyncio.create_task(_loop_refresh_pool(1800),                          name="refresh_pool"),
        asyncio.create_task(_loop_reset_circuit(),                             name="reset_circuit"),
        asyncio.create_task(_loop_track_outcomes(3600),                        name="track_outcomes"),
    ]

    # ── 5. 优雅退出 ─────────────────────────────────────────────────────────
    stop_event = asyncio.Event()

    def _handle_signal():
        logger.info("[main] 收到退出信号，准备停止...")
        stop_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError:
            # Windows 不支持 add_signal_handler
            pass

    # 捕获所有未处理的协程异常，防止 loop 意外退出
    loop.set_exception_handler(_handle_task_exception)

    logger.info("[main] 所有任务已启动，等待退出信号...")
    # 监控所有 task，任意一个提前结束就记录
    async def _watchdog():
        while True:
            await asyncio.sleep(10)
            done = [t for t in tasks if t.done()]
            if done:
                for t in done:
                    exc = t.exception() if not t.cancelled() else None
                    logger.error(f"[main] WATCHDOG: task {t.get_name()} 意外结束 "
                                 f"cancelled={t.cancelled()} exc={exc}")
                stop_event.set()
                return
    watchdog_task = asyncio.create_task(_watchdog(), name="watchdog")

    try:
        await stop_event.wait()
        logger.info("[main] stop_event 已触发，开始停止流程")
    except asyncio.CancelledError:
        logger.info("[main] stop_event.wait() 被取消")
    finally:
        watchdog_task.cancel()

    # 停止
    # [v3.89 #105] 各组件独立 try/except + 单项 10 秒超时 + 整体 30 秒截止；汇总结果后再报告。
    #   原写法 gather/pm.stop/ws.stop 无保护，signal_watcher 四个 stop 共用一个 try，
    #   任一步异常或卡住都会跳过后续清理，却仍无条件打印「已安全停止」。
    for t in tasks:
        t.cancel()

    async def _stop_tasks():
        await asyncio.gather(*tasks, return_exceptions=True)

    def _report_orphans():
        pend = getattr(signal_watcher, "_orphan_orders", None) or {}
        for (_s, _oid), _rec in list(pend.items()):
            logger.error(f"[main] 退出时仍有未对账的撤单结果未知订单: {_s} id={_oid} "
                         f"尝试={_rec.get('attempts')} 原因={_rec.get('reason')}，请人工核查")
        return len(pend)

    _results = await _shutdown_components([
        ("tasks",                    _stop_tasks),
        ("pm",                       pm.stop),
        ("signal_watcher.gc",        signal_watcher.stop_gc),
        ("signal_watcher.polling",   signal_watcher.stop_polling),
        ("signal_watcher.recheck",   signal_watcher.stop_recheck),
        ("signal_watcher.btc_guard", signal_watcher.stop_btc_guard),
        ("orphan_orders_report",     _report_orphans),
        ("ws",                       ws.stop),
    ], total_timeout=30.0, per_timeout=10.0)
    _bad = {k: v for k, v in _results.items() if v != "ok"}
    if _bad:
        logger.error(f"[main] fx 系统已停止，但部分组件未正常清理: {_bad}")
    else:
        logger.info("[main] fx 系统已安全停止")


if __name__ == "__main__":
    import sys, traceback as _tb
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("[main] KeyboardInterrupt，正常退出")
    except SystemExit as e:
        logger.info(f"[main] SystemExit({e.code})")
    except Exception as e:
        logger.critical(f"[main] 未捕获异常导致退出: {e}", exc_info=True)
        sys.exit(1)
    logger.info("[main] asyncio.run 正常返回，进程退出")
