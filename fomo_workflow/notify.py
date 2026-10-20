# ============================================================
# notify.py — Jarvis + 钉钉 双渠道推送通知 + 买入失败记录
# ============================================================
import json, os, time, hashlib, hmac, base64, urllib.parse, requests

from config import JARVIS_TARGET, GAS_HIGH_THRESHOLD_USD, MULTI_BUY_WINDOW_MIN

# ── 钉钉群机器人 webhook（加签模式）──────────────────────────
_DINGTALK_ACCESS_TOKEN = os.environ.get("FOMO_WORKFLOW_NOTIFY__DINGTALK_ACCESS_TOKEN", "")
_DINGTALK_SECRET = os.environ.get("FOMO_WORKFLOW_NOTIFY__DINGTALK_SECRET", "")

_BUY_FAILURE_FILE = os.path.join(os.path.dirname(__file__), "buy_failures.json")

# 延迟导入 _log / _record_high_gas_event，由 ws_monitor 注入
# （避免循环依赖：ws_monitor → notify → ws_monitor）
_log_fn = None
_gas_event_fn = None

def init_notify(log_fn, gas_event_fn):
    global _log_fn, _gas_event_fn
    _log_fn = log_fn
    _gas_event_fn = gas_event_fn

def _log(msg):
    if _log_fn:
        _log_fn(msg)

def _record_high_gas_event(gas_usd, symbol, tx):
    if _gas_event_fn:
        _gas_event_fn(gas_usd, symbol, tx)


