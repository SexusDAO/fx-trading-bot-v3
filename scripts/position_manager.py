"""
position_manager.py — 综合仓位管理 + 市场扫描
严格按 TRADING_RULES.md + entry_logic.py 执行

运行逻辑：
  1. 维护已有仓位（止损/止盈检查、移动止损、提前平仓判断、补仓）
  2. 若仓位数 < 5，扫描市场寻找新信号并自动开仓
"""
import sys, os, requests, json, math, time, subprocess, datetime
sys.path.insert(0, "/root/.openclaw/workspace")
import binance_api as api
import coinglass_api as cg
from manual_positions import is_manual
sys.path.insert(0, "/root/.openclaw/workspace/scripts")
from pre_open_guard import check_before_open
import sys as _sys_lg; _sys_lg.path.insert(0, "/root/.openclaw/workspace/scripts")
from liquidation_guard import (safe_notional as _safe_notional,
                               check_liquidation_risk as _check_liq,
                               _get_account_state as _lg_refresh_state)

# 运行开始时主动刷新账户状态缓存
try:
    _lg_refresh_state()
except Exception:
    pass

# ── 信号评分 → 仓位大小映射 ──────────────────────────────────────────────────
# 评分区间 → 名义仓位（U），最低300U，最高1000U
# 评分90~100: CHASE模式，信心最强 → 最大仓位
# 评分75~89:  回调模式，普通信号  → 按分数线性插值
# 涨跌幅榜前五: 额外封顶500U（高波动保护）
def calc_notional_by_score(score: float,
                            sl_dist_pct: float,
                            is_volatile_top5: bool = False) -> float:
    """
    根据信号评分和止损距离动态计算开仓名义仓位。
    score       : 最终评分（0-100）
    sl_dist_pct : 止损距离百分比（0.05 = 5%）
    is_volatile_top5: 是否在涨跌幅榜前五
    返回: 最终名义仓位（U），已取整到10U
    """
    MIN_N  = 300.0    # 最低仓位
    MAX_N  = 1000.0   # 最高仓位
    TOP5_CAP = 500.0  # 涨跌幅榜前五封顶

    # 1. 按评分线性插值
    #    score=75  → 300U（最低）
    #    score=90  → 700U（中档）
    #    score=95  → 850U
    #    score=100 → 1000U（最高）
    # 复盘模式：固定100U（真实档位见 calc_position_size_real.py）
    try:
        import sys, os
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from calc_position_size import calc as _cps
        score_notional = _cps('trend', score, 'LONG', {})
    except Exception:
        score_notional = 100.0

    # 2. 按风险金额反推：单笔亏损不超过账户2%（约动态本金×2%）
    #    用固定保守值 MAX_LOSS_PER_TRADE 限制
    MAX_LOSS = 50.0   # 单笔最大允许亏损U（提升到50U，配合1000U仓位）
    if sl_dist_pct > 0:
        risk_notional = MAX_LOSS / sl_dist_pct
    else:
        risk_notional = score_notional

    # 3. 取评分仓位和风险仓位的较小值
    notional = min(score_notional, risk_notional)

    # 4. 止损太近时额外打折（止损<3%说明开仓位置紧贴高低点，风险高）
    if sl_dist_pct < 0.03:
        discount = sl_dist_pct / 0.03   # 线性打折：1.1%->37%, 2%->67%, 3%->100%
        notional = notional * (0.3 + 0.7 * discount)

    notional = max(MIN_N, notional)      # 不低于300U
    notional = min(MAX_N, notional)      # 不超过1000U

    # 5. 涨跌幅榜前五额外封顶
    if is_volatile_top5:
        notional = min(notional, TOP5_CAP)

    return round(notional / 10) * 10   # 取整到10U
# ─────────────────────────────────────────────────────────────────────────────
from entry_logic import (
    classify_signal, calc_score_trend, calc_score_reversal,
    calc_score_short, classify_short_entry,
    liquidation_map, calc_entry_mode,
    check_oi_divergence, check_btc_filter, check_funding_settlement_window,
    get_token_report_score,
    rsi_calc, atr_calc, ema_calc, fmt_price, fmt_qty
)

PROXY = {"http": api.PROXY_URL, "https": api.PROXY_URL}
# _is_major 已迁移到 binance_api.py → api.is_major


# ═══════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════
BASE_F       = "https://fapi.binance.com"
CFG_FILE     = "/root/.openclaw/workspace/strategy-config.json"
STATE_FILE   = "/root/.openclaw/workspace/ws_cache/scanner_state.json"
WATCHLIST_FILE = "/root/.openclaw/workspace/ws_cache/watchlist.json"

cfg = json.load(open(CFG_FILE))
FIRST_SIZE   = cfg["position"]["first_size_usdt"]        # 500U
MAX_POS      = cfg["position"]["max_positions_total"]    # 5

# 做空独立配置
SHORT_NOTIONAL      = 200          # 做空名义値固定 200U
MAX_SHORT_POS       = 2            # 最多 2 个空单（每个上限 300U）
SHORT_SCORE_WATCH   = 75           # 进入观察区
SHORT_SCORE_FIRE    = 85           # 直接开空
SHORT_SL_PCT        = 0.06         # 默认止损 +6%
# 做空熔断线动态从 circuit_limit 获取（本金×20%）
import sys as _sys_pm; _sys_pm.path.insert(0, "/root/.openclaw/workspace/scripts")
try:
    from circuit_limit import get_daily_loss_limit as _get_circuit_limit
    SHORT_CIRCUIT_LOSS = _get_circuit_limit()
except Exception:
    SHORT_CIRCUIT_LOSS = 80.0   # fallback
SHORT_STATE_FILE    = "/root/.openclaw/workspace/ws_cache/short_state.json"
SHORT_CIRCUIT_FILE  = "/root/.openclaw/workspace/ws_cache/short_circuit.json"
TARGET_LEV   = cfg["position"]["leverage_target"]        # 50
ADDON_MAX    = cfg["addon"]["max_times"]                 # 1
ADDON_TRIGGER= cfg["addon"]["trigger_loss_pct"]          # -3.0
ADDON_INTERVAL= cfg["addon"]["min_interval_minutes"]     # 30

os.makedirs("/root/.openclaw/workspace/ws_cache", exist_ok=True)
state = json.load(open(STATE_FILE)) if os.path.exists(STATE_FILE) else {}
# state 结构: { "positions": { "BTCUSDT": {...}, ... } }
if "positions" not in state:
    state["positions"] = {}

# 全局超时保护（80秒后强制退出）
import signal as _gsig
def _global_timeout(s, f):
    print("⏰ 脚本执行超时(80s)，本轮跳过", flush=True)
    import sys; sys.exit(0)
_gsig.signal(_gsig.SIGALRM, _global_timeout)
_gsig.alarm(80)

# 并行工具
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FutureTimeout

reports = []  # 本轮输出，必须在所有逻辑之前初始化

_state_dirty = False  # 脏标记，避免频繁写盘

def save_state(immediate=False):
    """写入 state。默认只标记脏，由 flush_state() 统一写盘。"""
    global _state_dirty
    _state_dirty = True
    if immediate:
        _flush_state()

def _flush_state():
    """真正写盘，每轮结束调用一次。"""
    global _state_dirty
    if not _state_dirty:
        return
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    _state_dirty = False

def get_sym_info(sym):
    """使用缓存版 exchangeInfo，避免每次重复下载"""
    return api.get_sym_info_cached(sym)

def get_max_leverage(sym):
    try:
        b = api.request("GET", api.BASE_FAPI + "/fapi/v1/leverageBracket", {"symbol": sym})
        if isinstance(b, list) and b:
            return b[0]["brackets"][0]["initialLeverage"]
        if isinstance(b, dict) and "brackets" in b:
            return b["brackets"][0]["initialLeverage"]
    except Exception: pass  # 静默异常
    return TARGET_LEV

def set_leverage(sym):
    max_lev = get_max_leverage(sym)
    lev = min(TARGET_LEV, max_lev)
    api.request("POST", f"{BASE_F}/fapi/v1/leverage", {"symbol": sym, "leverage": lev})
    return lev

def get_sl_tp_from_coinglass(sym, direction, entry, current_price, atr, lmap_fallback, k1h=None):
    """
    优先用 Coinglass 真实清算图，失败则 fallback 到 liquidation_map。
    止损增加近期高低点兜底：确保止损放在近期极值外侧+ATR缓冲，避免太近被扫。
    返回 (sl, tp1, tp2, source_str)
    """
    # ── 近期高低点兜底计算（独立于 Coinglass，始终执行）──
    recent_high = recent_low = None
    if k1h and len(k1h) >= 12:
        recent_high = max(float(k[2]) for k in k1h[-12:])  # 近12根1h高点
        recent_low  = min(float(k[3]) for k in k1h[-12:])  # 近12根1h低点

    try:
        time.sleep(7)  # 速率保护（跨进程限速由 coinglass_api._wait_rate_limit 处理）
        zones = cg.get_liq_zones(sym, current_price)
        if direction == 'LONG':
            below = zones.get('below', [])
            sl_cg = below[-1][0] - atr * 0.3 if below else lmap_fallback['hard_sl']
            # 兜底：止损不得高于近期低点下方（多单止损必须在低点外侧）
            if recent_low:
                sl_floor = recent_low - atr * 0.5
                sl_cg = min(sl_cg, sl_floor)  # 取更低的那个（更安全）
            sl = sl_cg
            above = zones.get('above', [])
            # ── 多单 TP：最小距离 + 按量取最大清算区 + 上边界（顶部） ──
            _MIN_TP1_L = entry * 1.02
            _MIN_TP2_L = entry * 1.04
            if above:
                _above_sorted = sorted(above, key=lambda z: z[1] if len(z) > 1 else 0, reverse=True)
                _tp1_c_l = [z[0] + atr * 0.1 for z in _above_sorted if z[0] >= _MIN_TP1_L]
                _tp2_c_l = [z[0] + atr * 0.1 for z in _above_sorted if z[0] >= _MIN_TP2_L]
                tp1 = _tp1_c_l[0] if _tp1_c_l else _MIN_TP1_L
                tp2 = (_tp2_c_l[0] if _tp2_c_l else tp1 * 1.02)
            else:
                tp1 = max(lmap_fallback['soft_tp1'], _MIN_TP1_L)
                tp2 = max(lmap_fallback['soft_tp2'], _MIN_TP2_L)
        else:  # SHORT
            above = zones.get('above', [])
            sl_cg = above[-1][0] + atr * 0.3 if above else lmap_fallback['hard_sl']
            # 兜底：空单止损必须在近期高点外侧+ATR×0.8 缓冲
            # 防止上方无清算区时 lmap 给出过近的止损（ESPORTS 教训）
            if recent_high:
                sl_floor = recent_high + atr * 0.8
                sl_cg = max(sl_cg, sl_floor)  # 取更高的那个（更安全）
            sl = sl_cg
            below = zones.get('below', [])
            # ── 最小距离保护 + 按量取最大清算区 + 空单用下边界（穿过清算区） ──
            _MIN_TP1 = entry * 0.98  # 空单 TP1 必须 ≤ 入场 -2%
            _MIN_TP2 = entry * 0.96  # 空单 TP2 必须 ≤ 入场 -4%
            if below:
                _below_sorted = sorted(below, key=lambda z: z[1] if len(z) > 1 else 0, reverse=True)
                _tp1_c = [z[0] - atr * 0.1 for z in _below_sorted if z[0] <= _MIN_TP1]
                _tp2_c = [z[0] - atr * 0.1 for z in _below_sorted if z[0] <= _MIN_TP2]
                tp1 = _tp1_c[0] if _tp1_c else _MIN_TP1
                tp2 = (_tp2_c[0] if _tp2_c else tp1 * 0.98)
            else:
                tp1 = min(lmap_fallback['soft_tp1'], _MIN_TP1)
                tp2 = min(lmap_fallback['soft_tp2'], _MIN_TP2)
        return sl, tp1, tp2, 'coinglass+recent_high_guard+min_dist'
    except Exception as e:
        # fallback 到 liquidation_map，同样加近期高低点兜底
        sl_fb = lmap_fallback['hard_sl']
        if direction == 'SHORT' and recent_high:
            sl_fb = max(sl_fb, recent_high + atr * 0.8)
        elif direction == 'LONG' and recent_low:
            sl_fb = min(sl_fb, recent_low - atr * 0.5)
        return (sl_fb,
                lmap_fallback['soft_tp1'],
                lmap_fallback['soft_tp2'],
                f'lmap_fallback+recent_guard({e})')


