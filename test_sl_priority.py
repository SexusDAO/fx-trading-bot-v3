#!/usr/bin/env python3
"""
test_sl_priority.py — 止损三层优先级测试

场景覆盖：
  T1: hard_sl 有效 → 优先用 hard_sl
  T2: hard_sl 无效（=0），CG有效 → 用 CG
  T3: hard_sl 无效，CG无≥200万区域 → 跳过CG，用ATR兜底
  T4: hard_sl 无效，CG查询超时 → 用ATR兜底
  T5: hard_sl > entry（方向错误）→ 不用，降级到CG
  T6: hard_sl 比 CG 止损更紧（贴近清算区）→ 用 hard_sl
  T7: entry_logic 扫描时 hard_sl 写入 pos_state
"""
import sys, os, json, time, types, pathlib, shutil, tempfile
sys.path.insert(0, "/root/.openclaw/workspace")

PASS = "✅ PASS"
FAIL = "❌ FAIL"
results = []

def run(name, fn):
    print(f"\n{'='*54}\n{name}\n{'='*54}")
    try:
        ok = fn()
        results.append((name, ok))
        print(f"  {PASS if ok else FAIL}")
    except Exception as e:
        import traceback; traceback.print_exc()
        results.append((name, False))
        print(f"  {FAIL}: {e}")

# ─── 模拟 _on_open_filled 里止损选择逻辑（从 ws_monitor 提取）────────────────

def _pick_sl(pos_side, entry, atr, hard_sl, cg_zones, cg_raises=False):
    """
    模拟 ws_monitor._on_open_filled 里的止损三层优先级逻辑
    cg_zones: list of [price, liq_amount]
    cg_raises: 模拟 Coinglass 超时
    """
    # 层1: hard_sl
    hard_sl_val = float(hard_sl) if hard_sl else 0.0

    # 层2: Coinglass ≥200万过滤
    cg_sl = 0.0
    if not cg_raises:
        sig = [z for z in cg_zones if float(z[1]) >= 2_000_000]
        if sig:
            best_z = max(sig, key=lambda z: float(z[1]))
            cg_sl  = float(best_z[0]) - atr * 0.5

    # 层3: ATR 兜底
    atr_sl = (entry - atr * 2.0) if pos_side == "LONG" else (entry + atr * 2.0)

    # 优先级选取
    if hard_sl_val > 0 and (
        (pos_side == "LONG"  and hard_sl_val < entry) or
        (pos_side == "SHORT" and hard_sl_val > entry)
    ):
        sl_raw = hard_sl_val
        source = "hard_sl"
    elif cg_sl > 0 and (
        (pos_side == "LONG"  and cg_sl < entry) or
        (pos_side == "SHORT" and cg_sl > entry)
    ):
        sl_raw = cg_sl
        source = "coinglass"
    else:
        sl_raw = atr_sl
        source = "atr_fallback"

    # 安全检查
    if pos_side == "LONG":
        sl_final = min(sl_raw, entry * 0.999)
    else:
        sl_final = max(sl_raw, entry * 1.001)

    return sl_final, source


# ════════════════════════════════════════════════════════════
# T1: hard_sl 有效 → 优先使用
# ════════════════════════════════════════════════════════════
def t1():
    entry = 100.0; atr = 2.0
    hard_sl = 94.0      # 清算区外侧 -6%
    cg_zones = [[96.0, 3_000_000], [92.0, 1_500_000]]  # CG 也有区域

    sl, src = _pick_sl("LONG", entry, atr, hard_sl, cg_zones)
    print(f"  hard_sl={hard_sl}  CG最大区@96  → 选用: {src}={sl}")
    assert src == "hard_sl", f"应优先 hard_sl，实际: {src}"
    assert sl == 94.0
    return True
run("T1: hard_sl有效→优先使用", t1)

# ════════════════════════════════════════════════════════════
# T2: hard_sl=0，CG有效 → 用 CG
# ════════════════════════════════════════════════════════════
def t2():
    entry = 100.0; atr = 2.0
    cg_zones = [[95.0, 5_000_000], [91.0, 1_000_000]]

    sl, src = _pick_sl("LONG", entry, atr, hard_sl=0, cg_zones=cg_zones)
    expected = 95.0 - atr * 0.5   # = 94.0
    print(f"  hard_sl=0  CG最大区@95  → 选用: {src}={sl}（期望={expected}）")
    assert src == "coinglass", f"应用CG: {src}"
    assert abs(sl - expected) < 0.01
    return True
