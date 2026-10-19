#!/usr/bin/env python3
"""
bstock_scanner.py v2.0 — bStock 自动交易工作流
改进：
  1. 限价挂单入场（比当前价低 LIMIT_OFFSET_PCT，有效期 LIMIT_TTL_SEC）
  2. 合约 WebSocket 实时价格监控持仓，止盈/止损延迟 <1s
  3. 扫描并发化（ThreadPoolExecutor），68标的从5min→1min
  4. 挂单状态追踪 + 过期自动取消
  5. TP1/TP2/SL/时间止损逻辑修复
"""

import sys, json, time, subprocess, datetime, math, logging, threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, '/root/.openclaw/workspace')
import binance_api as api

# ✨ 接入 Clawby 新信号
try:
    from fx.shared.clawby_adapter import (
        get_btc_etf_flow, get_btc_etf_aum,
        get_hl_whale_alerts, get_hl_whale_positions,
        get_altcoin_season, get_fear_greed,
    )
    _CLAWBY_OK = True
except ImportError:
    _CLAWBY_OK = False

# ── 配置 ──────────────────────────────────────────────────────────────────────
STATE_FILE   = Path('/tmp/bstock_state.json')
LOG_FILE     = Path('/tmp/bstock_scanner.log')
BSTOCK_LIST  = Path('/tmp/bstock_list.json')

USDT_ADDR    = "0x55d398326f99059fF775485246999027B3197955"
CHAIN_ID     = "56"

PER_TRADE_U     = 300           # 每笔买入金额（本金缩减至1500U，单仓300U×5）
MIN_SCORE       = 70            # 最低入场分（提高门槛过滤弱信号）
TP1_PCT         = 0.035         # 止盈一 3.5%（盈亏比优化：原2.5%→3.5%，目标盈亏比≥1.4）
TP2_PCT         = 0.070         # 止盈二 7.0%（原4.5%→7.0%）
SL_HARD_PCT     = 0.05          # 硬止损 5%
SL_MAX_PCT      = 0.10          # SL最大距离上限 10%（防低价币小数点异常）
LIMIT_OFFSET_PCT= 0.000         # 限价单贴近现价挂单（原-0.3%→0%，降低snap1_miss率）
LIMIT_TTL_SEC   = 7200          # 限价单有效期 2h（超时自动取消）
SCAN_INTERVAL   = 1800          # 主扫描间隔 30min
WS_CHECK_INTV   = 30            # WS 断线重连检测间隔
MAX_NEW_PER_ROUND = 5           # 每轮最多挂新单数量（最多5仓）

# ── 新版入场参数 ──────────────────────────────────────────────────────────────
MAX_CONCURRENT_MONITORS = 5     # 同时监控的标的上限（最多5仓）
MONITOR_WINDOW_SEC      = 7200  # 单个标的监控超时(2h)，超时后重扫
ENTRY_BAND_PCT          = 0.005 # 入场触发区间 ±0.5%
MAX_NEW_30MIN           = 5     # 30分钟内最多开仓数（最多5仓）
TOP_CANDIDATES          = 5     # 每次扫描取 Top5（对应5仓上限）

# 美股交易时段 UTC
US_OPEN  = (13, 30)
US_CLOSE = (20,  0)

# ── 日志 ──────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M UTC',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger('bstock')

# ── 全局 WS 价格缓存 ─────────────────────────────────────────────────────────
_ws_prices: dict[str, float] = {}   # fut_sym -> last_price
_ws_lock   = threading.Lock()
_ws_proc   = None                   # WebSocket 子进程
_ws_thread = None

# ── Jarvis 通知 ──────────────────────────────────────────────────────────────
def notify(msg: str):
    try:
        subprocess.run([
            'openclaw', 'message', 'send',
            '--channel', 'jarvis',
            '--to', '432401404:thread:019fcac8-4a93-7bea-a930-7f0e59e4897c',
            '--message', msg
        ], capture_output=True, timeout=10)
    except Exception as e:
        log.warning(f"notify失败: {e}")

# ── 状态管理 ─────────────────────────────────────────────────────────────────
_state_lock = threading.Lock()

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {
        'positions':     {},   # sym -> position dict
        'pending_orders':{},   # sym -> pending limit order dict
        'history':       [],
        'total_pnl':     0.0,
        'scan_count':    0,
    }

def save_state(state: dict):
    with _state_lock:
        STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))

# ── 时间工具 ─────────────────────────────────────────────────────────────────
# 收盘前禁止开仓的提前量（单位：分钟）
US_PRE_CLOSE_MIN = 120   # 收盘前 2h 停止开新仓
# 休市后收紧止损的时间窗口（单位：分钟）
US_TIGHTEN_WINDOW_MIN = 30  # 收盘后 30min 内执行一次止损收紧

def us_market_open() -> bool:
    now = datetime.datetime.utcnow()
    t = (now.hour, now.minute)
    return US_OPEN <= t < US_CLOSE

def us_allow_new_entry() -> bool:
    """[v2.1 活动冲榜模式] 24小时允许开新仓，不再限制交易时段
    休市期间流动性低/滑点大，由信号质量和 should_hold_or_sl 判断是否进场
    活动结束后恢复为：open_min <= t_min < (close_min - US_PRE_CLOSE_MIN)
    """
    return True  # 24h全时段运行，争取活动排名

def us_just_closed() -> bool:
    """是否处于收盘后 30min 窗口（止损收紧窗口）"""
    now   = datetime.datetime.utcnow()
    t_min = now.hour * 60 + now.minute
    close_min = US_CLOSE[0] * 60 + US_CLOSE[1]
    return close_min <= t_min < close_min + US_TIGHTEN_WINDOW_MIN

def now_str() -> str:
    return datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')

# ── 技术分析 ─────────────────────────────────────────────────────────────────
def ema(c: list, n: int) -> float:
    if len(c) < n: n = len(c)
    k = 2 / (n + 1); e = c[0]
    for x in c[1:]: e = x * k + e * (1 - k)
    return e

def rsi(c: list, n: int = 14) -> float:
    if len(c) < n + 1: return 50
    g = [max(c[i] - c[i-1], 0) for i in range(1, len(c))]
    l = [max(c[i-1] - c[i], 0) for i in range(1, len(c))]
    ag = sum(g[-n:]) / n; al = sum(l[-n:]) / n
    return 100 if al == 0 else 100 - 100 / (1 + ag / al)

def atr(k: list, n: int = 14) -> float:
    trs = []
    for i in range(1, len(k)):
        h = float(k[i][2]); lo = float(k[i][3]); pc = float(k[i-1][4])
        trs.append(max(h - lo, abs(h - pc), abs(lo - pc)))
    return sum(trs[-n:]) / n if trs else 0

def _has_reversal_candle(k1h: list, lookback: int = 3) -> bool:
    """
    检测最近 lookback 根 1h K 线是否有止跨形态：
    条件：下影线 >= 实体的 1.5倍 （锤子线 / 十字星线）且收阳
    """
    for bar in k1h[-lookback:]:
        o, h, l, c = float(bar[1]), float(bar[2]), float(bar[3]), float(bar[4])
        body   = abs(c - o)
        lower  = min(o, c) - l
        upper  = h - max(o, c)
        if body > 0 and lower >= body * 1.5 and lower > upper and c > o:
            return True
    return False

def _rsi_bullish_divergence(closes: list, n_check: int = 6) -> bool:
    """
    简化 RSI 底部背离：近 n_check 根 K 线价格创新低 但 RSI 未创新低
    """
    if len(closes) < n_check + 14: return False
    def _rsi(c):
        g=[max(c[i]-c[i-1],0) for i in range(1,len(c))]
        l=[max(c[i-1]-c[i],0) for i in range(1,len(c))]
        ag=sum(g[-14:])/14; al=sum(l[-14:])/14
        return 100 if al==0 else 100-100/(1+ag/al)
    prev_low_price = min(closes[-(n_check*2):-n_check])
    curr_low_price = min(closes[-n_check:])
    if curr_low_price >= prev_low_price: return False  # 价格没创新低
    prev_rsi = _rsi(closes[-(n_check*2+14):-n_check])
    curr_rsi = _rsi(closes[-(n_check+14):])
    return curr_rsi > prev_rsi + 3   # RSI 没创新低 = 背离

