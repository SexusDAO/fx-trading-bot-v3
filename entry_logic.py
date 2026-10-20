"""
entry_logic.py — 开仓核心逻辑模块
所有扫描/开仓脚本必须 import 本模块，禁止在脚本里直接写 mark*0.995 或固定 ATR 倍数。

函数列表：
  liquidation_map(symbol, k1h, k4h, direction) -> dict
  calc_entry_mode(k1h, direction, lmap, vol_threshold) -> (mode, limit_price, wait_seconds)
  calc_score_trend(sym, k1h, k4h, k15m, ticker, funding, token_report_score=0) -> (score, details)
  calc_score_reversal(sym, k1h, k4h, k15m, ticker, funding, token_report_score=0) -> (score, details)
  check_oi_divergence(symbol) -> (is_diverging, oi_chg_pct)
  check_btc_filter(direction) -> bool   # True = 通过，False = 拦截
  classify_signal(k1h, k4h, funding, chg24) -> 'trend' | 'reversal'
"""

import requests
import datetime
import math
import sys
import os

sys.path.insert(0, "/root/.openclaw/workspace")
import binance_api as api

BASE_F = "https://fapi.binance.com"
_btc_cache = {}  # {ts_hour: (ma7, ma25)}


# ─────────────────────────────────────────────
# 基础指标工具
# ─────────────────────────────────────────────

def ema_calc(data, n):
    k = 2 / (n + 1)
    e = data[0]
    for x in data[1:]:
        e = x * k + e * (1 - k)
    return e

def rsi_calc(closes, n=14):
    diffs = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    g = [max(d, 0) for d in diffs[-n:]]
    l = [abs(min(d, 0)) for d in diffs[-n:]]
    ag = sum(g) / n; al = sum(l) / n
    return 100 - 100 / (1 + ag / al) if al > 0 else 50

def atr_calc(klines, n=14):
    trs = []
    for i in range(1, len(klines)):
        h = float(klines[i][2]); lo = float(klines[i][3]); pc = float(klines[i-1][4])
        trs.append(max(h - lo, abs(h - pc), abs(lo - pc)))
    return sum(trs[-n:]) / n if trs else 0

def round_tick(p, tick_str):
    tick = float(tick_str)
    dec = max(0, -int(math.floor(math.log10(tick)))) if tick < 1 else 0
    return round(round(p / tick) * tick, dec)

def fmt_price(p, tick):
    dt = max(0, -int(math.floor(math.log10(tick)))) if tick < 1 else 0
    return f"{round(round(p / tick) * tick, dt):.{dt}f}"

def fmt_qty(q, step):
    ds = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
    return f"{round(round(q / step) * step, ds):.{ds}f}"


# ─────────────────────────────────────────────
# 信号分类
# ─────────────────────────────────────────────

def _detect_reversal_candle(k1h):
    """
    检测最近一根1H K线是否出现止跌信号：
    - 下影线 > 实体×2（锤头/下影针）
    - 阳包阴（当前收盘>前一开盘 且 当前开盘<前一收盘）
    返回 True/False
    """
    if len(k1h) < 2:
        return False
    k = k1h[-1]; kp = k1h[-2]
    o = float(k[1]); h = float(k[2]); lo = float(k[3]); c = float(k[4])
    op = float(kp[1]); cp = float(kp[4])
    body  = abs(c - o)
    lower = min(o, c) - lo
    hammer = lower > body * 2 and body > 0
    bullish_engulf = c > op and o < cp  # 阳包阴
    return hammer or bullish_engulf


def classify_signal(k1h, k4h, funding, chg24, symbol: str = ""):
    """
    v4: 路径A升级为两级入场，消除 RSI超卖 和 止跌K线 互斥问题
    ─────────────────────────────────────────────────────────
    路径A基础门槛（必须满足）：
      · 24H跌幅 > 15%（幅度够大）
      · 资金费率 < -0.1%（空头付费，情绪偏空）
      · 额外保护：BTC 5min跌幅<2%、近3根量能未持续萎缩、24H成交量>100万U

    级别一（reversal_l1，RSI主导，125U半仓）：
      · RSI 4H < 25（极度超卖）且 RSI 1H < 35
      · 无止跌K线也可触发（RSI本身就是信号）
      → 风险更高，半仓试探

    级别二（reversal，K线主导，250U正常仓）：
      · 出现止跌K线（下影针/阳包阴/连续阳线≥2根）
      · RSI 1H < 50（允许已开始回升）
      · RSI 4H < 45
      → 更稳健的入场时机

    同时满足两级：优先级别二（250U）
    路径B：趋势延续空单（跌>5% + EMA空排 + RSI中段 + 无止跌信号）
    路径C：兜底，走 trend 逻辑

    返回值: 'reversal'(A2) | 'reversal_l1'(A1) | 'trend_short'(B) | 'trend'(C)
    """
    # ── 股票代币休市拦截：非美股交易时段直接跳过（v3.1） ──
    try:
        from calc_position_size_real import _is_stock_token, _us_market_open
        if _is_stock_token(symbol) and not _us_market_open():
            return "skip_stock_closed"
    except Exception:
        pass

    c1h = [float(k[4]) for k in k1h]
    c4h = [float(k[4]) for k in k4h]
    v1h = [float(k[5]) for k in k1h]
    rsi_1h = rsi_calc(c1h)
    rsi_4h = rsi_calc(c4h)
    has_reversal_candle = _detect_reversal_candle(k1h)

    # 连续阳线（近2根）
    consec_green = len(c1h) >= 3 and c1h[-1] > c1h[-2] and c1h[-2] > c1h[-3]
    has_stop_signal = has_reversal_candle or consec_green

    # EMA空排（4H）
    ema9_4h  = ema_calc(c4h[-9:],  9)  if len(c4h) >= 9  else ema_calc(c4h, len(c4h))
    ema20_4h = ema_calc(c4h[-20:], 20) if len(c4h) >= 20 else ema_calc(c4h, len(c4h))
    bear_4h  = ema9_4h < ema20_4h

    # ── 额外保护（两级共用）──────────────────────────────────────────────────
    # 保护1：近3根量能持续萎缩（卖压未减弱，止跌信号不可信）
    _vol_shrink = False
    if len(v1h) >= 4:
        v3 = v1h[-3:]
        _vol_shrink = v3[0] > v3[1] > v3[2]  # 连续3根缩量
    # 保护2：24H成交量<100万（流动性不足）
    _low_liquidity = float(sum(v1h[-24:]) * float(k1h[-1][4])) < 1_000_000 if len(v1h) >= 24 else False
    _extra_guard_ok = not _vol_shrink and not _low_liquidity

    # ── 路径A基础门槛 ────────────────────────────────────────────────────────
    # v2.9优化: 资金费率门槛放宽
    # 原逻辑要求 funding < -0.001（空头拥挤），但新币/股票代币暴跌时
    # 空头开仓少，费率往往中性（|funding| < 0.0005），直接被误排。
    # 修复：强烈负费率（<-0.001）OR 费率接近中性（|f|<0.0005，无人借空）都放行
    # 明确排除正费率>0.0005（多头仍在付费=抄底风险高）
    _funding_ok_a = funding < -0.001 or abs(funding) < 0.0005
    base_a = chg24 < -15 and _funding_ok_a and _extra_guard_ok

    # 级别二（止跌确认，优先）
    level2 = base_a and has_stop_signal and rsi_1h < 50 and rsi_4h < 45
    # 级别一（RSI极度超卖，半仓试探）
    level1 = base_a and rsi_4h < 25 and rsi_1h < 35 and not level2

    # ── 路径B：趋势延续空单 ──────────────────────────────────────────────────
    # v2.7修复: 增加 bear_1h 强制条件
    # 原因：BEATUSDT案例——跌幅>5%+4H空排条件满足，但1H EMA仍是多排
    # 均线还没反应过来时做空 = 在趋势夹缝里开单，极易被反弹扫止损
    # 修复方案：1H EMA20必须也空排（EMA20 < EMA50），双周期同向才走空
    ema20_1h = ema_calc(c1h[-20:], 20) if len(c1h) >= 20 else ema_calc(c1h, len(c1h))
    ema50_1h = ema_calc(c1h[-50:], 50) if len(c1h) >= 50 else ema_calc(c1h, len(c1h))
    bear_1h  = ema20_1h < ema50_1h  # 1H 也必须空排
    # v2.8优化: RSI窗口放宽 35-65 → 25-70，跌中段RSI常落在25-35被误排
    path_b = chg24 < -5 and bear_4h and bear_1h and 25 <= rsi_1h <= 70 and not has_reversal_candle

    # ── 路径D：超涨逆势空单（对称路径A的设计）────────────────────────────────
    # 基础门槛：24H涨幅>15% + 资金费率>0.1% + 量能保护（近3根不持续萎缩）
    # 额外保护：量能萎缩=成交量已枯竭，不追空
    _vol_expand = not _vol_shrink  # 量能未萎缩才考虑做空
    # v2.8修复: _funding_hot 逻辑修正
    # 超涨>30% 本身就是超买信号，直接通过；费率>0.05% 多头拥挤通过；
    # 费率极度负值(<-0.2%)说明套利已开始反转，空单反而有优势（原逻辑漏掉了这种情况）
    _funding_hot = chg24 > 30 or funding > 0.0005 or funding < -0.002
    base_d = chg24 > 15 and _funding_hot and _vol_expand

    # 见顶K线：上影线>实体2倍（插针/上影长线）
    def _has_top_candle(klines):
        if len(klines) < 2: return False
        k = klines[-1]
        o=float(k[1]); h=float(k[2]); lo=float(k[3]); c=float(k[4])
        body=abs(c-o); upper=h-max(o,c)
        shooting_star = upper > body*2 and body > 0
        # 阴包阳（看跌吞没）
        kp=klines[-2]; op=float(kp[1]); cp=float(kp[4])
        bearish_engulf = c < op and o > cp and c < o  # 当前阴线包裹前根阳线
        return shooting_star or bearish_engulf

    # BB极值：价格在布林带外（价格>上轨）
    _sma20 = sum(c1h[-20:])/20 if len(c1h)>=20 else c1h[-1]
    _std20 = (sum((x-_sma20)**2 for x in c1h[-20:])/20)**0.5 if len(c1h)>=20 else 0
    _bb_u  = _sma20 + 2*_std20
    _bb_extreme = c1h[-1] > _bb_u if _std20 > 0 else False

    # 级别二（空单，见顶确认，250U）
    level_d2 = base_d and _has_top_candle(k1h) and rsi_1h > 55 and rsi_4h > 55
    # 级别一（空单，RSI极超买，125U半仓）
    level_d1 = base_d and rsi_4h > 80 and rsi_1h > 75 and _bb_extreme and not level_d2

    # ── 最终路径优先级（防止同一标的走多条路径）──────────────────────────────
    # 跌幅>15%时穷举（条目5）：
    #   第一步: 止跌K线+RSI1H<50+RSI4H<45 → reversal（逆势多单）
    #   第二步: 无止跌K线+RSI35~65+EMA空排 → trend_short（趋势空单）
    #   第三步: 无止跌K线+RSI<35           → reversal_l1（逆势多，仍有下探）
    # 涨幅>15%时：优先级别二/一逆势空单
    if level2:
        return 'reversal'          # 逆势多单级别二
    elif level1:
        return 'reversal_l1'       # 逆势多单级别一
    elif level_d2:
        return 'reversal_short'    # 逆势空单级别二
    elif level_d1:
        return 'reversal_short_l1' # 逆势空单级别一
    elif path_b:
        return 'trend_short'       # 趋势空单（跌中段EMA空排）
    else:
        return 'trend'             # 兜底（含趋势多单）'


# ─────────────────────────────────────────────
# 评分：趋势单
# ─────────────────────────────────────────────

