#!/usr/bin/env python3
"""
market_scan.py — 全量市场扫描 + 推送
优化：K线本地缓存，每次只拉最新2根增量，减少请求 ~80%
所有推送价格直接来自接口数据，不手写估算值
"""
import sys, json, time, os, requests, hashlib
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
sys.path.insert(0, '/root/.openclaw/workspace')

import binance_api as api
import coinglass_api as cg

CACHE_FILE   = '/tmp/kline_cache.json'
CACHE_TTL    = 35 * 60   # 35分钟过期（比扫描间隔略长）
RESULT_FILE  = '/tmp/scan_enriched.json'
VOL_MIN_M    = 3          # 24h成交额最低门槛（百万U）
TOP_N        = 80         # 取成交量前N名
PRESCREEN_CHG = 3.0       # 预筛阈值：24h涨跌幅超过此值(%)才主动拉K线
KLINE_WORKERS = 8         # K线并行拉取线程数
CG_TOP_N     = 8          # 做CG深度评分的候选数量
OPEN_THRESH  = 75         # 综合分>=此值才推送高分信号


# ═══════════════════════════════════════════════════════════════════════════════
# K线缓存
# ═══════════════════════════════════════════════════════════════════════════════

def _load_cache() -> dict:
    try:
        if os.path.exists(CACHE_FILE):
            raw = json.load(open(CACHE_FILE))
            # 清理过期条目
            now = time.time()
            return {k: v for k, v in raw.items()
                    if now - v.get('ts', 0) < CACHE_TTL}
    except Exception:
        pass
    return {}


def _save_cache(cache: dict):
    try:
        json.dump(cache, open(CACHE_FILE, 'w'))
    except Exception:
        pass


def get_klines_cached(sym: str, interval: str, limit: int, cache: dict) -> list:
    """
    优先用缓存 + 只拉增量。
    缓存有效期内：只拉最新2根补到末尾。
    缓存过期或不存在：全量拉 limit 根。
    """
    key = f'{sym}_{interval}'
    entry = cache.get(key)
    now   = time.time()

    if entry and (now - entry['ts']) < CACHE_TTL:
        cached_klines = entry['klines']
        # 只拉最新2根
        try:
            fresh = api.get_klines_proxy(sym, interval, 2)
            if fresh:
                # 合并：去掉末尾重复的（以开盘时间为key）
                existing_ts = {k[0] for k in cached_klines}
                for k in fresh:
                    if k[0] not in existing_ts:
                        cached_klines.append(k)
                    else:
                        # 更新最后一根（当前未完成K线）
                        cached_klines = [x if x[0] != k[0] else k for x in cached_klines]
                # 只保留最新 limit 根
                merged = cached_klines[-limit:]
                cache[key] = {'ts': now, 'klines': merged}
                return merged
        except Exception:
            return cached_klines  # 增量失败时用旧缓存

    # 全量拉取
    try:
        klines = api.get_klines_proxy(sym, interval, limit)
        if klines:
            cache[key] = {'ts': now, 'klines': klines}
        return klines or []
    except Exception:
        return []


# ═══════════════════════════════════════════════════════════════════════════════
# 技术评分
# ═══════════════════════════════════════════════════════════════════════════════

