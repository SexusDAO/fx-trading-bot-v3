"""
scanner_llm.py — 扫描主流程 v2.0

流程：
  1. 熔断检查
  2. 持仓槽位检查
  3. 获取候选标的（gainers15 + losers15 + volume30，去重 ~50个）
  4. 批量预拉 ticker（1次 API）
  5. 并发评分（ThreadPoolExecutor，8线程）
     ├─ scorer.score()  → 5维度评分
     ├─ 分叉：< 55丢弃 / 55~64写观察池 / ≥65进场景匹配
     └─ scene_matcher.match() → 场景A/B
  6. pre_order_gate + 写 pending_orders.json
  7. 触发 watchlist_monitor（观察池重新评分 + 升级检测）
  8. 推送播报
"""

import json
import sys
import time
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

WORKSPACE = Path(__file__).parent.parent
sys.path.insert(0, str(WORKSPACE))

from modules.scorer          import score as do_score
from modules.scene_matcher   import match as do_match
from modules.pre_order_gate  import pre_order_gate
from modules.position_size   import calc_position_size
from modules.pos_state_store import get_all as get_pos_state
from modules.trade_logger    import daily_summary
from modules.watchlist_store import add_or_update as wl_add, add_volume_warning, get_volume_warnings
from modules.watchlist_monitor import run_watchlist_monitor, run_watchlist_maintenance
from modules.dynamic_baseline import compute as compute_baseline
from modules.top10_selector   import select_top10, cutoff_info

logger = logging.getLogger("scanner_llm")

# ── DRY-RUN trace 全局开关 ─────────────────────────────────────────────────────
DRY_RUN: bool = False      # 由 run_scan(dry_run=True) 或 __main__ --dry-run 设置

def _trace(msg: str):
    """dry-run 模式下输出详细决策链路日志"""
    if DRY_RUN:
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        print(f"[TRACE {ts}] {msg}")
        logger.info(f"[DRY] {msg}")

# ── 路径 ──────────────────────────────────────────────────────────────────────
PENDING_FILE   = WORKSPACE / "config" / "pending_orders.json"
CIRCUIT_FILE   = WORKSPACE / "config" / "circuit_breaker.json"
REGIME_FILE    = WORKSPACE / "config" / "market_regime.json"
STRATEGY_FILE  = WORKSPACE / "config" / "strategy-config.json"
TRIGGER_FILE   = WORKSPACE / "config" / "scan_trigger.json"


# ── 工具 ──────────────────────────────────────────────────────────────────────

def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _load_json(path: Path):
    try:
        with open(path) as f: return json.load(f)
    except Exception: return {}

def _save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    tmp.replace(path)

def _is_circuit_open():
    return bool(_load_json(CIRCUIT_FILE).get("open", False))

def _consume_trigger() -> str | None:
    """
    检查并消耗 scan_trigger.json。
    返回 reason 字符串（如 'position_closed'/'tp1_triggered'/'time_sl'），
    不存在则返回 None。
    """
    try:
        if not TRIGGER_FILE.exists():
            return None
        data = _load_json(TRIGGER_FILE)
        if not data.get("trigger"):
            TRIGGER_FILE.unlink(missing_ok=True)
            return None
        reason = data.get("reason", "unknown")
        TRIGGER_FILE.unlink(missing_ok=True)  # 消耗 flag
        logger.info(f"[scanner] 消耗 scan_trigger: {reason}")
        return reason
    except Exception as e:
        logger.debug(f"[scanner] _consume_trigger 异常: {e}")
        return None

def _get_regime():
    return _load_json(REGIME_FILE).get("mode", "range")

def _get_btc_trend():
    return _load_json(REGIME_FILE).get("btc_trend", "neutral")

def _get_equity(api=None):
    try:
        from modules import binance_api as _api
        real = api or _api
        info = real.get_futures_account()
        return float(info.get("totalWalletBalance", 0))
    except Exception:
        return 0.0

def _get_consecutive_losses():
    return daily_summary().get("consecutive_losses", 0)


# ── 候选标的获取 ──────────────────────────────────────────────────────────────

