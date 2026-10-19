#!/usr/bin/env python3
"""
熔断器每日重置 - 每天 00:00 UTC 执行，纯 Python，不走 LLM
直接写 circuit_breaker.json
"""
import sys
sys.path.insert(0, '/root/.openclaw/workspace')

from modules.circuit_breaker import CircuitBreaker
import logging
from datetime import datetime

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('/root/.openclaw/workspace/logs/circuit_reset.log'),
        logging.StreamHandler()
    ]
)

def daily_reset():
    """每日重置熔断器"""
    logging.info("=== 熔断器每日重置 ===")
    
    cb = CircuitBreaker()
    
    # 获取重置前状态
    old_status = cb.get_status()
    logging.info(f"重置前状态：{old_status}")
    
    # 执行重置
    cb.daily_reset()
    
    # 获取重置后状态
    new_status = cb.get_status()
    logging.info(f"重置后状态：{new_status}")
    
    logging.info("✅ 熔断器已重置")

if __name__ == '__main__':
    daily_reset()
