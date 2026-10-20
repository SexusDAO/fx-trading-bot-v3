"""
daily_baby_task.py — 每日自动完成 Babylon BABY 广场创作者活动
任务清单：
  任务1：发布原创广场帖子（短篇 + 长篇各一篇）
  任务2：买入 $10 BABY（完成后卖出，资金原路返回合约）

调度：每天 09:00 UTC 由 openclaw cron baby-daily-task 自动触发
成功静默（TASK_OK → HEARTBEAT_OK），失败才推送通知
活动截止：2026-08-06
"""

import sys, os, json, time, datetime, requests
sys.path.insert(0, "/root/.openclaw/workspace")
import binance_api as api

# ── 配置 ──────────────────────────────────────────────────────────────────────
SQUARE_KEY  = "42c42b7e6b594c168d4a66f45565a860"
SQUARE_URL  = "https://www.binance.com/bapi/composite/v1/public/pgc/openApi/content/add"
LOG_FILE    = "/root/.openclaw/workspace/daily_task_log.json"
BUY_AMOUNT  = 10.5     # 略多于 10U 确保任务达标
USDT_FLOOR  = 12.0     # 现货 USDT 低于此值才从合约补充
TRANSFER_IN = 15.0     # 每次从合约划转的金额
POST_DELAY  = 8        # 两篇帖子之间间隔（秒），避免限流

