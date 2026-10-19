#!/usr/bin/env python3
"""
eth_short_watcher.py — ETH 做空全自动执行工作流

完整流程（无需人工确认，自主运行）：
  1. 每60秒监控 ETH 价格 + 技术指标
  2. 综合评分 >= 70 且价格进入做空区 → 自动在 ETHUSDC 挂限价空单（初仓2ETH）
  3. 持仓后每30秒轮询：
     a. 价格触达 TP1 → 自动平仓60%，移动止损收到入场价
     b. 价格触达 TP2 → 自动全平剩余
     c. 价格触达 SL  → 自动止损全平
     d. 移动止损跟踪：价格每上涨5点，止损上移5点
  4. 成交/平仓后推送通知，亏损超50U时推送警告

仓位规则（保守）：
  初仓 2 ETH → 方向确认后可补仓 2 ETH → 最大 5 ETH

ETHUSDC 价差：实时读取，所有挂单价自动换算
"""

import sys, time, json, os, datetime as dt, traceback
sys.path.insert(0, "/root/.openclaw/workspace")
import binance_api as api
from entry_logic import rsi_calc, ema_calc, atr_calc
import coinglass_api as cg

# ── 配置 ────────────────────────────────────────────────────────
TARGET_SHORT_LOW  = 2590   # 做空入场区下沿（USDT参考价）
TARGET_SHORT_HIGH = 2620   # 做空入场区上沿
TP_POINTS_1       = 100    # TP1 目标点数（平60%）
TP_POINTS_2       = 200    # TP2 目标点数（全平）
SL_ATR_MULT       = 2.0    # SL = 入场价 + N×ATR
TP1_CLOSE_RATIO   = 0.6    # TP1 平仓比例
TRAIL_OFFSET      = 20     # 移动止损距最高价偏移点数（做空时距最低价）
TRAIL_STEP        = 5      # 移动止损每次移动步长

POS_INITIAL_ETH   = 2      # 初仓
POS_ADD_ETH       = 2      # 补仓
POS_MAX_ETH       = 5      # 最大总仓

SCAN_INTERVAL     = 300    # 入场扫描间隔（秒）5分钟一次，降低代理请求频率
MONITOR_INTERVAL  = 60     # 持仓监控间隔（秒）60秒，保证止损响应及时
NOTIFY_LOSS_LIMIT = 50     # 亏损超此U推送通知

SYM_ANALYSIS = "ETHUSDT"   # 分析用（清算图、RSI等）
SYM_TRADE    = "ETHUSDC"   # 实际下单用（限价免手续费）
STATE_FILE   = "eth_short_state.json"
LOG_FILE     = "logs/eth_short_watcher.log"
# ───────────────────────────────────────────────────────────────

os.makedirs("logs", exist_ok=True)


def log(msg: str):
    ts = dt.datetime.utcnow().strftime("%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def notify(msg: str):
    """推送通知到 Jarvis"""
    try:
        import subprocess
        subprocess.Popen(
            ["python3", "-c",
             f"import sys; sys.path.insert(0,'.'); "
             f"from jarvis_notify import send; send({repr(msg)})"],
            cwd="/root/.openclaw/workspace"
        )
    except Exception:
        pass
    log(f"[NOTIFY] {msg}")


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"status": "idle", "positions": [], "history": []}


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


# ── 价差工具 ─────────────────────────────────────────────────────
def get_spread() -> tuple:
    """返回 (usdt_mark, usdc_mark, spread)"""
    try:
        usdt = float(api.get_mark_price(SYM_ANALYSIS)["markPrice"])
        usdc = float(api.get_mark_price(SYM_TRADE)["markPrice"])
        return usdt, usdc, usdt - usdc
    except:
        usdt = float(api.get_mark_price(SYM_ANALYSIS)["markPrice"])
        return usdt, usdt - 1.0, 1.0


def to_usdc(price_usdt: float, spread: float, buf: float = 0.5) -> float:
    return round(price_usdt - spread - buf, 2)


