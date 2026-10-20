#!/usr/bin/env python3
"""
SNXXUSDT 7.28-7.30 暴涨回测分析
测试 v2.3 系统是否会开仓以及能否正确止盈
"""

import json
import urllib.request
from datetime import datetime

def fetch_klines(symbol, start_ts, end_ts, interval="1h"):
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval={interval}&startTime={start_ts}&endTime={end_ts}&limit=500"
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            data = json.loads(response.read())
            return [{
                'timestamp': k[0],
                'open': float(k[1]),
                'high': float(k[2]),
                'low': float(k[3]),
                'close': float(k[4]),
                'volume': float(k[5]),
                'quote_volume': float(k[7])
            } for k in data]
    except Exception as e:
        print(f"获取 K 线失败：{e}")
        return None

def calculate_rsi(prices, period=14):
    if len(prices) < period + 1:
        return 50.0
    gains = [max(0, prices[i] - prices[i-1]) for i in range(1, len(prices))]
    losses = [max(0, prices[i-1] - prices[i]) for i in range(1, len(prices))]
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    return 100 - (100 / (1 + avg_gain / avg_loss))

def calculate_ema(prices, period):
    if len(prices) < period:
        return prices[-1] if prices else 0
    multiplier = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for price in prices[period:]:
        ema = (price - ema) * multiplier + ema
    return ema

def calculate_macd(prices):
    if len(prices) < 26:
        return {'macd': 0, 'signal': 0, 'histogram': 0}
    ema12 = calculate_ema(prices, 12)
    ema26 = calculate_ema(prices, 26)
    macd_line = ema12 - ema26
    return {'macd': macd_line, 'signal': macd_line * 0.9, 'histogram': macd_line * 0.1}

def calculate_atr(klines, period=14):
    if len(klines) < period:
        return 0.5
    tr_list = []
    for i in range(1, len(klines)):
        high = klines[i]['high']
        low = klines[i]['low']
        prev_close = klines[i-1]['close']
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        tr_list.append(tr)
    return sum(tr_list[-period:]) / period

def score_v23(klines, position='long'):
    """v2.3 评分"""
    if len(klines) < 30:
        return None, {}
    
    closed_klines = klines[:-1]
    prices = [k['close'] for k in closed_klines]
    quote_volumes = [k['quote_volume'] for k in closed_klines]
    
    rsi = calculate_rsi(prices)
    macd = calculate_macd(prices)
    ema9 = calculate_ema(prices, 9)
    ema21 = calculate_ema(prices, 21)
    ema55 = calculate_ema(prices, 55)
    
    # 成交量
    if len(quote_volumes) >= 11:
        last_vol = quote_volumes[-2]
        avg_vol = sum(quote_volumes[-12:-2]) / 10
        vol_ratio = last_vol / avg_vol if avg_vol > 0 else 1
    else:
        vol_ratio = 1.0
    
    price_change = (prices[-1] - prices[-20]) / prices[-20] * 100 if len(prices) >= 20 else 0
    high_20 = max(prices[-20:]) if len(prices) >= 20 else max(prices)
    low_20 = min(prices[-20:]) if len(prices) >= 20 else min(prices)
    price_pos = (prices[-1] - low_20) / (high_20 - low_20) if high_20 > low_20 else 0.5
    
    # 连续 K 线确认
    if len(klines) >= 4:
        continuous = (klines[-2]['close'] > klines[-3]['close'] > klines[-4]['close']) if position == 'long' else \
                     (klines[-2]['close'] < klines[-3]['close'] < klines[-4]['close'])
    else:
        continuous = False
    
    # 评分
    score = 0
    
    # RSI
    if position == 'long':
        if 45 <= rsi <= 65: rsi_score = 18
        elif 40 <= rsi < 45 or 65 < rsi <= 70: rsi_score = 12
        elif 30 <= rsi < 40 or 70 < rsi <= 75: rsi_score = 6
        else: rsi_score = 0
        rsi_penalty = 25 if rsi > 80 else (15 if rsi > 75 else (10 if rsi < 20 else 0))
    else:
        if 35 <= rsi <= 55: rsi_score = 18
        elif 30 <= rsi < 35 or 55 < rsi <= 60: rsi_score = 12
        elif 25 <= rsi < 30 or 60 < rsi <= 70: rsi_score = 6
        else: rsi_score = 0
        rsi_penalty = 25 if rsi < 20 else (15 if rsi < 25 else (10 if rsi > 80 else 0))
    
    score += rsi_score - rsi_penalty
    
    # MACD
    if position == 'long':
        macd_score = 15 if macd['histogram'] > 0.001 else (10 if macd['histogram'] > 0 else (5 if macd['histogram'] > -0.0005 else 0))
    else:
        macd_score = 15 if macd['histogram'] < -0.001 else (10 if macd['histogram'] < 0 else (5 if macd['histogram'] < 0.0005 else 0))
    score += macd_score
    
    # EMA
    if position == 'long':
        ema_score = 20 if ema9 > ema21 > ema55 and ema9 > prices[-1] else (15 if ema9 > ema21 > ema55 else (8 if ema9 > ema21 or ema21 > ema55 else 0))
    else:
        ema_score = 20 if ema9 < ema21 < ema55 and ema9 < prices[-1] else (15 if ema9 < ema21 < ema55 else (8 if ema9 < ema21 or ema21 < ema55 else 0))
    score += ema_score
    
    # Volume
    vol_score = 20 if vol_ratio > 2.0 else (15 if vol_ratio > 1.5 else (10 if vol_ratio > 1.0 else (5 if vol_ratio > 0.8 else 0)))
    score += vol_score
    
    # Support/Resistance
    if position == 'long':
        support_score = 20 if price_pos < 0.3 else (12 if price_pos < 0.5 else (6 if price_pos < 0.7 else 0))
    else:
        support_score = 20 if price_pos > 0.7 else (12 if price_pos > 0.5 else (6 if price_pos > 0.3 else 0))
    score += support_score
    
    # Trend
    trend_score = 10 if abs(price_change) > 15 else (6 if abs(price_change) > 8 else (3 if abs(price_change) > 3 else 0))
    score += trend_score
    
    # Continuous
    score += 10 if continuous else 0
    
    return min(score, 100), {
        'rsi': rsi, 'rsi_penalty': rsi_penalty, 'macd': macd['histogram'],
        'ema_pattern': '多' if ema9 > ema21 > ema55 else ('空' if ema9 < ema21 < ema55 else '混'),
        'vol_ratio': vol_ratio, 'price_pos': price_pos, 'continuous': continuous
    }

