#!/bin/bash
# robinhood_sniper watchdog — 完全静默，只打印结果给AI转发
if pgrep -f "robinhood_sniper.start" > /dev/null 2>&1; then
    echo "RUNNING"
else
    cd /root/.openclaw/workspace
    nohup python3 -m robinhood_sniper.start >> /tmp/sniper_watchdog.log 2>&1 &
    sleep 3
    if pgrep -f "robinhood_sniper.start" > /dev/null 2>&1; then
        echo "RESTARTED"
    else
        echo "RESTART_FAILED"
    fi
fi
