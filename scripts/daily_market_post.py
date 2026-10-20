"""
daily_market_post.py — 每日自动发布涨跌幅榜分析到币安广场
每天发3篇：涨幅冠军 + 跌幅冠军 + 市场综述
字数控制在150字以内，保证可读性
"""
import sys, os, requests, json, datetime, time
sys.path.insert(0, "/root/.openclaw/workspace")

SQUARE_KEY = "42c42b7e6b594c168d4a66f45565a860"
SQUARE_URL = "https://www.binance.com/bapi/composite/v1/public/pgc/openApi/content/add"
PROXY = {"http": "socks5://REDACTED_PROXY_CREDENTIALS",
         "https": "socks5://REDACTED_PROXY_CREDENTIALS"}

# ── 技术指标 ──
def rsi(closes, n=14):
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0)); losses.append(max(-d, 0))
    if not gains: return 50
    ag = sum(gains[-n:]) / n; al = sum(losses[-n:]) / n
    return round(100 - 100 / (1 + ag / al), 1) if al else 100

def ema(closes, n):
    k = 2 / (n + 1); e = closes[0]
    for c in closes[1:]: e = c * k + e * (1 - k)
    return e

def get_klines(symbol, interval, limit=100):
    r = requests.get("https://fapi.binance.com/fapi/v1/klines",
                     params={"symbol": symbol, "interval": interval, "limit": limit},
                     proxies=PROXY, timeout=12)
    return r.json()

def fmt_price(p):
    """智能格式化价格"""
    if p >= 100: return f"{p:.2f}"
    if p >= 1:   return f"{p:.3f}"
    if p >= 0.01: return f"{p:.4f}"
    return f"{p:.6f}"

def analyze(symbol):
    """获取技术分析数据"""
    try:
        k1h = get_klines(symbol, "1h", 100)
        k4h = get_klines(symbol, "4h", 60)
        c1h = [float(k[4]) for k in k1h]
        c4h = [float(k[4]) for k in k4h]
        h1h = [float(k[2]) for k in k1h[-30:]]
        l1h = [float(k[3]) for k in k1h[-30:]]
        price = c1h[-1]
        rsi1h = rsi(c1h)
        rsi4h = rsi(c4h)
        e7 = ema(c1h, 7); e25 = ema(c1h, 25)
        trend = "多" if e7 > e25 else "空"
        resist = max(h1h)
        support = min(l1h)
        return {"price": price, "rsi1h": rsi1h, "rsi4h": rsi4h,
                "trend": trend, "resist": resist, "support": support}
    except Exception as e:
        return None

def get_rankings():
    """获取涨跌幅榜"""
    r = requests.get("https://fapi.binance.com/fapi/v1/ticker/24hr",
                     proxies=PROXY, timeout=15)
    data = r.json()
    usdt = [x for x in data if x['symbol'].endswith('USDT')
            and float(x['quoteVolume']) > 50_000_000]
    gainers = sorted(usdt, key=lambda x: float(x['priceChangePercent']), reverse=True)[:5]
    losers  = sorted(usdt, key=lambda x: float(x['priceChangePercent']))[:5]
    return gainers, losers

def post_to_square(content):
    """发帖，返回 (ok, url_or_err)"""
    try:
        r = requests.post(SQUARE_URL,
            headers={"X-Square-OpenAPI-Key": SQUARE_KEY,
                     "Content-Type": "application/json",
                     "clienttype": "binanceSkill"},
            json={"bodyTextOnly": content},
            proxies=PROXY, timeout=15)
        d = r.json()
        if d.get("code") == "000000":
            pid = d["data"]["id"]
            return True, f"https://www.binance.com/square/post/{pid}"
        else:
            return False, f"code={d.get('code')} {d.get('message','')}"
    except Exception as e:
        return False, str(e)

def build_gainer_post(sym_info, ta):
    """涨幅冠军帖子（≤150字）"""
    sym = sym_info['symbol'].replace('USDT', '')
    pct = float(sym_info['priceChangePercent'])
    price = float(sym_info['lastPrice'])
    today = datetime.datetime.utcnow().strftime("%m/%d")

    if ta:
        entry_low  = fmt_price(ta['support'] * 1.01)
        entry_high = fmt_price(ta['price'] * 0.97)
        tp1 = fmt_price(ta['resist'])
        sl  = fmt_price(ta['support'] * 0.97)
        rsi_str = f"RSI {ta['rsi1h']:.0f}/{ ta['rsi4h']:.0f}"
        trend_str = "EMA多排✅" if ta['trend'] == "多" else "EMA空排⚠️"
    else:
        entry_low = entry_high = tp1 = sl = "—"
        rsi_str = trend_str = ""

    return f"""📈 涨幅榜冠军 ${sym} +{pct:.1f}%｜{today}

现价 ${fmt_price(price)}｜{rsi_str}｜{trend_str}

📌 参考操作：
入场区：{entry_low}~{entry_high}（等回踩）
止盈：{tp1}（压力位）
止损：{sl}（破支撑离场）

⚠️ 单日涨幅较大，追高风险高，建议等回调机会。"""

