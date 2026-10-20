# ============================================================
# ws_monitor.py — WebSocket 信号监听 + 策略路由
# ============================================================
import asyncio, json, time, os, sys, requests
from collections import defaultdict

# 影子跟踪器
# shadow_tracker 已停用（样本不足，无统计意义）
# from shadow_tracker import shadow_record, shadow_check_cycle, get_tracker
from datetime import datetime, timezone

try:
    import websockets
except ImportError:
    import subprocess
    subprocess.run([sys.executable, '-m', 'pip', 'install', 'websockets',
                    '--break-system-packages', '-q'], check=False)
    import websockets

from config import (
    FOMO_API_KEY, FOMO_WS_URL, CHAIN_ID,
    SIZE_KOL, SIZE_MULTI, SIZE_MULTI_STRONG, MAX_POSITION_SIZE_USD,
    MULTI_BUY_WINDOW_MIN, MULTI_BUY_THRESHOLD, MULTI_BUY_STRONG_THR,
    HIGH_MC_FILTER_THRESHOLD, HIGH_MC_MIN_DRAWDOWN, HIGH_MC_TREND_RISE_6H,
    HIGH_MC_STOP_TIER_1, HIGH_MC_STOP_TIER_2, HIGH_MC_STOP_LOSS_T1, HIGH_MC_STOP_LOSS_T2,
    HIGH_MC_POSITION_HALF, HIGH_MC_CALM_SEC, HIGH_MC_CALM_DRIFT_MAX, HIGH_MC_CALM_DROP_MAX,
    HIGH_MC_DAILY_TRADE_MAX, WHALE_STRATEGY_MC_CAP,
    WHALE_C_THRESHOLD_USD, WHALE_C_QUEUE_USD, WHALE_B_THRESHOLD_USD, WHALE_A_THRESHOLD_USD,
    WHALE_BUY_SIZE_USD, WHALE_CONFIRM_WINDOW_MIN,
    WHALE_SELL_WARN_USD, WHALE_SELL_SOFT_USD, WHALE_SELL_HARD_USD,
    CONSENSUS_WHALE_USD, CONSENSUS_TRAILING_DRAWDOWN,
    PRICE_DRIFT_MAX_PCT, PRICE_DRIFT_ENABLED,
    TOKEN_BLACKLIST,
    DCA_ENABLED, DCA_DROP_PCT, DCA_SIZE_USD, DCA_MAX_TIMES, DCA_MAX_TOTAL_USD,
    DCA_SKIP_1H_DROP, DCA_SKIP_VOL_RATIO, DCA_SKIP_LOW_MC_6H,
    KOL_SELL_WHALE_THRESHOLD_USD,
    WHALE_SIGNAL_CONFIRM_SCORE, WHALE_SIGNAL_CONFIRM_MAX_PNL,
    GAS_HIGH_THRESHOLD_USD, GAS_PAUSE_WINDOW_SEC, GAS_PAUSE_BUY_ONLY,
    SIGNAL_GRADE_S2_THR, SIGNAL_GRADE_S3_WHALE_USD, SIZE_SIGNAL_S,
    KOL_SELL_SCORE_PENALTY,
    LOG_FILE, JARVIS_TARGET, WS_RPC_URL,
    POSITION_MAX_HOLD_H,
    WHALE_FOLLOW_ENABLED, WHALE_FOLLOW_MC_MAX, WHALE_FOLLOW_SIZE_USD, WHALE_FOLLOW_SELL_RATIO,
    OLD_TOKEN_SURGE_WINDOW_MIN, OLD_TOKEN_SURGE_THRESHOLD, OLD_TOKEN_SURGE_MIN_USD,
    OLD_TOKEN_SURGE_COOLDOWN_HR, OLD_TOKEN_SURGE_MC_MAX, OLD_TOKEN_SURGE_DEV_MAX,
)
from pair_listener import run_pair_listener
import position_manager as pm
from position_manager import (update_signal_on_kol_buy, update_signal_on_kol_sell,
                               update_signal_on_whale,
                               is_s_consensus, s_consensus_apply_score,
                               s_consensus_hard_stop, s_consensus_score_action)
import executor as ex
from gas_manager import refill_eth_if_needed, check_gas_before_trade
from token_filter import (run_filters, check_token_security, check_token_liquidity,
                          check_token_age, check_smart_money_confirmation,
                          check_anti_wash, check_deployer_history, get_boost_amount,
                          refresh_boost_set, get_recent_new_buyers,
                          get_holder_risk, check_holder_risk, run_safety_extensions)
from shadow_tracker import register_signal as shadow_register, shadow_loop as shadow_loop_main

# ──────────────────────────────────────────────
# KOL watchlist（EVM 地址 → handle）
# ──────────────────────────────────────────────

def load_kol_map():
    # 优先从 kol_watchlist.json 加载（包含所有 KOL，用于多人共买场景）
    path = os.path.join(os.path.dirname(__file__), "kol_watchlist.json")
    if not os.path.exists(path):
        path = os.path.join(os.path.dirname(__file__), "evm_watchlist.json")
    with open(path) as f:
        raw = json.load(f)
    # kol_watchlist.json 格式：{kols: [...], meta: {...}}
    # evm_watchlist.json 格式：[...]
    if isinstance(raw, dict) and "kols" in raw:
        data = raw["kols"]
    else:
        data = raw
    # evm_addr → handle
    evm_map = {item["evm"].lower(): item["handle"] for item in data}
    # fomo_handle(小写) → {evm, score, grade}——策略A匹配用
    fomo_map = {
        item["fomo_handle"].lower(): {
            "evm":   item["evm"].lower(),
            "score": item.get("score") or 0,
            "grade": item.get("grade") or "C",
        }
        for item in data if item.get("fomo_handle")
    }
    # 额外构建 evm_addr → {handle, score, grade} 映射（用于多人共买场景）
    evm_info_map = {
        item["evm"].lower(): {
            "handle": item.get("handle", "unknown"),
            "score": item.get("score") or 0,
            "grade": item.get("grade") or "C",
        }
        for item in data
    }
    # 构建 handle(小写) → {evm, score, grade} 映射（用于多人共买场景，所有 KOL 都算）
    handle_map = {
        item["handle"].lower(): {
            "evm":   item["evm"].lower(),
            "score": item.get("score") or 0,
            "grade": item.get("grade") or "C",
        }
        for item in data if item.get("handle")
    }
    return evm_map, fomo_map, evm_info_map, handle_map


# ── KOL_MAP TTL 懒加载缓存 ──────────────────────────────────────
# 进程常驻期间每 6h 自动从文件重载一次，无需重启进程
_KOL_MAP_CACHE: tuple = ({}, {}, {}, {})  # (KOL_MAP, FOMO_MAP, evm_info_map, handle_map)
_KOL_MAP_TS    = 0.0
_KOL_MAP_TTL   = 6 * 3600   # 6 小时
_KOL_MAP_LOCK  = asyncio.Lock()

async def _get_kol_maps() -> tuple:
    """返回 (KOL_MAP, FOMO_MAP)，TTL 过期时自动重载，双重检查防并发重复 I/O。"""
    global _KOL_MAP_CACHE, _KOL_MAP_TS
    if time.time() - _KOL_MAP_TS > _KOL_MAP_TTL:
        async with _KOL_MAP_LOCK:
            if time.time() - _KOL_MAP_TS > _KOL_MAP_TTL:   # 双重检查
                _KOL_MAP_CACHE = load_kol_map()  # (evm_map, fomo_map, evm_info_map, handle_map)
                _KOL_MAP_TS    = time.time()
                kol_m, fomo_m, evm_info_m, handle_m  = _KOL_MAP_CACHE
                s = sum(1 for v in fomo_m.values() if v["grade"] == "S")
                a = sum(1 for v in fomo_m.values() if v["grade"] == "A")
                b = sum(1 for v in fomo_m.values() if v["grade"] == "B")
                c = sum(1 for v in fomo_m.values() if v["grade"] == "C")
                _log(f"[KOL RELOAD] {len(kol_m)} EVM / {len(fomo_m)} fomo_handle / {len(handle_m)} total handles "
                     f"S={s} A={a} B={b} C={c}")
    return _KOL_MAP_CACHE

# 兼容旧代码：启动时仍赋值，但只读，运行时通过 _get_kol_maps() 访问
KOL_MAP:  dict = {}   # evm → handle
FOMO_MAP: dict = {}   # fomo_handle(小写) → {evm, score, grade}

# 等级阈値：S/A级单人触发，B/C级需至少 2 人
SINGLE_TRIGGER_GRADES = {"S", "A"}

# ── 场外强手（非白名单）信号参数 ──────────────────────────────────────────────
# 买入持仓期加分：场外强手大额买入给 signal_score 小幅加分
OUTSIDER_BUY_SCORE_THRESHOLD = 5_000   # 场外强手单笔 >= $5K 才加分
OUTSIDER_BUY_SCORE_DELTA     = 3       # 加分幅度（比 C 级 KOL 的 5 分略低）
# 卖出持仓期扣分：场外强手大额卖出给 signal_score 扣分
OUTSIDER_SELL_SCORE_THRESHOLD = 50_000  # 场外强手单笔卖出 >= $50K 才扣分
OUTSIDER_SELL_SCORE_DELTA     = 5       # 扣分幅度
# 软黑名单解锁：场外强手大额买入参与 AB 解锁（门槛更高：4人而非3人）
OUTSIDER_UNLOCK_THRESHOLD    = 10_000   # 单笔 >= $10K 才算解锁贡献
OUTSIDER_UNLOCK_COUNT        = 4        # 需要 4 个不同场外 handle（比白名单的3人多1人）
MULTI_CONFIRM_MIN     = 2      # B/C级需几人同时买入

# 按等级进行仓位大小匩分
GRADE_SIZE_MULTIPLIER = {"S": 1.5, "A": 1.2, "B": 1.0, "C": 0.8}

# ── 全局并发买入限制 ─────────────────────────────────────────────────
# 防止1分钟内多个不同token同时触发，资金超出风险预算
# _BUY_SEMAPHORE 在 main() 里初始化（asyncio.Semaphore 必须在事件循环内创建）
_BUY_SEMAPHORE: asyncio.Semaphore | None = None   # 延迟初始化
MAX_CONCURRENT_BUYS: int = 2                       # 同时最多 2 笔买入
_LAST_BUY_TS:        float = 0.0                   # 上一笔买入完成时间戳
GLOBAL_COOLDOWN_SEC: int   = 45                    # 两笔买入之间最短间隔（秒）

# ── WS 消息去重（防 replay 重复处理）──────────────────────────
_SEEN_EVENTS: dict = {}   # event_id → timestamp
_DEDUP_TTL = 86400        # 24h TTL，防止 seen_events 无限膨胀（内存泄漏防护）

# ★ 买入信号去重：记录正在排队/执行买入的 token_addr
# 仓位满了但旧信号还在 queue 里时，防止同一 token 重复触发
# 买入信号去重：{token_addr: 加入时间戳}
# in-flight 防并发 TTL=300s；历史去重 TTL=86400s
_PENDING_BUY: dict = {}          # token_addr → timestamp

# ── Gas 高峰暂停状态 ────────────────────────────────
_GAS_PAUSE_UNTIL: float = 0.0    # Unix 时间戳，前暂停买入（或全部）


def _record_high_gas_event(gas_usd: float, symbol: str, tx: str):
    """单笔 gas 超阈值：记录日志 + 触发暂停窗口 + 推送通知"""
    global _GAS_PAUSE_UNTIL
    resume_ts = time.time() + GAS_PAUSE_WINDOW_SEC
    _GAS_PAUSE_UNTIL = resume_ts
    resume_str = time.strftime("%H:%M:%S", time.gmtime(resume_ts))
    pause_target = "买入" if GAS_PAUSE_BUY_ONLY else "买入和卖出"
    _log(f"[GAS SPIKE] 单笔gas=${gas_usd:.2f} 超过阈值${GAS_HIGH_THRESHOLD_USD}，"
         f"暂停{pause_target} {GAS_PAUSE_WINDOW_SEC}s 至 {resume_str} UTC")
    _notify(
        f"⛽️ Gas 高峰预警\n"
        f"本笔实际Gas: ${gas_usd:.2f}（阈值 ${GAS_HIGH_THRESHOLD_USD}\uff09\n"
        f"已暂停{pause_target}，{resume_str} UTC 后恢复\n"
        f"该笔: ${symbol} Tx {tx[:16]}…"
    )


def _is_gas_paused(for_sell: bool = False) -> bool:
    """
    返回当前是否处于 Gas 暂停窗口。
    for_sell=True 且 GAS_PAUSE_BUY_ONLY=True 时卖出不受影响。
    """
    if time.time() >= _GAS_PAUSE_UNTIL:
        return False
    if for_sell and GAS_PAUSE_BUY_ONLY:
        return False   # 买入暂停时卖出仍允许
    return True
_PENDING_BUY_ACTIVE_TTL = 300    # 执行期最长保留 5 分钟
_PENDING_BUY_DEDUP_TTL  = 86400  # 24h 周期内同一 token 不重复开仓
_PENDING_BUY_TS:  dict = {}      # ← 不再使用，保留空 dict 兼容旧引用


# ──────────────────────────────────────────────
# 通知模块（已拆分到 notify.py）
# ──────────────────────────────────────────────
from notify import (
    init_notify, _notify, _record_buy_failure,
    _notify_buy, _notify_signal, _notify_close, _notify_partial_sell, _notify_sell,
)

# ── 鲸鱼跟单状态表 ──────────────────────────────────────────────────────
# token_addr → {trader, buy_usd, ts, sold}
_WHALE_FOLLOW: dict = {}

# 鲸鱼信号去重：(trader_lower, token_addr) → 最近一次推送的 timestamp
# 60秒内同一(trader, token)只发一条，避免短时间内连续两笔买入重复推送
_WHALE_SIGNAL_DEDUP: dict = {}  # (trader_lower, token_addr) → ts
_WHALE_SIGNAL_DEDUP_TTL = 60    # 秒

# ── 鲸鱼接盘黑名单（延迟加载，_log 定义后初始化）──
_WHALE_BLACKLIST: set = set()
_WHALE_BL_LOADED = False

def _load_whale_blacklist():
    global _WHALE_BL_LOADED
    if _WHALE_BL_LOADED:
        return
    _WHALE_BL_LOADED = True
    try:
        import json as _json_bl, os as _os_bl
        _bl_path = _os_bl.path.join(_os_bl.path.dirname(__file__), 'whale_blacklist.json')
        with open(_bl_path) as _fbl:
            for _entry in _json_bl.load(_fbl):
                _h = _entry.get('handle', '').lower()
                if _h:
                    _WHALE_BLACKLIST.add(_h)
        _log(f"[INIT] 鲸鱼黑名单加载: {len(_WHALE_BLACKLIST)} 个接盘地址")
    except Exception as _e_bl:
        _log(f"[INIT] 鲸鱼黑名单加载失败: {_e_bl}")

# ── S/A KOL 信号合并缓冲 ──────────────────────────────────────────
# 同一 token 在 5 分钟内有多位 S/A KOL 买入时，合并为一条信号推送
_KOL_MERGE_BUFFER: dict = {}   # token_addr → {kols: [{handle, grade, score, usd, ts}], timer_task}
_KOL_MERGE_WINDOW = 120        # 合并窗口：2 分钟内到达的 S/A KOL 合并为一条
_KOL_MERGE_MAX_KOLS = 5        # 达到此人数立即发送（不等窗口结束）

# ── 信号分类系统 ──────────────────────────────────────────────────
# 根据信号源 + MC + 年龄 + 趋势 + 买家质量，给出分类标签和持仓建议
def _classify_signal(source: str, grade: str = '', mc: float = 0,
                     age_hours: float = 0, trend_phase: str = '',
                     multi_count: int = 0, kol_count: int = 1) -> dict:
    """
    返回 {category, emoji, priority, hold_hint, hold_emoji} 用于信号卡片。
    分类逻辑：
      🟢 大K埋伏  — S/A KOL 早期买入 (MC<$2M, age<24h, trend=早期)
      🔥 热点引爆  — 2+ KOL + 多人共买 + 拉升中
      ⚡ KOL共识  — 2+ S/A KOL 同币共识
      🐋 鲸鱼扫货  — A/B 级鲸鱼大额买入
      🔄 老币复苏  — S/A KOL 买入老币 (>3天)
      📈 共买趋势  — 4+ 人共买，无S/A KOL领单
    """
    cat, emoji, priority, hold_hint, hold_emoji = '未知', '❓', '⚪ 低', '', ''

    is_old = age_hours > 72  # > 3 天
    is_early = trend_phase == 'early' or (age_hours < 24 and trend_phase != 'pumping')
    is_pumping = trend_phase == 'pumping'

    if source == 'kol':
        if kol_count >= 2 and multi_count >= 4:
            cat, emoji = '热点引爆', '🔥'
            priority = '🔴 高'
            hold_hint = '快进快出 | 严格追踪止损 | 警惕砸盘'
            hold_emoji = '⚡ 短线'
        elif kol_count >= 2:
            cat, emoji = 'KOL共识', '⚡'
            priority = '🔴 高'
            hold_hint = '共识强信号 | 中短线 | 可适当加大仓位'
            hold_emoji = '📈 中短'
        elif grade in ('S', 'A') and is_old:
            cat, emoji = '老币复苏', '🔄'
            priority = '🟡 中' if grade == 'A' else '🔴 高'
            hold_hint = '叙事轮动或社区拉升 | 快进快出 | 警惕老币流动性差'
            hold_emoji = '⚡ 短线'
        elif grade in ('S', 'A') and is_early and mc < 2_000_000:
            cat, emoji = '大K埋伏', '🟢'
            priority = '🔴 高'
            hold_hint = '聪明钱早期布局 | 耐心持有 | 等叙事发酵'
            hold_emoji = '📅 中长线'
        elif grade in ('S', 'A'):
            cat, emoji = '聪明钱买入', '⭐'
            priority = '🟡 中'
            hold_hint = 'S/A级KOL信号 | 跟单后按趋势离场'
            hold_emoji = '📈 中短线'
        else:
            cat, emoji = 'KOL买入', '✅'
            priority = '⚪ 低' if grade == 'C' else '🟡 中'
            hold_hint = 'B/C级KOL | 需多人确认 | 参考为主'
            hold_emoji = '📈 短线'
    elif source == 'whale':
        if grade in ('A', 'B'):
            cat, emoji = '鲸鱼扫货', '🐋'
            priority = '🔴 高' if grade == 'A' else '🟡 中'
            hold_hint = '跟鲸鱼离场 | 鲸鱼卖则卖 | 中线持有'
            hold_emoji = '📅 中线'
        else:
            cat, emoji = '鲸鱼买入', '🐳'
            priority = '⚪ 低'
            hold_hint = 'C级鲸鱼 | 信号较弱 | 参考为主'
            hold_emoji = '📈 短线'
    elif source == 'multi':
        if multi_count >= 14:
            cat, emoji = 'FOMO共振', '🔥'
            priority = '🟡 中'
            hold_hint = '强共识但可能见顶 | 严格止损 | 快进快出'
            hold_emoji = '⚡ 短线'
        else:
            cat, emoji = '共买趋势', '👥'
            priority = '⚪ 低'
            hold_hint = 'FOMO情绪 | 注意追高风险 | 短线操作'
            hold_emoji = '⚡ 短线'
    elif source == 'fomo':
        cat, emoji = 'FOMO热潮', '🔥'
        priority = '⚪ 低'
        hold_hint = '大众FOMO | 最容易接盘 | 谨慎参与'
        hold_emoji = '⚡ 短线'

    return {
        'category': cat,
        'emoji': emoji,
        'priority': priority,
        'hold_hint': hold_hint,
        'hold_emoji': hold_emoji,
    }

# ── 每币每小时推送冷却：同一 token 1h 内最多推送 3 次 ──
_TOKEN_PUSH_LOG: dict = {}     # token_addr → [timestamp, ...] 最近1h的推送时间
_TOKEN_PUSH_MAX_PER_HR = 3    # 每小时最多 3 次
def _check_token_push_cooldown(token_addr: str) -> bool:
    """返回 True 表示可以推送，False 表示超出每小时上限被静默。"""
    now = time.time()
    cutoff = now - 3600  # 1h 前
    addr = token_addr.lower().strip()
    ts_list = _TOKEN_PUSH_LOG.get(addr, [])
    # 清理超过 1h 的旧记录
    ts_list = [t for t in ts_list if t >= cutoff]
    if len(ts_list) >= _TOKEN_PUSH_MAX_PER_HR:
        _TOKEN_PUSH_LOG[addr] = ts_list  # 保存清理后的列表
        return False
    ts_list.append(now)
    _TOKEN_PUSH_LOG[addr] = ts_list
    return True

# 鲸鱼推送次数：token_addr → 已推送次数（前3次带中文简述）
_WHALE_PUSH_COUNT: dict = {}
_WHALE_PUSH_NARRATIVE_LIMIT = 3  # 前 N 次推送带叙事简述

# ── S/A KOL 合并推送逻辑 ──────────────────────────────────────────
async def _kol_merge_add_and_flush(token_addr: str, token_symbol: str,
                                   trader: str, grade: str, score: int,
                                   buy_usd: float, mc: float):
    """
    S/A KOL 信号合并：同 token 2分钟内多位 S/A KOL 买入时合并为一条信号。
    - 第 1 位 KOL 到达 → 创建缓冲，启动 2min 定时器
    - 后续 KOL 到达 → 加入缓冲，达到 _KOL_MERGE_MAX_KOLS 人立即发送
    - 定时器到期 → 发送合并信号（1人或多人）
    """
    addr = token_addr.lower()
    now = time.time()
    entry = {"handle": trader, "grade": grade, "score": score,
            "usd": buy_usd, "ts": now}

    # 预检分层过滤，避免等 2min 后才被过滤
    from config import TIERED_FILTERS
    _tier = TIERED_FILTERS['sa_kol']
    # MC 过滤
    if mc > 0 and mc > _tier['mc_max']:
        _log(f"[KOL MERGE SKIP] {token_symbol} MC ${mc/1e6:.1f}M > ${_tier['mc_max']/1e6:.0f}M，不进入合并窗口")
        return
    # Dev 过滤
    try:
        from token_filter import get_token_dev_info
        _dev = get_token_dev_info(token_addr) or {}
        _dev_count = int(_dev.get("twitter_create_token_count") or _dev.get("creator_open_count") or 0)
        if _dev_count > _tier['dev_max']:
            _log(f"[KOL MERGE SKIP] {token_symbol} Dev 发币 {_dev_count} 次 > {_tier['dev_max']}，不进入合并窗口")
            return
    except Exception:
        pass
    # 币龄过滤（获取不到也放行，假设新币）
    try:
        from token_filter import get_token_info
        _info = get_token_info(token_addr) or {}
        _open_ts = _info.get("open_timestamp")
        if _open_ts:
            _age_days = (time.time() - int(_open_ts)) / 86400
            if _age_days > _tier['age_max_days']:
                _log(f"[KOL MERGE SKIP] {token_symbol} 创建 {_age_days:.1f}天 > {_tier['age_max_days']}天，不进入合并窗口")
                return
    except Exception:
        pass

    if addr not in _KOL_MERGE_BUFFER:
        # 首位 KOL → 创建缓冲 + 定时器
        _KOL_MERGE_BUFFER[addr] = {"kols": [entry], "token": token_symbol, "mc": mc}
        _log(f"[KOL MERGE] {token_symbol} 首位 {grade}级 @{trader} 开启合并窗口")
        # 启动定时器
        await asyncio.sleep(_KOL_MERGE_WINDOW)
        # 定时器到期 → 发送
        buf = _KOL_MERGE_BUFFER.pop(addr, None)
        if buf and buf["kols"]:
            await _flush_kol_merge(token_addr, buf)
    else:
        # 后续 KOL → 加入缓冲
        buf = _KOL_MERGE_BUFFER[addr]
        # 防重复：同一 handle 不重复加入
        if not any(k["handle"].lower() == trader.lower() for k in buf["kols"]):
            buf["kols"].append(entry)
            buf["mc"] = mc  # 更新最新 MC
        kol_count = len(buf["kols"])
        _log(f"[KOL MERGE] {token_symbol} +{grade}级 @{trader} → {kol_count}位 KOL 合并中")
        # 达到上限立即发送
        if kol_count >= _KOL_MERGE_MAX_KOLS:
            buf = _KOL_MERGE_BUFFER.pop(addr, None)
            if buf:
                await _flush_kol_merge(token_addr, buf)


async def _flush_kol_merge(token_addr: str, buf: dict):
    """发送合并后的 KOL 共识信号。"""
    kols = buf["kols"]
    token = buf["token"]
    mc = buf["mc"]
    kol_count = len(kols)

    # 构建 KOL 列表字符串
    kol_labels = []
    for k in kols:
        kol_labels.append(f"@{k['handle']}({k['grade']}级 {k['score']}分)")
    kol_str = " + ".join(kol_labels)
    total_usd = sum(k["usd"] for k in kols)

    # 获取代币年龄和趋势
    try:
        from token_filter import get_token_info
        info = get_token_info(token_addr) or {}
        open_ts = info.get("open_timestamp")
        if open_ts:
            age_h = (time.time() - int(open_ts)) / 3600
        else:
            from executor import get_token_age_sec
            _age_sec = get_token_age_sec(token_addr)
            age_h = _age_sec / 3600 if _age_sec else 999
    except Exception:
        age_h = 999

    # 信号分类
    classification = _classify_signal(
        source='kol', grade=kols[0]['grade'], mc=mc,
        age_hours=age_h, trend_phase='', multi_count=0, kol_count=kol_count)

    # 构建 msg_lines
    if kol_count >= 2:
        title = f"{classification['emoji']} {classification['category']}信号  ${token}  {kol_count}位KOL共识"
    else:
        title = f"{classification['emoji']} {classification['category']}信号  ${token}  {kols[0]['grade']}级"

    msg_lines = [
        title,
        f"👤 {kol_str}",
        f"→ {kol_count}位 S/A KOL 共识买入",
    ]
    if total_usd > 0:
        msg_lines.append(f"💰 合计: ${total_usd:,.0f}")

    await _signal_notify_with_meta(token, token_addr, msg_lines, mc=mc,
                                   source='kol', bypass_cooldown=True,
                                   classification=classification, kol_count=kol_count)




