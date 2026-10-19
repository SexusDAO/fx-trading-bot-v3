#!/usr/bin/env python3
"""
LLM-7: 异常告警
需要：描述异常情况
原因：需要语言描述
触发：WS 断线 3 次/熔断触发/pos_state 写入失败
输出：推送告警消息
"""
import sys
sys.path.insert(0, '/root/.openclaw/workspace')

import json
from datetime import datetime
from pathlib import Path

def generate_alert(alert_type, details):
    """
    生成异常告警
    这是 LLM-7 的核心调用点
    
    alert_type: 'ws_disconnect' | 'circuit_trigger' | 'pos_state_error' | 'api_error'
    details: 详细信息 dict
    """
    print(f"=== 生成异常告警：{alert_type} ===")
    
    # 告警模板
    templates = {
        'ws_disconnect': {
            'title': '⚠️ WS 连接异常',
            'message': 'WS 断线 {count} 次，已自动重启',
            'priority': 'high'
        },
        'circuit_trigger': {
            'title': '🔴 熔断器触发',
            'message': '原因：{reason}\n已暂停交易 {duration} 小时',
            'priority': 'critical'
        },
        'pos_state_error': {
            'title': '❌ 仓位状态写入失败',
            'message': '错误：{error}\n请手动检查 pos_state.json',
            'priority': 'high'
        },
        'api_error': {
            'title': '⚠️ API 报错',
            'message': '接口：{endpoint}\n错误：{error}',
            'priority': 'medium'
        }
    }
    
    template = templates.get(alert_type, {
        'title': '⚠️ 系统告警',
        'message': '未知异常：{details}',
        'priority': 'medium'
    })
    
    # 填充模板
    message = template['message'].format(**details)
    
    alert = {
        'timestamp': datetime.utcnow().isoformat(),
        'type': alert_type,
        'title': template['title'],
        'message': message,
        'priority': template['priority'],
        'details': details
    }
    
    # 保存告警历史
    alert_path = Path('logs/alerts.json')
    alert_path.parent.mkdir(parents=True, exist_ok=True)
    
    alerts = []
    try:
        with open(alert_path, 'r') as f:
            alerts = json.load(f)
    except FileNotFoundError:
        pass
    
    alerts.append(alert)
    
    # 保留最近 100 条告警
    alerts = alerts[-100:]
    
    with open(alert_path, 'w') as f:
        json.dump(alerts, f, indent=2, ensure_ascii=False)
    
    print(f"告警已记录：{alert['title']}")
    print(f"消息：{message}")
    
    # TODO: 推送消息到 Jarvis
    # message.send({
    #     'action': 'send',
    #     'channel': 'jarvis',
    #     'target': '432401404:thread:019fc2fd-971c-726d-8f63-a93d8a5814e0',
    #     'message': f"{alert['title']}\n\n{message}"
    # })
    
    return alert

if __name__ == '__main__':
    # 测试
    test_alerts = [
        ('ws_disconnect', {'count': 3}),
        ('circuit_trigger', {'reason': '连续亏损 3 笔', 'duration': 1}),
        ('pos_state_error', {'error': 'JSON decode error'}),
        ('api_error', {'endpoint': '/fapi/v1/positionRisk', 'error': '429 Too Many Requests'}),
    ]
    
    for alert_type, details in test_alerts:
        generate_alert(alert_type, details)
        print()
