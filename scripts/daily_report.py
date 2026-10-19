#!/usr/bin/env python3
"""
daily_report.py — 模拟盘每日精简报告
只输出：净值、浮盈、已实现、胜率对比
"""
import json, sys
from datetime import datetime, timezone

sys.path.insert(0, "/root/.openclaw/workspace")

def load_state(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        return {"_error": str(e)}

def get_mark(sym):
    try:
        import binance_api as api
        return float(api.get_mark_price(sym)["markPrice"])
    except:
        return None

def summarize(state, label):
    if "_error" in state:
        return f"{label}: 读取失败", 1000.0

    positions = state.get("positions", {})
    hist      = state.get("history", [])

    wins     = sum(1 for h in hist if h.get("pnl_u", h.get("pnl", 0)) > 0)
    losses   = sum(1 for h in hist if h.get("pnl_u", h.get("pnl", 0)) < 0)
    realized = sum(h.get("pnl_u", h.get("pnl", 0)) for h in hist)
    total    = wins + losses
    wr_pct   = f"{100*wins//total}%" if total else "—"

    float_pnl = 0.0
    for sym, p in positions.items():
        mk = get_mark(sym)
        ep = p.get("entry", p.get("entry_price", 0))
        sz = p.get("pos_usdt", p.get("size_usdt", p.get("notional", 0)))
        dr = p.get("direction", "LONG")
        if mk and ep > 0 and sz > 0:
            float_pnl += sz * (mk - ep) / ep if dr == "LONG" else sz * (ep - mk) / ep

    equity = 1000 + realized + float_pnl
    return (
        f"{label}  净值 {equity:.2f}U  浮盈 {float_pnl:+.2f}U  已实现 {realized:+.2f}U  胜率 {wins}/{total} ({wr_pct})",
        equity
    )


# 三个工作流
sim_state   = load_state("/root/.openclaw/workspace/paper_sim_state.json")
test_state  = load_state("/root/.openclaw/workspace/paper_test_state.json")
major_state = load_state("/root/.openclaw/workspace/fx_major/logs/major_paper_state.json")

now = datetime.now(timezone.utc).strftime("%m-%d %H:%M UTC")

sim_line,   sim_eq   = summarize(sim_state,   "📈 动态仓位版")
test_line,  test_eq  = summarize(test_state,  "📊 固定100U版")
major_line, major_eq = summarize(major_state, "🏦 主流前100版")

print(f"📋 模拟盘日报 | {now}")
print()
print(sim_line)
print(test_line)
print(major_line)
print()
best = max([("动态版", sim_eq), ("固定版", test_eq), ("主流版", major_eq)], key=lambda x: x[1])
print(f"领先: {best[0]} {best[1]:.2f}U")