def score_signal(sym: str, k1h: list, k4h: list, funding: float, chg24: float) -> dict:
    """
    改进版评分逻辑 v2.1：
    - 强制门槛：1H 空排 + 近8h 跌幅 > -2% 直接拒绝
    - 超卖必须配合止跨形态（K线锤子 / RSI背离）才加分
    - 入场价用真实支撑位（布林下轨 / 8h低点），不是「现价-0.3%」
    - 1H 趋势确认作为开仓必要条件
    """
    c1h  = [float(x[4]) for x in k1h]
    c4h  = [float(x[4]) for x in k4h]
    v1h  = [float(x[5]) for x in k1h]
    price = c1h[-1]

    e9_1h  = ema(c1h[-9:],  9)
    e20_1h = ema(c1h[-20:], 20)
    e9_4h  = ema(c4h[-9:],  9)
    e20_4h = ema(c4h[-20:], 20)

    r1h   = rsi(c1h)
    r4h   = rsi(c4h)
    atr1h = atr(k1h)

    bull_1h = e9_1h > e20_1h and price > e20_1h
    bull_4h = e9_4h > e20_4h

    vol_avg = sum(v1h[-20:]) / 20
    vol_r   = v1h[-1] / vol_avg if vol_avg > 0 else 1

    h24 = max(float(k[2]) for k in k1h[-24:])
    l24 = min(float(k[3]) for k in k1h[-24:])
    h8  = max(float(k[2]) for k in k1h[-8:])
    l8  = min(float(k[3]) for k in k1h[-8:])

    sma20 = sum(c1h[-20:]) / 20
    std20 = (sum((x - sma20) ** 2 for x in c1h[-20:]) / 20) ** 0.5
    bb_u  = sma20 + 2 * std20
    bb_l  = sma20 - 2 * std20
    bb_pct = (price - bb_l) / (bb_u - bb_l) * 100 if bb_u != bb_l else 50

    # 近期趋势
    trend_8h  = (c1h[-1] - c1h[-9])  / c1h[-9]  * 100 if len(c1h) >= 9  else 0
    trend_3h  = (c1h[-1] - c1h[-4])  / c1h[-4]  * 100 if len(c1h) >= 4  else 0

    reasons = []
    score   = 50

    # ═══════════════════════════════════════════════════════
    # 强制门槛（1）1H 空排 — 直接拒绝，不论评分
    # 复盘：IRENB 被套两次根因：1H空排时仍入场，接飞刀
    # ═══════════════════════════════════════════════════════
    if not bull_1h:
        return {
            'score': 0, 'direction': 'REJECT',
            'limit_price': price, 'cur_price': price,
            'sl': 0, 'tp1': 0, 'tp2': 0, 'atr': atr1h,
            'reasons': f'[强制拒绝] 1H空排(e9={e9_1h:.3f}<e20={e20_1h:.3f})，等1H多排确认后再入场',
        }

    # 强制门槛（2）4H 空排 — 直接拒绝
    # 复盘：全仓7笔浮亏根因：开盘时段集中买入，全部4H空排，中期趋势未反转
    if not bull_4h:
        return {
            'score': 0, 'direction': 'REJECT',
            'limit_price': price, 'cur_price': price,
            'sl': 0, 'tp1': 0, 'tp2': 0, 'atr': atr1h,
            'reasons': f'[强制拒绝] 4H空排(e9={e9_4h:.3f}<e20={e20_4h:.3f})，等4H多排确认后再入场',
        }

    # 强制门槛（3）双空排兜底（理论上被门槛1/2覆盖，保留防御）
    if not bull_1h and not bull_4h and bb_pct > 30:
        return {
            'score': 0, 'direction': 'REJECT',
            'limit_price': price, 'cur_price': price,
            'sl': 0, 'tp1': 0, 'tp2': 0, 'atr': atr1h,
            'reasons': f'[强制拒绝] 双空排且布林{bb_pct:.0f}%不在支撑区',
        }

    # 强制门槛（3）布林上轨以上（bb_pct > 90）且非强对齐——追高就是平时赏罚
    if bb_pct > 90 and not (bull_1h and bull_4h and r1h < 70):
        return {
            'score': 0, 'direction': 'REJECT',
            'limit_price': price, 'cur_price': price,
            'sl': 0, 'tp1': 0, 'tp2': 0, 'atr': atr1h,
            'reasons': f'[强制拒绝] 布林上轨追高({bb_pct:.0f}%)',
        }

    # ═══════════════════════════════════════════════════════
    # EMA 趋势结构（必要条件：1H 趋势合格）
    # ═══════════════════════════════════════════════════════
    if bull_1h and bull_4h:
        score += 15; reasons.append('双周期多排+15')
    elif bull_4h and not bull_1h:
        # 4H 多排但 1H 空排：必须有止跨形态才能进
        has_rev = _has_reversal_candle(k1h) or _rsi_bullish_divergence(c1h)
        if has_rev:
            score += 5; reasons.append('4H多排+1H止跨形态+5')
        else:
            score -= 5; reasons.append('4H多排但无止跨确认-5')
    elif bull_1h and not bull_4h:
        score += 3;  reasons.append('1H多排待确认+3')
    else:  # 双空排
        score -= 15; reasons.append('双空排-15')

    # ═══════════════════════════════════════════════════════
    # RSI — 超卖必需止跨形态才加分
    # ═══════════════════════════════════════════════════════
    if 40 <= r1h <= 60 and 40 <= r4h <= 65:
        score += 10; reasons.append(f'RSI中性(1H={r1h:.0f},4H={r4h:.0f})+10')
    elif r1h < 35:
        has_rev   = _has_reversal_candle(k1h, lookback=4)
        has_div   = _rsi_bullish_divergence(c1h)
        if has_rev or has_div:
            bonus = 12 if has_div else 8
            tag   = 'RSI背离' if has_div else 'K线止跨'
            score += bonus; reasons.append(f'超卖已止跨({tag},RSI={r1h:.0f})+{bonus}')
        else:
            # 裸超卖不加分，反而小扣（超卖可能更超卖）
            score -= 5; reasons.append(f'裸超卖无确认(RSI={r1h:.0f})-5')
    elif r1h > 75:
        score -= 15; reasons.append(f'1H超买(RSI={r1h:.0f})-15')
    elif 60 < r1h <= 75:
        score += 5;  reasons.append(f'RSI偶强(1H={r1h:.0f})+5')

    # ═══════════════════════════════════════════════════════
    # 资金费率
    # ═══════════════════════════════════════════════════════
    if -0.02 <= funding <= 0.02:
        score += 8;  reasons.append(f'费率中性({funding:.3f}%)+8')
    elif funding < -0.05:
        score += 12; reasons.append(f'费率超负({funding:.3f}%)+12')
    elif funding > 0.05:
        score -= 8;  reasons.append(f'费率过高({funding:.3f}%)-8')

    # ═══════════════════════════════════════════════════════
    # 24h 涨跌幅
    # ═══════════════════════════════════════════════════════
    if -8 <= chg24 <= -1:
        score += 10; reasons.append(f'适度回调({chg24:.1f}%)+10')
    elif 0 < chg24 <= 5:
        score += 5;  reasons.append(f'温和上涨({chg24:.1f}%)+5')
    elif chg24 > 10:
        score -= 5;  reasons.append(f'涨幅过大({chg24:.1f}%)-5')
    elif chg24 < -12:
        score -= 12; reasons.append(f'暴跌({chg24:.1f}%)-12')
    elif chg24 < -8:
        score -= 5;  reasons.append(f'跌幅偏大({chg24:.1f}%)-5')

    # ═══════════════════════════════════════════════════════
    # 布林带位置
    # ═══════════════════════════════════════════════════════
    if 10 <= bb_pct <= 40:
        score += 10; reasons.append(f'布林中下轨({bb_pct:.0f}%)+10')
    elif bb_pct < 10:
        # 贴近下轨要求止跨形态
        has_rev = _has_reversal_candle(k1h) or _rsi_bullish_divergence(c1h)
        if has_rev:
            score += 12; reasons.append(f'布林下轨止跨({bb_pct:.0f}%)+12')
        else:
            score += 3;  reasons.append(f'布林极下(无确认,{bb_pct:.0f}%)+3')
    elif 40 < bb_pct <= 60:
        score += 3;  reasons.append(f'布林中轨({bb_pct:.0f}%)+3')
    elif bb_pct > 80:
        score -= 10; reasons.append(f'布林上轨追高({bb_pct:.0f}%)-10')

    # ═══════════════════════════════════════════════════════
    # 近期趋势奉奖惩
    # ═══════════════════════════════════════════════════════
    if trend_3h > 0.5:
        score += 5;  reasons.append(f'近3h上涨({trend_3h:+.1f}%)+5')
    elif trend_8h > 1.0:
        score += 5;  reasons.append(f'近8h上涨({trend_8h:+.1f}%)+5')

    # 量比
    if 0.3 <= vol_r <= 0.8:
        score += 5;  reasons.append(f'缩量({vol_r:.1f}x)+5')
    elif vol_r > 3:
        score -= 5;  reasons.append(f'放量异常({vol_r:.1f}x)-5')

    score = max(0, min(100, int(score)))

    # ═══════════════════════════════════════════════════════
    # 入场价计算：真实支撑位，不是「现价 -0.3%」
    # 策略：布林下轨 / 8h低点 / 24h低点取最高者作为支撑位
    # 如果支撑位在现价以下：挂在支撑位 + 0.1%（等廙跳）
    # 如果支撑位在现价以上（已穿趋支撑）：挂现价 -0.2%
    # ═══════════════════════════════════════════════════════
    support = max(bb_l, l8)          # 取布林下轨和 8h低点中较高者作为支撑
    support = max(support, l24 * 0.99)  # 不能低于 24h 低点的 99%

    if support < price * 0.998:          # 支撑位在现价以下：挂单在支撑上方小幅
        limit_price = round(support * 1.001, 6)
    else:                                # 支撑位已在现价附近或以上：微回调挂单
        limit_price = round(price * 0.998, 6)

    # 硬止损：最大距离不超过 SL_MAX_PCT（10%）防止低价币小数点异常
    hard_sl     = round(max(l24 * 0.975, price * (1 - SL_HARD_PCT)), 6)
    hard_sl     = max(hard_sl, price * (1 - SL_MAX_PCT))  # SL 上限 10%
    limit_price = max(limit_price, hard_sl * 1.005)

    sl   = hard_sl
    tp1  = round(limit_price * (1 + TP1_PCT), 6)
    tp2  = round(limit_price * (1 + TP2_PCT), 6)
    # 用技术位修正 TP（不超过历史应压位）
    if h8  * 0.998 > tp1: tp1 = round(min(h8  * 0.998, limit_price * (1 + TP1_PCT * 1.5)), 6)
    if h24 * 0.998 > tp2: tp2 = round(min(h24 * 0.998, limit_price * (1 + TP2_PCT * 1.5)), 6)

    return {
        'score':       score,
        'direction':   'LONG',
        'limit_price': limit_price,
        'cur_price':   round(price, 6),
        'sl':          sl,
        'tp1':         tp1,
        'tp2':         tp2,
        'atr':         round(atr1h, 6),
        'reasons':     ' | '.join(reasons),
    }

# ── baw 工具函数 ─────────────────────────────────────────────────────────────
def _baw_parse(r) -> dict:
    out = (r.stdout or '').strip()
    err = (r.stderr or '').strip()
    if not out:
        log.warning(f"baw 空输出 stderr={err[:200]}")
        return {}
    try:
        return json.loads(out)
    except json.JSONDecodeError as e:
        log.warning(f"baw JSON解析失败: {e}  out={out[:200]}")
        return {}

