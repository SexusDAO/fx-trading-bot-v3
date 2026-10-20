#!/usr/bin/env python3
"""
daily_analysis.py — 每日模拟盘报告 + 自动分析 + 优化建议
由 cron 每天触发，输出结果由 AI 直接转发
"""
import json, sys, pathlib, time
from datetime import datetime, timezone
from collections import defaultdict

sys.path.insert(0, "/root/.openclaw/workspace")

WORKSPACE = pathlib.Path("/root/.openclaw/workspace")

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

def analyze_version(state, label, initial=1000):
    if "_error" in state:
        return {"label": label, "error": state["_error"]}

    pos     = state.get("positions", {})
    hist    = state.get("history", [])
    pending = state.get("pending_orders", {})

    valid   = [h for h in hist if "8h" not in h.get("reason","") and "未成交" not in h.get("reason","")]
    wins    = sum(1 for h in valid if h.get("pnl_u", 0) > 0)
    losses  = sum(1 for h in valid if h.get("pnl_u", 0) <= 0)
    realized = sum(h.get("pnl_u", 0) for h in valid)

    sl_hits = [h for h in valid if "SL" in h.get("reason","") or "止损" in h.get("reason","")]
    tp_hits = [h for h in valid if "TP" in h.get("reason","") or "底安" in h.get("reason","") or "移动" in h.get("reason","")]

    sl_avg = sum(h.get("pnl_u",0) for h in sl_hits) / len(sl_hits) if sl_hits else 0
    tp_avg = sum(h.get("pnl_u",0) for h in tp_hits) / len(tp_hits) if tp_hits else 0
    rr     = abs(tp_avg / sl_avg) if sl_avg else 0
    wr     = 100 * wins // len(valid) if valid else 0

    float_pnl = 0.0
    for sym, p in pos.items():
        mk = get_mark(sym)
        ep = p.get("entry", p.get("entry_price", 0))
        sz = p.get("pos_usdt", p.get("position_size_usdt", p.get("notional", 100)))
        dr = p.get("direction", "LONG")
        if mk and ep > 0 and sz > 0:
            float_pnl += sz * ((mk - ep) / ep if dr == "LONG" else (ep - mk) / ep)

    equity = initial + realized + float_pnl
    recent = valid[-10:]
    recent_pnl = sum(h.get("pnl_u",0) for h in recent)
    recent_wr  = sum(1 for h in recent if h.get("pnl_u",0) > 0)

    # 自动诊断
    issues = []
    if len(valid) >= 10:
        if wr < 35:
            issues.append(f"胜率偏低({wr}%<35%)，信号方向准确率不足")
        if rr < 1.0 and len(tp_hits) > 0:
            issues.append(f"盈亏比偏低({rr:.2f}:1<1.0)，TP1可能设太近")
        if recent_pnl < -15 and len(recent) >= 8:
            issues.append(f"近{len(recent)}笔合计{recent_pnl:+.1f}U，近期连续亏损")
        # 相关性风险
        longs = [s for s,p in pos.items() if p.get("direction")=="LONG"]
        major = {"BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT","AVAXUSDT","LINKUSDT","DOGEUSDT"}
        major_longs = [s for s in longs if s in major]
        if len(major_longs) >= 3:
            issues.append(f"主流多单{len(major_longs)}个({','.join(major_longs[:3])})相关性过高")
        # SL方向错误
        for sym, p in pos.items():
            ep = p.get("entry",0); sl = p.get("sl_price",0); dr = p.get("direction","LONG")
            has_trail = p.get("trailing_sl") is not None
            if not has_trail and ep>0 and sl>0:
                if (dr=="LONG" and sl>=ep) or (dr=="SHORT" and sl<=ep):
                    issues.append(f"{sym} SL方向错误(sl={sl:.4g} entry={ep:.4g})")

    return {
        "label":      label,
        "equity":     equity,
        "realized":   realized,
        "float_pnl":  float_pnl,
        "n":          len(valid),
        "wins":       wins,
        "losses":     losses,
        "wr":         wr,
        "rr":         round(rr, 2),
        "sl_avg":     round(sl_avg, 2),
        "tp_avg":     round(tp_avg, 2),
        "recent_wr":  recent_wr,
        "recent_n":   len(recent),
        "recent_pnl": round(recent_pnl, 2),
        "positions":  len(pos),
        "pending":    len(pending),
        "issues":     issues,
        "reset_at":   state.get("reset_at", "未重置"),
    }


