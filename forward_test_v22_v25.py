#!/usr/bin/env python3
"""
v2.2 vs v2.5 前向测试（Forward Test）
使用真实历史数据，按时间顺序模拟扫描
每个时点只用该时点之前的数据评分
挂单后追踪真实后续 K 线判断成交和盈亏
"""

import json
import urllib.request
from datetime import datetime, timedelta

def fetch_klines_historical(symbol, start_ts, end_ts, interval="1h", limit=500):
    """获取历史 K 线数据"""
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval={interval}&startTime={start_ts}&endTime={end_ts}&limit={limit}"
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            data = json.loads(response.read())
            return [{
                'timestamp': k[0],
                'open': float(k[1]), 'high': float(k[2]),
                'low': float(k[3]), 'close': float(k[4]),
                'quote_volume': float(k[7])
            } for k in data]
    except:
        return None

def calculate_rsi(prices, period=14):
    if len(prices) < period + 1: return 50.0
    gains = [max(0, prices[i] - prices[i-1]) for i in range(1, len(prices))]
    losses = [max(0, prices[i-1] - prices[i]) for i in range(1, len(prices))]
    avg_gain, avg_loss = sum(gains[-period:])/period, sum(losses[-period:])/period
    return 100 - (100 / (1 + avg_gain/avg_loss)) if avg_loss > 0 else 100.0

def calculate_ema(prices, period):
    if len(prices) < period: return prices[-1] if prices else 0
    mult = 2/(period+1)
    ema = sum(prices[:period])/period
    for p in prices[period:]: ema = (p-ema)*mult + ema
    return ema

def calculate_macd(prices):
    if len(prices) < 26: return {'histogram': 0}
    e12, e26 = calculate_ema(prices,12), calculate_ema(prices,26)
    return {'histogram': (e12-e26)*0.1}

def calculate_atr(klines, period=14):
    if len(klines) < period: return klines[-1]['close'] * 0.05 if klines else 0.05
    tr = [max(klines[i]['high']-klines[i]['low'], abs(klines[i]['high']-klines[i-1]['close']), abs(klines[i]['low']-klines[i-1]['close'])) for i in range(1, len(klines))]
    return sum(tr[-period:])/period if tr else 0.05

def count_consecutive(klines, direction):
    if len(klines) < 2: return 0
    count = 0
    for i in range(len(klines)-2, -1, -1):
        curr, prev = klines[i]['close'], klines[i+1]['close'] if i+1<len(klines) else klines[i]['open']
        if (direction=='long' and curr>prev) or (direction=='short' and curr<prev): count+=1
        else: break
    return min(count, 3)

def score_v22(klines):
    """v2.2 评分（基础分 50）"""
    if len(klines) < 30: return None
    closed = klines[:-1]
    prices = [k['close'] for k in closed]
    vols = [k['quote_volume'] for k in closed]
    rsi = calculate_rsi(prices)
    macd = calculate_macd(prices)
    e9, e21, e55 = calculate_ema(prices,9), calculate_ema(prices,21), calculate_ema(prices,55)
    vol_ratio = vols[-1]/sum(vols[-10:])*10 if len(vols)>=10 else 1.0
    p_change = (prices[-1]-prices[-20])/prices[-20]*100 if len(prices)>=20 else 0
    
    score = 50
    score += 10 if 40<=rsi<=60 else (5 if 30<=rsi<40 or 60<rsi<=70 else 0)
    score += 10 if macd['histogram']>0 else (5 if macd['histogram']>-0.001 else 0)
    score += 15 if e9>e21>e55 else 5
    score += 15 if vol_ratio>2.0 else (10 if vol_ratio>1.5 else (5 if vol_ratio>1.0 else 0))
    score += 10 if abs(p_change)>10 else (5 if abs(p_change)>5 else 0)
    return min(score, 100)

