#!/usr/bin/env python3
"""
问题诊断 - 为什么交易频率异常高
"""

import json
import urllib.request
from datetime import datetime, timedelta

def fetch_klines(symbol, end_ts, limit=500):
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval=1h&endTime={end_ts}&limit={limit}"
    with urllib.request.urlopen(url, timeout=10) as response:
        data = json.loads(response.read())
        return [{'timestamp': k[0], 'close': float(k[4])} for k in data]

def main():
    symbol = 'BTCUSDT'
    end_ts = int(datetime.utcnow().timestamp() * 1000)
    
    klines = fetch_klines(symbol, end_ts, limit=500)
    
    print("="*80)
    print("问题诊断")
    print("="*80)
    
    # 检查时间范围
    first_time = datetime.utcfromtimestamp(klines[0]['timestamp']/1000)
    last_time = datetime.utcfromtimestamp(klines[-1]['timestamp']/1000)
    days = (last_time - first_time).total_seconds() / 86400
    
    print(f"\n📅 K 线时间范围：{first_time} 到 {last_time}")
    print(f"📊 K 线数量：{len(klines)} 根")
    print(f"📈 实际天数：{days:.1f} 天")
    
    # 7 天应该是多少根 K 线
    expected_hours = 7 * 24
    print(f"\n✅ 7 天应该有：{expected_hours} 根 K 线")
    print(f"⚠️ 实际有：{len(klines)} 根（多了{len(klines)-expected_hours}根）")
    
    # 正确的 7 天数据
    klines_7d = klines[-expected_hours:]
    print(f"\n📋 截取最后 7 天：{len(klines_7d)} 根 K 线")
    
    # 重新计算扫描次数
    scan_interval = 4  # 每 4 小时扫描
    # 从第 30 根开始（需要足够数据计算指标），到倒数第 20 根（留 20 根追踪盈亏）
    start_idx = 30
    end_idx = len(klines_7d) - 20
    scan_count = (end_idx - start_idx) // scan_interval + 1
    
    print(f"\n📍 扫描范围：索引{start_idx} 到 {end_idx}")
    print(f"📍 扫描次数：{scan_count} 次")
    print(f"   计算公式：({end_idx} - {start_idx}) ÷ {scan_interval} + 1 = {scan_count}")
    
    # 预期扫描次数
    expected_scans = (len(klines_7d) - 20 - 30) // 4
    print(f"\n✅ 预期扫描次数：约{expected_scans} 次（7 天÷4 小时≈42 次）")
    
    print(f"\n{'='*80}")
    print("问题根源")
    print("="*80)
    print("""
1. ❌ 之前脚本使用了 500 根 K 线（约 20 天），不是 7 天
   → 导致扫描次数翻倍（113 次 vs 42 次）

2. ❌ 扫描间隔逻辑有误
   → 代码写的是每 4 小时扫描，但实际遍历了所有 K 线

3. ❌ v2.2 评分门槛 70 分太容易达到
   → 基础分 50 分，稍微满足几个条件就超过 70 分
   → 信号率高达 57.5%

修正方案：
1. 严格限制为 7 天数据（168 根 K 线）
2. 扫描间隔改为每 6 小时或 12 小时（更接近实盘）
3. 或提高 v2.2 开仓门槛到 80 分
""")
    
    print(f"\n{'='*80}")
    print(f"诊断完成：{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")

if __name__ == '__main__':
    main()
