#!/usr/bin/env python3
"""
v2.4 开仓信号模拟追踪 - 优化版
使用更现实的挂单策略：
- 强势币（24H>+10%）：挂当前价或 +0.5%（市价成交）
- 震荡币（24H±5%）：挂 -1%
- 弱势币（24H<-5%）：挂 -2%
"""

import json
import urllib.request
from datetime import datetime, timedelta

def fetch_klines(symbol, limit=200):
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval=1h&limit={limit}"
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            data = json.loads(response.read())
            return [{
                'timestamp': k[0], 'open': float(k[1]), 'high': float(k[2]),
                'low': float(k[3]), 'close': float(k[4]), 'quote_volume': float(k[7])
            } for k in data]
    except Exception as e:
        return None

def fetch_ticker(symbol):
    url = f"https://fapi.binance.com/fapi/v1/ticker/24hr?symbol={symbol}"
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return json.loads(response.read())
    except:
        return None

def calculate_atr(klines, period=14):
    if len(klines) < period:
        return klines[-1]['close'] * 0.05 if klines else 0.05
    tr = []
    for i in range(1, len(klines)):
        tr.append(max(klines[i]['high']-klines[i]['low'], 
                      abs(klines[i]['high']-klines[i-1]['close']), 
                      abs(klines[i]['low']-klines[i-1]['close'])))
    return sum(tr[-period:]) / period if tr else 0.05

def simulate_order_realistic(symbol, klines, ticker):
    """
    优化版模拟：根据 24H 涨跌幅决定挂单策略
    """
    if not klines or len(klines) < 30:
        return None
    
    change_24h = float(ticker.get('priceChangePercent', 0)) if ticker else 0
    current_price = klines[-1]['close']
    current_time = datetime.utcfromtimestamp(klines[-1]['timestamp'] / 1000)
    
    # 根据 24H 涨跌决定挂单价格
    if change_24h > 10:
        # 强势币：挂当前价 +0.5%（追涨）
        order_price = current_price * 1.005
        order_type = '追涨'
    elif change_24h < -5:
        # 弱势币：挂 -2%（等回踩）
        order_price = current_price * 0.98
        order_type = '回踩'
    else:
        # 震荡币：挂 -1%
        order_price = current_price * 0.99
        order_type = '震荡'
    
    # 计算 ATR 和止损止盈
    atr = calculate_atr(klines)
    stop_loss = order_price - atr * 1.5
    tp1 = order_price + atr * 2
    tp2 = order_price + atr * 3
    position_size = 440  # 假设 440U/笔
    
    result = {
        'symbol': symbol,
        'signal_time': current_time,
        'signal_price': current_price,
        'change_24h': change_24h,
        'order_type': order_type,
        'order_price': order_price,
        'stop_loss': stop_loss,
        'tp1': tp1,
        'tp2': tp2,
        'position_size': position_size,
        'status': 'pending',
        'fill_price': None,
        'fill_time': None,
        'exit_price': None,
        'exit_time': None,
        'exit_reason': None,
        'pnl': 0,
        'pnl_pct': 0,
        'max_profit_pct': 0,
        'max_loss_pct': 0,
        'current_price': current_price
    }
    
    # 检查挂单后 K 线（用过去 20 根 K 线模拟未来走势）
    # 简化：假设挂单后立即成交（市价单逻辑）
    result['status'] = 'filled'
    result['fill_price'] = current_price  # 按当前价成交
    result['fill_time'] = current_time
    
    # 用过去 20 根 K 线的波动模拟持仓期间走势
    past_klines = klines[-20:-1]  # 用信号前 20 根 K 线模拟
    if len(past_klines) > 0:
        # 查找是否触及止盈止损
        for k in past_klines:
            # 更新最大盈亏
            profit_pct = (k['high'] - order_price) / order_price * 100
            loss_pct = (k['low'] - order_price) / order_price * 100
            result['max_profit_pct'] = max(result['max_profit_pct'], profit_pct)
            result['max_loss_pct'] = min(result['max_loss_pct'], loss_pct)
            
            # 检查止损
            if k['low'] <= stop_loss and not result.get('stopped'):
                result['status'] = 'closed'
                result['exit_price'] = stop_loss
                result['exit_time'] = current_time
                result['exit_reason'] = 'stop_loss'
                result['pnl'] = position_size * (stop_loss - order_price) / order_price
                result['pnl_pct'] = (stop_loss - order_price) / order_price * 100
                result['stopped'] = True
                break
            
            # 检查止盈 2
            if k['high'] >= tp2 and not result.get('tp2_hit'):
                result['status'] = 'closed'
                result['exit_price'] = tp2
                result['exit_time'] = current_time
                result['exit_reason'] = 'tp2'
                result['pnl'] = position_size * (tp2 - order_price) / order_price
                result['pnl_pct'] = (tp2 - order_price) / order_price * 100
                result['tp2_hit'] = True
                break
        
        # 如果仍未平仓，记录当前状态
        if result['status'] == 'filled':
            result['pnl'] = position_size * (result['current_price'] - order_price) / order_price
            result['pnl_pct'] = (result['current_price'] - order_price) / order_price * 100
    
    return result

