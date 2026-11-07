"use strict";

// Every value on the page comes from /api/poll or from a kill/restart
// response. Dot changes are CSS transitions on data-state, nothing here
// animates. Server data only reaches the page through textContent.

const TICK_MS = 2000;
const STATE_WORDS = {
  ok: "OK", slow: "SLOW", gated: "GATED", down: "DOWN",
  unconfigured: "NOT SET", none: "NO DATA",
};
const SEVERITY = { none: 0, unconfigured: 0, ok: 1, slow: 2, gated: 2, down: 3 };
const EDGE_MARKERS = [
  ["CF-Ray", (h) => "cf-ray" in h],
  ["CF-Cache-Status", (h) => "cf-cache-status" in h],
  ["Server: cloudflare", (h) => (h.server || "").toLowerCase() === "cloudflare"],
];

const $ = (id) => document.getElementById(id);
let polling = false;
let busy = false;
let lastCycle = { phase: "idle" };
let manageTunnel = false;
let restartHint = "";

function setHop(id, result) {
  const hop = $(id);
  const state = result ? result.state : "none";
  hop.dataset.state = state;
  hop.querySelector(".hop-state").textContent = STATE_WORDS[state] || state;
  hop.querySelector(".hop-latency").textContent =
    result && result.latency !== null ? `${Math.round(result.latency)} ms` : "-";
}

function setLink(id, left, right) {
  const worse = SEVERITY[left] >= SEVERITY[right] ? left : right;
  $(id).dataset.state = worse;
}

function renderPipeline(poll) {
  const states = poll
    ? [poll.public.state, poll.public.state, poll.public.state,
       poll.local_tun.state, poll.local_full.state]
    : ["none", "none", "none", "none", "none"];
  ["hop-user", "hop-edge", "hop-tunnel"].forEach((id) => setHop(id, poll && poll.public));
  setHop("hop-tun", poll && poll.local_tun);
  setHop("hop-full", poll && poll.local_full);
  [1, 2, 3, 4].forEach((n) => setLink(`link-${n}`, states[n - 1], states[n]));
}

function renderBanner(poll) {
  const banner = $("banner");
  const say = (tone, text) => { banner.dataset.tone = tone; banner.textContent = text; };
  if (!poll) return say("bad", "Dashboard lost contact with showcase.py");
  const localUp = poll.local_tun.status === 200 && poll.local_full.status === 200;
  const pub = poll.public.state;
  if (!localUp) return say("bad", "Origin not responding");
  if (pub === "unconfigured") return say("warn", "Origin healthy. No public tunnel found");
  if (pub === "down") return say("bad", "PUBLIC PATH DOWN. Origin still healthy");
  if (pub === "gated") return say("warn", "Cloudflare Access is gating this path");
  say("good", "Traffic reaching origin through Cloudflare");
}

function headerRow(name, value, isEdge) {
  const row = document.createElement("tr");
  if (isEdge) row.className = "edge";
  [name, value].forEach((text) => {
    const cell = document.createElement("td");
    cell.textContent = text;
    row.appendChild(cell);
  });
  return row;
}

function renderHeaderColumn(bodyId, ageId, result, edgeOnly) {
  const rows = result
    ? Object.entries(result.headers)
        .sort(([a], [b]) => Number(edgeOnly.includes(b)) - Number(edgeOnly.includes(a)))
        .map(([name, value]) => headerRow(name, value, edgeOnly.includes(name)))
    : [];
  $(bodyId).replaceChildren(...rows);
  $(ageId).textContent = result ? `last 200 at ${result.time}` : "no response yet";
}

function renderHeaders(poll) {
  const { public: pub, local, edge_only: edgeOnly } = poll.header_diff;
  renderHeaderColumn("headers-public", "public-age", pub, edgeOnly);
  renderHeaderColumn("headers-local", "local-age", local, []);
  if (pub && pub.state === "gated") {
    $("public-age").textContent = `Access 302 at ${pub.time}`;
  }
  const seen = {};
  Object.entries(pub ? pub.headers : {}).forEach(([k, v]) => { seen[k.toLowerCase()] = v; });
  const chips = EDGE_MARKERS.map(([label, test]) => {
    const chip = document.createElement("li");
    chip.textContent = label;
    chip.dataset.seen = test(seen) ? "yes" : "no";
    return chip;
  });
  const note = document.createElement("li");
  note.textContent = "CF-Connecting-IP: request header, sent to origin";
  chips.push(note);
  $("markers").replaceChildren(...chips);
}

function renderLog(rows) {
  const items = rows.map((r) => {
    const row = document.createElement("tr");
    row.dataset.state = r.state;
    const status = r.status === null ? "-" : String(r.status);
    [r.time, r.endpoint, status, Math.round(r.latency), r.ray || "-"].forEach((value, i) => {
      const cell = document.createElement("td");
      cell.textContent = value;
      if (i === 3) cell.className = "num";
      row.appendChild(cell);
    });
    if (r.detail) {
      const tag = document.createElement("span");
      tag.className = "tag";
      tag.textContent = r.detail;
      row.cells[2].appendChild(tag);
    }
    return row;
  });
  $("log-rows").replaceChildren(...items);
}

