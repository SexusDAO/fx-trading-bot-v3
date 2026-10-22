"""userData 无数据验证 listenKey 测试：全部 mock，不联网。"""
import asyncio
import logging
from unittest.mock import MagicMock

import pytest
import requests

import config
from shared import ws_listener as wl
from shared import binance_api as api


def run(c):
    return asyncio.new_event_loop().run_until_complete(c)


@pytest.fixture
def lst(monkeypatch):
    w = wl.WsOrderListener()
    w._user_connected = True
    w._listen_key = "LK_OLD"
    w._user_ws = MagicMock()
    w._user_connected_at = 1000.0
    w._stream_last_msg["userData"] = 0.0
    forced = []

    async def _force(reason):
        forced.append(reason)
    monkeypatch.setattr(w, "_force_user_reconnect", _force)

    async def _rs(fn, *a, timeout=None, **k):
        return fn(*a, **k)
    monkeypatch.setattr(wl, "_run_sync", _rs)
    return w, forced


def _put(monkeypatch, code=None, exc=None):
    m = MagicMock()
    if exc:
        m.side_effect = exc
    else:
        r = MagicMock(); r.status_code = code; m.return_value = r
    monkeypatch.setattr(api, "_listen_key_request", m)
    return m


def test_within_60s_no_check(lst, monkeypatch):
    w, forced = lst
    m = _put(monkeypatch, 200)
    assert run(w._user_idle_check_once(now=1000.0 + 59)) is None
    m.assert_not_called() and None
    assert forced == []


def test_verify_ok_no_reconnect(lst, monkeypatch):
    w, forced = lst
    m = _put(monkeypatch, 200)
    assert run(w._user_idle_check_once(now=1061.0)) == "ok"
    assert forced == [] and w._listen_key == "LK_OLD"
    assert m.call_args.args[0] == "PUT" and m.call_args.kwargs["params"] == {"listenKey": "LK_OLD"}
    assert run(w._user_idle_check_once(now=1100.0)) is None          # 至少再等 60 秒
    assert run(w._user_idle_check_once(now=1122.0)) == "ok"


def test_verify_invalid_forces_reconnect(lst, monkeypatch, caplog):
    w, forced = lst
    _put(monkeypatch, 400)
    with caplog.at_level(logging.WARNING):
        assert run(w._user_idle_check_once(now=1061.0)) == "invalid"
    assert len(forced) == 1 and w._listen_key is None
    assert "[userData] listenKey可能失效，已强制重新获取并重连" in caplog.text


def test_reconnect_uses_new_key(monkeypatch):
    """invalid 后 _listen_key 置空 → 重连循环重新 POST 获取新 key，并用新 key 建连。"""
    w = wl.WsOrderListener()
    keys = iter(["LK_NEW"])
    monkeypatch.setattr(api, "get_listen_key", lambda: next(keys))

    async def _rs(fn, *a, timeout=None, **k):
        return fn(*a, **k)
    monkeypatch.setattr(wl, "_run_sync", _rs)
    urls = []

    class _Stop(Exception):
        pass

    def _connect(url, **k):
        urls.append(url)
        w._stop_flag = True
        raise _Stop()
    monkeypatch.setattr(wl.websockets, "connect", _connect)

    async def _sleep(*a, **k):
        return None
    monkeypatch.setattr(wl.asyncio, "sleep", _sleep)
    monkeypatch.setattr(w, "_mark_user_stream_down", lambda: None)
    run(w._user_data_loop())
    assert w._listen_key == "LK_NEW" and urls and urls[0].endswith("/ws/LK_NEW")


@pytest.mark.parametrize("kind", ["timeout", "neterr", "5xx", "429"])
def test_verify_timeout_or_neterr_no_reconnect(lst, monkeypatch, kind):
    w, forced = lst
    if kind == "timeout":
        async def _rs(fn, *a, timeout=None, **k):
            raise wl.RunSyncTimeout()
        monkeypatch.setattr(wl, "_run_sync", _rs)
        expect = "timeout"
    elif kind == "neterr":
        _put(monkeypatch, exc=requests.exceptions.ProxyError("SOCKS failure")); expect = "error"
    else:
        _put(monkeypatch, 503 if kind == "5xx" else 429); expect = "error"
    assert run(w._user_idle_check_once(now=1061.0)) == expect
    assert forced == [] and w._listen_key == "LK_OLD"


def test_recent_event_resets_idle(lst, monkeypatch):
    w, _ = lst
    m = _put(monkeypatch, 200)
    w._stream_last_msg["userData"] = 1050.0
    assert run(w._user_idle_check_once(now=1100.0)) is None
    m.assert_not_called()


def test_keepalive_interval_20min():
    assert config.WS_KEEPALIVE_INTERVAL == 20 * 60
    assert wl.KEEPALIVE_INTERVAL == 20 * 60
    assert wl.USER_IDLE_VERIFY_SEC == 60
