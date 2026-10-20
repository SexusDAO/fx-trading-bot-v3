#!/usr/bin/env python3
"""
test_scanner_loop_timing.py — 验证 scanner_loop 的 :10/:40 触发时机

结论判断标准：
  在任意分钟 m，下次触发时间应该是：
    m < 10  → 等到本小时 :10
    10 <= m < 40 → 等到本小时 :40
    40 <= m < 60 → 等到下一小时 :10

检查的核心计算逻辑（从 ws_monitor.py 原文抄出）：
    if minute < 10:
        wait_sec = (10 - minute) * 60 - now.second
    elif minute < 40:
        wait_sec = (40 - minute) * 60 - now.second
    else:
        wait_sec = (70 - minute) * 60 - now.second  # ← 这里是关键
"""

import sys, math
from datetime import datetime, timezone, timedelta

PASS = "✅ PASS"
FAIL = "❌ FAIL"
results = []

def calc_wait(minute, second=0):
    """原始代码逻辑，原样复制"""
    if minute < 10:
        wait_sec = (10 - minute) * 60 - second
    elif minute < 40:
        wait_sec = (40 - minute) * 60 - second
    else:
        wait_sec = (70 - minute) * 60 - second   # 原代码
    return wait_sec

def calc_wait_fixed(minute, second=0):
    """正确逻辑"""
    if minute < 10:
        wait_sec = (10 - minute) * 60 - second
    elif minute < 40:
        wait_sec = (40 - minute) * 60 - second
    else:
        # 等到下一小时 :10 = (60 - minute + 10) * 60 - second
        wait_sec = (70 - minute) * 60 - second
    return wait_sec

def next_trigger_time(minute, second=0):
    """从当前分钟推算下次触发的分钟数（相对于当前小时）"""
    wait = calc_wait(minute, second)
    # 换算成"当前小时内的分钟偏移"
    total_seconds_in_hour = minute * 60 + second + wait
    target_minute = (total_seconds_in_hour // 60) % 60
    next_hour = total_seconds_in_hour >= 3600
    return target_minute, next_hour, wait

print("=" * 62)
print("scanner_loop 时机模拟：从每个分钟出发，下次触发在哪里？")
print("=" * 62)
print(f"{'当前分钟':>8}  {'等待秒':>8}  {'等待分':>8}  {'触发时刻':>12}  {'是否正确':>10}")
print("-" * 62)

bugs = []
for m in range(0, 60):
    wait = calc_wait(m, second=0)
    target_m, next_h, _ = next_trigger_time(m, 0)
    expected = 10 if m < 10 else (40 if m < 40 else 10)
    is_next_hour_expected = (m >= 40)

    correct = (target_m == expected) and (next_h == is_next_hour_expected)
    marker = "✅" if correct else "❌"

    label = f"下小时:{target_m:02d}" if next_h else f":  {target_m:02d}"
    exp_label = f"下小时:10" if is_next_hour_expected else f":{expected:02d}"

    print(f"  m={m:02d}      {wait:>7}s   {wait/60:>7.1f}m   触发@{label}  期望@{exp_label}  {marker}")

    if not correct:
        bugs.append((m, wait, target_m, next_h, expected, is_next_hour_expected))

print()
if bugs:
    print(f"❌ 发现 {len(bugs)} 个分钟计算有误：")
    for m, wait, got_m, got_next_h, exp_m, exp_next_h in bugs:
        print(f"   m={m:02d}: 等待{wait}s → 触发@{'下小时:' if got_next_h else ':'}{got_m:02d}, 期望@{'下小时:' if exp_next_h else ':'}{exp_m:02d}")
else:
    print("✅ 所有分钟时机计算正确")

print()
print("=" * 62)
print("关键区间验证")
print("=" * 62)

# 特别验证关键边界
cases = [
    (0,  0, 10,  False, ":00 → 等10分到 :10"),
    (9,  0, 10,  False, ":09 → 等1分到 :10"),
    (10, 0, 40,  False, ":10 刚触发完 → 等30分到 :40"),
    (11, 0, 40,  False, ":11 → 等29分到 :40"),
    (39, 0, 40,  False, ":39 → 等1分到 :40"),
    (40, 0, 10,  True,  ":40 刚触发完 → 等30分到下小时:10"),
    (41, 0, 10,  True,  ":41 → 等29分到下小时:10"),
    (59, 0, 10,  True,  ":59 → 等11分到下小时:10"),
]

all_ok = True
for m, s, exp_m, exp_next_h, desc in cases:
    wait = calc_wait(m, s)
    got_m, got_next_h, _ = next_trigger_time(m, s)
    ok = (got_m == exp_m) and (got_next_h == exp_next_h)
    marker = "✅" if ok else "❌"
    if not ok:
        all_ok = False
    print(f"  {marker}  m={m:02d}  等{wait//60}m{wait%60}s  →  {'下小时:' if got_next_h else ':'}{got_m:02d}  ({desc})")

print()
results.append(("时机计算无bug", len(bugs) == 0))
results.append(("关键边界全部正确", all_ok))

# ── 额外：验证 :40 分之后的等待时间是否合理 ──
print("=" * 62)
print(":40~:59 区间详细等待时长")
print("=" * 62)
wait_reasonable = True
for m in range(40, 60):
    wait = calc_wait(m, 0)
    expected_wait = (70 - m) * 60
    # :40 时等 30 分钟，:59 时等 11 分钟，都应该 > 0
    label_ok = wait > 0
    if not label_ok:
        wait_reasonable = False
    print(f"  m={m:02d}: wait={wait}s ({wait//60}m{wait%60}s)  {'✅' if label_ok else '❌ 负值!'}")

results.append((":40以后等待时长均为正值", wait_reasonable))

print()
print("=" * 62)
print("汇总")
print("=" * 62)
for name, ok in results:
    print(f"  {'✅' if ok else '❌'}  {name}")

all_passed = all(ok for _, ok in results)
print()
if not all_passed:
    print("❌ 存在问题，见上方详细输出")
else:
    print("✅ scanner_loop 时机逻辑正确，每小时 :10 和 :40 触发")

sys.exit(0 if all_passed else 1)
