"""
pin_scanner.py — 插针反弹扫描（每5分钟触发）

逻辑：
  全市场扫描5分钟K线，发现插针形态（影线>实体3倍）立即写入观察区
  下影线 > 实体3倍 → 多单信号（插针后反弹）
  上影线 > 实体3倍 → 空单信号（插针后回落）

观察区写入：
  多单 → watchlist.json（与 scanner.py 共享，ws_monitor 实时监控）
  空单 → short_state.json（与 short_scanner.py 共享）

插针信号有效期：30分钟
超时未成交自动移出观察区（由各自的 scanner 在下一轮清理）

通知：发现即推送，不等下一轮15分钟扫描
"""
import sys, os, requests, json, math, time, datetime

sys.path.insert(0, "/root/.openclaw/workspace")
sys.path.insert(0, "/root/.openclaw/workspace/scripts")
import binance_api as api
from entry_logic import (
    liquidation_map, atr_calc, rsi_calc, ema_calc, fmt_price, fmt_qty,
    calc_score_trend, calc_score_short
)

BASE_F    = "https://fapi.binance.com"
PROXY     = {"http": api.PROXY_URL, "https": api.PROXY_URL}
JARVIS_TO = "432401404:thread:019f9203-3b29-7d9e-bfb4-b67c0b06587c"

WATCHLIST_FILE   = "/root/.openclaw/workspace/watchlist.json"
SHORT_STATE_FILE = "/root/.openclaw/workspace/ws_cache/short_state.json"
PIN_TTL          = 1800   # 30分钟有效期（秒）

# ── 插针判断参数 ──
PIN_RATIO        = 3.0    # 影线 > 实体 * 3
MIN_BODY_PCT     = 0.001  # 实体至少是价格的0.1%（过滤十字星）
MIN_VOL_USD      = 5_000_000  # 最低24h成交额
MIN_NOTIONAL_CHG = 0.005  # 插针幅度至少0.5%（过滤微型插针）

def notify(msg: str):
    print(f"__NOTIFY__:{msg}")

def load_watchlist():
    try:
        with open(WATCHLIST_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def save_watchlist(wl: dict):
    tmp = WATCHLIST_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(wl, f, ensure_ascii=False, indent=2)
    os.replace(tmp, WATCHLIST_FILE)

def load_short_state():
    try:
        with open(SHORT_STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"positions": {}, "watchlist": {}, "cooldown": {}}

def save_short_state(s):
    tmp = SHORT_STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SHORT_STATE_FILE)

def get_klines(sym, interval, limit=10):
    return api.get_klines_proxy(sym, interval, limit)

def detect_pin(k5m_list):
    """
    检测最近一根5m K线是否有插针。
    返回 (pin_type, shadow_ratio, body_pct, price)
      pin_type: 'LONG'（下影针，做多信号）/ 'SHORT'（上影针，做空信号）/ None
    """
    if not k5m_list:
        return None, 0, 0, 0
    k = k5m_list[-1]
    o = float(k[1]); h = float(k[2]); lo = float(k[3]); c = float(k[4])
    price = c
    body   = abs(c - o)
    lower  = min(o, c) - lo   # 下影线
    upper  = h - max(o, c)    # 上影线
    body_pct = body / price if price > 0 else 0

    # 过滤十字星（实体太小）
    if body_pct < MIN_BODY_PCT:
        return None, 0, 0, price

    # 下影针 → 多单信号
    if lower > body * PIN_RATIO and lower / price > MIN_NOTIONAL_CHG:
        ratio = lower / body if body > 0 else lower / (price * 0.0001)
        return 'LONG', ratio, body_pct, price

    # 上影针 → 空单信号
    if upper > body * PIN_RATIO and upper / price > MIN_NOTIONAL_CHG:
        ratio = upper / body if body > 0 else upper / (price * 0.0001)
        return 'SHORT', ratio, body_pct, price

    return None, 0, 0, price

# ── 获取全市场ticker ──
try:
    all_tickers = requests.get(f"{BASE_F}/fapi/v1/ticker/24hr", proxies=PROXY, timeout=15).json()
except Exception as e:
    print(f"⚠️ pin_scanner: ticker获取失败 {e}")
    sys.exit(1)

# 过滤：USDT合约 + 最低成交额
candidates = [
    t for t in all_tickers
    if t["symbol"].endswith("USDT")
    and float(t.get("quoteVolume", 0)) > MIN_VOL_USD
]
# 按成交量排序，取前150个（避免扫太多小币）
candidates.sort(key=lambda x: float(x.get("quoteVolume", 0)), reverse=True)
candidates = candidates[:150]

