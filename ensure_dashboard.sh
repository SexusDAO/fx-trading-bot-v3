#!/bin/bash
# 自愈脚本：确保 dashboard + ngrok 持续运行
# 由 cron 每5分钟调用，自动处理容器重置场景
WS="/root/.openclaw/workspace"
LOG="$WS/logs/dashboard_guard.log"
mkdir -p "$WS/logs"
TS=$(date -u '+%H:%M:%S')

changed=0

# ── 0. 确保 Flask 已安装 ──
python3 -c "from flask import Flask" 2>/dev/null || {
    echo "[$TS] 安装 flask..." >> "$LOG"
    pip3 install flask flask-cors --break-system-packages -q 2>/dev/null
}

# ── 1. Flask dashboard —— 已注释，dashboard 功能暂停 ──
# if ! pgrep -f "dashboard_api.py" > /dev/null; then
#     echo "[$TS] Flask 未运行，重启中..." >> "$LOG"
#     cd "$WS"
#     python3 -c "
# import os,sys,subprocess
# pid=os.fork()
# if pid>0: sys.exit(0)
# os.setsid()
# pid2=os.fork()
# if pid2>0: sys.exit(0)
# os.chdir('/root/.openclaw/workspace')
# with open('logs/dashboard.log','ab') as log:
#     proc=subprocess.Popen(['python3','-u','dashboard_api.py'],stdout=log,stderr=log,cwd='/root/.openclaw/workspace')
#     open('/tmp/dashboard.pid','w').write(str(proc.pid))
# " 2>/dev/null
#     sleep 3
#     if pgrep -f "dashboard_api.py" > /dev/null; then
#         echo "[$TS] Flask 启动成功" >> "$LOG"
#         changed=1
#     else
#         echo "[$TS] Flask 启动失败" >> "$LOG"
#     fi
# fi

# ── 2. ngrok —— 已注释，ngrok 功能暂停 ──
# if ! pgrep -f "ngrok" > /dev/null; then
#     echo "[$TS] ngrok 未运行，检查安装..." >> "$LOG"
#     if ! command -v ngrok > /dev/null 2>&1; then
#         echo "[$TS] 安装 ngrok..." >> "$LOG"
#         curl -sL https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-linux-amd64.tgz -o /tmp/ngrok.tgz 2>/dev/null
#         tar -xzf /tmp/ngrok.tgz -C /usr/local/bin 2>/dev/null
#         echo "[$TS] ngrok 安装完成" >> "$LOG"
#     fi
#     nohup ngrok http 8765 --log=stdout > /tmp/ngrok_guard.log 2>&1 &
#     sleep 7
#     NEW_URL=$(grep -o 'url=https://[^[:space:]]*' /tmp/ngrok_guard.log 2>/dev/null | tail -1 | sed 's/url=//')
#     if [ -n "$NEW_URL" ]; then
#         echo "[$TS] ngrok 启动成功: $NEW_URL" >> "$LOG"
#         echo "$NEW_URL" > /tmp/dashboard_url.txt
#         changed=1
#         echo "NEW_URL=$NEW_URL"
#     else
#         echo "[$TS] ngrok 启动失败" >> "$LOG"
#     fi
# else
#     URL=$(grep -o 'url=https://[^[:space:]]*' /tmp/ngrok_guard.log 2>/dev/null | tail -1 | sed 's/url=//')
#     [ -z "$URL" ] && URL=$(grep -o 'url=https://[^[:space:]]*' /tmp/ngrok2.log 2>/dev/null | tail -1 | sed 's/url=//')
#     [ -n "$URL" ] && echo "$URL" > /tmp/dashboard_url.txt
# fi

echo "changed=$changed"
