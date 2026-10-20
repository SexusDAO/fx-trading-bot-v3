"""
arb/entry_engine.py  —  资金费率套利开仓引擎 v2.0
=====================================================
核心改进（v2.0 vs v1.0）：
  1. 两腿锚定同一 ref_price（两所价差的中点），挂价差 < offset×2
  2. 贴价限价 = 直接挂 bid/ask（不额外偏移），maker 概率最大化
  3. 超时追价改为先 cancel+replace 到当前 bid/ask（5s再判），
     仍不成交才改市价 → 绝大多数情况保持 maker，省手续费
  4. 成交后名义量对齐：差 > 2 张时补仓
  5. 全程异常隔离：任一步失败立即平掉已成交腿再报错
"""

import time
import math
import threading
import requests
import hmac
import hashlib
import json
import urllib.parse
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("arb.entry_engine")


# ── 配置 ──────────────────────────────────────────────────
@dataclass
class EntryConfig:
    # 价差门控
    max_spread_pct:      float = 0.10   # 两所跨交所价差 > 0.10% 不入场
    abort_spread_pct:    float = 0.20   # 成交后价差 > 0.20% 立即平仓

    # 市场环境过滤
    max_btc_1h_move_pct: float = 1.50   # BTC 1h 涨跌幅 > 1.5% 暂缓
    max_ask_bid_spread:  float = 0.10   # 标的 ask-bid spread > 0.1% 跳过

    # 限价单参数
    # v2: offset=0 表示直接挂在 bid/ask 上（最优档，maker 概率最大）
    limit_offset_pct:    float = 0.00   # 0 = 贴买一/卖一价

    # 超时策略（v2: 两步超时）
    limit_timeout_sec:   int   = 45     # 第一步：等 45s 看是否成交
    replace_wait_sec:    int   = 10     # 第二步：cancel+replace 后再等 10s
    # 超过以上两步仍未成交 → 市价追单

    # 名义量对齐阈值（张数差超过此值则补仓）
    size_align_threshold: int  = 2

    # 杠杆
    leverage:            int   = 2

    # 手续费率
    gate_maker_fee:      float = 0.00015   # 0.015%
    gate_taker_fee:      float = 0.0005    # 0.050%
    bybit_maker_fee:     float = 0.0002    # 0.020%
    bybit_taker_fee:     float = 0.00055   # 0.055%