# ── 数据获取 ─────────────────────────────────────────────────────
def fetch_data() -> dict:
    eth_k1h  = api.get_klines(SYM_ANALYSIS, "1h", 50)
    eth_k15m = api.get_klines(SYM_ANALYSIS, "15m", 30)
    eth_k4h  = api.get_klines(SYM_ANALYSIS, "4h", 20)
    btc_k1h  = api.get_klines("BTCUSDT", "1h", 20)

    eth_c1h  = [float(k[4]) for k in eth_k1h]
    eth_c15m = [float(k[4]) for k in eth_k15m]
    eth_c4h  = [float(k[4]) for k in eth_k4h]
    btc_c1h  = [float(k[4]) for k in btc_k1h]

    usdt_mark, usdc_mark, spread = get_spread()
    fr = float(api.get_mark_price(SYM_ANALYSIS).get("lastFundingRate", 0)) * 100

    rsi1h  = rsi_calc(eth_c1h, 14)
    rsi15m = rsi_calc(eth_c15m, 14)
    rsi4h  = rsi_calc(eth_c4h, 14)
    btc_rsi= rsi_calc(btc_c1h, 14)
    atr    = atr_calc(eth_k1h)

    # 1H K线方向
    last_k    = eth_k1h[-2]
    k_bearish = float(last_k[4]) < float(last_k[1])

    # RSI 15m 顶背离
    rsi15m_series = [rsi_calc(eth_c15m[:i+1], 14) for i in range(14, len(eth_c15m))]
    prices_8  = eth_c15m[-8:]
    rsis_8    = rsi15m_series[-8:] if len(rsi15m_series) >= 8 else rsi15m_series
    bearish_div = (len(prices_8) > 1 and prices_8[-1] > max(prices_8[:-1]) and
                   len(rsis_8) > 1 and rsis_8[-1] < max(rsis_8[:-1]) * 0.98)
    rsi15m_fall = (len(rsis_8) >= 3 and rsis_8[-1] < rsis_8[-2] < rsis_8[-3])

    # BTC 上方最近大墙
    try:
        bz = cg.get_liq_zones("BTCUSDT", float(api.get_mark_price("BTCUSDT")["markPrice"]),
                               range_="3d", top_n=5)
        btc_above = sorted(bz.get("above", []), key=lambda x: x[0])
        btc_wall  = btc_above[0][0] if btc_above else 99999
        btc_wall_vol = btc_above[0][1] / 1e6 if btc_above else 0
    except:
        btc_wall = 99999; btc_wall_vol = 0

    btc_chg = float(api.get_ticker_24h("BTCUSDT").get("priceChangePercent", 0))

    return {
        "eth_mark": usdt_mark, "eth_usdc": usdc_mark, "spread": spread,
        "rsi1h": rsi1h, "rsi15m": rsi15m, "rsi4h": rsi4h,
        "btc_rsi": btc_rsi, "btc_chg": btc_chg,
        "atr": atr, "fr": fr,
        "k_bearish": k_bearish,
        "bearish_div": bearish_div, "rsi15m_fall": rsi15m_fall,
        "btc_wall": btc_wall, "btc_wall_vol": btc_wall_vol,
    }


# ── 评分 ─────────────────────────────────────────────────────────
def evaluate(d: dict, target: float = None) -> tuple:
    low  = target or TARGET_SHORT_LOW
    high = low + (TARGET_SHORT_HIGH - TARGET_SHORT_LOW)
    mark = d["eth_mark"]
    score = 0

    in_zone   = low <= mark <= high
    near_zone = mark >= low * 0.99
    if in_zone:   score += 30
    elif near_zone: score += 10

    if d["rsi4h"] >= 95: score += 20
    elif d["rsi4h"] >= 85: score += 12

    if d["rsi1h"] >= 72: score += 15
    elif d["rsi1h"] >= 65: score += 5

    if d["bearish_div"]: score += 15
    elif d["rsi15m_fall"]: score += 10
    elif d["rsi15m"] >= 75: score += 3

    if d["k_bearish"]: score += 10

    if d["btc_rsi"] <= 65:  score += 10
    elif d["btc_rsi"] <= 72: score += 4

    if d["fr"] > 0.05:  score = min(score + 5, 100)
    elif d["fr"] < -0.05: score = max(score - 5, 0)

    return score, in_zone, low, high


