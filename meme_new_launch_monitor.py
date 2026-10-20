#!/usr/bin/env python3
"""
Meme 打新监控 - 模拟仓模式
检测 Flap/Four.meme 新币，通过安全过滤后模拟买入并推送到信号监控
"""

import os
import sys
import json
import time
import requests
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set
import logging

# 添加 bsc_monitor 路径以使用推送功能
sys.path.insert(0, '/root/.openclaw/workspace/bsc_monitor')
from notify import notify_md
import card
import safety

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('/root/.openclaw/workspace/meme_new_launch.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# 配置
CONFIG = {
    'poll_interval': 2,  # 轮询间隔（秒）
    'max_token_age_minutes': 120,  # 最大代币年龄（分钟）- 放宽到2小时
    'min_liquidity_bnb': 0.8,  # 最小流动性（BNB）
    'min_liquidity_usd': 500,  # 最小流动性（USD）- 降低门槛捕捉早期机会
    'max_buy_tax': 10,  # 最大买入税（%）
    'max_sell_tax': 15,  # 最大卖出税（%）
    'max_top10_holder': 50,  # 最大前10持有人占比（%）
    'max_creator_holding': 10,  # 最大开发者持仓（%）
    'max_bundler_sniper': 30,  # 最大 bundler+sniper 占比（%）
    'allow_mint': False,  # 是否允许 mint 功能
    'allow_proxy': False,  # 是否允许 proxy 合约
    'simulate_buy': False,  # 真实买入模式（测试）
    'buy_amount_usd': 5,  # 买入金额（USD）
    'buy_amount_bnb': 0.0064,  # 买入金额（BNB）
    # 新增过滤条件
    'min_holders': 20,  # 最少持有人数 - 降低门槛捕捉早期机会
    'min_tx_count': 20,  # 最少交易次数
    'min_kol_or_smart': 2,  # 最少 KOL + 聪明钱持有者（合计≥2人）
    'max_bot_ratio': 0.5,  # 最大 bot/bundler 占比（50%）
    'max_dev_token_count': 500,  # 开发者发币数量上限（超过则拦截）
}

# BNB 地址（BSC）
BNB_ADDRESS = '0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE'

# 用户钱包地址（用于买入后转账）
USER_WALLET_ADDRESS = '0xFb4fC5BCAb0D5d2e731a8FD94933b2894e113D37'

# 开发者黑名单文件
DEV_BLACKLIST_FILE = '/root/.openclaw/workspace/bsc_monitor/dev_blacklist.json'

# 已处理的代币（去重）- 使用 LRU 缓存限制大小
from collections import OrderedDict
processed_tokens = OrderedDict()
MAX_PROCESSED_TOKENS = 5000  # 最多保留 5000 个

# Bench 机制：按拒绝原因设置不同的冷却时间
# 参考 savipww 的 Jev + Grok Bot 设计
BENCH_MINUTES = {
    # 事实性问题，不会改变 - 永久不重试
    'honeypot': 100_000,
    'creator_blacklist': 100_000,
    'dev_blacklist': 100_000,
    'dev_token_count': 100_000,  # 开发者发币过多，不会改变
    'mintable': 100_000,
    'proxy_contract': 100_000,
    
    # 社交问题，改变较慢 - 6小时冷却
    'recycled_account': 360,
    'fake_twitter': 360,
    
    # 链上数据，可能随时间改变 - 90分钟冷却
    'top10_concentration': 90,
    'creator_holding': 90,
    'bot_ratio': 90,
    
    # 市场数据，可能快速改变 - 25分钟冷却
    'liquidity': 25,
    'volume': 25,
    'holders': 25,
    'tx_count': 25,
    'age_too_young': 20,
    'age_too_old': 100_000,  # 老币不会变年轻（永久冷却）
    'no_kol': 25,
    'no_sell': 25,
    'holder_decline': 15,  # 持有人持续减少（清仓信号），15分钟后可重试
    
    # 默认冷却时间
    'default': 45,
}

# Bench 存储：{token_address: {'reason': str, 'until': timestamp}}
bench_storage = {}
MAX_BENCH_SIZE = 10000

def bench_token(token_address: str, reason: str):
    """将代币加入 Bench（冷却名单）"""
    token_address = token_address.lower()
    minutes = BENCH_MINUTES.get(reason, BENCH_MINUTES['default'])
    until = time.time() + minutes * 60
    bench_storage[token_address] = {'reason': reason, 'until': until}
    
    # 限制 Bench 大小
    if len(bench_storage) > MAX_BENCH_SIZE:
        # 移除最早过期的
        oldest = min(bench_storage.items(), key=lambda x: x[1]['until'])
        del bench_storage[oldest[0]]
    
    logger.debug(f"[BENCH] {token_address[:10]}... 加入冷却: {reason}, {minutes}分钟")

def is_benched(token_address: str) -> tuple:
    """检查代币是否在冷却中，返回 (是否冷却, 原因)"""
    token_address = token_address.lower()
    if token_address not in bench_storage:
        return False, None
    
    entry = bench_storage[token_address]
    if time.time() < entry['until']:
        return True, entry['reason']
    else:
        # 已过期，移除
        del bench_storage[token_address]
        return False, None

# 已推送的打新代币（用于信号监控去重）
new_launch_purchased = set()
MAX_PURCHASED_TOKENS = 1000  # 最多保留 1000 个

# KOL/聪明钱追踪：记录每个代币的 KOL/聪明钱数量变化（用于检测新买入）
# 格式: {token_address: [(timestamp, kol_count, smart_count), ...]}
# 保留最近 5 分钟的数据
kol_smart_tracker = {}
MAX_TRACKER_AGE_SECONDS = 300  # 保留 5 分钟数据

# 持有人追踪：记录每个代币的持有人数量变化（用于检测清仓）
# 格式: {token_address: [(timestamp, holder_count), ...]}
# 保留最近 10 分钟的数据
holder_tracker = {}
MAX_HOLDER_TRACKER_AGE_SECONDS = 600  # 保留 10 分钟数据


def update_kol_smart_tracker(token_address: str, kol_count: int, smart_count: int):
    """更新 KOL/聪明钱追踪器"""
    token_address = token_address.lower()
    now = time.time()
    
    if token_address not in kol_smart_tracker:
        kol_smart_tracker[token_address] = []
    
    # 添加新记录
    kol_smart_tracker[token_address].append((now, kol_count, smart_count))
    
    # 清理过期数据（保留最近 5 分钟）
    kol_smart_tracker[token_address] = [
        (ts, k, s) for ts, k, s in kol_smart_tracker[token_address]
        if now - ts < MAX_TRACKER_AGE_SECONDS
    ]


def get_new_kol_smart_buys(token_address: str, minutes: int = 3) -> int:
    """
    获取最近 N 分钟内新增的 KOL/聪明钱数量
    返回新增的 KOL + 聪明钱总数
    """
    token_address = token_address.lower()
    now = time.time()
    
    if token_address not in kol_smart_tracker or len(kol_smart_tracker[token_address]) < 2:
        return 0
    
    records = kol_smart_tracker[token_address]
    
    # 找到 N 分钟前的记录
    target_time = now - minutes * 60
    oldest_in_range = None
    for ts, k, s in records:
        if ts <= target_time:
            oldest_in_range = (ts, k, s)
        else:
            break
    
    if oldest_in_range is None:
        # 没有 N 分钟前的记录，用最早的记录
        oldest_in_range = records[0]
    
    # 最新的记录
    latest = records[-1]
    
    # 计算新增数量
    old_kol = oldest_in_range[1]
    old_smart = oldest_in_range[2]
    new_kol = latest[1]
    new_smart = latest[2]
    
    new_buys = (new_kol - old_kol) + (new_smart - old_smart)
    return max(0, new_buys)  # 不能是负数


def update_holder_tracker(token_address: str, holder_count: int):
    """更新持有人追踪器"""
    token_address = token_address.lower()
    now = time.time()
    
    if token_address not in holder_tracker:
        holder_tracker[token_address] = []
    
    # 添加新记录
    holder_tracker[token_address].append((now, holder_count))
    
    # 清理过期数据（保留最近 10 分钟）
    holder_tracker[token_address] = [
        (ts, h) for ts, h in holder_tracker[token_address]
        if now - ts < MAX_HOLDER_TRACKER_AGE_SECONDS
    ]


def check_holder_decline(token_address: str, minutes: int = 5) -> tuple:
    """
    检查持有人是否持续减少（清仓信号）
    返回 (是否持续减少, 减少百分比, 减少次数)
    
    逻辑：如果最近 N 分钟内持有人持续减少（每次检查都比上次少），
    说明在清仓，应该拦截买入
    """
    token_address = token_address.lower()
    now = time.time()
    
    if token_address not in holder_tracker or len(holder_tracker[token_address]) < 3:
        # 数据不足，无法判断
        return False, 0.0, 0
    
    records = holder_tracker[token_address]
    
    # 获取最近 N 分钟内的记录
    target_time = now - minutes * 60
    recent_records = [(ts, h) for ts, h in records if ts >= target_time]
    
    if len(recent_records) < 3:
        # 数据不足，无法判断
        return False, 0.0, 0
    
    # 检查是否持续减少
    decline_count = 0
    for i in range(1, len(recent_records)):
        if recent_records[i][1] < recent_records[i-1][1]:
            decline_count += 1
    
    # 如果减少次数 >= 总检查次数 - 1（即几乎每次都在减少）
    total_checks = len(recent_records) - 1
    if decline_count >= total_checks and total_checks >= 2:
        # 计算减少百分比
        first_holder = recent_records[0][1]
        last_holder = recent_records[-1][1]
        if first_holder > 0:
            decline_pct = (first_holder - last_holder) / first_holder * 100
        else:
            decline_pct = 0.0
        
        return True, decline_pct, decline_count
    
    return False, 0.0, 0

def load_dev_blacklist() -> Dict[str, Dict]:
    """加载开发者黑名单"""
    if not os.path.exists(DEV_BLACKLIST_FILE):
        return {}
    try:
        with open(DEV_BLACKLIST_FILE, 'r') as f:
            data = json.load(f)
            # 黑名单格式: {"blacklist": {"0x...": {"ath_mc": ..., "token_count": ...}}}
            return data.get('blacklist', {})
    except Exception as e:
        logger.error(f"加载开发者黑名单失败: {e}")
        return {}


def check_creator_history(dev_address: str, blacklist: Dict[str, Dict]) -> bool:
    """
    检查开发者历史
    返回 True = 通过（可以买入），False = 不通过（拦截）
    
    注意：如果没有开发者地址，返回 False（拦截），而不是放行
    这是为了防止垃圾开发者绕过检查
    """
    if not dev_address:
        logger.warning("没有开发者地址，拦截买入（防止垃圾开发者绕过检查）")
        return False  # 没有开发者信息，拦截
    
    dev_address_lower = dev_address.lower()
    
    # 检查是否在黑名单中
    if dev_address_lower in blacklist:
        info = blacklist[dev_address_lower]
        token_count = info.get('token_count', '?')
        ath_mc = info.get('ath_mc', 0)
        logger.warning(f"开发者 {dev_address[:10]}... 在黑名单中: 发币{token_count}个, 历史最高MC ${ath_mc:.0f}")
        return False
    
    return True


def fetch_new_tokens() -> List[Dict]:
    """获取新代币列表（Flap + Four.meme + 热点代币 + FOMO + GMGN）"""
    url = 'https://web3.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/market/token/pulse/rank/list/ai'
    url_hot = 'https://web3.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/market/token/pulse/exclusive/rank/list/ai'
    headers = {
        'Content-Type': 'application/json',
        'Accept-Encoding': 'identity',
        'User-Agent': 'binance-web3/1.1 (Skill)'
    }
    
    all_tokens = []
    
    # 获取内盘新币（rankType=10）- 扩大到500名
    try:
        payload = {
            'chainId': '56',  # BSC
            'rankType': 10,  # New
            'limit': 500
        }
        resp = requests.post(url, json=payload, headers=headers, timeout=10)
        data = resp.json().get('data', [])
        all_tokens.extend(data)
        logger.debug(f"获取内盘新币: {len(data)} 个")
    except Exception as e:
        logger.error(f"获取内盘新币失败: {e}")
    
    # 获取已迁移代币（rankType=30，有DEX流动性）- 扩大到500名
    try:
        payload = {
            'chainId': '56',
            'rankType': 30,  # Migrated
            'limit': 500
        }
        resp = requests.post(url, json=payload, headers=headers, timeout=10)
        data = resp.json().get('data', [])
        all_tokens.extend(data)
        logger.debug(f"获取已迁移代币: {len(data)} 个")
    except Exception as e:
        logger.error(f"获取已迁移代币失败: {e}")
    
    # 获取热点代币（exclusive rank，按热度排序）- 扩大到200名
    try:
        params = {
            'chainId': '56',
            'rankType': 30,  # Migrated
            'page': 1,
            'size': 200
        }
        resp = requests.get(url_hot, params=params, headers=headers, timeout=10)
        data = resp.json().get('data', {}).get('tokens', [])
        # 转换格式以匹配现有处理逻辑
        for t in data:
            # 确保有必要的字段
            if 'createTime' not in t and 'migrateTime' in t:
                t['createTime'] = t.get('migrateTime', 0)
            if 'liquidity' not in t:
                t['liquidity'] = 0
            if 'holders' not in t:
                t['holders'] = 0
            if 'kolHolders' not in t:
                t['kolHolders'] = 0
            if 'smartMoneyHolders' not in t:
                t['smartMoneyHolders'] = 0
            if 'priceChange5m' not in t and 'percentChange' in t:
                t['priceChange5m'] = t.get('percentChange', 0)
        all_tokens.extend(data)
        logger.debug(f"获取热点代币: {len(data)} 个")
    except Exception as e:
        logger.error(f"获取热点代币失败: {e}")
    
    # 获取 FOMO 信号代币（KOL/聪明钱买入）
    try:
        fomo_tokens = fetch_fomo_signals()
        all_tokens.extend(fomo_tokens)
        logger.debug(f"获取FOMO信号代币: {len(fomo_tokens)} 个")
    except Exception as e:
        logger.error(f"获取FOMO信号失败: {e}")
    
    # 获取 GMGN 信号代币（聪明钱/KOL实时买卖）
    try:
        gmgn_tokens = fetch_gmgn_signals()
        all_tokens.extend(gmgn_tokens)
        logger.debug(f"获取GMGN信号代币: {len(gmgn_tokens)} 个")
    except Exception as e:
        logger.error(f"获取GMGN信号失败: {e}")
    
    # 去重（按合约地址）
    seen = set()
    unique_tokens = []
    for t in all_tokens:
        addr = t.get('contractAddress', '').lower()
        if addr and addr not in seen:
            seen.add(addr)
            unique_tokens.append(t)
    
    return unique_tokens


def fetch_fomo_signals() -> List[Dict]:
    """从 FOMO API 获取 KOL/聪明钱买入信号"""
    tokens = []
    try:
        # FOMO REST API - 获取 BSC 链上的最新 alerts
        url = 'https://api.fomoapi.io/v2/alerts?chain=bsc&limit=50'
        headers = {
            'User-Agent': 'Mozilla/5.0',
            'Accept': 'application/json'
        }
        
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code != 200:
            logger.debug(f"FOMO API 返回 {resp.status_code}")
            return tokens
        
        data = resp.json()
        alerts = data.get('alerts', []) or data.get('data', [])
        
        # 统计每个代币的 KOL/聪明钱买入次数
        token_stats = {}
        for alert in alerts:
            if alert.get('type') != 'buy':
                continue
            
            token_addr = alert.get('tokenAddress', '').lower()
            if not token_addr:
                continue
            
            usd = abs(float(alert.get('usdValue', 0) or 0))
            if usd < 100:  # 忽略小额交易
                continue
            
            if token_addr not in token_stats:
                token_stats[token_addr] = {
                    'address': token_addr,
                    'kol_count': 0,
                    'smart_count': 0,
                    'total_usd': 0,
                    'buy_count': 0
                }
            
            token_stats[token_addr]['total_usd'] += usd
            token_stats[token_addr]['buy_count'] += 1
        
        # 获取每个代币的详细信息
        for addr, stats in token_stats.items():
            if stats['buy_count'] < 2:  # 至少2次买入
                continue
            
            # 获取代币基本信息
            try:
                token_info = get_token_info_from_chain(addr)
                if token_info:
                    tokens.append(token_info)
            except Exception as e:
                logger.debug(f"获取代币信息失败 {addr[:10]}: {e}")
    
    except Exception as e:
        logger.error(f"FOMO 信号获取失败: {e}")
    
    return tokens


def fetch_gmgn_signals() -> List[Dict]:
    """从 GMGN 获取聪明钱/KOL 买卖信号"""
    tokens = []
    try:
        # GMGN smartmoney API
        url = 'https://gmgn.ai/defi/quotation/v1/smartmoney/bsc/swaps?limit=50&orderby=profit_usd&direction=desc'
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/json'
        }
        
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code != 200:
            logger.debug(f"GMGN API 返回 {resp.status_code}")
            return tokens
        
        data = resp.json()
        swaps = data.get('data', []) or data.get('items', [])
        
        # 统计每个代币的聪明钱活动
        token_stats = {}
        for swap in swaps:
            token_addr = swap.get('token_address', '').lower()
            if not token_addr:
                continue
            
            tags = swap.get('maker_tags', []) or []
            is_smart = 'smart_degen' in tags or 'smart_money' in tags
            is_kol = 'kol' in tags
            
            if not is_smart and not is_kol:
                continue
            
            usd = abs(float(swap.get('amount_usd', 0) or 0))
            if usd < 100:
                continue
            
            if token_addr not in token_stats:
                token_stats[token_addr] = {
                    'address': token_addr,
                    'kol_count': 0,
                    'smart_count': 0,
                    'total_usd': 0
                }
            
            if is_kol:
                token_stats[token_addr]['kol_count'] += 1
            if is_smart:
                token_stats[token_addr]['smart_count'] += 1
            token_stats[token_addr]['total_usd'] += usd
        
        # 获取每个代币的详细信息
        for addr, stats in token_stats.items():
            if stats['kol_count'] + stats['smart_count'] < 2:
                continue
            
            try:
                token_info = get_token_info_from_chain(addr)
                if token_info:
                    # 补充 KOL/聪明钱数据
                    token_info['kolHolders'] = stats['kol_count']
                    token_info['smartMoneyHolders'] = stats['smart_count']
                    tokens.append(token_info)
            except Exception as e:
                logger.debug(f"获取代币信息失败 {addr[:10]}: {e}")
    
    except Exception as e:
        logger.error(f"GMGN 信号获取失败: {e}")
    
    return tokens


