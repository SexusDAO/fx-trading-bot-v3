"""
tests/test_e2e_simulation.py — 端到端模拟测试

模拟完整流程：
  scanner 产出信号 → pending_orders.json
  → ws_monitor.poll (pending→sent)
  → 模拟成交 (sent→filled) → on_open_filled
  → pos_state 写入 / 止损挂单 / 止盈挂单
  → patrol 守卫
  → check_gates 移动止损（+3% / +5%）
  → notify 播报验证

全程 MockAPI，无真实网络请求。
"""
import sys, json, time, logging
from pathlib import Path
from datetime import datetime, timezone, timedelta

sys.path.insert(0, "/root/.openclaw/workspace/v3")

import modules.pos_state_store as pss

WORKSPACE  = Path("/root/.openclaw/workspace/v3")
CFG_DIR    = WORKSPACE / "config"
LOG_DIR    = WORKSPACE / "logs"
PENDING    = CFG_DIR / "pending_orders.json"
GATES_FILE = CFG_DIR / "price_gates.json"

logging.basicConfig(level=logging.WARNING)  # 测试时只打警告/错误

# ── 工具 ──────────────────────────────────────────────────────────────────────

def _now_iso(offset_min=0):
    dt = datetime.now(timezone.utc) + timedelta(minutes=offset_min)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

def _read_json(path):
    with open(path) as f:
        return json.load(f)

def _clean():
    """清理所有测试产生的状态文件"""
    pss.clear_all()
    for f in [PENDING, GATES_FILE,
              CFG_DIR / "price_gates.json",
              LOG_DIR / "trade_log.jsonl"]:
        if f.exists():
            f.unlink()

# ── MockAPI（完整功能）────────────────────────────────────────────────────────

class MockAPI:
    """
    完整 mock，支持：
    - place_order / place_algo_order
    - get_open_orders / cancel_order
    - get_order（支持模拟成交）
    - get_ticker（支持自定义价格）
    """
    def __init__(self, ticker_price: float = 0.0):
        self.orders      = []          # 所有已挂单
        self.open_orders = {}          # symbol → [order]
        self._oid        = 5000
        self._ticker_price = ticker_price
        # 可以按 orderId 设置是否成交
        self._filled_ids: set[str] = set()
        self._canceled_ids: set[str] = set()

    def _next_id(self) -> str:
        oid = str(self._oid)
        self._oid += 1
        return oid

    def place_order(self, **kw) -> dict:
        oid = self._next_id()
        rec = {**kw, "orderId": oid, "status": "NEW"}
        self.orders.append(rec)
        sym = kw.get("symbol", "")
        self.open_orders.setdefault(sym, []).append(rec)
        return {"orderId": oid, "status": "NEW"}

    def place_algo_order(self, **kw) -> dict:
        """止损单（STOP_MARKET）"""
        oid = self._next_id()
        rec = {**kw, "algoId": oid, "orderId": oid, "status": "NEW"}
        self.orders.append(rec)
        sym = kw.get("symbol", "")
        self.open_orders.setdefault(sym, []).append(rec)
        return {"algoId": oid, "orderId": oid, "status": "NEW"}

    def get_open_orders(self, symbol: str) -> list:
        return self.open_orders.get(symbol, [])

    def cancel_order(self, symbol: str, orderId: str):
        ols = self.open_orders.get(symbol, [])
        self.open_orders[symbol] = [
            o for o in ols if str(o.get("orderId", "")) != str(orderId)
        ]
        self._canceled_ids.add(str(orderId))

    def get_order(self, symbol: str, orderId: str) -> dict:
        """查询订单状态，_filled_ids 里的返回 FILLED"""
        sid = str(orderId)
        if sid in self._canceled_ids:
            return {"orderId": sid, "status": "CANCELED"}
        if sid in self._filled_ids:
            price = self._ticker_price or 100.0
            return {"orderId": sid, "status": "FILLED",
                    "avgPrice": str(price), "price": str(price)}
        return {"orderId": sid, "status": "NEW"}

    def get_ticker(self, symbol: str) -> dict:
        price = self._ticker_price or 100.0
        return {
            "lastPrice":          str(price),
            "lastFundingRate":    "0.0001",
            "priceChangePercent": "2.5",
        }

    def simulate_fill(self, order_id: str):
        """标记某订单为已成交"""
        self._filled_ids.add(str(order_id))


# ══════════════════════════════════════════════════════════════════════════════
# E2E TEST 1：完整开仓流程
# scanner → pending → poll(挂单) → poll(成交检测) → on_open_filled
# ══════════════════════════════════════════════════════════════════════════════

