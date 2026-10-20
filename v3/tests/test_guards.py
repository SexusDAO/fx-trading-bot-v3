"""tests/test_guards.py"""
import sys, json, types
from pathlib import Path
sys.path.insert(0, "/root/.openclaw/workspace/v3")

from modules.pos_state_store import clear_all, write as ps_write

WORKSPACE = Path("/root/.openclaw/workspace/v3")
CFG_DIR   = WORKSPACE / "config"

def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f: json.dump(data, f)

def _read_json(path):
    with open(path) as f: return json.load(f)

# ── Mock API ──────────────────────────────────────────────────────────────────

class MockAPI:
    def __init__(self, has_sl=True):
        self._has_sl = has_sl
        self.placed  = []
        self._oid    = 9000

    def place_order(self, **kw):
        oid = str(self._oid); self._oid += 1
        self.placed.append({**kw, "orderId": oid})
        return {"orderId": oid, "status": "NEW"}

    def place_algo_order(self, **kw):
        """mock place_algo_order (用于 STOP_MARKET 止损单)"""
        oid = str(self._oid); self._oid += 1
        self.placed.append({**kw, "algoId": oid})
        return {"algoId": oid, "orderId": oid, "status": "NEW"}

    def get_open_orders(self, symbol):
        if self._has_sl:
            return [{"orderId": "SL_EXISTS"}]
        return []

# ══════════════════════════════════════════════════════════════
# patrol_sl_check
# ══════════════════════════════════════════════════════════════

def test_patrol_sl_no_positions():
    clear_all()
    from guards.patrol_sl_check import run
    r = run(api=MockAPI())
    assert r["checked"] == 0 and r["reattached"] == 0
    print("✅ patrol_sl 空仓情况 HEARTBEAT_OK")

def test_patrol_sl_exists():
    """止损单存在 → 无补挂"""
    clear_all()
    ps_write("BTCUSDT", "LONG", {
        "entry_price": 65000, "notional": 500,
        "sl_price": 63000, "sl_order_id": "SL_EXISTS",
    })
    from guards.patrol_sl_check import run
    r = run(api=MockAPI(has_sl=True))
    assert r["reattached"] == 0
    assert r["errors"] == 0
    print(f"✅ patrol_sl 止损存在 → 无操作 checked={r['checked']}")

def test_patrol_sl_missing_reattach():
    """止损单丢失 → 自动补挂"""
    clear_all()
    ps_write("ETHUSDT", "LONG", {
        "entry_price": 3200, "notional": 300,
        "sl_price": 3000, "sl_order_id": "SL_GONE",
    })
    api = MockAPI(has_sl=False)
    from guards.patrol_sl_check import run
    r = run(api=api)
    assert r["reattached"] == 1, f"期望补挂1次，得{r['reattached']}"
    assert r["errors"] == 0
    # pos_state 里 sl_order_id 已更新
    from modules.pos_state_store import get as ps_get
    pos = ps_get("ETHUSDT", "LONG")
    assert pos["sl_order_id"] != "SL_GONE", "sl_order_id 应被更新"
    print(f"✅ patrol_sl 补挂成功 new_id={pos['sl_order_id']}")

def test_patrol_sl_multiple_positions():
    """多仓位混合（1个正常 + 1个丢失）"""
    clear_all()
    ps_write("BTCUSDT", "LONG", {
        "entry_price": 65000, "notional": 500,
        "sl_price": 63000, "sl_order_id": "SL_OK",
    })
    ps_write("SOLUSDT", "SHORT", {
        "entry_price": 150, "notional": 300,
        "sl_price": 160, "sl_order_id": "SL_MISSING",
    })

    class MixedAPI(MockAPI):
        def get_open_orders(self, symbol):
            if symbol == "BTCUSDT":
                return [{"orderId": "SL_OK"}]
            return []

    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "patrol_sl_v3", str(WORKSPACE / "guards" / "patrol_sl_check.py"))
    g = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(g)

    api = MixedAPI()
    r = g.run(api=api)
    assert r["checked"] == 2
    assert r["reattached"] == 1
    print(f"✅ patrol_sl 混合 checked=2 reattached=1")


# ══════════════════════════════════════════════════════════════
# circuit_daily_reset
# ══════════════════════════════════════════════════════════════

def _load_circuit_guard():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "circuit_reset_v3", str(WORKSPACE / "guards" / "circuit_daily_reset.py"))
    g = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(g)
    return g

def test_circuit_reset_opens_closed():
    """重置后 open=False，daily_loss=0"""
    _write_json(CFG_DIR / "circuit_breaker.json", {"open": True, "daily_loss": 150.0})
    g = _load_circuit_guard()
    r = g.run()
    cb = _read_json(CFG_DIR / "circuit_breaker.json")
    assert cb["open"] is False
    assert cb["daily_loss"] == 0.0
    assert len(r["actions"]) == 3
    print(f"✅ circuit_reset 熔断关闭 actions={r['actions']}")

def test_circuit_reset_cancel_counter():
    _write_json(CFG_DIR / "daily_cancel_counter.json", {"count": 17})
    _load_circuit_guard().run()
    cc = _read_json(CFG_DIR / "daily_cancel_counter.json")
    assert cc["count"] == 0
    print("✅ circuit_reset 撤单计数清零")

def test_circuit_reset_daily_risk():
    _write_json(CFG_DIR / "daily_risk.json", {"daily_loss": 200.0, "daily_pnl": -200.0})
    _load_circuit_guard().run()
    dr = _read_json(CFG_DIR / "daily_risk.json")
    assert dr["daily_loss"] == 0.0
    assert dr["daily_pnl"]  == 0.0
    print("✅ circuit_reset 每日风控清零")

if __name__ == "__main__":
    test_patrol_sl_no_positions()
    test_patrol_sl_exists()
    test_patrol_sl_missing_reattach()
    test_patrol_sl_multiple_positions()
    test_circuit_reset_opens_closed()
    test_circuit_reset_cancel_counter()
    test_circuit_reset_daily_risk()
    print("\n🎉 guards 全部测试通过")
