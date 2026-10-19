# Arc Sniper — Arc 链自动打新机器人

在 Circle 推出的 Arc 链（EVM 兼容 L1，Gas 用 USDC）上自动发现并买入新 meme 币。

**不含止盈止损** — 机器人只负责发现和买入，离场由用户自行操作。

## 架构

```
arc_sniper/
├── config.py      # 所有配置参数
├── scanner.py     # 监听 DEX 工厂新币事件
├── onchain.py     # 链上数据查询（持有人、流动性、蜜罐检测）
├── filter.py      # 评分过滤逻辑
├── executor.py    # web3.py 买入执行
├── notify.py      # Jarvis 推送通知
└── main.py        # 主入口
```

## 核心流程

```
Scanner 发现新币 → OnChain 拉数据 → Filter 评分 → 评分通过 → Executor 买入 → Notify 推送
                                              → 评分不足 → 记日志跳过
```

## 1. Scanner（信号发现）

监听三个 DEX 工厂合约的新币创建事件：

| DEX | 机制 | 监听方式 |
|-----|------|----------|
| TOLLY | 不走联合曲线，全供应进永久锁定 USDC 池 | 工厂 TokenCreated 事件 |
| WARP | 联合曲线 → $69k 后迁移到 WarpDex | 工厂 TokenCreated 事件 |
| Uniswap V3 | 预计 Day1 支持 | PoolCreated 事件（标准 topic） |

- 每 2 秒轮询 `eth_getLogs`
- 已发现的代币不会重复处理（文件持久化）
- 支持多个工厂并行监控

## 2. OnChain（链上数据）

新币发现后 3 秒内拉取：

- **代币信息**: name, symbol, decimals, totalSupply（ERC20 标准调用）
- **持有人统计**: 扫描 Transfer 事件，构建余额映射，计算：
  - 持有人总数（排除零余额、合约自身）
  - Top10 持仓集中度
- **流动性**: 查池中 USDC 余额 × 2（双边估值）
- **蜜罐检测**: 模拟买入→卖出，检测是否能卖出（需 DEX ABI 确认后实现）
- **代币年龄**: 从创建区块时间戳计算

## 3. Filter（评分过滤）

### 一票否决（直接拒绝）

| 条件 | 阈值 | 原因 |
|------|------|------|
| 流动性 < $5k | `MIN_LIQUIDITY_USD = 5000` | 池太小，进出都砸盘 |
| 持有人 < 30 | `MIN_HOLDERS = 30` | 太少人，可能是测试币 |
| Top10 > 50% | `MAX_TOP10_PCT = 0.50` | 高度集中，rug 风险 |
| 年龄 > 30min | `MAX_TOKEN_AGE_SEC = 1800` | 开荒期只打新 |
| 蜜罐检测失败 | | 只能买不能卖 |
| 名称命中黑名单 | test/fake/scam/honeypot/rug | 明显垃圾 |

### 评分（满分 100）

| 维度 | 权重 | 评分逻辑 |
|------|------|----------|
| 流动性 | 25 | $5k=5分, $20k=15分, $50k=20分, $100k+=25分 |
| 持有人 | 25 | 30人=10分, 50人=15分, 100人=20分, 200+=25分 |
| 持仓分布 | 20 | Top10≤10%=20分, ≤20%=16分, ≤30%=12分, ≤40%=8分, >40%=4分 |
| 叙事关键词 | 15 | 名称匹配 Circle/Arc 生态关键词，每个+5分，上限15分 |
| 代币年龄 | 15 | <1min=15分, <5min=12分, <10min=8分, <30min=4分 |

**最低买入评分: 50/100** (`MIN_SCORE_TO_BUY`)

叙事关键词列表（可在 config.py 修改）：
```
cool, architect, arcat, beancat, cat, usdc, circle, arc, stable,
dollar, payfi, pepe, doge, shib, inu, meme, anime, ai, agent...
```

## 4. Executor（买入执行）

- 使用 **web3.py 直连 Arc RPC**，私钥本地签名
- 不依赖 Agent Wallet (baw)，支持 Arc 链 Day1
- Gas 用 USDC 支付（Arc 链原生特性）
- 支持三种 DEX 路径（TOLLY / WARP / Uniswap V3）
- 自动 USDC 授权（approve MAX_UINT256，只需一次）
- 固定仓位 $20 USDC/笔（可配置）

### 频率控制

| 限制 | 值 | 说明 |
|------|-----|------|
| 最小买入间隔 | 30秒 | 两笔买入之间最少等 30s |
| 每小时上限 | 10笔 | 防止短时间内买太多 |

## 5. Notify（通知）

买入成功后推送 Jarvis 消息，包含：
- 代币名称、符号、合约地址
- DEX 来源
- 买入金额和收到的代币数量
- 评分明细
- 交易哈希

## 配置

### 必须配置（9/16 主网上线后确认）

在 `config.py` 中修改或通过环境变量设置：

```bash
# 钱包私钥（必须）
export ARC_PRIVATE_KEY="你的私钥"

# Jarvis 通知目标（必须）
export ARC_JARVIS_TARGET="userId:thread:threadId"

# RPC URL（可选，默认 https://rpc.arc-scan.org）
export ARC_RPC_URL="https://rpc.arc-scan.org"
```

