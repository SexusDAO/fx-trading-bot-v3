"""
robinhood_sniper/main.py
主入口 — 整合六层工作流，统一调度循环
"""

import asyncio
import logging
import time
import sys
import os
from typing import Optional, Callable

# 把 workspace 加入 path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from robinhood_sniper.config import (POLL_INTERVAL_SEC, POLL_INTERVAL_HOT_SEC, MONITOR_INTERVAL_SEC,
    MAX_TOKEN_AGE_MIN, MIN_SCORE, MAX_BUY_PER_ROUND, MAX_ENTRY_MC_USD,
    PROBE_ENABLED, PROBE_USD, PROBE_MAX_AGE_MIN, PROBE_MIN_LIQ_USD,
    NOTIFY_VIA_JARVIS, PROBE_MULTI_POOL_MAX_PRICE_RATIO,
    BTC_FUSE_ENABLED, BTC_FUSE_DROP_PCT, BTC_FUSE_COOLDOWN_SEC)
from robinhood_sniper.signal import fetch_new_tokens, fetch_new_tokens_rpc, fetch_pair_data, fetch_pair_age_min, mark_bought_24h, is_bought_24h, is_sl_cooldown
from robinhood_sniper.filter import score_token, score_token_full, security_gate, classify_token, momentum_gate, rejection_gate
from robinhood_sniper.clawby_rpc import check_creator_risk, get_latest_block
from robinhood_sniper.blacklist import (
    is_blacklisted, add_to_blacklist, blacklist_summary,
    is_scan_blacklisted, add_to_scan_blacklist,
)
from robinhood_sniper.trend_hunter import TrendHunter
from robinhood_sniper.executor import (
    get_eth_balance, calc_position, get_quote,
    buy_token, get_token_balance, setup_exit_orders, check_sell_quote,
    SELL_QUOTE_OK, SELL_QUOTE_DENY, SELL_QUOTE_RETRY,
)
from robinhood_sniper.monitor import PositionMonitor, Position

# ─────────────────────────────────────────────────────────────────────────────
# 注意：日志配置只在入口 start.py 做一次。
# main.py 作为被 import 的模块，只获取 logger，不重复添加 handler。
logger = logging.getLogger("sniper.main")


import subprocess as _subprocess

_JARVIS_TARGET = "432401404:thread:01a05e3a-924a-79ee-b124-9007af8994f4"

def notify(msg: str):
    """通知：写日志 + 打印 + 发送 Jarvis 消息"""
    print(f"\n🔔 {msg}\n")
    logger.info(f"[Notify] {msg}")
    if NOTIFY_VIA_JARVIS:
        try:
            _subprocess.Popen(
                ["openclaw", "message", "send",
                 "--channel", "jarvis",
                 "--target", _JARVIS_TARGET,
                 "--message", msg],
                stdout=_subprocess.DEVNULL,
                stderr=_subprocess.DEVNULL,
            )
        except Exception as _e:
            logger.debug(f"[Notify] Jarvis 发送失败: {_e}")


# 探针并发保护：正在买入中的地址集合，防止 RPC/DS 双通道同时触发
_probing_set: set = set()
_probing_lock = __import__('threading').Lock()

# process_token 并发保护：防止同一地址在第一笔 tx confirm 前被重复买入
_processing_set: set = set()
_processing_lock = __import__('threading').Lock()


def probe_token(profile: dict, monitor: PositionMonitor,
                notify: Callable) -> bool:
    """
    探针仓：发现新币立刻以 $5 占位，不等5分钟评分。
    只检查：年龄<3min + 流动性>$5k + 蜜罐检测 + 黑名单。
    后续 process_token 正常跑完整流程；探针仓已在则合并仓位。
    """
    if not PROBE_ENABLED:
        return False

    addr  = profile["address"].lower()
    short = addr[:10] + "..."

    # ── 并发保护：买入进行中 → 直接跳过（防止双通道重复触发）────────────
    with _probing_lock:
        if addr in _probing_set:
            return False
        _probing_set.add(addr)

    try:
        return _probe_token_inner(profile, monitor, notify, addr, short)
    finally:
        with _probing_lock:
            _probing_set.discard(addr)


