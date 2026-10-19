# 🤖 Robinhood Chain AI 打狗机器人

> 基于 **Clawby DexScreener + 币安 Web3 Audit API + baw CLI** 构建的完整链上 Meme 代币交易工作流。
> 无需暴露私钥，所有交易通过 Binance Agent Wallet 执行。

---

## 🏗️ 整体架构

```
[Clawby DexScreener]          [币安 Web3 Audit API]        [baw CLI]
  token_profiles 轮询   →    安全风控一票否决        →    market-order swap
  token_pairs 行情数据        买税/卖税/蜜罐/黑名单        limit-order 止盈止损

         ↓                          ↓                          ↓
    [信号层]  →  [AI评分筛选]  →  [安全风控]  →  [执行层]  →  [监控层]  →  [退出层]
```

---

## 💡 核心优势

| 维度 | 传统 Sniper Bot | 本方案 |
|------|---------------|--------|
| 私钥安全 | 明文私钥签名 | ✅ baw 托管，零私钥暴露 |
| 新币发现 | 监听 RPC PairCreated | ✅ Clawby DexScreener 30s 轮询 |
| 安全检测 | 手动调 GoPlus | ✅ 币安 Audit API 原生支持链 4663 |
| 止盈执行 | 自己写循环监控 | ✅ 市值驱动自动触发，三套止盈阶梯 |
| 通知推送 | 需额外搭 TG Bot | ✅ 直接通过 Jarvis/OpenClaw 推送 |

---

## 📁 文件结构

```
robinhood_sniper/
├── config.py      ← ⭐ 所有参数改这里，不需要动其他文件
├── signal.py      ← 信号层：Clawby DexScreener 轮询新代币
├── filter.py      ← 筛选层：AI 打分 + 安全审计一票否决
├── executor.py    ← 执行层：baw 买入 / 报价 / 挂止盈止损单
├── monitor.py     ← 监控层 + 退出层：市值止盈 / 止损 / 清仓
├── main.py        ← 主入口：asyncio 三协程并发
├── sniper.log     ← 运行日志（自动生成）
└── README.md      ← 本文档
```

---

## ⚡ 快速启动

### 前置条件

1. **Agent 钱包（Robinhood Chain）有 ETH** — 需先跨链充值
   ```
   Robinhood Chain Agent 钱包地址：0x284D2c9B296003EF57264fdFf571C3083FA46152
   跨链路径：BSC BNB → Relay.link → Robinhood ETH（已验证可用）
   ```

2. **依赖安装**
   ```bash
   pip install requests
   ```

### 启动

```bash
cd /root/.openclaw/workspace
python3 -m robinhood_sniper.main
```

### 停止

`Ctrl+C` — 已挂的限价单不受影响，继续在链上有效。

---

## ⚙️ 参数配置（config.py）

### 信号层

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `POLL_INTERVAL_SEC` | 30 | 新币发现轮询间隔（秒） |
| `MAX_TOKEN_AGE_MIN` | 60 | 忽略超过 N 分钟的代币 |

### 筛选层（AI 打分）

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `MIN_SCORE` | 5 | 最低进入风控的分数（满分 12） |
| `MIN_LIQUIDITY_USD` | $5,000 | 流动性门槛 |

**评分规则：**

| 条件 | 分值 |
|------|------|
| 流动性 ≥ $5,000 | +2 |
| 代币 < 10 分钟（超早期） | +3 |
| 代币 10-30 分钟 | +1 |
| 5min 买单 > 卖单且 > 5 笔 | +2 |
| 1h 买入 > 50 笔 | +1 |
| 24h 成交量 ≥ $10,000 | +1 |
| 有项目描述 | +1 |
| 有社交链接 | +1 |
| 5min 价格涨 > 5% | +1 |

### 风控层（一票否决）

| 否决条件 | 说明 |
|---------|------|
| `riskLevelEnum = HIGH` | 币安审计高风险 |
| `isFlaggedByVendor` | 被安全供应商标记 |
| `isBlacklist` | 黑名单代币 |
| `buyTax > 10%` | 买税过高 |
| `sellTax > 10%` | 卖税过高（防止无法卖出） |

