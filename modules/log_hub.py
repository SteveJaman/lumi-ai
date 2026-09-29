"""
Log hub — central diagnostic event stream with a localhost dashboard.

Modules send structured events here instead of spamming print(). Events
are kept in an in-memory ring buffer, written to data/logs/events.log
for persistence, and served at http://127.0.0.1:<port>/ as a live
dashboard.

Designed to eliminate terminal clutter from high-frequency loops like
ambient perception (skips every 30s) and heartbeat (ticks every 30s)
while keeping important events visible and reviewable.
"""

import json
import os
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from constants import DATA_ROOT


DEFAULT_PORT = 5001
DEFAULT_MAXLEN = 800

# Default log location — under data/logs/ so the repo root stays clean
# and everything the app writes at runtime lives in one gitignored folder.
_DEFAULT_LOG_FILE = os.path.join(DATA_ROOT, "logs", "events.log")


def emit(signals, category, message, level="info"):
    """Convenience helper: safely emit to the hub if it exists on signals.

    Call from any module: emit(self.signals, "ambient", "skip ...").
    No-op if the hub isn't attached (defensive for tests / partial setups).
    """
    hub = getattr(signals, "log_hub", None)
    if hub is not None:
        hub.emit(category, message, level)


class LogHub:
    def __init__(self, port=DEFAULT_PORT, maxlen=DEFAULT_MAXLEN,
                 log_file=_DEFAULT_LOG_FILE):
        self.port = port
        self.events = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self.log_file = log_file
        os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
        self._server = None
        self._thread = None

    def emit(self, category, message, level="info"):
        """Record an event. Thread-safe. Cheap — call it freely."""
        event = {
            "ts": time.time(),
            "time": time.strftime("%H:%M:%S"),
            "category": str(category).lower(),
            "level": level,
            "message": str(message)[:600],
        }
        with self._lock:
            self.events.append(event)

        try:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(f"[{event['time']}] {event['category'].upper()}: "
                        f"{event['message']}\n")
        except Exception:
            pass

    def get_events(self, limit=400):
        with self._lock:
            return list(self.events)[-limit:]

    def start(self):
        """Start the dashboard HTTP server in a daemon thread."""
        hub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                pass  # silence default HTTP logging

            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if path in ("/", "/index.html"):
                    body = DASHBOARD_HTML.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif path == "/events":
                    body = json.dumps(hub.get_events()).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(404)
                    self.end_headers()

        def serve():
            try:
                self._server = ThreadingHTTPServer(
                    ("127.0.0.1", hub.port), Handler
                )
                self._server.serve_forever()
            except Exception as e:
                print(f"\033[91mLOG HUB: Failed to start on port "
                      f"{hub.port} — {e}\033[0m")

        self._thread = threading.Thread(
            target=serve, daemon=True, name="LogHubServer"
        )
        self._thread.start()

    def stop(self):
        if self._server:
            try:
                self._server.shutdown()
            except Exception:
                pass


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Lumi — Diagnostics</title>
<style>
  :root {
    --bg:#0d1117; --panel:#161b22; --border:#30363d;
    --text:#c9d1d9; --dim:#8b949e; --accent:#58a6ff;
    --ambient:#a371f7; --heartbeat:#f778ba; --memory:#7ee787;
    --emotional:#ffa657; --mic:#79c0ff; --system:#8b949e;
    --vts:#56d4dd;
    --print:#6e7681;
    --warn:#f0883e; --error:#f85149;
  }
  * { box-sizing: border-box; }
  body { background:var(--bg); color:var(--text);
         font-family:'Cascadia Code','Consolas','Menlo',monospace;
         margin:0; padding:20px; font-size:13px; }
  header { display:flex; align-items:center; justify-content:space-between;
           border-bottom:1px solid var(--border); padding-bottom:12px;
           margin-bottom:16px; }
  h1 { font-size:16px; margin:0; color:var(--accent); font-weight:600;
       letter-spacing:0.02em; }
  .status { color:var(--dim); font-size:12px; }
  .status .dot { display:inline-block; width:8px; height:8px;
                 border-radius:50%; background:var(--memory);
                 margin-right:6px; vertical-align:middle;
                 animation:pulse 2s ease-in-out infinite; }
  @keyframes pulse { 0%,100%{opacity:1;} 50%{opacity:0.35;} }
  .filters { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:12px; }
  .filter { background:var(--panel); border:1px solid var(--border);
            color:var(--text); padding:5px 12px; border-radius:4px;
            cursor:pointer; font-family:inherit; font-size:12px;
            transition:background 0.15s, color 0.15s; }
  .filter:hover { border-color:var(--accent); }
  .filter.active { background:var(--accent); color:var(--bg);
                   border-color:var(--accent); font-weight:600; }
  #events { background:var(--panel); border:1px solid var(--border);
            border-radius:6px; padding:6px 0;
            max-height:calc(100vh - 190px); overflow-y:auto; }
  .event { padding:5px 14px; border-bottom:1px solid rgba(48,54,61,0.35);
           display:grid; grid-template-columns:72px 90px 1fr;
           gap:12px; align-items:baseline; }
  .event:last-child { border-bottom:none; }
  .event:hover { background:rgba(88,166,255,0.06); }
  .ts { color:var(--dim); font-size:11px; }
  .cat { font-weight:600; font-size:11px; text-transform:uppercase;
         letter-spacing:0.04em; }
  .cat.ambient   { color:var(--ambient); }
  .cat.heartbeat { color:var(--heartbeat); }
  .cat.memory    { color:var(--memory); }
  .cat.emotional { color:var(--emotional); }
  .cat.mic       { color:var(--mic); }
  .cat.vts       { color:var(--vts); }
  .cat.print     { color:var(--print); font-style:italic; }
  .cat.system    { color:var(--system); }
  .cat.log       { color:var(--dim); }
  .cat.discord   { color:#7c9cff; }
  .msg { color:var(--text); word-break:break-word; line-height:1.45; }
  .msg.warn  { color:var(--warn); }
  .msg.error { color:var(--error); }
  .empty { color:var(--dim); padding:24px; text-align:center;
           font-style:italic; }
</style>
</head>
<body>
<header>
  <h1>◆ Lumi — Diagnostics</h1>
  <div class="status">
    <span class="dot"></span>
    <span id="count">0</span> events &middot; refresh 2s
  </div>
</header>
<div class="filters" id="filters"></div>
<div id="events"><div class="empty">Waiting for events…</div></div>
<script>
  const CATEGORIES = ['all','ambient','heartbeat','memory',
                      'emotional','mic','vts','discord','system','print'];
  let activeFilter = 'all';
  let events = [];

  const filtersEl = document.getElementById('filters');
  CATEGORIES.forEach(c => {
    const btn = document.createElement('button');
    btn.className = 'filter' + (c === 'all' ? ' active' : '');
    btn.textContent = c;
    btn.onclick = () => {
      activeFilter = c;
      [...filtersEl.children].forEach(b =>
        b.classList.toggle('active', b.textContent === c));
      render();
    };
    filtersEl.appendChild(btn);
  });

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c =>
      ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  }

  function render() {
    const container = document.getElementById('events');
    const filtered = activeFilter === 'all'
      ? events
      : events.filter(e => e.category === activeFilter);

    if (filtered.length === 0) {
      container.innerHTML = '<div class="empty">No events in this category.</div>';
    } else {
      container.innerHTML = filtered.slice(-300).map(e => {
        const levelClass = e.level !== 'info' ? ' ' + e.level : '';
        return '<div class="event">' +
          '<span class="ts">' + e.time + '</span>' +
          '<span class="cat ' + e.category + '">' + e.category + '</span>' +
          '<span class="msg' + levelClass + '">' + escapeHtml(e.message) + '</span>' +
          '</div>';
      }).join('');
    }
    container.scrollTop = container.scrollHeight;
    document.getElementById('count').textContent = events.length;
  }

  async function poll() {
    try {
      const r = await fetch('/events', { cache: 'no-store' });
      events = await r.json();
      render();
    } catch (e) { /* server restarting or down — ignore */ }
  }

  poll();
  setInterval(poll, 2000);
</script>
</body>
</html>
"""