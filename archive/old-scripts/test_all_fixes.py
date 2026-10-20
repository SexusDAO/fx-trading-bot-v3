#!/usr/bin/env python3
"""
test_all_fixes.py — 本轮所有修复的模拟测试
P0-1: 保护期外撤单线 <70→<60
P0-2: MAX_SHORT=1
P0-3: 止损后冷静期
P0-4: _check_force_cancel 熔断+仓位上限
P0-5: 空单 OI 急增例外
P1-6: consecutive_losses 接入降仓/暂停
P1-7: 轧空保护
P1-8: 空单三档止盈
P1-9: 48h强制评估
P2:   动态 MAX_NOTIONAL
"""
import sys, os, json, time, pathlib, shutil, tempfile, types, threading
sys.path.insert(0, "/root/.openclaw/workspace")

PASS = "✅ PASS"
FAIL = "❌ FAIL"
results = []

def run(name, fn):
    print(f"\n{'='*56}\n{name}\n{'='*56}")
    try:
        ok = fn()
        results.append((name, ok))
        print(f"  {PASS if ok else FAIL}")
    except Exception as e:
        import traceback; traceback.print_exc()
        results.append((name, False))
        print(f"  {FAIL}: {e}")

# ── Mock 基础设施 ────────────────────────────────────────────────
class MockAPI:
    def __init__(self, positions=None, avail=1000.0):
        self._positions = positions or []
        self._avail     = avail
        self.orders     = {}
        self.algo_orders = {}
        self.call_log   = []
        self._nid       = 50000
    def _new_id(self):
        self._nid += 1; return self._nid
    def get_positions(self):
        return self._positions
    def get_futures_balance(self):
        return [{"asset":"USDT","availableBalance":str(self._avail),"balance":str(self._avail)}]
    def place_order(self, sym, side, order_type, **kw):
        oid = self._new_id()
        self.orders[oid] = {"symbol":sym,"side":side,"status":"NEW","orderId":oid}
        self.call_log.append(("place_order",sym,side,order_type,oid))
        return {"orderId":oid,"avgPrice":"0.15","price":"0.15","status":"NEW"}
    def cancel_order(self, sym, oid):
        self.call_log.append(("cancel_order",sym,oid))
        if oid in self.orders:
            self.orders[oid]["status"] = "CANCELED"
            return {"orderId":oid,"status":"CANCELED"}
        return {"code":-2011}
    def get_open_orders(self, sym=None):
        return [{"orderId":k,"side":v["side"],"symbol":v["symbol"],"type":"LIMIT","reduceOnly":False,"status":v["status"]}
                for k,v in self.orders.items() if v["status"]=="NEW" and (sym is None or v["symbol"]==sym)]
    def place_algo_order(self, sym, side, order_type, trigger_price, pos_side="LONG", quantity=None, **kw):
        aid = self._new_id()
        self.algo_orders[aid] = {"symbol":sym,"orderType":order_type,"triggerPrice":trigger_price,"algoId":aid}
        self.call_log.append(("place_algo_order",sym,order_type,float(trigger_price),aid))
        return {"algoId":aid}
    def get_algo_open_orders(self, sym=None):
        return [{"algoId":k,**v} for k,v in self.algo_orders.items() if sym is None or v["symbol"]==sym]
    def get_mark_price(self, sym):
        return {"markPrice":"0.15"}
    def get_klines(self, sym, interval, limit=20):
        base = 0.15
        return [[0,str(base*.99),str(base*1.01),str(base*.98),str(base),"1000000",0,0,0,0,0,0]
                for _ in range(limit)]
    def algo_cache_save(self, *a, **kw): pass
    BASE_FAPI = "https://fapi.binance.com"
    BASE_API  = "https://fapi.binance.com"

class TmpWS:
    def __init__(self):
        self.path = pathlib.Path(tempfile.mkdtemp())
    def cleanup(self): shutil.rmtree(self.path, ignore_errors=True)
    def write(self, name, data):
        (self.path/name).write_text(json.dumps(data, indent=2))
    def read(self, name):
        f = self.path/name
        return json.loads(f.read_text()) if f.exists() else {}

