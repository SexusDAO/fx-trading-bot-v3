#!/usr/bin/env python3
"""
analytics.py — 模拟工作流数据分析模块
用法：python3 analytics.py [--state paper_test_state.json] [--json]

输出：
  - 整体 / 多空独立胜率、盈亏比
  - trend_score / timing_score 与盈亏相关性
  - 矛盾信号（consistency_log.jsonl）实际胜率
  - 最近10笔明细
"""

import json, pathlib, sys, argparse, datetime
from collections import defaultdict

WORKSPACE = pathlib.Path(__file__).parent
DEFAULT_STATE = WORKSPACE / "paper_test_state.json"
CONSISTENCY_LOG = WORKSPACE / "ws_cache" / "consistency_log.jsonl"


# ─────────────────────────────────────────────────────────────────────────────
# 数据加载
# ─────────────────────────────────────────────────────────────────────────────

def load_trades(state_file: pathlib.Path) -> list[dict]:
    """读取所有已平仓记录，标准化字段"""
    if not state_file.exists():
        print(f"[analytics] ❌ 状态文件不存在: {state_file}")
        return []

    state = json.loads(state_file.read_text())

    # 兼容两种 key
    trades = state.get("closed_trades", state.get("history", []))

    normalized = []
    for t in trades:
        pnl = float(t.get("pnl_usdt", t.get("pnl_u", t.get("pnl", 0))))
        normalized.append({
            "symbol":       t.get("symbol", t.get("sym", "?")),
            "direction":    t.get("direction", "LONG"),
            "pnl_u":        pnl,
            "pnl_pct":      float(t.get("pnl_pct", 0)),
            "reason":       t.get("reason", "?"),
            "score":        float(t.get("score", 0)),
            "trend_score":  float(t.get("trend_score", 0)),
            "timing_score": float(t.get("timing_score", 0)),
            "opened_at":    t.get("opened_at", t.get("placed_at", "")),
            "closed_at":    t.get("closed_at", t.get("close_at", t.get("exit_at", ""))),
            "entry":        float(t.get("entry", t.get("entry_price", 0))),
            "close_price":  float(t.get("close_price", t.get("exit_price", 0))),
            "pos_usdt":     float(t.get("pos_usdt", 100)),
            "note":         t.get("reason", ""),
        })
    return normalized


def load_open_positions(state_file: pathlib.Path) -> list[dict]:
    """读取当前持仓"""
    if not state_file.exists():
        return []
    state = json.loads(state_file.read_text())
    result = []
    for sym, pos in state.get("positions", {}).items():
        result.append({**pos, "symbol": sym})
    return result


def load_consistency_log() -> list[dict]:
    """读取矛盾信号日志"""
    if not CONSISTENCY_LOG.exists():
        return []
    entries = []
    for line in CONSISTENCY_LOG.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                entries.append(json.loads(line))
            except Exception:
                pass
    return entries


# ─────────────────────────────────────────────────────────────────────────────
# 统计函数
# ─────────────────────────────────────────────────────────────────────────────

def winrate_stats(trades: list[dict], label="全部") -> dict:
    if not trades:
        return {"label": label, "n": 0}
    wins   = [t for t in trades if t["pnl_u"] > 0]
    losses = [t for t in trades if t["pnl_u"] <= 0]
    total_pnl = sum(t["pnl_u"] for t in trades)
    avg_win   = sum(t["pnl_u"] for t in wins)   / len(wins)   if wins   else 0
    avg_loss  = sum(t["pnl_u"] for t in losses) / len(losses) if losses else 0
    rr = abs(avg_win / avg_loss) if avg_loss != 0 else 0
    wr = len(wins) / len(trades) * 100
    return {
        "label":    label,
        "n":        len(trades),
        "wins":     len(wins),
        "losses":   len(losses),
        "winrate":  round(wr, 1),
        "avg_win":  round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "rr":       round(rr, 2),
        "total_pnl": round(total_pnl, 2),
        "expectancy": round(wr/100 * avg_win + (1 - wr/100) * avg_loss, 2),
    }


