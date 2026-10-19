# OPTIMIZATION_CHANGELOG — v3.83 / v3.84 改动清单（供评审）

> 原则：每项改动都有开关或可单独回滚；代码里以 `[v3.8x ...]` 注释标出原因、依据和评估方法。
> 以下"预期效果"是基于金融逻辑的假设，**没有经过回测或实盘验证**，请用模拟盘/回测确认后再决定是否合并。

## 分支与内容

| 分支 | 内容 | 性质 |
|---|---|---|
| `fix/scanner-obvious-bugs` | 预筛方向判断反了；熊市预筛过宽；EMA 趋势空头分支不可达 | bug 修复，会增加信号数量 |
| `fix/logic-conflicts-pm` | `_entry` 未定义、`open_ts` 字段错、`signal_watcher` 缺 `api` 导入；价格循环异常隔离 | bug 修复 |
| `opt/first-pass-v384`（本分支） | 下表 OPT-1 ~ OPT-3 + 保本阶梯缓冲 + 工具与单测 | 优化，均有开关 |

## 本分支的优化项

| 编号 | 位置 | 原逻辑 | 新逻辑 | 开关（config.py） | 预期效果（假设） | 怎么评估 |
|---|---|---|---|---|---|---|
| OPT-1 | `position_manager._check_time_stop` | 持仓 6H(反转 8H) 且浮亏>固定 2% 就市价平仓，比止损(≥2.5%/3%/5%)更紧，是"隐形止损" | 阈值=max(2%, 0.6×止损距离) | `OPT_STAGNATION_SCALE_BY_SL` | 宽止损标的少被提前砍，实现亏损次数下降 | 对比 `close_reason=early_stagnation` 占比及其后续是否触及止损 |
| OPT-2 | `position_manager._on_price_update` | TP1 后价格跌穿入场价，却把止损放到入场价另一侧(错误一侧)，会被拒单或立即成交 | 此时直接平掉剩余仓位；价格仍在有利侧才移动止损到保本 | `OPT_TP1_PROTECT_CLOSE` | 避免拒单/不可预期成交，锁定 TP1 利润 | 看 `close_reason=tp1_protect_exit` 的笔数与平均盈亏；观察日志有无 -2021 |
| OPT-3 | `calc_sl_tp._entry_pullback` | 无清算区降级：多 0.3ATR、空 0.8ATR，不对称 | 两侧统一 0.5ATR(最小0.3，上限2.0) | `OPT_PULLBACK_ENTRY_SYMMETRIC` | 多头入场价更优；空头更易成交，开仓次数上升 | 对比回踩单成交率、成交后 MAE |
| OPT-4 | `config.TRAIL_STEPS_TREND` | 浮盈 0.8ATR 就把止损挪到恰好入场价 | 1.0ATR 才挪，且放在入场价+0.1ATR(覆盖手续费滑点)，其余档后移 | `OPT_TRAIL_BE_BUFFER` | 减少"赚一点又被扫回 0"的零收益止损 | 把止损拆成"保本/移动止损"与"真亏损止损"分别统计 |

## 辅助

- `shared/opt_v384.py`：上面三项的纯函数实现，便于单测与评审。
- `tests/test_opt_v384.py`：12 个单测，`cd fx && python3 -m unittest tests.test_opt_v384 -v`。
- `tools/reject_funnel.py`：只读，统计各路径通过率与拒绝原因 Top N，用于评估放宽过滤后的效果。

## 刻意没有改的内容

- 熔断、杠杆、保证金预检、日亏损上限等风控项（按你的要求留到最后）。
- Stop Hunt 已是 `STOP_HUNT_ENABLED=False`，入场价随机化已是 `ORDER_ENTRY_RANDOMIZE=False`，无需处理。
- `strategy-config.json` 未动。

## 合并前请重点观察

