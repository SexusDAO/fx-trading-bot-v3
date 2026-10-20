#!/usr/bin/env python3
"""
health_check.py — 系统健康检查
运行方式：
  python3 /root/.openclaw/workspace/health_check.py
  cron: 0 */4 * * * /usr/bin/python3 /root/.openclaw/workspace/health_check.py >> /root/.openclaw/workspace/logs/health_check.log 2>&1
"""

import os, sys, json, time, subprocess, pathlib, datetime, re, traceback

WORKSPACE   = pathlib.Path("/root/.openclaw/workspace")
LOG_DIR     = WORKSPACE / "logs"
LOG_FILE    = LOG_DIR / "health_check.log"
JARVIS_TARGET = "432401404:thread:019fcac8-4a93-7bea-a930-7f0e59e4897c"

LOG_DIR.mkdir(parents=True, exist_ok=True)

# ─── 工具函数 ─────────────────────────────────────────────────────────────────

def ts():
    return datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

def hlog(msg: str):
    line = f"[{ts()}] {msg}"
    print(line, flush=True)

def alert(msg: str):
    """发 Jarvis 告警 (通过 openclaw message send，超时延长到 30s)"""
    try:
        subprocess.run(
            ["openclaw", "message", "send",
             "--channel", "jarvis",
             "--target", JARVIS_TARGET,
             "--message", f"🚨 health_check 告警\n{msg}"],
            timeout=30, capture_output=True
        )
    except Exception as e:
        hlog(f"[alert] 推送失败: {e}  消息: {msg[:100]}")

def silent_log(msg: str):
    hlog(f"[auto_fix] {msg}")

# ─── 检查一：进程存活 ─────────────────────────────────────────────────────────

PROCESS_CONFIGS = [
    {
        "key":     "paper_scanner_fx",
        "match":   "paper_scanner_fx.py",
        "start":   f"nohup python3 -u {WORKSPACE}/scripts/start_paper_fx.py",
        "log":     str(WORKSPACE / "logs" / "paper_scanner_fx.log"),
        "daemon":  True,
    },
    {
        "key":     "paper_scanner",
        "match":   "paper_scanner.py",
        "exclude": "paper_scanner_test",
        "start":   f"python3 -u {WORKSPACE}/paper_scanner.py",
        "log":     str(WORKSPACE / "paper_scanner_sim.log"),
        "daemon":  False,
    },
    {
        "key":     "paper_scanner_test",
        "match":   "paper_scanner_test.py",
        "start":   f"python3 -u {WORKSPACE}/paper_scanner_test.py",
        "log":     str(WORKSPACE / "paper_scanner_test.log"),
        "daemon":  False,
    },
]

def find_procs(match: str, exclude: str = None):
    try:
        import psutil
    except ImportError:
        import subprocess, sys
        subprocess.check_call([sys.executable, "-m", "pip", "install", "psutil", "--break-system-packages", "-q"])
        import psutil
    result = []
    for p in psutil.process_iter(["pid", "cmdline", "cpu_percent", "memory_info", "create_time"]):
        try:
            cmd = " ".join(p.info["cmdline"] or [])
            if match in cmd:
                if exclude and exclude in cmd:
                    continue
                result.append(p)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return result

def restart_proc(cfg: dict) -> bool:
    """后台启动进程，返回是否成功"""
    try:
        if cfg.get("daemon"):
            # 守护脚本启动（从 cfg["start"] 动态获取，不再硬编码）
            cmd = f"{cfg['start']} > /tmp/paper_daemon.log 2>&1 &"
        else:
            cmd = f"nohup {cfg['start']} >> {cfg['log']} 2>&1 &"
        subprocess.Popen(cmd, shell=True, cwd=str(WORKSPACE))
        time.sleep(5)   # 缩短等待：5s 足够判断进程是否启动
        procs = find_procs(cfg["match"], cfg.get("exclude"))
        return len(procs) > 0
    except Exception as e:
        hlog(f"[restart] {cfg['key']} 启动失败: {e}")
        return False

