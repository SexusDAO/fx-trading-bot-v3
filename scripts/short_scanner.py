"""
short_scanner.py — 独立做空扫描脚本
每30分钟由独立 cron 触发，不影响主仓位管理脚本
"""
import sys, os, requests, json, math, time, subprocess, datetime, signal
import coinglass_api as cg

# 加载策略配置
_cfg_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'strategy-config.json')
with open(_cfg_path) as _f:
    cfg = json.load(_f)
sys.path.insert(0, "/root/.openclaw/workspace")
import binance_api as api
from manual_positions import is_manual
import sys as _sys; _sys.path.insert(0, "/root/.openclaw/workspace/scripts")
from pre_open_guard import check_before_open as _check_before_open
from liquidation_guard import safe_notional as _safe_notional_ss, _get_account_state as _lg_refresh_state
# ── 门控模块（复用旧版完整逻辑）──────────────────────────────────────────────
try:
    from pre_order_gate import check as _pre_gate          # 7项开仓前检查
    from market_filter import apply as _market_filter      # 环境修正评分
    from calc_position_size import calc as _calc_pos_size  # 统一仓位计算
    _GATE_AVAILABLE = True
except ImportError:
    _GATE_AVAILABLE = False

# 运行开始时主动刷新账户状态缓存，确保守卫数据准确
try:
    _lg_state = _lg_refresh_state()
    if _lg_state.get('_fallback'):
        reports = reports if 'reports' in dir() else []
except Exception:
    pass
from entry_logic import (
    calc_score_short, classify_short_entry, liquidation_map,
    check_oi_divergence, check_funding_settlement_window,
    get_token_report_score, rsi_calc, atr_calc, ema_calc,
    fmt_price, fmt_qty, calc_score_trend
)

PROXY = {"http": api.PROXY_URL, "https": api.PROXY_URL}

BASE_F     = "https://fapi.binance.com"
POS_STATE  = "/root/.openclaw/workspace/pos_state.json"

def _load_pos_state():
    import pathlib as _pl
    f = _pl.Path(POS_STATE)
    return json.loads(f.read_text()) if f.exists() else {}

def _save_pos_state(ps: dict):
    import pathlib as _pl
    f = _pl.Path(POS_STATE)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(ps, indent=2, ensure_ascii=False))
    tmp.replace(f)

SHORT_NOTIONAL    = int(cfg.get('notional_limits', {}).get('hard_cap_per_position', 300))
MAX_SHORT_POS     = 2
MAX_LONG_POS      = 3    # 多单最大持仓数
MAX_TOTAL_POS     = 5    # 总持仓上限
SHORT_SCORE_WATCH = 75
SHORT_SCORE_FIRE  = 85
SHORT_SL_PCT      = 0.06

# ── 动态总仓位上限（每次 run_scan 开始时从账户余额计算）──
_DYN_MAX_NOTIONAL = 3000.0  # fallback 默认值，会被动态覆盖
_DYN_AVAIL_BAL    = 1000.0  # 可用余额缓存

def _refresh_notional_limit():
    """从账户实时余额计算动态名义仓位上限 = 可用余额 × 2.5，最低500U。"""
    global _DYN_MAX_NOTIONAL, _DYN_AVAIL_BAL
    try:
        bal = api.request("GET", api.BASE_FAPI + "/fapi/v2/balance", {})
        avail = float([b for b in bal if b["asset"] == "USDT"][0]["availableBalance"])
        _DYN_AVAIL_BAL    = avail
        _DYN_MAX_NOTIONAL = max(500.0, avail * 2.5)
        return avail, _DYN_MAX_NOTIONAL
    except Exception as e:
        return _DYN_AVAIL_BAL, _DYN_MAX_NOTIONAL

def _get_pos_state_full():
    """获取完整仓位状态（多/空/总数 + 已用名义仓位）。"""
    try:
        positions = [p for p in api.get_positions()
                     if abs(float(p.get("positionAmt", 0))) > 0]
        long_pos  = [p for p in positions if float(p["positionAmt"]) > 0]
        short_pos = [p for p in positions if float(p["positionAmt"]) < 0]
        used = sum(
            abs(float(p["positionAmt"])) * float(p.get("entryPrice", 0))
            for p in positions
        )
        return {
            "count":       len(positions),
            "long_count":  len(long_pos),
            "short_count": len(short_pos),
            "active_syms": {p["symbol"] for p in positions},
            "used_notional": used,
            "remaining":   max(0.0, _DYN_MAX_NOTIONAL - used),
            "long_slots":  max(0, MAX_LONG_POS  - len(long_pos)),
            "short_slots": max(0, MAX_SHORT_POS - len(short_pos)),
        }
    except Exception:
        return {"count":0,"long_count":0,"short_count":0,"active_syms":set(),
                "used_notional":0.0,"remaining":_DYN_MAX_NOTIONAL,
                "long_slots":MAX_LONG_POS,"short_slots":MAX_SHORT_POS}