def calc_score_trend(sym, k1h, k4h, k15m, ticker, funding, token_report_score=0):
    """
    趋势单评分，满分 ~115（含聪明钱加分）
    门槛 75 分
    返回 (score, details_dict)
    """
    # funding 单位化安全层：如果传入的是原始小数（|f|<0.1），自动转为百分比
    # auto_open.py 传 fr_raw （如 0.0001 = 0.01%）；short_scanner 传 funding_pct （如 0.1 = 0.1%）
    if abs(funding) < 0.1:
        funding = funding * 100  # 原始小数转百分比
    c1h  = [float(k[4]) for k in k1h]
    c4h  = [float(k[4]) for k in k4h]
    c15m = [float(k[4]) for k in k15m] if k15m else c1h
    v1h  = [float(k[5]) for k in k1h]

    rsi_1h  = rsi_calc(c1h)
    rsi_4h  = rsi_calc(c4h)
    rsi_15m = rsi_calc(c15m)

    ema20_1h = ema_calc(c1h[-20:], 20)
    ema50_1h = ema_calc(c1h[-50:], 50) if len(c1h) >= 50 else ema_calc(c1h, len(c1h))
    ema20_4h = ema_calc(c4h[-20:], 20) if len(c4h) >= 20 else ema_calc(c4h, len(c4h))
    ema50_4h = ema_calc(c4h[-50:], 50) if len(c4h) >= 50 else ema_calc(c4h, len(c4h))

    vb   = sum(v1h[-20:-3]) / 17 if len(v1h) >= 20 else sum(v1h) / len(v1h)
    vr   = (sum(v1h[-3:]) / 3) / vb if vb > 0 else 1
    # 绝对成交量：优先用 ticker.quoteVolume（24H USDT成交额），更准确
    # 小价格币（如0.02 USDT）用数量×价格会严重低估
    _q24h = float(ticker.get("quoteVolume", 0) or 0)
    abs_vol_usdt = _q24h  # 24H USDT 成交额
    # 新币K线不足标记（<20根时量比失真）
    thin_history = len(v1h) < 20

    sma20 = sum(c1h[-20:]) / 20
    std20 = (sum((x - sma20) ** 2 for x in c1h[-20:]) / 20) ** 0.5
    bb_u  = sma20 + 2 * std20; bb_l = sma20 - 2 * std20
    bb_pct = (c1h[-1] - bb_l) / (bb_u - bb_l) * 100 if (bb_u - bb_l) > 0 else 50

    lk = k1h[-1]
    o = float(lk[1]); h = float(lk[2]); l2 = float(lk[3]); c = float(lk[4])
    body  = abs(c - o); lower = min(o, c) - l2
    hammer = lower > body * 2 and body > 0

    dir1h = '↑' if c1h[-1] > c1h[-2] > c1h[-3] else ('↓' if c1h[-1] < c1h[-2] < c1h[-3] else '~')
    chg24 = float(ticker.get('priceChangePercent', 0))

    # ── 异常大K线冷却过滤（v2.8新增）──────────────────────────────────────────
    # 若最近12根1H K线内出现 range > 3×ATR 的异常K线（暴跌/暴涨插针/清算），
    # 说明该币处于结构受损冷却期，直接返回0分跳过，避免在大波动后追多。
    _atr_now = atr_calc(k1h)
    _spike_found = False
    _spike_info = ''
    for _ki in range(-12, 0):
        _kline = k1h[_ki]
        _kh, _kl = float(_kline[2]), float(_kline[3])
        _kr = _kh - _kl
        _kc, _ko = float(_kline[4]), float(_kline[1])
        _range_pct = _kr / _kl * 100 if _kl > 0 else 0
        if _atr_now > 0 and _kr > _atr_now * 3.5:
            # 额外确认：必须是真实异动（不是正常高波动币的普通K线）
            # 用range/ATR > 3.5 + 单根跌幅>10% 双重确认
            _kbody = abs(_kc - _ko)
            _kdown_pct = (_ko - _kc) / _ko * 100 if _ko > 0 else 0  # 阴线跌幅
            if _range_pct > 10 or _kdown_pct > 8:
                _spike_found = True
                _spike_info = f'range={_kr:.4f}({_range_pct:.1f}%) > 3.5×ATR({_atr_now:.4f})'
                break
    if _spike_found:
        return 0, {'skip_reason': f'异常大K线冷却期 {_spike_info}', 'rsi_1h': rsi_calc(c1h),
                   'rsi_4h': rsi_calc(c4h), 'vr': 0, 'bb_pct': 0, 'dir': '~',
                   'hammer': False, 'ema_1h': '', 'ema_4h': '', 'chg24': chg24,
                   'funding': funding, 'token_report_score': 0,
                   'abs_vol_usdt': float(ticker.get('quoteVolume', 0) or 0),
                   'thin_history': False}
    # ─────────────────────────────────────────────────────────────────────────

    score = 0

    # ── 子分数初始化（功能二 v3.1）────────────────────────────────────────
    # trend_score：趋势确认分（满分60）= EMA排列 + RSI1H + RSI4H + 资金费率
    # timing_score：入场时机分（满分40）= 量能/量比 + 布林带位置 + K线方向 + 24H涨幅
    # 两个子分数独立计算，现阶段只写入 details，不作为拒绝依据（等样本积累）
    _ts = 0   # trend_score 累计
    _ms = 0   # timing_score 累计
    # ─────────────────────────────────────────────────────────────────────

    # EMA 排列（满分 30）→ trend_score
    both_long  = ema20_1h > ema50_1h and ema20_4h > ema50_4h
    one_long   = ema20_1h > ema50_1h or  ema20_4h > ema50_4h
    if both_long:  score += 30; _ts += 30
    elif one_long: score += 15; _ts += 15

    # RSI 1H（满分 15）→ trend_score
    if 50 <= rsi_1h <= 65:   score += 15; _ts += 15
    elif 40 <= rsi_1h < 50:  score += 8;  _ts += 8

    # RSI 4H（满分 10）→ trend_score
    if 50 <= rsi_4h <= 65:   score += 10; _ts += 10
    elif 40 <= rsi_4h < 50:  score += 5;  _ts += 5

    # v2.7修复: RSI 4H 过热下属分（插针猎杀保护）
    # 原因：NBISUSDT(RSI4H=70.6)、AXTIUSDT(RSI4H=67.9)开仓后无充足回调空间，止损被插针扫去
    # RSI4H>68: 已处于调整金层/超买进入区，下方缓冲不足，止损容易被插针扫去
    if rsi_4h > 75:   score -= 15; _ts -= 15   # 严重超买，下属分
    elif rsi_4h > 68: score -= 8;  _ts -= 8    # 偏高，轻度惩罚

    # 量能（满分 20）→ timing_score
    # 优先级一：绝对成交量兜底（新币/高波动币量比失真时使用）
    # >200M USDT/h → 量能充足，满分；>50M → 量能良好，80%
    # 新币K线<20根 → 量比权重减半，避免均值失真
    if abs_vol_usdt > 200_000_000:
        score += 20; _ms += 20   # 绝对量充足，直接满分，不看量比
    elif abs_vol_usdt > 50_000_000:
        score += 16; _ms += 16   # 绝对量良好，80%
    elif thin_history:
        # 新币：量比权重减半
        if vr > 1.5:   score += 10; _ms += 10
        elif vr > 1.2: score += 6;  _ms += 6
        elif vr > 1.0: score += 3;  _ms += 3
    else:
        # 正常量比判断
        if vr > 1.5:   score += 20; _ms += 20
        elif vr > 1.2: score += 12; _ms += 12
        elif vr > 1.0: score += 6;  _ms += 6

    # ── 量比过低上限保护（v2.9新增）────────────────────────────────────────
    # 量比<0.5x：极度缩量，买方缺席，评分上限60分，避免假信号
    # 周末（周六/周日 UTC）：流动性天然偏低，门槛整体上调，评分上限70分
    import datetime as _dt
    _weekday = _dt.datetime.utcnow().weekday()  # 0=周一 ... 5=周六 6=周日
    _is_weekend = _weekday >= 5

    if vr < 0.5 and not (abs_vol_usdt > 200_000_000):  # 绝对量大的不受限
        score = min(score, 60)
    if _is_weekend:
        score = min(score, 80)  # 周末整体上限80分（门槛仍75，留差异空间，避免上限=门槛歧义）
    # ─────────────────────────────────────────────────────────────────────────

    # 结构/情绪（满分 40，含聪明钱）→ timing_score
    if bb_pct > 55:      score += 10; _ms += 10
    if dir1h == '↑':     score += 8;  _ms += 8
    if hammer:           score += 7;  _ms += 7
    if funding < -0.1:   score += 5;  _ts += 5   # 资金费率 → trend_score
    if chg24 > 3:        score += 10; _ms += 10
    score += token_report_score  # 正面+10~15，负面-15~20

    # ── 优化B: BTC 4H大方向协同加分 ──────────────────────────────────────
    # 多头方向：BTC 4H近期涨跌幅决定协同加/扣分
    # 实时拉BTC 4H K线（最多5根），避免依赖外部传参
    btc_4h_chg = 0.0
    _btc_long_bonus = 0
    _btc_long_penalty = 0
    try:
        import urllib.request as _ur, json as _js
        _btc_url = "https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval=4h&limit=5"
        _btc_raw = _js.loads(_ur.urlopen(_btc_url, timeout=5).read())
        if len(_btc_raw) >= 2:
            btc_4h_chg = (float(_btc_raw[-1][4]) - float(_btc_raw[-2][1])) / float(_btc_raw[-2][1]) * 100
        if btc_4h_chg > 0.5:
            _btc_long_bonus = 10
        elif btc_4h_chg < -0.5:
            _btc_long_penalty = -10
    except Exception:
        pass  # 网络异常时跳过，不影响主评分
    score += _btc_long_bonus
    score += _btc_long_penalty
    # ─────────────────────────────────────────────────────────────────────────

    # ── 子分数上限同步（不超过 cap）────────────────────────────────────────
    _ts = max(0, _ts)  # trend_score 不为负
    _ms = max(0, _ms)  # timing_score 不为负
    # [v2.1 同步 paper_test 样本 ≥32笔，激活子分数双门槛]
    # trend_score < 35：趋势确认分过低，EMA/RSI 对齐不足，拒绝入场
    if _ts < 35:
        _detail_reject = {
            "trend_score": _ts, "timing_score": _ms,
            "reject_reason": f"trend_score={_ts}<35，趋势确认不足",
            "final_score": 0,
        }
        return 0, _detail_reject
    # ─────────────────────────────────────────────────────────────────────

    # ── 评分内部一致性检查（v3.3.2）──────────────────────────────────────
    # 只记录日志，不直接拒绝，供后期 analytics 分析矛盾信号实际表现
    _consistency_flags = []
    if rsi_4h > 68 and chg24 > 5:
        _consistency_flags.append(f"RSI超买({rsi_4h:.0f})+大涨幅({chg24:.1f}%)")
    if rsi_4h < 30 and chg24 < -5:
        _consistency_flags.append(f"RSI超卖({rsi_4h:.0f})+连续下跌({chg24:.1f}%)")
    if funding > 0.001 and vr > 1.8:
        _consistency_flags.append(f"资金费率偏高({funding*100:.3f}%)+放量({vr:.1f}x)")
    if _consistency_flags:
        try:
            import pathlib as _pl, datetime as _dtt, json as _jj
            _clog = _pl.Path("/root/.openclaw/workspace/ws_cache/consistency_log.jsonl")
            _entry = {"ts": _dtt.datetime.utcnow().isoformat(), "sym": sym,
                      "flags": _consistency_flags, "score": score,
                      "trend_score": _ts, "timing_score": _ms,
                      "rsi_4h": round(rsi_4h, 1), "chg24": round(chg24, 2),
                      "funding": round(funding, 5), "vr": round(vr, 2)}
            with open(_clog, "a") as _f:
                _f.write(_jj.dumps(_entry, ensure_ascii=False) + "\n")
        except Exception:
            pass

    ema_1h_str = '多排' if ema20_1h > ema50_1h else '空排'
    ema_4h_str = '多排' if ema20_4h > ema50_4h else '空排'

    return score, {
        'rsi_1h': rsi_1h, 'rsi_4h': rsi_4h, 'vr': vr,
        'bb_pct': bb_pct, 'dir': dir1h, 'hammer': hammer,
        'ema_1h': ema_1h_str, 'ema_4h': ema_4h_str,
        'chg24': chg24, 'funding': funding,
        'token_report_score': token_report_score,
        'abs_vol_usdt': abs_vol_usdt,
        'thin_history': thin_history,
        'btc_4h_chg': btc_4h_chg,
        'btc_long_bonus': _btc_long_bonus,
        'btc_long_penalty': _btc_long_penalty,
        'trend_score': _ts,    # 趋势确认子分数（满分60）
        'timing_score': _ms,   # 入场时机子分数（满分40）
    }


# ─────────────────────────────────────────────
# 评分：逆势单
# ─────────────────────────────────────────────