# ── 下单函数 ─────────────────────────────────────────────────────
def get_qty_precision(symbol: str) -> int:
    """返回数量精度（小数位数）"""
    try:
        info = api.request("GET", api.BASE_FAPI + "/fapi/v1/exchangeInfo", {})
        for s in info.get("symbols", []):
            if s["symbol"] == symbol:
                for f in s.get("filters", []):
                    if f.get("filterType") == "LOT_SIZE":
                        step = f["stepSize"].rstrip("0")
                        return len(step.split(".")[-1]) if "." in step else 0
    except:
        pass
    return 3


def _place_signed(method: str, path: str, params: dict) -> dict:
    """直接构造签名请求，绕过 api.place_order 的参数限制"""
    import time as _t, hmac as _hmac, hashlib as _hashlib
    import requests as _req
    params["timestamp"] = int(_t.time() * 1000)
    params["recvWindow"] = 5000
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    sig = _hmac.new(api.SECRET.encode(), qs.encode(), _hashlib.sha256).hexdigest()
    params["signature"] = sig
    url = api.BASE_FAPI + path
    headers = {"X-MBX-APIKEY": api.API_KEY}
    if method == "POST":
        r = _req.post(url, params=params, headers=headers, proxies=api.PROXIES, timeout=10)
    elif method == "DELETE":
        r = _req.delete(url, params=params, headers=headers, proxies=api.PROXIES, timeout=10)
    else:
        r = _req.get(url, params=params, headers=headers, proxies=api.PROXIES, timeout=10)
    return r.json()


def place_short_entry(entry_usdc: float, qty_eth: float, spread: float) -> dict:
    """在 ETHUSDC 挂限价空单（双向持仓模式，positionSide=SHORT）"""
    prec = get_qty_precision(SYM_TRADE)
    qty  = round(qty_eth, prec)
    log(f"[ORDER] 挂限价空单 {SYM_TRADE} qty={qty} price={entry_usdc}")
    result = _place_signed("POST", "/fapi/v1/order", {
        "symbol":       SYM_TRADE,
        "side":         "SELL",
        "positionSide": "SHORT",
        "type":         "LIMIT",
        "quantity":     str(qty),
        "price":        str(entry_usdc),
        "timeInForce":  "GTC",
    })
    log(f"[ORDER] 结果: {result}")
    return result


def place_close_order(qty_eth: float, price_usdc: float, label: str = "") -> dict:
    """在 ETHUSDC 挂限价平仓买单（双向持仓，positionSide=SHORT）"""
    prec = get_qty_precision(SYM_TRADE)
    qty  = round(qty_eth, prec)
    log(f"[CLOSE] {label} 挂平仓买单 qty={qty} price={price_usdc}")
    result = _place_signed("POST", "/fapi/v1/order", {
        "symbol":       SYM_TRADE,
        "side":         "BUY",
        "positionSide": "SHORT",
        "type":         "LIMIT",
        "quantity":     str(qty),
        "price":        str(price_usdc),
        "timeInForce":  "GTC",
    })
    log(f"[CLOSE] 结果: {result}")
    return result


def place_market_close(qty_eth: float, label: str = "") -> dict:
    """市价全平（止损用，positionSide=SHORT）"""
    prec = get_qty_precision(SYM_TRADE)
    qty  = round(qty_eth, prec)
    log(f"[MARKET_CLOSE] {label} 市价平仓 qty={qty}")
    result = _place_signed("POST", "/fapi/v1/order", {
        "symbol":       SYM_TRADE,
        "side":         "BUY",
        "positionSide": "SHORT",
        "type":         "MARKET",
        "quantity":     str(qty),
        "reduceOnly":   "true",
    })
    log(f"[MARKET_CLOSE] 结果: {result}")
    return result


