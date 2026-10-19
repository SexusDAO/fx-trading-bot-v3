#!/usr/bin/env python3
"""
v2.2 完全优化版 - 涨幅榜/跌幅榜筛选
7.25-8.2 真实数据回测

修正：
1. 从涨幅榜/跌幅榜选币（高波动山寨币）
2. 恢复止盈止损：SL=ATR×1.5, TP1=ATR×2, TP2=ATR×4
3. 追踪周期延长到 100 根 K 线（4 天）
"""

import json
import urllib.request
from datetime import datetime, timedelta

def fetch_tickers():
    """获取 24h 涨跌幅数据"""
    url = "https://fapi.binance.com/fapi/v1/ticker/24hr"
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            data = json.loads(response.read())
            # 过滤 USDT 永续合约，按成交量排序
            usdt_pairs = [t for t in data if t['symbol'].endswith('USDT') and float(t['quoteVolume']) > 50000000]
            return usdt_pairs
    except Exception as e:
        print(f"Fetch tickers error: {e}")
        return []

def fetch_klines(symbol, start_ts, end_ts):
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval=1h&startTime={start_ts}&endTime={end_ts}&limit=1000"
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            data = json.loads(response.read())
            return [{'timestamp': k[0], 'open': float(k[1]), 'high': float(k[2]), 'low': float(k[3]), 'close': float(k[4]), 'quote_volume': float(k[7])} for k in data]
    except:
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
    if len(klines) < 30:
        return None
    
    closed = klines[:-1]
    prices = [k['close'] for k in closed]
    vols = [k['quote_volume'] for k in closed]
    
    rsi = calculate_rsi(prices)
    macd = calculate_macd(prices)
    e9 = calculate_ema(prices, 9)
    e21 = calculate_ema(prices, 21)
    e55 = calculate_ema(prices, 55)
    vol_ratio = vols[-1] / sum(vols[-10:]) * 10 if len(vols) >= 10 and sum(vols[-10:]) > 0 else 1.0
    p_change = (prices[-1] - prices[-20]) / prices[-20] * 100 if len(prices) >= 20 else 0
    
    score = 50
    if 40 <= rsi <= 60:
        score += 10
    elif 30 <= rsi < 40 or 60 < rsi <= 70:
        score += 5
    
    if macd['histogram'] > 0:
        score += 10
    elif macd['histogram'] > -0.001:
        score += 5
    
    if e9 > e21 > e55:
        score += 15
    else:
        score += 5
    
    if vol_ratio > 2.0:
        score += 15
    elif vol_ratio > 1.5:
        score += 10
    elif vol_ratio > 1.0:
        score += 5
    
    if abs(p_change) > 10:
        score += 10
    elif abs(p_change) > 5:
        score += 5
    
    # 强势币加分
    if 70 < rsi < 80 and vol_ratio > 2.0:
        score += 5
    
    # 时间过滤
    hour = scan_time.hour
    if 13 <= hour <= 22:
        score += 3
    elif 0 <= hour <= 6:
        score -= 3
    
    # 相关性格离
    min_score = 70
    if open_positions_count >= 3:
        min_score = 80
    
    return (min(score, 100), min_score)

def get_position_size(score):
    if score >= 90:
        return 660
    elif score >= 80:
        return 440
    else:
        return 220

def simulate_trade(entry, atr, position_size, future_klines):
    """
    恢复原始止盈止损距离（适合高波动山寨币）
    SL: ATR×1.5
    TP1: ATR×2.0 (50% 仓位)
    TP2: ATR×4.0 (50% 仓位)
    """
    sl = entry - atr * 1.5
    tp1 = entry + atr * 2.0
    tp2 = entry + atr * 4.0
    
    position_remaining = 1.0
    total_pnl = 0
    reason = 'holding'
    exit_time = None
    exit_price = None
    best_price = entry
    
    for k in future_klines:
        if k['high'] > best_price:
            best_price = k['high']
        
        # 检查止损（优先）
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
        
        # 追踪止损
        if position_remaining > 0 and best_price > entry + atr * 0.5:
            trailing_sl = best_price - atr * 0.5
            if trailing_sl > sl:
                sl = trailing_sl
    
    # 未平仓不计入
    if position_remaining > 0:
        return None, 'holding', None, None, sl, tp1, tp2
    
    return total_pnl, reason, exit_time, exit_price, sl, tp1, tp2

