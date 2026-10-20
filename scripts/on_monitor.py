#!/usr/bin/env python3
"""
通用空单/多单仓位监控脚本 — ONUSDT 实例
可复用：修改顶部 CONFIG 即可适配其他币种

功能：
1. 多头拉升明显 → 市价平仓 + 推送
2. 价格下跌且盈利 → 下移止盈（动态跟踪）
3. 价格跌破开仓均价（空单开始盈利） → 挂止损（按规则移动止损）
4. 平仓后自动关闭监控（删除自身 cron）
5. 无异常 → HEARTBEAT_OK（静默）
"""
import sys, json, datetime, requests
sys.path.insert(0, "/root/.openclaw/workspace")
import binance_api as api
import entry_logic as el

# ══════════════════════════════════════════
# CONFIG — 修改这里适配其他币种
# ══════════════════════════════════════════
SYM          = "ONUSDT"
POSITION_SIDE = "SHORT"          # SHORT 或 LONG
CRON_JOB_ID  = "a2e1854b-e756-416a-8149-ebba03615786"  # 平仓后自动删除此 cron
NOTIFY_TO    = "432401404:thread:019f9203-3b29-7d9e-bfb4-b67c0b06587c"
STATE_FILE   = f"/root/.openclaw/workspace/on_monitor_state.json"

# 多头拉升触发阈值（空单平仓）
BULL_SURGE_A_MOM = 5.0   # 15m 近4根涨幅 > 5% 且 RSI>55
BULL_SURGE_B_MOM = 8.0   # 15m 近4根涨幅 > 8%（急拉，无视RSI）
LOSS_PROTECT_PCT = 8.0   # 浮亏超过 8% 触发保护（价格反向移动）

# 止盈下移触发阈值
TP_ADJUST_DROP = 1.0     # 价格下跌 > 1% 且在盈利区，触发止盈下移

# ══════════════════════════════════════════
# 工具函数
# ══════════════════════════════════════════
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except:
        return {}

def save_state(s):
    with open(STATE_FILE, "w") as f:
        json.dump(s, f, indent=2)

def notify(msg):
    """通过 message 工具推送消息（由 cron agent 执行时会调用）"""
    # 直接 print 让 cron agent 读到并发送
    print(f"__NOTIFY__:{msg}")

def close_cron():
    """平仓后删除自身 cron 任务"""
    import subprocess
    subprocess.run(["openclaw", "cron", "rm", CRON_JOB_ID], capture_output=True)

# ══════════════════════════════════════════
# 主逻辑
# ══════════════════════════════════════════
state   = load_state()
now_str = datetime.datetime.utcnow().strftime('%H:%M UTC')

# ── 检查仓位 ──
positions = api.get_positions()
on_pos    = [p for p in positions
             if p.get('symbol') == SYM
             and p.get('positionSide', '').upper() == POSITION_SIDE.upper()
             and float(p.get('positionAmt', 0)) != 0]

# 仓位已关闭 → 关闭监控（双重确认：positionSide + positionAmt）
if not on_pos:
    # 再查一次防止 API 抖动
    import time as _time
    _time.sleep(2)
    positions2 = api.get_positions()
    on_pos2    = [p for p in positions2
                  if p.get('symbol') == SYM
                  and p.get('positionSide', '').upper() == POSITION_SIDE.upper()
                  and float(p.get('positionAmt', 0)) != 0]
    if not on_pos2:
        # 第三次确认，再等 5s，径除偏差性空返回
        _time.sleep(5)
        positions3 = api.get_positions()
        on_pos3    = [p for p in positions3
                      if p.get('symbol') == SYM
                      and p.get('positionSide', '').upper() == POSITION_SIDE.upper()
                      and float(p.get('positionAmt', 0)) != 0]
        if not on_pos3:
            close_cron()
            notify(f"✅ **{SYM} {POSITION_SIDE} 仓位已关闭，监控任务自动停止** | {now_str}")
            # 止盈全仓平仓后：等1分钟，再分析多空方向，择优挂单
            if state.get('mode') == 'full_exit_then_reentry':
                import time as _t2
                notify(f"⏳ **{SYM} 止盈触达，等待1分钟后分析再入场方向...** | {now_str}")
                _t2.sleep(60)
                try:
                    _analyze_reentry(SYM)
                except Exception as _e:
                    notify(f"⚠️ {SYM} 再入场分析失败: {_e}")
            sys.exit(0)
        on_pos = on_pos3  # 第三次确认有仓位，继续执行
    on_pos = on_pos2  # 第二次确认有仓位，继续执行

