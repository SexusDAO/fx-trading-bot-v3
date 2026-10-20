"""
仓位状态管理模块 - 纯 Python，不走 LLM
管理 pos_state.json，用于 patrol 检查止损止盈
"""
import sys
sys.path.insert(0, '/root/.openclaw/workspace')

import json
from datetime import datetime
from pathlib import Path

class PositionState:
    def __init__(self, filepath='config/pos_state.json'):
        self.filepath = filepath
        self.positions = self.load()
    
    def load(self):
        """加载仓位状态"""
        try:
            with open(self.filepath, 'r') as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
    
    def save(self):
        """保存仓位状态"""
        Path(self.filepath).parent.mkdir(parents=True, exist_ok=True)
        with open(self.filepath, 'w') as f:
            json.dump(self.positions, f, indent=2)
    
    def add_position(self, symbol, side, entry_price, sl_price, tp_price, position_usdt):
        """
        添加新仓位
        symbol: 交易对
        side: LONG 或 SHORT
        entry_price: 入场价
        sl_price: 止损价
        tp_price: 止盈价
        position_usdt: 仓位大小 (USDT)
        """
        self.positions[symbol] = {
            'symbol': symbol,
            'side': side,
            'entry_price': entry_price,
            'sl_price': sl_price,
            'tp_price': tp_price,
            'position_usdt': position_usdt,
            'opened_at': datetime.utcnow().isoformat(),
            'last_check': datetime.utcnow().isoformat(),
            'status': 'active'
        }
        self.save()
    
    def update_position(self, symbol, **kwargs):
        """更新仓位信息"""
        if symbol not in self.positions:
            return False
        
        for key, value in kwargs.items():
            if key in self.positions[symbol]:
                self.positions[symbol][key] = value
        
        self.positions[symbol]['last_check'] = datetime.utcnow().isoformat()
        self.save()
        return True
    
    def remove_position(self, symbol):
        """移除仓位"""
        if symbol in self.positions:
            del self.positions[symbol]
            self.save()
            return True
        return False
    
    def get_position(self, symbol):
        """获取单个仓位信息"""
        return self.positions.get(symbol)
    
    def get_all_positions(self):
        """获取所有仓位"""
        return self.positions
    
    def check_missing_sl(self, symbol, binance_api):
        """
        检查 Binance 是否有止损单，没有则返回需要补挂的价格
        返回：(needs_sl, sl_price) 或 (False, None)
        """
        if symbol not in self.positions:
            return False, None
        
        pos = self.positions[symbol]
        sl_price = pos.get('sl_price')
        
        if not sl_price:
            return False, None
        
        # 查询 Binance 现有订单
        orders = binance_api.get_open_orders(symbol)
        
        # 检查是否有止损单
        has_sl = False
        for order in orders:
            order_type = order.get('type', '')
            stop_price = order.get('stopPrice')
            
            if 'STOP' in order_type and stop_price:
                # 检查止损价格是否匹配
                if abs(float(stop_price) - sl_price) / sl_price < 0.01:  # 1% 容差
                    has_sl = True
                    break
        
        if not has_sl:
            return True, sl_price
        
        return False, None
    
    def check_missing_tp(self, symbol, binance_api):
        """
        检查 Binance 是否有止盈单，没有则返回需要补挂的价格
        返回：(needs_tp, tp_price) 或 (False, None)
        """
        if symbol not in self.positions:
            return False, None
        
        pos = self.positions[symbol]
        tp_price = pos.get('tp_price')
        
        if not tp_price:
            return False, None
        
        # 查询 Binance 现有订单
        orders = binance_api.get_open_orders(symbol)
        
        # 检查是否有限价止盈单
        has_tp = False
        for order in orders:
            order_type = order.get('type', '')
            order_price = order.get('price')
            side = order.get('side')
            position_side = pos.get('side')
            
            # 多单的止盈是卖出限价，空单的止盈是买入限价
            if order_type == 'LIMIT' and order_price:
                if position_side == 'LONG' and side == 'SELL':
                    if abs(float(order_price) - tp_price) / tp_price < 0.01:
                        has_tp = True
                        break
                elif position_side == 'SHORT' and side == 'BUY':
                    if abs(float(order_price) - tp_price) / tp_price < 0.01:
                        has_tp = True
                        break
        
        if not has_tp:
            return True, tp_price
        
        return False, None
    
    def get_positions_older_than(self, minutes):
        """获取持仓超过指定时长的仓位"""
        cutoff = datetime.utcnow() - timedelta(minutes=minutes)
        old_positions = []
        
        for symbol, pos in self.positions.items():
            opened_at = datetime.fromisoformat(pos['opened_at'])
            if opened_at < cutoff:
                old_positions.append(pos)
        
        return old_positions


if __name__ == '__main__':
    # 测试
    ps = PositionState()
    
    # 添加测试仓位
    ps.add_position(
        symbol='ETHUSDT',
        side='LONG',
        entry_price=3500,
        sl_price=3400,
        tp_price=3800,
        position_usdt=1500
    )
    
    print(f"仓位列表：{list(ps.get_all_positions().keys())}")
    
    # 获取仓位
    pos = ps.get_position('ETHUSDT')
    print(f"ETHUSDT 仓位：{pos}")
