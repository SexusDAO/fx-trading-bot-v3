#!/usr/bin/env python3
"""
2026-08-02 全天完整模拟测试 - v2.3 系统
全程模拟，不执行真实下单
扫描频率：每 30 分钟
"""

import json
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
import hashlib

# ===== 配置 =====
SIM_DATE = "2026-08-02"
START_TIME = datetime(2026, 8, 2, 0, 0, 0)
END_TIME = datetime(2026, 8, 2, 16, 0, 0)  # 当前时间约 16:17 UTC
SCAN_INTERVAL_MINUTES = 30
V23_OPEN_THRESHOLD = 70
V23_OBSERVE_THRESHOLD = 60
MAX_POSITIONS = 5
ORDER_TIMEOUT_HOURS = 4

# ===== 数据获取 =====
def fetch_klines(symbol, end_ts, limit=200, interval="1h"):
    """获取 K 线数据（end_ts 之前的数据）"""
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval={interval}&endTime={end_ts}&limit={limit}"
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

def fetch_ticker_24hr(ts):
    """获取 24H 行情排行（模拟 T 时刻的快照）"""
    # 使用当前 API 数据作为近似（历史快照需要存档数据）
    url = "https://fapi.binance.com/fapi/v1/ticker/24hr"
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            data = json.loads(response.read())
            return data
    except:
        return []

def fetch_top_symbols():
    """获取候选标的列表"""
    tickers = fetch_ticker_24hr(None)
    
    # 涨幅榜前 20
    gainers = sorted([t for t in tickers if t['symbol'].endswith('USDT')], 
                     key=lambda x: float(x['priceChangePercent']), reverse=True)[:20]
    
    # 跌幅榜前 20
    losers = sorted([t for t in tickers if t['symbol'].endswith('USDT')], 
                    key=lambda x: float(x['priceChangePercent']))[:20]
    
    # 成交量前 60
    by_volume = sorted([t for t in tickers if t['symbol'].endswith('USDT')], 
                       key=lambda x: float(x['quoteVolume']), reverse=True)[:60]
    
    # 合并去重
    symbols = set()
    candidates = []
    for t in gainers + losers + by_volume:
        sym = t['symbol']
        if sym not in symbols and float(t['quoteVolume']) > 500000:  # 流动性过滤
            symbols.add(sym)
            candidates.append({
                'symbol': sym,
                'price': float(t['lastPrice']),
                'change_pct': float(t['priceChangePercent']),
                'volume': float(t['quoteVolume'])
            })
    
    return candidates[:100]  # 约 90-100 个候选

# ===== v2.3 评分系统 =====
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
    if len(klines) < period: return 1.0
    tr = [max(klines[i]['high']-klines[i]['low'], 
              abs(klines[i]['high']-klines[i-1]['close']), 
              abs(klines[i]['low']-klines[i-1]['close'])) 
          for i in range(1, len(klines))]
    return sum(tr[-period:])/period if tr else 1.0