1. OPT-4 会让趋势单的止损更晚保本，**单笔最大回撤可能变大**，请与"零收益止损减少"一起权衡。
2. 前两个分支的 bug 修复会让"TP1 后保本"首次真正运行，请先看 OPT-2 的日志。
3. 本轮改动都没有在实盘或回测环境验证过，仅做了语法、静态检查和纯函数单测。

---

# 第二轮（v3.85）— 分支 `opt/second-pass-v385`（基于 `opt/first-pass-v384`）

> 验证：静态检查无未定义变量；新增 4 个单测通过（两轮合计 16 个）；原有测试结果与改动前一致（`test_judge.py` 改动前后均为 8 失败 / 13 通过，均为既有问题）。未回测、未跑模拟盘。

| 编号 | 在哪里 | 改了什么 | 为什么改 | 开关（config.py） |
|---|---|---|---|---|
| OPT-5 | `shared/judges.py` `_judge_breakout_impl` | 判断突破前，若最后一根 1H K 线未收盘（`k[6]` > 当前时间）就去掉它 | 原代码把 `k1h[-1]` 当作已闭合突破 K 线，但 scanner 拉到的最后一根通常未收盘：盘中触发后会被重绘成假突破，量比也只算了部分成交量 | `OPT_BREAKOUT_CLOSED_BAR` |
| OPT-6 | `scanner.py` 新增 `_in_symbol_funding_window`；`analyze_symbol`；`run_scan` | 资金费禁开仓窗口改为按该标的自己的 `nextFundingTime` 和结算周期（1/4/8H）判断；启用后不再因 BTC 费率整轮停扫；拿不到结算时间时回退旧逻辑 | 原逻辑只看 UTC 0/8/16 整点 + BTC 一个费率：1H/4H 结算的合约在其他结算时刻没有保护，而所有标的又会被一起停扫，白白丢失扫描轮次 | `OPT_FUNDING_WINDOW_PER_SYMBOL` |
| FIX-1 | `shared/judges.py` `_judge_breakout_impl`；`config.py` `BREAKOUT_RSI_*` | 突破的 RSI 门槛改为读取配置；配置值同步为原代码实际生效值（多 65、空 35） | 原写法 `65 if tier=="high" else 65` 两个分支相同，分层是假的；配置里 `BREAKOUT_RSI_MAX_LONG=75` 等从未被读取。**本项行为不变**，只是让配置真正生效 | 无（行为不变） |
| FIX-2 | `position_manager.py` `_maybe_pyramid` | 加仓所需 RSI 拿不到时，本轮不加仓 | 原注释写"保守允许加仓"，但数据缺失时继续加仓恰恰不保守 | `OPT_PYRAMID_FAIL_CLOSED` |
| DOC-1 | `position_manager.py` `_check_time_stop` | 更正硬上限注释：实际所有类型统一 16H；并注明 TP1 已成交且盈利的持仓实际上限是 72H | 原注释写"reversal 16H、其他 12H"，与实现不符，容易误导调参 | 无（仅注释） |
| DOC-2 | `config.py` | 给 93 个代码从未引用的常量加上注释 `[v3.85 DOC-2] 代码未引用此常量，修改无效` | 这些常量（候选池、反转、扫描间隔等）改了不会生效，调参时会误以为改了 | 无（仅注释） |

辅助文件：`shared/opt_v385.py`（逐标的结算窗口的纯函数）、`tests/test_opt_v385.py`（4 个单测）。

## 本轮发现但刻意未改（需要你决定）

- **反转做空没有分层门槛**（`judges.py` `_judge_reversal_impl`）：`_rev_chg24_short`、`rsi_short_thresh`、`fr_short_thresh` 算了但没用，做空实际只用写死的 `chg24>=5`、`RSI4H>65`。接上会改变信号数量，留给你定。
- **TP1 可达性检查只完成一半**（`signal_watcher.py`）：读取了 TP1 价格但从未使用，补全需要定义"不可达"的标准。
- **保证金预检写死 10x**（`executor._account_final_check`）：属于风控，按你的要求暂不动。
- **有 51 个既有测试报错、`test_judge.py` 有 8 个失败**：改动前就存在，测试本身已过时，暂未修。

