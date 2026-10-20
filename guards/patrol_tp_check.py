#!/usr/bin/env python3
"""
Patrol 止盈检查 - 每 3 分钟执行，纯 Python，不走 LLM
- REST 查询 Binance 是否有止盈单
- 无止盈则从 pos_state.json 读取参数直接补挂
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
        logging.FileHandler('/root/.openclaw/workspace/logs/patrol_tp.log'),
        logging.StreamHandler()
    ]
)

def check_and_place_tp():
    """检查并补挂止盈单"""
    logging.info("=== Patrol 止盈检查启动 ===")
    
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
        
        # 检查是否需要补挂止盈
        needs_tp, tp_price = ps.check_missing_tp(symbol, api)
        
        if needs_tp:
            logging.warning(f"  ⚠️  {symbol} 缺少止盈单，准备补挂 @ {tp_price}")
            
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
            
            # 计算止盈单数量
            qty = abs(position_amt)
            
            # 挂止盈单（限价单）
            # 多单：止盈卖出 LIMIT
            # 空单：止盈买入 LIMIT
            order_side = 'SELL' if side == 'LONG' else 'BUY'
            
            try:
                order = api.place_limit_order(
                    symbol=symbol,
                    side=order_side,
                    price=tp_price,
                    qty=qty,
                    type='LIMIT',
                    timeInForce='GTC'
                )
                
                logging.info(f"  ✅ 已补挂止盈单：{order['orderId']}")
                placed_count += 1
                
            except Exception as e:
                logging.error(f"  ❌ 补挂止盈失败：{e}")
        else:
            logging.info(f"  ✓ {symbol} 止盈单正常")
    
    logging.info(f"=== Patrol 止盈检查完成，补挂 {placed_count} 个止盈单 ===")

if __name__ == '__main__':
    check_and_place_tp()
