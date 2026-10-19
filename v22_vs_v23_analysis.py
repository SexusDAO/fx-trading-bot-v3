#!/usr/bin/env python3
"""
v2.2 vs v2.3 评分系统深度对比分析
找出高分信号消失的根本原因
"""

def compare_scoring():
    print("="*80)
    print("v2.2 vs v2.3 评分系统深度对比")
    print("="*80)
    
    print("\n【核心差异总览】")
    print("-"*80)
    
    comparisons = [
        ("基础分", "50 分", "0 分", "-50 分 ⬇️"),
        ("RSI 最高分", "10 分", "18 分", "+8 分 ⬆️"),
        ("RSI 扣分", "无", "最多 -25 分", "新增 ⚠️"),
        ("MACD 最高", "15 分", "15 分", "不变"),
        ("EMA 最高", "15 分", "20 分", "+5 分 ⬆️"),
        ("成交量最高", "15 分", "20 分", "+5 分 ⬆️"),
        ("支撑位", "无", "20 分", "新增 ⬆️"),
        ("背离加分", "20 分", "20 分", "不变"),
        ("趋势分", "10 分", "10 分", "不变"),
        ("连续 K 线", "无", "10 分", "新增 ⬆️"),
        ("理论满分", "115 分", "113 分", "-2 分"),
        ("实际典型分", "90-100 分", "40-70 分", "-40 分 ⬇️️"),
    ]
    
    print(f"{'评分项':<12} {'v2.2':<15} {'v2.3':<15} {'变化':<20}")
    print("-"*80)
    for item in comparisons:
        print(f"{item[0]:<12} {item[1]:<15} {item[2]:<15} {item[3]:<20}")
    
    print("\n\n【关键差异 1: 基础分归零】")
    print("-"*80)
    print("""
v2.2: 基础分 50 分
  → 所有标的起步就是 50 分
  → 相当于"保送"一半分数
  → 即使各项指标一般，也能轻松达到 60-70 分

v2.3: 基础分 0 分
  → 从 0 开始纯加分
  → 没有任何"保底分"
  → 每个指标都要真实得分

📉 影响：所有标的评分直接 -50 分
    """)
    
    print("\n【关键差异 2: RSI 超买超卖扣分】")
    print("-"*80)
    print("""
v2.2: 无扣分机制
  → RSI=88 仍然不扣分
  → 追高风险标的仍能获得高分

v2.3: 新增扣分机制
  → RSI>80: 扣 25 分 ⚠️
  → RSI>75: 扣 15 分 ⚠️
  → RSI<20: 扣 10 分 ⚠️

📉 影响：超买/超卖标的评分大幅下降
    """)
    
    print("\n【关键差异 3: 成交量算法修正】")
    print("-"*80)
    print("""
v2.2: 当前 K 线成交量 / 前 10 根均量
  → 当前 K 线可能未走完
  → 成交量天然偏低
  → 大部分标的量比<1.0，得 0 分

v2.3: 前一根已收盘 K 线 / 前 10 根均量
  → 使用已完成 K 线
  → 更准确反映真实成交量
  → 但仍严格，量比>2 才得满分

📊 影响：成交量评分更真实，但仍是主要扣分项
    """)
    
    print("\n【关键差异 4: 新增评分项】")
    print("-"*80)
    print("""
v2.3 新增:
  ✅ 支撑位评分 (20 分): 价格接近区间底部才高分
  ✅ 连续 K 线确认 (10 分): 需要连续 2 根同向 K 线

📊 影响：
  - 支撑位：高位标的只能拿 0-6 分
  - 连续 K 线：震荡行情中很难满足
    """)
    
    print("\n\n【实际案例对比：同一个标的，两个版本评分】")
    print("-"*80)
    
    # 模拟几个典型场景
    scenarios = [
        {
            "name": "场景 1: 强势上涨币（RSI 超买）",
            "metrics": {
                "rsi": 82, "macd_pos": True, "ema_bull": True,
                "vol_ratio": 0.8, "price_pos": 0.75, "trend_strong": True, "cont": True
            }
        },
        {
            "name": "场景 2: 震荡币（指标中性）",
            "metrics": {
                "rsi": 55, "macd_pos": True, "ema_mixed": True,
                "vol_ratio": 1.1, "price_pos": 0.5, "trend_weak": True, "cont": False
            }
        },
        {
            "name": "场景 3: 底部反弹币（理想）",
            "metrics": {
                "rsi": 58, "macd_pos": True, "ema_bull": True,
                "vol_ratio": 2.5, "price_pos": 0.2, "trend_strong": True, "cont": True
            }
        },
    ]
    
    def score_v22(m):
        score = 50  # 基础分
        score += 10 if 40<=m['rsi']<=60 else (5 if 30<=m['rsi']<40 or 60<m['rsi']<=70 else 0)
        score += 15 if m['macd_pos'] else 0
        score += 15 if m.get('ema_bull') or m.get('ema_bear') else 5
        score += 15 if m['vol_ratio']>2 else (10 if m['vol_ratio']>1.5 else (5 if m['vol_ratio']>1 else 0))
        score += 20 if m.get('divergence') else 5
        score += 10 if m.get('trend_strong') else (5 if m.get('trend_medium') else 0)
        return min(score, 100)
    
    def score_v23(m):
        score = 0  # 基础分归零
        # RSI
        if 45<=m['rsi']<=65: rs=18
        elif 40<=m['rsi']<45 or 65<m['rsi']<=70: rs=12
        elif 30<=m['rsi']<40 or 70<m['rsi']<=75: rs=6
        else: rs=0
        rp = 25 if m['rsi']>80 else (15 if m['rsi']>75 else (10 if m['rsi']<20 else 0))
        score += rs - rp
        # MACD
        score += 15 if m['macd_pos'] else 0
        # EMA
        score += 20 if m.get('ema_bull') else (5 if m.get('ema_mixed') else 0)
        # Volume
        score += 20 if m['vol_ratio']>2 else (15 if m['vol_ratio']>1.5 else (10 if m['vol_ratio']>1 else 5))
        # Support
        score += 20 if m['price_pos']<0.3 else (12 if m['price_pos']<0.5 else (6 if m['price_pos']<0.7 else 0))
        # Trend
        score += 10 if m.get('trend_strong') else (6 if m.get('trend_medium') else 3)
        # Continuous
        score += 10 if m.get('cont') else 0
        return min(score, 100)
    
    for scen in scenarios:
        m = scen['metrics']
        s22 = score_v22(m)
        s23 = score_v23(m)
        print(f"\n{scen['name']}")
        print(f"  指标：RSI={m['rsi']} | MACD={'正' if m['macd_pos'] else '负'} | EMA={'多' if m.get('ema_bull') else '混'} | Vol={m['vol_ratio']}x | Pos={m['price_pos']}")
        print(f"  v2.2 评分：{s22}分 {'✅ 开仓' if s22>=70 else '🟡 观察' if s22>=60 else '❌ 跳过'}")
        print(f"  v2.3 评分：{s23}分 {'✅ 开仓' if s23>=70 else '🟡 观察' if s23>=60 else '❌ 跳过'}")
        print(f"  变化：{s23-s22:+d}分")
    
    print("\n\n【高分信号消失的 5 大原因】")
    print("-"*80)
    
    reasons = [
        ("基础分归零", "-50 分", "所有标的起步分从 50 降至 0"),
        ("RSI 超买扣分", "-15~25 分", "RSI>75 的标的被大幅扣分"),
        ("支撑位要求", "-10~20 分", "高位标的支撑位只能拿 0-6 分"),
        ("成交量严格", "-5~10 分", "量比<1.5 只能拿 5-10 分"),
        ("连续 K 线", "-10 分", "震荡行情中很难连续同向"),
    ]
    
    print(f"{'原因':<20} {'影响':<15} {'说明':<40}")
    print("-"*80)
    for r in reasons:
        print(f"{r[0]:<20} {r[1]:<15} {r[2]:<40}")
    
    total_impact = sum([-50, -20, -15, -10, -10])
    print(f"\n  累计影响：约 {total_impact} 分")
    
    print("\n\n【为什么 v2.2 普遍 90+ 分？】")
    print("-"*80)
    print("""
v2.2 高分公式:
  基础分 50 + RSI(10) + MACD(15) + EMA(15) + 背离 (20) = 110 分

即使:
  - 成交量 0 分（量比不足）
  - 趋势 0 分（无明显趋势）
  - RSI 超买不扣分

仍能轻松获得 80-90 分！

这就是为什么 v2.2 回测中 90% 标的触发开仓。
    """)
    
    print("\n【为什么 v2.3 普遍 40-60 分？】")
    print("-"*80)
    print("""
v2.3 典型评分（中性标的）:
  RSI(55): 18 分 ✅
  MACD(正): 15 分 ✅
  EMA(混): 5 分 ⚠️
  Volume(1.1x): 10 分 ⚠️
  Support(0.5): 12 分 ⚠️
  Trend(弱): 3 分 ⚠️
  Continuous(无): 0 分 ❌
  ───────────────────────
  总计：63 分 → 观察区边缘

如果 RSI>75:
  再扣 15-25 分 → 降至 38-48 分 → 跳过

这就是为什么 v2.3 开仓率从 90% 降至 1-2%。
    """)
    
    print("\n\n【系统设计哲学的变化】")
    print("-"*80)
    print("""
v2.2: "宁可错杀，不可错过"
  → 宽松评分，普遍高分
  → 开仓率 90%，资金分散
  → 风险：过度交易，追高风险

v2.3: "宁可错过，不可做错"
  → 严格评分，筛选高质量信号
  → 开仓率 1-2%，集中资金
  → 风险：错过机会，但避免大亏

这是从"激进"到"稳健"的根本转变。
    """)
    
    print("\n\n【是否需要调整？】")
    print("-"*80)
    print("""
如果开仓率持续<2%，可以考虑:

1. 微调开仓门槛：70 分 → 68 分
   → 预计开仓率从 1% 升至 3-5%

2. 降低成交量权重：20 分 → 15 分
   → 量比 0.8-1.5 的标的少扣 5 分

3. 观察区转开仓机制:
   → 60-69 分标的，若连续 2 次评分上升，可转开仓

4. 双轨制:
   → 趋势策略：≥70 分（100% 仓位）
   → 稳健策略：≥65 分（50% 仓位）

⚠️ 但注意：v2.3 的设计意图就是严格筛选
   调整前需确认是"系统过严"还是"市场确实无机会"
    """)
    
    print("\n" + "="*80)
    print("分析完成")
    print("="*80)

if __name__ == '__main__':
    compare_scoring()
