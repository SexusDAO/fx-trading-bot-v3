#!/usr/bin/env python3
"""
v2.2 vs v2.5 前向测试 - 简化版
测试 10 个主流标的，7 天数据，每 4 小时扫描
"""

import json
import urllib.request
from datetime import datetime, timedelta

def fetch_klines(symbol, end_ts, limit=500):
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval=1h&endTime={end_ts}&limit={limit}"
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            data = json.loads(response.read())
            return [{'timestamp': k[0], 'open': float(k[1]), 'high': float(k[2]), 'low': float(k[3]), 'close': float(k[4]), 'quote_volume': float(k[7])} for k in data]
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

def score_v22(klines):
    if len(klines) < 30: return None
    closed = klines[:-1]
    prices = [k['close'] for k in closed]
    vols = [k['quote_volume'] for k in closed]
    rsi = calculate_rsi(prices)
    macd = calculate_macd(prices)
    e9, e21, e55 = calculate_ema(prices,9), calculate_ema(prices,21), calculate_ema(prices,55)
    vol_ratio = vols[-1]/sum(vols[-10:])*10 if len(vols)>=10 and sum(vols[-10:])>0 else 1.0
    p_change = (prices[-1]-prices[-20])/prices[-20]*100 if len(prices)>=20 else 0
    score = 50
    score += 10 if 40<=rsi<=60 else (5 if 30<=rsi<40 or 60<rsi<=70 else 0)
    score += 10 if macd['histogram']>0 else (5 if macd['histogram']>-0.001 else 0)
    score += 15 if e9>e21>e55 else 5
    score += 15 if vol_ratio>2.0 else (10 if vol_ratio>1.5 else (5 if vol_ratio>1.0 else 0))
    score += 10 if abs(p_change)>10 else (5 if abs(p_change)>5 else 0)
    return min(score, 100)

def score_v25(klines, is_low_cap=False):
    if len(klines) < 30: return None
    closed = klines[:-1]
    prices = [k['close'] for k in closed]
    vols = [k['quote_volume'] for k in closed]
    rsi = calculate_rsi(prices)
    macd = calculate_macd(prices)
    e9, e21, e55 = calculate_ema(prices,9), calculate_ema(prices,21), calculate_ema(prices,55)
    vol_ratio = vols[-2]/sum(vols[-12:-2])*10 if len(vols)>=12 and sum(vols[-12:-2])>0 else 1.0
    p_change = (prices[-1]-prices[-20])/prices[-20]*100 if len(prices)>=20 else 0
    high_20, low_20 = max(prices[-20:]), min(prices[-20:])
    p_pos = (prices[-1]-low_20)/(high_20-low_20) if high_20>low_20 else 0.5
    support_touches = sum(1 for p in prices[-20:] if (p-low_20)/(high_20-low_20)<0.15) if high_20>low_20 else 0
    consecutive = sum(1 for i in range(len(klines)-2, max(0,len(klines)-5), -1) if klines[i]['close']>klines[i+1]['close'])
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

