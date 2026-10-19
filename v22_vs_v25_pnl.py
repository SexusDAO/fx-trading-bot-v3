#!/usr/bin/env python3
"""
v2.2 vs v2.5 完整收益对比模拟
使用相同的入场、止盈、止损逻辑，对比两个版本的最终盈亏
"""

import json
import urllib.request
import random
from datetime import datetime

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
    if len(klines) < period: return klines[-1]['close'] * 0.05
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

def fetch_klines(symbol, limit=200):
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval=1h&limit={limit}"
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            data = json.loads(response.read())
            return [{'timestamp': k[0], 'open': float(k[1]), 'high': float(k[2]), 'low': float(k[3]), 'close': float(k[4]), 'quote_volume': float(k[7])} for k in data]
    except:
        return None

def fetch_tickers():
    url = "https://fapi.binance.com/fapi/v1/ticker/24hr"
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.loads(response.read())

def simulate_trade(klines, entry_price, position_size=440):
    """
    模拟交易：用过去 20 根 K 线模拟未来走势
    返回：盈亏金额、平仓原因
    """
    if len(klines) < 20:
        return 0, 'no_data'
    
    # 用过去 20 根 K 线的波动模拟
    past_klines = klines[-20:-1]
    atr = calculate_atr(klines[:-1])
    
    stop_loss = entry_price - atr * 1.5
    tp1 = entry_price + atr * 2
    tp2 = entry_price + atr * 3
    
    stopped = False
    tp1_hit = False
    
    for k in past_klines:
        # 检查止损
        if k['low'] <= stop_loss and not stopped:
            pnl = position_size * (stop_loss - entry_price) / entry_price
            return pnl, 'stop_loss'
        
        # 检查止盈 2
        if k['high'] >= tp2:
            pnl = position_size * (tp2 - entry_price) / entry_price
            return pnl, 'tp2'
        
        # 检查止盈 1
        if k['high'] >= tp1 and not tp1_hit:
            tp1_hit = True
            stop_loss = entry_price  # 止损上移保本
    
    # 未平仓，按最后一根 K 线收盘价计算浮盈浮亏
    final_price = past_klines[-1]['close']
    pnl = position_size * (final_price - entry_price) / entry_price
    return pnl, 'holding'