def place_sl_tp(sym, sl_price, tp_price, qty_str, pos_side="LONG", tp2_price=0, tp3_price=0):
    """
    挂止损 + 三档止盈（40%/30%/30%）
    tp_price  = TP1（40%仓位）
    tp2_price = TP2（30%仓位），0 则跳过
    tp3_price = TP3（30%仓位，移动止盈追踪），0 则跳过
    返回 (sl_ok, tp1_ok)
    """
    close_side = "SELL" if pos_side == "LONG" else "BUY"
    qty_f = float(qty_str)

    # ── 防重复：下单前先撤掉该 symbol 所有残留 algo 单 ──
    # 注意：每次调用 place_sl_tp 都会先清空再重挂，调用方需确保只在必要时调用
    try:
        existing = api.request("GET", api.BASE_FAPI + "/fapi/v1/openAlgoOrders")
        if isinstance(existing, list):
            sym_existing = [x for x in existing if x.get("symbol") == sym]
            for o in sym_existing:
                api.request("DELETE", api.BASE_FAPI + "/fapi/v1/algoOrder", {"algoId": o.get("algoId")})
            if sym_existing:
                import time as _t; _t.sleep(0.3)  # 撤单后等一小会再挂，避免交易所未及时处理
    except Exception:
        pass  # 静默异常，不阻断下单

    # ── 止损（全仓）──
    sl_ok = False
    try:
        _sl_oid, r = place_sl_order(sym, close_side, int(qty_f), sl_price, pos_side)
        sl_ok = bool(_sl_oid)
    except Exception: pass  # 静默异常

    # ── 三档止盈数量分配：40% / 30% / 30% ──
    qty_tp1 = int(qty_f * 0.40)
    qty_tp2 = int(qty_f * 0.30)
    qty_tp3 = int(qty_f) - qty_tp1 - qty_tp2  # 余量归 TP3

    # TP1（40%）
    tp1_ok = False
    try:
        r = api.place_algo_order(sym, close_side, "TAKE_PROFIT_MARKET", tp_price, pos_side, quantity=qty_tp1)
        tp1_ok = bool(r.get("algoId")) if isinstance(r, dict) else False
    except Exception: pass  # 静默异常

    # TP2（30%）
    if tp2_price > 0:
        try:
            api.place_algo_order(sym, close_side, "TAKE_PROFIT_MARKET", tp2_price, pos_side, quantity=qty_tp2)
        except Exception: pass  # 静默异常

    # TP3（30%，移动止盈：以 tp3_price 为触发，触发后转为追踪）
    if tp3_price > 0:
        try:
            api.place_algo_order(sym, close_side, "TAKE_PROFIT_MARKET", tp3_price, pos_side, quantity=qty_tp3)
        except Exception: pass  # 静默异常

    return sl_ok, tp1_ok

def has_pending_buy(sym):
    """检查该 symbol 是否已有 BUY 方向挂单，有则返回 True 防止重复"""
    try:
        existing = api.request("GET", api.BASE_FAPI + "/fapi/v1/openOrders", {"symbol": sym})
        if isinstance(existing, list):
            return any(o.get("side") == "BUY" and o.get("status") == "NEW" for o in existing)
    except Exception: pass  # 静默异常
    return False


def _get_om_tp_ids():
    """实时读取所有受保护订单 ID（优先读注册表，备选读 state 文件）"""
    try:
        from order_registry import registry as _reg
        ids = _reg.get_protected_ids()
        if ids:
            return ids
    except Exception:
        pass
    # 备选：直接读 state 文件
    ids = set()
    state_files = [
        "/root/.openclaw/workspace/on_monitor_state.json",
        "/root/.openclaw/workspace/on_long_state.json",
    ]
    for path in state_files:
        try:
            import json as _j
            with open(path) as _f:
                _om = _j.load(_f)
            for _k in ('tp1_order', 'tp2_order', 'tp3_order', 'tp_order', 'sl_order_id'):
                if _om.get(_k):
                    ids.add(str(_om[_k]))
        except Exception:
            pass
    return ids

def cancel_all_orders(sym):
    """撤销该 symbol 所有挂单（普通单 + algo 条件单）— 跳过 on_monitor 止盈单"""
    om_tp_ids = _get_om_tp_ids()
    # 1. 撤普通挂单（逐笔撤，跳过白名单）
    try:
        all_open = api.request("GET", api.BASE_FAPI + "/fapi/v1/openOrders", {"symbol": sym})
        if isinstance(all_open, list):
            for o in all_open:
                if str(o.get("orderId")) in om_tp_ids:
                    continue  # 跳过 on_monitor 止盈单
                try:
                    api.cancel_order(sym, o["orderId"])
                except Exception as e:
                    print(f"[cancel_all_orders] 撤单异常 {sym} #{o['orderId']}: {e}")
    except Exception as e:
        print(f"[cancel_all_orders] 普通单撤销异常 {sym}: {e}")
    # 2. 撤 algo 条件单（STOP_MARKET / TAKE_PROFIT_MARKET）
    try:
        algo_orders = api.request("GET", api.BASE_FAPI + "/fapi/v1/openAlgoOrders")
        if isinstance(algo_orders, list):
            sym_algo = [o for o in algo_orders if o.get("symbol") == sym]
            for o in sym_algo:
                aid = o.get("algoId")
                if aid:
                    try:
                        api.request("DELETE", api.BASE_FAPI + "/fapi/v1/algoOrder", {"algoId": aid})
                    except Exception as e2:
                        print(f"[cancel_all_orders] algo单撤销异常 algoId={aid}: {e2}")
    except Exception as e:
        print(f"[cancel_all_orders] 查询algo单异常 {sym}: {e}")

def place_sl_order(symbol, side, qty, stop_price, position_side):
    """统一止损下单：优先 algo_order，fallback STOP_MARKET"""
    try:
        r = api.place_algo_order(
            symbol=symbol, side=side,
            order_type="STOP", stop_price=stop_price,
            position_side=position_side, quantity=str(qty)
        )
        oid = r.get('orderId') or r.get('newOrderId') or r.get('order', {}).get('orderId')
        if oid:
            return str(oid), r
    except Exception:
        pass
    # fallback
    r2 = api.place_order(
        symbol=symbol, side=side, order_type="STOP_MARKET",
        quantity=str(qty), stopPrice=str(stop_price),
        positionSide=position_side
    )
    return str(r2.get('orderId', '')), r2

# ═══════════════════════════════════════════════
# 第零步：重新评估当前挂单是否还需要保留
# ═══════════════════════════════════════════════
open_orders = api.request("GET", api.BASE_FAPI + "/fapi/v1/openOrders", {})

# 加载 on_monitor 止盈单 ID 白名单（这些单是已持仓的止盈单，不是待入场挂单，不得撤销）
_on_monitor_tp_ids = set()
try:
    import json as _json
    _om_state_path = "/root/.openclaw/workspace/on_monitor_state.json"
    with open(_om_state_path) as _f:
        _om = _json.load(_f)
    for _k in ('tp1_order', 'tp2_order', 'tp3_order'):
        if _om.get(_k):
            _on_monitor_tp_ids.add(str(_om[_k]))
except Exception:
    pass

# 加载 active_symbols 列表——被专属监控管理的标的，position_manager 不重复干预
_ACTIVE_SYMBOL_FILE = '/root/.openclaw/workspace/ws_cache/active_symbols.json'
_active_symbols = {}
try:
    with open(_ACTIVE_SYMBOL_FILE) as _f:
        _active_symbols = _json.load(_f)
except Exception:
    pass

pending_orders = []
if isinstance(open_orders, list):
    pending_orders = [
        o for o in open_orders
        if o.get("status") == "NEW"
        and str(o.get("orderId")) not in _on_monitor_tp_ids  # 跳过 on_monitor 止盈单
        and not o.get("reduceOnly", False)  # 跳过止盈/止损减仓单
        and (
            # 多单开仓：BUY LONG
            (o.get("side") == "BUY" and o.get("positionSide") in ("LONG", "BOTH"))
            # 空单开仓：SELL SHORT（short_scanner 挂的，需要复查信号）
            or (o.get("side") == "SELL" and o.get("positionSide") == "SHORT")
        )
    ]