p      = on_pos[0]
amt    = float(p['positionAmt'])
entry  = float(p['entryPrice'])
mark   = float(p['markPrice'])
pnl    = float(p['unRealizedProfit'])
liq    = float(p['liquidationPrice'])

# 空单：价格上涨 = 亏损，价格下跌 = 盈利
# pct_move = 价格相对均价的变动方向（正=价格上涨）
pct_move  = (mark - entry) / entry * 100
is_profit = (POSITION_SIDE == "SHORT" and mark < entry) or \
            (POSITION_SIDE == "LONG"  and mark > entry)
profit_pct = abs(pct_move) if is_profit else -abs(pct_move)

# ── 拉市场数据 ──
k1h  = requests.get("https://fapi.binance.com/fapi/v1/klines",
    params={'symbol': SYM, 'interval': '1h', 'limit': 50}, timeout=8).json()
k4h  = requests.get("https://fapi.binance.com/fapi/v1/klines",
    params={'symbol': SYM, 'interval': '4h', 'limit': 50}, timeout=8).json()
k15m = requests.get("https://fapi.binance.com/fapi/v1/klines",
    params={'symbol': SYM, 'interval': '15m', 'limit': 20}, timeout=8).json()

c1h     = [float(k[4]) for k in k1h]
c15m_cl = [float(k[4]) for k in k15m]
rsi_1h  = el.rsi_calc(c1h)
atr_14  = el.atr_calc(k1h, 14)

# 15m 近4根动量
p15m_4  = [float(k[4]) for k in k15m[-4:]]
mom_15m = (p15m_4[-1] - p15m_4[0]) / p15m_4[0] * 100 if p15m_4[0] > 0 else 0

# 上次记录的价格
last_price = state.get('last_price', mark)

# ── 当前挂单 ──
open_orders = api.get_open_orders()
on_orders   = [o for o in open_orders if o.get('symbol') == SYM]
sl_orders   = [o for o in on_orders if o.get('type') in ('STOP_MARKET', 'STOP')]
tp_orders   = [o for o in on_orders if o.get('side') == ('BUY' if POSITION_SIDE=='SHORT' else 'SELL')
               and o.get('type') == 'LIMIT']

# ══════════════════════════════════════════
# 情况1：多头拉升 / 反向保护 → 市价平仓
# ══════════════════════════════════════════
bull_surge_A = (POSITION_SIDE == "SHORT") and mom_15m > BULL_SURGE_A_MOM and rsi_1h > 55
bull_surge_B = (POSITION_SIDE == "SHORT") and mom_15m > BULL_SURGE_B_MOM
loss_protect = (POSITION_SIDE == "SHORT") and pct_move > LOSS_PROTECT_PCT

if bull_surge_A or bull_surge_B or loss_protect:
    if loss_protect:
        reason = f"浮亏超{LOSS_PROTECT_PCT}%（当前{pct_move:.1f}%），触发保护性平仓"
    elif bull_surge_B:
        reason = f"15m急拉 +{mom_15m:.1f}%，空头挤压风险"
    else:
        reason = f"15m涨 +{mom_15m:.1f}% + RSI 1H={rsi_1h:.0f}，多头动能转强"

    # 取消全部挂单
    for o in on_orders:
        api.cancel_order(SYM, o['orderId'])

    # 市价平仓
    close_qty = str(abs(int(amt)))
    close_side = "BUY" if POSITION_SIDE == "SHORT" else "SELL"
    r = api.place_order(
        symbol=SYM, side=close_side, order_type="MARKET",
        quantity=close_qty, positionSide=POSITION_SIDE
    )

    msg = (
        f"⚡ **{SYM} {POSITION_SIDE} 已市价平仓** | {now_str}\n\n"
        f"触发：{reason}\n"
        f"平仓均价：约 {mark:.5f}\n"
        f"仓位盈亏：{pnl:+.2f}U（{profit_pct:.1f}%）\n"
        f"订单号：{r.get('orderId','?')}"
    )
    notify(msg)
    close_cron()

    # ══════════════════════════════════════════
    # 平仓后：分析是否具备开多条件，若具备则挂限价单
    # ══════════════════════════════════════════
    try:
        _analyze_reentry(SYM)
    except Exception as e:
        notify(f"⚠️ 平仓后开多分析失败: {e}")

    sys.exit(0)