# ── 短篇帖子库（13 篇，按 UTC 日期轮换）────────────────────────────────────────
POSTS = [
    """Babylon Trustless Bitcoin Vaults (TBV) 让比特币真正参与到 PoS 链的安全共识中，无需跨链桥，无需托管，BTC 始终在比特币主链上。这是比特币扩展性的一次重大突破。

$BABY 作为协议核心代币，随着接入 Babylon 的 PoS 链数量增加，其使用场景持续扩大。关注 TBV 进展，也是在关注整个 Bitcoin DeFi 的演进方向。

@BabylonLabs_io #baby""",

    """很多人问 Babylon TBV 和传统 BTC 质押有什么区别？核心在于：TBV 通过比特币原生脚本实现时间锁定，不需要将 BTC 转移给任何第三方，安全性由比特币网络本身保障。

$BABY 持有者能参与协议治理，也能从质押奖励分配中受益。随着机构关注度提升，这个赛道值得长期跟踪。

@BabylonLabs_io #baby""",

    """Babylon TBV 的设计哲学是"比特币安全，无需信任"。通过将 BTC 的经济安全引入 PoS 链，Babylon 正在构建一个去中心化的安全层。

$BABY 代币在这个生态中扮演关键角色——从激励质押者到治理协议参数。如果 Babylon 生态持续扩张，$BABY 的价值捕获潜力不可忽视。

@BabylonLabs_io #baby""",

    """今天来聊聊 Babylon TBV 的技术细节。比特币脚本天然支持时间锁（CLTV/CSV），TBV 利用这一特性让 BTC 在不离开主链的情况下完成"质押承诺"。

$BABY 是整个 Babylon 协议的血液，无论是 finality providers 的激励还是协议治理，都离不开它。看好这个方向的朋友可以持续关注。

@BabylonLabs_io #baby""",

    """Babylon Protocol 的 TBV 解决了一个长期存在的问题：如何让比特币持有者在不放弃资产控制权的前提下，为其他区块链提供安全背书。

$BABY 代币的长期价值与 Babylon 生态的采用率直接挂钩。目前已有多条 PoS 链接入，后续发展值得期待。

@BabylonLabs_io #baby""",

    """在 Web3 世界里，"信任最小化"是最难实现的目标之一。Babylon TBV 通过比特币脚本原生实现 BTC 质押，不依赖任何中间方，将信任最小化做到了极致。

$BABY 作为 Babylon 生态的治理与激励代币，是参与这个赛道最直接的方式。持续关注 TBV 的落地进展。

@BabylonLabs_io #baby""",

    """Babylon TBV 给 PoS 链提供了一种全新的安全来源——比特币的算力和经济安全。这不仅提升了 PoS 链的抗攻击成本，也让 BTC 持有者有了新的收益场景。

$BABY 在这个双向受益的生态中处于核心位置，长期逻辑清晰。看好 Babylon 团队的执行力。

@BabylonLabs_io #baby""",

    """今天复盘一下 $BABY 的价值逻辑：Babylon TBV 让 BTC 可以无托管参与 PoS 质押，解锁了比特币的"休眠价值"。随着协议成熟，质押的 BTC 总量有望持续增长。

$BABY 持有者不仅能参与治理，也能从协议增长中分享收益。这是我持续关注 Babylon 生态的核心原因。

@BabylonLabs_io #baby""",

    """比特币的安全性是整个加密世界中最高的，但长期以来这种安全性只服务于比特币自身。Babylon TBV 改变了这一现状，让 BTC 的经济安全可以"出租"给其他 PoS 链。

$BABY 代币是这个新市场的核心资产，随着需求方（PoS 链）增多，供需关系将持续改善。

@BabylonLabs_io #baby""",

    """Babylon TBV 协议已经在主网稳定运行，证明了"无托管 BTC 质押"的可行性。这是整个 Bitcoin Layer 生态的重要里程碑。

$BABY 的市场表现将越来越多地反映协议的实际使用数据。关注链上质押量、接入 PoS 链数量等核心指标，是判断 $BABY 价值的关键维度。

@BabylonLabs_io #baby""",

    """很多人低估了 Babylon TBV 的创新程度。它不是简单的跨链桥，而是利用比特币自身的脚本能力，在不修改比特币协议的前提下实现了质押功能。

$BABY 代币的长期价值建立在这个坚实的技术基础上。如果你相信比特币是最安全的区块链，那么 Babylon 就是让这种安全性产生更大价值的最佳路径之一。

@BabylonLabs_io #baby""",

    """进入 Babylon 生态最直接的方式就是关注 $BABY 和 TBV 的发展动态。TBV 让每个 BTC 持有者都能成为 PoS 链安全的贡献者，同时保持完全的资产控制权。

这种"参与即安全，安全即价值"的设计，是 Babylon 协议最吸引人的地方。长期看好 $BABY 的发展轨迹。

@BabylonLabs_io #baby""",

    """Babylon TBV 的核心创新：比特币持有者可以通过时间锁脚本，在不移动 BTC 的情况下，为 PoS 链的最终确定性提供担保。一旦出现恶意行为，BTC 会被自动罚没（slashing）。

这种机制让 $BABY 支撑的生态具备了真正的经济安全性。随着更多 PoS 链接入，Babylon 的网络效应会持续增强。

@BabylonLabs_io #baby""",

    """$BABY 今日行情值得关注。Babylon 协议持续吸引机构级 BTC 持有者参与无托管质押，锁仓量稳步增长。

TBV 机制下，BTC 的经济安全正在为更广泛的 PoS 生态服务。这是比特币生态叙事的重要升级，$BABY 是直接受益者。

@BabylonLabs_io #baby""",

    """Babylon 的 finality providers 节点数量持续扩张，意味着更多参与者在竞争为 PoS 链提供安全服务。

这对 $BABY 是利好信号：更多节点竞争意味着更高的 $BABY 质押需求，也意味着整个协议的去中心化程度在提升。

@BabylonLabs_io #baby""",

    """比较一下 Babylon 和其他 BTC Layer2 方案：大多数 L2 需要信任某个多签委员会，而 TBV 的安全边界完全由比特币脚本保障。

这种"无需额外信任"的设计，让 Babylon 在竞争激烈的 BTC 扩展赛道中独树一帜。$BABY 持有者直接分享这个差异化优势。

@BabylonLabs_io #baby""",

    """Babylon 主网数据更新：质押 BTC 总量再创新高。每一笔新增质押，都在强化 Babylon 作为"比特币安全层"的市场地位。

$BABY 的价值来源于真实的协议使用，而不是纯粹的叙事炒作。这种基本面支撑，是长期持有的核心逻辑。

@BabylonLabs_io #baby""",
]