def tech_score(sym: str, price: float, cache: dict) -> dict | None:
    klines = get_klines_cached(sym, '1h', 50, cache)
    if not klines or len(klines) < 20:
        return None

    closes = [float(k[4]) for k in klines]
    vols   = [float(k[5]) for k in klines]
    highs  = [float(k[2]) for k in klines]
    lows   = [float(k[3]) for k in klines]

    # RSI-14
    gains  = [max(closes[i]-closes[i-1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i-1]-closes[i], 0) for i in range(1, len(closes))]
    avg_g  = sum(gains[-14:]) / 14
    avg_l  = sum(losses[-14:]) / 14
    rsi    = 100 - 100 / (1 + avg_g / (avg_l + 1e-9))

    # 量比
    vol_avg    = sum(vols[-20:]) / 20
    vol_ratio  = vols[-1] / (vol_avg + 1e-9)

    # ATR%
    trs    = [max(highs[i]-lows[i],
                  abs(highs[i]-closes[i-1]),
                  abs(lows[i]-closes[i-1])) for i in range(1, len(closes))]
    atr_pct = (sum(trs[-14:]) / 14) / price * 100

    # 5根动量
    mom5 = (closes[-1] - closes[-6]) / closes[-6] * 100 if len(closes) >= 6 else 0

    score = 50
    if rsi < 35:    score += 12
    elif rsi < 45:  score += 6
    elif rsi > 75:  score += 10
    elif rsi > 65:  score += 6

    if vol_ratio > 2.0:   score += 10
    elif vol_ratio > 1.5: score += 6
    elif vol_ratio < 0.5: score -= 5

    if atr_pct < 0.3:  score -= 10
    elif atr_pct > 1.5: score += 5

    if abs(mom5) > 3: score += 5

    direction = 'LONG' if rsi < 50 else 'SHORT'

    return {
        'symbol': sym, 'price': price, 'direction': direction,
        'score': score, 'rsi': rsi, 'vol_ratio': vol_ratio,
        'atr_pct': atr_pct, 'mom5': mom5,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 主扫描流程
# ═══════════════════════════════════════════════════════════════════════════════

def run_scan(notify_fn=None) -> list:
    t0 = time.time()
    cache = _load_cache()
    cache_hits = 0

    # 1. 全市场 ticker（1次请求）
    tickers = requests.get(
        'https://fapi.binance.com/fapi/v1/ticker/24hr', timeout=10
    ).json()
    tickers = [t for t in tickers
               if t['symbol'].endswith('USDT')
               and float(t.get('quoteVolume', 0)) > VOL_MIN_M * 1e6]
    tickers.sort(key=lambda x: -float(x.get('quoteVolume', 0)))
    top_syms = [t['symbol'] for t in tickers[:TOP_N]]

    # 排除已持仓
    pos_syms = {p['symbol'] for p in api.get_positions()}
    syms = [s for s in top_syms if s not in pos_syms]

    # 2. 预筛：有缓存直接用；无缓存时仅对24h涨跌>阈值或量比异常的标的拉K线
    ticker_map = {t['symbol']: t for t in tickers}
    sym_info = []
    for sym in syms:
        t_info = ticker_map.get(sym, {})
        price  = float(t_info.get('lastPrice', 0))
        chg24h = abs(float(t_info.get('priceChangePercent', 0)))
        key    = f'{sym}_1h'
        if price <= 0:
            continue
        has_cache = key in cache
        need_fetch = has_cache or chg24h >= PRESCREEN_CHG  # 有缓存必拉增量；无缓存只拉异动标的
        if has_cache:
            cache_hits += 1
        if need_fetch:
            sym_info.append((sym, price, t_info))

    # 3. 并行拉取 K 线
    def _fetch_score(args):
        sym, price, t_info = args
        r = tech_score(sym, price, cache)
        if r:
            r['vol24h_m'] = float(t_info.get('quoteVolume', 0)) / 1e6
        return r

    results = []
    api_calls = len(sym_info)
    with ThreadPoolExecutor(max_workers=KLINE_WORKERS) as ex:
        futs = {ex.submit(_fetch_score, args): args for args in sym_info}
        for fut in as_completed(futs):
            r = fut.result()
            if r:
                results.append(r)

    _save_cache(cache)   # 并行拉取完成后统一写入缓存
    results.sort(key=lambda x: -x['score'])

    # 3. CG 深度评分（只对技术分前N名 + 量比>0.08 + ATR>0.5 的候选）
    cg_candidates = [r for r in results[:15]
                     if r['vol_ratio'] > 0.08 and r['atr_pct'] > 0.5][:CG_TOP_N]

    enriched = []
    for r in cg_candidates:
        sym   = r['symbol']
        dir_  = r['direction']
        price = r['price']   # 直接用扫描时的接口价格
        try:
            time.sleep(2.2)
            sig      = cg.get_signal_for_open(sym, dir_, price)
            cg_score = sig.get('score_adjust', 0)
            tech_s100 = max(0, min(100, round(50 + (r['score'] - 50) * 2)))
            cg_s100   = max(0, min(100, round(50 + cg_score * 1.5)))
            combined  = round(tech_s100 * 0.4 + cg_s100 * 0.6)
            dr = sig.get('deriv_sig', {})
            enriched.append({
                **r,
                'cg_score': cg_score,
                'cg_bias':  sig.get('bias', '?'),
                'combined': combined,
                'tp1': sig.get('tp1'), 'tp2': sig.get('tp2'), 'sl': sig.get('sl'),
                'fr':       dr.get('fr_latest'),
                'fr_trend': dr.get('fr_trend', '?'),
                'oi':       dr.get('oi_signal', '?'),
                'lsr':      dr.get('lsr_signal', '?'),
                'taker':    dr.get('taker_signal', '?'),
                'liq_sig':  dr.get('liq_signal', '?'),
            })
        except Exception as e:
            pass

    enriched.sort(key=lambda x: -x['combined'])
    json.dump(enriched, open(RESULT_FILE, 'w'), indent=2, ensure_ascii=False)

    elapsed = time.time() - t0
    print(f'扫描完成: {len(results)}个标的(预筛{len(sym_info)}/{len(syms)})  CG深评{len(enriched)}个  '
          f'API请求≈{api_calls+1}次  缓存命中{cache_hits}/{len(syms)}  并行{KLINE_WORKERS}线程  耗时{elapsed:.1f}s')

    return enriched


# ═══════════════════════════════════════════════════════════════════════════════
# 格式化推送（所有价格直接来自数据，不手写）
# ═══════════════════════════════════════════════════════════════════════════════

def format_signal(r: dict) -> str:
    sym      = r['symbol']
    dir_cn   = '多头' if r['direction'] == 'LONG' else '空头'
    price    = r['price']         # 接口实际价格
    combined = r['combined']
    rsi      = r['rsi']
    vol_r    = r['vol_ratio']
    fr       = r.get('fr')
    fr_trend = r.get('fr_trend', '')
    bias     = r.get('cg_bias', '')
    oi       = r.get('oi', '')
    tp1      = r.get('tp1')
    tp2      = r.get('tp2')
    sl       = r.get('sl')

    icon = '🟢' if r['direction'] == 'LONG' else '🔴'
    lines = [f"{icon} {sym} {dir_cn} | 综合评分 {combined}/100"]
    lines.append(f"   现价：{price}")
    lines.append(f"   RSI {rsi:.1f} | 量比 {vol_r:.2f}x | 热力图 {bias.upper()}")

    extras = []
    if fr is not None:
        fr_icon = '📉' if fr_trend == 'falling' else ('📈' if fr_trend == 'rising' else '➡️')
        fr_dir  = '收资金费' if (r['direction']=='LONG' and fr<0) or (r['direction']=='SHORT' and fr>0) else '付资金费'
        extras.append(f"资金率{fr_icon}{fr:.4%}({fr_dir})")
    if oi == 'accumulate':
        extras.append('OI持续增加')
    elif oi == 'deleverage':
        extras.append('OI去杠杆')
    if extras:
        lines.append(f"   {' | '.join(extras)}")

    if sl and tp1:
        sl_dist  = abs(price - sl) / price * 100
        tp1_dist = abs(tp1 - price) / price * 100
        rr = tp1_dist / sl_dist if sl_dist > 0 else 0
        lines.append(f"   SL: {sl:.6g}(-{sl_dist:.1f}%) | TP1: {tp1:.6g}(+{tp1_dist:.1f}%) | R:R={rr:.1f}")
        if tp2:
            tp2_dist = abs(tp2 - price) / price * 100
            lines.append(f"   TP2: {tp2:.6g}(+{tp2_dist:.1f}%)")

    return '\n'.join(lines)


def build_report(enriched: list, scan_time: str) -> str | None:
    high = [r for r in enriched if r['combined'] >= OPEN_THRESH]
    watch = [r for r in enriched if 65 <= r['combined'] < OPEN_THRESH]

    if not high and not watch:
        return None

    lines = [f"📊 扫描报告 | {scan_time}"]
    lines.append(f"共评估 {len(enriched)} 个候选")
    lines.append('')

    if high:
        lines.append('━━━━ 🔥 高分信号 ━━━━')
        for i, r in enumerate(high, 1):
            lines.append(f'\n{i}. {format_signal(r)}')

    if watch:
        lines.append('')
        lines.append('━━━━ 👀 观察区 ━━━━')
        for r in watch:
            dir_cn = '多' if r['direction'] == 'LONG' else '空'
            icon   = '🟢' if r['direction'] == 'LONG' else '🔴'
            lines.append(f"  {icon} {r['symbol']} {dir_cn} {r['combined']}/100 | 现价:{r['price']}")

    return '\n'.join(lines)


if __name__ == '__main__':
    print(f'开始扫描 {datetime.now(timezone.utc).strftime("%H:%M UTC")}')
    enriched = run_scan()
    scan_time = datetime.now(timezone.utc).strftime('%m-%d %H:%M UTC')
    report = build_report(enriched, scan_time)
    if report:
        print('\n' + report)
    else:
        print('无高分信号')
