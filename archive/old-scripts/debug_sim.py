#!/usr/bin/env python3
"""
调试 - 检查单笔交易的止盈止损逻辑
"""

import json
import urllib.request
from datetime import datetime

def fetch_klines(symbol, start_ts, end_ts):
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval=1h&startTime={start_ts}&endTime={end_ts}&limit=1000"
    with urllib.request.urlopen(url, timeout=10) as response:
        data = json.loads(response.read())
        return [{'timestamp': k[0], 'open': float(k[1]), 'high': float(k[2]), 'low': float(k[3]), 'close': float(k[4])} for k in data]

def calculate_atr(klines, period=14):
    if len(klines) < period:
        return klines[-1]['close'] * 0.05
    tr = []
    for i in range(1, len(klines)):
        high_low = klines[i]['high'] - klines[i]['low']
        high_close = abs(klines[i]['high'] - klines[i-1]['close'])
        low_close = abs(klines[i]['low'] - klines[i-1]['close'])
        tr.append(max(high_low, high_close, low_close))
    return sum(tr[-period:]) / period if tr else 0.05

# 获取 BTC 数据
start_ts = int(datetime(2026, 7, 29, 0, 0, 0).timestamp() * 1000)
end_ts = int(datetime(2026, 8, 2, 23, 59, 59).timestamp() * 1000)
klines = fetch_klines('BTCUSDT', start_ts, end_ts)

print("="*80)
print("调试：检查 07-29 12:00 这笔交易")
print("="*80)

# 找到 07-29 12:00 的 K 线索引
for i, k in enumerate(klines):
    t = datetime.utcfromtimestamp(k['timestamp']/1000)
    if t.day == 29 and t.hour == 12:
        print(f"\n找到目标 K 线：索引{i}, 时间{t}, 收盘价${k['close']:.2f}")
        
        # 计算 ATR
        snapshot = klines[:i+1]
        atr = calculate_atr(snapshot)
        print(f"ATR: ${atr:.2f}")
        
        entry = k['close'] * 0.99
        sl = entry - atr * 1.5
        tp1 = entry + atr * 2
        tp2 = entry + atr * 4
        
        print(f"\n开仓价：${entry:.2f}")
        print(f"止损价：${sl:.2f} ({(sl-entry)/entry*100:.2f}%)")
        print(f"TP1:   ${tp1:.2f} ({(tp1-entry)/entry*100:.2f}%)")
        print(f"TP2:   ${tp2:.2f} ({(tp2-entry)/entry*100:.2f}%)")
        
        # 检查后续 K 线
        future = klines[i+1:i+21]
        print(f"\n后续{len(future)}根 K 线:")
        
        for j, fk in enumerate(future):
            ft = datetime.utcfromtimestamp(fk['timestamp']/1000)
            
            # 检查是否触发
            if fk['low'] <= sl:
                print(f"  [{j}] {ft}: LOW=${fk['low']:.2f} <= SL${sl:.2f} → 触发止损")
                pnl = 220 * (sl - entry) / entry
                print(f"      盈亏：${pnl:.2f}U")
                break
            elif fk['high'] >= tp1:
                print(f"  [{j}] {ft}: HIGH=${fk['high']:.2f} >= TP1${tp1:.2f} → 触发 TP1")
                if fk['high'] >= tp2:
                    print(f"      HIGH=${fk['high']:.2f} >= TP2${tp2:.2f} → 同时触发 TP2")
                    pnl = 220 * (tp2 - entry) / entry
                    print(f"      盈亏：${pnl:.2f}U")
                    break
                else:
                    print(f"      但未触发 TP2，继续...")
        
        break

print(f"\n{'='*80}")
print("问题诊断")
print("="*80)
print("""
可能的问题：
1. ATR 太小 → 止盈止损距离太近，容易被触发
2. 模拟逻辑：先检查止损还是先检查止盈？
3. 99% 胜率说明几乎所有交易都盈利，这不符合真实市场

检查点：
- BTC 正常波动：每小时±0.5-2%
- ATR 应该反映真实波动
- 止盈止损应该合理分布
""")