# ── 首次出现检测（三源共用，同一代币只通知一次）──
_FIRST_SEEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".first_seen.json")
_first_seen: dict = {}

def _load_first_seen():
    if os.path.exists(_FIRST_SEEN_FILE):
        try:
            return json.load(open(_FIRST_SEEN_FILE))
        except Exception:
            pass
    return {}

def _save_first_seen():
    try:
        json.dump(_first_seen, open(_FIRST_SEEN_FILE, "w"))
    except Exception:
        pass

def _check_and_record_first_seen(token_addr: str) -> bool:
    """
    检查代币是否首次出现在任何信号源，并记录。
    三源（whale/kol/fomo）共用，返回 True 表示首次出现。
    """
    addr = token_addr.lower().strip()
    if addr in _first_seen:
        return False
    _first_seen[addr] = int(time.time())
    _save_first_seen()
    return True

_first_seen = _load_first_seen()



# ── 鲸鱼信号胜率缓存 ────────────────────────────────────────────────────────
_WHALE_WIN_CACHE: dict = {}   # handle → (win_rate, wins, total, ts)
_WHALE_WIN_TTL   = 3600       # 1h 再刷新


def _get_whale_win_rate(handle: str) -> tuple:
    """查 fomoapi 计算 KOL 胜率，返回 (win_rate, wins, total)，失败时返回 (-1,0,0)。"""
    now = time.time()
    if handle in _WHALE_WIN_CACHE:
        wr, wins, total, ts = _WHALE_WIN_CACHE[handle]
        if now - ts < _WHALE_WIN_TTL:
            return wr, wins, total
    try:
        import requests as _req
        from config import FOMO_API_KEY
        _P = {"https": "socks5://REDACTED_PROXY_CREDENTIALS",
               "http":  "socks5://REDACTED_PROXY_CREDENTIALS"}
        r = _req.get(f"https://api.fomoapi.io/v2/users/{handle}/trades",
                     headers={"authorization": f"Bearer {FOMO_API_KEY}"},
                     proxies=_P, timeout=8)
        if r.status_code != 200:
            return -1, 0, 0
        trades = r.json() if isinstance(r.json(), list) else r.json().get("trades", [])
        if not trades:
            return -1, 0, 0
        # 只统计已平仓（closed）的交易，排除持仓中（open）导致 realizedPnl=0 误展胜率
        closed = [t for t in trades if t.get("status") == "closed"]
        if not closed:
            return -1, 0, 0
        total = len(closed)
        wins  = sum(1 for t in closed if float(t.get("realizedPnlUsd") or 0) > 0)
        wr    = wins / total if total else 0
        _WHALE_WIN_CACHE[handle] = (wr, wins, total, now)
        return wr, wins, total
    except Exception:
        return -1, 0, 0


def _gmgn_enrich(token_addr: str) -> dict:
    """从 GMGN API 拉取代币补充信息：社交链接、dev 信息、聪明钱、热门度等。"""
    try:
        import subprocess, json as _json
        r = subprocess.run(
            ["gmgn-cli", "token", "info", "--chain", "robinhood", "--address", token_addr, "--raw"],
            capture_output=True, text=True, timeout=10
        )
        if r.returncode != 0 or not r.stdout.strip():
            return {}
        return _json.loads(r.stdout)
    except Exception:
        return {}


def _build_narrative(token_symbol: str, token_addr: str, cinfo: dict,
                    mc: float, age_h: float,
                    unique_buyers_1h: int, buy_vol_1h: float, sell_vol_1h: float,
                    dev_count: int, tw_user: str, tg_link: str,
                    description: str, smart_wallets, grade: str,
                    buy_usd: float, trader: str) -> str | None:
    """根据已获取的数据 + GMGN 补充数据生成一段简短中文叙事简述，解释为什么这个币突然热度上升。"""
    try:
        bits = []

        # ── GMGN 补充数据 ──
        _gmgn = _gmgn_enrich(token_addr)
        _link = _gmgn.get("link", {}) if _gmgn else {}
        _dev = _gmgn.get("dev", {}) if _gmgn else {}
        _wts = _gmgn.get("wallet_tags_stat", {}) if _gmgn else {}
        _stat = _gmgn.get("stat", {}) if _gmgn else {}
        _visiting = _gmgn.get("visiting_count", 0) if _gmgn else 0

        # 币龄叙事
        if age_h < 6:
            bits.append(f"${token_symbol} 上线仅 {age_h:.0f} 小时，属于极早期新币，鲸鱼 @{trader} 此时买入 ${buy_usd:,.0f} 属于抢占先手")
        elif age_h < 24:
            bits.append(f"${token_symbol} 上线 {age_h:.0f} 小时，仍在早期阶段")
        else:
            bits.append(f"${token_symbol} 已上线 {age_h/24:.1f} 天")

        # 鲸鱼信号热度
        if grade in ("A", "S"):
            bits.append(f"{grade}级鲸鱼 @{trader} 重仓买入 ${buy_usd:,.0f}，信号质量高")
        elif grade == "B":
            bits.append(f"B级鲸鱼 @{trader} 买入 ${buy_usd:,.0f}")
        else:
            bits.append(f"鲸鱼 @{trader} 买入 ${buy_usd:,.0f}")

        # 1h 买卖热度
        if unique_buyers_1h >= 30:
            bits.append(f"1h 内 {unique_buyers_1h} 个独立买家涌入，热度爆发")
        elif unique_buyers_1h >= 10:
            bits.append(f"1h 内 {unique_buyers_1h} 个独立买家")

        if buy_vol_1h > 0:
            net = buy_vol_1h - sell_vol_1h
            if net > 10000:
                bits.append(f"净流入 ${net:,.0f}，买盘强势")
            elif net < -10000:
                bits.append(f"净流出 ${abs(net):,.0f}，注意卖压")

        # 市值区间叙事
        if mc < 500_000:
            bits.append(f"MC 仅 ${mc/1000:.0f}K，超低微盘弹性极大")
        elif mc < 2_000_000:
            bits.append(f"MC ${mc/1e6:.1f}M，处于低市值区间")
        elif mc < 5_000_000:
            bits.append(f"MC ${mc/1e6:.1f}M，中等市值")

        # 聪明钱
        if smart_wallets is not None and smart_wallets >= 10:
            bits.append(f"{smart_wallets} 个聪明钱地址已进场")
        elif smart_wallets is not None and smart_wallets >= 3:
            bits.append(f"{smart_wallets} 个聪明钱在场")

        # 社区
        # GMGN 社交数据优先
        _tw = _link.get("twitter_username", "") or tw_user
        _tg = _link.get("telegram", "") or tg_link
        _desc = (_link.get("description", "") or description or "").strip()
        if _tw:
            bits.append(f"Twitter @{_tw}")
        if _tg:
            bits.append(f"有 TG 群")
        if _desc:
            bits.append(f"项目介绍：{_desc[:60]}")

        # GMGN 热度
        if _visiting and _visiting >= 100:
            bits.append(f"GMGN 搜索热度 {_visiting}，关注度较高")

        # GMGN 聪明钱 / KOL
        _smart = _wts.get("smart_wallets", 0) or 0
        _kol = _wts.get("renowned_wallets", 0) or 0
        if _smart >= 10:
            bits.append(f"GMGN 标记 {_smart} 个聪明钱持仓")
        if _kol >= 3:
            bits.append(f"{_kol} 个 KOL 钱包在场")

        # GMGN Dev 状态
        _dev_status = _dev.get("creator_token_status", "")
        if _dev_status == "creator_close":
            bits.append("Dev 已清仓离场，无抛压风险")
        elif _dev_status == "creator_hold":
            bits.append("⚠️ Dev 仍持仓，注意抛压")
        _dev_twitter = _dev.get("twitter_create_token_count", 0) or 0
        if _dev_twitter >= 5:
            bits.append(f"Dev 在 Twitter 推广过 {_dev_twitter} 个代币")

        # Dev
        if dev_count == 0:
            bits.append("Dev 首次发币，记录干净")
        elif dev_count <= 2:
            bits.append(f"Dev 仅发币 {dev_count} 次，履历较干净")

        if not bits:
            return None

        return "💡 " + "\n".join(bits)
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════
# 共享代币分析函数 — 鲸鱼/KOL/FOMO 三源统一调用
# ═══════════════════════════════════════════════════════════════

async def _build_token_analysis(token_addr: str, token_symbol: str,
                                 mc: float, trader: str = "",
                                 buy_usd: float = 0, grade: str = "C"):
    """
    全量代币分析：安全/Dev/币龄/持仓/鲸鱼胜率/Twitter/趋势 + 七维度评分 + 建议。
    鲸鱼、KOL共买、FOMO共买三源统一调用，保证分析标准完全一致。
    返回 dict: {
        lines: [str],          # 分析行列表（不含标题行）
        score: int,             # 综合评分
        rating: str,            # 评级 🟢🟡🟠🔴
        rec: str,               # 主观建议
        first_tag: str,          # 首次出现标记（调用方提供）
        mc: float,              # 实时修正后的 MC
        entry_price: float,     # 当前价格（用于影子跟踪）
    }
    """
    import asyncio as _aio
    import requests as _req
    _P = {"https": "socks5://REDACTED_PROXY_CREDENTIALS",
          "http":  "socks5://REDACTED_PROXY_CREDENTIALS"}
    CLAWBY_KEY   = "pk_xBgG9wi3q1ZnIe6peyFrBHQP_6v2hG2C"
    CLAWBY_RELAY = "https://api.openclawby.com/api/relay"

    def _clawby():
        try:
            r = _req.post(CLAWBY_RELAY,
                headers={"X-API-Key": CLAWBY_KEY, "Content-Type": "application/json"},
                json={"name": "dex_token_info",
                      "params": {"chain": "robinhood", "address": token_addr}},
                proxies=_P, timeout=10)
            return r.json().get("data", {}).get("data", {})
        except Exception:
            return {}

    def _fomo_stats():
        try:
            from config import FOMO_API_KEY
            r = _req.get(f"https://api.fomoapi.io/token/{token_addr}/stats",
                headers={"authorization": f"Bearer {FOMO_API_KEY}"},
                proxies=_P, timeout=8)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        return {}

    def _fomo_holders():
        try:
            from config import FOMO_API_KEY
            r = _req.get(
                f"https://api.fomoapi.io/token/{token_addr}/holders",
                params={"networkId": 4663, "network": "robinhood"},
                headers={"authorization": f"Bearer {FOMO_API_KEY}"},
                proxies=_P, timeout=8)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        return {}

    def _win_rate():
        if trader:
            return _get_whale_win_rate(trader)
        return (-1, 0, 0)

    def _token_age():
        """返回 (age_str, age_hours) 供评分复用。优先用 Clawby open_timestamp，回退 DexScreener pairCreatedAt。"""
        try:
            from token_filter import get_token_info
            info = get_token_info(token_addr) or {}
            open_ts = info.get("open_timestamp")
            if not open_ts:
                # Clawby 返回空，回退 DexScreener pairCreatedAt
                from executor import get_token_age_sec
                _age_sec = get_token_age_sec(token_addr)
                if _age_sec is not None:
                    age_h = _age_sec / 3600
                    label = f"{age_h:.0f}小时" if age_h < 24 else f"{age_h/24:.1f}天"
                    return label, age_h
                return "未知", 999
            age_h = (time.time() - int(open_ts)) / 3600
            label = f"{age_h:.0f}小时" if age_h < 24 else f"{age_h/24:.1f}天"
            return label, age_h
        except Exception:
            pass
        return "未知", 999

    def _price_trend():
        try:
            r = _req.get(
                f"https://api.dexscreener.com/latest/dex/tokens/{token_addr}",
                timeout=8, proxies=_P)
            pairs = r.json().get("pairs", []) or []
            if not pairs:
                return {}
            pair = max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))
            pc = pair.get("priceChange", {})
            m5 = float(pc.get("m5", 0) or 0)
            h1 = float(pc.get("h1", 0) or 0)
            h6 = float(pc.get("h6", 0) or 0)
            h24 = float(pc.get("h24", 0) or 0)
            txns = pair.get("txns", {})
            t5 = txns.get("m5", {})
            t1 = txns.get("h1", {})
            buys_5m = int(t5.get("buys", 0) or 0)
            sells_5m = int(t5.get("sells", 0) or 0)
            buys_1h = int(t1.get("buys", 0) or 0)
            sells_1h = int(t1.get("sells", 0) or 0)
            created = pair.get("pairCreatedAt")
            age_h_trend = 999
            if created:
                age_h_trend = (time.time() - created / 1000) / 3600
            from_peak_pct = 0.0
            if m5 < 0: from_peak_pct = max(from_peak_pct, abs(m5))
            if h1 < 0: from_peak_pct = max(from_peak_pct, abs(h1))
            if h6 < 0: from_peak_pct = max(from_peak_pct, abs(h6))
            if h24 < 0: from_peak_pct = max(from_peak_pct, abs(h24))
            pump_tf = ""
            if m5 > 20 or (h1 > 50 and m5 > 5):
                phase, emoji, label = "pumping", "🟡", "拉升中"
                desc = f"正在拉升，5m {m5:+.1f}% 1h {h1:+.1f}%，追高风险"
            elif (m5 < 0 and (h1 > 25 or h6 > 40)) or (h1 < 0 and h6 > 40):
                pump_tf = "1h内" if (h1 > 30 or (m5 < 0 and h1 > 0)) else "1-6h前"
                if h1 < -10 and h6 < -20:
                    phase, emoji, label = "decline", "⚫", "阴跌"
                    desc = f"已拉过({pump_tf})，距峰值跌{from_peak_pct:.0f}%，持续阴跌"
                else:
                    phase, emoji, label = "correction", "🔴", "已拉过"
                    desc = f"已拉过({pump_tf})，距峰值跌{from_peak_pct:.0f}%，入场=接盘"
            elif h1 < -10 and h6 < -20:
                phase, emoji, label = "decline", "⚫", "阴跌"
                desc = f"距峰值跌{from_peak_pct:.0f}%，1h {h1:.1f}% 6h {h6:.1f}%，持续阴跌"
            elif h24 > 80 and h6 < 0:
                phase, emoji, label = "correction", "🔴", "已拉过"
                desc = f"已拉过(6-24h前)，距峰值跌{from_peak_pct:.0f}%，入场=接盘"
            elif age_h_trend < 6 and h1 < 30 and m5 < 15:
                phase, emoji, label = "early", "🟢", "早期"
                desc = f"上线{age_h_trend:.1f}h未大涨，5m {m5:+.1f}% 1h {h1:+.1f}%"
            else:
                phase, emoji, label = "early", "🟢", "早期"
                desc = f"价格平稳，5m {m5:+.1f}% 1h {h1:+.1f}%"
            return {"phase": phase, "phase_emoji": emoji, "phase_label": label,
                    "phase_desc": desc, "m5": m5, "h1": h1, "h6": h6, "h24": h24,
                    "buys_5m": buys_5m, "sells_5m": sells_5m,
                    "buys_1h": buys_1h, "sells_1h": sells_1h,
                    "from_peak_pct": from_peak_pct}
        except Exception:
            return {}

    cinfo, fomo_stats, wr_tuple, _age_result, fomo_holders_data, trend_data = await _aio.gather(
        _aio.to_thread(_clawby),
        _aio.to_thread(_fomo_stats),
        _aio.to_thread(_win_rate),
        _aio.to_thread(_token_age),
        _aio.to_thread(_fomo_holders),
        _aio.to_thread(_price_trend),
    )
    wr, wins, total = wr_tuple
    age_str, _age_h = _age_result

    stat   = cinfo.get("stat", {})
    dev    = cinfo.get("dev", {})
    pool   = cinfo.get("pool", {})
    link   = cinfo.get("link", {})
    wts    = cinfo.get("wallet_tags_stat", {})
    price  = cinfo.get("price", {})

    holder_count  = stat.get("holder_count") or fomo_stats.get("holders") or None
    top10_rate    = float(stat.get("top_10_holder_rate") or 0) * 100 if stat.get("top_10_holder_rate") else None
    bot_rate      = float(stat.get("bot_degen_rate") or 0) * 100 if stat.get("bot_degen_rate") else None
    entrap_rate   = float(stat.get("top_entrapment_trader_percentage") or 0) * 100 if stat.get("top_entrapment_trader_percentage") else None
    smart_wallets = wts.get("smart_wallets") or None
    liquidity     = float(pool.get("liquidity") or pool.get("liquidityUsd") or 0) or None
    honeypot      = cinfo.get("security", {}).get("is_honeypot") or stat.get("is_honeypot")
    buy_tax       = float(cinfo.get("security", {}).get("buy_tax") or stat.get("buy_tax") or 0)
    sell_tax      = float(cinfo.get("security", {}).get("sell_tax") or stat.get("sell_tax") or 0)

    dev_addr       = dev.get("creator_address", "")
    dev_count      = int(dev.get("twitter_create_token_count") or dev.get("creator_open_count") or 0)
    creator_status = dev.get("creator_token_status", "")
    dev_cleared    = "close" in str(creator_status).lower() or str(dev.get("creator_token_balance") or "0") == "0"
    dev_hold_pct   = float(stat.get("creator_hold_rate") or 0) * 100
    cto_flag       = bool(dev.get("cto_flag"))

    _w1h  = (fomo_stats.get("windows") or {}).get("1h", {})
    buy_1h  = int(_w1h.get("buys") or price.get("buys_1h") or 0)
    sell_1h = int(_w1h.get("sells") or price.get("sells_1h") or 0)
    buy_vol_1h  = float(_w1h.get("buyVolumeUsd") or 0)
    sell_vol_1h = float(_w1h.get("sellVolumeUsd") or 0)
    unique_buyers_1h = int(_w1h.get("uniqueBuyers") or 0)

    tw_user = link.get("twitter_username", "")
    tg_link = link.get("telegram", "")

    _clawby_price  = float(price.get("price") or 0)
    _clawby_supply = float(cinfo.get("circulating_supply") or cinfo.get("total_supply") or 0)
    if _clawby_price > 0 and _clawby_supply > 0:
        _mc_fresh = _clawby_price * _clawby_supply
        if _mc_fresh > 0:
            mc = _mc_fresh

    mc_str = f"${mc/1_000_000:.2f}M" if mc >= 1_000_000 else f"${mc/1000:.0f}K" if mc > 0 else "新币"

    # ── 七维度评分 ──
    score = 0
    if honeypot:
        score -= 10
    elif buy_tax > 5 or sell_tax > 5:
        score -= 2
    if dev_count > 10:
        score -= 3
    elif dev_count > 5:
        score -= 1
    elif dev_count == 0:
        score += 1
    if not dev_cleared and dev_hold_pct > 5:
        score -= 2
    elif dev_cleared:
        score += 1
    if _age_h < 6:
        score += 3
    elif _age_h < 24:
        score += 1
    elif _age_h > 48:
        score -= 2
    if bot_rate is not None:
        if bot_rate > 70: score -= 2
        elif bot_rate < 30: score += 1
    if entrap_rate is not None and entrap_rate > 60:
        score -= 2
    if smart_wallets is not None and smart_wallets >= 30:
        score += 2
    elif smart_wallets is not None and smart_wallets >= 10:
        score += 1
    if mc < 500_000:
        score += 2
    elif mc < 1_000_000:
        score += 1
    elif mc > 5_000_000:
        score -= 1
    if liquidity and liquidity < 10_000:
        score -= 2
    if buy_vol_1h > 0 and sell_vol_1h > 0:
        net_ratio = (buy_vol_1h - sell_vol_1h) / (buy_vol_1h + sell_vol_1h)
        if net_ratio > 0.3: score += 2
        elif net_ratio < -0.3: score -= 1
    if wr >= 0.6:
        score += 1
    elif 0 <= wr < 0.4 and total >= 5:
        score -= 1
    if trend_data:
        _tp = trend_data.get("phase", "")
        if _tp == "early": score += 2
        elif _tp == "pumping": score += 1
        elif _tp == "correction": score -= 3
        elif _tp == "decline": score -= 5

    if score >= 7: rating = "🟢 建议跟单"
    elif score >= 4: rating = "🟡 可以考虑"
    elif score >= 1: rating = "🟠 谨慎观望"
    else: rating = "🔴 不建议"

    # ── 分析行组装 ──
    if honeypot:
        基本面 = "⚠️ 蜜罐合约，禁止买入"
    else:
        sec_part = "合约安全" if buy_tax <= 5 and sell_tax <= 5 else f"税率高(买{buy_tax:.0f}/卖{sell_tax:.0f})"
        if dev_count > 10:
            基本面 = f"Dev发币{dev_count}次，割肉风险高；{sec_part}"
        elif dev_cleared:
            基本面 = f"Dev已清仓，{dev_count}次发币；{sec_part}"
        else:
            hold = f"，仍持{dev_hold_pct:.1f}%" if dev_hold_pct > 1 else ""
            基本面 = f"Dev发币{dev_count}次{hold}；{sec_part}"

    age_part = f"发币{_age_h:.0f}h" if _age_h < 24 else f"发币{_age_h/24:.1f}天"
    mc_part = f"MC {mc_str}"
    mc_note = "超低微盘，弹性大但流动性差" if mc < 500_000 else ("低市值，有上涨空间" if mc < 2_000_000 else "")
    市值线 = f"{age_part}，{mc_part}{'，' + mc_note if mc_note else ''}"

    struct_parts = []
    if smart_wallets is not None and smart_wallets >= 10:
        struct_parts.append(f"聪明钱{smart_wallets}个")
    if holder_count:
        struct_parts.append(f"{holder_count:,}地址")
    if bot_rate is not None and bot_rate > 60:
        struct_parts.append(f"机器人{bot_rate:.0f}%")
    elif bot_rate is not None and bot_rate < 30:
        struct_parts.append(f"机器人低{bot_rate:.0f}%")
    if entrap_rate is not None and entrap_rate > 60:
        struct_parts.append(f"陷阱比{entrap_rate:.0f}%")
    if buy_vol_1h > 0 and sell_vol_1h > 0:
        net = buy_vol_1h - sell_vol_1h
        if net > 5000: struct_parts.append(f"1h净流入${net:,.0f}")
        elif net < -5000: struct_parts.append(f"1h净流出${abs(net):,.0f}")
    if unique_buyers_1h >= 30:
        struct_parts.append(f"{unique_buyers_1h}新买家")
    if cto_flag:
        struct_parts.append("CTO")
    持仓线 = "、".join(struct_parts) if struct_parts else "数据不足"

    if trader and wr >= 0.6:
        鲸鱼线 = f"@{trader} 胜率{wr*100:.0f}%，信号可靠"
    elif trader and wr >= 0 and total >= 5:
        鲸鱼线 = f"@{trader} 胜率{wr*100:.0f}%，参考为主"
    elif trader:
        鲸鱼线 = f"@{trader} 胜率数据不足"
    else:
        鲸鱼线 = ""

    tw_line = ""
    _tw_analysis = None
    try:
        import sys as _g_sys, os as _g_os
        _g_sys.path.insert(0, _g_os.path.join(_g_os.path.dirname(_g_os.path.abspath(__file__)), ".."))
        from grok_analyzer import analyze_token_twitter, combined_recommendation
        _tw_analysis = analyze_token_twitter(token_addr, chain_hint="Robinhood chain")
        if _tw_analysis and _tw_analysis.get("composite_score", 0) > 0:
            tw_score = _tw_analysis["composite_score"]
            tw_sig = _tw_analysis.get("buy_signal", "")
            tw_tweets = _tw_analysis.get("tweet_count", 0)
            tw_views = _tw_analysis.get("total_views", 0)
            tw_kol = _tw_analysis.get("kol_count", 0)
            view_str = f"{tw_views/1e6:.1f}M" if tw_views >= 1e6 else f"{tw_views/1e3:.0f}K" if tw_views >= 1e3 else str(tw_views)
            tw_line = f"综合{tw_score}/10({tw_sig})，{tw_tweets}推文/{view_str}浏览/{tw_kol}KOL"
            _log(f"[GROK TW] {token_symbol} 综合={tw_score}/10 信号={tw_sig}")
    except Exception as _e:
        _log(f"[GROK TW] 分析失败: {_e}")

    _rec = ""
    try:
        from grok_analyzer import combined_recommendation as _combined
        _trend_phase = trend_data.get("phase", "") if trend_data else ""
        _base_rec = _combined(grade, rating, score, _tw_analysis)
        if _trend_phase == "decline":
            _rec = "🔴 不建议入场 — 已拉过盘且持续阴跌"
        elif _trend_phase == "correction" and "建议买入" in _base_rec:
            _rec = "⚠️ 谨慎 — 信号虽强但已拉过盘回调，等企稳再入"
        elif _trend_phase == "correction":
            _rec = "🔴 不建议 — 已拉过盘回调，入场=接盘"
        elif _trend_phase == "pumping" and "建议买入" in _base_rec:
            _rec = _base_rec + "（⚠️ 拉升中，追高控制仓位）"
        elif _trend_phase == "early" and "建议买入" in _base_rec:
            _rec = _base_rec + " ✨ 早期入场点"
        else:
            _rec = _base_rec
    except Exception:
        pass

    social_short = f"@{tw_user}" if tw_user else (tg_link.rstrip('/').split('/')[-1] if tg_link else "")

    trend_line = ""
    if trend_data:
        trend_line = f"{trend_data.get('phase_emoji','')} {trend_data.get('phase_label','')} — {trend_data.get('phase_desc','')}"

    lines = [f"🔧 {基本面}", f"📊 {市值线}"]
    if trend_line:
        lines.append(f"📉 {trend_line}")
    lines.append(f"📈 {持仓线}")
    # ── 持仓结构风险（bundler/insider/sniper）──
    try:
        _hr = get_holder_risk(token_addr)
        if _hr['bundler_pct'] > 0 or _hr['insider_pct'] > 0 or _hr['sniper_pct'] > 0:
            _hr_parts = []
            if _hr['bundler_pct'] > 5:
                _hr_parts.append(f"bundler{_hr['bundler_pct']:.0f}%")
            if _hr['insider_pct'] > 5:
                _hr_parts.append(f"insider{_hr['insider_pct']:.0f}%")
            if _hr['sniper_pct'] > 5:
                _hr_parts.append(f"sniper{_hr['sniper_pct']:.0f}%")
            if _hr_parts:
                lines.append(f"🤖 持仓结构: {' '.join(_hr_parts)}")
        if _hr['verdict'] == 'reject':
            lines.append(f"⚠️ 持仓异常: {'; '.join(_hr['reasons'])}")
    except Exception:
        pass
    # ── 安全扩展检查 (#3 #5 #8) ──
    try:
        _safety_ext = run_safety_extensions(token_addr, token_symbol or token_addr[:8])
        if _safety_ext['verdict'] == 'reject':
            lines.append(f"🔴 危险: {'; '.join(_safety_ext['rejects'])}")
        elif _safety_ext['verdict'] == 'warn':
            lines.append(f"⚠️ 注意: {'; '.join(_safety_ext['warnings'])}")
    except Exception:
        pass
    if 鲸鱼线:
        lines.append(f"🐳 {鲸鱼线}")
    if tw_line:
        lines.append(f"🐦 {tw_line}")
    if social_short:
        lines.append(f"🌐 {social_short}")
    if _rec:
        lines += ["", _rec]

    return {
        "lines": lines,
        "score": score,
        "rating": rating,
        "rec": _rec,
        "mc": mc,
        "mc_str": mc_str,
        "entry_price": _clawby_price,
        "age_str": age_str,
    }



