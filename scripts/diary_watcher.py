#!/usr/bin/env python3
"""
diary_watcher.py — 监控 DIARY 价格，翻倍时自动卖出全部持仓
触发条件：DIARY 当前均价 >= 买入成本均价 × 2
买入记录：83,805 @ ~$0.000199（原仓） + 193,203 @ ~$0.000265（本次）
合计约 277,008 DIARY，加权成本约 $0.000241
翻倍目标：$0.000482
"""
import sys, json, subprocess, datetime
sys.path.insert(0, "/root/.openclaw/workspace")

NOTIFY_TO = "432401404:thread:019fadbf-37d4-710e-bda4-d7816642e957"
DIARY_ADDR = "9MEYExzYucFLtAk2v8PCdw4SM4xox5o4gPZEywyvpump"
SOL_ADDR   = "So11111111111111111111111111111111111111111"
CHAIN      = "CT_501"

# 加权成本：(83805 × 0.000199 + 193203 × 0.000265) / 277008 ≈ 0.000241
# 翻倍 = 成本 × 2
COST_BASIS   = 0.000241
TARGET_PRICE = COST_BASIS * 2   # $0.000482

def notify(msg):
    print(f"__NOTIFY__:{msg}")

def log(msg):
    ts = datetime.datetime.utcnow().strftime('%H:%M UTC')
    print(f"[{ts}] {msg}")

# 1. 读取当前 DIARY 余额和价格
try:
    r = subprocess.run(["baw", "wallet", "balance", "--json"],
                       capture_output=True, text=True, timeout=15)
    data = json.loads(r.stdout)
    balances = data.get("data", [])
    diary_entry = next((b for b in balances if b.get("address") == DIARY_ADDR), None)

    if not diary_entry:
        log("钱包中未找到 DIARY，可能已卖出或未持仓")
        sys.exit(0)

    current_price = float(diary_entry.get("price", 0))
    diary_balance = float(diary_entry.get("balance", 0))
    usd_value     = float(diary_entry.get("value", 0))

    log(f"DIARY: {diary_balance:.0f} 枚 @ ${current_price:.8f} = ${usd_value:.2f}")
    log(f"目标价: ${TARGET_PRICE:.8f}（当前进度 {current_price/TARGET_PRICE*100:.1f}%）")

    if diary_balance < 1:
        log("持仓为 0，退出监控")
        sys.exit(0)

except Exception as e:
    log(f"读取余额异常: {e}")
    notify(f"⚠️ DIARY 监控异常：{e}")
    sys.exit(1)

# 2. 判断是否触发翻倍卖出
if current_price < TARGET_PRICE:
    log(f"未达目标，继续等待（还差 {(TARGET_PRICE/current_price-1)*100:.1f}%）")
    sys.exit(0)

# 3. 触发！执行卖出
log(f"🎯 价格触发！{current_price:.8f} >= 目标 {TARGET_PRICE:.8f}，执行卖出")

try:
    sell_qty = f"{diary_balance:.6f}"
    result = subprocess.run([
        "baw", "market-order", "swap",
        "--fromTokenQty", sell_qty,
        "--fromToken", DIARY_ADDR,
        "--toToken", SOL_ADDR,
        "--binanceChainId", CHAIN,
        "--json"
    ], capture_output=True, text=True, timeout=30)

    resp = json.loads(result.stdout)
    if resp.get("success"):
        order_id = resp["data"]["orderId"]
        notify(
            f"✅ **DIARY 翻倍出本，已自动卖出！**\n\n"
            f"卖出数量：{diary_balance:.0f} DIARY\n"
            f"触发价格：${current_price:.8f}（目标 ${TARGET_PRICE:.8f}）\n"
            f"预计收回：~${usd_value:.2f} 等值 SOL\n"
            f"订单ID：{order_id}"
        )
        log(f"卖出成功，orderId={order_id}")
    else:
        err = resp.get("error", {}).get("message", str(resp))
        notify(f"⚠️ DIARY 卖出失败：{err}")
        log(f"卖出失败: {err}")
        sys.exit(1)

except Exception as e:
    notify(f"⚠️ DIARY 卖出异常：{e}")
    log(f"卖出异常: {e}")
    sys.exit(1)
