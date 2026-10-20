#!/usr/bin/env python3
"""
ETH 回测脚本 - 修复版（趋势过滤 v2.0 + 时间止损 v2.0）

修复内容：
1. 趋势过滤：改用 4H EMA50 斜率，动态门槛（不是二元拒绝）
2. 时间止损：改用小时制，加入进度判断
"""

import sys
import json
import requests
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))

from modules.dynamic_baseline import compute as compute_baseline
from modules.scene_matcher import match
from modules.time_stop import check_time_stop


def fetch_klines(symbol: str, start_date: str, end_date: str, interval: str = "1h") -> list:
    """获取历史 K 线"""
    print(f"📊 获取 {symbol} 历史 K 线（{start_date} ~ {end_date}）...")
    
    url = "https://fapi.binance.com/fapi/v1/klines"
    
    from datetime import datetime
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
    """获取 4H K 线（用于趋势过滤）"""
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


class BacktestEngine:
    """回测引擎（修复版）"""
    
    def __init__(self, leverage: int = 5, enable_trend_filter: bool = True, enable_time_stop: bool = True):
        self.leverage = leverage
        self.initial_capital = 10000
        self.capital = self.initial_capital
        self.enable_trend_filter = enable_trend_filter
        self.enable_time_stop = enable_time_stop
        
        # 统计
        self.signals_scanned = 0
        self.orders_placed = 0
        self.orders_filled = 0
        self.tp_hits = 0
        self.sl_hits = 0
        self.time_sl_hits = 0
        
        # 趋势过滤统计
        self.trend_filter_stats = {
            "NEUTRAL": 0, "BULLISH": 0, "BEARISH": 0,
            "filtered_by_trend": 0, "passed_trend": 0
        }
        
        # 交易记录
        self.trades = []
        self.current_position = None
        self.pending_orders = []
    
    def run(self, klines: list, btc_4h: list, eth_4h: list):
        """运行回测"""
        print(f"\n🚀 开始回测（修复版）...")
        print(f"   杠杆倍数：{self.leverage}x")
        print(f"   趋势过滤：{'启用' if self.enable_trend_filter else '禁用'}")
        print(f"   时间止损：{'启用' if self.enable_time_stop else '禁用'}")
        
        window_size = 200
        
        for i in range(window_size, len(klines)):
            window = klines[i-window_size:i]
            current_k = klines[i]
            mark_price = current_k[4]
            current_time = current_k[0]
            
            bl = compute_baseline(window)
            
            # 扫描信号（带趋势过滤）
            result_long = match(window, mark_price, "LONG", "oscillation", bl,
                               "ETHUSDT", enable_trend_filter=self.enable_trend_filter,
                               btc_closes_4h=btc_4h, eth_closes_4h=eth_4h)
            result_short = match(window, mark_price, "SHORT", "oscillation", bl,
                                "ETHUSDT", enable_trend_filter=self.enable_trend_filter,
                                btc_closes_4h=btc_4h, eth_closes_4h=eth_4h)
            
            # 统计信号
            if result_long['matched']:
                self.signals_scanned += 1
            if result_short['matched']:
                self.signals_scanned += 1
            
            # 统计趋势过滤
            if self.enable_trend_filter:
                # 从 conditions 中读取 trend_bias
                if result_long.get('conditions', {}).get('trend_bias'):
                    bias = result_long['conditions']['trend_bias']
                    self.trend_filter_stats[bias] = self.trend_filter_stats.get(bias, 0) + 1
                    if result_long['matched']:
                        self.trend_filter_stats["passed_trend"] += 1
                    else:
                        self.trend_filter_stats["filtered_by_trend"] += 1
                
                if result_short.get('conditions', {}).get('trend_bias'):
                    bias = result_short['conditions']['trend_bias']
                    self.trend_filter_stats[bias] = self.trend_filter_stats.get(bias, 0) + 1
                    if result_short['matched']:
                        self.trend_filter_stats["passed_trend"] += 1
                    else:
                        self.trend_filter_stats["filtered_by_trend"] += 1
            
            # 尝试开仓
            if result_long['matched'] and not self.current_position:
                self._try_open_position(result_long, current_k, i)
            
            if result_short['matched'] and not self.current_position:
                self._try_open_position(result_short, current_k, i)
            
            # 检查挂单成交
            self._check_pending_orders(current_k, i)
            
            # 检查现有仓位
            if self.current_position:
                self._check_position(current_k, i)
        
        self._finalize()
    
    def _try_open_position(self, result: dict, kline: list, bar_index: int):
        """尝试开仓"""
        entry = result['entry_price']
        sl = result['sl_price']
        tp = result['tp_price']
        second_score = result['second_score']
        scene = result['scene']
        
        # 计算仓位
        position_size_usd = calc_position_size_usd(second_score)
        
        if position_size_usd == 0:
            return
        
        # 计算合约数量
        position_value = position_size_usd * self.leverage
        contract_size = position_value / entry
        
        # 检查挂单价合理性
        current_price = kline[4]
        if result['direction'] == 'LONG':
            if entry > current_price * 1.02:
                return
        else:
            if entry < current_price * 0.98:
                return
        
        # 添加挂单
        self.pending_orders.append({
            "direction": result['direction'],
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "size": contract_size,
            "position_size_usd": position_size_usd,
            "second_score": second_score,
            "scene": scene,
            "open_bar_index": bar_index,
            "open_time": kline[0],
        })
        
        self.orders_placed += 1
    
    def _check_pending_orders(self, kline: list, current_bar_index: int):
        """检查挂单成交"""
        high = kline[2]
        low = kline[3]
        
        filled_orders = []
        for order in self.pending_orders:
            filled = False
            
            if order['direction'] == 'LONG':
                if low <= order['entry']:
                    filled = True
            else:
                if high >= order['entry']:
                    filled = True
            
            if filled:
                self.orders_filled += 1
                self.current_position = {
                    "direction": order['direction'],
                    "entry": order['entry'],
                    "sl": order['sl'],
                    "tp": order['tp'],
                    "size": order['size'],
                    "position_size_usd": order['position_size_usd'],
                    "second_score": order['second_score'],
                    "scene": order['scene'],
                    "open_bar_index": current_bar_index,
                    "open_time": order['open_time'],
                }
                filled_orders.append(order)
        
        for order in filled_orders:
            self.pending_orders.remove(order)
        
        if self.current_position:
            self.pending_orders = []
    
    def _check_position(self, kline: list, current_bar_index: int):
        """检查仓位状态"""
        if not self.current_position:
            return
        
        pos = self.current_position
        high = kline[2]
        low = kline[3]
        
        # 检查时间止损（修复版）
        if self.enable_time_stop:
            time_sl_triggered, time_sl_reason = check_time_stop(
                entry_time=pos['open_time'],
                current_time=kline[0],
                scene=pos['scene'],
                second_score=pos['second_score'],
                entry_price=pos['entry'],
                current_price=kline[4],
                tp_price=pos['tp'],
                direction=pos['direction']
            )
            
            if time_sl_triggered:
                pnl = (kline[4] - pos['entry']) * pos['size'] if pos['direction'] == 'LONG' else (pos['entry'] - kline[4]) * pos['size']
                self.capital += pnl
                self.time_sl_hits += 1
                self._close_position(pnl, f"TIME_SL({time_sl_reason})", kline)
                return
        
        # 检查价格止损/止盈
        if pos['direction'] == 'LONG':
            if low <= pos['sl']:
                pnl = (pos['sl'] - pos['entry']) * pos['size']
                self.capital += pnl
                self.sl_hits += 1
                self._close_position(pnl, "SL", kline)
                return
            
            if high >= pos['tp']:
                pnl = (pos['tp'] - pos['entry']) * pos['size']
                self.capital += pnl
                self.tp_hits += 1
                self._close_position(pnl, "TP", kline)
                return
        else:
            if high >= pos['sl']:
                pnl = (pos['entry'] - pos['sl']) * pos['size']
                self.capital += pnl
                self.sl_hits += 1
                self._close_position(pnl, "SL", kline)
                return
            
            if low <= pos['tp']:
                pnl = (pos['entry'] - pos['tp']) * pos['size']
                self.capital += pnl
                self.tp_hits += 1
                self._close_position(pnl, "TP", kline)
                return
    
    def _close_position(self, pnl: float, reason: str, kline: list):
        """平仓"""
        trade = {
            "direction": self.current_position['direction'],
            "entry": self.current_position['entry'],
            "exit": kline[4],
            "pnl": pnl,
            "reason": reason,
            "second_score": self.current_position['second_score'],
            "position_size_usd": self.current_position['position_size_usd'],
            "scene": self.current_position['scene'],
        }
        self.trades.append(trade)
        self.current_position = None
    
    def _finalize(self):
        """回测结束"""
        if self.current_position:
            pnl = 0
            self._close_position(pnl, "END", [0, 0, 0, 0, 0, 0])
    
    def _print_report(self, label: str):
        """打印回测报告"""
        print(f"\n{'='*70}")
        print(f"📊 {label}")
        print(f"{'='*70}")
        
        total_trades = len(self.trades)
        winning_trades = sum(1 for t in self.trades if t['pnl'] > 0)
        win_rate = winning_trades / total_trades * 100 if total_trades > 0 else 0
        
        total_pnl = sum(t['pnl'] for t in self.trades)
        total_pnl_pct = total_pnl / self.initial_capital * 100
        
        # 计算盈亏比
        gross_profit = sum(t['pnl'] for t in self.trades if t['pnl'] > 0)
        gross_loss = abs(sum(t['pnl'] for t in self.trades if t['pnl'] < 0))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else 0
        
        print(f"\n【交易统计】")
        print(f"  信号扫描：    {self.signals_scanned}")
        print(f"  挂单数量：    {self.orders_placed}")
        print(f"  实际成交：    {self.orders_filled}")
        print(f"  总交易数：    {total_trades}")
        print(f"  止盈：        {self.tp_hits}")
        print(f"  止损：        {self.sl_hits}")
        print(f"  时间止损：    {self.time_sl_hits}")
        print(f"  胜率：        {win_rate:.1f}%")
        print(f"  盈亏比：      {profit_factor:.2f}")
        print(f"\n【盈亏统计】")
        print(f"  总盈亏：      ${total_pnl:.2f} ({total_pnl_pct:.2f}%)")
        print(f"  初始资金：    ${self.initial_capital:.2f}")
        print(f"  最终资金：    ${self.capital:.2f}")
        
        if self.enable_trend_filter:
            print(f"\n【趋势过滤统计】")
            print(f"  NEUTRAL:      {self.trend_filter_stats.get('NEUTRAL', 0)}")
            print(f"  BULLISH:      {self.trend_filter_stats.get('BULLISH', 0)}")
            print(f"  BEARISH:      {self.trend_filter_stats.get('BEARISH', 0)}")
            print(f"  通过：        {self.trend_filter_stats.get('passed_trend', 0)}")
            print(f"  过滤：        {self.trend_filter_stats.get('filtered_by_trend', 0)}")


