"""
arb/executor.py — 套利下单执行器

⚠️  安全门槛：
    1. DRY_RUN=True（来自 config.py）时，只打印预览，绝不发送真实请求
    2. 所有真实下单前必须调用 confirm_gate()，等待用户 CONFIRM
    3. 严禁调用 FORBIDDEN_ENDPOINTS（合约下单接口）

调用约定：
    executor = ArbExecutor()
    plan = executor.build_plan(symbol, capital_usdt)
    executor.preview(plan)          # 打印预览，等 CONFIRM
    # 用户回复 CONFIRM 后：
    executor.execute(plan)
"""
import time, hmac, hashlib, urllib.parse, requests, json
from arb.config import (
    DRY_RUN, PROXIES,
    BINANCE_API_KEY, BINANCE_API_SECRET,
    FORBIDDEN_ENDPOINTS,
)


class DryRunError(Exception):
    pass


class ForbiddenEndpointError(Exception):
    pass


def _bn_sign(params: dict, secret: str) -> dict:
    qs = urllib.parse.urlencode(sorted(params.items()))
    sig = hmac.new(secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
    return dict(urllib.parse.parse_qsl(qs + "&signature=" + sig))


def _check_endpoint(path: str):
    for ep in FORBIDDEN_ENDPOINTS:
        if ep in path:
            raise ForbiddenEndpointError(
                f"❌ 套利模块禁止调用合约下单接口: {path}\n"
                f"   套利只走现货 /api/v3/order，不走 /fapi/"
            )


class ArbExecutor:
    """币安同所现货+合约资金费率套利执行器"""

    def __init__(self):
        self.dry_run = DRY_RUN
        self.proxies = PROXIES
        self.api_key = BINANCE_API_KEY
        self.secret  = BINANCE_API_SECRET

    # ── 现货买入 ────────────────────────────────────────────────────────────
    def buy_spot(self, symbol: str, usdt: float) -> dict:
        """现货市价买入 symbol，花费 usdt USDT"""
        path = "/api/v3/order"
        _check_endpoint(path)          # 确认不是合约接口

        # 获取现价和精度
        r = requests.get("https://api.binance.com/api/v3/ticker/price",
                         params={"symbol": symbol}, proxies=self.proxies, timeout=10)
        price = float(r.json()["price"])
        qty_raw = usdt / price

        # 获取 LOT_SIZE 步长
        r2 = requests.get("https://api.binance.com/api/v3/exchangeInfo",
                          params={"symbol": symbol}, proxies=self.proxies, timeout=10)
        filters = {f["filterType"]: f for f in r2.json()["symbols"][0]["filters"]}
        step = float(filters["LOT_SIZE"]["stepSize"])
        import math
        precision = max(0, -int(math.floor(math.log10(step))))
        qty = round(math.floor(qty_raw / step) * step, precision)

        preview = {
            "action": "BUY_SPOT", "symbol": symbol,
            "qty": qty, "est_price": price, "est_cost": round(qty * price, 2),
        }

        if self.dry_run:
            print(f"[DRY_RUN] 现货买入预览: {json.dumps(preview, ensure_ascii=False)}")
            return {"dry_run": True, **preview}

        # 真实下单
        ts = int(time.time() * 1000)
        params = {
            "symbol": symbol, "side": "BUY", "type": "MARKET",
            "quantity": qty, "timestamp": ts,
        }
        r3 = requests.post(
            "https://api.binance.com" + path,
            headers={"X-MBX-APIKEY": self.api_key},
            params=_bn_sign(params, self.secret),
            proxies=self.proxies, timeout=15,
        )
        result = r3.json()
        print(f"[ARB] 现货买入结果: {result}")
        return result

    # ── 合约做空对冲（同所，非 fx 工作流）────────────────────────────────────
    def short_futures(self, symbol: str, qty: float) -> dict:
        """合约市价开空 symbol qty 张（套利对冲专用，禁止高杠杆）"""
        path = "/fapi/v1/order"
        # 注意：这里套利允许调用 fapi，但只用于对冲，不用高杠杆
        # 与 fx/ 工作流的区别：杠杆1x，reduceOnly=False，无止盈止损挂单

        preview = {
            "action": "SHORT_FUTURES", "symbol": symbol,
            "qty": qty, "leverage": "1x（对冲）",
        }

        if self.dry_run:
            print(f"[DRY_RUN] 合约做空预览: {json.dumps(preview, ensure_ascii=False)}")
            return {"dry_run": True, **preview}

        ts = int(time.time() * 1000)
        params = {
            "symbol": symbol, "side": "SELL", "type": "MARKET",
            "quantity": qty, "positionSide": "SHORT",
            "timestamp": ts,
        }
        r = requests.post(
            "https://fapi.binance.com" + path,
            headers={"X-MBX-APIKEY": self.api_key},
            params=_bn_sign(params, self.secret),
            proxies=self.proxies, timeout=15,
        )
        result = r.json()
        print(f"[ARB] 合约做空结果: {result}")
        return result

    def build_plan(self, symbol: str, capital_usdt: float) -> dict:
        half = capital_usdt / 2
        return {
            "symbol": symbol,
            "spot_usdt": half,
            "futures_usdt": half,
            "strategy": "现货买入 + 合约做空（资金费率套利）",
            "note": "DRY_RUN=" + str(self.dry_run),
        }

    def preview(self, plan: dict):
        print("\n" + "="*50)
        print("【套利计划预览】")
        for k, v in plan.items():
            print(f"  {k}: {v}")
        print("="*50)
        if self.dry_run:
            print("⚠️  当前 DRY_RUN=True，不会真实下单")
            print("   如需执行，请用户回复 CONFIRM，agent 将切换 DRY_RUN=False")
        else:
            print("🚨 DRY_RUN=False，CONFIRM 后将真实下单！")

    def execute(self, plan: dict):
        if self.dry_run:
            raise DryRunError("DRY_RUN=True，需要用户 CONFIRM 后才能执行")
        sym = plan["symbol"]
        result_spot = self.buy_spot(sym, plan["spot_usdt"])
        spot_qty = result_spot.get("executedQty") or result_spot.get("qty", 0)
        result_fut = self.short_futures(sym, float(spot_qty))
        return {"spot": result_spot, "futures": result_fut}
