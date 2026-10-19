#!/usr/bin/env python3
"""
Dashboard 守护进程启动器
用法: python3 start_dashboard_daemon.py
会双 fork 变成守护进程，彻底脱离 shell session
"""
import os, sys, subprocess

def daemonize():
    pid = os.fork()
    if pid > 0: sys.exit(0)
    os.setsid()
    pid2 = os.fork()
    if pid2 > 0: sys.exit(0)
    os.chdir("/root/.openclaw/workspace")
    os.makedirs("logs", exist_ok=True)
    with open("logs/dashboard.log", "ab") as log:
        proc = subprocess.Popen(
            ["python3", "-u", "dashboard_api.py"],
            stdout=log, stderr=log,
            cwd="/root/.openclaw/workspace"
        )
        with open("/tmp/dashboard.pid", "w") as f:
            f.write(str(proc.pid))
        proc.wait()

# ⚠️ Dashboard 守护进程已暂停 — 如需恢复请取消注释
# if __name__ == "__main__":
#     daemonize()
