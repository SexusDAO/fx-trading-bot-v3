#!/bin/bash
# daily-git-commit.sh - 每日自动 commit & push 到 GitHub
# 每次提交都附带中文变更说明，方便复盘

cd /root/.openclaw/workspace

# 检查是否有变更
if git diff --quiet && git diff --cached --quiet && [ -z "$(git ls-files --others --exclude-standard)" ]; then
    echo "$(date -u '+%Y-%m-%d %H:%M UTC') - No changes, skip commit"
    exit 0
fi

# 生成变更文件列表
CHANGED=$(git diff --name-only; git ls-files --others --exclude-standard)
DATE=$(date -u '+%Y-%m-%d')
TIME=$(date -u '+%H:%M UTC')

# 自动识别本次变更类型，生成中文说明
MSG="每日备份 $DATE $TIME"

# 根据变更文件生成说明
NOTES=""
if echo "$CHANGED" | grep -q "ws_monitor.py"; then
    NOTES="${NOTES}\n- ws_monitor.py: 实时监控脚本更新"
fi
if echo "$CHANGED" | grep -q "position_state.json"; then
    NOTES="${NOTES}\n- position_state.json: 仓位状态变更"
fi
if echo "$CHANGED" | grep -q "price_gates.json"; then
    NOTES="${NOTES}\n- price_gates.json: 价位门控状态更新"
fi
if echo "$CHANGED" | grep -q "watchlist.json"; then
    NOTES="${NOTES}\n- watchlist.json: 观察区候选更新"
fi
if echo "$CHANGED" | grep -q "strategy-config.json"; then
    NOTES="${NOTES}\n- strategy-config.json: 策略配置变更"
fi
if echo "$CHANGED" | grep -q "binance_api.py"; then
    NOTES="${NOTES}\n- binance_api.py: API封装更新"
fi
if echo "$CHANGED" | grep -q "TRADING_RULES.md"; then
    NOTES="${NOTES}\n- TRADING_RULES.md: 交易规则更新"
fi
if echo "$CHANGED" | grep -q "CHANGELOG.md"; then
    NOTES="${NOTES}\n- CHANGELOG.md: 更新日志更新"
fi
if echo "$CHANGED" | grep -q "sl_tp_manager.py"; then
    NOTES="${NOTES}\n- sl_tp_manager.py: 止损止盈管理更新"
fi
if echo "$CHANGED" | grep -q "circuit_breaker.json"; then
    NOTES="${NOTES}\n- circuit_breaker.json: 熔断状态变更"
fi

# 构建完整 commit message
if [ -n "$NOTES" ]; then
    FULL_MSG="${MSG}

变更内容：$(echo -e $NOTES)

自动备份，记录当日所有代码和状态变化。"
else
    FULL_MSG="${MSG}

自动备份，记录当日所有代码和状态变化。"
fi

git add -A
git commit -m "$FULL_MSG"
git push origin main 2>&1

echo "$(date -u '+%Y-%m-%d %H:%M UTC') - Pushed to GitHub OK"
