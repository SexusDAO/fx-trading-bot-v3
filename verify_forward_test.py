#!/usr/bin/env python3
"""
前向测试验证 - 详细检查
"""

import json
import urllib.request
from datetime import datetime, timedelta

def fetch_klines(symbol, end_ts, limit=500):
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval=1h&endTime={end_ts}&limit={limit}"
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            data = json.loads(response.read())
            return [{'timestamp': k[0], 'open': float(k[1]), 'high': float(k[2]), 'low': float(k[3]), 'close': float(k[4]), 'quote_volume': float(k[7])} for k in data]
    except Exception as e:
        print(f"Fetch error: {e}")
        return None

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

def calculate_atr(klines, period=14):
    if len(klines) < period: return klines[-1]['close'] * 0.05 if klines else 0.05
    tr = [max(klines[i]['high']-klines[i]['low'], abs(klines[i]['high']-klines[i-1]['close']), abs(klines[i]['low']-klines[i-1]['close'])) for i in range(1, len(klines))]
    return sum(tr[-period:])/period if tr else 0.05

def score_v22(klines):
    if len(klines) < 30: return None
    closed = klines[:-1]
    prices = [k['close'] for k in closed]
    vols = [k['quote_volume'] for k in closed]
    rsi = calculate_rsi(prices)
    macd = calculate_macd(prices)
    e9, e21, e55 = calculate_ema(prices,9), calculate_ema(prices,21), calculate_ema(prices,55)
    vol_ratio = vols[-1]/sum(vols[-10:])*10 if len(vols)>=10 and sum(vols[-10:])>0 else 1.0
    p_change = (prices[-1]-prices[-20])/prices[-20]*100 if len(prices)>=20 else 0
    score = 50
    score += 10 if 40<=rsi<=60 else (5 if 30<=rsi<40 or 60<rsi<=70 else 0)
    score += 10 if macd['histogram']>0 else (5 if macd['histogram']>-0.001 else 0)
    score += 15 if e9>e21>e55 else 5
    score += 15 if vol_ratio>2.0 else (10 if vol_ratio>1.5 else (5 if vol_ratio>1.0 else 0))
    score += 10 if abs(p_change)>10 else (5 if abs(p_change)>5 else 0)
    return min(score, 100)