# ══════════════════════════════════════════
# 止损下单辅助函数（用 algo order，兼容双向持仓模式）
# ══════════════════════════════════════════
def _place_sl(stop_price_fmt):
    """挂止损单（algo STOP_MARKET），返回 algoId 字符串或 None"""
    try:
        r = api.place_algo_order(
            symbol=SYM,
            side="BUY" if POSITION_SIDE == "SHORT" else "SELL",
            order_type="STOP_MARKET",
            trigger_price=stop_price_fmt,
            position_side=POSITION_SIDE,
            quantity=str(abs(int(amt)))
        )
        oid = r.get('algoId') or r.get('orderId')
        if oid:
            return str(oid)
    except Exception as e:
        pass
    return None

def _cancel_sl(sl_id):
    """取消止损单，兼容 algoId 和 orderId"""
    try:
        api.cancel_algo_order(SYM, int(sl_id))
    except Exception:
        try:
            api.cancel_order(SYM, sl_id)
        except Exception:
            pass

# ══════════════════════════════════════════
# 情况2 & 3：动态止损（随盈利扩大逐步上移）
# 空单止损始终在均价上方，盈利越大止损越低
# ══════════════════════════════════════════
# 止损档位（空单方向：止损价 = 均价 × 系数，系数>1=亏损保护，系数<1=锁定利润）
# 浮盈 ≥3%  → 止损移至均价（保本）
# 浮盈 ≥5%  → 止损移至均价×0.98（锁住2%）
# 浮盈 ≥8%  → 止损移至均价×0.97（锁住3%+）
# 浮盈 ≥12% → 止损移至均价×0.95（锁住5%+）
# 浮盈 ≥18% → 止损移至均价×0.92（锁住8%+）

SL_TIERS = [
    (18.0, 0.92),
    (12.0, 0.95),
    ( 8.0, 0.97),
    ( 5.0, 0.98),
    ( 3.0, 1.00),
]

def _target_sl_price():
    """根据当前盈利档位返回目标止损价，未达3%返回 None"""
    for threshold, factor in SL_TIERS:
        if profit_pct >= threshold:
            return entry * factor
    return None

target_sl = _target_sl_price()

# 读取当前已挂止损单（algo order 不在 open_orders，从 state 读取）
sl_orders_live = [o for o in on_orders
                  if o.get('type') in ('STOP_MARKET', 'STOP')
                  and o.get('side') == ('BUY' if POSITION_SIDE=='SHORT' else 'SELL')]
current_sl_price = float(sl_orders_live[0].get('stopPrice') or sl_orders_live[0].get('price')) \
                   if sl_orders_live else state.get('sl_price')
sl_order_id = sl_orders_live[0].get('orderId') if sl_orders_live \
              else (state.get('sl_order_id') if state.get('sl_activated') else None)

tick = 0.00001

