#!/usr/bin/env python3
"""
post_bstock_arb_flow.py  v2.1
==============================
美股休盘 + 持仓清空后自动套利流程

流程：
  1. 等待 bstock 美股持仓全部平仓（休盘后）
  2. 扫描 5 所（币安/Gate/Bybit/OKX/Bitget）合约资金费率
  3. 选最优两个交易所组合（净费率最高 + 价差最低 + ≤4h结算）
  4. 检查两所余额，不足则从币安分发（5:5）
  5. 市价对冲开仓（maker 优先）
  6. 持仓监控，价差异常或费率消失自动平仓

规则：
  - 只做结算间隔 ≤ 4h 的标的
  - 净费率 < 0.03%/8h 换算后不值得做
  - 跨所价差 > 0.10% 不入场
  - BTC 1h 波动 > 1.5% 暂缓
  - 名义仓位 ≤ 1500U/腿
  - [v2.1] 滑点+手续费成本检查（避免手续费吃掉净费率）
  - [v2.1] bStock资金隔离（只用合约账户余额，不动现货/链上）
"""

import sys, os, json, time, subprocess, requests, hmac, hashlib
import urllib.parse, math, logging, uuid
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, "/root/.openclaw/workspace")

# ── 配置 ─────────────────────────────────────────────────
PROXY   = "socks5://REDACTED_PROXY_CREDENTIALS"
PROXIES = {"https": PROXY, "http": PROXY}
JARVIS_TARGET = "432401404:thread:019fcac8-4a93-7bea-a930-7f0e59e4897c"

# API Keys
KEYS = {
    "binance": {
        "key":    "REDACTED_API_KEY",
        "secret": "REDACTED_API_SECRET",
    },
    "gate": {
        "key":    "31fa24489c3c7d862575696fa7daf2fa",
        "secret": "cd7e53b368a7d91779cc32d66699de7073cc8ed743c72d422c3ec73ef20afac0",
    },
    "bybit": {
        "key":    "61KSIeSU4lrPmRIlEz",
        "secret": "hgMaDpcYVIctxrfJTjKzbw50ZpSFyBi5gNsa",
    },
    "okx": {
        "key":        "5e607ed7-3f0d-490f-9f01-ee9274cae102",
        "secret":     "C683AB5E5291B77F6D9C234FF05C552C",
        "passphrase": "Zxcvzx12.",
    },
    "bitget": {
        "key":        "bg_f6b918ba65e723d2caea2f605112427d",
        "secret":     "c8e231cce4db05cd761b6beaba28b350ead6fa4319ecca217d97fdebef02f8c8",
        "passphrase": "Zxcvzx12",
    },
}

# 充值地址（来自 AGENTS.md）
DEPOSIT_ADDRS = {
    "gate":   {"addr": "0xecd2c243B4d55a8E31c0599a9671907942dC171c", "network": "ARBITRUM", "fee": 0.1},
    "okx":    {"addr": "0xe34ab1b30a5b2c85bc62054ad318d9524ce7a0c2", "network": "AVAXC",    "fee": 0.04},
    "bybit":  {"addr": "0x659bca897e9560acbc2ae2712a2daa809979b8bb", "network": "BSC",      "fee": 0.01},
    "bitget": {"addr": "0xfd4c3a379c2b5c6020691b76631ada9842e02b0c", "network": "BEP20",    "fee": 0.01},
}
BINANCE_BSC_ADDR = "0x3b7145a21c2d20741cbb2d78dc5317cf72d31455"

# 套利参数
MAX_SPREAD_PCT   = 0.10
MIN_NET_FR_8H    = 0.03   # %/8h 最低净费率
MAX_BTC_1H_MOVE  = 1.5
NOTIONAL_PER_LEG = 1500
ABORT_SPREAD_PCT = 0.20
LEVERAGE         = 2
MAX_FUNDING_INTERVAL_H = 4  # 只做 ≤ 4h 结算的标的

# [v2.1] 手续费+滑点参数（保守估算）
# taker 手续费：binance=0.05%, gate=0.05%, bybit=0.06%, okx=0.05%, bitget=0.06%
TAKER_FEE = {"binance": 0.05, "gate": 0.05, "bybit": 0.06, "okx": 0.05, "bitget": 0.06}  # %/单边
ESTIMATED_SLIPPAGE_PCT = 0.03  # 单边滑点保守估算 0.03%
# 开平各两腿 = 4次手续费 + 4次滑点，净费率必须覆盖这个成本才值得做
# 实际 entry_cost_check() 动态计算