# ── 老币活跃度突变检测 ──────────────────────────────────────────────
def _old_token_surge_record(token_addr: str, trader: str, usd: float, source: str):
    """
    记录一笔被币龄过滤器拦住的买入信号到老币突涨队列。
    当窗口内达到阈值时推送「老币活跃度突变」信号。
    source: 'whale' / 'kol' / 'multi'
    """
    # KOL/multi 来源无单笔金额，只计次数；whale 来源有金额门槛
    if source == "whale" and usd < OLD_TOKEN_SURGE_MIN_USD:
        return

    addr = token_addr.lower().strip()
    now = time.time()
    window_sec = OLD_TOKEN_SURGE_WINDOW_MIN * 60

    # 清理过期条目
    _OLD_TOKEN_SURGE[addr][:] = [
        e for e in _OLD_TOKEN_SURGE[addr] if now - e["ts"] < window_sec
    ]

    # 防同一来源重复计入（用 trader 字段去重，若 trader 是标题文本则用 source 去重）
    _dedup_key = trader.lower() if trader and not trader.startswith("🔄") and not trader.startswith("⭐") and not trader.startswith("✅") and not trader.startswith("👥") and not trader.startswith("🔥") else source
    if any(e["trader"].lower() == _dedup_key for e in _OLD_TOKEN_SURGE[addr]):
        return

    _OLD_TOKEN_SURGE[addr].append({
        "trader": _dedup_key, "ts": now, "usd": usd, "source": source
    })

    count = len(_OLD_TOKEN_SURGE[addr])
    _trader_labels = []
    for e in _OLD_TOKEN_SURGE[addr]:
        if e["usd"] > 0:
            _trader_labels.append(f"{e['source']}(${e['usd']/1000:.0f}K)")
        else:
            _trader_labels.append(e['source'])
    traders_str = ", ".join(_trader_labels)
    _log(f"[OLD SURGE] {token_addr[:12]}… | {count}/{OLD_TOKEN_SURGE_THRESHOLD} 笔 ({traders_str})")

    # 达到阈值 → 推送
    if count >= OLD_TOKEN_SURGE_THRESHOLD:
        # 冷却检查
        last_push = _OLD_TOKEN_SURGE_LAST_PUSH.get(addr, 0)
        if now - last_push < OLD_TOKEN_SURGE_COOLDOWN_HR * 3600:
            _log(f"[OLD SURGE] {token_addr[:12]}… 冷却中，不重复推送")
            return
        _OLD_TOKEN_SURGE_LAST_PUSH[addr] = now
        # 清空窗口防止重复触发
        _OLD_TOKEN_SURGE[addr] = []
        asyncio.create_task(_push_old_token_surge(token_addr, _OLD_TOKEN_SURGE_LAST_PUSH, count, traders_str))


async def _push_old_token_surge(token_addr: str, _last_push_ref: dict, count: int, traders_str: str):
    """推送老币活跃度突变信号，绕过币龄过滤器，但仍检查 MC/Dev。"""
    # 获取代币信息
    from token_filter import get_token_info, get_token_dev_info
    token_symbol = "?"
    try:
        info = get_token_info(token_addr) or {}
        token_symbol = info.get("symbol", "?")
    except Exception:
        pass

    mc = fetch_market_cap(token_addr)

    # MC 上限检查
    if mc > 0 and mc > OLD_TOKEN_SURGE_MC_MAX:
        _log(f"[OLD SURGE SKIP] {token_symbol} MC ${mc/1e6:.1f}M > ${OLD_TOKEN_SURGE_MC_MAX/1e6:.0f}M 上限")
        return

    # Dev 发币次数检查
    try:
        _dev = get_token_dev_info(token_addr) or {}
        _dev_count = int(_dev.get("twitter_create_token_count") or _dev.get("creator_open_count") or 0)
        if _dev_count > OLD_TOKEN_SURGE_DEV_MAX:
            _log(f"[OLD SURGE SKIP] {token_symbol} Dev 发币 {_dev_count} 次 > {OLD_TOKEN_SURGE_DEV_MAX} 次")
            return
    except Exception:
        pass

    # 获取币龄信息用于展示
    age_str = "未知"
    try:
        _info = get_token_info(token_addr) or {}
        _open_ts = _info.get("open_timestamp")
        if _open_ts:
            _age_days = (time.time() - int(_open_ts)) / 86400
            age_str = f"{_age_days:.0f}天"
        else:
            from executor import get_token_age_sec
            _age_sec = get_token_age_sec(token_addr)
            if _age_sec is not None:
                age_str = f"{_age_sec/86400:.0f}天"
    except Exception:
        pass

    mc_str = f"${mc/1e6:.2f}M" if mc >= 1_000_000 else f"${mc/1e3:.0f}K" if mc > 0 else "未知"

    beijing_time = time.strftime("%H:%M:%S", time.gmtime(time.time() + 8*3600)) + " CST"

    lines = [
        f"🔄 老币复苏 | 🟡 中优先",
        f"🔄 老币活跃度突变  ${token_symbol}  {mc_str}",
        f"──────────────────",
        f"⚡ 短线 | 叙事轮动或社区拉升 | 快进快出",
        f"──────────────────",
        f"📊 币龄: {age_str} | MC: {mc_str}",
        f"🔥 {OLD_TOKEN_SURGE_WINDOW_MIN}分钟内 {count} 笔大额买入（被币龄过滤，现补推）",
        f"👤 买入者: {traders_str}",
        f"──────────────────",
        f"💡 老币突然被炒作，可能是叙事轮动或社区拉升",
        f"📌 GMGN: https://gmgn.ai/bsc/token/{token_addr}",
        f"⏰ {beijing_time}",
    ]
    _notify("\n".join(lines))
    _log(f"[OLD SURGE SENT] {token_symbol} {count}笔 突涨信号已推送 MC={mc_str} age={age_str}")


async def _push_whale_signal(token_addr: str, token_symbol: str,
                              trader: str, buy_usd: float,
                              mc: float, grade: str):
    """鲸鱼信号卡片：分层过滤 + 信号分类 + 新卡片格式 + 鲸鱼黑名单。"""
    # ── 鲸鱼黑名单检查（延迟加载）──
    if not _WHALE_BL_LOADED:
        _load_whale_blacklist()
    if trader.lower() in _WHALE_BLACKLIST:
        _log(f"[WHALE BLACKLIST] @{trader} 在接盘黑名单中，静默忽略")
        return
    # ── 每币每小时推送冷却 ──
    if not _check_token_push_cooldown(token_addr):
        _log(f"[PUSH COOLDOWN] {token_symbol} 1h内已推送≥{_TOKEN_PUSH_MAX_PER_HR}次，静默")
        return
    # ── 首次出现检测 ──
    _is_first = _check_and_record_first_seen(token_addr)
    if _is_first:
        _log(f"[FIRST SEEN] {token_symbol} 首次出现在信号监控 {token_addr[:12]}…")

    # ── 分层过滤：鲸鱼使用 whale tier ──
    from config import TIERED_FILTERS
    tier = TIERED_FILTERS['whale']
    if mc > 0 and mc > tier['mc_max']:
        _log(f"[SIGNAL SKIP] {token_symbol} MC ${mc/1e6:.1f}M > ${tier['mc_max']/1e6:.0f}M 鲸鱼上限，不推送")
        return
    # 币龄过滤
    _age_h = 999
    try:
        from token_filter import get_token_info
        _info = get_token_info(token_addr) or {}
        _open_ts = _info.get("open_timestamp")
        if _open_ts:
            _age_h = (time.time() - int(_open_ts)) / 3600
            _age_days = _age_h / 24
            if _age_days > tier['age_max_days']:
                _log(f"[SIGNAL SKIP] {token_symbol} 创建 {_age_days:.1f}天 > {tier['age_max_days']}天，不推送")
                _old_token_surge_record(token_addr, trader, buy_usd, "whale")
                return
        else:
            from executor import get_token_age_sec
            _age_sec = get_token_age_sec(token_addr)
            if _age_sec is not None:
                _age_h = _age_sec / 3600
                _age_days = _age_h / 24
                if _age_days > tier['age_max_days']:
                    _log(f"[SIGNAL SKIP] {token_symbol} 创建 {_age_days:.1f}天 > {tier['age_max_days']}天 (DexScreener兜底)，不推送")
                    _old_token_surge_record(token_addr, trader, buy_usd, "whale")
                    return
            else:
                # 新币无法获取币龄 → 假设为新币放行，而非丢弃
                _log(f"[SIGNAL] {token_symbol} 无法获取币龄，假设为新币放行")
                _age_h = 0
    except Exception as _e:
        _log(f"[SIGNAL] {token_symbol} 币龄检查异常: {_e}，假设为新币放行")
        _age_h = 0
    # Dev 过滤
    try:
        from token_filter import get_token_dev_info
        _dev = get_token_dev_info(token_addr) or {}
        _dev_count = int(_dev.get("twitter_create_token_count") or _dev.get("creator_open_count") or 0)
        if _dev_count > tier['dev_max']:
            _log(f"[SIGNAL SKIP] {token_symbol} Dev 发币 {_dev_count} 次 > {tier['dev_max']} 次，不推送")
            return
    except Exception:
        pass

    # ── 安全扩展检查 (#3 #5 #8) ──
    _safety = await asyncio.to_thread(run_safety_extensions, token_addr, token_symbol)
    if _safety['verdict'] == 'reject':
        _log(f"[WHALE SIGNAL SKIP] {token_symbol} 安全扩展拒绝: {'; '.join(_safety['rejects'])}")
        return
    if _safety['warnings']:
        _log(f"[WHALE SIGNAL WARN] {token_symbol} 安全扩展警告: {'; '.join(_safety['warnings'])}")

    # ── 调用共享分析 ──
    _analysis = await _build_token_analysis(token_addr, token_symbol, mc, trader=trader, buy_usd=buy_usd, grade=grade)
    _lines = _analysis["lines"]
    _score = _analysis["score"]
    _rating = _analysis["rating"]
    _mc_str = _analysis["mc_str"]
    _entry_price = _analysis["entry_price"]
    _mc_final = _analysis["mc"]

    # ── 信号分类 ──
    classification = _classify_signal(
        source='whale', grade=grade, mc=_mc_final,
        age_hours=_age_h, trend_phase='', multi_count=0, kol_count=1)

    # ── 新卡片格式 ──
    _first_tag = "🆕 首次出现 " if _is_first else ""
    beijing_time = time.strftime("%H:%M:%S", time.gmtime(time.time() + 8*3600)) + " CST"

    header = f"{classification['emoji']} {classification['category']} | {classification['priority']}优先 | 评分 {_score}/10"
    sub = f"{grade}级鲸鱼 ${token_symbol} {_mc_str} | @{trader} 买入${buy_usd:,.0f}"
    separator = "─" * 18
    hold_line = f"{classification['hold_emoji']} {classification['hold_hint']}"

    lines_out = [
        header,
        sub,
        separator,
        hold_line,
        separator,
    ] + _lines + ["", f"📌 GMGN: https://gmgn.ai/bsc/token/{token_addr}", f"⏰ {beijing_time}"]

    _notify("\n".join(l for l in lines_out if l))
    _log(f"[WHALE SIGNAL SENT] {token_symbol} {grade}级 @{trader} ${buy_usd:,.0f} MC={_mc_str} score={_score} cat={classification['category']}")

    # ── 影子跟踪注册 ──
    try:
        shadow_register(token_addr, token_symbol,
                        classification['category'], classification['priority'],
                        _score, _mc_final, _entry_price or 0,
                        source='whale', trader=trader, grade=grade)
    except Exception:
        pass
def _whale_follow_check_age(token_addr: str) -> tuple[bool, str]:
    """检查代币年龄，返回 (ok, age_str)。拿不到时放行。"""
    try:
        from token_filter import get_token_info
        info = get_token_info(token_addr) or {}
        open_ts = info.get("open_timestamp")
        if open_ts:
            age_days = (time.time() - int(open_ts)) / 86400
            if age_days > 2:
                return False, f"{age_days:.1f}天"
            age_str = f"{age_days*24:.0f}小时" if age_days < 1 else f"{age_days:.1f}天"
            return True, age_str
    except Exception:
        pass
    return True, "未知"  # 拿不到年龄时放行


async def _whale_follow_buy(token_addr: str, token_symbol: str,
                             trader: str, buy_usd: float, mc: float):
    """鲸鱼跟单买入：MC < WHALE_FOLLOW_MC_MAX，无止盈止损，跟鲸鱼卖出。"""
    if not WHALE_FOLLOW_ENABLED:
        return
    if mc <= 0 or mc > WHALE_FOLLOW_MC_MAX:
        return
    if token_addr in _WHALE_FOLLOW:
        _log(f"[FOLLOW] {token_symbol} 已在跟单持仓中，跳过")
        return

    age_ok, age_str = _whale_follow_check_age(token_addr)
    if not age_ok:
        _log(f"[FOLLOW] {token_symbol} 创建 {age_str} > 2天，跳过")
        return

    _log(f"[FOLLOW BUY] {token_symbol} 跟单 @{trader} ${buy_usd:,.0f}，MC=${mc/1e6:.2f}M，买入 ${WHALE_FOLLOW_SIZE_USD}")
    result = await asyncio.to_thread(
        ex.buy_token, token_addr, token_symbol, WHALE_FOLLOW_SIZE_USD,
        -1.0, None, "whale_follow"
    )
    if result:
        _WHALE_FOLLOW[token_addr] = {
            "trader": trader, "buy_usd": buy_usd,
            "ts": time.time(), "sold": False,
        }
        import time as _t
        cst = _t.strftime("%H:%M:%S", _t.gmtime(_t.time() + 8*3600))
        _notify(
            f"🐠 鲸鱼跟单 开仓 ${token_symbol}\n"
            f"💰 买入: ${WHALE_FOLLOW_SIZE_USD:.0f}  |  鲸鱼: @{trader} ${buy_usd:,.0f}\n"
            f"📊 MC: {('$' + format(mc/1e6, '.2f') + 'M') if mc >= 1_000_000 else ('$' + format(mc/1000, '.0f') + 'K') if mc > 0 else '新币'}  |  发币: {age_str}前\n"
            f"📌 GMGN: https://gmgn.ai/bsc/token/{token_addr}\n"
            f"⏰ {cst} CST"
        )
    else:
        _log(f"[FOLLOW BUY FAIL] {token_symbol} 买入失败")


async def _whale_follow_sell(token_addr: str, token_symbol: str,
                              trader: str, sell_usd: float):
    """鲸鱼卖出时跟卖。"""
    if not WHALE_FOLLOW_ENABLED:
        return
    rec = _WHALE_FOLLOW.get(token_addr)
    if not rec or rec["sold"]:
        return
    if rec["trader"] != trader:
        return  # 只跟原始触发布鲸鱼
    if sell_usd < rec["buy_usd"] * WHALE_FOLLOW_SELL_RATIO:
        _log(f"[FOLLOW] {token_symbol} 鲸鱼卖出 ${sell_usd:,.0f} < 门槛 ${rec['buy_usd']*WHALE_FOLLOW_SELL_RATIO:,.0f}，不跟卖")
        return

    _log(f"[FOLLOW SELL] {token_symbol} 鲸鱼 @{trader} 卖出 ${sell_usd:,.0f}，跟卖")
    rec["sold"] = True
    # 查链上余额再卖
    balance = await asyncio.to_thread(ex.get_token_balance, token_addr)
    result = await asyncio.to_thread(
        ex.sell_token, token_addr, token_symbol, 1.0, balance
    )
    import time as _t
    cst = _t.strftime("%H:%M:%S", _t.gmtime(_t.time() + 8*3600))
    status = "✅ 卖出成功" if result else "⚠️ 卖出失败，请手动处理"
    _notify(
        f"🐠 鲸鱼跟单 离场 ${token_symbol}\n"
        f"👌 鲸鱼 @{trader} 卖出 ${sell_usd:,.0f}，跟卖\n"
        f"{status}\n"
        f"⏰ {cst} CST"
    )
    _WHALE_FOLLOW.pop(token_addr, None)


def _interim_notify(msg: str):
    """中间状态通知：SIGNAL_ONLY 模式下只记日志，不推送给用户。"""
    from config import SIGNAL_ONLY
    if SIGNAL_ONLY:
        _log(f"[INTERIM] {msg.splitlines()[0][:100]}")
    else:
        _notify(msg)


async def _signal_notify(symbol: str, token_addr: str, msg_lines: list, mc: float = 0,
                           source: str = "kol", bypass_cooldown: bool = False,
                           classification: dict = None, kol_count: int = 1,
                           kol_grade: str = ""):
    """
    信号通知（KOL共买/FOMO共买）：分层过滤 + 信号分类 + 新卡片格式。
    source: 信号源名称（kol/multi/fomo）
    kol_grade: KOL 等级（S/A/B/C），用于选择分层过滤 tier
    classification: 可选，外部预计算的分类信息（KOL合并场景传入）
    """
    from config import TIERED_FILTERS
    # ── 确定过滤层级 ──
    if source == 'kol':
        # S/A KOL 使用 sa_kol tier, B/C KOL 使用 bc_kol tier
        if kol_grade in ("B", "C"):
            tier = TIERED_FILTERS['bc_kol']
        else:
            tier = TIERED_FILTERS['sa_kol']  # S/A 或未知等级默认用最宽松
    else:
        tier = TIERED_FILTERS.get(source, TIERED_FILTERS['multi'])

    # ── 每币每小时推送冷却（S/A KOL 可绕过）──
    if not bypass_cooldown and not _check_token_push_cooldown(token_addr):
        _log(f"[PUSH COOLDOWN] {symbol} 1h内已推送≥{_TOKEN_PUSH_MAX_PER_HR}次，静默 (source={source})")
        return
    # ── 首次出现检测 ──
    _is_first = _check_and_record_first_seen(token_addr)
    if _is_first:
        _log(f"[FIRST SEEN] {symbol} 首次出现在信号监控({source}) {token_addr[:12]}…")

    # ── 分层 MC 过滤 ──
    if mc > 0 and mc > tier['mc_max']:
        _log(f"[SIGNAL SKIP] {symbol} MC ${mc/1e6:.1f}M > ${tier['mc_max']/1e6:.0f}M ({source})上限，不推送")
        return

    # ── 分层币龄过滤 ──
    _age_h = 999
    try:
        from token_filter import get_token_info
        info = get_token_info(token_addr) or {}
        open_ts = info.get("open_timestamp")
        if open_ts:
            _age_h = (time.time() - int(open_ts)) / 3600
            age_days = _age_h / 24
            if age_days > tier['age_max_days']:
                _log(f"[SIGNAL SKIP] {symbol} 创建 {age_days:.1f}天 > {tier['age_max_days']}天 ({source})，不推送")
                _old_token_surge_record(token_addr, msg_lines[0] if msg_lines else "", 0, source)
                return
        else:
            from executor import get_token_age_sec
            _age_sec = get_token_age_sec(token_addr)
            if _age_sec is not None:
                _age_h = _age_sec / 3600
                age_days = _age_h / 24
                if age_days > tier['age_max_days']:
                    _log(f"[SIGNAL SKIP] {symbol} 创建 {age_days:.1f}天 > {tier['age_max_days']}天 ({source} DexScreener兜底)，不推送")
                    _old_token_surge_record(token_addr, msg_lines[0] if msg_lines else "", 0, source)
                    return
            else:
                # 新币无法获取币龄 → 假设为新币放行
                _log(f"[SIGNAL] {symbol} 无法获取币龄，假设为新币放行 ({source})")
                _age_h = 0
    except Exception as _e:
        _log(f"[SIGNAL] {symbol} 币龄检查异常: {_e}，假设为新币放行 ({source})")
        _age_h = 0

    # ── Dev 过滤 ──
    try:
        from token_filter import get_token_dev_info
        _dev = get_token_dev_info(token_addr) or {}
        _dev_count = int(_dev.get("twitter_create_token_count") or _dev.get("creator_open_count") or 0)
        if _dev_count > tier['dev_max']:
            _log(f"[SIGNAL SKIP] {symbol} Dev 发币 {_dev_count} 次 > {tier['dev_max']} 次，不推送")
            return
    except Exception:
        pass

    # ── 安全扩展检查 (#3 #5 #8) ──
    _safety = await asyncio.to_thread(run_safety_extensions, token_addr, symbol)
    if _safety['verdict'] == 'reject':
        _log(f"[SIGNAL SKIP] {symbol} 安全扩展拒绝: {'; '.join(_safety['rejects'])}")
        return
    if _safety['warnings']:
        _log(f"[SIGNAL WARN] {symbol} 安全扩展警告: {'; '.join(_safety['warnings'])}")

    # ── 调用共享分析 ──
    _analysis = await _build_token_analysis(token_addr, symbol, mc, trader="", buy_usd=0, grade="C")
    _analysis_lines = _analysis["lines"]
    _score = _analysis["score"]
    _rating = _analysis["rating"]

    # ── 信号分类（如未外部传入） ──
    if not classification:
        # 获取 multi_count 从 msg_lines 解析
        _mc = 0
        for line in msg_lines:
            if '人共买' in line or '人同时' in line:
                import re as _re
                _n = _re.search(r'(\d+)人', line)
                if _n:
                    _mc = int(_n.group(1))
                    break
        classification = _classify_signal(
            source=source, mc=mc, age_hours=_age_h,
            trend_phase='', multi_count=_mc, kol_count=kol_count)

    # ── 新卡片格式 ──
    _first_tag = "🆕 首次出现 " if _is_first else ""
    beijing_time = time.strftime("%H:%M:%S", time.gmtime(time.time() + 8*3600)) + " CST"

    header = f"{classification['emoji']} {classification['category']} | {classification['priority']}优先 | 评分 {_score}/10"
    separator = "─" * 18
    hold_line = f"{classification['hold_emoji']} {classification['hold_hint']}"

    lines = [
        header,
    ]
    if msg_lines:
        lines.append(_first_tag + msg_lines[0] if _first_tag else msg_lines[0])
        lines.extend(msg_lines[1:])
    lines += [
        separator,
        hold_line,
        separator,
    ]
    lines += _analysis_lines
    lines += ["", f"📌 GMGN: https://gmgn.ai/bsc/token/{token_addr}", f"⏰ {beijing_time}"]

    _notify("\n".join(lines))
    _log(f"[SIGNAL NOTIFY SENT] {symbol} source={source} score={_score} cat={classification['category']} priority={classification['priority']}")

    # ── 影子跟踪注册 ──
    try:
        shadow_register(token_addr, symbol,
                        classification['category'], classification['priority'],
                        _score, mc, _analysis.get('entry_price') or 0,
                        source=source, trader='', grade='')
    except Exception:
        pass


async def _signal_notify_with_meta(symbol: str, token_addr: str, msg_lines: list, mc: float = 0,
                                  source: str = "kol", bypass_cooldown: bool = False,
                                  classification: dict = None, kol_count: int = 1,
                                  kol_grade: str = ""):
    """带预计算分类的信号推送（KOL合并场景使用）。"""
    await _signal_notify(symbol, token_addr, msg_lines, mc=mc, source=source,
                         bypass_cooldown=bypass_cooldown,
                         classification=classification, kol_count=kol_count,
                         kol_grade=kol_grade)