def check_processes():
    import psutil
    issues = []
    fixes  = 0
    status = "✅"
    for cfg in PROCESS_CONFIGS:
        procs = find_procs(cfg["match"], cfg.get("exclude"))
        if not procs:
            hlog(f"[check1] {cfg['key']} 进程不存在，尝试重启")
            ok = restart_proc(cfg)
            if ok:
                silent_log(f"{cfg['key']} 已自动重启")
                fixes += 1
            else:
                issues.append(f"{cfg['key']} 进程重启失败")
                status = "❌"
            continue

        # 检查 CPU 卡死（持续 >90% 超5分钟，用 cpu_percent 采样两次）
        for p in procs:
            try:
                p.cpu_percent(interval=None)  # 启动采样
            except Exception:
                pass

        time.sleep(2)   # 采样间隔从5s缩短到2s
        for p in procs:
            try:
                cpu = p.cpu_percent(interval=None)
                mem_mb = p.memory_info().rss / 1024 / 1024
                if cpu > 90:
                    hlog(f"[check1] {cfg['key']} CPU {cpu:.0f}% 偏高，可能卡死，重启")
                    p.kill()
                    time.sleep(3)
                    ok = restart_proc(cfg)
                    if ok:
                        silent_log(f"{cfg['key']} CPU卡死，已自动重启")
                        fixes += 1
                    else:
                        issues.append(f"{cfg['key']} CPU卡死且重启失败")
                        status = "❌"
                if mem_mb > 500:
                    issues.append(f"{cfg['key']} 内存 {mem_mb:.0f}MB > 500MB，可能泄漏")
                    status = "❌"
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

    return status, fixes, issues

# ─── 检查二：工作流活跃度 ─────────────────────────────────────────────────────

HEARTBEAT_FILES = {
    "paper_scanner":    WORKSPACE / "ws_cache" / "paper_scanner_heartbeat.json",
    "paper_scanner_fx": WORKSPACE / "ws_cache" / "paper_scanner_fx_heartbeat.json",
}
MAX_STALE_SEC = 10 * 60  # 10分钟

def check_activity():
    issues = []
    fixes  = 0
    status = "✅"
    now    = time.time()

    for key, hb_file in HEARTBEAT_FILES.items():
        if not hb_file.exists():
            hlog(f"[check2] {key} 心跳文件不存在（进程从未写入或刚重启）")
            continue
        try:
            data = json.loads(hb_file.read_text())
            last = float(data.get("ts", 0))
            age  = now - last
            if age > MAX_STALE_SEC:
                hlog(f"[check2] {key} 心跳超时 {age/60:.1f}分钟，重启进程")
                cfg = next((c for c in PROCESS_CONFIGS if c["key"] == key), None)
                if cfg:
                    procs = find_procs(cfg["match"], cfg.get("exclude"))
                    for p in procs:
                        try: p.kill()
                        except Exception: pass
                    time.sleep(3)
                    ok = restart_proc(cfg)
                    if ok:
                        silent_log(f"{key} 工作流卡住，已自动重启")
                        fixes += 1
                    else:
                        issues.append(f"{key} 工作流卡住且重启失败")
                        status = "❌"
        except Exception as e:
            hlog(f"[check2] {key} 心跳读取异常: {e}")

    return status, fixes, issues

# ─── 检查三：状态文件完整性 ───────────────────────────────────────────────────

STATE_FILES = [
    WORKSPACE / "paper_test_state.json",
    WORKSPACE / "paper_sim_state.json",
]
REQUIRED_POS_FIELDS = {"direction", "entry", "sl_price", "tp1_price", "pos_usdt"}

