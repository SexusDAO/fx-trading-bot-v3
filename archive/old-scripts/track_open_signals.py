#!/usr/bin/env python3
"""
追踪 v2.4 开仓信号的模拟成交和持仓情况
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
                'timestamp': k[0],
                'open': float(k[1]), 'high': float(k[2]),
                'low': float(k[3]), 'close': float(k[4]),
                'quote_volume': float(k[7])
            } for k in data]
    except Exception as e:
        print(f"获取 {symbol} 失败：{e}")
        return None

def calculate_atr(klines, period=14):
    if len(klines) < period:
        return klines[-1]['close'] * 0.05 if klines else 0.05
    tr = []
    for i in range(1, len(klines)):
        high = klines[i]['high']
        low = klines[i]['low']
        prev_close = klines[i-1]['close']
        tr.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return sum(tr[-period:]) / period if tr else 0.05

def simulate_order(symbol, signal_time_idx, klines):
    """
    模拟挂单和成交
    挂单规则：限价单，多单挂在当前价下方 2%
    有效期：4 小时
    """
    if signal_time_idx >= len(klines):
        return None
    
    signal_kline = klines[signal_time_idx]
    signal_price = signal_kline['close']
    signal_time = datetime.utcfromtimestamp(signal_kline['timestamp'] / 1000)
    
    # 挂单价格：-2% 限价
    order_price = signal_price * 0.98
    expire_time = signal_time + timedelta(hours=4)
    
    # 计算 ATR 和止损止盈
    atr = calculate_atr(klines[:signal_time_idx+1]) if signal_time_idx >= 14 else signal_price * 0.05
    stop_loss = order_price - atr * 1.5
    tp1 = order_price + atr * 2
    tp2 = order_price + atr * 3
    position_size = 300 + (70 - 60) * 14  # 假设评分 70 分，约 440U
    
    # 追踪挂单后 4 小时内的 K 线
    result = {
        'symbol': symbol,
        'signal_time': signal_time,
        'signal_price': signal_price,
        'order_price': order_price,
        'expire_time': expire_time,
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
        'max_profit': 0,
        'max_loss': 0,
        'current_price': klines[-1]['close'] if klines else signal_price
    }
    
    # 检查 4 小时内是否成交
    end_idx = min(signal_time_idx + 4, len(klines))
    for i in range(signal_time_idx + 1, end_idx):
        k = klines[i]
        k_time = datetime.utcfromtimestamp(k['timestamp'] / 1000)
        
        # 检查是否触及挂单价（多单：低价触及）
        if k['low'] <= order_price:
            result['status'] = 'filled'
            result['fill_price'] = order_price
            result['fill_time'] = k_time
            result['exit_reason'] = 'pending'
            
            # 成交后追踪止盈止损
            for j in range(i + 1, len(klines)):
                k2 = klines[j]
                
                # 更新最大盈亏
                if k2['high'] > tp1 and not result.get('tp1_hit'):
                    result['max_profit'] = max(result['max_profit'], (k2['high'] - order_price) / order_price * 100)
                if k2['low'] < order_price:
                    result['max_loss'] = min(result['max_loss'], (k2['low'] - order_price) / order_price * 100)
                
                # 检查止损
                if k2['low'] <= stop_loss:
                    result['status'] = 'closed'
                    result['exit_price'] = stop_loss
                    result['exit_time'] = datetime.utcfromtimestamp(k2['timestamp'] / 1000)
                    result['exit_reason'] = 'stop_loss'
                    result['pnl'] = position_size * (stop_loss - order_price) / order_price
                    result['pnl_pct'] = (stop_loss - order_price) / order_price * 100
                    break
                
                # 检查止盈 2
                if k2['high'] >= tp2:
                    result['status'] = 'closed'
                    result['exit_price'] = tp2
                    result['exit_time'] = datetime.utcfromtimestamp(k2['timestamp'] / 1000)
                    result['exit_reason'] = 'tp2'
                    result['pnl'] = position_size * (tp2 - order_price) / order_price
                    result['pnl_pct'] = (tp2 - order_price) / order_price * 100
                    break
                
                # 检查止盈 1（部分止盈，简化为全部止盈）
                if k2['high'] >= tp1 and not result.get('tp1_hit'):
                    result['tp1_hit'] = True
                    # 止损上移保本
                    stop_loss = order_price
            
            # 如果仍未平仓，记录当前状态
            if result['status'] == 'filled':
                result['pnl'] = position_size * (result['current_price'] - order_price) / order_price
                result['pnl_pct'] = (result['current_price'] - order_price) / order_price * 100
            
            break
        else:
            # 检查是否超时
            if k_time > expire_time:
                result['status'] = 'cancelled'
                result['exit_reason'] = 'timeout'
                break
    
    return result

def main():
    print("="*90)
    print("v2.4 开仓信号模拟追踪")
    print("追踪 6 个开仓信号的挂单、成交、止盈止损情况")
    print("="*90)
    
    # 6 个开仓信号
    signals = [
        {'symbol': '1000PEPEUSDT', 'score': 82, 'price': None},
        {'symbol': 'XAUUSDT', 'score': 82, 'price': None},
        {'symbol': '1000SHIBUSDT', 'score': 76, 'price': None},
        {'symbol': 'ZECUSDT', 'score': 72, 'price': None},
        {'symbol': 'LINKUSDT', 'score': 72, 'price': None},
        {'symbol': 'HYPERUSDT', 'score': 72, 'price': None},
    ]
    
    results = []
    
    for sig in signals:
        print(f"\n{'='*90}")
        print(f"标的：{sig['symbol']} (评分：{sig['score']}分)")
        print(f"{'='*90}")
        
        klines = fetch_klines(sig['symbol'], limit=200)
        if not klines or len(klines) < 50:
            print("❌ 数据不足，跳过")
            continue
        
        # 使用最新 K 线作为信号时间点（模拟）
        signal_idx = len(klines) - 1
        sig['price'] = klines[signal_idx]['close']
        
        print(f"信号时间：{datetime.utcfromtimestamp(klines[signal_idx]['timestamp']/1000)}")
        print(f"信号价格：${sig['price']:.6f}")
        
        # 模拟挂单
        result = simulate_order(sig['symbol'], signal_idx, klines)
        if not result:
            print("❌ 模拟失败")
            continue
        
        results.append(result)
        
        # 输出结果
        print(f"\n挂单详情:")
        print(f"  挂单价格：${result['order_price']:.6f} (-2% 限价)")
        print(f"  止损：${result['stop_loss']:.6f} ({(result['stop_loss']/result['order_price']-1)*100:.1f}%)")
        print(f"  止盈 1: ${result['tp1']:.6f} (+{(result['tp1']/result['order_price']-1)*100:.1f}%)")
        print(f"  止盈 2: ${result['tp2']:.6f} (+{(result['tp2']/result['order_price']-1)*100:.1f}%)")
        print(f"  仓位：{result['position_size']:.0f}U")
        print(f"  有效期：4 小时")
        
        print(f"\n成交状态:")
        if result['status'] == 'pending':
            print(f"  ❌ 未成交（挂单后价格未回踩至${result['order_price']:.6f}）")
        elif result['status'] == 'filled':
            print(f"  ✅ 已成交 @ ${result['fill_price']:.6f}")
            print(f"  成交时间：{result['fill_time']}")
        elif result['status'] == 'cancelled':
            print(f"  ⏰ 超时撤单（4 小时内未触及挂单价）")
        elif result['status'] == 'closed':
            print(f"  ✅ 已成交 @ ${result['fill_price']:.6f}")
            print(f"  成交时间：{result['fill_time']}")
        
        if result['status'] == 'closed':
            print(f"\n平仓详情:")
            print(f"  平仓价格：${result['exit_price']:.6f}")
            print(f"  平仓时间：{result['exit_time']}")
            print(f"  平仓原因：{result['exit_reason']}")
            print(f"  盈亏：${result['pnl']:+.1f}U ({result['pnl_pct']:+.1f}%)")
        elif result['status'] == 'filled':
            print(f"\n当前持仓:")
            print(f"  当前价格：${result['current_price']:.6f}")
            print(f"  浮盈浮亏：${result['pnl']:+.1f}U ({result['pnl_pct']:+.1f}%)")
            print(f"  期间最高：+{result['max_profit']:.1f}%")
            print(f"  期间最低：{result['max_loss']:.1f}%")
    
    # 汇总
    print(f"\n{'='*90}")
    print("汇总报告")
    print(f"{'='*90}")
    
    total = len(results)
    filled = sum(1 for r in results if r['status'] in ['filled', 'closed'])
    cancelled = sum(1 for r in results if r['status'] == 'cancelled')
    closed_tp = sum(1 for r in results if r.get('exit_reason') == 'tp2')
    closed_sl = sum(1 for r in results if r.get('exit_reason') == 'stop_loss')
    
    total_pnl = sum(r['pnl'] for r in results if r['status'] == 'closed')
    floating_pnl = sum(r['pnl'] for r in results if r['status'] == 'filled')
    
    print(f"""
