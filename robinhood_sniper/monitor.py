"""
robinhood_sniper/monitor.py
第五层：持仓监控  第六层：自动退出
- 市值驱动止盈（normal / narrative / platform 三套阶梯）
- 先回本原则：2x 时卖 50% 确保本金安全
- 成交量衰减预警
- 流动性崩塌 / 硬止损 / 超时清仓
"""

import time
import logging
import json
import os
from dataclasses import dataclass, field
from typing import Optional
from .signal import fetch_pair_data, mark_sl_cooldown
from .executor import get_token_balance, _baw, cancel_limit_order, setup_exit_orders
from .blacklist import add_to_blacklist
from .config import (
    CHAIN_ID, NATIVE_TOKEN,
    MONITOR_INTERVAL_SEC,
    STOP_LOSS_PCT, LIQUIDITY_DROP_STOP, LIQUIDITY_EMERGENCY,
    DYNAMIC_SL_STAGE1_SEC, DYNAMIC_SL_STAGE2_SEC,
    DYNAMIC_SL_STAGE1_PCT, DYNAMIC_SL_STAGE2_PCT, DYNAMIC_SL_STAGE3_PCT,
    PRICE_CRASH_1MIN_PCT, MAX_HOLD_HOURS,
    SLIPPAGE_HOT, SLIPPAGE_SELL, GAS_LEVEL,
    TP_EARLY, TP_NORMAL, TP_NARRATIVE, TP_PLATFORM,
    VOLUME_DECAY_TRIGGER, BREAKEVEN_MC_USD,
    LIQUIDITY_DEAD_USD, VOLUME_DEAD_DECAY, VOLUME_DEAD_ABS_USD,
    VOLUME_DEAD_EARLY_MAX_MIN, VOLUME_DEAD_EARLY_RATIO, VOLUME_DEAD_EARLY_MIN_H1,
    WATCH_WINDOW_SEC, WATCH_FORCE_STOP_LIQ_DROP, WATCH_RECOVERY_PCT,
    TRAILING_STOP_TIERS,
    NARRATIVE_UPGRADE_ENABLED, NARRATIVE_UPGRADE_MIN_PNL,
    NARRATIVE_UPGRADE_MIN_VOL, NARRATIVE_UPGRADE_VOL_RATIO,
    NARRATIVE_UPGRADE_MIN_LIQ,
    MIN_SELL_VALUE_USD,
)

logger = logging.getLogger("sniper.monitor")

# 共识币止盈里程碑（MC 已经更大，$500K/$1M/$3M/$10M）
TP_CONSENSUS = [
    (100_000,    0.30),   # MC $10万 → 卖 30% 回本（共识币入场MC $30-100k）
    (500_000,    0.25),   # MC $50万 → 卖 25%
    (2_000_000,  0.25),   # MC $200万 → 卖 25%
    (10_000_000, 0.20),   # MC $1000万 → 卖 20%
]
_TP_MAP = {"early": TP_EARLY, "normal": TP_NORMAL, "narrative": TP_NARRATIVE,
           "platform": TP_PLATFORM, "consensus": TP_CONSENSUS}


@dataclass
class Position:
    # ── 必填字段 ──
    token_address:   str
    token_symbol:    str
    entry_price_usd: float
    entry_qty:       float
    # ── 可选字段（有默认值）──
    token_type:         str   = "normal"
    entry_time:         float = field(default_factory=time.time)
    entry_liq_usd:      float = 0.0
    peak_volume_h1:     float = 0.0
    peak_pnl:           float = 0.0
    creator_address:    str   = ""
    _stop_loss_override: float = 0.0  # 非0时覆盖默认止损（共识币用-20%）
    # list 类型必须用 field
    sl_order_ids:       list  = field(default_factory=list)
    tp_triggered_mc:    list  = field(default_factory=list)
    # ── 运行时状态 ──
    closed:             bool  = False
    pnl_pct:            float = 0.0
    remaining_qty:      float = 0.0
    breakeven_done:     bool  = False
    prev_price_1min:    float = 0.0
    prev_price_ts:      float = 0.0
    # ── 待观察止损 ──
    watching:           bool  = False
    watch_triggered_at: float = 0.0
    # ── 卖出重试 ──
    exit_pending:       bool  = False
    exit_pending_since: float = 0.0    # exit_pending 首次标记时间戳（检测死循环）
    pool_dead:          bool  = False   # 池子异常（连续失败→低频重试模式）
    pool_dead_retry_at: float = 0.0    # pool_dead 模式下下次允许重试的时间戳
    last_vol_warn_ts:   float = 0.0
    sell_fail_count:    int   = 0      # 连续卖出失败次数（含余额误报）
    # ── PnL 止盈速度计时 ──
    first_50pct_time:   float = 0.0  # 首次达到 +50% 的时间戳（0=未触发）


