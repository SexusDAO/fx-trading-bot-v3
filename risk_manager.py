#!/usr/bin/env python3
"""
risk_manager.py — 风险管理模块（统一熔断 & 仓位风控）

职责：
  - 实时追踪当日亏损，驱动熔断（circuit breaker）逻辑
  - 维护连续亏损笔数（consecutive_losses），用于动态风控
  - 独立追踪做空方向当日亏损（short_loss_today）
  - 计算账户当前总名义仓位（total_notional）
  - 从 ws_monitor.py 剥离出来的熔断相关逻辑，接口向下兼容
  - 对外暴露 get_risk_summary() 用于快速查询当前风险状态

数据文件：
  /root/.openclaw/workspace/circuit_breaker.json
  字段：date / daily_loss / open / consecutive_losses / short_loss / last_known_equity

用法示例：
  import risk_manager
  print(risk_manager.get_risk_summary())
"""

import json
import os
import threading
from datetime import datetime, timezone

# ─── 路径常量 ─────────────────────────────────────────────────────────────────
WORKSPACE     = "/root/.openclaw/workspace"
CIRCUIT_FILE  = f"{WORKSPACE}/circuit_breaker.json"
CONFIG_FILE   = f"{WORKSPACE}/strategy-config.json"

# ─── 全局线程锁，保证多线程写入安全 ──────────────────────────────────────────
_circuit_lock = threading.Lock()


# ─────────────────────────────────────────────────────────────────────────────
# 内部辅助函数
# ─────────────────────────────────────────────────────────────────────────────

def _get_daily_loss_limit() -> float:
    """
    从 strategy-config.json 读取每日亏损限额（daily_loss_limit）。
    查找顺序：顶层 daily_loss_limit → risk.daily_loss_limit → 默认 100.0。
    若配置为字符串（如"动态=本金×20%"），则尝试从 last_known_equity 动态计算。
    """
    try:
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)

        # 尝试直接读取数值型配置
        val = cfg.get("daily_loss_limit") or cfg.get("risk", {}).get("daily_loss_limit")
        if val is not None:
            try:
                return float(val)
            except (ValueError, TypeError):
                pass

        # 配置为字符串（动态模式）：从 circuit_breaker.json 读 last_known_equity × 20%
        equity = _get_last_known_equity()
        if equity > 0:
            return round(equity * 0.20, 2)

    except Exception:
        pass
    return 100.0


def _get_last_known_equity() -> float:
    """
    从 circuit_breaker.json 读取上次已知权益（last_known_equity）。
    用于动态熔断线计算（本金 × 20%）。
    """
    try:
        with open(CIRCUIT_FILE) as f:
            data = json.load(f)
        return float(data.get("last_known_equity", 0.0))
    except Exception:
        return 0.0


# ─────────────────────────────────────────────────────────────────────────────
# 熔断文件读写（向下兼容 ws_monitor.py 的 load_circuit / save_circuit）
# ─────────────────────────────────────────────────────────────────────────────

