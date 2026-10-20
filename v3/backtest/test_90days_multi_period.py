#!/usr/bin/env python3
"""
任务 B：90 天回测（三个时间段）

时间段 1：ETH/BTC 上涨趋势（2024-01-01 ~ 2024-01-31）
时间段 2：PEPE 下跌行情（2024-07-01 ~ 2024-07-31）
时间段 3：多币种并行（2026-07-01 ~ 2026-07-31）BTC+ETH+PEPE+SOL
"""

import sys
import json
import requests
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, str(Path(__file__).parent.parent))

from modules.dynamic_baseline import compute as compute_baseline
from modules.scene_matcher import match
from modules.top10_selector import select_top10


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


class MultiPeriodBacktest:
    """多时间段回测引擎"""
    
    def __init__(self, leverage: int = 5):
        self.leverage = leverage
        self.initial_capital = 10000
        self.capital = self.initial_capital
        
        # 统计
        self.scene_stats = {"A_pullback": 0, "B_breakout": 0, "C_first_pull": 0, "E_short": 0, "F_breakdown": 0, "G_first_rally": 0}
        self.scene_wins = {k: 0 for k in self.scene_stats}
        self.scene_losses = {k: 0 for k in self.scene_stats}
        
        self.total_trades = 0
        self.winning_trades = 0
        self.consecutive_losses = 0
        self.max_consecutive_losses = 0
        
        # 仓位限制跟踪
        self.max_long = 2
        self.max_short = 2
        self.current_long = 0
        self.current_short = 0
        
        # 预警池统计
        self.volume_warnings = 0
        self.warning_to_trade = 0
        
        # 交易记录
        self.trades = []
        self.positions = {}  # symbol -> position
    
    def run_single_symbol(self, symbol: str, klines: list, period_name: str) -> dict:
        """单个币种回测"""
        result = {
            "symbol": symbol,
            "period": period_name,
            "trades": [],
            "scene_stats": {k: 0 for k in self.scene_stats},
        }
        
        window_size = 200
        
        for i in range(window_size, len(klines)):
            window = klines[i-window_size:i]
            current_k = klines[i]
            mark_price = current_k[4]
            
            bl = compute_baseline(window)
            
            # 扫描信号
            result_long = match(window, mark_price, "LONG", "oscillation", bl, symbol)
            result_short = match(window, mark_price, "SHORT", "oscillation", bl, symbol)
            
            # 场景统计
            if result_long['matched']:
                scene = result_long['scene']
                result['scene_stats'][scene] = result['scene_stats'].get(scene, 0) + 1
            
            if result_short['matched']:
                scene = result_short['scene']
                result['scene_stats'][scene] = result['scene_stats'].get(scene, 0) + 1
        
        return result
    
    def run_multi_symbol(self, symbols: list, klines_map: dict, period_name: str) -> dict:
        """多币种并行回测（带 Top10 竞争）"""
        print(f"\n🚀 多币种并行回测：{symbols}")
        
        result = {
            "period": period_name,
            "symbols": symbols,
            "trades": [],
            "scene_stats": {k: 0 for k in self.scene_stats},
            "position_limits_hit": {"long": 0, "short": 0},
        }
        
        window_size = 200
        
        for i in range(window_size, len(klines_map[symbols[0]])):
            # 所有币种使用相同的时间窗口
            window_map = {sym: klines_map[sym][i-window_size:i] for sym in symbols}
            current_k_map = {sym: klines_map[sym][i] for sym in symbols}
            
            # 并发扫描所有币种
            all_results = []
            for symbol in symbols:
                window = window_map[symbol]
                mark_price = window[-1][4]
                bl = compute_baseline(window)
                
                result_long = match(window, mark_price, "LONG", "oscillation", bl, symbol)
                result_short = match(window, mark_price, "SHORT", "oscillation", bl, symbol)
                
                if result_long['matched']:
                    all_results.append({
                        "symbol": symbol,
                        "direction": "LONG",
                        "score": result_long['second_score'],
                        "scene": result_long['scene'],
                        "entry": result_long['entry_price'],
                        "sl": result_long['sl_price'],
                        "tp": result_long['tp_price'],
                    })
                
                if result_short['matched']:
                    all_results.append({
                        "symbol": symbol,
                        "direction": "SHORT",
                        "score": result_short['second_score'],
                        "scene": result_short['scene'],
                        "entry": result_short['entry_price'],
                        "sl": result_short['sl_price'],
                        "tp": result_short['tp_price'],
                    })
            
            # Top10 竞争
            top10 = select_top10(all_results)
            
            # 检查仓位限制
            for item in top10:
                if item['direction'] == 'LONG' and self.current_long >= self.max_long:
                    result['position_limits_hit']['long'] += 1
                    continue
                if item['direction'] == 'SHORT' and self.current_short >= self.max_short:
                    result['position_limits_hit']['short'] += 1
                    continue
                
                # 开仓
                self._open_position(item, current_k_map[item['symbol']])
        
        return result
    
    def _open_position(self, item: dict, kline: list):
        """开仓"""
        symbol = item['symbol']
        direction = item['direction']
        
        # 更新仓位计数
        if direction == 'LONG':
            self.current_long += 1
        else:
            self.current_short += 1
        
        # 记录仓位
        self.positions[symbol] = {
            **item,
            "open_time": kline[0],
            "open_price": kline[4],
        }
    
    def _check_positions(self, klines_map: dict, current_idx: int):
        """检查所有仓位"""
        closed = []
        
        for symbol, pos in list(self.positions.items()):
            if symbol not in klines_map:
                continue
            
            kline = klines_map[symbol][current_idx]
            high = kline[2]
            low = kline[3]
            
            if pos['direction'] == 'LONG':
                if low <= pos['sl']:
                    pnl = (pos['sl'] - pos['entry']) * pos['score']  # 简化
                    self._close_position(symbol, pnl, "SL", kline)
                    closed.append(symbol)
                elif high >= pos['tp']:
                    pnl = (pos['tp'] - pos['entry']) * pos['score']
                    self._close_position(symbol, pnl, "TP", kline)
                    closed.append(symbol)
            else:
                if high >= pos['sl']:
                    pnl = (pos['entry'] - pos['sl']) * pos['score']
                    self._close_position(symbol, pnl, "SL", kline)
                    closed.append(symbol)
                elif low <= pos['tp']:
                    pnl = (pos['entry'] - pos['tp']) * pos['score']
                    self._close_position(symbol, pnl, "TP", kline)
                    closed.append(symbol)
        
        for symbol in closed:
            del self.positions[symbol]
    
    def _close_position(self, symbol: str, pnl: float, reason: str, kline: list):
        """平仓"""
        pos = self.positions[symbol]
        
        if pos['direction'] == 'LONG':
            self.current_long -= 1
        else:
            self.current_short -= 1
        
        # 更新统计
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
            "symbol": symbol,
            "direction": pos['direction'],
            "scene": pos['scene'],
            "pnl": pnl,
            "reason": reason,
        })
    
    def print_report(self, result: dict):
        """打印回测报告"""
        print(f"\n{'='*70}")
        print(f"📊 {result.get('period', '回测报告')}")
        print(f"{'='*70}")
        
        # 场景分布
        print(f"\n【场景分布】")
        for scene, count in result['scene_stats'].items():
            if count > 0:
                wins = self.scene_wins.get(scene, 0)
                losses = self.scene_losses.get(scene, 0)
                total = wins + losses
                win_rate = wins / total * 100 if total > 0 else 0
                print(f"  {scene}: {count}笔触发 胜率={win_rate:.1f}% ({wins}W/{losses}L)")
        
        # 总体统计
        total_trades = len(self.trades)
        winning_trades = self.winning_trades
        win_rate = winning_trades / total_trades * 100 if total_trades > 0 else 0
        
        total_pnl = sum(t['pnl'] for t in self.trades)
        
        print(f"\n【总体统计】")
        print(f"  总交易数：    {total_trades}")
        print(f"  胜率：        {win_rate:.1f}%")
        print(f"  总盈亏：      ${total_pnl:.2f}")
        print(f"  最大连续止损：{self.max_consecutive_losses}")
        
        # 仓位限制
        if 'position_limits_hit' in result:
            print(f"\n【仓位限制】")
            print(f"  多头超限：    {result['position_limits_hit']['long']}次")
            print(f"  空头超限：    {result['position_limits_hit']['short']}次")