def _probe_token_inner(profile: dict, monitor: PositionMonitor,
                       notify: Callable, addr: str, short: str) -> bool:
    """probe_token 的真实执行体（已持锁保护）"""
    # 已有持仓（含探针）→ 不重复
    if get_token_balance(addr) > 0:
        return False

    # RPC 发现 → 链上直读（probe 也走链上，实现秒级响应）
    pair = None
    pair_addr_hint = profile.get("pair_addr", "")
    if pair_addr_hint and profile.get("source") == "rpc_pair_created":
        try:
            from robinhood_sniper.on_chain_data import fetch_pool_data
            pair = fetch_pool_data(pair_addr_hint, addr)
            if pair:
                pair["pairCreatedAt"] = int(time.time() * 1000)
        except Exception:
            pair = None
    if not pair:
        pair = fetch_pair_data(addr)
    if not pair:
        return False

    age_min = fetch_pair_age_min(pair)
    if age_min > PROBE_MAX_AGE_MIN:
        return False

    liq = float((pair.get("liquidity") or {}).get("usd", 0) or 0)
    if liq < PROBE_MIN_LIQ_USD:
        return False

    mc = float(pair.get("marketCap") or pair.get("fdv") or 0)
    if mc > MAX_ENTRY_MC_USD:
        return False

    symbol  = (pair.get("baseToken") or {}).get("symbol", addr[:8])
    creator = (pair.get("baseToken") or {}).get("address", addr)

    if is_blacklisted(creator):
        return False

    # ── 拒绝门：和 process_token 共用同一套规则，先过再买探针 ──────────────
    from robinhood_sniper.filter import rejection_gate, score_token_full
    rej_ok, rej_msg = rejection_gate(pair, symbol)
    if not rej_ok:
        logger.info(f"[Probe] {symbol} 拒绝门拦截，不建探针: {rej_msg[:60]}")
        add_to_scan_blacklist(addr, symbol, rej_msg[:50])
        return False

    # 扫描黑名单
    if is_scan_blacklisted(addr):
        return False

    # 快速评分（最低 MIN_SCORE-2 才建探针，给评分一点宽容）
    score, _ = score_token_full(pair, {}, age_min, symbol=symbol)
    if score < max(MIN_SCORE - 2, 3):
        logger.info(f"[Probe] {symbol} 评分{score}不足，不建探针")
        return False

    # ── 多池陷阱检测：同一代币有多个 pair 且价差 > 5x → 跳过探针 ─────────────
    # GBC 案例：项目方用废弃小池诱导买入，主力在另一个 pair 拉盘
    # 探针 $5 不值得冒这个险，直接跳过等主力池稳定后再走正式评分流程
    try:
        from robinhood_sniper.signal import _clawby
        _raw = _clawby("dexscreener_token_pairs", {"chainId": "robinhood", "tokenAddresses": addr})
        _all_pairs = [p for p in (_raw or {}).get("pairs", []) if p.get("chainId") == "robinhood"]
        if len(_all_pairs) >= 2:
            _prices = [float(p.get("priceUsd") or 0) for p in _all_pairs if float(p.get("priceUsd") or 0) > 0]
            if len(_prices) >= 2:
                _ratio = max(_prices) / min(_prices)
                if _ratio > PROBE_MULTI_POOL_MAX_PRICE_RATIO:
                    logger.info(f"[Probe] {symbol} 多池陷阱: {len(_all_pairs)}个pair价差{_ratio:.1f}x>5x，跳过探针")
                    return False
    except Exception:
        pass

    sell_ok, _ = check_sell_quote(addr)
    if not sell_ok:
        return False

    eth_bal   = get_eth_balance()
    eth_price = 2420.0
    probe_eth = PROBE_USD / eth_price
    if eth_bal < probe_eth * 1.2:
        return False

    quote = get_quote(addr, probe_eth)
    if not quote:
        return False
    token_qty = float(quote.get("toCoinAmount") or 0)
    if token_qty <= 0:
        return False

    logger.info(f"[Probe] {symbol} 探针买入 ${PROBE_USD} age={age_min:.1f}min MC=${mc/1000:.0f}k")
    ok, data = buy_token(addr, probe_eth)
    if not ok:
        logger.warning(f"[Probe] {symbol} 探针买入失败: {data}")
        return False

    real_qty    = get_token_balance(addr)
    entry_price = (probe_eth * eth_price) / real_qty if real_qty > 0 else 0
    token_type  = classify_token(pair, profile, age_min)

    pos = Position(
        token_address   = addr,
        token_symbol    = symbol,
        entry_price_usd = entry_price,
        entry_qty       = real_qty,
        entry_liq_usd   = liq,
        token_type      = token_type,
        creator_address = creator,
    )
    monitor.add(pos)
    notify(f"🔍 [探针] {symbol} $5占位 age={age_min:.1f}min MC=${mc/1000:.0f}k | 等待完整评分加仓")
    logger.info(f"[Probe] {symbol} 探针仓建立 {real_qty:.4f}枚 entry=${entry_price:.8f}")
    return True


