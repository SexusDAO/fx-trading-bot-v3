"""
robinhood_sniper/executor.py
第四层：执行层 — 仓位计算 + baw 买入 + 挂止盈止损单
"""

import subprocess
import logging
import json
from typing import Optional
from .config import (
    CHAIN_ID, NATIVE_TOKEN,
    MIN_POSITION_USD, MAX_POSITION_USD, MIN_POSITION_ETH,
    HIGH_CONF_SCORE, HIGH_CONF_MULTIPLIER, HIGH_CONF_MAX_USD,
    SLIPPAGE_NORMAL, SLIPPAGE_HOT, SLIPPAGE_SELL, HOT_THRESHOLD_BUYS,
    MEV_PROTECTION, GAS_LEVEL,
    STOP_LOSS_PCT,
)

logger = logging.getLogger("sniper.executor")


def _baw(args: list[str]) -> tuple[bool, dict]:
    """执行 baw 命令，返回 (success, json_data)"""
    cmd = ["baw"] + args + ["--json"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        data = json.loads(result.stdout)
        if data.get("success"):
            return True, data.get("data", {})
        else:
            logger.error(f"baw error: {data}")
            return False, data
    except Exception as e:
        logger.error(f"baw 执行异常: {e}")
        return False, {}


def get_eth_balance() -> float:
    """查询 agent 钱包 Robinhood Chain 上的 ETH 余额"""
    ok, data = _baw(["wallet", "balance", "--binanceChainId", CHAIN_ID,
                     "--tokenAddress", NATIVE_TOKEN])
    if not ok:
        return 0.0
    tokens = data if isinstance(data, list) else []
    for t in tokens:
        addr_field = t.get("tokenAddress") or t.get("address", "")
        if addr_field.lower() == NATIVE_TOKEN.lower():
            return float(t.get("balance", 0) or 0)
    return 0.0


def get_eth_price_usd() -> float:
    """通过 baw quote 获取当前 ETH/USD 价格"""
    ok, data = _baw([
        "wallet", "balance",
        "--binanceChainId", CHAIN_ID,
        "--tokenAddress", NATIVE_TOKEN,
    ])
    if ok and isinstance(data, list):
        for t in data:
            addr_field = t.get("tokenAddress") or t.get("address", "")
        if addr_field.lower() == NATIVE_TOKEN.lower():
                price = float(t.get("price") or 0)
                if price > 0:
                    return price
    # fallback：用 BSC 上 ETH 价格
    try:
        import subprocess, json as _json
        r = subprocess.run(
            ["baw", "wallet", "balance", "--binanceChainId", "1",
             "--symbol", "ETH", "--json"],
            capture_output=True, text=True, timeout=10
        )
        d = _json.loads(r.stdout)
        for t in (d.get("data") or []):
            p = float(t.get("price") or 0)
            if p > 0:
                return p
    except Exception:
        pass
    return 2400.0  # 最终 fallback


def calc_position(score: int, eth_balance: float) -> float:
    """
    根据评分在 $10~$25 USD 区间线性插值，实时换算为 ETH 买入量。

    score 满分 = 10（P1 修复后去掉了描述/社交 2 分）
    score=5  → $10 USD（最低门槛）
    score=10 → $25 USD（满分）
    中间线性插值：每 +1 分 +$3 USD
    """
    if eth_balance <= 0:
        return 0.0

    # 线性插值：score 7→10 对应 $10→$25
    min_score, max_score = 7, 10
    score_clamped = max(min_score, min(max_score, score))
    ratio = (score_clamped - min_score) / (max_score - min_score)  # 0.0~1.0
    position_usd  = MIN_POSITION_USD + ratio * (MAX_POSITION_USD - MIN_POSITION_USD)

    # 高置信度信号：score >= HIGH_CONF_SCORE 仓位翻倍（最高 $50）
    if score >= HIGH_CONF_SCORE:
        position_usd = min(position_usd * HIGH_CONF_MULTIPLIER, HIGH_CONF_MAX_USD)
        logger.info(f"[Exec] 高置信度信号 score={score}，仓位翻倍 → ${position_usd:.1f}")

    # 换算为 ETH
    eth_price     = get_eth_price_usd()
    position_eth  = position_usd / eth_price if eth_price > 0 else 0.0

    # 余额保护：不超过可用余额 50%
    position_eth  = min(position_eth, eth_balance * 0.5)
    position_eth  = max(position_eth, MIN_POSITION_ETH)

    logger.info(
        f"[Exec] 仓位计算 score={score} → ${position_usd:.1f} USD "
        f"/ ETH价格=${eth_price:.0f} → {position_eth:.6f} ETH"
    )
    return round(position_eth, 6)


def get_quote(token_address: str, eth_amount: float) -> Optional[dict]:
    """买入前获取报价，确认滑点可接受"""
    ok, data = _baw([
        "market-order", "quote",
        "--binanceChainId", CHAIN_ID,
        "--fromToken", NATIVE_TOKEN,
        "--toToken", token_address,
        "--fromTokenQty", str(eth_amount),
    ])
    return data if ok else None


# 模拟卖出结果常量
SELL_QUOTE_OK      = "ok"       # 通过
SELL_QUOTE_DENY    = "deny"     # 确认蜜罐（报价为0 / 合约拒绝）
SELL_QUOTE_RETRY   = "retry"    # 暂时无流动性，可稍后重试


def check_sell_quote(token_address: str) -> tuple[str, str]:
    """
    模拟卖出检测：向 DEX 询价「代币→ETH」。
    返回 (status, message)，status 为上方三个常量之一：
      - SELL_QUOTE_OK    : 通过，可买入
      - SELL_QUOTE_DENY  : 确认蜜罐/貔貅，直接拒绝
      - SELL_QUOTE_RETRY : 暂时无流动性（池子刚上线），建议 60s 后重试

    使用等值 $10 的代币数量询价（而非固定1枚），避免极低价代币因精度截断
    返回 0.00000000 ETH 而被误判为通过（WORTHLESS 式 bug）。
    """
    # 先用少量 ETH 反向询价，算出等值 $10 的代币数量
    qty_to_use = "1000"  # 默认 fallback：1000枚
    try:
        p_ok, p_data = _baw([
            "market-order", "quote",
            "--binanceChainId", CHAIN_ID,
            "--fromToken", NATIVE_TOKEN,
            "--toToken", token_address,
            "--fromTokenQty", "0.001",
            "--slippage", "25",
        ])
        if p_ok and p_data:
            tok_per_001eth = float(
                str(p_data.get("toCoinAmount") or p_data.get("toAmount") or 0)
                .replace(",", "") or 0
            )
            if tok_per_001eth > 0:
                # 0.001 ETH ≈ $2.4，等值 $10 ≈ 4.2x
                qty_for_10usd = int(tok_per_001eth * 4.2)
                qty_to_use = str(max(qty_for_10usd, 1))
    except Exception:
        pass

    ok, data = _baw([
        "market-order", "quote",
        "--binanceChainId", CHAIN_ID,
        "--fromToken", token_address,
        "--toToken", NATIVE_TOKEN,
        "--fromTokenQty", qty_to_use,
        "--slippage", "25",
    ])
    if ok and data:
        to_amt = float(
            str(data.get("toCoinAmount") or data.get("toAmount") or "0")
            .replace(",", "") or 0
        )
        if to_amt > 1e-9:  # 必须 > 0.000000001 ETH，过滤浮点噪声
            return SELL_QUOTE_OK, f"✅ 模拟卖出通过 ({qty_to_use}枚→{to_amt:.8f} ETH)"
        return SELL_QUOTE_DENY, f"❌ 模拟卖出报价为0，疑似貔貅/burn合约 (qty={qty_to_use})"

    err_str = str(data).lower()
    if "no liquidity" in err_str or "liquidity available" in err_str:
        return SELL_QUOTE_RETRY, f"⏳ 暂无流动性，等待重试: {str(data)[:80]}"
    return SELL_QUOTE_DENY, f"❌ 模拟卖出报价失败: {str(data)[:80]}"


def cancel_limit_order(order_id: str) -> bool:
    """取消链上限价单（partial_sell 后重置止损单使用）"""
    if not order_id:
        return False
    ok, data = _baw([
        "limit-order", "cancel",
        "--binanceChainId", CHAIN_ID,
        "--orderId", order_id,
    ])
    logger.info(f"[Exec] 取消止损单 {order_id[:12]}... {'✅' if ok else '❌'}")
    return ok


def buy_token(token_address: str, eth_amount: float,
              is_hot: bool = False) -> tuple[bool, dict]:
    """
    市价买入
    is_hot=True 时使用高滑点（49%），适合快速拉升代币
    """
    slippage = str(SLIPPAGE_HOT if is_hot else SLIPPAGE_NORMAL)
    logger.info(f"[Exec] 买入 {token_address[:10]}... "
                f"金额={eth_amount} ETH 滑点={slippage}%")

    ok, data = _baw([
        "market-order", "swap",
        "--binanceChainId", CHAIN_ID,
        "--fromToken", NATIVE_TOKEN,
        "--toToken", token_address,
        "--fromTokenQty", str(eth_amount),
        "--slippage", slippage,
        "--mev", MEV_PROTECTION,
        "--gasLevel", GAS_LEVEL,
    ])

    if ok:
        logger.info(f"[Exec] ✅ 买入成功: {data}")
    else:
        logger.error(f"[Exec] ❌ 买入失败: {data}")

    return ok, data


def get_token_balance(token_address: str) -> float:
    """查询某代币持仓数量"""
    ok, data = _baw(["wallet", "balance",
                     "--binanceChainId", CHAIN_ID,
                     "--tokenAddress", token_address])
    if not ok:
        return 0.0
    tokens = data if isinstance(data, list) else []
    for t in tokens:
        addr_field = t.get("tokenAddress") or t.get("address", "")
        if addr_field.lower() == token_address.lower():
            return float(t.get("balance", 0) or 0)
    return 0.0


def place_limit_sell(token_address: str, qty: float,
                     trigger_price_usd: float) -> tuple[bool, dict]:
    """挂限价卖出单（止盈/止损）"""
    from .config import NATIVE_TOKEN as QUOTE_TOKEN
    ok, data = _baw([
        "limit-order", "sell",
        "--binanceChainId", CHAIN_ID,
        "--fromToken", token_address,
        "--toToken", NATIVE_TOKEN,
        "--fromTokenQty", str(round(qty, 6)),
        "--triggerPrice", f"${trigger_price_usd:.8f}",
        "--slippage", str(SLIPPAGE_SELL),  # 卖出滑点
        "--gasLevel", GAS_LEVEL,
    ])
    if ok:
        logger.info(f"[Exec] 挂止盈/损单 ${trigger_price_usd:.6f} qty={qty:.4f} ✅")
    else:
        logger.warning(f"[Exec] 挂单失败: {data}")
    return ok, data


def smart_sell(token_address: str, symbol: str,
               liq_usd: float = 0, slippage: int = None) -> tuple[bool, float]:
    """
    智能卖出：默认一笔交易清空全部持仓。
    只有当持仓价值 > 流动性 5% 时才分批，避免不必要的多笔 gas。

    返回 (success, recv_eth_total)
    """
    import time as _t
    from robinhood_sniper.signal import fetch_pair_data

    qty = get_token_balance(token_address)
    if qty <= 0:
        logger.info(f"[Exec] smart_sell {symbol} 余额为0，跳过")
        return True, 0.0

    # 查实时价格和流动性（如果没有传入）
    if liq_usd <= 0:
        pair = fetch_pair_data(token_address)
        if pair:
            liq_usd = float((pair.get('liquidity') or {}).get('usd', 0) or 0)
            price   = float(pair.get('priceUsd') or 0)
        else:
            liq_usd = 0
            price   = 0
    else:
        pair  = fetch_pair_data(token_address)
        price = float(pair.get('priceUsd') or 0) if pair else 0

    holding_val = qty * price
    _slippage   = slippage or SLIPPAGE_SELL

    # 判断是否需要分批（持仓价值 > 流动性5%才分）
    need_batch = liq_usd > 0 and holding_val > liq_usd * 0.05
    if need_batch:
        batch_count = min(int(holding_val / (liq_usd * 0.04)) + 1, 5)
        logger.info(f"[Exec] smart_sell {symbol} 持仓${holding_val:.1f} > 流动性{liq_usd:.0f}×5%，"
                    f"分{batch_count}批卖出")
    else:
        batch_count = 1
        logger.info(f"[Exec] smart_sell {symbol} 持仓${holding_val:.1f} 一次卖出")

    recv_total = 0.0
    for i in range(batch_count):
        cur_qty = get_token_balance(token_address)
        if cur_qty <= 0:
            break
        _raw_qty = cur_qty if i == batch_count - 1 else cur_qty / (batch_count - i)
        # 优先用整数（避免 baw INVALID_AMOUNT 精度问题），小额代币才保留小数
        sell_qty = int(_raw_qty) if _raw_qty >= 1 else round(_raw_qty, 8)
        if sell_qty <= 0:
            break
        ok, data = _baw([
            "market-order", "swap",
            "--binanceChainId", CHAIN_ID,
            "--fromToken",     token_address,
            "--toToken",       NATIVE_TOKEN,
            "--fromTokenQty",  str(sell_qty),
            "--slippage",      str(min(_slippage + i * 3, 49)),  # 每批+3%滑点
            "--mev",           "false",
            "--gasLevel",      GAS_LEVEL,
        ])
        if ok:
            recv_total += float((data or {}).get('toCoinAmount') or
                                (data or {}).get('toAmount') or 0)
            logger.info(f"[Exec] smart_sell {symbol} 批次{i+1}/{batch_count} ✅ 卖出{sell_qty:.0f}枚")
            if i < batch_count - 1:
                _t.sleep(2)
        else:
            err = str(data).lower()
            logger.warning(f"[Exec] smart_sell {symbol} 批次{i+1} 失败: {str(data)[:80]}")
            if 'insufficient' in err or 'balance' in err:
                logger.info(f"[Exec] smart_sell {symbol} 余额不足，停止")
                break

    remaining = get_token_balance(token_address)
    logger.info(f"[Exec] smart_sell {symbol} 完成，剩余粉尘={remaining:.0f}枚")
    return recv_total > 0, recv_total


def setup_exit_orders(token_address: str, total_qty: float,
                      entry_price_usd: float) -> list[dict]:
    """
    挂止损限价单（硬止损兜底）。
    止盈改为 monitor.py 按实时市值自动触发市价卖出，不在此挂限价单。
    止损用限价单作为链上兜底，极端流动性下可能不成交，monitor 另有市价补刀。
    """
    orders = []
    sl_price = entry_price_usd * (1 + STOP_LOSS_PCT)
    if total_qty > 0 and sl_price > 0:
        ok, data = place_limit_sell(token_address, round(total_qty, 6), sl_price)
        order_id = (data or {}).get("orderId") or (data or {}).get("order_id") or ""
        orders.append({
            "type":     "stop_loss",
            "trigger":  sl_price,
            "qty":      round(total_qty, 6),
            "pct":      STOP_LOSS_PCT,
            "placed":   ok,
            "order_id": order_id,
        })
        logger.info(f"[Exec] 止损兜底单已挂 trigger=${sl_price:.6f} qty={total_qty:.4f} placed={ok}")
    return orders
