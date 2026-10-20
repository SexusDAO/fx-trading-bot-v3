#!/bin/bash
# watchdog_all.sh — 检查三个模拟进程，全部正常输出 ALL_OK，有问题输出一行摘要
WORKSPACE=/root/.openclaw/workspace
ISSUES=()

check_proc() {
    local name="$1"
    local match="$2"
    local cmd="$3"
    local log="$4"

    if pgrep -f "$match" > /dev/null 2>&1; then
        return 0
    fi

    # 尝试重启
    cd "$WORKSPACE" && nohup $cmd >> "$log" 2>&1 &
    sleep 5
    if pgrep -f "$match" > /dev/null 2>&1; then
        ISSUES+=("$name 已重启")
    else
        ISSUES+=("$name 重启失败")
    fi
}

check_proc "paper_scanner(动态)" "paper_scanner\.py$" \
    "python3 -u paper_scanner.py" "paper_scanner_sim.log"

check_proc "paper_scanner_test(固定100U)" "paper_scanner_test\.py" \
    "python3 -u paper_scanner_test.py" "paper_scanner_test.log"

check_proc "paper_scanner_fx" "paper_scanner_fx\.py" \
    "python3 -u scripts/start_paper_fx.py" "logs/paper_scanner_fx.log"

if [ ${#ISSUES[@]} -eq 0 ]; then
    echo "ALL_OK"
else
    echo "⚠️ watchdog: $(IFS=', '; echo "${ISSUES[*]}")"
fi
