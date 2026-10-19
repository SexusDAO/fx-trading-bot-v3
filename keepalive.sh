#!/bin/bash
# ws_monitor_guard.sh — 守护 ws_monitor.py 常驻运行
# 由 cron 每5分钟调用一次
WORKSPACE="/root/.openclaw/workspace"
LOG="$WORKSPACE/ws_monitor.log"
PID_FILE="$WORKSPACE/ws_monitor.pid"

# 检查进程是否在跑
if pgrep -f "ws_monitor.py" > /dev/null 2>&1; then
    # 跑着，更新 pid 文件
    pgrep -f "ws_monitor.py" > "$PID_FILE"
    exit 0
fi

# 没在跑，重启
echo "[$(date -u '+%Y-%m-%d %H:%M UTC')] ws_monitor 未运行，重启中..." >> "$LOG"
cd "$WORKSPACE"
nohup python3 ws_monitor.py >> "$LOG" 2>&1 &
echo $! > "$PID_FILE"
echo "[$(date -u '+%Y-%m-%d %H:%M UTC')] ws_monitor 已重启 PID=$(cat $PID_FILE)" >> "$LOG"

WORKSPACE="/root/.openclaw/workspace"
TAILSCALE_STATE="$WORKSPACE/tailscale-state"
TAILSCALE_SOCK="/tmp/tailscaled.sock"
LOG="/tmp/keepalive.log"

echo "[$(date)] keepalive check" >> "$LOG"

# 1. 确保 tailscaled 在跑
if ! pgrep -x tailscaled > /dev/null 2>&1; then
  echo "[$(date)] tailscaled not running, starting..." >> "$LOG"
  mkdir -p "$TAILSCALE_STATE"
  nohup "$WORKSPACE/tailscaled" --tun=userspace-networking \
    --statedir="$TAILSCALE_STATE" \
    --socket="$TAILSCALE_SOCK" >> /tmp/tailscaled.log 2>&1 &
  sleep 3
fi

# 2. 确保 tailscale 已连接（不重新授权，复用已保存的 state）
TS_STATUS=$("$WORKSPACE/tailscale" --socket="$TAILSCALE_SOCK" status 2>/dev/null | head -1)
if [ -z "$TS_STATUS" ] || echo "$TS_STATUS" | grep -qi "NeedsLogin\|Stopped"; then
  echo "[$(date)] tailscale needs reconnect" >> "$LOG"
  "$WORKSPACE/tailscale" --socket="$TAILSCALE_SOCK" up --authkey=tskey-auth-ksm1F9EKJM11CNTRL-WGSedjzeC6cdrPEKxWm46ccpQXLLnS13 --hostname=openclaw-server >> /tmp/tailscaled.log 2>&1 &
  sleep 5
fi

# 3. 确保 ws-proxy 在跑
if ! pgrep -f "ws-proxy.js" > /dev/null 2>&1; then
  echo "[$(date)] ws-proxy not running, starting..." >> "$LOG"
  nohup node "$WORKSPACE/ws-proxy.js" >> /tmp/ws-proxy.log 2>&1 &
fi

echo "[$(date)] keepalive done" >> "$LOG"