def get_candidates(api) -> list[str]:
    """gainers15 + losers15 + volume30，去重，过滤黑名单"""
    seen = set()
    result = []
    try:
        tickers = api.get_all_tickers()
        usdt = [t for t in tickers
                if isinstance(t, dict) and t.get("symbol", "").endswith("USDT")]
        usdt.sort(key=lambda x: float(x.get("priceChangePercent", 0)), reverse=True)

        for t in usdt[:15] + usdt[-15:]:       # gainers + losers
            s = t["symbol"]
            if s not in seen: seen.add(s); result.append(s)

        usdt.sort(key=lambda x: float(x.get("quoteVolume", 0)), reverse=True)
        for t in usdt[:30]:                     # top volume
            s = t["symbol"]
            if s not in seen: seen.add(s); result.append(s)
    except Exception as e:
        logger.warning(f"[scanner] get_candidates 失败: {e}")
    return result


# ── 批量预拉 ticker ───────────────────────────────────────────────────────────

def _prefetch_tickers() -> dict[str, dict]:
    """一次 API 调用拉全量 USDT 合约 ticker + mark price"""
    result = {}
    try:
        from modules import binance_api as _api
        t24s  = _api.get_all_tickers()
        marks = _api.get_all_mark_prices()
        mark_map = {m["symbol"]: m for m in marks} if isinstance(marks, list) else {}
        for t in (t24s if isinstance(t24s, list) else []):
            sym = t.get("symbol", "")
            if not sym.endswith("USDT"): continue
            m = mark_map.get(sym, {})
            result[sym] = {
                "lastPrice":          str(m.get("markPrice", t.get("lastPrice", 0))),
                "lastFundingRate":    str(m.get("lastFundingRate", 0)),
                "priceChangePercent": str(t.get("priceChangePercent", 0)),
            }
    except Exception as e:
        logger.warning(f"[scanner] prefetch_tickers 失败: {e}")
    return result


# ── 单标的分析 ────────────────────────────────────────────────────────────────

def analyze_symbol(symbol: str, api, ticker_cache: dict | None) -> dict | None:
    """
    对单个标的运行评分 + 场景匹配。
    返回 None = 丢弃
    返回 dict  = 含 score / scene / watchlist_only 等字段
    """
    cfg        = _load_json(STRATEGY_FILE)
    threshold  = cfg.get("scoring", {}).get("trend_threshold", 65)
    watch_min  = cfg.get("scoring", {}).get("watchlist_threshold", 55)
    btc_trend  = _get_btc_trend()

    # 拉 ticker（优先用预拉取缓存）
    if ticker_cache and symbol in ticker_cache:
        t = ticker_cache[symbol]
        mark_price = float(t.get("lastPrice", 0))
        funding    = float(t.get("lastFundingRate", 0))
        chg24      = float(t.get("priceChangePercent", 0)) / 100
    else:
        try:
            t = (api or __import__("modules.binance_api", fromlist=["get_ticker"])
                 ).get_ticker(symbol)
            mark_price = float(t.get("lastPrice", 0))
            funding    = float(t.get("lastFundingRate", 0))
            chg24      = float(t.get("priceChangePercent", 0)) / 100
        except Exception:
            return None

    if mark_price <= 0:
        return None

    # 拉 klines
    try:
        from modules import binance_api as _api_mod
        _klines_api = api if api is not None else _api_mod
        k1h = _klines_api.get_klines(symbol, "1h", limit=100)
        k4h = _klines_api.get_klines(symbol, "4h", limit=60)
    except Exception as e:
        logger.debug(f"[scanner] {symbol} klines失败: {e}")
        return None

    if len(k1h) < 20:
        return None

    # 计算动态基准
    bl = compute_baseline(k1h)

    # ── 成交量预警检测（新增）──────────────────────────────────────────────
    # 触发条件：① 近 3 根成交量均值 > vol_p75 ② ATR < atr_p60 ③ OI 增长 > oi_std×1.0
    try:
        vols = [float(k[5]) for k in k1h[-3:]]
        vol_avg_3 = sum(vols) / len(vols) if vols else 0
        atr_now = bl.atr_baseline  # 简化：用 baseline ATR
        
        vol_warning = (
            vol_avg_3 > bl.vol_p75 and  # 成交量 > 75 百分位
            atr_now < bl.atr_p60        # ATR < 60 百分位（价格未启动）
            # OI 数据暂缺，先只用成交量 +ATR 两个条件
        )
        
        if vol_warning:
            # 写入预警池（direction/sr 在 scorer 之后才有，此处用占位值）
            add_volume_warning(symbol, "UNKNOWN", mark_price, "volume_warning", ttl_min=360)
            _trace(f"[VOLUME_WARNING] {symbol} → 成交量异常 (vol_avg={vol_avg_3:.0f} > p75={bl.vol_p75:.0f})")
    except Exception as e:
        logger.debug(f"[scanner] {symbol} 成交量预警检测失败：{e}")

    # 评分（v3 动态版 scorer）
    try:
        sr = do_score(k1h, k4h, mark_price, funding, chg24, btc_trend,
                      baseline=bl)
    except Exception as e:
        logger.debug(f"[scanner] {symbol} scorer失败: {e}")
        return None

    total     = sr["total"]
    direction = sr["direction"]
    track     = sr.get("track", "oscillation")

    # trace 评分
    _trace(f"[SCORER] {symbol} → 赛道:{track} 方向:{direction} 总分:{total}分 "
           f"(趋势:{sr['trend_clarity']['score']}/量能:{sr['volume_health']['score']}/"
           f"波动:{sr['volatility_fit']['score']}/结构:{sr['structure']['score']}/"
           f"情绪:{sr['mkt_sync']['score']})")

    # 方向不明确 → 丢弃（分数由 top10_selector 竞争决定，不在此过滤）
    if direction == "NONE":
        _trace(f"[DISCARD] {symbol} → 方向不明，丢弃")
        return None

    # 返回完整分析结果（交给 top10_selector 竞争排序）
    # scene_matcher 在 top10 选出后才调用，此处只记录 baseline + track
    return {
        "symbol":       symbol,
        "direction":    direction,
        "signal_type":  sr["signal_type"],
        "score":        total,
        "score_result": sr,
        "track":        track,
        "baseline":     bl,
        "mark_price":   mark_price,
        "funding_rate": funding,
        "chg24":        chg24,
        "k1h":          k1h,
        "watchlist_only": False,   # 由 top10_selector 决定是否入场
    }

    # ── 以下代码已移至 run_scan 的 top10 处理段 ─────────────────────────────
    # (保留注释供对照)
    return {
        "symbol":       symbol,
        "direction":    direction,
        "signal_type":  sr["signal_type"],
        "score":        total,
        "score_result": sr,
        "mark_price":   mark_price,
        "funding_rate": funding,
        "chg24":        chg24,
        "k1h":          k1h,
        "scene_result": sm,
        "watchlist_only": not sm["matched"],  # 场景不满足 → 也进观察池
    }


