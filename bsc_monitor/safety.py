# ============================================================
# safety.py — BSC 代币安全检测 + 多维数据富化
#
# 数据源（实测延迟）：
#   1. GoPlus api.gopluslabs.io (.io 域名，.com 有 SSL 问题)
#      → 蜜罐 / 买卖税 / 黑名单 / owner / LP 锁定 / holders，~0.8s
#   2. gmgn-cli token info + security
#      → top10 持仓率 / 狙击手 / entrapment / bot / creator 发币数，~1.2s
#
# 缓存 15 分钟（信号爆发期同一币会被反复查询）
# 失败优雅降级：安全数据拿不到 → unknown，不拦截推送（监控场景宁可不误杀）
# ============================================================
import json
import logging
import os
import subprocess
import threading
import time
from typing import Optional

import requests

from config import PROXY

log = logging.getLogger("bsc_safety")

_PROXIES = {"http": PROXY, "https": PROXY}
_GEMPLUS_URL = "https://api.gopluslabs.io/api/v1/token_security/56"

# 导入 Binance API（用于补充安全审计）
try:
    import binance_api
    _HAS_BINANCE = True
except ImportError:
    _HAS_BINANCE = False

# GMGN rug_ratio 查询用的 trending/trenches 全量缓存（每 15 分钟刷一次）
_RUG_LIST_TTL = 900
_rug_cache: dict = {"ts": 0, "map": {}}   # addr.lower() → (rug_ratio, age_hours)
_rug_lock = threading.Lock()

# token 安全缓存
_safety_cache: dict = {}   # addr.lower() → (ts, result)
_SAFETY_TTL = 900
_lock = threading.Lock()


# ─────────────────────────────────────────────────────────────
# GoPlus
# ─────────────────────────────────────────────────────────────
def _goplus(token: str) -> Optional[dict]:
    try:
        r = requests.get(
            f"{_GEMPLUS_URL}?contract_addresses={token}",
            proxies=_PROXIES, timeout=12)
        d = r.json().get("result", {}).get(token.lower(), {})
        if not d:
            return None
        return d
    except Exception as e:
        log.debug(f"[GoPlus] {token[:10]} err: {e}")
        return None


def _goplus_flags(d: dict) -> list:
    """GoPlus 字段 → 风险标签列表"""
    flags = []

    def _f(k):
        v = d.get(k)
        if v in (None, ""):
            return None
        try:
            return int(v)
        except (ValueError, TypeError):
            return None

    if _f("is_honeypot") == 1:
        flags.append("蜜罐")
    if _f("cannot_sell_all") == 1:
        flags.append("禁止卖出")
    if _f("is_blacklisted") == 1:
        flags.append("黑名单")
    if _f("is_mintable") == 1:
        flags.append("可增发")
    if _f("hidden_owner") == 1:
        flags.append("隐藏owner")
    if _f("is_proxy") == 1:
        flags.append("代理合约")
    if _f("owner_change_balance") == 1:
        flags.append("owner改余额")
    if _f("can_take_back_ownership") == 1:
        flags.append("可收回owner")
    if _f("is_anti_whale") == 1:
        flags.append("防鲸鱼")
    if _f("transfer_pausable") == 1:
        flags.append("可暂停转账")
    if _f("trading_cooldown") == 1:
        flags.append("交易冷却")
    if _f("is_open_source") == 0:
        flags.append("未开源")
    if _f("selfdestruct") == 1:
        flags.append("可自毁")

    # 税
    try:
        buy_tax = float(d.get("buy_tax") or 0)
        sell_tax = float(d.get("sell_tax") or 0)
        if buy_tax > 0.10 or sell_tax > 0.10:
            flags.append(f"税过高 {buy_tax*100:.0f}%/{sell_tax*100:.0f}%")
        elif buy_tax > 0.05 or sell_tax > 0.05:
            flags.append(f"税 {buy_tax*100:.0f}%/{sell_tax*100:.0f}%")
    except (ValueError, TypeError):
        pass

    # 蜜罐同 creator（历史发过蜜罐）
    if _f("honeypot_with_same_creator") == 1:
        flags.append("creator发过蜜罐")

    return flags


