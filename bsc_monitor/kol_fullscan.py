#!/usr/bin/env python3
"""全量体检 BSC 动态 KOL 名单：对未体检的地址逐个拉 gmgn 7d 战绩，增量存 /tmp/kol_health.json"""
import json, subprocess, time

KOL_FILE = '/root/.openclaw/workspace/bsc_monitor/bsc_kol_wallets.json'
HEALTH_FILE = '/tmp/kol_health.json'

kol = json.load(open(KOL_FILE))
addrs = list(kol.keys())
try:
    health = json.load(open(HEALTH_FILE))
except Exception:
    health = {}
print(f'total={len(addrs)} done={len(health)} todo={len(addrs)-len(health)}', flush=True)

def probe(a):
    r = subprocess.run(['gmgn-cli', 'portfolio', 'stats', '--chain', 'bsc', '--wallet', a,
                        '--period', '7d', '--raw'], capture_output=True, text=True, timeout=35)
    d = json.loads(r.stdout)
    ps = d.get('pnl_stat') or {}
    try:
        pnl = float(d.get('realized_profit') or 0)
    except (TypeError, ValueError):
        pnl = 0.0
    wr = ps.get('winrate')
    if wr is not None:
        wr = float(wr)
    n = ps.get('token_num')
    buys = int(d.get('buy') or 0)
    return {'pnl': pnl, 'wr': wr, 'n': n, 'buys': buys}

def run_pass(todo, pass_name):
    BATCH = 10
    for i in range(0, len(todo), BATCH):
        batch = todo[i:i+BATCH]
        for a in batch:
            handle = kol.get(a) or '?'
            try:
                d = probe(a)
                health[a] = d
                wr = d['wr']
                wr_s = '?' if wr is None else f'{wr*100:.0f}%'
                print(f"{a[:10]}… @{handle}: 7d=${d['pnl']:,.0f} wr={wr_s} buys={d['buys']}", flush=True)
            except Exception as e:
                print(f"{a[:10]}… @{handle}: ERR {str(e)[:60]}", flush=True)
            time.sleep(2)
        json.dump(health, open(HEALTH_FILE, 'w'))
        print(f'--- {pass_name} progress {len(health)}/{len(addrs)} ---', flush=True)
        if i + BATCH < len(todo):
            time.sleep(20)

todo = [a for a in addrs if a not in health]
run_pass(todo, 'pass1')
# 失败重试一轮
todo = [a for a in addrs if a not in health]
if todo:
    print(f'retry pass: {len(todo)} failed', flush=True)
    time.sleep(30)
    run_pass(todo, 'pass2')
json.dump(health, open(HEALTH_FILE, 'w'))
print(f'DONE sampled={len(health)}/{len(addrs)}', flush=True)
