#!/usr/bin/env python3
"""
分析任务 1：入场时机标注分析

对 ETH 7 月 + PEPE 7 月的所有回测交易进行标注：
- 入场 K 线形态（阳线/阴线、上下影线比例、成交量）
- 确认信号数量
- 入场来源（liq_zone/structure/rr_fallback）

统计不同确认数量下的胜率差异
"""

import sys
import json
import requests
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))

from modules.dynamic_baseline import compute as compute_baseline
from modules.scene_matcher import match


def fetch_klines(symbol: str, start_date: str, end_date: str, interval: str = "1h", as_dict: bool = False) -> list:
    """获取历史 K 线
    
    Args:
        as_dict: True=返回字典格式，False=返回列表格式（兼容 dynamic_baseline）
    """
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
            if as_dict:
                klines.append({
                    "time": k[0],
                    "open": float(k[1]),
                    "high": float(k[2]),
                    "low": float(k[3]),
                    "close": float(k[4]),
                    "volume": float(k[5])
                })
            else:
                # 列表格式兼容 dynamic_baseline
                klines.append([
                    k[0], float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])
                ])
        
        print(f"✅ 获取成功：{len(klines)} 根 K 线")
        return klines
    except Exception as e:
        print(f"❌ 获取失败：{e}")
        return []


def analyze_candle(candle: dict, prev_candles: list, direction: str) -> dict:
    """
    分析单根 K 线形态
    
    Returns:
        {
            "direction": "阳线/阴线",
            "body": 实体大小,
            "lower_shadow_ratio": 下影线/实体,
            "upper_shadow_ratio": 上影线/实体,
            "volume_vs_avg3": 当根成交量/前 3 根均值,
            "confirmations": ["阳线收盘", "长下影线", ...],
            "confirmation_count": 0~3
        }
    """
    open_p = candle["open"]
    close_p = candle["close"]
    high_p = candle["high"]
    low_p = candle["low"]
    volume = candle["volume"]
    
    # K 线方向
    is_bullish = close_p > open_p
    candle_direction = "阳线" if is_bullish else "阴线"
    
    # 实体大小
    body = abs(close_p - open_p)
    
    # 上下影线
    if is_bullish:
        lower_shadow = open_p - low_p
        upper_shadow = high_p - close_p
    else:
        lower_shadow = close_p - low_p
        upper_shadow = high_p - open_p
    
    # 影线/实体比例
    lower_shadow_ratio = lower_shadow / body if body > 0 else 0
    upper_shadow_ratio = upper_shadow / body if body > 0 else 0
    
    # 成交量对比
    if len(prev_candles) >= 3:
        avg_vol_3 = sum(c["volume"] for c in prev_candles[-3:]) / 3
        volume_vs_avg3 = volume / avg_vol_3 if avg_vol_3 > 0 else 1.0
    else:
        volume_vs_avg3 = 1.0
    
    # 确认信号
    confirmations = []
    
    if direction == "LONG":
        # 确认 1：阳线收盘
        if is_bullish:
            confirmations.append("阳线收盘")
        
        # 确认 2：长下影线（锤子形态）
        if lower_shadow_ratio > 1.5:
            confirmations.append("长下影线（锤子形态）")
        
        # 确认 3：止跌放量
        if volume_vs_avg3 > 1.2:
            confirmations.append("止跌放量")
    
    elif direction == "SHORT":
        # 确认 1：阴线收盘
        if not is_bullish:
            confirmations.append("阴线收盘")
        
        # 确认 2：长上影线（射击之星）
        if upper_shadow_ratio > 1.5:
            confirmations.append("长上影线（射击之星）")
        
        # 确认 3：见顶放量
        if volume_vs_avg3 > 1.2:
            confirmations.append("见顶放量")
    
    return {
        "direction": candle_direction,
        "body": round(body, 8),
        "lower_shadow_ratio": round(lower_shadow_ratio, 2),
        "upper_shadow_ratio": round(upper_shadow_ratio, 2),
        "volume_vs_avg3": round(volume_vs_avg3, 2),
        "confirmations": confirmations,
        "confirmation_count": len(confirmations)
    }


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


