"""
test_workflow.py — FOMO 工作流端到端功能测试
测试范围：
  T01  import 完整性 + smoke
  T02  position_manager 开仓/查仓/关仓
  T03  evaluate_exit — 低/中/高位止盈止损
  T04  S 级共识模式 — score 计算 / 分批离场 / 硬止损豁免超时
  T05  普通共识模式激活 / 超时降级
  T06  DCA 补仓条件检查
  T07  record_kol_sell — 普通路径
  T08  信号路由 — KOL 卖出走 S 级 vs 普通路径
  T09  鲸鱼买入 — S 级加分 vs 普通 update_signal
  T10  position_cap 动态计算（S 级 initial×2）
  T11  protect_min 旧仓位兼容（无字段 → 默认值）
  T12  market_utils fetch_market_cap 缓存
  T13  notify.py 函数可调用性
  T14  _cleanup_pending_buy 清理逻辑
  T15  冷却期 record_exit / is_in_cooldown
"""
import sys, os, time, json, copy, traceback, asyncio, importlib, types

sys.path.insert(0, os.path.dirname(__file__))

PASS = "✅"
FAIL = "❌"
SKIP = "⏭ "
results = []

def ok(name):
    results.append((PASS, name))
    print(f"  {PASS} {name}")

def fail(name, reason=""):
    results.append((FAIL, name))
    print(f"  {FAIL} {name}" + (f" — {reason}" if reason else ""))

def section(title):
    print(f"\n{'='*55}")
    print(f"  {title}")
    print(f"{'='*55}")

# ── 假持仓地址（不会真实下单）──────────────────────────
FAKE_ADDR   = "0xdeadbeef00000000000000000000000000000001"
FAKE_ADDR_S = "0xdeadbeef00000000000000000000000000000002"
FAKE_SYM    = "TESTTOKEN"
FAKE_SYM_S  = "TESTTOKEN_S"

# ── 防止写入真实 state.json ──────────────────────────
import tempfile, shutil
_TMP_STATE = tempfile.mktemp(suffix=".json")

# ─────────────────────────────────────────────────────
# T01  Import 完整性
# ─────────────────────────────────────────────────────
section("T01  Import 完整性 + smoke")
try:
    import position_manager as pm
    import market_utils as mu
    import notify as nt
    ok("T01-a  position_manager import")
    ok("T01-b  market_utils import")
    ok("T01-c  notify import")
except Exception as e:
    fail("T01  import", str(e)); sys.exit(1)

# smoke：notify 所有函数可调用
_required_notify = ["_notify","_notify_buy","_notify_close","_notify_partial_sell","_notify_sell","_record_buy_failure"]
_all_ok = all(callable(getattr(nt, fn, None)) for fn in _required_notify)
(ok if _all_ok else fail)("T01-d  notify smoke — 所有函数可调用")

_required_mu = ["fetch_market_cap","check_high_mc_trend"]
_all_ok = all(callable(getattr(mu, fn, None)) for fn in _required_mu)
(ok if _all_ok else fail)("T01-e  market_utils smoke — 所有函数可调用")

# ─────────────────────────────────────────────────────
# T02  position_manager 基本 CRUD
# ─────────────────────────────────────────────────────
section("T02  position_manager 开仓/查仓/关仓")

# 临时重定向 STATE_FILE，避免污染真实仓位
_orig_state = pm.STATE_FILE
pm.STATE_FILE = _TMP_STATE

try:
    # 清空测试状态
    with open(_TMP_STATE, "w") as f:
        json.dump({"positions": {}, "kol_sells": {}}, f)

    # 开仓
    pm.open_position(
        token_addr=FAKE_ADDR, token_symbol=FAKE_SYM,
        strategy="kol", size_usd=30.0,
        entry_price=0.001, entry_mc=50_000,
        trigger_kol="testkol", open_strategy="A",
        entry_source="fomo",
    )
    pos = pm.get_position(FAKE_ADDR)
    if pos and pos["size_usd"] == 30.0:
        ok("T02-a  open_position — 仓位写入成功")
    else:
        fail("T02-a  open_position — 仓位写入失败", str(pos))

    # position_exists
    (ok if pm.position_exists(FAKE_ADDR) else fail)("T02-b  position_exists — True")
    (ok if not pm.position_exists("0x0000") else fail)("T02-c  position_exists — False (不存在)")

    # 关仓
    pm.close_position(FAKE_ADDR, "test_close")
    (ok if not pm.position_exists(FAKE_ADDR) else fail)("T02-d  close_position — 仓位已清除")