# ──────────────────────────────────────────────
# 市值 + 趋势过滤（已拆分到 market_utils.py）
# ──────────────────────────────────────────────
from market_utils import init_market_utils, fetch_market_cap, check_high_mc_trend, is_whale, get_whale_grade, get_whale_sell_level
# ──────────────────────────────────────────────
# 多人共买窗口跟踪
# ──────────────────────────────────────────────

# token_addr → [ {trader, ts, usd_value} ]
_MULTI_BUY_WINDOW: dict = defaultdict(list)

# ── 老币活跃度突变跟踪 ──────────────────────────────────────────────
# 当老币（创建 >3 天）在 OLD_TOKEN_SURGE_WINDOW_MIN 分钟内出现 >= OLD_TOKEN_SURGE_THRESHOLD 笔买入时触发
# token_addr → [ {trader, ts, usd, source} ]
_OLD_TOKEN_SURGE: dict = defaultdict(list)
# token_addr → 上次触发推送的 timestamp（冷却去重）
_OLD_TOKEN_SURGE_LAST_PUSH: dict = {}  # token_addr → ts

# B/C 级 KOL 等待确认队列：{token_addr: [{trader, grade, score, ts}, ...]}
_KOL_CONFIRM_WINDOW: dict = defaultdict(list)

# 鲸鱼大额买入确认窗口：B 级鲸鱼需在 WHALE_CONFIRM_WINDOW_MIN 内搭配另一信号
_WHALE_CONFIRM_WINDOW: dict = {}   # token_addr → {ts, usd}

# 策略W C级等待队列：{token_addr: {ts, usd, trigger_handle}}
# 触发后等 5min 验证「新地址买入」，通过才正式开仓
_WHALE_W_PENDING: dict = {}   # token_addr → {ts, usd, trigger_handle}
_WHALE_W_CONFIRM_SEC = 5 * 60   # 等待 5 分钟
_WHALE_W_MIN_NEW_BUYERS = 2     # 至少 2 个新地址 $5k+ 买入
_WHALE_W_NEW_BUYER_MIN_USD = 5_000
_KOL_CONFIRM_TTL = 300   # 5分钟内第2人确认才有效

# ── Deployer 实时 Transfer 监控 ───────────────────────────────────────────────
# ERC-20 Transfer 事件 topic
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
MAX_DEPLOYER_MONITORS = 20    # 最多同时监控 20 个 token，防止订阅过载
DEPLOYER_SELL_THRESHOLD = 0.10  # Deployer 卸弹比例 >= 10% 触发全清

_deployer_monitors: dict = {}  # token_addr_lower → asyncio.Task


async def _get_total_supply_raw(token_addr: str) -> float:
    """通过 eth_call totalSupply() 获取原始整数总供应量（同 Transfer.value 单位）。"""
    from config import WS_RPC_URL
    try:
        http_url = WS_RPC_URL.replace("wss://", "https://").replace("ws://", "http://")
        r = requests.post(
            http_url,
            json={
                "jsonrpc": "2.0", "id": 1,
                "method": "eth_call",
                "params": [{"to": token_addr, "data": "0x18160ddd"}, "latest"],
            },
            timeout=5,
            proxies={"https": "socks5://REDACTED_PROXY_CREDENTIALS",
                     "http":  "socks5://REDACTED_PROXY_CREDENTIALS"},
        )
        result = r.json().get("result", "0x0") or "0x0"
        return float(int(result, 16))
    except Exception as e:
        _log(f"[DEPLOYER] totalSupply 查询失败: {e}")
        return 0.0


async def _watch_deployer_transfers(
    token_addr: str, token_symbol: str,
    deployer_addr: str, total_supply: float
):
    """
    订阅 token 合约的 Transfer 事件，过滤 from==deployer。
    累计卡出量 / 总供应量 >= DEPLOYER_SELL_THRESHOLD → 全清并推送 Jarvis。
    平仓后自动取消订阅。
    """
    from config import WS_RPC_URL
    key         = token_addr.lower()
    sold_raw    = 0.0
    threshold   = total_supply * DEPLOYER_SELL_THRESHOLD

    _log(f"[DEPLOYER WATCH] {token_symbol} 开始监控 deployer {deployer_addr[:12]}…"
         f" 总供应 {total_supply:.0f}，触发线 {threshold:.0f} ({DEPLOYER_SELL_THRESHOLD:.0%})")
    try:
        async with websockets.connect(
            WS_RPC_URL, ping_interval=20, ping_timeout=10, open_timeout=15
        ) as ws:
            await ws.send(json.dumps({
                "jsonrpc": "2.0", "id": 88,
                "method": "eth_subscribe",
                "params": ["logs", {
                    "address": token_addr,
                    "topics":  [TRANSFER_TOPIC],
                }],
            }))
            r = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            _log(f"[DEPLOYER WATCH] {token_symbol} Transfer 订阅 id={r.get('result')}")

            async for raw in ws:
                # 平仓后自动退出
                if not pm.position_exists(token_addr):
                    _log(f"[DEPLOYER WATCH] {token_symbol} 仓位已关闭，取消订阅")
                    return

                msg = json.loads(raw)
                if "params" not in msg:
                    continue
                log    = msg["params"]["result"]
                tpics  = log.get("topics", [])
                # Transfer(from, to, value): topics[1]=from, data=value
                if len(tpics) < 3:
                    continue
                from_addr = ("0x" + tpics[1][-40:]).lower()
                if from_addr != deployer_addr.lower():
                    continue

                # 解析 value（uint256，原始整数）
                try:
                    val_hex  = log.get("data", "0x0") or "0x0"
                    sold_raw += int(val_hex, 16)
                except Exception:
                    continue

                ratio = sold_raw / total_supply if total_supply > 0 else 0
                _log(f"[DEPLOYER WATCH] {token_symbol} deployer 已卖出 {ratio:.1%}")

                if sold_raw >= threshold:
                    _log(f"[DEPLOYER WATCH] {token_symbol} ⚠️ deployer 卖出 {ratio:.1%} >= {DEPLOYER_SELL_THRESHOLD:.0%}，触发全清")
                    _notify(
                        f"🚨 {token_symbol} Deployer 实时出货！\n"
                        f"创建者 {deployer_addr[:16]}...\n"
                        f"已卖出 {ratio:.1%} 总供应量 → 全清"
                    )
                    await execute_exit(token_addr, token_symbol, {
                        "action": "full_sell", "sell_pct": 1.0,
                        "reason": f"Deployer 实时出货 {ratio:.1%} 总供应量",
                        "exit_reason": "liquidity_crash",
                    })
                    return
    except asyncio.CancelledError:
        _log(f"[DEPLOYER WATCH] {token_symbol} 监控任务已取消")
    except Exception as e:
        _log(f"[DEPLOYER WATCH] {token_symbol} 监控异常: {e}")
    finally:
        _deployer_monitors.pop(key, None)


async def start_deployer_monitor(token_addr: str, token_symbol: str):
    """开仓后启动 Deployer 实时监控任务。"""
    from token_filter import get_token_deployer
    key = token_addr.lower()

    if len(_deployer_monitors) >= MAX_DEPLOYER_MONITORS:
        _log(f"[DEPLOYER WATCH] 已达上限 {MAX_DEPLOYER_MONITORS}，跳过 {token_symbol}")
        return
    if key in _deployer_monitors:
        return  # 已在监控

    try:
        deployer_addr = await asyncio.to_thread(get_token_deployer, token_addr)
        if not deployer_addr:
            _log(f"[DEPLOYER WATCH] {token_symbol} 无法获取 deployer，跳过")
            return

        total_supply = await _get_total_supply_raw(token_addr)
        if total_supply <= 0:
            _log(f"[DEPLOYER WATCH] {token_symbol} 总供应量为 0，跳过")
            return

        task = asyncio.create_task(
            _watch_deployer_transfers(token_addr, token_symbol, deployer_addr, total_supply)
        )
        _deployer_monitors[key] = task
        _log(f"[DEPLOYER WATCH] {token_symbol} 监控任务已启动，当前监控数={len(_deployer_monitors)}")
    except Exception as e:
        _log(f"[DEPLOYER WATCH] {token_symbol} 启动失败: {e}")

# ── 高位币当日进出计数器 ──────────────────────────────────────────────────────────────
# {token_addr_lower: {"count": int, "net_pnl": float, "locked": bool}}
_HIGH_MC_DAILY_TRADES: dict = {}
_HIGH_MC_DAILY_DATE:   str  = ""    # 日期字符串，用于日切换重置


def _high_mc_date_reset():
    """UTC 00:00 跨日时自动重置高位币当日计数器。"""
    global _HIGH_MC_DAILY_DATE
    today = __import__('datetime').datetime.utcnow().strftime("%Y-%m-%d")
    if _HIGH_MC_DAILY_DATE != today:
        _HIGH_MC_DAILY_TRADES.clear()
        _HIGH_MC_DAILY_DATE = today


def record_high_mc_trade(token_addr: str, entry_mc: float):
    """buy_token 成功后记录高位币交易次数。"""
    from config import HIGH_MC_POSITION_HALF
    if entry_mc < HIGH_MC_POSITION_HALF:
        return
    _high_mc_date_reset()
    key = token_addr.lower()
    if key not in _HIGH_MC_DAILY_TRADES:
        _HIGH_MC_DAILY_TRADES[key] = {"count": 0, "net_pnl": 0.0, "locked": False}
    _HIGH_MC_DAILY_TRADES[key]["count"] += 1
    _log(f"[HIGH-MC] {key[:10]}… 当日交易第 {_HIGH_MC_DAILY_TRADES[key]['count']} 次")


def update_high_mc_pnl(token_addr: str, realized_pnl_usd: float, entry_mc: float):
    """position 卖出后更新高位币当日净盈亏并检查是否触发锁定。"""
    from config import HIGH_MC_POSITION_HALF, HIGH_MC_DAILY_TRADE_MAX
    if entry_mc < HIGH_MC_POSITION_HALF:
        return
    _high_mc_date_reset()
    key = token_addr.lower()
    if key not in _HIGH_MC_DAILY_TRADES:
        return
    _HIGH_MC_DAILY_TRADES[key]["net_pnl"] += realized_pnl_usd
    rec = _HIGH_MC_DAILY_TRADES[key]
    if rec["count"] >= HIGH_MC_DAILY_TRADE_MAX and rec["net_pnl"] < 0 and not rec["locked"]:
        rec["locked"] = True
        _notify(
            f"🔒 高位币当日锁定：{key[:10]}…\n"
            f"当日交易 {rec['count']} 次，净亏损 ${abs(rec['net_pnl']):.1f}\n"
            f"今日不再买入"
        )
        _log(f"[HIGH-MC LOCK] {key[:10]}… 当日锁定，净亏损 ${abs(rec['net_pnl']):.1f}")


def is_high_mc_daily_locked(token_addr: str) -> bool:
    _high_mc_date_reset()
    return _HIGH_MC_DAILY_TRADES.get(token_addr.lower(), {}).get("locked", False)


def _kol_confirm_add(token_addr: str, token: str, trader: str,
                     grade: str, score: int, size_usd: float):
    """B/C 级 KOL 买入时加入确认队列，达到 MULTI_CONFIRM_MIN 人则自动触发。返回 (should_buy, reason)"""
    now = time.time()
    window = _KOL_CONFIRM_WINDOW[token_addr]

    # 清理过期条目
    window[:] = [e for e in window if now - e["ts"] < _KOL_CONFIRM_TTL]

    # 防重复：同一个 trader 不重复计入
    if any(e["trader"].lower() == trader.lower() for e in window):
        return

    window.append({"trader": trader, "grade": grade, "score": score,
                   "size_usd": size_usd, "ts": now})

    traders_str = ", ".join(f"@{e['trader']}({e['grade']})​" for e in window)
    _log(f"[KOL PENDING] {token} | B/C级 {grade}级 @{trader}({score}分) "
         f"| 队列 {len(window)}/{MULTI_CONFIRM_MIN} 人: {traders_str}")

    if len(window) >= MULTI_CONFIRM_MIN:
        # 达到阈値，用内部最高评分的 size 下单
        best = max(window, key=lambda e: e["score"])
        final_size = best["size_usd"]
        _log(f"[SIGNAL-A确认] {token} | {len(window)}个B/C级KOL共同确认！"
             f"将用 @{best['trader']} 的仓位大小 {final_size:.0f}U")
        # 清空队列防止重复触发
        _KOL_CONFIRM_WINDOW[token_addr] = []
        # 异步触发不能直接用 await，改用 asyncio.create_task
        loop = asyncio.get_event_loop()
        loop.create_task(try_open_position(
            token_addr=token_addr, token_symbol=token,
            strategy="kol", size_usd=final_size,
            trigger_kol="|".join(e["trader"] for e in window),
        ))

    # ── KOL 共买 >3 人：额外推送通知 ──
    if len(window) >= 4:
        traders_str_full = ", ".join(f"@{e['trader']}({e['grade']})" for e in window)
        _log(f"[KOL MULTI] {token} | {len(window)}个KOL共买！{traders_str_full}")
        asyncio.create_task(_signal_notify(token, token, token_addr, [
            f"👥 KOL共买信号 ({len(window)}人)",
            f"👤 {traders_str_full}",
            f"→ {len(window)}个B/C级KOL同时买入，共识强烈",
        ], source="kol", kol_grade="B"))


def _cleanup_window(token_addr: str):
    """移除超过时间窗口的记录"""
    cutoff = time.time() - MULTI_BUY_WINDOW_MIN * 60
    _MULTI_BUY_WINDOW[token_addr] = [
        e for e in _MULTI_BUY_WINDOW[token_addr]
        if e["ts"] >= cutoff
    ]


def record_multi_buy(token_addr: str, trader: str, usd_value: float) -> bool:
    """
    记录一笔买入到窗口，返回 True 表示达到触发阈值。
    multi 策略已开仓的 token 直接清除窗口、跳过计数（防日志刷屏）。
    """
    # 已开过 multi 仓 → 清窗口不再累积
    if pm.position_exists(token_addr):
        pos = pm.get_position(token_addr)
        if "multi" in pos.get("strategies", []):
            _MULTI_BUY_WINDOW.pop(token_addr, None)
            return False

    _cleanup_window(token_addr)
    existing_traders = {e["trader"] for e in _MULTI_BUY_WINDOW[token_addr]}
    if trader not in existing_traders:
        _MULTI_BUY_WINDOW[token_addr].append({
            "trader": trader,
            "ts": time.time(),
            "usd": usd_value,
        })
    count = len(_MULTI_BUY_WINDOW[token_addr])
    return count >= MULTI_BUY_THRESHOLD


# ──────────────────────────────────────────────
# 策略 A 辅助：KOL 卖出量级过滤 + 价格行为确认
# ──────────────────────────────────────────────

# KOL 等级 → 最低记录金额门槛（低于此金额直接静默忽略）
_KOL_SELL_MIN_USD: dict = {
    "S": 500,
    "A": 300,
    "B": 200,
    "C": 50,
}

# 5 分钟窗口内的 KOL 卖出聚合记录
# {token_addr: [ {trader, grade, usd_value, ts} ]}
_KOL_SELL_WINDOW: dict = defaultdict(list)
_KOL_SELL_WINDOW_SEC = 5 * 60   # 5 分钟


def _kol_sell_above_min(grade: str, usd_value: float) -> bool:
    """量级过滤：是否达到记录门槛"""
    return usd_value >= _KOL_SELL_MIN_USD.get(grade, 100)


def _kol_sell_record_window(token_addr: str, trader: str, grade: str, usd_value: float):
    """将满足量级的卖出事件写入 5min 窗口聚合"""
    now = time.time()
    window = _KOL_SELL_WINDOW[token_addr.lower()]
    # 清理超期
    window[:] = [e for e in window if now - e["ts"] < _KOL_SELL_WINDOW_SEC]
    # 同一 trader 不重复记录（同一人限列最新一条）
    window[:] = [e for e in window if e["trader"].lower() != trader.lower()]
    window.append({"trader": trader, "grade": grade, "usd_value": usd_value, "ts": now})


def _get_kol_sell_window(token_addr: str) -> list:
    """返回 5min 窗口内的有效卖出记录（已去重）"""
    now = time.time()
    window = _KOL_SELL_WINDOW[token_addr.lower()]
    window[:] = [e for e in window if now - e["ts"] < _KOL_SELL_WINDOW_SEC]
    return list(window)


def _get_price_behavior(token_addr: str) -> str:
    """
    判断当前价格行为：confirmed / contradicted / neutral
    - confirmed：5min 价格下跌 > 5% 且 1min 下跌 > 2%
    - contradicted：5min 价格上涨 > 3%
    - neutral：其他情况
    """
    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{token_addr}",
            timeout=5,
            proxies={"https": "socks5://REDACTED_PROXY_CREDENTIALS"},
        )
        pairs = r.json().get("pairs", [])
        if not pairs:
            return "neutral"
        p = sorted(pairs, key=lambda x: float((x.get("liquidity") or {}).get("usd", 0) or 0), reverse=True)[0]
        chg_5m = float(p.get("priceChange", {}).get("m5", 0) or 0)
        # DexScreener 不提供 1min 数据，用 5min 的一半作为近似
        if chg_5m < -5:
            return "confirmed"
        if chg_5m > 3:
            return "contradicted"
        return "neutral"
    except Exception:
        return "neutral"


def _get_price_1h_change(token_addr: str) -> float:
    """
    返回该 token 过1h的价格变化百分比（小数，如 -0.15 = 跌 15%）。
    查不到时返回 0.0（按中性处理，不触发极端兜底）。
    """
    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{token_addr}",
            timeout=5,
            proxies={"https": "socks5://REDACTED_PROXY_CREDENTIALS"},
        )
        pairs = r.json().get("pairs", [])
        if not pairs:
            return 0.0
        p = sorted(pairs, key=lambda x: float((x.get("liquidity") or {}).get("usd", 0) or 0), reverse=True)[0]
        chg_1h = float(p.get("priceChange", {}).get("h1", 0) or 0)
        return chg_1h / 100.0   # DexScreener 返回的是百分比数字（如 -15 = -15%）
    except Exception:
        return 0.0

# ──────────────────────────────────────────────
# 信号处理
# ──────────────────────────────────────────────


async def _whale_w_confirm(token_addr: str, token_symbol: str,
                           trigger_handle: str, trigger_usd: float):
    """
    策略W 新地址验证协程：
    每 10 秒轮询一次 GeckoTerminal，满足条件立刻开仓，不等满 5 分钟。
    同时检查价格漂移：当前价相对触发时价格涨幅 >= 50% 则放弃。
    最长等待 _WHALE_W_CONFIRM_SEC（5min）。
    """
    trigger_price = await asyncio.to_thread(ex._get_token_price, token_addr)
    _log(f"[WHALE-W QUEUE] {token_symbol} 触发价格快照: ${trigger_price:.8f}，开始10s轮询...")

    deadline = time.time() + _WHALE_W_CONFIRM_SEC
    poll_interval = 10  # 10s轮询，5min窗口内最多30次机会
    elapsed = 0

    while time.time() < deadline:
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval

        if pm.position_exists(token_addr):
            _log(f"[WHALE-W CONFIRM] {token_symbol} 已有仓位，停止轮询")
            _WHALE_W_PENDING.pop(token_addr, None)
            return

        if token_addr not in _WHALE_W_PENDING:
            _log(f"[WHALE-W CONFIRM] {token_symbol} 队列已清，停止轮询")
            return

        # 价格漂移检查：涨超 50% → 放弃
        if trigger_price and trigger_price > 0:
            cur_price = await asyncio.to_thread(ex._get_token_price, token_addr)
            if cur_price and cur_price > 0:
                drift_pct = (cur_price - trigger_price) / trigger_price
                if drift_pct >= 0.50:
                    _WHALE_W_PENDING.pop(token_addr, None)
                    _log(f"[WHALE-W DRIFT] {token_symbol} 价格已涨 {drift_pct:.0%}"
                         f"（触发价${trigger_price:.8f} -> 现价${cur_price:.8f}），放弃高位追涨")
                    _log(f"[WHALE-W DRIFT] {token_symbol} 价格漂移放弃，不推送")  # SIGNAL_ONLY: 纯噪音
                    return

        # 查新地址买入
        new_buyers = await asyncio.to_thread(
            get_recent_new_buyers,
            token_addr,
            trigger_handle,
            elapsed + 10,
            _WHALE_W_NEW_BUYER_MIN_USD,
        )
        count = len(new_buyers)
        total_new_usd = sum(b["usd"] for b in new_buyers)
        _log(f"[WHALE-W POLL] {token_symbol} +{elapsed}s | 新地址: {count}个 ${total_new_usd:,.0f}")

        if count >= _WHALE_W_MIN_NEW_BUYERS:
            pending = _WHALE_W_PENDING.pop(token_addr, {})
            _is_s3  = pending.get("signal_grade") == "S"
            _w_size = SIZE_SIGNAL_S if _is_s3 else WHALE_BUY_SIZE_USD
            buyers_str = ", ".join(
                f"{b['address'][:10]}...(${b['usd']:,.0f})" for b in new_buyers[:4]
            )
            _log(f"[WHALE-W PASS] {token_symbol} +{elapsed}s {count}个新地址 ${total_new_usd:,.0f} -> 开仓${_w_size}U")
            await _signal_notify(token_symbol, token_addr, [
                f"✅ 巨鲸验证通过信号 ${token_symbol}",
                f"⏱ 耗时: +{elapsed}s",
                f"📊 {count}个新地址跟买",
                f"💰 合计: ${total_new_usd:,.0f}",
                f"👤 {buyers_str}",
                f"→ 新地址跟买验证通过",
            ], source="whale")
            _pb_ts = _PENDING_BUY.get(token_addr, 0)
            if not _pb_ts or time.time() - _pb_ts >= _PENDING_BUY_ACTIVE_TTL:
                _PENDING_BUY[token_addr] = time.time()
                try:
                    await _try_open_position_inner(
                        token_addr=token_addr, token_symbol=token_symbol,
                        strategy="whale", size_usd=_w_size,
                        extra_fields={
                            **({"signal_grade": "S"} if _is_s3 else {}),
                            "whale_new_buyers":     count,
                            "whale_new_buyers_usd": total_new_usd,
                            "whale_confirm_sec":    elapsed,
                        },
                    )
                finally:
                    _PENDING_BUY[token_addr] = time.time()
            return

    # 5 分钟超时
    pending = _WHALE_W_PENDING.pop(token_addr, {})
    if pending:
        _log(f"[WHALE-W TIMEOUT] {token_symbol} 5min内未满足条件，放弃")
        _log(f"[WHALE-W TIMEOUT] {token_symbol} 超时放弃 @{trigger_handle} ${trigger_usd:,.0f}")  # SIGNAL_ONLY: 纯噪音，不推送

