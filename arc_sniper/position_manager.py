#!/usr/bin/env python3
"""
Position Manager — Arc Sniper 持仓生命周期管理
================================================

规则:
  - 买入后记录持仓 (positions.json)
  - 止盈: mcap < $1M → 卖出价统一 $1M mcap; mcap > $1M → 涨 50% 卖出
  - 无止损: 除非砸盘 (价格 -50% 或流动性大幅衰减) → 市价清仓
  - 动态仓位: 买入金额随 USDC 余额增长而增大
  - 定时巡检: 每分钟检查所有持仓

用法:
    from position_manager import PositionManager
    pm = PositionManager()
    pm.add_position(token, tx_hash, buy_price, amount, mcap)
    pm.check_all()  # 巡检所有持仓
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dag_swap import DagSwap
from config import PRIVATE_KEY, RPC_URL, DAG_ROUTER, WUSDC_ADDRESS

logger = logging.getLogger("arc.position_manager")

# ═══════════════════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════════════════

BASE_DIR = Path(__file__).parent
POSITIONS_FILE = BASE_DIR / "positions.json"

# 止盈规则
TARGET_MCAP_SMALL = 1_000_000   # mcap < $1M → 目标 $1M
TP_PCT_LARGE = 50                # mcap > $1M → 涨 50% 卖

# 砸盘保护
CRASH_PRICE_DROP_PCT = 50        # 价格距买入 -50% → 砸盘
CRASH_LIQUIDITY_DROP_PCT = 60    # 流动性距买入 -60% → 砸盘

# 动态仓位
BASE_BUY_AMOUNT = 5              # 基础买入 $5
MAX_BUY_AMOUNT = 50              # 最大买入 $50
# 余额越多仓位越大: amount = clamp(BASE + (balance-10) * 0.15, BASE, MAX)

# 检查间隔
CHECK_INTERVAL_SEC = 60          # 每分钟巡检


# ═══════════════════════════════════════════════════════════════
# 数据结构
# ═══════════════════════════════════════════════════════════════

@dataclass
class Position:
    token_address: str
    token_symbol: str
    tx_hash: str
    buy_price_usdc: float        # 买入时 1 token = ? USDC
    buy_mcap_usd: float          # 买入时 mcap
    buy_liquidity_usd: float     # 买入时池子流动性
    amount_token: float          # 持有数量
    cost_usdc: float             # 花费 USDC
    buy_time: float             # unix timestamp
    status: str = "open"         # open / sold / crashed
    sell_tx_hash: str = ""
    sell_price_usdc: float = 0.0
    sell_time: float = 0.0
    sell_reason: str = ""        # tp / crash / manual
    pnl_usdc: float = 0.0
    peak_price_usdc: float = 0.0 # 持仓期间最高价
    peak_mcap_usd: float = 0.0
    peak_time: float = 0.0
    is_simulated: bool = False    # 模拟持仓（不真实交易，只追踪价格到止盈）


# ═══════════════════════════════════════════════════════════════
# Position Manager
# ═══════════════════════════════════════════════════════════════

class PositionManager:
    """持仓全生命周期管理。"""

    def __init__(self, private_key: str = PRIVATE_KEY, rpc_url: str = RPC_URL):
        self.swap = DagSwap(rpc_url, private_key)
        self.positions: dict[str, Position] = {}  # token_address → Position
        self._load()

    # ─────────────────────────────────────────────────────────────
    # 持久化
    # ─────────────────────────────────────────────────────────────

    def _load(self):
        """从 positions.json 加载持仓"""
        if not POSITIONS_FILE.exists():
            return
        try:
            data = json.loads(POSITIONS_FILE.read_text())
            for addr, pos_data in data.items():
                self.positions[addr] = Position(**pos_data)
            logger.info(f"[PM] 加载 {len(self.positions)} 个持仓")
        except Exception as e:
            logger.error(f"[PM] 加载持仓失败: {e}")

    def _save(self):
        """保存持仓到 positions.json"""
        data = {addr: asdict(pos) for addr, pos in self.positions.items()}
        POSITIONS_FILE.write_text(json.dumps(data, indent=2))

    # ─────────────────────────────────────────────────────────────
    # 仓位管理
    # ─────────────────────────────────────────────────────────────

    @staticmethod
    def calc_buy_amount(usdc_balance: float) -> float:
        """
        动态仓位计算: 余额越多买入越多，但有上限。
        余额 $10 → $5, 余额 $50 → $11, 余额 $200 → $30.5, 余额 $500+ → $50
        """
        if usdc_balance <= 0:
            return 0
        amount = BASE_BUY_AMOUNT + max(0, usdc_balance - 10) * 0.15
        return min(MAX_BUY_AMOUNT, max(BASE_BUY_AMOUNT, round(amount, 2)))

    def add_position(
        self,
        token_address: str,
        token_symbol: str,
        tx_hash: str,
        buy_price_usdc: float,
        buy_mcap_usd: float,
        buy_liquidity_usd: float,
        amount_token: float,
        cost_usdc: float,
        is_simulated: bool = False,
    ):
        """买入成功后记录持仓"""
        pos = Position(
            token_address=token_address.lower(),
            token_symbol=token_symbol,
            tx_hash=tx_hash,
            buy_price_usdc=buy_price_usdc,
            buy_mcap_usd=buy_mcap_usd,
            buy_liquidity_usd=buy_liquidity_usd,
            amount_token=amount_token,
            cost_usdc=cost_usdc,
            buy_time=time.time(),
            peak_price_usdc=buy_price_usdc,
            peak_mcap_usd=buy_mcap_usd,
            peak_time=time.time(),
            is_simulated=is_simulated,
        )
        self.positions[token_address.lower()] = pos
        self._save()
        tag = "[SIM]" if is_simulated else ""
        logger.info(
            f"[PM] {tag}新持仓: {token_symbol} @ ${buy_price_usdc:.8f} "
            f"mcap=${buy_mcap_usd:,.0f} amt={amount_token:.2f} cost=${cost_usdc:.2f}"
        )

    # ─────────────────────────────────────────────────────────────
    # 价格 & 流动性查询
    # ─────────────────────────────────────────────────────────────

    def get_current_price(self, token_address: str) -> Optional[dict]:
        """
        查询当前价格和流动性。
        通过模拟小额 swap 获取价格。

        返回:
            {
                "price_usdc": float,     # 1 token = ? USDC
                "mcap_usd": float,        # 市值 (需要 totalSupply)
                "liquidity_usd": float,   # 池子 wUSDC 量 * 2 (估算)
                "token_balance": float,  # 我们持有的量
            }
        """
        addr = token_address.lower()
        pool_path = self.swap.find_pool_path(token_address)
        if not pool_path:
            return self._estimate_price_from_reserves(token_address)

        # 模拟 0.01 USDC 买入获取价格
        sim = self.swap.simulate(token_address, 0.01, pool_path)
        if not sim.get("success"):
            return self._estimate_price_from_reserves(token_address)

        # price = expected_tokens / 0.01 USDC → 但这是 USDC per token
        # expected_output = tokens for 0.01 USDC
        # price_usdc = 0.01 / expected_output (1 token = ? USDC)
        tokens_per_001 = sim["expected_output"]
        if tokens_per_001 <= 0:
            return self._estimate_price_from_reserves(token_address)
        price_usdc = 0.01 / tokens_per_001

        # 获取 token balance (我们持有多少)
        token_bal = self.swap.get_token_balance(token_address)

        # 获取池子流动性 (wUSDC 余额)
        pool_addr = pool_path["pool"]
        wusdc = WUSDC_ADDRESS
        bal_data = "0x70a08231" + pool_addr[2:].lower().zfill(64)
        resp = self.swap._curl_rpc("eth_call", [
            {"to": wusdc, "data": "0x" + bal_data}, "latest"
        ])
        pool_wusdc = int(resp.get("result", "0x0"), 16) / 1e6 if resp.get("result", "0x") != "0x" else 0

        # totalSupply 获取 mcap
        ts_data = "0x18160ddd"  # totalSupply()
        resp2 = self.swap._curl_rpc("eth_call", [
            {"to": token_address, "data": "0x" + ts_data}, "latest"
        ])
        total_supply = int(resp2.get("result", "0x0"), 16) / 1e18 if resp2.get("result", "0x") != "0x" else 0

        mcap_usd = total_supply * price_usdc if total_supply > 0 else 0
        liquidity_usd = pool_wusdc * 2  # 粗估：两边等值

        return {
            "price_usdc": price_usdc,
            "mcap_usd": mcap_usd,
            "liquidity_usd": liquidity_usd,
            "token_balance": token_bal,
            "total_supply": total_supply,
        }

    def _estimate_price_from_reserves(self, token_address: str) -> Optional[dict]:
        """
        备用价格估算：先尝试 DexScreener API，再尝试链上储备金。
        """
        # 方法A: DexScreener API
        try:
            import requests as _req
            r = _req.get(f"https://api.dexscreener.com/latest/dex/tokens/{token_address}", timeout=10)
            if r.status_code == 200:
                pairs = r.json().get("pairs", [])
                if pairs:
                    best = max(pairs, key=lambda p: (p.get("liquidity", {}) or {}).get("usd", 0))
                    price = float(best.get("priceUsd", 0) or 0)
                    liq = (best.get("liquidity", {}) or {}).get("usd", 0) or 0
                    mcap = float(best.get("fdv", 0) or 0)
                    if price > 0:
                        return {
                            "price_usdc": price,
                            "mcap_usd": mcap,
                            "liquidity_usd": liq,
                            "token_balance": 0,
                            "total_supply": 0,
                        }
        except Exception as e:
            logger.debug(f"[PM] DexScreener query failed {token_address[:12]}...: {e}")

        # 方法B: 链上储备金估算 (最后手段)
        try:
            resp_ts = self.swap._curl_rpc("eth_call", [
                {"to": token_address, "data": "0x18160ddd"}, "latest"
            ])
            total_supply = int(resp_ts.get("result", "0x0"), 16) / 1e18 if resp_ts.get("result", "0x") != "0" else 0
            if total_supply <= 0:
                return None

            transfer_topic = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df57f1170"
            resp_log = self.swap._curl_rpc("eth_getLogs", [{
                "address": token_address,
                "topics": [transfer_topic],
                "fromBlock": "0x" + format(max(0, int(self.swap._curl_rpc("eth_blockNumber", []).get("result", "0x0"), 16) - 5000), "x"),
                "toBlock": "latest",
            }])
            logs = resp_log.get("result", [])
            pair_addr = None
            for log in logs[:20]:
                to_addr = "0x" + log["topics"][2][26:]
                bal_data = "0x70a08231" + to_addr[2:].lower().zfill(64)
                resp_bal = self.swap._curl_rpc("eth_call", [{"to": WUSDC_ADDRESS, "data": "0x" + bal_data}, "latest"])
                wusdc_bal = int(resp_bal.get("result", "0x0"), 16) / 1e6 if resp_bal.get("result", "0x") != "0" else 0
                if wusdc_bal > 0:
                    pair_addr = to_addr
                    break
            if not pair_addr:
                return None

            bal_data = "0x70a08231" + pair_addr[2:].lower().zfill(64)
            resp_bal = self.swap._curl_rpc("eth_call", [
                {"to": WUSDC_ADDRESS, "data": "0x" + bal_data}, "latest"
            ])
            pool_wusdc = int(resp_bal.get("result", "0x0"), 16) / 1e6 if resp_bal.get("result", "0x") != "0" else 0

            bal_data2 = "0x70a08231" + pair_addr[2:].lower().zfill(64)
            resp_bal2 = self.swap._curl_rpc("eth_call", [
                {"to": token_address, "data": "0x" + bal_data2}, "latest"
            ])
            pool_tokens = int(resp_bal2.get("result", "0x0"), 16) / 1e18 if resp_bal2.get("result", "0x") != "0" else 0

            if pool_tokens <= 0 or pool_wusdc <= 0:
                return None

            price_usdc = pool_wusdc / pool_tokens
            mcap_usd = total_supply * price_usdc
            liquidity_usd = pool_wusdc * 2

            return {
                "price_usdc": price_usdc,
                "mcap_usd": mcap_usd,
                "liquidity_usd": liquidity_usd,
                "token_balance": 0,
                "total_supply": total_supply,
            }
        except Exception as e:
            logger.debug(f"[PM] reserves estimation failed {token_address[:12]}...: {e}")
            return None

    def _check_take_profit(self, pos: Position, current: dict) -> Optional[str]:
        """
        检查是否触发止盈。

        规则:
            - mcap < $1M 买入 → 目标 mcap $1M (卖出全部)
            - mcap > $1M 买入 → 涨 50% 卖出

        返回: 止盈原因或 None
        """
        current_mcap = current["mcap_usd"]
        current_price = current["price_usdc"]

        # 更新峰值
        if current_price > pos.peak_price_usdc:
            pos.peak_price_usdc = current_price
            pos.peak_mcap_usd = current_mcap
            pos.peak_time = time.time()

        # 规则 1: 小 mcap → 目标 $1M
        if pos.buy_mcap_usd < TARGET_MCAP_SMALL:
            if current_mcap >= TARGET_MCAP_SMALL:
                return f"tp_mcap_1M (mcap ${current_mcap:,.0f} ≥ $1M)"

        # 规则 2: 大 mcap → 涨 50%
        else:
            tp_price = pos.buy_price_usdc * (1 + TP_PCT_LARGE / 100)
            if current_price >= tp_price:
                return f"tp_50pct (price ${current_price:.8f} ≥ ${tp_price:.8f})"

        return None

    def _check_crash(self, pos: Position, current: dict) -> Optional[str]:
        """
        检查是否触发砸盘保护。

        规则:
            - 价格距买入 -50% → 砸盘
            - 流动性距买入 -60% → 砸盘
            - 两个满足任一即触发

        返回: 砸盘原因或 None
        """
        current_price = current["price_usdc"]
        current_liq = current["liquidity_usd"]

        # 价格暴跌
        price_drop_pct = (1 - current_price / pos.buy_price_usdc) * 100
        if price_drop_pct >= CRASH_PRICE_DROP_PCT:
            return f"crash_price (跌 {price_drop_pct:.1f}%)"

        # 流动性大幅衰减
        if pos.buy_liquidity_usd > 0:
            liq_drop_pct = (1 - current_liq / pos.buy_liquidity_usd) * 100
            if liq_drop_pct >= CRASH_LIQUIDITY_DROP_PCT:
                return f"crash_liq (流动性 -{liq_drop_pct:.1f}%)"

        return None

    # ─────────────────────────────────────────────────────────────
    # 执行卖出
    # ─────────────────────────────────────────────────────────────

    def _execute_sell(self, pos: Position, reason: str, current: dict) -> bool:
        """
        执行卖出 swap，更新持仓状态。
        模拟持仓只记录状态，不真实交易。
        """
        # 模拟持仓：只记录状态，不执行真实 swap
        if pos.is_simulated:
            sim_pnl = (current["price_usdc"] / pos.buy_price_usdc - 1) * pos.cost_usdc if pos.buy_price_usdc > 0 else 0
            pos.status = "sold"
            pos.sell_price_usdc = current["price_usdc"]
            pos.sell_time = time.time()
            pos.sell_reason = reason
            pos.pnl_usdc = sim_pnl
            self._save()
            logger.info(
                f"[PM] 📝 [SIM] 模拟卖出: {pos.token_symbol} "
                f"reason={reason} sim_PnL=${sim_pnl:+.2f}"
            )
            return True

        token_addr = pos.token_address
        amount = current["token_balance"]

        if amount <= 0:
            logger.warning(f"[PM] {pos.token_symbol} 余额为 0，标记为已卖")
            pos.status = "sold"
            pos.sell_reason = reason
            pos.sell_time = time.time()
            self._save()
            return True

        logger.info(
            f"[PM] 执行卖出: {pos.token_symbol} amount={amount:.4f} reason={reason}"
        )

        result = self.swap.sell(
            token_address=token_addr,
            amount_token=amount,
            slippage_pct=25,  # 砸盘时允许更大滑点
        )

        if result["success"]:
            pos.status = "sold"
            pos.sell_tx_hash = result["tx_hash"]
            pos.sell_price_usdc = current["price_usdc"]
            pos.sell_time = time.time()
            pos.sell_reason = reason
            pos.pnl_usdc = result.get("usdc_received", 0) - pos.cost_usdc
            self._save()
            logger.info(
                f"[PM] ✅ 卖出成功: {pos.token_symbol} "
                f"PnL=${pos.pnl_usdc:.2f} reason={reason}"
            )
            return True
        else:
            logger.error(f"[PM] ❌ 卖出失败: {result.get('error', '?')}")
            # 卖出失败不改变状态，下次巡检重试
            return False

    # ─────────────────────────────────────────────────────────────
    # 巡检
    # ─────────────────────────────────────────────────────────────

    def check_all(self) -> dict:
        """
        巡检所有 open 持仓，返回操作摘要。
        每次巡检前从文件重新加载，确保模拟持仓能被检测到。

        返回:
            {
                "checked": int,
                "sold": list[dict],   # 本次卖出的持仓
                "errors": list[str],
            }
        """
        self._load()  # 重新加载，拾取其他实例写入的持仓
        result = {"checked": 0, "sold": [], "errors": []}
        open_positions = [p for p in self.positions.values() if p.status == "open"]

        if not open_positions:
            return result

        for pos in open_positions:
            result["checked"] += 1
            try:
                current = self.get_current_price(pos.token_address)
                if not current:
                    logger.warning(f"[PM] {pos.token_symbol} 价格查询失败，跳过")
                    result["errors"].append(f"{pos.token_symbol}: price query failed")
                    continue

                # 更新峰值
                if current["price_usdc"] > pos.peak_price_usdc:
                    pos.peak_price_usdc = current["price_usdc"]
                    pos.peak_mcap_usd = current["mcap_usd"]
                    pos.peak_time = time.time()

                # 1. 砸盘保护 — 模拟持仓跳过（无止损，只有盈利离场）
                if not pos.is_simulated:
                    crash_reason = self._check_crash(pos, current)
                    if crash_reason:
                        logger.warning(f"[PM] 🚨 砸盘保护触发: {pos.token_symbol} — {crash_reason}")
                        if self._execute_sell(pos, crash_reason, current):
                            result["sold"].append({
                                "symbol": pos.token_symbol,
                                "reason": crash_reason,
                                "price": current["price_usdc"],
                                "pnl": pos.pnl_usdc,
                            })
                        continue

                # 2. 检查止盈
                tp_reason = self._check_take_profit(pos, current)
                if tp_reason:
                    logger.info(f"[PM] 🎯 止盈触发: {pos.token_symbol} — {tp_reason}")
                    if self._execute_sell(pos, tp_reason, current):
                        result["sold"].append({
                            "symbol": pos.token_symbol,
                            "reason": tp_reason,
                            "price": current["price_usdc"],
                            "pnl": pos.pnl_usdc,
                        })
                    continue

                # 3. 正常持仓 — 记录状态
                logger.debug(
                    f"[PM] {pos.token_symbol} 持仓中: "
                    f"price=${current['price_usdc']:.8f} "
                    f"mcap=${current['mcap_usd']:,.0f} "
                    f"PnL={(current['price_usdc'] / pos.buy_price_usdc - 1) * 100:+.1f}%"
                )

            except Exception as e:
                logger.error(f"[PM] {pos.token_symbol} 巡检异常: {e}")
                result["errors"].append(f"{pos.token_symbol}: {e}")

        self._save()
        return result

    # ─────────────────────────────────────────────────────────────
    # 查询
    # ─────────────────────────────────────────────────────────────

    def get_open_positions(self) -> list[Position]:
        """获取所有 open 持仓"""
        return [p for p in self.positions.values() if p.status == "open"]

    def get_position(self, token_address: str) -> Optional[Position]:
        """查询单个持仓"""
        return self.positions.get(token_address.lower())

    def remove_position(self, token_address: str):
        """手动移除持仓"""
        addr = token_address.lower()
        if addr in self.positions:
            del self.positions[addr]
            self._save()

    def mark_manual(self, token_address: str, note: str = ""):
        """标记为手动管理（从自动系统中移除）"""
        pos = self.get_position(token_address)
        if pos:
            pos.status = "manual"
            pos.sell_reason = f"manual: {note}"
            self._save()


# ═══════════════════════════════════════════════════════════════
# 通知
# ═══════════════════════════════════════════════════════════════

def notify_sell(symbol: str, reason: str, price: float, pnl: float, tx_hash: str):
    """卖出通知 — 已静默（用户通过 Web3 钱包自行查看）"""
    emoji = "🎯" if "tp" in reason else "🚨"
    logger.info(f"[PM] {emoji} 卖出 {symbol} ({reason}) PnL=${pnl:+.2f}")
    # 不发送 Jarvis 通知


def notify_buy(symbol: str, price: float, amount: float, cost: float, mcap: float, tx_hash: str):
    """买入通知 — 已静默（用户通过 Web3 钱包自行查看）"""
    logger.info(f"[PM] ✅ 买入 {symbol} @ ${price:.8f} amt={amount:.2f} cost=${cost:.2f}")
    # 不发送 Jarvis 通知


# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════

def main():
    """命令行: python3 position_manager.py [check|status|sell <token>]"""
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

    pm = PositionManager()

    if len(sys.argv) < 2 or sys.argv[1] == "check":
        result = pm.check_all()
        print(f"\n巡检完成: {result['checked']} 个持仓")
        if result["sold"]:
            for s in result["sold"]:
                print(f"  卖出: {s['symbol']} ({s['reason']}) PnL={s['pnl']:.2f}")
        if result["errors"]:
            for e in result["errors"]:
                print(f"  错误: {e}")

    elif sys.argv[1] == "status":
        positions = pm.get_open_positions()
        if not positions:
            print("无 open 持仓")
        for pos in positions:
            pnl_pct = (pos.peak_price_usdc / pos.buy_price_usdc - 1) * 100 if pos.buy_price_usdc > 0 else 0
            print(f"\n{pos.token_symbol} ({pos.token_address[:12]}...)")
            print(f"  买入价: ${pos.buy_price_usdc:.8f}")
            print(f"  数量: {pos.amount_token:.4f}")
            print(f"  花费: ${pos.cost_usdc:.2f}")
            print(f"  买入市值: ${pos.buy_mcap_usd:,.0f}")
            print(f"  峰值价: ${pos.peak_price_usdc:.8f} (+{pnl_pct:.1f}%)")
            print(f"  状态: {pos.status}")
            print(f"  TX: {pos.tx_hash[:18]}...")

    elif sys.argv[1] == "sell" and len(sys.argv) >= 3:
        token = sys.argv[2]
        pos = pm.get_position(token)
        if not pos:
            print(f"未找到持仓: {token}")
            sys.exit(1)
        current = pm.get_current_price(token)
        if current:
            ok = pm._execute_sell(pos, "manual", current)
            if ok:
                print(f"✅ 卖出成功: {pos.token_symbol}")
            else:
                print(f"❌ 卖出失败")
        else:
            print(f"无法获取价格")

    elif sys.argv[1] == "positions":
        print(json.dumps(
            {addr: asdict(p) for addr, p in pm.positions.items()},
            indent=2,
        ))

    else:
        print("用法: python3 position_manager.py [check|status|sell <token>|positions]")


if __name__ == "__main__":
    main()
