#!/usr/bin/env python3
"""
评分系统 v2.3 - 修正版
- 基础分归零，纯加分制
- RSI 超买超卖扣分
- 成交量使用已收盘 K 线
- 连续 K 线确认
"""

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
    signal_line = macd_line * 0.9
    return {'macd': macd_line, 'signal': signal_line, 'histogram': macd_line - signal_line}

def detect_divergence(klines):
    if len(klines) < 20:
        return 'none'
    prices = [k['close'] for k in klines[-20:]]
    macd_hist = [calculate_macd(prices[:i+1])['histogram'] for i in range(len(prices))]
    for i in range(5, len(prices) - 3):
        if prices[i] < min(prices[i-3:i]) and prices[i] < min(prices[i+1:i+4]) and macd_hist[i] > macd_hist[i-3]:
            return 'bullish'
        if prices[i] > max(prices[i-3:i]) and prices[i] > max(prices[i+1:i+4]) and macd_hist[i] < macd_hist[i-3]:
            return 'bearish'
    return 'none'

def check_continuous_confirmation(klines, direction):
    """
    检查连续 2 根 K 线同方向确认
    direction: 'long' 或 'short'
    """
    if len(klines) < 3:
        return False
    
    # 检查最后 2 根已收盘 K 线
    last_close = klines[-2]['close']
    prev_close = klines[-3]['close']
    prev_prev_close = klines[-4]['close'] if len(klines) > 4 else klines[-3]['open']
    
    if direction == 'long':
        # 多单需要连续上涨
        return last_close > prev_close and prev_close > prev_prev_close
    else:
        # 空单需要连续下跌
        return last_close < prev_close and prev_close < prev_prev_close

