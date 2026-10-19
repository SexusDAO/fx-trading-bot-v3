# ============================================================
# token_filter.py — 买入前安全过滤层
# 移植自 robinhood_sniper/filter.py，适配 fomo_workflow
# ============================================================
# 过滤层级（按顺序执行，任一不通过 → 拒绝买入）：
#   F1. 扫描黑名单检查（近期已被拒绝的 token，跳过重复检测）
#   F2. 币安 Web3 安全审计（蜜罐/高风险合约/买卖税率）
#   F3. 模拟卖出检测（防蜜罐：买入后能否卖出）
# ============================================================
import subprocess, json, time, uuid, os, requests
from config import CHAIN_ID, LOG_FILE

_PROXIES = {"http":  "socks5://REDACTED_PROXY_CREDENTIALS",
            "https": "socks5://REDACTED_PROXY_CREDENTIALS"}

_AUDIT_URL = "https://web3.binance.com/bapi/defi/v1/public/wallet-direct/security/token/audit"
_AUDIT_HEADERS = {
    "Content-Type":  "application/json",
    "Accept-Encoding": "identity",
    "User-Agent":    "Mozilla/5.0",
}

MAX_BUY_TAX        = 10.0
MAX_SELL_TAX       = 10.0
BLOCK_RISK_LEVELS  = {"HIGH"}
SCAN_BL_TTL        = 3600   # 拒绝后 1h 内不重复检测

# 内存级扫描黑名单 {addr: {reason, ts}}
_scan_bl: dict = {}


def _log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────
# F1. 扫描黑名单
# ─────────────────────────────────────────────────────────────

def is_scan_blacklisted(token_addr: str) -> bool:
    entry = _scan_bl.get(token_addr.lower())
    if not entry:
        return False
    if time.time() - entry["ts"] >= SCAN_BL_TTL:
        del _scan_bl[token_addr.lower()]
        return False
    return True


def _add_scan_bl(token_addr: str, symbol: str, reason: str):
    _scan_bl[token_addr.lower()] = {"symbol": symbol, "reason": reason, "ts": time.time()}
    _log(f"[FILTER] 🔕 加入扫描黑名单 {symbol} ← {reason}")


# ─────────────────────────────────────────────────────────────
# F2. 币安 Web3 安全审计
# ─────────────────────────────────────────────────────────────

def audit_token(token_addr: str) -> dict | None:
    try:
        r = requests.post(
            _AUDIT_URL,
            headers=_AUDIT_HEADERS,
            json={
                "binanceChainId":  CHAIN_ID,
                "contractAddress": token_addr,
                "requestId":       str(uuid.uuid4()),
            },
            timeout=12,
            proxies=_PROXIES,
        )
        d = r.json()
        if d.get("code") == "000000":
            return d.get("data")
    except Exception as e:
        _log(f"[FILTER] 审计 API 异常: {e}")
    return None


def security_gate(token_addr: str, symbol: str) -> tuple[bool, str]:
    """
    安全审计一票否决。
    返回 (passed, reason)
    """
    audit = audit_token(token_addr)

    if audit is None:
        # API 无响应：允许继续（不阻断），但记录
        _log(f"[FILTER] ⚠️  {symbol} 审计 API 无响应，跳过安全门")
        return True, "⚠️ 审计无响应，未经验证"

    risk_level = audit.get("riskLevelEnum", "UNKNOWN")
    extra      = audit.get("extraInfo") or {}
    buy_tax    = float(extra.get("buyTax",  0) or 0)
    sell_tax   = float(extra.get("sellTax", 0) or 0)
    flagged    = extra.get("isFlaggedByVendor", False)
    blacklist  = extra.get("isBlacklist", False)

    if risk_level in BLOCK_RISK_LEVELS:
        return False, f"❌ 安全审计: 风险等级 {risk_level}"
    if flagged:
        return False, "❌ 安全审计: 被安全供应商标记为危险"
    if blacklist:
        return False, "❌ 安全审计: 在黑名单中"
    if buy_tax > MAX_BUY_TAX:
        return False, f"❌ 安全审计: 买税 {buy_tax}% > {MAX_BUY_TAX}%"
    if sell_tax > MAX_SELL_TAX:
        return False, f"❌ 安全审计: 卖税 {sell_tax}% > {MAX_SELL_TAX}%"

    # 检查高危风险项
    for item in (audit.get("riskItems") or []):
        for detail in (item.get("details") or []):
            if detail.get("isHit") and detail.get("riskType") == "HIGH":
                return False, f"❌ 安全审计: 高危风险 [{detail.get('title', '')}]"

    return True, f"✅ 安全: 风险={risk_level} 买税={buy_tax}% 卖税={sell_tax}%"


# ─────────────────────────────────────────────────────────────
# F3. 模拟卖出检测（蜜罐检测）
# ─────────────────────────────────────────────────────────────

USDG_ADDRESS = "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"
HONEYPOT_PROBE_USD = 5.0   # 用 5 USDG 等值测试是否能卖出


# simulate_sell 缓存：同一 token 5分钟内不重复执行模拟卖出（节省 2次 baw quote 调用）
_SIM_CACHE: dict = {}   # addr_lower -> (result_tuple, timestamp)
_SIM_CACHE_TTL = 300    # 5 分钟（合约安全状态变化慢）


def simulate_sell(token_addr: str, symbol: str) -> tuple[bool, str]:
    """
    模拟卖出：先询价 5 USDG 能买多少 token，再询价这些 token 能卖回多少 USDG。
    如果卖回金额 > 0 说明 DEX 可以成交，非蜜罐。
    缓存 TTL=300s，5分钟内同一 token 不重复执行（节省 2 次 baw quote）。
    """
    addr = token_addr.lower()
    now  = time.time()
    if addr in _SIM_CACHE and now - _SIM_CACHE[addr][1] < _SIM_CACHE_TTL:
        cached_result = _SIM_CACHE[addr][0]
        return cached_result
    try:
        # Step1: USDG → token（算出 5U 能买多少 token）
        r1 = subprocess.run(
            ["baw", "market-order", "quote",
             "--binanceChainId", CHAIN_ID,
             "--fromTokenQty", str(HONEYPOT_PROBE_USD),
             "--fromToken", USDG_ADDRESS,
             "--toToken",   token_addr,
             "--json"],
            capture_output=True, text=True, timeout=12,
        )
        d1 = json.loads(r1.stdout)
        if not d1.get("success"):
            return True, "⚠️ 模拟买入询价失败，跳过蜜罐检测"

        token_qty = float(d1["data"].get("toCoinAmount", 0) or 0)
        if token_qty <= 0:
            r = (False, "❌ 蜜罐检测: 买入询价返回 0 token")
            _SIM_CACHE[addr] = (r, now)
            return r

        # Step2: token → USDG（卖出刚才能买到的 token 量）
        r2 = subprocess.run(
            ["baw", "market-order", "quote",
             "--binanceChainId", CHAIN_ID,
             "--fromTokenQty", str(round(token_qty * 0.99, 6)),   # 99%，避免舍入
             "--fromToken", token_addr,
             "--toToken",   USDG_ADDRESS,
             "--json"],
            capture_output=True, text=True, timeout=12,
        )
        d2 = json.loads(r2.stdout)
        if not d2.get("success"):
            r = (False, "❌ 蜜罐检测: 无法获得卖出报价（可能是蜜罐）")
            _SIM_CACHE[addr] = (r, now)
            return r

        usdg_back = float(d2["data"].get("toCoinAmount", 0) or 0)
        if usdg_back <= 0:
            r = (False, "❌ 蜜罐检测: 卖出报价为 0（蜜罐确认）")
            _SIM_CACHE[addr] = (r, now)
            return r

        # 计算损耗（正常滑点+手续费应 < 20%）
        loss_pct = (HONEYPOT_PROBE_USD - usdg_back) / HONEYPOT_PROBE_USD * 100
        if loss_pct > 50:
            r = (False, f"❌ 蜜罐检测: 卖出损耗 {loss_pct:.1f}% > 50%（疑似蜜罐）")
            _SIM_CACHE[addr] = (r, now)
            return r

        r = (True, f"✅ 模拟卖出通过 (损耗 {loss_pct:.1f}%)")
        _SIM_CACHE[addr] = (r, now)
        # 写入缓存，供 SIGNAL_ONLY 读取
        _SEC_DETAIL_CACHE.setdefault(addr, {})["simulate_slippage"] = loss_pct / 100
        return r

    except Exception as e:
        _log(f"[FILTER] 模拟卖出异常 {symbol}: {e}")
        return True, f"⚠️ 模拟卖出异常({e})，跳过"


# ─────────────────────────────────────────────────────────────
# 主入口：全量过滤
# ─────────────────────────────────────────────────────────────

