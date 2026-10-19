# 新系统架构文档

## 核心设计原则

1. **最小化 LLM 调用**：仅 7 个场景使用 LLM，其余全部纯脚本执行
2. **静默执行**：非 LLM 场景全部静默，不推送消息
3. **模块化**：清晰分离纯脚本模块和 LLM 任务
4. **可审计**：所有决策有日志，可追溯

---

## 扫描范围

每轮扫描标的池（自动去重）：
- 涨幅榜前 20
- 跌幅榜前 20
- 成交量前 60
- 热度榜前 10

**合计约 90~100 个标的（去重后）**

---

## 持仓上限

### 总持仓上限：5 个

### 市场模式自动切换

| 模式 | 触发条件 | 多单上限 | 空单上限 | 总计上限 |
|------|----------|----------|----------|----------|
| **牛市** | BTC 4H EMA 多排且价格>MA99 | 3 | 2 | 5 |
| **熊市** | BTC 4H EMA 空排且价格<MA99 | 2 | 3 | 5 |
| **震荡** | 其他 | 2 | 2 | 4 |

**重新判断频率**：每 30 分钟

### 总名义仓位上限

```
账户余额 × 3
```

---

## 双网挂单触发条件

仅同时满足以下全部条件才走双网：

1. ✅ 信号评分 ≥ 85 分
2. ✅ 突破位置上方（多单）/下方（空单）清算密集区足够大
3. ✅ 清算量 > 当前持仓量的 10%

**其他情况一律走单一限价单入场，不强制使用双网**

---

## 模块执行方式分类

### 【纯脚本执行，完全不走 LLM】

| # | 模块 | 执行方式 | 频率 |
|---|------|----------|------|
| 1 | 进程守护（5 个） | shell 脚本 pgrep+ 重启 | 持续 |
| 2 | 熔断检查 | 读 circuit_breaker.json | 每次扫描前 |
| 3 | Patrol 止损检查 | REST 查询 + 补挂 | 每 3 分钟 |
| 4 | Patrol 止盈检查 | REST 查询 + 补挂 | 每 3 分钟 |
| 5 | 开仓评分计算 | Python 公式 | 每次扫描 |
| 6 | 仓位大小计算 | Python 公式 | 每次开仓 |
| 7 | 市场环境判断 | K 线数据计算 | 每 30 分钟 |
| 8 | 熔断重置 | 写 JSON | 每天 00:00 UTC |
| 9 | WS 事件处理 | Python 进程内 | 实时 |
| 10 | Git 自动备份 | shell 脚本 | 每天 |

### 【需要 LLM 的场景】

| ID | 场景 | 频率 | 触发条件 | 输出 |
|----|------|------|----------|------|
| **LLM-1** | Scanner 主扫描 | 每 30 分钟 | 定时 | 是否挂单 + 挂单位置 + 仓位大小 |
| **LLM-2** | 双网 B 单突破确认 | 实时 | 价格接近突破位（WS 触发） | 是否执行 B 单 + 止盈位置 |
| **LLM-3** | 持仓健康检查 | 每 30 分钟 | 定时 | 继续持有 / 主动平仓 |
| **LLM-4** | SL/TP 位置计算 | 开仓成功后 | open_filled 事件 | 止损价格 + 止盈价格 |
| **LLM-5** | 每日报告 | 每天 01:00 UTC | 定时 | 当日盈亏 + 胜率 + 复盘建议 |
| **LLM-6** | 活动公告检查 | 每天 20:00 UTC | 定时 | 有无新交易大赛/活动 |
| **LLM-7** | 异常告警 | 实时 | WS 断线 3 次/熔断/写入失败 | 推送告警消息 |

---

## 文件结构

```
/root/.openclaw/workspace/
├── config/
│   ├── market_rules.py          # 市场环境判断（纯 Python）
│   ├── market_regime.json       # 市场模式缓存
│   ├── circuit_breaker.json     # 熔断器状态
│   └── pos_state.json           # 仓位状态
│
├── modules/
│   ├── scoring.py               # 评分引擎（纯 Python）
│   ├── circuit_breaker.py       # 熔断器模块
│   └── position_state.py        # 仓位状态管理
│
├── guards/
│   ├── process_guardian.sh      # 进程守护（shell）
│   ├── patrol_sl_check.py       # 止损检查（每 3 分钟）
│   ├── patrol_tp_check.py       # 止盈检查（每 3 分钟）
│   └── circuit_daily_reset.py   # 熔断重置（每天）
│
├── llm_tasks/
│   ├── scanner_llm.py           # LLM-1: 主扫描
│   ├── health_check_llm.py      # LLM-3: 健康检查
│   ├── sl_tp_llm.py             # LLM-4: SL/TP 计算
│   ├── daily_report_llm.py      # LLM-5: 每日报告
│   ├── activity_check_llm.py    # LLM-6: 活动检查
│   └── alert_llm.py             # LLM-7: 异常告警
│
└── scripts/
    └── register_crons.py        # Cron 任务注册
```

