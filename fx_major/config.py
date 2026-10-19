"""
fx_major/config.py — 主流币工作流专用配置

设计原则：
  - 独立于 fx/ 山寨工作流，参数单独调优
  - 本金基准 1000U（模拟盘），仓位动态计算
  - 目标：单次盈利让本金增加 1%-5%
  - 多空都做，趋势/区间自动切换
"""

import os

# ══════════════════════════════════════════════════════════════════════════════
# 基础连接（复用 fx/ 的代理和 API Key）
# ══════════════════════════════════════════════════════════════════════════════
PROXY_URL          = "socks5://REDACTED_PROXY_CREDENTIALS"
PROXIES            = {"http": PROXY_URL, "https": PROXY_URL}
BINANCE_BASE_URL   = "https://fapi.binance.com"
BINANCE_API_KEY    = "E621Ob…DZhG"   # 仅模拟盘阶段不实际下单，key 占位
BINANCE_API_SECRET = "ACLFzj…TloX"

# ══════════════════════════════════════════════════════════════════════════════
# 候选池：市值前 100（每日通过 Binance 接口更新）
# ══════════════════════════════════════════════════════════════════════════════
# 初始白名单（接口失败时的 fallback，覆盖有合约的主流币）
UNIVERSE_WHITELIST = [
    "BTCUSDT",  "ETHUSDT",  "BNBUSDT",  "XRPUSDT",  "SOLUSDT",
    "ADAUSDT",  "DOGEUSDT", "TRXUSDT",  "AVAXUSDT", "LINKUSDT",
    "DOTUSDT",  "MATICUSDT","LTCUSDT",  "NEARUSDT", "ATOMUSDT",
    "UNIUSDT",  "XLMUSDT",  "ETCUSDT",  "FILUSDT",  "AAVEUSDT",
    "APTUSDT",  "ARBUSDT",  "OPUSDT",   "INJUSDT",  "SUIUSDT",
    "SEIUSDT",  "TIAUSDT",  "RUNEUSDT", "RENDERUSDT","FETUSDT",
]
UNIVERSE_TOP_N         = 100    # 取市值前 N
UNIVERSE_REFRESH_HOURS = 24     # 每 24 小时刷新一次排名
UNIVERSE_CACHE_FILE    = "logs/universe_cache.json"

# 最低要求（成交量过滤）
UNIVERSE_MIN_VOL_24H   = 50_000_000   # 最低 5000 万 U 日成交（主流币标准）

# ══════════════════════════════════════════════════════════════════════════════
# 行情模式识别参数
# ══════════════════════════════════════════════════════════════════════════════
# 趋势识别
TREND_CONFIRM_DAYS     = 2      # 连续 N 根日线同向 → 判断趋势
TREND_ATR_EXPAND_RATIO = 1.1    # ATR 相较5日均值扩张超过 10% → 趋势确认
TREND_EMA_FAST         = 9
TREND_EMA_MID          = 20
TREND_EMA_SLOW         = 50

# 区间识别
RANGE_ATR_SHRINK_RATIO = 0.95   # ATR 相较5日均值收缩 → 区间确认
RANGE_MAX_BODY_RATIO   = 0.5    # 日线实体/整体 < 50% → 区间震荡特征

# 区间分位阈值
RANGE_LONG_UPPER_PCT   = 0.30   # 在区间下 30% 分位内做多
RANGE_SHORT_LOWER_PCT  = 0.70   # 在区间上 70% 分位上做空
RANGE_MIN_SPREAD_PCT   = 0.015  # 区间宽度 / 价格最小 1.5%（否则太窄无利可图）
RANGE_TP_TARGET_PCT    = 0.60   # 止盈吃区间宽度的 60%

# ══════════════════════════════════════════════════════════════════════════════
# 信号触发条件
# ══════════════════════════════════════════════════════════════════════════════
# 共用
RSI_PERIOD        = 14
RSI_OVERSOLD      = 40     # 区间做多：RSI < 40
RSI_OVERBOUGHT    = 60     # 区间做空：RSI > 60
RSI_TREND_MID_LO  = 40     # 趋势回踩多：RSI 40-60
RSI_TREND_MID_HI  = 60
EMA_PULLBACK_ATR  = 0.5    # 回踩距离 EMA20 < ATR×0.5 才算有效回踩

# 趋势回踩深度限制
TREND_PULLBACK_MAX_BODY_RATIO = 0.50  # 回踩不超过前日线实体的 50%

# 成交量确认（趋势信号）
TREND_VOL_CONFIRM_MIN  = 0.7   # 入场K线量比 >= 0.7（不要求放量，但不能萎缩太厉害）

# 区间入场成交量（量缩确认）
RANGE_VOL_SHRINK_MAX   = 0.9   # 近3根量比均值 < 0.9 → 量缩回踩

