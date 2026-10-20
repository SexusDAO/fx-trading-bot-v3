# FOMO 打土狗工作流

Robinhood Chain 链上信号跟单机器人。监听 KOL 钱包行为和鲸鱼大额买入，自动执行开仓、止盈和止损。

---

## 1. 整体架构图

```
┌──────────────────────────────────────────────────────────────────────┐
│                         信号源层（双轨）                               │
│                                                                      │
│  轨道A：WebSocket RPC（QuickNode）    轨道B：fomoapi WebSocket         │
│  新 Pair 零延迟监听                   KOL买卖 / 鲸鱼 / 多人共买         │
│  pair_listener.py                    handle_alert()                  │
│         │                                  │                         │
│         └─────────────┬──────────────────  ┘                         │
│                       ▼                                              │
│                 ws_monitor.py 信号路由                                │
└───────────────────────┬──────────────────────────────────────────────┘
                        │
                        ▼
┌──────────────────────────────────────────────────────────────────────┐
│                       全局风控门                                       │
│  Token黑名单 · 分级冷却期 · 硬/软黑名单 · 每日单币黑名单                 │
│  全局日亏损熔断$150 · Gas高峰暂停300s · 并发上限2 · 全局冷却45s           │
│  仓位数量上限6 · ETH余额保留$50                                         │
└───────────────────────┬──────────────────────────────────────────────┘
                        │
                        ▼
┌──────────────────────────────────────────────────────────────────────┐
│                  安全过滤层 F1~F11（依序拦截）                           │
│  F1扫描黑名单→F2Binance审计→F3模拟卖出→F5Clawby安全                     │
│  →F6流动性分层→F7新币过滤→F8SmartMoney→F9防刷量                         │
│  →F10Boost评分→F11Deployer黑名单                                      │
└───────────────────────┬──────────────────────────────────────────────┘
                        │
                        ▼
┌──────────────────────────────────────────────────────────────────────┐
│          策略路由 / 仓位计算 / 买入执行（executor.py · baw swap）        │
│  策略A KOL跟单    策略B 多人共买    策略W 鲸鱼信号                       │
└───────────────────────┬──────────────────────────────────────────────┘
                        │
                        ▼
┌──────────────────────────────────────────────────────────────────────┐
│              持仓巡检（每 30 秒）                                       │
│  check_exit_signal() → 按策略A/B/W分层离场 · 止盈止损 · 巡检S级共识      │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 2. 文件说明

| 文件 | 职责 |
|------|------|
| `main.py` | 启动入口（start/status/log），flock 防止双开 |
| `ws_monitor.py` | 核心：信号路由、策略判断、持仓巡检（每30s）、执行离场 |
| `position_manager.py` | 仓位状态读写、`check_exit_signal()` 分策略止盈止损、黑名单 |
| `executor.py` | baw swap 执行层：买入/卖出、滑点递增重试、分批卖出 |
| `config.py` | 所有可调参数（常量集中管理） |
| `token_filter.py` | F1~F11 安全过滤层（蜜罐/流动性/新币/防刷量等） |
| `pair_listener.py` | WebSocket RPC：监听 V2/V3 工厂新 Pair 事件 |
| `gas_manager.py` | ETH 余额监控（ETH 同时作为 gas + 交易资金） |
| `notify.py` | Jarvis 推送通知封装 |
| `market_utils.py` | DexScreener 市值/价格工具 |
| `smart_money_scorer.py` | KOL 胜率/评分计算 |
| `refresh_watchlist.py` | 从 Binance PnL 榜单刷新 KOL 名单 |
| `refresh_rt_watchlist.py` | 从 robinhoodtrenches/windvane 补充聪明钱地址 |
| `kol_exit_review.py` | KOL 触发卖出后复盘分析 |
| `evm_watchlist.json` | KOL EVM 地址白名单（handle + grade + score） |
| `deployer_blacklist.json` | Rug 创建者地址持久化黑名单 |
| `state.json` | 持仓状态（原子写入，含 .bak 备份） |
| `trade.log` | 运行日志（supervisor 自动 rotate，20MB×5份） |
| `supervisord.conf` | 进程守护配置 |
| `fomo_ctl.sh` | 一键启停脚本 |
| `watchdog.sh` | 外部看门狗（cron 每5分钟）+ Jarvis 崩溃告警 |

---

## 3. 双轨信号源

### 轨道 A — WebSocket RPC（零延迟）

- **URL**：`WS_RPC_URL`（QuickNode Robinhood Mainnet WSS）
- **监听**：Uniswap V2 工厂 `PairCreated` + V3 工厂 `PoolCreated` 事件
- **作用**：新 Pair 上线即时推入 `pair_queue`；fomoapi 信号到达时若 token 已在队列则标记 `entry_source="rpc"`
- **保护期**：RPC 路径入场保护期 **60 分钟**（`ENTRY_PROTECT_MIN_RPC`）；fomoapi 路径 **30 分钟**（`ENTRY_PROTECT_MIN_FOMO`）
- **降级**：60 秒窗口内断线 ≥ 3 次（`WS_ERROR_RATE_THRESHOLD`）→ 自动切换 DexScreener 轮询，间隔 15s（`WS_FALLBACK_POLL_SEC`）

### 轨道 B — fomoapi WebSocket

- **URL**：`wss://api.fomoapi.io/ws/alerts`
- **认证**：`FOMO_API_KEY`
- **监听**：KOL 买卖 alert（buy/sell）+ 多人 FOMO 买入
- **断线重连**：指数退避（初始 2s，最大 60s）