class PositionMonitor:
    def __init__(self):
        self.positions: list[Position] = []

    # ─────────────────────────────────────────────────────────────────────
    def add(self, pos: Position):
        pos.remaining_qty = pos.entry_qty
        self.positions.append(pos)
        ladder = _TP_MAP.get(pos.token_type, TP_NORMAL)
        milestones = " → ".join(f"${mc/1e6:.1f}M" for mc, _ in ladder)
        logger.info(
            f"[Monitor] 开始监控 {pos.token_symbol} "
            f"type={pos.token_type} | 止盈里程碑: {milestones}"
        )

    # ─────────────────────────────────────────────────────────────────────
    def run_once(self, notify_fn=None) -> list[str]:
        self._last_notify_fn = notify_fn  # 供 _emergency_exit 使用
        events = []
        for pos in list(self.positions):
            if pos.closed:
                continue
            events.extend(self._check(pos, notify_fn))
        return events

    # ─────────────────────────────────────────────────────────────────────
    def _check(self, pos: Position, notify_fn) -> list[str]:
        events = []

        pair = fetch_pair_data(pos.token_address)
        if not pair:
            return events

        price     = float(pair.get("priceUsd") or 0)
        liq       = float((pair.get("liquidity") or {}).get("usd", 0) or 0)
        fdv       = float(pair.get("fdv") or 0)
        mc        = float(pair.get("marketCap") or fdv)
        vol_h1    = float((pair.get("volume") or {}).get("h1", 0) or 0)
        vol_m5    = float((pair.get("volume") or {}).get("m5", 0) or 0)

        if price <= 0:
            return events

        # ── 1min 价格崩溃检测（Rug 极速止损）────────────────────────────────
        # P2 修复：防重复触发 —— 如果已经是 exit_pending/pool_dead 状态，
        # 说明上轮卖出尝试过（包括和极速崩盘触发），不再重复推送清仓通知
        now = time.time()
        if pos.prev_price_1min <= 0:
            pos.prev_price_1min = price
            pos.prev_price_ts   = now
        elif now - pos.prev_price_ts >= 60:
            crash_pct = (price - pos.prev_price_1min) / pos.prev_price_1min
            if crash_pct <= PRICE_CRASH_1MIN_PCT:
                # 已在 exit_pending/pool_dead 中：不发通知，直接返回（防重复盘崩通知）
                if pos.exit_pending or pos.pool_dead:
                    return events
                ok = self._emergency_exit(pos)
                msg = (f"{'✅' if ok else '❌'} [{pos.token_symbol}] "
                       f"🚨 1min 极速崩盘 {crash_pct:.0%}，Rug 清仓")
                if notify_fn: notify_fn(msg)
                return [msg]
            # 滚动更新 1min 基准
            pos.prev_price_1min = price
            pos.prev_price_ts   = now

        if vol_h1 > pos.peak_volume_h1:
            pos.peak_volume_h1 = vol_h1

        # entry_price_usd=0 发生在探针仓买入后余额查询失败，用当前价代替
        if pos.entry_price_usd <= 0:
            pos.entry_price_usd = price if price > 0 else 1e-18
        now_ts = time.time()
        pnl = (price - pos.entry_price_usd) / pos.entry_price_usd
        pos.pnl_pct = pnl
        if pnl > pos.peak_pnl:
            pos.peak_pnl = pnl
        # 记录首次达到 +50% 的时间（用于龙头豁免的脚冲速度判断）
        if pnl >= 0.50 and pos.first_50pct_time == 0.0:
            pos.first_50pct_time = now_ts

        # ── 优先处理上轮卖出失败（exit_pending）─────────────────────────────
        # P0 修复：区分"临时失败"和"池子已死"
        #   - pool_dead=False：每次 _check 都重试（20s 间隔，快速出场）
        #   - pool_dead=True ：改为低频重试（每 120s 一次），同时通知用户手动干预
        #     连续失败超过 30 分钟后放弃自动重试，保留仓位让用户处理
        # remaining_qty 可能因浮点漂移变为极小值（<1e-4）但非零，
        # 此时也应走 exit_pending 重试逻辑（余额校准会在 _partial_sell 里修正）
        _ep_qty = pos.remaining_qty if pos.remaining_qty > 1e-4 else 0
        if pos.exit_pending and _ep_qty > 0:
            _now = time.time()

            if pos.pool_dead:
                # 低频重试模式：每 120s 才尝试一次
                if _now < pos.pool_dead_retry_at:
                    return events  # 还没到重试时间，跳过本轮
                # 池子已死超过 30 分钟 → 停止自动重试，通知用户
                if pos.exit_pending_since > 0 and _now - pos.exit_pending_since > 1800:
                    logger.warning(
                        f"[Monitor] {pos.token_symbol} pool_dead 超30min无法出场，"
                        f"请手动执行: baw market-order swap "
                        f"--fromToken {pos.token_address} --toToken 0xeeee...eeee --slippage 49"
                    )
                    # 每 10 分钟再提醒一次，不彻底放弃
                    pos.pool_dead_retry_at = _now + 600
                    return events
                logger.warning(
                    f"[Monitor] {pos.token_symbol} pool_dead 低频重试 "
                    f"(已等待 {(_now - pos.exit_pending_since)/60:.0f}min)..."
                )
            else:
                logger.warning(f"[Monitor] {pos.token_symbol} exit_pending 重试卖出...")

            ok, data = self._partial_sell(pos, pos.remaining_qty, "exit_pending重试")
            if ok:
                pos.pool_dead = False
                pos.exit_pending_since = 0.0
                msg = f"✅ [{pos.token_symbol}] 延迟出场成功"
                events.append(msg)
                if notify_fn: notify_fn(msg)
            else:
                # 检测是否是池子异常（insufficient balance = 报价返回0，真实池子已死）
                err_str = str(data).lower()
                is_pool_err = ("insufficient" in err_str or "order_api_error" in err_str
                               or "30003001" in str(data))
                if is_pool_err and not pos.pool_dead:
                    pos.pool_dead = True
                    pos.pool_dead_retry_at = _now + 120  # 首次进入低频：2min 后重试
                    if pos.exit_pending_since <= 0:
                        pos.exit_pending_since = _now
                    msg = (f"⚠️ [{pos.token_symbol}] 池子异常，切换低频重试模式 "
                           f"(2min 后重试，如持续请手动处理)")
                    logger.warning(f"[Monitor] {msg}")
                    if notify_fn: notify_fn(msg)
                elif pos.pool_dead:
                    # 继续低频，每次延长 2min
                    pos.pool_dead_retry_at = _now + 120
            return events

        # ── 优先检测止损 ──────────────────────────────────────────────────
        exit_reason = None
        now_ts = time.time()
        age_sec = now_ts - pos.entry_time

        # 流动性检测（待观察期内也应用，作为强制触发）
        liq_drop = 0.0
        if pos.entry_liq_usd > 0:
            liq_drop = (pos.entry_liq_usd - liq) / pos.entry_liq_usd
            if liq_drop >= LIQUIDITY_EMERGENCY:
                exit_reason = f"🚨 流动性紧急撤池 -{liq_drop:.0%}"
            elif liq_drop >= LIQUIDITY_DROP_STOP and not exit_reason:
                warn_msg = (f"⚠️ [{pos.token_symbol}] 流动性下降 {liq_drop:.0%} "
                            f"(阈值 {LIQUIDITY_DROP_STOP:.0%}) | 注意撤池风险")
                logger.warning(f"[Monitor] {warn_msg}")
                if notify_fn: notify_fn(warn_msg)

        # 流动性绝对值过低 → 僵尸池，直接清仓（不管相对跌幅）
        if not exit_reason and liq > 0 and liq < LIQUIDITY_DEAD_USD:
            hold_h = (now_ts - pos.entry_time) / 3600
            if hold_h >= 0.5:  # 买入30分钟后才启用，避免误杀新池
                exit_reason = f"💀 流动性枯竭 ${liq:.0f}<${LIQUIDITY_DEAD_USD:.0f} 僵尸池清仓"

        # ── 早期成交量死亡止损（买入10分钟内，5min量极低 → 无人接盘，不等价格跌够）──
        # 共识币（2h+稳定币）豁免早期量死止损
        if not exit_reason and pos.token_type != "consensus" and vol_m5 >= 0 and vol_h1 >= VOLUME_DEAD_EARLY_MIN_H1:
            hold_min = (now_ts - pos.entry_time) / 60
            if hold_min <= VOLUME_DEAD_EARLY_MAX_MIN:
                vol_h1_avg_per_5min = vol_h1 / 12  # h1 均摊到每个 5min 窗口
                if vol_h1_avg_per_5min > 0 and vol_m5 < vol_h1_avg_per_5min * VOLUME_DEAD_EARLY_RATIO:
                    exit_reason = (
                        f"📉 早期量死止损 5min=${vol_m5:.0f} < "
                        f"h1均值${vol_h1_avg_per_5min:.0f}×{VOLUME_DEAD_EARLY_RATIO:.0%} "
                        f"(持仓{hold_min:.1f}min)"
                    )

        # 时间止损：超过 MAX_HOLD_HOURS 强制清仓
        # 豁免条件：已回本 OR 已触止盈 且当前仍盈利 → 免费筹码不做时间止损
        if not exit_reason and (now_ts - pos.entry_time) / 3600 >= MAX_HOLD_HOURS:
            is_free_chips = (pos.breakeven_done or bool(pos.tp_triggered_mc)) and pnl > 0
            if is_free_chips:
                logger.info(
                    f"[Monitor] {pos.token_symbol} 持仓超时 {MAX_HOLD_HOURS}h "
                    f"但已回本/止盈且盈利 PnL={pnl:.1%}，豁免时间止损"
                )
            else:
                exit_reason = f"⏰ 持仓超时 {MAX_HOLD_HOURS}h 强制清仓"

        # ── 三段式动态止损：买入早期放宽止损，避免被短期波动误止损 ──────────
        # 阶段1(0-3min): -65%, 阶段2(3-10min): -45%, 阶段3(10min+): -28%
        # 共识币用固定止损（不需要早期放宽，本身就是稳定币）
        if pos._stop_loss_override != 0.0:
            effective_sl = pos._stop_loss_override
        elif age_sec <= DYNAMIC_SL_STAGE1_SEC:
            effective_sl = DYNAMIC_SL_STAGE1_PCT
        elif age_sec <= DYNAMIC_SL_STAGE2_SEC:
            effective_sl = DYNAMIC_SL_STAGE2_PCT
        else:
            effective_sl = DYNAMIC_SL_STAGE3_PCT

        # PnL 止损：5分钟待观察逻辑（使用动态止损线）
        if not exit_reason and pnl <= effective_sl:
            in_watch_window = age_sec <= WATCH_WINDOW_SEC
            if in_watch_window and liq_drop < WATCH_FORCE_STOP_LIQ_DROP:
                # 买入后5分钟内且流动性正常 → 待观察，不立即止损
                if not pos.watching:
                    pos.watching = True
                    pos.watch_triggered_at = now_ts
                    stage = 1 if age_sec <= DYNAMIC_SL_STAGE1_SEC else (2 if age_sec <= DYNAMIC_SL_STAGE2_SEC else 3)
                    msg = (f"👁️ [{pos.token_symbol}] PnL={pnl:.1%} 触发动态止损线{effective_sl:.0%}(阶段{stage}/{age_sec:.0f}s)，"
                           f"待观察 | 流动性正常，等回调...")
                    logger.info(f"[Monitor] {msg}")
                    events.append(msg)
                    if notify_fn: notify_fn(msg)
                # 待观察期间继续等下一轮
            elif pos.watching:
                # 待观察窗口小结
                watch_elapsed = now_ts - pos.watch_triggered_at
                if pnl >= WATCH_RECOVERY_PCT:
                    # 选项A：PnL回升到阈值 → 解除待观察
                    pos.watching = False
                    pos.watch_triggered_at = 0.0
                    msg = (f"✅ [{pos.token_symbol}] 待观察期间回调至 PnL={pnl:.1%}，"
                           f"解除待观察模式")
                    logger.info(f"[Monitor] {msg}")
                    events.append(msg)
                    if notify_fn: notify_fn(msg)
                elif pos.peak_pnl < 0.05 and watch_elapsed >= 60:
                    # 选项B：待观察期内峰值反弹 < +5% 且已过60s → 直接止损
                    # （WORTHLESS 式：买同40s就-24.5%，反弹+13.8%后继续跌，总峰值不到5%不应等待）
                    exit_reason = (f"🛑 待观察止损(峰值反弹不足+5%) PnL={pnl:.1%} peak={pos.peak_pnl:.1%}")
                elif watch_elapsed >= WATCH_WINDOW_SEC or liq_drop >= WATCH_FORCE_STOP_LIQ_DROP:
                    # 选项C：5分钟到期仍在止损线下 → 执行硬止损
                    reason = "5min到期" if watch_elapsed >= WATCH_WINDOW_SEC else "流动性下滑"
                    exit_reason = (f"🛑 待观察止损({reason}) PnL={pnl:.1%}")
            else:
                # 超过待观察窗口（买入>5分钟）→ 直接硬止损
                exit_reason = f"🛑 硬止损 PnL={pnl:.1%}"

        if exit_reason:
            ok = self._emergency_exit(pos)
            msg = f"{'✅' if ok else '❌'} [{pos.token_symbol}] {exit_reason} | PnL {pnl:.1%}"
            events.append(msg)
            if notify_fn: notify_fn(msg)
            # 亏损出场（止损）→ 加入 24h 冷却名单，防止重复摂入
            if pnl < -0.10:  # PnL < -10% 才当停损，徽小解出不记
                mark_sl_cooldown(pos.token_address, pos.token_symbol, pnl)
            # 亏损出场（非正常止盈）→ 自动拉黑创建者地址
            if pnl < -0.30 and pos.creator_address:
                add_to_blacklist(pos.creator_address, pos.token_symbol,
                                 f"{exit_reason} PnL={pnl:.1%}")
            return events

        # ── 先回本原则：MC 到 $15万 卖 30% ─────────────────────────────
        # 用MC阈值而非价格2x，防止低MC买入时过早清仓
        if not pos.breakeven_done and mc >= BREAKEVEN_MC_USD:
            qty = round(pos.remaining_qty * 0.30, 6)
            ok, _ = self._partial_sell(pos, qty, f"回本 MC${mc/1000:.0f}k 卖30%")
            if ok:
                pos.breakeven_done = True
                _save_position_state(pos)
                msg = (f"💰 [{pos.token_symbol}] MC ${mc/1000:.0f}k 回本，卖出 30% ({qty:.4f}) "
                       f"| 剩余 {pos.remaining_qty:.4f} 继续持有")
                events.append(msg)
                if notify_fn: notify_fn(msg)

        # ── 龙头动态升级：early → narrative ──────────────────────────────
        # 触发条件：盈利充足 + 流动性健康 + 成交量未衰减 → 说明是真实龙头，拿更久
        if (NARRATIVE_UPGRADE_ENABLED
                and pos.token_type in ("early", "normal")
                and not pos.closed):
            try:
                _upgrade = False
                if (pnl >= NARRATIVE_UPGRADE_MIN_PNL              # 盈利 >= 150%
                        and vol_h1 >= NARRATIVE_UPGRADE_MIN_VOL   # 1h 成交量充足
                        and liq   >= NARRATIVE_UPGRADE_MIN_LIQ    # 流动性健康
                        and pos.peak_volume_h1 > 0
                        and vol_h1 >= pos.peak_volume_h1 * NARRATIVE_UPGRADE_VOL_RATIO):  # 未大幅衰减
                    _upgrade = True
                if _upgrade:
                    old_type = pos.token_type
                    pos.token_type = "narrative"
                    # 重置已触发里程碑：从当前 MC 重新计算 narrative 档位
                    pos.tp_triggered_mc = [
                        mc_t for mc_t, _ in TP_NARRATIVE if mc_t <= mc
                    ]
                    _upg_msg = (
                        f"🚀 [{pos.token_symbol}] 龙头升级 {old_type}→narrative "
                        f"PnL={pnl:.0%} vol1h=${vol_h1:,.0f} liq=${liq:,.0f} "
                        f"| 切换至大里程碑止盈"
                    )
                    logger.info(f"[Monitor] {_upg_msg}")
                    if notify_fn: notify_fn(_upg_msg)
                    events.append(_upg_msg)
            except Exception:
                pass

        # ── PnL 阶梯止盈（与 MC 止盈并联，解决「低MC大涨幅」盲区）────────
        # 触发条件：MC < TP_PNL_MC_EXEMPT（MC止盈尚未接管）且 PnL 达到档位
        # 豁免条件：检测到龙头动量信号 → 跳过本档，让 MC 止盈接管拿大行情
        from .config import (TP_PNL_TIERS, TP_PNL_STRONG_LIQ_RATIO,
                             TP_PNL_STRONG_VOL_RATIO, TP_PNL_MC_EXEMPT,
                             TP_PNL_SLOW_PUMP_SECS,
                             TP_PNL_FORCE_THRESHOLD, TP_PNL_FORCE_RATIO)
        if mc < TP_PNL_MC_EXEMPT and pos.remaining_qty > 0 and not pos.closed:
            for pnl_thresh, sell_ratio in TP_PNL_TIERS:
                # 用特殊 key 区分 PnL 档（避免和 MC 档的 tp_triggered_mc 混用）
                pnl_key = f"pnl_{int(pnl_thresh*100)}"
                if pnl_key in pos.tp_triggered_mc:
                    continue
                if pnl >= pnl_thresh:
                    # ── 龙头豁免检查：liq/vol 健康 + 上涨速度缓慢（非脚冲）──
                    is_strong = False
                    try:
                        liq_strong   = (pos.entry_liq_usd > 0
                                        and liq >= pos.entry_liq_usd * TP_PNL_STRONG_LIQ_RATIO)
                        vol_strong   = (pos.peak_volume_h1 > 0
                                        and vol_h1 >= pos.peak_volume_h1 * TP_PNL_STRONG_VOL_RATIO)
                        # 上涨速度：首次 +50% 超过 10 分钟才算缓慢上涨
                        pump_is_slow = (
                            pos.first_50pct_time > 0
                            and (pos.first_50pct_time - pos.entry_time) >= TP_PNL_SLOW_PUMP_SECS
                        )
                        is_strong = (liq_strong or vol_strong) and pump_is_slow
                    except Exception:
                        pass

                    if is_strong:
                        pump_secs = int(pos.first_50pct_time - pos.entry_time) if pos.first_50pct_time > 0 else 0
                        logger.info(
                            f"[Monitor] {pos.token_symbol} PnL={pnl:.0%} 达+{pnl_thresh:.0%}档 "
                            f"龙头豁免（拉盘速={pump_secs}s缓慢 liq/vol健康）等MC接管"
                        )
                        continue  # 跳过本档，等 MC 止盈

                    pos.tp_triggered_mc.append(pnl_key)
                    qty = round(pos.remaining_qty * sell_ratio, 6)
                    if qty > 0:
                        ok, _ = self._partial_sell(pos, qty,
                            f"PnL+{pnl_thresh:.0%} 止盈")
                        msg = (f"💰 [{pos.token_symbol}] PnL {pnl:.0%} 触发 +{pnl_thresh:.0%}档 "
                               f"→ 卖 {sell_ratio*100:.0f}% ({qty:.4f}) "
                               f"| 剩余 {pos.remaining_qty:.4f}")
                        events.append(msg)
                        if notify_fn: notify_fn(msg)
                    break  # 每轮只触发最高档，下轮继续检查

        # ── 高倍强制兜底：PnL >= +500% 必须至少卖一次，不受龙头豁免保护 ──────
        # 防止 is_strong + MC止盈同时跳过，导致暴利区间完全没有卖出
        if pos.remaining_qty > 0 and not pos.closed and pnl >= TP_PNL_FORCE_THRESHOLD:
            force_key = f"force_{int(TP_PNL_FORCE_THRESHOLD*100)}"
            if force_key not in pos.tp_triggered_mc:
                pos.tp_triggered_mc.append(force_key)
                qty = round(pos.remaining_qty * TP_PNL_FORCE_RATIO, 6)
                if qty > 0:
                    ok, _ = self._partial_sell(pos, qty,
                        f"高倍强制兜底 PnL+{pnl:.0%} 卖{TP_PNL_FORCE_RATIO:.0%}")
                    msg = (f"🔥 [{pos.token_symbol}] PnL {pnl:.0%} 触发高倍强制兜底 "
                           f"→ 卖 {TP_PNL_FORCE_RATIO*100:.0f}% ({qty:.4f}) "
                           f"| 不受龙头豁免限制 | 剩余 {pos.remaining_qty:.4f}")
                    events.append(msg)
                    if notify_fn: notify_fn(msg)

        # ── 市值阶梯止盈 ──────────────────────────────────────────────────
        if mc > 0:
            ladder = _TP_MAP.get(pos.token_type, TP_NORMAL)
            for mc_threshold, sell_ratio in ladder:
                if mc_threshold in pos.tp_triggered_mc:
                    continue
                if mc >= mc_threshold:
                    pos.tp_triggered_mc.append(mc_threshold)
                    qty = round(pos.remaining_qty * sell_ratio, 6)
                    if qty <= 0:
                        continue
                    ok, _ = self._partial_sell(pos, qty,
                                               f"MC ${mc_threshold/1e6:.1f}M 止盈")
                    msg = (f"📈 [{pos.token_symbol}] MC ${mc/1e6:.1f}M "
                           f"→ 卖 {sell_ratio*100:.0f}% ({qty:.4f}) "
                           f"| PnL {pnl:.1%} | 剩余 {pos.remaining_qty:.4f}")
                    events.append(msg)
                    if notify_fn: notify_fn(msg)

        # ── 动态回撤保护（Trailing Stop）────────────────────────────────
        # 只在已有显著盈利（peak_pnl > 第一档阈值）且剩余仓位不为零时触发
        if pos.remaining_qty > 0 and not pos.closed:
            active_tier = None
            for thresh, max_drawdown in sorted(TRAILING_STOP_TIERS, reverse=True):
                if pos.peak_pnl >= thresh:
                    active_tier = (thresh, max_drawdown)
                    break
            if active_tier:
                thresh, max_drawdown = active_tier
                # 从峰值的回撤幅度：peak=2.0(+200%), cur=1.0(+100%) → drawdown=0.33
                drawdown_from_peak = (pos.peak_pnl - pnl) / (1 + pos.peak_pnl)
                if drawdown_from_peak >= max_drawdown:
                    qty = pos.remaining_qty
                    ok, _ = self._partial_sell(pos, qty,
                        f"动态止盈 峰值{pos.peak_pnl:.0%}→现{pnl:.0%} 回撤{drawdown_from_peak:.0%}")
                    msg = (f"📉 [{pos.token_symbol}] 触发动态回撤保护 "
                           f"峰值{pos.peak_pnl:+.0%} → 现{pnl:+.0%} "
                           f"回撤{drawdown_from_peak:.0%}≥{max_drawdown:.0%} | 清仓剩余{qty:.4f}")
                    events.append(msg)
                    if notify_fn: notify_fn(msg)
                    if ok:
                        return events  # 已清仓，结束本轮

        # ── 成交量枯竭强制清仓（无人交易的僵尸币）─────────────────────────
        if pos.remaining_qty > 0 and not pos.closed and pos.peak_volume_h1 > 0:
            decay = (pos.peak_volume_h1 - vol_h1) / pos.peak_volume_h1 if vol_h1 > 0 else 1.0
            hold_h = (now_ts - pos.entry_time) / 3600
            if (decay >= VOLUME_DEAD_DECAY and vol_h1 < VOLUME_DEAD_ABS_USD
                    and hold_h >= 1.0):  # 持仓至少1小时后才触发
                qty = pos.remaining_qty
                ok, _ = self._partial_sell(pos, qty,
                    f"成交量枯竭 1h vol=${vol_h1:.0f} 衰减{decay:.0%}")
                msg = (f"💤 [{pos.token_symbol}] 成交量枯竭清仓 "
                       f"1h=${vol_h1:.0f}(峰值${pos.peak_volume_h1:.0f}) 衰减{decay:.0%} "
                       f"| 释放资金 {qty:.4f}枚 | PnL {pnl:.1%}")
                events.append(msg)
                if notify_fn: notify_fn(msg)
                if ok:
                    return events

        # ── 成交量衰减预警（4h 冷却，不重复刷屏）────────────────────────────
        if pos.peak_volume_h1 > 0 and vol_h1 > 0 and pnl > 0:
            decay = (pos.peak_volume_h1 - vol_h1) / pos.peak_volume_h1
            cooldown_ok = (now_ts - pos.last_vol_warn_ts) >= 4 * 3600
            if VOLUME_DECAY_TRIGGER <= decay < VOLUME_DEAD_DECAY and cooldown_ok:
                pos.last_vol_warn_ts = now_ts
                msg = (f"⚠️ [{pos.token_symbol}] 1h 成交量衰减 {decay:.0%} "
                       f"(峰值 ${pos.peak_volume_h1:,.0f} → 现 ${vol_h1:,.0f}) "
                       f"| 注意减仓")
                logger.warning(f"[Monitor] {msg}")
                events.append(msg)
                if notify_fn: notify_fn(msg)

        logger.info(
            f"[Monitor] {pos.token_symbol} | MC=${mc/1e6:.2f}M "
            f"PnL={pnl:.1%} liq=${liq:,.0f} 剩余={pos.remaining_qty:.4f}"
        )
        return events

    # ─────────────────────────────────────────────────────────────────────
    def _partial_sell(self, pos: Position, qty: float, reason: str,
                      _retry: int = 0, _slippage: int = None) -> tuple[bool, dict]:
        """
        卖出执行：失败后 5s 间隔、每次滑点 +3% 递增重试，最多 5 次。
        _retry=0 是首次，_retry>=5 放弃并标记 exit_pending。
        """
        MAX_RETRIES   = 5
        SLIPPAGE_STEP = 5    # 每次失败后加 5%（原3%太慢，RH链Rug后需要更快爬升）
        MAX_SLIPPAGE  = 50   # 滑点上限 50%（原35%不够，Rug行情下真实滑点可达40%+）
        RETRY_DELAY   = 5    # 秒

        # ── 最小卖出价值检查（节省 gas）──────────────────────────────────────
        # 当持仓当前价值 < MIN_SELL_VALUE_USD 时不执行卖出，直接放弃
        # 覆盖场景：GBC 式归零探针仓（价值 $0.03），卖出手续费比回收价值更高
        try:
            _cur_price = pos.entry_price_usd * (1 + pos.pnl_pct) if pos.entry_price_usd > 0 else 0
            _cur_value_usd = qty * _cur_price
            if _cur_value_usd > 0 and _cur_value_usd < MIN_SELL_VALUE_USD:
                logger.info(
                    f"[Monitor] {pos.token_symbol} 卖出价值 ${_cur_value_usd:.4f} "
                    f"< 最小阈值 ${MIN_SELL_VALUE_USD}，放弃卖出节省 gas | {reason}"
                )
                # 直接标记为已关闭，清理仓位
                pos.closed = True
                pos.remaining_qty = 0
                _save_position_state(pos)
                return True, {"skipped": "value_too_low"}
        except Exception:
            pass

        if _slippage is None:
            _slippage = SLIPPAGE_SELL   # 卖出初始滑点20%，失败后每次+5%递增

        # ── qty 最小值兜底：传入 qty 极小时直接用链上余额 ─────────────────────
        if qty < 1.0:
            try:
                _fallback = get_token_balance(pos.token_address)
                if _fallback > 0:
                    logger.info(
                        f"[Monitor] {pos.token_symbol} qty={qty:.6f} 过小，"
                        f"改用链上余额 {_fallback:.4f}"
                    )
                    qty = _fallback
                    pos.remaining_qty = _fallback
                else:
                    pos.remaining_qty = 0
                    pos.closed = True
                    _save_position_state(pos)
                    return True, {"skipped": "qty_too_small_zero_balance"}
            except Exception:
                pass

        # ── 余额自校准：防止 remaining_qty 浮点漂移导致 INVALID_AMOUNT ──────
        # 场景：部分止盈后 remaining_qty 因浮点运算变成极小值（如 0.0000034），
        # 传给 baw 被截断为 0.0000 → 报 INVALID_AMOUNT。
        # 修复：卖出前查链上真实余额；如果链上余额比 qty 多出 >1%，以链上为准。
        if qty < 1.0 or _retry == 0:
            try:
                _real_bal = get_token_balance(pos.token_address)
                if _real_bal > qty * 1.01:
                    # 链上余额明显多于系统记录 → 以链上为准
                    logger.info(
                        f"[Monitor] {pos.token_symbol} 余额校准: "
                        f"系统qty={qty:.4f} → 链上={_real_bal:.4f}，以链上为准"
                    )
                    qty = _real_bal
                    pos.remaining_qty = _real_bal  # 同步修正内存状态
                elif _real_bal <= 0 and qty > 0:
                    # 链上已无余额，系统误以为还有 → 直接关闭
                    logger.info(
                        f"[Monitor] {pos.token_symbol} 链上余额=0，"
                        f"remaining_qty={qty:.4f} 为误报，标记关闭"
                    )
                    pos.remaining_qty = 0
                    pos.closed = True
                    _save_position_state(pos)
                    return True, {"skipped": "zero_balance"}
            except Exception as _be:
                logger.debug(f"[Monitor] {pos.token_symbol} 余额校准失败(非致命): {_be}")

        ok, data = _baw([
            "market-order", "swap",
            "--binanceChainId", CHAIN_ID,
            "--fromToken", pos.token_address,
            "--toToken", NATIVE_TOKEN,
            "--fromTokenQty", str(round(qty, 6)),
            "--slippage", str(min(_slippage, MAX_SLIPPAGE)),
            "--mev", "false",
            "--gasLevel", GAS_LEVEL,
        ])

        if ok:
            pos.remaining_qty = max(0.0, pos.remaining_qty - qty)
            if pos.remaining_qty < 0.000001:
                pos.closed = True
                for oid in (pos.sl_order_ids or []):
                    cancel_limit_order(oid)
                pos.sl_order_ids = []
                # 封层启动异步平仓复盘
                try:
                    import threading as _t
                    _t.Thread(
                        target=_post_trade_review,
                        args=(pos, reason),
                        daemon=True,
                    ).start()
                except Exception as _e:
                    logger.debug(f"[Review] 启动复盘线程失败: {_e}")
            elif pos.sl_order_ids:
                for oid in pos.sl_order_ids:
                    cancel_limit_order(oid)
                new_orders = setup_exit_orders(
                    pos.token_address, pos.remaining_qty, pos.entry_price_usd)
                pos.sl_order_ids = [
                    o["order_id"] for o in new_orders
                    if o["placed"] and o.get("order_id")
                ]
            if _retry > 0:
                logger.info(f"[Monitor] {pos.token_symbol} 第{_retry}次重试卖出成功"
                            f" 滑点={_slippage}%")
            logger.info(f"[Monitor] {pos.token_symbol} 卖出 {qty:.4f} [{reason}] "
                        f"剩余={pos.remaining_qty:.4f}")
            pos.exit_pending = False
            _save_position_state(pos)
        else:
            err_str = str(data).lower()
            # 池子归零/代币消失 → 不重试，直接关闭
            if "insufficient" in err_str and "balance" in err_str and _retry == 0:
                # 先确认是真的余额不足还是滑点问题
                pass  # 继续走重试逻辑
            if _retry >= MAX_RETRIES:
                # 超过最大重试次数：标记 exit_pending，下轮继续尝试
                pos.exit_pending = True
                if pos.exit_pending_since <= 0:
                    pos.exit_pending_since = time.time()
                # 检测是否是池子异常（insufficient balance = 报价0 = 池子已死）
                _err = str(data).lower()
                if ("insufficient" in _err or "order_api_error" in _err
                        or "30003001" in str(data)):
                    if not pos.pool_dead:
                        pos.pool_dead = True
                        pos.pool_dead_retry_at = time.time() + 120
                _save_position_state(pos)
                logger.error(
                    f"[Monitor] {pos.token_symbol} 卖出失败 {MAX_RETRIES} 次，"
                    f"标记 exit_pending pool_dead={pos.pool_dead}，等下轮重试: {data}"
                )
            else:
                # 等 5 秒后用更高滑点重试
                next_slip = min(_slippage + SLIPPAGE_STEP, MAX_SLIPPAGE)
                logger.warning(
                    f"[Monitor] {pos.token_symbol} 卖出失败(第{_retry+1}次)"
                    f" 滑点={_slippage}% → {next_slip}%，5s后重试"
                )
                import time as _time
                _time.sleep(RETRY_DELAY)
                return self._partial_sell(pos, qty, reason,
                                          _retry=_retry + 1, _slippage=next_slip)
        return ok, data

    def _emergency_exit(self, pos: Position) -> bool:
        qty = get_token_balance(pos.token_address)
        if qty <= 0:
            pos.closed = True
            logger.info(f"[Monitor] {pos.token_symbol} 链上余额为0，标记关闭")
            return True
        ok, data = _baw([
            "market-order", "swap",
            "--binanceChainId", CHAIN_ID,
            "--fromToken", pos.token_address,
            "--toToken", NATIVE_TOKEN,
            "--fromTokenQty", str(round(qty, 6)),
            "--slippage", "49",  # 紧急出场用最高滑点49%确保成交（SLIPPAGE_HOT=30不够）
            "--mev", "false",
            "--gasLevel", GAS_LEVEL,
        ])
        if ok:
            pos.closed = True
        else:
            # 卖出失败：区分"真的余额没了"vs"baw 路由临时故障"
            err_str = str(data).lower()
            if "insufficient" in err_str or "balance" in err_str or "30003001" in err_str:
                from robinhood_sniper.executor import get_token_balance as _gtb
                real_bal = _gtb(pos.token_address)
                if real_bal <= 0:
                    pos.closed = True
                    logger.info(f"[Monitor] {pos.token_symbol} 链上余额已为0，标记关闭")
                    return False
                # 链上有余额：baw 路由故障，累计失败次数
                pos.sell_fail_count += 1
                MAX_SELL_FAIL = 5
                if pos.sell_fail_count >= MAX_SELL_FAIL:
                    # 5次 baw 路由失败后，切换为直接卖出模式（同手动卖出逻辑）
                    # fromToken 用代币合约地址，toToken 用 0xeeee（native ETH）
                    # 这和手动执行 "baw market-order swap --fromToken <addr> --toToken 0xeeee" 完全一致
                    from robinhood_sniper.signal import fetch_pair_data as _fpd
                    _pair = _fpd(pos.token_address)
                    cur_price = float((_pair or {}).get("priceUsd") or pos.entry_price_usd or 0)
                    val_usd = real_bal * cur_price
                    _notify = getattr(self, "_last_notify_fn", None)

                    if val_usd < 1.0:
                        pos.closed = True
                        logger.warning(f"[Monitor] {pos.token_symbol} 残值${val_usd:.2f}<$1 直接放弃")
                        return False

                    logger.warning(
                        f"[Monitor] {pos.token_symbol} 连续{MAX_SELL_FAIL}次路由失败，"
                        f"切换直接卖出模式（余额={real_bal:.0f}枚≈${val_usd:.2f}）"
                    )
                    # 直接卖出：fromToken=代币地址, toToken=0xeeee（与手动卖出完全一致）
                    import subprocess as _sp
                    _cmd = [
                        "baw", "market-order", "swap",
                        "--binanceChainId", "4663",
                        "--fromToken", pos.token_address,
                        "--fromTokenQty", str(real_bal),
                        "--toToken", "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
                        "--slippage", "30",
                        "--json"
                    ]
                    try:
                        _r = _sp.run(_cmd, capture_output=True, text=True, timeout=30)
                        import json as _json
                        _res = _json.loads(_r.stdout)
                        if _res.get("success"):
                            pos.closed = True
                            pos.sell_fail_count = 0
                            msg = (f"✅ [{pos.token_symbol}] 直接卖出成功"
                                   f"（orderId={_res.get('data',{}).get('orderId','')}）")
                            logger.info(f"[Monitor] {msg}")
                            if _notify: _notify(msg)
                            return True
                        else:
                            # 直接卖出也失败，这才通知用户手动处理
                            pos.closed = True
                            msg = (f"⚠️ [{pos.token_symbol}] 自动+直接卖出均失败，请手动处理"
                                   f" | 余额={real_bal:.0f}枚≈${val_usd:.2f}"
                                   f" | 执行: baw market-order swap"
                                   f" --fromToken {pos.token_address}"
                                   f" --toToken 0xeeee...eeee --slippage 30")
                            logger.warning(f"[Monitor] {msg}")
                            if _notify: _notify(msg)
                            return False
                    except Exception as _e:
                        pos.closed = True
                        msg = (f"⚠️ [{pos.token_symbol}] 直接卖出异常，请手动处理"
                               f" | {_e}")
                        logger.warning(f"[Monitor] {msg}")
                        if _notify: _notify(msg)
                        return False
                logger.warning(
                    f"[Monitor] {pos.token_symbol} baw路由故障，"
                    f"第{pos.sell_fail_count}/{MAX_SELL_FAIL}次，下轮重试"
                )
                return False
        return ok

    def summary(self) -> str:
        active = [p for p in self.positions if not p.closed]
        closed = [p for p in self.positions if p.closed]
        lines  = [f"📊 持仓概览 | 活跃:{len(active)} 已平:{len(closed)}"]
        for p in active:
            emoji = "🟢" if p.pnl_pct >= 0 else "🔴"
            mc_hits = len(p.tp_triggered_mc)
            lines.append(
                f"  {emoji} {p.token_symbol} [{p.token_type}] "
                f"PnL={p.pnl_pct:.1%} 剩余={p.remaining_qty:.4f} "
                f"止盈已触发={mc_hits}档"
            )
        return "\n".join(lines)


