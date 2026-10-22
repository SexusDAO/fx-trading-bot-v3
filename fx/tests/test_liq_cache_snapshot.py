"""liq_cache 磁盘快照测试：全部 mock，不联网。"""
import json
import os
import threading
import time

import pytest

from shared import liq_cache as lc


@pytest.fixture(autouse=True)
def _iso(monkeypatch, tmp_path):
    with lc._CACHE_LOCK:
        saved = dict(lc._CACHE)
        lc._CACHE.clear()
    path = tmp_path / "logs" / "liq_cache_snapshot.json"
    monkeypatch.setattr(lc, "_snapshot_cfg", lambda: (path, 6 * 3600))
    yield path
    lc.stop_bg_refresh(timeout=2)
    with lc._CACHE_LOCK:
        lc._CACHE.clear()
        lc._CACHE.update(saved)


def _write(path, age_sec, entries=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    entries = entries or {"BTCUSDT:3d": {"data": {"LONG": [{"p": 1}], "SHORT": [], "ts": 1000.0, "src_level": 2},
                                         "ts": 1000.0}}
    path.write_text(json.dumps(entries), encoding="utf-8")
    t = time.time() - age_sec
    os.utime(path, (t, t))


def _run_bg(monkeypatch, wait=0.5):
    calls = []
    done = threading.Event()

    def fake_prefetch(syms):
        calls.append(list(syms))
        with lc._CACHE_LOCK:
            lc._CACHE["ETHUSDT:3d"] = {"LONG": [], "SHORT": [], "ts": time.time(), "src_level": 1}
        done.set()
    monkeypatch.setattr(lc, "prefetch_liq_cache", fake_prefetch)
    lc.start_bg_refresh(lambda: ["ETHUSDT"])
    done.wait(wait)
    time.sleep(0.1)
    lc.stop_bg_refresh(timeout=2)
    return calls


def test_fresh_snapshot_loaded_skip_prefetch(monkeypatch, _iso):
    _write(_iso, age_sec=3600)
    before = time.time()
    calls = _run_bg(monkeypatch)
    assert calls == []                                      # 首轮预热被跳过
    e = lc._CACHE["BTCUSDT:3d"]
    assert e["ts"] >= before and e["src_level"] == 2        # ts 改为加载时间，src_level 保留


def test_stale_snapshot_prefetch_again(monkeypatch, _iso):
    _write(_iso, age_sec=7 * 3600)
    calls = _run_bg(monkeypatch)
    assert calls == [["ETHUSDT"]]
    assert "BTCUSDT:3d" not in lc._CACHE


def test_corrupt_snapshot_falls_back(monkeypatch, _iso, caplog):
    _iso.parent.mkdir(parents=True, exist_ok=True)
    _iso.write_text("{not json", encoding="utf-8")
    with caplog.at_level("WARNING"):
        calls = _run_bg(monkeypatch)
    assert calls == [["ETHUSDT"]]
    assert "快照加载失败" in caplog.text


def test_snapshot_written_after_prefetch(monkeypatch, _iso):
    assert not _iso.exists()
    _run_bg(monkeypatch)
    assert _iso.exists()
    snap = json.loads(_iso.read_text(encoding="utf-8"))
    assert "ETHUSDT:3d" in snap and set(snap["ETHUSDT:3d"]) == {"data", "ts"}
    assert not _iso.with_name(_iso.name + ".tmp").exists()   # 临时文件已替换


def test_periodic_snapshot_loop(monkeypatch, _iso):
    monkeypatch.setattr(lc, "_SNAPSHOT_INTERVAL", 0.05)
    with lc._CACHE_LOCK:
        lc._CACHE["SOLUSDT:24h"] = {"LONG": [], "SHORT": [], "ts": time.time(), "src_level": 1}
    _write(_iso, age_sec=60, entries={"SOLUSDT:24h": {"data": {"LONG": [], "ts": 1}, "ts": 1}})
    monkeypatch.setattr(lc, "prefetch_liq_cache", lambda syms: None)
    lc.start_bg_refresh(lambda: [])
    time.sleep(0.3)
    lc.stop_bg_refresh(timeout=2)
    snap = json.loads(_iso.read_text(encoding="utf-8"))
    assert "SOLUSDT:24h" in snap
