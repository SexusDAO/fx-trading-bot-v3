"""
modules/watchlist_monitor.py — 观察池升级监控 v2.0

每次 scanner cron 触发时附带运行。
职责：
  1. 清理过期 / 评分降级条目（purge）
  2. 对 watching 条目重新走 scorer + scene_matcher
  3. 升级过滤器：评分 ≥65（含趋势加成）+ 追高≤3% + 场景匹配
  4. 通过 → 写入 pending_orders.json

追高保护：当前价 > 进池时价格 × 1.03 → 即使评分满足也不升级
"""

import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

WORKSPACE     = Path(__file__).parent.parent
PENDING_FILE  = WORKSPACE / "config" / "pending_orders.json"
STRATEGY_FILE = WORKSPACE / "config" / "strategy-config.json"

logger = logging.getLogger("watchlist_monitor")

UPGRADE_SCORE_THRESHOLD = 65
DROP_SCORE_THRESHOLD    = 45
MAX_CHASE_PCT           = 0.03   # 追高超3%就不升级


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


def run_watchlist_monitor(api=None,
                          btc_trend: str = "neutral",
                          notify_fn=None) -> dict:
    """
    对观察池里所有 watching 条目：
      1. 重新用 scorer 评分
      2. 升级检查（评分+追高+scene_matcher）
      3. 通过 → 写 pending_orders.json

    Returns: {checked, upgraded, dropped}
    """
    from modules.watchlist_store import (get_watching, add_or_update,
                                         mark_upgraded, mark_dropped,
                                         purge_expired, effective_score)
    from modules.scorer       import score as do_score
    from modules.scene_matcher import match as do_match
    from modules.pre_order_gate import pre_order_gate
    from modules.position_size  import calc_position_size
    from modules.pos_state_store import get_all as get_pos_state
    from modules import binance_api as _api_module

    real_api = api or _api_module
    result   = {"checked": 0, "upgraded": 0, "dropped": 0}

    purge_expired(DROP_SCORE_THRESHOLD)

    watching = get_watching()
    if not watching:
        return result

    cfg     = _load_json(STRATEGY_FILE)
    ttl_min = cfg.get("ttl_minutes", {}).get("limit_low_score", 120)

    # 批量预拉 ticker（一次 API 调用）
    ticker_cache: dict = {}
    use_mock = (api is not None)
    if not use_mock:
        try:
            t24s  = real_api.get_all_tickers()
            marks = real_api.get_all_mark_prices()
            mark_map = {m["symbol"]: m for m in marks} if isinstance(marks, list) else {}
            for t in (t24s if isinstance(t24s, list) else []):
                sym = t.get("symbol", "")
                if not sym.endswith("USDT"): continue
                m = mark_map.get(sym, {})
                ticker_cache[sym] = {
                    "lastPrice":          str(m.get("markPrice", t.get("lastPrice", 0))),
                    "lastFundingRate":    str(m.get("lastFundingRate", 0)),
                    "priceChangePercent": str(t.get("priceChangePercent", 0)),
                }
        except Exception as e:
            logger.warning(f"[wl] 批量预担ticker失败: {e}")

    try:
        acct   = real_api.get_futures_account()
        equity = float(acct.get("totalWalletBalance", 0))
    except Exception:
        equity = 0.0

    pending_data  = _load_json(PENDING_FILE) if PENDING_FILE.exists() else {"orders": []}
    if not isinstance(pending_data, dict):
        pending_data = {"orders": []}
    existing_syms = {o["symbol"] for o in pending_data.get("orders", [])}
    upgraded_list = []

    # 槽位预检：如果总仓位已满，快速返回（不标记 dropped）
    cfg_pos   = _load_json(STRATEGY_FILE)
    limits    = cfg_pos.get("position", {}).get("market_mode_limits", {})
    pos_state = get_pos_state()
    _regime   = _load_json(WORKSPACE / "config" / "market_regime.json").get("mode", "range")
    _mode_l   = limits.get(_regime, {"max_long": 2, "max_short": 2, "max_total": 4})
    _max_tot  = _mode_l.get("max_total", 4)
    if len(pos_state) >= _max_tot:
        logger.info(f"[wl] 槽位已满({len(pos_state)}/{_max_tot})，跳过观察池升级")
        return result

    for item in watching:
        symbol    = item["symbol"]
        direction = item.get("direction", "LONG")
        result["checked"] += 1

        if symbol in existing_syms or f"{symbol}_{direction}" in pos_state:
            # 已有挂单或持仓 → 跨过，不标记 dropped（仓位关闭后自然过期）
            logger.debug(f"[wl] {symbol} 已有挂单或持仓，跳过")
            result["checked"] -= 1  # 不计入 checked
            continue

        # 拉 klines
        try:
            k1h = real_api.get_klines(symbol, "1h", limit=100)
            k4h = real_api.get_klines(symbol, "4h", limit=60)
        except Exception as e:
            logger.warning(f"[wl] {symbol} klines失败: {e}")
            continue

        if len(k1h) < 20:
            continue

        # 当前价（优先用批量缓存）
        if ticker_cache and symbol in ticker_cache:
            tc = ticker_cache[symbol]
            mark_price = float(tc.get("lastPrice", 0))
            funding    = float(tc.get("lastFundingRate", 0))
            chg24      = float(tc.get("priceChangePercent", 0)) / 100
        else:
            try:
                ticker     = real_api.get_ticker(symbol)
                mark_price = float(ticker.get("lastPrice", 0))
                funding    = float(ticker.get("lastFundingRate", 0))
                chg24      = float(ticker.get("priceChangePercent", 0)) / 100
            except Exception:
                mark_price = item.get("latest_price", 0)
                funding = chg24 = 0.0

        if mark_price <= 0:
            continue

        # 重新评分（新 scorer）
        try:
            sr = do_score(k1h, k4h, mark_price, funding, chg24, btc_trend)
        except Exception as e:
            logger.warning(f"[wl] {symbol} scorer失败: {e}")
            continue

        new_score = sr["total"]
        # 更新观察池记录（续期 + 追加评分历史）
        item = add_or_update(symbol, direction, new_score, mark_price,
                             sr["signal_type"], ttl_min=ttl_min)
        eff  = effective_score(item)

        logger.info(f"[wl] {symbol} 重评 raw={new_score} eff={eff} "
                    f"history={item.get('score_history', [])}")

        # 评分恶化 → 移除
        if eff < DROP_SCORE_THRESHOLD:
            mark_dropped(symbol, direction)
            result["dropped"] += 1
            logger.info(f"[wl] {symbol} eff={eff} < {DROP_SCORE_THRESHOLD}，移除")
            continue

        # 有效评分不足 → 继续等
        if eff < UPGRADE_SCORE_THRESHOLD:
            logger.debug(f"[wl] {symbol} eff={eff} 未达{UPGRADE_SCORE_THRESHOLD}，继续观察")
            continue

        # 追高检查（超3%不升级）
        snap_price = item.get("entry_price_snapshot", mark_price)
        if snap_price > 0:
            chase_pct = (mark_price - snap_price) / snap_price
            if direction == "LONG" and chase_pct > MAX_CHASE_PCT:
                logger.info(f"[wl] {symbol} 追高{chase_pct:.1%}>{MAX_CHASE_PCT:.0%}，不升级")
                continue
            if direction == "SHORT" and chase_pct < -MAX_CHASE_PCT:
                logger.info(f"[wl] {symbol} 追跌{abs(chase_pct):.1%}>{MAX_CHASE_PCT:.0%}，不升级")
                continue

        # 场景匹配
        sm = do_match(k1h, mark_price, direction)
        if not sm["matched"]:
            logger.info(f"[wl] {symbol} 场景不满足: {sm['reason']}")
            continue

        # pre_order_gate
        pos_sz = calc_position_size(sr["signal_type"], eff,
                                    btc_trend=btc_trend, consecutive_losses=0)
        gate   = pre_order_gate(symbol, direction, eff, funding, pos_sz, equity=equity)
        if not gate["ok"]:
            logger.info(f"[wl] {symbol} gate拒绝: {gate['reason']}")
            continue

        # 构建 pending order（接口和 scanner 完全一致）
        expire = (datetime.now(timezone.utc) + timedelta(minutes=ttl_min)
                  ).strftime("%Y-%m-%dT%H:%M:%SZ")

        # tp2 = tp1 等幅再延伸（RR 更高档）
        tp1   = sm["tp_price"]
        risk  = abs(sm["entry_price"] - sm["sl_price"])
        tp2   = round(
            sm["entry_price"] + risk * 3.0 if direction == "LONG"
            else sm["entry_price"] - risk * 3.0, 8
        )

        order = {
            "id":            f"wl_{_now_iso().replace(':', '').replace('-', '')}_{symbol}",
            "symbol":        symbol,
            "direction":     direction,
            "entry_price":   sm["entry_price"],
            "entry_method":  sm["scene"],
            "notional":      pos_sz,
            "signal_type":   sr["signal_type"],
            "score":         eff,
            "score_raw":     new_score,
            "score_details": {
                "trend_clarity":  sr["trend_clarity"]["score"],
                "volume_health":  sr["volume_health"]["score"],
                "volatility_fit": sr["volatility_fit"]["score"],
                "structure":      sr["structure"]["score"],
                "mkt_sync":       sr["mkt_sync"]["score"],
            },
            "hard_sl":       sm["sl_price"],
            "tp1_price":     tp1,
            "tp2_price":     tp2,
            "funding_rate":  funding,
            "source":        "watchlist",
            "scene":         sm["scene"],
            "scene_reason":  sm["reason"],
            "status":        "pending",
            "placed_at":     _now_iso(),
            "expire_at":     expire,
            "exchange_order_id": None,
        }

        pending_data.setdefault("orders", []).append(order)
        upgraded_list.append(order)
        mark_upgraded(symbol, direction)
        result["upgraded"] += 1
        existing_syms.add(symbol)
        logger.info(f"[wl] ✅ {symbol} 升级 eff={eff} scene={sm['scene']} "
                    f"entry={sm['entry_price']:.6f} rr={sm['rr']}")

        if notify_fn:
            snap = item.get("entry_score_snapshot", 0)
            notify_fn(
                f"👀 观察池升级开仓\n"
                f"{direction} {symbol}  {snap}分→{eff}分\n"
                f"场景: {sm['scene']}  RR={sm['rr']}\n"
                f"限价 {sm['entry_price']:.6f}  止损 {sm['sl_price']:.6f}  "
                f"止盈 {tp1:.6f}"
            )

    if upgraded_list:
        pending_data["generated_at"] = _now_iso()
        _save_json(PENDING_FILE, pending_data)

    return result


