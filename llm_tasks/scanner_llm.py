"""
LLM-1: Scanner 主扫描任务（每 30 分钟）
需要：拉取实时数据 + 综合判断入场时机
输入：评分结果 + 清算图数据 + 技术位分析
输出：是否挂单 + 挂单位置 + 仓位大小
"""
import sys
sys.path.insert(0, '/root/.openclaw/workspace')

from modules.scoring import ScoringEngine
from modules.circuit_breaker import CircuitBreaker
from modules.position_state import PositionState
from config.market_rules import MarketFilter
from binance_api import BinanceAPI
import json

def fetch_scan_candidates():
    """
    获取扫描标的池
    - 涨幅榜前 20
    - 跌幅榜前 20
    - 成交量前 60
    - 热度榜前 10
    去重后约 90~100 个标的
    """
    api = BinanceAPI()
    
    # 获取 24h 行情
    tickers = api.get_tickers_24h()
    
    # 涨幅榜
    gainers = sorted(tickers, key=lambda x: float(x.get('priceChangePercent', 0)), reverse=True)[:20]
    
    # 跌幅榜
    losers = sorted(tickers, key=lambda x: float(x.get('priceChangePercent', 0)))[:20]
    
    # 成交量榜
    by_volume = sorted(tickers, key=lambda x: float(x.get('quoteVolume', 0)), reverse=True)[:60]
    
    # 热度榜（通过 binance-pro-cli 获取）
    # 这里简化处理，实际应调用 binance-pro-cli search hotrank
    hotrank = []  # 待实现
    
    # 去重
    symbols = set()
    candidates = []
    
    for ticker_list in [gainers, losers, by_volume, hotrank]:
        for ticker in ticker_list:
            symbol = ticker['symbol']
            if symbol not in symbols and symbol.endswith('USDT'):
                symbols.add(symbol)
                candidates.append(ticker)
    
    return candidates

def analyze_symbol(symbol, api):
    """
    分析单个标的
    返回：(score, details, klines, liquidation_data)
    """
    # 获取 K 线数据
    klines = api.get_klines(symbol, interval='1h', limit=100)
    if not klines:
        return None, None, None, None
    
    # 计算技术指标
    closes = [float(k[4]) for k in klines]
    volumes = [float(k[5]) for k in klines]
    
    # 简化版指标计算（实际应使用 talib 或 pandas_ta）
    rsi = 50  # 待实现
    macd_hist = 0  # 待实现
    ema_alignment = 50  # 待实现
    volume_ratio = volumes[-1] / (sum(volumes[-10:-1]) / 9) if len(volumes) > 10 else 1.0
    
    details = {
        'rsi': rsi,
        'macd_hist': macd_hist,
        'ema_alignment': ema_alignment,
        'volume_ratio': volume_ratio,
        'support_strength': 50,  # 待实现
        'resistance_strength': 50,  # 待实现
        'risk_reward_ratio': 2.0,  # 待实现
        'confidence': 0.7  # 待实现
    }
    
    # 计算评分
    score, breakdown = ScoringEngine.calc_score_trend(details)
    
    # 获取清算数据（通过 Coinglass API）
    liquidation_data = {}  # 待实现
    
    return score, details, klines, liquidation_data

def check_liquidation_condition(symbol, side, liquidation_data, position_size):
    """
    检查清算密集区条件
    仅同时满足以下全部条件才走双网：
    - 信号评分 ≥ 85 分
    - 突破位置上方（多单）/下方（空单）清算密集区足够大
    - 清算量 > 当前持仓量的 10%
    """
    if not liquidation_data:
        return False, "无清算数据"
    
    # 简化版检查（实际应解析 Coinglass 清算图）
    liquidation_volume = liquidation_data.get('liquidation_volume', 0)
    
    if liquidation_volume > position_size * 0.10:
        return True, f"清算量{liquidation_volume} > 持仓 10%"
    
    return False, f"清算量不足"

