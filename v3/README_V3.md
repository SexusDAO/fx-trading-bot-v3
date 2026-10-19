# V3 精简架构 - 完成报告

**版本**: v3.0  
**完成时间**: 2026-08-03 14:35 UTC  
**状态**: ✅ 核心功能完成，可投入实盘观察

---

## 架构概览

```
v3/
├── llm_tasks/           # LLM 任务（cron 触发）
│   └── scanner_llm.py   # 主扫描器（30 分钟）
├── guards/              # 守卫脚本（cron 触发）
│   ├── patrol_sl_check.py    # 止损巡逻（3 分钟）
│   └── circuit_daily_reset.py # 熔断重置（每日 00:00）
├── modules/             # 核心模块
│   ├── binance_api.py   # API 适配层
│   ├── scorer.py        # 5 维度动态评分
│   ├── scene_matcher.py # 场景匹配（A/B）
│   ├── entry_calc.py    # 入场价格计算
│   ├── sl_handler.py    # 止损/止盈挂单
│   ├── pos_state_store.py # 仓位状态管理
│   ├── watchlist_store.py # 观察池存储
│   ├── watchlist_monitor.py # 观察池监控
│   ├── pre_order_gate.py # 下单前检查
│   ├── position_size.py # 仓位计算
│   ├── dynamic_baseline.py # 动态基准
│   ├── top10_selector.py # Top10 竞争池
│   └── trade_logger.py  # 交易日志
├── config/              # 配置文件
│   ├── strategy-config.json
│   ├── pos_state.json
│   ├── pending_orders.json
│   ├── price_gates.json
│   ├── watchlist.json
│   ├── circuit_breaker.json
│   └── ...
├── logs/                # 日志文件
│   ├── scanner.log
│   ├── patrol_sl.log
│   └── ...
├── tests/               # 测试套件（18 个测试文件）
├── scripts/             # 工具脚本
│   ├── register_crons.py
│   └── apply_p0_fixes.py
└── ws_monitor.py        # WebSocket 监控（pending_orders 轮询）
```

---

## 核心流程

### 1. 扫描流程（scanner_llm.py）

```
熔断检查 → 持仓槽位检查 → 获取候选标的 (50-60 个)
  → 批量预拉 ticker → 并发评分 (8 线程)
  → Top10 竞争池 → 场景匹配 → 二次打分≥70
  → 写 pending_orders.json → 观察池更新 → 推送播报
```

**关键决策点**:
- 评分 < 55: 丢弃
- 评分 55-64: 观察池
- 评分 ≥65 且 Top10: 进入场景匹配
- 二次打分 ≥70: 准备挂单
- 二次打分 < 70: 降级进观察池

### 2. 止损巡逻（patrol_sl_check.py）

```
读取 pos_state.json → 对每个持仓查询交易所挂单
  → 止损单丢失 → 补挂 STOP_MARKET → 更新 pos_state
```

**巡逻频率**: 每 3 分钟  
**重试机制**: 3 次重试，全败则记录错误日志

### 3. WS 监控（ws_monitor.py）

```
轮询 pending_orders.json (5 秒)
  → status=pending → 挂限价单 → 改为 sent
  → status=sent → 检测成交 → 触发 on_open_filled
  → on_open_filled: 挂止损 + 注册 gates + 写 pos_state + 挂止盈
```

---

## 已修复的关键问题

### P0 - 已修复

1. ✅ **递归导入问题**: `v3/modules/binance_api.py` 使用 `importlib` 加载真实 API，避免循环导入
2. ✅ **止损巡逻 API 兼容性**: `_get_open_order_ids()` 兼容 `get_algo_open_orders_cached()` 和 `get_algo_open_orders(symbol)` 两种方法
3. ✅ **日志噪音**: 将非关键错误日志从 `warning` 降级为 `debug`
4. ✅ **测试 ID 清理**: `apply_p0_fixes.py` 自动清理 pos_state 中的测试 ID

### P1 - 已实现

1. ✅ **观察池 TTL 管理**: `watchlist_monitor.py` 支持 TTL 过期自动移除
2. ✅ **动态基准**: `dynamic_baseline.py` 根据市场波动率调整评分权重
3. ✅ **场景匹配**: `scene_matcher.py` 识别场景 A（趋势）和场景 B（震荡）

---

## 配置说明

### strategy-config.json

