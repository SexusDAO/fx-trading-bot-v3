# 止盈止损规则说明

**版本**: v3.1-Dynamic  
**更新时间**: 2026-08-03 16:45 UTC  
**模块**: `v3/modules/sl_handler.py`, `v3/modules/scene_matcher_dynamic.py`

---

## 总体架构

### 止损（Stop Loss）

**类型**: STOP_MARKET（市价止损单）  
**挂单方式**: 通过 `place_algo_order` (/fapi/v1/algoOrder)  
**重试机制**: 3 次重试，全败则返回 False（调用方应执行市价平仓）  
**执行顺序**: 开仓时先挂止损 → 确认 → 注册价格门 → 写 pos_state → 挂止盈

### 止盈（Take Profit）

**类型**: TAKE_PROFIT（限价止盈单）  
**挂单方式**: 通过 `place_order` (/fapi/v1/order)  
**执行方式**: 异步安全，失败只记日志不中断主流程  
**分批止盈**: 场景 C/G 支持 tp1（60%）+ tp2（40%）

---

## 各场景止损规则

### 场景 A：回调入场（LONG/SHORT）

```python
# LONG
sl = min(lows[-5:]) - 0.5 * bl.atr_baseline

# SHORT
sl = max(highs[-5:]) + 0.5 * bl.atr_baseline
```

**逻辑**: 近 5 根极值点 ± 0.5×ATR 缓冲

---

### 场景 B：蓄势突破（LONG/SHORT）

```python
# LONG（突破区间上沿）
entry = zone_top + buffer  # buffer 基于假突破率动态计算
sl = zone_bottom - 0.3 * bl.atr_baseline

# SHORT（跌破区间下沿）
entry = zone_bottom - buffer
sl = zone_top + 0.3 * bl.atr_baseline
```

**逻辑**: 区间另一侧 ± 0.3×ATR 缓冲

---

### 场景 C：强势回踩（LONG）

```python
recent_low = min(lows[-5:])
sl = recent_low - 0.25 * bl.atr_baseline  # 略微收紧

# 分批止盈
tp1 = swing_high                    # 第一目标：前高（保守锁利）
tp2 = swing_high + risk * 0.5       # 第二目标：前高 +0.5×风险

# ws_monitor 触发逻辑：
# 到达 tp1 时平仓 60%，剩余 40% 等 tp2
# 如果 tp1 后回落超过 0.5×atr_baseline，全部平仓不等 tp2
```

**逻辑**: 近 5 根低点 - 0.25×ATR（收紧版）

---

### 场景 E：反弹做空（SHORT）

```python
sl = max(highs[-5:]) + 0.5 * bl.atr_baseline
tp = entry - risk * 2.5
```

**逻辑**: 与场景 A 对称，近 5 根高点 + 0.5×ATR

---

### 场景 F：破位做空（SHORT）

```python
# 破位入场：区间下沿下方 动态缓冲
entry = zone_bottom - buffer
sl = zone_top + 0.3 * bl.atr_baseline
tp = entry - (h_range + 0.5 * bl.atr_baseline)
```

**逻辑**: 与场景 B 对称，区间另一侧 + 0.3×ATR

---

### 场景 G：强势反弹（SHORT）

```python
recent_high = max(highs[-5:])
sl = recent_high + 0.25 * bl.atr_baseline  # 略微收紧

# 分批止盈
tp1 = swing_low                   # 第一目标：前低
tp2 = swing_low - risk * 0.5      # 第二目标：前低 -0.5×风险
```

**逻辑**: 与场景 C 对称，近 5 根高点 + 0.25×ATR

---

## 止盈规则总结

### 单一止盈（场景 A/B/E/F）

```python
# LONG
risk = entry - sl
tp = entry + risk * RR_multiplier  # RR_multiplier = 2.0~2.5

# SHORT
risk = sl - entry
tp = entry - risk * RR_multiplier
```

### 分批止盈（场景 C/G）

```python
# 场景 C（LONG）
tp1 = swing_high                   # 前高（保守锁利）
tp2 = swing_high + risk * 0.5      # 前高 +0.5×风险

# 场景 G（SHORT）
tp1 = swing_low                    # 前低
tp2 = swing_low - risk * 0.5       # 前低 -0.5×风险

# ws_monitor 触发逻辑
if price >= tp1 and not tp1_hit:
    close_percent = 0.6  # 平仓 60%
    tp1_hit = True
    # 移动止损到保本位
    
if price >= tp2:
    close_percent = 1.0  # 全部平仓
    
# 提前平仓条件
if tp1_hit and price < tp1 - 0.5 * atr_baseline:
    close_percent = 1.0  # 全部平仓不等 tp2
```

---

## 止损缓冲系数对比

| 场景 | 多头止损 | 空头止损 | 缓冲系数 | 说明 |
|------|---------|---------|---------|------|
| **A** | lows[-5:] - 0.5×ATR | highs[-5:] + 0.5×ATR | 0.5 | 标准缓冲 |
| **B** | zone_bottom - 0.3×ATR | zone_top + 0.3×ATR | 0.3 | 区间突破，缓冲较小 |
| **C** | lows[-5:] - 0.25×ATR | N/A | 0.25 | 强势回踩，缓冲收紧 |
| **E** | N/A | highs[-5:] + 0.5×ATR | 0.5 | 标准缓冲 |
| **F** | N/A | zone_top + 0.3×ATR | 0.3 | 区间破位，缓冲较小 |
| **G** | N/A | highs[-5:] + 0.25×ATR | 0.25 | 强势反弹，缓冲收紧 |

---

## 盈亏比（RR）要求