两轨通过 `asyncio.gather(..., return_exceptions=True)` 并发启动，协程异常互不影响。

---

## 4. 三种交易策略

### 策略 A — KOL 跟单

**触发条件**

- **S/A 级**（`SINGLE_TRIGGER_GRADES`）：单人买入即直接触发
- **B/C 级**：计入 5min 确认窗口（`_KOL_CONFIRM_TTL = 300s`），≥ 2 人买入同一 token 才触发

**基础仓位（`BASE_POSITION`）**

| 等级 | 基础仓位 | 最大仓位上限（`MAX_POSITION`） |
|------|---------|-------------------------------|
| S    | $45     | $75 |
| A    | $36     | $65 |
| B    | $30     | $55 |
| C    | $24     | $55 |

**仓位加分项（叠加后不超过对应等级 MAX_POSITION）**

| 条件 | 加分 |
|------|------|
| DexScreener Boost ≥ $100（`BONUS_BOOST_STRONG`） | +$5 |
| Clawby ≥ 2 买家双源确认（`BONUS_CLAWBY_CONFIRM`） | +$8 |
| ≥ 3 个 KOL 同时买同一 token（`BONUS_MULTI_KOL`） | +$10 |
| 同时出现鲸鱼买入信号（`BONUS_WHALE_ALIGN`） | +$8 |
| 触发 KOL 30d 胜率 ≥ 70%（`BONUS_HIGH_WINRATE`） | +$5 |

**price drift 保护**（`PRICE_DRIFT_ENABLED=True`）：信号快照价格与执行前偏差 > 10%（`PRICE_DRIFT_MAX_PCT`）拒绝买入；报价偏差 > 12%（`QUOTE_DRIFT_MAX_PCT`）同样拒绝。

---

### 策略 B — 多人共买

**触发条件**：5 分钟窗口（`MULTI_BUY_WINDOW_MIN`）内 ≥ **4** 个不同 trader 买入同一 token（`MULTI_BUY_THRESHOLD`）

**分档仓位**

| 人数 | 信号等级 | 仓位 |
|------|---------|------|
| 4 ~ 13 人 | 常规 | $15（`SIZE_MULTI`）|
| ≥ 14 人（`MULTI_BUY_STRONG_THR` / `SIGNAL_GRADE_S2_THR`） | S 级 | $25（`SIZE_SIGNAL_S`），自动进入 S 级共识持仓模式 |

**高市值过滤**（见第 11 章）：当前 MC ≥ $5M 时需额外验证回撤 ≥ 30% 且 6h 涨幅 ≤ 5% 才允许买入。

---

### 策略 W — 鲸鱼信号

#### 鲸鱼判断：双重门槛（绝对金额 + MC 占比）

单笔买入必须**同时满足**：
1. 绝对金额 ≥ `WHALE_MIN_USD`（$10,000）
2. 买入金额 / 当前 MC ≥ 动态比例阈值

**MC 占比动态阈值表（`WHALE_MC_RATIO_TABLE`）**

| 当前 MC | 最低占比 |
|---------|--------|
| < $500k | ≥ 1.0% |
| $500k ~ $2M | ≥ 0.8% |
| $2M ~ $5M | ≥ 0.5% |
| $5M ~ $20M | ≥ 0.3% |
| $20M ~ $50M | ≥ 0.2% |
| ≥ $50M | ≥ 0.15% |

不满足占比的大额买入（如大 MC 上的 $10k）直接忽略，不进队列。

#### 鲸鱼买入分级

| 等级 | 绝对金额门槛 | 行为 |
|------|------------|------|
| C 级 | ≥ $10,000（`WHALE_C_THRESHOLD_USD`）| 持仓期间 signal_score +（不触发策略W）|
| C 级进队 | ≥ $20,000（`WHALE_C_QUEUE_USD`）| 进入策略W 5min验证队列 |
| B 级 | ≥ $50,000（`WHALE_B_THRESHOLD_USD`）| 直接触发策略W确认流程 |
| A 级 | ≥ $100,000（`WHALE_A_THRESHOLD_USD`）| 触发策略W S级，最高仓位 |

**确认流程（10 分钟窗口 `WHALE_CONFIRM_WINDOW_MIN=10`）**

1. 触发后快照当前价格，启动异步轮询协程
2. 每 **10 秒**查询 GeckoTerminal，等待 ≥ **2** 个新地址（`_WHALE_W_MIN_NEW_BUYERS`）各自买入 ≥ **$5,000**（`_WHALE_W_NEW_BUYER_MIN_USD`）
3. 满足条件 → 立即开仓，B 级 $30（`WHALE_BUY_SIZE_USD`），A 级（≥ $100k）升级 S 级 → $45（`SIZE_SIGNAL_S`）
4. 价格从快照涨幅 ≥ **50%** → 放弃（防高位追涨）
5. 窗口内未满足条件 → 超时放弃

策略 W 开仓时天然进入 **S 级共识持仓模式**（`signal_score` 初始 75，持有线 60）。

