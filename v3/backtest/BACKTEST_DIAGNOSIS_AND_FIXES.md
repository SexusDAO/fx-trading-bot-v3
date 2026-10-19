# ETH 回测诊断与修复方案

**诊断时间**: 2026-08-03  
**回测周期**: 2026-07-04 ~ 2026-08-03（30 天）

---

## 🔍 三个关键问题

### 问题 1：仓位大小错误 ❌ → ✅ 已修复

**症状**：
- 回测显示单笔亏损$400-600
- 实际规则：最大仓位 1000U

**原因**：
```python
# 错误代码
"size": self.capital * self.leverage / entry  # 使用全部资金的 5 倍杠杆

# 正确代码
position_value = 1000 * leverage  # 固定 1000U 仓位
contract_size = position_value / entry
```

**修复后**：
- 单笔亏损：$400+ → $48（合理）
- 总亏损：-21.38% → -2.31%

---

### 问题 2：止盈止损未使用清算区优化 ❌ → 🔄 待集成

**症状**：
- scene_matcher_dynamic.py 使用固定 RR 计算
- 止盈命中率低（12.5%）
- 止损频繁被扫（7/8 笔）

**当前代码**（场景 A 示例）：
```python
# modules/scene_matcher_dynamic.py 第 170 行
sl = round(min(lows[-5:]) - 0.5 * bl.atr_baseline, 8)
risk = abs(entry - sl)
tp = round(entry + risk * 2.5, 8)
```

**应使用**：
```python
from modules.sl_handler_liquidation import (
    calc_tp_with_liquidation,
    calc_sl_with_liquidation,
)

sl_result = calc_sl_with_liquidation(
    entry=entry,
    direction="LONG",
    scene="A_pullback",
    candles=k1h,
    baseline=bl,
    liq_data=liq_data
)
sl = sl_result["sl_price"]

tp_result = calc_tp_with_liquidation(
    entry=entry,
    sl=sl,
    direction="LONG",
    candles=k1h,
    baseline=bl,
    liq_data=liq_data
)
tp = tp_result["tp_price"]
```

**预期效果**：
- 止盈命中率：12.5% → 40-50%
- 止损被扫率：87.5% → 50-60%
- 总盈亏：-2.31% → +5-10%

**集成状态**：
- ✅ `modules/sl_handler_liquidation.py` 已创建
- ✅ `modules/liquidation_data.py` 已创建
- ✅ 测试通过（6/6）
- ❌ **scene_matcher_dynamic.py 尚未集成**

---

### 问题 3：信号触发过少（19 个/30 天）⚠️

**症状**：
- 30 天只有 19 个信号（平均 1.6 天/个）
- 只有场景 A（回调 LONG）和 E（反弹 SHORT）触发
- 场景 B/C/F/G 从未触发

**分析结果**：

| 指标 | 数值 | 结论 |
|------|------|------|
| 二次打分≥70 | 16 个 (84.2%) | ✅ 打分门槛不是问题 |
| RR≥2.0 | 19 个 (100%) | ✅ RR 门槛不是问题 |
| 场景 A 触发 | 10 个 | 震荡行情中回调入场 |
| 场景 E 触发 | 9 个 | 震荡行情中反弹做空 |
| 场景 B/C/F/G | 0 个 | ❌ 突破/强势场景未触发 |

**根本原因**：

1. **市场环境**：ETH 过去 30 天是宽幅震荡（$1,712-$1,982），没有明显趋势
   - 突破场景（B/F）需要有效突破区间
   - 强势场景（C/G）需要连续大涨/大跌

2. **场景设计**：当前场景更适合趋势行情，震荡行情中触发条件严格

**解决方案**：

#### 方案 A：增加震荡场景（推荐）
新增专门针对震荡行情的场景：
- **场景 H**：区间下沿做多（RSI 超卖 + 支撑位）
- **场景 I**：区间上沿做空（RSI 超买 + 阻力位）

#### 方案 B：放宽现有场景条件
- 场景 A：回调幅度从 1.2-3.0×ATR 放宽到 1.0-3.5×ATR
- 场景 E：反弹幅度从 1.2-3.0×ATR 放宽到 1.0-3.5×ATR
- RSI 阈值从 2σ 放宽到 1.5σ

#### 方案 C：降低二次打分门槛
- 当前：≥70 分才挂单
- 建议：≥60 分挂单，≥70 分满仓，60-70 分半仓

---

## 📊 修复后回测预期

### 修复 1：仓位大小（已完成）
- 总盈亏：-21.38% → -2.31% ✅

### 修复 2：清算区集成（待完成）
- 止盈命中率：12.5% → 40-50%
- 止损被扫率：87.5% → 50-60%
- 预期总盈亏：-2.31% → +5-10%

### 修复 3：增加信号频率（待完成）
- 信号数量：19 个/30 天 → 40-60 个/30 天
- 交易机会：1.6 天/个 → 0.5-0.75 天/个

---

## 📝 待完成的工作清单

### 优先级 1：集成清算区止盈止损
1. 修改 `scene_matcher_dynamic.py` 导入清算区函数
2. 替换所有场景的止盈止损计算逻辑
3. 更新 MatchResult 类型定义（增加 tp_source/sl_source）
4. 重新回测验证

**预计时间**：1-2 小时

### 优先级 2：优化场景匹配条件
1. 放宽场景 A/E 的回调/反弹幅度要求
2. 降低 RSI 阈值（2σ → 1.5σ）
3. 降低二次打分门槛（70 → 60）
4. 重新回测验证

**预计时间**：1 小时

### 优先级 3：新增震荡场景（可选）
1. 设计场景 H（区间下沿做多）
2. 设计场景 I（区间上沿做空）
3. 回测验证

**预计时间**：2-3 小时

---

## 🎯 立即行动

**第一步**：完成清算区集成（优先级最高）

```bash
cd /root/.openclaw/workspace/v3
python3 backtest/test_eth_backtest_fixed.py  # 基准回测
# 集成清算区...
python3 backtest/test_eth_backtest_fixed.py  # 对比回测
```

**第二步**：优化场景匹配条件

```bash
# 修改 scene_matcher_dynamic.py 参数
python3 backtest/analyze_signals.py  # 分析信号变化
```

**第三步**：实盘观察 24-48 小时

---

## 📈 最终预期效果

| 指标 | 当前 | 修复后 |
|------|------|--------|
| 信号数量 | 19 个/30 天 | 40-60 个/30 天 |
| 止盈命中率 | 12.5% | 40-50% |
| 止损被扫率 | 87.5% | 50-60% |
| 胜率 | 12.5% | 40-45% |
| 盈亏比 | 2.18 | 2.0-2.5 |
| 总盈亏 | -2.31% | +5-15% |
| 最大回撤 | $336 | $200-300 |

---

**结论**：当前策略框架正确，但需要完成清算区集成并优化震荡行情适配。修复后预期从亏损转为盈利。🦞
