"""
开仓评分计算模块 v2.2 - 背离检测 + 门槛提高
修复问题：
1. 趋势策略门槛从 63 分 → 70 分
2. 63-69 分进入观察区
3. 实现简化版背离检测（纯 Python）
"""
import sys
sys.path.insert(0, '/root/.openclaw/workspace')


class PreFilter:
    """强制跳过前置检查"""
    
    @staticmethod
    def check(volume_ratio, spread_pct, price_change_24h, funding_rate):
        """
        评分前强制检查
        返回：(should_skip, reason)
        """
        # 1. 成交量 < 0.1x → 直接跳过
        if volume_ratio < 0.1:
            return True, f"成交量枯竭 ({volume_ratio:.2f}x)"
        
        # 2. 点差 > 1% → 直接跳过
        if spread_pct > 1.0:
            return True, f"点差过大 ({spread_pct:.2f}%)"
        
        # 3. 24h 涨跌幅 > 30% → 直接跳过
        if abs(price_change_24h) > 30:
            return True, f"极端波动 ({price_change_24h:+.1f}%)"
        
        # 4. 资金费率 > 0.05% → 直接跳过
        if abs(funding_rate) > 0.05:
            return True, f"资金费率过高 ({funding_rate:.4f}%)"
        
        return False, None


