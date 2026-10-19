"""
LLM-4: SL/TP 位置计算（开仓成功后）
需要：清算图数据 + 技术位分析
原因：止损止盈位置需要结合清算区和技术位
触发：open_filled 事件
输出：具体的止损价格 + 止盈价格
"""
import sys
sys.path.insert(0, '/root/.openclaw/workspace')

from binance_api import BinanceAPI

def calculate_sl_tp(symbol, side, entry_price, klines, liquidation_data):
    """
    计算止损止盈位置
    结合清算区和技术位
    
    参数：
    - symbol: 交易对
    - side: LONG 或 SHORT
    - entry_price: 入场价
    - klines: K 线数据
    - liquidation_data: 清算数据
    
    返回：
    - sl_price: 止损价
    - tp_price: 止盈价
    - rationale: 计算依据
    """
    if not klines or len(klines) < 50:
        # 数据不足，使用固定比例
        if side == 'LONG':
            sl_price = entry_price * 0.97  # -3%
            tp_price = entry_price * 1.06  # +6%
        else:
            sl_price = entry_price * 1.03  # +3%
            tp_price = entry_price * 0.94  # -6%
        
        return sl_price, tp_price, "使用固定比例（数据不足）"
    
    # 提取高低点
    highs = [float(k[2]) for k in klines]
    lows = [float(k[3]) for k in klines]
    closes = [float(k[4]) for k in klines]
    
    # 计算 ATR（14 周期）
    tr_values = []
    for i in range(1, min(15, len(closes))):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i-1]),
            abs(lows[i] - closes[i-1])
        )
        tr_values.append(tr)
    
    atr = sum(tr_values) / len(tr_values) if tr_values else (highs[-1] - lows[-1])
    
    # 识别关键支撑阻力位（简化版）
    recent_lows = sorted(lows[-20:])[:3]  # 最近 20 根 K 线的 3 个最低点
    recent_highs = sorted(highs[-20:])[-3:]  # 最近 20 根 K 线的 3 个最高点
    
    avg_recent_low = sum(recent_lows) / len(recent_lows)
    avg_recent_high = sum(recent_highs) / len(recent_highs)
    
    # 解析清算数据（简化版）
    # 实际应从 Coinglass API 获取清算密集区
    liquidation_levels = liquidation_data.get('levels', []) if liquidation_data else []
    
    if side == 'LONG':
        # 多单：止损放在支撑位下方，止盈放在阻力位或清算区上方
        
        # 止损：最近支撑位下方 1-2 个 ATR
        sl_distance = atr * 1.5
        sl_price = min(avg_recent_low, entry_price * 0.98) - sl_distance
        sl_price = round(sl_price, 2)
        
        # 止盈：2:1 或 3:1 风险回报比
        risk = entry_price - sl_price
        reward_target = risk * 2.5  # 2.5:1 风险回报比
        tp_price = entry_price + reward_target
        
        # 检查是否有清算区在止盈目标附近
        if liquidation_levels:
            # 寻找上方的清算密集区
            above_liquidations = [l for l in liquidation_levels if l['price'] > entry_price]
            if above_liquidations:
                # 选择最近的清算密集区作为止盈目标
                nearest_liquidation = min(above_liquidations, key=lambda x: x['price'])
                tp_price = nearest_liquidation['price'] * 0.995  # 略低于清算区
        
        tp_price = round(tp_price, 2)
        
        rationale = f"多单：止损={sl_price} (支撑下方{sl_distance:.2f}), 止盈={tp_price} (R:R=2.5:1)"
    
    else:
        # 空单：止损放在阻力位上方，止盈放在支撑位或清算区下方
        
        # 止损：最近阻力位上方 1-2 个 ATR
        sl_distance = atr * 1.5
        sl_price = max(avg_recent_high, entry_price * 1.02) + sl_distance
        sl_price = round(sl_price, 2)
        
        # 止盈：2:1 或 3:1 风险回报比
        risk = sl_price - entry_price
        reward_target = risk * 2.5
        tp_price = entry_price - reward_target
        
        # 检查是否有清算区在止盈目标附近
        if liquidation_levels:
            # 寻找下方的清算密集区
            below_liquidations = [l for l in liquidation_levels if l['price'] < entry_price]
            if below_liquidations:
                # 选择最近的清算密集区作为止盈目标
                nearest_liquidation = min(below_liquidations, key=lambda x: abs(x['price'] - entry_price))
                tp_price = nearest_liquidation['price'] * 1.005  # 略高于清算区
        
        tp_price = round(tp_price, 2)
        
        rationale = f"空单：止损={sl_price} (阻力上方{sl_distance:.2f}), 止盈={tp_price} (R:R=2.5:1)"
    
    return sl_price, tp_price, rationale

def llm_sl_tp_decision(symbol, side, entry_price, filled_qty):
    """
    LLM 决策：计算具体的 SL/TP 位置
    这是 LLM-4 的核心调用点
    """
    api = BinanceAPI()
    
    # 获取 K 线数据
    klines = api.get_klines(symbol, interval='1h', limit=100)
    
    # 获取清算数据（通过 Coinglass API）
    # 简化版：空数据
    liquidation_data = {}
    
    # 计算 SL/TP
    sl_price, tp_price, rationale = calculate_sl_tp(
        symbol, side, entry_price, klines, liquidation_data
    )
    
    return {
        'symbol': symbol,
        'side': side,
        'entry_price': entry_price,
        'sl_price': sl_price,
        'tp_price': tp_price,
        'rationale': rationale,
        'action': 'place_orders'
    }

if __name__ == '__main__':
    # 测试
    test_cases = [
        ('BTCUSDT', 'LONG', 95000),
        ('ETHUSDT', 'SHORT', 3500),
        ('SOLUSDT', 'LONG', 180),
    ]
    
    results = []
    for symbol, side, entry_price in test_cases:
        print(f"\n测试 {symbol} {side} @ {entry_price}")
        result = llm_sl_tp_decision(symbol, side, entry_price, 0)
        print(f"  止损：{result['sl_price']}")
        print(f"  止盈：{result['tp_price']}")
        print(f"  依据：{result['rationale']}")
        results.append(result)
    
    # 保存结果
    import json
    with open('config/sl_tp_test_results.json', 'w') as f:
        json.dump(results, f, indent=2)
