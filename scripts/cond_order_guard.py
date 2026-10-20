#!/usr/bin/env python3
"""
cond_order_guard.py — 条件限价单巡逻脚本
每15分钟由 cron 触发，负责：
  1. 超时撤单（挂出4小时内未成交）
  2. 评分下滑撤单（实时重算评分，跌破60）
  3. 技术指标反向撤单（EMA翻转/RSI越界/BTC快速单向/价格远离锚点）
  4. 熔断触发时撤所有条件单
  5. 检测条件单是否已成交（清理已成交状态）

成交检测：ws_monitor 负责处理条件单的成交推送（已有 on_order_filled），
本脚本仅做撤单和维护。
"""
import sys, os, requests, json, time, datetime
sys.path.insert(0, "/root/.openclaw/workspace")
import binance_api as api
from entry_logic import (
    rsi_calc, ema_calc, calc_score_trend, calc_score_reversal,
    classify_signal, liquidation_map, calc_entry_mode
)

PROXY          = {"http": api.PROXY_URL, "https": api.PROXY_URL}
BASE_F         = "https://fapi.binance.com"
COND_STATE     = "/root/.openclaw/workspace/ws_cache/cond_order_state.json"
SHORT_CIRCUIT  = "/root/.openclaw/workspace/ws_cache/short_circuit.json"
SCORE_DROP_THR = 60     # 评分跌破此值撤单
COND_TTL       = 4 * 3600
# v2.10修复: ANCHOR_DRIFT 改为动态阈值，与 entry_logic._dev_threshold() 保持一致
# 旧值15%太宽：价格漂移超过dev_max时条件单已经不合理，应立即撤单让下轮重新评估
# UBUSDT案例：旧逻辑等价格涨15%（0.189）才撤，新逻辑H+1（涨9%）即撤，提前2+小时释放
# 不再使用固定的 ANCHOR_DRIFT 常量，改在循环内动态计算
ANCHOR_DRIFT   = None   # 已废弃，保留变量避免破坏引用

SCORE_TREND    = 75
SCORE_REVERSAL = 65

def _load(path, default={}):
    try:
        with open(path) as f: return json.load(f)
    except: return default

def _save(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f: json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)

now_ts  = int(time.time())
reports = []

# ── 熔断检查 ──
circuit = _load(SHORT_CIRCUIT, {})
today   = datetime.datetime.utcnow().strftime("%Y-%m-%d")
if circuit.get("reset_date") != today:
    circuit = {"triggered": False, "daily_loss": 0.0, "reset_date": today}
breaker_on = circuit.get("triggered", False)

cond = _load(COND_STATE, {})
if not cond:
    print("HEARTBEAT_OK")
    sys.exit(0)

# ── BTC 环境快速采样（用于技术指标检查）──
btc_ema_bull = False
btc_ema_bear = False
btc_fast_chg = 0.0
try:
    btc_k4h = api.get_klines_proxy("BTCUSDT", "4h", 60)
    btc_c4h = [float(k[4]) for k in btc_k4h]
    e9  = ema_calc(btc_c4h[-9:],  9)
    e20 = ema_calc(btc_c4h[-20:], 20)
    e50 = ema_calc(btc_c4h[-50:], 50) if len(btc_c4h) >= 50 else ema_calc(btc_c4h, len(btc_c4h))
    btc_ema_bull = e9 > e20 > e50
    btc_ema_bear = e9 < e20 < e50
    # 近4根15分钟涨跌幅
    btc_k15m = api.get_klines_proxy("BTCUSDT", "15m", 6)
    btc_c15m = [float(k[4]) for k in btc_k15m]
    if len(btc_c15m) >= 4:
        btc_fast_chg = (btc_c15m[-1] - btc_c15m[-4]) / btc_c15m[-4] * 100
except Exception:
    pass

