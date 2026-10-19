"""运行：cd fx && python3 -m unittest tests.test_opt_v387_step6 -v
[v3.87 STEP6] 止损数量取整、入场复核规则。"""
import asyncio, sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
import executor as ex


class TestRoundSumQty(unittest.TestCase):
    def test_float_noise_removed(self):
        self.assertEqual(ex._round_sum_qty(0.1, 0.2, 0.1), 0.3)
        self.assertEqual(str(ex._round_sum_qty(0.1, 0.2, 0.1)), "0.3")

    def test_integer_step(self):
        self.assertEqual(ex._round_sum_qty(60.0, 40.0, 1.0), 100.0)

    def test_fine_step(self):
        self.assertEqual(str(ex._round_sum_qty(0.006, 0.004, 0.001)), "0.01")


class TestEntryRecheck(unittest.TestCase):
    def _run(self, direction, mode, snap):
        return asyncio.run(ex._entry_recheck("XUSDT", direction, mode, 100.0, 1.0, snap, {}))

    def test_short_pump_rule_removed(self):
        snap = {"funding_rate": 0.0, "vol_ratio": 5.0, "entry_price_snap": 90.0, "prev_close": 90.0}
        verdict, _ = self._run("SHORT", "pullback", snap)
        self.assertEqual(verdict, "HOLD")

    def test_extreme_funding_still_closes(self):
        self.assertEqual(self._run("LONG", "pullback", {"funding_rate": 0.006})[0], "CLOSE")
        self.assertEqual(self._run("SHORT", "pullback", {"funding_rate": -0.006})[0], "CLOSE")

    def test_normal_holds(self):
        self.assertEqual(self._run("LONG", "breakout", {"funding_rate": 0.0001})[0], "HOLD")


if __name__ == "__main__":
    unittest.main()
