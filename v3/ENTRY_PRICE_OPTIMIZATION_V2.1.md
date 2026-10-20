# 入场价优化 v2.1 — 完成报告

**完成时间**: 2026-08-03 15:00 UTC  
**模块**: `v3/modules/entry_calc.py`  
**状态**: ✅ 已完成并验证

---

## 问题背景

### 固定比例入场价的问题

原有逻辑使用固定比例（如 `current_price × 1.003` = +0.3%）计算入场价，导致：

| 币种 | ATR | 0.3% 对应 | 问题 |
|------|-----|----------|------|
| BTCUSDT | 800U (~1.2%) | 200U (~0.3%) | **完全在噪音范围内**，极易被扫掉 |
| MEME 币 | 0.3% | 0.3% = 1×ATR | **刚好 1 个 ATR**，太激进 |

**核心问题**：同一个公式，在不同波动率的币种上表现完全不同。

---

## 优化方案：三层动态入场价

### 设计目标

入场价应该解决三个问题：

1. **不被噪音扫掉**：入场后立刻反向触发止损
2. **不追得太深**：入场价太差，RR 直接废掉
3. **尽量在真实卖压/买盘聚集的地方成交**：提高成交质量

### 三层逻辑

```
第一层（base）：ATR 缓冲 → 解决噪音问题
  空头：current_price + 0.25×ATR（挂在上方等反弹）
  多头：current_price - 0.25×ATR（挂在下方等回调）

第二层（resistance/support）：技术位 → 解决成交质量问题
  空头：阻力位 - 0.1×ATR（在阻力位下方挂，等反弹触阻回落）
  多头：支撑位 + 0.1×ATR（在支撑位上方挂，等回调触支撑企稳）
  条件：技术位必须在 1.5×ATR 范围内才采用

第三层（liq_zone）：清算区锚点 → 解决套牢问题
  空头：清算区顶 + 0.15×ATR（在清算区上方挂，等多单清算被消化后入）
  多头：清算区底 - 0.15×ATR（在清算区下方挂，等空单清算被消化后入）
  条件：清算区在 base_entry 的 2% 范围内才采用
```

### 综合决策

```python
# 空头：取所有候选里最低的（不追高）
# 但不能低于 current_price（空头入场价必须在当前价上方）
final = max(current_price, min(candidates))

# 多头：取所有候选里最高的（不追低）
# 但不能高于 current_price（多头入场价必须在当前价下方）
final = min(current_price, max(candidates))
```

### 安全检查

```python
# 入场价不得超过 current_price ± 1.5×ATR（追太高/太低就不追）
max_allowed = current_price + 1.5 * atr
min_allowed = current_price - 1.5 * atr

# 最小偏移：必须至少有 0.15×ATR 的缓冲（防止太接近现价）
min_offset = current_price - atr * 0.15  # LONG
```

---

## 技术位搜索逻辑

### 阻力位（空头用）

来源（按优先级）：
1. 近 5 根 K 线最高价（最近的卖压聚集点）
2. 前期高点（近 20 根排除近 5 根）
3. EMA20 / EMA50 压制位（均线阻力）

过滤：必须在 `current_price ~ current_price + 1.5×ATR` 范围内

返回：最近的阻力位（最低的），因为空头应该挂在阻力位下方一点。

### 支撑位（多头用）

来源（按优先级）：
1. 近 5 根 K 线最低价（最近的买盘聚集点）
2. 前期低点（近 20 根排除近 5 根）
3. EMA20 / EMA50 支撑位（均线支撑）

过滤：必须在 `current_price - 1.5×ATR ~ current_price` 范围内

返回：最近的支撑位（最高的），因为多头应该挂在支撑位上方一点。

---

## 验证结果

### 测试数据（真实 API）

| 币种 | 当前价 | ATR(14) | ATR% | 空头入场 | 多头入场 | 状态 |
|------|--------|---------|------|----------|----------|------|
| BTCUSDT | 63,659 | 342 | 0.54% | +0.25×ATR | -0.25×ATR | ✅ |
| ETHUSDT | 1,863 | 12.9 | 0.69% | +0.25×ATR | -0.25×ATR | ✅ |
| SOLUSDT | 73.55 | 0.42 | 0.57% | +0.21×ATR | -0.25×ATR | ✅ |

### 验证标准

