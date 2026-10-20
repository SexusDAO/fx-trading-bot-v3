#!/usr/bin/env python3
"""
诊断任务 1：BTC 止损 9 连败根因分析

逐笔分析 BTC 的 15 笔交易，输出详细数据
"""

import sys
import json
import requests
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))

from modules.dynamic_baseline import compute as compute_baseline
from modules.scene_matcher import match
from modules.trend_filter import judge_market_bias


def fetch_klines(symbol: str, start_date: str, end_date: str, interval: str = "1h") -> list:
    """获取历史 K 线"""
    print(f"📊 获取 {symbol} 历史 K 线（{start_date} ~ {end_date}）...")
    
    url = "https://fapi.binance.com/fapi/v1/klines"
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")
    days = (end - start).days + 1
    
    params = {"symbol": symbol, "interval": interval, "limit": days * 24}
    
    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        klines = []
        for k in data:
            klines.append([
                k[0], float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])
            ])
        
        print(f"✅ 获取成功：{len(klines)} 根 K 线")
        return klines
    except Exception as e:
        print(f"❌ 获取失败：{e}")
        return []


def fetch_4h_klines(symbol: str, days: int = 10) -> list:
    """获取 4H K 线（用于趋势判断）"""
    print(f"📊 获取 {symbol} 4H K 线（过去{days}天）...")
    
    url = "https://fapi.binance.com/fapi/v1/klines"
    params = {"symbol": symbol, "interval": "4h", "limit": days * 6}
    
    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        klines = [float(k[4]) for k in data]  # 只取收盘价
        print(f"✅ 获取成功：{len(klines)} 根 4H K 线")
        return klines
    except Exception as e:
        print(f"❌ 获取失败：{e}")
        return []


def calc_position_size_usd(second_score: int) -> int:
    """根据二次打分计算仓位大小"""
    if second_score < 70:
        return 0
    elif second_score <= 76:
        return 250
    elif second_score <= 83:
        return 400
    elif second_score <= 90:
        return 600
    elif second_score <= 96:
        return 800
    else:
        return 1000