def _goplus_holders(d: dict) -> tuple:
    """返回 (top10_pct, top1_pct, lp_locked_pct)"""
    holders = d.get("holders") or []
    top1 = 0.0
    top10 = 0.0
    for h in holders[:10]:
        try:
            pct = float(h.get("percent", 0)) * 100
            top10 += pct
            if h is holders[0]:
                top1 = pct
        except (ValueError, TypeError):
            pass
    lp_locked = 0.0
    for l in (d.get("lp_holders") or []):
        try:
            if int(l.get("is_locked", 0)) == 1:
                lp_locked += float(l.get("percent", 0)) * 100
        except (ValueError, TypeError):
            pass
    return top10, top1, lp_locked


# ─────────────────────────────────────────────────────────────
# GMGN rug_ratio 列表缓存（trending+trenches 全量，15 分钟刷新）
# ─────────────────────────────────────────────────────────────
def _load_rug_lists():
    with _rug_lock:
        now = time.time()
        if now - _rug_cache["ts"] < _RUG_LIST_TTL:
            return
        m = {}
        try:
            r = subprocess.run(
                ["gmgn-cli", "market", "trending", "--chain", "bsc",
                 "--interval", "24h", "--limit", "100", "--raw"],
                capture_output=True, text=True, timeout=90)
            rank = (json.loads(r.stdout).get("data") or {}).get("rank") or []
            for row in rank:
                rr = row.get("rug_ratio")
                if rr is not None:
                    m[row["address"].lower()] = float(rr)
        except Exception as e:
            log.debug(f"[RUG] trending err: {e}")
        try:
            r = subprocess.run(
                ["gmgn-cli", "market", "trenches", "--chain", "bsc", "--raw"],
                capture_output=True, text=True, timeout=90)
            d = json.loads(r.stdout)
            for b in ("completed", "near_completion", "new_creation"):
                for row in (d.get(b) or []):
                    rr = row.get("rug_ratio")
                    if rr is not None:
                        m[row["address"].lower()] = float(rr)
        except Exception as e:
            log.debug(f"[RUG] trenches err: {e}")
        if m:
            _rug_cache["map"] = m
            _rug_cache["ts"] = now
            log.info(f"[RUG] rug_ratio 列表: {len(m)} 个币")


def rug_ratio(token: str) -> Optional[float]:
    """查 GMGN rug_ratio；不在列表返回 None（unknown，不等于 0）"""
    _load_rug_lists()
    return _rug_cache["map"].get(token.lower())


# ─────────────────────────────────────────────────────────────
# gmgn-cli token info/security（线程池外部调用，带缓存）
# ─────────────────────────────────────────────────────────────
def _gmgn(token: str) -> Optional[dict]:
    """合并 token info + security 关键字段"""
    try:
        r1 = subprocess.run(
            ["gmgn-cli", "token", "info", "--chain", "bsc", "--address", token, "--raw"],
            capture_output=True, text=True, timeout=30)
        info = json.loads(r1.stdout) if r1.stdout.strip() else {}
        r2 = subprocess.run(
            ["gmgn-cli", "token", "security", "--chain", "bsc", "--address", token, "--raw"],
            capture_output=True, text=True, timeout=30)
        sec = json.loads(r2.stdout) if r2.stdout.strip() else {}
        return {"info": info or {}, "sec": sec or {}}
    except Exception as e:
        log.debug(f"[GMGN] {token[:10]} err: {e}")
        return None


def _gmgn_flags(info: dict, sec: dict) -> list:
    flags = []
    if sec.get("is_honeypot") is True:
        flags.append("蜜罐(GMGN)")
    if sec.get("is_blacklist") is True:
        flags.append("黑名单(GMGN)")
    if sec.get("is_renounced") is False:
        flags.append("owner未放弃")
    if sec.get("is_open_source") is False:
        flags.append("未开源(GMGN)")
    ls = sec.get("lock_summary") or {}
    if isinstance(ls, dict) and ls.get("is_locked") is False:
        flags.append("LP未锁")
    return flags


