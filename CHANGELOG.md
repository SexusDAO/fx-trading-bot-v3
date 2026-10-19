# CHANGELOG.md

记录版本变更、已知问题与待办事项。
此文件不注入 system prompt，需要时手动 `read CHANGELOG.md`。

---

## 已知问题

### [KI-001] WS proxy 断连导致止损感知延迟最长 5 分钟

- **发现时间：** 2026-10-19
- **位置：** `position_manager._check_manual_close` / `ws_listener`
- **现象：**
  WS proxy 断连时 `ORDER_TRADE_UPDATE` 事件丢失，止损在 Binance 侧已成交，
  但本地系统最长需要等 patrol 间隔（5 分钟）才能感知到持仓清零。
- **影响：**
  - 止损后的冷却期、黑名单写入、通知推送均延迟最长 5 分钟触发
  - 不影响资金安全：止损单已在 Binance 交易所侧成交，持仓已了结
  - 影响统计准确性：trade_log 里的 close_time 记录的是 patrol 发现时间，不是实际成交时间
- **已确认案例：** SKUUUSDT SHORT（2026-10-19 02:10 开仓，约 06:02 手动补录）、APRUSDT SHORT
- **建议方案（待实现）：**
  1. WS 断连事件触发时，立即执行一次 `patrol_once()`，不等下一个 5 分钟周期
  2. 或：缩短 WS 重连期间的 patrol 间隔（如 30 秒），待 WS 恢复后还原
- **优先级：** 中（不影响资金安全，止损单已在交易所侧成交）
- **相关代码：** `main.py` `_loop_patrol(300, 30)` / `ws_listener.py` reconnect handler

---

## 版本记录

### v3.88-bugfix · 2026-10-19

**修复**

1. **[PM] `_check_manual_close` 区分 SL 触发 vs 手动平仓**
   - 文件：`fx/position_manager.py`
   - 问题：patrol 检测到持仓清零时，一律写 `close_reason="manual_close"`，
     无法区分「止损 Algo 单触发成交」和「用户手动市价平仓」
   - 修复：持仓清零时，额外查一次交易所 `openAlgoOrders`：
     - SL algoId **不在** → `close_reason="sl_triggered"`
     - SL algoId **仍在** → `close_reason="manual_close"`（用户手动，SL 还挂着）
   - 不影响：SL/TP 价格计算、冷却期黑名单（`_close_position_post_static` 已有 `sl_triggered` 分支）

2. **[Scanner] 候选池 `ts` 加 `ts_unit` 防误读字段**
   - 文件：`fx/scanner.py`
   - 问题：`ts` 为秒级（`time.time()`），健检脚本误当毫秒除以 1000，显示「57 年前」
   - 修复：写入时加 `"ts_unit": "seconds"`，生产读取逻辑无需改动
   - 根因：生产 `_load_pool` 逻辑本身正确，候选池一直正常运行

3. **[Data] SKUUUSDT 孤儿开仓记录补录**
   - 文件：`fx/logs/trade_log.jsonl`（gitignore 外，手动维护）
   - 补填字段：`result="stop_loss"` / `pnl=-9.75` / `manually_patched=true`

**新增单测**
- `fx/tests/test_bugfix_oct19.py`：7 条，全部通过
  - T1~T4：`_check_manual_close` 推断逻辑
  - T5~T7：候选池 ts 单位一致性
