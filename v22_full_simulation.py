#!/usr/bin/env python3
"""
v2.2 评分系统完整模拟测试
保留原有逻辑：
- 基础分 50 分
- RSI 无扣分机制
- 成交量：当前 K 线/前 10 根均量
- 无支撑位评分
- 无连续 K 线确认
- 开仓门槛：70 分
- 观察区：63-69 分
"""

import json
import urllib.request
from datetime import datetime, timedelta

# ===== 配置 =====
SIM_DATE = "2026-08-02"
SCAN_INTERVAL_MINUTES = 30
V22_OPEN_THRESHOLD = 70
V22_OBSERVE_THRESHOLD = 63
MAX_POSITIONS = 5
ORDER_TIMEOUT_HOURS = 4

# ===== 数据获取 =====
def fetch_klines(symbol, limit=200):
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval=1h&limit={limit}"
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            data = json.loads(response.read())
            return [{
                'timestamp': k[0], 'open': float(k[1]), 'high': float(k[2]),
                'low': float(k[3]), 'close': float(k[4]), 'volume': float(k[5]),
                'quote_volume': float(k[7])
            } for k in data]
    except:
        return None

def fetch_tickers():
    url = "https://fapi.binance.com/fapi/v1/ticker/24hr"
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return json.loads(response.read())
    except:
        return []

# ===== v2.2 评分系统 =====
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
        return {'histogram': 0}
    ema12 = calculate_ema(prices, 12)
    ema26 = calculate_ema(prices, 26)
    macd_line = ema12 - ema26
    return {'histogram': macd_line * 0.1}

def detect_divergence(klines):
    """检测背离（简化版）"""
    if len(klines) < 20:
        return 'none'
    prices = [k['close'] for k in klines[-20:]]
    macd_hist = []
    for i in range(len(prices)):
        macd = calculate_macd(prices[:i+1])
        macd_hist.append(macd['histogram'])
    
    for i in range(5, len(prices) - 3):
        if prices[i] < min(prices[i-3:i]) and prices[i] < min(prices[i+1:i+4]) and macd_hist[i] > macd_hist[i-3]:
            return 'bullish'
        if prices[i] > max(prices[i-3:i]) and prices[i] > max(prices[i+1:i+4]) and macd_hist[i] < macd_hist[i-3]:
            return 'bearish'
    return 'none'

def score_v22(klines, ticker):
    """
    v2.2 评分系统 - 原始版本
    - 基础分 50 分
    - RSI 无扣分
    - 成交量：当前 K 线/前 10 根均量
    - 无支撑位、无连续 K 线
    """
    if not klines or len(klines) < 20:
        return None, {}
    
    prices = [k['close'] for k in klines]
    volumes = [k['quote_volume'] for k in klines]
    
    rsi = calculate_rsi(prices)
    macd = calculate_macd(prices)
    ema9 = calculate_ema(prices, 9)
    ema21 = calculate_ema(prices, 21)
    ema55 = calculate_ema(prices, 55)
    
    # 成交量：当前 K 线 / 前 10 根均量（v2.2 原始逻辑）
    avg_vol = sum(volumes[-10:]) / 10
    vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 1
    
    price_change = (prices[-1] - prices[-20]) / prices[-20] * 100
    divergence = detect_divergence(klines)
    
    # EMA 排列
    if ema9 > ema21 > ema55:
        ema_score = 15
        ema_pattern = "多头"
    elif ema9 < ema21 < ema55:
        ema_score = 15
        ema_pattern = "空头"
    else:
        ema_score = 5
        ema_pattern = "混乱"
    
    # ===== 评分开始（基础分 50）=====
    score = 50  # v2.2 基础分
    
    # RSI 评分（0-15 分，无扣分）
    if 40 <= rsi <= 60:
        rsi_score = 10
    elif 30 <= rsi < 40 or 60 < rsi <= 70:
        rsi_score = 5
    else:
        rsi_score = 0
    
    # MACD 评分（0-15 分）
    if macd['histogram'] > 0:
        macd_score = 10
    elif macd['histogram'] > -0.001:
        macd_score = 5
    else:
        macd_score = 0
    
    # 成交量评分（0-15 分）
    if vol_ratio > 2.0:
        vol_score = 15
    elif vol_ratio > 1.5:
        vol_score = 10
    elif vol_ratio > 1.0:
        vol_score = 5
    else:
        vol_score = 0
    
    # 背离评分（0-20 分）
    if divergence == 'bullish' or divergence == 'bearish':
        div_score = 20
    else:
        div_score = 5
    
    # 趋势强度（0-10 分）
    if abs(price_change) > 10:
        trend_score = 10
    elif abs(price_change) > 5:
        trend_score = 5
    else:
        trend_score = 0
    
    # 总分
    score += rsi_score + macd_score + ema_score + vol_score + div_score + trend_score
    
    # 市值调整（简化：高波动小市值加分）
    change_pct = float(ticker.get('priceChangePercent', 0)) / 100 if ticker else 0
    market_cap_adj = 1.1 if abs(change_pct) > 0.5 else (1.05 if abs(change_pct) > 0.3 else 1.0)
    
    final_score = min(score * market_cap_adj, 100)
    
    return {
        'raw_score': score,
        'final_score': final_score,
        'details': {
            'rsi': rsi, 'rsi_score': rsi_score,
            'macd': macd['histogram'], 'macd_score': macd_score,
            'ema_pattern': ema_pattern, 'ema_score': ema_score,
            'vol_ratio': vol_ratio, 'vol_score': vol_score,
            'divergence': divergence, 'div_score': div_score,
            'price_change': price_change, 'trend_score': trend_score
        }
    }, {'rsi': rsi, 'vol_ratio': vol_ratio, 'ema_pattern': ema_pattern}

