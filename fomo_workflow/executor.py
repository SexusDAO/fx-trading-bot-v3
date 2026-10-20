# ============================================================
# executor.py — baw swap 执行层 v2（修复卖出失败问题）
# ============================================================
# 旧工作流卖出失败的根本原因（已全部修复）：
#
# 【问题1】滑点太低（0.05 = 5%）
#   土狗流动性极薄，5% 滑点几乎必然失败。
#   → 卖出默认 20%，失败后自动递增重试（最高 49%）
#
# 【问题2】精度问题导致 INVALID_AMOUNT
#   sell_qty = balance * pct 产生极长小数（如 506.075012345678）
#   baw 对某些 token 有精度限制，非整数会报错。
#   → 余额 ≥ 1 时用整数，< 1 时保留 8 位小数
#
# 【问题3】一次全量卖出冲击流动性
#   持仓价值 > 流动性 5% 时一笔卖出会严重滑点或直接失败。
#   → 分批卖出：持仓 > 流动性 5% 时自动分 2~3 批
#
# 【问题4】卖出后残留粉尘仍记为"未清仓"
#   → 卖出后检查剩余余额，< $0.5 自动标记为已清仓
#
# 【问题5】gas 费不够导致交易 revert
#   → gas_manager 已处理，executor 只做最终兜底检查
# ============================================================

import subprocess, json, time, os
from config import CHAIN_ID, USDG_ADDRESS, BUY_TOKEN, BUY_TOKEN_SYMBOL, GAS_RESERVE_USD, LOG_FILE
from config import PRICE_DRIFT_MAX_PCT, PRICE_DRIFT_ENABLED, QUOTE_DRIFT_MAX_PCT
from config import (
    BASE_POSITION, MAX_POSITION,
    BONUS_BOOST_STRONG, BONUS_CLAWBY_CONFIRM, BONUS_MULTI_KOL,
    BONUS_WHALE_ALIGN, BONUS_HIGH_WINRATE,
    ENTRY_PROTECT_MIN_RPC, ENTRY_PROTECT_MIN_FOMO,
    NEW_PAIR_DELAY_SEC,
)

ETH_ADDRESS = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"

# ── 滑点配置 ──────────────────────────────────────────────
# 买入滑点改为动态（按流动性），这里保留兜底默认值
SLIPPAGE_BUY_NORMAL  = 0.15   # 买入默认（无流动性数据时使用）
SLIPPAGE_BUY_MAX     = 0.49   # 买入最高 49%（重试递增）
SLIPPAGE_SELL_BASE   = 0.20   # 卖出起始 20%
SLIPPAGE_SELL_MAX    = 0.49   # 卖出最高 49%（每次重试 +5%）
SLIPPAGE_SELL_STEP   = 0.05   # 每次重试递增
SELL_RETRY_COUNT     = 4      # 最多重试次数
DUST_USD_THRESHOLD   = 0.50   # 粉尘阈値

# ── 动态买入滑点（按流动性档位）─────────────────────────
# 流动性越低，价格冲击越大，需要更高容忍度
SLIPPAGE_BUY_LIQ_HIGH   = 0.10   # 流动性 > $500k
SLIPPAGE_BUY_LIQ_MID    = 0.15   # 流动性 $100k~$500k
SLIPPAGE_BUY_LIQ_LOW    = 0.25   # 流动性 < $100k
SLIPPAGE_BUY_LIQ_TINY   = 0.35   # 流动性 < $20k（极薄）

# ── Gas 最小值保护 ────────────────────────────────────────
# opBNB 上每笔 swap 约 0.00014 ETH gas
# 如果本次交易 ETH 量 < gas_est × GAS_MIN_RATIO，跳过（纯亏gas）
GAS_EST_ETH      = 0.00020   # 保守估算每笔 gas（ETH）
GAS_MIN_RATIO    = 5         # 交易量至少是 gas 的 5 倍才值得做


# ══════════════════════════════════════════════════════════════
# 全局 Gas 档位管理器
# 逻辑：
#   - 默认使用 LOW（标准，最省gas）
#   - 任意订单等待 >= GAS_ESCALATE_TIMEOUT_S 仍在 pending
#     → 全局切换到 MEDIUM（快速）
#   - MEDIUM 模式持续 GAS_FAST_DURATION_S（30min）后自动回落 LOW
#   - 所有新订单读取 _current_gas_level() 决定本次用哪档
# ══════════════════════════════════════════════════════════════
import time as _time_mod

_GAS_ESCALATED_UNTIL: float = 0.0          # 升级结束时间戳（0=未升级）
GAS_ESCALATE_TIMEOUT_S  = 180              # 180s pending → 升级到 MEDIUM
GAS_FAST_DURATION_S     = 30 * 60          # 升级持续 30min 后回 LOW


def _current_gas_level() -> str:
    """返回当前应使用的 gas 档位字符串（LOW / MEDIUM）"""
    if _time_mod.time() < _GAS_ESCALATED_UNTIL:
        return "MEDIUM"
    return "LOW"