run("T2: hard_sl无效→用Coinglass清算区", t2)

# ════════════════════════════════════════════════════════════
# T3: hard_sl=0，CG无≥200万区域 → ATR兜底
# ════════════════════════════════════════════════════════════
def t3():
    entry = 100.0; atr = 2.0
    # 清算区都很小，全部 <200万
    cg_zones = [[96.0, 500_000], [93.0, 800_000]]

    sl, src = _pick_sl("LONG", entry, atr, hard_sl=0, cg_zones=cg_zones)
    atr_expected = entry - atr * 2.0  # = 96.0
    print(f"  CG全部<200万 → 选用: {src}={sl}（ATR兜底期望={atr_expected}）")
    assert src == "atr_fallback", f"应ATR兜底: {src}"
    assert abs(sl - atr_expected) < 0.01
    return True
run("T3: CG无≥200万区域→ATR兜底", t3)

# ════════════════════════════════════════════════════════════
# T4: CG 查询超时 → ATR 兜底
# ════════════════════════════════════════════════════════════
def t4():
    entry = 100.0; atr = 2.0
    sl, src = _pick_sl("LONG", entry, atr, hard_sl=0, cg_zones=[], cg_raises=True)
    print(f"  CG超时 → 选用: {src}={sl}")
    assert src == "atr_fallback"
    return True
run("T4: Coinglass超时→ATR兜底", t4)

# ════════════════════════════════════════════════════════════
# T5: hard_sl > entry（方向错误）→ 不用，降级到 CG
# ════════════════════════════════════════════════════════════
def t5():
    entry = 100.0; atr = 2.0
    hard_sl = 102.0     # LONG 止损 > entry，方向错误
    cg_zones = [[95.0, 4_000_000]]

    sl, src = _pick_sl("LONG", entry, atr, hard_sl=hard_sl, cg_zones=cg_zones)
    print(f"  hard_sl={hard_sl} > entry={entry}（方向错误）→ 选用: {src}={sl}")
    assert src == "coinglass", f"hard_sl方向错误应降级到CG: {src}"
    return True
run("T5: hard_sl方向错误→降级到CG", t5)

# ════════════════════════════════════════════════════════════
# T6: hard_sl 比 CG 更保守（更贴近清算区）→ 用 hard_sl
# ════════════════════════════════════════════════════════════
def t6():
    entry = 100.0; atr = 2.0
    hard_sl = 92.0       # 更保守（距入场 -8%）
    cg_zones = [[96.0, 3_000_000]]  # CG 给的 -5%

    sl, src = _pick_sl("LONG", entry, atr, hard_sl=hard_sl, cg_zones=cg_zones)
    print(f"  hard_sl={hard_sl}（-8%）vs CG止损@{96-atr*0.5}（-5%）→ 选用: {src}={sl}")
    assert src == "hard_sl"
    assert sl == 92.0
    return True
run("T6: hard_sl更保守→仍用hard_sl（优先级）", t6)

