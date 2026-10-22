"""配置热重载测试：全部 mock，不联网。"""
import os
import time

import pytest

import config
from shared import hot_config as hc


@pytest.fixture
def tun(tmp_path):
    hc._reset_for_tests()
    p = tmp_path / "config_tunable.py"
    yield p
    hc._reset_for_tests()


def _write(p, text):
    p.write_text(text, encoding="utf-8")
    t = time.time() + 1 + len(text) * 1e-3
    os.utime(p, (t, t))


def test_change_written_back(tun):
    orig = config.TRIGGER_COOL_PULLBACK
    _write(tun, f"TRIGGER_COOL_PULLBACK = {orig + 111}\n")
    assert hc.check_once(tun)
    assert config.TRIGGER_COOL_PULLBACK == orig + 111
    assert hc.get("TRIGGER_COOL_PULLBACK") == orig + 111
    _write(tun, f"TRIGGER_COOL_PULLBACK = {orig + 222}\n")
    assert hc.check_once(tun)
    assert config.TRIGGER_COOL_PULLBACK == orig + 222
    assert hc.check_once(tun) is False          # mtime 未变不重载


def test_failure_keeps_old(tun, caplog):
    orig = config.EMA_TREND_RSI4H_MAX
    _write(tun, f"EMA_TREND_RSI4H_MAX = {orig + 1}\n")
    hc.check_once(tun)
    _write(tun, "EMA_TREND_RSI4H_MAX = = 3\n")
    with caplog.at_level("WARNING"):
        hc.check_once(tun)
    assert config.EMA_TREND_RSI4H_MAX == orig + 1
    assert "加载失败，保留旧值" in caplog.text


def test_removed_restores_original(tun):
    o1, o2 = config.TRIGGER_COOL_REVERSAL, config.STOP_HUNT_VOL_RATIO_MIN
    _write(tun, f"TRIGGER_COOL_REVERSAL = {o1 + 5}\nSTOP_HUNT_VOL_RATIO_MIN = {o2 + 1}\n")
    hc.check_once(tun)
    _write(tun, f"STOP_HUNT_VOL_RATIO_MIN = {o2 + 1}\n")
    hc.check_once(tun)
    assert config.TRIGGER_COOL_REVERSAL == o1
    assert config.STOP_HUNT_VOL_RATIO_MIN == o2 + 1


def test_unknown_param_rejected(tun, caplog):
    _write(tun, "NOT_IN_CONFIG_XYZ = 1\nlower_case = 2\n")
    with caplog.at_level("WARNING"):
        hc.check_once(tun)
    assert not hasattr(config, "NOT_IN_CONFIG_XYZ") and not hasattr(config, "lower_case")
    assert "忽略未知参数 NOT_IN_CONFIG_XYZ" in caplog.text


def test_shipped_tunable_matches_config():
    from pathlib import Path
    p = Path(hc.__file__).resolve().parent.parent / "config_tunable.py"
    ns = {}
    exec(compile(p.read_text(encoding="utf-8"), str(p), "exec"), ns)
    keys = [k for k in ns if k.isupper()]
    assert keys and all(getattr(config, k) == ns[k] for k in keys)   # 上线行为不变
    for k in ("PULLBACK_SHORT_VOL_RATIO_MIN", "PULSE_CANDLE_VOL_RATIO_1H", "PULSE_CANDLE_VOL_RATIO_4H"):
        assert k not in keys                                        # 模块加载即读入，不收录


def test_start_stop_thread(tun):
    orig = config.TRIGGER_COOL_STOP_HUNT
    _write(tun, f"TRIGGER_COOL_STOP_HUNT = {orig + 7}\n")
    hc.start(interval=0.05, path=tun)
    assert config.TRIGGER_COOL_STOP_HUNT == orig + 7
    _write(tun, f"TRIGGER_COOL_STOP_HUNT = {orig + 9}\n")
    time.sleep(0.3)
    hc.stop()
    assert config.TRIGGER_COOL_STOP_HUNT == orig + 9