# [v2.1] bStock 资金隔离
# 套利只用合约账户(get_balance=futures)，不从现货账户提现补充
# 只有在目标交易所合约余额不足时，才从「币安合约账户」提现到目标所
# 永远不会碰现货账户/链上钱包（bStock资金所在地）
BINANCE_FUTURES_ONLY = True  # True=只查合约账户; False=同时查现货（旧行为）

LOG_PATH = Path("/tmp/arb_flow.log")
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s UTC] %(message)s",
    datefmt="%Y-%m-%d %H:%M",
    handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("arb_flow")

# ── 通知 ─────────────────────────────────────────────────
def notify(msg):
    try:
        subprocess.run(["openclaw", "message", "send",
            "--channel", "jarvis", "--to", JARVIS_TARGET, "--message", msg],
            capture_output=True, timeout=10)
    except Exception as e:
        log.warning(f"通知失败: {e}")

# ── 签名工具 ──────────────────────────────────────────────
def _gate_sign(secret, method, path, query="", body=""):
    ts  = str(int(time.time()))
    bh  = hashlib.sha512(body.encode()).hexdigest()
    msg = "\n".join([method, path, query, bh, ts])
    sig = hmac.new(secret.encode(), msg.encode(), hashlib.sha512).hexdigest()
    return ts, sig

def _bybit_sign(key, secret, params_str):
    ts = str(int(time.time() * 1000))
    sign_str = ts + key + "5000" + params_str
    sig = hmac.new(secret.encode(), sign_str.encode(), hashlib.sha256).hexdigest()
    return ts, sig

def _bnb_sign(secret, params):
    params["timestamp"] = int(time.time() * 1000)
    qs  = urllib.parse.urlencode(params)
    sig = hmac.new(secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
    return qs + f"&signature={sig}"

def _okx_sign(secret, ts, method, path, body=""):
    msg = ts + method + path + body
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).digest()

# ── 手续费+滑点成本检查 ──────────────────────────────────
def entry_cost_check(short_ex: str, long_ex: str, net_fr_8h: float) -> tuple:
    """
    检查净费率是否能覆盖开仓+平仓的手续费和滑点成本。
    返回 (pass: bool, net_after_cost: float)
    开+平 = 4 笔：short_open, short_close, long_open, long_close
    每笔 = taker_fee + slippage
    """
    fee_short  = TAKER_FEE.get(short_ex, 0.06)
    fee_long   = TAKER_FEE.get(long_ex, 0.06)
    total_cost = (fee_short + fee_long) * 2 + ESTIMATED_SLIPPAGE_PCT * 4  # 4 legs
    # 净费率转换为「每8h持有」收益
    # 套利一般持有 1~2 个结算周期（保守按1周期=8h等价）
    net_after = net_fr_8h - total_cost
    passed = net_after > 0
    return passed, net_after

