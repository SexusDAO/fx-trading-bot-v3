# ============================================================
# gas_manager.py — ETH 余额监控（ETH 兼作 gas + 买入资金）
# ============================================================
import subprocess, json, time
from config import CHAIN_ID, ETH_ADDRESS, GAS_RESERVE_USD, GAS_WARN_USD, LOG_FILE, JARVIS_TARGET

_last_check_ts = 0
CHECK_INTERVAL = 60


def _log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def get_eth_balance_usd() -> tuple[float, float]:
    """返回 (eth_qty, eth_usd_value)
    注意：0xEeee... 占位地址在多条链上均使用，例如 BNB(chain=56) 和 ETH(chain=4663)。
    必须优先匹配 Robinhood 链 (CHAIN_ID=4663) 的条目，防止读到 BNB 的低余额。
    """
    try:
        r = subprocess.run(["baw", "wallet", "balance", "--json"],
                           capture_output=True, text=True, timeout=15)
        d = json.loads(r.stdout)
        items = d.get("data", [])
        addr_lower = ETH_ADDRESS.lower()

        # 第一轮：优先匹配目标链 (CHAIN_ID) + 地址
        for item in items:
            if (item.get("address", "").lower() == addr_lower
                    and str(item.get("chain", "")) == str(CHAIN_ID)):
                qty   = float(item.get("balance", 0) or 0)
                value = float(item.get("value",   0) or 0)
                return qty, value

        # 第二轮：匹配 symbol=ETH + 目标链（以防地址匹配失败）
        for item in items:
            if (item.get("symbol", "").upper() == "ETH"
                    and str(item.get("chain", "")) == str(CHAIN_ID)):
                qty   = float(item.get("balance", 0) or 0)
                value = float(item.get("value",   0) or 0)
                return qty, value

        # 备用：任意链上 symbol=ETH（较大余额优先）
        eth_items = [item for item in items if item.get("symbol", "").upper() == "ETH"]
        if eth_items:
            best = max(eth_items, key=lambda x: float(x.get("value", 0) or 0))
            qty   = float(best.get("balance", 0) or 0)
            value = float(best.get("value",   0) or 0)
            return qty, value

    except Exception as e:
        _log(f"[GAS] 查询 ETH 余额失败: {e}")
    return 0.0, 0.0


def get_usdg_balance() -> float:
    """兼容接口：ETH 模式下返回 ETH 的 USD 等值（扣除 gas 保留后的可用资金）"""
    _, eth_usd = get_eth_balance_usd()
    available = max(0.0, eth_usd - GAS_RESERVE_USD)
    return available


def refill_eth_if_needed() -> bool:
    """
    ETH 模式下：检查 ETH 余额是否低于 GAS_WARN_USD。
    低于则推送告警（无法自动补充，需要用户手动充值）。
    """
    global _last_check_ts
    now = time.time()
    if now - _last_check_ts < CHECK_INTERVAL:
        return True
    _last_check_ts = now

    eth_qty, eth_usd = get_eth_balance_usd()
    _log(f"[GAS] ETH 余额: {eth_qty:.5f} ≈ ${eth_usd:.2f}")

    if eth_usd < GAS_WARN_USD:
        _log(f"[GAS] ⚠️  ETH 余额 ${eth_usd:.2f} < ${GAS_WARN_USD}，推送告警")
        _notify_low_gas(eth_usd)
        return eth_usd > 5.0

    return True


def check_gas_before_trade() -> tuple[bool, str]:
    """
    交易前检查：ETH 余额需大于 GAS_RESERVE_USD + 单笔交易金额。
    至少保留 GAS_RESERVE_USD ($50) 的 ETH 不动用。
    """
    eth_qty, eth_usd = get_eth_balance_usd()
    if eth_usd < GAS_RESERVE_USD + 5:
        return False, f"ETH 余额 ${eth_usd:.2f} 不足（保留 ${GAS_RESERVE_USD} gas 后可用资金 < $5）"
    return True, f"ETH 余额充足 (${eth_usd:.2f}，保留 ${GAS_RESERVE_USD} 后可用 ${eth_usd-GAS_RESERVE_USD:.0f})"


def _notify_low_gas(eth_usd: float):
    try:
        msg = (
            f"⚠️ ETH 余额不足\n"
            f"当前: ${eth_usd:.2f}\n"
            f"需要: 至少 ${GAS_RESERVE_USD} 保留\n"
            f"请手动充值 ETH 到 Agent 钱包"
        )
        subprocess.Popen(
            ["openclaw", "message", "send", "--channel", "jarvis",
             "-t", JARVIS_TARGET, "-m", msg],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass
