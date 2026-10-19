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


class TestDenominators(unittest.TestCase):
    """[v3.88 OPT-14d] 三种分母与记录字段。"""
    def test_concentration(self):
        from shared.opt_v388 import liq_concentration
        zones = [{"liq_usd": 3e6}, {"liq_usd": 1e6}]
        self.assertAlmostEqual(liq_concentration(3e6, zones), 0.75)
        self.assertEqual(liq_concentration(5e4, [{"liq_usd": 5e4}]), 1.0)   # 单区恒为 1，已在文档注明
        self.assertIsNone(liq_concentration(1e6, []))

    def test_oi_requires_usd(self):
        from shared.opt_v388 import oi_relative
        self.assertAlmostEqual(oi_relative(1e6, 2e7), 0.05)
        self.assertIsNone(oi_relative(1e6, 0))

    def test_relative_by_denom(self):
        from shared.opt_v388 import relative_by_denom
        z = [{"liq_usd": 1e6}, {"liq_usd": 1e6}]
        self.assertAlmostEqual(relative_by_denom("vol24", 1e6, z, 1e7, 4e7), 0.1)
        self.assertAlmostEqual(relative_by_denom("oi", 1e6, z, 1e7, 4e7), 0.025)
        self.assertAlmostEqual(relative_by_denom("concentration", 1e6, z, 1e7, 4e7), 0.5)

    def test_metrics_record(self):
        from shared.opt_v388 import liq_metrics
        z = {"liq_usd": 2e6}
        r = liq_metrics(z, [z, {"liq_usd": 2e6}], 1e7, 4e7)
        self.assertEqual(r["rel_vol24"], 0.2)
        self.assertEqual(r["rel_oi"], 0.05)
        self.assertEqual(r["concentration"], 0.5)
        self.assertEqual(liq_metrics(None, [], 1, 1), {})


class TestTp1Lock(unittest.TestCase):
    """[v3.88 OPT-14e] 开关开启时，重算带入锁定缓冲则沿用；记录实际采用的缓冲。"""
    def setUp(self):
        import config
        self.cfg = config
        self.orig = config.OPT_LIQ_RELATIVE_STRENGTH
        config.OPT_LIQ_RELATIVE_STRENGTH = True

    def tearDown(self):
        self.cfg.OPT_LIQ_RELATIVE_STRENGTH = self.orig

    def test_locked_buffer_used(self):
        liq_short = [{"price_low": 110.0, "price_high": 111.0, "liq_usd": 9e6}]
        ctx = {"tp1_buffer_locked": 0.2}
        tp1, _, deg = cst._calc_tp(100.0, 96.0, 2.0, "LONG", [], liq_short, liq_ctx=ctx)
        self.assertAlmostEqual(tp1, 110.0 - 0.4)
        self.assertFalse(deg)
        self.assertEqual(ctx["tp1_buffer_used"], 0.2)

    def test_dynamic_buffer_recorded(self):
        liq_short = [{"price_low": 110.0, "price_high": 111.0, "liq_usd": 3e6}]
        ctx = {"vol24_usd": 1e7, "oi_usd": 0.0}           # 相对强度 30% → 强档 0.2ATR
        tp1, _, _ = cst._calc_tp(100.0, 96.0, 2.0, "LONG", [], liq_short, liq_ctx=ctx)
        self.assertAlmostEqual(tp1, 110.0 - 0.4)
        self.assertEqual(ctx["tp1_buffer_used"], 0.2)

    def test_missing_data_falls_back(self):
        liq_short = [{"price_low": 110.0, "price_high": 111.0, "liq_usd": 3e6}]
        ctx = {}
        tp1, _, _ = cst._calc_tp(100.0, 96.0, 2.0, "LONG", [], liq_short, liq_ctx=ctx)
        self.assertAlmostEqual(tp1, 110.0 - 0.6)


class TestSingleZoneFlag(unittest.TestCase):
    """[v3.88 OPT-14d] 单区标记，且三种分母互相独立。"""
    def test_flag(self):
        from shared.opt_v388 import liq_metrics
        z = {"liq_usd": 1e6}
        self.assertFalse(liq_metrics(z, [z], 1e7, 4e7)["concentration_valid"])
        self.assertTrue(liq_metrics(z, [z, z], 1e7, 4e7)["concentration_valid"])

    def test_independent(self):
        from shared.opt_v388 import liq_metrics
        z = {"liq_usd": 1e6}
        r = liq_metrics(z, [z, z], 0, 4e7)        # 成交额缺失
        self.assertIsNone(r["rel_vol24"])
        self.assertEqual(r["rel_oi"], 0.025)
        self.assertEqual(r["concentration"], 0.5)
        r = liq_metrics(z, [z, z], 1e7, 0)        # OI 缺失
        self.assertIsNone(r["rel_oi"])
        self.assertEqual(r["rel_vol24"], 0.1)