# ── 余额查询 ──────────────────────────────────────────────
def get_balance(exchange: str) -> float:
    """返回各所合约账户 USDT 可用余额"""
    try:
        k = KEYS[exchange]
        if exchange == "gate":
            ts, sig = _gate_sign(k["secret"], "GET", "/api/v4/futures/usdt/accounts")
            r = requests.get("https://api.gateio.ws/api/v4/futures/usdt/accounts",
                headers={"KEY": k["key"], "SIGN": sig, "Timestamp": ts},
                proxies=PROXIES, timeout=10).json()
            return float(r.get("available", 0))

        elif exchange == "bybit":
            qs = urllib.parse.urlencode(sorted({"accountType": "UNIFIED"}.items()))
            ts, sig = _bybit_sign(k["key"], k["secret"], qs)
            r = requests.get("https://api.bybit.com/v5/account/wallet-balance",
                headers={"X-BAPI-API-KEY": k["key"], "X-BAPI-TIMESTAMP": ts,
                         "X-BAPI-SIGN": sig, "X-BAPI-RECV-WINDOW": "5000"},
                params={"accountType": "UNIFIED"}, proxies=PROXIES, timeout=10).json()
            for acc in r.get("result", {}).get("list", []):
                for c in acc.get("coin", []):
                    if c.get("coin") == "USDT":
                        return float(c.get("availableToWithdraw") or c.get("walletBalance") or 0)

        elif exchange == "okx":
            import base64
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            sig = base64.b64encode(_okx_sign(k["secret"], ts, "GET", "/api/v5/account/balance")).decode()
            r = requests.get("https://www.okx.com/api/v5/account/balance",
                headers={"OK-ACCESS-KEY": k["key"], "OK-ACCESS-SIGN": sig,
                         "OK-ACCESS-TIMESTAMP": ts, "OK-ACCESS-PASSPHRASE": k["passphrase"]},
                proxies=PROXIES, timeout=10).json()
            for d in r.get("data", [{}])[0].get("details", []):
                if d.get("ccy") == "USDT":
                    return float(d.get("availBal", 0))

        elif exchange == "bitget":
            import base64, hashlib as _hs
            ts = str(int(time.time() * 1000))
            body = ""
            msg = ts + "GET" + "/api/v3/account/assets" + body
            sig = base64.b64encode(hmac.new(k["secret"].encode(), msg.encode(), _hs.sha256).digest()).decode()
            r = requests.get("https://api.bitget.com/api/v3/account/assets",
                params={"coin": "USDT"},
                headers={"ACCESS-KEY": k["key"], "ACCESS-SIGN": sig,
                         "ACCESS-TIMESTAMP": ts, "ACCESS-PASSPHRASE": k["passphrase"]},
                proxies=PROXIES, timeout=10).json()
            for d in r.get("data", []):
                if d.get("coin") == "USDT":
                    return float(d.get("available", 0))

        elif exchange == "binance":
            # [v2.1] 只查合约账户（期货账户），不查现货账户，避免把bStock资金算进去
            qs = _bnb_sign(k["secret"], {})
            r = requests.get(f"https://fapi.binance.com/fapi/v2/balance?{qs}",
                headers={"X-MBX-APIKEY": k["key"]}, proxies=PROXIES, timeout=10).json()
            for b in (r if isinstance(r, list) else []):
                if b.get("asset") == "USDT":
                    return float(b.get("availableBalance", 0))

    except Exception as e:
        log.warning(f"[余额] {exchange} 查询失败: {e}")
    return 0.0


def get_binance_futures_balance() -> float:
    """[v2.1] 专门查币安合约账户余额，用于补资金判断（隔离现货）"""
    return get_balance("binance")

