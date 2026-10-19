#!/usr/bin/env python3
"""
评分系统 v2.4 - 校准版
在 v2.3 基础上上调各维度分值，让正常信号达到 65-70 分
同时保持 RSI 超买扣分等严格机制
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

def count_consecutive_klines(klines, direction):
    """
    计算连续同向 K 线数量
    direction: 'long' 或 'short'
    """
    if len(klines) < 2:
        return 0
    
    count = 0
    for i in range(len(klines)-2, -1, -1):
        curr_close = klines[i]['close']
        prev_close = klines[i+1]['close'] if i+1 < len(klines) else klines[i]['open']
        
        if direction == 'long':
            if curr_close > prev_close:
                count += 1
            else:
                break
        else:
            if curr_close < prev_close:
                count += 1
            else:
                break
    
    return min(count, 3)  # 最多计 3 根

def score_v24(klines, ticker=None, position='long', is_low_cap=False):
    """
    v2.4 评分系统 - 校准版
    - 基础分 0，纯加分制
    - 各维度分值上调
    - RSI 超买超卖扣分保留
    - 连续 K 线放宽
    """
    if not klines or len(klines) < 30:
        return None, {}
    
    # 使用已收盘 K 线（排除当前未走完的）
    closed_klines = klines[:-1]
    prices = [k['close'] for k in closed_klines]
    quote_volumes = [k['quote_volume'] for k in closed_klines]
    
    # 计算指标
    rsi = calculate_rsi(prices)
    macd = calculate_macd(prices)
    ema9 = calculate_ema(prices, 9)
    ema21 = calculate_ema(prices, 21)
    ema55 = calculate_ema(prices, 55)
    
    # 成交量：前一根 K 线 / 前 10 根均量
    if len(quote_volumes) >= 11:
        last_vol = quote_volumes[-2]  # 前一根已收盘
        avg_vol = sum(quote_volumes[-12:-2]) / 10  # 前 10 根均量
        vol_ratio = last_vol / avg_vol if avg_vol > 0 else 1
    else:
        vol_ratio = 1.0
    
    # 价格变化
    price_change = (prices[-1] - prices[-20]) / prices[-20] * 100 if len(prices) >= 20 else 0
    
    # 支撑/阻力位：统计过去 20 根 K 线的价格触及次数
    high_20 = max(prices[-20:])
    low_20 = min(prices[-20:])
    price_pos = (prices[-1] - low_20) / (high_20 - low_20) if high_20 > low_20 else 0.5
    
    # 简化支撑位强度：统计价格接近低点的次数
    support_touches = sum(1 for p in prices[-20:] if (p - low_20) / (high_20 - low_20) < 0.15) if high_20 > low_20 else 0
    
    # 连续 K 线确认
    consecutive = count_consecutive_klines(klines, position)
    
    # ===== 评分开始（基础分 0）=====
    score = 0
    details = {}
    
    # 1. RSI 评分（最高 22 分，v2.3 是 18 分）
    if position == 'long':
        if 45 <= rsi <= 65:
            rsi_score = 22
        elif 40 <= rsi < 45 or 65 < rsi <= 70:
            rsi_score = 14
        elif 30 <= rsi < 40 or 70 < rsi <= 75:
            rsi_score = 8
        else:
            rsi_score = 0
        # RSI 扣分机制（保留 v2.3）
        rsi_penalty = 25 if rsi > 80 else (15 if rsi > 75 else (10 if rsi < 20 else 0))
    else:  # short
        if 35 <= rsi <= 55:
            rsi_score = 22
        elif 30 <= rsi < 35 or 55 < rsi <= 60:
            rsi_score = 14
        elif 25 <= rsi < 30 or 60 < rsi <= 70:
            rsi_score = 8
        else:
            rsi_score = 0
        rsi_penalty = 25 if rsi < 20 else (15 if rsi < 25 else (10 if rsi > 80 else 0))
    
    details['rsi'] = rsi
    details['rsi_score'] = rsi_score
    details['rsi_penalty'] = rsi_penalty
    score += rsi_score - rsi_penalty
    
    # 2. MACD 评分（最高 18 分，v2.3 是 15 分）
    if position == 'long':
        if macd['histogram'] > 0.001:
            macd_score = 18
        elif macd['histogram'] > 0:
            macd_score = 12
        elif macd['histogram'] > -0.0005:
            macd_score = 6
        else:
            macd_score = 0
    else:  # short
        if macd['histogram'] < -0.001:
            macd_score = 18
        elif macd['histogram'] < 0:
            macd_score = 12
        elif macd['histogram'] < 0.0005:
            macd_score = 6
        else:
            macd_score = 0
    
    details['macd'] = macd['histogram']
    details['macd_score'] = macd_score
    score += macd_score
    
    # 3. EMA 排列（最高 20 分，不变）
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
    
    # 4. 成交量评分（最高 22 分，v2.3 是 20 分）
    if vol_ratio > 2.0:
        vol_score = 22
    elif vol_ratio > 1.5:
        vol_score = 16  # v2.3 是 13 分
    elif vol_ratio > 1.0:
        vol_score = 12  # v2.3 是 10 分
    elif vol_ratio > 0.8:
        vol_score = 6
    else:
        vol_score = 0
    
    details['vol_ratio'] = vol_ratio
    details['vol_score'] = vol_score
    score += vol_score
    
    # 5. 支撑/阻力位评分（最高 20 分，中位从 14→16 分）
    if position == 'long':
        if price_pos < 0.3 or support_touches >= 3:  # 接近底部或强支撑
            support_score = 20
        elif price_pos < 0.5 or support_touches >= 2:  # 中位
            support_score = 16  # v2.3 是 14 分
        elif price_pos < 0.7:
            support_score = 8
        else:
            support_score = 0
    else:  # short
        if price_pos > 0.7:  # 接近顶部
            support_score = 20
        elif price_pos > 0.5:
            support_score = 16
        elif price_pos > 0.3:
            support_score = 8
        else:
            support_score = 0
    
    details['price_pos'] = price_pos
    details['support_touches'] = support_touches
    details['support_score'] = support_score
    score += support_score
    
    # 6. 趋势强度（最高 10 分，不变）
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
    
    # 7. 连续 K 线确认（放宽：1 根 +6 分，2 根 +10 分）
    if consecutive >= 2:
        confirm_score = 10
    elif consecutive == 1:
        confirm_score = 6  # 新增：单根 K 线也有分
    else:
        confirm_score = 0
    
    details['consecutive'] = consecutive
    details['confirm_score'] = confirm_score
    score += confirm_score
    
    # 低市值币种调整（-5 分，因为波动大风险高）
    low_cap_adj = -5 if is_low_cap else 0
    details['low_cap_adj'] = low_cap_adj
    score += low_cap_adj
    
    # 上限 100 分归一化
    final_score = min(score, 100)
    
    return {
        'raw_score': score,
        'final_score': final_score,
        'details': details
    }

# v2.4 开仓门槛
V24_OPEN_THRESHOLD = 70  # 主要标的
V24_OPEN_THRESHOLD_LOW_CAP = 68  # 低市值标的（等效 73 分）
V24_OBSERVE_THRESHOLD = 60  # 观察区
V24_OBSERVE_TO_OPEN = 67  # 观察区升仓门槛（连续上升时）
