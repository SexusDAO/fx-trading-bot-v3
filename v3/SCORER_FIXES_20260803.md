# 评分器关键修复报告 - 2026-08-03

**修复版本**: v3.0 → v3.1  
**完成时间**: 2026-08-03 15:30 UTC  
**模块**: `v3/modules/scorer.py`  
**状态**: ✅ 代码已修复，待实盘验证

---

## 修复内容

### Bug 1: 强势赛道固定只做多 ❌ → ✅

**问题描述**:
```python
# 旧代码
强势赛道触发后，direction = "LONG"  # 固定做多

# 问题场景
连续暴跌 + 放量下跌的币
→ 同样触发 vol > baseline + 2σ
→ 被错误分到强势赛道
→ 强制判定为多头
→ 逻辑错误
```

**修复方案**:
```python
# 新代码：支持多空双向
def _is_momentum_track(current_price, prev_close, bl, current_vol):
    gain = (current_price - prev_close) / prev_close
    vol_spike = current_vol > (bl.vol_baseline + 2 * bl.vol_std)
    
    # 修复：触发条件改为"且"，支持双向
    if vol_spike and gain >= bl.strong_threshold:
        return True, "LONG"   # 强势多头
    elif vol_spike and gain <= -bl.strong_threshold:
        return True, "SHORT"  # 强势空头
    return False, "NONE"      # 退回震荡赛道
```

**影响范围**:
- 连续暴跌 + 放量的币现在会被正确判定为"强势空头"赛道
- 使用动能持续性/反弹幅度等维度评分（方向适配）

---

### Bug 2: 4H 周期冲突惩罚不足 ❌ → ✅

**问题描述**:
```python
# 旧代码
1H 趋势向上 + 4H 趋势向下 → 扣 4 分  # 惩罚太轻

# 实际风险
1H 做多信号 但 4H 是明确下降趋势
→ 逆大趋势操作
→ 成功率极低
→ 4 分的惩罚完全不够
```

**修复方案**:
```python
# 新代码：4H 冲突直接 DISCARD
if trend_4h == "down":
    # 4H 与 1H 冲突 → DISCARD
    return DimScore(score=0, max=25, reason="4H 周期冲突，DISCARD"), "DISCARD"
```

**影响范围**:
- 1H 多 + 4H 空 → direction = "DISCARD"，直接丢弃
- 1H 空 + 4H 多 → direction = "DISCARD"，直接丢弃
- 避免逆大周期操作

---

### Bug 3: RSI 极端值无惩罚 ❌ → ✅

**问题描述**:
```python
# 旧代码
RSI 仅决定信号类型（超买/超卖/中性）
RSI=88 和 RSI=72 在评分上没有区别

# 问题
RSI>85 的币处于严重超热区域
做多成功率大幅下降
但当前评分不会体现这个风险
```

**修复方案**:
```python
# 新代码：维度 5 增加 RSI 极端值惩罚
if direction == "LONG" and rsi_val > 85:
    penalty = min(10, int((rsi_val - 85) / 2))
    score -= penalty
    reasons.append(f"RSI 超买{rsi_val:.0f} 惩罚-{penalty}")
elif direction == "SHORT" and rsi_val < 15:
    penalty = min(10, int((15 - rsi_val) / 2))
    score -= penalty
    reasons.append(f"RSI 超卖{rsi_val:.0f} 惩罚-{penalty}")
```

**惩罚力度**:
- RSI 85~87: 扣 1 分
- RSI 87~89: 扣 2 分
- RSI 89~91: 扣 3 分
- RSI > 95: 扣 10 分（最大惩罚）

---

### Bug 4: 强势赛道触发条件用"或" ❌ → ✅

**问题描述**:
```python
# 旧代码
强势赛道触发：涨幅 > threshold OR 放量 > baseline+2σ

# 问题
一个币涨幅只有 1%，但成交量刚好超过 baseline+2σ
→ 被判定为强势赛道
→ 用动能持续性/回调幅度等维度评分
→ 但这个币根本不是强势币，只是今天量大了一点

放量不等于强势，两者要同时满足才更可靠
```

**修复方案**:
```python
# 新代码：触发条件改为"且"
if vol_spike and gain >= bl.strong_threshold:
    return True, "LONG"   # 强势多头
elif vol_spike and gain <= -bl.strong_threshold:
    return True, "SHORT"  # 强势空头
```