def main():
    print("="*90)
    print("v2.4 开仓信号模拟追踪 - 优化版")
    print("使用动态挂单策略（根据 24H 涨跌幅调整）")
    print("="*90)
    
    # 6 个开仓信号
    signals = [
        '1000PEPEUSDT', 'XAUUSDT', '1000SHIBUSDT', 
        'ZECUSDT', 'LINKUSDT', 'HYPERUSDT'
    ]
    
    results = []
    
    for symbol in signals:
        print(f"\n{'='*90}")
        print(f"标的：{symbol}")
        print(f"{'='*90}")
        
        klines = fetch_klines(symbol, limit=200)
        ticker = fetch_ticker(symbol)
        
        if not klines or len(klines) < 30:
            print("❌ 数据不足，跳过")
            continue
        
        current_price = klines[-1]['close']
        change_24h = float(ticker.get('priceChangePercent', 0)) if ticker else 0
        
        print(f"当前价格：${current_price:.6f}")
        print(f"24H 涨跌：{change_24h:+.1f}%")
        
        result = simulate_order_realistic(symbol, klines, ticker)
        if not result:
            print("❌ 模拟失败")
            continue
        
        results.append(result)
        
        # 输出挂单详情
        print(f"\n挂单策略：{result['order_type']}")
        print(f"  挂单价格：${result['order_price']:.6f}")
        print(f"  止损：${result['stop_loss']:.6f} ({(result['stop_loss']/result['order_price']-1)*100:.1f}%)")
        print(f"  止盈 1: ${result['tp1']:.6f} (+{(result['tp1']/result['order_price']-1)*100:.1f}%)")
        print(f"  止盈 2: ${result['tp2']:.6f} (+{(result['tp2']/result['order_price']-1)*100:.1f}%)")
        print(f"  仓位：{result['position_size']:.0f}U")
        
        # 成交状态
        print(f"\n成交状态:")
        if result['status'] == 'filled':
            print(f"  ✅ 已成交 @ ${result['fill_price']:.6f}")
            print(f"  成交时间：{result['fill_time']}")
        elif result['status'] == 'closed':
            print(f"  ✅ 已成交 @ ${result['fill_price']:.6f}")
            print(f"  成交时间：{result['fill_time']}")
        
        # 平仓/持仓详情
        if result['status'] == 'closed':
            print(f"\n平仓详情:")
            print(f"  平仓价格：${result['exit_price']:.6f}")
            print(f"  平仓原因：{result['exit_reason']}")
            print(f"  盈亏：${result['pnl']:+.1f}U ({result['pnl_pct']:+.1f}%)")
        elif result['status'] == 'filled':
            print(f"\n当前持仓:")
            print(f"  当前价格：${result['current_price']:.6f}")
            print(f"  浮盈浮亏：${result['pnl']:+.1f}U ({result['pnl_pct']:+.1f}%)")
            print(f"  期间最高：+{result['max_profit_pct']:.1f}%")
            print(f"  期间最低：{result['max_loss_pct']:.1f}%")
    
    # 汇总
    print(f"\n{'='*90}")
    print("汇总报告")
    print(f"{'='*90}")
    
    total = len(results)
    filled = sum(1 for r in results if r['status'] in ['filled'])
    closed_tp = sum(1 for r in results if r.get('exit_reason') == 'tp2')
    closed_sl = sum(1 for r in results if r.get('exit_reason') == 'stop_loss')
    still_open = sum(1 for r in results if r['status'] == 'filled')
    
    total_pnl = sum(r['pnl'] for r in results if r['status'] == 'closed')
    floating_pnl = sum(r['pnl'] for r in results if r['status'] == 'filled')
    
    print(f"""
挂单总数：{total}笔
成交笔数：{filled}笔 (成交率 {filled/total*100:.0f}%)
止盈出局：{closed_tp}笔
止损出局：{closed_sl}笔
当前持仓：{still_open}笔

已实现盈亏：${total_pnl:+.1f}U
当前浮盈浮亏：${floating_pnl:+.1f}U
总盈亏：${total_pnl + floating_pnl:+.1f}U
""")
    
    # 详细列表
    print(f"\n逐笔记录:")
    print(f"{'标的':<15} {'24H':<10} {'策略':<8} {'入金价':<12} {'当前/退出':<12} {'盈亏':<12}")
    print("-"*75)
    for r in results:
        status = '✅ 持仓' if r['status']=='filled' else ('✅ 止盈' if r.get('exit_reason')=='tp2' else ('❌ 止损' if r.get('exit_reason')=='stop_loss' else '?'))
        change = f"{r['change_24h']:+.1f}%"
        strategy = r['order_type']
        entry = f"${r['fill_price']:.4f}" if r['fill_price'] else '-'
        current_exit = f"${r['exit_price']:.4f}" if r.get('exit_price') else f"${r['current_price']:.4f}"
        pnl = f"${r['pnl']:+.1f}U" if r['pnl'] != 0 else '$0.0U'
        print(f"{r['symbol']:<15} {change:<10} {strategy:<8} {entry:<12} {current_exit:<12} {pnl:<12}")
    
    print(f"\n{'='*90}")
    print(f"追踪完成：{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*90}")

if __name__ == '__main__':
    main()
