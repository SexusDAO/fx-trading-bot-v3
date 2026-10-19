#!/usr/bin/env python3
"""
复盘之前推送的信号 - 正确版本
用 GMGN 拉取最新数据，计算当前市值
"""

import subprocess
import json
import re

def extract_pushed_tokens():
    """从日志中提取推送过的代币地址"""
    tokens = {}
    with open('/root/.openclaw/workspace/bsc_monitor/monitor.log', 'r') as f:
        content = f.read()
    
    # 找到所有推送信号和对应的 GMGN 链接
    pattern = r'🔔 (🎯||🐋) [^\n]+ \$([^\n]+)\n.*?🔗 https://gmgn\.ai/bsc/token/(0x[a-f0-9]+)'
    matches = re.findall(pattern, content, re.DOTALL)
    
    for emoji, symbol, addr in matches:
        addr_lower = addr.lower()
        if addr_lower not in tokens:
            tokens[addr_lower] = {
                'symbol': symbol,
                'type': 'KOL' if emoji == '🎯' else ('共买' if emoji == '👥' else '鲸鱼'),
                'count': 0
            }
        tokens[addr_lower]['count'] += 1
    
    return tokens

def query_token_status(addr):
    """查询代币当前状态"""
    try:
        result = subprocess.run(
            ['gmgn-cli', 'token', 'info', '--chain', 'bsc', '--address', addr, '--raw'],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode == 0:
            return json.loads(result.stdout)
    except Exception as e:
        print(f"  查询失败：{e}")
    return None

def calc_market_cap(data):
    """计算市值：价格 × 流通供应量"""
    price = float(data.get('price', {}).get('price', 0) or 0)
    supply = float(data.get('circulating_supply', 0) or 0)
    if price > 0 and supply > 0:
        return price * supply
    # 备用：从 ATH 数据获取
    ath_mc = float(data.get('dev', {}).get('ath_token_info', {}).get('ath_mc', 0) or 0)
    if ath_mc > 0:
        return ath_mc
    return 0

def analyze_token(addr, info):
    """分析代币当前状态"""
    if not info:
        return "查询失败", 0, 0, 0, 0
    
    mc = calc_market_cap(info)
    price = float(info.get('price', {}).get('price', 0) or 0)
    liq = float(info.get('pool', {}).get('liquidity', 0) or 0)
    holders = int(info.get('stat', {}).get('holder_count', 0) or 0)
    
    # 判断状态
    if mc == 0:
        status = "归零"
    elif mc < 5000:
        status = "濒危"
    elif mc < 50000:
        status = "低迷"
    elif mc < 200000:
        status = "存活"
    else:
        status = "成功"
    
    return status, mc, price, liq, holders

def main():
    print("=" * 70)
    print("📊 复盘之前推送的信号（用 GMGN 最新数据）")
    print("=" * 70)
    
    tokens = extract_pushed_tokens()
    print(f"\n找到 {len(tokens)} 个推送过的代币\n")
    
    results = {'归零': [], '濒危': [], '低迷': [], '存活': [], '成功': [], '查询失败': []}
    
    for addr, info in list(tokens.items())[:20]:
        symbol = info['symbol']
        push_count = info['count']
        
        print(f"查询 ${symbol} (推送{push_count}次)...")
        
        token_data = query_token_status(addr)
        status, mc, price, liq, holders = analyze_token(addr, token_data)
        
        if token_data:
            print(f"  状态：{status}")
            print(f"  当前市值：${mc:,.0f}")
            print(f"  当前价格：${price:.10f}")
            print(f"  流动性：${liq:,.0f}")
            print(f"  持有人：{holders}")
            print()
        
        results[status].append({
            'symbol': symbol,
            'addr': addr,
            'push_count': push_count,
            'mc': mc,
            'price': price,
            'liq': liq,
            'holders': holders
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
            for t in tokens_list[:5]:
                print(f"  - ${t['symbol']} (推送{t['push_count']}次) 当前 MC ${t['mc']:,.0f}")
    
    # 输出建议
    print("\n" + "=" * 70)
    print("💡 优化建议")
    print("=" * 70)
    
    success_rate = (len(results['成功']) + len(results['存活'])) / total * 100 if total > 0 else 0
    print(f"\n信号成功率（MC>$200K）: {success_rate:.1f}%")
    print(f"归零率：{len(results['归零']) / total * 100:.1f}%")
    
    if len(results['归零']) > 0:
        print("\n归零代币特征:")
        for t in results['归零'][:5]:
            print(f"  ${t['symbol']}: MC ${t['mc']:,.0f}, 持有人 {t['holders']}")

if __name__ == "__main__":
    main()