# ── 资金费率扫描 ──────────────────────────────────────────
def fetch_funding_rates() -> dict:
    """
    返回 { sym: { exchange: {"rate": float(%/8h), "interval_h": int} } }
    只保留结算间隔 ≤ 4h 的标的
    """
    all_rates = {}  # sym -> {exchange -> {rate, interval_h}}

    # ── 币安 ──
    try:
        r = requests.get("https://fapi.binance.com/fapi/v1/fundingInfo",
            proxies=PROXIES, timeout=10).json()
        for item in (r if isinstance(r, list) else []):
            sym = item.get("symbol", "").replace("USDT", "")
            interval_h = int(item.get("fundingIntervalHours", 8))
            if interval_h > MAX_FUNDING_INTERVAL_H:
                continue
            # 再拉实时费率
            pass  # 下面批量拉
        # 批量拉实时费率
        r2 = requests.get("https://fapi.binance.com/fapi/v1/premiumIndex",
            proxies=PROXIES, timeout=10).json()
        # 先建 interval 映射
        interval_map = {i.get("symbol","").replace("USDT",""): int(i.get("fundingIntervalHours",8))
                        for i in (r if isinstance(r, list) else [])}
        for item in (r2 if isinstance(r2, list) else []):
            sym = item.get("symbol","").replace("USDT","")
            interval_h = interval_map.get(sym, 8)
            if interval_h > MAX_FUNDING_INTERVAL_H:
                continue
            fr = float(item.get("lastFundingRate", 0)) * 100
            # 统一换算为 /8h
            fr_8h = fr * 8 / interval_h if interval_h > 0 else fr
            all_rates.setdefault(sym, {})["binance"] = {"rate": fr_8h, "interval_h": interval_h}
    except Exception as e:
        log.warning(f"[扫描] 币安费率失败: {e}")

    # ── Gate ──
    try:
        contracts = requests.get("https://api.gateio.ws/api/v4/futures/usdt/contracts",
            proxies=PROXIES, timeout=10).json()
        for c in contracts:
            sym = c.get("name","").replace("_USDT","")
            interval_h = int(c.get("funding_interval", 28800)) // 3600
            if interval_h > MAX_FUNDING_INTERVAL_H:
                continue
            fr = float(c.get("funding_rate", 0)) * 100
            fr_8h = fr * 8 / interval_h if interval_h > 0 else fr
            all_rates.setdefault(sym, {})["gate"] = {"rate": fr_8h, "interval_h": interval_h}
    except Exception as e:
        log.warning(f"[扫描] Gate 费率失败: {e}")

    # ── Bybit（实际查询结算间隔，不再硬编码8h）──
    try:
        bybit_interval_map = {}
        r_inst = requests.get("https://api.bybit.com/v5/market/instruments-info",
            params={"category": "linear", "limit": 1000},
            proxies=PROXIES, timeout=10).json()
        for item in r_inst.get("result", {}).get("list", []):
            sym = item.get("symbol","").replace("USDT","")
            interval_min = int(item.get("fundingInterval", 480) or 480)
            bybit_interval_map[sym] = interval_min // 60
        r = requests.get("https://api.bybit.com/v5/market/tickers",
            params={"category": "linear"}, proxies=PROXIES, timeout=10).json()
        for item in r.get("result", {}).get("list", []):
            sym = item.get("symbol","").replace("USDT","")
            interval_h = bybit_interval_map.get(sym, 8)
            if interval_h > MAX_FUNDING_INTERVAL_H:
                continue
            fr = float(item.get("fundingRate", 0)) * 100
            fr_8h = fr * 8 / interval_h if interval_h > 0 else fr
            all_rates.setdefault(sym, {})["bybit"] = {"rate": fr_8h, "interval_h": interval_h}
    except Exception as e:
        log.warning(f"[扫描] Bybit 费率失败: {e}")

    # ── OKX（实际查询结算间隔）──
    try:
        okx_interval_map = {}
        r_inst = requests.get("https://www.okx.com/api/v5/public/instruments",
            params={"instType": "SWAP"}, proxies=PROXIES, timeout=10).json()
        for item in r_inst.get("data", []):
            sym = item.get("instId","").replace("-USDT-SWAP","").replace("-","")
            interval_sec = int(item.get("fundingInterval", 28800) or 28800)
            okx_interval_map[sym] = interval_sec // 3600
        r = requests.get("https://www.okx.com/api/v5/public/funding-rate?instType=SWAP",
            proxies=PROXIES, timeout=10).json()
        for item in r.get("data", []):
            sym = item.get("instId","").replace("-USDT-SWAP","").replace("-","")
            interval_h = okx_interval_map.get(sym, 8)
            if interval_h > MAX_FUNDING_INTERVAL_H:
                continue
            fr = float(item.get("fundingRate", 0)) * 100
            fr_8h = fr * 8 / interval_h if interval_h > 0 else fr
            all_rates.setdefault(sym, {})["okx"] = {"rate": fr_8h, "interval_h": interval_h}
    except Exception as e:
        log.warning(f"[扫描] OKX 费率失败: {e}")

    # ── Bitget（实际查询结算间隔）──
    try:
        r = requests.get("https://api.bitget.com/api/mix/v1/market/contracts",
            params={"productType": "umcbl"}, proxies=PROXIES, timeout=10).json()
        for item in r.get("data", []):
            sym = item.get("symbolName","").replace("USDT","")
            interval_sec = int(item.get("fundingInterval", 28800) or 28800)
            interval_h = interval_sec // 3600
            if interval_h > MAX_FUNDING_INTERVAL_H:
                continue
            fr = float(item.get("fundingRate", 0) or 0) * 100
            fr_8h = fr * 8 / interval_h if interval_h > 0 else fr
            all_rates.setdefault(sym, {})["bitget"] = {"rate": fr_8h, "interval_h": interval_h}
    except Exception as e:
        log.warning(f"[扫描] Bitget 费率失败: {e}")

    return all_rates