def main():
    """主函数"""
    print("="*70)
    print("🧪 任务 B：90 天多时间段回测")
    print("="*70)
    
    # ── 时间段 1：ETH/BTC 上涨趋势 ───────────────────────────────────────
    print("\n\n📌 时间段 1：ETH/BTC 上涨趋势（2024-01）")
    btc_klines_jan = fetch_klines("BTCUSDT", "2024-01-01", "2024-01-31", "1h")
    eth_klines_jan = fetch_klines("ETHUSDT", "2024-01-01", "2024-01-31", "1h")
    
    if btc_klines_jan and eth_klines_jan:
        bt1 = MultiPeriodBacktest(leverage=5)
        result1 = bt1.run_multi_symbol(
            ["BTCUSDT", "ETHUSDT"],
            {"BTCUSDT": btc_klines_jan, "ETHUSDT": eth_klines_jan},
            "时间段 1: 2024-01 上涨趋势"
        )
        bt1.print_report(result1)
    
    # ── 时间段 2：PEPE 下跌行情 ─────────────────────────────────────────
    print("\n\n📌 时间段 2：PEPE 下跌行情（2024-07）")
    pepe_klines_jul = fetch_klines("1000PEPEUSDT", "2024-07-01", "2024-07-31", "1h")
    
    if pepe_klines_jul:
        bt2 = MultiPeriodBacktest(leverage=5)
        result2 = bt2.run_single_symbol("1000PEPEUSDT", pepe_klines_jul, "时间段 2: 2024-07 下跌")
        bt2.print_report(result2)
    
    # ── 时间段 3：多币种并行 ────────────────────────────────────────────
    print("\n\n📌 时间段 3：多币种并行（2026-07）")
    btc_klines = fetch_klines("BTCUSDT", "2026-07-01", "2026-07-31", "1h")
    eth_klines = fetch_klines("ETHUSDT", "2026-07-01", "2026-07-31", "1h")
    pepe_klines = fetch_klines("1000PEPEUSDT", "2026-07-01", "2026-07-31", "1h")
    sol_klines = fetch_klines("SOLUSDT", "2026-07-01", "2026-07-31", "1h")
    
    if btc_klines and eth_klines and pepe_klines and sol_klines:
        bt3 = MultiPeriodBacktest(leverage=5)
        result3 = bt3.run_multi_symbol(
            ["BTCUSDT", "ETHUSDT", "1000PEPEUSDT", "SOLUSDT"],
            {
                "BTCUSDT": btc_klines,
                "ETHUSDT": eth_klines,
                "1000PEPEUSDT": pepe_klines,
                "SOLUSDT": sol_klines,
            },
            "时间段 3: 2026-07 多币种并行"
        )
        bt3.print_report(result3)
    
    # ── 导出 JSON ───────────────────────────────────────────────────────
    output_file = Path(__file__).parent / "90days_backtest_results.json"
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump({
            "period1": result1 if btc_klines_jan else None,
            "period2": result2 if pepe_klines_jul else None,
            "period3": result3 if btc_klines else None,
        }, f, ensure_ascii=False, indent=2)
    print(f"\n✅ 数据已导出：{output_file}")


if __name__ == "__main__":
    main()
