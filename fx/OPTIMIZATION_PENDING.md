# 待实现优化与技术债清单

> 分支 `feature/passive-trigger`，基线 `3234320f`（2026-10-22 整理）。
> 只记录讨论过但未进入任何 commit 的事项，以及已有开关但默认关闭（暂缓）的优化。
> 行号以基线为准，修改前请重新核对。

---

## 一、缺陷与技术债（优先处理）

## [P-01] 标的注册锁范围过大，下单期间同标的替换信号被丢弃
- **来源**：`fdde5ff4`（signal_watcher 重构【3】）引入；排查 `test_trigger_identity` 时确认，`3234320f` 在测试文件头记录
- **问题**：`_trigger` 调用 `on_trigger`（下单）时持有该标的注册锁；下单期间同标的 `add_signal(replace=True)` 等锁 5 秒后放弃，`_trigger` 结束时移除旧条目，监控队列里没有该标的。`tests/test_trigger_identity.py::test_finally_does_not_pop_replacement_entry` 因此失败（KeyError）。线上影响：下单中同标的的替换/重挂信号被丢弃，该标的暂时无挂单监控；executor 下单越慢越容易发生
- **方案**：下单时不持注册锁，只在"检查并写入监控队列"那一小段加锁；或改为允许同一任务重入；同时给该测试补交易所查询 mock
- **涉及文件**：`shared/signal_watcher.py`、`tests/test_trigger_identity.py`
- **依赖**：无
- **状态**：待实现

## [P-02] add_signal / _trigger 中的同步 IO 与串行网络请求拖慢事件循环
- **来源**：用户提出；`fdde5ff4`（交易所冲突检查）、`7b2ae3e1`（盘口粗筛、冷却前置）引入相关调用
- **问题**：
  - `add_signal` 里 `state.is_in_cooldown` 是同步的文件读写 + 线程锁，直接在事件循环里执行
  - 注册前依次 await 标记价格、盘口、持仓、挂单 4 个请求（虽在线程池执行，但串行，单次注册耗时叠加）；默认线程池可能与其他任务争用
  - `_trigger` 里 `_get_hub_or_rest(symbol, "mark")` 是同步调用，hub 数据过旧时会退回 REST，直接阻塞事件循环
  - `_check_sl_breach` 每 30 秒同步调用 `state.get_all_positions()`，在价格回调里执行
- **方案**：同步文件读写和 REST 回退统一改走有界线程池（参考 `ws_listener` 的 `_run_sync`）；注册前的几个查询改为并发（`asyncio.gather`）并设总超时；持仓缓存改为后台定时刷新
- **涉及文件**：`shared/signal_watcher.py`
- **依赖**：建议与 P-01 一起改（都涉及注册/下单路径）
- **状态**：待实现

## [P-03] 冷缓存时注册阶段清算区为空，止损降级为 ATR
- **来源**：`cb792429`（`fetch_if_missing=False`）
- **问题**：后台刷新周期与缓存有效期都是 15 分钟，刷新稍有延迟或标的刚进池时缓存过期，注册阶段拿到空清算区，止损止盈改用 ATR 公式；794 秒卡顿是否卡在这 4 处调用未验证
- **方案**：已在 scanner 注册阶段 4 处清算区调用加耗时日志 `[liq-timing] {symbol} fetch_liq_zones 耗时 {ms}ms 结果=有数据/空`；上线后按日志统计"空"占比与耗时，再决定是否放宽缓存有效期到刷新周期的 1.5~2 倍
- **涉及文件**：`scanner.py`（已加日志）；后续可能改 `shared/liq_cache.py`
- **依赖**：上线日志
- **状态**：日志已加，待观察数据
## [P-04] binance_api 签名接口没有代理主备切换
- **来源**：`3852aa1b`（clawby_adapter 主备切换）、`d43efc6e`（只做了 listenKey）
- **问题**：`BINANCE_PROXY` 改成日本节点后，下单、行情等交易所请求也走它，节点断了不会切到备用；只有 listenKey 两个函数有切换
- **方案**：`_SESSION` 请求统一加连接失败切 `PROXY_URL_BACKUP` 重试一次的逻辑；写请求（下单）需区分"未送达"和"结果未知"，结果未知时不能重试
- **涉及文件**：`shared/binance_api.py`
- **依赖**：备用节点出口 IP 需加入 API key 白名单
- **状态**：待实现

