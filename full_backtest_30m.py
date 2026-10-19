#!/usr/bin/env python3
"""
v2.2 完全优化版 - 完整严谨回测
- 涨幅榜/跌幅榜前 10
- 30 分钟 K 线，每 30 分钟扫描
- 7 天完整数据
- 信号不达标不开仓
"""

import json
import urllib.request
from datetime import datetime, timedelta

def fetch_tickers():
    """获取 24h 涨跌幅数据"""
    url = "https://fapi.binance.com/fapi/v1/ticker/24hr"
    try:
        with urllib.request.urlopen(url, timeout=15) as response:
            data = json.loads(response.read())
            # 过滤 USDT 永续合约，成交量>1 亿
            usdt_pairs = [t for t in data if t['symbol'].endswith('USDT') and float(t['quoteVolume']) > 100000000]
            return usdt_pairs
    except Exception as e:
        print(f"Fetch tickers error: {e}")
        return []

def fetch_30m_klines(symbol, start_ts, end_ts):
    """获取 30 分钟 K 线"""
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval=30m&startTime={start_ts}&endTime={end_ts}&limit=1000"
    try:
        with urllib.request.urlopen(url, timeout=15) as response:
            data = json.loads(response.read())
            return [{'timestamp': k[0], 'open': float(k[1]), 'high': float(k[2]), 'low': float(k[3]), 'close': float(k[4]), 'volume': float(k[5]), 'quote_volume': float(k[7])} for k in data]
    except Exception as e:
        print(f"Fetch {symbol} error: {e}")
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

def score_v22(klines, scan_time, open_positions_count):
    """v2.2 评分（固定止盈版本）"""
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
    
    # 强势币加分（不提高门槛）
    if 70 < rsi < 80 and vol_ratio > 2.0:
        score += 5
    
    # 时间过滤（美盘/欧盘加分）
    hour = scan_time.hour
    if 13 <= hour <= 22:
        score += 3
    elif 0 <= hour <= 6:
        score -= 3
    
    # 相关性格离（持仓≥3 笔时提高门槛）
    min_score = 70
    if open_positions_count >= 3:
        min_score = 80
    
    final_score = min(score, 100)
    return (final_score, min_score)

def get_position_size(score, entry_atr, entry_price, max_loss=50):
    """
    仓位分级 + 最大亏损限制
    - 95 分：660U
    - 85-94 分：440U
    - 70-84 分：220U
    - 单笔最大亏损不超过 50U
    """
    if score >= 95:
        base_size = 660
    elif score >= 85:
        base_size = 440
    else:
        base_size = 220
    
    # 最大亏损限制
    sl_distance = entry_atr * 1.5
    max_position = max_loss / (sl_distance / entry_price) if sl_distance > 0 else base_size
    
    return min(base_size, max_position)

def simulate_trade(entry, entry_atr, position_size, future_klines, max_hours=96):
    """
    固定止盈止损模拟
    SL: ATR × 1.5
    TP1: ATR × 2.0 (50%)
    TP2: ATR × 4.0 (50%)
    追踪止损：盈利>2ATR 后移到保本
    """
    sl = entry - entry_atr * 1.5
    tp1 = entry + entry_atr * 2.0
    tp2 = entry + entry_atr * 4.0
    
    position_remaining = 1.0
    total_pnl = 0
    reason = 'holding'
    exit_time = None
    exit_price = None
    best_price = entry
    entry_time = datetime.utcfromtimestamp(future_klines[0]['timestamp']/1000) if future_klines else datetime.utcnow()
    
    for i, k in enumerate(future_klines):
        k_time = datetime.utcfromtimestamp(k['timestamp']/1000)
        holding_hours = (k_time - entry_time).total_seconds() / 3600
        
        # 超过最大持仓时间，强制平仓
        if holding_hours > max_hours:
            last_price = k['close']
            pnl = position_size * (last_price - entry) / entry * position_remaining
            total_pnl += pnl
            reason = 'timeout'
            exit_time = k_time
            exit_price = last_price
            position_remaining = 0
            break
        
        # 更新最高价
        if k['high'] > best_price:
            best_price = k['high']
        
        # 检查止损（优先）
        if k['low'] <= sl and position_remaining > 0:
            pnl = position_size * (sl - entry) / entry * position_remaining
            total_pnl += pnl
            if reason == 'holding':
                reason = 'sl'
                exit_time = k_time
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
            exit_time = k_time
            exit_price = tp2
            position_remaining = 0
            break
        
        # 追踪止损：盈利>2ATR 后移到保本
        if position_remaining > 0 and best_price > entry + entry_atr * 2:
            sl = max(sl, entry)
    
    # 未平仓不计入
    if position_remaining > 0:
        return None, 'holding', None, None
    
    return total_pnl, reason, exit_time, exit_price