def place_algo_sl(qty_eth: float, trigger: float, label: str = "") -> dict:
    """挂 algo 止损单（STOP_MARKET，账户不支持普通 STOP_MARKET 时使用）"""
    prec = get_qty_precision(SYM_TRADE)
    qty  = round(qty_eth, prec)
    log(f"[ALGO_SL] {label} algoOrder STOP_MARKET qty={qty} trigger={trigger}")
    result = _place_signed("POST", "/fapi/v1/algoOrder", {
        "symbol":       SYM_TRADE,
        "side":         "BUY",
        "positionSide": "SHORT",
        "algoType":     "CONDITIONAL",
        "type":         "STOP_MARKET",
        "quantity":     str(qty),
        "triggerPrice": str(trigger),
    })
    log(f"[ALGO_SL] 结果: {result}")
    return result


def place_algo_tp(qty_eth: float, trigger: float, label: str = "") -> dict:
    """挂 algo 止盈单（TAKE_PROFIT_MARKET）"""
    prec = get_qty_precision(SYM_TRADE)
    qty  = round(qty_eth, prec)
    log(f"[ALGO_TP] {label} algoOrder TAKE_PROFIT_MARKET qty={qty} trigger={trigger}")
    result = _place_signed("POST", "/fapi/v1/algoOrder", {
        "symbol":       SYM_TRADE,
        "side":         "BUY",
        "positionSide": "SHORT",
        "algoType":     "CONDITIONAL",
        "type":         "TAKE_PROFIT_MARKET",
        "quantity":     str(qty),
        "triggerPrice": str(trigger),
    })
    log(f"[ALGO_TP] 结果: {result}")
    return result


def cancel_all_open(symbol: str):
    """撤销该标的所有未成交普通挂单"""
    try:
        orders = api.get_open_orders(symbol)
        for o in orders:
            api.cancel_order(symbol, o["orderId"])
            log(f"[CANCEL] 撤单 orderId={o['orderId']} price={o.get('price')}")
    except Exception as e:
        log(f"[CANCEL_ERR] {e}")


def cancel_all_algo(symbol: str):
    """撤销该标的所有未成交 algo 单（止损/止盈）"""
    try:
        result = _place_signed("GET", "/fapi/v1/openAlgoOrders", {"symbol": symbol})
        if isinstance(result, list):
            for o in result:
                aid = o.get("algoId")
                if aid:
                    r = _place_signed("DELETE", "/fapi/v1/algoOrder", {"algoId": aid})
                    log(f"[CANCEL_ALGO] algoId={aid} type={o.get('orderType')} -> {r.get('msg','ok')}")
    except Exception as e:
        log(f"[CANCEL_ALGO_ERR] {e}")


def get_position_qty(symbol: str) -> float:
    """获取当前空单持仓量（绝对值）"""
    try:
        positions = api.get_positions()
        for p in positions:
            if p.get("symbol") == symbol:
                amt = float(p.get("positionAmt", 0))
                return abs(amt)
    except:
        pass
    return 0.0