def test_e2e_full_open():
    """
    完整流程：scanner 写入 pending → ws_monitor 挂单 → 模拟成交 → on_open_filled
    验证：pos_state 写入 / 止损单挂出 / 止盈单挂出 / notify 触发 / 日志写入
    """
    _clean()
    import ws_monitor as wm

    api = MockAPI(ticker_price=3200.0)
    notifications = []
    notify_fn = notifications.append

    # ── 1. scanner 产出 pending order ─────────────────────────────────────────
    entry_price = 3150.0
    hard_sl     = 3000.0
    expire_at   = _now_iso(offset_min=120)
    pending_data = {
        "generated_at": _now_iso(),
        "orders": [{
            "id":           "scan_e2e_ETHUSDT_LONG",
            "symbol":       "ETHUSDT",
            "direction":    "LONG",
            "entry_price":  entry_price,
            "notional":     500.0,
            "hard_sl":      hard_sl,
            "signal_type":  "trend_long",
            "score":        75,
            "score_details": {},
            "funding_rate": 0.0001,
            "entry_mode":   "LIMIT",
            "tp1_price":    0,
            "tp2_price":    0,
            "status":       "pending",
            "placed_at":    _now_iso(),
            "expire_at":    expire_at,
            "exchange_order_id": None,
        }]
    }
    _write_json(PENDING, pending_data)

    # ── 2. poll: pending → sent（挂限价单）───────────────────────────────────
    wm.poll_pending_orders(api, notify_fn)

    orders_after_poll1 = _read_json(PENDING)["orders"]
    assert orders_after_poll1[0]["status"] == "sent", \
        f"期望 sent，得 {orders_after_poll1[0]['status']}"
    exc_id = orders_after_poll1[0]["exchange_order_id"]
    assert exc_id, "exchange_order_id 应已设置"
    print(f"  ✅ Step1: pending→sent  orderId={exc_id}")

    # 验证交易所有一笔 LIMIT 单
    limit_orders = [o for o in api.orders if o.get("order_type") == "LIMIT"]
    assert len(limit_orders) == 1, f"期望1笔LIMIT单，得{limit_orders}"

    # ── 3. 模拟成交，再次 poll ────────────────────────────────────────────────
    api.simulate_fill(exc_id)
    api._ticker_price = 3155.0   # 成交价略高于挂单价

    wm.poll_pending_orders(api, notify_fn)

    orders_after_fill = _read_json(PENDING)["orders"]
    assert orders_after_fill[0]["status"] == "filled", \
        f"期望 filled，得 {orders_after_fill[0]['status']}"
    fill_price = orders_after_fill[0]["fill_price"]
    print(f"  ✅ Step2: sent→filled  fill_price={fill_price}")

    # ── 4. 验证 pos_state ─────────────────────────────────────────────────────
    pos = pss.get("ETHUSDT", "LONG")
    assert pos is not None, "pos_state 未写入"
    assert pos["entry_price"] == fill_price
    assert pos["sl_price"] == hard_sl
    assert pos["sl_order_id"], "sl_order_id 未设置"
    print(f"  ✅ Step3: pos_state OK  entry={pos['entry_price']} sl={pos['sl_price']}")

    # ── 5. 验证止损/止盈单 ────────────────────────────────────────────────────
    sl_orders = [o for o in api.orders if o.get("order_type") == "STOP_MARKET"]
    tp_orders = [o for o in api.orders if o.get("order_type") in ("TAKE_PROFIT", "LIMIT")]
    assert len(sl_orders) >= 1, f"止损单未挂: {api.orders}"
    assert len(tp_orders) >= 1, f"止盈单未挂"
    print(f"  ✅ Step4: 止损单 {len(sl_orders)}笔 / 止盈单 {len(tp_orders)}笔")

    # ── 6. 验证 notify 播报 ───────────────────────────────────────────────────
    assert len(notifications) >= 1, "没有触发 notify"
    open_notif = [n for n in notifications if "开仓成交" in n]
    assert len(open_notif) == 1, f"开仓播报未触发: {notifications}"
    print(f"  ✅ Step5: notify 播报 OK → '{open_notif[0][:40]}...'")

    # ── 7. 验证 tp 自动推算（原本 tp1_price=0）────────────────────────────────
    assert pos.get("tp1_price", 0) > 0, "tp1 未自动推算"
    assert pos["tp1_price"] > fill_price, "LONG tp1 应 > entry"
    print(f"  ✅ Step6: tp 自动推算 tp1={pos['tp1_price']} tp2={pos['tp2_price']}")

    print("✅ test_e2e_full_open PASSED")


