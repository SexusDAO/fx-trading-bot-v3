"""
BallsCoin 回测 v2 — 修正豁免条件后的对比
核心修正：龙头豁免加入"拉盘速度"检查
  - 快速拉盘（<10min 内从 +0% 涨到 +100%）= 脉冲，不豁免
  - 缓慢上涨（>20min 到达 +100%）= 龙头，豁免
"""

# ── BallsCoin 真实时序（含时间信息用于速度判断）──────────────────────────────
# 格式：(秒偏移, pnl%, mc_usd, liq_usd, vol_h1_usd)
TIMELINE = [
    (  0,  0.000, 20_000,  5_943,  23_535),
    ( 11,  0.067, 20_000,  6_190,  23_535),
    ( 73,  0.077, 20_000,  6_230,   7_819),
    ( 95,  0.077, 20_000,  6_230,   7_819),
    (116,  0.103, 20_000,  6_320,   7_900),
    (138,  0.103, 20_000,  6_320,   7_900),
    (163,  0.026, 20_000,  6_046,   6_500),
    (185,  0.026, 20_000,  6_046,   6_500),
    (211,  0.245, 20_000,  6_818,   9_000),
    (232,  0.245, 20_000,  6_818,   9_000),
    (256,  1.103, 40_000,  9_671,  18_000),   # +110% 出现在 t+256s (约4.3分钟)
    (278,  1.103, 40_000,  9_671,  18_000),
    (302,  1.166, 40_000,  9_501,  16_000),   # 峰值 +116%
    (324,  1.166, 40_000,  9_501,  16_000),
    (348,  0.040, 23_000,  7_200,   4_000),   # 砸盘 +4%
]

ENTRY_USD    = 75.0
ENTRY_QTY    = 4_013_658.0
ENTRY_PRICE  = ENTRY_USD / ENTRY_QTY
ENTRY_LIQ    = 5_943.0
ENTRY_MC     = 20_000.0
ENTRY_TIME   = 0  # 相对 t=0

# ── 止盈参数 ─────────────────────────────────────────────────────────────────
TP_TIERS_MC    = [(50_000, 0.45), (150_000, 0.25), (300_000, 0.15)]
TP_TIERS_PNL   = [(0.50, 0.25), (1.00, 0.20), (2.00, 0.15)]
TRAILING_TIERS = [(0.50, 0.30), (1.00, 0.35), (2.00, 0.40), (5.00, 0.45)]
PNL_MC_EXEMPT  = 50_000

# 龙头豁免参数（v2 修正版）
PNL_LIQ_RATIO      = 1.5    # liq >= 入场 1.5x
PNL_VOL_RATIO      = 0.60   # vol_h1 >= 峰值 60%
PNL_SLOW_PUMP_SECS = 600    # 从 +0% 到 +50% 必须超过 10 分钟才算"缓慢上涨"


