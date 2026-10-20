#!/usr/bin/env python3
"""
calc_position_size_real.py — 动态仓位计算（真实账户用）

仓位规则：
  1. 基础仓位由信号类型 + 评分 + 市场环境决定
  2. 四层标的分类乘数（v3.0）：
       L1 主流高流动（BTC/ETH，ATR<4%，24h成交>5亿）：2.5x，硬顶2000U
       L2 主流中流动（SOL/BNB/股票ETF，ATR<6%，成交1-5亿）：1.8x，硬顶1200U
       L3 普通山寨（ATR 6-15%，成交>3000万）：1.0x基准，硬顶700U
       L4 低流动/高风险（低波动但成交<3000万，或ATR>15%）：0.6x，硬顶400U
  3. 净值通过 details["net_value"] 传入；不传时使用保守默认值 1000U
  4. 股票代币非交易时段额外降至 0.5x（美股 14:30-21:00 UTC 以外）

复盘还原：复盘模式固定100U，用 real_size/100 得到倍数乘以复盘盈亏
"""

import datetime as _dt

POS_MIN_U  = 125
LEVERAGE   = 5
# MAX_POSITIONS 和层级硬顶从 fx/config 读，无法导入时用默认安全値
_DEFAULT_MAX_POSITIONS = 5   # 默认最大仓位数（和 fx/config.MAX_POSITIONS_TOTAL_BULL 一致）

# —— 单仓上限公式（v3.1）——
# 单仓最大名义仓位 = net_value ÷ MAX_POSITIONS
# 语义：以 1/N 本金为保证金，充分利用 5倍杠杆，最大名义刚好等于本金
# 例：本金1000U / 5仓 = 1000U名义；本金3000U / 5仓 = 3000U名义；调整为10仓 = 1500U名义

# 四层硬顶（任何情况下单笔不超过这个前提）
_CAP_L1 = 2000
_CAP_L2 = 1200
_CAP_L3 = 700
_CAP_L4 = 400

# 美股交易时段（UTC）：14:30 ~ 21:00
_US_MARKET_OPEN  = (14, 30)
_US_MARKET_CLOSE = (21,  0)

# 股票代币关键词（symbol 包含这些字符串视为股票代币）
_STOCK_KEYWORDS = [
    "NVDA", "TSLA", "AAPL", "AMZN", "GOOGL", "META", "MSFT", "NFLX",
    "QQQ", "SPY", "SOXL", "SOXS", "TQQQ", "UPRO",
    "SAMSUNG", "SKHYNIX", "SKHY", "KORU", "EWY", "SNXX", "SNDKUSDT",
    "MSTR", "COIN", "HOOD", "RDDT",
]


def _is_stock_token(symbol: str) -> bool:
    """判断是否为股票代币"""
    s = symbol.upper()
    return any(kw in s for kw in _STOCK_KEYWORDS)


def _us_market_open() -> bool:
    """当前是否在美股交易时段（UTC 14:30~21:00）"""
    now = _dt.datetime.utcnow()
    t = (now.hour, now.minute)
    return _US_MARKET_OPEN <= t < _US_MARKET_CLOSE


def classify_tier(atr_14: float, price: float, vol24_usd: float, symbol: str = "") -> str:
    """
    四层标的分类：
      L1: ATR/price < 4% AND vol24 > 5亿U  （BTC/ETH 级别）
      L2: ATR/price < 6% AND vol24 1-5亿U  （SOL/BNB/股票ETF 级别）
      L3: ATR/price 6-15% AND vol24 > 3000万U （普通山寨）
      L4: 其余（低流动低波动 or 高波动小盘）
    """
    if price <= 0:
        return "L4"
    atr_pct = atr_14 / price * 100
    v = vol24_usd

    if atr_pct < 4 and v > 500_000_000:
        return "L1"
    elif atr_pct < 6 and v > 100_000_000:
        return "L2"
    elif 6 <= atr_pct <= 15 and v > 30_000_000:
        return "L3"
    else:
        return "L4"