def main():
    print("="*100)
    print("前向测试验证报告")
    print("="*100)
    
    # 只测试 BTCUSDT
    symbol = 'BTCUSDT'
    end_ts = int(datetime.utcnow().timestamp() * 1000)
    start_ts = int((datetime.utcnow() - timedelta(days=7)).timestamp() * 1000)
    
    klines = fetch_klines(symbol, end_ts, limit=500)
    if not klines or len(klines) < 168:
        print(f"❌ 数据不足：{len(klines) if klines else 0} 根 K 线")
        return
    
    print(f"\n📅 时间范围：{datetime.utcfromtimestamp(start_ts/1000)} 到 {datetime.utcfromtimestamp(end_ts/1000)}")
    print(f"📊 标的：{symbol}")
    print(f"📈 K 线数量：{len(klines)} 根")
    
    # 详细交易记录
    trades = []
    scan_count = 0
    signal_count = 0
    
    # 每 4 小时扫描一次
    for i in range(30, len(klines)-20, 4):
        scan_count += 1
        snapshot = klines[:i+1]
        future = klines[i+1:i+21]
        
        current_price = snapshot[-1]['close']
        scan_time = datetime.utcfromtimestamp(snapshot[-1]['timestamp']/1000)
        
        # v2.2 评分
        s22 = score_v22(snapshot)
        if s22 and s22 >= 70:
            signal_count += 1
            atr = calculate_atr(snapshot)
            entry = current_price * 0.99
            sl = entry - atr * 1.5
            tp2 = entry + atr * 3
            
            # 检查后续 K 线
            pnl = 0
            reason = 'holding'
            exit_time = None
            exit_price = None
            
            for j, k in enumerate(future):
                if k['low'] <= sl:
                    pnl = 440 * (sl - entry) / entry
                    reason = 'sl'
                    exit_time = datetime.utcfromtimestamp(k['timestamp']/1000)
                    exit_price = sl
                    break
                if k['high'] >= tp2:
                    pnl = 440 * (tp2 - entry) / entry
                    reason = 'tp2'
                    exit_time = datetime.utcfromtimestamp(k['timestamp']/1000)
                    exit_price = tp2
                    break
            
            trades.append({
                'scan_time': scan_time,
                'entry_price': entry,
                'atr': atr,
                'sl': sl,
                'tp2': tp2,
                'exit_time': exit_time,
                'exit_price': exit_price,
                'pnl': pnl,
                'reason': reason,
                'score': s22
            })
    
    # 验证报告
    print(f"\n{'='*100}")
    print("1️⃣ 交易频率验证")
    print(f"{'='*100}")
    print(f"扫描次数：{scan_count} 次（7 天×24 小时÷4 小时=42 次）")
    print(f"信号次数：{signal_count} 次")
    print(f"信号率：{signal_count/scan_count*100:.1f}%")
    
    if signal_count > 30:
        print(f"\n⚠️ 警告：7 天内开仓{signal_count}笔，远超正常范围（应<30 笔）")
        print(f"   平均每天开仓：{signal_count/7:.1f} 笔")
        print(f"   问题：扫描间隔可能过短 或 评分门槛过低")
    else:
        print(f"\n✅ 交易频率正常：{signal_count}笔/7天")
    
    print(f"\n{'='*100}")
    print("2️⃣ 胜率计算验证")
    print(f"{'='*100}")
    
    winning_trades = [t for t in trades if t['pnl'] > 0]
    losing_trades = [t for t in trades if t['pnl'] <= 0]
    
    print(f"总交易笔数：{len(trades)}")
    print(f"盈利笔数：{len(winning_trades)}")
    print(f"亏损笔数：{len(losing_trades)}")
    print(f"\n胜率计算公式：盈利笔数 / 总交易笔数")
    print(f"胜率 = {len(winning_trades)} / {len(trades)} = {len(winning_trades)/len(trades)*100:.1f}%")
    print(f"\n止损/止盈统计：")
    tp_count = sum(1 for t in trades if t['reason']=='tp2')
    sl_count = sum(1 for t in trades if t['reason']=='sl')
    holding_count = sum(1 for t in trades if t['reason']=='holding')
    print(f"  止盈出场：{tp_count} 笔")
    print(f"  止损出场：{sl_count} 笔")
    print(f"  未平仓：{holding_count} 笔")
    
    print(f"\n{'='*100}")
    print("3️⃣ 仓位计算验证")
    print(f"{'='*100}")
    print(f"单笔仓位：440U（固定）")
    print(f"杠杆：1x（现货模拟）")
    print(f"最大同时持仓：理论无限制（实际应受账户余额限制）")
    
    total_position = len(trades) * 440
    print(f"\n7 天总交易金额：${total_position:.0f}U")
    print(f"日均交易金额：${total_position/7:.0f}U")
    
    print(f"\n{'='*100}")
    print("4️⃣ 手续费验证")
    print(f"{'='*100}")
    print(f"手续费率：0.04%/笔（开仓+平仓=0.08%）")
    print(f"单笔手续费：440U × 0.08% = ${440*0.0008:.2f}U")
    
    total_fee = len(trades) * 440 * 0.0008
    gross_pnl = sum(t['pnl'] for t in trades)
    net_pnl = gross_pnl - total_fee
    
    print(f"\n总手续费：${total_fee:.2f}U")
    print(f"毛盈亏：${gross_pnl:.2f}U")
    print(f"净盈亏（扣除手续费）：${net_pnl:.2f}U")
    print(f"手续费影响：{total_fee/gross_pnl*100:.1f}%（如 gross_pnl>0）")
    
    print(f"\n{'='*100}")
    print("5️⃣ BTCUSDT 详细交易记录")
    print(f"{'='*100}")
    
    if len(trades) <= 50:
        print(f"\n{'序号':<4} {'开仓时间':<18} {'开仓价':<10} {'止损价':<10} {'止盈价':<10} {'平仓时间':<18} {'平仓价':<10} {'盈亏':<10} {'结果':<8}")
        print("-"*110)
        for idx, t in enumerate(trades, 1):
            entry_str = f"${t['entry_price']:.2f}"
            sl_str = f"${t['sl']:.2f}"
            tp2_str = f"${t['tp2']:.2f}"
            exit_str = t['exit_time'].strftime('%m-%d %H:%M') if t['exit_time'] else '未平仓'
            exit_price_str = f"${t['exit_price']:.2f}" if t['exit_price'] else '-'
            pnl_str = f"${t['pnl']:+.2f}U"
            reason_str = '止盈' if t['reason']=='tp2' else ('止损' if t['reason']=='sl' else '持仓')
            print(f"{idx:<4} {t['scan_time'].strftime('%m-%d %H:%M'):<18} {entry_str:<10} {sl_str:<10} {tp2_str:<10} {exit_str:<18} {exit_price_str:<10} {pnl_str:<10} {reason_str:<8}")
    else:
        print(f"\n交易过多（{len(trades)}笔），仅显示前 20 笔：")
        print(f"\n{'序号':<4} {'开仓时间':<18} {'开仓价':<10} {'盈亏':<10} {'结果':<8}")
        print("-"*70)
        for idx, t in enumerate(trades[:20], 1):
            pnl_str = f"${t['pnl']:+.2f}U"
            reason_str = '止盈' if t['reason']=='tp2' else ('止损' if t['reason']=='sl' else '持仓')
            print(f"{idx:<4} {t['scan_time'].strftime('%m-%d %H:%M'):<18} ${t['entry_price']:.2f}{' ':<5} {pnl_str:<10} {reason_str:<8}")
        print(f"... 还有{len(trades)-20}笔")
    
    print(f"\n{'='*100}")
    print("📊 汇总")
    print(f"{'='*100}")
    print(f"总交易：{len(trades)}笔")
    print(f"总盈亏（未扣手续费）：${gross_pnl:+.2f}U")
    print(f"总手续费：${total_fee:.2f}U")
    print(f"净盈亏：${net_pnl:+.2f}U")
    print(f"胜率：{len(winning_trades)/len(trades)*100:.1f}%")
    print(f"平均盈亏/笔：${gross_pnl/len(trades):+.2f}U")
    
    print(f"\n{'='*100}")
    print(f"验证完成：{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")

if __name__ == '__main__':
    main()
