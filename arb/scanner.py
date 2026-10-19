"""
arb/scanner.py — 资金费率扫描（只查询，绝不下单）

✨ 2026-08-28: 接入 Clawby funding_rate_arbitrage 跨所套利信号
  - scan_cross_exchange_arb() 扫描跨交易所资金费率套利机会
  - get_upcoming_macro_events() 拉取近期宏观事件，重大事件前暂停套利
  - get_oi_liquidity_check()   检查标的 OI 流动性
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import requests
from arb.config import PROXIES, ARB_MIN_RATE_8H, ARB_MIN_VOLUME_M

# Clawby 套利接口
try:
    from fx.shared.clawby_adapter import (
        get_funding_arb_opps, get_funding_exchange_list,
        get_oi_exchange_list, get_economic_calendar,
    )
    _CLAWBY_OK = True
except ImportError:
    _CLAWBY_OK = False


def scan_binance_funding(min_rate_8h: float = None, min_vol_m: float = None,
                         check_spot: bool = True) -> list:
    """
    扫描币安期货资金费率，返回排名靠前的套利候选。
    check_spot=True 时过滤掉在币安现货不存在的标的（无法做现货+合约对冲）。
    纯查询，不调用任何下单接口。
    """
    if min_rate_8h is None:
        min_rate_8h = ARB_MIN_RATE_8H
    if min_vol_m is None:
        min_vol_m = ARB_MIN_VOLUME_M

    # 获取资金费率
    r = requests.get("https://fapi.binance.com/fapi/v1/premiumIndex",
                     proxies=PROXIES, timeout=15)
    rates = {item["symbol"]: float(item.get("lastFundingRate", 0))
             for item in r.json() if "USDT" in item.get("symbol", "")}

    # 获取期货交易量
    r2 = requests.get("https://fapi.binance.com/fapi/v1/ticker/24hr",
                      proxies=PROXIES, timeout=15)
    vols = {t["symbol"]: float(t.get("quoteVolume", 0)) / 1e6
            for t in r2.json()}

    # 获取现货可交易标的列表（过滤掉纯合约、无现货对应的标的）
    spot_symbols = set()
    if check_spot:
        r3 = requests.get("https://api.binance.com/api/v3/exchangeInfo",
                          proxies=PROXIES, timeout=15)
        for s in r3.json().get("symbols", []):
            if s.get("status") == "TRADING" and s.get("quoteAsset") == "USDT":
                spot_symbols.add(s["symbol"])

    candidates = []
    for sym, rate in rates.items():
        vol = vols.get(sym, 0)
        if rate * 100 < min_rate_8h or vol < min_vol_m:
            continue
        if check_spot and sym not in spot_symbols:
            continue   # 现货不存在，无法做同所现货+合约对冲
        candidates.append({
            "symbol":      sym,
            "rate_8h":     round(rate * 100, 4),
            "ann_pct":     round(rate * 3 * 365 * 100, 1),
            "vol_m":       round(vol, 1),
            "spot_avail":  True,
        })

    candidates.sort(key=lambda x: -x["rate_8h"])
    return candidates


def print_candidates(candidates: list, top: int = 10):
    print(f"\n{'Symbol':<22} {'Rate/8h':>9} {'Ann%':>8} {'Vol(M$)':>10}")
    print("-" * 55)
    for c in candidates[:top]:
        print(f"{c['symbol']:<22} {c['rate_8h']:>8.3f}% {c['ann_pct']:>7.1f}% {c['vol_m']:>9.0f}M")



def scan_cross_exchange_arb(min_rate_diff: float = 0.05,
                             min_oi_m: float = 5.0,
                             top_n: int = 20) -> list:
    """
    ✨ 新增：基于 Clawby futures_funding_rate_arbitrage 的跨所套利扫描。
    返回按费率差降序排列的套利机会列表。
    """
    if not _CLAWBY_OK:
        return []
    try:
        opps = get_funding_arb_opps(top_n=100)
    except Exception:
        return []

    results = []
    for op in opps:
        symbol = op.get("symbol", "")
        buy    = op.get("buy",  {})
        sell   = op.get("sell", {})
        if not buy or not sell:
            continue

        # 实际字段名：funding_rate（百分比已）
        buy_rate  = float(buy.get("funding_rate",  0))
        sell_rate = float(sell.get("funding_rate", 0))
        rate_diff = abs(sell_rate - buy_rate)
        oi_usd    = float(buy.get("open_interest_usd", 0))

        if rate_diff < min_rate_diff:
            continue
        if oi_usd < min_oi_m * 1e6:
            continue

        results.append({
            "symbol":        symbol,
            "buy_exchange":  buy.get("exchange",  ""),
            "buy_rate":      buy_rate,
            "sell_exchange": sell.get("exchange", ""),
            "sell_rate":     sell_rate,
            "rate_diff":     round(rate_diff, 6),
            "oi_usd":        oi_usd,
            # funding_rate 已是百分比，假设每8h结算则年化 = diff * 3 * 365
            "ann_pct":       round(rate_diff * 3 * 365, 2),
        })

    results.sort(key=lambda x: -x["rate_diff"])
    return results[:top_n]


def get_upcoming_macro_events(hours_ahead: int = 48) -> list:
    """
    ✨ 新增：获取未来 hours_ahead 小时内的高影响宏观事件。
    重要事件（importance_level >= 3）前建议暂停开新套利仓。
    返回: [{calendar_name, country_code, publish_timestamp, importance_level}, ...]
    """
    if not _CLAWBY_OK:
        return []
    import time
    try:
        events   = get_economic_calendar(limit=50)
        now_ms   = time.time() * 1000
        cutoff   = (time.time() + hours_ahead * 3600) * 1000
        upcoming = [
            e for e in events
            if now_ms <= float(e.get("publish_timestamp", 0)) <= cutoff
            and int(e.get("importance_level", 0)) >= 3
        ]
        return upcoming
    except Exception:
        return []


def get_oi_liquidity_check(symbols: list) -> dict:
    """
    ✨ 新增：批量检查标的在各交易所的 OI，过滤流动性过小的标的。
    返回: {symbol: oi_usd_total}
    """
    if not _CLAWBY_OK:
        return {}
    result = {}
    for sym in symbols:
        try:
            oi_list = get_oi_exchange_list(sym)
            total   = sum(float(e.get("open_interest_usd", 0)) for e in oi_list
                         if e.get("exchange", "") != "All")
            result[sym] = total
        except Exception:
            result[sym] = 0.0
    return result



    results = scan_binance_funding()
    print_candidates(results)
    print("\n=== Clawby 跨所套利机会 (Top 5) ===")
    for op in scan_cross_exchange_arb(top_n=5):
        print(f"  {op['symbol']:<20} 买:{op['buy_exchange']}({op['buy_rate']:+.4f}%) "
              f"卖:{op['sell_exchange']}({op['sell_rate']:+.4f}%) "
              f"差:{op['rate_diff']:.4f}% OI:{op['oi_usd']/1e6:.1f}M")  
