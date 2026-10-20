#!/bin/bash
# watchdog_paper.sh — 守护 paper_scanner 和 paper_scanner_test 进程
# 每60秒检查一次，挂了自动拉起
WORKSPACE="/root/.openclaw/workspace"
LOG="$WORKSPACE/watchdog_paper.log"

log() { echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] $1" | tee -a "$LOG"; }

start_main() {
    log "[watchdog] 启动 paper_scanner (main)..."
    cd "$WORKSPACE"
    nohup python3 paper_scanner.py >> paper_scanner.log 2>&1 &
    echo $! > /tmp/paper_scanner_main.pid
    log "[watchdog] paper_scanner main PID=$!"
}

start_test() {
    log "[watchdog] 启动 paper_scanner_test..."
    cd "$WORKSPACE"
    nohup python3 paper_scanner_test.py >> paper_scanner_test.log 2>&1 &
    echo $! > /tmp/paper_scanner_test.pid
    log "[watchdog] paper_scanner_test PID=$!"
}

start_bstock() {
    log "[watchdog] 启动 bstock_scanner..."
    cd "$WORKSPACE"
    nohup python3 bstock_scanner.py >> /tmp/bstock_scanner.log 2>&1 &
    echo $! > /tmp/bstock_scanner.pid
    log "[watchdog] bstock_scanner PID=$!"
}

# 初次启动
pgrep -f "paper_scanner.py" > /dev/null 2>&1 || start_main
pgrep -f "paper_scanner_test.py" > /dev/null 2>&1 || start_test
pgrep -f "bstock_scanner.py" > /dev/null 2>&1 || start_bstock

while true; do
    sleep 60

    # 检查 main
    if ! pgrep -f "paper_scanner.py" > /dev/null 2>&1; then
        log "[watchdog] ⚠️ paper_scanner (main) 已停止，重启中..."
        start_main
    fi

    # 检查 test
    if ! pgrep -f "paper_scanner_test.py" > /dev/null 2>&1; then
        log "[watchdog] ⚠️ paper_scanner_test 已停止，重启中..."
        start_test
    fi

    # 检查 bstock
    if ! pgrep -f "bstock_scanner.py" > /dev/null 2>&1; then
        log "[watchdog] ⚠️ bstock_scanner 已停止，重启中..."
        start_bstock
    fi
done
