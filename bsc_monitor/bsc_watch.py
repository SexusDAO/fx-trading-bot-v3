# ============================================================
# bsc_watch.py — BSC 主流台子新币监控（纯推送，不买卖）
#
# 信号源：
#   1. PancakeSwap V2 Factory PairCreated（零延迟新对）→ 只登记，
#      出现过聪明钱/KOL/fomo 买入行为后才推送（消除无交易新对噪声）
#   2. fomoapi REST alerts（BSC 实时流）→ trade_detail 解析出
#      真实钱包地址，与 fomo_workflow KOL/聪明钱 watchlist 匹配
#   3. GMGN smartmoney / KOL 实时买卖流（带 gmgn 标签）
#
# 推送：钉钉 markdown 卡片（card.py 统一样式）
# ============================================================
import asyncio
import json
import logging
import os
import subprocess
import sys
import time
import threading
from collections import defaultdict
from typing import Optional

import requests
import websockets

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import (
    BSC_WS_RPC, PROXY, PCS_V2_FACTORY, PAIR_CREATED_TOPIC, WBNB, USDT,
    RPC_POLL_SEC, SMARTMONEY_POLL_SEC, FOMO_POLL_SEC, STATUS_INTERVAL_SEC,
    SIGNAL_BUY_USD_MIN, MULTI_BUY_WINDOW_MIN,
    KOL_MIN_USD, SMART_MIN_USD, MULTI_BUY_THRESHOLD, MULTI_BUY_MIN_TOTAL,
    NEW_KOL_MIN_USD, NEW_SMART_MIN_USD, NEW_MULTI_THRESHOLD, NEW_MULTI_MIN_TOTAL,
    EARLY_KOL_MIN_USD, EARLY_SMART_MIN_USD, EARLY_MULTI_THRESHOLD, EARLY_MULTI_MIN_TOTAL,
    WHALE_BUY_USD_MIN, MIN_MC_NOTIFY, MAX_MC_NOTIFY, MAX_TOKEN_AGE_MIN, MIN_LIQUIDITY_USD,
    NEW_MIN_LIQUIDITY_USD, NEW_MIN_MC_USD, NEW_MIN_HOLDERS, NEW_MIN_TX_24H,
    EARLY_MIN_LIQUIDITY_USD, EARLY_MIN_MC_USD, EARLY_MIN_HOLDERS, EARLY_MIN_TX_24H,
    OLD_MIN_LIQUIDITY_USD, OLD_MIN_MC_USD, OLD_MIN_HOLDERS, OLD_MIN_TX_24H,
    MAX_PRICE_CHANGE_5M, MAX_SELL_BUY_RATIO,
    SKIP_CREATOR_TOKENS, HARD_BLOCK_CREATOR_TOKENS, MIN_HOLDERS_NOTIFY,
    SKIP_BUNDLER_RATE, SKIP_BOT_RATE, SKIP_TOP10_RATE,
    STRONG_KOL_COUNT, STRONG_MULTI_COUNT, STRONG_USD,
    MAINSTREAM_LAUNCHPADS, MAINSTREAM_DEX,
    FOMO_WS_URL, SEEN_PAIRS_FILE,
    AUTO_BUY_ENABLED, AUTO_BUY_AMOUNT_BNB, AUTO_BUY_AMOUNT_USD,
    USER_WALLET_ADDRESS, BNB_ADDRESS, HARD_BLOCK_DEV_TOKEN_COUNT,
    AUTO_BUY_POSITIONS_FILE,
)
from notify import notify, notify_md
import card
import fomoapi
import safety
import binance_api


# ============================================================
# Signal log: record all signals (pushed + blocked) for review
# ============================================================
_SIGNAL_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "signal_log.jsonl")

def _log_signal(token, symbol, action, status, reason="", usd=0, label="", gmgn_url=""):
    """Record signal to log file (JSONL format)"""
    try:
        entry = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() + 8*3600)) + " CST",
            "token": token,
            "symbol": symbol,
            "action": action,
            "status": status,
            "reason": reason,
            "usd": usd,
            "label": label,
            "gmgn_url": gmgn_url or f"https://gmgn.ai/bsc/token/{token}",
        }
        with open(_SIGNAL_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        log.debug(f"[LOG] write signal log failed: {e}")




def _risk_flags(token_addr: str, sym: str = "") -> list:
    """检查风险过滤条件，返回触发的风险标签列表（空列表=无风险）。
    不再直接拦截——由调用方决定是静默丢弃还是带风险标注推送。"""
    s = safety.check(token_addr) or {}
    flags = []
    ct = s.get("creator_tokens") or 0
    if ct > SKIP_CREATOR_TOKENS:
        flags.append(f"⚠️ 工厂币: creator 发过 {ct} 个币")
    holders = s.get("holders") or 0
    try:
        holders = int(float(holders))
    except (ValueError, TypeError):
        holders = 0
    if holders and holders < MIN_HOLDERS_NOTIFY:
        flags.append(f"⚠️ 持有人过少: {holders} 人")
    b = s.get("bundler_pct") or 0
    if b >= SKIP_BUNDLER_RATE:
        flags.append(f"⚠️ 捆绑买入: {b:.0f}%")
    bt = s.get("bot_pct") or 0
    if bt >= SKIP_BOT_RATE:
        flags.append(f"⚠️ Bot 占比高: {bt:.0f}%")
    t10 = s.get("top10_gmgn_pct") or 0
    if t10 >= SKIP_TOP10_RATE and holders >= 30:
        flags.append(f"⚠️ 控盘风险: top10 持 {t10:.0f}%")
    if flags:
        log.info(f"[RISK] ${sym} {token_addr[:10]} {'; '.join(flags)}")
    return flags


def _hard_skip(token_addr: str, sym: str = "") -> bool:
    """向后兼容包装：有任意风险标签 → True（弱信号时仍拦截）。"""
    return bool(_risk_flags(token_addr, sym))


def _hard_block_creator(token_addr: str, sym: str = "") -> bool:
    """硬拦截：creator 发币 >200 次 → 直接拦截，不豁免。"""
    s = safety.check(token_addr) or {}
    ct = s.get("creator_tokens") or 0
    if ct > HARD_BLOCK_CREATOR_TOKENS:
        log.info(f"[HARD BLOCK] ${sym} {token_addr[:10]} creator 发过 {ct} 个币 > {HARD_BLOCK_CREATOR_TOKENS}，硬拦截")
        return True
    return False


def _hard_block_dev_blacklist(token_addr: str, sym: str = "") -> bool:
    """硬拦截：开发者在黑名单中（经常发币但历史最高市值从未超过 500K）→ 直接拦截，不豁免。"""
    s = safety.check(token_addr) or {}
    dev_bl = s.get("dev_blacklist") or {}
    if dev_bl.get("blocked"):
        reason = dev_bl.get("reason", "")
        log.info(f"[HARD BLOCK] ${sym} {token_addr[:10]} {reason}，硬拦截")
        return True
    return False


def _hard_block_token_blacklist(token_addr: str, sym: str = "") -> bool:
    """硬拦截：代币本身在黑名单中（用户手动拉黑的特定代币）。"""
    token_bl_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "token_blacklist.json")
    try:
        if os.path.exists(token_bl_path):
            with open(token_bl_path, 'r') as f:
                token_bl = json.load(f)
            if token_addr.lower() in token_bl:
                info = token_bl[token_addr.lower()]
                reason = info.get("reason", "用户手动拉黑")
                log.info(f"[HARD BLOCK] ${sym} {token_addr[:10]} 在代币黑名单中：{reason}")
                return True
    except Exception as e:
        log.warning(f"读取代币黑名单失败: {e}")
    return False


def _hard_block_dev_token_count(token_addr: str, sym: str = "") -> bool:
    """硬拦截：开发者发币数量 > 500 个（工业化流水线）。"""
    s = safety.check(token_addr) or {}
    creator_count = s.get("creator_tokens") or 0
    if creator_count > HARD_BLOCK_DEV_TOKEN_COUNT:
        log.info(f"[HARD BLOCK] ${sym} {token_addr[:10]} 开发者发币 {creator_count} 个 > {HARD_BLOCK_DEV_TOKEN_COUNT}，硬拦截")
        return True
    return False


# ── 自动买入功能 ──
_auto_buy_lock = threading.Lock()
_auto_buy_positions: dict = {}  # token_addr -> {buy_time, usd, symbol, tx_hash}


def _load_auto_buy_positions():
    """加载已自动买入的代币记录"""
    global _auto_buy_positions
    if os.path.exists(AUTO_BUY_POSITIONS_FILE):
        try:
            with open(AUTO_BUY_POSITIONS_FILE, 'r') as f:
                _auto_buy_positions = json.load(f)
            log.info(f"[AUTO-BUY] 加载已买入记录: {len(_auto_buy_positions)} 个")
        except Exception as e:
            log.warning(f"[AUTO-BUY] 加载已买入记录失败: {e}")
            _auto_buy_positions = {}


def _save_auto_buy_positions():
    """保存已自动买入的代币记录"""
    try:
        with open(AUTO_BUY_POSITIONS_FILE, 'w') as f:
            json.dump(_auto_buy_positions, f, indent=2)
    except Exception as e:
        log.warning(f"[AUTO-BUY] 保存已买入记录失败: {e}")


def _has_auto_bought(token_addr: str) -> bool:
    """检查是否已经自动买入过该代币"""
    return token_addr.lower() in _auto_buy_positions


