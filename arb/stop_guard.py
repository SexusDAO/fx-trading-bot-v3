"""
arb/stop_guard.py — 套利仓位双保险止损模块

功能：
1. 【程序止损】每 30 秒检查价格，逼近触发价时并发平仓两腿
2. 【交易所止损】开仓时同步在 Bybit 挂原生止损单（兜底，程序挂了也能执行）

触发逻辑（可配置）：
  - 触发价 = 爆仓价向内缩 SAFETY_BUFFER（默认 20%）
  - 即：1x 杠杆几乎永远不触发；2x 杠杆触发距离 = 开仓价的 30%

使用方式：
  guard = StopGuard(gate_pos, bybit_pos)
  guard.place_exchange_stops()   # 开仓后立刻挂原生止损
  guard.start_monitor()          # 启动后台价格监控线程
  guard.stop()                   # 平仓后停止
"""
import os
import requests, hmac, hashlib, time, json, urllib.parse, threading, math, sys
sys.path.insert(0, "/root/.openclaw/workspace")
from arb.config import (PROXIES, BINANCE_API_KEY, BINANCE_API_SECRET,
                         BYBIT_API_KEY, BYBIT_API_SECRET, GATE_API_KEY, GATE_API_SECRET)

# ── 配置 ──────────────────────────────────────────────────────────────────
SAFETY_BUFFER    = 0.20   # 在爆仓价的 20% 安全距离触发止损
CHECK_INTERVAL   = 30     # 价格检查间隔（秒）
EMERGENCY_BUFFER = 0.05   # 紧急止损缓冲（再进一步时直接市价平仓）
# ─────────────────────────────────────────────────────────────────────────

GATE_KEY    = "31fa24489c3c7d862575696fa7daf2fa"
GATE_SECRET = os.environ.get("ARB_STOP_GUARD_GATE_SECRET", "")
BYBIT_KEY   = "61KSIeSU4lrPmRIlEz"
BYBIT_SEC   = "hgMaDpcYVIctxrfJTjKzbw50ZpSFyBi5gNsa"


def gate_sign(method, path, query="", body=""):
    ts = str(int(time.time()))
    bh = hashlib.sha512(body.encode()).hexdigest()
    msg = "\n".join([method, path, query, bh, ts])
    sig = hmac.new(GATE_SECRET.encode(), msg.encode(), hashlib.sha512).hexdigest()
    return ts, sig


def bybit_req(method, path, data=None):
    body_str = json.dumps(data) if method == "POST" and data else ""
    qs = urllib.parse.urlencode(sorted(data.items())) if method == "GET" and data else ""
    ts = str(int(time.time() * 1000))
    sign_str = ts + BYBIT_KEY + "5000" + (body_str if method == "POST" else qs)
    sig = hmac.new(BYBIT_SEC.encode(), sign_str.encode(), hashlib.sha256).hexdigest()
    headers = {"X-BAPI-API-KEY": BYBIT_KEY, "X-BAPI-TIMESTAMP": ts,
               "X-BAPI-SIGN": sig, "X-BAPI-RECV-WINDOW": "5000",
               "Content-Type": "application/json"}
    if method == "POST":
        return requests.post("https://api.bybit.com" + path, headers=headers,
                             data=body_str, proxies=PROXIES, timeout=15).json()
    return requests.get("https://api.bybit.com" + path, headers=headers,
                        params=data, proxies=PROXIES, timeout=15).json()


