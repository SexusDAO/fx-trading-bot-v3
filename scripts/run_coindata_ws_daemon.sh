#!/bin/bash
# run_coindata_ws_daemon.sh — coindata_ws.py 守护脚本
# WebSocket 实时推送：/ws/stream (funding-rate/OI) + /ws/kol (signals/whale)
# 进程崩溃后自动重启，重启前等待 10 秒
# 用法：nohup bash scripts/run_coindata_ws_daemon.sh > /dev/null 2>&1 &

WORKSPACE="/root/.openclaw/workspace"
SCRIPT="scripts/coindata_ws.py"
LOG="$WORKSPACE/ws_cache/coindata_ws.log"
PIDFILE="$WORKSPACE/ws_cache/coindata_ws_daemon.pid"

mkdir -p "$WORKSPACE/ws_cache"
echo $$ > "$PIDFILE"

cd "$WORKSPACE" || exit 1

echo "[daemon] $(date -u '+%Y-%m-%d %H:%M UTC') coindata_ws 守护进程启动 (PID=$$)" >> "$LOG"

while true; do
    echo "[daemon] $(date -u '+%Y-%m-%d %H:%M UTC') 启动 coindata_ws.py" >> "$LOG"
    python3 -u "$SCRIPT" >> "$LOG" 2>&1
    EXIT_CODE=$?
    echo "[daemon] $(date -u '+%Y-%m-%d %H:%M UTC') 进程退出 (code=$EXIT_CODE)，10秒后重启..." >> "$LOG"
    sleep 10
done