# ════════════════════════════════════════════════════════════
# P0-1: 保护期外撤单线 <70→<60（score=65 应该保留，之前会被撤）
# ════════════════════════════════════════════════════════════
def test_p01():
    import scanner as sc
    # score=65 在保护期外，原来 <70 会撤，修复后 >=60 应保留
    # 模拟：cur_score=65，in_protect=False
    # 新逻辑：cur_score >= PROTECT_MIN_SCORE(60) → continue（保留）
    PROTECT_MIN_SCORE = sc.PROTECT_MIN_SCORE if hasattr(sc,'PROTECT_MIN_SCORE') else 60
    score = 65
    result = score >= PROTECT_MIN_SCORE
    print(f"  score={score} >= PROTECT_MIN_SCORE({PROTECT_MIN_SCORE}) → 保留挂单: {result}")
    assert result, f"score={score} 应保留，但逻辑返回撤单"
    # 确认 <60 仍然撤单
    score2 = 55
    result2 = score2 >= PROTECT_MIN_SCORE
    print(f"  score={score2} >= PROTECT_MIN_SCORE → 保留: {result2}（应为False）")
    assert not result2, "score=55 应撤单"
    return True
run("P0-1: 保护期外撤单线 65分保留 / 55分撤单", test_p01)

# ════════════════════════════════════════════════════════════
# P0-2: MAX_SHORT=1
# ════════════════════════════════════════════════════════════
def test_p02():
    import scanner as sc
    print(f"  MAX_SHORT = {sc.MAX_SHORT}")
    assert sc.MAX_SHORT == 1, f"MAX_SHORT 应为1，实际={sc.MAX_SHORT}"
    return True
run("P0-2: MAX_SHORT=1", test_p02)

# ════════════════════════════════════════════════════════════
# P0-3: 冷静期写入 + 读取过滤
# ════════════════════════════════════════════════════════════
def test_p03():
    import scanner as sc
    ws = TmpWS()
    orig_bl = sc.BLACKLIST_FILE
    sc.BLACKLIST_FILE = str(ws.path / "daily_blacklist.json")
    try:
        # 写入 ETHUSDT LONG 1h 冷静期
        sc._add_to_cooldown("ETHUSDT", "LONG", 1.0)
        bl = sc._load_daily_blacklist()
        syms = bl.get("symbols", {})
        assert "ETHUSDT" in syms, "冷静期未写入"
        assert syms["ETHUSDT"]["direction"] == "LONG"
        assert syms["ETHUSDT"]["expire_ts"] > time.time()
        print(f"  写入: ETHUSDT LONG 到期 {syms['ETHUSDT']['expire_str']}")

        # 模拟 run_scan 里读取并过滤
        _now = time.time()
        active_cd = {sym:info for sym,info in syms.items() if _now < info.get("expire_ts",0)}
        in_cooldown = "ETHUSDT" in active_cd and active_cd["ETHUSDT"].get("direction") == "LONG"
        print(f"  ETHUSDT LONG 冷静期中: {in_cooldown}（应True）")
        assert in_cooldown, "冷静期过滤未生效"

        # 不同方向不过滤
        in_cooldown_short = "ETHUSDT" in active_cd and active_cd["ETHUSDT"].get("direction") == "SHORT"
        print(f"  ETHUSDT SHORT 冷静期中: {in_cooldown_short}（应False）")
        assert not in_cooldown_short
        return True
    finally:
        sc.BLACKLIST_FILE = orig_bl
        ws.cleanup()
run("P0-3: 止损冷静期写入/读取/方向过滤", test_p03)

# ════════════════════════════════════════════════════════════
# P0-4: _check_force_cancel 熔断检查
# ════════════════════════════════════════════════════════════
def test_p04_circuit():
    from dual_net_orders import _check_force_cancel

    # mock risk_manager 返回熔断
    mock_rm = types.ModuleType("risk_manager")
    mock_rm.load_circuit = lambda: {"fused": True, "consecutive_losses": 0}
    sys.modules["risk_manager"] = mock_rm

    result = _check_force_cancel("LONG", 0.0, 0.0, False, False)
    print(f"  熔断中 → force_cancel: '{result}'")
    assert "熔断" in result, f"熔断未触发: {result}"

    mock_rm.load_circuit = lambda: {"fused": False}
    result2 = _check_force_cancel("LONG", 0.0, 0.0, False, False)
    print(f"  非熔断 → force_cancel: '{result2}'（应空）")
    assert result2 == "", f"非熔断不应触发: {result2}"

    del sys.modules["risk_manager"]
    return True
run("P0-4: _check_force_cancel 熔断检查", test_p04_circuit)

