#!/usr/bin/env python3
"""
失败交易分析脚本

分析内容：
1. 入场后多少根 K 线内止损？
2. 止损时价格运动方向？
3. 入场价来源（liq_zone vs structure）
4. 失败单子入场价来源分布
"""

import sys
import json
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))


def load_trades(trade_file: str) -> list:
    """加载交易记录"""
    path = Path(__file__).parent / trade_file
    
    if not path.exists():
        print(f"❌ 文件不存在：{path}")
        return []
    
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    return data.get('trades', [])


def load_klines(symbol: str, days: int = 30) -> list:
    """加载 K 线数据"""
    import requests
    
    if symbol == "ETH":
        binance_symbol = "ETHUSDT"
    elif symbol == "PEPE":
        binance_symbol = "1000PEPEUSDT"
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


def analyze_failed_trades(trades: list, klines: list):
    """分析失败交易"""
    print("=" * 70)
    print("🔍 失败交易深度分析")
    print("=" * 70)
    
    # 筛选止损交易
    sl_trades = [t for t in trades if t['reason'] == 'SL']
    
    if not sl_trades:
        print("❌ 没有止损交易")
        return
    
    print(f"\n【概览】")
    print(f"  总交易数：{len(trades)}")
    print(f"  止损交易：{len(sl_trades)} ({len(sl_trades)/len(trades)*100:.1f}%)")
    print(f"  止盈交易：{len([t for t in trades if t['reason'] == 'TP'])}")
    
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
        print(f"  止损：{trade['sl']:.6f}")
        print(f"  盈亏：${trade['pnl']:.2f}")
        print(f"  时间：{datetime.fromtimestamp(trade['close_time']/1000)}")
        
        # 查找入场 K 线索引
        entry_time = trade.get('open_time', trade['close_time'])
        entry_idx = None
        for j, k in enumerate(klines):
            if abs(k[0] - entry_time) < 3600000:  # 1 小时误差
                entry_idx = j
                break
        
        if entry_idx is None:
            print(f"  ⚠️ 未找到入场 K 线")
            continue
        
        # 查找止损 K 线索引
        sl_idx = None
        for j in range(entry_idx, min(entry_idx + 50, len(klines))):
            k = klines[j]
            if trade['direction'] == 'LONG':
                if k[3] <= trade['sl']:  # low <= sl
                    sl_idx = j
                    break
            else:
                if k[2] >= trade['sl']:  # high >= sl
                    sl_idx = j
                    break
        
        if sl_idx:
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
        if sl_idx and sl_idx + 5 < len(klines):
            sl_price = trade['sl']
            price_after_5 = klines[sl_idx + 5][4]  # 5 根 K 线后的收盘价
            
            if trade['direction'] == 'LONG':
                # 多头止损后，价格上涨 = 立即反转
                if price_after_5 > sl_price * 1.01:
                    directions_after_entry["立即反转"] += 1
                    print(f"  🔄 止损后价格反转：+{(price_after_5/sl_price-1)*100:.2f}%")
                else:
                    directions_after_entry["继续不利"] += 1
                    print(f"  ⬇️ 止损后继续下跌：{(price_after_5/sl_price-1)*100:.2f}%")
            else:
                # 空头止损后，价格下跌 = 立即反转
                if price_after_5 < sl_price * 0.99:
                    directions_after_entry["立即反转"] += 1
                    print(f"  🔄 止损后价格反转：-{(sl_price/price_after_5-1)*100:.2f}%")
                else:
                    directions_after_entry["继续不利"] += 1
                    print(f"  ⬆️ 止损后继续上涨：{(price_after_5/sl_price-1)*100:.2f}%")
        
        # 入场价来源（需要查看回测日志或添加字段）
        # 暂时标记为 unknown
        entry_source = trade.get('entry_source', 'unknown')
        entry_source_stats[entry_source] = entry_source_stats.get(entry_source, 0) + 1
        print(f"  📍 入场来源：{entry_source}")
    
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
    for direction, count in directions_after_entry.items():
        if count > 0:
            print(f"  {direction}: {count} 笔 ({count/total_dir*100:.1f}%)")
    
    print(f"\n【入场价来源分布】")
    total_source = sum(entry_source_stats.values())
    for source, count in entry_source_stats.items():
        if count > 0:
            print(f"  {source}: {count} 笔 ({count/total_source*100:.1f}%)")
    
    # 保存分析结果
    output_file = Path(__file__).parent / "failed_trades_analysis.json"
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump({
            "summary": {
                "total_trades": len(trades),
                "sl_trades": len(sl_trades),
                "avg_candles_to_sl": sum(candles_to_sl) / len(candles_to_sl) if candles_to_sl else 0,
                "candles_distribution": {
                    "quick": len([c for c in candles_to_sl if c <= 3]),
                    "normal": len([c for c in candles_to_sl if 4 <= c <= 10]),
                    "slow": len([c for c in candles_to_sl if c > 10]),
                },
                "direction_after_sl": directions_after_entry,
                "entry_source": entry_source_stats,
            },
            "trades": sl_trades,
        }, f, indent=2, ensure_ascii=False)
    
    print(f"\n💾 分析结果已保存：{output_file}")


def main():
    print("📊 任务 1：ETH 失败交易分析")
    print("=" * 70)
    
    # 加载 ETH 交易记录
    trades = load_trades("eth_backtest_v3_liquidation_trades.json")
    
    if not trades:
        print("❌ 无交易记录")
        return
    
    # 加载 K 线数据
    print("\n📈 加载 ETH K 线数据...")
    klines = load_klines("ETH", days=30)
    
    if not klines:
        print("❌ 无 K 线数据")
        return
    
    print(f"✅ 加载 {len(klines)} 根 K 线")
    
    # 分析失败交易
    analyze_failed_trades(trades, klines)


if __name__ == "__main__":
    main()