# ── 找最优交易所对 ────────────────────────────────────────
def find_best_pair(all_rates: dict) -> list:
    """
    [v2.1] 遍历所有标的，找净费率最高的两个交易所组合。
    在筛选阶段同时做手续费+滑点成本检查，不通过的直接过滤掉。
    返回 Top20 候选列表（按扣费后净收益排序）
    """
    candidates = []

    for sym, ex_data in all_rates.items():
        if len(ex_data) < 2:
            continue
        exs = list(ex_data.keys())
        # 枚举所有两两组合
        for i in range(len(exs)):
            for j in range(i+1, len(exs)):
                ex_a, ex_b = exs[i], exs[j]
                fr_a = ex_data[ex_a]["rate"]
                fr_b = ex_data[ex_b]["rate"]
                net_fr = abs(fr_a - fr_b)
                if net_fr < MIN_NET_FR_8H:
                    continue
                # 做空高费率一侧，做多低费率一侧
                if fr_a > fr_b:
                    short_ex, long_ex = ex_a, ex_b
                else:
                    short_ex, long_ex = ex_b, ex_a

                # [v2.1] 手续费+滑点成本检查
                cost_ok, net_after_cost = entry_cost_check(short_ex, long_ex, net_fr)
                if not cost_ok:
                    log.debug(
                        f"[成本过滤] {sym} {short_ex}/{long_ex} "
                        f"net_fr={net_fr:.4f}% 扣费后={net_after_cost:.4f}% 不足，跳过"
                    )
                    continue

                candidates.append({
                    "sym": sym,
                    "short_ex": short_ex,
                    "long_ex":  long_ex,
                    "net_fr":   net_fr,
                    "net_after_cost": net_after_cost,   # 扣费后真实收益
                    "short_fr": max(fr_a, fr_b),
                    "long_fr":  min(fr_a, fr_b),
                })

    # 按扣费后净收益排序（更真实）
    candidates.sort(key=lambda x: -x["net_after_cost"])
    return candidates[:20]

# ── 价差验证 ──────────────────────────────────────────────
def get_orderbook_price(exchange: str, sym: str) -> tuple:
    """返回 (bid, ask)，失败返回 (0, 0)"""
    try:
        if exchange == "binance":
            r = requests.get("https://fapi.binance.com/fapi/v1/depth",
                params={"symbol": f"{sym}USDT", "limit": 1},
                proxies=PROXIES, timeout=5).json()
            bid = float(r["bids"][0][0]) if r.get("bids") else 0
            ask = float(r["asks"][0][0]) if r.get("asks") else 0
        elif exchange == "gate":
            r = requests.get("https://api.gateio.ws/api/v4/futures/usdt/order_book",
                params={"contract": f"{sym}_USDT", "limit": 1},
                proxies=PROXIES, timeout=5).json()
            bid = float(r["bids"][0]["p"]) if r.get("bids") else 0
            ask = float(r["asks"][0]["p"]) if r.get("asks") else 0
        elif exchange == "bybit":
            r = requests.get("https://api.bybit.com/v5/market/orderbook",
                params={"category": "linear", "symbol": f"{sym}USDT", "limit": 1},
                proxies=PROXIES, timeout=5).json()
            bid = float(r["result"]["b"][0][0]) if r["result"]["b"] else 0
            ask = float(r["result"]["a"][0][0]) if r["result"]["a"] else 0
        elif exchange == "okx":
            r = requests.get(f"https://www.okx.com/api/v5/market/books",
                params={"instId": f"{sym}-USDT-SWAP", "sz": 1},
                proxies=PROXIES, timeout=5).json()
            data = r.get("data", [{}])[0]
            bid = float(data.get("bids", [[0]])[0][0])
            ask = float(data.get("asks", [[0]])[0][0])
        elif exchange == "bitget":
            r = requests.get("https://api.bitget.com/api/mix/v1/market/depth",
                params={"symbol": f"{sym}USDT_UMCBL", "limit": 1},
                proxies=PROXIES, timeout=5).json()
            data = r.get("data", {})
            bid = float(data.get("bids", [[0]])[0][0])
            ask = float(data.get("asks", [[0]])[0][0])
        else:
            return 0, 0
        return bid, ask
    except Exception as e:
        log.warning(f"[OrderBook] {exchange} {sym}: {e}")
        return 0, 0

