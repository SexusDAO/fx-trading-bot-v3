#!/bin/bash
# ws_monitor_guard.sh — 守护 ws_monitor.py 常驻运行
# 由 cron 每5分钟调用一次
WORKSPACE="/root/.openclaw/workspace"
LOG="$WORKSPACE/ws_monitor.log"

# 检查进程是否在跑
if pgrep -f "ws_monitor.py" > /dev/null 2>&1; then
    exit 0
fi

# 没在跑，重启
echo "[$(date -u '+%Y-%m-%d %H:%M UTC')] ws_monitor 未运行，重启中..." >> "$LOG"
cd "$WORKSPACE"
nohup python3 ws_monitor.py >> "$LOG" 2>&1 &
echo "[$(date -u '+%Y-%m-%d %H:%M UTC')] ws_monitor 已重启 PID=$!" >> "$LOG"
