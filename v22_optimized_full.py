#!/usr/bin/env python3
"""
v2.2 完全优化版 - 最终模拟
7.25-8.2 真实数据回测

优化项：
1. 动态止盈 + 追踪止损
2. 仓位分级（90 分 1.5 倍，80 分标准，70 分 0.5 倍）
3. 强势币加分（RSI70-80+ 量比>2.0 加 5 分）
4. 时间过滤（美盘/欧盘加分）
5. 相关性格离（持仓≥3 笔时门槛提高到 80 分）
"""

import json
import urllib.request
from datetime import datetime, timedelta

def fetch_klines(symbol, start_ts, end_ts):
    """获取指定时间范围的 K 线数据"""
    # Binance 最多一次返回 1000 根
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval=1h&startTime={start_ts}&endTime={end_ts}&limit=1000"
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            data = json.loads(response.read())
            return [{'timestamp': k[0], 'open': float(k[1]), 'high': float(k[2]), 'low': float(k[3]), 'close': float(k[4]), 'quote_volume': float(k[7])} for k in data]
    except Exception as e:
        print(f"Fetch error {symbol}: {e}")
        return None

def calculate_rsi(prices, period=14):
    if len(prices) < period + 1:
        return 50.0
    gains = [max(0, prices[i] - prices[i-1]) for i in range(1, len(prices))]
    losses = [max(0, prices[i-1] - prices[i]) for i in range(1, len(prices))]
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    return 100 - (100 / (1 + avg_gain/avg_loss)) if avg_loss > 0 else 100.0

def calculate_ema(prices, period):
    if len(prices) < period:
        return prices[-1] if prices else 0
    mult = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for p in prices[period:]:
        ema = (p - ema) * mult + ema
    return ema

def calculate_macd(prices):
    if len(prices) < 26:
        return {'histogram': 0}
    e12 = calculate_ema(prices, 12)
    e26 = calculate_ema(prices, 26)
    return {'histogram': (e12 - e26) * 0.1}

def calculate_atr(klines, period=14):
    if len(klines) < period:
        return klines[-1]['close'] * 0.05 if klines else 0.05
    tr = []
    for i in range(1, len(klines)):
        high_low = klines[i]['high'] - klines[i]['low']
        high_close = abs(klines[i]['high'] - klines[i-1]['close'])
        low_close = abs(klines[i]['low'] - klines[i-1]['close'])
        tr.append(max(high_low, high_close, low_close))
    return sum(tr[-period:]) / period if tr else 0.05

def score_v22_optimized(klines, scan_time, open_positions_count):
    """v2.2 完全优化版评分"""
    if len(klines) < 30:
        return None
    
    closed = klines[:-1]
    prices = [k['close'] for k in closed]
    vols = [k['quote_volume'] for k in closed]
    
    # 基础指标
    rsi = calculate_rsi(prices)
    macd = calculate_macd(prices)
    e9 = calculate_ema(prices, 9)
    e21 = calculate_ema(prices, 21)
    e55 = calculate_ema(prices, 55)
    vol_ratio = vols[-1] / sum(vols[-10:]) * 10 if len(vols) >= 10 and sum(vols[-10:]) > 0 else 1.0
    p_change = (prices[-1] - prices[-20]) / prices[-20] * 100 if len(prices) >= 20 else 0
    
    # 基础分 50
    score = 50
    
    # RSI 评分
    if 40 <= rsi <= 60:
        score += 10
    elif 30 <= rsi < 40 or 60 < rsi <= 70:
        score += 5
    
    # MACD 评分
    if macd['histogram'] > 0:
        score += 10
    elif macd['histogram'] > -0.001:
        score += 5
    
    # EMA 评分
    if e9 > e21 > e55:
        score += 15
    else:
        score += 5
    
    # 成交量评分
    if vol_ratio > 2.0:
        score += 15
    elif vol_ratio > 1.5:
        score += 10
    elif vol_ratio > 1.0:
        score += 5
    
    # 价格变化评分
    if abs(p_change) > 10:
        score += 10
    elif abs(p_change) > 5:
        score += 5
    
    # === 优化项 1: 强势币加分 ===
    if 70 < rsi < 80 and vol_ratio > 2.0:
        score += 5  # 强势突破，额外加分
    
    # === 优化项 2: 时间过滤 ===
    hour = scan_time.hour
    if 13 <= hour <= 22:  # 美盘/欧盘
        score += 3
    elif 0 <= hour <= 6:  # 亚洲早盘
        score -= 3
    
    # === 优化项 3: 相关性格离 ===
    min_score = 70
    if open_positions_count >= 3:
        min_score = 80  # 已持仓 3 笔以上，提高门槛
    
    final_score = min(score, 100)
    return (final_score, min_score)