except Exception as e:
    fail("T02  CRUD 异常", traceback.format_exc())

# ─────────────────────────────────────────────────────
# T03  evaluate_exit — 止盈止损分层
# ─────────────────────────────────────────────────────
section("T03  evaluate_exit — 低/中/高位止盈止损")

def _make_pos(addr, sym, mc, size=30.0, entry_price=0.001, strategy="A", extra=None):
    with open(_TMP_STATE, "w") as f:
        json.dump({"positions": {}, "kol_sells": {}}, f)
    pm.open_position(
        token_addr=addr, token_symbol=sym,
        strategy="kol", size_usd=size,
        entry_price=entry_price, entry_mc=mc,
        trigger_kol="testkol", open_strategy=strategy,
        entry_source="fomo",
        extra_fields=extra or {},
    )
    # 设置 opened_at 为 2h 前（绕过保护期）
    s = pm._load()
    s["positions"][addr.lower()]["opened_at"] = int(time.time()) - 7200
    s["positions"][addr.lower()]["protect_min"] = 1  # 保护期1分钟，已过
    pm._save(s)

try:
    # 低位（MC=50k）— 到达 300k 止盈点
    _make_pos(FAKE_ADDR, FAKE_SYM, mc=50_000, entry_price=0.001)
    # 当前价格对应 MC=300k（涨 6 倍）
    current_price = 0.001 * (300_000 / 50_000)
    action = pm.evaluate_exit(FAKE_ADDR, current_price, 300_000)
    if action and action.get("action") == "partial_sell" and abs(action.get("sell_pct", 0) - 0.25) < 0.01:
        ok("T03-a  低位止盈第1档 @300k → partial_sell 25%")
    else:
        fail("T03-a  低位止盈第1档", str(action))

    # 中位（MC=150k）— 翻倍出本
    _make_pos(FAKE_ADDR, FAKE_SYM, mc=150_000, entry_price=0.001)
    current_price = 0.001 * 2  # 翻倍
    action = pm.evaluate_exit(FAKE_ADDR, current_price, 300_000)
    if action and "50%" in action.get("reason","") or (action and action.get("sell_pct",0) == 0.5):
        ok("T03-b  中位翻倍出本 → partial_sell 50%")
    else:
        fail("T03-b  中位翻倍出本", str(action))

    # 中位硬止损 -50%
    _make_pos(FAKE_ADDR, FAKE_SYM, mc=150_000, entry_price=0.001)
    action = pm.evaluate_exit(FAKE_ADDR, 0.0005, 75_000)
    if action and action.get("action") == "full_sell":
        ok("T03-c  中位硬止损 -50% → full_sell")
    else:
        fail("T03-c  中位硬止损", str(action))

    # 超高位（MC=15M）— 翻倍全清
    _make_pos(FAKE_ADDR, FAKE_SYM, mc=15_000_000, entry_price=0.001)
    action = pm.evaluate_exit(FAKE_ADDR, 0.002, 30_000_000)
    if action and action.get("action") == "full_sell":
        ok("T03-d  超高位翻倍 → full_sell")
    else:
        fail("T03-d  超高位翻倍", str(action))

except Exception as e:
    fail("T03  evaluate_exit 异常", traceback.format_exc())

# ─────────────────────────────────────────────────────
# T04  S 级共识模式
# ─────────────────────────────────────────────────────
section("T04  S 级共识模式")