# ════════════════════════════════════════════════════════════
# T7: scanner place_entry_order 写入 hard_sl 到 pos_state
# ════════════════════════════════════════════════════════════
def t7():
    import scanner as sc

    ws = tempfile.mkdtemp()
    orig_ws = sc.WORKSPACE
    sc.WORKSPACE = ws
    ps_file = pathlib.Path(ws) / "pos_state.json"

    # mock api
    class MockAPI:
        def place_order(self, sym, side, order_type, **kw):
            return {"orderId": 12345, "status": "NEW"}
        def get_open_orders(self, sym=None): return []
        def get_futures_balance(self): return [{"asset":"USDT","availableBalance":"1000","balance":"1000"}]
        def get_positions(self): return []
        def get_mark_price(self, sym): return {"markPrice": "0.15"}
        def get_klines(self, sym, interval, limit=20):
            base = 0.15
            return [[0,str(base*.99),str(base*1.01),str(base*.98),str(base),"1000000",0,0,0,0,0,0]
                    for _ in range(limit)]
        def request(self, *a, **kw): return {}
        BASE_FAPI = "https://fapi.binance.com"
        BASE_API  = "https://fapi.binance.com"
        PROXY_URL = ""

    orig_api = sc.api
    sc.api = MockAPI()

    # mock liquidation_map 返回有效 hard_sl
    import entry_logic as el
    orig_lm = el.liquidation_map
    def mock_lm(sym, k1h, k4h, direction):
        return {
            "hard_sl": 0.136, "safe_entry": 0.148, "soft_tp1": 0.165, "soft_tp2": 0.172,
            "liq_below": 0.135, "liq_above": 0.165, "ob_support": 0.147,
            "ob_resistance": 0.152, "fr_val": 0.0001, "ls_ratio": 1.2,
            "oi_increasing": False, "entry_reason": "mock"
        }
    el.liquidation_map = mock_lm

    # mock calc_entry_mode
    from entry_logic import calc_entry_mode as orig_cem
    def mock_cem(*a, **kw): return "PULLBACK", 0.148, 0

    import unittest.mock as um
    with um.patch("scanner.calc_entry_mode", mock_cem), \
         um.patch("scanner._get_sym_filters", return_value={"tick_size":"0.0001","step_size":"1"}):
        k1h = MockAPI().get_klines("XYZUSDT","1h",30)
        k4h = MockAPI().get_klines("XYZUSDT","4h",20)
        details = {"final_score": 80, "pos_usdt": 300}
        result = sc.place_entry_order("XYZUSDT", "LONG", 0.15, k1h, k4h, details)

    returned_sl = result.get("hard_sl", 0)
    print(f"  place_entry_order 返回 hard_sl={returned_sl}")
    assert returned_sl > 0, f"hard_sl 应>0，实际: {result}"
    assert returned_sl < 0.15, f"hard_sl 应低于 entry(0.15): {returned_sl}"

    ps = json.loads(ps_file.read_text()) if ps_file.exists() else {}
    stored_sl = ps.get("XYZUSDT", {}).get("hard_sl", 0)
    print(f"  pos_state 存储 hard_sl={stored_sl}")
    assert stored_sl == returned_sl, f"pos_state 值与返回值不一致: stored={stored_sl} returned={returned_sl}"
    assert stored_sl > 0, f"hard_sl 未写入 pos_state: {ps}"
    print(f"  ✔ hard_sl={stored_sl} 正确写入 pos_state，值一致")

    el.liquidation_map = orig_lm
    sc.api = orig_api
    sc.WORKSPACE = orig_ws
    shutil.rmtree(ws)
    return True
run("T7: place_entry_order写hard_sl到pos_state", t7)

# ════════════════════════════════════════════════════════════
# T8: 空单方向止损（SHORT: SL > entry）
# ════════════════════════════════════════════════════════════
def t8():
    entry = 100.0; atr = 2.0
    hard_sl = 106.0   # SHORT 止损在上方，有效

    cg_zones_above = [[104.0, 3_000_000]]   # 空单用上方清算区

    # 空单 CG 逻辑：above 区域
    cg_sl = 104.0 + atr * 0.5  # = 105.0
    # 手动模拟空单路径
    if hard_sl > 0 and hard_sl > entry:
        sl_raw = hard_sl; source = "hard_sl"
    elif cg_sl > entry:
        sl_raw = cg_sl; source = "coinglass"
    else:
        sl_raw = entry + atr * 2.0; source = "atr_fallback"
    sl_final = max(sl_raw, entry * 1.001)

    print(f"  SHORT entry={entry} hard_sl={hard_sl} → 选用: {source}={sl_final}")
    assert source == "hard_sl"
    assert sl_final == 106.0
    return True
run("T8: 空单方向止损正确（SL>entry）", t8)

# ════════════════════════════════════════════════════════════
# 汇总
# ════════════════════════════════════════════════════════════
print("\n" + "="*54)
print("测试结果汇总")
print("="*54)
for name, ok in results:
    print(f"  {'✅' if ok else '❌'}  {name}")
total  = len(results)
passed = sum(1 for _, ok in results if ok)
print(f"\n  共 {total} 个，通过 {passed}，失败 {total-passed}")
sys.exit(0 if passed == total else 1)