def calc_score_reversal(sym, k1h, k4h, k15m, ticker, funding, token_report_score=0):
    """
    逆势单评分，满分 ~115（含聪明钱加分）
    门槛 65 分
    返回 (score, details_dict)
    """
    # funding 单位化安全层（同 calc_score_trend）
    if abs(funding) < 0.1:
        funding = funding * 100
    c1h  = [float(k[4]) for k in k1h]
    c4h  = [float(k[4]) for k in k4h]
    c15m = [float(k[4]) for k in k15m] if k15m else c1h
    v1h  = [float(k[5]) for k in k1h]

    rsi_1h  = rsi_calc(c1h)
    rsi_4h  = rsi_calc(c4h)
    rsi_15m = rsi_calc(c15m)

    vb  = sum(v1h[-20:-3]) / 17 if len(v1h) >= 20 else sum(v1h) / len(v1h)
    vr  = (sum(v1h[-3:]) / 3) / vb if vb > 0 else 1

    sma20 = sum(c1h[-20:]) / 20
    std20 = (sum((x - sma20) ** 2 for x in c1h[-20:]) / 20) ** 0.5
    bb_u  = sma20 + 2 * std20; bb_l = sma20 - 2 * std20
    bb_pct = (c1h[-1] - bb_l) / (bb_u - bb_l) * 100 if (bb_u - bb_l) > 0 else 50

    lk = k1h[-1]
    o = float(lk[1]); l2 = float(lk[3]); c = float(lk[4])
    body  = abs(c - o); lower = min(o, c) - l2
    hammer = lower > body * 2 and body > 0

    rsi_div = c1h[-1] < c1h[-6] and rsi_1h > rsi_calc(c1h[:-5])
    chg24   = float(ticker.get('priceChangePercent', 0))

    score = 0

    # 超卖深度（满分 35）
    if rsi_4h < 10:   score += 20
    elif rsi_4h < 15: score += 15
    if rsi_1h < 30:   score += 8
    if rsi_15m < 30:  score += 4
    if bb_pct < 5:    score += 3

    # 资金费率（满分 25）— funding 已单位化为百分比
    if funding < -0.5:   score += 15
    elif funding < -0.3: score += 10
    elif funding < -0.1: score += 5

    # 反弹动能（满分 35，含聪明钱）
    if hammer:      score += 10
    if vr > 1.2:    score += 6
    if rsi_div:     score += 4
    score += token_report_score  # 正面+15，负面-20

    # 跌幅（满分 20）
    if chg24 < -20:   score += 12
    elif chg24 < -15: score += 8
    elif chg24 < -10: score += 4

    return score, {
        'rsi_1h': rsi_1h, 'rsi_4h': rsi_4h, 'vr': vr,
        'bb_pct': bb_pct, 'hammer': hammer, 'rsi_div': rsi_div,
        'chg24': chg24, 'funding': funding,
        'token_report_score': token_report_score,
    }


# ─────────────────────────────────────────────
# OI 背离检查
# ─────────────────────────────────────────────

def check_oi_divergence(symbol):
    """
    检查价格创近期高点但 OI 同期下降是否 > 20%
    返回 (is_diverging: bool, oi_chg_pct: float)
    is_diverging=True → 禁止做多
    """
    try:
        oi_data = requests.get(
            f"{BASE_F}/futures/data/openInterestHist",
            params={'symbol': symbol, 'period': '1h', 'limit': 24},
            timeout=8
        ).json()
        if not isinstance(oi_data, list) or len(oi_data) < 6:
            return False, 0.0

        oi_vals   = [float(x['sumOpenInterest']) for x in oi_data]
        oi_recent = oi_vals[-1]
        oi_peak   = max(oi_vals[-24:])
        oi_chg    = (oi_recent - oi_peak) / (oi_peak + 1e-9)

        # 同期价格是否创高
        kl = requests.get(f"{BASE_F}/fapi/v1/klines",
                          params={'symbol': symbol, 'interval': '1h', 'limit': 24},
                          timeout=8).json()
        if not isinstance(kl, list): return False, 0.0
        prices    = [float(k[4]) for k in kl]
        price_now = prices[-1]
        price_24h_high = max(prices)

        near_high  = price_now >= price_24h_high * 0.98  # 接近24h高点
        oi_dropped = oi_chg < -0.20                       # OI 下降超过 20%

        return (near_high and oi_dropped), round(oi_chg * 100, 2)
    except:
        return False, 0.0


# ─────────────────────────────────────────────
# BTC 大环境过滤
# ─────────────────────────────────────────────

def check_funding_settlement_window(funding_rate_pct=0.0):
    """
    资金费率结算窗口拦截。
    每小时整点结算，主力习惯在结算前后砸盘：
      - 普通费率：结算前 15 分钟 + 结算后 5 分钟 = 20 分钟窗口
      - 极端费率（|rate| > 0.5%）：结算前 20 分钟 + 结算后 10 分钟 = 30 分钟窗口
    返回 (is_blocked: bool, reason: str)
    """
    now     = datetime.datetime.utcnow()
    minute  = now.minute
    extreme = abs(funding_rate_pct) > 0.5

    pre_block  = 20 if extreme else 15   # 结算前拦截分钟数
    post_block = 10 if extreme else 5    # 结算后拦截分钟数

    # 结算后窗口：0 ~ post_block 分钟
    if minute < post_block:
        reason = '资金费率刚结算 %d 分钟，窗口拦截 (%s费率=%.3f%%)' % (
            minute, '极端' if extreme else '普通', funding_rate_pct)
        return True, reason

    # 结算前窗口：60 - pre_block ~ 60 分钟
    if minute >= 60 - pre_block:
        mins_to_settlement = 60 - minute
        reason = '距资金费率结算还有 %d 分钟，窗口拦截 (%s费率=%.3f%%)' % (
            mins_to_settlement, '极端' if extreme else '普通', funding_rate_pct)
        return True, reason

    return False, ''


def check_btc_filter(direction: str = "LONG") -> bool:
    """
    BTC 大环境过滤 — 当前已禁用凌晨拦截，始终返回 True。
    如需重新开启，恢复原逻辑即可。
    direction: 'LONG' | 'SHORT'
    返回 True 表示通过，False 表示拦截。
    """
    return True


# ─────────────────────────────────────────────
# Token Report 评分
# ─────────────────────────────────────────────

_TOKEN_REPORT_CACHE: dict = {}   # sym → (score, veto, reason, ts)
_TOKEN_REPORT_TTL = 1800         # 30分钟缓存（一轮扫描内完全复用）

def get_token_report_score(symbol):
    """
    调用 binance-pro-cli 获取 Token Report，返回加减分和是否一票否决。
    返回 (score: int, veto: bool, reason: str)
    缓存30分钟，避免同轮扫描重复调用导致超时阻塞。
    """
    import subprocess, time as _time
    cached = _TOKEN_REPORT_CACHE.get(symbol)
    if cached and (_time.time() - cached[3]) < _TOKEN_REPORT_TTL:
        return cached[0], cached[1], cached[2]
    token = symbol.replace('USDT', '').replace('PERP', '')
    try:
        tr = subprocess.run(
            ["binance-pro-cli", "search", "report", "--symbol", token],
            capture_output=True, text=True, timeout=5  # 压缩超时：超时返回neutral不阻塞
        )
        text = tr.stdout.lower()

        veto_kws = [
            "treasury transfer", "team wallet sell", "team wallet transfer",
            "exploit", "hack", "rug", "exit scam",
            "treasury multisig", "project wallet"
        ]
        positive_kws = [
            "smart money buy", "whale accumulate", "institutional buy",
            "net inflow", "kol buy", "market maker", "团队未动",
            "聪明钱买入", "鲸鱼增持", "净流入"
        ]
        negative_kws = [
            "smart money sell", "whale dump", "net outflow",
            "聪明钱卖出", "鲸鱼减持", "净流出", "大额解锁"
        ]

        hit_veto     = [kw for kw in veto_kws     if kw in text]
        hit_positive = [kw for kw in positive_kws if kw in text]
        hit_negative = [kw for kw in negative_kws if kw in text]

        if hit_veto:
            _r=(-20,True,f"一票否决: {hit_veto[0]}"); _TOKEN_REPORT_CACHE[symbol]=(*_r,_time.time()); return _r
        if hit_negative:
            _r=(-15,False,f"负面信号: {hit_negative[0]}"); _TOKEN_REPORT_CACHE[symbol]=(*_r,_time.time()); return _r
        if hit_positive:
            _r=(12,False,f"正面信号: {hit_positive[0]}"); _TOKEN_REPORT_CACHE[symbol]=(*_r,_time.time()); return _r
        _r=(0,False,"中性/无数据"); _TOKEN_REPORT_CACHE[symbol]=(*_r,_time.time()); return _r
    except Exception as e:
        return 0, False, f"检查异常: {e}"


# ─────────────────────────────────────────────
# Token 安全审计（发行方/合约风险过滤）
# ─────────────────────────────────────────────

_AUDIT_CACHE: dict = {}   # sym → (veto, reason, ts)
_AUDIT_TTL   = 3600       # 1小时缓存，同一个币不重复查

# 主流大币直接跳过审计（合约成熟，无需检查）
_AUDIT_SKIP = {
    "BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT","AVAXUSDT",
    "LINKUSDT","DOTUSDT","ADAUSDT","LTCUSDT","BCHUSDT","MATICUSDT",
    "UNIUSDT","ATOMUSDT","NEARUSDT","APTUSDT","OPUSDT","ARBUSDT",
}

def _get_contract_address(coin: str) -> tuple:
    """
    从 Binance capital API 获取 coin 的合约地址，优先返回 BSC，其次 ETH/BASE/SOL。
    返回 (chain_id, contract_address) 或 (None, None)
    """
    import requests as _req, hmac as _hmac, hashlib as _hs, time as _ti, urllib.parse as _up
    import sys as _sys
    _sys.path.insert(0, "/root/.openclaw/workspace")
    import binance_api as _api

    chain_pref = [("BSC", "56"), ("ETH", "1"), ("BASE", "8453"), ("SOL", "CT_501")]
    try:
        ts     = int(_ti.time() * 1000)
        params = {"timestamp": ts}
        query  = _up.urlencode(params)
        sig    = _hmac.new(_api.SECRET.encode(), query.encode(), _hs.sha256).hexdigest()
        params["signature"] = sig
        r = _req.get(
            "https://api.binance.com/sapi/v1/capital/config/getall",
            headers={"X-MBX-APIKEY": _api.API_KEY},
            params=params, proxies=_api.PROXIES, timeout=8
        )
        coins = r.json()
        if not isinstance(coins, list):
            return None, None
        target = next((c for c in coins if c.get("coin", "").upper() == coin.upper()), None)
        if not target:
            return None, None
        nets = {n["network"]: n.get("contractAddress", "") for n in target.get("networkList", [])}
        for net_name, chain_id in chain_pref:
            addr = nets.get(net_name, "")
            if addr and len(addr) > 10:
                return chain_id, addr
    except Exception:
        pass
    return None, None


def issuer_risk_check(symbol: str) -> tuple:
    """
    开仓前检查 token 合约安全风险。
    返回 (veto: bool, reason: str)
      veto=True  → 一票否决，不开仓
      veto=False → 通过或无数据，继续

    判断逻辑：
      riskLevel >= 4 (HIGH)         → 一票否决
      SCAM_RISK 任意 isHit=True     → 一票否决
      sellTax > 10%                 → 一票否决（貔貅盘）
      CONTRACT_RISK: Mintable+无锁仓 → 降分警告（不否决，记入日志）
    """
    import time as _ti, uuid as _uu, requests as _req
    import sys as _sys
    _sys.path.insert(0, "/root/.openclaw/workspace")
    import binance_api as _api

    if symbol in _AUDIT_SKIP:
        return False, "主流币跳过审计"

    cached = _AUDIT_CACHE.get(symbol)
    if cached and (_ti.time() - cached[2]) < _AUDIT_TTL:
        return cached[0], cached[1]

    coin = symbol.replace("USDT", "").replace("PERP", "")
    chain_id, addr = _get_contract_address(coin)
    if not chain_id or not addr:
        # 拿不到合约地址 → 无法审计，放行但记录
        result = (False, f"无合约地址数据，跳过审计")
        _AUDIT_CACHE[symbol] = (*result, _ti.time())
        return result

    try:
        r = _req.post(
            "https://web3.binance.com/bapi/defi/v1/public/wallet-direct/security/token/audit",
            headers={"Content-Type": "application/json", "source": "agent", "Accept-Encoding": "identity"},
            json={"binanceChainId": chain_id, "contractAddress": addr, "requestId": str(_uu.uuid4())},
            proxies=_api.PROXIES, timeout=8
        )
        d    = r.json()
        data = d.get("data") or {}
        if not data:
            result = (False, "审计API无数据，放行")
            _AUDIT_CACHE[symbol] = (*result, _ti.time())
            return result

        risk_level = int(data.get("riskLevel", 0))
        sell_tax   = float(data.get("extraInfo", {}).get("sellTax") or 0)

        # 一票否决条件
        if risk_level >= 4:
            result = (True, f"合约高风险 riskLevel={risk_level}")
            _AUDIT_CACHE[symbol] = (*result, _ti.time())
            return result

        if sell_tax > 10:
            result = (True, f"卖出税过高 sellTax={sell_tax}%（貔貅盘）")
            _AUDIT_CACHE[symbol] = (*result, _ti.time())
            return result

        for item in data.get("riskItems", []):
            if item.get("id") == "SCAM_RISK":
                scam_hits = [x["title"] for x in item.get("details", []) if x.get("isHit")]
                if scam_hits:
                    result = (True, f"SCAM_RISK命中: {scam_hits[0]}")
                    _AUDIT_CACHE[symbol] = (*result, _ti.time())
                    return result

        result = (False, f"审计通过 riskLevel={risk_level}")
        _AUDIT_CACHE[symbol] = (*result, _ti.time())
        return result

    except Exception as e:
        result = (False, f"审计异常({e})，放行")
        _AUDIT_CACHE[symbol] = (*result, _ti.time())
        return result


# ─────────────────────────────────────────────
# 清算密集位推算（核心）
# ─────────────────────────────────────────────

