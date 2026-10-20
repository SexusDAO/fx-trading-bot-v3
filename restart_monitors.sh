#!/bin/bash
# restart_monitors.sh — 交易进程守护脚本（纯 bash，不消耗 AI token）
# 由系统 crontab 每5分钟调用一次
# 日志：/tmp/monitor_guard.log

WORKSPACE="/root/.openclaw/workspace"
LOG="/tmp/monitor_guard.log"
MAX_LOG_LINES=500

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"
}

# 日志滚动（超过500行则截断保留后300行）
if [ -f "$LOG" ] && [ "$(wc -l < "$LOG")" -gt "$MAX_LOG_LINES" ]; then
    tail -300 "$LOG" > "${LOG}.tmp" && mv "${LOG}.tmp" "$LOG"
fi

RESTARTED=0

# ── ws_monitor.py ────────────────────────────────────────────────────────────
if ! pgrep -f "ws_monitor.py" > /dev/null 2>&1; then
    log "⚠️  ws_monitor.py 未运行，正在重启..."
    cd "$WORKSPACE"
    nohup python3 ws_monitor.py >> /tmp/ws_monitor.log 2>&1 &
    sleep 3
    if pgrep -f "ws_monitor.py" > /dev/null 2>&1; then
        log "✅ ws_monitor.py 重启成功 (PID=$(pgrep -f ws_monitor.py))"
        RESTARTED=1
    else
        log "❌ ws_monitor.py 重启失败！请手动检查"
        RESTARTED=2
    fi
fi

# ── smart_copy_trader_ws.py ──────────────────────────────────────────────────
if ! pgrep -f "smart_copy_trader_ws.py" > /dev/null 2>&1; then
    log "⚠️  smart_copy_trader_ws.py 未运行，正在重启..."
    cd "$WORKSPACE"
    nohup python3 smart_copy_trader_ws.py >> /tmp/smart_copy_trader_ws.log 2>&1 &
    sleep 3
    if pgrep -f "smart_copy_trader_ws.py" > /dev/null 2>&1; then
        log "✅ smart_copy_trader_ws.py 重启成功 (PID=$(pgrep -f smart_copy_trader_ws.py))"
        RESTARTED=1
    else
        log "❌ smart_copy_trader_ws.py 重启失败！请手动检查"
        RESTARTED=2
    fi
fi

# 重启通知已注释 — 不再主动推送重启消息
# if [ "$RESTARTED" -eq 1 ]; then
#     openclaw message --channel jarvis \
#         --to "432401404:thread:019f9203-3b29-7d9e-bfb4-b5bd3ce6bec4" \
#         "🔄 进程守护：检测到进程中断，已自动重启，当前运行正常。" 2>/dev/null || true
# fi

# if [ "$RESTARTED" -eq 2 ]; then
#     openclaw message --channel jarvis \
#         --to "432401404:thread:019f9203-3b29-7d9e-bfb4-b5bd3ce6bec4" \
#         "❌ 进程守护：自动重启失败，请手动检查！查看日志：tail -50 /tmp/monitor_guard.log" 2>/dev/null || true
# fi

exit 0