async def handle_alert(alert: dict):
    chain      = alert.get("chain", "")
    alert_type = alert.get("alertType") or alert.get("type", "")  # WS 用 alertType，REST 用 type
    trader     = alert.get("trader", "")
    token      = alert.get("token", "")
    token_addr = (alert.get("tokenAddress") or "").lower()
    usd_value  = alert.get("usdValue") or 0
    text       = alert.get("text", "")

    # 只处理罗宾汉链
    if chain != "robinhood":
        return

    _log(f"[ALERT] {alert_type:4s} | @{trader:20s} | {token:10s} | ${usd_value:,.0f} | {token_addr[:10]}…")

    # ── 卖出信号：KOL 卖出检测 ──────────────
    if alert_type == "sell":
        # 判断是否是我们 watchlist 中的 KOL（按 handle 比对，alert 里没有 evm 地址）
        _, fomo_map_live, _, _ = await _get_kol_maps()
        kol_sell_info = fomo_map_live.get(trader.lower())   # {evm, score, grade} 或 None
        is_kol = kol_sell_info is not None
        # ── 场外强手大额卖出 → signal_score 扣分（非白名单，持仓中）──────────
        if not is_kol and token_addr and pm.position_exists(token_addr)                 and usd_value >= OUTSIDER_SELL_SCORE_THRESHOLD:
            _ns_out = pm.add_signal_score(
                token_addr, -OUTSIDER_SELL_SCORE_DELTA,
                reason=f"场外强手 @{trader} ${usd_value:,.0f} 大额卖出")
            _log(f"[OUTSIDER-] {token} @{trader} ${usd_value:,.0f} 大额卖出"
                 f" signal_score-{OUTSIDER_SELL_SCORE_DELTA}→{_ns_out}")

        if is_kol and token_addr and pm.position_exists(token_addr):
            kol_grade_sell = kol_sell_info.get("grade", "C")
            kol_addr_sell  = kol_sell_info.get("evm", trader)
            pos_sell       = pm.get_position(token_addr)

            # ── 策略 A 第一步：量级过滤 ──────────────────────────────────────
            if not _kol_sell_above_min(kol_grade_sell, usd_value):
                _log(f"[KOL SELL IGNORE] {token} @{trader}({kol_grade_sell}级) 卖出 ${usd_value:,.0f}"
                     f" < 量级门槛 ${_KOL_SELL_MIN_USD.get(kol_grade_sell, 100)}，静默忽略")
                return

            # ── S 级共识持仓模式：走专属路径 ────────────────────────────────────────────
            if pm.is_s_consensus(token_addr):
                # 先收集当前 kol_sells 供硬止损共振检测
                _s_state   = pm._load()
                _ks_list   = _s_state.get("kol_sells", {}).get(token_addr, [])

                # 1. 硬止损检查（庄出货 / KOL共振 → 无视 score 全清）
                s_hard = pm.s_consensus_hard_stop(
                    token_addr, trader, usd_value, _ks_list)
                if s_hard:
                    s_hard["exit_reason"] = "kol_sell"
                    _log(f"[S-HARD STOP] {token} KOL @{trader} 触发S级硬止损")
                    pm.record_kol_sell(token_addr, trader, usd_value)
                    await execute_exit(token_addr, token, s_hard)
                    return

                # 2. 软信号：score - KOL_SELL_SCORE_PENALTY，按区间分批离场
                from config import S_SCORE_KOL_SELL_SMALL
                new_sc, s_action = pm.s_consensus_apply_score(
                    token_addr, -S_SCORE_KOL_SELL_SMALL,
                    reason=f"KOL @{trader}({kol_grade_sell}级)卖出")
                pm.record_kol_sell(token_addr, trader, usd_value)
                if s_action:
                    if s_action.get("action") == "full_sell":
                        s_action["exit_reason"] = "kol_sell"
                    else:
                        s_action.setdefault("exit_reason", "take_profit")
                    _notify(f"🔔 {token} [S级] KOL @{trader}({kol_grade_sell}级)卖出\n"
                            f"signal_score → {new_sc}\n"
                            f"决策: {s_action['reason']}")
                    await execute_exit(token_addr, token, s_action)
                else:
                    _notify(f"⚠️ {token} [S级] KOL @{trader}({kol_grade_sell}级)卖出\n"
                            f"signal_score → {new_sc}（高于持有线 60，持仓不动）")
                    _log(f"[KOL SELL] {token} S级持有线以上，不减仓")
                return

            # ── 普通策略（A/B/W）卖出路径 — 按策略分层 ──────────────────────────────────────
            pm.record_kol_sell(token_addr, trader, usd_value)

            open_strategy = pos_sell.get("open_strategy", "") if pos_sell else ""

            # ── 策略A 路径 ──────────────────────────────────────────────────────────────────────
            if open_strategy == "A":
                # P1: 触发者KOL卖出 且 金额 >= $2,000 → 全清（最高优先级）
                from config import STRATEGY_A_TRIGGER_SELL_MIN_USD
                trig_addrs   = [k.get("address", "").lower() for k in pos_sell.get("trigger_kols", [])]
                trig_handles = [k.get("handle", "").lower()  for k in pos_sell.get("trigger_kols", [])]
                is_trigger = (kol_addr_sell.lower() in trig_addrs or trader.lower() in trig_handles)

                if is_trigger:
                    if usd_value >= STRATEGY_A_TRIGGER_SELL_MIN_USD:
                        _log(f"[KOL SELL A-P1] {token} 触发者 @{trader} 卖出 ${usd_value:,.0f} → 全清")
                        pos_sell["trigger_kol_exited"] = True
                        pm._save_pos(token_addr, pos_sell)
                        await execute_exit(token_addr, token, {
                            "action": "full_sell", "sell_pct": 1.0,
                            "reason": f"策略A触发者KOL @{trader} 离场 ${usd_value:,.0f}",
                            "exit_reason": "kol_sell",
                        })
                    else:
                        # 金额 < $2,000：只记录，不操作
                        _log(f"[KOL SELL A] {token} 触发者 @{trader} 小额卖出 ${usd_value:,.0f} < "
                             f"${STRATEGY_A_TRIGGER_SELL_MIN_USD:,.0f}，静默忽略")
                    return

                # 非触发者：极端兜底检查
                # 5min 内 >= STRATEGY_A_EXTREME_SELL_COUNT 个KOL卖出 且 1h价格跌幅 > STRATEGY_A_EXTREME_PRICE_DROP
                from config import STRATEGY_A_EXTREME_SELL_COUNT, STRATEGY_A_EXTREME_PRICE_DROP
                _kol_sell_record_window(token_addr, trader, kol_grade_sell, usd_value)
                _sell_window_a = _get_kol_sell_window(token_addr)
                _sell_count_a  = len(_sell_window_a)

                if _sell_count_a >= STRATEGY_A_EXTREME_SELL_COUNT:
                    _price_1h_chg = await asyncio.to_thread(_get_price_1h_change, token_addr)
                    _log(f"[KOL SELL A] {token} 非触发者 5min {_sell_count_a}人卖出 | 1h涨跌: {_price_1h_chg:.1%}")
                    if _price_1h_chg <= STRATEGY_A_EXTREME_PRICE_DROP:
                        _log(f"[KOL SELL A-EXTREME] {token} 5min {_sell_count_a}人 + 1h跌{_price_1h_chg:.1%} → 极端兜底全清")
                        _notify(
                            f"🚨 {token} [策略A] 极端兜底全清\n"
                            f"5min内{_sell_count_a}个KOL卖出 + 1h跌幅{_price_1h_chg:.1%}\n"
                            f"触发极端兜底离场"
                        )
                        await execute_exit(token_addr, token, {
                            "action": "full_sell", "sell_pct": 1.0,
                            "reason": f"策略A极端兜底：5min{_sell_count_a}人KOL卖出+1h跌{_price_1h_chg:.1%}",
                            "exit_reason": "kol_sell",
                        })
                        return

                # 非触发者且未触发极端兜底：仅更新 signal_score，不操作
                _new_score_a = pm.add_signal_score(
                    token_addr, -KOL_SELL_SCORE_PENALTY,
                    reason=f"策略A非触发者KOL @{trader}({kol_grade_sell}级)卖出")
                _log(f"[KOL SELL A] {token} 非触发者 @{trader}({kol_grade_sell}级) "
                     f"${usd_value:,.0f}，仅扣分 signal_score→{_new_score_a}")
                return

            # ── 策略B 路径 ──────────────────────────────────────────────────────────────────────
            elif open_strategy == "B":
                # 正常走 signal_score 扣分，巡检时 check_exit_signal 按分数触发减仓
                new_score_b, action_score_b = update_signal_on_kol_sell(
                    token_addr, kol_addr_sell, kol_grade_sell)
                if action_score_b:
                    if action_score_b.get("action") == "full_sell":
                        action_score_b["exit_reason"] = "kol_sell"
                    else:
                        action_score_b.setdefault("exit_reason", "stop_loss")
                    _log(f"[KOL SELL B] {token} signal_score={new_score_b} → {action_score_b['action']}")
                    _notify(f"🔔 {token} [策略B] KOL @{trader}({kol_grade_sell}级)卖出\n"
                            f"signal_score → {new_score_b}\n决策: {action_score_b['reason']}")
                    await execute_exit(token_addr, token, action_score_b)
                else:
                    _notify(f"⚠️ {token} [策略B] KOL @{trader}({kol_grade_sell}级)卖出 ${usd_value:,.0f}\n"
                            f"signal_score → {new_score_b}（信号仍强，不操作）")
                    _log(f"[KOL SELL B] {token} score={new_score_b}，信号仍强，不操作")
                return

            # ── 策略W 路径 ──────────────────────────────────────────────────────────────────────
            elif open_strategy == "W":
                # 策略W 的 KOL 卖出信号完全忽略，只记录日志
                _log(f"[KOL SELL W] {token} 策略W收到KOL卖出信号（忽略）："
                     f"@{trader}({kol_grade_sell}级) ${usd_value:,.0f}")
                return

            # ── 未知策略兜底路径 ────────────────────────────────────────────────────────────────
            else:
                new_score, action_score = update_signal_on_kol_sell(
                    token_addr, kol_addr_sell, kol_grade_sell)
                if action_score:
                    if action_score.get("action") == "full_sell":
                        action_score["exit_reason"] = "kol_sell"
                    else:
                        action_score.setdefault("exit_reason", "stop_loss")
                    _log(f"[KOL SELL ?] {token} signal_score={new_score} → {action_score['action']}")
                    await execute_exit(token_addr, token, action_score)
                else:
                    _log(f"[KOL SELL ?] {token} score={new_score}，信号仍强，不操作")

        # ── Deployer 出货检测（持仓期间，任意金额）──────────────────────────
        # 非 KOL 地址卖出 → 检查是否是 deployer，若是立即全清
        elif not is_kol and token_addr and pm.position_exists(token_addr):
            try:
                from token_filter import get_token_deployer
                _dep_addr = await asyncio.to_thread(get_token_deployer, token_addr)
                # fomoapi alert 中 trader 是 evm 地址（小写）
                _trader_addr = (alert.get("walletAddress") or alert.get("wallet") or trader or "").lower()
                if _dep_addr and _trader_addr and _dep_addr == _trader_addr:
                    _dep_reason = (f"Deployer 出货: 创建者 {_dep_addr[:12]}… 卖出 ${usd_value:,.0f}")
                    _log(f"[DEPLOYER SELL] {token} | {_dep_reason}")
                    _notify(
                        f"🚨 {token} Deployer 出货！\n"
                        f"创建者地址 {_dep_addr[:16]}...\n"
                        f"卖出 ${usd_value:,.0f} → 全清"
                    )
                    await execute_exit(token_addr, token, {
                        "action": "full_sell", "sell_pct": 1.0,
                        "reason": _dep_reason,
                        "exit_reason": "liquidity_crash",
                    })
                    return
            except Exception as _de:
                _log(f"[DEPLOYER CHECK ERR] {token} {_de}")

        # ── 非 KOL 鲸鱼卖出（双重门槛：绝对金额 + MC占比）────────────────────────────────
        if not is_kol and token_addr and pm.position_exists(token_addr):
            _pos_ws = pm.get_position(token_addr)
            _open_strategy_ws = _pos_ws.get("open_strategy", "") if _pos_ws else ""
            _whale_sell_mc = _pos_ws.get("entry_mc", 0) if _pos_ws else 0  # 用入场MC估算
            _ws_level = get_whale_sell_level(usd_value, _whale_sell_mc)

            if _ws_level is None:
                # 不满足鲸鱼卖出门槛，忽略
                return

            _log(f"[WHALE SELL] {token} {_ws_level.upper()}级鲸鱼卖出 ${usd_value:,.0f}（MC~${_whale_sell_mc/1_000_000:.2f}M）")

            # ★ 鲸鱼跟单离场
            asyncio.create_task(_whale_follow_sell(token_addr, token, trader, usd_value))

            # ── S 级共识模式：硬止损检测优先（hard/soft 均进入）────────────────────────
            if pm.is_s_consensus(token_addr):
                if _ws_level == 'hard':
                    # hard 级：直接硬止损全清（不走 score 路径）
                    _log(f"[S-HARD STOP] {token} 鲸鱼硬止损级别卖出 ${usd_value:,.0f}")
                    await execute_exit(token_addr, token, {
                        "action": "full_sell", "sell_pct": 1.0,
                        "reason": f"S级鲸鱼硬止损卖出 ${usd_value:,.0f}",
                        "exit_reason": "kol_sell",
                    })
                    return
                # soft/warn：走 score 扣分路径
                _s_state_w  = pm._load()
                _ks_list_w  = _s_state_w.get("kol_sells", {}).get(token_addr, [])
                s_hard_w = pm.s_consensus_hard_stop(
                    token_addr, f"whale:{trader[:8]}", usd_value, _ks_list_w)
                if s_hard_w:
                    s_hard_w["exit_reason"] = "kol_sell"
                    _log(f"[S-HARD STOP] {token} S级鲸鱼共振止损 ${usd_value:,.0f}")
                    await execute_exit(token_addr, token, s_hard_w)
                    return
                from config import S_SCORE_VOL_DROP_50
                _score_delta = -S_SCORE_VOL_DROP_50 if _ws_level == 'soft' else -5
                new_sc_w, s_act_w = pm.s_consensus_apply_score(
                    token_addr, _score_delta,
                    reason=f"鲸鱼{_ws_level}级卖出 ${usd_value:,.0f}")
                if s_act_w:
                    s_act_w.setdefault("exit_reason", "take_profit")
                    _notify(f"⚠️ {token} [S级] 鲸鱼{_ws_level}级卖出 ${usd_value:,.0f}\n"
                            f"signal_score → {new_sc_w}\n决策: {s_act_w['reason']}")
                    await execute_exit(token_addr, token, s_act_w)
                else:
                    _notify(f"⚠️ {token} [S级] 鲸鱼{_ws_level}级卖出 ${usd_value:,.0f}，score={new_sc_w}，持仓不动")
                    _log(f"[WHALE SELL] {token} S级 score={new_sc_w}，持仓不动")
                return

            # ── 策略W ────────────────────────────────────────────────────────────────────
            if _open_strategy_ws == "W":
                if _ws_level in ('hard', 'soft'):
                    _pos_ws["whale_reversed"] = True
                    pm._save_pos(token_addr, _pos_ws)
                    _log(f"[WHALE SELL W] {token} {_ws_level}级鲸鱼卖出 ${usd_value:,.0f} → 全清")
                    _notify(
                        f"🐳 {token} [策略W] 鲸鱼反向卖出（{_ws_level}级）\n"
                        f"金额: ${usd_value:,.0f}\n触发全清"
                    )
                    await execute_exit(token_addr, token, {
                        "action": "full_sell", "sell_pct": 1.0,
                        "reason": f"策略W鲸鱼{_ws_level}级卖出 ${usd_value:,.0f}",
                        "exit_reason": "kol_sell",
                    })
                elif _ws_level == 'warn':
                    _notify(f"⚠️ {token} [策略W] 鲸鱼预警卖出 ${usd_value:,.0f}（不操作，观察中）")
                    _log(f"[WHALE SELL W] {token} warn级 ${usd_value:,.0f}，策略W观察")
                return

            # ── 策略A：所有级别鲸鱼卖出只记录预警，不操作 ───────────────────────────────
            elif _open_strategy_ws == "A":
                _notify(
                    f"⚠️ {token} [策略A] {_ws_level}级鲸鱼卖出 ${usd_value:,.0f}\n"
                    f"（不操作，等待触发者KOL信号）"
                )
                _log(f"[WHALE SELL A] {token} {_ws_level}级鲸鱼卖出 ${usd_value:,.0f}，策略A不响应")
                return

            # ── 策略B：按鲸鱼级别分级响应 ───────────────────────────────────────────────
            elif _open_strategy_ws == "B":
                if _ws_level == 'hard':
                    _log(f"[WHALE SELL B-HARD] {token} 鲸鱼硬止损级别 ${usd_value:,.0f} → 全清")
                    await execute_exit(token_addr, token, {
                        "action": "full_sell", "sell_pct": 1.0,
                        "reason": f"策略B鲸鱼硬止损卖出 ${usd_value:,.0f}",
                        "exit_reason": "hard_stop",
                    })
                elif _ws_level == 'soft':
                    new_score_ws, action_ws = update_signal_on_whale(token_addr, is_buy=False)
                    if action_ws:
                        action_ws.setdefault("exit_reason", "stop_loss")
                        _log(f"[WHALE SELL B] {token} signal_score={new_score_ws} → {action_ws['action']}")
                        await execute_exit(token_addr, token, action_ws)
                    else:
                        _log(f"[WHALE SELL B] {token} score={new_score_ws}，信号仍强，不操作")
                elif _ws_level == 'warn':
                    _new_ws_b = pm.add_signal_score(token_addr, -5,
                                                     reason=f"鲸鱼warn级卖出 ${usd_value:,.0f}")
                    _notify(f"⚠️ {token} [策略B] 鲸鱼预警卖出 ${usd_value:,.0f}，signal_score-5→{_new_ws_b}")
                    _log(f"[WHALE SELL B] {token} warn级，signal_score-5→{_new_ws_b}")
                return

            # ── 未知策略兜底 ────────────────────────────────────────────────────────────
            if _ws_level == 'hard':
                await execute_exit(token_addr, token, {
                    "action": "full_sell", "sell_pct": 1.0,
                    "reason": f"鲸鱼硬止损卖出 ${usd_value:,.0f}",
                    "exit_reason": "hard_stop",
                })
            elif _ws_level == 'soft':
                new_score_ws, action_ws = update_signal_on_whale(token_addr, is_buy=False)
                if action_ws:
                    action_ws.setdefault("exit_reason", "stop_loss")
                    await execute_exit(token_addr, token, action_ws)
        return

    # ── 买入信号 ──────────────────────────
    if alert_type != "buy":
        return

    if not token_addr:
        return

    # ─ 策略A：评分制 KOL 跟单 ──────────────────────────────
    _, fomo_map_live, _, handle_map_live = await _get_kol_maps()
    # 优先用 fomo_map（有 fomo_handle 的 KOL），其次用 handle_map（所有 KOL）
    kol_info = fomo_map_live.get(trader.lower()) or handle_map_live.get(trader.lower())
    is_kol_buy = kol_info is not None

    # ── WS RPC 预备队列命中检查 ──────────────────────────────────────
    # 如果这个 token 之前被 WS RPC 捕捉到并放入预备队列，
    # 现在 KOL 信号到来就是「确认信号」，打印一条日志做记录
    rpc_pending = _RPC_PENDING.pop(token_addr, None)
    _is_rpc_confirmed = bool(rpc_pending)  # True = WS RPC 提前捕捉，KOL 为确认信号
    if rpc_pending:
        age_s = time.time() - rpc_pending["ts"]
        _log(f"[WS-RPC] ✅ 预备队列命中！{token} 在 {age_s:.0f}s 前被链上捕捉，"
             f"KOL @{trader} 确认，entry_source=rpc")

    if is_kol_buy:
        grade = kol_info["grade"]
        score = kol_info["score"]
        # 仓位积分制（信号在此时还没经过过滤层，暂不知 boost/clawby，
        # 先用基础仓位；boost/clawby 在 _try_open_position_inner 里再做）
        # high_winrate 可从 kol_info 中读取（scorer 写入了 win_rate 字段）
        _win_rate = float(kol_info.get("win_rate", 0) or 0)
        size_kol  = ex.calc_position_size(grade, {
            "high_winrate": _win_rate >= 70,
        })

        if grade in SINGLE_TRIGGER_GRADES:
            # S/A 级：单人直接触发
            # ★ S/A KOL 信号合并：通过缓冲层推送，同token 2min内多位KOL合并为一条
            _kol_mc = fetch_market_cap(token_addr)
            asyncio.create_task(_kol_merge_add_and_flush(
                token_addr, token, trader, grade, score, usd_value, _kol_mc))
            # ★ 软黑名单解锁：S/A级KOL买入计入S级信号（每次+1，达3次解锁）
            _s_unlocked = pm.record_reentry_signal(token_addr, "S")
            if _s_unlocked:
                _log(f"[SOFT-UNLOCK] {token} S/A级KOL @{trader} 触发S级信号解锁，允许重入")
            # ★ 在进入 try_open_position 之前先抢占 _PENDING_BUY（防并发信号竞争）
            _pb_ts = _PENDING_BUY.get(token_addr, 0)
            if _pb_ts and time.time() - _pb_ts < _PENDING_BUY_ACTIVE_TTL:
                _log(f"[DEDUP-EARLY] {token} [{grade}级] 已在买入队列，丢弃并发信号")
            else:
                _PENDING_BUY[token_addr] = time.time()
                # ★ 快照当前价格，用于入场偏差检查
                _snap_price = ex._get_token_price(token_addr)
                _log(f"[SIGNAL-A] [{grade}级 {score}分] @{trader} 买入 {token} → {size_kol:.0f}U "
                     f"(快照价=${_snap_price:.8f})" if _snap_price else 
                     f"[SIGNAL-A] [{grade}级 {score}分] @{trader} 买入 {token} → {size_kol:.0f}U")
                _src_sa = "rpc" if _is_rpc_confirmed else "fomo"
                try:
                    await _try_open_position_inner(
                        token_addr=token_addr, token_symbol=token,
                        strategy="kol", size_usd=size_kol, trigger_kol=trader,
                        snapshot_price=_snap_price,
                        entry_source=_src_sa,
                    )
                finally:
                    _PENDING_BUY[token_addr] = time.time()  # 标记完成时间，用于 24h 去重
        else:
            # B/C 级：计入等待队列，≥2 人确认才触发
            # ★ 软黑名单解锁：A/B级KOL买入（按handle去重，5min内3人解锁）
            if grade in ("A", "B"):
                _ab_unlocked = pm.record_reentry_signal(token_addr, "AB", grade=grade, handle=trader)
                if _ab_unlocked:
                    _log(f"[SOFT-UNLOCK] {token} A/B级KOL @{trader} 触发AB解锁，允许重入")
            _kol_confirm_add(token_addr, token, trader, grade, score, size_kol)

    # ── 策略B：多人共买 ─────────────────────────────────────
    triggered = record_multi_buy(token_addr, trader, usd_value)

    # ── FOMO 共买 >7 人：额外推送通知（独立于买入决策）──
    _multi_count_now = len(_MULTI_BUY_WINDOW.get(token_addr, []))
    if _multi_count_now >= 7 and not is_kol_buy:
        _fomo_dedup_key = ("fomo7", token_addr)
        _fomo_dedup_last = _WHALE_SIGNAL_DEDUP.get(_fomo_dedup_key, 0)
        if time.time() - _fomo_dedup_last > 300:  # 5分钟内不重复推
            _WHALE_SIGNAL_DEDUP[_fomo_dedup_key] = time.time()
            _multi_traders = [e["trader"] for e in _MULTI_BUY_WINDOW[token_addr]]
            _multi_traders_str = ", ".join(f"@{t}" for t in _multi_traders[:10])
            if len(_multi_traders) > 10:
                _multi_traders_str += f" 等{len(_multi_traders)}人"
            _multi_mc = fetch_market_cap(token_addr)
            _log(f"[FOMO MULTI] {token} | {_multi_count_now}人共买！{_multi_traders_str}")
            asyncio.create_task(_signal_notify(token, token_addr, [
                f"🔥 FOMO多人共买信号 ({_multi_count_now}人/{MULTI_BUY_WINDOW_MIN}min)",
                f"👤 {_multi_traders_str}",
                f"→ {_multi_count_now}人同时买入，FOMO情绪强烈",
            ], mc=_multi_mc, source="fomo"))

    if triggered and not is_kol_buy:
        count = len(_MULTI_BUY_WINDOW.get(token_addr, []))
        _log(f"[SIGNAL-B] 多人共买 {token} ({count}人/{MULTI_BUY_WINDOW_MIN}min)")
        window = _MULTI_BUY_WINDOW[token_addr]
        trigger_count = sum(1 for e in window if e.get("triggered"))
        if trigger_count == 0:
            for e in window:
                e["triggered"] = True
            # ★ 推送多人共买信号到钉钉
            _multi_traders_b = [e["trader"] for e in window]
            _multi_traders_str_b = ", ".join(f"@{t}" for t in _multi_traders_b[:10])
            if len(_multi_traders_b) > 10:
                _multi_traders_str_b += f" 等{len(_multi_traders_b)}人"
            asyncio.create_task(_signal_notify(token, token_addr, [
                f"👥 多人共买信号 ({count}人/{MULTI_BUY_WINDOW_MIN}min)",
                f"👤 {_multi_traders_str_b}",
                f"→ {count}人同时买入，形成早期共识",
            ], mc=fetch_market_cap(token_addr), source="multi"))
            # ★ 同样抢占 _PENDING_BUY
            _pb_ts_b = _PENDING_BUY.get(token_addr, 0)
            if _pb_ts_b and time.time() - _pb_ts_b < _PENDING_BUY_ACTIVE_TTL:
                _log(f"[DEDUP-EARLY] {token} [multi] 已在买入队列，丢弃并发信号")
            else:
                # ★ 分档仓位 + 信号等级
                # S级：>=14人共振，使用顶级跟单离场逻辑
                # 常规：5-13人，使用趋势共振离场逻辑
                count_now = len(window)
                is_s_grade = count_now >= SIGNAL_GRADE_S2_THR
                _size_b = SIZE_SIGNAL_S if is_s_grade else SIZE_MULTI
                _log(f"[SIGNAL-B] {token} {count_now}人共买 → {'S级强信号' if is_s_grade else '常规'} ${_size_b}U")
                # ★ 软黑名单解锁：策略B达到S2级（≥14人）算一次S级信号
                if is_s_grade:
                    _b_unlocked = pm.record_reentry_signal(token_addr, "S")
                    if _b_unlocked:
                        _log(f"[SOFT-UNLOCK] {token} 策略B S级({count_now}人)触发解锁，允许重入")
                _PENDING_BUY[token_addr] = time.time()
                _snap_price_b = ex._get_token_price(token_addr)
                try:
                    await _try_open_position_inner(
                        token_addr=token_addr, token_symbol=token,
                        strategy="multi", size_usd=_size_b,
                        snapshot_price=_snap_price_b,
                        extra_fields={"signal_grade": "S"} if is_s_grade else {},
                    )
                finally:
                    _PENDING_BUY[token_addr] = time.time()

    # S/A 级 KOL 买 且 多人也达到阈值：叠加仓位
    # ★ 修复：1) 必须经过 _PENDING_BUY 去重；2) 必须带价格快照，否则跳过（防绕过 DCA 价格检查）
    if is_kol_buy and triggered and kol_info["grade"] in SINGLE_TRIGGER_GRADES:
        count = len(_MULTI_BUY_WINDOW.get(token_addr, []))
        _pb_ts_ab = _PENDING_BUY.get(token_addr, 0)
        if _pb_ts_ab and time.time() - _pb_ts_ab < _PENDING_BUY_ACTIVE_TTL:
            _log(f"[DEDUP-AB] {token} SIGNAL-AB 已在队列，丢弃")
        else:
            _snap_price_ab = ex._get_token_price(token_addr)
            if not _snap_price_ab:
                _log(f"[SKIP-AB] {token} 无法获取快照价格，跳过叠仓（防 DCA 门槛绕过）")
            else:
                _log(f"[SIGNAL-AB] {kol_info['grade']}级KOL + 多人共买 {token} ({count}人) → 叠仓 快照价=${_snap_price_ab:.8f}")
                _kol_ab_mc = fetch_market_cap(token_addr)
                asyncio.create_task(_signal_notify(token, token_addr, [
                    f"🔥 KOL共振买入信号",
                    f"👤 {kol_info['grade']}级 @{trader} + {count}人共买",
                    f"→ KOL与多人共买共振，信号叠加",
                    f"📊 MC: {('$' + format(_kol_ab_mc/1_000_000, '.2f') + 'M') if _kol_ab_mc >= 1_000_000 else ('$' + format(_kol_ab_mc/1000, '.0f') + 'K') if _kol_ab_mc > 0 else '新币'}",
                    f"💵 快照价: ${_snap_price_ab:.6g}",
                    f"→ 通过所有过滤后推送信号卡片",
                ], mc=_kol_ab_mc, source="kol", kol_grade=kol_info['grade']))
                _PENDING_BUY[token_addr] = time.time()
                try:
                    await _try_open_position_inner(
                        token_addr=token_addr, token_symbol=token,
                        strategy="multi", size_usd=SIZE_MULTI, trigger_kol=trader,
                        snapshot_price=_snap_price_ab,
                    )
                finally:
                    _PENDING_BUY[token_addr] = time.time()

    # ── 策略C/W：鲸鱼大额买入（非 KOL 名单但单笔大额）────────────
    # 先获取当前 MC（用于 is_whale 双重门槛判断）
    _whale_buy_mc = fetch_market_cap(token_addr)
    _whale_grade  = get_whale_grade(usd_value, _whale_buy_mc) if not is_kol_buy else None

    if _whale_grade is not None:
        # ★ 鲸鱼跟单模式：MC < $1M 时自动跟单 $10
        asyncio.create_task(_whale_follow_buy(
            token_addr, token, trader, usd_value, _whale_buy_mc))

        # 信号卡片去重：60s 内同一 (trader, token) 只推一条
        _dedup_key = (trader.lower(), token_addr)
        _dedup_now = time.time()
        _dedup_last = _WHALE_SIGNAL_DEDUP.get(_dedup_key, 0)
        if _dedup_now - _dedup_last < _WHALE_SIGNAL_DEDUP_TTL:
            _log(f"[WHALE DEDUP] {token} @{trader} {_whale_grade}级 ${usd_value:,.0f} 在 {_WHALE_SIGNAL_DEDUP_TTL}s 内重复，跳过")
        else:
            _WHALE_SIGNAL_DEDUP[_dedup_key] = _dedup_now
            _log(f"[WHALE SIGNAL] {token} {_whale_grade}级鲸鱼 ${usd_value:,.0f} 直接推送")
            asyncio.create_task(_push_whale_signal(
                token_addr, token, trader, usd_value, _whale_buy_mc, _whale_grade))

    # ── 持仓期间信号追踪：KOL 买入 / 鲸鱼买入 → 更新 signal_score ──
    if pm.position_exists(token_addr):
        # ── KOL 持仓追加买入 → 提升 signal_score ──
        if is_kol_buy:
            kol_addr_live = kol_info.get("evm", trader)
            kol_gr_live   = kol_info.get("grade", "C")
            kol_sc_live   = int(kol_info.get("score", 0))

            # S 级共识模式：走专属加分路径
            if pm.is_s_consensus(token_addr):
                from config import (
                    S_SCORE_KOL_BUY_A, S_SCORE_KOL_BUY_B, S_SCORE_KOL_BUY_C)
                _delta_map = {"S": S_SCORE_KOL_BUY_A, "A": S_SCORE_KOL_BUY_A,
                               "B": S_SCORE_KOL_BUY_B, "C": S_SCORE_KOL_BUY_C}
                _delta_buy = _delta_map.get(kol_gr_live, S_SCORE_KOL_BUY_C)
                new_sc_buy, _ = pm.s_consensus_apply_score(
                    token_addr, _delta_buy,
                    reason=f"KOL @{trader}({kol_gr_live}级)买入")
                _log(f"[S-SCORE+] {token} KOL @{trader}({kol_gr_live}级)持仓期买入 signal_score={new_sc_buy}")
                # 共识模式下将 KOL 应记入 consensus_signals
                pm.record_consensus_signal(token_addr, kol_addr_live, usd_value)
            else:
                new_score_buy = update_signal_on_kol_buy(
                    token_addr, kol_addr_live, kol_gr_live, kol_sc_live)
                _log(f"[SIGNAL+] {token} KOL @{trader}({kol_gr_live}级)持仓期追加买入 signal_score={new_score_buy}")

                # ── 策略W 信号确认：只尝试升级共识模式，不补仓 ──────────
                _pos_w = pm.get_position(token_addr)
                if _pos_w and new_score_buy >= WHALE_SIGNAL_CONFIRM_SCORE:
                    _ep = _pos_w.get("entry_price", 0)
                    _lp = ex._get_token_price(token_addr) or 0
                    _pnl_w = (_lp - _ep) / _ep if _ep > 0 and _lp > 0 else 0
                    # 只记录共识信号，由巡检在盈利时升级为共识模式（不补仓）
                    pm.record_consensus_signal(token_addr, kol_addr_live, usd_value)
                    _log(f"[SIGNAL-CONFIRM] {token} 策略W signal_score={new_score_buy}"
                         f" pnl={_pnl_w:.1%}，累积共识信号（不补仓）")

        # ── 鲸鱼大额买入（满足动态鲸鱼门槛）→ 按等级提升 signal_score ──
        elif not is_kol_buy and is_whale(usd_value, fetch_market_cap(token_addr)):
            _w_grade_live  = get_whale_grade(usd_value, fetch_market_cap(token_addr))
            _w_score_delta = {"A": 15, "B": 10, "C": 5}.get(_w_grade_live or "C", 5)
            if pm.is_s_consensus(token_addr):
                from config import S_SCORE_WHALE_BUY
                _s_delta = max(S_SCORE_WHALE_BUY, _w_score_delta)  # 取较大值
                new_sc_wb, _ = pm.s_consensus_apply_score(
                    token_addr, _s_delta,
                    reason=f"鲸鱼{_w_grade_live}级 ${usd_value:,.0f} 买入")
                pm.record_consensus_signal(token_addr, f"whale:{trader[:8]}", usd_value)
                _log(f"[S-SCORE+] {token} 鲸鱼{_w_grade_live}级 ${usd_value:,.0f} 持仓期买入 signal_score={new_sc_wb}")
            else:
                _new_sc = pm.add_signal_score(token_addr, _w_score_delta,
                                               reason=f"鲸鱼{_w_grade_live}级 ${usd_value:,.0f} 持仓期买入")
                _log(f"[SIGNAL+] {token} 鲸鱼{_w_grade_live}级 ${usd_value:,.0f} 持仓期买入 signal_score+{_w_score_delta}→{_new_sc}")

        # ── 场外强手（非白名单）持仓期买入 → 小幅加分 ────────────────────
        # 非白名单、非已计入鲸鱼/KOL路径的买入，金额 >= $5K 给 signal_score +3
        elif not is_kol_buy and usd_value >= OUTSIDER_BUY_SCORE_THRESHOLD                 and alert_type == "buy":
            _s, _ = pm._load_signal_score_only(token_addr)
            _ns = pm.add_signal_score(token_addr, OUTSIDER_BUY_SCORE_DELTA,
                                       reason=f"场外强手 @{trader} ${usd_value:,.0f} 买入")
            _log(f"[OUTSIDER+] {token} @{trader} ${usd_value:,.0f} 买入 signal_score+{OUTSIDER_BUY_SCORE_DELTA}→{_ns}")

    # ── 场外强手喊单 → 软黑名单解锁贡献（不论是否持仓，无金额门槛）──────────
    # 场外强手 = 不在 fomo_watchlist 的 handle（没有链上钱包，无法追踪金额）
    # 条件：不在白名单、token 在软黑名单中
    # 门槛：5min 内 >= 4 个不同场外 handle 喊单买入（无金额要求）
    if alert_type == "buy" and not is_kol_buy \
            and token_addr and pm.is_soft_reentry_blacklisted(token_addr):
        _out_unlocked = pm.record_reentry_signal(
            token_addr, "AB",
            grade="C",
            handle=f"outsider:{trader.lower()}")
        # 独立计数（不与白名单 AB 混计）
        _soft_rec = pm._SOFT_REENTRY_BLACKLIST.get(token_addr.lower(), {})
        _now_out = __import__('time').time()
        _cutoff_out = _now_out - pm.SOFT_UNLOCK_WINDOW
        _outsider_kols = [
            (t, g, h) for t, g, h in _soft_rec.get("ab_kols", [])
            if t >= _cutoff_out and h.startswith("outsider:")
        ]
        _unique_out = len({h for _, _, h in _outsider_kols})
        _log(f"[OUTSIDER-UNLOCK] {token} @{trader} 场外强手喊单贡献"
             f" {_unique_out}/{OUTSIDER_UNLOCK_COUNT} 人")
        if _unique_out >= OUTSIDER_UNLOCK_COUNT:
            pm._SOFT_REENTRY_BLACKLIST.pop(token_addr.lower(), None)
            _log(f"[SOFT-UNLOCK] {token} {_unique_out}个场外强手喊单解锁成功！")
            _notify(f"🔓 {token} 场外强手 {_unique_out} 人喊单买入，软黑名单解锁，允许重入")

