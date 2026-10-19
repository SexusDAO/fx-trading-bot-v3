#!/bin/bash
cd /root/.openclaw/workspace
while true; do
    echo "$(date): 启动 meme_new_launch_monitor.py" >> meme_monitor_watchdog.log
    python3 meme_new_launch_monitor.py
    EXIT_CODE=$?
    echo "$(date): 进程退出 (code=$EXIT_CODE)，3秒后重启" >> meme_monitor_watchdog.log
    sleep 3
done
