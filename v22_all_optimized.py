#!/usr/bin/env python3
"""
v2.2 完全优化版 - 综合优化
7.25-8.2 真实数据回测

优化项：
1. ✅ 信号质量过滤（70-75 分 + 低量比→跳过）
2. ✅ 仓位管理增强（币种系数 + 连亏降仓）
3. ✅ 动态止盈 v2（基于市场状态 ADX）
4. ✅ 支撑位共振（简化版清算图）
5. ✅ 信号去重（相关性检查）
6. ✅ 时间过滤增强（分币种）

不做：
❌ 币种黑名单（用户要求：模拟结果不代表实盘）
"""

import json
import urllib.request
from datetime import datetime, timedelta
from collections import defaultdict

def fetch_tickers():
    """获取 24h 涨跌幅数据"""
    url = "https://fapi.binance.com/fapi/v1/ticker/24hr"
    try:
        with urllib.request.urlopen(url, timeout=15) as response:
            data = json.loads(response.read())
            usdt_pairs = [t for t in data if t['symbol'].endswith('USDT') and float(t['quoteVolume']) > 100000000]
            return usdt_pairs
    except:
        return []

def fetch_30m_klines(symbol, start_ts, end_ts):
    """获取 30 分钟 K 线"""
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval=30m&startTime={start_ts}&endTime={end_ts}&limit=1000"
    try:
        with urllib.request.urlopen(url, timeout=15) as response:
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

def calculate_adx(klines, period=14):
    """计算 ADX 趋势强度"""
    if len(klines) < period + 1:
        return 20
    
    plus_dm = []
    minus_dm = []
    tr = []
    
    for i in range(1, len(klines)):
        high_diff = klines[i]['high'] - klines[i-1]['high']
        low_diff = klines[i-1]['low'] - klines[i]['low']
        
        if high_diff > low_diff and high_diff > 0:
            plus_dm.append(high_diff)
        else:
            plus_dm.append(0)
        
        if low_diff > high_diff and low_diff > 0:
            minus_dm.append(low_diff)
        else:
            minus_dm.append(0)
        
        high_low = klines[i]['high'] - klines[i]['low']
        high_close = abs(klines[i]['high'] - klines[i-1]['close'])
        low_close = abs(klines[i]['low'] - klines[i-1]['close'])
        tr.append(max(high_low, high_close, low_close))
    
    if len(tr) < period:
        return 20
    
    atr = sum(tr[-period:]) / period
    plus_di = (sum(plus_dm[-period:]) / period) / atr * 100 if atr > 0 else 0
    minus_di = (sum(minus_dm[-period:]) / period) / atr * 100 if atr > 0 else 0
    
    dx = abs(plus_di - minus_di) / (plus_di + minus_di) * 100 if (plus_di + minus_di) > 0 else 0
    return dx

def detect_market_state(klines):
    """
    检测市场状态
    返回：'trending' | 'ranging' | 'volatile'
    """
    adx = calculate_adx(klines)
    current_atr = calculate_atr(klines[-14:])
    avg_atr = calculate_atr(klines[-50:]) if len(klines) >= 50 else current_atr
    atr_ratio = current_atr / avg_atr if avg_atr > 0 else 1.0
    
    if adx > 25 and atr_ratio > 1.2:
        return 'trending'
    elif adx < 20:
        return 'ranging'
    else:
        return 'volatile'

def find_support_level(klines, lookback=50):
    """寻找支撑位（简化版清算图）"""
    if len(klines) < lookback:
        return klines[-1]['close']
    
    # 找最近 50 根 K 线的最低点作为支撑
    lows = [k['low'] for k in klines[-lookback:]]
    return min(lows)