# ── Gate 客户端 ───────────────────────────────────────────
class GateClient:
    BASE = "https://api.gateio.ws"

    def __init__(self, key: str, secret: str, proxies: dict):
        self.key     = key
        self.secret  = secret
        self.proxies = proxies

    def _sign(self, method, path, query="", body=""):
        ts  = str(int(time.time()))
        bh  = hashlib.sha512(body.encode()).hexdigest()
        msg = "\n".join([method, path, query, bh, ts])
        sig = hmac.new(self.secret.encode(), msg.encode(), hashlib.sha512).hexdigest()
        return ts, sig

    def get_orderbook(self, sym: str) -> dict:
        r = requests.get(
            f"{self.BASE}/api/v4/futures/usdt/order_book",
            params={"contract": f"{sym}_USDT", "limit": 1},
            proxies=self.proxies, timeout=5,
        )
        d = r.json()
        best_bid = float(d["bids"][0]["p"]) if d.get("bids") else 0.0
        best_ask = float(d["asks"][0]["p"]) if d.get("asks") else 0.0
        return {"bid": best_bid, "ask": best_ask}

    def get_btc_1h_change(self) -> float:
        r = requests.get(
            f"{self.BASE}/api/v4/futures/usdt/candlesticks",
            params={"contract": "BTC_USDT", "interval": "1h", "limit": 2},
            proxies=self.proxies, timeout=5,
        )
        candles = r.json()
        if len(candles) < 2:
            return 0.0
        return abs(float(candles[1]["c"]) - float(candles[0]["c"])) / float(candles[0]["c"]) * 100

    def set_leverage(self, sym: str, lev: int):
        body = json.dumps({"leverage": str(lev), "cross_leverage_limit": "0"})
        ts, sig = self._sign("POST", f"/api/v4/futures/usdt/positions/{sym}_USDT/leverage", "", body)
        requests.post(
            f"{self.BASE}/api/v4/futures/usdt/positions/{sym}_USDT/leverage",
            headers={"KEY": self.key, "SIGN": sig, "Timestamp": ts, "Content-Type": "application/json"},
            data=body, proxies=self.proxies, timeout=10,
        )

    def place_limit(self, sym: str, size: int, price: float) -> str:
        body = json.dumps({
            "contract":    f"{sym}_USDT",
            "size":        size,
            "price":       str(round(price, 4)),
            "tif":         "gtc",
            "reduce_only": False,
            "text":        "t-arb-v2",
        })
        ts, sig = self._sign("POST", "/api/v4/futures/usdt/orders", "", body)
        r = requests.post(
            f"{self.BASE}/api/v4/futures/usdt/orders",
            headers={"KEY": self.key, "SIGN": sig, "Timestamp": ts, "Content-Type": "application/json"},
            data=body, proxies=self.proxies, timeout=10,
        ).json()
        return str(r.get("id", ""))

    def replace_order(self, sym: str, order_id: str, size: int, new_price: float) -> str:
        """撤旧单挂新单（Gate 无 amend 接口，只能撤+重挂）"""
        self.cancel_order(order_id)
        time.sleep(0.3)
        return self.place_limit(sym, size, new_price)

    def place_market(self, sym: str, size: int) -> str:
        body = json.dumps({
            "contract": f"{sym}_USDT",
            "size":     size,
            "price":    "0",
            "tif":      "ioc",
            "text":     "t-arb-chase",
        })
        ts, sig = self._sign("POST", "/api/v4/futures/usdt/orders", "", body)
        r = requests.post(
            f"{self.BASE}/api/v4/futures/usdt/orders",
            headers={"KEY": self.key, "SIGN": sig, "Timestamp": ts, "Content-Type": "application/json"},
            data=body, proxies=self.proxies, timeout=10,
        ).json()
        return str(r.get("id", ""))

    def get_order(self, order_id: str) -> dict:
        ts, sig = self._sign("GET", f"/api/v4/futures/usdt/orders/{order_id}")
        return requests.get(
            f"{self.BASE}/api/v4/futures/usdt/orders/{order_id}",
            headers={"KEY": self.key, "SIGN": sig, "Timestamp": ts},
            proxies=self.proxies, timeout=10,
        ).json()

    def cancel_order(self, order_id: str):
        ts, sig = self._sign("DELETE", f"/api/v4/futures/usdt/orders/{order_id}")
        requests.delete(
            f"{self.BASE}/api/v4/futures/usdt/orders/{order_id}",
            headers={"KEY": self.key, "SIGN": sig, "Timestamp": ts},
            proxies=self.proxies, timeout=10,
        )

    def get_position(self, sym: str) -> dict:
        ts, sig = self._sign("GET", f"/api/v4/futures/usdt/positions/{sym}_USDT")
        r = requests.get(
            f"{self.BASE}/api/v4/futures/usdt/positions/{sym}_USDT",
            headers={"KEY": self.key, "SIGN": sig, "Timestamp": ts},
            proxies=self.proxies, timeout=10,
        ).json()
        return {"size": int(r.get("size", 0)), "entry": float(r.get("entry_price", 0))}


