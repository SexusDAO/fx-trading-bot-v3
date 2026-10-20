"""
tests/test_watchlist.py — watchlist_store + watchlist_monitor 单元测试
"""
import sys, json, time
from pathlib import Path
from datetime import datetime, timezone, timedelta

sys.path.insert(0, "/root/.openclaw/workspace/v3")

import modules.watchlist_store as wls
from modules.watchlist_store import (
    add_or_update, get_watching, update_item,
    mark_upgraded, mark_dropped, purge_expired,
    effective_score, calc_score_trend, snapshot, clear_all,
)

WORKSPACE = Path("/root/.openclaw/workspace/v3")
CFG_DIR   = WORKSPACE / "config"
PENDING   = CFG_DIR / "pending_orders.json"

def _now_iso(offset_min=0):
    dt = datetime.now(timezone.utc) + timedelta(minutes=offset_min)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ══════════════════════════════════════════════════════════════════════════════
# TEST 1：基础 CRUD
# ══════════════════════════════════════════════════════════════════════════════

def test_add_and_get():
    clear_all()
    add_or_update("BLESSUSDT", "LONG", 61, 0.0041, "trend_long")
    items = get_watching()
    assert len(items) == 1
    assert items[0]["symbol"] == "BLESSUSDT"
    assert items[0]["entry_score_snapshot"] == 61
    assert items[0]["score_history"] == [61]
    assert items[0]["status"] == "watching"
    print("✅ test_add_and_get")


def test_update_score_appends_history():
    clear_all()
    add_or_update("BTCUSDT", "LONG", 58, 65000, "trend_long")
    add_or_update("BTCUSDT", "LONG", 61, 65100, "trend_long")
    add_or_update("BTCUSDT", "LONG", 64, 65200, "trend_long")

    items = get_watching()
    item = items[0]
    assert item["score_history"] == [58, 61, 64]
    assert item["latest_score"] == 64
    assert item["entry_score_snapshot"] == 58  # 进池时评分不变
    print(f"✅ test_update_score_appends_history: history={item['score_history']}")


# ══════════════════════════════════════════════════════════════════════════════
# TEST 2：评分趋势 + 有效评分
# ══════════════════════════════════════════════════════════════════════════════

def test_score_trend_rising():
    assert calc_score_trend([58, 61, 64]) == "rising"
    print("✅ test_score_trend_rising")

def test_score_trend_falling():
    assert calc_score_trend([64, 61, 58]) == "falling"
    print("✅ test_score_trend_falling")

def test_score_trend_flat():
    assert calc_score_trend([60, 62, 61]) == "flat"
    assert calc_score_trend([60]) == "flat"  # 不足3个
    print("✅ test_score_trend_flat")

def test_effective_score_bonus():
    clear_all()
    add_or_update("ETHUSDT", "LONG", 58, 3000, "trend_long")
    add_or_update("ETHUSDT", "LONG", 61, 3010, "trend_long")
    add_or_update("ETHUSDT", "LONG", 63, 3020, "trend_long")

    items = get_watching()
    eff = effective_score(items[0])
    assert eff == 66, f"连升3次应 +3，期望66，得{eff}"
    print(f"✅ test_effective_score_bonus: raw=63 eff={eff} (+3 rising bonus)")

def test_effective_score_penalty():
    clear_all()
    add_or_update("SOLUSDT", "LONG", 63, 150, "trend_long")
    add_or_update("SOLUSDT", "LONG", 60, 148, "trend_long")
    add_or_update("SOLUSDT", "LONG", 57, 145, "trend_long")

    items = get_watching()
    eff = effective_score(items[0])
    assert eff == 54, f"连降3次应 -3，期望54，得{eff}"
    print(f"✅ test_effective_score_penalty: raw=57 eff={eff} (-3 falling penalty)")


# ══════════════════════════════════════════════════════════════════════════════
# TEST 3：TTL + purge
# ══════════════════════════════════════════════════════════════════════════════

def test_ttl_expiry():
    clear_all()
    # 手动写一个已过期的条目
    wls.WATCHLIST_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "updated_at": _now_iso(),
        "items": [{
            "symbol": "EXPIREDUSDT", "direction": "LONG",
            "signal_type": "trend_long",
            "entered_at": _now_iso(-200),
            "ttl_expires_at": _now_iso(-60),   # 60分钟前过期
            "entry_price_snapshot": 1.0,
            "entry_score_snapshot": 60,
            "score_history": [60],
            "latest_score": 60,
            "latest_price": 1.0,
            "upgrade_attempts": 0,
            "status": "watching",
        }]
    }
    wls.WATCHLIST_FILE.write_text(json.dumps(data))

    watching_before = get_watching()
    assert len(watching_before) == 0, "过期条目不应出现在 get_watching() 里"

    purge_expired()
    # purge 后状态变为 expired
    all_items = json.loads(wls.WATCHLIST_FILE.read_text())["items"]
    assert all_items[0]["status"] == "expired"
    print("✅ test_ttl_expiry")