def _escalate_gas(token_symbol: str = ""):
    """订单 pending 超时 → 全局升级到 MEDIUM，持续 30min"""
    global _GAS_ESCALATED_UNTIL
    if _time_mod.time() < _GAS_ESCALATED_UNTIL:
        return  # 已经在 MEDIUM 档，不重复记录
    _GAS_ESCALATED_UNTIL = _time_mod.time() + GAS_FAST_DURATION_S
    resume_str = _time_mod.strftime("%H:%M UTC", _time_mod.gmtime(_GAS_ESCALATED_UNTIL))
    _log(f"[GAS ESCALATE] {token_symbol} 订单pending超时，全局切换 LOW→MEDIUM，"
         f"持续30min至 {resume_str}")


def _check_gas_restored():
    """每次提交新订单前调用，若升级已过期则打印恢复日志（只打印一次）"""
    global _GAS_ESCALATED_UNTIL
    if _GAS_ESCALATED_UNTIL > 0 and _time_mod.time() >= _GAS_ESCALATED_UNTIL:
        _GAS_ESCALATED_UNTIL = 0.0
        _log("[GAS RESTORE] 30min已过，全局gas档位恢复为 LOW（标准）")



def calc_position_size(kol_grade: str, signals: dict) -> float:
    """
    信号积分叠加制仓位计算（替代原乘法加乘 ×1.2 ×1.5）。

    kol_grade: "S"/"A"/"B"/"C"
    signals 字典键（均为 bool）：
      boost_strong    — DexScreener Boost ≥ $100
      clawby_confirm  — Clawby ≥ 2 买家双源确认
      multi_kol       — ≥ 3 个 KOL 同时买同一 token
      whale_align     — 同时出现鲸鱼买入信号
      high_winrate    — 触发 KOL 30d 胜率 ≥ 70%

    返回最终仓位金额（USD）。
    DCA 补仓路径不调用此函数，继续使用固定 $DCA_SIZE_USD。
    """
    grade = kol_grade if kol_grade in BASE_POSITION else "C"
    base  = BASE_POSITION[grade]
    bonus = 0
    details = []

    if signals.get("boost_strong"):
        bonus += BONUS_BOOST_STRONG
        details.append(f"+${BONUS_BOOST_STRONG} Boost强信号")

    if signals.get("clawby_confirm"):
        bonus += BONUS_CLAWBY_CONFIRM
        details.append(f"+${BONUS_CLAWBY_CONFIRM} Clawby双源确认")

    if signals.get("multi_kol"):
        bonus += BONUS_MULTI_KOL
        details.append(f"+${BONUS_MULTI_KOL} 多KOL共振")

    if signals.get("whale_align"):
        bonus += BONUS_WHALE_ALIGN
        details.append(f"+${BONUS_WHALE_ALIGN} 鲸鱼同向")

    if signals.get("high_winrate"):
        bonus += BONUS_HIGH_WINRATE
        details.append(f"+${BONUS_HIGH_WINRATE} KOL高胜率")

    cap   = MAX_POSITION[grade]
    final = min(base + bonus, cap)
    detail_str = "  ".join(details) if details else "无加分项"
    _log(f"[POSITION CALC] {grade}级 基础${base} | {detail_str} | 合计${base+bonus} → 上限${cap} → 最终${final:.0f}")
    return float(final)


# ETH 价格缓存（防止接口挂了导致 buy_qty=0 静默失败）
_ETH_PRICE_CACHE: float = 0.0
_ETH_PRICE_CACHE_TS: float = 0.0
_ETH_PRICE_CACHE_TTL: float = 30.0   # 30s TTL
ETH_PRICE_MIN = 100.0                # 价格合理区间（异常值就是接口广忘/返回不合理数据）
ETH_PRICE_MAX = 100_000.0


def _get_eth_price() -> float:
    """
    获取 ETH/USD 实时价格。
    主源：Binance 公开 API（不依赖 baw 链上价格，RH 链 ETH != 主网价）
    备用：DexScreener WETH/USDC
    """
    global _ETH_PRICE_CACHE, _ETH_PRICE_CACHE_TS
    now = time.time()
    if now - _ETH_PRICE_CACHE_TS < _ETH_PRICE_CACHE_TTL and _ETH_PRICE_CACHE > 0:
        return _ETH_PRICE_CACHE

    price = 0.0
    _PROXY = {"https": "socks5://REDACTED_PROXY_CREDENTIALS",
              "http":  "socks5://REDACTED_PROXY_CREDENTIALS"}

    # 主源：Binance 公开 API（无需认证，直接取主网 ETH 价格）
    try:
        import requests as _req
        r = _req.get("https://api.binance.com/api/v3/ticker/price?symbol=ETHUSDT",
                     timeout=5, proxies=_PROXY)
        price = float(r.json().get("price", 0) or 0)
    except Exception:
        pass

    # 备用源：DexScreener WETH 主网
    if not (ETH_PRICE_MIN <= price <= ETH_PRICE_MAX):
        try:
            import requests as _req
            r = _req.get(
                "https://api.dexscreener.com/latest/dex/tokens/"
                "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
                timeout=5, proxies=_PROXY)
            pairs = r.json().get("pairs", [])
            stable_pairs = [p for p in pairs if any(
                s in p.get("quoteToken", {}).get("symbol", "").upper()
                for s in ("USDC", "USDT", "DAI"))]
            best = stable_pairs[0] if stable_pairs else (pairs[0] if pairs else None)
            if best:
                price = float(best.get("priceUsd", 0) or 0)
        except Exception:
            pass

    if ETH_PRICE_MIN <= price <= ETH_PRICE_MAX:
        _ETH_PRICE_CACHE    = price
        _ETH_PRICE_CACHE_TS = now
        _log(f"[ETH PRICE] ${price:.2f}")
        return price

    if _ETH_PRICE_CACHE > 0:
        _log(f"[ETH PRICE] 双源失败({price:.2f})，使用缓存 ${_ETH_PRICE_CACHE:.2f}")
        return _ETH_PRICE_CACHE

    _log("[ETH PRICE] 警告: 双源全失且无缓存，返回 0")
    return 0.0