# ── 长篇帖子库（8 篇，按 UTC 日期轮换）─────────────────────────────────────────
LONG_POSTS = [
    """# Babylon Trustless Bitcoin Vaults（TBV）深度解析

## 什么是 TBV？

Babylon 的 Trustless Bitcoin Vaults（TBV）是一种无需信任的比特币质押机制，允许 BTC 持有者在不将资产转移给任何第三方的情况下，为 Proof-of-Stake（PoS）链提供安全担保。

## 技术原理

TBV 的核心是利用比特币原生脚本能力——具体是 CLTV（CheckLockTimeVerify）和 CSV（CheckSequenceVerify）时间锁——在比特币主链上创建一个"质押保险库"。持有者的 BTC 在设定的时间锁期间无法被转移，相当于完成了对某条 PoS 链的安全背书。

如果被质押的节点（Finality Provider）出现双签或其他恶意行为，比特币脚本会自动触发 slashing 机制，销毁对应的 BTC。这一切都发生在比特币链上，不依赖任何外部预言机或桥接合约。

## 为什么这很重要？

传统的 PoS 链安全性取决于本链代币的市值，容易受到攻击成本低的问题困扰。而 Babylon TBV 将比特币的经济安全性"借用"给 PoS 链，大幅提升了攻击成本。这是整个 Bitcoin Layer 2 和 Bitcoin DeFi 生态的重要基础设施。

## $BABY 代币的价值捕获

$BABY 是 Babylon 协议的核心代币，主要承担以下职能：
1. 协议治理：持有者可以投票决定关键参数，如 slashing 比例、手续费分配等
2. 质押激励：Finality Providers 需要质押 $BABY 参与共识，获取奖励
3. 生态费用结算：接入 Babylon 的 PoS 链支付的安全服务费，部分以 $BABY 计价

随着接入 Babylon 网络的 PoS 链数量增加，$BABY 的需求侧逻辑持续增强。长期持有 $BABY 相当于持有整个 Bitcoin Security-as-a-Service 市场的份额。

关注 Babylon 的进展，就是关注比特币生态扩展性的未来。$BABY @BabylonLabs_io #baby #BTC #DeFi""",

    """# 为什么说 Babylon 是 Bitcoin DeFi 的基础设施层？

## 比特币的"休眠价值"问题

比特币是加密世界市值最大、安全性最高的资产，但长期以来，持有 BTC 的最优策略只有一种：HODL。超过 70% 的 BTC 在过去 12 个月内没有发生移动，这意味着巨量的经济安全性处于"休眠"状态，没有为更广泛的 DeFi 生态贡献价值。

Babylon 的出现，正是为了激活这部分休眠的 BTC 价值。

## Babylon 如何解决这个问题？

Babylon Protocol 构建了一套无需信任的比特币质押基础设施。核心设计包括：

1. 无需跨链桥：BTC 永远不离开比特币主链，通过比特币原生脚本实现质押锁定，消除了桥接风险。

2. 可验证的 Slashing：如果验证节点作恶，惩罚逻辑直接在比特币链上执行，任何人都可以独立验证，无需信任第三方。

3. 模块化接入：任何 PoS 链都可以通过集成 Babylon 的 IBC/轻客户端模块，快速获得比特币的安全背书，无需从头构建安全基础设施。

## 市场空间有多大？

当前全球 PoS 链的市值合计超过 3000 亿美元，而它们的安全性（即本链质押的代币市值）普遍只有总市值的 20%-40%。Babylon 提供的"比特币安全即服务"直接面向这个市场。

即使只有 1% 的 BTC 参与 Babylon 质押，按当前市值计算也超过 100 亿美元的经济安全性注入 PoS 生态。这个数字足以彻底改变中小型 PoS 链的安全格局。

## $BABY 的投资逻辑

$BABY 的长期价值与 Babylon 网络的 TVL（锁定 BTC 总量）和接入链数量正相关。目前主网已稳定运行，多条头部 PoS 链完成接入，这为 $BABY 的价值增长提供了坚实基础。

如果你看好 Bitcoin DeFi 的长期叙事，$BABY 是参与这个赛道最直接的方式之一。 $BABY @BabylonLabs_io #baby #Bitcoin #Web3""",

    """# Babylon 主网数据表现与未来展望

## 主网现状

Babylon Protocol 主网正式上线以来，协议表现出了强劲的增长势头。核心指标包括质押 BTC 总量持续攀升、接入的 PoS 链数量稳步增加、以及 Finality Providers 节点数量的扩张。

这些数据共同反映出一个核心事实：市场对"无托管 BTC 质押"这一产品有真实的需求。

## 技术里程碑回顾

Babylon 团队在过去一年完成了多项关键里程碑：

Phase 1 封顶测试：首次开放 BTC 质押时，在极短时间内达到封顶上限，证明了市场需求的热度。

Phase 2 无上限质押：移除质押上限，允许 BTC 持有者自由参与，协议进入真正意义上的开放运营阶段。

多链接入：包括 Cosmos 生态和 EVM 兼容链在内的多条 PoS 链完成 Babylon 安全层集成，从单一测试场景扩展为真实的多链安全网络。

## $BABY 代币经济模型分析

$BABY 的代币分配设计注重长期可持续性：
- 质押奖励：为 BTC 质押者和 $BABY 质押者提供持续激励
- 生态发展基金：支持开发者生态和新链接入
- 团队和投资者份额：设有合理的解锁周期，避免短期抛压

从供需角度看，随着协议使用量增加，$BABY 的质押需求和治理需求会持续增长，中期供需关系有望持续改善。

## 风险提示

投资任何加密资产都存在风险。需要关注的风险包括：比特币脚本升级可能性、竞争协议的挑战、以及宏观市场的波动。理性评估风险，做好仓位管理，是参与这个赛道的前提。

$BABY @BabylonLabs_io #baby #Bitcoin #Staking""",

    """# 比较分析：Babylon TBV vs 传统 BTC 跨链桥

## 两种路径的本质区别

在 Babylon 出现之前，让 BTC 参与 DeFi 的主流方式是跨链桥：将 BTC 锁在某个多签或 MPC 托管合约里，然后在目标链上铸造一个"包装 BTC"。这种方式虽然实现了 BTC 的跨链流动，但引入了严重的信任假设。

Babylon TBV 选择了完全不同的路径——不移动 BTC，在比特币主链上直接实现质押语义。

## 安全性对比

**跨链桥方案：** 第三方托管（多签/MPC），攻击面包括桥合约漏洞、私钥泄露，历史上发生多起亿级黑客攻击。代表性事故：Ronin Bridge（6.24亿美元）、Wormhole（3.2亿美元）。

**Babylon TBV：** 自托管（时间锁脚本），攻击面仅限比特币脚本逻辑，无历史事故记录（基于比特币原生能力），只需信任比特币共识本身，无需额外信任任何第三方。

## 用户体验对比

跨链桥的用户体验相对更直观——拿到包装 BTC 后可以在 DeFi 中自由使用。Babylon TBV 目前的主要用途是为 PoS 链提供安全背书和赚取质押收益，用户体验更接近"定期存款"。

随着 Babylon 生态成熟，BTC 质押后的流动性凭证（Liquid Staking Token）有望解决这一限制，届时 TBV 的应用场景将大幅扩展。

## 结语

Babylon TBV 代表了 Bitcoin DeFi 的正确方向：不妥协安全性，不引入额外信任假设，用比特币自身的能力解决问题。这也是 $BABY 长期价值的核心支撑。 $BABY @BabylonLabs_io #baby #Bitcoin #Security""",

    """# Babylon 生态与 $BABY 代币：现状与展望

## 关键里程碑回顾

Babylon 协议从白皮书到主网上线，走过了一条扎实的技术路线。团队由斯坦福大学密码学教授 David Tse 领衔，学术背景深厚。

关键里程碑：
- 2023年：发布 Babylon Bitcoin Staking 白皮书
- 2024年初：完成 7000万美元 B 轮融资（Paradigm、Polychain 等参投）
- 2024年下半年：主网 Phase 1 上线，BTC 质押数小时内达到封顶上限
- 2025年：Phase 2 开放无上限质押，多链接入完成，$BABY TGE

## $BABY 当前处于什么阶段？

从代币生命周期来看，$BABY 目前处于"早期增长期"：协议已经过验证（主网稳定运行），但市场对 Babylon 的认知度和接入链数量还有大量提升空间。

这个阶段通常是参与价值最大的窗口期——基本面已经确立，但市值还没有完全反映协议的长期潜力。

## 未来关键看点

1. 接入 PoS 链数量：每新增一条，$BABY 安全服务需求就增加一份
2. Liquid Staking Token 推出：将大幅提升 TVL 和使用便捷性
3. 机构参与：灰度、贝莱德等 BTC ETF 若开放质押，将带来巨大增量
4. 生态应用扩展：基于 Babylon 安全层的新 DeFi 应用持续上线

$BABY @BabylonLabs_io #baby #Bitcoin #Crypto2025""",

    """# $BABY 质押经济学：为什么 Finality Provider 需要质押 $BABY？

## Finality Provider 的角色

在 Babylon 协议中，Finality Provider（FP）是连接比特币安全和 PoS 链的关键角色。FP 需要同时质押 BTC 和 $BABY，才能参与共识并获得收益。

这种双重质押设计并非偶然——它确保了 FP 在行为上真正对齐协议利益：
- BTC 质押：提供经济安全的"硬背书"，一旦作恶立即被 slash
- $BABY 质押：表达对协议长期发展的信心，同时参与治理权重计算

## $BABY 质押的经济激励

从供需角度分析：
- **需求侧**：每新增一个 FP，就需要锁定一定量的 $BABY；随着更多 PoS 链接入，FP 数量持续扩张
- **供给侧**：$BABY 有合理的解锁周期，流通量受控

随着协议规模扩大，FP 对 $BABY 的需求会呈现结构性增长，这是 $BABY 中长期价值的重要支撑。

## 普通持有者的参与路径

即使不运营 FP 节点，普通 $BABY 持有者也可以通过以下方式参与：
1. 参与治理投票：决定关键协议参数
2. 委托给 FP：获取委托奖励分成
3. 持有等待生态成熟：随协议 TVL 增长分享价值上涨

Babylon 正在构建的，是比特币安全性的货币化市场。$BABY 是进入这个市场的门票。

$BABY @BabylonLabs_io #baby #Bitcoin #Staking #DeFi""",

    """# 从技术角度看 Babylon 的护城河

## 比特币脚本的独特优势

Babylon 的核心技术建立在比特币脚本之上，这是一个极难被复制的护城河。原因在于：

1. **不需要比特币协议升级**：TBV 完全利用现有的比特币脚本能力（CLTV/CSV），无需 fork 或等待软/硬升级，可以立即部署。

2. **最小信任假设**：整个质押-slash 逻辑都在比特币链上执行，连 Babylon 团队自己也无法干预已经锁定的 BTC。

3. **可组合性**：BTC 质押后产生的凭证可以作为其他 DeFi 协议的抵押品，为整个生态解锁流动性。

## 竞争格局分析

目前 BTC 质押赛道的竞争者主要分三类：
- **中心化托管**（如 WBTC）：依赖信任，有单点故障风险
- **MPC 多签**：减少但未消除信任假设，历史上有攻击先例  
- **Babylon TBV**：无需信任，完全链上验证，护城河最深

从技术竞争力来看，Babylon 处于第一梯队，且先发优势明显。

## 对 $BABY 估值的影响

如果 Babylon 在 BTC 无托管质押这个赛道建立起事实标准，$BABY 作为协议核心资产的溢价将逐步体现。这不是短期炒作，而是基于技术护城河的长期价值积累。

$BABY @BabylonLabs_io #baby #Bitcoin #DeFi #Web3""",

    """# Babylon 生态的机构级机会

## 机构为什么关注 Babylon？

传统机构持有大量 BTC（ETF、企业金库），但这些 BTC 一直处于"纯持有"状态，无法产生收益。Babylon TBV 提供了一条符合机构风控要求的收益路径：

- **无需转移 BTC**：资产始终在比特币链上，符合托管规范
- **可验证的风险边界**：slash 条件和概率完全透明，可以精算
- **收益来源清晰**：PoS 链安全服务费，不是庞氏结构

这对于持有数十亿美元 BTC 的机构来说，是一个全新的资产管理维度。

## 机构参与对 $BABY 的影响

一旦灰度、贝莱德等机构开始引导其 BTC ETF 参与 Babylon 质押：
1. 质押 BTC 总量将出现数量级增长
2. 对 Finality Provider 和 $BABY 质押的需求随之爆发
3. $BABY 的机构认知度将大幅提升，流动性溢价显现

这不是"如果"，而是"何时"的问题。监管环境改善、链上基础设施成熟，机构入场的条件正在逐步具备。

## 现在布局的意义

在机构大规模入场之前，散户参与者仍然有先发优势。了解 Babylon 协议机制，持有 $BABY，就是在押注这个机构化进程。

$BABY @BabylonLabs_io #baby #Bitcoin #Institutional #DeFi""",
]


