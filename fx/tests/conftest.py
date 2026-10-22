"""测试公共夹具。"""
import pytest

# [v2-7] 撤单结果未知时 signal_watcher 会后台查持仓补止损；
# 以下旧撤单测试只 mock 了撤单接口，这里把查持仓/补止损也挡掉，避免真实联网。
_GUARD_FILES = {"test_cancel_paths.py", "test_cancel_state_batch1.py"}

# [hp-3] add_signal 注册前盘口粗筛会请求 get_order_book / get_mark_price；
# 以下旧测试未 mock，这里给正常深度，避免真实联网。
_DEPTH_FILES = {"test_notifier_flush_timing.py"}


def _normal_book(*a, **k):
    """标记价 100 附近 ±0.5% 内买卖各 5 档，每档 10000U（测试文件 tg 夹具把 markPrice 设为 100）。"""
    asks = [[f"{100 + i * 0.05:.2f}", f"{10000 / (100 + i * 0.05):.6f}"] for i in range(5)]
    bids = [[f"{100 - i * 0.05:.2f}", f"{10000 / (100 - i * 0.05):.6f}"] for i in range(5)]
    return {"asks": asks, "bids": bids}


@pytest.fixture(autouse=True)
def _mock_unknown_fill_guard(request, monkeypatch):
    name = request.node.fspath.basename
    if name in _GUARD_FILES:
        from shared import binance_api as _api
        monkeypatch.setattr(_api, "get_position", lambda *a, **k: [], raising=False)
        monkeypatch.setattr(_api, "place_sl", lambda *a, **k: {}, raising=False)
        monkeypatch.setattr(_api, "get_order_book",
                            lambda *a, **k: {"asks": [["1", "1e12"]], "bids": [["1", "1e12"]]}, raising=False)
        monkeypatch.setattr(_api, "get_mark_price", lambda *a, **k: {"markPrice": "1"}, raising=False)
    if name in _DEPTH_FILES:
        from shared import binance_api as _api
        monkeypatch.setattr(_api, "get_order_book", _normal_book, raising=False)
        # 注册前查交易所真实持仓/挂单（fdde5ff4 引入）
        monkeypatch.setattr(_api, "get_position", lambda *a, **k: [], raising=False)
        monkeypatch.setattr(_api, "get_open_orders", lambda *a, **k: [], raising=False)
        monkeypatch.setattr(_api, "get_mark_price",
                            lambda *a, **k: {"markPrice": "100", "indexPrice": "100", "time": 0}, raising=False)
    yield
