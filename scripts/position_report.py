#!/usr/bin/env python3
"""
position_report.py — 持仓分析推送脚本
只报告已开仓信息 + 持续持有建议，不推送观察区/挂单/低分信号
"""
import sys, json, time
sys.path.insert(0, '/root/.openclaw/workspace')

import binance_api as api
import coinglass_api as cg

# ── 阈值 ────────────────────────────────────────────────────────────────────
WARN_LOSS_PCT    = -2.0   # 浮亏超过此值 → ⚠️ 提示关注
WARN_LOSS_U      = -30.0  # 浮亏超过此U值 → ⚠️
SCORE_HOLD_MIN   = -10    # CG综合评分低于此值 → 建议离场/收紧止损
SCORE_GOOD       = 5      # CG综合评分高于此值 → 建议持有

def emoji_pnl(v):
    if v > 0: return "🟢"
    if v < -20: return "🔴"
    if v < 0: return "🟡"
    return "⚪"

def score_to_100(s: int) -> int:
    """将 CG 综合评分（约 -40~+31）映射到 0~100，基准50"""
    return max(0, min(100, round(50 + s * 1.5)))

def fmt_pct(v):
    return f"{v:+.2f}%"

def hold_advice(side, score, fr_trend, oi_sig, taker_sig, pct):
    """根据信号给出持仓建议"""
    lines = []

    # CG综合评分
    if score >= SCORE_GOOD:
        lines.append(f"✅ 综合评分顺势({score:+d})，信号支持续持")
    elif score <= SCORE_HOLD_MIN:
        lines.append(f"⚠️ 综合评分偏弱({score:+d})，建议收紧止损或减仓")
    else:
        lines.append(f"⚪ 综合评分中性({score:+d})，正常持有")

    # OI信号
    if side == "LONG":
        if oi_sig == "accumulate":
            lines.append("📈 OI持续增加（多头建仓中）→ 趋势未结束")
        elif oi_sig == "deleverage":
            lines.append("📉 OI下降（去杠杆）→ 关注是否止损踩踏")
    else:
        if oi_sig == "accumulate":
            lines.append("📈 OI持续增加（多头入场）→ 空单压力增大")
        elif oi_sig == "deleverage":
            lines.append("📉 OI下降 → 空单方向可能受益")

    # 资金费率
    if side == "LONG":
        if fr_trend == "falling":
            lines.append("💸 资金费率下行 → 多头持仓成本降低")
        elif fr_trend == "rising":
            lines.append("💸 资金费率上行 → 多头持仓成本增加")
    else:
        if fr_trend == "rising":
            lines.append("💸 资金费率上行 → 空头收益增加")
        elif fr_trend == "falling":
            lines.append("💸 资金费率下行 → 空头持仓成本增加")

    # 大单流向
    if side == "LONG" and taker_sig == "strong_sell":
        lines.append("🐋 大单主卖强势 → 多单短期承压")
    elif side == "LONG" and taker_sig == "strong_buy":
        lines.append("🐋 大单主买强势 → 多单顺势")
    elif side == "SHORT" and taker_sig == "strong_buy":
        lines.append("🐋 大单主买强势 → 空单承压")
    elif side == "SHORT" and taker_sig == "strong_sell":
        lines.append("🐋 大单主卖强势 → 空单顺势")

    # 浮亏
    if pct < WARN_LOSS_PCT:
        lines.append(f"⚠️ 浮亏较大({fmt_pct(pct)})，确认止损位有效")

    return lines


def run():
    # 获取持仓
    positions = api.get_positions()

    if not positions:
        print("NO_POSITION")
        return

    # 获取账户余额
    balance  = api.request('GET', f'{api.BASE_FAPI}/fapi/v2/balance')
    usdt     = next((b for b in balance if b.get('asset') == 'USDT'), {}) if isinstance(balance, list) else {}
    equity   = float(usdt.get('crossWalletBalance', 0))
    total_upnl = sum(float(p.get('unRealizedProfit', 0)) for p in positions)

    lines = []
    lines.append(f"📊 持仓报告 | {time.strftime('%m-%d %H:%M', time.gmtime())} UTC")
    lines.append(f"权益 {equity:.0f}U | 总浮盈 {total_upnl:+.1f}U")
    lines.append("")

    for p in positions:
        sym   = p['symbol']
        amt   = float(p['positionAmt'])
        ep    = float(p['entryPrice'])
        mp    = float(p['markPrice'])
        upnl  = float(p.get('unRealizedProfit', 0))
        side  = 'LONG' if amt > 0 else 'SHORT'
        pct   = (mp - ep) / ep * 100 * (1 if amt > 0 else -1)
        side_cn = '多' if side == 'LONG' else '空'
        icon  = emoji_pnl(upnl)

        # ── CG 综合评分 ─────────────────────────────────────────────────────
        try:
            sig   = cg.get_signal_for_open(sym, side, mp)
            score = sig.get('score_adjust', 0)
            dr    = sig.get('deriv_sig', {})
            fr_trend  = dr.get('fr_trend', 'neutral')
            oi_sig    = dr.get('oi_signal', 'neutral')
            taker_sig = dr.get('taker_signal', 'neutral')
        except Exception:
            score = 0
            fr_trend = oi_sig = taker_sig = 'neutral'

        # ── 一句话建议 ──────────────────────────────────────────────────────
        s100 = score_to_100(score)
        if score >= SCORE_GOOD:
            adv = '✅ 建议继续持有'
        elif score <= SCORE_HOLD_MIN:
            adv = '⚠️ 建议离场或收紧止损'
        else:
            adv = '⚪ 保持观望，正常持有'

        side_full = '多头' if side == 'LONG' else '空头'
        lines.append(f"{icon} {sym} {side_full}")
        lines.append(f"浮盈：{upnl:+.1f}U（{pct:+.1f}%）｜综合评分：{s100}/100")
        lines.append(f"操作提示：{adv}")
        lines.append("")

    print("\n".join(lines))


if __name__ == '__main__':
    run()
