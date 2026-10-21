"""阶段3：影子触发器（只观测，不下单、不发 REST、不给 hub 加回调）。

每秒轮询 market_data_hub 内存：用 K 线 open_time 变化判断收盘；8 种策略边沿触发 + 冷却，
写 logs/trigger_log.jsonl；并监视 logs/scan_signals.jsonl 做 [trigger-miss]/[trigger-fp] 对账。
config.TRIGGER_DETECTOR_ENABLED=False 时 start() 直接返回。
"""
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("fx.trigger")

_FX = Path(__file__).parent.parent
TRIGGER_LOG = _FX / "logs" / "trigger_log.jsonl"
SCAN_SIGNALS_LOG = _FX / "logs" / "scan_signals.jsonl"
LOG_WARN_BYTES = 10 * 1024 * 1024

COOLDOWN = {
    "pullback": 900, "reversal": 900, "breakout": 1800, "funding_arb": 7200,
    "range_bounce": 3600, "stop_hunt": 300, "oi_divergence": 1200, "ema_trend": 14400,
}
FUNDING_ARB_INTERVAL = 300
RECONCILE_EXCLUDE = {"funding_arb"}


def _cfg(name, default):
    try:
        import config
        return getattr(config, name, default)
    except Exception:
        return default


def _ema(c, n):
    from shared.indicators import ema
    return ema(c, n)


def _rsi(c):
    from shared.indicators import rsi
    return rsi(c)


def _atr(k):
    from shared.indicators import atr
    return atr(k)


def _is_weekend(now):
    wd, h = now.isoweekday(), now.hour
    return (wd == 5 and h >= 22) or wd in (6, 7)


def _default_oi(symbol):
    """读 oi_cache.json：(oi_now, oi_1h_ago)，任一无效返回 (0,0)。"""
    try:
        from shared.state import _load_oi_cache
        s = _load_oi_cache().get(symbol) or []
        if len(s) < 2:
            return 0.0, 0.0
        now_ms = int(time.time() * 1000)
        last = max(s, key=lambda r: r["timestamp"])
        if now_ms - last["timestamp"] > 3600 * 1000:
            return 0.0, 0.0
        tgt = now_ms - 3600 * 1000
        ago = min(s, key=lambda r: abs(r["timestamp"] - tgt))
        if abs(ago["timestamp"] - tgt) > 900 * 1000:
            return 0.0, 0.0
        return float(last["oi"]), float(ago["oi"])
    except Exception:
        return 0.0, 0.0


class _St:
    __slots__ = ("triggered", "last_trigger_ts", "last_value")

    def __init__(self):
        self.triggered = False
        self.last_trigger_ts = 0.0
        self.last_value = 0.0


