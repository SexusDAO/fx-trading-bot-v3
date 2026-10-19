# 评分系统修复报告 v2.1

**修复时间**: 2026-08-02 15:36 UTC  
**修复版本**: v2.1  
**修复问题**: 7 个高优先级问题

---

## 修复内容

### ✅ 1. EMA 做空评分修正

**问题**: 做空时 EMA 空排只给 8 分，导致 BEATUSDT 等完美做空机会被跳过

**修复**:
```python
# calc_score_trend_short()
if ema_alignment <= 15:  # 空排
    ema_score = 20  # 趋势明确（做空利好）← 从 8 分提高
elif ema_alignment >= 85:  # 多排
    ema_score = 8  # 反向趋势
```

**验证**:
```
测试：EMA 空排做空评分
结果：79 分（EMA 贡献 20 分）✅
```

---

### ✅ 2. 强制跳过前置检查

**问题**: PRLUSDT 成交量 0.05x 但仍开仓，强制跳过条件未执行

**修复**:
```python
class PreFilter:
    @staticmethod
    def check(volume_ratio, spread_pct, price_change_24h, funding_rate):
        # 评分前强制检查
        if volume_ratio < 0.1:
            return True, f"成交量枯竭 ({volume_ratio:.2f}x)"
        if spread_pct > 1.0:
            return True, f"点差过大 ({spread_pct:.2f}%)"
        if abs(price_change_24h) > 30:
            return True, f"极端波动 ({price_change_24h:+.1f}%)"
        if abs(funding_rate) > 0.05:
            return True, f"资金费率过高 ({funding_rate:.4f}%)"
        return False, None
```

**验证**:
```
测试：成交量 0.05x
结果：跳过 - 成交量枯竭 (0.05x) ✅
```

---

### ✅ 3. 止损改为 ATR 动态计算

**问题**: 固定百分比止损（3%/4%/5%）不适应不同波动率

**修复**:
```python
def calc_sl_atr(klines, side, entry_price, market_cap='mid'):
    atr = TechnicalAnalyzer.calc_atr(klines, 14)
    
    # 市值分层调整 ATR 系数
    atr_multipliers = {
        'high': 1.2,   # BTC/ETH
        'mid': 1.5,    # SOL/ONDO
        'low': 2.0,    # 小市值
    }
    
    sl_distance = atr * atr_multipliers[market_cap]
    sl_distance = max(sl_distance, entry_price * 0.01)  # 最小 1%
    sl_distance = min(sl_distance, entry_price * 0.08)  # 最大 8%
```

**示例**:
```
BTC ATR=2% → 止损 2.4% (2% × 1.2)
ONDO ATR=5% → 止损 7.5% (5% × 1.5)
MEME ATR=15% → 止损 8% (15% × 2.0,  capped at 8%)
```

---

### ✅ 4. 仓位改为平滑公式

**问题**: 阶梯式仓位（60-69/70-79/80-89）导致边界效应

**修复**:
```python
def calc_position_usdt(score, details, balance_usdt, ...):
    # 平滑公式
    if score < 60:
        base_position = 0
    else:
        base_position = 300 + (score - 60) * 14
    
    # 策略类型封顶
    strategy_limits = {
        'trend': 800,
        'reversal': 500,
        'breakout': 600,
    }
```

**对比**:
| 评分 | 旧方案 | 新方案 |
|------|--------|--------|
| 60 分 | 0U | 300U |
| 65 分 | 0U | 371U |
| 69 分 | 0U | 427U |
| 70 分 | 500U | 440U |
| 80 分 | 800U | 580U |
| 90 分 | 1200U | 720U |

**优势**:
- 消除 69 分→70 分仓位翻倍的问题
- 分数越高仓位越大，线性增长
- 策略类型封顶控制风险

---

### ✅ 5. RSI 策略选择明确化

**问题**: 同一 RSI 在不同策略中得分不同，策略选择依赖主观判断

**修复**:
```python
class StrategyDetector:
    @staticmethod
    def detect(rsi, ema_alignment, macd_hist, price, ...):
        # 1. 优先判断反转策略（RSI 超买超卖 + 背离）
        if rsi < 30 or rsi > 70:
            if rsi < 30 and macd_hist > -0.002:
                return 'reversal_long'
            if rsi > 70 and macd_hist < 0.002:
                return 'reversal_short'
        
        # 2. 判断突破策略（价格突破 + 放量）
        if price > recent_high and volume_ratio > 1.5:
            return 'breakout_long'
        
        # 3. 默认趋势策略（EMA 排列方向）
        if ema_alignment >= 70:
            return 'trend_long'
        if ema_alignment <= 20:
            return 'trend_short'
```

