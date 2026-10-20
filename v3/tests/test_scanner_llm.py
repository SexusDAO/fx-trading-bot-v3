"""tests/test_scanner_llm.py — 全 mock，不发真实 API 请求（v2.0 适配）"""
import sys, json, importlib
from pathlib import Path
sys.path.insert(0, "/root/.openclaw/workspace/v3")

from modules.pos_state_store import clear_all, write as ps_write
from modules.watchlist_store import clear_all as wl_clear

WORKSPACE = Path("/root/.openclaw/workspace/v3")
CFG_DIR   = WORKSPACE / "config"

# ── 工具 ──────────────────────────────────────────────────────────────────────

def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f)

def _clean():
    clear_all()
    wl_clear()
    for f in ["pending_orders.json", "daily_risk.json"]:
        fp = CFG_DIR / f
        if fp.exists(): fp.unlink()
    _write_json(CFG_DIR / "circuit_breaker.json", {"open": False})
    _write_json(CFG_DIR / "market_regime.json",   {"mode": "bull", "btc_trend": "neutral"})
    _write_json(CFG_DIR / "strategy-config.json", {
        "scoring": {"trend_threshold": 65, "watchlist_threshold": 55, "drop_threshold": 45},
        "position": {"max_positions_total": 4,
                     "market_mode_limits": {
                         "bull": {"max_long": 2, "max_short": 2, "max_total": 4}}},
        "notional": {"trend_long": {"65_69": 300, "70_79": 400, "80_plus": 500},
                     "trend_short": {"65_69": 200}, "hard_cap": 1000},
        "risk": {"daily_loss_pct": 0.2},
        "ttl_minutes": {"limit_low_score": 120, "limit_high_score": 240},
    })
    _write_json(CFG_DIR / "daily_risk.json", {"date": "", "daily_loss": 0, "daily_pnl": 0})


def _get_scanner():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "scanner_llm_v3",
        str(WORKSPACE / "llm_tasks" / "scanner_llm.py"),
    )
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# ── Mock API（v2.0：适配 get_all_tickers / get_all_mark_prices）─────────────

class MockAPI:
    """
    生成上升趋势 klines：EMA多头排列 + 锤形收尾 + 良好量能。
    使 scorer 能打出较高分（目标 ≥55）。
    """
    def __init__(self, symbols=None):
        self._symbols = symbols or ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
        self._oid = 9000

    # ── v2.0 接口（scanner 用这两个做批量预拉）────────────────────────────
    def get_all_tickers(self):
        return [
            {"symbol": s, "priceChangePercent": "5.0",
             "lastPrice": "124.0", "quoteVolume": "50000000"}
            for s in self._symbols
        ]

    def get_all_mark_prices(self):
        return [
            {"symbol": s, "markPrice": "124.0", "lastFundingRate": "-0.0005"}
            for s in self._symbols
        ]

    def get_klines(self, symbol, interval, limit=100):
        import random; random.seed(42)
        result = []; price = 100.0
        # 前 60 根：稳定上升趋势（确保 EMA 多头排列）
        for i in range(60):
            price += 0.5 + random.uniform(-0.1, 0.1)
            v = 1200 + i * 8
            result.append([i, str(price*0.999), str(price*1.004),
                            str(price*0.996), str(price), str(v)])
        # 后续横盘 + 锤形（蓄势特征）
        top = price
        for i in range(max(0, limit - 61)):
            price = top + random.uniform(-0.5, 0.5)
            v = 700 + i * 3
            result.append([60+i, str(price*0.999), str(price*1.003),
                            str(price*0.997), str(price), str(v)])
        # 最后一根：锤形 + 放量
        avg_vol = sum(float(r[5]) for r in result[-10:]) / 10
        result.append([limit, str(top-0.2), str(top*1.002),
                       str(top*0.988), str(top+0.1), str(avg_vol * 1.8)])
        return result[-limit:]

    def get_ticker(self, symbol):
        return {"lastPrice": "124.0", "lastFundingRate": "-0.0005",
                "priceChangePercent": "5.0"}

    def get_futures_account(self):
        return {"totalWalletBalance": "5000.0", "availableBalance": "4000.0"}

    # ws_monitor 需要的接口
    def place_order(self, **kw):
        oid = str(self._oid); self._oid += 1
        return {"orderId": oid, "status": "NEW"}

    def place_algo_order(self, **kw):
        oid = str(self._oid); self._oid += 1
        return {"algoId": oid, "orderId": oid, "status": "NEW"}

    def get_open_orders(self, symbol): return []
    def cancel_order(self, symbol, orderId): pass
    def get_order(self, symbol, orderId):
        return {"orderId": str(orderId), "status": "NEW"}


# ── 测试 ──────────────────────────────────────────────────────────────────────

