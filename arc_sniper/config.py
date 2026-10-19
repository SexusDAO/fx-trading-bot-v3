"""
Arc Sniper 配置文件 — 主网 (9/16 上线)
======================================
已确认参数：
  - Chain ID: 5042
  - RPC: https://rpc.arc-scan.org
  - USDC: 原生代币, 18 decimals, 不需要合约地址
  - 钱包: 0x8A966Db06aD9624a0E41bb4adfD817c5e7e55F54
  - 余额: 687.825 USDC
  - 测试交易成功: 0x6e51b0e6...

DEX 信息:
  - Tolly DEX: 0x114d574247342eafea95b7a9234e62984d48cD9E (22k code, custom swap)
  - Uniswap V3 Factory: 0x1F98431c8aD98523631AE4a59f267346ea31F984 (标准地址, 有代码)
  - Uniswap V3 SwapRouter: 0xE592427A0AEce92De3Edee1F18E0157C05861564 (标准地址, 有代码)
  - Uniswap V4 PoolManager: 0x000000000004444c6dc89C062C1C4524364CEe13 (无代码, AKA 可能用自己的)
  - AKA Launchpad: @akadotfun — Arc 主网 Day1 发射台, 使用 Uniswap V4 hooks

生态信息 (Twitter 9/16):
  - 活跃代币: ARGUS ($34M mcap), TOLLY ($25M), LONG ($17M), COOL ($6M), ARCAT ($5M)
  - 6 个早期项目: aka.fun, UnitFlow, Synthra, Tower, Hibachi, Sidoor
  - GMGN 已追踪 Arc 链数据
"""

import os

# ═══════════════════════════════════════════════════════════════
# Arc 链参数 (主网已确认)
# ═══════════════════════════════════════════════════════════════
CHAIN_ID  = 5042
TESTNET_CHAIN_ID = 5042002
RPC_URL   = os.getenv("ARC_RPC_URL", "https://rpc.mainnet.arc.io")
TESTNET_RPC_URL = "https://rpc.testnet.arc.io"

# Arc 原生代币是 USDC，用于支付 Gas 和交易
USDC_ADDRESS   = ""  # 原生 USDC，不需要合约地址
NATIVE_TOKEN = os.environ.get("ARC_SNIPER_CONFIG_NATIVE_TOKEN", "")
USDC_DECIMALS  = 18  # Arc 原生 USDC 使用 18 位小数 (官方文档确认)
WUSDC_ADDRESS  = "0x3600000000000000000000000000000000000000"      # wrapped USDC (6 decimals, ERC-20 可选接口)
NATIVE_USDC_ADDR = "0xfffffff7fffffffffffffffffffffffffffffffE"  # native USDC (18 decimals, EIP-7708 system emitter)

# EURC (Circle 官方欧元稳定币, 6 decimals)
# 来自 docs.arc.io/arc/references/contract-addresses
EURC_ADDRESS = "0xbEf5f6d51CB62b58e6A8f77868681825C6fe21c1"

# System Emitter (EIP-7708) — 所有 USDC Transfer 事件都从这里发
# 监听这个地址可以获取所有 USDC 转账，不需要逐个查代币合约
SYSTEM_EMITTER = "0xffffFFFfFFffffffffffffffFfFFFfffFFFfFFfE"

# Uniswap V4 PoolManager (官方地址)
# 来自 docs.arc.io/arc/references/contract-addresses
UNISWAP_V4_POOLMANAGER = "0x000000000004444c6dc89C062C1C4524364CEe13"

# WebSocket endpoint (官方文档推荐用于事件订阅)
WS_URL = "wss://rpc.mainnet.arc.io"

# ═══════════════════════════════════════════════════════════════
# DEX 合约 — 已确认地址
# ═══════════════════════════════════════════════════════════════
# Tolly DEX — 自定义 swap 合约 (22k code, owner=0x03D3...)
TOLLY_TOKEN_ADDR = os.environ.get("ARC_SNIPER_CONFIG_TOLLY_TOKEN_ADDR", "")
TOLLY_DEX          = "0x114d574247342eafea95b7a9234e62984d48cD9E"
TOLLY_FACTORY      = ""  # 待确认: Tolly 工厂创建新池的合约
TOLLY_FACTORY_ABI  = []

# DAG Router (唯一验证可用的 swap 路径)
DAG_ROUTER = "0x4e3bcce28caf98a143fd8bd9e4875ccab3e7bbe0"
WUSDC_ADDRESS    = "0x3600000000000000000000000000000000000000"      # wrapped USDC (6 decimals)
NATIVE_USDC_ADDR = "0xfffffff7fffffffffffffffffffffffffffffffE"  # native USDC (18 decimals)