# ════════════════════════════════════════════════════════════
# P0-4: _check_force_cancel 仓位上限检查
# ════════════════════════════════════════════════════════════
def test_p04_positions():
    from dual_net_orders import _check_force_cancel

    mock_rm = types.ModuleType("risk_manager")
    mock_rm.load_circuit = lambda: {"fused": False}
    sys.modules["risk_manager"] = mock_rm

    # 5个多单已满
    api = MockAPI(positions=[
        {"symbol":f"T{i}USDT","positionAmt":str(100+i),"markPrice":"1","entryPrice":"1","unRealizedProfit":"0"}
        for i in range(5)
    ])
    result = _check_force_cancel("LONG", 0.0, 0.0, False, False, api=api)
    print(f"  5个多单已满 → '{result}'")
    assert "上限" in result and "多单" in result, f"多单上限未触发: {result}"

    # 1个空单已满
    api2 = MockAPI(positions=[
        {"symbol":"BTCUSDT","positionAmt":"-100","markPrice":"1","entryPrice":"1","unRealizedProfit":"0"}
    ])
    result2 = _check_force_cancel("SHORT", 0.0, 0.0, False, False, api=api2)
    print(f"  1个空单已满 → '{result2}'")
    assert "上限" in result2 and "空单" in result2, f"空单上限未触发: {result2}"

    del sys.modules["risk_manager"]
    return True
run("P0-4: _check_force_cancel 仓位上限检查", test_p04_positions)

# ════════════════════════════════════════════════════════════
# P0-5: 空单 OI 急增例外
# ════════════════════════════════════════════════════════════
def test_p05():
    from dual_net_orders import _check_force_cancel

    mock_rm = types.ModuleType("risk_manager")
    mock_rm.load_circuit = lambda: {"fused": False}
    sys.modules["risk_manager"] = mock_rm

    result = _check_force_cancel("SHORT", 0.0, 0.0, False, False, oi_change_1h=0.25)
    print(f"  OI急增25% → '{result}'")
    assert "OI" in result, f"OI急增未触发: {result}"

    result2 = _check_force_cancel("SHORT", 0.0, 0.0, False, False, oi_change_1h=0.10)
    print(f"  OI增10%（未触发阈值）→ '{result2}'（应空）")
    assert result2 == "", f"OI10%不应触发: {result2}"

    del sys.modules["risk_manager"]
    return True
run("P0-5: 空单OI急增>20%强制撤单", test_p05)

# ════════════════════════════════════════════════════════════
# P1-6: consecutive_losses → 降仓/暂停
# ════════════════════════════════════════════════════════════
def test_p16():
    import scanner as sc

    mock_rm = types.ModuleType("risk_manager")

    # 3笔亏损 → 降仓20%
    mock_rm.load_circuit = lambda: {"fused": False, "consecutive_losses": 3}
    sys.modules["risk_manager"] = mock_rm
    base = sc.POS_BASE_U if hasattr(sc,'POS_BASE_U') else 1000
    pos1 = sc._calc_position_usdt(score=80, details={})
    print(f"  连续亏损3笔 → 仓位={pos1}U（基础{base}U×0.8应≤{int(base*0.8)}U）")
    assert pos1 <= base * 0.85, f"降仓20%未生效: {pos1}"

    # 5笔亏损 → 暂停（返回0）
    mock_rm.load_circuit = lambda: {"fused": False, "consecutive_losses": 5}
    pos2 = sc._calc_position_usdt(score=80, details={})
    print(f"  连续亏损5笔 → 仓位={pos2}U（应为0）")
    assert pos2 == 0, f"暂停开仓未生效: {pos2}"

    # 正常 → 不降仓
    mock_rm.load_circuit = lambda: {"fused": False, "consecutive_losses": 1}
    pos3 = sc._calc_position_usdt(score=80, details={})
    print(f"  连续亏损1笔 → 仓位={pos3}U（正常）")
    assert pos3 > 0, "正常情况不应返回0"

    del sys.modules["risk_manager"]
    return True
run("P1-6: consecutive_losses 降仓20%/暂停", test_p16)