def get_token_info_from_chain(token_address: str) -> Optional[Dict]:
    """从链上/API获取代币基本信息"""
    try:
        # 使用 Binance Web3 API 获取代币信息
        url = 'https://web3.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/market/token/pulse/rank/list/ai'
        headers = {
            'Content-Type': 'application/json',
            'Accept-Encoding': 'identity',
            'User-Agent': 'binance-web3/1.1 (Skill)'
        }
        
        # 尝试从已迁移列表获取
        payload = {'chainId': '56', 'rankType': 30, 'limit': 500}
        resp = requests.post(url, json=payload, headers=headers, timeout=10)
        tokens = resp.json().get('data', [])
        
        for t in tokens:
            if t.get('contractAddress', '').lower() == token_address.lower():
                return t
        
        # 如果不在列表中，尝试从 GoPlus 获取基本信息
        goplus_url = f'https://api.gopluslabs.io/api/v1/token_security/56?contract_addresses={token_address}'
        resp = requests.get(goplus_url, timeout=10)
        data = resp.json()
        
        if data.get('code') == '1':
            result = data.get('result', {}).get(token_address.lower(), {})
            if result:
                # 构建基本代币信息
                return {
                    'contractAddress': token_address,
                    'symbol': result.get('token_symbol', 'UNKNOWN'),
                    'name': result.get('token_name', 'Unknown'),
                    'liquidity': 0,  # GoPlus 不提供流动性
                    'holders': int(result.get('holder_count', 0) or 0),
                    'kolHolders': 0,
                    'smartMoneyHolders': 0,
                    'createTime': 0,  # 未知
                    'marketCap': 0,
                    'priceChange5m': 0,
                }
    
    except Exception as e:
        logger.debug(f"获取代币信息失败 {token_address[:10]}: {e}")
    
    return None