def _notify_dingtalk(msg: str):
    """推送到钉钉群机器人 webhook（text 模式，MC<1M 用 ⚡ 高亮）"""
    try:
        # 钉钉 text 消息最长 4096 字节，超长截断
        content = msg if len(msg) <= 4000 else msg[:4000] + "..."
        
        # MC < 1M 时在行首加 ⚡ 高亮（text 模式不支持加粗，用 emoji 替代）
        import re
        lines = content.split("\n")
        formatted_lines = []
        for line in lines:
            mc_match = re.search(r'\$(\d+(?:\.\d+)?)K', line)
            mc_m_match = re.search(r'\$(\d+(?:\.\d+)?)M', line)
            is_mc_line = ('MC' in line or '市值' in line or '📊' in line)
            
            if is_mc_line and mc_match and not mc_m_match:
                # MC < 1M，加 ⚡ 前缀高亮
                formatted_lines.append(f"⚡ {line}")
            else:
                formatted_lines.append(line)
        
        text_content = "\n".join(formatted_lines)
        
        timestamp = str(round(time.time() * 1000))
        string_to_sign = f"{timestamp}\n{_DINGTALK_SECRET}"
        hmac_code = hmac.new(
            _DINGTALK_SECRET.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
        url = (
            f"https://oapi.dingtalk.com/robot/send"
            f"?access_token={_DINGTALK_ACCESS_TOKEN}"
            f"&timestamp={timestamp}&sign={sign}"
        )
        resp = requests.post(
            url,
            json={
                "msgtype": "text",
                "text": {
                    "content": text_content,
                },
            },
            timeout=5,
        )
        if resp.status_code != 200 or resp.json().get("errcode") != 0:
            _log(f"[DINGTALK ERROR] {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        _log(f"[DINGTALK ERROR] {e}")


def _notify(msg: str):
    """双渠道推送：Jarvis + 钉钉"""
    # 1) Jarvis 渠道
    try:
        import subprocess
        subprocess.Popen(
            ["openclaw", "message", "send",
             "--channel", "jarvis",
             "-t", JARVIS_TARGET,
             "-m", msg],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        _log(f"[NOTIFY ERROR] {e}")
    # 2) 钉钉渠道
    _notify_dingtalk(msg)


def _record_buy_failure(token_addr: str, token_symbol: str,
                        size_usd: float, strategy: str,
                        entry_mc: float = 0, trigger_kol: str = "",
                        fail_reason: str = ""):
    record = {
        "ts":          int(time.time()),
        "time":        time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
        "symbol":      token_symbol,
        "token_addr":  token_addr,
        "strategy":    strategy,
        "size_usd":    size_usd,
        "entry_mc":    entry_mc,
        "trigger_kol": trigger_kol,
        "fail_reason": fail_reason,
        "mc_tier":     ("micro" if entry_mc < 100_000
                        else "small" if entry_mc < 1_000_000
                        else "mid"),
    }
    try:
        try:
            with open(_BUY_FAILURE_FILE) as f:
                data = json.load(f)
        except Exception:
            data = {"failures": []}
        data["failures"].append(record)
        data["failures"] = data["failures"][-500:]
        with open(_BUY_FAILURE_FILE, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        _log(f"[BUY FAILURE LOG] 写入失败: {e}")
    _log(f"[BUY FAILURE] {token_symbol} mc_tier={record['mc_tier']} "
         f"entry_mc=${entry_mc:,.0f} reason={fail_reason}")


def _notify_buy(symbol: str, token_addr: str, strategy: str,
                size_usd: float, entry_mc: float, trigger_kol: str = "",
                multi_count: int = 0):
    """买入静默：仅记录日志"""
    mc_str = f"${entry_mc/1000:.0f}K" if entry_mc < 1_000_000 else f"${entry_mc/1_000_000:.1f}M"
    if strategy == "kol":
        reason = f"KOL 跟单 @{trigger_kol} 买入"
    elif strategy == "multi":
        reason = f"{multi_count} 人 {MULTI_BUY_WINDOW_MIN} 分钟内共同买入"
    else:
        reason = strategy
    _log(f"[BUY SILENT] ${symbol} {size_usd}U @{mc_str} 策略={strategy} {reason}")


def _notify_signal(
    symbol: str,
    token_addr: str,
    strategy: str,
    trigger_desc: str,       # 触发原因：如 "KOL @xxx $50k 买入" / "鲸鱼 B级 $80k"
    entry_mc: float,
    size_usd: float,         # 原本打算买多少（仅供参考）
    # F3: 模拟卖出损耗
    sell_slippage: float | None = None,   # 0.05 = 5%
    # F5: 安全审计
    honeypot: bool | None = None,         # True=蜜罐
    buy_tax: float | None = None,
    sell_tax: float | None = None,
    # F7: 流动性
    liquidity_usd: float | None = None,
    # F11: Deployer
    deployer_addr: str = "",
    deployer_token_count: int = 0,
    # 当前价格
    price_usd: float | None = None,
    # 额外备注
    extra: str = "",
):
    """SIGNAL_ONLY 模式下推送详细信号卡片，代替实际买入。"""
    mc_str = f"${entry_mc/1000:.0f}K" if entry_mc < 1_000_000 else f"${entry_mc/1_000_000:.2f}M"
    price_str = f"${price_usd:.8g}" if price_usd else "—"

    # 策略标签
    strat_map = {"A": "策略A KOL跟单", "B": "策略B 多人共买", "W": "策略W 鲸鱼",
                 "kol": "策略A KOL跟单", "multi": "策略B 多人共买", "whale": "策略W 鲸鱼"}
    strat_label = strat_map.get(strategy, strategy)

    # 蜜罐/安全检测行
    if honeypot is True:
        safety = "⛔ 蜜罐检测: 危险，无法卖出"
    elif honeypot is False:
        tax_str = ""
        if buy_tax is not None:
            tax_str = f" 买税{buy_tax:.1%}/卖税{sell_tax:.1%}" if sell_tax is not None else f" 买税{buy_tax:.1%}"
        slip_str = f" 模拟滑点{sell_slippage:.1%}" if sell_slippage is not None else ""
        safety = f"✅ 安全检测通过{tax_str}{slip_str}"
    else:
        safety = "⚠️ 安全检测未执行"

    # 流动性行
    if liquidity_usd is not None:
        liq_str = f"${liquidity_usd:,.0f}"
        liq_ok = "✅" if liquidity_usd >= 30_000 else "⚠️ 流动性偏低"
        liq_line = f"{liq_ok} 流动性: {liq_str}"
    else:
        liq_line = "⚠️ 流动性未查询"

    # Deployer 行
    if deployer_addr:
        dep_short = deployer_addr[:8] + "…" + deployer_addr[-4:]
        dep_warn = f" ⚠️ 历史发币{deployer_token_count}次" if deployer_token_count > 5 else ""
        dep_line = f"🔨 Deployer: {dep_short}{dep_warn}"
    else:
        dep_line = "🔨 Deployer: 未获取"

    extra_line = f"📝 {extra}" if extra else ""

    import time as _time
    snapshot_time = _time.strftime("%H:%M:%S", _time.gmtime(_time.time() + 8*3600)) + " CST"

    lines = [
        f"📡 【信号提醒】${symbol}",
        f"──────────────────",
        f" GMGN: https://gmgn.ai/bsc/token/{token_addr}",
        f"🎯 策略: {strat_label}",
        f"📣 触发: {trigger_desc}",
        f"──────────────────",
        f"📊 买入市值: {mc_str} | 当前价: {price_str}",
        liq_line,
        safety,
        dep_line,
        f"──────────────────",
        f"💡 参考仓位: ${size_usd:.0f}  （SIGNAL_ONLY 模式，未自动买入）",
        f"⏰ 快照时间: {snapshot_time}",
    ]
    if extra_line:
        lines.append(extra_line)

    _notify("\n".join(l for l in lines if l is not None))
    _log(f"[SIGNAL NOTIFY] ${symbol} {strat_label} @{mc_str} addr={token_addr}")


def _notify_close(token_addr: str, symbol: str,
                  exit_reason: str,
                  eth_received: float, gas_eth: float,
                  pnl_usd: float | None, tx: str,
                  pos_snapshot: dict | None = None):
    try:
        from executor import _get_eth_price
        eth_price = _get_eth_price()
    except Exception:
        eth_price = 0.0

    eth_usd     = eth_received * eth_price if eth_price > 0 else 0.0
    gas_usd     = gas_eth * eth_price if eth_price > 0 else 0.0
    pnl_str     = f"${pnl_usd:+,.2f}" if pnl_usd is not None else "N/A"
    pnl_val     = pnl_usd or 0.0
    emoji       = "🟢" if pnl_val >= 0 else "🔴"

    pos          = pos_snapshot or {}
    opened_at    = pos.get("opened_at", 0)
    hold_min     = int((time.time() - opened_at) / 60) if opened_at else 0
    hold_str     = f"{hold_min//60}h{hold_min%60}m" if hold_min >= 60 else f"{hold_min}m"
    open_strat   = pos.get("open_strategy", "?")
    trigger_kol  = pos.get("trigger_kol", "")
    entry_mc     = pos.get("entry_mc", 0)
    dca_count    = pos.get("dca_count", 0)
    signal_score = pos.get("signal_score", 0)
    cost_usd     = pos.get("size_usd", 0)
    entry_price  = pos.get("entry_price", 0)
    strategies   = pos.get("strategies", [])

    dca_avg_str = f" | DCA均价 ${entry_price:.8f}" if dca_count and entry_price else ""
    mc_str = f"${entry_mc/1000:.0f}K" if entry_mc < 1_000_000 else f"${entry_mc/1_000_000:.1f}M"

    if open_strat == "A":
        entry_desc = f"策略A — KOL @{trigger_kol} 跟单"
    elif open_strat == "W":
        entry_desc = "策略W — 鲸鱼大额买入"
    else:
        entry_desc = "策略B — 多人共买"
    if dca_count:
        entry_desc += f" +{dca_count}次DCA补仓"
    if len(strategies) > 1:
        entry_desc += f" [叠仓:{'+'.join(strategies)}]"

    gas_pct  = gas_usd / cost_usd * 100 if cost_usd > 0 else 0
    gas_flag = (f" ⚠️ 高gas（占成本 {gas_pct:.1f}%）" if gas_usd >= GAS_HIGH_THRESHOLD_USD
                else f" （占成本 {gas_pct:.1f}%）" if gas_pct > 3 else "")

    pnl_pct_str = ""
    if cost_usd > 0 and pnl_usd is not None:
        pnl_pct_str = f" ({pnl_usd / cost_usd * 100:+.1f}%)"

    tx_short = tx[:20] + "…" if len(tx) > 20 else tx

    lines = [
        f"{emoji} ${symbol} 交易完结", "",
        f"📥 入场: {entry_desc}",
        f"📊 买入市值: {mc_str} | 成本: ${cost_usd:.1f}{dca_avg_str}",
        f"📤 离场: {exit_reason}",
        f"⏱  持仓: {hold_str} | 信号分: {signal_score}", "",
        f"💰 收到: {eth_received:.5f} ETH ≈ ${eth_usd:.2f}",
        f"📈 盈亏: {pnl_str}{pnl_pct_str}",
        f"⛽ Gas: ≈${gas_usd:.2f}{gas_flag}",
        f"🔗 Tx: {tx_short}",
    ]
    _notify("\n".join(lines))

    if gas_usd >= GAS_HIGH_THRESHOLD_USD:
        _record_high_gas_event(gas_usd, symbol, tx)


def _notify_partial_sell(symbol: str, sell_pct: float, reason: str,
                         eth_received: float = 0.0,
                         pnl_usd: float | None = None, tx: str = ""):
    try:
        from executor import _get_eth_price
        eth_price = _get_eth_price()
    except Exception:
        eth_price = 0.0
    eth_usd = eth_received * eth_price if eth_price > 0 else 0.0
    pnl_str = f"${pnl_usd:+,.2f}" if pnl_usd is not None else "—"
    tx_s    = tx[:16] + "…" if len(tx) > 16 else tx
    _notify(
        f"🟡 ${symbol} 减仓 {sell_pct:.0%}\n"
        f"触发: {reason}\n"
        f"收到: {eth_received:.5f} ETH ≈ ${eth_usd:.2f} | 盈亏: {pnl_str}\n"
        f"🔗 {tx_s}"
    )


def _notify_sell(symbol: str, action: str, sell_pct: float, reason: str,
                 eth_received: float = 0.0, gas_eth: float = 0.0,
                 pnl_usd: float | None = None, tx: str = "",
                 triggered_reason: str = "",
                 token_addr: str = "",
                 pos_snapshot: dict | None = None):
    if action == "full_sell":
        _notify_close(
            token_addr=token_addr, symbol=symbol,
            exit_reason=triggered_reason or reason,
            eth_received=eth_received, gas_eth=gas_eth,
            pnl_usd=pnl_usd, tx=tx,
            pos_snapshot=pos_snapshot,
        )
    else:
        _notify_partial_sell(
            symbol=symbol, sell_pct=sell_pct,
            reason=triggered_reason or reason,
            eth_received=eth_received,
            pnl_usd=pnl_usd, tx=tx,
        )