def main():
    """主函数"""
    print("="*70)
    print("🧪 ETH 回测 - 修复版对比测试")
    print("="*70)
    
    # 获取 ETH 2026 年 7 月数据
    klines = fetch_klines("ETHUSDT", "2026-07-01", "2026-07-31", "1h")
    
    if not klines:
        print("❌ 无法获取 K 线数据，退出测试")
        return
    
    # 获取 BTC 和 ETH 4H 数据（用于趋势过滤）
    btc_4h = fetch_4h_klines("BTCUSDT", days=10)
    eth_4h = fetch_4h_klines("ETHUSDT", days=10)
    
    # 测试 1：基础版（无趋势过滤，无时间止损）
    print("\n\n📌 测试 1：基础版（无趋势过滤，无时间止损）")
    engine1 = BacktestEngine(leverage=5, enable_trend_filter=False, enable_time_stop=False)
    engine1.run(klines, btc_4h, eth_4h)
    engine1._print_report("基础版")
    
    # 测试 2：修复版（趋势过滤 v2.0 + 时间止损 v2.0）
    print("\n\n📌 测试 2：修复版（趋势过滤 v2.0 + 时间止损 v2.0）")
    engine2 = BacktestEngine(leverage=5, enable_trend_filter=True, enable_time_stop=True)
    engine2.run(klines, btc_4h, eth_4h)
    engine2._print_report("修复版")
    
    # 对比总结
    print("\n\n" + "="*70)
    print("📊 对比总结")
    print("="*70)
    
    results = [
        ("基础版", engine1),
        ("修复版", engine2),
    ]
    
    print(f"\n{'版本':<12} {'交易数':<8} {'胜率':<8} {'盈亏比':<10} {'总盈亏':<12} {'时间止损':<10}")
    print("-"*65)
    for label, engine in results:
        total_trades = len(engine.trades)
        winning_trades = sum(1 for t in engine.trades if t['pnl'] > 0)
        win_rate = winning_trades / total_trades * 100 if total_trades > 0 else 0
        total_pnl = sum(t['pnl'] for t in engine.trades)
        gross_profit = sum(t['pnl'] for t in engine.trades if t['pnl'] > 0)
        gross_loss = abs(sum(t['pnl'] for t in engine.trades if t['pnl'] < 0))
        pf = gross_profit / gross_loss if gross_loss > 0 else 0
        print(f"{label:<12} {total_trades:<8} {win_rate:<8.1f}% {pf:<10.2f} ${total_pnl:<11.2f} {engine.time_sl_hits:<10}")
    
    print("\n💡 关键观察：")
    print("  1. 趋势过滤是否合理拒绝了逆势信号？")
    print("  2. 时间止损触发率是否从 60% 降下来？")
    print("  3. 整体胜率和盈亏比是否改善？")


if __name__ == "__main__":
    main()