def run_filters(token_addr: str, symbol: str,
                pool_version: str = "", pool_addr: str = "") -> tuple[bool, str]:
    """
    依次执行 F1 → F2 → F3 → F3.5，任一拒绝返回 (False, reason)。
    全部通过返回 (True, "✅ 过滤通过")
    pool_version: "V2"/"V3"/"V4"，由 pair_listener 传入，默认空字符串（跳过 F3.5）
    pool_addr: V4 池地址，用于 F3.5 Hook 检测
    """
    addr = token_addr.lower()

    # F1: 扫描黑名单
    if is_scan_blacklisted(addr):
        entry = _scan_bl.get(addr, {})
        return False, f"🔕 黑名单: {entry.get('reason', '近期已拒绝')}"

    # F2: 安全审计
    passed, reason = security_gate(addr, symbol)
    if not passed:
        _add_scan_bl(addr, symbol, reason)
        return False, reason

    # F3: 模拟卖出（蜜罐检测）
    passed, reason = simulate_sell(addr, symbol)
    if not passed:
        _add_scan_bl(addr, symbol, reason)
        return False, reason

    # F3.5: Pool Hook 检测（仅 V4 池子，检测失败不阻断买入，只记录警告日志）
    if pool_version in ("V4", "v4") and pool_addr:
        hook_safe, hook_reason = check_hook_contract(pool_addr, pool_version)
        if not hook_safe:
            _log(f"[F3.5 WARN] {symbol} Hook 风险: {hook_reason}（不阻断买入，记录日志）")
        else:
            _log(f"[F3.5 OK] {symbol} {hook_reason}")

    _log(f"[FILTER] ✅ {symbol} 通过全部过滤: {reason}")
    return True, "✅ 过滤通过"


# ─────────────────────────────────────────────────────────────
# F4: Windvane fo量检查
# fo量 = FOMO 持仓人数 / 当前持仓 token 的总持仓人数
# 推荐阈值：fo量 > 20% 时胜率显著更高
# ─────────────────────────────────────────────────────────────

_WV_COOKIE = 'bnc-uuid=52fe9a2c-9a68-425a-a395-111e2588c4bb; board_sid=9C7MsD2h96fM2oE4dRijzJP2FB46XMi26I4yOhxkVVY'
_WV_BASE   = 'https://wind.jokkimon.club'
_WV_CACHE: dict = {}   # token_addr → (fo_pct, ts)
_WV_CACHE_TTL = 120    # 2分钟缓存

def get_fo_pct(token_addr: str) -> float | None:
    """
    查询 token 在 windvane 的 fo量（FOMO 持仓占比）。
    利用 accounts_index 的 evm_addr 列表与 fomoapi 实时持仓人数交叉计算。
    返回 0.0~1.0 或 None（查询失败）。
    """
    addr = token_addr.lower()
    now  = time.time()
    if addr in _WV_CACHE and now - _WV_CACHE[addr][1] < _WV_CACHE_TTL:
        return _WV_CACHE[addr][0]

    try:
        proxies = {
            'https': 'socks5://REDACTED_PROXY_CREDENTIALS',
            'http':  'socks5://REDACTED_PROXY_CREDENTIALS',
        }
        hdrs = {
            'User-Agent': 'Mozilla/5.0 Chrome/125',
            'Accept': 'application/json',
            'Referer': 'https://wind.jokkimon.club/windvane',
        }
        session = requests.Session()
        session.proxies  = proxies
        session.headers.update(hdrs)
        for c in _WV_COOKIE.split('; '):
            k, v = c.split('=', 1)
            session.cookies.set(k, v, domain='wind.jokkimon.club')

        # 用 robinhoodtrenches /api/tape 查最近买入了该 token 的 trader 列表
        # 对应 fomo handle 和 windvane 的 accounts_index 交叉
        r_tape = requests.get(
            'https://robinhoodtrenches.com/api/tape',
            headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://robinhoodtrenches.com/'},
            proxies=proxies, timeout=8
        )
        tape = r_tape.json()

        # 统计持有该 token 的 trader 数量
        token_holders = {t['handle'] for t in tape
                         if t['token'].lower() == addr and t['side'] == 'buy'}
        total_holders  = len({t['handle'] for t in tape})   # 近期活跃 trader 总数

        # robinhoodtrenches /api/traders 里持有该 token 的
        r_traders = requests.get(
            'https://robinhoodtrenches.com/api/traders',
            headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://robinhoodtrenches.com/'},
            proxies=proxies, timeout=8
        )
        traders = r_traders.json()
        fo_holders  = sum(1 for t in traders
                          if any(b.get('token','').lower() == addr
                                 for b in (t.get('open_bags') if isinstance(t.get('open_bags'), list) else [])))
        total_traders = len(traders)

        # fo量 = 持有该 token 的 fomo trader / 总 fomo trader
        if total_traders > 0 and fo_holders > 0:
            fo_pct = fo_holders / total_traders
            _WV_CACHE[addr] = (fo_pct, now)
            return fo_pct

        # 备用：用 tape 里的信号
        if total_holders > 0 and token_holders:
            fo_pct = len(token_holders) / total_holders
            _WV_CACHE[addr] = (fo_pct, now)
            return fo_pct

        return None

    except Exception as e:
        _log(f"[FO_PCT] 查询失败 {token_addr[:16]}: {e}")
        return None


# ─────────────────────────────────────────────────────────────
# F3.5: Pool Hook 合约检测（仅 V4 池子）
# 恶意 Hook 可在 swap 时执行任意逻辑（如阻止卖出）
# 检测失败不阻断买入，只记录警告日志
# ─────────────────────────────────────────────────────────────

_HOOK_WHITELIST: set = set()   # 可信 Hook 地址（未来可从 config 加载）
_HOOK_CACHE: dict = {}         # hook_addr_lower → (safe: bool, ts: float)
_HOOK_CACHE_TTL = 3600         # 1 小时缓存

DANGEROUS_HOOK_METHODS = {
    "beforeSwap", "afterSwap",
    "beforeAddLiquidity", "afterAddLiquidity",
    "beforeRemoveLiquidity", "afterRemoveLiquidity",
}


def _get_pool_hook(pool_addr: str) -> str | None:
    """通过 eth_call slot0() 获取 V4 pool 的 hook 地址（slot0 返回的最后 20 字节）。"""
    try:
        r = requests.post(
            "https://mainnet.robinhood.io",
            json={
                "jsonrpc": "2.0", "id": 1,
                "method": "eth_call",
                "params": [{"to": pool_addr, "data": "0x3850c7bd"}, "latest"],
            },
            timeout=5,
            proxies={"https": "socks5://REDACTED_PROXY_CREDENTIALS",
                     "http":  "socks5://REDACTED_PROXY_CREDENTIALS"},
        )
        result = r.json().get("result", "") or ""
        # result 是 abi 编码的返回值，取最后 40 个十六进制字符作为地址
        if len(result) >= 42:
            return "0x" + result[-40:]
        return None
    except Exception:
        return None


def check_hook_contract(pool_addr: str, version: str) -> tuple[bool, str]:
    """
    F3.5：检测 V4 Pool 的 Hook 合约是否安全。
    非 V4 直接返回 (True, "")，不执行任何检测。
    返回 (safe: bool, reason: str)。
    safe=False 时调用方只记录日志，不拒绝买入。
    """
    if version not in ("V4", "v4"):
        return True, ""

    try:
        hook_addr = _get_pool_hook(pool_addr)
        zero_addr = "0x" + "0" * 40

        # 无 Hook 合约（零地址）→ 通过
        if not hook_addr or hook_addr.lower() == zero_addr:
            return True, "无 Hook 合约"

        hook_addr = hook_addr.lower()

        # 白名单直接放行
        if hook_addr in _HOOK_WHITELIST:
            return True, f"Hook {hook_addr[:12]}… 在白名单"

        # 命中缓存
        if hook_addr in _HOOK_CACHE:
            safe, ts = _HOOK_CACHE[hook_addr]
            if time.time() - ts < _HOOK_CACHE_TTL:
                return safe, f"Hook {'安全' if safe else '风险'}（缓存）"

        # 查 Blockscout 验证状态
        r = requests.get(
            f"https://robinhood.blockscout.com/api/v2/smart-contracts/{hook_addr}",
            timeout=6,
            proxies={"https": "socks5://REDACTED_PROXY_CREDENTIALS"},
        )
        if r.status_code != 200:
            _HOOK_CACHE[hook_addr] = (False, time.time())
            return False, f"Hook {hook_addr[:12]}… 源码未在 Blockscout 验证"

        data = r.json()
        if not data.get("is_verified"):
            _HOOK_CACHE[hook_addr] = (False, time.time())
            return False, f"Hook {hook_addr[:12]}… 未验证源码"

        # 检查 ABI 中是否含危险方法
        abi       = data.get("abi") or []
        abi_funcs = {f.get("name", "") for f in abi if f.get("type") == "function"}
        dangerous = DANGEROUS_HOOK_METHODS & abi_funcs
        if dangerous:
            _HOOK_CACHE[hook_addr] = (False, time.time())
            return False, f"Hook {hook_addr[:12]}… 含危险方法: {sorted(dangerous)}"

        _HOOK_CACHE[hook_addr] = (True, time.time())
        return True, f"Hook {hook_addr[:12]}… ABI 验证通过"

    except Exception as e:
        # 检测本身异常：不阻断，返回安全（保守策略：宁可漏报不误报）
        return True, f"Hook 检测异常（跳过）: {e}"


# ─────────────────────────────────────────────────────────────
# F5: Clawby dex_token_security — 买入前安全审计
# 过滤蜜罐、高税率、黑名单合约
# ─────────────────────────────────────────────────────────────

CLAWBY_KEY     = "pk_xBgG9wi3q1ZnIe6peyFrBHQP_6v2hG2C"
CLAWBY_RELAY   = "https://api.openclawby.com/api/relay"

# ── Clawby 燕断器（全局，所有接口共用） ──────────────────────────
# 连续 3 次 5xx → 燕断 300s，期间所有 Clawby 调用直接走 fallback
_CLAWBY_FAIL_COUNT   = 0
_CLAWBY_FAIL_MAX     = 3
_CLAWBY_COOLDOWN_SEC = 300
_CLAWBY_DOWN_UNTIL: float = 0.0