def notify(msg: str):
    import subprocess
    subprocess.Popen(
        ["openclaw", "message", "send", "--channel", "jarvis",
         "--to", "432401404:thread:019fcac8-4a93-7bea-a930-7f0e59e4897c",
         "--message", msg],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    print(f"[NOTIFY] {msg}")


class StopGuard:
    """
    双保险止损守卫。

    gate_pos  = {"contract": "XXX_USDT", "size": 0, "entry": 0.0,
                 "liq_price": 0.0, "direction": "long"}
    bybit_pos = {"symbol": "XXXUSDT", "size": 0, "entry": 0.0,
                 "liq_price": 0.0, "direction": "short"}
    """

    def __init__(self, gate_pos: dict, bybit_pos: dict,
                 safety_buffer: float = SAFETY_BUFFER):
        self.gate   = gate_pos
        self.bybit  = bybit_pos
        self.buf    = safety_buffer
        self._stop_event = threading.Event()
        self._thread     = None
        self._triggered  = False

        # 计算止损触发价
        # Gate 做多：价格下跌风险，触发价在强平价上方 buffer
        if float(gate_pos.get("liq_price", 0)) > 0:
            liq = float(gate_pos["liq_price"])
            self.gate_stop_price = round(liq * (1 + self.buf), 8)
        else:
            # 无强平价（1x全仓），用开仓价 × (1 - 80%) 作为极端保底
            self.gate_stop_price = round(float(gate_pos["entry"]) * 0.20, 8)

        # Bybit 做空：价格上涨风险，触发价在强平价下方 buffer
        if float(bybit_pos.get("liq_price", 0)) > 0:
            liq = float(bybit_pos["liq_price"])
            self.bybit_stop_price = round(liq * (1 - self.buf), 8)
        else:
            self.bybit_stop_price = round(float(bybit_pos["entry"]) * 2.5, 8)

        print(f"[StopGuard] 初始化完成")
        print(f"  Gate  做多止损触发价: {self.gate_stop_price:.6f}  "
              f"(强平价: {gate_pos.get('liq_price', 'N/A')})")
        print(f"  Bybit 做空止损触发价: {self.bybit_stop_price:.6f}  "
              f"(强平价: {bybit_pos.get('liq_price', 'N/A')})")

    # ── 交易所原生止损（Bybit 支持，Gate 用条件单）────────────────────────

    def place_exchange_stops(self):
        """在 Bybit 挂原生止损单（做空仓位：止损 = 价格上涨触发 Buy）"""
        sym     = self.bybit["symbol"]
        qty     = str(self.bybit["size"])
        sl_price = str(round(self.bybit_stop_price, 6))

        # Bybit：止损单（triggerBy=LastPrice，达到 sl_price 时市价平仓）
        r = bybit_req("POST", "/v5/order/create", {
            "category":    "linear",
            "symbol":      sym,
            "side":        "Buy",         # 空仓平仓方向
            "orderType":   "Market",
            "qty":         qty,
            "stopLoss":    sl_price,
            "slTriggerBy": "LastPrice",
            "positionIdx": 0,
            "timeInForce": "GTC",
            "reduceOnly":  True,
        })
        if r.get("retCode") == 0:
            print(f"[StopGuard] ✅ Bybit 原生止损已挂: 触发价={sl_price} (空仓平仓)")
        else:
            # Bybit 止损也可以通过 set-trading-stop 设置
            r2 = bybit_req("POST", "/v5/position/trading-stop", {
                "category":    "linear",
                "symbol":      sym,
                "stopLoss":    sl_price,
                "slTriggerBy": "LastPrice",
                "positionIdx": 0,
            })
            if r2.get("retCode") == 0:
                print(f"[StopGuard] ✅ Bybit trading-stop 已设置: SL={sl_price}")
            else:
                print(f"[StopGuard] ⚠️ Bybit 原生止损设置失败: {r.get('retMsg')} / {r2.get('retMsg')}")

        # Gate 条件单（做多仓位止损：价格跌破触发价时平仓）
        stop_body = json.dumps({
            "contract":    self.gate["contract"],
            "size":        -self.gate["size"],   # 负数 = 平多
            "price":       "0",                  # 市价
            "tif":         "ioc",
            "text":        "arb-stop-guard",
            "auto_size":   "",
            "price_type":  "mark",               # 按标记价触发
            "trigger_price": str(round(self.gate_stop_price, 8)),
            "order_type":  "market",
        })
        ts, sig = gate_sign("POST", "/api/v4/futures/usdt/price_orders", body=stop_body)
        r3 = requests.post("https://api.gateio.ws/api/v4/futures/usdt/price_orders",
                           headers={"KEY": GATE_KEY, "SIGN": sig, "Timestamp": ts,
                                    "Content-Type": "application/json"},
                           data=stop_body, proxies=PROXIES, timeout=15)
        resp3 = r3.json()
        if resp3.get("id") or resp3.get("status") == "open":
            print(f"[StopGuard] ✅ Gate 条件止损已挂: 触发价={self.gate_stop_price:.6f}")
        else:
            print(f"[StopGuard] ⚠️ Gate 条件止损设置失败: {resp3}")

    # ── 程序监控（主防线）────────────────────────────────────────────────

    def _get_mark_price(self) -> float:
        try:
            r = requests.get("https://api.bybit.com/v5/market/tickers",
                             params={"category": "linear", "symbol": self.bybit["symbol"]},
                             proxies=PROXIES, timeout=8)
            return float(r.json()["result"]["list"][0]["lastPrice"])
        except:
            return -1

    def _close_both_legs(self, reason: str):
        """并发平仓两腿"""
        import concurrent.futures
        print(f"[StopGuard] 🚨 触发止损！原因: {reason}")
        notify(f"🚨 套利止损触发！{reason}\n正在并发平仓两腿...")

        def close_gate():
            body = json.dumps({
                "contract": self.gate["contract"],
                "size": -self.gate["size"],
                "price": "0", "tif": "ioc", "text": "arb-stop-close",
            })
            ts, sig = gate_sign("POST", "/api/v4/futures/usdt/orders", body=body)
            r = requests.post("https://api.gateio.ws/api/v4/futures/usdt/orders",
                              headers={"KEY": GATE_KEY, "SIGN": sig, "Timestamp": ts,
                                       "Content-Type": "application/json"},
                              data=body, proxies=PROXIES, timeout=15)
            return r.json()

        def close_bybit():
            return bybit_req("POST", "/v5/order/create", {
                "category": "linear", "symbol": self.bybit["symbol"],
                "side": "Buy", "orderType": "Market",
                "qty": str(int(self.bybit["size"])),
                "positionIdx": 0, "timeInForce": "IOC", "reduceOnly": True,
            })

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
            fg = ex.submit(close_gate)
            fb = ex.submit(close_bybit)
            gate_r  = fg.result()
            bybit_r = fb.result()

        notify(f"✅ 止损平仓完成\nGate: {gate_r}\nBybit: {bybit_r.get('retMsg')}")
        self._triggered = True
        self._stop_event.set()

    def _monitor_loop(self):
        print(f"[StopGuard] 监控启动，每 {CHECK_INTERVAL}s 检查价格")
        consecutive_alerts = 0
        while not self._stop_event.is_set() and not self._triggered:
            price = self._get_mark_price()
            if price <= 0:
                time.sleep(CHECK_INTERVAL)
                continue

            # 检查两个方向
            gate_danger  = price <= self.gate_stop_price    # 价格跌破做多止损线
            bybit_danger = price >= self.bybit_stop_price   # 价格涨破做空止损线

            if gate_danger or bybit_danger:
                consecutive_alerts += 1
                direction = "价格下跌" if gate_danger else "价格上涨"
                print(f"[StopGuard] ⚠️ 警报 #{consecutive_alerts}: price={price:.6f} "
                      f"{'< Gate止损' if gate_danger else '> Bybit止损'}")
                if consecutive_alerts >= 2:
                    # 连续2次确认，执行平仓
                    self._close_both_legs(
                        f"{direction} price={price:.6f}，"
                        f"{'低于Gate止损' + str(self.gate_stop_price) if gate_danger else '高于Bybit止损' + str(self.bybit_stop_price)}"
                    )
                    break
                # 第一次告警
                notify(f"⚠️ 套利预警：价格={price:.6f}，"
                       f"逼近{'Gate多仓' if gate_danger else 'Bybit空仓'}止损线！"
                       f"30秒后未恢复将执行平仓。")
            else:
                consecutive_alerts = 0

            time.sleep(CHECK_INTERVAL)

        print("[StopGuard] 监控已停止")

    def start_monitor(self):
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()
        print("[StopGuard] 后台监控线程已启动")

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)


# ── 快速测试（dry run）─────────────────────────────────────────────────────
if __name__ == "__main__":
    # 填入实际开仓参数后再运行
    # gate_pos  = {"contract": "XXX_USDT", "size": 0, "entry": 0.0, "liq_price": 0.0, "direction": "long"}
    # bybit_pos = {"symbol": "XXXUSDT",   "size": 0, "entry": 0.0, "liq_price": 0.0, "direction": "short"}
    print("[StopGuard] 请填入实际仓位参数后运行")