async def try_open_position(
    token_addr: str,
    token_symbol: str,
    strategy: str,
    size_usd: float,
    trigger_kol: str = "",
):
    """检查仓位上限，执行买入，记录持仓"""

    # ★ 信号去重：同一 token 已在买入队列中 → 直接丢弃
    # 防止仓位满了但信号还在 replay，导致 ZZZ 那种 16+ 次重复尝试
    now = time.time()
    _pb_ts_outer = _PENDING_BUY.get(token_addr, 0)
    if _pb_ts_outer and now - _pb_ts_outer < _PENDING_BUY_ACTIVE_TTL:
        _log(f"[DEDUP] {token_symbol} 已在买入队列中，丢弃重复信号")
        return
    _PENDING_BUY[token_addr] = now
    try:
        await _try_open_position_inner(token_addr, token_symbol, strategy, size_usd, trigger_kol)
    finally:
        _PENDING_BUY[token_addr] = time.time()  # 完成时更新时间戳，用于 24h 去重


async def _try_open_position_inner(
    token_addr: str,
    token_symbol: str,
    strategy: str,
    size_usd: float,
    trigger_kol: str = "",
    snapshot_price: float | None = None,
    entry_source: str = "fomo",
    extra_fields: dict | None = None,  # 额外字段，如 signal_grade="S"
):
    """内部实现：检查仓位上限，执行买入，记录持仓"""

    # ── Gas 高峰暂停检查（仅影响新开仓，不影响 DCA）──
    if not pm.position_exists(token_addr) and _is_gas_paused(for_sell=False):
        remaining = int(_GAS_PAUSE_UNTIL - time.time())
        _log(f'[GAS PAUSE] {token_symbol} Gas高峰暂停中，{remaining}s 后恢复买入，跳过')
        return

    # ── 全局并发限制：同时最多 MAX_CONCURRENT_BUYS 笔买入 ────
    global _LAST_BUY_TS
    if _BUY_SEMAPHORE is None:
        _log(f'[SKIP] {token_symbol} Semaphore 未初始化，跳过')
        return
    # 冷却期检查（全局，非单 token）
    elapsed = time.time() - _LAST_BUY_TS
    if elapsed < GLOBAL_COOLDOWN_SEC and not pm.position_exists(token_addr):
        _log(f'[COOLDOWN] {token_symbol} 全局冷却中（距上笔 {elapsed:.0f}s < {GLOBAL_COOLDOWN_SEC}s），跳过')
        return
    # 获取并发 Semaphore（非阻塞：拿不到直接跳过，不排队等待）
    if not _BUY_SEMAPHORE._value and not pm.position_exists(token_addr):
        _log(f'[CONCURRENT] {token_symbol} 已有 {MAX_CONCURRENT_BUYS} 笔买入进行中，跳过')
        return


    # ★ 黑名单检查：永久屏蔽反复亏损的 token
    bl_reason = TOKEN_BLACKLIST.get(token_symbol.upper())
    if bl_reason:
        _log(f"[BLACKLIST] {token_symbol} 在黑名单中，跳过 | 原因: {bl_reason}")
        return

    # ★ 冷却期检查：30min 内刚卖出过此 token，不重新建仓
    if not pm.position_exists(token_addr) and pm.is_in_cooldown(token_addr):
        _log(f"[SKIP] {token_symbol} 冷却期中（分级冷却），跳过建仓")
        return

    # ★ 硬黑名单：止损/硬止损/流动性崩溃离场后当日不可重入
    if not pm.position_exists(token_addr) and pm.is_hard_reentry_blacklisted(token_addr):
        _log(f"[SKIP] {token_symbol} 硬黑名单（当日止损离场），禁止重入")
        return

    # ★ 软黑名单：止盈/KOL卖等离场后需强信号解锁才能重入
    # 解锁信号在 handle_alert 买入路径中通过 record_reentry_signal 累积
    # 此处检查：若仍在软黑名单中（未解锁），拦截建仓
    if not pm.position_exists(token_addr) and pm.is_soft_reentry_blacklisted(token_addr):
        _log(f"[SKIP] {token_symbol} 软黑名单（当日已离场），等待强信号解锁")
        return

    # ★ 每日亏损黑名单：当天已亏损超过 DAILY_LOSS_BLACKLIST_USD 就不再开仓
    if not pm.position_exists(token_addr) and pm.is_daily_blacklisted(token_addr):
        _log(f"[SKIP] {token_symbol} 当日亏损黑名单，当日不再开仓")
        return

    # ★ 全局日亏损熔断：当日总亏损超过 DAILY_LOSS_LIMIT_USD 时停止所有新开仓
    if not pm.position_exists(token_addr) and pm.is_global_circuit_broken():
        total_loss = pm.get_daily_total_loss()
        _log(f"[CIRCUIT BREAKER] 当日总亏损 ${total_loss:.2f} >= ${pm.DAILY_LOSS_LIMIT_USD}，熔断触发，停止新开仓")
        return

    # ★ 高位币当日锁定：同一 token 当日交易 >= HIGH_MC_DAILY_TRADE_MAX 次且净亏损 → 当日不再开仓
    if not pm.position_exists(token_addr) and is_high_mc_daily_locked(token_addr):
        _log(f"[HIGH-MC LOCK] {token_symbol} 当日高位币已锁定，不再买入")
        return

    # ── 防重复开仓：同一 token + 同一策略 → 直接跳过 ──────
    is_dca = False   # DCA 路径标记（保留字段，当前不走 DCA）
    if pm.position_exists(token_addr):
        pos = pm.get_position(token_addr)
        # ★ 已持仓：任何新的买入信号都不补仓，只通过信号追踪区（上方 859 行）累积加分
        # 加分逻辑在 handle_alert() 的「持仓期间信号追踪」区统一处理
        # 唯一例外：信号累积到共识升级门槛时，record_consensus_signal 会标记 consensus_pending，
        # 并在巡检时通过 try_activate_consensus() 升级为共识模式，不在此补仓
        _log(f"[SKIP] {token_symbol} 策略[{strategy}]已开过仓，跳过补仓（已持仓只加分）")
        return

    # ── 仓位数量上限 ──────────────────────────
    if not pm.can_open(size_usd) and not pm.position_exists(token_addr):
        _log(f"[SKIP] 大仓位已满({pm.MAX_POSITIONS})，丢弃信号 {token_symbol}")
        return

    # 获取市值
    mc = fetch_market_cap(token_addr)
    _log(f"[MC] {token_symbol} 当前市值 ${mc:,.0f}")

    # 高市值趋势过滤（仅策略B多人共买；KOL跟单不限制，KOL自己判断）
    if strategy == "multi" and mc >= HIGH_MC_FILTER_THRESHOLD:
        allow, reason = check_high_mc_trend(token_addr, mc)
        if not allow:
            _log(f"[FILTER] {token_symbol} 被高市值趋势过滤 | {reason}")
            return


    # ★ 策略W/S级鲸鱼跟单 MC 上限拦截：超过 WHALE_STRATEGY_MC_CAP 直接拒绝
    # 复盘(2026-09-06)：MEME@36.7M/UBIK@19.8M/SHROOM@34.4M 均被鲸鱼快进快出割韭菜
    if strategy == "whale" and mc >= WHALE_STRATEGY_MC_CAP:
        _log(f"[WHALE-MC-CAP] {token_symbol} 入场MC ${mc/1e6:.1f}M >= 上限${WHALE_STRATEGY_MC_CAP/1e6:.0f}M，拒绝策略W开仓")
        _interim_notify(f"⛔ {token_symbol} 策略W 被MC上限拦截（${mc/1e6:.1f}M >= {WHALE_STRATEGY_MC_CAP/1e6:.0f}M）")
        return
    # ── 高位冷静期（MC >= $5M）─────────────────────────────────────────────────────
    if mc >= HIGH_MC_POSITION_HALF:
        snap_price_calm = await asyncio.to_thread(ex._get_token_price, token_addr)
        _log(f"[HIGH-MC CALM] {token_symbol} MC ${mc/1e6:.1f}M >= $5M，冷静期 {HIGH_MC_CALM_SEC}s，"
             f"快照价 ${snap_price_calm:.8f}")
        _interim_notify(f"⏳ {token_symbol} 高位币（MC ${mc/1e6:.1f}M）进入{HIGH_MC_CALM_SEC}s冷静期")
        await asyncio.sleep(HIGH_MC_CALM_SEC)
        cur_price_calm = await asyncio.to_thread(ex._get_token_price, token_addr)
        if snap_price_calm and snap_price_calm > 0 and cur_price_calm and cur_price_calm > 0:
            price_change_calm = (cur_price_calm - snap_price_calm) / snap_price_calm
            if price_change_calm >= HIGH_MC_CALM_DRIFT_MAX:
                _log(f"[HIGH-MC CALM] {token_symbol} 冷静期内涨幅 {price_change_calm:.1%} "
                     f">= {HIGH_MC_CALM_DRIFT_MAX:.0%}，放弃买入")
                _interim_notify(f"⚠️ {token_symbol} 高位冷静期：涨幅过大 {price_change_calm:.1%}，放弃")
                return
            if price_change_calm <= HIGH_MC_CALM_DROP_MAX:
                _log(f"[HIGH-MC CALM] {token_symbol} 冷静期内跌幅 {price_change_calm:.1%} "
                     f"<= {HIGH_MC_CALM_DROP_MAX:.0%}，信号衰减，放弃买入")
                _interim_notify(f"⚠️ {token_symbol} 高位冷静期：信号衰减 {price_change_calm:.1%}，放弃")
                return
            _log(f"[HIGH-MC CALM] {token_symbol} 冷静期通过，价格变动 {price_change_calm:.1%}，继续买入")

    # ── 高位仓位减半（MC >= $5M，冷静期后执行）──────────────────────────────────────
    if mc >= HIGH_MC_POSITION_HALF:
        original_size = size_usd
        size_usd = round(size_usd * 0.5, 2)
        _log(f"[HIGH-MC HALF] {token_symbol} 高位仓位减半 ${original_size:.0f} → ${size_usd:.0f}"
             f"（MC ${mc/1e6:.1f}M）")

    # Gas 检查
    can_trade, gas_msg = check_gas_before_trade()
    if not can_trade:
        _log(f"[GAS BLOCK] {token_symbol} 交易被 gas 不足阻断: {gas_msg}")
        return

    # ETH 可用余额检查（扣除 GAS_RESERVE_USD 后的可用资金）
    from gas_manager import get_usdg_balance as get_available_usd
    available_usd = await asyncio.to_thread(get_available_usd)
    if available_usd < size_usd - 0.5:
        _log(f"[SKIP] {token_symbol} ETH 可用资金 ${available_usd:.2f} < ${size_usd}，跳过")
        return

    # ── 安全审计（蜜罐/税率/黑名单）──────────────────────────
    safe, sec_reason = await asyncio.to_thread(check_token_security, token_addr, token_symbol)
    if not safe:
        _log(f"[SECURITY BLOCK] {token_symbol} | {sec_reason}")
        return

    # ── 流动性检查（市值分层门槛 + 冲击比拒绝）──────────────────────
    # ── 流动性检查（市値分层门槛 + 充击比拒绝） ──
    liq_ok, liq_reason = await asyncio.to_thread(
        check_token_liquidity, token_addr, token_symbol,
        current_mc=mc, buy_size_usd=size_usd,
    )
    if not liq_ok:
        _log(f"[LIQUIDITY BLOCK] {token_symbol} | {liq_reason}")
        return

    # ── 新币过滤（上线 > 7 天跳过）───────────────────────
    age_ok, age_reason = await asyncio.to_thread(check_token_age, token_addr, token_symbol)
    if not age_ok:
        _log(f"[AGE BLOCK] {token_symbol} | {age_reason}")
        return

    # ── 防刷量检测（买卖地址去重比 / 单钱包占比 / 小额刷量）──────────
    wash_ok, wash_reason = await asyncio.to_thread(check_anti_wash, token_addr, token_symbol)
    if not wash_ok:
        _log(f'[WASH BLOCK] {token_symbol} | {wash_reason}')
        return
    _log(f'[WASH OK] {token_symbol} | {wash_reason}')

    # ── 安全扩展检查 (#3 单钱包集中度 / #5 资金到账 / #8 量比) ──────
    _safety_ext = await asyncio.to_thread(run_safety_extensions, token_addr, token_symbol)
    if _safety_ext['verdict'] == 'reject':
        _log(f"[SAFETY EXT BLOCK] {token_symbol} | {'; '.join(_safety_ext['rejects'])}")
        return
    if _safety_ext['warnings']:
        _log(f"[SAFETY EXT WARN] {token_symbol} | {'; '.join(_safety_ext['warnings'])}")

    # ── Deployer 黑名单检测（历史 Rug 创建者一票否决）────────────
    dep_ok, dep_reason = await asyncio.to_thread(check_deployer_history, token_addr, token_symbol)
    if not dep_ok:
        _log(f'[DEPLOYER BLOCK] {token_symbol} | {dep_reason}')
        return

    # ── DexScreener Boost + Clawby 交叉验证（积分制追加，DCA路径跳过）──
    boost_amt = get_boost_amount(token_addr)
    confirmed, buyer_cnt, sm_reason = await asyncio.to_thread(
        check_smart_money_confirmation, token_addr, token_symbol)
    _log(f"[CLAWBY SM] {token_symbol} | {sm_reason}")

    if not is_dca:
        # 仅新开仓和叠仓路径做信号积分追加
        # 读取 kol_info 中已计算的 win_rate（由 smart_money_scorer 写入）
        _kol_info_now = (await _get_kol_maps())[1].get(
            trigger_kol.lower() if trigger_kol else "", {})
        _win_rate_now = float(_kol_info_now.get("win_rate", 0) or 0)

        extra_signals = {
            "boost_strong":   boost_amt >= 100,
            "clawby_confirm": confirmed and buyer_cnt >= 2,
            "whale_align":    False,   # 鲸鱼同向由外层调用时传入（此处暂无感知）
            "high_winrate":   _win_rate_now >= 70,
        }
        # 仅当存在加分项时才重新计算（避免覆盖已由外层正确计算的 size_usd）
        if any(extra_signals.values()):
            # 通过当前 grade 重算（grade 由外层写入 strategy 字段，或默认 C）
            _cur_grade = _kol_info_now.get("grade", "C") if _kol_info_now else "C"
            size_usd = ex.calc_position_size(_cur_grade, extra_signals)
            _log(f"[POSITION] {token_symbol} 信号积分重算 → ${size_usd:.0f}")
        else:
            _log(f"[POSITION] {token_symbol} 无额外加分，保持 ${size_usd:.0f}")

        if boost_amt >= 10:
            _log(f'[BOOST] {token_symbol} Boost ${boost_amt:.0f}')
    else:
        if boost_amt >= 10:
            _log(f'[BOOST] {token_symbol} Boost ${boost_amt:.0f}（DCA路径，不参与积分）')

    # 其他过滤（流动性、模拟卖出等）
    passed, filter_reason = await asyncio.to_thread(run_filters, token_addr, token_symbol)
    if not passed:
        _log(f"[FILTER BLOCK] {token_symbol} | {filter_reason}")
        return

    # ★ 入场价偏差检查：对比信号快照价 vs 当前实时价
    if PRICE_DRIFT_ENABLED and snapshot_price and snapshot_price > 0:
        cur_price_now = ex._get_token_price(token_addr)
        if cur_price_now and cur_price_now > 0:
            drift = (cur_price_now - snapshot_price) / snapshot_price
            if abs(drift) > PRICE_DRIFT_MAX_PCT:
                direction = f"↑+{drift:.1%}" if drift > 0 else f"↓{drift:.1%}"
                _log(f"[PRICE DRIFT] {token_symbol} 入场价偏差 {direction}"
                     f" (信号快照=${snapshot_price:.8f} → 当前=${cur_price_now:.8f})，"
                     f"超过 {PRICE_DRIFT_MAX_PCT:.0%}，放弃跟单")
                return
            _log(f"[PRICE OK] {token_symbol} 入场价偏差 {drift:+.1%}，在允许范围内")

    # ★★ SIGNAL_ONLY 拦截：通过所有过滤后，改为推送信号卡片，不执行买入
    from config import SIGNAL_ONLY
    if SIGNAL_ONLY:
        from token_filter import get_token_dev_info, get_security_detail, get_liquidity_detail
        # 安全审计详情（已缓存）
        sec_detail = get_security_detail(token_addr)
        is_hp    = sec_detail.get("honeypot")
        buy_tax  = sec_detail.get("buy_tax")
        sell_tax = sec_detail.get("sell_tax")
        sim_slip = sec_detail.get("simulate_slippage")
        # 流动性（已缓存）
        liq_detail = get_liquidity_detail(token_addr)
        liq_usd  = liq_detail.get("liquidity_usd")
        # Deployer 信息
        dev      = get_token_dev_info(token_addr)
        dep_addr = dev.get("creator_address", "")
        dep_count= int(dev.get("twitter_create_token_count") or 0)
        # 当前价格
        try:
            cur_price = ex._get_token_price(token_addr)
        except Exception:
            cur_price = snapshot_price
        # 触发描述
        _ef = extra_fields or {}
        _grade = _ef.get("signal_grade", "") or _ef.get("whale_grade", "")
        _usd   = _ef.get("whale_usd", 0) or _ef.get("kol_usd", 0)
        if strategy in ("kol", "A") and trigger_kol:
            _usd_str = f" ${_usd:,.0f}" if _usd else ""
            trigger_desc = f"KOL @{trigger_kol}{_usd_str} 买入 (入场 MC ${mc/1e6:.2f}M)"
        elif strategy in ("whale", "W"):
            _usd_str = f"${_usd:,.0f}" if _usd else ""
            trigger_desc = f"鲸鱼 {_grade}级 {_usd_str} 买入 (入场 MC ${mc/1e6:.2f}M)"
        else:
            trigger_desc = f"多人共买 (入场 MC ${mc/1e6:.2f}M)"
        _notify_signal(
            symbol=token_symbol,
            token_addr=token_addr,
            strategy=entry_source,
            trigger_desc=trigger_desc,
            entry_mc=mc,
            size_usd=size_usd,
            sell_slippage=sim_slip,
            honeypot=is_hp,
            buy_tax=buy_tax,
            sell_tax=sell_tax,
            liquidity_usd=liq_usd,
            deployer_addr=dep_addr,
            deployer_token_count=dep_count,
            price_usd=cur_price,
        )
        _log(f"[SIGNAL_ONLY] {token_symbol} 信号已推送，跳过买入")
        return

    # 执行买入（用 Semaphore 限制并发）
    async with _BUY_SEMAPHORE:
        result = await asyncio.to_thread(
            ex.buy_token, token_addr, token_symbol, size_usd,
            -1.0, snapshot_price, entry_source,
        )
        if not result:
            _log(f"[BUY FAILED] {token_symbol}")
            _record_buy_failure(
                token_addr=token_addr, token_symbol=token_symbol,
                size_usd=size_usd, strategy=strategy,
                entry_mc=mc, trigger_kol=trigger_kol,
                fail_reason="buy_token returned None",
            )
            return

        # 买入成功，更新全局冷却时间戳
        _LAST_BUY_TS = time.time()

    # 获取成交价（从 baw 结果或余额推算）
    entry_price, _ = ex.get_token_price_and_mc(token_addr)

    # ── 影子跟踪：记录买入决策 ──
    # shadow_record 已停用
    # shadow_record(token_addr, token_symbol, 0, "buy", strategy,
    #               entry_price=entry_price, entry_mc=mc)

    # ── 策略 B：开仓时记录 DexScreener txns 快照（用于后续买卖比离场判断）──
    _entry_txns_snapshot: dict = {}
    if not is_dca:
        try:
            _snap_r = requests.get(
                f"https://api.dexscreener.com/latest/dex/tokens/{token_addr}",
                timeout=5,
                proxies={"https": "socks5://REDACTED_PROXY_CREDENTIALS"},
            )
            _snap_pairs = _snap_r.json().get("pairs", [])
            if _snap_pairs:
                _sp = sorted(_snap_pairs, key=lambda x: float((x.get("liquidity") or {}).get("usd", 0) or 0), reverse=True)[0]
                _txns = _sp.get("txns", {}) or {}
                _entry_txns_snapshot = {
                    "entry_txns_buys_1h":  int((_txns.get("h1") or {}).get("buys",  0) or 0),
                    "entry_txns_sells_1h": int((_txns.get("h1") or {}).get("sells", 0) or 0),
                    "entry_txns_buys_5m":  int((_txns.get("m5") or {}).get("buys",  0) or 0),
                    "entry_volume_1h":     float((_sp.get("volume") or {}).get("h1", 0) or 0),
                    "entry_buy_sell_ratio": round(
                        int((_txns.get("h1") or {}).get("buys", 0) or 0) /
                        max(int((_txns.get("h1") or {}).get("sells", 1) or 1), 1), 2
                    ),
                }
                _log(f"[SNAPSHOT-B] {token_symbol} txns快照: buys1h={_entry_txns_snapshot['entry_txns_buys_1h']}"
                     f" sells1h={_entry_txns_snapshot['entry_txns_sells_1h']}"
                     f" ratio={_entry_txns_snapshot['entry_buy_sell_ratio']:.2f}")
        except Exception as _e:
            _log(f"[SNAPSHOT-B] {token_symbol} 快照失败（不阻断开仓）: {_e}")

    # 构建开仓上下文（传入 open_position）
    _open_strat  = ("A" if strategy == "kol"   else
                    "W" if strategy == "whale"  else "B")
    _kol_map_now = (await _get_kol_maps())[1]
    _kol_entry   = _kol_map_now.get(trigger_kol.lower(), {}) if trigger_kol else {}
    _trigger_kols = []
    if _open_strat == "A" and _kol_entry:
        _trigger_kols = [{
            "address": _kol_entry.get("evm", trigger_kol),
            "grade":   _kol_entry.get("grade", "C"),
            "score":   int(_kol_entry.get("score", 0)),
        }]

    pm.open_position(
        token_addr=token_addr,
        token_symbol=token_symbol,
        strategy=strategy,
        size_usd=size_usd,
        entry_price=entry_price,
        entry_mc=mc,
        trigger_kol=trigger_kol,
        open_strategy=_open_strat,
        trigger_kols=_trigger_kols,
        boost_at_entry=boost_amt if not is_dca else 0.0,
        clawby_confirmed=bool(confirmed and buyer_cnt >= 2) if not is_dca else False,
        entry_source=entry_source,
        extra_fields={**(extra_fields or {}), **_entry_txns_snapshot},
    )

    # ── 高位币当日进出计数 ────────────────────────────────────────────────────────
    record_high_mc_trade(token_addr, mc)

    # ── 启动 Deployer 实时 Transfer 监控 ─────────────────────────────────────────
    asyncio.create_task(start_deployer_monitor(token_addr, token_symbol))

    # ── 微盘免止损：开仓后异步查发币时间，决定是否启用免止损模式 ──────────
    _entry_mc_for_micro = mc  # 捕获当前值供 lambda 使用
    async def _check_micro_no_stop():
        try:
            from config import (MICRO_NO_STOP_MC_THRESHOLD,
                                MICRO_NO_STOP_MAX_AGE_DAYS,
                                MICRO_NO_STOP_BROKE_1M_THR)
            # 条件一：入场 MC < 500K
            if _entry_mc_for_micro <= 0 or _entry_mc_for_micro >= MICRO_NO_STOP_MC_THRESHOLD:
                return
            # 条件二：发币时间 ≤ 3 天
            age_sec = await asyncio.to_thread(
                __import__('executor').get_token_age_sec, token_addr)
            if age_sec is None:
                # 无发币时间数据，保守起见不豁免
                _log(f"[MICRO-STOP] {token_symbol} 无发币时间数据，不启用免止损")
                return
            age_days = age_sec / 86400
            if age_days > MICRO_NO_STOP_MAX_AGE_DAYS:
                _log(f"[MICRO-STOP] {token_symbol} 发币 {age_days:.1f}d > {MICRO_NO_STOP_MAX_AGE_DAYS}d，不豁免")
                return
            # 条件三：当前 MC < 1M（说明从未破过，后续 evaluate_exit 会持续更新 mc_peak 判断）
            # 这里只做初始检查，实际峰值由巡检滚动更新
            _log(f"[MICRO-STOP] {token_symbol} 入场MC=${_entry_mc_for_micro:,.0f} "
                 f"发币{age_days:.1f}d ≤ {MICRO_NO_STOP_MAX_AGE_DAYS}d → 启用免止损模式 🎲")
            s = pm._load()
            key = token_addr.lower()
            if key in s["positions"]:
                s["positions"][key]["micro_no_stop"] = True
                pm._save(s)
        except Exception as e:
            _log(f"[MICRO-STOP CHECK ERR] {token_symbol} {e}")
    asyncio.create_task(_check_micro_no_stop())

    # 推送买入通知
    multi_count = len(_MULTI_BUY_WINDOW.get(token_addr, []))
    _notify_buy(
        symbol=token_symbol,
        token_addr=token_addr,
        strategy=strategy,
        size_usd=size_usd,
        entry_mc=mc,
        trigger_kol=trigger_kol,
        multi_count=multi_count,
    )

    # 交易后补充 gas
    await asyncio.to_thread(refill_eth_if_needed)