```json
{
  "scoring": {
    "trend_threshold": 65,      // 趋势信号最低分数
    "watchlist_threshold": 55,  // 观察池最低分数
    "drop_threshold": 30        // 丢弃阈值
  },
  "position": {
    "max_positions_total": 4,
    "market_mode_limits": {
      "bull": {"max_long": 2, "max_short": 2, "max_total": 4}
    }
  },
  "notional": {
    "trend_long": {"65_69": 300},  // 65-69 分名义仓位 300U
    "hard_cap": 1000               // 总仓位上限 1000U
  },
  "risk": {
    "daily_loss_pct": 0.2  // 日亏损 20% 熔断
  },
  "ttl_minutes": {
    "limit_low_score": 120,  // 低分信号观察池 TTL 120 分钟
    "limit_high_score": 240  // 高分信号观察池 TTL 240 分钟
  }
}
```

---

## 运行指南

### 1. 注册 cron 任务

```bash
cd /root/.openclaw/workspace/v3
python3 scripts/register_crons.py --force
```

**注册的任务**:
- `v3-scanner-main`: 每 30 分钟扫描
- `v3-patrol-sl`: 每 3 分钟止损巡逻
- `v3-circuit-reset`: 每日 00:00 熔断重置
- `v3-daily-report`: 每日 01:00 生成日报

### 2. 手动测试

```bash
# 干跑测试（不挂单）
python3 llm_tasks/scanner_llm.py --dry-run

# 止损巡逻测试
python3 guards/patrol_sl_check.py

# 查看测试结果
cat test_output.json
```

### 3. 监控日志

```bash
# 扫描日志
tail -f logs/scanner.log

# 止损巡逻日志
tail -f logs/patrol_sl.log

# 查看 cron 状态
openclaw cron list
```

---

## 测试覆盖

| 测试文件 | 覆盖模块 | 状态 |
|---------|---------|------|
| `test_scorer_scene.py` | scorer.py, scene_matcher.py | ✅ 通过 |
| `test_entry_calc.py` | entry_calc.py | ✅ 通过 |
| `test_sl_handler.py` | sl_handler.py | ✅ 通过 |
| `test_watchlist.py` | watchlist_store.py, watchlist_monitor.py | ✅ 通过 |
| `test_v3_integration.py` | 集成测试 | ✅ 通过 |
| `test_static_cases.py` | 静态案例测试 | ✅ 通过 |
| `test_e2e_simulation.py` | 端到端模拟 | ✅ 通过 |

---

## 下一步计划

### 实盘观察（24-48 小时）

1. 观察扫描器是否生成信号
2. 验证止损巡逻是否正常工作
3. 检查观察池升级机制
4. 监控日志是否有异常

### 优化方向

1. **性能**: 批量 API 调用改为并发（当前已部分实现）
2. **日志**: 结构化日志（JSON 格式），便于查询分析
3. **风控**: 添加最大回撤保护
4. **报告**: 完善每日报告内容（盈亏分析、信号统计）

---

## 故障排查

### 问题：扫描无信号

**可能原因**:
- 市场波动率低，所有标的不满足评分阈值
- 二次打分 < 70，信号降级进观察池

**检查方法**:
```bash
cat test_output.json | jq '.all_analyzed'  # 查看所有分析结果
cat config/watchlist.json  # 查看观察池
```

### 问题：止损单补挂失败

**可能原因**:
- API 密钥权限不足
- 交易所 API 限流
- pos_state 中有测试 ID 残留

**检查方法**:
```bash
tail -100 logs/patrol_sl.log
python3 scripts/apply_p0_fixes.py  # 清理测试 ID
```

### 问题：cron 任务不执行

**检查方法**:
```bash
openclaw cron list
openclaw cron runs --id <jobId>
```

---

## 总结

V3 精简架构已完成核心功能开发，主要特点：

1. **职责分离**: 扫描器只决策，ws_monitor 只执行，patrol 只守卫
2. **双层巡逻**: 止损巡逻（脚本）+ WS 监控（轮询）
3. **观察池机制**: 55-64 分信号进入观察池，等待升级
4. **场景匹配**: 二次打分确保只交易高质量信号
5. **熔断保护**: 日亏损 20% 自动熔断

当前系统处于**可投入实盘观察**状态，建议先小仓位运行 24-48 小时，验证稳定性后再逐步扩大仓位。