# ── 持仓监控（TP/SL/移动止损）────────────────────────────────────
def monitor_position(state: dict):
    """
    持仓期间每30秒轮询一次。
    state["positions"] 里记录每个仓位的入场信息。
    """
    if not state.get("positions"):
        return state

    pos = state["positions"][0]   # 目前只管一个主仓位
    entry_usdc = pos["entry_usdc"]
    entry_usdt = pos["entry_usdt"]
    sl_usdc    = pos["sl_usdc"]
    tp1_usdc   = pos["tp1_usdc"]
    tp2_usdc   = pos["tp2_usdc"]
    total_qty  = pos["total_qty"]
    remaining  = pos.get("remaining_qty", total_qty)
    tp1_done   = pos.get("tp1_done", False)
    trail_sl   = pos.get("trail_sl", sl_usdc)  # 当前移动止损价

    # 获取当前价格
    _, curr_usdc, spread = get_spread()
    curr_price = curr_usdc

    log(f"[MONITOR] 持仓 {total_qty}ETH 剩余{remaining}ETH | 现价{curr_price:.2f} | "
        f"SL={trail_sl:.2f} TP1={tp1_usdc:.2f} TP2={tp2_usdc:.2f} trail={trail_sl:.2f}")

    # ── 止损触发（价格 >= 移动止损价）────────────────────────────
    if curr_price >= trail_sl:
        log(f"[SL] 触发！现价{curr_price:.2f} >= SL{trail_sl:.2f}，市价全平 {remaining}ETH")
        cancel_all_open(SYM_TRADE)
        cancel_all_algo(SYM_TRADE)
        try:
            place_market_close(remaining, label="SL")
        except Exception as e:
            log(f"[SL_ERR] {e}")
        pnl = (entry_usdc - curr_price) * remaining
        msg = f"⚠️ ETH空单止损触发 | 平仓价≈{curr_price:.2f} | PnL≈{pnl:+.1f}U"
        notify(msg)
        state["positions"] = []
        state["status"] = "idle"
        state["history"].append({
            "type": "short", "entry_usdc": entry_usdc,
            "close_price": curr_price, "qty": remaining,
            "pnl": pnl, "reason": "SL",
            "closed_at": dt.datetime.utcnow().strftime("%m-%d %H:%M UTC")
        })
        save_state(state)
        return state

    # ── TP1 触发（价格 <= tp1_usdc 且未执行过）───────────────────
    if not tp1_done and curr_price <= tp1_usdc:
        qty_close = round(remaining * TP1_CLOSE_RATIO, 3)
        qty_left  = round(remaining - qty_close, 3)
        log(f"[TP1] 触发！现价{curr_price:.2f} <= TP1{tp1_usdc:.2f}，平仓{qty_close}ETH")
        cancel_all_open(SYM_TRADE)
        cancel_all_algo(SYM_TRADE)
        try:
            place_close_order(qty_close, curr_price - 0.5, label="TP1")
        except Exception as e:
            log(f"[TP1_ERR] {e}")
        # 移动止损收到入场价+20（保本）
        new_trail = round(entry_usdc + 20, 2)
        pnl_tp1   = (entry_usdc - curr_price) * qty_close
        msg = (f"✅ ETH空单 TP1触发 | 平{qty_close}ETH @ ≈{curr_price:.2f} | "
               f"PnL≈{pnl_tp1:+.1f}U | 剩余{qty_left}ETH 移动止损收到{new_trail}")
        notify(msg)
        pos["tp1_done"]     = True
        pos["remaining_qty"]= qty_left
        pos["trail_sl"]     = new_trail
        pos["low_price"]    = curr_price   # 追踪最低价用于移动止损
        save_state(state)
        return state

    # ── TP2 触发（价格 <= tp2_usdc）──────────────────────────────
    if tp1_done and curr_price <= tp2_usdc:
        log(f"[TP2] 触发！现价{curr_price:.2f} <= TP2{tp2_usdc:.2f}，全平 {remaining}ETH")
        cancel_all_open(SYM_TRADE)
        cancel_all_algo(SYM_TRADE)
        try:
            place_close_order(remaining, curr_price - 0.5, label="TP2")
        except Exception as e:
            log(f"[TP2_ERR] {e}")
        pnl = (entry_usdc - curr_price) * remaining
        msg = f"🎯 ETH空单 TP2触发 | 全平{remaining}ETH @ ≈{curr_price:.2f} | PnL≈{pnl:+.1f}U"
        notify(msg)
        state["positions"] = []
        state["status"] = "idle"
        state["history"].append({
            "type": "short", "entry_usdc": entry_usdc,
            "close_price": curr_price, "qty": remaining,
            "pnl": pnl, "reason": "TP2",
            "closed_at": dt.datetime.utcnow().strftime("%m-%d %H:%M UTC")
        })
        save_state(state)
        return state

    # ── 移动止损更新（TP1触发后，追踪最低价）────────────────────
    if tp1_done:
        low_price = pos.get("low_price", curr_price)
        if curr_price < low_price:
            pos["low_price"] = curr_price
            low_price = curr_price
        # 止损跟随最低价 + TRAIL_OFFSET
        new_trail = round(low_price + TRAIL_OFFSET, 2)
        if new_trail < trail_sl:   # 只允许止损向下移动（有利方向）
            log(f"[TRAIL] 移动止损更新 {trail_sl:.2f} → {new_trail:.2f}（最低价{low_price:.2f}+{TRAIL_OFFSET}）")
            pos["trail_sl"] = new_trail
            save_state(state)

    return state