def verify_twitter_account(token: Dict, token_address: str) -> tuple:
    """
    验证 Twitter 账号是否真的是项目方的
    参考 savipww 的 Jev + Grok Bot 设计
    
    返回: (是否通过, 原因, twitter_handle)
    - 通过 = 账号看起来是真的 / 没有 Twitter 信息（不拦截）
    - 不通过 = 明显是假号/回收号
    """
    # 尝试从多个来源获取 Twitter handle
    twitter_handle = None
    
    # 1. 从 Binance API 获取
    description = token.get('description', '') or ''
    websites = token.get('websites', []) or []
    
    # 从 description 中提取 Twitter handle
    import re
    twitter_patterns = [
        r'twitter\.com/([a-zA-Z0-9_]{1,15})',
        r'x\.com/([a-zA-Z0-9_]{1,15})',
        r'@([a-zA-Z0-9_]{1,15})',
    ]
    for pattern in twitter_patterns:
        match = re.search(pattern, description)
        if match:
            twitter_handle = match.group(1)
            break
    
    # 如果没有 Twitter 信息，不拦截（很多新币还没有）
    if not twitter_handle:
        return True, "no_twitter_info", None
    
    # 2. 检查 handle 是否看起来可疑
    handle_lower = twitter_handle.lower()
    
    # 可疑特征：
    suspicious_patterns = [
        # 名字里包含 "fan", "official" (冒充), "group" 等
        'fan', 'official', 'community', 'group', 'news',
        # 名字太通用
        'crypto', 'token', 'coin', 'defi', 'nft',
    ]
    
    # 检查 handle 是否包含可疑词
    for pattern in suspicious_patterns:
        if pattern in handle_lower:
            logger.warning(f"Twitter @{twitter_handle} 包含可疑词: {pattern}")
            return False, "fake_twitter", twitter_handle
    
    # 3. 检查 handle 长度（太短或太长都可疑）
    if len(twitter_handle) < 3 or len(twitter_handle) > 15:
        logger.warning(f"Twitter @{twitter_handle} 长度异常")
        return False, "fake_twitter", twitter_handle
    
    # 4. 尝试获取 Twitter 账号信息（免费 API 有限）
    # 这里只做基本检查，完整验证需要付费 API
    try:
        # 使用 nitter 或其他免费代理检查账号是否存在
        # 注意：这可能被 rate limit
        url = f"https://syndication.twitter.com/srv/timeline-profile/screen-name/{twitter_handle}"
        resp = requests.get(url, timeout=5, headers={'User-Agent': 'Mozilla/5.0'})
        
        if resp.status_code == 404:
            logger.warning(f"Twitter @{twitter_handle} 账号不存在")
            return False, "fake_twitter", twitter_handle
        
        # 账号存在，通过基本检查
        logger.debug(f"Twitter @{twitter_handle} 基本检查通过")
        return True, "twitter_ok", twitter_handle
        
    except Exception as e:
        # 网络错误不拦截，只记录日志
        logger.debug(f"Twitter 验证网络错误: {e}")
        return True, "twitter_check_failed", twitter_handle