class TriggerDetector:
    def __init__(self, hub=None, clock: Callable[[], float] = time.time,
                 oi_reader: Callable = _default_oi, now_utc: Callable = lambda: datetime.now(timezone.utc),
                 trigger_log: Path = TRIGGER_LOG, scan_log: Path = SCAN_SIGNALS_LOG):
        self._hub = hub
        self._clock = clock
        self._oi = oi_reader
        self._now_utc = now_utc
        self._tlog = Path(trigger_log)
        self._slog = Path(scan_log)
        self._st = {}
        self._open = {}          # (sym, iv) -> 最后一根 open_time
        self._initialized = False
        self._last_arb = 0.0
        self._scan_pos = 0
        self._triggers = []      # [(ts_sec, sym, strategy)]
        self._pending_scan = []  # [(ts_sec, set(sym))] 待 fp 判定
        self._warned = False
        self.stats = {"miss": 0, "fp": 0, "oi_miss": 0, "oi_fp": 0}
        self._stop = threading.Event()
        self._thread = None

    # ── 公共 ────────────────────────────────────────────────────────────
    def hub(self):
        if self._hub is not None:
            return self._hub
        try:
            from shared.market_data_hub import get_hub
            return get_hub()
        except Exception:
            return None

    def symbols(self, hub):
        try:
            with hub._kl_lock:
                return sorted({s for (s, iv) in hub._series.keys() if iv == "1h"})
        except Exception:
            return []

    def poll_once(self):
        hub = self.hub()
        if hub is None:
            return []
        now = self._clock()
        fired = []
        first = not self._initialized
        arb_due = now - self._last_arb >= FUNDING_ARB_INTERVAL
        for sym in self.symbols(hub):
            try:
                fired += self._check_symbol(hub, sym, now, first, arb_due)
            except Exception as e:
                logger.debug(f"[trigger] {sym} 检查失败: {e}")
        if arb_due:
            self._last_arb = now
        self._initialized = True
        if not first:
            self.reconcile(now)
        return fired

    # ── 单标的 ──────────────────────────────────────────────────────────
    def _closed(self, sym, iv, rows):
        if not rows:
            return False
        ot = rows[-1][0]
        prev = self._open.get((sym, iv))
        self._open[(sym, iv)] = ot
        return prev is not None and prev != ot

    def _check_symbol(self, hub, sym, now, first, arb_due):
        k1h = hub.get_klines(sym, "1h", 60)
        k15 = hub.get_klines(sym, "15m", 30)
        k4h = hub.get_klines(sym, "4h", 60)
        mp = hub.get_mark_price(sym)
        tk = hub.get_ticker(sym)
        if not k1h or not mp:
            return []
        price = float(mp.get("markPrice") or 0)
        if price <= 0:
            return []
        c15 = self._closed(sym, "15m", k15)
        c1h = self._closed(sym, "1h", k1h)
        c4h = self._closed(sym, "4h", k4h)

        k1hc = k1h[:-1]
        c1 = [float(k[4]) for k in k1hc]
        if len(c1) < 21:
            return []
        ema20 = _ema(c1, 20)
        ema50 = _ema(c1, 50) if len(c1) >= 50 else _ema(c1, len(c1))
        rsi1 = _rsi(c1)
        atr1 = _atr(k1hc)
        atr_r = atr1 / price * 100
        funding = float(mp.get("lastFundingRate") or 0)
        chg24 = float(tk.get("priceChangePercent") or 0) if tk else 0.0
        snap = {"chg24": chg24, "rsi_1h": rsi1, "ema20_1h": ema20, "atr_ratio": atr_r, "funding": funding}
        out = []

        def emit(strat, cond, direction, reason, edge=True):
            st = self._st.setdefault((sym, strat), _St())
            was = st.triggered
            st.triggered = bool(cond)
            if first or not cond or (edge and was):
                return
            if now - st.last_trigger_ts < COOLDOWN[strat]:
                return
            st.last_trigger_ts = now
            out.append(self._fire(sym, strat, direction, reason, price, snap, now))

        # 1 pullback（15M 收盘）
        if c15 or first:
            d = "LONG" if ema20 >= ema50 else "SHORT"
            lo, hi = (ema20 - atr1, ema20 + 0.3 * atr1) if d == "LONG" else (ema20 - 0.3 * atr1, ema20 + atr1)
            emit("pullback", lo <= price <= hi, d, f"in_zone[{lo:.6g},{hi:.6g}]", edge=False)

        # 2 reversal（实时，RSI 迟滞）
        st = self._st.setdefault((sym, "reversal_rsi"), _St())
        if rsi1 < 28:
            st.last_value = -1
        elif rsi1 > 72:
            st.last_value = 1
        elif st.last_value == -1 and rsi1 > 32:
            st.last_value = 0
        elif st.last_value == 1 and rsi1 < 68:
            st.last_value = 0
        rsi_flag = st.last_value
        chg_min = _cfg("CLASSIFY_REVERSAL_CHG24_MIN", 15.0)
        fund_min = _cfg("CLASSIFY_REVERSAL_FUNDING_MIN", 0.001)
        rev = abs(chg24) > chg_min or abs(funding) > fund_min or rsi_flag != 0
        d = "LONG" if (rsi_flag == -1 or (rsi_flag == 0 and chg24 < 0)) else "SHORT"
        emit("reversal", rev, d, f"chg24={chg24:.2f} fr={funding:.5f} rsi={rsi1:.1f}")

        # 3 breakout（实时）
        last20 = k1hc[-20:]
        h20 = max(float(k[2]) for k in last20)
        l20 = min(float(k[3]) for k in last20)
        bmin = _cfg("CLASSIFY_BREAKOUT_CHG24_MIN", 5.0)
        up = abs(chg24) > bmin and chg24 > 0 and price >= h20
        dn = abs(chg24) > bmin and chg24 < 0 and price <= l20
        emit("breakout", up or dn, "LONG" if up else "SHORT", f"h20={h20:.6g} l20={l20:.6g}")

        # 4 funding_arb（每 5 分钟）
        if arb_due or first:
            nft = float(mp.get("nextFundingTime") or 0)
            soon = 0 < nft / 1000 - now <= 7200 if nft else False
            cond = abs(funding) > _cfg("FUNDING_EXTREME_THRESH", 0.005) and soon
            emit("funding_arb", cond, "LONG" if funding < 0 else "SHORT", f"fr={funding:.5f}", edge=False)

        # 5 range_bounce（1H 收盘）
        if c1h or first:
            emit("range_bounce", self._range_bounce(c1, price, atr_r), "LONG", "peak→range_lower", edge=False)

        # 6 stop_hunt（15M 实时帧）
        if k15:
            sh = self._stop_hunt(c1, k15)
            emit("stop_hunt", sh is not None, sh or "LONG", "pierce_pivot")

        # 7 oi_divergence（实时，OI 来自缓存）
        oi_now, oi_ago = self._oi(sym)
        hi24 = float(tk.get("highPrice") or 0) if tk else 0
        lo24 = float(tk.get("lowPrice") or 0) if tk else 0
        cond, d = False, "SHORT"
        if oi_now > 0 and oi_ago > 0 and hi24 > 0 and lo24 > 0 and atr_r >= 0.5:
            ch = (oi_now - oi_ago) / oi_ago
            if abs(ch) >= 0.08:
                if price >= hi24 * 0.97 and ch < 0:
                    cond, d = True, "SHORT"
                elif price <= lo24 * 1.03 and ch > 0:
                    cond, d = True, "LONG"
        emit("oi_divergence", cond, d, "oi_src=cache")

        # 8 ema_trend（4H 收盘）
        if (c4h or first) and k4h:
            c4 = [float(k[4]) for k in k4h[:-1]]
            if len(c4) >= 50:
                e20, e50 = _ema(c4, 20), _ema(c4, 50)
                ok, d = False, "LONG"
                if e20 != e50:
                    lg = e20 > e50
                    d = "LONG" if lg else "SHORT"
                    pb = (price - e20) / e20
                    ok = (45 <= rsi1 <= (70 if lg else 72) and -8.0 <= chg24 <= 12.0
                          and ((-0.025 <= pb <= 0.03) if lg else (-0.03 <= pb <= 0.025)))
                emit("ema_trend", ok, d, f"ema20_4h={e20:.6g}", edge=False)
        return out

    def _range_bounce(self, closes, price, atr_r):
        if len(closes) < 14:
            return False
        if atr_r < (1.5 if _is_weekend(self._now_utc()) else 2.0):
            return False
        r12 = closes[-12:]
        pv = max(r12)
        pa = len(closes) - 12 + r12.index(pv)
        base = closes[max(0, pa - 6)]
        if (pv - base) / (base + 1e-9) * 100 < 15:
            return False
        post = closes[pa:]
        if len(post) < 2:
            return False
        ch, cl = max(post), min(post)
        if (ch - cl) / (cl + 1e-9) * 100 > 20:
            return False
        return price <= (ch + cl) / 2

    def _stop_hunt(self, closes_1h, k15):
        if len(closes_1h) < 24 or len(k15) < 28:
            return None
        r = closes_1h[-20:]
        pl = ph = None
        mn, mx = min(r), max(r)
        i = r.index(mn)
        if 3 <= i <= len(r) - 4 and all(r[i - j] > mn and r[i + j] > mn for j in (1, 2, 3)):
            if sum(1 for v in r[i + 1:] if v > mn * 1.003) >= 4:
                pl = mn
        i = r.index(mx)
        if 3 <= i <= len(r) - 4 and all(r[i - j] < mx and r[i + j] < mx for j in (1, 2, 3)):
            if sum(1 for v in r[i + 1:] if v < mx * 0.997) >= 4:
                ph = mx
        if pl is None and ph is None:
            return None
        kc = k15[:-1]
        vs = kc[-26:-6]
        if len(vs) < 5:
            return None
        vma = sum(float(k[5]) for k in vs) / len(vs)
        if vma <= 0:
            return None
        for b in kc[-6:]:
            if float(b[5]) / vma < 1.5:
                continue
            if pl is not None and float(b[3]) <= pl * 0.997:
                return "LONG"
            if ph is not None and float(b[2]) >= ph * 1.003:
                return "SHORT"
        return None

    # ── 输出 ────────────────────────────────────────────────────────────
    def _fire(self, sym, strat, direction, reason, price, snap, now):
        logger.info(f"[trigger] {sym} {strat} {direction} {reason} price={price}")
        rec = {"ts": int(now * 1000), "symbol": sym, "strategy": strat, "direction": direction,
               "reason": reason, "price": price, "hub_snapshot": snap}
        self._triggers.append((now, sym, strat))
        try:
            self._tlog.parent.mkdir(parents=True, exist_ok=True)
            with open(self._tlog, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            if not self._warned and os.path.getsize(self._tlog) > LOG_WARN_BYTES:
                self._warned = True
                logger.warning(f"[trigger] {self._tlog.name} 超过 10MB（不轮转）")
        except Exception as e:
            logger.debug(f"[trigger] 写日志失败: {e}")
        return rec

    # ── 对账 ────────────────────────────────────────────────────────────
    def _read_new_scan(self):
        rows = []
        try:
            if not self._slog.exists():
                return rows
            with open(self._slog, encoding="utf-8") as f:
                f.seek(self._scan_pos)
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            rows.append(json.loads(line))
                        except Exception:
                            pass
                self._scan_pos = f.tell()
        except Exception:
            pass
        return rows

    def reconcile(self, now):
        win = lambda s: max(COOLDOWN.get(s, 300), 300)
        batches = {}
        for r in self._read_new_scan():
            if "symbol" not in r:
                batches.setdefault(r.get("ts", 0), [])
                continue
            batches.setdefault(r.get("ts", 0), []).append(r)
        for ts, sigs in batches.items():
            ts_s = ts / 1000
            for s in sigs:
                st = s.get("strategy")
                if st in RECONCILE_EXCLUDE:
                    continue
                hit = any(sym == s["symbol"] and abs(t - ts_s) <= win(st) for t, sym, _ in self._triggers)
                if not hit:
                    self.stats["miss"] += 1
                    if st == "oi_divergence":
                        self.stats["oi_miss"] += 1
                    logger.info(f"[trigger-miss] {s['symbol']} {st} {s.get('direction')}"
                                + (" (OI来自缓存)" if st == "oi_divergence" else ""))
            self._pending_scan.append((ts_s, {s["symbol"] for s in sigs}))
        # fp：触发后超过窗口仍无扫描信号
        keep = []
        for t, sym, st in self._triggers:
            if st in RECONCILE_EXCLUDE:
                if now - t < 7200:
                    keep.append((t, sym, st))
                continue
            w = win(st)
            if any(sym in syms and abs(ts - t) <= w for ts, syms in self._pending_scan):
                if now - t < w:
                    keep.append((t, sym, st))
                continue
            if now - t >= w:
                self.stats["fp"] += 1
                if st == "oi_divergence":
                    self.stats["oi_fp"] += 1
                tag = " (OI来自缓存)" if st == "oi_divergence" else (" (仅记录)" if st == "pullback" else "")
                logger.info(f"[trigger-fp] {sym} {st}{tag}")
            else:
                keep.append((t, sym, st))
        self._triggers = keep
        self._pending_scan = [(ts, s) for ts, s in self._pending_scan if now - ts < 14400]

    # ── 线程 ────────────────────────────────────────────────────────────
    def _run(self):
        try:
            if self._slog.exists():
                self._scan_pos = self._slog.stat().st_size
        except Exception:
            pass
        while not self._stop.wait(1.0):
            try:
                self.poll_once()
            except Exception as e:
                logger.debug(f"[trigger] poll 异常: {e}")

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="trigger-detector", daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()


_default: Optional[TriggerDetector] = None


def start() -> Optional[TriggerDetector]:
    global _default
    if not _cfg("TRIGGER_DETECTOR_ENABLED", False):
        return None
    if _default is None:
        _default = TriggerDetector()
        _default.start()
    return _default


def write_scan_signals(signals: list, path: Path = SCAN_SIGNALS_LOG, ts_ms: Optional[int] = None) -> None:
    """scanner 调用：每轮扫描 judge 通过的原始信号；无信号写 {"ts", "signals": []}。异常吞掉。"""
    try:
        ts = int(ts_ms if ts_ms is not None else time.time() * 1000)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            if not signals:
                f.write(json.dumps({"ts": ts, "signals": []}) + "\n")
            for s in signals:
                f.write(json.dumps({"ts": ts, "symbol": s.get("symbol"), "strategy": s.get("sig_type"),
                                    "direction": s.get("direction"),
                                    "route_quality": s.get("route_quality")}, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.debug(f"[trigger] 写 scan_signals 失败: {e}")
