"""tests/test_ws_monitor.py — mock API，不发真实请求"""
import sys, json, types
from pathlib import Path
sys.path.insert(0, "/root/.openclaw/workspace/v3")

from modules.pos_state_store import clear_all, get as ps_get, get_all as ps_get_all
from modules.trade_logger    import LOG_FILE

WORKSPACE = Path("/root/.openclaw/workspace/v3")
CFG_DIR   = WORKSPACE / "config"
PENDING   = CFG_DIR / "pending_orders.json"
GATES     = CFG_DIR / "price_gates.json"

# ── Mock API ──────────────────────────────────────────────────────────────────

class MockAPI:
    def __init__(self):
        self.orders   = []      # 已挂单记录
        self.open_orders = {}   # symbol → [order dicts]
        self._oid     = 1000

    def place_order(self, **kw):
        oid = str(self._oid); self._oid += 1
        self.orders.append({**kw, "orderId": oid})
        sym = kw.get("symbol", "")
        self.open_orders.setdefault(sym, []).append({"orderId": oid, **kw})
        return {"orderId": oid, "status": "NEW"}

    def place_algo_order(self, **kw):
        """mock 止损单 (STOP_MARKET algo order)"""
        oid = str(self._oid); self._oid += 1
        self.orders.append({**kw, "algoId": oid, "orderId": oid})
        sym = kw.get("symbol", "")
        self.open_orders.setdefault(sym, []).append({"orderId": oid, "algoId": oid, **kw})
        return {"algoId": oid, "orderId": oid, "status": "NEW"}

    def get_open_orders(self, symbol):
        return self.open_orders.get(symbol, [])

    def cancel_order(self, symbol, orderId):
        ols = self.open_orders.get(symbol, [])
        self.open_orders[symbol] = [o for o in ols if str(o["orderId"]) != str(orderId)]

# ── 工具 ──────────────────────────────────────────────────────────────────────

def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f)

def _clean():
    clear_all()
    for f in [PENDING, GATES, CFG_DIR/"price_gates.json"]:
        if f.exists(): f.unlink()
    if LOG_FILE.exists(): LOG_FILE.unlink()