def _calc_single_pos_cap(direction: str, score: float, sig_type: str = "trend") -> float:
    """
    计算单笔仓位上限，取以下三者最小值：
      1. calc_position_size 按评分计算的仓位
      2. 可用余额 × 20%（账户规模自适应）
      3. 剩余名义额度
    """
    if _GATE_AVAILABLE:
        base = _calc_pos_size(sig_type, score, direction, {})
    else:
        base = 500.0 if score >= 80 else (300.0 if score >= 70 else 200.0)
    cap_by_balance = _DYN_AVAIL_BAL * 0.20          # 单笔不超账户20%
    cap_by_notional = _DYN_MAX_NOTIONAL * 0.25      # 单笔不超总额度25%
    result = min(base, cap_by_balance, cap_by_notional)
    return max(100.0, round(result / 10) * 10)      # 最低100U，10U取整

# ── 空单动态仓位：按信号评分 300U~1000U ──
def _calc_short_notional(score: float, sl_dist_pct: float, is_top5: bool = False) -> float:
    MIN_N, MAX_N, TOP5_CAP = 300.0, 1000.0, 500.0
    # 1. 按评分线性插值
    if score >= 100:   n = MAX_N
    elif score >= 90:  n = 700 + (score - 90) / 10 * 300
    elif score >= 75:  n = 300 + (score - 75) / 15 * 400
    else:              n = MIN_N
    # 2. 风险反推：单笔亏损不超过50U
    # 注意：止损越近（pct小）风险越高，仓位应越小而不是越大
    # 不设下限，让风险反推自然压缩仓位
    if sl_dist_pct > 0:
        risk_n = 50.0 / sl_dist_pct   # 最大亏损50U对应的仓位
        n = min(n, risk_n)             # 取评分和风险两者较小的
    # 止损太近时额外打折（<3%说明开仓位置紧贴高点，风险高）
    if sl_dist_pct < 0.03:
        discount = sl_dist_pct / 0.03  # 线性打折
        n = n * (0.3 + 0.7 * discount)
    n = max(MIN_N, min(MAX_N, n))
    if is_top5:
        n = min(n, TOP5_CAP)
    return round(n / 10) * 10
SHORT_CIRCUIT_LOSS= 100
SHORT_STATE_FILE  = "/root/.openclaw/workspace/ws_cache/short_state.json"
SHORT_CIRCUIT_FILE= "/root/.openclaw/workspace/ws_cache/short_circuit.json"

reports = []

def get_klines(sym, interval, limit=60):
    return api.get_klines_proxy(sym, interval, limit)

def load_short_state():
    if os.path.exists(SHORT_STATE_FILE):
        with open(SHORT_STATE_FILE) as f: return json.load(f)
    return {"positions": {}, "watchlist": {}, "cooldown": {}}

def save_short_state(s):
    with open(SHORT_STATE_FILE, "w") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)

def load_short_circuit():
    if os.path.exists(SHORT_CIRCUIT_FILE):
        with open(SHORT_CIRCUIT_FILE) as f: return json.load(f)
    return {"triggered": False, "daily_loss": 0.0, "reset_date": ""}

def save_short_circuit(c):
    with open(SHORT_CIRCUIT_FILE, "w") as f:
        json.dump(c, f, ensure_ascii=False, indent=2)

def get_sym_info(sym):
    return api.get_sym_info_cached(sym)

def check_short_forbidden(sym, k1h, k4h, funding, lmap):
    c1h = [float(k[4]) for k in k1h]
    price = c1h[-1]
    if funding < -0.3:
        return True, f"资金费率{funding:.3f}%极负禁空"
    lows_20 = [float(k[3]) for k in k1h[-20:]]
    if (price - min(lows_20)) / min(lows_20) * 100 < 5:
        return True, "距近期低点<5%，空间不足"
    # BTC 趋势过滤：按币种分类差异化处理
    # major: 必须 BTC 全空排才允许做空
    # altcoin/volatile_alt: 只要 BTC 不是全多排即可，横盘/震荡允许做空
    try:
        import binance_api as _api
        btc_k4h = get_klines("BTCUSDT", "4h", 60)
        btc_c4h = [float(k[4]) for k in btc_k4h]
        e9  = ema_calc(btc_c4h[-9:], 9)
        e20 = ema_calc(btc_c4h[-20:], 20)
        e50 = ema_calc(btc_c4h[-50:], 50) if len(btc_c4h)>=50 else ema_calc(btc_c4h, len(btc_c4h))
        btc_bull = e9 > e20 > e50 and btc_c4h[-1] > e20  # BTC 全多排
        btc_bear = e9 < e20 < e50 and btc_c4h[-1] < e20  # BTC 全空排

        vol_sym = float(lmap.get("_vol_24h", 0))
        sym_class = _api.classify_symbol(sym, vol_sym)

        if sym_class == "major":
            # 主流币：必须 BTC 全空排
            if btc_bull: return True, "BTC全多排禁空(主流币)"
            if not btc_bear: return True, "BTC方向不明禁空(主流币)"
        else:
            # 山寨/高波动山寨：BTC 全多排才禁，横盘/震荡允许
            if btc_bull: return True, "BTC全多排禁空(山寨币)"
    except Exception: pass  # 静默异常
    try:
        depth = requests.get(f"{BASE_F}/fapi/v1/depth",
            params={"symbol": sym, "limit": 5}, proxies=PROXY, timeout=5).json()
        bid = float(depth["bids"][0][0]); ask = float(depth["asks"][0][0])
        if bid > 0 and (ask-bid)/bid*100 > 1.0: return True, f"价差>{(ask-bid)/bid*100:.2f}%"
    except Exception: pass  # 静默异常
    return False, ""

