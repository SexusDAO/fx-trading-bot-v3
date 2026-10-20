# ============================================================
# card.py — 钉钉 markdown 信号卡片 v2
# 优化：更紧凑、更清晰、突出关键信息
# ============================================================
import time


def _usd(v: float) -> str:
    if v is None or v <= 0:
        return "-"
    if v >= 1_000_000:
        return f"${v/1e6:.2f}M"
    if v >= 1_000:
        return f"${v/1e3:.1f}K"
    return f"${v:.0f}"


def _cst(ts: float = None) -> str:
    if ts is None:
        ts = time.time()
    return time.strftime("%H:%M:%S", time.gmtime(ts + 8 * 3600)) + " CST"


def _ago_s(ts: float) -> str:
    """距离现在的中文时长"""
    d = max(0, time.time() - ts)
    if d < 60:
        return f"{d:.0f}s 前"
    if d < 3600:
        return f"{d/60:.0f}m 前"
    return f"{d/3600:.1f}h 前"


def _gmgn_link(addr: str) -> str:
    return f"https://gmgn.ai/bsc/token/{addr}"


def _safety_lines(safe: dict) -> list:
    """安全区块：简洁版"""
    if not safe:
        return []
    lines = []
    s = safe.get("score")
    label = safe.get("label", "")
    issues = safe.get("issues") or []
    
    if s == 0:
        lines.append("⛔ 蜜罐")
    elif s is None:
        lines.append("⚠️ 数据未索引")
    elif s >= 80:
        good = []
        lp_locked = safe.get("lp_locked_pct") or 0
        if lp_locked >= 95:
            good.append("LP已锁")
        if safe.get("renounced"):
            good.append("owner放弃")
        lines.append(f"✅ 安全{s:.0f}分" + (f" ({' '.join(good)})" if good else ""))
    else:
        lines.append(f"🛡 {s:.0f}分 {label}" + (f" | {' '.join(issues[:3])}" if issues else ""))
    
    return lines


def _safety_title_suffix(safe: dict) -> str:
    """蜜罐/极高风险 → 标题直接打标"""
    if not safe:
        return ""
    s = safe.get("score")
    if s == 0:
        return " ⛔蜜罐"
    if s is not None and s < 40:
        return " ⚠高危"
    return ""


def signal_card(*, emoji: str, category: str, priority: str, symbol: str,
                body_lines: list, addr: str = "", url: str = "",
                launchpad: str = "", source_tag: str = "",
                safety: dict = None, trade_ts: float = None,
                ts_precise: bool = True,
                description: str = "", trigger_logic: str = "") -> str:
    """
    v2 样式：更紧凑，关键信息突出
    """
    category = (category or "") + _safety_title_suffix(safety)
    lines = []
    
    # 标题行
    head = f"{emoji} **{category} · ${symbol}**"
    if priority and "高危" not in category and "蜜罐" not in category:
        head += f" · {priority}"
    lines.append(head)

    # 来源
    if source_tag:
        lines.append(source_tag)

    # 项目介绍（如果有）
    if description:
        lines.append(f" {description}")

    # 触发逻辑（如果有）
    if trigger_logic:
        lines.append(f"📈 触发逻辑：{trigger_logic}")

    # 触发/市场数据块
    for ln in body_lines:
        if ln and ln.strip():
            lines.append(ln)

    # 安全块
    sl = _safety_lines(safety)
    lines.extend(sl)

    # GMGN 链接（突出显示）
    if addr:
        lines.append(f"🔗 {_gmgn_link(addr)}")

    # 时间
    if trade_ts:
        pfx = "" if ts_precise else "≈"
        lines.append(f"⏱ {pfx}{_cst(trade_ts)}（{_ago_s(trade_ts)}）")
    lines.append(f" {_cst()}")
    
    return "\n\n".join(lines)


