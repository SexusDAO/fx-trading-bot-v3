# ============================================================
# FOMO 打土狗工作流 — 配置模板
# 复制此文件为 config.py，填入你自己的参数后运行
# ============================================================

# ---------- API 凭证（必填） ----------
FOMO_API_KEY = "<在这里填入你的 Key>"   # fomoapi.io 的 Bearer Key
FOMO_WS_URL  = "wss://api.fomoapi.io/ws/alerts"
CHAIN_ID     = "4663"   # Robinhood chain EVM chain ID

# ---------- 仓位参数 ----------
MAX_POSITIONS   = 5        # 最大同时持仓数（仅针对 >= POSITION_LIMIT_THRESHOLD_USD 的仓位）
POSITION_LIMIT_THRESHOLD_USD = 50.0   # 小于此金额的仓位不受 MAX_POSITIONS 限制
SIZE_KOL        = 30.0     # 策略A KOL跟单 单笔 USDG
SIZE_MULTI      = 15.0     # 策略B 多人共买 单笔 USDG
MULTI_BUY_WINDOW_MIN = 5   # 多人共买检测窗口（分钟）
MULTI_BUY_THRESHOLD  = 5   # 窗口内买入人数触发阈值

# ---------- 止盈分层（按买入时市值） ----------
# 低位：买入市值 < 100K → 阶梯止盈，不设止损
LOW_MC_THRESHOLD = 100_000
LOW_MC_EXITS = [
    {"mc": 300_000,   "sell_pct": 0.25},   # 第1档 25%
    {"mc": 500_000,   "sell_pct": 0.25},   # 第2档 25%
    {"mc": 1_000_000, "sell_pct": 0.25},   # 第3档 25%
    {"mc": 5_000_000, "sell_pct": 1.0},    # 第4档 全清
]

# 中位：100K ≤ 买入市值 < 300K
MID_MC_THRESHOLD = 300_000
MID_MC_DOUBLE_SELL = 0.50   # 翻倍时卖出 50%（出本）
MID_MC_STOP_LOSS   = -0.50  # 止损 -50%

# 动态追踪止损（Trailing Stop）
TRAILING_ACTIVATE_PCT  = 0.30   # 盈利达到此值后激活追踪止损
TRAILING_DRAWDOWN_PCT  = 0.25   # 从峰值回撤此比例触发
EARLY_STOP_PCT         = -0.35  # 入场保护期内的硬止损线
EARLY_STOP_MINUTES     = 30     # 入场保护期（分钟）

# 高位：买入市值 ≥ 300K
HIGH_MC_DOUBLE_SELL  = 0.50
HIGH_MC_FINAL_MC     = 5_000_000
HIGH_MC_STOP_LOSS    = -0.50

# 超高位：买入市值 > 10M
VERY_HIGH_MC_THRESHOLD  = 10_000_000
VERY_HIGH_MC_STOP_LOSS  = -0.50

# ---------- KOL 卖出信号 ----------
KOL_SELL_STRICT_COUNT        = 2
KOL_SELL_WHALE_THRESHOLD_USD = 50_000

# ---------- 鲸鱼大额买入信号 ----------
WHALE_C_THRESHOLD_USD    = 20_000
WHALE_B_THRESHOLD_USD    = 50_000
WHALE_BUY_SIZE_USD       = 30.0
WHALE_CONFIRM_WINDOW_MIN = 10

# ---------- 共识模式 ----------
CONSENSUS_MIN_SIGNALS        = 2
CONSENSUS_WINDOW_MIN         = 60
CONSENSUS_MIN_PNL            = 0.0
CONSENSUS_TRAILING_DRAWDOWN  = 0.40
CONSENSUS_TRAILING_ACTIVATE  = 0.20
CONSENSUS_TIMEOUT_H          = 4
CONSENSUS_WHALE_USD          = 30_000
CONSENSUS_CANCEL_DOUBLE_OUT  = True

# ---------- 入场价格偏差保护 ----------
PRICE_DRIFT_MAX_PCT  = 0.10
PRICE_DRIFT_ENABLED  = True
QUOTE_DRIFT_MAX_PCT  = 0.12

# ---------- Token 黑名单 ----------
TOKEN_BLACKLIST: dict = {
    # "SYMBOL": "原因",
}

# ---------- 仓位叠加 ----------
ALLOW_POSITION_STACK  = True
MAX_POSITION_SIZE_USD = 45.0

# ---------- DCA 补仓 ----------
DCA_ENABLED       = True
DCA_DROP_PCT      = 0.20
DCA_SIZE_USD      = 15.0
DCA_MAX_TIMES     = 3
DCA_MAX_TOTAL_USD = 90.0

# ---------- Token 地址 ----------
USDG_ADDRESS     = "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"
ETH_ADDRESS      = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"
BUY_TOKEN        = ETH_ADDRESS
BUY_TOKEN_SYMBOL = "ETH"

# ---------- Gas 保留 ----------
GAS_RESERVE_USD = 50.0
GAS_WARN_USD    = 30.0

# ---------- 高市值趋势过滤 ----------
HIGH_MC_FILTER_THRESHOLD = 5_000_000
HIGH_MC_MIN_DRAWDOWN     = 0.30
HIGH_MC_TREND_RISE_6H    = 0.05

# ---------- 通知设置（Jarvis/Telegram） ----------
JARVIS_USER_ID   = "your_user_id"
JARVIS_THREAD_ID = "your_thread_id"
JARVIS_TARGET    = f"{JARVIS_USER_ID}:thread:{JARVIS_THREAD_ID}"

# ---------- 状态文件路径 ----------
STATE_FILE = "/path/to/fomo_workflow/state.json"
LOG_FILE   = "/path/to/fomo_workflow/trade.log"

# ---------- WebSocket RPC（信号源 A：零延迟新 Pair 监听）----------
# 申请免费 key：https://www.alchemy.com/ → 新建 App → Network: Robinhood Mainnet
# 填入后自动启用双轨信号源，留空则只用 fomoapi WS（原有行为不变）
WS_RPC_URL = ""   # 例: "wss://robinhood-mainnet.g.alchemy.com/v2/YOUR_KEY"
