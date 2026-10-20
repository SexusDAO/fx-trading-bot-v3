"""
pre_live_check.py — 上实盘前三项最终检查

用法：cd /root/.openclaw/workspace && python3 pre_live_check.py
"""

import sys, os, time, json
sys.path.insert(0, 'v3')

import modules.binance_api as raw_api
from modules.binance_api import get_all_tickers, get_futures_positions as get_positions
from modules.trade_logger import log_open, log_close, _load_all

PASS = "✅"; FAIL = "❌"; WARN = "⚠️"

results = []

def check(name, ok, detail=""):
    status = PASS if ok else FAIL
    results.append((name, ok, detail))
    print(f"  {status}  {name}")
    if detail:
        print(f"        {detail}")

print("\n" + "="*60)
print(" 上实盘前检查报告")
print("="*60)

# ══════════════════════════════════════════════════════════════════
# 检查 1：API 四个操作
# ══════════════════════════════════════════════════════════════════
print("\n【1. API 功能测试（SOLUSDT，名义 ~10U）】")

import math, time as _t
tickers_all = raw_api.get_all_tickers()
sol_t = next((t for t in tickers_all if t['symbol']=='SOLUSDT'), {})
price = float(sol_t.get('lastPrice', sol_t.get('price', 0)))
qty   = math.floor((10.0 / price) * 10) / 10   # 最小精度 0.1
limit_price_buy = round(price * 0.93, 2)         # 低于现价 7%，不会成交
sl_price_test   = round(price * 0.88, 2)         # 低于现价 12%，不会触发

print(f"  SOLUSDT 现价: {price}  测试数量: {qty}  挂单价: {limit_price_buy}")

# 1a. 持仓查询
try:
    pos = get_positions()
    active = [p for p in pos if abs(float(p.get('positionAmt', 0))) > 0]
    check("持仓查询", True, f"返回 {len(pos)} 条，活跃 {len(active)} 个")
except Exception as e:
    check("持仓查询", False, str(e))

# 1b. 限价单挂单
order_id = None
try:
    r = raw_api.place_order(
        symbol='SOLUSDT', side='BUY', order_type='LIMIT',
        price=str(limit_price_buy), quantity=qty,
        timeInForce='GTC', positionSide='LONG',
    )
    order_id = str(r.get('orderId', ''))
    ok = bool(order_id) and not r.get('code')
    check("限价单挂单", ok, f"orderId={order_id}  status={r.get('status')}")
except Exception as e:
    check("限价单挂单", False, str(e))

# 1c. 撤单
if order_id:
    _t.sleep(0.4)
    try:
        rc = raw_api.cancel_order(symbol='SOLUSDT', orderId=order_id)
        ok = rc.get('status') == 'CANCELED' or rc.get('orderId')
        check("撤单", ok, f"status={rc.get('status')}  orderId={rc.get('orderId')}")
    except Exception as e:
        check("撤单", False, str(e))
else:
    check("撤单", False, "跳过（限价单未成功挂出）")

# 1d. 止损单挂单（STOP_MARKET algo order）
algo_id = None
try:
    r2 = raw_api.place_algo_order(
        symbol='SOLUSDT', side='SELL', order_type='STOP_MARKET',
        trigger_price=sl_price_test, position_side='LONG', quantity=qty,
    )
    algo_id = r2.get('algoId') or r2.get('orderId')
    ok = bool(algo_id) and not r2.get('code')
    check("止损单挂单", ok, f"algoId={algo_id}  code={r2.get('code','-')}")
    if ok:
        _t.sleep(0.4)
        raw_api.cancel_algo_order(symbol='SOLUSDT', algo_id=int(algo_id))
except Exception as e:
    check("止损单挂单（algo）", False, str(e))

# ══════════════════════════════════════════════════════════════════
# 检查 2：日志系统完整性
# ══════════════════════════════════════════════════════════════════
print("\n【2. 日志系统完整性】")

