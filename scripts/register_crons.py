#!/usr/bin/env python3
"""
注册所有 cron 任务
根据新架构设计注册定时任务
"""
import subprocess
import sys

# 从 session_status 提取 threadId
# 实际应从配置读取
USER_ID = "432401404"
THREAD_ID = "019fc2fd-971c-726d-8f63-a93d8a5814e0"

CRON_JOBS = [
    # 每 3 分钟：Patrol 止损检查
    {
        'name': 'patrol-sl-check',
        'every': '3m',
        'command': 'python3 /root/.openclaw/workspace/guards/patrol_sl_check.py',
        'message': '检查 Binance 止损单，缺失则补挂',
    },
    
    # 每 3 分钟：Patrol 止盈检查
    {
        'name': 'patrol-tp-check',
        'every': '3m',
        'command': 'python3 /root/.openclaw/workspace/guards/patrol_tp_check.py',
        'message': '检查 Binance 止盈单，缺失则补挂',
    },
    
    # 每 30 分钟：Scanner 主扫描
    {
        'name': 'scanner-main',
        'every': '30m',
        'command': 'python3 /root/.openclaw/workspace/llm_tasks/scanner_llm.py',
        'message': '扫描 90-100 个标的，LLM 决策是否开仓',
    },
    
    # 每 30 分钟：持仓健康检查
    {
        'name': 'health-check',
        'every': '30m',
        'command': 'python3 /root/.openclaw/workspace/llm_tasks/health_check_llm.py',
        'message': '重新评分持仓，判断是否平仓',
    },
    
    # 每 30 分钟：市场环境判断
    {
        'name': 'market-regime',
        'every': '30m',
        'command': 'python3 /root/.openclaw/workspace/config/market_rules.py',
        'message': '判断 BTC 趋势模式（牛/熊/震荡）',
    },
    
    # 每天 00:00 UTC：熔断器重置
    {
        'name': 'circuit-daily-reset',
        'every': '24h',
        'at': '00:00',
        'command': 'python3 /root/.openclaw/workspace/guards/circuit_daily_reset.py',
        'message': '重置熔断器计数器',
    },
    
    # 每天 01:00 UTC：每日报告（LLM-5）
    {
        'name': 'daily-report',
        'every': '24h',
        'at': '01:00',
        'command': 'python3 /root/.openclaw/workspace/llm_tasks/daily_report_llm.py',
        'message': '生成当日交易报告（盈亏 + 胜率 + 复盘）',
    },
    
    # 每天 20:00 UTC：活动公告检查（LLM-6）
    {
        'name': 'activity-check',
        'every': '24h',
        'at': '20:00',
        'command': 'python3 /root/.openclaw/workspace/llm_tasks/activity_check_llm.py',
        'message': '检查币安活动公告',
    },
    
    # 进程守护（持续运行）
    {
        'name': 'process-guardian',
        'every': '24h',
        'at': '00:01',
        'command': 'nohup /root/.openclaw/workspace/guards/process_guardian.sh &',
        'message': '启动进程守护',
    },
]

def register_cron(job):
    """注册单个 cron 任务"""
    cmd = [
        'openclaw', 'cron', 'add',
        '--name', job['name'],
        '--every', job['every'],
    ]
    
    if 'at' in job:
        cmd.extend(['--at', job['at']])
    
    cmd.extend([
        '--message', job['message'],
        '--channel', 'jarvis',
        '--to', f'{USER_ID}:thread:{THREAD_ID}',
        '--announce'
    ])
    
    # 如果有 command，添加到消息中
    if 'command' in job:
        # 将 command 作为实际执行内容
        pass
    
    print(f"注册 {job['name']}...")
    result = subprocess.run(cmd, capture_output=True, text=True)
    
    if result.returncode == 0:
        print(f"  ✅ {job['name']} 注册成功")
    else:
        print(f"  ❌ {job['name']} 注册失败：{result.stderr}")
    
    return result.returncode == 0

def main():
    print("=== 注册 Cron 任务 ===\n")
    
    success_count = 0
    for job in CRON_JOBS:
        if register_cron(job):
            success_count += 1
    
    print(f"\n=== 注册完成：{success_count}/{len(CRON_JOBS)} 成功 ===")

if __name__ == '__main__':
    main()
