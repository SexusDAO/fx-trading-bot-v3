#!/bin/bash
# run_paper_test_daemon.sh — paper_scanner_test.py 守护脚本
# 进程崩溃后自动重启，重启前等待 10 秒
# 用法：nohup bash scripts/run_paper_test_daemon.sh > /dev/null 2>&1 &

WORKSPACE="/root/.openclaw/workspace"
SCRIPT="paper_scanner_test.py"
LOG="$WORKSPACE/paper_scanner_test.log"
PIDFILE="$WORKSPACE/ws_cache/paper_test_daemon.pid"

# 写入守护进程自身的 PID
echo $$ > "$PIDFILE"

cd "$WORKSPACE" || exit 1

echo "[daemon] $(date -u '+%Y-%m-%d %H:%M UTC') paper_test 守护进程启动 (daemon PID=$$)" >> "$LOG"

while true; do
    echo "[daemon] $(date -u '+%Y-%m-%d %H:%M UTC') 启动 paper_scanner_test.py" >> "$LOG"
    python3 -u "$SCRIPT" > /dev/null 2>&1  # stdout 由进程内 log() 自己写文件，这里丢弃避免重复
    EXIT_CODE=$?
    echo "[daemon] $(date -u '+%Y-%m-%d %H:%M UTC') 进程退出 (code=$EXIT_CODE)，10秒后重启..." >> "$LOG"
    sleep 10
done