class DivergenceDetector:
    """背离检测器 - 简化版（纯 Python）"""
    
    @staticmethod
    def find_local_extrema(values, window=3):
        """
        找出局部极值点
        返回：(local_lows, local_highs) 索引列表
        """
        local_lows = []
        local_highs = []
        
        for i in range(window, len(values) - window):
            # 局部低点
            is_low = True
            for j in range(i - window, i + window + 1):
                if j != i and values[j] <= values[i]:
                    is_low = False
                    break
            if is_low:
                local_lows.append(i)
            
            # 局部高点
            is_high = True
            for j in range(i - window, i + window + 1):
                if j != i and values[j] >= values[i]:
                    is_high = False
                    break
            if is_high:
                local_highs.append(i)
        
        return local_lows, local_highs
    
    @staticmethod
    def detect_bullish_divergence(klines):
        """
        底背离检测
        扫描近 20 根 1H K 线
        条件：价格新低但 MACD 柱不新低
        
        返回：(is_divergence, strength)
        - strength: 0-100，背离强度
        """
        if len(klines) < 20:
            return False, 0
        
        # 提取数据
        lows = [float(k[3]) for k in klines[-20:]]
        closes = [float(k[4]) for k in klines[-20:]]
        
        # 计算 MACD 柱（简化）
        macd_hist = []
        for i in range(26, len(closes)):
            ema12 = sum(closes[i-12:i]) / 12
            ema26 = sum(closes[i-26:i]) / 26
            hist = (ema12 - ema26)
            macd_hist.append(hist)
        
        if len(macd_hist) < 10:
            return False, 0
        
        # 找出局部低点
        local_lows, _ = DivergenceDetector.find_local_extrema(lows, window=3)
        
        if len(local_lows) < 2:
            return False, 0
        
        # 检查最近的两个低点
        recent_low_idx = local_lows[-1]
        prev_low_idx = local_lows[-2]
        
        # 条件 1: 低点二价格 < 低点一价格（价格新低）
        price_lower = lows[recent_low_idx] < lows[prev_low_idx]
        
        # 条件 2: 低点二 MACD 柱 > 低点一 MACD 柱（MACD 不新低）
        # 注意：MACD 柱索引比 K 线索引少 26
        macd_idx_1 = prev_low_idx - 26 if prev_low_idx >= 26 else 0
        macd_idx_2 = recent_low_idx - 26 if recent_low_idx >= 26 else 0
        
        if macd_idx_1 < 0 or macd_idx_2 < 0 or macd_idx_1 >= len(macd_hist) or macd_idx_2 >= len(macd_hist):
            return False, 0
        
        macd_higher = macd_hist[macd_idx_2] > macd_hist[macd_idx_1]
        
        # 底背离成立
        if price_lower and macd_higher:
            # 计算背离强度
            price_diff_pct = (lows[prev_low_idx] - lows[recent_low_idx]) / lows[prev_low_idx] * 100
            macd_diff = macd_hist[macd_idx_2] - macd_hist[macd_idx_1]
            
            # 强度 = 价格跌幅 + MACD 涨幅（标准化）
            strength = min(100, int(price_diff_pct * 10 + macd_diff * 1000))
            return True, strength
        
        return False, 0
    
    @staticmethod
    def detect_bearish_divergence(klines):
        """
        顶背离检测
        扫描近 20 根 1H K 线
        条件：价格新高但 MACD 柱不新高
        
        返回：(is_divergence, strength)
        - strength: 0-100，背离强度
        """
        if len(klines) < 20:
            return False, 0
        
        # 提取数据
        highs = [float(k[2]) for k in klines[-20:]]
        closes = [float(k[4]) for k in klines[-20:]]
        
        # 计算 MACD 柱（简化）
        macd_hist = []
        for i in range(26, len(closes)):
            ema12 = sum(closes[i-12:i]) / 12
            ema26 = sum(closes[i-26:i]) / 26
            hist = (ema12 - ema26)
            macd_hist.append(hist)
        
        if len(macd_hist) < 10:
            return False, 0
        
        # 找出局部高点
        _, local_highs = DivergenceDetector.find_local_extrema(highs, window=3)
        
        if len(local_highs) < 2:
            return False, 0
        
        # 检查最近的两个高点
        recent_high_idx = local_highs[-1]
        prev_high_idx = local_highs[-2]
        
        # 条件 1: 高点二价格 > 高点一价格（价格新高）
        price_higher = highs[recent_high_idx] > highs[prev_high_idx]
        
        # 条件 2: 高点二 MACD 柱 < 高点一 MACD 柱（MACD 不新高）
        macd_idx_1 = prev_high_idx - 26 if prev_high_idx >= 26 else 0
        macd_idx_2 = recent_high_idx - 26 if recent_high_idx >= 26 else 0
        
        if macd_idx_1 < 0 or macd_idx_2 < 0 or macd_idx_1 >= len(macd_hist) or macd_idx_2 >= len(macd_hist):
            return False, 0
        
        macd_lower = macd_hist[macd_idx_2] < macd_hist[macd_idx_1]
        
        # 顶背离成立
        if price_higher and macd_lower:
            # 计算背离强度
            price_diff_pct = (highs[recent_high_idx] - highs[prev_high_idx]) / highs[prev_high_idx] * 100
            macd_diff = macd_hist[macd_idx_1] - macd_hist[macd_idx_2]
            
            # 强度 = 价格涨幅 + MACD 跌幅（标准化）
            strength = min(100, int(price_diff_pct * 10 + macd_diff * 1000))
            return True, strength
        
        return False, 0


class StrategyDetector:
    """策略类型自动识别"""
    
    @staticmethod
    def detect(rsi, ema_alignment, macd_hist, price, recent_high, recent_low, volume_ratio):
        """
        自动判断策略类型
        返回：'reversal', 'breakout', 'trend', 或 None
        """
        # 1. 优先判断反转策略（RSI 超买超卖 + 背离）
        if rsi < 30 or rsi > 70:
            # 简化背离检测：MACD 与价格方向不一致
            if rsi < 30 and macd_hist > -0.002:  # 超卖 + MACD 未创新低
                return 'reversal_long'
            if rsi > 70 and macd_hist < 0.002:  # 超买 + MACD 未创新高
                return 'reversal_short'
        
        # 2. 判断突破策略（价格突破近期高低点 + 放量）
        if price > recent_high and volume_ratio > 1.5:
            return 'breakout_long'
        if price < recent_low and volume_ratio > 1.5:
            return 'breakout_short'
        
        # 3. 默认趋势策略（EMA 排列方向）
        if ema_alignment >= 70:  # 多排
            return 'trend_long'
        if ema_alignment <= 20:  # 空排
            return 'trend_short'
        
        # 4. EMA 混乱但其他条件一般 → 趋势（默认）
        if ema_alignment >= 50:
            return 'trend_long'
        else:
            return 'trend_short'