def _fetch_sym_data(sym):
    """并行拉取单个币种的5个数据，节省约4x时间"""
    with ThreadPoolExecutor(max_workers=5) as ex:
        f_tk   = ex.submit(requests.get, f"{BASE_F}/fapi/v1/ticker/24hr",
                           **{"params":{"symbol":sym},"timeout":6,"proxies":PROXY})
        f_k1h  = ex.submit(requests.get, f"{BASE_F}/fapi/v1/klines",
                           **{"params":{"symbol":sym,"interval":"1h","limit":30},"timeout":6,"proxies":PROXY})
        f_k4h  = ex.submit(requests.get, f"{BASE_F}/fapi/v1/klines",
                           **{"params":{"symbol":sym,"interval":"4h","limit":30},"timeout":6,"proxies":PROXY})
        f_k15m = ex.submit(requests.get, f"{BASE_F}/fapi/v1/klines",
                           **{"params":{"symbol":sym,"interval":"15m","limit":30},"timeout":6,"proxies":PROXY})
        f_fr   = ex.submit(requests.get, f"{BASE_F}/fapi/v1/premiumIndex",
                           **{"params":{"symbol":sym},"timeout":6,"proxies":PROXY})
    return (f_tk.result().json(), f_k1h.result().json(),
            f_k4h.result().json(), f_k15m.result().json(), f_fr.result().json())

for po in pending_orders:
    sym      = po["symbol"]
    op_price = float(po["price"])
    op_qty   = float(po["origQty"])
    oid      = po["orderId"]

    # 被专属监控管理的标的，position_manager 跳过，不撤单不重下
    if sym in _active_symbols:
        reports.append(f"⏭️ {sym} 被 {_active_symbols[sym].get('role','?')} 专属监控，position_manager 跳过")
        continue

    try:
        tk, k1h, k4h, k15m, fr_r = _fetch_sym_data(sym)
        mark  = float(tk.get("lastPrice", 0))
        chg24 = float(tk.get("priceChangePercent", 0))
        funding = float(fr_r.get("lastFundingRate", 0)) * 100

        is_short_order = (po.get("side") == "SELL" and po.get("positionSide") == "SHORT")

        if is_short_order:
            # short_scanner 的空单开仓单：用做空评分复查
            from entry_logic import calc_score_short as _css
            score, d = _css(sym, k1h, k4h, k15m, tk, funding)
            threshold = 75
            sig_type  = "short"
        else:
            sig_type  = classify_signal(k1h, k4h, funding, chg24)
            threshold = 65 if sig_type == "reversal" else 75
            if sig_type == "reversal":
                score, d = calc_score_reversal(sym, k1h, k4h, k15m, tk, funding)
            else:
                score, d = calc_score_trend(sym, k1h, k4h, k15m, tk, funding)

        dist_pct = (op_price - mark) / mark * 100  # 挂单价距现价百分比

        # ── 资金费率结算窗口：窗口内暂停评估（保留挂单，不撤）──
        blocked_w, block_reason_w = check_funding_settlement_window(funding)
        if blocked_w:
            reports.append(f"⏰ {sym} 挂单暂停评估({block_reason_w})，挂单保留")
            continue

        # ── 判断是否需要撤单 ──
        cancel_reason = ""
        # 0. 超时清理：挂单超过 8 小时未成交，自动撤销
        order_age_h = (time.time() * 1000 - po.get("time", time.time()*1000)) / 3_600_000
        if order_age_h >= 8.0:
            cancel_reason = f"挂单超过8小时未成交({order_age_h:.1f}h)，超时清理"
        # 1. 评分大幅下滑（跌破门槛的70%）
        elif score < threshold * 0.70:
            cancel_reason = f"评分下滑至{score}/{threshold}，低于门槛70%"
        # 2a. 多单趋势反转：1H+4H 双空排
        elif sig_type == "trend" and d.get("ema_1h") == "空排" and d.get("ema_4h") == "空排":
            cancel_reason = f"趋势单但1H+4H双空排，趋势反转"
        # 2b. 空单趋势反转：1H+4H 双多排（价格大涨，做空逻辑失效）
        elif is_short_order and d.get("ema_1h") == "多排" and d.get("ema_4h") == "多排":
            cancel_reason = f"空单1H+4H双多排，做空信号失效"
        # 3. 量能极度萎缩（量比<0.3x 且 挂单价距现价超过-8%，回踩无望）
        elif d.get("vr", 1) < 0.3 and dist_pct < -8:
            cancel_reason = f"量比{d['vr']:.2f}x 极弱且挂单价距现价{dist_pct:.1f}%，回踩无望"
        # 4. 资金费率转正（逆势单失去逼空逻辑）
        elif sig_type == "reversal" and funding > 0.1:
            cancel_reason = f"逆势单资金费率转正({funding:.3f}%)，逼空逻辑消失"

        # ── 动态调仓：偏离超15% 且评分仍达标 → 撤旧单重挂新位置 ──
        REPIN_DIST = -15.0  # 偏离阈值（挂单价低于现价15%以上）
        repin = (
            not cancel_reason
            and not is_short_order         # SHORT 单不做 repin，避免错误调仓
            and dist_pct < REPIN_DIST
            and score >= threshold
            and not check_funding_settlement_window(funding)[0]
        )

        if cancel_reason:
            api.cancel_order(sym, oid)
            ps = state["positions"].get(sym, {})
            if ps and not ps.get("pending_order_id"):
                # 纯挂单（未成交），清理 state
                del state["positions"][sym]
            save_state()
            reports.append(
                f"🗑️ 撤单 {sym} orderId={oid} | {cancel_reason}\n"
                f"   挂单价={op_price} 现价={mark:.4f} 评分={score}/{threshold} 量比={d.get('vr',0):.2f}x"
            )
        elif repin:
            # 撤旧单，重新计算清算地图，在新支撑位重挂
            api.cancel_order(sym, oid)
            try:
                lmap_new = liquidation_map(sym, k1h, k4h, "LONG")
                tick_r, step_r = get_sym_info(sym)
                _, lp_new, _, _ = calc_entry_mode(k1h, "LONG", lmap_new, 1.0, final_score=score)
                new_price_str = fmt_price(lp_new, tick_r)
                new_qty_str   = fmt_qty(FIRST_SIZE / float(new_price_str), step_r)

                # 检查新价格是否比旧价格更合理（必须在现价以下）
                if float(new_price_str) < mark * 0.995:
                    # 优先用 Coinglass 计算 SL/TP，失败时 fallback 到 lmap_new
                    _atr_rp = atr_calc(k1h)
                    _sl_rp, _tp1_rp, _tp2_rp, _cg_src_rp = get_sl_tp_from_coinglass(
                        sym, 'LONG', float(new_price_str), float(new_price_str), _atr_rp, lmap_new, k1h=k1h
                    )
                    r_new = api.place_order(sym, "BUY", "LIMIT",
                        quantity=new_qty_str, price=new_price_str,
                        timeInForce="GTC", positionSide="LONG")
                    new_oid = r_new.get("orderId") if isinstance(r_new, dict) else None
                    if new_oid:
                        # 更新 state
                        if sym not in state["positions"]:
                            state["positions"][sym] = {}
                        state["positions"][sym].update({
                            "entry_price":      float(new_price_str),
                            "sl_price":         _sl_rp,
                            "tp_price":         _tp1_rp,
                            "tp2_price":        _tp2_rp,
                            "tp3_price":        _tp2_rp * 1.03,
                            "pending_order_id": new_oid,
                            "opened_at":        int(time.time()),
                        })
                        save_state()
                        reports.append(
                            f"🔄 动态调仓 {sym} | 旧挂单{op_price}偏离{dist_pct:.1f}% → "
                            f"撤旧挂新位置={new_price_str} (新orderId={new_oid})"
                        )
                    else:
                        reports.append(f"⚠️ {sym} 动态调仓重挂失败: {r_new}")
                else:
                    reports.append(
                        f"⏭️ {sym} 偏离{dist_pct:.1f}%但新计算价{new_price_str}≥现价，跳过重挂"
                    )
            except Exception as e_repin:
                reports.append(f"⚠️ {sym} 动态调仓异常: {e_repin}")
        else:
            # 评估通过，正常维持
            keep_reason = f"评分{score}/{threshold} {'仍达标' if score >= threshold else '尚可，继续等待'}"
            reports.append(
                f"📌 挂单维持 {sym} | 限价={op_price} 现价={mark:.4f} ({dist_pct:+.1f}%) | "
                f"{keep_reason} | RSI 1H={d['rsi_1h']:.0f} 4H={d['rsi_4h']:.0f} 量比={d.get('vr',0):.2f}x "
                f"EMA={d.get('ema_1h','?')}/{d.get('ema_4h','?')} 费率={funding:.3f}%"
            )
    except Exception as e:
        reports.append(f"⚠️ {sym} 挂单评估异常: {e}")

# ═══════════════════════════════════════════════
# 第一步：维护已有仓位
# ═══════════════════════════════════════════════
positions   = api.get_positions()
# 自动化仅处理非手动仓位
# 双向持仓模式：用 (symbol, positionSide) 作为 key，避免同标的多空仓位互相覆盖
active_pos_full = {
    (p["symbol"], p.get("positionSide","BOTH")): p
    for p in positions
    if abs(float(p.get("positionAmt", 0))) > 0.0001
    and not is_manual(p["symbol"], p.get("positionSide","LONG"))
}
# 仅保留 LONG 仓位给多单维护逻辑（空单由做空体系单独维护）
active_pos = {
    sym: p
    for (sym, side), p in active_pos_full.items()
    if side == "LONG"
}