def process_token(profile: dict, monitor: PositionMonitor) -> bool:
    """
    处理单个新代币：筛选 → 风控 → 执行 → 监控注册
    返回 True 表示成功买入
    """
    addr = profile["address"].lower()
    short = addr[:10] + "..."

    # ── 并发保护：tx pending 期间防止第二次扫描重复买入（同 probe_token 的锁机制）────
    with _processing_lock:
        if addr in _processing_set:
            logger.info(f"[Flow] {short} 买入进行中，跳过重复触发")
            return False
        _processing_set.add(addr)

    try:
        return _process_token_inner(profile, monitor, addr, short)
    finally:
        with _processing_lock:
            _processing_set.discard(addr)


def _process_token_inner(profile: dict, monitor: PositionMonitor,
                         addr: str, short: str) -> bool:
    """process_token 的真实执行体（已持锁保护）"""
    # ── 0. 已持仓检查：探针仓继续加仓，其他重复直接跳过 ─────────────────────
    logger.info(f"[Flow] ── 处理 {short} ──")

    # 扫描黑名单快速过滤（24h 内已确认不买的地址）
    if is_scan_blacklisted(addr):
        logger.info(f"[Flow] {short} 在扫描黑名单内，24h 内跳过")
        return False

    existing_qty = get_token_balance(addr)
    has_probe = existing_qty > 0 and any(
        p.token_address.lower() == addr.lower() and p.remaining_qty < 20
        for p in monitor.positions
    )
    if existing_qty > 0 and not has_probe:
        logger.info(f"[Flow] {short} 已有正式持仓 {existing_qty:.4f}，跳过")
        return False
    is_add_on = has_probe  # 探针仓加仓标记

    # ── 1. 拉取交易对数据 ─────────────────────────────────────────────────────
    # RPC 发现的代币：pair_addr 已知 → 直接读链上数据，跳过 DS 15~25min 等待
    pair = None
    pair_addr_hint = profile.get("pair_addr", "")
    if pair_addr_hint and profile.get("source") == "rpc_pair_created":
        try:
            from robinhood_sniper.on_chain_data import fetch_pool_data
            pair = fetch_pool_data(pair_addr_hint, addr)  # 同步调用，timeout=5s 内完成
            if pair:
                pair["pairCreatedAt"] = int(time.time() * 1000)
                logger.info(f"[Flow] {short} ✅ 链上直读: "
                            f"liq=${float((pair.get('liquidity') or {}).get('usd',0)):,.0f} "
                            f"MC=${float(pair.get('marketCap',0)):,.0f}")
        except Exception as _oe:
            logger.debug(f"[Flow] {short} 链上读取失败({_oe})，回退 DS")
            pair = None
    if not pair:
        pair = fetch_pair_data(addr)
    if not pair:
        logger.info(f"[Flow] {short} 无交易对数据，跳过")
        return False

    age_min = fetch_pair_age_min(pair)
    if age_min > MAX_TOKEN_AGE_MIN:
        logger.info(f"[Flow] {short} 代币已 {age_min:.1f}min，太老跳过")
        add_to_scan_blacklist(addr, short, f"太老 {age_min:.0f}min")
        return False

    # ── MC 上限过滤：MC 已超过入场阈值则跳过（防止追高进场后立即触发止盈）
    fdv_check = float(pair.get("fdv") or 0)
    mc_check  = float(pair.get("marketCap") or fdv_check)
    if mc_check > MAX_ENTRY_MC_USD:
        logger.info(f"[Flow] {short} MC 已达 ${mc_check/1000:.0f}k > 上限 ${MAX_ENTRY_MC_USD/1000:.0f}k，跳过")
        add_to_scan_blacklist(addr, short, f"MC超上限 ${mc_check/1000:.0f}k")
        return False

    symbol = (pair.get("baseToken") or {}).get("symbol", addr[:8])

    # ── 1b. 同名代币过滤：只买流动性/成交量最强的那个合约 ────────────────────
    # 防止同名蜜罐/劣质 fork 绕过 _seen（不同地址但相同 symbol）
    try:
        from robinhood_sniper.signal import _clawby
        same_name = _clawby("dexscreener_token_profiles", {"query": symbol}) or {}
        rival_pairs = same_name.get("pairs") or []
        cur_liq  = float((pair.get("liquidity") or {}).get("usd", 0) or 0)
        cur_vol  = float((pair.get("volume") or {}).get("h24", 0) or 0)
        for rp in rival_pairs:
            rp_addr = ((rp.get("baseToken") or {}).get("address") or "").lower()
            if rp_addr == addr.lower():
                continue  # 就是自己
            rp_liq = float((rp.get("liquidity") or {}).get("usd", 0) or 0)
            rp_vol = float((rp.get("volume") or {}).get("h24", 0) or 0)
            # 存在另一个同名代币流动性或成交量明显更强 → 当前是劣势版，跳过
            if rp_liq > cur_liq * 2 or rp_vol > cur_vol * 2:
                logger.info(
                    f"[Flow] {symbol} 存在同名更强合约 {rp_addr[:10]}... "
                    f"(liq ${rp_liq:.0f} vs ${cur_liq:.0f})，跳过当前"
                )
                add_to_scan_blacklist(addr, symbol, f"同名劣势合约")
                return False
    except Exception as e:
        logger.debug(f"[Flow] {symbol} 同名过滤查询失败(非致命): {e}")

    # ── 2. 拒绝门：庄家操控信号一票否决（在评分之前）────────────────────
    rej_ok, rej_msg = rejection_gate(pair, symbol)
    logger.info(f"[Filter] {symbol} 拒绝门: {rej_msg}")
    if not rej_ok:
        logger.info(f"[Filter] {symbol} 拒绝门拦截，加入扫描黑名单")
        add_to_scan_blacklist(addr, symbol, rej_msg[:60])
        return False

    # ── 3. AI 打分筛选 ─────────────────────────────────────────────────────────
    score, reasons = score_token_full(pair, profile, age_min, symbol=symbol)
    logger.info(f"[Filter] {symbol} 评分={score}/{12} | " + " | ".join(reasons))

    # 链上早期模式（age<5min）用宽松门槛，DS 路径用标准 MIN_SCORE
    from robinhood_sniper.config import ONCHAIN_EARLY_AGE_MIN, ONCHAIN_EARLY_MIN_SCORE
    effective_min = (
        ONCHAIN_EARLY_MIN_SCORE
        if pair.get("_onchain") and age_min < ONCHAIN_EARLY_AGE_MIN
        else MIN_SCORE
    )
    if score < effective_min:
        logger.info(f"[Filter] {symbol} 分数不足({score}<{effective_min})，加入扫描黑名单")
        add_to_scan_blacklist(addr, symbol, f"评分不足 {score}/{effective_min}")
        return False

    # ── 3b. 价格动量方向过滤（拒绝在下跌中/卖压主导时买入）──────────────────
    mom_ok, mom_msg = momentum_gate(pair, symbol)
    logger.info(f"[Filter] {symbol} 动量检测: {mom_msg}")
    if not mom_ok:
        logger.info(f"[Filter] {symbol} 动量过滤拒绝，不拉黑（下次扫描可能已恢复）")
        # 动量不平不拉黑，只是本次不买，下轮扫描可能就得到正常了
        return False

    # ── 3. 安全风控（币安 Audit API）────────────────────────────────────────
    passed, audit_msg = security_gate(addr)
    logger.info(f"[Security] {symbol} {audit_msg}")
    if not passed:
        notify(f"🚫 [{symbol}] 安全风控未通过: {audit_msg}")
        add_to_scan_blacklist(addr, symbol, f"安全风控未通过: {audit_msg[:40]}")
        return False

    # ── 3b-0. 黑名单检查（本地快速过滤）─────────────────────────────────
    creator = (pair.get("baseToken") or {}).get("address", addr)
    if is_blacklisted(creator):
        logger.info(f"[Security] {symbol} 创建者 {creator[:12]}... 在黑名单，直接拒绝")
        add_to_scan_blacklist(addr, symbol, "创建者在黑名单")
        return False

    # ── 3b. 创建者钱包风险检测（Clawby address_labels）────────────────────
    creator_safe, creator_msg = check_creator_risk(creator)
    logger.info(f"[Security] {symbol} 创建者: {creator_msg}")
    if not creator_safe:
        notify(f"🚫 [{symbol}] 创建者钱包高危: {creator_msg}")
        add_to_scan_blacklist(addr, symbol, f"创建者高危: {creator_msg[:40]}")
        return False

    # ── 3c-0. 24h 全局买入去重 ─────────────────────────────────────────────
    if is_bought_24h(addr):
        logger.info(f"[Flow] {symbol} 24h 内已买过，跳过")
        return False

    # 止损冷却检查：止损过的24h内不重复买入（防止 people 式反复摂入）
    if is_sl_cooldown(addr):
        logger.info(f"[Flow] {symbol} 在止损冷却期，24h 内不再买入")
        return False

    # ── 3c. 模拟卖出检测（防貔貅/burn合约）──────────────────────────────────
    # 区分三种结果：
    #   SELL_QUOTE_OK    → 直接买入
    #   SELL_QUOTE_RETRY → 暂时无流动性（新池子刚上线），等 60s 后重试一次
    #   SELL_QUOTE_DENY  → 确认蜜罐/合约拒绝，加入扫描黑名单
    sell_status, sell_msg = check_sell_quote(addr)
    logger.info(f"[Security] {symbol} 模拟卖出: {sell_msg}")

    if sell_status == SELL_QUOTE_RETRY:
        logger.info(f"[Security] {symbol} 暂时无流动性，60s 后重试一次...")
        notify(f"⏳ [{symbol}] 评分{score}分通过，池子流动性未就绪，等60s重试")
        import time as _t; _t.sleep(60)
        sell_status, sell_msg = check_sell_quote(addr)
        logger.info(f"[Security] {symbol} 重试结果: {sell_msg}")
        if sell_status != SELL_QUOTE_OK:
            logger.info(f"[Security] {symbol} 重试仍失败，跳过本次扫描（下轮再看）")
            notify(f"⏭️ [{symbol}] 重试后流动性仍未就绪，本次跳过")
            # 注意：retry 情况不拉黑，下次扫描到时会重新检测
            return False

    if sell_status == SELL_QUOTE_DENY:
        notify(f"🍯 [{symbol}] 疑似蜜罐！模拟卖出失败: {sell_msg}")
        add_to_scan_blacklist(addr, symbol, f"蜜罐: {sell_msg[:40]}")
        return False

    # ── 4. 仓位计算 + 执行买入 ──────────────────────────────────────────────────
    eth_balance = get_eth_balance()
    if eth_balance < 0.001:
        logger.warning(f"[Exec] ETH 余额不足 ({eth_balance:.6f})，暂停")
        notify(f"⚠️ ETH 余额不足 {eth_balance:.6f}，跳过 {symbol}")
        return False

    position_eth = calc_position(score, eth_balance)
    position_usd  = position_eth * 2400  # 粗略估算 USD
    logger.info(f"[Exec] {symbol} 计划买入 {position_eth:.6f} ETH "
                f"(余额={eth_balance:.4f}, 评分={score}, 估算约${position_usd:.1f})")

    # ── 最低买入金额过滤：< $8 不值得花 gas ─────────────────────────────
    MIN_BUY_USD = 8.0  # gas 单笔约$0.55，最少需要买入$8才有意义（gas占比<7%）
    if position_usd < MIN_BUY_USD:
        logger.info(f"[Exec] {symbol} 估算仓位${position_usd:.1f} < 最低${MIN_BUY_USD}，跳过"
                    f"（gas成本占比过高，不划算）")
        return False

    # 报价确认
    quote = get_quote(addr, position_eth)
    if quote:
        logger.info(f"[Exec] 报价: {position_eth} ETH → {quote.get('toCoinAmount','?')} {symbol}")

    # 自动分类 token_type（platform > narrative > normal）
    token_type = classify_token(pair, profile, age_min=age_min)
    logger.info(f"[Flow] {symbol} 自动分类: {token_type} (age={age_min:.1f}min)")

    # 判断是否热门代币
    m5_buys = (pair.get("txns") or {}).get("m5", {}).get("buys", 0)
    is_hot  = m5_buys >= 20

    # 执行买入
    success, tx_data = buy_token(addr, position_eth, is_hot=is_hot)
    if not success:
        notify(f"❌ [{symbol}] 买入失败")
        return False

    # ── 5. 获取买入后持仓 & 挂止盈止损 ──────────────────────────────────────────
    time.sleep(5)  # 等链上确认
    token_qty = get_token_balance(addr)
    entry_price = float(pair.get("priceUsd") or 0)
    entry_liq   = float((pair.get("liquidity") or {}).get("usd", 0) or 0)

    notify(
        f"✅ [{symbol}] 买入成功!\n"
        f"  地址: {addr}\n"
        f"  金额: {position_eth:.6f} ETH | 数量: {token_qty:.4f}\n"
        f"  入场价: ${entry_price:.6f} | 流动性: ${entry_liq:,.0f}\n"
        f"  评分: {score}/12 | {audit_msg}"
    )

    # 挂止盈止损单，记录止损单 orderId（partial_sell 后用于取消旧单）
    sl_ids = []
    if token_qty > 0 and entry_price > 0:
        orders = setup_exit_orders(addr, token_qty, entry_price)
        tp_count = sum(1 for o in orders if o["placed"] and o["type"] == "take_profit")
        sl_count = sum(1 for o in orders if o["placed"] and o["type"] == "stop_loss")
        sl_ids   = [o["order_id"] for o in orders
                    if o["placed"] and o["type"] == "stop_loss" and o.get("order_id")]
        logger.info(f"[Exec] {symbol} 已挂 {tp_count}个止盈单 + {sl_count}个止损单")

    # ── 6. 注册到监控器 ──────────────────────────────────────────────────────────
    pos = Position(
        token_address=addr,
        token_symbol=symbol,
        entry_price_usd=entry_price,
        entry_qty=token_qty,
        entry_liq_usd=entry_liq,
        token_type=token_type,
        sl_order_ids=sl_ids,
        creator_address=creator,
    )
    mark_bought_24h(addr)  # 24h 去重记录
    monitor.add(pos)
    return True



