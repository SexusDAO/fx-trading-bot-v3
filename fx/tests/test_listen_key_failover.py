"""listenKey 主备代理切换测试：全部 mock，不联网。"""
import logging
from unittest.mock import MagicMock

import pytest
import requests

from shared import binance_api as api

BAK = {"http": "socks5h://bak:2", "https": "socks5h://bak:2"}


def _resp(code=200, body=None):
    r = MagicMock()
    r.status_code = code
    r.ok = code < 400
    r.json.return_value = body if body is not None else {"listenKey": "LK1"}
    if code >= 400:
        r.raise_for_status.side_effect = requests.exceptions.HTTPError(f"{code}")
    return r


@pytest.fixture
def sess(monkeypatch):
    m = MagicMock()
    monkeypatch.setattr(api._SESSION, "request", m)
    monkeypatch.setattr(api, "_listen_key_backup_proxies", lambda: BAK)
    monkeypatch.setattr(api, "_retry", lambda fn, *a, **k: fn())     # 隔离 _retry 的重建 Session 逻辑
    return m


def _proxies(m):
    return [c.kwargs.get("proxies") for c in m.call_args_list]


def test_main_ok_no_switch(sess):
    sess.return_value = _resp()
    assert api.get_listen_key() == "LK1"
    assert api.keepalive_listen_key("LK1") is True
    assert _proxies(sess) == [None, None]


@pytest.mark.parametrize("exc", [requests.exceptions.SSLError, requests.exceptions.ProxyError,
                                 requests.exceptions.ConnectionError])
def test_connect_fail_switches_backup(sess, exc):
    sess.side_effect = [exc("SSLZeroReturnError / SOCKS failure"), _resp()]
    assert api.get_listen_key() == "LK1"
    assert _proxies(sess) == [None, BAK]


def test_keepalive_switches_backup(sess):
    sess.side_effect = [requests.exceptions.ProxyError("SOCKS failure"), _resp()]
    assert api.keepalive_listen_key("LK1") is True
    assert _proxies(sess) == [None, BAK]


def test_both_fail_warning(sess, caplog):
    sess.side_effect = [requests.exceptions.ProxyError("m"), requests.exceptions.SSLError("b")]
    with caplog.at_level(logging.WARNING):
        assert api.keepalive_listen_key("LK1") is False
    assert "主备代理均连接失败" in caplog.text
    sess.side_effect = [requests.exceptions.ProxyError("m"), requests.exceptions.SSLError("b")]
    with pytest.raises(requests.exceptions.ConnectionError):
        api.get_listen_key()


def test_http_error_no_switch(sess):
    sess.return_value = _resp(400, {"code": -1022, "msg": "bad sig"})
    with pytest.raises(Exception, match="BinanceError code=-1022"):
        api.get_listen_key()
    sess.return_value = _resp(503)
    assert api.keepalive_listen_key("LK1") is False
    assert _proxies(sess) == [None, None]


def test_no_backup_no_retry(sess, monkeypatch):
    monkeypatch.setattr(api, "_listen_key_backup_proxies", lambda: {})
    sess.side_effect = requests.exceptions.ProxyError("down")
    assert api.keepalive_listen_key("LK1") is False
    assert _proxies(sess) == [None]


def test_not_sticky(sess):
    sess.side_effect = [requests.exceptions.ProxyError("down"), _resp(), _resp()]
    api.keepalive_listen_key("LK1")
    api.keepalive_listen_key("LK1")
    assert _proxies(sess) == [None, BAK, None]


@pytest.mark.parametrize("raw,expect", [
    ("socks5://u:p@1.2.3.4:6688", "socks5h://u:p@1.2.3.4:6688"),
    ("SOCKS5://u:p@1.2.3.4:6688", "socks5h://u:p@1.2.3.4:6688"),
    ("socks5h://u:p@1.2.3.4:6688", "socks5h://u:p@1.2.3.4:6688"),
    ("http://1.2.3.4:8080", "http://1.2.3.4:8080"),
])
def test_backup_socks5_normalized(monkeypatch, raw, expect):
    import config
    monkeypatch.setattr(config, "PROXY_URL_BACKUP", raw, raising=False)
    assert api._listen_key_backup_proxies() == {"http": expect, "https": expect}


def test_backup_empty(monkeypatch):
    import config
    monkeypatch.setattr(config, "PROXY_URL_BACKUP", "", raising=False)
    assert api._listen_key_backup_proxies() == {}
