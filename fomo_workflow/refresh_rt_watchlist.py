#!/usr/bin/env python3
"""
refresh_rt_watchlist.py
从 robinhoodtrenches + windvane 自动补充高质量聪明钱地址到 evm_watchlist.json

数据源1: robinhoodtrenches.com/api/traders  — 按 realized_pnl 筛选
数据源2: wind.jokkimon.club accounts_index  — 按 active（近期交易次数）筛选

筛选条件:
  RT: realized_pnl > 10000 且 (win_rate > 0.35 或 closed_trades >= 3)
  WV: active >= 50
"""
import json, requests, sys, os
from datetime import datetime, timezone

WATCHLIST = os.path.join(os.path.dirname(__file__), 'evm_watchlist.json')
PROXIES   = {
    'https': 'socks5://REDACTED_PROXY_CREDENTIALS',
    'http':  'socks5://REDACTED_PROXY_CREDENTIALS',
}
WV_COOKIE = 'bnc-uuid=52fe9a2c-9a68-425a-a395-111e2588c4bb; board_sid=9C7MsD2h96fM2oE4dRijzJP2FB46XMi26I4yOhxkVVY'

def load_watchlist():
    with open(WATCHLIST) as f:
        return json.load(f)

def save_watchlist(wl):
    with open(WATCHLIST, 'w') as f:
        json.dump(wl, f, indent=2, ensure_ascii=False)

def fetch_rt_traders():
    r = requests.get(
        'https://robinhoodtrenches.com/api/traders',
        headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://robinhoodtrenches.com/'},
        proxies=PROXIES, timeout=15
    )
    return r.json()

def fetch_wv_accounts():
    s = requests.Session()
    s.proxies = PROXIES
    s.headers.update({'User-Agent': 'Mozilla/5.0 Chrome/125', 'Accept': 'application/json',
                      'Referer': 'https://wind.jokkimon.club/windvane'})
    for c in WV_COOKIE.split('; '):
        k, v = c.split('=', 1)
        s.cookies.set(k, v, domain='wind.jokkimon.club')
    r = s.get('https://wind.jokkimon.club/api/chase/accounts_index', timeout=15)
    items = r.json().get('items', [])
    return [i for i in items if i.get('platform') == 'fomo' and i.get('evm_addr')]

def main():
    wl = load_watchlist()
    existing = {x['evm'].lower() for x in wl}
    existing_handles = {x['handle'].lower() for x in wl}
    added = []

    # ── 数据源1: robinhoodtrenches ─────────────────────────
    try:
        traders = fetch_rt_traders()
        for t in traders:
            addr  = t['address'].lower()
            pnl   = t.get('realized_pnl') or 0
            wr    = t.get('win_rate')
            closed = t.get('closed_trades', 0)
            if addr in existing or pnl < 10000:
                continue
            if wr is None and closed <= 1:
                continue
            handle = f"RT_{t['handle']}"
            if handle.lower() in existing_handles:
                continue
            wl.append({'evm': addr, 'handle': handle})
            existing.add(addr)
            existing_handles.add(handle.lower())
            added.append(f"{handle} pnl=${pnl:,.0f} WR={wr:.0%}" if wr else f"{handle} pnl=${pnl:,.0f}")
    except Exception as e:
        print(f"[RT] 获取失败: {e}")

    # ── 数据源2: windvane ──────────────────────────────────
    try:
        accounts = fetch_wv_accounts()
        for item in sorted(accounts, key=lambda x: x.get('active', 0), reverse=True):
            addr   = item['evm_addr'].lower()
            active = item.get('active', 0)
            if addr in existing or active < 50:
                continue
            handle = f"WV_{item['handle']}"
            if handle.lower() in existing_handles:
                continue
            wl.append({'evm': addr, 'handle': handle})
            existing.add(addr)
            existing_handles.add(handle.lower())
            added.append(f"{handle} active={active}")
    except Exception as e:
        print(f"[WV] 获取失败: {e}")

    save_watchlist(wl)

    ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    if added:
        lines = [f"✅ [{ts}] 聪明钱名单更新 +{len(added)} 个 (总计 {len(wl)} 个)"]
        for a in added:
            lines.append(f"  + {a}")
        print('\n'.join(lines))
    else:
        print(f"[{ts}] 无新增地址（当前 {len(wl)} 个）")

if __name__ == '__main__':
    main()