## [P-05] 网关编辑消息参数可能错误
- **来源**：`36c88c25`（通知去重）排查时发现
- **问题**：`shared/notifier.py` 的 `_http_edit` 传 `"action": "send"` 加 `messageId`，若网关按发新消息处理，编辑时仍会多出一条通知；Socket 编辑只要发出就返回成功，不看回复
- **方案**：确认网关编辑接口的正确参数后修正，Socket 编辑改为检查回复
- **涉及文件**：`shared/notifier.py`
- **依赖**：需要网关接口文档
- **状态**：暂缓（等确认网关参数）

## [P-06] 进程重启后已有信号的后续通知丢失
- **来源**：`36c88c25`（编辑失败/无卡片/无消息 ID 一律丢弃）
- **问题**：进程重启后卡片缓存缺失，已注册信号的开仓、止损等进展不再推送
- **方案**：卡片缓存按 sig_id 持久化并在启动时恢复；或对"无卡片"场景单独发一条带标的的摘要消息（需权衡"一个信号一条通知"原则）
- **涉及文件**：`shared/notifier.py`
- **依赖**：P-05
- **状态**：暂缓（先观察重启频率）

## [P-07] 卡片后台任务在测试结束时未完成告警
- **来源**：`a7184c09`（通知改为后台任务）
- **问题**：`_trigger` 中投递卡片的后台任务在测试/进程退出时可能仍挂起，产生"task was destroyed but it is pending"告警；卡片顺序可能错乱（已接受）
- **方案**：保存任务引用，退出时统一等待或取消
- **涉及文件**：`shared/signal_watcher.py`
- **依赖**：无
- **状态**：待实现（低优先级）

## [P-08] 名义价值计算单位错误，流动性阈值随币价缩放
- **来源**：`7b2ae3e1`（高优先级修复【2】【3】）
- **问题**（已核对，`_signal_notional` 实际在 `executor.py`，不在 signal_watcher）：
  - `pre_params` 来自 `calc_sl_tp` 返回值，只有 entry_price / sl_price / tp1_price / tp2_price / rr / 降级标记等，**没有 `notional`，也没有 `position_size`**
  - `ind` 没有 `mark_price`，但有 `markPrice`（scanner 分析阶段写入），代码两个键都读，所以能取到价格
  - 结果：第二步 `position_size` 缺失时取默认值 100 → 名义价值 = **100 × 标记价格**，不是 100U 硬底；只有 markPrice 为 0 时才落到 100U
  - 影响：BTC（约 6 万）名义价值被算成约 600 万 U，盘口粗筛要求 ±0.5% 深度 ≥ 1200 万 U，大概率被误拦；低价币（<1U）名义价值 < 100U，阈值实际由 3000U 硬底决定
