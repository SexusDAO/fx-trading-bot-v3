#!/usr/bin/env python3
"""
tradfi-daily/execute_tradfi_open.py
====================================
TradFi 美股开盘自动入场脚本

流程：
  1. 运行 tradfi-daily pipeline 获取最新候选标的
  2. 筛选：成交额 > 500M + 4h 趋势确认
  3. 按仓位规则计算入场参数（支撑位 + ATR）
  4. 自动挂限价入场单 + Algo SL/TP1/TP2

仓位规则：
  Top1（成交额最大）: 700U × 3x
  Top2:               500U × 3x
  Top3:               300U × 3x

入场价: 近4根1h最低支撑上方 0.1%
SL:    支撑位下方 0.5×ATR(1h)
TP1:   入场价 + 1.5×ATR（平50%仓）
TP2:   入场价 + 2.5×ATR（平剩余）
"""

import sys, os, json, time, math, subprocess, requests, hmac, hashlib, urllib.parse
sys.path.insert(0, "/root/.openclaw/workspace")
import binance_api as api

# ── 配置 ─────────────────────────────────────────────────
KEY    = "REDACTED_API_KEY"
SECRET = "REDACTED_API_SECRET"
PROXY  = "socks5://REDACTED_PROXY_CREDENTIALS"
PROXIES = {"https": PROXY, "http": PROXY}
BASE_FAPI = "https://fapi.binance.com"

LEVERAGE        = 3
MIN_VOLUME_M    = 500       # 最低成交额门槛（百万U）
MAX_POSITIONS   = 3         # 最多同时开几个
NOTIONAL_TIERS  = [700, 500, 300]   # Top1/Top2/Top3 仓位（U）
REPORT_PATH     = "/root/.openclaw/workspace/tradfi-daily/tradfi_report.json"
PIPELINE_DIR    = "/app/skills/bn-strategy-cases/cases/tradfi-daily/scripts"

DRY_RUN = "--dry-run" in sys.argv

# ── 工具函数 ──────────────────────────────────────────────
def fapi_get(path, params={}):
    p = dict(params)
    p["timestamp"] = int(time.time() * 1000)
    qs = urllib.parse.urlencode(p)
    sig = hmac.new(SECRET.encode(), qs.encode(), hashlib.sha256).hexdigest()
    r = requests.get(f"{BASE_FAPI}{path}?{qs}&signature={sig}",
                     headers={"X-MBX-APIKEY": KEY}, proxies=PROXIES, timeout=10)
    return r.json()

def fapi_post(path, params):
    p = dict(params)
    p["timestamp"] = int(time.time() * 1000)
    qs = urllib.parse.urlencode(p)
    sig = hmac.new(SECRET.encode(), qs.encode(), hashlib.sha256).hexdigest()
    r = requests.post(f"{BASE_FAPI}{path}?{qs}&signature={sig}",
                      headers={"X-MBX-APIKEY": KEY}, proxies=PROXIES, timeout=10)
    return r.json()

def get_klines(sym, interval, limit=30):
    r = requests.get(f"{BASE_FAPI}/fapi/v1/klines",
                     params={"symbol": sym, "interval": interval, "limit": limit},
                     proxies=PROXIES, timeout=10)
    return r.json()

def atr(klines, n=14):
    trs = [float(k[2]) - float(k[3]) for k in klines[-n:]]
    return sum(trs) / len(trs) if trs else 0

def get_filters(sym):
    info = requests.get(f"{BASE_FAPI}/fapi/v1/exchangeInfo",
                        proxies=PROXIES, timeout=10).json()
    for s in info["symbols"]:
        if s["symbol"] == sym:
            qty_step = price_tick = 0.01
            for f in s["filters"]:
                if f["filterType"] == "LOT_SIZE":
                    qty_step = float(f["stepSize"])
                if f["filterType"] == "PRICE_FILTER":
                    price_tick = float(f["tickSize"])
            return qty_step, price_tick
    return 0.01, 0.01

def rp(p, tick):
    dec = max(0, -int(math.floor(math.log10(tick)))) if 0 < tick < 1 else 0
    return round(round(p / tick) * tick, dec)

def rq(q, step):
    dec = max(0, -int(math.floor(math.log10(step)))) if 0 < step < 1 else 0
    return round(math.floor(q / step) * step, dec)

def log(msg):
    print(msg, flush=True)

# ── Step 1: 运行 pipeline 获取最新数据 ───────────────────
def run_pipeline():
    log("[1/5] 运行 tradfi-daily pipeline...")
    r = subprocess.run(
        ["python3", "run_pipeline.py", "--top", "5"],
        cwd=PIPELINE_DIR, capture_output=True, text=True, timeout=60
    )
    if r.returncode != 0:
        raise RuntimeError(f"pipeline 失败: {r.stderr[:200]}")
    log(f"      pipeline 完成")