def build_loser_post(sym_info, ta):
    """跌幅冠军帖子（≤150字）"""
    sym = sym_info['symbol'].replace('USDT', '')
    pct = float(sym_info['priceChangePercent'])
    price = float(sym_info['lastPrice'])
    today = datetime.datetime.utcnow().strftime("%m/%d")

    oversold = ta and ta['rsi4h'] < 30

    if ta:
        entry_low  = fmt_price(price * 0.99)
        entry_high = fmt_price(price * 1.03)
        tp1 = fmt_price(ta['resist'] * 0.6)  # 反弹第一目标（保守）
        sl  = fmt_price(price * 0.90)
        rsi_str = f"RSI {ta['rsi1h']:.0f}/{ta['rsi4h']:.0f}"
        ob_tag = "⚡超卖反弹机会" if oversold else "趋势仍弱"
    else:
        entry_low = entry_high = tp1 = sl = "—"
        rsi_str = ob_tag = ""

    return f"""📉 跌幅榜冠军 ${sym} {pct:.1f}%｜{today}

现价 ${fmt_price(price)}｜{rsi_str}｜{ob_tag}

📌 参考操作：
反弹入场：{entry_low}~{entry_high}
止盈：{tp1}（短期反压）
止损：{sl}（跌破离场）

⚠️ 暴跌需确认无利空消息，控制仓位，勿重仓。"""

def build_summary_post(gainers, losers):
    """市场综述帖子（≤150字，不带$符号避免触发币对限制）"""
    today = datetime.datetime.utcnow().strftime("%m/%d")
    top3g = " / ".join([f"{x['symbol'].replace('USDT','')} +{float(x['priceChangePercent']):.1f}%" for x in gainers[:3]])
    top3l = " / ".join([f"{x['symbol'].replace('USDT','')} {float(x['priceChangePercent']):.1f}%" for x in losers[:3]])
    return f"""🔥 今日加密市场快报｜{today}

📈 强势：{top3g}
📉 弱势：{top3l}

市场分化明显，强弱分化行情为主。
操作思路：强势品种等回调介入，不追高；超跌品种关注反弹机会，控制仓位，耐心等待确认信号。

#加密货币 #市场分析"""

# ── 主流程 ──
if __name__ == "__main__":
    now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    print(f"=== 每日行情发帖 {now} ===")

    # 1. 获取排行榜
    print("拉取涨跌幅数据...")
    gainers, losers = get_rankings()
    top_gainer = gainers[0]
    top_loser  = losers[0]
    print(f"涨幅冠军: {top_gainer['symbol']} +{float(top_gainer['priceChangePercent']):.1f}%")
    print(f"跌幅冠军: {top_loser['symbol']} {float(top_loser['priceChangePercent']):.1f}%")

    results = []

    # 2. 帖子1：涨幅冠军
    ta_g = analyze(top_gainer['symbol'])
    post1 = build_gainer_post(top_gainer, ta_g)
    print(f"\n[帖子1 涨幅冠军] {len(post1)}字")
    ok, url = post_to_square(post1)
    results.append(("涨幅冠军", ok, url))
    print(f"  {'✅' if ok else '❌'} {url}")
    time.sleep(3)

    # 3. 帖子2：跌幅冠军
    ta_l = analyze(top_loser['symbol'])
    post2 = build_loser_post(top_loser, ta_l)
    print(f"\n[帖子2 跌幅冠军] {len(post2)}字")
    ok, url = post_to_square(post2)
    results.append(("跌幅冠军", ok, url))
    print(f"  {'✅' if ok else '❌'} {url}")
    time.sleep(3)

    # 4. 帖子3：市场综述
    post3 = build_summary_post(gainers, losers)
    print(f"\n[帖子3 市场综述] {len(post3)}字")
    ok, url = post_to_square(post3)
    results.append(("市场综述", ok, url))
    print(f"  {'✅' if ok else '❌'} {url}")

    # 汇总
    print(f"\n{'='*40}")
    all_ok = all(r[1] for r in results)
    print(f"{'TASK_OK' if all_ok else 'TASK_PARTIAL'}")
    for name, ok, msg in results:
        print(f"  {'✅' if ok else '❌'} {name}: {msg}")
