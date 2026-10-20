#!/usr/bin/env python3
"""
paper-compare-4h 精简版 — 只输出净值/浮盈/胜率对比
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
        return f"{label}: 读取失败 {state['_error']}"

    positions = state.get("positions", {})
    hist      = state.get("history", [])
    pending   = state.get("pending_orders", {})

    wins     = sum(1 for h in hist if h.get("pnl_u", h.get("pnl", 0)) > 0)
    losses   = sum(1 for h in hist if h.get("pnl_u", h.get("pnl", 0)) < 0)
    realized = sum(h.get("pnl_u", h.get("pnl", 0)) for h in hist)
    wr       = f"{wins}胜{losses}负" if hist else "暂无"

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
        f"**{label}**  净值 {equity:.2f}U | 浮盈 {float_pnl:+.2f}U | 已实现 {realized:+.2f}U | {wr}",
        equity
    )


sim_state  = load_state("/root/.openclaw/workspace/paper_sim_state.json")
test_state = load_state("/root/.openclaw/workspace/paper_test_state.json")

now = datetime.now(timezone.utc).strftime("%m-%d %H:%M UTC")

sim_result  = summarize(sim_state,  "动态仓位版")
test_result = summarize(test_state, "固定100U版")

if isinstance(sim_result, tuple):
    sim_line, sim_eq = sim_result
else:
    sim_line, sim_eq = sim_result, 1000.0

if isinstance(test_result, tuple):
    test_line, test_eq = test_result
else:
    test_line, test_eq = test_result, 1000.0

diff = test_eq - sim_eq
leader = "动态版领先" if diff < 0 else "固定版领先"

print(f"📊 模拟盘对比 | {now}")
print(sim_line)
print(test_line)
print(f"差距: {leader} {abs(diff):.2f}U")