# ── 入场信号扫描 ─────────────────────────────────────────────────
def try_enter(state: dict, d: dict, score: int, in_zone: bool,
              entry_low: float) -> dict:
    """当评分>=70且在做空区时，自动开仓"""
    if state.get("status") != "idle":
        return state
    if score < 70 or not in_zone:
        return state

    # BTC 大墙检查：若 BTC 离最近大墙 < 0.3%，暂缓入场
    btc_mark = float(api.get_mark_price("BTCUSDT")["markPrice"])
    btc_wall = d.get("btc_wall", 99999)
    if (btc_wall - btc_mark) / btc_mark < 0.003:
        log(f"[HOLD] BTC离大墙{btc_wall:.0f}太近({(btc_wall-btc_mark)/btc_mark*100:.2f}%)，暂缓入场")
        return state

    # 已有未成交挂单则不重复下
    try:
        open_orders = api.get_open_orders(SYM_TRADE)
        if any(o.get("side") == "SELL" for o in open_orders):
            log("[HOLD] 已有空单挂单，跳过")
            return state
    except:
        pass

    # 计算实际挂单参数
    _, _, spread = get_spread()
    entry_usdt = min(d["eth_mark"], TARGET_SHORT_HIGH)
    entry_usdc = to_usdc(entry_usdt, spread)
    sl_usdc    = round(entry_usdc + d["atr"] * SL_ATR_MULT + spread + 0.5, 2)
    tp1_usdc   = round(entry_usdc - TP_POINTS_1, 2)
    tp2_usdc   = round(entry_usdc - TP_POINTS_2, 2)
    trail_init = round(entry_usdc + 20, 2)

    log(f"[SIGNAL] 评分{score} 触发！入场 USDC={entry_usdc} SL={sl_usdc} TP1={tp1_usdc} TP2={tp2_usdc}")

    try:
        result = place_short_entry(entry_usdc, POS_INITIAL_ETH, spread)
        order_id = result.get("orderId")
        if not order_id:
            log(f"[ORDER_ERR] 下单返回异常: {result}")
            notify(f"❌ ETH空单下单失败: {result.get('msg', result)}")
            return state
    except Exception as e:
        log(f"[ORDER_ERR] 下单失败: {e}")
        notify(f"❌ ETH空单下单失败: {e}")
        return state

    # 入场单成功后立即挂 algo 止损/止盈
    try:
        place_algo_sl(POS_INITIAL_ETH, sl_usdc, label="entry-SL")
    except Exception as e:
        log(f"[ALGO_SL_ERR] 止损单挂单失败: {e}")
    try:
        place_algo_tp(POS_INITIAL_ETH, tp1_usdc, label="entry-TP1")
    except Exception as e:
        log(f"[ALGO_TP_ERR] 止盈单挂单失败: {e}")

    state["status"] = "pending"
    state["positions"] = [{
        "order_id":     order_id,
        "entry_usdt":   entry_usdt,
        "entry_usdc":   entry_usdc,
        "sl_usdc":      sl_usdc,
        "tp1_usdc":     tp1_usdc,
        "tp2_usdc":     tp2_usdc,
        "trail_sl":     sl_usdc,
        "total_qty":    float(POS_INITIAL_ETH),
        "remaining_qty":float(POS_INITIAL_ETH),
        "tp1_done":     False,
        "score":        score,
        "atr":          d["atr"],
        "rsi1h":        d["rsi1h"],
        "rsi4h":        d["rsi4h"],
        "opened_at":    dt.datetime.utcnow().strftime("%m-%d %H:%M UTC"),
    }]
    save_state(state)

    notify(
        f"📋 ETH空单已挂单\n"
        f"标的: {SYM_TRADE}  qty={POS_INITIAL_ETH}ETH\n"
        f"入场: {entry_usdc}  SL: {sl_usdc}  TP1: {tp1_usdc}  TP2: {tp2_usdc}\n"
        f"评分: {score}  RSI1H: {d['rsi1h']:.1f}  RSI4H: {d['rsi4h']:.1f}  ATR: {d['atr']:.2f}"
    )
    return state