def score_correlation(trades: list[dict]) -> dict:
    """分析 trend_score / timing_score / total_score 与盈亏的相关性"""
    valid = [t for t in trades if t["trend_score"] > 0 or t["timing_score"] > 0 or t["score"] > 0]
    if len(valid) < 5:
        return {"note": f"样本不足（{len(valid)}笔，需≥5笔）"}

    def pearson(xs, ys):
        n = len(xs)
        if n < 3:
            return 0
        mx, my = sum(xs)/n, sum(ys)/n
        num = sum((x-mx)*(y-my) for x,y in zip(xs,ys))
        dx  = (sum((x-mx)**2 for x in xs) ** 0.5)
        dy  = (sum((y-my)**2 for y in ys) ** 0.5)
        return round(num / (dx * dy), 3) if dx * dy > 0 else 0

    pnls = [t["pnl_u"] for t in valid]
    result = {}
    for key in ("trend_score", "timing_score", "score"):
        vals = [t[key] for t in valid]
        r = pearson(vals, pnls)
        result[key] = r

    # 分段分析：高/中/低分胜率
    buckets = {"高分(≥85)": [], "中分(75-84)": [], "低分(<75)": []}
    for t in trades:
        s = t["score"]
        if s >= 85:   buckets["高分(≥85)"].append(t)
        elif s >= 75: buckets["中分(75-84)"].append(t)
        else:         buckets["低分(<75)"].append(t)

    result["buckets"] = {
        k: {
            "n":      len(v),
            "winrate": round(sum(1 for t in v if t["pnl_u"]>0) / len(v) * 100, 1) if v else 0,
            "avg_pnl": round(sum(t["pnl_u"] for t in v) / len(v), 2) if v else 0,
        }
        for k, v in buckets.items()
    }
    return result


def consistency_analysis(trades: list[dict], clog: list[dict]) -> dict:
    """分析矛盾信号（consistency_log）实际胜率 vs 非矛盾信号"""
    if not clog:
        return {"note": "暂无矛盾信号记录（consistency_log.jsonl 为空）"}

    conflict_syms = set()
    flag_counter  = defaultdict(int)
    for entry in clog:
        sym = entry.get("sym", "")
        if sym:
            conflict_syms.add(sym)
        for flag in entry.get("flags", []):
            # 取 "(" 前缀作为分类（如 "RSI超买(75)+大涨幅(6.2%)" → "RSI超买"）
            # 但同一条目里可能有多个 "+" 连接的信号，只取整体描述的前缀
            key = flag.split("(")[0].split("+")[0].strip()
            flag_counter[key] += 1

    conflict_trades    = [t for t in trades if t["symbol"] in conflict_syms]
    no_conflict_trades = [t for t in trades if t["symbol"] not in conflict_syms]

    return {
        "total_conflict_syms":    len(conflict_syms),
        "conflict_trades":        winrate_stats(conflict_trades, "矛盾信号标的"),
        "no_conflict_trades":     winrate_stats(no_conflict_trades, "无矛盾信号标的"),
        "flag_frequency":         dict(sorted(flag_counter.items(), key=lambda x: -x[1])),
    }


def reason_breakdown(trades: list[dict]) -> dict:
    """按平仓原因分类统计"""
    groups = defaultdict(list)
    for t in trades:
        r = t["reason"]
        if "SL" in r or "sl" in r or "止损" in r:
            groups["止损(SL)"].append(t)
        elif "TP1" in r or "tp1" in r or "止盈" in r:
            groups["止盈(TP1)"].append(t)
        elif "TP2" in r or "tp2" in r:
            groups["止盈(TP2)"].append(t)
        elif "health" in r or "健康" in r:
            groups["健康检查强制平仓"].append(t)
        elif "time" in r or "超时" in r:
            groups["时间止损"].append(t)
        else:
            groups[f"其他({r[:20]})"].append(t)

    return {
        k: {
            "n":       len(v),
            "avg_pnl": round(sum(t["pnl_u"] for t in v) / len(v), 2) if v else 0,
            "total":   round(sum(t["pnl_u"] for t in v), 2),
        }
        for k, v in groups.items()
    }