_EXITING_TOKENS: set = set()  # 正在卖出的 token，防并发重复触发

async def execute_exit(token_addr: str, token_symbol: str, action: dict):
    """根据 action 执行卖出——强制先写日志再执行，任何异常不封禁"""
    if token_addr in _EXITING_TOKENS:
        _log(f"[EXIT SKIP] {token_symbol} 已在执行卖出，跳过重复触发")
        return
    _EXITING_TOKENS.add(token_addr)
    try:
        await _do_execute_exit(token_addr, token_symbol, action)
    finally:
        _EXITING_TOKENS.discard(token_addr)

async def _fallback_sell(token_addr: str, token_symbol: str, action: dict):
    """
    卖出兜底机制：当正常卖出失败时，依次尝试更高滑点，最终全量清仓。
    所有尝试均建立在底层 sell_token 之上，不依赖主进程锁。
    """
    FALLBACK_SLIPPAGES = [0.30, 0.45, 0.60]   # 逐步渐升滑点
    FALLBACK_DELAY_SEC = 8                     # 每轮间隔

    bal = await asyncio.to_thread(ex.get_token_balance, token_addr)
    if bal <= 0:
        _log(f"[FALLBACK] {token_symbol} 链上余额=0，已无需卖出")
        _pos_fb = pm.get_position(token_addr)
        pm.close_position(token_addr, "底层确认余额为0")
        return

    _log(f"[FALLBACK] {token_symbol} 启动兜底卖出，余额={bal:.4f}，将尝试滑点 {FALLBACK_SLIPPAGES}")
    _notify(f"🚨 {token_symbol} 卖出失败，启动兜底重试\n"
            f"触发: {action['reason']}\n"
            f"将尝试滑点: {' / '.join(f'{int(s*100)}%' for s in FALLBACK_SLIPPAGES)}")

    for slippage in FALLBACK_SLIPPAGES:
        await asyncio.sleep(FALLBACK_DELAY_SEC)
        # 底层直接调用 sell_token，绕过主进程锁校验
        bal_now = await asyncio.to_thread(ex.get_token_balance, token_addr)
        if bal_now <= 0:
            _log(f"[FALLBACK] {token_symbol} 余额已清零，尝试成功")
            pm.close_position(token_addr, f"兜底卖出成功 slippage={slippage:.0%}")
            return

        _log(f"[FALLBACK] {token_symbol} 尝试 slippage={slippage:.0%}，qty={bal_now:.4f}")
        # 直接调用 sell_token，滑点已由我们管理，内部锁校验用 strict=False
        import executor as _ex_fb
        _ex_fb._verify_process_lock = lambda strict=False: True  # 兜底绕过锁
        result = await asyncio.to_thread(
            _ex_fb.sell_token,
            token_addr=token_addr,
            token_symbol=token_symbol,
            sell_pct=1.0,
            current_balance=bal_now,
            slippage=slippage,
        )
        if result and result.get("status") not in ("FAILED", None):
            eth_out  = float(result.get("toTokenActualQty", 0) or 0)
            tx       = result.get("txHash", "")
            pnl_usd  = None
            _pos_fb  = pm.get_position(token_addr)
            if _pos_fb:
                ep   = _pos_fb.get("entry_price", 0)
                lp   = await asyncio.to_thread(ex._get_token_price, token_addr) or 0
                cost = _pos_fb.get("size_usd", 0)
                eth_price = ex._get_eth_price()
                pnl_usd = eth_out * eth_price - cost if eth_price > 0 else None
            pm.close_position(token_addr, f"兜底卖出 slippage={slippage:.0%}", realized_pnl_usd=pnl_usd or 0.0)
            update_high_mc_pnl(token_addr, pnl_usd or 0.0, _pos_fb.get("entry_mc", 0) if _pos_fb else 0)
            _notify_close(
                token_addr=token_addr, symbol=token_symbol,
                exit_reason=f"兜底卖出（{action['reason']}） slippage={slippage:.0%}",
                eth_received=eth_out, gas_eth=0,
                pnl_usd=pnl_usd, tx=tx,
                pos_snapshot=_pos_fb,
            )
            _log(f"[FALLBACK] {token_symbol} 兜底卖出成功 slippage={slippage:.0%} ETH={eth_out:.5f}")
            return

    # 全部尝试均失败：向用户推送紧急通知
    _log(f"[FALLBACK GIVE UP] {token_symbol} 所有滑点均失败，需要手动干预")
    _notify(f"🚨🚨 {token_symbol} 自动卖出全部失败！\n"
            f"已尝试滑点: 30% / 45% / 60% 全部失败\n"
            f"请立即手动在钱包卸载 {token_symbol}\n"
            f"合约: {token_addr[:12]}…")


async def _do_execute_exit(token_addr: str, token_symbol: str, action: dict):
    """实际执行卖出逻辑 — 等链上确认后才关仓/推送"""
    _log(f"[EXECUTE EXIT] {token_symbol} | {action['action']} {action['sell_pct']:.0%} | {action['reason']}")
    # ── Gas 高峰暂停检查（止损类卖出强制执行，不受 Gas 暂停影响）──
    _exit_reason_check = action.get("exit_reason", "")
    _is_stop_loss_exit = _exit_reason_check in ("stop_loss", "hard_stop", "liquidity_crash", "kol_sell")
    if _is_gas_paused(for_sell=True) and not _is_stop_loss_exit:
        remaining = int(_GAS_PAUSE_UNTIL - time.time())
        _log(f"[GAS PAUSE] {token_symbol} Gas高峰暂停中，非止损卖出延迟 {remaining}s，持仓继续运行")
        return  # 由下次巡检自然重新触发
    elif _is_gas_paused(for_sell=True) and _is_stop_loss_exit:
        _log(f"[GAS PAUSE BYPASS] {token_symbol} 止损/KOL卖出，Gas 暂停中但强制执行")


    balance = await asyncio.to_thread(ex.get_token_balance, token_addr)
    if balance <= 0:
        _log(f"[EXIT SKIP] {token_symbol} 链上余额为0")
        pm.close_position(token_addr, "balance=0")
        return

    _log(f"[SELL START] {token_symbol} | 余额={balance:.4f} | 卖出{action['sell_pct']:.0%} | 触达: {action['reason']}")
    # ★ 卖出开始前锁定仓位，防止并发信号在出本期间触发加仓
    pm.lock_position(token_addr)
    result = await asyncio.to_thread(
        ex.sell_token,
        token_addr, token_symbol,
        action["sell_pct"], balance,
    )
    # ★ 卖出完成（不管成功失败）都解锁
    pm.unlock_position(token_addr)

    if result and result.get("status") == "FINISHED":
        # ── 链上确认成功 ──
        tx        = result.get("txHash", "")
        eth_out   = float(result.get("toTokenActualQty") or 0)
        # gas 从 wallet tx-history 无法实时获取，用固定估算（约 0.00025 ETH in RH）
        gas_eth   = 0.00025

        # 计算实际 PnL（卖出ETH价值 - 买入成本）
        eth_price = ex._get_eth_price()
        eth_usd   = eth_out * eth_price if eth_price > 0 else 0
        pos       = pm.get_position(token_addr)
        cost_usd  = pos.get("size_usd", 0) * action["sell_pct"] if pos else 0
        pnl_usd   = eth_usd - cost_usd if cost_usd > 0 else None

        _log(f"[SELL CONFIRMED] {token_symbol} ✅ tx={tx[:20]} ETH={eth_out:.6f} PnL=${pnl_usd:+.2f}" if pnl_usd is not None else f"[SELL CONFIRMED] {token_symbol} ✅ tx={tx[:20]} ETH={eth_out:.6f}")

        _exit_reason = action.get("exit_reason", "stop_loss")

        # 在 close_position 前先拍快照（关仓后数据就丢失了）
        _pos_snapshot = pm.get_position(token_addr)
        _snap_entry_mc = _pos_snapshot.get("entry_mc", 0) if _pos_snapshot else 0

        if action["action"] == "full_sell":
            pm.close_position(token_addr, action["reason"], realized_pnl_usd=pnl_usd or 0.0)
            pm.record_exit(token_addr, _exit_reason)
            _RPC_PENDING.pop(token_addr.lower(), None)
            # 高位币盈亏更新
            update_high_mc_pnl(token_addr, pnl_usd or 0.0, _snap_entry_mc)
        else:
            # 部分卖出：更新每日单币亏损累计（不写重入黑名单，仍持仓中）
            if pnl_usd is not None and pnl_usd < 0:
                pm.record_partial_loss(token_addr, pnl_usd)
            # 高位币部分盈亏也追踪更新
            update_high_mc_pnl(token_addr, pnl_usd or 0.0, _snap_entry_mc)
            # ★ partial_sell 不调用 record_exit，避免误写黑名单
            # record_exit 只在 full_sell 时调用（仓位完全关闭）

        _notify_sell(
            symbol=token_symbol,
            action=action["action"],
            sell_pct=action["sell_pct"],
            reason=action["reason"],
            eth_received=eth_out,
            gas_eth=gas_eth,
            pnl_usd=pnl_usd,
            tx=tx,
            triggered_reason=action["reason"],
            token_addr=token_addr,
            pos_snapshot=_pos_snapshot,
        )
        await asyncio.to_thread(refill_eth_if_needed)

    elif result and result.get("status") == "FAILED":
        # ── 链上失败：executor 已做过多次滑点重试，这里不再重复，直接通知手动处理 ──
        tx = result.get("txHash", "?")
        _log(f"[SELL CHAIN FAILED] {token_symbol} 链上失败 tx={tx[:20]}，已放弃自动重试，请手动处理")
        pm.unlock_position(token_addr)
        _notify(
            f"🚨 {token_symbol} 卖出链上失败，已放弃自动重试\n"
            f"原因: {action['reason']}\n"
            f"tx: {tx[:20]}...\n"
            f"请手动在钱包清仓\n合约: {token_addr[:16]}..."
        )

    else:
        # ── 提交失败（API 层）：不再自动兜底重试，直接通知 ──
        _log(f"[SELL FAILED] {token_symbol} API层失败，已放弃自动重试，请手动处理")
        pm.unlock_position(token_addr)
        _notify(
            f"🚨 {token_symbol} 卖出API失败，已放弃自动重试\n"
            f"原因: {action['reason']}\n"
            f"请手动在钱包清仓\n合约: {token_addr[:16]}..."
        )


# ──────────────────────────────────────────────
# 持仓巡检（每 30 秒一次）
# ──────────────────────────────────────────────

# ── 成交量断崖检测 ───────────────────────────────────────────────────
# 1h vol 相比上个 1h 下降 > 70%，且持仓超过 15 分钟 → 主动减仓 50%
_VOL_CLIFF_THRESH = 0.70   # 成交量下降超过此比例才触发
_VOL_CLIFF_MIN_AGE_MIN = 15  # 持仓至少 15 分钟才检测（避免刚入场就触发）
_VOL_CLIFF_CACHE: dict = {}  # token_addr → (vol_h1_prev, ts)


def _check_dca_preconditions(token_addr: str, token_symbol: str,
                              entry_mc: float, dca_count: int) -> tuple[bool, str]:
    """
    DCA 三个前置拦截检查，任一满足即跳过补仓：
    1. 1h 价格变化 < DCA_SKIP_1H_DROP（默认 -5%）→ 仍在下跌
    2. 当前 1h 成交量 < 上个 1h 成交量 × DCA_SKIP_VOL_RATIO（默认 50%）→ 成交量萎缩
    3. 低位币（entry_mc < 100k）且已补仓 ≥ 1 次 且 6h 跌幅 < DCA_SKIP_LOW_MC_6H（默认 -30%）
    返回 (skip: bool, reason: str)
    """
    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{token_addr}",
            timeout=6,
            proxies={"https": "socks5://REDACTED_PROXY_CREDENTIALS"},
        )
        pairs = r.json().get("pairs", [])
        if not pairs:
            return False, ""  # 无数据，不拦截
        p = sorted(pairs,
                   key=lambda x: float((x.get("liquidity") or {}).get("usd", 0) or 0),
                   reverse=True)[0]

        chg_1h  = float(p.get("priceChange", {}).get("h1",  0) or 0)
        chg_6h  = float(p.get("priceChange", {}).get("h6",  0) or 0)
        vol     = p.get("volume", {}) or {}
        vol_h1  = float(vol.get("h1", 0) or 0)
        vol_h6  = float(vol.get("h6", 0) or 0)
        # 上个 1h 成交量估算 = (h6 - h1) / 5
        vol_prev_h1 = (vol_h6 - vol_h1) / 5 if vol_h6 > vol_h1 else 0

        # 检查一：1h 仍在下跌
        if chg_1h < DCA_SKIP_1H_DROP:
            return True, f"DCA 跳过：1h 仍在下跌 {chg_1h:.1f}%"

        # 检查二：成交量萎缩超 50%
        if vol_prev_h1 > 0 and vol_h1 < vol_prev_h1 * DCA_SKIP_VOL_RATIO:
            return True, (f"DCA 跳过：成交量萎缩超 50%，无人接盘"
                          f"（当前1h ${vol_h1:,.0f} vs 上1h ${vol_prev_h1:,.0f}）")

        # 检查三：低位币 6h 跌幅过大（仅已补过 ≥1 次时）
        if entry_mc > 0 and entry_mc < 100_000 and dca_count >= 1:
            if chg_6h < DCA_SKIP_LOW_MC_6H:
                return True, f"DCA 跳过：低位币 6h 跌幅过大（{chg_6h:.1f}%）"

    except Exception as e:
        _log(f"[DCA PRECHECK ERROR] {token_symbol} {e}，不拦截")

    return False, ""


def _check_volume_cliff(token_addr: str, token_symbol: str,
                        pos: dict, current_price: float) -> dict | None:
    """
    检测成交量断崖：DexScreener 当前 1h vol 与上个 1h vol 对比。
    下降 > 70% 且持仓超 15min → 返回 full_sell action（热度消退，出货窗口关闭）。
    """
    now = time.time()
    entry_ts = pos.get("opened_at", pos.get("entry_ts", 0))   # 包容旧字段名
    age_min  = (now - entry_ts) / 60 if entry_ts else 0
    if age_min < _VOL_CLIFF_MIN_AGE_MIN:
        return None   # 持仓太短，不检测

    # 检测频率限制：每 5 分钟查一次（避免频繁调 API）
    cache = _VOL_CLIFF_CACHE.get(token_addr.lower())
    if cache and now - cache[1] < 300:
        return None

    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{token_addr}",
            timeout=6,
            proxies={"https": "socks5://REDACTED_PROXY_CREDENTIALS"},
        )
        pairs = r.json().get("pairs", [])
        if not pairs:
            return None
        p = sorted(pairs, key=lambda x: float((x.get("liquidity") or {}).get("usd", 0) or 0), reverse=True)[0]

        vol = p.get("volume", {}) or {}
        vol_h1  = float(vol.get("h1",  0) or 0)
        vol_h6  = float(vol.get("h6",  0) or 0)
        # 上个 1h 估算 = (h6 - h1) / 5（近似）
        vol_prev_h1 = (vol_h6 - vol_h1) / 5 if vol_h6 > vol_h1 else 0

        _VOL_CLIFF_CACHE[token_addr.lower()] = (vol_h1, now)

        if vol_prev_h1 > 0 and vol_h1 >= 0:
            drop = (vol_prev_h1 - vol_h1) / vol_prev_h1
            if drop >= _VOL_CLIFF_THRESH:
                # ── 盈利状态保护：盈利时只减仓 40%，不全清（保留底仓等反弹）──
                # 亏损时才全清（热度已散，及时止损）
                entry_p = pos.get("entry_price", 0)
                cur_pnl = (current_price - entry_p) / entry_p if entry_p > 0 and current_price > 0 else 0
                if cur_pnl >= 0.05:
                    # 盈利 > 5%：减仓 40% 并等待下一次巡检再决定
                    reason = (f"成交量断崖(盈利保护): 1h vol ${vol_h1:,.0f} vs 上1h ${vol_prev_h1:,.0f}"
                              f" 下降 {drop:.0%}，盈利{cur_pnl:.1%}，减仓40%留底仓")
                    _log(f"[VOL CLIFF] {token_symbol} | {reason}")
                    return {"action": "partial_sell", "sell_pct": 0.40, "reason": reason,
                            "exit_reason": "vol_cliff"}
                else:
                    # 亏损/平仓：全清
                    reason = (f"成交量断崖: 1h vol ${vol_h1:,.0f} vs 上1h ${vol_prev_h1:,.0f}"
                              f" 下降 {drop:.0%} > {_VOL_CLIFF_THRESH:.0%}，热度消退")
                    _log(f"[VOL CLIFF] {token_symbol} | {reason}")
                    return {"action": "full_sell", "sell_pct": 1.0, "reason": reason,
                            "exit_reason": "take_profit"}

    except Exception as e:
        _log(f"[VOL CLIFF ERROR] {token_symbol} {e}")

    return None


