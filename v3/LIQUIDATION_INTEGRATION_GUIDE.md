# 清算区集成指南 v3.2

本文档说明如何将清算区止盈止损逻辑完整集成到 V3 系统中。

---

## ✅ 已完成的工作

### 1. 核心模块创建

| 文件 | 状态 | 说明 |
|------|------|------|
| `modules/sl_handler_liquidation.py` | ✅ 完成 | 清算区止盈止损计算核心 |
| `modules/liquidation_data.py` | ✅ 完成 | Coinglass API 数据获取 |
| `modules/scene_matcher_dynamic.py` | 🔄 部分完成 | 已添加导入，需更新函数签名和计算逻辑 |

### 2. 核心函数

```python
# sl_handler_liquidation.py
calc_tp_with_liquidation(entry, sl, direction, candles, baseline, liq_data)
calc_sl_with_liquidation(entry, direction, scene, candles, baseline, liq_data)
calc_batch_tp_with_liquidation(entry, sl, direction, candles, baseline, liq_data)
check_liquidation_block(entry, tp, direction, liq_data, baseline)
```

---

## 📝 待完成的工作

### 步骤 1：更新 scene_matcher_dynamic.py 函数签名

**目标函数**（6 个）：
- `_scene_a_dynamic()` — 回调入场（LONG）
- `_scene_b_dynamic()` — 蓄势突破（LONG）
- `_scene_c_dynamic()` — 强势回踩（LONG，分批止盈）
- `_scene_e_dynamic()` — 反弹做空（SHORT）
- `_scene_f_dynamic()` — 破位做空（SHORT）
- `_scene_g_dynamic()` — 强势反弹（SHORT，分批止盈）

**修改前**：
```python
def _scene_a_dynamic(k1h: list, mark_price: float,
                     direction: str, bl: DynamicBaseline) -> MatchResult:
```

**修改后**：
```python
def _scene_a_dynamic(k1h: list, mark_price: float,
                     direction: str, bl: DynamicBaseline,
                     liq_data: LiquidationData = None) -> MatchResult:
```

**主接口修改**：
```python
def match_dynamic(k1h: list, mark_price: float,
                  direction: str, track: str = "oscillation",
                  bl: Optional[DynamicBaseline] = None,
                  symbol: str = "",
                  liq_data: LiquidationData = None) -> MatchResult:
```

---

### 步骤 2：替换止盈止损计算逻辑

#### 场景 A（回调入场 LONG）示例

**原逻辑**（在 `_scene_a_dynamic` 中）：
```python
# 止损
recent_low = min(lows[-5:])
sl = round(recent_low - bl.atr_baseline * 0.5, 8)

# 止盈
risk = entry - sl
tp = round(entry + risk * 2.0, 8)
rr = round((tp - entry) / risk, 2)
```

**新逻辑**（集成清算区）：
```python
# 止损（使用清算区调整）
sl = calc_sl_with_liquidation(
    entry=entry,
    direction="LONG",
    scene="A_pullback",
    candles=k1h,
    baseline=bl,
    liq_data=liq_data
)

# 止盈（优先使用清算区）
tp, tp_source = calc_tp_with_liquidation(
    entry=entry,
    sl=sl,
    direction="LONG",
    candles=k1h,
    baseline=bl,
    liq_data=liq_data
)

# 记录止盈来源（用于日志）
cond["tp_source"] = tp_source  # "liq_zone" | "structure" | "rr_fallback"

# 计算 RR
rr = round((tp - entry) / (entry - sl), 2) if entry > sl else 0
```

#### 场景 C（强势回踩 LONG，分批止盈）示例

**原逻辑**：
```python
tp1 = round(swing_high, 8)
tp2 = round(swing_high + risk * 0.5, 8)
```

**新逻辑**：
```python
tp1, tp2 = calc_batch_tp_with_liquidation(
    entry=entry,
    sl=sl,
    direction="LONG",
    candles=k1h,
    baseline=bl,
    liq_data=liq_data
)

# 记录来源
cond["tp1_source"] = "liq_zone" if tp1 != swing_high else "structure"
cond["tp2_source"] = "liq_zone_max" if tp2 != swing_high + risk * 0.5 else "structure"
```

#### 场景 E/F/G（SHORT）对称处理

与 LONG 场景完全对称，只需将 `direction="SHORT"` 传入即可。

---

### 步骤 3：更新 MatchResult 类型定义

**修改前**：
```python
class MatchResult(TypedDict):
    matched:      bool
    scene:        str
    direction:    str
    entry_price:  float
    sl_price:     float
    tp_price:     float
    tp1_price:    Optional[float]
    tp2_price:    Optional[float]
    rr:           float
    second_score: int
    reason:       str
    conditions:   dict
```

**修改后**（新增来源字段）：
```python
class MatchResult(TypedDict):
    matched:      bool
    scene:        str
    direction:    str
    entry_price:  float
    sl_price:     float
    tp_price:     float
    tp1_price:    Optional[float]
    tp2_price:    Optional[float]
    rr:           float
    second_score: int
    reason:       str
    conditions:   dict
    # 新增字段（可选）
    tp_source:    Optional[str]  # "liq_zone" | "structure" | "rr_fallback"
    sl_source:    Optional[str]  # "structure" | "liq_adjusted"
```

