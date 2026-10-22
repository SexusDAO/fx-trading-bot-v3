"""通知去重：一个信号只有一条通知（全部 mock，不联网）。"""
import logging
from unittest.mock import MagicMock

import pytest

from shared import notifier as nt


@pytest.fixture
def io(monkeypatch):
    send = MagicMock(return_value="M1")
    edit = MagicMock(return_value=True)
    notify = MagicMock()
    monkeypatch.setattr(nt, "_send", send)
    monkeypatch.setattr(nt, "_edit", edit)
    monkeypatch.setattr(nt, "notify", notify)
    monkeypatch.setattr(nt, "_save_card_state", lambda *a, **k: None)
    monkeypatch.setattr(nt, "_card_cache", {})
    monkeypatch.setattr(nt, "_signal_buffer", nt._signal_buffer.__class__())
    return send, edit, notify


def test_flush_sends_single_message(io):
    send, edit, notify = io
    nt.card_create("S1", "标题")
    nt.card_append("S1", "进入监控")
    nt.card_append("S1", "价格触发")
    assert nt.flush_signal("S1")
    send.assert_called_once_with("标题\n进入监控\n价格触发")
    edit.assert_not_called()
    notify.assert_not_called()


def test_edit_failure_dropped_no_new_message(io, caplog):
    send, edit, notify = io
    nt.card_create("S2", "标题")
    nt.flush_signal("S2")
    edit.return_value = False
    with caplog.at_level(logging.WARNING):
        assert nt.card_append("S2", "开仓成交") is False
    notify.assert_not_called()
    assert send.call_count == 1
    assert nt._card_cache["S2"]["message_id"] == "M1"   # mid 保留
    assert "编辑失败" in caplog.text


def test_edit_success_appends(io):
    send, edit, notify = io
    nt.card_create("S3", "标题")
    nt.flush_signal("S3")
    assert nt.card_append("S3", "开仓成交")
    edit.assert_called_once_with("M1", "标题\n开仓成交")
    assert send.call_count == 1 and not notify.called


def test_no_card_dropped(io, caplog):
    send, edit, notify = io
    with caplog.at_level(logging.WARNING):
        assert nt.card_append("NOPE", "x") is False
    assert not send.called and not notify.called and not edit.called
    assert "无卡片缓存" in caplog.text


def test_no_message_id_dropped(io, caplog):
    send, edit, notify = io
    send.return_value = None          # Popen 兜底，无 mid
    nt.card_create("S4", "标题")
    nt.flush_signal("S4")
    with caplog.at_level(logging.WARNING):
        assert nt.card_append("S4", "开仓成交") is False
    assert send.call_count == 1 and not notify.called and not edit.called
    assert "无 message_id" in caplog.text
