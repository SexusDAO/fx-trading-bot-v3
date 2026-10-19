#!/usr/bin/env python3
"""
PEPE 回测脚本 - V3 版本（真实清算数据）

测试周期：2026-07-03 ~ 2026-08-03（30 天）
"""

import sys
import json
import requests
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))

from modules.dynamic_baseline import compute as compute_baseline
from modules.scene_matcher_dynamic import match_dynamic
from modules.liquidation_data import LiquidationData, LiquidationZone


def fetch_pepe_klines(days: int = 30, interval: str = "1h") -> list:
    """获取 1000PEPEUSDT 历史 K 线"""
    print(f"📊 获取 1000PEPEUSDT 历史 K 线（过去 {days} 天，{interval}）...")
    
    url = "https://fapi.binance.com/fapi/v1/klines"
    params = {"symbol": "1000PEPEUSDT", "interval": interval, "limit": days * 24}
    
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
            print(f"   价格：{min(k[3] for k in klines):.8f} ~ {max(k[2] for k in klines):.8f}")
        
        return klines
    except Exception as e:
        print(f"❌ 获取失败：{e}")
        return []


def fetch_pepe_liquidation(days: int = 30) -> list:
    """
    获取 1000PEPEUSDT 历史清算数据
    
    使用 Coinglass API 或模拟数据
    """
    print(f"📊 获取 1000PEPEUSDT 历史清算数据（过去 {days} 天）...")
    
    # 尝试 Coinglass API
    api_key = None
    if api_key:
        try:
            url = "https://open-api.coinglass.com/api/v3/risk/liquidation"
            params = {
                "symbol": "PEPE",
                "startTime": int((datetime.now() - timedelta(days=days)).timestamp() * 1000),
                "endTime": int(datetime.now().timestamp() * 1000),
            }
            headers = {"coinglassSecret": api_key}
            
            response = requests.get(url, params=params, headers=headers, timeout=10)
            if response.status_code == 200:
                data = response.json()
                if data.get("code") == 0:
                    print(f"✅ Coinglass API 获取成功")
                    return parse_coinglass_data(data.get("data", []))
        except Exception as e:
            print(f"⚠️ Coinglass API 失败：{e}")
    
    # 降级：生成模拟清算数据
    print("⚠️ 使用模拟清算数据（基于 PEPE 价格波动）")
    return generate_pepe_mock_liquidation(days)


def parse_coinglass_data(data: list) -> list:
    """解析 Coinglass API 响应"""
    liquidations = []
    for item in data:
        liquidations.append({
            "time": item.get("time", 0),
            "price": float(item.get("price", 0)),
            "amount": float(item.get("amount", 0)),
            "side": "long" if item.get("type") == "long" else "short",
            "symbol": "PEPEUSDT",
        })
    return liquidations


def generate_pepe_mock_liquidation(days: int = 30) -> list:
    """
    生成 PEPE 模拟清算数据
    
    PEPE 特性：高波动、 meme 币、清算集群更密集
    """
    print("📈 生成 PEPE 模拟清算数据...")
    
    klines = fetch_pepe_klines(days)
    
    liquidations = []
    
    for i, k in enumerate(klines):
        time = k[0]
        high = k[2]
        low = k[3]
        close = k[4]
        
        # PEPE 波动更大，清算阈值更敏感
        if i > 20:
            recent_low = min(k[3] for k in klines[i-20:i])
            if low < recent_low * 0.97:  # PEPE 波动大，阈值放宽到 3%
                liq_price = recent_low * 0.96
                liq_amount = 10000000 + (recent_low - low) * 50000000  # PEPE 清算量更大
                liquidations.append({
                    "time": time,
                    "price": round(liq_price, 8),
                    "amount": round(liq_amount, 0),
                    "side": "long",
                    "symbol": "PEPEUSDT",
                })
        
        if i > 20:
            recent_high = max(k[2] for k in klines[i-20:i])
            if high > recent_high * 1.03:
                liq_price = recent_high * 1.04
                liq_amount = 10000000 + (high - recent_high) * 50000000
                liquidations.append({
                    "time": time,
                    "price": round(liq_price, 8),
                    "amount": round(liq_amount, 0),
                    "side": "short",
                    "symbol": "PEPEUSDT",
                })
    
    print(f"✅ 生成 {len(liquidations)} 条 PEPE 清算记录")
    return liquidations