## 本轮合并后请观察

1. OPT-5：突破信号会晚出现约一根 K 线内的时间，但假突破应减少；对比突破单的 TP1 触发率。
2. OPT-6：资金费窗口内仍会扫描并对非结算标的开仓，扫描次数会增加，注意内存和接口限频；`get_funding_interval` 每 6 小时全量刷新一次缓存。


---

# 第三轮（v3.86）— 分支 `opt/third-pass-v386`（基于 `opt/second-pass-v385`）

> 验证：全量测试由改动前的「51 个报错 + 2 个文件无法导入」变为「151 通过 / 9 失败」；剩余 9 个失败在 main 上单独运行同样失败（`test_judge.py` 8 个、`test_risk.py` 1 个），属于测试本身过时，非本轮引入。未回测、未跑模拟盘。

| 编号 | 在哪里 | 改了什么 | 为什么改 | 开关（config.py） |
|---|---|---|---|---|
| FIX-3 | `shared/signal_watcher.py` BTC 剧烈波动后的「止损踩空」检查 | `from shared.notify` 改为 `from shared.notifier` | 原模块名不存在，ImportError 被 except 吞掉，止损踩空告警从未发出过 | 无（bug 修复） |
| OPT-7 | `shared/judges.py` `_judge_reversal_impl` 做空条件二 | 做空 RSI4H 门槛取 `max(65, rsi_short_thresh)`，即高波动层 70、其余 65 | 原代码已按波动层算出 `rsi_short_thresh` 却从未使用；高波动币 RSI 更容易冲高，65 不算超买，易在加速段过早做空 | `OPT_REVERSAL_SHORT_TIER_RSI` |
| TEST-1 | `tests/test_exit_signal_and_direction.py` | 桩模块只在本文件测试运行期间临时装入（fixture），收集结束即恢复真实模块 | 原实现永久覆盖 `sys.modules` 中的 `shared.*`，导致其他测试文件导入到空桩，出现「无法导入」和 51 个 AttributeError，测试失去保护作用 | 无（仅测试） |

## 本轮评估后决定不改

- `fr_short_thresh`（反转做空资金费门槛 0.25%/0.35%）：作为硬门槛会大幅减少做空信号，与 v3.6 有意放宽冲突，仅在代码中注释说明。
- TP1 可达性检查（`signal_watcher.py` 读取了 `_tp12` 未使用）：需要先定义"不可达"的标准（例如 TP1 距现价超过 N 倍 ATR），留待你定。
- 剩余 9 个过时测试：需要按当前判定逻辑重写测试数据，工作量较大，单独一轮处理更清楚。

## 本轮合并后请观察

- OPT-7：高波动币的反转做空信号会减少，对比这类单的胜率。
- FIX-3：BTC 剧烈波动时会开始收到「止损踩空」告警，若过于频繁可再调节流。

---

# 第四轮（v3.87）开仓场景重构·第一批：分支 `opt/entry-redesign-v387`（基于 `opt/third-pass-v386`）

> 统一原则（四要素）：**触发 → 确认 → 在确认处入场 → 止损放在"开仓理由失效位"外侧**。目标是让每一次止损都能归因：要么市场整体波动，要么结构真的失效，而不是入场位置本身有问题。
> 验证：静态检查无未定义变量；新增 7 个单测通过（四轮合计 23 个）；全量测试 158 通过 / 9 失败，9 个失败与改动前相同（均为既有过时测试）。中途发现本批改动使 3 个旧测试失败，已修正实现（见 OPT-9 说明），并更新 1 个测试断言。未回测、未跑模拟盘。

