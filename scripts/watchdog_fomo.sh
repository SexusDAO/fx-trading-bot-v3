#!/bin/bash
# watchdog_fomo.sh — 检查 fomo_workflow 进程
WORKSPACE=/root/.openclaw/workspace
LOCKFILE=/tmp/fomo_workflow.lock
LOGFILE=$WORKSPACE/fomo_workflow/trade.log

# 用 flock 检查锁是否被占用（进程存活则锁被占用）
if flock -n "$LOCKFILE" true 2>/dev/null; then
    # 锁没被占用 = 进程不在跑
    cd "$WORKSPACE/fomo_workflow" 2>/dev/null && \
        nohup python3 main.py start >> "$LOGFILE" 2>&1 &
    sleep 3
    if pgrep -f "fomo_workflow/main.py\|fomo.*main\.py" > /dev/null 2>&1; then
        echo "RESTARTED"
    else
        echo "RESTART_FAILED"
    fi
else
    echo "ALL_OK"
fi
