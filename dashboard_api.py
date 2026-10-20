#!/usr/bin/env python3
"""Dashboard API Server - 端口 8765"""
import json, subprocess, time, sys, threading, collections
from pathlib import Path
from datetime import datetime, timezone

try:
    from flask import Flask, jsonify, request as flask_request
    from flask_cors import CORS
except ImportError:
    subprocess.check_call([sys.executable,"-m","pip","install","flask","flask-cors","--break-system-packages","-q"])
    from flask import Flask, jsonify, request as flask_request
    from flask_cors import CORS

sys.path.insert(0, '/root/.openclaw/workspace')
import binance_api as api
import requests as req_lib

app  = Flask(__name__)
CORS(app)
WS   = Path("/root/.openclaw/workspace")

def rjson(p):
    try: return json.loads(Path(p).read_text())
    except: return {}

def rlist(p):
    try:
        d = json.loads(Path(p).read_text())
        return d if isinstance(d, list) else []
    except: return []

# ── 合约 ──────────────────────────────────────────────────────────
@app.route("/api/futures/overview")
def f_overview():
    try:
        # 用 v2/account 获取真实保证金余额（和 APP 一致）
        acct = api.request("GET", f"{api.BASE_FAPI}/fapi/v2/account")
        margin_bal = float(acct.get("totalMarginBalance", 0))
        avail_bal  = float(acct.get("availableBalance", 0))
        pnl        = float(acct.get("totalUnrealizedProfit", 0))
        pos  = [p for p in api.get_positions() if float(p.get("positionAmt",0))!=0]
        ords = api.get_open_orders()
        return jsonify({"wallet":round(margin_bal,2),"avail":round(avail_bal,2),
                        "pnl":round(pnl,4),"pos_cnt":len(pos),"ord_cnt":len(ords)})
    except Exception as e: return jsonify({"error":str(e)}),500

@app.route("/api/futures/positions")
def f_positions():
    try:
        out=[]
        for p in api.get_positions():
            amt=float(p.get("positionAmt",0))
            if amt==0: continue
            entry=float(p.get("entryPrice",0)); pnl=float(p.get("unRealizedProfit",0))
            mark=float(p.get("markPrice",0))
            # 名义价値用 markPrice 计算（和 APP 一致）
            notional = round(abs(amt)*mark, 2)
            # 盈产比 = 浮盈 / 初始保证金（开仓价开仓名义）
            init_margin = abs(amt)*entry
            pnl_pct = round(pnl/init_margin*100, 2) if init_margin else 0
            out.append({"symbol":p["symbol"],"side":"LONG" if amt>0 else "SHORT",
                        "amount":abs(amt),"entry":entry,"mark":mark,
                        "pnl":round(pnl,4),"pnl_pct":pnl_pct,
                        "notional":notional})
        return jsonify(out)
    except Exception as e: return jsonify({"error":str(e)}),500

@app.route("/api/futures/orders")
def f_orders():
    try:
        out=[]
        for o in api.get_open_orders():
            out.append({"id":o.get("orderId"),"symbol":o.get("symbol"),"side":o.get("side"),
                        "type":o.get("type"),"price":float(o.get("price",0)),
                        "stop":float(o.get("stopPrice",0)),"qty":float(o.get("origQty",0)),
                        "status":o.get("status"),
                        "time":datetime.utcfromtimestamp(o.get("time",0)/1000).strftime("%m-%d %H:%M")})
        return jsonify(out)
    except Exception as e: return jsonify({"error":str(e)}),500

@app.route("/api/futures/history")
def f_history():
    a=rlist(WS/"position-entries.json"); b=rlist(WS/"position-monitor-log.json")
    def _ts(x):
        v=x.get("ts",x.get("time",0))
        try: return int(v)
        except: return 0
    combined=sorted(a+b,key=_ts,reverse=True)
    return jsonify(combined[:150])

