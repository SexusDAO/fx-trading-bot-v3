#!/usr/bin/env python3
"""
sniper watchdog — 每 5 分钟由 cron 调用
检查三件事：
  1. robinhood_sniper 进程是否存活，挂了就拉起
  2. monitor_loop 是否卡死（日志 > 3 分钟没有新行）
  3. 最近 5 分钟日志是否有 UnboundLocalError / 持续异常
发现问题 → 自动重启进程 → 推送通知
"""
import subprocess, os, sys, time, re, datetime

LOG  = "/root/.openclaw/workspace/robinhood_sniper/sniper.log"
WORK = "/root/.openclaw/workspace"
PROC = "robinhood_sniper.start"

# ── 1. 检查进程 ────────────────────────────────────────────────────────
def get_pid():
    r = subprocess.run(["pgrep", "-f", PROC], capture_output=True, text=True)
    pids = r.stdout.strip().split()
    return int(pids[0]) if pids else None

def restart():
    pid = get_pid()
    if pid:
        subprocess.run(["kill", str(pid)], capture_output=True)
        time.sleep(2)
    # 清 pyc
    subprocess.run(
        "find /root/.openclaw/workspace/robinhood_sniper/__pycache__ -name '*.pyc' -delete",
        shell=True, capture_output=True
    )
    log = open(LOG, "a")
    subprocess.Popen(
        [sys.executable, "-m", PROC],
        cwd=WORK, stdout=log, stderr=log,
        start_new_session=True
    )
    time.sleep(3)
    return get_pid()

# ── 2. 检查日志活跃度 ──────────────────────────────────────────────────
def last_log_age_secs():
    """返回日志最后一行距现在多少秒"""
    try:
        r = subprocess.run(["tail", "-1", LOG], capture_output=True, text=True)
        line = r.stdout.strip()
        m = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
        if m:
            ts = datetime.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
            ts = ts.replace(tzinfo=datetime.timezone.utc)
            return (datetime.datetime.now(datetime.timezone.utc) - ts).total_seconds()
    except Exception:
        pass
    return 0

# ── 3. 检查近期异常 ────────────────────────────────────────────────────
def recent_errors(minutes=5):
    """返回最近 N 分钟内的错误行"""
    errors = []
    try:
        r = subprocess.run(["tail", "-200", LOG], capture_output=True, text=True)
        cutoff = time.time() - minutes * 60
        for line in r.stdout.splitlines():
            m = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
            if not m:
                continue
            ts = datetime.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=datetime.timezone.utc).timestamp()
            if ts < cutoff:
                continue
            if any(kw in line for kw in ["UnboundLocalError", "Error", "异常", "Traceback", "卡死"]):
                errors.append(line[:120])
    except Exception:
        pass
    return errors

# ── 通知 ──────────────────────────────────────────────────────────────
def notify(msg):
    try:
        import json
        payload = json.dumps({
            "action": "send",
            "target": "432401404:thread:019fcac8-4a93-7bea-a930-7f0e59e4897c",
            "message": msg,
            "channel": "jarvis",
        })
        subprocess.run(["openclaw", "message", "--json", payload],
                       capture_output=True, timeout=10)
    except Exception as e:
        print(f"notify failed: {e}")

# ── 主逻辑 ─────────────────────────────────────────────────────────────
def main():
    issues = []
    restarted = False

    pid = get_pid()
    log_age = last_log_age_secs()
    errors  = recent_errors(5)

    # 条件1：进程不存在
    if not pid:
        issues.append("❌ 进程不存在")

    # 条件2：日志超过 3 分钟没有新内容（卡死）
    if log_age > 180:
        issues.append(f"⏱️ 日志 {log_age/60:.1f}min 无更新（疑似卡死）")

    # 条件3：近 5min 内连续异常
    if len(errors) >= 3:
        issues.append(f"🔴 近5min {len(errors)} 条错误")

    if issues:
        new_pid = restart()
        restarted = True
        err_sample = "\n".join(errors[-2:]) if errors else ""
        msg = (
            f"🚨 **Sniper 自动修复**\n"
            f"\n"
            f"发现问题：\n" +
            "\n".join(f"  • {i}" for i in issues) +
            f"\n\n已自动重启 → PID {new_pid}\n"
            + (f"\n错误样本:\n`{err_sample}`" if err_sample else "")
        )
        notify(msg)
        print(f"[watchdog] 重启完成 PID={new_pid}, issues={issues}")
    else:
        print(f"[watchdog] OK — pid={pid} log_age={log_age:.0f}s errors={len(errors)}")

if __name__ == "__main__":
    main()
