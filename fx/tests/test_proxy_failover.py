"""clawby_adapter 代理主备切换测试：全部 mock，不联网。"""
import logging
from unittest.mock import MagicMock

import pytest
import requests

from shared import clawby_adapter as ca

MAIN = {"http": "socks5h://main:1", "https": "socks5h://main:1"}
BAK = {"http": "socks5h://bak:2", "https": "socks5h://bak:2"}


def _resp(code=200):
    r = MagicMock()
    r.status_code = code
    return r


@pytest.fixture
def req(monkeypatch):
    monkeypatch.setattr(ca, "_PROXIES", MAIN)
    monkeypatch.setattr(ca, "_PROXIES_BACKUP", BAK)
    m = MagicMock()
    monkeypatch.setattr(ca.requests, "request", m)
    return m


def _proxies(m):
    return [c.kwargs["proxies"] for c in m.call_args_list]


def test_main_ok_no_switch(req):
    req.return_value = _resp()
    assert ca._request("GET", "http://x").status_code == 200
    assert _proxies(req) == [MAIN]


@pytest.mark.parametrize("exc", [requests.exceptions.ConnectionError, requests.exceptions.ProxyError,
                                 requests.exceptions.ConnectTimeout])
def test_connect_fail_switches_to_backup(req, exc):
    req.side_effect = [exc("down"), _resp()]
    assert ca._request("GET", "http://x").status_code == 200
    assert _proxies(req) == [MAIN, BAK]


def test_both_fail_warn_and_raise(req, caplog):
    req.side_effect = [requests.exceptions.ProxyError("m"), requests.exceptions.ConnectionError("b")]
    with caplog.at_level(logging.WARNING), pytest.raises(requests.exceptions.ConnectionError):
        ca._request("GET", "http://x")
    assert "主备代理均连接失败" in caplog.text


def test_http_error_no_switch(req):
    req.return_value = _resp(503)
    assert ca._request("GET", "http://x").status_code == 503
    assert _proxies(req) == [MAIN]


def test_read_timeout_no_switch(req):
    req.side_effect = requests.exceptions.ReadTimeout("slow")
    with pytest.raises(requests.exceptions.ReadTimeout):
        ca._request("GET", "http://x")
    assert _proxies(req) == [MAIN]


def test_no_backup_no_retry(req, monkeypatch):
    monkeypatch.setattr(ca, "_PROXIES_BACKUP", {})
    req.side_effect = requests.exceptions.ConnectionError("down")
    with pytest.raises(requests.exceptions.ConnectionError):
        ca._request("GET", "http://x")
    assert _proxies(req) == [MAIN]


def test_not_sticky_on_backup(req):
    req.side_effect = [requests.exceptions.ConnectionError("down"), _resp(), _resp()]
    ca._request("GET", "http://x")
    ca._request("GET", "http://y")
    assert _proxies(req) == [MAIN, BAK, MAIN]


def test_cg_get_uses_failover(req, monkeypatch):
    monkeypatch.setattr(ca, "_wait_cg_rate_limit", lambda: None)
    ok = _resp()
    ok.json.return_value = {"code": "0", "data": [1, 2]}
    req.side_effect = [requests.exceptions.ProxyError("m"), ok]
    assert ca.cg_get("/api/x") == [1, 2]
    assert _proxies(req) == [MAIN, BAK]


def test_no_direct_requests_calls_left():
    import inspect
    src = inspect.getsource(ca)
    assert "requests.get(" not in src and "requests.post(" not in src
    # 只有 _request 内部直接带代理参数，其余请求点都走 _request
    body = inspect.getsource(ca._request)
    assert src.count("proxies=_PROXIES") == body.count("proxies=_PROXIES") == 2