# ══════════════════════════════════════════════════════════════════════════════
# 止盈止损参数
# ══════════════════════════════════════════════════════════════════════════════
# 区间模式
RANGE_SL_ATR_MULT      = 0.5   # 止损 = 区间边界外 ATR×0.5
RANGE_TP_RATIO         = 0.60  # 止盈 = 区间宽度 × 60%（不追到另一端）

# 趋势模式
TREND_TP1_ATR_MULT     = 2.0   # TP1（60%仓位）= 入场价 + ATR×2
TREND_TP2_ATR_MULT     = 4.0   # TP2（40%仓位）= 入场价 + ATR×4
TREND_SL_ATR_MULT      = 0.5   # 止损 = 回踩低点 - ATR×0.5
TREND_TP1_RATIO        = 0.60  # TP1 平仓比例
TREND_TP2_RATIO        = 0.40  # TP2 平仓比例

# 最大止损距离上限（避免止损太宽）
SL_MAX_PCT             = 0.06  # 止损距离不超过入场价 6%（主流币用，比山寨的 8% 窄）

# ══════════════════════════════════════════════════════════════════════════════
# 仓位计算参数
# ══════════════════════════════════════════════════════════════════════════════
PAPER_CAPITAL          = 1000.0   # 模拟本金（U）
LEVERAGE               = 10       # 默认杠杆

# 目标盈利区间（% 本金）：动态仓位的反推基础
TARGET_PROFIT_MIN_PCT  = 0.01   # 1%
TARGET_PROFIT_MAX_PCT  = 0.05   # 5%

# 仓位硬顶（按标的类型）
CAP_BTC_ETH            = 2500   # BTC/ETH 名义仓位上限
CAP_MAJOR              = 1500   # 其他主流币上限
CAP_DEFAULT            = 1000   # 默认上限

# 单次最大保证金占比（防止单仓吃掉太多本金）
MAX_MARGIN_PCT         = 0.25   # 单仓保证金 <= 本金 25%

# ══════════════════════════════════════════════════════════════════════════════
# 持仓槽位（独立于 fx/ 山寨工作流）
# ══════════════════════════════════════════════════════════════════════════════
MAX_POSITIONS_TOTAL    = 3
MAX_POSITIONS_LONG     = 2
MAX_POSITIONS_SHORT    = 2

# ══════════════════════════════════════════════════════════════════════════════
# 熔断 & 风控
# ══════════════════════════════════════════════════════════════════════════════
CIRCUIT_DAILY_LOSS_U   = 80    # 单日亏损超过 80U 触发熔断（8% 本金）
BTC_SHOCK_PCT          = 0.04  # BTC 1H 涨跌超过 4% 暂停开仓
COOLDOWN_HOURS_SL      = 4     # 止损后冷却 4 小时

# ══════════════════════════════════════════════════════════════════════════════
# 运行参数
# ══════════════════════════════════════════════════════════════════════════════
SCAN_INTERVAL_SEC      = 300   # 5 分钟扫描一次
KLINE_1H_LIMIT         = 100
KLINE_15M_LIMIT        = 100
KLINE_1D_LIMIT         = 30

# 挂单超时
ORDER_TIMEOUT_TREND_MIN  = 240   # 趋势信号挂单 4 小时
ORDER_TIMEOUT_RANGE_MIN  = 120   # 区间信号挂单 2 小时

# 时间止损（主流币波动节奏快，缩短）
TIME_STOP_HOURS          = 6
TIME_STOP_HARD_HOURS     = 10

# 文件路径
STATE_FILE             = "logs/major_paper_state.json"
TRADE_LOG_FILE         = "logs/major_paper_trades.jsonl"
SCAN_LOG_FILE          = "logs/major_scanner.log"
SYSTEM_LOG_FILE        = "logs/major_system.log"

# ══════════════════════════════════════════════════════════════════════════════
# Clawby / Coinglass 复用（从 fx/ 引用，不重复实现）
# ══════════════════════════════════════════════════════════════════════════════
# 清算图信号阈值
LIQ_ZONE_MIN_USD       = 500_000   # 清算区最低量（主流币比山寨币要求更高）
LIQ_ZONE_MAX_ATR_MULT  = 3.0       # 清算区距离 <= ATR×3（太远不算有效）
LIQ_BONUS_SCORE        = 15        # 有清算区支持时加分

# 恐惧贪婪指数
FEAR_GREED_EXTREME_FEAR  = 25      # 极度恐惧 → 做多信号加分
FEAR_GREED_EXTREME_GREED = 75      # 极度贪婪 → 做空信号加分
FEAR_GREED_BONUS_SCORE   = 10

# ══════════════════════════════════════════════════════════════════════════════
# Telegram 通知
# ══════════════════════════════════════════════════════════════════════════════
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
