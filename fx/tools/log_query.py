#!/usr/bin/env python3
"""
fx/tools/log_query.py — 结构化日志查询工具

用法示例：
  python fx/tools/log_query.py --level error --keyword "task_scan" --start "2026-10-20 15:28" --end "2026-10-20 15:40" --context 5
  python fx/tools/log_query.py --level all --symbol PROMUSDT --start "2026-10-20 18:20" --end "2026-10-20 18:35"

参数说明：
  --level     日志级别过滤：all / debug / info / warning / error / critical（默认 all）
  --keyword   关键词（空格分隔表示 OR 匹配），如 "place_order pending 挂单"
  --symbol    标的过滤，如 PROMUSDT（大小写不敏感）
  --start     起始时间，格式 "YYYY-MM-DD HH:MM" 或 "YYYY-MM-DD HH:MM:SS"
  --end       结束时间，同上
  --tail      只看最后 N 条匹配（不指定则全部）
  --context   匹配行上下各打印 N 行（默认 0）
  --log-dir   日志目录（默认 fx/logs/）

输出格式：
  >>> 文件名|行号|原始日志行      ← 匹配行
      文件名|行号|原始日志行      ← 上下文行
"""

from __future__ import annotations
import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

MAX_OUTPUT_LINES = 200

# ── 日志级别权重 ──────────────────────────────────────────────────────────────
_LEVEL_WEIGHT = {
    "debug": 10, "info": 20, "warning": 30, "warn": 30,
    "error": 40, "critical": 50,
}
# 匹配日志行里的级别词
_LEVEL_RE = re.compile(
    r"\b(DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL)\b", re.IGNORECASE
)
# 匹配行首时间戳：2026-10-20 15:30:39,266  或  [2026-10-20 15:30:39 UTC]
_TS_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})"
)


def _parse_ts(s: str) -> datetime | None:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    return None


def _line_ts(line: str) -> datetime | None:
    m = _TS_RE.search(line)
    if not m:
        return None
    return _parse_ts(m.group(1))


def _line_level(line: str) -> str:
    m = _LEVEL_RE.search(line)
    return m.group(1).lower() if m else "info"


def _matches_level(line: str, min_level: str) -> bool:
    if min_level == "all":
        return True
    lw = _LEVEL_WEIGHT.get(min_level, 0)
    ll = _LEVEL_WEIGHT.get(_line_level(line), 20)
    return ll >= lw


def _matches_keyword(line: str, keywords: list[str]) -> bool:
    if not keywords:
        return True
    low = line.lower()
    return any(k.lower() in low for k in keywords)


def _matches_symbol(line: str, symbol: str) -> bool:
    if not symbol:
        return True
    return symbol.upper() in line.upper()


def _matches_time(line: str, start: datetime | None, end: datetime | None) -> bool | None:
    """返回 True=在范围内, False=超出范围, None=无法判断（无时间戳，不过滤）"""
    ts = _line_ts(line)
    if ts is None:
        return None
    if start and ts < start:
        return False
    if end and ts > end:
        return False
    return True


def query_file(
    path: Path,
    keywords: list[str],
    symbol: str,
    min_level: str,
    start: datetime | None,
    end: datetime | None,
    context: int,
) -> list[tuple[int, str, bool]]:
    """
    返回 [(lineno, line, is_match), ...]，仅包含命中行及其上下文范围内的行。
    """
    try:
        lines = path.read_text(errors="replace").splitlines()
    except Exception:
        return []

    hit_indices: list[int] = []
    for i, line in enumerate(lines):
        tm = _matches_time(line, start, end)
        if tm is False:
            continue
        # 有时间且在范围内，或者无时间戳（跟随上文，不独立过滤）
        if not _matches_level(line, min_level):
            continue
        if not _matches_keyword(line, keywords):
            continue
        if not _matches_symbol(line, symbol):
            continue
        hit_indices.append(i)

    if not hit_indices:
        return []

    # 展开上下文窗口
    include: set[int] = set()
    for idx in hit_indices:
        for j in range(max(0, idx - context), min(len(lines), idx + context + 1)):
            include.add(j)

    result: list[tuple[int, str, bool]] = []
    for i in sorted(include):
        result.append((i + 1, lines[i], i in set(hit_indices)))
    return result


def _parse_hours(s: str) -> float:
    s = (s or "").strip().lower()
    if s.endswith("h"):
        s = s[:-1]
    v = float(s)
    if v <= 0:
        raise ValueError("--last 必须 > 0")
    return v


