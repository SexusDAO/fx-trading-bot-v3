#!/bin/bash
# 进程守护脚本 - 纯 shell，不走 LLM
# 检测 5 个核心进程，自动重启

PROCESSES=(
    "ws_monitor.py"
    "circuit_reset_listener.py"
    "dashboard.py"
    "tailscale_keepalive.py"
    "sol_trader.py"
)

LOG_FILE="/root/.openclaw/workspace/logs/guardian.log"

log() {
    echo "[$(date -u '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG_FILE"
}

check_and_restart() {
    local process_name=$1
    local script_name=$2
    
    # 使用 pgrep 检测进程
    if ! pgrep -f "$process_name" > /dev/null; then
        log "⚠️  进程 $process_name 未运行，正在重启..."
        
        # 启动进程
        cd /root/.openclaw/workspace
        nohup python3 "$script_name" >> "/root/.openclaw/workspace/logs/${script_name%.py}.log" 2>&1 &
        
        log "✅ 已启动 $script_name (PID: $!)"
    else
        pid=$(pgrep -f "$process_name")
        log "✓ $process_name 运行中 (PID: $pid)"
    fi
}

# 主循环
log "=== 进程守护启动 ==="

while true; do
    check_and_restart "ws_monitor.py" "ws_monitor.py"
    check_and_restart "circuit_reset_listener.py" "scripts/circuit_reset_listener.py"
    check_and_restart "dashboard.py" "dashboard.py"
    check_and_restart "tailscale" "tailscale_keepalive.sh"
    check_and_restart "sol_trader.py" "sol_trader.py"
    
    # 每 60 秒检查一次
    sleep 60
done