def _auto_buy(token_addr: str, symbol: str, signal_source: str, trigger_info: str) -> bool:
    """
    自动买入代币并转账到用户钱包
    
    Args:
        token_addr: 代币合约地址
        symbol: 代币符号
        signal_source: 信号来源 (fomoapi/gmgn_smartmoney/gmgn_kol)
        trigger_info: 触发信息描述
    
    Returns:
        bool: 是否成功买入
    """
    if not AUTO_BUY_ENABLED:
        log.debug(f"[AUTO-BUY] 自动买入未启用")
        return False
    
    token_addr = token_addr.lower()
    
    # 检查是否已经买入
    if _has_auto_bought(token_addr):
        log.info(f"[AUTO-BUY] ${symbol} 已买入过，跳过")
        return False
    
    with _auto_buy_lock:
        # 再次检查（防止并发）
        if _has_auto_bought(token_addr):
            return False
        
        log.info(f"[AUTO-BUY] 开始买入 ${symbol} ({token_addr[:10]}...)")
        log.info(f"[AUTO-BUY] 信号来源: {signal_source}, 触发: {trigger_info}")
        
        try:
            # 1. 执行买入（使用 baw market-order swap）
            buy_cmd = [
                'baw', 'market-order', 'swap',
                '--binanceChainId', '56',
                '--fromTokenQty', str(AUTO_BUY_AMOUNT_BNB),
                '--fromToken', BNB_ADDRESS,
                '--toToken', token_addr,
                '--slippage', 'auto',
                '--mev', 'true',
                '--gasLevel', 'MEDIUM'
            ]
            
            log.info(f"[AUTO-BUY] 执行买入命令: {' '.join(buy_cmd)}")
            result = subprocess.run(buy_cmd, capture_output=True, text=True, timeout=30)
            
            if result.returncode != 0:
                log.error(f"[AUTO-BUY] 买入失败: {result.stderr}")
                return False
            
            # 解析交易哈希
            try:
                buy_data = json.loads(result.stdout)
                tx_hash = buy_data.get('data', {}).get('txHash', '')
            except:
                tx_hash = ''
            
            log.info(f"[AUTO-BUY] 买入成功: ${symbol}, TX: {tx_hash[:20]}..." if tx_hash else f"[AUTO-BUY] 买入成功: ${symbol}")
            
            # 2. 等待交易确认
            time.sleep(2)
            
            # 3. 转账到用户钱包
            transfer_cmd = [
                'baw', 'wallet', 'send',
                '--binanceChainId', '56',
                '--tokenAddress', token_addr,
                '--recipient', USER_WALLET_ADDRESS,
                '--max',
                '--json'
            ]
            
            log.info(f"[AUTO-BUY] 执行转账命令: {' '.join(transfer_cmd)}")
            transfer_result = subprocess.run(transfer_cmd, capture_output=True, text=True, timeout=30)
            
            transfer_tx = ''
            if transfer_result.returncode == 0:
                try:
                    transfer_data = json.loads(transfer_result.stdout)
                    transfer_tx = transfer_data.get('data', {}).get('txHash', '')
                except:
                    pass
                log.info(f"[AUTO-BUY] 转账成功: ${symbol} -> {USER_WALLET_ADDRESS[:10]}..., TX: {transfer_tx[:20]}..." if transfer_tx else f"[AUTO-BUY] 转账成功: ${symbol}")
            else:
                log.warning(f"[AUTO-BUY] 转账失败: {transfer_result.stderr}")
            
            # 4. 记录买入
            _auto_buy_positions[token_addr] = {
                'buy_time': time.time(),
                'symbol': symbol,
                'usd': AUTO_BUY_AMOUNT_USD,
                'bnb': AUTO_BUY_AMOUNT_BNB,
                'signal_source': signal_source,
                'trigger_info': trigger_info,
                'buy_tx': tx_hash,
                'transfer_tx': transfer_tx,
                'user_wallet': USER_WALLET_ADDRESS
            }
            _save_auto_buy_positions()
            
            log.info(f"[AUTO-BUY] ✅ 完成: ${symbol}, 买入 ${AUTO_BUY_AMOUNT_USD}")
            return True
            
        except subprocess.TimeoutExpired:
            log.error(f"[AUTO-BUY] 买入超时: ${symbol}")
            return False
        except Exception as e:
            log.error(f"[AUTO-BUY] 买入异常: ${symbol}, {e}")
            return False

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger("bsc_watch")

_PROXIES = {"http": PROXY, "https": PROXY}
_SESSION = requests.Session()
_SESSION.proxies = _PROXIES

# ── KOL/聪明钱 watchlist（复用 fomo_workflow 的 641 地址名单 + BSC 动态名单）──
_WL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bsc_kol_wallets_merged.json")
_SMART_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "smart_watchlist.json")
_KOL_MAP: dict = {}          # evm_addr → handle
_KOL_HANDLES: set = set()    # fomo handle（小写）
_BSC_KOL_WALLETS: dict = {}  # BSC 动态名单：wallet → twitter handle（GMGN kol 流自动积累）
_KOL_MAP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bsc_kol_wallets.json")
_WL_LOCK = threading.Lock()


def _load_kol_map():
    global _KOL_MAP, _KOL_HANDLES, _BSC_KOL_WALLETS
    _KOL_MAP = {}
    _KOL_HANDLES = set()
    try:
        data = json.load(open(_WL_PATH))
        for item in data:
            addr = item["evm"].lower()
            handle = item.get("handle", "")
            # 根据标签决定显示名称
            tags = item.get("tags", [])
            if "KOL" in tags:
                label = f"KOL {handle}" if handle else "KOL"
            elif "Smart Money" in tags:
                label = f"聪明钱 {handle}" if handle else "聪明钱"
            else:
                label = f"KOL {handle}" if handle else "KOL"
            _KOL_MAP[addr] = label
            if handle:
                _KOL_HANDLES.add(handle.lower())
        log.info(f"[WL] 合并名单加载: {len(_KOL_MAP)} 个地址")
    except Exception as e:
        log.warning(f"[WL] 合并名单加载失败: {e}")
    # BSC 聪明钱名单（refresh_bsc_watchlist.py 每日更新）
    try:
        data = json.load(open(_SMART_PATH))
        for item in data:
            addr = item["evm"].lower()
            tag = "聪明钱"
            if item.get("label"):
                tag = f"聪明钱 {item['label']}"
            _KOL_MAP[addr] = tag
        n_smart = sum(1 for v in _KOL_MAP.values() if "聪明钱" in v)
        log.info(f"[WL] BSC smart 名单: {n_smart} 个")
    except Exception as e:
        log.warning(f"[WL] BSC smart 名单加载失败: {e}")
    log.info(f"[WL] 名单合计: {len(_KOL_MAP)} 个地址")
    # BSC 动态名单
    if os.path.exists(_KOL_MAP_FILE):
        try:
            _BSC_KOL_WALLETS = json.load(open(_KOL_MAP_FILE))
            log.info(f"[WL] BSC 动态 KOL 名单: {len(_BSC_KOL_WALLETS)} 个")
        except Exception:
            _BSC_KOL_WALLETS = {}


def _save_bsc_kol():
    """只把新增条目 merge 进文件，不覆盖已体检清理过的名单。"""
    try:
        # 读出磁盘当前内容，只追加 _BSC_KOL_WALLETS 里磁盘没有的新地址
        try:
            on_disk = json.load(open(_KOL_MAP_FILE))
        except Exception:
            on_disk = {}
        added = {k: v for k, v in _BSC_KOL_WALLETS.items() if k not in on_disk}
        if added:
            on_disk.update(added)
            json.dump(on_disk, open(_KOL_MAP_FILE, "w"))
    except Exception:
        pass


def _match_kol(wallet: str, handle: str) -> str:
    """匹配 KOL：BSC 动态名单 → fomo 钱包名单 → fomo handle 名单。返回标签或空"""
    w = (wallet or "").lower()
    h = (handle or "").lower()
    if w and w in _BSC_KOL_WALLETS:
        tw = _BSC_KOL_WALLETS[w]
        return f"KOL @{tw}" if tw else "KOL"
    if w and w in _KOL_MAP:
        return _KOL_MAP[w]
    if h and h in _KOL_HANDLES:
        return f"KOL @{handle}"
    return ""


# ── 去重状态 ──
_seen_pairs: set = set()
_seen_alert_ids: set = set()
_seen_gmgn_tx: set = set()
_recent_buys: dict = defaultdict(list)      # token → [(ts, usd, trader, label, position_usd)]
_token_push_log: dict = defaultdict(list)   # token → [ts] 冷却窗口内已推记录
_last_push: dict = {}                        # token → (ts, fingerprint) 同币去重
_og_tokens: dict = {}                        # symbol → first address (OG token)
_kol_first_buy: dict = {}                    # token → set of kol_wallets（记录哪些KOL已买过，用于区分首买/加仓）
_TTL_ALERT = 3600
_TTL_TX = 3600
_TTL_BUY = MULTI_BUY_WINDOW_MIN * 60
_TOKEN_PUSH_MAX_PER_DAY = 3                 # 同币每天（24h 滚动窗口）最多推 3 次
_TOKEN_COOLDOWN_SEC = 1800                   # 同币推送后冷却 30 分钟（前 15 分钟内同币连发最严重）

# ── Binance 信号去重（持久化到磁盘）──
_BINANCE_DEDUP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".binance_dedup.json")
_seen_signal_ids: set = set()       # Smart Money signal IDs
_seen_meme_addrs: set = set()       # Pulse 新币地址
_seen_inflow_addrs: set = set()     # Inflow 排名地址
_binance_push_ts: dict = {}          # addr → last_push_timestamp（跨源去重，同币 24h 不重推）
_BINANCE_PUSH_COOLDOWN = 86400       # 同币 24h 冷却


def _load_binance_dedup():
    """从磁盘加载 Binance 信号去重集合"""
    global _seen_signal_ids, _seen_meme_addrs, _seen_inflow_addrs, _binance_push_ts
    if os.path.exists(_BINANCE_DEDUP_FILE):
        try:
            data = json.load(open(_BINANCE_DEDUP_FILE))
            _seen_signal_ids = set(data.get('signal_ids', []))
            _seen_meme_addrs = set(data.get('meme_addrs', []))
            _seen_inflow_addrs = set(data.get('inflow_addrs', []))
            _binance_push_ts = data.get('push_ts', {})
            # 清理过期的 push_ts（>24h）
            now = time.time()
            _binance_push_ts = {k: v for k, v in _binance_push_ts.items() if now - v < _BINANCE_PUSH_COOLDOWN}
            log.info(f"[BINANCE-DEDUP] 加载: signals={len(_seen_signal_ids)}, memes={len(_seen_meme_addrs)}, inflow={len(_seen_inflow_addrs)}, push_ts={len(_binance_push_ts)}")
        except Exception as e:
            log.warning(f"[BINANCE-DEDUP] 加载失败: {e}")


def _save_binance_dedup():
    """保存 Binance 信号去重集合到磁盘"""
    try:
        data = {
            'signal_ids': list(_seen_signal_ids)[-5000:],
            'meme_addrs': list(_seen_meme_addrs)[-5000:],
            'inflow_addrs': list(_seen_inflow_addrs)[-5000:],
            'push_ts': {k: v for k, v in _binance_push_ts.items() if time.time() - v < _BINANCE_PUSH_COOLDOWN},
        }
        json.dump(data, open(_BINANCE_DEDUP_FILE, 'w'))
    except Exception as e:
        log.debug(f"[BINANCE-DEDUP] 保存失败: {e}")


