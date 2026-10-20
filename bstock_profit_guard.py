#!/usr/bin/env python3
"""
bstock_profit_guard.py
======================
总盈利监控：当7笔持仓浮盈合计 >= TARGET_PROFIT_U 时，全部市价平仓。
每30秒检查一次，平仓成功后通知并退出。

用法：python3 bstock_profit_guard.py [目标盈利U，默认50]
"""

import sys, json, time, subprocess, requests, logging
from pathlib import Path

TARGET_PROFIT_U = float(sys.argv[1]) if len(sys.argv) > 1 else 20.0

PROXY   = "socks5://REDACTED_PROXY_CREDENTIALS"
PROXIES = {"https": PROXY, "http": PROXY}
STATE_FILE    = Path('/tmp/bstock_state.json')
LOG_FILE      = Path('/tmp/profit_guard.log')
JARVIS_TARGET = "432401404:thread:019fcac8-4a93-7bea-a930-7f0e59e4897c"

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s UTC] %(message)s",
    datefmt="%Y-%m-%d %H:%M",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("profit_guard")

def notify(msg):
    try:
        subprocess.run(["openclaw", "message", "send",
            "--channel", "jarvis", "--to", JARVIS_TARGET, "--message", msg],
            capture_output=True, timeout=10)
    except Exception as e:
        log.warning(f"通知失败: {e}")

def get_prices(syms):
    try:
        r = requests.get("https://fapi.binance.com/fapi/v1/ticker/price",
            proxies=PROXIES, timeout=10).json()
        return {x['symbol']: float(x['price']) for x in r}
    except Exception as e:
        log.warning(f"价格获取失败: {e}")
        return {}

def close_all(positions, prices):
    """调用 baw CLI 逐笔市价卖出全部持仓"""
    results = []
    for sym, pos in list(positions.items()):
        addr  = pos.get('addr', '')
        qty   = pos.get('qty') or pos.get('token_qty', 0)
        fut   = pos.get('fut_sym', pos.get('ticker','') + 'USDT')
        cur   = prices.get(fut, 0)
        entry = pos.get('entry_price') or pos.get('entry', 0)
        cost  = pos.get('usdt_cost', 0)
        pnl   = cost * (cur - entry) / entry if entry > 0 else 0

        log.info(f"[平仓] {sym} qty={qty:.6f} addr={addr[:12]}... cur={cur:.4g} pnl={pnl:+.2f}U")
        try:
            # 用 baw 市价卖出（同 bstock_scanner.py 里的 baw_sell 逻辑）
            cmd = [
                "baw", "swap",
                "--fromToken",   addr,          # bStock 代币地址
                "--toToken",     "0x55d398326f99059fF775485246999027B3197955",  # USDT BSC
                "--fromTokenQty", str(qty),
                "--chainId",     "56",
                "--slippage",    "2",
                "--json",
            ]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            out = {}
            try:
                out = json.loads(r.stdout)
            except Exception:
                pass
            ok = out.get('success') or out.get('status') == 'success'
            results.append((sym, ok, pnl))
            if ok:
                log.info(f"[平仓] ✅ {sym} 卖出成功")
            else:
                log.warning(f"[平仓] ❌ {sym} 卖出失败: {r.stderr[:100]}")
        except Exception as e:
            log.error(f"[平仓] {sym} 异常: {e}")
            results.append((sym, False, pnl))
        time.sleep(2)
    return results

def main():
    log.info(f"{'='*50}")
    log.info(f"  总盈利守护 启动 | 目标: +{TARGET_PROFIT_U:.0f}U 时全部平仓")
    log.info(f"{'='*50}")
    notify(f"🛡️ 总盈利守护已启动\n目标：浮盈合计 ≥ +{TARGET_PROFIT_U:.0f}U 时自动全部平仓\n每30秒检查一次")

    while True:
        try:
            state = json.loads(STATE_FILE.read_text())
            positions = state.get('positions', {})

            if not positions:
                log.info("无持仓，守护退出")
                notify("✅ 已无持仓，总盈利守护退出")
                break

            prices = get_prices(list(positions.keys()))
            total_float = 0.0
            lines = []
            for sym, pos in positions.items():
                fut   = pos.get('fut_sym', pos.get('ticker','') + 'USDT')
                entry = pos.get('entry_price') or pos.get('entry', 0)
                cost  = pos.get('usdt_cost', 0)
                cur   = prices.get(fut, 0)
                if cur > 0 and entry > 0:
                    pct = (cur - entry) / entry * 100
                    pnl = cost * pct / 100
                    total_float += pnl
                    lines.append(f"  {sym:<8} {pct:+.2f}% {pnl:+.2f}U")

            log.info(f"当前浮盈: {total_float:+.2f}U / 目标 {TARGET_PROFIT_U:.0f}U  "
                     f"({', '.join(p.strip() for p in lines)})")

            if total_float >= TARGET_PROFIT_U:
                log.info(f"🎯 目标达成！浮盈 {total_float:+.2f}U ≥ {TARGET_PROFIT_U:.0f}U，开始全部平仓")
                notify(f"🎯 目标达成！浮盈 {total_float:+.2f}U ≥ +{TARGET_PROFIT_U:.0f}U\n开始全部市价平仓...")
                results = close_all(positions, prices)

                # 更新 state
                state2 = json.loads(STATE_FILE.read_text())
                total_pnl = state2.get('total_pnl', 0)
                summary_lines = []
                for sym, ok, pnl in results:
                    if ok:
                        total_pnl += pnl
                        state2['positions'].pop(sym, None)
                        summary_lines.append(f"  ✅ {sym} {pnl:+.2f}U")
                    else:
                        summary_lines.append(f"  ❌ {sym} 平仓失败")
                state2['total_pnl'] = total_pnl
                STATE_FILE.write_text(json.dumps(state2, ensure_ascii=False, indent=2))

                ok_cnt = sum(1 for _, ok, _ in results if ok)
                total_realized = sum(pnl for _, ok, pnl in results if ok)
                msg = (f"✅ 总盈利平仓完成\n"
                       f"成功 {ok_cnt}/{len(results)} 笔\n"
                       f"本次锁定盈利: +{total_realized:.2f}U\n"
                       f"累计总PnL: {total_pnl:+.2f}U\n"
                       + '\n'.join(summary_lines))
                log.info(msg)
                notify(msg)
                break

        except Exception as e:
            log.warning(f"检查异常: {e}")

        time.sleep(30)

if __name__ == "__main__":
    main()