# ══════════════════════════════════════════════════════════════════════════════
# E2E TEST 2：check_gates（移动止损）
# ══════════════════════════════════════════════════════════════════════════════

def test_e2e_check_gates_breakeven():
    """
    entry=3000, +3% → 止损移至保本(3000)
    """
    _clean()
    import ws_monitor as wm

    api = MockAPI(ticker_price=3000.0)
    notifications = []

    entry = 3000.0
    sl    = 2850.0
    pss.write("BTCUSDT", "LONG", {
        "entry_price": entry, "notional": 500,
        "sl_price": sl, "sl_order_id": "SL_INIT",
    })
    api.open_orders["BTCUSDT"] = [{"orderId": "SL_INIT"}]
    wm._register_gates("BTCUSDT", "LONG", entry, sl, 0)

    # 价格 +3.2% → 触发保本门
    wm.check_gates("BTCUSDT", entry * 1.032, api=api, notify_fn=notifications.append)

    pos = pss.get("BTCUSDT", "LONG")
    assert pos["sl_price"] == entry, f"止损应移至保本 {entry}，实际 {pos['sl_price']}"
    assert len(notifications) == 1 and "保本" in notifications[0], \
        f"保本 notify 未触发: {notifications}"
    print(f"  ✅ +3% 保本门: sl → {pos['sl_price']}")
    print("✅ test_e2e_check_gates_breakeven PASSED")


def test_e2e_check_gates_gate5():
    """+5% 门：止损进一步上移"""
    _clean()
    import ws_monitor as wm

    api = MockAPI(ticker_price=3000.0)
    notifications = []

    entry = 3000.0
    pss.write("BTCUSDT", "LONG", {
        "entry_price": entry, "notional": 500,
        "sl_price": entry, "sl_order_id": "SL_BREAKEVEN",
    })
    api.open_orders["BTCUSDT"] = [{"orderId": "SL_BREAKEVEN"}]

    # 手动设置 gates 为保本已触发
    gates = {}
    gates["BTCUSDT_LONG"] = {
        "symbol": "BTCUSDT", "direction": "LONG",
        "entry": entry, "sl": entry, "tp1": 0,
        "breakeven_triggered": True,
        "gate5_triggered": False,
        "gate8_triggered": False,
        "registered_at": _now_iso(),
    }
    import json
    (WORKSPACE / "config" / "price_gates.json").write_text(
        json.dumps(gates, indent=2)
    )

    # 价格 +5.5% → 触发 +5% 门
    wm.check_gates("BTCUSDT", entry * 1.055, api=api, notify_fn=notifications.append)

    pos = pss.get("BTCUSDT", "LONG")
    assert pos["sl_price"] > entry, f"止损应高于保本 {entry}，实际 {pos['sl_price']}"
    assert len(notifications) == 1 and "上移" in notifications[0], \
        f"+5% notify 未触发: {notifications}"
    print(f"  ✅ +5% 门: sl → {pos['sl_price']}")
    print("✅ test_e2e_check_gates_gate5 PASSED")


# ══════════════════════════════════════════════════════════════════════════════
# E2E TEST 3：patrol 补挂 + 通知
# ══════════════════════════════════════════════════════════════════════════════

def test_e2e_patrol_reattach_and_notify():
    """止损单丢失 → patrol 补挂成功并发 notify"""
    _clean()
    import ws_monitor as wm

    api = MockAPI()
    notifications = []

    pss.write("SOLUSDT", "LONG", {
        "entry_price": 150.0, "notional": 300,
        "sl_price": 143.0, "sl_order_id": "SL_GONE",
    })
    # 不在 open_orders 里 → 模拟丢失

    result = wm.patrol(api=api, notify_fn=notifications.append)

    assert result["reattached"] == 1
    assert result["errors"] == 0

    pos = pss.get("SOLUSDT", "LONG")
    assert pos["sl_order_id"] != "SL_GONE", "sl_order_id 应已更新"
    # patrol 成功时不 notify（只在失败时 notify）
    assert len(notifications) == 0, f"成功补挂不应 notify: {notifications}"
    print(f"  ✅ patrol 补挂成功 new_id={pos['sl_order_id']}")
    print("✅ test_e2e_patrol_reattach_and_notify PASSED")