#### 鲸鱼卖出分级（持仓中）

| 等级 | 绝对金额门槛 | 行为 |
|------|------------|------|
| 预警 | ≥ $10,000（`WHALE_SELL_WARN_USD`）且满足动态比例 | 记录日志，不操作 |
| Soft | ≥ $50,000（`WHALE_SELL_SOFT_USD`）且满足动态比例 | 策略 W 全清 |
| Hard | ≥ $100,000（`WHALE_SELL_HARD_USD`）且满足动态比例 | 策略 B/W/S 级全清；策略 A 仅预警 |

---

### 多策略叠仓

同一 token 同时触发策略 A 和策略 B，可叠加一次（A $30 + B $15 = $45），单 token 最大持仓 **$45**（`MAX_POSITION_SIZE_USD`）。

---

## 5. 全局风控门

买入前依次检查，任一触发则跳过本次开仓（不影响卖出）：

| 检查项 | 参数/值 | 行为 |
|--------|--------|------|
| Token 静态黑名单 | `TOKEN_BLACKLIST`（config.py 硬编码） | 永久跳过 |
| 分级冷却期 | 止盈=0s / 普通止损=1800s / 流动性崩溃=14400s / KOL卖=3600s / 成交量断崖=1800s / 追踪止损=1200s / 硬止损=7200s | 冷却期内跳过开仓 |
| 硬黑名单 | 止损/硬止损/流动性崩溃离场 → 当日全天 | 当日禁止重入 |
| 软黑名单 | 止盈/KOL卖/追踪止损离场 → 需强信号解锁 | 未解锁前禁止 |
| 每日单币黑名单 | 单 token 当日已亏损 ≥ $30（`DAILY_LOSS_BLACKLIST_USD`） | 当日禁止 |
| 全局日亏损熔断 | 所有已实现亏损总和 ≥ $150（`DAILY_LOSS_LIMIT_USD`） | 停止所有新开仓 |
| Gas 高峰暂停 | 单笔 gas ≥ $2（`GAS_HIGH_THRESHOLD_USD`）→ 暂停 300s（`GAS_PAUSE_WINDOW_SEC`）；`GAS_PAUSE_BUY_ONLY=True` 仅暂停买入，卖出不受影响 | 暂停买入 |
| 全局并发上限 | 同时最多 **2** 笔买入进行中（`MAX_CONCURRENT_BUYS`） | 丢弃多余信号 |
| 全局买入冷却 | 两笔买入之间最短间隔 **45s**（`GLOBAL_COOLDOWN_SEC`） | 冷却期跳过 |
| 仓位数量上限 | ≥ $50 的大仓最多 **6** 个同时持有（`MAX_POSITIONS`，`POSITION_LIMIT_THRESHOLD_USD=50`） | 满仓跳过 |
| ETH 余额 | 保留 $50 不动（`GAS_RESERVE_USD`），可用余额不足时拒绝 | 余额不足跳过 |

---

## 6. 安全过滤层

共 **11 层**，按 F1 → F11 顺序执行，任一不通过则拒绝买入并加入扫描黑名单（TTL 1 小时）。

| 层 | 名称 | 数据源 | 拦截条件 |
|----|------|--------|---------|
| F1 | 扫描黑名单 | 内存（TTL 1h） | 1 小时内已被拒绝过的 token |
| F2 | Binance Web3 安全审计 | `web3.binance.com` | 风险等级=HIGH / 被安全商标记 / 黑名单合约 / 买税 > 10% / 卖税 > 10% / HIGH 级风险项（`MAX_BUY_TAX=10.0`，`MAX_SELL_TAX=10.0`） |
| F3 | 模拟卖出（蜜罐检测） | `baw quote`（5U 探针） | 卖出报价 = 0 或损耗 > 50%（缓存 5 分钟） |
| F4 | fo 量检查 | robinhoodtrenches API | 信息性指标（不拦截，供参考） |
| F5 | Clawby 安全审计 | Clawby `dex_token_security` | 蜜罐 / 无法卖出 / 卖税 > 10% / 含黑名单功能（缓存 5 分钟） |
| F6 | 流动性分层 | Clawby `dex_token_pool` | MC < $100k 需 ≥ $8k；MC $100k~$300k 需 ≥ $25k；MC ≥ $300k 需 ≥ $60k；冲击比 `buy_size / liq > 0.5%` 拒绝（缓存 1 分钟） |
| F7 | 新币过滤 | Clawby `dex_token_info` | 上线 **> 7 天**的老币拒绝（缓存 2 分钟） |
| F8 | SmartMoney 交叉验证 | Clawby `dex_smart_money` | 信息性指标（不拦截；双源确认时仓位 +$8） |
| F9 | 防刷量检测 | DexScreener 5min txns | 买入地址去重比 < 30%（`WASH_UNIQUE_ADDR_RATIO_MIN`） / 单钱包持仓 > 50%（`WASH_TOP_WALLET_MAX`） / 均值买入 < $1 且笔数 > 20（缓存 1 分钟）；< 5 笔时跳过检测 |
| F10 | DexScreener Boost | `token-boosts/latest/v1` | 信息性指标（≥ $100 有效 boost → 仓位 +$5；全量缓存 10 分钟刷新） |
| F11 | Deployer 历史检查 | Clawby `dex_token_info` + `deployer_blacklist.json` | **三步**：①从 Clawby 获取 `creator_address`（无额外费用）；②命中本地黑名单 → 一票否决；③`twitter_create_token_count` > `DEPLOYER_MAX_TOKEN_COUNT`（5）→ 预警日志（`DEPLOYER_SUSPECT_LOG_ONLY=True`）或拒绝；查不到 creator 时：`DEPLOYER_FAIL_REJECT=True` 拒绝 |

