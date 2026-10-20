"""tests/test_pos_state_store.py"""
import sys, json
sys.path.insert(0, "/root/.openclaw/workspace/v3")
from modules.pos_state_store import write, get, get_all, remove, clear_all

def test_write_and_get():
    clear_all()
    write("BTCUSDT", "LONG", {
        "entry_price": 65000,
        "notional": 500,
        "sl_price": 63000,
        "score_at_open": 87,
    })
    pos = get("BTCUSDT", "LONG")
    assert pos is not None, "持仓不存在"
    assert pos["entry_price"] == 65000
    assert pos["symbol"] == "BTCUSDT"
    assert pos["direction"] == "LONG"
    assert "updated_at" in pos
    print("✅ write + get 通过")

def test_update():
    write("BTCUSDT", "LONG", {"sl_price": 64000})
    pos = get("BTCUSDT", "LONG")
    assert pos["sl_price"] == 64000
    assert pos["entry_price"] == 65000   # 旧字段保留
    print("✅ update 字段保留 + 覆盖 通过")

def test_multiple_positions():
    write("ETHUSDT", "SHORT", {"entry_price": 3200, "notional": 300})
    all_pos = get_all()
    assert "BTCUSDT_LONG" in all_pos
    assert "ETHUSDT_SHORT" in all_pos
    print(f"✅ 多仓位 通过（{len(all_pos)} 条）")

def test_remove():
    remove("ETHUSDT", "SHORT")
    assert get("ETHUSDT", "SHORT") is None
    print("✅ remove 通过")

def test_atomic_write():
    """并发写入不会产生空文件"""
    import threading
    clear_all()
    def _w(i):
        write(f"COIN{i}USDT", "LONG", {"entry_price": i * 100})
    threads = [threading.Thread(target=_w, args=(i,)) for i in range(10)]
    for t in threads: t.start()
    for t in threads: t.join()
    all_pos = get_all()
    assert len(all_pos) == 10, f"期望10条，实际{len(all_pos)}条"
    print(f"✅ 并发写入 通过（{len(all_pos)} 条）")

if __name__ == "__main__":
    test_write_and_get()
    test_update()
    test_multiple_positions()
    test_remove()
    test_atomic_write()
    print("\n🎉 pos_state_store 全部测试通过")