LOCK_FILE = "/tmp/fomo_workflow.lock"

def _verify_process_lock(strict: bool = True) -> bool:
    """
    验证当前进程是否允许执行交易。
    strict=True  (买入用): PID 必须匹配，同进程直接放行（无需 flock 二次验证）。
    strict=False (卖出用): 任何活跃主进程存在即放行，避免重启窗口期误拦截。
    """
    import fcntl
    try:
        with open(LOCK_FILE, 'r') as fd:
            content = fd.read().strip()

        if not content:
            _log("[LOCK DEBUG] 锁文件为空，拒绝")
            return False

        lock_pid = int(content)
        my_pid = os.getpid()

        if strict:
            # 买入：PID 必须匹配
            if lock_pid != my_pid:
                _log(f"[LOCK MISMATCH] 买入拦截: 当前 PID={my_pid} != 锁 PID={lock_pid}")
                return False
            # PID 匹配即为同进程，直接放行（flock 同进程验证存在竞争问题）
            return True

        # 非严格模式（卖出）：验证锁是否被任何进程持有
        fd2 = open(LOCK_FILE, 'r')
        try:
            fcntl.flock(fd2, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # 能拿到 → 锁已释放（主进程已退出）
            fcntl.flock(fd2, fcntl.LOCK_UN)
            fd2.close()
            # 用 kill(0) 兜底：进程还活着就放行
            try:
                os.kill(lock_pid, 0)
                _log(f"[LOCK FALLBACK] 卖出放行: PID={lock_pid} 仍活着")
                return True
            except (ProcessLookupError, PermissionError):
                _log(f"[LOCK FALLBACK] PID={lock_pid} 不存在，重启窗口期，卖出放行")
                return True
        except BlockingIOError:
            fd2.close()
            return True   # flock 正常持有，放行
    except (FileNotFoundError, ValueError) as e:
        _log(f"[LOCK DEBUG] 文件异常: {e}")
        return False
    except Exception as e:
        _log(f"[LOCK DEBUG] 未知异常: {e}")
        return False


def get_token_age_sec(token_addr: str) -> float | None:
    """Public alias for _get_pair_age_seconds，供 ws_monitor 调用"""
    return _get_pair_age_seconds(token_addr)


def _get_pair_age_seconds(token_addr: str) -> float | None:
    """
    查询 DexScreener 获取 token 的最早 pair 创建时间，
    返回该 pair 至今的存在秒数。如果无法获取则返回 None。
    """
    try:
        import requests as _req
        r = _req.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{token_addr}",
            timeout=5,
            proxies={"https": "socks5://REDACTED_PROXY_CREDENTIALS"},
        )
        pairs = r.json().get("pairs", []) or []
        if not pairs:
            return None
        # 取创建时间最早的 pair
        created_ats = [
            p.get("pairCreatedAt") for p in pairs
            if p.get("pairCreatedAt")
        ]
        if not created_ats:
            return None
        earliest_ms = min(created_ats)
        age_sec = time.time() - earliest_ms / 1000
        return max(0.0, age_sec)
    except Exception:
        return None


def _log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _safe_qty(qty: float) -> str:
    """
    将数量转换为 baw 可接受的格式：
    - 整数 token（≥ 1）→ 取整，避免精度拒绝
    - 小数 token（< 1）→ 保留 8 位小数
    """
    if qty >= 1:
        # 土狗通常是整数，截断小数
        return str(int(qty))
    return str(round(qty, 8))


def _get_dynamic_buy_slippage(token_addr: str) -> float:
    """
    根据流动性动态返回买入滑点：
      > $500k  → 10%
      $100k~$500k → 15%
      $20k~$100k  → 25%
      < $20k      → 35%
    无数据时回退到默认 15%
    """
    liq = _get_liquidity(token_addr)
    if liq <= 0:
        return SLIPPAGE_BUY_NORMAL
    if liq >= 500_000:
        slippage = SLIPPAGE_BUY_LIQ_HIGH
    elif liq >= 100_000:
        slippage = SLIPPAGE_BUY_LIQ_MID
    elif liq >= 20_000:
        slippage = SLIPPAGE_BUY_LIQ_LOW
    else:
        slippage = SLIPPAGE_BUY_LIQ_TINY
    _log(f"[SLIPPAGE] 流动性=${liq:,.0f} → 买入滑点={slippage:.0%}")
    return slippage