def score_symbol_v23(klines, ticker, position='long'):
    """
    v2.3 评分系统 - 纯加分制，基础分 0
    position: 'long' 或 'short'
    """
    if not klines or len(klines) < 30:
        return None
    
    # 使用已收盘 K 线（排除当前未走完的）
    closed_klines = klines[:-1]
    prices = [k['close'] for k in closed_klines]
    quote_volumes = [k['quote_volume'] for k in closed_klines]
    
    # 计算指标
    rsi = calculate_rsi(prices)
    macd = calculate_macd(prices)
    ema9, ema21, ema55 = calculate_ema(prices, 9), calculate_ema(prices, 21), calculate_ema(prices, 55)
    
    # 成交量：前一根 K 线 / 前 10 根均量
    if len(quote_volumes) >= 11:
        last_vol = quote_volumes[-2]  # 前一根已收盘
        avg_vol = sum(quote_volumes[-12:-2]) / 10  # 前 10 根均量
        vol_ratio = last_vol / avg_vol if avg_vol > 0 else 1
    else:
        vol_ratio = 1.0
    
    price_change = (prices[-1] - prices[-20]) / prices[-20] * 100
    divergence = detect_divergence(klines)
    
    # 连续 K 线确认
    continuous_confirmed = check_continuous_confirmation(klines, position)
    
    # ===== 评分开始（基础分 0）=====
    score = 0
    details = {}
    
    # 1. RSI 评分（最高 18 分）
    if position == 'long':
        if 45 <= rsi <= 65:
            rsi_score = 18
        elif 40 <= rsi < 45 or 65 < rsi <= 70:
            rsi_score = 12
        elif 30 <= rsi < 40 or 70 < rsi <= 75:
            rsi_score = 6
        else:
            rsi_score = 0
    else:  # short
        if 35 <= rsi <= 55:
            rsi_score = 18
        elif 30 <= rsi < 35 or 55 < rsi <= 60:
            rsi_score = 12
        elif 25 <= rsi < 30 or 60 < rsi <= 70:
            rsi_score = 6
        else:
            rsi_score = 0
    
    # RSI 扣分机制
    rsi_penalty = 0
    if position == 'long':
        if rsi > 80:
            rsi_penalty = 25
        elif rsi > 75:
            rsi_penalty = 15
        elif rsi < 20:
            rsi_penalty = 10
    else:  # short
        if rsi < 20:
            rsi_penalty = 25
        elif rsi < 25:
            rsi_penalty = 15
        elif rsi > 80:
            rsi_penalty = 10
    
    details['rsi'] = rsi
    details['rsi_score'] = rsi_score
    details['rsi_penalty'] = rsi_penalty
    score += rsi_score - rsi_penalty
    
    # 2. MACD 评分（最高 15 分）
    if position == 'long':
        if macd['histogram'] > 0.001:
            macd_score = 15
        elif macd['histogram'] > 0:
            macd_score = 10
        elif macd['histogram'] > -0.0005:
            macd_score = 5
        else:
            macd_score = 0
    else:  # short
        if macd['histogram'] < -0.001:
            macd_score = 15
        elif macd['histogram'] < 0:
            macd_score = 10
        elif macd['histogram'] < 0.0005:
            macd_score = 5
        else:
            macd_score = 0
    
    details['macd'] = macd
    details['macd_score'] = macd_score
    score += macd_score
    
    # 3. EMA 排列（最高 20 分）
    if position == 'long':
        if ema9 > ema21 > ema55 and ema9 > prices[-1]:
            ema_score = 20
        elif ema9 > ema21 > ema55:
            ema_score = 15
        elif ema9 > ema21 or ema21 > ema55:
            ema_score = 8
        else:
            ema_score = 0
    else:  # short
        if ema9 < ema21 < ema55 and ema9 < prices[-1]:
            ema_score = 20
        elif ema9 < ema21 < ema55:
            ema_score = 15
        elif ema9 < ema21 or ema21 < ema55:
            ema_score = 8
        else:
            ema_score = 0
    
    details['ema9'] = ema9
    details['ema21'] = ema21
    details['ema55'] = ema55
    if ema9 > ema21 > ema55:
        details['ema_pattern'] = '多头'
    elif ema9 < ema21 < ema55:
        details['ema_pattern'] = '空头'
    else:
        details['ema_pattern'] = '混乱'
    details['ema_score'] = ema_score
    score += ema_score
    
    # 4. 成交量评分（最高 20 分）
    if vol_ratio > 2.0:
        vol_score = 20
    elif vol_ratio > 1.5:
        vol_score = 15
    elif vol_ratio > 1.0:
        vol_score = 10
    elif vol_ratio > 0.8:
        vol_score = 5
    else:
        vol_score = 0
    
    details['vol_ratio'] = vol_ratio
    details['vol_score'] = vol_score
    score += vol_score
    
    # 5. 支撑/阻力位评分（最高 20 分）
    # 简化：用价格位置判断
    high_20 = max(prices[-20:])
    low_20 = min(prices[-20:])
    price_pos = (prices[-1] - low_20) / (high_20 - low_20) if high_20 > low_20 else 0.5
    
    if position == 'long':
        if price_pos < 0.3:  # 接近区间底部
            support_score = 20
        elif price_pos < 0.5:
            support_score = 12
        elif price_pos < 0.7:
            support_score = 6
        else:
            support_score = 0
    else:  # short
        if price_pos > 0.7:  # 接近区间顶部
            support_score = 20
        elif price_pos > 0.5:
            support_score = 12
        elif price_pos > 0.3:
            support_score = 6
        else:
            support_score = 0
    
    details['price_pos'] = price_pos
    details['support_score'] = support_score
    score += support_score
    
    # 6. 背离加分（最高 20 分）
    if divergence == 'bullish' and position == 'long':
        div_score = 20
    elif divergence == 'bearish' and position == 'short':
        div_score = 20
    elif divergence != 'none':
        div_score = 10
    else:
        div_score = 5
    
    details['divergence'] = divergence
    details['div_score'] = div_score
    score += div_score
    
    # 7. 趋势强度（最高 10 分）
    if abs(price_change) > 15:
        trend_score = 10
    elif abs(price_change) > 8:
        trend_score = 6
    elif abs(price_change) > 3:
        trend_score = 3
    else:
        trend_score = 0
    
    details['price_change'] = price_change
    details['trend_score'] = trend_score
    score += trend_score
    
    # 8. 连续 K 线确认加分（10 分）
    if continuous_confirmed:
        confirm_score = 10
    else:
        confirm_score = 0
    
    details['continuous_confirmed'] = continuous_confirmed
    details['confirm_score'] = confirm_score
    score += confirm_score
    
    # 上限 100 分
    final_score = min(score, 100)
    
    return {
        'raw_score': score,
        'final_score': final_score,
        'details': details
    }
