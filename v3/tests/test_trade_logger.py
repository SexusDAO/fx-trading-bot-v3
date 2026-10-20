"""tests/test_trade_logger.py"""
import sys, json
from pathlib import Path
sys.path.insert(0, "/root/.openclaw/workspace/v3")
from modules.trade_logger import log_open, log_close, daily_summary, LOG_FILE

def _clear_log():
    if LOG_FILE.exists():
        LOG_FILE.unlink()

# ── log_open ──────────────────────────────────────────────────────────────────

def test_log_open():
    _clear_log()
    r = log_open(
        symbol="BTCUSDT", direction="LONG",
        entry_price=65000, notional=500,
        signal_type="trend", score=82,
        score_details={"ema": 28, "rsi": 18, "vol": 16, "struct": 20},
        entry_mode="DUAL_NET",
        funding_rate=-0.001, btc_trend="bull",
        sl_price=63000, tp1_price=68000, tp2_price=71000,
    )
    assert LOG_FILE.exists()
    assert r["event"] == "open"
    assert r["symbol"] == "BTCUSDT"
    assert r["score"] == 82
    assert "open_time" in r
    print(f"✅ log_open 写入: {r['key']} score={r['score']}")

def test_log_open_score_details_persisted():
    lines = LOG_FILE.read_text().strip().split("\n")
    record = json.loads(lines[-1])
    assert record["score_details"]["ema"] == 28
    print("✅ score_details 完整保存")

# ── log_close ─────────────────────────────────────────────────────────────────

def test_log_close_pnl_long():
    r = log_close(
        symbol="BTCUSDT", direction="LONG",
        close_price=68000, close_reason="tp1",
        entry_price=65000, notional=500,
    )
    assert r["pnl_usdt"] > 0
    assert r["pnl_pct"] > 0
    assert r["close_reason"] == "tp1"
    print(f"✅ log_close 盈利 pnl={r['pnl_usdt']}U ({r['pnl_pct']}%)")

def test_log_close_pnl_short_profit():
    r = log_close(
        symbol="ETHUSDT", direction="SHORT",
        close_price=3000, close_reason="tp1",
        entry_price=3200, notional=300,
    )
    assert r["pnl_usdt"] > 0
    expected_pct = (3200 - 3000) / 3200 * 100
    assert abs(r["pnl_pct"] - round(expected_pct, 3)) < 0.01
    print(f"✅ log_close 空单盈利 pnl={r['pnl_usdt']}U ({r['pnl_pct']}%)")

def test_log_close_pnl_loss():
    r = log_close(
        symbol="XUSDT", direction="LONG",
        close_price=60000, close_reason="sl",
        entry_price=65000, notional=500,
    )
    assert r["pnl_usdt"] < 0
    print(f"✅ log_close 亏损 pnl={r['pnl_usdt']}U")

def test_log_close_avg_price():
    """补仓后均价影响 PnL"""
    r = log_close(
        symbol="SOLSDT", direction="LONG",
        close_price=105, close_reason="manual",
        entry_price=100, notional=500,
        avg_price=98, add_count=1,
    )
    expected = (105 - 98) / 98 * 500
    assert abs(r["pnl_usdt"] - round(expected, 2)) < 0.01
    print(f"✅ log_close 含补仓均价 pnl={r['pnl_usdt']}U")

# ── daily_summary ─────────────────────────────────────────────────────────────

def test_daily_summary():
    _clear_log()
    today = __import__("modules.trade_logger", fromlist=["_today_str"])._today_str()

    # 3 胜 2 负
    log_close("A", "LONG",  110, "tp1",   100, 300)   # +30
    log_close("B", "LONG",  105, "tp2",   100, 500)   # +25
    log_close("C", "SHORT", 90,  "tp1",   100, 400)   # +40 (空单)
    log_close("D", "LONG",  95,  "sl",    100, 300)   # -15
    log_close("E", "LONG",  97,  "sl",    100, 200)   # -6

    s = daily_summary(today)
    assert s["total_trades"] == 5, f"期望5，得{s['total_trades']}"
    assert s["wins"] == 3
    assert s["losses"] == 2
    assert abs(s["win_rate"] - 0.6) < 0.01
    assert s["total_pnl_usdt"] > 0
    assert s["consecutive_losses"] == 2
    print(f"✅ daily_summary: {s['wins']}胜{s['losses']}负 "
          f"pnl={s['total_pnl_usdt']}U winrate={s['win_rate']:.0%}")

def test_daily_summary_by_reason():
    s = daily_summary()
    assert "sl" in s["by_reason"]
    assert s["by_reason"]["sl"] == 2
    assert s["by_reason"]["tp1"] == 2
    print(f"✅ by_reason: {s['by_reason']}")

def test_daily_summary_empty():
    s = daily_summary("2000-01-01")
    assert s["total_trades"] == 0
    assert s["win_rate"] == 0
    print("✅ 空日期统计正常")

def test_consecutive_losses():
    _clear_log()
    log_close("A", "LONG", 90, "sl", 100, 300)   # loss
    log_close("B", "LONG", 88, "sl", 100, 300)   # loss
    log_close("C", "LONG", 85, "sl", 100, 300)   # loss
    s = daily_summary()
    assert s["consecutive_losses"] == 3
    print(f"✅ 连续亏损计数 = {s['consecutive_losses']}")

if __name__ == "__main__":
    test_log_open()
    test_log_open_score_details_persisted()
    test_log_close_pnl_long()
    test_log_close_pnl_short_profit()
    test_log_close_pnl_loss()
    test_log_close_avg_price()
    test_daily_summary()
    test_daily_summary_by_reason()
    test_daily_summary_empty()
    test_consecutive_losses()
    print("\n🎉 trade_logger 全部测试通过")