def _atr_sl_multiplier(atr_14: float, price: float, vol24_usd: float = 0, symbol: str = "") -> float:
    """
    根据 ATR/价格比动态决定止损倍数：
      ATR/price > 15%  → 高波动山寨：2.5
      ATR/price 8-15%  → 中波动：2.0
      ATR/price < 8%   → 主流／正常：1.8

    v2（2026-08-25）：整体上调，原 1.2/1.5/2.0 导致主流币 SL 只有 2-3%，
    正常波动即可扫掉。新值使 SL 扩展到约 4-5%，给仓位更多呼吸空间。
    TP1 由 liquidation_map 同步拉远（liq_above 锚点不变，RR 比例自然维持）。
    """
    try:
        from calc_position_size_real import classify_tier
        tier = classify_tier(atr_14, price, vol24_usd, symbol)
    except Exception:
        ratio = atr_14 / price if price > 0 else 0
        if ratio > 0.15 or (ratio < 0.06 and vol24_usd < 30_000_000):
            tier = "L4"
        elif ratio < 0.04:
            tier = "L1"
        else:
            tier = "L3"
    return {"L1": 1.8, "L2": 1.8, "L3": 2.0, "L4": 2.5}.get(tier, 2.0)


def calc_multi_source_support(symbol, k1h, k4h, direction, lmap=None):
    """
    多源锚点竞选支撑位（优化版）。
    四个来源都计算锁点，选择距现价最近且在合理范围内的一个。

    来源1：清算图锄点（原有 liquidation_map）
    来源2：成交密集区（近00根K线价格分布热力图）
    来源3：前低/前期整理区间下沿（近20根K线最低点）
    来源4：均线支撑（MA25/MA99）

    选择规则：取四个候选中，在 [price*(1-MAX_DRIFT), price*(1-MIN_DRIFT)] 内的，
    距现价偏离最小的一个。
    """
    price = float(k1h[-1][4])
    closes = [float(k[4]) for k in k1h]
    lows   = [float(k[3]) for k in k1h]
    highs  = [float(k[2]) for k in k1h]
    vols   = [float(k[5]) for k in k1h]  # 成交量

    candidates = []  # (price, source_name, confidence)

    # ── 来源1：清算图锄点 ────────────────────────────────────────
    if lmap is not None:
        liq_anchor = lmap.get('liq_below') if direction == 'LONG' else lmap.get('liq_above')
        ob_anchor  = lmap.get('ob_support') if direction == 'LONG' else lmap.get('ob_resistance')
        # 要求订单簿支撑量 > 50万U（案点可靠性过滤）
        try:
            ob = requests.get(f"{BASE_F}/fapi/v1/depth",
                              params={'symbol': symbol, 'limit': 50}, timeout=5).json()
            bid_orders = [(float(p), float(q)) for p, q in ob.get('bids', [])]
            ask_orders = [(float(p), float(q)) for p, q in ob.get('asks', [])]
            # 找最大的挂单墙的总值
            if direction == 'LONG' and bid_orders:
                max_bid = max(bid_orders, key=lambda x: x[1])
                ob_val_u = max_bid[1] * max_bid[0]
                if ob_val_u >= 500_000:  # > 50万U
                    candidates.append((max_bid[0], '订单簿墙', 0.9))
            elif direction == 'SHORT' and ask_orders:
                max_ask = max(ask_orders, key=lambda x: x[1])
                ob_val_u = max_ask[1] * max_ask[0]
                if ob_val_u >= 500_000:
                    candidates.append((max_ask[0], '订单簿墙', 0.9))
        except:
            pass
        if liq_anchor and liq_anchor > 0:
            candidates.append((liq_anchor, '清算图', 0.85))

    # ── 来源2：成交密集区（近100根K线热力图） ─────────────────────
    try:
        n = min(100, len(closes))
        _lows   = lows[-n:]
        _highs  = highs[-n:]
        _vols   = vols[-n:]
        _min_p  = min(_lows)
        _max_p  = max(_highs)
        # 将价格分为 30 个节点
        buckets = 30
        bucket_size = (_max_p - _min_p) / buckets if _max_p > _min_p else 1e-10
        vol_dist = [0.0] * buckets
        for i in range(n):
            mid_p = (_lows[i] + _highs[i]) / 2
            idx = int((mid_p - _min_p) / bucket_size)
            idx = max(0, min(buckets - 1, idx))
            vol_dist[idx] += _vols[i]
        # 成交量最集中的区间
        peak_idx = vol_dist.index(max(vol_dist))
        vol_cluster_price = _min_p + (peak_idx + 0.5) * bucket_size
        candidates.append((vol_cluster_price, '成交密集区', 0.8))
    except:
        pass

    # ── 来源3：前20根K线最低点 / 整理区间下沿 ─────────────────
    try:
        recent_lows = sorted(lows[-20:])[:3]  # 最低的三个小高
        prev_low = sum(recent_lows) / len(recent_lows)  # 平均前低
        candidates.append((prev_low, '前低小高', 0.75))
    except:
        pass

    # ── 来源4：均线支撑（MA25 / MA99）──────────────────────────
    try:
        def _ma(c, n):
            return sum(c[-n:]) / n if len(c) >= n else sum(c) / len(c)
        ma25 = _ma(closes, 25)
        ma99 = _ma(closes, 99)
        for ma_val, ma_name in [(ma25, 'MA25'), (ma99, 'MA99')]:
            if direction == 'LONG' and ma_val < price:
                candidates.append((ma_val, ma_name, 0.7))
            elif direction == 'SHORT' and ma_val > price:
                candidates.append((ma_val, ma_name, 0.7))
    except:
        pass

    if not candidates:
        # 兑底：返回现价偏离 3%
        return price * 0.97 if direction == 'LONG' else price * 1.03, '兑底(ATR)'

    # ── 竞选规则：选距现价最近且在合理偏离范围内的 ───────────
    MIN_DRIFT = 0.003   # 至少偶离现价 0.3%
    MAX_DRIFT = 0.10    # 最多偏离现价 10%

    valid = []
    for cp, name, conf in candidates:
        if direction == 'LONG':
            drift = (price - cp) / price
        else:
            drift = (cp - price) / price
        if MIN_DRIFT <= drift <= MAX_DRIFT:
            valid.append((cp, name, conf, drift))

    if not valid:
        # 所有候选偶离过大或过小，选最近的一个
        if direction == 'LONG':
            best = min(candidates, key=lambda x: abs(price - x[0]))
        else:
            best = min(candidates, key=lambda x: abs(x[0] - price))
        return best[0], best[1]

    # 直接选偏离最小的（距现价最近的支撑位，提高成交概率）
    valid.sort(key=lambda x: x[3])  # 按 drift 升序
    chosen = valid[0]
    return chosen[0], chosen[1]


