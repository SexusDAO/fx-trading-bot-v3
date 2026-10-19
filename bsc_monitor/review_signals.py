#!/usr/bin/env python3
"""
复盘之前推送的信号，用 GMGN 拉取最新数据判断现状
"""

import subprocess
import json
import re

# 从 monitor.log 提取推送过的代币地址
def extract_pushed_tokens():
    """从日志中提取推送过的代币地址"""
    tokens = {}
    with open('/root/.openclaw/workspace/bsc_monitor/monitor.log', 'r') as f:
        content = f.read()
    
    # 找到所有推送信号
    # 格式：🔔 🎯 KOL/聪明钱买入 $XXX ... 🔗 https://gmgn.ai/bsc/token/0x...
    pattern = r'🔔 (🎯|👥|🐋) [^\n]+ \$([^\n]+)\n.*?🔗 https://gmgn\.ai/bsc/token/(0x[a-f0-9]+)'
    matches = re.findall(pattern, content, re.DOTALL)
    
    for emoji, symbol, addr in matches:
        if addr not in tokens:
            tokens[addr] = {
                'symbol': symbol,
                'type': 'KOL' if emoji == '🎯' else ('共买' if emoji == '👥' else '鲸鱼'),
                'count': 0
            }
        tokens[addr]['count'] += 1
    
    return tokens

# 用 GMGN CLI 查询代币当前状态
def query_token_status(addr):
    """查询代币当前状态"""
    try:
        result = subprocess.run(
            ['gmgn-cli', 'token', 'info', '--chain', 'bsc', '--address', addr, '--raw'],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            return data
    except Exception as e:
        print(f"  查询失败: {e}")
    return None

# 分析代币状态
def analyze_token(addr, info):
    """分析代币当前状态"""
    if not info:
        return "查询失败"
    
    # 提取关键数据
    token_data = info.get('token', {})
    price_usd = float(token_data.get('price_usd') or 0)
    market_cap = float(token_data.get('market_cap') or 0)
    liquidity = float(token_data.get('liquidity') or 0)
    holders = int(token_data.get('holders') or 0)
    
    # 判断状态
    if price_usd == 0 or market_cap < 1000:
        return "归零"
    elif market_cap < 10000:
        return "濒危"
    elif market_cap < 50000:
        return "低迷"
    elif market_cap < 200000:
        return "存活"
    else:
        return "成功"

def main():
    print("=" * 70)
    print("📊 复盘之前推送的信号（用 GMGN 最新数据）")
    print("=" * 70)
    
    # 提取推送过的代币
    tokens = extract_pushed_tokens()
    print(f"\n找到 {len(tokens)} 个推送过的代币\n")
    
    # 统计结果
    results = {
        '归零': [],
        '濒危': [],
        '低迷': [],
        '存活': [],
        '成功': [],
        '查询失败': []
    }
    
    # 查询每个代币的当前状态
    for addr, info in list(tokens.items())[:20]:  # 只查前20个
        symbol = info['symbol']
        push_count = info['count']
        
        print(f"查询 ${symbol} ({addr[:10]}...) 推送{push_count}次...")
        
        token_data = query_token_status(addr)
        status = analyze_token(addr, token_data)
        
        if token_data:
            t = token_data.get('token', {})
            price = float(t.get('price_usd') or 0)
            mc = float(t.get('market_cap') or 0)
            liq = float(t.get('liquidity') or 0)
            holders = int(t.get('holders') or 0)
            
            print(f"  状态: {status}")
            print(f"  价格: ${price:.10f}")
            print(f"  市值: ${mc:,.0f}")
            print(f"  流动性: ${liq:,.0f}")
            print(f"  持有人: {holders}")
            print()
        
        results[status].append({
            'symbol': symbol,
            'addr': addr,
            'push_count': push_count,
            'data': token_data
        })
    
    # 输出统计
    print("=" * 70)
    print("📈 复盘结果统计")
    print("=" * 70)
    
    total = sum(len(v) for v in results.values())
    for status, tokens_list in results.items():
        if tokens_list:
            pct = len(tokens_list) / total * 100 if total > 0 else 0
            print(f"\n{status}: {len(tokens_list)} 个 ({pct:.1f}%)")
            for t in tokens_list[:5]:  # 只显示前5个
                print(f"  - ${t['symbol']} (推送{t['push_count']}次)")
    
    # 输出建议
    print("\n" + "=" * 70)
    print("💡 优化建议")
    print("=" * 70)
    
    success_rate = (len(results['成功']) + len(results['存活'])) / total * 100 if total > 0 else 0
    print(f"\n信号成功率: {success_rate:.1f}%")
    print(f"归零率: {len(results['归零']) / total * 100:.1f}%")
    
    if len(results['归零']) > 0:
        print("\n归零代币特征分析:")
        for t in results['归零'][:5]:
            if t['data']:
                token = t['data'].get('token', {})
                print(f"  ${t['symbol']}:")
                print(f"    - 创建者发币数: {token.get('creator_open_count', 'N/A')}")
                print(f"    - 风险评分: {token.get('risk_level', 'N/A')}")

if __name__ == "__main__":
    main()
