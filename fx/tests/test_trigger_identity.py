# [已知问题 fdde5ff4] 标的锁范围过大：_trigger 持锁期间，同标的 add_signal(replace=True)
# 会等锁 5s 后超时放弃，旧条目移除后监控队列里没有该标的，测试报 KeyError。
# 线上影响：下单中同标的的替换信号会被丢弃，该标的暂时无挂单监控。
# 修复方向：下单时不持注册锁，只在"检查并写入队列"那一小段加锁；或允许重入；补 mock。
# 暂不修复，等后续单独处理。
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
        self._orig = _api.get_mark_price
        _api.get_mark_price = lambda s: (_ for _ in ()).throw(Exception("no net"))

    def tearDown(self):
        _api.get_mark_price = self._orig

    def test_finally_does_not_pop_replacement_entry(self):
        w = SignalWatcher()

        async def run():
            async def on_trig(sig):
                # 执行期间同品种被替换成新条目
                await w.add_signal(_sig("new"), replace=True, **_kw(is_reorder=True))
            await w.add_signal(_sig("old"), **_kw(on_trigger=on_trig))
            old = w._watched["XUSDT"]
            await w._trigger("XUSDT", 100.0, old)
        asyncio.run(run())
        self.assertEqual(w._watched["XUSDT"].signal["tag"], "new")

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