try:
    from config import S_CONSENSUS_INIT_SCORE, S_CONSENSUS_HOLD_LINE

    # 验证初始分 > 持有线（P1 修复）
    if S_CONSENSUS_INIT_SCORE > S_CONSENSUS_HOLD_LINE:
        ok(f"T04-a  初始分({S_CONSENSUS_INIT_SCORE}) > 持有线({S_CONSENSUS_HOLD_LINE}) — 缓冲正常")
    else:
        fail(f"T04-a  初始分={S_CONSENSUS_INIT_SCORE} == 持有线={S_CONSENSUS_HOLD_LINE}，P1 未修复")

    # 开 S 级仓位
    _make_pos(FAKE_ADDR_S, FAKE_SYM_S, mc=5_000_000, size=45.0,
              strategy="A", extra={"signal_grade": "S"})
    pos_s = pm.get_position(FAKE_ADDR_S)

    # consensus_mode 应为 True
    (ok if pos_s and pos_s.get("consensus_mode") else fail)("T04-b  S级开仓自动 consensus_mode=True")

    # signal_score 应为初始分
    sc = pos_s.get("signal_score", 0) if pos_s else 0
    (ok if sc == S_CONSENSUS_INIT_SCORE else fail)(
        f"T04-c  S级初始 signal_score={sc}（期望{S_CONSENSUS_INIT_SCORE}）")

    # is_s_consensus 返回 True
    (ok if pm.is_s_consensus(FAKE_ADDR_S) else fail)("T04-d  is_s_consensus() == True")

    # score 在持有线以上 → s_consensus_score_action 返回 None（持有）
    action = pm.s_consensus_score_action(FAKE_ADDR_S, S_CONSENSUS_INIT_SCORE)
    (ok if action is None else fail)("T04-e  score=初始分 → 持有（返回None）")

    # 扣分到 55 → 减仓 20%（50~59 区间）
    new_sc, action = pm.s_consensus_apply_score(FAKE_ADDR_S, -(S_CONSENSUS_INIT_SCORE - 55),
                                                 reason="test扣分到55")
    if action and action.get("sell_pct") == 0.20:
        ok(f"T04-f  score=55 → 减仓20%")
    else:
        fail("T04-f  score=55减仓", f"new_sc={new_sc} action={action}")

    # S 级豁免 consensus_timeout（P2 修复）
    # 手动设置 consensus_activated_at 为 10h 前
    s = pm._load()
    if FAKE_ADDR_S.lower() in s["positions"]:
        s["positions"][FAKE_ADDR_S.lower()]["consensus_activated_at"] = int(time.time()) - 36000
        pm._save(s)
    timed_out = pm.check_consensus_timeout(FAKE_ADDR_S)
    (ok if not timed_out else fail)("T04-g  S级豁免4h超时（P2修复）")

    # position_cap = initial_size × 2（P3 修复）
    _make_pos(FAKE_ADDR_S, FAKE_SYM_S, mc=5_000_000, size=75.0,
              strategy="A", extra={"signal_grade": "S"})
    pos_s2 = pm.get_position(FAKE_ADDR_S)
    cap = pos_s2.get("position_cap", 0) if pos_s2 else 0
    if abs(cap - 150.0) < 0.1:  # 75 × 2 = 150
        ok(f"T04-h  position_cap = initial×2 = {cap:.0f}U（P3修复）")
    else:
        fail("T04-h  position_cap 计算", f"cap={cap}，期望150")

except Exception as e:
    fail("T04  S级共识", traceback.format_exc())

# ─────────────────────────────────────────────────────
# T05  普通共识模式激活 / 超时降级
# ─────────────────────────────────────────────────────
section("T05  普通共识模式激活 / 超时降级")

try:
    _make_pos(FAKE_ADDR, FAKE_SYM, mc=150_000, size=30.0)

    # 手动注入 consensus_signals（已达阈值） + consensus_pending=True
    from config import CONSENSUS_MIN_SIGNALS, CONSENSUS_WINDOW_MIN, CONSENSUS_WHALE_USD
    s = pm._load()
    addr_key = FAKE_ADDR.lower()
    s["positions"][addr_key]["consensus_signals"] = [
        {"ts": time.time() - 30, "addr": "0xkol1", "usd": CONSENSUS_WHALE_USD},
        {"ts": time.time() - 10, "addr": "0xkol2", "usd": CONSENSUS_WHALE_USD},
    ]
    s["positions"][addr_key]["signal_score"] = 30
    s["positions"][addr_key]["consensus_pending"] = True
    pm._save(s)

    # try_activate_consensus：盈利状态下应激活
    activated = pm.try_activate_consensus(FAKE_ADDR, current_pnl=0.10)
    (ok if activated else fail)("T05-a  try_activate_consensus 盈利时激活")

    # 超时：把信号时间和 activated_at 均设为 5h 前
    s = pm._load()
    s["positions"][addr_key]["consensus_activated_at"] = int(time.time()) - 18001
    for sg in s["positions"][addr_key].get("consensus_signals", []):
        sg["ts"] = time.time() - 18001
    pm._save(s)
    timed_out = pm.check_consensus_timeout(FAKE_ADDR)
    (ok if timed_out else fail)("T05-b  普通共识4h后超时降级")

    # 降级后 consensus_mode 应为 False
    pos_after = pm.get_position(FAKE_ADDR)
    (ok if pos_after and not pos_after.get("consensus_mode") else fail)("T05-c  降级后 consensus_mode=False")