---

## 执行流程

### Scanner 主扫描流程（LLM-1）

```
1. 检查熔断器 → 开启则跳过
2. 判断市场模式 → 获取持仓上限
3. 检查当前持仓 → 达上限则跳过
4. 获取 90-100 个候选标的
5. 逐个分析：
   - 拉取 K 线数据
   - 计算技术指标
   - 纯 Python 评分
   - 获取清算数据
6. LLM 决策：
   - 评分 < 70 → skip
   - 评分 >= 85 + 清算充足 → dual_net
   - 评分 >= 85 + 清算不足 → limit_order
   - 评分 70-85 → limit_order
7. 执行挂单
8. 保存结果
```

### 持仓健康检查流程（LLM-3）

```
1. 加载 pos_state.json
2. 逐个持仓分析：
   - 拉取最新 K 线
   - 重新评分（纯 Python）
   - 计算未实现盈亏
   - 检查聪明钱信号
3. LLM 决策：
   - 评分 < 35 + 持仓 > 60 分钟 → close
   - 聪明钱反向 → close
   - 亏损 > 5% → close
   - 评分 >= 70 → hold
   - 其他 → hold
4. 执行高优先级平仓
5. 保存结果
```

### Patrol 止损检查流程（纯 Python）

```
1. 加载 pos_state.json
2. 逐个持仓检查：
   - REST 查询 Binance 订单
   - 检查是否有匹配的止损单
   - 无止损 → 读取 sl_price → 补挂 STOP_MARKET
3. 记录日志
```

---

## 通知规则

### 静默执行（不推送）
- 所有纯脚本模块
- Scanner 扫描（无开仓时）
- 健康检查（无平仓时）
- Patrol 检查
- 熔断检查

### 推送通知
- LLM-1 开仓成交
- LLM-3 主动平仓
- 单笔亏损 > 50U
- LLM-7 异常告警
- LLM-6 新活动公告
- 系统异常/API 报错

---

## 部署步骤

1. **备份现有配置**
   ```bash
   cp config/*.json backup/
   ```

2. **注册 Cron 任务**
   ```bash
   python3 scripts/register_crons.py
   ```

3. **启动进程守护**
   ```bash
   nohup bash guards/process_guardian.sh &
   ```

4. **验证 Cron 任务**
   ```bash
   openclaw cron list
   ```

5. **测试 Patrol 脚本**
   ```bash
   python3 guards/patrol_sl_check.py
   python3 guards/patrol_tp_check.py
   ```

6. **测试 Scanner**
   ```bash
   python3 llm_tasks/scanner_llm.py
   ```

---

## 监控与调试

### 日志文件位置
```
/root/.openclaw/workspace/logs/
├── guardian.log           # 进程守护日志
├── patrol_sl.log          # 止损检查日志
├── patrol_tp.log          # 止盈检查日志
├── circuit_reset.log      # 熔断重置日志
├── scanner.log            # Scanner 日志
└── health_check.log       # 健康检查日志
```

### 查看实时日志
```bash
tail -f logs/patrol_sl.log
tail -f logs/scanner.log
```

### 检查 Cron 运行记录
```bash
openclaw cron runs --id <jobId>
```

---

## 故障处理

### 进程挂掉
```bash
# 手动重启
python3 ws_monitor.py
python3 guards/patrol_sl_check.py
```

### 熔断触发
```bash
# 查看状态
cat config/circuit_breaker.json

# 手动重置
python3 -c "from modules.circuit_breaker import CircuitBreaker; cb = CircuitBreaker(); cb.reset()"
```

### 仓位状态异常
```bash
# 查看状态
cat config/pos_state.json

# 手动修复
python3 -c "
from modules.position_state import PositionState
ps = PositionState()
ps.remove_position('ETHUSDT')  # 删除异常仓位
"
```

---

## 性能优化

1. **减少 API 调用**：批量获取 K 线，缓存结果
2. **并行扫描**：多个标的并行分析
3. **增量更新**：pos_state.json 仅更新变化部分
4. **日志轮转**：保留最近 7 天日志

---

## 安全边界

1. **最大仓位**：单笔不超过余额 50%
2. **最大杠杆**：默认 5x，不超过 10x
3. **熔断保护**：连续亏损 3 笔触发 1 小时熔断
4. **每日止损**：单日亏损 > 10% 停止交易

---

## 版本历史

- **v2.0** (2026-08-02): 新架构设计确认，LLM 调用从过度使用缩减到 7 个场景
- **v1.0**: 初始版本，过度依赖 LLM
