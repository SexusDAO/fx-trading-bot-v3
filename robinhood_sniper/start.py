"""
robinhood_sniper/start.py
启动入口：
- 注入现有 NUDES 持仓（$5M 全清模式）
- 正常运行新币发现工作流（无限制）
"""

import asyncio
import logging
import time
import sys
import os
import json
import subprocess

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from robinhood_sniper.main import (
    signal_loop, monitor_loop, status_loop, notify
)
from robinhood_sniper.consensus_scanner import (
    ConsensusScanner, CONSENSUS_POSITION_USD, CONSENSUS_STOP_LOSS, CONSENSUS_TP_MILESTONES
)
from robinhood_sniper.monitor import PositionMonitor, Position
from robinhood_sniper.executor import buy_token, get_token_balance
from robinhood_sniper.executor import get_eth_balance, get_token_balance

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("/root/.openclaw/workspace/robinhood_sniper/sniper.log"),
    ]
)
logger = logging.getLogger("sniper.start")


# ─── NUDES 持仓信息（手动录入，来自测试买入记录）────────────────────────
NUDES_POSITION = {
    "token_address": "0xbe98b75361935b18d688409424a869a4c3dc7401",
    "token_symbol":  "NUDES",
    "entry_price_usd": 0.000051,
    "token_type":    "full_exit_5m",   # 特殊标记：$5M 全清
}

# $5M 全清的止盈阶梯（一档触发，全部卖出）
TP_FULL_EXIT_5M = [
    (5_000_000, 1.00),   # MC $5M → 卖 100%（全清）
]