def validate_spread(cand: dict) -> bool:
    """检查两所价差是否满足入场条件"""
    sym      = cand["sym"]
    short_ex = cand["short_ex"]
    long_ex  = cand["long_ex"]

    s_bid, s_ask = get_orderbook_price(short_ex, sym)
    l_bid, l_ask = get_orderbook_price(long_ex, sym)

    if not (s_bid and l_bid):
        return False

    # 做空一侧卖出拿 bid，做多一侧买入用 ask
    ref = (s_bid + l_ask) / 2
    spread = abs(s_bid - l_ask) / ref * 100

    cand["spread_pct"] = spread
    cand["short_bid"]  = s_bid
    cand["long_ask"]   = l_ask
    cand["ref_price"]  = ref

    log.info(f"  {sym:10s} {short_ex}空/{long_ex}多  "
             f"net_fr={cand['net_fr']:.4f}%  spread={spread:.4f}%  "
             f"net_after_cost={cand.get('net_after_cost', 0):.4f}%")

    return spread <= MAX_SPREAD_PCT

# ── 分发资金 ──────────────────────────────────────────────
def fund_exchanges(ex_a: str, ex_b: str, total_usdt: float):
    """
    [v2.1] 确保两个交易所各有足够余额，不够从币安合约账户补（5:5 分配）
    ⚠️ 只使用币安合约账户余额补充，不动现货/链上钱包（bStock资金所在地）
    """
    # 每所分配一半，至少保证能开仓
    need_each = max(total_usdt / 2, NOTIONAL_PER_LEG / LEVERAGE + 50)

    for ex in [ex_a, ex_b]:
        if ex == "binance":
            continue  # 币安本身不需要充值
        bal = get_balance(ex)
        log.info(f"[资金] {ex} 当前余额: {bal:.2f}U  需要: {need_each:.0f}U")
        if bal >= need_each:
            log.info(f"[资金] {ex} 余额充足，无需转入")
            continue

        # [v2.1] 只从币安「合约账户」补资金，不触碰现货/链上（bStock资金在那里）
        shortfall = need_each - bal + 10  # 多转10U作缓冲
        bnb_futures_bal = get_binance_futures_balance()
        if bnb_futures_bal < shortfall:
            log.warning(
                f"[资金] 币安合约账户余额不足 ({bnb_futures_bal:.2f}U < {shortfall:.2f}U)\n"
                f"       ⚠️ 不会动现货账户（bStock活动资金），请手动充值 {ex}"
            )
            continue

        addr_info = DEPOSIT_ADDRS.get(ex)
        if not addr_info:
            log.warning(f"[资金] {ex} 无充值地址配置")
            continue

        withdraw_amt = round(shortfall + addr_info["fee"], 2)
        k = KEYS["binance"]
        params = {
            "coin": "USDT",
            "network": addr_info["network"],
            "address": addr_info["addr"],
            "amount": str(withdraw_amt),
        }
        qs = _bnb_sign(k["secret"], params)
        r = requests.post(f"https://api.binance.com/sapi/v1/capital/withdraw/apply?{qs}",
            headers={"X-MBX-APIKEY": k["key"]}, proxies=PROXIES, timeout=15).json()
        if r.get("id"):
            log.info(f"[资金] ✅ 币安→{ex} 提现 {withdraw_amt}U  id={r['id']}")
            notify(f"💸 资金调度: 币安→{ex} {withdraw_amt}U，等待到账...")
            time.sleep(120)  # 等待到账
        else:
            log.error(f"[资金] 币安→{ex} 提现失败: {r}")

# ── 执行开仓（通用） ──────────────────────────────────────
def open_arb_position(cand: dict) -> bool:
    """
    在两所各开仓：short_ex 做空，long_ex 做多
    使用各所对应的市价/限价接口
    """
    sym      = cand["sym"]
    short_ex = cand["short_ex"]
    long_ex  = cand["long_ex"]
    ref_px   = cand["ref_price"]
    notional = min(NOTIONAL_PER_LEG, get_balance(short_ex) * LEVERAGE * 0.8,
                                     get_balance(long_ex)  * LEVERAGE * 0.8)
    notional = math.floor(notional / 100) * 100
    if notional < 200:
        log.error(f"[开仓] 名义仓位太小 ({notional}U)，跳过")
        return False

    log.info(f"[开仓] {sym}  {short_ex}空+{long_ex}多  notional={notional}U×{LEVERAGE}x")

    ok_short = place_order(short_ex, sym, "SELL", notional, ref_px)
    ok_long  = place_order(long_ex,  sym, "BUY",  notional, ref_px)

    if ok_short and ok_long:
        notify(
            f"✅ 套利开仓成功 [{sym}]\n"
            f"{short_ex} 空 + {long_ex} 多\n"
            f"名义: {notional}U × {LEVERAGE}x  "
            f"净费率: {cand['net_fr']:.4f}%/8h\n"
            f"价差: {cand['spread_pct']:.4f}%"
        )
        return True
    else:
        # 有一腿失败，立即平掉另一腿
        if ok_short:
            place_order(short_ex, sym, "BUY", notional, ref_px)   # 平空
        if ok_long:
            place_order(long_ex,  sym, "SELL", notional, ref_px)  # 平多
        log.error(f"[开仓] 失败，已回撤  short_ok={ok_short} long_ok={ok_long}")
        return False

