"""影子追踪：记录、计数上限、分类与离线判定。"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("BINANCE_API_KEY", "x")
os.environ.setdefault("BINANCE_API_SECRET", "y")
from shared import shadow_trace as st  # noqa: E402
from tools import shadow_verify as sv  # noqa: E402

T0 = 1_700_000_000_000


def _k(t_min, hi, lo):
    return [T0 + t_min * 60_000, 0, hi, lo, 0]


class TestRecord(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old = st.LOG_PATH
        st.LOG_PATH = Path(self.tmp.name) / "rejected_log.jsonl"
        st.new_round()

    def tearDown(self):
        st.LOG_PATH = self._old
        self.tmp.cleanup()

    def _lines(self):
        return [json.loads(x) for x in st.LOG_PATH.read_text(encoding="utf-8").splitlines()]

    def test_write_fields(self):
        self.assertTrue(st.record(symbol="ETHUSDT", signal_type="pullback", direction="long",
                                  first_rejected_at="judge_pullback", entry_candidate=100, atr=2, stage="A"))
        r = self._lines()[0]
        self.assertEqual(r["direction"], "LONG")
        self.assertTrue(r["would_pass_all"])
        self.assertIsNone(r["sl"])

    def test_cap(self):
        for _ in range(st.CAP_READONLY + 5):
            st.record(symbol="X", signal_type="pullback", direction="LONG", first_rejected_at="a")
        self.assertEqual(len(self._lines()), st.CAP_READONLY)
        st.new_round()
        self.assertTrue(st.record(symbol="X", signal_type="pullback", direction="LONG", first_rejected_at="a"))

    def test_would_pass_all(self):
        self.assertTrue(st.would_pass_all([]))
        self.assertFalse(st.would_pass_all(["precheck:liquidity_gate"]))
        self.assertIsNone(st.would_pass_all([st.SKIP_LLM]))

    def test_categorize(self):
        self.assertEqual(st.categorize_scanner_reason("利空新闻一票否决 LONG"), "news_veto")
        self.assertEqual(st.categorize_scanner_reason("LLM新闻语义: caution升级为bearish"), "llm_filter")
        self.assertEqual(st.categorize_scanner_reason("多单槽位已满"), "slot_limit")
        self.assertEqual(st.categorize_scanner_reason("相关性过滤: LONG 合计3>=3"), "corr_filter")
        self.assertEqual(st.categorize_precheck_reason("流动性不足: 深度 100U"), "liquidity_gate")

    def test_scanner_rejections_llm_skip(self):
        st.probe_liquidity = lambda sig: []   # 不访问网络
        n = st.record_scanner_rejections(
            {"rejected": [{"symbol": "A", "reason": "LLM新闻语义: 一票否决"}, {"symbol": "ZZZ", "reason": "x"}]},
            [{"symbol": "A", "mode": "breakout", "direction": "LONG", "pre_entry_price": 10}])
        self.assertEqual(n, 1)                 # 未通过判定的 ZZZ 不记录
        r = self._lines()[0]
        self.assertEqual(r["first_rejected_at"], "llm_filter")
        self.assertEqual(r["would_also_fail"], [st.SKIP_LLM])
        self.assertIsNone(r["would_pass_all"])

    def test_disabled(self):
        import config
        old = getattr(config, "SHADOW_TRACE_ENABLED", True)
        config.SHADOW_TRACE_ENABLED = False
        try:
            self.assertFalse(st.record(symbol="X", signal_type="p", direction="LONG", first_rejected_at="a"))
        finally:
            config.SHADOW_TRACE_ENABLED = old


class TestVerify(unittest.TestCase):
    def rec(self, **kw):
        base = {"ts": T0, "signal_type": "breakout", "direction": "LONG", "entry_candidate": 100.0,
                "sl": 95.0, "tp1": 110.0, "atr": 3.0}
        base.update(kw)
        return base

    def test_tp_first(self):
        r = sv.label_record(self.rec(), [_k(0, 101, 99), _k(5, 111, 100)])
        self.assertEqual(r["label"], "先止盈")

    def test_sl_first(self):
        r = sv.label_record(self.rec(), [_k(0, 101, 99), _k(5, 100, 94)])
        self.assertEqual(r["label"], "先止损")

    def test_unfilled(self):
        r = sv.label_record(self.rec(), [_k(0, 105, 101), _k(35, 99, 97)])   # 30 分钟内没到 100
        self.assertEqual(r["label"], "未成交")

    def test_same_bar(self):
        r = sv.label_record(self.rec(), [_k(0, 112, 94)])
        self.assertEqual(r["label"], "无法判定")

    def test_timeout(self):
        bars = [_k(0, 101, 99)] + [_k(m, 102, 98) for m in range(5, 8 * 60 + 10, 5)]
        self.assertEqual(sv.label_record(self.rec(), bars)["label"], "超时")

    def test_window_not_over(self):
        self.assertEqual(sv.label_record(self.rec(), [_k(0, 101, 99), _k(5, 102, 98)])["why"], "结果窗口尚未结束")

    def test_fallback_atr(self):
        r = sv.label_record(self.rec(sl=None, tp1=None), [_k(0, 101, 99), _k(5, 103.5, 100)])
        self.assertEqual(r["label"], "先止盈")
        self.assertTrue(r["fallback"])

    def test_short(self):
        r = sv.label_record(self.rec(direction="SHORT", sl=105.0, tp1=90.0), [_k(0, 101, 99), _k(5, 100, 89)])
        self.assertEqual(r["label"], "先止盈")

    def test_stop_hunt_fill_90(self):
        r = sv.label_record(self.rec(signal_type="stop_hunt", sl=99.0, tp1=101.0),
                            [_k(0, 105, 102), _k(80, 100.5, 99.5), _k(85, 101.5, 100)])
        self.assertEqual(r["label"], "先止盈")       # 第 80 分钟成交仍在 90 分钟成交窗口内


if __name__ == "__main__":
    unittest.main()
