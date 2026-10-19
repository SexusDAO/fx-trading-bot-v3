#!/usr/bin/env python3
"""
测试 GMGN API 库
- 验证 JWT token 是否可用
- 测试 BSC 链支持
- 测试 new_pools 和 wallet_trades 通道
"""

import os
import asyncio
import json
from gmgnapi import GmGnClient

# 用户提供的 JWT token
JWT_TOKEN = os.environ.get("TEST_GMGN_API_JWT_TOKEN", "")

# 测试用的 BSC KOL 地址（从 bsc_kol_wallets.json 随机选一个）
TEST_WALLET = "0x435a26a15f85e5e1a2d768e4e23da2be1b15e5e4"

message_count = 0
MAX_MESSAGES = 20  # 收集 20 条消息后停止

async def on_new_pool(data):
    """处理新池子消息"""
    global message_count
    message_count += 1
    print(f"\n[NEW POOL #{message_count}]")
    print(json.dumps(data, indent=2, default=str)[:500])
    
    if message_count >= MAX_MESSAGES:
        print(f"\n✅ 已收集 {MAX_MESSAGES} 条消息，测试成功！")
        return True

async def on_wallet_trade(data):
    """处理钱包交易消息"""
    global message_count
    message_count += 1
    print(f"\n[WALLET TRADE #{message_count}]")
    print(json.dumps(data, indent=2, default=str)[:500])
    
    if message_count >= MAX_MESSAGES:
        print(f"\n✅ 已收集 {MAX_MESSAGES} 条消息，测试成功！")
        return True

async def on_error(error):
    """处理错误"""
    print(f"\n❌ ERROR: {error}")

async def test_bsc_new_pools():
    """测试 BSC 链的 new_pools 通道"""
    print("=" * 60)
    print("测试 1: BSC new_pools 通道")
    print("=" * 60)
    
    client = GmGnClient(access_token=JWT_TOKEN)
    
    # 注册事件处理器
    client.on('new_pools', on_new_pool)
    client.on('error', on_error)
    
    try:
        print("正在连接 WebSocket...")
        await client.connect()
        print("✅ 连接成功")
        
        print("正在订阅 BSC new_pools...")
        await client.subscribe_new_pools(chain='bsc')
        print("✅ 订阅成功，等待数据...")
        
        # 等待消息
        await asyncio.sleep(30)
        
    except Exception as e:
        print(f"❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await client.disconnect()
        print("已断开连接")

async def test_bsc_wallet_trades():
    """测试 BSC 链的 wallet_trades 通道"""
    print("\n" + "=" * 60)
    print("测试 2: BSC wallet_trades 通道")
    print("=" * 60)
    
    client = GmGnClient(access_token=JWT_TOKEN)
    
    # 注册事件处理器
    client.on('wallet_trades', on_wallet_trade)
    client.on('error', on_error)
    
    try:
        print("正在连接 WebSocket...")
        await client.connect()
        print("✅ 连接成功")
        
        print(f"正在订阅 BSC wallet_trades (钱包: {TEST_WALLET[:10]}...)")
        await client.subscribe_wallet_trades(chain='bsc', wallet_address=TEST_WALLET)
        print("✅ 订阅成功，等待数据...")
        
        # 等待消息
        await asyncio.sleep(30)
        
    except Exception as e:
        print(f"❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await client.disconnect()
        print("已断开连接")

async def main():
    """主测试函数"""
    print("🚀 GMGN API 测试开始\n")
    print(f"JWT Token: {JWT_TOKEN[:50]}...")
    print(f"测试钱包: {TEST_WALLET}")
    
    # 测试 1: new_pools
    await test_bsc_new_pools()
    
    # 重置计数器
    global message_count
    message_count = 0
    
    # 测试 2: wallet_trades
    await test_bsc_wallet_trades()
    
    print("\n" + "=" * 60)
    print("🎉 测试完成")
    print("=" * 60)

if __name__ == "__main__":
    asyncio.run(main())
