#!/usr/bin/env python3
"""
start_paper_sim.py — paper_scanner.py（动态仓位版）自动重启守护进程
进程崩溃后等待 10 秒自动重启
用法：nohup python3 -u scripts/start_paper_sim.py > /tmp/paper_sim_daemon.log 2>&1 &
"""
import subprocess, time, sys, os, datetime

WORKSPACE = "/root/.openclaw/workspace"
LOG_FILE  = f"{WORKSPACE}/paper_scanner.log"

def log(msg):
    ts = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    line = f"[daemon] {ts} {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass

log(f"动态仓位版守护进程启动 (PID={os.getpid()})")

restart_count = 0
while True:
    restart_count += 1
    log(f"启动 paper_scanner.py (第{restart_count}次)")
    try:
        proc = subprocess.run(
            [sys.executable, "-u", f"{WORKSPACE}/paper_scanner.py"],
            cwd=WORKSPACE,
        )
        exit_code = proc.returncode
    except Exception as e:
        exit_code = -1
        log(f"启动异常: {e}")

    log(f"进程退出 code={exit_code}，10秒后重启...")
    time.sleep(10)
