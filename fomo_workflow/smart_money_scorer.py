#!/usr/bin/env python3
"""
smart_money_scorer.py
给 watchlist 里的聪明钱打分，写入 evm_watchlist.json 的 score / grade 字段。

评分维度（满分 100）：
  A. 盈利能力  40分  — fomoapi pnlUsd (7d/30d)
  B. 胜率       25分  — RT win_rate
  C. 活跃度     20分  — fomoapi trades (7d) + RT active
  D. 影响力     10分  — followers
  E. 数据完整度  5分  — 两个数据源都有则满分

等级划分：
  S级 (80-100)：顶级聪明钱 → 单人买入即触发策略A
  A级 (60-79) ：优质聪明钱 → 单人买入即触发策略A
  B级 (40-59) ：普通聪明钱 → 需要 2 人同时买才触发
  C级 (<40)   ：待观察      → 需要 2 人同时买才触发
"""

import json, math, os, time, requests
try:
    from config import KOL_DEFAULT_WINRATE_SCORE
except ImportError:
    KOL_DEFAULT_WINRATE_SCORE = 12.5

WATCHLIST = os.path.join(os.path.dirname(__file__), "evm_watchlist.json")
FOMO_KEY  = "fapi_a67aa12f7457419db205c051964c142132f317ae9f724097ba0ad306f6b3e56f"
PROXY     = {"https": "socks5://REDACTED_PROXY_CREDENTIALS",
              "http":  "socks5://REDACTED_PROXY_CREDENTIALS"}


def _log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}")


def _fetch_fomoapi():
    """拉 fomoapi 7d + 30d 榜单，返回 {handle_lower: trader_dict}"""
    hdrs = {"Authorization": f"Bearer {FOMO_KEY}"}
    result = {}
    for period in ["7d", "30d"]:
        try:
            r = requests.get(f"https://api.fomoapi.io/v2/leaderboard/{period}",
                             headers=hdrs, proxies=PROXY, timeout=15)
            traders = r.json() if isinstance(r.json(), list) \
                      else r.json().get("traders", r.json().get("data", []))
            for t in traders:
                h = t.get("handle", "").lower()
                if h and h not in result:
                    result[h] = t
                elif h:
                    # 用更大的 pnl 数据覆盖
                    if float(t.get("pnlUsd", 0) or 0) > float(result[h].get("pnlUsd", 0) or 0):
                        result[h] = t
        except Exception as e:
            _log(f"fomoapi {period} 拉取失败: {e}")
    return result


def _fetch_rt():
    """拉 robinhoodtrenches /api/traders，返回两个 dict"""
    rt_hdrs = {"User-Agent": "Mozilla/5.0", "Referer": "https://robinhoodtrenches.com/"}
    try:
        r = requests.get("https://robinhoodtrenches.com/api/traders",
                         headers=rt_hdrs, proxies=PROXY, timeout=15)
        traders = r.json()
        by_addr   = {t["address"].lower(): t for t in traders}
        by_handle = {t["handle"].lower(): t for t in traders}
        return by_addr, by_handle
    except Exception as e:
        _log(f"RT 拉取失败: {e}")
        return {}, {}


