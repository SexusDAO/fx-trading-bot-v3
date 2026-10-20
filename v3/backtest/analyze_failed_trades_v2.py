#!/usr/bin/env python3
"""
失败交易分析脚本 V2

从日志中解析止损价和入场来源
"""

import sys
import json
import re
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))


def load_trades_with_logs(trade_file: str) -> dict:
    """加载交易记录和日志"""
    path = Path(__file__).parent / trade_file
    
    if not path.exists():
        return None
    
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    return data


def parse_sl_from_logs(logs: list, entry_price: float, direction: str) -> float:
    """从日志中解析止损价"""
    for log in logs:
        if "挂单" in log and f"{entry_price:.4f}" in log:
            # 提取 SL=xxx
            match = re.search(r'SL=([0-9.]+)', log)
            if match:
                return float(match.group(1))
    return 0.0


def parse_entry_source_from_logs(logs: list, entry_price: float) -> str:
    """从日志中解析入场来源"""
    for log in logs:
        if "挂单" in log and f"{entry_price:.4f}" in log:
            if "tp_source=liq_zone" in log:
                return "liq_zone"
            elif "tp_source=structure" in log:
                return "structure"
            else:
                return "unknown"
    return "unknown"


def analyze_failed_trades(data: dict, klines: list):
    """分析失败交易"""
    print("=" * 70)
    print("🔍 失败交易深度分析")
    print("=" * 70)
    
    trades = data.get('trades', [])
    logs = data.get('logs', [])
    
    # 筛选止损交易
    sl_trades = [t for t in trades if t['reason'] == 'SL']
    tp_trades = [t for t in trades if t['reason'] == 'TP']
    
    if not sl_trades:
        print("❌ 没有止损交易")
        return
    
    print(f"\n【概览】")
    print(f"  总交易数：{len(trades)}")
    print(f"  止损交易：{len(sl_trades)} ({len(sl_trades)/len(trades)*100:.1f}%)")
    print(f"  止盈交易：{len(tp_trades)} ({len(tp_trades)/len(trades)*100:.1f}%)")
    
    print(f"\n【逐笔分析】")
    
    entry_source_stats = {"liq_zone": 0, "structure": 0, "unknown": 0}
    candles_to_sl = []
    directions_after_entry = {"继续不利": 0, "立即反转": 0, "震荡后止损": 0}
    
    for i, trade in enumerate(sl_trades, 1):
        print(f"\n--- 交易 #{i} ---")
        print(f"  方向：{trade['direction']}")
        print(f"  打分：{trade['second_score']}")
        print(f"  仓位：${trade['position_size_usd']}")
        print(f"  入场：{trade['entry']:.6f}")
        print(f"  盈亏：${trade['pnl']:.2f}")
        print(f"  时间：{datetime.fromtimestamp(trade['close_time']/1000)}")
        
        # 从日志解析止损价
        sl_price = parse_sl_from_logs(logs, trade['entry'], trade['direction'])
        if sl_price > 0:
            print(f"  止损：{sl_price:.6f}")
        
        # 入场来源
        entry_source = parse_entry_source_from_logs(logs, trade['entry'])
        entry_source_stats[entry_source] += 1
        print(f"  📍 入场来源：{entry_source}")
        
        # 查找入场 K 线索引
        entry_time = trade.get('close_time') - 3600000  # 近似
        entry_idx = None
        for j, k in enumerate(klines):
            if abs(k[0] - trade['close_time']) < 7200000:  # 2 小时误差
                # 找到接近出场时间的 K 线，往前找入场
                for back_j in range(max(0, j-20), j):
                    if abs(klines[back_j][4] - trade['entry']) < trade['entry'] * 0.01:  # 1% 误差
                        entry_idx = back_j
                        break
                if entry_idx:
                    sl_idx = j
                    break
        
        if entry_idx and sl_idx:
            candles_held = sl_idx - entry_idx
            candles_to_sl.append(candles_held)
            print(f"  📊 持仓：{candles_held} 根 K 线")
            
            if candles_held <= 3:
                print(f"     → 快速止损（≤3 根 K 线）")
            elif candles_held <= 10:
                print(f"     → 正常止损（4-10 根 K 线）")
            else:
                print(f"     → 持久止损（>10 根 K 线）")
            
            # 分析止损后价格运动
            if sl_idx + 5 < len(klines):
                sl_price_current = klines[sl_idx][4]
                price_after_5 = klines[sl_idx + 5][4]
                
                if trade['direction'] == 'LONG':
                    if price_after_5 > sl_price_current * 1.01:
                        directions_after_entry["立即反转"] += 1
                        change = (price_after_5/sl_price_current-1)*100
                        print(f"  🔄 止损后价格反转：+{change:.2f}%")
                    else:
                        directions_after_entry["继续不利"] += 1
                        change = (price_after_5/sl_price_current-1)*100
                        print(f"  ⬇️ 止损后继续下跌：{change:.2f}%")
                else:
                    if price_after_5 < sl_price_current * 0.99:
                        directions_after_entry["立即反转"] += 1
                        change = (sl_price_current/price_after_5-1)*100
                        print(f"  🔄 止损后价格反转：-{change:.2f}%")
                    else:
                        directions_after_entry["继续不利"] += 1
                        change = (price_after_5/sl_price_current-1)*100
                        print(f"  ⬆️ 止损后继续上涨：{change:.2f}%")
        else:
            print(f"  ⚠️ 无法定位 K 线索引")
    
    # 统计汇总
    print(f"\n" + "=" * 70)
    print("【统计汇总】")
    print("=" * 70)
    
    print(f"\n【持仓 K 线数分布】")
    if candles_to_sl:
        avg_candles = sum(candles_to_sl) / len(candles_to_sl)
        print(f"  平均持仓：{avg_candles:.1f} 根 K 线")
        print(f"  最短：{min(candles_to_sl)} 根")
        print(f"  最长：{max(candles_to_sl)} 根")
        
        quick_sl = len([c for c in candles_to_sl if c <= 3])
        normal_sl = len([c for c in candles_to_sl if 4 <= c <= 10])
        slow_sl = len([c for c in candles_to_sl if c > 10])
        
        print(f"  快速止损（≤3 根）: {quick_sl} 笔 ({quick_sl/len(candles_to_sl)*100:.1f}%)")
        print(f"  正常止损（4-10 根）: {normal_sl} 笔 ({normal_sl/len(candles_to_sl)*100:.1f}%)")
        print(f"  持久止损（>10 根）: {slow_sl} 笔 ({slow_sl/len(candles_to_sl)*100:.1f}%)")
    
    print(f"\n【止损后价格方向】")
    total_dir = sum(directions_after_entry.values())
    if total_dir > 0:
        for direction, count in directions_after_entry.items():
            if count > 0:
                print(f"  {direction}: {count} 笔 ({count/total_dir*100:.1f}%)")
    else:
        print("  无数据")
    
    print(f"\n【入场价来源分布】")
    total_source = sum(entry_source_stats.values())
    for source, count in entry_source_stats.items():
        if count > 0:
            print(f"  {source}: {count} 笔 ({count/total_source*100:.1f}%)")
    
    # 保存分析结果
    output_file = Path(__file__).parent / "failed_trades_analysis_v2.json"
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump({
            "summary": {
                "total_trades": len(trades),
                "sl_trades": len(sl_trades),
                "tp_trades": len(tp_trades),
                "avg_candles_to_sl": sum(candles_to_sl) / len(candles_to_sl) if candles_to_sl else 0,
                "candles_distribution": {
                    "quick": len([c for c in candles_to_sl if c <= 3]),
                    "normal": len([c for c in candles_to_sl if 4 <= c <= 10]),
                    "slow": len([c for c in candles_to_sl if c > 10]),
                },
                "direction_after_sl": directions_after_entry,
                "entry_source": entry_source_stats,
            },
            "sl_trades": sl_trades,
        }, f, indent=2, ensure_ascii=False)
    
    print(f"\n💾 分析结果已保存：{output_file}")


