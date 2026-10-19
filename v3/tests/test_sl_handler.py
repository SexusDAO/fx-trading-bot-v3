"""tests/test_sl_handler.py — mock binance_api，不发真实请求"""
import sys, types, pytest
sys.path.insert(0, "/root/.openclaw/workspace/v3")

# ── mock binance_api（fixture 级别注入，测试后恢复）──────────────────────────

_call_log = []

def _make_mock_api():
    mock = types.ModuleType("binance_api")

    def _place_order(**kw):
        _call_log.append(kw)
        if kw.get("_fail"):
            raise RuntimeError("mock fail")
        return {"orderId": f"ORD_{len(_call_log)}", "status": "NEW"}

    def _place_algo_order(**kw):
        _call_log.append(kw)
        if kw.get("_fail"):
            raise RuntimeError("mock fail")
        oid = f"ALGO_{len(_call_log)}"
        return {"algoId": oid, "orderId": oid, "status": "NEW"}

    mock.place_order      = _place_order
    mock.place_algo_order = _place_algo_order
    return mock


@pytest.fixture(autouse=True)
def _inject_mock_api():
    """每个测试前注入假 binance_api，测试后恢复原模块"""
    _call_log.clear()
    old = sys.modules.get("binance_api")
    mock = _make_mock_api()
    sys.modules["binance_api"] = mock
    yield mock
    # 恢复
    if old is None:
        sys.modules.pop("binance_api", None)
    else:
        sys.modules["binance_api"] = old


from modules.sl_handler import place_sl, place_tp, calc_qty

# ── 测试 ──────────────────────────────────────────────────────────────────────

def test_place_sl_success(_inject_mock_api):
    mock = _inject_mock_api
    _call_log.clear()
    ok, order_id = place_sl("BTCUSDT", "LONG", sl_price=63000.0, qty=0.008)
    assert ok is True
    assert order_id.startswith("ALGO_")
    assert _call_log[-1]["side"] == "SELL"
    assert _call_log[-1]["trigger_price"] == 63000.0
    print(f"✅ place_sl 成功 orderId={order_id}")

def test_place_sl_short_side(_inject_mock_api):
    _call_log.clear()
    ok, order_id = place_sl("ETHUSDT", "SHORT", sl_price=3400.0, qty=0.15)
    assert ok is True
    assert _call_log[-1]["side"] == "BUY"
    print("✅ place_sl 空单方向正确 side=BUY")

def test_place_sl_retry_then_succeed(_inject_mock_api):
    """前2次失败，第3次成功"""
    _call_log.clear()
    mock = _inject_mock_api
    call_count = [0]
    original = mock.place_algo_order

    def flaky(**kwargs):
        call_count[0] += 1
        if call_count[0] <= 2:
            raise RuntimeError("mock fail")
        oid = "ALGO_OK"
        return {"algoId": oid, "orderId": oid, "status": "NEW"}

    mock.place_algo_order = flaky
    ok, order_id = place_sl("BTCUSDT", "LONG", 63000.0, 0.008, retries=3)
    mock.place_algo_order = original

    assert ok is True
    assert call_count[0] == 3
    print(f"✅ place_sl 重试成功（调用{call_count[0]}次）")

def test_place_sl_all_fail(_inject_mock_api):
    """全部失败，返回 False"""
    mock = _inject_mock_api
    original = mock.place_algo_order
    mock.place_algo_order = lambda **kw: (_ for _ in ()).throw(RuntimeError("always fail"))

    ok, order_id = place_sl("BTCUSDT", "LONG", 63000.0, 0.008, retries=3)
    mock.place_algo_order = original

    assert ok is False
    assert order_id is None
    print("✅ place_sl 全败返回 False")

def test_place_tp(_inject_mock_api):
    _call_log.clear()
    result = place_tp("BTCUSDT", "LONG",
                      tp1_price=68000.0, tp1_qty=0.005,
                      tp2_price=71000.0, tp2_qty=0.003)
    assert result["ok"] is True
    assert result["tp1_order_id"] is not None
    assert result["tp2_order_id"] is not None
    assert len(_call_log) == 2
    print(f"✅ place_tp TP1+TP2 orderId={result['tp1_order_id']},{result['tp2_order_id']}")

def test_calc_qty(_inject_mock_api):
    q = calc_qty(notional=500, entry_price=65000)
    assert abs(q - 0.0077) < 0.0001, f"期望~0.0077，实际{q}"
    print(f"✅ calc_qty 500U/65000 = {q}")

if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