if target_sl is not None:
    target_sl_fmt = el.fmt_price(target_sl, tick)
    target_sl_f   = float(target_sl_fmt)

    need_place  = current_sl_price is None
    # 空单：止损只能往下移（新价 < 当前价，即更低更安全）
    need_update = (current_sl_price is not None) and (target_sl_f < current_sl_price - tick)

    if need_place or need_update:
        if sl_order_id:
            _cancel_sl(sl_order_id)
        new_oid = _place_sl(target_sl_fmt)

        # 描述触发档位
        for thr, fac in SL_TIERS:
            if profit_pct >= thr:
                tier_desc = f"盈利 {profit_pct:.1f}% ≥{thr}%，止损→均价×{fac}"
                break

        if new_oid:
            action = "首次挂止损" if need_place else f"止损上移 {current_sl_price:.5f}→{target_sl_fmt}"
            msg = (
                f"🛡️ **{SYM} {action}** | {now_str}\n\n"
                f"触发：{tier_desc}\n"
                f"止损价：{target_sl_fmt}\n"
                f"当前盈利：{profit_pct:.2f}%（{pnl:+.2f}U）\n"
                f"订单号：#{new_oid}"
            )
            notify(msg)
            state['sl_activated'] = True
            state['sl_price']     = target_sl_f
            state['sl_order_id']  = new_oid
            save_state(state)
        else:
            notify(
                f"⚠️ **{SYM} 止损挂单失败** | {now_str}\n"
                f"目标价：{target_sl_fmt} | 请手动检查"
            )

sl_activated = state.get('sl_activated', False)

# ══════════════════════════════════════════
# 情况4：价格下跌（空单顺势）→ 下移止盈
# 支持分批模式：只下移 TP1，TP2/TP3 保持不动
# ══════════════════════════════════════════
price_drop = (last_price - mark) / last_price * 100 if last_price > 0 else 0
bear_move  = price_drop > TP_ADJUST_DROP and is_profit

