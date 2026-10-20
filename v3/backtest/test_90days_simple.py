#!/usr/bin/env python3
"""
任务 B：90 天回测（简化版）

三个时间段：
1. ETH/BTC 上涨趋势（2024-01）
2. PEPE 下跌行情（2024-07）
3. 多币种并行（2026-07）
"""

import sys
import json
import requests
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))

from modules.dynamic_baseline import compute as compute_baseline
from modules.scene_matcher import match


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


class SimpleBacktest:
    """简化回测引擎"""
    
    def __init__(self, symbol: str, leverage: int = 5):
        self.symbol = symbol
        self.leverage = leverage
        self.initial_capital = 10000
        self.capital = self.initial_capital
        
        # 统计
        self.scene_stats = {}
        self.scene_wins = {}
        self.scene_losses = {}
        
        self.total_trades = 0
        self.winning_trades = 0
        self.consecutive_losses = 0
        self.max_consecutive_losses = 0
        
        # 交易记录
        self.trades = []
        self.position = None
    
    def run(self, klines: list):
        """运行回测"""
        print(f"\n🚀 回测 {self.symbol}...")
        
        window_size = 200
        
        for i in range(window_size, len(klines)):
            window = klines[i-window_size:i]
            current_k = klines[i]
            mark_price = current_k[4]
            
            bl = compute_baseline(window)
            
            # 扫描信号
            result_long = match(window, mark_price, "LONG", "oscillation", bl, self.symbol)
            result_short = match(window, mark_price, "SHORT", "oscillation", bl, self.symbol)
            
            # 统计场景触发
            for r in [result_long, result_short]:
                if r['matched']:
                    scene = r['scene']
                    self.scene_stats[scene] = self.scene_stats.get(scene, 0) + 1
            
            # 开仓（无仓位时）
            if not self.position:
                if result_long['matched'] and result_long['second_score'] >= 70:
                    self._open_position(result_long, current_k, i)
                elif result_short['matched'] and result_short['second_score'] >= 70:
                    self._open_position(result_short, current_k, i)
            
            # 检查仓位
            if self.position:
                self._check_position(current_k, i)
        
        self._finalize()
    
    def _open_position(self, result: dict, kline: list, bar_index: int):
        """开仓"""
        position_size_usd = calc_position_size_usd(result['second_score'])
        if position_size_usd == 0:
            return
        
        position_value = position_size_usd * self.leverage
        contract_size = position_value / result['entry_price']
        
        self.position = {
            "direction": result['direction'],
            "entry": result['entry_price'],
            "sl": result['sl_price'],
            "tp": result['tp_price'],
            "size": contract_size,
            "scene": result['scene'],
            "second_score": result['second_score'],
            "open_bar_index": bar_index,
        }
    
    def _check_position(self, kline: list, current_bar_index: int):
        """检查仓位"""
        if not self.position:
            return
        
        pos = self.position
        high = kline[2]
        low = kline[3]
        
        if pos['direction'] == 'LONG':
            if low <= pos['sl']:
                pnl = (pos['sl'] - pos['entry']) * pos['size']
                self._close_position(pnl, "SL", kline)
            elif high >= pos['tp']:
                pnl = (pos['tp'] - pos['entry']) * pos['size']
                self._close_position(pnl, "TP", kline)
        else:
            if high >= pos['sl']:
                pnl = (pos['entry'] - pos['sl']) * pos['size']
                self._close_position(pnl, "SL", kline)
            elif low <= pos['tp']:
                pnl = (pos['entry'] - pos['tp']) * pos['size']
                self._close_position(pnl, "TP", kline)
    
    def _close_position(self, pnl: float, reason: str, kline: list):
        """平仓"""
        pos = self.position
        
        self.total_trades += 1
        if pnl > 0:
            self.winning_trades += 1
            self.scene_wins[pos['scene']] = self.scene_wins.get(pos['scene'], 0) + 1
            self.consecutive_losses = 0
        else:
            self.consecutive_losses += 1
            self.max_consecutive_losses = max(self.max_consecutive_losses, self.consecutive_losses)
            self.scene_losses[pos['scene']] = self.scene_losses.get(pos['scene'], 0) + 1
        
        self.capital += pnl
        
        self.trades.append({
            "direction": pos['direction'],
            "scene": pos['scene'],
            "pnl": pnl,
            "reason": reason,
            "second_score": pos['second_score'],
        })
        
        self.position = None
    
    def _finalize(self):
        """回测结束"""
        if self.position:
            # 按最后价格平仓
            pass
    
    def print_report(self, period_name: str):
        """打印报告"""
        print(f"\n{'='*70}")
        print(f"📊 {period_name} - {self.symbol}")
        print(f"{'='*70}")
        
        # 场景分布
        print(f"\n【场景分布】")
        for scene, count in sorted(self.scene_stats.items()):
            wins = self.scene_wins.get(scene, 0)
            losses = self.scene_losses.get(scene, 0)
            total = wins + losses
            win_rate = wins / total * 100 if total > 0 else 0
            print(f"  {scene}: {count}笔触发 胜率={win_rate:.1f}% ({wins}W/{losses}L)")
        
        # 总体统计
        total_trades = self.total_trades
        win_rate = self.winning_trades / total_trades * 100 if total_trades > 0 else 0
        total_pnl = sum(t['pnl'] for t in self.trades)
        
        # 计算盈亏比
        gross_profit = sum(t['pnl'] for t in self.trades if t['pnl'] > 0)
        gross_loss = abs(sum(t['pnl'] for t in self.trades if t['pnl'] < 0))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else 0
        
        print(f"\n【总体统计】")
        print(f"  总交易数：    {total_trades}")
        print(f"  胜率：        {win_rate:.1f}%")
        print(f"  盈亏比：      {profit_factor:.2f}")
        print(f"  总盈亏：      ${total_pnl:.2f} ({total_pnl/self.initial_capital*100:.2f}%)")
        print(f"  最大连续止损：{self.max_consecutive_losses}")
        
        return {
            "symbol": self.symbol,
            "period": period_name,
            "scene_stats": self.scene_stats,
            "scene_wins": self.scene_wins,
            "scene_losses": self.scene_losses,
            "total_trades": total_trades,
            "winning_trades": self.winning_trades,
            "win_rate": win_rate,
            "total_pnl": total_pnl,
            "profit_factor": profit_factor,
            "max_consecutive_losses": self.max_consecutive_losses,
            "trades": self.trades,
        }