# ── Step 2: 读报告 + 筛选候选标的 ───────────────────────
def select_candidates(report):
    merged = report["discovery"]["merged"]

    candidates = []
    for x in merged:
        vol_m = x.get("volume_24h_quote", 0) / 1e6
        if vol_m < MIN_VOLUME_M:
            log(f"  跳过 {x['ticker']}: 成交额 {vol_m:.0f}M < {MIN_VOLUME_M}M")
            continue
        candidates.append(x)

    log(f"[2/5] 候选标的（成交额>{MIN_VOLUME_M}M）: {[c['ticker'] for c in candidates]}")
    return candidates[:MAX_POSITIONS]

# ── Step 3: 技术分析 — 支撑位 + ATR + 4h趋势 ────────────
def analyze(sym, ticker):
    k1h = get_klines(sym, "1h", 30)
    k4h = get_klines(sym, "4h", 10)

    price    = float(k1h[-1][4])
    atr_1h   = atr(k1h, 14)
    atr_pct  = atr_1h / price * 100

    # 近4根1h 低点支撑
    support  = min(float(k[3]) for k in k1h[-4:])

    # 4h 趋势（最新收盘 vs 3根前收盘）
    closes_4h = [float(k[4]) for k in k4h[-5:]]
    trend_4h  = "up" if closes_4h[-1] > closes_4h[-3] else "down"

    # 入场价 = 支撑上方 0.1%
    entry = round(support * 1.001, 4)

    # SL = 支撑下方 0.5×ATR
    sl    = round(support - atr_1h * 0.5, 4)

    # TP1 = 入场 + 1.5×ATR，TP2 = 入场 + 2.5×ATR
    tp1   = round(entry + atr_1h * 1.5, 4)
    tp2   = round(entry + atr_1h * 2.5, 4)

    return {
        "sym": sym, "ticker": ticker, "price": price,
        "atr_1h": atr_1h, "atr_pct": atr_pct,
        "support": support, "trend_4h": trend_4h,
        "entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2,
    }

# ── Step 4: 挂限价入场单 + Algo SL/TP ───────────────────
def place_orders(info, notional):
    sym    = info["sym"]
    entry  = info["entry"]
    sl_px  = info["sl"]
    tp1_px = info["tp1"]
    tp2_px = info["tp2"]

    qty_step, price_tick = get_filters(sym)
    qty      = rq(notional * LEVERAGE / entry, qty_step)
    qty_tp1  = rq(qty * 0.5, qty_step)
    qty_tp2  = rq(qty - qty_tp1, qty_step)

    entry_r  = rp(entry,  price_tick)
    sl_r     = rp(sl_px,  price_tick)
    tp1_r    = rp(tp1_px, price_tick)
    tp2_r    = rp(tp2_px, price_tick)

    if DRY_RUN:
        log(f"  [DRY] {sym} qty={qty} entry={entry_r} sl={sl_r} tp1={tp1_r} tp2={tp2_r}")
        return {"dry_run": True, "sym": sym, "qty": qty,
                "entry": entry_r, "sl": sl_r, "tp1": tp1_r, "tp2": tp2_r}

    # 设杠杆
    fapi_post("/fapi/v1/leverage", {"symbol": sym, "leverage": LEVERAGE})

    # 入场限价单
    r_entry = fapi_post("/fapi/v1/order", {
        "symbol": sym, "side": "BUY", "positionSide": "LONG",
        "type": "LIMIT", "timeInForce": "GTC",
        "quantity": str(qty), "price": str(entry_r),
    })
    entry_oid = r_entry.get("orderId")
    if not entry_oid:
        raise RuntimeError(f"{sym} 入场单失败: {r_entry}")
    log(f"  入场限价单: orderId={entry_oid} price={entry_r} qty={qty}")

    # Algo SL
    r_sl = api.place_algo_order(sym, "SELL", "STOP_MARKET", sl_r, "LONG", qty)
    log(f"  SL:  algoId={r_sl.get('algoId','❌')} triggerPx={sl_r}")

    # Algo TP1 50%
    r_tp1 = api.place_algo_order(sym, "SELL", "TAKE_PROFIT_MARKET", tp1_r, "LONG", qty_tp1)
    log(f"  TP1: algoId={r_tp1.get('algoId','❌')} triggerPx={tp1_r} qty={qty_tp1}")

    # Algo TP2 剩余
    r_tp2 = api.place_algo_order(sym, "SELL", "TAKE_PROFIT_MARKET", tp2_r, "LONG", qty_tp2)
    log(f"  TP2: algoId={r_tp2.get('algoId','❌')} triggerPx={tp2_r} qty={qty_tp2}")

    return {
        "sym": sym, "ticker": info["ticker"],
        "entry_oid": entry_oid, "qty": qty,
        "sl_algo": r_sl.get("algoId"), "tp1_algo": r_tp1.get("algoId"),
        "tp2_algo": r_tp2.get("algoId"),
        "entry": entry_r, "sl": sl_r, "tp1": tp1_r, "tp2": tp2_r,
        "notional": notional, "leverage": LEVERAGE,
        "rr": round(abs(tp1_r - entry_r) / abs(entry_r - sl_r), 1) if abs(entry_r - sl_r) > 0 else 0,
    }