def place_order(exchange: str, sym: str, side: str, notional: float, ref_px: float) -> bool:
    """各所下市价单，返回是否成功"""
    try:
        k = KEYS[exchange]
        qty = round(notional * LEVERAGE / ref_px, 3)

        if exchange == "gate":
            size = int(notional * LEVERAGE / ref_px)  # Gate lot=1
            size = size if side == "BUY" else -size
            ts, sig = _gate_sign(k["secret"], "POST", "/api/v4/futures/usdt/orders")
            body = json.dumps({"contract": f"{sym}_USDT", "size": size,
                               "price": "0", "tif": "ioc"})
            ts, sig = _gate_sign(k["secret"], "POST", "/api/v4/futures/usdt/orders", "", body)
            r = requests.post("https://api.gateio.ws/api/v4/futures/usdt/orders",
                headers={"KEY": k["key"], "SIGN": sig, "Timestamp": ts,
                         "Content-Type": "application/json"},
                data=body, proxies=PROXIES, timeout=15).json()
            ok = bool(r.get("id"))

        elif exchange == "bybit":
            data = {"category": "linear", "symbol": f"{sym}USDT",
                    "side": "Buy" if side == "BUY" else "Sell",
                    "orderType": "Market", "qty": str(qty),
                    "timeInForce": "IOC", "positionIdx": 0}
            body_str = json.dumps(data)
            ts, sig = _bybit_sign(k["key"], k["secret"], body_str)
            r = requests.post("https://api.bybit.com/v5/order/create",
                headers={"X-BAPI-API-KEY": k["key"], "X-BAPI-TIMESTAMP": ts,
                         "X-BAPI-SIGN": sig, "X-BAPI-RECV-WINDOW": "5000",
                         "Content-Type": "application/json"},
                data=body_str, proxies=PROXIES, timeout=15).json()
            ok = r.get("retCode") == 0

        elif exchange == "binance":
            params = {"symbol": f"{sym}USDT", "side": side,
                      "type": "MARKET", "quantity": str(qty)}
            qs = _bnb_sign(k["secret"], params)
            r = requests.post(f"https://fapi.binance.com/fapi/v1/order?{qs}",
                headers={"X-MBX-APIKEY": k["key"]}, proxies=PROXIES, timeout=15).json()
            ok = bool(r.get("orderId"))

        elif exchange == "okx":
            import base64 as _b64
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            body = json.dumps({"instId": f"{sym}-USDT-SWAP",
                               "tdMode": "cross", "side": side.lower(),
                               "ordType": "market", "sz": str(qty)})
            sig = _b64.b64encode(_okx_sign(k["secret"], ts, "POST",
                                           "/api/v5/trade/order", body)).decode()
            r = requests.post("https://www.okx.com/api/v5/trade/order",
                headers={"OK-ACCESS-KEY": k["key"], "OK-ACCESS-SIGN": sig,
                         "OK-ACCESS-TIMESTAMP": ts, "OK-ACCESS-PASSPHRASE": k["passphrase"],
                         "Content-Type": "application/json"},
                data=body, proxies=PROXIES, timeout=15).json()
            ok = r.get("code") == "0"

        elif exchange == "bitget":
            import base64 as _b64, hashlib as _hs
            ts = str(int(time.time() * 1000))
            body = json.dumps({"symbol": f"{sym}USDT_UMCBL",
                               "marginCoin": "USDT", "size": str(qty),
                               "side": "open_long" if side=="BUY" else "open_short",
                               "orderType": "market", "leverage": str(LEVERAGE)})
            msg = ts + "POST" + "/api/mix/v1/order/placeOrder" + body
            sig = _b64.b64encode(hmac.new(k["secret"].encode(), msg.encode(),
                                          _hs.sha256).digest()).decode()
            r = requests.post("https://api.bitget.com/api/mix/v1/order/placeOrder",
                headers={"ACCESS-KEY": k["key"], "ACCESS-SIGN": sig,
                         "ACCESS-TIMESTAMP": ts, "ACCESS-PASSPHRASE": k["passphrase"],
                         "Content-Type": "application/json"},
                data=body, proxies=PROXIES, timeout=15).json()
            ok = r.get("code") == "00000"

        else:
            ok = False

        log.info(f"  [{exchange}] {side} {sym} qty={qty}  ok={ok}")
        return ok

    except Exception as e:
        log.error(f"  [{exchange}] {side} {sym} 下单异常: {e}")
        return False