class TechnicalAnalyzer:
    """技术指标计算"""
    
    @staticmethod
    def calc_macd_ratio(macd_hist, price):
        """
        MACD 标准化比率
        消除价格量级影响
        """
        if price == 0:
            return 0
        return (macd_hist / price) * 1000
    
    @staticmethod
    def calc_support_strength(klines, current_price, side='long'):
        """
        自动识别支撑/阻力位强度
        扫描近 100 根 K 线，找出价格反转点
        """
        if len(klines) < 20:
            return 4, 0  # 无法识别
        
        lows = [float(k[3]) for k in klines]
        highs = [float(k[2]) for k in klines]
        
        # 找出局部极值点（前后各 5 根 K 线的极值）
        support_levels = []
        resistance_levels = []
        
        for i in range(5, len(lows) - 5):
            # 支撑位（局部低点）
            window = lows[i-5:i+6]
            if lows[i] == min(window):
                support_levels.append(lows[i])
            
            # 阻力位（局部高点）
            window = highs[i-5:i+6]
            if highs[i] == max(window):
                resistance_levels.append(highs[i])
        
        # 统计当前价格附近的测试次数
        tolerance = 0.02  # 2% 容差
        
        if side == 'long':
            # 找支撑位
            nearby_supports = [s for s in support_levels if abs(s - current_price) / current_price < tolerance]
            test_count = len(nearby_supports)
        else:
            # 找阻力位
            nearby_resistances = [r for r in resistance_levels if abs(r - current_price) / current_price < tolerance]
            test_count = len(nearby_resistances)
        
        # 转换为分数
        if test_count >= 3:
            return 20, test_count
        elif test_count == 2:
            return 14, test_count
        elif test_count == 1:
            return 8, test_count
        else:
            return 4, 0
    
    @staticmethod
    def calc_atr(klines, period=14):
        """计算 ATR (Average True Range)"""
        if len(klines) < period + 1:
            return 0
        
        tr_values = []
        for i in range(1, len(klines)):
            high = float(klines[i][2])
            low = float(klines[i][3])
            prev_close = float(klines[i-1][4])
            
            tr = max(
                high - low,
                abs(high - prev_close),
                abs(low - prev_close)
            )
            tr_values.append(tr)
        
        # 简化：用最近 period 个 TR 的平均值
        if len(tr_values) >= period:
            atr = sum(tr_values[-period:]) / period
        else:
            atr = sum(tr_values) / len(tr_values)
        
        return atr


