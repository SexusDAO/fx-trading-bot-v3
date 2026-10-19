#!/bin/bash
# fx_major/watchdog.sh — 守护 fx_major paper_scanner daemon
WORKSPACE="/root/.openclaw/workspace/fx_major"
LOG="$WORKSPACE/logs/major_paper.log"

if pgrep -f "fx_major/paper_scanner\|cd.*fx_major.*paper_scanner" > /dev/null 2>&1; then
    exit 0
fi

# Check by cwd
for pid in $(pgrep -f "paper_scanner.py"); do
    cwd=$(readlink /proc/$pid/cwd 2>/dev/null)
    if [ "$cwd" = "$WORKSPACE" ]; then
        exit 0
    fi
done

# Not running — restart
echo "[$(date -u '+%Y-%m-%d %H:%M UTC')] fx_major paper_scanner not running, restarting..." >> "$LOG"
cd "$WORKSPACE"
nohup python3 paper_scanner.py >> "$LOG" 2>&1 &
echo "[$(date -u '+%Y-%m-%d %H:%M UTC')] fx_major paper_scanner restarted PID=$!" >> "$LOG"