| 场景 | 最低 RR | 二次打分 RR 要求 |
|------|--------|----------------|
| **A** | ≥2.0 | RR≥历史平均 120%: +10 分 |
| **B** | ≥1.8 | RR≥2.0: +10 分 |
| **C** | ≥1.8 | RR≥2.0: +10 分 |
| **E** | ≥2.0 | RR≥2.5: +10 分 |
| **F** | ≥1.8 | RR≥2.0: +10 分 |
| **G** | ≥1.8 | RR≥2.0: +10 分 |

---

## 止损巡逻机制

**文件**: `v3/guards/patrol_sl_check.py`  
**频率**: 每 3 分钟 cron 触发

### 巡逻流程

```
读取 pos_state.json
  → 对每个持仓查询交易所挂单
  → 发现止损单丢失 → 补挂 STOP_MARKET
  → 更新 pos_state.json
  → 静默执行，无行动不推送
```

### 补挂逻辑

```python
# 检查止损单是否存在
open_ids = _get_open_order_ids(symbol, api)
if sl_id not in open_ids:
    # 补挂
    ok, new_id = place_sl(symbol, direction, sl_price, qty, api)
    if ok:
        ps_write(symbol, direction, {
            "sl_order_id": new_id,
            "sl_reattached_at": _now_iso(),
        })
```

### 清理残留

如果实际无持仓但 pos_state 有记录：
```python
if not has_position:
    logger.info(f"{symbol} 实际无持仓，清理 pos_state 残留")
    ps_remove(symbol, direction)
    # 撤销残余 algo 订单
    for o in stale_algo_orders:
        if o.get("symbol") == symbol:
            api.cancel_algo_order(symbol, o.get("algoId"))
```

---

## 价格门（Price Gates）

**文件**: `v3/modules/price_gates.py`（待实现）  
**用途**: 注册移动止损触发点，当价格触及某个阈值时触发巡逻检查

### 注册逻辑

```python
# 开仓后注册价格门
register_gate(
    symbol=symbol,
    trigger_price=tp1,  # 到达第一止盈位
    callback="move_sl_to_breakeven",  # 移动止损到保本位
)

register_gate(
    symbol=symbol,
    trigger_price=entry + risk * 0.5,  # 盈利 0.5×风险
    callback="partial_close",  # 部分平仓
)
```

---

## ws_monitor 集成

**文件**: `v3/ws_monitor.py`

### 开仓顺序

```python
# 1. 挂止损单
ok, sl_id = place_sl(symbol, direction, sl_price, qty)
if not ok:
    logger.error(f"止损单挂单失败，放弃开仓")
    return

# 2. 确认开仓成功
position = api.get_position(symbol)

# 3. 注册价格门（移动止损触发点）
register_gates(symbol, entry, sl_price, tp1, tp2)

# 4. 写 pos_state
ps_write(symbol, direction, {
    "entry_price": entry,
    "sl_price": sl_price,
    "sl_order_id": sl_id,
    "tp1_price": tp1,
    "tp2_price": tp2,
    "tp1_hit": False,
})

# 5. 挂止盈单
place_tp(symbol, direction, tp1, qty*0.6, tp2, qty*0.4)
```

### 分批止盈触发

```python
# 轮询检查价格
if price >= tp1 and not position['tp1_hit']:
    # 平仓 60%
    close_position(symbol, percent=0.6)
    position['tp1_hit'] = True
    
    # 移动止损到保本位
    new_sl = entry + (tp1 - entry) * 0.1  # 锁定 10% 利润
    place_sl(symbol, direction, new_sl, remaining_qty)
    
    # 注册回落检查
    register_fall_check(symbol, tp1 - 0.5 * atr_baseline)

if price >= tp2:
    # 平仓剩余 40%
    close_position(symbol, percent=1.0)

# 回落检查
if position['tp1_hit'] and price < tp1 - 0.5 * atr_baseline:
    # 全部平仓不等 tp2
    close_position(symbol, percent=1.0)
```

---

## 下一步优化

### 1. 动态止损缓冲

当前使用固定系数（0.25/0.3/0.5），可改为基于：
- `bl.atr_std`：波动率大的币种用更大缓冲
- `bl.vol_p90`：异常放量时放宽止损

```python
# 示例：动态缓冲系数
if bl.atr_std / bl.atr_baseline > 0.5:  # 波动率离散度高
    sl_buffer = 0.5 * bl.atr_baseline  # 放宽到 0.5×ATR
else:
    sl_buffer = 0.25 * bl.atr_baseline  # 收紧到 0.25×ATR
```

### 2. 移动止损自动化

当前需要价格门触发，可改为：
- 每上涨 0.5×ATR，自动上移止损 0.25×ATR
- 追踪止损（Trailing Stop）：从最高点回撤 1×ATR 触发

### 3. 时间止损

开仓后 N 根 K 线仍未触及 tp1：
- N=20（20 小时）：平仓 50%
- N=40（40 小时）：全部平仓

### 4. 盈亏比分批止盈优化

根据 RR 动态调整 tp1/tp2 比例：
- RR≥3.0：tp1=50%, tp2=50%（让利润奔跑）
- RR=2.0~2.5：tp1=70%, tp2=30%（保守锁利）

---

## 总结

当前止盈止损规则已实现：

1. ✅ **场景化止损**：不同场景使用不同的止损缓冲系数
2. ✅ **分批止盈**：场景 C/G 支持 tp1+tp2 分批平仓
3. ✅ **止损巡逻**：每 3 分钟检查止损单是否丢失并补挂
4. ✅ **动态入场**：入场价基于动态 ATR 和假突破率
5. ✅ **RR 检查**：开仓前验证盈亏比是否满足最低要求

待优化：
- 动态止损缓冲（基于 atr_std）
- 移动止损自动化
- 时间止损
- 盈亏比分批止盈优化