def check_token_security(token: Dict, token_address: str) -> Optional[Dict]:
    """
    检查代币安全性
    优先使用 Binance API 数据，GoPlus 作为补充（honeypot 检测）
    返回安全信息字典，失败返回 None
    """
    # 从 Binance API 提取安全数据
    # 优先使用 Binance API 的 devAddress，如果为空再使用 GoPlus 的 creator_address
    binance_dev_address = token.get('devAddress', '')
    
    security = {
        'top10_holder_rate': float(token.get('holdersTop10Percent', 0) or 0),
        'creator_address': binance_dev_address,  # 优先使用 Binance API 的开发者地址
        'holder_count': int(token.get('holders', 0) or 0),
        'buy_tax': float(token.get('taxRateBuy', 0) or 0),
        'sell_tax': float(token.get('taxRateSell', 0) or 0),
        'bundler_percent': float(token.get('bundlerHoldingPercent', 0) or 0),
        'sniper_percent': float(token.get('holdersSniperPercent', 0) or 0),
        'insider_percent': float(token.get('holdersInsiderPercent', 0) or 0),
        'kol_holders': int(token.get('kolHolders', 0) or 0),
        'smart_money_holders': int(token.get('smartMoneyHolders', 0) or 0),
        # 默认值，后续可能从 GoPlus 获取
        'is_honeypot': False,
        'is_mintable': False,
        'is_proxy': False,
        'creator_percent': 0,  # Binance API 没有直接提供，需要从 holders 数据计算
        'creator_created_count': 0,  # 开发者发币数量（从 safety.check 获取）
    }
    
    # 尝试从 GoPlus 获取 honeypot 检测（新币可能有延迟）
    try:
        url = f'https://api.gopluslabs.io/api/v1/token_security/56?contract_addresses={token_address}'
        resp = requests.get(url, timeout=3)  # 缩短超时
        data = resp.json()
        
        if data.get('code') == '1':
            result = data.get('result', {}).get(token_address.lower())
            if result:
                security['is_honeypot'] = result.get('is_honeypot') == '1'
                security['is_mintable'] = result.get('is_mintable') == '1'
                security['is_proxy'] = result.get('is_proxy') == '1'
                # GoPlus 的 creator_percent 更准确
                security['creator_percent'] = float(result.get('creator_percent', '0')) * 100
                # 如果 Binance API 没有返回开发者地址，使用 GoPlus 的
                if not security['creator_address']:
                    security['creator_address'] = result.get('creator_address', '')
                logger.debug(f"GoPlus 数据可用: honeypot={security['is_honeypot']}")
    except Exception as e:
        logger.debug(f"GoPlus 查询失败（可能是新币）: {e}")
        # GoPlus 失败不阻塞，使用 Binance 数据继续
    
    # 从 safety.check() 获取开发者发币数量（creator_created_count）
    try:
        safety_data = safety.check(token_address)
        if safety_data:
            # safety_data 中有 issues 列表，检查是否有 "creator发过N币" 的信息
            # 同时从 GMGN 数据中提取 creator_created_count
            issues = safety_data.get('issues', [])
            for issue in issues:
                if 'creator发过' in str(issue) and '币' in str(issue):
                    # 提取数字
                    import re
                    match = re.search(r'creator发过(\d+)币', str(issue))
                    if match:
                        security['creator_created_count'] = int(match.group(1))
                        logger.debug(f"开发者发币数量: {security['creator_created_count']}")
                        break
    except Exception as e:
        logger.debug(f"safety.check 获取开发者发币数量失败: {e}")
    
    # 更新持有人追踪器
    try:
        holder_count = security.get('holder_count', 0)
        if holder_count > 0:
            update_holder_tracker(token_address, holder_count)
    except Exception as e:
        logger.debug(f"更新持有人追踪器失败: {e}")
    
    return security