def baw_quote(from_qty: float, to_addr: str) -> dict:
    """解析 quote 文字输出，返回 {toCoinAmount, fromCoinAmount}"""
    r = subprocess.run([
        'baw', 'market-order', 'quote',
        '--binanceChainId', CHAIN_ID,
        '--fromTokenQty', str(from_qty),
        '--fromToken', USDT_ADDR,
        '--toToken', to_addr,
    ], capture_output=True, text=True, timeout=20)
    out = (r.stdout or '') + (r.stderr or '')
    to_amt = 0.0
    for line in out.splitlines():
        if line.strip().startswith('To:'):
            parts = line.split()
            if len(parts) >= 2:
                try: to_amt = float(parts[1])
                except ValueError: pass
            break
    return {'toCoinAmount': str(to_amt), 'fromCoinAmount': str(from_qty)}

def baw_limit_buy(to_addr: str, usdt_qty: float, trigger_price: float) -> dict:
    """挂限价买单，返回 {success, data:{strategyId, orderId}}"""
    r = subprocess.run([
        'baw', 'limit-order', 'buy',
        '--binanceChainId', CHAIN_ID,
        '--triggerPrice',   f'{trigger_price:.6f}',
        '--fromTokenQty',   str(usdt_qty),
        '--fromToken',      USDT_ADDR,
        '--toToken',        to_addr,
        '--slippage',       '1',
        '--json',
    ], capture_output=True, text=True, timeout=30)
    return _baw_parse(r)

def baw_limit_cancel(strategy_id) -> dict:
    """取消限价单"""
    r = subprocess.run([
        'baw', 'limit-order', 'cancel',
        '--strategyId', str(strategy_id),
        '--json',
    ], capture_output=True, text=True, timeout=20)
    return _baw_parse(r)

def baw_limit_status(strategy_id) -> dict:
    """查询单个限价单状态"""
    r = subprocess.run([
        'baw', 'limit-order', 'list',
        '--strategyId', str(strategy_id),
        '--json',
    ], capture_output=True, text=True, timeout=20)
    d = _baw_parse(r)
    lst = d.get('data', {}).get('list', [])
    return lst[0] if lst else {}

def _baw_sell_ok(result: dict) -> bool:
    """
    判断 baw sell 是否成功。
    成功条件：
      - success=True
      - 或 data.orderId 存在
      - 或 error.data.orderId 存在（流动性不足但订单已提交）
    """
    if result.get('success'):
        return True
    if result.get('data', {}).get('orderId'):
        return True
    # baw 返回 success=false 但订单已提交的情况
    if result.get('error', {}).get('data', {}).get('orderId'):
        return True
    return False

def _baw_liquidity_error(result: dict) -> bool:
    """30003001 = 流动性不足，需要稍后重试"""
    return result.get('error', {}).get('code') == 30003001

def baw_sell(from_addr: str, token_qty: float, slippage: int = 1,
             max_retries: int = 3, retry_delay: float = 8.0) -> dict:
    """
    市价卖出（止盈止损用）。
    - 卖出前自动用钱包实际余额校正 qty，避免精度差导致 balance insufficient
    - 自动重试 3 次，每次间隔 8s
    - 流动性不足时递增滑点到 3%
    """
    # ★ 卖出前用钱包实际余额校正（避免浮点精度差导致 balance insufficient）
    try:
        actual_qty = get_token_balance(from_addr)
        if actual_qty > 0:
            diff_pct = abs(actual_qty - token_qty) / token_qty * 100
            if diff_pct <= 5:        # 误差 5% 以内直接用实际余额
                if diff_pct > 0.01:  # 确实有差异才记录
                    log.info(f"[baw_sell] qty校正: {token_qty:.6f} → {actual_qty:.6f} "
                             f"(差{diff_pct:.3f}%)")
                token_qty = actual_qty
            elif diff_pct > 5:
                log.warning(f"[baw_sell] 钱包余额{actual_qty:.6f} 与状态qty{token_qty:.6f} "
                            f"差异{diff_pct:.1f}%过大，不自动替换")
    except Exception as e:
        log.warning(f"[baw_sell] 余额校正失败: {e}，使用原 qty={token_qty:.6f}")

    last_result = {}
    for attempt in range(1, max_retries + 1):
        cur_slippage = min(slippage + (attempt - 1), 3)
        r = subprocess.run([
            'baw', 'market-order', 'swap',
            '--binanceChainId', CHAIN_ID,
            '--fromTokenQty',   str(token_qty),
            '--fromToken',      from_addr,
            '--toToken',        USDT_ADDR,
            '--slippage',       str(cur_slippage),
            '--json',
        ], capture_output=True, text=True, timeout=60)
        last_result = _baw_parse(r)
        if _baw_sell_ok(last_result):
            if attempt > 1:
                log.info(f"[baw_sell] 第{attempt}次重试成功 slippage={cur_slippage}%")
            return last_result
        err_code = last_result.get('error', {}).get('code')
        log.warning(f"[baw_sell] 尝试{attempt}/{max_retries} 失败 code={err_code}: "
                    f"{last_result.get('error',{}).get('message','')[:80]}")
        if attempt < max_retries:
            time.sleep(retry_delay)
    return last_result

def _parse_balance_assets(stdout: str) -> list:
    d = json.loads(stdout)
    if isinstance(d, list): return d
    data = d.get('data', [])
    return data if isinstance(data, list) else data.get('assets', [])

def get_wallet_usdt() -> float:
    r = subprocess.run(['baw', 'wallet', 'balance', '--json'],
                       capture_output=True, text=True, timeout=15)
    try:
        assets = _parse_balance_assets(r.stdout)
        for a in assets:
            if a.get('symbol') == 'USDT' and \
               a.get('binanceChainId', a.get('chainId', '')) == '56':
                return float(a.get('balance', 0))
    except Exception:
        pass
    return 0.0

# ── 合约价格缓存（REST 批量轮询，30s 一次）────────────────────────────────────
_price_poll_interval = 30   # 秒

def _price_poll_thread():
    """
    每 30s 用合约 REST 批量拉全量 ticker 价格，更新 _ws_prices 缓存。
    单次 API 调用 ~1s，覆盖 700+ 合约，延迟远低于原来的 30min 轮询。
    """
    log.info("[价格轮询] 启动合约价格轮询线程（每30s刷新）")
    while True:
        try:
            tickers = api.request('GET', api.BASE_FAPI + '/fapi/v1/ticker/price', {})
            if isinstance(tickers, list):
                with _ws_lock:
                    for t in tickers:
                        sym = t.get('symbol', '')
                        p   = float(t.get('price', 0))
                        if sym and p > 0:
                            _ws_prices[sym] = p
                log.debug(f"[价格轮询] 更新 {len(tickers)} 个合约价格")
        except Exception as e:
            log.warning(f"[价格轮询] 失败: {e}")
        time.sleep(_price_poll_interval)

def get_ws_price(fut_sym: str) -> float:
    """获取最新合约价格（来自 REST 轮询缓存）"""
    with _ws_lock:
        return _ws_prices.get(fut_sym, 0.0)

def ensure_ws(fut_symbols: list[str]):
    """确保价格轮询线程在跑"""
    global _ws_thread
    if _ws_thread and _ws_thread.is_alive():
        return
    log.info("[价格轮询] 启动价格监控线程...")
    _ws_thread = threading.Thread(target=_price_poll_thread, daemon=True)
    _ws_thread.start()
    time.sleep(2)  # 等第一次拉取完成

# ── 并发扫描 ─────────────────────────────────────────────────────────────────
def _scan_one(item: dict, positions: dict) -> dict | None:
    """扫描单个 bStock，返回 sig 或 None"""
    sym    = item.get('symbol', '')
    cs     = item.get('cs', '')
    addr   = item.get('contractAddress', '')
    ticker = item.get('ticker', '')

    if not cs or not addr: return None
    if sym in positions:   return None

    fut_sym = ticker + 'USDT'
    try:
        k1h = api.get_klines(fut_sym, '1h', 52)
        k4h = api.get_klines(fut_sym, '4h', 22)
        if len(k1h) < 30 or len(k4h) < 10: return None

        t = api.get_ticker_24h(fut_sym)
        if isinstance(t, list): t = t[0]
        chg24 = float(t.get('priceChangePercent', 0))
        vol24 = float(t.get('quoteVolume', 0))

        if vol24 < 3_000_000: return None  # 过滤低流动性

        try:
            fr = api.request('GET', api.BASE_FAPI + '/fapi/v1/fundingRate',
                             {'symbol': fut_sym, 'limit': 1})
            funding = float(fr[0]['fundingRate']) * 100 if fr else 0
        except Exception:
            funding = 0.0

        sig = score_signal(sym, k1h, k4h, funding, chg24)
        if sig['score'] < MIN_SCORE or sig.get('direction') == 'REJECT': return None

        sig.update({'sym': sym, 'fut_sym': fut_sym, 'addr': addr,
                    'ticker': ticker, 'vol24': vol24, 'chg24': chg24,
                    'funding': funding})
        return sig

    except Exception:
        return None


# ── 合约先行指标增强（OI + 多空比 + 清算位）────────────────────────────────────
def _get_contract_signals(fut_sym: str) -> dict:
    """
    从币安合约拉取额外先行信号：
    - OI 变化率（1h）
    - 多空比（Long/Short Ratio）
    - 近期清算密集区（下方空单清算 = 支撑，上方多单清算 = 阻力）
    返回 dict，失败时返回空 dict
    """
    result = {}
    try:
        # OI 变化
        oi_hist = api.request('GET', api.BASE_FAPI + '/futures/data/openInterestHist',
                              {'symbol': fut_sym, 'period': '1h', 'limit': 3})
        if oi_hist and len(oi_hist) >= 2:
            oi_now  = float(oi_hist[-1]['sumOpenInterest'])
            oi_prev = float(oi_hist[-2]['sumOpenInterest'])
            result['oi_chg_pct'] = (oi_now - oi_prev) / oi_prev * 100 if oi_prev > 0 else 0
        else:
            result['oi_chg_pct'] = 0

        # 多空比（账户多空比）
        ls = api.request('GET', api.BASE_FAPI + '/futures/data/globalLongShortAccountRatio',
                         {'symbol': fut_sym, 'period': '1h', 'limit': 1})
        if ls:
            result['ls_ratio'] = float(ls[-1].get('longShortRatio', 1.0))
        else:
            result['ls_ratio'] = 1.0

        # 清算密集区：通过近期强平数据近似
        # 用近4根1h K线的低点作为下方支撑（空单清算区）
        k1h = api.get_klines(fut_sym, '1h', 8)
        if k1h:
            lows  = [float(k[3]) for k in k1h[-4:]]
            highs = [float(k[2]) for k in k1h[-4:]]
            price = float(k1h[-1][4])
            result['support_liq'] = min(lows)   # 下方空单清算密集区（支撑）
            result['resist_liq']  = max(highs)  # 上方多单清算密集区（阻力）
            result['price']       = price
        else:
            result['support_liq'] = 0
            result['resist_liq']  = 0
            result['price']       = 0

    except Exception as e:
        log.debug(f"[合约信号] {fut_sym} 失败: {e}")
    return result


