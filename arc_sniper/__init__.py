"""
Arc Sniper — 在 Arc 链上自动发现并买入新 meme 币的机器人。

架构：
    Scanner  → 监听 DEX 工厂合约的新币事件
    OnChain  → 拉取链上数据（持有人、流动性、蜜罐检测）
    Filter   → 评分过滤，决定是否买入
    Executor → web3.py 直连 RPC 执行买入
    Notify   → 推送通知到 Jarvis

不含止盈止损 — 用户自行控制离场。
"""
