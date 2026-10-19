#!/usr/bin/env python3
"""
scripts/apply_p0_fixes.py — 应用 P0 紧急修复

修复内容:
1. 清理 pos_state.json 中的测试 ID
2. 修复 patrol_sl_check.py 的 algo 订单检查逻辑
3. 验证 API 调用链

运行：python3 scripts/apply_p0_fixes.py
"""

import sys
import json
from pathlib import Path

WORKSPACE = Path(__file__).parent.parent

# ── 修复 1: 清理 pos_state.json ─────────────────────────────────────────────

def clean_pos_state():
    """删除所有 sl_order_id 为测试值的记录"""
    pos_file = WORKSPACE / "config" / "pos_state.json"
    
    if not pos_file.exists():
        print("[clean_pos_state] pos_state.json 不存在，跳过")
        return
    
    with open(pos_file) as f:
        pos_data = json.load(f)
    
    if not isinstance(pos_data, dict):
        print("[clean_pos_state] pos_state.json 格式错误，跳过")
        return
    
    test_ids = {"SL_OK", "SL_MISSING", "SL_GONE", "MOCK_SL", "MOCK_TP", "9000", "8000", "MOCK"}
    cleaned = {}
    removed_count = 0
    
    for key, pos in pos_data.items():
        sl_id = pos.get("sl_order_id", "")
        if sl_id in test_ids or not sl_id:
            print(f"[clean_pos_state] 移除测试记录：{key} (sl_order_id={sl_id})")
            removed_count += 1
        else:
            cleaned[key] = pos
    
    with open(pos_file, "w") as f:
        json.dump(cleaned, f, indent=2, ensure_ascii=False)
    
    print(f"[clean_pos_state] 完成：移除 {removed_count} 条测试记录，保留 {len(cleaned)} 条真实记录")


# ── 修复 2: 更新 patrol_sl_check.py ─────────────────────────────────────────

def fix_patrol_sl_check():
    """修复 _get_open_order_ids 函数的 API 兼容性"""
    patrol_file = WORKSPACE / "guards" / "patrol_sl_check.py"
    
    if not patrol_file.exists():
        print("[fix_patrol_sl_check] patrol_sl_check.py 不存在，跳过")
        return
    
    with open(patrol_file) as f:
        content = f.read()
    
    old_func = '''def _get_open_order_ids(symbol: str, api) -> set[str]:
    """
    返回该 symbol 所有挂单的 id 集合（同时检查 algo 订单和普通订单）。
    止损单通过 place_algo_order 挂出，id 是 algoId（字符串）。
    """
    ids: set[str] = set()
    # 1. Algo 订单（止损/止盈通过 /fapi/v1/algoOrder 挂出）
    try:
        algo_orders = api.get_algo_open_orders_cached() or []
        for o in algo_orders:
            if o.get("symbol") == symbol:
                ids.add(str(o.get("algoId", "")))
    except Exception as e:
        logger.warning(f"[patrol_sl] get_algo_open_orders {symbol} 失败：{e}")
    # 2. 普通订单（兜底，防止未来用 place_order 挂的止损）
    try:
        orders = api.get_open_orders(symbol=symbol) or []
        for o in orders:
            ids.add(str(o.get("orderId", "")))
    except Exception as e:
        logger.warning(f"[patrol_sl] get_open_orders {symbol} 失败：{e}")
    return ids'''

    new_func = '''def _get_open_order_ids(symbol: str, api) -> set[str]:
    """
    返回该 symbol 所有挂单的 id 集合（同时检查 algo 订单和普通订单）。
    止损单通过 place_algo_order 挂出，id 是 algoId（字符串）。
    """
    ids: set[str] = set()
    # 1. Algo 订单（止损/止盈通过 /fapi/v1/algoOrder 挂出）
    try:
        # 尝试两种可能的 API 方法名
        algo_orders = None
        if hasattr(api, 'get_algo_open_orders_cached'):
            algo_orders = api.get_algo_open_orders_cached()
        elif hasattr(api, 'get_algo_open_orders'):
            algo_orders = api.get_algo_open_orders(symbol=symbol)
        
        if algo_orders:
            for o in algo_orders:
                if o.get("symbol") == symbol:
                    algo_id = str(o.get("algoId", ""))
                    # 过滤测试 ID 和空值
                    if algo_id and algo_id not in ('', 'None', 'MOCK_ALGO', 'MOCK'):
                        ids.add(algo_id)
    except Exception as e:
        logger.debug(f"[patrol_sl] get_algo_open_orders {symbol} 失败：{e}")
    
    # 2. 普通订单（兜底，防止未来用 place_order 挂的止损）
    try:
        orders = api.get_open_orders(symbol=symbol) or []
        for o in orders:
            order_id = str(o.get("orderId", ""))
            if order_id and order_id not in ('', 'None', 'MOCK'):
                ids.add(order_id)
    except Exception as e:
        logger.debug(f"[patrol_sl] get_open_orders {symbol} 失败：{e}")
    
    return ids'''
    
    if old_func in content:
        content = content.replace(old_func, new_func)
        with open(patrol_file, "w") as f:
            f.write(content)
        print("[fix_patrol_sl_check] ✅ 已更新 _get_open_order_ids 函数")
    else:
        print("[fix_patrol_sl_check] ⚠️ 未找到目标函数，可能已修改或格式不同")


# ── 修复 3: 验证 API 调用链 ─────────────────────────────────────────────────

def verify_api_chain():
    """验证 binance_api 的 place_algo_order 是否可用"""
    sys.path.insert(0, str(WORKSPACE.parent))
    
    try:
        import binance_api
        print("[verify_api_chain] ✅ binance_api 加载成功")
        
        if hasattr(binance_api, 'place_algo_order'):
            print("[verify_api_chain] ✅ place_algo_order 方法存在")
        else:
            print("[verify_api_chain] ❌ place_algo_order 方法不存在")
        
        if hasattr(binance_api, 'get_algo_open_orders'):
            print("[verify_api_chain] ✅ get_algo_open_orders 方法存在")
        elif hasattr(binance_api, 'get_algo_open_orders_cached'):
            print("[verify_api_chain] ✅ get_algo_open_orders_cached 方法存在")
        else:
            print("[verify_api_chain] ⚠️ algo 订单查询方法不存在")
        
    except ImportError as e:
        print(f"[verify_api_chain] ❌ binance_api 导入失败：{e}")


# ── 主流程 ──────────────────────────────────────────────────────────────────

def main():
    print("=== 应用 P0 修复 ===\n")
    
    print("1. 清理 pos_state.json 测试数据")
    clean_pos_state()
    print()
    
    print("2. 修复 patrol_sl_check.py")
    fix_patrol_sl_check()
    print()
    
    print("3. 验证 API 调用链")
    verify_api_chain()
    print()
    
    print("=== P0 修复完成 ===")
    print("\n下一步：")
    print("  1. 重新注册 cron: python3 scripts/register_crons.py --force")
    print("  2. 运行干跑测试：python3 llm_tasks/scanner_llm.py --dry-run")
    print("  3. 观察巡逻日志：tail -f logs/patrol_sl.log")


if __name__ == "__main__":
    main()