def liquidation_map(symbol, k1h, k4h, direction):
    """
    用订单簿最大挂单墙 + K线关键位 + OI/多空比/费率 联合推算清算密集区。
    返回字典：
      liq_below    : 下方清算密集价（多头爆仓区）
      liq_above    : 上方清算密集价（空头爆仓区）
      safe_entry   : 推荐入场价
      hard_sl      : 推荐止损（清算密集区外侧）
      soft_tp1     : TP1（下一个清算密集区前沿）
      soft_tp2     : TP2（延伸目标）
      entry_reason : 入场逻辑说明
      ob_support   : 订单簿最大买单墙
      ob_resistance: 订单簿最大卖单墙
      fr_val       : 当前资金费率
      ls_ratio     : 多空比
      oi_increasing: OI 是否过热
    """
    price  = float(k1h[-1][4])
    atr_14 = sum(abs(float(k[2]) - float(k[3])) for k in k1h[-14:]) / 14

    # ── 1. 订单簿：找最大挂单墙 ──
    try:
        ob = requests.get(f"{BASE_F}/fapi/v1/depth",
                          params={'symbol': symbol, 'limit': 100}, timeout=5).json()
        bids = [(float(p), float(q)) for p, q in ob.get('bids', [])]
        asks = [(float(p), float(q)) for p, q in ob.get('asks', [])]
        bids_sorted = sorted(bids[:50], key=lambda x: -x[1])
        asks_sorted = sorted(asks[:50], key=lambda x: -x[1])
        biggest_bid = bids_sorted[0][0] if bids_sorted else price * 0.98
        biggest_ask = asks_sorted[0][0] if asks_sorted else price * 1.02
        ob_support    = biggest_bid
        ob_resistance = biggest_ask
    except:
        biggest_bid   = price - atr_14 * 1.0
        biggest_ask   = price + atr_14 * 1.0
        ob_support    = biggest_bid
        ob_resistance = biggest_ask

    # ── 2. OI 变化 ──
    oi_increasing = False
    try:
        oi_data = requests.get(
            f"{BASE_F}/futures/data/openInterestHist",
            params={'symbol': symbol, 'period': '1h', 'limit': 6}, timeout=5).json()
        if isinstance(oi_data, list) and len(oi_data) >= 4:
            oi_vals = [float(x['sumOpenInterest']) for x in oi_data]
            oi_chg3h = (oi_vals[-1] - oi_vals[-4]) / (oi_vals[-4] + 1e-9)
            oi_increasing = oi_chg3h > 0.05
    except:
        pass

    # ── 3. 多空比 ──
    ls_ratio = 1.0
    try:
        ls_data = requests.get(
            f"{BASE_F}/futures/data/globalLongShortAccountRatio",
            params={'symbol': symbol, 'period': '1h', 'limit': 3}, timeout=5).json()
        if isinstance(ls_data, list) and ls_data:
            ls_ratio = float(ls_data[-1].get('longShortRatio', 1.0))
    except:
        pass

    long_crowded  = ls_ratio > 1.5
    short_crowded = ls_ratio < 0.7

    # ── 4. 资金费率 ──
    fr_val = 0.0
    try:
        fr_data = requests.get(f"{BASE_F}/fapi/v1/premiumIndex",
                               params={'symbol': symbol}, timeout=5).json()
        fr_val = float(fr_data.get('lastFundingRate', 0))
    except:
        pass

    fr_long_heat  = fr_val > 0.0003
    fr_short_heat = fr_val < -0.0003

    # ── 5. K线关键位 ──
    k1h_lows   = [float(k[3]) for k in k1h[-20:]]
    k1h_highs  = [float(k[2]) for k in k1h[-20:]]
    key_support    = min(k1h_lows)
    key_resistance = max(k1h_highs[-12:])

    # ── 6. Coinglass 多时间框架清算密集区（3d主锚 + 7d/30d稀疏补充）──
    # 主锚：3d 高精度近期仓位；3d 稀疏（<2M U）时自动用 7d/30d 补充参考
    _cg_below = None  # 下方主锚清算密集区价格
    _cg_above = None  # 上方主锚清算密集区价格
    _cg_src   = 'kline+ob_fallback'
    _cg_above_ref = None  # 7d/30d 补充参考（上方）
    _cg_below_ref = None  # 7d/30d 补充参考（下方）
    _cg_ref_src   = None
    try:
        import coinglass_api as _cg_api

        # ── 主锚：3天图（高精度近期仓位）──
        _liq = _cg_api.get_liq_zones(symbol, price, top_n=5, range_='3d')
        _b_zones = _liq.get('below', [])   # [(price, volume), ...]
        _a_zones = _liq.get('above', [])
        if _b_zones:
            _cg_below = max(_b_zones, key=lambda z: z[1] if len(z) > 1 else 0)[0]
            _cg_src = 'cg_3d'
        if _a_zones:
            # 取离现价最近的上方清算区
            _cg_above = sorted(_a_zones, key=lambda z: z[0])[0][0]
            _cg_src = 'cg_3d'

        # ── 稀疏度判断：3d 数据量不足时用 7d/30d 补充 ──
        # 稀疏定义：最大清算区体量 < 2M U，说明近3天该方向建仓少，历史清算区更有参考价值
        _3d_above_vol = max((z[1] for z in _a_zones if len(z) > 1), default=0)
        _3d_below_vol = max((z[1] for z in _b_zones if len(z) > 1), default=0)
        _sparse_above = _3d_above_vol < 2_000_000
        _sparse_below = _3d_below_vol < 2_000_000

        # 依次尝试 7d、30d 补充（取第一个成功的）
        for _ref_rng in ['7d', '30d']:
            if not (_sparse_above or _sparse_below):
                break  # 3d 已足够密集，不需要补充
            try:
                _liq_ref = _cg_api.get_liq_zones(symbol, price, top_n=5, range_=_ref_rng)
                _a_ref = _liq_ref.get('above', [])
                _b_ref = _liq_ref.get('below', [])
                if _sparse_above and _a_ref and _cg_above_ref is None:
                    # 优先取体量 >= 3M U 的最近区；无则取最近区
                    _a_ref_sorted = sorted(_a_ref, key=lambda z: z[0])
                    _heavy_a = [z for z in _a_ref_sorted if len(z) > 1 and z[1] >= 3_000_000]
                    _cg_above_ref = (_heavy_a[0][0] if _heavy_a else _a_ref_sorted[0][0])
                    _cg_ref_src = _ref_rng
                if _sparse_below and _b_ref and _cg_below_ref is None:
                    _b_ref_sorted = sorted(_b_ref, key=lambda z: z[0], reverse=True)
                    _heavy_b = [z for z in _b_ref_sorted if len(z) > 1 and z[1] >= 3_000_000]
                    _cg_below_ref = (_heavy_b[0][0] if _heavy_b else _b_ref_sorted[0][0])
                    _cg_ref_src = _ref_rng
                # 两方向都补到了就停
                if (_cg_above_ref or not _sparse_above) and (_cg_below_ref or not _sparse_below):
                    break
            except Exception:
                continue

        # 若 3d 完全无数据，把补充参考升级为主锚
        if _cg_above is None and _cg_above_ref is not None:
            _cg_above = _cg_above_ref
            _cg_src = f'cg_{_cg_ref_src}_fallback'
            _cg_above_ref = None
        if _cg_below is None and _cg_below_ref is not None:
            _cg_below = _cg_below_ref
            _cg_src = f'cg_{_cg_ref_src}_fallback'
            _cg_below_ref = None

    except Exception:
        pass

    # 综合定义清算密集区：Coinglass 优先，否则用 K线结构 + 订单簿墙
    # ATR 波动率加权：价差 > 1 ATR 时采用中间值，避免锚点过远
    # 若 3d 稀疏但有 7d/30d 参考，以 3d 主锚为主（权重0.7）、参考锚补充（权重0.3）做融合
    kline_s = min(key_support, biggest_bid)
    kline_r = max(key_resistance, biggest_ask)

    if _cg_below is not None and _cg_below < price:
        if _cg_below_ref is not None and _cg_below_ref < price:
            # 3d主锚+长期参考融合（主锚更近更精确，权重0.7）
            _merged_below = _cg_below * 0.7 + _cg_below_ref * 0.3
        else:
            _merged_below = _cg_below
        liq_below = _merged_below * 0.6 + kline_s * 0.4
        liq_below = max(liq_below, price - atr_14 * 3.0)
    elif _cg_below_ref is not None and _cg_below_ref < price:
        # 3d 无数据，纯用长期参考（权重降低，与K线各半）
        liq_below = _cg_below_ref * 0.5 + kline_s * 0.5
        liq_below = max(liq_below, price - atr_14 * 3.0)
    else:
        liq_below = kline_s

    if _cg_above is not None and _cg_above > price:
        if _cg_above_ref is not None and _cg_above_ref > price:
            # 3d主锚+长期参考融合；取更近的那个作为主要压力
            _ref_closer = _cg_above_ref < _cg_above  # 长期参考更近（说明有更近的历史压力）
            _merged_above = (_cg_above_ref * 0.6 + _cg_above * 0.4) if _ref_closer else (_cg_above * 0.7 + _cg_above_ref * 0.3)
        else:
            _merged_above = _cg_above
        liq_above = _merged_above * 0.6 + kline_r * 0.4
        liq_above = min(liq_above, price + atr_14 * 3.0)
    elif _cg_above_ref is not None and _cg_above_ref > price:
        liq_above = _cg_above_ref * 0.5 + kline_r * 0.5
        liq_above = min(liq_above, price + atr_14 * 3.0)
    else:
        liq_above = kline_r

    # OI/多空比修正：过热时微调清算密集区（小幅修正，不覆盖 Coinglass 数据）
    if oi_increasing and long_crowded:   liq_below = min(liq_below + atr_14 * 0.2, price - atr_14 * 0.5)
    if oi_increasing and short_crowded:  liq_above = max(liq_above - atr_14 * 0.2, price + atr_14 * 0.5)

    # ── 7. 确定入场/止损/止盈 ──
    entry_reason = ""
    if direction == 'SHORT':
        # 空单入场：在卖单墙（压力位）下方 ATR×0.05 挂单，越贴近成交率越高
        safe_entry = biggest_ask - atr_14 * 0.05
        # 若 Coinglass 有上方清算密集区，入场贴近清算区前沿更精准
        if _cg_above is not None and _cg_above > price:
            safe_entry = min(_cg_above - atr_14 * 0.1, biggest_ask - atr_14 * 0.05)
        entry_reason = (
            f"多头过热(FR={fr_val*100:.3f}%,L/S={ls_ratio:.2f})，清算区={_cg_above or biggest_ask:.5f}前沿入场[{_cg_src}]"
            if (fr_long_heat or long_crowded)
            else f"常规空单，压力位{biggest_ask:.5f}下方入场[{_cg_src}]"
        )
        hard_sl  = liq_above + atr_14 * 0.5   # 止损在清算密集区上方
        soft_tp1 = liq_below + atr_14 * 0.3   # TP1 在下方清算密集区上沿
        soft_tp2 = liq_below - atr_14 * 0.5   # TP2 穿过清算密集区
    else:  # LONG
        # 多单入场：在买单墙（支撑位）上方 ATR×0.05，贴近成交
        safe_entry = biggest_bid + atr_14 * 0.05
        if _cg_below is not None and _cg_below < price:
            safe_entry = max(_cg_below + atr_14 * 0.1, biggest_bid + atr_14 * 0.05)
        entry_reason = (
            f"空头过热(FR={fr_val*100:.3f}%,L/S={ls_ratio:.2f})，清算区={_cg_below or biggest_bid:.5f}上方入场[{_cg_src}]"
            if (fr_short_heat or short_crowded)
            else f"常规多单，支撑位{biggest_bid:.5f}上方入场[{_cg_src}]"
        )
        _vol24 = float(k1h[-1][7]) * float(k1h[-1][4]) if len(k1h[-1]) > 7 else 0
        sl_mult  = _atr_sl_multiplier(atr_14, price, _vol24, symbol)
        hard_sl  = liq_below - atr_14 * sl_mult   # 止损在清算密集区下方
        soft_tp1 = liq_above - atr_14 * 0.3
        soft_tp2 = liq_above + atr_14 * 0.5

    # 止损兜底：不能比入场价更差
    if direction == 'SHORT' and hard_sl <= safe_entry:
        hard_sl = safe_entry + atr_14 * 1.5
    if direction == 'LONG'  and hard_sl >= safe_entry:
        _vol24b = float(k1h[-1][7]) * float(k1h[-1][4]) if len(k1h[-1]) > 7 else 0
        sl_mult  = _atr_sl_multiplier(atr_14, price, _vol24b, symbol)
        hard_sl = safe_entry - atr_14 * sl_mult

    return {
        'liq_below':     round(liq_below,     8),
        'liq_above':     round(liq_above,     8),
        'safe_entry':    round(safe_entry,    8),
        'hard_sl':       round(hard_sl,       8),
        'soft_tp1':      round(soft_tp1,      8),
        'soft_tp2':      round(soft_tp2,      8),
        'entry_reason':  entry_reason,
        'ob_support':    round(ob_support,    8),
        'ob_resistance': round(ob_resistance, 8),
        'fr_val':        fr_val,
        'ls_ratio':      ls_ratio,
        'oi_increasing': oi_increasing,
        'atr_14':        atr_14,
    }


# ─────────────────────────────────────────────
# 入场模式判断（v2 — 废弃 MA25，使用清算区+订单簿墙+K线结构）
# ─────────────────────────────────────────────

def _find_anchor_long(lmap, k1h, atr):
    """
    多单入场锚点计算 v2 — 三维综合锚点，不依赖固定价格或 MA25。

    综合权重：
      1. Coinglass 清算密集区（liq_below）  权重最高 —— 清算区就是多头爆仓聚集地，
         价格靠近时支撑力最强，是最有意义的入场锚点。
      2. 订单簿买单墙（ob_support）         权重中等 —— 反映当前市场挂单意愿。
      3. K线结构支撑（近20根低点中位数）     权重辅助 —— 避免被一根插针拉偏。
      4. ATR 波动率约束                      兜底 —— 锚点不能离现价超过 2.5 ATR。

    返回 (anchor_price, anchor_source: str)
    """
    liq_below  = lmap['liq_below']
    ob_support = lmap['ob_support']
    price      = float(k1h[-1][4])
    k_lows     = sorted([float(k[3]) for k in k1h[-20:]])
    kline_mid  = k_lows[9]   # 近20根低点中位数（第10低），比最低值更稳健

    # 各候选锚点距现价的距离（%）
    def dist(p): return (price - p) / price * 100 if price > 0 else 999

    # 过滤：锚点必须在现价下方，且不能超过 2.5 ATR
    max_dist = atr * 2.5
    candidates = []
    if liq_below > 0 and liq_below < price and (price - liq_below) <= max_dist:
        candidates.append((liq_below, 'liq_below', 3))      # 权重3
    if ob_support > 0 and ob_support < price and (price - ob_support) <= max_dist:
        candidates.append((ob_support, 'ob_support', 2))    # 权重2
    if kline_mid > 0 and kline_mid < price and (price - kline_mid) <= max_dist:
        candidates.append((kline_mid, 'kline_mid20', 1))    # 权重1

    if not candidates:
        # 所有锚点都太远，用 ATR×1.5 贴近现价
        return round(price - atr * 1.5, 8), 'atr_fallback'

    # 加权综合：weight × price / sum(weights)
    total_w = sum(w for _, _, w in candidates)
    anchor  = sum(p * w for p, _, w in total_w and candidates) / total_w if total_w > 0 else candidates[0][0]
    # 修正语法
    anchor  = sum(p * w for p, _, w in candidates) / total_w
    src     = '+'.join(s for _, s, _ in candidates)

    # 清算区与订单簿墙高度重叠时（价差≤0.5%），优先返回两者均值（共振点更强）
    if liq_below > 0 and ob_support > 0:
        overlap_pct = abs(liq_below - ob_support) / max(liq_below, ob_support, 1e-9) * 100
        if overlap_pct <= 0.5:
            return round((liq_below + ob_support) / 2.0, 8), 'liq+ob_resonance'

    return round(anchor, 8), src


def _find_anchor_short(lmap, k1h, atr):
    """
    空单入场锚点计算 v2 — 三维综合锚点，逻辑与 _find_anchor_long 对称。

    综合权重：
      1. Coinglass 清算密集区（liq_above）   权重最高 —— 空头爆仓聚集地，压力最强。
      2. 订单簿卖单墙（ob_resistance）        权重中等。
      3. K线结构压力（近20根高点中位数）       权重辅助。
      4. ATR 波动率约束                        兜底，锚点不超过 2.5 ATR。

    返回 (anchor_price, anchor_source: str)
    """
    liq_above     = lmap['liq_above']
    ob_resistance = lmap['ob_resistance']
    price         = float(k1h[-1][4])
    k_highs       = sorted([float(k[2]) for k in k1h[-20:]], reverse=True)
    kline_mid     = k_highs[9]   # 近20根高点中位数（第10高）

    max_dist = atr * 2.5
    candidates = []
    if liq_above > 0 and liq_above > price and (liq_above - price) <= max_dist:
        candidates.append((liq_above, 'liq_above', 3))
    if ob_resistance > 0 and ob_resistance > price and (ob_resistance - price) <= max_dist:
        candidates.append((ob_resistance, 'ob_resistance', 2))
    if kline_mid > 0 and kline_mid > price and (kline_mid - price) <= max_dist:
        candidates.append((kline_mid, 'kline_mid20', 1))

    if not candidates:
        return round(price + atr * 1.5, 8), 'atr_fallback'

    # 清算区与卖单墙高度重叠（共振点）
    if liq_above > 0 and ob_resistance > 0:
        overlap_pct = abs(liq_above - ob_resistance) / max(liq_above, ob_resistance, 1e-9) * 100
        if overlap_pct <= 0.5:
            return round((liq_above + ob_resistance) / 2.0, 8), 'liq+ob_resonance'

    total_w = sum(w for _, _, w in candidates)
    anchor  = sum(p * w for p, _, w in candidates) / total_w
    src     = '+'.join(s for _, s, _ in candidates)
    return round(anchor, 8), src


def _dev_threshold(atr: float, price: float) -> float:
    """
    根据 ATR/价格比动态决定偏差阈值：
      ATR/price > 15%  → 高波动山寨：ATR×1.5（无固定上限，只要锚点合理）
      ATR/price 6-15%  → 山寨币：6%
      ATR/price < 6%   → 主流/正常：3%
    """
    ratio = atr / price if price > 0 else 0
    if ratio > 0.15:
        return atr * 1.5 / price * 100  # 动态：ATR×1.5 换算成 %
    elif ratio > 0.06:
        return 6.0
    else:
        return 3.0


# 条件单偏差上限：超过此比例说明锚点离现价太远，无意义
_COND_ORDER_MAX_DEV = 30.0   # 30%，超出直接真WAIT
_COND_ORDER_MIN_SCORE = 60   # 条件单最低评分门槛


