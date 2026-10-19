#!/usr/bin/env python3
"""
诊断任务 2：B/C/F/G 场景从未触发的原因

分析 ETH 2024-01 上涨趋势中，被评分但未进入场景匹配的标的
"""

import sys
import json
import requests
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))

from modules.dynamic_baseline import compute as compute_baseline
from modules.scene_matcher import _scene_a, _scene_b, _scene_c, _scene_e


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


class SceneRejectionAnalyzer:
    """场景拒绝分析器"""
    
    def __init__(self, symbol: str):
        self.symbol = symbol
        self.rejected_signals = []
        self.triggered_signals = []
    
    def run(self, klines: list):
        """运行分析"""
        print(f"\n🔍 分析 {self.symbol} 场景拒绝原因...")
        
        window_size = 200
        
        for i in range(window_size, len(klines)):
            window = klines[i-window_size:i]
            current_k = klines[i]
            mark_price = current_k[4]
            
            bl = compute_baseline(window)
            
            # 扫描所有场景（不管是否匹配）
            result_long_a = _scene_a(window, mark_price, "LONG", bl)
            result_long_b = _scene_b(window, mark_price, "LONG", bl)
            result_long_c = _scene_c(window, mark_price, bl)
            result_short_e = _scene_e(window, mark_price, bl)
            
            # 记录所有信号（包括未匹配的）
            # 使用场景 ID 而不是返回值的 scene 字段（未匹配时是"none"）
            all_results = [
                (result_long_a, "A_pullback", "LONG"),
                (result_long_b, "B_breakout", "LONG"),
                (result_long_c, "C_first_pull", "LONG"),
                (result_short_e, "E_short", "SHORT"),
            ]
            
            for result, scene_id, direction in all_results:
                entry = {
                    "bar_index": i,
                    "time": current_k[0],
                    "mark_price": mark_price,
                    "scene": scene_id,
                    "direction": direction,
                    "second_score": result.get('second_score', 0),
                    "conditions": result.get('conditions', {}),
                    "reason": result.get('reason', ''),
                    "matched": result['matched'],
                }
                
                if result['matched']:
                    self.triggered_signals.append(entry)
                else:
                    self.rejected_signals.append(entry)
        
        print(f"✅ 分析完成：触发{len(self.triggered_signals)}笔，拒绝{len(self.rejected_signals)}笔")
    
    def print_report(self):
        """打印诊断报告"""
        print(f"\n{'='*70}")
        print(f"📊 场景拒绝分析报告 - {self.symbol}")
        print(f"{'='*70}")
        
        # 场景触发统计
        print(f"\n【场景触发统计】")
        scene_counts = {}
        for sig in self.triggered_signals:
            scene = sig['scene']
            scene_counts[scene] = scene_counts.get(scene, 0) + 1
        
        for scene, count in sorted(scene_counts.items()):
            print(f"  {scene}: {count}笔")
        
        # 场景拒绝统计
        print(f"\n【场景拒绝统计】")
        reject_counts = {}
        for sig in self.rejected_signals:
            scene = sig['scene']
            reject_counts[scene] = reject_counts.get(scene, 0) + 1
        
        for scene, count in sorted(reject_counts.items()):
            print(f"  {scene}: {count}笔被拒绝")
        
        # 重点分析 B/C 场景拒绝原因
        print(f"\n【B/C 场景拒绝原因分析】")
        
        b_rejected = [s for s in self.rejected_signals if s['scene'] == 'B_breakout']
        c_rejected = [s for s in self.rejected_signals if s['scene'] == 'C_first_pull']
        
        # 分析 B 场景
        if b_rejected:
            print(f"\n  B_breakout 场景（{len(b_rejected)}笔被拒绝）:")
            
            # 统计失败条件
            cond_failures = {}
            for sig in b_rejected[:50]:  # 只分析前 50 笔
                for cond_name, cond_value in sig.get('conditions', {}).items():
                    if isinstance(cond_value, bool) and not cond_value:
                        cond_failures[cond_name] = cond_failures.get(cond_name, 0) + 1
            
            print(f"    最常见失败条件:")
            for cond, count in sorted(cond_failures.items(), key=lambda x: -x[1])[:5]:
                print(f"      {cond}: {count}次")
            
            # 分析具体数值差距
            print(f"    典型案例分析（前 5 笔）:")
            for i, sig in enumerate(b_rejected[:5]):
                print(f"      #{i+1}: {sig['reason'][:100]}")
        
        # 分析 C 场景
        if c_rejected:
            print(f"\n  C_first_pull 场景（{len(c_rejected)}笔被拒绝）:")
            
            # 统计失败条件
            cond_failures = {}
            for sig in c_rejected[:50]:
                for cond_name, cond_value in sig.get('conditions', {}).items():
                    if isinstance(cond_value, bool) and not cond_value:
                        cond_failures[cond_name] = cond_failures.get(cond_name, 0) + 1
            
            print(f"    最常见失败条件:")
            for cond, count in sorted(cond_failures.items(), key=lambda x: -x[1])[:5]:
                print(f"      {cond}: {count}次")
            
            # 分析具体数值差距
            print(f"    典型案例分析（前 5 笔）:")
            for i, sig in enumerate(c_rejected[:5]):
                print(f"      #{i+1}: {sig['reason'][:100]}")
        
        # 导出 JSON
        output_file = Path(__file__).parent / "scene_rejection_analysis.json"
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump({
                "symbol": self.symbol,
                "triggered_signals": self.triggered_signals,
                "rejected_signals": self.rejected_signals,
                "summary": {
                    "triggered_count": len(self.triggered_signals),
                    "rejected_count": len(self.rejected_signals),
                    "scene_counts": scene_counts,
                    "reject_counts": reject_counts,
                }
            }, f, ensure_ascii=False, indent=2, default=str)
        print(f"\n✅ 详细数据已导出：{output_file}")


def main():
    """主函数"""
    print("="*70)
    print("🧪 诊断任务 2：B/C/F/G 场景未触发原因")
    print("="*70)
    
    # ETH 2024-01
    eth_klines = fetch_klines("ETHUSDT", "2024-01-01", "2024-01-31", "1h")
    
    if not eth_klines:
        print("❌ 无法获取 ETH 数据，退出诊断")
        return
    
    analyzer = SceneRejectionAnalyzer("ETHUSDT")
    analyzer.run(eth_klines)
    analyzer.print_report()


if __name__ == "__main__":
    main()