def _check_gas_vs_trade(trade_eth: float) -> bool:
    """
    Gas 最小値保护：如果本次交易金额 < gas_est × GAS_MIN_RATIO，返回 False。
    避免“$0.05 的单子花了 $0.51 Gas”这种败局。
    """
    min_trade = GAS_EST_ETH * GAS_MIN_RATIO
    if trade_eth < min_trade:
        _log(f"[GAS SKIP] 交易量 {trade_eth:.6f} ETH < Gas估算 {GAS_EST_ETH:.5f}×{GAS_MIN_RATIO} = {min_trade:.5f} ETH，跳过")
        return False
    return True


def _get_quote(from_token: str, to_token: str, amount: float) -> dict | None:
    """获取 swap 报价，返回 dict 或 None"""
    cmd = [
        "baw", "market-order", "quote",
        "--binanceChainId", CHAIN_ID,
        "--fromTokenQty", str(amount),
        "--fromToken", from_token,
        "--toToken", to_token,
        "--json",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        d = json.loads(r.stdout)
        if d.get("success"):
            return d["data"]
    except Exception as e:
        _log(f"[QUOTE ERROR] {e}")
    return None


def buy_token(token_addr: str, token_symbol: str,
              size_usd: float, slippage: float = -1.0,
              snapshot_price: float | None = None,
              entry_source: str = "fomo") -> dict | None:
    """买入 token_addr，等待链上确认后才返回
    slippage=-1 表示自动按流动性动态设置。
    snapshot_price: 信号触发时的 token 价格快照，用于报价偏差检查。
    entry_source: "rpc" = WebSocket 零延迟触发；"fomo" = fomoapi KOL 信号触发。
    """
    # 新币上线 < 2min 延迟买入
    pair_age_sec = _get_pair_age_seconds(token_addr)
    if pair_age_sec is not None and pair_age_sec < 120:
        _log(f"[NEW PAIR DELAY] {token_symbol} 上线仅 {pair_age_sec:.0f}s，"
             f"等待 {NEW_PAIR_DELAY_SEC}s 再买入")
        import time as _time
        _time.sleep(NEW_PAIR_DELAY_SEC)
    _log(f"[BUY] {token_symbol} | {size_usd}U ETH → {token_addr}")
    if not _verify_process_lock(strict=True):   # 买入：严格 PID 校验
        _log(f"[BUY ABORT] {token_symbol} 当前进程未持有锁")
        return None

    # ─ 动态滑点
    if slippage < 0:
        slippage = _get_dynamic_buy_slippage(token_addr)

    # ─ Gas 最小值保护 + ETH 价格异常硬拒绝
    eth_price = _get_eth_price()
    if eth_price <= 0:
        _log(f"[BUY ABORT] {token_symbol} ETH 价格获取失败，暂停买入")
        return None
    if not (ETH_PRICE_MIN <= eth_price <= ETH_PRICE_MAX):
        _log(f"[BUY ABORT] {token_symbol} ETH 价格异常 ${eth_price:.2f}，暂停买入")
        return None
    trade_eth = size_usd / eth_price
    if not _check_gas_vs_trade(trade_eth):
        _log(f"[BUY SKIP] {token_symbol} 交易金额太小，不如 gas，放弃")
        return None

    current_slippage = slippage
    for attempt in range(1, SELL_RETRY_COUNT + 1):
        eth_price = _get_eth_price()
        buy_qty   = round(size_usd / eth_price, 8) if eth_price > 0 else 0
        if buy_qty <= 0:
            _log(f"[BUY ERROR] {token_symbol} 无法获取 ETH 价格")
            return None

        # ★ 报价偏差检查：执行前再次查实时价 vs 快照价
        # 首次尝试才做，重试时跳过（已确定要买，只是滑点问题）
        if attempt == 1 and snapshot_price is not None and snapshot_price > 0 and PRICE_DRIFT_ENABLED:
            live_price = _get_token_price(token_addr)
            if live_price and live_price > 0:
                quote_drift = abs(live_price - snapshot_price) / snapshot_price
                if quote_drift > QUOTE_DRIFT_MAX_PCT:
                    direction = "↑" if live_price > snapshot_price else "↓"
                    _log(f"[QUOTE DRIFT] {token_symbol} 买入前实时价偏差 {quote_drift:.1%}{direction}"
                         f" (实时={live_price:.8f} vs 快照={snapshot_price:.8f})，超过 {QUOTE_DRIFT_MAX_PCT:.0%}，放弃")
                    return None
                _log(f"[QUOTE OK] {token_symbol} 买入前实时价偏差 {quote_drift:.1%}，在允许范围内")

        _check_gas_restored()
        _gas_lvl = _current_gas_level()
        cmd = [
            "baw", "market-order", "swap",
            "--binanceChainId", CHAIN_ID,
            "--fromTokenQty", str(buy_qty),
            "--fromToken", BUY_TOKEN,
            "--toToken", token_addr,
            "--slippage", str(round(current_slippage, 2)),
            "--gasLevel", _gas_lvl,
            "--json",
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            d = json.loads(r.stdout)
            if d.get("success"):
                order_id = d.get("data", {}).get("orderId", "")
                _log(f"[BUY SUBMITTED] {token_symbol} 第{attempt}次 orderId={order_id} slippage={current_slippage:.0%} gasLevel={_gas_lvl} | 等待链上确认...")
                confirmed = _wait_order_confirmed(order_id, token_symbol, timeout=480)
                if confirmed and confirmed["status"] == "FINISHED":
                    token_out = float(confirmed.get("toTokenActualQty") or 0)
                    tx = confirmed.get("txHash", "?")
                    _log(f"[BUY CONFIRMED] {token_symbol} ✅ tx={tx[:20]} 获得={token_out:.4f} slippage={current_slippage:.0%}")
                    return confirmed
                elif confirmed and confirmed["status"] == "FAILED":
                    tx = confirmed.get("txHash", "?")
                    _log(f"[BUY CHAIN FAILED] {token_symbol} ❌ tx={tx[:20]} 链上买入失败 slippage={current_slippage:.0%}，递增滑点重试")
                else:
                    # 超时：继续等原订单，不立刻发新单（防止重复成交）
                    _escalate_gas(token_symbol)
                    _log(f"[BUY TIMEOUT] {token_symbol} orderId={order_id} 480s未确认→升级gas，继续等+300s...")
                    confirmed2 = _wait_order_confirmed(order_id, token_symbol, timeout=300)
                    if confirmed2 and confirmed2["status"] == "FINISHED":
                        token_out = float(confirmed2.get("toTokenActualQty") or 0)
                        tx = confirmed2.get("txHash", "?")
                        _log(f"[BUY CONFIRMED] {token_symbol} ✅ 第二轮确认 tx={tx[:20]} 获得={token_out:.4f}")
                        return confirmed2
                    elif confirmed2 and confirmed2["status"] == "FAILED":
                        tx = confirmed2.get("txHash", "?")
                        _log(f"[BUY CHAIN FAILED] {token_symbol} ❌ 链上明确FAILED tx={tx[:20]}，递增滑点重试")
                    else:
                        _log(f"[BUY TIMEOUT2] {token_symbol} 两轮共780s仍未确认，认定失败，递增滑点重试")
            else:
                _log(f"[BUY FAIL #{attempt}] {token_symbol} slippage={current_slippage:.0%} | {str(d)[:120]}")
            current_slippage = min(current_slippage + 0.10, SLIPPAGE_BUY_MAX)
            if attempt < SELL_RETRY_COUNT:
                time.sleep(2)
        except Exception as e:
            _log(f"[BUY ERROR #{attempt}] {token_symbol} {e}")
            time.sleep(2)
    liq_info = ""
    try:
        from token_filter import _get_liq_usd
        liq = _get_liq_usd(token_addr)
        liq_info = f" liq=${liq:,.0f}" if liq else ""
    except Exception:
        pass
    _log(f"[BUY FAILED ALL] {token_symbol} 全部 {SELL_RETRY_COUNT} 次重试均失败 "
         f"max_slippage={current_slippage:.0%}{liq_info} — 建议复盘是否提高低市值滑点")
    return None


def sell_token(
    token_addr: str,
    token_symbol: str,
    sell_pct: float,          # 0~1，卖出比例
    current_balance: float,   # 链上实际 token 余额
    slippage: float = SLIPPAGE_SELL_BASE,
) -> dict | None:
    """
    卖出指定比例，自动处理：
    1. 精度问题（整数 token 取整）
    2. 滑点递增重试（最多 SELL_RETRY_COUNT 次）
    3. 大额分批（持仓 > 流动性 5% 时）
    """
    if current_balance <= 0:
        _log(f"[SELL SKIP] {token_symbol} 余额为 0")
        return None

    # 回收价值检查：如果当前持仓总价値 < $1，连 gas 都不尚，直接放弃
    token_price_now = _get_token_price(token_addr)
    holding_val_now = current_balance * sell_pct * token_price_now if token_price_now > 0 else 0
    if holding_val_now < 1.0:
        _log(f"[SELL SKIP] {token_symbol} 预计回收价值 ${holding_val_now:.3f} < $1，不如功覆薄 gas，放弃卖出")
        return None

    # ★ Gas 最小值保护：回收 ETH < gas_est×倍数 则不划算
    eth_price_now = _get_eth_price()
    if eth_price_now > 0 and holding_val_now > 0:
        trade_eth_est = holding_val_now / eth_price_now
        if not _check_gas_vs_trade(trade_eth_est):
            _log(f"[SELL SKIP] {token_symbol} 卖出金额 ${holding_val_now:.3f} < Gas×{GAS_MIN_RATIO}，放弃")
            return None

    # 计算本次卖出数量
    raw_qty = current_balance * sell_pct
    if raw_qty <= 0:
        _log(f"[SELL SKIP] {token_symbol} 计算卖出量为 0")
        return None

    # 检查是否需要分批（查询流动性）
    liq_usd = _get_liquidity(token_addr, token_symbol)
    token_price = _get_token_price(token_addr)
    holding_val = raw_qty * token_price if token_price > 0 else 0

    if liq_usd > 0 and holding_val > liq_usd * 0.05:
        # 分批卖出
        return _batch_sell(token_addr, token_symbol, raw_qty, liq_usd, slippage)
    else:
        # 单笔卖出（滑点递增重试）
        return _single_sell(token_addr, token_symbol, raw_qty, slippage)


def _wait_order_confirmed(order_id: str, token_symbol: str,
                          timeout: int = 60) -> dict | None:
    """
    轮询 baw market-order list --orderId，等待链上 FINISHED 或 FAILED。
    返回完整 order dict（含 txHash, toTokenActualQty, status），
    超时返回 None。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = subprocess.run(
                ["baw", "market-order", "list",
                 "--orderId", order_id, "--json"],
                capture_output=True, text=True, timeout=15
            )
            d = json.loads(r.stdout)
            orders = d.get("data", {}).get("list", [])
            if orders:
                order = orders[0]
                status = order.get("status", "")
                if status in ("FINISHED", "FAILED"):
                    return order
        except Exception as e:
            _log(f"[ORDER POLL] {token_symbol} orderId={order_id} err={e}")
        time.sleep(3)
    _log(f"[ORDER TIMEOUT] {token_symbol} orderId={order_id} 超过 {timeout}s 未确认")
    return None


def _confirm_sell_via_balance(token_addr: str, token_symbol: str,
                              expected_qty: float, order_id: str = "") -> dict | None:
    """
    卖出超时后的余额兜底：
    直接查链上余额，如果 < expected_qty * 0.1 则认为已成交。
    返回模拟的 FINISHED order dict 或 None。
    """
    real_balance = get_token_balance(token_addr)
    if real_balance < expected_qty * 0.1:
        # 余额已清，必然是链上成交了
        _log(f"[SELL CONFIRMED via balance] {token_symbol} 余额={real_balance:.4f} < "
             f"{expected_qty*0.1:.4f}，认定链上已成交 (orderId={order_id})")
        # 查 tx-history 获取真实收益
        eth_recv = _fetch_recent_sell_eth(token_addr, token_symbol)
        return {
            "status":          "FINISHED",
            "orderId":         order_id,
            "toTokenActualQty": str(eth_recv) if eth_recv > 0 else "0",
            "txHash":          "(confirmed-via-balance)",
            "_via_balance":    True,
            "_real_balance":   real_balance,
        }
    else:
        _log(f"[BALANCE CHECK] {token_symbol} 余额={real_balance:.4f} 仍存在，确实在链失败")
        return None


def _fetch_recent_sell_eth(token_addr: str, token_symbol: str,
                           lookback_sec: int = 300) -> float:
    """查 baw tx-history 中最近 lookback_sec 秒内该 token 的卖出 ETH 收到量"""
    try:
        r = subprocess.run(
            ["baw", "wallet", "tx-history", "--json", "--size=20"],
            capture_output=True, text=True, timeout=20
        )
        d = json.loads(r.stdout)
        if not d.get("success"):
            return 0.0
        txs = d.get("data", {}).get("transactions", [])
        cutoff = time.time() - lookback_sec
        stable_syms = {"ETH","WETH","BNB","WBNB","USDG","USDT","USDC"}
        for tx in txs:
            if tx.get("txType") != "swap" or tx.get("status") != "confirmed":
                continue
            # 过滤时间
            try:
                from datetime import datetime, timezone
                tx_dt = datetime.fromisoformat(tx["txTime"].replace("Z","+00:00"))
                if tx_dt.timestamp() < cutoff:
                    break  # 幹序降序，超过时间窗口直接必算
            except Exception:
                pass
            for item in tx.get("txHashList", []):
                sends = item.get("instructions", {}).get("send", [])
                recvs = item.get("instructions", {}).get("receive", [])
                # 卖出：send 包含该 token
                for s in sends:
                    si = s.get("tokenInfo", {})
                    ca = (si.get("contractAddress") or "").lower()
                    if ca == token_addr.lower() or si.get("symbol") == token_symbol:
                        eth_recv = sum(
                            float(rv.get("amount", 0))
                            for rv in recvs
                            if rv.get("tokenInfo", {}).get("symbol") in ("ETH","WETH")
                        )
                        if eth_recv > 0:
                            _log(f"[TX HISTORY] {token_symbol} 卖出确认: +{eth_recv:.6f} ETH")
                            return eth_recv
    except Exception as e:
        _log(f"[TX HISTORY ERROR] {e}")
    return 0.0


def _single_sell(token_addr: str, token_symbol: str,
                 qty: float, base_slippage: float) -> dict | None:
    """单笔卖出，失败时递增滑点重试，等待链上确认后返回结果"""
    sell_qty_str = _safe_qty(qty)
    _log(f"[SELL] {token_symbol} | qty={sell_qty_str} | slippage={base_slippage:.0%}")
    # 卖出：宽松锁校验（strict=False）——确保止损/止盈不被 PID 不匹配误拦
    if not _verify_process_lock(strict=False):
        _log(f"[SELL ABORT] {token_symbol} 没有任何活跃主进程，拒绝执行")
        return None

    # ★ 修复(2026-09-07)：记录发单前余额，BALANCE CHECK时对比
    # 根因：每次check余额>0就认定失败并重试，但余额在持续减少说明上笔已链上成交
    # 修法：若余额比发单前减少 >= 发单量*0.8，认定上笔已成交，停止重试
    _balance_before_sell = get_token_balance(token_addr)
    _log(f"[SELL PRE-BAL] {token_symbol} 发单前余额={_balance_before_sell:.4f}")

    current_slippage = base_slippage
    for attempt in range(1, SELL_RETRY_COUNT + 1):
        try:
            _check_gas_restored()
            _gas_lvl = _current_gas_level()
            # 每次发单前检查：余额是否已比初始余额减少了>=80%的发单量？
            _cur_bal_pre = get_token_balance(token_addr)
            _expected_sell = float(sell_qty_str)
            if _balance_before_sell > 0 and (_balance_before_sell - _cur_bal_pre) >= _expected_sell * 0.8:
                _log(f"[SELL SKIP-DUP] {token_symbol} 余额已从{_balance_before_sell:.0f}降至{_cur_bal_pre:.0f}"
                     f"，减少{_balance_before_sell-_cur_bal_pre:.0f}>={_expected_sell*0.8:.0f}，"
                     f"上笔已链上成交，停止重试")
                eth_recv = _fetch_recent_sell_eth(token_addr, token_symbol)
                return {"status": "FINISHED", "orderId": "(skip-dup)",
                        "toTokenActualQty": str(eth_recv), "_via_balance": True}
            cmd = [
                "baw", "market-order", "swap",
                "--binanceChainId", CHAIN_ID,
                "--fromTokenQty", sell_qty_str,
                "--fromToken", token_addr,
                "--toToken", BUY_TOKEN,    # 卖回 ETH
                "--slippage", str(round(current_slippage, 2)),
                "--gasLevel", _gas_lvl,
                "--json",
            ]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            d = json.loads(r.stdout)

            if d.get("success"):
                order_id = d.get("data", {}).get("orderId", "")
                _log(f"[SELL SUBMITTED] {token_symbol} 第{attempt}次 orderId={order_id} slippage={current_slippage:.0%} gasLevel={_gas_lvl} | 等待链上确认...")

                # 等待链上确认
                # 第一轮等 480s（Robinhood Chain 高峰期可能需要 3~5 分钟结算）
                confirmed = _wait_order_confirmed(order_id, token_symbol, timeout=480)
                if confirmed is None:
                    _log(f"[SELL TIMEOUT] {token_symbol} orderId={order_id} 一轮(480s)未确认，先查余额")
                    # ① 余额已清 → 链上已成交，不发新单
                    via_bal = _confirm_sell_via_balance(
                        token_addr, token_symbol, expected_qty=float(sell_qty_str), order_id=order_id
                    )
                    if via_bal:
                        eth_out = float(via_bal.get("toTokenActualQty") or 0)
                        _log(f"[SELL CONFIRMED] {token_symbol} ✅ 余额兜底确认成交 ETH收到={eth_out:.6f}")
                        return via_bal
                    # ② 余额仍在 → 订单可能还在 pending，再等一轮 300s，等到链上明确 FAILED 才重试
                    _escalate_gas(token_symbol)
                    _log(f"[SELL PENDING] {token_symbol} orderId={order_id} 余额仍在→升级gas，继续等链上明确结果(+300s)...")
                    confirmed2 = _wait_order_confirmed(order_id, token_symbol, timeout=300)
                    if confirmed2 is not None:
                        # 第二轮等到了明确结果
                        if confirmed2["status"] == "FINISHED":
                            eth_out = float(confirmed2.get("toTokenActualQty") or 0)
                            tx = confirmed2.get("txHash", "?")
                            _log(f"[SELL CONFIRMED] {token_symbol} ✅ 第二轮确认成交 tx={tx[:20]} ETH={eth_out:.6f}")
                            return confirmed2
                        else:
                            tx = confirmed2.get("txHash", "?")
                            _log(f"[SELL CHAIN FAILED] {token_symbol} ❌ 链上明确FAILED tx={tx[:20]}，递增滑点重试")
                            # 继续下方 slippage 递增逻辑
                    else:
                        # 两轮共 780s 仍无结果，再查一次余额兜底
                        via_bal2 = _confirm_sell_via_balance(
                            token_addr, token_symbol, expected_qty=float(sell_qty_str), order_id=order_id
                        )
                        if via_bal2:
                            eth_out = float(via_bal2.get("toTokenActualQty") or 0)
                            _log(f"[SELL CONFIRMED] {token_symbol} ✅ 二次余额兜底确认 ETH={eth_out:.6f}")
                            return via_bal2
                        _log(f"[SELL RETRY] {token_symbol} 两轮共780s仍未确认且余额仍在，认定失败，递增滑点重试")
                elif confirmed["status"] == "FINISHED":
                    eth_out = float(confirmed.get("toTokenActualQty") or 0)
                    tx = confirmed.get("txHash", "?")
                    _log(f"[SELL CONFIRMED] {token_symbol} ✅ tx={tx[:20]} ETH收到={eth_out:.6f} slippage={current_slippage:.0%}")
                    return confirmed   # 返回完整 order dict
                else:  # FAILED
                    tx = confirmed.get("txHash", "?")
                    _log(f"[SELL CHAIN FAILED] {token_symbol} ❌ tx={tx[:20]} status=FAILED slippage={current_slippage:.0%} | 递增滑点重试")
                    # 链上失败→递增滑点继续重试
            else:
                err_msg = str(d).lower()
                _log(f"[SELL FAIL #{attempt}] {token_symbol} slippage={current_slippage:.0%} | {str(d)[:100]}")
                # 精度问题：尝试取整重试
                if ("invalid" in err_msg or "amount" in err_msg) and qty >= 1:
                    sell_qty_str = str(int(qty) - 1)
                    _log(f"[SELL RETRY] {token_symbol} 精度修正 qty → {sell_qty_str}")

            # 递增滑点
            current_slippage = min(current_slippage + SLIPPAGE_SELL_STEP, SLIPPAGE_SELL_MAX)
            if attempt < SELL_RETRY_COUNT:
                time.sleep(2)

        except Exception as e:
            _log(f"[SELL ERROR #{attempt}] {token_symbol} {e}")
            time.sleep(2)

    _log(f"[SELL FAILED] {token_symbol} 全部 {SELL_RETRY_COUNT} 次重试均失败，可能流动性枯竭")
    return None


def _batch_sell(token_addr: str, token_symbol: str,
                total_qty: float, liq_usd: float,
                base_slippage: float) -> dict | None:
    """分批卖出（持仓过大时）"""
    token_price = _get_token_price(token_addr)
    holding_val = total_qty * token_price if token_price > 0 else 0

    # 每批不超过流动性的 4%
    batch_count = min(max(int(holding_val / (liq_usd * 0.04)) + 1, 2), 5)
    _log(f"[SELL BATCH] {token_symbol} 持仓 ${holding_val:.0f} > 流动性 ${liq_usd:.0f}×5%，分 {batch_count} 批卖出")

    last_result = None
    for i in range(batch_count):
        # 每批重新查余额（之前批次可能已卖掉部分）
        cur_balance = get_token_balance(token_addr)
        if cur_balance <= 0:
            break

        # 最后一批全清，其余均分
        if i == batch_count - 1:
            batch_qty = cur_balance
        else:
            batch_qty = cur_balance / (batch_count - i)

        # 每批滑点递增
        batch_slippage = min(base_slippage + i * SLIPPAGE_SELL_STEP, SLIPPAGE_SELL_MAX)
        _log(f"[SELL BATCH {i+1}/{batch_count}] {token_symbol} qty={_safe_qty(batch_qty)} slippage={batch_slippage:.0%}")

        result = _single_sell(token_addr, token_symbol, batch_qty, batch_slippage)
        if result:
            last_result = result
        if i < batch_count - 1:
            time.sleep(3)

    return last_result


def get_token_balance(token_addr: str) -> float:
    """查询 agent 钱包中 token 的链上余额"""
    try:
        r = subprocess.run(
            ["baw", "wallet", "balance", "--json"],
            capture_output=True, text=True, timeout=15,
        )
        d = json.loads(r.stdout)
        for item in d.get("data", []):
            if item.get("address", "").lower() == token_addr.lower():
                bal = float(item.get("balance", 0) or 0)
                val = float(item.get("value", 0) or 0)
                # 粉尘过滤：价值 < $0.5 视为已清仓
                if val < DUST_USD_THRESHOLD and bal > 0:
                    _log(f"[DUST] {token_addr[:10]}… 价值 ${val:.3f} < ${DUST_USD_THRESHOLD}，视为粉尘忽略")
                    return 0.0
                return bal
    except Exception as e:
        _log(f"[BALANCE ERROR] {token_addr} {e}")
    return 0.0


# token 价格短期缓存（防止同一快照节点多次调用 baw balance）
_PRICE_CACHE: dict = {}   # addr_lower → (price, ts)
_PRICE_CACHE_TTL = 10     # 10s TTL，同一信号内各策略分支公用同一快照


def _get_token_price(token_addr: str) -> float:
    """从 baw balance 获取当前价格，10s 短期缓存防重复调用"""
    addr = token_addr.lower()
    now  = time.time()
    if addr in _PRICE_CACHE and now - _PRICE_CACHE[addr][1] < _PRICE_CACHE_TTL:
        return _PRICE_CACHE[addr][0]
    try:
        r = subprocess.run(
            ["baw", "wallet", "balance", "--json"],
            capture_output=True, text=True, timeout=15,
        )
        d = json.loads(r.stdout)
        for item in d.get("data", []):
            if item.get("address", "").lower() == addr:
                price = float(item.get("price", 0) or 0)
                _PRICE_CACHE[addr] = (price, now)
                return price
    except Exception:
        pass
    return 0.0


def _get_liquidity(token_addr: str, symbol: str = "") -> float:
    """从 DexScreener 获取流动性（用于分批判断）"""
    try:
        import requests as req
        r = req.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{token_addr}",
            timeout=6,
            proxies={"https": "socks5://REDACTED_PROXY_CREDENTIALS"},
        )
        pairs = r.json().get("pairs", [])
        if pairs:
            p = sorted(pairs, key=lambda x: x.get("liquidity", {}).get("usd", 0), reverse=True)[0]
            return float(p.get("liquidity", {}).get("usd", 0) or 0)
    except Exception:
        pass
    return 0.0


def get_token_price_and_mc(token_addr: str) -> tuple[float, float]:
    """返回 (price_usd, market_cap_usd)"""
    price = _get_token_price(token_addr)
    return price, 0.0
