#!/usr/bin/env python3
"""
用修复后的 v2.1 系统重新测试 6 笔实际交易
验证 BEATUSDT 和 BULLAUSDT 是否能正确识别
验证 PRLUSDT 是否会被前置检查跳过
"""
import sys
sys.path.insert(0, '/root/.openclaw/workspace')

import requests
from datetime import datetime

from modules.scoring import DecisionEngine, PreFilter

# 6 笔实际交易
REAL_TRADES = [
    {
        'symbol': 'UNIUSDT',
        'side': 'long',
        'entry_price': 4.213,
        'pnl_u': -2.13,
        'pnl_pct': -42.64,
        'old_result': '亏损平仓'
    },
    {
        'symbol': 'KAITOUSDT',
        'side': 'long',
        'entry_price': 1.158997,
        'pnl_u': -4.51,
        'pnl_pct': -45.09,
        'old_result': '快速止损'
    },
    {
        'symbol': 'BEATUSDT',
        'side': 'short',
        'entry_price': 4.381,
        'pnl_u': 16.27,
        'pnl_pct': 54.62,
        'old_result': '盈利平仓 ✅'
    },
    {
        'symbol': 'BULLAUSDT',
        'side': 'long',
        'entry_price': 0.0141024,
        'pnl_u': 6.21,
        'pnl_pct': 6.22,
        'old_result': '盈利平仓 ✅'
    },
    {
        'symbol': 'PRLUSDT',
        'side': 'long',
        'entry_price': 0.2554,
        'pnl_u': -3.30,
        'pnl_pct': -22.00,
        'old_result': '亏损平仓'
    },
    {
        'symbol': 'ALLOUSDT',
        'side': 'short',
        'entry_price': 0.2589394,
        'pnl_u': -7.56,
        'pnl_pct': -18.89,
        'old_result': '亏损平仓'
    },
]

def get_klines(symbol, interval='1h', limit=100):
    """获取 K 线数据"""
    try:
        url = "https://fapi.binance.com/fapi/v1/klines"
        params = {'symbol': symbol, 'interval': interval, 'limit': limit}
        r = requests.get(url, params=params, timeout=10)
        return r.json()
    except Exception as e:
        print(f"  获取 {symbol} K 线失败：{e}")
        return None