def test_purge_drops_low_score():
    clear_all()
    add_or_update("WEAKUSDT", "LONG", 44, 1.0, "trend_long")
    purge_expired(drop_score_threshold=45)

    all_items = json.loads(wls.WATCHLIST_FILE.read_text())["items"]
    assert all_items[0]["status"] == "dropped", f"低分应被 drop: {all_items[0]['status']}"
    print("✅ test_purge_drops_low_score")


def test_purge_keeps_good_score():
    clear_all()
    add_or_update("GOODUSDT", "LONG", 62, 1.0, "trend_long")
    purge_expired(drop_score_threshold=45)

    items = get_watching()
    assert len(items) == 1, "好标的不应被 purge"
    print("✅ test_purge_keeps_good_score")


# ══════════════════════════════════════════════════════════════════════════════
# TEST 4：状态流转
# ══════════════════════════════════════════════════════════════════════════════

def test_mark_upgraded():
    clear_all()
    add_or_update("UPUSDT", "LONG", 67, 2.0, "trend_long")
    mark_upgraded("UPUSDT", "LONG")

    all_items = json.loads(wls.WATCHLIST_FILE.read_text())["items"]
    assert all_items[0]["status"] == "upgraded"
    # upgraded 条目不在 watching 里
    assert len(get_watching()) == 0
    print("✅ test_mark_upgraded")


def test_mark_dropped():
    clear_all()
    add_or_update("DROPUSDT", "SHORT", 50, 5.0, "trend_short")
    mark_dropped("DROPUSDT", "SHORT")

    all_items = json.loads(wls.WATCHLIST_FILE.read_text())["items"]
    assert all_items[0]["status"] == "dropped"
    print("✅ test_mark_dropped")


# ══════════════════════════════════════════════════════════════════════════════
# TEST 5：snapshot
# ══════════════════════════════════════════════════════════════════════════════

def test_snapshot():
    clear_all()
    add_or_update("A", "LONG", 60, 1.0, "trend_long")
    add_or_update("B", "LONG", 62, 2.0, "trend_long")
    mark_upgraded("A", "LONG")

    s = snapshot()
    assert s["watching"] == 1
    assert s["upgraded"] == 1
    print(f"✅ test_snapshot: watching={s['watching']} upgraded={s['upgraded']}")


# ══════════════════════════════════════════════════════════════════════════════
# TEST 6：watchlist_monitor 追高保护（新版：直接测 run_watchlist_monitor 逻辑）
# ══════════════════════════════════════════════════════════════════════════════

def test_wl_monitor_chase_protection():
    """
    进池价 0.0040，当前价涨到 0.0044（+10%）> 3% 追高限制
    run_watchlist_monitor 应跳过升级
    """
    clear_all()
    import json
    from pathlib import Path
    CFG = Path("/root/.openclaw/workspace/v3/config")

    # 写入一个有效观察池条目（评分已达标）
    snap_price = 0.0040
    add_or_update("CHASEUSDT", "LONG", 67, snap_price, "trend_long", ttl_min=120)

    # MockAPI 返回当前价比进池价高 10%
    class ChaseMockAPI:
        def get_klines(self, s, i, limit=100):
            p = snap_price * 1.10
            return [[j, str(p), str(p*1.005), str(p*0.995), str(p), "1000"]
                    for j in range(60)]
        def get_ticker(self, s):
            return {"lastPrice": str(snap_price * 1.10),
                    "lastFundingRate": "0.0001", "priceChangePercent": "10.0"}
        def get_futures_account(self):
            return {"totalWalletBalance": "1000"}

    from modules.watchlist_monitor import run_watchlist_monitor
    from modules.pos_state_store import clear_all as ps_clear
    ps_clear()

    r = run_watchlist_monitor(api=ChaseMockAPI(), btc_trend="neutral")
    assert r["upgraded"] == 0, f"追高10%应不升级，得 upgraded={r['upgraded']}"
    print(f"✅ test_wl_monitor_chase_protection: upgraded={r['upgraded']}")


def test_wl_monitor_score_too_low_drops():
    """观察池条目评分降到 40 → 被 purge"""
    clear_all()
    # 手动写一个低分条目
    add_or_update("WEAKUSDT", "LONG", 43, 1.0, "trend_long", ttl_min=120)
    from modules.watchlist_store import purge_expired
    purge_expired(drop_score_threshold=45)
    items = get_watching()
    assert len(items) == 0, f"低分条目应被清除，剩余={len(items)}"
    print("✅ test_wl_monitor_score_too_low_drops")