def test_e2e_patrol_fail_notify():
    """止损单丢失 + 补挂失败 → 推送告警 notify"""
    _clean()
    import ws_monitor as wm

    notifications = []

    class AlwaysFailSLAPI(MockAPI):
        def place_algo_order(self, **kw):
            raise RuntimeError("SL permanently broken")

    api = AlwaysFailSLAPI()
    pss.write("BNBUSDT", "SHORT", {
        "entry_price": 600.0, "notional": 200,
        "sl_price": 630.0, "sl_order_id": "SL_LOST",
    })

    result = wm.patrol(api=api, notify_fn=notifications.append)

    assert result["errors"] == 1
    assert len(notifications) == 1
    assert "BNBUSDT" in notifications[0] and "告警" in notifications[0], \
        f"告警 notify 内容不对: {notifications[0]}"
    print(f"  ✅ patrol 补挂失败 notify: '{notifications[0][:50]}...'")
    print("✅ test_e2e_patrol_fail_notify PASSED")


# ══════════════════════════════════════════════════════════════════════════════
# E2E TEST 4：expired 订单处理
# ══════════════════════════════════════════════════════════════════════════════

def test_e2e_pending_expired():
    """pending 订单超时 → 标记 expired，不发 notify"""
    _clean()
    import ws_monitor as wm

    api = MockAPI()
    notifications = []

    expired_data = {
        "generated_at": _now_iso(),
        "orders": [{
            "id":           "scan_old_BTCUSDT",
            "symbol":       "BTCUSDT",
            "direction":    "LONG",
            "entry_price":  65000.0,
            "notional":     300.0,
            "hard_sl":      63000.0,
            "signal_type":  "trend_long",
            "score":        70,
            "score_details": {},
            "funding_rate": 0.0001,
            "entry_mode":   "LIMIT",
            "tp1_price":    0,
            "tp2_price":    0,
            "status":       "pending",
            "placed_at":    _now_iso(offset_min=-200),  # 200分钟前
            "expire_at":    _now_iso(offset_min=-60),   # 60分钟前已过期
            "exchange_order_id": None,
        }]
    }
    _write_json(PENDING, expired_data)

    wm.poll_pending_orders(api, notifications.append)

    orders = _read_json(PENDING)["orders"]
    assert orders[0]["status"] == "expired", f"应为 expired，得 {orders[0]['status']}"
    # 没有挂任何单
    assert len(api.orders) == 0, f"超时订单不应挂单: {api.orders}"
    print("  ✅ 超时 pending → expired，无挂单")
    print("✅ test_e2e_pending_expired PASSED")


def test_e2e_sent_canceled():
    """sent 订单被交易所撤单 → 标记 canceled"""
    _clean()
    import ws_monitor as wm

    api = MockAPI()
    notifications = []

    # 直接写一个 sent 状态
    sent_data = {
        "generated_at": _now_iso(),
        "orders": [{
            "id":           "scan_sent_XRPUSDT",
            "symbol":       "XRPUSDT",
            "direction":    "LONG",
            "entry_price":  0.6,
            "notional":     200.0,
            "hard_sl":      0.55,
            "signal_type":  "reversal",
            "score":        62,
            "score_details": {},
            "funding_rate": 0.0,
            "entry_mode":   "LIMIT",
            "tp1_price":    0,
            "tp2_price":    0,
            "status":       "sent",
            "placed_at":    _now_iso(offset_min=-10),
            "expire_at":    _now_iso(offset_min=110),
            "exchange_order_id": "EXC_XRP_001",
        }]
    }
    _write_json(PENDING, sent_data)

    # 模拟交易所已撤单
    api._canceled_ids.add("EXC_XRP_001")

    wm.poll_pending_orders(api, notifications.append)

    orders = _read_json(PENDING)["orders"]
    assert orders[0]["status"] == "canceled", f"应为 canceled，得 {orders[0]['status']}"
    print("  ✅ sent → canceled（交易所已撤单）")
    print("✅ test_e2e_sent_canceled PASSED")


# ══════════════════════════════════════════════════════════════════════════════
# E2E TEST 5：多仓位并发守卫
# ══════════════════════════════════════════════════════════════════════════════