- **方案**（已实现）：scanner 拿不到真实仓位（executor 下单时才算），在 `pre_params` 写入 `notional = MIN_NOTIONAL_GUARD`（100U），粗筛门槛 = 100 × 2 = 200U；精确深度检查仍由 executor 下单前完成。未采用"scanner 读余额估算仓位"方案（需复刻 executor 公式，改动不值当）
- **涉及文件**：`scanner.py`（已改）；`executor.py` 未改
- **依赖**：无
- **状态**：已修复（粗筛降为 200U 门槛，只拦盘口极薄的币）
## [P-09] oi_divergence 存活检查的 OI 基准是"扫描前 1 小时"而非"触发前"
- **来源**：`fdde5ff4`（【9】OI 恢复撤单）
- **问题**（已核对）：scanner 分析阶段会写入 `ind["oi_1h_ago"]`（预取的 OI 历史第 1 条；另一条分析路径同样写入），所以 oi_divergence 信号带有这个字段，存活检查不是只按价格撤单。但字段含义是"扫描时往前 1 小时的 OI"，与注释里的"触发前水平"不完全等价；OI 本就在该值 ±1% 内时，注册后很快就会被判"已恢复"撤单
- **方案**：观察 `oi_divergence OI 已恢复到触发前水平` 撤单日志的频率与注册到撤单的时长；若过早撤单，改为注册时记录当时 OI 作为基准
- **涉及文件**：`scanner.py`、`shared/signal_watcher.py`
- **依赖**：上线日志
- **状态**：已核对，字段存在；暂缓（先看撤单日志）
## [P-10] 漏斗统计不完整
- **来源**：`afd4506c`（漏斗统计）
- **问题**：`run_scan` 有 5 个提前返回分支（熔断等）不写漏斗记录；chg24、熊市禁止、资金费率等原因都归入"其他"，占比可能偏大
- **方案**：跑几轮真实数据后，按"其他"中高频原因补关键词规则；提前返回分支补一条带 `skipped_reason` 的记录
- **涉及文件**：`scanner.py`
- **依赖**：需要部署后的漏斗数据
- **状态**：暂缓（用户决定先观察数据）

## [P-11] 热重载不校验数值范围
- **来源**：`afd4506c`（配置热重载）
- **问题**：`config_tunable.py` 语法错误会保留旧值，但不合理的数值（阈值写错、负数）会直接生效；`PULLBACK_SHORT_VOL_RATIO_MIN`、`PULSE_CANDLE_VOL_RATIO_1H/4H`、`SCAN_TOTAL_TIMEOUT_SEC`、`SCAN_MAX_WORKERS` 在模块加载时绑定，热重载不生效
- **方案**：为可热更新参数定义类型/范围表，越界时拒绝并记 WARNING；需要热更新的加载时绑定参数改为调用时现读
- **涉及文件**：`shared/hot_config.py`、`config_tunable.py`；后者涉及 `shared/judges.py`、`scanner.py`
- **依赖**：无
- **状态**：暂缓（用户决定使用时人工注意）

## [P-12] userData listenKey 失效状态码未在真实环境确认
- **来源**：`afd4506c`（userData 无数据验证）
- **问题**：按 4xx（除 418/429）判定 listenKey 失效；Binance 对失效 key 的实际返回（预期 400 / -1125）未确认；`get_listen_key` 主备都失败时外层 `_retry` 会叠加重试，耗时成倍增加
- **方案**：部署后看日志确认状态码；必要时按错误码而非状态码判断；限制 listenKey 获取的总耗时
- **涉及文件**：`shared/ws_listener.py`、`shared/binance_api.py`
- **依赖**：部署日志
- **状态**：暂缓（部署后确认）

## [P-13] 测试套件问题
- **来源**：多次回归中发现
- **问题**：
  - 已新建 `fx/pytest.ini` 固定回归范围（原手动"扩大回归组"17 个文件），`cd fx && python3 -m pytest` 即可
  - `test_shutdown.py`、`test_task_exception.py` 收集阶段报 StopIteration，已在 pytest.ini 中排除，问题本身未修
  - `test_phase5_dispatch.py` 整文件跑会卡住，仍靠文件内 skip（`FX_RUN_PHASE5=1` 启用）
  - `test_config_fallback_signal_watcher.py` 与 `test_exit_signal_and_direction.py` 同跑报"no current event loop"，不在固定范围内
  - 已加 `network` 标记并默认排除，但现有联网测试尚未打标记，仍靠 `tests/conftest.py` 按文件补 mock
- **方案**：逐个修复收集失败与事件循环问题后加回 testpaths；给联网测试补 `@pytest.mark.network`
- **涉及文件**：`pytest.ini`（已建）、`tests/` 下相关文件
- **依赖**：无
- **状态**：部分完成（固定范围已建），其余待实现
## [P-14] stop_hunt 仓位上限写死 0.1，没有观察期退出机制
- **来源**：`36c88c25` 后的 judges 代码阅读
- **问题**：`shared/judges.py` 第 3090 行 `pos_cap = 0.1`，注释称"观察期，积累胜率数据"，但没有结束条件、没有胜率统计，也不会提升；函数外是否再被修改未查；`STOP_HUNT_ENABLED=False`（胜率约 9%）
- **方案**：先查 executor/仓位计算是否覆盖该值；如需恢复，增加按历史胜率与样本数提升仓位的规则
- **涉及文件**：`shared/judges.py`、可能涉及 `executor.py`
- **依赖**：需要实盘胜率数据
- **状态**：暂缓（策略本身已关闭）