@app.route("/api/futures/scanner")
def f_scanner():
    try:
        r1=req_lib.get("https://fapi.binance.com/fapi/v1/ticker/24hr",proxies=api.PROXIES,timeout=10)
        tickers={t["symbol"]:t for t in r1.json() if t["symbol"].endswith("USDT")}
        r2=req_lib.get("https://fapi.binance.com/fapi/v1/premiumIndex",proxies=api.PROXIES,timeout=10)
        fr={f["symbol"]:float(f.get("lastFundingRate",0)) for f in r2.json()}
        out=[]
        for sym,t in tickers.items():
            chg=float(t.get("priceChangePercent",0)); vol=float(t.get("quoteVolume",0)); f=fr.get(sym,0)
            s=0; d="LONG"
            if chg>3: s+=15
            if chg>8: s+=10
            if vol>5e7: s+=10
            if vol>2e8: s+=10
            if f<-0.0005: s+=15
            if f>0.001: s-=10
            ss=0
            if chg<-3: ss+=15
            if chg<-8: ss+=10
            if f>0.001: ss+=15
            if vol>5e7: ss+=10
            if ss>s: s=ss; d="SHORT"
            if s>=25:
                out.append({"symbol":sym,"score":s,"direction":d,"chg24":round(chg,2),
                            "vol24m":round(vol/1e6,1),"fr":round(f*100,4),"price":float(t.get("lastPrice",0))})
        out.sort(key=lambda x:-x["score"])
        return jsonify(out[:30])
    except Exception as e: return jsonify({"error":str(e)}),500

# ── 跟单 ──────────────────────────────────────────────────────────
@app.route("/api/ct/overview")
def ct_overview():
    st=rjson(WS/"cw_state.json"); logs=rlist(WS/"cw_log.json"); curve=rlist(WS/"cw_equity_curve.json")
    pos=st.get("positions",{}); now_ms=int(time.time()*1000)
    today=[l for l in logs if l.get("ts",0)>(time.time()-86400)*1000]
    buys=[l for l in today if l.get("action")=="BUY_OK"]
    exits=[l for l in today if l.get("action")=="EXIT"]
    return jsonify({"open":len(pos),"buys_today":len(buys),"wins":len([l for l in exits if l.get("pnl_pct",0)>0]),
                    "losses":len([l for l in exits if l.get("pnl_pct",0)<=0]),"fused":st.get("fused",False),
                    "equity_days":len(curve),"latest_equity":curve[-1].get("end_usdt",0) if curve else 0})

@app.route("/api/ct/positions")
def ct_positions():
    st=rjson(WS/"cw_state.json"); pos=st.get("positions",{}); now_ms=int(time.time()*1000)
    out=[]
    for addr,p in pos.items():
        held=(now_ms-p.get("buy_ts",0))/3_600_000
        out.append({"token_addr":addr,"symbol":p.get("symbol","?"),"buy_price":p.get("buy_price",0),
                    "buy_usd":p.get("buy_usd",0),"sl":round(p.get("sl",0),8),"tp1":round(p.get("tp1",0),8),
                    "tp2":round(p.get("tp2",0),8),"tp1_done":p.get("tp1_done",False),
                    "trail_sl":round(p.get("trail_sl",0),8),"peak":round(p.get("peak",0),8),
                    "buyers":p.get("buyers",[]),"held_h":round(held,1),
                    "dex_url":f"https://dexscreener.com/bsc/{addr}"})
    return jsonify(out)

@app.route("/api/ct/history")
def ct_history():
    logs=rlist(WS/"cw_log.json"); logs.sort(key=lambda x:x.get("ts",0),reverse=True)
    return jsonify(logs[:200])

@app.route("/api/ct/equity")
def ct_equity():
    return jsonify(rlist(WS/"cw_equity_curve.json"))

@app.route("/api/ct/watchlist")
def ct_watchlist():
    try:
        r=subprocess.run(["python3","-c",
            "import sys;sys.path.insert(0,'/root/.openclaw/workspace');"
            "from smart_copy_trader_ws import WATCH_ADDRS,_tier;import json;"
            "print(json.dumps(sorted([{'addr':a,'label':i['label'],'score':i['score'],'tier':_tier(i['score'])}"
            " for a,i in WATCH_ADDRS.items()],key=lambda x:-x['score'])))"],
            capture_output=True,text=True,timeout=10)
        return jsonify(json.loads(r.stdout))
    except Exception as e: return jsonify({"error":str(e)}),500