def calc_entry_mode(k1h, direction, lmap, vol_threshold=1.0, final_score=0):
    """
    判断入场模式，返回 (mode, limit_price, wait_seconds, anchor_source)
    mode: 'CHASE' | 'PULLBACK' | 'COND_ORDER' | 'WAIT'

    v4 — 条件限价单模式：
    原来 PULLBACK 偏差超阈值 → WAIT 不挂单
    现在 PULLBACK 偏差超阈值 → COND_ORDER（预埋条件限价单，等价格回调触发）
    只有锚点偏差>30% 或 评分<60 或 方向不明 才走真正的 WAIT。
    """
    c1h   = [float(k[4]) for k in k1h]
    v1h   = [float(k[5]) for k in k1h]
    price = c1h[-1]
    atr   = lmap['atr_14']

    highs_20  = [float(k[2]) for k in k1h[-20:]]
    near_high = max(highs_20)
    rsi_1h    = rsi_calc(c1h)

    vb        = sum(v1h[-10:-3]) / 7 if len(v1h) >= 10 else sum(v1h) / len(v1h)
    vol_ratio = (sum(v1h[-3:]) / 3) / vb if vb > 0 else 1

    open_price = float(k1h[0][1])
    open_chg   = (price - open_price) / open_price * 100
    dist_high  = (near_high - price) / near_high * 100 if near_high > 0 else 0

    # ── 动态偏差阈值（文档5.3节） ──
    dev_max = _dev_threshold(atr, price)

    # ── 第零步：偏差分流（v2.10核心改动）──────────────────────────────────
    # 背景：UBUSDT/AKEUSDT等强势标的，清算区在市价下方5~15%，
    #       系统直接挂清算区边缘限价单，4H内永远等不到成交，循环踏空。
    # 改动：在评分分流之前，先算市价与清算区锚点的偏差，根据偏差大小决定入场模式。
    #
    # 偏差定义：|市价 - 清算区锚点| / 市价
    # 动态阈值（dev_max）：ATR%>15% → ATR*1.5%；ATR%6~15% → 6%；ATR%<6% → 3%
    #
    # 三档分流：
    #   偏差 ≤ dev_max          → 清算区附近，正常走评分分流（原逻辑不变）
    #   dev_max < 偏差 ≤ 30%   → 清算区偏远，做追涨合法性验证，通过→TREND_RIDE，否→COND_ORDER
    #   偏差 > 30%             → 价格严重脱离清算区，WAIT（不管评分多高）
    #
    # 注意：偏差分流只对「有清算区锚点」的情况生效；无清算区时偏差=None，走原有逻辑。
    _anchor_for_dev = lmap.get('liq_below') or lmap.get('ob_support') or 0
    if direction == 'SHORT':
        _anchor_for_dev = lmap.get('liq_above') or lmap.get('ob_resistance') or 0

    _dev_from_anchor = None
    if _anchor_for_dev and _anchor_for_dev > 0:
        _dev_from_anchor = abs(price - _anchor_for_dev) / price * 100

    if _dev_from_anchor is not None and _dev_from_anchor > 30.0:
        # 偏差>30%：价格严重脱离，无论评分多高都不入场
        return 'WAIT', price, 0, f'dev_too_large_{_dev_from_anchor:.1f}pct'

    # _needs_trend_ride_check：偏差在(dev_max, 30%]之间，需要做追涨合法性验证
    # 注意：仅对 LONG 方向生效；空单走的是PULLBACK等回调，不需要追涨验证
    _needs_trend_ride_check = (
        _dev_from_anchor is not None and
        _dev_from_anchor > dev_max and
        direction == 'LONG'
    )

    # ── 第一步：刚突破判断 ──
    just_breakout = False
    if len(highs_20) >= 3:
        prev_high = max(highs_20[:-2])
        c1 = c1h[-1]; c2 = c1h[-2]
        just_breakout = (c1 > prev_high or c2 > prev_high) and near_high > prev_high

    # 追涨条件（全部满足）
    # v2.9修复: 量比门槛从1.5降到1.2，RSI上限从65放宽到70
    # 原因: 昨天UBUSDT/AKEUSDT量比1.2~1.5、RSI65~70，全部被chase_conditions拦在外面
    # 导致强势突破信号走PULLBACK → 价格直接拉升不回调 → 踏空
    chase_conditions = (
        just_breakout and
        vol_ratio >= 1.2 and
        rsi_1h < 70 and
        dist_high > 1.0
    )

    # 回调条件（任意一条）
    # chase 作为少数高分突破场景，不满足 chase 全部条件时就走 pullback
    # 消除任何 dist_high 区间导致的无人带
    pullback_conditions = (
        not chase_conditions or          # chase 不满足就是 pullback
        dist_high < 1.0 or
        rsi_1h > 65 or
        (open_chg > 2.0 and vol_ratio < 1.5)
    )

    # ── DUAL_NET 门槛判断 ──
    # v2.8修复: 门槛从85提高到88，让85~87分标的仍走PULLBACK等回调
    # 原因: SNDKUSDT案例—评分85直接走DUAL_NET(市价附近入)，偏离清算区仅0.9%
    # 等于零回调保护，被插针扫止损。88分以上才说明信号足够强，值得不等回调
    _DUAL_NET_MIN_SCORE = 88

    # ── CHASE 路径 ── v2.9: 门槛从80降到75，讨75~87分强势突破信号也可追涨入场
    if chase_conditions and not pullback_conditions and final_score >= 75:
        if direction == 'LONG':
            limit_price = price * 1.001
        else:
            limit_price = price * 0.999
        dev_pct = abs(limit_price - price) / price * 100
        if dev_pct > dev_max:
            # 高分 CHASE 偏差过大 → 降级到 COND_ORDER 而非直接丢弃
            if final_score >= _COND_ORDER_MIN_SCORE:
                return 'COND_ORDER', round(limit_price, 8), 14400, 'chase_cond_order_fallback'
            return 'WAIT', price, 0, 'chase_dev_too_large'
        return 'CHASE', round(limit_price, 8), 3600, 'mark_price'

    # ── TREND_RIDE 路径（v2.9新增）──────────────────────────────────────
    # 触发场景：连续单边拉升行情，价格持续高于均线，没有回调机会
    # 特征：EMA多排 + 量比持续>1.0 + 近3根K线无大插针 + 未突破近高点（不触发CHASE）
    # 核心逻辑：不等回调，贴现价上方 ATR*0.3 挂单，给微小波动留缓冲
    # 区别于CHASE：CHASE需要刚突破高点；TREND_RIDE是已在趋势中持续运行
    # 区别于PULLBACK：PULLBACK等回调到支撑区；TREND_RIDE顺势贴近现价
    c1h_cl  = c1h  # 已有
    ema9_v  = ema_calc(c1h_cl[-9:],  9)  if len(c1h_cl) >= 9  else price
    ema20_v = ema_calc(c1h_cl[-20:], 20) if len(c1h_cl) >= 20 else price
    ema50_v = ema_calc(c1h_cl[-50:], 50) if len(c1h_cl) >= 50 else price
    bull_ema = (ema9_v > ema20_v > ema50_v)  # 三线多排

    # 近3根K线无大插针（下影线 < ATR*0.8）
    recent3 = k1h[-3:]
    max_lower_wick = max(
        abs(float(k[1]) - float(k[3])) for k in recent3  # open - low
    ) if recent3 else atr
    no_big_spike = max_lower_wick < atr * 0.8

    # 量比持续放大（近5根均量 > 之前20根均量）
    v_recent5 = sum(v1h[-5:]) / 5   if len(v1h) >= 5  else 0
    v_base20  = sum(v1h[-25:-5]) / 20 if len(v1h) >= 25 else (sum(v1h) / len(v1h) if v1h else 1)
    vol_trend_ok = v_recent5 > v_base20 * 1.0  # 量能未萎缩即可

    trend_ride_ok = (
        direction == 'LONG' and
        bull_ema and
        no_big_spike and
        vol_trend_ok and
        rsi_1h < 72 and
        final_score >= 70 and
        not just_breakout
    )

    # ── 追涨合法性验证（v2.10新增）───────────────────────────────────────
    # 当偏差在(dev_max, 30%]之间时（清算区偏远），TREND_RIDE必须通过以下4个条件才允许
    # 这个验证是trend_ride_ok的上层过滤：先满足trend_ride_ok，再判断合法性
    # 不通过：降级到COND_ORDER，挂清算区边缘等回调，4小时有效期
    if trend_ride_ok and _needs_trend_ride_check:
        _chase_valid = True
        _chase_fail_reason = ''

        # 条件1：评分≥·85分（追涨风险更高，需要更强的信号补偿）
        if final_score < 85:
            _chase_valid = False
            _chase_fail_reason = f'score={final_score}<85'

        # 条件2：最近15分钟已闭和3根K线量比均>1.2
        if _chase_valid:
            _v15m = lmap.get('v15m_recent3', [])  # scanner填充，无则用v1h代理
            if not _v15m:
                _v15m_ratio = vol_ratio  # fallback用已有量比
            else:
                _v15m_base = sum(_v15m) / len(_v15m) if _v15m else 1
                _v15m_ratio = min(_v15m) / (_v15m_base if _v15m_base > 0 else 1)
            if _v15m_ratio < 1.2:
                _chase_valid = False
                _chase_fail_reason = f'15m_vol_ratio={_v15m_ratio:.2f}<1.2'

        # 条件3：RSI1H < 72（已在trend_ride_ok里检查，这里仅做标记）
        # 条件4：盈亏比 ≥ 1.5（上方清算区到入场价 ÷ 入场价到止损价）
        if _chase_valid:
            _tp_above = lmap.get('soft_tp1') or lmap.get('liq_above_price') or 0
            _sl_below = lmap.get('hard_sl') or 0
            _tr_entry  = price - atr * 0.3  # 预估TREND_RIDE入场价
            if _tp_above > 0 and _sl_below > 0 and _tr_entry > _sl_below:
                _rr = (_tp_above - _tr_entry) / (_tr_entry - _sl_below)
                if _rr < 1.5:
                    _chase_valid = False
                    _chase_fail_reason = f'rr={_rr:.2f}<1.5'
            # 没有TP数据时不强制检查，后续由calc_trend_ride_params重算

        if not _chase_valid:
            # 合法性验证未通过：降级到COND_ORDER，挂清算区边缘等回调
            _cond_price = _anchor_for_dev * 1.002 if _anchor_for_dev > 0 else price * (1 - dev_max/100)
            return 'COND_ORDER', round(_cond_price, 8), 14400, f'trend_ride_check_fail:{_chase_fail_reason}'

    if trend_ride_ok:
        # 入场价：现价 - ATR*0.3（给极小回调留缓冲，提高成交概率）
        limit_price = price - atr * 0.3
        if limit_price <= 0: limit_price = price * 0.997
        dev_pct = abs(limit_price - price) / price * 100
        if dev_pct > dev_max:
            return 'COND_ORDER', round(limit_price, 8), 14400, 'trend_ride_cond_fallback'
        return 'TREND_RIDE', round(limit_price, 8), 7200, 'ema_trend_follow'  # 有效期2H

    # ── PULLBACK 路径 ──
    # v2.8修复1: 增加最小偏差门槛 min_dev
    # 原因: NBISUSDT案例——偏差仅0.3%，系统判断“已在清算区附近直接入”，实际没有等到任何回调
    # 修复: 当偏差 < ATR×0.5 时，不允许直接入，必须等价格进一步回调到 锐点 - ATR×0.5 的位置再挂单
    if pullback_conditions or (chase_conditions and final_score < _DUAL_NET_MIN_SCORE):
        if direction == 'LONG':
            anchor, anchor_src = _find_anchor_long(lmap, k1h, atr)
            limit_price = anchor + atr * 0.2
            if limit_price >= price:
                limit_price = price * 0.99 - atr * 0.2
                anchor_src += '+adj_below_price'
        else:
            anchor, anchor_src = _find_anchor_short(lmap, k1h, atr)
            limit_price = anchor - atr * 0.2
            if limit_price <= price:
                limit_price = price * 1.01 + atr * 0.2
                anchor_src += '+adj_above_price'

        dev_pct = abs(limit_price - price) / price * 100

        # v2.8修复: 动态最小偏差门槛（精准版）
        # 分析结论: 大部分标的就是强势上涨不给回调，统一平移锁价造成大量蹏空
        # 正确方案: 只对高位插针风险标的（RSI4H>68 且 24H涨幅>10%）施加最小偏差保护
        # 其他情况保留原始锁价，不强制平移
        _chg24 = float(k1h[-1][4]) / float(k1h[-25][4]) * 100 - 100 if len(k1h)>=25 else 0
        _needs_min_dev = (rsi_1h > 68 and _chg24 > 10)
        if _needs_min_dev:
            _min_dev_pct = atr * 0.4 / price * 100
            if dev_pct < _min_dev_pct:
                if direction == 'LONG':
                    limit_price = price * (1.0 - _min_dev_pct / 100)
                else:
                    limit_price = price * (1.0 + _min_dev_pct / 100)
                dev_pct = abs(limit_price - price) / price * 100
                anchor_src += '+min_dev_highrsi'

        if dev_pct > dev_max:
            # ── v4: PULLBACK 偏差过大 → 评估是否能异步挂条件单 ──
            # 假设条件: 评分足够、偶差在可接受范围内（20%内）
            if (final_score >= _COND_ORDER_MIN_SCORE and
                    dev_pct <= _COND_ORDER_MAX_DEV):
                return 'COND_ORDER', round(limit_price, 8), 14400, anchor_src
            return 'WAIT', price, 0, f'pullback_dev_{dev_pct:.1f}%>(threshold={dev_max:.1f}%)_from_{anchor_src}'

        return 'PULLBACK', round(limit_price, 8), 14400, anchor_src

    # ── 方向不明 → WAIT ──
    return 'WAIT', price, 0, 'no_clear_condition'