| 编号 | 在哪里 | 改了什么 | 为什么改 | 开关（config.py） |
|---|---|---|---|---|
| OPT-8 | `shared/calc_sl_tp.py` `_entry_pullback` | 回踩限价挂在 `mark∓0.1ATR`，即确认发生处；入场模式记为 `at_confirm` | 回踩判定已要求"价格在回踩区 + 止跌/止涨信号"，原逻辑却在确认后再挂到更深的位置：只有价格继续走坏才成交，而这说明信号已失效，成交天然偏向坏单（逆向选择）。**覆盖第一轮 OPT-3 的降级参数**（OPT-8 开启时 OPT-3 不再生效） | `OPT_PULLBACK_ENTRY_AT_CONFIRM` |
| OPT-9 | `shared/calc_sl_tp.py` `calc_sl_tp` 止损段 | 回踩 / EMA 趋势 / 反转：止损放在最近 8 根已闭合 1H 结构低点（高点）外 0.3ATR，距入场 1.0~3.0ATR；同时不小于原波动层最小距离（2.5% / 3% / 5%）；结构太远则保留原止损。`sl_degraded` 保持原值，新增 `signal["_sl_source"]="invalidation"` 供日志统计 | 原止损由清算图（非币安官方数据，常缺失）或固定 ATR/百分比决定，与开仓理由无关。改为只用币安官方 K 线的结构失效位。**注意**：止损距离变化会通过 `RISK_PER_TRADE` 改变名义仓位（仍受单仓上限约束） | `OPT_INVALIDATION_SL` |
| OPT-10 | `shared/judges.py` `_judge_oi_divergence_impl` | 新增衰竭确认：① 最后一根已闭合 1H 未创近 3 根新低（做空：新高）② 主动买入占比高于（做空：低于）近 20 根均值 | 原逻辑"价格在 24H 低点附近 + OI 1H 增 10% → 做多"，同样是空头顺势加仓的典型形态，更常见的是继续下跌。只有卖压衰竭、买盘回升，才是"空头被困" | `OPT_OI_DIV_EXHAUSTION_CONFIRM` |
| OPT-11 | `shared/judges.py` `_judge_ema_trend_impl` | 要求 1H 或 15M 出现止跌（做多）/止涨（做空）形态；形态名记入 `trigger_bar` | 原逻辑价格回到 4H EMA20 附近就以现价立即入场，等于"到了区域就买"，回踩可能还在进行中 | `OPT_EMA_TREND_TRIGGER_BAR` |

辅助文件：`shared/opt_v387.py`（失效位止损、衰竭确认两个纯函数）、`tests/test_opt_v387.py`（7 个单测）；`tests/test_calc_sl_tp.py` 的入场模式断言加入 `at_confirm`。

## 场景评估结论（本轮的设计依据）

| 场景 | 结论 | 本批是否改动 |
|---|---|---|
| 回踩 | 保留结构；入场与止损改为四要素 | 是（OPT-8、OPT-9） |
| OI 背离 | 方向假设不可靠，需衰竭确认 | 是（OPT-10） |
| EMA 趋势 | 保留；缺触发 K 线 | 是（OPT-11、OPT-9） |
| 反转 | 条件严但清晰；只换止损锚点 | 仅 OPT-9 |
| 突破 | 保留；执行方式需改（真突破不回踩会错过） | 下一批 |
| 区间反弹 | 保留；可补对称的做空版本 | 下一批 |
| 资金费套利 | 保留 | 否 |
| Stop Hunt | 维持关闭（延续率约 50%，无优势） | 否 |

## 下一批计划

1. 突破：拆成"突破位回踩"与"15M 延续确认"两种入场，止损放在突破 K 线另一端外侧。
2. 区间反弹：补"暴跌后横盘上沿做空"。
3. 止盈锚点：从清算图逐步换成官方 K 线可推导的结构位与成交量分布（价值区高低点）。

## 本批合并后请观察

1. 回踩成交率会上升（不再等更深的价），关注成交后 1 小时内的最大不利偏移（MAE）是否下降。
2. 按 `_sl_source=invalidation` 统计止损单：被打掉时价格是否真的越过了结构位。
3. OI 背离与 EMA 趋势的信号数会减少，这是预期内的。
4. 止损距离变化会改变仓位大小，首批成交请核对名义仓位。