挂单总数：{total}笔
成交笔数：{filled}笔 (成交率 {filled/total*100:.0f}%)
超时撤单：{cancelled}笔
止盈出局：{closed_tp}笔
止损出局：{closed_sl}笔

已实现盈亏：${total_pnl:+.1f}U
当前浮盈浮亏：${floating_pnl:+.1f}U
""")
    
    # 详细列表
    print(f"\n逐笔记录:")
    print(f"{'标的':<15} {'状态':<10} {'入金价':<12} {'当前/出金价':<12} {'盈亏':<12}")
    print("-"*70)
    for r in results:
        status_map = {'pending': '❌ 未成交', 'filled': '✅ 持仓中', 'closed': '✅ 已平仓', 'cancelled': '⏰ 超时'}
        status = status_map.get(r['status'], r['status'])
        entry = f"${r['order_price']:.4f}" if r['order_price'] else '-'
        current_exit = f"${r['exit_price']:.4f}" if r.get('exit_price') else (f"${r['current_price']:.4f}" if r['status']=='filled' else '-')
        pnl = f"${r['pnl']:+.1f}U" if r['status'] in ['closed', 'filled'] else '-'
        print(f"{r['symbol']:<15} {status:<10} {entry:<12} {current_exit:<12} {pnl:<12}")
    
    print(f"\n{'='*90}")
    print(f"追踪完成：{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*90}")

if __name__ == '__main__':
    main()