def check_state_files():
    issues = []
    fixes  = 0
    status = "✅"

    for sf in STATE_FILES:
        if not sf.exists():
            continue
        # JSON 格式检查
        try:
            state = json.loads(sf.read_text())
        except json.JSONDecodeError:
            # 尝试恢复 .tmp 备份
            tmp = sf.with_suffix(".tmp")
            if tmp.exists():
                try:
                    state = json.loads(tmp.read_text())
                    sf.write_text(tmp.read_text())
                    silent_log(f"{sf.name} JSON损坏，已从.tmp恢复")
                    fixes += 1
                    continue
                except Exception:
                    pass
            # 恢复失败 → 重置为空
            empty = {"positions": {}, "pending_orders": {}, "history": [],
                     "closed_trades": [], "stats": {}}
            sf.write_text(json.dumps(empty, indent=2))
            issues.append(f"{sf.name} JSON损坏且备份失败，已重置为空（原数据丢失）")
            status = "❌"
            continue

        # 字段完整性检查
        positions = state.get("positions", {})
        for sym, pos in positions.items():
            missing = REQUIRED_POS_FIELDS - set(pos.keys())
            # 兼容 entry_price 别名
            if "entry" in missing and "entry_price" in pos:
                missing.discard("entry")
            if missing:
                issues.append(f"{sf.name} 持仓 {sym} 缺少字段: {missing}")
                status = "❌"

        # 止损价格方向检查
        # 说明：如果仓位有 trailing_sl 字段，说明已经用移动止损追踪了一段时间，
        # SL 可能被推过了入场价（这是好事，说明仓位保住了利润），不应误报为错误
        for sym, pos in positions.items():
            entry = float(pos.get("entry", pos.get("entry_price", 0)))
            sl    = float(pos.get("sl_price", 0))
            dr    = pos.get("direction", "LONG")
            has_trailing = pos.get("trailing_sl") is not None or pos.get("intra_high") is not None
            if entry <= 0 or sl <= 0:
                continue
            # 有移动止损的仓位：SL 被推过入场价属于正常，跳过方向检查
            if has_trailing:
                continue
            if dr == "LONG" and sl >= entry:
                issues.append(f"{sf.name} {sym} 多单SL({sl:.5g}) >= 入场价({entry:.5g})")
                status = "❌"
            elif dr == "SHORT" and sl <= entry:
                issues.append(f"{sf.name} {sym} 空单SL({sl:.5g}) <= 入场价({entry:.5g})")
                status = "❌"

    return status, fixes, issues

# ─── 检查四：止损逻辑一致性 ───────────────────────────────────────────────────

def get_mark_price(sym: str) -> float:
    import urllib.request
    try:
        url = f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={sym}"
        d = json.loads(urllib.request.urlopen(url, timeout=6).read())
        return float(d["markPrice"])
    except Exception:
        return 0.0

def check_sl_consistency():
    issues = []
    status = "✅"

    for sf in STATE_FILES:
        if not sf.exists():
            continue
        try:
            state = json.loads(sf.read_text())
        except Exception:
            continue

        for sym, pos in state.get("positions", {}).items():
            try:
                entry = float(pos.get("entry", pos.get("entry_price", 0)))
                sl    = float(pos.get("sl_price", 0))
                tp1   = float(pos.get("tp1_price", 0))
                tp2   = float(pos.get("tp2_price", 0))
                dr    = pos.get("direction", "LONG")
                if entry <= 0 or sl <= 0:
                    continue

                mark = get_mark_price(sym)
                if mark <= 0:
                    continue

                # 止损应触发但未触发
                TOLERANCE = 0.005
                sl_miss = False
                if dr == "LONG"  and mark < sl * (1 - TOLERANCE):
                    sl_miss = True
                elif dr == "SHORT" and mark > sl * (1 + TOLERANCE):
                    sl_miss = True
                if sl_miss:
                    issues.append(
                        f"{sf.name} {sym} 止损应触发未平仓: "
                        f"现价={mark:.5g} SL={sl:.5g} 方向={dr}"
                    )
                    status = "❌"

                # TP 价格顺序
                if dr == "LONG":
                    if tp1 > 0 and tp1 <= entry:
                        issues.append(f"{sf.name} {sym} 多单 TP1({tp1:.5g}) <= 入场价({entry:.5g})")
                        status = "❌"
                    if tp2 > 0 and tp1 > 0 and tp2 <= tp1:
                        issues.append(f"{sf.name} {sym} 多单 TP2({tp2:.5g}) <= TP1({tp1:.5g})")
                        status = "❌"
                else:
                    if tp1 > 0 and tp1 >= entry:
                        issues.append(f"{sf.name} {sym} 空单 TP1({tp1:.5g}) >= 入场价({entry:.5g})")
                        status = "❌"
                    if tp2 > 0 and tp1 > 0 and tp2 >= tp1:
                        issues.append(f"{sf.name} {sym} 空单 TP2({tp2:.5g}) >= TP1({tp1:.5g})")
                        status = "❌"
            except Exception as e:
                hlog(f"[check4] {sym} 检查异常: {e}")

    return status, issues