# ── 主扫描 ────────────────────────────────────────────────────────────────────

def run_scan(api=None, notify_fn=None, dry_run: bool = False) -> dict:
    """dry_run=True: 不写 pending_orders.json，结果输出到 test_output.json 且全程 trace"""
    global DRY_RUN
    if dry_run:
        DRY_RUN = True
    t_start = time.time()
    trigger_reason: str | None = None
    result  = {
        "scanned": 0, "signals": 0, "placed": 0,
        "skipped_circuit": False, "skipped_full": False,
        "watchlist_added": 0, "watchlist_upgraded": 0,
        "orders": [], "watchlist_preview": [],
        "dry_run": DRY_RUN,
    }

    # 1. 熔断
    if _is_circuit_open():
        result["skipped_circuit"] = True
        return result

    # 2. 持仓槽位
    pos_state = get_pos_state()
    regime    = _get_regime()
    cfg       = _load_json(STRATEGY_FILE)
    limits    = cfg.get("position", {}).get("market_mode_limits", {})
    mode_l    = limits.get(regime, {"max_long": 2, "max_short": 2, "max_total": 4})
    max_total = mode_l.get("max_total", 4)

    long_cnt  = sum(1 for k in pos_state if k.endswith("_LONG"))
    short_cnt = sum(1 for k in pos_state if k.endswith("_SHORT"))
    total_cnt = long_cnt + short_cnt

    if total_cnt >= max_total:
        # 满仓：清掉 trigger flag（避免循环触发），做轻量维护后退出
        _consume_trigger()
        logger.info("[scanner] 满仓，跳过完整扫描，执行轻量维护")
        run_watchlist_maintenance()
        result["skipped_full"] = True
        return result

    # 2b. 检查 scan_trigger.json（ws_monitor 写入的平仓事件 flag）
    trigger_reason = _consume_trigger()
    if trigger_reason:
        logger.info(f"[scanner] 触发扫描 ← {trigger_reason}")
        _trace(f"[TRIGGER] 由事件触发: {trigger_reason}")

    # 3. 候选标的
    from modules import binance_api as _api_module
    real_api   = api or _api_module
    candidates = get_candidates(real_api)
    if not candidates:
        return result
    result["scanned"] = len(candidates)

    # 4. 批量预拉 ticker
    ticker_cache: dict = {}
    use_mock = (api is not None)
    if not use_mock:
        ticker_cache = _prefetch_tickers()

    # 5. 并发分析（8线程）
    def _analyze_one(sym):
        return analyze_symbol(sym, api, ticker_cache if not use_mock else None)

    all_results = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = {pool.submit(_analyze_one, sym): sym for sym in candidates}
        for fut in as_completed(futs):
            try:
                r = fut.result()
                if r: all_results.append(r)
            except Exception as e:
                logger.debug(f"[scanner] analyze error: {e}")

    result["signals"] = len(all_results)

    # 6. Top10 竞争池 → 场景匹配（新流程）
    btc_trend = _get_btc_trend()
    equity    = _get_equity(api)
    consec    = _get_consecutive_losses()
    wl_ttl    = cfg.get("ttl_minutes", {}).get("limit_low_score", 120)

    # cutoff 信息（日志用）
    ci = cutoff_info(all_results)
    _trace(f"[SELECTOR] 总{ci['total']}个有效得分 分布: "
           f"min={ci['min']} p30={ci['p30']} p50={ci['p50']} max={ci['max']}")

    # 获取预警池标的（跳过 Top10 竞争，直接评分）
    volume_warnings = get_volume_warnings()
    warning_symbols = {w["symbol"] for w in volume_warnings}
    _trace(f"[VOLUME_WARNING] 预警池：{warning_symbols}")
    
    # Top10 竞争池（多头 Top5+ 空头 Top5，绝对下限 40 分）
    # 预警池标的跳过竞争，直接进入后续流程
    top10     = select_top10(all_results)
    non_top10 = [r for r in all_results if r not in top10 and r["symbol"] not in warning_symbols]
    # 获取预警池标的（跳过 Top10 竞争，直接评分）
    volume_warnings = get_volume_warnings()
    warning_symbols = {w["symbol"] for w in volume_warnings}
    _trace(f"[VOLUME_WARNING] 预警池：{warning_symbols}")
    
    # Top10 竞争池（多头 Top5+ 空头 Top5，绝对下限 40 分）
    # 预警池标的跳过竞争，直接进入后续流程
    top10     = select_top10(all_results)
    non_top10 = [r for r in all_results if r not in top10 and r["symbol"] not in warning_symbols]
    # 获取预警池标的（跳过 Top10 竞争，直接评分）
    volume_warnings = get_volume_warnings()
    warning_symbols = {w["symbol"] for w in volume_warnings}
    _trace(f"[VOLUME_WARNING] 预警池：{warning_symbols}")
    
    # Top10 竞争池（多头 Top5+ 空头 Top5，绝对下限 40 分）
    # 预警池标的跳过竞争，直接进入后续流程
    top10     = select_top10(all_results)
    non_top10 = [r for r in all_results if r not in top10 and r["symbol"] not in warning_symbols]
    _trace(f"[SELECTOR] Top10: {[r['symbol'] for r in top10]}")

    # 非Top10 全部进观察池
    for r in non_top10:
        wl_add(r["symbol"], r["direction"], r["score"],
               r["mark_price"], r["signal_type"], ttl_min=wl_ttl)

    # Top10 逐个：位置判断 + 场景匹配 + 二次打分 ≥70 才进挂单
    watch_items  = []
    active_items = []
    for item in top10:
        sym   = item["symbol"]
        bl    = item.get("baseline")
        track = item.get("track", "oscillation")
        try:
            sm = do_match(item["k1h"], item["mark_price"],
                          item["direction"], track=track, bl=bl,
                          symbol=sym)
        except Exception as e:
            logger.debug(f"[scanner] {sym} scene_matcher失败: {e}")
            watch_items.append(item)
            continue

        _trace(f"[SCENE] {sym} 赛道:{track} → 场景:{sm.get('scene','none')} "
               f"二次分:{sm.get('second_score',0)} RR={sm.get('rr',0)}")

        if sm["matched"] and sm.get("second_score", 0) >= 70:
            item["scene_result"] = sm
            active_items.append(item)
            _trace(f"[ORDER] {sym} 二次分{sm['second_score']}≥70 → 准备挂单")
        else:
            watch_items.append(item)
            reason = (f"二次分{sm.get('second_score',0)}<70"
                      if sm["matched"] else sm["reason"][:50])
            _trace(f"[WATCH] {sym} → {reason}，进观察池")

    # 写观察池（非Top10 + Top10中场景不满足）
    for w in watch_items:
        wl_add(w["symbol"], w["direction"], w["score"],
               w["mark_price"], w["signal_type"], ttl_min=wl_ttl)
    result["watchlist_added"] = len(watch_items) + len(non_top10)

    # 观察池升级检查
    wl_r = run_watchlist_monitor(api=api, btc_trend=btc_trend, notify_fn=notify_fn)
    result["watchlist_upgraded"] = wl_r.get("upgraded", 0)

    # 槽位分配（long/short 分别限制）
    available   = max_total - total_cnt
    max_long_av = mode_l.get("max_long", 2) - long_cnt
    max_short_av= mode_l.get("max_short", 2) - short_cnt
    active_items.sort(key=lambda r: r["score"], reverse=True)
    top_items = []
    cnt_l = cnt_s = 0
    for it in active_items:
        if len(top_items) >= available: break
        if it["direction"] == "LONG":
            if cnt_l < max_long_av: top_items.append(it); cnt_l += 1
        else:
            if cnt_s < max_short_av: top_items.append(it); cnt_s += 1

    # 7. Gate + 写 pending_orders
    pending = _load_json(PENDING_FILE) if PENDING_FILE.exists() else {"orders": []}
    if not isinstance(pending, dict): pending = {"orders": []}
    existing_syms = {o["symbol"] for o in pending.get("orders", [])}

    for item in top_items:
        sym       = item["symbol"]
        direction = item["direction"]
        score     = item["score"]
        sm        = item.get("scene_result", {})
        sr        = item.get("score_result", {})

        if sym in existing_syms or f"{sym}_{direction}" in pos_state:
            continue

        pos_sz = calc_position_size(
            item["signal_type"], score,
            btc_trend=btc_trend,
            consecutive_losses=consec,
        )
        gate = pre_order_gate(sym, direction, score,
                              item["funding_rate"], pos_sz, equity=equity)
        if not gate["ok"]:
            # gate 拒绝 → 降入观察池
            wl_add(sym, direction, score, item["mark_price"],
                   item["signal_type"], ttl_min=wl_ttl)
            logger.info(f"[scanner] {sym} gate拒绝→观察池: {gate['reason']}")
            continue

        entry  = sm.get("entry_price", item["mark_price"])
        hard_sl = sm.get("sl_price", 0)
        tp1    = sm.get("tp_price", 0)
        risk   = abs(entry - hard_sl)
        tp2    = round(entry + risk * 3.0 if direction == "LONG"
                       else entry - risk * 3.0, 8)

        ttl_min_val = cfg.get("ttl_minutes", {}).get(
            "limit_low_score" if score < 80 else "limit_high_score", 120)
        expire = (datetime.now(timezone.utc) + timedelta(minutes=ttl_min_val)
                  ).strftime("%Y-%m-%dT%H:%M:%SZ")

        order = {
            "id":           f"scan_{_now_iso().replace(':', '').replace('-', '')}_{sym}",
            "symbol":       sym,
            "direction":    direction,
            "entry_price":  entry,
            "entry_method": sm.get("scene", "LIMIT"),
            "notional":     pos_sz,
            "signal_type":  item["signal_type"],
            "score":        score,
            "score_details": {
                "trend_clarity":  sr.get("trend_clarity", {}).get("score", 0),
                "volume_health":  sr.get("volume_health", {}).get("score", 0),
                "volatility_fit": sr.get("volatility_fit", {}).get("score", 0),
                "structure":      sr.get("structure", {}).get("score", 0),
                "mkt_sync":       sr.get("mkt_sync", {}).get("score", 0),
            },
            "hard_sl":      hard_sl,
            "tp1_price":    tp1,
            "tp2_price":    tp2,
            "funding_rate": item["funding_rate"],
            "scene":        sm.get("scene", ""),
            "scene_reason": sm.get("reason", ""),
            "status":       "pending",
            "placed_at":    _now_iso(),
            "expire_at":    expire,
            "exchange_order_id": None,
        }
        pending.setdefault("orders", []).append(order)
        result["orders"].append(order)
        result["placed"] += 1
        existing_syms.add(sym)
        logger.info(f"[scanner] ✅ {sym} {direction} {score}分 "
                    f"scene={sm.get('scene')} entry={entry:.6f} RR={sm.get('rr', 0)}")

    pending["generated_at"] = _now_iso()
    if DRY_RUN:
        # dry-run: 写 test_output.json 而非 pending_orders.json
        out_file = WORKSPACE / "test_output.json"
        dry_out = {
            "dry_run": True,
            "generated_at": _now_iso(),
            "elapsed": round(time.time() - t_start, 1),
            "scanned": result["scanned"],
            "signals": result["signals"],
            "placed":  result["placed"],
            "watchlist_added": result["watchlist_added"],
            "all_analyzed": [
                {
                    "symbol":      r["symbol"],
                    "direction":   r["direction"],
                    "score":       r["score"],
                    "signal_type": r["signal_type"],
                    "outcome":     "order" if not r.get("watchlist_only") else "watchlist",
                    "scene":       r.get("scene_result", {}).get("scene", "n/a"),
                    "rr":          r.get("scene_result", {}).get("rr", 0),
                    "score_detail": {
                        "trend":     r["score_result"]["trend_clarity"]["score"],
                        "volume":    r["score_result"]["volume_health"]["score"],
                        "volatility":r["score_result"]["volatility_fit"]["score"],
                        "structure": r["score_result"]["structure"]["score"],
                        "mkt_sync":  r["score_result"]["mkt_sync"]["score"],
                    },
                }
                for r in all_results
            ],
            "orders": pending.get("orders", []),
        }
        _save_json(out_file, dry_out)
        _trace(f"[DRY-RUN] 结果已写入 test_output.json (共{len(pending.get('orders',[]))} 笔信号)")
    else:
        _save_json(PENDING_FILE, pending)

    result["trigger_reason"] = trigger_reason
    result["elapsed"] = round(time.time() - t_start, 1)
    result["watchlist_preview"] = [
        {"symbol": w["symbol"], "score": w["score"], "signal_type": w["signal_type"]}
        for w in watch_items[:3]
    ]

    if notify_fn:
        _send_report(result, pos_state, notify_fn)

    return result