def load_circuit() -> dict:
    """
    读取 circuit_breaker.json。
    若日期与今日不符，则自动重置 daily_loss / open / consecutive_losses / short_loss，
    保留 last_known_equity（不清空，供动态熔断线使用）。
    返回完整的 dict，字段缺失时填默认值。
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    default = {
        "date": today,
        "daily_loss": 0.0,
        "open": False,
        "consecutive_losses": 0,
        "short_loss": 0.0,
        "last_known_equity": 0.0,
    }
    try:
        with open(CIRCUIT_FILE) as f:
            data = json.load(f)

        if data.get("date") != today:
            # 新的一天：重置交易计数，保留权益快照
            equity = float(data.get("last_known_equity", 0.0))
            reset = dict(default)
            reset["last_known_equity"] = equity
            save_circuit(reset)
            return reset

        # 补全可能缺失的新字段（兼容旧格式文件）
        for k, v in default.items():
            data.setdefault(k, v)
        return data

    except Exception:
        save_circuit(default)
        return default


def save_circuit(data: dict):
    """
    原子写入 circuit_breaker.json（先写 .tmp 再 os.replace）。
    保证写入过程中断也不会损坏数据文件。
    """
    try:
        tmp = CIRCUIT_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, CIRCUIT_FILE)
    except Exception as e:
        # 写入失败只记录，不抛出，避免影响主流程
        import sys
        print(f"[risk_manager][save_circuit_err] {e}", file=sys.stderr)


# ─────────────────────────────────────────────────────────────────────────────
# 核心状态更新函数
# ─────────────────────────────────────────────────────────────────────────────

def add_loss(pnl_u: float, side: str = None):
    """
    累计一笔已实现亏损（pnl_u < 0 才计入）。
    - 更新 daily_loss（当日累计亏损）
    - 更新 consecutive_losses（连续亏损笔数）
    - 若 side == 'SHORT'，同步更新 short_loss（做空方向独立亏损统计）
    - 超过 daily_loss_limit 触发熔断（open = True）
    - 若 pnl_u >= 0 表示盈利，重置 consecutive_losses（连续亏损中断）

    参数：
      pnl_u : float  已实现盈亏（USDT）；负值为亏损，正值为盈利
      side  : str    仓位方向，"LONG" 或 "SHORT"；None 表示不区分方向
    """
    with _circuit_lock:
        data = load_circuit()

        if pnl_u >= 0:
            # 盈利笔：重置连续亏损计数
            data["consecutive_losses"] = 0
            save_circuit(data)
            return

        loss_abs = abs(pnl_u)
        data["daily_loss"] = round(data.get("daily_loss", 0.0) + loss_abs, 4)
        data["consecutive_losses"] = data.get("consecutive_losses", 0) + 1

        if side and side.upper() == "SHORT":
            data["short_loss"] = round(data.get("short_loss", 0.0) + loss_abs, 4)

        limit = _get_daily_loss_limit()
        if not data.get("open") and data["daily_loss"] >= limit:
            data["open"] = True

        save_circuit(data)


def reset_consecutive_losses():
    """
    手动重置连续亏损计数。
    用于：平仓后确认盈利、用户手动干预等场景。
    """
    with _circuit_lock:
        data = load_circuit()
        data["consecutive_losses"] = 0
        save_circuit(data)


def update_equity(equity: float):
    """
    更新 last_known_equity（当前账户权益快照）。
    由 ws_monitor.py 或外部调用，用于动态熔断线计算（本金 × 20%）。
    不加锁（equity 更新不影响熔断计数原子性）。
    """
    try:
        with open(CIRCUIT_FILE) as f:
            data = json.load(f)
    except Exception:
        data = {}
    data["last_known_equity"] = round(float(equity), 7)
    save_circuit(data)


def is_circuit_open() -> bool:
    """
    查询当前熔断状态。
    返回 True 表示今日已触发熔断，禁止新开仓。
    线程安全，可在任意线程频繁调用。
    """
    with _circuit_lock:
        return load_circuit().get("open", False)


def reset_circuit():
    """
    手动重置熔断（用于测试或用户强制恢复）。
    正常情况下由 load_circuit() 在新的一天自动重置。
    """
    with _circuit_lock:
        data = load_circuit()
        data["open"] = False
        data["daily_loss"] = 0.0
        data["consecutive_losses"] = 0
        data["short_loss"] = 0.0
        save_circuit(data)


# ─────────────────────────────────────────────────────────────────────────────
# 总名义仓位计算（从 binance_api 实时获取）
# ─────────────────────────────────────────────────────────────────────────────

def get_total_notional() -> float:
    """
    从 binance_api.get_positions() 获取当前所有持仓，
    计算总名义仓位（notionalValue 之和，取绝对值）。
    单位：USDT。
    若 API 调用失败，返回 -1.0 表示获取异常。
    """
    try:
        import sys
        sys.path.insert(0, WORKSPACE)
        import binance_api as api

        positions = api.get_positions()
        if not isinstance(positions, list):
            return -1.0

        total = 0.0
        for p in positions:
            # 优先使用 notionalValue 字段；其次用 |positionAmt × markPrice|
            nv = p.get("notionalValue") or p.get("notional")
            if nv is not None:
                total += abs(float(nv))
            else:
                amt = float(p.get("positionAmt", 0))
                mark = float(p.get("markPrice", 0))
                total += abs(amt * mark)

        return round(total, 4)
    except Exception:
        return -1.0


# ─────────────────────────────────────────────────────────────────────────────
# 对外聚合接口
# ─────────────────────────────────────────────────────────────────────────────

def get_risk_summary() -> dict:
    """
    返回当前完整风险状态快照，供监控脚本、决策引擎调用。

    返回字段：
      daily_loss         : float  — 今日累计已实现亏损（USDT）
      loss_limit         : float  — 今日熔断阈值（USDT）
      circuit_open       : bool   — 当前是否已熔断（True=禁止开仓）
      consecutive_losses : int    — 当前连续亏损笔数
      short_loss         : float  — 今日做空方向独立亏损（USDT）
      total_notional     : float  — 当前账户总名义仓位（USDT）；-1 表示获取失败
    """
    with _circuit_lock:
        data = load_circuit()

    daily_loss         = data.get("daily_loss", 0.0)
    circuit_open       = data.get("open", False)
    consecutive_losses = data.get("consecutive_losses", 0)
    short_loss         = data.get("short_loss", 0.0)
    loss_limit         = _get_daily_loss_limit()
    total_notional     = get_total_notional()

    # 保证金安全垫（功能三 v3.3）
    try:
        _ok, _mreason, _mstats = check_margin_safety()
    except Exception:
        _mstats = {}

    return {
        "daily_loss":         daily_loss,
        "loss_limit":         loss_limit,
        "circuit_open":       circuit_open,
        "consecutive_losses": consecutive_losses,
        "short_loss":         short_loss,
        "total_notional":     total_notional,
        "margin_usage_pct":   _mstats.get("margin_usage_pct", -1),
        "margin_warning":     _mstats.get("margin_warning", False),
        "margin_tight_hint":  _mstats.get("margin_tight_hint", ""),
    }


# ─────────────────────────────────────────────────────────────────────────────
# 功能三：保证金安全垫检查（v3.3）
# ─────────────────────────────────────────────────────────────────────────────
_MARGIN_WARN_LAST_TS: float = 0.0   # 告警限频：每小时最多1次

def check_margin_safety(notifier_fn=None) -> tuple[bool, str, dict]:
    """
    检查账户保证金使用率。
    返回 (ok_to_open, reason, stats_dict)。
    ok_to_open=False 时拒绝新开仓。
    stats_dict 包含 usage_pct / used / available / total。
    """
    global _MARGIN_WARN_LAST_TS
    import sys
    sys.path.insert(0, WORKSPACE)
    try:
        import binance_api as _bapi
        acct = _bapi.get_account()
        total_bal   = float(acct.get("totalWalletBalance", 0))
        init_margin = float(acct.get("totalInitialMargin", 0))
        avail_bal   = float(acct.get("availableBalance", 0))
    except Exception as e:
        return True, "", {"error": str(e)}

    if total_bal <= 0:
        return True, "", {}

    usage_pct = init_margin / total_bal * 100
    stats = {
        "margin_usage_pct": round(usage_pct, 1),
        "margin_used":      round(init_margin, 2),
        "margin_available": round(avail_bal, 2),
        "margin_total":     round(total_bal, 2),
        "margin_warning":   usage_pct >= 60,
    }

    now = _time.time()

    # ── 级别一：使用率 ≥ 75%：硬止新开仓 + 告警 ──
    if usage_pct >= 75:
        if notifier_fn and now - _MARGIN_WARN_LAST_TS >= 3600:
            _MARGIN_WARN_LAST_TS = now
            try:
                notifier_fn(
                    f"⚠️ 保证金告警\n"
                    f"当前使用率：{usage_pct:.1f}%（硬停线：75%）\n"
                    f"可用余额：{avail_bal:.1f}U\n"
                    f"已占用：{init_margin:.1f}U\n"
                    f"已自动暂停新开仓\n"
                    f"恢复条件：使用率降至75%以下"
                )
            except Exception:
                pass
        return False, f"保证金安全垫不足 使用率{usage_pct:.1f}%≥75%", stats

    # ── 级别二：使用率 60-75%：自动缩小新开仓仓位（指导圣 real_size 降级） ──
    if 60 <= usage_pct < 75:
        # 缩减比例：60% → scale=0.7，70% → scale=0.4，线性插値
        scale = round(1.0 - (usage_pct - 60) / 75 * 2.5, 2)
        scale = max(0.3, min(1.0, scale))   # 小于60%不缩，大于75%已硬停
        stats["margin_tight_hint"] = f"保证金偏紧，使用率{usage_pct:.1f}%"
        stats["position_scale"]    = scale   # 供外部读取并乘以仓位大小
        if notifier_fn and usage_pct >= 65 and now - _MARGIN_WARN_LAST_TS >= 3600:
            _MARGIN_WARN_LAST_TS = now
            try:
                notifier_fn(
                    f"🟡 保证金偏紧\n"
                    f"当前使用率：{usage_pct:.1f}%（预警线：60%）\n"
                    f"新开仓仓位自动缩小至 {scale*100:.0f}%\n"
                    f"可用余额：{avail_bal:.1f}U"
                )
            except Exception:
                pass

    return True, "", stats


# ─── Convenience class wrapper (thin façade over module-level functions) ──────
class RiskManager:
    """Thin OOP wrapper so callers can do: _risk = RiskManager()"""

    def add_loss(self, pnl_u: float, side: str = None):
        return add_loss(pnl_u, side=side)

    def is_circuit_open(self) -> bool:
        return is_circuit_open()

    def load_circuit(self) -> dict:
        return load_circuit()

    def save_circuit(self, data: dict):
        return save_circuit(data)

    def reset_circuit(self):
        return reset_circuit()

    def get_risk_summary(self) -> dict:
        return get_risk_summary()

    def check_margin_safety(self, notifier_fn=None):
        return check_margin_safety(notifier_fn=notifier_fn)


# ─────────────────────────────────────────────────────────────────────────────
# 功能一：大行情熔断（Market Shock Detector）v1.0
# ─────────────────────────────────────────────────────────────────────────────
import time as _time

SHOCK_STATE_FILE = f"{WORKSPACE}/ws_cache/market_shock_state.json"
SHOCK_DURATION   = 30 * 60   # 熔断持续30分钟
SHOCK_BTC_THRESH = 5.0       # BTC 1H 涨跌幅阈值（绝对值 %）
SHOCK_LIQ_THRESH = 500_000_000  # 全市场1H清算量阈值（5亿U）

def _load_shock_state() -> dict:
    try:
        import pathlib
        p = pathlib.Path(SHOCK_STATE_FILE)
        if p.exists():
            return json.loads(p.read_text())
    except Exception:
        pass
    return {"active": False, "triggered_at": 0, "reason": ""}

def _save_shock_state(state: dict):
    import pathlib
    pathlib.Path(SHOCK_STATE_FILE).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(SHOCK_STATE_FILE).write_text(json.dumps(state, ensure_ascii=False, indent=2))

def is_market_shock_active() -> bool:
    """检查熔断是否仍在激活窗口内（外部调用：scanner 用于跳过开仓）"""
    state = _load_shock_state()
    if not state.get("active"):
        return False
    elapsed = _time.time() - state.get("triggered_at", 0)
    if elapsed >= SHOCK_DURATION:
        state["active"] = False
        _save_shock_state(state)
        return False
    return True

def check_market_shock(notifier_fn=None) -> bool:
    """
    每轮扫描开始时调用。
    检测条件：BTC 1H K线涨跌幅绝对值 > 5%，或全市场1H清算量 > 5亿U。
    触发时：激活熔断状态30分钟，并通知 position_manager 收紧止损。
    返回 True 表示本轮应暂停开仓。
    """
    import urllib.request as _ur

    # ── 检查是否已在熔断窗口内 ──
    if is_market_shock_active():
        return True

    shock_reason = None

    # ── 条件1：BTC 1H 近一根K线涨跌幅 ──
    try:
        url = "https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval=1h&limit=2"
        klines = json.loads(_ur.urlopen(url, timeout=8).read())
        if len(klines) >= 2:
            k = klines[-2]  # 已收盘的上一根
            open_p  = float(k[1])
            close_p = float(k[4])
            chg_pct = abs((close_p - open_p) / open_p * 100)
            if chg_pct >= SHOCK_BTC_THRESH:
                shock_reason = f"BTC 1H 涨跌幅 {chg_pct:.1f}% ≥ {SHOCK_BTC_THRESH}%"
    except Exception:
        pass

    # ── 条件2：Coinglass 全市场1H清算量 ──
    if not shock_reason:
        try:
            import coinglass_api as _cg
            liq = _cg.get_liquidation_heatmap_total_1h()  # 返回 float（USD）
            if liq and liq >= SHOCK_LIQ_THRESH:
                shock_reason = f"全市场1H清算量 {liq/1e8:.1f}亿U ≥ 5亿U"
        except Exception:
            pass

    if not shock_reason:
        return False

    # ── 触发熔断 ──
    now_ts = _time.time()
    state = {
        "active": True,
        "triggered_at": now_ts,
        "reason": shock_reason,
    }
    _save_shock_state(state)

    # ── 收紧所有持仓止损 ──
    tightened = _tighten_all_sl(shock_reason, notifier_fn)

    # ── 推送告警 ──
    msg = (
        f"🚨 大行情熔断触发\n"
        f"原因：{shock_reason}\n"
        f"状态：暂停新开仓 30 分钟\n"
        f"收紧止损：{', '.join(tightened) if tightened else '无持仓'}"
    )
    if notifier_fn:
        try:
            notifier_fn(msg)
        except Exception:
            pass
    print(f"[market_shock] {msg}")
    return True

def _tighten_all_sl(reason: str, notifier_fn=None) -> list:
    """
    收紧所有持仓止损：止损向入场价方向移动 ATR×0.5，
    新止损不超过（原止损价 + 入场价）/ 2。
    返回已收紧的币种列表。
    """
    import pathlib
    import urllib.request as _ur

    tightened = []

    # 从 pos_state.json 读取所有持仓
    pos_files = [
        f"{WORKSPACE}/ws_cache/positions.json",
        f"{WORKSPACE}/paper_sim_state.json",
        f"{WORKSPACE}/paper_test_state.json",
    ]

    for pf in pos_files:
        try:
            if not pathlib.Path(pf).exists():
                continue
            data = json.loads(pathlib.Path(pf).read_text())
            positions = data.get("positions", {})
            if not positions:
                continue

            changed = False
            for sym, pos in positions.items():
                try:
                    entry = float(pos.get("entry") or pos.get("entry_price", 0))
                    sl    = float(pos.get("sl_price", 0))
                    direction = pos.get("direction", "LONG").upper()
                    if entry <= 0 or sl <= 0:
                        continue

                    # 拉当前 ATR
                    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={sym}&interval=1h&limit=20"
                    klines = json.loads(_ur.urlopen(url, timeout=8).read())
                    trs = []
                    for i in range(1, len(klines)):
                        h = float(klines[i][2]); lo = float(klines[i][3]); pc = float(klines[i-1][4])
                        trs.append(max(h-lo, abs(h-pc), abs(lo-pc)))
                    atr = sum(trs[-14:]) / min(14, len(trs)) if trs else abs(entry - sl) * 0.5

                    move = atr * 0.5
                    midpoint = (sl + entry) / 2

                    if direction == "LONG":
                        new_sl = sl + move          # 向上收紧
                        new_sl = min(new_sl, midpoint)  # 不超过中点
                        if new_sl > sl:
                            pos["sl_price"] = round(new_sl, 8)
                            pos["shock_tightened"] = True
                            changed = True
                            tightened.append(f"{sym}(SL {sl:.4g}→{new_sl:.4g})")
                    else:
                        new_sl = sl - move          # 向下收紧
                        new_sl = max(new_sl, midpoint)  # 不超过中点
                        if new_sl < sl:
                            pos["sl_price"] = round(new_sl, 8)
                            pos["shock_tightened"] = True
                            changed = True
                            tightened.append(f"{sym}(SL {sl:.4g}→{new_sl:.4g})")
                except Exception:
                    continue

            if changed:
                pathlib.Path(pf).write_text(json.dumps(data, ensure_ascii=False, indent=2))
        except Exception:
            continue

    return tightened