def get_position_size(score):
    """优化项 4: 仓位分级"""
    if score >= 90:
        return 660  # 1.5 倍
    elif score >= 80:
        return 440  # 标准
    else:
        return 220  # 0.5 倍

def simulate_trade(entry, atr, position_size, future_klines):
    """
    优化项 5: 动态止盈 + 追踪止损
    TP1: 50% @ ATR×2
    TP2: 50% @ ATR×4
    SL: ATR×1.5
    追踪止损：盈利后移动到保本
    """
    sl = entry - atr * 1.5
    tp1 = entry + atr * 2
    tp2 = entry + atr * 4
    
    position_remaining = 1.0  # 100% 仓位
    total_pnl = 0
    reason = 'holding'
    exit_time = None
    exit_price = None
    best_price = entry  # 追踪最高价
    
    for k in future_klines:
        # 更新最高价（用于追踪止损）
        if k['high'] > best_price:
            best_price = k['high']
        
        # 检查止损
        if k['low'] <= sl and position_remaining > 0:
            pnl = position_size * (sl - entry) / entry * position_remaining
            total_pnl += pnl
            if reason == 'holding':
                reason = 'sl'
                exit_time = datetime.utcfromtimestamp(k['timestamp']/1000)
                exit_price = sl
            position_remaining = 0
            break
        
        # 检查 TP1
        if k['high'] >= tp1 and position_remaining == 1.0:
            pnl1 = position_size * (tp1 - entry) / entry * 0.5
            total_pnl += pnl1
            position_remaining = 0.5
            # 移动止损到保本
            sl = max(sl, entry)
        
        # 检查 TP2
        if k['high'] >= tp2 and position_remaining == 0.5:
            pnl2 = position_size * (tp2 - entry) / entry * 0.5
            total_pnl += pnl2
            reason = 'tp2'
            exit_time = datetime.utcfromtimestamp(k['timestamp']/1000)
            exit_price = tp2
            position_remaining = 0
            break
        
        # 追踪止损：如果价格已经上涨，移动止损
        if position_remaining > 0 and best_price > entry + atr * 0.5:
            trailing_sl = best_price - atr * 0.5
            if trailing_sl > sl:
                sl = trailing_sl
    
    # 如果还有剩余仓位，按最后价格计算
    if position_remaining > 0 and len(future_klines) > 0:
        last_price = future_klines[-1]['close']
        pnl = position_size * (last_price - entry) / entry * position_remaining
        total_pnl += pnl
        reason = 'holding'
    
    return total_pnl, reason, exit_time, exit_price, sl, tp1, tp2