def calc_entry_price(fut_sym: str, k1h: list) -> tuple:
    """
    计算入场价和止损价：
    - 入场价 = max(近4根1h低点支撑, 空单清算密集区下沿) × 1.001
    - SL = 支撑位 - 0.5 × ATR(1h)
    返回 (entry_price, sl_price)
    """
    cs = _get_contract_signals(fut_sym)
    lows  = [float(k[3]) for k in k1h[-4:]]
    support_1h = min(lows)
    support_liq = cs.get('support_liq', support_1h)

    # 取较高的支撑位（更保守，避免入场价太低等不到）
    support = max(support_1h, support_liq)
    entry   = round(support * 1.001, 6)

    atr1h  = atr(k1h)
    sl     = round(support - atr1h * 0.5, 6)

    return entry, sl, cs


def _enrich_signal(sig: dict) -> dict:
    """给 score_signal 结果追加合约先行指标分数"""
    fut_sym = sig.get('fut_sym', '')
    k1h     = api.get_klines(fut_sym, '1h', 8) if fut_sym else []
    if not k1h:
        return sig

    entry, sl, cs = calc_entry_price(fut_sym, k1h)
    oi_chg   = cs.get('oi_chg_pct', 0)
    ls_ratio = cs.get('ls_ratio', 1.0)

    bonus = 0
    reasons_extra = []

    # OI 上升 + 价格上涨 → 趋势健康
    if oi_chg > 1.0:
        bonus += 5
        reasons_extra.append(f"OI↑{oi_chg:.1f}%")
    elif oi_chg < -1.0:
        bonus -= 5
        reasons_extra.append(f"OI↓{oi_chg:.1f}%")

    # 多空比 > 1.1 → 多方明显占优
    if ls_ratio > 1.1:
        bonus += 5
        reasons_extra.append(f"多空比{ls_ratio:.2f}")
    elif ls_ratio < 0.9:
        bonus -= 5
        reasons_extra.append(f"空方占优{ls_ratio:.2f}")

    sig['score']      = sig.get('score', 50) + bonus
    sig['entry_price'] = entry
    sig['sl']          = sl
    sig['entry_from_contract'] = True
    if reasons_extra:
        sig['reasons'] = sig.get('reasons', '') + ' | ' + ' '.join(reasons_extra)
    return sig


# ── 价格监控入场（替代旧的挂单逻辑）────────────────────────────────────────────
_monitor_lock  = threading.Lock()
_active_monitors: dict = {}   # sym -> {sig, start_ts}
_entry_times_30min: list = [] # 记录30min内的开仓时间戳


def _can_open_new() -> bool:
    """30分钟内最多开3个新仓"""
    now = time.time()
    recent = [t for t in _entry_times_30min if now - t < 1800]
    _entry_times_30min.clear()
    _entry_times_30min.extend(recent)
    return len(recent) < MAX_NEW_30MIN


def _record_new_entry():
    _entry_times_30min.append(time.time())


def start_monitor(sig: dict, state: dict):
    """将标的加入价格监控队列（替代旧的 place_limit_order）"""
    sym = sig['sym']
    with _monitor_lock:
        if sym in _active_monitors:
            log.info(f"[监控] {sym} 已在监控中，跳过")
            return
        if len(_active_monitors) >= MAX_CONCURRENT_MONITORS:
            log.info(f"[监控] 已达上限({MAX_CONCURRENT_MONITORS})，跳过 {sym}")
            return
        _active_monitors[sym] = {
            'sig':      sig,
            'start_ts': time.time(),
        }
    log.info(f"[监控] ✅ 开始监控 {sym}  入场价={sig['entry_price']:.4g}  "
             f"SL={sig['sl']:.4g}  score={sig['score']}")
    notify(
        f"👁 开始监控 [{sym}]\n"
        f"等待价格回踩 ${sig['entry_price']:.4g} (+-0.5%)\n"
        f"TP1: +3%  TP2: +5%  SL: ${sig['sl']:.4g}\n"
        f"评分: {sig['score']}分 | {sig.get('reasons','')[:60]}"
    )


def _price_monitor_thread(state_ref: list):
    """
    独立线程：每30秒检查所有监控标的价格
    触发条件：合约现价进入 entry_price ± ENTRY_BAND_PCT
    超时2h：从监控队列移除，等待主循环重扫
    """
    log.info("[价格监控线程] 启动")
    while True:
        try:
            now = time.time()
            state = state_ref[0]
            to_remove = []

            with _monitor_lock:
                items = list(_active_monitors.items())

            for sym, mon in items:
                sig    = mon['sig']
                start  = mon['start_ts']
                entry  = sig.get('entry_price', 0)
                fut_sym = sig.get('fut_sym', '')

                # 超时检查
                if now - start > MONITOR_WINDOW_SEC:
                    log.info(f"[价格监控] {sym} 监控超时2h，移出队列（主循环将重扫）")
                    to_remove.append(sym)
                    continue

                # 当前价
                price = get_ws_price(fut_sym)
                if price <= 0:
                    continue

                # 已经持仓则移出
                if sym in state.get('positions', {}):
                    to_remove.append(sym)
                    continue

                # 价格进入入场区间
                band = entry * ENTRY_BAND_PCT
                if abs(price - entry) <= band:
                    if not _can_open_new():
                        log.info(f"[价格监控] {sym} 价格触达但30min内已开仓3笔，暂不入场")
                        continue

                    log.info(f"[价格监控] {sym} 价格 ${price:.4g} 触达入场区间 "
                             f"${entry:.4g}±{band:.4g}，市价买入")

                    # 更新 sig 里的价格信息，直接市价买入
                    sig['cur_price']   = price
                    sig['limit_price'] = entry  # 用于 gap 检查
                    ok = place_limit_order(sig, state)
                    if ok:
                        _record_new_entry()
                        to_remove.append(sym)
                        state_ref[0] = state

            with _monitor_lock:
                for sym in to_remove:
                    _active_monitors.pop(sym, None)

        except Exception as e:
            import traceback
            log.warning(f"[价格监控线程] 异常: {e}\n{traceback.format_exc()[:300]}")
        time.sleep(30)

def get_macro_filter() -> dict:
    """
    ✨ 宏观过滤层：从 Clawby 拉取 BTC ETF 资金流、巨鲸方向、山寨季指数、贪婪恐惧指数
    返回宏观坏境标志：{"block_entry": bool, "reason": str, "etf_flow_7d": float,
                         "whale_direction": str, "altcoin_idx": float, "fear_greed": float}
    block_entry=True 时主循环应暂停开新仓
    """
    if not _CLAWBY_OK:
        return {"block_entry": False, "reason": "clawby未加载"}

    result = {"block_entry": False, "reason": "",
              "etf_flow_7d": 0.0, "whale_direction": None,
              "altcoin_idx": 50.0, "fear_greed": 50.0}
    reasons = []

    # 1. BTC ETF 近7日净流入
    try:
        etf = get_btc_etf_flow(7)
        if etf:
            flow_7d = sum(float(e.get("flow_usd", 0)) for e in etf[-7:])
            result["etf_flow_7d"] = flow_7d
            if flow_7d < -500_000_000:  # 连续净流出 > 5亿美元 → 机构开始逃跟
                reasons.append(f"ETF连续流出{flow_7d/1e8:.1f}亿")
    except Exception:
        pass

    # 2. HL 巨鲸主导方向
    try:
        positions = get_hl_whale_positions(20)
        if positions:
            long_sz  = sum(float(p.get("position_size", 0))
                           for p in positions if str(p.get("side","")).lower() in ("long","buy"))
            short_sz = sum(float(p.get("position_size", 0))
                           for p in positions if str(p.get("side","")).lower() in ("short","sell"))
            total = long_sz + short_sz
            if total > 0:
                long_pct = long_sz / total
                if long_pct > 0.6:
                    result["whale_direction"] = "LONG"
                elif long_pct < 0.4:
                    result["whale_direction"] = "SHORT"
                    reasons.append(f"HL巨鲸升空占主({1-long_pct:.0%})")
    except Exception:
        pass

    # 3. 山寨季指数
    try:
        alt = get_altcoin_season(3)
        if alt:
            idx = float(alt[-1].get("altcoin_index", 50))
            result["altcoin_idx"] = idx
            # 山寨季指数 > 75 → 资金流入山寨，利好 bstocks
            if idx > 75:
                result["reason"] = f"山寨季开启({idx:.0f})，利好入场"
            elif idx < 25:
                reasons.append(f"山寨季指数低({idx:.0f})，BTC主导")
    except Exception:
        pass

    # 4. 贪婪恐惧指数
    try:
        fg = get_fear_greed(3)
        fg_list = fg.get("data_list", []) if fg else []
        if fg_list:
            latest_fg = float(fg_list[-1])
            result["fear_greed"] = latest_fg
            if latest_fg < 15:   # 极度恐惧 → 暂停开新山寨
                reasons.append(f"恐惧指数极低({latest_fg:.0f})，市场恐惧暂停")
    except Exception:
        pass

    if reasons:
        result["block_entry"] = True
        result["reason"] = result.get("reason", "") + " | ".join(reasons)

    return result