# ─────────────────────────────────────────────────────────────────────────────
# 格式化输出
# ─────────────────────────────────────────────────────────────────────────────

def fmt_stats(s: dict) -> str:
    if s.get("n", 0) == 0:
        return f"  {s.get('label','?')}: 暂无数据"
    icon = "✅" if s["winrate"] >= 60 else ("⚠️" if s["winrate"] >= 45 else "❌")
    return (
        f"  {s['label']}: {s['n']}笔  {icon} 胜率{s['winrate']}%  "
        f"盈亏比{s['rr']}  期望值{s['expectancy']:+.2f}U/笔  总PnL{s['total_pnl']:+.2f}U\n"
        f"    均盈:{s['avg_win']:+.2f}U  均亏:{s['avg_loss']:+.2f}U"
    )


def print_report(trades, positions, clog, state_file):
    now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n{'='*62}")
    print(f"  📊 analytics.py — 模拟工作流分析报告")
    print(f"  {now}  |  数据源: {state_file.name}")
    print(f"{'='*62}")

    # 1. 基础统计
    print(f"\n【一、整体胜率统计】  历史:{len(trades)}笔  当前持仓:{len(positions)}笔")
    if not trades:
        print("  暂无历史记录，继续积累样本中…")
    else:
        all_stats   = winrate_stats(trades, "整体")
        long_trades  = [t for t in trades if t["direction"] == "LONG"]
        short_trades = [t for t in trades if t["direction"] == "SHORT"]
        print(fmt_stats(all_stats))
        print(fmt_stats(winrate_stats(long_trades,  "多单独立")))
        print(fmt_stats(winrate_stats(short_trades, "空单独立")))

        # 上线标准检查
        print(f"\n  📋 上线标准检查:")
        n_ok    = "✅" if len(trades) >= 20 else f"❌ ({len(trades)}/20)"
        wr_ok   = "✅" if all_stats["winrate"] >= 60 else f"❌ ({all_stats['winrate']}%/60%)"
        long_wr = winrate_stats(long_trades, "")["winrate"] if len(long_trades) >= 5 else -1
        short_wr= winrate_stats(short_trades,"")["winrate"] if len(short_trades) >= 5 else -1
        long_ok  = "✅" if long_wr  >= 60 else (f"❌ ({long_wr}%)" if long_wr >= 0 else f"⏳ (样本不足{len(long_trades)}/5)")
        short_ok = "✅" if short_wr >= 60 else (f"❌ ({short_wr}%)" if short_wr >= 0 else f"⏳ (样本不足{len(short_trades)}/5)")
        print(f"    样本≥20笔: {n_ok}  整体胜率≥60%: {wr_ok}")
        print(f"    多单独立≥60%: {long_ok}  空单独立≥60%: {short_ok}")

    # 2. 评分相关性
    print(f"\n【二、评分与盈亏相关性】")
    corr = score_correlation(trades)
    if "note" in corr:
        print(f"  {corr['note']}")
    else:
        def corr_label(r):
            if r > 0.3:   return f"正相关 ✅ (r={r})"
            elif r < -0.3: return f"负相关 ❌ (r={r})"
            else:          return f"弱相关 ⚠️ (r={r})"
        print(f"  总分   vs PnL: {corr_label(corr.get('score',0))}")
        print(f"  trend  vs PnL: {corr_label(corr.get('trend_score',0))}")
        print(f"  timing vs PnL: {corr_label(corr.get('timing_score',0))}")

        if "buckets" in corr:
            print(f"\n  评分分段胜率:")
            for bk, bv in corr["buckets"].items():
                n = bv["n"]
                if n == 0: continue
                wr_b = bv["winrate"]
                icon = "✅" if wr_b >= 60 else "⚠️"
                print(f"    {bk}: {n}笔  {icon} {wr_b}%  均PnL{bv['avg_pnl']:+.2f}U")

    # 3. 平仓原因
    if trades:
        print(f"\n【三、平仓原因分布】")
        for reason, rv in reason_breakdown(trades).items():
            print(f"  {reason}: {rv['n']}笔  总PnL{rv['total']:+.2f}U  均PnL{rv['avg_pnl']:+.2f}U")

    # 4. 矛盾信号分析
    print(f"\n【四、矛盾信号分析】")
    ca = consistency_analysis(trades, clog)
    if "note" in ca:
        print(f"  {ca['note']}")
    else:
        print(f"  累计记录矛盾信号标的: {ca['total_conflict_syms']} 个")
        if ca["flag_frequency"]:
            print(f"  矛盾类型频次: {ca['flag_frequency']}")
        if trades:
            print(fmt_stats(ca["conflict_trades"]))
            print(fmt_stats(ca["no_conflict_trades"]))
            # 判断矛盾信号是否值得过滤
            ct = ca["conflict_trades"]
            nt = ca["no_conflict_trades"]
            if ct.get("n", 0) >= 3 and nt.get("n", 0) >= 3:
                diff = nt.get("winrate", 0) - ct.get("winrate", 0)
                if diff >= 10:
                    print(f"  💡 建议：矛盾信号标的胜率低 {diff:.0f}%，考虑启用拒绝门槛")
                else:
                    print(f"  ℹ️  矛盾信号与非矛盾信号胜率差异 {diff:.0f}%，暂不需要过滤")

    # 5. 最近10笔明细
    print(f"\n【五、最近10笔明细】")
    if not trades:
        print("  暂无历史记录")
    else:
        for t in trades[-10:]:
            icon = "🟢" if t["pnl_u"] > 0 else "🔴"
            sc   = f" 评分:{t['score']:.0f}" if t["score"] > 0 else ""
            ts_s = f" trend:{t['trend_score']:.0f}" if t["trend_score"] > 0 else ""
            ti_s = f" timing:{t['timing_score']:.0f}" if t["timing_score"] > 0 else ""
            print(f"  {icon} {t['symbol']:<16} {t['pnl_u']:>+7.2f}U  [{t['reason'][:20]}]{sc}{ts_s}{ti_s}")

    # 6. 当前持仓快照
    if positions:
        print(f"\n【六、当前持仓快照（{len(positions)}笔）】")
        for pos in sorted(positions, key=lambda x: -x.get("score", 0))[:5]:
            sym   = pos.get("symbol", "?")
            score = pos.get("score", 0)
            entry = pos.get("entry", 0)
            sl    = pos.get("sl_price", 0)
            sl_d  = abs(entry - sl) / entry * 100 if entry > 0 else 0
            print(f"  {sym:<16} 评分:{score}  SL距离:{sl_d:.1f}%  开仓:{pos.get('opened_at','')}")
        if len(positions) > 5:
            print(f"  … 另有 {len(positions)-5} 笔")

    print(f"\n{'='*62}\n")


