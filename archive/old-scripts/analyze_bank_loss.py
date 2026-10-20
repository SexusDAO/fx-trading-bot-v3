#!/usr/bin/env python3
"""
分析 BANKUSDT 亏损原因
检查：1. 价格走势 2. 信号方向 3. 是否应该做空
"""

import json
import urllib.request
from datetime import datetime, timedelta

def fetch_30m_klines(symbol, start_ts, end_ts):
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

def main():
    print("="*100)
    print("BANKUSDT 亏损分析")
    print("="*100)
    
    # 获取 7 天数据
    end_ts = int(datetime.utcnow().timestamp() * 1000)
    start_ts = int((datetime.utcnow() - timedelta(days=7)).timestamp() * 1000)
    
    klines = fetch_30m_klines('BANKUSDT', start_ts, end_ts)
    if not klines:
        print("❌ 无法获取数据")
        return
    
    print(f"\n📅 时间范围：{datetime.utcfromtimestamp(klines[0]['timestamp']/1000)} 到 {datetime.utcfromtimestamp(klines[-1]['timestamp']/1000)}")
    print(f"📊 K 线数量：{len(klines)} 根")
    
    # 价格走势
    start_price = klines[0]['close']
    end_price = klines[-1]['close']
    high_price = max(k['high'] for k in klines)
    low_price = min(k['low'] for k in klines)
    
    print(f"\n📈 价格走势：")
    print(f"   开盘：${start_price:.4f}")
    print(f"   收盘：${end_price:.4f}")
    print(f"   最高：${high_price:.4f}")
    print(f"   最低：${low_price:.4f}")
    print(f"   7 天涨跌：{(end_price-start_price)/start_price*100:+.2f}%")
    
    # 分段分析
    print(f"\n📊 分段价格走势：")
    chunk_size = len(klines) // 7  # 每天
    for i in range(7):
        chunk = klines[i*chunk_size:(i+1)*chunk_size]
        if chunk:
            chunk_start = chunk[0]['close']
            chunk_end = chunk[-1]['close']
            day_change = (chunk_end - chunk_start) / chunk_start * 100
            day = datetime.utcfromtimestamp(chunk[0]['timestamp']/1000)
            print(f"   Day {i+1} ({day.strftime('%m-%d')}): ${chunk_start:.4f} → ${chunk_end:.4f}  {day_change:>+6.2f}%")
    
    # 模拟做多 vs 做空
    print(f"\n{'='*100}")
    print("做多 vs 做空 对比测试")
    print(f"{'='*100}")
    
    long_wins = 0
    long_losses = 0
    long_pnl = 0
    
    short_wins = 0
    short_losses = 0
    short_pnl = 0
    
    # 简单策略：RSI<30 做多，RSI>70 做空
    for i in range(30, len(klines)-50, 6):
        snapshot = klines[:i+1]
        future = klines[i+1:i+51]
        prices = [k['close'] for k in snapshot]
        rsi = calculate_rsi(prices)
        
        entry = snapshot[-1]['close']
        
        # 做多信号
        if rsi < 30:
            # 检查后续 50 根 K 线
            best_price = max(k['high'] for k in future)
            worst_price = min(k['low'] for k in future)
            
            # 止盈 2%，止损 2%
            tp = entry * 1.02
            sl = entry * 0.98
            
            if worst_price <= sl:
                pnl = -2  # 止损
                long_losses += 1
            elif best_price >= tp:
                pnl = 2  # 止盈
                long_wins += 1
            else:
                pnl = (future[-1]['close'] - entry) / entry * 100
                if pnl > 0:
                    long_wins += 1
                else:
                    long_losses += 1
            long_pnl += pnl
        
        # 做空信号
        if rsi > 70:
            # 检查后续 50 根 K 线
            best_price = min(k['low'] for k in future)
            worst_price = max(k['high'] for k in future)
            
            # 止盈 2%，止损 2%
            tp = entry * 0.98
            sl = entry * 1.02
            
            if worst_price >= sl:
                pnl = -2  # 止损
                short_losses += 1
            elif best_price <= tp:
                pnl = 2  # 止盈
                short_wins += 1
            else:
                pnl = (entry - future[-1]['close']) / entry * 100
                if pnl > 0:
                    short_wins += 1
                else:
                    short_losses += 1
            short_pnl += pnl
    
    print(f"""
┌────────────────────────────────────────────────────────────────┐
│  策略          │  交易次数  │  盈利  │  亏损  │  胜率  │  总盈亏  │
├────────────────────────────────────────────────────────────────┤
│  做多 (RSI<30)  │  {long_wins+long_losses:>6}     │ {long_wins:>4}   │ {long_losses:>4}   │ {long_wins/max(1,long_wins+long_losses)*100:>5.1f}%  │ {long_pnl:>+6.2f}%  │
│  做空 (RSI>70)  │  {short_wins+short_losses:>6}     │ {short_wins:>4}   │ {short_losses:>4}   │ {short_wins/max(1,short_wins+short_losses)*100:>5.1f}%  │ {short_pnl:>+6.2f}%  │
└────────────────────────────────────────────────────────────────┘
""")
    
    # 结论
    print(f"\n{'='*100}")
    print("🔍 亏损原因分析")
    print(f"{'='*100}")
    
    if (end_price - start_price) / start_price < -0.05:
        print(f"\n⚠️ **主要原因：BANKUSDT 7 天下跌 {(end_price-start_price)/start_price*100:.1f}%**")
        print(f"   → 做多策略在下跌趋势中天然劣势")
        print(f"   → 如果做空，可能盈利 {short_pnl:+.2f}%")
    
    if short_pnl > long_pnl:
        print(f"\n✅ **做空表现更好**：做空盈亏 {short_pnl:+.2f}% vs 做多 {long_pnl:+.2f}%")
        print(f"   → 建议：增加做空机制")
    
    print(f"\n💡 优化建议：")
    print(f"   1. 增加做空信号（RSI>70 或 EMA 空头排列）")
    print(f"   2. 趋势过滤：下跌趋势中优先做空，上涨趋势中优先做多")
    print(f"   3. BANKUSDT 这种持续下跌的币种，应该以做空为主")
    
    print(f"\n{'='*100}")
    print(f"分析完成：{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*100}")

if __name__ == '__main__':
    main()