def _clawby_is_down() -> bool:
    import time as _t
    return _t.time() < _CLAWBY_DOWN_UNTIL

def _clawby_record_fail(status: int = 0):
    global _CLAWBY_FAIL_COUNT, _CLAWBY_DOWN_UNTIL
    import time as _t
    _CLAWBY_FAIL_COUNT += 1
    if _CLAWBY_FAIL_COUNT >= _CLAWBY_FAIL_MAX:
        _CLAWBY_DOWN_UNTIL = _t.time() + _CLAWBY_COOLDOWN_SEC
        _log(f"[CLAWBY] 连续失败 {_CLAWBY_FAIL_COUNT} 次 (HTTP {status})，燕断 {_CLAWBY_COOLDOWN_SEC}s走 fallback")
    else:
        _log(f"[CLAWBY] 请求失败 HTTP {status}，失败计数 {_CLAWBY_FAIL_COUNT}/{_CLAWBY_FAIL_MAX}")

def _clawby_record_ok():
    global _CLAWBY_FAIL_COUNT, _CLAWBY_DOWN_UNTIL
    _CLAWBY_FAIL_COUNT = 0
    _CLAWBY_DOWN_UNTIL = 0.0

_SEC_CACHE: dict = {}
_SEC_CACHE_TTL  = 300  # 5分钟缓存（安全信息变化慢）

# GoPlus chain id 映射（Clawby 挂掉时的 fallback）
# Robinhood chain 4663 目前 GoPlus 未收录，用 DexScreener sells 反推
_GOPLUS_CHAIN_MAP = {"1": "1", "56": "56", "137": "137", "42161": "42161",
                     "10": "10", "8453": "8453", "4663": None}  # None=不支持

def _check_security_goplus(token_addr: str, sym: str) -> tuple[bool, str] | None:
    """GoPlus fallback。链不支持时返回 None（放行上层决策）。"""
    _PROXY = {"https": "socks5://REDACTED_PROXY_CREDENTIALS",
               "http":  "socks5://REDACTED_PROXY_CREDENTIALS"}
    # Robinhood chain 尚未收录 GoPlus，回退到 DexScreener 反推
    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{token_addr}",
            timeout=8, proxies=_PROXY)
        pairs = r.json().get("pairs", []) or []
        if not pairs:
            return None
        pair = max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))
        txns = pair.get("txns", {})
        sells_5m = int((txns.get("m5") or {}).get("sells", 0))
        sells_1h = int((txns.get("h1") or {}).get("sells", 0))
        # 如果完全没有卖出记录（且有买入），高度疑似蜜罐
        buys_1h = int((txns.get("h1") or {}).get("buys", 0))
        if buys_1h > 5 and sells_1h == 0 and sells_5m == 0:
            reason = f"{sym} ⚠️ [GoPlus-fallback/DexScreener] 无卖出记录，疑似蜜罐"
            _log(f"[SECURITY-FB] {reason}")
            return False, reason
        _log(f"[SECURITY-FB] {sym} DexScreener 反推: buys_1h={buys_1h} sells_1h={sells_1h}，放行")
        return True, f"{sym} [DexScreener-fallback] sells_1h={sells_1h} 可卖出"
    except Exception as e:
        _log(f"[SECURITY-FB] {sym} DexScreener 反推异常({e})，放行")
        return None


def check_token_security(token_addr: str, token_symbol: str = "") -> tuple[bool, str]:
    """
    调用 Clawby dex_token_security 检测 token 安全性。
    Clawby 失败时自动 fallback → GoPlus（EVM 主链）→ DexScreener 反推（Robinhood链）。
    返回 (safe, reason)
      safe=True  → 通过，可以买入
      safe=False → 危险，拒绝买入
    """
    addr = token_addr.lower()
    now  = time.time()
    if addr in _SEC_CACHE and now - _SEC_CACHE[addr][1] < _SEC_CACHE_TTL:
        return _SEC_CACHE[addr][0]

    sym = token_symbol or addr[:8]
    _clawby_ok = False
    sec = None
    if not _clawby_is_down():
        try:
            r = requests.post(
                CLAWBY_RELAY,
                headers={"X-API-Key": CLAWBY_KEY, "Content-Type": "application/json"},
                json={"name": "dex_token_security",
                      "params": {"chain": "robinhood", "address": token_addr}},
                proxies={"https": "socks5://REDACTED_PROXY_CREDENTIALS",
                         "http":  "socks5://REDACTED_PROXY_CREDENTIALS"},
                timeout=8,
            )
            if r.status_code >= 500:
                _clawby_record_fail(r.status_code)
            else:
                outer = r.json()
                if "data" in outer:
                    sec = outer["data"].get("data") or outer["data"]
                    if sec and isinstance(sec, dict) and sec.get("address"):
                        _clawby_ok = True
                        _clawby_record_ok()
        except Exception as e:
            _clawby_record_fail()
            _log(f"[SECURITY] {sym} Clawby 异常({e})，尝试 fallback")
    else:
        _log(f"[SECURITY] {sym} Clawby 熔断中，直接走 fallback")

    # ── Clawby 失败 → fallback ──────────────────────────────
    if not _clawby_ok:
        _log(f"[SECURITY] {sym} Clawby 无效响应，触发 fallback")
        fb_result = _check_security_goplus(token_addr, sym)
        if fb_result is not None:
            ok, reason = fb_result
            _SEC_CACHE[addr] = ((ok, reason), now)
            if not ok:
                _SEC_DETAIL_CACHE[addr] = {"honeypot": True, "ts": now}
            return ok, reason
        # fallback 也拿不到数据，放行
        reason = f"{sym} 安全审计全部失败，放行"
        _log(f"[SECURITY] {reason}")
        return True, reason

    if not sec or (isinstance(sec, dict) and not sec.get("address")):
        _log(f"[SECURITY] {sym} 无安全数据，放行")
        return True, "无安全数据，放行"

    # ── 拦截条件 ───────────────────────────────────────────────
    try:
        # 1. 蜜罐
        if sec.get("is_honeypot") or str(sec.get("honeypot", "0")) == "1":
            reason = f"{sym} ⚠️ 蜜罐合约，拒绝买入"
            _SEC_CACHE[addr] = ((False, reason), now)
            _SEC_DETAIL_CACHE[addr] = {"honeypot": True, "ts": now}
            return False, reason

        # 2. 无法卖出
        can_sell = sec.get("can_sell")
        if can_sell is not None and str(can_sell) == "0" and str(sec.get("can_not_sell","0")) == "1":
            reason = f"{sym} ⚠️ 合约禁止卖出，拒绝买入"
            _SEC_CACHE[addr] = ((False, reason), now)
            return False, reason

        # 3. 高卖出税（>10%）
        try:
            sell_tax = float(sec.get("sell_tax") or 0)
            if sell_tax > 10:
                reason = f"{sym} ⚠️ 卖出税 {sell_tax:.0f}%，拒绝买入"
                _SEC_CACHE[addr] = ((False, reason), now)
                return False, reason
        except (ValueError, TypeError):
            pass

        # 4. 黑名单合约
        if sec.get("is_blacklist") or str(sec.get("blacklist", "0")) == "1":
            reason = f"{sym} ⚠️ 含黑名单功能，拒绝买入"
            _SEC_CACHE[addr] = ((False, reason), now)
            return False, reason

        # ── 通过 ───────────────────────────────────────────────
        top10 = sec.get("top_10_holder_rate", "?")
        renounced = "✅已放弃" if sec.get("is_renounced") else "⚠️未放弃"
        reason = f"{sym} 安全通过 | renounced={renounced} top10={top10} sell_tax={sec.get('sell_tax','0')}%"
        _log(f"[SECURITY] {reason}")
        _SEC_CACHE[addr] = ((True, reason), now)
        # 写入详情缓存（供 SIGNAL_ONLY 使用）
        _SEC_DETAIL_CACHE[addr] = {
            "honeypot": False,
            "buy_tax":  float(sec.get("buy_tax") or 0),
            "sell_tax": float(sec.get("sell_tax") or 0),
            "ts": now,
        }
        return True, reason

    except Exception as e:
        _log(f"[SECURITY] {sym} 审计异常({e})，放行")
        return True, f"审计异常，放行"


# ─────────────────────────────────────────────────────────────
# F6: Clawby dex_token_pool — 流动性过滤
# 极薄池子（< $10k）拒绝买入
# ─────────────────────────────────────────────────────────────

_POOL_CACHE: dict = {}
_POOL_CACHE_TTL = 60  # 1分钟

# 按市値分层的最低流动性门槛（适配 Robinhood Chain 新兴链薄池子环境）
# 市値 < 100k  → $8k
# 市値 100k~300k → $25k
# 市値 ≥ 300k  → $60k
_LIQ_TIER = [
    (100_000,  8_000),
    (300_000, 25_000),
    (float("inf"), 60_000),
]

# 充击比门槛：买入金额 / 池子流动性 > 0.5% 时拒绝（防止小池子被冲击带死）
IMPACT_RATIO_MAX = 0.005   # 0.5%

# 安全审计详情缓存（供 SIGNAL_ONLY 模式读取）
_SEC_DETAIL_CACHE: dict = {}  # addr_lower → {honeypot, buy_tax, sell_tax, simulate_slippage, ts}
_LIQ_DETAIL_CACHE: dict = {}  # addr_lower → {liquidity_usd, ts}


