#!/usr/bin/env python3
"""
diary_limit_monitor.py
监控 DIARY 限价买单（strategyId=10652738）是否成交。
成交后：
  1. 读取钱包实际 DIARY 余额和当前价格
  2. 撤销旧卖单（strategyId=10652369）
  3. 重新核算加权成本（原仓 277008.93 @ $0.000241 + 新买数量 @ $0.00018）
  4. 挂新翻倍卖单（总持仓，触发价=新成本×2）
  5. 成功后推送通知，自删 cron（通过退出码 0 + 标记文件）
"""
import sys, os, json, subprocess, datetime
sys.path.insert(0, "/root/.openclaw/workspace")

NOTIFY_TO      = "432401404:thread:019fadbf-37d4-710e-bda4-d7816642e957"
DIARY_ADDR     = "9MEYExzYucFLtAk2v8PCdw4SM4xox5o4gPZEywyvpump"
SOL_ADDR       = "So11111111111111111111111111111111111111111"
CHAIN          = "CT_501"
BUY_STRATEGY   = "10652738"   # 限价买单
SELL_STRATEGY  = "10652369"   # 当前卖单
DONE_FLAG      = "/root/.openclaw/workspace/ws_cache/diary_monitor_done.flag"

# 原有仓位成本
EXISTING_QTY   = 277008.93
EXISTING_COST  = 0.000241     # 加权均价

def notify(msg):
    print(f"__NOTIFY__:{msg}")

def log(msg):
    ts = datetime.datetime.utcnow().strftime('%H:%M UTC')
    print(f"[{ts}] {msg}")

def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    try:
        return json.loads(r.stdout)
    except:
        return {"success": False, "raw": r.stdout, "err": r.stderr}

# 已完成则静默退出
if os.path.exists(DONE_FLAG):
    sys.exit(0)

# ── 1. 检查买单状态 ──
resp = run(["baw", "limit-order", "list", "--strategyId", BUY_STRATEGY, "--json"])
if not resp.get("success"):
    log(f"查询买单失败: {resp}")
    sys.exit(0)

orders = resp.get("data", {}).get("list", [])
if not orders:
    log("买单不存在，可能已过期或取消")
    sys.exit(0)

order = orders[0]
status = order.get("status", "")
log(f"买单状态: {status}")

if status != "FINISHED":
    # 未成交，静默等待
    sys.exit(0)

# ── 2. 买单已成交 ──
log("✅ 买单已成交，开始重新核算成本...")

# 读取实际 DIARY 余额
bal_resp = run(["baw", "wallet", "balance", "--json"])
balances = bal_resp.get("data", [])
diary_entry = next((b for b in balances if b.get("address") == DIARY_ADDR), None)

if not diary_entry:
    notify("⚠️ DIARY 买单成交但读取余额失败，请手动检查")
    sys.exit(1)

total_qty   = float(diary_entry.get("balance", 0))
cur_price   = float(diary_entry.get("price", 0))
usd_value   = float(diary_entry.get("value", 0))

# ── 3. 重新核算加权成本 ──
new_qty      = total_qty - EXISTING_QTY            # 本次买入数量
new_cost_usd = new_qty * 0.00018                   # 按触发价估算
new_weighted_cost = (
    (EXISTING_QTY * EXISTING_COST + new_qty * 0.00018) / total_qty
    if total_qty > 0 else 0.00018
)
new_target = round(new_weighted_cost * 2, 9)

log(f"总持仓: {total_qty:.2f} DIARY")
log(f"新加仓: ~{new_qty:.2f} DIARY @ $0.00018")
log(f"新加权成本: ${new_weighted_cost:.9f}")
log(f"新翻倍目标: ${new_target:.9f}")

# ── 4. 撤销旧卖单 ──
cancel_resp = run(["baw", "limit-order", "cancel", "--strategyId", SELL_STRATEGY, "--json"])
if cancel_resp.get("success"):
    log(f"旧卖单 {SELL_STRATEGY} 已撤销")
else:
    err = cancel_resp.get("error", {}).get("message", str(cancel_resp))
    # 可能已触发/执行，继续
    log(f"撤销旧卖单返回: {err}（可能已成交，继续挂新单）")

# ── 5. 挂新翻倍卖单 ──
sell_qty = f"{total_qty:.2f}"
new_sell = run([
    "baw", "limit-order", "sell",
    "--triggerPrice", str(new_target),
    "--fromTokenQty", sell_qty,
    "--fromToken", DIARY_ADDR,
    "--toToken", SOL_ADDR,
    "--binanceChainId", CHAIN,
    "--json"
])

if new_sell.get("success"):
    new_strategy_id = new_sell["data"]["strategyId"]
    log(f"新卖单挂成功: strategyId={new_strategy_id}")

    # 写完成标记，下次不再执行
    os.makedirs(os.path.dirname(DONE_FLAG), exist_ok=True)
    with open(DONE_FLAG, "w") as f:
        json.dump({"done": True, "new_sell_strategy": new_strategy_id}, f)

    notify(
        f"✅ **DIARY 限价买单成交，已重新挂翻倍卖单**\n\n"
        f"新买入：~{new_qty:.0f} DIARY @ $0.00018\n"
        f"总持仓：{total_qty:.0f} DIARY（~${usd_value:.2f}）\n"
        f"新加权成本：${new_weighted_cost:.8f}\n"
        f"新翻倍目标：**${new_target:.8f}**\n"
        f"新卖单ID：{new_strategy_id}\n"
        f"监控已自动停止 ✓"
    )
else:
    err = new_sell.get("error", {}).get("message", str(new_sell))
    notify(f"⚠️ DIARY 新卖单挂单失败：{err}\n请手动在 ${new_target:.8f} 挂卖单，总量 {total_qty:.0f} DIARY")
    sys.exit(1)