def _binance_cooldown_ok(addr: str) -> bool:
    """跨源去重：检查同币 24h 内是否已推送（只检查，不更新）"""
    addr = addr.lower()
    now = time.time()
    last = _binance_push_ts.get(addr, 0)
    if now - last < _BINANCE_PUSH_COOLDOWN:
        return False
    return True


def _binance_mark_pushed(addr: str):
    """标记代币已推送（在成功推送后调用）"""
    addr = addr.lower()
    _binance_push_ts[addr] = time.time()


def _load_seen():
    global _seen_pairs, _og_tokens, _kol_first_buy
    if os.path.exists(SEEN_PAIRS_FILE):
        try:
            data = json.load(open(SEEN_PAIRS_FILE))
            if isinstance(data, list):
                _seen_pairs = set(data)
                log.info(f"[SEEN] 加载 {len(_seen_pairs)} 个历史 pair")
            elif isinstance(data, dict):
                # 新格式：包含 OG tokens 映射
                _seen_pairs = set(data.get('pairs', []))
                _og_tokens = data.get('og_tokens', {})
                # 加载 KOL 首买记录
                kol_fb = data.get('kol_first_buy', {})
                _kol_first_buy = {k: set(v) for k, v in kol_fb.items()}
                log.info(f"[SEEN] 加载 {len(_seen_pairs)} 个历史 pair, {len(_og_tokens)} 个 OG tokens, {len(_kol_first_buy)} 个代币的KOL首买记录")
        except Exception:
            _seen_pairs = set()
            _og_tokens = {}
            _kol_first_buy = {}


def _save_seen():
    try:
        # 保存为字典格式，包含 pairs、og_tokens 和 kol_first_buy
        data = {
            'pairs': list(_seen_pairs)[-20000:],
            'og_tokens': _og_tokens,
            'kol_first_buy': {k: list(v) for k, v in _kol_first_buy.items()},
        }
        json.dump(data, open(SEEN_PAIRS_FILE, "w"))
    except Exception:
        pass


def _is_kol_first_buy(token: str, wallet: str) -> bool:
    """检查 KOL 是否首次买入该代币
    
    Returns:
        True - 首次买入（之前没买过）
        False - 加仓（之前买过）
    """
    token = token.lower()
    wallet = wallet.lower()
    
    if token not in _kol_first_buy:
        _kol_first_buy[token] = set()
    
    # 检查是否已买过
    if wallet in _kol_first_buy[token]:
        return False  # 加仓
    
    # 记录为首买
    _kol_first_buy[token].add(wallet)
    return True  # 首买


def _count_kol_first_buys(token: str) -> int:
    """统计该代币的 KOL 首买人数"""
    token = token.lower()
    return len(_kol_first_buy.get(token, set()))


def _check_og_status(symbol: str, address: str) -> str:
    """检查代币是否为 OG 币或分流币。
    
    返回:
        "OG" - 第一个出现的同名代币
        "分流" - 同名代币的仿盘
        "" - 无符号或未记录
    """
    if not symbol or symbol == "?":
        return ""
    
    symbol_upper = symbol.upper()
    address_lower = address.lower()
    
    # 检查是否已记录该符号的 OG 代币
    if symbol_upper in _og_tokens:
        og_address = _og_tokens[symbol_upper]
        if og_address == address_lower:
            return "OG"
        else:
            return "分流"
    else:
        # 首次出现，记录为 OG
        _og_tokens[symbol_upper] = address_lower
        return "OG"


def _push_ok(token: str, fingerprint: str = "") -> bool:
    """同币推送节流：
    1. 30 分钟冷却（冷却期内即使新信号也压住，防止同币连发）
    2. 同币同指纹（钱包+金额±20%）不重推
    3. 冷却期外每币每天（24h 滚动）最多 3 次
    """
    now = time.time()
    addr = token.lower()
    # 同币同指纹去重：金额接近的同钱包买入不重推
    if fingerprint:
        last = _last_push.get(addr)
        if last and now - last[0] < 7200:
            lt, lf = last
            if lf == fingerprint:
                return False
            # 金额接近的同钱包（±20%）视为同信号
            try:
                fa, la = float(fingerprint.rsplit("|", 1)[1]), float(lf.rsplit("|", 1)[1])
                if fingerprint.rsplit("|", 1)[0] == lf.rsplit("|", 1)[0] and la * 0.8 <= fa <= la * 1.2:
                    return False
            except (ValueError, IndexError):
                pass
    # 冷却窗口检查
    ts_list = [t for t in _token_push_log[addr] if now - t < _TOKEN_COOLDOWN_SEC]
    if ts_list:
        _token_push_log[addr] = ts_list
        return False
    # 每天上限（24h 滚动窗口）
    ts_list = [t for t in _token_push_log[addr] if now - t < 86400]
    if len(ts_list) >= _TOKEN_PUSH_MAX_PER_DAY:
        _token_push_log[addr] = ts_list
        return False
    ts_list.append(now)
    _token_push_log[addr] = ts_list
    if fingerprint:
        _last_push[addr] = (now, fingerprint)
    return True


def _record_buy(token: str, usd: float, trader: str, label: str, position_usd: float = 0) -> tuple:
    """记录买入，返回 (multi_count, multi_total, avg_position_usd, kol_count, kol_first_buy_count, useful_count)
    
    kol_first_buy_count: 当前时间窗口内 KOL 首买人数（之前没买过这个币的 KOL 数量）
    useful_count: 当前时间窗口内"有用的人"数量（聪明钱 + KOL + fomo 的独立人数）
    """
    now = time.time()
    addr = token.lower()
    _recent_buys[addr].append((now, usd, trader, label, position_usd))
    _recent_buys[addr] = [b for b in _recent_buys[addr] if now - b[0] < _TTL_BUY]
    buyers = _recent_buys[addr]
    n = len({b[2] for b in buyers})
    total = sum(b[1] for b in buyers)
    # 计算持仓均市值（有 position 数据的买家）
    pos_vals = [b[4] for b in buyers if b[4] and b[4] > 0]
    avg_pos = sum(pos_vals) / len(pos_vals) if pos_vals else 0
    # 计算 KOL 数量（label 中包含 "KOL" 的）
    kol_count = len({b[2] for b in buyers if b[3] and "KOL" in b[3]})
    
    # 检查 KOL 首买（对于 KOL 买入，检查是否首次买入该代币）
    kol_first_buy_count = 0
    for b in buyers:
        if b[3] and "KOL" in b[3]:
            wallet = b[2].lower()
            if _is_kol_first_buy(addr, wallet):
                kol_first_buy_count += 1
    
    # 计算"有用的人"数量（聪明钱 + KOL + fomo 的独立人数）
    # label 包含 "KOL" 或 "聪明钱" 或 "fomo" 的买家
    useful_wallets = set()
    for b in buyers:
        lbl = b[3] or ""
        if "KOL" in lbl or "聪明钱" in lbl or "fomo" in lbl:
            useful_wallets.add(b[2].lower())
    useful_count = len(useful_wallets)
    
    return n, total, avg_pos, kol_count, kol_first_buy_count, useful_count


# ═══════════════════════════════════════════════════════════════
# 行情查询：DexScreener
# ═══════════════════════════════════════════════════════════════
_ds_cache: dict = {}
_DS_TTL = 30


def fetch_pair_data(token: str) -> Optional[dict]:
    addr = token.lower()
    now = time.time()
    if addr in _ds_cache and now - _ds_cache[addr][0] < _DS_TTL:
        return _ds_cache[addr][1]
    try:
        r = _SESSION.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{addr}",
            timeout=10)
        d = r.json()
        pairs = [p for p in (d.get("pairs") or []) if p.get("chainId") == "bsc"]
        if not pairs:
            _ds_cache[addr] = (now, None)
            return None
        best = sorted(pairs,
                      key=lambda x: (x.get("liquidity") or {}).get("usd") or 0,
                      reverse=True)[0]
        _ds_cache[addr] = (now, best)
        return best
    except Exception as e:
        log.debug(f"[DS] {addr[:10]} err {e}")
        return None


def enrich(token: str) -> dict:
    p = fetch_pair_data(token)
    if not p:
        return {}
    age_min = 0
    created = p.get("pairCreatedAt")
    if created:
        age_min = (time.time() * 1000 - created) / 60000
    # 涨跌幅（DexScreener 提供）
    pc = p.get("priceChange") or {}
    # 买卖笔数（用于计算买卖比）
    txns = p.get("txns") or {}
    buys_m5 = (txns.get("m5") or {}).get("buys", 0)
    sells_m5 = (txns.get("m5") or {}).get("sells", 0)
    buys_h1 = (txns.get("h1") or {}).get("buys", 0)
    sells_h1 = (txns.get("h1") or {}).get("sells", 0)
    result = {
        "symbol": p.get("baseToken", {}).get("symbol", "?"),
        "mc": p.get("marketCap") or 0,
        "liq": (p.get("liquidity") or {}).get("usd") or 0,
        "vol1h": (p.get("volume") or {}).get("h1") or 0,
        "vol5m": (p.get("volume") or {}).get("m5") or 0,
        "buys_m5": buys_m5,
        "sells_m5": sells_m5,
        "buys_h1": buys_h1,
        "sells_h1": sells_h1,
        "price_change_5m": pc.get("m5") or 0,
        "price_change_1h": pc.get("h1") or 0,
        "price_change_24h": pc.get("h24") or 0,
        "age_min": age_min,
        "dex": p.get("dexId", "?"),
        "url": p.get("url", ""),
        "price": float(p.get("priceUsd") or 0),
    }
    
    # 补充 Binance Web3 数据（KOL holders, smart money holders 等）
    try:
        bn_dynamic = binance_api.get_token_dynamic("56", token.lower())
        if bn_dynamic:
            result["kol_holders"] = bn_dynamic.get("kolHolders", 0)
            result["smart_money_holders"] = bn_dynamic.get("smartMoneyHolders", 0)
            result["pro_holders"] = bn_dynamic.get("proHolders", 0)
            result["holders"] = bn_dynamic.get("holders", 0)
            result["kol_holding_pct"] = bn_dynamic.get("kolHoldingPercent", 0)
            result["smart_money_holding_pct"] = bn_dynamic.get("smartMoneyHoldingPercent", 0)
            result["top10_pct"] = bn_dynamic.get("top10HoldersPercentage", 0)
            # 如果 DexScreener 没有 holders 数据，用 Binance 的
            if not result.get("holders", 0):
                result["holders"] = bn_dynamic.get("holders", 0)
    except Exception as e:
        log.debug(f"[ENRICH-BINANCE] {token[:10]} err: {e}")
    
    return result