def get_security_detail(token_addr: str) -> dict:
    """SIGNAL_ONLY 辅助：返回该 token 已缓存的安全详情。
    数据在 check_token_security 执行期间写入。
    未命中缓存时返回空 dict。"""
    return _SEC_DETAIL_CACHE.get(token_addr.lower(), {})


def get_liquidity_detail(token_addr: str) -> dict:
    """返回最近一次 check_token_liquidity 查询到的流动性数据。"""
    return _LIQ_DETAIL_CACHE.get(token_addr.lower(), {})


def check_token_liquidity(token_addr: str, token_symbol: str = "",
                           min_liquidity_usd: float = 0,
                           current_mc: float = 0,
                           buy_size_usd: float = 0) -> tuple[bool, str]:
    """
    查询 dex_token_pool 确认流动性充足。
    - 按市値分层设置最低流动性门槛（current_mc 不为 0 时生效）
    - 充击比检查：buy_size_usd / liq > 0.5% 时拒绝（buy_size_usd 不为 0 时生效）
    返回 (ok, reason)
    """
    addr = token_addr.lower()
    now  = time.time()
    if addr in _POOL_CACHE and now - _POOL_CACHE[addr][1] < _POOL_CACHE_TTL:
        return _POOL_CACHE[addr][0]

    sym = token_symbol or addr[:8]
    _PROXY = {"https": "socks5://REDACTED_PROXY_CREDENTIALS",
               "http":  "socks5://REDACTED_PROXY_CREDENTIALS"}
    liq = None

    # ── 先尝试 Clawby（熔断期内直接跳过） ──
    if not _clawby_is_down():
        try:
            r = requests.post(
                CLAWBY_RELAY,
                headers={"X-API-Key": CLAWBY_KEY, "Content-Type": "application/json"},
                json={"name": "dex_token_pool",
                      "params": {"chain": "robinhood", "address": token_addr}},
                proxies=_PROXY, timeout=8,
            )
            if r.status_code >= 500:
                _clawby_record_fail(r.status_code)
            else:
                d = r.json()
                if "data" in d:
                    pool = d["data"].get("data", d["data"])
                    if pool and isinstance(pool, dict):
                        liq = float(pool.get("liquidity") or 0)
                        _clawby_record_ok()
        except Exception as e:
            _clawby_record_fail()
            _log(f"[LIQUIDITY] {sym} Clawby 异常({e})")

    # ── Clawby 失败/熔断 → DexScreener fallback ──
    if liq is None:
        _log(f"[LIQUIDITY] {sym} Clawby 无效，fallback DexScreener")
        try:
            r2 = requests.get(
                f"https://api.dexscreener.com/latest/dex/tokens/{token_addr}",
                timeout=8, proxies=_PROXY)
            pairs = r2.json().get("pairs", []) or []
            if pairs:
                best = max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))
                liq = float((best.get("liquidity") or {}).get("usd") or 0)
                _log(f"[LIQUIDITY-FB] {sym} DexScreener liquidity=${liq:,.0f}")
        except Exception as e2:
            _log(f"[LIQUIDITY-FB] {sym} DexScreener 异常({e2})，放行")
            return True, f"流动性查询全部失败，放行"

    if liq is None:
        return True, "无池子数据，放行"

    try:
        # 市値分层门槛（current_mc 不为 0 时走分层逻辑，否则回落 min_liquidity_usd 延展参数）
        if current_mc > 0:
            for mc_threshold, liq_threshold in _LIQ_TIER:
                if current_mc < mc_threshold:
                    effective_min = liq_threshold
                    break
        else:
            effective_min = min_liquidity_usd or _LIQ_TIER[0][1]  # 无市値时用最低档

        if liq < effective_min:
            reason = (f"{sym} 流动性 ${liq:,.0f} < ${effective_min:,.0f}"
                      f"(市値分层门槛 MC={'${:,.0f}'.format(current_mc) if current_mc else '?'})，拒绝买入")
            _POOL_CACHE[addr] = ((False, reason), now)
            return False, reason

        # 充击比检查（buy_size_usd 不为 0 且池子流动性可用）
        if buy_size_usd > 0 and liq > 0:
            impact = buy_size_usd / liq
            if impact > IMPACT_RATIO_MAX:
                reason = (f"{sym} 充击比 {impact:.2%} > {IMPACT_RATIO_MAX:.1%}"
                          f" (${buy_size_usd:.0f} / ${liq:,.0f})，池子太薄拒绝")
                _POOL_CACHE[addr] = ((False, reason), now)
                return False, reason

        reason = f"{sym} 流动性 ${liq:,.0f} 充击比 {buy_size_usd/liq:.2%} ✅" if buy_size_usd else f"{sym} 流动性 ${liq:,.0f} ✅"
        _POOL_CACHE[addr] = ((True, reason), now)
        _LIQ_DETAIL_CACHE[addr] = {"liquidity_usd": liq, "ts": now}  # 写入详情缓存
        return True, reason

    except Exception as e:
        _log(f"[LIQUIDITY] {sym} 查询异常({e})，放行")
        return True, f"流动性查询异常，放行"


# ─────────────────────────────────────────────────────────────
# F7: Clawby dex_token_info — 新币过滤 + 补全 symbol
# 上线超过 7 天的老币跳过（罗宾汉链跟单价值在新发射阶段）
# ─────────────────────────────────────────────────────────────

_INFO_CACHE: dict = {}
_INFO_CACHE_TTL = 120

MAX_TOKEN_AGE_DAYS = 7  # 上线超过 7 天视为老币

def get_token_info(token_addr: str) -> dict | None:
    """
    查询 token 基础信息，含 symbol/name/上线时间/持仓人数。
    Clawby 失败时 fallback → DexScreener（拼装兼容格式）。
    """
    addr = token_addr.lower()
    now  = time.time()
    if addr in _INFO_CACHE and now - _INFO_CACHE[addr][1] < _INFO_CACHE_TTL:
        return _INFO_CACHE[addr][0]

    _PROXY = {"https": "socks5://REDACTED_PROXY_CREDENTIALS",
               "http":  "socks5://REDACTED_PROXY_CREDENTIALS"}
    info = None

    # ── 先尝试 Clawby（熔断期内直接跳过） ──
    if not _clawby_is_down():
        try:
            r = requests.post(
                CLAWBY_RELAY,
                headers={"X-API-Key": CLAWBY_KEY, "Content-Type": "application/json"},
                json={"name": "dex_token_info",
                      "params": {"chain": "robinhood", "address": token_addr}},
                proxies=_PROXY, timeout=8,
            )
            if r.status_code >= 500:
                _clawby_record_fail(r.status_code)
            else:
                d = r.json()
                _info = d.get("data", {}).get("data") or d.get("data", {})
                if _info and _info.get("symbol"):
                    info = _info
                    _clawby_record_ok()
        except Exception:
            _clawby_record_fail()

    # ── Clawby 失败/熔断 → DexScreener fallback（拼装兼容格式） ──
    if not info:
        try:
            r2 = requests.get(
                f"https://api.dexscreener.com/latest/dex/tokens/{token_addr}",
                timeout=8, proxies=_PROXY)
            pairs = r2.json().get("pairs", []) or []
            if pairs:
                pair = max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))
                bt = pair.get("baseToken", {})
                created = pair.get("pairCreatedAt")
                # 拼装与 Clawby info 兼容的字段
                info = {
                    "symbol":          bt.get("symbol", ""),
                    "name":            bt.get("name", ""),
                    "address":         token_addr,
                    "open_timestamp":  int(created / 1000) if created else None,
                    "holder_count":    None,   # DexScreener 无此字段
                    "_source":         "dexscreener_fallback",
                }
                _log(f"[INFO-FB] {token_addr[:12]}… DexScreener fallback: {info['symbol']}")
        except Exception as e2:
            _log(f"[INFO-FB] {token_addr[:12]}… DexScreener 异常({e2})")

    if info:
        _INFO_CACHE[addr] = (info, now)
    return info


def check_token_age(token_addr: str, token_symbol: str = "") -> tuple[bool, str]:
    """
    检查 token 上线时间，超过 MAX_TOKEN_AGE_DAYS 天则跳过。
    返回 (ok, reason)
    """
    sym = token_symbol or token_addr[:8]
    info = get_token_info(token_addr)
    if not info:
        return True, f"{sym} 无上线时间数据，放行"

    open_ts = info.get("open_timestamp")
    if not open_ts:
        return True, f"{sym} 无上线时间数据，放行"

    age_days = (time.time() - int(open_ts)) / 86400
    if age_days > MAX_TOKEN_AGE_DAYS:
        reason = f"{sym} 上线 {age_days:.0f} 天 > {MAX_TOKEN_AGE_DAYS} 天，非新币跳过"
        return False, reason

    reason = f"{sym} 上线 {age_days:.1f} 天 ✅ 新币"
    return True, reason


# ─────────────────────────────────────────────────────────────
# F8: Clawby dex_smart_money — 独立信号交叉验证
# fomoapi 信号触发后，检查 Clawby 近 10 笔是否有相同 token 买入
# ─────────────────────────────────────────────────────────────

_SM_CACHE: dict = {}
_SM_CACHE_TTL = 120  # 120s（原 30s）— smart_money 列表变化慢，接受少量延迟可节偦4x调用

