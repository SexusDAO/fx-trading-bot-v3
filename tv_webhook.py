import os
#!/usr/bin/env python3
"""
TradingView Webhook 接收服务
监听端口 9876，接收 TV Alert 的 POST 请求
解析信号后注入合约扫描器的优先队列
"""
from http.server import HTTPServer, BaseHTTPRequestHandler
import json, subprocess, time, hmac, hashlib, threading
from pathlib import Path
from datetime import datetime, timezone

PORT        = 80
SECRET      = os.environ.get("TV_WEBHOOK_SECRET", "")          # TV Alert message 里带上这个做验证
WORKSPACE   = Path("/root/.openclaw/workspace")
LOG_FILE    = WORKSPACE / "tv_webhook.log"
PRIORITY_FILE = WORKSPACE / "tv_priority_signals.json"
JARVIS_TARGET = "432401404:thread:019fa944-6d97-7061-b957-beca3ac89d75"

def log(msg):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

def notify(msg):
    subprocess.run([
        "/usr/local/bin/openclaw", "message", "send",
        "--target", JARVIS_TARGET, "--channel", "jarvis", "--message", msg
    ], capture_output=True, timeout=15)

def inject_priority(signal: dict):
    """写入优先信号文件，scanner 下次扫描时优先处理"""
    signals = []
    if PRIORITY_FILE.exists():
        try: signals = json.loads(PRIORITY_FILE.read_text())
        except: pass
    signal["recv_ts"] = int(time.time() * 1000)
    signals.append(signal)
    # 只保留最近 50 条
    PRIORITY_FILE.write_text(json.dumps(signals[-50:], ensure_ascii=False, indent=2))

class WebhookHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # 关闭默认 access log

    def do_GET(self):
        log(f"[ACCESS] GET {self.path} from {self.client_address[0]} UA={self.headers.get('User-Agent','')[:80]}")
        if self.path in ("/", "/health"):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
        else:
            self.send_response(404)
            self.end_headers()

    def do_OPTIONS(self):
        # CORS 预检请求
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_POST(self):
        log(f"[ACCESS] POST {self.path} from {self.client_address[0]} UA={self.headers.get('User-Agent','')[:80]}")
        try:
            length = int(self.headers.get("Content-Length", 0))
            body   = self.rfile.read(length).decode("utf-8")
            data   = json.loads(body)

            # 验证 secret
            if data.get("secret") != SECRET:
                log(f"[REJECT] 无效 secret，来自 {self.client_address[0]}")
                self.send_response(403)
                self.end_headers()
                return

            sym    = data.get("symbol", "").upper()
            action = data.get("action", "")   # LONG / SHORT / CLOSE
            price  = float(data.get("price", 0))
            tf     = data.get("tf", "")
            reason = data.get("reason", "TV Alert")

            log(f"[RECV] {sym} {action} @{price} tf={tf} reason={reason}")

            signal = {
                "symbol":  sym,
                "action":  action,
                "price":   price,
                "tf":      tf,
                "reason":  reason,
                "source":  "tradingview",
            }
            inject_priority(signal)

            notify(
                f"📡 TradingView 信号\n"
                f"${sym} {action} @{price}\n"
                f"时间框架: {tf}\n"
                f"理由: {reason}\n"
                f"→ 已注入扫描队列"
            )

            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ok":true}')

        except Exception as e:
            log(f"[ERROR] {e}")
            self.send_response(500)
            self.end_headers()

# ⚠️ TV Webhook 已暂停 — 如需恢复请取消注释
# if __name__ == "__main__":
#     server = HTTPServer(("0.0.0.0", PORT), WebhookHandler)
#     log(f"TV Webhook 服务启动 → 0.0.0.0:{PORT}")
#     log(f"健康检查: http://34.146.238.192:{PORT}/health")
#     log(f"信号接收: http://34.146.238.192:{PORT}/")
#     server.serve_forever()

# 临时 patch: 在 handler 里打印所有请求
_orig_handle = WebhookHandler.handle
