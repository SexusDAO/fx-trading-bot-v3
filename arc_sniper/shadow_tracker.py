"""
Shadow Tracker — 影子表现跟踪
=================================

记录每个评分过的代币（无论买入还是跳过），定期回查价格。
用来验证评分系统是否有效：
    - 买入组涨了 → 评分准
    - 跳过组涨了 → 评分太严
    - 买入组跌了 → 评分太松

跟踪窗口: 5min / 15min / 30min / 1h / 2h / 6h / 24h
统计指标: 中位涨跌、胜率、样本数
50 个样本后才认为有统计意义。
"""

import json
import logging
import os
import time
from typing import Optional

logger = logging.getLogger("arc.shadow")

# 影子跟踪文件
_SHADOW_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            ".shadow_tracker.json")

# 回查时间点（秒）
CHECKPOINTS = [300, 900, 1800, 3600, 7200, 21600, 86400]  # 5m,15m,30m,1h,2h,6h,24h

# 最小样本数
MIN_SAMPLES = 50


def _load_records() -> list[dict]:
    if os.path.exists(_SHADOW_FILE):
        try:
            return json.load(open(_SHADOW_FILE))
        except Exception:
            pass
    return []


def _save_records(records: list[dict]):
    # 最多保留 5000 条
    records = records[-5000:]
    json.dump(records, open(_SHADOW_FILE, "w"))


class ShadowTracker:
    """影子表现跟踪器。"""

    def __init__(self, w3=None):
        self.w3 = w3
        self.records = _load_records()
        logger.info(f"[Shadow] 已加载 {len(self.records)} 条影子记录")

    def record(self, token_address: str, token_name: str, token_symbol: str,
               score: int, action: str, source: str, entry_price: float = 0.0):
        """
        记录一个代币的评分决策。

        参数:
            token_address: 代币合约地址
            token_name:    代币名称
            token_symbol:   代币符号
            score:         评分 (0-100)
            action:        "buy" 或 "skip"
            source:        信号来源
            entry_price:   记录时的价格 (0 表示未知)
        """
        record = {
            "address": token_address.lower(),
            "name": token_name,
            "symbol": token_symbol,
            "score": score,
            "action": action,
            "source": source,
            "entry_price": entry_price,
            "timestamp": int(time.time()),
            "prices": {},  # {checkpoint_sec: price} 延迟填充
            "completed": False,
        }
        self.records.append(record)
        _save_records(self.records)
        logger.info(f"[Shadow] 记录 {token_symbol} score={score} action={action}")

    def get_pending_checks(self) -> list[tuple[dict, int]]:
        """获取需要回查价格的记录。返回 (record, checkpoint_sec) 列表。"""
        now = int(time.time())
        pending = []
        for r in self.records:
            if r.get("completed"):
                continue
            elapsed = now - r["timestamp"]
            for cp in CHECKPOINTS:
                cp_key = str(cp)
                if cp_key not in r["prices"] and elapsed >= cp:
                    pending.append((r, cp))
            # 检查是否所有 checkpoint 都已完成
            if all(str(cp) in r["prices"] for cp in CHECKPOINTS):
                r["completed"] = True
        return pending

    def update_price(self, record_idx: int, checkpoint_sec: int, price: float):
        """更新某个 checkpoint 的价格。"""
        if 0 <= record_idx < len(self.records):
            self.records[record_idx]["prices"][str(checkpoint_sec)] = price
            _save_records(self.records)

    def get_stats(self) -> dict:
        """统计买入组 vs 跳过组的表现。"""
        buy_results = []
        skip_results = []

        for r in self.records:
            if not r.get("completed"):
                continue
            entry = r.get("entry_price", 0)
            if entry <= 0:
                continue
            # 用 24h 价格计算涨跌
            final = r["prices"].get("86400", 0)
            if final <= 0:
                continue
            pnl = (final - entry) / entry
            if r["action"] == "buy":
                buy_results.append(pnl)
            else:
                skip_results.append(pnl)

        def _summary(results):
            if not results:
                return {"count": 0, "median": 0, "win_rate": 0}
            results.sort()
            mid = len(results) // 2
            median = results[mid] if len(results) % 2 else (results[mid-1] + results[mid]) / 2
            wins = sum(1 for r in results if r > 0)
            return {
                "count": len(results),
                "median": median,
                "win_rate": wins / len(results),
            }

        return {
            "buy": _summary(buy_results),
            "skip": _summary(skip_results),
            "total_records": len(self.records),
            "completed": sum(1 for r in self.records if r.get("completed")),
            "min_samples": MIN_SAMPLES,
            "has_significance": len(buy_results) >= MIN_SAMPLES or len(skip_results) >= MIN_SAMPLES,
        }

    def run_forever(self, poll_interval: int = 60):
        """主循环：定期回查价格。"""
        logger.info(f"[Shadow] 影子跟踪器启动，每 {poll_interval}s 检查一次")

        while True:
            try:
                pending = self.get_pending_checks()

                if pending:
                    logger.info(f"[Shadow] {len(pending)} 个待回查")

                for record, cp in pending:
                    # 这里需要通过 GMGN 或 DexScreener 查当前价格
                    # 暂时只记录，实际价格查询在 9/16 后可用
                    pass

                time.sleep(poll_interval)

            except KeyboardInterrupt:
                break
            except Exception as e:
                logger.error(f"[Shadow] 主循环异常: {e}")
                time.sleep(poll_interval)