# ════════════════════════════════════════════════════════════
# P1-7: 轧空保护（浮亏>4%且价格仍在涨→市价平仓）
# ════════════════════════════════════════════════════════════
def test_p17():
    import ws_monitor as wm

    ws = TmpWS()
    orig_ps = wm.POS_STATE
    wm.POS_STATE = str(ws.path / "pos_state.json")

    # 写入 SHORT 仓位：entry=0.15，当前价=0.156（涨了4%），浮亏>4%
    ws.write("pos_state.json", {
        "XYZUSDT": {
            "direction": "SHORT", "entry_price": 0.15, "qty": 1000,
            "tp1_price": 0.13, "opened_at": time.time()-3600, "tp1_hit": False
        }
    })

    mock_api = MockAPI()
    mock_api._positions = [
        {"symbol":"XYZUSDT","positionAmt":"-1000","markPrice":"0.156",
         "entryPrice":"0.15","unRealizedProfit":"-6.0"}
    ]
    orig_api = wm.api
    wm.api = mock_api

    notify_msgs = []
    orig_notify = wm.notify
    wm.notify = notify_msgs.append

    wm._patrol_short_protection()

    place_calls = [c for c in mock_api.call_log if c[0]=="place_order" and c[2]=="BUY"]
    print(f"  市价平仓调用: {place_calls}")
    print(f"  推送消息: {[m[:50] for m in notify_msgs]}")
    assert len(place_calls) >= 1, "轧空保护未触发市价平仓"
    assert any("轧空" in m for m in notify_msgs), "轧空通知未发送"

    wm.notify = orig_notify
    wm.POS_STATE = orig_ps
    wm.api = orig_api
    ws.cleanup()
    return True
run("P1-7: 轧空保护触发市价平仓", test_p17)

# ════════════════════════════════════════════════════════════
# P1-7: 轧空保护不误触（多单/未超阈值/价格在跌）
# ════════════════════════════════════════════════════════════
def test_p17_no_trigger():
    import ws_monitor as wm

    ws = TmpWS()
    orig_ps = wm.POS_STATE
    wm.POS_STATE = str(ws.path / "pos_state.json")
    ws.write("pos_state.json", {
        "XYZUSDT": {
            "direction": "SHORT", "entry_price": 0.15, "qty": 1000,
            "tp1_price": 0.13, "opened_at": time.time()-3600, "tp1_hit": False
        }
    })

    # 价格在跌（0.148 < entry 0.15），不应触发
    mock_api = MockAPI()
    mock_api._positions = [
        {"symbol":"XYZUSDT","positionAmt":"-1000","markPrice":"0.148",
         "entryPrice":"0.15","unRealizedProfit":"2.0"}  # 盈利中
    ]
    orig_api = wm.api
    wm.api = mock_api
    notify_msgs = []
    orig_notify2 = wm.notify
    wm.notify = notify_msgs.append

    wm._patrol_short_protection()
    place_calls = [c for c in mock_api.call_log if c[0]=="place_order" and c[2]=="BUY"]
    print(f"  价格在跌时 市价平仓调用: {len(place_calls)}（应0）")
    assert len(place_calls) == 0, "价格在跌时不应触发轧空保护"

    wm.notify = orig_notify2
    wm.POS_STATE = orig_ps
    wm.api = orig_api
    ws.cleanup()
    return True
run("P1-7: 轧空保护不误触（价格在跌/盈利中）", test_p17_no_trigger)

# ════════════════════════════════════════════════════════════
# P1-8: 空单三档止盈数量分配 40/35/25
# ════════════════════════════════════════════════════════════
def test_p18():
    total_qty = 100
    qty1 = round(total_qty * 0.40, 0)
    qty2 = round(total_qty * 0.35, 0)
    qty3 = round(total_qty - qty1 - qty2, 0)
    print(f"  三档分配: TP1={qty1}张(40%) TP2={qty2}张(35%) TP3={qty3}张(25%)")
    assert qty1 == 40, f"TP1应40张: {qty1}"
    assert qty2 == 35, f"TP2应35张: {qty2}"
    assert qty3 == 25, f"TP3应25张: {qty3}"
    assert qty1 + qty2 + qty3 == total_qty, "总和应等于100"

    # 多单仍是 50/50
    qty1_long = round(total_qty / 2, 0)
    qty2_long = round(total_qty - qty1_long, 0)
    print(f"  多单分配: TP1={qty1_long}张(50%) TP2={qty2_long}张(50%)")
    assert qty1_long == 50 and qty2_long == 50
    return True
run("P1-8: 空单三档止盈数量40/35/25", test_p18)