# ── Bybit 客户端 ──────────────────────────────────────────
class BybitClient:
    BASE = "https://api.bybit.com"

    def __init__(self, key: str, secret: str, proxies: dict):
        self.key     = key
        self.secret  = secret
        self.proxies = proxies

    def _req(self, method, path, data=None):
        body_str = json.dumps(data) if method == "POST" and data else ""
        qs       = urllib.parse.urlencode(sorted(data.items())) if method == "GET" and data else ""
        ts       = str(int(time.time() * 1000))
        sign_str = ts + self.key + "5000" + (body_str if method == "POST" else qs)
        sig      = hmac.new(self.secret.encode(), sign_str.encode(), hashlib.sha256).hexdigest()
        headers  = {
            "X-BAPI-API-KEY":      self.key,
            "X-BAPI-TIMESTAMP":    ts,
            "X-BAPI-SIGN":         sig,
            "X-BAPI-RECV-WINDOW":  "5000",
            "Content-Type":        "application/json",
        }
        if method == "POST":
            return requests.post(f"{self.BASE}{path}", headers=headers,
                                 data=body_str, proxies=self.proxies, timeout=10).json()
        return requests.get(f"{self.BASE}{path}", headers=headers,
                            params=data, proxies=self.proxies, timeout=10).json()

    def get_orderbook(self, sym: str) -> dict:
        r = self._req("GET", "/v5/market/orderbook", {"category": "linear",
                                                       "symbol": f"{sym}USDT", "limit": 1})
        bids = r["result"]["b"]
        asks = r["result"]["a"]
        return {
            "bid": float(bids[0][0]) if bids else 0.0,
            "ask": float(asks[0][0]) if asks else 0.0,
        }

    def set_leverage(self, sym: str, lev: int):
        self._req("POST", "/v5/position/set-leverage", {
            "category":    "linear",
            "symbol":      f"{sym}USDT",
            "buyLeverage": str(lev),
            "sellLeverage":str(lev),
        })

    def place_limit(self, sym: str, size: int, price: float, side: str = "Sell") -> str:
        r = self._req("POST", "/v5/order/create", {
            "category":    "linear",
            "symbol":      f"{sym}USDT",
            "side":        side,
            "orderType":   "Limit",
            "qty":         str(size),
            "price":       str(round(price, 4)),
            "timeInForce": "GTC",
            "reduceOnly":  False,
            "positionIdx": 0,
        })
        return r.get("result", {}).get("orderId", "")

    def amend_order(self, sym: str, order_id: str, new_price: float) -> bool:
        """Bybit 支持 amend，直接改价不撤单（无空窗期）"""
        r = self._req("POST", "/v5/order/amend", {
            "category": "linear",
            "symbol":   f"{sym}USDT",
            "orderId":  order_id,
            "price":    str(round(new_price, 4)),
        })
        return r.get("retCode", -1) == 0

    def place_market(self, sym: str, size: int, side: str = "Sell") -> str:
        r = self._req("POST", "/v5/order/create", {
            "category":    "linear",
            "symbol":      f"{sym}USDT",
            "side":        side,
            "orderType":   "Market",
            "qty":         str(size),
            "timeInForce": "IOC",
            "reduceOnly":  False,
            "positionIdx": 0,
        })
        return r.get("result", {}).get("orderId", "")

    def get_order(self, sym: str, order_id: str) -> dict:
        r = self._req("GET", "/v5/order/realtime",
                      {"category": "linear", "symbol": f"{sym}USDT", "orderId": order_id})
        lst = r.get("result", {}).get("list", [])
        if not lst:
            r2 = self._req("GET", "/v5/order/history",
                           {"category": "linear", "symbol": f"{sym}USDT", "orderId": order_id})
            lst = r2.get("result", {}).get("list", [])
        return lst[0] if lst else {}

    def get_position(self, sym: str) -> dict:
        r = self._req("GET", "/v5/position/list",
                      {"category": "linear", "symbol": f"{sym}USDT"})
        p = r["result"]["list"][0] if r["result"]["list"] else {}
        return {"size": float(p.get("size", 0)), "entry": float(p.get("avgPrice", 0))}


# ── 结果 ─────────────────────────────────────────────────
@dataclass
class EntryResult:
    success:        bool
    gate_entry:     float = 0.0
    bybit_entry:    float = 0.0
    gate_size:      int   = 0
    bybit_size:     int   = 0
    spread_pct:     float = 0.0
    gate_fee:       float = 0.0
    bybit_fee:      float = 0.0
    total_fee:      float = 0.0
    gate_is_maker:  bool  = True
    bybit_is_maker: bool  = True
    abort_reason:   str   = ""


