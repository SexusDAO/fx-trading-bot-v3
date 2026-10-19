# 任务二：观察区细筛（每10分钟）
<!-- 由 watchlist-finetune-10m cron 触发 -->

## 强制规则

- 全程静默执行，禁止英文输出、中间步骤、进度说明
- 除「开仓成功」外一律 NO_REPLY
- API 调用间隔 ≥ 300ms
- 禁止使用 binance-cli，所有调用走代理模块：

```python
import sys; sys.path.insert(0,"/root/.openclaw/workspace")
import binance_api as api
import coinglass_api as cg
```

---

## 第一步：熔断检查

读 `/root/.openclaw/workspace/circuit_breaker.json`：
- `circuit_open=true` 且 `date=今日UTC日期` → 观察区正常维护评分，但**不执行任何开仓操作**，NO_REPLY

---

## 第二步：读取观察区 & 账户状态

```python
# 观察区
with open('/root/.openclaw/workspace/watchlist.json') as f:
    watchlist = json.load(f)
candidates = watchlist.get('candidates', [])
if not candidates:
    # 无候选 → NO_REPLY
    pass

# 账户状态（仅用于开仓判断，不展示）
balance   = api.get_futures_balance()
usdt_avail = float([b for b in balance if b['asset']=='USDT'][0]['availableBalance'])
positions = api.get_positions()
held      = {p['symbol'] for p in positions if float(p.get('positionAmt',0)) != 0}
slot_used = len(held)

# BTC 大环境（拉一次，供所有标的共用）
btc_k1h = api.get_klines('BTCUSDT','1h',50)
btc_ma7  = sum(float(k[4]) for k in btc_k1h[-7:]) / 7
btc_ma25 = sum(float(k[4]) for k in btc_k1h[-25:]) / 25
btc_trend = 'UP' if btc_ma7 > btc_ma25 else 'DOWN'
```

---

## 第三步：逐个重新评分（串行，每币间隔 300ms）

对观察区每个标的执行完整精筛评分：

```python
k1h  = api.get_klines(symbol, '1h',  50)
k4h  = api.get_klines(symbol, '4h',  30)
k15m = api.get_klines(symbol, '15m', 60)
ticker = api.get_ticker_24h(symbol)
```

**AI 自主评分维度（满分100）：**

| 维度 | 参考权重 | 说明 |
|------|---------|------|
| 多周期趋势结构 | ~25% | 1H/4H MA趋势方向共振 |
| RSI 位置 | ~20% | 是否处于健康入场区间 |
| 量能配合 | ~20% | 量比是否支撑方向 |
| 资金费率 | ~15% | 多空成本是否合理 |
| 短周期动量 | ~15% | 15m 方向确认 |
| BTC 大环境 | ~5% | btc_trend 是否支持方向 |

> AI 可根据市场状态自行调整权重，纳入额外维度（波动率、24h涨跌位置、资金流向等）。

**评分 ≥ 60 时可选调用 Coinglass 辅助：**
```python
# 调用时机：评分≥75 且距上次调用 ≥ 30min
cg_result = cg.get_signal_for_open(symbol, direction)
score += cg_result.get('score_adjust', 0)  # LSR超时时 score_adjust=0
```

**更新 watchlist.json：**
```python
# 先备份旧分，再写新分
candidate['prev_score']    = candidate.get('score')
candidate['score']         = new_score
candidate['current_price'] = price_now
candidate['last_reviewed'] = int(time.time())
candidate['next_review']   = int(time.time()) + 600
```

---

## 第四步：按评分决策

### 评分 < 60 → 立即移出观察区，不通知，NO_REPLY

### 评分 60~74 → 继续留在观察区，等待下次复盘，NO_REPLY

### 评分 75~84 → AI 综合判断是否开仓

综合考虑：
- **量比门槛（动态）**：北京时间 04:00~20:00 量比 ≥ 1.0x；凌晨 UTC 20:00~04:00 量比 ≥ 1.3x
- RSI 是否在入场健康区
- BTC 大环境是否配合
- 是否存在顶底背离等风险
- 评分变化趋势（持续上升 vs 震荡）

```python
import datetime
utc_hour = datetime.datetime.utcnow().hour
vol_threshold = 1.3 if (20 <= utc_hour or utc_hour < 4) else 1.0
# 量比不达门槛 → 继续留在观察区，不开仓
```