# ═══════════════════════════════════════════════════════════════
# 代币介绍 & 触发逻辑查询
# ═══════════════════════════════════════════════════════════════
_desc_cache: dict = {}  # token_addr → description
_trigger_cache: dict = {}  # symbol → (ts, trigger_text)


def _get_token_description(token_addr: str) -> str:
    """获取代币简介：优先 Binance Token Metadata，其次 GMGN，最后 '暂无介绍'。"""
    addr = token_addr.lower()
    now = time.time()
    if addr in _desc_cache and now - _desc_cache[addr][0] < 3600:
        return _desc_cache[addr][1]
    
    desc = ""
    
    # 1. 优先从 Binance Web3 API 获取 description
    try:
        desc = binance_api.get_token_description("56", addr)
    except Exception as e:
        log.debug(f"[DESC-BINANCE] {addr[:10]} err: {e}")
    
    # 2. 如果 Binance 没有，回退到 GMGN CLI
    if not desc:
        try:
            r = subprocess.run(
                ["gmgn-cli", "token", "info", "--chain", "bsc", "--address", addr, "--raw"],
                capture_output=True, text=True, timeout=15
            )
            if r.returncode == 0:
                data = json.loads(r.stdout)
                link = data.get("link", {})
                gmgn_desc = link.get("description", "") or ""
                twitter = link.get("twitter_username", "") or ""
                if gmgn_desc:
                    desc = gmgn_desc
                elif twitter:
                    desc = f"Twitter: @{twitter}"
        except Exception as e:
            log.debug(f"[DESC-GMGN] {addr[:10]} err: {e}")
    
    if not desc:
        desc = "暂无介绍"
    
    _desc_cache[addr] = (now, desc)
    return desc


def _find_trigger_logic(symbol: str) -> str:
    """搜索代币拉盘原因，返回触发逻辑文本。使用 Binance Square 搜索。"""
    now = time.time()
    if symbol in _trigger_cache and now - _trigger_cache[symbol][0] < 1800:
        return _trigger_cache[symbol][1]
    
    trigger = ""
    try:
        # 使用 binance-pro-cli square search 搜索代币相关帖子
        import subprocess
        result = subprocess.run(
            ['/usr/local/bin/binance-pro-cli', 'square', 'search', 
             '--window', '4h', '--keyword', symbol, '--format', 'json', '--page-size', '10'],
            capture_output=True, text=True, timeout=10
        )
        
        if result.returncode == 0 and result.stdout:
            data = json.loads(result.stdout)
            items = data.get('items', [])
            
            if items:
                # 分析帖子内容，提取触发原因
                keywords_map = {
                    'listing': '上线交易所',
                    'listed': '上线交易所',
                    'partnership': '合作伙伴关系',
                    'partners': '合作伙伴关系',
                    'announcement': '官方公告',
                    'announce': '官方公告',
                    'binance': '币安相关',
                    'bnb chain': 'BNB Chain 生态',
                    'summit': '峰会活动',
                    'conference': '会议活动',
                    'airdrop': '空投活动',
                    'burn': '销毁机制',
                    'upgrade': '技术升级',
                    'mainnet': '主网上线',
                    'launch': '项目发布',
                    'celebrity': '名人效应',
                    'elon': '名人效应',
                    'whale': '鲸鱼买入',
                    'smart money': '聪明钱买入',
                }
                
                # 统计关键词出现次数
                keyword_counts = {}
                for item in items[:10]:  # 只看前10条
                    body = item.get('body', '').lower()
                    for kw, desc in keywords_map.items():
                        if kw in body:
                            keyword_counts[desc] = keyword_counts.get(desc, 0) + 1
                
                # 找到最常见的触发原因
                if keyword_counts:
                    top_reason = max(keyword_counts, key=keyword_counts.get)
                    trigger = f"{top_reason}（{keyword_counts[top_reason]}条相关讨论）"
                else:
                    # 没有找到明确关键词，但有帖子讨论
                    trigger = f"社区热议（{len(items)}条相关帖子）"
    except Exception as e:
        log.debug(f"[TRIGGER] {symbol} err: {e}")
    
    _trigger_cache[symbol] = (now, trigger)
    return trigger


# ═══════════════════════════════════════════════════════════════
# 信号源 1：PancakeSwap V2 PairCreated（新对登记，行为触发后才推）
# ═══════════════════════════════════════════════════════════════
async def rpc_pair_loop():
    """扫最新块的新对并登记。推送逻辑：登记的币出现任何信号（buy/sell/gmgn）时补全信息推送。"""
    import websocket as _ws
    log.info("[RPC] PairCreated 扫描启动")

    def _sync_scan():
        ws = _ws.create_connection(BSC_WS_RPC, timeout=12)
        ws.send(json.dumps({"jsonrpc": "2.0", "id": 1,
                            "method": "eth_blockNumber", "params": []}))
        blk = int(json.loads(ws.recv())["result"], 16)
        ws.send(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "eth_getLogs",
                            "params": [{
                                "fromBlock": hex(blk - 12), "toBlock": hex(blk),
                                "address": PCS_V2_FACTORY,
                                "topics": [PAIR_CREATED_TOPIC]}]}))
        res = json.loads(ws.recv())
        logs = res.get("result") or []
        new_tokens = []
        for l in logs:
            t0 = "0x" + l["topics"][1][26:66]
            t1 = "0x" + l["topics"][2][26:66]
            for t in (t0, t1):
                if t.lower() in (WBNB, USDT):
                    continue
                if t not in _seen_pairs:
                    _seen_pairs.add(t)
                    new_tokens.append(t)
        ws.close()
        return new_tokens

    while True:
        try:
            tokens = await asyncio.get_event_loop().run_in_executor(None, _sync_scan)
            if tokens:
                log.info(f"[RPC] 本轮新对登记 {len(tokens)} 个")
                _save_seen()
        except Exception as e:
            log.warning(f"[RPC] scan err: {e}")
        await asyncio.sleep(RPC_POLL_SEC)


# ═══════════════════════════════════════════════════════════════
# 信号源 2：fomoapi REST alerts（BSC）+ trade 详情 → KOL 匹配
# ═══════════════════════════════════════════════════════════════
def _too_old(info: dict) -> bool:
    """发币时间超过 MAX_TOKEN_AGE_MIN 的老币不推（0 = 不过滤）"""
    if not MAX_TOKEN_AGE_MIN:
        return False
    age = (info or {}).get("age_min") or 0
    if age and age > MAX_TOKEN_AGE_MIN:
        log.debug(f"[AGE] 过滤老币: 币龄 {age/1440:.1f} 天 > {MAX_TOKEN_AGE_MIN/1440:.0f} 天")
        return True
    return False


def _already_pumped(info: dict, sym: str = "") -> bool:
    """已经拉爆的币不推（5分钟涨幅 > MAX_PRICE_CHANGE_5M）
    只对老币（>4小时）生效，新币不限制"""
    if not info or not MAX_PRICE_CHANGE_5M:
        return False
    # 新币（<4小时）不做此过滤
    age_min = info.get("age_min") or 0
    if age_min < 240:  # 4小时 = 240分钟
        return False
    pc5 = info.get("price_change_5m") or 0
    if pc5 > MAX_PRICE_CHANGE_5M:
        log.debug(f"[PUMP] 过滤已拉爆老币: ${sym} 币龄{age_min/60:.1f}h 5m涨幅{pc5:.0f}% > {MAX_PRICE_CHANGE_5M}%")
        return True
    return False


def _heavy_sell_pressure(info: dict, sym: str = "") -> bool:
    """抛压太重不推（5分钟卖出笔数 > 买入笔数 * MAX_SELL_BUY_RATIO）
    只对老币（>4小时）生效，新币不限制"""
    if not info or not MAX_SELL_BUY_RATIO:
        return False
    # 新币（<4小时）不做此过滤
    age_min = info.get("age_min") or 0
    if age_min < 240:  # 4小时 = 240分钟
        return False
    buys = info.get("buys_m5") or 0
    sells = info.get("sells_m5") or 0
    if buys >= 10 and sells > buys * MAX_SELL_BUY_RATIO:
        log.debug(f"[SELL] 过滤抛压老币: ${sym} 币龄{age_min/60:.1f}h 5m买{buys}笔 卖{sells}笔 比值{sells/buys:.1f}x")
        return True
    return False


def _get_tier_thresholds(token_addr: str, info: dict) -> dict:
    """根据代币年龄和是否首次出现，返回对应的门槛配置。
    
    分龄策略：
    - 新币（< 4小时 + 首次出现）：KOL $200, 聪明钱 $500, 共买 2人+$500
    - 早期币（4-24小时）：KOL $1000, 聪明钱 $1500, 共买 3人+$2000
    - 老币（> 24小时）：KOL $3000, 聪明钱 $3000, 共买 3人+$5000
    """
    age_min = (info or {}).get("age_min") or 0
    # 判断是否首次出现：从未在 _seen_pairs 中记录过
    is_new_token = token_addr.lower() not in _seen_pairs
    
    # 新币：币龄 < 4小时 + 首次出现
    if age_min < 240 and is_new_token:
        return {
            "tier": "new",
            "kol_min": NEW_KOL_MIN_USD,
            "smart_min": NEW_SMART_MIN_USD,
            "multi_threshold": NEW_MULTI_THRESHOLD,
            "multi_min_total": NEW_MULTI_MIN_TOTAL,
        }
    # 早期币：4-24小时
    elif 240 <= age_min < 1440:
        return {
            "tier": "early",
            "kol_min": EARLY_KOL_MIN_USD,
            "smart_min": EARLY_SMART_MIN_USD,
            "multi_threshold": EARLY_MULTI_THRESHOLD,
            "multi_min_total": EARLY_MULTI_MIN_TOTAL,
        }
    # 老币：> 24小时
    else:
        return {
            "tier": "old",
            "kol_min": KOL_MIN_USD,
            "smart_min": SMART_MIN_USD,
            "multi_threshold": MULTI_BUY_THRESHOLD,
            "multi_min_total": MULTI_BUY_MIN_TOTAL,
        }