# ── 内部：轮询单个订单直到成交或超时 ─────────────────────
def _wait_fill(label: str, poll_fn, timeout_sec: int, interval: float = 3.0):
    """
    poll_fn() 应返回 (filled: bool, fill_price: float)
    返回 (filled, fill_price)
    """
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        filled, px = poll_fn()
        if filled:
            return True, px
        time.sleep(interval)
    return False, 0.0


# ── 主入口 ────────────────────────────────────────────────
def open_position(
    sym:       str,
    notional:  float,
    gate_cli:  GateClient,
    bybit_cli: BybitClient,
    cfg:       EntryConfig = None,
    dry_run:   bool = False,
) -> EntryResult:
    """
    双腿对冲开仓（v2.0）：
      gate  做多（合约 long）
      bybit 做空（合约 short）

    dry_run=True 只打印预估，不实际下单。
    """
    if cfg is None:
        cfg = EntryConfig()

    # ── Step 1: BTC 1h 波动 ───────────────────────────────
    btc_move = gate_cli.get_btc_1h_change()
    if btc_move > cfg.max_btc_1h_move_pct:
        logger.warning(f"[entry] ❌ BTC 1h={btc_move:.2f}% 超阈值，暂缓")
        return EntryResult(False, abort_reason=f"BTC_MOVE_{btc_move:.2f}%")

    # ── Step 2: Orderbook ────────────────────────────────
    g_ob = gate_cli.get_orderbook(sym)
    b_ob = bybit_cli.get_orderbook(sym)

    if not g_ob["bid"] or not b_ob["bid"]:
        return EntryResult(False, abort_reason="ORDERBOOK_EMPTY")

    g_spd = (g_ob["ask"] - g_ob["bid"]) / g_ob["ask"] * 100
    b_spd = (b_ob["ask"] - b_ob["bid"]) / b_ob["ask"] * 100
    if g_spd > cfg.max_ask_bid_spread or b_spd > cfg.max_ask_bid_spread:
        return EntryResult(False, abort_reason=f"SPREAD_WIDE_G{g_spd:.3f}%_B{b_spd:.3f}%")

    # ── Step 3: 跨所价差（Gate 多腿吃 ask，Bybit 空腿吃 bid）──
    # 关注 Gate ask（我们需要以这个价或以下买入）
    # 关注 Bybit bid（我们需要以这个价或以上卖出）
    cross_spread = (g_ob["ask"] - b_ob["bid"]) / b_ob["bid"] * 100
    if cross_spread > cfg.max_spread_pct:
        logger.warning(f"[entry] ❌ 跨所价差 {cross_spread:.4f}% 超阈值")
        return EntryResult(False, abort_reason=f"CROSS_SPREAD_{cross_spread:.4f}%")

    # ── Step 4: 计算挂单价（v2 核心：两腿锚定同一 ref_price）──
    # ref_price = 两所中点，两腿分别在 ref 两侧挂
    ref_price = (g_ob["ask"] + b_ob["bid"]) / 2

    # Gate 多腿：挂在 Gate bid（买一），等对手方来成交 → maker
    # Bybit 空腿：挂在 Bybit ask（卖一），等对手方来成交 → maker
    # 如果 ref_price 更好，就用 ref_price（对自己更有利但仍 maker）
    g_limit = min(g_ob["bid"], ref_price)   # 不高于 ask，且贴近 bid
    b_limit = max(b_ob["ask"], ref_price)   # 不低于 bid，且贴近 ask

    # 计算开仓量
    g_size = math.floor(notional / (ref_price * 1))   # Gate lot=1
    b_size = math.floor(notional / ref_price)

    logger.info(
        f"[entry] ref={ref_price:.4f}  Gate多@{g_limit:.4f}({g_size}张)  "
        f"Bybit空@{b_limit:.4f}({b_size}张)  跨所价差={cross_spread:.4f}%  BTC1h={btc_move:.2f}%"
    )

    if dry_run:
        g_fee = ref_price * g_size * cfg.gate_maker_fee
        b_fee = ref_price * b_size * cfg.bybit_maker_fee
        print(f"\n[DRY RUN] 不下单，仅预估：")
        print(f"  ref_price = {ref_price:.4f}")
        print(f"  Gate  多 {g_size}张 @ {g_limit:.4f}")
        print(f"  Bybit 空 {b_size}张 @ {b_limit:.4f}")
        print(f"  预估开仓手续费(全maker): Gate {g_fee:.3f}U + Bybit {b_fee:.3f}U = {g_fee+b_fee:.3f}U")
        print(f"  预估四笔(开+平): {(g_fee+b_fee)*2:.3f}U")
        return EntryResult(True, gate_entry=g_limit, bybit_entry=b_limit,
                           gate_size=g_size, bybit_size=b_size,
                           gate_fee=g_fee, bybit_fee=b_fee, total_fee=g_fee+b_fee)

    # ── Step 5: 设杠杆 ────────────────────────────────────
    gate_cli.set_leverage(sym, cfg.leverage)
    bybit_cli.set_leverage(sym, cfg.leverage)

    # ── Step 6: 并发下单 ──────────────────────────────────
    g_oid = b_oid = ""
    g_err = b_err = None

    def _gate():
        nonlocal g_oid, g_err
        try:
            g_oid = gate_cli.place_limit(sym, g_size, g_limit)
        except Exception as e:
            g_err = e

    def _bybit():
        nonlocal b_oid, b_err
        try:
            b_oid = bybit_cli.place_limit(sym, b_size, b_limit, side="Sell")
        except Exception as e:
            b_err = e

    t1 = threading.Thread(target=_gate)
    t2 = threading.Thread(target=_bybit)
    t1.start(); t2.start()
    t1.join();  t2.join()

    def _emergency_close(reason):
        """紧急撤销/平仓，保证不留单腿敞口"""
        logger.error(f"[entry] ⚠️ 紧急处理: {reason}")
        try:
            if g_oid:
                gate_cli.cancel_order(g_oid)
        except Exception:
            pass
        try:
            if b_oid:
                bybit_cli._req("POST", "/v5/order/cancel",
                               {"category": "linear", "symbol": f"{sym}USDT", "orderId": b_oid})
        except Exception:
            pass
        # 如果已经有仓位（追价后），平掉
        try:
            gp = gate_cli.get_position(sym)
            if abs(gp["size"]) > 0:
                gate_cli.place_market(sym, -gp["size"])
        except Exception:
            pass
        try:
            bp = bybit_cli.get_position(sym)
            if bp["size"] > 0:
                bybit_cli.place_market(sym, int(bp["size"]), side="Buy")
        except Exception:
            pass

    if g_err or b_err:
        _emergency_close(f"下单失败 gate={g_err} bybit={b_err}")
        return EntryResult(False, abort_reason="PLACE_FAILED")

    logger.info(f"[entry] 限价单已挂 gate_oid={g_oid}  bybit_oid={b_oid}")

    # ── Step 7: 第一等待期（limit_timeout_sec）────────────
    g_filled = b_filled = False
    g_fill_px = b_fill_px = 0.0
    g_is_maker = b_is_maker = True

    deadline1 = time.time() + cfg.limit_timeout_sec
    while time.time() < deadline1:
        if not g_filled:
            go = gate_cli.get_order(g_oid)
            if go.get("status") == "finished":
                g_filled  = True
                g_fill_px = float(go.get("fill_price", 0))
                logger.info(f"[entry] ✅ Gate 成交 @ {g_fill_px}")
        if not b_filled:
            bo = bybit_cli.get_order(sym, b_oid)
            if bo.get("orderStatus") == "Filled":
                b_filled  = True
                b_fill_px = float(bo.get("avgPrice", 0))
                logger.info(f"[entry] ✅ Bybit 成交 @ {b_fill_px}")
        if g_filled and b_filled:
            break
        time.sleep(3)

    # ── Step 8: 第二步 — cancel+replace 到当前 bid/ask ───
    if not g_filled or not b_filled:
        logger.info("[entry] 第一等待期超时，执行 cancel+replace 到当前 bid/ask")
        # 获取最新 orderbook
        g_ob2 = gate_cli.get_orderbook(sym)
        b_ob2 = bybit_cli.get_orderbook(sym)

        if not g_filled:
            # Gate: 撤旧单，在当前 ask 位置重挂（贴最优 ask，成交概率极高）
            new_g_price = g_ob2["ask"]
            g_oid = gate_cli.replace_order(sym, g_oid, g_size, new_g_price)
            logger.info(f"[entry] Gate 重挂 @ {new_g_price:.4f}")

        if not b_filled:
            # Bybit: amend 到当前 bid（不撤单，无空窗期）
            new_b_price = b_ob2["bid"]
            ok = bybit_cli.amend_order(sym, b_oid, new_b_price)
            logger.info(f"[entry] Bybit amend @ {new_b_price:.4f} ok={ok}")

        # 等 replace_wait_sec
        deadline2 = time.time() + cfg.replace_wait_sec
        while time.time() < deadline2:
            if not g_filled:
                go = gate_cli.get_order(g_oid)
                if go.get("status") == "finished":
                    g_filled  = True
                    g_fill_px = float(go.get("fill_price", 0))
                    logger.info(f"[entry] ✅ Gate(replace) 成交 @ {g_fill_px}")
            if not b_filled:
                bo = bybit_cli.get_order(sym, b_oid)
                if bo.get("orderStatus") == "Filled":
                    b_filled  = True
                    b_fill_px = float(bo.get("avgPrice", 0))
                    logger.info(f"[entry] ✅ Bybit(amend) 成交 @ {b_fill_px}")
            if g_filled and b_filled:
                break
            time.sleep(2)

    # ── Step 9: 最后手段 — 市价追单 ──────────────────────
    if not g_filled:
        logger.warning("[entry] Gate 仍未成交，市价追单")
        gate_cli.cancel_order(g_oid)
        time.sleep(0.3)
        gate_cli.place_market(sym, g_size)
        g_is_maker = False
        for _ in range(10):
            time.sleep(2)
            pos = gate_cli.get_position(sym)
            if abs(pos["size"]) >= g_size * 0.9:
                g_fill_px = pos["entry"]
                g_filled  = True
                break

    if not b_filled:
        logger.warning("[entry] Bybit 仍未成交，市价追单")
        bybit_cli._req("POST", "/v5/order/cancel",
                       {"category": "linear", "symbol": f"{sym}USDT", "orderId": b_oid})
        time.sleep(0.3)
        bybit_cli.place_market(sym, b_size, side="Sell")
        b_is_maker = False
        for _ in range(10):
            time.sleep(2)
            pos = bybit_cli.get_position(sym)
            if pos["size"] >= b_size * 0.9:
                b_fill_px = pos["entry"]
                b_filled  = True
                break

    if not (g_filled and b_filled):
        _emergency_close("追价失败，需人工介入")
        return EntryResult(False, abort_reason="CHASE_FAILED_MANUAL")

    # ── Step 10: 验证成交价差 ──────────────────────────────
    actual_spread = abs(g_fill_px - b_fill_px) / ((g_fill_px + b_fill_px) / 2) * 100
    if actual_spread > cfg.abort_spread_pct:
        logger.error(f"[entry] ❌ 成交价差 {actual_spread:.4f}% 超阈值，立即平仓")
        _emergency_close(f"FILL_SPREAD_{actual_spread:.4f}%")
        return EntryResult(False, abort_reason=f"FILL_SPREAD_{actual_spread:.4f}%")

    # ── Step 11: 名义量对齐 ────────────────────────────────
    gp = gate_cli.get_position(sym)
    bp = bybit_cli.get_position(sym)
    g_actual = abs(gp["size"])
    b_actual  = int(bp["size"])
    diff = abs(g_actual - b_actual)
    if diff > cfg.size_align_threshold:
        logger.warning(f"[entry] 名义量不对齐 Gate={g_actual} Bybit={b_actual}，补仓 {diff}张")
        if g_actual > b_actual:
            bybit_cli.place_market(sym, diff, side="Sell")
        else:
            gate_cli.place_market(sym, diff)
        b_is_maker = False  # 补仓是市价

    # ── Step 12: 计算手续费 ────────────────────────────────
    g_fee = g_fill_px * g_size * (cfg.gate_maker_fee  if g_is_maker else cfg.gate_taker_fee)
    b_fee = b_fill_px * b_size * (cfg.bybit_maker_fee if b_is_maker else cfg.bybit_taker_fee)

    logger.info(
        f"[entry] ✅ 开仓完成  Gate@{g_fill_px:.4f}({'M' if g_is_maker else 'T'})  "
        f"Bybit@{b_fill_px:.4f}({'M' if b_is_maker else 'T'})  "
        f"价差={actual_spread:.4f}%  手续费={g_fee+b_fee:.3f}U"
    )

    return EntryResult(
        success=True,
        gate_entry=g_fill_px, bybit_entry=b_fill_px,
        gate_size=g_size,     bybit_size=b_size,
        spread_pct=actual_spread,
        gate_fee=g_fee,       bybit_fee=b_fee,
        total_fee=g_fee + b_fee,
        gate_is_maker=g_is_maker,
        bybit_is_maker=b_is_maker,
    )