AI 自主决定：**挂单** 或 **继续观察**。无需通知，无需确认。

**硬性拦截（任一触发则跳过，继续留观察区）：**
1. 同一 symbol 已有持仓
2. 熔断中
3. 可用余额 < 20U
4. 资金费率 fr > 0.5%
5. 量比 < 当前时段门槛
6. **当前持仓数 ≥ 5 → 暂停开仓，等仓位降至 5 个以下再恢复**
7. **加仓名义量超限**：该 symbol 现有持仓名义量 + 本次开仓名义量 > 600U → 硬拦截
8. **追涨开空拦截**：direction=SHORT 且近 2 根 1H K 线涨幅均 > 3% → 拦截，等回调
   ```python
   if direction == 'SHORT':
       last2_chg = [(float(k1h[-i][4])-float(k1h[-i][1]))/float(k1h[-i][1])*100 for i in [1,2]]
       if all(c > 3.0 for c in last2_chg): skip("追涨开空拦截")
   ```
9. **凌晨BTC弱势拦截**：UTC 20:00~06:00 且 direction=LONG 且 BTC MA7 < MA25 → 拦截
   ```python
   utc_h = datetime.datetime.utcnow().hour
   if (utc_h >= 20 or utc_h < 6) and direction == 'LONG':
       if btc_ma7 < btc_ma25: skip("凌晨BTC下行，禁止开多")
   ```

若决定挂单 → 执行 **Step 5 开仓流程**。

### 评分 ≥ 85 → 当仓位 < 5 个时必须挂单，否则暂停待仓位释放

- 量比同样执行动态门槛（凌晨 1.3x，其他时段 1.0x）；低于门槛则降级为 75~84 处理
- 调用一次 Coinglass 最终确认（超时则按原评分执行）
- 执行 **Step 5 开仓流程**
- 挂单成功后推送通知并移出观察区

---

## Step 5: 开仓执行

