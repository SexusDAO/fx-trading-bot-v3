#!/usr/bin/env python3
"""
test_dual_net_fixes.py — 三个修复路径的模拟测试
========================================================
Bug-1: scanner 撤旧重挂后未同步 pending_orders，B单成交时互斥撤单用过期 orderId
Bug-2: B单成交后 _on_open_filled 因 pos_state 为空无法建止盈止损
Bug-3: check_breakout 里 B单成交后 pos_state 里无 entry_price/qty/direction

每个 Test 场景：BEFORE（复现 Bug）→ AFTER（验证修复）
"""

import os, sys, json, time, copy, threading, tempfile, shutil, pathlib
sys.path.insert(0, "/root/.openclaw/workspace")

# ─────────────────────────── Mock API ────────────────────────────────────────
class MockAPI:
    def __init__(self):
        self.orders      = {}   # orderId -> status ("NEW"/"CANCELED"/"FILLED")
        self.algo_orders = {}   # algoId  -> orderType
        self.positions   = []
        self.mark_prices = {}
        self.klines_data = {}
        self.call_log    = []
        self._next_id    = 10000

    def _new_id(self):
        self._next_id += 1
        return self._next_id

    def place_order(self, sym, side, order_type, quantity=None, price=None,
                    positionSide="LONG", timeInForce="GTC", **kw):
        oid = self._new_id()
        avg = float(price) if price else self.mark_prices.get(sym, 0.14)
        self.orders[oid] = {"symbol": sym, "side": side, "status": "NEW",
                            "price": avg, "orderId": oid}
        self.call_log.append(("place_order", sym, side, order_type, oid))
        return {"orderId": oid, "avgPrice": avg, "price": avg, "status": "NEW"}

    def cancel_order(self, sym, order_id):
        self.call_log.append(("cancel_order", sym, order_id))
        if order_id in self.orders:
            self.orders[order_id]["status"] = "CANCELED"
            return {"orderId": order_id, "status": "CANCELED"}
        # 过期 ID → 返回错误码 -2011
        return {"code": -2011, "msg": "Unknown order sent."}

    def get_open_orders(self, sym=None):
        result = [
            {**v, "orderId": k, "type": "LIMIT",
             "reduceOnly": False, "side": v["side"]}
            for k, v in self.orders.items()
            if v["status"] == "NEW" and (sym is None or v["symbol"] == sym)
        ]
        self.call_log.append(("get_open_orders", sym, len(result)))
        return result

    def place_algo_order(self, sym, side, order_type, trigger_price,
                         pos_side="LONG", quantity=None, **kw):
        aid = self._new_id()
        self.algo_orders[aid] = {"symbol": sym, "orderType": order_type,
                                 "triggerPrice": trigger_price, "algoId": aid}
        self.call_log.append(("place_algo_order", sym, order_type, trigger_price, aid))
        return {"algoId": aid, "symbol": sym}

    def get_algo_open_orders(self, sym=None):
        result = [
            {**v, "algoId": k}
            for k, v in self.algo_orders.items()
            if sym is None or v["symbol"] == sym
        ]
        return result

    def algo_cache_save(self, sym, order_type, algo_id, price):
        pass  # 不需要在测试中实现

    def get_mark_price(self, sym):
        return {"markPrice": str(self.mark_prices.get(sym, 0.14))}

    def get_klines(self, sym, interval, limit=20):
        """返回伪造的 K 线数据"""
        base = self.mark_prices.get(sym, 0.14)
        return [
            [0, str(base*0.99), str(base*1.01), str(base*0.98), str(base), "1000000", 0, 0, 0, 0, 0, 0]
            for _ in range(limit)
        ]

    def get_positions(self):
        return self.positions

    def request(self, method, url, params=None):
        return {}

    BASE_FAPI = "https://fapi.binance.com"
    BASE_API  = "https://fapi.binance.com"

    def calls_of(self, action):
        return [c for c in self.call_log if c[0] == action]


