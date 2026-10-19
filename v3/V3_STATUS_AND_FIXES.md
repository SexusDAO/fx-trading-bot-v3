# V3 版本状态与修复报告

**生成时间**: 2026-08-03 14:30 UTC  
**版本**: v3.0 (精简架构版)

---

## 当前状态

### ✅ 已完成的模块

| 模块 | 文件 | 状态 | 说明 |
|------|------|------|------|
| 评分器 | `modules/scorer.py` | ✅ 完整 | 5 维度动态评分 (trend/volume/volatility/structure/mkt_sync) |
| 场景匹配 | `modules/scene_matcher.py` | ✅ 完整 | 场景 A/B 识别，RR 计算 |
| 入场计算 | `modules/entry_calc.py` | ✅ 完整 | 限价/市价入场价格计算 |
| 止损处理 | `modules/sl_handler.py` | ✅ 完整 | STOP_MARKET 挂单，3 次重试 |
| 仓位状态 | `modules/pos_state_store.py` | ✅ 完整 | pos_state.json 读写 |
| 观察池 | `modules/watchlist_store.py` | ✅ 完整 | 观察池写入与 TTL 管理 |
| 观察池监控 | `modules/watchlist_monitor.py` | ✅ 完整 | 观察池重新评分 + 升级检测 |
| 主扫描器 | `llm_tasks/scanner_llm.py` | ✅ 完整 | 完整扫描流程 |
| 止损巡逻 | `guards/patrol_sl_check.py` | ⚠️ 需修复 | algo 订单检查逻辑需优化 |
| 熔断重置 | `guards/circuit_daily_reset.py` | ✅ 完整 | 每日熔断器重置 |
| WS 监控 | `ws_monitor.py` | ✅ 完整 | pending_orders 轮询 |
| 测试套件 | `tests/` | ✅ 完整 | 18 个测试文件 |

### ⚠️ 发现的问题

#### 1. 止损单补挂失败 (严重)

**现象**: `logs/patrol_sl.log` 显示 BTCUSDT 和 SOLUSDT 多次补挂止损单失败

**根本原因**:
- `pos_state.json.bak_20260803` 中的 `sl_order_id` 是测试 ID (`"SL_OK"`, `"9000"`)
- `patrol_sl_check._get_open_order_ids()` 使用 `api.get_algo_open_orders_cached()` 但真实 API 可能没有这个方法
- 路径导入问题：`guards/patrol_sl_check.py` 通过 `sys.path.insert(0, WORKSPACE.parent)` 访问 workspace 根目录的 `binance_api.py`，但可能加载了错误的模块

**修复方案**:
1. 修改 `_get_open_order_ids()` 兼容两种 API 方法名
2. 清理 pos_state 中的测试 ID
3. 确保 `place_algo_order` 返回真实的 algoId

#### 2. 观察池 TTL 过期处理

**现象**: `config/watchlist.json` 中有 FLATUSDT 记录，但未见过期清理逻辑

**修复**: 在 `watchlist_monitor.py` 中添加 TTL 检查，过期自动移除

#### 3. API 路径混乱

**现象**: `v3/modules/binance_api.py` 和 workspace 根目录的 `binance_api.py` 可能冲突

**修复**: 明确 v3 模块只通过 `v3/modules/binance_api.py` 适配层访问，禁止直接 import workspace 根目录的 binance_api

---

## 修复计划

### P0 - 立即修复 (影响实盘)

1. **修复 patrol_sl_check.py 的 algo 订单检查**
   - 兼容 `get_algo_open_orders_cached()` 和 `get_algo_open_orders(symbol)` 两种方法
   - 添加日志级别降级 (warning → debug)，避免日志噪音

2. **清理 pos_state.json 测试数据**
   - 删除所有 `sl_order_id` 为 `"SL_OK"`, `"MOCK_*"`, `"9000"` 的记录
   - 确保下次巡逻从干净状态开始

3. **验证 place_algo_order 真实调用**
   - 在 sl_handler.py 中添加详细日志
   - 确认返回的 algoId 格式正确

### P1 - 功能完善

1. **添加 watchlist TTL 清理**
2. **完善 ws_monitor.py 的成交检测**
3. **添加每日报告生成器**

### P2 - 优化

1. **性能优化**: 批量 API 调用改为并发
2. **日志优化**: 结构化日志，便于查询
3. **测试覆盖**: 添加端到端集成测试

---

## 配置文件说明

### v3/config/ 目录

| 文件 | 用途 | 格式 |
|------|------|------|
| `strategy-config.json` | 策略参数 (评分阈值/仓位限制) | JSON |
| `pos_state.json` | 当前持仓状态 | JSON |
| `pending_orders.json` | 待处理订单队列 | JSON |
| `price_gates.json` | 价格门 (移动止损触发点) | JSON |
| `watchlist.json` | 观察池 (55-64 分信号) | JSON |
| `circuit_breaker.json` | 熔断器状态 | JSON |
| `market_regime.json` | 市场状态 (bull/bear/range) | JSON |
| `daily_risk.json` | 每日盈亏统计 | JSON |
| `daily_cancel_counter.json` | 每日撤单计数 | JSON |
| `blacklist.json` | 黑名单标的 | JSON |
| `token_report_cache.json` | Token Report 缓存 | JSON |

---

## 运行命令

### 注册 cron 任务
```bash
cd /root/.openclaw/workspace/v3
python3 scripts/register_crons.py --force
```

### 手动运行扫描
```bash
cd /root/.openclaw/workspace/v3
python3 llm_tasks/scanner_llm.py --dry-run
```

### 手动运行止损巡逻
```bash
cd /root/.openclaw/workspace/v3
python3 guards/patrol_sl_check.py
```

### 查看 cron 状态
```bash
openclaw cron list
```

---

## 下一步

1. 应用 P0 修复
2. 重新注册 cron 任务
3. 运行一次完整干跑测试
4. 观察 24 小时实盘表现