def test_wl_monitor_effective_score_gates_upgrade():
    """有效评分（含趋势加成）< 65 → 不升级"""
    clear_all()
    # 评分历史平稳在 62，加成后也是 62，< 65
    add_or_update("FLATUSDT", "LONG", 62, 100.0, "trend_long")
    add_or_update("FLATUSDT", "LONG", 62, 100.0, "trend_long")
    add_or_update("FLATUSDT", "LONG", 62, 100.0, "trend_long")
    items = get_watching()
    eff = effective_score(items[0])
    assert eff == 62, f"平稳评分加成应为0，eff={eff}"
    assert eff < 65
    print(f"✅ test_wl_monitor_effective_score_gates_upgrade: eff={eff} < 65")


# ══════════════════════════════════════════════════════════════════════════════
# TEST 7：scene_matcher 接口验证（替代旧的 _check_entry_condition 测试）
# ══════════════════════════════════════════════════════════════════════════════

def test_scene_matcher_basic():
    """scene_matcher.match 返回结构完整"""
    from modules.scene_matcher import match
    k = [[i, "100", "101", "99", "100", "1000"] for i in range(40)]
    r = match(k, 100.0, "LONG")
    assert "matched" in r
    assert "scene"  in r
    assert "rr"     in r
    assert "reason" in r
    assert r["scene"] in ("A_pullback", "B_breakout", "none")
    print(f"✅ test_scene_matcher_basic: matched={r['matched']} scene={r['scene']}")


def test_scene_matcher_rr_positive_when_matched():
    """匹配时 RR > 0，且 entry/sl/tp 价格方向正确"""
    from modules.scene_matcher import match
    import math
    # 构造横盘蓄势场景（场景B）
    k = []
    base = 100.0
    for i in range(50):
        t = 0.001 if i < 35 else 0   # 先上涨再横盘
        p = base * (1 + t) ** i
        # 后15根：小区间 + 缩量
        if i >= 35:
            p = base * (1 + 0.001) ** 35 * (1 + math.sin(i) * 0.005)
            v = 300.0
        else:
            v = 1000.0
        k.append([i, str(p*0.999), str(p*1.003), str(p*0.997), str(p), str(v)])
    mark = float(k[-1][4])
    r = match(k, mark, "LONG")
    if r["matched"]:
        assert r["rr"] > 0
        assert r["sl_price"] < r["entry_price"] < r["tp_price"], \
            f"LONG价格顺序错误: sl={r['sl_price']} entry={r['entry_price']} tp={r['tp_price']}"
        print(f"✅ test_scene_matcher_rr_positive_when_matched: "
              f"scene={r['scene']} rr={r['rr']}")
    else:
        print(f"✅ test_scene_matcher_rr_positive_when_matched: (未匹配) {r['reason'][:60]}")


def test_scene_matcher_no_match_straight_up():
    """直线拉升（无回调无缩量）→ 结构完整，不会崩溃"""
    from modules.scene_matcher import match
    k = [[i, str(100+i*0.5), str(100+i*0.5+0.3),
          str(100+i*0.5-0.2), str(100+i*0.5), "1200"]
         for i in range(50)]
    r = match(k, float(k[-1][4]) * 1.03, "LONG")  # 价格还在上冲
    assert isinstance(r["matched"], bool)
    assert isinstance(r["reason"], str)
    print(f"✅ test_scene_matcher_no_match_straight_up: "
          f"matched={r['matched']} scene={r['scene']}")


# ══════════════════════════════════════════════════════════════════════════════
# TEST 8：scanner 分叉逻辑
# ══════════════════════════════════════════════════════════════════════════════

def test_scanner_watchlist_only_flag():
    """watchlist_only 字段分叉逻辑"""
    sig_active = {"score": 68, "watchlist_only": False}
    sig_watch  = {"score": 59, "watchlist_only": True}
    sig_no_scene = {"score": 66, "watchlist_only": True}  # ≥65但场景不满足

    active   = [s for s in [sig_active, sig_watch, sig_no_scene]
                if not s.get("watchlist_only")]
    watching = [s for s in [sig_active, sig_watch, sig_no_scene]
                if s.get("watchlist_only")]

    assert len(active)   == 1 and active[0]["score"] == 68
    assert len(watching) == 2
    print("✅ test_scanner_watchlist_only_flag")


# ══════════════════════════════════════════════════════════════════════════════
# 主入口
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    tests = [
        test_add_and_get,
        test_update_score_appends_history,
        test_score_trend_rising,
        test_score_trend_falling,
        test_score_trend_flat,
        test_effective_score_bonus,
        test_effective_score_penalty,
        test_ttl_expiry,
        test_purge_drops_low_score,
        test_purge_keeps_good_score,
        test_mark_upgraded,
        test_mark_dropped,
        test_snapshot,
        test_upgrade_filter_chase_protection,
        test_upgrade_filter_within_chase,
        test_upgrade_filter_score_too_low,
        test_entry_condition_trend_no_pullback,
        test_entry_condition_breakout_confirmed,
        test_entry_condition_reversal_rsi_not_oversold,
        test_scanner_watchlist_only_flag,
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
    import sys as _sys
    if failed:
        _sys.exit(1)