# ─────────────────────────── 测试基础设施 ────────────────────────────────────
class TmpWorkspace:
    """每个测试用独立临时目录，避免互相污染"""
    def __init__(self):
        self.path = pathlib.Path(tempfile.mkdtemp())
        (self.path / "scanner.log").touch()

    def pending_file(self):
        return self.path / "pending_orders.json"

    def pos_state_file(self):
        return self.path / "pos_state.json"

    def load_pending(self):
        f = self.pending_file()
        return json.loads(f.read_text()) if f.exists() else {}

    def load_pos_state(self):
        f = self.pos_state_file()
        return json.loads(f.read_text()) if f.exists() else {}

    def cleanup(self):
        shutil.rmtree(self.path, ignore_errors=True)


def _make_entry(a_order_id, b_status="watching"):
    """构造一个标准 pending_orders entry"""
    return {
        "sym": "UBUSDT",
        "direction": "LONG",
        "score": 85,
        "tp": 0.160,
        "a_order": {
            "order_id": a_order_id,
            "price":    0.1378,
            "qty":      2322.0,
            "status":   "open",
            "placed_at": time.time(),
        },
        "b_order": {
            "trigger_price": 0.145,
            "qty":           1700.0,
            "sl":            0.137,
            "status":        b_status,
            "placed_at":     None,
            "order_id":      None,
            "avg_price":     0.0,
        },
        "meta": {
            "status":       "active",
            "placed_at":    time.time(),
            "expire_at":    time.time() + 14400,
            "protect_end":  time.time() + 7200,
            "score_history": [],
            "adjust_count_h": 0,
            "adjust_hour":  0,
            "last_adjust_ts": 0,
        }
    }


PASS = "✅ PASS"
FAIL = "❌ FAIL"


# ═══════════════════════════════════════════════════════════════════════════
# Test-1: Fix-1 — sync_a_order_id 同步新 orderId
# ═══════════════════════════════════════════════════════════════════════════
def test_fix1_sync_a_order_id():
    print("\n" + "="*60)
    print("Test-1: sync_a_order_id 同步新 orderId")
    print("="*60)

    import dual_net_orders as dn

    ws = TmpWorkspace()
    # 临时覆盖模块级路径
    orig_pending = dn.PENDING_FILE
    orig_log     = dn.LOG_FILE
    dn.PENDING_FILE = ws.pending_file()
    dn.LOG_FILE     = ws.path / "scanner.log"
    (dn.LOG_FILE).touch()

    try:
        # 初始 A单 orderId = 99001（旧 ID）
        entry = _make_entry(a_order_id=99001)
        ws.pending_file().write_text(json.dumps({"UBUSDT": entry}, indent=2))

        # BEFORE: 直接查 pending，仍是旧 ID
        p0 = ws.load_pending()
        old_id = p0["UBUSDT"]["a_order"]["order_id"]
        print(f"  BEFORE: a_order.order_id = {old_id}")
        assert old_id == 99001, f"前置条件不符: {old_id}"

        # 模拟 scanner 撤旧重挂，新 orderId = 99999
        dn.sync_a_order_id("UBUSDT", new_order_id=99999, new_price=0.1380)

        # AFTER: pending 里应已更新
        p1 = ws.load_pending()
        new_id = p1["UBUSDT"]["a_order"]["order_id"]
        new_px = p1["UBUSDT"]["a_order"]["price"]
        print(f"  AFTER:  a_order.order_id = {new_id}, price = {new_px}")

        assert new_id == 99999, f"ID 未更新: {new_id}"
        assert new_px == 0.1380, f"price 未更新: {new_px}"
        print(f"  {PASS}")
        return True
    except AssertionError as e:
        print(f"  {FAIL}: {e}")
        return False
    finally:
        dn.PENDING_FILE = orig_pending
        dn.LOG_FILE     = orig_log
        ws.cleanup()


