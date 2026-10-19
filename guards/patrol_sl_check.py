#!/usr/bin/env python3
"""
Patrol 止损检查 - 每 3 分钟执行，纯 Python，不走 LLM
- REST 查询 Binance 是否有止损单
- 无止损则从 pos_state.json 读取参数直接补挂
"""
import sys
sys.path.insert(0, '/root/.openclaw/workspace')

from modules.position_state import PositionState
from binance_api import BinanceAPI
import logging
from datetime import datetime

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('/root/.openclaw/workspace/logs/patrol_sl.log'),
        logging.StreamHandler()
    ]
)

def check_and_place_sl():
    """检查并补挂止损单"""
    logging.info("=== Patrol 止损检查启动 ===")
    
    # 加载仓位状态
    ps = PositionState()
    positions = ps.get_all_positions()
    
    if not positions:
        logging.info("当前无持仓")
        return
    
    api = BinanceAPI()
    placed_count = 0
    
    for symbol, position in positions.items():
        logging.info(f"检查 {symbol}...")
        
        # 检查是否需要补挂止损
        needs_sl, sl_price = ps.check_missing_sl(symbol, api)
        
        if needs_sl:
            logging.warning(f"  ⚠️  {symbol} 缺少止损单，准备补挂 @ {sl_price}")
            
            # 获取仓位信息
            side = position['side']
            position_amt = position.get('position_amt', 0)
            
            if not position_amt:
                # 从 Binance 获取实际持仓
                positions_data = api.get_positions()
                for pos in positions_data:
                    if pos['symbol'] == symbol:
                        position_amt = float(pos['positionAmt'])
                        break
            
            if not position_amt:
                logging.error(f"  ❌ 无法获取 {symbol} 持仓数量")
                continue
            
            # 计算止损单数量
            qty = abs(position_amt)
            
            # 挂止损单
            # 多单：止损卖出 STOP_MARKET
            # 空单：止损买入 STOP_MARKET
            order_side = 'SELL' if side == 'LONG' else 'BUY'
            
            try:
                order = api.place_stop_order(
                    symbol=symbol,
                    side=order_side,
                    stop_price=sl_price,
                    qty=qty,
                    type='STOP_MARKET'
                )
                
                logging.info(f"  ✅ 已补挂止损单：{order['orderId']}")
                placed_count += 1
                
            except Exception as e:
                logging.error(f"  ❌ 补挂止损失败：{e}")
        else:
            logging.info(f"  ✓ {symbol} 止损单正常")
    
    logging.info(f"=== Patrol 止损检查完成，补挂 {placed_count} 个止损单 ===")

if __name__ == '__main__':
    check_and_place_sl()
