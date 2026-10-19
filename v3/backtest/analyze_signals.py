#!/usr/bin/env python3
"""
信号分析脚本 - 诊断为什么信号触发这么少

分析内容：
1. 每个信号的二次打分分布
2. RR 分布
3. 被过滤的原因（打分不足/RR 不足）
"""

import sys
import json
import requests
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))

from modules.dynamic_baseline import compute as compute_baseline
from modules.scene_matcher_dynamic import match_dynamic


def fetch_eth_klines(days: int = 30, interval: str = "1h") -> list:
    """获取 ETHUSDT 历史 K 线"""
    url = "https://fapi.binance.com/fapi/v1/klines"
    params = {"symbol": "ETHUSDT", "interval": interval, "limit": days * 24}
    
    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        klines = []
        for k in data:
            klines.append([k[0], float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])])
        
        return klines
    except Exception as e:
        print(f"❌ 获取失败：{e}")
        return []


def analyze_signals(klines: list):
    """分析所有信号"""
    print("=" * 70)
    print("🔍 ETH 信号分析（过去 30 天）")
    print("=" * 70)
    
    window_size = 200
    all_signals = []
    
    for i in range(window_size, len(klines)):
        window = klines[i-window_size:i]
        mark_price = klines[i][4]
        
        bl = compute_baseline(window)
        
        result_long = match_dynamic(window, mark_price, "LONG", "oscillation", bl, "ETHUSDT")
        result_short = match_dynamic(window, mark_price, "SHORT", "oscillation", bl, "ETHUSDT")
        
        if result_long['matched']:
            all_signals.append({
                "time": klines[i][0],
                "price": mark_price,
                "direction": "LONG",
                "scene": result_long['scene'],
                "second_score": result_long['second_score'],
                "rr": result_long['rr'],
                "entry": result_long['entry_price'],
                "sl": result_long['sl_price'],
                "tp": result_long['tp_price'],
                "reason": result_long['reason'],
            })
        
        if result_short['matched']:
            all_signals.append({
                "time": klines[i][0],
                "price": mark_price,
                "direction": "SHORT",
                "scene": result_short['scene'],
                "second_score": result_short['second_score'],
                "rr": result_short['rr'],
                "entry": result_short['entry_price'],
                "sl": result_short['sl_price'],
                "tp": result_short['tp_price'],
                "reason": result_short['reason'],
            })
    
    # 统计分析
    print(f"\n【信号总览】")
    print(f"  总信号数：{len(all_signals)}")
    
    if not all_signals:
        print("  ❌ 没有信号")
        return
    
    # 二次打分分布
    scores = [s['second_score'] for s in all_signals]
    print(f"\n【二次打分分布】")
    print(f"  平均分：  {sum(scores)/len(scores):.1f}")
    print(f"  最高分：  {max(scores)}")
    print(f"  最低分：  {min(scores)}")
    print(f"  ≥70 分：   {len([s for s in scores if s >= 70])} 个 ({len([s for s in scores if s >= 70])/len(scores)*100:.1f}%)")
    print(f"  ≥60 分：   {len([s for s in scores if s >= 60])} 个 ({len([s for s in scores if s >= 60])/len(scores)*100:.1f}%)")
    print(f"  ≥50 分：   {len([s for s in scores if s >= 50])} 个 ({len([s for s in scores if s >= 50])/len(scores)*100:.1f}%)")
    
    # RR 分布
    rrs = [s['rr'] for s in all_signals]
    print(f"\n【RR 分布】")
    print(f"  平均 RR：  {sum(rrs)/len(rrs):.2f}")
    print(f"  最高 RR：  {max(rrs):.2f}")
    print(f"  最低 RR：  {min(rrs):.2f}")
    print(f"  RR≥2.0：   {len([r for r in rrs if r >= 2.0])} 个")
    print(f"  RR≥1.8：   {len([r for r in rrs if r >= 1.8])} 个")
    
    # 场景分布
    scenes = {}
    for s in all_signals:
        scene = s['scene']
        scenes[scene] = scenes.get(scene, 0) + 1
    
    print(f"\n【场景分布】")
    for scene, count in sorted(scenes.items(), key=lambda x: x[1], reverse=True):
        print(f"  {scene}: {count} 个")
    
    # 方向分布
    long_count = len([s for s in all_signals if s['direction'] == 'LONG'])
    short_count = len([s for s in all_signals if s['direction'] == 'SHORT'])
    print(f"\n【方向分布】")
    print(f"  LONG:  {long_count} 个")
    print(f"  SHORT: {short_count} 个")
    
    # 显示前 10 个信号详情
    print(f"\n【前 10 个信号详情】")
    for i, s in enumerate(all_signals[:10]):
        time_str = datetime.fromtimestamp(s['time']/1000).strftime('%m-%d %H:%M')
        print(f"{i+1}. {time_str} {s['direction']} {s['scene']} | "
              f"价格={s['price']:.2f} 打分={s['second_score']} RR={s['rr']:.2f}")
        print(f"   入场={s['entry']:.2f} SL={s['sl']:.2f} TP={s['tp']:.2f}")
        print(f"   原因：{s['reason'][:80]}...")
    
    # 保存详细数据
    output_file = Path(__file__).parent / "eth_signals_analysis.json"
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump({
            "summary": {
                "total_signals": len(all_signals),
                "avg_score": sum(scores)/len(scores),
                "max_score": max(scores),
                "min_score": min(scores),
                "score_ge_70": len([s for s in scores if s >= 70]),
                "score_ge_60": len([s for s in scores if s >= 60]),
                "avg_rr": sum(rrs)/len(rrs),
                "scenes": scenes,
            },
            "signals": all_signals,
        }, f, indent=2, ensure_ascii=False)
    
    print(f"\n💾 详细数据已保存：{output_file}")
    
    # 建议
    print(f"\n💡 优化建议：")
    if len([s for s in scores if s >= 70]) < 5:
        print(f"  ⚠️  二次打分≥70 的信号太少（{len([s for s in scores if s >= 70])}个）")
        print(f"  建议：降低门槛到 60 分，或优化打分逻辑")
    
    if len(all_signals) < 30:
        print(f"  ⚠️  总信号数过少（{len(all_signals)}个/30 天）")
        print(f"  建议：检查场景匹配条件是否过于严格")


def main():
    klines = fetch_eth_klines(days=30, interval="1h")
    
    if len(klines) < 200:
        print("❌ K 线数据不足")
        return
    
    analyze_signals(klines)


if __name__ == "__main__":
    main()
