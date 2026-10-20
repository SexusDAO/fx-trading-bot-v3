"""tests/test_add_position.py"""
import sys, time
sys.path.insert(0, "/root/.openclaw/workspace/v3")
from modules.add_position import (
    check_flatten, apply_flatten,
    check_signal_enhance, apply_signal_enhance,
    calc_avg_price,
)

# ── 工具 ──────────────────────────────────────────────────────────────────────

def _pos(entry=100.0, notional=500, direction="LONG",
         flatten_count=0, last_flatten_at=None,
         scene_four=False, full_notional=0, enhance_done=False):
    return {
        "entry_price":    entry,
        "avg_price":      entry,
        "notional":       notional,
        "total_notional": notional,
        "first_notional": notional,
        "direction":      direction,
        "flatten_count":  flatten_count,
        "last_flatten_at": last_flatten_at,
        "scene_four":     scene_four,
        "full_notional":  full_notional,
        "enhance_done":   enhance_done,
    }

# ── calc_avg_price ────────────────────────────────────────────────────────────

def test_avg_price():
    avg = calc_avg_price(500, 100.0, 500, 95.0)
    assert avg == 97.5, f"期望97.5，得{avg}"
    print(f"✅ calc_avg_price 500@100+500@95 = {avg}")

def test_avg_price_zero():
    avg = calc_avg_price(0, 100.0, 300, 95.0)
    assert avg == 95.0
    print(f"✅ calc_avg_price 首仓为0兜底 = {avg}")

# ── 摊平型补仓 ────────────────────────────────────────────────────────────────

def test_flatten_triggers():
    """浮亏>3% + 健康≥60 + 未满2次 + 间隔足够 → 触发"""
    pos = _pos(entry=100.0, notional=300)
    r = check_flatten(pos, current_price=96.5, health_score=70)
    assert r["should_add"] is True, f"应触发：{r}"
    assert r["add_notional"] == 300
    assert r["add_type"] == "flatten"
    print(f"✅ 摊平触发 add_notional={r['add_notional']}")

def test_flatten_loss_not_enough():
    pos = _pos(entry=100.0)
    r = check_flatten(pos, current_price=98.5, health_score=70)  # 只亏1.5%
    assert r["should_add"] is False
    print(f"✅ 浮亏不足不触发: {r['reason']}")

def test_flatten_health_too_low():
    pos = _pos(entry=100.0)
    r = check_flatten(pos, current_price=96.0, health_score=55)
    assert r["should_add"] is False
    print(f"✅ 健康分不足不触发: {r['reason']}")

def test_flatten_max_times():
    pos = _pos(entry=100.0, flatten_count=2)
    r = check_flatten(pos, current_price=95.0, health_score=70)
    assert r["should_add"] is False
    print(f"✅ 次数上限不触发: {r['reason']}")

def test_flatten_interval():
    """距上次补仓不足30分钟"""
    from modules.add_position import _now_iso
    pos = _pos(entry=100.0, last_flatten_at=_now_iso())
    r = check_flatten(pos, current_price=95.0, health_score=70)
    assert r["should_add"] is False
    print(f"✅ 间隔不足不触发: {r['reason']}")

def test_flatten_apply():
    """补仓后均价、次数、总仓位正确更新"""
    pos = _pos(entry=100.0, notional=500)
    pos2 = apply_flatten(pos, add_price=96.0, add_notional=500)
    assert pos2["flatten_count"] == 1
    assert pos2["total_notional"] == 1000
    expected_avg = (500*100 + 500*96) / 1000  # = 98.0
    assert abs(pos2["avg_price"] - expected_avg) < 0.001, \
        f"期望{expected_avg}，得{pos2['avg_price']}"
    assert pos2["last_flatten_at"] is not None
    print(f"✅ apply_flatten 均价={pos2['avg_price']} 总仓={pos2['total_notional']}")

def test_flatten_hard_cap():
    """补仓后不超过1000U硬上限"""
    pos = _pos(entry=100.0, notional=700)
    pos["total_notional"] = 700
    pos["first_notional"] = 700
    r = check_flatten(pos, current_price=95.0, health_score=70)
    assert r["should_add"] is True
    assert r["add_notional"] == 300   # 1000-700=300，不是700
    print(f"✅ 硬上限截断 add_notional={r['add_notional']}")

# ── 信号增强型补仓 ────────────────────────────────────────────────────────────

def test_enhance_triggers():
    pos = _pos(scene_four=True, notional=150, full_notional=300)
    pos["total_notional"] = 150
    r = check_signal_enhance(pos, new_score=72)
    assert r["should_add"] is True
    assert r["add_notional"] == 150   # 300-150
    assert r["add_type"] == "signal_enhance"
    print(f"✅ 信号增强触发 add_notional={r['add_notional']}")

def test_enhance_score_not_enough():
    pos = _pos(scene_four=True, notional=150, full_notional=300)
    pos["total_notional"] = 150
    r = check_signal_enhance(pos, new_score=68)
    assert r["should_add"] is False
    print(f"✅ 评分不足不触发: {r['reason']}")

def test_enhance_not_scene_four():
    pos = _pos(scene_four=False, notional=300)
    r = check_signal_enhance(pos, new_score=80)
    assert r["should_add"] is False
    print(f"✅ 非场景四不触发: {r['reason']}")

def test_enhance_already_done():
    pos = _pos(scene_four=True, enhance_done=True, notional=300, full_notional=300)
    pos["total_notional"] = 300
    r = check_signal_enhance(pos, new_score=80)
    assert r["should_add"] is False
    print(f"✅ 已执行过不重复触发: {r['reason']}")

def test_enhance_apply():
    pos = _pos(entry=100.0, notional=150, scene_four=True, full_notional=300)
    pos["total_notional"] = 150
    pos2 = apply_signal_enhance(pos, add_price=101.0, add_notional=150)
    assert pos2["enhance_done"] is True
    assert pos2["total_notional"] == 300
    expected = (150*100 + 150*101) / 300  # 100.5
    assert abs(pos2["avg_price"] - expected) < 0.001
    print(f"✅ apply_signal_enhance 均价={pos2['avg_price']} enhance_done=True")

if __name__ == "__main__":
    test_avg_price()
    test_avg_price_zero()
    test_flatten_triggers()
    test_flatten_loss_not_enough()
    test_flatten_health_too_low()
    test_flatten_max_times()
    test_flatten_interval()
    test_flatten_apply()
    test_flatten_hard_cap()
    test_enhance_triggers()
    test_enhance_score_not_enough()
    test_enhance_not_scene_four()
    test_enhance_already_done()
    test_enhance_apply()
    print("\n🎉 add_position 全部测试通过")
