"""
order_registry.py — 统一挂单注册表
所有脚本挂单前登记，撤单前查表确认，防止误撤保护性挂单。

用法：
  from order_registry import registry
  registry.register(order_id, symbol, side, role, source, price, qty)
  registry.is_protected(order_id)  → True/False
  registry.remove(order_id)
  registry.get_all()               → list[dict]
  registry.sync_from_states()      → 从所有 state 文件重建注册表
"""
import json, os, time, fcntl
from typing import Optional

REGISTRY_FILE = "/root/.openclaw/workspace/ws_cache/order_registry.json"
os.makedirs(os.path.dirname(REGISTRY_FILE), exist_ok=True)

# 受保护的角色（不可被其他脚本撤销）
PROTECTED_ROLES = {"tp1", "tp2", "tp3", "sl", "tp", "sl_algo"}

# state 文件列表（新增监控脚本时在此追加）
STATE_FILES = [
    "/root/.openclaw/workspace/on_monitor_state.json",    # ON 空单
    "/root/.openclaw/workspace/on_long_state.json",       # ON 多单
]

# state 文件里的订单字段映射
STATE_ORDER_FIELDS = {
    "tp1_order":  "tp1",
    "tp2_order":  "tp2",
    "tp3_order":  "tp3",
    "tp_order":   "tp",
    "sl_order_id": "sl",
}


class OrderRegistry:
    def __init__(self):
        self._load()

    def _load(self):
        try:
            with open(REGISTRY_FILE) as f:
                self._data = json.load(f)
        except Exception:
            self._data = {}

    def _save(self):
        os.makedirs(os.path.dirname(REGISTRY_FILE), exist_ok=True)
        tmp = REGISTRY_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, REGISTRY_FILE)

    def register(self, order_id: str, symbol: str, side: str,
                 role: str, source: str, price: float = 0.0, qty: float = 0.0):
        """登记一个挂单"""
        self._load()
        self._data[str(order_id)] = {
            "symbol":     symbol,
            "side":       side,
            "role":       role,       # tp1/tp2/tp3/sl/sl_algo/entry
            "source":     source,     # on_long_monitor/on_monitor/auto_open/...
            "price":      price,
            "qty":        qty,
            "registered_at": int(time.time()),
        }
        self._save()

    def remove(self, order_id: str):
        """移除登记（成交/手动撤销后调用）"""
        self._load()
        self._data.pop(str(order_id), None)
        self._save()

    def is_protected(self, order_id: str) -> bool:
        """是否受保护（不可被其他脚本撤销）"""
        self._load()
        entry = self._data.get(str(order_id))
        if not entry:
            return False
        return entry.get("role", "") in PROTECTED_ROLES

    def get_all(self) -> list:
        self._load()
        return [{"order_id": k, **v} for k, v in self._data.items()]

    def get_protected_ids(self) -> set:
        """返回所有受保护的订单 ID 集合"""
        self._load()
        return {k for k, v in self._data.items()
                if v.get("role", "") in PROTECTED_ROLES}

    def sync_from_states(self):
        """从所有 state 文件重建注册表（启动时/修复时调用）"""
        self._load()
        for path in STATE_FILES:
            try:
                with open(path) as f:
                    st = json.load(f)
                sym = st.get("symbol", "ONUSDT")
                src = os.path.basename(path).replace(".json", "")
                for field, role in STATE_ORDER_FIELDS.items():
                    oid = st.get(field)
                    if oid and str(oid) not in self._data:
                        self.register(
                            order_id=str(oid),
                            symbol=sym,
                            side="SELL" if st.get("direction") == "LONG" else "BUY",
                            role=role,
                            source=src,
                            price=st.get(f"{field.replace('_order','')}_price", 0),
                        )
            except Exception:
                pass
        self._save()


# 单例
registry = OrderRegistry()
# 启动时同步
registry.sync_from_states()
