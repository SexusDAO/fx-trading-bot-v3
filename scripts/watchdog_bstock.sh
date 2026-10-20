#!/bin/bash
# bstock_scanner watchdog — 完全静默，只打印结果给AI转发
if pgrep -f bstock_scanner.py > /dev/null 2>&1; then
    echo "RUNNING"
else
    cd /root/.openclaw/workspace
    nohup python3 bstock_scanner.py >> /tmp/bstock_scanner.log 2>&1 &
    sleep 2
    if pgrep -f bstock_scanner.py > /dev/null 2>&1; then
        echo "RESTARTED"
    else
        echo "RESTART_FAILED"
    fi
fi
