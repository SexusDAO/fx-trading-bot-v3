#!/usr/bin/env python3
"""
weekly_report.py — 每周模拟盘趋势报告
对比7天内每日快照，判断策略是否在改善
"""
import json, pathlib, sys
from datetime import datetime, timezone

WORKSPACE = pathlib.Path("/root/.openclaw/workspace")

def main():
    snap_path = WORKSPACE / "ws_cache" / "weekly_perf.jsonl"
    if not snap_path.exists():
        print("📊 周报: 暂无足够数据（需至少2天日报快照）")
        return

    rows = []
    with open(snap_path) as f:
        for line in f:
            try:
                rows.append(json.loads(line.strip()))
            except:
                pass

    if len(rows) < 2:
        print(f"📊 周报: 数据不足（当前{len(rows)}天，需≥2天）")
        return

    # 取最近7条
    rows = rows[-7:]
    now  = datetime.now(timezone.utc).strftime("%m-%d %H:%M UTC")
    lines = [f"📊 模拟盘周报 | {now} | 近{len(rows)}天趋势", ""]

    labels = ["动态仓位版", "固定100U版", "主流前100版"]

    for label in labels:
        data = []
        for r in rows:
            v = r.get("versions", {}).get(label)
            if v:
                data.append((r["date"], v))

        if len(data) < 2:
            continue

        first_eq = data[0][1].get("equity", 1000)
        last_eq  = data[-1][1].get("equity", 1000)
        delta    = last_eq - first_eq

        # 胜率趋势
        wrs = [v.get("wr", 0) for _, v in data if v.get("wr") is not None]
        rrs = [v.get("rr", 0) for _, v in data if v.get("rr") is not None]
        avg_wr = sum(wrs) / len(wrs) if wrs else 0
        avg_rr = sum(rrs) / len(rrs) if rrs else 0

        # 判断趋势方向
        if delta > 10:
            trend = "📈 改善"
        elif delta < -10:
            trend = "📉 恶化"
        else:
            trend = "➡️  持平"

        # 达标判断
        wr_ok = "✅" if avg_wr >= 40 else "❌"
        rr_ok = "✅" if avg_rr >= 1.5 else "❌"

        lines.append(f"**{label}** {trend}")
        lines.append(f"   净值: {first_eq:.0f}→{last_eq:.0f}U ({delta:+.0f}U)")
        lines.append(f"   周均胜率: {avg_wr:.0f}% {wr_ok}  周均盈亏比: {avg_rr:.2f}:1 {rr_ok}")

        # 每日净值序列
        eq_seq = " → ".join(f"{v.get('equity',0):.0f}" for _, v in data)
        lines.append(f"   日净值: {eq_seq}")
        lines.append("")

    # 上实盘评估
    lines.append("─" * 40)
    lines.append("上实盘门槛（需同时满足）:")
    lines.append("  胜率≥40% ✦ 盈亏比≥1.5:1 ✦ 净值正收益 ✦ 近20笔无连续亏损")

    # 检查是否有版本达标
    ready = []
    for label in labels:
        data = [(r["date"], r["versions"].get(label, {})) for r in rows if label in r.get("versions",{})]
        if not data:
            continue
        last = data[-1][1]
        if (last.get("wr",0) >= 40 and last.get("rr",0) >= 1.5
                and last.get("equity",0) >= 1000):
            ready.append(label)

    if ready:
        lines.append(f"  🟢 {', '.join(ready)} 接近达标，可考虑小仓位实盘测试")
    else:
        lines.append("  🔴 三个版本均未达标，继续模拟")

    print("\n".join(lines))


if __name__ == "__main__":
    main()
