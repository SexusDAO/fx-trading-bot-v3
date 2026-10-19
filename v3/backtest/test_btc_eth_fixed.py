#!/usr/bin/env python3
"""
BTC 2026-07 + ETH 2024-01 回测（修复后版本）

重点验证：
① 止损距离最小值是否≥0.5×ATR
② market_bias 判断是否合理
③ 连续止损次数是否从 9 次降下来
④ ETH 场景 C 是否开始触发
"""

import sys
import json
import requests
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))

from modules.dynamic_baseline import compute as compute_baseline
from modules.scene_matcher import match
from modules.trend_filter import judge_market_bias_stable


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
    """获取 4H K 线"""
    print(f"📊 获取 {symbol} 4H K 线（过去{days}天）...")
    
    url = "https://fapi.binance.com/fapi/v1/klines"
    params = {"symbol": symbol, "interval": "4h", "limit": days * 6}
    
    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        klines = [float(k[4]) for k in data]
        print(f"✅ 获取成功：{len(klines)} 根 4H K 线")
        return klines
    except Exception as e:
        print(f"❌ 获取失败：{e}")
        return []


def calc_position_size_usd(second_score: int) -> int:
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


class FixedBacktest:
    def __init__(self, symbol: str, leverage: int = 5):
        self.symbol = symbol
        self.leverage = leverage
        self.initial_capital = 10000
        self.capital = self.initial_capital
        
        self.trades = []
        self.position = None
        self.scene_stats = {}
        self.consecutive_losses = 0
        self.max_consecutive_losses = 0
    
    def run(self, klines: list, btc_4h: list, eth_4h: list):
        print(f"\n🚀 回测 {self.symbol}...")
        
        window_size = 200
        
        for i in range(window_size, len(klines)):
            window = klines[i-window_size:i]
            current_k = klines[i]
            mark_price = current_k[4]
            
            bl = compute_baseline(window)
            
            # 获取市场趋势
            trend_bias = judge_market_bias_stable(self.symbol, btc_4h, eth_4h)
            
            # 扫描信号
            result_long = match(window, mark_price, "LONG", "oscillation", bl, self.symbol,
                               enable_trend_filter=True, btc_closes_4h=btc_4h, eth_closes_4h=eth_4h)
            result_short = match(window, mark_price, "SHORT", "oscillation", bl, self.symbol,
                                enable_trend_filter=True, btc_closes_4h=btc_4h, eth_closes_4h=eth_4h)
            
            # 统计场景触发
            for r in [result_long, result_short]:
                if r['matched']:
                    scene = r['scene']
                    self.scene_stats[scene] = self.scene_stats.get(scene, 0) + 1
            
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
        position_size_usd = calc_position_size_usd(result['second_score'])
        if position_size_usd == 0:
            return
        
        position_value = position_size_usd * self.leverage
        contract_size = position_value / result['entry_price']
        
        # 计算止损距离（ATR 倍数）
        sl_distance = abs(result['entry_price'] - result['sl_price'])
        sl_distance_atr_multiple = sl_distance / bl.atr_baseline if bl.atr_baseline > 0 else 0
        
        self.position = {
            "direction": result['direction'],
            "entry": result['entry_price'],
            "sl": result['sl_price'],
            "tp": result['tp_price'],
            "size": contract_size,
            "scene": result['scene'],
            "second_score": result['second_score'],
            "open_bar_index": bar_index,
            "market_bias_at_entry": trend_bias,
            "sl_distance_atr_multiple": round(sl_distance_atr_multiple, 2),
        }
    
    def _check_position(self, kline: list, current_bar_index: int, bl):
        if not self.position:
            return
        
        pos = self.position
        high = kline[2]
        low = kline[3]
        
        if pos['direction'] == 'LONG':
            if low <= pos['sl']:
                pnl = (pos['sl'] - pos['entry']) * pos['size']
                self._close_position(pnl, "sl", kline, current_bar_index)
            elif high >= pos['tp']:
                pnl = (pos['tp'] - pos['entry']) * pos['size']
                self._close_position(pnl, "tp", kline, current_bar_index)
        else:
            if high >= pos['sl']:
                pnl = (pos['entry'] - pos['sl']) * pos['size']
                self._close_position(pnl, "sl", kline, current_bar_index)
            elif low <= pos['tp']:
                pnl = (pos['entry'] - pos['tp']) * pos['size']
                self._close_position(pnl, "tp", kline, current_bar_index)
    
    def _close_position(self, pnl: float, exit_type: str, kline: list, current_bar_index: int):
        pos = self.position
        bars_held = current_bar_index - pos['open_bar_index']
        
        self.trades.append({
            "direction": pos['direction'],
            "scene": pos['scene'],
            "second_score": pos['second_score'],
            "market_bias_at_entry": pos['market_bias_at_entry'],
            "sl_distance_atr_multiple": pos['sl_distance_atr_multiple'],
            "exit_type": exit_type,
            "bars_held": bars_held,
            "pnl": round(pnl, 2),
        })
        
        if pnl > 0:
            self.consecutive_losses = 0
        else:
            self.consecutive_losses += 1
            self.max_consecutive_losses = max(self.max_consecutive_losses, self.consecutive_losses)
        
        self.capital += pnl
        self.position = None
    
    def _finalize(self):
        if self.position:
            pass
    
    def print_report(self, period_name: str):
        print(f"\n{'='*70}")
        print(f"📊 {period_name} - {self.symbol}")
        print(f"{'='*70}")
        
        # 场景分布
        print(f"\n【场景分布】")
        for scene, count in sorted(self.scene_stats.items()):
            print(f"  {scene}: {count}笔")
        
        # 总体统计
        total = len(self.trades)
        wins = sum(1 for t in self.trades if t['pnl'] > 0)
        win_rate = wins / total * 100 if total > 0 else 0
        total_pnl = sum(t['pnl'] for t in self.trades)
        
        gross_profit = sum(t['pnl'] for t in self.trades if t['pnl'] > 0)
        gross_loss = abs(sum(t['pnl'] for t in self.trades if t['pnl'] < 0))
        pf = gross_profit / gross_loss if gross_loss > 0 else 0
        
        print(f"\n【总体统计】")
        print(f"  总交易数：    {total}")
        print(f"  胜率：        {win_rate:.1f}%")
        print(f"  盈亏比：      {pf:.2f}")
        print(f"  总盈亏：      ${total_pnl:.2f}")
        print(f"  最大连续止损：{self.max_consecutive_losses}")
        
        # 重点验证指标
        print(f"\n【修复验证】")
        sl_distances = [t['sl_distance_atr_multiple'] for t in self.trades]
        min_sl = min(sl_distances) if sl_distances else 0
        print(f"  止损距离最小值：{min_sl:.2f}x ATR {'✅' if min_sl >= 0.5 else '❌'}")
        
        bias_dist = {}
        for t in self.trades:
            bias = t['market_bias_at_entry']
            bias_dist[bias] = bias_dist.get(bias, 0) + 1
        print(f"  market_bias 分布：{bias_dist}")
        print(f"  最大连续止损：{self.max_consecutive_losses} {'✅' if self.max_consecutive_losses < 9 else '⚠️'}")
        
        # 逐笔交易
        print(f"\n【逐笔交易】")
        for i, t in enumerate(self.trades):
            print(f"  {i+1:2d}. {t['direction']:<5} {t['scene']:<15} | 打分={t['second_score']:<3} 趋势={t['market_bias_at_entry']:<10} | "
                  f"SL={t['sl_distance_atr_multiple']:.2f}x 持仓={t['bars_held']:3d}K 退出={t['exit_type']:<4} PnL=${t['pnl']:>7.2f}")
        
        return {
            "symbol": self.symbol,
            "period": period_name,
            "scene_stats": self.scene_stats,
            "total_trades": total,
            "winning_trades": wins,
            "win_rate": win_rate,
            "total_pnl": total_pnl,
            "profit_factor": pf,
            "max_consecutive_losses": self.max_consecutive_losses,
            "sl_distance_min": min_sl,
            "bias_distribution": bias_dist,
            "trades": self.trades,
        }


def main():
    print("="*70)
    print("🧪 BTC 2026-07 + ETH 2024-01 回测（修复后）")
    print("="*70)
    
    # 获取数据
    btc_klines = fetch_klines("BTCUSDT", "2026-07-01", "2026-07-31", "1h")
    eth_klines_2024 = fetch_klines("ETHUSDT", "2024-01-01", "2024-01-31", "1h")
    btc_4h = fetch_4h_klines("BTCUSDT", days=10)
    eth_4h = fetch_4h_klines("ETHUSDT", days=10)
    
    results = []
    
    # BTC 2026-07
    if btc_klines:
        bt_btc = FixedBacktest("BTCUSDT")
        bt_btc.run(btc_klines, btc_4h, eth_4h)
        results.append(bt_btc.print_report("2026-07 BTC"))
    
    # ETH 2024-01
    if eth_klines_2024:
        bt_eth = FixedBacktest("ETHUSDT")
        bt_eth.run(eth_klines_2024, btc_4h, eth_4h)
        results.append(bt_eth.print_report("2024-01 ETH"))
    
    # 导出
    output_file = Path(__file__).parent / "btc_eth_fixed_backtest.json"
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n✅ 数据已导出：{output_file}")


if __name__ == "__main__":
    main()