---

## 7. 离场信号体系

持仓巡检每 **30 秒**执行一次，调用 `check_exit_signal()` 按策略路由，返回 `None`（hold）或 action dict。

---

### 策略 A 离场（`_check_exit_strategy_a`）

| 优先级 | 触发条件 | 动作 |
|--------|---------|------|
| **P1** | `trigger_kol_exited=True`（触发者 KOL 卖出 ≥ $2,000，由 `handle_alert` 实时置位） | 全清 |
| **P2** | `micro_no_stop=False` **且** `entry_mc < $500,000`（`STRATEGY_A_MICRO_MC_THRESHOLD`）**且** `pnl ≤ -35%`（`STRATEGY_A_MICRO_STOP_LOSS`） | 全清（微盘止损）|
| **P3** | 市值里程碑 | 每档卖出 **10%** |

**市值止盈阶梯（`STRATEGY_A_TP_LADDER`）**

| MC 达到 | 卖出比例 |
|---------|---------|
| $300,000  | 10% |
| $500,000  | 10% |
| $1,000,000 | 10% |
| $3,000,000 | 10% |
| $5,000,000 | 10% |

**非触发者 KOL 卖出（`handle_alert` 实时处理）**

- **极端兜底**：5 min 内 ≥ **5** 个不同 KOL 卖出（`STRATEGY_A_EXTREME_SELL_COUNT`）**且** 1h 价格跌幅 ≤ **-10%**（`STRATEGY_A_EXTREME_PRICE_DROP`）→ 立即全清
- **其余**：只扣 `signal_score` -15（`KOL_SELL_SCORE_PENALTY`），不执行任何卖出操作

**策略 A 完全不生效的止损条件**：硬止损 -50% / 追踪止损 / 成交量断崖 / 超时止损

---

### 策略 B 离场（`_check_exit_strategy_b`）

| 优先级 | 触发条件 | 动作 |
|--------|---------|------|
| **P1** | `pnl ≤ -35%`（`STRATEGY_B_HARD_STOP`，全程生效，不区分保护期） | 全清 |
| **P2** | 持仓 > 15 min **且** 1h 成交量下降 ≥ 70%（`STRATEGY_B_VOLUME_CLIFF`） | 全清 |
| **P3** | 持仓时间 ≥ 12h（`STRATEGY_B_TIMEOUT_HOURS`） | 全清 |
| **P4** | `signal_score < 0`（`STRATEGY_B_SCORE_EXIT`） | 全清 |
| **P4** | `0 ≤ signal_score < 25`（`STRATEGY_B_SCORE_REDUCE`） | 减仓 60% |
| **P4** | `25 ≤ signal_score < 50`（`STRATEGY_B_SCORE_TRIM`） | 减仓 30% |
| **P5** | `peak_pnl ≥ 30%`（`STRATEGY_B_TRAILING_ACTIVE`）后激活，从峰值回撤 ≥ 25%（`STRATEGY_B_TRAILING_DROP`） | 全清（追踪止损）|
| **P6** | 市值里程碑 | 按比例卖出 |

**市值止盈阶梯（`STRATEGY_B_TP_LADDER`）**

| MC 达到 | 卖出比例 |
|---------|---------|
| $300,000  | 25% |
| $500,000  | 25% |
| $1,000,000 | 25% |
| $5,000,000 | **100%（全清）** |

---

### 策略 W 离场（`_check_exit_strategy_w`）

| 优先级 | 触发条件 | 动作 |
|--------|---------|------|
| **P1** | `whale_reversed=True`（鲸鱼卖出 ≥ $20,000，`STRATEGY_W_WHALE_EXIT_USD`，由 `handle_alert` 实时置位） | 全清 |
| **P2** | 市值里程碑 | 每档卖出 **10%** |

**市值止盈阶梯（`STRATEGY_W_TP_LADDER`）**：与策略 A 相同（$300k / $500k / $1M / $3M / $5M 各卖 10%）

**策略 W 完全不生效的止损条件**：硬止损 / 追踪止损 / 成交量断崖 / 超时止损 / KOL 卖出信号

---

### S 级共识持仓模式

策略 W 开仓 / 策略 B ≥ 14 人共买时自动进入。此外，普通策略 A/B 开仓后若持续收到足够多的新信号，也可在持仓期**动态升级**为共识模式。

**动态升级条件（`try_activate_consensus`）**：开仓后在 `CONSENSUS_WINDOW_MIN=60` 分钟内累计 ≥ `CONSENSUS_MIN_SIGNALS=4` 个不同来源的新信号（KOL/鲸鱼/共识信号），且当前盈利 ≥ `CONSENSUS_MIN_PNL=0%` → 升级，超过 `CONSENSUS_TIMEOUT_H=4h` 无新信号自动降回普通模式。