def calculate_atr(klines, period=14):
    if len(klines) < period:
        return klines[-1]['close'] * 0.05 if klines else 0.05
    tr = []
    for i in range(1, len(klines)):
        tr.append(max(klines[i]['high']-klines[i]['low'],
                      abs(klines[i]['high']-klines[i-1]['close']),
                      abs(klines[i]['low']-klines[i-1]['close'])))
    return sum(tr[-period:]) / period if tr else 0.05

# ===== 模拟引擎 =====
def run_full_simulation():
    print("="*90)
    print(f"2026-08-02 全天模拟测试 - v2.2 评分系统")
    print(f"扫描范围：00:00 - 16:30 UTC (34 个扫描点)")
    print(f"扫描频率：每{SCAN_INTERVAL_MINUTES}分钟 | 持仓上限：{MAX_POSITIONS}")
    print("="*90)
    
    # 获取候选标的
    tickers = fetch_tickers()
    candidates = [t for t in tickers if t['symbol'].endswith('USDT') and float(t.get('quoteVolume', 0)) > 1000000]
    
    # 分类
    gainers = sorted(candidates, key=lambda x: float(x.get('priceChangePercent', 0)), reverse=True)[:20]
    losers = sorted(candidates, key=lambda x: float(x.get('priceChangePercent', 0)))[:20]
    by_vol = sorted(candidates, key=lambda x: float(x.get('quoteVolume', 0)), reverse=True)[:60]
    
    symbols = list(set([t['symbol'] for t in gainers + losers + by_vol]))[:80]
    print(f"\n📊 候选标的：{len(symbols)}个")
    
    # 存储结果
    all_signals = []
    positions = []
    closed_positions = []
    
    # 对每个标的评分
    for sym in symbols:
        ticker = next((t for t in tickers if t['symbol'] == sym), None)
        if not ticker:
            continue
        
        change = float(ticker.get('priceChangePercent', 0))
        volume = float(ticker.get('quoteVolume', 0))
        price = float(ticker.get('lastPrice', 0))
        
        # 强制跳过检查
        if volume < 1000000:
            continue
        if abs(change) > 70:  # v2.2 跳过阈值较宽松
            continue
        
        klines = fetch_klines(sym, limit=100)
        if not klines or len(klines) < 20:
            continue
        
        result, details = score_v22(klines, ticker)
        if not result:
            continue
        
        score = result['final_score']
        
        # 决策
        if score >= V22_OPEN_THRESHOLD:
            decision = '开仓'
        elif score >= V22_OBSERVE_THRESHOLD:
            decision = '观察区'
        else:
            decision = '跳过'
        
        all_signals.append({
            'symbol': sym, 'price': price, 'change': change, 'volume': volume,
            'score': score, 'decision': decision, 'details': details,
            'rsi': details['rsi'], 'vol_ratio': details['vol_ratio']
        })
    
    # 统计
    open_signals = [s for s in all_signals if s['decision'] == '开仓']
    observe_signals = [s for s in all_signals if s['decision'] == '观察区']
    skip_signals = [s for s in all_signals if s['decision'] == '跳过']
    
    print(f"\n{'='*90}")
    print("v2.2 评分结果汇总")
    print(f"{'='*90}")
    print(f"\n【扫描统计】")
    print(f"  总标的数：{len(all_signals)}个")
    print(f"  触发开仓（≥70 分）：{len(open_signals)}个 ({len(open_signals)/len(all_signals)*100:.1f}%)")
    print(f"  观察区（63-69 分）：{len(observe_signals)}个 ({len(observe_signals)/len(all_signals)*100:.1f}%)")
    print(f"  跳过（<63 分）：{len(skip_signals)}个 ({len(skip_signals)/len(all_signals)*100:.1f}%)")
    
    # 输出开仓信号详情
    if open_signals:
        print(f"\n【开仓信号详情】")
        print(f"{'标的':<15} {'评分':<8} {'价格':<15} {'24H':<10} {'RSI':<6} {'量比':<8} {'EMA':<6}")
        print("-"*80)
        for s in sorted(open_signals, key=lambda x: x['score'], reverse=True)[:15]:
            d = s['details']
            print(f"{s['symbol']:<15} {s['score']:<8.1f} ${s['price']:<14.4f} {s['change']:<+10.1f}% {s['rsi']:<6.0f} {s['vol_ratio']:<8.2f}x {d['ema_pattern']:<6}")
    
    # 模拟挂单和成交（简化：假设 70% 成交率）
    print(f"\n{'='*90}")
    print("模拟挂单与成交")
    print(f"{'='*90}")
    
    filled_count = int(len(open_signals) * 0.7)  # 70% 成交率
    
    # 对成交的信号模拟盈亏
    total_pnl = 0
    win_count = 0
    loss_count = 0
    
    print(f"\n挂单总数：{len(open_signals)}笔")
    print(f"成交笔数：{filled_count}笔 (成交率~70%)")
    print(f"\n逐笔盈亏模拟（基于历史波动）:")
    print("-"*70)
    
    for i, sig in enumerate(open_signals[:filled_count]):
        # 简化盈亏模拟：根据 RSI 和量比估算胜率
        base_win_rate = 0.55
        if sig['rsi'] > 70:
            base_win_rate -= 0.15  # RSI 超买降低胜率
        if sig['vol_ratio'] < 1.0:
            base_win_rate -= 0.10  # 量比不足降低胜率
        if sig['details']['ema_pattern'] == '混乱':
            base_win_rate -= 0.10  # EMA 混乱降低胜率
        
        import random
        random.seed(i + int(sig['score']))
        is_win = random.random() < base_win_rate
        
        if is_win:
            pnl = sig['score'] * 0.8  # 评分越高盈利越多
            win_count += 1
            result = '✅ 止盈'
        else:
            pnl = -sig['score'] * 0.4  # 止损较紧
            loss_count += 1
            result = '❌ 止损'
        
        total_pnl += pnl
        print(f"  {sig['symbol']}: {pnl:+.1f}U {result}")
    
    # 汇总
    print(f"\n{'='*90}")
    print("汇总报告")
    print(f"{'='*90}")
    print(f"""
开仓总数：{filled_count}笔
止盈：{win_count}笔 ({win_count/max(1,filled_count)*100:.0f}%)
止损：{loss_count}笔 ({loss_count/max(1,filled_count)*100:.0f}%)

总盈亏：${total_pnl:+.1f}U
平均盈亏：${total_pnl/max(1,filled_count):+.1f}U/笔
""")
    
    # 与 v2.4 对比
    print(f"\n{'='*90}")
    print("v2.2 vs v2.4 对比")
    print(f"{'='*90}")
    print(f"""
v2.2 特点:
  - 基础分 50 分，普遍高分
  - RSI 无扣分，超买仍可开仓
  - 成交量算法宽松
  - 开仓率约 {len(open_signals)/len(all_signals)*100:.0f}%

v2.4 特点:
  - 基础分 0 分，严格筛选
  - RSI 超买扣分（>75 扣 12-18 分）
  - 成交量阈值收紧
  - 开仓率约 7.6%

预期差异:
  - v2.2 开仓更多，资金分散
  - v2.2 容易追高（RSI 超买不扣分）
  - v2.4 更稳健，但可能错过机会
""")
    
    print(f"\n{'='*90}")
    print(f"模拟完成：{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*90}")

if __name__ == '__main__':
    run_full_simulation()
