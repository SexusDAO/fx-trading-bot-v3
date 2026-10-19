"""
市场环境判断规则 - 纯 Python 计算，不走 LLM
每 30 分钟重新判断一次 BTC 趋势模式
"""
import json
import sys
from datetime import datetime

sys.path.insert(0, '/root/.openclaw/workspace')

try:
    import sys
    sys.path.insert(0, '/root/.openclaw/workspace')
    from binance_api import BinanceAPI
except ImportError:
    BinanceAPI = None
    print("警告：binance_api 模块不可用，将使用简化模式")

class MarketRegime:
    BULL = "bull"      # 牛市模式：多单最多 3 个，空单最多 2 个
    BEAR = "bear"      # 熊市模式：空单最多 3 个，多单最多 2 个
    RANGE = "range"    # 震荡模式：多单最多 2 个，空单最多 2 个，总计不超过 4 个

class MarketFilter:
    def __init__(self):
        self.api = BinanceAPI() if BinanceAPI else None
        self.last_check = None
        self.current_regime = MarketRegime.RANGE
    
    def get_btc_klines(self, interval='4h', limit=100):
        """获取 BTC 4H K 线数据"""
        if self.api and hasattr(self.api, 'get_klines'):
            return self.api.get_klines('BTCUSDT', interval=interval, limit=limit)
        
        # 简化版：直接调用 requests
        try:
            import requests
            url = "https://fapi.binance.com/fapi/v1/klines"
            params = {'symbol': 'BTCUSDT', 'interval': interval, 'limit': limit}
            r = requests.get(url, params=params, timeout=10)
            return r.json()
        except Exception as e:
            print(f"获取 K 线失败：{e}")
            return None
    
    def calc_ema(self, prices, period):
        """计算 EMA"""
        if len(prices) < period:
            return None
        
        multiplier = 2 / (period + 1)
        ema = sum(prices[:period]) / period
        
        for price in prices[period:]:
            ema = (price - ema) * multiplier + ema
        
        return ema
    
    def calc_ma(self, prices, period):
        """计算简单移动平均"""
        if len(prices) < period:
            return None
        return sum(prices[-period:]) / period
    
    def check_btc_filter(self):
        """
        判断 BTC 趋势模式
        返回：MarketRegime 枚举值
        """
        klines = self.get_btc_klines(interval='4h', limit=100)
        if not klines or len(klines) < 100:
            return MarketRegime.RANGE
        
        # 提取收盘价
        closes = [float(k[4]) for k in klines]
        current_price = closes[-1]
        
        # 计算 EMA(9), EMA(21), EMA(55)
        ema9 = self.calc_ema(closes, 9)
        ema21 = self.calc_ema(closes, 21)
        ema55 = self.calc_ema(closes, 55)
        
        if not all([ema9, ema21, ema55]):
            return MarketRegime.RANGE
        
        # 计算 MA99
        ma99 = self.calc_ma(closes, 99)
        if not ma99:
            return MarketRegime.RANGE
        
        # 判断多排：EMA9 > EMA21 > EMA55 且价格在 MA99 上方
        bull_condition = (ema9 > ema21 > ema55) and (current_price > ma99)
        
        # 判断空排：EMA9 < EMA21 < EMA55 且价格在 MA99 下方
        bear_condition = (ema9 < ema21 < ema55) and (current_price < ma99)
        
        if bull_condition:
            regime = MarketRegime.BULL
        elif bear_condition:
            regime = MarketRegime.BEAR
        else:
            regime = MarketRegime.RANGE
        
        self.current_regime = regime
        self.last_check = datetime.utcnow()
        
        return regime
    
    def get_position_limits(self):
        """
        根据当前市场模式返回持仓上限
        返回：(max_long, max_short, max_total)
        """
        if self.last_check is None or (datetime.utcnow() - self.last_check).total_seconds() > 1800:
            # 超过 30 分钟未检查，重新判断
            self.check_btc_filter()
        
        limits = {
            MarketRegime.BULL: (3, 2, 5),
            MarketRegime.BEAR: (2, 3, 5),
            MarketRegime.RANGE: (2, 2, 4)
        }
        
        return limits[self.current_regime]
    
    def save_state(self, filepath='config/market_regime.json'):
        """保存当前市场状态"""
        state = {
            'regime': self.current_regime,
            'last_check': self.last_check.isoformat() if self.last_check else None
        }
        with open(filepath, 'w') as f:
            json.dump(state, f, indent=2)
    
    def load_state(self, filepath='config/market_regime.json'):
        """加载市场状态"""
        try:
            with open(filepath, 'r') as f:
                state = json.load(f)
            self.current_regime = state['regime']
            self.last_check = datetime.fromisoformat(state['last_check']) if state['last_check'] else None
        except FileNotFoundError:
            self.check_btc_filter()


if __name__ == '__main__':
    # 测试
    mf = MarketFilter()
    regime = mf.check_btc_filter()
    limits = mf.get_position_limits()
    print(f"当前市场模式：{regime}")
    print(f"持仓上限：多单{limits[0]}, 空单{limits[1]}, 总计{limits[2]}")
    mf.save_state()
