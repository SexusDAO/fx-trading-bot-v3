#!/usr/bin/env python3
"""
ru7_monitor.py — 监听 Ru7Bot 频道
解析 OI 增幅榜（>30% 且 OI>$20M）注入候选池
做市商预警（评分≥8）注入候选池

信号仅作参考，仍走完整工作流（评分≥75）才开单
通知策略：全部静默，开仓成功由 paper_scanner 主动通知
"""
import json, re, sys, pathlib, datetime, urllib.request

WORKSPACE    = pathlib.Path("/root/.openclaw/workspace")
INJECT_FILE  = WORKSPACE / "ws_cache" / "ru7_inject.json"

OI_MIN_CHG   = 30.0    # OI 增幅阈值 %
OI_MIN_ABS_M = 20.0    # OI 绝对值下限（百万U）
MM_MIN_SCORE = 8       # 做市商预警分数下限

# ── 拉取频道 HTML ──────────────────────────────────────────────────────────────
def fetch_channel() -> str:
    url = "https://t.me/s/ru7bot8"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.read().decode("utf-8", errors="ignore")
    except Exception as e:
        print(f"[ru7] fetch 失败: {e}")
        return ""

# ── 解析 OI 增幅榜 ─────────────────────────────────────────────────────────────
def parse_oi_gainers(html: str) -> list[dict]:
    """
    解析形如：
      TRUMP +161.5% OI $94.25M
    筛选 OI 增幅 >OI_MIN_CHG% 且绝对值 >OI_MIN_ABS_M M 的标的
    这类标的代表主力在快速建仓，是我们现有成交量/涨幅池容易遗漏的信号
    """
    results = []
    # 找 OI 增幅块
    blocks = re.findall(
        r'OI 增幅 Top.*?(?=OI 减少|交易额|━━)',
        html, re.DOTALL
    )
    for block in blocks:
        ts_m = re.search(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2})', block)
        ts   = ts_m.group(1) if ts_m else datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M")

        lines = re.findall(
            r'(\w+)\s+\+(\d+\.?\d*)%\s+OI\s+\$([\d.]+)([MBK]?)',
            block
        )
        for sym, chg_str, oi_str, unit in lines:
            chg  = float(chg_str)
            oi_m = float(oi_str)
            if unit == "B": oi_m *= 1000
            elif unit == "K": oi_m /= 1000
            if chg >= OI_MIN_CHG and oi_m >= OI_MIN_ABS_M:
                sym_full = sym.upper()
                if not sym_full.endswith("USDT"):
                    sym_full += "USDT"
                results.append({
                    "sym":    sym_full,
                    "oi_chg": chg,
                    "oi_m":   oi_m,
                    "source": "oi_gainer",
                    "ts":     ts,
                })
    return results

# ── 解析做市商预警 ─────────────────────────────────────────────────────────────
def parse_market_maker(html: str) -> list[dict]:
    """
    解析形如：
      ↑ CYS [多] $0.5783 | 🔥异常放量 📈阶梯拉盘 | 9分 | $17.9M
    筛选评分 ≥ MM_MIN_SCORE 的标的
    """
    results = []
    blocks = re.findall(
        r'做市商预警.*?(?=做市商预警|RU7 日报|合约市场动态|Alpha|$)',
        html, re.DOTALL
    )
    for block in blocks:
        ts_m = re.search(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2})', block)
        ts   = ts_m.group(1) if ts_m else "?"
        lines = re.findall(
            r'[↑↓]\s+(\w+)\s+\[(多|空)\]\s+\S+\s*\|\s*(.*?)\|\s*(\d+)分\s*\|\s*\$([\d.]+)([MK]?)',
            block
        )
        for sym, direction, tags_raw, score_str, vol_str, vol_unit in lines:
            score = int(score_str)
            vol_m = float(vol_str)
            if vol_unit == "K": vol_m /= 1000
            if score >= MM_MIN_SCORE:
                sym_full = sym.upper()
                if not sym_full.endswith("USDT"):
                    sym_full += "USDT"
                tags = [t.strip() for t in re.sub(r'[🔥📈📌⚠️]','',tags_raw).split() if t.strip()]
                results.append({
                    "sym":       sym_full,
                    "direction": "LONG" if direction == "多" else "SHORT",
                    "score":     score,
                    "vol_m":     vol_m,
                    "tags":      tags,
                    "source":    "market_maker",
                    "ts":        ts,
                })
    return results

# ── 写入注入文件 ───────────────────────────────────────────────────────────────
def inject_to_pool(oi_list: list, mm_list: list) -> int:
    """
    合并写入 ru7_inject.json
    返回新增标的数量
    """
    INJECT_FILE.parent.mkdir(parents=True, exist_ok=True)
    existing = {}
    if INJECT_FILE.exists():
        try:
            existing = json.loads(INJECT_FILE.read_text())
        except Exception:
            existing = {}

    now_str = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    cutoff  = datetime.datetime.utcnow() - datetime.timedelta(hours=4)

    def is_fresh(e):
        try:
            t = datetime.datetime.strptime(e.get("inject_time",""), "%Y-%m-%d %H:%M UTC")
            return t >= cutoff
        except Exception:
            return True

    # OI 增幅标的
    oi_pool     = [e for e in existing.get("oi_gainers",[]) if is_fresh(e)]
    oi_syms     = {e["sym"] for e in oi_pool}
    oi_added    = 0
    for item in oi_list:
        if item["sym"] not in oi_syms:
            item["inject_time"] = now_str
            item["consumed"]    = False
            oi_pool.append(item)
            oi_added += 1

    # 做市商预警
    mm_pool     = [e for e in existing.get("market_maker",[]) if is_fresh(e)]
    mm_syms     = {e["sym"] for e in mm_pool}
    mm_added    = 0
    for item in mm_list:
        if item["sym"] not in mm_syms:
            item["inject_time"] = now_str
            item["consumed"]    = False
            mm_pool.append(item)
            mm_added += 1

    result = {
        "updated":      now_str,
        "oi_gainers":   oi_pool,
        "market_maker": mm_pool,
        # 保持向后兼容（旧字段名）
        "hot_contracts": [],
    }
    INJECT_FILE.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    return oi_added + mm_added

# ── 主流程 ─────────────────────────────────────────────────────────────────────
def main():
    html = fetch_channel()
    if not html:
        print("HEARTBEAT_OK")
        return

    oi_list = parse_oi_gainers(html)
    mm_list = parse_market_maker(html)
    total   = inject_to_pool(oi_list, mm_list)

    if total > 0:
        oi_new = [s for s in oi_list]
        mm_new = [s for s in mm_list]
        print(f"[ru7] 新增候选: OI增幅榜 {len(oi_new)} 个 + 做市商预警 {len(mm_new)} 个，已注入候选池")
        for s in oi_new:
            print(f"  OI  {s['sym']:<18} OI+{s['oi_chg']:.0f}% / ${s['oi_m']:.0f}M @ {s['ts']}")
        for s in mm_new:
            dir_zh = "多" if s["direction"] == "LONG" else "空"
            print(f"  MM  {s['sym']:<18} [{dir_zh}] {s['score']}分 @ {s['ts']}")
    else:
        print("HEARTBEAT_OK")

if __name__ == "__main__":
    main()