# ─────────────────────────────────────────────
# 做空评分体系
# ─────────────────────────────────────────────

def calc_score_trend_short(sym, k1h, k4h, k15m, ticker, funding, token_report_score=0):
    """
    趋势延续空单评分，满分 ~100（含聪明钱加分）
    专为"跌势中段+EMA空排"场景设计，与 calc_score_short（超涨反转）完全独立。
    门槛：≥60 进观察区，≥80 直接挂单
    """
    # funding 单位化安全层（同 calc_score_trend）
    if abs(funding) < 0.1:
        funding = funding * 100
    c1h  = [float(k[4]) for k in k1h]
    c4h  = [float(k[4]) for k in k4h]
    c15m = [float(k[4]) for k in k15m] if k15m else c1h
    h1h  = [float(k[2]) for k in k1h]
    l1h  = [float(k[3]) for k in k1h]
    v1h  = [float(k[5]) for k in k1h]
    chg24 = float(ticker.get('priceChangePercent', 0))
    price = float(ticker.get('lastPrice', c1h[-1]))

    rsi_1h  = rsi_calc(c1h)
    rsi_4h  = rsi_calc(c4h)
    rsi_15m = rsi_calc(c15m) if c15m != c1h else rsi_1h

    # EMA（9/20/50 三级）
    ema9_1h  = ema_calc(c1h[-9:],  9)
    ema20_1h = ema_calc(c1h[-20:], 20)
    ema50_1h = ema_calc(c1h[-50:], 50) if len(c1h) >= 50 else ema_calc(c1h, len(c1h))
    ema9_4h  = ema_calc(c4h[-9:],  9)
    ema20_4h = ema_calc(c4h[-20:], 20)
    ema50_4h = ema_calc(c4h[-50:], 50) if len(c4h) >= 50 else ema_calc(c4h, len(c4h))
    ema9_15m = ema_calc(c15m[-9:], 9) if len(c15m) >= 9 else ema_calc(c15m, len(c15m))
    ema20_15m= ema_calc(c15m[-20:], 20) if len(c15m) >= 20 else ema_calc(c15m, len(c15m))

    # 空排：ema9 < ema20 < ema50
    bear_4h  = ema9_4h  < ema20_4h  and ema20_4h  < ema50_4h
    bear_1h  = ema9_1h  < ema20_1h  and ema20_1h  < ema50_1h
    bear_15m = ema9_15m < ema20_15m if len(c15m) >= 20 else False

    # 量能
    vb  = sum(v1h[-20:-3]) / 17 if len(v1h) >= 20 else sum(v1h) / max(len(v1h), 1)
    vr  = (sum(v1h[-3:]) / 3) / vb if vb > 0 else 1
    # 下跌时量比（近3根中阴线的量）
    bear_candles_vol = sum(v1h[i] for i in range(-3, 0) if c1h[i] < c1h[i-1])
    bear_vol_ratio   = (bear_candles_vol / max(vb, 1e-10)) if bear_candles_vol > 0 else 0
    # 反弹时量比（近3根中阳线的量）
    bull_candles_vol = sum(v1h[i] for i in range(-3, 0) if c1h[i] >= c1h[i-1])
    bull_vol_ratio   = (bull_candles_vol / max(vb, 1e-10)) if bull_candles_vol > 0 else 0

    # BB%
    sma20 = sum(c1h[-20:]) / 20
    std20 = (sum((x - sma20) ** 2 for x in c1h[-20:]) / 20) ** 0.5
    bb_u  = sma20 + 2 * std20
    bb_l  = sma20 - 2 * std20
    bb_pct = (c1h[-1] - bb_l) / (bb_u - bb_l) * 100 if (bb_u - bb_l) > 0 else 50

    # MACD 1H（简化：ema12 vs ema26 相对位置）
    ema12_1h = ema_calc(c1h[-12:], 12) if len(c1h) >= 12 else ema_calc(c1h, len(c1h))
    ema26_1h = ema_calc(c1h[-26:], 26) if len(c1h) >= 26 else ema_calc(c1h, len(c1h))
    macd_bear = ema12_1h < ema26_1h   # MACD 空头排列（快线在慢线下方）

    # 反弹幅度 vs 前跌幅（斐波那契38.2%回调检测）
    recent_low   = min(l1h[-20:])
    recent_high  = max(h1h[-20:])
    total_drop   = recent_high - recent_low if recent_high > recent_low else 1
    current_rebound = price - recent_low
    rebound_ratio   = current_rebound / total_drop if total_drop > 0 else 1
    weak_rebound    = rebound_ratio < 0.382   # 反弹不足38.2%，趋势延续性强

    score = 0
    details = {}

    # ── 1. 趋势结构（35分）────────────────────────────────
    t_score = 0
    if bear_4h:   t_score += 15   # 4H全空排（最重要）
    if bear_1h:   t_score += 10   # 1H全空排
    if bear_15m:  t_score += 6    # 15M空排（短线确认）
    if price < ema50_1h: t_score += 4  # 价格在EMA50下方（替代MA200）
    score += t_score
    details['trend_score'] = t_score

    # ── 2. 动量维度（25分）────────────────────────────────
    m_score = 0
    if 30 <= rsi_4h <= 60:  m_score += 10   # 4H RSI跌势中段
    elif rsi_4h < 30:       m_score += 5    # 超卖区：仍给分，但少（可能反弹）
    if 35 <= rsi_1h <= 65:  m_score += 8    # 1H RSI跌势中段
    if macd_bear:           m_score += 7    # MACD空头排列
    score += m_score
    details['momentum_score'] = m_score

    # ── 3. 量能维度（20分）────────────────────────────────
    v_score = 0
    if bull_vol_ratio < 1.0:  v_score += 8  # 反弹无量（卖压未减）
    if bear_vol_ratio > 1.2:  v_score += 7  # 下跌放量（趋势确认）
    elif bear_vol_ratio > 0.8: v_score += 3
    # OI代理：近5根K线成交量持续放大（模拟空头持续入场）
    if len(v1h) >= 5 and v1h[-1] > v1h[-2] > v1h[-3]: v_score += 5
    score += v_score
    details['volume_score'] = v_score

    # ── 4. 结构情绪（20分）────────────────────────────────
    e_score = 0
    ma20_price = sum(c1h[-20:]) / 20
    if price < ma20_price:     e_score += 6  # 价格在近20根均值下方
    if bb_pct < 50:            e_score += 5  # BB%在中轨下方
    if 0 <= funding <= 0.3:    e_score += 5  # 资金费率正常偏正（多头仍在付费）
    elif funding > 0.3:        e_score += 3  # 费率偏高，略有过热
    if weak_rebound:           e_score += 4  # 弱反弹，趋势延续性强
    score += e_score
    details['emotion_score'] = e_score

    # ── 聪明钱（同 calc_score_short）──────────────────────
    score += token_report_score
    details['token_report_score'] = token_report_score

    # ── 第7项补充一：BTC 1H EMA 方向协同降级（v3.2）───────────────────────────
    # 如果 BTC 1H EMA20 > EMA50（BTC 仍在多头结构），空单信号降级为弱信号：
    # 趋势确认分（trend_score）上限降至 45 分
    # 不直接拒绝，但只有结构极清晰的空单才能通过
    _btc_in_bull = False
    _btc_ema_cap_applied = False
    try:
        import urllib.request as _ur2, json as _js2
        _url2 = "https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval=1h&limit=52"
        _btc_k = _js2.loads(_ur2.urlopen(_url2, timeout=5).read())
        if len(_btc_k) >= 50:
            _bc = [float(k[4]) for k in _btc_k]
            _be20 = ema_calc(_bc[-20:], 20)
            _be50 = ema_calc(_bc[-50:], 50)
            _btc_in_bull = _be20 > _be50
    except Exception:
        pass
    if _btc_in_bull:
        # BTC 多头结构：趋势确认分上限 45
        t_score_cap = 45
        if t_score > t_score_cap:
            score -= (t_score - t_score_cap)
            details['trend_score'] = t_score_cap
            _btc_ema_cap_applied = True
    details['btc_in_bull_struct'] = _btc_in_bull
    details['btc_ema_cap_applied'] = _btc_ema_cap_applied
    # ─────────────────────────────────────────────────────────────────────

    details.update({
        'rsi_1h': rsi_1h, 'rsi_4h': rsi_4h,
        'bear_4h': bear_4h, 'bear_1h': bear_1h,
        'macd_bear': macd_bear, 'weak_rebound': weak_rebound,
        'bb_pct': bb_pct, 'vr': vr,
        'rebound_ratio': rebound_ratio,
        'chg24': chg24, 'funding': funding,
    })
    return score, details


def calc_score_short(sym, k1h, k4h, k15m, ticker, funding, token_report_score=0):
    """
    做空信号评分，满分 ~110（含聪明钱）
    门槛：观察区 ≥75，直接开空 ≥85
    返回 (score, details_dict)
    """
    # funding 单位化安全层（同 calc_score_trend）
    if abs(funding) < 0.1:
        funding = funding * 100
    c1h  = [float(k[4]) for k in k1h]
    c4h  = [float(k[4]) for k in k4h]
    c15m = [float(k[4]) for k in k15m] if k15m else c1h
    h1h  = [float(k[2]) for k in k1h]
    v1h  = [float(k[5]) for k in k1h]

    rsi_1h  = rsi_calc(c1h)
    rsi_4h  = rsi_calc(c4h)
    rsi_15m = rsi_calc(c15m)

    # EMA（用 9/20/50 判断空排）
    ema9_1h  = ema_calc(c1h[-9:],  9)
    ema20_1h = ema_calc(c1h[-20:], 20)
    ema50_1h = ema_calc(c1h[-50:], 50) if len(c1h) >= 50 else ema_calc(c1h, len(c1h))
    ema9_4h  = ema_calc(c4h[-9:],  9)  if len(c4h) >= 9  else ema_calc(c4h, len(c4h))
    ema20_4h = ema_calc(c4h[-20:], 20) if len(c4h) >= 20 else ema_calc(c4h, len(c4h))
    ema50_4h = ema_calc(c4h[-50:], 50) if len(c4h) >= 50 else ema_calc(c4h, len(c4h))
    ema9_15m = ema_calc(c15m[-9:], 9)  if len(c15m) >= 9  else ema_calc(c15m, len(c15m))
    ema20_15m= ema_calc(c15m[-20:],20) if len(c15m) >= 20 else ema_calc(c15m, len(c15m))
    ema50_15m= ema_calc(c15m[-50:],50) if len(c15m) >= 50 else ema_calc(c15m, len(c15m))

    # MA200（1H 做多 用 ema_calc 近似）
    ma200_1h = ema_calc(c1h[-200:], 200) if len(c1h) >= 200 else ema_calc(c1h, len(c1h))
    below_ma200 = c1h[-1] < ma200_1h

    # BB%
    sma20 = sum(c1h[-20:]) / 20
    std20 = (sum((x - sma20)**2 for x in c1h[-20:]) / 20)**0.5
    bb_u  = sma20 + 2*std20; bb_l = sma20 - 2*std20
    bb_pct = (c1h[-1] - bb_l) / (bb_u - bb_l) * 100 if (bb_u - bb_l) > 0 else 50

    # 量比
    vb  = sum(v1h[-20:-3]) / 17 if len(v1h) >= 20 else sum(v1h) / max(len(v1h),1)
    vr  = (sum(v1h[-3:]) / 3) / vb if vb > 0 else 1

    # 顶背离：价格近3根K线创新高，但RSI未跟随创新高
    price_new_high = c1h[-1] >= max(c1h[-10:-1])
    rsi_prev       = rsi_calc(c1h[:-3])
    top_divergence = price_new_high and rsi_1h < rsi_prev

    chg24 = float(ticker.get('priceChangePercent', 0))

    score = 0
    details = {}

    # ── 趋势评分（30分）──
    t_score = 0
    full_bear_4h = ema9_4h < ema20_4h < ema50_4h
    full_bear_1h = ema9_1h < ema20_1h < ema50_1h
    full_bear_15m= ema9_15m < ema20_15m < ema50_15m
    if full_bear_4h:   t_score += 12
    if full_bear_1h:   t_score += 8
    if full_bear_15m:  t_score += 6
    if below_ma200:    t_score += 4
    score += t_score
    details['trend_score'] = t_score
    details['ema_4h'] = '空排' if full_bear_4h else '非空排'
    details['ema_1h'] = '空排' if full_bear_1h else '非空排'

    # ── 动量评分（25分）──
    # 见顶K线：上影线>实体2倍 或 阴包阳
    top_candle = False
    if len(k1h) >= 2:
        _k=k1h[-1]; _kp=k1h[-2]
        _o=float(_k[1]); _h=float(_k[2]); _c=float(_k[4])
        _op=float(_kp[1]); _cp=float(_kp[4])
        _body=abs(_c-_o); _upper=_h-max(_o,_c)
        shooting_star = _upper > _body*2 and _body > 0
        bearish_engulf = _c < _op and _o > _cp and _c < _o
        top_candle = shooting_star or bearish_engulf

    # 反转信号：顶背离 OR 见顶K线（二选一，消除互斥矛盾）
    reversal_signal = top_divergence or top_candle

    m_score = 0
    if rsi_4h > 75:    m_score += 10
    elif rsi_4h > 65:  m_score += 5
    if rsi_1h > 70:    m_score += 8
    elif rsi_1h > 60:  m_score += 4
    # 原只有顶背离，现在见顶K线同样加分
    if reversal_signal: m_score += 7
    score += m_score
    details['momentum_score'] = m_score
    details['top_candle'] = top_candle
    details['rsi_1h'] = rsi_1h
    details['rsi_4h'] = rsi_4h
    details['top_divergence'] = top_divergence

    # ── 量能评分（20分）──
    v_score = 0
    if vr < 0.6:   v_score += 8    # 严重萎缩
    elif vr < 1.0: v_score += 4    # 轻微萎缩
    # OI背离（复用已有函数）
    try:
        oi_div, oi_chg = check_oi_divergence(sym)
        if oi_div: v_score += 7
        details['oi_divergence'] = oi_div
    except:
        details['oi_divergence'] = False
    # 成交量持续萎缩（近5根均小于近20根均）
    if len(v1h) >= 20:
        v5  = sum(v1h[-5:]) / 5
        v20 = sum(v1h[-20:]) / 20
        if v5 < v20 * 0.8: v_score += 5
    score += v_score
    details['volume_score'] = v_score
    details['vr'] = vr

    # ── 结构情绪评分（25分）──
    e_score = 0
    if funding > 0.5:   e_score += 10
    elif funding > 0.2: e_score += 5
    if bb_pct > 95:     e_score += 8
    elif bb_pct > 85:   e_score += 4
    if chg24 > 20:      e_score += 5
    elif chg24 > 10:    e_score += 2
    # 多空比（近似：资金费率极高时多头占比>70%）
    if funding > 0.3:   e_score += 2
    score += e_score
    details['emotion_score'] = e_score
    details['bb_pct'] = bb_pct
    details['funding'] = funding
    details['chg24'] = chg24

    # ── Token Report 加减分（聪明钱）──
    # 注意：做空时正面信号（净流入）→ 一票否决由调用方处理
    score += token_report_score
    details['token_report_score'] = token_report_score

    return score, details


