#!/usr/bin/env python3
"""
ETH 回测脚本 - 过去一个月数据

测试内容：
1. 扫描器触发信号次数
2. 开仓挂单成交次数
3. 止盈/止损触发情况
4. 最终盈亏结果

数据范围：2026-07-03 ~ 2026-08-03（30 天）
时间周期：1 小时 K 线
"""

import sys
import json
import requests
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Dict, Any

# 添加模块路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from modules.dynamic_baseline import compute as compute_baseline
from modules.scene_matcher_dynamic import match_dynamic


# ══════════════════════════════════════════════════════════════════════════════
# 数据获取
# ══════════════════════════════════════════════════════════════════════════════

def fetch_eth_klines(days: int = 30, interval: str = "1h") -> List[list]:
    """
    获取 ETHUSDT 历史 K 线数据（列表格式）
    
    使用 Binance 公共 API（无需鉴权）
    返回格式：[[time, open, high, low, close, vol], ...]
    """
    print(f"📊 获取 ETHUSDT 历史 K 线（过去 {days} 天，{interval}）...")
    
    url = "https://fapi.binance.com/fapi/v1/klines"
    params = {
        "symbol": "ETHUSDT",
        "interval": interval,
        "limit": days * 24
    }
    
    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        klines = []
        for k in data:
            # 列表格式：[time, open, high, low, close, vol]
            klines.append([
                k[0],  # time
                float(k[1]),  # open
                float(k[2]),  # high
                float(k[3]),  # low
                float(k[4]),  # close
                float(k[5]),  # volume
            ])
        
        print(f"✅ 获取成功：{len(klines)} 根 K 线")
        if klines:
            start_time = datetime.fromtimestamp(klines[0][0]/1000)
            end_time = datetime.fromtimestamp(klines[-1][0]/1000)
            min_price = min(k[3] for k in klines)
            max_price = max(k[2] for k in klines)
            print(f"   时间范围：{start_time} ~ {end_time}")
            print(f"   价格范围：{min_price:.2f} ~ {max_price:.2f}")
        
        return klines
    
    except Exception as e:
        print(f"❌ 获取 K 线失败：{e}")
        return generate_mock_klines(days * 24)


def generate_mock_klines(count: int = 720, base_price: float = 3500):
    """生成模拟 K 线（降级方案）"""
    import random
    klines = []
    price = base_price
    base_time = int((datetime.now() - timedelta(days=30)).timestamp() * 1000)
    
    for i in range(count):
        change = random.uniform(-0.03, 0.03)
        close = price * (1 + change)
        high = max(price, close) * (1 + random.uniform(0, 0.02))
        low = min(price, close) * (1 - random.uniform(0, 0.02))
        open_p = price
        volume = random.uniform(10000, 50000)
        
        klines.append([
            base_time + i * 3600000,
            open_p,
            high,
            low,
            close,
            volume,
        ])
        price = close
    
    print(f"⚠️ 使用模拟数据：{len(klines)} 根 K 线")
    return klines


# ══════════════════════════════════════════════════════════════════════════════
# 回测引擎
# ══════════════════════════════════════════════════════════════════════════════

class BacktestEngine:
    """回测引擎"""
    
    def __init__(self, initial_capital: float = 10000, leverage: int = 5):
        self.initial_capital = initial_capital
        self.capital = initial_capital
        self.leverage = leverage
        
        # 统计
        self.signals_triggered = 0
        self.orders_placed = 0
        self.orders_filled = 0
        self.tp_hits = 0
        self.sl_hits = 0
        self.blocked_signals = 0
        
        # 交易记录
        self.trades = []
        self.current_position = None
        self.logs = []
    
    def log(self, msg: str):
        self.logs.append(msg)
    
    def run(self, klines: List[list]):
        """运行回测"""
        print("\n🚀 开始回测...")
        print(f"   初始资金：${self.initial_capital:,.2f}")
        print(f"   杠杆倍数：{self.leverage}x")
        print(f"   K 线数量：{len(klines)}")
        
        window_size = 200
        
        for i in range(window_size, len(klines)):
            window = klines[i-window_size:i]
            current_k = klines[i]
            mark_price = current_k[4]
            
            bl = compute_baseline(window)
            
            result_long = match_dynamic(window, mark_price, "LONG", "oscillation", bl, "ETHUSDT")
            result_short = match_dynamic(window, mark_price, "SHORT", "oscillation", bl, "ETHUSDT")
            
            if result_long['matched'] and not self.current_position:
                self.signals_triggered += 1
                self._try_open_position(result_long, current_k, "LONG")
            
            if result_short['matched'] and not self.current_position:
                self.signals_triggered += 1
                self._try_open_position(result_short, current_k, "SHORT")
            
            if self.current_position:
                self._check_position(current_k)
        
        self._finalize()
    
    def _try_open_position(self, result: Dict, kline: list, direction: str):
        """尝试开仓"""
        self.orders_placed += 1
        self.orders_filled += 1
        
        entry = result['entry_price']
        sl = result['sl_price']
        tp = result['tp_price']
        tp1 = result.get('tp1_price')
        tp2 = result.get('tp2_price')
        
        self.current_position = {
            "direction": direction,
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "tp1": tp1,
            "tp2": tp2,
            "size": self.capital * self.leverage / entry,
            "open_time": kline[0],
            "open_price": kline[4],
        }
        
        self.log(f"🦞 开仓 {direction} @ {entry:.2f} | SL={sl:.2f} TP={tp:.2f}")
    
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
                    self.log(f"✅ 止盈 1/2 @ {pos['tp1']:.2f} | PnL={pnl1:.2f}")
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
                    self.log(f"✅ 止盈 1/2 @ {pos['tp1']:.2f} | PnL={pnl1:.2f}")
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
        }
        self.trades.append(trade)
        
        status = "✅" if pnl > 0 else "❌"
        self.log(f"{status} 平仓 {reason} @ {kline[4]:.2f} | PnL={pnl:.2f}")
        
        self.current_position = None
    
    def _finalize(self):
        """回测结束"""
        if self.current_position:
            self.log(f"⚠️ 回测结束仍有未平仓位")
        
        self._print_report()
    
    def _print_report(self):
        """打印回测报告"""
        print("\n" + "=" * 70)
        print("📊 ETH 回测报告（过去 30 天）")
        print("=" * 70)
        
        print(f"\n【交易统计】")
        print(f"  信号触发次数：{self.signals_triggered}")
        print(f"  挂单次数：    {self.orders_placed}")
        print(f"  成交次数：    {self.orders_filled}")
        print(f"  止盈触发：    {self.tp_hits}")
        print(f"  止损触发：    {self.sl_hits}")
        
        if self.orders_filled > 0:
            win_rate = self.tp_hits / self.orders_filled * 100
            print(f"  胜率：        {win_rate:.1f}%")
        
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
        output_file = Path(__file__).parent / "eth_backtest_trades.json"
        with open(output_file, 'w') as f:
            json.dump({
                "trades": self.trades,
                "logs": self.logs[-50:],
            }, f, indent=2, ensure_ascii=False)
        print(f"\n💾 交易记录已保存：{output_file}")


def main():
    print("=" * 70)
    print("🦞 ETH 回测 - 过去 30 天数据")
    print("=" * 70)
    
    klines = fetch_eth_klines(days=30, interval="1h")
    
    if len(klines) < 200:
        print("❌ K 线数据不足，无法回测")
        return
    
    engine = BacktestEngine(initial_capital=10000, leverage=5)
    engine.run(klines)
    
    print("\n✅ 回测完成！")


if __name__ == "__main__":
    main()
