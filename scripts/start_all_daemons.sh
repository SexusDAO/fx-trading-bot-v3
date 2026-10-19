#!/bin/bash
# start_all_daemons.sh — 服务器重启后拉起所有守护进程
# 由 @reboot crontab 触发，也可手动运行
# 用法：bash scripts/start_all_daemons.sh

WORKSPACE="/root/.openclaw/workspace"
cd "$WORKSPACE" || exit 1

mkdir -p ws_cache

START_LOG="$WORKSPACE/ws_cache/start_all.log"
echo "[start_all] $(date -u '+%Y-%m-%dT%H:%M UTC') 服务器重启，开始拉起所有守护进程" >> "$START_LOG"

# ── 清理残留锁文件（防止进程启动时卡死）─────────────────────────────────────
for LOCK in /tmp/coinglass_rate.lock /tmp/paper_main.lock /tmp/paper_test.lock; do
    if [ -f "$LOCK" ]; then
        rm -f "$LOCK"
        echo "[start_all] 清理锁文件: $LOCK" >> "$START_LOG"
    fi
done

# ── 等待网络就绪（最多 60 秒）────────────────────────────────────────────────
echo "[start_all] 等待网络就绪..." >> "$START_LOG"
for i in $(seq 1 12); do
    if curl -s --max-time 5 https://api.binance.com/api/v3/ping > /dev/null 2>&1; then
        echo "[start_all] 网络就绪（第 ${i} 次尝试）" >> "$START_LOG"
        break
    fi
    sleep 5
done

# ── 1. paper_scanner.py（主力 paper）────────────────────────────────────────
if pgrep -f "paper_scanner.py" > /dev/null 2>&1 && ! pgrep -f "paper_scanner_test" > /dev/null 2>&1; then
    echo "[start_all] paper_main 已在运行，跳过" >> "$START_LOG"
else
    nohup bash "$WORKSPACE/scripts/run_paper_main_daemon.sh" > /dev/null 2>&1 &
    echo "[start_all] paper_main 守护进程已启动 (PID=$!)" >> "$START_LOG"
fi

sleep 2

# ── 2. paper_scanner_test.py（测试版 paper）─────────────────────────────────
if pgrep -f "paper_scanner_test.py" > /dev/null 2>&1; then
    echo "[start_all] paper_test 已在运行，跳过" >> "$START_LOG"
else
    nohup bash "$WORKSPACE/scripts/run_paper_test_daemon.sh" > /dev/null 2>&1 &
    echo "[start_all] paper_test 守护进程已启动 (PID=$!)" >> "$START_LOG"
fi

sleep 2

# ── 3. coindata_ws.py（KOL/巨鲸信号 WebSocket）──────────────────────────────
if pgrep -f "coindata_ws.py" > /dev/null 2>&1; then
    echo "[start_all] coindata_ws 已在运行，跳过" >> "$START_LOG"
else
    nohup bash "$WORKSPACE/scripts/run_coindata_ws_daemon.sh" > /dev/null 2>&1 &
    echo "[start_all] coindata_ws 守护进程已启动 (PID=$!)" >> "$START_LOG"
fi

echo "[start_all] 全部完成，当前进程：" >> "$START_LOG"
pgrep -a -f "paper_scanner|coindata_ws" >> "$START_LOG" 2>&1

echo "[start_all] $(date -u '+%Y-%m-%dT%H:%M UTC') done" >> "$START_LOG"