def check_pending_fill(state: dict) -> dict:
    """检查 pending 状态的挂单是否已成交"""
    if state.get("status") != "pending":
        return state
    if not state.get("positions"):
        state["status"] = "idle"
        return state

    pos = state["positions"][0]
    order_id = pos.get("order_id")
    if not order_id:
        return state

    try:
        orders = api.get_open_orders(SYM_TRADE)
        still_open = any(o["orderId"] == order_id for o in orders)
        if not still_open:
            # 订单不在挂单列表里，可能已成交
            real_qty = get_position_qty(SYM_TRADE)
            if real_qty >= 0.001:
                log(f"[FILLED] 空单已成交！持仓={real_qty}ETH")
                state["status"] = "holding"
                pos["total_qty"]    = real_qty
                pos["remaining_qty"]= real_qty
                save_state(state)
                notify(f"✅ ETH空单成交 {real_qty}ETH @ {pos['entry_usdc']} | "
                       f"SL={pos['sl_usdc']} TP1={pos['tp1_usdc']}")
            else:
                log("[CANCELLED] 挂单已撤销或未成交，重置为idle")
                state["status"] = "idle"
                state["positions"] = []
                save_state(state)
    except Exception as e:
        log(f"[FILL_CHECK_ERR] {e}")

    return state


# ── 主循环 ───────────────────────────────────────────────────────
def main():
    log("=" * 60)
    log("ETH 做空全自动工作流启动")
    log(f"入场区={TARGET_SHORT_LOW}~{TARGET_SHORT_HIGH} USDT")
    log(f"TP1=-{TP_POINTS_1}点  TP2=-{TP_POINTS_2}点  SL={SL_ATR_MULT}×ATR")
    log(f"仓位: 初仓{POS_INITIAL_ETH}ETH 最大{POS_MAX_ETH}ETH")
    log("=" * 60)

    state = load_state()

    while True:
        try:
            status = state.get("status", "idle")

            if status == "idle":
                # 扫描入场信号
                d = fetch_data()
                score, in_zone, entry_low, entry_high = evaluate(d)
                log(f"[SCAN] ETH={d['eth_mark']:.1f} 评分={score} 区间={entry_low}~{entry_high} "
                    f"RSI4H={d['rsi4h']:.1f} RSI1H={d['rsi1h']:.1f} BTC_RSI={d['btc_rsi']:.1f} "
                    f"在区={'✅' if in_zone else '❌'}")
                state = try_enter(state, d, score, in_zone, entry_low)
                time.sleep(SCAN_INTERVAL)

            elif status == "pending":
                # 等待成交确认
                state = check_pending_fill(state)
                time.sleep(15)

            elif status == "holding":
                # 持仓监控
                state = monitor_position(state)
                # 检查是否已无持仓（全平后）
                if state.get("status") == "holding":
                    real_qty = get_position_qty(SYM_TRADE)
                    if real_qty < 0.001:
                        log("[CLEARED] 持仓已全部清空，切回 idle")
                        state["status"] = "idle"
                        state["positions"] = []
                        save_state(state)
                time.sleep(MONITOR_INTERVAL)

            else:
                log(f"[WARN] 未知状态 {status}，重置为 idle")
                state["status"] = "idle"
                save_state(state)
                time.sleep(SCAN_INTERVAL)

        except KeyboardInterrupt:
            log("工作流已手动停止。")
            break
        except Exception as e:
            log(f"[ERROR] {traceback.format_exc()}")
            time.sleep(15)


if __name__ == "__main__":
    main()