def main():
    """主函数"""
    print("="*70)
    print("🧪 任务 B：90 天多时间段回测（简化版）")
    print("="*70)
    
    results = []
    
    # ── 时间段 1：ETH/BTC 上涨趋势 ───────────────────────────────────────
    print("\n\n📌 时间段 1：ETH 上涨趋势（2024-01）")
    eth_klines_jan = fetch_klines("ETHUSDT", "2024-01-01", "2024-01-31", "1h")
    
    if eth_klines_jan:
        bt = SimpleBacktest("ETHUSDT")
        bt.run(eth_klines_jan)
        results.append(bt.print_report("2024-01 ETH 上涨"))
    
    # ── 时间段 2：PEPE 下跌行情 ─────────────────────────────────────────
    print("\n\n📌 时间段 2：PEPE 下跌行情（2024-07）")
    pepe_klines_jul = fetch_klines("1000PEPEUSDT", "2024-07-01", "2024-07-31", "1h")
    
    if pepe_klines_jul:
        bt = SimpleBacktest("1000PEPEUSDT")
        bt.run(pepe_klines_jul)
        results.append(bt.print_report("2024-07 PEPE 下跌"))
    
    # ── 时间段 3：多币种并行（2026-07）──────────────────────────────────
    print("\n\n📌 时间段 3：多币种并行（2026-07）")
    for symbol in ["BTCUSDT", "ETHUSDT", "1000PEPEUSDT", "SOLUSDT"]:
        klines = fetch_klines(symbol, "2026-07-01", "2026-07-31", "1h")
        if klines:
            bt = SimpleBacktest(symbol)
            bt.run(klines)
            results.append(bt.print_report(f"2026-07 {symbol}"))
    
    # ── 导出 JSON ───────────────────────────────────────────────────────
    output_file = Path(__file__).parent / "90days_backtest_simple.json"
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n✅ 数据已导出：{output_file}")


if __name__ == "__main__":
    main()
