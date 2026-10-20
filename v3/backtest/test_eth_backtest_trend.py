#!/usr/bin/env python3
"""
ETH 趋势行情回测 - 2024 年 10-11 月上涨段

测试 B/C 场景是否触发，胜率是否提升
"""

import sys
import json
import requests
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).parent.parent))

from modules.dynamic_baseline import compute as compute_baseline
from modules.scene_matcher_dynamic import match_dynamic
from modules.liquidation_data import LiquidationData


def fetch_eth_klines_custom(start_date: str, days: int = 30) -> list:
    """获取 ETH 自定义时间段 K 线"""
    print(f"📊 获取 ETHUSDT 历史 K 线（{start_date} 起 {days} 天）...")
    
    start_ts = int(datetime.strptime(start_date, "%Y-%m-%d").timestamp() * 1000)
    
    url = "https://fapi.binance.com/fapi/v1/klines"
    params = {
        "symbol": "ETHUSDT",
        "interval": "1h",
        "startTime": start_ts,
        "limit": days * 24
    }
    
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
        if klines:
            print(f"   时间：{datetime.fromtimestamp(klines[0][0]/1000)} ~ {datetime.fromtimestamp(klines[-1][0]/1000)}")
            print(f"   价格：{min(k[3] for k in klines):.2f} ~ {max(k[2] for k in klines):.2f}")
            
            # 计算涨跌幅
            start_price = klines[0][4]
            end_price = klines[-1][4]
            change_pct = (end_price - start_price) / start_price * 100
            print(f"   涨跌幅：{change_pct:+.1f}%")
        
        return klines
    except Exception as e:
        print(f"❌ 获取失败：{e}")
        return []


def generate_mock_liquidation(klines: list) -> dict:
    """生成模拟清算数据"""
    liquidations = []
    
    for i, k in enumerate(klines):
        time = k[0]
        high = k[2]
        low = k[3]
        
        if i > 20:
            recent_low = min(k[3] for k in klines[i-20:i])
            if low < recent_low * 0.99:
                liq_price = recent_low * 0.98
                liq_amount = 50000000 + (recent_low - low) * 10000000
                liquidations.append({
                    "time": time,
                    "price": round(liq_price, 2),
                    "amount": round(liq_amount, 0),
                    "side": "long",
                    "symbol": "ETHUSDT",
                })
            
            recent_high = max(k[2] for k in klines[i-20:i])
            if high > recent_high * 1.01:
                liq_price = recent_high * 1.02
                liq_amount = 50000000 + (high - recent_high) * 10000000
                liquidations.append({
                    "time": time,
                    "price": round(liq_price, 2),
                    "amount": round(liq_amount, 0),
                    "side": "short",
                    "symbol": "ETHUSDT",
                })
    
    # 按时间索引
    liq_by_time = {}
    for liq in liquidations:
        time_bucket = liq["time"]
        if time_bucket not in liq_by_time:
            liq_by_time[time_bucket] = {"long": [], "short": []}
        
        from modules.liquidation_data import LiquidationZone
        zone = LiquidationZone(
            price=liq["price"],
            liquidation_amount=liq["amount"],
            side=liq["side"],
            symbol="ETHUSDT"
        )
        
        if liq["side"] == "long":
            liq_by_time[time_bucket]["long"].append(zone)
        else:
            liq_by_time[time_bucket]["short"].append(zone)
    
    return liq_by_time


