# ============================================================
# bsc_monitor 配置 — KOL/聪明钱/FOMO 共买监控 + 自动买入
# ============================================================
import os

BSC_WS_RPC   = "wss://bsc.publicnode.com"
PROXY        = "socks5://REDACTED_PROXY_CREDENTIALS"

# ── PancakeSwap V2 factory（实测确认：PairCreated topic 尾号 d0e9）──
PCS_V2_FACTORY   = "0xcA143Ce32Fe78f1f7019d7d551a6402fC5350c73"
PAIR_CREATED_TOPIC = "0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9"
WBNB = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
USDT = "0x55d398326f99059ff775485246999027b3197955"

# ── 轮询节奏 ──
RPC_POLL_SEC          = 10      # PairCreated 扫描间隔（20s→10s，更快发现新对）
SMARTMONEY_POLL_SEC   = 30      # GMGN smartmoney/KOL 轮询（45s→30s）
FOMO_POLL_SEC         = 10      # FOMO REST 轮询（15s→10s，更快捕获买入信号）
STATUS_INTERVAL_SEC   = 3600    # 心跳状态推送

# ── 信号门槛（分龄策略：新币宽松，老币严格）──
# KOL/聪明钱单笔买入达到此金额 → 推送（USD）
SIGNAL_BUY_USD_MIN      = 2_000

# 新币门槛（< 4小时 + 首次出现）：早期信号最敏感
NEW_KOL_MIN_USD         = 200     # KOL 新币门槛
NEW_SMART_MIN_USD       = 500     # 聪明钱 新币门槛
NEW_MULTI_THRESHOLD     = 2       # 新币共买人数门槛
NEW_MULTI_MIN_TOTAL     = 500     # 新币共买最低总金额

# 早期币门槛（4-24小时）：过渡期适度放宽
EARLY_KOL_MIN_USD       = 1_000   # KOL 早期币门槛
EARLY_SMART_MIN_USD     = 1_500   # 聪明钱 早期币门槛
EARLY_MULTI_THRESHOLD   = 3       # 早期币共买人数门槛
EARLY_MULTI_MIN_TOTAL   = 2_000   # 早期币共买最低总金额

# 老币门槛（> 24小时）：保持严格，过滤噪声
KOL_MIN_USD             = 3_000   # KOL 老币门槛
SMART_MIN_USD           = 3_000   # 聪明钱 老币门槛
MULTI_BUY_THRESHOLD     = 3       # 老币共买人数门槛
MULTI_BUY_MIN_TOTAL     = 5_000   # 老币共买最低总金额

# 多人共买时间窗口
MULTI_BUY_WINDOW_MIN    = 10

# 大额鲸鱼买入（无标签地址）单笔达到此值 → 推送
WHALE_BUY_USD_MIN       = 20_000
# 币龄过滤：只关注 N 分钟内的新对（0 = 不过滤，主流台子加新池都要）
MAX_PAIR_AGE_MIN        = 0
# 市值过滤：MC 低于下限或超过上限的不推
MIN_MC_NOTIFY           = 50_000    # 市值 <$50K 不推（太小、流动性薄、容易归零）
MAX_MC_NOTIFY           = 50_000_000
# 币龄过滤：发币超过此时间的不再推（一周 = 10080 分钟；0 = 不过滤）
MAX_TOKEN_AGE_MIN       = 7 * 24 * 60
# 最低流动性：低于此值不推（全是垃圾）
MIN_LIQUIDITY_USD       = 3_000

# ── 数据质量门槛（分龄策略，过滤归零盘）──
# 新币（< 4小时）
NEW_MIN_LIQUIDITY_USD   = 1_500     # 流动性 ≥ $1,500
NEW_MIN_MC_USD          = 5_000     # 市值 ≥ $5,000
NEW_MIN_HOLDERS         = 30        # 持币人数 ≥ 30
NEW_MIN_TX_24H          = 50        # 24h交易数 ≥ 50tx

# 早期币（4-24小时）
EARLY_MIN_LIQUIDITY_USD = 2_500     # 流动性 ≥ $2,500
EARLY_MIN_MC_USD        = 15_000    # 市值 ≥ $15,000
EARLY_MIN_HOLDERS       = 80        # 持币人数 ≥ 80
EARLY_MIN_TX_24H        = 150       # 24h交易数 ≥ 150tx

