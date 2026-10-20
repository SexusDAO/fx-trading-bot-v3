#!/usr/bin/env python3
"""
start_paper_fx.py — paper_scanner_fx.py 自动重启守护进程
与 start_paper_test.py 结构一致，确保模拟FX进程持续运行
"""
import subprocess, time, sys, os, datetime

WORKSPACE = "/root/.openclaw/workspace"
SCANNER   = f"{WORKSPACE}/paper_scanner_fx.py"
LOG_FILE  = f"{WORKSPACE}/logs/paper_scanner_fx.log"
FX_DIR    = f"{WORKSPACE}/fx"   # cwd 必须在 fx/ 才能正确 import scanner

def log(msg):
    ts = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    line = f"[daemon] {ts} {msg}"
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass

log(f"守护进程启动 (PID={os.getpid()})")

restart_count = 0
while True:
    restart_count += 1
    log(f"启动 paper_scanner_fx.py (第{restart_count}次)")
    try:
        with open(LOG_FILE, "a") as lf:
            proc = subprocess.run(
                [sys.executable, "-u", SCANNER],
                cwd=FX_DIR,
                stdout=lf,   # 子进程 stdout 写入日志，不再由守护进程重复输出
                stderr=lf,
                timeout=None,
            )
        log(f"paper_scanner_fx.py 退出，返回码={proc.returncode}")
    except Exception as e:
        log(f"启动失败: {e}")
    log("10秒后重启...")
    time.sleep(10)