# ── 熔断检查 ──
short_state = load_short_state()
circuit = load_short_circuit()
today_str = datetime.datetime.utcnow().strftime("%Y-%m-%d")
if circuit.get("reset_date") != today_str:
    circuit = {"triggered": False, "daily_loss": 0.0, "reset_date": today_str}
    save_short_circuit(circuit)
if circuit.get("triggered"):
    print(f"🔒 做空熔断中（今日亏损≥{SHORT_CIRCUIT_LOSS}U），退出")
    sys.exit(0)

# ── 恐慌贪婪指数（全局检查）──
fg_index = 50
try:
    fg_r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=5, proxies=PROXY).json()
    fg_index = int(fg_r["data"][0]["value"])
    if fg_index < 20:
        print(f"🔴 恐慌贪婪={fg_index}<20（极度恐慌），禁止做空")
        sys.exit(0)
except Exception: pass  # 静默异常

# ── 时段降级参数（仅用于主流币）──
now_utc = datetime.datetime.utcnow()
h, m = now_utc.hour, now_utc.minute
time_penalty_for_major = 0
if (h == 13 and m >= 15) or (h == 14 and m <= 30):
    time_penalty_for_major = 10
elif 2 <= h < 6:
    time_penalty_for_major = 10

# total_penalty 在循环内按主流/山寨币单独计算，这里设为0
total_penalty = 0

# ── 手动暂停开仓开关 ──
if cfg.get("pause_new_entries", False):
    print("⏸️ 开仓已暂停（pause_new_entries=True），空单扫描跳过")
    sys.exit(0)

# ── 资金费率结算窗口 ──
fr_block, fr_reason = check_funding_settlement_window()
if fr_block:
    print(f"⏸️ 费率窗口：{fr_reason}")
    sys.exit(0)

# ── 宏观事件窗口（重大数据发布前后2h暂停做空）──
try:
    sys.path.insert(0, "/root/.openclaw/workspace")
    from macro_calendar import check_macro_window
    _mb, _mr, _mu = check_macro_window()
    if _mb:
        print(f"🌐 宏观窗口拦截，暂停做空扫描: {_mr}")
        sys.exit(0)
    elif _mu and _mu[0]["minutes_away"] <= 240:
        n = _mu[0]
        reports.append(f"⚠️ 宏观预警: {n['title']} ({n['country']}) 还有 {n['minutes_away']//60}h{n['minutes_away']%60}m")
except Exception:
    pass

# ── 当前空单数量（排除手动仓位）──
pos_all = api.request("GET", api.BASE_FAPI + "/fapi/v2/positionRisk")
if not isinstance(pos_all, list):
    pos_all = []
active_shorts = {p["symbol"] for p in pos_all
                 if isinstance(p, dict) and float(p.get("positionAmt",0)) < 0
                 and not is_manual(p["symbol"], "SHORT")}

# ── 动态名义额度 + 完整仓位状态（每轮扫描刷新一次）──
_avail_bal, _max_notional = _refresh_notional_limit()
_ps_full = _get_pos_state_full()
reports.append(
    f"💰 账户: 可用={_avail_bal:.0f}U  名义上限={_max_notional:.0f}U  "
    f"已用={_ps_full['used_notional']:.0f}U  剩余={_ps_full['remaining']:.0f}U  "
    f"持仓={_ps_full['count']}个(多{_ps_full['long_count']}/空{_ps_full['short_count']})"
)

# 空单槽位门控：数量 + 总仓位 + 剩余额度 三重检查
slot_full = (
    len(active_shorts) >= MAX_SHORT_POS or
    _ps_full['count']  >= MAX_TOTAL_POS or
    _ps_full['remaining'] < 50
)
if slot_full:
    reports.append(
        f"🔒 空单门控触发（空单{len(active_shorts)}/{MAX_SHORT_POS} "
        f"总仓{_ps_full['count']}/{MAX_TOTAL_POS} "
        f"剩余额度{_ps_full['remaining']:.0f}U），继续扫描但不开新仓"
    )

# ── 冷却期 ──
cooldowns = short_state.get("cooldown", {})
now_ts = time.time()
cooldowns = {s: ts for s, ts in cooldowns.items() if now_ts - ts < max(3600, 7200)}

# ── 候选池（扩展版：超涨池+成交量池+跌幅池三合一）──
ticker_all = requests.get(f"{BASE_F}/fapi/v1/ticker/24hr", proxies=PROXY, timeout=15).json()