- ✅ 空头入场价 > 当前价
- ✅ 多头入场价 < 当前价
- ✅ 偏离在 0.15~1.5×ATR 范围内
- ✅ 方法为 `atr_buffer` / `resistance` / `support` / `liq_zone` / `combined`

---

## 代码变更

### 修改的文件

1. `v3/modules/entry_calc.py`
   - 优化 `calc_dynamic_entry()` 函数的三层逻辑
   - 添加最小偏移检查（0.15×ATR）
   - 改进阻力位/支撑位搜索逻辑
   - 完善文档注释

2. `v3/tests/test_entry_with_real_data.py`（新增）
   - 使用真实 API 数据验证入场价计算
   - 覆盖不同波动率的币种

### 关键代码片段

```python
def calc_dynamic_entry(
    direction: str,
    current_price: float,
    k1h: list,
    atr_baseline: float,
    liq_zone_price: Optional[float] = None,
) -> tuple[float, str]:
    """三层动态入场价计算（v2.1 - 优化版）"""
    
    atr = atr_baseline if atr_baseline > 0 else current_price * 0.01
    max_allowed_up = current_price + 1.5 * atr
    max_allowed_down = current_price - 1.5 * atr
    
    if direction == "SHORT":
        candidates = []
        
        # 第一层：ATR 缓冲
        base_entry = current_price + atr * 0.25
        candidates.append((base_entry, "atr_buffer"))
        
        # 第二层：阻力位
        resistance = _find_nearest_resistance(k1h, current_price, atr)
        if resistance is not None and resistance <= max_allowed_up:
            entry_resist = resistance - atr * 0.1
            if entry_resist > current_price:
                candidates.append((entry_resist, "resistance"))
        
        # 第三层：清算区
        if liq_zone_price is not None and liq_zone_price > current_price:
            if liq_zone_price <= base_entry * 1.02:
                entry_liq = liq_zone_price + atr * 0.15
                if entry_liq <= max_allowed_up:
                    candidates.append((entry_liq, "liq_zone"))
        
        # 空头：取最低（不追高）
        best_price, best_method = min(candidates, key=lambda x: x[0])
        final = max(current_price, best_price)
        final = min(final, max_allowed_up)
        
    else:  # LONG
        # ... 对称逻辑 ...
    
    return round(final, 8), method
```

---

## 与旧版本对比

| 特性 | 旧版本 (v2.0) | 新版本 (v2.1) | 改进 |
|------|--------------|--------------|------|
| 缓冲比例 | 固定 0.3% | 0.25×ATR（动态） | 适应不同波动率 |
| 技术位优先级 | 有但逻辑混乱 | 清晰三层递进 | 更易维护 |
| 最小偏移 | 无 | 0.15×ATR | 防止入场价太接近现价 |
| 清算区处理 | 简单覆盖 | 2% 范围检查 + 0.15×ATR 缓冲 | 更精确 |
| 文档 | 简略 | 详细注释 + 设计目标 | 更易理解 |

---

## 下一步

### 集成到 scene_matcher

当前 `calc_dynamic_entry()` 已优化完成，需要在 `scene_matcher.py` 中替换旧的固定比例逻辑：

```python
# 旧代码（scene_matcher.py 约第 400 行）
entry_price = current_price * 1.003  # ❌ 固定比例

# 新代码
from modules.entry_calc import calc_dynamic_entry
entry_price, method = calc_dynamic_entry(
    direction=direction,
    current_price=current_price,
    k1h=k1h,
    atr_baseline=atr_val,
    liq_zone_price=liq_zone.get("top") if direction == "SHORT" else liq_zone.get("bottom"),
)
```

### 回测验证

建议运行一次完整回测，对比优化前后的：
- 成交率（入场价是否更容易被扫到）
- 平均盈亏比（入场价质量是否提高）
- 被扫止损的比例（是否减少了噪音扫损）

---

## 总结

入场价优化 v2.1 已完成核心逻辑改进：

1. ✅ **动态 ATR 缓冲**：替代固定比例，适应不同波动率
2. ✅ **三层优先级**：ATR 缓冲 → 技术位 → 清算区，逻辑清晰
3. ✅ **安全检查**：±1.5×ATR 范围 + 最小 0.15×ATR 缓冲
4. ✅ **真实数据验证**：BTC/ETH/SOL 三个主流币种通过测试

下一步需要集成到 `scene_matcher.py` 并运行回测验证实际效果。