def _tier_multiplier_and_cap(tier: str) -> tuple[float, float]:
    """返回 (仓位乘数, 硬顶U)"""
    return {
        "L1": (2.5, _CAP_L1),
        "L2": (1.8, _CAP_L2),
        "L3": (1.0, _CAP_L3),
        "L4": (0.6, _CAP_L4),
    }.get(tier, (1.0, _CAP_L3))


def _dynamic_cap(net_value: float, hard_cap: float, max_positions: int = None) -> float:
    """
    动态单仓上限 v3.2：
      1. 单仓最大名义 = net_value / max_positions  (每仓占用 1/N 本金为保证金)
      2. 比例兆底：单仓保证金不超过本金的 MAX_MARGIN_PCT_PER_POS，防止极小账户L4占比异常
      3. 再取 min(该値, 层级硬顶)
    """
    MAX_MARGIN_PCT_PER_POS = 0.30   # 单仓保证金最多占本金30%

    if max_positions is None:
        try:
            import sys as _s, pathlib as _p
            _s.path.insert(0, str(_p.Path(__file__).parent / "fx"))
            from config import MAX_POSITIONS_TOTAL_BULL as _n
            max_positions = _n
        except Exception:
            max_positions = _DEFAULT_MAX_POSITIONS

    per_slot  = net_value / max_positions          # 每仓应占保证金
    notional  = per_slot * LEVERAGE               # 每仓最大名义

    # 比例兆底：保证金占本金比不超过 MAX_MARGIN_PCT_PER_POS
    pct_cap   = net_value * MAX_MARGIN_PCT_PER_POS * LEVERAGE

    return min(notional, hard_cap, pct_cap)


def real_size(sig_type: str, score: float, direction: str = "LONG", details: dict = None) -> float:
    """返回真实仓位（U），含四层标的分类乘数"""
    if details is None:
        details = {}

    scenario = details.get("scenario", 0)
    symbol   = details.get("symbol", "")

    # ── 基础仓位（按信号类型+评分）──
    if sig_type in ("reversal_l1", "reversal_short_l1"):
        base = 125.0
    elif sig_type in ("reversal", "reversal_short"):
        base = 250.0
    elif sig_type == "trend_short" or direction == "SHORT":
        if score >= 80:   base = 500.0
        elif score >= 75: base = 350.0
        else:             base = 250.0
    else:
        if score >= 90:   base = 700.0
        elif score >= 85: base = 600.0
        elif score >= 80: base = 500.0
        else:             base = 350.0  # 75~79分

    # ── 场景折半（清算区偏远场景）──
    if scenario == 4:
        base *= 0.5

    # ── 市场环境乘数 ──
    btc_trend = details.get("btc_trend", "")
    sentiment = details.get("sentiment", "")
    if btc_trend == "up" and sentiment in ("greedy", "extreme_greed"):
        base *= 1.2
    elif btc_trend == "down" or sentiment in ("fearful", "extreme_fear"):
        base *= 0.8

    # ── 四层标的分类乘数 ──
    atr_14    = float(details.get("atr_14", 0))
    price     = float(details.get("price", 0))
    vol24_usd = float(details.get("vol24_usd", 0))
    tier      = details.get("tier") or classify_tier(atr_14, price, vol24_usd, symbol)
    tier_mult, hard_cap = _tier_multiplier_and_cap(tier)
    base *= tier_mult

    # ── 股票代币：非美股交易时段再降 50% ──
    if _is_stock_token(symbol) and not _us_market_open():
        base *= 0.5

    # ── 动态上限：本金 ÷ 最大仓位数 × LEVERAGE，再取层级硬顶和动态上限中的小値 ──
    net_value    = float(details.get("net_value", 1000.0))
    max_pos      = details.get("max_positions")  # 外部可传入，否则自动读取配置
    dyn_cap      = _dynamic_cap(net_value, hard_cap, max_pos)

    # [v3.89 #81] 最小仓位与动态上限冲突（dyn_cap < POS_MIN_U）时，旧写法的外层 max 会静默突破动态上限。
    #   现在返回 0.0 表示「不可执行」并记录原因，不修改任何上限数值；dyn_cap >= POS_MIN_U 时结果与之前一致。
    if dyn_cap < POS_MIN_U:
        import logging as _lg
        _lg.getLogger("fx.calc_position_size").warning(
            f"[real_size] {symbol or sig_type} 动态上限 {dyn_cap:.1f}U < 最小仓位 {POS_MIN_U}U，不可执行，返回 0")
        return 0.0
    return round(max(POS_MIN_U, min(dyn_cap, base)), 1)