def _make_pending(symbol="BTCUSDT", direction="LONG",
                  entry_price=65000.0, notional=500,
                  score=80, hard_sl=63000.0,
                  tp1=68000.0, tp2=71000.0,
                  status="pending"):
    from datetime import datetime, timezone, timedelta
    expire = (datetime.now(timezone.utc)+timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _write_json(PENDING, {"generated_at": "2026-08-03T00:00:00Z", "orders": [{
        "id": f"scan_001_{symbol}",
        "symbol":       symbol,
        "direction":    direction,
        "entry_price":  entry_price,
        "notional":     notional,
        "signal_type":  "trend",
        "score":        score,
        "score_details": {"ema": 25, "rsi": 15, "vol": 18, "struct": 22},
        "hard_sl":      hard_sl,
        "tp1_price":    tp1,
        "tp2_price":    tp2,
        "funding_rate": -0.001,
        "status":       status,
        "placed_at":    "2026-08-03T00:00:00Z",
        "expire_at":    expire,
        "exchange_order_id": None,
    }]})

# ── 测试：poll_pending_orders ─────────────────────────────────────────────────

def test_poll_places_order():
    """pending → 挂单 → status 改 sent"""
    _clean()
    _make_pending()
    api = MockAPI()
    import ws_monitor as wm
    wm.poll_pending_orders(api)

    pending = json.loads(PENDING.read_text())
    o = pending["orders"][0]
    assert o["status"] == "sent", f"期望sent，得{o['status']}"
    assert o["exchange_order_id"] is not None
    assert len(api.orders) == 1
    assert api.orders[0]["symbol"] == "BTCUSDT"
    print(f"✅ poll 挂单成功 orderId={o['exchange_order_id']}")

def test_poll_skips_sent():
    """status=sent 的订单不重复挂单"""
    _clean()
    _make_pending(status="sent")
    api = MockAPI()
    import ws_monitor as wm
    wm.poll_pending_orders(api)
    assert len(api.orders) == 0
    print("✅ sent 状态跳过，不重复挂单")

def test_poll_expires():
    """超时订单标记 expired"""
    _clean()
    _write_json(PENDING, {"orders": [{
        "id": "x", "symbol": "XUSDT", "direction": "LONG",
        "entry_price": 1.0, "notional": 100,
        "status": "pending",
        "placed_at": "2026-01-01T00:00:00Z",
        "expire_at": "2026-01-01T01:00:00Z",  # 已过期
        "exchange_order_id": None,
    }]})
    api = MockAPI()
    import ws_monitor as wm
    wm.poll_pending_orders(api)
    pending = json.loads(PENDING.read_text())
    assert pending["orders"][0]["status"] == "expired"
    assert len(api.orders) == 0
    print("✅ 超时订单标记 expired")

# ── 测试：on_open_filled ──────────────────────────────────────────────────────

def test_on_open_filled_happy_path():
    """正常成交：止损→gates→pos_state→止盈→日志 全部完成"""
    _clean()
    api = MockAPI()
    pending_order = {
        "signal_type": "trend", "score": 80,
        "score_details": {"ema": 25},
        "entry_mode": "LIMIT",
        "funding_rate": -0.001,
        "hard_sl": 63000.0,
        "tp1_price": 68000.0, "tp2_price": 71000.0,
        "notional": 500,
    }
    import ws_monitor as wm
    ok = wm.on_open_filled(
        "BTCUSDT", "LONG",
        fill_price=65100.0, fill_qty=0.008,
        order_id="ENT001", pending_order=pending_order, api=api
    )
    assert ok is True

    # pos_state 已写入
    pos = ps_get("BTCUSDT", "LONG")
    assert pos is not None, "pos_state 未写入"
    assert pos["sl_price"] == 63000.0
    assert pos["sl_order_id"] is not None
    assert pos["entry_price"] == 65100.0

    # gates 已注册
    gates = json.loads(GATES.read_text())
    assert "BTCUSDT_LONG" in gates

    # API 被调用（止损 + 止盈2笔 = 3次）
    order_types = [o.get("order_type") for o in api.orders]
    assert "STOP_MARKET" in order_types, f"止损单未挂: {order_types}"

    # 日志已记录
    assert LOG_FILE.exists()
    lines = LOG_FILE.read_text().strip().split("\n")
    rec = json.loads(lines[-1])
    assert rec["event"] == "open"
    assert rec["symbol"] == "BTCUSDT"
    print(f"✅ on_open_filled 完整流程 orders={len(api.orders)}")

def test_on_open_filled_sl_fail_emergency_close():
    """止损挂单全部失败 → 市价平仓"""
    _clean()

    # API 对 place_algo_order （止损单）固定失败
    class FailSLAPI(MockAPI):
        def place_algo_order(self, **kw):
            if kw.get("order_type") == "STOP_MARKET":
                raise RuntimeError("SL fail")
            return super().place_algo_order(**kw)

    api = FailSLAPI()
    import ws_monitor as wm
    ok = wm.on_open_filled(
        "ETHUSDT", "LONG", 3200.0, 0.1,
        "ENT002",
        {"hard_sl": 3000.0, "notional": 300,
         "signal_type": "trend", "score": 70,
         "score_details": {}, "entry_mode": "LIMIT",
         "funding_rate": 0.0, "tp1_price": 0, "tp2_price": 0},
        api=api
    )
    assert ok is False
    # 市价平仓单应该被挂
    market_orders = [o for o in api.orders if o.get("order_type") == "MARKET"]
    assert len(market_orders) == 1, f"期望1笔市价平仓，得{market_orders}"
    print("✅ 止损失败 → 市价平仓触发")

# ── 测试：gates ──────────────────────────────────────────────────────────────

def test_gates_breakeven():
    """浮盈 +3% 时止损移至保本"""
    _clean()
    api = MockAPI()
    import ws_monitor as wm

    # 先注册 gate
    wm._register_gates("BTCUSDT", "LONG", 65000.0, 63000.0, 68000.0)
    ps_write = __import__("modules.pos_state_store", fromlist=["write"]).write
    ps_write("BTCUSDT", "LONG", {
        "entry_price": 65000.0, "notional": 500,
        "sl_price": 63000.0, "sl_order_id": "SL_OLD"
    })
    api.open_orders["BTCUSDT"] = [{"orderId": "SL_OLD"}]

    # 价格 +3.5%
    wm.check_gates("BTCUSDT", current_price=65000*1.035, api=api)

    pos = ps_get("BTCUSDT", "LONG")
    assert pos["sl_price"] == 65000.0, f"止损应移至保本65000，实际{pos['sl_price']}"
    print(f"✅ gates breakeven 止损移至 {pos['sl_price']}")

# ── 测试：patrol ─────────────────────────────────────────────────────────────

def test_patrol_reattach_sl():
    """止损单丢失时自动补挂"""
    _clean()
    api = MockAPI()
    # api 里没有任何 open_order（模拟止损单丢失）
    import modules.pos_state_store as pss
    pss.write("SOLUSDT", "LONG", {
        "entry_price": 150.0, "notional": 300,
        "sl_price": 143.0, "sl_order_id": "SL_MISSING",
    })
    import ws_monitor as wm
    wm.patrol(api)

    # 应该补挂了一笔止损单
    stop_orders = [o for o in api.orders if o.get("order_type") == "STOP_MARKET"]
    assert len(stop_orders) == 1, f"期望补挂1笔止损，实际{stop_orders}"
    print(f"✅ patrol 补挂止损单 orderId={stop_orders[0]['orderId']}")

def test_patrol_no_action_when_sl_exists():
    """止损单存在时不重复挂单"""
    _clean()
    api = MockAPI()
    api.open_orders["SOLUSDT"] = [{"orderId": "SL_OK"}]
    import modules.pos_state_store as pss
    pss.write("SOLUSDT", "LONG", {
        "entry_price": 150.0, "notional": 300,
        "sl_price": 143.0, "sl_order_id": "SL_OK",
    })
    import ws_monitor as wm
    wm.patrol(api)
    assert len(api.orders) == 0   # 没有新挂单
    print("✅ patrol 止损存在时静默不挂")

if __name__ == "__main__":
    test_poll_places_order()
    test_poll_skips_sent()
    test_poll_expires()
    test_on_open_filled_happy_path()
    test_on_open_filled_sl_fail_emergency_close()
    test_gates_breakeven()
    test_patrol_reattach_sl()
    test_patrol_no_action_when_sl_exists()
    print("\n🎉 ws_monitor 全部测试通过")