def main():
    print("="*110)
    print("v2.2 完全优化版 - 涨幅榜/跌幅榜筛选（高波动山寨币）")
    print("时间范围：2026-07-25 00:00 到 2026-08-02 23:59")
    print("="*110)
    
    # 获取涨幅榜/跌幅榜
    tickers = fetch_tickers()
    if not tickers:
        print("❌ 无法获取涨跌幅数据，使用备用列表")
        symbols = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT', 'XRPUSDT', 
                   'ADAUSDT', 'DOGEUSDT', 'LINKUSDT', 'AVAXUSDT', 'MATICUSDT']
    else:
        # 按涨跌幅排序，选前 10 个涨 + 前 10 个跌
        sorted_by_change = sorted(tickers, key=lambda x: abs(float(x['priceChangePercent'])), reverse=True)
        symbols = [t['symbol'] for t in sorted_by_change[:20]]
        print(f"\n📊 选币逻辑：按 24h 涨跌幅绝对值排序（高波动）")
        print(f"   选中：{', '.join(symbols[:10])}...")
    
    start_ts = int(datetime(2026, 7, 25, 0, 0, 0).timestamp() * 1000)
    end_ts = int(datetime(2026, 8, 2, 23, 59, 59).timestamp() * 1000)
    
    print(f"\n📅 时间范围：{datetime.utcfromtimestamp(start_ts/1000)} 到 {datetime.utcfromtimestamp(end_ts/1000)}")
    print(f"📊 标的：{len(symbols)}个（高波动山寨币为主）")
    print(f"📍 扫描间隔：6 小时")
    print(f"📏 追踪周期：100 根 K 线（约 4 天）")
    print(f"📏 止盈止损：SL=ATR×1.5, TP1=ATR×2, TP2=ATR×4")
    
    klines_dict = {}
    for sym in symbols:
        klines = fetch_klines(sym, start_ts, end_ts)
        if klines and len(klines) >= 200:
            klines_dict[sym] = klines
            print(f"  ✅ {sym}: {len(klines)} 根 K 线")
        else:
            print(f"  ⚠️ {sym}: 数据不足")
    
    all_trades = []
    open_positions = []
    
    print(f"\n{'='*110}")
    print("开始模拟...")
    print(f"{'='*110}")
    
    for sym in symbols:
        klines = klines_dict.get(sym)
        if not klines:
            continue
        
        # 每 6 小时扫描，追踪 100 根 K 线
        for i in range(30, len(klines)-100, 6):
            snapshot = klines[:i+1]
            future = klines[i+1:i+101]  # 100 根 K 线
            
            current_price = snapshot[-1]['close']
            scan_time = datetime.utcfromtimestamp(snapshot[-1]['timestamp']/1000)
            atr = calculate_atr(snapshot)
            
            score_result = score_v22_optimized(snapshot, scan_time, len(open_positions))
            if not score_result:
                continue
            
            score, min_score = score_result
            
            if score >= min_score:
                position_size = get_position_size(score)
                entry = current_price * 0.99
                
                pnl, reason, exit_time, exit_price, sl, tp1, tp2 = simulate_trade(
                    entry, atr, position_size, future
                )
                
                if pnl is not None:
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
                
                # 更新持仓
                open_positions.append({'time': scan_time, 'symbol': sym})
                open_positions = [p for p in open_positions 
                                  if (datetime.utcfromtimestamp(klines[min(i+20, len(klines)-1)]['timestamp']/1000) - p['time']).total_seconds() < 72000]
    
    # 汇总
    print(f"\n{'='*110}")
    print("📊 模拟结果汇总")
    print(f"{'='*110}")
    
    total_pnl = sum(t['pnl'] for t in all_trades)
    winning = [t for t in all_trades if t['pnl'] > 0]
    losing = [t for t in all_trades if t['pnl'] <= 0]
    tp_count = sum(1 for t in all_trades if t['reason']=='tp2')
    sl_count = sum(1 for t in all_trades if t['reason']=='sl')
    
    total_position = sum(t['position_size'] for t in all_trades)
    total_fee = total_position * 0.0008
    net_pnl = total_pnl - total_fee
    
    closed_count = len(all_trades)
    
    print(f"""
┌─────────────────────────────────────────────────────────────────────────┐
│  指标                  │  数值                                          │
├─────────────────────────────────────────────────────────────────────────┤
│  已平仓                │  {closed_count:>3} 笔                                               │
│  总仓位金额            │  ${total_position:>8.0f}U                                           │
│  总手续费              │  ${total_fee:>8.2f}U                                           │
├─────────────────────────────────────────────────────────────────────────┤
│  毛盈亏                │  ${total_pnl:>8.2f}U                                           │
│  净盈亏                │  ${net_pnl:>8.2f}U                                           │
│  平均盈亏/笔           │  ${net_pnl/max(1,closed_count):>+6.2f}U                                            │
├─────────────────────────────────────────────────────────────────────────┤
│  止盈 (TP2)            │  {tp_count:>3} 笔 ({tp_count/max(1,closed_count)*100:.1f}%)                                     │
│  止损 (SL)             │  {sl_count:>3} 笔 ({sl_count/max(1,closed_count)*100:.1f}%)                                     │
│  胜率                  │  {len(winning)/max(1,closed_count)*100:>5.1f}%                                             │
└─────────────────────────────────────────────────────────────────────────┘
""")
    
    # 各标的
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
    
    print(f"\n{'标的':<15} {'笔数':>6} {'盈利':>6} {'胜率':>8} {'总盈亏':>12} {'平均/笔':>10}")
    print("-"*65)
    for sym, data in sorted(by_symbol.items(), key=lambda x: x[1]['pnl'], reverse=True)[:15]:
        win_rate = data['win']/data['count']*100 if data['count']>0 else 0
        avg = data['pnl']/data['count'] if data['count']>0 else 0
        print(f"{sym:<15} {data['count']:>6} {data['win']:>6} {win_rate:>7.1f}% ${data['pnl']:>+10.2f}U ${avg:>+8.2f}U")
    
    # 大赢大亏
    print(f"\n{'='*110}")
    print("🎯 大赢大亏分析")
    print(f"{'='*110}")
    
    big_wins = [t for t in all_trades if t['pnl'] > 20]
    big_losses = [t for t in all_trades if t['pnl'] < -10]
    
    print(f"\n大赢交易 (>$20): {len(big_wins)} 笔")
    if big_wins:
        for t in sorted(big_wins, key=lambda x: x['pnl'], reverse=True)[:10]:
            print(f"  {t['symbol']:<12} {t['scan_time'].strftime('%m-%d %H:%M')} 评分{t['score']} 仓位${t['position_size']}U ${t['pnl']:+.2f}U")
    
    print(f"\n大亏交易 (<$-10): {len(big_losses)} 笔")
    if big_losses:
        for t in sorted(big_losses, key=lambda x: x['pnl'])[:10]:
            print(f"  {t['symbol']:<12} {t['scan_time'].strftime('%m-%d %H:%M')} 评分{t['score']} 仓位${t['position_size']}U ${t['pnl']:+.2f}U")
    
    # 对比
    print(f"\n{'='*110}")
    print("📈 对比：主流币 vs 高波动山寨币")
    print(f"{'='*110}")
    print("""
┌──────────────────┬──────────────┬──────────────┬──────────────┐
│  指标            │  主流币 9 个  │  高波动 20 个  │  差异        │
├──────────────────┼──────────────┼──────────────┼──────────────┤
│  交易笔数        │  116 笔       │  TBD         │  ?           │
│  净盈亏          │  +$243U      │  TBD         │  ?           │
│  平均/笔         │  +$2.10U     │  TBD         │  ?           │
│  止盈率          │  59.5%       │  TBD         │  ?           │
│  胜率            │  94.0%       │  TBD         │  ?           │
└──────────────────┴──────────────┴──────────────┴──────────────┘

预期：高波动山寨币应该带来更高收益（但风险也更大）
""")
    
    print(f"\n{'='*110}")
    print(f"模拟完成：{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*110}")

if __name__ == '__main__':
    main()