def score_v23(klines, position='long'):
    """v2.3 评分系统"""
    if len(klines) < 30: return None, {}
    
    closed = klines[:-1]
    prices = [k['close'] for k in closed]
    vols = [k['quote_volume'] for k in closed]
    
    rsi = calculate_rsi(prices)
    macd = calculate_macd(prices)
    e9, e21, e55 = calculate_ema(prices,9), calculate_ema(prices,21), calculate_ema(prices,55)
    
    # 成交量：前一根 K 线 / 前 10 根均量
    vol_ratio = vols[-2]/sum(vols[-12:-2])*10 if len(vols)>=12 else 1.0
    
    # 价格变化
    p_change = (prices[-1]-prices[-20])/prices[-20]*100 if len(prices)>=20 else 0
    
    # 价格位置
    high_20, low_20 = max(prices[-20:]), min(prices[-20:])
    p_pos = (prices[-1]-low_20)/(high_20-low_20) if high_20 > low_20 else 0.5
    
    # 连续 K 线确认
    cont = len(klines)>=4 and ((klines[-2]['close']>klines[-3]['close']>klines[-4]['close']) if position=='long' 
                                else (klines[-2]['close']<klines[-3]['close']<klines[-4]['close']))
    
    score = 0
    
    # RSI 评分（多单）
    if position == 'long':
        if 45<=rsi<=65: rs=18
        elif 40<=rsi<45 or 65<rsi<=70: rs=12
        elif 30<=rsi<40 or 70<rsi<=75: rs=6
        else: rs=0
        rp = 25 if rsi>80 else (15 if rsi>75 else (10 if rsi<20 else 0))
    else:  # short
        if 35<=rsi<=55: rs=18
        elif 30<=rsi<35 or 55<rsi<=60: rs=12
        elif 25<=rsi<30 or 60<rsi<=70: rs=6
        else: rs=0
        rp = 25 if rsi<20 else (15 if rsi<25 else (10 if rsi>80 else 0))
    
    score += rs - rp
    
    # MACD
    if position == 'long':
        score += 15 if macd['histogram']>0.001 else (10 if macd['histogram']>0 else (5 if macd['histogram']>-0.0005 else 0))
    else:
        score += 15 if macd['histogram']<-0.001 else (10 if macd['histogram']<0 else (5 if macd['histogram']<0.0005 else 0))
    
    # EMA
    if position == 'long':
        score += 20 if e9>e21>e55 and e9>prices[-1] else (15 if e9>e21>e55 else (8 if e9>e21 or e21>e55 else 0))
    else:
        score += 20 if e9<e21<e55 and e9<prices[-1] else (15 if e9<e21<e55 else (8 if e9<e21 or e21<e55 else 0))
    
    # Volume
    score += 20 if vol_ratio>2 else (15 if vol_ratio>1.5 else (10 if vol_ratio>1 else (5 if vol_ratio>0.8 else 0)))
    
    # Support/Resistance
    if position == 'long':
        score += 20 if p_pos<0.3 else (12 if p_pos<0.5 else (6 if p_pos<0.7 else 0))
    else:
        score += 20 if p_pos>0.7 else (12 if p_pos>0.5 else (6 if p_pos>0.3 else 0))
    
    # Trend
    score += 10 if abs(p_change)>15 else (6 if abs(p_change)>8 else (3 if abs(p_change)>3 else 0))
    
    # Continuous
    score += 10 if cont else 0
    
    return min(score, 100), {
        'rsi': rsi, 'rsi_penalty': rp, 'macd': macd['histogram'],
        'ema': '多' if e9>e21>e55 else ('空' if e9<e21<e55 else '混'),
        'vol_ratio': vol_ratio, 'price_pos': p_pos, 'continuous': cont
    }