# Web3 Wallet Router (发现于 π 卖出 TX 0x65a3b4a9...)
# 360 bytes proxy, 方法 0x810c705b, 支持多跳路由 + V4 PoolManager
# 路径: token → V3 pool → 中间代币 → V3 pool → USDC
WEB3_WALLET_ROUTER = "0xb322222828d8c3e78ec32093801f0d3523600a65"
WEB3_WALLET_ROUTER_METHOD = "0x810c705b"
# π Token (已确认卖出成功)
PI_TOKEN_ADDR = os.environ.get("ARC_SNIPER_CONFIG_PI_TOKEN_ADDR", "")

# wUSDC (ERC20, 用于和池子交互)
WUSDC = WUSDC_ADDRESS

# WARP 发射台
WARP_TOKEN_ADDR = os.environ.get("ARC_SNIPER_CONFIG_WARP_TOKEN_ADDR", "")
WARP_FACTORY       = ""
WARP_FACTORY_ABI   = []

# Uniswap V3 (标准地址已部署代码)
# 注意: Arc 链的 V3 Factory 不是标准地址！
# 实际工厂: 0xf0db7b5837... (49k bytes, fee=200)
# 标准 Uniswap V3 Factory (0x1F98431c...) 在 Arc 上有代码但不创建池
UNISWAP_V3_FACTORY = "0xf0db7b58379503491d857db50ac9ece64c653918"
UNISWAP_V3_ROUTER  = "0xE592427A0AEce92De3Edee1F18E0157C05861564"
POOL_CREATED_TOPIC = "0x783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118"

# Wallet address
WALLET_ADDRESS = "0x8A966Db06aD9624a0E41bb4adfD817c5e7e55F54"

# ═══════════════════════════════════════════════════════════════
# 私钥 (从环境变量或 deploy_testnet.py fallback)
# ═══════════════════════════════════════════════════════════════
PRIVATE_KEY = os.getenv("ARC_PRIVATE_KEY", "")
# Fallback: 之前用户提供过的私钥 (9/12)
if not PRIVATE_KEY:
    PRIVATE_KEY = os.environ.get("ARC_SNIPER_CONFIG_PRIVATE_KEY", "")

# ═══════════════════════════════════════════════════════════════
# 买入参数
# ═══════════════════════════════════════════════════════════════
# 动态仓位 (position_manager 控制)
BUY_AMOUNT_USDC   = 5      # 基础买入 $5 (会根据余额动态调整)
MAX_BUY_AMOUNT     = 50     # 最大单笔买入 $50
MAX_SLIPPAGE_PCT  = 15     # 最大滑点 15%
GAS_LIMIT         = 500000

# 止盈规则
TP_TARGET_MCAP_SMALL = 1_000_000  # mcap < $1M 买入 → 目标 $1M 卖出
TP_PCT_LARGE         = 50         # mcap > $1M 买入 → 涨 50% 卖出

# 砸盘保护 (无止损，仅砸盘时市价清仓)
CRASH_PRICE_DROP_PCT  = 50   # 价格距买入 -50% → 砸盘
CRASH_LIQUIDITY_DROP_PCT = 60  # 流动性距买入 -60% → 砸盘

# 频率控制
MIN_BUY_INTERVAL_SEC = 30
MAX_BUYS_PER_HOUR    = 10

# ═══════════════════════════════════════════════════════════════
# 过滤门槛 — Arc 链适配 (2026-09-16 修订)
# ═══════════════════════════════════════════════════════════════
# Arc 链是新链，流动性/持有人/成交量普遍低于 Solana/Eth
# 阈值大幅降低，更重视信号源和合约安全
MIN_LIQUIDITY_USD = 1000      # 500→1000，必须有基本交易深度，低于此值滑点太大
MIN_HOLDERS       = 5         # 3→5，不是纯死池
MAX_TOP10_PCT     = 0.50      # 不变
MAX_TOKEN_AGE_SEC  = 3600     # 1800→3600，给更多观察时间
HONEYPOT_CHECK    = True

# 评分权重（总分 100）
WEIGHT_LIQUIDITY    = 20      # 25→20，流动性权重降低（新链天然低）
WEIGHT_HOLDERS      = 15      # 25→15，holder 权重降低
WEIGHT_DISTRIBUTION = 15      # 20→15
WEIGHT_NARRATIVE    = 25      # 15→25，叙事/KOL 权重提高（Arc 早期靠叙事）
WEIGHT_AGE          = 10      # 15→10
WEIGHT_SMART_MONEY  = 15      # 新增：聪明钱共振权重

# 决策阈值
MIN_SCORE_TO_BUY = 30        # 15→30，需要一定质量信号，避免买太多垃圾

