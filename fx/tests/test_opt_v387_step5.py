"""运行：cd fx && python3 -m unittest tests.test_opt_v387_step5 -v
[v3.87 STEP5-8] 价格位移重挂：先注册新单、成功后才撤旧单；注册失败时旧单保留。"""
import asyncio, sys, time, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from shared.signal_watcher import SignalWatcher


async def _noop(*a, **k):
    return None


def _kwargs(**over):
    kw = dict(entry_low=99.0, entry_high=101.0, entry_price=100.0,
              expire_ts=time.time() + 600, on_trigger=_noop,
              trigger_price=100.0, trigger_rsi_1h=50.0, breakout_ref=0.0,
              ema20_at_trigger=100.0, atr_at_trigger=1.0, is_reorder=True)
    kw.update(over)
    return kw


class TestReorderReplace(unittest.TestCase):
    def _watcher(self, events):
        w = SignalWatcher()

        async def fake_cancel(symbol, order_id):
            events.append(("cancel", symbol, order_id, w._watched[symbol].signal.get("tag")))
        w._cancel_order = fake_cancel
        return w

    def test_new_registered_before_old_cancelled(self):
        events = []
        w = self._watcher(events)

        async def run():
            await w.add_signal({"symbol": "XUSDT", "mode": "breakout", "direction": "LONG", "tag": "old"},
                               **_kwargs(is_reorder=False))
            old = w._watched["XUSDT"]
            old.limit_order_id = 777
            await w._reorder_replace("XUSDT", old, {"symbol": "XUSDT", "mode": "breakout",
                                                    "direction": "LONG", "tag": "new"}, **_kwargs())
        asyncio.run(run())
        # 撤旧单发生时，监控队列里已经是新条目
        self.assertEqual(events, [("cancel", "XUSDT", 777, "new")])
        self.assertEqual(w._watched["XUSDT"].signal["tag"], "new")
        self.assertIs(w._watched["XUSDT"].on_trigger, _noop)

    def test_failure_keeps_old_and_does_not_cancel(self):
        events = []
        w = self._watcher(events)

        async def run():
            await w.add_signal({"symbol": "XUSDT", "mode": "breakout", "direction": "LONG", "tag": "old"},
                               **_kwargs(is_reorder=False))
            old = w._watched["XUSDT"]
            old.limit_order_id = 777
            bad = _kwargs()
            bad["bogus_param"] = 1          # 模拟 add_signal 因参数错误抛 TypeError
            with self.assertRaises(TypeError):
                await w._reorder_replace("XUSDT", old, {"symbol": "XUSDT", "mode": "breakout",
                                                        "direction": "LONG", "tag": "new"}, **bad)
        asyncio.run(run())
        self.assertEqual(events, [])
        self.assertEqual(w._watched["XUSDT"].signal["tag"], "old")

    def test_duplicate_without_replace_is_skipped(self):
        w = SignalWatcher()

        async def run():
            await w.add_signal({"symbol": "XUSDT", "mode": "breakout", "direction": "LONG", "tag": "old"},
                               **_kwargs(is_reorder=False))
            await w.add_signal({"symbol": "XUSDT", "mode": "breakout", "direction": "LONG", "tag": "new"},
                               **_kwargs())
        asyncio.run(run())
        self.assertEqual(w._watched["XUSDT"].signal["tag"], "old")


if __name__ == "__main__":
    unittest.main()
