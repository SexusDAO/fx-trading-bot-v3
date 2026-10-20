#!/usr/bin/env python3
"""
scripts/register_crons.py — v3 系统 cron 任务注册

运行：python3 scripts/register_crons.py [--dry-run] [--force]

--dry-run  只打印命令，不执行
--force    先删除同名旧任务，再重新注册
"""

import subprocess, json, sys, argparse
from pathlib import Path

WORKSPACE  = Path(__file__).parent.parent
USER_ID    = "432401404"
THREAD_ID  = "019fc621-af64-76d7-8b2f-f89f5bafe19c"
TARGET     = f"jarvis:{USER_ID}:thread:{THREAD_ID}"

# ── cron 任务定义表 ───────────────────────────────────────────────────────────
# script 路径相对于 WORKSPACE/v3
CRONS = [
    {
        "name":     "v3-scanner-main",
        "schedule": "--every 30m",
        "message":  (
            "运行 v3 主扫描：python3 /root/.openclaw/workspace/v3/llm_tasks/scanner_llm.py "
            "的 run_scan()。扫描完成后推送播报，无开仓信号则静默。"
        ),
        "announce": True,
    },
    {
        "name":     "v3-patrol-sl",
        "schedule": "--every 3m",
        "message":  (
            "执行止损守卫：python3 /root/.openclaw/workspace/v3/guards/patrol_sl_check.py "
            "检查所有持仓止损单是否存在，缺失则补挂。无操作时回复 HEARTBEAT_OK。"
        ),
        "announce": True,
    },
    {
        "name":     "v3-circuit-reset",
        "schedule": "--at 'cron 0 0 * * *'",
        "message":  (
            "执行每日熔断重置："
            "python3 /root/.openclaw/workspace/v3/guards/circuit_daily_reset.py "
            "重置 circuit_breaker.json open=False 和每日计数器。完成后回复 HEARTBEAT_OK。"
        ),
        "announce": True,
    },
    {
        "name":     "v3-daily-report",
        "schedule": "--at 'cron 0 1 * * *'",
        "message":  (
            "生成每日交易复盘报告：读取 /root/.openclaw/workspace/v3/logs/trade_log.jsonl "
            "统计今日盈亏、胜率、各信号类型表现，推送简洁日报。"
        ),
        "announce": True,
    },
]

# ── 工具函数 ──────────────────────────────────────────────────────────────────

def get_existing_crons() -> dict[str, str]:
    """返回 {name: id} 的现有 cron 任务字典"""
    try:
        result = subprocess.run(
            ["openclaw", "cron", "list", "--json"],
            capture_output=True, text=True
        )
        jobs = json.loads(result.stdout)
        return {j["name"]: j["id"] for j in jobs}
    except Exception:
        # 降级：从文本解析
        result = subprocess.run(
            ["openclaw", "cron", "list"],
            capture_output=True, text=True
        )
        existing = {}
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2:
                job_id, name = parts[0], parts[1]
                if len(job_id) == 36:  # UUID 格式
                    existing[name] = job_id
        return existing


def delete_cron(job_id: str, dry_run: bool):
    cmd = ["openclaw", "cron", "rm", job_id]
    print(f"  删除: {' '.join(cmd)}")
    if not dry_run:
        subprocess.run(cmd, check=True)


def register_cron(cron: dict, dry_run: bool):
    schedule = cron["schedule"]
    # 处理 cron 表达式（需要用 --at）
    if schedule.startswith("--at"):
        # --at 'cron ...' → 展开为两个参数
        import shlex
        schedule_parts = shlex.split(schedule)
    else:
        schedule_parts = schedule.split()

    cmd = (
        ["openclaw", "cron", "add",
         "--name", cron["name"]]
        + schedule_parts +
        ["--message", cron["message"],
         "--channel", "jarvis",
         "--to", TARGET]
    )
    if cron.get("announce"):
        cmd.append("--announce")

    print(f"  注册: openclaw cron add --name {cron['name']} {cron['schedule']} ...")
    if not dry_run:
        subprocess.run(cmd, check=True)
        print(f"  ✅ {cron['name']} 注册成功")
    else:
        print(f"  [dry-run] {' '.join(cmd)}")


# ── 主流程 ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force",   action="store_true",
                        help="先删除同名任务再重新注册")
    args = parser.parse_args()

    print(f"=== v3 cron 注册 ({'dry-run' if args.dry_run else 'LIVE'}) ===")
    print(f"Target: {TARGET}\n")

    existing = get_existing_crons()
    print(f"现有 cron 任务：{list(existing.keys())}\n")

    for cron in CRONS:
        name = cron["name"]
        if name in existing:
            if args.force:
                print(f"[{name}] 已存在，--force 删除后重新注册")
                delete_cron(existing[name], args.dry_run)
            else:
                print(f"[{name}] 已存在，跳过（用 --force 强制重新注册）")
                continue
        else:
            print(f"[{name}] 新注册")
        register_cron(cron, args.dry_run)
        print()

    print("=== 完成 ===")
    if not args.dry_run:
        subprocess.run(["openclaw", "cron", "list"])


if __name__ == "__main__":
    main()