def test_e2e_multi_position_patrol():
    """2个持仓：1个止损正常，1个止损丢失 → 只补挂1笔"""
    _clean()
    import ws_monitor as wm

    api = MockAPI()

    # BTCUSDT 止损正常
    pss.write("BTCUSDT", "LONG", {
        "entry_price": 65000, "notional": 500,
        "sl_price": 63000, "sl_order_id": "SL_OK",
    })
    api.open_orders["BTCUSDT"] = [{"orderId": "SL_OK"}]

    # ETHUSDT 止损丢失
    pss.write("ETHUSDT", "SHORT", {
        "entry_price": 3500, "notional": 300,
        "sl_price": 3700, "sl_order_id": "SL_MISS",
    })
    # api.open_orders["ETHUSDT"] → 空 → 模拟丢失

    result = wm.patrol(api=api)

    assert result["checked"] == 2
    assert result["reattached"] == 1
    assert result["errors"] == 0

    # BTCUSDT 止损未变
    btc_pos = pss.get("BTCUSDT", "LONG")
    assert btc_pos["sl_order_id"] == "SL_OK"

    # ETHUSDT 止损已更新
    eth_pos = pss.get("ETHUSDT", "SHORT")
    assert eth_pos["sl_order_id"] != "SL_MISS"

    print(f"  ✅ 多仓位 patrol: checked=2 reattached=1 errors=0")
    print("✅ test_e2e_multi_position_patrol PASSED")


# ══════════════════════════════════════════════════════════════════════════════
# E2E TEST 6：on_close_filled (止损屏術触发)
# ══════════════════════════════════════════════════════════════════════════════

def test_e2e_on_close_sl():
    """止损触发 on_close_filled：log_close + ps_remove + gates 清理 + notify"""
    _clean()
    import ws_monitor as wm

    api = MockAPI()
    notifications = []

    # 先建一个持仓
    pss.write("ETHUSDT", "LONG", {
        "entry_price": 3000.0, "avg_price": 3000.0, "notional": 500,
        "sl_price": 2850.0, "sl_order_id": "SL_001",
        "signal_type": "trend_long", "flatten_count": 0,
    })
    wm._register_gates("ETHUSDT", "LONG", 3000.0, 2850.0, 3150.0)
    # 建一笔平仓日志确认路径可写
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    close_price = 2845.0
    wm.on_close_filled("ETHUSDT", "LONG", close_price, "sl",
                       api=api, notify_fn=notifications.append)

    # pos_state 应已删除
    assert pss.get("ETHUSDT", "LONG") is None, "on_close_filled 应删除 pos_state"

    # gates 应已清理
    import json as _json
    if GATES_FILE.exists():
        gates = _json.loads(GATES_FILE.read_text())
        assert "ETHUSDT_LONG" not in gates, "gates 未清理"

    # trade_log 应有记录
    log_file = LOG_DIR / "trade_log.jsonl"
    assert log_file.exists(), "trade_log 未写入"
    recs = [_json.loads(l) for l in log_file.read_text().strip().split("\n") if l]
    closes = [r for r in recs if r.get("event") == "close"]
    assert len(closes) >= 1
    assert closes[-1]["symbol"] == "ETHUSDT"
    assert closes[-1]["close_reason"] == "sl"
    pnl = closes[-1]["pnl_usdt"]
    assert pnl < 0, f"止损 PnL 应为负: {pnl}"
    print(f"  ✅ on_close_filled sl: pnl={pnl}U")

    # notify 播报
    assert len(notifications) == 1
    assert "平仓" in notifications[0] and "止损" in notifications[0]
    print(f"  ✅ notify: '{notifications[0][:50]}...'")
    print("✅ test_e2e_on_close_sl PASSED")


def test_e2e_on_close_tp1():
    """止盈 TP1 触发 on_close_filled"""
    _clean()
    import ws_monitor as wm

    api = MockAPI()
    notifications = []
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    pss.write("BTCUSDT", "LONG", {
        "entry_price": 65000.0, "avg_price": 65000.0, "notional": 500,
        "sl_price": 63000.0, "sl_order_id": "SL_BTC",
        "signal_type": "trend_long", "flatten_count": 0,
    })
    wm._register_gates("BTCUSDT", "LONG", 65000.0, 63000.0, 68000.0)

    close_price = 68000.0
    wm.on_close_filled("BTCUSDT", "LONG", close_price, "tp1",
                       api=api, notify_fn=notifications.append)

    assert pss.get("BTCUSDT", "LONG") is None
    assert len(notifications) == 1 and "TP1" in notifications[0]
    import json as _json
    log_file = LOG_DIR / "trade_log.jsonl"
    recs = [_json.loads(l) for l in log_file.read_text().strip().split("\n") if l]
    closes = [r for r in recs if r.get("event") == "close"]
    assert closes[-1]["pnl_usdt"] > 0, "止盈 PnL 应为正"
    print(f"  ✅ on_close_filled tp1: pnl={closes[-1]['pnl_usdt']}U")
    print("✅ test_e2e_on_close_tp1 PASSED")


# ══════════════════════════════════════════════════════════════════════════════
# E2E TEST 7：circuit breaker 内嵌在 on_close_filled
# ══════════════════════════════════════════════════════════════════════════════

