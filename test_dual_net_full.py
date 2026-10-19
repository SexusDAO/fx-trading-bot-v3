#!/usr/bin/env python3
"""
test_dual_net_full.py — 双网系统完整场景模拟
=============================================================
覆盖所有主要路径：
  Scene-1:  正常A单成交 → 撤B单
  Scene-2:  正常B单突破 → 撤A单（含旧ID兼容）
  Scene-3:  scanner撤旧重挂 → sync_a_order_id → B单再触发，撤单用新ID
  Scene-4:  B单成交 pos_state为空 → _schedule_b_order_sl_tp 兜底
  Scene-5:  B单成交 pos_state已有 → 兜底跳过（不重复挂单）
  Scene-6:  双单同时成交 → on_both_filled 平B单
  Scene-7:  保护期内评分不撤单
  Scene-8:  保护期内评分 < 60 → 撤单
  Scene-9:  A单到期4h → patrol_pending 清理
  Scene-10: B单 RSI超买/量比不足 → 不触发
  Scene-11: _schedule_b_order_sl_tp 已有止损单 → 兜底跳过
  Scene-12: _b_sl_tp_fired 防重入
  Scene-13: Coinglass 超时 → ATR 兜底止损止盈
  Scene-14: pos_state 并发写安全（tmp+replace）
"""

import os, sys, json, time, copy, threading, tempfile, shutil, pathlib, types
sys.path.insert(0, "/root/.openclaw/workspace")

PASS = "✅ PASS"
FAIL = "❌ FAIL"
results = []

# ─────────────────────────── Mock 基础设施 ────────────────────────────────────

