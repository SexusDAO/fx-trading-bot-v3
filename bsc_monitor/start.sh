#!/bin/sh
# bsc_monitor 守护启动（幂等：已在跑则跳过）
cd /root/.openclaw/workspace/bsc_monitor
if ps -ef | grep -v grep | grep -q "python3 bsc_watch.py"; then
    echo "bsc_watch already running"
    exit 0
fi
nohup python3 bsc_watch.py > monitor.log 2>&1 &
echo "started pid=$!"