def filter_token(token: Dict, security: Dict, dev_blacklist: Dict) -> tuple[bool, str, str]:
    """
    过滤代币
    返回 (是否通过, 原因描述, 原因代码)
    原因代码用于 Bench 机制设置不同的冷却时间
    """
    now = time.time() * 1000
    
    # 1. 检查代币年龄（必须 ≥60秒，但 >3 个 KOL 可豁免；且 ≤10分钟）
    create_time = token.get('createTime', 0)
    age_seconds = (now - create_time) / 1000
    age_minutes = age_seconds / 60
    kol_holders = int(token.get('kolHolders', 0) or 0)
    
    # 最大年龄限制（10分钟）
    if age_minutes > CONFIG['max_token_age_minutes']:
        return False, f"代币年龄 {age_minutes:.1f}分钟 > {CONFIG['max_token_age_minutes']}分钟", 'age_too_old'
    
    # 最小年龄限制（60秒），KOL ≥ 1 可豁免
    if age_seconds < 60 and kol_holders < 1:
        return False, f"代币年龄 {age_seconds:.0f}s < 60s（KOL={kol_holders}人，需要≥1人才能豁免）", 'age_too_young'
    
    # 2. 检查流动性
    liquidity = float(token.get('liquidity', 0) or 0)
    if liquidity < CONFIG['min_liquidity_usd']:
        return False, f"流动性 ${liquidity:.0f} < ${CONFIG['min_liquidity_usd']}", 'liquidity'
    
    # 3. 检查持有人数
    holders = int(token.get('holders', 0) or 0)
    if holders < CONFIG['min_holders']:
        return False, f"持有人 {holders} < {CONFIG['min_holders']}", 'holders'
    
    # 4. 检查交易次数
    total_tx = int(token.get('count', 0) or 0)
    if total_tx < CONFIG['min_tx_count']:
        return False, f"交易次数 {total_tx} < {CONFIG['min_tx_count']}", 'tx_count'
    
    # 5. 检查不能全是买入（必须有卖出）
    buy_count = int(token.get('countBuy', 0) or 0)
    sell_count = int(token.get('countSell', 0) or 0)
    if sell_count == 0:
        return False, f"全是买入（{buy_count}买/0卖），无真实交易", 'no_sell'
    
    # 6. 检查至少有 2 个 KOL/聪明钱持有者（用户要求）
    kol_holders = int(token.get('kolHolders', 0) or 0)
    smart_holders = int(token.get('smartMoneyHolders', 0) or 0)
    useful_count = kol_holders + smart_holders
    if useful_count < CONFIG['min_kol_or_smart']:
        return False, f"KOL+聪明钱 {useful_count}人 < {CONFIG['min_kol_or_smart']}人", 'no_kol'
    
    # 7. 检查 bot/bundler 占比不能太高
    bundler_pct = float(token.get('bundlerHoldingPercent', 0) or 0)
    sniper_pct = float(token.get('holdersSniperPercent', 0) or 0)
    new_wallet_pct = float(token.get('newWalletHoldingPercent', 0) or 0)
    # bot 占比 = bundler + sniper + 新钱包（可能是机器人创建）
    bot_estimate = bundler_pct + sniper_pct
    if holders > 0 and bot_estimate / 100 > CONFIG['max_bot_ratio']:
        return False, f"Bot占比过高: bundler={bundler_pct:.1f}% sniper={sniper_pct:.1f}%", 'bot_ratio'
    
    # 8. 检查 honeypot
    if security['is_honeypot']:
        return False, "Honeypot 检测失败", 'honeypot'
    
    # 9. 检查买卖税
    if security['buy_tax'] > CONFIG['max_buy_tax']:
        return False, f"买入税 {security['buy_tax']:.1f}% > {CONFIG['max_buy_tax']}%", 'honeypot'
    if security['sell_tax'] > CONFIG['max_sell_tax']:
        return False, f"卖出税 {security['sell_tax']:.1f}% > {CONFIG['max_sell_tax']}%", 'honeypot'
    
    # 10. 检查 Top 10 持有人集中度
    if security['top10_holder_rate'] > CONFIG['max_top10_holder']:
        return False, f"Top 10 持有人 {security['top10_holder_rate']:.1f}% > {CONFIG['max_top10_holder']}%", 'top10_concentration'
    
    # 11. 检查开发者持仓（如果有数据）
    if security['creator_percent'] > CONFIG['max_creator_holding']:
        return False, f"开发者持仓 {security['creator_percent']:.1f}% > {CONFIG['max_creator_holding']}%", 'creator_holding'
    
    # 12. 检查 mint 功能
    if not CONFIG['allow_mint'] and security['is_mintable']:
        return False, "合约可增发（mintable）", 'mintable'
    
    # 13. 检查 proxy 合约
    if not CONFIG['allow_proxy'] and security['is_proxy']:
        return False, "Proxy 合约", 'proxy_contract'
    
    # 14. 检查开发者黑名单
    dev_address = security.get('creator_address', '')
    if not check_creator_history(dev_address, dev_blacklist):
        return False, "开发者在黑名单中（工业化流水线）", 'dev_blacklist'
    
    # 14.5. 检查开发者发币数量（超过500个拦截）
    dev_token_count = security.get('creator_created_count', 0)
    if dev_token_count > CONFIG['max_dev_token_count']:
        return False, f"开发者发币{dev_token_count}个 > {CONFIG['max_dev_token_count']}个（工业化流水线）", 'dev_token_count'
    
    # 15. 持有人持续减少检查（清仓信号）
    # 如果币龄 > 5分钟，检查持有人是否持续减少
    if age_minutes > 5:
        token_address = token.get('contractAddress', '')
        is_declining, decline_pct, decline_count = check_holder_decline(token_address, minutes=5)
        
        if is_declining and decline_pct > 10:
            # 持有人持续减少超过 10%，说明在清仓
            return False, f"持有人持续减少{decline_pct:.1f}%（清仓信号，{decline_count}次连续减少）", 'holder_decline'
    
    return True, "通过所有过滤", 'pass'


