"""运行：cd fx && python3 -m unittest tests.test_trigger_identity -v
[v3.89 #102-partial] _trigger 的 finally / 冷却分支 pop 前核对条目身份。"""
import asyncio, sys, time, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from shared.signal_watcher import SignalWatcher
from shared import binance_api as _api


async def _noop(*a, **k):
    return None


def _kw(**o):
    kw = dict(entry_low=99.0, entry_high=101.0, entry_price=100.0, expire_ts=time.time() + 600,
              on_trigger=_noop, trigger_price=100.0, trigger_rsi_1h=50.0, breakout_ref=0.0,
              ema20_at_trigger=100.0, atr_at_trigger=1.0, is_reorder=False)
    kw.update(o)
    return kw


def _sig(tag):
    return {"symbol": "XUSDT", "mode": "breakout", "direction": "LONG", "tag": tag}


class TestTriggerPopIdentity(unittest.TestCase):
    def setUp(self):
        # 交易所查询全部 mock，不联网
        self._orig = {n: getattr(_api, n) for n in
                      ("get_mark_price", "get_order_book", "get_position", "get_open_orders")}
        _api.get_mark_price = lambda s: (_ for _ in ()).throw(Exception("no net"))
        _api.get_order_book = lambda s, limit=20: {}
        _api.get_position = lambda s: []
        _api.get_open_orders = lambda s: []

    def tearDown(self):
        for n, f in self._orig.items():
            setattr(_api, n, f)

    def test_finally_does_not_pop_replacement_entry(self):
        """[P-01] 旧信号触发期间 add_signal 替换会被拒；这里直接把新条目写入队列
        （模拟替换在触发前已完成、旧引用随后才走到 finally 的合法场景），
        验证 finally 核对身份后不误删新条目。"""
        w = SignalWatcher()
        w_new = SignalWatcher()

        async def run():
            await w_new.add_signal(_sig("new"), **_kw())
            new_entry = w_new._watched["XUSDT"]

            async def on_trig(sig):
                async with w._lock:
                    w._watched["XUSDT"] = new_entry
            await w.add_signal(_sig("old"), **_kw(on_trigger=on_trig))
            old = w._watched["XUSDT"]
            await w._trigger("XUSDT", 100.0, old)
            return new_entry
        new_entry = asyncio.run(run())
        self.assertIs(w._watched["XUSDT"], new_entry)
        self.assertEqual(w._watched["XUSDT"].signal["tag"], "new")

    def test_replace_succeeds_when_old_not_triggered(self):
        """[P-01] 旧信号未触发时 replace=True 替换成功；旧引用随后触发被跳过，新信号留下。"""
        w = SignalWatcher()

        async def run():
            await w.add_signal(_sig("old"), **_kw())
            old = w._watched["XUSDT"]
            self.assertFalse(old.triggered)
            await w.add_signal(_sig("new"), replace=True, **_kw(is_reorder=True))
            await w._trigger("XUSDT", 100.0, old)
        asyncio.run(run())
        self.assertEqual(w._watched["XUSDT"].signal["tag"], "new")

    def test_replace_rejected_while_old_triggered(self):
        """[P-01] 旧条目 triggered=True（正在下单）时，replace=True 的新信号被拒，旧条目保留。"""
        w = SignalWatcher()

        async def run():
            await w.add_signal(_sig("old"), **_kw())
            old = w._watched["XUSDT"]
            old.triggered = True
            with self.assertLogs("fx.signal_watcher", level="WARNING") as cm:
                await w.add_signal(_sig("new"), replace=True, **_kw(is_reorder=True))
            return old, cm.output
        old, out = asyncio.run(run())
        self.assertIs(w._watched["XUSDT"], old)
        self.assertEqual(w._watched["XUSDT"].signal["tag"], "old")
        self.assertTrue(any("替换被拒：旧信号正在下单中" in x for x in out))

    def test_finally_pops_own_entry(self):
        w = SignalWatcher()

        async def run():
            await w.add_signal(_sig("old"), **_kw())
            await w._trigger("XUSDT", 100.0, w._watched["XUSDT"])
        asyncio.run(run())
        self.assertNotIn("XUSDT", w._watched)

    def test_cooldown_branch_pops_own_entry(self):
        w = SignalWatcher()

        async def run():
            await w.add_signal(_sig("old"), **_kw())
            w._dir_last_trigger["LONG_breakout"] = time.time()
            await w._trigger("XUSDT", 100.0, w._watched["XUSDT"])
        asyncio.run(run())
        self.assertNotIn("XUSDT", w._watched)


if __name__ == "__main__":
    unittest.main()