def save_weekly_snapshot(results):
    """追加本次数据到周报历史文件"""
    snap_path = WORKSPACE / "ws_cache" / "weekly_perf.jsonl"
    snap_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts":  time.time(),
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "versions": {r["label"]: {
            "equity": r.get("equity"),
            "wr":     r.get("wr"),
            "rr":     r.get("rr"),
            "n":      r.get("n"),
        } for r in results if "error" not in r}
    }
    with open(snap_path, "a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_weekly_trend():
    """读取最近7条每日快照，计算趋势"""
    snap_path = WORKSPACE / "ws_cache" / "weekly_perf.jsonl"
    if not snap_path.exists():
        return None
    rows = []
    with open(snap_path) as f:
        for line in f:
            try:
                rows.append(json.loads(line.strip()))
            except:
                pass
    return rows[-7:] if len(rows) >= 2 else None


def format_report(results):
    now = datetime.now(timezone.utc).strftime("%m-%d %H:%M UTC")
    lines = [f"📋 模拟盘日报 | {now}", ""]

    best = max([r for r in results if "error" not in r], key=lambda x: x["equity"], default=None)

    for r in results:
        if "error" in r:
            lines.append(f"❌ {r['label']}: {r['error']}")
            continue

        eq_pct = (r["equity"] - 1000) / 1000 * 100
        eq_icon = "📈" if r["equity"] >= 1000 else "📉"
        lines.append(f"{eq_icon} **{r['label']}**")
        lines.append(f"   净值 {r['equity']:.0f}U ({eq_pct:+.1f}%) | 浮盈 {r['float_pnl']:+.1f}U | 已实现 {r['realized']:+.1f}U")
        lines.append(f"   {r['n']}笔 胜率{r['wr']}% 盈亏比{r['rr']:.1f}:1 | 近{r['recent_n']}笔:{r['recent_wr']}胜 {r['recent_pnl']:+.1f}U | 持仓{r['positions']}")

        if r["issues"]:
            for iss in r["issues"]:
                lines.append(f"   ⚠️ {iss}")
        lines.append("")

    if best:
        lines.append(f"🏆 领先: {best['label']} {best['equity']:.0f}U")

    # 周趋势
    trend = load_weekly_trend()
    if trend and len(trend) >= 2:
        lines.append("")
        lines.append("📊 周趋势(最近7天 equity变化):")
        for label in ["动态仓位版", "固定100U版", "主流前100版"]:
            equities = [r["versions"].get(label, {}).get("equity") for r in trend]
            equities = [e for e in equities if e is not None]
            if len(equities) >= 2:
                delta = equities[-1] - equities[0]
                trend_icon = "↑" if delta > 0 else "↓"
                lines.append(f"   {label}: {equities[0]:.0f}→{equities[-1]:.0f}U ({delta:+.0f}U) {trend_icon}")

    return "\n".join(lines)


def main():
    sim_state   = load_state(str(WORKSPACE / "paper_sim_state.json"))
    test_state  = load_state(str(WORKSPACE / "paper_test_state.json"))
    major_state = load_state(str(WORKSPACE / "fx_major/logs/major_paper_state.json"))

    results = [
        analyze_version(sim_state,   "动态仓位版"),
        analyze_version(test_state,  "固定100U版"),
        analyze_version(major_state, "主流前100版"),
    ]

    # 保存快照供周趋势使用
    save_weekly_snapshot(results)

    # 输出报告
    print(format_report(results))


if __name__ == "__main__":
    main()