# ── 钱包 ──────────────────────────────────────────────────────────
@app.route("/api/wallet/balance")
def w_balance():
    try:
        r=subprocess.run(["baw","wallet","balance","--json"],capture_output=True,text=True,timeout=15)
        d=json.loads(r.stdout); items=d if isinstance(d,list) else (d.get("data") or [])
        return jsonify(items)
    except Exception as e: return jsonify({"error":str(e)}),500

@app.route("/api/wallet/defi")
def w_defi():
    try:
        r=subprocess.run(["baw","defi","position","--json"],capture_output=True,text=True,timeout=15)
        d=json.loads(r.stdout); pos=d if isinstance(d,list) else (d.get("data") or {}).get("list",[])
        return jsonify(pos)
    except Exception as e: return jsonify({"error":str(e)}),500

@app.route("/api/wallet/txhistory")
def w_txhistory():
    try:
        r=subprocess.run(["baw","wallet","tx-history","--json","--size","50"],capture_output=True,text=True,timeout=15)
        d=json.loads(r.stdout); txs=d if isinstance(d,list) else (d.get("data") or {}).get("list",[])
        return jsonify(txs)
    except Exception as e: return jsonify({"error":str(e)}),500

# ── 任务 ──────────────────────────────────────────────────────────
@app.route("/api/tasks/cron")
def t_cron():
    try:
        # 直接读 jobs.json 更可靠，openclaw cron list 进程容易被 SIGKILL
        cron_path = Path("/root/.openclaw/cron/jobs.json")
        if not cron_path.exists():
            return jsonify([])
        raw = json.loads(cron_path.read_text())
        jobs = raw if isinstance(raw, list) else raw.get("jobs", list(raw.values()) if isinstance(raw,dict) else [])
        out = []
        for j in jobs:
            sched = j.get("schedule", {})
            kind  = sched.get("kind","?")
            if kind == "cron":   freq = sched.get("expr","?")
            elif kind == "every": freq = f"每{int(sched.get('everyMs',0)//60000)}分钟" if sched.get('everyMs',0)<3600000 else f"每{int(sched.get('everyMs',0)//3600000)}小时"
            elif kind == "at":    freq = f"定时 {sched.get('at','?')[:16]}"
            else: freq = "?"
            out.append({
                "id":      j.get("id","")[:8],
                "name":    j.get("name","?"),
                "enabled": j.get("enabled", True),
                "freq":    freq,
                "deleteAfterRun": j.get("deleteAfterRun", False),
            })
        return jsonify(out)
    except Exception as e: return jsonify({"error":str(e)}),500

@app.route("/api/tasks/processes")
def t_processes():
    out=[]
    log_map = {
        "ws_monitor.py":            "/tmp/ws_monitor.log",  # ws_monitor 写到 /tmp
        "smart_copy_trader_ws.py":  WS/"logs"/"smart_copy_ws.log",
        "dashboard_api.py":         WS/"logs"/"dashboard.log",
    }
    for name,script in [
        ("ws_monitor（合约交易）",    "ws_monitor.py"),
        ("smart_copy_trader（BSC跟单）","smart_copy_trader_ws.py"),
        ("dashboard_api（看板）",     "dashboard_api.py"),
    ]:
        r=subprocess.run(["pgrep","-f",script],capture_output=True,text=True)
        alive=r.returncode==0; pids=r.stdout.strip().split() if alive else []
        last=""
        logf = log_map.get(script)
        try:
            lines=open(logf).readlines()
            last="".join(lines[-3:]).strip()[-300:]
        except: pass
        out.append({"name":name,"running":alive,"pids":pids,"last_log":last})
    return jsonify(out)

@app.route("/api/tasks/daily_log")
def t_daily(): return jsonify(rjson(WS/"daily_task_log.json"))