def _check_data_quality(token_addr: str, info: dict, tier: str) -> tuple:
    """检查代币数据质量，返回 (通过, 失败原因)。
    
    数据质量门槛（分龄）：
    - 新币：流动性≥$1500, 市值≥$5000, 持有人≥30, 24h交易≥50tx
    - 早期币：流动性≥$2500, 市值≥$15000, 持有人≥80, 24h交易≥150tx
    - 老币：流动性≥$3000, 市值≥$30000, 持有人≥150, 24h交易≥300tx
    """
    if not info:
        return False, "no_data"
    
    # 根据 tier 选择门槛
    if tier == "new":
        min_liq = NEW_MIN_LIQUIDITY_USD
        min_mc = NEW_MIN_MC_USD
        min_holders = NEW_MIN_HOLDERS
        min_tx = NEW_MIN_TX_24H
    elif tier == "early":
        min_liq = EARLY_MIN_LIQUIDITY_USD
        min_mc = EARLY_MIN_MC_USD
        min_holders = EARLY_MIN_HOLDERS
        min_tx = EARLY_MIN_TX_24H
    else:  # old
        min_liq = OLD_MIN_LIQUIDITY_USD
        min_mc = OLD_MIN_MC_USD
        min_holders = OLD_MIN_HOLDERS
        min_tx = OLD_MIN_TX_24H
    
    # 检查流动性
    liq = info.get("liq") or 0
    if liq < min_liq:
        return False, f"liq<{min_liq}"
    
    # 检查市值
    mc = info.get("mc") or 0
    if mc < min_mc:
        return False, f"mc<{min_mc}"
    
    # 检查持币人数（从 safety 数据获取）
    safe = safety.check(token_addr) or {}
    holders = safe.get("holders") or 0
    try:
        holders = int(float(holders))
    except (ValueError, TypeError):
        holders = 0
    if holders < min_holders:
        return False, f"holders<{min_holders}"
    
    # 检查 24h 交易数（从 DexScreener 数据获取）
    # 注意：DexScreener 返回的是 buys + sells
    buys_h1 = info.get("buys_h1") or 0
    sells_h1 = info.get("sells_h1") or 0
    tx_1h = buys_h1 + sells_h1
    # 估算 24h 交易数（假设 1h 交易量均匀分布）
    tx_24h_est = tx_1h * 24
    if tx_24h_est < min_tx:
        return False, f"tx_24h<{min_tx}"
    
    return True, ""


def _handle_alert(a: dict):
    """处理单条 alert：解析钱包、匹配 KOL、推送"""
    global _pushes_last_hour
    atype = a.get("type")
    trader = a.get("trader") or "?"
    token_addr = (a.get("tokenAddress") or "").lower()
    usd = abs(float(a.get("usdValue") or 0))
    tid = a.get("tradeId") or ""
    if not token_addr:
        return

    # 拉 trade 详情（带 wallet + liquidity）
    wallet = ""
    info = enrich(token_addr)
    trade_ts = None
    if tid:
        td = fomoapi.trade_detail(tid)
        if td:
            wallet = (td.get("trader") or {}).get("wallet") or ""
            liq = td.get("token", {}).get("liquidityUsd")
            if liq and not info.get("liq"):
                info["liq"] = float(liq)
            # 链上买入时间（block timestamp 秒）
            bts = td.get("timestamp") or td.get("blockTimestamp") or (td.get("token") or {}).get("timestamp")
            try:
                if bts:
                    bts = float(bts)
                    trade_ts = bts if bts < 1e12 else bts / 1000  # 毫秒归一化为秒
            except (ValueError, TypeError):
                pass

    # KOL 匹配：trade wallet（动态名单+RH名单）→ fomo handle 名单兜底
    label = _match_kol(wallet, trader)

    is_buy = (atype == "buy")

    if is_buy:
        multi_n, multi_total, avg_pos, kol_count, kol_first_n, useful_count = _record_buy(token_addr, usd, wallet or trader, label or "fomo", float(a.get('positionValueUsd') or 0))
        is_smart_no_kol = (label or "").startswith("聪明钱") and not label.startswith("KOL")
        
        # 根据代币年龄和是否首次出现，使用不同门槛
        tier_cfg = _get_tier_thresholds(token_addr, info)
        tier_label = tier_cfg["tier"]
        
        # 新币场景：KOL 必须是首次买入才算信号
        # 如果是加仓（kol_first_n == 0），需要走其他触发场景（如多人共买、大额鲸鱼）
        if tier_label == "new" and label and "KOL" in label:
            # 新币 + KOL 买入：必须是首买
            if kol_first_n < 1:
                # 加仓，不算新币信号，但可以走其他场景
                log.debug(f"[NEW-TOKEN] ${info.get('symbol','')} KOL 加仓（非首买），跳过新币信号")
                _log_signal(token_addr, info.get("symbol",""), "buy", "blocked", f"kol_add_position({tier_label})", usd, label or "")
                return
        
        # 打新场景质量门槛：最少2个"有用的人"买入（聪明钱+KOL+fomo）
        if tier_label == "new" and useful_count < 2:
            log.debug(f"[NEW-TOKEN] ${info.get('symbol','')} 有用人数 {useful_count} < 2，信号质量不足")
            _log_signal(token_addr, info.get("symbol",""), "buy", "blocked", f"useful_count<2({useful_count})", usd, label or "")
            return
        
        push = (
            (label and usd >= tier_cfg["kol_min"]) or            # KOL/聪明钱（分龄门槛）
            usd >= WHALE_BUY_USD_MIN or                          # 大额鲸鱼
            (kol_count >= 2 and multi_total >= tier_cfg["multi_min_total"])  # 至少 2 个 KOL 共买且总额达标
        )
        # 聪明钱单笔低于门槛不推（小额散买噪声）
        if is_smart_no_kol and usd < tier_cfg["smart_min"]:
            push = False
        # 聪明钱必须有至少一个 KOL 跟买才推（纯聪明钱不推）
        if is_smart_no_kol and kol_count < 1:
            push = False
            log.debug(f"[SMART-NO-KOL] ${info.get('symbol','')} 聪明钱买入但无 KOL 跟买，拦截")
        if not push:
            _log_signal(token_addr, info.get("symbol",""), "buy", "blocked", f"not_push({tier_label})", usd, label or "")
            return
        if not _push_ok(token_addr, f"{wallet or trader}|{usd:.0f}"):
            _log_signal(token_addr, info.get("symbol",""), "buy", "blocked", "cooldown/dup", usd, label or "")
            return
        # 数据质量检查（分龄门槛）
        quality_pass, quality_reason = _check_data_quality(token_addr, info, tier_label)
        if not quality_pass:
            _log_signal(token_addr, info.get("symbol",""), "buy", "blocked", f"quality({quality_reason})", usd, label or "")
            return
        if info and info.get("mc", 0) > MAX_MC_NOTIFY:
            _log_signal(token_addr, info.get("symbol",""), "buy", "blocked", f"mc>{MAX_MC_NOTIFY}", usd, label or "")
            return
        if _too_old(info):
            _log_signal(token_addr, info.get("symbol",""), "buy", "blocked", "too_old", usd, label or "")
            return
        # 硬拦截：creator 发币 >200 次
        if _hard_block_creator(token_addr, info.get("symbol", "")):
            _log_signal(token_addr, info.get("symbol",""), "buy", "blocked", "hard_block_creator", usd, label or "")
            return
        # 硬拦截：开发者在黑名单中
        if _hard_block_dev_blacklist(token_addr, info.get("symbol", "")):
            _log_signal(token_addr, info.get("symbol",""), "buy", "blocked", "hard_block_dev_blacklist", usd, label or "")
            return
        # 硬拦截：代币本身在黑名单中
        if _hard_block_token_blacklist(token_addr, info.get("symbol", "")):
            _log_signal(token_addr, info.get("symbol",""), "buy", "blocked", "hard_block_token_blacklist", usd, label or "")
            return
        # 硬拦截：开发者发币数量 > 500 个
        if _hard_block_dev_token_count(token_addr, info.get("symbol", "")):
            _log_signal(token_addr, info.get("symbol",""), "buy", "blocked", "hard_block_dev_token_count", usd, label or "")
            return
        if _already_pumped(info, info.get("symbol", "")):
            _log_signal(token_addr, info.get("symbol",""), "buy", "blocked", "already_pumped", usd, label or "")
            return
        if _heavy_sell_pressure(info, info.get("symbol", "")):
            _log_signal(token_addr, info.get("symbol",""), "buy", "blocked", "heavy_sell", usd, label or "")
            return
        sym = info.get("symbol") or (a.get("token") or token_addr[:8])
        # 软过滤器：检查风险标注，强信号时豁免拦截
        risk_flags = _risk_flags(token_addr, sym)
        if risk_flags:
            # 判断是否为强信号（豁免条件：多 KOL 共买 / 多人共买 / 超大单）
            kol_count = sum(1 for b in _recent_buys.get(token_addr, []) if len(b) >= 4 and b[3] and "KOL" in b[3])
            
            # 特殊规则：持有人过少（<50人）需要至少 3 个 KOL 才能豁免
            holders = (safety.check(token_addr) or {}).get("holders") or 0
            try:
                holders = int(float(holders))
            except (ValueError, TypeError):
                holders = 0
            
            if holders < 50 and kol_count < 3:
                # 持有人<50 且 KOL<3，直接拦截
                return
            
            is_strong = (kol_count >= STRONG_KOL_COUNT
                         or multi_n >= STRONG_MULTI_COUNT
                         or usd >= STRONG_USD)
            if not is_strong:
                return  # 弱信号 + 有风险 → 静默丢弃
            # 强信号豁免：继续推送，风险标注会在 extra_lines 里展示
            log.info(f"[BYPASS] ${sym} 强信号豁免风险过滤: {'; '.join(risk_flags)}")
        if label:
            emoji, cat, pri = "🎯", "KOL/聪明钱买入", "🔴 高"
        elif kol_count >= 2 and usd < WHALE_BUY_USD_MIN:
            emoji, cat, pri = "👥", "多人共买", " 中"
        else:
            emoji, cat, pri = "🐋", "鲸鱼买入", "🟡 中"
        src = "fomoapi"
        if label:
            src = f"fomoapi · {label}"
        
        # 检查 OG 状态
        og_status = _check_og_status(sym, token_addr)
        og_tag = " 🔥 OG" if og_status == "OG" else (" ⚠️ 分流" if og_status == "分流" else "")
        
        # 获取项目介绍和触发逻辑
        description = _get_token_description(token_addr)
        trigger_logic = _find_trigger_logic(sym)
        
        md = card.buy_card(
            emoji=emoji, category=cat + og_tag, priority=pri, symbol=sym,
            trader=trader, usd=usd, mc=info.get("mc", 0), liq=info.get("liq", 0),
            vol1h=info.get("vol1h", 0), vol5m=info.get("vol5m", 0),
            buys_m5=info.get("buys_m5", 0), sells_m5=info.get("sells_m5", 0),
            buys_h1=info.get("buys_h1", 0), sells_h1=info.get("sells_h1", 0),
            price_change_5m=info.get("price_change_5m", 0),
            price_change_1h=info.get("price_change_1h", 0),
            price_change_24h=info.get("price_change_24h", 0),
            age_min=info.get("age_min", 0),
            multi_count=multi_n, multi_total=multi_total,
            avg_position=avg_pos,
            addr=token_addr, url=info.get("url", ""),
            launchpad=_guess_launchpad(info), source_tag=src,
            safety=safety.check(token_addr),
            extra_lines=risk_flags or None,
            trade_ts=trade_ts, ts_precise=bool(trade_ts),
            description=description, trigger_logic=trigger_logic,
            kol_holders=info.get("kol_holders", 0),
            smart_money_holders=info.get("smart_money_holders", 0),
            holders=info.get("holders", 0),
        )
        notify_md(f"{emoji} {cat} ${sym}", md)
        _log_signal(token_addr, sym, "buy", "pushed", source_tag or "fomoapi", usd, label or "")
        _pushes_last_hour += 1
        # 自动买入（仅买入信号，不处理卖出）
        if is_buy and AUTO_BUY_ENABLED:
            trigger_desc = f"{cat}|{label or source_tag}|${usd:.0f}|{multi_n}人共买${multi_total:.0f}"
            _auto_buy(token_addr, sym, source_tag or "fomoapi", trigger_desc)
    else:
        # 卖出：仅 KOL 命中或大额
        if not (label or usd >= WHALE_BUY_USD_MIN):
            return
        if not _push_ok(token_addr, f"sell|{wallet or trader}|{usd:.0f}"):
            _log_signal(token_addr, info.get("symbol",""), "sell", "blocked", "cooldown/dup", usd, label or "")
            return
        if info and info.get("mc", 0) > MAX_MC_NOTIFY:
            _log_signal(token_addr, info.get("symbol",""), "sell", "blocked", f"mc>{MAX_MC_NOTIFY}", usd, label or "")
            return
        if _too_old(info):
            _log_signal(token_addr, info.get("symbol",""), "sell", "blocked", "too_old", usd, label or "")
            return
        sym = info.get("symbol") or (a.get("token") or token_addr[:8])
        # 卖出：有风险标注且不是超大单 → 静默丢弃
        _sell_flags = _risk_flags(token_addr, sym)
        if _sell_flags and usd < STRONG_USD:
            return
        extra = []
        if label:
            extra.append(f"来源: {label}")