_base_usdt = [
    t for t in ticker_all
    if t["symbol"].endswith("USDT")
    and float(t.get("quoteVolume", 0)) > 3_000_000
    and t["symbol"] not in active_shorts
    and t["symbol"] not in cooldowns
]
# 池六：超涨回落做空（原有逻辑保留）
_gain_pool = [
    t for t in _base_usdt
    if float(t.get("priceChangePercent", 0)) > 8
    and (
        int(t.get("count", 0)) >= 2000
        or (float(t.get("priceChangePercent", 0)) >= 30 and int(t.get("count", 0)) >= 200)
    )
]
_gain_pool.sort(key=lambda x: float(x["priceChangePercent"]), reverse=True)
_gain_pool = _gain_pool[:10]
# 池三：成交量前20（量能异动，配合价格结构判断空单方向）
_vol_pool = sorted(_base_usdt, key=lambda x: float(x.get("quoteVolume", 0)), reverse=True)[:20]
# 池二：跌幅前20（跌势延续做空 / 超跌反弹后空 / 死猫反弹顶部做空）
_drop_pool = sorted(_base_usdt, key=lambda x: float(x.get("priceChangePercent", 0)))[:20]
# 合并去重（超涨池优先，再补成交量池和跌幅池各取前10）
_seen_ss = {t["symbol"] for t in _gain_pool}
_extra_vol  = [t for t in _vol_pool  if t["symbol"] not in _seen_ss][:10]
_extra_drop = [t for t in _drop_pool if t["symbol"] not in _seen_ss
               and t["symbol"] not in {x["symbol"] for x in _extra_vol}][:10]
candidates = _gain_pool + _extra_vol + _extra_drop

# ── 反向仓位过滤：已有多单的标的不做空 ──
active_longs = {p["symbol"] for p in pos_all
                if float(p.get("positionAmt",0)) > 0
                and p.get("positionSide","BOTH") in ("LONG","BOTH")}
candidates = [t for t in candidates if t["symbol"] not in active_longs]

# ── 涨跌幅榜前五（含跌幅榜前五）：限仓 500U ──
try:
    _all_ss = sorted(ticker_all, key=lambda x: abs(float(x.get("priceChangePercent",0))), reverse=True)
    _top5_volatile_ss = {t["symbol"] for t in _all_ss[:5]}
except Exception:
    _top5_volatile_ss = set()

reports.append(f"🔍 做空扫描候选池: {len(candidates)} 个（超涨{len(_gain_pool)}+量能{len(_extra_vol)}+跌幅{len(_extra_drop)}）FG={fg_index}")

# ── 清理观察区超过24小时未出现的币 ──
wl_now = int(time.time())
wl_stale = [s for s, v in short_state.get("watchlist", {}).items()
            if wl_now - v.get("last_seen_ts", 0) > 86400]
for s in wl_stale:
    del short_state["watchlist"][s]
if wl_stale:
    save_short_state(short_state)
    reports.append(f"🧹 观察区清理过期: {', '.join(wl_stale)}")