def load_klines(symbol: str, days: int = 30) -> list:
    """加载 K 线数据"""
    import requests
    
    if symbol == "ETH":
        binance_symbol = "ETHUSDT"
    else:
        binance_symbol = f"{symbol}USDT"
    
    url = "https://fapi.binance.com/fapi/v1/klines"
    params = {"symbol": binance_symbol, "interval": "1h", "limit": days * 24}
    
    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        klines = []
        for k in data:
            klines.append([
                k[0], float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])
            ])
        
        return klines
    except Exception as e:
        print(f"❌ 获取 K 线失败：{e}")
        return []


def main():
    print("📊 任务 1：ETH 失败交易分析")
    print("=" * 70)
    
    # 加载 ETH 交易记录
    data = load_trades_with_logs("eth_backtest_v3_liquidation_trades.json")
    
    if not data:
        print("❌ 无交易记录")
        return
    
    trades = data.get('trades', [])
    print(f"\n✅ 加载 {len(trades)} 笔交易")
    
    # 加载 K 线数据
    print("\n📈 加载 ETH K 线数据...")
    klines = load_klines("ETH", days=30)
    
    if not klines:
        print("❌ 无 K 线数据")
        return
    
    print(f"✅ 加载 {len(klines)} 根 K 线")
    
    # 分析失败交易
    analyze_failed_trades(data, klines)


if __name__ == "__main__":
    main()