def multiplier(sig_type: str, score: float, direction: str = "LONG", details: dict = None) -> float:
    """返回复盘还原倍数（real_size / 100）"""
    return real_size(sig_type, score, direction, details) / 100.0


if __name__ == "__main__":
    print("\n四层分类仓位对照表（净值=1000U）")
    print(f"{'sig_type':<18} {'score':>5} {'tier':<4} {'base':>6} {'final':>7} {'cap':>6}")
    print("-" * 55)
    cases = [
        ("trend", 90, "LONG", {"tier": "L1", "net_value": 1000}),  # BTC
        ("trend", 90, "LONG", {"tier": "L2", "net_value": 1000}),  # SOL
        ("trend", 90, "LONG", {"tier": "L3", "net_value": 1000}),  # 山寨
        ("trend", 90, "LONG", {"tier": "L4", "net_value": 1000}),  # 低流动
        ("trend", 80, "LONG", {"tier": "L1", "net_value": 1000}),
        ("trend", 80, "LONG", {"tier": "L3", "net_value": 1000}),
        ("trend", 80, "LONG", {"tier": "L4", "net_value": 1000}),
        ("reversal", 75, "LONG", {"tier": "L3", "net_value": 1000}),
        ("trend_short", 82, "SHORT", {"tier": "L3", "net_value": 1000}),
    ]
    for st, sc, dr, d in cases:
        r = real_size(st, sc, dr, d)
        t = d["tier"]
        _, hc = _tier_multiplier_and_cap(t)
        print(f"{st:<18} {sc:>5} {t:<4} {sc:>6} {r:>7.0f}U {hc:>5.0f}U")

    print("\n股票代币时段测试：")
    for sym in ["NVDAUSDT", "SKHYNIXUSDT", "BTCUSDT"]:
        r_in  = real_size("trend", 85, "LONG", {"tier": "L2", "symbol": sym, "net_value": 1000})
        r_out = real_size("trend", 85, "LONG", {"tier": "L2", "symbol": sym, "net_value": 1000})
        stock = _is_stock_token(sym)
        mkt   = _us_market_open()
        print(f"  {sym:<16} stock={stock} mkt_open={mkt} 仓位={r_in:.0f}U")

    print("\n层级识别示例：")
    samples = [
        ("BTCUSDT",  500, 100000, 3_000_000_000),
        ("ETHUSDT",  300, 4000,   1_000_000_000),
        ("SOLUSDT",  30,  200,    500_000_000),
        ("SKHYUSDT", 5,   160,    15_000_000),
        ("PEPEUSDT", 0.005, 0.00002, 200_000_000),
        ("MEMEUSDT", 0.01, 0.1,   5_000_000),
    ]
    for sym, atr, price, vol in samples:
        t = classify_tier(atr, price, vol, sym)
        mult, cap = _tier_multiplier_and_cap(t)
        print(f"  {sym:<14} ATR/price={atr/price*100:.1f}% vol=${vol/1e6:.0f}M → {t} {mult}x 硬顶{cap}U")
