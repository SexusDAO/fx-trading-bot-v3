#!/bin/bash
# 一键恢复 Dashboard + ngrok
# 用法: bash /root/.openclaw/workspace/restart_dashboard.sh
# ⚠️ 已注释 dashboard + ngrok 功能，暂停使用

set -e
WS="/root/.openclaw/workspace"
cd "$WS"

# === 检查/安装 ngrok — 已注释 ===
# if ! command -v ngrok &>/dev/null; then
#   echo "安装 ngrok..."
#   curl -sL https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-linux-amd64.tgz -o /tmp/ngrok.tgz
#   tar -xzf /tmp/ngrok.tgz -C /usr/local/bin
#   echo "ngrok $(ngrok version) 安装完成"
# else
#   echo "ngrok 已存在: $(ngrok version)"
# fi

# === 重启 Dashboard API — 已注释 ===
# pkill -f "dashboard_api.py" 2>/dev/null || true
# sleep 1
# python3 -c "
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
# "
# sleep 3
# if pgrep -f dashboard_api.py > /dev/null; then
#   echo "Flask 启动成功 PID=$(cat /tmp/dashboard.pid 2>/dev/null)"
# else
#   echo "Flask 启动失败，查看 logs/dashboard.log"
#   exit 1
# fi

# === 启动 ngrok — 已注释 ===
# pkill -f ngrok 2>/dev/null || true
# sleep 1
# nohup ngrok http 8765 --log=stdout > /tmp/ngrok2.log 2>&1 &
# sleep 6
# URL=$(grep -o 'url=https://[^[:space:]]*' /tmp/ngrok2.log | tail -1 | sed 's/url=//')
# if [ -n "$URL" ]; then
#   echo ""
#   echo "✅ Dashboard 已上线"
#   echo "👉 访问地址: $URL"
# else
#   echo "ngrok 启动失败，查看 /tmp/ngrok2.log"
# fi

echo "⚠️ restart_dashboard.sh: dashboard + ngrok 功能已暂停，如需恢复请取消注释"