def score_v25(klines, is_low_cap=False):
    """v2.5 评分（基础分 20）"""
    if len(klines) < 30: return None
    closed = klines[:-1]
    prices = [k['close'] for k in closed]
    vols = [k['quote_volume'] for k in closed]
    rsi = calculate_rsi(prices)
    macd = calculate_macd(prices)
    e9, e21, e55 = calculate_ema(prices,9), calculate_ema(prices,21), calculate_ema(prices,55)
    vol_ratio = vols[-2]/sum(vols[-12:-2])*10 if len(vols)>=12 else 1.0
    p_change = (prices[-1]-prices[-20])/prices[-20]*100 if len(prices)>=20 else 0
    high_20, low_20 = max(prices[-20:]), min(prices[-20:])
    p_pos = (prices[-1]-low_20)/(high_20-low_20) if high_20>low_20 else 0.5
    support_touches = sum(1 for p in prices[-20:] if (p-low_20)/(high_20-low_20)<0.15) if high_20>low_20 else 0
    consecutive = count_consecutive(klines, 'long')
    
    score = 20
    if 45<=rsi<=65: rs=18
    elif 40<=rsi<45 or 65<rsi<=70: rs=12
    elif 30<=rsi<40 or 70<rsi<=75: rs=6
    else: rs=0
    rp = 15 if rsi>75 else (10 if rsi>70 else (8 if rsi<20 else 0))
    score += rs - rp
    score += 15 if macd['histogram']>0.001 else (10 if macd['histogram']>0 else (5 if macd['histogram']>-0.0005 else 0))
    score += 18 if e9>e21>e55 and e9>prices[-1] else (13 if e9>e21>e55 else (7 if e9>e21 or e21>e55 else 0))
    score += 18 if vol_ratio>2.0 else (13 if vol_ratio>1.5 else (10 if vol_ratio>1.0 else (5 if vol_ratio>0.8 else 0)))
    score += 15 if (p_pos<0.3 or support_touches>=3) else (10 if (p_pos<0.5 or support_touches>=2) else (5 if p_pos<0.7 else 0))
    score += 10 if abs(p_change)>15 else (6 if abs(p_change)>8 else (3 if abs(p_change)>3 else 0))
    score += 7 if consecutive>=2 else (4 if consecutive==1 else 0)
    if is_low_cap: score -= 3
    
    return min(score, 100)