# ── pending 单成交检查：自动补挂止损/止盈 ──
for sym, ps in list(state["positions"].items()):
    # 被专属监控管理的标的跳过，止盈止损由专属监控处理
    if sym in _active_symbols:
        continue
    oid = ps.get("pending_order_id")
    if not oid: continue
    if sym in active_pos:  # 已有真实持仓，说明成交了
        o = api.request("GET", api.BASE_FAPI + "/fapi/v1/order", {"symbol": sym, "orderId": oid})
        if isinstance(o, dict) and o.get("status") == "FILLED":
            entry_price = float(o.get("avgPrice", ps["entry_price"]))
            state["positions"][sym]["entry_price"] = entry_price
            state["positions"][sym]["pending_order_id"] = None
            # 检查是否已有止损/止盈（必须查 algo 条件单接口，普通 openOrders 查不到）
            ex_algo = api.request("GET", api.BASE_FAPI + "/fapi/v1/openAlgoOrders")
            ex_algo_sym = [o2 for o2 in ex_algo if o2.get("symbol") == sym] if isinstance(ex_algo, list) else []
            ex_types = [o2.get("orderType") for o2 in ex_algo_sym]
            tick_s, step_s = get_sym_info(sym)
            qty_str = ps.get("qty", "0")
            _no_sl = cfg.get("no_sl_symbols", [])
            if sym not in _no_sl and "STOP_MARKET" not in ex_types and ps.get("sl_price"):
                sl_str = fmt_price(ps["sl_price"], tick_s)
                _sl_oid, r_sl = place_sl_order(sym, "SELL", int(float(qty_str)), float(sl_str), "LONG")
                if _sl_oid:
                    reports.append(f"🛡️ {sym} 成交后补挂止损={sl_str}")
            if "TAKE_PROFIT_MARKET" not in ex_types and ps.get("tp_price"):
                qty_f = float(qty_str)
                tp1_str = fmt_price(ps["tp_price"], tick_s)
                tp2_str = fmt_price(ps.get("tp2_price", ps["tp_price"] * 1.02), tick_s)
                tp3_str = fmt_price(ps.get("tp3_price", ps["tp_price"] * 1.04), tick_s)
                q1 = int(qty_f * 0.40); q2 = int(qty_f * 0.30); q3 = int(qty_f) - q1 - q2
                api.place_algo_order(sym, "SELL", "TAKE_PROFIT_MARKET", float(tp1_str), "LONG", quantity=q1)
                api.place_algo_order(sym, "SELL", "TAKE_PROFIT_MARKET", float(tp2_str), "LONG", quantity=q2)
                api.place_algo_order(sym, "SELL", "TAKE_PROFIT_MARKET", float(tp3_str), "LONG", quantity=q3)
                reports.append(f"🎯 {sym} 成交后补挂止盈40%@{tp1_str} 30%@{tp2_str} 30%@{tp3_str}")
            save_state()
            reports.append(f"✅ {sym} 限价单已成交 入场={entry_price:.4f}")

            # ── 成交后检查：若仓位已达 MAX_POS，撤销所有其他开仓挂单 ──
            try:
                pos_after = api.get_positions()
                active_after = [p for p in pos_after if abs(float(p.get("positionAmt", 0))) > 0.0001]
                if len(active_after) >= MAX_POS:
                    # 查所有 BUY LIMIT 挂单，撑除刚成交的那笔
                    all_open = api.request("GET", api.BASE_FAPI + "/fapi/v1/openOrders", {})
                    if isinstance(all_open, list):
                        _om_ids = _get_om_tp_ids()
                        cancel_orders = [
                            o for o in all_open
                            if o.get("side") == "BUY"
                            and o.get("type") == "LIMIT"
                            and o.get("symbol") != sym  # 不撤刚成交的
                            and str(o.get("orderId")) not in _om_ids  # 不撤 on_monitor 止盈单
                        ]
                        for co in cancel_orders:
                            api.cancel_order(co["symbol"], co["orderId"])
                            # 清理对应 state
                            if co["symbol"] in state["positions"] and state["positions"][co["symbol"]].get("pending_order_id") == co["orderId"]:
                                del state["positions"][co["symbol"]]
                        if cancel_orders:
                            save_state()
                            syms_cancelled = ", ".join(set(o["symbol"] for o in cancel_orders))
                            reports.append(f"🔒 仓位已达{MAX_POS}个，撤销 {len(cancel_orders)} 笔开仓挂单：{syms_cancelled}")
            except Exception as e_cap:
                reports.append(f"⚠️ 仓位满戒检查异常: {e_cap}")

            # ── 成交后立即执行移动止损检查，不等下一轮扫描 ──
            try:
                pos_now = api.request("GET", api.BASE_FAPI + "/fapi/v2/positionRisk", {"symbol": sym})
                p_now   = next((p for p in pos_now if abs(float(p.get("positionAmt",0))) > 0), None)
                if p_now:
                    mark_now = float(p_now["markPrice"])
                    pct_now  = (mark_now - entry_price) / entry_price * 100
                    k1h_now  = api.get_klines(sym, "1h", 20)
                    atr_now  = atr_calc([float(k[2]) for k in k1h_now],
                                       [float(k[3]) for k in k1h_now],
                                       [float(k[4]) for k in k1h_now]) if k1h_now else 0
                    cur_sl   = float(ps.get("sl_price", 0))
                    new_sl   = None
                    if pct_now >= 12.0 and cur_sl < entry_price + atr_now * 2.0:
                        new_sl = round(entry_price + atr_now * 2.0, 8)
                    elif pct_now >= 8.0 and cur_sl < entry_price + atr_now * 1.5:
                        new_sl = round(entry_price + atr_now * 1.5, 8)
                    elif pct_now >= 5.0 and cur_sl < entry_price + atr_now * 1.0:
                        new_sl = round(entry_price + atr_now * 1.0, 8)
                    elif pct_now >= 3.0 and cur_sl < entry_price:
                        new_sl = round(entry_price, 8)
                    if new_sl and sym not in cfg.get("no_sl_symbols", []):
                        tick_m, _ = get_sym_info(sym)
                        new_sl_str = fmt_price(new_sl, tick_m)
                        _msl_oid, r_msl = place_sl_order(sym, "SELL", int(float(qty_str)), float(new_sl_str), "LONG")
                        if _msl_oid:
                            state["positions"][sym]["sl_price"] = float(new_sl_str)
                            save_state()
                            reports.append(f"📈 {sym} 成交后立即移动止损 → {new_sl_str}（浮盈{pct_now:.1f}%）")
            except Exception as e_msl:
                reports.append(f"⚠️ {sym} 成交后移动止损异常: {e_msl}")
    else:  # 无持仓，检查挂单是否还在
        o = api.request("GET", api.BASE_FAPI + "/fapi/v1/order", {"symbol": sym, "orderId": oid})
        if isinstance(o, dict) and o.get("status") in ("CANCELED","EXPIRED","REJECTED"):
            del state["positions"][sym]
            save_state()
            reports.append(f"🗑️ {sym} 挂单已撤/过期，清理 state")

# 清理 state 中已平仓的记录，并撤销所有残留条件单
for sym in list(state["positions"].keys()):
    if sym not in active_pos:
        # 平仓后自动撤销该 symbol 所有未触发的条件单（止盈/止损）
        try:
            all_algo = api.request("GET", api.BASE_FAPI + "/fapi/v1/openAlgoOrders")
            if isinstance(all_algo, list):
                sym_algos = [o for o in all_algo if o["symbol"] == sym]
                for o in sym_algos:
                    api.cancel_algo_order(sym, o["algoId"])
                if sym_algos:
                    reports.append(f"🗑️ {sym} 平仓后撤销 {len(sym_algos)} 笔残留条件单")
        except Exception as e_clean:
            reports.append(f"⚠️ {sym} 清理条件单异常: {e_clean}")
        del state["positions"][sym]
save_state()