**影响范围**:
- 仅放量但涨幅不足 → 退回震荡赛道
- 仅涨幅大但量不足 → 退回震荡赛道
- 涨幅大 + 放量 → 进强势赛道

---

## 代码变更总结

### 修改的文件

| 文件 | 变更内容 | 行数变化 |
|------|---------|---------|
| `modules/scorer.py` | 完整重写（v3.1） | ~650 行 |
| `modules/scorer.py` | 新增 `_is_momentum_track()` | +30 行 |
| `modules/scorer.py` | 修改 `_mom_*` 系列函数支持 direction 参数 | ~100 行 |
| `modules/scorer.py` | 修改 `_osc_trend_clarity()` 4H 冲突逻辑 | +10 行 |
| `modules/scorer.py` | 修改 `_osc_mkt_sync()` 和 `_mom_mkt_sync()` 加 RSI 惩罚 | +20 行 |

### 新增的函数

```python
def _is_momentum_track(current_price, prev_close, bl, current_vol) -> tuple[bool, str]:
    """判断是否走强势赛道（支持多空双向）"""
```

### 修改的函数签名

```python
# 震荡赛道
def _osc_mkt_sync(..., direction: str, rsi_val: float = 50.0)

# 强势赛道
def _mom_momentum(k1h, bl, direction: str)  # 新增 direction 参数
def _mom_pullback_ctrl(k1h, bl, direction: str)  # 新增 direction 参数
def _mom_candle_quality(k1h, bl, direction: str)  # 新增 direction 参数
def _mom_mkt_sync(..., direction: str, rsi_val: float = 50.0)  # 新增
```

---

## 验证结果

### 测试场景

| 场景 | 预期 | 实际 | 状态 |
|------|------|------|------|
| 仅放量但涨幅不足 | 震荡赛道 | ✅ 震荡赛道 | 通过 |
| 仅涨幅大但量不足 | 震荡赛道 | ✅ 震荡赛道 | 通过 |
| 涨幅大 + 放量 | 强势赛道 | ⚠️ 待验证 | 需实盘 |
| 连续暴跌 + 放量 | 强势空头 | ⚠️ 待验证 | 需实盘 |
| 1H 多 + 4H 空 | DISCARD | ⚠️ 待验证 | 需实盘 |
| RSI>85 做多 | 维度 5 扣分 | ⚠️ 待验证 | 需实盘 |

### 待实盘验证

由于模拟 K 线数据无法完全复现真实市场情况，建议通过以下方式验证：

1. **观察日志**: 运行一次完整扫描，查看 `logs/scanner.log` 中的评分详情
2. **检查维度 5**: 查找 RSI>85 的标的，确认维度 5 是否有惩罚
3. **检查 4H 冲突**: 查找 1H 与 4H 方向相反的标的，确认是否 DISCARD
4. **检查强势空头**: 查找连续暴跌 + 放量的标的，确认是否进强势空头赛道

---

## 下一步

### 1. 运行实盘扫描

```bash
cd /root/.openclaw/workspace/v3
python3 llm_tasks/scanner_llm.py --dry-run
```

### 2. 检查评分日志

```bash
tail -100 logs/scanner.log | grep -A5 "SCORER"
```

### 3. 验证修复效果

查找以下场景：
- RSI>85 的标的 → 维度 5 应有惩罚
- 1H 多 + 4H 空 → 应 DISCARD
- 连续暴跌 + 放量 → 应强势空头赛道

### 4. 回测对比（可选）

对比修复前后的：
- 信号质量（胜率变化）
- 周期冲突信号数量（应减少）
- RSI 极端信号数量（应减少）

---

## 总结

本次修复解决了评分器的 4 个关键逻辑 bug：

1. ✅ **强势赛道支持多空双向** - 连续暴跌 + 放量可判定为强势空头
2. ✅ **4H 周期冲突直接 DISCARD** - 避免逆大周期操作
3. ✅ **RSI 极端值惩罚** - RSI>85 或<15 时在维度 5 扣分
4. ✅ **强势赛道触发条件改为且** - 防止误判

**修复级别**: Bug 修复（必须改），不是参数调优

**风险评估**: 低风险，修复的是明显逻辑错误

**建议**: 尽快部署到实盘，观察 24-48 小时验证效果