class EntryTimingAnalyzer:
    """入场时机分析器"""
    
    def __init__(self, symbol: str, leverage: int = 5):
        self.symbol = symbol
        self.leverage = leverage
        self.initial_capital = 10000
        self.capital = self.initial_capital
        
        # 交易记录（带标注）
        self.trades = []
        self.current_position = None
        self.pending_orders = []
        self.klines_dict = None  # 字典格式 K 线（用于形态分析）
    
    def run(self, klines: list, btc_4h: list = None, eth_4h: list = None, klines_dict: list = None):
        """运行分析
        
        Args:
            klines: 列表格式 K 线（用于回测）
            klines_dict: 字典格式 K 线（用于形态分析）
        """
        print(f"\n🔍 分析 {self.symbol} 入场时机...")
        
        # 保存字典格式 K 线用于形态分析
        self.klines_dict = klines_dict if klines_dict else []
        
        window_size = 200
        
        for i in range(window_size, len(klines)):
            window = klines[i-window_size:i]
            current_k = klines[i]
            # 列表格式：[time, open, high, low, close, volume]
            mark_price = current_k[4] if isinstance(current_k, list) else current_k["close"]
            
            bl = compute_baseline(window)
            
            # 扫描信号
            result_long = match(window, mark_price, "LONG", "oscillation", bl,
                               self.symbol, enable_trend_filter=True,
                               btc_closes_4h=btc_4h, eth_closes_4h=eth_4h)
            result_short = match(window, mark_price, "SHORT", "oscillation", bl,
                                self.symbol, enable_trend_filter=True,
                                btc_closes_4h=btc_4h, eth_closes_4h=eth_4h)
            
            # 尝试开仓
            if result_long['matched'] and not self.current_position:
                self._try_open_position(result_long, klines, i)
            
            if result_short['matched'] and not self.current_position:
                self._try_open_position(result_short, klines, i)
            
            # 检查挂单成交
            self._check_pending_orders(klines, i)
            
            # 检查现有仓位
            if self.current_position:
                self._check_position(klines, i)
        
        self._finalize()
    
    def _try_open_position(self, result: dict, klines: list, bar_index: int):
        """尝试开仓"""
        entry = result['entry_price']
        sl = result['sl_price']
        tp = result['tp_price']
        second_score = result['second_score']
        scene = result['scene']
        
        position_size_usd = calc_position_size_usd(second_score)
        if position_size_usd == 0:
            return
        
        position_value = position_size_usd * self.leverage
        contract_size = position_value / entry
        
        current_k = klines[bar_index]
        current_price = current_k[4] if isinstance(current_k, list) else current_k["close"]
        if result['direction'] == 'LONG':
            if entry > current_price * 1.02:
                return
        else:
            if entry < current_price * 0.98:
                return
        
        # 获取入场来源
        entry_source = "structure"
        conditions = result.get('conditions', {})
        if conditions.get('entry_method'):
            if 'liq' in conditions['entry_method'].lower():
                entry_source = "liq_zone"
            elif 'rr' in conditions['entry_method'].lower():
                entry_source = "rr_fallback"
        
        self.pending_orders.append({
            "direction": result['direction'],
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "size": contract_size,
            "position_size_usd": position_size_usd,
            "second_score": second_score,
            "scene": scene,
            "entry_source": entry_source,
            "open_bar_index": bar_index,
            "open_time": klines[bar_index][0] if isinstance(klines[bar_index], list) else klines[bar_index]["time"],
        })
    
    def _check_pending_orders(self, klines: list, current_bar_index: int):
        """检查挂单成交"""
        filled_orders = []
        for order in self.pending_orders:
            filled = False
            fill_bar_index = current_bar_index
            
            current_k = klines[current_bar_index]
            low = current_k[3] if isinstance(current_k, list) else current_k["low"]
            high = current_k[2] if isinstance(current_k, list) else current_k["high"]
            
            if order['direction'] == 'LONG':
                if low <= order['entry']:
                    filled = True
            else:
                if high >= order['entry']:
                    filled = True
            
            if filled:
                # 分析入场 K 线形态（使用字典格式）
                if self.klines_dict and len(self.klines_dict) > fill_bar_index:
                    entry_candle = self.klines_dict[fill_bar_index]
                    prev_candles = self.klines_dict[max(0, fill_bar_index-4):fill_bar_index]
                else:
                    # 回退：从列表格式转换
                    k = klines[fill_bar_index]
                    entry_candle = {
                        "time": k[0], "open": k[1], "high": k[2],
                        "low": k[3], "close": k[4], "volume": k[5]
                    }
                    prev_candles = [{
                        "time": k[0], "open": k[1], "high": k[2],
                        "low": k[3], "close": k[4], "volume": k[5]
                    } for k in klines[max(0, fill_bar_index-4):fill_bar_index]]
                
                candle_analysis = analyze_candle(
                    entry_candle, prev_candles, order['direction']
                )
                
                self.current_position = {
                    **order,
                    "entry_bar_index": fill_bar_index,
                    "entry_candle_analysis": candle_analysis,
                }
                filled_orders.append(order)
        
        for order in filled_orders:
            self.pending_orders.remove(order)
        
        if self.current_position:
            self.pending_orders = []
    
    def _check_position(self, klines: list, current_bar_index: int):
        """检查仓位状态"""
        if not self.current_position:
            return
        
        pos = self.current_position
        current_k = klines[current_bar_index]
        high = current_k[2] if isinstance(current_k, list) else current_k["high"]
        low = current_k[3] if isinstance(current_k, list) else current_k["low"]
        
        if pos['direction'] == 'LONG':
            if low <= pos['sl']:
                self._close_position("loss", current_k)
                return
            if high >= pos['tp']:
                self._close_position("win", current_k)
                return
        else:
            if high >= pos['sl']:
                self._close_position("loss", current_k)
                return
            if low <= pos['tp']:
                self._close_position("win", current_k)
                return
    
    def _close_position(self, result: str, exit_candle):
        """平仓"""
        exit_price = exit_candle[4] if isinstance(exit_candle, list) else exit_candle["close"]
        trade = {
            "symbol": self.symbol,
            "direction": self.current_position['direction'],
            "entry": self.current_position['entry'],
            "exit": exit_price,
            "result": result,
            "second_score": self.current_position['second_score'],
            "position_size_usd": self.current_position['position_size_usd'],
            "scene": self.current_position['scene'],
            "entry_source": self.current_position['entry_source'],
            "entry_candle_analysis": self.current_position['entry_candle_analysis'],
        }
        self.trades.append(trade)
        self.current_position = None
    
    def _finalize(self):
        """分析结束"""
        if self.current_position:
            self._close_position("end", {"close": 0})
    
    def print_report(self):
        """打印分析报告"""
        print(f"\n{'='*70}")
        print(f"📊 {self.symbol} 入场时机分析报告")
        print(f"{'='*70}")
        
        total_trades = len(self.trades)
        if total_trades == 0:
            print("⚠️ 无交易记录")
            return
        
        # 按确认数量分组统计
        by_confirmation = {0: [], 1: [], 2: [], 3: []}
        for trade in self.trades:
            count = trade['entry_candle_analysis']['confirmation_count']
            if count >= 3:
                by_confirmation[3].append(trade)
            else:
                by_confirmation[count].append(trade)
        
        print(f"\n【按确认数量分组统计】")
        print(f"{'确认数':<8} {'交易数':<10} {'盈利':<8} {'亏损':<8} {'胜率':<10} {'平均盈亏':<12}")
        print("-"*60)
        
        for count in range(4):
            trades = by_confirmation[count]
            if len(trades) == 0:
                continue
            
            wins = sum(1 for t in trades if t['result'] == 'win')
            win_rate = wins / len(trades) * 100
            
            # 估算盈亏（简化）
            avg_pnl = "N/A"
            
            print(f"{count:<8} {len(trades):<10} {wins:<8} {len(trades)-wins:<8} {win_rate:<10.1f}% {avg_pnl:<12}")
        
        # 按入场来源分组统计
        by_source = {"liq_zone": [], "structure": [], "rr_fallback": []}
        for trade in self.trades:
            source = trade['entry_source']
            if source in by_source:
                by_source[source].append(trade)
        
        print(f"\n【按入场来源分组统计】")
        print(f"{'来源':<15} {'交易数':<10} {'盈利':<8} {'亏损':<8} {'胜率':<10}")
        print("-"*55)
        
        for source, trades in by_source.items():
            if len(trades) == 0:
                continue
            
            wins = sum(1 for t in trades if t['result'] == 'win')
            win_rate = wins / len(trades) * 100
            print(f"{source:<15} {len(trades):<10} {wins:<8} {len(trades)-wins:<8} {win_rate:<10.1f}%")
        
        # 详细交易记录
        print(f"\n【详细交易记录】")
        for i, trade in enumerate(self.trades):
            candle = trade['entry_candle_analysis']
            print(f"\n交易{i+1}: {trade['symbol']} {trade['direction']} | {trade['result']}")
            print(f"  打分={trade['second_score']} 场景={trade['scene']} 来源={trade['entry_source']}")
            print(f"  K 线={candle['direction']} 下影线/实体={candle['lower_shadow_ratio']} 上影线/实体={candle['upper_shadow_ratio']}")
            print(f"  成交量/前 3 均={candle['volume_vs_avg3']} 确认数={candle['confirmation_count']} 确认={candle['confirmations']}")
        
        # 导出 JSON
        output_file = Path(__file__).parent / f"{self.symbol}_entry_timing_analysis.json"
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(self.trades, f, ensure_ascii=False, indent=2)
        print(f"\n✅ 详细数据已导出：{output_file}")


