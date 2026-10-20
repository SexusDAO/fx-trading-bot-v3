#!/usr/bin/env python3
"""
v2.2 vs v2.5 前向测试 - 修正版
严格 7 天数据，回答 5 个验证问题
"""

import json
import urllib.request
from datetime import datetime, timedelta

def fetch_klines(symbol, end_ts, limit=200):
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval=1h&endTime={end_ts}&limit={limit}"
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            data = json.loads(response.read())
            return [{'timestamp': k[0], 'open': float(k[1]), 'high': float(k[2]), 'low': float(k[3]), 'close': float(k[4]), 'quote_volume': float(k[7])} for k in data]
    except:
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

def score_v25(klines, is_low_cap=False):
    if len(klines) < 30: return None
    closed = klines[:-1]
    prices = [k['close'] for k in closed]
    vols = [k['quote_volume'] for k in closed]
    rsi = calculate_rsi(prices)
    macd = calculate_macd(prices)
    e9, e21, e55 = calculate_ema(prices,9), calculate_ema(prices,21), calculate_ema(prices,55)
    vol_ratio = vols[-2]/sum(vols[-12:-2])*10 if len(vols)>=12 and sum(vols[-12:-2])>0 else 1.0
    p_change = (prices[-1]-prices[-20])/prices[-20]*100 if len(prices)>=20 else 0
    high_20, low_20 = max(prices[-20:]), min(prices[-20:])
    p_pos = (prices[-1]-low_20)/(high_20-low_20) if high_20>low_20 else 0.5
    support_touches = sum(1 for p in prices[-20:] if (p-low_20)/(high_20-low_20)<0.15) if high_20>low_20 else 0
    consecutive = sum(1 for i in range(len(klines)-2, max(0,len(klines)-5), -1) if klines[i]['close']>klines[i+1]['close'])
    score = 20
    if 45<=rsi<=65: rs=18
    elif 40<=rsi<45 or 65<rsi<=70: rs=12
    elif 30<=rsi<40 or 70<rsi<=75: rs=6
    else: rs=0
    rp = 15 if rsi>75 else (10 if rsi>70 else (8 if rsi<20 else 0))
    score += rs - rp
    score += 15 if macd['histogram']>0.001 else (10 if macd['histogram']>0 else (5 if macd['histogram']>-0.0005 else 0))
    score += 18 if e9>e21>e55 and e9>prices[-1] else (13 if e9>e21>e55 else (7 if e9>e21 or e21>e55 else 0))
    score += 18 if vol_ratio>2.0 else (13 if vol_ratio>1.5 else (10 if vol_ratio>1.0 else (5 if vol_ratio>0.8 else 0)))
    score += 15 if (p_pos<0.3 or support_touches>=3) else (10 if (p_pos<0.5 or support_touches>=2) else (5 if p_pos<0.7 else 0))
    score += 10 if abs(p_change)>15 else (6 if abs(p_change)>8 else (3 if abs(p_change)>3 else 0))
    score += 7 if consecutive>=2 else (4 if consecutive==1 else 0)
    if is_low_cap: score -= 3
    return min(score, 100)