# ─────────────────────────────────────────────────────────────────────────────
# 主入口
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="模拟工作流数据分析")
    parser.add_argument("--state",  default=str(DEFAULT_STATE), help="状态文件路径")
    parser.add_argument("--json",   action="store_true",        help="输出原始 JSON 结果")
    parser.add_argument("--all",    action="store_true",        help="同时分析两个状态文件")
    args = parser.parse_args()

    state_files = [pathlib.Path(args.state)]
    if args.all:
        for f in ["paper_test_state.json", "paper_sim_state.json"]:
            p = WORKSPACE / f
            if p not in state_files and p.exists():
                state_files.append(p)

    clog = load_consistency_log()

    for sf in state_files:
        trades    = load_trades(sf)
        positions = load_open_positions(sf)

        if args.json:
            result = {
                "state_file":    str(sf),
                "n_trades":      len(trades),
                "n_positions":   len(positions),
                "overall":       winrate_stats(trades),
                "long":          winrate_stats([t for t in trades if t["direction"]=="LONG"],  "多单"),
                "short":         winrate_stats([t for t in trades if t["direction"]=="SHORT"], "空单"),
                "score_corr":    score_correlation(trades),
                "reasons":       reason_breakdown(trades),
                "consistency":   consistency_analysis(trades, clog),
            }
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print_report(trades, positions, clog, sf)


if __name__ == "__main__":
    main()