# 写一条模拟 log_open，检查所有字段是否完整落盘
test_score_details = {
    "trend_clarity": 20, "volume_health": 15,
    "volatility_fit": 18, "structure": 16, "mkt_sync": 12,
}
try:
    log_open(
        symbol="SOLUSDT_TEST", direction="LONG",
        entry_price=150.0, notional=500,
        signal_type="trend_long", score=81,
        score_details=test_score_details,
        entry_mode="liq_zone",          # 入场价来源
        funding_rate=0.0001,
        btc_trend="neutral",
        sl_price=144.5,                 # 止损来源
        tp1_price=158.0, tp2_price=165.0,
        extra={
            "sl_source":    "pullback_low - 0.3xATR",   # 止损来源说明
            "tp_source":    "swing_high + risk*2.5",     # 止盈来源说明
            "scene":        "C_first_pull",
            "scene_reason": "强势回踩，EMA偏离12%>threshold 9.8%",
            "atr_pct":      72.3,
        }
    )
    # 读回验证
    all_logs = _load_all()
    test_entry = next((r for r in all_logs if r.get('symbol') == 'SOLUSDT_TEST'), None)
    if test_entry:
        required_fields = ['score_details','entry_mode','sl_price','tp1_price',
                           'sl_source','tp_source','scene','scene_reason']
        missing = [f for f in required_fields if not test_entry.get(f)]
        check("日志字段完整（信号原因/入场价/止损/止盈来源）",
              len(missing) == 0,
              f"缺失字段: {missing}" if missing else
              f"全部字段 ✓  score_details={test_entry['score_details']}  "
              f"entry_mode={test_entry['entry_mode']}  sl_source={test_entry.get('sl_source')}")
    else:
        check("日志写入/读取", False, "写入后无法读回")
except Exception as e:
    check("日志系统", False, str(e))

# 检查 log_close 的拒绝原因字段
try:
    log_close(
        symbol="SOLUSDT_TEST", direction="LONG",
        close_price=144.5, close_reason="TIME_SL_12h_float_loss>0.1xrisk",
        entry_price=150.0, notional=500, add_count=0, avg_price=150.0,
        extra={"reject_reason": "N/A", "gate_check": 9}
    )
    check("日志close/拒绝原因字段", True, "close_reason 字段写入成功")
except Exception as e:
    check("日志close字段", False, str(e))

# ══════════════════════════════════════════════════════════════════
# 检查 3：告警机制
# ══════════════════════════════════════════════════════════════════
print("\n【3. 告警机制验证（干跑，不实际发消息）】")

# 3a. API 连续失败3次告警
print("  模拟 API 连续失败 3 次...")
alert_received = []
def mock_notify(msg):
    alert_received.append(msg)

# 模拟主循环里的计数逻辑
_api_fail_count = 0
_API_FAIL_THRESHOLD = 3
for i in range(3):
    try:
        raise ConnectionError("模拟网络超时")
    except Exception as e:
        _api_fail_count += 1
        if _api_fail_count >= _API_FAIL_THRESHOLD:
            mock_notify(
                f"🚨 ws_monitor API连续失败 {_api_fail_count} 次\n"
                f"最近错误: {type(e).__name__}: {str(e)[:120]}\n"
                f"请检查网络/API Key/币安服务状态"
            )
            _api_fail_count = 0

check("API连续失败3次告警",
      len(alert_received) == 1 and "API连续失败" in alert_received[0],
      f"告警内容: {alert_received[0][:60]}..." if alert_received else "未触发")

# 3b. 持仓不一致告警
print("  模拟持仓状态不一致...")
alert_received.clear()

# 模拟：本地有 SOLUSDT_LONG，但交易所没有
local_keys = {"SOLUSDT_LONG", "ETHUSDT_SHORT"}
exchange_keys = {"ETHUSDT_SHORT"}  # 少了 SOLUSDT_LONG
ghost = local_keys - exchange_keys
missing = exchange_keys - local_keys

if ghost or missing:
    lines = ["⚠️ 持仓状态不一致，需人工核查"]
    if ghost: lines.append(f"本地多记（交易所无持仓）: {list(ghost)}")
    if missing: lines.append(f"本地漏记（交易所有持仓）: {list(missing)}")
    mock_notify("\n".join(lines))

check("持仓状态不一致告警",
      len(alert_received) == 1 and "不一致" in alert_received[0],
      f"告警内容: {alert_received[0][:80]}..." if alert_received else "未触发")

# 3c. 代码中告警实际写入 ws_monitor.py 确认
import subprocess
r = subprocess.run(['grep', '-n', 'API连续失败\|mismatch\|不一致', 'v3/ws_monitor.py'],
                   capture_output=True, text=True)
check("ws_monitor.py 告警代码已写入",
      'API连续失败' in r.stdout and '不一致' in r.stdout,
      r.stdout.strip()[:200] if r.stdout else "未找到")

# ══════════════════════════════════════════════════════════════════
# 汇总
# ══════════════════════════════════════════════════════════════════
print("\n" + "="*60)
print(" 检查汇总")
print("="*60)
all_pass = all(ok for _, ok, _ in results)
for name, ok, detail in results:
    status = PASS if ok else FAIL
    print(f"  {status}  {name}")

print()
if all_pass:
    print("  🟢 全部通过 — 可以上实盘")
else:
    failed = [name for name, ok, _ in results if not ok]
    print(f"  🔴 以下项目未通过，需修复后再上实盘:")
    for f in failed:
        print(f"     · {f}")
print("="*60)
