#!/usr/bin/env python3
"""
集成脚本：将清算区止盈止损逻辑集成到 scene_matcher_dynamic.py

此脚本会：
1. 在文件顶部添加 liquidation_data 导入
2. 修改每个场景函数的签名，添加 liq_data 参数
3. 替换止盈止损计算逻辑为清算区版本
4. 保留原有逻辑作为降级方案
"""

import re
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).parent.parent
SCENE_FILE = WORKSPACE_ROOT / "modules" / "scene_matcher_dynamic.py"

def add_imports(content: str) -> str:
    """在文件顶部添加清算区模块导入"""
    import_block = """
from modules.sl_handler_liquidation import (
    calc_tp_with_liquidation,
    calc_sl_with_liquidation,
    calc_batch_tp_with_liquidation,
)
from modules.liquidation_data import LiquidationData
"""
    # 在现有 import 块之后添加
    pattern = r"(from modules\.dynamic_baseline import.*?\))"
    match = re.search(pattern, content, re.DOTALL)
    if match:
        insert_pos = match.end()
        content = content[:insert_pos] + import_block + content[insert_pos:]
    return content


def update_function_signature(content: str, func_name: str) -> str:
    """更新函数签名，添加 liq_data 参数"""
    # 匹配函数定义
    pattern = rf"(def {func_name}\(k1h: list, mark_price: float,\s*direction: str, bl: DynamicBaseline\))"
    replacement = r"\1, liq_data: LiquidationData = None)"
    return re.sub(pattern, replacement, content)


def update_main_interface(content: str) -> str:
    """更新主接口 match_dynamic，添加 liq_data 参数并传递给子函数"""
    # 更新函数签名
    old_sig = "def match_dynamic(k1h: list, mark_price: float,\n                  direction: str, track: str = \"oscillation\",\n                  bl: Optional[DynamicBaseline] = None,\n                  symbol: str = \"\")"
    new_sig = "def match_dynamic(k1h: list, mark_price: float,\n                  direction: str, track: str = \"oscillation\",\n                  bl: Optional[DynamicBaseline] = None,\n                  symbol: str = \"\",\n                  liq_data: LiquidationData = None)"
    content = content.replace(old_sig, new_sig)
    
    # 更新所有子函数调用，传递 liq_data
    content = re.sub(
        r"(return _scene_[a-z]_dynamic\(k1h, mark_price, direction, bl\))",
        r"return _scene_\1, liq_data)",
        content
    )
    # 修正上面的替换（因为 capture group 包含了 return _scene_）
    content = content.replace("return _scene_return _scene_", "return _scene_")
    content = re.sub(
        r"(_scene_[a-z]_dynamic\(k1h, mark_price, direction, bl\))",
        r"\1, liq_data=liq_data)",
        content
    )
    
    return content


def main():
    if not SCENE_FILE.exists():
        print(f"❌ 文件不存在：{SCENE_FILE}")
        return False
    
    print(f"📖 读取 {SCENE_FILE}...")
    content = SCENE_FILE.read_text(encoding="utf-8")
    
    # 1. 添加导入
    print("📥 添加清算区模块导入...")
    content = add_imports(content)
    
    # 2. 更新函数签名
    print("✏️ 更新函数签名...")
    for func in ["_scene_a_dynamic", "_scene_b_dynamic", "_scene_c_dynamic",
                 "_scene_e_dynamic", "_scene_f_dynamic", "_scene_g_dynamic"]:
        content = update_function_signature(content, func)
    
    # 3. 更新主接口
    print("🔄 更新主接口 match_dynamic...")
    content = update_main_interface(content)
    
    # 4. 写回文件
    print(f"💾 保存修改...")
    SCENE_FILE.write_text(content, encoding="utf-8")
    
    print("✅ 集成完成！")
    print("\n⚠️  下一步需要手动完成：")
    print("   1. 在每个场景函数中替换止盈止损计算逻辑")
    print("   2. 更新 ws_monitor.py 支持 tp_source/sl_source 字段")
    print("   3. 更新 scanner_llm.py 传递 liq_data 参数")
    
    return True


if __name__ == "__main__":
    main()
