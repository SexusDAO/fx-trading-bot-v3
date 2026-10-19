#!/usr/bin/env python3
"""
评分系统 v2.4 Final - 精细校准版
目标：开仓率 5-8%，平均提升 +8-10 分
"""

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

def count_consecutive(klines, direction):
    if len(klines) < 2: return 0
    count = 0
    for i in range(len(klines)-2, -1, -1):
        curr, prev = klines[i]['close'], klines[i+1]['close'] if i+1<len(klines) else klines[i]['open']
        if (direction=='long' and curr>prev) or (direction=='short' and curr<prev): count+=1
        else: break
    return min(count, 3)

def score_v24_final(klines, ticker=None, position='long', is_low_cap=False):
    """
    v2.4 Final 评分系统 - 精细校准
    目标：中性信号 ~63-67 分，强信号 ~72-78 分，开仓率 5-8%
    """
    if len(klines) < 30: return None, {}
    closed = klines[:-1]
    prices = [k['close'] for k in closed]
    vols = [k['quote_volume'] for k in closed]
    
    rsi = calculate_rsi(prices)
    macd = calculate_macd(prices)
    e9, e21, e55 = calculate_ema(prices,9), calculate_ema(prices,21), calculate_ema(prices,55)
    
    # 成交量：前一根 K 线 / 前 10 根均量
    vol_ratio = vols[-2]/sum(vols[-12:-2])*10 if len(vols)>=12 else 1.0
    
    # 价格位置和支撑位
    p_change = (prices[-1]-prices[-20])/prices[-20]*100 if len(prices)>=20 else 0
    high_20, low_20 = max(prices[-20:]), min(prices[-20:])
    p_pos = (prices[-1]-low_20)/(high_20-low_20) if high_20>low_20 else 0.5
    support_touches = sum(1 for p in prices[-20:] if (p-low_20)/(high_20-low_20)<0.15) if high_20>low_20 else 0
    consecutive = count_consecutive(klines, position)
    
    score = 0
    
    # RSI 评分（最高 20 分，v2.3 是 18 分，v2.4 alpha 是 22 分）
    if position == 'long':
        if 48 <= rsi <= 62:  # 收窄理想区间
            rsi_score = 20
        elif 42 <= rsi < 48 or 62 < rsi <= 68:
            rsi_score = 12
        elif 32 <= rsi < 42 or 68 < rsi <= 74:
            rsi_score = 6
        else:
            rsi_score = 0
        # RSI 扣分机制（保持严格）
        rsi_penalty = 25 if rsi > 82 else (18 if rsi > 78 else (12 if rsi > 74 else (10 if rsi < 20 else 0)))
    else:  # short
        if 38 <= rsi <= 52:
            rsi_score = 20
        elif 32 <= rsi < 38 or 52 < rsi <= 58:
            rsi_score = 12
        elif 26 <= rsi < 32 or 58 < rsi <= 68:
            rsi_score = 6
        else:
            rsi_score = 0
        rsi_penalty = 25 if rsi < 18 else (18 if rsi < 22 else (12 if rsi < 26 else (10 if rsi > 82 else 0)))
    
    score += rsi_score - rsi_penalty
    
    # MACD 评分（最高 16 分，v2.3 是 15 分）
    if position == 'long':
        macd_score = 16 if macd['histogram'] > 0.001 else (10 if macd['histogram'] > 0 else (5 if macd['histogram'] > -0.0005 else 0))
    else:
        macd_score = 16 if macd['histogram'] < -0.001 else (10 if macd['histogram'] < 0 else (5 if macd['histogram'] < 0.0005 else 0))
    score += macd_score
    
    # EMA 排列（最高 18 分，v2.3 是 20 分，略微降低）
    if position == 'long':
        ema_score = 18 if e9 > e21 > e55 and e9 > prices[-1] else (13 if e9 > e21 > e55 else (7 if e9 > e21 or e21 > e55 else 0))
    else:
        ema_score = 18 if e9 < e21 < e55 and e9 < prices[-1] else (13 if e9 < e21 < e55 else (7 if e9 < e21 or e21 < e55 else 0))
    score += ema_score
    
    # 成交量评分（最高 18 分，v2.3 是 20 分）
    if vol_ratio > 2.2:  # 提高阈值
        vol_score = 18
    elif vol_ratio > 1.6:
        vol_score = 13
    elif vol_ratio > 1.1:
        vol_score = 10
    elif vol_ratio > 0.85:
        vol_score = 5
    else:
        vol_score = 0
    score += vol_score
    
    # 支撑/阻力位（最高 18 分，v2.3 是 20 分）
    if position == 'long':
        if p_pos < 0.28 or support_touches >= 3:
            support_score = 18
        elif p_pos < 0.45 or support_touches >= 2:
            support_score = 13
        elif p_pos < 0.65:
            support_score = 7
        else:
            support_score = 0
    else:
        if p_pos > 0.72:
            support_score = 18
        elif p_pos > 0.55:
            support_score = 13
        elif p_pos > 0.35:
            support_score = 7
        else:
            support_score = 0
    score += support_score
    
    # 趋势强度（最高 10 分，不变）
    if abs(p_change) > 18:  # 提高阈值
        trend_score = 10
    elif abs(p_change) > 10:
        trend_score = 6
    elif abs(p_change) > 4:
        trend_score = 3
    else:
        trend_score = 0
    score += trend_score
    
    # 连续 K 线确认（1 根 +5 分，2 根 +8 分）
    if consecutive >= 2:
        confirm_score = 8
    elif consecutive == 1:
        confirm_score = 5
    else:
        confirm_score = 0
    score += confirm_score
    
    # 低市值调整（-5 分）
    if is_low_cap:
        score -= 5
    
    # 上限 100 分
    final_score = min(score, 100)
    
    return {
        'raw_score': score,
        'final_score': final_score,
        'details': {
            'rsi': rsi, 'rsi_score': rsi_score, 'rsi_penalty': rsi_penalty,
            'macd': macd['histogram'], 'macd_score': macd_score,
            'ema_pattern': '多' if e9>e21>e55 else ('空' if e9<e21<e55 else '混'), 'ema_score': ema_score,
            'vol_ratio': vol_ratio, 'vol_score': vol_score,
            'price_pos': p_pos, 'support_touches': support_touches, 'support_score': support_score,
            'price_change': p_change, 'trend_score': trend_score,
            'consecutive': consecutive, 'confirm_score': confirm_score
        }
    }

# v2.4 Final 门槛
V24_OPEN_THRESHOLD = 70  # 主要标的
V24_OPEN_THRESHOLD_LOW_CAP = 65  # 低市值（等效 70 分）
V24_OBSERVE_THRESHOLD = 60
V24_OBSERVE_TO_OPEN = 65  # 观察区升仓（连续上升时）