#         md = card.sell_card(symbol=sym, trader=trader, usd=usd,
#                             mc=info.get("mc", 0), liq=info.get("liq", 0),
#                             url=info.get("url", ""), addr=token_addr,
#                             extra_lines=extra, safety=safety.check(token_addr),
#                             trade_ts=trade_ts, ts_precise=bool(trade_ts))
#         notify_md(f" 大额卖出 ${sym}", md)
#         _log_signal(token_addr, sym, "sell", "pushed", "fomoapi", usd, label or "")
#         _pushes_last_hour += 1


def _guess_launchpad(info: dict) -> str:
    """从 dexscreener 信息里猜台子（简单版：查 pairs dexId）"""
    dex = (info.get("dex") or "").lower()
    for k in MAINSTREAM_DEX:
        if k in dex:
            return "pancakeswap"
    return ""


def fomo_rest_loop():
    """fomoapi REST 轮询（每 15s）"""
    log.info("[FOMO-REST] BSC alerts 轮询启动")
    cursor = ""
    while True:
        try:
            d = fomoapi.alerts_since(cursor, chain="bsc")
            alerts = d.get("alerts") or []
            if not alerts:
                time.sleep(FOMO_POLL_SEC)
                continue
            # 只处理最新的一条游标之后的部分：简单做法是全量去重处理
            fresh = 0
            for a in alerts:
                aid = a.get("id") or a.get("eventId") or ""
                if not aid or aid in _seen_alert_ids:
                    continue
                _seen_alert_ids.add(aid)
                # 内存控制
                if len(_seen_alert_ids) > 50000:
                    _seen_alert_ids.clear()
                fresh += 1
                try:
                    _handle_alert(a)
                except Exception as e:
                    log.debug(f"[FOMO-REST] handle err: {e}")
            if fresh:
                log.info(f"[FOMO-REST] 本轮处理 {fresh} 条新 alert")
        except Exception as e:
            log.warning(f"[FOMO-REST] loop err: {e}")
            time.sleep(FOMO_POLL_SEC)
        time.sleep(FOMO_POLL_SEC)