# ═══════════════════════════════════════════════════════════════════════════
# Test-2: Fix-1 — on_order_filled 互斥撤单用实时挂单 ID（而非 pending 里过期 ID）
# ═══════════════════════════════════════════════════════════════════════════
def test_fix1_mutual_cancel_uses_live_id():
    print("\n" + "="*60)
    print("Test-2: 互斥撤单用实时 orderId（防过期 ID 失败）")
    print("="*60)

    import dual_net_orders as dn

    ws = TmpWorkspace()
    orig_pending = dn.PENDING_FILE
    orig_log     = dn.LOG_FILE
    dn.PENDING_FILE = ws.pending_file()
    dn.LOG_FILE     = ws.path / "scanner.log"
    (dn.LOG_FILE).touch()

    try:
        api = MockAPI()
        api.mark_prices["UBUSDT"] = 0.146

        # pending 里存的是过期 ID = 88001（已被 scanner 撤掉）
        entry = _make_entry(a_order_id=88001)
        entry["b_order"]["avg_price"] = 0.146
        entry["b_order"]["qty"]       = 1700.0
        ws.pending_file().write_text(json.dumps({"UBUSDT": entry}, indent=2))

        # 实际交易所上挂的是新 A单 orderId = 88999
        api.orders[88999] = {"symbol": "UBUSDT", "side": "BUY",
                              "status": "NEW", "price": 0.1380, "orderId": 88999}

        notify_msgs = []

        # BEFORE-SCENARIO: 如果用 pending 里的 88001 去撤，会返回 -2011 → cancelled=False
        result_before = api.cancel_order("UBUSDT", 88001)
        before_ok = result_before.get("orderId") is not None
        print(f"  BEFORE（用过期ID 88001）: cancelled={before_ok}  ← 撤单失败")
        assert not before_ok, "前置条件：过期ID应撤单失败"

        # AFTER-SCENARIO: on_order_filled 会先查实时挂单，用 88999 撤单
        dn.on_order_filled("UBUSDT", "B", 99998, api, notify_msgs.append)
        time.sleep(0.2)  # 等待线程

        cancel_calls = [c for c in api.call_log if c[0] == "cancel_order"]
        used_ids = [c[2] for c in cancel_calls]
        print(f"  AFTER（实时查询）: cancel_order 使用的 ID = {used_ids}")

        # 88999 应该被撤销
        assert 88999 in used_ids or api.orders.get(88999, {}).get("status") == "CANCELED", \
            f"新 ID 88999 未被撤单，call_log={cancel_calls}"

        # 88001（过期ID）应该不会被使用（或被视为成功）
        p_after = ws.load_pending()
        a_status = p_after["UBUSDT"]["a_order"]["status"]
        print(f"  A单最终状态: {a_status}")
        assert a_status in ("cancelled", "cancel_failed"), f"状态异常: {a_status}"

        print(f"  {PASS}")
        return True
    except AssertionError as e:
        print(f"  {FAIL}: {e}")
        return False
    finally:
        dn.PENDING_FILE = orig_pending
        dn.LOG_FILE     = orig_log
        ws.cleanup()


