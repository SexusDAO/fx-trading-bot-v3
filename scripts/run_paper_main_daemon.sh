#!/bin/bash
WORKSPACE="/root/.openclaw/workspace"
SCRIPT="paper_scanner.py"
LOG="$WORKSPACE/paper_scanner.log"
PIDFILE="$WORKSPACE/ws_cache/paper_main_daemon.pid"
echo $$ > "$PIDFILE"
cd "$WORKSPACE" || exit 1
echo "[daemon] $(date -u '+%Y-%m-%d %H:%M UTC') paper_main 守护进程启动 (PID=$$)" >> "$LOG"
while true; do
    echo "[daemon] $(date -u '+%Y-%m-%d %H:%M UTC') 启动 paper_scanner.py" >> "$LOG"
    python3 -u "$SCRIPT" > /dev/null 2>&1
    EXIT_CODE=$?
    echo "[daemon] $(date -u '+%Y-%m-%d %H:%M UTC') 进程退出 (code=$EXIT_CODE)，10s后重启..." >> "$LOG"
    sleep 10
done