---

### 步骤 4：更新 scanner_llm.py 传递 liq_data

在 `scanner_llm.py` 的信号扫描主循环中：

**修改前**：
```python
for symbol in symbols:
    k1h = fetch_klines(symbol, "1h", limit=200)
    mark_price = get_mark_price(symbol)
    
    result_long = match_dynamic(k1h, mark_price, "LONG", track, bl)
    result_short = match_dynamic(k1h, mark_price, "SHORT", track, bl)
```

**修改后**：
```python
from modules.liquidation_data import fetch_liquidation_for_symbol

for symbol in symbols:
    k1h = fetch_klines(symbol, "1h", limit=200)
    mark_price = get_mark_price(symbol)
    
    # 获取清算数据（失败时返回 None，函数内部会降级）
    liq_data = fetch_liquidation_for_symbol(symbol)
    
    result_long = match_dynamic(k1h, mark_price, "LONG", track, bl, symbol, liq_data)
    result_short = match_dynamic(k1h, mark_price, "SHORT", track, bl, symbol, liq_data)
    
    # 记录日志
    if result_long["matched"]:
        tp_src = result_long.get("tp_source", "unknown")
        logger.info(f"{symbol} LONG tp_source={tp_src}")
```

---

### 步骤 5：更新 ws_monitor.py 支持新字段

在 `ws_monitor.py` 的仓位状态管理中：

**修改前**：
```python
pos_state = {
    "symbol": symbol,
    "entry": entry,
    "sl": sl,
    "tp": tp,
    "tp1": tp1,
    "tp2": tp2,
    ...
}
```

**修改后**：
```python
pos_state = {
    "symbol": symbol,
    "entry": entry,
    "sl": sl,
    "tp": tp,
    "tp1": tp1,
    "tp2": tp2,
    "tp_source": match_result.get("tp_source", "unknown"),
    "sl_source": match_result.get("sl_source", "structure"),
    ...
}
```

**日志输出增强**：
```python
logger.info(
    f"🦞 开仓 {symbol} {direction} | "
    f"entry={entry} sl={sl} tp={tp} | "
    f"tp_source={pos_state['tp_source']} sl_source={pos_state['sl_source']}"
)
```

---

### 步骤 6：更新 pos_state_store.py

在 `modules/pos_state_store.py` 中：

```python
class PositionState(TypedDict, total=False):
    symbol: str
    direction: str
    entry: float
    sl: float
    tp: float
    tp1: Optional[float]
    tp2: Optional[float]
    # 新增字段
    tp_source: str  # "liq_zone" | "structure" | "rr_fallback"
    sl_source: str  # "structure" | "liq_adjusted"
    liq_data_ts: float  # 清算数据时间戳（用于过期检查）
```

---

## 🧪 测试计划

### 干跑测试

```bash
cd v3
python tests/test_liquidation_integration.py
```

**预期输出**：
- ✅ 止盈来源统计：liq_zone 60%, structure 35%, rr_fallback 5%
- ✅ 止损调整率：~20% 的止损被清算区调整
- ✅ 入场过滤：~10% 的信号因清算区阻挡被过滤

### 日志验证

检查 `logs/scanner.log`：
```
2026-08-03 16:00:00 INFO BTCUSDT LONG tp_source=liq_zone sl_source=structure
2026-08-03 16:00:00 INFO ETHUSDT SHORT tp_source=structure sl_source=liq_adjusted
2026-08-03 16:00:00 INFO SOLUSDT LONG 被清算区阻挡，进观察池
```

---

## ⚠️ 注意事项

### 1. API 降级处理

当 Coinglass API 失败时，`liq_data` 为 `None` 或空数据：
- `calc_tp_with_liquidation()` 会自动降级到结构位/固定 RR
- `calc_sl_with_liquidation()` 会保持结构止损不变
- **系统不会崩溃，只是不使用清算区优化**

### 2. 清算数据过期

建议每 30 分钟刷新一次清算数据：
```python
if liq_data and (time.time() - liq_data.timestamp) > 1800:
    liq_data = fetch_liquidation_for_symbol(symbol)  # 重新获取
```

### 3. 性能影响

单次 API 调用约 200-500ms，建议：
- 批量获取：`fetcher.fetch_batch(symbols)` 而非逐个获取
- 缓存：同一轮扫描中复用 liq_data
- 异步：在后台线程预取下一轮数据

---

## 📊 预期效果

| 指标 | 改前 | 改后（预期） |
|------|------|-------------|
| 止盈命中率 | ~60% | ~75% |
| 平均 RR | 2.0~2.5 | 2.2~2.8 |
| 被扫止损率 | ~40% | ~30% |
| 阻挡信号过滤 | N/A | ~10-15% |

---

## 🔗 相关文件

- `modules/sl_handler_liquidation.py` — 核心计算
- `modules/liquidation_data.py` — 数据获取
- `modules/scene_matcher_dynamic.py` — 场景匹配（需更新）
- `scanner_llm.py` — 信号扫描（需更新）
- `ws_monitor.py` — 开仓监控（需更新）
- `modules/pos_state_store.py` — 状态存储（需更新）

---

**集成完成后，系统止盈止损将基于市场最真实的流动性分布，而非纯技术指标。** 🦞