def get_liquidation_at_time(liq_by_time: dict, target_time: int, window_hours: int = 12) -> LiquidationData:
    """获取目标时间附近的清算数据"""
    window_ms = window_hours * 3600 * 1000
    
    all_long = []
    all_short = []
    
    for bucket_time, data in liq_by_time.items():
        if abs(bucket_time - target_time) <= window_ms:
            all_long.extend(data["long"])
            all_short.extend(data["short"])
    
    return LiquidationData(
        symbol="ETHUSDT",
        long_liquidations=all_long,
        short_liquidations=all_short,
        total_long_amount=sum(z.liquidation_amount for z in all_long),
        total_short_amount=sum(z.liquidation_amount for z in all_short),
        timestamp=target_time
    )


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
    """回测引擎"""
    
    def __init__(self, leverage: int = 5):
        self.leverage = leverage
        self.initial_capital = 10000
        self.capital = self.initial_capital
        
        # 统计
        self.signals_scanned = 0
        self.signals_triggered = 0
        self.orders_placed = 0
        self.orders_filled = 0
        self.tp_hits = 0
        self.sl_hits = 0
        self.blocked_by_liquidation = 0
        
        # 场景统计
        self.scene_stats = {}
        
        # 仓位统计
        self.position_size_stats = {"250U": 0, "400U": 0, "600U": 0, "800U": 0, "1000U": 0}
        
        # 交易记录
        self.trades = []
        self.current_position = None
        self.pending_orders = []
        self.logs = []
    
    def log(self, msg: str):
        self.logs.append(msg)
    
    def run(self, klines: list, liq_by_time: dict):
        """运行回测"""
        print(f"\n🚀 开始回测...")
        print(f"   杠杆倍数：{self.leverage}x")
        print(f"   初始资金：${self.initial_capital}")
        
        window_size = 200
        
        for i in range(window_size, len(klines)):
            window = klines[i-window_size:i]
            current_k = klines[i]
            mark_price = current_k[4]
            current_time = current_k[0]
            
            bl = compute_baseline(window)
            liq_data = get_liquidation_at_time(liq_by_time, current_time, window_hours=12)
            
            result_long = match_dynamic(window, mark_price, "LONG", "oscillation", bl, "ETHUSDT", liq_data)
            result_short = match_dynamic(window, mark_price, "SHORT", "oscillation", bl, "ETHUSDT", liq_data)
            
            # 统计场景
            if result_long['matched']:
                self.signals_scanned += 1
                scene = result_long['scene']
                self.scene_stats[scene] = self.scene_stats.get(scene, 0) + 1
            
            if result_short['matched']:
                self.signals_scanned += 1
                scene = result_short['scene']
                self.scene_stats[scene] = self.scene_stats.get(scene, 0) + 1
            
            if result_long['matched'] and not self.current_position:
                self.signals_triggered += 1
                self._try_open_position(result_long, current_k, "LONG")
            
            if result_short['matched'] and not self.current_position:
                self.signals_triggered += 1
                self._try_open_position(result_short, current_k, "SHORT")
            
            self._check_pending_orders(current_k)
            
            if self.current_position:
                self._check_position(current_k)
        
        self._finalize()
    
    def _try_open_position(self, result: dict, kline: list, direction: str):
        """尝试开仓"""
        entry = result['entry_price']
        sl = result['sl_price']
        tp = result['tp_price']
        tp1 = result.get('tp1_price')
        tp2 = result.get('tp2_price')
        second_score = result['second_score']
        
        if result.get('blocked_by_liquidation'):
            self.blocked_by_liquidation += 1
            return
        
        position_size_usd = calc_position_size_usd(second_score)
        
        if position_size_usd == 0:
            return
        
        size_key = f"{position_size_usd}U"
        self.position_size_stats[size_key] = self.position_size_stats.get(size_key, 0) + 1
        
        position_value = position_size_usd * self.leverage
        contract_size = position_value / entry
        
        current_price = kline[4]
        if direction == "LONG":
            if entry > current_price * 1.02:
                return
        else:
            if entry < current_price * 0.98:
                return
        
        self.pending_orders.append({
            "direction": direction,
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "tp1": tp1,
            "tp2": tp2,
            "size": contract_size,
            "position_size_usd": position_size_usd,
            "second_score": second_score,
            "scene": result['scene'],
            "create_time": kline[0],
            "create_price": current_price,
        })
        
        self.orders_placed += 1
        self.log(f"📋 挂单 {direction} {result['scene']} | 打分={second_score} 仓位=${position_size_usd}")
    
    def _check_pending_orders(self, kline: list):
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
                    "tp1": order['tp1'],
                    "tp2": order['tp2'],
                    "size": order['size'],
                    "position_size_usd": order['position_size_usd'],
                    "second_score": order['second_score'],
                    "scene": order['scene'],
                    "open_time": kline[0],
                    "open_price": kline[4],
                }
                self.log(f"✅ 成交 {order['direction']} @ {order['entry']:.2f}")
                filled_orders.append(order)
        
        for order in filled_orders:
            self.pending_orders.remove(order)
        
        if self.current_position:
            self.pending_orders = []
    
    def _check_position(self, kline: list):
        """检查仓位状态"""
        if not self.current_position:
            return
        
        pos = self.current_position
        high = kline[2]
        low = kline[3]
        
        if pos['direction'] == 'LONG':
            if low <= pos['sl']:
                pnl = (pos['sl'] - pos['entry']) * pos['size']
                self.capital += pnl
                self.sl_hits += 1
                self._close_position(pnl, "SL", kline)
                return
            
            if pos['tp1'] and high >= pos['tp1']:
                pnl1 = (pos['tp1'] - pos['entry']) * (pos['size'] * 0.5)
                self.capital += pnl1
                
                if pos['tp2'] and high >= pos['tp2']:
                    pnl2 = (pos['tp2'] - pos['entry']) * (pos['size'] * 0.5)
                    self.capital += pnl2
                    self.tp_hits += 1
                    self._close_position(pnl1 + pnl2, "TP1+TP2", kline)
                else:
                    pos['size'] *= 0.5
                    pos['sl'] = pos['entry']
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
            
            if pos['tp1'] and low <= pos['tp1']:
                pnl1 = (pos['entry'] - pos['tp1']) * (pos['size'] * 0.5)
                self.capital += pnl1
                
                if pos['tp2'] and low <= pos['tp2']:
                    pnl2 = (pos['entry'] - pos['tp2']) * (pos['size'] * 0.5)
                    self.capital += pnl2
                    self.tp_hits += 1
                    self._close_position(pnl1 + pnl2, "TP1+TP2", kline)
                else:
                    pos['size'] *= 0.5
                    pos['sl'] = pos['entry']
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
            "close_time": kline[0],
            "second_score": self.current_position['second_score'],
            "position_size_usd": self.current_position['position_size_usd'],
            "scene": self.current_position['scene'],
        }
        self.trades.append(trade)
        
        status = "✅" if pnl > 0 else "❌"
        self.log(f"{status} 平仓 {reason} @ {kline[4]:.2f} | PnL=${pnl:.2f}")
        
        self.current_position = None
    
    def _finalize(self):
        """回测结束"""
        if self.current_position:
            self.log(f"⚠️ 回测结束仍有未平仓位")
        
        self._print_report()
    
    def _print_report(self):
        """打印回测报告"""
        print("\n" + "=" * 70)
        print("📊 ETH 趋势行情回测报告")
        print("=" * 70)
        
        print(f"\n【场景统计】")
        for scene, count in sorted(self.scene_stats.items(), key=lambda x: x[1], reverse=True):
            print(f"  {scene}: {count} 个")
        
        print(f"\n【信号统计】")
        print(f"  扫描到信号：  {self.signals_scanned}")
        print(f"  触发挂单：    {self.signals_triggered}")
        print(f"  实际成交：    {self.orders_filled}")
        print(f"  止盈触发：    {self.tp_hits}")
        print(f"  止损触发：    {self.sl_hits}")
        
        if self.orders_filled > 0:
            win_rate = self.tp_hits / self.orders_filled * 100
            print(f"  胜率：        {win_rate:.1f}%")
        
        print(f"\n【仓位分布】")
        for size, count in sorted(self.position_size_stats.items(), key=lambda x: int(x[0].replace('U',''))):
            if count > 0:
                print(f"  {size}: {count} 笔")
        
        print(f"\n【盈亏统计】")
        print(f"  初始资金：    ${self.initial_capital:,.2f}")
        print(f"  最终资金：    ${self.capital:,.2f}")
        total_pnl = self.capital - self.initial_capital
        print(f"  总盈亏：      ${total_pnl:,.2f} ({total_pnl/self.initial_capital*100:.2f}%)")
        
        if self.trades:
            winning_trades = [t for t in self.trades if t['pnl'] > 0]
            losing_trades = [t for t in self.trades if t['pnl'] <= 0]
            avg_win = sum(t['pnl'] for t in winning_trades) / len(winning_trades) if winning_trades else 0
            avg_loss = sum(t['pnl'] for t in losing_trades) / len(losing_trades) if losing_trades else 0
            print(f"  盈利交易：    {len(winning_trades)} 笔，平均 ${avg_win:.2f}")
            print(f"  亏损交易：    {len(losing_trades)} 笔，平均 ${avg_loss:.2f}")
            if avg_loss != 0:
                print(f"  盈亏比：      {abs(avg_win / avg_loss):.2f}")
        
        print(f"\n【风险指标】")
        max_drawdown = self._calc_max_drawdown()
        print(f"  最大回撤：    ${max_drawdown:,.2f}")
        
        print("\n" + "=" * 70)
        
        self._save_trades()
    
    def _calc_max_drawdown(self) -> float:
        """计算最大回撤"""
        if not self.trades:
            return 0
        
        peak = self.initial_capital
        max_dd = 0
        cumulative = self.initial_capital
        
        for trade in self.trades:
            cumulative += trade['pnl']
            peak = max(peak, cumulative)
            dd = peak - cumulative
            max_dd = max(max_dd, dd)
        
        return max_dd
    
    def _save_trades(self):
        """保存交易记录"""
        output_file = Path(__file__).parent / "eth_backtest_trend_trades.json"
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump({
                "trades": self.trades,
                "scene_stats": self.scene_stats,
                "logs": self.logs[-100:],
            }, f, indent=2, ensure_ascii=False)
        print(f"\n💾 交易记录已保存：{output_file}")


def main():
    print("=" * 70)
    print("🦞 ETH 趋势行情回测 - 2024 年 10-11 月")
    print("=" * 70)
    
    # 2024 年 1 月 15 日起 30 天（ETH 从~$2500 涨到~$2900，上涨行情）
    klines = fetch_eth_klines_custom(start_date="2024-01-15", days=30)
    
    if len(klines) < 200:
        print("❌ K 线数据不足")
        return
    
    liq_by_time = generate_mock_liquidation(klines)
    
    engine = BacktestEngine(leverage=5)
    engine.run(klines, liq_by_time)
    
    print("\n✅ 回测完成！")


if __name__ == "__main__":
    main()