def main():
    print("="*90)
    print("v2.2 vs v2.5 前向测试 - 简化版")
    print("7 天数据，每 4 小时扫描，真实后续 K 线判断盈亏")
    print("="*90)
    
    # 测试标的
    symbols = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT', 'XRPUSDT', 'ADAUSDT', 'DOGEUSDT', 'LINKUSDT', 'AVAXUSDT', 'MATICUSDT']
    
    # 7 天前时间戳
    end_ts = int(datetime.utcnow().timestamp() * 1000)
    start_ts = int((datetime.utcnow() - timedelta(days=7)).timestamp() * 1000)
    
    print(f"\n📅 时间范围：{datetime.utcfromtimestamp(start_ts/1000)} 到 {datetime.utcfromtimestamp(end_ts/1000)}")
    print(f"标的：{len(symbols)}个")
    
    results_v22 = []
    results_v25 = []
    
    for sym in symbols:
        klines = fetch_klines(sym, end_ts, limit=500)
        if not klines or len(klines) < 168:  # 7 天=168 小时
            continue
        
        # 每 4 小时扫描一次
        for i in range(30, len(klines)-20, 4):  # 从第 30 根开始，留 20 根追踪盈亏
            snapshot = klines[:i+1]
            future = klines[i+1:i+21]  # 后续 20 根 K 线
            
            current_price = snapshot[-1]['close']
            is_low_cap = sum(k['quote_volume'] for k in snapshot[-24:]) < 50000000
            
            # v2.2 评分
            s22 = score_v22(snapshot)
            if s22 and s22 >= 70:
                atr = calculate_atr(snapshot)
                entry = current_price * 0.99
                sl = entry - atr * 1.5
                tp2 = entry + atr * 3
                
                # 检查后续 K 线
                pnl = 0
                reason = 'holding'
                for k in future:
                    if k['low'] <= sl:
                        pnl = 440 * (sl - entry) / entry
                        reason = 'sl'
                        break
                    if k['high'] >= tp2:
                        pnl = 440 * (tp2 - entry) / entry
                        reason = 'tp2'
                        break
                
                results_v22.append({'symbol': sym, 'score': s22, 'pnl': pnl, 'reason': reason})
            
            # v2.5 评分
            s25 = score_v25(snapshot, is_low_cap)
            if s25 and (s25 >= 70 or (is_low_cap and s25 >= 67)):
                atr = calculate_atr(snapshot)
                entry = current_price * 0.99
                sl = entry - atr * 1.5
                tp2 = entry + atr * 3
                
                pnl = 0
                reason = 'holding'
                for k in future:
                    if k['low'] <= sl:
                        pnl = 440 * (sl - entry) / entry
                        reason = 'sl'
                        break
                    if k['high'] >= tp2:
                        pnl = 440 * (tp2 - entry) / entry
                        reason = 'tp2'
                        break
                
                results_v25.append({'symbol': sym, 'score': s25, 'pnl': pnl, 'reason': reason})
    
    # 汇总
    print(f"\n{'='*90}")
    print("前向测试结果")
    print(f"{'='*90}")
    
    total_pnl_v22 = sum(r['pnl'] for r in results_v22)
    tp_v22 = sum(1 for r in results_v22 if r['reason']=='tp2')
    sl_v22 = sum(1 for r in results_v22 if r['reason']=='sl')
    
    total_pnl_v25 = sum(r['pnl'] for r in results_v25)
    tp_v25 = sum(1 for r in results_v25 if r['reason']=='tp2')
    sl_v25 = sum(1 for r in results_v25 if r['reason']=='sl')
    
    print(f"""
┌─────────────────────────────────────────────────────────────┐
│  指标          │  v2.2        │  v2.5        │  差异       │
├─────────────────────────────────────────────────────────────┤
│  开仓笔数      │  {len(results_v22):>3}          │  {len(results_v25):>3}          │  {len(results_v25)-len(results_v22):>+#3}        │
│  总盈亏        │  ${total_pnl_v22:>+8.1f}U   │  ${total_pnl_v25:>+8.1f}U   │  ${total_pnl_v25-total_pnl_v22:>+8.1f}U   │
│  平均盈亏/笔   │  ${total_pnl_v22/max(1,len(results_v22)):>+6.1f}U    │  ${total_pnl_v25/max(1,len(results_v25)):>+6.1f}U    │  ${total_pnl_v25/max(1,len(results_v25))-total_pnl_v22/max(1,len(results_v22)):>+6.1f}U    │
│  止盈          │  {tp_v22:>3}          │  {tp_v25:>3}          │  {tp_v25-tp_v22:>+#3}        │
│  止损          │  {sl_v22:>3}          │  {sl_v25:>3}          │  {sl_v25-sl_v22:>+#3}        │
│  胜率          │  {tp_v22/max(1,tp_v22+sl_v22)*100:>5.1f}%       │  {tp_v25/max(1,tp_v25+sl_v25)*100:>5.1f}%       │  {(tp_v25/max(1,tp_v25+sl_v25)-tp_v22/max(1,tp_v22+sl_v22))*100:>+5.1f}%      │
└─────────────────────────────────────────────────────────────┘
""")
    
    # 结论
    print(f"\n{'='*90}")
    if total_pnl_v22 > 0 and total_pnl_v25 > 0:
        print("✅ 两个版本都盈利")
    elif total_pnl_v22 < 0 and total_pnl_v25 < 0:
        print("❌ 两个版本都亏损")
    elif total_pnl_v22 > 0:
        print("⚠️ v2.2 盈利，v2.5 亏损")
    else:
        print("⚠️ v2.5 盈利，v2.2 亏损")
    
    print(f"\nv2.2: ${total_pnl_v22:+.1f}U ({len(results_v22)}笔，{tp_v22}止盈/{sl_v22}止损)")
    print(f"v2.5: ${total_pnl_v25:+.1f}U ({len(results_v25)}笔，{tp_v25}止盈/{sl_v25}止损)")
    print(f"\n{'='*90}")
    print(f"完成：{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")

if __name__ == '__main__':
    main()