def run_comparison():
    print("="*100)
    print("v2.2 vs v2.5 完整收益对比模拟")
    print("使用相同的入场、止盈、止损逻辑")
    print("="*100)
    
    tickers = fetch_tickers()
    candidates = [t for t in tickers if t['symbol'].endswith('USDT') and float(t.get('quoteVolume', 0)) > 1000000]
    gainers = sorted(candidates, key=lambda x: float(x.get('priceChangePercent', 0)), reverse=True)[:20]
    losers = sorted(candidates, key=lambda x: float(x.get('priceChangePercent', 0)))[:20]
    by_vol = sorted(candidates, key=lambda x: float(x.get('quoteVolume', 0)), reverse=True)[:60]
    symbols = list(set([t['symbol'] for t in gainers + losers + by_vol]))[:80]
    
    print(f"\n📊 候选标的：{len(symbols)}个")
    
    # 分别跑 v2.2 和 v2.5
    results_v22 = []
    results_v25 = []
    
    for sym in symbols:
        ticker = next((t for t in tickers if t['symbol']==sym), None)
        if not ticker: continue
        volume = float(ticker.get('quoteVolume', 0))
        change = float(ticker.get('priceChangePercent', 0))
        if volume < 1000000 or abs(change) > 70: continue
        
        klines = fetch_klines(sym, limit=200)
        if not klines or len(klines) < 50: continue
        
        is_low_cap = volume < 10000000
        current_price = klines[-1]['close']
        
        # v2.2 评分
        score_v22_val = score_v22(klines)
        if score_v22_val and score_v22_val >= 70:
            pnl, reason = simulate_trade(klines, current_price)
            results_v22.append({
                'symbol': sym, 'score': score_v22_val, 'price': current_price,
                'pnl': pnl, 'reason': reason, 'change': change
            })
        
        # v2.5 评分
        score_v25_val = score_v25(klines, is_low_cap)
        if score_v25_val and (score_v25_val >= 70 or (is_low_cap and score_v25_val >= 67)):
            pnl, reason = simulate_trade(klines, current_price)
            results_v25.append({
                'symbol': sym, 'score': score_v25_val, 'price': current_price,
                'pnl': pnl, 'reason': reason, 'change': change, 'is_low_cap': is_low_cap
            })
    
    # 汇总对比
    print(f"\n{'='*100}")
    print("收益对比汇总")
    print(f"{'='*100}")
    
    # v2.2
    total_pnl_v22 = sum(r['pnl'] for r in results_v22)
    win_v22 = sum(1 for r in results_v22 if r['pnl'] > 0)
    loss_v22 = sum(1 for r in results_v22 if r['pnl'] < 0)
    holding_v22 = sum(1 for r in results_v22 if r['reason'] == 'holding')
    tp2_v22 = sum(1 for r in results_v22 if r['reason'] == 'tp2')
    sl_v22 = sum(1 for r in results_v22 if r['reason'] == 'stop_loss')
    
    # v2.5
    total_pnl_v25 = sum(r['pnl'] for r in results_v25)
    win_v25 = sum(1 for r in results_v25 if r['pnl'] > 0)
    loss_v25 = sum(1 for r in results_v25 if r['pnl'] < 0)
    holding_v25 = sum(1 for r in results_v25 if r['reason'] == 'holding')
    tp2_v25 = sum(1 for r in results_v25 if r['reason'] == 'tp2')
    sl_v25 = sum(1 for r in results_v25 if r['reason'] == 'stop_loss')
    
    print(f"""
┌─────────────────────────────────────────────────────────────────┐
│  指标              │  v2.2        │  v2.5        │  差异       │
├─────────────────────────────────────────────────────────────────┤
│  开仓笔数          │  {len(results_v22):>3}          │  {len(results_v25):>3}          │  {len(results_v25)-len(results_v22):>+#3}        │
│  总盈亏            │  ${total_pnl_v22:>+8.1f}U   │  ${total_pnl_v25:>+8.1f}U   │  ${total_pnl_v25-total_pnl_v22:>+8.1f}U   │
│  平均盈亏/笔       │  ${total_pnl_v22/max(1,len(results_v22)):>+6.1f}U    │  ${total_pnl_v25/max(1,len(results_v25)):>+6.1f}U    │  ${total_pnl_v25/max(1,len(results_v25))-total_pnl_v22/max(1,len(results_v22)):>+6.1f}U    │
│  胜率              │  {win_v22/max(1,len(results_v22))*100:>5.1f}%       │  {win_v25/max(1,len(results_v25))*100:>5.1f}%       │  {(win_v25/max(1,len(results_v25))-win_v22/max(1,len(results_v22)))*100:>+5.1f}%      │
│  止盈 (TP2)        │  {tp2_v22:>3}          │  {tp2_v25:>3}          │  {tp2_v25-tp2_v22:>+#3}        │
│  止损 (SL)         │  {sl_v22:>3}          │  {sl_v25:>3}          │  {sl_v25-sl_v22:>+#3}        │
│  持仓中            │  {holding_v22:>3}          │  {holding_v25:>3}          │  {holding_v25-holding_v22:>+#3}        │
└─────────────────────────────────────────────────────────────────┘
""")
    
    # 详细盈亏分布
    print(f"\n{'='*100}")
    print("盈亏分布对比")
    print(f"{'='*100}")
    
    def pnl_distribution(results):
        large_win = sum(1 for r in results if r['pnl'] > 50)
        small_win = sum(1 for r in results if 0 < r['pnl'] <= 50)
        small_loss = sum(1 for r in results if -20 <= r['pnl'] < 0)
        large_loss = sum(1 for r in results if r['pnl'] < -20)
        return large_win, small_win, small_loss, large_loss
    
    lw_v22, sw_v22, sl_v22_dist, ll_v22 = pnl_distribution(results_v22)
    lw_v25, sw_v25, sl_v25_dist, ll_v25 = pnl_distribution(results_v25)
    
    print(f"""
v2.2 盈亏分布:
  大赢 (>$50):  {lw_v22}笔
  小赢 ($0-50): {sw_v22}笔
  小亏 ($-20-0): {sl_v22_dist}笔
  大亏 (<$-20): {ll_v22}笔

v2.5 盈亏分布:
  大赢 (>$50):  {lw_v25}笔
  小赢 ($0-50): {sw_v25}笔
  小亏 ($-20-0): {sl_v25_dist}笔
  大亏 (<$-20): {ll_v25}笔
""")
    
    # 逐笔对比（前 20 笔）
    print(f"\n{'='*100}")
    print("逐笔盈亏对比（前 20 笔）")
    print(f"{'='*100}")
    print(f"{'标的':<15} {'v2.2 评分':<10} {'v2.2 盈亏':<12} {'v2.5 评分':<10} {'v2.5 盈亏':<12} {'状态':<10}")
    print("-"*80)
    
    all_symbols = set([r['symbol'] for r in results_v22] + [r['symbol'] for r in results_v25])
    for sym in list(all_symbols)[:20]:
        r_v22 = next((r for r in results_v22 if r['symbol']==sym), None)
        r_v25 = next((r for r in results_v25 if r['symbol']==sym), None)
        
        s22 = f"{r_v22['score']:.0f}分" if r_v22 else '-'
        pnl22 = f"${r_v22['pnl']:+.1f}U" if r_v22 else '-'
        s25 = f"{r_v25['score']:.0f}分" if r_v25 else '-'
        pnl25 = f"${r_v25['pnl']:+.1f}U" if r_v25 else '-'
        
        status_v22 = '✅' if r_v22 and r_v22['pnl']>0 else ('❌' if r_v22 and r_v22['pnl']<0 else '-')
        status_v25 = '✅' if r_v25 and r_v25['pnl']>0 else ('❌' if r_v25 and r_v25['pnl']<0 else '-')
        
        print(f"{sym:<15} {s22:<10} {pnl22:<12} {s25:<10} {pnl25:<12} {status_v22+status_v25:<10}")
    
    # 结论
    print(f"\n{'='*100}")
    print("结论")
    print(f"{'='*100}")
    
    if total_pnl_v22 > 0 and total_pnl_v25 > 0:
        conclusion = "✅ 两个版本都盈利"
    elif total_pnl_v22 < 0 and total_pnl_v25 < 0:
        conclusion = "❌ 两个版本都亏损"
    elif total_pnl_v22 > 0:
        conclusion = "⚠️ v2.2 盈利，v2.5 亏损"
    else:
        conclusion = "⚠️ v2.5 盈利，v2.2 亏损"
    
    print(f"""
{conclusion}

v2.2 总盈亏：${total_pnl_v22:+.1f}U ({len(results_v22)}笔，平均${total_pnl_v22/max(1,len(results_v22)):+.1f}U/笔)
v2.5 总盈亏：${total_pnl_v25:+.1f}U ({len(results_v25)}笔，平均${total_pnl_v25/max(1,len(results_v25)):+.1f}U/笔)

差异：${total_pnl_v25-total_pnl_v22:+.1f}U

建议：
""")
    
    if total_pnl_v25 > total_pnl_v22:
        print("  ✅ v2.5 收益更优，建议采用 v2.5")
    elif total_pnl_v22 > total_pnl_v25:
        print("  ⚠️ v2.2 收益更优，但开仓过多（{0}笔 vs v2.5 的{1}笔）".format(len(results_v22), len(results_v25)))
        print("  建议：考虑 v2.5 微调，平衡收益和开仓频率")
    
    print(f"\n{'='*100}")
    print(f"模拟完成：{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*100}")

if __name__ == '__main__':
    run_comparison()