for sym, pos in active_pos.items():
    ps = state["positions"].get(sym, {})
    entry   = float(pos["entryPrice"])
    qty     = abs(float(pos["positionAmt"]))
    mark_p  = float(pos["markPrice"])
    pnl     = float(pos.get("unRealizedProfit", 0))
    _side   = 1 if float(pos.get("positionAmt",0)) > 0 else -1
    pct     = _side * (mark_p - entry) / entry * 100 if entry > 0 else 0
    sig_type= ps.get("sig_type", "trend")
    sl_price= ps.get("sl_price", 0)
    tp_price= ps.get("tp_price", 0)
    addon_count = ps.get("addon_count", 0)
    addon_last  = ps.get("addon_last_ts", 0)

    # 拉指标（并行）
    try:
        tk_pos, k1h, k4h, _, fr_pos = _fetch_sym_data(sym)
        c1h = [float(k[4]) for k in k1h]
        c4h = [float(k[4]) for k in k4h]
        rsi_1h = rsi_calc(c1h)
        rsi_4h = rsi_calc(c4h)
        atr    = atr_calc(k1h)
        ema20_1h = ema_calc(c1h[-20:], 20)
        ema50_1h = ema_calc(c1h[-50:], 50) if len(c1h)>=50 else ema_calc(c1h, len(c1h))
        ema20_4h = ema_calc(c4h[-20:], 20) if len(c4h)>=20 else ema_calc(c4h, len(c4h))
        ema50_4h = ema_calc(c4h[-50:], 50) if len(c4h)>=50 else ema_calc(c4h, len(c4h))
        trend_1h = "多排" if ema20_1h > ema50_1h else "空排"
        trend_4h = "多排" if ema20_4h > ema50_4h else "空排"
        v1h  = [float(k[5]) for k in k1h]
        vb   = sum(v1h[-10:-3])/7 if len(v1h)>=10 else sum(v1h)/len(v1h)
        vr   = (sum(v1h[-3:])/3)/vb if vb>0 else 1
        fr_r = requests.get(f"{BASE_F}/fapi/v1/premiumIndex", params={"symbol":sym}, timeout=8, proxies=PROXY).json()
        funding = float(fr_r.get("lastFundingRate", 0))*100
    except Exception as e:
        reports.append(f"⚠️ {sym} 指标拉取失败: {e}")
        continue

    tick, step_size = get_sym_info(sym)
    qty_str = fmt_qty(qty, step_size) if step_size else str(qty)
    actions = []

    # ── 提前平仓判断 ──
    # 阈值用 ATR 动态换算，不同波动率币种有自己的尺度
    atr_pct_m = atr / entry * 100 if entry > 0 else 1.0
    EXIT_TREND_M = max(0.5, atr_pct_m * 0.5)   # 趋势反转保护：至少盈利 0.5 ATR
    EXIT_RSI_M   = max(1.0, atr_pct_m * 1.0)   # RSI 极端保护：至少盈利 1.0 ATR
    exit_now = False
    exit_reason = ""
    # 1H+4H 双空排 且 已有盈利 → 止盈保护
    if trend_1h == "空排" and trend_4h == "空排" and pct > EXIT_TREND_M:
        exit_now = True; exit_reason = f"1H+4H 双空排转势，保护利润平仓(阈值={EXIT_TREND_M:.2f}%)"
    # RSI 极度超买
    elif rsi_1h > 82 and pct > EXIT_RSI_M:
        exit_now = True; exit_reason = f"1H RSI={rsi_1h:.0f} 极度超买，止盈(阈值={EXIT_RSI_M:.2f}%)"
    # 浮亏超过 止损价 → 补止损
    elif sl_price > 0 and mark_p <= sl_price * 1.002:
        actions.append(f"⚠️ 接近止损位 {sl_price}，市场已触发或即将触发")

    if exit_now and pct > 0:
        # 市价平仓
        close_side = "SELL"
        r_close = api.place_order(sym, close_side, "MARKET",
            quantity=qty_str, positionSide="LONG", reduceOnly="true")
        cancel_all_orders(sym)
        del state["positions"][sym]
        save_state()
        reports.append(f"🔴 {sym} 提前平仓 | {exit_reason} | 盈亏={pnl:+.2f}U ({pct:+.1f}%)")
        continue

    # ── 移动止损 ──
    # no_sl_symbols 列表里的币种，用户主动选择不设置止损，跳过所有止损逻辑
    _no_sl_list = cfg.get('no_sl_symbols', [])
    if sym in _no_sl_list:
        pass  # 跳过移动止损
    else:
        # 移动止损：触发阈值全部用 ATR 倍数动态化（atr_pct_m 已在上方计算）
        # reversal 信号保本阈值更宽松（0.3 ATR 即保本），trend 信号需要 1.0 ATR
        be_mult = 0.3 if sig_type == "reversal" else 1.0
        T1_m = max(0.3, atr_pct_m * be_mult)   # 保本触发
        T2_m = max(2.0, atr_pct_m * 1.5)
        T3_m = max(4.0, atr_pct_m * 2.5)
        T4_m = max(6.0, atr_pct_m * 4.0)
        new_sl = None
        if pct >= T4_m and sl_price < entry + atr*2.0:
            new_sl = round(entry + atr*2.0, 8)
        elif pct >= T3_m and sl_price < entry + atr*1.5:
            new_sl = round(entry + atr*1.5, 8)
        elif pct >= T2_m and sl_price < entry + atr*1.0:
            new_sl = round(entry + atr*1.0, 8)
        elif pct >= T1_m and sl_price < entry:
            new_sl = round(entry, 8)

        if new_sl and tick:
            new_sl_str = fmt_price(new_sl, tick)
            # 新止损和当前止损相差小于 1 tick 则不更新，避免浮点误差反复触发
            if abs(new_sl - sl_price) < float(tick) * 2:
                pass  # 无实质变化，跳过
            else:
                # 幂等检查：先查当前 algo 单，若止损价已经是目标价则跳过，避免重复挂单
                _skip_trail = False
                try:
                    _cur_algo = api.request("GET", api.BASE_FAPI + "/fapi/v1/openAlgoOrders")
                    if isinstance(_cur_algo, list):
                        _cur_sl = next((float(o.get("triggerPrice", 0)) for o in _cur_algo
                                        if o.get("symbol") == sym and o.get("orderType") == "STOP_MARKET"), None)
                        if _cur_sl is not None and abs(_cur_sl - float(new_sl_str)) < float(tick) * 2:
                            _skip_trail = True  # 止损价未实质变化，跳过
                except Exception:
                    pass
                if not _skip_trail:
                    tp2_price_val = float(ps.get("tp2_price", 0) or 0)
                    tp3_price_val = float(ps.get("tp3_price", 0) or 0)
                    cancel_all_orders(sym)
                    sl_ok, tp_ok = place_sl_tp(
                        sym, float(new_sl_str), tp_price, qty_str,
                        tp2_price=tp2_price_val, tp3_price=tp3_price_val
                    )
                    if sl_ok:
                        state["positions"][sym]["sl_price"] = float(new_sl_str)
                        save_state()
                        actions.append(f"📈 移动止损 → {new_sl_str}（浮盈{pct:.1f}%）")

    # ── 补仓判断 ──
    now_ts = int(time.time())

    # ── C1 硬性计数器验证：读取 add_position_counter.json，最多2次补仓，间隔≥30分钟 ──
    COUNTER_FILE = "/root/.openclaw/workspace/add_position_counter.json"
    _counter_key = f"{sym}_LONG"
    try:
        with open(COUNTER_FILE) as _cf:
            _counter = json.load(_cf)
    except Exception:
        _counter = {}

    _sym_count = _counter.get(_counter_key, {}).get('count', 0)
    _last_ts_counter = _counter.get(_counter_key, {}).get('last_ts', 0)

    if _sym_count >= 2:
        actions.append(f"❌ {sym} 补仓次数已达上限({_sym_count}/2)，硬性拒绝补仓")
    elif now_ts - _last_ts_counter < 1800:
        _wait_min = int((1800 - (now_ts - _last_ts_counter)) / 60)
        actions.append(f"❌ {sym} 补仓间隔不足30分钟（还需等约{_wait_min}分钟），跳过")

    # 规则1：触发阈值从 -3% 提高到 -8%，避免在下跌途中过早补仓
    addon_loss_thresh = -8.0

    addon_eligible = (
        _sym_count < 2 and                          # 硬性计数器限制（2次上限）
        (now_ts - _last_ts_counter) >= 1800 and     # 硬性间隔检查（30分钟）
        addon_count < ADDON_MAX and
        pct <= addon_loss_thresh and
        (now_ts - addon_last) >= ADDON_INTERVAL * 60 and
        trend_1h == "多排"  # 1H EMA 仍多排，说明结构未完全破坏
    )
    if addon_eligible:
        lmap_addon = liquidation_map(sym, k1h, k4h, "LONG")
        _, lp_addon, _, _ = calc_entry_mode(k1h, "LONG", lmap_addon, 1.0)
        if tick:
            lp_str  = fmt_price(lp_addon, tick)
            lp_val  = float(lp_str)

            # 规则2：补仓价必须比当前止损价高出至少 1.5 ATR
            # 防止补仓价和止损太近，补仓还没站稳就被同一个止损扫掉
            atr_list = [max(float(k1h[i][2])-float(k1h[i][3]),
                            abs(float(k1h[i][2])-float(k1h[i-1][4])),
                            abs(float(k1h[i][3])-float(k1h[i-1][4])))
                        for i in range(1, len(k1h))]
            atr_val = sum(atr_list[-14:]) / 14
            sl_cur  = float(ps.get("sl_price", 0) or 0)

            if sl_cur > 0 and (lp_val - sl_cur) < atr_val * 1.5:
                actions.append(
                    f"⚠️ 补仓取消：补仓价 {lp_str} 距止损 {sl_cur} 仅 "
                    f"{lp_val - sl_cur:.2f}（需 > {atr_val*1.5:.2f} = 1.5ATR）"
                )
            else:
                # 规则3：补仓后止损以两笔均价重新计算（均价 - 1.5 ATR）
                addon_qty_str = fmt_qty(FIRST_SIZE / lp_val, step_size)
                addon_qty_f   = float(addon_qty_str)
                cur_qty_f     = abs(float(next(
                    (p["positionAmt"] for p in api.get_positions() if p["symbol"] == sym), 0)))
                avg_entry = (entry * cur_qty_f + lp_val * addon_qty_f) / (cur_qty_f + addon_qty_f)
                new_sl    = avg_entry - atr_val * 1.5
                new_sl_str = fmt_price(new_sl, tick)

                r_addon = api.place_order(sym, "BUY", "LIMIT",
                    quantity=addon_qty_str, price=lp_str,
                    timeInForce="GTC", positionSide="LONG")
                if isinstance(r_addon, dict) and r_addon.get("orderId"):
                    if sym not in state["positions"]:
                        state["positions"][sym] = {}
                    state["positions"][sym]["addon_count"]   = addon_count + 1
                    state["positions"][sym]["addon_last_ts"] = now_ts
                    state["positions"][sym]["sl_price"]      = float(new_sl_str)
                    save_state()
                    # ── C1：补仓成功后更新硬性计数器 ──
                    try:
                        _counter[_counter_key] = {
                            'count':      _sym_count + 1,
                            'last_ts':    now_ts,
                            'add_price':  lp_val,
                            'add_qty':    float(addon_qty_str),
                            'last_add_at': datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
                        }
                        with open(COUNTER_FILE, 'w') as _cf:
                            json.dump(_counter, _cf, ensure_ascii=False, indent=2)
                    except Exception as _ce:
                        reports.append(f"⚠️ {sym} 计数器更新失败: {_ce}")
                    # 同步更新止损单（撤旧止损，挂新均价止损）
                    cancel_all_orders(sym)
                    place_sl_order(sym, "SELL", int(cur_qty_f + addon_qty_f), float(new_sl_str), "LONG")
                    actions.append(
                        f"➕ 补仓挂单 {lp_str}×{addon_qty_str} (第{addon_count+1}次) "
                        f"均价={avg_entry:.2f} 新止损={new_sl_str}"
                    )

    # ── 汇报：从交易所实时拉取实际 algo 单，确保播报与挂单一致 ──
    try:
        _live_algo = api.request("GET", api.BASE_FAPI + "/fapi/v1/openAlgoOrders")
        _live_sym  = [o for o in _live_algo if o.get("symbol") == sym] if isinstance(_live_algo, list) else []
        _live_sl  = next((float(o["triggerPrice"]) for o in _live_sym if o["orderType"] == "STOP_MARKET"), None)
        _live_tps = sorted([float(o["triggerPrice"]) for o in _live_sym if o["orderType"] == "TAKE_PROFIT_MARKET"])
        _live_tp  = _live_tps[0] if _live_tps else None
        _sl_show  = _live_sl  if _live_sl  is not None else (sl_price  if sl_price  else None)
        _tp_show  = _live_tp  if _live_tp  is not None else (tp_price  if tp_price  else None)
    except Exception:
        _sl_show = sl_price if sl_price else None
        _tp_show = tp_price if tp_price else None

    line = (f"📊 {sym} [{sig_type}] 入场={entry:.4f} 现价={mark_p:.4f} "
            f"浮盈={pnl:+.2f}U ({pct:+.1f}%) | "
            f"SL={_sl_show if _sl_show else '未设'} TP={_tp_show if _tp_show else '未设'} | "
            f"EMA 1H={trend_1h} 4H={trend_4h} RSI={rsi_1h:.0f} 费率={funding:.3f}%")
    reports.append(line)
    for a in actions:
        reports.append(f"   {a}")

# ═══════════════════════════════════════════════
# 第二步：扫描新信号 ← 已禁用
# 扫描职责已统一由 scanner.py（常驻进程，15分钟轮询）负责
# position_manager 仅做仓位维护，不重复扫描开仓
# 消除双扫描冲突：scanner + position_manager 各司其职
# ═══════════════════════════════════════════════
slot_left = 0  # 禁用扫描，不开新仓
if reports:
    ts = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    print(f"📡 Position Manager | {ts}")
    print()
    for r in reports:
        print(r)