def scan_all_concurrent(bstock_items: list, state: dict) -> list:
    """并发扫描所有 bStock，返回按评分排序的候选列表"""
    positions = state.get('positions', {})
    # 已有挂单的也跳过
    pending = state.get('pending_orders', {})
    skip = set(positions.keys()) | set(pending.keys())

    # 同 symbol 冷却期：平仓后 4h 内不再开同一标的
    COOLDOWN_SEC = 4 * 3600
    now_ts = time.time()
    for h in state.get('history', []):
        sym_h = h.get('sym', '')
        closed_ts = h.get('_closed_ts', 0)
        if not closed_ts:
            # 兼容旧数据：用 closed_at 字符串估算
            try:
                import datetime
                ca = h.get('closed_at', '')
                closed_ts = datetime.datetime.strptime(ca, '%Y-%m-%d %H:%M UTC').replace(
                    tzinfo=datetime.timezone.utc).timestamp()
            except Exception:
                closed_ts = 0
        if sym_h and now_ts - closed_ts < COOLDOWN_SEC:
            skip.add(sym_h)

    items = [it for it in bstock_items
             if it.get('cs') and it.get('contractAddress')
             and it.get('symbol') not in skip]

    candidates = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_scan_one, it, positions): it for it in items}
        for f in as_completed(futures):
            sig = f.result()
            if sig:
                candidates.append(sig)

    candidates.sort(key=lambda x: -x['score'])
    return candidates

# ── 市价直接买入入场（限价单链上不支持，改为市价）────────────────────────────
def place_limit_order(sig: dict, state: dict) -> bool:
    """市价直接买入（baw limit-order 链上不支持，统一走 market-order swap）"""
    sym       = sig['sym']
    addr      = sig['addr']
    cur_price = sig['cur_price']
    usdt      = PER_TRADE_U

    # 价格偏离保护：限价参考价与当前价差 > 2% 说明已跑远，不追
    limit_ref = sig['limit_price']
    gap_pct   = abs(cur_price - limit_ref) / limit_ref * 100
    if gap_pct > 2.0:
        log.info(f"[跳过入场] {sym} 当前价${cur_price:.4g} 偏离参考价${limit_ref:.4g} "
                 f"达{gap_pct:.1f}%>2%，价格已跑远，放弃")
        return False

    log.info(f"[市价买入] {sym} 市价=${cur_price:.4g} (参考限价=${limit_ref:.4g} 偏离{gap_pct:.2f}%)")

    r = subprocess.run([
        'baw', 'market-order', 'swap',
        '--binanceChainId', CHAIN_ID,
        '--fromTokenQty',   str(usdt),
        '--fromToken',      USDT_ADDR,
        '--toToken',        addr,
        '--slippage',       '1',
        '--json',
    ], capture_output=True, text=True, timeout=30)
    result = _baw_parse(r)

    if not _baw_sell_ok(result):
        log.error(f"[买入失败] {sym}: {result}")
        return False

    data     = result.get('data', {})
    order_id = data.get('orderId', data.get('txHash', ''))

    # 以实际成交价为基础计算 TP/SL（用当前价近似）
    entry = cur_price
    tp1   = round(entry * (1 + TP1_PCT), 6)
    tp2   = round(entry * (1 + TP2_PCT), 6)
    sl_raw = round(entry * (1 - SL_HARD_PCT), 6)
    sl    = max(sl_raw, round(entry * (1 - SL_MAX_PCT), 6))  # SL上限 10%

    # 写入持仓（直接进持仓，跳过 pending 环节）
    token_qty = usdt / entry
    state['positions'][sym] = {
        'sym':        sym,
        'addr':       addr,
        'ticker':     sig['ticker'],
        'fut_sym':    sig['fut_sym'],
        'entry':      entry,
        'token_qty':  token_qty,
        'usdt_cost':  usdt,
        'tp1':        tp1,
        'tp2':        tp2,
        'sl':         sl,
        'tp1_hit':    False,
        'score':      sig['score'],
        'reasons':    sig['reasons'],
        'entered_at': now_str(),
        'enter_ts':   time.time(),
        'order_id':   order_id,
    }
    save_state(state)

    notify(f"✅ 市价买入 [{sym}] ({sig['ticker']})\n"
           f"成交价: ${entry:.4g}  仓位: {usdt}U\n"
           f"TP1: ${tp1:.4g} (+{TP1_PCT*100:.1f}%)\n"
           f"TP2: ${tp2:.4g} (+{TP2_PCT*100:.1f}%)\n"
           f"SL:  ${sl:.4g} (-{SL_HARD_PCT*100:.1f}%)\n"
           f"评分: {sig['score']}分\n"
           f"  {sig['reasons'][:80]}")
    return True

# ── 挂单状态追踪 ──────────────────────────────────────────────────────────────
def check_pending_orders(state: dict):
    """
    检查所有挂单：
    - TRIGGERED/FINISHED → 转为持仓
    - 超时 → 取消
    - CANCELED/EXPIRED/FAILED → 清除
    """
    pending = state.get('pending_orders', {})
    if not pending:
        return

    now_ts = time.time()
    for sym in list(pending.keys()):
        order = pending[sym]
        strategy_id = order.get('strategy_id')
        placed_ts   = order.get('placed_ts', 0)

        # 超时 → 取消
        if now_ts - placed_ts > LIMIT_TTL_SEC:
            log.info(f"[挂单超时] {sym} 取消限价单 strategyId={strategy_id}")
            if strategy_id:
                baw_limit_cancel(strategy_id)
            del state['pending_orders'][sym]
            save_state(state)
            notify(f"⏰ [{sym}] 限价单已超时取消\n"
                   f"挂单价 ${order['limit_price']:.4g} 未触发，2h已到期")
            continue

        if not strategy_id:
            continue

        # 查询状态
        try:
            status_data = baw_limit_status(strategy_id)
            status = status_data.get('status', '')
        except Exception as e:
            log.warning(f"[挂单查询失败] {sym}: {e}")
            continue

        if status in ('TRIGGERED', 'FINISHED'):
            # 成交！转为持仓
            actual_price = float(status_data.get('triggerPrice',
                                                  order['limit_price']))
            # 用挂单量估算获得 token 数
            to_amt_est = order['usdt_cost'] / actual_price

            state['positions'][sym] = {
                'sym':        sym,
                'addr':       order['addr'],
                'ticker':     order['ticker'],
                'fut_sym':    order['fut_sym'],
                'entry':      round(actual_price, 6),
                'qty':        round(to_amt_est, 6),
                'usdt_cost':  order['usdt_cost'],
                'tp1':        order['tp1'],
                'tp2':        order['tp2'],
                'sl':         order['sl'],
                'opened_at':  now_str(),
                'opened_ts':  now_ts,
                'half_sold':  False,
                'score':      order['score'],
                'reasons':    order['reasons'],
            }
            del state['pending_orders'][sym]
            save_state(state)

            notify(f"✅ 限价单成交 [{sym}] ({order['ticker']})\n"
                   f"成交价: ${actual_price:.4g}  数量≈{to_amt_est:.4f}\n"
                   f"TP1: ${order['tp1']:.4g}  TP2: ${order['tp2']:.4g}\n"
                   f"SL:  ${order['sl']:.4g}")
            log.info(f"[限价成交] {sym} @ ${actual_price:.4g}")

        elif status in ('CANCELED', 'EXPIRED', 'FAILED'):
            log.info(f"[挂单取消] {sym} status={status}")
            del state['pending_orders'][sym]
            save_state(state)

# ── WS 驱动的止盈止损 ────────────────────────────────────────────────────────
def _tighten_sl_after_close(state: dict) -> int:
    """
    美股收盘后自动收紧止损，每交易日最多执行一次。
    策略：
      - 有浮盈(>0.5%) 且 TP1 未触发 → 止损移到成本价+0.3%（保本止损）
      - TP1 已触发剩半仓     → 止损移到 TP1 出场价（锁住已赚的一半）
      - 浮盈小或浮亏           → 不动，保持原始止损
    返回收紧了多少笔。
    """
    positions  = state.get('positions', {})
    tightened  = 0
    msgs       = []

    # 批量获取当前价格
    try:
        all_tk = __import__('binance_api').request(
            'GET',
            __import__('binance_api').BASE_FAPI + '/fapi/v1/ticker/price', {})
        pm = {t['symbol']: float(t['price']) for t in all_tk}
    except Exception:
        pm = {}

    for sym in list(positions.keys()):
        pos       = positions[sym]
        fut       = pos.get('fut_sym', pos.get('ticker', '') + 'USDT')
        ep        = pos['entry']
        cur_sl    = pos['sl']
        half_sold = pos.get('half_sold', False)
        tp1_exit  = pos.get('tp1_exit_price', 0)
        cp        = pm.get(fut, 0)
        if cp <= 0 or ep <= 0:
            continue

        pnl_pct = (cp - ep) / ep * 100

        if half_sold and tp1_exit > 0:
            # TP1 已触发：止损移到 TP1 出场价下方 0.3%（不能低于原始止损）
            new_sl = round(tp1_exit * 0.997, 6)
            new_sl = max(new_sl, cur_sl)   # 只运居不降
            if new_sl > cur_sl:
                state['positions'][sym]['sl'] = new_sl
                tightened += 1
                msgs.append(f"{sym} 止损 {cur_sl:.4g}→{new_sl:.4g}"
                            f"（TP1后锁住，包保）")
        elif pnl_pct >= 0.5:
            # 有浮盈：止损移到成本价+0.3%以上（不能低于原始止损）
            new_sl = round(ep * 1.003, 6)
            new_sl = max(new_sl, cur_sl)
            if new_sl > cur_sl:
                state['positions'][sym]['sl'] = new_sl
                tightened += 1
                msgs.append(f"{sym} 止损 {cur_sl:.4g}→{new_sl:.4g}"
                            f"（浮盈+{pnl_pct:.1f}%，保本止损）")
        else:
            log.info(f"[close_tighten] {sym} 浮盈{pnl_pct:.1f}%小，不收紧止损")

    if tightened > 0:
        detail = '\n'.join(msgs)
        log.info(f"[close_tighten] 收盘止损收紧 {tightened} 笔\n{detail}")
        notify(f"🔒 美股收盘，止损已自动收紧\n{detail}")

    return tightened


