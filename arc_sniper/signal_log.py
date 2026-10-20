#!/usr/bin/env python3
"""
Signal Log — 模拟买入信号记录
================================
当余额为 0 无法真实买入时，记录所有通过评分的信号到日志文件，
供后续复盘评估信号质量。

日志文件: arc_sniper/signal_log.jsonl (JSON Lines 格式，每行一个信号)
汇总文件: arc_sniper/signal_summary.json (按天汇总)

用法:
    from signal_log import SignalLog
    slog = SignalLog()
    slog.log_signal(
        token_address="0x...",
        token_symbol="π",
        token_name="π",
        score=72,
        details={...},
        simulated_buy_amount=5.0,
        buy_price_usdc=0.0000123,
        mcap_usd=500000,
        liquidity_usd=8000,
        holder_count=42,
        top10_pct=0.35,
        block_number=21193528,
        reason="score_above_threshold",
    )
    slog.daily_summary()  # 打印当日汇总
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).parent
SIGNAL_LOG_FILE = BASE_DIR / "signal_log.jsonl"
SIGNAL_SUMMARY_FILE = BASE_DIR / "signal_summary.json"


@dataclass
class SignalEntry:
    """一个信号记录"""
    timestamp: float           # 记录时间 (unix)
    token_address: str
    token_symbol: str
    token_name: str
    score: int                 # 0-100
    details: dict              # 评分详情
    should_buy: bool           # 是否通过买入阈值
    simulated_buy_amount: float  # 如果真实买入会花多少 USDC
    simulated_buy_price: float   # 模拟买入价 (USDC per token)
    simulated_tokens: float      # 模拟买入数量
    mcap_usd: float
    liquidity_usd: float
    holder_count: int
    top10_pct: float
    block_number: int
    pair_address: str
    honeypot_safe: bool
    signal_source: str          # onchain / grok / smart_money
    reason: str                 # 记录原因
    # 后续追踪 (填入后用于复盘)
    status: str = "pending"     # pending / tracked / expired
    tracked_price_1h: float = 0.0
    tracked_price_4h: float = 0.0
    tracked_price_24h: float = 0.0
    tracked_mcap_24h: float = 0.0
    notes: str = ""


class SignalLog:
    """信号日志管理器"""

    def __init__(self):
        self.log_file = SIGNAL_LOG_FILE
        self.summary_file = SIGNAL_SUMMARY_FILE

    def log_signal(self, **kwargs) -> dict:
        """记录一个信号到 JSONL 文件"""
        if 'timestamp' not in kwargs:
            kwargs['timestamp'] = time.time()
        entry = SignalEntry(**kwargs)
        line = json.dumps(asdict(entry), ensure_ascii=False)
        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        return asdict(entry)

    def load_signals(self, date_str: str = None) -> list[dict]:
        """加载信号，可按日期过滤 (格式: 2026-09-16)"""
        if not self.log_file.exists():
            return []
        signals = []
        for line in self.log_file.read_text(encoding="utf-8").strip().split("\n"):
            if not line:
                continue
            try:
                sig = json.loads(line)
                if date_str:
                    sig_date = time.strftime("%Y-%m-%d", time.gmtime(sig["timestamp"]))
                    if sig_date != date_str:
                        continue
                signals.append(sig)
            except json.JSONDecodeError:
                continue
        return signals

    def daily_summary(self, date_str: str = None) -> dict:
        """打印当日汇总"""
        if not date_str:
            date_str = time.strftime("%Y-%m-%d", time.gmtime())

        signals = self.load_signals(date_str)
        if not signals:
            print(f"No signals for {date_str}")
            return {"date": date_str, "count": 0}

        buy_signals = [s for s in signals if s["should_buy"]]
        skip_signals = [s for s in signals if not s["should_buy"]]

        summary = {
            "date": date_str,
            "total_signals": len(signals),
            "buy_signals": len(buy_signals),
            "skip_signals": len(skip_signals),
            "buy_details": [],
        }

        print(f"\n{'='*60}")
        print(f"📊 信号汇总 — {date_str}")
        print(f"{'='*60}")
        print(f"  总信号数: {len(signals)}")
        print(f"  通过买入阈值: {len(buy_signals)}")
        print(f"  未通过: {len(skip_signals)}")

        if buy_signals:
            print(f"\n  🟢 通过买入阈值的信号:")
            for s in buy_signals:
                print(
                    f"    {s['token_symbol'][:10]:10s} "
                    f"score={s['score']:3d} "
                    f"mcap=${s['mcap_usd']:>10,.0f} "
                    f"liq=${s['liquidity_usd']:>8,.0f} "
                    f"holders={s['holder_count']:3d} "
                    f"sim_buy=${s['simulated_buy_amount']:.2f} "
                    f"@ ${s['simulated_buy_price']:.10f}"
                )
                summary["buy_details"].append({
                    "symbol": s["token_symbol"],
                    "score": s["score"],
                    "mcap": s["mcap_usd"],
                    "liquidity": s["liquidity_usd"],
                    "holders": s["holder_count"],
                    "sim_amount": s["simulated_buy_amount"],
                    "sim_price": s["simulated_buy_price"],
                })

        if skip_signals:
            print(f"\n  🔴 未通过的信号 (前10):")
            for s in skip_signals[:10]:
                print(
                    f"    {s['token_symbol'][:10]:10s} "
                    f"score={s['score']:3d} "
                    f"reason={s['reason'][:30]}"
                )

        # 保存汇总
        self.summary_file.write_text(json.dumps(summary, indent=2, ensure_ascii=False))
        print(f"\n  汇总已保存: {self.summary_file}")
        print(f"{'='*60}\n")

        return summary

    def clear(self):
        """清空信号日志"""
        self.log_file.write_text("")


def main():
    """命令行: python3 signal_log.py [summary|clear|show]"""
    import sys

    slog = SignalLog()

    if len(sys.argv) < 2 or sys.argv[1] == "summary":
        date = sys.argv[2] if len(sys.argv) >= 3 else None
        slog.daily_summary(date)

    elif sys.argv[1] == "show":
        date = sys.argv[2] if len(sys.argv) >= 3 else None
        signals = slog.load_signals(date)
        print(json.dumps(signals, indent=2, ensure_ascii=False))

    elif sys.argv[1] == "clear":
        slog.clear()
        print("信号日志已清空")

    elif sys.argv[1] == "count":
        signals = slog.load_signals()
        print(f"总信号数: {len(signals)}")
        dates = {}
        for s in signals:
            d = time.strftime("%Y-%m-%d", time.gmtime(s["timestamp"]))
            dates[d] = dates.get(d, 0) + 1
        for d, c in sorted(dates.items()):
            print(f"  {d}: {c} signals")

    else:
        print("用法: python3 signal_log.py [summary [date]|show [date]|clear|count]")


if __name__ == "__main__":
    main()
