"""
Arc Sniper — 主入口

主循环流程:
    1. 初始化 Web3 连接、Scanner、Executor
    2. 每 2 秒扫描 DEX 工厂合约
    3. 发现新代币 → 拉取链上数据 → 评分 → 买入 → 通知
    4. 频率控制：30s 间隔 + 每小时 10 笔上限

运行方式:
    export ARC_PRIVATE_KEY="你的私钥"
    export ARC_JARVIS_TARGET="userId:thread:threadId"
    python -m arc_sniper.main

    或:
    python -m arc_sniper.main --rpc https://rpc.arc-scan.org --amount 20
"""

import argparse
import logging
import time
import sys
import os
import threading

# 确保父目录在 path 中（python -m 时自动有，python 直接运行时可能缺）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web3 import Web3
from arc_sniper import config
from arc_sniper.scanner import Scanner
from arc_sniper.onchain import (
    get_token_info,
    get_holder_stats,
    check_honeypot,
    get_pair_liquidity,
    get_token_age_sec,
)
from arc_sniper.signal_aggregator import score_token_multi_dim, quick_prescreen
from arc_sniper.executor import Executor
from arc_sniper.notify import notify_buy, notify_skip
from arc_sniper.grok_monitor import GrokMonitor
from arc_sniper.nft_minter import NFTMinter
from arc_sniper.position_manager import PositionManager, notify_buy as pm_notify_buy, notify_sell as pm_notify_sell
from arc_sniper.signal_log import SignalLog

# ═══════════════════════════════════════════════════════════════
# 模拟模式状态
# ═══════════════════════════════════════════════════════════════
_simulate_mode = False     # True = 余额不足，只记录信号不买入
_signal_log: SignalLog = None  # 延迟初始化

def _init_signal_log():
    global _signal_log
    if _signal_log is None:
        _signal_log = SignalLog()
    return _signal_log

# ═══════════════════════════════════════════════════════════════
# 日志配置
# ═══════════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
    handlers=[
        logging.FileHandler(config.LOG_FILE, encoding="utf-8"),
    ],
)
logger = logging.getLogger("arc.main")


# ═══════════════════════════════════════════════════════════════
# 频率控制
# ═══════════════════════════════════════════════════════════════
_last_buy_time = 0.0
_buy_history: list[float] = []  # 每小时买入时间戳列表


def _can_buy() -> tuple[bool, str]:
    """检查频率限制"""
    global _last_buy_time, _buy_history

    now = time.time()

    # 间隔限制
    if now - _last_buy_time < config.MIN_BUY_INTERVAL_SEC:
        wait = config.MIN_BUY_INTERVAL_SEC - (now - _last_buy_time)
        return False, f"频率限制：还需等待 {wait:.0f}s"

    # 清理 1 小时前的记录
    _buy_history = [t for t in _buy_history if now - t < 3600]

    # 每小时上限
    if len(_buy_history) >= config.MAX_BUYS_PER_HOUR:
        return False, f"小时限额：已买 {_buy_history}/{config.MAX_BUYS_PER_HOUR} 笔"

    return True, ""


def _record_buy():
    """记录一次买入"""
    global _last_buy_time, _buy_history
    _last_buy_time = time.time()
    _buy_history.append(_last_buy_time)


# ═══════════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════════

