"""
Lumi x Minecraft — Diagnostics dashboard (http://127.0.0.1:5002/).

Standalone server — run separately from minecraft_bot.py:
    python plugin/mc_dashboard.py

Panels:
  • Connection status  — Lumi SDK client status + bot.js control API reachability
  • Bot telemetry      — position, health, food, inventory, current goal
  • Command relay      — live stream of SDK and control-API events from events.jsonl
  • Error tracker      — recent errors extracted from the event log

Data sources (all local files written by minecraft_bot.py / bot.js):
  data/minecraft/telemetry.json  — live bot state
  data/minecraft/events.jsonl    — agent + SDK event log

The page also makes client-side calls to 127.0.0.1:5057/status and
127.0.0.1:5057/events directly (bot.js adds CORS headers).
"""

from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

DATA_ROOT      = os.path.join(_ROOT, "data")
MC_DIR         = os.path.join(DATA_ROOT, "minecraft")
TELEMETRY_PATH = os.path.join(MC_DIR, "telemetry.json")
EVENTS_PATH    = os.path.join(MC_DIR, "events.jsonl")
PORT           = int(os.environ.get("MC_DASH_PORT", "5002"))
MAX_EVENTS     = 200


def _read_telemetry() -> dict:
    try:
        with open(TELEMETRY_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _read_events(limit: int = MAX_EVENTS) -> list:
    events = []
    try:
        with open(EVENTS_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        events.append(json.loads(line))
                    except Exception:
                        pass
    except Exception:
        pass
    return events[-limit:]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # silence default HTTP logs

    def do_GET(self):
        path = self.path.split("?")[0]

        if path in ("/", "/index.html"):
            body = _DASHBOARD_HTML.encode("utf-8")
            self._respond(200, "text/html; charset=utf-8", body)

        elif path == "/api/telemetry":
            body = json.dumps(_read_telemetry()).encode("utf-8")
            self._respond(200, "application/json", body)

        elif path == "/api/events":
            body = json.dumps(_read_events()).encode("utf-8")
            self._respond(200, "application/json", body)

        else:
            self._respond(404, "text/plain", b"not found")

    def _respond(self, code: int, content_type: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)


def serve():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"[mc-dash] dashboard → http://127.0.0.1:{PORT}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[mc-dash] stopped")


# ─── Dashboard HTML ───────────────────────────────────────────────────────────

_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lumi × Minecraft — Diagnostics</title>
<style>
/* ── Lumi design tokens (dark default) ─────────────────────────── */
:root {
  --bg:          #08121A;
  --bg-elev:     #0F1D28;
  --bg-inset:    #050B11;
  --border:      #1C3340;
  --border-hi:   #2E4D5E;
  --text:        #CFE9F5;
  --text-dim:    #7EA8BF;
  --text-mute:   #4D7186;
  --accent:      #81D4C6;
  --accent-2:    #B9F1FA;
  --success:     #4ADE9E;
  --warn:        #FBBF6B;
  --error:       #F87171;
  --sh:          0 4px 16px rgba(0,0,0,.35);
  --font-mono:   'Cascadia Code','Consolas','Menlo',monospace;
}
html[data-theme="light"] {
  --bg:          #F4F9F9;
  --bg-elev:     #FFFFFF;
  --bg-inset:    #EAF1F2;
  --border:      #CFDBDE;
  --border-hi:   #AAB9BD;
  --text:        #1E2A2E;
  --text-dim:    #4C5D63;
  --text-mute:   #7E9298;
  --accent:      #389183;
  --accent-2:    #3A8FA0;
  --success:     #2DA37D;
  --warn:        #D69A2E;
  --error:       #D65A5A;
  --sh:          0 4px 12px rgba(30,42,46,.10);
}

*  { box-sizing: border-box; margin: 0; padding: 0; }
body {
  background: var(--bg);
  color: var(--text);
  font-family: var(--font-mono);
  font-size: 13px;
  min-height: 100vh;
}

/* ── Header ─────────────────────────────────────────────────────── */
header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 20px;
  border-bottom: 1px solid var(--border);
  background: var(--bg-elev);
  position: sticky;
  top: 0;
  z-index: 10;
}
header h1 {
  font-size: 15px;
  font-weight: 700;
  color: var(--accent);
  letter-spacing: .03em;
}
.header-right { display: flex; gap: 16px; align-items: center; }
.pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  padding: 3px 9px;
  border-radius: 12px;
  border: 1px solid var(--border);
  color: var(--text-dim);
  background: var(--bg-inset);
}
.pill .dot {
  width: 7px; height: 7px;
  border-radius: 50%;
  background: var(--text-mute);
  flex-shrink: 0;
}
.pill.ok   .dot { background: var(--success); animation: pulse 2s infinite; }
.pill.warn .dot { background: var(--warn); }
.pill.err  .dot { background: var(--error); }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.35} }

