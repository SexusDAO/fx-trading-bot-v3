#!/usr/bin/env python3
"""
tools/reject_funnel.py — 信号拒绝漏斗统计（只读，不改任何状态）

用途：评估「放宽/修复某条过滤」前后，信号数量与拒绝原因分布的变化。
输入：fx/logs/signal_analytics.jsonl（含轮转文件 .1~.5）。
用法：python3 tools/reject_funnel.py [--days 7] [--top 15]
输出：每个策略路径的通过率，以及按「拒绝原因（归一化）」统计的 Top N。
"""
import argparse
import json
import re
import time
from collections import Counter, defaultdict
from pathlib import Path

LOG_DIR = Path(__file__).parent.parent / "logs"


def _norm(reason: str) -> str:
    """把数字/价格归一化，使同类拒绝原因能聚合到一起。"""
    r = re.sub(r"-?\d+(\.\d+)?(e-?\d+)?%?", "#", reason or "未知")
    return r[:60]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--top", type=int, default=15)
    a = ap.parse_args()
    cutoff = (time.time() - a.days * 86400) * 1000
    files = sorted(LOG_DIR.glob("signal_analytics*.jsonl"))
    total = defaultdict(lambda: [0, 0])          # mode -> [通过, 总数]
    rej = defaultdict(Counter)                   # mode -> Counter(归一化原因)
    for f in files:
        for line in f.read_text(encoding="utf-8", errors="ignore").splitlines():
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("ts", 0) < cutoff:
                continue
            m = r.get("mode", "?")
            total[m][1] += 1
            if r.get("judge_passed"):
                total[m][0] += 1
            else:
                rej[m][_norm(r.get("reject_reason"))] += 1
    if not total:
        print("没有找到 signal_analytics 记录（检查 fx/logs/ 或 --days）")
        return
    print(f"== 最近 {a.days:g} 天各路径通过率 ==")
    for m, (p, n) in sorted(total.items(), key=lambda x: -x[1][1]):
        print(f"{m:<14} 通过 {p:>5} / {n:<6} = {p / n * 100:5.1f}%")
    for m in total:
        print(f"\n== {m} 拒绝原因 Top {a.top} ==")
        for reason, c in rej[m].most_common(a.top):
            print(f"{c:>6}  {reason}")


if __name__ == "__main__":
    main()