def run_test(symbols, klines_dict, scan_interval=6):
    """扫描间隔改为 6 小时，更接近实盘"""
    results = {}
    
    for sym in symbols:
        klines = klines_dict[sym]
        if not klines or len(klines) < 168:
            continue
        
        trades = []
        # 每 scan_interval 小时扫描一次
        for i in range(30, len(klines)-20, scan_interval):
            snapshot = klines[:i+1]
            future = klines[i+1:i+21]
            
            current_price = snapshot[-1]['close']
            scan_time = datetime.utcfromtimestamp(snapshot[-1]['timestamp']/1000)
            is_low_cap = sum(k['quote_volume'] for k in snapshot[-24:]) < 50000000
            
            # v2.2
            s22 = score_v22(snapshot)
            if s22 and s22 >= 70:
                atr = calculate_atr(snapshot)
                entry = current_price * 0.99
                sl = entry - atr * 1.5
                tp2 = entry + atr * 3
                
                pnl, reason, exit_time, exit_price = 0, 'holding', None, None
                for k in future:
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
                
                trades.append({'symbol': sym, 'score': s22, 'pnl': pnl, 'reason': reason, 
                              'entry': entry, 'exit_time': exit_time, 'exit_price': exit_price,
                              'scan_time': scan_time})
            
            # v2.5
            s25 = score_v25(snapshot, is_low_cap)
            if s25 and (s25 >= 70 or (is_low_cap and s25 >= 67)):
                atr = calculate_atr(snapshot)
                entry = current_price * 0.99
                sl = entry - atr * 1.5
                tp2 = entry + atr * 3
                
                pnl, reason, exit_time, exit_price = 0, 'holding', None, None
                for k in future:
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
                
                trades.append({'symbol': sym, 'score': s25, 'pnl': pnl, 'reason': reason,
                              'entry': entry, 'exit_time': exit_time, 'exit_price': exit_price,
                              'scan_time': scan_time, 'v25': True})
        
        results[sym] = trades
    
    return results