if bear_move:
    lmap    = el.liquidation_map(SYM, k1h, k4h, POSITION_SIDE)
    tick    = 0.00001
    tp1_new = lmap['soft_tp1']
    tp1_fmt = el.fmt_price(tp1_new, tick)

    # 只修改 TP1，且仅向下移
    cur_tp1_order = [o for o in tp_orders
                     if o.get('orderId') == state.get('tp1_order')]
    cur_tp1_price = float(cur_tp1_order[0]['price']) if cur_tp1_order else None

    if cur_tp1_price is None or float(tp1_fmt) < cur_tp1_price - tick:
        tp1_qty = state.get('tp1_qty') or (abs(int(amt)) // 3)
        # 取消旧 TP1
        if cur_tp1_order:
            api.cancel_order(SYM, cur_tp1_order[0]['orderId'])
        r_tp1 = api.place_order(
            symbol=SYM, side="BUY", order_type="LIMIT",
            quantity=str(tp1_qty), price=tp1_fmt,
            positionSide=POSITION_SIDE, timeInForce="GTC"
        )
        tp1_pnl = (entry - float(tp1_fmt)) * tp1_qty
        msg = (
            f"📉 **{SYM} TP1 下移** | {now_str}\n\n"
            f"价格从 {last_price:.5f} 跌至 {mark:.5f}（-{price_drop:.1f}%）\n"
            f"新 TP1：{tp1_fmt} @ {tp1_qty} ON → +{tp1_pnl:.1f}U\n"
            f"当前盈利：{pnl:+.2f}U"
        )
        notify(msg)
        state['tp1_order'] = r_tp1.get('orderId')
        state['tp1_price'] = float(tp1_fmt)
        save_state(state)

# ══════════════════════════════════════════
# 再入场分析：多空双向评分，择优自动挂单
# ══════════════════════════════════════════
def _analyze_reentry(sym):
    """
    同时输出多空两个方向评分，择高分方向。
    兩方评分都达标→选更高的。
    都未达标→推送评分明细，不自动开仓。
    """
    import json as _json, time as _t
    now_r = datetime.datetime.utcnow().strftime('%H:%M UTC')
    NOTIONAL = 1000
    tick = 0.00001
    step = 1.0

    # ── 拉公共数据 ──
    k1h  = requests.get("https://fapi.binance.com/fapi/v1/klines",
        params={'symbol': sym, 'interval': '1h',  'limit': 100}, timeout=8).json()
    k4h  = requests.get("https://fapi.binance.com/fapi/v1/klines",
        params={'symbol': sym, 'interval': '4h',  'limit': 100}, timeout=8).json()
    k15m = requests.get("https://fapi.binance.com/fapi/v1/klines",
        params={'symbol': sym, 'interval': '15m', 'limit': 60},  timeout=8).json()
    ticker   = api.get_ticker_24h(sym)
    fr_data  = requests.get(f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={sym}", timeout=5).json()
    fr_raw   = float(fr_data.get('lastFundingRate', 0))
    fr_pct   = fr_raw * 100
    price    = float(k1h[-1][4])
    chg24    = float(ticker.get('priceChangePercent', 0))
    c1h      = [float(k[4]) for k in k1h]
    c4h      = [float(k[4]) for k in k4h]
    rsi_1h   = el.rsi_calc(c1h)
    rsi_4h   = el.rsi_calc(c4h)
    atr      = el.atr_calc(k1h, 14)
    ema20_4h = el.ema_calc(c4h[-20:], 20)
    ema50_4h = el.ema_calc(c4h[-50:], 50)

    blocked, block_reason = el.check_funding_settlement_window(fr_pct)
    oi_div, _             = el.check_oi_divergence(sym)

    # ── 评分 ──
    sig_type = el.classify_signal(k1h, k4h, fr_raw, chg24)
    if sig_type == 'trend':
        score, _ = el.calc_score_trend(sym, k1h, k4h, k15m, ticker, fr_raw)
        threshold = 75
    else:
        score, _ = el.calc_score_reversal(sym, k1h, k4h, k15m, ticker, fr_raw)
        threshold = 65

    # liquidation_map 双方向
    lmap_s = el.liquidation_map(sym, k1h, k4h, 'SHORT')
    lmap_l = el.liquidation_map(sym, k1h, k4h, 'LONG')

    # ── 决策逻辑 ──
    # 空单所需： RSI超买 + 评分达标 + 未拦截
    can_short = (score >= threshold and not blocked and not oi_div
                 and (rsi_1h > 65 or rsi_4h > 60))
    # 多单所需： RSI超卖 + 评分达标 + 未拦截
    can_long  = (score >= threshold and not blocked and not oi_div
                 and (rsi_1h < 35 or rsi_4h < 40))

    # 两者同时达标时选 RSI 偏差更大的方向
    if can_short and can_long:
        short_bias = rsi_1h - 65
        long_bias  = 35 - rsi_1h
        can_short  = short_bias >= long_bias
        can_long   = not can_short

    def _place_and_notify(side, lmap):
        """side = 'SHORT' | 'LONG' — 全仓打 TP1，不分批"""
        posside    = side
        order_side = 'SELL' if side == 'SHORT' else 'BUY'
        tp_side    = 'BUY'  if side == 'SHORT' else 'SELL'

        limit_p = lmap.get('safe_entry', price * (1.005 if side=='SHORT' else 0.995))
        sl_p    = lmap.get('hard_sl',   price * (1.03  if side=='SHORT' else 0.97))
        tp1_p   = lmap.get('soft_tp1',  price * (0.95  if side=='SHORT' else 1.05))

        # Coinglass 第一止盈位修正
        try:
            import coinglass_api as _cg
            _zones = _cg.get_liq_zones(sym, price, range_='3d', top_n=3)
            _below = _zones.get('below', [])
            _above = _zones.get('above', [])
            if side == 'SHORT' and _below:
                tp1_p = _below[0][0]   # 下方最近清算密集区
            elif side == 'LONG' and _above:
                tp1_p = _above[0][0]   # 上方最近清算密区
        except Exception:
            pass

        tick      = 0.00001
        step      = 1.0
        limit_fmt = el.fmt_price(limit_p, tick)
        sl_fmt    = el.fmt_price(sl_p,    tick)
        tp1_fmt   = el.fmt_price(tp1_p,   tick)

        qty     = NOTIONAL / float(limit_fmt)
        qty_fmt = el.fmt_qty(qty, step)

        entry_pct = (float(limit_fmt) - price) / price * 100
        sl_pct    = abs(float(sl_fmt)  - float(limit_fmt)) / float(limit_fmt) * 100
        tp_pct    = abs(float(tp1_fmt) - float(limit_fmt)) / float(limit_fmt) * 100
        tp1_pnl   = abs(float(tp1_fmt) - float(limit_fmt)) * float(qty_fmt)
        rr        = tp_pct / sl_pct if sl_pct > 0 else 0

        # 检查仓位上限和余额
        balance_c   = api.request("GET", api.BASE_FAPI + "/fapi/v2/balance", {})
        avail_c     = float([b for b in balance_c if b['asset']=='USDT'][0]['availableBalance'])
        positions_c = api.get_positions()
        slots_c     = len([p2 for p2 in positions_c if float(p2.get('positionAmt',0)) != 0])

        if slots_c >= 5 or avail_c < 50:
            notify(
                f"🟡 **{sym} 再入场分析 — {side}** | {now_r}\n\n"
                f"评分={score} 具备开{'空' if side=='SHORT' else '多'}，但仓位已满或余额不足\n"
                f"仓位={slots_c}/5 | 可用={avail_c:.1f}U\n"
                f"参考入场价：{limit_fmt} | TP1：{tp1_fmt} | SL：{sl_fmt}"
            )
            return

        # 设杆杆
        api.request("POST", "https://fapi.binance.com/fapi/v1/leverage",
            {"symbol": sym, "leverage": 50})

        # 挂入场限价单
        r_entry = api.place_order(
            symbol=sym, side=order_side, order_type="LIMIT",
            quantity=qty_fmt, price=limit_fmt,
            positionSide=posside, timeInForce="GTC"
        )
        if not r_entry.get('orderId'):
            notify(f"⚠️ {sym} {side} 入场挂单失败: {r_entry}")
            return

        # 全仓单档止盈（只打 TP1）
        r_tp1 = api.place_order(
            symbol=sym, side=tp_side, order_type="LIMIT",
            quantity=qty_fmt, price=tp1_fmt,
            positionSide=posside, timeInForce="GTC"
        )

        notify(
            f"🟢 **{sym} 再入场 {side} 已挂单** | {now_r}\n\n"
            f"评分：{score}（{sig_type}）| RSI 1H={rsi_1h:.0f} 4H={rsi_4h:.0f} | FR={fr_pct:.3f}%\n"
            f"入场限价：{limit_fmt}（距现价 {entry_pct:+.1f}%）\n"
            f"止损：{sl_fmt}（-{sl_pct:.1f}%）| TP1：{tp1_fmt}（+{tp_pct:.1f}%，预计+{tp1_pnl:.1f}U）\n"
            f"盈亏比 1:{rr:.1f} | 名义 {NOTIONAL}U | 全仓单档止盈\n"
            f"入场#{r_entry.get('orderId')} | TP1#{r_tp1.get('orderId')}"
        )

    if can_short:
        _place_and_notify('SHORT', lmap_s)
    elif can_long:
        _place_and_notify('LONG', lmap_l)
    else:
        reasons = []
        if score < threshold:       reasons.append(f"评分{score}<{threshold}")
        if blocked:                 reasons.append(f"资金费率结算窗口")
        if oi_div:                  reasons.append("OI背离")
        if not (rsi_1h > 65 or rsi_4h > 60 or rsi_1h < 35 or rsi_4h < 40):
            reasons.append(f"RSI中性区间(1H={rsi_1h:.0f},4H={rsi_4h:.0f})")
        notify(
            f"🟡 **{sym} 暂无清晰再入场信号** | {now_r}\n\n"
            f"评分：{score}（{sig_type}）| 原因：{', '.join(reasons)}\n"
            f"RSI 1H={rsi_1h:.0f} 4H={rsi_4h:.0f} | FR={fr_pct:.3f}%\n"
            f"EMA 4H={ema4h_dir_r} | 充分评价后再考虑建仓"
        )


# ══════════════════════════════════════════
# 无异常 → 静默
# ══════════════════════════════════════════

    """空单平仓后立即分析是否具备开多条件，若具备则挂 300U 限价单（带止盈止损）"""
    now = datetime.datetime.utcnow().strftime('%H:%M UTC')

    k1h_l  = requests.get("https://fapi.binance.com/fapi/v1/klines",
        params={'symbol': sym, 'interval': '1h',  'limit': 100}, timeout=8).json()
    k4h_l  = requests.get("https://fapi.binance.com/fapi/v1/klines",
        params={'symbol': sym, 'interval': '4h',  'limit': 100}, timeout=8).json()
    k15m_l = requests.get("https://fapi.binance.com/fapi/v1/klines",
        params={'symbol': sym, 'interval': '15m', 'limit': 60},  timeout=8).json()
    ticker_l = api.get_ticker_24h(sym)

    price_l  = float(k1h_l[-1][4])
    atr_l    = el.atr_calc(k1h_l, 14)
    c1h_l    = [float(k[4]) for k in k1h_l]
    c4h_l    = [float(k[4]) for k in k4h_l]
    rsi_1h_l = el.rsi_calc(c1h_l)
    rsi_4h_l = el.rsi_calc(c4h_l)

    ema20_1h = el.ema_calc(c1h_l[-20:], 20)
    ema50_1h = el.ema_calc(c1h_l[-50:], 50)
    ema20_4h = el.ema_calc(c4h_l[-20:], 20)
    ema50_4h = el.ema_calc(c4h_l[-50:], 50)

    vols_l = [float(k[5]) for k in k1h_l]
    vavg_l = sum(vols_l[-20:-3]) / 17
    vr_l   = sum(vols_l[-3:]) / 3 / vavg_l if vavg_l > 0 else 1

    fr_l   = requests.get(f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={sym}", timeout=5).json()
    fr_raw = float(fr_l.get('lastFundingRate', 0))
    fr_pct = fr_raw * 100

    chg24_l = float(ticker_l.get('priceChangePercent', 0))

    # 评分
    sig_type_l = el.classify_signal(k1h_l, k4h_l, fr_raw, chg24_l)
    if sig_type_l == 'trend':
        score_l, _ = el.calc_score_trend(sym, k1h_l, k4h_l, k15m_l, ticker_l, fr_raw)
        threshold  = 75
    else:
        score_l, _ = el.calc_score_reversal(sym, k1h_l, k4h_l, k15m_l, ticker_l, fr_raw)
        threshold  = 65

    # 资金费率结算窗口拦截
    blocked_l, block_reason_l = el.check_funding_settlement_window(fr_pct)

    # OI 背离
    oi_div_l, _ = el.check_oi_divergence(sym)

    # liquidation_map（多单方向）
    lmap_l = el.liquidation_map(sym, k1h_l, k4h_l, 'LONG')

    # RSI 超卖判断（逆势反弹核心条件）
    rsi_oversold = rsi_1h_l < 35 or rsi_4h_l < 40

    # 是否具备开多条件
    can_long = (
        score_l >= threshold
        and not blocked_l
        and not oi_div_l
        and rsi_oversold
        and (ema20_4h > ema50_4h)  # 4H 多排，大趋势向上
    )

    if not can_long:
        reasons = []
        if score_l < threshold:  reasons.append(f"评分{score_l}<{threshold}")
        if blocked_l:            reasons.append(f"结算窗口拦截")
        if oi_div_l:             reasons.append("OI背离")
        if not rsi_oversold:     reasons.append(f"RSI未超卖(1H={rsi_1h_l:.0f},4H={rsi_4h_l:.0f})")
        if ema20_4h <= ema50_4h: reasons.append("4H空排")

        notify(
            f"📊 **{sym} 平仓后开多分析** | {now}\n\n"
            f"❌ 暂不具备开多条件\n"
            f"原因：{', '.join(reasons)}\n"
            f"评分：{score_l}（类型={sig_type_l}）| RSI 1H={rsi_1h_l:.0f} 4H={rsi_4h_l:.0f}\n"
            f"EMA 4H={ema4h_dir_r} | 量比={vr_l:.2f}x | FR={fr_pct:.3f}%"
        )
        return

    # ── 具备条件，计算入场位 ──
    NOTIONAL_LONG = 1000
    tick_l = 0.00001
    step_l = 1.0

    # PULLBACK 入场（等回调到清算密集带上方）
    limit_l   = lmap_l['safe_entry']
    sl_l      = lmap_l['hard_sl']
    tp1_l     = lmap_l['soft_tp1']
    tp2_l     = lmap_l['soft_tp2']

    limit_fmt_l = el.fmt_price(limit_l, tick_l)
    sl_fmt_l    = el.fmt_price(sl_l,    tick_l)
    tp1_fmt_l   = el.fmt_price(tp1_l,   tick_l)
    tp2_fmt_l   = el.fmt_price(tp2_l,   tick_l)

    qty_l     = NOTIONAL_LONG / float(limit_fmt_l)
    qty_fmt_l = el.fmt_qty(qty_l, step_l)

    tp1_pnl_l = (float(tp1_fmt_l) - float(limit_fmt_l)) * float(qty_fmt_l)
    sl_pnl_l  = (float(limit_fmt_l) - float(sl_fmt_l))  * float(qty_fmt_l)
    rr_l      = tp1_pnl_l / sl_pnl_l if sl_pnl_l > 0 else 0

    # 检查仓位上限
    balance_l  = api.request("GET", api.BASE_FAPI + "/fapi/v2/balance", {})
    avail_l    = float([b for b in balance_l if b['asset']=='USDT'][0]['availableBalance'])
    positions_l = api.get_positions()
    slots_l    = len([p2 for p2 in positions_l if float(p2.get('positionAmt',0)) != 0])

    if slots_l >= 5 or avail_l < 20:
        notify(
            f"📊 **{sym} 平仓后开多分析** | {now}\n\n"
            f"✅ 评分={score_l} 具备反弹开多条件，但仓位已满或余额不足\n"
            f"仓位={slots_l}/5 | 可用余额={avail_l:.1f}U\n"
            f"参考入场价：{limit_fmt_l}，等仓位释放后手动挂单"
        )
        return

    # 挂限价开多单
    api.request("POST", "https://fapi.binance.com/fapi/v1/leverage",
        {"symbol": sym, "leverage": 50})

    r_long = api.place_order(
        symbol=sym, side="BUY", order_type="LIMIT",
        quantity=qty_fmt_l, price=limit_fmt_l,
        positionSide="LONG", timeInForce="GTC"
    )

    if not r_long.get('orderId'):
        notify(f"⚠️ {sym} 开多挂单失败: {r_long}")
        return

    # 同时挂止盈止损（成交后由 position_manager 跟踪，这里先记录到 state）
    long_state = {
        "symbol": sym,
        "direction": "LONG",
        "entry_price": float(limit_fmt_l),
        "sl_price": float(sl_fmt_l),
        "tp1_price": float(tp1_fmt_l),
        "tp2_price": float(tp2_fmt_l),
        "qty": float(qty_fmt_l),
        "notional": NOTIONAL_LONG,
        "score": score_l,
        "order_id": r_long.get('orderId'),
        "status": "pending",
        "note": f"空单平仓后自动开多，反弹搏单 | {now}"
    }
    import os
    ls_file = f"/root/.openclaw/workspace/on_long_state.json"
    with open(ls_file, 'w') as f:
        json.dump(long_state, f, indent=2)

    notify(
        f"🟢 **{sym} 平仓后自动开多挂单** | {now}\n\n"
        f"评分：{score_l}（{sig_type_l}）| RSI 1H={rsi_1h_l:.0f} 4H={rsi_4h_l:.0f}\n"
        f"限价入场：{limit_fmt_l}（距现价 {(float(limit_fmt_l)-price_l)/price_l*100:+.1f}%）\n"
        f"止损：{sl_fmt_l}（距入场 -{(float(limit_fmt_l)-float(sl_fmt_l))/float(limit_fmt_l)*100:.1f}%）\n"
        f"TP1：{tp1_fmt_l}（距入场 +{(float(tp1_fmt_l)-float(limit_fmt_l))/float(limit_fmt_l)*100:.1f}%，预计 +{tp1_pnl_l:.1f}U）\n"
        f"TP2：{tp2_fmt_l}（距入场 +{(float(tp2_fmt_l)-float(limit_fmt_l))/float(limit_fmt_l)*100:.1f}%）\n"
        f"名义：{NOTIONAL_LONG}U | 数量：{qty_fmt_l} ON | 盈亏比 1:{rr_l:.1f}"
    )


# ══════════════════════════════════════════
# 无异常 → 静默
# ══════════════════════════════════════════
state['last_price'] = mark
save_state(state)
print("HEARTBEAT_OK")