.theme-btn {
  background: none; border: 1px solid var(--border);
  color: var(--text-dim); border-radius: 6px;
  padding: 4px 10px; cursor: pointer; font-family: inherit; font-size: 11px;
}
.theme-btn:hover { border-color: var(--accent); color: var(--text); }

/* ── Grid ────────────────────────────────────────────────────────── */
.grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  grid-template-rows: auto auto;
  gap: 16px;
  padding: 16px 20px;
  max-width: 1400px;
  margin: 0 auto;
}
@media (max-width: 860px) { .grid { grid-template-columns: 1fr; } }

/* ── Panel ───────────────────────────────────────────────────────── */
.panel {
  background: var(--bg-elev);
  border: 1px solid var(--border);
  border-radius: 8px;
  display: flex;
  flex-direction: column;
  box-shadow: var(--sh);
  overflow: hidden;
}
.panel-head {
  padding: 10px 14px;
  border-bottom: 1px solid var(--border);
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: .06em;
  color: var(--text-dim);
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.panel-head .badge {
  background: var(--bg-inset);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 1px 7px;
  font-size: 10px;
  font-weight: 600;
  color: var(--accent);
}
.panel-body {
  padding: 12px 14px;
  flex: 1;
  overflow-y: auto;
  max-height: 340px;
}

/* ── Telemetry rows ──────────────────────────────────────────────── */
.trow {
  display: grid;
  grid-template-columns: 120px 1fr;
  gap: 8px;
  padding: 5px 0;
  border-bottom: 1px solid rgba(28,51,64,.5);
  align-items: baseline;
}
.trow:last-child { border-bottom: none; }
.trow .label { color: var(--text-dim); font-size: 11px; }
.trow .value { color: var(--text); word-break: break-word; }
.trow .value.accent  { color: var(--accent); font-weight: 600; }
.trow .value.ok      { color: var(--success); }
.trow .value.warn    { color: var(--warn); }
.trow .value.bad     { color: var(--error); }

/* ── Progress bar ────────────────────────────────────────────────── */
.progress-wrap { margin-top: 4px; }
.progress-bar {
  height: 5px;
  background: var(--bg-inset);
  border-radius: 3px;
  overflow: hidden;
  margin-top: 6px;
}
.progress-fill {
  height: 100%;
  background: linear-gradient(90deg, var(--accent), var(--accent-2));
  border-radius: 3px;
  transition: width .5s ease;
}

/* ── Event log ───────────────────────────────────────────────────── */
.log-filters { display: flex; gap: 6px; flex-wrap: wrap; padding: 8px 14px; border-bottom: 1px solid var(--border); }
.lf {
  background: var(--bg-inset); border: 1px solid var(--border);
  color: var(--text-dim); border-radius: 4px; padding: 3px 9px;
  cursor: pointer; font-family: inherit; font-size: 11px;
}
.lf:hover { border-color: var(--accent); }
.lf.active { background: var(--accent); color: var(--bg); border-color: var(--accent); font-weight: 700; }

.log-body { max-height: 280px; overflow-y: auto; }
.log-line {
  display: grid;
  grid-template-columns: 60px 70px 1fr;
  gap: 10px;
  padding: 4px 14px;
  border-bottom: 1px solid rgba(28,51,64,.4);
  font-size: 12px;
}
.log-line:hover { background: rgba(129,212,198,.05); }
.log-line:last-child { border-bottom: none; }
.ll-time { color: var(--text-mute); font-size: 11px; }
.ll-cat  { font-weight: 700; font-size: 10px; text-transform: uppercase; letter-spacing: .05em; }
.ll-cat.sdk        { color: var(--accent); }
.ll-cat.relay      { color: var(--accent-2); }
.ll-cat.agent      { color: #A78BFA; }
.ll-cat.curriculum { color: #FBBF6B; }
.ll-cat.skill      { color: #34D399; }
.ll-cat.result     { color: var(--success); }
.ll-cat.warn       { color: var(--warn); }
.ll-cat.error      { color: var(--error); }
.ll-cat.context    { color: var(--text-mute); }
.ll-msg  { color: var(--text); word-break: break-word; line-height: 1.4; }
.ll-msg.error { color: var(--error); }
.ll-msg.warn  { color: var(--warn); }

/* ── Error panel ─────────────────────────────────────────────────── */
.err-item {
  display: flex;
  gap: 10px;
  padding: 6px 0;
  border-bottom: 1px solid rgba(248,113,113,.15);
  font-size: 12px;
}
.err-item:last-child { border-bottom: none; }
.err-time { color: var(--text-mute); flex-shrink: 0; font-size: 11px; }
.err-msg  { color: var(--error); word-break: break-word; line-height: 1.4; }
.empty    { color: var(--text-mute); text-align: center; padding: 20px; font-style: italic; }

/* ── Status pills row ────────────────────────────────────────────── */
.status-row {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  padding: 12px 14px;
  border-bottom: 1px solid var(--border);
}
.s-pill {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 5px 12px; border-radius: 20px; font-size: 12px;
  border: 1px solid var(--border); background: var(--bg-inset);
  color: var(--text-dim);
}
.s-pill .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--text-mute); flex-shrink: 0; }
.s-pill.ok   { border-color: rgba(74,222,158,.35); color: var(--success); }
.s-pill.ok   .dot { background: var(--success); animation: pulse 2.5s infinite; }
.s-pill.warn { border-color: rgba(251,191,107,.35); color: var(--warn); }
.s-pill.warn .dot { background: var(--warn); }
.s-pill.bad  { border-color: rgba(248,113,113,.35); color: var(--error); }
.s-pill.bad  .dot { background: var(--error); }
</style>
</head>
<body>

<header>
  <h1>◆ Lumi × Minecraft — Diagnostics</h1>
  <div class="header-right">
    <span class="pill" id="pill-agent">
      <span class="dot"></span><span id="pill-agent-txt">agent: —</span>
    </span>
    <span class="pill" id="pill-bot">
      <span class="dot"></span><span id="pill-bot-txt">bot: —</span>
    </span>
    <span style="font-size:11px;color:var(--text-mute)" id="refresh-label">refresh 2 s</span>
    <button class="theme-btn" onclick="toggleTheme()">☀ / ☾</button>
  </div>
</header>

<div class="grid">

  <!-- ── Telemetry ─────────────────────────────────────────────── -->
  <div class="panel">
    <div class="panel-head">
      Bot Telemetry
      <span class="badge" id="tel-updated">—</span>
    </div>
    <div class="panel-body" id="tel-body">
      <div class="empty">Waiting for telemetry…</div>
    </div>
  </div>

  <!-- ── Connection / status ───────────────────────────────────── -->
  <div class="panel">
    <div class="panel-head">Connection Status</div>
    <div class="status-row" id="conn-pills">
      <span class="s-pill" id="cp-agent"><span class="dot"></span> MinecraftAgent SDK</span>
      <span class="s-pill" id="cp-bot"><span class="dot"></span> bot.js control API (:5057)</span>
      <span class="s-pill" id="cp-loop"><span class="dot"></span> Skill loop</span>
    </div>
    <div class="panel-body" id="skill-body">
      <div class="empty">Waiting…</div>
    </div>
  </div>

  <!-- ── Command relay ─────────────────────────────────────────── -->
  <div class="panel" style="grid-column: 1 / -1">
    <div class="panel-head">
      Live Command Relay
      <span class="badge" id="ev-count">0</span>
    </div>
    <div class="log-filters" id="log-filters"></div>
    <div class="log-body" id="log-body">
      <div class="empty">No events yet.</div>
    </div>
  </div>

  <!-- ── Server settings ───────────────────────────────────────── -->
  <div class="panel" style="grid-column: 1 / -1">
    <div class="panel-head">
      Minecraft Server
      <span class="badge" id="srv-badge">LAN</span>
    </div>
    <div class="panel-body" style="max-height:none">
      <p style="color:var(--text-dim);font-size:12px;margin-bottom:12px">
        LAN worlds use a random port each launch. Enter the host and port shown
        in Minecraft's <em>Open to LAN</em> screen and click Reconnect —
        the bot reconnects immediately without restarting.
      </p>
      <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end">
        <label style="display:flex;flex-direction:column;gap:4px;font-size:11px;color:var(--text-dim)">
          Host
          <input id="srv-host" type="text" value="localhost"
            style="background:var(--bg-inset);border:1px solid var(--border);color:var(--text);
                   padding:6px 10px;border-radius:5px;font-family:inherit;font-size:13px;width:200px">
        </label>
        <label style="display:flex;flex-direction:column;gap:4px;font-size:11px;color:var(--text-dim)">
          Port
          <input id="srv-port" type="number" min="1" max="65535" value="25565"
            style="background:var(--bg-inset);border:1px solid var(--border);color:var(--text);
                   padding:6px 10px;border-radius:5px;font-family:inherit;font-size:13px;width:110px">
        </label>
        <button id="srv-btn" onclick="applyServer()"
          style="background:var(--accent);color:var(--bg);border:none;border-radius:5px;
                 padding:7px 18px;font-family:inherit;font-size:13px;font-weight:700;
                 cursor:pointer;align-self:flex-end">
          Reconnect
        </button>
        <span id="srv-msg" style="font-size:12px;color:var(--text-dim);align-self:flex-end"></span>
      </div>
    </div>
  </div>

  <!-- ── Error tracker ─────────────────────────────────────────── -->
  <div class="panel" style="grid-column: 1 / -1">
    <div class="panel-head">
      Error Tracker
      <span class="badge" id="err-count">0</span>
    </div>
    <div class="panel-body" id="err-body">
      <div class="empty">No errors.</div>
    </div>
  </div>

</div>

<script>
// ── State ─────────────────────────────────────────────────────────
let _tel  = {};
let _evts = [];  // from /api/events  (agent events)
let _bEvt = [];  // from :5057/events (bot.js relay events)
let _botSt = null;
let _logFilter = "all";

const LOG_CATS = ["all","sdk","relay","agent","curriculum","skill","result","warn","error"];

// ── Theme ─────────────────────────────────────────────────────────
function toggleTheme() {
  const html = document.documentElement;
  html.dataset.theme = html.dataset.theme === "dark" ? "light" : "dark";
  try { localStorage.setItem("lumi-mc-theme", html.dataset.theme); } catch(_) {}
}
try {
  const saved = localStorage.getItem("lumi-mc-theme");
  if (saved) document.documentElement.dataset.theme = saved;
} catch(_) {}

// ── Helpers ───────────────────────────────────────────────────────
function esc(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}
function cls(level) {
  return level === "error" ? " error" : level === "warn" ? " warn" : "";
}

// ── Build log filters ─────────────────────────────────────────────
const filtersEl = document.getElementById("log-filters");
LOG_CATS.forEach(c => {
  const b = document.createElement("button");
  b.className = "lf" + (c === "all" ? " active" : "");
  b.textContent = c;
  b.onclick = () => {
    _logFilter = c;
    [...filtersEl.children].forEach(x => x.classList.toggle("active", x.textContent === c));
    renderLog();
  };
  filtersEl.appendChild(b);
});

// ── Render telemetry ──────────────────────────────────────────────
function renderTelemetry() {
  const t = _tel;
  if (!t || Object.keys(t).length === 0) return;

  const inv = (t.inventory || []).map(i => `${i.name}×${i.count}`).join(", ") || "empty";
  const pos = t.position
    ? `${t.position.x}, ${t.position.y}, ${t.position.z}`
    : "unknown";
  const hp  = t.health  ?? "—";
  const fd  = t.food    ?? "—";
  const hpClass = t.health > 10 ? "ok" : t.health > 5 ? "warn" : "bad";
  const fdClass = t.food   > 10 ? "ok" : t.food   > 5 ? "warn" : "bad";

  // progress
  const total   = 10;
  const done    = t.skills_verified || 0;
  const pct     = Math.round(done / total * 100);

  document.getElementById("tel-updated").textContent = t.last_update || "—";

  document.getElementById("tel-body").innerHTML = `
    <div class="trow"><span class="label">Status</span><span class="value ${t.bot_status === 'ready' ? 'ok' : 'warn'}">${esc(t.bot_status || "—")}</span></div>
    <div class="trow"><span class="label">Position</span><span class="value accent">${esc(pos)}</span></div>
    <div class="trow"><span class="label">Health</span><span class="value ${hpClass}">${hp}/20</span></div>
    <div class="trow"><span class="label">Food</span><span class="value ${fdClass}">${fd}/20</span></div>
    <div class="trow"><span class="label">Inventory</span><span class="value">${esc(inv)}</span></div>
    <div class="trow"><span class="label">Current goal</span><span class="value accent">${esc(t.current_goal || "none")}</span></div>
    <div class="trow"><span class="label">Goal desc</span><span class="value">${esc(t.goal_description || "—")}</span></div>
    <div class="trow">
      <span class="label">Curriculum</span>
      <span class="value">
        ${done}/${total} verified (${pct}%)
        <div class="progress-bar"><div class="progress-fill" style="width:${pct}%"></div></div>
      </span>
    </div>
  `;
}

// ── Render connection status ──────────────────────────────────────
function renderStatus() {
  const t   = _tel;
  const bot = _botSt;

  // MinecraftAgent SDK pill
  const agentOk = t && t.lumi_sdk === "connected";
  setPill("cp-agent", agentOk ? "ok" : "warn",
    "Lumi SDK: " + (t.lumi_sdk || "offline"));

  // bot.js control API pill
  const botOk = bot && bot.ready;
  setPill("cp-bot", botOk ? "ok" : bot !== null ? "warn" : "bad",
    "bot.js :5057: " + (bot ? (bot.ready ? "ready" : "spawning") : "unreachable"));

  // Loop pill
  const loopOk = t && t.loop_state === "running";
  const loopPaused = t && t.loop_state === "paused";
  setPill("cp-loop", loopOk ? "ok" : loopPaused ? "warn" : "bad",
    "Skill loop: " + (t.loop_state || "—"));

  // Header pills
  updatePill("pill-agent", loopOk, "agent: " + (t.loop_state || "offline"));
  updatePill("pill-bot", botOk, "bot: " + (bot ? (bot.ready ? bot.username || "ready" : "spawning") : "unreachable"));

  // Skill table
  const skills = ["collect_wood","craft_planks","craft_workbench","craft_wood_pickaxe",
                  "mine_stone","craft_stone_pickaxe","mine_iron_ore","smelt_iron",
                  "craft_iron_pickaxe","mine_diamond"];
  // This data lives in telemetry's skills_verified count — we can't get per-skill
  // status from telemetry.json alone, so show the count only.
  document.getElementById("skill-body").innerHTML = `
    <div class="trow"><span class="label">Loop state</span><span class="value ${loopOk ? 'ok' : 'warn'}">${esc(t.loop_state || "—")}</span></div>
    <div class="trow"><span class="label">Bot username</span><span class="value">${esc(bot ? bot.username || "—" : "—")}</span></div>
    <div class="trow"><span class="label">Server</span><span class="value">${esc(bot ? bot.server || "—" : "—")}</span></div>
    <div class="trow"><span class="label">Skills done</span><span class="value ok">${(t.skills_verified || 0)} / ${skills.length}</span></div>
  `;
}

function setPill(id, state, label) {
  const el = document.getElementById(id);
  if (!el) return;
  el.className = "s-pill " + state;
  el.innerHTML = `<span class="dot"></span> ${esc(label)}`;
}
function updatePill(id, ok, label) {
  const el = document.getElementById(id);
  if (!el) return;
  el.className = "pill " + (ok ? "ok" : "err");
  el.innerHTML = `<span class="dot"></span><span>${esc(label)}</span>`;
}

// ── Render log ────────────────────────────────────────────────────
function renderLog() {
  // Merge agent events + bot relay events, sort by ts
  const merged = [..._evts, ..._bEvt].sort((a, b) => a.ts - b.ts);
  document.getElementById("ev-count").textContent = merged.length;
  const filtered = _logFilter === "all"
    ? merged : merged.filter(e => e.category === _logFilter);
  const tail = filtered.slice(-250);
  if (tail.length === 0) {
    document.getElementById("log-body").innerHTML = '<div class="empty">No events in this category.</div>';
    return;
  }
  document.getElementById("log-body").innerHTML = tail.map(e => `
    <div class="log-line">
      <span class="ll-time">${esc(e.time)}</span>
      <span class="ll-cat ${esc(e.category)}">${esc(e.category)}</span>
      <span class="ll-msg${cls(e.category)}">${esc(e.message)}</span>
    </div>
  `).join("");
  const lb = document.getElementById("log-body");
  lb.scrollTop = lb.scrollHeight;
}

// ── Render errors ─────────────────────────────────────────────────
function renderErrors() {
  const errs = (_tel.errors || []).slice().reverse();
  document.getElementById("err-count").textContent = errs.length;
  if (errs.length === 0) {
    document.getElementById("err-body").innerHTML = '<div class="empty">No errors recorded.</div>';
    return;
  }
  document.getElementById("err-body").innerHTML = errs.map(e => `
    <div class="err-item">
      <span class="err-time">${esc(e.time)}</span>
      <span class="err-msg">${esc(e.message)}</span>
    </div>
  `).join("");
}

// ── Fetch helpers ─────────────────────────────────────────────────
async function fetchJson(url) {
  try {
    const r = await fetch(url, { cache: "no-store" });
    if (!r.ok) return null;
    return await r.json();
  } catch (_) { return null; }
}

async function poll() {
  // Agent telemetry + events from Python server
  const [tel, evts] = await Promise.all([
    fetchJson("/api/telemetry"),
    fetchJson("/api/events"),
  ]);
  if (tel)  _tel  = tel;
  if (evts) _evts = evts;

  // Bot status + relay events directly from bot.js (:5057)
  const [botSt, botEvt] = await Promise.all([
    fetchJson("http://127.0.0.1:5057/status"),
    fetchJson("http://127.0.0.1:5057/events"),
  ]);
  _botSt = botSt;
  if (botEvt) _bEvt = botEvt;

  renderTelemetry();
  renderStatus();
  renderLog();
  renderErrors();
}

// ── Server settings ───────────────────────────────────────────────
async function loadServerConfig() {
  const cfg = await fetchJson("http://127.0.0.1:5057/config");
  if (!cfg) return;
  document.getElementById("srv-host").value = cfg.host || "localhost";
  document.getElementById("srv-port").value = cfg.port || 25565;
  document.getElementById("srv-badge").textContent = `${cfg.host}:${cfg.port}`;
}

async function applyServer() {
  const host = document.getElementById("srv-host").value.trim();
  const port = parseInt(document.getElementById("srv-port").value, 10);
  const msg  = document.getElementById("srv-msg");
  const btn  = document.getElementById("srv-btn");
  if (!host || isNaN(port)) { msg.textContent = "Enter a valid host and port."; return; }
  btn.disabled = true;
  btn.textContent = "Connecting…";
  msg.style.color = "var(--text-dim)";
  msg.textContent = "";
  try {
    const r = await fetch("http://127.0.0.1:5057/config", {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify({ host, port }),
    });
    const data = await r.json();
    if (data.success) {
      msg.style.color = "var(--success)";
      msg.textContent = data.message;
      document.getElementById("srv-badge").textContent = `${host}:${port}`;
    } else {
      msg.style.color = "var(--error)";
      msg.textContent = data.message || "Failed";
    }
  } catch (e) {
    msg.style.color = "var(--error)";
    msg.textContent = "Cannot reach bot.js control API (:5057) — is bot.js running?";
  }
  btn.disabled = false;
  btn.textContent = "Reconnect";
}

poll();
loadServerConfig();
setInterval(poll, 2000);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    serve()