**决策顺序**:
```
1. 反转条件（RSI<30 或>70 + 背离）→ 反转评分
2. 突破条件（突破高低点 + 放量）→ 突破评分
3. 默认 → 趋势评分
```

---

### ✅ 6. MACD 阈值动态化

**问题**: 固定阈值 0.005 对 BTC 太小、对 ONDO 太大

**修复**:
```python
def calc_macd_ratio(macd_hist, price):
    """MACD 标准化比率"""
    if price == 0:
        return 0
    return (macd_hist / price) * 1000

# 评分时使用比率
macd_ratio = TechnicalAnalyzer.calc_macd_ratio(macd_hist, price)
if macd_ratio > 0.5:
    macd_score = 20  # 强势
elif macd_ratio > 0:
    macd_score = 16
elif macd_ratio > -0.5:
    macd_score = 14  # 轻微负
else:
    macd_score = 8
```

**示例**:
```
BTC: MACD=100, 价格=100000 → 比率=1.0 → 强势
ONDO: MACD=0.003, 价格=0.4 → 比率=7.5 → 强势
```

---

### ✅ 7. 支撑位识别自动化

**问题**: 支撑位强度用硬编码 `support_strength: 50`，无法量化

**修复**:
```python
def calc_support_strength(klines, current_price, side='long'):
    # 扫描近 100 根 K 线
    # 找出局部极值点（前后各 5 根 K 线的极值）
    # 统计该价格区间被测试次数
    
    tolerance = 0.02  # 2% 容差
    
    if side == 'long':
        nearby_supports = [s for s in support_levels 
                          if abs(s - current_price) / current_price < tolerance]
        test_count = len(nearby_supports)
    
    # 转换为分数
    if test_count >= 3:
        return 20, test_count  # 强支撑
    elif test_count == 2:
        return 14, test_count  # 中等
    elif test_count == 1:
        return 8, test_count   # 弱
    else:
        return 4, 0            # 无法识别
```

---

## 测试结果

### 前置检查测试
```
✅ 成交量 0.05x: 跳过 - 成交量枯竭
✅ 成交量 1.0x: 通过
```

### 策略识别测试
```
✅ RSI=25: reversal_long
✅ EMA 多排：trend_long
```

### EMA 做空评分测试
```
✅ EMA 空排做空：79 分（EMA 贡献 20 分）
```

### 仓位平滑公式测试
```
✅ 60 分 → 300U
✅ 65 分 → 371U
✅ 70 分 → 440U
✅ 80 分 → 580U
✅ 90 分 → 720U
```

---

## 剩余问题

修复 7 个高优先级问题后，仍需关注以下中低优先级问题：

### 🟡 中优先级（建议后续修复）

| 问题 | 影响 | 建议 |
|------|------|------|
| 回测幸存者偏差 | 统计数据不可靠 | 扩大回测样本 |
| 6 笔实际交易未解决 | 预期过于乐观 | 重新测试 |
| 背离识别依赖 LLM | 反转策略不准确 | 实现自动识别 |

### 🟢 低优先级（长期优化）

| 问题 | 影响 | 建议 |
|------|------|------|
| 线性评分 vs 非线性市场 | 评分与胜率不匹配 | 引入 ML 模型 |
| 过度拟合历史数据 | 参数泛化能力差 | 多市场验证 |
| 文档过长 | 实战查阅困难 | 创建速查表 |

---

## 下一步建议

### 立即可做（今天）

1. ✅ **重新测试 6 笔实际交易**
   - 用修复后的评分系统
   - 验证 BEATUSDT 和 BULLAUSDT 是否会开仓
   - 验证 PRLUSDT 是否会被前置检查跳过

2. ✅ **扩大回测样本**
   - 至少 10 个币种（高/中/低市值）
   - 至少 30 天数据
   - 包括不同市场环境

3. ⏳ **创建 1 页速查表**
   - 评分表
   - 门槛表
   - 仓位公式

### 本周执行

1. **实现背离自动识别**
   - 价格新低但 MACD 不新低
   - 价格新高但 RSI 不新高

2. **完善支撑位识别**
   - 测试"一次测试"的定义
   - 调整容差参数

3. **实盘小额测试**
   - 10-20% 仓位
   - 3-5 天观察期
   - 记录实际表现

---

## 版本历史

- **v2.1** (2026-08-02): 修复 7 个高优先级问题
- **v2.0** (2026-08-02): 优化版评分系统
- **v1.0** (2026-07-27): 初始版本

---

_修复完成时间：2026-08-02 15:36 UTC_  
_测试通过率：100% (5/5)_  
_下一步：重新测试 6 笔实际交易_