def simulate_buy(token: Dict, security: Dict):
    """模拟买入并使用标准信号卡片格式推送"""
    symbol = token.get('symbol', 'UNKNOWN')
    name = token.get('name', 'UNKNOWN')
    address = token.get('contractAddress', '')
    protocol = token.get('protocol', 0)
    protocol_name = {2001: 'Four.meme', 2002: 'Flap'}.get(protocol, f'code={protocol}')
    
    liquidity = float(token.get('liquidity', 0) or 0)
    market_cap = float(token.get('marketCap', 0) or 0)
    create_time = token.get('createTime', 0)
    age_minutes = (time.time() * 1000 - create_time) / 1000 / 60
    
    # 提取交易数据
    holders = int(token.get('holders', 0) or 0)
    kol_holders = int(token.get('kolHolders', 0) or 0)
    smart_money_holders = int(token.get('smartMoneyHolders', 0) or 0)
    buy_count = int(token.get('countBuy', 0) or 0)
    sell_count = int(token.get('countSell', 0) or 0)
    
    # 提取价格变化
    price_change_5m = float(token.get('priceChange5m', 0) or 0)
    price_change_1h = float(token.get('priceChange1h', 0) or 0)
    price_change_24h = float(token.get('priceChange24h', 0) or 0)
    
    # 提取成交量
    volume_1h = float(token.get('volume1h', 0) or 0)
    
    # ===== 买入前二次验证：重新获取最新数据，确认KOL/聪明钱还在 =====
    if not CONFIG['simulate_buy']:
        try:
            logger.info(f"买入前二次验证: {symbol}...")
            url_check = 'https://web3.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/market/token/pulse/rank/list/ai'
            headers_check = {
                'Content-Type': 'application/json',
                'Accept-Encoding': 'identity',
                'User-Agent': 'binance-web3/1.1 (Skill)'
            }
            payload_check = {'chainId': '56', 'rankType': 30, 'limit': 500}
            resp_check = requests.post(url_check, json=payload_check, headers=headers_check, timeout=10)
            migrated = resp_check.json().get('data', [])
            
            current = None
            for t in migrated:
                if t.get('contractAddress', '').lower() == address.lower():
                    current = t
                    break
            
            if not current:
                logger.warning(f"⛔ 买入前验证失败: {symbol} 已跌出已迁移代币排名，拦截买入")
                return None
            
            # 重新检查关键条件
            latest_kol = int(current.get('kolHolders', 0) or 0)
            latest_smart = int(current.get('smartMoneyHolders', 0) or 0)
            latest_holders = int(current.get('holders', 0) or 0)
            latest_liq = float(current.get('liquidity', 0) or 0)
            latest_kol_smart = latest_kol + latest_smart
            
            # 验证条件（与初始过滤一致）
            if latest_kol_smart < CONFIG['min_kol_or_smart']:
                logger.warning(f"⛔ 买入前验证失败: {symbol} KOL+聪明钱={latest_kol_smart} < {CONFIG['min_kol_or_smart']}（KOL已走）")
                return None
            
            if latest_holders < CONFIG['min_holders']:
                logger.warning(f"⛔ 买入前验证失败: {symbol} 持有人={latest_holders} < {CONFIG['min_holders']}")
                return None
            
            if latest_liq < CONFIG['min_liquidity_usd']:
                logger.warning(f"⛔ 买入前验证失败: {symbol} 流动性=${latest_liq:.0f} < ${CONFIG['min_liquidity_usd']}")
                return None
            
            # 检查开发者发币数量
            dev_token_count = security.get('creator_created_count', 0)
            if dev_token_count > CONFIG['max_dev_token_count']:
                logger.warning(f"⛔ 买入前验证失败: {symbol} 开发者发币{dev_token_count}个 > {CONFIG['max_dev_token_count']}（工业化流水线）")
                return None
            
            logger.info(f"✅ 买入前验证通过: {symbol} KOL={latest_kol} 聪明钱={latest_smart} 持有人={latest_holders} 流动性=${latest_liq:.0f}")
            
        except Exception as e:
            logger.warning(f"买入前验证异常: {symbol} {e}，继续执行买入")
    
    # 构建触发逻辑描述
    trigger_parts = []
    if kol_holders > 0:
        trigger_parts.append(f"{kol_holders}个KOL持有")
    if smart_money_holders > 0:
        trigger_parts.append(f"{smart_money_holders}个聪明钱持有")
    trigger_parts.append(f"{holders}持有人")
    trigger_parts.append(f"{buy_count}买/{sell_count}卖")
    trigger_logic = " · ".join(trigger_parts)
    
    # 真实买入逻辑
    buy_result = None
    actual_market_cap = market_cap  # 默认使用快照市值
    
    if not CONFIG['simulate_buy']:
        buy_result_data = execute_real_buy(address, symbol)
        buy_result = buy_result_data.get('message', '')
        
        # 使用实际到账后的市值
        if buy_result_data.get('actual_mc', 0) > 0:
            actual_market_cap = buy_result_data['actual_mc']
            logger.info(f"使用实际市值: ${actual_market_cap:.0f} (快照市值: ${market_cap:.0f})")
    
    # 使用 card.py 的标准格式
    md = card.buy_card(
        emoji="🆕",
        category="Meme打新",
        priority="真实买入" if not CONFIG['simulate_buy'] else "模拟仓",
        symbol=symbol,
        trader="系统检测",
        usd=CONFIG['buy_amount_usd'],
        action="真实买入" if not CONFIG['simulate_buy'] else "模拟买入",
        mc=actual_market_cap,  # 使用实际市值
        liq=liquidity,
        vol1h=volume_1h,
        buys_m5=0,  # API 没提供 5m 数据
        sells_m5=0,
        buys_h1=buy_count,
        sells_h1=sell_count,
        price_change_5m=price_change_5m,
        price_change_1h=price_change_1h,
        price_change_24h=price_change_24h,
        age_min=age_minutes,
        extra_lines=[
            f"平台: {protocol_name}",
            f"地址: `{address[:10]}...{address[-6:]}`",
        ] + ([f"买入结果: {buy_result}"] if buy_result else []),
        addr=address,
        url=f"https://dexscreener.com/bsc/{address}",
        launchpad=protocol_name,
        source_tag="打新监控",
        safety=safety.check(address),
        trade_ts=time.time(),
        ts_precise=False,
        description=f"{name}",
        trigger_logic=trigger_logic,
        kol_holders=kol_holders,
        smart_money_holders=smart_money_holders,
        holders=holders,
    )
    
    # 记录到日志
    logger.info(f"{'真实买入' if not CONFIG['simulate_buy'] else '模拟买入'}: {symbol} ({address})")
    
    # 推送到钉钉
    try:
        notify_md(f"🆕 Meme打新 ${symbol}", md)
        logger.info(f"已推送到钉钉: {symbol}")
    except Exception as e:
        logger.error(f"推送失败: {e}")
    
    # 添加到已推送列表（用于去重）
    new_launch_purchased.add(address.lower())
    
    # 限制去重集合大小
    if len(new_launch_purchased) > MAX_PURCHASED_TOKENS:
        # 清除一半旧的
        to_remove = list(new_launch_purchased)[:MAX_PURCHASED_TOKENS // 2]
        for item in to_remove:
            new_launch_purchased.discard(item)
    
    # 保存到文件（供信号监控读取）
    save_simulated_position(token, security, buy_result)
    
    return md


def execute_real_buy(token_address: str, symbol: str) -> dict:
    """执行真实买入并转账到用户钱包，返回买入结果和实际市值"""
    import subprocess
    
    result_data = {
        'success': False,
        'tx_hash': '',
        'actual_mc': 0,
        'message': ''
    }
    
    try:
        # 1. 执行买入（使用 baw market-order swap）
        buy_cmd = [
            'baw', 'market-order', 'swap',
            '--binanceChainId', '56',
            '--fromTokenQty', str(CONFIG['buy_amount_bnb']),
            '--fromToken', BNB_ADDRESS,
            '--toToken', token_address,
            '--slippage', 'auto',
            '--mev', 'true',
            '--gasLevel', 'MEDIUM'
        ]
        
        logger.info(f"执行买入命令: {' '.join(buy_cmd)}")
        result = subprocess.run(buy_cmd, capture_output=True, text=True, timeout=60)
        
        if result.returncode != 0:
            error_msg = f"买入失败: {result.stderr[:200]}"
            logger.error(error_msg)
            result_data['message'] = error_msg
            return result_data
        
        # 解析买入结果
        try:
            buy_data = json.loads(result.stdout)
            tx_hash = buy_data.get('data', {}).get('txHash', 'unknown')
            logger.info(f"买入成功: {symbol} | TX: {tx_hash}")
            result_data['tx_hash'] = tx_hash
        except:
            tx_hash = 'unknown'
            logger.info(f"买入成功: {symbol}")
        
        # 2. 等待 5 秒让交易确认
        time.sleep(5)
        
        # 3. 查询实际到账后的市值（重试3次，每次间隔2秒）
        actual_mc = 0
        for retry in range(3):
            try:
                logger.info(f"查询市值 (尝试 {retry+1}/3)...")
                dexscreener_url = f'https://api.dexscreener.com/latest/dex/tokens/{token_address}'
                dexscreener_resp = requests.get(dexscreener_url, timeout=5)
                dexscreener_data = dexscreener_resp.json()
                
                if dexscreener_data.get('pairs'):
                    # 取第一个交易对的市值
                    actual_mc = float(dexscreener_data['pairs'][0].get('marketCap', 0) or 0)
                    if actual_mc > 0:
                        result_data['actual_mc'] = actual_mc
                        logger.info(f"实际到账市值: ${actual_mc:.0f}")
                        break
                    else:
                        logger.info(f"市值为0，等待2秒后重试...")
                else:
                    logger.info(f"没有找到交易对，等待2秒后重试...")
            except Exception as e:
                logger.warning(f"查询市值失败 (尝试 {retry+1}/3): {e}")
            
            if retry < 2:
                time.sleep(2)
        
        if actual_mc == 0:
            logger.warning(f"无法获取实际市值，使用快照市值")
        
        # 4. 转账到用户钱包
        transfer_cmd = [
            'baw', 'wallet', 'send',
            '--binanceChainId', '56',
            '--tokenAddress', token_address,
            '--recipient', USER_WALLET_ADDRESS,
            '--max',  # 转账全部余额
            '--json'
        ]
        
        logger.info(f"执行转账命令: {' '.join(transfer_cmd)}")
        transfer_result = subprocess.run(transfer_cmd, capture_output=True, text=True, timeout=60)
        
        if transfer_result.returncode != 0:
            error_msg = f"转账失败: {transfer_result.stderr[:200]}"
            logger.error(error_msg)
            result_data['message'] = f"买入成功(TX:{tx_hash[:10]}...) 但{error_msg}"
            result_data['success'] = True
            return result_data
        
        try:
            transfer_data = json.loads(transfer_result.stdout)
            transfer_tx = transfer_data.get('data', {}).get('txHash', 'unknown')
            logger.info(f"转账成功: {symbol} -> {USER_WALLET_ADDRESS[:10]}... | TX: {transfer_tx}")
            result_data['success'] = True
            result_data['message'] = f"✅ 买入+转账成功 (TX:{tx_hash[:10]}...)"
        except:
            logger.info(f"转账成功: {symbol} -> {USER_WALLET_ADDRESS[:10]}...")
            result_data['success'] = True
            result_data['message'] = f"✅ 买入+转账成功 (TX:{tx_hash[:10]}...)"
        
        return result_data
        
    except subprocess.TimeoutExpired:
        error_msg = "买入超时（60秒）"
        logger.error(error_msg)
        result_data['message'] = error_msg
        return result_data
    except Exception as e:
        error_msg = f"买入异常: {str(e)[:200]}"
        logger.error(error_msg)
        result_data['message'] = error_msg
        return result_data


def save_simulated_position(token: Dict, security: Dict, buy_result: str = None):
    """保存模拟仓位到文件"""
    positions_file = '/root/.openclaw/workspace/meme_new_launch_positions.json'
    
    # 加载现有仓位
    positions = {}
    if os.path.exists(positions_file):
        try:
            with open(positions_file, 'r') as f:
                positions = json.load(f)
        except:
            positions = {}
    
    # 添加新仓位
    address = token.get('contractAddress', '').lower()
    positions[address] = {
        'symbol': token.get('symbol', ''),
        'name': token.get('name', ''),
        'protocol': token.get('protocol', 0),
        'buy_price': token.get('price', '0'),
        'buy_time': datetime.now().isoformat(),
        'buy_amount_usd': CONFIG['buy_amount_usd'],
        'liquidity': token.get('liquidity', '0'),
        'market_cap': token.get('marketCap', '0'),
        'holder_count': security.get('holder_count', 0),
        'creator_address': security.get('creator_address', ''),
        'status': 'real' if not CONFIG['simulate_buy'] else 'simulated',
        'buy_result': buy_result,
    }
    
    # 保存
    with open(positions_file, 'w') as f:
        json.dump(positions, f, indent=2)


def process_token(token: Dict, dev_blacklist: Dict):
    """处理单个代币"""
    address = token.get('contractAddress', '').lower()
    symbol = token.get('symbol', 'UNKNOWN')
    
    # 去重检查：只跳过已经推送过的币
    if address in processed_tokens:
        return None
    
    # 检查是否在 Bench 冷却中
    benched, bench_reason = is_benched(address)
    if benched:
        logger.debug(f"{symbol} 在冷却中: {bench_reason}")
        return None
    
    # 安全检查
    security = check_token_security(token, address)
    if not security:
        return None  # 安全检查失败，不标记为已处理，下次再试
    
    # 更新 KOL/聪明钱追踪器（用于检测新买入）
    kol_count = int(token.get('kolHolders', 0) or 0)
    smart_count = int(token.get('smartMoneyHolders', 0) or 0)
    update_kol_smart_tracker(address, kol_count, smart_count)
    
    # Twitter 验证（参考 savipww 的设计）
    twitter_ok, twitter_reason, twitter_handle = verify_twitter_account(token, address)
    if not twitter_ok:
        bench_token(address, twitter_reason)
        logger.warning(f"{symbol} Twitter 验证失败: {twitter_reason} (@{twitter_handle})")
        return None
    
    # 把 Twitter handle 加入 security 数据，供推送使用
    if twitter_handle:
        security['twitter_handle'] = twitter_handle
    
    # 过滤
    passed, reason, reason_code = filter_token(token, security, dev_blacklist)
    if not passed:
        # 未通过过滤，加入 Bench 冷却
        bench_token(address, reason_code)
        logger.info(f"❌ {symbol} 未通过: {reason} (冷却: {reason_code})")
        return None
    
    # 通过所有过滤，标记为已处理
    processed_tokens[address] = True
    
    # 限制缓存大小，防止内存泄漏
    if len(processed_tokens) >= MAX_PROCESSED_TOKENS:
        # 删除最旧的 1000 个
        for _ in range(1000):
            processed_tokens.popitem(last=False)
    
    # 模拟买入
    logger.info(f"✅ {symbol} 通过所有过滤，模拟买入")
    return simulate_buy(token, security)


def check_single_instance():
    """检查是否只有一个实例在运行，防止重复启动"""
    pid_file = '/root/.openclaw/workspace/meme_new_launch_monitor.pid'
    
    # 检查 PID 文件
    if os.path.exists(pid_file):
        try:
            with open(pid_file, 'r') as f:
                old_pid = int(f.read().strip())
            
            # 检查进程是否还在运行
            try:
                os.kill(old_pid, 0)  # 发送信号0检查进程是否存在
                logger.error(f"另一个实例正在运行 (PID: {old_pid})，退出")
                sys.exit(1)
            except OSError:
                # 进程不存在，可以安全启动
                logger.info(f"清理旧的 PID 文件 (PID: {old_pid} 已不存在)")
        except Exception as e:
            logger.warning(f"读取 PID 文件失败: {e}")
    
    # 写入当前 PID
    with open(pid_file, 'w') as f:
        f.write(str(os.getpid()))
    
    logger.info(f"单实例检查通过 (PID: {os.getpid()})")


def main():
    """主循环"""
    # 检查单实例
    check_single_instance()
    
    logger.info("=" * 60)
    logger.info("Meme 打新监控启动（模拟仓模式）")
    logger.info(f"轮询间隔: {CONFIG['poll_interval']} 秒")
    logger.info(f"最大代币年龄: {CONFIG['max_token_age_minutes']} 分钟")
    logger.info(f"最小流动性: ${CONFIG['min_liquidity_usd']}")
    logger.info(f"模拟买入金额: ${CONFIG['buy_amount_usd']}")
    logger.info("=" * 60)
    
    # 加载开发者黑名单
    dev_blacklist = load_dev_blacklist()
    logger.info(f"开发者黑名单: {len(dev_blacklist)} 个地址")
    
    # 加载已买入的代币（不再重复买入）
    positions_file = '/root/.openclaw/workspace/meme_new_launch_positions.json'
    try:
        if os.path.exists(positions_file):
            with open(positions_file, 'r') as f:
                positions = json.load(f)
            for addr in positions.keys():
                processed_tokens[addr] = True
            logger.info(f"已买入代币: {len(positions)} 个（不再重复买入）")
    except Exception as e:
        logger.error(f"加载已买入代币失败: {e}")
    
    loop_count = 0
    
    # 主循环
    while True:
        try:
            loop_count += 1
            
            # 每 100 次循环打印一次状态
            if loop_count % 100 == 0:
                logger.info(f"[状态] 循环 {loop_count} 次 | 已处理 {len(processed_tokens)} 个代币 | 已推送 {len(new_launch_purchased)} 个")
            
            # 获取新代币
            tokens = fetch_new_tokens()
            logger.info(f"[循环 {loop_count}] 获取到 {len(tokens)} 个代币")
            
            # 处理每个代币
            for token in tokens:
                try:
                    message = process_token(token, dev_blacklist)
                    if message:
                        # 这里可以推送到钉钉/信号监控
                        # 暂时只记录到日志
                        logger.info("=" * 60)
                        logger.info(message)
                        logger.info("=" * 60)
                except Exception as e:
                    logger.error(f"处理代币失败: {e}", exc_info=False)
                    continue
            
            # 等待下一次轮询
            time.sleep(CONFIG['poll_interval'])
            
        except KeyboardInterrupt:
            logger.info("监控停止")
            # 清理 PID 文件
            pid_file = '/root/.openclaw/workspace/meme_new_launch_monitor.pid'
            if os.path.exists(pid_file):
                try:
                    os.remove(pid_file)
                    logger.info("已清理 PID 文件")
                except Exception as e:
                    logger.warning(f"清理 PID 文件失败: {e}")
            break
        except Exception as e:
            logger.error(f"主循环错误: {e}", exc_info=False)
            time.sleep(5)


if __name__ == '__main__':
    main()
