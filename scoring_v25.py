#!/usr/bin/env python3
"""
评分系统 v2.5 - 折中优化版
综合 v2.2 和 v2.4 的优点：
- 基础分 20 分（折中）
- RSI 扣分适度（>75 扣 15 分，不是 25 分）
- 成交量阈值保持>2.0x
- 开仓门槛保持 70 分
- 观察区 55-69 分
- 低市值 -3 分（不是 -5 分）
- 新增观察区升仓机制
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

def score_v25(klines, ticker=None, position='long', is_low_cap=False, observe_history=None):
    """
    v2.5 评分系统 - 折中优化版
    
    新增特性:
    1. 基础分 20 分（v2.2 是 50，v2.4 是 0）
    2. RSI 扣分适度（>75 扣 15 分，v2.4 是 25 分）
    3. 成交量阈值>2.0x（保持 v2.2/v2.4 中间值）
    4. 观察区升仓机制（连续 2 次评分上升，65 分即可开仓）
    5. 低市值 -3 分（v2.4 是 -5 分）
    6. 支撑位权重降低（最高 15 分，v2.4 是 20 分）
    7. 连续 K 线适度加分（1 根 +4 分，2 根 +7 分）
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
    
    # ===== 评分开始（基础分 20）=====
    score = 20  # v2.5 基础分
    
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
        # RSI 扣分机制（适度，v2.5 特色）
        rsi_penalty = 15 if rsi > 75 else (10 if rsi > 70 else (8 if rsi < 20 else 0))
    else:  # short
        if 35 <= rsi <= 55:
            rsi_score = 18
        elif 30 <= rsi < 35 or 55 < rsi <= 60:
            rsi_score = 12
        elif 25 <= rsi < 30 or 60 < rsi <= 70:
            rsi_score = 6
        else:
            rsi_score = 0
        rsi_penalty = 15 if rsi < 25 else (10 if rsi < 30 else (8 if rsi > 75 else 0))
    
    details['rsi'] = rsi
    details['rsi_score'] = rsi_score
    details['rsi_penalty'] = rsi_penalty
    score += rsi_score - rsi_penalty
    
    # 2. MACD 评分（最高 15 分）
    if position == 'long':
        macd_score = 15 if macd['histogram'] > 0.001 else (10 if macd['histogram'] > 0 else (5 if macd['histogram'] > -0.0005 else 0))
    else:
        macd_score = 15 if macd['histogram'] < -0.001 else (10 if macd['histogram'] < 0 else (5 if macd['histogram'] < 0.0005 else 0))
    
    details['macd'] = macd['histogram']
    details['macd_score'] = macd_score
    score += macd_score
    
    # 3. EMA 排列（最高 18 分）
    if position == 'long':
        ema_score = 18 if e9 > e21 > e55 and e9 > prices[-1] else (13 if e9 > e21 > e55 else (7 if e9 > e21 or e21 > e55 else 0))
    else:
        ema_score = 18 if e9 < e21 < e55 and e9 < prices[-1] else (13 if e9 < e21 < e55 else (7 if e9 < e21 or e21 < e55 else 0))
    
    details['ema9'] = e9
    details['ema21'] = e21
    details['ema55'] = e55
    details['ema_pattern'] = '多' if e9>e21>e55 else ('空' if e9<e21<e55 else '混')
    details['ema_score'] = ema_score
    score += ema_score
    
    # 4. 成交量评分（最高 18 分，阈值>2.0x）
    if vol_ratio > 2.0:
        vol_score = 18
    elif vol_ratio > 1.5:
        vol_score = 13
    elif vol_ratio > 1.0:
        vol_score = 10
    elif vol_ratio > 0.8:
        vol_score = 5
    else:
        vol_score = 0
    
    details['vol_ratio'] = vol_ratio
    details['vol_score'] = vol_score
    score += vol_score
    
    # 5. 支撑/阻力位（最高 15 分，权重降低）
    if position == 'long':
        if p_pos < 0.3 or support_touches >= 3:
            support_score = 15
        elif p_pos < 0.5 or support_touches >= 2:
            support_score = 10
        elif p_pos < 0.7:
            support_score = 5
        else:
            support_score = 0
    else:
        if p_pos > 0.7:
            support_score = 15
        elif p_pos > 0.5:
            support_score = 10
        elif p_pos > 0.3:
            support_score = 5
        else:
            support_score = 0
    
    details['price_pos'] = p_pos
    details['support_touches'] = support_touches
    details['support_score'] = support_score
    score += support_score
    
    # 6. 趋势强度（最高 10 分）
    if abs(p_change) > 15:
        trend_score = 10
    elif abs(p_change) > 8:
        trend_score = 6
    elif abs(p_change) > 3:
        trend_score = 3
    else:
        trend_score = 0
    
    details['price_change'] = p_change
    details['trend_score'] = trend_score
    score += trend_score
    
    # 7. 连续 K 线确认（适度加分：1 根 +4 分，2 根 +7 分）
    if consecutive >= 2:
        confirm_score = 7
    elif consecutive == 1:
        confirm_score = 4
    else:
        confirm_score = 0
    
    details['consecutive'] = consecutive
    details['confirm_score'] = confirm_score
    score += confirm_score
    
    # 8. 低市值调整（-3 分，v2.4 是 -5 分）
    if is_low_cap:
        score -= 3
    details['is_low_cap'] = is_low_cap
    details['low_cap_adj'] = -3 if is_low_cap else 0
    
    # 9. 观察区升仓机制（v2.5 新增）
    observe_bonus = 0
    if observe_history and len(observe_history) >= 2:
        # 检查评分是否连续上升
        if observe_history[-1] > observe_history[-2] > observe_history[-3] if len(observe_history)>=3 else True:
            current_score = observe_history[-1]
            if 65 <= current_score < 70:
                observe_bonus = 5  # 加分至 70 分，触发开仓
                details['observe_bonus'] = observe_bonus
                score += observe_bonus
    
    # 上限 100 分
    final_score = min(score, 100)
    
    return {
        'raw_score': score,
        'final_score': final_score,
        'details': details
    }

# v2.5 门槛
V25_OPEN_THRESHOLD = 70  # 保持 70 分
V25_OBSERVE_LOW = 55     # 观察区下限（v2.4 是 60）
V25_OBSERVE_HIGH = 69    # 观察区上限
V25_OBSERVE_TO_OPEN = 65 # 观察区升仓门槛（连续上升时）
V25_LOW_CAP_THRESHOLD = 67  # 低市值开仓门槛（等效 70 分）
