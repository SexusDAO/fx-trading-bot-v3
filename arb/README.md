# arb/ — 资金费率套利机器人

## 职责边界
- 本目录只做跨交易所资金费率套利（现货+合约对冲，或A所多+B所空）
- **严禁 import fx/ 下的任何模块**
- **严禁调用 /fapi/v1/order（合约下单接口）用于套利执行**
  - 套利只用现货买卖 + 该所自己的永续合约（如有）
  - 所有合约下单必须经过 arb/executor.py 的 DRY_RUN 开关

## 目录结构
```
arb/
  config.py       — 套利专用配置（交易所、资金、仓位上限）
  scanner.py      — 资金费率扫描，只查询，不下单
  executor.py     — 下单执行，必须有 DRY_RUN=True 保护
  state.json      — 当前套利仓位状态
  README.md       — 本文件
```

## 上线前必须通过的检查
1. DRY_RUN=True 模拟跑通全流程
2. 用户看到预览后回复 CONFIRM
3. 实际下单后写入 state.json

## 已完成的物理隔离
- arb/ 不依赖 fx/
- arb/ 不依赖 paper_scanner
- agent 在套利任务中只允许 import arb/
