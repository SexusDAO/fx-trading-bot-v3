"""影子信号穿透追踪（v1，只记录，不改变任何交易行为）。

被拒信号不再直接丢弃：记录「第一道拒绝它的关卡」，并对后续只读关卡做一次探测，
写入 logs/rejected_log.jsonl。结果（是否成交、先止盈还是先止损）由 tools/shadow_verify.py 离线补标签。

追踪范围：
  A 判定精筛被拒（judge_xxx）          first_rejected_at = "judge_{route}"
  B 扫描后处理被拒（相关性/BTC/新闻/低分/槽位） first_rejected_at = 过滤器名
  C 下单前检查被拒（executor._precheck）   first_rejected_at = "precheck:{类别}"
不追踪：分类粗筛、挂单阶段（存活复查/超时/BTC 急跌撤单）、funding_arb。

隔离原则：影子探测只调用只读函数；不下单、不入挂单队列、不发通知、不写冷却期/黑名单/持仓状态；
LLM 关卡一律跳过（记为 shadow_skip_llm）；需要查盘口的探测每轮最多 10 条，其余每轮最多 30 条。
"""
import json
import logging
import threading
import time
from pathlib import Path

logger = logging.getLogger("fx.shadow_trace")

LOG_PATH = Path(__file__).resolve().parent.parent / "logs" / "rejected_log.jsonl"
CAP_ORDERBOOK = 10      # 需要查盘口的探测（流动性门槛）每轮上限
CAP_READONLY = 30       # 其余记录每轮上限

SKIP_LLM = "shadow_skip_llm"
SKIP_CAP = "shadow_skip_cap"       # 超过每轮上限，未探测
SKIP_ERR = "shadow_skip_error"     # 探测出错

_lock = threading.Lock()
_round = {"orderbook": 0, "records": 0}


def enabled() -> bool:
    try:
        import config as _c
        return bool(getattr(_c, "SHADOW_TRACE_ENABLED", True))
    except Exception:
        return False


def new_round() -> None:
    """每轮扫描开始时调用，重置每轮计数。"""
    with _lock:
        _round["orderbook"] = 0
        _round["records"] = 0


def _take(kind: str, cap: int) -> bool:
    with _lock:
        if _round[kind] >= cap:
            return False
        _round[kind] += 1
        return True


def categorize_scanner_reason(reason: str) -> str:
    """扫描后处理的中文拒绝原因 → 过滤器名。"""
    r = reason or ""
    if "LLM" in r:
        return "llm_filter"
    if "新闻" in r or "news" in r.lower():
        return "news_veto"
    if "相关性" in r:
        return "corr_filter"
    if "槽位" in r:
        return "slot_limit"
    if "BTC" in r:
        return "btc_bear_filter"
    if "评分" in r or "分数" in r or "score" in r.lower():
        return "low_score_filter"
    return "scanner_other"


def categorize_precheck_reason(reason: str) -> str:
    """executor._precheck 的拒绝原因 → 类别。"""
    r = reason or ""
    if "流动性" in r or "深度" in r:
        return "liquidity_gate"
    if "已有" in r or "重复" in r:
        return "duplicate"
    if "相关" in r:
        return "correlation"
    if "余额" in r or "保证金" in r:
        return "balance"
    if "上限" in r or "满" in r:
        return "position_limit"
    return "other"


def would_pass_all(would_also_fail: list):
    """后续关卡全部放行 → True；有关卡拒绝 → False；有未探测项（跳过）→ None（未知）。"""
    real = [x for x in would_also_fail if not str(x).startswith("shadow_skip")]
    if real:
        return False
    if any(str(x).startswith("shadow_skip") for x in would_also_fail):
        return None
    return True


def probe_liquidity(sig: dict) -> list:
    """只读探测下单前的流动性门槛，返回 would_also_fail 片段。"""
    if not _take("orderbook", CAP_ORDERBOOK):
        return [SKIP_CAP]
    try:
        from executor import _check_liquidity_gate
        ok, _ = _check_liquidity_gate(
            sig.get("symbol", ""), str(sig.get("direction", "LONG")).upper(), notional=100.0,
            entry_price=sig.get("pre_entry_price"),
            oi_now=(sig.get("ind") or {}).get("oi_now", 0.0),
        )
        return [] if ok else ["precheck:liquidity_gate"]
    except Exception as e:
        logger.debug(f"[shadow] 流动性探测失败: {e}")
        return [SKIP_ERR]


def _num(x):
    try:
        v = float(x)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def record(*, symbol: str, signal_type: str, direction: str, first_rejected_at: str,
           would_also_fail: list | None = None, entry_candidate=None, sl=None, tp1=None,
           atr=None, reason: str = "", stage: str = "") -> bool:
    """写一条影子记录。超过每轮上限或关闭时不写。永不抛异常。"""
    if not enabled():
        return False
    if not _take("records", CAP_READONLY):
        return False
    waf = list(would_also_fail or [])
    rec = {
        "ts": int(time.time() * 1000),
        "stage": stage,
        "symbol": symbol,
        "signal_type": signal_type,
        "direction": str(direction or "").upper(),
        "first_rejected_at": first_rejected_at,
        "reason": (reason or "")[:160],
        "would_also_fail": waf,
        "would_pass_all": would_pass_all(waf),
        "entry_candidate": _num(entry_candidate),
        "sl": _num(sl),
        "tp1": _num(tp1),
        "atr": _num(atr),
    }
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _lock, open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return True
    except Exception as e:
        logger.debug(f"[shadow] 写入失败: {e}")
        return False


def _sig_prices(sig: dict) -> tuple:
    p = sig.get("params") or {}
    return (sig.get("pre_entry_price") or p.get("entry_price"),
            sig.get("sl_price") or p.get("sl_price"),
            sig.get("tp1_price") or p.get("tp1_price"))


def record_scanner_rejections(result: dict, signals: list) -> int:
    """B 类：扫描结束时，把 result["rejected"] 里属于已通过判定的信号写入影子日志。"""
    if not enabled():
        return 0
    by_sym = {}
    for s in signals or []:
        if isinstance(s, dict) and s.get("symbol"):
            by_sym.setdefault(s["symbol"], s)
    n = 0
    for rej in (result or {}).get("rejected", []):
        sig = by_sym.get(rej.get("symbol"))
        if not sig:
            continue
        reason = str(rej.get("reason", ""))
        cat = categorize_scanner_reason(reason)
        waf = [SKIP_LLM] if cat == "llm_filter" else []
        waf += probe_liquidity(sig)
        e, sl, tp = _sig_prices(sig)
        if record(symbol=sig["symbol"], signal_type=sig.get("mode", ""), direction=sig.get("direction", ""),
                  first_rejected_at=cat, would_also_fail=waf, entry_candidate=e, sl=sl, tp1=tp,
                  atr=(sig.get("ind") or {}).get("atr_1h") or sig.get("atr"), reason=reason, stage="B"):
            n += 1
    return n