def check_smart_money_confirmation(token_addr: str, token_symbol: str = "") -> tuple[bool, int, str]:
    """
    查询 dex_smart_money 近期是否有人买入同一 token。
    返回 (confirmed, buyer_count, reason)
      confirmed=True  → Clawby 也看到买入，信号双源确认
      confirmed=False → 仅 fomoapi 单源，正常买入也可，不拦截
    """
    addr = token_addr.lower()
    sym  = token_symbol or addr[:8]
    now  = time.time()
    if addr in _SM_CACHE and now - _SM_CACHE[addr][1] < _SM_CACHE_TTL:
        return _SM_CACHE[addr][0]

    try:
        r = requests.post(
            CLAWBY_RELAY,
            headers={"X-API-Key": CLAWBY_KEY, "Content-Type": "application/json"},
            json={"name": "dex_smart_money",
                  "params": {"chain": "robinhood", "limit": 20}},
            proxies={"https": "socks5://REDACTED_PROXY_CREDENTIALS",
                     "http":  "socks5://REDACTED_PROXY_CREDENTIALS"},
            timeout=8,
        )
        d = r.json()
        rows = d.get("data", {}).get("data", {}).get("list", [])

        # 统计近期买入该 token 的聪明钱数量
        buyers = [row for row in rows
                  if row.get("base_address", "").lower() == addr
                  and row.get("side") == "buy"]
        count = len(buyers)

        if count > 0:
            total_usd = sum(float(b.get("amount_usd") or 0) for b in buyers)
            reason = f"{sym} Clawby 确认 {count} 笔买入 总${total_usd:,.0f} ✅ 双源信号"
            result = (True, count, reason)
        else:
            reason = f"{sym} Clawby 暂无买入记录，单源信号"
            result = (False, 0, reason)

        _SM_CACHE[addr] = (result, now)
        return result

    except Exception as e:
        return False, 0, f"{sym} Clawby 查询异常({e})"


# ─────────────────────────────────────────────────────────────
# F9: 防刷量检测 — 买卖地址去重比 / 单钱包占比 / tx 间隔均匀性
# 数据源：DexScreener pair txns（5min 窗口）
# ─────────────────────────────────────────────────────────────

_ANTIWASH_CACHE: dict = {}
_ANTIWASH_TTL = 60   # 1分钟缓存

# 拦截阈值
WASH_UNIQUE_ADDR_RATIO_MIN = 0.30   # 买入地址去重比 < 30% → 疑似刷量
WASH_TOP_WALLET_MAX        = 0.50   # 单钱包买入占比 > 50% → 一票否决
WASH_INTERVAL_CV_MAX       = 0.20   # tx 时间间隔变异系数 < 0.20 → 机器人特征（过于均匀）
WASH_MIN_TX_FOR_CHECK      = 5      # 少于 5 笔 tx 时跳过检测（样本不足）


def check_anti_wash(token_addr: str, token_symbol: str = "") -> tuple[bool, str]:
    """
    防刷量检测：通过 DexScreener pair 数据分析 5min 买入行为。
    返回 (ok, reason)
      ok=False → 疑似刷量，拦截
      ok=True  → 正常，放行
    """
    addr = token_addr.lower()
    sym  = token_symbol or addr[:8]
    now  = time.time()
    if addr in _ANTIWASH_CACHE and now - _ANTIWASH_CACHE[addr][1] < _ANTIWASH_TTL:
        return _ANTIWASH_CACHE[addr][0]

    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{token_addr}",
            timeout=8,
            proxies={"https": "socks5://REDACTED_PROXY_CREDENTIALS"},
        )
        pairs = r.json().get("pairs", [])
        if not pairs:
            return True, f"{sym} DexScreener 无数据，放行"

        # 取流动性最高的 pair
        p = sorted(pairs, key=lambda x: float((x.get("liquidity") or {}).get("usd", 0) or 0), reverse=True)[0]

        # 5min 买卖笔数
        txns5 = p.get("txns", {}).get("m5", {}) or {}
        buys5  = int(txns5.get("buys",  0) or 0)
        sells5 = int(txns5.get("sells", 0) or 0)

        if buys5 < WASH_MIN_TX_FOR_CHECK:
            result = (True, f"{sym} 5min 买入 {buys5} 笔不足 {WASH_MIN_TX_FOR_CHECK}，跳过防刷量")
            _ANTIWASH_CACHE[addr] = (result, now)
            return result

        # ── 检测一：买卖地址去重比（用 volume 做代理估算）──
        # DexScreener 不直接返回 unique_buyers，用 buyers 字段（部分 pair 有）
        buyers_unique = p.get("uniqueBuyers5m") or p.get("uniqueBuyers")
        if buyers_unique and buys5 > 0:
            ratio = float(buyers_unique) / buys5
            if ratio < WASH_UNIQUE_ADDR_RATIO_MIN:
                reason = f"{sym} 买入去重比 {ratio:.1%} < {WASH_UNIQUE_ADDR_RATIO_MIN:.0%}，疑似刷量"
                _ANTIWASH_CACHE[addr] = ((False, reason), now)
                return False, reason

        # ── 检测二：top holder 集中度（用 DexScreener holders 字段）──
        holders = p.get("holders", {}) or {}
        top1_pct = float(holders.get("top1Percentage") or 0)
        if top1_pct > WASH_TOP_WALLET_MAX * 100:
            reason = f"{sym} 单钱包持仓 {top1_pct:.0f}% > {WASH_TOP_WALLET_MAX:.0%}，疑似坐庄"
            _ANTIWASH_CACHE[addr] = ((False, reason), now)
            return False, reason

        # ── 检测三：买卖比例异常（买入笔数 > 卖出笔数 * 10 且 vol 极低）──
        vol5_usd = float((p.get("volume") or {}).get("m5", 0) or 0)
        if buys5 > 0 and vol5_usd > 0:
            avg_buy_usd = vol5_usd / buys5
            if avg_buy_usd < 1.0 and buys5 > 20:
                reason = f"{sym} 5min {buys5} 笔买入均值 ${avg_buy_usd:.2f}，超低额刷量特征"
                _ANTIWASH_CACHE[addr] = ((False, reason), now)
                return False, reason

        result = (True, f"{sym} 防刷量通过 buys5={buys5} vol5=${vol5_usd:.0f}")
        _ANTIWASH_CACHE[addr] = (result, now)
        return result

    except Exception as e:
        _log(f"[ANTIWASH] {sym} 检测异常({e})，放行")
        return True, f"防刷量检测异常，放行"


# ─────────────────────────────────────────────────────────────
# F10: DexScreener Boost 查询
# 项目方付费 boost 的 token → 有真实项目运营意愿，可加分
# ─────────────────────────────────────────────────────────────

_BOOST_CACHE: dict = {}        # addr_lower → (amount, ts)
_BOOST_CACHE_TTL = 120         # 2分钟刷新
_BOOST_SET: dict  = {}         # addr_lower → totalAmount（全局热更新）
_BOOST_SET_TS    = 0.0
_BOOST_SET_TTL   = 600         # 10分钟全量刷新一次（原 90s）— boost 变化极慢，节偦6x DexScreener 调用

BOOST_SCORE_THRESHOLD = 10     # totalAmount >= 10 才算有效 boost
BOOST_SCORE_HIGH      = 100    # totalAmount >= 100 为高 boost


def refresh_boost_set():
    """从 DexScreener token-boosts 拉取全量 Robinhood Chain boost 列表（后台定期调用）"""
    global _BOOST_SET, _BOOST_SET_TS
    try:
        r = requests.get(
            "https://api.dexscreener.com/token-boosts/latest/v1",
            timeout=8,
            proxies={"https": "socks5://REDACTED_PROXY_CREDENTIALS"},
        )
        items = r.json()
        if not isinstance(items, list):
            return
        new_set = {}
        for item in items:
            if item.get("chainId") == "robinhood":
                ca = item.get("tokenAddress", "").lower()
                amt = float(item.get("totalAmount", 0) or 0)
                if ca and amt >= BOOST_SCORE_THRESHOLD:
                    new_set[ca] = amt
        _BOOST_SET = new_set
        _BOOST_SET_TS = time.time()
        _log(f"[BOOST] 刷新完成，Robinhood Chain 有效 boost token: {len(new_set)} 个")
    except Exception as e:
        _log(f"[BOOST] 刷新失败: {e}")


def get_boost_score(token_addr: str) -> tuple[int, str]:
    """
    查询 token 的 DexScreener Boost 额度。
    返回 (score_bonus, reason)
      score_bonus: 0 / +1 / +2（加入评分体系用）
    """
    addr = token_addr.lower()
    if time.time() - _BOOST_SET_TS > _BOOST_SET_TTL:
        refresh_boost_set()
    amt = _BOOST_SET.get(addr, 0)
    if amt <= 0:
        return 0, "无 Boost"
    if amt >= BOOST_SCORE_HIGH:
        return 2, f"DexScreener 高额 Boost ${amt:.0f} 🚀"
    return 1, f"DexScreener Boost ${amt:.0f}"


def get_boost_amount(token_addr: str) -> float:
    """直接返回 boost 金额，0 表示无 boost"""
    if time.time() - _BOOST_SET_TS > _BOOST_SET_TTL:
        refresh_boost_set()
    return _BOOST_SET.get(token_addr.lower(), 0)


# ─────────────────────────────────────────────────────────────
# F11: Deployer 黑名单 — 历史 Rug 创建者一票否决
# 本地维护，可手动追加，进程内存级
# ─────────────────────────────────────────────────────────────

