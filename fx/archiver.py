"""
archiver.py — 每日仓位数据归档到 Git
触发点：position_manager._generate_daily_report() 末尾调用 archive_daily()
"""
from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger("archiver")

_REPO_ROOT = Path(__file__).parent.parent   # trading-bot-backup/
_FX_ROOT   = Path(__file__).parent         # fx/


def _bj_yesterday() -> str:
    """返回北京时间昨天的日期字符串，格式 YYYY-MM-DD"""
    now_bj = datetime.now(timezone.utc) + timedelta(hours=8)
    return (now_bj - timedelta(days=1)).strftime("%Y-%m-%d")


def _day_range_ms(date_str: str) -> tuple[int, int]:
    """返回北京时间某天 00:00 ~ 23:59:59.999 对应的 UTC 毫秒时间戳"""
    bj_tz   = timezone(timedelta(hours=8))
    day     = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=bj_tz)
    start   = int(day.timestamp() * 1000)
    end     = start + 86400_000 - 1
    return start, end


def archive_daily(date_str: str | None = None) -> bool:
    """
    读 trade_log.jsonl + pos_state.json，生成日归档 JSON 并 git commit。
    date_str: 指定日期（YYYY-MM-DD），默认为北京时间昨天。
    返回 True 表示成功，False 表示失败（不抛异常）。
    """
    try:
        from config import ARCHIVE_ENABLED, ARCHIVE_GIT_PUSH, ARCHIVE_DIR
    except Exception as e:
        logger.warning(f"[archiver] config import 失败: {e}")
        return False

    if not ARCHIVE_ENABLED:
        return True

    date_str = date_str or _bj_yesterday()
    start_ms, end_ms = _day_range_ms(date_str)

    # ── 1. 读平仓记录 ─────────────────────────────────────────────────────────
    trade_log_path = _FX_ROOT / "logs" / "trade_log.jsonl"
    trades = []
    try:
        if trade_log_path.exists():
            with open(trade_log_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    # 筛选：北京时间昨天平仓（close_time 字段）
                    ct = r.get("close_time", 0)
                    if start_ms <= ct <= end_ms:
                        trades.append(r)
    except Exception as e:
        logger.error(f"[archiver] 读 trade_log 失败: {e}")
        return False

    # ── 2. 当前持仓快照 ───────────────────────────────────────────────────────
    open_snapshot = []
    try:
        from shared.state import get_all_positions
        all_pos = get_all_positions()
        for p in all_pos:
            if p.get("status") == "open":
                open_snapshot.append({
                    "symbol":                p.get("symbol"),
                    "direction":             p.get("direction"),
                    "mode":                  p.get("mode"),
                    "sig_type":              p.get("sig_type"),
                    "entry_price":           p.get("entry_price"),
                    "current_sl":            p.get("sl_price"),
                    "tp1_price":             p.get("tp1_price"),
                    "tp2_price":             p.get("tp2_price"),
                    "quantity":              p.get("quantity"),
                    "leverage":              p.get("leverage"),
                    "open_time":             p.get("open_time"),
                    "holding_minutes":       round(
                        (datetime.now(timezone.utc).timestamp() * 1000 - (p.get("open_time") or 0)) / 60000, 1
                    ),
                    "signal_strength":       p.get("signal_strength"),
                    "tp1_filled":            p.get("tp1_filled", False),
                    "trailing_stop_activated": p.get("trailing_stop_activated", False),
                    "atr_at_entry":          p.get("atr_at_entry"),
                })
    except Exception as e:
        logger.warning(f"[archiver] 读持仓快照失败: {e}")

    # ── 3. 计算 summary ───────────────────────────────────────────────────────
    total   = len(trades)
    wins    = [t for t in trades if (t.get("pnl_usdt") or 0) > 0]
    losses  = [t for t in trades if (t.get("pnl_usdt") or 0) <= 0]
    total_pnl = sum(t.get("pnl_usdt", 0) for t in trades)
    win_rate  = len(wins) / total if total else 0.0

    by_mode: dict = {}
    by_strength: dict = {}
    for t in trades:
        m  = t.get("mode") or t.get("sig_type") or "unknown"
        s  = t.get("signal_strength") or "unknown"
        pnl = t.get("pnl_usdt", 0) or 0
        is_win = pnl > 0

        if m not in by_mode:
            by_mode[m] = {"count": 0, "win": 0, "pnl": 0.0}
        by_mode[m]["count"] += 1
        by_mode[m]["pnl"]   += pnl
        if is_win:
            by_mode[m]["win"] += 1

        if s not in by_strength:
            by_strength[s] = {"count": 0, "win": 0, "pnl": 0.0}
        by_strength[s]["count"] += 1
        by_strength[s]["pnl"]   += pnl
        if is_win:
            by_strength[s]["win"] += 1

    # 给 pnl 保留2位小数
    for d in list(by_mode.values()) + list(by_strength.values()):
        d["pnl"] = round(d["pnl"], 2)

    # ── 4. 格式化 trades 字段 ──────────────────────────────────────────────────
    trade_records = []
    for t in trades:
        ot = t.get("open_time") or 0
        ct = t.get("close_time") or 0
        hold_min = round((ct - ot) / 60000, 1) if ot and ct else None
        trade_records.append({
            "trade_id":          t.get("trade_id") or t.get("pos_id"),
            "symbol":            t.get("symbol"),
            "direction":         t.get("direction"),
            "mode":              t.get("mode") or t.get("sig_type"),
            "signal_strength":   t.get("signal_strength"),
            "volatility_tier":   t.get("volatility_tier"),
            "entry_price":       t.get("entry_price") or t.get("fill_price"),
            "exit_price":        t.get("exit_price"),
            "sl_price":          t.get("sl_price"),
            "tp1_price":         t.get("tp1_price"),
            "tp2_price":         t.get("tp2_price"),
            "sl_dist_pct":       t.get("sl_dist_pct"),
            "rr_tp1":            t.get("rr_tp1"),
            "quantity":          t.get("quantity") or t.get("fill_qty"),
            "leverage":          t.get("leverage"),
            "pnl_usdt":          round(t.get("pnl_usdt", 0) or 0, 4),
            "pnl_pct":           round(t.get("pnl_pct", 0) or 0, 4),
            "holding_minutes":   hold_min,
            "open_time":         ot,
            "close_time":        ct,
            "close_reason":      t.get("close_reason") or t.get("exit_reason"),
            "tp1_filled":        t.get("tp1_filled"),
            "tp2_filled":        t.get("tp2_filled"),
            "sl_degraded":       t.get("sl_degraded"),
            "liq_data_available": t.get("liq_data_available"),
            "vol_ratio_at_entry": t.get("vol_ratio_at_entry") or (
                (t.get("judge_detail") or {}).get("vol_ratio")
            ),
            "momentum_coeff":    t.get("momentum_coeff"),
            "consistency_score": t.get("consistency_score"),
            "judge_detail":      t.get("judge_detail"),
            # ── v3.66 复盘字段：清算区降级信息 ──────────────────────────────
            "liq_entry_raw":        t.get("liq_entry_raw"),
            "liq_entry_dist_atr":  t.get("liq_entry_dist_atr"),
            "entry_degraded_reason": t.get("entry_degraded_reason"),
        })

    # ── 4b. 资金费率成本汇总（从 pos_state 里的 funding_cost_total 读取）─────────
    funding_summary: dict = {}
    try:
        from shared.state import get_all_positions as _gp
        _all_pos = _gp()
        _total_paid = 0.0
        _total_received = 0.0
        _by_position = []
        for _p in _all_pos:
            _fc = float(_p.get("funding_cost_total", 0) or 0)
            if _fc == 0:
                continue
            _dir = (_p.get("direction") or "long").upper()
            if _fc < 0:
                _total_paid += _fc
            else:
                _total_received += _fc
            _by_position.append({
                "symbol":    _p.get("symbol"),
                "direction": _dir,
                "funding_cost": round(_fc, 4),
            })
        try:
            from shared.binance_api import get_account as _ga
            _acc = _ga()
            _eq = float(_acc.get("totalMarginBalance", 0) or 0)
        except Exception:
            _eq = 0.0
        _net = round(_total_paid + _total_received, 4)
        funding_summary = {
            "total_paid":          round(_total_paid, 4),
            "total_received":      round(_total_received, 4),
            "net_cost":            _net,
            "net_cost_pct_of_equity": round(_net / (_eq + 1e-9) * 100, 4) if _eq > 0 else None,
            "by_position":         _by_position,
        }
    except Exception as _fe:
        logger.warning(f"[archiver] funding_summary 计算失败（非致命）: {_fe}")

    # ── 4c. 滑点汇总（从 trade_records 里聚合）──────────────────────────────────
    slippage_summary: dict = {}
    try:
        _entry_slips = [
            float(t.get("entry_slippage_pct") or 0)
            for t in trade_records if t.get("entry_slippage_pct") is not None
        ]
        _sl_slips = [
            abs(float(t.get("sl_slippage_pct") or 0))
            for t in trade_records if t.get("sl_slippage_pct") is not None
        ]
        _tp_slips = [
            abs(float(t.get("tp_slippage_pct") or 0))
            for t in trade_records if t.get("tp_slippage_pct") is not None
        ]
        _total_slip_u = sum(float(t.get("entry_slippage_u") or 0) for t in trade_records)
        slippage_summary = {
            "avg_entry_slippage_pct": round(sum(_entry_slips) / len(_entry_slips), 4) if _entry_slips else None,
            "avg_sl_slippage_pct":    round(sum(_sl_slips) / len(_sl_slips), 4) if _sl_slips else None,
            "max_sl_slippage_pct":    round(max(_sl_slips), 4) if _sl_slips else None,
            "avg_tp_slippage_pct":    round(sum(_tp_slips) / len(_tp_slips), 4) if _tp_slips else None,
            "total_slippage_cost_u":  round(_total_slip_u, 4),
        }
    except Exception as _se:
        logger.warning(f"[archiver] slippage_summary 计算失败（非致命）: {_se}")

    # ── 5. 组装输出 ───────────────────────────────────────────────────────────
    archive_obj = {
        "date":         date_str,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "summary": {
            "total_trades":  total,
            "win":           len(wins),
            "loss":          len(losses),
            "win_rate":      round(win_rate, 4),
            "total_pnl_usdt": round(total_pnl, 2),
            "by_mode":        by_mode,
            "by_signal_strength": by_strength,
        },
        "funding_summary":         funding_summary,
        "slippage_summary":        slippage_summary,
        "trades":                  trade_records,
        "open_positions_snapshot": open_snapshot,
    }

    # ── 6. 写文件 ─────────────────────────────────────────────────────────────
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    out_path = ARCHIVE_DIR / f"{date_str}.json"
    try:
        out_path.write_text(
            json.dumps(archive_obj, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info(f"[archiver] 写入 {out_path}")
    except Exception as e:
        logger.error(f"[archiver] 写文件失败: {e}")
        return False

    # ── 7. git commit ─────────────────────────────────────────────────────────
    rel_path = out_path.relative_to(_REPO_ROOT)
    commit_msg = (
        f"archive: {date_str} 交易记录 {total}笔 "
        f"PnL{'+' if total_pnl >= 0 else ''}{total_pnl:.1f}U"
    )
    try:
        subprocess.run(
            ["git", "add", str(rel_path)],
            cwd=str(_REPO_ROOT), check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", commit_msg],
            cwd=str(_REPO_ROOT), check=True, capture_output=True
        )
        logger.info(f"[archiver] git commit: {commit_msg}")
    except subprocess.CalledProcessError as e:
        logger.warning(f"[archiver] git commit 失败（已有 nothing to commit 或其他）: {e.stderr.decode()[:200]}")
        # commit 失败不影响整体流程

    # ── 8. git push（可选） ───────────────────────────────────────────────────
    if ARCHIVE_GIT_PUSH:
        try:
            result = subprocess.run(
                ["git", "push"],
                cwd=str(_REPO_ROOT), capture_output=True, timeout=30
            )
            if result.returncode == 0:
                logger.info("[archiver] git push 成功")
            else:
                logger.warning(f"[archiver] git push 失败（非致命）: {result.stderr.decode()[:200]}")
        except Exception as e:
            logger.warning(f"[archiver] git push 异常（非致命）: {e}")

    # ── 8b. 全市场资金费率情绪快照（归档记录）──────────────────────────────────
    market_sentiment_snap: dict = {}
    try:
        from shared.clawby_adapter import get_market_funding_sentiment as _gfs2
        market_sentiment_snap = _gfs2()
        if "error" in market_sentiment_snap:
            market_sentiment_snap = {}
    except Exception as _mse:
        logger.debug(f"[archiver] market_sentiment 快照失败（非致命）: {_mse}")

    # 追加到 archive_obj
    if market_sentiment_snap:
        try:
            import json as _j2
            if out_path.exists():
                _obj2 = _j2.loads(out_path.read_text())
                _obj2["market_sentiment"] = market_sentiment_snap
                out_path.write_text(_j2.dumps(_obj2, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    # ── 9. 触发 AI 参数顾问分析（异步，不阻塞日报主流程）─────────────────────
    try:
        from ai_advisor import analyze as _ai_analyze
        import threading
        t = threading.Thread(
            target=_ai_analyze,
            args=(date_str,),
            daemon=True,
            name="ai_advisor",
        )
        t.start()
        logger.info("[archiver] AI 参数顾问已在后台启动")
    except Exception as e:
        logger.warning(f"[archiver] AI 参数顾问启动失败（非致命）: {e}")

    # ── 9b. 组合 Beta/Delta 快照（写入已保存的 archive_obj 文件追加字段）─────────
    try:
        from tools.portfolio_risk import calc_portfolio_risk as _pf_risk
        from shared.notifier import notify as _notify_fn
        _pr = _pf_risk()
        if "error" not in _pr:
            # 读回 archive 文件，追加 portfolio_risk 字段后重写
            _ap = out_path
            if _ap.exists():
                import json as _j
                _obj = _j.loads(_ap.read_text())
                _obj["portfolio_risk"] = _pr
                _ap.write_text(_j.dumps(_obj, ensure_ascii=False, indent=2), encoding="utf-8")
            # 告警检查
            from config import PORTFOLIO_BETA_WARN_THRESH, PORTFOLIO_SINGLE_POS_MAX_PCT
            _beta = _pr.get("portfolio_beta", 0.0)
            _mx   = _pr.get("max_single_position_pct", 0.0)
            _drop = _pr.get("btc_10pct_drop_loss_pct", 0.0)
            if abs(_beta) >= PORTFOLIO_BETA_WARN_THRESH:
                _notify_fn(f"⚠️ 组合 Beta={_beta:.2f}，BTC 跌10%预估亏损 {abs(_drop):.1f}%")
            if _mx >= PORTFOLIO_SINGLE_POS_MAX_PCT * 100:
                _notify_fn(f"⚠️ 单仓集中度过高：最大单仓占比 {_mx:.1f}%")
            logger.info(f"[archiver] portfolio_risk 已写入 Beta={_beta:.2f}")
    except Exception as e:
        logger.warning(f"[archiver] portfolio_risk 写入失败（非致命）: {e}")

    # ── 10. 事后追踪 + 参数敏感性分析（Signal Analytics，v3.28 新增）──────────────────
    try:
        from tools.track_outcomes import run as _track_outcomes
        _to_stats = _track_outcomes()
        logger.info(f"[archiver] track_outcomes: {_to_stats}")
    except Exception as e:
        logger.warning(f"[archiver] track_outcomes 失败（非致命）: {e}")

    try:
        from tools.param_sensitivity import daily_report as _param_report
        _report_text = _param_report(days=14)
        if _report_text and "数据不足" not in _report_text and "暂不生成" not in _report_text:
            logger.info("[archiver] 参数敏感性报告已生成")
            # 将报告附加到日报文件（若存在）
            _rpt_file = ROOT / "logs" / f"param_sensitivity_{date_str}.txt"
            _rpt_file.write_text(_report_text, encoding="utf-8")
    except Exception as e:
        logger.warning(f"[archiver] param_sensitivity 失败（非致命）: {e}")

    return True