function renderTunnel(tunnel) {
  const line = $("tunnel-line");
  line.dataset.running = tunnel.running ? "yes" : "no";
  line.textContent = tunnel.running
    ? `cloudflared: RUNNING (pid ${tunnel.pids.join(", ")})`
    : "cloudflared: STOPPED";
  $("tunnel-connections").textContent = tunnel.running
    ? `Edge connections: ${tunnel.connections === null ? "not reported" : tunnel.connections}`
    : "";
  $("tunnel-problem").hidden = !tunnel.problem;
  $("tunnel-problem").textContent = tunnel.problem || "";
}

const seconds = (value) => (value === null ? "-" : `${value.toFixed(1)}s`);

function renderMeasurement(cycle) {
  lastCycle = cycle;
  const hasResult = cycle.phase === "down" || cycle.phase === "recovered"
    || cycle.phase === "no_failure";
  $("measurement").hidden = !hasResult && cycle.phase !== "killing";
  $("m-outage").textContent = seconds(cycle.outage);
  $("m-outage-label").textContent = cycle.phase === "down" ? "Outage so far" : "Last outage";
  $("m-recovery").textContent = seconds(cycle.recovery);
  $("m-unreachable").textContent = seconds(cycle.time_to_unreachable);
  const healthy = $("m-healthy");
  const known = cycle.phase === "down" || cycle.phase === "recovered";
  healthy.dataset.value = known ? (cycle.origin_healthy ? "yes" : "no") : "none";
  healthy.textContent = known ? (cycle.origin_healthy ? "YES" : "NO") : "-";
  const clocks = {
    restart_spawned: "Recovery timed from the restart command.",
    process_seen: "Recovery timed from when cloudflared was first seen running.",
    button_press: "Recovery timed from the button press; cloudflared was already running.",
  };
  $("m-clock").textContent = cycle.phase === "no_failure"
    ? `PUBLIC never failed in the wait window. Another connector may be serving it.`
    : (clocks[cycle.recovery_clock] || "");
}

function renderButtons(poll) {
  const down = lastCycle.phase === "down";
  $("kill").disabled = busy || !poll || !poll.config.public_url || !poll.tunnel.running || down;
  $("restart").disabled = busy || !down;
  $("restart").textContent = manageTunnel ? "Restart tunnel" : "Watch for recovery";
}

async function pollOnce() {
  if (polling) return;       // a slow tick never overlaps the next one
  polling = true;
  try {
    const response = await fetch("/api/poll", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const poll = await response.json();
    manageTunnel = poll.config.manage_tunnel;
    restartHint = poll.config.restart_hint;
    $("public-url").textContent = poll.config.public_url
      ? poll.config.public_url + poll.config.health_path : "no public URL yet";
    renderPipeline(poll);
    renderBanner(poll);
    renderHeaders(poll);
    renderLog(poll.log);
    renderTunnel(poll.tunnel);
    renderMeasurement(poll.cycle);
    renderButtons(poll);
  } catch (error) {
    renderPipeline(null);    // no data means no claim: dots go grey
    renderBanner(null);
    renderButtons(null);
  } finally {
    polling = false;
  }
}

async function post(path) {
  const response = await fetch(path, { method: "POST" });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
  return body;
}

async function runAction(path, pending, finished) {
  busy = true;
  $("kill").disabled = true;
  $("restart").disabled = true;
  $("action-message").textContent = pending;
  try {
    const cycle = await post(path);
    renderMeasurement(cycle);
    $("action-message").textContent = finished(cycle);
    return cycle;
  } catch (error) {
    $("action-message").textContent = error.message;
    return null;
  } finally {
    busy = false;
  }
}

function watchManualRestart() {
  return runAction("/api/restart",
    `Start the tunnel in Terminal 2: ${restartHint}`,
    (c) => (c.phase === "recovered" ? "" : "Still down. Recovery is recorded when PUBLIC returns."));
}

$("kill").addEventListener("click", async () => {
  const cycle = await runAction("/api/kill", "Killing cloudflared, probing PUBLIC...",
    (c) => (c.phase === "down" ? "" : "cloudflared was killed but PUBLIC never failed."));
  if (cycle && cycle.phase === "down" && !manageTunnel) await watchManualRestart();
});

$("restart").addEventListener("click", () => (manageTunnel
  ? runAction("/api/restart", "Restarting tunnel, probing PUBLIC...",
      (c) => (c.phase === "recovered" ? "" : "Still down. Check the tunnel log."))
  : watchManualRestart()));

pollOnce();
setInterval(pollOnce, TICK_MS);