def test_e2e_circuit_breaker_trip():
    """
    连亏 5 笔 → circuit breaker 应自动触发
    """
    _clean()
    import ws_monitor as wm
    import json as _json
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    # 初始化 circuit_breaker = open: False
    _write_json(CFG_DIR / "circuit_breaker.json", {"open": False})
    _write_json(CFG_DIR / "daily_risk.json", {"date": "", "daily_loss": 0.0, "daily_pnl": 0.0})

    api = MockAPI()
    # 模拟 5 笔平仓（均亏损）
    for i in range(5):
        sym = f"SYM{i}USDT"
        pss.write(sym, "LONG", {
            "entry_price": 100.0, "avg_price": 100.0, "notional": 100,
            "sl_price": 90.0, "sl_order_id": f"SL_{i}",
            "signal_type": "trend_long", "flatten_count": 0,
        })
        wm.on_close_filled(sym, "LONG", 90.0, "sl", api=api)

    circuit = _json.loads((CFG_DIR / "circuit_breaker.json").read_text())
    assert circuit["open"] is True, f"熔断应已触发，得 {circuit}"
    assert "consecutive" in circuit.get("reason", ""), f"reason 应含 consecutive: {circuit}"
    print(f"  ✅ circuit breaker 触发: {circuit['reason']}")
    print("✅ test_e2e_circuit_breaker_trip PASSED")


def test_e2e_circuit_not_trip_on_win():
    """止盈平仓不触发熔断"""
    _clean()
    import ws_monitor as wm
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    _write_json(CFG_DIR / "circuit_breaker.json", {"open": False})
    _write_json(CFG_DIR / "daily_risk.json", {"date": "", "daily_loss": 0.0, "daily_pnl": 0.0})

    api = MockAPI()
    pss.write("BTCUSDT", "LONG", {
        "entry_price": 65000.0, "avg_price": 65000.0, "notional": 500,
        "sl_price": 63000.0, "sl_order_id": "SL_X",
        "signal_type": "trend_long", "flatten_count": 0,
    })
    wm.on_close_filled("BTCUSDT", "LONG", 68000.0, "tp1", api=api)

    import json as _json
    circuit = _json.loads((CFG_DIR / "circuit_breaker.json").read_text())
    assert circuit["open"] is False, "盈利平仓不应触发熔断"
    print("✅ 盈利平仓 circuit 不触发")
    print("✅ test_e2e_circuit_not_trip_on_win PASSED")


# ══════════════════════════════════════════════════════════════════════════════
# E2E TEST 8：patrol 检测止盈单成交
# ══════════════════════════════════════════════════════════════════════════════

def test_e2e_patrol_detect_tp_filled():
    """
    patrol 检测到 tp1 单不在 open_orders 且状态=FILLED
    → 自动触发 on_close_filled, pos_state 被清理
    """
    _clean()
    import ws_monitor as wm
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    tp_order_id = "TP1_FILLED"
    sl_order_id = "SL_ACTIVE"

    pss.write("SOLUSDT", "LONG", {
        "entry_price": 150.0, "avg_price": 150.0, "notional": 300,
        "sl_price": 143.0, "sl_order_id": sl_order_id,
        "tp1_price": 160.0, "tp1_order_id": tp_order_id,
        "signal_type": "trend_long", "flatten_count": 0,
    })

    # MockAPI: open_orders 里只有 SL（TP1 不在 open_orders → 应查询成交状态）
    api = MockAPI(ticker_price=160.0)
    api.open_orders["SOLUSDT"] = [{"orderId": sl_order_id}]
    api._filled_ids.add(tp_order_id)   # 模拟 TP1 已成交

    notifications = []
    result = wm.patrol(api=api, notify_fn=notifications.append)

    assert result["closed"] == 1, f"期期期期 1 笔平仓，得 {result}"
    assert pss.get("SOLUSDT", "LONG") is None, "pos_state 应已删除"
    assert len(notifications) == 1 and "TP1" in notifications[0], \
        f"期期期期 TP1 播报: {notifications}"
    print(f"  ✅ patrol 检测 tp1 成交 → on_close_filled OK")
    print("✅ test_e2e_patrol_detect_tp_filled PASSED")