```python
import datetime

# ══════════════════════════════════════════════════════════════════
# 零、清算热力图替代方案：推算清算密集区（入场/止损/止盈全部锚定到这里）
# ══════════════════════════════════════════════════════════════════

def liquidation_map(symbol, k1h, k4h, direction):
    """
    用 OI变化 + 资金费率 + 多空比 + 订单簿 联合推算清算密集区。
    返回字典：
      liq_below  : 下方最近清算密集价（多头爆仓区域）
      liq_above  : 上方最近清算密集价（空头爆仓区域）
      safe_entry : 推荐入场价（避开密集区中心，在流动性好的位置）
      hard_sl    : 推荐止损（清算密集区外侧 + ATR buffer）
      soft_tp1   : 第一目标（下一个清算密集区前沿）
      soft_tp2   : 第二目标（延伸目标，如无明显阻力则 ATR×3）
      entry_reason: 入场逻辑说明（供通知显示）
    """
    import requests as _req

    price   = float(k1h[-1][4])
    atr_14  = sum(abs(float(k[2])-float(k[3])) for k in k1h[-14:]) / 14
    highs20 = [float(k[2]) for k in k1h[-20:]]
    lows20  = [float(k[3]) for k in k1h[-20:]]
    vols1h  = [float(k[5]) for k in k1h]

    # ── 1. 订单簿：找短期支撑/阻力墙 ──
    try:
        ob = _req.get(f'https://fapi.binance.com/fapi/v1/depth?symbol={symbol}&limit=100', timeout=5).json()
        bids = [(float(p), float(q)) for p,q in ob.get('bids',[])]  # 买盘支撑
        asks = [(float(p), float(q)) for p,q in ob.get('asks',[])]  # 卖盘阻力
        # 找最大挂单墙（前20档中qty最大的）
        top_bid = max(bids[:20], key=lambda x: x[1], default=(price*0.98, 0))
        top_ask = max(asks[:20], key=lambda x: x[1], default=(price*1.02, 0))
        ob_support    = top_bid[0]   # 最厚买单墙 = 多头防守位
        ob_resistance = top_ask[0]   # 最厚卖单墙 = 空头防守位
    except:
        ob_support    = price - atr_14 * 1.0
        ob_resistance = price + atr_14 * 1.0

    # ── 2. OI 变化：判断是否有持仓过度积累 ──
    oi_increasing = False
    try:
        oi_data = _req.get(
            f'https://fapi.binance.com/futures/data/openInterestHist?symbol={symbol}&period=1h&limit=6',
            timeout=5).json()
        if isinstance(oi_data, list) and len(oi_data) >= 3:
            oi_vals = [float(x['sumOpenInterest']) for x in oi_data]
            oi_chg3h = (oi_vals[-1] - oi_vals[-4]) / (oi_vals[-4] + 1e-9)
            oi_increasing = oi_chg3h > 0.05   # OI 3h 涨幅 > 5%，持仓过热
    except:
        pass

    # ── 3. 多空比：判断持仓方向是否失衡 ──
    ls_ratio = 1.0
    try:
        ls_data = _req.get(
            f'https://fapi.binance.com/futures/data/globalLongShortAccountRatio?symbol={symbol}&period=1h&limit=3',
            timeout=5).json()
        if isinstance(ls_data, list) and ls_data:
            ls_ratio = float(ls_data[-1].get('longShortRatio', 1.0))
    except:
        pass
    # ls_ratio > 1.5 → 多头过多 → 下方清算密集
    # ls_ratio < 0.7 → 空头过多 → 上方清算密集
    long_crowded  = ls_ratio > 1.5
    short_crowded = ls_ratio < 0.7

    # ── 4. 资金费率方向：正费率=多头付费=多头过热，负费率=空头过热 ──
    try:
        fr_data = _req.get(f'https://fapi.binance.com/fapi/v1/premiumIndex?symbol={symbol}', timeout=5).json()
        fr_val  = float(fr_data.get('lastFundingRate', 0))
    except:
        fr_val = 0.0
    fr_long_heat  = fr_val > 0.0003   # 多头过热
    fr_short_heat = fr_val < -0.0003  # 空头过热

    # ── 5. K线关键位（宽窗口，更稳定）──
    k1h_lows  = [float(k[3]) for k in k1h]
    k1h_highs = [float(k[2]) for k in k1h]
    key_support    = min(k1h_lows[-20:])    # 近20h低点 = 多头清算密集基准
    key_resistance = max(k1h_highs[-12:])   # 近12h高点 = 空头清算密集基准
    h4_lows  = [float(k[3]) for k in k4h[-10:]]
    h4_highs = [float(k[2]) for k in k4h[-10:]]

    # ── 6. 综合推算清算密集位 ──
    # 策略：用 100 档订单簿找最大挂单墙，与 K线宽窗口关键位取极值
    # 加权：OI 过热/多空比失衡时，密集区往里缩 ATR×0.3
    try:
        bids_sorted = sorted([(float(p),float(q)) for p,q in ob.get('bids',[])], key=lambda x:-x[1])
        asks_sorted = sorted([(float(p),float(q)) for p,q in ob.get('asks',[])], key=lambda x:-x[1])
        biggest_bid = bids_sorted[0][0] if bids_sorted else ob_support
        biggest_ask = asks_sorted[0][0] if asks_sorted else ob_resistance
    except:
        biggest_bid, biggest_ask = ob_support, ob_resistance

    liq_below = min(key_support, biggest_bid)    # 多头清算区 = 近20h低点 vs 最大买单墙 取低
    liq_above = max(key_resistance, biggest_ask) # 空头清算区 = 近12h高点 vs 最大卖单墙 取高
    if oi_increasing and long_crowded:   liq_below += atr_14 * 0.3
    if oi_increasing and short_crowded:  liq_above -= atr_14 * 0.3

    # ── 7. 确定入场/止损/止盈 ──
    entry_reason = ""

    if direction == 'SHORT':
        # 做空：紧贴最大卖单墙下方入场，不追顶
        safe_entry = biggest_ask - atr_14 * 0.05
        if fr_long_heat or long_crowded:
            entry_reason = f"多头过热(FR={fr_val*100:.3f}%,L/S={ls_ratio:.2f})，卖单墙{biggest_ask:.5f}入场"
        else:
            entry_reason = f"常规空单，卖单墙{biggest_ask:.5f}下方入场"

        hard_sl  = min(liq_above + atr_14 * 0.3, price * 1.06)  # 止损上限6%
        soft_tp1 = liq_below + atr_14 * 0.3    # TP1 多头清算区前沿（留缓冲不刮底）
        soft_tp2 = liq_below - atr_14 * 0.5    # TP2 穿透清算区延伸

    else:  # LONG
        # 做多：紧贴最大买单墙上方入场，不抄底过早
        safe_entry = biggest_bid + atr_14 * 0.05
        if fr_short_heat or short_crowded:
            entry_reason = f"空头过热(FR={fr_val*100:.3f}%,L/S={ls_ratio:.2f})，买单墙{biggest_bid:.5f}入场"
        else:
            entry_reason = f"常规多单，买单墙{biggest_bid:.5f}上方入场"

        hard_sl  = max(liq_below - atr_14 * 0.3, price * 0.94)  # 止损下限6%
        soft_tp1 = liq_above - atr_14 * 0.3    # TP1 空头清算区前沿
        soft_tp2 = liq_above + atr_14 * 0.5    # TP2 穿透清算区延伸

    # ── 8. 安全检查：止损不能比入场价更差（兜底用 ATR×1.5）──
    if direction == 'SHORT' and hard_sl <= safe_entry:
        hard_sl = safe_entry + atr_14 * 1.5
    if direction == 'LONG'  and hard_sl >= safe_entry:
        hard_sl = safe_entry - atr_14 * 1.5

    return {
        'liq_below':    round(liq_below,  8),
        'liq_above':    round(liq_above,  8),
        'safe_entry':   round(safe_entry, 8),
        'hard_sl':      round(hard_sl,    8),
        'soft_tp1':     round(soft_tp1,   8),
        'soft_tp2':     round(soft_tp2,   8),
        'entry_reason': entry_reason,
        'ob_support':   round(ob_support,    8),
        'ob_resistance':round(ob_resistance, 8),
        'fr_val':       fr_val,
        'ls_ratio':     ls_ratio,
        'oi_increasing':oi_increasing,
    }

# 调用 liquidation_map（k4h 在评分阶段已拉好，直接复用）
lmap = liquidation_map(symbol, k1h, k4h, direction)

# ──────────────────────────────────────────────────────────────────
# 一、入场方式判断（必须先执行，禁止直接用固定比例追涨）
# ──────────────────────────────────────────────────────────────────

# 基础数据（k1h 在评分阶段已拉好，直接复用）
price      = float(k1h[-1][4])
atr        = sum(abs(float(k[2])-float(k[3])) for k in k1h[-14:]) / 14
highs_20   = [float(k[2]) for k in k1h[-20:]]
lows_20    = [float(k[3]) for k in k1h[-20:]]
near_high  = max(highs_20)
rsi_1h     = rsi14(k1h)
vols       = [float(k[5]) for k in k1h]
vavg       = sum(vols[-10:-3])/7 if len(vols)>=10 else 1
vol_ratio  = vols[-1]/vavg if vavg else 1
ticker     = api.get_ticker_24h(symbol)
open_price = float(ticker.get('openPrice', price))
open_chg   = (price - open_price) / open_price * 100   # 距开盘涨幅%
dist_high  = (near_high - price) / near_high * 100     # 距近期高点距离%

# 判断是否"刚突破"（近20根高点在最近2根K线内首次被突破）
just_breakout = False
if len(highs_20) >= 3:
    prev_high = max(highs_20[:-2])
    c1 = float(k1h[-1][4]); c2 = float(k1h[-2][4])
    just_breakout = (c1 > prev_high or c2 > prev_high) and near_high > prev_high

# 量能是否持续萎缩
vol_shrinking = len(vols) >= 5 and all(vols[-5+i] <= vols[-5+i-1] for i in range(1,5))
# 是否横盘（MA7与MA25差距<0.3%）
ma7_1h  = sum(float(k[4]) for k in k1h[-7:])  / 7
ma25_1h = sum(float(k[4]) for k in k1h[-25:]) / 25
sideways = abs(ma7_1h - ma25_1h) / ma25_1h * 100 < 0.3 if ma25_1h else False

# ── 等待观望（量能萎缩+横盘，跳过本次） ──
if vol_shrinking and sideways:
    pass  # 不挂单，留在观察区

# ── 追涨条件（必须同时满足全部） ──
chase_ok = (
    just_breakout      and   # 刚突破，近2根K线内
    vol_ratio >= 1.5   and   # 明显放量
    rsi_1h    <  65    and   # RSI未过热
    dist_high >  1.0         # 距近期高点>1%，未在顶部
)

# ── 回调条件（满足任一即回调） ──
pullback_ok = (
    dist_high  <  0.5                        or   # 接近近期高点顶部
    rsi_1h     >  65                         or   # RSI偏高
    (open_chg  >  2.0 and vol_ratio < 1.5)        # 已涨>2%但量能不足
)

# ── 强势整理条件：趋势好+量能好+RSI健康，但不满足追涨的「刚突破」要求，也没有回调警告 ──
# 典型形态：高点附近1~3%区间内横盘整理，等小幅回调再进
consolidate_ok = (
    not chase_ok and not pullback_ok   and   # 追涨/回调条件均不命中
    t1 == ('UP' if direction=='LONG' else 'DN')  and   # 1H趋势方向对
    vol_ratio >= vol_threshold         and   # 量能达标
    (40 <= rsi_1h <= 68)               and   # RSI在健康区
    0.5 <= dist_high <= 3.0                  # 距高点0.5~3%，整理区间内
)

# ── 无法判断方向：量能萎缩+横盘，或三个条件都不满足 ──
if not chase_ok and not pullback_ok and not consolidate_ok:
    pass  # 不挂单，留在观察区

# ── 确定入场方式和有效期（入场价锚定清算密集位，不追顶/不抄底）──
if chase_ok and not pullback_ok:
    order_mode   = 'CHASE'
    # 追涨时也必须在订单簿阻力/支撑墙附近，不追顶
    if direction == 'LONG':
        limit_offset = min(lmap['safe_entry'], price * 1.001)
    else:
        limit_offset = max(lmap['safe_entry'], price * 0.999)
    wait_seconds = 3600
elif consolidate_ok:
    order_mode   = 'CONSOLIDATE'
    # 强势整理：小幅回踩到支撑墙附近
    if direction == 'LONG':
        limit_offset = lmap['safe_entry'] - atr * 0.1
    else:
        limit_offset = lmap['safe_entry'] + atr * 0.1
    wait_seconds = 7200
else:
    order_mode   = 'PULLBACK'
    # 回调单：直接用清算密集位旁的安全入场价
    limit_offset = lmap['safe_entry']
    wait_seconds = 14400

# ──────────────────────────────────────────────────────────────────
# 二、常规开仓流程（止损/止盈全部用 lmap 清算位替代固定 ATR）
# ──────────────────────────────────────────────────────────────────

api.request("POST", "https://fapi.binance.com/fapi/v1/leverage", {"symbol": symbol, "leverage": 50})

notional_usdt = 350 if score >= 90 else 300
side          = 'BUY'  if direction == 'LONG' else 'SELL'
pos_side      = 'LONG' if direction == 'LONG' else 'SHORT'
close_side    = 'SELL' if direction == 'LONG' else 'BUY'

def round_tick(p, tick_str):
    tick = float(tick_str)
    dec  = len(tick_str.rstrip('0').split('.')[-1]) if '.' in tick_str else 0
    return round(round(p / tick) * tick, dec)

# 止损/止盈全部由 liquidation_map 推算，不再用固定 ATR 倍数
sl_price  = round_tick(lmap['hard_sl'],  tick_size)   # 清算密集区外侧止损
tp_price  = round_tick(lmap['soft_tp1'], tick_size)   # TP1：下一个清算密集区前沿
tp2_price = round_tick(lmap['soft_tp2'], tick_size)   # TP2：延伸目标备用

limit_price = round_tick(limit_offset, tick_size)
qty         = round(notional_usdt / price, qty_precision)

# ── 并发防重复：下单前检查该 symbol 是否已有未成交的开仓挂单 ──
existing_orders = api.get_signed_request('GET', '/fapi/v1/openOrders', {'symbol': symbol})
existing_open = [o for o in (existing_orders if isinstance(existing_orders, list) else [])
                 if o.get('side') == side and o.get('positionSide') == pos_side
                 and o.get('type') == 'LIMIT' and o.get('status') == 'NEW']
if existing_open:
    # 已有同方向挂单，跳过，避免重复开仓
    pass  # 不执行以下代码
else:

r = api.place_order(symbol, side, 'LIMIT',
    quantity=str(qty), price=str(limit_price),
    timeInForce='GTC', positionSide=pos_side)

order_id = r.get('orderId')
if not order_id:
    pass  # 下单失败，跳过
else:
    # 轮询等待成交（按有效期，每5s查一次）
    max_checks = wait_seconds // 5
    filled     = False
    for _ in range(max_checks):
        time.sleep(5)
        o = api.get_signed_request('GET', '/fapi/v1/order',
                                   {'symbol': symbol, 'orderId': order_id})
        if o.get('status') == 'FILLED':
            entry_price = float(o.get('avgPrice', price))
            filled      = True
            break

    if filled:
        # 立即补挂止损 + TP1（用清算密集位推算的位置）
        r_sl = api.place_algo_order(symbol, close_side, 'STOP_MARKET',        sl_price,  pos_side)
        r_tp = api.place_algo_order(symbol, close_side, 'TAKE_PROFIT_MARKET', tp_price,  pos_side)

        tp_pnl = round(abs(tp_price - entry_price) / entry_price * notional_usdt, 1)
        sl_pnl = round(abs(entry_price - sl_price) / entry_price * notional_usdt, 1)
        rr     = round(tp_pnl / sl_pnl, 1) if sl_pnl else 0

        tp_targets[symbol] = {
            'tp': tp_price, 'tp2': tp2_price, 'sl': sl_price,
            'entry': entry_price,
            'tp_pnl': tp_pnl, 'sl_pnl': sl_pnl, 'rr': rr,
            'side': pos_side, 'notional': notional_usdt,
            'order_mode': order_mode,
            'liq_map': {
                'liq_below': lmap['liq_below'],
                'liq_above': lmap['liq_above'],
                'ob_support': lmap['ob_support'],
                'ob_resistance': lmap['ob_resistance'],
                'fr': lmap['fr_val'],
                'ls_ratio': lmap['ls_ratio'],
                'entry_reason': lmap['entry_reason'],
            }
        }
        watchlist['candidates'] = [c for c in watchlist['candidates'] if c['symbol'] != symbol]
        NOTIFY = True

    else:
        # 超时未成交 → 撤单，留在观察区等下次评估
        api.cancel_order(symbol, order_id)
```


