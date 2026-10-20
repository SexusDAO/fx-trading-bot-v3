#!/usr/bin/env python3
"""
scripts/daily_report.py — 每日交易复盘（UTC 01:00 cron 触发）

输出格式（直接 print，cron --announce 会推送到 Jarvis）：
  📊 每日复盘 2026-08-03
  成交 N 笔 | 胜率 xx% | PnL +xx.xU
  止损 N 笔 / TP1 N 笔 / TP2 N 笔
  最佳: BTCUSDT +12.3U | 最差: ETHUSDT -8.1U
  信号分布: trend_long 3W1L / reversal 1W2L
"""

import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta

WORKSPACE = Path(__file__).parent.parent
sys.path.insert(0, str(WORKSPACE))

from modules.trade_logger import daily_summary, _load_all   # noqa: F401


def _today_utc(offset_days: int = 0) -> str:
    dt = datetime.now(timezone.utc) + timedelta(days=offset_days)
    return dt.strftime("%Y-%m-%d")


def _make_report(date_str: str | None = None) -> str:
    date_str = date_str or _today_utc(-1)   # 默认昨天（01:00 跑时报昨天数据）
    summary  = daily_summary(date_str)

    total   = summary.get("total_trades", 0)
    wins    = summary.get("wins", 0)
    losses  = summary.get("losses", 0)
    pnl     = summary.get("total_pnl_usdt", 0.0)
    wr      = summary.get("win_rate", 0.0)
    by_r    = summary.get("by_reason", {})
    by_sig  = summary.get("by_signal_type", {})
    consec  = summary.get("consecutive_losses", 0)

    if total == 0:
        return f"📊 每日复盘 {date_str}\n无成交记录"

    pnl_str  = f"+{pnl:.1f}U" if pnl >= 0 else f"{pnl:.1f}U"
    wr_str   = f"{wr*100:.0f}%"

    lines = [
        f"📊 每日复盘 {date_str}",
        f"成交 {total} 笔 | 胜率 {wr_str} ({wins}W{losses}L) | PnL {pnl_str}",
    ]

    # 平仓原因分布
    reason_parts = []
    for r, n in by_r.items():
        label = {"sl": "止损", "tp1": "TP1", "tp2": "TP2",
                 "manual": "手动", "circuit": "熔断"}.get(r, r)
        reason_parts.append(f"{label} {n}")
    if reason_parts:
        lines.append("  " + " / ".join(reason_parts))

    # 信号类型分布
    if by_sig:
        sig_parts = []
        for sig, d in by_sig.items():
            sig_parts.append(
                f"{sig} {d['wins']}W{d['trades']-d['wins']}L "
                f"({'+' if d['pnl'] >= 0 else ''}{d['pnl']:.1f}U)"
            )
        lines.append("信号: " + " | ".join(sig_parts))

    # 最佳/最差单笔
    all_closes = [r for r in _load_all()
                  if r.get("event") == "close"
                  and r.get("close_time", "").startswith(date_str)]
    if all_closes:
        best  = max(all_closes, key=lambda r: r.get("pnl_usdt", 0))
        worst = min(all_closes, key=lambda r: r.get("pnl_usdt", 0))
        b_pnl = f"+{best['pnl_usdt']:.1f}U" if best['pnl_usdt'] >= 0 else f"{best['pnl_usdt']:.1f}U"
        w_pnl = f"+{worst['pnl_usdt']:.1f}U" if worst['pnl_usdt'] >= 0 else f"{worst['pnl_usdt']:.1f}U"
        lines.append(f"最佳 {best['symbol']} {b_pnl} | 最差 {worst['symbol']} {w_pnl}")

    # 连续亏损告警
    if consec >= 3:
        lines.append(f"⚠️ 当前连续亏损 {consec} 笔，请留意风控")

    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=None, help="指定日期 YYYY-MM-DD，默认昨天")
    args = parser.parse_args()

    report = _make_report(args.date)
    print(report)