def _save_position_state(pos):
    """持久化持仓状态，防止重启后重置 breakeven_done / tp_triggered_mc"""
    STATE_FILE = os.path.join(os.path.dirname(__file__), ".position_state.json")
    try:
        try:
            all_states = json.load(open(STATE_FILE))
        except Exception:
            all_states = {}
        all_states[pos.token_address.lower()] = {
            "symbol":              pos.token_symbol,
            "breakeven_done":      pos.breakeven_done,
            "tp_triggered_mc":     pos.tp_triggered_mc,
            "entry_price_usd":     pos.entry_price_usd,
            "entry_liq_usd":       pos.entry_liq_usd,
            "peak_volume_h1":      pos.peak_volume_h1,
            "token_type":          pos.token_type,
            "watching":            pos.watching,
            "watch_triggered_at":  pos.watch_triggered_at,
            "peak_pnl":            pos.peak_pnl,
            "entry_time":          pos.entry_time,
            "first_50pct_time":    pos.first_50pct_time,
        }
        json.dump(all_states, open(STATE_FILE, "w"), indent=2)
    except Exception as e:
        logger.warning(f"[Monitor] 状态持久化失败: {e}")


# ═══════════════════════════════════════════════════════════════
# 交易完成后自动复盘 + 优化判断
# ═══════════════════════════════════════════════════════════════