else:
    print("HEARTBEAT_OK")


# ═══════════════════════════════════════════════════════
# 做空体系：独立状态管理 + 扫描 + 开仓 + 仓位维护
# ═══════════════════════════════════════════════════════

def load_short_state():
    if os.path.exists(SHORT_STATE_FILE):
        with open(SHORT_STATE_FILE) as f:
            return json.load(f)
    return {"positions": {}, "watchlist": {}}

def save_short_state(s):
    with open(SHORT_STATE_FILE, "w") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)

def load_short_circuit():
    if os.path.exists(SHORT_CIRCUIT_FILE):
        with open(SHORT_CIRCUIT_FILE) as f:
            return json.load(f)
    return {"triggered": False, "daily_loss": 0.0, "reset_date": ""}

def save_short_circuit(c):
    with open(SHORT_CIRCUIT_FILE, "w") as f:
        json.dump(c, f, ensure_ascii=False, indent=2)

def check_short_forbidden(sym, k1h, k4h, funding, chg24, lmap):
    """
    做空一票否决检查（含BTC趋势/流动性/资金费率分段）
    返回 (forbidden: bool, reason: str)
    """
    c1h = [float(k[4]) for k in k1h]
    price = c1h[-1]

    # 1. 资金费率分段过滤（< -0.3% 极度偏空，禁空）
    if funding < -0.3:
        return True, f"资金费率={funding:.3f}%（< -0.3%，空头严重堆积，禁空）"

    # 2. 距近期低点 < 5%（空间不足）
    lows_20 = [float(k[3]) for k in k1h[-20:]]
    recent_low = min(lows_20)
    if (price - recent_low) / recent_low * 100 < 5:
        return True, f"距近期低点{(price-recent_low)/recent_low*100:.1f}% < 5%，空间不足"

    # 3. BTC趋势过滤：按币种分类差异化
    #    major: 必须 BTC 全空排才允许做空
    #    altcoin/volatile_alt: BTC 不是全多排即可，横盘/震荡允许做空
    try:
        btc_k4h = requests.get(f"{BASE_F}/fapi/v1/klines",
            params={"symbol":"BTCUSDT","interval":"4h","limit":60},
            proxies=PROXY, timeout=8).json()
        btc_c4h = [float(k[4]) for k in btc_k4h]
        btc_e9  = ema_calc(btc_c4h[-9:],  9)
        btc_e20 = ema_calc(btc_c4h[-20:], 20)
        btc_e50 = ema_calc(btc_c4h[-50:], 50) if len(btc_c4h) >= 50 else ema_calc(btc_c4h, len(btc_c4h))
        btc_price = btc_c4h[-1]
        btc_bear = btc_e9 < btc_e20 < btc_e50 and btc_price < btc_e20
        btc_bull = btc_e9 > btc_e20 > btc_e50 and btc_price > btc_e20

        # 计算 ATR 用于分类（复用 k1h）
        c1h_p = [float(k[4]) for k in k1h]
        atr_raw = sum(max(float(k1h[i][2])-float(k1h[i][3]),
                          abs(float(k1h[i][2])-c1h_p[i-1]),
                          abs(float(k1h[i][3])-c1h_p[i-1]))
                      for i in range(1, len(k1h))) / (len(k1h)-1)
        atr_pct_val = atr_raw / price * 100 if price > 0 else 0
        sym_class = api.classify_symbol(sym, 0, atr_pct_val)

        if sym_class == "major":
            # 主流币严格：必须 BTC 全空排
            if btc_bull:
                return True, "BTC 4H 全多排，禁空（主流币）"
            if not btc_bear:
                return True, "BTC 方向不明，禁空（主流币）"
        else:
            # 山寨/高波动山寨：只要 BTC 不是全多排即可
            if btc_bull:
                return True, "BTC 4H 全多排，禁空（山寨币）"
            # 横盘/震荡允许做空，不做额外拦截
    except Exception: pass  # 静默异常

    # 4. 流动性过滤：盘口价差 > 1%
    try:
        depth = requests.get(f"{BASE_F}/fapi/v1/depth",
            params={"symbol": sym, "limit": 5}, proxies=PROXY, timeout=6).json()
        best_bid = float(depth["bids"][0][0]) if depth.get("bids") else 0
        best_ask = float(depth["asks"][0][0]) if depth.get("asks") else 0
        if best_bid > 0 and best_ask > 0:
            spread_pct = (best_ask - best_bid) / best_bid * 100
            if spread_pct > 1.0:
                return True, f"盘口价差{spread_pct:.2f}% > 1%，流动性不足"
    except Exception: pass  # 静默异常

    return False, ""

def run_short_position_maintenance(short_state, reports):
    """
    维护已有空单：移动止损、超48h强制平仓、平仓后清理条件单
    """
    pos_all = api.request("GET", api.BASE_FAPI + "/fapi/v2/positionRisk")
    active_shorts = {
        p["symbol"]: p for p in pos_all
        if float(p.get("positionAmt", 0)) < 0  # 空单 positionAmt < 0
    }

    for sym in list(short_state["positions"].keys()):
        ps = short_state["positions"][sym]

        # 已平仓 → 撤条件单 + 清 state
        if sym not in active_shorts:
            try:
                all_algo = api.request("GET", api.BASE_FAPI + "/fapi/v1/openAlgoOrders")
                if isinstance(all_algo, list):
                    sym_algos = [o for o in all_algo if o["symbol"] == sym]
                    for o in sym_algos:
                        api.cancel_algo_order(sym, o["algoId"])
                    if sym_algos:
                        reports.append(f"🗑️ {sym} 空单平仓后撤销 {len(sym_algos)} 笔残留条件单")
            except Exception: pass  # 静默异常
            del short_state["positions"][sym]
            continue

        p = active_shorts[sym]
        entry  = float(ps.get("entry_price", float(p["entryPrice"])))
        mark   = float(p["markPrice"])
        qty    = abs(float(p["positionAmt"]))
        pnl    = float(p["unRealizedProfit"])
        pct    = (entry - mark) / entry * 100  # 正数 = 盈利

        cur_sl = float(ps.get("sl_price", entry * (1 + SHORT_SL_PCT)))
        # TP fallback: ATR动态（下方 _atr_s 在新增的ATR计算块里）
        tp1    = float(ps.get("tp_price",  entry - entry * 0.06))
        tp2    = float(ps.get("tp2_price", entry - entry * 0.12))  # 初始值，实际由下方ATR块覆盖
        new_sl = None
        tick, step = get_sym_info(sym)

        # ── on_short: ATR 动态计算移动止损阈值 ──
        try:
            k1h_s = requests.get(f"{BASE_F}/fapi/v1/klines",
                params={"symbol": sym, "interval": "1h", "limit": 20}, timeout=8).json()
            _atr_s = sum(abs(float(k[2]) - float(k[3])) for k in k1h_s[-14:]) / 14
        except Exception:
            _atr_s = entry * 0.03   # fallback 3%
        _atr_pct_s = _atr_s / entry * 100 if entry > 0 else 1.0
        # 空单锁利档位：止损从上方往下追（值变小）
        # T_BE=0.5ATR  T2=1.5ATR  T3=2.5ATR  T4=4ATR
        T_BE_s = max(0.5, _atr_pct_s * 0.5)
        T2_s   = max(2.0, _atr_pct_s * 1.5)
        T3_s   = max(4.0, _atr_pct_s * 2.5)
        T4_s   = max(6.0, _atr_pct_s * 4.0)

        # ── 止盈触发后止损上移 ──
        tp1_triggered = ps.get("tp1_triggered", False)
        tp2_triggered = ps.get("tp2_triggered", False)
        if not tp1_triggered and mark <= tp1:
            short_state["positions"][sym]["tp1_triggered"] = True
            new_sl = entry
            reports.append(f"🎯 {sym} 第一档止盈触发，止损移至保本价{fmt_price(entry, tick)}")
        elif tp1_triggered and not tp2_triggered and mark <= tp2:
            short_state["positions"][sym]["tp2_triggered"] = True
            new_sl = tp1
            reports.append(f"🎯 {sym} 第二档止盈触发，止损移至{fmt_price(tp1, tick)}")

        # ── 动态止损上移（止盈未触发时，用 ATR 倍数动态阈值）──
        if not tp1_triggered:
            if pct >= T4_s and cur_sl > entry - _atr_s * 3.0:
                new_sl = entry - _atr_s * 3.0   # 大幅盈利：止损追到 -3ATR
            elif pct >= T3_s and cur_sl > entry - _atr_s * 2.0:
                new_sl = entry - _atr_s * 2.0
            elif pct >= T2_s and cur_sl > entry - _atr_s * 1.0:
                new_sl = entry - _atr_s * 1.0
            elif pct >= T_BE_s and cur_sl > entry:
                new_sl = entry   # 保本

        if new_sl and new_sl < cur_sl:
            new_sl_str = fmt_price(new_sl, tick)
            _msl_oid, r_msl = place_sl_order(sym, "BUY", int(qty), float(new_sl_str), "SHORT")
            if _msl_oid:
                short_state["positions"][sym]["sl_price"] = float(new_sl_str)
                reports.append(f"📈 {sym} 空单止损上移 → {new_sl_str}（浮盈{pct:.1f}%）")

        # ── 轧空保护 ──
        squeeze_reason = None
        if pct < -4:
            squeeze_reason = f"浮亏{abs(pct):.1f}% > 4%，价格持续上涨，轧空风险"
        if not squeeze_reason:
            try:
                oi_hist = api.request("GET", api.BASE_FAPI + "/futures/data/openInterestHist",
                    {"symbol": sym, "period": "5m", "limit": 3})
                if isinstance(oi_hist, list) and len(oi_hist) >= 2:
                    oi_old = float(oi_hist[0]["sumOpenInterest"])
                    oi_new = float(oi_hist[-1]["sumOpenInterest"])
                    oi_chg = (oi_new - oi_old) / oi_old * 100 if oi_old > 0 else 0
                    if oi_chg > 15 and mark >= entry * 0.99:
                        squeeze_reason = f"OI 10分钟暴增{oi_chg:.1f}%且价格上涨，多头涌入"
            except Exception: pass  # 静默异常
        if not squeeze_reason:
            try:
                funding_now = float(api.request("GET", api.BASE_FAPI + "/fapi/v1/premiumIndex",
                    {"symbol": sym}).get("lastFundingRate", 0)) * 100
                entry_funding = ps.get("entry_funding", 0)
                if entry_funding > 0 and funding_now < -0.1:
                    squeeze_reason = f"资金费率从{entry_funding:.3f}%极速转负{funding_now:.3f}%，情绪逆转"
            except Exception: pass  # 静默异常

        if squeeze_reason:
            r_sq = api.place_order(sym, "BUY", "MARKET",
                quantity=str(int(qty)), positionSide="SHORT", reduceOnly="true")
            if isinstance(r_sq, dict) and r_sq.get("orderId"):
                if "cooldown" not in short_state: short_state["cooldown"] = {}
                short_state["cooldown"][sym] = time.time() + 7200
                reports.append(f"🔴 {sym} 轧空保护触发！{squeeze_reason}，市价平仓 PNL={pnl:.2f}U，2h内禁止重开空")
            continue

        # ── 超48h未达第一档止盈 → 强制平仓 ──
        opened_at = ps.get("opened_at", time.time())
        age_h = (time.time() - opened_at) / 3600
        if age_h > 48 and pct < 5:
            r_close = api.place_order(sym, "BUY", "MARKET",
                quantity=str(int(qty)), positionSide="SHORT", reduceOnly="true")
            if isinstance(r_close, dict) and r_close.get("orderId"):
                # 1h冷却
                if "cooldown" not in short_state: short_state["cooldown"] = {}
                short_state["cooldown"][sym] = time.time() + 3600
                reports.append(f"⏰ {sym} 空单超48h未达止盈，强制平仓 PNL={pnl:.2f}U")

    save_short_state(short_state)
    return active_shorts

