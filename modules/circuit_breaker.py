"""
熔断器模块 - 纯 Python，不走 LLM
每天 00:00 UTC 自动重置
"""
import sys
sys.path.insert(0, '/root/.openclaw/workspace')

import json
from datetime import datetime, timedelta
from pathlib import Path

class CircuitBreaker:
    def __init__(self, filepath='config/circuit_breaker.json'):
        self.filepath = filepath
        self.state = self.load()
    
    def load(self):
        """加载熔断器状态"""
        try:
            with open(self.filepath, 'r') as f:
                return json.load(f)
        except FileNotFoundError:
            return {
                'is_open': False,
                'triggered_at': None,
                'reason': None,
                'reset_at': None,
                'trip_count_today': 0
            }
    
    def save(self):
        """保存熔断器状态"""
        Path(self.filepath).parent.mkdir(parents=True, exist_ok=True)
        with open(self.filepath, 'w') as f:
            json.dump(self.state, f, indent=2)
    
    def check(self):
        """
        检查熔断器状态
        返回：(is_open, reason)
        """
        if self.state['is_open']:
            # 检查是否已过期
            if self.state['reset_at']:
                reset_at = datetime.fromisoformat(self.state['reset_at'])
                if datetime.utcnow() > reset_at:
                    # 熔断已过期，自动重置
                    self.reset()
                    return False, None
            return True, self.state['reason']
        return False, None
    
    def trigger(self, reason, duration_hours=1):
        """
        触发熔断
        reason: 触发原因
        duration_hours: 熔断持续时间（小时）
        """
        self.state['is_open'] = True
        self.state['triggered_at'] = datetime.utcnow().isoformat()
        self.state['reason'] = reason
        self.state['reset_at'] = (datetime.utcnow() + timedelta(hours=duration_hours)).isoformat()
        self.state['trip_count_today'] += 1
        self.save()
    
    def reset(self):
        """重置熔断器"""
        self.state = {
            'is_open': False,
            'triggered_at': None,
            'reason': None,
            'reset_at': None,
            'trip_count_today': self.state.get('trip_count_today', 0)
        }
        self.save()
    
    def daily_reset(self):
        """每日重置（00:00 UTC 调用）"""
        self.state = {
            'is_open': False,
            'triggered_at': None,
            'reason': None,
            'reset_at': None,
            'trip_count_today': 0
        }
        self.save()
    
    def get_status(self):
        """获取熔断器状态摘要"""
        return {
            'is_open': self.state['is_open'],
            'reason': self.state['reason'],
            'trips_today': self.state['trip_count_today'],
            'reset_at': self.state['reset_at']
        }


if __name__ == '__main__':
    # 测试
    cb = CircuitBreaker()
    
    # 检查状态
    is_open, reason = cb.check()
    print(f"熔断器状态：{'开启' if is_open else '关闭'}")
    if reason:
        print(f"原因：{reason}")
    
    # 获取状态
    status = cb.get_status()
    print(f"今日触发次数：{status['trips_today']}")
