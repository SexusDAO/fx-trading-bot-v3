"""测试公共夹具。"""
import pytest

# [v2-7] 撤单结果未知时 signal_watcher 会后台查持仓补止损；
# 以下旧撤单测试只 mock 了撤单接口，这里把查持仓/补止损也挡掉，避免真实联网。
_GUARD_FILES = {"test_cancel_paths.py", "test_cancel_state_batch1.py"}


@pytest.fixture(autouse=True)
def _mock_unknown_fill_guard(request, monkeypatch):
    if request.node.fspath.basename in _GUARD_FILES:
        from shared import binance_api as _api
        monkeypatch.setattr(_api, "get_position", lambda *a, **k: [], raising=False)
        monkeypatch.setattr(_api, "place_sl", lambda *a, **k: {}, raising=False)
        # [hp-3] add_signal 注册前盘口粗筛：旧测试未 mock，这里给足深度避免联网
        monkeypatch.setattr(_api, "get_order_book",
                            lambda *a, **k: {"asks": [["1", "1e12"]], "bids": [["1", "1e12"]]}, raising=False)
        monkeypatch.setattr(_api, "get_mark_price", lambda *a, **k: {"markPrice": "1"}, raising=False)
    yield