def get_daily_post() -> str:
    """按 UTC 日期选短篇帖子（循环）"""
    day = datetime.datetime.utcnow().timetuple().tm_yday
    return POSTS[day % len(POSTS)]


def get_daily_long_post() -> str:
    """按 UTC 日期选长篇帖子（循环）"""
    day = datetime.datetime.utcnow().timetuple().tm_yday
    return LONG_POSTS[day % len(LONG_POSTS)]


# ── 核心函数 ──────────────────────────────────────────────────────────────────

def transfer_from_futures_if_needed() -> tuple[bool, str]:
    """现货 USDT 不足时从合约划转"""
    try:
        r = api.request("GET", api.BASE_API + "/api/v3/account")
        usdt_free = 0.0
        for b in r.get("balances", []):
            if b["asset"] == "USDT":
                usdt_free = float(b["free"])
                break

        if usdt_free >= USDT_FLOOR:
            return True, f"现货 USDT 充足: ${usdt_free:.2f}"

        result = api.request("POST", api.BASE_API + "/sapi/v1/asset/transfer", {
            "type": "UMFUTURE_MAIN",
            "asset": "USDT",
            "amount": str(TRANSFER_IN),
        })
        if "tranId" in result:
            return True, f"划转 {TRANSFER_IN}U 成功 (tranId: {result['tranId']})"
        return False, f"划转失败: {result}"
    except Exception as e:
        return False, f"划转异常: {e}"