async def signal_loop(monitor: PositionMonitor, buy_limit: int = 0):
    """
    双通道信号捕捉：
    - 主通道：直连 RPC 监听 PairCreated（零延迟，每 5s 轮询新区块）
    - 补充通道：DexScreener token_profiles（15s 轮询，兜底发现）
    buy_limit: 最多买入 N 个代币（0=无限制，1=测试模式买一个）
    """
    logger.info(f"[Signal] 启动双通道 | RPC 5s + DexScreener {POLL_INTERVAL_SEC}s "
                f"| buy_limit={buy_limit or '无限制'}")
    hot_mode_until    = 0.0
    ds_last_poll      = 0.0
    btc_last_poll     = 0.0
    btc_fuse_until    = 0.0   # 熔断结束时间戳
    btc_prev_price    = 0.0
    bought_count      = 0

    while True:
        now = time.time()
        try:
            # ── BTC 宏观熔断检测（每 30s 查一次，BTC 急跌 >3% 暂停买入 10min）────
            if BTC_FUSE_ENABLED and now - btc_last_poll >= 30:
                btc_last_poll = now
                try:
                    btc_pair = fetch_pair_data("0x2260fac5e5542a773aa44fbcfedf7c193bc2c599")  # WBTC as proxy
                    # 用 binance public API 更准确
                    import requests as _req
                    _proxies = {"http": "socks5://REDACTED_PROXY_CREDENTIALS",
                                "https": "socks5://REDACTED_PROXY_CREDENTIALS"}
                    _r = _req.get("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT",
                                  timeout=5, proxies=_proxies)
                    btc_price = float(_r.json().get("price", 0))
                    if btc_prev_price > 0 and btc_price > 0:
                        btc_chg = (btc_price - btc_prev_price) / btc_prev_price
                        if btc_chg <= BTC_FUSE_DROP_PCT:
                            btc_fuse_until = now + BTC_FUSE_COOLDOWN_SEC
                            msg = (f"🔴 BTC 急跌 {btc_chg:.1%}，触发宏观熔断，"
                                   f"{BTC_FUSE_COOLDOWN_SEC//60}min 内暂停新仓开立")
                            logger.warning(f"[Signal] {msg}")
                            notify(msg)
                    if btc_price > 0:
                        btc_prev_price = btc_price
                except Exception as _be:
                    logger.debug(f"[Signal] BTC 熔断检测失败: {_be}")

            # ── 主通道：RPC PairCreated ──────────────────────────────────
            rpc_tokens = fetch_new_tokens_rpc()
            if rpc_tokens:
                logger.info(f"[RPC] 本轮发现 {len(rpc_tokens)} 个新交易对")
                for profile in rpc_tokens:
                    if buy_limit and bought_count >= buy_limit:
                        break
                    # ── BTC 熔断：跳过新仓开立，但仍允许探针（探针$5风险极小）
                    if BTC_FUSE_ENABLED and now < btc_fuse_until:
                        logger.info(f"[Signal] BTC熔断中，跳过买入 {profile.get('address','')[:10]}...")
                        continue
                    # 探针仓：先用 $5 占位，不等完整评分
                    probe_token(profile, monitor, notify)
                    bought = process_token(profile, monitor)
                    if bought:
                        bought_count += 1
                        hot_mode_until = now + 120
                        if buy_limit and bought_count >= buy_limit:
                            logger.info(f"[Signal] 已达买入上限 {buy_limit}，停止信号扫描")
                            return

            # ── 补充通道：DexScreener（降频轮询）────────────────────────
            if now - ds_last_poll >= POLL_INTERVAL_SEC:
                ds_last_poll = now
                ds_tokens = fetch_new_tokens()
                if ds_tokens:
                    logger.info(f"[DexScreener] 本轮发现 {len(ds_tokens)} 个新代币")
                    round_bought = 0   # 本轮买入计数，上限 MAX_BUY_PER_ROUND
                    for profile in ds_tokens:
                        if buy_limit and bought_count >= buy_limit:
                            break
                        if round_bought >= MAX_BUY_PER_ROUND:
                            logger.info(f"[DexScreener] 本轮买入已达上限 {MAX_BUY_PER_ROUND}，等待下轮")
                            break
                        # ── BTC 熔断：跳过新仓开立
                        if BTC_FUSE_ENABLED and now < btc_fuse_until:
                            logger.info(f"[Signal] BTC熔断中，跳过买入 {profile.get('address','')[:10]}...")
                            continue
                        # 探针仓：先用 $5 占位
                        probe_token(profile, monitor, notify)
                        bought = process_token(profile, monitor)
                        if bought:
                            bought_count += 1
                            round_bought += 1
                            hot_mode_until = now + 120
                            if buy_limit and bought_count >= buy_limit:
                                logger.info(f"[Signal] 已达买入上限 {buy_limit}，停止信号扫描")
                                return

        except Exception as e:
            logger.error(f"[Signal] 异常: {e}", exc_info=True)

        interval = POLL_INTERVAL_HOT_SEC if time.time() < hot_mode_until else 5
        await asyncio.sleep(interval)