# 老币（> 24小时）
OLD_MIN_LIQUIDITY_USD   = 3_000     # 流动性 ≥ $3,000
OLD_MIN_MC_USD          = 30_000    # 市值 ≥ $30,000
OLD_MIN_HOLDERS         = 150       # 持币人数 ≥ 150
OLD_MIN_TX_24H          = 300       # 24h交易数 ≥ 300tx

# ── Dev 地址黑名单（工厂币 creator）──
DEV_BLACKLIST = set()  # 运行时动态加载
# 涨跌幅过滤：5分钟涨幅超过此值不推（已经拉爆了，追高危险）
MAX_PRICE_CHANGE_5M     = 200     # 5分钟涨超200%不推（已经拉爆）
# 买卖比过滤：5分钟内卖出笔数 > 买入笔数 * 此倍数不推（抛压太重）
MAX_SELL_BUY_RATIO      = 3.0     # 卖出笔数是买入的3倍以上不推

# ── v8 新增过滤：发射台工厂币 / 死币 ──
SKIP_CREATOR_TOKENS     = 50      # creator 历史发币 >50 次 → 触发风险标注（强信号可豁免）
HARD_BLOCK_CREATOR_TOKENS = 200   # creator 历史发币 >200 次 → 硬拦截（不豁免）
MIN_HOLDERS_NOTIFY      = 200     # 持币地址 <200 → 触发风险标注

# ── 风险过滤门槛（不再硬拦截，触发后附风险标注；强信号豁免直接推送）──
SKIP_BUNDLER_RATE       = 50      # 捆绑买入占比 ≥50% → 触发风险标注
SKIP_BOT_RATE           = 70      # bot/degen 交易占比 ≥70% → 触发风险标注
SKIP_TOP10_RATE         = 90      # 前十持仓 ≥90%（且持有人≥30）→ 触发风险标注

# ── 强信号豁免门槛：触发风险过滤时，满足以下任一条件 → 仍推送（附风险标注）──
STRONG_KOL_COUNT        = 2       # 同一时间窗口内 ≥2 个 KOL/聪明钱买入同一币
STRONG_MULTI_COUNT      = 5       # 共买人数 ≥5 人
STRONG_USD              = 50_000  # 单笔金额 ≥$50K

# ── 主流台子（GMGN launchpad 标签）──
MAINSTREAM_LAUNCHPADS = ["fourmeme", "flap", "geniusfun", "pancakeswap"]
# DexScreener dexId 里的主流 DEX
MAINSTREAM_DEX = ["pancakeswap"]

# ── fomoapi（复用 fomo_workflow 的 key，BSC 实测可用）──
FOMO_API_KEY = ""   # 启动时从 fomo_workflow/config.py 读取
FOMO_WS_URL  = "wss://api.fomoapi.io/ws/alerts"

# ── 推送渠道 ──
DINGTALK_ACCESS_TOKEN = os.environ.get("BSC_MONITOR_CONFIG_DINGTALK_ACCESS_TOKEN", "")
DINGTALK_SECRET = os.environ.get("BSC_MONITOR_CONFIG_DINGTALK_SECRET", "")
DINGTALK_KEYWORD      = ""     # 加签模式无需关键词
NOTIFY_VIA_JARVIS     = False  # BSC 信号只推钉钉，不刷 Jarvis

# ── 自动买入配置 ──
AUTO_BUY_ENABLED    = True        # 是否启用自动买入
AUTO_BUY_AMOUNT_BNB = 0.0064      # 每次买入金额（BNB）
AUTO_BUY_AMOUNT_USD = 5           # 每次买入金额（USD，用于显示）
USER_WALLET_ADDRESS = '0xFb4fC5BCAb0D5d2e731a8FD94933b2894e113D37'  # 用户无私钥钱包
BNB_ADDRESS         = '0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE'   # BNB 原生代币地址

# ── 开发者发币数量硬拦截（与打新监控一致）──
HARD_BLOCK_DEV_TOKEN_COUNT = 500  # 开发者发币 > 500 个 → 直接拦截

# ── 状态文件 ──
STATE_DIR = os.path.dirname(os.path.abspath(__file__))
SEEN_PAIRS_FILE   = os.path.join(STATE_DIR, ".seen_pairs.json")
KOL_MAP_FILE      = os.path.join(STATE_DIR, "kol_watchlist.json")
AUTO_BUY_POSITIONS_FILE = os.path.join(STATE_DIR, "auto_buy_positions.json")  # 自动买入记录