# 历史 Rug 创建者地址黑名单（全小写）
# 来源：启动时从 deployer_blacklist.json 加载 + 运行期动态追加
_DEPLOYER_BL_FILE = os.path.join(os.path.dirname(__file__), "deployer_blacklist.json")

def _load_deployer_blacklist() -> set:
    """Startup: load persisted deployer blacklist from JSON."""
    try:
        with open(_DEPLOYER_BL_FILE, "r") as f:
            data = json.load(f)
        return {addr.lower() for addr in data.get("blacklist", []) if addr}
    except Exception:
        return set()

RUG_DEPLOYER_BLACKLIST: set = _load_deployer_blacklist()

_DEPLOYER_CACHE: dict = {}   # token_addr → (deployer, ts)
_DEPLOYER_CACHE_TTL = 300    # 5分钟


def get_token_deployer(token_addr: str) -> str | None:
    """
    获取合约创建者地址。
    优先复用 F2 安全审计的缓存（_SEC_CACHE），零额外调用；
    缓存未命中时才独立请求 Clawby。
    """
    addr = token_addr.lower()
    now  = time.time()
    if addr in _DEPLOYER_CACHE and now - _DEPLOYER_CACHE[addr][1] < _DEPLOYER_CACHE_TTL:
        return _DEPLOYER_CACHE[addr][0]

    # ★ 首先尝试复用 F2 安全审计缓存（_SEC_CACHE）
    if addr in _SEC_CACHE and now - _SEC_CACHE[addr][1] < _SEC_CACHE_TTL:
        sec_data = _SEC_CACHE[addr][0]
        if isinstance(sec_data, dict):
            deployer = (sec_data.get("owner_address") or
                        sec_data.get("creator_address") or
                        sec_data.get("deployer") or "").lower()
            if deployer and deployer != "0x0000000000000000000000000000000000000000":
                _DEPLOYER_CACHE[addr] = (deployer, now)
                return deployer

    try:
        # 如果 F2 缓存没有命中，才进行独立调用
        r = requests.post(
            CLAWBY_RELAY,
            headers={"X-API-Key": CLAWBY_KEY, "Content-Type": "application/json"},
            json={"name": "dex_token_security",
                  "params": {"chain": "robinhood", "address": token_addr}},
            proxies={"https": "socks5://REDACTED_PROXY_CREDENTIALS",
                     "http":  "socks5://REDACTED_PROXY_CREDENTIALS"},
            timeout=8,
        )
        sec = r.json().get("data", {}).get("data") or r.json().get("data", {})
        deployer = (sec.get("owner_address") or sec.get("creator_address") or
                    sec.get("deployer") or "").lower()
        if deployer and deployer != "0x0000000000000000000000000000000000000000":
            _DEPLOYER_CACHE[addr] = (deployer, now)
            return deployer
    except Exception:
        pass

    _DEPLOYER_CACHE[addr] = (None, now)
    return None


# F11 辅助：查询 dev 完整信息（含 creator_address + twitter_create_token_count）
_DEV_INFO_CACHE: dict = {}   # token_addr → (dev_dict, ts)
_DEV_INFO_TTL  = 300

def get_token_dev_info(token_addr: str) -> dict:
    """
    通过 Clawby dex_token_info 获取 dev 字段：
    {'creator_address', 'twitter_create_token_count', 'creator_token_status', ...}
    失败时返回空 dict。
    """
    addr = token_addr.lower()
    now  = time.time()
    if addr in _DEV_INFO_CACHE and now - _DEV_INFO_CACHE[addr][1] < _DEV_INFO_TTL:
        return _DEV_INFO_CACHE[addr][0]
    try:
        r = requests.post(
            CLAWBY_RELAY,
            headers={"X-API-Key": CLAWBY_KEY, "Content-Type": "application/json"},
            json={"name": "dex_token_info",
                  "params": {"chain": "robinhood", "address": token_addr}},
            proxies={"https": "socks5://REDACTED_PROXY_CREDENTIALS",
                     "http":  "socks5://REDACTED_PROXY_CREDENTIALS"},
            timeout=8,
        )
        dev = r.json()["data"]["data"].get("dev", {})
        _DEV_INFO_CACHE[addr] = (dev, now)
        return dev
    except Exception:
        _DEV_INFO_CACHE[addr] = ({}, now)
        return {}


# F11 配置
DEPLOYER_FAIL_REJECT        = True    # 查不到 creator 时拒绝（而非放行）
DEPLOYER_MAX_TOKEN_COUNT    = 5       # creator 历史发币数超过此值视为可疑（twitter_create_token_count）
DEPLOYER_SUSPECT_LOG_ONLY   = True    # True=仅记录日志不拒绝；False=直接拒绝（可疑但不在黑名单）


def check_deployer_history(token_addr: str, token_symbol: str = "") -> tuple[bool, str]:
    """
    F11 — Deployer 历史检查（三步）：
    1. 从 Clawby dex_token_info 拿 creator_address（零额外费用）
    2. 与本地 RUG_DEPLOYER_BLACKLIST 比对 → 命中则一票否决
    3. 检查 twitter_create_token_count > DEPLOYER_MAX_TOKEN_COUNT → 可疑预警
    查不到 creator 时：DEPLOYER_FAIL_REJECT=True 则拒绝，否则放行
    返回 (ok, reason)
    """
    sym = token_symbol or token_addr[:8]

    # ── Step 1：获取 creator_address ────────────────────────────
    dev  = get_token_dev_info(token_addr)
    deployer = (
        dev.get("creator_address") or
        get_token_deployer(token_addr) or  # 兜底复用旧路径
        ""
    ).lower()

    if not deployer or deployer == "0x0000000000000000000000000000000000000000":
        if DEPLOYER_FAIL_REJECT:
            reason = f"{sym} ⚠️ 无法获取 creator，拒绝（DEPLOYER_FAIL_REJECT=True）"
            return False, reason
        return True, f"{sym} 无法获取 deployer，放行（DEPLOYER_FAIL_REJECT=False）"

    # ── Step 2：黑名单比对 ───────────────────────────────────────
    if deployer in RUG_DEPLOYER_BLACKLIST:
        reason = f"{sym} ❌ 创建者 {deployer[:12]}… 在 Rug 黑名单中，拒绝"
        return False, reason

    # ── Step 3：高频发币可疑预警 ──────────────────────────────────
    token_count = int(dev.get("twitter_create_token_count") or 0)
    if token_count > DEPLOYER_MAX_TOKEN_COUNT:
        msg = (f"{sym} ⚠️ 创建者 {deployer[:12]}… 历史发币 {token_count} 个"
               f" > {DEPLOYER_MAX_TOKEN_COUNT} 上限")
        if DEPLOYER_SUSPECT_LOG_ONLY:
            _log(f"[DEPLOYER SUSPECT] {msg}（仅预警，DEPLOYER_SUSPECT_LOG_ONLY=True）")
            # 不拒绝，但记录到缓存供后续追查
        else:
            return False, msg + "，拒绝"

    return True, f"{sym} creator={deployer[:12]}… 历史清白 ✅ (发币{token_count}次)"