def should_hold_or_sl(sym: str, fut_sym: str, entry: float, cur_price: float,
                       k1h: list) -> tuple[str, str]:
    """
    [v2.1 活动期间智能止损判断]
    触发硬止损位时，先分析当前是「震荡」还是「单边下跌」，再决定是否真止损。

    返回 (decision, reason)
      decision: 'STOP_LOSS' | 'HOLD'
      reason:   简要描述

    判断维度（每项 -1~+1 分）：
      1. 清算图平衡性  (上方多单清算 vs 下方空单清算)
      2. OI 变化方向    (下跌中 OI 增加=新空单入场=单边; OI 减少=多头止损=震荡)
      3. 资金费率偏向    (资费率值 > 0.02% =多头拥挤=单边风险)
      4. 价格位置 (RSI)   (RSI > 65=高位入场=单边风险; < 55=中位=震荡可能)

    综合分数 >= 2 点 → HOLD（震荡特征明显）
    综合分数 <= -1 点 → STOP_LOSS（单边特征明显）
    其余         → HOLD（不确定默认持有，活动期保护排名）
    """
    score = 0
    reasons = []

    try:
        # --- 维度 1: 清算图平衡性 ---
        liq_data = api.request('GET', api.BASE_FAPI + '/futures/data/takerlongshortRatio',
                               {'symbol': fut_sym, 'period': '1h', 'limit': 6})
        if liq_data:
            # 用近 6h 的多空比平均评估平衡性
            avg_ls = sum(float(d.get('buySellRatio', 1)) for d in liq_data) / len(liq_data)
            if 0.85 <= avg_ls <= 1.15:
                score += 1
                reasons.append(f'清算平衡(LS={avg_ls:.2f})')
            elif avg_ls < 0.7:
                score -= 1
                reasons.append(f'空头压倒多头(LS={avg_ls:.2f})')
    except Exception:
        pass

    try:
        # --- 维度 2: OI 变化方向 ---
        oi_hist = api.request('GET', api.BASE_FAPI + '/futures/data/openInterestHist',
                              {'symbol': fut_sym, 'period': '1h', 'limit': 4})
        if oi_hist and len(oi_hist) >= 3:
            oi_vals = [float(d['sumOpenInterest']) for d in oi_hist]
            oi_trend = (oi_vals[-1] - oi_vals[0]) / oi_vals[0] * 100  # 3h内变化%
            # 价格下跌 + OI 增加 = 新空单入场 → 单边下跌
            # 价格下跌 + OI 减少 = 多头止损出场 → 震荡可能
            if cur_price < entry and oi_trend > 1.0:
                score -= 1
                reasons.append(f'价跌+OI升(单边,+{oi_trend:.1f}%)')
            elif cur_price < entry and oi_trend < -0.5:
                score += 1
                reasons.append(f'价跌+OI降(震荡,{oi_trend:.1f}%)')
    except Exception:
        pass

    try:
        # --- 维度 3: 资金费率 ---
        fr_data = api.request('GET', api.BASE_FAPI + '/fapi/v1/fundingRate',
                              {'symbol': fut_sym, 'limit': 2})
        if fr_data:
            fr_latest = float(fr_data[-1].get('fundingRate', 0)) * 100
            if fr_latest > 0.02:
                score -= 1
                reasons.append(f'资费率偏高(+{fr_latest:.3f}%,多头拥挤)')
            elif -0.01 <= fr_latest <= 0.01:
                score += 1
                reasons.append(f'资费率中性({fr_latest:.3f}%,震荡信号)')
    except Exception:
        pass

    try:
        # --- 维度 4: 价格位置 (RSI 14) ---
        if k1h and len(k1h) >= 15:
            closes = [float(k[4]) for k in k1h[-15:]]
            gains  = [max(closes[i]-closes[i-1], 0) for i in range(1, len(closes))]
            losses = [max(closes[i-1]-closes[i], 0) for i in range(1, len(closes))]
            avg_g  = sum(gains)  / len(gains)  if gains  else 0
            avg_l  = sum(losses) / len(losses) if losses else 1
            rs     = avg_g / avg_l if avg_l > 0 else 99
            rsi    = 100 - 100 / (1 + rs)
            if rsi > 65:
                score -= 1
                reasons.append(f'RSI高位({rsi:.0f},入场过高)')
            elif rsi < 55:
                score += 1
                reasons.append(f'RSI中性({rsi:.0f},震荡区)')
    except Exception:
        pass

    log.info(f"[should_hold_or_sl] {sym} score={score}  {' | '.join(reasons) or '数据不足'}")

    if score >= 2:
        return 'HOLD', f'震荡特征明显(score={score}): {" | ".join(reasons)}'
    elif score <= -1:
        return 'STOP_LOSS', f'单边下跌特征(score={score}): {" | ".join(reasons)}'
    else:
        # 不确定默认持有（活动期排名保护）
        return 'HOLD', f'不确定默认持有(score={score}): {" | ".join(reasons) or "数据不足，谨慎不止损"}'


def _calc_hold_score(pos: dict, cur_price: float, k1h_closes: list, now_ts: float) -> tuple[int, list]:
    """
    计算仓位持续持有评分。
    分数越低说明走势越弱，触发提前出场。
    返回 (hold_score, reasons)
    """
    entry     = pos['entry']
    opened_ts = pos.get('opened_ts', pos.get('enter_ts', time.time() - 3600))
    hold_h    = (now_ts - opened_ts) / 3600
    pnl_pct   = (cur_price - entry) / entry * 100

    score   = 0
    reasons = []

    c = k1h_closes
    if len(c) >= 20:
        def ema_s(cc, n):
            if len(cc) < n: n = len(cc)
            k = 2/(n+1); e = cc[0]
            for x in cc[1:]: e = x*k+e*(1-k)
            return e
        e9  = ema_s(c[-9:],  9)
        e20 = ema_s(c[-20:], 20)
        if e9 < e20:
            score -= 2; reasons.append('EMA空排-2')

    # RSI
    if len(c) >= 15:
        g = [max(c[i]-c[i-1], 0) for i in range(1, len(c))]
        l = [max(c[i-1]-c[i], 0) for i in range(1, len(c))]
        ag = sum(g[-14:])/14; al = sum(l[-14:])/14
        r = 100 if al == 0 else 100 - 100/(1+ag/al)
        if r < 35:
            score -= 1; reasons.append(f'RSI弱({r:.0f})-1')

    # 连跌根数
    down = 0
    for i in range(len(c)-1, 0, -1):
        if c[i] < c[i-1]: down += 1
        else: break
    if down >= 3:
        score -= 2; reasons.append(f'连跌{down}根-2')

    # 入场后浮亏 > -1.5%
    if pnl_pct < -1.5:
        score -= 2; reasons.append(f'浮亏{pnl_pct:.1f}%-2')

    # 持仓超4h且亏损
    if hold_h > 4 and pnl_pct < 0:
        score -= 1; reasons.append(f'持{hold_h:.0f}h未盈利-1')

    return score, reasons


