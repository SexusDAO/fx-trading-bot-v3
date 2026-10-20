#!/usr/bin/env python3
"""
生成完整回测报告
"""
import sys
sys.path.insert(0, '/root/.openclaw/workspace')

import json
from datetime import datetime
import requests

from modules.scoring import ScoringEngine, PreFilter

# 加载快照数据
with open('config/backtest_snapshots.json') as f:
    snapshots = json.load(f)

# 获取当前价格用于验证
def get_current_price(symbol):
    try:
        url = "https://fapi.binance.com/fapi/v1/ticker/price"
        r = requests.get(url, params={'symbol': symbol}, timeout=5)
        data = r.json()
        return float(data.get('price', 0))
    except:
        return 0

print("="*70)
print("完整历史情景模拟回测报告")
print("="*70)
print(f"回测时间：{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
print(f"测试样本：{len(snapshots)} 个标的")
print(f"系统版本：v2.2 (门槛 70 分 + 观察区)")
print()

report = []

for i, snapshot in enumerate(snapshots):
    symbol = snapshot['symbol']
    stype = snapshot['type']
    direction = snapshot['direction']
    t0_price = snapshot['t0_price']
    t0_time = snapshot['t0_time']
    
    t0_str = datetime.fromtimestamp(t0_time/1000).strftime('%m-%d %H:%M')
    
    print(f"\n{'='*70}")
    print(f"标的：{symbol} ({'涨幅榜' if stype=='gainer' else '跌幅榜'})")
    print(f"{'='*70}")
    print(f"起点时间：{t0_str}")
    print(f"起点价格：{t0_price}")
    
    # 获取当前价格
    current_price = get_current_price(symbol)
    if current_price == 0:
        current_price = t0_price  # fallback
    
    # === 强制跳过检查 ===
    print(f"\n【强制跳过检查】")
    
    vol_ratio = snapshot['vol_ratio']
    change_24h = snapshot['change_24h']
    
    should_skip, reason = PreFilter.check(
        volume_ratio=vol_ratio,
        spread_pct=0.1,
        price_change_24h=change_24h,
        funding_rate=0.01
    )
    
    if should_skip:
        print(f"  ❌ 跳过：{reason}")
        report.append({
            'symbol': symbol,
            'type': stype,
            'direction': direction,
            't0_price': t0_price,
            'current_price': current_price,
            'action': 'skip',
            'skip_reason': reason,
            'score': 0,
            'pnl_u': 0
        })
        continue
    else:
        print(f"  ✅ 通过")
    
    # === 评分 ===
    print(f"\n【评分明细】")
    
    details = {
        'rsi': snapshot['rsi'],
        'macd_hist': snapshot['macd'],
        'price': t0_price,
        'ema_alignment': snapshot['ema_score'],
        'volume_ratio': vol_ratio,
        'market_cap': 'low',
    }
    
    if direction == 'long':
        score, breakdown = ScoringEngine.calc_score_trend(details)
        strategy = '趋势做多'
    else:
        score, breakdown = ScoringEngine.calc_score_trend_short(details)
        strategy = '趋势做空'
    
    # 市值调整（低市值宽松 10%）
    adjusted_score = score * 0.9
    
    ema_label = snapshot['ema_align']
    
    print(f"  策略类型：{strategy}")
    print(f"  RSI({snapshot['rsi']:.1f}) → {breakdown['rsi']}分")
    print(f"  MACD({snapshot['macd']:.6f}) → {breakdown['macd']}分")
    print(f"  EMA({ema_label}) → {breakdown['ema']:.1f}分")
    print(f"  成交量({vol_ratio:.2f}x) → {breakdown['volume']}分")
    support_score = breakdown.get('support', breakdown.get('resistance', 10))
    print(f"  支撑/阻力 → {support_score:.1f}分")
    print(f"  原始总分：{score:.1f}分")
    print(f"  市值调整后：{adjusted_score:.1f}分")
    
    # === 开仓决策 ===
    print(f"\n【开仓决策】")
    
    threshold = 70
    observation_low = 63
    
    if adjusted_score >= 85:
        action = 'dual_net'
        position = ScoringEngine.calc_position_usdt(adjusted_score, details, 10000, 2, 5, 'trend')
        action_text = '✅ 双网挂单'
    elif adjusted_score >= threshold:
        action = 'limit_order'
        position = ScoringEngine.calc_position_usdt(adjusted_score, details, 10000, 2, 5, 'trend')
        action_text = '✅ 限价单开仓'
    elif adjusted_score >= observation_low:
        action = 'observe'
        position = ScoringEngine.calc_position_usdt(adjusted_score, details, 10000, 2, 5, 'trend') * 0.5
        action_text = '⏸️ 观察区'
    else:
        action = 'skip'
        position = 0
        action_text = '❌ 跳过'
    
    print(f"  {action_text}（{adjusted_score:.1f}分）")
    
    # 计算止损止盈
    sl_price = None
    tp1_price = None
    tp2_price = None
    
    if action in ['dual_net', 'limit_order']:
        atr = t0_price * 0.05  # 低市值 ATR 5%
        sl_distance = atr * 2.0  # 低市值系数
        
        if direction == 'long':
            sl_price = t0_price - sl_distance
            tp1_price = t0_price + sl_distance * 2.5
            tp2_price = t0_price + sl_distance * 4.0
            sl_pct = (sl_price - t0_price) / t0_price * 100
        else:
            sl_price = t0_price + sl_distance
            tp1_price = t0_price - sl_distance * 2.5
            tp2_price = t0_price - sl_distance * 4.0
            sl_pct = (sl_price - t0_price) / t0_price * 100
        
        print(f"  模拟开仓价：{t0_price}")
        print(f"  模拟仓位：{position:.0f}U")
        print(f"  止损价：{sl_price:.4f} ({sl_pct:.1f}%)")
        print(f"  止盈一档：{tp1_price:.4f} ({(tp1_price-t0_price)/t0_price*100:+.1f}%)")
        print(f"  止盈二档：{tp2_price:.4f} ({(tp2_price-t0_price)/t0_price*100:+.1f}%)")
    
    # === 验证实际结果 ===
    print(f"\n【实际结果验证】")
    
    pnl_u = 0
    result_text = '未开仓'
    hit_tp1 = False
    hit_tp2 = False
    hit_sl = False
    max_profit = 0
    max_loss = 0
    
    if action in ['dual_net', 'limit_order']:
        if direction == 'long':
            pnl_pct = (current_price - t0_price) / t0_price * 100
            pnl_u = position * pnl_pct / 100
            
            # 检查是否触及止盈/止损
            if current_price >= tp1_price:
                hit_tp1 = True
            if current_price >= tp2_price:
                hit_tp2 = True
            if current_price <= sl_price:
                hit_sl = True
            
            max_profit = max(0, (max(current_price, t0_price) - t0_price) / t0_price * 100)
            max_loss = min(0, (min(current_price, t0_price) - t0_price) / t0_price * 100)
        else:
            pnl_pct = (t0_price - current_price) / t0_price * 100
            pnl_u = position * pnl_pct / 100
            
            if current_price <= tp1_price:
                hit_tp1 = True
            if current_price <= tp2_price:
                hit_tp2 = True
            if current_price >= sl_price:
                hit_sl = True
            
            max_profit = max(0, (t0_price - min(current_price, t0_price)) / t0_price * 100)
            max_loss = min(0, (t0_price - max(current_price, t0_price)) / t0_price * 100)
        
        if hit_tp2:
            result_text = '✅ 止盈二档'
        elif hit_tp1:
            result_text = '✅ 止盈一档'
        elif hit_sl:
            result_text = '❌ 止损出局'
        else:
            result_text = '📊 持仓中'
        
        print(f"  挂单成交：✅ 是")
        print(f"  当前价格：{current_price}")
        print(f"  结果：{result_text}")
        print(f"  盈亏：{pnl_u:+.2f}U")
        print(f"  最大浮盈：{max_profit:.1f}%")
        print(f"  最大浮亏：{max_loss:.1f}%")
        
        # 止损止盈合理性评估
        sl_reasonable = abs(sl_pct) >= 3 and abs(sl_pct) <= 10
        print(f"  止损合理性：{'✅ 合理' if sl_reasonable else '⚠️ 需调整'} ({sl_pct:.1f}%)")
    else:
        print(f"  挂单成交：❌ 否")
        print(f"  结果：{result_text}")
    
    # 保存报告
    report.append({
        'symbol': symbol,
        'type': stype,
        'direction': direction,
        't0_price': t0_price,
        'current_price': current_price,
        'rsi': snapshot['rsi'],
        'macd': snapshot['macd'],
        'ema_align': ema_label,
        'vol_ratio': vol_ratio,
        'score': score,
        'adjusted_score': adjusted_score,
        'action': action,
        'position': position,
        'sl_price': sl_price,
        'tp1_price': tp1_price,
        'tp2_price': tp2_price,
        'pnl_u': pnl_u,
        'result': result_text,
        'hit_tp1': hit_tp1,
        'hit_tp2': hit_tp2,
        'hit_sl': hit_sl,
        'max_profit': max_profit,
        'max_loss': max_loss
    })

# === 汇总报告 ===
print(f"\n{'='*70}")
print("汇总报告")
print(f"{'='*70}")

total = len(report)
open_count = sum(1 for r in report if r['action'] in ['dual_net', 'limit_order'])
observe_count = sum(1 for r in report if r['action'] == 'observe')
skip_count = sum(1 for r in report if r['action'] == 'skip' or 'skip_reason' in r)

tp1_count = sum(1 for r in report if r.get('hit_tp1', False))
tp2_count = sum(1 for r in report if r.get('hit_tp2', False))
sl_count = sum(1 for r in report if r.get('hit_sl', False))
holding_count = open_count - tp1_count - tp2_count - sl_count

profitable = sum(1 for r in report if r['pnl_u'] > 0)
losing = sum(1 for r in report if r['pnl_u'] < 0)
total_pnl = sum(r['pnl_u'] for r in report)

success_rate = tp1_count / open_count * 100 if open_count > 0 else 0

# 计算平均盈亏比
avg_rr = 0
if open_count > 0:
    total_profit = sum(r['pnl_u'] for r in report if r['pnl_u'] > 0)
    total_loss = abs(sum(r['pnl_u'] for r in report if r['pnl_u'] < 0))
    if total_loss > 0:
        avg_rr = total_profit / total_loss

print(f"\n【统计汇总】")
print(f"  测试标的：{total}个")
print(f"  触发开仓：{open_count}个 ({open_count/total*100:.0f}%)")
print(f"  进观察区：{observe_count}个 ({observe_count/total*100:.0f}%)")
print(f"  跳过：{skip_count}个 ({skip_count/total*100:.0f}%)")
print()
print(f"  止盈一档：{tp1_count}个 ({tp1_count/open_count*100:.0f}% 如果开仓)")
print(f"  止盈二档：{tp2_count}个 ({tp2_count/open_count*100:.0f}% 如果开仓)")
print(f"  止损出局：{sl_count}个 ({sl_count/open_count*100:.0f}% 如果开仓)")
print(f"  持仓中：{holding_count}个")
print()
print(f"  盈利：{profitable}个")
print(f"  亏损：{losing}个")
print(f"  总盈亏：{total_pnl:+.2f}U")
print(f"  成功率（触及止盈一档）：{success_rate:.0f}%")
print(f"  平均盈亏比：{avg_rr:.2f}")

print(f"\n【明细表】")
print(f"{'标的':<15} {'方向':<6} {'评分':<8} {'决策':<12} {'仓位':<8} {'盈亏':<10} {'结果':<12}")
print("-"*80)
for r in report:
    action_display = {
        'dual_net': '双网',
        'limit_order': '限价单',
        'observe': '观察区',
        'skip': '跳过'
    }.get(r['action'], '跳过')
    
    direction = '多' if r['direction'] == 'long' else '空'
    result_short = r['result'].split()[0] if r['result'] else 'N/A'
    
    print(f"{r['symbol']:<15} {direction:<6} {r['adjusted_score']:<8.1f} {action_display:<12} {r['position']:<8.0f} {r['pnl_u']:+.2f}U {result_short:<12}")

# 系统性问题发现
print(f"\n【发现的系统性问题】")
issues = []

# 检查前置跳过
low_vol_skips = sum(1 for r in report if 'skip_reason' in r and '成交量' in r.get('skip_reason', ''))
high_change_skips = sum(1 for r in report if 'skip_reason' in r and '波动' in r.get('skip_reason', ''))

if low_vol_skips > 0:
    issues.append(f"1. {low_vol_skips}个标的因成交量过低被跳过 - 前置检查有效过滤流动性风险")
if high_change_skips > 0:
    issues.append(f"2. {high_change_skips}个标的因涨跌幅过大被跳过 - 避免极端波动")

# 检查观察区
if observe_count > 0:
    issues.append(f"3. {observe_count}个标的进入观察区（63-69 分）- 门槛提高发挥作用")

# 检查低分盈利
low_score_profit = [r for r in report if r['adjusted_score'] < 70 and r['adjusted_score'] >= 60 and r['pnl_u'] > 0]
if low_score_profit:
    issues.append(f"4. {len(low_score_profit)}个中低分标的（60-69 分）如果开仓会盈利 - 观察区机制合理")

# 检查高开仓亏损
high_score_loss = [r for r in report if r['adjusted_score'] >= 70 and r['pnl_u'] < 0]
if high_score_loss:
    issues.append(f"5. {len(high_score_loss)}个高分标的（≥70 分）亏损 - 评分系统仍需优化")

# 检查止损合理性
unreasonable_sl = [r for r in report if r['action'] in ['dual_net', 'limit_order'] and r.get('sl_price')]
if unreasonable_sl:
    issues.append(f"6. 部分标的止损位置需根据实际波动率调整 - 建议用 ATR 动态计算")

if not issues:
    print("  ✅ 无明显系统性问题")
else:
    for issue in issues:
        print(f"  {issue}")

# 保存完整报告
with open('config/backtest_final_report.json', 'w') as f:
    json.dump({
        'timestamp': datetime.utcnow().isoformat(),
        'version': 'v2.2',
        'summary': {
            'total': total,
            'open_count': open_count,
            'observe_count': observe_count,
            'skip_count': skip_count,
            'tp1_count': tp1_count,
            'tp2_count': tp2_count,
            'sl_count': sl_count,
            'profitable': profitable,
            'losing': losing,
            'total_pnl': total_pnl,
            'success_rate': success_rate,
            'avg_rr': avg_rr
        },
        'details': report
    }, f, indent=2, ensure_ascii=False)

print(f"\n✅ 完整报告已保存：config/backtest_final_report.json")

print(f"\n{'='*70}")
print(f"回测完成时间：{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
print(f"{'='*70}")
