#!/usr/bin/env python3
"""
新架构测试脚本
验证核心模块是否正常工作
"""
import sys
sys.path.insert(0, '/root/.openclaw/workspace')

import json
from datetime import datetime

print("=" * 60)
print("新架构核心模块测试")
print("=" * 60)

# 测试 1: 市场环境判断
print("\n[1/6] 测试市场环境判断...")
try:
    from config.market_rules import MarketFilter, MarketRegime
    
    mf = MarketFilter()
    regime = mf.check_btc_filter()
    limits = mf.get_position_limits()
    
    print(f"  ✅ 市场模式：{regime}")
    print(f"  ✅ 持仓上限：多单{limits[0]}, 空单{limits[1]}, 总计{limits[2]}")
    
    # 保存状态
    mf.save_state()
    print(f"  ✅ 状态已保存到 config/market_regime.json")
except Exception as e:
    print(f"  ❌ 失败：{e}")

# 测试 2: 评分引擎
print("\n[2/6] 测试评分引擎...")
try:
    from modules.scoring import ScoringEngine
    
    details = {
        'rsi': 35,
        'macd_hist': 0.002,
        'ema_alignment': 80,
        'volume_ratio': 1.8,
        'support_strength': 75,
        'risk_reward_ratio': 2.5,
        'confidence': 0.8
    }
    
    score, breakdown = ScoringEngine.calc_score_trend(details)
    print(f"  ✅ 趋势评分：{score}")
    print(f"  ✅ 评分明细：{breakdown}")
    
    position = ScoringEngine.calc_position_usdt(
        score=score,
        details=details,
        balance_usdt=10000,
        current_positions_count=2,
        max_positions=5
    )
    print(f"  ✅ 建议仓位：{position} USDT")
except Exception as e:
    print(f"  ❌ 失败：{e}")

# 测试 3: 熔断器
print("\n[3/6] 测试熔断器...")
try:
    from modules.circuit_breaker import CircuitBreaker
    
    cb = CircuitBreaker()
    is_open, reason = cb.check()
    
    print(f"  ✅ 熔断器状态：{'开启' if is_open else '关闭'}")
    if reason:
        print(f"  ✅ 原因：{reason}")
    
    status = cb.get_status()
    print(f"  ✅ 今日触发次数：{status['trips_today']}")
except Exception as e:
    print(f"  ❌ 失败：{e}")

# 测试 4: 仓位状态管理
print("\n[4/6] 测试仓位状态管理...")
try:
    from modules.position_state import PositionState
    
    ps = PositionState()
    
    # 添加测试仓位
    ps.add_position(
        symbol='TESTUSDT',
        side='LONG',
        entry_price=100,
        sl_price=95,
        tp_price=120,
        position_usdt=500
    )
    
    positions = ps.get_all_positions()
    print(f"  ✅ 仓位数量：{len(positions)}")
    
    test_pos = ps.get_position('TESTUSDT')
    if test_pos:
        print(f"  ✅ TESTUSDT 仓位：入场{test_pos['entry_price']}, 止损{test_pos['sl_price']}, 止盈{test_pos['tp_price']}")
    
    # 清理测试仓位
    ps.remove_position('TESTUSDT')
    print(f"  ✅ 测试仓位已清理")
except Exception as e:
    print(f"  ❌ 失败：{e}")

# 测试 5: LLM 任务文件检查
print("\n[5/6] 检查 LLM 任务文件...")
llm_files = [
    'llm_tasks/scanner_llm.py',
    'llm_tasks/health_check_llm.py',
    'llm_tasks/sl_tp_llm.py',
    'llm_tasks/daily_report_llm.py',
    'llm_tasks/activity_check_llm.py',
    'llm_tasks/alert_llm.py'
]

for file in llm_files:
    try:
        with open(file, 'r') as f:
            content = f.read()
            if 'LLM' in content or 'llm' in content:
                print(f"  ✅ {file}")
            else:
                print(f"  ⚠️  {file} (可能缺少 LLM 标识)")
    except FileNotFoundError:
        print(f"  ❌ {file} (文件不存在)")

# 测试 6: 守护脚本检查
print("\n[6/6] 检查守护脚本...")
guard_files = [
    'guards/process_guardian.sh',
    'guards/patrol_sl_check.py',
    'guards/patrol_tp_check.py',
    'guards/circuit_daily_reset.py'
]

import os
for file in guard_files:
    if os.path.exists(file):
        print(f"  ✅ {file}")
    else:
        print(f"  ❌ {file} (文件不存在)")

# 总结
print("\n" + "=" * 60)
print("测试完成")
print("=" * 60)

# 显示架构文档
print("\n📄 架构文档位置:")
print("  - ARCHITECTURE.md: 系统架构设计")
print("  - MIGRATION_GUIDE.md: 迁移指南")
print("\n📁 文件结构:")
print("  - config/: 配置模块")
print("  - modules/: 核心模块")
print("  - guards/: 守护脚本")
print("  - llm_tasks/: LLM 任务")
print("  - scripts/: 工具脚本")
