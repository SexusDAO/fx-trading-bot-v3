"""
coinglass_api.py — 根目录兼容层（2026-08-29 迁移到 Clawby）

原接口全部保留，实现已迁移到 fx/shared/clawby_adapter.py
所有 import coinglass_api 的调用方（paper_scanner / scanner / entry_logic 等）
无需改动，自动走 Clawby（api.openclawby.com）。

旧接口 coinglass-standard.quant-store.com 已停用。
"""

import os as _os
import sys as _sys
import importlib.util as _util
import logging as _log

_logger = _log.getLogger("coinglass_api")

# ── 1. 把 fx/shared 加入路径，clawby_adapter 就在那里 ────────────────────────
_HERE    = _os.path.dirname(_os.path.abspath(__file__))
_FX_SHARED = _os.path.join(_HERE, "fx", "shared")
if _FX_SHARED not in _sys.path:
    _sys.path.insert(0, _FX_SHARED)

# ── 2. 从 clawby_adapter 导入全部公开接口 ────────────────────────────────────
from clawby_adapter import (  # noqa: F401
    clawby_relay, test_connection,
    _cache_get, _cache_set, _sym_full, _sym_base,
    CACHE_TTL,
    get_liq_heatmap, get_liq_density, get_liq_zones,
    get_liquidation_zone, get_liq_zones_list,
    get_liq_aggregated_heatmap,
    _get_liq_history, get_liq_summary_1h, get_liq_orders, get_liq_coin_list,
    _get_top_lsr, _get_taker, _get_oi_history, _get_funding_rate,
    get_derivatives_signals,
    get_whale_index, get_cvd, get_coinbase_premium, get_fear_greed,
    get_boll, get_liq_max_pain, get_bull_market_peak,
    get_funding_arb_opps, get_funding_exchange_list,
    get_oi_exchange_list, get_economic_calendar,
    get_btc_etf_flow, get_btc_etf_aum,
    get_hl_whale_alerts, get_hl_whale_positions,
    get_altcoin_season,
    _cb_record as _cg_record,
)

# ── 3. 从 fx/shared/coinglass_api.py 直接加载高层评分函数（避免循环 import）──
_fx_cg_path = _os.path.join(_FX_SHARED, "coinglass_api.py")
try:
    _spec = _util.spec_from_file_location("_fx_cg", _fx_cg_path)
    _fx_cg = _util.module_from_spec(_spec)
    _spec.loader.exec_module(_fx_cg)
    # 导出高层函数
    get_whale_signals     = _fx_cg.get_whale_signals
    get_kol_signals       = _fx_cg.get_kol_signals
    get_signal_for_open   = _fx_cg.get_signal_for_open
    score_symbol_cg_bonus = getattr(_fx_cg, "score_symbol_cg_bonus", None)
    adjust_score_by_cg    = getattr(_fx_cg, "adjust_score_by_cg",    None)
    _calc_deriv_score     = getattr(_fx_cg, "_calc_deriv_score",     None)
    print_analysis        = getattr(_fx_cg, "print_analysis",        None)
except Exception as _e:
    _logger.warning(f"[coinglass_api] fx/shared 高层函数加载失败（不影响基础接口）: {_e}")

# ── 4. 健康统计（向后兼容）──────────────────────────────────────────────────
import threading as _threading, time as _time
_CG_HEALTH: dict = {"ok": 0, "fail_404": 0, "fail_timeout": 0, "hour_start": _time.time()}
_CG_HEALTH_LOCK = _threading.Lock()
