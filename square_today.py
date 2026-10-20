#!/usr/bin/env python3
import os
import requests, json, sys

api_key = os.environ.get("SQUARE_TODAY_API_KEY", "")
proxy_cfg = json.load(open('proxy_config.json'))
proxy_url = proxy_cfg['proxy']
proxies = {'http': proxy_url, 'https': proxy_url}

posts = [
    """BABY 的核心价值主张正在兑现

Babylon Trustless Bitcoin Vaults 让原生 BTC 无需包装直接用作 Aave v4 的抵押品。

这意味着什么？
→ 比特币持有者不必把 BTC 换成 wBTC 或其他衍生品
→ 私钥从不离开冷钱包，真正自托管
→ BTC 进入 DeFi 借贷市场，年化收益唾手可得

过去 24H BABY 涨幅 2.42%，成交量激增 198K USDT，市场在用真金白银投票。

比特币不应该只是"数字黄金"，它也可以是 DeFi 的核心流动性基础。

你觉得 Babylon 能让 BTC 真正活起来吗？评论区聊聊

@BabylonLabs_io $BABY #baby""",

    """为什么我选择 Babylon 而不是中心化的 BTC 理财？

最近很多平台都推"BTC 年化 5%~8%"的产品，但有多少人真正看过合约细则？

这些产品通常的运作方式：
X 你的 BTC 被借出给做市商
X 你持有的是"平台欠你的 BTC"，不是真实链上资产
X 平台出问题，你的币就没了

Babylon 的逻辑完全不同：
√ BTC 用时间锁锁在比特币链上
√ 你的私钥从不上传，协议无法动用你的 BTC
√ 收益来自 PoS 链的安全共识，而非借贷风险

自托管就是自由。别把自由交出去只为了那点利息。

你更在乎收益率还是资产的真正所有权？

@BabylonLabs_io $BABY #baby"""
]

for i, post in enumerate(posts, 1):
    resp = requests.post(
        'https://www.binance.com/bapi/composite/v1/public/pgc/openApi/content/add',
        headers={
            'X-Square-OpenAPI-Key': api_key,
            'Content-Type': 'application/json',
            'clienttype': 'binanceSkill'
        },
        json={'bodyTextOnly': post},
        proxies=proxies,
        timeout=20
    )
    data = resp.json()
    if data.get('code') == '000000':
        post_id = data.get('data', {}).get('id', '')
        print(f'Post {i} OK: https://www.binance.com/square/post/{post_id}')
    else:
        print(f'Post {i} FAIL: {data}')