# ═══════════════════════════════════════════════════════════════════════════
# Test-3: Fix-3 — check_breakout 后立刻写 pos_state
# ═══════════════════════════════════════════════════════════════════════════
def test_fix3_pos_state_written_after_breakout():
    print("\n" + "="*60)
    print("Test-3: B单成交后立刻写 pos_state.json")
    print("="*60)

    import dual_net_orders as dn

    ws = TmpWorkspace()
    orig_pending  = dn.PENDING_FILE
    orig_log      = dn.LOG_FILE
    orig_workspace = dn.WORKSPACE
    dn.PENDING_FILE = ws.pending_file()
    dn.LOG_FILE     = ws.path / "scanner.log"
    dn.WORKSPACE    = ws.path
    (dn.LOG_FILE).touch()

    try:
        api = MockAPI()
        api.mark_prices["UBUSDT"] = 0.146

        # 部署一个 B单监控中的 entry
        entry = _make_entry(a_order_id=77001)
        entry["b_order"]["trigger_price"] = 0.145
        entry["b_order"]["status"]        = "watching"
        ws.pending_file().write_text(json.dumps({"UBUSDT": entry}, indent=2))

        # BEFORE: pos_state 为空
        ps_before = ws.load_pos_state()
        assert "UBUSDT" not in ps_before, "前置条件：pos_state 应为空"
        print(f"  BEFORE: pos_state['UBUSDT'] = {ps_before.get('UBUSDT', 'null')}")

        notify_msgs = []
        # 触发突破（价格>trigger，量比>1.5，RSI<75）
        triggered = dn.check_breakout(
            sym="UBUSDT", price=0.146, vol_ratio=2.0, rsi_1h=60.0,
            kline_close=0.1463, api=api, notify_fn=notify_msgs.append
        )
        time.sleep(0.3)  # 等线程

        print(f"  check_breakout 触发: {triggered}")
        assert triggered, "B单应被触发"

        # AFTER: pos_state 应有 UBUSDT 的 entry_price/qty/direction
        ps_after = ws.load_pos_state()
        ub = ps_after.get("UBUSDT", {})
        print(f"  AFTER: pos_state['UBUSDT'] = {json.dumps(ub, ensure_ascii=False)}")

        assert ub.get("entry_price", 0) > 0,    f"entry_price 未写入: {ub}"
        assert ub.get("qty",         0) > 0,    f"qty 未写入: {ub}"
        assert ub.get("direction")  == "LONG",  f"direction 错误: {ub}"
        assert ub.get("source")     == "dual_net_b", f"source 错误: {ub}"

        print(f"  {PASS}")
        return True
    except AssertionError as e:
        print(f"  {FAIL}: {e}")
        return False
    finally:
        dn.PENDING_FILE = orig_pending
        dn.LOG_FILE     = orig_log
        dn.WORKSPACE    = orig_workspace
        ws.cleanup()


# ═══════════════════════════════════════════════════════════════════════════
# Test-4: Fix-2 — B单成交后兜底止盈止损补挂
# ═══════════════════════════════════════════════════════════════════════════
def test_fix2_b_order_sl_tp_fallback():
    print("\n" + "="*60)
    print("Test-4: B单成交后兜底止盈止损 _schedule_b_order_sl_tp")
    print("="*60)

    import dual_net_orders as dn

    ws = TmpWorkspace()
    orig_workspace = dn.WORKSPACE
    orig_log       = dn.LOG_FILE
    dn.WORKSPACE   = ws.path
    dn.LOG_FILE    = ws.path / "scanner.log"
    (dn.LOG_FILE).touch()

    # 注入 mock coinglass_api
    import types, sys as _sys
    mock_cg = types.ModuleType("coinglass_api")
    mock_cg.get_liq_zones = lambda sym, mark, **kw: {
        "above": [[0.160, 5_000_000], [0.170, 3_000_000]],
        "below": [[0.130, 4_000_000], [0.120, 2_000_000]],
    }
    _sys.modules["coinglass_api"] = mock_cg

    try:
        api = MockAPI()
        api.mark_prices["UBUSDT"] = 0.146
        notify_msgs = []

        # 模拟：刚成交，还没有任何 algo 单（BEFORE 状态）
        assert len(api.algo_orders) == 0, "前置条件：无 algo 单"
        print(f"  BEFORE: algo_orders = {api.algo_orders}")

        # 直接调用兜底函数（绕过 5s sleep）
        dn._schedule_b_order_sl_tp(
            sym="UBUSDT", avg_price=0.146, qty=1700.0,
            direction="LONG", api=api, notify_fn=notify_msgs.append
        )

        # AFTER: 应有 STOP_MARKET + TAKE_PROFIT_MARKET
        algo_types = [v["orderType"] for v in api.algo_orders.values()]
        print(f"  AFTER: algo_orders types = {algo_types}")
        print(f"  通知: {notify_msgs[-1][:80] if notify_msgs else '无'}...")

        assert any("STOP_MARKET" in t for t in algo_types),        "缺少止损单 STOP_MARKET"
        assert any("TAKE_PROFIT_MARKET" in t for t in algo_types), "缺少止盈单 TAKE_PROFIT_MARKET"
        assert len([t for t in algo_types if "TAKE_PROFIT" in t]) == 2, \
            f"应有 TP1+TP2 两个止盈单，实际: {algo_types}"

        # pos_state 应已写入 tp1/tp2
        ps = ws.load_pos_state()
        ub = ps.get("UBUSDT", {})
        print(f"  pos_state 止盈: tp1={ub.get('tp1_price')} tp2={ub.get('tp2_price')}")
        assert ub.get("tp1_price", 0) > 0, "tp1_price 未写入 pos_state"

        print(f"  {PASS}")
        return True
    except AssertionError as e:
        print(f"  {FAIL}: {e}")
        return False
    finally:
        dn.WORKSPACE = orig_workspace
        dn.LOG_FILE  = orig_log
        ws.cleanup()
        if "coinglass_api" in _sys.modules:
            del _sys.modules["coinglass_api"]


