#!/usr/bin/env python3
"""
tools/track_rejected.py — 拒绝信号事后追踪（对照组）

对 judge_passed=false 的记录，追踪信号产生后 8H 内价格
是否往「正确方向」走，用于评估各拒绝原因的准确性。

「方向正确」定义：
  - reversal SHORT / pullback SHORT：信号后 8H 最大跌幅 > 2%  → 拒绝可能是误拒
  - reversal LONG  / pullback LONG ：信号后 8H 最大涨幅 > 2%  → 拒绝可能是误拒
  - 其余情况：拒绝是合理的
"""
import sys, json, time, logging
from pathlib import Path
from collections import defaultdict

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

logger = logging.getLogger("fx.track_rejected")

TRACK_WINDOW_H  = 8     # 追踪窗口
CORRECT_MOVE    = 2.0   # 正确方向幅度门槛（%）


def _get_klines(symbol, start_ms, limit=32):
    try:
        import requests, os
        proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        proxies = {"https": proxy, "http": proxy} if proxy else None
        r = requests.get(
            "https://fapi.binance.com/fapi/v1/klines",
            params={"symbol": symbol, "interval": "15m",
                    "startTime": start_ms, "limit": limit},
            timeout=8, proxies=proxies,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.debug(f"[track_rejected] {symbol} K线失败: {e}")
        return []


def _check_rejected_outcome(rec: dict) -> dict | None:
    """
    追踪一条拒绝记录，返回 {correct_direction: bool, max_move_pct: float}
    或 None（数据不足）。
    """
    direction = (rec.get("direction") or "").upper()
    snap = rec.get("snapshot") or {}
    ref_price = snap.get("price")
    if not ref_price or not direction:
        return None

    klines = _get_klines(rec["symbol"], rec["ts"], limit=32)
    if not klines:
        return None

    window_end = rec["ts"] + TRACK_WINDOW_H * 3600 * 1000
    max_up_pct   = 0.0
    max_down_pct = 0.0

    for k in klines:
        if int(k[0]) > window_end:
            break
        hi = float(k[2])
        lo = float(k[3])
        max_up_pct   = max(max_up_pct,   (hi - ref_price) / ref_price * 100)
        max_down_pct = max(max_down_pct, (ref_price - lo) / ref_price * 100)

    if direction == "SHORT":
        correct = max_down_pct >= CORRECT_MOVE
        max_move = max_down_pct
    else:
        correct = max_up_pct >= CORRECT_MOVE
        max_move = max_up_pct

    return {"correct_direction": correct, "max_move_pct": round(max_move, 2)}


def run(analytics_file: Path | None = None, days: int = 7) -> dict:
    """
    主入口：统计各拒绝原因的「方向正确率」，返回分析 dict。
    """
    from config import SIGNAL_ANALYTICS_FILE
    fp = analytics_file or (ROOT / SIGNAL_ANALYTICS_FILE)
    if not fp.exists():
        return {}

    now_ms     = int(time.time() * 1000)
    cutoff_ms  = now_ms - days * 86400 * 1000
    min_age_ms = TRACK_WINDOW_H * 3600 * 1000  # 至少等 8H 才追踪

    rejected_recs = []
    with open(fp, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("judge_passed"):
                continue
            if rec.get("ts", 0) < cutoff_ms:
                continue
            if now_ms - rec.get("ts", 0) < min_age_ms:
                continue
            rejected_recs.append(rec)

    # 按拒绝原因分组统计
    groups: dict[str, list] = defaultdict(list)
    for rec in rejected_recs:
        reason = rec.get("reject_reason") or "unknown"
        # 归类：取前半段（去掉动态数值部分）
        import re
        key = re.sub(r'[=:：].*', '', reason).strip()[:60]
        result = _check_rejected_outcome(rec)
        if result:
            groups[key].append(result)
        time.sleep(0.03)

    report = {}
    for reason, results in sorted(groups.items(), key=lambda x: -len(x[1])):
        n      = len(results)
        n_corr = sum(1 for r in results if r["correct_direction"])
        avg_mv = sum(r["max_move_pct"] for r in results) / n
        report[reason] = {
            "samples":       n,
            "correct_pct":   round(n_corr / n * 100, 1),
            "avg_move_pct":  round(avg_mv, 2),
            "assessment":    (
                "⚠️ 可能误拒（正确率>55%）" if n_corr / n > 0.55
                else "✅ 拒绝合理（正确率≤55%）"
            ),
        }

    logger.info(f"[track_rejected] 分析完成: {len(rejected_recs)} 条拒绝记录，{len(report)} 种原因")
    return report


def print_report(report: dict) -> None:
    print(f"\n{'═'*62}")
    print(f"  被拒绝信号的事后表现")
    print(f"{'═'*62}")
    print(f"  {'拒绝原因':<35} {'样本':>4} {'正确率':>7} {'平均幅度':>8}  评估")
    print(f"  {'-'*60}")
    for reason, d in report.items():
        print(f"  {reason:<35} {d['samples']:>4} {d['correct_pct']:>6.0f}% "
              f"{d['avg_move_pct']:>7.2f}%  {d['assessment']}")
    print(f"{'═'*62}\n")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    rpt = run(days=7)
    print_report(rpt)
