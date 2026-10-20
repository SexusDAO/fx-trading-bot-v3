"""
fetch_week_data.py — 拉取 2026-07-28~2026-08-03 一周期货K线数据
直接访问 Binance Futures 公共 API（无需 API Key）
写入 /tmp/sim_week_data.json
"""

import json, time, requests
from datetime import datetime, timezone, timedelta

BASE = "https://fapi.binance.com"
SESS = requests.Session()
SESS.headers.update({"User-Agent": "BinanceAIPro/1.0"})

START_MS = 1753660800000   # 2026-07-28 00:00:00 UTC
END_MS   = 1754265600000   # 2026-08-04 00:00:00 UTC

SYMBOLS = [
    "BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT",
    "DOGEUSDT","ADAUSDT","AVAXUSDT","TRXUSDT","LINKUSDT",
    "DOTUSDT","LTCUSDT","UNIUSDT","ATOMUSDT","XLMUSDT",
    "ETCUSDT","BCHUSDT","APTUSDT","ARBUSDT","OPUSDT",
    "SEIUSDT","SUIUSDT","INJUSDT","TIAUSDT","WLDUSDT",
    "ORDIUSDT","STXUSDT","RUNEUSDT","ARUSDT","JTOUSDT",
    "PYTHUSDT","ALTUSDT","JUPUSDT","DYMUSDT","WUSDT",
    "MANTAUSDT","LISTAUSDT","BOMEUSDT","NOTUSDT","HMSTRUSDT",
    "EIGENUSDT","MOODENGUSDT","PNUTUSDT","ACTUSDT","PENGUUSDT",
    "MOVEUSDT","VIRTUALUSDT","GRIFFAINUSDT","TRUMPUSDT",
    "BERAUSDT","IPUSDT","REDUSDT","BICOUSDT","BABAUSDT",
    "BANKUSDT","TSTUSDT","1000SHIBUSDT","1000PEPEUSDT",
    "TAOUSDT","NEARUSDT","FILUSDT","SANDUSDT","MANAUSDT",
    "GALAUSDT","APEUSDT","FETUSDT","AGIXUSDT","RENDERUSDT",
]

DAY_KEYS = ['0728','0729','0730','0731','0801','0802','0803']
DAY_START = {
    '0728': 1753660800000,
    '0729': 1753747200000,
    '0730': 1753833600000,
    '0731': 1753920000000,
    '0801': 1754006400000,
    '0802': 1754092800000,
    '0803': 1754179200000,
}

def get(url, params, retries=3):
    for i in range(retries):
        try:
            r = SESS.get(url, params=params, timeout=15)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                time.sleep(2)
        except Exception as e:
            if i == retries-1: raise
            time.sleep(1)
    return []

def fetch_klines(symbol, interval, start_ms, end_ms, limit=1000):
    url = f"{BASE}/fapi/v1/klines"
    all_bars = []
    cur = start_ms
    while cur < end_ms:
        bars = get(url, {"symbol": symbol, "interval": interval,
                         "startTime": cur, "endTime": end_ms-1, "limit": limit})
        if not bars or not isinstance(bars, list):
            break
        all_bars.extend(bars)
        cur = int(bars[-1][0]) + 1
        if len(bars) < limit:
            break
        time.sleep(0.02)
    return all_bars

def fetch_funding(symbol, start_ms, end_ms):
    url = f"{BASE}/fapi/v1/fundingRate"
    rates = get(url, {"symbol": symbol, "startTime": start_ms,
                      "endTime": end_ms-1, "limit": 200})
    return rates if isinstance(rates, list) else []

def main():
    result = {'days': DAY_KEYS, 'kdata': {}, 'fdata': {}}
    bg_start = START_MS - 48 * 3600 * 1000  # 4H 背景多拉 48H

    total = len(SYMBOLS)
    ok_count = 0
    for i, sym in enumerate(SYMBOLS):
        print(f"  [{i+1:2d}/{total}] {sym:20s}", end=' ', flush=True)
        sym_data = {'1h_by_day': {}, '4h': []}

        # 4H 背景（最多取 20 根）
        try:
            bars_4h = fetch_klines(sym, '4h', bg_start, END_MS, limit=80)
            sym_data['4h'] = [[b[0],b[1],b[2],b[3],b[4],b[5]] for b in bars_4h]
            print(f"4H:{len(bars_4h):2d}", end=' ', flush=True)
        except Exception as e:
            print(f"4H-err:{e} ", end=' ', flush=True)

        # 每天 1H K线
        for dk in DAY_KEYS:
            d_start = DAY_START[dk]
            d_end   = d_start + 24 * 3600 * 1000
            try:
                bars_1h = fetch_klines(sym, '1h', d_start, d_end, limit=24)
                sym_data['1h_by_day'][dk] = [
                    [b[0],b[1],b[2],b[3],b[4],b[5]] for b in bars_1h
                ]
            except:
                pass

        days_ok = sum(1 for dk in DAY_KEYS if sym_data['1h_by_day'].get(dk))
        result['kdata'][sym] = sym_data

        # 资金费率
        try:
            rates = fetch_funding(sym, START_MS, END_MS)
            result['fdata'][sym] = rates
        except:
            result['fdata'][sym] = []

        fr_cnt = len(result['fdata'].get(sym, []))
        ok_count += (1 if days_ok == 7 else 0)
        print(f"days:{days_ok}/7  fr:{fr_cnt:2d} ✓")

    with open('/tmp/sim_week_data.json', 'w') as f:
        json.dump(result, f)

    size_mb = len(json.dumps(result)) / 1024 / 1024
    print(f"\n✅ 数据写入 /tmp/sim_week_data.json  ({size_mb:.1f} MB)")
    print(f"   标的: {len(result['kdata'])}  完整7天: {ok_count}")

if __name__ == '__main__':
    main()