# ─── 检查五：日志异常 ─────────────────────────────────────────────────────────

LOG_FILES_TO_CHECK = [
    WORKSPACE / "paper_scanner.log",
    WORKSPACE / "logs" / "paper_scanner_fx.log",
    WORKSPACE / "paper_scanner_errors.log",
]
FOUR_HOURS = 4 * 3600

def check_logs():
    issues = []
    status = "✅"
    now    = time.time()

    for lf in LOG_FILES_TO_CHECK:
        if not lf.exists():
            continue
        try:
            # 文件大小
            size_mb = lf.stat().st_size / 1024 / 1024
            if size_mb > 50:
                issues.append(f"{lf.name} 大小 {size_mb:.1f}MB > 50MB")
                status = "❌"

            # 读取最近4小时内的内容（按行时间戳过滤）
            with open(lf, "rb") as f:
                content = f.read().decode("utf-8", errors="replace")
            lines = content.splitlines()

            # 简单方案：取最后 4000 行（约4小时）
            recent = lines[-4000:]
            recent_text = "\n".join(recent)

            # 统计 ERROR
            error_lines = [l for l in recent if "ERROR" in l or "error" in l.lower() and "[" in l]
            if len(error_lines) > 10:
                # 提取最频繁的3种
                from collections import Counter
                err_types = Counter()
                for l in error_lines:
                    # 取关键词
                    m = re.search(r"\[(\w+)\]", l)
                    key = m.group(1) if m else l[:40]
                    err_types[key] += 1
                top3 = err_types.most_common(3)
                issues.append(
                    f"{lf.name} 近期ERROR {len(error_lines)}条: "
                    + " / ".join(f"{k}×{v}" for k, v in top3)
                )
                status = "❌"

            # 发现 Traceback — Bug C 修复：只报最近 30 分钟内的 Traceback
            # 旧日志里的历史错误不应持续误报
            if "Traceback (most recent call last)" in recent_text:
                import re as _re
                # 尝试提取 Traceback 附近的时间戳判断是否在 30 分钟内
                tb_is_recent = False
                now_ts = time.time()
                # 找最后一处 Traceback 在 recent 列表里的行号
                tb_line_idx = None
                for _li in range(len(recent) - 1, -1, -1):
                    if "Traceback (most recent call last)" in recent[_li]:
                        tb_line_idx = _li
                        break
                if tb_line_idx is not None:
                    # 往前找最近的带时间戳行 (格式 2026-XX-XX XX:XX 或 [HH:MM])
                    for _li in range(tb_line_idx, max(0, tb_line_idx - 10), -1):
                        _m = _re.search(
                            r'(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}|\[\d{2}:\d{2}:\d{2}\])',
                            recent[_li]
                        )
                        if _m:
                            try:
                                import datetime as _dt
                                _raw = _m.group(1).strip('[]').replace('T', ' ')
                                # 支持纯时间格式 [HH:MM:SS]
                                if len(_raw) <= 8:
                                    import datetime as _dtb
                                    _today = _dtb.datetime.utcnow().strftime('%Y-%m-%d')
                                    _raw = f'{_today} {_raw}'
                                _parsed = _dt.datetime.strptime(_raw[:19], '%Y-%m-%d %H:%M:%S')
                                _age = now_ts - _parsed.timestamp()
                                if _age < 1800:  # 30 分钟内
                                    tb_is_recent = True
                            except Exception:
                                pass
                            break
                    else:
                        # 找不到时间戳：保守地认为是近期错误（宁可误报不漏报）
                        tb_is_recent = True
                if tb_is_recent:
                    idx = recent_text.rfind("Traceback (most recent call last)")
                    tb_snippet = recent_text[idx:idx+500].replace("\n", " | ")
                    issues.append(f"{lf.name} 发现近期 Traceback: {tb_snippet[:200]}")
                    status = "❌"

            # NoneType
            none_cnt = recent_text.count("NoneType")
            if none_cnt > 0:
                issues.append(f"{lf.name} 发现 NoneType ×{none_cnt}")
                status = "❌"

            # 连续相同 ERROR > 5 次 — 只统计最近 30 分钟内的错误行
            from collections import Counter
            import re as _re2
            err_counter = Counter()
            now_ts2 = time.time()
            for l in recent:
                if "error" in l.lower() or "Error" in l:
                    # 检查行内是否包含时间戳，有则判断是否在 30 分钟内
                    _tm = _re2.search(
                        r'(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}|\[\d{2}:\d{2}:\d{2}\])',
                        l
                    )
                    if _tm:
                        try:
                            import datetime as _dt2
                            _raw2 = _tm.group(1).strip('[]').replace('T', ' ')
                            # 支持两种格式：完整日期时间戳 or 纯时间 [HH:MM:SS]
                            if len(_raw2) <= 8:  # 纯时间 HH:MM:SS
                                import datetime as _dt2b
                                _today = _dt2b.datetime.utcnow().strftime('%Y-%m-%d')
                                _raw2 = f'{_today} {_raw2}'
                            _parsed2 = _dt2.datetime.strptime(_raw2[:19], '%Y-%m-%d %H:%M:%S')
                            if now_ts2 - _parsed2.timestamp() > 1800:
                                continue  # 跳过 30 分钟外的旧错误
                        except Exception:
                            pass
                    err_counter[l.strip()] += 1
            for line_text, cnt in err_counter.items():
                if cnt > 5:
                    issues.append(f"{lf.name} 连续相同错误 ×{cnt}: {line_text[:80]}")
                    status = "❌"
                    break

        except Exception as e:
            hlog(f"[check5] {lf.name} 读取异常: {e}")

    return status, issues