def _score_trader(fomo: dict, rt: dict) -> tuple[int, str]:
    """
    计算综合评分 (0-100) 和等级。
    fomo: fomoapi 榜单数据（可能为空 {}）
    rt:   robinhoodtrenches 数据（可能为空 {}）
    """
    score = 0

    # ── A. 盈利能力 40分 ────────────────────────────────────
    pnl = float(fomo.get("pnlUsd", 0) or 0)
    rt_pnl = float(rt.get("realized_pnl", 0) or 0)
    best_pnl = max(pnl, rt_pnl)
    # 对数映射：$1k→8分, $10k→20分, $100k→32分, $1M→40分
    if best_pnl > 0:
        pnl_score = min(40, int(40 * math.log10(max(best_pnl, 1)) / math.log10(1_000_000)))
    else:
        pnl_score = 0
    score += pnl_score

    # ── B. 胜率 25分 ────────────────────────────────────────
    wr = float(rt.get("win_rate") or 0)
    closed = int(rt.get("closed_trades") or 0)
    if closed < 3:
        # 样本不足：给默认中间分，避免系统性低估新 KOL
        wr_score = KOL_DEFAULT_WINRATE_SCORE
    elif wr > 0:
        # 胜率 50%→12分, 60%→16分, 70%→20分, 80%→24分, 90%+→25分
        wr_score = min(25, int(wr * 25 + 0.5))
        # 样本较少打折
        if closed < 5:
            wr_score = int(wr_score * 0.7)
        elif closed < 10:
            wr_score = int(wr_score * 0.85)
    else:
        wr_score = 0
    score += wr_score

    # ── C. 活跃度 20分 ──────────────────────────────────────
    trades_7d   = int(fomo.get("trades", 0) or 0)
    active_bags = int(rt.get("open_bags", 0) or 0)
    rt_active   = bool(rt.get("active"))
    # trades_7d: 1→4, 10→8, 50→12, 200→16, 500+→18
    if trades_7d >= 500: t_score = 18
    elif trades_7d >= 200: t_score = 16
    elif trades_7d >= 50:  t_score = 12
    elif trades_7d >= 10:  t_score = 8
    elif trades_7d >= 1:   t_score = 4
    else: t_score = 0
    # active bags 补充: +2
    if rt_active and active_bags > 0:
        t_score = min(20, t_score + 2)
    score += t_score

    # ── D. 影响力 10分 ──────────────────────────────────────
    followers = int(fomo.get("followers", 0) or rt.get("followers", 0) or 0)
    if followers >= 100_000:  f_score = 10
    elif followers >= 50_000: f_score = 8
    elif followers >= 10_000: f_score = 6
    elif followers >= 1_000:  f_score = 4
    elif followers >= 100:    f_score = 2
    else:                     f_score = 0
    score += f_score

    # ── E. 数据完整度 5分 ────────────────────────────────────
    has_fomo = bool(fomo)
    has_rt   = bool(rt)
    if has_fomo and has_rt: score += 5
    elif has_fomo or has_rt: score += 2

    # ── 等级 ─────────────────────────────────────────────────
    if score >= 80:   grade = "S"
    elif score >= 60: grade = "A"
    elif score >= 40: grade = "B"
    else:             grade = "C"

    return score, grade


def run_scoring():
    _log("开始拉取评分数据...")
    fomo_map          = _fetch_fomoapi()
    rt_by_addr, rt_by_handle = _fetch_rt()
    _log(f"fomoapi: {len(fomo_map)} 个, RT: {len(rt_by_addr)} 个")

    with open(WATCHLIST) as f:
        wl = json.load(f)

    scored = 0
    grade_dist = {"S": 0, "A": 0, "B": 0, "C": 0, "—": 0}

    for item in wl:
        fh  = (item.get("fomo_handle") or "").lower()
        evm = item["evm"].lower()

        fomo_data = fomo_map.get(fh, {})
        rt_data   = rt_by_addr.get(evm) or rt_by_handle.get(fh) or {}

        if not fomo_data and not rt_data:
            item["score"] = None
            item["grade"] = "—"
            grade_dist["—"] += 1
            continue

        s, g = _score_trader(fomo_data, rt_data)
        item["score"] = s
        item["grade"] = g
        item["score_ts"] = int(time.time())
        scored += 1
        grade_dist[g] += 1

    with open(WATCHLIST, "w") as f:
        json.dump(wl, f, indent=2, ensure_ascii=False)

    _log(f"评分完成: {scored}/{len(wl)} 个有分")
    _log(f"等级分布: S={grade_dist['S']} A={grade_dist['A']} B={grade_dist['B']} C={grade_dist['C']} 无数据={grade_dist['—']}")

    # 打印 S/A 级地址
    sa = sorted([x for x in wl if x.get("grade") in ("S","A")],
                key=lambda x: x.get("score", 0), reverse=True)
    print(f"\n{'等级':4s} {'评分':5s} {'Handle':25s} {'EVM':20s}")
    print("-" * 65)
    for x in sa[:20]:
        print(f"{x['grade']:4s} {x['score']:5d} {x.get('fomo_handle') or x['handle']:25s} {x['evm'][:18]}...")
    return wl


if __name__ == "__main__":
    run_scoring()
