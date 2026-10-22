"""tools/log_query.py --funnel --last Nh 测试：只读本地临时文件。"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import log_query as lq   # noqa: E402

NOW = 1_800_000_000_000


def _rec(age_h, coarse, rej, pas):
    return {"ts": NOW - int(age_h * 3600_000), "coarse_by_route": coarse,
            "reject_by_route": rej, "pass_by_route": pas}


@pytest.fixture
def logdir(tmp_path):
    lines = [
        _rec(1, {"pullback": 50, "reversal": 10}, {"pullback": {"量能不足": 30, "波动率": 10}},
             {"pullback": 4}),
        _rec(2, {"pullback": 37, "reversal": 13}, {"pullback": {"量能不足": 15, "波动率": 13, "其他": 12},
                                                   "reversal": {"RSI条件": 18, "EMA结构": 3}},
             {"pullback": 3, "reversal": 2}),
        _rec(30, {"pullback": 999}, {}, {}),            # 超出时间段
    ]
    (tmp_path / "funnel_log.jsonl").write_text(
        "\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\nnot-json\n", encoding="utf-8")
    return tmp_path


def test_aggregate_last_hours(logdir):
    out = lq.funnel_report(logdir, 6, now_ms=NOW)
    assert out[0] == "[funnel] 最近 6h 共 2 轮扫描"
    pb = [l for l in out if l.startswith("[pullback]")][0]
    assert "初筛命中=87次" in pb and "量能不足=45" in pb and "波动率=23" in pb and "其他=12" in pb
    assert pb.endswith("精筛通过=7")
    assert pb.index("量能不足") < pb.index("波动率") < pb.index("其他")   # 按次数降序
    rv = [l for l in out if l.startswith("[reversal]")][0]
    assert "初筛命中=23次" in rv and "RSI条件=18" in rv and "EMA结构=3" in rv and rv.endswith("精筛通过=2")


def test_no_last_reads_all(logdir):
    out = lq.funnel_report(logdir, None, now_ms=NOW)
    assert "共 3 轮" in out[0]
    assert "初筛命中=1086次" in [l for l in out if l.startswith("[pullback]")][0]


def test_missing_file(tmp_path):
    assert "未找到" in lq.funnel_report(tmp_path, 6)[0]


@pytest.mark.parametrize("s,v", [("6h", 6.0), ("1.5h", 1.5), ("12", 12.0)])
def test_parse_hours(s, v):
    assert lq._parse_hours(s) == v


def test_cli(logdir):
    p = subprocess.run([sys.executable, str(Path(lq.__file__)), "--funnel", "--last", "100000h",
                        "--log-dir", str(logdir)], capture_output=True, text=True, timeout=30)
    assert p.returncode == 0 and "[pullback]" in p.stdout and "初筛命中=" in p.stdout
    bad = subprocess.run([sys.executable, str(Path(lq.__file__)), "--funnel", "--last", "abc",
                          "--log-dir", str(logdir)], capture_output=True, text=True, timeout=30)
    assert bad.returncode == 2