class MockAPI:
    def __init__(self):
        self.orders      = {}
        self.algo_orders = {}
        self.positions   = []
        self.mark_prices = {}
        self.call_log    = []
        self._next_id    = 20000

    def _new_id(self):
        self._next_id += 1
        return self._next_id

    def place_order(self, sym, side, order_type, quantity=None, price=None,
                    positionSide="LONG", timeInForce="GTC", **kw):
        oid = self._new_id()
        avg = float(price) if price else self.mark_prices.get(sym, 0.14)
        self.orders[oid] = {"symbol": sym, "side": side, "status": "NEW",
                            "price": avg, "orderId": oid, "reduceOnly": False}
        self.call_log.append(("place_order", sym, side, order_type, oid))
        return {"orderId": oid, "avgPrice": avg, "price": avg, "status": "NEW"}

    def cancel_order(self, sym, order_id):
        self.call_log.append(("cancel_order", sym, order_id))
        if order_id in self.orders:
            self.orders[order_id]["status"] = "CANCELED"
            return {"orderId": order_id, "status": "CANCELED"}
        return {"code": -2011, "msg": "Unknown order sent."}

    def get_open_orders(self, sym=None):
        result = [
            {**v, "orderId": k, "type": "LIMIT", "reduceOnly": v.get("reduceOnly", False)}
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
        self.call_log.append(("place_algo_order", sym, order_type, float(trigger_price), aid))
        return {"algoId": aid, "symbol": sym}

    def get_algo_open_orders(self, sym=None):
        return [
            {**v, "algoId": k}
            for k, v in self.algo_orders.items()
            if sym is None or v["symbol"] == sym
        ]

    def algo_cache_save(self, *a, **kw): pass

    def get_mark_price(self, sym):
        return {"markPrice": str(self.mark_prices.get(sym, 0.14))}

    def get_klines(self, sym, interval, limit=20):
        base = self.mark_prices.get(sym, 0.14)
        return [
            [0, str(base*0.99), str(base*1.01), str(base*0.98), str(base), "1000000",0,0,0,0,0,0]
            for _ in range(limit)
        ]

    def get_positions(self):
        return self.positions

    def request(self, *a, **kw): return {}
    BASE_FAPI = "https://fapi.binance.com"
    BASE_API  = "https://fapi.binance.com"

    def cancel_calls(self):
        return [c for c in self.call_log if c[0] == "cancel_order"]

    def algo_types(self, sym=None):
        return [v["orderType"] for v in self.algo_orders.values()
                if sym is None or v["symbol"] == sym]


class TmpWS:
    def __init__(self):
        self.path = pathlib.Path(tempfile.mkdtemp())
        (self.path / "scanner.log").touch()

    def pending_file(self):  return self.path / "pending_orders.json"
    def pos_state_file(self): return self.path / "pos_state.json"
    def load_pending(self):
        f = self.pending_file()
        return json.loads(f.read_text()) if f.exists() else {}
    def load_pos_state(self):
        f = self.pos_state_file()
        return json.loads(f.read_text()) if f.exists() else {}
    def cleanup(self): shutil.rmtree(self.path, ignore_errors=True)


def _make_entry(a_id, b_status="watching", score=85, direction="LONG",
                b_avg_price=0.0, b_qty=1700.0, expire_offset=14400):
    now = time.time()
    return {
        "sym": "UBUSDT", "direction": direction, "score": score, "tp": 0.160,
        "a_order": {"order_id": a_id, "price": 0.1378, "qty": 2322.0,
                    "status": "open", "placed_at": now},
        "b_order": {"trigger_price": 0.145, "qty": b_qty, "sl": 0.137,
                    "status": b_status, "placed_at": None, "order_id": None,
                    "avg_price": b_avg_price, "signal_valid_until": None},
        "meta": {"status": "active", "placed_at": now,
                 "expire_at": now + expire_offset,
                 "protect_end": now + 7200,
                 "score_history": [{"ts": now, "score": score}],
                 "adjust_count_h": 0, "adjust_hour": 0, "last_adjust_ts": 0},
    }


def run_test(name, fn):
    print(f"\n{'='*58}\n{name}\n{'='*58}")
    try:
        ok = fn()
        results.append((name, ok))
        print(f"  {PASS if ok else FAIL}")
    except Exception as e:
        import traceback; traceback.print_exc()
        results.append((name, False))
        print(f"  {FAIL}: {e}")


def with_dn(fn):
    """装饰器：给测试函数注入独立 workspace 和 dn 模块状态"""
    import dual_net_orders as dn
    ws = TmpWS()
    orig = (dn.PENDING_FILE, dn.LOG_FILE, dn.WORKSPACE)
    dn.PENDING_FILE = ws.pending_file()
    dn.LOG_FILE     = ws.path / "scanner.log"
    dn.WORKSPACE    = ws.path
    (dn.LOG_FILE).touch()
    # 清空防重入集合
    with dn._lock:
        dn._b_sl_tp_fired.clear()
    try:
        return fn(dn, ws)
    finally:
        dn.PENDING_FILE, dn.LOG_FILE, dn.WORKSPACE = orig
        ws.cleanup()


def inject_cg(liq_zones=None):
    """注入 mock coinglass_api"""
    if liq_zones is None:
        liq_zones = {
            "above": [[0.162, 6_000_000], [0.172, 3_000_000]],
            "below": [[0.128, 4_500_000], [0.118, 2_000_000]],
        }
    mock_cg = types.ModuleType("coinglass_api")
    mock_cg.get_liq_zones = lambda *a, **kw: liq_zones
    sys.modules["coinglass_api"] = mock_cg
    return mock_cg


def remove_cg():
    sys.modules.pop("coinglass_api", None)


# ═══════════════════════════════════════════════════════════════════
# Scene-1: A单正常成交 → 自动撤B单（B状态=watching，无需API）
# ═══════════════════════════════════════════════════════════════════
def scene1():
    def _run(dn, ws):
        api = MockAPI()
        entry = _make_entry(a_id=1001)
        ws.pending_file().write_text(json.dumps({"UBUSDT": entry}))
        msgs = []
        dn.on_order_filled("UBUSDT", "A", 1001, api, msgs.append)
        p = ws.load_pending()
        status = p["UBUSDT"]["meta"]["status"]
        b_status = p["UBUSDT"]["b_order"]["status"]
        print(f"  meta.status={status}  b_order.status={b_status}")
        assert status == "a_filled", f"meta状态错误: {status}"
        assert b_status == "cancelled", f"B单未标记cancelled: {b_status}"
        # B单为watching → 不调 cancel_order
        cancel_calls = api.cancel_calls()
        print(f"  cancel_order调用次数: {len(cancel_calls)}")
        assert len(cancel_calls) == 0, "watching状态不应调用cancel_order"
        return True
    return with_dn(_run)
run_test("Scene-1: A单成交→B单(watching)自动撤", scene1)


# ═══════════════════════════════════════════════════════════════════
# Scene-2: B单突破成交 → 撤A单（A单在交易所上存在）
# ═══════════════════════════════════════════════════════════════════
def scene2():
    def _run(dn, ws):
        api = MockAPI()
        api.mark_prices["UBUSDT"] = 0.146
        entry = _make_entry(a_id=2001)
        entry["b_order"]["avg_price"] = 0.146
        entry["b_order"]["qty"]       = 1700.0
        ws.pending_file().write_text(json.dumps({"UBUSDT": entry}))
        # A单实际挂在交易所
        api.orders[2001] = {"symbol":"UBUSDT","side":"BUY","status":"NEW","price":0.1378,"orderId":2001,"reduceOnly":False}
        msgs = []
        dn.on_order_filled("UBUSDT", "B", 99, api, msgs.append)
        time.sleep(0.3)
        p = ws.load_pending()
        a_status = p["UBUSDT"]["a_order"]["status"]
        print(f"  a_order.status={a_status}")
        assert a_status == "cancelled", f"A单未撤: {a_status}"
        assert api.orders[2001]["status"] == "CANCELED"
        return True
    return with_dn(_run)
run_test("Scene-2: B单成交→A单正常撤销", scene2)


# ═══════════════════════════════════════════════════════════════════
# Scene-3: scanner撤旧重挂 → sync_a_order_id → B单触发用新ID撤A单
# ═══════════════════════════════════════════════════════════════════
def scene3():
    def _run(dn, ws):
        api = MockAPI()
        api.mark_prices["UBUSDT"] = 0.146
        # pending 里是旧ID 3001
        entry = _make_entry(a_id=3001)
        ws.pending_file().write_text(json.dumps({"UBUSDT": entry}))
        # scanner 撤旧重挂，新ID 3999
        api.orders[3999] = {"symbol":"UBUSDT","side":"BUY","status":"NEW","price":0.1380,"orderId":3999,"reduceOnly":False}
        dn.sync_a_order_id("UBUSDT", 3999, 0.1380)
        # 确认pending已更新
        p = ws.load_pending()
        assert p["UBUSDT"]["a_order"]["order_id"] == 3999
        # B单突破
        msgs = []
        dn.on_order_filled("UBUSDT", "B", 99, api, msgs.append)
        time.sleep(0.3)
        cancel_ids = [c[2] for c in api.cancel_calls()]
        print(f"  撤单IDs={cancel_ids}")
        assert 3999 in cancel_ids, f"应撤新ID 3999，实际: {cancel_ids}"
        assert 3001 not in cancel_ids, f"旧ID 3001 不应出现: {cancel_ids}"
        return True
    return with_dn(_run)
run_test("Scene-3: sync_a_order_id后B单触发用新ID撤A单", scene3)


# ═══════════════════════════════════════════════════════════════════
# Scene-4: B单成交 pos_state为空 → 兜底补止损止盈
# ═══════════════════════════════════════════════════════════════════
def scene4():
    def _run(dn, ws):
        inject_cg()
        api = MockAPI()
        api.mark_prices["UBUSDT"] = 0.146
        # pos_state 不存在
        msgs = []
        dn._schedule_b_order_sl_tp("UBUSDT", 0.146, 1700.0, "LONG", api, msgs.append)
        at = dn.algo_types if hasattr(dn, 'algo_types') else None
        types_list = api.algo_types("UBUSDT")
        print(f"  algo_types={types_list}")
        assert any("STOP" in t for t in types_list),        "缺止损"
        assert any("TAKE_PROFIT" in t for t in types_list), "缺止盈"
        assert len([t for t in types_list if "TAKE_PROFIT" in t]) == 2, "需要TP1+TP2"
        # pos_state 写入 tp1
        ps = ws.load_pos_state()
        assert ps.get("UBUSDT", {}).get("tp1_price", 0) > 0
        remove_cg()
        return True
    return with_dn(_run)
run_test("Scene-4: pos_state为空→兜底补止损止盈", scene4)


# ═══════════════════════════════════════════════════════════════════
# Scene-5: B单成交 但止损止盈已存在 → 兜底跳过
# ═══════════════════════════════════════════════════════════════════
def scene5():
    def _run(dn, ws):
        inject_cg()
        api = MockAPI()
        api.mark_prices["UBUSDT"] = 0.146
        # 预先挂上止损和止盈
        api.algo_orders[5001] = {"symbol":"UBUSDT","orderType":"STOP_MARKET","triggerPrice":0.128,"algoId":5001}
        api.algo_orders[5002] = {"symbol":"UBUSDT","orderType":"TAKE_PROFIT_MARKET","triggerPrice":0.162,"algoId":5002}
        msgs = []
        dn._schedule_b_order_sl_tp("UBUSDT", 0.146, 1700.0, "LONG", api, msgs.append)
        new_algo = [k for k in api.algo_orders if k > 5002]
        print(f"  兜底后新增algo单数量: {len(new_algo)}")
        assert len(new_algo) == 0, f"已有止损止盈时不应新增: {new_algo}"
        remove_cg()
        return True
    return with_dn(_run)
run_test("Scene-5: 止损止盈已存在→兜底跳过", scene5)


# ═══════════════════════════════════════════════════════════════════
# Scene-6: 双单同时成交 → on_both_filled 平B单仓位
# ═══════════════════════════════════════════════════════════════════
def scene6():
    def _run(dn, ws):
        api = MockAPI()
        api.mark_prices["UBUSDT"] = 0.146
        api.positions = [{"symbol":"UBUSDT","positionAmt":"1700","positionSide":"LONG",
                          "entryPrice":"0.146","markPrice":"0.146","unRealizedProfit":"0"}]
        msgs = []
        dn.on_both_filled("UBUSDT", api, msgs.append)
        place_calls = [c for c in api.call_log if c[0]=="place_order" and c[3]=="MARKET"]
        print(f"  市价平仓调用: {place_calls}")
        assert len(place_calls) == 1, f"应有一次市价平仓: {place_calls}"
        assert any("双网异常" in m or "紧急" in m for m in msgs), f"未发推送: {msgs}"
        return True
    return with_dn(_run)
run_test("Scene-6: 双单同时成交→紧急平B单", scene6)


# ═══════════════════════════════════════════════════════════════════
# Scene-7: 保护期内评分60~79 → 不撤单
# ═══════════════════════════════════════════════════════════════════
def scene7():
    def _run(dn, ws):
        api = MockAPI()
        api.mark_prices["UBUSDT"] = 0.146
        api.orders[7001] = {"symbol":"UBUSDT","side":"BUY","status":"NEW","price":0.1378,"orderId":7001,"reduceOnly":False}
        entry = _make_entry(a_id=7001, score=85)
        # 保护期还剩1小时
        entry["meta"]["protect_end"] = time.time() + 3600
        ws.pending_file().write_text(json.dumps({"UBUSDT": entry}))
        msgs = []
        result = dn.check_protection(
            sym="UBUSDT", new_score=65,
            btc_drop_15m=0.0, funding_rate=0.0001,
            has_reversal=False, support_broken=False,
            new_support=0.137, api=api, notify_fn=msgs.append
        )
        print(f"  check_protection 返回: {result}")
        # 保护期内 score=65 >= SCORE_CANCEL(60) → 续期或保持
        assert result in ("protected", "extended", "adjusted"), \
            f"保护期内不应撤单，实际: {result}"
        return True
    return with_dn(_run)
run_test("Scene-7: 保护期内score=65→不撤单", scene7)


# ═══════════════════════════════════════════════════════════════════
# Scene-8: 保护期内评分 < 60 → 强制撤单
# ═══════════════════════════════════════════════════════════════════
def scene8():
    def _run(dn, ws):
        api = MockAPI()
        api.mark_prices["UBUSDT"] = 0.146
        api.orders[8001] = {"symbol":"UBUSDT","side":"BUY","status":"NEW","price":0.1378,"orderId":8001,"reduceOnly":False}
        entry = _make_entry(a_id=8001, score=85)
        entry["meta"]["protect_end"] = time.time() + 3600  # 保护期内
        ws.pending_file().write_text(json.dumps({"UBUSDT": entry}))
        msgs = []
        result = dn.check_protection(
            sym="UBUSDT", new_score=45,  # < 60
            btc_drop_15m=0.0, funding_rate=0.0001,
            has_reversal=False, support_broken=False,
            new_support=0.137, api=api, notify_fn=msgs.append
        )
        print(f"  check_protection 返回: {result}")
        assert result == "cancelled", f"score<60应撤单，实际: {result}"
        # A单应被撤
        cancel_ids = [c[2] for c in api.cancel_calls()]
        assert 8001 in cancel_ids, f"A单8001应被撤: {cancel_ids}"
        return True
    return with_dn(_run)
run_test("Scene-8: 保护期内score<60→强制撤单", scene8)


# ═══════════════════════════════════════════════════════════════════
# Scene-9: A单超过4小时到期 → patrol_pending 清理
# ═══════════════════════════════════════════════════════════════════
def scene9():
    def _run(dn, ws):
        api = MockAPI()
        api.orders[9001] = {"symbol":"UBUSDT","side":"BUY","status":"NEW","price":0.1378,"orderId":9001,"reduceOnly":False}
        entry = _make_entry(a_id=9001, expire_offset=-10)  # 已过期
        ws.pending_file().write_text(json.dumps({"UBUSDT": entry}))
        msgs = []
        dn.patrol_pending(api, msgs.append)
        p = ws.load_pending()
        print(f"  patrol后 UBUSDT 是否还在pending: {'UBUSDT' in p}")
        assert "UBUSDT" not in p, "超期条目应被清除"
        cancel_ids = [c[2] for c in api.cancel_calls()]
        assert 9001 in cancel_ids, f"到期应撤A单: {cancel_ids}"
        return True
    return with_dn(_run)
run_test("Scene-9: A单4h到期→patrol_pending清理", scene9)


# ═══════════════════════════════════════════════════════════════════
# Scene-10: B单 RSI超买 / 量比不足 → 不触发
# ═══════════════════════════════════════════════════════════════════
def scene10():
    def _run(dn, ws):
        api = MockAPI()
        api.mark_prices["UBUSDT"] = 0.146
        entry = _make_entry(a_id=10001)
        ws.pending_file().write_text(json.dumps({"UBUSDT": entry}))
        msgs = []

        # 量比不足
        r1 = dn.check_breakout("UBUSDT", 0.146, vol_ratio=1.2, rsi_1h=60.0,
                                kline_close=0.146, api=api, notify_fn=msgs.append)
        # RSI超买
        r2 = dn.check_breakout("UBUSDT", 0.146, vol_ratio=2.5, rsi_1h=80.0,
                                kline_close=0.146, api=api, notify_fn=msgs.append)
        # 价格未突破
        r3 = dn.check_breakout("UBUSDT", 0.143, vol_ratio=2.5, rsi_1h=60.0,
                                kline_close=0.143, api=api, notify_fn=msgs.append)
        print(f"  量比不足={r1} RSI超买={r2} 价格未破={r3}")
        assert not r1 and not r2 and not r3
        place_calls = [c for c in api.call_log if c[0]=="place_order"]
        assert len(place_calls) == 0, "不应下单"
        return True
    return with_dn(_run)
run_test("Scene-10: B单RSI超买/量比不足→不触发", scene10)


# ═══════════════════════════════════════════════════════════════════
# Scene-11: _schedule_b_order_sl_tp 止损单已存在 → 只补止盈，不补止损
# ═══════════════════════════════════════════════════════════════════
def scene11():
    def _run(dn, ws):
        inject_cg()
        api = MockAPI()
        api.mark_prices["UBUSDT"] = 0.146
        # 已有止损，但无止盈
        api.algo_orders[11001] = {"symbol":"UBUSDT","orderType":"STOP_MARKET","triggerPrice":0.128,"algoId":11001}
        msgs = []
        dn._schedule_b_order_sl_tp("UBUSDT", 0.146, 1700.0, "LONG", api, msgs.append)
        new_algo = {k: v for k,v in api.algo_orders.items() if k > 11001}
        new_types = [v["orderType"] for v in new_algo.values()]
        print(f"  新增algo类型: {new_types}")
        assert not any("STOP" in t for t in new_types), "已有止损不应重复挂"
        assert any("TAKE_PROFIT" in t for t in new_types), "缺止盈需补挂"
        remove_cg()
        return True
    return with_dn(_run)
run_test("Scene-11: 已有止损→只补止盈", scene11)


# ═══════════════════════════════════════════════════════════════════
# Scene-12: _b_sl_tp_fired 防重入 — 同一sym只触发一次
# ═══════════════════════════════════════════════════════════════════
def scene12():
    def _run(dn, ws):
        inject_cg()
        api = MockAPI()
        api.mark_prices["UBUSDT"] = 0.146
        entry = _make_entry(a_id=12001)
        entry["b_order"]["avg_price"] = 0.146
        entry["b_order"]["qty"]       = 1700.0
        ws.pending_file().write_text(json.dumps({"UBUSDT": entry}))

        call_count = [0]
        orig_fn = dn._schedule_b_order_sl_tp
        def counting_fn(*a, **kw):
            call_count[0] += 1
            orig_fn(*a, **kw)
        dn._schedule_b_order_sl_tp = counting_fn

        msgs = []
        # 模拟两条路径同时触发 on_order_filled(B)
        t1 = threading.Thread(target=dn.on_order_filled,
                              args=("UBUSDT","B",99,api,msgs.append), daemon=True)
        t2 = threading.Thread(target=dn.on_order_filled,
                              args=("UBUSDT","B",99,api,msgs.append), daemon=True)
        t1.start(); t2.start()
        t1.join(2); t2.join(2)
        time.sleep(0.5)

        print(f"  _schedule_b_order_sl_tp 实际调用次数: {call_count[0]}")
        assert call_count[0] <= 1, f"防重入失效，调用了{call_count[0]}次"

        dn._schedule_b_order_sl_tp = orig_fn
        remove_cg()
        return True
    return with_dn(_run)
run_test("Scene-12: _b_sl_tp_fired防重入", scene12)


# ═══════════════════════════════════════════════════════════════════
# Scene-13: Coinglass 超时 → ATR 兜底止损止盈
# ═══════════════════════════════════════════════════════════════════
def scene13():
    def _run(dn, ws):
        # 注入超时的 coinglass
        mock_cg = types.ModuleType("coinglass_api")
        def _timeout_liq(*a, **kw):
            raise TimeoutError("Coinglass API timeout (simulated)")
        mock_cg.get_liq_zones = _timeout_liq
        sys.modules["coinglass_api"] = mock_cg

        api = MockAPI()
        api.mark_prices["UBUSDT"] = 0.146
        msgs = []
        dn._schedule_b_order_sl_tp("UBUSDT", 0.146, 1700.0, "LONG", api, msgs.append)
        types_list = api.algo_types("UBUSDT")
        print(f"  Coinglass超时后algo类型: {types_list}")
        assert any("STOP" in t for t in types_list),        "ATR兜底应有止损"
        assert any("TAKE_PROFIT" in t for t in types_list), "ATR兜底应有止盈"
        # ATR兜底止损应在入场价 * 0.95
        sl_vals = [v["triggerPrice"] for v in api.algo_orders.values() if "STOP" in v["orderType"]]
        print(f"  ATR兜底止损价: {sl_vals}")
        assert all(v < 0.146 for v in sl_vals), "多单止损应低于入场价"
        remove_cg()
        return True
    return with_dn(_run)
run_test("Scene-13: Coinglass超时→ATR兜底止损止盈", scene13)


# ═══════════════════════════════════════════════════════════════════
# Scene-14: pos_state 原子写安全（tmp+replace 不留 .tmp 残留）
# ═══════════════════════════════════════════════════════════════════
def scene14():
    def _run(dn, ws):
        api = MockAPI()
        api.mark_prices["UBUSDT"] = 0.146

        # 并发写10次 pos_state
        errors = []
        def _write(i):
            try:
                dn._write_pos_state_for_b_order(
                    "UBUSDT", "LONG", 0.146 + i*0.0001, 1700.0 + i, {}
                )
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=_write, args=(i,)) for i in range(10)]
        for t in threads: t.start()
        for t in threads: t.join(3)

        print(f"  并发写错误数: {len(errors)}")
        if errors: print(f"  错误: {errors[:3]}")

        # .tmp 不应残留
        tmp_file = ws.path / "pos_state.tmp"
        assert not tmp_file.exists(), ".tmp 残留文件存在"

        # pos_state 是合法 JSON
        ps = ws.load_pos_state()
        assert "UBUSDT" in ps, "pos_state应有UBUSDT"
        assert ps["UBUSDT"]["direction"] == "LONG"

        assert len(errors) == 0, f"并发写有异常: {errors}"
        return True
    return with_dn(_run)