# ═══════════════════════════════════════════════════════════════
# 信号源 3：GMGN smartmoney / KOL
# ═══════════════════════════════════════════════════════════════
def _gmgn_track(kind: str) -> list:
    import subprocess
    try:
        r = subprocess.run(
            ["gmgn-cli", "track", kind, "--chain", "bsc", "--raw"],
            capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            return []
        d = json.loads(r.stdout)
        return d.get("list") or []
    except Exception as e:
        log.debug(f"[GMGN] {kind} err {e}")
        return []


def _handle_gmgn_trade(t: dict, src_kind: str):
    global _pushes_last_hour
    txid = t.get("transaction_hash") or ""
    if not txid or txid in _seen_gmgn_tx:
        return
    _seen_gmgn_tx.add(txid)
    if len(_seen_gmgn_tx) > 50000:
        _seen_gmgn_tx.clear()

    side = t.get("side")
    usd = float(t.get("amount_usd") or 0)
    token = (t.get("base_address") or "").lower()
    maker = t.get("maker") or ""
    # GMGN 流带精确链上时间（unix 秒）
    try:
        trade_ts = float(t.get("timestamp") or 0) or None
    except (ValueError, TypeError):
        trade_ts = None
    if trade_ts and trade_ts > 1e12:
        trade_ts /= 1000
    if not token:
        return

    info = enrich(token)
    sym = (t.get("base_token") or {}).get("symbol") or (info.get("symbol") or token[:8])
    tags = (t.get("maker_info") or {}).get("tags") or []
    tw = (t.get("maker_info") or {}).get("twitter_username") or ""
    is_smart = "smart_degen" in tags or "app_smart_money" in tags
    is_kol = "kol" in tags
    if is_kol:
        # 积累 BSC 动态 KOL 名单
        if maker and tw:
            _BSC_KOL_WALLETS[maker.lower()] = tw
            if len(_BSC_KOL_WALLETS) % 10 == 0:
                _save_bsc_kol()
        label = f"KOL @{tw}" if tw else "KOL"
    elif is_smart:
        label = "聪明钱"
    else:
        label = ""

    if side == "buy":
        multi_n, multi_total, avg_pos, kol_count, kol_first_n, useful_count = _record_buy(token, usd, maker, label or f"gmgn_{src_kind}")
        is_smart_no_kol = is_smart and not is_kol
        
        # 根据代币年龄和是否首次出现，使用不同门槛
        tier_cfg = _get_tier_thresholds(token, info)
        tier_label = tier_cfg["tier"]
        
        # 新币场景：KOL 必须是首次买入才算信号
        # 如果是加仓（kol_first_n == 0），需要走其他触发场景（如多人共买、大额鲸鱼）
        if tier_label == "new" and is_kol:
            # 新币 + KOL 买入：必须是首买
            if kol_first_n < 1:
                # 加仓，不算新币信号，但可以走其他场景
                log.debug(f"[NEW-TOKEN] ${sym} KOL 加仓（非首买），跳过新币信号")
                _log_signal(token, sym, "buy", "blocked", f"kol_add_position({tier_label})", usd, label or "")
                return
        
        # 打新场景质量门槛：最少2个"有用的人"买入（聪明钱+KOL+fomo）
        if tier_label == "new" and useful_count < 2:
            log.debug(f"[NEW-TOKEN] ${sym} 有用人数 {useful_count} < 2，信号质量不足")
            _log_signal(token, sym, "buy", "blocked", f"useful_count<2({useful_count})", usd, label or "")
            return
        
        push = (
            ((is_kol or is_smart) and usd >= tier_cfg["kol_min"]) or
            usd >= WHALE_BUY_USD_MIN or
            (kol_count >= 2 and multi_total >= tier_cfg["multi_min_total"])  # 至少 2 个 KOL 共买且总额达标
        )
        # 聪明钱单笔低于门槛不推（小额散买噪声）
        if is_smart_no_kol and usd < tier_cfg["smart_min"]:
            push = False
        # 聪明钱必须有至少一个 KOL 跟买才推（纯聪明钱不推）
        if is_smart_no_kol and kol_count < 1:
            push = False
            log.debug(f"[SMART-NO-KOL] ${sym} 聪明钱买入但无 KOL 跟买，拦截")
        if not push:
            _log_signal(token, sym, "buy", "blocked", f"not_push({tier_label})", usd, label or "")
            return
        if not _push_ok(token, f"{maker}|{usd:.0f}"):
            _log_signal(token, sym, "buy", "blocked", "cooldown/dup", usd, label or "")
            return
        # 数据质量检查（分龄门槛）
        quality_pass, quality_reason = _check_data_quality(token, info, tier_label)
        if not quality_pass:
            _log_signal(token, sym, "buy", "blocked", f"quality({quality_reason})", usd, label or "")
            return
        if info and info.get("mc", 0) > MAX_MC_NOTIFY:
            _log_signal(token, sym, "buy", "blocked", f"mc>{MAX_MC_NOTIFY}", usd, label or "")
            return
        if _too_old(info):
            _log_signal(token, sym, "buy", "blocked", "too_old", usd, label or "")
            return
        # 硬拦截：creator 发币 >200 次
        if _hard_block_creator(token, sym):
            _log_signal(token, sym, "buy", "blocked", "hard_block_creator", usd, label or "")
            return
        # 硬拦截：开发者在黑名单中
        if _hard_block_dev_blacklist(token, sym):
            _log_signal(token, sym, "buy", "blocked", "hard_block_dev_blacklist", usd, label or "")
            return
        # 硬拦截：代币本身在黑名单中
        if _hard_block_token_blacklist(token, sym):
            _log_signal(token, sym, "buy", "blocked", "hard_block_token_blacklist", usd, label or "")
            return
        # 硬拦截：开发者发币数量 > 500 个
        if _hard_block_dev_token_count(token, sym):
            _log_signal(token, sym, "buy", "blocked", "hard_block_dev_token_count", usd, label or "")
            return
        if _already_pumped(info, sym):
            _log_signal(token, sym, "buy", "blocked", "already_pumped", usd, label or "")
            return
        if _heavy_sell_pressure(info, sym):
            _log_signal(token, sym, "buy", "blocked", "heavy_sell", usd, label or "")
            return
        # 软过滤器：检查风险标注，强信号时豁免
        risk_flags = _risk_flags(token, sym)
        if risk_flags:
            kol_count = sum(1 for b in _recent_buys.get(token, []) if len(b) >= 4 and b[3] and "KOL" in b[3])
            
            # 特殊规则：持有人过少（<50人）需要至少 3 个 KOL 才能豁免
            holders = (safety.check(token) or {}).get("holders") or 0
            try:
                holders = int(float(holders))
            except (ValueError, TypeError):
                holders = 0
            
            if holders < 50 and kol_count < 3:
                # 持有人<50 且 KOL<3，直接拦截
                return
            
            is_strong = (kol_count >= STRONG_KOL_COUNT
                         or multi_n >= STRONG_MULTI_COUNT
                         or usd >= STRONG_USD)
            if not is_strong:
                return
            log.info(f"[BYPASS] ${sym} 强信号豁免风险过滤: {'; '.join(risk_flags)}")
        if is_kol:
            emoji, cat, pri = "🎯", "KOL/聪明钱买入", "🔴 高"
        elif kol_count >= 2 and usd < WHALE_BUY_USD_MIN:
            emoji, cat, pri = "👥", "多人共买", "🟡 中"
        else:
            emoji, cat, pri = "🐋", "鲸鱼买入", "🟡 中"
        src = f"gmgn {src_kind}"
        if label:
            src = f"gmgn {src_kind} · {label}"
        
        # 检查 OG 状态
        og_status = _check_og_status(sym, token)
        og_tag = " 🔥 OG" if og_status == "OG" else (" ⚠️ 分流" if og_status == "分流" else "")
        
        # 获取项目介绍和触发逻辑
        description = _get_token_description(token)
        trigger_logic = _find_trigger_logic(sym)
        
        md = card.buy_card(
            emoji=emoji, category=cat + og_tag, priority=pri, symbol=sym,
            trader=(f"@{tw}" if (is_kol and tw) else maker[:10] + "…"), usd=usd,
            mc=info.get("mc", 0), liq=info.get("liq", 0),
            vol1h=info.get("vol1h", 0), vol5m=info.get("vol5m", 0),
            buys_m5=info.get("buys_m5", 0), sells_m5=info.get("sells_m5", 0),
            buys_h1=info.get("buys_h1", 0), sells_h1=info.get("sells_h1", 0),
            price_change_5m=info.get("price_change_5m", 0),
            price_change_1h=info.get("price_change_1h", 0),
            price_change_24h=info.get("price_change_24h", 0),
            age_min=info.get("age_min", 0),
            multi_count=multi_n, multi_total=multi_total,
            avg_position=avg_pos,
            addr=token, url=info.get("url", ""),
            launchpad=(t.get("base_token") or {}).get("launchpad") or _guess_launchpad(info),
            source_tag=src,
            safety=safety.check(token),
            extra_lines=risk_flags or None,
            trade_ts=trade_ts, ts_precise=True,
            description=description, trigger_logic=trigger_logic,
            kol_holders=info.get("kol_holders", 0),
            smart_money_holders=info.get("smart_money_holders", 0),
            holders=info.get("holders", 0),
        )
        notify_md(f"{emoji} {cat} ${sym}", md)
        _log_signal(token, sym, "buy", "pushed", source_tag or f"gmgn_{src_kind}", usd, label or "")
        _pushes_last_hour += 1
        # 自动买入（仅买入信号，不处理卖出）
        if side == "buy" and AUTO_BUY_ENABLED:
            trigger_desc = f"{cat}|{label or f'gmgn_{src_kind}'}|${usd:.0f}|{multi_n}人共买${multi_total:.0f}"
            _auto_buy(token, sym, source_tag or f"gmgn_{src_kind}", trigger_desc)
    else:
        # 大额卖出预警
        if usd < WHALE_BUY_USD_MIN:
            return
        if not _push_ok(token, f"sell|{maker}|{usd:.0f}"):
            _log_signal(token, sym, "sell", "blocked", "cooldown/dup", usd, label or "")
            return
        if info and info.get("mc", 0) > MAX_MC_NOTIFY:
            return
        if _too_old(info):
            return
        # 卖出：有风险标注且不是超大单 → 静默丢弃
        _sell_flags_g = _risk_flags(token, sym)
        if _sell_flags_g and usd < STRONG_USD:
            return
        extra = []
        if label:
            extra.append(f"来源: gmgn {src_kind} · {label}")
#         md = card.sell_card(symbol=sym, trader=(f"@{tw}" if (is_kol and tw) else maker[:10] + "…"),
#                             usd=usd, mc=info.get("mc", 0), liq=info.get("liq", 0),
#                             url=info.get("url", ""), addr=token, extra_lines=extra,
#                             safety=safety.check(token),
#                             trade_ts=trade_ts, ts_precise=True)
#         notify_md(f"🚨 大额卖出 ${sym}", md)
        _pushes_last_hour += 1


def gmgn_sync_loop():
    """GMGN smartmoney + KOL 轮询（同步线程版）"""
    log.info("[GMGN] smartmoney/KOL 轮询启动")
    while True:
        try:
            for kind in ("smartmoney", "kol"):
                for t in _gmgn_track(kind):
                    try:
                        _handle_gmgn_trade(t, kind)
                    except Exception as e:
                        log.debug(f"[GMGN] handle err: {e}")
        except Exception as e:
            log.warning(f"[GMGN] loop err: {e}")
        time.sleep(SMARTMONEY_POLL_SEC)


# ═══════════════════════════════════════════════════════════════
# 信号源 4：Binance Smart Money Signals（官方聪明钱信号）
# ═══════════════════════════════════════════════════════════════
_seen_signal_ids: set = set()


def binance_smart_money_loop():
    """Binance 官方聪明钱信号轮询（每 60s）"""
    global _pushes_last_hour
    log.info("[BINANCE-SM] Smart Money 信号轮询启动")
    save_counter = 0
    while True:
        try:
            signals = binance_api.get_smart_money_signals("56", page_size=20)
            for s in signals:
                sid = s.get("signalId")
                if not sid or sid in _seen_signal_ids:
                    continue
                _seen_signal_ids.add(sid)
                
                # 只处理 buy 信号
                if s.get("direction") != "buy":
                    continue
                
                token_addr = s.get("contractAddress", "").lower()
                if not token_addr:
                    continue
                
                # 跨源去重：同币 24h 内不重推
                if not _binance_cooldown_ok(token_addr):
                    continue
                
                # 基本过滤（分龄策略）
                total_value = s.get("totalValue", 0) or 0
                sm_count = s.get("smartMoneyCount", 0) or 0
                
                # 获取代币信息
                info = enrich(token_addr)
                if not info:
                    continue
                
                sym = info.get("symbol", s.get("ticker", "?"))
                mc = info.get("mc", 0) or 0
                liq = info.get("liq", 0) or 0
                age_min = info.get("age_min", 0) or 0
                
                # 分龄门槛（与 FOMO/GMGN 保持一致）
                if age_min < 240:  # 新币 < 4h
                    min_value = 1000  # 至少 $1K
                    min_sm_count = 3
                elif age_min < 1440:  # 早期币 4-24h
                    min_value = 3000  # 至少 $3K
                    min_sm_count = 4
                else:  # 老币 > 24h
                    min_value = 5000  # 至少 $5K
                    min_sm_count = 5
                
                # 应用过滤
                if total_value < min_value:
                    continue
                if sm_count < min_sm_count:
                    continue
                
                # 市值过滤
                if mc > MAX_MC_NOTIFY or mc < MIN_MC_NOTIFY:
                    continue
                
                # 冷却检查
                if not _push_ok(token_addr, f"binance_sm|{sid}"):
                    continue
                
                # 数据质量检查（与 FOMO/GMGN 一致）
                quality_pass, quality_reason = _check_data_quality(token_addr, info, "new" if age_min < 240 else ("early" if age_min < 1440 else "old"))
                if not quality_pass:
                    continue
                
                # 硬拦截
                if _hard_block_creator(token_addr, sym):
                    continue
                if _hard_block_dev_blacklist(token_addr, sym):
                    continue
                if _hard_block_token_blacklist(token_addr, sym):
                    continue
                # 硬拦截：开发者发币数量 > 500 个
                if _hard_block_dev_token_count(token_addr, sym):
                    continue
                
                # 构建卡片
                alert_mc = s.get("alertMarketCap", 0) or 0
                max_gain = s.get("maxGain", 0) or 0
                extra = [
                    f"🧠 {sm_count}个聪明钱买入 **${total_value:,.0f}**",
                    f"📈 触发时MC ${alert_mc:,.0f}",
                ]
                if max_gain > 0:
                    extra.append(f"🔺 最高涨幅 {max_gain:.1f}%")
                
                # OG 状态
                og_status = _check_og_status(sym, token_addr)
                og_tag = " 🔥 OG" if og_status == "OG" else (" ⚠️ 分流" if og_status == "分流" else "")
                
                description = _get_token_description(token_addr)
                trigger_logic = _find_trigger_logic(sym)
                
                md = card.buy_card(
                    emoji="🧠", category=f"聪明钱信号{og_tag}", priority="🔴 高", symbol=sym,
                    trader=f"{sm_count}个聪明钱", usd=total_value,
                    mc=mc, liq=liq,
                    vol1h=info.get("vol1h", 0), vol5m=info.get("vol5m", 0),
                    price_change_5m=info.get("price_change_5m", 0),
                    price_change_1h=info.get("price_change_1h", 0),
                    age_min=info.get("age_min", 0),
                    addr=token_addr, source_tag="binance smart money",
                    safety=safety.check(token_addr),
                    extra_lines=extra,
                    description=description, trigger_logic=trigger_logic,
                    kol_holders=info.get("kol_holders", 0),
                    smart_money_holders=info.get("smart_money_holders", 0),
                    holders=info.get("holders", 0),
                )
                notify_md(f"🧠 聪明钱信号 ${sym}", md)
                _log_signal(token_addr, sym, "buy", "pushed", "binance_smart_money", total_value, f"{sm_count}个聪明钱")
                _pushes_last_hour += 1
                # 自动买入
                if AUTO_BUY_ENABLED:
                    trigger_desc = f"聪明钱信号|{sm_count}个聪明钱|${total_value:.0f}"
                    _auto_buy(token_addr, sym, "binance_smart_money", trigger_desc)
                
                # 标记已推送并保存去重状态
                _binance_mark_pushed(token_addr)
                _save_binance_dedup()
                
        except Exception as e:
            import traceback
            log.warning(f"[BINANCE-SM] loop err: {e}")
            traceback.print_exc()
        time.sleep(60)


# ═══════════════════════════════════════════════════════════════
# 信号源 5：Binance Meme Rush（Pulse 新迁移代币）
# ═══════════════════════════════════════════════════════════════


def binance_meme_rush_loop():
    """Binance Pulse 新迁移代币监控（每 60s）"""
    global _pushes_last_hour
    log.info("[BINANCE-MEME] Meme Rush 轮询启动")
    save_counter = 0
    while True:
        try:
            # rankType=30 表示刚迁移到 DEX 的代币
            tokens = binance_api.get_meme_rush("56", rank_type=30, limit=20)
            for t in tokens:
                addr = t.get("contractAddress", "").lower()
                if not addr or addr in _seen_meme_addrs:
                    continue
                _seen_meme_addrs.add(addr)
                
                # 跨源去重：同币 24h 内不重推
                if not _binance_cooldown_ok(addr):
                    continue
                
                # 基本过滤（提高门槛）
                mc = t.get("marketCap", 0) or 0
                liq = t.get("liquidity", 0) or 0
                holders = t.get("holders", 0) or 0
                kol = t.get("kolHolders", 0) or 0
                
                # Pulse 新币门槛：市值 >= $30K, 流动性 >= $5K, 持有人 >= 50
                if mc < 30000:
                    continue
                if liq < 5000:
                    continue
                if holders < 50:
                    continue
                # 至少要有 1 个 KOL 买入，或者市值 > $100K
                if kol < 1 and mc < 100000:
                    continue
                
                # 冷却检查
                if not _push_ok(addr, f"binance_meme|{addr[:10]}"):
                    continue
                
                # 工厂币检查
                dev_migrate_count = t.get("devMigrateCount", 0) or 0
                if dev_migrate_count > HARD_BLOCK_CREATOR_TOKENS:
                    continue
                # 硬拦截：开发者发币数量 > 500 个
                if _hard_block_dev_token_count(addr, sym):
                    continue
                # 硬拦截：开发者在黑名单中
                if _hard_block_dev_blacklist(addr, sym):
                    continue
                # 硬拦截：代币在黑名单中
                if _hard_block_token_blacklist(addr, sym):
                    continue
                
                sym = t.get("symbol", "?")
                
                # 构建卡片
                extra = [
                    f"🆕 Pulse 新迁移代币",
                ]
                if kol > 0:
                    extra.append(f"🎯 {kol}个 KOL 已买入")
                if t.get("tagDevBurnedToken"):
                    extra.append("🔥 Dev 已烧毁代币")
                
                # OG 状态
                og_status = _check_og_status(sym, addr)
                og_tag = " 🔥 OG" if og_status == "OG" else (" ⚠️ 分流" if og_status == "分流" else "")
                
                description = _get_token_description(addr)
                
                md = card.buy_card(
                    emoji="🆕", category=f"Pulse新币{og_tag}", priority="🟡 中", symbol=sym,
                    trader="Pulse Launchpad", usd=liq,
                    mc=mc, liq=liq,
                    age_min=(time.time() * 1000 - (t.get("createTime") or 0)) / 60000 if t.get("createTime") else 0,
                    addr=addr, source_tag="binance pulse",
                    safety=safety.check(addr),
                    extra_lines=extra,
                    description=description,
                    kol_holders=kol or 0,
                    holders=holders or 0,
                )
                notify_md(f"🆕 Pulse新币 ${sym}", md)
                _log_signal(addr, sym, "new", "pushed", "binance_pulse", mc, "Pulse")
                _pushes_last_hour += 1
                # 自动买入
                if AUTO_BUY_ENABLED:
                    trigger_desc = f"Pulse新币|市值${mc:.0f}|流动性${liq:.0f}|{kol}个KOL"
                    _auto_buy(addr, sym, "binance_pulse", trigger_desc)
                
                # 标记已推送并保存去重状态
                _binance_mark_pushed(addr)
                _save_binance_dedup()
                
        except Exception as e:
            import traceback
            log.warning(f"[BINANCE-MEME] loop err: {e}")
            traceback.print_exc()
        time.sleep(60)


# ═══════════════════════════════════════════════════════════════
# 信号源 6：Binance Smart Money Inflow（聪明钱流入排名）
# ═══════════════════════════════════════════════════════════════


def binance_inflow_loop():
    """Binance 聪明钱流入排名监控（每 120s）"""
    global _pushes_last_hour
    log.info("[BINANCE-INFLOW] Smart Money Inflow 轮询启动")
    save_counter = 0
    while True:
        try:
            # 获取 1h 聪明钱流入排名
            tokens = binance_api.get_smart_money_inflow("56", period="1h")
            for t in tokens[:10]:  # 只看前 10
                addr = t.get("contractAddress", "").lower()
                if not addr or addr in _seen_inflow_addrs:
                    continue
                
                inflow = t.get("inflow", 0) or 0
                traders = t.get("traders", 0) or 0
                
                # 提高门槛：净流入 > $2K 且至少 3 个聪明钱地址
                if inflow < 2000 or traders < 3:
                    continue
                
                # 首次发现才推送
                _seen_inflow_addrs.add(addr)
                
                # 跨源去重：同币 24h 内不重推
                if not _binance_cooldown_ok(addr):
                    continue
                
                mc = t.get("marketCap", 0) or 0
                liq = t.get("liquidity", 0) or 0
                
                if mc > MAX_MC_NOTIFY or mc < MIN_MC_NOTIFY:
                    continue
                if liq < 2000:
                    continue
                
                # 冷却检查
                if not _push_ok(addr, f"binance_inflow|{addr[:10]}"):
                    continue
                
                # 硬拦截
                if _hard_block_creator(addr, sym):
                    continue
                if _hard_block_dev_blacklist(addr, sym):
                    continue
                if _hard_block_token_blacklist(addr, sym):
                    continue
                # 硬拦截：开发者发币数量 > 500 个
                if _hard_block_dev_token_count(addr, sym):
                    continue
                
                sym = t.get("tokenName", "?")
                price_chg = t.get("priceChangeRate", 0)
                
                # 构建卡片
                extra = [
                    f"💰 1h聪明钱净流入 **${inflow:,.0f}**",
                    f"👥 {traders}个聪明钱地址交易",
                ]
                if price_chg:
                    extra.append(f"📈 1h涨幅 {price_chg:+.1f}%")
                
                # OG 状态
                og_status = _check_og_status(sym, addr)
                og_tag = " 🔥 OG" if og_status == "OG" else (" ⚠️ 分流" if og_status == "分流" else "")
                
                description = _get_token_description(addr)
                trigger_logic = _find_trigger_logic(sym)
                
                md = card.buy_card(
                    emoji="💰", category=f"聪明钱流入{og_tag}", priority="🟡 中", symbol=sym,
                    trader=f"{traders}个聪明钱", usd=inflow,
                    mc=mc, liq=liq,
                    price_change_1h=price_chg,
                    addr=addr, source_tag="binance inflow rank",
                    safety=safety.check(addr),
                    extra_lines=extra,
                    description=description, trigger_logic=trigger_logic,
                    holders=t.get("holders") or 0,
                )
                notify_md(f"💰 聪明钱流入 ${sym}", md)
                _log_signal(addr, sym, "inflow", "pushed", "binance_inflow", inflow, f"{traders}个聪明钱")
                _pushes_last_hour += 1
                # 自动买入
                if AUTO_BUY_ENABLED:
                    trigger_desc = f"聪明钱流入|{traders}个聪明钱|${inflow:.0f}净流入"
                    _auto_buy(addr, sym, "binance_inflow", trigger_desc)
                
                # 标记已推送并保存去重状态
                _binance_mark_pushed(addr)
                _save_binance_dedup()
                
        except Exception as e:
            import traceback
            log.warning(f"[BINANCE-INFLOW] loop err: {e}")
            traceback.print_exc()
        time.sleep(120)


# ═══════════════════════════════════════════════════════════════
# 心跳状态
# ═══════════════════════════════════════════════════════════════
_START_TS = time.time()
_pushes_last_hour: int = 0


async def status_loop():
    global _pushes_last_hour
    n0 = len(_seen_pairs)
    while True:
        await asyncio.sleep(STATUS_INTERVAL_SEC)
        uptime = int(time.time() - _START_TS) // 60
        md = card.status_card(seen_pairs=len(_seen_pairs),
                              new_pairs_h=len(_seen_pairs) - n0,
                              gmgn_cache=len(_seen_gmgn_tx),
                              fomo_cache=len(_seen_alert_ids),
                              uptime_min=uptime, pushes_h=_pushes_last_hour)
        notify_md("💚 BSC 监控心跳", md)
        _pushes_last_hour = 0
        n0 = len(_seen_pairs)
        _save_seen()
        _save_binance_dedup()


# ═══════════════════════════════════════════════════════════════
# 启动
# ═══════════════════════════════════════════════════════════════
async def main():
    _load_seen()
    _load_binance_dedup()
    _load_kol_map()
    _load_auto_buy_positions()
    log.info("🦞 BSC meme 监控启动（v4：+自动买入 +开发者发币>500硬拦截）")
    log.info("📊 数据源：FOMO + GMGN + Binance Web3 API（Smart Money/Meme Rush/Inflow Rank）")
    log.info(f"💰 自动买入: {'✅ 启用' if AUTO_BUY_ENABLED else '❌ 禁用'} | 每次 ${AUTO_BUY_AMOUNT_USD} | 开发者发币>{HARD_BLOCK_DEV_TOKEN_COUNT}拦截")
    log.info("🔒 去重：Binance 信号持久化 + 跨源 24h 冷却 + KOL 首买检测 + 自动买入去重")
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, fomo_rest_loop)
    loop.run_in_executor(None, gmgn_sync_loop)
    loop.run_in_executor(None, binance_smart_money_loop)
    loop.run_in_executor(None, binance_meme_rush_loop)
    loop.run_in_executor(None, binance_inflow_loop)
    await asyncio.gather(
        rpc_pair_loop(),
        status_loop(),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("收到退出信号，bye")