def _send_report(result: dict, pos_state: dict, notify_fn):
    lines = [f"📊 扫描完成 | {result['elapsed']}s | "
             f"扫 {result['scanned']} 个标的\n"]

    if pos_state:
        lines.append("**当前持仓**")
        for key, p in pos_state.items():
            lines.append(f"  {p.get('direction','')} {p.get('symbol','')} "
                         f"均价 {p.get('avg_price', p.get('entry_price', '?'))}")
    else:
        lines.append("持仓：空仓")

    if result["orders"]:
        lines.append(f"\n**新挂单 {result['placed']} 笔**")
        for o in result["orders"]:
            lines.append(f"  {o['direction']} {o['symbol']}  "
                         f"{o['score']}分  {o['scene']}  "
                         f"限价 {o['entry_price']}  RR待定")
    else:
        lines.append("\n无新挂单")

    added   = result.get("watchlist_added", 0)
    upgraded = result.get("watchlist_upgraded", 0)
    if added or upgraded:
        lines.append(f"\n👀 观察池: +{added} 新增  ↑{upgraded} 升级")

    if result["watchlist_preview"]:
        lines.append("\n**观察池 Top3**")
        for w in result["watchlist_preview"]:
            lines.append(f"  {w['symbol']} {w['score']}分 {w['signal_type']}")

    notify_fn("\n".join(lines))


# ── 主入口 ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse, logging as _log
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="不写 pending_orders，输出 test_output.json 且全程 trace")
    args = parser.parse_args()

    (WORKSPACE / "logs").mkdir(parents=True, exist_ok=True)
    _log.basicConfig(
        level=_log.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            _log.FileHandler(str(WORKSPACE / "logs" / "scanner.log")),
            _log.StreamHandler(),
        ]
    )

    def _notify(msg: str):
        logger.info(f"[notify] {msg}")
        print(msg)

    r = run_scan(notify_fn=_notify, dry_run=args.dry_run)
    if r["skipped_circuit"]:
        print("HEARTBEAT_OK  # 熔断中")
    elif r["skipped_full"]:
        print("HEARTBEAT_OK  # 满仓，跳过完整扫描")
    elif r["placed"] == 0 and r["watchlist_upgraded"] == 0:
        if args.dry_run:
            scanned = r["scanned"]
            wl = r["watchlist_added"]
            print(f"🔍 dry-run 完成: 扫描 {scanned} 个标的 → 观察池 {wl} 个")
            if (WORKSPACE / "test_output.json").exists():
                print("✅ test_output.json 已生成")
        else:
            print("HEARTBEAT_OK  # 无信号")