| 参数 | 值 |
|------|---|
| 初始 signal_score | 75（`S_CONSENSUS_INIT_SCORE`）|
| 持有线（≥ 此分不操作） | 60（`S_CONSENSUS_HOLD_LINE`）|
| 50~59 → 减仓 | -20% |
| 35~49 → 减仓 | -40% |
| 20~34 → 减仓 | -70% |
| < 20 → 全清 | 100% |
| 硬止损（30min ≥ 2 个 KOL 卖出，`S_HARD_STOP_KOL_RESONANCE`） | 全清 |
| 硬止损（单笔卖出满足动态鲸鱼门槛且 ≥ $100,000，`S_HARD_STOP_WHALE_SELL`） | 全清 |

**加减分规则**

| 事件 | 分值 |
|------|------|
| A 级 KOL 持仓期间买入 | +20（`S_SCORE_KOL_BUY_A`）|
| B 级 KOL 持仓期间买入 | +12（`S_SCORE_KOL_BUY_B`）|
| C 级 KOL 持仓期间买入 | +8（`S_SCORE_KOL_BUY_C`）|
| 鲸鱼买入 | +10（`S_SCORE_WHALE_BUY`）|
| 小额 KOL 卖出（非庄出货） | -15（`S_SCORE_KOL_SELL_SMALL`）|
| 1h 热度下降（买入人数 < 入场时 50%） | -8（`S_SCORE_HEAT_DROP`）|

---

### 微盘免止损模式（micro_no_stop）

满足以下**全部条件**时标记 `micro_no_stop=True`，不设任何止损，只走 MC 阶梯止盈：

- 入场 MC < $500,000（`MICRO_NO_STOP_MC_THRESHOLD`）
- 发币时间 ≤ **3 天**（`MICRO_NO_STOP_MAX_AGE_DAYS`）
- 历史 MC 峰值从未超过 $1,000,000（`MICRO_NO_STOP_BROKE_1M_THR`）

MC 峰值一旦突破 $1M，自动取消豁免，恢复正常止损。

---

## 8. DCA 补仓逻辑

**当前状态：已禁用**（`DCA_ENABLED = False`）

已持仓时，任何新买入信号直接 return，不补仓。配置项保留供后续重新启用：

| 参数 | 值 |
|------|---|
| `DCA_DROP_PCT` | 0.15（相对均价跌 15% 才触发）|
| `DCA_SIZE_USD` | $15 每次 |
| `DCA_MAX_TIMES` | 最多 3 次 |
| `DCA_MAX_TOTAL_USD` | 单 token 总仓位上限 $120 |
| `DCA_SKIP_1H_DROP` | -5%（1h 仍在下跌则跳过）|
| `DCA_SKIP_VOL_RATIO` | 0.50（当前 1h 量 < 上 1h 量×50% 则跳过）|
| `DCA_SKIP_LOW_MC_6H` | -30%（低位币 entry_mc < $100k 且已补过 ≥1 次，6h 跌幅超此则跳过）|

---

## 9. 执行层

### ETH 价格获取

主源：Binance 公开 API（`/api/v3/ticker/price?symbol=ETHUSDT`）；备用：DexScreener WETH/USDC。
缓存 TTL **30 秒**（`_ETH_PRICE_CACHE_TTL`），合理区间 $100 ~ $100,000（`ETH_PRICE_MIN` / `ETH_PRICE_MAX`），双源全部失败则拒绝买入。

### 买入滑点（按流动性动态设置）

| 流动性 | 起始滑点 |
|--------|---------|
| > $500,000 | 10%（`SLIPPAGE_BUY_LIQ_HIGH`）|
| $100,000 ~ $500,000 | 15%（`SLIPPAGE_BUY_LIQ_MID`）|
| $20,000 ~ $100,000 | 25%（`SLIPPAGE_BUY_LIQ_LOW`）|
| < $20,000 | 35%（`SLIPPAGE_BUY_LIQ_TINY`）|
| 无流动性数据 | 15%（`SLIPPAGE_BUY_NORMAL`）|

失败后每次递增 +10%，上限 49%（`SLIPPAGE_BUY_MAX`），最多重试 **4 次**（`SELL_RETRY_COUNT`）。

### 卖出策略

- 起始滑点 **20%**（`SLIPPAGE_SELL_BASE`），每次重试递增 **+5%**（`SLIPPAGE_SELL_STEP`），上限 **49%**（`SLIPPAGE_SELL_MAX`），最多重试 4 次
- 持仓价值 > 流动性 **5%** → 触发分批卖出，每批不超过流动性 4%，分 2~5 批（`_batch_sell`）
- **粉尘过滤**：持仓价值 < **$0.50**（`DUST_USD_THRESHOLD`）视为粉尘，跳过卖出
- **最低收益检查**：预计回收价值 < $1 或 < gas×5（`GAS_MIN_RATIO`）→ 跳过，避免负收益

### 交易确认等待

- 第一轮等待 **480 秒**，超时后查余额兜底确认
- 余额未清 → 升级 Gas 至 MEDIUM，再等 **300 秒**
- 两轮合计 **780 秒**仍未确认 → 认定失败，递增滑点重试
- 任意订单 pending ≥ **180 秒**（`GAS_ESCALATE_TIMEOUT_S`）→ 全局 Gas 升级到 MEDIUM，持续 30 分钟后自动回落