def llm_scanner_decision(score, details, klines, liquidation_data, symbol):
    """
    LLM 决策：是否挂单 + 挂单位置 + 仓位大小
    这是 LLM-1 的核心调用点
    """
    # 这里应该调用 LLM API
    # 当前用规则引擎模拟
    
    if score < 70:
        return {
            'action': 'skip',
            'reason': f'评分{score} < 70'
        }
    
    if score >= 85:
        # 高分标的，检查清算条件
        position_size = details.get('position_size', 1000)
        use_dual, liquidation_reason = check_liquidation_condition(
            symbol, 'LONG', liquidation_data, position_size
        )
        
        if use_dual:
            return {
                'action': 'dual_net',
                'reason': f'评分{score} >= 85, {liquidation_reason}',
                'entry_price': klines[-1][4],  # 最新收盘价
                'position_size': position_size,
                'tp_price': details.get('tp_price', 0),
                'sl_price': details.get('sl_price', 0)
            }
        else:
            return {
                'action': 'limit_order',
                'reason': f'评分{score} >= 85, 但{liquidation_reason}',
                'entry_price': klines[-1][4],
                'position_size': position_size,
                'tp_price': details.get('tp_price', 0),
                'sl_price': details.get('sl_price', 0)
            }
    
    return {
        'action': 'limit_order',
        'reason': f'评分{score} 在 70-85 之间',
        'entry_price': klines[-1][4],
        'position_size': details.get('position_size', 500),
        'tp_price': details.get('tp_price', 0),
        'sl_price': details.get('sl_price', 0)
    }

def run_scanner():
    """
    主扫描流程
    """
    print("=== Scanner 主扫描任务启动 ===")
    
    # 检查熔断器
    cb = CircuitBreaker()
    is_open, reason = cb.check()
    if is_open:
        print(f"熔断器开启，跳过扫描：{reason}")
        return
    
    # 检查市场环境和持仓上限
    mf = MarketFilter()
    regime = mf.check_btc_filter()
    limits = mf.get_position_limits()
    max_long, max_short, max_total = limits
    
    print(f"市场模式：{regime}")
    print(f"持仓上限：多单{max_long}, 空单{max_short}, 总计{max_total}")
    
    # 获取当前持仓数
    api = BinanceAPI()
    positions = api.get_positions()
    long_count = sum(1 for p in positions if float(p['positionAmt']) > 0)
    short_count = sum(1 for p in positions if float(p['positionAmt']) < 0)
    
    print(f"当前持仓：多单{long_count}, 空单{short_count}")
    
    # 检查是否达到持仓上限
    if long_count >= max_long:
        print(f"多单已达上限{max_long}，跳过新多单")
    if short_count >= max_short:
        print(f"空单已达上限{max_short}，跳过新空单")
    if len(positions) >= max_total:
        print(f"总持仓已达上限{max_total}，跳过新开仓")
        return
    
    # 获取候选标的
    candidates = fetch_scan_candidates()
    print(f"候选标的数量：{len(candidates)}")
    
    # 逐个分析
    decisions = []
    for ticker in candidates[:10]:  # 限制前 10 个测试
        symbol = ticker['symbol']
        print(f"\n分析 {symbol}...")
        
        score, details, klines, liquidation_data = analyze_symbol(symbol, api)
        
        if not score:
            print(f"  跳过：无法获取数据")
            continue
        
        print(f"  评分：{score}")
        
        # LLM 决策
        decision = llm_scanner_decision(score, details, klines, liquidation_data, symbol)
        decision['symbol'] = symbol
        decisions.append(decision)
        
        print(f"  决策：{decision['action']} - {decision['reason']}")
    
    # 汇总结果
    print("\n=== 扫描完成 ===")
    print(f"分析标的：{len(decisions)}")
    print(f"建议开仓：{sum(1 for d in decisions if d['action'] != 'skip')}")
    
    return decisions

if __name__ == '__main__':
    decisions = run_scanner()
    
    # 保存结果
    with open('config/scanner_results.json', 'w') as f:
        json.dump({
            'timestamp': __import__('datetime').datetime.utcnow().isoformat(),
            'decisions': decisions
        }, f, indent=2)