def main():
    """主函数"""
    print("="*70)
    print("🧪 分析任务 1：入场时机标注分析")
    print("="*70)
    
    # ETH 7 月 - 列表格式用于回测
    eth_klines = fetch_klines("ETHUSDT", "2026-07-01", "2026-07-31", "1h", as_dict=False)
    # ETH 7 月 - 字典格式用于 K 线分析
    eth_klines_dict = fetch_klines("ETHUSDT", "2026-07-01", "2026-07-31", "1h", as_dict=True)
    
    btc_4h = fetch_klines("BTCUSDT", "2026-06-20", "2026-07-31", "4h", as_dict=False)
    eth_4h = fetch_klines("ETHUSDT", "2026-06-20", "2026-07-31", "4h", as_dict=False)
    
    btc_4h_closes = [k[4] for k in btc_4h] if btc_4h else []
    eth_4h_closes = [k[4] for k in eth_4h] if eth_4h else []
    
    analyzer_eth = EntryTimingAnalyzer("ETHUSDT")
    analyzer_eth.run(eth_klines, btc_4h_closes, eth_4h_closes, eth_klines_dict)
    analyzer_eth.print_report()
    
    # PEPE 7 月
    pepe_klines = fetch_klines("1000PEPEUSDT", "2026-07-01", "2026-07-31", "1h", as_dict=False)
    pepe_klines_dict = fetch_klines("1000PEPEUSDT", "2026-07-01", "2026-07-31", "1h", as_dict=True)
    
    analyzer_pepe = EntryTimingAnalyzer("1000PEPEUSDT")
    analyzer_pepe.run(pepe_klines, btc_4h_closes, eth_4h_closes, pepe_klines_dict)
    analyzer_pepe.print_report()


if __name__ == "__main__":
    main()