### 数量精度

- 余额 ≥ 1 token → 取整（避免土狗合约精度拒绝）
- 余额 < 1 token → 保留 8 位小数

---

## 10. 黑名单机制

### Token 静态黑名单（`TOKEN_BLACKLIST`）

`config.py` 硬编码，进程内存常驻，买入路径第一关检查。

### 扫描黑名单（`_scan_bl`）

安全过滤层 F1~F11 任一失败的 token，内存存储，TTL **1 小时**后自动过期。

### 每日单币黑名单（`_DAILY_LOSS`）

当日单 token 已实现亏损 ≥ **$30**（`DAILY_LOSS_BLACKLIST_USD`），当日不再开仓，跨日自动重置。

### 全局日亏损熔断

当日所有已实现亏损总和 ≥ **$150**（`DAILY_LOSS_LIMIT_USD`），停止所有新开仓，跨日自动重置。

### 分级冷却期（离场后设置）

| 离场原因 | 冷却时长 |
|---------|---------|
| `take_profit`（止盈） | **0**（无冷却，`COOLDOWN_TAKE_PROFIT`）|
| `stop_loss`（普通止损） | **30 分钟**（`COOLDOWN_NORMAL_STOP_LOSS`）|
| `vol_cliff`（成交量断崖） | **30 分钟**（`VOL_CLIFF_COOLDOWN_MIN`）|
| `kol_sell`（KOL 卖出跟单） | **60 分钟**（`KOL_SELL_COOLDOWN_MIN`）|
| `trailing_stop`（追踪止损） | **20 分钟**（`TRAILING_STOP_COOLDOWN_MIN`）|
| `hard_stop`（硬止损 -35%） | **120 分钟**（`HARD_STOP_COOLDOWN_MIN`）|
| `liquidity_crash`（流动性崩溃） | **240 分钟**（`COOLDOWN_LIQUIDITY_CRASH`）|

### 硬黑名单（`_HARD_REENTRY_BLACKLIST`）

`stop_loss` / `hard_stop` / `liquidity_crash` 离场 → **当日不可重入**，跨日自动重置。

### 软黑名单（`_SOFT_REENTRY_BLACKLIST`）

`take_profit` / `kol_sell` / `vol_cliff` / `trailing_stop` 离场 → 需强信号解锁才能重入。

**解锁条件（满足任一）**

| 条件 | 参数 |
|------|------|
| 5 min 内 ≥ 3 个 S 级信号 | `SOFT_UNLOCK_S_COUNT=3`，窗口 300s（`SOFT_UNLOCK_WINDOW`）|
| 5 min 内 ≥ 3 个不同 A/B 级 KOL 各自买入 | `SOFT_UNLOCK_AB_COUNT=3` |
| 5 min 内 ≥ 4 个不同场外强手**喊单**（无金额要求，`OUTSIDER_UNLOCK_COUNT=4`）| 非白名单 handle，统计无链上金额要求 |

### Deployer 黑名单（`deployer_blacklist.json`）

历史 Rug 创建者地址持久化存储，启动时加载，运行期可通过 `add_rug_deployer()` 动态追加并推送 Jarvis 通知。

F11 检查流程：
1. 从 Clawby `dex_token_info` 获取 `dev.creator_address`（并列，无额外 API 费用）
2. 与黑名单比对，命中则一票否决
3. 检查 `twitter_create_token_count`：超过 `DEPLOYER_MAX_TOKEN_COUNT=5` 次→ 按 `DEPLOYER_SUSPECT_LOG_ONLY` 开关决定仅预警还是直接拒绝
4. 查不到 creator 时：`DEPLOYER_FAIL_REJECT=True`（默认）拒绝买入，而非放行

---

## 11. 高市值专属规则（MC ≥ $5M）

策略 B 触发时，若当前市值 ≥ **$5,000,000**（`HIGH_MC_FILTER_THRESHOLD`），在买入前额外验证：

1. 距历史高点回撤幅度 ≥ **30%**（`HIGH_MC_MIN_DRAWDOWN`）
2. 6 小时涨幅 ≤ **5%**（`HIGH_MC_TREND_RISE_6H`）——上涨趋势中不追入

两个条件须**同时满足**才允许买入，任一不满足则拒绝本次信号。

策略 A / W 的高市值信号不经此过滤，由离场逻辑的 MC 阶梯止盈（$3M / $5M 档）自然控制。

---

## 12. 运维说明

### 启动

```bash
cd /root/.openclaw/workspace/fomo_workflow
supervisord -c supervisord.conf
supervisorctl -c supervisord.conf start fomo_workflow
# 或快捷脚本
./fomo_ctl.sh start
```

### 停止 / 重启

```bash
supervisorctl -c supervisord.conf stop fomo_workflow
supervisorctl -c supervisord.conf restart fomo_workflow
./fomo_ctl.sh restart
```

### 状态查看

```bash
supervisorctl -c supervisord.conf status
python3 main.py status      # 查看当前持仓
python3 main.py log 100     # 查看最近 100 行日志
./fomo_ctl.sh tail          # 实时 tail 日志
```

### 进程守护