async def monitor_loop(monitor: PositionMonitor):
    """持仓监控主循环"""
    logger.info(f"[Monitor] 监控层启动，间隔 {MONITOR_INTERVAL_SEC}s")
    while True:
        try:
            monitor.run_once(notify_fn=notify)
        except Exception as e:
            logger.error(f"[Monitor] 异常: {e}", exc_info=True)
        await asyncio.sleep(MONITOR_INTERVAL_SEC)


async def trend_loop(hunter: TrendHunter):
    """推特热度追踪循环，每 30s tick 一次"""
    logger.info("[Trend] 热度追踪模块启动")
    while True:
        try:
            hunter.tick()
        except Exception as e:
            logger.error(f"[Trend] tick 异常: {e}", exc_info=True)
        await asyncio.sleep(30)


async def status_loop(monitor: PositionMonitor):
    """每 5 分钟打印一次持仓汇总"""
    while True:
        await asyncio.sleep(300)
        summary = monitor.summary()
        logger.info(summary)
        print(summary)


async def main():
    logger.info("=" * 60)
    logger.info("🤖 Robinhood Chain AI 打狗机器人启动")
    logger.info("=" * 60)

    # 启动前检查
    eth_balance = get_eth_balance()
    logger.info(f"💰 Agent 钱包 ETH 余额: {eth_balance:.6f}")
    if eth_balance < 0.001:
        logger.warning("⚠️ 余额极低，信号层将持续运行但实际买入会跳过")

    monitor = PositionMonitor()

    # buy_limit=1 → 只买一个代币（测试模式），改为 0 恢复正常运行
    buy_limit = int(sys.argv[1]) if len(sys.argv) > 1 else 0

    trend_hunter = TrendHunter(monitor, notify_fn=notify)

    await asyncio.gather(
        signal_loop(monitor, buy_limit=buy_limit),
        monitor_loop(monitor),
        status_loop(monitor),
        trend_loop(trend_hunter),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 用户中断，程序退出")