def add_rug_deployer(deployer_addr: str, reason: str = ""):
    """
    将创建者地址加入黑名单：
    - 更新内存集合
    - 写回 deployer_blacklist.json
    - 推送 Jarvis 通知
    供 kol_exit_review 或手动调用。
    """
    addr = deployer_addr.lower()
    if addr in RUG_DEPLOYER_BLACKLIST:
        _log(f"[DEPLOYER-BL] {addr[:12]}… 已在黑名单中，跳过")
        return
    RUG_DEPLOYER_BLACKLIST.add(addr)
    # 写回 JSON
    try:
        try:
            with open(_DEPLOYER_BL_FILE, "r") as f:
                data = json.load(f)
        except Exception:
            data = {"blacklist": [], "last_updated": "", "source": "auto"}
        if addr not in data["blacklist"]:
            data["blacklist"].append(addr)
        data["last_updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with open(_DEPLOYER_BL_FILE, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        _log(f"[DEPLOYER-BL] 写入 JSON 失败: {e}")
    _log(f"[DEPLOYER-BL] 新增 Rug 创建者: {addr} | 原因: {reason}")
    # Jarvis 通知
    try:
        from config import JARVIS_TARGET
        msg = (f"🚨 Deployer 黑名单新增\n"
               f"地址: {addr}\n"
               f"原因: {reason or '未说明'}")
        subprocess.Popen(
            ["openclaw", "message", "send",
             "--channel", "jarvis", "-t", JARVIS_TARGET, "-m", msg],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════
# GeckoTerminal 新地址验证（策略W入场确认用）
# ══════════════════════════════════════════════════════════════
import time as _time_mod2

_POOL_ADDR_CACHE: dict = {}   # token_addr → (pool_addr, ts)
_POOL_CACHE_TTL  = 3600       # pool地址1h缓存

_PROXIES_GT = {"https": "socks5://REDACTED_PROXY_CREDENTIALS"}


def _get_pool_addr(token_addr: str) -> str:
    """通过 DexScreener 拿 pool 地址（带缓存）"""
    now = _time_mod2.time()
    cached = _POOL_ADDR_CACHE.get(token_addr.lower())
    if cached and now - cached[1] < _POOL_CACHE_TTL:
        return cached[0]
    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{token_addr}",
            timeout=6, proxies=_PROXIES_GT,
        )
        pairs = r.json().get("pairs") or []
        if not pairs:
            return ""
        pool = sorted(pairs, key=lambda x: float((x.get("liquidity") or {}).get("usd", 0) or 0), reverse=True)[0]
        pool_addr = pool.get("pairAddress", "")
        _POOL_ADDR_CACHE[token_addr.lower()] = (pool_addr, now)
        return pool_addr
    except Exception:
        return ""


def get_recent_new_buyers(
    token_addr: str,
    trigger_wallet: str,
    window_sec: int = 300,
    min_usd: float = 5_000,
) -> list:
    """
    查 GeckoTerminal 最近 window_sec 秒内的买入记录，
    返回满足条件的「新地址」列表：
      - 买入金额 >= min_usd
      - 地址不是 trigger_wallet（触发鲸鱼本人）
      - 地址在更早的历史里从未出现过（首次交易此 token）
    返回：[{"address": ..., "usd": ..., "ts": ...}, ...]
    """
    pool_addr = _get_pool_addr(token_addr)
    if not pool_addr:
        return []

    try:
        r = requests.get(
            f"https://api.geckoterminal.com/api/v2/networks/robinhood/pools/{pool_addr}/trades",
            timeout=8, proxies=_PROXIES_GT,
        )
        all_trades = r.json().get("data", [])
    except Exception:
        return []

    if not all_trades:
        return []

    now = _time_mod2.time()
    cutoff = now - window_sec

    # 分两组：窗口内 vs 更早的历史
    window_trades  = []
    history_addrs  = set()   # 窗口之前出现过的地址

    for t in all_trades:
        attr = t.get("attributes", {})
        # GeckoTerminal 时间戳格式：ISO8601
        try:
            import datetime
            ts_str = attr.get("block_timestamp", "")
            ts = datetime.datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
        except Exception:
            continue

        addr = (attr.get("tx_from_address") or "").lower()
        kind = attr.get("kind", "")
        vol  = float(attr.get("volume_in_usd") or 0)

        if ts >= cutoff:
            if kind == "buy":
                window_trades.append({"address": addr, "usd": vol, "ts": ts})
        else:
            # 更早的历史——记录所有出现过的地址（买卖都算）
            history_addrs.add(addr)

    # 过滤：金额达标 + 不是触发鲸鱼 + 是新地址
    trigger_lower = trigger_wallet.lower() if trigger_wallet else ""
    new_buyers = []
    seen = set()
    for b in window_trades:
        addr = b["address"]
        if b["usd"] < min_usd:
            continue
        if trigger_lower and addr == trigger_lower:
            continue
        if addr in history_addrs:
            continue   # 历史上出现过 = 老地址
        if addr in seen:
            continue   # 同一地址只计一次
        seen.add(addr)
        new_buyers.append(b)

    return new_buyers


# ─────────────────────────────────────────────────────────────
# F12: 持仓结构分析 — bundler/insider/sniper/wash/关联地址
# 借鉴 meme-radar analyzeWallets()，使用 Clawby stat 字段
# ─────────────────────────────────────────────────────────────

_HOLDER_RISK_CACHE: dict = {}    # addr_lower → (result_dict, ts)
_HOLDER_RISK_TTL   = 120         # 2分钟缓存

# 拒绝阈值（与 meme-radar 对齐）
Bundler_MAX_PCT     = 30.0    # 捆绑机器人占比 > 30% → 拒绝
INSIDER_MAX_PCT     = 30.0    # 内幕/老鼠仓占比 > 30% → 拒绝
SNIPER_MAX_PCT      = 30.0    # 狙击手持仓 > 30% → 拒绝
BOT_DEGEN_MAX_PCT   = 40.0    # 机器人交易者占比 > 40% → 拒绝
FRESH_WALLET_MAX_PCT = 50.0   # 新钱包占比 > 50% → 可疑（仅警告）
TOP10_MAX_PCT       = 60.0    # Top10 持仓 > 60% → 拒绝
DEV_HOLD_MAX_PCT    = 20.0    # Dev 持仓 > 20% → 拒绝

def get_holder_risk(token_addr: str) -> dict:
    """
    获取 token 持仓结构风险分析。
    返回 dict:
      bundler_pct, insider_pct, sniper_pct, bot_degen_pct,
      fresh_wallet_pct, top10_pct, dev_hold_pct,
      wallet_tags: {smart, renowned, sniper, bundler, rat, ...},
      verdict: 'pass' | 'reject' | 'warn',
      reasons: [str],
      raw: dict (原始 stat 数据)
    """
    addr = token_addr.lower()
    now  = time.time()
    if addr in _HOLDER_RISK_CACHE and now - _HOLDER_RISK_CACHE[addr][1] < _HOLDER_RISK_TTL:
        return _HOLDER_RISK_CACHE[addr][0]

    result = {
        'bundler_pct': 0, 'insider_pct': 0, 'sniper_pct': 0,
        'bot_degen_pct': 0, 'fresh_wallet_pct': 0, 'top10_pct': 0,
        'dev_hold_pct': 0, 'wallet_tags': {},
        'verdict': 'pass', 'reasons': [], 'raw': {},
    }

    try:
        r = requests.post(
            CLAWBY_RELAY,
            headers={"X-API-Key": CLAWBY_KEY, "Content-Type": "application/json"},
            json={"name": "dex_token_info",
                  "params": {"chain": "robinhood", "address": token_addr}},
            proxies={"https": "socks5://REDACTED_PROXY_CREDENTIALS",
                     "http":  "socks5://REDACTED_PROXY_CREDENTIALS"},
            timeout=8,
        )
        data = r.json().get("data", {}).get("data", {})
        stat = data.get("stat", {}) or {}
        tags = data.get("wallet_tags_stat", {}) or {}

        result['bundler_pct']       = float(stat.get("top_bundler_trader_percentage") or 0)
        result['insider_pct']       = float(stat.get("top_rat_trader_percentage") or 0)
        result['sniper_pct']        = float(stat.get("top70_sniper_hold_rate") or 0)
        result['bot_degen_pct']     = float(stat.get("top_bot_degen_percentage") or 0)
        result['fresh_wallet_pct']  = float(stat.get("fresh_wallet_rate") or 0)
        result['top10_pct']         = float(stat.get("top_10_holder_rate") or 0)
        result['dev_hold_pct']      = float(stat.get("dev_team_hold_rate") or stat.get("creator_hold_rate") or 0)
        result['wallet_tags']       = {
            'smart':     int(tags.get("smart_wallets") or 0),
            'renowned':  int(tags.get("renowned_wallets") or 0),
            'sniper':    int(tags.get("sniper_wallets") or 0),
            'bundler':   int(tags.get("bundler_wallets") or 0),
            'rat':       int(tags.get("rat_trader_wallets") or 0),
            'whale':     int(tags.get("whale_wallets") or 0),
            'fresh':     int(tags.get("fresh_wallets") or 0),
            'creator':   int(tags.get("creator_wallets") or 0),
        }
        result['raw'] = stat

        # 判定
        reasons = []
        if result['bundler_pct'] > Bundler_MAX_PCT:
            reasons.append(f"bundler占比{result['bundler_pct']:.0f}%>{Bundler_MAX_PCT:.0f}%")
        if result['insider_pct'] > INSIDER_MAX_PCT:
            reasons.append(f"insider占比{result['insider_pct']:.0f}%>{INSIDER_MAX_PCT:.0f}%")
        if result['sniper_pct'] > SNIPER_MAX_PCT:
            reasons.append(f"sniper持仓{result['sniper_pct']:.0f}%>{SNIPER_MAX_PCT:.0f}%")
        if result['bot_degen_pct'] > BOT_DEGEN_MAX_PCT:
            reasons.append(f"机器人占比{result['bot_degen_pct']:.0f}%>{BOT_DEGEN_MAX_PCT:.0f}%")
        if result['top10_pct'] > TOP10_MAX_PCT:
            reasons.append(f"Top10持仓{result['top10_pct']:.0f}%>{TOP10_MAX_PCT:.0f}%")
        if result['dev_hold_pct'] > DEV_HOLD_MAX_PCT:
            reasons.append(f"Dev持仓{result['dev_hold_pct']:.0f}%>{DEV_HOLD_MAX_PCT:.0f}%")

        if reasons:
            result['verdict'] = 'reject'
            result['reasons'] = reasons
        elif result['fresh_wallet_pct'] > FRESH_WALLET_MAX_PCT:
            result['verdict'] = 'warn'
            result['reasons'] = [f"新钱包占比{result['fresh_wallet_pct']:.0f}%偏高"]

    except Exception as e:
        _log(f"[HOLDER RISK] {token_addr[:12]}… 分析异常({e})，放行")
        result['verdict'] = 'pass'

    _HOLDER_RISK_CACHE[addr] = (result, now)
    return result


def check_holder_risk(token_addr: str, token_symbol: str = "") -> tuple[bool, str]:
    """
    F12 — 持仓结构安全检查。
    返回 (ok, reason)
    ok=False → 持仓结构异常，拒绝
    ok=True  → 通过（含 warn 级别）
    """
    risk = get_holder_risk(token_addr)
    sym = token_symbol or token_addr[:8]

    if risk['verdict'] == 'reject':
        reason = f"{sym} ❌ 持仓结构异常: {'; '.join(risk['reasons'])}"
        _log(f"[HOLDER RISK] {reason}")
        return False, reason
    elif risk['verdict'] == 'warn':
        reason = f"{sym} ⚠️ {risk['reasons'][0]}，仅警告"
        return True, reason
    else:
        smart = risk['wallet_tags'].get('smart', 0)
        return True, f"{sym} 持仓结构OK | bundler={risk['bundler_pct']:.0f}% insider={risk['insider_pct']:.0f}% smart={smart}"


# ============================================================
# F13. 单钱包集中度检查 (#3 筹码集中度)
# 跳过 LP 池地址，检查剩余最大单钱包持仓是否 > 5%
# ============================================================

_SINGLE_WALLET_MAX_PCT = 5.0   # 单个非 LP 钱包 > 5% → 危险标注
_SINGLE_WALLET_REJECT_PCT = 15.0  # 单个非 LP 钱包 > 15% → 拒绝

def check_single_wallet_concentration(token_addr: str, token_symbol: str = "") -> tuple[str, str]:
    """
    F13 — 单钱包集中度检查。
    返回 (verdict, detail)
    verdict: 'pass' | 'warn' | 'reject'
    """
    sym = token_symbol or token_addr[:8]
    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{token_addr}",
            timeout=8,
            proxies=_PROXIES,
        )
        pairs = r.json().get("pairs", [])
        if not pairs:
            return 'pass', f"{sym} 无 DexScreener 数据，跳过单钱包检查"

        p = sorted(pairs, key=lambda x: float((x.get("liquidity") or {}).get("usd", 0) or 0), reverse=True)[0]
        holders = p.get("holders", {}) or {}
        top1_pct = float(holders.get("top1Percentage") or 0)
        top10_pct_val = float(holders.get("top10Percentage") or 0)

        # DexScreener 的 top1Percentage 通常包含 LP 池
        # 如果 top1 很高，可能是 LP（正常），也可能是庄家（危险）
        # 用 Clawby stat 的 private_vault_hold_rate 做交叉验证
        risk = get_holder_risk(token_addr)
        lp_pct = float(risk['raw'].get("private_vault_hold_rate") or 0) * 100  # LP 持仓比例

        # 如果 top1 > 5% 且不是 LP，标记危险
        if top1_pct > 0:
            # 估算非 LP 的最大持仓：如果 top1 接近 lp_pct，说明 top1 是 LP
            non_lp_top = top1_pct - lp_pct if lp_pct > 0 else top1_pct
            if non_lp_top > _SINGLE_WALLET_REJECT_PCT:
                return 'reject', f"{sym} 单钱包持仓 {non_lp_top:.1f}% > {_SINGLE_WALLET_REJECT_PCT:.0f}%，高度控盘风险"
            elif non_lp_top > _SINGLE_WALLET_MAX_PCT:
                return 'warn', f"{sym} 单钱包持仓 {non_lp_top:.1f}% > {_SINGLE_WALLET_MAX_PCT:.0f}%，集中度偏高"

        return 'pass', f"{sym} 单钱包集中度OK"
    except Exception as e:
        _log(f"[SINGLE WALLET] {sym} 检查异常({e})，放行")
        return 'pass', f"{sym} 单钱包检查异常，放行"