# ── 加载现有持仓，过滤已有仓位 ──
try:
    pos_all = api.request("GET", api.BASE_FAPI + "/fapi/v2/positionRisk")
    active_syms = {p["symbol"] for p in pos_all if abs(float(p.get("positionAmt", 0))) > 0.001}
except Exception:
    active_syms = set()

# ── 加载现有观察区（防止重复写入） ──
wl_long  = load_watchlist()
wl_short = load_short_state()
existing_long_wl  = set(wl_long.keys())
existing_short_wl = set(wl_short.get("watchlist", {}).keys())

now_ts   = int(time.time())
ts_str   = datetime.datetime.utcnow().strftime("%H:%M UTC")
reports  = []
new_pins = []

for t in candidates:
    sym = t["symbol"]
    if sym in active_syms:
        continue  # 已有持仓，跳过

    try:
        k5m = get_klines(sym, "5m", 5)
        if not k5m:
            continue
        pin_type, ratio, body_pct, price = detect_pin(k5m)
        if pin_type is None:
            continue

        # 基础质量过滤：拉取1h K线做简单评分
        k1h  = get_klines(sym, "1h", 30)
        k4h  = get_klines(sym, "4h", 20)
        k15m = get_klines(sym, "15m", 15)
        if not k1h:
            continue

        chg24  = float(t.get("priceChangePercent", 0))
        funding = float(api.request(
            "GET", api.BASE_FAPI + "/fapi/v1/premiumIndex", {"symbol": sym}
        ).get("lastFundingRate", 0)) * 100

        if pin_type == 'LONG':
            # 下影针→多单：写入 watchlist.json
            if sym in existing_long_wl:
                continue  # 已在多单观察区，跳过
            try:
                score, _ = calc_score_trend(sym, k1h, k4h, k15m, t, funding)
            except Exception:
                score = 55  # 无法评分时给保底分

            # ── 条目6：PIN信号轻量过滤（三维度检查）──
            _pin_watch_reason = []
            # 检查1：资金费率极端异常（绝对值>0.5%）
            if abs(funding) > 0.5:
                _pin_watch_reason.append(f"费率极端{funding:+.3f}%")
            # 检查2：聪明钱方向反向（Token Report 负面信号）
            try:
                from entry_logic import get_token_report_score as _gtr
                _tr_score, _tr_veto, _tr_reason = _gtr(sym)
                if _tr_veto or _tr_score < 0:
                    _pin_watch_reason.append(f"聪明钱反向({_tr_reason})")
            except Exception:
                pass
            # 检查3：OI反向暴变（多单PIN但OI下降>20%）
            try:
                from entry_logic import check_oi_divergence as _coid
                _oi_div, _oi_chg = _coid(sym)
                if _oi_div:
                    _pin_watch_reason.append(f"OI背离{_oi_chg:+.1f}%")
            except Exception:
                pass
            if _pin_watch_reason:
                # 任意一个异常 → 进观察区（降级，不直接挂单）
                _watch_flag = True
                entry_note = f"插针多单 下影{ratio:.1f}x 评分={score:.0f} ⚠️过滤:{'/'.join(_pin_watch_reason)}"
            else:
                _watch_flag = False
                entry_note = f"插针多单 下影{ratio:.1f}x 评分={score:.0f}"
            # 计算建议入场价（当前价上方一点）
            atr = atr_calc(k1h) if k1h else price * 0.01
            suggest_entry = round(price + atr * 0.1, 8)
            # 按 scanner.py add_to_watchlist 的字段格式写入，确保 ws_monitor 兼容
            wl_long[sym] = {
                "target":     suggest_entry,
                "direction":  "below",           # LONG 候选等价格下探
                "note":       entry_note,
                "added_at":   str(now_ts),
                "score":      score,
                "sig_type":   "pin_long",
                "expire_at":  now_ts + PIN_TTL,  # 30分钟有效期
                "chg24":      chg24,
                "funding":    funding,
                "pin_watch":  _watch_flag,       # True=异常标记，需二次确认才挂单
            }
            if _watch_flag:
                new_pins.append(f"⚠️ {sym} 下影插针 {ratio:.1f}x 多单→观察区(过滤) {'/'.join(_pin_watch_reason)} 评分={score:.0f} 价格={price}")
            else:
                new_pins.append(f"📌 {sym} 下影插针 {ratio:.1f}x 多单→观察区 评分={score:.0f} 价格={price}")

        elif pin_type == 'SHORT':
            # 上影针→空单：写入 short_state.watchlist
            if sym in existing_short_wl:
                continue  # 已在空单观察区，跳过
            try:
                score, _ = calc_score_short(sym, k1h, k4h, k15m, t, funding)
            except Exception:
                score = 55

            # ── 条目6：PIN信号轻量过滤（三维度检查）──
            _pin_watch_reason_s = []
            if abs(funding) > 0.5:
                _pin_watch_reason_s.append(f"费率极端{funding:+.3f}%")
            try:
                from entry_logic import get_token_report_score as _gtr2
                _tr2_score, _tr2_veto, _tr2_reason = _gtr2(sym)
                if _tr2_score > 0:  # 聪明钱正面（净流入）→禁空
                    _pin_watch_reason_s.append(f"聪明钱反向({_tr2_reason})")
            except Exception:
                pass
            try:
                from entry_logic import check_oi_divergence as _coid2
                _oi_div2, _oi_chg2 = _coid2(sym)
                # 空单PIN但OI上升>20%（多头入场，与空单方向相反）
                if not _oi_div2:  # check_oi_divergence返回 True 表示 OI 下降
                    try:
                        _oi_d2 = requests.get(
                            f"{BASE_F}/futures/data/openInterestHist",
                            params={'symbol': sym, 'period': '1h', 'limit': 6}, timeout=5
                        ).json()
                        if isinstance(_oi_d2, list) and len(_oi_d2) >= 3:
                            _oi_v2 = [float(x['sumOpenInterest']) for x in _oi_d2]
                            _oi_rise = (_oi_v2[-1] - _oi_v2[-4]) / (_oi_v2[-4] + 1e-9) if len(_oi_v2) >= 4 else 0
                            if _oi_rise > 0.20:
                                _pin_watch_reason_s.append(f"OI激增{_oi_rise*100:+.1f}%")
                    except Exception:
                        pass
            except Exception:
                pass
            if _pin_watch_reason_s:
                _watch_flag_s = True
                entry_note = f"插针空单 上影{ratio:.1f}x 评分={score:.0f} ⚠️过滤:{'/'.join(_pin_watch_reason_s)}"
            else:
                _watch_flag_s = False
                entry_note = f"插针空单 上影{ratio:.1f}x 评分={score:.0f}"
            atr = atr_calc(k1h) if k1h else price * 0.01
            suggest_entry = round(price - atr * 0.1, 8)
            wl_short_dict = wl_short.setdefault("watchlist", {})
            wl_short_dict[sym] = {
                "entry_mode":     "PIN_SHORT",
                "last_score":     score,
                "last_seen_ts":   now_ts,
                "first_seen_ts":  now_ts,
                "first_seen_price": price,
                "expire_at":      now_ts + PIN_TTL,   # 30分钟有效期
                "hit_count":      1,
                "note":           entry_note,
                "chg24":          chg24,
                "funding":        funding,
                "pin_watch":      _watch_flag_s,      # True=异常标记
            }
            if _watch_flag_s:
                new_pins.append(f"⚠️ {sym} 上影插针 {ratio:.1f}x 空单→观察区(过滤) {'/'.join(_pin_watch_reason_s)} 评分={score:.0f} 价格={price}")
            else:
                new_pins.append(f"📌 {sym} 上影插针 {ratio:.1f}x 空单→观察区 评分={score:.0f} 价格={price}")

    except Exception as e:
        reports.append(f"⚠️ {sym} 插针扫描异常: {e}")

# ── 清理过期的插针观察区条目 ──
expired_long  = [s for s, v in wl_long.items()
                 if v.get("sig_type","").startswith("pin") and now_ts > v.get("expire_at", 0)]
expired_short = [s for s, v in wl_short.get("watchlist", {}).items()
                 if v.get("entry_mode","") == "PIN_SHORT" and now_ts > v.get("expire_at", 0)]
for s in expired_long:
    del wl_long[s]
for s in expired_short:
    del wl_short["watchlist"][s]

# ── 保存 ──
if new_pins or expired_long or expired_short:
    save_watchlist(wl_long)
    save_short_state(wl_short)

# ── 输出 ──
if new_pins:
    msg = f"📍 插针信号 | {ts_str}\n" + "\n".join(new_pins)
    if expired_long or expired_short:
        msg += f"\n🧹 清理过期插针: 多{len(expired_long)}个 空{len(expired_short)}个"
    notify(msg)
    print(msg)
elif expired_long or expired_short:
    print(f"🧹 清理过期插针观察区: 多{len(expired_long)}个 空{len(expired_short)}个")
    print("HEARTBEAT_OK")
else:
    if reports:
        for r in reports:
            print(r)
    else:
        print("HEARTBEAT_OK")
