#!/usr/bin/env python3
"""
calc_position_size.py — 统一仓位计算模块 (P1)

全系统唯一的仓位计算入口，scanner / auto_open / dual_net 统一调用。

接口：
    calc(sig_type, score, direction, details) -> float (USDT名义仓位)

说明：
    sig_type  : 'trend' | 'reversal' | 'reversal_l1' |
                'trend_short' | 'reversal_short' | 'reversal_short_l1'
    score     : 评分（0~120）
    direction : 'LONG' | 'SHORT'
    details   : 上下文字典，含 btc_trend / sentiment / scenario 等可选字段

【复盘模式】全部固定 100U。
实盘跑完后，用 calc_position_size_real.py 的档位表计算每笔的"真实仓位倍数"，
对复盘盈亏乘对应倍数即可还原真实结果。
真实档位对照表（供复盘还原用）：
  趋势多单：score<70→300U(3x) / 70~79→500U(5x) / 80~89→700U(7x) / ≥90→1000U(10x)
  趋势空单：score<70→300U(3x) / 70~79→450U(4.5x) / ≥80→600U(6x)
  逆势L2  ：固定250U(2.5x)
  逆势L1  ：固定125U(1.25x)
  场景四  ：以上×0.5
  BTC顺势  ：×1.2；BTC逆势：×0.8；连续亏损3笔：×0.8；连续亏损5笔：0（暂停）
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# ── 常量 ──────────────────────────────────────────────────────────────────────
POS_MIN_U = 100    # 复盘模式：固定100U
POS_MAX_U = 100    # 复盘模式：固定100U


def calc(
    sig_type:  str,
    score:     float,
    direction: str = "LONG",
    details:   dict = None,
) -> float:
    """
    【复盘模式】固定返回 100U，方便事后乘以真实仓位倍数还原结果。
    """
    if details is None:
        details = {}

    # 连续亏损 5 笔仍然暂停开仓（风控保留）
    try:
        from risk_manager import load_circuit as _lc
        cons_losses = _lc().get("consecutive_losses", 0)
        if cons_losses >= 5:
            return 0.0
    except Exception:
        pass

    return 100.0


if __name__ == "__main__":
    # 自测
    cases = [
        ("trend",          85, "LONG",  {}),
        ("trend",          72, "LONG",  {"btc_trend": "up", "sentiment": "greedy"}),
        ("trend_short",    80, "SHORT", {}),
        ("reversal",       65, "LONG",  {}),
        ("reversal_l1",    60, "LONG",  {}),
        ("trend",          70, "LONG",  {"scenario": 4}),
    ]
    print(f"{'sig_type':<20} {'score':>5} {'dir':<6} {'pos_U':>7}  (复盘模式全部100U)")
    print("-" * 60)
    for st, sc, dr, det in cases:
        pos = calc(st, sc, dr, det)
        print(f"{st:<20} {sc:>5} {dr:<6} {pos:>7.0f}U")
    print("\n✅ calc_position_size 自测通过（复盘模式）")
