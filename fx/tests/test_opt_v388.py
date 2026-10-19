"""运行：cd fx && python3 -m unittest tests.test_opt_v388 -v"""
import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from shared.opt_v388 import liq_relative_strength, liq_weight, tp1_buffer_atr
import shared.calc_sl_tp as cst


class TestRelativeStrength(unittest.TestCase):
    def test_btc_vs_alt(self):
        self.assertAlmostEqual(liq_relative_strength(5e6, 3e9), 5e6 / 3e9)
        self.assertAlmostEqual(liq_relative_strength(5e6, 5e6), 1.0)

    def test_missing_returns_none(self):
        self.assertIsNone(liq_relative_strength(0, 1e6))
        self.assertIsNone(liq_relative_strength(1e6, 0))
        self.assertIsNone(liq_relative_strength(None, None))


class TestWeight(unittest.TestCase):
    def test_examples(self):
        self.assertAlmostEqual(liq_weight(0.05), 1.0)
        self.assertAlmostEqual(liq_weight(0.10), 2.0)
        self.assertAlmostEqual(liq_weight(0.30), 3.0)
        self.assertAlmostEqual(liq_weight(0.001), 0.5)

    def test_unknown_keeps_original(self):
        self.assertEqual(liq_weight(None), 2.0)


class TestTp1Buffer(unittest.TestCase):
    def test_tiers(self):
        self.assertEqual(tp1_buffer_atr(0.25), 0.20)
        self.assertEqual(tp1_buffer_atr(0.20), 0.20)
        self.assertEqual(tp1_buffer_atr(0.10), 0.35)
        self.assertEqual(tp1_buffer_atr(0.05), 0.35)
        self.assertEqual(tp1_buffer_atr(0.01), 0.50)

    def test_unknown_keeps_legacy(self):
        self.assertEqual(tp1_buffer_atr(None), 0.30)


class TestFlagOffUnchanged(unittest.TestCase):
    """开关关闭时，回踩入场价与 TP1 必须与原公式完全一致。"""
    def test_entry_and_tp_unchanged(self):
        import config
        self.assertFalse(config.OPT_LIQ_RELATIVE_STRENGTH)
        k = [[0, 100, 101, 99, 100, 1000, 0, 0, 0, 0, 0, 0] for _ in range(30)]
        liq_long = [{"price_low": 97.0, "price_high": 98.0, "liq_usd": 9e6}]
        liq_short = [{"price_low": 110.0, "price_high": 111.0, "liq_usd": 9e6}]
        a = cst._entry_pullback(100.0, 2.0, 99.0, liq_long, liq_short, "LONG", k, vol24_usd=1e6)
        b = cst._entry_pullback(100.0, 2.0, 99.0, liq_long, liq_short, "LONG", k)
        self.assertEqual(a, b)
        t1 = cst._calc_tp(100.0, 96.0, 2.0, "LONG", liq_long, liq_short, vol24_usd=1e6)
        t2 = cst._calc_tp(100.0, 96.0, 2.0, "LONG", liq_long, liq_short)
        self.assertEqual(t1, t2)
        self.assertAlmostEqual(t1[0], 110.0 - 2.0 * 0.3)


if __name__ == "__main__":
    unittest.main()