def buy_card(*, emoji: str, category: str, priority: str, symbol: str,
             trader: str, usd: float, action: str = "买入",
             mc: float = 0, liq: float = 0, vol1h: float = 0, vol5m: float = 0,
             buys_m5: int = 0, sells_m5: int = 0, buys_h1: int = 0, sells_h1: int = 0,
             price_change_5m: float = 0, price_change_1h: float = 0, price_change_24h: float = 0,
             age_min: float = 0,
             multi_count: int = 0, multi_total: float = 0,
             avg_position: float = 0,
             extra_lines: list = None, addr: str = "", url: str = "",
             launchpad: str = "", source_tag: str = "", safety: dict = None,
             trade_ts: float = None, ts_precise: bool = True,
             description: str = "", trigger_logic: str = "",
             kol_holders: int = 0, smart_money_holders: int = 0,
             holders: int = 0) -> str:
    """买入信号卡片 v2：更紧凑"""
    body = []
    
    # 触发信息
    who = f"**{trader}**" if trader.startswith("@") else f"**{trader[:10]}…**" if trader else "未知"
    body.append(f" {who} {action} **{_usd(usd)}**")
    
    # 多人共买
    if multi_count >= 2:
        body.append(f"🔥 {multi_count}人共买 **{_usd(multi_total)}**")
    
    # 市场数据（合并成两行）
    stat1 = [f"MC {_usd(mc)}", f"流动性 {_usd(liq)}"]
    if age_min and age_min < 24 * 60:
        stat1.append(f"币龄 {age_min/60:.1f}h" if age_min >= 60 else f"币龄 {age_min:.0f}m")
    body.append("📊 " + " · ".join(stat1))
    
    # KOL/聪明钱持有者数据（来自 Binance Web3）
    if (kol_holders or 0) > 0 or (smart_money_holders or 0) > 0:
        holder_stats = []
        if (kol_holders or 0) > 0:
            holder_stats.append(f"KOL {kol_holders}人")
        if (smart_money_holders or 0) > 0:
            holder_stats.append(f"聪明钱 {smart_money_holders}人")
        if (holders or 0) > 0:
            holder_stats.append(f"持有人 {holders}")
        body.append("👥 " + " · ".join(holder_stats))
    
    # 涨跌幅 + 成交量
    stats2 = []
    if price_change_5m:
        stats2.append(f"{'🟢' if price_change_5m > 0 else '🔴'}5m {price_change_5m:+.0f}%")
    if price_change_1h:
        stats2.append(f"{'🟢' if price_change_1h > 0 else '🔴'}1h {price_change_1h:+.0f}%")
    if vol1h:
        stats2.append(f"1h量 {_usd(vol1h)}")
    if buys_m5 or sells_m5:
        ratio = sells_m5 / buys_m5 if buys_m5 > 0 else 0
        stats2.append(f"5m买{buys_m5}/卖{sells_m5}")
        if ratio > 0:
            stats2.append(f"比1:{ratio:.1f}")
    if stats2:
        body.append("📈 " + " · ".join(stats2))
    
    if extra_lines:
        body.extend(extra_lines)
    
    return signal_card(emoji=emoji, category=category, priority=priority,
                       symbol=symbol, body_lines=body, addr=addr, url=url,
                       launchpad=launchpad, source_tag=source_tag,
                       safety=safety, trade_ts=trade_ts, ts_precise=ts_precise,
                       description=description, trigger_logic=trigger_logic)


def sell_card(*, symbol: str, trader: str, usd: float,
              mc: float = 0, liq: float = 0, url: str = "",
              addr: str = "", extra_lines: list = None, safety: dict = None,
              trade_ts: float = None, ts_precise: bool = True) -> str:
    """大额卖出离场预警卡片"""
    body = []
    who = f"**{trader}**" if trader.startswith("@") else f"**{trader[:10]}…**"
    body.append(f"📣 {who} 卖出 **{_usd(usd)}**")
    body.append(f"📊 MC {_usd(mc)} · 流动性 {_usd(liq)}")
    if extra_lines:
        body.extend(extra_lines)
    return signal_card(emoji="🚨", category="大额卖出", priority="⚠️ 离场",
                       symbol=symbol, body_lines=body, addr=addr, url=url,
                       source_tag="fomoapi", safety=safety,
                       trade_ts=trade_ts, ts_precise=ts_precise)


def status_card(*, seen_pairs: int, new_pairs_h: int, gmgn_cache: int,
                fomo_cache: int, uptime_min: int, pushes_h: int) -> str:
    """心跳状态卡片"""
    body = [
        f"运行 {uptime_min//60}h{uptime_min%60:02d}m · 新对 {seen_pairs} (+{new_pairs_h}/h)",
        f"去重: gmgn {gmgn_cache} · fomo {fomo_cache} · 1h推送 {pushes_h}",
    ]
    return signal_card(emoji="", category="BSC 监控", priority="✅",
                       symbol="monitor", body_lines=body)