def buy_baby() -> tuple[bool, str]:
    """买入约 $10 的 BABY"""
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/ticker/price",
            params={"symbol": "BABYUSDT"},
            proxies=api.PROXIES,
            timeout=10,
        )
        price = float(r.json()["price"])
        qty = max(1, int(BUY_AMOUNT / price))

        result = api.request("POST", api.BASE_API + "/api/v3/order", {
            "symbol": "BABYUSDT",
            "side": "BUY",
            "type": "MARKET",
            "quantity": str(qty),
        })
        if "orderId" in result:
            cost = float(result.get("cummulativeQuoteQty", 0))
            return True, f"买入 {qty} BABY，成本 ${cost:.2f}"
        return False, f"买入失败: {result}"
    except Exception as e:
        return False, f"买入异常: {e}"


def sell_baby_back() -> tuple[bool, str]:
    """卖出所有 BABY，划转 USDT 回合约（重试查余额，避免固定等待过长）"""
    try:
        # 最多等 60 秒，每 5 秒查一次余额，直到 BABY 到账
        baby_free = 0.0
        for _ in range(12):
            time.sleep(5)
            r = api.request("GET", api.BASE_API + "/api/v3/account")
            for b in r.get("balances", []):
                if b["asset"] == "BABY":
                    baby_free = float(b["free"])
            if baby_free >= 1:
                break

        if baby_free < 1:
            return False, "BABY 余额不足，跳过卖出（等待超时）"

        sell_qty = int(baby_free)
        result = api.request("POST", api.BASE_API + "/api/v3/order", {
            "symbol": "BABYUSDT",
            "side": "SELL",
            "type": "MARKET",
            "quantity": str(sell_qty),
        })
        if "orderId" not in result:
            return False, f"卖出失败: {result}"

        recv = float(result.get("cummulativeQuoteQty", 0))

        # 查最终 USDT 余额并划转回合约
        time.sleep(3)
        r2 = api.request("GET", api.BASE_API + "/api/v3/account")
        usdt_total = sum(
            float(b["free"]) for b in r2.get("balances", []) if b["asset"] == "USDT"
        )

        transfer_amt = round(usdt_total - 0.05, 2)
        if transfer_amt > 0.5:
            tr = api.request("POST", api.BASE_API + "/sapi/v1/asset/transfer", {
                "type": "MAIN_UMFUTURE",
                "asset": "USDT",
                "amount": str(transfer_amt),
            })
            if "tranId" in tr:
                return True, f"卖出 {sell_qty} BABY 回收 ${recv:.2f}，划转 ${transfer_amt} 回合约"
            return True, f"卖出 {sell_qty} BABY 回收 ${recv:.2f}，划转失败: {tr}"

        return True, f"卖出 {sell_qty} BABY 回收 ${recv:.2f}，余额过小跳过划转"
    except Exception as e:
        return False, f"卖出/划转异常: {e}"


