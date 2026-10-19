#!/usr/bin/env python3
"""
start_paper_test.py — paper_scanner_test.py 自动重启守护进程
进程崩溃后等待 10 秒自动重启，保证止损心跳持续运行
"""
import subprocess, time, sys, os, datetime

WORKSPACE = "/root/.openclaw/workspace"
LOG_FILE  = f"{WORKSPACE}/paper_scanner_test.log"

def log(msg):
    ts = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    line = f"[daemon] {ts} {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass

log(f"守护进程启动 (PID={os.getpid()})")

restart_count = 0
while True:
    restart_count += 1
    log(f"启动 paper_scanner_test.py (第{restart_count}次)")
    try:
        proc = subprocess.run(
            [sys.executable, "-u", f"{WORKSPACE}/paper_scanner_test.py"],
            cwd=WORKSPACE,
        )
        exit_code = proc.returncode
    except Exception as e:
        exit_code = -1
        log(f"启动异常: {e}")

    log(f"进程退出 code={exit_code}，10秒后重启...")
    time.sleep(10)
