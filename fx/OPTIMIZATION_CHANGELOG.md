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
