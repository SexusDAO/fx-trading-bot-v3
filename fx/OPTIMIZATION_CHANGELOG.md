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
