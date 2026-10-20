"""
Vol Regime Validator
验证 Deribit DVOL 接口连通性 + 数据质量
对比 IV (DVOL) vs RV (自算) 的历史相关性
"""

import json
import time
import math
import urllib.request
import urllib.error
from datetime import datetime, timezone

PROXY = "socks5://REDACTED_PROXY_CREDENTIALS"

# ── 配置 ──────────────────────────────────────────────
BINANCE_KLINE_URL = "https://fapi.binance.com/fapi/v1/klines"
DERIBIT_DVOL_URL  = "https://www.deribit.com/api/v2/public/get_volatility_index_data"
SYMBOL   = "BTCUSDT"
INTERVAL = "1d"       # 日线，算30日RV
LIMIT    = 60         # 取60天K线
CURRENCY = "BTC"
DVOL_RES = "1D"        # Deribit resolution 字符串

# ── 工具函数 ──────────────────────────────────────────

def _make_opener():
    """支持 socks5 proxy 的 opener"""
    try:
        import socks
        import socket
        parts = PROXY.replace("socks5://", "").split("@")
        auth, host_port = parts[0], parts[1]
        user, pwd = auth.split(":")
        host, port = host_port.split(":")
        socks.set_default_proxy(socks.SOCKS5, host, int(port), username=user, password=pwd)
        socket.socket = socks.socksocket
        print(f"  [proxy] socks5 proxy configured: {host}:{port}")
        return urllib.request.build_opener()
    except ImportError:
        print("  [proxy] PySocks not installed, trying direct connection")
        return urllib.request.build_opener()


def http_get(url, params=None, timeout=15):
    if params:
        query = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{url}?{query}"
    opener = _make_opener()
    req = urllib.request.Request(url, headers={"User-Agent": "vol-validator/1.0"})
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


# ── 1. 拉 Deribit DVOL ───────────────────────────────

def fetch_dvol(days=60):
    """返回 [(timestamp_ms, open, high, low, close), ...] 近 days 天"""
    end_ts   = int(time.time() * 1000)
    start_ts = end_ts - days * 86400 * 1000
    params = {
        "currency":   CURRENCY,
        "start_timestamp": start_ts,
        "end_timestamp":   end_ts,
        "resolution": DVOL_RES,
    }
    data = http_get(DERIBIT_DVOL_URL, params)
    # result.data = [[ts, open, high, low, close], ...]
    rows = data.get("result", {}).get("data", [])
    return rows   # each: [ts_ms, o, h, l, c]


# ── 2. 拉 Binance 日线 K 线，算 RV ──────────────────

def fetch_binance_closes(days=60):
    """返回 [(open_time_ms, close_price), ...]"""
    params = {
        "symbol":   SYMBOL,
        "interval": INTERVAL,
        "limit":    days + 5,
    }
    rows = http_get(BINANCE_KLINE_URL, params)
    # each row: [open_time, open, high, low, close, ...]
    return [(int(r[0]), float(r[4])) for r in rows]


def calc_rv(closes, window=30):
    """年化 RV (%)，返回 [(date_str, rv_pct), ...] 从第 window 天开始"""
    results = []
    for i in range(window, len(closes)):
        slice_ = closes[i - window: i]
        log_rets = [math.log(slice_[j][1] / slice_[j-1][1]) for j in range(1, len(slice_))]
        std = math.sqrt(sum(r**2 for r in log_rets) / len(log_rets))   # daily std
        rv_ann = std * math.sqrt(365) * 100
        ts_ms  = closes[i][0]
        date_s = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        results.append((ts_ms, date_s, round(rv_ann, 2)))
    return results


# ── 3. 对齐 DVOL 和 RV，输出对比 ────────────────────

def align_and_compare(dvol_rows, rv_rows):
    """按日期对齐，输出 IV/RV 对比表"""
    # dvol_rows: [ts_ms, o, h, l, close_iv]
    dvol_map = {}
    for row in dvol_rows:
        ts_ms   = row[0]
        close_v = row[4]
        date_s  = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        dvol_map[date_s] = round(float(close_v), 2)

    results = []
    for (ts_ms, date_s, rv) in rv_rows:
        iv = dvol_map.get(date_s)
        if iv is None:
            continue
        ratio = round(iv / rv, 3) if rv > 0 else None
        regime = "vol_rich" if ratio and ratio > 1.2 else \
                 "vol_cheap" if ratio and ratio < 0.85 else "neutral"
        results.append({
            "date":   date_s,
            "IV_dvol": iv,
            "RV_30d":  rv,
            "IV/RV":   ratio,
            "regime":  regime,
        })
    return results