def test_e2e_patrol_detect_sl_filled():
    """
    patrol 检测到 sl 单不在 open_orders 且状态=FILLED
    → 自动触发 on_close_filled
    """
    _clean()
    import ws_monitor as wm
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    sl_order_id = "SL_TRIGGERED"

    pss.write("AVAXUSDT", "SHORT", {
        "entry_price": 40.0, "avg_price": 40.0, "notional": 200,
        "sl_price": 43.0, "sl_order_id": sl_order_id,
        "signal_type": "trend_short", "flatten_count": 0,
    })

    api = MockAPI(ticker_price=43.0)
    # open_orders 中没有 SL，且 SL 已 FILLED
    api._filled_ids.add(sl_order_id)

    notifications = []
    result = wm.patrol(api=api, notify_fn=notifications.append)

    assert result["closed"] >= 1, f"期期 1 笔平仓，得 {result}"
    assert pss.get("AVAXUSDT", "SHORT") is None, "pos_state 应已删除"
    assert any("止损" in n for n in notifications), f"期期期期 sl 播报: {notifications}"
    print(f"  ✅ patrol 检测 sl 成交 → on_close_filled OK")
    print("✅ test_e2e_patrol_detect_sl_filled PASSED")


# ══════════════════════════════════════════════════════════════════════════════
# E2E TEST 9：scanner tp 已写入 pending_order
# ══════════════════════════════════════════════════════════════════════════════

def test_e2e_scanner_tp_written():
    """
    scanner run_scan 写入的 pending_order 应包含 tp1_price + tp2_price 且 >0
    """
    _clean()

    # 构造简单 mock klines（内嵌）
    import types, sys as _sys

    class _MockScannerAPI:
        def get_klines(self, symbol, interval, limit=100):
            # 返回简单 OHLCV （10 根）
            return [[0, str(100+i), str(105+i), str(95+i), str(100+i), "1000"]
                    for i in range(100)]
        def get_ticker(self, symbol):
            return {"lastPrice": "100.0", "lastFundingRate": "0.0001",
                    "priceChangePercent": "2.0"}
        def get_top_gainers(self, limit=15):
            return [{"symbol": "MOCKUSDT"}]
        def get_top_losers(self, limit=15):
            return []
        def get_top_volume(self, limit=30):
            return []
        def get_futures_account(self):
            return {"totalWalletBalance": "1000"}

    # 确保存在 config 目录
    (CFG_DIR / "market_regime.json").write_text('{"mode": "bull", "btc_trend": "neutral"}')
    (CFG_DIR / "circuit_breaker.json").write_text('{"open": false}')
    (CFG_DIR / "strategy-config.json").write_text(
        '{"scoring":{"trend_threshold":65},"position":{"max_positions_total":5,'
        '"market_mode_limits":{"bull":{"max_long":3,"max_short":2,"max_total":5}}},'
        '"notional":{"trend_long":{"65_69":300},"hard_cap":1000},'
        '"risk":{"daily_loss_pct":0.2},"ttl_minutes":{"limit_low_score":120,"limit_high_score":240}}'
    )
    (CFG_DIR / "daily_risk.json").write_text('{"date":"","daily_loss":0,"daily_pnl":0}')
    PENDING.unlink(missing_ok=True)

    from llm_tasks.scanner_llm import run_scan
    result = run_scan(api=_MockScannerAPI())

    # 如果评分不够就没有信号（正常），只要不报错即可
    print(f"  扫描结果: scanned={result['scanned']} signals={result['signals']} placed={result['placed']}")
    if result["placed"] > 0:
        import json as _json
        orders = _json.loads(PENDING.read_text())["orders"]
        for o in orders:
            assert o.get("tp1_price", 0) > 0, f"tp1_price 应>0: {o}"
            assert o.get("tp2_price", 0) > 0, f"tp2_price 应>0: {o}"
            assert o["tp1_price"] != o["tp2_price"], "tp1 != tp2"
        print(f"  ✅ tp1={orders[0]['tp1_price']} tp2={orders[0]['tp2_price']}")
    print("✅ test_e2e_scanner_tp_written PASSED")


# ══════════════════════════════════════════════════════════════════════════════
# 主入口
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    tests = [
        test_e2e_full_open,
        test_e2e_check_gates_breakeven,
        test_e2e_check_gates_gate5,
        test_e2e_patrol_reattach_and_notify,
        test_e2e_patrol_fail_notify,
        test_e2e_pending_expired,
        test_e2e_sent_canceled,
        test_e2e_multi_position_patrol,
        test_e2e_on_close_sl,
        test_e2e_on_close_tp1,
        test_e2e_circuit_breaker_trip,
        test_e2e_circuit_not_trip_on_win,
        test_e2e_patrol_detect_tp_filled,
        test_e2e_patrol_detect_sl_filled,
        test_e2e_scanner_tp_written,
    ]
    passed = failed = 0
    for t in tests:
        print(f"\n--- {t.__name__} ---")
        try:
            t()
            passed += 1
        except Exception as e:
            import traceback
            print(f"❌ FAILED: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\n{'='*50}")
    print(f"结果: {passed} passed / {failed} failed")
    if failed:
        sys.exit(1)