# ─────────────────────────────────────────────────────────────
# 综合评分
# ─────────────────────────────────────────────────────────────
def score(gp: dict, gm: dict, rug: Optional[float]) -> tuple:
    """返回 (score 0-100, level_label, key_issues)
    纯规则评分，不依赖 gmgn-dev-score 的完整流程（那个太重）。
    硬伤直接 0：蜜罐 / GoPlus can't sell
    """
    issues = []
    s = 100.0

    gp_flags = _goplus_flags(gp or {})
    gm_flags = _gmgn_flags((gm or {}).get("info", {}), (gm or {}).get("sec", {}))

    # ── 硬伤 ──
    if "蜜罐" in gp_flags or "蜜罐(GMGN)" in gm_flags:
        return 0, "🚫 蜜罐", gp_flags + gm_flags
    if "禁止卖出" in gp_flags:
        return 0, "🚫 禁止卖出", gp_flags + gm_flags

    # ── 税 ──
    try:
        bt = float((gp or {}).get("buy_tax") or 0) * 100
        st = float((gp or {}).get("sell_tax") or 0) * 100
        mx = max(bt, st)
        if mx > 10:
            s -= 25
            issues.append(f"税 {mx:.0f}%")
        elif mx > 5:
            s -= 10
            issues.append(f"税 {mx:.0f}%")
    except (ValueError, TypeError):
        pass

    # ── GMGN 合约风险 ──
    sec = (gm or {}).get("sec") or {}
    if sec.get("is_renounced") is False:
        s -= 8
        issues.append("owner未放弃")
    if sec.get("is_blacklist") is True:
        s -= 20
        issues.append("黑名单")
    if sec.get("is_open_source") is False:
        s -= 15
        issues.append("未开源")
    ls = sec.get("lock_summary") or {}
    if isinstance(ls, dict) and ls.get("is_locked") is False:
        s -= 12
        issues.append("LP未锁")

    # ── 持仓集中度 ──
    stat = ((gm or {}).get("info") or {}).get("stat") or {}
    info = (gm or {}).get("info") or {}
    t10 = None
    for src in (stat.get("top_10_holder_rate"), sec.get("top_10_holder_rate")):
        if src not in (None, "", 0, "0"):
            t10 = float(src) * 100
            break
    if t10 is None:
        t10, _, _ = _goplus_holders(gp or {})
    if t10:
        if t10 > 50:
            s -= 25
            issues.append(f"top10 {t10:.0f}%")
        elif t10 > 30:
            s -= 14
            issues.append(f"top10 {t10:.0f}%")
        elif t10 > 20:
            s -= 6
            issues.append(f"top10 {t10:.0f}%")

    # 狙击手 / entrapment / bundler / bot
    def _pct(k):
        v = stat.get(k)
        if v in (None, "", 0, "0"):
            return None
        try:
            return float(v) * 100
        except (ValueError, TypeError):
            return None

    v = _pct("top_entrapment_trader_percentage")
    if v and v > 50:
        s -= 22
        issues.append(f"entrapment {v:.0f}%")
    elif v and v > 20:
        s -= 16
        issues.append(f"entrapment {v:.0f}%")

    v = _pct("top_bundler_trader_percentage")
    if v and v > 30:
        s -= 20
        issues.append(f"bundler {v:.0f}%")
    elif v and v > 15:
        s -= 10
        issues.append(f"bundler {v:.0f}%")

    v = _pct("top70_sniper_hold_rate")
    if v and v > 15:
        s -= 15
        issues.append(f"狙击手 {v:.0f}%")
    elif v and v > 5:
        s -= 6
        issues.append(f"狙击手 {v:.0f}%")

    v = _pct("top_rat_trader_percentage")
    if v and v > 5:
        s -= 12
        issues.append(f"老鼠仓 {v:.0f}%")

    v = _pct("bot_degen_rate")
    if v and v > 70:
        s -= 12
        issues.append(f"bot {v:.0f}%")
    elif v and v > 50:
        s -= 6
        issues.append(f"bot {v:.0f}%")

    # creator 发币数（工厂币信号）
    cc = stat.get("creator_created_count")
    if cc not in (None, "", 0):
        try:
            cc = int(cc)
            if cc >= 500:
                s -= 18
                issues.append(f"creator发过{cc}币")
            elif cc >= 200:
                s -= 14
                issues.append(f"creator发过{cc}币")
            elif cc >= 50:
                s -= 10
                issues.append(f"creator发过{cc}币")
            elif cc >= 10:
                s -= 6
                issues.append(f"creator发过{cc}币")
        except (ValueError, TypeError):
            pass

    # rug_ratio（GMGN 自己的 rug 标签）
    if rug is not None and rug >= 0.5:
        s = min(s, 59)
        issues.append("GMGN rug标签")
    elif rug is not None and rug >= 0.3:
        s = min(s, 79)
        issues.append("GMGN rug标签")

    s = max(0.0, min(100.0, s))

    if s >= 80:
        label = "✅ 安全"
    elif s >= 60:
        label = "⚠️ 中风险"
    elif s >= 40:
        label = "❌ 高风险"
    else:
        label = "🚫 极高风险"
    return s, label, issues