@app.route("/api/tasks/manual")
def t_manual():
    """手动维护的任务状态（活动、一次性任务等）"""
    import datetime
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    now_bj  = now_utc.replace(tzinfo=None) + datetime.timedelta(hours=8)

    # Baby 每日任务（每天 UTC 07:00 = 北京 15:00 触发）
    baby_log = rjson(WS/"daily_task_log.json")
    baby_date = baby_log.get("date","")
    today_str = now_bj.strftime("%Y-%m-%d")
    baby_done = (baby_date == today_str)
    # 下次触发：今天北京15:00，如果已过则明天
    next_baby_bj = now_bj.replace(hour=15, minute=0, second=0, microsecond=0)
    if now_bj >= next_baby_bj:
        next_baby_bj += datetime.timedelta(days=1)
    next_baby_str = next_baby_bj.strftime("%m-%d %H:%M 北京时间")

    baby_subtasks = []
    if baby_log.get("babylon_trade"): baby_subtasks.append({"name":"交易任务","done":True,"detail":baby_log["babylon_trade"]})
    else: baby_subtasks.append({"name":"交易任务","done":False,"detail":""})
    post1 = baby_log.get("post_1",{})
    post2 = baby_log.get("post_2",{})
    if isinstance(post1,dict) and post1.get("ok"): baby_subtasks.append({"name":"发帖1","done":True,"detail":post1.get("url","")})
    else: baby_subtasks.append({"name":"发帖1","done":False,"detail":""})
    if isinstance(post2,dict) and post2.get("ok"): baby_subtasks.append({"name":"发帖2","done":True,"detail":post2.get("url","")})
    else: baby_subtasks.append({"name":"发帖2","done":False,"detail":""})

    tasks = [
        {
            "name": "🍼 Baby 创作者任务",
            "type": "recurring",
            "status": "done" if baby_done else "waiting",
            "status_text": "今日已完成" if baby_done else f"等待触发 → {next_baby_str}",
            "subtasks": baby_subtasks,
            "note": "每日北京15:00自动执行：发2篇Square帖子 + 交易837 BABY",
        },
        {
            "name": "💎 DeFi 质押 (Lista DAO / USD1)",
            "type": "oneshot",
            "status": "done",
            "status_text": "已完成，等待收益发放",
            "subtasks": [
                {"name":"质押 USD1 到 Lista DAO","done":True,"detail":"≈0.00284 USD1 质押中"},
                {"name":"收益发放","done":False,"detail":"持续产生，质押期间自动累积"},
            ],
            "note": "BSC Lista DAO Yield池，USD1 质押中，APY 约 3-5%",
        },
        {
            "name": "🏆 合约交易者大赛",
            "type": "competition",
            "status": "active",
            "status_text": "进行中",
            "subtasks": [
                {"name":"自动参赛","done":True,"detail":"合约账户交易自动计入"},
                {"name":"当前排名","done":False,"detail":"需登录 APP 查看"},
            ],
            "note": "合约交易量自动统计，系统持续交易中",
        },
        {
            "name": "📈 亚股 ETF 嘉年华",
            "type": "competition",
            "status": "waiting",
            "status_text": "等待信号触发",
            "subtasks": [
                {"name":"监控亚股ETF合约","done":True,"detail":"ws_monitor 持续扫描"},
                {"name":"开仓交易","done":False,"detail":"等待≥70分信号"},
            ],
            "note": "策略：等待亚股ETF合约出现≥70分信号后自动开仓",
        },
    ]
    return jsonify(tasks)