---

## 二、已有开关但默认关闭的优化（暂缓）

> 代码已在 `config.py` 中，开关默认 False。暂缓原因引自 config.py 注释（v3.87 合并决策 / 评审结论）。

## [OPT-1] 停滞平仓按止损距离缩放（`OPT_STAGNATION_SCALE_BY_SL`）
- **来源**：v3.87 合并决策
- **问题**：early_stagnation 判断未考虑止损宽度
- **方案**：开关已实现
- **涉及文件**：`config.py`（开关）、`position_manager.py`
- **依赖**：无
- **状态**：暂缓（先看 early_stagnation 频率再决定）

## [OPT-2] TP1 后保护性平仓（`OPT_TP1_PROTECT_CLOSE`）
- **来源**：v3.87 合并决策
- **问题**：TP1 后回撤保护
- **方案**：开关已实现
- **涉及文件**：`config.py`、`position_manager.py`
- **依赖**：BUG-1/2 修复跑稳
- **状态**：暂缓（BUG-1/2 跑稳约 1 周后再开）

## [OPT-3] 回踩入场多空对称（`OPT_PULLBACK_ENTRY_SYMMETRIC`）
- **来源**：v3.87 合并决策
- **问题**：空头回踩入场为 0.8ATR，与多头不对称
- **方案**：开关已实现
- **涉及文件**：`config.py`、`shared/judges.py` / `shared/calc_sl_tp.py`
- **依赖**：无
- **状态**：暂缓（空头 0.8ATR 为有意设计，待数据）

## [OPT-4] 保本档推迟并加缓冲（`OPT_TRAIL_BE_BUFFER`）
- **开关映射**：OPT-4 = `OPT_TRAIL_BE_BUFFER`（保本档推迟并加缓冲）
- **来源**：v3.84 / v3.87 合并决策
- **问题**：保本止损上移过早
- **方案**：开关已实现
- **涉及文件**：`config.py`、`position_manager.py`
- **依赖**：无
- **状态**：暂缓（先观察单笔最大回撤）

## [OPT-7] 反转做空 RSI4H 门槛按波动层区分（`OPT_REVERSAL_SHORT_TIER_RSI`）
- **来源**：v3.87 合并决策
- **问题**：高波动标的做空 RSI 门槛统一
- **方案**：开关已实现（高波动 70）
- **涉及文件**：`config.py`、`shared/judges.py`
- **依赖**：无
- **状态**：暂缓（待对比高波动做空历史胜率）

## [OPT-8] 回踩在确认处入场（`OPT_PULLBACK_ENTRY_AT_CONFIRM`）
- **来源**：v3.87 合并决策
- **问题**：入场价改为 mark∓0.1ATR
- **方案**：开关已实现
- **涉及文件**：`config.py`、`shared/calc_sl_tp.py`
- **依赖**：无
- **状态**：暂缓——评审结论为舍弃（会绕过清算区加权入场价），代码保留但永久关闭

## [OPT-9] 失效位止损（`OPT_INVALIDATION_SL`）
- **来源**：v3.87 合并决策
- **问题**：回踩/EMA 趋势/反转改用 1H 结构点外 0.3ATR 止损
- **方案**：开关已实现
- **涉及文件**：`config.py`、`shared/calc_sl_tp.py`
- **依赖**：无
- **状态**：暂缓——评审结论为舍弃（会绕过清算区止损），代码保留但永久关闭

