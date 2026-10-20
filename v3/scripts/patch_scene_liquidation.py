#!/usr/bin/env python3
"""
批量补丁脚本：将 scene_matcher_dynamic.py 中所有场景的止盈止损
替换为清算区版本（带降级逻辑）
"""

import re
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).parent.parent
SCENE_FILE = WORKSPACE_ROOT / "modules" / "scene_matcher_dynamic.py"

def patch_scene_a(content: str) -> str:
    """补丁场景 A"""
    # 找到场景 A 的止盈止损部分并替换
    old_pattern = r'''    # 集成清算区止盈止损
    if LIQUIDATION_ENABLED and liq_data:
        sl_result = calc_sl_with_liquidation\(
            entry=entry, direction=direction, scene="A_pullback",
            candles=k1h, baseline=bl, liq_data=liq_data
        \)
        sl = sl_result\["sl_price"\]
        
        tp_result = calc_tp_with_liquidation\(
            entry=entry, sl=sl, direction=direction,
            candles=k1h, baseline=bl, liq_data=liq_data
        \)
        tp = tp_result\["tp_price"\]
    else:
        # 降级：固定 RR 计算
        if direction == "LONG":
            sl = round\(min\(lows\[-5:\]\) - 0\.5 \* bl\.atr_baseline, 8\)
            risk = abs\(entry - sl\)
            tp = round\(entry \+ risk \* 2\.5, 8\)
        else:
            sl = round\(max\(highs\[-5:\]\) \+ 0\.5 \* bl\.atr_baseline, 8\)
            risk = abs\(entry - sl\)
            tp = round\(entry - risk \* 2\.5, 8\)'''
    
    # 已经补丁过了，跳过
    return content


def patch_scene_c(content: str) -> str:
    """补丁场景 C（分批止盈）"""
    old_code = '''    recent_low = min(lows[-5:])
    sl = round(recent_low - bl.atr_baseline * 0.25, 8)
    
    risk = abs(entry - sl)
    tp1 = round(swing_high, 8)  # 第一目标：前高
    tp2 = round(swing_high + risk * 0.5, 8)  # 第二目标：前高 +0.5×风险'''
    
    new_code = '''    # 集成清算区止盈止损
    if LIQUIDATION_ENABLED and liq_data:
        sl_result = calc_sl_with_liquidation(
            entry=entry, direction=direction, scene="C_first_pull",
            candles=k1h, baseline=bl, liq_data=liq_data
        )
        sl = sl_result["sl_price"]
        
        tp_result = calc_batch_tp_with_liquidation(
            entry=entry, sl=sl, direction=direction,
            candles=k1h, baseline=bl, liq_data=liq_data
        )
        tp1 = tp_result["tp1_price"]
        tp2 = tp_result["tp2_price"]
    else:
        # 降级：固定计算
        recent_low = min(lows[-5:])
        sl = round(recent_low - bl.atr_baseline * 0.25, 8)
        risk = abs(entry - sl)
        tp1 = round(swing_high, 8)
        tp2 = round(swing_high + risk * 0.5, 8)'''
    
    return content.replace(old_code, new_code)


def patch_scene_g(content: str) -> str:
    """补丁场景 G（分批止盈）"""
    old_code = '''    recent_high = max(highs[-5:])
    sl = round(recent_high + bl.atr_baseline * 0.25, 8)
    
    risk = abs(entry - sl)
    tp1 = round(swing_low, 8)
    tp2 = round(swing_low - risk * 0.5, 8)'''
    
    new_code = '''    # 集成清算区止盈止损
    if LIQUIDATION_ENABLED and liq_data:
        sl_result = calc_sl_with_liquidation(
            entry=entry, direction=direction, scene="G_rally_short",
            candles=k1h, baseline=bl, liq_data=liq_data
        )
        sl = sl_result["sl_price"]
        
        tp_result = calc_batch_tp_with_liquidation(
            entry=entry, sl=sl, direction=direction,
            candles=k1h, baseline=bl, liq_data=liq_data
        )
        tp1 = tp_result["tp1_price"]
        tp2 = tp_result["tp2_price"]
    else:
        # 降级：固定计算
        recent_high = max(highs[-5:])
        sl = round(recent_high + bl.atr_baseline * 0.25, 8)
        risk = abs(entry - sl)
        tp1 = round(swing_low, 8)
        tp2 = round(swing_low - risk * 0.5, 8)'''
    
    return content.replace(old_code, new_code)


def main():
    if not SCENE_FILE.exists():
        print(f"❌ 文件不存在：{SCENE_FILE}")
        return False
    
    print(f"📖 读取 {SCENE_FILE}...")
    content = SCENE_FILE.read_text(encoding='utf-8')
    
    print("🔧 补丁场景 C...")
    content = patch_scene_c(content)
    
    print("🔧 补丁场景 G...")
    content = patch_scene_g(content)
    
    print(f"💾 保存修改...")
    SCENE_FILE.write_text(content, encoding='utf-8')
    
    print("✅ 补丁完成！")
    return True


if __name__ == "__main__":
    main()
