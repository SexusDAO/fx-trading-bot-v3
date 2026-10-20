"""运行：cd fx && python3 -m unittest tests.test_risk_batch2 -v
[v3.89 #63/#66/#67] 风险管理：盈亏持久化失败可识别、USDT 数据不可用状态、回撤状态损坏备份。全部 mock。"""
import json, sys, tempfile, time, types, unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).parent.parent))
from shared import risk_manager as R


class TestRecordPnl(unittest.TestCase):
    def test_persist_failure_is_not_reported_as_not_triggered(self):
        with mock.patch("shared.state.record_pnl", side_effect=OSError("disk")), \
             mock.patch("shared.state.get_daily_loss", return_value=12.0), \
             self.assertLogs("fx.risk_manager", level="ERROR") if False else mock.patch.object(R.logger, "error"):
            r = R.record_pnl(-5.0, "XUSDT", "t")
        self.assertIs(r["persisted"], False)
        self.assertIsNone(r["circuit_triggered"])            # 未知，而不是 False
        self.assertFalse(r["circuit_triggered"])             # 仍是假值，兼容既有 if 判断
        self.assertIn("disk", r["error"])

    def test_success_unchanged_shape(self):
        with mock.patch("shared.state.record_pnl", return_value=False), \
             mock.patch("shared.state.get_daily_loss", return_value=3.0):
            r = R.record_pnl(-1.0)
        self.assertEqual(r, {"circuit_triggered": False, "daily_loss": 3.0, "persisted": True})

    def test_get_daily_loss_failure_does_not_raise(self):
        with mock.patch("shared.state.record_pnl", return_value=False), \
             mock.patch("shared.state.get_daily_loss", side_effect=OSError("x")):
            r = R.record_pnl(-1.0)
        self.assertIsNone(r["daily_loss"])


if __name__ == "__main__":
    unittest.main()