- Supervisor 崩溃后 **5 秒**内自动重启，无限次（`startretries=999`）
- 优雅退出：收到 `SIGTERM/SIGINT` 时最多等待 **30 秒**让进行中的卖出完成
- 外部看门狗（`watchdog.sh`）由 cron 每 5 分钟检查，进程不在则告警 Jarvis

### 日志轮转

Supervisor 自动管理：单份最大 **20MB**，保留 **5 份**，总计约 100MB。

### 状态文件保护

`state.json` 采用"先写 `.tmp` 再 `os.replace()` 原子替换"策略，同时维护 `.bak` 备份。`.tmp` 残留时启动自动从 `.bak` 恢复，防止进程崩溃导致状态损坏。

### 关键参数速查

| 参数 | 值 | 说明 |
|------|---|------|
| `MAX_POSITIONS` | 6 | 大仓（≥$50）最大同时持有数 |
| `SIZE_KOL` | $30 | 策略 A 基础仓位 |
| `SIZE_MULTI` / `SIZE_MULTI_STRONG` | $15 / $25 | 策略 B 常规 / 强信号仓位 |
| `WHALE_BUY_SIZE_USD` | $30 | 策略 W 普通仓位 |
| `SIZE_SIGNAL_S` | $45 | S 级信号仓位（策略B强信号/策略W S3级）|
| `MAX_POSITION_SIZE_USD` | $45 | 单 token 最大持仓（叠仓上限）|
| `GAS_RESERVE_USD` | $50 | ETH 保留不动用于 gas |
| `DAILY_LOSS_LIMIT_USD` | $150 | 全局日亏损熔断线 |
| `MULTI_BUY_THRESHOLD` | 4 人 | 策略 B 触发门槛 |
| `MULTI_BUY_STRONG_THR` | 14 人 | 策略 B 升级 S 级门槛 |
| `STRATEGY_W_WHALE_EXIT_USD` | $20,000 | 策略 W 鲸鱼卖出全清线 |
| `STRATEGY_A_TRIGGER_SELL_MIN_USD` | $2,000 | 策略 A 触发者 KOL 卖出门槛 |
| `STRATEGY_B_HARD_STOP` | -35% | 策略 B 全程硬止损 |
| `GLOBAL_COOLDOWN_SEC` | 45s | 两笔买入最短间隔 |
| `MAX_CONCURRENT_BUYS` | 2 | 最大并发买入数 |
| `ENTRY_PROTECT_MIN_RPC` | 60 min | RPC 触发入场保护期 |
| `ENTRY_PROTECT_MIN_FOMO` | 30 min | fomoapi 触发入场保护期 |
| `DEPLOYER_SELL_THRESHOLD` | 10% | Deployer 卖出 totalSupply 占比触发全清（`ws_monitor.py` 顶部）|
| `MAX_DEPLOYER_MONITORS` | 20 | 最多同时订阅监控的 token 数（`ws_monitor.py` 顶部）|
| `WHALE_MC_RATIO_TABLE` | 见第4章 | 鲸鱼判断动态占比表（`config.py`）|
| `CONSENSUS_MIN_SIGNALS` | 4 | 持仓升级共识模式所需新信号数 |
| `CONSENSUS_WINDOW_MIN` | 60 min | 共识信号统计窗口 |
| `OUTSIDER_UNLOCK_COUNT` | 4 人 | 场外强手喊单解锁软黑名单所需人数（无金额要求）|

---

## 13. 版本历史

### v2.7（2026-09-06）

**F11 Deployer 检查全面增强**，解决“查不到 deployer = 放行”和“黑名单为空形同虚设”两个漏洞：

- **查不到 creator 时改为拒绝**：`DEPLOYER_FAIL_REJECT=True`（旧逻辑“查不到=放行”已修复）
- **接入 Clawby `dex_token_info` 获取 `creator_address`**：直接复用已有 Clawby 调用，无额外费用，并列查询不喀主流程
- **高频发币可疑预警**：`twitter_create_token_count > DEPLOYER_MAX_TOKEN_COUNT=5` → `[DEPLOYER SUSPECT]` 日志预警（`DEPLOYER_SUSPECT_LOG_ONLY=True` 时仅预警不拒绝）
- OPAR 复盘确认：其 creator `0xe2324ad98c…` 历史发币 11 个 token，新逻辑下会触发 `[DEPLOYER SUSPECT]` 预警

### v2.6（2026-09-06）

三项增强补强，均已通过语法与功能验证，进程正常运行，提交 `cbb42f2`：

- **① Deployer 实时 Transfer 监控**：开仓后通过独立 WSS 连接订阅 token 合约 `Transfer` 事件，过滤 `from == deployer` 的转账，累计卖出量 / totalSupply ≥ **10%** 时全清仓并推送 Jarvis 通知；最多同时监控 **20** 个 token（`DEPLOYER_MONITOR_MAX`）防订阅过载；平仓后检测 `position_exists` 自动退出协程
- **② 多 DEX 工厂地址覆盖**：引入 `MONITORED_FACTORIES` 统一管理工厂地址，`pair_listener` 动态遍历建立订阅；V2/V3 已激活，SushiSwap/Noxa V4/Pons/Flaunch 已写好框架并注释占位，从 Blockscout 查到地址后取消注释即生效
- **③ F3.5 Pool Hook 检测**：插在 F3（模拟卖出）之后、F5（Clawby 安全）之前，仅对 V4 池执行；零地址（无 Hook）或 Hook 在白名单则通过；Blockscout 源码未验证或 ABI 含危险方法（beforeSwap 等）仅记警告日志，**不阻断买入**（误判成本高，后续如需强制拒绝，在 `check_hook_contract` 返回 `False` 后 `return False` 即可）