def process_new_token(w3, scanner, executor, token: dict) -> bool:
    """
    处理一个新发现的代币。

    统一管道: 数据 → 快速预检 → 拒绝门 → 多维评分 → 聪明钱叠加 → 买入 → 通知
    返回: 是否执行了买入
    """
    token_addr = token["token_address"]
    dex_type   = "dag"  # 统一走 DAG Router
    block_num  = token["block_number"]
    pair_addr  = token.get("pair_address", "")

    # 1. 获取代币基本信息
    token_info = get_token_info(w3, token_addr)
    if not token_info:
        logger.warning(f"[Main] 无法获取代币信息: {token_addr[:10]}...")
        return False
    token_info["address"] = token_addr
    token_info["pair_address"] = pair_addr
    token_info["dex_type"] = "dag"

    symbol = token_info.get("symbol", "?")
    name   = token_info.get("name", "?")
    logger.info(f"[Main] 📋 处理 {name} (${symbol})")

    # 2. 获取持有人数据
    holder_stats = get_holder_stats(w3, token_addr, from_block=block_num)
    token_info["holder_count"] = holder_stats.get("count", 0)
    token_info["top_10_holder_rate"] = holder_stats.get("top10_pct", 1.0)
    logger.info(
        f"[Main] 持有人: {holder_stats['count']}人, "
        f"Top10={holder_stats['top10_pct']:.0%}"
    )

    # 3. 获取流动性
    liquidity = 0.0
    if pair_addr:
        liquidity = get_pair_liquidity(w3, pair_addr, token_addr)
    token_info["liquidity_usd"] = liquidity
    logger.info(f"[Main] 流动性: ${liquidity:,.0f}")

    # 4. 代币年龄
    age_sec = get_token_age_sec(w3, block_num)
    token_info["creation_timestamp"] = int(time.time()) - age_sec
    logger.info(f"[Main] 代币年龄: {age_sec/60:.1f}min")

    # 5. 蜜罐检测
    if config.HONEYPOT_CHECK:
        honeypot_safe, honeypot_reason = check_honeypot(w3, token_addr, pair_addr)
        token_info["is_honeypot"] = 0 if honeypot_safe else 1
        logger.info(f"[Main] 蜜罐检测: {honeypot_reason}")
    else:
        token_info["is_honeypot"] = 0

    # 6. 获取链上 pair 数据（用于 filter.rejection_gate）
    if pair_addr:
        try:
            from arc_sniper.onchain import fetch_pool_data
            pair_data = fetch_pool_data(pair_addr, token_addr)
            if pair_data:
                token_info["_pair_data"] = pair_data
        except Exception:
            pass

    # 7. 快速预检
    passed, prescreen_msg = quick_prescreen(token_info)
    if not passed:
        logger.info(f"[Main] 预检淘汰 {symbol}: {prescreen_msg}")
        # 如果是因为流动性不足，加入 watchlist 等5分钟后复查
        if "流动性" in prescreen_msg:
            try:
                from arc_sniper.watchlist import add_token
                add_token(token_info)
            except Exception:
                pass
        notify_skip(token_info, 0, prescreen_msg, "预检未通过")
        return False

    # 8. 统一评分（安全门 + 拒绝门 + 六维评分 + 聪明钱）
    result = score_token_multi_dim(token_info, signal_source="onchain")
    score = result["score"]
    details_str = " | ".join(
        f"{k}={v}" for k, v in result["details"].items()
        if k not in ("blocked", "block_reason")
    )
    logger.info(f"[Main] 评分: {score}/100 — {details_str}")

    if not result["should_buy"]:
        notify_skip(token_info, score, details_str, result["reason"])
        return False

    # 9. 频率检查（模拟模式也记录频率）
    can, reason = _can_buy()
    if not can:
        logger.info(f"[Main] 跳过: {reason}")
        return False

    # 10. 动态仓位计算
    usdc_balance = executor.get_usdc_balance() if executor else 0.0
    buy_amount = PositionManager.calc_buy_amount(usdc_balance) if usdc_balance > 0 else 5.0  # 模拟默认 $5

    if _simulate_mode or usdc_balance < 1.0:
        # ── 模拟模式：记录信号不买入 ──
        logger.info(f"[Main] 📝 [SIM] 模拟买入 {name} (${symbol}) — score={score}/100 仓位=${buy_amount:.2f}")

        # 估算模拟买入价
        sim_price = 0.0
        sim_tokens = 0.0
        # 方法1: 通过 simulate swap 获取价格
        try:
            from arc_sniper.dag_swap import DagSwap
            swap = DagSwap(config.RPC_URL, config.PRIVATE_KEY)
            pool_path = swap.find_pool_path(token_addr)
            if pool_path:
                sim = swap.simulate(token_addr, 0.01, pool_path)
                if sim.get("success") and sim.get("expected_output", 0) > 0:
                    sim_tokens = sim["expected_output"] * (buy_amount / 0.01)
                    sim_price = buy_amount / sim_tokens if sim_tokens > 0 else 0
        except Exception:
            pass

        # 方法2: 用 total_supply + mcap 估算
        if sim_price == 0:
            total_supply = token_info.get("total_supply", 0) or 0
            mcap_est = token_info.get("market_cap", 0) or token_info.get("mcap_usd", 0) or 0
            if total_supply > 0 and mcap_est > 0:
                sim_price = mcap_est / total_supply
                sim_tokens = buy_amount / sim_price if sim_price > 0 else 0
            elif total_supply > 0 and liquidity > 0:
                # 粗估: 假设池子里 token 占一半
                sim_price = (liquidity * 0.5) / (total_supply * 0.01)  # 粗估
                sim_tokens = buy_amount / sim_price if sim_price > 0 else 0

        # 方法3: DexScreener API
        if sim_price == 0:
            try:
                import requests as _req
                r = _req.get(f"https://api.dexscreener.com/latest/dex/tokens/{token_addr}", timeout=10)
                if r.status_code == 200:
                    pairs = r.json().get("pairs", [])
                    if pairs:
                        best = max(pairs, key=lambda p: (p.get("liquidity", {}) or {}).get("usd", 0))
                        ds_price = float(best.get("priceUsd", 0) or 0)
                        ds_mcap = float(best.get("fdv", 0) or 0)
                        if ds_price > 0:
                            sim_price = ds_price
                            sim_tokens = buy_amount / sim_price if sim_price > 0 else 0
                            if mcap == 0:
                                mcap = ds_mcap
                            logger.info(f"[Main] [SIM] DexScreener 价格: ${sim_price:.10f} mcap=${mcap:,.0f}")
            except Exception:
                pass

        mcap = token_info.get("market_cap", 0) or token_info.get("mcap_usd", 0)
        # 如果没有 mcap 数据，用 total_supply * sim_price 估算
        if mcap == 0 and sim_price > 0:
            total_supply = token_info.get("total_supply", 0) or 0
            if total_supply > 0:
                mcap = total_supply * sim_price
                logger.info(f"[Main] [SIM] 估算 mcap=${mcap:,.0f} (supply={total_supply:.0f} x price={sim_price:.10f})")

        # 记录到信号日志
        slog = _init_signal_log()
        slog.log_signal(
            token_address=token_addr,
            token_symbol=symbol,
            token_name=name,
            score=score,
            details=result.get("details", {}),
            should_buy=True,
            simulated_buy_amount=buy_amount,
            simulated_buy_price=sim_price,
            simulated_tokens=sim_tokens,
            mcap_usd=mcap,
            liquidity_usd=liquidity,
            holder_count=token_info.get("holder_count", 0),
            top10_pct=token_info.get("top_10_holder_rate", 0),
            block_number=block_num,
            pair_address=pair_addr,
            honeypot_safe=token_info.get("is_honeypot", 0) == 0,
            signal_source="onchain",
            reason=f"score_above_threshold (sim_mode={'auto' if not _simulate_mode else 'manual'})",
        )

        _record_buy()  # 记录频率
        logger.info(
            f"[Main] 📝 [SIM] 信号已记录: {symbol} score={score} "
            f"mcap=${mcap:,.0f} liq=${liquidity:,.0f} "
            f"holders={token_info.get('holder_count',0)} "
            f"sim_buy=${buy_amount:.2f} @ ${sim_price:.10f}"
        )

        # 创建模拟持仓，让 position_manager 追踪止盈
        if sim_price > 0 and sim_tokens > 0:
            try:
                pm_sim = PositionManager()
                pm_sim.add_position(
                    token_address=token_addr,
                    token_symbol=symbol,
                    tx_hash="simulated",
                    buy_price_usdc=sim_price,
                    buy_mcap_usd=mcap,
                    buy_liquidity_usd=liquidity,
                    amount_token=sim_tokens,
                    cost_usdc=buy_amount,
                    is_simulated=True,
                )
                logger.info(f"[Main] 📝 [SIM] 模拟持仓已创建: {symbol} @ ${sim_price:.10f}")
            except Exception as e:
                logger.warning(f"[Main] [SIM] 创建模拟持仓失败: {e}")
        else:
            logger.debug(f"[Main] [SIM] {symbol} 无价格数据，跳过模拟持仓创建")

        return True  # 信号已记录

    # 11. 真实买入
    logger.info(f"[Main] 🚀 买入 {name} (${symbol}) — ${buy_amount:.2f} USDC")
    buy_result = executor.buy_token(
        token_addr, buy_amount, "dag", pair_addr,
    )
    buy_result["dex_type"] = "dag"
    buy_result["buy_amount"] = buy_amount

    # 12. 通知 + 记录持仓
    if buy_result.get("success"):
        _record_buy()
        notify_buy(token_info, score, result.get("details", {}), buy_result)
        logger.info(f"[Main] ✅ 买入成功 tx={buy_result.get('tx_hash', '')[:18]}...")

        # 记录持仓到 position_manager
        token_received = buy_result.get("token_received", 0)
        buy_price = buy_amount / token_received if token_received > 0 else 0
        try:
            pm = PositionManager()
            pm.add_position(
                token_address=token_addr,
                token_symbol=symbol,
                tx_hash=buy_result.get("tx_hash", ""),
                buy_price_usdc=buy_price,
                buy_mcap_usd=token_info.get("market_cap", 0) or token_info.get("mcap_usd", 0),
                buy_liquidity_usd=liquidity,
                amount_token=token_received,
                cost_usdc=buy_amount,
            )
            pm_notify_buy(symbol, buy_price, token_received, buy_amount,
                          token_info.get("market_cap", 0) or token_info.get("mcap_usd", 0),
                          buy_result.get("tx_hash", ""))
        except Exception as e:
            logger.error(f"[Main] 持仓记录失败: {e}")
    else:
        logger.error(f"[Main] ❌ 买入失败: {buy_result.get('error')}")
        notify_buy(token_info, score, result.get("details", {}), buy_result)

    return buy_result.get("success", False)