def run_short_scan(short_state, active_shorts, reports):
    """
    扫描做空机会，独立于做多扫描
    """
    def get_klines(sym, interval, limit=100):
        r = requests.get(f"{BASE_F}/fapi/v1/klines",
            params={"symbol": sym, "interval": interval, "limit": limit},
            proxies=PROXY, timeout=12)
        return r.json()
    # 熔断检查
    circuit = load_short_circuit()
    today_str = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    if circuit.get("reset_date") != today_str:
        circuit = {"triggered": False, "daily_loss": 0.0, "reset_date": today_str}
        save_short_circuit(circuit)
    if circuit.get("triggered"):
        reports.append(f"🔒 做空熔断已触发（今日空单累计亏损≥{SHORT_CIRCUIT_LOSS}U），停止做空")
        return

    # 计算今日空单 realized pnl
    try:
        income = api.request("GET", api.BASE_FAPI + "/fapi/v1/income", {
            "incomeType": "REALIZED_PNL",
            "startTime": int(datetime.datetime.utcnow().replace(
                hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
        })
        short_syms = list(short_state["positions"].keys())
        daily_short_loss = sum(
            float(i["income"]) for i in (income or [])
            if i.get("symbol") in short_syms and float(i["income"]) < 0
        )
        if abs(daily_short_loss) >= SHORT_CIRCUIT_LOSS:
            circuit["triggered"] = True
            circuit["daily_loss"] = daily_short_loss
            save_short_circuit(circuit)
            reports.append(f"🔒 做空熔断触发！今日空单亏损 {daily_short_loss:.2f}U ≥ {SHORT_CIRCUIT_LOSS}U")
            return
    except Exception: pass  # 静默异常

    # 已有空单 ≥ 1，不再开新空
    if len(active_shorts) >= MAX_SHORT_POS:
        reports.append(f"🔒 已有 {len(active_shorts)} 个空单（上限 {MAX_SHORT_POS}），暂停开空扫描")
        return

    # 资金费率结算窗口检查
    fr_block, fr_reason = check_funding_settlement_window()
    if fr_block:
        reports.append(f"⏸️ 做空跳过：{fr_reason}")
        return

    # ── 恐慌贪婪指数过滤 ──
    fg_index = 50  # 默认中性
    fg_penalty = 0
    try:
        fg_r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=5, proxies=PROXY).json()
        fg_index = int(fg_r["data"][0]["value"])
        if fg_index < 20:
            reports.append(f"🔴 恐慌贪婪指数={fg_index}（极度恐慌），禁止做空")
            return
        elif fg_index <= 45:
            fg_penalty = 10
            reports.append(f"⚠️ 恐慌贪婪指数={fg_index}（恐慌区间），做空评分-10")
    except Exception: pass  # 静默异常

    # ── 时段降级处理 ──
    now_utc = datetime.datetime.utcnow()
    hour_utc = now_utc.hour
    minute_utc = now_utc.minute
    time_penalty = 0
    # 北京时21:15~22:30 = UTC 13:15~14:30
    if (hour_utc == 13 and minute_utc >= 15) or (hour_utc == 14 and minute_utc <= 30):
        time_penalty = 10
        reports.append(f"⏰ 美股开盘时段(UTC {hour_utc}:{minute_utc:02d})，做空评分-10")
    # UTC 02:00~06:00 低流动性
    elif 2 <= hour_utc < 6:
        time_penalty = 10
        reports.append(f"⏰ 亚洲低流动性时段(UTC {hour_utc}时)，做空评分-10")

    total_penalty = fg_penalty + time_penalty

    # ── 1H 冷却期检查（止损出局后同币种1h内禁新建空单）──
    cooldown_syms = short_state.get("cooldown", {})
    now_ts = time.time()
    # 清理过期冷却
    cooldown_syms = {s: ts for s, ts in cooldown_syms.items() if now_ts - ts < 3600}
    short_state["cooldown"] = cooldown_syms

    # ── 拉全市场 ticker，筛出近期大涨且成交量充足的标的 ──
    ticker_all = requests.get(f"{BASE_F}/fapi/v1/ticker/24hr", proxies=PROXY, timeout=15).json()
    ticker_map = {t["symbol"]: t for t in ticker_all}
    candidates = [
        t for t in ticker_all
        if t["symbol"].endswith("USDT")
        and float(t.get("quoteVolume", 0)) > 3_000_000   # 最低 3M成交量
        and int(t.get("count", 99999)) >= 2000
        and float(t.get("priceChangePercent", 0)) > 8
    ]
    candidates.sort(key=lambda x: float(x["priceChangePercent"]), reverse=True)
    candidates = candidates[:10]

    reports.append(f"🔍 做空扫描候选池: {len(candidates)} 个")

    for t in candidates:
        sym = t["symbol"]
        if sym in active_shorts:
            continue

        # ── 反向仓位检查：同标的已有 LONG 仓位，禁止开 SHORT ──
        _long_pos = [p for p in api.get_positions()
                     if p.get('symbol')==sym
                     and p.get('positionSide')=='LONG'
                     and abs(float(p.get('positionAmt',0))) > 0]
        if _long_pos:
            reports.append(f"🚫 {sym} 已有多单仓位，跳过开空，防止双向持仓")
            continue

        # ── 最终守卫：pre_open_guard 五项检查 ──
        _guard_ok, _guard_reason = check_before_open(sym, "SHORT", notional=500)
        if not _guard_ok:
            reports.append(f"🛡️ {sym} 守卫拦截(空单): {_guard_reason}")
            continue

        # 冷却期检查
        if sym in cooldown_syms:
            continue

        # 已有同向空单挂单
        open_orders = api.request("GET", api.BASE_FAPI + "/fapi/v1/openOrders")
        if any(o.get("side") == "SELL" and o.get("positionSide") == "SHORT" and o["symbol"] == sym
               for o in open_orders):
            continue

        try:
            k1h  = get_klines(sym, "1h",  100)
            k4h  = get_klines(sym, "4h",  100)
            k15m = get_klines(sym, "15m", 100)

            chg24   = float(t.get("priceChangePercent", 0))
            funding = float(api.request("GET", api.BASE_FAPI + "/fapi/v1/premiumIndex",
                {"symbol": sym}).get("lastFundingRate", 0)) * 100

            # ── 资金费率分段附加分 ──
            funding_adj = 0
            if funding > 0.3:
                funding_adj = +5    # 多头过热，增强做空
            elif -0.1 <= funding <= 0:
                funding_adj = -5    # 轻度偏空，适当降分
            elif -0.3 <= funding < -0.1:
                funding_adj = -15   # 中度偏空，降分较大
            # < -0.3 已在 check_short_forbidden 禁空

            # Token report / 聪明钱（4秒超时保护）
            tr_score, tr_veto, tr_reason = 0, False, "跳过"
            try:
                import signal as _sig
                def _th(s, f): raise TimeoutError()
                _sig.signal(_sig.SIGALRM, _th); _sig.alarm(4)
                tr_score, tr_veto, tr_reason = get_token_report_score(sym)
                _sig.alarm(0)
            except Exception: pass  # 静默异常
            if tr_veto:
                reports.append(f"\U0001f6ab {sym} 做空一票否决: {tr_reason}")
                continue
            if tr_score > 0:
                reports.append(f"\u26a0\ufe0f {sym} 聪明钱正面信号({tr_reason})，禁止做空")
                continue

            # ── 做空评分 ──
            score, details = calc_score_short(sym, k1h, k4h, k15m, t, funding,
                                              token_report_score=tr_score)
            # 加入费率和时段附加分
            final_score = score + funding_adj - total_penalty
            effective_fire = SHORT_SCORE_FIRE + total_penalty
            effective_watch = SHORT_SCORE_WATCH + total_penalty

            if final_score < SHORT_SCORE_WATCH:
                continue

            # ── 多空信号冲突检查（同时触发做多和做空 → 静默跳过）──
            try:
                long_score, _ = calc_score_trend(sym, k1h, k4h, k15m, t, funding)
                if long_score >= 75:
                    continue  # 多空冲突，静默跳过
            except Exception: pass  # 静默异常

            # 一票否决检查
            lmap = liquidation_map(sym, k1h, k4h, "SHORT")
            forbidden, reason = check_short_forbidden(sym, k1h, k4h, funding, chg24, lmap)
            if forbidden:
                reports.append(f"🚫 {sym} 做空禁止: {reason} (评分={final_score})")
                continue

            # 入场方式
            entry_mode, limit_price = classify_short_entry(k1h, k15m, lmap, funding,
                                                        sym=sym, vol_24h=float(t.get("quoteVolume", 0) if isinstance(t, dict) else 0))
            if entry_mode == "WAIT":
                reports.append(f"⏳ {sym} 做空观察中 评分={final_score} 方向不明，等待")
                continue

            if final_score < effective_fire and entry_mode != "CHASE":
                reports.append(f"📋 {sym} 做空观察区 评分={final_score}/{effective_fire} 模式={entry_mode}，等候更强信号")
                continue

            # 计算数量
            tick, step = get_sym_info(sym)
            limit_str = fmt_price(limit_price, tick)
            qty_f = SHORT_NOTIONAL / float(limit_str)
            qty_str = fmt_qty(qty_f, step)

            # 止损价（入场 +6%）
            sl_price = limit_price * (1 + SHORT_SL_PCT)
            sl_str   = fmt_price(sl_price, tick)

            # 三档止盈（从 liquidation_map 下方支撑密集区）
            # TP fallback: 先用 lmap，若 lmap 也无则用 ATR 动态倍数（不用固定6/12%）
            try:
                _k1h_cf = api.get_klines(sym, '1h', 20)
                _atr_cf = sum(abs(float(k[2]) - float(k[3])) for k in _k1h_cf[-14:]) / 14
            except Exception:
                _atr_cf = limit_price * 0.03
            tp1 = lmap.get("liq_below",  round(limit_price - _atr_cf * 2.0, 8))
            tp2 = lmap.get("hard_sl",    round(limit_price - _atr_cf * 4.0, 8))
            tp3 = tp2 * 0.93                                   # -~19%
            tp1_str = fmt_price(tp1, tick)
            tp2_str = fmt_price(tp2, tick)
            tp3_str = fmt_price(tp3, tick)

            qty_tp1 = int(float(qty_str) * 0.40)
            qty_tp2 = int(float(qty_str) * 0.35)
            qty_tp3 = int(float(qty_str)) - qty_tp1 - qty_tp2

            # 下单（SELL LIMIT 开空）
            r_order = api.place_order(sym, "SELL", "LIMIT",
                quantity=qty_str, price=limit_str,
                timeInForce="GTC", positionSide="SHORT")

            if not isinstance(r_order, dict) or not r_order.get("orderId"):
                reports.append(f"❌ {sym} 做空下单失败: {r_order}")
                continue

            oid = r_order["orderId"]

            # 写入 short_state
            short_state["positions"][sym] = {
                "entry_price":      float(limit_str),
                "sl_price":         float(sl_str),
                "tp_price":         float(tp1_str),
                "tp2_price":        float(tp2_str),
                "tp3_price":        float(tp3_str),
                "qty":              qty_str,
                "sig_score":        score,
                "entry_mode":       entry_mode,
                "pending_order_id": oid,
                "opened_at":        int(time.time()),
                "atr":              atr_calc(k1h),
            }
            save_short_state(short_state)

            reports.append(
                f"📉 做空挂单 {sym} 评分={score} 模式={entry_mode}\n"
                f"   限价={limit_str} 止损={sl_str} "
                f"TP1={tp1_str}(40%) TP2={tp2_str}(35%) TP3={tp3_str}(25%)\n"
                f"   趋势:{details['ema_4h']}/{details['ema_1h']} "
                f"RSI:{details['rsi_4h']:.0f}/{details['rsi_1h']:.0f} "
                f"资金费率:{funding:.3f}% 量比:{details['vr']:.2f}x"
            )

        except Exception as e:
            reports.append(f"⚠️ {sym} 做空扫描异常: {e}")


def run_short_pending_check(short_state, reports):
    """
    检查空单 pending 挂单是否成交，成交后立即挂止损+三档止盈
    """
    open_orders = {str(o["orderId"]): o
                   for o in api.request("GET", api.BASE_FAPI + "/fapi/v1/openOrders")}

    for sym, ps in list(short_state["positions"].items()):
        oid = ps.get("pending_order_id")
        if not oid or str(oid) in open_orders:
            continue

        # 已成交
        entry_price = ps["entry_price"]
        qty_str     = ps["qty"]
        qty_f       = float(qty_str)
        tick, step  = get_sym_info(sym)

        sl_str  = fmt_price(ps["sl_price"], tick)
        tp1_str = fmt_price(ps["tp_price"], tick)
        tp2_str = fmt_price(ps["tp2_price"], tick)
        tp3_str = fmt_price(ps["tp3_price"], tick)
        qty_tp1 = int(qty_f * 0.40)
        qty_tp2 = int(qty_f * 0.35)
        qty_tp3 = int(qty_f) - qty_tp1 - qty_tp2

        # 挂止损
        place_sl_order(sym, "BUY", int(qty_f), float(sl_str), "SHORT")
        # 挂三档止盈
        api.place_algo_order(sym, "BUY", "TAKE_PROFIT_MARKET",
            float(tp1_str), "SHORT", quantity=qty_tp1)
        api.place_algo_order(sym, "BUY", "TAKE_PROFIT_MARKET",
            float(tp2_str), "SHORT", quantity=qty_tp2)
        api.place_algo_order(sym, "BUY", "TAKE_PROFIT_MARKET",
            float(tp3_str), "SHORT", quantity=qty_tp3)

        short_state["positions"][sym]["pending_order_id"] = None
        short_state["positions"][sym]["opened_at"] = int(time.time())
        save_short_state(short_state)

        reports.append(
            f"✅ {sym} 空单成交 入场={entry_price:.4f}\n"
            f"   止损={sl_str} TP1={tp1_str}(40%) TP2={tp2_str}(35%) TP3={tp3_str}(25%)"
        )

# 统一写盘（替代循环中多次 save_state）
_flush_state()

# ═══ 做空仓位维护（仅维护已有空单，扫描由 short_scanner.py 独立运行）═══
_short_reports = []
_short_state   = load_short_state()
# ── 裸仓兜底：真实空单持仓存在但 state 为空 → 自动补挂止损止盈 ──
def _repair_naked_short_positions(short_state, reports):
    """
    修复裸仓：有真实 SHORT 持仓但 short_state 里没有记录的情况。
    自动用 Coinglass + 近期高点规则计算止损止盈并补挂。
    """
    import requests as _req
    try:
        pos_all = api.request("GET", api.BASE_FAPI + "/fapi/v2/positionRisk")
        real_shorts = {
            p["symbol"]: p for p in pos_all
            if float(p.get("positionAmt", 0)) < 0
            and p["symbol"] not in _active_symbols  # 专属监控的不处理
        }
    except Exception:
        return

    for sym, p in real_shorts.items():
        # state 里有记录且不是纯 pending 状态就跳过
        ps = short_state["positions"].get(sym, {})
        if ps and ps.get("sl_price") and not ps.get("pending_order_id"):
            continue

        # 检查是否已经有止损单
        try:
            all_algo = api.request("GET", api.BASE_FAPI + "/fapi/v1/openAlgoOrders")
            existing_sl = [a for a in all_algo
                           if a.get("symbol")==sym and a.get("positionSide")=="SHORT"
                           and "STOP" in a.get("orderType","")]
            if existing_sl:
                continue  # 已有止损，不重复补
        except Exception:
            pass

        entry = float(p["entryPrice"])
        mark  = float(p["markPrice"])
        qty   = int(abs(float(p["positionAmt"])))
        if qty == 0:
            continue

        reports.append(f"🔧 {sym} 裸仓检测：有空单持仓但无止损，自动补挂")

        try:
            _BASE_PM = "https://fapi.binance.com"
            _PROXY_PM = {"http": api.PROXY_URL, "https": api.PROXY_URL}
            k1h_repair = _req.get(f"{_BASE_PM}/fapi/v1/klines",
                params={"symbol":sym,"interval":"1h","limit":20},
                proxies=_PROXY_PM, timeout=8).json()
            _atr_r = atr_calc(k1h_repair)
            _high_r = max(float(k[2]) for k in k1h_repair[-12:])
            sl_r    = _high_r + _atr_r * 0.8
        except Exception:
            sl_r = entry + _atr_r * 2.0  # 极端 fallback: ATR×2 而非固定6%

        try:
            zones_r = cg.get_liq_zones(sym, mark, range_="3d", top_n=4)
            below_r = zones_r.get("below", [])
            tp1_r   = below_r[0][0] + _atr_r * 0.1 if below_r else entry * 0.97
            tp2_r   = below_r[1][0] if len(below_r) > 1 else (entry - _atr_r * 4.0)
            tp3_r   = below_r[2][0] if len(below_r) > 2 else entry * 0.91
        except Exception:
            tp1_r, tp2_r, tp3_r = entry*0.97, entry*0.94, entry*0.91

        tick_r, _ = get_sym_info(sym)
        sl_str_r  = fmt_price(sl_r,  tick_r)
        tp1_str_r = fmt_price(tp1_r, tick_r)
        tp2_str_r = fmt_price(tp2_r, tick_r)
        tp3_str_r = fmt_price(tp3_r, tick_r)
        q1 = int(qty * 0.40); q2 = int(qty * 0.35); q3 = qty - q1 - q2

        # 挂止损
        _sl_oid, _ = place_sl_order(sym, "BUY", qty, float(sl_str_r), "SHORT")
        # 挂三档止盈
        _tp1_r = api.place_algo_order(sym,"BUY","TAKE_PROFIT_MARKET",float(tp1_str_r),"SHORT",quantity=q1)
        _tp2_r = api.place_algo_order(sym,"BUY","TAKE_PROFIT_MARKET",float(tp2_str_r),"SHORT",quantity=q2)
        _tp3_r = api.place_algo_order(sym,"BUY","TAKE_PROFIT_MARKET",float(tp3_str_r),"SHORT",quantity=q3)

        # 写入 state
        short_state["positions"][sym] = {
            "entry_price": entry, "sl_price": float(sl_str_r),
            "tp_price": float(tp1_str_r), "tp2_price": float(tp2_str_r), "tp3_price": float(tp3_str_r),
            "qty": str(qty), "sl_order_id": str(_sl_oid) if _sl_oid else None,
            "pending_order_id": None, "opened_at": int(time.time()),
            "repaired": True,
        }
        reports.append(
            f"  ✅ {sym} 裸仓修复: SL={sl_str_r} TP1={tp1_str_r} TP2={tp2_str_r} TP3={tp3_str_r}"
        )

    save_short_state(short_state)

_repair_naked_short_positions(_short_state, _short_reports)
# run_short_position_maintenance 已禁用（冲突3修复）
# 空单止盈止损统一由 ws_monitor.py 实时维护，pos_state.json 为唯一状态源
# _active_shorts = run_short_position_maintenance(_short_state, _short_reports)
run_short_pending_check(_short_state, _short_reports)
# 做空扫描已独立到 short_scanner.py，此处不调用 run_short_scan

if _short_reports:
    ts_s = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n📉 空单维护 | {ts_s}")
    for r in _short_reports:
        print(r)