def monitor_positions_ws(state: dict):
    """
    用价格缓存检查所有持仓：
    优先级：TP1 > TP2 > 硬止损 > 动态弱势出场 > 时间衰减止损 > 时间止损
    """
    positions = state.get('positions', {})
    if not positions:
        return

    now_ts = time.time()

    # 批量拉 K 线（每30轮拉一次，约2.5min，避免频繁API）
    kline_cache = state.setdefault('_kline_cache', {})
    kline_ts    = state.setdefault('_kline_ts', 0)
    if now_ts - kline_ts > 150:   # 2.5分钟刷新一次
        for sym, pos in list(positions.items()):
            fut = pos.get('fut_sym', pos.get('ticker', '') + 'USDT')
            try:
                k = api.get_klines(fut, '1h', 24)
                kline_cache[sym] = [float(x[4]) for x in k]
            except Exception:
                pass
        state['_kline_ts'] = now_ts

    for sym in list(positions.keys()):
        pos = positions.get(sym)
        if not pos: continue

        fut_sym   = pos.get('fut_sym', pos.get('ticker', '') + 'USDT')
        entry     = pos['entry']
        tp1       = pos['tp1']
        tp2       = pos['tp2']
        sl        = pos['sl']
        qty       = pos.get('qty') or pos.get('token_qty', 0)
        cost      = pos['usdt_cost']
        half_sold = pos.get('half_sold', pos.get('tp1_hit', False))
        opened_ts = pos.get('opened_ts', pos.get('enter_ts', time.time() - 3600))
        addr      = pos['addr']
        hold_h    = (now_ts - opened_ts) / 3600

        cur_price = get_ws_price(fut_sym)
        if cur_price <= 0:
            continue

        pnl_pct = (cur_price - entry) / entry * 100
        pnl_u   = cost * pnl_pct / 100

        log.info(f"[持仓WS] {sym} entry={entry:.4g} now={cur_price:.4g} "
                 f"pnl={pnl_pct:+.2f}%({pnl_u:+.1f}U) hold={hold_h:.1f}h")

        # ══════════════════════════════════════════════════
        # 1. TP1：到达止盈一，卖出一半
        # ══════════════════════════════════════════════════
        if cur_price >= tp1 and not half_sold:
            last_sell_ts = state['positions'][sym].get('_last_sell_ts', 0)
            if time.time() - last_sell_ts < 60:
                log.debug(f"[TP1] {sym} 冷却中，跳过")
                continue
            state['positions'][sym]['_last_sell_ts'] = time.time()

            sell_qty = qty / 2
            log.info(f"[TP1] {sym} 触发 卖出一半 {sell_qty:.6f}")
            result = baw_sell(addr, sell_qty)
            if _baw_sell_ok(result):
                pnl_half = sell_qty * cur_price - cost / 2
                state['positions'][sym].update({
                    'half_sold': True,
                    'qty':       round(qty - sell_qty, 6),
                    'usdt_cost': cost / 2,
                    'tp1_exit_price': cur_price,
                })
                state['total_pnl'] = state.get('total_pnl', 0) + pnl_half
                save_state(state)
                notify(f"✅ [{sym}] TP1止盈 卖出一半\n"
                       f"卖出价: ${cur_price:.4g}  盈利: +{pnl_half:.2f}U\n"
                       f"剩余仓位等TP2: ${tp2:.4g}\n"
                       f"累计PnL: {state['total_pnl']:+.2f}U")
            else:
                err = result.get('error', {}).get('message', '')
                log.warning(f"[TP1] {sym} 卖出失败: {err[:60]}，60s后重试")
            continue

        # ══════════════════════════════════════════════════
        # 2. TP2：到达止盈二，全仓卖出
        # ══════════════════════════════════════════════════
        if cur_price >= tp2:
            # 冷却检查：同一标的 60s 内不重复尝试（嵌入到 positions 里）
            last_sell_ts = state['positions'][sym].get('_last_sell_ts', 0)
            if time.time() - last_sell_ts < 60:
                log.debug(f"[TP2] {sym} 冷却中，跳过")
                continue
            state['positions'][sym]['_last_sell_ts'] = time.time()

            cur_qty  = state['positions'][sym].get('qty') or state['positions'][sym].get('token_qty', 0)
            cur_cost = state['positions'][sym]['usdt_cost']
            log.info(f"[TP2] {sym} 触发 全仓卖出")
            result = baw_sell(addr, cur_qty)
            if _baw_sell_ok(result):
                pnl_final = cur_qty * cur_price - cur_cost
                state['total_pnl'] = state.get('total_pnl', 0) + pnl_final
                state['history'].append({
                    'sym': sym, 'entry': entry, 'exit': cur_price,
                    'pnl_u': pnl_final, 'reason': 'TP2',
                    'hold_h': round(hold_h, 1), 'closed_at': now_str(), '_closed_ts': time.time()
                })
                del state['positions'][sym]
                save_state(state)
                notify(f"🎉 [{sym}] TP2全部止盈\n"
                       f"入场: ${entry:.4g} → 出场: ${cur_price:.4g}\n"
                       f"总盈利: +{pnl_final:.2f}U\n"
                       f"累计PnL: {state['total_pnl']:+.2f}U")
            else:
                err = result.get('error', {}).get('message', '')
                log.warning(f"[TP2] {sym} 卖出失败: {err[:60]}，60s后重试")
            continue

        # ══════════════════════════════════════════════════
        # ══════════════════════════════════════════════════
        # 3. 硬止损（先调 should_hold_or_sl 判断震荡 vs 单边）
        # ══════════════════════════════════════════════════
        if cur_price <= sl:
            last_sell_ts = state['positions'][sym].get('_last_sell_ts', 0)
            if time.time() - last_sell_ts < 60:
                log.debug(f"[SL] {sym} 冷却中，跳过")
                continue
            state['positions'][sym]['_last_sell_ts'] = time.time()

            cur_qty  = state['positions'][sym].get('qty') or state['positions'][sym].get('token_qty', 0)
            cur_cost = state['positions'][sym]['usdt_cost']

            # [v2.1] 智能止损判断：震荡则持有，单边才真止损
            k1h_for_sl = kline_cache.get(sym, [])
            decision, reason = should_hold_or_sl(sym, fut_sym, entry, cur_price, k1h_for_sl)
            if decision == 'HOLD':
                notify(
                    f"⏸️ [{sym}] 触发止损位，但判断为震荡——将持有\n"
                    f"当前: ${cur_price:.4g}  止损位: ${sl:.4g}\n"
                    f"原因: {reason}\n"
                    f"亮色止损已放宽至入场价-8%"
                )
                # 宽松护盾止损：入场价 × (1 - 8%)
                fallback_sl = round(entry * 0.92, 6)
                if state['positions'][sym].get('sl', sl) > fallback_sl:
                    state['positions'][sym]['sl'] = fallback_sl
                    log.info(f"[SL-HOLD] {sym} 止损放宽到 {fallback_sl:.4g}")
                save_state(state)
                continue

            log.info(f"[SL] {sym} 硬止损触发（判断: {decision}  {reason}）")
            result = baw_sell(addr, cur_qty)
            if _baw_sell_ok(result):
                loss = cur_qty * cur_price - cur_cost
                state['total_pnl'] = state.get('total_pnl', 0) + loss
                state['history'].append({
                    'sym': sym, 'entry': entry, 'exit': cur_price,
                    'pnl_u': loss, 'reason': 'SL',
                    'hold_h': round(hold_h, 1), 'closed_at': now_str(), '_closed_ts': time.time()
                })
                del state['positions'][sym]
                save_state(state)
                notify(f"🔴 [{sym}] 硬止损触发（{reason}）\n"
                       f"入场: ${entry:.4g} → 出场: ${cur_price:.4g}\n"
                       f"亏损: {loss:.2f}U  累计PnL: {state['total_pnl']:+.2f}U")
            else:
                err = result.get('error', {}).get('message', '')
                log.warning(f"[SL] {sym} 止损卖出失败: {err[:60]}，60s后重试")
            continue

        # ══════════════════════════════════════════════════
        # 4. 机制一：动态弱势评分 → 提前出场
        # ══════════════════════════════════════════════════
        c1h = kline_cache.get(sym, [])
        hold_score, hs_reasons = _calc_hold_score(pos, cur_price, c1h, now_ts)

        if hold_score <= -6:
            # 趋势极差，无论盈亏直接出场
            cur_qty  = state['positions'][sym].get('qty') or state['positions'][sym].get('token_qty', 0)
            cur_cost = state['positions'][sym]['usdt_cost']
            loss_or_gain = cur_qty * cur_price - cur_cost
            log.info(f"[弱势出场] {sym} hold_score={hold_score} 强制出场: {', '.join(hs_reasons)}")
            result = baw_sell(addr, cur_qty)
            if _baw_sell_ok(result):
                state['total_pnl'] = state.get('total_pnl', 0) + loss_or_gain
                state['history'].append({
                    'sym': sym, 'entry': entry, 'exit': cur_price,
                    'pnl_u': loss_or_gain, 'reason': 'WEAK_EXIT',
                    'hold_h': round(hold_h, 1), 'closed_at': now_str(), '_closed_ts': time.time()
                })
                del state['positions'][sym]
                save_state(state)
                notify(f"⚠️ [{sym}] 弱势强制出场\n"
                       f"原因: {', '.join(hs_reasons)}\n"
                       f"入场: ${entry:.4g} → 出场: ${cur_price:.4g}\n"
                       f"盈亏: {loss_or_gain:+.2f}U  累计PnL: {state['total_pnl']:+.2f}U")
            continue

        if hold_score <= -4:
            # 趋势偏弱：有浮盈则提前锁定，微亏则提前出场减损
            cur_qty  = state['positions'][sym].get('qty') or state['positions'][sym].get('token_qty', 0)
            cur_cost = state['positions'][sym]['usdt_cost']
            if pnl_pct >= 0.3:   # 有浮盈 → 提前止盈
                log.info(f"[弱势止盈] {sym} hold_score={hold_score} 有盈利提前锁定")
                result = baw_sell(addr, cur_qty)
                reason_tag = 'WEAK_TP'
            elif pnl_pct >= -1.0:  # 微亏 → 提前减损出场
                log.info(f"[弱势减损] {sym} hold_score={hold_score} 微亏提前出场")
                result = baw_sell(addr, cur_qty)
                reason_tag = 'WEAK_CUT'
            else:
                result = {}  # 亏损 > 1% 且 score=-4 暂不出场，等硬止损
                reason_tag = ''
                log.info(f"[弱势观察] {sym} hold_score={hold_score} 亏损{pnl_pct:.1f}%，等硬止损")

            if reason_tag and (_baw_sell_ok(result)):
                pnl_final = cur_qty * cur_price - cur_cost
                state['total_pnl'] = state.get('total_pnl', 0) + pnl_final
                state['history'].append({
                    'sym': sym, 'entry': entry, 'exit': cur_price,
                    'pnl_u': pnl_final, 'reason': reason_tag,
                    'hold_h': round(hold_h, 1), 'closed_at': now_str(), '_closed_ts': time.time()
                })
                del state['positions'][sym]
                save_state(state)
                tag_cn = '弱势提前止盈' if reason_tag == 'WEAK_TP' else '弱势提前减损'
                notify(f"🟡 [{sym}] {tag_cn}\n"
                       f"原因: {', '.join(hs_reasons)}\n"
                       f"入场: ${entry:.4g} → 出场: ${cur_price:.4g}\n"
                       f"盈亏: {pnl_final:+.2f}U  累计PnL: {state['total_pnl']:+.2f}U")
            continue

        # ══════════════════════════════════════════════════
        # 5. 机制二：TP1后追踪止损（保护TP1以上的盈利）
        # ══════════════════════════════════════════════════
        if half_sold:
            tp1_exit = pos.get('tp1_exit_price', tp1)
            # 从TP1出场价涨了多少
            gain_above_tp1 = (tp1_exit - entry) / entry * 100
            # 如果价格从TP1出场价回落超过50%的涨幅 → 追踪止损出场
            trailing_sl = tp1_exit * (1 - gain_above_tp1 / 100 * 0.5)
            if cur_price <= trailing_sl and cur_price < tp1_exit:
                cur_qty  = state['positions'][sym].get('qty') or state['positions'][sym].get('token_qty', 0)
                cur_cost = state['positions'][sym]['usdt_cost']
                log.info(f"[追踪止损] {sym} 价格回落超TP1涨幅50%，出场")
                result = baw_sell(addr, cur_qty)
                if _baw_sell_ok(result):
                    pnl_final = cur_qty * cur_price - cur_cost
                    state['total_pnl'] = state.get('total_pnl', 0) + pnl_final
                    state['history'].append({
                        'sym': sym, 'entry': entry, 'exit': cur_price,
                        'pnl_u': pnl_final, 'reason': 'TRAILING_SL',
                        'hold_h': round(hold_h, 1), 'closed_at': now_str(), '_closed_ts': time.time()
                    })
                    del state['positions'][sym]
                    save_state(state)
                    notify(f"🔒 [{sym}] 追踪止损（保护TP1盈利）\n"
                           f"TP1出场价: ${tp1_exit:.4g} → 现价: ${cur_price:.4g}\n"
                           f"剩余仓位盈亏: {pnl_final:+.2f}U  累计PnL: {state['total_pnl']:+.2f}U")
                continue

        # ══════════════════════════════════════════════════
        # 6. 时间止损：美股休盘 + 亏损
        # ══════════════════════════════════════════════════
        # [v2.1 24h模式] 休市期间不强制时间止损，由 should_hold_or_sl 判断
        # 只有严重单边下跌才止损，否则持有到价格恢复
        if not us_market_open() and pnl_pct < -3 and hold_h > 4:
            log.info(f"[v2.1休市] {sym} 休市中亏损{pnl_pct:.1f}%，调用should_hold_or_sl判断")
            k1h_data = kline_cache.get(sym, [])
            fut_sym  = pos.get('fut_sym', pos.get('ticker','') + 'USDT')
            decision, reason = should_hold_or_sl(sym, fut_sym, entry, cur_price, k1h_data)
            if decision == 'STOP_LOSS' and pnl_pct < -5:
                cur_qty = state['positions'][sym].get('qty') or state['positions'][sym].get('token_qty', 0)
                sell_qty = cur_qty / 2
                log.info(f"[v2.1休市止损] {sym} 单边下跌({reason})，平一半")
                result = baw_sell(addr, sell_qty)
                if _baw_sell_ok(result):
                    loss_half = sell_qty * cur_price - cost / 2
                    state['positions'][sym]['qty']       = round(cur_qty - sell_qty, 6)
                    state['positions'][sym]['usdt_cost'] = cost / 2
                    state['positions'][sym]['half_sold'] = True
                    state['total_pnl'] = state.get('total_pnl', 0) + loss_half
                    save_state(state)
                    notify(f"⏰ [{sym}] 休市单边止损（{reason}）\n已平仓一半，亏损: {loss_half:.2f}U")
            else:
                log.info(f"[v2.1休市持有] {sym} {reason}，继续持有")