async def main():
    logger.info("=" * 60)
    logger.info("🤖 Robinhood Chain AI 打狗机器人启动（正常模式）")
    logger.info("=" * 60)

    eth_balance = get_eth_balance()
    logger.info(f"💰 ETH 余额: {eth_balance:.6f}")

    monitor = PositionMonitor()

    # ── 注入 NUDES 现有仓位（从持仓状态文件恢复，防止重启重置）────────────
    STATE_FILE = os.path.join(os.path.dirname(__file__), ".position_state.json")

    nudes_qty = get_token_balance(NUDES_POSITION["token_address"])
    if nudes_qty > 0:
        from robinhood_sniper import monitor as mon_module
        mon_module._TP_MAP["full_exit_5m"] = TP_FULL_EXIT_5M

        # 从文件恢复状态（breakeven_done / tp_triggered_mc 等）
        saved = {}
        if os.path.exists(STATE_FILE):
            try:
                all_states = json.load(open(STATE_FILE))
                saved = all_states.get(NUDES_POSITION["token_address"], {})
                logger.info(f"[Start] 从文件恢复 NUDES 状态: {saved}")
            except Exception as e:
                logger.warning(f"[Start] 状态文件读取失败: {e}")

        pos = Position(
            token_address      = NUDES_POSITION["token_address"],
            token_symbol       = NUDES_POSITION["token_symbol"],
            entry_price_usd    = saved.get("entry_price_usd", NUDES_POSITION["entry_price_usd"]),
            entry_qty          = nudes_qty,
            token_type         = saved.get("token_type", "full_exit_5m"),
            entry_time         = saved.get('entry_time', time.time() - 600),
            entry_liq_usd      = saved.get("entry_liq_usd", 27200),
            peak_volume_h1     = saved.get("peak_volume_h1", 0),
            breakeven_done     = saved.get("breakeven_done", False),
            tp_triggered_mc    = saved.get("tp_triggered_mc", []),
            watching           = saved.get("watching", False),
            watch_triggered_at = saved.get("watch_triggered_at", 0.0),
            peak_pnl           = saved.get("peak_pnl", 0.0),
            first_50pct_time   = saved.get("first_50pct_time", 0.0),
        )
        monitor.add(pos)
        notify(
            f"📌 已注入 NUDES 持仓\n"
            f"  数量: {nudes_qty:,.0f} NUDES\n"
            f"  入场价: ${pos.entry_price_usd:.6f}\n"
            f"  策略: MC 到 $5M 全部清仓\n"
            f"  已回本: {'✅' if pos.breakeven_done else '❌'}\n"
            f"  已触发止盈: {pos.tp_triggered_mc}"
        )
    else:
        logger.warning("⚠️ NUDES 余额为 0，跳过注入（可能已被卖出）")

    # ── 自动恢复其他链上持仓（防止重启后孤儿仓位无人监控）──────────────
    from robinhood_sniper.signal import _clawby
    logger.info("[Start] 扫描链上所有代币余额，恢复监控...")

    r = subprocess.run(["baw","wallet","balance","--binanceChainId","4663","--json"],
                       capture_output=True, text=True)
    all_tokens = json.loads(r.stdout).get("data", [])

    # 读取状态文件
    all_saved = {}
    if os.path.exists(STATE_FILE):
        try:
            all_saved = json.load(open(STATE_FILE))
        except Exception:
            pass

    for t in all_tokens:
        addr  = (t.get("tokenAddress") or t.get("address","")).lower()
        sym   = t.get("symbol","?")
        bal   = float(t.get("balance", 0) or 0)
        price = float(t.get("price", 0) or 0)

        # 跳过 ETH 本身 和 已注入的 NUDES
        if addr in ("0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
                    NUDES_POSITION["token_address"].lower()):
            continue
        if bal <= 0:
            continue
        # 已在 monitor 里的跳过
        if any(p.token_address.lower() == addr for p in monitor.positions):
            continue

        # 跳过市值/流动性极低（池子归零）的代币，避免无效监控
        cur_usd = float(t.get("value") or 0)
        if cur_usd < 5.0:
            logger.warning(f"[Start] 跳过 {sym} 现值 ${cur_usd:.2f}（流动性极低，疑似归零）")
            continue

        saved = all_saved.get(addr, {})
        # 入场价优先级：state文件 > baw costPrice > 链上历史交易均价 > 当前价（最后fallback，不可靠）
        baw_cost = float(t.get("costPrice") or t.get("averageCost") or 0)
        entry_price = (
            saved.get("entry_price_usd") or   # 1. 持久化记录（最准）
            (baw_cost if baw_cost > 0 else 0)  # 2. baw 成本价
        )

        # 3. fallback：尝试从链上历史交易记录查真实买入均价
        if not entry_price:
            try:
                from robinhood_sniper.executor import _baw as _b
                ok, hist = _b(["wallet", "tx-history",
                               "--binanceChainId", "4663",
                               "--size", "50", "--json"])
                if ok and hist:
                    NATIVE = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
                    buy_eth_total = 0.0
                    buy_count     = 0
                    txs = (hist.get("data") or {}).get("transactions") or []
                    for tx in txs:
                        if tx.get("txType") != "swap": continue
                        for h in (tx.get("txHashList") or []):
                            instr = h.get("instructions") or {}
                            recvs = instr.get("receive") or []
                            sends = instr.get("send") or []
                            for r in recvs:
                                ti = r.get("tokenInfo") or {}
                                if ti.get("contractAddress","").lower() == addr:
                                    eth_spent = sum(
                                        float(s.get("amount", 0))
                                        for s in sends
                                        if (s.get("tokenInfo") or {}).get(
                                            "contractAddress","").lower() == NATIVE
                                    )
                                    recv_qty = float(r.get("amount") or 0)
                                    if eth_spent > 0 and recv_qty > 0:
                                        buy_eth_total += eth_spent
                                        buy_count     += 1
                    if buy_eth_total > 0 and buy_count > 0:
                        # 用买入时 ETH 金额 / 收到代币数量 推算单价
                        # 此处用 price 作为单价参考，但不直接用当前价
                        # 实际 ETH 成本 / 总余额 = 平均成本价
                        total_bal_now = bal
                        if total_bal_now > 0:
                            eth_price_usd = float(t.get("ethPrice") or 2400)
                            entry_price = (buy_eth_total * eth_price_usd) / total_bal_now
                            logger.info(
                                f"[Start] {sym} 链上历史推算入场价: "
                                f"${entry_price:.8f} (买入{buy_eth_total:.5f}ETH / {total_bal_now:.0f}枚)"
                            )
            except Exception as e:
                logger.debug(f"[Start] {sym} 链上查询入场价失败: {e}")

        # 3b. 从 baw market-order list 查买入记录（tx-history 没有时的补充）
        if not entry_price:
            try:
                from robinhood_sniper.executor import _baw as _b2
                ok2, orders = _b2(["market-order", "list",
                                   "--binanceChainId", "4663",
                                   "--json"])
                if ok2 and orders:
                    order_list = (orders.get("data") or {}).get("list", [])
                    buy_orders = [
                        o for o in order_list
                        if (o.get("toToken") or "").lower() == addr
                        and o.get("status") == "FINISHED"
                    ]
                    if buy_orders:
                        eth_spent = sum(float(o.get("fromTokenQty") or 0) for o in buy_orders)
                        tok_recv  = sum(float(o.get("toTokenActualQty") or 0) for o in buy_orders)
                        if eth_spent > 0 and tok_recv > 0:
                            eth_usd = float(buy_orders[0].get("ethPrice") or t.get("ethPrice") or 2400)
                            entry_price = (eth_spent * eth_usd) / tok_recv
                            logger.info(
                                f"[Start] {sym} market-order 推算入场价: "
                                f"${entry_price:.8f} ({len(buy_orders)}笔买入)"
                            )
            except Exception as e:
                logger.debug(f"[Start] {sym} market-order 查询入场价失败: {e}")

        # 最后fallback到当前价（确实无法获取时）
        if not entry_price:
            entry_price = price
            logger.warning(
                f"[Start] {sym} 无法获取真实入场价，fallback到当前价${price:.8f}，"
                f"止损基准不准！建议手动确认"
            )

        pos = Position(
            token_address      = addr,
            token_symbol       = sym,
            entry_price_usd    = entry_price,
            entry_qty          = bal,
            token_type         = saved.get("token_type", "normal"),
            entry_time         = saved.get('entry_time', time.time() - 3600),
            entry_liq_usd      = saved.get("entry_liq_usd", 0),
            peak_volume_h1     = saved.get("peak_volume_h1", 0),
            breakeven_done     = saved.get("breakeven_done", False),
            tp_triggered_mc    = saved.get("tp_triggered_mc", []),
            watching           = saved.get("watching", False),
            watch_triggered_at = saved.get("watch_triggered_at", 0.0),
            peak_pnl           = saved.get("peak_pnl", 0.0),
            first_50pct_time   = saved.get("first_50pct_time", 0.0),
        )
        monitor.add(pos)
        logger.info(f"[Start] 自动恢复监控: {sym} {bal:.2f}枚 entry=${entry_price:.6f}")

    logger.info(f"[Start] 共监控 {len(monitor.positions)} 个持仓")

    # ── 启动全部协程 ────────────────────────────────────────────────────────
    async def _guarded(coro_fn, name, *args, **kwargs):
        """包裹协程：异常/正常退出后自动重启，防止单个协程退出导致进程结束"""
        while True:
            try:
                await coro_fn(*args, **kwargs)
                logger.warning(f"[Main] {name} 正常返回，5s 后重启")
            except asyncio.CancelledError:
                break
            except Exception as e:
                import traceback
                logger.error(f"[Main] {name} 崩溃: {e}")
                traceback.print_exc()
            await asyncio.sleep(5)

    # ── 共识币扫描器（独立线程，每60s扫描 2h+ 稳定币）──────────────────
    import threading

    def _on_consensus_signal(candidate: dict):
        """共识币扫描器发现信号时的买入回调"""
        pair  = candidate["pair"]
        score = candidate["score"]
        token_addr = candidate["token_addr"]
        sym   = pair.get("baseToken", {}).get("symbol", "?")
        mc    = float(pair.get("marketCap") or 0)
        liq   = float((pair.get("liquidity") or {}).get("usd", 0) or 0)
        price = float(pair.get("priceUsd") or 0)

        # 已持仓检查
        if any(p.token_address.lower() == token_addr for p in monitor.positions):
            logger.info(f"[Consensus] {sym} 已持仓，跳过")
            return

        # 计算买入 ETH 金额
        eth_price = 2495.0  # 近似，executor 会用实际价格
        eth_amount = round(CONSENSUS_POSITION_USD / eth_price, 6)

        logger.info(f"[Consensus] 📥 买入 {sym} | score={score} | ${CONSENSUS_POSITION_USD} | eth={eth_amount:.6f}")
        notify(f"📥 共识币买入: {sym} | score={score:.1f} | MC=${mc:,.0f} | 流动性=${liq:,.0f}")

        ok, tx_data = buy_token(token_addr, eth_amount)
        if not ok:
            logger.warning(f"[Consensus] {sym} 买入失败: {tx_data}")
            notify(f"⚠️ 共识币买入失败: {sym}")
            return

        # 等链上确认再读实际余额
        time.sleep(5)
        recv_qty = get_token_balance(token_addr)
        if recv_qty <= 0:
            logger.warning(f"[Consensus] {sym} 买入后余额为0，可能尚未确认")
            recv_qty = CONSENSUS_POSITION_USD / price if price > 0 else 1.0  # fallback估算

        entry_price = (eth_amount * eth_price) / recv_qty if recv_qty > 0 else price
        pos = Position(
            token_address   = token_addr,
            token_symbol    = sym,
            entry_price_usd = entry_price,
            entry_qty       = recv_qty,
            entry_liq_usd   = liq,
            token_type      = "consensus",   # 区分打新 vs 共识
            _stop_loss_override = CONSENSUS_STOP_LOSS,  # -20% 止损（比打新宽松）
        )
        monitor.add(pos)
        consensus_scanner.mark_bought(token_addr)
        notify(f"✅ 共识币入场: {sym} | {recv_qty:,.0f}枚 | 入场价${entry_price:.8f}")

    consensus_scanner = ConsensusScanner(
        on_signal  = _on_consensus_signal,
        notify_fn  = notify,
    )
    consensus_thread = threading.Thread(
        target=consensus_scanner.run,
        name="consensus-scanner",
        daemon=True,
    )
    consensus_thread.start()
    logger.info("[Main] ✅ 共识币扫描线程已启动")

    await asyncio.gather(
        _guarded(signal_loop,  "signal",  monitor, buy_limit=0),
        _guarded(monitor_loop, "monitor", monitor),
        _guarded(status_loop,  "status",  monitor),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 用户中断，程序退出")