def funnel_report(log_dir: Path, last_hours: float | None, now_ms: int | None = None) -> list[str]:
    """读 funnel_log.jsonl，按时间段过滤后按场景聚合，返回输出行。"""
    import json as _json
    import time as _time
    fp = Path(log_dir) / "funnel_log.jsonl"
    if not fp.exists():
        return [f"[log_query] 未找到 {fp}"]
    now_ms = int(_time.time() * 1000) if now_ms is None else now_ms
    since = now_ms - int(last_hours * 3600 * 1000) if last_hours else 0
    coarse, rej, pas, rounds = {}, {}, {}, 0
    with open(fp, encoding="utf-8") as f:
        for line in f:
            try:
                d = _json.loads(line)
            except Exception:
                continue
            if int(d.get("ts", 0) or 0) < since:
                continue
            rounds += 1
            for r, n in (d.get("coarse_by_route") or {}).items():
                coarse[r] = coarse.get(r, 0) + int(n or 0)
            for r, m in (d.get("reject_by_route") or {}).items():
                tgt = rej.setdefault(r, {})
                for c, n in (m or {}).items():
                    tgt[c] = tgt.get(c, 0) + int(n or 0)
            for r, n in (d.get("pass_by_route") or {}).items():
                pas[r] = pas.get(r, 0) + int(n or 0)
    span = f"最近 {last_hours:g}h" if last_hours else "全部"
    out = [f"[funnel] {span} 共 {rounds} 轮扫描"]
    order = ["pullback", "reversal", "breakout", "stop_hunt", "oi_divergence", "ema_trend", "range_bounce"]
    routes = [r for r in order if r in coarse or r in rej or r in pas]
    routes += sorted((set(coarse) | set(rej) | set(pas)) - set(order))
    w = max([len(r) for r in routes] + [8]) + 2
    for r in routes:
        rj = sorted(rej.get(r, {}).items(), key=lambda kv: -kv[1])
        rj_s = " ".join(f"{c}={n}" for c, n in rj if n) or "无"
        out.append(f"{('[' + r + ']').ljust(w)}初筛命中={coarse.get(r, 0)}次  精筛拒绝: {rj_s}  精筛通过={pas.get(r, 0)}")
    if not routes:
        out.append("[log_query] 时间段内没有漏斗记录")
    return out


def run(args: argparse.Namespace) -> None:
    if getattr(args, "funnel", False):
        try:
            hrs = _parse_hours(args.last) if args.last else None
        except ValueError as e:
            print(f"[log_query] --last 格式错误（例：--last 6h）: {e}", file=sys.stderr)
            sys.exit(2)
        print("\n".join(funnel_report(Path(args.log_dir), hrs)))
        return
    log_dir = Path(args.log_dir)
    if not log_dir.exists():
        print(f"[log_query] 日志目录不存在: {log_dir}", file=sys.stderr)
        sys.exit(1)

    # 收集所有 .log 文件（按修改时间升序）
    log_files = sorted(log_dir.glob("*.log"), key=lambda p: p.stat().st_mtime)
    if not log_files:
        print(f"[log_query] {log_dir} 下没有 .log 文件", file=sys.stderr)
        sys.exit(0)

    keywords = [k for k in (args.keyword or "").split() if k]
    symbol   = args.symbol or ""
    start_dt = _parse_ts(args.start) if args.start else None
    end_dt   = _parse_ts(args.end)   if args.end   else None
    min_level = (args.level or "all").lower()
    context  = max(0, args.context or 0)

    # 按文件收集所有结果行
    all_results: list[tuple[str, int, str, bool]] = []  # (filename, lineno, line, is_match)
    for f in log_files:
        rows = query_file(f, keywords, symbol, min_level, start_dt, end_dt, context)
        for lineno, line, is_match in rows:
            all_results.append((f.name, lineno, line, is_match))

    # --tail 截取最后 N 条匹配
    if args.tail:
        match_indices = [i for i, r in enumerate(all_results) if r[3]]
        if len(match_indices) > args.tail:
            keep_from = match_indices[-args.tail]
            all_results = all_results[keep_from:]

    if not all_results:
        print("[log_query] 无匹配结果")
        return

    # 截断保护
    truncated = False
    output_lines: list[str] = []
    for fname, lineno, line, is_match in all_results:
        prefix = ">>>" if is_match else "   "
        output_lines.append(f"{prefix} {fname}|{lineno}|{line}")

    total = len(output_lines)
    if total > MAX_OUTPUT_LINES:
        output_lines = output_lines[:MAX_OUTPUT_LINES]
        truncated = True

    print("\n".join(output_lines))
    if truncated:
        print(f"\n[log_query] 结果超过 {MAX_OUTPUT_LINES} 行，已截断（共 {total} 行）。"
              f"请用 --start/--end/--symbol/--keyword 缩小范围，或用 --tail N 只看最后 N 条。")
    else:
        match_count = sum(1 for r in all_results if r[3])
        print(f"\n[log_query] 共 {match_count} 条匹配，{total} 行输出（含上下文）")


def main() -> None:
    # 默认 log_dir：相对脚本位置推断 fx/logs/
    _default_log_dir = str(Path(__file__).parent.parent / "logs")

    parser = argparse.ArgumentParser(
        description="fx 日志查询工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--level",   default="all",
                        help="最低日志级别: all/debug/info/warning/error/critical（默认 all）")
    parser.add_argument("--keyword", default="",
                        help="关键词，空格分隔表示 OR 匹配")
    parser.add_argument("--symbol",  default="",
                        help="标的过滤，如 PROMUSDT")
    parser.add_argument("--start",   default="",
                        help="起始时间 'YYYY-MM-DD HH:MM' 或 'YYYY-MM-DD HH:MM:SS'")
    parser.add_argument("--end",     default="",
                        help="结束时间，同上")
    parser.add_argument("--tail",    type=int, default=0,
                        help="只看最后 N 条匹配")
    parser.add_argument("--context", type=int, default=0,
                        help="匹配行上下各打印 N 行（默认 0）")
    parser.add_argument("--funnel",  action="store_true",
                        help="按场景聚合 logs/funnel_log.jsonl（配合 --last Nh）")
    parser.add_argument("--last",    default="",
                        help="与 --funnel 同用：最近 N 小时，如 6h")
    parser.add_argument("--log-dir", default=_default_log_dir,
                        help=f"日志目录（默认 {_default_log_dir}）")
    args = parser.parse_args()
    run(args)


if __name__ == "__main__":
    main()