# ── 获取当前所有挂单（用于确认状态）──
try:
    open_orders_all = api.request("GET", api.BASE_FAPI + "/fapi/v1/openOrders")
    open_order_ids  = {int(o['orderId']) for o in open_orders_all if isinstance(o, dict)}
except Exception:
    open_order_ids  = set()

to_remove = []  # 需要从 cond_state 删除的 sym

for sym, entry in list(cond.items()):
    direction  = entry.get('direction', 'LONG')
    order_id   = int(entry.get('order_id', 0))
    limit_p    = float(entry.get('limit_price', 0))
    placed_at  = int(entry.get('placed_at', 0))
    expire_at  = int(entry.get('expire_at', 0))
    sig_type   = entry.get('sig_type', 'trend')
    score_orig = float(entry.get('score', 0))
    reason     = None

    # ── 1. 检测是否已成交（订单不在 open_orders 里）──
    if order_id and order_id not in open_order_ids:
        # 可能已成交，也可能从未生效：查订单状态
        try:
            o_info = api.request("GET", api.BASE_FAPI + "/fapi/v1/order",
                                 {"symbol": sym, "orderId": order_id})
            status = o_info.get("status", "")
            if status in ("FILLED", "PARTIALLY_FILLED"):
                # 成交了，ws_monitor 会接管，cond_guard 只清理状态
                to_remove.append(sym)
                reports.append(f"✅ {sym} 条件单已成交，清理状态 orderId={order_id}")
                continue
            elif status in ("CANCELED", "EXPIRED", "REJECTED"):
                to_remove.append(sym)
                reports.append(f"🗑️ {sym} 条件单已{status}，清理状态")
                continue
        except Exception:
            pass

    # ── 2. 熔断 → 撤所有条件单 ──
    if breaker_on:
        reason = "熔断触发"

    # ── 3. 超时撤单 ──
    elif expire_at > 0 and now_ts > expire_at:
        reason = f"超时{(now_ts - placed_at)//3600}h{(now_ts - placed_at)%3600//60}m"

    else:
        # ── 4. 技术指标反向检查 ──
        try:
            k1h  = api.get_klines_proxy(sym, "1h",  50)
            k4h  = api.get_klines_proxy(sym, "4h",  20)
            k15m = api.get_klines_proxy(sym, "15m", 30)
            c1h  = [float(k[4]) for k in k1h]
            v1h  = [float(k[5]) for k in k1h]
            rsi  = rsi_calc(c1h)
            cur_p = c1h[-1]

            ema20_1h = ema_calc(c1h[-20:], 20)
            ema50_1h = ema_calc(c1h[-50:], 50) if len(c1h) >= 50 else ema_calc(c1h, len(c1h))
            ema20_4h_v = [float(k[4]) for k in k4h]
            ema20_4h = ema_calc(ema20_4h_v[-20:], 20) if len(ema20_4h_v) >= 20 else ema_calc(ema20_4h_v, len(ema20_4h_v))
            ema50_4h = ema_calc(ema20_4h_v[-50:], 50) if len(ema20_4h_v) >= 50 else ema_calc(ema20_4h_v, len(ema20_4h_v))
            ema_1h_bull = ema20_1h > ema50_1h
            ema_4h_bull = ema20_4h > ema50_4h

            # 近3根K线成交量放大判断
            vb  = sum(v1h[-10:-3]) / 7 if len(v1h) >= 10 else 1
            vr3 = (sum(v1h[-3:]) / 3) / vb if vb > 0 else 1
            big_candle_bear = (c1h[-1] < c1h[-2] and vr3 > 1.5)  # 放量大阴线
            big_candle_bull = (c1h[-1] > c1h[-2] and vr3 > 1.5)  # 放量大阳线

            # 价格漂移锚点（反方向跑了）
            # v2.10修复：阈值改为动态dev_max，与entry_logic._dev_threshold保持一致
            from entry_logic import _dev_threshold as _devth
            _dynamic_drift = _devth(atr, cur_p) if atr > 0 else 6.0
            if direction == 'LONG' and limit_p > 0:
                anchor_drift = (cur_p - limit_p) / limit_p * 100
                if anchor_drift > _dynamic_drift:
                    reason = f"价格脱离锚点{anchor_drift:.1f}%>动态阈值{_dynamic_drift:.1f}%，撤单重新评估"
            elif direction == 'SHORT' and limit_p > 0:
                anchor_drift = (limit_p - cur_p) / limit_p * 100
                if anchor_drift > _dynamic_drift:
                    reason = f"价格脱离锚点{anchor_drift:.1f}%>动态阈值{_dynamic_drift:.1f}%，撤单重新评估"

            # 多单条件单的撤单情形
            if not reason and direction == 'LONG':
                if not ema_1h_bull and not ema_4h_bull:
                    reason = "EMA 1H+4H 均转空排"
                elif rsi < 35 and big_candle_bear:
                    reason = f"RSI={rsi:.0f}<35 且放量阴线，趋势恶化"
                elif btc_fast_chg < -5.0:
                    reason = f"BTC 近1h急跌{btc_fast_chg:.1f}%（>5%强制撤单）"

            # 空单条件单的撤单情形
            elif not reason and direction == 'SHORT':
                if ema_1h_bull and ema_4h_bull:
                    reason = "EMA 1H+4H 均转多排"
                elif rsi > 65 and big_candle_bull:
                    reason = f"RSI={rsi:.0f}>65 且放量阳线，方向反转"
                elif btc_fast_chg > 5.0:
                    reason = f"BTC 近1h急涨{btc_fast_chg:.1f}%（>5%强制撤单）"

            # ── 5. 评分下滑撤单 ──
            if not reason:
                try:
                    fr_raw = float(api.request("GET", api.BASE_FAPI + "/fapi/v1/premiumIndex",
                                               {"symbol": sym}).get("lastFundingRate", 0))
                    chg24  = float(api.request("GET", api.BASE_FAPI + "/fapi/v1/ticker/24hr",
                                               {"symbol": sym}).get("priceChangePercent", 0))
                    ticker = {"lastPrice": str(cur_p), "priceChangePercent": str(chg24),
                              "quoteVolume": str(sum(v1h) * cur_p)}

                    _new_sig = classify_signal(k1h, k4h, fr_raw, chg24)
                    if _new_sig in ('trend', 'reversal'):
                        new_sc, _ = (calc_score_trend(sym, k1h, k4h, k15m, ticker, fr_raw)
                                     if _new_sig == 'trend'
                                     else calc_score_reversal(sym, k1h, k4h, k15m, ticker, fr_raw))
                    else:
                        new_sc = score_orig  # 其他类型保持不变

                    if new_sc < SCORE_DROP_THR:
                        reason = f"评分降至{new_sc:.0f}<{SCORE_DROP_THR}（原{score_orig:.0f}）"
                except Exception:
                    pass

        except Exception as _tech_err:
            reports.append(f"⚠️ {sym} 技术指标检查异常: {_tech_err}")

    if reason:
        # 执行撤单
        try:
            if order_id:
                cancel_r = api.cancel_order(sym, order_id)
                reports.append(f"🗑️ {sym} {direction} 条件单已撤 原因={reason} orderId={order_id}")
            to_remove.append(sym)
        except Exception as _ce:
            reports.append(f"⚠️ {sym} 撤条件单异常: {_ce}")

# ── 清理已撤/已成交记录 ──
changed = False
for sym in to_remove:
    if sym in cond:
        del cond[sym]
        changed = True

if changed:
    _save(COND_STATE, cond)

# ── 输出 ──
if not reports:
    print("HEARTBEAT_OK")
else:
    ts_str = datetime.datetime.utcnow().strftime("%H:%M UTC")
    print(f"🔍 条件单巡检 | {ts_str} | 存活={len(cond)}个")
    for rp in reports:
        print(rp)
