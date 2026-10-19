#!/usr/bin/env python3
# ============================================================
# main.py — 启动入口 + 状态查看
# 用法：
#   python3 main.py start    # 启动工作流
#   python3 main.py status   # 查看当前持仓
#   python3 main.py log      # 查看最近日志
# ============================================================
import sys, json, os, time

LOCK_FILE = "/tmp/fomo_workflow.lock"

def _acquire_lock():
    """用 flock 文件锁防止双进程，进程退出后自动释放"""
    import fcntl
    # 全局持有锁文件描述符，进程退出时 OS 自动释放
    global _lock_fd
    _lock_fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_fd.write(str(os.getpid()))
        _lock_fd.flush()
        import os as _os
        _os.fsync(_lock_fd.fileno())
    except BlockingIOError:
        print("⚠️  工作流已在运行，退出")
        sys.exit(1)

def _release_lock():
    try:
        import fcntl
        fcntl.flock(_lock_fd, fcntl.LOCK_UN)
        _lock_fd.close()
        # 不再 os.remove，避免 inode 被替换后 flock 失效
    except Exception:
        pass

def cmd_start():
    import asyncio
    from ws_monitor import main
    _acquire_lock()
    try:
        print("🦞 FOMO 打土狗工作流启动…")
        asyncio.run(main())
    finally:
        _release_lock()

def cmd_status():
    from config import STATE_FILE
    if not os.path.exists(STATE_FILE):
        print("暂无持仓记录")
        return
    with open(STATE_FILE) as f:
        s = json.load(f)
    positions = s.get("positions", {})
    if not positions:
        print("✅ 当前无持仓")
        return
    print(f"📊 当前持仓 {len(positions)} 个：")
    for addr, pos in positions.items():
        hold_min = (time.time() - pos.get('opened_at', time.time())) / 60
        print(f"  {pos['symbol']:10s} | 策略={','.join(pos['strategies'])} | 成本={pos['size_usd']}U"
              f" | 买入MC=${pos.get('entry_mc',0):,.0f} | 持仓{hold_min:.0f}分钟"
              f" | 剩余{pos.get('remaining_pct',1)*100:.0f}%"
              f" | 已过档位={pos.get('exits_done',[])} | {addr[:10]}…")

def cmd_log(lines=50):
    from config import LOG_FILE
    if not os.path.exists(LOG_FILE):
        print("暂无日志")
        return
    with open(LOG_FILE) as f:
        all_lines = f.readlines()
    for line in all_lines[-lines:]:
        print(line, end="")

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "start"
    if cmd == "start":
        cmd_start()
    elif cmd == "status":
        cmd_status()
    elif cmd == "log":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 50
        cmd_log(n)
    else:
        print("用法: python3 main.py [start|status|log]")
