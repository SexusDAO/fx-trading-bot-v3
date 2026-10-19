"""
macro_calendar.py — 宏观经济事件窗口检查
数据源: ForexFactory JSON calendar (本周 + 下周)
用法:
    from macro_calendar import check_macro_window
    blocked, reason, upcoming = check_macro_window()
    if blocked:
        print(f"宏观窗口拦截: {reason}")

返回:
    blocked  : bool   — 当前是否在高影响事件窗口内（前2h / 后2h）
    reason   : str    — 拦截原因（事件名 + 时间）
    upcoming : list   — 未来24h内的高影响事件列表 [{title, country, time_utc, minutes_away}]
"""

import requests
import datetime
import json
import os

# ── 配置 ──
WINDOW_BEFORE_MIN = 120   # 事件前2小时拦截
WINDOW_AFTER_MIN  = 120   # 事件后2小时拦截
UPCOMING_HOURS    = 24    # 提前预告窗口（小时）

# 只关注对加密市场有实质影响的事件关键词（USD为主，含美联储/就业/通胀）
HIGH_IMPACT_KEYWORDS = [
    # 美联储
    "FOMC", "Federal Reserve", "Fed Rate", "Interest Rate Decision",
    "Powell", "Fed Chair",
    # 就业
    "Non-Farm", "Nonfarm", "NFP", "Unemployment", "Initial Jobless",
    "ADP Non-Farm",
    # 通胀
    "CPI", "Core CPI", "PCE", "Core PCE", "PPI",
    # 增长
    "GDP", "Retail Sales",
    # 美联储讲话
    "FOMC Minutes", "Monetary Policy",
]

# 只看这些货币（USD 为核心，EUR/GBP/JPY 次要）
HIGH_IMPACT_COUNTRIES = {"USD", "EUR", "GBP", "JPY"}

CACHE_FILE = "/root/.openclaw/workspace/ws_cache/macro_calendar_cache.json"
CACHE_TTL_SECONDS = 3600  # 1小时缓存

PROXY = None
try:
    import binance_api as _api
    PROXY = {"http": _api.PROXY_URL, "https": _api.PROXY_URL}
except Exception:
    pass


def _fetch_calendar() -> list:
    """拉取本周+下周的 ForexFactory 日历，带本地缓存"""
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)

    # 读缓存
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE) as f:
                cached = json.load(f)
            if datetime.datetime.utcnow().timestamp() - cached.get("fetched_at", 0) < CACHE_TTL_SECONDS:
                return cached.get("events", [])
        except Exception:
            pass

    events = []
    urls = [
        "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
        "https://nfs.faireconomy.media/ff_calendar_nextweek.json",
    ]
    for url in urls:
        try:
            r = requests.get(url, proxies=PROXY, timeout=10)
            r.raise_for_status()
            events.extend(r.json())
        except Exception:
            pass

    # 写缓存
    try:
        with open(CACHE_FILE, "w") as f:
            json.dump({"fetched_at": datetime.datetime.utcnow().timestamp(), "events": events}, f)
    except Exception:
        pass

    return events


def _is_high_impact(event: dict) -> bool:
    """判断是否为需要关注的高影响事件"""
    # impact 必须是 High
    if event.get("impact", "") != "High":
        return False
    # 货币必须在关注列表
    if event.get("country", "") not in HIGH_IMPACT_COUNTRIES:
        return False
    # 标题必须包含关键词（USD 事件额外宽松，EUR/GBP/JPY 仅限关键词匹配）
    title = event.get("title", "")
    country = event.get("country", "")
    if country == "USD":
        return True  # USD High impact 全部关注
    return any(kw.lower() in title.lower() for kw in HIGH_IMPACT_KEYWORDS)


def _parse_event_time(event: dict) -> datetime.datetime:
    """解析事件时间为 UTC datetime"""
    date_str = event.get("date", "")
    if not date_str:
        return None
    try:
        # 格式: "2026-07-24T08:30:00-04:00"
        if date_str.endswith("Z"):
            dt = datetime.datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        else:
            dt = datetime.datetime.fromisoformat(date_str)
        # 转 UTC
        utc_offset = dt.utcoffset()
        if utc_offset is not None:
            dt = (dt - utc_offset).replace(tzinfo=None)
        return dt
    except Exception:
        return None


def check_macro_window() -> tuple:
    """
    检查当前是否在宏观事件窗口内。
    返回 (blocked: bool, reason: str, upcoming: list)
    """
    now_utc = datetime.datetime.utcnow()
    events = _fetch_calendar()

    blocked = False
    block_reason = ""
    upcoming = []

    for ev in events:
        if not _is_high_impact(ev):
            continue
        ev_time = _parse_event_time(ev)
        if ev_time is None:
            continue

        diff_min = (ev_time - now_utc).total_seconds() / 60

        # 窗口拦截：事件前后各2小时
        if -WINDOW_AFTER_MIN <= diff_min <= WINDOW_BEFORE_MIN:
            if not blocked:
                blocked = True
                if diff_min > 0:
                    block_reason = (f"{ev.get('title')} ({ev.get('country')}) "
                                    f"还有 {int(diff_min)} 分钟发布，暂停开仓")
                else:
                    block_reason = (f"{ev.get('title')} ({ev.get('country')}) "
                                    f"{int(-diff_min)} 分钟前刚发布，市场波动期")

        # 未来24h预告
        if 0 < diff_min <= UPCOMING_HOURS * 60:
            upcoming.append({
                "title":       ev.get("title"),
                "country":     ev.get("country"),
                "time_utc":    ev_time.strftime("%m-%d %H:%M UTC"),
                "minutes_away": int(diff_min),
                "impact":      ev.get("impact"),
            })

    # 按时间排序
    upcoming.sort(key=lambda x: x["minutes_away"])

    return blocked, block_reason, upcoming


def get_upcoming_summary() -> str:
    """返回未来24h高影响事件的简短摘要，用于日报"""
    _, _, upcoming = check_macro_window()
    if not upcoming:
        return "未来24h无高影响宏观事件"
    lines = ["📅 未来24h宏观事件:"]
    for ev in upcoming[:5]:
        h = ev["minutes_away"] // 60
        m = ev["minutes_away"] % 60
        lines.append(f"  • {ev['time_utc']} {ev['title']} ({ev['country']}) — 还有{h}h{m}m")
    return "\n".join(lines)


if __name__ == "__main__":
    blocked, reason, upcoming = check_macro_window()
    print(f"当前拦截: {blocked}")
    if blocked:
        print(f"原因: {reason}")
    print()
    print(get_upcoming_summary())