# ── 主流程 ────────────────────────────────────────────────
def main():
    # Step 1: 运行 pipeline
    run_pipeline()

    # Step 2: 读报告
    with open(REPORT_PATH) as f:
        report = json.load(f)
    log(f"      报告时间: {report['meta']['generated_at']}")

    candidates = select_candidates(report)
    if not candidates:
        log("❌ 无满足条件的候选标的，退出")
        return

    # Step 3: 技术分析 + 过滤
    log(f"[3/5] 技术分析（支撑位 + ATR + 4h趋势）...")
    valid = []
    for i, cand in enumerate(candidates):
        sym    = cand["fapi_symbol"]
        ticker = cand["ticker"]
        try:
            info = analyze(sym, ticker)
            trend = info["trend_4h"]

            # 4h 下跌不开多
            if trend == "down":
                log(f"  跳过 {ticker}: 4h趋势下跌，不做多")
                continue

            # 当前价格必须高于入场价（挂低等回调，不追高）
            if info["price"] < info["entry"] * 0.995:
                log(f"  跳过 {ticker}: 当前价 {info['price']:.4f} 已低于支撑，风险过高")
                continue

            notional = NOTIONAL_TIERS[len(valid)] if len(valid) < len(NOTIONAL_TIERS) else NOTIONAL_TIERS[-1]
            info["notional"] = notional
            valid.append(info)
            log(f"  ✅ {ticker} price={info['price']:.4f} support={info['support']:.4f} "
                f"entry={info['entry']:.4f} sl={info['sl']:.4f} tp1={info['tp1']:.4f} "
                f"atr={info['atr_pct']:.2f}% trend4h={trend} notional={notional}U")
        except Exception as e:
            log(f"  ❌ {ticker} 分析失败: {e}")
        time.sleep(0.2)

    if not valid:
        log("❌ 技术过滤后无可交易标的")
        return

    # Step 4: 检查账户余额
    log(f"[4/5] 账户检查...")
    bal = fapi_get("/fapi/v2/balance")
    avail = 0
    for b in (bal if isinstance(bal, list) else []):
        if b.get("asset") == "USDT":
            avail = float(b.get("availableBalance", 0))
    log(f"      可用余额: {avail:.2f}U")

    total_needed = sum(v["notional"] for v in valid)
    if avail < total_needed * 1.1 and not DRY_RUN:
        log(f"⚠️  余额 {avail:.0f}U 可能不足（需要约 {total_needed:.0f}U），继续执行...")

    # Step 5: 下单
    log(f"[5/5] 挂单执行（{'DRY RUN' if DRY_RUN else '实盘'}）...")
    results = []
    for info in valid:
        log(f"\n  ── {info['ticker']} ({info['sym']}) {info['notional']}U×{LEVERAGE}x ──")
        try:
            r = place_orders(info, info["notional"])
            results.append(r)
        except Exception as e:
            log(f"  ❌ {info['ticker']} 下单失败: {e}")
        time.sleep(0.5)

    # 结果汇总
    log(f"\n{'='*55}")
    log(f"  TradFi 开仓完成  {'[DRY RUN]' if DRY_RUN else ''}")
    log(f"{'='*55}")
    for r in results:
        if r.get("dry_run"):
            log(f"  [DRY] {r['sym']}  qty={r['qty']}  entry={r['entry']}  "
                f"sl={r['sl']}  tp1={r['tp1']}  tp2={r['tp2']}")
        else:
            log(f"  ✅ {r['ticker']:10s} entry={r['entry']:>10.4f}  sl={r['sl']:>10.4f}  "
                f"tp1={r['tp1']:>10.4f}  tp2={r['tp2']:>10.4f}  "
                f"qty={r['qty']}  {r['notional']}U×{r['leverage']}x  RR={r['rr']}x")
    log(f"{'='*55}")

    # 保存结果
    out_path = "/root/.openclaw/workspace/tradfi-daily/last_open.json"
    with open(out_path, "w") as f:
        json.dump({"timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   "results": results}, f, indent=2, ensure_ascii=False)
    log(f"  结果已保存: {out_path}")

if __name__ == "__main__":
    main()