# ─────────────────────────────────────────────────────────────
# 对外接口
# ─────────────────────────────────────────────────────────────
def check(token: str) -> Optional[dict]:
    """完整安全检查，返回 None 表示数据全拿不到。
    结果缓存 15 分钟。数据源：GoPlus + GMGN + Binance Audit"""
    addr = (token or "").lower()
    now = time.time()
    with _lock:
        hit = _safety_cache.get(addr)
        if hit and now - hit[0] < _SAFETY_TTL:
            return hit[1]

    gp = _goplus(addr)
    gm = _gmgn(addr)
    rug = rug_ratio(addr)
    
    # Binance Audit 补充数据
    bn_audit = None
    if _HAS_BINANCE:
        try:
            bn_audit = binance_api.get_token_audit("56", addr)
        except Exception as e:
            log.debug(f"[BINANCE-AUDIT] {addr[:10]} err: {e}")

    if not gp and not gm and not bn_audit:
        return None

    info = (gm or {}).get("info") or {}
    sec = (gm or {}).get("sec") or {}
    s, label, issues = score(gp, gm, rug)
    
    # 获取 creator 地址并检查开发者历史
    dev_info = info.get("dev") or {}
    creator_addr = dev_info.get("creator_address", "")
    dev_blacklist_result = None
    if creator_addr:
        dev_blacklist_result = _check_creator_history(creator_addr)
        if dev_blacklist_result.get("blocked"):
            # 开发者在黑名单中，直接标记为蜜罐
            s = 0
            label = "🚫 开发者黑名单"
            issues.append(dev_blacklist_result["reason"])
    
    # 用 Binance Audit 补充/修正安全数据
    if bn_audit:
        # 如果 GoPlus/GMGN 没检测到蜜罐，但 Binance 检测到了
        bn_risks = bn_audit.get("risks", [])
        for r in bn_risks:
            title = r.get("title", "").lower()
            if "honeypot" in title and "蜜罐" not in str(issues):
                s = 0
                label = "🚫 蜜罐"
                issues.append("蜜罐(Binance)")
            elif "blacklist" in title and "黑名单" not in str(issues):
                s = max(s - 20, 0)
                issues.append("黑名单(Binance)")
        
        # 补充税率数据
        bn_buy_tax = bn_audit.get("buyTax", 0)
        bn_sell_tax = bn_audit.get("sellTax", 0)
        if bn_buy_tax > 10 or bn_sell_tax > 10:
            issues.append(f"税过高(Binance) {bn_buy_tax:.0f}%/{bn_sell_tax:.0f}%")
            s = max(s - 25, 0)

    top10 = None
    stat = info.get("stat") or {}
    for src in (stat.get("top_10_holder_rate"), sec.get("top_10_holder_rate")):
        if src not in (None, "", 0, "0"):
            try:
                top10 = float(src) * 100
                break
            except (ValueError, TypeError):
                pass
    g_top10, g_top1, g_lp_locked = _goplus_holders(gp or {})
    if top10 is None and g_top10:
        top10 = g_top10

    _holders_raw = info.get("holder_count") or ((gp or {}).get("holder_count")) or 0
    try:
        holders_n = int(float(_holders_raw))
    except (ValueError, TypeError):
        holders_n = 0

    out = {
        "score": s,
        "label": label,
        "issues": issues,
        "top10_pct": top10,
        "top1_pct": g_top1 or None,
        "lp_locked_pct": g_lp_locked or None,
        "holders": holders_n or None,
        "flags": sorted(set(_goplus_flags(gp or {}) + _gmgn_flags(info, sec))),
        "creator_tokens": stat.get("creator_created_count"),
        "rug_ratio": rug,
        "buy_tax": gp.get("buy_tax") if gp else (str(bn_audit.get("buyTax", 0)) if bn_audit else None),
        "sell_tax": gp.get("sell_tax") if gp else (str(bn_audit.get("sellTax", 0)) if bn_audit else None),
        "mintable": (gp or {}).get("is_mintable") == "1",
        "renounced": sec.get("is_renounced"),
        "open_source": gp.get("is_open_source") if gp else None,
        "has_gp": bool(gp),
        "has_gmgn": bool(info),
        "has_binance": bool(bn_audit),
        "binance_risk_level": bn_audit.get("riskLevelEnum") if bn_audit else None,
        "creator_addr": creator_addr,
        "dev_blacklist": dev_blacklist_result,
    }

    # v9：筹码/交易结构指标（百分比数值，拿不到为 None）
    def _rate(k):
        v = stat.get(k)
        if v in (None, "", 0, "0"):
            return 0.0
        try:
            return float(v) * 100
        except (ValueError, TypeError):
            return 0.0
    out["bundler_pct"] = _rate("top_bundler_trader_percentage")
    out["bot_pct"] = _rate("bot_degen_rate")
    # top10 硬拦截只用 GMGN 口径（剔除 LP；GoPlus 口径含 LP 会误杀）
    out["top10_gmgn_pct"] = _rate("top_10_holder_rate")
    with _lock:
        _safety_cache[addr] = (now, out)
        if len(_safety_cache) > 2000:
            # 清理最老的
            oldest = sorted(_safety_cache.items(), key=lambda kv: kv[1][0])[:500]
            for k, _ in oldest:
                _safety_cache.pop(k, None)
    return out