# ── CLI 快速测试 ──────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    PROXY = "socks5://REDACTED_PROXY_CREDENTIALS"
    PROXIES = {"https": PROXY, "http": PROXY}

    gate  = GateClient(
        "31fa24489c3c7d862575696fa7daf2fa",
        "cd7e53b368a7d91779cc32d66699de7073cc8ed743c72d422c3ec73ef20afac0",
        PROXIES,
    )
    bybit = BybitClient(
        "61KSIeSU4lrPmRIlEz",
        "hgMaDpcYVIctxrfJTjKzbw50ZpSFyBi5gNsa",
        PROXIES,
    )

    g_ob  = gate.get_orderbook("GAS")
    b_ob  = bybit.get_orderbook("GAS")
    btc   = gate.get_btc_1h_change()

    ref   = (g_ob["ask"] + b_ob["bid"]) / 2
    cross = (g_ob["ask"] - b_ob["bid"]) / b_ob["bid"] * 100
    g_spd = (g_ob["ask"] - g_ob["bid"]) / g_ob["ask"] * 100
    b_spd = (b_ob["ask"] - b_ob["bid"]) / b_ob["ask"] * 100

    print(f"\n{'='*50}")
    print(f"  GAS  Gate  bid={g_ob['bid']:.4f}  ask={g_ob['ask']:.4f}  spread={g_spd:.4f}%")
    print(f"  GAS  Bybit bid={b_ob['bid']:.4f}  ask={b_ob['ask']:.4f}  spread={b_spd:.4f}%")
    print(f"  ref_price={ref:.4f}  跨所价差={cross:.4f}%  BTC 1h={btc:.2f}%")
    print(f"{'='*50}")

    cfg = EntryConfig()
    issues = []
    if cross > cfg.max_spread_pct:
        issues.append(f"跨所价差 {cross:.4f}% > {cfg.max_spread_pct}%")
    if btc > cfg.max_btc_1h_move_pct:
        issues.append(f"BTC 1h {btc:.2f}% > {cfg.max_btc_1h_move_pct}%")
    if g_spd > cfg.max_ask_bid_spread:
        issues.append(f"Gate spread {g_spd:.4f}% > {cfg.max_ask_bid_spread}%")
    if b_spd > cfg.max_ask_bid_spread:
        issues.append(f"Bybit spread {b_spd:.4f}% > {cfg.max_ask_bid_spread}%")

    if issues:
        print(f"  ❌ 当前不满足入场条件：")
        for i in issues:
            print(f"     · {i}")
    else:
        print(f"  ✅ 条件满足，执行 dry_run 预估：\n")
        open_position("GAS", 3500, gate, bybit, cfg=cfg, dry_run=True)