def run_backtest(label, use_pnl_tiers, use_slow_pump_check):
    remaining_qty  = ENTRY_QTY
    triggered_mc   = set()
    triggered_pnl  = set()
    peak_pnl       = 0.0
    peak_vol_h1    = 0.0
    t_first_50pct  = None   # 第一次达到 +50% 的时间
    total_usd      = 0.0
    sells          = []

    print(f"\n{'='*62}")
    print(f"  {label}")
    print(f"{'='*62}")

    for t, pnl, mc, liq, vol_h1 in TIMELINE:
        if remaining_qty <= 0:
            break

        if pnl > peak_pnl:
            peak_pnl = pnl
        if vol_h1 > peak_vol_h1:
            peak_vol_h1 = vol_h1
        if pnl >= 0.50 and t_first_50pct is None:
            t_first_50pct = t

        # ── PnL 阶梯止盈 ────────────────────────────────────────────────
        if use_pnl_tiers and mc < PNL_MC_EXEMPT:
            for pnl_thresh, sell_ratio in TP_TIERS_PNL:
                pnl_key = f"pnl_{int(pnl_thresh*100)}"
                if pnl_key in triggered_pnl:
                    continue
                if pnl < pnl_thresh:
                    continue

                # 龙头豁免检查
                liq_strong   = liq >= ENTRY_LIQ * PNL_LIQ_RATIO
                vol_strong   = peak_vol_h1 > 0 and vol_h1 >= peak_vol_h1 * PNL_VOL_RATIO

                # v2 新增：拉盘速度检查
                # 从买入到首次 +50% 不到 10 分钟 → 脉冲式拉盘，不豁免
                pump_is_slow = (t_first_50pct is not None and
                                (t_first_50pct - ENTRY_TIME) >= PNL_SLOW_PUMP_SECS)

                is_strong = (liq_strong or vol_strong) and (not use_slow_pump_check or pump_is_slow)

                if is_strong:
                    pump_t = t_first_50pct - ENTRY_TIME if t_first_50pct else 0
                    print(f"  t+{t:>3}s  PnL={pnl:+.0%}  🐉 龙头豁免+{pnl_thresh:.0%} "
                          f"(拉速={pump_t}s {'缓慢✓' if pump_is_slow else '脉冲✗'}) → 等MC接管")
                    continue

                triggered_pnl.add(pnl_key)
                sell_qty = remaining_qty * sell_ratio
                sell_usd = sell_qty * ENTRY_PRICE * (1 + pnl)
                remaining_qty -= sell_qty
                total_usd     += sell_usd
                sells.append((t, f"PnL+{pnl_thresh:.0%}", sell_ratio, pnl, sell_usd))
                print(f"  t+{t:>3}s  PnL={pnl:+.0%}  MC=${mc/1000:.0f}k  "
                      f"💰 PnL止盈+{pnl_thresh:.0%} → 卖{sell_ratio:.0%} (${sell_usd:.1f}) "
                      f"剩余{remaining_qty/ENTRY_QTY:.0%}")
                break

        # ── MC 阶梯止盈 ──────────────────────────────────────────────────
        for mc_thresh, sell_ratio in TP_TIERS_MC:
            if mc_thresh in triggered_mc or mc < mc_thresh:
                continue
            triggered_mc.add(mc_thresh)
            sell_qty = remaining_qty * sell_ratio
            sell_usd = sell_qty * ENTRY_PRICE * (1 + pnl)
            remaining_qty -= sell_qty
            total_usd     += sell_usd
            sells.append((t, f"MC${mc_thresh/1000:.0f}k", sell_ratio, pnl, sell_usd))
            print(f"  t+{t:>3}s  PnL={pnl:+.0%}  MC=${mc/1000:.0f}k  "
                  f"📈 MC止盈${mc_thresh/1000:.0f}k → 卖{sell_ratio:.0%} (${sell_usd:.1f}) "
                  f"剩余{remaining_qty/ENTRY_QTY:.0%}")

        # ── Trailing Stop ─────────────────────────────────────────────────
        if remaining_qty > 0:
            active = None
            for thresh, max_dd in sorted(TRAILING_TIERS, reverse=True):
                if peak_pnl >= thresh:
                    active = (thresh, max_dd)
                    break
            if active:
                thresh, max_dd = active
                drawdown = (peak_pnl - pnl) / (1 + peak_pnl)
                if drawdown >= max_dd:
                    sell_usd = remaining_qty * ENTRY_PRICE * (1 + pnl)
                    total_usd     += sell_usd
                    sells.append((t, f"Trailing回撤{drawdown:.0%}", 1.0, pnl, sell_usd))
                    print(f"  t+{t:>3}s  PnL={pnl:+.0%}  MC=${mc/1000:.0f}k  "
                          f"📉 Trailing 峰值{peak_pnl:+.0%}→现{pnl:+.0%} "
                          f"回撤{drawdown:.0%}≥{max_dd:.0%} → 清仓 (${sell_usd:.1f})")
                    remaining_qty = 0
                    break

    # 剩余仓位
    if remaining_qty > 0:
        last_pnl = TIMELINE[-1][1]
        last_usd = remaining_qty * ENTRY_PRICE * (1 + last_pnl)
        total_usd += last_usd
        print(f"  [剩余{remaining_qty/ENTRY_QTY:.0%}仓位 按最终价估算 ${last_usd:.1f}]")

    net = total_usd - ENTRY_USD
    pct = net / ENTRY_USD
    print(f"{'─'*62}")
    print(f"  成本 ${ENTRY_USD:.1f} → 回收 ${total_usd:.1f} | 净盈亏 {'+' if net>=0 else ''}${net:.1f} ({pct:+.1%})")
    for t, r, ratio, p, usd in sells:
        print(f"    ▶ t+{t:>3}s  {r:<22} PnL={p:+.0%}  收 ${usd:.2f}")
    print(f"{'='*62}")
    return total_usd, net, pct


if __name__ == "__main__":
    print("\n🔁  BallsCoin 回测 — 三种工作流对比")
    print("    买入 $75 | 峰值 +117% (t+302s, 约5min) | MC峰值 $40k | 最终 +4%")

    # 场景 A：旧工作流
    _, a_net, a_pct = run_backtest(
        "A. 旧工作流  （仅MC止盈 + Trailing）",
        use_pnl_tiers=False, use_slow_pump_check=False
    )
    # 场景 B：新工作流 v1（PnL止盈，但豁免不含速度检查）
    _, b_net, b_pct = run_backtest(
        "B. 新工作流 v1（PnL止盈 + 龙头豁免，无速度检查）",
        use_pnl_tiers=True, use_slow_pump_check=False
    )
    # 场景 C：新工作流 v2（PnL止盈 + 豁免含速度检查）
    _, c_net, c_pct = run_backtest(
        "C. 新工作流 v2（PnL止盈 + 龙头豁免含速度检查）",
        use_pnl_tiers=True, use_slow_pump_check=True
    )

    print(f"\n{'─'*62}")
    print(f"  {'场景':<42} {'净盈亏':>8}  {'收益率':>8}")
    print(f"  {'─'*58}")
    for lbl, net, pct in [
        ("A. 旧工作流（基准）", a_net, a_pct),
        ("B. 新v1（无速度检查）", b_net, b_pct),
        ("C. 新v2（含速度检查 ✅ 推荐）", c_net, c_pct),
    ]:
        arrow = "→ 基准" if "A." in lbl else f"→ vs旧 {'+' if net-a_net>=0 else ''}${net-a_net:.1f}"
        print(f"  {lbl:<42} ${net:>6.1f}   {pct:>+7.1%}  {arrow}")
    print()