---

### v2.5（2026-09-06）

- 多 DEX 工厂监听框架初版：`pair_listener` 支持多工厂地址并行订阅，为 v2.6 的 `MONITORED_FACTORIES` 统一管理做铺垫
- Deployer 监控基础结构：`ws_monitor` 中新增协程槽位和最大监控数量常量

---

### v2.4（2026-09-06）

基于实盘数据（60 笔）重构离场信号体系，**按策略完全分层**：

- **策略 A**：主要离场改为"触发者 KOL 卖出 ≥ $2,000 → 全清"；10% 阶梯止盈（$300k/$500k/$1M/$3M/$5M）；微盘 entry_mc < $500k 止损 -35%；极端兜底（5min ≥ 5人 + 1h 跌 > -10%）；删除旧 P2-P7 六个共振/价格判断逻辑
- **策略 B**：硬止损从"保护期 -35%+保护期后 -50%"统一改为**全程 -35%**
- **策略 W**：鲸鱼卖出 ≥ $20k → 全清；KOL 卖出信号完全忽略
- 新增字段：`trigger_kol_exited` / `whale_reversed` / `tp_triggered_mcs`
- 新增函数：`check_exit_signal()` 分策略路由；`_get_price_1h_change()` 极端兜底用
- `tp_triggered_mcs` 替代 `exits_done` 中的数字 MC 档位记录
- STACK 叠仓时清空 `tp_triggered_mcs` 重算（均价降低后止盈档位应重置）
- 数据依据：策略A 43%胜率 均值+1126%；策略W 36%胜率 均值+1706%；策略B 20%胜率 均值+579%

---

### v2.3（2026-09-05）

- 场外强手信号接入：非白名单地址大额买入（≥ $5k）持仓期间给 `signal_score` +3；大额卖出（≥ $50k）扣 -5；≥ 4 个场外 handle 各自买入 ≥ $10k 可解锁软黑名单重入
- 策略 A 触发者 KOL 卖出门槛 ≥ $2,000 正式化
- 旧 P2-P7 清理预备（完整清理在 v2.4 完成）

---

### v2.2（2026-09-03）

- 修复 `partial_sell` 成功后误调 `record_exit()` 导致 token 进入黑名单、后续信号被拦截
- 修复 Gas 高峰暂停时止损卖出也被拦截（止损/硬止损类卖出现在强制绕过 Gas 暂停）
- 修复 `DCA_ENABLED=True` 与代码实际"已持仓直接 return"矛盾，改为 `False` 并标注废弃
- 修复 `asyncio.gather` 未用 `return_exceptions=True` 导致单协程崩溃拖垮整个进程
- 新增 Supervisor 守护（`startretries=999`，崩溃后约 5 秒重启）
- 新增 `SIGTERM/SIGINT` 优雅退出处理（等待进行中的卖出完成）
- Supervisor 自动 log rotate（20MB/份，保留 5 份）

---

### v2.1（2026-09-01）

- KOL 名单评分体系：引入 30d 胜率加权评分（`KOL_DEFAULT_WINRATE_SCORE=12.5`）
- 仓位积分制：基础仓位 + 各维度加分 + 等级硬上限（`BASE_POSITION` / `MAX_POSITION`）
- Deployer 黑名单持久化（`deployer_blacklist.json`）+ 持仓期间 deployer 出货检测
- 软黑名单解锁机制：止盈离场后需强信号解锁才能重入（S 级 ≥ 3 次 / A/B 级 KOL ≥ 3 人）
- 策略 W：5 分钟新地址验证窗口（GeckoTerminal 每 10s 轮询）+ 价格漂移保护（≥ 50% 放弃）
- DexScreener Boost 全量缓存（10 分钟刷新）

---

### v2.0（2026-08-28）

- 双轨信号源：WebSocket RPC（QuickNode）+ fomoapi WS，新 Pair 零延迟捕捉
- 三策略分离（A/B/W）+ `open_strategy` 字段记录入场策略
- 全局并发买入限制（最多 2 笔同时进行）+ 全局冷却（45s 间隔）
- 分级冷却期（止盈 0s / 普通止损 30min / 流动性崩溃 4h）
- Gas 档位管理（LOW/MEDIUM，pending 超时自动升级）
- 卖出超时余额兜底确认（两轮共 780s）
- ETH 价格双源（Binance API 主源 + DexScreener 备用，30s 缓存）

---

### v1.x（2026-08 早期）

- fomoapi 单轨信号源
- KOL 跟单（策略 A）+ 多人共买（策略 B）基础实现
- 统一止盈止损（未按策略分层）
- Binance Web3 安全审计 + 模拟卖出蜜罐检测（F1~F3）
- Clawby 多层过滤（F5~F8：安全/流动性/新币/SmartMoney）
- 防刷量检测（F9）+ Boost 评分（F10）