# ── 总览 ──────────────────────────────────────────────────────────
@app.route("/api/overview")
def overview():
    try:
        acct=api.request("GET", f"{api.BASE_FAPI}/fapi/v2/account")
        margin_bal=float(acct.get("totalMarginBalance",0))
        avail_bal=float(acct.get("availableBalance",0))
        pnl=float(acct.get("totalUnrealizedProfit",0))
        pos=[p for p in api.get_positions() if float(p.get("positionAmt",0))!=0]
        st=rjson(WS/"cw_state.json"); cw_logs=rlist(WS/"cw_log.json")
        today=[l for l in cw_logs if l.get("ts",0)>(time.time()-86400)*1000]
        cw_positions = st.get("positions", {})
        ct_invested = sum(float(p.get("buy_usd",0)) for p in cw_positions.values())
        wb=0; wb_bnb=0
        try:
            r=subprocess.run(["baw","wallet","balance","--json"],capture_output=True,text=True,timeout=8)
            d=json.loads(r.stdout); items=d if isinstance(d,list) else (d.get("data") or [])
            wb=sum(float(i.get("balance",0)) for i in items if (i.get("symbol") or "").upper()=="USDT")
            wb_bnb=sum(float(i.get("value",0)) for i in items if (i.get("symbol") or "").upper()=="BNB")
        except: pass
        return jsonify({"futures_balance":round(margin_bal,2),"futures_avail":round(avail_bal,2),
                        "ct_invested":round(ct_invested,2),"wallet_bnb_usd":round(wb_bnb,2),
                        "futures_pnl":round(pnl,4),"futures_positions":len(pos),
                        "ct_positions":len(cw_positions),
                        "ct_trades_today":len([l for l in today if l.get("action")=="BUY_OK"]),
                        "wallet_usdt":round(wb,2),"ts":datetime.now(timezone.utc).isoformat()})
    except Exception as e: return jsonify({"error":str(e)}),500

# ── 聊天 ──────────────────────────────────────────────────────────
JARVIS_TARGET = "432401404:thread:019f9203-3b29-7d9e-bfb4-b67c0b06587c"
CHAT_LOG = WS / "logs" / "webchat_log.json"
_chat_lock = threading.Lock()

def _load_chat():
    try:
        return json.loads(CHAT_LOG.read_text())
    except:
        return []

def _save_chat(msgs):
    CHAT_LOG.parent.mkdir(exist_ok=True)
    CHAT_LOG.write_text(json.dumps(msgs[-200:], ensure_ascii=False, indent=2))

@app.route("/api/chat/history")
def chat_history():
    since = int(flask_request.args.get("since", 0))
    msgs = _load_chat()
    return jsonify([m for m in msgs if m["ts"] > since])

@app.route("/api/chat/send", methods=["POST"])
def chat_send():
    data = flask_request.get_json(force=True)
    text = (data.get("message") or "").strip()
    if not text:
        return jsonify({"error": "empty"}), 400

    ts = int(time.time() * 1000)
    user_msg = {"role": "user", "text": text, "ts": ts}
    with _chat_lock:
        msgs = _load_chat()
        msgs.append(user_msg)
        _save_chat(msgs)

    # 异步发送给 Jarvis，不阻塞响应
    def _send():
        try:
            r = subprocess.run(
                ["/usr/local/bin/openclaw", "message", "send",
                 "--target", JARVIS_TARGET, "--channel", "jarvis",
                 "--message", text],
                capture_output=True, text=True, timeout=30
            )
        except Exception as e:
            pass  # 静默失败，前端通过轮询拿回复
    threading.Thread(target=_send, daemon=True).start()

    return jsonify({"ok": True, "ts": ts})

@app.route("/api/chat/push", methods=["POST"])
def chat_push():
    """由 Jarvis/外部推入 AI 回复（可选扩展，当前用轮询兜底）"""
    data = flask_request.get_json(force=True)
    text = (data.get("message") or "").strip()
    if not text:
        return jsonify({"error": "empty"}), 400
    msg = {"role": "assistant", "text": text, "ts": int(time.time() * 1000)}
    with _chat_lock:
        msgs = _load_chat()
        msgs.append(msg)
        _save_chat(msgs)
    return jsonify({"ok": True})


    from flask import send_file
    return send_file(WS/"dashboard.html")

# ⚠️ Dashboard API 已暂停 — 如需恢复请取消注释
# if __name__=="__main__":
#     print("Dashboard API → http://0.0.0.0:8765")
#     app.run(host="0.0.0.0",port=8765,debug=False,threaded=True)