def main():
    print("="*110)
    print("v2.2 完全优化版 - 完整严谨回测")
    print("涨幅榜/跌幅榜前 10 | 30 分钟 K 线 | 7 天数据 | 半小时扫描")
    print("="*110)
    
    # 获取涨幅榜/跌幅榜
    tickers = fetch_tickers()
    if not tickers:
        print("❌ 无法获取涨跌幅数据")
        return
    
    # 按涨跌幅绝对值排序，选前 10
    sorted_by_change = sorted(tickers, key=lambda x: abs(float(x['priceChangePercent'])), reverse=True)
    symbols = [t['symbol'] for t in sorted_by_change[:10]]
    
    print(f"\n📊 选币逻辑：24h 涨跌幅绝对值前 10（高波动）")
    for i, sym in enumerate(symbols, 1):
        ticker = next((t for t in tickers if t['symbol']==sym), None)
        if ticker:
            pct = float(ticker['priceChangePercent'])
            vol = f"{float(ticker['quoteVolume'])/1000000:.0f}M"
            print(f"   {i}. {sym:<15} 24h:{pct:>+7.2f}% 成交量:{vol}")
    
    # 时间范围：7 天
    end_ts = int(datetime.utcnow().timestamp() * 1000)
    start_ts = int((datetime.utcnow() - timedelta(days=7)).timestamp() * 1000)
    
    print(f"\n📅 时间范围：{datetime.utcfromtimestamp(start_ts/1000)} 到 {datetime.utcfromtimestamp(end_ts/1000)}")
    print(f"📊 标的：{len(symbols)}个")
    print(f"📍 扫描间隔：30 分钟")
    print(f"📏 追踪周期：192 根 K 线（96 小时=4 天）")
    
    print(f"\n🎯 交易规则：")
    print(f"   开仓门槛：70 分（持仓≥3 笔时提高到 80 分）")
    print(f"   仓位分级：95 分 660U, 85-94 分 440U, 70-84 分 220U")
    print(f"   单笔最大亏损：50U")
    print(f"   止盈止损：SL=ATR×1.5, TP1=ATR×2(50%), TP2=ATR×4(50%)")
    print(f"   追踪止损：盈利>2ATR 后移到保本")
    print(f"   最大持仓时间：96 小时（超时强制平仓）")
    
    # 获取 K 线数据
    klines_dict = {}
    for sym in symbols:
        print(f"\n  获取 {sym} 数据...", end=' ')
        klines = fetch_30m_klines(sym, start_ts, end_ts)
        if klines and len(klines) >= 300:  # 7 天=336 根 30m K 线
            klines_dict[sym] = klines
            print(f"✅ {len(klines)} 根")
        else:
            print(f"⚠️ 数据不足 ({len(klines) if klines else 0} 根)")
    
    if not klines_dict:
        print("\n❌ 没有足够数据的标的")
        return
    
    # 模拟交易
    all_trades = []
    open_positions = []  # 当前持仓
    total_scans = 0
    signal_scans = 0
    
    print(f"\n{'='*110}")
    print("开始模拟交易...")
    print(f"{'='*110}")
    
    for sym in klines_dict:
        klines = klines_dict[sym]
        
        # 每 30 分钟扫描一次
        for i in range(30, len(klines)-192, 1):  # 每根 K 线都扫描（30 分钟间隔）
            total_scans += 1
            snapshot = klines[:i+1]
            future = klines[i+1:i+193]  # 192 根 K 线（96 小时）
            
            current_price = snapshot[-1]['close']
            scan_time = datetime.utcfromtimestamp(snapshot[-1]['timestamp']/1000)
            entry_atr = calculate_atr(snapshot)
            
            # 评分
            score_result = score_v22(snapshot, scan_time, len(open_positions))
            if not score_result:
                continue
            
            score, min_score = score_result
            
            # 信号不达标不开仓
            if score >= min_score:
                signal_scans += 1
                position_size = get_position_size(score, entry_atr, current_price)
                entry = current_price * 0.99  # 滑点模拟
                
                # 模拟交易
                pnl, reason, exit_time, exit_price = simulate_trade(
                    entry, entry_atr, position_size, future
                )
                
                # 只统计已平仓
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
                        'entry_atr': entry_atr
                    }
                    all_trades.append(trade)
                
                # 更新持仓（简化：假设 20 小时后平仓）
                open_positions.append({'time': scan_time, 'symbol': sym})
                open_positions = [p for p in open_positions 
                                  if (datetime.utcfromtimestamp(klines[min(i+40, len(klines)-1)]['timestamp']/1000) - p['time']).total_seconds() < 72000]
    
    # ========== 汇总报告 ==========
    print(f"\n{'='*110}")
    print("📊 回测结果汇总")
    print(f"{'='*110}")
    
    total_pnl = sum(t['pnl'] for t in all_trades)
    winning = [t for t in all_trades if t['pnl'] > 0]
    losing = [t for t in all_trades if t['pnl'] <= 0]
    tp_count = sum(1 for t in all_trades if t['reason']=='tp2')
    sl_count = sum(1 for t in all_trades if t['reason']=='sl')
    timeout_count = sum(1 for t in all_trades if t['reason']=='timeout')
    
    total_position = sum(t['position_size'] for t in all_trades)
    total_fee = total_position * 0.0008  # 0.08%
    net_pnl = total_pnl - total_fee
    
    closed_count = len(all_trades)
    signal_rate = signal_scans / total_scans * 100 if total_scans > 0 else 0
    
    print(f"""
┌─────────────────────────────────────────────────────────────────────────┐
│  扫描统计                                                              │
├─────────────────────────────────────────────────────────────────────────┤
│  总扫描次数            │  {total_scans:>6} 次  ({total_scans/len(symbols):.0f} 次/标的)                          │
│  触发信号次数          │  {signal_scans:>6} 次  (信号率：{signal_rate:.1f}%)                               │
├─────────────────────────────────────────────────────────────────────────┤
│  交易统计                                                              │
├─────────────────────────────────────────────────────────────────────────┤
│  已平仓交易            │  {closed_count:>6} 笔                                               │
│  总仓位金额            │  ${total_position:>8.0f}U                                           │
│  总手续费              │  ${total_fee:>8.2f}U                                           │
├─────────────────────────────────────────────────────────────────────────┤
│  盈亏统计                                                              │
├─────────────────────────────────────────────────────────────────────────┤
│  毛盈亏                │  ${total_pnl:>8.2f}U                                           │
│  净盈亏（扣手续费）    │  ${net_pnl:>8.2f}U                                           │
│  平均盈亏/笔           │  ${net_pnl/max(1,closed_count):>+6.2f}U                                            │
├─────────────────────────────────────────────────────────────────────────┤
│  出场方式                                                              │
├─────────────────────────────────────────────────────────────────────────┤
│  止盈 (TP2)            │  {tp_count:>6} 笔 ({tp_count/max(1,closed_count)*100:.1f}%)                                     │
│  止损 (SL)             │  {sl_count:>6} 笔 ({sl_count/max(1,closed_count)*100:.1f}%)                                     │
│  超时平仓              │  {timeout_count:>6} 笔 ({timeout_count/max(1,closed_count)*100:.1f}%)                                   │
├─────────────────────────────────────────────────────────────────────────┤
│  胜率统计                                                              │
├─────────────────────────────────────────────────────────────────────────┤
│  盈利笔数              │  {len(winning):>6} 笔                                              │
│  亏损笔数              │  {len(losing):>6} 笔                                              │
│  胜率                  │  {len(winning)/max(1,closed_count)*100:>5.1f}%                                             │
└─────────────────────────────────────────────────────────────────────────┘
""")
    
    # 各标的表现
    print(f"\n{'='*110}")
    print("📋 各标的交易详情")
    print(f"{'='*110}")
    
    by_symbol = {}
    for t in all_trades:
        if t['symbol'] not in by_symbol:
            by_symbol[t['symbol']] = {'count': 0, 'pnl': 0, 'win': 0, 'tp': 0, 'sl': 0}
        by_symbol[t['symbol']]['count'] += 1
        by_symbol[t['symbol']]['pnl'] += t['pnl']
        if t['pnl'] > 0:
            by_symbol[t['symbol']]['win'] += 1
        if t['reason'] == 'tp2':
            by_symbol[t['symbol']]['tp'] += 1
        if t['reason'] == 'sl':
            by_symbol[t['symbol']]['sl'] += 1
    
    print(f"\n{'标的':<15} {'笔数':>6} {'盈利':>6} {'胜率':>8} {'止盈':>6} {'止损':>6} {'总盈亏':>12} {'平均/笔':>10}")
    print("-"*75)
    for sym, data in sorted(by_symbol.items(), key=lambda x: x[1]['pnl'], reverse=True):
        win_rate = data['win']/data['count']*100 if data['count']>0 else 0
        avg = data['pnl']/data['count'] if data['count']>0 else 0
        print(f"{sym:<15} {data['count']:>6} {data['win']:>6} {win_rate:>7.1f}% {data['tp']:>6} {data['sl']:>6} ${data['pnl']:>+10.2f}U ${avg:>+8.2f}U")
    
    # 大赢大亏
    print(f"\n{'='*110}")
    print("🎯 大赢大亏 TOP10")
    print(f"{'='*110}")
    
    big_wins = [t for t in all_trades if t['pnl'] > 15]
    big_losses = [t for t in all_trades if t['pnl'] < -10]
    
    print(f"\n大赢交易 (>$15): {len(big_wins)} 笔")
    if big_wins:
        print(f"{'标的':<12} {'开仓时间':<14} {'评分':>6} {'仓位':>8} {'盈亏':>10}")
        print("-"*55)
        for t in sorted(big_wins, key=lambda x: x['pnl'], reverse=True)[:10]:
            print(f"{t['symbol']:<12} {t['scan_time'].strftime('%m-%d %H:%M'):<14} {t['score']:>6} ${t['position_size']:>6}U ${t['pnl']:>+10.2f}U")
    
    print(f"\n大亏交易 (<$-10): {len(big_losses)} 笔")
    if big_losses:
        print(f"{'标的':<12} {'开仓时间':<14} {'评分':>6} {'仓位':>8} {'盈亏':>10}")
        print("-"*55)
        for t in sorted(big_losses, key=lambda x: x['pnl'])[:10]:
            print(f"{t['symbol']:<12} {t['scan_time'].strftime('%m-%d %H:%M'):<14} {t['score']:>6} ${t['position_size']:>6}U ${t['pnl']:>+10.2f}U")
    
    # 按评分分段统计
    print(f"\n{'='*110}")
    print("📈 按评分分段统计")
    print(f"{'='*110}")
    
    by_score_range = {}
    for t in all_trades:
        if t['score'] >= 95:
            rng = '95-100'
        elif t['score'] >= 90:
            rng = '90-94'
        elif t['score'] >= 85:
            rng = '85-89'
        elif t['score'] >= 80:
            rng = '80-84'
        else:
            rng = '70-79'
        
        if rng not in by_score_range:
            by_score_range[rng] = {'count': 0, 'pnl': 0, 'win': 0}
        by_score_range[rng]['count'] += 1
        by_score_range[rng]['pnl'] += t['pnl']
        if t['pnl'] > 0:
            by_score_range[rng]['win'] += 1
    
    print(f"\n{'评分区间':<12} {'笔数':>6} {'盈利':>6} {'胜率':>8} {'总盈亏':>12} {'平均/笔':>10}")
    print("-"*60)
    for rng in ['95-100', '90-94', '85-89', '80-84', '70-79']:
        if rng in by_score_range:
            data = by_score_range[rng]
            win_rate = data['win']/data['count']*100 if data['count']>0 else 0
            avg = data['pnl']/data['count'] if data['count']>0 else 0
            print(f"{rng:<12} {data['count']:>6} {data['win']:>6} {win_rate:>7.1f}% ${data['pnl']:>+10.2f}U ${avg:>+8.2f}U")
    
    # 最终结论
    print(f"\n{'='*110}")
    print("🏁 最终结论")
    print(f"{'='*110}")
    
    if net_pnl > 0:
        print(f"\n✅ **盈利**：7 天净收益 ${net_pnl:+.2f}U（日均 ${net_pnl/7:+.2f}U）")
        print(f"   交易{closed_count}笔，胜率{len(winning)/closed_count*100:.1f}%，平均${net_pnl/closed_count:+.2f}U/笔")
    else:
        print(f"\n❌ **亏损**：7 天净亏损 ${net_pnl:+.2f}U（日均 ${net_pnl/7:+.2f}U）")
        print(f"   交易{closed_count}笔，胜率{len(winning)/closed_count*100:.1f}%")
    
    print(f"\n关键发现：")
    if len(big_losses) > 5:
        print(f"   ⚠️ 大亏交易{len(big_losses)}笔，需要加强风险控制")
    if tp_count < sl_count:
        print(f"   ⚠️ 止盈率偏低 ({tp_count/closed_count*100:.1f}%)，可能需要扩大止盈距离")
    
    print(f"\n{'='*110}")
    print(f"回测完成：{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*110}")

if __name__ == '__main__':
    main()