async def _check_manual_targets(positions: list, price_map: dict):
    """
    轮询 manual_targets.json，匹配到目标条件时自动执行卖出。
    支持 trigger_type: mc_gte (市値 >= 目标) / price_gte (价格 >= 目标)
    """
    targets_file = os.path.join(os.path.dirname(__file__), "manual_targets.json")
    try:
        with open(targets_file) as f:
            targets = json.load(f)
    except Exception:
        return
    if not targets:
        return

    fired = []
    for addr, tgt in targets.items():
        addr_l = addr.lower()
        sym    = tgt.get("symbol", addr_l[:8])
        if not pm.position_exists(addr_l):
            fired.append(addr)   # 仓位已关，自动清除触发器
            continue

        ttype  = tgt.get("trigger_type", "price_gte")
        target_price = float(tgt.get("target_price", 0))
        target_mc    = float(tgt.get("target_mc", 0))

        # 从已有巡检价格缓存取
        cur_price = price_map.get(addr_l, {}).get("price", 0)
        cur_mc    = price_map.get(addr_l, {}).get("mc", 0)

        triggered = False
        if ttype == "mc_gte" and target_mc > 0 and cur_mc >= target_mc:
            triggered = True
            _log(f"[MANUAL TARGET] {sym} 市値 ${cur_mc:,.0f} >= 目标 ${target_mc:,.0f} → 触发")
        elif ttype == "price_gte" and target_price > 0 and cur_price >= target_price:
            triggered = True
            _log(f"[MANUAL TARGET] {sym} 价格 ${cur_price:.8f} >= 目标 ${target_price:.8f} → 触发")

        if triggered:
            action = {
                "action":   tgt.get("action", "full_sell"),
                "sell_pct": 1.0,
                "reason":   tgt.get("reason", f"手动目标触发"),
                "exit_reason": "take_profit",
            }
            await execute_exit(addr_l, sym, action)
            fired.append(addr)

    # 清除已触发/已关仓的目标
    if fired:
        for addr in fired:
            targets.pop(addr, None)
        with open(targets_file, "w") as f:
            json.dump(targets, f, indent=2, ensure_ascii=False)


async def position_checker():
    """定期检查持仓是否触发止盈/止损，并补全 entry_price/mc=0 的持仓"""
    _err_rounds      = 0   # 连续全错误巡检轮数
    _ERR_ALERT_THR   = 5   # 连续全错 N 轮后推送报警
    _alerted         = False
    while True:
        try:
            # 用 wait_for + shield 确保 sleep 不被事件循环延迟吞掉
            await asyncio.sleep(30)
            _log(f"[CHECK] 开始巡检，当前持仓数={len(pm.get_all_positions())}")
            positions = pm.get_all_positions()
            if not positions:
                _err_rounds = 0  # 无持仓，不计错误
                continue
            _round_ok = 0   # 本轮巡检成功的持仓数

            # ── manual_targets 价格缓存（巡检中顺带收集）────────
            _price_map: dict = {}

            for pos in positions:
                token_addr   = pos["token_addr"]
                token_symbol = pos.get("token_symbol") or pos.get("symbol") or "UNKNOWN"
                try:
                    # ① 优先用 DexScreener 查实时价格+市值（不依赖 baw balance）
                    price, mc = 0.0, 0.0
                    r = await asyncio.to_thread(
                        lambda a=token_addr: __import__('requests').get(
                            f"https://api.dexscreener.com/latest/dex/tokens/{a}",
                            timeout=6,
                            proxies={"https": "socks5://REDACTED_PROXY_CREDENTIALS"},
                        )
                    )
                    pairs = r.json().get("pairs", [])
                    liq_usd = 0.0
                    if pairs:
                        # ★ 修复(2026-09-07)：多池套利导致单池MC失真
                        # 原因：SNOWBALL有30个pair，流动性瞬时最高的pool价格可能严重偏离主池
                        # 修法：① 只取robinhood链 ② 过滤流动性<500U的碎片池
                        #       ③ 取流动性加权的中位MC，而非单取第一名
                        _rh_pairs = [
                            _pp for _pp in pairs
                            if _pp.get("chainId", "") == "robinhood"
                            and float((_pp.get("liquidity") or {}).get("usd", 0) or 0) >= 500
                            and float(_pp.get("marketCap", _pp.get("fdv", 0)) or 0) > 0
                        ]
                        if not _rh_pairs:
                            # 降级：robinhood链全部pair，不管流动性
                            _rh_pairs = [_pp for _pp in pairs if _pp.get("chainId", "") == "robinhood"] or pairs
                        _rh_pairs_s = sorted(_rh_pairs, key=lambda x: x.get("liquidity",{}).get("usd",0), reverse=True)
                        # 取流动性最高的pair作为价格基准
                        p = _rh_pairs_s[0]
                        price   = float(p.get("priceUsd", 0) or 0)
                        liq_usd = float((p.get("liquidity") or {}).get("usd", 0) or 0)
                        # MC取前3大流动性池的加权中位数（防单池失真）
                        _top3 = _rh_pairs_s[:3]
                        _mc_vals  = [float(_pp.get("marketCap", _pp.get("fdv", 0)) or 0) for _pp in _top3]
                        _liq_vals = [float((_pp.get("liquidity") or {}).get("usd", 0) or 0) for _pp in _top3]
                        _total_liq = sum(_liq_vals)
                        if _total_liq > 0 and all(v > 0 for v in _mc_vals):
                            mc = sum(_mc_vals[i] * _liq_vals[i] for i in range(len(_mc_vals))) / _total_liq
                        else:
                            mc = float(p.get("marketCap", p.get("fdv", 0)) or 0)
                        mc = round(mc)
                        if mc > 0:
                            import market_utils as _mu
                            _mu._MC_CACHE[token_addr.lower()] = (mc, time.time())
                    # 收集到 price_map 供 manual_targets 使用
                    _price_map[token_addr.lower()] = {"price": price, "mc": mc}

                    if price <= 0:
                        continue

                    # ② 补全 entry_price=0
                    if pos["entry_price"] <= 0 and price > 0:
                        _log(f"[FILL] {token_symbol} 补全 entry_price=${price:.8f}")
                        s = pm._load()
                        s["positions"][token_addr]["entry_price"] = price
                        pm._save(s)
                        continue

                    # ③ 补全 entry_mc=0
                    if pos["entry_mc"] <= 0 and mc > 0:
                        _log(f"[FILL] {token_symbol} 补全 entry_mc=${mc:,.0f}")
                        s = pm._load()
                        s["positions"][token_addr]["entry_mc"] = mc
                        pm._save(s)
                        pos = pm.get_position(token_addr)

                    # ④ ZeroDivision 保护（position_manager 也有，双重保险）
                    if pos["entry_price"] <= 0 or pos["entry_mc"] <= 0:
                        continue

                    # ★ 尝试激活共识模式（有实时价格才能判断 PnL）
                    if not pm.is_consensus_mode(token_addr):
                        entry_p = pos.get('entry_price', 0)
                        if entry_p > 0 and price > 0:
                            cur_pnl = (price - entry_p) / entry_p
                            if pm.try_activate_consensus(token_addr, cur_pnl):
                                _log(f"[CONSENSUS ON] {pos.get('token_symbol','?')} PnL={cur_pnl:.1%} 共识模式激活")

                    action = pm.evaluate_exit_checked(token_addr, price, mc)

                    # ★ 持仓超时强制清仓（12h，任何策略均执行）
                    if not action:
                        hold_h = (time.time() - pos.get("opened_at", time.time())) / 3600
                        if hold_h >= POSITION_MAX_HOLD_H:
                            timeout_reason = (f"持仓超时 {hold_h:.1f}h >= {POSITION_MAX_HOLD_H}h，"
                                              f"强制清仓 | pnl={( price - pos.get('entry_price',price)) / pos.get('entry_price', price):.1%}")
                            _log(f"[HOLD TIMEOUT] {token_symbol} | {timeout_reason}")
                            action = {"action": "full_sell", "sell_pct": 1.0,
                                      "reason": timeout_reason,
                                      "exit_reason": "hard_stop"}

                    # ★ 成交量断崖检测：1h vol 相比上个 1h 下降 > 70% 且持仓 > 15min
                    if not action:
                        action = _check_volume_cliff(token_addr, token_symbol, pos, price)

                    # ★ 策略 B 买卖比离场检测（需要有入场快照）
                    if not action and pos.get("open_strategy") == "B":
                        try:
                            _b_r = requests.get(
                                f"https://api.dexscreener.com/latest/dex/tokens/{token_addr}",
                                timeout=5,
                                proxies={"https": "socks5://REDACTED_PROXY_CREDENTIALS"},
                            )
                            _b_pairs = _b_r.json().get("pairs", [])
                            if _b_pairs:
                                _bp = sorted(_b_pairs,
                                             key=lambda x: float((x.get("liquidity") or {}).get("usd", 0) or 0),
                                             reverse=True)[0]
                                _bx = _bp.get("txns", {}) or {}
                                _buys_1h    = int((_bx.get("h1") or {}).get("buys",  0) or 0)
                                _sells_1h   = int((_bx.get("h1") or {}).get("sells", 0) or 0)
                                _buys_5m    = int((_bx.get("m5") or {}).get("buys",  0) or 0)
                                _sells_5m   = int((_bx.get("m5") or {}).get("sells", 0) or 0)
                                # 入场快照基准
                                _e_buys_1h  = pos.get("entry_txns_buys_1h",  0)
                                _e_sells_1h = pos.get("entry_txns_sells_1h", 0)
                                _e_buys_5m  = pos.get("entry_txns_buys_5m",  0)

                                # B-P1: 5min 卖出笔数 > 买入笔数 × 2（连续确认防抖）
                                _b_sell_spike = _buys_5m > 0 and _sells_5m > _buys_5m * 2
                                _b_sell_prev  = getattr(position_checker, "_b_sell_spike_prev", {})
                                _b_sell_prev_key = token_addr.lower()
                                if _b_sell_spike and _b_sell_prev.get(_b_sell_prev_key):
                                    # 连续 2 轮触发才全清
                                    _log(f"[STRAT-B P1] {token_symbol} 连续2轮 5min卖出({_sells_5m}) > 买入({_buys_5m})×2 → 全清")
                                    action = {"action": "full_sell", "sell_pct": 1.0,
                                              "reason": f"策略B: 5min卖出笔数({_sells_5m}) > 买入({_buys_5m})×2，市场出货",
                                              "exit_reason": "vol_cliff"}
                                _b_sell_prev[_b_sell_prev_key] = _b_sell_spike
                                position_checker._b_sell_spike_prev = _b_sell_prev

                                if not action and _e_buys_1h > 0:
                                    # B-P2: 当前 1h 买入笔数 < 入场快照 × 30% → 全清
                                    if _buys_1h < _e_buys_1h * 0.30:
                                        _log(f"[STRAT-B P2] {token_symbol} 1h买入({_buys_1h}) < 入场({_e_buys_1h})×30% → 全清")
                                        action = {"action": "full_sell", "sell_pct": 1.0,
                                                  "reason": f"策略B: FOMO情绪退潮 买入({_buys_1h}) < 入场({_e_buys_1h})×30%",
                                                  "exit_reason": "vol_cliff"}

                        except Exception as _be:
                            _log(f"[STRAT-B CHECK ERROR] {token_symbol} {_be}")

                    # ★ 流动性崩溃检测：持仓期间流动性相对入场时下降 > 30%
                    if not action and liq_usd > 0:
                        entry_liq = pos.get("entry_liquidity_usd", 0)
                        if entry_liq <= 0 and liq_usd > 0:
                            # 首次记录入场流动性
                            _s = pm._load()
                            _s["positions"][token_addr]["entry_liquidity_usd"] = liq_usd
                            pm._save(_s)
                        elif entry_liq > 0:
                            liq_drop = (entry_liq - liq_usd) / entry_liq
                            if liq_drop >= 0.30:
                                age_min_liq = (time.time() - pos.get("opened_at", time.time())) / 60
                                if age_min_liq >= 5:   # 至少持仓 5 分钟，防误判
                                    liq_reason = (f"流动性崩溃: ${liq_usd:,.0f} vs 入场 ${entry_liq:,.0f}"
                                                  f" 下降 {liq_drop:.0%} > 30%")
                                    _log(f"[LIQ CRASH] {token_symbol} | {liq_reason}")
                                    action = {"action": "full_sell", "sell_pct": 1.0,
                                              "reason": liq_reason,
                                              "exit_reason": "liquidity_crash"}

                    if action:
                        _log(f"[CHECK EXIT] {token_symbol} | {action['reason']}")
                        await execute_exit(token_addr, token_symbol, action)
                    _round_ok += 1

                except Exception as e:
                    # 单个持仓异常不影响其他持仓的巡检
                    _log(f"[CHECK ERROR] {token_symbol} 单仓异常: {e}")
                    continue

            # ── 连续全错误检测 ──────────────────────────────────────
            if _round_ok == 0:
                _err_rounds += 1
                if _err_rounds >= _ERR_ALERT_THR and not _alerted:
                    _alerted = True
                    _err_msg = (
                        "⚠️ 巡检连续 " + str(_err_rounds) + " 轮全部报错\n"
                        "可能原因：SOCKS 代理故障 / 网络中断\n"
                        "持仓数=" + str(len(pm.get_all_positions())) + "，止盈止损已暂停！\n"
                        "请检查: pip install requests[socks] PySocks"
                    )
                    _log(f"[CHECK ALERT] 连续 {_err_rounds} 轮全错误，推送报警")
                    _notify(_err_msg)
            else:
                _err_rounds = 0
                _alerted    = False

            # ── 巡检完成后检查 manual_targets ──
            await _check_manual_targets(positions, _price_map)

        except Exception as e:
            # 协程级别异常捕获：不让整个 position_checker 退出
            _log(f"[CHECK FATAL] 巡检协程异常，5s后恢复: {e}")
            await asyncio.sleep(5)


# ──────────────────────────────────────────────
# WebSocket 主循环
# ──────────────────────────────────────────────

# ── 双轨信号源 B：WS RPC 新 Pair 队列 ──────────────────────────────
# 由 pair_listener.run_pair_listener 写入，handle_new_pair_loop 消费
_PAIR_QUEUE: asyncio.Queue = asyncio.Queue()

# USDG / WETH 地址（用于识别哪个 token 是"新发行代币"）
_BASE_TOKENS = {
    "0x5fc5360d0400a0fd4f2af552add042d716f1d168",  # USDG
    "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",  # ETH native
    "0x4200000000000000000000000000000000000006",  # WETH (Arbitrum Orbit 标准)
}

# ── WS RPC 预备队列（方案一：KOL 信号确认后才执行买入）────────────
# {token_addr_lower: {"ver": "V2/V3", "pair": str, "ts": float}}
# 10 分钟内未收到 KOL 信号则丢弃
_RPC_PENDING: dict = {}
_RPC_PENDING_TTL = 600   # 10 分钟

# ── boost_poller 强制刷新事件标志 ────────────────────
# 新 Pair 到达时 set() 导致 boost_poller 立即刷新，不等 600s 计时
_BOOST_FORCE_REFRESH: asyncio.Event = asyncio.Event()


async def handle_new_pair_loop():
    """
    消费 _PAIR_QUEUE 里的新 Pair 事件（来自 WS RPC）。

    【方案一：KOL 确认模式】
    WS RPC 捕捉到新 pair → 放入 _RPC_PENDING 预备队列（10min TTL）
    → 等待 fomoapi KOL 信号命中同一 token → 此时才执行买入

    好处：零误买（没有 KOL 背书的新币不会直接买入，如 MGI 案例）
    代价：相比纯链上监听多了 KOL 确认延迟（秒级，fomoapi 仍是主路径）
    """
    if not WS_RPC_URL:
        _log("[WS-RPC] WS_RPC_URL 未配置，pair_queue 消费协程退出")
        return

    _log("[WS-RPC] pair_queue 消费协程启动（KOL确认模式，不直接买入）")
    while True:
        try:
            event = await asyncio.wait_for(_PAIR_QUEUE.get(), timeout=60)
        except asyncio.TimeoutError:
            # 顺便清理过期预备项
            now = time.time()
            expired = [k for k, v in list(_RPC_PENDING.items())
                       if now - v["ts"] > _RPC_PENDING_TTL]
            for k in expired:
                _RPC_PENDING.pop(k, None)
                _log(f"[WS-RPC] ⏱ 预备队列超时丢弃 {k[:12]}…（10min 内无 KOL 确认）")
            continue

        token0 = event.get("token0", "").lower()
        token1 = event.get("token1", "").lower()
        pair   = event.get("pair", "")
        ver    = event.get("version", "V2")

        # 识别新发行代币（排除 USDG/WETH 等基础代币）
        if token0 in _BASE_TOKENS:
            new_token = token1
        elif token1 in _BASE_TOKENS:
            new_token = token0
        else:
            new_token = token0

        if not new_token or new_token in _BASE_TOKENS:
            continue

        # 放入预备队列，等待 KOL 确认
        _RPC_PENDING[new_token] = {"ver": ver, "pair": pair, "ts": time.time()}
        _log(f"[WS-RPC] 🆕 {ver} 新 pair 进入预备队列 token={new_token[:12]}… "
             f"等待 10min 内 KOL 确认")
        # ★ 新 Pair 到达：立即触发 boost 强制刷新
        _BOOST_FORCE_REFRESH.set()


async def boost_poller():
    """
    每 600s 刷新一次 DexScreener token-boosts，热更新 _BOOST_SET。
    独立协程，不阻塞 WS 主循环。
    新 Pair 到达时 _BOOST_FORCE_REFRESH 事件会提前触发刷新。
    """
    _log("[BOOST POLLER] 启动，正常轮询 600s / 新Pair 强制刷新")
    POLL_INTERVAL = 600
    next_poll = time.time()
    while True:
        now = time.time()
        remaining = max(0.0, next_poll - now)
        # 等待：计时到期 或 新Pair 事件触发，先到先执行
        try:
            await asyncio.wait_for(
                asyncio.shield(asyncio.create_task(
                    _BOOST_FORCE_REFRESH.wait())),
                timeout=remaining,
            )
            triggered_by = "new_pair"
        except asyncio.TimeoutError:
            triggered_by = "timer"
        _BOOST_FORCE_REFRESH.clear()
        try:
            await asyncio.to_thread(refresh_boost_set)
            _log(f"[BOOST POLLER] 刷新完成 (触发源={triggered_by})")
        except Exception as e:
            _log(f"[BOOST POLLER ERROR] {e}")
        next_poll = time.time() + POLL_INTERVAL


async def _cleanup_pending_buy():
    """
    每 3600s 清理 _PENDING_BUY 中超过 24h 的历史去重记录。
    24h 内同一 token 不重复开仓；24h 后允许再次进入。
    """
    while True:
        await asyncio.sleep(600)
        now = time.time()
        expired = [addr for addr, ts in list(_PENDING_BUY.items())
                   if now - ts > _PENDING_BUY_DEDUP_TTL]
        for addr in expired:
            _PENDING_BUY.pop(addr, None)
        if expired:
            _log(f"[PENDING-BUY CLEANUP] 清理 {len(expired)} 个超期去重记录")


async def run_ws():
    uri = f"{FOMO_WS_URL}?key={FOMO_API_KEY}&chain=robinhood"
    _ws_proxy = "socks5://REDACTED_PROXY_CREDENTIALS"
    reconnect_delay = 5

    while True:
        try:
            _log(f"[WS] 连接 {FOMO_WS_URL} …")
            async with websockets.connect(uri, ping_interval=15, ping_timeout=120, close_timeout=10, open_timeout=30, proxy=_ws_proxy) as ws:
                reconnect_delay = 5
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                        if msg.get("type") == "welcome":
                            _log(f"[WS] 已连接，plan={msg.get('plan','?')}")
                            continue
                        if msg.get("type") in ("alert", "buy", "sell", "thesis"):
                            # 过滤 replay 历史消息
                            if msg.get("replay"):
                                continue
                            # event_id 去重（防 WS 重连 replay 重复处理）
                            eid = msg.get("eventId") or msg.get("id") or ""
                            if eid:
                                now = time.time()
                                # 清理过期记录
                                expired = [k for k, v in _SEEN_EVENTS.items() if now - v > _DEDUP_TTL]
                                for k in expired:
                                    del _SEEN_EVENTS[k]
                                if eid in _SEEN_EVENTS:
                                    continue  # 重复消息，跳过
                                _SEEN_EVENTS[eid] = now
                            # handle_alert 改为 Task，不阻塞 WS 消息接收和 position_checker
                            task = asyncio.create_task(handle_alert(msg))
                            task.add_done_callback(
                                lambda t: _log(f"[TASK ERROR] {t.exception()}") 
                                if not t.cancelled() and t.exception() else None
                            )
                    except json.JSONDecodeError:
                        pass
        except Exception as e:
            _log(f"[WS ERROR] {e}，{reconnect_delay}s 后重连…")
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, 60)


# ──────────────────────────────────────────────
# 工具
# ──────────────────────────────────────────────

def _log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    # 只写文件，不 print 到 stdout
    # （防止 supervisord/nohup 把 stdout 转发到同一 log 文件造成双写）
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ──────────────────────────────────────────────
# 影子跟踪定期回查
# ──────────────────────────────────────────────

async def shadow_checker():
    """影子跟踪器：启动后台循环，定期采样信号后价格变化。"""
    await shadow_loop_main()


# ──────────────────────────────────────────────
# 入口
# ──────────────────────────────────────────────

async def main():
    global KOL_MAP, FOMO_MAP, _KOL_MAP_CACHE, _KOL_MAP_TS, _BUY_SEMAPHORE
    _BUY_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_BUYS)
    # 拆分模块函数存在性检查（防止拆分后静默 import 失败）
    _smoke = [
        ("notify",       ["_notify","_notify_buy","_notify_close","_notify_partial_sell","_notify_sell","_record_buy_failure"]),
        ("market_utils", ["fetch_market_cap","check_high_mc_trend"]),
    ]
    import importlib
    for _mod, _funcs in _smoke:
        _m = importlib.import_module(_mod)
        for _fn in _funcs:
            if not callable(getattr(_m, _fn, None)):
                _log(f"[SMOKE FAIL] {_mod}.{_fn} 不可调用，请检查拆分模块")
                raise ImportError(f"{_mod}.{_fn} missing")
    _log("[SMOKE OK] notify + market_utils 函数检查通过")
    # 初始化拆分模块的 _log 注入
    init_notify(_log, _record_high_gas_event)
    init_market_utils(_log)
    KOL_MAP, FOMO_MAP, _EVM_INFO_MAP, _HANDLE_MAP = load_kol_map()
    _KOL_MAP_CACHE = (KOL_MAP, FOMO_MAP, _EVM_INFO_MAP, _HANDLE_MAP)   # 初始化缓存（4个值）
    _KOL_MAP_TS    = time.time()
    s_cnt = sum(1 for v in FOMO_MAP.values() if v["grade"] == "S")
    a_cnt = sum(1 for v in FOMO_MAP.values() if v["grade"] == "A")
    b_cnt = sum(1 for v in FOMO_MAP.values() if v["grade"] == "B")
    c_cnt = sum(1 for v in FOMO_MAP.values() if v["grade"] == "C")
    _log(f"[INIT] 加载 {len(KOL_MAP)} 个 KOL EVM 地址，{len(FOMO_MAP)} 个 FOMO handle")
    _log(f"[INIT] 评分分布 S={s_cnt} A={a_cnt} B={b_cnt} C={c_cnt} | 单人触发={s_cnt+a_cnt} 双人确认={b_cnt+c_cnt}")
    _log(f"[INIT] 最大仓位={pm.MAX_POSITIONS} | KOL={SIZE_KOL}U | 多人共买={SIZE_MULTI}U")

    # 双轨信号源：WS_RPC_URL 配置后启用零延迟新 Pair 监听
    coroutines = [
        run_ws(),
        position_checker(),
        boost_poller(),
        handle_new_pair_loop(),
        _cleanup_pending_buy(),
        shadow_checker(),               # 影子跟踪定期回查价格
    ]
    if WS_RPC_URL:
        coroutines.append(run_pair_listener(WS_RPC_URL, _PAIR_QUEUE))
        _log(f"[WS-RPC] 双轨信号源已启用: {WS_RPC_URL[:50]}...")
    else:
        _log("[WS-RPC] WS_RPC_URL 未配置，仅使用 fomoapi WS 信号源")
    results = await asyncio.gather(*coroutines, return_exceptions=True)
    for coro, result in zip(coroutines, results):
        if isinstance(result, Exception):
            _log(f"[FATAL] 协程崩溃: {coro} → {result}")


def _handle_sigterm(signum, frame):
    """SIGTERM/SIGINT 优雅退出：打日志后正常退出，不中断进行中的 sell"""
    import sys as _sys
    try:
        with open(LOG_FILE, "a") as _f:
            import time as _t
            _f.write(f"[{_t.strftime('%Y-%m-%d %H:%M:%S')}] [SHUTDOWN] 收到信号 {signum}，优雅退出\n")
    except Exception:
        pass
    _sys.exit(0)


if __name__ == "__main__":
    import signal
    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT,  _handle_sigterm)
    asyncio.run(main())
