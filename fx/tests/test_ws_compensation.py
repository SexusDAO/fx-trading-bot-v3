"""运行：cd fx && python3 -m unittest tests.test_ws_compensation -v
[v3.89 #3/#7] userData 断线补偿任务、listenKey 续期失败/过期驱动重建。全部 mock，不连交易所。"""
import asyncio, sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from shared import ws_listener as W


class FakeWs:
    def __init__(self): self.closed = 0
    async def close(self): self.closed += 1


class TestCompensation(unittest.TestCase):
    def setUp(self):
        self._t, self._i = W.COMPENSATION_TIMEOUT, W.COMPENSATION_INTERVAL
        W.COMPENSATION_TIMEOUT, W.COMPENSATION_INTERVAL = 0.5, 0.05

    def tearDown(self):
        W.COMPENSATION_TIMEOUT, W.COMPENSATION_INTERVAL = self._t, self._i

    def test_starts_single_task_and_stops_after_reconnect(self):
        async def run():
            ws = W.WsOrderListener()
            calls = []

            async def cb(): calls.append(1)
            ws.on_disconnect(cb)
            ws._mark_user_stream_down()
            t1 = ws._compensation_task
            ws._mark_user_stream_down()               # 重复断线不应再起一个
            self.assertIs(ws._compensation_task, t1)
            await asyncio.sleep(0.2)
            n = len(calls)
            self.assertGreaterEqual(n, 1)
            ws._user_connected = True                 # 重连成功
            await asyncio.wait_for(t1, timeout=1)
            self.assertTrue(t1.done())
            await asyncio.sleep(0.15)
            self.assertLessEqual(len(calls), n + 1)   # 不再发起新一轮（最多是正在执行的那一轮）
        asyncio.run(run())

    def test_bounded_by_timeout(self):
        async def run():
            ws = W.WsOrderListener()
            ws.on_disconnect(lambda: asyncio.sleep(0))
            ws._mark_user_stream_down()
            await asyncio.wait_for(ws._compensation_task, timeout=2)
            self.assertTrue(ws._compensation_task.done())
        asyncio.run(run())

    def test_callback_error_does_not_kill_task(self):
        async def run():
            ws = W.WsOrderListener()
            calls = []

            async def bad(): calls.append(1); raise RuntimeError("x")
            ws.on_disconnect(bad)
            ws._mark_user_stream_down()
            await asyncio.sleep(0.2)
            self.assertGreaterEqual(len(calls), 2)    # 异常后仍继续下一轮
            ws._user_connected = True
            await asyncio.wait_for(ws._compensation_task, timeout=1)
        asyncio.run(run())

    def test_no_callbacks_no_task(self):
        async def run():
            ws = W.WsOrderListener()
            ws._mark_user_stream_down()
            self.assertIsNone(ws._compensation_task)
        asyncio.run(run())

    def test_stop_cancels_compensation(self):
        async def run():
            ws = W.WsOrderListener()
            ws.on_disconnect(lambda: asyncio.sleep(0))
            ws._mark_user_stream_down()
            await ws.stop()
            self.assertTrue(ws._compensation_task.done())
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
