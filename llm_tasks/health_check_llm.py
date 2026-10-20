"""
LLM-3: 持仓健康检查（每 30 分钟）
需要：对持仓重新评分 + 判断是否平仓
原因：需要重新分析技术结构
- 评分<35 且持仓>60 分钟 → 判断是否平仓
- 聪明钱反向 → 判断是否平仓
输出：继续持有 / 主动平仓
"""
import sys
sys.path.insert(0, '/root/.openclaw/workspace')

from modules.scoring import ScoringEngine
from modules.position_state import PositionState
from binance_api import BinanceAPI
import json
from datetime import datetime, timedelta

def analyze_position_health(symbol, position, api):
    """
    分析单个持仓的健康状况
    返回：(current_score, breakdown, klines, smart_money_signal)
    """
    # 获取最新 K 线
    klines = api.get_klines(symbol, interval='1h', limit=100)
    if not klines:
        return None, None, None, None
    
    closes = [float(k[4]) for k in klines]
    current_price = closes[-1]
    entry_price = position['entry_price']
    side = position['side']
    
    # 计算未实现盈亏
    if side == 'LONG':
        pnl_pct = (current_price - entry_price) / entry_price * 100
    else:
        pnl_pct = (entry_price - current_price) / entry_price * 100
    
    # 重新计算评分
    # 简化版（实际应完整计算）
    rsi = 50  # 待实现
    macd_hist = 0  # 待实现
    ema_alignment = 50  # 待实现
    
    details = {
        'rsi': rsi,
        'macd_hist': macd_hist,
        'ema_alignment': ema_alignment,
        'volume_ratio': 1.0,
        'support_strength': 50,
        'resistance_strength': 50,
        'risk_reward_ratio': 1.5,
        'confidence': 0.6
    }
    
    # 根据方向选择评分方法
    if side == 'LONG':
        score, breakdown = ScoringEngine.calc_score_trend(details)
    else:
        score, breakdown = ScoringEngine.calc_score_trend_short(details)
    
    # 检查聪明钱信号（通过 trading-signal skill）
    # 简化版：假设聪明钱信号与持仓方向相反
    smart_money_signal = 'neutral'  # 待实现：bullish/bearish/neutral
    
    return score, breakdown, klines, smart_money_signal

def llm_health_decision(score, pnl_pct, position_age_minutes, smart_money_signal, position):
    """
    LLM 决策：继续持有 / 主动平仓
    规则：
    - 评分<35 且持仓>60 分钟 → 判断是否平仓
    - 聪明钱反向 → 判断是否平仓
    """
    symbol = position['symbol']
    side = position['side']
    
    # 规则 1: 低评分 + 长时间持仓
    if score < 35 and position_age_minutes > 60:
        return {
            'action': 'close',
            'reason': f'评分{score} < 35 且持仓{position_age_minutes}分钟 > 60 分钟',
            'priority': 'high',
            'symbol': symbol
        }
    
    # 规则 2: 聪明钱反向信号
    if smart_money_signal != 'neutral':
        if side == 'LONG' and smart_money_signal == 'bearish':
            return {
                'action': 'close',
                'reason': f'聪明钱转向做空',
                'priority': 'high',
                'symbol': symbol
            }
        elif side == 'SHORT' and smart_money_signal == 'bullish':
            return {
                'action': 'close',
                'reason': f'聪明钱转向做多',
                'priority': 'high',
                'symbol': symbol
            }
    
    # 规则 3: 大额亏损（>5%）
    if pnl_pct < -5:
        return {
            'action': 'close',
            'reason': f'亏损{pnl_pct:.2f}% > 5%',
            'priority': 'medium',
            'symbol': symbol
        }
    
    # 规则 4: 评分仍然良好
    if score >= 70:
        return {
            'action': 'hold',
            'reason': f'评分{score} >= 70，趋势健康',
            'priority': 'low',
            'symbol': symbol
        }
    
    # 默认：继续观察
    return {
        'action': 'hold',
        'reason': f'评分{score} 中等，继续观察',
        'priority': 'low',
        'symbol': symbol
    }

def run_health_check():
    """
    主流程：检查所有持仓
    """
    print("=== 持仓健康检查启动 ===")
    
    # 加载仓位状态
    ps = PositionState()
    positions = ps.get_all_positions()
    
    if not positions:
        print("当前无持仓")
        return []
    
    print(f"检查 {len(positions)} 个持仓...\n")
    
    api = BinanceAPI()
    decisions = []
    
    for symbol, position in positions.items():
        print(f"检查 {symbol}...")
        
        # 分析持仓健康状况
        score, breakdown, klines, smart_money_signal = analyze_position_health(
            symbol, position, api
        )
        
        if not score:
            print(f"  跳过：无法获取数据")
            continue
        
        # 计算持仓时长
        opened_at = datetime.fromisoformat(position['opened_at'])
        position_age_minutes = (datetime.utcnow() - opened_at).total_seconds() / 60
        
        # 计算未实现盈亏
        current_price = float(klines[-1][4])
        entry_price = position['entry_price']
        side = position['side']
        
        if side == 'LONG':
            pnl_pct = (current_price - entry_price) / entry_price * 100
        else:
            pnl_pct = (entry_price - current_price) / entry_price * 100
        
        print(f"  当前评分：{score}")
        print(f"  持仓时长：{position_age_minutes:.0f}分钟")
        print(f"  未实现盈亏：{pnl_pct:.2f}%")
        print(f"  聪明钱信号：{smart_money_signal}")
        
        # LLM 决策
        decision = llm_health_decision(
            score, pnl_pct, position_age_minutes, smart_money_signal, position
        )
        decisions.append(decision)
        
        print(f"  决策：{decision['action']} - {decision['reason']}")
        print()
    
    # 汇总结果
    print("=== 健康检查完成 ===")
    close_count = sum(1 for d in decisions if d['action'] == 'close')
    hold_count = sum(1 for d in decisions if d['action'] == 'hold')
    print(f"建议平仓：{close_count}")
    print(f"建议持有：{hold_count}")
    
    # 执行平仓（高优先级）
    high_priority_closes = [d for d in decisions if d['action'] == 'close' and d['priority'] == 'high']
    
    if high_priority_closes:
        print(f"\n执行 {len(high_priority_closes)} 个高优先级平仓...")
        for decision in high_priority_closes:
            symbol = decision['symbol']
            print(f"  平仓 {symbol}...")
            # 实际应调用 api.close_position(symbol)
            # 这里仅标记
            ps.remove_position(symbol)
    
    return decisions

if __name__ == '__main__':
    decisions = run_health_check()
    
    # 保存结果
    with open('config/health_check_results.json', 'w') as f:
        json.dump({
            'timestamp': datetime.utcnow().isoformat(),
            'decisions': decisions
        }, f, indent=2)