`config.py` 中需要确认的参数：
```python
USDC_ADDRESS = ""           # Circle 原生 USDC 合约地址
TOLLY_FACTORY = ""          # TOLLY 工厂合约（不是 TOLLY 代币地址）
WARP_FACTORY = ""           # WARP 工厂合约
UNISWAP_V3_FACTORY = ""     # Uniswap V3 Factory
UNISWAP_V3_ROUTER = ""      # Uniswap V3 SwapRouter
```

### 可调参数

```python
BUY_AMOUNT_USDC = 20       # 每笔买入金额
MAX_SLIPPAGE_PCT = 15      # 最大滑点
MIN_LIQUIDITY_USD = 5000  # 最低流动性
MIN_HOLDERS = 30           # 最少持有人
MAX_TOP10_PCT = 0.50       # Top10 最高占比
MAX_TOKEN_AGE_SEC = 1800   # 代币最大年龄
MIN_SCORE_TO_BUY = 50      # 最低买入评分
MIN_BUY_INTERVAL_SEC = 30  # 最小买入间隔
MAX_BUYS_PER_HOUR = 10     # 每小时最多买入
```

## 使用

### 安装依赖

```bash
pip install -r arc_sniper/requirements.txt
```

### 环境变量

```bash
# 必须设置
export ARC_PRIVATE_KEY="你的钱包私钥"
export ARC_JARVIS_TARGET="userId:thread:threadId"

# Grok 监控（可选，推荐）
export GROK_API_KEY="你的xAI API Key"
```

### 运行模式

```bash
# 1. 只启动 DEX Scanner（监听工厂事件）
python -m arc_sniper.main

# 2. DEX Scanner + Grok Twitter 监控同时运行
python -m arc_sniper.main --grok

# 3. 只启动 Grok Twitter 监控（不监听 DEX 工厂）
python -m arc_sniper.main --grok-only

# 4. Dry Run（只扫描评分，不实际买入）
python -m arc_sniper.main --dry-run

# 5. 自定义 RPC 和金额
python -m arc_sniper.main --rpc https://rpc.arc-scan.org --amount 50
```

### 测试网测试

```bash
python -m arc_sniper.main --rpc https://rpc.testnet.arc.io --dry-run
```

## 双通道信号系统

### 通道 1: DEX Scanner（链上事件监听）

- 每 2 秒轮询 TOLLY / WARP / Uniswap V3 工厂合约
- 发现新币 → 链上数据评分 → 评分≥50 → 买入 $20 USDC
- 优势：零延迟，第一时间发现

### 通道 2: Grok Twitter 监控（推荐）

- 每 10 秒通过 Grok API 检索 Twitter
- 监控 12 个 Arc 生态项目方账号
- 发现推文中的合约地址 → 链上验证 → 买入 $50 USDC
- 优势：项目方发的币确定性更高，仓位更大

**监控账号分级：**

| 优先级 | 类型 | 账号 | 买入金额 |
|--------|------|------|----------|
| P1 | Arc 官方 | @arc | $50 |
| P2 | 发射台 | @Arcanedotfi @Zyoradotfun @rwarcdotfun @sashimidotfun @arcpad_meme @eve_dot_fun @Ayooclub @liftdotfun @focidotfamily | $50 |
| P3 | 生态项目 | @Longdotsupply @PlayMossling @OrixaDomains | $50 |

**Grok vs Clawby x_search：**

| 对比 | Clawby x_search | Grok API |
|------|-----------------|----------|
| 数据源 | 第三方中转 | Twitter 原生 |
| 覆盖面 | 受 API 限制 | 全量 Twitter |
| 智能提取 | 正则匹配 | LLM 理解语义 |
| 延迟 | ~3s | ~5-10s |
| 成本 | Clawby 额度 | xAI 额度 |

两个通道并行运行，各自独立频率控制，不会互相干扰。

## 与 Robinhood Sniper 的区别

| 对比项 | Robinhood Sniper | Arc Sniper |
|--------|-----------------|------------|
| 链 | Robinhood Chain (ETH Gas) | Arc Chain (USDC Gas) |
| 执行 | baw (Agent Wallet) | web3.py 直连 (私钥签名) + Grok API |
| 止盈止损 | 600+ 行，PnL阶梯+Trailing Stop | 无，用户自行离场 |
| 仓位 | 按评分动态计算 $10-50 | Scanner $20 / Grok 信号 $50 |
| 信号源 | DexScreener + RPC + 聪明钱 | DEX 工厂事件 + Grok Twitter 监控 |
| 复杂度 | ~2000 行 | ~2300 行 |
| 持仓监控 | 有（完整生命周期管理） | 无（只买入） |

## 安全

- 私钥从环境变量读取，不写入文件
- `.secrets.json` 和 `.seen_tokens.json` 已在 `.gitignore` 中
- 买入金额固定，不会一次性花光钱包
- 频率限制防止短时间大量买入
- 蜜罐检测防止买到貔貅币（待 DEX ABI 确认后激活）
- 一票否决机制过滤明显垃圾币

## 风险提示

- Arc 链公共主网 9/16 上线，早期资产无官方背书
- 流动性极浅，进出容易砸盘
- 合约未审计，有 rug 风险
- RPC 可能不稳定
- 本工具不构成投资建议，风险自负