class ScoringEngine:
    """评分引擎 v2.2 - 背离检测 + 门槛提高"""
    
    # v2.2 新门槛
    TREND_THRESHOLD = 70  # 从 63 提高到 70
    REVERSAL_THRESHOLD = 60  # 维持不变
    BREAKOUT_THRESHOLD = 65  # 维持不变
    OBSERVATION_LOW = 63  # 观察区下限
    OBSERVATION_HIGH = 69  # 观察区上限
    
    @staticmethod
    def calc_score_trend(details, klines=None):
        """
        趋势策略评分（做多）
        """
        score = 0
        breakdown = {}
        
        rsi = details.get('rsi', 50)
        macd_hist = details.get('macd_hist', 0)
        price = details.get('price', 1)
        ema_alignment = details.get('ema_alignment', 50)
        volume_ratio = details.get('volume_ratio', 1.0)
        
        # 1. RSI 评分 (0-25 分)
        if 30 <= rsi <= 45:
            rsi_score = 22  # 底部区域，最佳
        elif 45 < rsi <= 60:
            rsi_score = 18  # 中性
        elif 60 < rsi <= 70:
            rsi_score = 15  # 偏高
        elif rsi < 30:
            rsi_score = 20  # 超卖
        else:
            rsi_score = 10  # 超买风险
        breakdown['rsi'] = rsi_score
        score += rsi_score
        
        # 2. MACD 评分 (0-20 分) - 动态阈值
        macd_ratio = TechnicalAnalyzer.calc_macd_ratio(macd_hist, price)
        if macd_ratio > 0.5:
            macd_score = 20  # 强势
        elif macd_ratio > 0:
            macd_score = 16  # 温和
        elif macd_ratio > -0.5:
            macd_score = 14  # 轻微负（底部常态）
        else:
            macd_score = 8  # 大幅负
        breakdown['macd'] = macd_score
        score += macd_score
        
        # 3. EMA 排列评分 (0-20 分)
        if ema_alignment >= 85:  # 多排
            ema_score = 20
        elif ema_alignment <= 15:  # 空排
            ema_score = 8  # 反向趋势
        else:  # 混乱
            ema_score = 14  # 趋势转换期
        breakdown['ema'] = ema_score
        score += ema_score
        
        # 4. 成交量评分 (0-15 分)
        if volume_ratio > 2.0:
            vol_score = 15
        elif volume_ratio > 1.5:
            vol_score = 13
        elif volume_ratio > 0.8:
            vol_score = 11
        else:
            vol_score = 8  # 缩量但不 penalize 太多
        breakdown['volume'] = vol_score
        score += vol_score
        
        # 5. 支撑位强度 (0-20 分) - 自动识别
        if klines:
            support_score, test_count = TechnicalAnalyzer.calc_support_strength(
                klines, price, side='long'
            )
        else:
            support_score = details.get('support_strength', 50) / 100 * 20
            test_count = 0
        breakdown['support'] = support_score
        breakdown['support_tests'] = test_count
        score += support_score
        
        return round(score, 2), breakdown
    
    @staticmethod
    def calc_score_trend_short(details, klines=None):
        """
        趋势策略评分（做空）
        EMA 空排 20 分（与做多对称）
        """
        score = 0
        breakdown = {}
        
        rsi = details.get('rsi', 50)
        macd_hist = details.get('macd_hist', 0)
        price = details.get('price', 1)
        ema_alignment = details.get('ema_alignment', 50)
        volume_ratio = details.get('volume_ratio', 1.0)
        
        # 1. RSI 评分 (0-25 分) - 做空时 RSI 偏高更好
        if 55 <= rsi <= 70:
            rsi_score = 22  # 顶部区域，最佳
        elif 40 <= rsi < 55:
            rsi_score = 18  # 中性
        elif 30 <= rsi < 40:
            rsi_score = 15  # 偏低
        elif rsi > 70:
            rsi_score = 20  # 超买
        else:
            rsi_score = 10  # 超卖风险
        breakdown['rsi'] = rsi_score
        score += rsi_score
        
        # 2. MACD 评分 (0-20 分) - 做空时负值更好
        macd_ratio = TechnicalAnalyzer.calc_macd_ratio(macd_hist, price)
        if macd_ratio < -0.5:
            macd_score = 20  # 强势负
        elif macd_ratio < 0:
            macd_score = 16  # 温和负
        elif macd_ratio < 0.5:
            macd_score = 14  # 轻微正（顶部常态）
        else:
            macd_score = 8  # 大幅正
        breakdown['macd'] = macd_score
        score += macd_score
        
        # 3. EMA 排列评分 (0-20 分) - EMA 空排 20 分
        if ema_alignment <= 15:  # 空排
            ema_score = 20  # 趋势明确（做空利好）
        elif ema_alignment >= 85:  # 多排
            ema_score = 8  # 反向趋势
        else:  # 混乱
            ema_score = 14  # 趋势转换期
        breakdown['ema'] = ema_score
        score += ema_score
        
        # 4. 成交量评分 (0-15 分)
        if volume_ratio > 2.0:
            vol_score = 15
        elif volume_ratio > 1.5:
            vol_score = 13
        elif volume_ratio > 0.8:
            vol_score = 11
        else:
            vol_score = 8
        breakdown['volume'] = vol_score
        score += vol_score
        
        # 5. 阻力位强度 (0-20 分) - 自动识别
        if klines:
            resistance_score, test_count = TechnicalAnalyzer.calc_support_strength(
                klines, price, side='short'
            )
        else:
            resistance_score = details.get('resistance_strength', 50) / 100 * 20
            test_count = 0
        breakdown['resistance'] = resistance_score
        breakdown['resistance_tests'] = test_count
        score += resistance_score
        
        return round(score, 2), breakdown
    
    @staticmethod
    def calc_score_reversal(details, klines=None):
        """
        反转策略评分（做多/做空）
        侧重超买超卖 + 背离
        """
        score = 0
        breakdown = {}
        
        rsi = details.get('rsi', 50)
        macd_hist = details.get('macd_hist', 0)
        price = details.get('price', 1)
        volume_ratio = details.get('volume_ratio', 1.0)
        side = details.get('side', 'long')
        
        # 1. RSI 超买超卖评分 (0-30 分)
        if side == 'long':  # 做多反转（超卖）
            if rsi < 25:
                rsi_score = 30
            elif rsi < 30:
                rsi_score = 25
            elif rsi < 35:
                rsi_score = 18
            elif rsi < 40:
                rsi_score = 10
            else:
                rsi_score = 5
            breakdown['rsi_oversold'] = rsi_score
        else:  # 做空反转（超买）
            if rsi > 75:
                rsi_score = 30
            elif rsi > 70:
                rsi_score = 25
            elif rsi > 65:
                rsi_score = 18
            elif rsi > 60:
                rsi_score = 10
            else:
                rsi_score = 5
            breakdown['rsi_overbought'] = rsi_score
        score += rsi_score
        
        # 2. 背离检测 (0-30 分) - v2.2 新增自动检测
        if klines:
            if side == 'long':
                has_div, strength = DivergenceDetector.detect_bullish_divergence(klines)
            else:
                has_div, strength = DivergenceDetector.detect_bearish_divergence(klines)
            
            if has_div:
                div_score = min(30, 20 + strength // 10)  # 20-30 分
            else:
                # 简化：MACD 与 RSI 方向不一致算弱背离
                if side == 'long' and rsi < 35 and macd_hist > -0.002:
                    div_score = 15
                elif side == 'short' and rsi > 65 and macd_hist < 0.002:
                    div_score = 15
                else:
                    div_score = 5
        else:
            divergence = details.get('divergence', 'none')
            if divergence == 'bullish' or divergence == 'bearish':
                div_score = 30
            elif divergence == 'weak':
                div_score = 15
            else:
                div_score = 5
        
        breakdown['divergence'] = div_score
        score += div_score
        
        # 3. 支撑/阻力位测试 (0-25 分)
        if klines:
            support_score, test_count = TechnicalAnalyzer.calc_support_strength(
                klines, price, side=side
            )
            support_score = min(25, support_score + 5)  # 反转策略支撑/阻力位更重要
        else:
            support_tests = details.get('support_tests', 0)
            if support_tests >= 3:
                support_score = 25
            elif support_tests == 2:
                support_score = 18
            elif support_tests == 1:
                support_score = 10
            else:
                support_score = 5
        breakdown['support_tests'] = support_score
        score += support_score
        
        # 4. 成交量萎缩 (0-15 分)
        if volume_ratio < 0.5:
            vol_score = 15  # 极度缩量（抛压竭/买盘竭）
        elif volume_ratio < 0.7:
            vol_score = 12
        elif volume_ratio < 0.9:
            vol_score = 8
        else:
            vol_score = 5
        breakdown['volume_decline'] = vol_score
        score += vol_score
        
        return round(score, 2), breakdown
    
    @staticmethod
    def calc_score_breakout(details, klines=None):
        """
        突破策略评分
        侧重突破位置 + 放量
        """
        score = 0
        breakdown = {}
        
        volume_ratio = details.get('volume_ratio', 1.0)
        price = details.get('price', 1)
        recent_high = details.get('recent_high', price)
        recent_low = details.get('recent_low', price)
        side = details.get('side', 'long')
        
        # 1. 突破位置 (0-30 分)
        if side == 'long':
            if price > recent_high * 1.01:  # 突破 1%
                breakout_score = 30
            elif price > recent_high * 0.99:  # 接近突破
                breakout_score = 20
            else:
                breakout_score = 10
        else:
            if price < recent_low * 0.99:  # 跌破 1%
                breakout_score = 30
            elif price < recent_low * 1.01:  # 接近跌破
                breakout_score = 20
            else:
                breakout_score = 10
        breakdown['breakout'] = breakout_score
        score += breakout_score
        
        # 2. 成交量 (0-25 分)
        if volume_ratio > 3.0:
            vol_score = 25
        elif volume_ratio > 2.0:
            vol_score = 20
        elif volume_ratio > 1.5:
            vol_score = 15
        else:
            vol_score = 8
        breakdown['volume'] = vol_score
        score += vol_score
        
        # 3. 突破幅度 (0-20 分)
        if side == 'long':
            breakout_pct = (price - recent_high) / recent_high * 100
        else:
            breakout_pct = (recent_low - price) / recent_low * 100
        
        if breakout_pct > 5:
            amp_score = 20
        elif breakout_pct > 3:
            amp_score = 15
        elif breakout_pct > 1:
            amp_score = 10
        else:
            amp_score = 5
        breakdown['breakout_pct'] = amp_score
        score += amp_score
        
        # 4. 回踩确认 (0-15 分)
        pullback_score = details.get('pullback_confirmed', False)
        score += 15 if pullback_score else 10
        breakdown['pullback'] = 15 if pullback_score else 10
        
        # 5. 清算密集区 (0-10 分)
        liquidation = details.get('liquidation_nearby', False)
        score += 10 if liquidation else 3
        breakdown['liquidation'] = 10 if liquidation else 3
        
        return round(score, 2), breakdown
    
    @staticmethod
    def calc_position_usdt(score, details, balance_usdt, current_positions_count, max_positions, strategy='trend'):
        """
        计算仓位大小 - 平滑公式
        """
        # v2.2: 63-69 分观察区，仓位减半
        if score < ScoringEngine.OBSERVATION_LOW:
            base_position = 0  # 低于 63 分不开仓
        elif score < ScoringEngine.OBSERVATION_HIGH + 1:
            # 观察区 63-69 分：基础仓位减半
            base_position = (300 + (score - 60) * 14) * 0.5
        else:
            # 70 分以上：正常仓位
            base_position = 300 + (score - 60) * 14
        
        # 策略类型封顶
        strategy_limits = {
            'trend': 800,
            'reversal': 500,
            'breakout': 600,
        }
        max_position = strategy_limits.get(strategy, 600)
        base_position = min(base_position, max_position)
        
        # 持仓数系数
        position_ratio = current_positions_count / max_positions if max_positions > 0 else 0
        position_multiplier = 1.0 - (position_ratio * 0.4)
        
        # 风险回报比调整
        rr = details.get('risk_reward_ratio', 1.0)
        rr_multiplier = min(1.2, max(0.5, rr / 2.5))
        
        # 最终仓位
        position = base_position * position_multiplier * rr_multiplier
        position = min(position, balance_usdt * 0.50)
        
        if score >= ScoringEngine.OBSERVATION_LOW:
            position = max(position, 50)
        
        return round(position, 2)
    
    @staticmethod
    def calc_sl_atr(klines, side, entry_price, market_cap='mid'):
        """
        计算 ATR 动态止损
        """
        atr = TechnicalAnalyzer.calc_atr(klines, 14)
        
        if atr == 0:
            if side == 'long':
                return entry_price * 0.96
            else:
                return entry_price * 1.04
        
        atr_multipliers = {
            'high': 1.2,
            'mid': 1.5,
            'low': 2.0,
        }
        atr_multiplier = atr_multipliers.get(market_cap, 1.5)
        
        sl_distance = atr * atr_multiplier
        sl_distance = max(sl_distance, entry_price * 0.01)
        sl_distance = min(sl_distance, entry_price * 0.08)
        
        if side == 'long':
            sl_price = entry_price - sl_distance
        else:
            sl_price = entry_price + sl_distance
        
        return round(sl_price, 4)


class DecisionEngine:
    """开仓决策引擎 v2.2"""
    
    @staticmethod
    def make_decision(symbol, klines, details, balance_usdt, current_positions, max_positions):
        """
        完整决策流程 v2.2
        1. 前置检查 → 2. 策略识别 → 3. 评分 → 4. 决策
        """
        # 1. 前置强制检查
        volume_ratio = details.get('volume_ratio', 1.0)
        spread_pct = details.get('spread_pct', 0.1)
        price_change_24h = details.get('price_change_24h', 0)
        funding_rate = details.get('funding_rate', 0.01)
        
        should_skip, reason = PreFilter.check(volume_ratio, spread_pct, price_change_24h, funding_rate)
        if should_skip:
            return {
                'action': 'skip',
                'reason': f'前置检查失败：{reason}',
                'symbol': symbol
            }
        
        # 2. 策略类型识别
        rsi = details.get('rsi', 50)
        ema_alignment = details.get('ema_alignment', 50)
        macd_hist = details.get('macd_hist', 0)
        price = float(klines[-1][4]) if klines else 1
        recent_high = max(float(k[2]) for k in klines[-20:]) if klines else price
        recent_low = min(float(k[3]) for k in klines[-20:]) if klines else price
        
        strategy = StrategyDetector.detect(
            rsi, ema_alignment, macd_hist, price, recent_high, recent_low, volume_ratio
        )
        
        # 3. 根据策略类型评分
        if strategy in ['reversal_long', 'reversal_short']:
            details['side'] = 'long' if 'long' in strategy else 'short'
            score, breakdown = ScoringEngine.calc_score_reversal(details, klines)
            strategy_name = '反转' + ('做多' if 'long' in strategy else '做空')
            threshold = ScoringEngine.REVERSAL_THRESHOLD
        elif strategy in ['breakout_long', 'breakout_short']:
            details['side'] = 'long' if 'long' in strategy else 'short'
            details['recent_high'] = recent_high
            details['recent_low'] = recent_low
            score, breakdown = ScoringEngine.calc_score_breakout(details, klines)
            strategy_name = '突破' + ('做多' if 'long' in strategy else '做空')
            threshold = ScoringEngine.BREAKOUT_THRESHOLD
        elif strategy == 'trend_long':
            score, breakdown = ScoringEngine.calc_score_trend(details, klines)
            strategy_name = '趋势做多'
            threshold = ScoringEngine.TREND_THRESHOLD  # v2.2: 70 分
        elif strategy == 'trend_short':
            score, breakdown = ScoringEngine.calc_score_trend_short(details, klines)
            strategy_name = '趋势做空'
            threshold = ScoringEngine.TREND_THRESHOLD  # v2.2: 70 分
        else:
            score, breakdown = ScoringEngine.calc_score_trend(details, klines)
            strategy_name = '趋势做多（默认）'
            threshold = ScoringEngine.TREND_THRESHOLD
        
        # 4. 市值分层调整
        market_cap = details.get('market_cap', 'mid')
        market_cap_adjustments = {
            'high': 1.1,
            'mid': 1.0,
            'low': 0.9,
        }
        adjustment = market_cap_adjustments.get(market_cap, 1.0)
        adjusted_score = score * adjustment
        
        # 5. 决策（v2.2 新逻辑）
        if adjusted_score >= 85:
            action = 'dual_net'
            position = ScoringEngine.calc_position_usdt(
                adjusted_score, details, balance_usdt,
                len(current_positions), max_positions, strategy='trend'
            )
        elif adjusted_score >= threshold:
            action = 'limit_order'
            position = ScoringEngine.calc_position_usdt(
                adjusted_score, details, balance_usdt,
                len(current_positions), max_positions, strategy=strategy.split('_')[0]
            )
        elif adjusted_score >= ScoringEngine.OBSERVATION_LOW and adjusted_score < threshold:
            # v2.2 新增：观察区
            action = 'observe'
            position = ScoringEngine.calc_position_usdt(
                adjusted_score, details, balance_usdt,
                len(current_positions), max_positions, strategy=strategy.split('_')[0]
            )
        else:
            action = 'skip'
            position = 0
        
        # 6. 计算止损止盈
        sl_price = None
        tp_price = None
        if action not in ['skip', 'observe'] and klines:
            side = 'long' if 'long' in strategy else 'short'
            sl_price = ScoringEngine.calc_sl_atr(klines, side, price, market_cap)
            
            if side == 'long':
                risk = price - sl_price
                tp_price = price + risk * 2.5
            else:
                risk = sl_price - price
                tp_price = price - risk * 2.5
        
        return {
            'action': action,
            'symbol': symbol,
            'strategy': strategy_name,
            'raw_score': score,
            'adjusted_score': adjusted_score,
            'threshold': threshold,
            'market_cap': market_cap,
            'position_usdt': position,
            'sl_price': sl_price,
            'tp_price': tp_price,
            'breakdown': breakdown,
            'reason': f'{strategy_name} {adjusted_score:.1f}分 {"达标" if action == "limit_order" else "双网" if action == "dual_net" else "观察区" if action == "observe" else "未达标"}'
        }


if __name__ == '__main__':
    # 测试
    print("="*60)
    print("评分系统 v2.2 测试 - 背离检测 + 门槛提高")
    print("="*60)
    
    # 测试 1: 背离检测
    print("\n[测试 1] 背离检测")
    
    # 模拟底背离 K 线数据
    test_klines_bullish = []
    for i in range(40):
        if i < 20:
            low = 100 - i * 0.5  # 下跌
        else:
            low = 90 + (i - 20) * 0.3  # 反弹
        test_klines_bullish.append([
            1700000000000 + i * 3600000,  # 时间戳
            low + 2,  # 开盘
            low + 5,  # 最高
            low,      # 最低
            low + 3,  # 收盘
            1000      # 成交量
        ])
    
    has_div, strength = DivergenceDetector.detect_bullish_divergence(test_klines_bullish)
    print(f"底背离测试：{'检测到' if has_div else '未检测到'} (强度：{strength})")
    
    # 测试 2: 门槛提高
    print("\n[测试 2] 门槛提高")
    print(f"趋势策略门槛：{ScoringEngine.TREND_THRESHOLD}分 (从 63 提高到 70)")
    print(f"观察区：{ScoringEngine.OBSERVATION_LOW}-{ScoringEngine.OBSERVATION_HIGH}分")
    
    # 测试 3: 观察区仓位
    print("\n[测试 3] 观察区仓位")
    for score in [63, 65, 68, 70, 75]:
        position = ScoringEngine.calc_position_usdt(
            score=score,
            details={'risk_reward_ratio': 2.0},
            balance_usdt=10000,
            current_positions_count=2,
            max_positions=5,
            strategy='trend'
        )
        status = "观察区" if score < 70 else "达标"
        print(f"{score}分 ({status}) → {position}U")
    
    print("\n✅ 测试完成")