### 执行层

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `BASE_POSITION_RATIO` | 2% | 每笔仓位占可用余额比例 |
| `MAX_POSITION_ETH` | 0.5 | 单笔最大 ETH 上限 |
| `SLIPPAGE_NORMAL` | 20% | 普通行情滑点 |
| `SLIPPAGE_HOT` | 49% | 热门代币滑点（m5 买单 > 20 笔） |

---

## 📊 止盈策略（三套阶梯，按市值触发）

> 核心原则：**先回本，再谈利润。市值不是唯一指标，结合成交量萎缩 + 大户出货综合判断。**

### 🐕 普通狗币（`token_type = "normal"`）

$100万开始分批，$1000万基本清完：

| 市值里程碑 | 卖出比例 | 累计卖出 |
|-----------|---------|---------|
| $100万    | 20%     | 20%     |
| $300万    | 25%     | 45%     |
| $500万    | 20%     | 65%     |
| $1000万   | 25%     | 90%     |
| 剩余 10%  | 玩家仓位 | —       |

### 🔥 热点叙事狗币（`token_type = "narrative"`）
AI+Dog、链上 IP 联名等，叙事加持可拿更久：

| 市值里程碑 | 卖出比例 | 累计卖出 |
|-----------|---------|---------|
| $500万    | 20%     | 20%     |
| $1000万   | 20%     | 40%     |
| $3000万   | 30%     | 70%     |
| $1亿      | 25%     | 95%     |
| 剩余 5%   | 玩家仓位 | —       |

### 🚀 平台背书（`token_type = "platform"`）
交易所点名、大V全力喊，溢价空间更大：

| 市值里程碑 | 卖出比例 | 累计卖出 |
|-----------|---------|---------|
| $1000万   | 20%     | 20%     |
| $5000万   | 30%     | 50%     |
| $1亿      | 30%     | 80%     |
| $3亿      | 15%     | 95%     |
| 剩余 5%   | 博取更高 | —       |

### 💡 止盈三原则（已内置）

1. **先回本** — 2x 时自动卖出 50%，剩余全是"免费筹码"
2. **成交量衰减预警** — 1h 成交量较峰值下降 60% 时推送警报
3. **不等最高点** — 按市值里程碑纪律性执行，不依赖主观判断

---

## 🛑 止损策略

| 触发条件 | 动作 |
|---------|------|
| 亏损 > 35%（`STOP_LOSS_PCT`） | 立即市价清仓 |
| 流动性下降 > 50%（`LIQUIDITY_DROP_STOP`） | 紧急清仓（疑似撤池） |
| 持仓超过 12h（`MAX_HOLD_HOURS`） | 超时强制清仓 |

---

## 🔧 代币类型设置

买入时默认使用 `normal` 阶梯。如需更改，在 `process_token()` 里设置 `token_type`，
或在 `config.py` 改 `DEFAULT_TOKEN_TYPE`：

```python
# config.py
DEFAULT_TOKEN_TYPE = "narrative"  # 当前市场叙事驱动时改这里
```

---

## 🔗 链上常量（Robinhood Chain）

| 参数 | 值 |
|------|----|
| Chain ID（baw） | `4663` |
| Chain ID（DexScreener） | `robinhood` |
| RPC | `https://rpc.mainnet.chain.robinhood.com` |
| Explorer | `https://robinhoodchain.blockscout.com` |
| 原生 ETH 地址 | `0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee` |
| WETH 合约 | `0x0bd7d308f8e1639fab988df18a8011f41eacad73` |
| Agent 钱包 | `0x284D2c9B296003EF57264fdFf571C3083FA46152` |

---

## 🌉 资金路径（BSC → Robinhood Chain）

Robinhood Chain 是 ETH 的 L2，BSC USDT 无法直接跨链。已验证路径：

```
① BSC USDT → BSC BNB
   baw market-order swap --binanceChainId 56

② BSC BNB → Robinhood Chain ETH（via Relay.link）
   baw contract-call execute
   合约: 0x4cd00e387622c35bddb9b4c962c136462338bc31（BSC Relay Depository）

③ Robinhood Chain ETH → 可直接用于打狗 ✅
```

---

## ⚠️ 风险提示

1. **打狗高风险**，每笔默认 2% 仓位，严格控制单笔损失上限
2. **新币发现延迟**约 10-30 秒，不适合极速抢跑场景
3. 止损单无法保证在极端流动性下完全成交
4. 本程序不构成投资建议，使用者自担风险

---

## 📜 License

MIT