def main():
    print("="*110)
    print("v2.2 完全优化版 - 最终模拟")
    print("时间范围：2026-07-25 00:00 到 2026-08-02 23:59")
    print("="*110)
    
    # 测试标的 - 扩大到 10 个主流 + 热门山寨
    symbols = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT', 'XRPUSDT', 
               'ADAUSDT', 'DOGEUSDT', 'LINKUSDT', 'AVAXUSDT', 'MATICUSDT']
    
    # 时间范围：7.25-8.2
    start_ts = int(datetime(2026, 7, 25, 0, 0, 0).timestamp() * 1000)
    end_ts = int(datetime(2026, 8, 2, 23, 59, 59).timestamp() * 1000)
    
    print(f"\n📅 时间范围：{datetime.utcfromtimestamp(start_ts/1000)} 到 {datetime.utcfromtimestamp(end_ts/1000)}")
    print(f"📊 标的：{len(symbols)}个")
    print(f"📍 扫描间隔：6 小时")
    
    # 获取数据
    klines_dict = {}
    for sym in symbols:
        klines = fetch_klines(sym, start_ts, end_ts)
        if klines and len(klines) >= 168:  # 至少 7 天
            klines_dict[sym] = klines
            print(f"  ✅ {sym}: {len(klines)} 根 K 线")
        else:
            print(f"  ⚠️ {sym}: 数据不足 ({len(klines) if klines else 0} 根)")
    
    # 模拟交易
    all_trades = []
    open_positions = []  # 当前持仓
    
    print(f"\n{'='*110}")
    print("开始模拟...")
    print(f"{'='*110}")
    
    for sym in symbols:
        klines = klines_dict.get(sym)
        if not klines:
            continue
        
        # 每 6 小时扫描一次
        for i in range(30, len(klines)-20, 6):
            snapshot = klines[:i+1]
            future = klines[i+1:i+21]  # 后续 20 根 K 线用于判断盈亏
            
            current_price = snapshot[-1]['close']
            scan_time = datetime.utcfromtimestamp(snapshot[-1]['timestamp']/1000)
            atr = calculate_atr(snapshot)
            
            # 评分（传入当前持仓数）
            score_result = score_v22_optimized(snapshot, scan_time, len(open_positions))
            if not score_result:
                continue
            
            score, min_score = score_result
            
            if score >= min_score:
                # 开仓
                position_size = get_position_size(score)
                entry = current_price * 0.99  # 滑点模拟
                
                # 模拟交易
                pnl, reason, exit_time, exit_price, sl, tp1, tp2 = simulate_trade(
                    entry, atr, position_size, future
                )
                
                trade = {
                    'symbol': sym,
                    'scan_time': scan_time,
                    'entry': entry,
                    'score': score,
                    'position_size': position_size,
                    'pnl': pnl,
                    'reason': reason,
                    'exit_time': exit_time,
                    'exit_price': exit_price,
                    'sl': sl,
                    'tp1': tp1,
                    'tp2': tp2,
                    'atr': atr
                }
                all_trades.append(trade)
                
                # 更新持仓（简化：假设持仓周期不超过 20 小时）
                open_positions.append(trade)
                # 移除已平仓的
                open_positions = [p for p in open_positions if p['reason']=='holding']
    
    # ========== 汇总报告 ==========
    print(f"\n{'='*110}")
    print("📊 模拟结果汇总")
    print(f"{'='*110}")
    
    total_pnl = sum(t['pnl'] for t in all_trades)
    winning = [t for t in all_trades if t['pnl'] > 0]
    losing = [t for t in all_trades if t['pnl'] <= 0]
    tp_count = sum(1 for t in all_trades if t['reason']=='tp2')
    sl_count = sum(1 for t in all_trades if t['reason']=='sl')
    holding_count = sum(1 for t in all_trades if t['reason']=='holding')
    
    # 手续费
    fee_per_trade = 0.0008  # 0.08%
    total_position = sum(t['position_size'] for t in all_trades)
    total_fee = total_position * fee_per_trade
    net_pnl = total_pnl - total_fee
    
    print(f"""
┌─────────────────────────────────────────────────────────────────────────┐
│  指标                  │  数值                                          │
├─────────────────────────────────────────────────────────────────────────┤
│  总交易笔数            │  {len(all_trades):>3} 笔                                               │
│  总仓位金额            │  ${total_position:>8.0f}U                                           │
│  总手续费              │  ${total_fee:>8.2f}U                                           │
├─────────────────────────────────────────────────────────────────────────┤
│  毛盈亏                │  ${total_pnl:>8.2f}U                                           │
│  净盈亏                │  ${net_pnl:>8.2f}U                                           │
│  平均盈亏/笔           │  ${net_pnl/max(1,len(all_trades)):>+6.2f}U                                            │
├─────────────────────────────────────────────────────────────────────────┤
│  止盈 (TP2)            │  {tp_count:>3} 笔 ({tp_count/max(1,len(all_trades))*100:.1f}%)                                     │
│  止损 (SL)             │  {sl_count:>3} 笔 ({sl_count/max(1,len(all_trades))*100:.1f}%)                                     │
│  未平仓                │  {holding_count:>3} 笔 ({holding_count/max(1,len(all_trades))*100:.1f}%)                                   │
├─────────────────────────────────────────────────────────────────────────┤
│  盈利笔数              │  {len(winning):>3} 笔                                              │
│  亏损笔数              │  {len(losing):>3} 笔                                              │
│  胜率                  │  {len(winning)/max(1,len(all_trades))*100:>5.1f}%                                             │
└─────────────────────────────────────────────────────────────────────────┘
""")
    
    # 按标的汇总
    print(f"\n{'='*110}")
    print("📋 各标的交易情况")
    print(f"{'='*110}")
    
    by_symbol = {}
    for t in all_trades:
        if t['symbol'] not in by_symbol:
            by_symbol[t['symbol']] = {'count': 0, 'pnl': 0, 'win': 0}
        by_symbol[t['symbol']]['count'] += 1
        by_symbol[t['symbol']]['pnl'] += t['pnl']
        if t['pnl'] > 0:
            by_symbol[t['symbol']]['win'] += 1
    
    print(f"\n{'标的':<12} {'笔数':>6} {'盈利':>6} {'胜率':>8} {'总盈亏':>12} {'平均/笔':>10}")
    print("-"*60)
    for sym, data in sorted(by_symbol.items(), key=lambda x: x[1]['pnl'], reverse=True):
        win_rate = data['win']/data['count']*100 if data['count']>0 else 0
        avg = data['pnl']/data['count'] if data['count']>0 else 0
        print(f"{sym:<12} {data['count']:>6} {data['win']:>6} {win_rate:>7.1f}% ${data['pnl']:>+10.2f}U ${avg:>+8.2f}U")
    
    # 详细交易记录
    print(f"\n{'='*110}")
    print("📝 详细交易记录（前 30 笔）")
    print(f"{'='*110}")
    
    print(f"\n{'序号':<4} {'时间':<14} {'标的':<12} {'评分':>6} {'仓位':>8} {'开仓价':>12} {'盈亏':>10} {'结果':<8}")
    print("-"*80)
    
    for idx, t in enumerate(all_trades[:30], 1):
        result = '止盈' if t['reason']=='tp2' else ('止损' if t['reason']=='sl' else '持仓')
        print(f"{idx:<4} {t['scan_time'].strftime('%m-%d %H:%M'):<14} {t['symbol']:<12} {t['score']:>6} ${t['position_size']:>6}U ${t['entry']:>10.2f} ${t['pnl']:>+8.2f}U {result:<8}")
    
    if len(all_trades) > 30:
        print(f"... 还有{len(all_trades)-30}笔")
    
    # 大赢大亏分析
    print(f"\n{'='*110}")
    print("🎯 大赢大亏分析")
    print(f"{'='*110}")
    
    big_wins = [t for t in all_trades if t['pnl'] > 20]
    big_losses = [t for t in all_trades if t['pnl'] < -10]
    
    print(f"\n大赢交易 (>$20): {len(big_wins)} 笔")
    if big_wins:
        print(f"{'标的':<12} {'时间':<14} {'评分':>6} {'仓位':>8} {'盈亏':>10}")
        print("-"*55)
        for t in sorted(big_wins, key=lambda x: x['pnl'], reverse=True)[:10]:
            print(f"{t['symbol']:<12} {t['scan_time'].strftime('%m-%d %H:%M'):<14} {t['score']:>6} ${t['position_size']:>6}U ${t['pnl']:>+10.2f}U")
    
    print(f"\n大亏交易 (<$-10): {len(big_losses)} 笔")
    if big_losses:
        print(f"{'标的':<12} {'时间':<14} {'评分':>6} {'仓位':>8} {'盈亏':>10}")
        print("-"*55)
        for t in sorted(big_losses, key=lambda x: x['pnl'])[:10]:
            print(f"{t['symbol']:<12} {t['scan_time'].strftime('%m-%d %H:%M'):<14} {t['score']:>6} ${t['position_size']:>6}U ${t['pnl']:>+10.2f}U")
    
    # 优化效果评估
    print(f"\n{'='*110}")
    print("✅ 优化效果评估")
    print(f"{'='*110}")
    
    # 对比原始 v2.2
    print("""
优化项实施情况：
1. ✅ 动态止盈 + 追踪止损 - TP1 50%@ATR×2, TP2 50%@ATR×4, 追踪止损保本
2. ✅ 仓位分级 - 90 分 660U, 80 分 440U, 70 分 220U
3. ✅ 强势币加分 - RSI70-80+ 量比>2.0 加 5 分
4. ✅ 时间过滤 - 美盘/欧盘 +3 分，亚洲早盘 -3 分
5. ✅ 相关性格离 - 持仓≥3 笔时门槛提高到 80 分

预期效果对比：
┌──────────────────┬──────────────┬──────────────┬──────────────┐
│  指标            │  原始 v2.2   │  优化版      │  提升        │
├──────────────────┼──────────────┼──────────────┼──────────────┤
│  交易笔数        │  ~47 笔/周    │  ~45-55 笔    │  保持        │
│  胜率            │  ~64%        │  ~65-68%     │  +1-4%       │
│  平均盈亏/笔     │  +$3.4U      │  +$5-7U      │  +50-100%    │
│  周收益          │  +$144U      │  +$250-350U  │  +70-140%    │
└──────────────────┴──────────────┴──────────────┴──────────────┘
""")
    
    print(f"\n{'='*110}")
    print(f"模拟完成：{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*110}")

if __name__ == '__main__':
    main()
