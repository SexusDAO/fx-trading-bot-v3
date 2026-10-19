#!/usr/bin/env python3
"""
ic_analysis.py — 信号质量 IC 分析
读取 ws_cache/signal_ic_log.jsonl，输出：
  - 总体相关性（score 与 pnl_pct 的 Pearson / Spearman IC）
  - 按 sig_type 分组的胜率 & 平均盈亏
  - 按 score 分段（<75 / 75-79 / 80-84 / 85+）的表现
  - 结论：高分信号是否真的更能赚钱
"""
import json, pathlib, sys
from collections import defaultdict

IC_LOG = pathlib.Path("/root/.openclaw/workspace/ws_cache/signal_ic_log.jsonl")

def load_records():
    if not IC_LOG.exists():
        return []
    records = []
    with open(IC_LOG) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except:
                    pass
    # 只看有效成交（非"8h内未成交"取消）
    return [r for r in records if r.get("reason") != "8h内未成交"]

def pearson(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    mx = sum(xs)/n; my = sum(ys)/n
    num = sum((x-mx)*(y-my) for x,y in zip(xs,ys))
    dx  = sum((x-mx)**2 for x in xs)**0.5
    dy  = sum((y-my)**2 for y in ys)**0.5
    if dx*dy == 0:
        return 0.0
    return round(num/(dx*dy), 4)

def spearman(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    rx = sorted(range(n), key=lambda i: xs[i])
    ry = sorted(range(n), key=lambda i: ys[i])
    rank_x = [0]*n; rank_y = [0]*n
    for rank, idx in enumerate(rx): rank_x[idx] = rank
    for rank, idx in enumerate(ry): rank_y[idx] = rank
    return pearson(rank_x, rank_y)

def main():
    records = load_records()
    n = len(records)

    print(f"📊 IC 信号质量分析 | 有效样本: {n} 笔")
    print()

    if n < 5:
        print(f"⚠️  样本不足（需≥5笔），当前 {n} 笔有效成交，继续积累中。")
        if n > 0:
            print("\n已有记录:")
            for r in records:
                print(f"  {r.get('sym','?')} {r.get('sig_type','?')} score={r.get('score',0)} pnl={r.get('pnl_pct',0):+.2f}%")
        return

    scores  = [r["score"]   for r in records]
    pnls    = [r["pnl_pct"] for r in records]

    ic_p = pearson(scores, pnls)
    ic_s = spearman(scores, pnls)

    print(f"── 总体 IC ──────────────────────────────")
    print(f"  Pearson  IC: {ic_p:>+.4f}  {'✅ 正相关' if ic_p and ic_p > 0.05 else '⚠️  弱/负相关'}")
    print(f"  Spearman IC: {ic_s:>+.4f}  {'✅ 正相关' if ic_s and ic_s > 0.05 else '⚠️  弱/负相关'}")
    print()

    # 按 sig_type 分组
    by_sig = defaultdict(list)
    for r in records:
        by_sig[r.get("sig_type","?")].append(r)

    print(f"── 按信号类型 ───────────────────────────")
    print(f"  {'sig_type':<20} {'n':>3} {'胜率':>6} {'avg_pnl%':>9} {'avg_score':>10}")
    for sig, recs in sorted(by_sig.items()):
        wins = sum(1 for r in recs if r["pnl_pct"] > 0)
        avg_pnl = sum(r["pnl_pct"] for r in recs) / len(recs)
        avg_sc  = sum(r["score"]   for r in recs) / len(recs)
        print(f"  {sig:<20} {len(recs):>3} {100*wins//len(recs):>5}% {avg_pnl:>+9.2f}% {avg_sc:>10.1f}")
    print()

    # 按评分分段
    buckets = [("<75", 0, 75), ("75-79", 75, 80), ("80-84", 80, 85), ("85+", 85, 999)]
    print(f"── 按评分分段 ───────────────────────────")
    print(f"  {'bucket':<8} {'n':>3} {'胜率':>6} {'avg_pnl%':>9} {'avg_win%':>9} {'avg_loss%':>10}")
    for label, lo, hi in buckets:
        recs = [r for r in records if lo <= r["score"] < hi]
        if not recs:
            continue
        wins  = [r for r in recs if r["pnl_pct"] > 0]
        losses= [r for r in recs if r["pnl_pct"] <= 0]
        avg_pnl  = sum(r["pnl_pct"] for r in recs) / len(recs)
        avg_win  = sum(r["pnl_pct"] for r in wins)  / len(wins)  if wins   else 0
        avg_loss = sum(r["pnl_pct"] for r in losses)/ len(losses) if losses else 0
        print(f"  {label:<8} {len(recs):>3} {100*len(wins)//len(recs):>5}% {avg_pnl:>+9.2f}% {avg_win:>+9.2f}% {avg_loss:>+10.2f}%")
    print()

    # 结论
    print(f"── 结论 ─────────────────────────────────")
    if ic_s and abs(ic_s) < 0.05:
        print("  ⚠️  Spearman IC < 0.05：评分与实际盈亏几乎无相关，当前评分体系预测力弱。")
    elif ic_s and ic_s > 0.1:
        print("  ✅ Spearman IC > 0.1：评分有一定预测力，高分信号更可靠。")
    else:
        print(f"  📈 Spearman IC = {ic_s}，预测力一般，样本继续积累中。")

    total_pnl = sum(pnls)
    avg_pnl_overall = total_pnl / n
    wins_overall = sum(1 for p in pnls if p > 0)
    print(f"  总盈亏: {total_pnl:+.2f}%  胜率: {100*wins_overall//n}%  平均每笔: {avg_pnl_overall:+.2f}%")
    if n < 20:
        print(f"  ℹ️  当前样本 {n} 笔，建议积累到 20+ 笔后再做定论。")

if __name__ == "__main__":
    main()
