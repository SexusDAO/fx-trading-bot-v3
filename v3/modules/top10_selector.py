"""
modules/top10_selector.py — Top10 竞争池筛选 v1.0

流程：
  1. 收集所有初筛分（已并发计算完毕）
  2. 过滤：score < 40 或 direction == NONE → 丢弃
  3. 方向分组：LONG / SHORT
  4. 各取最高分 Top5，合计 ≤ 10 个标的
  5. 某方向不足5个时取实际数量（不强行凑数）

输出：[ {symbol, direction, score, score_result, ...}, ... ]
"""

from __future__ import annotations

FLOOR_SCORE = 40          # 绝对下限（方向不明、极低分直接丢弃）
MAX_LONG    = 5
MAX_SHORT   = 5


def select_top10(analyzed: list[dict]) -> list[dict]:
    """
    参数：
        analyzed — list of analyze_symbol() 返回值
                   每项至少含: symbol / direction / score

    返回：
        top10 list（按分数降序排列，多空各最多5个）
    """
    # 1. 过滤明显无效项
    qualified = [
        r for r in analyzed
        if r is not None
        and r.get("direction", "NONE") not in ("NONE", "")
        and r.get("score", 0) >= FLOOR_SCORE
    ]

    # 2. 分方向
    longs  = sorted([r for r in qualified if r["direction"] == "LONG"],
                    key=lambda x: x["score"], reverse=True)
    shorts = sorted([r for r in qualified if r["direction"] == "SHORT"],
                    key=lambda x: x["score"], reverse=True)

    # 3. 各取 Top5
    top_long  = longs[:MAX_LONG]
    top_short = shorts[:MAX_SHORT]

    # 4. 合并，整体按分数降序
    combined = sorted(top_long + top_short,
                      key=lambda x: x["score"], reverse=True)
    return combined


def cutoff_info(analyzed: list[dict]) -> dict:
    """
    返回本次扫描的分布信息（调试 / 日志用）。
    """
    scores = [r["score"] for r in analyzed if r and r.get("direction") != "NONE"]
    if not scores:
        return {"total": 0, "floor": FLOOR_SCORE,
                "min": 0, "max": 0, "p30": 0, "p50": 0, "p70": 0}

    from modules.dynamic_baseline import _percentile
    s = sorted(scores)
    return {
        "total": len(scores),
        "floor": FLOOR_SCORE,
        "min":   s[0],
        "max":   s[-1],
        "p30":   round(_percentile(s, 30), 1),
        "p50":   round(_percentile(s, 50), 1),
        "p70":   round(_percentile(s, 70), 1),
    }