def pearson_corr(pairs):
    n = len(pairs)
    if n < 2:
        return None
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in pairs)
    dx  = math.sqrt(sum((x - mx)**2 for x in xs))
    dy  = math.sqrt(sum((y - my)**2 for y in ys))
    if dx == 0 or dy == 0:
        return None
    return round(num / (dx * dy), 4)


# ── main ─────────────────────────────────────────────

def main():
    print("=" * 60)
    print("Vol Regime Validator — IV vs RV")
    print("=" * 60)

    # Step 1: Deribit DVOL
    print("\n[1] 拉取 Deribit DVOL (BTC, 60天日线) ...")
    try:
        dvol_rows = fetch_dvol(days=60)
        print(f"  ✅ 获取 {len(dvol_rows)} 条 DVOL 数据")
        if dvol_rows:
            last = dvol_rows[-1]
            last_date = datetime.fromtimestamp(last[0]/1000, tz=timezone.utc).strftime("%Y-%m-%d")
            print(f"  最新 DVOL: {last[4]:.1f}%  ({last_date})")
    except Exception as e:
        print(f"  ❌ DVOL 拉取失败: {e}")
        dvol_rows = []

    # Step 2: Binance K线 + RV
    print("\n[2] 拉取 Binance 日线 K线 (BTCUSDT, 60天) ...")
    try:
        closes = fetch_binance_closes(days=60)
        print(f"  ✅ 获取 {len(closes)} 根 K 线")
        rv_rows = calc_rv(closes, window=30)
        print(f"  ✅ 计算 RV_30d: {len(rv_rows)} 条")
        if rv_rows:
            print(f"  最新 RV_30d: {rv_rows[-1][2]:.1f}%  ({rv_rows[-1][1]})")
    except Exception as e:
        print(f"  ❌ Binance K线拉取失败: {e}")
        closes, rv_rows = [], []

    if not dvol_rows or not rv_rows:
        print("\n⚠️  数据不完整，终止分析")
        return

    # Step 3: 对比
    print("\n[3] 对齐 IV vs RV，输出近20天对比 ...")
    compared = align_and_compare(dvol_rows, rv_rows)

    if not compared:
        print("  ⚠️  没有匹配的日期数据")
        return

    # 打印最近20条
    header = f"{'Date':<12} {'IV(DVOL)':>9} {'RV_30d':>9} {'IV/RV':>7}  {'Regime'}"
    print("  " + header)
    print("  " + "-" * 55)
    for row in compared[-20:]:
        print(f"  {row['date']:<12} {row['IV_dvol']:>8.1f}% {row['RV_30d']:>8.1f}% {row['IV/RV']:>7.3f}  {row['regime']}")

    # Step 4: 相关性
    pairs = [(r["IV_dvol"], r["RV_30d"]) for r in compared if r["IV/RV"] is not None]
    corr = pearson_corr(pairs)
    print(f"\n[4] IV vs RV Pearson 相关系数: {corr}")
    if corr is not None:
        if corr > 0.7:
            print("  ✅ 强正相关 — DVOL 是可靠的 RV 预测器，适合用作信号层")
        elif corr > 0.4:
            print("  ⚠️  中等相关 — 有参考价值，但需结合其他信号")
        else:
            print("  ❌ 弱相关 — DVOL 和历史RV偏离较大，需要谨慎使用")

    # Step 5: Regime 分布
    from collections import Counter
    regime_cnt = Counter(r["regime"] for r in compared)
    total = len(compared)
    print(f"\n[5] Regime 分布 (近{total}天):")
    for regime in ["vol_cheap", "neutral", "vol_rich"]:
        cnt = regime_cnt.get(regime, 0)
        pct = cnt / total * 100
        bar = "█" * int(pct / 5)
        print(f"  {regime:<12} {cnt:>3}天  {pct:5.1f}%  {bar}")

    # 当前状态
    latest = compared[-1]
    print(f"\n{'='*60}")
    print(f"📊 当前状态 ({latest['date']})")
    print(f"   IV (DVOL): {latest['IV_dvol']:.1f}%")
    print(f"   RV (30d):  {latest['RV_30d']:.1f}%")
    print(f"   IV/RV:     {latest['IV/RV']:.3f}")
    print(f"   Regime:    {latest['regime'].upper()}")
    if latest["regime"] == "vol_rich":
        print("   → 波动率被高估，建议收缩敞口 (仓位×0.8, 格距×1.2)")
    elif latest["regime"] == "vol_cheap":
        print("   → 波动率被低估，可适度扩张 (仓位×1.1, 格距×0.9)")
    else:
        print("   → 波动率中性，维持现有仓位参数")
    print("=" * 60)


if __name__ == "__main__":
    main()