def classify_short_entry(k1h, k15m, lmap, funding, sym='', vol_24h=0):
    """
    判断做空入场方式：'CHASE'（追空）| 'BOUNCE'（反弹做空）| 'WAIT'（观望）
    返回 (mode, limit_price)

    v2 — 山寨币专属路径：
      对于高波动山寨币（ATR/price > 8%），使用独立的入场逻辑：
        - 暴拉后高位追空（CHASE_ALT）：暴拉 ≥15% 后已从高点回落 ≥5%，RSI 仍偏高（>55）→ 直接追空
        - 高位减速做空（FADE）：RSI ≥75 且 bb_pct ≥85 → 在现价上方 ATR×0.3 挂空
        - 否则沿用主流币逻辑，但偏差阈值放宽
    """
    c1h  = [float(k[4]) for k in k1h]
    h1h  = [float(k[2]) for k in k1h]
    l1h  = [float(k[3]) for k in k1h]
    c15m = [float(k[4]) for k in k15m] if k15m else c1h

    price   = c1h[-1]
    atr     = atr_calc(k1h)
    rsi_1h  = rsi_calc(c1h)
    rsi_15m = rsi_calc(c15m)
    atr_pct = atr / price * 100 if price > 0 else 0

    # BB%
    sma20 = sum(c1h[-20:]) / 20 if len(c1h) >= 20 else sum(c1h) / len(c1h)
    std20 = (sum((x - sma20)**2 for x in c1h[-20:]) / 20)**0.5 if len(c1h) >= 20 else 0
    bb_u  = sma20 + 2*std20; bb_l = sma20 - 2*std20
    bb_pct = (c1h[-1] - bb_l) / (bb_u - bb_l) * 100 if (bb_u - bb_l) > 0 else 50

    # 近20根最高价 & 距高点距离
    near_high = max(h1h[-20:])
    dist_from_high = (near_high - price) / near_high * 100 if near_high > 0 else 0

    # ────────────────────────────────────────────────
    # 山寨币专属路径（ATR/price > 8%）
    # ────────────────────────────────────────────────
    if atr_pct > 6.0:
        # 近24根内最大涨幅（识别暴拉）
        low_24 = min(l1h[-24:]) if len(l1h) >= 24 else min(l1h)
        max_24 = max(h1h[-24:]) if len(h1h) >= 24 else max(h1h)
        surge_pct = (max_24 - low_24) / low_24 * 100 if low_24 > 0 else 0

        # 路径 A：暴拉后追空（CHASE_ALT）
        # 条件：24h 涨幅 ≥15% + 已从高点回落 ≥5% + RSI>55（还没超卖）+ 量比萎缩（动能衰减）
        v1h = [float(k[5]) for k in k1h]
        vb  = sum(v1h[-20:-3]) / 17 if len(v1h) >= 20 else sum(v1h) / max(len(v1h), 1)
        vr  = (sum(v1h[-3:]) / 3) / vb if vb > 0 else 1
        # CHASE_ALT RSI条件：RSI>55（原有）OR RSI从>80快速下跌>20点（暴拉后迅速崩盘）
        rsi_fast_drop = False
        if len(k1h) >= 5:
            rsi_prev3 = rsi_calc([float(k[4]) for k in k1h[:-3]])
            rsi_fast_drop = rsi_prev3 > 80 and (rsi_prev3 - rsi_1h) > 20
        rsi_chase_ok = rsi_1h > 55 or rsi_fast_drop
        if (surge_pct >= 15 and dist_from_high >= 5.0 and
                rsi_chase_ok and vr < 1.0):
            limit_price = price - atr * 0.1
            return 'CHASE_ALT', round(limit_price, 8)

        # 路径 B：高位减速做空（FADE）
        # 条件：RSI ≥75 且 BB% ≥85（极度超买顶部）
        if rsi_1h >= 75 and bb_pct >= 85:
            # 挂在现价上方 ATR×0.3（等小反弹，比主流币保守一点）
            limit_price = price + atr * 0.3
            return 'FADE', round(limit_price, 8)

        # 路径 C：沿用主流逻辑，但偏差阈值自动放宽（由调用方 dev_max 控制）
        # 不做额外限制，走下方通用逻辑

    # ────────────────────────────────────────────────
    # 通用路径（主流币 + 山寨币 fallback）
    # ────────────────────────────────────────────────

    # ── FADE（通用）：高位 RSI 60-82 且 BB%>80 ──
    # 填补"高分空单 RSI 在评分区间(65-80)但没有入场路径"的盲区
    # 逻辑：价格在布林上轨附近，RSI 过热，等小反弹 ATR×0.2 再挂空
    if 60 <= rsi_1h <= 82 and bb_pct >= 80:
        limit_price = price + atr * 0.2
        return 'FADE', round(limit_price, 8)

    # ── 支撑位：近20根低点中位数（比最低5根均值更稳健）──
    # 原：sum(sorted(l1h[-20:])[:5]) / 5  → 容易被插针价拉低，just_broke 触发过严
    # 新：近20根低点排序后取第10位（中位数），代表真实支撑带中枢
    sorted_lows = sorted(l1h[-20:])
    support = sorted_lows[9]   # 第10低 = 近20根中位数支撑

    # 追空条件：价格刚跌破支撑中枢（最近2根K线），RSI 未超卖
    just_broke = c1h[-1] < support and c1h[-2] >= support
    rsi_not_oversold = rsi_1h > 35

    if just_broke and rsi_not_oversold:
        limit_price = price - atr * 0.05   # 贴近现价下方一点点，提高成交率
        return 'CHASE', round(limit_price, 8)

    # ── BOUNCE：从高点回落中段，等反弹到 ATR 锚点再空 ──
    # RSI 上限放宽到 65（原60），避免与 FADE 路径重叠同时覆盖中间地带
    recent_low    = min(l1h[-20:])
    dist_to_low   = (price - recent_low) / recent_low * 100
    dist_from_h20 = (near_high - price) / near_high * 100 if near_high > 0 else 0
    bounce_cond   = dist_from_h20 > 3.0 and dist_to_low > 8.0 and rsi_1h < 65
    if bounce_cond:
        # 锚点优先用清算图压力位，没有则 ATR×0.3
        resistance = lmap.get('liq_above', lmap.get('ob_resistance', price + atr * 0.4))
        limit_price = min(resistance, price + atr * 0.3)
        return 'BOUNCE', round(limit_price, 8)

    return 'WAIT', price


# ─────────────────────────────────────────────────────────────────────────────
# calc_trend_ride_params  (v2.10 新增)
# TREND_RIDE 模式专属止损止盈仓位计算
# 入场价更高（贴近市价），止损/止盈/仓位必须单独计算，不能复用 PULLBACK 套路
# ─────────────────────────────────────────────────────────────────────────────
def calc_trend_ride_params(
    sym: str,
    direction: str,
    entry_price: float,
    lmap: dict,
    k1h: list,
    score: int,
    max_pos_usd: float = 1000.0,
    risk_per_trade: float = 50.0,   # 单笔固定风险额（USDT）
    leverage: int = 5,
) -> dict:
    """
    TREND_RIDE 专属参数计算，返回:
      sl        止损价
      tp1       止盈1（60%仓位平仓）
      tp2       止盈2（40%仓位平仓）
      pos_usd   名义仓位（USDT），用固定风险法计算再取评分档位上限
      rr        盈亏比（tp1 - entry）/（entry - sl）
      valid     是否满足最低盈亏比要求（≥1.5）

    止损逻辑：
      找入场价下方最近的清算密集区，止损放该区外侧 - ATR*sl_mult
      sl_mult 动态：ATR/价格>15% → 2.0；8~15% → 1.5；<8% → 1.2
      安全检查：sl 不能高于 entry（否则用 entry - ATR*sl_mult 兜底）

    止盈逻辑：
      TP1 = 上方最近清算区前沿 - ATR*0.3 缓冲（避免落在真空带）
      TP2 = 上方密度最大清算区 + ATR*0.5（穿越目标）

    仓位逻辑（固定风险法）：
      pos_usd = risk_per_trade / (|entry - sl| / entry) / leverage
      再和评分档位上限取 min
    """
    atr = lmap.get('atr_14', entry_price * 0.01)
    atr_pct = atr / entry_price * 100

    # ── 止损 ──
    sl_mult = 2.0 if atr_pct > 15 else (1.5 if atr_pct > 8 else 1.2)

    # 优先用入场价附近的下方清算区外侧
    liq_below_near = lmap.get('liq_below') or lmap.get('hard_sl') or 0
    if liq_below_near > 0 and liq_below_near < entry_price:
        sl = liq_below_near - atr * sl_mult
    else:
        sl = entry_price - atr * sl_mult

    # 安全检查
    if sl >= entry_price:
        sl = entry_price - atr * sl_mult

    # ── 止盈 ──
    liq_above_near = lmap.get('liq_above') or lmap.get('soft_tp1') or 0
    liq_above_max  = lmap.get('soft_tp2') or lmap.get('liq_above') or 0

    if liq_above_near > entry_price:
        tp1 = liq_above_near - atr * 0.3
    else:
        tp1 = entry_price + atr * 3.0   # fallback

    if liq_above_max > entry_price:
        tp2 = liq_above_max + atr * 0.5
    else:
        tp2 = entry_price + atr * 5.0   # fallback

    # ── 盈亏比 ──
    sl_dist  = entry_price - sl
    tp1_dist = tp1 - entry_price
    rr = tp1_dist / sl_dist if sl_dist > 0 else 0

    # ── 仓位（固定风险法）──
    sl_pct = sl_dist / entry_price if entry_price > 0 else 0.02
    pos_risk = risk_per_trade / sl_pct / leverage if sl_pct > 0 else 300

    # 评分档位上限
    if score >= 90:   score_cap = 1000
    elif score >= 80: score_cap = 700
    elif score >= 70: score_cap = 500
    else:             score_cap = 300

    pos_usd = min(pos_risk, score_cap, max_pos_usd)
    pos_usd = max(pos_usd, 100)   # 不低于100U

    return {
        'sl':      round(sl, 8),
        'tp1':     round(tp1, 8),
        'tp2':     round(tp2, 8),
        'pos_usd': round(pos_usd, 2),
        'rr':      round(rr, 2),
        'valid':   rr >= 1.5,
        'sl_mult': sl_mult,
        'atr':     atr,
        'note':    f'TREND_RIDE sl_mult={sl_mult} rr={rr:.2f} pos={pos_usd:.0f}U',
    }