# ============================================================
# F14. 交易量/市值比检查 (#8 交易量 vs 市值)
# vol.h24 / marketCap 比率过低 → 不活跃，危险
# ============================================================

_VOL_MC_MIN_RATIO = 0.1     # vol/MC < 0.1 → 不活跃，标注危险
_VOL_MC_WARN_RATIO = 0.3    # vol/MC < 0.3 → 偏低，警告
_VOL_MC_LARGE_MC = 50_000_000  # MC > 50M 时放宽阈值（大市值币量比天然低）
_VOL_MC_LARGE_MIN_RATIO = 0.02  # 大市值币 vol/MC < 0.02 才拒绝
_VOL_MC_LARGE_WARN_RATIO = 0.05  # 大市值币 vol/MC < 0.05 才警告

def check_vol_mc_ratio(token_addr: str, token_symbol: str = "") -> tuple[str, str]:
    """
    F14 — 交易量/市值比检查。
    返回 (verdict, detail)
    verdict: 'pass' | 'warn' | 'reject'
    """
    sym = token_symbol or token_addr[:8]
    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{token_addr}",
            timeout=8,
            proxies=_PROXIES,
        )
        pairs = r.json().get("pairs", [])
        if not pairs:
            return 'pass', f"{sym} 无 DexScreener 数据，跳过量比检查"

        p = sorted(pairs, key=lambda x: float((x.get("liquidity") or {}).get("usd", 0) or 0), reverse=True)[0]
        vol_h24 = float((p.get("volume") or {}).get("h24", 0) or 0)
        mc = float(p.get("marketCap") or 0)

        if mc <= 0 or vol_h24 <= 0:
            # 新币可能没有 h24 数据，不拦截
            return 'pass', f"{sym} vol/MC 数据不足 (vol={vol_h24:.0f} mc={mc:.0f})"

        ratio = vol_h24 / mc

        # 大市值币放宽阈值
        if mc > _VOL_MC_LARGE_MC:
            _min_r = _VOL_MC_LARGE_MIN_RATIO
            _warn_r = _VOL_MC_LARGE_WARN_RATIO
            _size_note = "(大市值放宽)"
        else:
            _min_r = _VOL_MC_MIN_RATIO
            _warn_r = _VOL_MC_WARN_RATIO
            _size_note = ""

        if ratio < _min_r:
            return 'reject', f"{sym} 量比 {ratio:.2f} 极低 {size_note} (vol ${vol_h24:,.0f} / MC ${mc:,.0f})，不活跃"
        elif ratio < _warn_r:
            return 'warn', f"{sym} 量比 {ratio:.2f} 偏低 {_size_note} (vol ${vol_h24:,.0f} / MC ${mc:,.0f})"

        return 'pass', f"{sym} 量比 {ratio:.2f} OK"
    except Exception as e:
        _log(f"[VOL MC] {sym} 检查异常({e})，放行")
        return 'pass', f"{sym} 量比检查异常，放行"


# ============================================================
# F15. 资金到账时间检查 (#5 同时到账 = 机器人刷量)
# 通过 Clawby stat 的 fresh_wallet_rate 和 bundler 组合判断
# ============================================================

def check_fund_arrival_pattern(token_addr: str, token_symbol: str = "") -> tuple[str, str]:
    """
    F15 — 资金到账模式检查。
    新钱包占比高 + bundler 占比高 = 同时到账刷量特征。
    返回 (verdict, detail)
    verdict: 'pass' | 'warn' | 'reject'
    """
    sym = token_symbol or token_addr[:8]
    try:
        risk = get_holder_risk(token_addr)
        fresh_pct = risk['fresh_wallet_pct']
        bundler_pct = risk['bundler_pct']
        bot_pct = risk['bot_degen_pct']

        # 组合判断：新钱包 > 50% 且 bundler > 20% → 高度疑似同时到账刷量
        if fresh_pct > 50 and bundler_pct > 20:
            return 'reject', f"{sym} 新钱包{fresh_pct:.0f}%+bundler{bundler_pct:.0f}%，疑似同时到账刷量"
        # 新钱包 > 40% 且 bundler > 10% → 警告
        elif fresh_pct > 40 and bundler_pct > 10:
            return 'warn', f"{sym} 新钱包{fresh_pct:.0f}%+bundler{bundler_pct:.0f}%，资金到账模式可疑"
        # 机器人占比极高 + 新钱包高
        elif fresh_pct > 50 and bot_pct > 60:
            return 'warn', f"{sym} 新钱包{fresh_pct:.0f}%+机器人{bot_pct:.0f}%，刷量特征明显"

        return 'pass', f"{sym} 资金到账模式OK (新{fresh_pct:.0f}% bundler{bundler_pct:.0f}%)"
    except Exception as e:
        _log(f"[FUND ARRIVAL] {sym} 检查异常({e})，放行")
        return 'pass', f"{sym} 到账检查异常，放行"


def run_safety_extensions(token_addr: str, token_symbol: str = "") -> dict:
    """
    运行扩展安全检查 (#3 #5 #8)，返回综合结果。
    不拦截信号，但标注危险级别。
    返回 dict:
      verdict: 'pass' | 'warn' | 'reject'
      warnings: [str]  — 警告列表
      rejects: [str]   — 拒绝理由列表
      details: [str]   — 所有检查详情
    """
    results = []
    warnings = []
    rejects = []
    details = []

    # F13: 单钱包集中度
    v1, d1 = check_single_wallet_concentration(token_addr, token_symbol)
    results.append(("single_wallet", v1, d1))
    details.append(d1)
    if v1 == 'reject': rejects.append(d1)
    elif v1 == 'warn': warnings.append(d1)

    # F14: 交易量/市值比
    v2, d2 = check_vol_mc_ratio(token_addr, token_symbol)
    results.append(("vol_mc", v2, d2))
    details.append(d2)
    if v2 == 'reject': rejects.append(d2)
    elif v2 == 'warn': warnings.append(d2)

    # F15: 资金到账模式
    v3, d3 = check_fund_arrival_pattern(token_addr, token_symbol)
    results.append(("fund_arrival", v3, d3))
    details.append(d3)
    if v3 == 'reject': rejects.append(d3)
    elif v3 == 'warn': warnings.append(d3)

    if rejects:
        verdict = 'reject'
    elif warnings:
        verdict = 'warn'
    else:
        verdict = 'pass'

    return {
        'verdict': verdict,
        'warnings': warnings,
        'rejects': rejects,
        'details': details,
    }