run_test("Scene-14: pos_state并发写安全", scene14)


# ═══════════════════════════════════════════════════════════════════
# Scene-15: B单突破但 pending 状态不是 active → 不触发
# ═══════════════════════════════════════════════════════════════════
def scene15():
    def _run(dn, ws):
        api = MockAPI()
        api.mark_prices["UBUSDT"] = 0.146
        entry = _make_entry(a_id=15001)
        entry["meta"]["status"] = "b_filled"   # 已成交，不应再触发
        ws.pending_file().write_text(json.dumps({"UBUSDT": entry}))
        msgs = []
        r = dn.check_breakout("UBUSDT", 0.146, 2.5, 60.0, 0.146, api, msgs.append)
        print(f"  check_breakout返回: {r}")
        assert not r, "b_filled状态不应触发"
        place_calls = [c for c in api.call_log if c[0]=="place_order"]
        assert len(place_calls) == 0
        return True
    return with_dn(_run)
run_test("Scene-15: b_filled状态→check_breakout不触发", scene15)


# ═══════════════════════════════════════════════════════════════════
# Scene-16: B单触发后 pos_state entry_price 正确写入 avg_price
# ═══════════════════════════════════════════════════════════════════
def scene16():
    def _run(dn, ws):
        api = MockAPI()
        # mock 让 place_order 返回特定 avgPrice
        orig_place = api.place_order
        def _place_with_avg(sym, side, order_type, **kw):
            r = orig_place(sym, side, order_type, **kw)
            r["avgPrice"] = "0.14650"
            return r
        api.place_order = _place_with_avg
        api.mark_prices["UBUSDT"] = 0.1465

        entry = _make_entry(a_id=16001)
        ws.pending_file().write_text(json.dumps({"UBUSDT": entry}))
        msgs = []
        dn.check_breakout("UBUSDT", 0.1465, 2.5, 60.0, 0.1468, api, msgs.append)
        time.sleep(0.3)

        ps = ws.load_pos_state()
        ep = ps.get("UBUSDT", {}).get("entry_price", 0)
        print(f"  pos_state.entry_price = {ep}")
        assert ep > 0, f"entry_price未写入: {ps.get('UBUSDT')}"
        assert abs(ep - 0.14650) < 0.001, f"entry_price应为成交均价: {ep}"
        return True
    return with_dn(_run)
run_test("Scene-16: B单成交均价正确写入pos_state", scene16)


# ═══════════════════════════════════════════════════════════════════
# 汇总
# ═══════════════════════════════════════════════════════════════════
print("\n" + "="*58)
print("完整模拟测试汇总")
print("="*58)
for name, ok in results:
    print(f"  {'✅' if ok else '❌'}  {name}")
total = len(results)
passed = sum(1 for _, ok in results if ok)
failed = total - passed
print(f"\n  共 {total} 个场景，通过 {passed}，失败 {failed}")

if failed > 0:
    print("\n失败场景：")
    for name, ok in results:
        if not ok:
            print(f"  ❌  {name}")

sys.exit(0 if failed == 0 else 1)