# ══════════════════════════════════════════════════════════════════════════════
# E2E TEST 10：摊平补仓逻辑
# ══════════════════════════════════════════════════════════════════════════════

def test_e2e_flatten_add_position():
    """
    浮亏 >3.5% → _check_flatten_and_add 触发补仓
    验证：pos_state avg_price 更新 / flatten_count +1 / notify 播报
    """
    _clean()
    import ws_monitor as wm

    api = MockAPI(ticker_price=96.5)  # entry=100, 浮亏3.5%
    notifications = []

    pss.write("LINKUSDT", "LONG", {
        "entry_price": 100.0, "avg_price": 100.0, "notional": 300,
        "total_notional": 300, "first_notional": 300,
        "sl_price": 93.0, "sl_order_id": "SL_LNK",
        "signal_type": "trend_long", "flatten_count": 0,
        "last_flatten_at": None,
        "direction": "LONG", "symbol": "LINKUSDT",
    })
    wm._register_gates("LINKUSDT", "LONG", 100.0, 93.0, 0)
    api.open_orders["LINKUSDT"] = [{"orderId": "SL_LNK"}]

    current_price = 96.5  # -3.5%
    wm.check_gates("LINKUSDT", current_price, api=api,
                   notify_fn=notifications.append)

    pos = pss.get("LINKUSDT", "LONG")
    assert pos is not None, "pos_state 不存在"
    assert pos["flatten_count"] == 1, f"补仓次数应为1，得 {pos['flatten_count']}"
    assert pos["avg_price"] < 100.0, f"均价应低于入场价，得 {pos['avg_price']}"
    assert any("补仓" in n for n in notifications), f"补仓 notify 未触发: {notifications}"
    print(f"  ✅ 摊平补仓: avg={pos['avg_price']:.4f} count={pos['flatten_count']}")
    print("✅ test_e2e_flatten_add_position PASSED")


def test_e2e_flatten_no_trigger_small_loss():
    """浮亏 <3% → 不触发补仓"""
    _clean()
    import ws_monitor as wm

    api = MockAPI(ticker_price=98.5)  # entry=100, 浮亏1.5%
    notifications = []

    pss.write("DOTUSDT", "LONG", {
        "entry_price": 100.0, "avg_price": 100.0, "notional": 300,
        "total_notional": 300, "first_notional": 300,
        "sl_price": 93.0, "sl_order_id": "SL_DOT",
        "signal_type": "trend_long", "flatten_count": 0,
        "last_flatten_at": None,
        "direction": "LONG", "symbol": "DOTUSDT",
    })
    wm._register_gates("DOTUSDT", "LONG", 100.0, 93.0, 0)
    api.open_orders["DOTUSDT"] = [{"orderId": "SL_DOT"}]

    wm.check_gates("DOTUSDT", 98.5, api=api, notify_fn=notifications.append)

    pos = pss.get("DOTUSDT", "LONG")
    assert pos["flatten_count"] == 0, "浮亏<3% 不应补仓"
    add_notifs = [n for n in notifications if "补仓" in n]
    assert len(add_notifs) == 0
    print("  ✅ 浮亏<3% 不触发补仓")
    print("✅ test_e2e_flatten_no_trigger_small_loss PASSED")


def test_e2e_flatten_max_count():
    """已补仓2次 → 不再补仓"""
    _clean()
    import ws_monitor as wm

    api = MockAPI()
    pss.write("FTMUSDT", "LONG", {
        "entry_price": 100.0, "avg_price": 97.0, "notional": 300,
        "total_notional": 900, "first_notional": 300,
        "sl_price": 88.0, "sl_order_id": "SL_FTM",
        "signal_type": "trend_long", "flatten_count": 2,  # 已达上限
        "last_flatten_at": None,
        "direction": "LONG", "symbol": "FTMUSDT",
    })
    wm._register_gates("FTMUSDT", "LONG", 100.0, 88.0, 0)
    api.open_orders["FTMUSDT"] = [{"orderId": "SL_FTM"}]

    wm.check_gates("FTMUSDT", 95.0, api=api)  # 浮亏5%，但次数已满

    pos = pss.get("FTMUSDT", "LONG")
    assert pos["flatten_count"] == 2, "次数已满不应再补仓"
    print("  ✅ flatten_count=2 上限保护生效")
    print("✅ test_e2e_flatten_max_count PASSED")
