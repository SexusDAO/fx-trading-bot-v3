"""tests/test_position_size_and_gate.py"""
import sys, json
sys.path.insert(0, "/root/.openclaw/workspace/v3")
from modules.position_size import calc_position_size
from modules.pre_order_gate import pre_order_gate
from modules.pos_state_store import clear_all, write as ps_write
from pathlib import Path

CFG_DIR = Path("/root/.openclaw/workspace/v3/config")

def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f)

def setup_clean():
    clear_all()
    _write_json(CFG_DIR / "circuit_breaker.json", {"open": False, "daily_loss": 0})
    _write_json(CFG_DIR / "blacklist.json", {"symbols": []})
    _write_json(CFG_DIR / "market_regime.json", {"mode": "bull"})

# ══════════════════════════════════════════════════════════════
# position_size
# ══════════════════════════════════════════════════════════════

def test_size_tiers():
    cases = [
        ("trend", 90, 1000),
        ("trend", 85, 700),
        ("trend", 72, 500),
        ("trend", 66, 300),
        ("trend_short", 82, 600),
        ("trend_short", 75, 450),
        ("reversal", 62, 250),
        ("reversal_l1", 60, 125),
    ]
    for sig, score, expected in cases:
        got = calc_position_size(sig, score)
        assert got == expected, f"{sig}@{score}: 期望{expected}，得{got}"
    print("✅ 仓位档位全部正确")

def test_size_scene_four():
    got = calc_position_size("trend", 67, scene_four=True)
    assert got == 150, f"场景四 67分 期望150U，得{got}U"
    # 非场景四不减半
    got2 = calc_position_size("trend", 67, scene_four=False)
    assert got2 == 300, f"非场景四 67分 期望300U，得{got2}U"
    print(f"✅ 场景四减半: 150U vs 正常: 300U")

def test_size_btc_coef():
    base = calc_position_size("trend", 75)               # 500U
    bull = calc_position_size("trend", 75, "bull")        # 600U
    bear = calc_position_size("trend", 75, "bear")        # 400U
    assert bull > base > bear, f"bull={bull} base={base} bear={bear}"
    print(f"✅ BTC 系数: 牛{bull} 中{base} 熊{bear}")

def test_size_consecutive_loss():
    base = calc_position_size("trend", 75)                # 500
    after3 = calc_position_size("trend", 75, consecutive_losses=3)  # 400
    halt   = calc_position_size("trend", 75, consecutive_losses=5)  # 0
    assert after3 < base, f"连亏3笔应缩仓: {after3}<{base}"
    assert halt == 0, f"连亏5笔应停仓: {halt}"
    print(f"✅ 连亏系数: 正常{base} 连亏3{after3} 连亏5→{halt}")

def test_size_hard_cap():
    # 牛市 + 强信号，不超过 1000U
    got = calc_position_size("trend", 95, "bull")
    assert got <= 1000, f"超过硬上限: {got}"
    print(f"✅ 硬上限 1000U: got={got}")

# ══════════════════════════════════════════════════════════════
# pre_order_gate
# ══════════════════════════════════════════════════════════════

def test_gate_pass():
    setup_clean()
    r = pre_order_gate("BTCUSDT", "LONG", score=75, funding=-0.001, notional=500, equity=5000)
    assert r["ok"] is True, f"应通过: {r}"
    print("✅ gate 全部通过")

def test_gate_check1_full():
    setup_clean()
    # 填满 bull 模式多单（3个）
    for i in range(3):
        ps_write(f"COIN{i}USDT", "LONG", {"notional": 300})
    r = pre_order_gate("NEWUSDT", "LONG", 75, 0.0, 300)
    assert r["ok"] is False and r["check"] == 1
    print(f"✅ 检查一：多单满 → {r['reason']}")

def test_gate_check2_circuit():
    setup_clean()
    _write_json(CFG_DIR / "circuit_breaker.json", {"open": True})
    r = pre_order_gate("BTCUSDT", "LONG", 75, 0.0, 500)
    assert r["ok"] is False and r["check"] == 2
    print(f"✅ 检查二：熔断 → {r['reason']}")

def test_gate_check3_blacklist():
    setup_clean()
    _write_json(CFG_DIR / "blacklist.json", {"symbols": ["RUGUSDT"]})
    r = pre_order_gate("RUGUSDT", "LONG", 75, 0.0, 300)
    assert r["ok"] is False and r["check"] == 3
    print(f"✅ 检查三：黑名单 → {r['reason']}")

def test_gate_check4_dup():
    setup_clean()
    ps_write("BTCUSDT", "LONG", {"notional": 500})
    r = pre_order_gate("BTCUSDT", "LONG", 75, 0.0, 500)
    assert r["ok"] is False and r["check"] == 4
    print(f"✅ 检查四：重复持仓 → {r['reason']}")

def test_gate_check6_smart_money():
    setup_clean()
    r = pre_order_gate("ETHUSDT", "SHORT", 70, 0.001, 400, smart_money_direction="buy")
    assert r["ok"] is False and r["check"] == 6
    print(f"✅ 检查六：聪明钱反向 → {r['reason']}")

def test_gate_check7_funding():
    setup_clean()
    r = pre_order_gate("ETHUSDT", "LONG", 70, funding=0.008, notional=400)
    assert r["ok"] is False and r["check"] == 7
    print(f"✅ 检查七：资金费率过高 → {r['reason']}")

if __name__ == "__main__":
    test_size_tiers()
    test_size_scene_four()
    test_size_btc_coef()
    test_size_consecutive_loss()
    test_size_hard_cap()
    test_gate_pass()
    test_gate_check1_full()
    test_gate_check2_circuit()
    test_gate_check3_blacklist()
    test_gate_check4_dup()
    test_gate_check6_smart_money()
    test_gate_check7_funding()
    print("\n🎉 position_size + pre_order_gate 全部测试通过")
