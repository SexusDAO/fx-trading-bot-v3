#!/bin/bash
# watchdog.sh — 进程守护 + 崩溃 Jarvis 推送
# 由 cron 每 5 分钟调用：*/5 * * * * /root/.openclaw/workspace/fomo_workflow/watchdog.sh

WF_DIR="/root/.openclaw/workspace/fomo_workflow"
LOCK_FILE="/tmp/fomo_workflow.lock"
LOG_FILE="$WF_DIR/trade.log"
CRASH_FLAG="$WF_DIR/.last_crash_notified"

# ── Jarvis 推送函数 ────────────────────────────────────────────────
notify_jarvis() {
    local msg="$1"
    # 通过 openclaw CLI 发送到 Jarvis（读取 config.py 中的 JARVIS_TARGET）
    local target
    target=$(python3 -c "
import sys
sys.path.insert(0, '$WF_DIR')
try:
    from config import JARVIS_TARGET
    print(JARVIS_TARGET)
except Exception:
    print('')
" 2>/dev/null)

    if [ -n "$target" ]; then
        openclaw message send \
            --channel jarvis \
            --to "$target" \
            --message "$msg" 2>/dev/null || true
    fi
}

# ── 主逻辑 ────────────────────────────────────────────────────────
if flock -n "$LOCK_FILE" true 2>/dev/null; then
    # 进程不存在，判断是崩溃还是首次启动
    NOW=$(date -u "+%Y-%m-%d %H:%M:%S UTC")

    # 获取崩溃原因（日志最后 20 行）
    LAST_LINES=""
    if [ -f "$LOG_FILE" ]; then
        LAST_LINES=$(tail -20 "$LOG_FILE" 2>/dev/null | grep -E "ERROR|Exception|Traceback|崩溃|CRITICAL" | tail -5)
    fi

    # 防止重复推送（同一崩溃只推一次）
    LAST_CRASH_MSG=""
    if [ -f "$CRASH_FLAG" ]; then
        LAST_CRASH_MSG=$(cat "$CRASH_FLAG" 2>/dev/null)
    fi

    CRASH_MSG="⚠️ fomo_workflow 进程已停止，正在重启...\n时间: $NOW"
    if [ -n "$LAST_LINES" ]; then
        CRASH_MSG="$CRASH_MSG\n最后错误:\n$LAST_LINES"
    fi

    if [ "$LAST_CRASH_MSG" != "$CRASH_MSG" ]; then
        notify_jarvis "$CRASH_MSG"
        echo "$CRASH_MSG" > "$CRASH_FLAG"
    fi

    echo "[$NOW] watchdog: 进程未运行，重新启动..."
    cd "$WF_DIR"
    nohup python3 main.py start >> "$LOG_FILE" 2>&1 &
    NEW_PID=$!
    echo "[$NOW] watchdog: 已重启 PID=$NEW_PID"

    # 等 10s 确认进程存活
    sleep 10
    if kill -0 "$NEW_PID" 2>/dev/null; then
        notify_jarvis "✅ fomo_workflow 已重启成功 PID=$NEW_PID ($NOW)"
        rm -f "$CRASH_FLAG"
    else
        notify_jarvis "❌ fomo_workflow 重启失败！请手动检查日志 ($NOW)"
    fi
else
    echo "[$(date -u "+%Y-%m-%d %H:%M:%S UTC")] watchdog: 正常运行中"
    # 清除崩溃标记（进程恢复后）
    rm -f "$CRASH_FLAG"
fi

# ── 每日自动运行 KOL 卖出复盘 ──────────────────────────────
LAST_REVIEW_DATE_FILE="/tmp/last_kol_review_date"
TODAY=$(date -u +%Y-%m-%d)
LAST_DATE=$(cat "$LAST_REVIEW_DATE_FILE" 2>/dev/null || echo "")

if [ "$TODAY" != "$LAST_DATE" ]; then
    echo "🔍 运行每日 KOL 卖出复盘..."
    python3 /root/.openclaw/workspace/fomo_workflow/kol_exit_review.py --push \
        >> "$LOG_FILE" 2>&1
    echo "$TODAY" > "$LAST_REVIEW_DATE_FILE"
    echo "✅ KOL 复盘完成，Deployer 黑名单已更新"
fi
