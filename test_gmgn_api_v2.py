#!/usr/bin/env python3
"""
测试 GMGN API - 版本 2
尝试在 headers 中传递 Authorization
"""

import os
import asyncio
import json
import websockets

# 用户提供的 JWT token
JWT_TOKEN = os.environ.get("TEST_GMGN_API_V2_JWT_TOKEN", "")

WS_URL = "wss://ws.gmgn.ai/quotation"

async def test_connection():
    """测试 WebSocket 连接"""
    print("=" * 60)
    print("测试 GMGN WebSocket 连接")
    print("=" * 60)
    
    # 构建 URL 参数
    import uuid
    from urllib.parse import urlencode
    
    params = {
        "device_id": str(uuid.uuid4()),
        "client_id": f"gmgn_python_{uuid.uuid4().hex[:8]}",
        "from_app": "gmgn",
        "app_ver": "20250729-1647-ffac485",
        "tz_name": "UTC",
        "tz_offset": "0",
        "app_lang": "en-US",
        "fp_did": uuid.uuid4().hex,
        "os": "python",
        "uuid": uuid.uuid4().hex,
    }
    
    query_string = urlencode(params)
    url = f"{WS_URL}?{query_string}"
    
    # 构建 headers
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Origin": "https://gmgn.ai",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Authorization": f"Bearer {JWT_TOKEN}",
    }
    
    print(f"\n连接 URL: {WS_URL}")
    print(f"Headers: Authorization=Bearer {JWT_TOKEN[:50]}...")
    
    try:
        print("\n正在连接...")
        async with websockets.connect(
            url,
            additional_headers=headers,
            ping_interval=30,
            ping_timeout=10,
        ) as ws:
            print("✅ 连接成功！")
            
            # 订阅 BSC new_pools
            subscription = {
                "action": "subscribe",
                "channel": "new_pool_info",
                "f": "w",
                "id": uuid.uuid4().hex[:16],
                "data": [{"chain": "bsc"}]
            }
            
            print(f"\n订阅 BSC new_pools...")
            await ws.send(json.dumps(subscription))
            print("✅ 订阅请求已发送")
            
            # 等待消息
            print("\n等待数据（30秒）...")
            message_count = 0
            start_time = asyncio.get_event_loop().time()
            
            while asyncio.get_event_loop().time() - start_time < 30:
                try:
                    message = await asyncio.wait_for(ws.recv(), timeout=5)
                    message_count += 1
                    
                    # 解析消息
                    data = json.loads(message)
                    print(f"\n[消息 #{message_count}]")
                    print(f"Channel: {data.get('channel', 'unknown')}")
                    
                    # 显示部分数据
                    if 'data' in data:
                        print(f"Data keys: {list(data['data'].keys()) if isinstance(data['data'], dict) else 'list'}")
                        # 如果是新池子，显示详细信息
                        if data.get('channel') == 'new_pool_info' and 'p' in data.get('data', {}):
                            pools = data['data']['p']
                            if pools:
                                pool = pools[0]
                                print(f"  新池子: {pool.get('ba', 'N/A')[:20]}...")
                                if 'bti' in pool:
                                    print(f"  Token: {pool['bti'].get('s', 'N/A')} (${pool['bti'].get('n', 'N/A')})")
                                    print(f"  MC: ${pool['bti'].get('mc', 0):,.0f}")
                    
                    if message_count >= 10:
                        print(f"\n✅ 已收到 {message_count} 条消息，测试成功！")
                        break
                        
                except asyncio.TimeoutError:
                    print(".", end="", flush=True)
                    continue
            
            print(f"\n\n总共收到 {message_count} 条消息")
            
    except websockets.exceptions.InvalidStatus as e:
        print(f"\n❌ 连接被拒绝: HTTP {e.response.status_code}")
        print(f"响应头: {e.response.headers}")
    except Exception as e:
        print(f"\n❌ 连接失败: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(test_connection())