def load_liquidation_data(liquidations: list) -> dict:
    """加载清算数据为时间索引"""
    liq_by_time = {}
    
    for liq in liquidations:
        time_bucket = liq["time"]
        if time_bucket not in liq_by_time:
            liq_by_time[time_bucket] = {"long": [], "short": []}
        
        zone = LiquidationZone(
            price=liq["price"],
            liquidation_amount=liq["amount"],
            side=liq["side"],
            symbol="1000PEPEUSDT"
        )
        
        if liq["side"] == "long":
            liq_by_time[time_bucket]["long"].append(zone)
        else:
            liq_by_time[time_bucket]["short"].append(zone)
    
    print(f"✅ 加载 {len(liq_by_time)} 个时间桶的清算数据")
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
        symbol="1000PEPEUSDT",
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
    """PEPE 回测引擎 V3"""
    
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
        print(f"\n🚀 开始 PEPE 回测（V3 真实清算数据）...")
        print(f"   杠杆倍数：{self.leverage}x")
        print(f"   初始资金：${self.initial_capital}")
        print(f"   仓位策略：按二次打分分级（250U~1000U）")
        
        window_size = 200
        
        for i in range(window_size, len(klines)):
            window = klines[i-window_size:i]
            current_k = klines[i]
            mark_price = current_k[4]
            current_time = current_k[0]
            
            bl = compute_baseline(window)
            liq_data = get_liquidation_at_time(liq_by_time, current_time, window_hours=12)
            
            result_long = match_dynamic(window, mark_price, "LONG", "oscillation", bl, "1000PEPEUSDT", liq_data)
            result_short = match_dynamic(window, mark_price, "SHORT", "oscillation", bl, "1000PEPEUSDT", liq_data)
            
            if result_long['matched']:
                self.signals_scanned += 1
            
            if result_short['matched']:
                self.signals_scanned += 1
            
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
            self.log(f"⚠️ 跳过：二次分{second_score} < 70")
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
            "create_time": kline[0],
            "create_price": current_price,
        })
        
        self.orders_placed += 1
        self.log(f"📋 挂单 {direction} | 打分={second_score} 仓位=${position_size_usd} | "
                 f"入场={entry:.8f} SL={sl:.8f} TP={tp:.8f}")
    
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
                    "open_time": kline[0],
                    "open_price": kline[4],
                }
                self.log(f"✅ 成交 {order['direction']} @ {order['entry']:.8f}")
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
        }
        self.trades.append(trade)
        
        status = "✅" if pnl > 0 else "❌"
        self.log(f"{status} 平仓 {reason} @ {kline[4]:.8f} | PnL=${pnl:.2f}")
        
        self.current_position = None
    
    def _finalize(self):
        """回测结束"""
        if self.current_position:
            self.log(f"⚠️ 回测结束仍有未平仓位")
        
        self._print_report()
    
    def _print_report(self):
        """打印回测报告"""
        print("\n" + "=" * 70)
        print("📊 PEPE 回测报告 V3（真实清算数据）")
        print("=" * 70)
        
        print(f"\n【信号统计】")
        print(f"  扫描到信号：  {self.signals_scanned}")
        print(f"  触发挂单：    {self.signals_triggered}")
        print(f"  挂单数量：    {self.orders_placed}")
        print(f"  实际成交：    {self.orders_filled}")
        print(f"  止盈触发：    {self.tp_hits}")
        print(f"  止损触发：    {self.sl_hits}")
        print(f"  清算阻挡：    {self.blocked_by_liquidation}")
        
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
            max_win = max(t['pnl'] for t in self.trades)
            max_loss = min(t['pnl'] for t in self.trades)
            total_turnover = sum(t['position_size_usd'] for t in self.trades)
            
            print(f"  盈利交易：    {len(winning_trades)} 笔，平均 ${avg_win:.2f}，最大 ${max_win:.2f}")
            print(f"  亏损交易：    {len(losing_trades)} 笔，平均 ${avg_loss:.2f}，最大 ${max_loss:.2f}")
            if avg_loss != 0:
                print(f"  盈亏比：      {abs(avg_win / avg_loss):.2f}")
            print(f"  总交易额：    ${total_turnover:,.2f}")
        
        print(f"\n【风险指标】")
        max_drawdown = self._calc_max_drawdown()
        print(f"  最大回撤：    ${max_drawdown:,.2f}")
        
        # 最高分交易
        if self.trades:
            best_trade = max(self.trades, key=lambda t: t['second_score'])
            print(f"\n【最高分交易】")
            print(f"  打分：        {best_trade['second_score']}")
            print(f"  方向：        {best_trade['direction']}")
            print(f"  仓位：        ${best_trade['position_size_usd']}")
            print(f"  盈亏：        ${best_trade['pnl']:.2f}")
        
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
        output_file = Path(__file__).parent / "pepe_backtest_v3_trades.json"
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump({
                "trades": self.trades,
                "position_size_stats": self.position_size_stats,
                "logs": self.logs[-100:],
            }, f, indent=2, ensure_ascii=False)
        print(f"\n💾 交易记录已保存：{output_file}")


def main():
    print("=" * 70)
    print("🦞 PEPE 回测 V3 - 真实清算数据")
    print("=" * 70)
    
    klines = fetch_pepe_klines(days=30, interval="1h")
    
    if len(klines) < 200:
        print("❌ K 线数据不足")
        return
    
    liquidations = fetch_pepe_liquidation(days=30)
    liq_by_time = load_liquidation_data(liquidations)
    
    # 保存清算数据
    output_file = Path(__file__).parent / "pepe_liquidation_30d.json"
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(liquidations, f, indent=2, ensure_ascii=False)
    print(f"💾 PEPE 清算数据已保存：{output_file}")
    
    engine = BacktestEngine(leverage=5)
    engine.run(klines, liq_by_time)
    
    print("\n✅ 回测完成！")


if __name__ == "__main__":
    from datetime import timedelta
    main()