def post_to_square(content: str, label: str = "帖子") -> tuple[bool, str]:
    """发布广场帖子（短篇和长篇共用，带代理）"""
    try:
        r = requests.post(
            SQUARE_URL,
            headers={
                "X-Square-OpenAPI-Key": SQUARE_KEY,
                "Content-Type": "application/json",
                "clienttype": "binanceSkill",
            },
            json={"bodyTextOnly": content},
            proxies=api.PROXIES,
            timeout=20,
        )
        data = r.json()
        if data.get("code") == "000000":
            post_id = data.get("data", {}).get("id", "")
            url = f"https://www.binance.com/square/post/{post_id}"
            return True, f"{label}发布成功: {url}"
        return False, f"{label}发布失败: code={data.get('code')} msg={data.get('message')}"
    except Exception as e:
        return False, f"{label}发布异常: {e}"


def save_log(results: list[tuple[str, bool, str]]):
    """写入执行日志到 daily_task_log.json"""
    try:
        log_entry = {
            "date": datetime.datetime.utcnow().strftime("%Y-%m-%d"),
            "time_utc": datetime.datetime.utcnow().strftime("%H:%M"),
        }
        for name, ok, msg in results:
            log_entry[name] = ("✅ " if ok else "❌ ") + msg

        # 读旧日志（保留最近 30 天）
        history = []
        if os.path.exists(LOG_FILE):
            try:
                with open(LOG_FILE) as f:
                    existing = json.load(f)
                    history = existing if isinstance(existing, list) else [existing]
            except Exception:
                pass

        history.append(log_entry)
        history = history[-30:]  # 只保留最近 30 条

        with open(LOG_FILE, "w") as f:
            json.dump(history, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[日志写入失败] {e}")


# ── 主流程 ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    now_str = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    print(f"=== Babylon BABY 每日任务 {now_str} ===")

    results: list[tuple[str, bool, str]] = []

    # Step 1: 确保现货有足够 USDT
    ok, msg = transfer_from_futures_if_needed()
    print(f"[资金] {msg}")
    results.append(("资金准备", ok, msg))
    if not ok:
        save_log(results)
        print("TASK_FAILED: 资金准备失败")
        sys.exit(1)

    time.sleep(2)

    # Step 2: 买入 BABY（任务2：交易积分）
    ok, msg = buy_baby()
    print(f"[交易-买入] {msg}")
    results.append(("交易BABY", ok, msg))

    # Step 3: 发布短篇帖子（任务1）
    ok, msg = post_to_square(get_daily_post(), label="短篇帖子")
    print(f"[发帖-短篇] {msg}")
    results.append(("广场短篇", ok, msg))

    # Step 4: 间隔后发布长篇帖子（任务1 长篇加分）
    time.sleep(POST_DELAY)
    ok, msg = post_to_square(get_daily_long_post(), label="长篇帖子")
    print(f"[发帖-长篇] {msg}")
    results.append(("广场长篇", ok, msg))

    # Step 5: 卖出 BABY 并划转回合约
    ok, msg = sell_baby_back()
    print(f"[交易-卖出] {msg}")
    results.append(("卖出划转", ok, msg))

    # 写入执行日志
    save_log(results)

    # 汇总输出（供 cron agent 判断是否推送）
    all_ok = all(r[1] for r in results)
    print(f"\n{'TASK_OK' if all_ok else 'TASK_PARTIAL'}")
    for name, ok, msg in results:
        print(f"  {'✅' if ok else '❌'} {name}: {msg}")