# ── 高频监控线程（每5秒检查一次止盈止损）────────────────────────────────────
def _position_monitor_thread(state_ref: list):
    """
    独立线程，每5秒从 WS 价格缓存检查持仓。
    state_ref 是 [state_dict] 的包装，用于传引用。
    """
    log.info("[监控线程] 持仓监控启动，每5s检查一次")
    while True:
        try:
            time.sleep(5)
            state = state_ref[0]
            if state.get('positions'):
                monitor_positions_ws(state)
        except Exception as e:
            import traceback
            log.warning(f"[监控线程] 异常: {e}\n{traceback.format_exc()[:400]}")

# ── 主循环 ───────────────────────────────────────────────────────────────────
def main():
    log.info("=" * 60)
    log.info("bStock 工作流 v2.0 启动（限价入场 + WS实时止盈）")
    log.info("=" * 60)

    raw = json.loads(BSTOCK_LIST.read_text())
    bstock_items = raw if isinstance(raw, list) else raw.get('data', [])
    log.info(f"加载 {len(bstock_items)} 个 bStock")

    state = load_state()

    # 收集所有需要监控的合约 symbol
    all_fut_syms = list({
        item['ticker'] + 'USDT'
        for item in bstock_items
        if item.get('ticker') and item.get('cs')
    })
    log.info(f"WS 监控 {len(all_fut_syms)} 个合约价格")

    # 启动 WS 价格流
    ensure_ws(all_fut_syms)

    # 启动持仓监控线程（高频，每5秒检查止盈止损）
    state_ref = [state]
    monitor_thread = threading.Thread(
        target=_position_monitor_thread, args=(state_ref,), daemon=True
    )
    monitor_thread.start()

    # 启动价格监控线程（新版：等价格回踩入场区间再买入）
    price_monitor_thread = threading.Thread(
        target=_price_monitor_thread, args=(state_ref,), daemon=True
    )
    price_monitor_thread.start()

    while True:
        scan_start = time.time()
        state['scan_count'] = state.get('scan_count', 0) + 1

        n_pos     = len(state.get('positions', {}))
        n_pending = len(state.get('pending_orders', {}))
        total_pnl = state.get('total_pnl', 0)

        log.info(f"\n{'='*50}")
        log.info(f"第 {state['scan_count']} 轮 | {now_str()} | "
                 f"美股{'开盘' if us_market_open() else '休盘'} | "
                 f"持仓:{n_pos} 挂单:{n_pending} PnL:{total_pnl:+.2f}U")

        # WS 重连检查
        ensure_ws(all_fut_syms)

        # 1. 检查挂单是否成交 / 超时
        check_pending_orders(state)
        state_ref[0] = state  # 同步给监控线程

        # 2. 收盘后止损收紧（只在收盘后 30min 窗口内执行一次）
        if us_just_closed():
            tighten_done = state.get('_sl_tighten_done_date', '')
            today_str    = datetime.datetime.utcnow().strftime('%Y-%m-%d')
            if tighten_done != today_str:
                tightened = _tighten_sl_after_close(state)
                if tightened > 0:
                    state['_sl_tighten_done_date'] = today_str
                    save_state(state)
                    state_ref[0] = state

        # 3. 开仓限制：24小时不间断运行，不限制美股时段
        if True:  # 已移除美股时段限制，全天候扫描
            _ = True  # placeholder
        if True:
            # 3. 并发扫描新信号
            log.info("并发扫描 bStock 信号...")
            usdt_bal = get_wallet_usdt()
            log.info(f"钱包 USDT: {usdt_bal:.2f}U")

            # 实际可用 = 余额 - 已挂单占用（挂单会锁定 USDT）
            reserved = n_pending * PER_TRADE_U
            available = usdt_bal - reserved
            log.info(f"可用 USDT: {available:.2f}U (挂单占用: {reserved:.0f}U)")

            if available >= PER_TRADE_U:
                # ✨ 宏观过滤：BTC ETF 资金流 + HL 巨鲸方向 + 山寨季 + 恐惧指数
                macro = get_macro_filter()
                log.info(f"[宏观过滤] ETF7d={macro['etf_flow_7d']/1e6:.0f}M "
                         f"巨鲸={macro['whale_direction']} "
                         f"山寨季={macro['altcoin_idx']:.0f} "
                         f"恐惧={macro['fear_greed']:.0f} "
                         f"block={macro['block_entry']}")
                if macro['block_entry']:
                    log.info(f"[宏观过滤] 暂停开新仓: {macro['reason']}")
                    notify(f"⚠️ bStock 宏观过滤触发，暂停开新仓\n原因: {macro['reason']}")
                else:
                    # 新版流程：评分 → 取 Top7 → 合约信号增强 → 加入价格监控队列
                    candidates = scan_all_concurrent(bstock_items, state)
                    log.info(f"发现 {len(candidates)} 个候选（≥{MIN_SCORE}分）")

                # Top7（按分数排序，scan_all_concurrent 已排序）
                top7 = []
                for sig in candidates:
                    if sig['sym'] in state.get('positions', {}): continue
                    if sig['sym'] in state.get('pending_orders', {}): continue
                    top7.append(sig)
                    if len(top7) >= TOP_CANDIDATES:
                        break

                log.info(f"Top{len(top7)} 候选: {[s['sym'] for s in top7]}")

                for sig in top7:
                    # 跳过已在监控中的
                    with _monitor_lock:
                        already = sig['sym'] in _active_monitors
                    if already:
                        continue

                    # 合约信号增强（OI + 多空比 + 清算位 → 计算入场价）
                    try:
                        sig = _enrich_signal(sig)
                    except Exception as e:
                        log.warning(f"  {sig['sym']} 增强失败: {e}")
                        sig['entry_price'] = sig.get('limit_price', sig['cur_price'])
                        sig['sl'] = sig.get('sl', sig['cur_price'] * 0.97)

                    log.info(f"  → {sig['sym']} score={sig['score']} "
                             f"入场价=${sig.get('entry_price', 0):.4g} "
                             f"SL=${sig.get('sl', 0):.4g} "
                             f"{sig.get('reasons','')[:60]}")

                    # 加入价格监控队列（最多同时监控3个）
                    start_monitor(sig, state)
                    time.sleep(0.5)
            else:
                log.info(f"USDT 不足 (可用{available:.0f}U < {PER_TRADE_U}U)")

        # 4. 每3轮推送状态摘要
        if state['scan_count'] % 3 == 0:
            history      = state.get('history', [])
            # 胜率仅统计系统自动成交笔数（过滤 MANUAL_CLOSE）
            auto_trades  = [h for h in history if h.get('reason', '') != 'MANUAL_CLOSE']
            wins         = sum(1 for h in auto_trades if h.get('pnl_u', 0) > 0)
            total_closed = len(auto_trades)
            win_rate     = wins / total_closed * 100 if total_closed > 0 else 0
            pos_lines = []
            for sym, pos in state.get('positions', {}).items():
                fp = get_ws_price(pos.get('fut_sym', pos.get('ticker','')+'USDT'))
                if fp > 0:
                    pnl = (fp - pos['entry']) / pos['entry'] * 100
                    pos_lines.append(f"  {sym}: {pnl:+.2f}%")
            pos_summary = '\n'.join(pos_lines) if pos_lines else '  无持仓'
            notify(f"📊 bStock v2.0 状态 {now_str()}\n"
                   f"持仓: {len(state.get('positions',{}))}笔  挂单: {n_pending}笔\n"
                   f"已平仓: {total_closed}笔  胜率: {win_rate:.0f}%\n"
                   f"累计PnL: {total_pnl:+.2f}U\n"
                   f"持仓盈亏:\n{pos_summary}")

        # 5. 等待下轮
        elapsed = time.time() - scan_start
        wait    = max(0, SCAN_INTERVAL - elapsed)
        log.info(f"本轮耗时 {elapsed:.0f}s，等待 {wait:.0f}s 后下轮...")
        time.sleep(wait)

if __name__ == '__main__':
    main()