# ── 轻量维护函数（满仓时调用，不拉数据不评分）──────────────────────────────────

def run_watchlist_maintenance() -> dict:
    """
    轻量观察池维护，仅做纯内存/文件操作，不调用任何外部 API。
    用于满仓时保持观察池整洁。

    操作：
      ① 清理 TTL 过期的观察池标的（purge_expired）
      ② 清理追高保护触发的标的（当前价 > 入池价 × 1.03 且仍在 watching 状态）

    Returns: {"purged_ttl": N, "purged_chase": N}
    """
    from modules.watchlist_store import (
        purge_expired, get_watching, mark_dropped,
    )
    from modules.watchlist_monitor import DROP_SCORE_THRESHOLD as _DST
    _drop_threshold = _DST

    result = {"purged_ttl": 0, "purged_chase": 0}

    # ① TTL 过期清理
    before_purge = len(get_watching())
    purge_expired(_drop_threshold)
    after_purge = len(get_watching())
    result["purged_ttl"] = before_purge - after_purge

    # ② 追高保护清理（纯读本地文件里的 entry_price，不拉实时价格）
    #    这里只清理已记录了 last_price 快照的条目（scanner 写入时缓存）
    watching = get_watching()
    chased = 0
    for item in watching:
        entry_price = float(item.get("entry_price", 0) or item.get("mark_price", 0) or 0)
        last_price  = float(item.get("last_price", 0) or entry_price)
        direction   = item.get("direction", "LONG")
        if entry_price <= 0 or last_price <= 0:
            continue
        # 多头追高：当前价比入池价高超过 3%
        if direction == "LONG" and last_price > entry_price * (1 + MAX_CHASE_PCT):
            mark_dropped(item["symbol"], item["direction"])
            chased += 1
            logger.info(f"[wl_maint] {item['symbol']} 追高保护触发 "
                        f"(last={last_price:.6f} > entry={entry_price:.6f}×{1+MAX_CHASE_PCT}), 清理")
        # 空头追空：当前价比入池价低超过 3%
        elif direction == "SHORT" and last_price < entry_price * (1 - MAX_CHASE_PCT):
            mark_dropped(item["symbol"], item["direction"])
            chased += 1
            logger.info(f"[wl_maint] {item['symbol']} 追空保护触发 "
                        f"(last={last_price:.6f} < entry={entry_price:.6f}×{1-MAX_CHASE_PCT}), 清理")

    result["purged_chase"] = chased

    if result["purged_ttl"] or result["purged_chase"]:
        logger.info(f"[wl_maint] 维护完成: TTL清理 {result['purged_ttl']} 追高清理 {result['purged_chase']}")
    else:
        logger.debug("[wl_maint] 无需清理")

    return result