for t in candidates:
    sym = t["symbol"]
    try:
        # 主流币才应用 FG + 时段降级
        vol_sym = float(t.get("quoteVolume", 0))
        sym_is_major = api.is_major(sym, vol_sym)
        if sym_is_major:
            fg_penalty  = 10 if fg_index <= 45 else 0
            time_penalty = time_penalty_for_major
            if fg_penalty:  reports.append(f"⚠️ {sym} 主流币 FG={fg_index}，评分-10")
            if time_penalty: reports.append(f"⏰ {sym} 主流币时段降级，评分-10")
        else:
            fg_penalty = 0
            time_penalty = 0
        total_penalty = fg_penalty + time_penalty

        k1h  = get_klines(sym, "1h",  60)
        k4h  = get_klines(sym, "4h",  60)
        k15m = get_klines(sym, "15m", 60)
        chg24 = float(t.get("priceChangePercent", 0))
        funding = float(api.request("GET", api.BASE_FAPI + "/fapi/v1/premiumIndex",
            {"symbol": sym}).get("lastFundingRate", 0)) * 100

        # 资金费率分段附加分
        funding_adj = 0
        if funding > 0.3:   funding_adj = +5
        elif -0.1 <= funding <= 0: funding_adj = -5
        elif -0.3 <= funding < -0.1: funding_adj = -15

        # Token report（4s超时）
        tr_score, tr_veto, tr_reason = 0, False, ""
        try:
            from concurrent.futures import ThreadPoolExecutor as _TPE
            with _TPE(max_workers=1) as _ex:
                _fut = _ex.submit(get_token_report_score, sym)
                tr_score, tr_veto, tr_reason = _fut.result(timeout=4)
        except Exception:
            tr_score, tr_veto, tr_reason = 0, False, "超时跳过" 
        if tr_veto: continue
        if tr_score > 0: continue  # 聪明钱正面，禁空

        # 做空评分
        score, details = calc_score_short(sym, k1h, k4h, k15m, t, funding,
                                          token_report_score=tr_score)
        final_score = score + funding_adj - total_penalty

        if final_score < SHORT_SCORE_WATCH:
            continue

        # Coinglass 清算图修正（速率由 coinglass_api._wait_rate_limit 管控，无需额外 sleep）
        try:
            cg_res = cg.get_signal_for_open(sym, "SHORT")
            final_score += cg_res.get('score_adjust', 0)
            cg_bias = cg_res.get('bias', 'neutral')
            cg_reason = cg_res.get('reason', '')
        except Exception as e:
            cg_bias = 'neutral'
            cg_reason = f'清算图API异常({e})'

        # ── 双向评分对比（Q3折中方案：空单扫描时顺手跑多单评分）──
        # 规则：多单评分 - 空单评分 > 15 → 多空信号均强，只开高分方向
        #       差值 ≤ 15 但多单评分 >= 75 → 信号冲突，两边都不开
        try:
            ls, _ = calc_score_trend(sym, k1h, k4h, k15m, t, funding)
            score_diff = final_score - ls  # 正数 = 空单更强，负数 = 多单更强
            if ls >= 75:
                if score_diff > 15:
                    pass   # 空单明显更强（差距 >15），允许做空
                else:
                    reports.append(f"⚖️ {sym} 多空冲突 空={final_score:.0f} 多={ls:.0f} 差={score_diff:.0f}，双向不开")
                    continue
        except Exception:
            pass  # 静默异常

        # 一票否决
        lmap = liquidation_map(sym, k1h, k4h, "SHORT")
        forbidden, reason = check_short_forbidden(sym, k1h, k4h, funding, lmap)
        if forbidden:
            reports.append(f"🚫 {sym} 禁止做空: {reason} (评分={final_score})")
            continue

        # 入场方式（传入 sym + vol_24h 以启用山寨币专属路径）
        entry_mode, limit_price = classify_short_entry(k1h, k15m, lmap, funding,
                                                        sym=sym, vol_24h=float(t.get('quoteVolume', 0)))
        # ── WAIT → 检查是否改挂条件限价单 ──
        if entry_mode == "WAIT":
            # 尝试用 calc_entry_mode 的逆势路径计算锚点
            try:
                _em_co, _lp_co, _, _as_co = __import__('entry_logic', fromlist=['calc_entry_mode']).calc_entry_mode(
                    k1h, 'SHORT', lmap, final_score=final_score)
                if _em_co == 'COND_ORDER':
                    entry_mode  = 'COND_ORDER'
                    limit_price = _lp_co
                    entry_note  = f'COND_ORDER 锚点={_as_co}'
                    reports.append(f"🟡 {sym} 空单条件限价单模式 限价={_lp_co:.6f} 锚点={_as_co} 评分={final_score}")
                else:
                    reports.append(f"⏳ {sym} 观察中 评分={final_score} 方向不明")
                    continue
            except Exception:
                reports.append(f"⏳ {sym} 观察中 评分={final_score} 方向不明")
                continue

        # 山寨币追空模式（CHASE_ALT/FADE）视同 CHASE，跳过评分门槛
        _is_alt_chase = entry_mode in ("CHASE_ALT", "FADE")

        effective_fire = SHORT_SCORE_FIRE + total_penalty
        if final_score < effective_fire and entry_mode not in ("CHASE", "CHASE_ALT", "FADE"):
            # ── 写入持久化观察区 ──
            wl = short_state.setdefault("watchlist", {})
            now_ts_w = int(time.time())
            cur_price_w = float(t.get("lastPrice", 0))
            if sym not in wl:
                wl[sym] = {
                    "first_seen_ts": now_ts_w,
                    "first_seen_price": cur_price_w,
                    "last_seen_ts": now_ts_w,
                    "last_score": final_score,
                    "hit_count": 1,
                    "entry_mode": entry_mode,
                }
                reports.append(f"📋 {sym} 首次进入观察区 评分={final_score}/{effective_fire} 模式={entry_mode} 价格={cur_price_w}")
            else:
                wl[sym]["hit_count"] = wl[sym].get("hit_count", 1) + 1
                wl[sym]["last_seen_ts"] = now_ts_w
                wl[sym]["last_score"] = final_score
                wl[sym]["entry_mode"] = entry_mode
                hit = wl[sym]["hit_count"]
                first_p = wl[sym].get("first_seen_price", cur_price_w)
                chg_since = (cur_price_w - first_p) / first_p * 100 if first_p else 0
                first_t = time.strftime('%m-%d %H:%M', time.gmtime(wl[sym]["first_seen_ts"]))
                reports.append(f"📋 {sym} 观察区第{hit}次 评分={final_score}/{effective_fire} 自入区{chg_since:+.1f}% 首次={first_t}")
            save_short_state(short_state)
            continue

        # 仓位已满：评分达标但不开仓，写入观察区等待仓位释放
        if slot_full:
            wl = short_state.setdefault("watchlist", {})
            now_ts_w = int(time.time())
            cur_price_w = float(t.get("lastPrice", 0))
            if sym not in wl:
                wl[sym] = {
                    "first_seen_ts": now_ts_w,
                    "first_seen_price": cur_price_w,
                    "last_seen_ts": now_ts_w,
                    "last_score": final_score,
                    "hit_count": 1,
                    "entry_mode": entry_mode,
                }
                reports.append(f"⏳ {sym} 达标(评分={final_score})但空单已满，写入观察区等待仓位释放")
            else:
                wl[sym]["hit_count"] = wl[sym].get("hit_count", 1) + 1
                wl[sym]["last_seen_ts"] = now_ts_w
                wl[sym]["last_score"] = final_score
                hit = wl[sym]["hit_count"]
                reports.append(f"⏳ {sym} 观察区等仓第{hit}次 评分={final_score} 模式={entry_mode}")
            save_short_state(short_state)
            continue

        # ── COND_ORDER：限价单预埋，不占用空单槽位 ──
        if entry_mode == 'COND_ORDER':
            _cond_state_file = "/root/.openclaw/workspace/ws_cache/cond_order_state.json"
            import pathlib as _pl
            _cs_now = json.loads(_pl.Path(_cond_state_file).read_text()) if _pl.Path(_cond_state_file).exists() else {}
            # 容量检查：空单条件单上限 5个
            _cs_shorts = [v for v in _cs_now.values() if v.get('direction') == 'SHORT']
            if sym in _cs_now:
                reports.append(f"⏳ {sym} 空单条件单已存在，跳过")
                continue
            if len(_cs_shorts) >= 5:
                reports.append(f"⏳ {sym} 空单条件单已达上限 5，跳过")
                continue
            tick_co, step_co = get_sym_info(sym)
            _lp_co = fmt_price(limit_price, tick_co)
            _atr_co = atr_calc(k1h)
            # 止损/止盈用 ATR 动态计算，不用固定6%/8%
            _sl_co_raw = lmap.get('liq_above', limit_price + _atr_co * 2.0)
            _sl_co_raw = max(_sl_co_raw, limit_price + _atr_co * 2.0)  # 至少 ATR×2
            _sl_co  = fmt_price(_sl_co_raw, tick_co)
            _tp_co_raw = lmap.get('liq_below', limit_price - _atr_co * 2.0)
            _tp_co_raw = min(_tp_co_raw, limit_price - _atr_co * 2.0)  # 至少 ATR×2
            _tp_co  = fmt_price(_tp_co_raw, tick_co)
            _notional_co = 300  # 条件单固定下限 300U
            if final_score >= 85: _notional_co = 600
            elif final_score >= 75: _notional_co = 450
            _qty_co = fmt_qty(_notional_co / float(_lp_co), step_co)
            # 设杯信权
            api.request("POST", api.BASE_FAPI + "/fapi/v1/leverage", {"symbol": sym, "leverage": 50})
            r_co = api.place_order(sym, "SELL", "LIMIT",
                                   quantity=_qty_co, price=_lp_co,
                                   positionSide="SHORT", timeInForce="GTC")
            if not isinstance(r_co, dict) or not r_co.get("orderId"):
                reports.append(f"❌ {sym} 空单条件单下单失败: {r_co}")
                continue
            _dev_co = abs(float(_lp_co) - float(t.get('lastPrice', _lp_co))) / float(_lp_co) * 100
            _now_co = int(time.time())
            _cs_now[sym] = {
                'direction': 'SHORT', 'order_id': r_co['orderId'],
                'limit_price': float(_lp_co), 'sl': float(_sl_co),
                'tp1': float(_tp_co), 'qty': float(_qty_co),
                'placed_at': _now_co, 'expire_at': _now_co + 4*3600,
                'score': final_score, 'sig_type': 'trend_short',
                'anchor_src': str(limit_price), 'notional': _notional_co,
            }
            _pl.Path(_cond_state_file).write_text(json.dumps(_cs_now, indent=2, ensure_ascii=False))
            reports.append(f"🟡 {sym} 空单条件限价单已挂 限价={_lp_co}（离现价{_dev_co:.1f}%） {_notional_co}U SL={_sl_co}")
            continue

        # ── 开仓执行──
        # 计算下单参数
        tick, step = get_sym_info(sym)
        limit_str = fmt_price(limit_price, tick)

        # ── 空单动态仓位：账户规模自适应 + 评分加权 ──────────────────────────
        _is_top5_ss  = sym in _top5_volatile_ss
        _notional_base = _calc_short_notional(score, SHORT_SL_PCT, is_top5=_is_top5_ss)
        # 单笔上限：评分计算结果（最高1000U），不额外限制账户比例
        # 剩余额度保护：若剩余不足以覆盖本次仓位，按剩余额度开（但不低于50U）
        _pos_cap_by_rem  = max(50.0, _ps_full["remaining"])
        _notional_ss     = min(_notional_base, _pos_cap_by_rem)
        _notional_ss     = round(_notional_ss / 10) * 10  # 10U取整
        if _is_top5_ss:
            _notional_ss = min(_notional_ss, 500)
            reports.append(f"📊 {sym} 涨跌幅榜前五，空单仓位封顶 500U")
        if _notional_ss < 50:
            reports.append(f"⚠️ {sym} 动态仓位不足50U，账户余额不足以开仓，跳过")
            continue
        reports.append(f"📐 {sym} 动态仓位={_notional_ss:.0f}U (base={_notional_base:.0f}U cap_bal={_pos_cap_by_bal:.0f}U cap_rem={_pos_cap_by_rem:.0f}U)")
        qty_str = fmt_qty(_notional_ss / float(limit_str), step)

        _atr_short = atr_calc(k1h)

        # ── 山寨币专属 SL/TP（CHASE_ALT / FADE 模式）──
        # 逻辑：止损放在近12根K线最高点上方 ATR×1.2（比主流币更宽）
        #       止盈分三档：TP1=高点下跌40%、TP2=高点下跌60%、TP3=TP2×0.93
        if _is_alt_chase:
            _alt_high = max((float(k[2]) for k in k1h[-24:]), default=limit_price)
            sl_price = _alt_high + _atr_short * 1.2   # 止损放在最高点上方
            tp1 = limit_price * (1 - 0.40)             # TP1: -40% from entry
            tp2 = limit_price * (1 - 0.60)             # TP2: -60% from entry
            tp3 = tp2 * 0.93
            _cg_src_short = f'alt_chase_rules({entry_mode})'
        else:
            # ── 优先用 Coinglass 真实清算图计算 SL/TP，失败则 fallback 到 lmap ──
            _lmap_fallback = {
                'hard_sl':  limit_price * (1 + SHORT_SL_PCT),   # 默认止损 +6%
                'soft_tp1': lmap.get('liq_below', limit_price * 0.94),
                'soft_tp2': lmap.get('hard_sl',   limit_price * 0.88),
            }
            try:
                _cg_zones = cg.get_liq_zones(sym, limit_price)
                _above = _cg_zones.get('above', [])
                _below = _cg_zones.get('below', [])
                sl_cg = _above[-1][0] + _atr_short * 0.3 if _above else limit_price * (1 + SHORT_SL_PCT)
                _recent_high_ss = max((float(k[2]) for k in k1h[-12:]), default=limit_price)
                _sl_floor_ss = _recent_high_ss + _atr_short * 0.8
                sl_price = max(sl_cg, _sl_floor_ss)

                # ── TP1/TP2：按量排序取最大清算区，加最小距离保护 ──
                # 空单 TP = 下方清算密集区下边界（让价格穿过清算区而非停在中心）
                # 按 volume 降序取前两名，确保小币也能选到最有意义的区域
                _MIN_TP1_PCT = 0.02  # TP1 最少离入场 2%
                _MIN_TP2_PCT = 0.04  # TP2 最少离入场 4%
                if _below:
                    # 按 volume 降序排列（每条 zone = [price, volume]）
                    _below_sorted = sorted(_below, key=lambda z: z[1] if len(z) > 1 else 0, reverse=True)
                    # 找满足最小距离的最大量清算区
                    _tp1_candidates = [z[0] for z in _below_sorted if z[0] <= limit_price * (1 - _MIN_TP1_PCT)]
                    _tp2_candidates = [z[0] for z in _below_sorted if z[0] <= limit_price * (1 - _MIN_TP2_PCT)]
                    # TP1 = 最近满足距离的大量清算区下边界（-ATR×0.1 缓冲穿越）
                    tp1 = (_tp1_candidates[0] - _atr_short * 0.1) if _tp1_candidates else limit_price * (1 - _MIN_TP1_PCT)
                    # TP2 = 第二个大量清算区（或比 TP1 更远的那个）
                    _tp2_raw = [z for z in _tp2_candidates if z < tp1 + _atr_short * 0.1]
                    tp2 = (_tp2_raw[0] if _tp2_raw else tp1 * (1 - _MIN_TP2_PCT))
                else:
                    # Coinglass 无数据：ATR 兜底 + 最小距离取大值
                    tp1 = min(limit_price * (1 - _MIN_TP1_PCT), limit_price - _atr_short * 2)
                    tp2 = min(limit_price * (1 - _MIN_TP2_PCT), limit_price - _atr_short * 4)
                    tp1 = min(tp1, limit_price * (1 - _MIN_TP1_PCT))
                    tp2 = min(tp2, limit_price * (1 - _MIN_TP2_PCT))
                    tp1 = limit_price * (1 - max(_MIN_TP1_PCT, _atr_short * 2 / limit_price))
                    tp2 = limit_price * (1 - max(_MIN_TP2_PCT, _atr_short * 4 / limit_price))
                _cg_src_short = 'coinglass+min_dist'
            except Exception as _e_cg_short:
                sl_price = _lmap_fallback['hard_sl']
                tp1      = _lmap_fallback['soft_tp1']
                tp2      = _lmap_fallback['soft_tp2']
                _cg_src_short = f'lmap_fallback({_e_cg_short})'

            # 近期高点兜底
            try:
                _recent_high_final = max((float(k[2]) for k in k1h[-12:]), default=limit_price)
                _sl_floor_final = _recent_high_final + _atr_short * 0.8
                if sl_price < _sl_floor_final:
                    sl_price = _sl_floor_final
                    _cg_src_short += '+recent_high_guard'
            except Exception:
                pass

            tp3 = tp2 * 0.93

        sl_str  = fmt_price(sl_price, tick)
        tp1_str = fmt_price(tp1, tick); tp2_str = fmt_price(tp2, tick); tp3_str = fmt_price(tp3, tick)

        # ── 用真实止损距离修正仓位（sl_price 确定后重算）──
        _real_sl_pct = abs(float(sl_str) - float(limit_str)) / float(limit_str) if float(limit_str) > 0 else _sl_est_pct
        _notional_ss = _calc_short_notional(score, _real_sl_pct, is_top5=_is_top5_ss)
        qty_str = fmt_qty(_notional_ss / float(limit_str), step)

        # ── 爆仓价安全检查：自动缩减仓位，确保爆仓缓冲 ≥ 30% ──
        _safe_n_ss, _liq_ss, _buf_ss, _reduced_ss = _safe_notional_ss(
            sym, "SHORT", float(limit_str), _notional_ss, min_notional=300.0
        )
        if _safe_n_ss == 0:
            reports.append(f"🚫 {sym} 爆仓风险过高，账户净值不足以安全开空，跳过")
            continue
        if _reduced_ss:
            reports.append(f"⚠️ {sym} 空单仓位从{_notional_ss:.0f}U缩减至{_safe_n_ss:.0f}U（爆仓缓冲={_buf_ss:.1%}）")
        _notional_ss = _safe_n_ss
        qty_str = fmt_qty(_notional_ss / float(limit_str), step)

        # ── 最终守卫：下单前最后一道防线 ──
        _g_ok, _g_reason = _check_before_open(sym, "SHORT", notional=float(qty_str)*float(limit_str))
        if not _g_ok:
            reports.append(f"🛡️ {sym} 守卫拦截: {_g_reason}")
            continue

        r_order = api.place_order(sym, "SELL", "LIMIT",
            quantity=qty_str, price=limit_str, timeInForce="GTC", positionSide="SHORT")

        if not isinstance(r_order, dict) or not r_order.get("orderId"):
            reports.append(f"❌ {sym} 做空下单失败: {r_order}")
            continue

        # ── SL/TP 全部等开仓成交后由 ws_monitor open_filled 回调挂单 ──
        # 开仓限价单未成交前不挂任何止损止盈，避免无效占位
        _sl_placed, _sl_id = False, None
        _tp_placed_note = f"TP目标已记录，成交后 ws_monitor 自动挂单 ({tp1_str}/{tp2_str})"

        # ── 统一写入 pos_state.json，由 ws_monitor 接管止盈止损维护 ──
        # 不再写 short_state["positions"]，消除双轨状态管理
        _ps_now = _load_pos_state()
        _ps_now.setdefault(sym, {})
        _ps_now[sym].update({
            "direction":        "SHORT",
            "hard_sl":          float(sl_str),
            "entry_price_hint": float(limit_str),
            "tp1_price":        float(tp1_str),
            "tp2_price":        float(tp2_str),
            "tp3_price":        float(tp3_str),
            "qty":              qty_str,
            "sig_score":        final_score,
            "entry_mode":       entry_mode,
            "pending_order_id": r_order["orderId"],
            "opened_at":        int(time.time()),
            "entry_funding":    funding,
            "sl_pre_placed":    False,
            "sl_algo_id":       None,
            "source":           "short_scanner",
        })
        _save_pos_state(_ps_now)
        sl_note = "SL/TP待成交后自动挂单"
        reports.append(
            f"📉 做空挂单 {sym} 评分={final_score} 模式={entry_mode}\n"
            f"   限价={limit_str} 止损={sl_str} TP1={tp1_str}(40%) TP2={tp2_str}(35%) TP3={tp3_str}(25%) SL/TP来源={_cg_src_short}\n"
            f"   {sl_note} | {_tp_placed_note}\n"
            f"   EMA:{details['ema_4h']}/{details['ema_1h']} RSI:{details['rsi_4h']:.0f}/{details['rsi_1h']:.0f} "
            f"费率:{funding:.3f}%({'+' if funding_adj>0 else ''}{funding_adj}) 量比:{details['vr']:.2f}x\n"
            f"   清算图偏向={cg_bias} {cg_reason}"
        )

    except Exception as e:
        reports.append(f"⚠️ {sym} 做空扫描异常: {e}")