def score_v22_optimized(klines, scan_time, open_positions, symbol_history, symbol):
    """
    v2.2 综合优化版评分
    """
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
    
    # === 优化 1: 强势币加分 ===
    if 70 < rsi < 80 and vol_ratio > 2.0:
        score += 5
    
    # === 优化 2: 时间过滤增强（分币种）===
    hour = scan_time.hour
    # 主流币美盘权重高
    if symbol in ['BTCUSDT', 'ETHUSDT', 'BNBUSDT']:
        if 13 <= hour <= 22:
            score += 3
        elif 0 <= hour <= 6:
            score -= 3
    else:
        # 山寨币 24 小时无差别
        pass
    
    # === 优化 3: 支撑位共振（简化清算图）===
    support = find_support_level(klines)
    current_price = prices[-1]
    if abs(current_price - support) / support < 0.02:  # 接近支撑位 2% 以内
        score += 10  # 共振加分
    
    # === 优化 4: 信号去重（相关性检查）===
    # 检查当前持仓中同生态系统的币种数量
    btc_eco = ['BTCUSDT', 'ETHUSDT', 'LINKUSDT', 'UNIUSDT']
    if len([p for p in open_positions if p['symbol'] in btc_eco]) >= 2:
        if symbol in btc_eco:
            score -= 10  # 降低相关性风险
    
    # === 优化 5: 信号质量过滤 ===
    # 70-75 分 + 低量比或 RSI 极端→跳过
    min_score = 70
    if 70 <= score < 75 and (vol_ratio < 1.5 or rsi > 80 or rsi < 20):
        min_score = 80  # 提高门槛，实际会跳过
    
    # 相关性格离（持仓≥3 笔时提高门槛）
    if len(open_positions) >= 3:
        min_score = max(min_score, 80)
    
    final_score = min(score, 100)
    return (final_score, min_score)

def get_position_size_optimized(score, symbol, consecutive_losses, entry_atr, entry_price, symbol_history, max_loss=50):
    """
    优化仓位管理：基础分级 + 币种系数 + 连亏降仓 + 最大亏损限制
    """
    # 基础仓位
    if score >= 95:
        base = 660
    elif score >= 85:
        base = 440
    else:
        base = 220
    
    # 币种系数（根据历史胜率）
    if symbol in symbol_history and symbol_history[symbol]['count'] >= 5:
        win_rate = symbol_history[symbol]['win'] / symbol_history[symbol]['count']
        if win_rate > 0.7:
            symbol_multiplier = 1.2
        elif win_rate < 0.4:
            symbol_multiplier = 0.5
        else:
            symbol_multiplier = 1.0
    else:
        symbol_multiplier = 1.0
    
    # 连续亏损降仓
    if consecutive_losses >= 3:
        loss_multiplier = 0.5
    elif consecutive_losses >= 5:
        loss_multiplier = 0.25
    else:
        loss_multiplier = 1.0
    
    position = base * symbol_multiplier * loss_multiplier
    
    # 最大亏损限制
    sl_distance = entry_atr * 1.5
    max_position = max_loss / (sl_distance / entry_price) if sl_distance > 0 else position
    position = min(position, max_position)
    
    return position

def get_dynamic_tp_sl(entry, atr, market_state):
    """
    优化动态止盈止损 v2 - 基于市场状态
    """
    if market_state == 'trending':
        # 趋势行情：让利润奔跑
        sl = entry - atr * 2.0
        tp1 = entry + atr * 3.0
        tp2 = entry + atr * 8.0
        trailing_trigger = 3.0
    
    elif market_state == 'ranging':
        # 震荡行情：快速止盈
        sl = entry - atr * 1.0
        tp1 = entry + atr * 1.5
        tp2 = entry + atr * 3.0
        trailing_trigger = 1.5
    
    else:  # volatile
        # 高波动：宽止损，分批止盈
        sl = entry - atr * 2.0
        tp1 = entry + atr * 2.5
        tp2 = entry + atr * 5.0
        trailing_trigger = 2.5
    
    return sl, tp1, tp2, trailing_trigger

def simulate_trade_dynamic(entry, entry_atr, position_size, future_klines, market_state):
    """
    动态止盈止损模拟
    """
    sl, tp1, tp2, trailing_trigger = get_dynamic_tp_sl(entry, entry_atr, market_state)
    
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
        
        # 超过 96 小时强制平仓
        if holding_hours > 96:
            last_price = k['close']
            pnl = position_size * (last_price - entry) / entry * position_remaining
            total_pnl += pnl
            reason = 'timeout'
            exit_time = k_time
            exit_price = last_price
            position_remaining = 0
            break
        
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
        
        # 追踪止损：盈利>trailing_trigger*ATR 后移动
        if position_remaining > 0 and best_price > entry + entry_atr * trailing_trigger:
            trailing_sl = best_price - entry_atr * 1
            if trailing_sl > sl:
                sl = trailing_sl
    
    # 未平仓不计入
    if position_remaining > 0:
        return None, 'holding', None, None
    
    return total_pnl, reason, exit_time, exit_price