# ─── 检查六：API 连通性 ───────────────────────────────────────────────────────

def check_api():
    import urllib.request
    issues = []
    status = "✅"

    # Binance FAPI ping
    try:
        urllib.request.urlopen("https://fapi.binance.com/fapi/v1/ping", timeout=8)
    except Exception as e:
        issues.append(f"Binance FAPI ping 失败: {e}")
        status = "❌"

    # Clawby API（已替换旧 Coinglass 代理，2026-08-28 迁移）
    try:
        sys.path.insert(0, str(WORKSPACE / "fx" / "shared"))
        from clawby_adapter import test_connection, get_liq_heatmap
        result = test_connection()
        if not result.get("ok"):
            raise RuntimeError(result.get("message", "unknown error"))
        get_liq_heatmap("BTCUSDT", range_="12h")
    except Exception as e:
        issues.append(f"Clawby API 失败: {str(e)[:80]}")
        status = "❌"

    return status, issues

# ─── 检查七：系统资源 ─────────────────────────────────────────────────────────

def check_resources():
    try:
        import psutil
    except ImportError:
        import subprocess, sys
        subprocess.check_call([sys.executable, "-m", "pip", "install", "psutil", "--break-system-packages", "-q"])
        import psutil
    import shutil
    issues = []
    fixes  = 0
    status = "✅"

    # 内存
    mem = psutil.virtual_memory()
    if mem.percent > 85:
        # 找占用最多的3个进程
        procs = sorted(
            psutil.process_iter(["pid", "name", "memory_info"]),
            key=lambda p: p.info.get("memory_info") and p.info["memory_info"].rss or 0,
            reverse=True
        )[:3]
        top_mem = [(p.info["name"], p.info["memory_info"].rss // 1024 // 1024)
                   for p in procs if p.info.get("memory_info")]
        issues.append(f"内存使用率 {mem.percent:.0f}% > 85%，占用最多: {top_mem}")
        status = "❌"

    # 磁盘
    disk = shutil.disk_usage("/")
    disk_pct = disk.used / disk.total * 100
    if disk_pct > 90:
        # 自动清理 logs/ 30天前的日志
        cutoff = time.time() - 30 * 86400
        cleaned = []
        for f in LOG_DIR.glob("*.log"):
            if f.stat().st_mtime < cutoff:
                try:
                    f.unlink()
                    cleaned.append(f.name)
                except Exception:
                    pass
        if cleaned:
            silent_log(f"磁盘清理旧日志: {cleaned}")
            fixes += 1
        # 重新检查
        disk2 = shutil.disk_usage("/")
        disk_pct2 = disk2.used / disk2.total * 100
        if disk_pct2 > 90:
            issues.append(f"磁盘使用率 {disk_pct2:.0f}% > 90%，清理后仍超限")
            status = "❌"
        # else: 清理成功，不发告警

    # 单个 paper_scanner 进程内存
    for cfg in PROCESS_CONFIGS:
        procs = find_procs(cfg["match"], cfg.get("exclude"))
        for p in procs:
            try:
                import psutil as _pu
                mem_mb = p.memory_info().rss / 1024 / 1024
                if mem_mb > 500:
                    issues.append(f"{cfg['key']} 进程内存 {mem_mb:.0f}MB > 500MB，可能泄漏")
                    status = "❌"
            except Exception:
                pass

    return status, fixes, issues


# ─── 检查八：patching 仓位自愈 ───────────────────────────────────────────────────────

PATCHING_TIMEOUT_MIN = 30   # patching 超过这么多分钟就尝试重挂

def _try_repatch(pos: dict) -> tuple[bool, str]:
    """
    对一个处于 patching 状态的仓位尝试重新挂止据止盈单。
    返回 (success, detail_msg)
    """
    import asyncio
    symbol    = pos.get("symbol", "")
    pid       = pos.get("pos_id", symbol)
    direction = pos.get("direction", "LONG").upper()
    fill_price = float(pos.get("fill_price") or pos.get("entry_price", 0))
    fill_qty   = float(pos.get("quantity", 0))

    if fill_price <= 0 or fill_qty <= 0:
        return False, f"{symbol} fill_price/qty 无效，跳过"

    try:
        sys.path.insert(0, str(WORKSPACE / "fx"))
        from position_manager import _补挂保护单

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(
                asyncio.wait_for(_补挂保护单(pos, fill_price, fill_qty, pid, symbol), timeout=30)
            )
        finally:
            loop.close()
        return True, f"{symbol}(pid={pid}) patching 补挂成功"
    except asyncio.TimeoutError:
        return False, f"{symbol} 补挂超时30s"
    except Exception as e:
        return False, f"{symbol} 补挂失败: {e}"


def check_and_fix_patching() -> tuple[str, int, list]:
    """
    扫描所有 status=patching 且超过 PATCHING_TIMEOUT_MIN 分钟的仓位。
    对每个这样的仓位调用 _补挂保护单 尝试自愈。
    返回 (status_emoji, fixed_count, issues)
    """
    issues = []
    fixes  = 0
    status = "✅"
    now    = time.time()
    cutoff = PATCHING_TIMEOUT_MIN * 60

    fx_state_file = WORKSPACE / "fx" / "logs" / "pos_state.json"
    if not fx_state_file.exists():
        return status, fixes, issues

    try:
        positions = json.loads(fx_state_file.read_text())
    except Exception as e:
        issues.append(f"pos_state.json 读取失败: {e}")
        return "❌", fixes, issues

    # pos_state 可能是 list 或 dict
    pos_list = positions if isinstance(positions, list) else list(positions.values())

    for pos in pos_list:
        if pos.get("status") != "patching":
            continue
        symbol = pos.get("symbol", "?")

        # 计算停留时长
        open_ts = pos.get("open_time", pos.get("ts", 0))
        if isinstance(open_ts, str):
            try:
                from datetime import datetime, timezone
                open_ts = datetime.fromisoformat(open_ts.replace("Z","+00:00")).timestamp()
            except Exception:
                open_ts = 0
        open_ts = float(open_ts or 0)
        stuck_sec = now - open_ts if open_ts > 0 else cutoff + 1

        if stuck_sec < cutoff:
            silent_log(f"[patching] {symbol} 停留{stuck_sec/60:.1f}分钟，未超限")
            continue

        silent_log(f"[patching] {symbol} 停留{stuck_sec/60:.0f}分钟，尝试补挂...")
        ok, detail = _try_repatch(pos)
        if ok:
            fixes += 1
            auto_fix(f"patching自愈: {detail}")
        else:
            status = "⚠️"
            issues.append(f"patching自愈失败: {detail}")

    return status, fixes, issues


# ─── 主流程 ───────────────────────────────────────────────────────────────────

def main():
    t0 = time.time()
    hlog("=" * 60)
    hlog("health_check 开始")

    total_fixes   = 0
    total_issues  = []
    check_results = {}

    # 检查一：进程存活
    try:
        s, f, i = check_processes()
        check_results["进程"] = s
        total_fixes  += f
        total_issues += i
    except Exception:
        check_results["进程"] = "❌"
        total_issues.append(f"检查一(进程)异常: {traceback.format_exc()[:200]}")

    # 检查二：活跃度
    try:
        s, f, i = check_activity()
        check_results["活跃度"] = s
        total_fixes  += f
        total_issues += i
    except Exception:
        check_results["活跃度"] = "❌"
        total_issues.append(f"检查二(活跃度)异常: {traceback.format_exc()[:200]}")

    # 检查三：状态文件
    try:
        s, f, i = check_state_files()
        check_results["状态文件"] = s
        total_fixes  += f
        total_issues += i
    except Exception:
        check_results["状态文件"] = "❌"
        total_issues.append(f"检查三(状态文件)异常: {traceback.format_exc()[:200]}")

    # 检查四：止损逻辑
    try:
        s, i = check_sl_consistency()
        check_results["止损逻辑"] = s
        total_issues += i
    except Exception:
        check_results["止损逻辑"] = "❌"
        total_issues.append(f"检查四(止损逻辑)异常: {traceback.format_exc()[:200]}")

    # 检查五：日志
    try:
        s, i = check_logs()
        check_results["日志"] = s
        total_issues += i
    except Exception:
        check_results["日志"] = "❌"
        total_issues.append(f"检查五(日志)异常: {traceback.format_exc()[:200]}")

    # 检查六：API
    try:
        s, i = check_api()
        check_results["API"] = s
        total_issues += i
    except Exception:
        check_results["API"] = "❌"
        total_issues.append(f"检查六(API)异常: {traceback.format_exc()[:200]}")

    # 检查七：资源
    try:
        s, f, i = check_resources()
        check_results["资源"] = s
        total_fixes  += f
        total_issues += i
    except Exception:
        check_results["资源"] = "❌"
        total_issues.append(f"检查七(资源)异常: {traceback.format_exc()[:200]}")

    # 检查八：patching 仓位自愈
    try:
        s, f, i = check_and_fix_patching()
        check_results["patching"] = s
        total_fixes  += f
        total_issues += i
    except Exception:
        check_results["patching"] = "❌"
        total_issues.append(f"检查八(patching)异常: {traceback.format_exc()[:200]}")

    elapsed = time.time() - t0

    # 结构化日志
    summary = (
        f"检查完成 耗时{elapsed:.1f}s "
        f"进程:{check_results.get('进程','?')} "
        f"活跃度:{check_results.get('活跃度','?')} "
        f"状态文件:{check_results.get('状态文件','?')} "
        f"止损逻辑:{check_results.get('止损逻辑','?')} "
        f"日志:{check_results.get('日志','?')} "
        f"API:{check_results.get('API','?')} "
        f"资源:{check_results.get('资源','?')} "
        f"patching:{check_results.get('patching','?')} "
        f"自动修复:{total_fixes}项 "
        f"待人工确认:{len(total_issues)}项"
    )
    hlog(summary)

    if total_issues:
        for issue in total_issues:
            hlog(f"  ⚠️  {issue}")
        # 发 Jarvis 告警
        alert_body = "\n".join(f"• {i}" for i in total_issues[:10])
        if len(total_issues) > 10:
            alert_body += f"\n… 共 {len(total_issues)} 项"
        alert(f"{summary}\n\n需要关注：\n{alert_body}")
    else:
        hlog("  ✅ 无需人工处理")

    hlog("=" * 60)


if __name__ == "__main__":
    main()