class BTCDiagnoser:
    """BTC 诊断分析器"""
    
    def __init__(self, leverage: int = 5):
        self.leverage = leverage
        self.initial_capital = 10000
        self.capital = self.initial_capital
        
        # 交易记录（详细诊断数据）
        self.trades = []
        self.position = None
        self.trade_id = 0
    
    def run(self, klines: list, btc_4h: list, eth_4h: list):
        """运行诊断"""
        print(f"\n🔍 诊断 BTC 2026-07 交易...")
        
        window_size = 200
        
        for i in range(window_size, len(klines)):
            window = klines[i-window_size:i]
            current_k = klines[i]
            mark_price = current_k[4]
            
            bl = compute_baseline(window)
            
            # 获取市场趋势
            trend_bias = judge_market_bias(btc_4h, eth_4h, slope_threshold=0.005)
            
            # 扫描信号
            result_long = match(window, mark_price, "LONG", "oscillation", bl, "BTCUSDT",
                               enable_trend_filter=True, btc_closes_4h=btc_4h, eth_closes_4h=eth_4h)
            result_short = match(window, mark_price, "SHORT", "oscillation", bl, "BTCUSDT",
                                enable_trend_filter=True, btc_closes_4h=btc_4h, eth_closes_4h=eth_4h)
            
            # 开仓
            if not self.position:
                if result_long['matched'] and result_long['second_score'] >= 70:
                    self._open_position(result_long, current_k, i, trend_bias, bl)
                elif result_short['matched'] and result_short['second_score'] >= 70:
                    self._open_position(result_short, current_k, i, trend_bias, bl)
            
            # 检查仓位
            if self.position:
                self._check_position(current_k, i, bl)
        
        self._finalize()
    
    def _open_position(self, result: dict, kline: list, bar_index: int, trend_bias: str, bl):
        """开仓"""
        position_size_usd = calc_position_size_usd(result['second_score'])
        if position_size_usd == 0:
            return
        
        position_value = position_size_usd * self.leverage
        contract_size = position_value / result['entry_price']
        
        # 计算止损距离（ATR 倍数）
        sl_distance = abs(result['entry_price'] - result['sl_price'])
        sl_distance_atr_multiple = sl_distance / bl.atr_baseline if bl.atr_baseline > 0 else 0
        
        # 获取入场来源
        entry_source = "structure"
        conditions = result.get('conditions', {})
        if conditions.get('entry_method'):
            method = conditions['entry_method'].lower()
            if 'liq' in method:
                entry_source = "liq_zone"
            elif 'rr' in method:
                entry_source = "rr_fallback"
        
        self.position = {
            "trade_id": self.trade_id + 1,
            "direction": result['direction'],
            "entry": result['entry_price'],
            "sl": result['sl_price'],
            "tp": result['tp_price'],
            "size": contract_size,
            "scene": result['scene'],
            "second_score": result['second_score'],
            "open_bar_index": bar_index,
            "market_bias_at_entry": trend_bias,
            "entry_source": entry_source,
            "sl_source": "structure",  # 简化：默认 structure
            "sl_distance_atr_multiple": round(sl_distance_atr_multiple, 2),
        }
    
    def _check_position(self, kline: list, current_bar_index: int, bl):
        """检查仓位"""
        if not self.position:
            return
        
        pos = self.position
        high = kline[2]
        low = kline[3]
        
        exit_type = None
        
        if pos['direction'] == 'LONG':
            if low <= pos['sl']:
                pnl = (pos['sl'] - pos['entry']) * pos['size']
                exit_type = "sl"
                self._close_position(pnl, exit_type, kline, current_bar_index, bl)
            elif high >= pos['tp']:
                pnl = (pos['tp'] - pos['entry']) * pos['size']
                exit_type = "tp"
                self._close_position(pnl, exit_type, kline, current_bar_index, bl)
        else:
            if high >= pos['sl']:
                pnl = (pos['entry'] - pos['sl']) * pos['size']
                exit_type = "sl"
                self._close_position(pnl, exit_type, kline, current_bar_index, bl)
            elif low <= pos['tp']:
                pnl = (pos['entry'] - pos['tp']) * pos['size']
                exit_type = "tp"
                self._close_position(pnl, exit_type, kline, current_bar_index, bl)
    
    def _close_position(self, pnl: float, exit_type: str, kline: list, current_bar_index: int, bl):
        """平仓"""
        pos = self.position
        
        bars_held = current_bar_index - pos['open_bar_index']
        
        trade = {
            **pos,
            "exit_type": exit_type,
            "bars_held": bars_held,
            "pnl": round(pnl, 2),
        }
        self.trades.append(trade)
        self.trade_id += 1
        self.capital += pnl
        self.position = None
    
    def _finalize(self):
        """诊断结束"""
        if self.position:
            pass
    
    def print_report(self):
        """打印诊断报告"""
        print(f"\n{'='*70}")
        print(f"📊 BTC 诊断报告 - 15 笔交易详细分析")
        print(f"{'='*70}")
        
        # 详细交易记录
        print(f"\n【逐笔交易数据】")
        for trade in self.trades:
            print(f"\nTrade {trade['trade_id']:2d}: {trade['direction']:<5} {trade['scene']:<15} | "
                  f"打分={trade['second_score']:<3} 趋势={trade['market_bias_at_entry']:<10} | "
                  f"入场={trade['entry_source']:<12} SL 距离={trade['sl_distance_atr_multiple']:.2f}xATR | "
                  f"持仓={trade['bars_held']:3d}K 线 退出={trade['exit_type']:<8} PnL=${trade['pnl']:>8.2f}")
        
        # 统计
        total = len(self.trades)
        long_count = sum(1 for t in self.trades if t['direction'] == 'LONG')
        short_count = sum(1 for t in self.trades if t['direction'] == 'SHORT')
        
        print(f"\n【多空分布】")
        print(f"  多头：{long_count}笔 ({long_count/total*100:.0f}%)")
        print(f"  空头：{short_count}笔 ({short_count/total*100:.0f}%)")
        
        # 按市场趋势分组
        neutral_trades = [t for t in self.trades if t['market_bias_at_entry'] == 'NEUTRAL']
        bullish_trades = [t for t in self.trades if t['market_bias_at_entry'] == 'BULLISH']
        bearish_trades = [t for t in self.trades if t['market_bias_at_entry'] == 'BEARISH']
        
        print(f"\n【按市场趋势分组】")
        print(f"  NEUTRAL: {len(neutral_trades)}笔 (多头{sum(1 for t in neutral_trades if t['direction']=='LONG')} 空头{sum(1 for t in neutral_trades if t['direction']=='SHORT')})")
        print(f"  BULLISH: {len(bullish_trades)}笔")
        print(f"  BEARISH: {len(bearish_trades)}笔")
        
        # SL 距离分布
        sl_distances = [t['sl_distance_atr_multiple'] for t in self.trades]
        print(f"\n【SL 距离 ATR 倍数分布】")
        print(f"  最小值：{min(sl_distances):.2f}x")
        print(f"  最大值：{max(sl_distances):.2f}x")
        print(f"  平均值：{sum(sl_distances)/len(sl_distances):.2f}x")
        print(f"  中位数：{sorted(sl_distances)[len(sl_distances)//2]:.2f}x")
        
        # 退出类型分布
        tp_count = sum(1 for t in self.trades if t['exit_type'] == 'tp')
        sl_count = sum(1 for t in self.trades if t['exit_type'] == 'sl')
        time_sl_count = sum(1 for t in self.trades if t['exit_type'] == 'time_sl')
        
        print(f"\n【退出类型分布】")
        print(f"  TP: {tp_count}笔 ({tp_count/total*100:.0f}%)")
        print(f"  SL: {sl_count}笔 ({sl_count/total*100:.0f}%)")
        print(f"  Time SL: {time_sl_count}笔 ({time_sl_count/total*100:.0f}%)")
        
        # 导出 JSON
        output_file = Path(__file__).parent / "btc_diagnosis.json"
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump({
                "trades": self.trades,
                "summary": {
                    "total": total,
                    "long_count": long_count,
                    "short_count": short_count,
                    "neutral_count": len(neutral_trades),
                    "bullish_count": len(bullish_trades),
                    "bearish_count": len(bearish_trades),
                    "sl_distance_stats": {
                        "min": min(sl_distances),
                        "max": max(sl_distances),
                        "avg": sum(sl_distances)/len(sl_distances),
                        "median": sorted(sl_distances)[len(sl_distances)//2]
                    },
                    "exit_type_stats": {
                        "tp": tp_count,
                        "sl": sl_count,
                        "time_sl": time_sl_count
                    }
                }
            }, f, ensure_ascii=False, indent=2)
        print(f"\n✅ 详细数据已导出：{output_file}")


def main():
    """主函数"""
    print("="*70)
    print("🧪 诊断任务 1：BTC 9 连败根因分析")
    print("="*70)
    
    # BTC 2026-07
    btc_klines = fetch_klines("BTCUSDT", "2026-07-01", "2026-07-31", "1h")
    btc_4h = fetch_4h_klines("BTCUSDT", days=10)
    eth_4h = fetch_4h_klines("ETHUSDT", days=10)
    
    if not btc_klines:
        print("❌ 无法获取 BTC 数据，退出诊断")
        return
    
    diagnoser = BTCDiagnoser(leverage=5)
    diagnoser.run(btc_klines, btc_4h, eth_4h)
    diagnoser.print_report()


if __name__ == "__main__":
    main()