def main():
    """主循环"""
    parser = argparse.ArgumentParser(description="Arc Sniper — Arc 链自动打新机器人")
    parser.add_argument("--rpc", default=None, help="Arc RPC URL")
    parser.add_argument("--amount", type=float, default=None, help="每笔买入金额 (USDC)")
    parser.add_argument("--dry-run", action="store_true", help="只扫描不买入")
    parser.add_argument("--simulate", action="store_true", help="模拟模式：信号通过时记录日志但不真实买入（余额不足时自动激活）")
    parser.add_argument("--grok", action="store_true", help="同时启动 Grok Twitter 监控")
    parser.add_argument("--grok-only", action="store_true", help="只启动 Grok 监控（不启动 DEX scanner）")
    args = parser.parse_args()

    # 覆盖配置
    if args.rpc:
        config.RPC_URL = args.rpc
        # 自动检测 testnet
        if 'testnet' in args.rpc.lower():
            config.CHAIN_ID = config.TESTNET_CHAIN_ID
            logger.info(f"[Main] 检测到 testnet RPC，Chain ID 切换为 {config.CHAIN_ID}")
    if args.amount:
        config.BUY_AMOUNT_USDC = args.amount

    logger.info("=" * 60)
    logger.info("🦞 Arc Sniper 启动")
    logger.info(f"  RPC: {config.RPC_URL}")
    logger.info(f"  Chain ID: {config.CHAIN_ID}")
    logger.info(f"  买入金额: ${config.BUY_AMOUNT_USDC}")
    logger.info(f"  最低评分: {config.MIN_SCORE_TO_BUY}/100")
    logger.info(f"  Dry Run: {args.dry_run}")
    logger.info(f"  Simulate: {args.simulate}")
    logger.info(f"  Grok Monitor: {args.grok or args.grok_only}")
    logger.info(f"  Grok Only: {args.grok_only}")
    logger.info("=" * 60)

    # 连接 RPC
    w3 = Web3(Web3.HTTPProvider(config.RPC_URL))
    try:
        connected = w3.is_connected()
    except Exception:
        connected = False

    if not connected:
        logger.error(f"[Main] ❌ 无法连接 RPC: {config.RPC_URL}")
        logger.info("[Main] 提示: Arc 主网 9/16 上线，上线前无法连接")
        logger.info("[Main] 可以先用 --rpc 指定测试网 URL 测试: python -m arc_sniper.main --rpc https://rpc.testnet.arc.io")
        sys.exit(1)

    logger.info(f"[Main] ✅ RPC 连接成功, 当前区块: {w3.eth.block_number}")

    # 初始化组件
    scanner = Scanner(w3)

    executor = None
    usdc_balance = 0.0
    if not args.dry_run:
        if not config.PRIVATE_KEY:
            logger.error("[Main] ❌ 未设置 ARC_PRIVATE_KEY 环境变量")
            logger.info("[Main] export ARC_PRIVATE_KEY='你的私钥'")
            sys.exit(1)
        executor = Executor(w3)
        usdc_balance = executor.get_usdc_balance()
        logger.info(f"[Main] 钱包 USDC 余额: ${usdc_balance:.2f}")
        if usdc_balance < 1.0:
            global _simulate_mode
            _simulate_mode = True
            logger.warning(f"[Main] ⚠️ 余额 ${usdc_balance:.2f} 不足 → 自动切换模拟模式")
            logger.info("[Main] 📝 模拟模式: 信号将记录到 signal_log.jsonl，不真实买入")
        elif args.simulate:
            _simulate_mode = True
            logger.info("[Main] 📝 手动模拟模式: 信号将记录到 signal_log.jsonl，不真实买入")
        else:
            if usdc_balance < config.BUY_AMOUNT_USDC:
                logger.warning(f"[Main] ⚠️ 余额不足: ${usdc_balance:.2f} < ${config.BUY_AMOUNT_USDC}")

    # ── Grok 监控（可选）──
    # 在独立线程中运行，不阻塞主循环
    grok_thread = None
    if args.grok or args.grok_only:
        nft_minter = None
        if not args.dry_run and config.PRIVATE_KEY:
            nft_minter = NFTMinter(w3, config.PRIVATE_KEY)
            logger.info("[Main] 🎨 NFT Minter 已就绪")
        grok_monitor = GrokMonitor(w3, executor, nft_minter)
        grok_thread = threading.Thread(target=grok_monitor.run_forever, daemon=True)
        grok_thread.start()
        logger.info("[Main] 🐦 Grok 监控已启动（独立线程，含 NFT 监控）")

    # ── Smart Money 聪明钱监听（可选）──
    # 定期扫描链上 Swap 事件，识别聪明钱共振
    # 不再需要独立线程，在主循环中调用即可
    signal_thread = None

    # ── Grok-only 模式：不启动 DEX scanner ──
    if args.grok_only:
        logger.info("[Main] Grok-only 模式，等待监控线程...")
        try:
            while (grok_thread and grok_thread.is_alive()) or \
                 (signal_thread and signal_thread.is_alive()):
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("[Main] 收到退出信号")
        return

    # ── DEX Scanner 主循环 ──
    logger.info("[Main] 开始扫描...")

    # 初始化持仓管理器
    pm = None
    if not args.dry_run and config.PRIVATE_KEY:
        pm = PositionManager()
        open_count = len(pm.get_open_positions())
        logger.info(f"[Main] 📊 持仓管理器已加载: {open_count} 个 open 持仓")

    last_pm_check = 0.0
    last_scan_debug = 0.0

    while True:
        try:
            # ── 持仓巡检 (每 60 秒) ──
            if pm and time.time() - last_pm_check >= 60:
                last_pm_check = time.time()
                pm_result = pm.check_all()
                if pm_result["sold"]:
                    for s in pm_result["sold"]:
                        logger.info(
                            f"[Main] 🎯 自动卖出: {s['symbol']} "
                            f"({s['reason']}) PnL=${s['pnl']:.2f}"
                        )
                        pm_notify_sell(
                            s["symbol"], s["reason"], s["price"], s["pnl"], ""
                        )
                if pm_result["errors"]:
                    for e in pm_result["errors"]:
                        logger.warning(f"[Main] 持仓巡检错误: {e}")

            # ── Watchlist 延迟复查 (每 60 秒) ──
            if time.time() - last_pm_check >= 60:
                try:
                    from arc_sniper.watchlist import get_ready_tokens
                    ready = get_ready_tokens()
                    if ready:
                        logger.info(f"[Main] 🔄 Watchlist 复查 {len(ready)} 个代币")
                        for entry in ready:
                            logger.info(f"[Main] 🔄 复查 {entry['symbol']} (初始 liq=${entry['initial_liquidity']:.0f})")
                            # 重新获取代币信息并评估
                            token_info_recheck = get_token_info(w3, entry['token_address'])
                            if token_info_recheck:
                                token_info_recheck['creation_timestamp'] = int(entry.get('added_at', time.time()) - 300)
                                token_info_recheck['creation_block'] = entry.get('block', 0)
                                process_token(w3, {**entry, 'token_address': entry['token_address']}, args, pm, executor, _simulate_mode)
                except Exception as e:
                    logger.debug(f"[Main] Watchlist 复查异常: {e}")

            # 扫描新代币
            new_tokens = scanner.scan()
            if new_tokens:
                logger.info(f"[Main] 发现 {len(new_tokens)} 个新代币")
            # 心跳: 每 120s 输出一次状态
            if time.time() - last_scan_debug >= 120:
                last_scan_debug = time.time()
                try:
                    cur_block = w3.eth.block_number
                except Exception:
                    cur_block = "?"
                lb = scanner.last_block if hasattr(scanner, 'last_block') else "?"
                logger.info(f"[Main] 心跳 block={cur_block} scanner_last={lb}")

            if new_tokens:
                for token in new_tokens:
                    try:
                        if args.dry_run:
                            # Dry run: 只拉数据评分，不买入
                            token_info = get_token_info(w3, token["token_address"])
                            if token_info:
                                token_info["address"] = token["token_address"]
                                holder_stats = get_holder_stats(
                                    w3, token["token_address"],
                                    from_block=token["block_number"],
                                )
                                token_info["holder_count"] = holder_stats.get("count", 0)
                                token_info["top_10_holder_rate"] = holder_stats.get("top10_pct", 1.0)
                                pair_addr = token.get("pair_address", "")
                                liquidity = get_pair_liquidity(w3, pair_addr, token["token_address"]) if pair_addr else 0
                                token_info["liquidity_usd"] = liquidity
                                age_sec = get_token_age_sec(w3, token["block_number"])
                                token_info["creation_timestamp"] = int(time.time()) - age_sec
                                hp_safe, hp_reason = check_honeypot(w3, token["token_address"], pair_addr)
                                token_info["is_honeypot"] = 0 if hp_safe else 1

                                result = score_token_multi_dim(token_info, signal_source="onchain")
                                logger.info(
                                    f"[Main] [DRY RUN] {token_info.get('name','?')} "
                                    f"(${token_info.get('symbol','?')}) "
                                    f"得分={result['score']}/100 "
                                    f"买入={result['should_buy']}"
                                )
                                # DRY RUN 模式也记录通过的信号
                                if result["should_buy"]:
                                    slog = _init_signal_log()
                                    mcap = token_info.get("market_cap", 0) or token_info.get("mcap_usd", 0)
                                    slog.log_signal(
                                        token_address=token["token_address"],
                                        token_symbol=token_info.get("symbol", "?"),
                                        token_name=token_info.get("name", "?"),
                                        score=result["score"],
                                        details=result.get("details", {}),
                                        should_buy=True,
                                        simulated_buy_amount=config.BUY_AMOUNT_USDC,
                                        simulated_buy_price=0,
                                        simulated_tokens=0,
                                        mcap_usd=mcap,
                                        liquidity_usd=liquidity,
                                        holder_count=token_info.get("holder_count", 0),
                                        top10_pct=token_info.get("top_10_holder_rate", 0),
                                        block_number=token["block_number"],
                                        pair_address=pair_addr,
                                        honeypot_safe=hp_safe,
                                        signal_source="onchain",
                                        reason="dry_run_score_above_threshold",
                                    )
                                    logger.info(f"[Main] [DRY RUN] 信号已记录到 signal_log.jsonl")
                        else:
                            # 正常或模拟模式: 完整流程
                            process_new_token(w3, scanner, executor, token)

                    except Exception as e:
                        logger.error(f"[Main] 处理代币失败: {e}")

            # 等待下一轮
            time.sleep(config.POLL_INTERVAL_SEC)

        except KeyboardInterrupt:
            logger.info("[Main] 收到退出信号，停止运行")
            break
        except Exception as e:
            logger.error(f"[Main] 主循环异常: {e}")
            time.sleep(config.POLL_INTERVAL_SEC)


if __name__ == "__main__":
    main()
