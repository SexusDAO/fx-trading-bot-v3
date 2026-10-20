"""
简单聊天代理服务器 — 转发消息到 Jarvis/OpenClaw
"""
from http.server import HTTPServer, BaseHTTPRequestHandler
import json, subprocess, threading, queue, os, time

CHAT_FILE = "/root/.openclaw/workspace/webchat/messages.jsonl"

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass  # 静默日志

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML.encode())
        elif self.path == "/messages":
            msgs = []
            if os.path.exists(CHAT_FILE):
                with open(CHAT_FILE) as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try: msgs.append(json.loads(line))
                            except: pass
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(msgs[-100:]).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/send":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            msg = body.get("message", "").strip()
            if msg:
                # 保存用户消息
                entry = {"role": "user", "text": msg, "ts": time.time()}
                with open(CHAT_FILE, "a") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                # 异步转发给 openclaw/jarvis session
                threading.Thread(target=self.forward_to_agent, args=(msg,), daemon=True).start()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
        else:
            self.send_response(404)
            self.end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def forward_to_agent(self, msg):
        try:
            result = subprocess.run(
                ["openclaw", "session", "send", "--session", "agent:main:jarvis:direct:432401404:t:019f9203-3b29-7d9e-bfb4-b67c0b06587c", "--message", msg],
                capture_output=True, text=True, timeout=60
            )
            reply = result.stdout.strip() or result.stderr.strip() or "(no response)"
        except Exception as e:
            reply = f"Error: {e}"
        entry = {"role": "assistant", "text": reply, "ts": time.time()}
        with open(CHAT_FILE, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

HTML = '''<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Binance AI Pro</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, sans-serif; background: #1a1a2e; color: #eee; height: 100vh; display: flex; flex-direction: column; }
#header { background: #F0B90B; padding: 14px 20px; font-weight: bold; font-size: 18px; color: #1a1a1a; display: flex; align-items: center; gap: 10px; }
#header span { font-size: 22px; }
#messages { flex: 1; overflow-y: auto; padding: 20px; display: flex; flex-direction: column; gap: 12px; }
.msg { max-width: 75%; padding: 10px 14px; border-radius: 16px; line-height: 1.5; white-space: pre-wrap; word-break: break-word; }
.user { background: #F0B90B; color: #1a1a1a; align-self: flex-end; border-bottom-right-radius: 4px; }
.assistant { background: #2a2a4a; color: #eee; align-self: flex-start; border-bottom-left-radius: 4px; }
.ts { font-size: 11px; opacity: 0.5; margin-top: 4px; }
#input-area { padding: 14px 16px; background: #16213e; display: flex; gap: 10px; border-top: 1px solid #333; }
#input { flex: 1; background: #2a2a4a; border: 1px solid #444; border-radius: 22px; padding: 10px 18px; color: #eee; font-size: 15px; outline: none; resize: none; }
#input:focus { border-color: #F0B90B; }
#send { background: #F0B90B; color: #1a1a1a; border: none; border-radius: 22px; padding: 10px 22px; font-weight: bold; cursor: pointer; font-size: 15px; }
#send:hover { background: #d4a309; }
.thinking { opacity: 0.5; font-style: italic; }
</style>
</head>
<body>
<div id="header"><span>🦞</span> Binance AI Pro</div>
<div id="messages"></div>
<div id="input-area">
  <textarea id="input" rows="1" placeholder="输入消息，按 Enter 发送…"></textarea>
  <button id="send">发送</button>
</div>
<script>
let lastCount = 0;

function fmt(ts) {
  return new Date(ts*1000).toLocaleTimeString('zh-CN', {hour:'2-digit',minute:'2-digit'});
}

async function loadMessages() {
  try {
    const r = await fetch('/messages');
    const msgs = await r.json();
    if (msgs.length !== lastCount) {
      lastCount = msgs.length;
      const el = document.getElementById('messages');
      // remove thinking indicator
      document.querySelectorAll('.thinking-msg').forEach(e=>e.remove());
      el.innerHTML = msgs.map(m => `
        <div class="msg ${m.role}">
          ${m.text.replace(/</g,'&lt;').replace(/>/g,'&gt;')}
          <div class="ts">${fmt(m.ts)}</div>
        </div>
      `).join('');
      el.scrollTop = el.scrollHeight;
    }
  } catch(e) {}
}

async function send() {
  const input = document.getElementById('input');
  const msg = input.value.trim();
  if (!msg) return;
  input.value = '';
  input.style.height = 'auto';

  // optimistic UI
  const el = document.getElementById('messages');
  const now = Date.now()/1000;
  el.innerHTML += `<div class="msg user">${msg.replace(/</g,'&lt;')}<div class="ts">${fmt(now)}</div></div>`;
  el.innerHTML += `<div class="msg assistant thinking thinking-msg">🤔 思考中…</div>`;
  el.scrollTop = el.scrollHeight;
  lastCount = 0; // force refresh

  await fetch('/send', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({message: msg})});
}

document.getElementById('send').onclick = send;
document.getElementById('input').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
});
document.getElementById('input').addEventListener('input', function() {
  this.style.height = 'auto';
  this.style.height = Math.min(this.scrollHeight, 120) + 'px';
});

setInterval(loadMessages, 2000);
loadMessages();
</script>
</body>
</html>'''

if __name__ == "__main__":
    port = 18800
    print(f"Chat server running on port {port}", flush=True)
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()