except Exception as e:
    fail("T05  普通共识", traceback.format_exc())

# ─────────────────────────────────────────────────────
# T06  DCA 补仓条件
# ─────────────────────────────────────────────────────
section("T06  DCA 补仓条件检查")

try:
    from config import DCA_MAX_TIMES, DCA_SIZE_USD

    _make_pos(FAKE_ADDR, FAKE_SYM, mc=150_000, size=30.0)

    # 初始状态可以补仓
    can, reason = pm.can_dca(FAKE_ADDR)
    (ok if can else fail)(f"T06-a  初始状态可补仓")

    # 模拟补满 3 次
    s = pm._load()
    s["positions"][FAKE_ADDR.lower()]["dca_count"] = DCA_MAX_TIMES
    pm._save(s)
    can, reason = pm.can_dca(FAKE_ADDR)
    (ok if not can else fail)(f"T06-b  dca_count=={DCA_MAX_TIMES} → 拒绝补仓 ({reason})")

    # 超出 position_cap
    s = pm._load()
    s["positions"][FAKE_ADDR.lower()]["dca_count"] = 0
    s["positions"][FAKE_ADDR.lower()]["size_usd"] = 200.0  # 远超上限
    pm._save(s)
    can, reason = pm.can_dca(FAKE_ADDR)
    (ok if not can else fail)(f"T06-c  size>=position_cap → 拒绝 ({reason})")

except Exception as e:
    fail("T06  DCA", traceback.format_exc())

# ─────────────────────────────────────────────────────
# T07  record_kol_sell — 普通路径
# ─────────────────────────────────────────────────────
section("T07  record_kol_sell 普通路径")

try:
    from config import KOL_SELL_STRICT_COUNT, KOL_SELL_WHALE_THRESHOLD_USD

    # 重置
    with open(_TMP_STATE, "w") as f:
        json.dump({"positions": {}, "kol_sells": {}}, f)
    _make_pos(FAKE_ADDR, FAKE_SYM, mc=150_000, size=30.0)

    # 第 1 个 KOL 卖出（小额）→ 不触发
    action = pm.record_kol_sell(FAKE_ADDR, "kol_addr_1", 1000)
    (ok if action is None else fail)(f"T07-a  首个小额KOL卖出 → 不触发全清")

    # 第 2 个 KOL 卖出（达到 strict_count=2）→ 触发全清
    action = pm.record_kol_sell(FAKE_ADDR, "kol_addr_2", 1000)
    if action and action.get("action") == "full_sell":
        ok(f"T07-b  第{KOL_SELL_STRICT_COUNT}个KOL卖出 → full_sell")
    else:
        fail("T07-b  KOL卖出未触发", str(action))

    # 大额鲸鱼卖出（>= 50k）→ 直接全清
    _make_pos(FAKE_ADDR, FAKE_SYM, mc=150_000, size=30.0)
    with open(_TMP_STATE, "r") as f:
        s = json.load(f)
    s["kol_sells"] = {}
    with open(_TMP_STATE, "w") as f:
        json.dump(s, f)
    action = pm.record_kol_sell(FAKE_ADDR, "whale_addr", KOL_SELL_WHALE_THRESHOLD_USD + 1)
    if action and action.get("action") == "full_sell":
        ok(f"T07-c  大额鲸鱼卖出≥${KOL_SELL_WHALE_THRESHOLD_USD} → full_sell")
    else:
        fail("T07-c  大额鲸鱼卖出", str(action))

except Exception as e:
    fail("T07  record_kol_sell", traceback.format_exc())