def _post_trade_review(pos, exit_reason: str):
    """
    每笔交易平仓后自动执行：
    1. 查链上真实买入/卖出 ETH（WETH burn）
    2. 计算实际盈亏
    3. 分析出场是否最优，提出优化建议
    4. 若满足自动优化条件，直接修改 config 并通知
    """
    import time, requests, importlib
    from .config import CHAIN_ID

    time.sleep(3)  # 等待链上数据确认

    RPC    = "https://rpc.mainnet.chain.robinhood.com"
    WETH   = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
    AGENT  = "0x284D2c9B296003EF57264fdFf571C3083FA46152"
    PROXIES = {"http": "socks5://REDACTED_PROXY_CREDENTIALS",
               "https":"socks5://REDACTED_PROXY_CREDENTIALS"}
    TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
    ZERO_PAD = "0x" + "0"*64
    agent_pad = "0x000000000000000000000000" + AGENT[2:].lower()

    sess = requests.Session()
    sess.proxies = PROXIES

    def rpc(m, p, t=8):
        try:
            r = sess.post(RPC, json={"jsonrpc":"2.0","method":m,"params":p,"id":1}, timeout=t)
            return r.json().get("result")
        except:
            return None

    sym       = pos.token_symbol
    tok_addr  = pos.token_address.lower()
    entry_ts  = int(pos.entry_time or 0)
    peak_pnl  = pos.peak_pnl
    tp_hit    = pos.tp_triggered_mc or []
    score     = getattr(pos, "score", 0)
    token_type = pos.token_type or "normal"

    logger.info(f"[Review] {sym} 开始链上复盘...")

    # ── 推算扫描块范围 ──────────────────────────────────────────
    try:
        latest_hex = rpc("eth_blockNumber", [])
        latest = int(latest_hex, 16)
        b_latest = rpc("eth_getBlockByNumber", [latest_hex, False])
        ts_now   = int(b_latest["timestamp"], 16)
        delta    = ts_now - entry_ts
        entry_blk = max(0, latest - int(delta / 0.1))
        from_blk  = max(0, entry_blk - 200)
        to_blk    = min(latest, entry_blk + 200000)  # 最多买入后 ~5.5h
    except Exception as e:
        logger.warning(f"[Review] {sym} 块号推算失败: {e}")
        return

    # ── 查买入 ETH ─────────────────────────────────────────────
    buy_eth = 0.0
    try:
        in_logs = rpc("eth_getLogs", [{"address": tok_addr, "fromBlock": hex(from_blk),
                                        "toBlock": hex(to_blk),
                                        "topics": [TRANSFER, None, agent_pad]}]) or []
        seen = set()
        for l in in_logs:
            txh = l["transactionHash"]
            if txh in seen: continue
            seen.add(txh)
            tx = rpc("eth_getTransactionByHash", [txh])
            rc = rpc("eth_getTransactionReceipt", [txh])
            if not tx or not rc: continue
            val = int(tx.get("value") or "0x0", 16) / 1e18
            gas = int(rc["gasUsed"], 16) * int(tx.get("gasPrice") or "0x0", 16) / 1e18
            buy_eth += val + gas
    except Exception as e:
        logger.warning(f"[Review] {sym} 查买入 tx 失败: {e}")

    # ── 查卖出 ETH（WETH burn）──────────────────────────────────
    sell_eth = 0.0
    try:
        out_logs = rpc("eth_getLogs", [{"address": tok_addr, "fromBlock": hex(from_blk),
                                         "toBlock": hex(to_blk),
                                         "topics": [TRANSFER, agent_pad, None]}]) or []
        seen2 = set()
        for l in out_logs:
            txh = l["transactionHash"]
            if txh in seen2: continue
            seen2.add(txh)
            rc = rpc("eth_getTransactionReceipt", [txh])
            tx = rpc("eth_getTransactionByHash", [txh])
            if not rc: continue
            gas = int(rc["gasUsed"], 16) * int((tx.get("gasPrice") or "0x0") if tx else "0x0", 16) / 1e18
            weth_out = 0.0
            for log in rc.get("logs", []):
                if (log["address"].lower() == WETH.lower()
                        and len(log.get("topics", [])) >= 3
                        and log["topics"][0] == TRANSFER
                        and log["topics"][2] == ZERO_PAD):
                    weth_out += int(log["data"], 16) / 1e18
            sell_eth += weth_out - gas
    except Exception as e:
        logger.warning(f"[Review] {sym} 查卖出 tx 失败: {e}")

    # ── 查代币现状（DS）─────────────────────────────────────────
    now_mc = 0.0
    now_pc_h1 = 0.0
    try:
        r = sess.get(f"https://api.dexscreener.com/latest/dex/tokens/{tok_addr}", timeout=6)
        pairs = r.json().get("pairs") or []
        rh = [p for p in pairs if p.get("chainId") == "robinhood"]
        if rh:
            best = max(rh, key=lambda p: float((p.get("liquidity") or {}).get("usd", 0) or 0))
            now_mc    = float(best.get("marketCap") or best.get("fdv") or 0)
            now_pc_h1 = float((best.get("priceChange") or {}).get("h1", 0) or 0)
    except Exception:
        pass

    # ── 计算盈亏 ────────────────────────────────────────────────
    eth_price_usd = 2401.0
    try:
        from .executor import get_eth_price_usd as _gep
        ep = _gep()
        if ep > 100: eth_price_usd = ep
    except Exception:
        pass

    net_eth = sell_eth - buy_eth
    net_usd = net_eth * eth_price_usd
    pct     = net_eth / buy_eth * 100 if buy_eth > 0 else 0.0

    # ── 出场分析 ────────────────────────────────────────────────
    verdict  = ""
    optimize = []

    # 判断1：高峰值却亏损 = 没有锁住利润
    if peak_pnl >= 0.5 and net_usd < 0:
        verdict = "🔴 高峰值亏损"
        # 检查是否是因为 PnL 止盈没触发（MC 太小，买入时MC<$50k）
        if not any(isinstance(x, str) and "pnl" in x for x in tp_hit):
            optimize.append(("PnL止盈未触发", 
                f"峰值+{peak_pnl:.0%} 但 MC 未到 TP1，PnL 止盈应已保住利润"))

    # 判断2：出场 PnL 远低于峰值 = Trailing 过早清出
    exit_pnl = pct / 100
    if peak_pnl > 0 and exit_pnl < peak_pnl * 0.5 and peak_pnl >= 0.5:
        missed_usd = buy_eth * (peak_pnl - exit_pnl) * eth_price_usd
        verdict = verdict or "⚠️ 峰值大幅回撤出场"
        optimize.append(("Trailing 过早",
            f"峰值+{peak_pnl:.0%} → 出场+{exit_pnl:.0%}，损失约${missed_usd:.1f}"))

    # 判断3：出场时仍在上涨（h1>20%）= 出早了
    if now_pc_h1 > 20 and now_mc > 0:
        verdict = verdict or "⚠️ 出场后仍在涨"
        optimize.append(("出场后继续涨",
            f"出场后h1={now_pc_h1:+.0f}% MC=${now_mc/1000:.0f}k，Trailing 太紧"))

    # 判断4：亏损且峰值也低 = 正常止损，无优化空间
    if peak_pnl < 0.3 and net_usd < 0:
        verdict = "✅ 正常止损"
        optimize = []

    # 判断5：盈利且出场合理
    if net_usd > 0 and (not optimize):
        verdict = "✅ 出场合理"

    # ── 自动优化（只做安全的参数调整）────────────────────────────
    auto_actions = []
    # 连续 2 笔以上高峰值亏损 → 提示需要调整，但不自动改（影响较大）
    # 仅记录，待用户确认

    # ── 组装复盘消息 ────────────────────────────────────────────
    lines = [
        f"📋 **{sym} 交易复盘**",
        f"",
        f"{'─'*32}",
        f"💰 链上真实盈亏",
        f"  买入花费:  {buy_eth:.4f} ETH  (${buy_eth*eth_price_usd:.2f})" if buy_eth > 0 else "  买入花费:  ⚠️ 未找到链上记录",
        f"  卖出收回:  {sell_eth:.4f} ETH  (${sell_eth*eth_price_usd:.2f})" if sell_eth > 0 else "  卖出收回:  ⚠️ 未找到链上记录",
        f"  净盈亏:    {net_eth:+.4f} ETH = **{'+' if net_usd>=0 else ''}${net_usd:.2f}** ({pct:+.1f}%)",
        f"",
        f"📊 交易过程",
        f"  入场评分:  {score}/12  类型:{token_type}",
        f"  峰值PnL:   +{peak_pnl:.0%}",
        f"  出场方式:  {exit_reason}",
        f"  止盈触发:  {tp_hit if tp_hit else '无'}",
        f"",
        f"🔍 现状（出场后）",
        f"  MC: ${now_mc/1000:.0f}k  h1价格: {now_pc_h1:+.0f}%" if now_mc > 0 else "  现状: 已归零或流动性极低",
        f"",
        f"{'─'*32}",
        f"**判断: {verdict}**",
    ]

    if optimize:
        lines.append(f"")
        lines.append(f"💡 优化建议:")
        for title, desc in optimize:
            lines.append(f"  • {title}: {desc}")

    msg = "\n".join(lines)
    logger.info(f"[Review] {sym} 复盘完成: {verdict} 净盈亏${net_usd:.2f}")

    # ── 发送到 Jarvis ────────────────────────────────────────────
    # 发到独立线程（不展开主对话历史，节约 token）
    try:
        import subprocess, json as _json
        payload = _json.dumps({
            "action": "send",
            "target": "432401404:thread:019fcac8-4a93-7bea-a930-7f0e59e4897c",
            "message": msg,
            "channel": "jarvis",
        })
        subprocess.run(
            ["openclaw", "message", "--json", payload],
            capture_output=True, timeout=10
        )
    except Exception as e:
        logger.warning(f"[Review] Jarvis 通知失败: {e}")

    # ── 保存复盘记录 ─────────────────────────────────────────────
    try:
        import datetime as _dt
        review_file = os.path.join(os.path.dirname(__file__), ".trade_reviews.jsonl")
        record = {
            "ts": _dt.datetime.utcnow().isoformat(),
            "symbol": sym, "token_type": token_type,
            "score": score, "peak_pnl": peak_pnl,
            "exit_reason": exit_reason, "tp_triggered": tp_hit,
            "buy_eth": buy_eth, "sell_eth": sell_eth,
            "net_eth": net_eth, "net_usd": net_usd, "pct": pct,
            "now_mc": now_mc, "now_pc_h1": now_pc_h1,
            "verdict": verdict,
            "optimize": [o[0] for o in optimize],
        }
        with open(review_file, "a") as f:
            f.write(_json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.debug(f"[Review] 记录保存失败: {e}")
