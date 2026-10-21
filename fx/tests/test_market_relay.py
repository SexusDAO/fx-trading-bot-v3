"""行情中转站（tools/market_relay.py）测试：本地假上游 + 真实 websockets 服务，不联网。"""
import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest
import websockets
from websockets.asyncio.server import serve

_spec = importlib.util.spec_from_file_location("market_relay", Path(__file__).parent.parent / "tools" / "market_relay.py")
mr = importlib.util.module_from_spec(_spec)
sys.modules["market_relay"] = mr
_spec.loader.exec_module(mr)

FRAME = '[{"e":"markPriceUpdate","s":"BTCUSDT","p":"100.5","r":"0.0001","T":1700003600000}]'


async def _start_upstream(frames_per_conn, hold=True):
    """假上游：每个连接依次发 frames_per_conn[i] 里的帧；hold=False 时发完即断开。"""
    conns = []

    async def h(ws):
        idx = len(conns)
        conns.append(ws)
        for f in (frames_per_conn[idx] if idx < len(frames_per_conn) else []):
            await ws.send(f)
        if hold:
            try:
                await ws.wait_closed()
            except Exception:
                pass

    srv = await serve(h, "127.0.0.1", 0)
    return srv, srv.sockets[0].getsockname()[1], conns


async def _start_relay(upstream_port, token="tok", **kw):
    relay = mr.Relay(f"ws://127.0.0.1:{upstream_port}", token, reconnect_base=0.05, **kw)
    ready = asyncio.get_running_loop().create_future()
    task = asyncio.ensure_future(relay.run("127.0.0.1", 0, ready))
    port = await asyncio.wait_for(ready, 5)
    return relay, task, port


async def _stop(task, *servers):
    task.cancel()
    try:
        await task
    except BaseException:
        pass
    for s in servers:
        s.close()
        await s.wait_closed()


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 20))


def test_frames_forwarded_to_authorized_client():
    async def go():
        up, up_port, _ = await _start_upstream([[FRAME, FRAME]])
        relay, task, port = await _start_relay(up_port)
        try:
            async with websockets.connect(f"ws://127.0.0.1:{port}/?token=tok") as c:
                m = await asyncio.wait_for(c.recv(), 5)
                assert m == FRAME
            assert relay.frame_count >= 1
        finally:
            await _stop(task, up)
    run(go())


def test_bearer_header_accepted_and_wrong_or_missing_token_rejected():
    async def go():
        up, up_port, _ = await _start_upstream([[FRAME]])
        relay, task, port = await _start_relay(up_port)
        try:
            async with websockets.connect(f"ws://127.0.0.1:{port}/", additional_headers={"Authorization": "Bearer tok"}) as c:
                assert await asyncio.wait_for(c.recv(), 5) == FRAME
            for url in (f"ws://127.0.0.1:{port}/", f"ws://127.0.0.1:{port}/?token=bad"):
                with pytest.raises(websockets.exceptions.InvalidStatus) as ei:
                    async with websockets.connect(url):
                        pass
                assert ei.value.response.status_code == 401
        finally:
            await _stop(task, up)
    run(go())


def test_new_client_gets_last_frame_immediately_and_broadcast_reaches_all():
    async def go():
        up, up_port, conns = await _start_upstream([[FRAME]])
        relay, task, port = await _start_relay(up_port)
        try:
            for _ in range(100):
                if relay.last_frame:
                    break
                await asyncio.sleep(0.02)
            async with websockets.connect(f"ws://127.0.0.1:{port}/?token=tok") as a, \
                       websockets.connect(f"ws://127.0.0.1:{port}/?token=tok") as b:
                assert await asyncio.wait_for(a.recv(), 5) == FRAME      # 最近一帧
                assert await asyncio.wait_for(b.recv(), 5) == FRAME
                new = FRAME.replace("100.5", "101")
                await conns[0].send(new)                                   # 之后的实时帧广播给所有人
                assert await asyncio.wait_for(a.recv(), 5) == new
                assert await asyncio.wait_for(b.recv(), 5) == new
        finally:
            await _stop(task, up)
    run(go())


def test_upstream_reconnects_after_drop():
    async def go():
        up, up_port, conns = await _start_upstream([[FRAME], [FRAME.replace("100.5", "102")]], hold=False)
        relay, task, port = await _start_relay(up_port)
        try:
            for _ in range(200):
                if relay.reconnect_count >= 1 and relay.frame_count >= 2:
                    break
                await asyncio.sleep(0.05)
            assert relay.reconnect_count >= 1 and len(conns) >= 2
            assert "102" in relay.last_frame
        finally:
            await _stop(task, up)
    run(go())


def test_refuses_to_start_without_token(monkeypatch, capsys):
    monkeypatch.delenv("RELAY_TOKEN", raising=False)
    assert mr.main([]) == 2
    assert "RELAY_TOKEN" in capsys.readouterr().err