def run_forward_test():
    print("="*100)
    print("v2.2 vs v2.5 前向测试（Forward Test）")
    print("测试周期：2026-07-26 至 2026-08-02（7 天）")
    print("扫描频率：每 4 小时一次")
    print("="*100)
    
    # 测试标的（选择流动性好的主流币 + 部分活跃山寨币）
    test_symbols = [
        'BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT', 'XRPUSDT',
        'ADAUSDT', 'DOGEUSDT', 'LINKUSDT', 'AVAXUSDT', 'MATICUSDT',
        'DOTUSDT', 'UNIUSDT', 'LTCUSDT', 'ATOMUSDT', 'ETCUSDT',
        'FILUSDT', 'NEARUSDT', 'APTUSDT', 'ARBUSDT', 'OPUSDT'
    ]
    
    # 时间范围：7 天前到现在
    end_ts = int(datetime.utcnow().timestamp() * 1000)
    start_ts = int((datetime.utcnow() - timedelta(days=7)).timestamp() * 1000)
    
    # 扫描时间点（每 4 小时）
    scan_times = []
    current_ts = start_ts
    while current_ts < end_ts:
        scan_times.append(current_ts)
        current_ts += 4 * 3600 * 1000  # 4 小时
    
    print(f"\n📅 扫描时间点：{len(scan_times)}个（每 4 小时）")
    print(f"时间范围：{datetime.utcfromtimestamp(start_ts/1000)} 到 {datetime.utcfromtimestamp(end_ts/1000)}")
    
    # 存储结果
    positions_v22 = []
    positions_v25 = []
    
    # 对每个扫描时间点执行
    for scan_idx, scan_ts in enumerate(scan_times):
        if scan_idx % 10 == 0:
            print(f"\n扫描进度：{scan_idx}/{len(scan_times)} ({scan_idx/len(scan_times)*100:.0f}%)")
        
        # 获取每个标的到当前时点的 K 线
        for symbol in test_symbols:
            # 获取历史数据（从 7 天前到当前扫描时点 + 额外 200 根用于后续追踪）
            klines_full = fetch_klines_historical(symbol, start_ts - 200*3600*1000, end_ts + 200*3600*1000, limit=1000)
            if not klines_full or len(klines_full) < 50:
                continue
            
            # 找到当前扫描时点对应的 K 线索引
            scan_idx_kline = next((i for i, k in enumerate(klines_full) if k['timestamp'] >= scan_ts), None)
            if scan_idx_kline is None or scan_idx_kline < 30:
                continue
            
            # 截取到当前时点的 K 线（用于评分）
            klines_snapshot = klines_full[:scan_idx_kline+1]
            
            # 获取 24H 成交量（用于判断低市值）
            volume_24h = sum(k['quote_volume'] for k in klines_snapshot[-24:]) if len(klines_snapshot)>=24 else sum(k['quote_volume'] for k in klines_snapshot[-10:])
            is_low_cap = volume_24h < 50000000  # 5000 万 USDT 以下为低市值
            
            # v2.2 评分
            score_v22_val = score_v22(klines_snapshot)
            
            # v2.5 评分
            score_v25_val = score_v25(klines_snapshot, is_low_cap)
            
            # 开仓决策
            current_price = klines_snapshot[-1]['close']
            current_time = datetime.utcfromtimestamp(scan_ts/1000)
            
            # v2.2 开仓检查
            if score_v22_val and score_v22_val >= 70:
                # 检查是否已有持仓
                if not any(p['symbol']==symbol and p['status']=='open' for p in positions_v22):
                    atr = calculate_atr(klines_snapshot)
                    order_price = current_price * 0.99  # -1% 限价
                    stop_loss = order_price - atr * 1.5
                    tp1 = order_price + atr * 2
                    tp2 = order_price + atr * 3
                    position_size = 440
                    
                    positions_v22.append({
                        'symbol': symbol, 'entry_time': current_time, 'entry_price': order_price,
                        'stop_loss': stop_loss, 'tp1': tp1, 'tp2': tp2,
                        'position_size': position_size, 'score': score_v22_val,
                        'status': 'pending', 'exit_price': None, 'exit_time': None,
                        'exit_reason': None, 'pnl': 0, 'klines': klines_full[scan_idx_kline:]
                    })
            
            # v2.5 开仓检查
            if score_v25_val and (score_v25_val >= 70 or (is_low_cap and score_v25_val >= 67)):
                if not any(p['symbol']==symbol and p['status']=='open' for p in positions_v25):
                    atr = calculate_atr(klines_snapshot)
                    order_price = current_price * 0.99
                    stop_loss = order_price - atr * 1.5
                    tp1 = order_price + atr * 2
                    tp2 = order_price + atr * 3
                    position_size = 440
                    
                    positions_v25.append({
                        'symbol': symbol, 'entry_time': current_time, 'entry_price': order_price,
                        'stop_loss': stop_loss, 'tp1': tp1, 'tp2': tp2,
                        'position_size': position_size, 'score': score_v25_val,
                        'status': 'pending', 'exit_price': None, 'exit_time': None,
                        'exit_reason': None, 'pnl': 0, 'klines': klines_full[scan_idx_kline:]
                    })
    
    # 追踪所有持仓的成交和盈亏
    print(f"\n{'='*100}")
    print("追踪持仓成交和盈亏...")
    print(f"{'='*100}")
    
    def track_positions(positions, version_name):
        filled = 0
        closed_tp = 0
        closed_sl = 0
        still_open = 0
        total_pnl = 0
        
        for pos in positions:
            klines_future = pos['klines'][1:50]  # 挂单后 50 根 K 线
            if not klines_future:
                continue
            
            # 检查 4 小时内是否成交
            order_price = pos['entry_price']
            expire_time = pos['entry_time'] + timedelta(hours=4)
            
            filled_price = None
            filled_time = None
            
            for k in klines_future[:16]:  # 4 小时 = 16 根 1H K 线
                k_time = datetime.utcfromtimestamp(k['timestamp']/1000)
                if k_time > expire_time:
                    break
                
                # 检查成交（多单：低价触及）
                if k['low'] <= order_price:
                    filled_price = order_price
                    filled_time = k_time
                    filled += 1
                    break
            
            if not filled_price:
                pos['status'] = 'cancelled'
                pos['exit_reason'] = 'timeout'
                continue
            
            # 成交后追踪止盈止损
            for k in klines_future[1:]:
                # 检查止损
                if k['low'] <= pos['stop_loss']:
                    pos['status'] = 'closed'
                    pos['exit_price'] = pos['stop_loss']
                    pos['exit_time'] = datetime.utcfromtimestamp(k['timestamp']/1000)
                    pos['exit_reason'] = 'stop_loss'
                    pos['pnl'] = pos['position_size'] * (pos['stop_loss'] - filled_price) / filled_price
                    total_pnl += pos['pnl']
                    closed_sl += 1
                    break
                
                # 检查止盈 2
                if k['high'] >= pos['tp2']:
                    pos['status'] = 'closed'
                    pos['exit_price'] = pos['tp2']
                    pos['exit_time'] = datetime.utcfromtimestamp(k['timestamp']/1000)
                    pos['exit_reason'] = 'tp2'
                    pos['pnl'] = pos['position_size'] * (pos['tp2'] - filled_price) / filled_price
                    total_pnl += pos['pnl']
                    closed_tp += 1
                    break
                
                # 检查止盈 1
                if k['high'] >= pos['tp1'] and pos['stop_loss'] < filled_price:
                    pos['stop_loss'] = filled_price  # 止损上移保本
            
            # 如果仍未平仓
            if pos['status'] == 'pending':
                pos['status'] = 'open'
                still_open += 1
                # 按最后一根 K 线收盘价计算浮盈浮亏
                if klines_future:
                    current_price = klines_future[-1]['close']
                    pos['pnl'] = pos['position_size'] * (current_price - filled_price) / filled_price
                    total_pnl += pos['pnl']
        
        return {
            'filled': filled, 'closed_tp': closed_tp, 'closed_sl': closed_sl,
            'still_open': still_open, 'total_pnl': total_pnl,
            'total': len(positions)
        }
    
    stats_v22 = track_positions(positions_v22, 'v2.2')
    stats_v25 = track_positions(positions_v25, 'v2.5')
    
    # 输出对比
    print(f"\n{'='*100}")
    print("前向测试结果对比")
    print(f"{'='*100}")
    
    print(f"""
┌─────────────────────────────────────────────────────────────────┐
│  指标              │  v2.2        │  v2.5        │  差异       │
├─────────────────────────────────────────────────────────────────┤
│  挂单总数          │  {stats_v22['total']:>3}          │  {stats_v25['total']:>3}          │  {stats_v25['total']-stats_v22['total']:>+#3}        │
│  成交笔数          │  {stats_v22['filled']:>3}          │  {stats_v25['filled']:>3}          │  {stats_v25['filled']-stats_v22['filled']:>+#3}        │
│  止盈 (TP2)        │  {stats_v22['closed_tp']:>3}          │  {stats_v25['closed_tp']:>3}          │  {stats_v25['closed_tp']-stats_v22['closed_tp']:>+#3}        │
│  止损 (SL)         │  {stats_v22['closed_sl']:>3}          │  {stats_v25['closed_sl']:>3}          │  {stats_v25['closed_sl']-stats_v22['closed_sl']:>+#3}        │
│  持仓中            │  {stats_v22['still_open']:>3}          │  {stats_v25['still_open']:>3}          │  {stats_v25['still_open']-stats_v22['still_open']:>+#3}        │
│  总盈亏            │  ${stats_v22['total_pnl']:>+8.1f}U   │  ${stats_v25['total_pnl']:>+8.1f}U   │  ${stats_v25['total_pnl']-stats_v22['total_pnl']:>+8.1f}U   │
│  平均盈亏/笔       │  ${stats_v22['total_pnl']/max(1,stats_v22['filled']):>+6.1f}U    │  ${stats_v25['total_pnl']/max(1,stats_v25['filled']):>+6.1f}U    │  ${stats_v25['total_pnl']/max(1,stats_v25['filled'])-stats_v22['total_pnl']/max(1,stats_v22['filled']):>+6.1f}U    │
│  胜率              │  {stats_v22['closed_tp']/max(1,stats_v22['closed_tp']+stats_v22['closed_sl'])*100:>5.1f}%       │  {stats_v25['closed_tp']/max(1,stats_v25['closed_tp']+stats_v25['closed_sl'])*100:>5.1f}%       │  {(stats_v25['closed_tp']/max(1,stats_v25['closed_tp']+stats_v25['closed_sl'])-stats_v22['closed_tp']/max(1,stats_v22['closed_tp']+stats_v22['closed_sl']))*100:>+5.1f}%      │
└─────────────────────────────────────────────────────────────────┘
""")
    
    # 结论
    print(f"\n{'='*100}")
    print("结论")
    print(f"{'='*100}")
    
    if stats_v22['total_pnl'] > 0 and stats_v25['total_pnl'] > 0:
        conclusion = "✅ 两个版本都盈利"
    elif stats_v22['total_pnl'] < 0 and stats_v25['total_pnl'] < 0:
        conclusion = "❌ 两个版本都亏损"
    elif stats_v22['total_pnl'] > 0:
        conclusion = "⚠️ v2.2 盈利，v2.5 亏损"
    else:
        conclusion = "⚠️ v2.5 盈利，v2.2 亏损"
    
    print(f"""
{conclusion}

v2.2: ${stats_v22['total_pnl']:+.1f}U ({stats_v22['filled']}笔成交，{stats_v22['closed_tp']}止盈/{stats_v22['closed_sl']}止损)
v2.5: ${stats_v25['total_pnl']:+.1f}U ({stats_v25['filled']}笔成交，{stats_v25['closed_tp']}止盈/{stats_v25['closed_sl']}止损)

差异：${stats_v25['total_pnl']-stats_v22['total_pnl']:+.1f}U

前向测试 vs 之前模拟:
  - 前向测试使用真实时间序列，更准确
  - 之前模拟用过去 K 线模拟未来，有偏差
  - 前向测试胜率更可靠
""")
    
    print(f"\n{'='*100}")
    print(f"测试完成：{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*100}")

if __name__ == '__main__':
    run_forward_test()
