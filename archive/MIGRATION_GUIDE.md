# 系统迁移指南

## 从旧架构迁移到新架构

### 变更概览

| 方面 | 旧架构 | 新架构 | 变更原因 |
|------|--------|--------|----------|
| LLM 调用 | 过度使用 | 仅 7 个场景 | 减少延迟，提高确定性 |
| 扫描频率 | 不固定 | 每 30 分钟 | 规律执行，便于监控 |
| 止损检查 | LLM 决策 | 纯脚本 Patrol | 快速响应，静默执行 |
| 市场环境判断 | 手动 | 自动（30 分钟） | 动态调整持仓上限 |
| 进程守护 | 无 | shell 脚本 | 自动恢复，减少人工干预 |
| 通知频率 | 频繁 | 仅关键事件 | 减少打扰 |

---

## 迁移步骤

### Step 1: 备份现有配置

```bash
cd /root/.openclaw/workspace
mkdir -p backup/$(date +%Y%m%d_%H%M%S)
cp config/*.json backup/
cp *.py backup/ 2>/dev/null || true
```

### Step 2: 停止旧进程

```bash
# 查找并停止所有相关进程
pkill -f ws_monitor.py
pkill -f scanner.py
pkill -f dual_net_orders.py
```

### Step 3: 部署新文件

新文件已创建在以下位置：
- `config/market_rules.py` - 市场环境判断
- `modules/scoring.py` - 评分引擎
- `modules/circuit_breaker.py` - 熔断器
- `modules/position_state.py` - 仓位管理
- `guards/process_guardian.sh` - 进程守护
- `guards/patrol_sl_check.py` - 止损检查
- `guards/patrol_tp_check.py` - 止盈检查
- `guards/circuit_daily_reset.py` - 熔断重置
- `llm_tasks/scanner_llm.py` - Scanner 主任务
- `llm_tasks/health_check_llm.py` - 健康检查
- `llm_tasks/sl_tp_llm.py` - SL/TP 计算
- `llm_tasks/daily_report_llm.py` - 每日报告
- `llm_tasks/activity_check_llm.py` - 活动检查
- `llm_tasks/alert_llm.py` - 异常告警
- `scripts/register_crons.py` - Cron 注册

### Step 4: 注册 Cron 任务

```bash
python3 scripts/register_crons.py
```

验证注册结果：
```bash
openclaw cron list
```

应看到以下任务：
- patrol-sl-check (每 3 分钟)
- patrol-tp-check (每 3 分钟)
- scanner-main (每 30 分钟)
- health-check (每 30 分钟)
- market-regime (每 30 分钟)
- circuit-daily-reset (每天 00:00)
- daily-report (每天 01:00)
- activity-check (每天 20:00)
- process-guardian (每天 00:01)

### Step 5: 启动进程守护

```bash
nohup bash guards/process_guardian.sh > logs/guardian.log 2>&1 &
```

验证进程运行：
```bash
ps aux | grep -E "(ws_monitor|dashboard|tailscale)"
```

### Step 6: 测试关键脚本

#### 测试 Patrol 止损检查
```bash
python3 guards/patrol_sl_check.py
```

#### 测试 Patrol 止盈检查
```bash
python3 guards/patrol_tp_check.py
```

#### 测试市场环境判断
```bash
python3 config/market_rules.py
```

查看输出：
```
当前市场模式：bull/bear/range
持仓上限：多单 X, 空单 Y, 总计 Z
```

#### 测试 Scanner（简化版）
```bash
python3 llm_tasks/scanner_llm.py
```

### Step 7: 配置 pos_state.json

如果当前有持仓，需要手动同步到 pos_state.json：

```bash
python3 -c "
from modules.position_state import PositionState
ps = PositionState()

# 示例：添加持仓
ps.add_position(
    symbol='BTCUSDT',
    side='LONG',
    entry_price=95000,
    sl_price=92000,
    tp_price=102000,
    position_usdt=2000
)

print('仓位已添加')
print(ps.get_all_positions())
"
```

### Step 8: 验证日志输出

检查日志文件是否正常写入：
```bash
ls -la logs/
tail -f logs/patrol_sl.log
tail -f logs/guardian.log
```

---

## 回滚步骤

如果新架构出现问题，可以回滚到旧架构：

### 1. 停止新进程
```bash
pkill -f process_guardian.sh
openclaw cron rm <jobId>  # 删除所有新 cron 任务
```

### 2. 恢复旧配置
```bash
cp backup/<timestamp>/*.json config/
```

### 3. 重启旧进程
```bash
# 根据旧架构的启动脚本
python3 ws_monitor.py &
python3 scanner.py &
```

---

## 常见问题

### Q1: Patrol 检查报"缺少止损单"
**A**: 这是正常现象，Patrol 会自动补挂。检查日志确认补挂成功即可。

### Q2: Scanner 扫描结果全是 skip
**A**: 可能原因：
- 市场条件不满足（评分<70）
- 已达持仓上限
- 熔断器开启

检查：
```bash
cat config/market_regime.json
cat config/circuit_breaker.json
```

### Q3: 进程守护不工作
**A**: 检查脚本权限和日志：
```bash
chmod +x guards/process_guardian.sh
tail -f logs/guardian.log
```

### Q4: Cron 任务不执行
**A**: 检查 Cron 状态：
```bash
openclaw cron status
openclaw cron runs --id <jobId>
```

---

## 监控检查清单

### 每日检查
- [ ] 查看 Patrol 日志（止损/止盈是否正常补挂）
- [ ] 查看 Scanner 结果（有无开仓决策）
- [ ] 检查持仓状态（pos_state.json 与实际持仓一致）
- [ ] 检查熔断器状态（未意外触发）

### 每周检查
- [ ] 查看健康检查报告
- [ ] 检查每日报告（盈亏统计）
- [ ] 审查异常告警日志
- [ ] 验证进程守护运行状态

### 每月检查
- [ ] 评估市场模式切换频率
- [ ] 统计胜率/盈亏比
- [ ] 优化评分参数
- [ ] 清理过期日志

---

## 性能基准

### 预期执行时间
| 任务 | 频率 | 预期耗时 |
|------|------|----------|
| Patrol SL 检查 | 每 3 分钟 | < 5 秒 |
| Patrol TP 检查 | 每 3 分钟 | < 5 秒 |
| Scanner 扫描 | 每 30 分钟 | 30-60 秒 |
| 健康检查 | 每 30 分钟 | 10-20 秒 |
| 市场环境判断 | 每 30 分钟 | < 3 秒 |

### API 调用频率
- Binance REST: ~20 次/分钟（Patrol + Scanner）
- Coinglass: ~10 次/分钟（清算数据）
- LLM 调用: ~2-4 次/小时（仅 Scanner + 健康检查）

---

## 联系支持

如遇到问题，检查以下资源：
1. ARCHITECTURE.md - 架构文档
2. logs/ - 日志文件
3. config/*.json - 配置文件状态