def main():
    print("="*110)
    print("v2.2 完全优化版 - 综合优化回测")
    print("涨幅榜/跌幅榜前 10 | 30 分钟 K 线 | 7 天数据 | 半小时扫描")
    print("="*110)
    
    # 获取涨幅榜/跌幅榜
    tickers = fetch_tickers()
    if not tickers:
        print("❌ 无法获取涨跌幅数据")
        return
    
    sorted_by_change = sorted(tickers, key=lambda x: abs(float(x['priceChangePercent'])), reverse=True)
    symbols = [t['symbol'] for t in sorted_by_change[:10]]
    
    print(f"\n📊 选币：24h 涨跌幅绝对值前 10（高波动）")
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
    print(f"📏 追踪周期：192 根 K 线（96 小时）")
    
    print(f"\n🎯 优化项：")
    print(f"   ✅ 信号质量过滤（70-75 分 + 低量比→跳过）")
    print(f"   ✅ 仓位管理增强（币种系数 + 连亏降仓）")
    print(f"   ✅ 动态止盈 v2（基于市场状态 ADX）")
    print(f"   ✅ 支撑位共振（简化版清算图）")
    print(f"   ✅ 信号去重（相关性检查）")
    print(f"   ✅ 时间过滤增强（分币种）")
    print(f"   ❌ 币种黑名单（暂不做）")
    
    # 获取 K 线数据
    klines_dict = {}
    for sym in symbols:
        klines = fetch_30m_klines(sym, start_ts, end_ts)
        if klines and len(klines) >= 300:
            klines_dict[sym] = klines
            print(f"  ✅ {sym}: {len(klines)} 根")
    
    if not klines_dict:
        print("\n❌ 没有足够数据的标的")
        return
    
    # 模拟交易
    all_trades = []
    open_positions = []
    symbol_history = defaultdict(lambda: {'count': 0, 'win': 0, 'loss': 0})
    consecutive_losses = defaultdict(int)
    total_scans = 0
    signal_scans = 0
    
    print(f"\n{'='*110}")
    print("开始模拟交易...")
    print(f"{'='*110}")
    
    for sym in klines_dict:
        klines = klines_dict[sym]
        consecutive_losses[sym] = 0
        
        for i in range(30, len(klines)-192, 1):
            total_scans += 1
            snapshot = klines[:i+1]
            future = klines[i+1:i+193]
            
            current_price = snapshot[-1]['close']
            scan_time = datetime.utcfromtimestamp(snapshot[-1]['timestamp']/1000)
            entry_atr = calculate_atr(snapshot)
            market_state = detect_market_state(snapshot)
            
            score_result = score_v22_optimized(snapshot, scan_time, open_positions, symbol_history, sym)
            if not score_result:
                continue
            
            score, min_score = score_result
            
            if score >= min_score:
                signal_scans += 1
                position_size = get_position_size_optimized(
                    score, sym, consecutive_losses[sym], entry_atr, current_price, symbol_history
                )
                entry = current_price * 0.99
                
                pnl, reason, exit_time, exit_price = simulate_trade_dynamic(
                    entry, entry_atr, position_size, future, market_state
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
                        'market_state': market_state
                    }
                    all_trades.append(trade)
                    
                    # 更新历史
                    symbol_history[sym]['count'] += 1
                    if pnl > 0:
                        symbol_history[sym]['win'] += 1
                        consecutive_losses[sym] = 0
                    else:
                        symbol_history[sym]['loss'] += 1
                        consecutive_losses[sym] += 1
                
                # 更新持仓
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
    total_fee = total_position * 0.0008
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
            by_symbol[t['symbol']] = {'count': 0, 'pnl': 0, 'win': 0}
        by_symbol[t['symbol']]['count'] += 1
        by_symbol[t['symbol']]['pnl'] += t['pnl']
        if t['pnl'] > 0:
            by_symbol[t['symbol']]['win'] += 1
    
    print(f"\n{'标的':<15} {'笔数':>6} {'盈利':>6} {'胜率':>8} {'总盈亏':>12} {'平均/笔':>10}")
    print("-"*65)
    for sym, data in sorted(by_symbol.items(), key=lambda x: x[1]['pnl'], reverse=True):
        win_rate = data['win']/data['count']*100 if data['count']>0 else 0
        avg = data['pnl']/data['count'] if data['count']>0 else 0
        print(f"{sym:<15} {data['count']:>6} {data['win']:>6} {win_rate:>7.1f}% ${data['pnl']:>+10.2f}U ${avg:>+8.2f}U")
    
    # 按市场状态分类
    print(f"\n{'='*110}")
    print("📈 按市场状态分类")
    print(f"{'='*110}")
    
    by_state = {}
    for t in all_trades:
        state = t['market_state']
        if state not in by_state:
            by_state[state] = {'count': 0, 'pnl': 0, 'win': 0}
        by_state[state]['count'] += 1
        by_state[state]['pnl'] += t['pnl']
        if t['pnl'] > 0:
            by_state[state]['win'] += 1
    
    print(f"\n{'市场状态':<15} {'笔数':>6} {'盈利':>6} {'胜率':>8} {'总盈亏':>12} {'平均/笔':>10}")
    print("-"*65)
    for state in ['trending', 'ranging', 'volatile']:
        if state in by_state:
            data = by_state[state]
            win_rate = data['win']/data['count']*100 if data['count']>0 else 0
            avg = data['pnl']/data['count'] if data['count']>0 else 0
            print(f"{state:<15} {data['count']:>6} {data['win']:>6} {win_rate:>7.1f}% ${data['pnl']:>+10.2f}U ${avg:>+8.2f}U")
    
    # 大赢大亏
    print(f"\n{'='*110}")
    print("🎯 大赢大亏 TOP10")
    print(f"{'='*110}")
    
    big_wins = [t for t in all_trades if t['pnl'] > 15]
    big_losses = [t for t in all_trades if t['pnl'] < -10]
    
    print(f"\n大赢交易 (>$15): {len(big_wins)} 笔")
    if big_wins:
        for t in sorted(big_wins, key=lambda x: x['pnl'], reverse=True)[:10]:
            print(f"  {t['symbol']:<12} {t['scan_time'].strftime('%m-%d %H:%M')} 评分{t['score']} ${t['position_size']:.0f}U ${t['pnl']:+.2f}U")
    
    print(f"\n大亏交易 (<$-10): {len(big_losses)} 笔")
    if big_losses:
        for t in sorted(big_losses, key=lambda x: x['pnl'])[:10]:
            print(f"  {t['symbol']:<12} {t['scan_time'].strftime('%m-%d %H:%M')} 评分{t['score']} ${t['position_size']:.0f}U ${t['pnl']:+.2f}U")
    
    # 对比之前版本
    print(f"\n{'='*110}")
    print("📊 版本对比")
    print(f"{'='*110}")
    print("""
┌──────────────────┬──────────────┬──────────────┬──────────────┐
│  指标            │  未优化      │  综合优化    │  提升        │
├──────────────────┼──────────────┼──────────────┼──────────────┤
│  交易笔数        │  800 笔       │  TBD         │  ?           │
│  净盈亏          │  +$2326U     │  TBD         │  ?           │
│  平均/笔         │  +$2.91U     │  TBD         │  ?           │
│  止盈率          │  32.8%       │  TBD         │  ?           │
│  胜率            │  60.0%       │  TBD         │  ?           │
└──────────────────┴──────────────┴──────────────┴──────────────┘
""")
    
    # 最终结论
    print(f"\n{'='*110}")
    print("🏁 最终结论")
    print(f"{'='*110}")
    
    if net_pnl > 0:
        print(f"\n✅ **盈利**：7 天净收益 ${net_pnl:+.2f}U（日均 ${net_pnl/7:+.2f}U）")
        print(f"   交易{closed_count}笔，胜率{len(winning)/closed_count*100:.1f}%，平均${net_pnl/closed_count:+.2f}U/笔")
    else:
        print(f"\n❌ **亏损**：7 天净亏损 ${net_pnl:+.2f}U（日均 ${net_pnl/7:+.2f}U）")
    
    print(f"\n优化效果评估：")
    if len(all_trades) < 800:
        print(f"   ✅ 交易笔数减少 {800-len(all_trades)} 笔（信号质量过滤生效）")
    if len(winning)/max(1,closed_count) > 0.6:
        print(f"   ✅ 胜率{len(winning)/closed_count*100:.1f}% > 60%（优化有效）")
    if net_pnl/7 > 332:
        print(f"   ✅ 日均收益 ${net_pnl/7:.0f}U > $332U（收益提升）")
    
    print(f"\n{'='*110}")
    print(f"回测完成：{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*110}")

if __name__ == '__main__':
    main()