---

## 开仓通知格式

```
✅ **观察区开仓**｜{SYM} {多头/空头}

🕒 {HH:MM} 北京

**开仓详情**
仓位：`{notional}U 名义`　入场：`@{entry}`
评分：`{score}%`　量比：`{vol}x`　模式：`{order_mode}`

**清算位分析**
📊 多头清算区：`{liq_below}`　空头清算区：`{liq_above}`
📖 订单簿支撑：`{ob_support}`　订单簿阻力：`{ob_resistance}`
💸 资金费率：`{fr}%`　多空比：`{ls_ratio}`
💡 入场理由：{entry_reason}

**风控点位**
🟢 TP1：`{tp}`　预估 `+{tp_pnl}U`（清算密集区前沿）
🟢 TP2：`{tp2}`　延伸目标（备用）
🔴 止损：`{sl}`　预估 `-{sl_pnl}U`（清算密集区外侧）
盈亏比：`1 : {rr}`
```

---

✅ **观察区开仓**｜{SYM} {多头/空头}

🕒 {HH:MM} 北京

**开仓详情**

仓位：`{notional}U 名义`　入场：`@{entry}`
评分：`{score}%`　量比：`{vol}x`

**风控点位**

🟢 止盈：`${tp}`　预估 `+{tp_pnl}U`
🔴 止损：`${sl}`　预估 `-{sl_pnl}U`
盈亏比：`1 : {rr}`
```

---

## 注意事项

1. 止损止盈必须用 `api.place_algo_order`，不能用 `api.place_order`
2. 资金费率 fr > 0.5% → 跳过开仓
3. 可用余额 < 20U → 不开仓
4. 限价单 60s 未成交 → 撤单，标的留在观察区等下次
5. 所有操作静默，只有开仓成功才推通知
6. 不推送任何「扫描汇总」「评分更新」等过程信息