# ═══════════════════════════════════════════════════════════════════════════
# Test-5: 端对端集成 — 完整 B单突破 + 互斥撤单 + pos_state + 兜底 SL/TP
# ═══════════════════════════════════════════════════════════════════════════
def test_e2e_b_breakout_full_flow():
    print("\n" + "="*60)
    print("Test-5: E2E — B单突破完整流程（模拟 UB 事故场景）")
    print("="*60)

    import dual_net_orders as dn
    import types, sys as _sys

    ws = TmpWorkspace()
    orig_pending   = dn.PENDING_FILE
    orig_log       = dn.LOG_FILE
    orig_workspace = dn.WORKSPACE
    dn.PENDING_FILE = ws.pending_file()
    dn.LOG_FILE     = ws.path / "scanner.log"
    dn.WORKSPACE    = ws.path
    (dn.LOG_FILE).touch()

    mock_cg = types.ModuleType("coinglass_api")
    mock_cg.get_liq_zones = lambda sym, mark, **kw: {
        "above": [[0.162, 6_000_000], [0.172, 3_000_000]],
        "below": [[0.128, 4_500_000], [0.118, 2_000_000]],
    }
    _sys.modules["coinglass_api"] = mock_cg

    try:
        api = MockAPI()
        api.mark_prices["UBUSDT"] = 0.146

        # Step-1: 部署双网（A单已挂 orderId=77001，但 scanner 已撤旧重挂成 88888）
        entry = _make_entry(a_order_id=77001)  # ← 过期 ID（Bug 复现前提）
        entry["b_order"]["trigger_price"] = 0.145
        ws.pending_file().write_text(json.dumps({"UBUSDT": entry}, indent=2))

        # Step-2: 模拟 scanner 撤旧重挂后调用 sync_a_order_id（Fix-1）
        api.orders[88888] = {"symbol": "UBUSDT", "side": "BUY",
                              "status": "NEW", "price": 0.1380, "orderId": 88888}
        dn.sync_a_order_id("UBUSDT", new_order_id=88888, new_price=0.1380)
        p_synced = ws.load_pending()
        assert p_synced["UBUSDT"]["a_order"]["order_id"] == 88888, "sync_a_order_id 失败"
        print(f"  Step-1&2: A单ID已同步为 88888 ✓")

        # Step-3: B单突破触发
        notify_msgs = []
        triggered = dn.check_breakout(
            sym="UBUSDT", price=0.146, vol_ratio=2.2, rsi_1h=62.0,
            kline_close=0.1463, api=api, notify_fn=notify_msgs.append
        )
        time.sleep(0.5)  # 等待异步线程

        assert triggered, "B单应被触发"
        print(f"  Step-3: B单触发成功 ✓")

        # Step-4: 验证 A单被正确撤销（用新 ID 88888）
        cancel_calls = [c for c in api.call_log if c[0] == "cancel_order"]
        cancelled_ids = [c[2] for c in cancel_calls]
        print(f"  Step-4: cancel_order 使用的 ID = {cancelled_ids}")
        assert 88888 in cancelled_ids, f"A单 88888 未被撤单，实际撤单 ID: {cancelled_ids}"
        assert 77001 not in cancelled_ids, f"过期 ID 77001 不应被使用"
        print(f"  Step-4: A单撤单使用了正确 ID（88888），过期ID(77001)未使用 ✓")

        # Step-5: 验证 pos_state 已写入（Fix-3）
        ps = ws.load_pos_state()
        ub = ps.get("UBUSDT", {})
        assert ub.get("entry_price", 0) > 0, f"pos_state entry_price 未写入: {ub}"
        print(f"  Step-5: pos_state 已写入 entry_price={ub['entry_price']} ✓")

        # Step-6: 验证兜底止盈止损（Fix-2）
        # 直接调用（跳过 5s sleep），确保结果可验证
        dn._schedule_b_order_sl_tp(
            sym="UBUSDT", avg_price=0.146, qty=1700.0,
            direction="LONG", api=api, notify_fn=notify_msgs.append
        )
        time.sleep(0.3)
        algo_types = [v["orderType"] for v in api.algo_orders.values()]
        has_sl = any("STOP" in t for t in algo_types)
        has_tp = any("TAKE_PROFIT" in t for t in algo_types)
        tp_count = len([t for t in algo_types if "TAKE_PROFIT" in t])
        print(f"  Step-6: algo_orders = {algo_types}")
        assert has_sl,        "止损单缺失"
        assert has_tp,        "止盈单缺失"
        assert tp_count == 2, f"应有 TP1+TP2，实际: {tp_count}"
        print(f"  Step-6: SL + TP1 + TP2 均已挂出 ✓")

        print(f"\n  {PASS}  端对端测试通过！")
        return True

    except AssertionError as e:
        print(f"  {FAIL}: {e}")
        import traceback; traceback.print_exc()
        return False
    finally:
        dn.PENDING_FILE = orig_pending
        dn.LOG_FILE     = orig_log
        dn.WORKSPACE    = orig_workspace
        ws.cleanup()
        if "coinglass_api" in _sys.modules:
            del _sys.modules["coinglass_api"]


# ─────────────────────────── 运行所有测试 ────────────────────────────────────
if __name__ == "__main__":
    results = []
    for test_fn in [
        test_fix1_sync_a_order_id,
        test_fix1_mutual_cancel_uses_live_id,
        test_fix3_pos_state_written_after_breakout,
        test_fix2_b_order_sl_tp_fallback,
        test_e2e_b_breakout_full_flow,
    ]:
        try:
            results.append((test_fn.__name__, test_fn()))
        except Exception as e:
            import traceback
            print(f"  CRASH in {test_fn.__name__}: {e}")
            traceback.print_exc()
            results.append((test_fn.__name__, False))

    print("\n" + "="*60)
    print("测试结果汇总")
    print("="*60)
    for name, ok in results:
        status = "✅ PASS" if ok else "❌ FAIL"
        print(f"  {status}  {name}")
    total = len(results)
    passed = sum(1 for _, ok in results if ok)
    print(f"\n  共 {total} 个测试，通过 {passed}，失败 {total-passed}")
    sys.exit(0 if passed == total else 1)