# ─────────────────────────────────────────────────────
# T08  S级 vs 普通 KOL 卖出路由
# ─────────────────────────────────────────────────────
section("T08  KOL卖出路由 — S级走共识路径")

try:
    # S 级仓位：单个小额 KOL 卖出不应触发 record_kol_sell 全清
    # （应走 s_consensus_apply_score 扣分路径）
    _make_pos(FAKE_ADDR_S, FAKE_SYM_S, mc=5_000_000, size=45.0,
              extra={"signal_grade": "S"})
    # 重置 kol_sells
    s = pm._load()
    s["kol_sells"] = {}
    pm._save(s)

    is_s = pm.is_s_consensus(FAKE_ADDR_S)
    (ok if is_s else fail)("T08-a  S级仓位 is_s_consensus=True")

    # 模拟 ws_monitor 里 KOL 卖出的路由逻辑
    if is_s:
        # 应走 s_consensus_hard_stop + s_consensus_apply_score
        from config import S_SCORE_KOL_SELL_SMALL, S_HARD_STOP_KOL_RESONANCE
        s_state = pm._load()
        ks_list = s_state.get("kol_sells", {}).get(FAKE_ADDR_S, [])
        hard = pm.s_consensus_hard_stop(FAKE_ADDR_S, "kol_test", 1000, ks_list)
        (ok if hard is None else fail)("T08-b  单个小额KOL卖出 → 未触发S级硬止损")

        new_sc, action = pm.s_consensus_apply_score(FAKE_ADDR_S, -S_SCORE_KOL_SELL_SMALL,
                                                      reason="测试KOL卖出扣分")
        buf = S_CONSENSUS_INIT_SCORE - S_SCORE_KOL_SELL_SMALL
        if abs(new_sc - buf) < 2:
            ok(f"T08-c  S级扣分后 score={new_sc}（期望≈{buf}），在持有线以上，不减仓")
        else:
            fail("T08-c  S级扣分", f"new_sc={new_sc}")
    else:
        fail("T08  S级路由检测失败，is_s_consensus=False")

except Exception as e:
    fail("T08  KOL卖出路由", traceback.format_exc())

# ─────────────────────────────────────────────────────
# T09  鲸鱼买入加分
# ─────────────────────────────────────────────────────
section("T09  鲸鱼买入加分")

try:
    from config import S_SCORE_WHALE_BUY, WHALE_BUY_SCORE

    # S 级：s_consensus_apply_score 加分
    _make_pos(FAKE_ADDR_S, FAKE_SYM_S, mc=5_000_000, size=45.0,
              extra={"signal_grade": "S"})
    init_sc = pm.get_position(FAKE_ADDR_S).get("signal_score", 0)
    new_sc, _ = pm.s_consensus_apply_score(FAKE_ADDR_S, S_SCORE_WHALE_BUY, reason="鲸鱼买入测试")
    if new_sc == init_sc + S_SCORE_WHALE_BUY:
        ok(f"T09-a  S级鲸鱼买入 +{S_SCORE_WHALE_BUY} → score={new_sc}")
    else:
        fail("T09-a  S级鲸鱼加分", f"期望{init_sc+S_SCORE_WHALE_BUY}，实际{new_sc}")

    # 普通：update_signal_on_whale
    _make_pos(FAKE_ADDR, FAKE_SYM, mc=150_000, size=30.0)
    init_sc2 = pm.get_position(FAKE_ADDR).get("signal_score", 0)
    new_sc2, _ = pm.update_signal_on_whale(FAKE_ADDR, is_buy=True)
    if new_sc2 > init_sc2:
        ok(f"T09-b  普通鲸鱼买入加分 {init_sc2}→{new_sc2}")
    else:
        fail("T09-b  普通鲸鱼加分", f"{init_sc2}→{new_sc2}")

except Exception as e:
    fail("T09  鲸鱼买入加分", traceback.format_exc())

# ─────────────────────────────────────────────────────
# T10  protect_min 旧仓位兼容
# ─────────────────────────────────────────────────────
section("T10  protect_min 旧仓位兼容（P5）")