# ═══════════════════════════════════════════════════════════════
# 叙事关键词
# ═══════════════════════════════════════════════════════════════
NARRATIVE_KEYWORDS = [
    "cool", "architect", "arcat", "beancat", "cat", "usdc", "circle",
    "arc", "stable", "dollar", "payfi", "coolcat", "bean",
    "blackrock", "visa", "mastercard",
    "pepe", "doge", "shib", "inu", "meme", "anime", "frog",
    "ai", "agent", "gpt", "robot", "nft",
    # AKA 生态
    "aka", "akadot", "akafun", "unitflow", "synthra", "hibachi",
    "tower", "sidoor", "argus", "arguspad", "tolly", "warp",
    "par", "long", "cool",
]

BLACKLIST_KEYWORDS = ["test", "fake", "scam", "honeypot", "rug"]

# ═══════════════════════════════════════════════════════════════
# 扫描参数
# ═══════════════════════════════════════════════════════════════
POLL_INTERVAL_SEC = 2
SCAN_BLOCK_RANGE  = 200

# ═══════════════════════════════════════════════════════════════
# 通知
# ═══════════════════════════════════════════════════════════════
JARVIS_TARGET = os.getenv("ARC_JARVIS_TARGET", "")

# ═══════════════════════════════════════════════════════════════
# 数据文件路径
# ═══════════════════════════════════════════════════════════════
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SEEN_TOKENS_FILE = os.path.join(BASE_DIR, ".seen_tokens.json")
LOG_FILE = os.path.join(BASE_DIR, "arc_sniper.log")

# ═══════════════════════════════════════════════════════════════
# ERC20 标准 ABI
# ═══════════════════════════════════════════════════════════════
ERC20_ABI = [
    {"constant": True, "inputs": [], "name": "name",
     "outputs": [{"name": "", "type": "string"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "symbol",
     "outputs": [{"name": "", "type": "string"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "decimals",
     "outputs": [{"name": "", "type": "uint8"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "totalSupply",
     "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
    {"constant": True, "inputs": [{"name": "", "type": "address"}], "name": "balanceOf",
     "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
    {"constant": False, "inputs": [
        {"name": "spender", "type": "address"},
        {"name": "amount", "type": "uint256"}
    ], "name": "approve",
     "outputs": [{"name": "", "type": "bool"}], "type": "function"},
    {"constant": True, "inputs": [
        {"name": "owner", "type": "address"},
        {"name": "spender", "type": "address"}
    ], "name": "allowance",
     "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
    {"anonymous": False, "inputs": [
        {"indexed": True, "name": "from", "type": "address"},
        {"indexed": True, "name": "to", "type": "address"},
        {"indexed": False, "name": "value", "type": "uint256"}
    ], "name": "Transfer", "type": "event"},
]

# Uniswap V2 风格 Router ABI
UNISWAP_V2_ROUTER_ABI = [
    {"constant": True, "inputs": [
        {"name": "amountIn", "type": "uint256"},
        {"name": "path", "type": "address[]"}
    ], "name": "getAmountsOut",
     "outputs": [{"name": "", "type": "uint256[]"}], "type": "function"},
    {"constant": False, "inputs": [
        {"name": "amountIn", "type": "uint256"},
        {"name": "amountOutMin", "type": "uint256"},
        {"name": "path", "type": "address[]"},
        {"name": "to", "type": "address"},
        {"name": "deadline", "type": "uint256"}
    ], "name": "swapExactTokensForTokens",
     "outputs": [{"name": "", "type": "uint256[]"}], "type": "function"},
]

# Arc V3 使用非标准 fee=200 (0.02%)
# 标准 Uniswap V3 fees (100/500/3000/10000) 在 Arc 上不使用

# Uniswap V3 SwapRouter ABI (exactInputSingle)
UNISWAP_V3_ROUTER_ABI = [
    {"inputs": [
        {"name": "tokenIn", "type": "address"},
        {"name": "tokenOut", "type": "address"},
        {"name": "fee", "type": "uint24"},
        {"name": "recipient", "type": "address"},
        {"name": "deadline", "type": "uint256"},
        {"name": "amountIn", "type": "uint256"},
        {"name": "amountOutMin", "type": "uint256"},
        {"name": "sqrtPriceLimitX96", "type": "uint160"}
    ], "name": "exactInputSingle",
     "outputs": [{"name": "amountOut", "type": "uint256"}],
     "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [
        {"name": "tokenIn", "type": "address"},
        {"name": "tokenOut", "type": "address"},
        {"name": "fee", "type": "uint24"},
        {"name": "recipient", "type": "address"},
        {"name": "amountIn", "type": "uint256"},
        {"name": "amountOutMin", "type": "uint256"},
        {"name": "sqrtPriceLimitX96", "type": "uint160"}
    ], "name": "exactInputSingle",
     "outputs": [{"name": "amountOut", "type": "uint256"}],
     "stateMutability": "payable", "type": "function"},
]