# ═══════════════════════════════════════════════════════════════
# 开发者黑名单：经常发币但历史最高市值从未超过 500K
# ═══════════════════════════════════════════════════════════════
_DEV_BLACKLIST_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dev_blacklist.json")
_dev_blacklist_lock = threading.Lock()
_dev_blacklist_cache = {"blacklist": {}, "threshold": {"min_tokens": 10, "max_ath_mc": 500000}}
_dev_check_cache = {}  # creator_addr -> (timestamp, result_dict)
_DEV_CHECK_TTL = 3600  # 1 小时缓存


def _load_dev_blacklist():
    """加载开发者黑名单"""
    global _dev_blacklist_cache
    try:
        if os.path.exists(_DEV_BLACKLIST_FILE):
            with open(_DEV_BLACKLIST_FILE, "r") as f:
                _dev_blacklist_cache = json.load(f)
    except Exception as e:
        log.debug(f"[DEV-BL] load err: {e}")


def _save_dev_blacklist():
    """保存开发者黑名单"""
    try:
        with open(_DEV_BLACKLIST_FILE, "w") as f:
            json.dump(_dev_blacklist_cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.debug(f"[DEV-BL] save err: {e}")


def _check_creator_history(creator_addr: str) -> dict:
    """检查开发者历史代币，返回 {blocked: bool, reason: str, ath_mc: float, token_count: int}"""
    if not creator_addr:
        return {"blocked": False, "reason": "", "ath_mc": 0, "token_count": 0}
    
    addr = creator_addr.lower()
    
    # 先检查黑名单
    with _dev_blacklist_lock:
        if addr in _dev_blacklist_cache.get("blacklist", {}):
            info = _dev_blacklist_cache["blacklist"][addr]
            return {
                "blocked": True,
                "reason": f"开发者黑名单 (历史最高MC ${info.get('ath_mc', 0):,.0f}, 发币 {info.get('token_count', 0)} 个)",
                "ath_mc": info.get("ath_mc", 0),
                "token_count": info.get("token_count", 0)
            }
    
    # 检查缓存
    now = time.time()
    if addr in _dev_check_cache:
        ts, result = _dev_check_cache[addr]
        if now - ts < _DEV_CHECK_TTL:
            return result
    
    # 查询 GMGN
    try:
        proc = subprocess.run(
            ["gmgn-cli", "portfolio", "created-tokens", "--chain", "bsc",
             "--wallet", addr, "--order-by", "token_ath_mc", "--direction", "desc", "--raw"],
            capture_output=True, text=True, timeout=15
        )
        if proc.returncode != 0:
            _dev_check_cache[addr] = (now, {"blocked": False, "reason": "", "ath_mc": 0, "token_count": 0})
            return {"blocked": False, "reason": "", "ath_mc": 0, "token_count": 0}
        
        data = json.loads(proc.stdout)
        token_count = data.get("inner_count", 0)
        ath_mc = 0
        
        # 从 creator_ath_info 获取历史最高市值
        ath_info = data.get("creator_ath_info", {})
        if ath_info:
            try:
                ath_mc = float(ath_info.get("ath_mc", 0))
            except (ValueError, TypeError):
                ath_mc = 0
        
        # 如果没有 creator_ath_info，从 tokens 数组里找最大值
        if ath_mc == 0 and data.get("tokens"):
            for t in data["tokens"]:
                try:
                    t_ath = float(t.get("token_ath_mc", 0))
                    if t_ath > ath_mc:
                        ath_mc = t_ath
                except (ValueError, TypeError):
                    pass
        
        result = {"blocked": False, "reason": "", "ath_mc": ath_mc, "token_count": token_count}
        
        # 判断是否符合拦截条件
        threshold = _dev_blacklist_cache.get("threshold", {})
        min_tokens = threshold.get("min_tokens", 10)
        max_ath_mc = threshold.get("max_ath_mc", 500000)
        
        if token_count >= min_tokens and ath_mc < max_ath_mc:
            # 加入黑名单
            with _dev_blacklist_lock:
                _dev_blacklist_cache.setdefault("blacklist", {})[addr] = {
                    "ath_mc": ath_mc,
                    "token_count": token_count,
                    "added_at": now
                }
                _save_dev_blacklist()
            result = {
                "blocked": True,
                "reason": f"开发者黑名单 (历史最高MC ${ath_mc:,.0f} < ${max_ath_mc:,.0f}, 发币 {token_count} 个 >= {min_tokens})",
                "ath_mc": ath_mc,
                "token_count": token_count
            }
            log.info(f"[DEV-BL] 加入黑名单: {addr[:10]}... | 发币 {token_count} 个 | 最高MC ${ath_mc:,.0f}")
        
        _dev_check_cache[addr] = (now, result)
        return result
        
    except Exception as e:
        log.debug(f"[DEV-BL] {addr[:10]} err: {e}")
        _dev_check_cache[addr] = (now, {"blocked": False, "reason": "", "ath_mc": 0, "token_count": 0})
        return {"blocked": False, "reason": "", "ath_mc": 0, "token_count": 0}


# 启动时加载黑名单
_load_dev_blacklist()