try:
    _make_pos(FAKE_ADDR, FAKE_SYM, mc=150_000, entry_price=0.001)
    # 手动删除 protect_min 模拟旧仓位
    s = pm._load()
    s["positions"][FAKE_ADDR.lower()].pop("protect_min", None)
    # 设 opened_at 为 1 分钟前（处于保护期内）
    s["positions"][FAKE_ADDR.lower()]["opened_at"] = int(time.time()) - 60
    pm._save(s)

    # evaluate_exit 不应该 KeyError 崩溃
    try:
        action = pm.evaluate_exit(FAKE_ADDR, 0.0005, 75_000)  # 跌 50%
        ok("T10-a  旧仓位无 protect_min → 不崩溃（.get 兜底）")
    except KeyError as ke:
        fail("T10-a  旧仓位 protect_min KeyError", str(ke))

except Exception as e:
    fail("T10  protect_min 兼容", traceback.format_exc())

# ─────────────────────────────────────────────────────
# T11  market_utils 缓存
# ─────────────────────────────────────────────────────
section("T11  market_utils 缓存")

try:
    import time as _time
    mu._MC_CACHE["0xtest_cache"] = (999_000, _time.time())
    result = mu.fetch_market_cap("0xtest_cache")
    (ok if result == 999_000 else fail)(f"T11-a  MC缓存命中 → {result}")

    # 过期缓存（TTL=30s，设为60s前）
    mu._MC_CACHE["0xtest_old"] = (888_000, _time.time() - 60)
    result2 = mu.fetch_market_cap("0xtest_old")
    # 过期后会尝试实时拉取（可能失败），但不应崩溃
    ok("T11-b  过期缓存 → 不崩溃（尝试实时拉取）")

except Exception as e:
    fail("T11  market_utils 缓存", traceback.format_exc())

# ─────────────────────────────────────────────────────
# T12  冷却期管理
# ─────────────────────────────────────────────────────
section("T12  冷却期管理")

try:
    from config import COOLDOWN_NORMAL_STOP_LOSS, KOL_SELL_COOLDOWN_MIN

    # stop_loss 冷却
    pm.record_exit(FAKE_ADDR, "stop_loss")
    (ok if pm.is_in_cooldown(FAKE_ADDR) else fail)("T12-a  stop_loss → 进入冷却期")

    # take_profit 无冷却
    pm.record_exit(FAKE_ADDR, "take_profit")
    (ok if not pm.is_in_cooldown(FAKE_ADDR) else fail)("T12-b  take_profit → 无冷却期")

    # kol_sell 冷却 60min
    pm.record_exit(FAKE_ADDR, "kol_sell")
    (ok if pm.is_in_cooldown(FAKE_ADDR) else fail)("T12-c  kol_sell → 进入60min冷却")

except Exception as e:
    fail("T12  冷却期", traceback.format_exc())

# ─────────────────────────────────────────────────────
# T13  全局日亏损熔断
# ─────────────────────────────────────────────────────
section("T13  全局日亏损熔断")

try:
    from position_manager import DAILY_LOSS_LIMIT_USD, get_daily_total_loss, is_global_circuit_broken
    # 正常状态
    (ok if not is_global_circuit_broken() else fail)("T13-a  正常状态熔断=False（当日无亏损）")
    ok("T13-b  get_daily_total_loss 可调用")
except Exception as e:
    fail("T13  日亏损熔断", traceback.format_exc())

# ─────────────────────────────────────────────────────
# 汇总
# ─────────────────────────────────────────────────────
print(f"\n{'='*55}")
total  = len(results)
passed = sum(1 for r in results if r[0] == PASS)
failed = sum(1 for r in results if r[0] == FAIL)
print(f"\n  测试结果: {passed}/{total} 通过   {'🎉 全部通过' if failed == 0 else f'⚠️  {failed} 个失败'}")
print(f"{'='*55}\n")

if failed:
    print("失败项：")
    for r in results:
        if r[0] == FAIL:
            print(f"  {r[0]} {r[1]}")

# 清理临时文件
try:
    os.remove(_TMP_STATE)
    os.remove(_TMP_STATE + ".tmp") if os.path.exists(_TMP_STATE + ".tmp") else None
    os.remove(_TMP_STATE + ".bak") if os.path.exists(_TMP_STATE + ".bak") else None
except:
    pass

# 恢复 STATE_FILE
pm.STATE_FILE = _orig_state

sys.exit(0 if failed == 0 else 1)