# ===== 模拟引擎 =====
class SimulationEngine:
    def __init__(self):
        self.scan_times = []
        self.scan_results = []
        self.orders = []  # 挂单
        self.positions = []  # 持仓
        self.closed_positions = []  # 已平仓
        self.observe_zone = []  # 观察区
        self.stats = {
            'total_scans': 0, 'total_symbols': 0, 'signals_70plus': 0,
            'observe_60_69': 0, 'skipped': 0, 'force_skipped': 0,
            'orders_placed': 0, 'orders_timeout': 0, 'orders_filled': 0,
            'positions_opened': 0, 'positions_long': 0, 'positions_short': 0,
            'stop_loss': 0, 'tp1': 0, 'tp2': 0, 'health_close': 0,
            'total_pnl': 0
        }
    
    def generate_scan_times(self):
        """生成所有扫描时间点"""
        current = START_TIME
        while current <= END_TIME:
            self.scan_times.append(current)
            current += timedelta(minutes=SCAN_INTERVAL_MINUTES)
        return self.scan_times
    
    def run_scan(self, scan_time):
        """执行单次扫描"""
        candidates = fetch_top_symbols()
        self.stats['total_symbols'] += len(candidates)
        
        signals = []
        for cand in candidates:
            symbol = cand['symbol']
            end_ts = int(scan_time.timestamp() * 1000)
            klines = fetch_klines(symbol, end_ts, limit=100)
            
            if not klines or len(klines) < 30:
                continue
            
            # 强制跳过检查
            skip_reason = None
            ticker_change = cand['change_pct']
            ticker_vol = cand['volume']
            
            if ticker_vol < 1000000:
                skip_reason = f"流动性不足 (${ticker_vol/1e6:.1f}M)"
            elif abs(ticker_change) > 50:
                skip_reason = f"24H 涨跌过大 ({ticker_change:+.0f}%)"
            
            if skip_reason:
                self.stats['force_skipped'] += 1
                continue
            
            # 多单评分
            score, details = score_v23(klines, 'long')
            if not score:
                continue
            
            self.stats['total_scans'] += 1
            
            # 决策
            if score >= V23_OPEN_THRESHOLD:
                self.stats['signals_70plus'] += 1
                signals.append({
                    'symbol': symbol, 'score': score, 'details': details,
                    'price': cand['price'], 'change': ticker_change,
                    'time': scan_time, 'direction': 'long'
                })
            elif score >= V23_OBSERVE_THRESHOLD:
                self.stats['observe_60_69'] += 1
                # 检查是否已在观察区
                if not any(o['symbol']==symbol for o in self.observe_zone):
                    self.observe_zone.append({
                        'symbol': symbol, 'score': score, 'time': scan_time
                    })
            else:
                self.stats['skipped'] += 1
        
        return signals
    
    def place_order(self, signal):
        """模拟挂单"""
        # 检查持仓上限
        if len(self.positions) >= MAX_POSITIONS:
            return None
        
        # 检查是否已有挂单
        if any(o['symbol']==signal['symbol'] and o['status']=='pending' for o in self.orders):
            return None
        
        klines = fetch_klines(signal['symbol'], int(signal['time'].timestamp()*1000), limit=50)
        atr = calculate_atr(klines) if klines else signal['price']*0.05
        
        # 挂单价格：限价单，多单挂在当前价下方 2%
        order_price = signal['price'] * 0.98
        stop_loss = order_price - atr * 1.5
        tp1 = order_price + atr * 2
        tp2 = order_price + atr * 3
        position_size = 300 + (signal['score'] - 60) * 14
        
        order = {
            'id': len(self.orders) + 1,
            'symbol': signal['symbol'],
            'direction': signal['direction'],
            'order_price': order_price,
            'stop_loss': stop_loss,
            'tp1': tp1,
            'tp2': tp2,
            'position_size': position_size,
            'time': signal['time'],
            'expire_time': signal['time'] + timedelta(hours=ORDER_TIMEOUT_HOURS),
            'status': 'pending',
            'score': signal['score'],
            'fill_price': None,
            'fill_time': None
        }
        
        self.orders.append(order)
        self.stats['orders_placed'] += 1
        return order
    
    def check_order_fill(self, order, current_time, current_klines):
        """检查挂单是否成交"""
        if order['status'] != 'pending':
            return
        
        # 检查超时
        if current_time > order['expire_time']:
            order['status'] = 'cancelled'
            order['cancel_reason'] = 'timeout'
            self.stats['orders_timeout'] += 1
            return
        
        # 检查成交（多单：价格下跌触及挂单价）
        if current_klines:
            low = min(k['low'] for k in current_klines[-10:])
            if order['direction'] == 'long' and low <= order['order_price']:
                order['status'] = 'filled'
                order['fill_price'] = order['order_price']
                order['fill_time'] = current_time
                self.stats['orders_filled'] += 1
                
                # 转为持仓
                self.positions.append({
                    'order_id': order['id'],
                    'symbol': order['symbol'],
                    'direction': order['direction'],
                    'entry_price': order['fill_price'],
                    'entry_time': order['fill_time'],
                    'stop_loss': order['stop_loss'],
                    'tp1': order['tp1'],
                    'tp2': order['tp2'],
                    'position_size': order['position_size'],
                    'score': order['score'],
                    'status': 'open',
                    'pnl': 0,
                    'tp1_hit': False,
                    'tp2_hit': False
                })
                self.stats['positions_opened'] += 1
                self.stats['positions_long' if order['direction']=='long' else 'positions_short'] += 1
    
    def check_position(self, pos, current_time, current_price):
        """检查持仓状态"""
        if pos['status'] != 'open':
            return
        
        # 计算当前盈亏
        if pos['direction'] == 'long':
            pos['pnl'] = (current_price - pos['entry_price']) / pos['entry_price'] * 100
        else:
            pos['pnl'] = (pos['entry_price'] - current_price) / pos['entry_price'] * 100
        
        # 检查止盈止损
        if current_price <= pos['stop_loss']:
            pos['status'] = 'closed'
            pos['close_reason'] = 'stop_loss'
            pos['close_time'] = current_time
            pos['close_pnl'] = -pos['position_size'] * 0.05  # 简化
            self.stats['stop_loss'] += 1
            self.stats['total_pnl'] += pos['close_pnl']
            self.closed_positions.append(pos)
            self.positions.remove(pos)
        elif current_price >= pos['tp2'] and not pos['tp2_hit']:
            pos['status'] = 'closed'
            pos['close_reason'] = 'tp2'
            pos['close_time'] = current_time
            pos['close_pnl'] = pos['position_size'] * 0.15  # 简化
            self.stats['tp2'] += 1
            self.stats['total_pnl'] += pos['close_pnl']
            self.closed_positions.append(pos)
            self.positions.remove(pos)
        elif current_price >= pos['tp1'] and not pos['tp1_hit']:
            pos['tp1_hit'] = True
            pos['stop_loss'] = pos['entry_price']  # 止损上移保本
            self.stats['tp1'] += 1
    
    def run_simulation(self):
        """运行完整模拟"""
        print("="*80)
        print(f"2026-08-02 全天模拟测试 - v2.3 系统")
        print(f"扫描范围：{START_TIME.strftime('%H:%M')} - {END_TIME.strftime('%H:%M')} UTC")
        print(f"扫描频率：每{SCAN_INTERVAL_MINUTES}分钟 | 持仓上限：{MAX_POSITIONS}")
        print("="*80)
        
        self.generate_scan_times()
        print(f"\n📅 生成扫描时间点：{len(self.scan_times)}个")
        
        for i, scan_time in enumerate(self.scan_times):
            print(f"\n[{i+1}/{len(self.scan_times)}] {scan_time.strftime('%H:%M')} UTC 扫描...")
            
            # 执行扫描
            signals = self.run_scan(scan_time)
            
            # 挂单
            for sig in signals:
                order = self.place_order(sig)
                if order:
                    print(f"  ✅ 挂单：{order['symbol']} @ ${order['order_price']:.4f} ({order['score']}分)")
            
            # 检查挂单成交
            for order in self.orders:
                if order['status'] == 'pending':
                    klines = fetch_klines(order['symbol'], int(scan_time.timestamp()*1000), limit=20)
                    self.check_order_fill(order, scan_time, klines)
            
            # 检查持仓
            for pos in self.positions[:]:
                klines = fetch_klines(pos['symbol'], int(scan_time.timestamp()*1000), limit=10)
                if klines:
                    self.check_position(pos, scan_time, klines[-1]['close'])
        
        # 输出报告
        self.print_report()
    
    def print_report(self):
        """输出完整日报"""
        print("\n" + "="*80)
        print("📊 模拟测试日报")
        print("="*80)
        
        print(f"\n【扫描统计】")
        print(f"  总扫描次数：{len(self.scan_times)}")
        print(f"  总扫描标的：{self.stats['total_symbols']}个（去重后约 90-100/次）")
        print(f"  触发≥70 分信号：{self.stats['signals_70plus']}个")
        print(f"  进入观察区：{self.stats['observe_60_69']}个")
        print(f"  跳过：<60 分 {self.stats['skipped']}个")
        print(f"  强制跳过：{self.stats['force_skipped']}个（流动性/涨跌幅）")
        
        print(f"\n【挂单统计】")
        print(f"  挂单总数：{self.stats['orders_placed']}笔")
        print(f"  成交笔数：{self.stats['orders_filled']}笔")
        print(f"  超时撤单：{self.stats['orders_timeout']}笔")
        print(f"  挂单成交率：{self.stats['orders_filled']/max(1,self.stats['orders_placed'])*100:.0f}%")
        
        print(f"\n【持仓统计】")
        print(f"  今日开仓：{self.stats['positions_opened']}笔")
        print(f"  多单：{self.stats['positions_long']}笔 | 空单：{self.stats['positions_short']}笔")
        
        print(f"\n【盈亏统计】")
        print(f"  止损出局：{self.stats['stop_loss']}笔")
        print(f"  止盈一档：{self.stats['tp1']}笔")
        print(f"  止盈二档：{self.stats['tp2']}笔")
        print(f"  当前持仓：{len(self.positions)}笔")
        print(f"  已实现盈亏：${self.stats['total_pnl']:+.1f}U（模拟）")
        
        if self.closed_positions:
            print(f"\n【已平仓记录】")
            for cp in self.closed_positions[:5]:
                print(f"  {cp['symbol']}: {cp['close_reason']} {cp['close_pnl']:+.1f}U")
        
        if self.positions:
            print(f"\n【当前持仓】")
            for p in self.positions:
                print(f"  {p['symbol']}: 开仓${p['entry_price']:.4f} 当前浮盈{p['pnl']:+.1f}%")
        
        print(f"\n{'='*80}")
        print(f"模拟完成时间：{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")

if __name__ == '__main__':
    engine = SimulationEngine()
    engine.run_simulation()