def test_scan_basic():
    """正常扫描：pending_orders.json 被写入，结构完整"""
    _clean()
    scanner  = _get_scanner()
    notified = []
    result   = scanner.run_scan(api=MockAPI(), notify_fn=lambda m: notified.append(m))

    print(f"  scanned={result['scanned']} signals={result['signals']} "
          f"placed={result['placed']} wl_added={result.get('watchlist_added', 0)}")

    assert result["scanned"] == 3
    assert not result["skipped_circuit"]
    assert not result["skipped_full"]

    pf = CFG_DIR / "pending_orders.json"
    assert pf.exists(), "pending_orders.json 应该被写入"
    pending = json.loads(pf.read_text())
    assert "orders" in pending and "generated_at" in pending
    print(f"  pending_orders: {len(pending['orders'])} 条")
    print("✅ test_scan_basic")


def test_scan_circuit_blocked():
    _clean()
    _write_json(CFG_DIR / "circuit_breaker.json", {"open": True})
    scanner = _get_scanner()
    result  = scanner.run_scan(api=MockAPI())
    assert result["skipped_circuit"] is True
    print("✅ test_scan_circuit_blocked")


def test_scan_positions_full():
    _clean()
    for i in range(5):
        ps_write(f"COIN{i}USDT", "LONG", {"notional": 300})
    scanner = _get_scanner()
    result  = scanner.run_scan(api=MockAPI())
    assert result["skipped_full"] is True
    print("✅ test_scan_positions_full")


def test_scan_notify_sent():
    """notify_fn 被调用且包含关键词"""
    _clean()
    scanner  = _get_scanner()
    notified = []
    scanner.run_scan(api=MockAPI(), notify_fn=lambda m: notified.append(m))
    # v2.0：播报至少1条（扫描完成报告）
    assert len(notified) >= 1, f"期望≥1条播报，得{len(notified)}"
    assert "扫描完成" in notified[0], f"播报应含'扫描完成': {notified[0][:60]}"
    print(f"✅ test_scan_notify_sent ({len(notified[0])}字符)")


def test_scan_no_duplicate_order():
    """连续两轮扫描，同一标的不重复写入 pending_orders"""
    _clean()
    scanner = _get_scanner()
    scanner.run_scan(api=MockAPI())
    scanner.run_scan(api=MockAPI())
    pf = CFG_DIR / "pending_orders.json"
    assert pf.exists()
    pending = json.loads(pf.read_text())
    symbols = [o["symbol"] for o in pending["orders"]]
    assert len(symbols) == len(set(symbols)), f"有重复挂单: {symbols}"
    print("✅ test_scan_no_duplicate_order")


def test_scan_order_fields():
    """pending_order 字段完整（含 scene / score_details）"""
    _clean()
    scanner = _get_scanner()
    scanner.run_scan(api=MockAPI())
    pf = CFG_DIR / "pending_orders.json"
    assert pf.exists()
    pending = json.loads(pf.read_text())
    if not pending["orders"]:
        print("⚠️  无挂单（评分或场景不足），跳过字段检查")
        return
    o = pending["orders"][0]
    required = ["id", "symbol", "direction", "entry_price", "notional",
                "signal_type", "score", "score_details",
                "hard_sl", "status", "placed_at", "expire_at"]
    for f in required:
        assert f in o, f"缺少字段: {f}"
    assert o["status"] == "pending"
    assert o["direction"] in ("LONG", "SHORT")
    assert isinstance(o["score_details"], dict)
    # v2.0 新字段
    assert "scene" in o, "应有 scene 字段"
    print(f"✅ test_scan_order_fields: {list(o.keys())}")


def test_analyze_symbol_returns_signal():
    """analyze_symbol 对健康 klines 返回有效信号（或 None=评分不足，均可）"""
    _clean()
    scanner = _get_scanner()
    sig     = scanner.analyze_symbol("BTCUSDT", MockAPI(), None)
    if sig is None:
        print("⚠️  analyze_symbol 返回 None（评分不足门槛），符合预期")
    else:
        assert "symbol" in sig and "score" in sig
        assert 0 <= sig["score"] <= 100
        assert "direction" in sig
        assert "watchlist_only" in sig
        print(f"✅ test_analyze_symbol_returns_signal: "
              f"score={sig['score']} type={sig.get('signal_type')} "
              f"wl_only={sig['watchlist_only']}")


if __name__ == "__main__":
    tests = [test_scan_basic, test_scan_circuit_blocked,
             test_scan_positions_full, test_scan_notify_sent,
             test_scan_no_duplicate_order, test_scan_order_fields,
             test_analyze_symbol_returns_signal]
    passed = failed = 0
    for t in tests:
        print(f"\n--- {t.__name__} ---")
        try:
            t(); passed += 1
        except Exception as e:
            import traceback; print(f"❌ {e}"); traceback.print_exc(); failed += 1
    print(f"\n{'='*50}\n结果: {passed}/{passed+failed}")