if not reports:
    print("HEARTBEAT_OK")
else:
    ts = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    print(f"📉 做空扫描 | {ts}")
    for r in reports:
        print(r)

# ── 观察区汇总（无论有没有新信号，都输出）──
wl_final = short_state.get("watchlist", {})
if wl_final:
    print(f"\n📌 观察区当前 {len(wl_final)} 个币:")
    for sym_w, info_w in wl_final.items():
        hit_w    = info_w.get("hit_count", 1)
        score_w  = info_w.get("last_score", "?")
        fp_w     = info_w.get("first_seen_price", 0)
        ft_w     = time.strftime('%m-%d %H:%M', time.gmtime(info_w.get("first_seen_ts", 0)))
        mode_w   = info_w.get("entry_mode", "?")
        # 当前价格
        try:
            cur_w = float(requests.get(f"{BASE_F}/fapi/v1/ticker/price",
                params={"symbol": sym_w}, proxies=PROXY, timeout=5).json().get("price", fp_w))
            chg_w = (cur_w - fp_w) / fp_w * 100 if fp_w else 0
            price_str = f"现价={cur_w} 自入区{chg_w:+.1f}%"
        except Exception:
            price_str = "价格获取失败"
        print(f"  {sym_w} | 评分={score_w} 模式={mode_w} 连续达标={hit_w}次 首次={ft_w} {price_str}")