def main():
    print("="*100)
    print("v2.2 vs v2.5 前向测试 - 修正版（回答 5 个验证问题）")
    print("="*100)
    
    symbols = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT', 'XRPUSDT']
    end_ts = int(datetime.utcnow().timestamp() * 1000)
    
    # 获取 7 天数据（168 根 K 线）
    klines_dict = {}
    for sym in symbols:
        klines = fetch_klines(sym, end_ts, limit=200)
        if klines and len(klines) >= 168:
            klines_dict[sym] = klines[-168:]  # 严格限制 7 天
        else:
            print(f"⚠️ {sym} 数据不足")
    
    print(f"\n📅 时间范围：{datetime.utcfromtimestamp(klines_dict['BTCUSDT'][0]['timestamp']/1000)} 到 {datetime.utcfromtimestamp(klines_dict['BTCUSDT'][-1]['timestamp']/1000)}")
    print(f"📊 标的：{len(klines_dict)}个（{', '.join(klines_dict.keys())}）")
    print(f"📈 K 线：每标的 168 根（7 天×24 小时）")
    print(f"📍 扫描间隔：6 小时（更接近实盘）")
    
    # 运行测试
    results = run_test(symbols, klines_dict, scan_interval=6)
    
    # 汇总
    all_trades_v22 = [t for trades in results.values() for t in trades if not t.get('v25')]
    all_trades_v25 = [t for trades in results.values() for t in trades if t.get('v25')]
    
    # ========== 回答 5 个验证问题 ==========
    print(f"\n{'='*100}")
    print("1️⃣ 交易频率验证 - 各标的开仓笔数")
    print(f"{'='*100}")
    
    by_symbol_v22 = {}
    for t in all_trades_v22:
        by_symbol_v22[t['symbol']] = by_symbol_v22.get(t['symbol'], 0) + 1
    
    print(f"\nv2.2 各标的开仓笔数（7 天）：")
    for sym, count in sorted(by_symbol_v22.items()):
        status = "⚠️ 过高" if count > 20 else "✅ 正常"
        print(f"  {sym}: {count} 笔 {status}")
    
    total_v22 = len(all_trades_v22)
    print(f"\n  总计：{total_v22} 笔")
    print(f"  平均每标的：{total_v22/len(klines_dict):.1f} 笔")
    print(f"  平均每天：{total_v22/7:.1f} 笔")
    
    if total_v22 > 30:
        print(f"\n⚠️ 问题：v2.2 开仓过多（{total_v22}笔），说明评分门槛 70 分太容易达到")
        print(f"   建议：提高门槛到 80 分，或降低基础分从 50→30")
    
    print(f"\n{'='*100}")
    print("2️⃣ 胜率计算验证")
    print(f"{'='*100}")
    
    winning_v22 = [t for t in all_trades_v22 if t['pnl'] > 0]
    losing_v22 = [t for t in all_trades_v22 if t['pnl'] <= 0]
    tp_v22 = sum(1 for t in all_trades_v22 if t['reason']=='tp2')
    sl_v22 = sum(1 for t in all_trades_v22 if t['reason']=='sl')
    
    print(f"\n胜率计算公式：**盈利笔数 / 总交易笔数**")
    print(f"  不是金额比率，不是盈利金额/总交易金额")
    print(f"\nv2.2 统计：")
    print(f"  总交易：{len(all_trades_v22)} 笔")
    print(f"  盈利：{len(winning_v22)} 笔")
    print(f"  亏损：{len(losing_v22)} 笔")
    print(f"  胜率 = {len(winning_v22)} / {len(all_trades_v22)} = {len(winning_v22)/max(1,len(all_trades_v22))*100:.1f}%")
    print(f"\n止损/止盈统计：")
    print(f"  止盈出场：{tp_v22} 笔（触发 TP2）")
    print(f"  止损出场：{sl_v22} 笔（触发 SL）")
    print(f"  未平仓：{len(all_trades_v22)-tp_v22-sl_v22} 笔（7 天内未触发 TP/SL）")
    print(f"\n  注：止损出局算**一笔亏损**，不分多次")
    
    print(f"\n{'='*100}")
    print("3️⃣ 仓位计算验证")
    print(f"{'='*100}")
    
    print(f"\n单笔仓位：**440U**（固定）")
    print(f"杠杆：**1x**（现货模拟，无杠杆）")
    print(f"\n仓位计算：")
    print(f"  总交易金额 = {len(all_trades_v22)} 笔 × 440U = ${len(all_trades_v22)*440:.0f}U")
    print(f"  日均交易金额 = ${len(all_trades_v22)*440/7:.0f}U/天")
    
    # 估算最大同时持仓
    # 简单假设：每 6 小时扫描，持仓周期约 1-20 小时
    avg_holding = 10  # 假设平均持仓 10 小时
    max_concurrent = total_v22 * avg_holding / (7*24)
    print(f"\n最大同时持仓估算：")
    print(f"  假设平均持仓{avg_holding}小时，扫描间隔 6 小时")
    print(f"  最大同时持仓 ≈ {max_concurrent:.0f} 笔 × 440U = ${max_concurrent*440:.0f}U")
    print(f"\n⚠️ 注意：代码未检查账户余额上限")
    print(f"   实盘需要检查：总持仓 ≤ 账户余额×3")
    
    print(f"\n{'='*100}")
    print("4️⃣ 手续费验证")
    print(f"{'='*100}")
    
    fee_rate = 0.0004  # 0.04%
    round_trip_fee = fee_rate * 2  # 开仓 + 平仓
    fee_per_trade = 440 * round_trip_fee
    
    gross_pnl = sum(t['pnl'] for t in all_trades_v22)
    total_fee = len(all_trades_v22) * fee_per_trade
    net_pnl = gross_pnl - total_fee
    
    print(f"\n手续费率：**0.04%/笔**（开仓 + 平仓 = **0.08%**）")
    print(f"单笔手续费：440U × 0.08% = ${fee_per_trade:.2f}U")
    print(f"\n总手续费：{len(all_trades_v22)} 笔 × ${fee_per_trade:.2f}U = ${total_fee:.2f}U")
    print(f"毛盈亏：${gross_pnl:.2f}U")
    print(f"净盈亏（扣除手续费）：${net_pnl:.2f}U")
    print(f"手续费影响：{total_fee/max(1,abs(gross_pnl))*100:.1f}%")
    
    print(f"\n{'='*100}")
    print("5️⃣ BTCUSDT 单标的详细验证")
    print(f"{'='*100}")
    
    btc_trades = [t for t in all_trades_v22 if t['symbol']=='BTCUSDT']
    print(f"\nBTCUSDT 7 天开仓：{len(btc_trades)} 笔")
    
    if len(btc_trades) > 30:
        print(f"⚠️ **超过 30 笔，说明信号生成逻辑有误**")
        print(f"   问题：v2.2 基础分 50 分太高，70 分门槛太容易达到")
    else:
        print(f"✅ 正常范围（≤30 笔）")
    
    print(f"\n详细交易记录：")
    print(f"{'序号':<4} {'开仓时间':<16} {'开仓价':<12} {'止损价':<12} {'止盈价':<12} {'结果':<8} {'盈亏':<10}")
    print("-"*80)
    
    for idx, t in enumerate(btc_trades[:30], 1):
        result = '止盈' if t['reason']=='tp2' else ('止损' if t['reason']=='sl' else '持仓')
        print(f"{idx:<4} {t['scan_time'].strftime('%m-%d %H:%M'):<16} ${t['entry']:.2f}{' ':<6} ${t['entry']-t['entry']*0.015:.2f}{' ':<6} ${t['entry']+t['entry']*0.03:.2f}{' ':<6} {result:<8} ${t['pnl']:+.2f}U")
    
    if len(btc_trades) > 30:
        print(f"... 还有{len(btc_trades)-30}笔")
    
    btc_gross = sum(t['pnl'] for t in btc_trades)
    btc_fee = len(btc_trades) * fee_per_trade
    btc_net = btc_gross - btc_fee
    
    print(f"\nBTCUSDT 汇总：")
    print(f"  交易：{len(btc_trades)} 笔")
    print(f"  毛盈亏：${btc_gross:.2f}U")
    print(f"  手续费：${btc_fee:.2f}U")
    print(f"  净盈亏：${btc_net:.2f}U")
    
    print(f"\n{'='*100}")
    print("📊 最终汇总（修正后）")
    print(f"{'='*100}")
    
    gross_v22 = sum(t['pnl'] for t in all_trades_v22)
    net_v22 = gross_v22 - total_fee
    gross_v25 = sum(t['pnl'] for t in all_trades_v25)
    fee_v25 = len(all_trades_v25) * fee_per_trade
    net_v25 = gross_v25 - fee_v25
    
    win_rate_v22 = len(winning_v22)/max(1,len(all_trades_v22))*100
    win_rate_v25 = sum(1 for t in all_trades_v25 if t['pnl']>0)/max(1,len(all_trades_v25))*100
    
    print(f"""
┌──────────────────────────────────────────────────────────────────┐
│  指标          │  v2.2           │  v2.5           │  差异       │
├──────────────────────────────────────────────────────────────────┤
│  开仓笔数      │  {len(all_trades_v22):>3}            │  {len(all_trades_v25):>3}            │  {len(all_trades_v25)-len(all_trades_v22):>+#3}        │
│  总盈亏（毛）  │  ${gross_v22:>8.1f}U    │  ${gross_v25:>8.1f}U    │  ${gross_v25-gross_v22:>+9.1f}U   │
│  总手续费      │  ${total_fee:>8.2f}U    │  ${fee_v25:>8.2f}U    │  ${fee_v25-total_fee:>+9.2f}U   │
│  净盈亏        │  ${net_v22:>8.1f}U    │  ${net_v25:>8.1f}U    │  ${net_v25-net_v22:>+9.1f}U   │
│  胜率          │  {win_rate_v22:>5.1f}%         │  {win_rate_v25:>5.1f}%         │  {win_rate_v25-win_rate_v22:>+5.1f}%       │
└──────────────────────────────────────────────────────────────────┘
""")
    
    print(f"\n{'='*100}")
    print(f"验证完成：{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")

if __name__ == '__main__':
    main()