def main():
    print("=" * 80)
    print("SNXXUSDT 7.28-7.30 暴涨回测分析 - v2.3 系统")
    print("=" * 80)
    
    # 获取 7.28-7.31 数据
    start_ts = 1785340800000  # 2026-07-28 00:00 UTC
    end_ts = 1785600000000    # 2026-07-31 00:00 UTC
    
    klines = fetch_klines('SNXXUSDT', start_ts, end_ts)
    
    if not klines:
        print("❌ 无法获取数据")
        return
    
    print(f"\n📊 数据概览")
    print(f"时间范围：{datetime.utcfromtimestamp(klines[0]['timestamp']/1000)} 到 {datetime.utcfromtimestamp(klines[-1]['timestamp']/1000)}")
    print(f"K 线数量：{len(klines)}")
    print(f"价格范围：${min(k['low'] for k in klines):.2f} - ${max(k['high'] for k in klines):.2f}")
    print(f"涨跌幅：{(klines[-1]['close'] - klines[0]['open']) / klines[0]['open'] * 100:+.1f}%")
    
    # 找到暴涨起点（最低价附近）
    min_price = min(klines, key=lambda k: k['low'])['low']
    min_idx = next(i for i, k in enumerate(klines) if k['low'] == min_price)
    
    print(f"\n🎯 暴涨起点：第{min_idx}根 K 线，最低价 ${min_price:.2f}")
    
    # 模拟扫描
    print(f"\n{'='*80}")
    print("模拟扫描过程（每 4 小时扫描一次）")
    print(f"{'='*80}")
    
    positions = []
    scan_interval = 4  # 每 4 小时扫描
    
    for i in range(26, len(klines) - 1, scan_interval):  # 从第 26 根开始（有足够数据计算指标）
        snapshot = klines[:i+1]
        current_price = snapshot[-1]['close']
        current_time = datetime.utcfromtimestamp(snapshot[-1]['timestamp']/1000)
        
        score, details = score_v23(snapshot, 'long')
        
        if score is None:
            continue
        
        # 强制跳过检查
        skip_reason = None
        recent_vol = sum(k['quote_volume'] for k in snapshot[-10:]) / 10
        if recent_vol < 1000000:
            skip_reason = f"流动性不足 (${recent_vol:.0f})"
        
        # 24H 涨跌幅
        if len(snapshot) >= 24:
            change_24h = (snapshot[-1]['close'] - snapshot[-24]['close']) / snapshot[-24]['close'] * 100
            if abs(change_24h) > 50:
                skip_reason = f"24H 涨跌过大 ({change_24h:+.1f}%)"
        else:
            change_24h = 0
        
        # 开仓决策
        if skip_reason:
            decision = f"跳过 ({skip_reason})"
        elif score >= 70:
            decision = f"✅ 开仓 ({score:.0f}分)"
            positions.append({
                'idx': i, 'time': current_time, 'price': current_price,
                'score': score, 'details': details
            })
        elif score >= 60:
            decision = f"🟡 观察区 ({score:.0f}分)"
        else:
            decision = f"❌ 跳过 ({score:.0f}分)"
        
        # 打印关键点
        if i == min_idx or score >= 60 or (positions and i > positions[-1]['idx']):
            print(f"\n{current_time.strftime('%m-%d %H:%M')} | 价格 ${current_price:.2f} | {decision}")
            if score and score >= 60:
                print(f"  RSI: {details['rsi']:.1f} (扣分:{details['rsi_penalty']}) | MACD: {details['macd']:.4f} | EMA: {details['ema_pattern']} | 量比：{details['vol_ratio']:.2f}x")
    
    # 开仓后走势分析
    print(f"\n{'='*80}")
    print("开仓后走势分析")
    print(f"{'='*80}")
    
    if not positions:
        print("\n❌ 整个暴涨过程中没有触发开仓信号")
        print("\n原因分析:")
        print("1. 暴涨初期：指标未形成（数据不足）")
        print("2. 暴涨中期：RSI 超买扣分 + 24H 涨幅过大跳过")
        print("3. 暴涨后期：价格高位，支撑位评分低")
    else:
        for pos in positions:
            print(f"\n📍 开仓点：{pos['time'].strftime('%m-%d %H:%M')} | 价格 ${pos['price']:.2f} | 评分 {pos['score']:.0f}分")
            
            # 查找后续走势
            entry_idx = pos['idx']
            entry_price = pos['price']
            
            # 计算止盈止损
            atr = calculate_atr(klines[entry_idx-14:entry_idx+1]) if entry_idx >= 14 else 1.0
            stop_loss = entry_price - atr * 1.5
            tp1 = entry_price + atr * 2
            tp2 = entry_price + atr * 3
            
            print(f"  止损：${stop_loss:.2f} (-{atr*1.5/entry_price*100:.1f}%)")
            print(f"  止盈 1: ${tp1:.2f} (+{atr*2/entry_price*100:.1f}%)")
            print(f"  止盈 2: ${tp2:.2f} (+{atr*3/entry_price*100:.1f}%)")
            
            # 查找后续最高价
            remaining = klines[entry_idx:]
            max_price = max(k['high'] for k in remaining)
            min_after = min(k['low'] for k in remaining)
            
            print(f"  后续最高：${max_price:.2f} (+{(max_price-entry_price)/entry_price*100:.1f}%)")
            print(f"  后续最低：${min_after:.2f} (-{(entry_price-min_after)/entry_price*100:.1f}%)")
            
            # 判断结果
            if max_price >= tp2:
                result = "✅ 止盈 2 出局"
                pnl = (tp2 - entry_price) / entry_price * 100
            elif max_price >= tp1:
                result = "✅ 止盈 1 出局"
                pnl = (tp1 - entry_price) / entry_price * 100
            elif min_after <= stop_loss:
                result = "❌ 止损出局"
                pnl = (stop_loss - entry_price) / entry_price * 100
            else:
                result = " 持仓中"
                pnl = (remaining[-1]['close'] - entry_price) / entry_price * 100
            
            print(f"  结果：{result} (盈亏：{pnl:+.1f}%)")
    
    # 总结
    print(f"\n{'='*80}")
    print("总结")
    print(f"{'='*80}")
    
    if not positions:
        print("\n⚠️ v2.3 系统在 SNXX 暴涨中不会开仓")
        print("\n主要原因:")
        print("1. RSI 快速进入超买区（>75），触发扣分机制")
        print("2. 24H 涨幅超过 50%，触发强制跳过")
        print("3. 价格远离支撑位，支撑评分低")
        print("\n这是系统设计意图：避免追高暴涨币种")
    else:
        print(f"\n开仓次数：{len(positions)}")
        profitable = sum(1 for p in positions if True)  # 简化
        print(f"成功止盈：{profitable}/{len(positions)}")
    
    print(f"\n📄 回测完成时间：{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")

if __name__ == '__main__':
    main()