## [OPT-10] OI 背离衰竭确认（`OPT_OI_DIV_EXHAUSTION_CONFIRM`）
- **来源**：v3.87 合并决策
- **问题**：OI 背离需不创新低(高) + 主动买卖比确认
- **方案**：开关已实现
- **涉及文件**：`config.py`、`shared/judges.py`
- **依赖**：无
- **状态**：暂缓（待 oi_divergence 累计 20+ 笔再定）

## [OPT-11] EMA 趋势需触发 K 线（`OPT_EMA_TREND_TRIGGER_BAR`）
- **来源**：v3.87 合并决策（评审未列入）
- **问题**：EMA 趋势入场缺少 1H/15M 触发 K 线确认
- **方案**：开关已实现
- **涉及文件**：`config.py`、`shared/judges.py`
- **依赖**：无
- **状态**：暂缓（保守默认关闭，待数据）

## [OPT-12] 放量突破放宽 RSI（`OPT_BREAKOUT_RSI_STRONG_RELAX`）
- **来源**：v3.87 合并决策
- **问题**：强量突破被 RSI 上限误拦
- **方案**：开关已实现
- **涉及文件**：`config.py`、`shared/judges.py`
- **依赖**：无
- **状态**：暂缓（待验证 RSI>65 突破单质量）

## [OPT-13] 突破延续入场（`OPT_BREAKOUT_MOMENTUM_ENTRY`）
- **来源**：v3.87 合并决策
- **问题**：距突破位 ≤0.5ATR 时仍强制等回踩
- **方案**：开关已实现
- **涉及文件**：`config.py`、`shared/calc_sl_tp.py` / `executor.py`
- **依赖**：无
- **状态**：暂缓（待对比成交率与止损率）

## [OPT-LRS] 清算区相对强度动态化（`OPT_LIQ_RELATIVE_STRENGTH`）
- **来源**：config.py 总开关
- **问题**：回踩入场清算区权重与 TP1 缓冲固定
- **方案**：开关已实现
- **涉及文件**：`config.py`、`shared/calc_sl_tp.py`
- **依赖**：无
- **状态**：暂缓（默认关闭，未见评审结论）

## [SW-1] 被动触发器与触发驱动分析
- **来源**：阶段 3（`4689af69`）/ 3.1（`aef21291`）/ 5（`d119fe2a`）
- **问题**：`TRIGGER_DETECTOR_ENABLED=False`、`TRIGGER_DISPATCH_ENABLED=False`，漏斗中触发数恒为 0
- **方案**：先开影子触发器对账，确认漏报/误报率后再开派发
- **涉及文件**：`config.py`
- **依赖**：漏斗数据（P-10）
- **状态**：暂缓（待影子对账数据）

## [SW-2] 分析进程池（`SCAN_USE_PROCESS_POOL`）
- **来源**：config.py
- **问题**：线程池受 GIL 限制，`SCAN_MAX_WORKERS=4` 时每轮分析 120~151 秒
- **方案**：开关已实现（spawn 进程池）
- **涉及文件**：`config.py`
- **依赖**：内存与稳定性验证
- **状态**：暂缓（默认关闭）

## [SW-3] 突破强平量过滤（`BREAKOUT_LIQ_FILTER_ENABLED`）
- **来源**：v3.49 / v3.50
- **问题**：假突破过滤
- **方案**：开关已实现；`config.py` 已补定义 `BREAKOUT_LIQ_FILTER_ENABLED = False`（此前未定义，导入失败时同样回落 False，行为不变）
- **涉及文件**：`config.py`（开关）、`shared/signal_watcher.py`；开启时把 config.py 中的值改为 True
- **依赖**：无
- **状态**：暂缓（等实盘数据足够后再开）

---

## 三、讨论过但明确不做的事项（备查）

- **IOC 下单 / 30 分钟撤单**：需改 executor，`fdde5ff4` 时决定不做
- **crash_guard 冷却"幅度 1.1 倍才延长"**：judges 不传幅度，`a7184c09` 时决定不做
- **`_alert_all_failed` 改 Popen**：已在后台线程，不阻塞调用方，`cb792429` 时决定不做
- **SCAN_MAX_WORKERS / 令牌桶热重载**：池子建好后不重建，`afd4506c` 时决定不做