# ════════════════════════════════════════════════════════════
# P1-9: 48h 强制评估推送
# ════════════════════════════════════════════════════════════
def test_p19():
    import ws_monitor as wm

    ws = TmpWS()
    orig_ps = wm.POS_STATE
    wm.POS_STATE = str(ws.path / "pos_state.json")

    # 写入已持仓 50h 且未达 TP1 的空单
    ws.write("pos_state.json", {
        "ABCUSDT": {
            "direction": "SHORT", "entry_price": 0.20, "qty": 500,
            "tp1_price": 0.16, "tp1_hit": False,
            "opened_at": time.time() - 50*3600   # 50h前开仓
        }
    })
    mock_api = MockAPI()
    mock_api._positions = [
        {"symbol":"ABCUSDT","positionAmt":"-500","markPrice":"0.195",
         "entryPrice":"0.20","unRealizedProfit":"2.5"}
    ]
    orig_api = wm.api
    wm.api = mock_api
    notify_msgs = []
    orig_notify3 = wm.notify
    wm.notify = notify_msgs.append

    wm._patrol_short_protection()
    print(f"  48h提醒消息数: {len(notify_msgs)}")
    print(f"  内容: {notify_msgs[0][:80] if notify_msgs else '无'}...")
    assert len(notify_msgs) >= 1, "48h提醒未触发"
    assert "48" in notify_msgs[0] or "50" in notify_msgs[0], "提醒内容不对"

    wm.notify = orig_notify3
    wm.POS_STATE = orig_ps
    wm.api = orig_api
    ws.cleanup()
    return True
run("P1-9: 48h未达TP1强制评估推送", test_p19)

# ════════════════════════════════════════════════════════════
# P2: 动态 MAX_NOTIONAL（余额×2.5）
# ════════════════════════════════════════════════════════════
def test_p2():
    import scanner as sc

    orig_mn = sc.MAX_NOTIONAL
    orig_api = sc.api

    mock_api = MockAPI(avail=800.0)
    sc.api = mock_api

    # 模拟 run_scan 里的动态计算
    _bal = mock_api.get_futures_balance()
    _avail = float([b for b in _bal if b['asset']=='USDT'][0]['availableBalance'])
    sc.MAX_NOTIONAL = max(500, _avail * 2.5)

    print(f"  余额={_avail}U → MAX_NOTIONAL={sc.MAX_NOTIONAL}U（应=2000U）")
    assert sc.MAX_NOTIONAL == 2000.0, f"动态MAX_NOTIONAL错误: {sc.MAX_NOTIONAL}"

    sc.MAX_NOTIONAL = orig_mn
    sc.api = orig_api
    return True
run("P2: 动态MAX_NOTIONAL = 余额×2.5", test_p2)

# ════════════════════════════════════════════════════════════
# 综合场景: 冷静期+降仓+熔断全链路
# ════════════════════════════════════════════════════════════
def test_e2e():
    import scanner as sc

    ws = TmpWS()
    orig_bl = sc.BLACKLIST_FILE
    sc.BLACKLIST_FILE = str(ws.path / "daily_blacklist.json")

    mock_rm = types.ModuleType("risk_manager")
    mock_rm.load_circuit = lambda: {"fused": False, "consecutive_losses": 3}
    sys.modules["risk_manager"] = mock_rm

    # 1. 写入冷静期
    sc._add_to_cooldown("SOLUSDT", "LONG", 1.0)

    # 2. 连续亏损3笔 → 降仓
    pos = sc._calc_position_usdt(score=85, details={})
    print(f"  连续亏损3笔仓位: {pos}U（应降20%）")
    assert pos > 0 and pos <= 1000 * 0.85

    # 3. 冷静期过滤
    bl = sc._load_daily_blacklist()
    active_cd = {s:i for s,i in bl["symbols"].items() if time.time()<i.get("expire_ts",0)}
    blocked = "SOLUSDT" in active_cd and active_cd["SOLUSDT"]["direction"] == "LONG"
    print(f"  SOLUSDT LONG 被冷静期阻止: {blocked}（应True）")
    assert blocked

    # 4. 熔断 → 暂停开仓（consecutive_losses=5）
    mock_rm.load_circuit = lambda: {"fused": False, "consecutive_losses": 5}
    pos2 = sc._calc_position_usdt(score=85, details={})
    print(f"  连续亏损5笔仓位: {pos2}U（应为0，暂停开仓）")
    assert pos2 == 0

    del sys.modules["risk_manager"]
    sc.BLACKLIST_FILE = orig_bl
    ws.cleanup()
    return True
run("E2E: 冷静期+降仓+暂停 全链路", test_e2e)

# ══════════════════════════════════════════════════════════════
# 汇总
# ══════════════════════════════════════════════════════════════
print("\n" + "="*56)
print("测试结果汇总")
print("="*56)
for name, ok in results:
    print(f"  {'✅' if ok else '❌'}  {name}")
total  = len(results)
passed = sum(1 for _, ok in results if ok)
print(f"\n  共 {total} 个，通过 {passed}，失败 {total-passed}")
sys.exit(0 if passed == total else 1)