def analyze_trade(trade):
    """用 v2.1 系统重新分析"""
    print(f"\n{'='*70}")
    print(f"{trade['symbol']} ({trade['side']}) @ {trade['entry_price']}")
    print(f"{'='*70}")
    print(f"老系统结果：{trade['old_result']} ({trade['pnl_u']}U, {trade['pnl_pct']}%)")
    
    # 获取 K 线
    klines = get_klines(trade['symbol'], interval='1h', limit=100)
    if not klines:
        return None
    
    closes = [float(k[4]) for k in klines]
    volumes = [float(k[5]) for k in klines]
    current_price = closes[-1]
    
    # 计算技术指标
    def calc_rsi(closes, period=14):
        if len(closes) < period + 1:
            return 50
        gains = []
        losses = []
        for i in range(1, len(closes)):
            change = closes[i] - closes[i-1]
            if change > 0:
                gains.append(change)
                losses.append(0)
            else:
                gains.append(0)
                losses.append(abs(change))
        if len(gains) < period:
            return 50
        avg_gain = sum(gains[-period:]) / period
        avg_loss = sum(losses[-period:]) / period
        if avg_loss == 0:
            return 100
        rs = avg_gain / avg_loss
        return round(100 - (100 / (1 + rs)), 2)
    
    def calc_macd_hist(closes):
        if len(closes) < 26:
            return 0
        recent = closes[-12:]
        prev = closes[-26:-12]
        recent_slope = (recent[-1] - recent[0]) / len(recent)
        prev_slope = (prev[-1] - prev[0]) / len(prev)
        return round((recent_slope - prev_slope) / prev[-1] * 1000 if prev[-1] else 0, 4)
    
    def calc_ema(closes, period):
        if len(closes) < period:
            return None
        multiplier = 2 / (period + 1)
        ema_val = sum(closes[:period]) / period
        for val in closes[period:]:
            ema_val = (val - ema_val) * multiplier + ema_val
        return ema_val
    
    rsi = calc_rsi(closes, 14)
    macd_hist = calc_macd_hist(closes)
    
    ema9 = calc_ema(closes, 9)
    ema21 = calc_ema(closes, 21)
    ema55 = calc_ema(closes, 55)
    
    if ema9 and ema21 and ema55:
        if ema9 > ema21 > ema55:
            ema_alignment = 85
        elif ema9 < ema21 < ema55:
            ema_alignment = 15
        else:
            ema_alignment = 50
    else:
        ema_alignment = 50
    
    volume_ratio = volumes[-1] / (sum(volumes[-10:-1]) / 9) if len(volumes) > 10 else 1.0
    
    print(f"\n【技术指标】")
    print(f"  RSI: {rsi}")
    print(f"  MACD: {macd_hist}")
    print(f"  EMA: {ema_alignment} ({'多排' if ema_alignment>70 else '空排' if ema_alignment<20 else '混乱'})")
    print(f"  成交量：{volume_ratio:.2f}x")
    
    # 1. 前置检查
    print(f"\n【前置检查】")
    should_skip, reason = PreFilter.check(
        volume_ratio=volume_ratio,
        spread_pct=0.1,  # 假设
        price_change_24h=5,  # 假设
        funding_rate=0.01  # 假设
    )
    if should_skip:
        print(f"  ❌ 跳过：{reason}")
        return {
            'symbol': trade['symbol'],
            'action': 'skip',
            'reason': reason,
            'old_pnl': trade['pnl_u'],
            'new_pnl': 0,
            'improvement': -trade['pnl_u']
        }
    else:
        print(f"  ✅ 通过")
    
    # 2. 决策引擎
    details = {
        'rsi': rsi,
        'macd_hist': macd_hist,
        'price': current_price,
        'ema_alignment': ema_alignment,
        'volume_ratio': volume_ratio,
        'market_cap': 'mid',  # 假设中市值
    }
    
    decision = DecisionEngine.make_decision(
        symbol=trade['symbol'],
        klines=klines,
        details=details,
        balance_usdt=10000,
        current_positions=[],
        max_positions=5
    )
    
    print(f"\n【v2.1 决策】")
    print(f"  策略：{decision['strategy']}")
    print(f"  评分：{decision['raw_score']} → 调整后 {decision['adjusted_score']}")
    print(f"  门槛：{decision['threshold']}")
    print(f"  决策：{decision['action']}")
    print(f"  仓位：{decision['position_usdt']}U")
    print(f"  止损：{decision['sl_price']}")
    print(f"  止盈：{decision['tp_price']}")
    
    # 预测结果
    if decision['action'] == 'skip':
        new_pnl = 0
        result = "不会开仓"
    else:
        # 假设同老系统结果
        new_pnl = trade['pnl_u']
        if trade['pnl_u'] > 0:
            result = "盈利平仓 ✅"
        else:
            result = "亏损平仓"
    
    print(f"\n【结果预测】")
    print(f"  {result} ({new_pnl}U)")
    
    return {
        'symbol': trade['symbol'],
        'action': decision['action'],
        'strategy': decision['strategy'],
        'score': decision['adjusted_score'],
        'old_pnl': trade['pnl_u'],
        'new_pnl': new_pnl,
        'improvement': -trade['pnl_u'] if decision['action'] == 'skip' else 0
    }