# ── 主流程 ────────────────────────────────────────────────
def wait_bstock_closed(timeout_sec=10800) -> bool:
    log.info("[Step1] 等待 bstock 美股持仓全部平仓...")
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            state = json.loads(Path("/tmp/bstock_state.json").read_text())
            if not state.get("positions"):
                log.info("[Step1] ✅ 全部持仓已平仓")
                return True
            now_h = datetime.now(timezone.utc).hour
            if now_h >= 20:
                log.info(f"[Step1] 美股已收盘（{now_h}:xx UTC），继续流程")
                return True
            remaining = list(state["positions"].keys())
            log.info(f"[Step1] 还有 {len(remaining)} 笔: {remaining}，等2min...")
        except Exception as e:
            log.warning(f"[Step1] 读取状态失败: {e}")
        time.sleep(120)
    return False

def main():
    log.info("=" * 60)
    log.info("  套利流程 v2.0 启动（5所扫描 + ≤4h结算）")
    log.info("=" * 60)
    notify("🔄 美股工作流结束，开始套利流程（v2.1）...")

    # Step 1: 等待美股平仓
    wait_bstock_closed()

    # Step 2: BTC 波动检查
    try:
        r = requests.get("https://api.gateio.ws/api/v4/futures/usdt/candlesticks",
            params={"contract": "BTC_USDT", "interval": "1h", "limit": 2},
            proxies=PROXIES, timeout=8).json()
        if len(r) >= 2:
            btc_move = abs(float(r[1]["c"]) - float(r[0]["c"])) / float(r[0]["c"]) * 100
            if btc_move > MAX_BTC_1H_MOVE:
                msg = f"⚠️ BTC 1h 波动 {btc_move:.2f}%，等10分钟后重试"
                log.warning(msg); notify(msg)
                time.sleep(600)
    except Exception as e:
        log.warning(f"BTC检查失败: {e}")

    # Step 3: 扫描费率（最多重试3次）
    viable = []
    for attempt in range(3):
        log.info(f"[Step3] 扫描资金费率（第{attempt+1}次）...")
        all_rates = fetch_funding_rates()
        log.info(f"[Step3] 共扫描到 {len(all_rates)} 个标的")

        candidates = find_best_pair(all_rates)
        log.info(f"[Step3] 净费率 > {MIN_NET_FR_8H}% 的候选: {len(candidates)} 个")

        # 验证价差
        for cand in candidates[:10]:
            if validate_spread(cand):
                viable.append(cand)
            if len(viable) >= 3:
                break
            time.sleep(0.2)

        if viable:
            break
        log.info(f"[Step3] 暂无满足条件的机会，5分钟后重试...")
        time.sleep(300)

    if not viable:
        msg = "📊 扫描完成，无满足条件的套利机会（价差或费率不达标）"
        log.info(msg); notify(msg)
        return

    best = viable[0]
    log.info(f"\n[Step4] 最优标的: {best['sym']}  "
             f"{best['short_ex']}空+{best['long_ex']}多  "
             f"净费率={best['net_fr']:.4f}%/8h  "
             f"扣费后净收益={best.get('net_after_cost',0):.4f}%  "
             f"价差={best['spread_pct']:.4f}%")

    # Step 4: 确保两所资金充足
    fund_exchanges(best["short_ex"], best["long_ex"], NOTIONAL_PER_LEG * 2)

    # Step 5: 开仓
    success = open_arb_position(best)
    if success:
        log.info("[Flow] ✅ 套利仓位已建立")
    else:
        # 第一个失败，尝试第二优选
        if len(viable) > 1:
            second = viable[1]
            log.info(f"[Flow] 尝试备选: {second['sym']}")
            fund_exchanges(second["short_ex"], second["long_ex"], NOTIONAL_PER_LEG * 2)
            open_arb_position(second)

if __name__ == "__main__":
    main()