def main():
    print("="*70)
    print("v2.1 系统重新测试 - 6 笔实际交易")
    print("="*70)
    print(f"测试时间：{datetime.utcnow().isoformat()}")
    
    results = []
    for trade in REAL_TRADES:
        result = analyze_trade(trade)
        if result:
            results.append(result)
    
    # 汇总
    print("\n" + "="*70)
    print("汇总对比")
    print("="*70)
    
    print(f"\n{'标的':<15} {'方向':<6} {'老系统':<10} {'v2.1 评分':<12} {'v2.1 决策':<15} {'改善':<10}")
    print("-"*70)
    
    total_old_pnl = 0
    total_new_pnl = 0
    
    for i, r in enumerate(results):
        total_old_pnl += r['old_pnl']
        total_new_pnl += r['new_pnl']
        improvement_str = f"+{r['improvement']:.2f}U" if r['improvement'] > 0 else f"{r['improvement']:.2f}U"
        
        action_display = r['action']
        if r['action'] != 'skip':
            action_display = r.get('strategy', 'unknown')[:10]
        
        side = REAL_TRADES[i]['side']
        print(f"{r['symbol']:<15} {side:<6} {r['old_pnl']:+.2f}U{'':<4} {r['score']:<12} {action_display:<15} {improvement_str:<10}")
    
    print("-"*70)
    print(f"{'合计':<15} {'':<6} {total_old_pnl:+.2f}U{'':<4} {'':<12} {'':<15} {total_new_pnl - total_old_pnl:+.2f}U")
    
    # 统计
    skip_count = sum(1 for r in results if r['action'] == 'skip')
    open_count = len(results) - skip_count
    
    winning_trades_old = sum(1 for t in REAL_TRADES if t['pnl_u'] > 0)
    winning_trades_new = sum(1 for r in results if r['new_pnl'] > 0)
    
    print(f"\n【统计分析】")
    print(f"  老系统：")
    print(f"    - 开仓：{len(results)}/{len(results)} (100%)")
    print(f"    - 盈利：{winning_trades_old}/{len(results)} ({winning_trades_old/len(results)*100:.0f}%)")
    print(f"    - 总盈亏：{total_old_pnl:+.2f}U")
    print()
    print(f"  v2.1 系统：")
    print(f"    - 开仓：{open_count}/{len(results)} ({open_count/len(results)*100:.0f}%)")
    print(f"    - 跳过：{skip_count}/{len(results)} ({skip_count/len(results)*100:.0f}%)")
    print(f"    - 盈利：{winning_trades_new}/{len(results)} ({winning_trades_new/len(results)*100:.0f}%)")
    print(f"    - 总盈亏：{total_new_pnl:+.2f}U")
    print(f"    - 改善幅度：{total_new_pnl - total_old_pnl:+.2f}U")
    
    # 关键洞察
    print(f"\n【关键洞察】")
    
    # 检查 BEAT 和 BULLA 是否被正确识别
    beat_result = next((r for r in results if r['symbol'] == 'BEATUSDT'), None)
    bulla_result = next((r for r in results if r['symbol'] == 'BULLAUSDT'), None)
    
    if beat_result and beat_result['action'] != 'skip':
        print(f"  ✅ BEATUSDT: v2.1 会开仓（{beat_result['strategy']}）")
    elif beat_result:
        print(f"  ❌ BEATUSDT: v2.1 仍然跳过（{beat_result['reason']}）")
    
    if bulla_result and bulla_result['action'] != 'skip':
        print(f"  ✅ BULLAUSDT: v2.1 会开仓（{bulla_result['strategy']}）")
    elif bulla_result:
        print(f"  ❌ BULLAUSDT: v2.1 仍然跳过（{bulla_result['reason']}）")
    
    # 检查 PRL 是否被前置检查跳过
    prl_result = next((r for r in results if r['symbol'] == 'PRLUSDT'), None)
    if prl_result and prl_result['action'] == 'skip' and '成交量' in prl_result.get('reason', ''):
        print(f"  ✅ PRLUSDT: v2.1 前置检查跳过（{prl_result['reason']}）")
    elif prl_result:
        print(f"  ⚠️  PRLUSDT: v2.1 仍会开仓（需优化）")

if __name__ == '__main__':
    main()
