/**
 * Lumi × Minecraft SDK client
 *
 * Speaks the Lumi SDK wire protocol over WebSocket and bridges
 * Lumi action dispatches (sdk__*) to mineflayer bot commands.
 *
 * Voyager integration: the `minecraft_execute_skill` action accepts
 * Voyager-compatible async JavaScript and runs it against the live
 * mineflayer bot. All other actions are direct mineflayer primitives.
 *
 * Usage:
 *   cp .env.example .env   # fill in your values
 *   npm install
 *   node bot.js
 *
 * Requirements: Node 18+, a running Minecraft server, Lumi running with
 * the SDK server enabled (ws://127.0.0.1:5056).
 */

"use strict";

const WebSocket = require("ws");
const mineflayer = require("mineflayer");
const { pathfinder, Movements, goals } = require("mineflayer-pathfinder");
const { Vec3 } = require("vec3");
const { houseBlueprint, materialNeeded, STRUCTURAL } = require("./house_blueprint");
const { plugin: toolPlugin }   = require("mineflayer-tool");
const { plugin: collectBlock } = require("mineflayer-collectblock");
const { plugin: pvpPlugin }    = require("mineflayer-pvp");
const fs   = require("fs");
const path = require("path");
const http = require("http");
const net  = require("net");

require("dotenv").config({ path: path.join(__dirname, ".env") });

// ─── Config ────────────────────────────────────────────────────────────────

const LUMI_HOST      = process.env.LUMI_HOST       || "127.0.0.1";
const LUMI_PORT      = parseInt(process.env.LUMI_PORT || "5056", 10);
// Relative to this file so the example works from any working directory.
const TOKEN_PATH     = process.env.LUMI_TOKEN_PATH
                     || path.join(__dirname, "../../../data/memories/sdk_token.txt");
const GAME_NAME      = "MinecraftBot";
const CLIENT_VERSION = "0.1.0";
const RECONNECT_MS   = 5000;

let   MC_HOST     = process.env.MC_HOST     || "localhost";
let   MC_PORT     = parseInt(process.env.MC_PORT || "25565", 10);
const MC_USERNAME = process.env.MC_USERNAME || "LumiBot";
const MC_AUTH     = process.env.MC_AUTH     || "offline";
const MC_VERSION       = process.env.MC_VERSION        || undefined;
// Internal control API used by minecraft_bot.py — localhost only.
const BOT_CONTROL_PORT = parseInt(process.env.BOT_CONTROL_PORT || "5057", 10);
// Push game events (damage, mobs, deaths, rare loot) for Lumi to react to.
// "major" (default): deaths, boss kills, top-tier loot. "all": also damage,
// nearby mobs, hunger, kills, joining. "off": none. Every spoken event costs
// a turn of Lumi's chat model, so "major" keeps her chat responsive.
const EVENT_REACTIONS  = (() => {
  const v = (process.env.EVENT_REACTIONS || "major").toLowerCase();
  return v === "true" ? "all" : v === "false" ? "off" : v;
})();
// Fight back automatically when a hostile mob hits the bot.
const AUTO_DEFEND      = (process.env.AUTO_DEFEND || "true").toLowerCase() !== "false";
const DEFEND_RADIUS    = 5;   // blocks: look at / fight hostiles this close
const THREAT_RADIUS    = 10;  // blocks: announce hostiles this close
// While a skill runs, auto-defend only interrupts it at or below this health.
const DEFEND_INTERRUPT_HEALTH = 12;
// The player Lumi plays near. Empty: the first other player online.
const MC_OWNER         = (process.env.MC_OWNER || "").trim();
// Where the house site is remembered between runs.
const HOUSE_PATH       = process.env.HOUSE_PATH
                       || path.join(__dirname, "../../../data/minecraft/house.json");
// First-person 3D view of what the bot sees (prismarine-viewer). 0 disables.
const VIEWER_PORT      = parseInt(process.env.VIEWER_PORT || "3000", 10);
const VIEWER_DISTANCE  = parseInt(process.env.VIEWER_DISTANCE || "6", 10);
// Serves the bot's skin PNG for CustomSkinLoader. 0 disables.
const SKIN_PORT        = parseInt(process.env.SKIN_PORT || "3001", 10);
const SKIN_PATH        = process.env.SKIN_PATH || path.join(__dirname, "lumi_skin.png");

// ─── Live viewer (optional) ────────────────────────────────────────────────

// Loaded once at startup. If it's missing or broken the bot runs without it.
let startViewerFor = null;
try {
  startViewerFor = require("prismarine-viewer").mineflayer;
} catch (e) {
  console.warn(`[viewer] live view disabled (${e.message.split("\n")[0]}). ` +
               "Run npm install in sdk/examples/minecraft to enable it.");
}
let viewerRunning = false;

// prismarine-viewer doesn't handle listen errors, so a port clash would
// crash the bot. Check first, binding the same way it does (all interfaces).
function portFree(port) {
  return new Promise((resolve) => {
    const probe = net.createServer()
      .once("error", () => resolve(false))
      .once("listening", () => probe.close(() => resolve(true)))
      .listen(port);
  });
}

async function startViewer(b) {
  if (!VIEWER_PORT || !startViewerFor || viewerRunning) return;
  if (!(await portFree(VIEWER_PORT))) {
    console.warn(`[viewer] port ${VIEWER_PORT} is in use — live view disabled (set VIEWER_PORT)`);
    return;
  }
  if (b !== bot) return;  // replaced by a reconnect while we checked
  try {
    startViewerFor(b, { port: VIEWER_PORT, firstPerson: true, viewDistance: VIEWER_DISTANCE });
    viewerRunning = true;
    console.log(`[viewer] live view on http://localhost:${VIEWER_PORT}`);
    _logEv("viewer", `live view on :${VIEWER_PORT}`);
  } catch (e) {
    console.warn(`[viewer] failed to start: ${e.message}`);
  }
}

function stopViewer(b) {
  if (!viewerRunning || !b || !b.viewer || !b.viewer.close) return;
  try { b.viewer.close(); } catch (_) {}
  viewerRunning = false;
}

// ─── Skin server ───────────────────────────────────────────────────────────
// A vanilla LAN server can't receive a skin from an offline-mode bot, so the
// skin is applied client-side by the CustomSkinLoader mod, which can load it
// from this URL. See README "Lumi's skin".

function startSkinServer() {
  if (!SKIN_PORT) return;
  const server = http.createServer((req, res) => {
    const url = (req.url || "").split("?")[0].toLowerCase();
    const known = url === "/lumi_skin.png" || url === `/skins/${MC_USERNAME.toLowerCase()}.png`;
    if (req.method !== "GET" || !known) {
      res.writeHead(404);
      res.end();
      return;
    }
    fs.readFile(SKIN_PATH, (err, data) => {
      if (err) {
        res.writeHead(404);
        res.end();
        return;
      }
      res.writeHead(200, { "Content-Type": "image/png", "Cache-Control": "no-cache",
                           "Access-Control-Allow-Origin": "*" });
      res.end(data);
    });
  });
  server.on("error", (e) => console.warn(`[skin] skin server not started on :${SKIN_PORT} (${e.code || e.message})`));
  server.listen(SKIN_PORT, "127.0.0.1", () =>
    console.log(`[skin] serving ${path.basename(SKIN_PATH)} on http://127.0.0.1:${SKIN_PORT}/skins/${MC_USERNAME}.png`));
}

// ─── Voyager control primitives (plugin/voyager submodule) ─────────────────

const VOYAGER_DIR      = process.env.VOYAGER_DIR
                       || path.join(__dirname, "../../../plugin/voyager/voyager");
const SKILL_TIMEOUT_MS = parseInt(process.env.SKILL_TIMEOUT_MS || "120000", 10);

let voyagerInject   = null;
let voyagerPrograms = "";
try {
  voyagerInject = require(path.join(VOYAGER_DIR, "env/mineflayer/lib/skillLoader.js")).inject;
  const primDir = path.join(VOYAGER_DIR, "control_primitives");
  const files   = fs.readdirSync(primDir).filter(f => f.endsWith(".js")).sort();
  voyagerPrograms = files.map(f => fs.readFileSync(path.join(primDir, f), "utf8")).join("\n");
  console.log(`[voyager] loaded ${files.length} control primitives`);
} catch (e) {
  console.warn(`[voyager] primitives not loaded (${e.message}). ` +
               "Run: git submodule update --init plugin/voyager");
}

// ─── Token ─────────────────────────────────────────────────────────────────

function loadToken() {
  const direct = (process.env.LUMI_SDK_TOKEN || "").trim();
  if (direct) return direct;
  try {
    return fs.readFileSync(TOKEN_PATH, "utf8").trim();
  } catch (e) {
    throw new Error(`Cannot read SDK token from ${TOKEN_PATH}: ${e.message}`);
  }
}

// ─── Mineflayer bot ─────────────────────────────────────────────────────────

let bot      = null;
let botReady = false;
let mcData   = null;   // minecraft-data for the connected server's version

// Survival/event state, reset on each new bot.
let lastHealth    = null;
let lastFood      = null;
let lastHurtAt    = 0;
let lastAttacker  = null;  // name of the hostile nearest when we last took damage
let activeActions = 0;     // actions currently running (skills, SDK/ctrl calls)
let defendTarget  = null;
let lastFoughtId  = null;  // last entity we fought, for the kill announcement
const announcedThreats = new Set();

function createBot() {
  if (bot) {
    stopViewer(bot);
    try { bot.end("reconnecting"); } catch (_) {}
    bot      = null;
    botReady = false;
  }

  console.log(`[mc] connecting to ${MC_HOST}:${MC_PORT} as ${MC_USERNAME}`);
  const opts = { host: MC_HOST, port: MC_PORT, username: MC_USERNAME, auth: MC_AUTH };
  if (MC_VERSION) opts.version = MC_VERSION;
  bot = mineflayer.createBot(opts);
  bot.loadPlugin(pathfinder);
  bot.loadPlugin(toolPlugin);
  bot.loadPlugin(collectBlock);
  bot.loadPlugin(pvpPlugin);
  // Adds bot.save / bot.inventoryUsed and the chatEvent hook the primitives use.
  if (voyagerInject) voyagerInject(bot);

  lastHealth = lastFood = defendTarget = lastAttacker = lastFoughtId = null;
  lastHurtAt = 0;
  announcedThreats.clear();

  bot.once("spawn", () => {
    mcData = require("minecraft-data")(bot.version);
    configurePathfinder();
    botReady   = true;
    lastHealth = bot.health;
    lastFood   = bot.food;
    console.log("[mc] bot spawned and ready");
    startViewer(bot);
    pushBotContext();
    pushEvent("spawn", `I just joined the world. Health ${fmtHp(bot.health)}/20, ` +
                       `hunger ${bot.food}/20, carrying: ${inventorySummary()}.`);
  });

  bot.on("death", () => {
    botReady = false;
    defendTarget = null;
    console.log("[mc] bot died");
    pushBotContext();
    const cause = lastAttacker && Date.now() - lastHurtAt < 10_000 ? ` A ${lastAttacker} got me.` : "";
    pushEvent("death", `I just died!${cause} I dropped everything I was carrying.`, true);
  });

  bot.on("respawn", () => {
    mcData = require("minecraft-data")(bot.version);
    configurePathfinder();
    botReady   = true;
    lastHealth = bot.health;
    lastFood   = bot.food;
    announcedThreats.clear();
    console.log("[mc] bot respawned");
    pushBotContext();
  });

  // Fires on any health or food change.
  bot.on("health", () => {
    if (!botReady) return;
    const hp = bot.health, food = bot.food;
    if (lastHealth !== null && hp < lastHealth - 0.01 && hp > 0) {
      lastHurtAt = Date.now();
      const t = nearestHostile(DEFEND_RADIUS + 3);
      lastAttacker = t ? mobName(t.entity) : null;
      const who = t ? ` A ${lastAttacker} is ${t.dist.toFixed(0)} blocks away.` : "";
      const urgency = hp <= 6 ? " I'm about to die!" : "";
      pushEvent("hurt", `I just took damage! Health is ${fmtHp(hp)}/20.${who}${urgency}`);
      maybeAutoDefend();
    }
    if (lastFood !== null && food <= 6 && lastFood > 6) {
      pushEvent("hunger", `I'm getting really hungry (hunger ${food}/20).` +
                          (pickFood() ? "" : " And I have no food on me."));
    }
    lastHealth = hp;
    lastFood   = food;
  });

  bot.on("entityHurt", (entity) => {
    if (entity === bot.entity) maybeAutoDefend();
  });

  bot.on("entityDead", (entity) => {
    const name = mobName(entity);
    const near = entity.position && bot.entity && bot.entity.position.distanceTo(entity.position) < 32;
    if (BOSSES.has(name) && near) {
      pushEvent("boss", entity.id === lastFoughtId
        ? `I just killed the ${name.replace(/_/g, " ")}! A boss!`
        : `The ${name.replace(/_/g, " ")} just died right next to me!`, true);
    } else if (entity.id === lastFoughtId) {
      pushEvent("kill", `I killed the ${name} that was after me.`);
    }
    if (entity.id === lastFoughtId) {
      lastFoughtId = null;
      defendTarget = null;
    }
  });

  bot.on("playerCollect", (collector, collected) => {
    if (collector !== bot.entity) return;
    let item = null;
    try { item = collected.getDroppedItem && collected.getDroppedItem(); } catch (_) {}
    if (!item) return;
    _logEv("collect", `${item.name}×${item.count}`);
    if (RARE_ITEMS.has(item.name)) {
      pushEvent(`collect:${item.name}`, `I just picked up ${item.count} ${item.name.replace(/_/g, " ")}!`,
                MAJOR_LOOT.has(item.name));
    }
  });

  bot.on("stoppedAttacking", () => { defendTarget = null; });

  bot.on("chat", (username, message) => {
    if (username === bot.username) return;
    // "!lumi ...", "Lumi, ..." or "Lumi ..." from the owner is an order for
    // the game agent. Anything else is ordinary chat for Lumi's context.
    const order = username === ownerName() ? stripWakeWord(message) : null;
    if (order !== null) {
      const r = setDirective(order, "chat", username);
      bot.chat(r.success ? `Got it. ${r.directive.text}.` : "Lumi here. What should I do?");
      return;
    }
    pushBotContext(`[chat] ${username}: ${message}`);
    // Only a short command or one that names the bot counts as a call, so
    // "I'll come back later" in conversation doesn't summon it.
    const addressed = /lumi/i.test(message) || message.trim().split(/\s+/).length <= 4;
    if (username === ownerName() && addressed && CALL_RE.test(message)) {
      ownerCalledAt = Date.now();
      _logEv("owner", `${username} called: ${message}`);
    }
  });

  bot.on("error", (err) => console.error("[mc] error:", err.code || err.message || err));

  const thisBot = bot;
  bot.on("end", (reason) => {
    // Ignore ends from a bot already replaced by a /config reconnect.
    if (bot !== thisBot) return;
    botReady = false;
    stopViewer(thisBot);
    console.log(`[mc] disconnected from ${MC_HOST}:${MC_PORT}:`, reason);
    setTimeout(() => { if (bot === thisBot) createBot(); }, 10000);
  });
}

// ─── World helpers ──────────────────────────────────────────────────────────

const HOSTILE_MOBS = new Set([
  "zombie", "skeleton", "creeper", "spider", "enderman", "witch",
  "slime", "cave_spider", "blaze", "ghast", "zombie_pigman",
  "wither_skeleton", "phantom", "drowned", "husk", "stray", "pillager",
  "vindicator", "ravager", "hoglin", "zoglin", "piglin_brute",
  "zombie_villager", "silverfish", "evoker", "vex", "magma_cube", "warden",
]);

// Pickups worth bragging about.
const RARE_ITEMS = new Set([
  "diamond", "diamond_ore", "deepslate_diamond_ore", "emerald", "raw_gold",
  "gold_ingot", "ancient_debris", "netherite_scrap", "enchanted_golden_apple",
  "golden_apple", "ender_pearl", "lapis_lazuli", "name_tag", "saddle",
  "netherite_ingot", "totem_of_undying", "elytra", "nether_star", "dragon_egg",
]);
// The subset Lumi reacts to out loud even with EVENT_REACTIONS=major.
const MAJOR_LOOT = new Set([
  "diamond", "emerald", "ancient_debris", "netherite_scrap", "netherite_ingot",
  "enchanted_golden_apple", "totem_of_undying", "elytra", "nether_star", "dragon_egg",
]);
const BOSSES = new Set(["ender_dragon", "wither", "elder_guardian", "warden"]);

const WEAPONS = [
  "netherite_sword", "diamond_sword", "iron_sword", "stone_sword", "golden_sword", "wooden_sword",
  "netherite_axe", "diamond_axe", "iron_axe", "stone_axe", "golden_axe", "wooden_axe",
];

const fmtHp = (v) => (v == null ? "?" : (Math.round(v * 2) / 2).toString());
const mobName = (e) => (e.name || e.mobType || e.displayName || "mob").toLowerCase().replace(/\s+/g, "_");

function inventorySummary(limit = 12) {
  return bot.inventory.items().slice(0, limit).map(i => `${i.name}×${i.count}`).join(", ") || "nothing";
}

// Nearest mob matching `filter` (a mob id) or, with no filter, the nearest hostile.
function nearestMob(maxDist, filter = null) {
  if (!bot || !bot.entity) return null;
  let best = null;
  for (const e of Object.values(bot.entities)) {
    if (e === bot.entity || !e.position) continue;
    // mineflayer reports "mob" on older data and hostile/animal/passive/... on newer.
    if (["player", "object", "orb", "global", "projectile", "other"].includes(e.type)) continue;
    const name = mobName(e);
    if (filter ? name !== filter : !HOSTILE_MOBS.has(name)) continue;
    const dist = bot.entity.position.distanceTo(e.position);
    if (dist <= maxDist && (!best || dist < best.dist)) best = { entity: e, dist };
  }
  return best;
}
const nearestHostile = (maxDist) => nearestMob(maxDist);

function nearbyThreats(maxDist = 16) {
  if (!bot || !bot.entity) return [];
  const out = [];
  for (const e of Object.values(bot.entities)) {
    if (e === bot.entity || !e.position || e.type === "player" || !HOSTILE_MOBS.has(mobName(e))) continue;
    const dist = bot.entity.position.distanceTo(e.position);
    if (dist <= maxDist) out.push({ name: mobName(e), distance: +dist.toFixed(1) });
  }
  return out.sort((a, b) => a.distance - b.distance);
}

// Best food in the inventory by hunger restored.
function pickFood(preferred = null) {
  if (!mcData) return null;
  const foods = mcData.foodsByName || {};
  const avoid = new Set(["rotten_flesh", "spider_eye", "poisonous_potato", "pufferfish", "chorus_fruit", "chicken"]);
  let best = null;
  for (const item of bot.inventory.items()) {
    if (preferred ? item.name !== preferred : (!foods[item.name] || avoid.has(item.name))) continue;
    const pts = foods[item.name] ? foods[item.name].foodPoints : 1;
    if (!best || pts > best.pts) best = { item, pts };
  }
  return best ? best.item : null;
}

async function equipWeapon() {
  for (const name of WEAPONS) {
    const item = bot.inventory.items().find(i => i.name === name);
    if (item) { try { await bot.equip(item, "hand"); } catch (_) {} return name; }
  }
  return null;
}

// canDig, allow1by1towers and maxDropDown 4 are already
// mineflayer-pathfinder's defaults; they're set here so the bot's
// abilities are explicit. What actually caused "Took to long to decide
// path to goal!" is the 5 s planning budget, too short for paths through
// forests and up to logs in tree canopies.
const PATH_THINK_MS = parseInt(process.env.PATH_THINK_MS || "12000", 10);

function configurePathfinder() {
  const moves = new Movements(bot, mcData);
  moves.canDig = true;            // dig through walls, including its own dirt hut
  moves.allow1by1towers = true;   // pillar up with scaffolding blocks to reach logs
  moves.allowParkour = true;
  moves.allowSprinting = true;
  moves.maxDropDown = 4;
  bot.pathfinder.setMovements(moves);
  bot.pathfinder.thinkTimeout = PATH_THINK_MS;
}

// Stop everything a skill may have started. A timed-out skill's code keeps
// running in the background, so without this it fights the next skill for
// the pathfinder ("Digging aborted", "Took to long to decide path").
function stopAllActivity() {
  try { bot.pathfinder.stop(); } catch (_) {}
  try { bot.pathfinder.setGoal(null); } catch (_) {}
  try { bot.pvp.stop(); } catch (_) {}
  try { bot.collectBlock.cancelTask(); } catch (_) {}
  try { if (bot.targetDigBlock) bot.stopDigging(); } catch (_) {}
  try { bot.clearControlStates(); } catch (_) {}
}

function nearbyBlock(name, maxDistance = 32) {
  const b = mcData && mcData.blocksByName[name];
  return Boolean(b && bot.findBlock({ matching: b.id, maxDistance }));
}

// ─── Orders (directives) for the game agent ─────────────────────────────────
// The owner gives orders in chat with a wake word, or the host from the web
// UI's Games page (via POST /directive). bot.js only stores the latest one
// and interrupts whatever skill is running; plugin/minecraft_bot.py takes it
// with GET /directive?take=1 and decides what to do.

const WAKE_RE = /^\s*(?:!lumi\b[\s,:]*|lumi\s*[,:!]\s*|lumi\s+)(.*)$/i;
const MAX_DIRECTIVE = 200;
let pendingDirective = null;   // { text, source, from, at }
let interruptSeq = 0;          // bumped by each order; running skills watch it

// The order after the wake word, "" for a bare "lumi", or null when the
// message isn't addressed to the bot.
function stripWakeWord(message) {
  if (/^\s*!?lumi\s*[!.?]?\s*$/i.test(message)) return "";
  const m = WAKE_RE.exec(message);
  return m ? m[1].trim() : null;
}

function cleanDirective(text) {
  // Printable text only, one line, bounded.
  return String(text ?? "").replace(/[\u0000-\u001f\u007f]+/g, " ").replace(/\s+/g, " ")
                           .trim().slice(0, MAX_DIRECTIVE);
}

function setDirective(text, source, from = null) {
  const clean = cleanDirective(text).replace(/[.!]+$/, "");
  if (!clean) return { success: false, message: "Empty order" };
  pendingDirective = { text: clean, source, from, at: Date.now() / 1000 };
  _logEv("directive", `${from || source}: ${clean}`);
  console.log(`[directive] ${from || source}: ${clean}`);
  interruptCurrent();
  return { success: true, message: `Order received: ${clean}`, directive: pendingDirective };
}

// Abort the running skill so a new order takes effect now. A fight in
// progress is left alone: survival comes first.
function interruptCurrent() {
  interruptSeq++;
  if (!defendTarget) stopAllActivity();
}

// ─── The player Lumi plays near ─────────────────────────────────────────────

// "come here", "follow me", "lumi come back", ... in chat from the owner.
const CALL_RE = /\b(come( here| back)?|follow( me)?|over here|where are you|get over here)\b/i;
let ownerCalledAt = 0;
let lastOwnerLook = 0;
let lastOwnerPos  = null;

function ownerName() {
  if (!bot || !bot.players) return null;
  if (MC_OWNER) return bot.players[MC_OWNER] ? MC_OWNER : null;
  return Object.keys(bot.players).find(n => n !== bot.username) || null;
}

// distance is null when the owner is online but too far away for the
// server to send their position.
function ownerInfo() {
  const name = ownerName();
  if (!name) return null;
  const ent = bot.players[name] && bot.players[name].entity;
  const called = Date.now() - ownerCalledAt < 60_000;
  if (!ent) return { name, distance: null, position: null, called };
  const p = ent.position;
  return {
    name,
    distance: +bot.entity.position.distanceTo(p).toFixed(1),
    position: { x: +p.x.toFixed(1), y: +p.y.toFixed(1), z: +p.z.toFixed(1) },
    called,
  };
}

// While idle, glance at the owner when they move, like checking in.
function lookAtOwnerSometimes() {
  if (activeActions > 0 || defendTarget || bot.pathfinder.isMoving()) return;
  const name = ownerName();
  const ent = name && bot.players[name] && bot.players[name].entity;
  if (!ent || bot.entity.position.distanceTo(ent.position) > 16) return;
  const moved = !lastOwnerPos || lastOwnerPos.distanceTo(ent.position) > 2;
  if (!moved && Date.now() - lastOwnerLook < 8000) return;
  lastOwnerPos = ent.position.clone();
  lastOwnerLook = Date.now();
  bot.lookAt(ent.position.offset(0, ent.height ?? 1.62, 0)).catch(() => {});
}

function buildStatusText() {
  if (!bot || !botReady) return "[MinecraftBot] Not connected to Minecraft server.";
  const pos     = bot.entity.position;
  const items   = bot.inventory.items().map(i => `${i.name}×${i.count}`).join(", ") || "empty";
  const threats = nearbyThreats().slice(0, 5).map(t => `${t.name} (${t.distance} blocks)`).join(", ") || "none";
  const held    = bot.heldItem ? bot.heldItem.name : "nothing";
  const tod     = bot.time && bot.time.isDay === false ? "night" : "day";
  return [
    `[MinecraftBot] ${bot.username} on ${MC_HOST}:${MC_PORT}`,
    `Position: ${pos.x.toFixed(1)}, ${pos.y.toFixed(1)}, ${pos.z.toFixed(1)}  Time: ${tod}`,
    `Health: ${fmtHp(bot.health)}/20  Hunger: ${(bot.food ?? "?")}/20  Holding: ${held}`,
    `Nearby threats: ${threats}`,
    `Inventory: ${items}`,
  ].join("\n");
}

function buildStatusJson() {
  if (!bot || !botReady) return { ready: false, username: MC_USERNAME };
  const pos = bot.entity.position;
  return {
    ready:     true,
    username:  bot.username,
    server:    `${MC_HOST}:${MC_PORT}`,
    position:  { x: +pos.x.toFixed(2), y: +pos.y.toFixed(2), z: +pos.z.toFixed(2) },
    health:    bot.health   ?? null,
    food:      bot.food     ?? null,
    inventory: bot.inventory.items().map(i => ({ name: i.name, count: i.count })),
    threats:   nearbyThreats(),
    is_day:    bot.time ? bot.time.isDay !== false : true,
    busy:      activeActions > 0,
    defending: Boolean(defendTarget),
    held:      bot.heldItem ? bot.heldItem.name : null,
    nearby:    { crafting_table: nearbyBlock("crafting_table"), furnace: nearbyBlock("furnace"),
                 chest: nearbyBlock("chest", 16) },
    free_slots: bot.inventory.emptySlotCount(),
    owner:     ownerInfo(),
    house:     house ? { origin: house.origin, material: house.material, complete: !!house.complete } : null,
    directive_pending: Boolean(pendingDirective),
  };
}

// ─── Event log (read by mc_dashboard.py via /events) ────────────────────────

const _evLog = [];
function _logEv(category, message) {
  const e = { ts: Date.now() / 1000, time: new Date().toTimeString().slice(0, 8), category, message };
  _evLog.push(e);
  if (_evLog.length > 300) _evLog.shift();
}

// ─── Lumi SDK WebSocket ─────────────────────────────────────────────────────

let ws             = null;
let stopping       = false;
let reconnectTimer = null;
let sdkOffline     = false;

function sendJson(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
}

function pushBotContext(extra = null) {
  let text = buildStatusText();
  if (extra) text += "\n" + extra;
  sendJson({ command: "context", game: GAME_NAME, data: { message: text, silent: true } });
}

// Per-kind cooldowns so a fight doesn't become a wall of reactions. Lumi's
// SDK server adds its own per-client cooldown on top.
const EVENT_COOLDOWN_MS = { hurt: 20_000, threat: 25_000, hunger: 120_000, collect: 15_000, kill: 10_000 };
const _lastEventAt = {};

// silent:false makes Lumi take a turn and react out loud. Only `major`
// events are sent unless EVENT_REACTIONS=all; the rest are just logged.
function pushEvent(kind, text, major = false) {
  _logEv("event", text);
  if (EVENT_REACTIONS === "off" || (EVENT_REACTIONS !== "all" && !major)) return;
  const now = Date.now();
  const cooldown = EVENT_COOLDOWN_MS[kind.split(":")[0]] ?? 0;
  if (now - (_lastEventAt[kind] || 0) < cooldown) return;
  _lastEventAt[kind] = now;
  sendJson({ command: "context", game: GAME_NAME,
             data: { message: `[MINECRAFT EVENT] ${text}`, silent: false } });
}

// ─── Self-defense ───────────────────────────────────────────────────────────

function startDefend(entity) {
  if (!entity || (defendTarget && defendTarget.id === entity.id)) return;
  defendTarget = entity;
  lastFoughtId = entity.id;
  _logEv("defend", `fighting ${mobName(entity)}`);
  equipWeapon().finally(() => {
    if (defendTarget === entity) bot.pvp.attack(entity);
  });
}

// Called when the bot is hurt. Fights back if a hostile is in range, but
// only interrupts a running skill when health is getting low.
function maybeAutoDefend() {
  if (!AUTO_DEFEND || !botReady || defendTarget) return;
  const t = nearestHostile(DEFEND_RADIUS);
  if (!t) return;
  if (activeActions > 0 && bot.health > DEFEND_INTERRUPT_HEALTH) return;
  startDefend(t.entity);
}

// Once a second: announce new hostiles and keep an eye on close ones.
function threatTick() {
  if (!bot || !botReady) return;
  for (const id of announcedThreats) if (!bot.entities[id]) announcedThreats.delete(id);

  const t = nearestHostile(THREAT_RADIUS);
  if (!t) { lookAtOwnerSometimes(); return; }
  if (!announcedThreats.has(t.entity.id)) {
    announcedThreats.add(t.entity.id);
    const name = mobName(t.entity);
    const warn = name === "creeper" ? " It's going to blow up!" : "";
    pushEvent("threat", `A ${name} is ${t.dist.toFixed(0)} blocks away from me.${warn}`);
  }
  if (t.dist > DEFEND_RADIUS || defendTarget || activeActions > 0) {
    if (t.dist > DEFEND_RADIUS) lookAtOwnerSometimes();
    return;
  }
  // Idle: face the threat, and fight it if it has hit us recently.
  // Looking at an enderman provokes it, so leave those alone.
  const e = t.entity;
  if (mobName(e) === "enderman" && Date.now() - lastHurtAt > 5_000) return;
  bot.lookAt(e.position.offset(0, e.height ?? 1.6, 0)).catch(() => {});
  if (AUTO_DEFEND && Date.now() - lastHurtAt < 5_000) startDefend(e);
}
setInterval(threatTick, 1000);

function registerActions() {
  sendJson({
    command: "actions/register",
    game: GAME_NAME,
    data: {
      actions: [
        {
          name: "minecraft_get_status",
          description: "Return your Minecraft character's health, hunger, position, held item, nearby hostile mobs, and inventory.",
          schema: { type: "object", properties: {} },
          capabilities: ["read"],
          requires_confirm: false,
        },
        {
          name: "minecraft_chat",
          description: "Send a chat message in Minecraft as the bot.",
          schema: {
            type: "object",
            properties: {
              message: { type: "string", description: "The text to send in chat." },
            },
            required: ["message"],
          },
          capabilities: ["write"],
          requires_confirm: false,
        },
        {
          name: "minecraft_move_to",
          description: "Navigate the bot to a specific x,y,z coordinate using pathfinding.",
          schema: {
            type: "object",
            properties: {
              x:     { type: "number",  description: "Target X coordinate." },
              y:     { type: "number",  description: "Target Y coordinate." },
              z:     { type: "number",  description: "Target Z coordinate." },
              range: { type: "number",  description: "Stop within this many blocks of the target. Default: 1.", default: 1 },
            },
            required: ["x", "y", "z"],
          },
          capabilities: ["write"],
          requires_confirm: false,
        },
        {
          name: "minecraft_look_at",
          description: "Make the bot look at the given x,y,z block position.",
          schema: {
            type: "object",
            properties: {
              x: { type: "number", description: "Target X coordinate." },
              y: { type: "number", description: "Target Y coordinate." },
              z: { type: "number", description: "Target Z coordinate." },
            },
            required: ["x", "y", "z"],
          },
          capabilities: ["write"],
          requires_confirm: false,
        },
        {
          name: "minecraft_attack_nearest",
          description: "Attack the nearest hostile mob. Optionally filter by mob type name (e.g. 'zombie', 'skeleton').",
          schema: {
            type: "object",
            properties: {
              mob_type: { type: "string", description: "Optional mob type filter, e.g. 'zombie'. Attacks nearest hostile if omitted." },
            },
          },
          capabilities: ["write"],
          requires_confirm: false,
        },
        {
          name: "minecraft_stop",
          description: "Stop the bot's current pathfinding or movement immediately.",
          schema: { type: "object", properties: {} },
          capabilities: ["write"],
          requires_confirm: false,
        },
        {
          name: "minecraft_dig",
          description: "Break/dig the block at integer x,y,z coordinates.",
          schema: {
            type: "object",
            properties: {
              x: { type: "integer", description: "Block X coordinate." },
              y: { type: "integer", description: "Block Y coordinate." },
              z: { type: "integer", description: "Block Z coordinate." },
            },
            required: ["x", "y", "z"],
          },
          capabilities: ["write"],
          requires_confirm: true,
        },
        {
          name: "minecraft_execute_skill",
          description: "Run Voyager-style async JavaScript on the Minecraft bot. In scope: bot, mcData, Vec3, Goal* classes, and Voyager primitives mineBlock(bot,name,count), craftItem(bot,name,count), smeltItem(bot,item,fuel,count), placeItem(bot,name,pos), killMob(bot,name,timeout), exploreUntil(bot,dir,maxTime,callback).",
          schema: {
            type: "object",
            properties: {
              code:        { type: "string", description: "Async JavaScript body, e.g. `await mineBlock(bot, \"oak_log\", 3);`" },
              description: { type: "string", description: "Short label for the skill (logged, not executed)." },
              timeout_ms:  { type: "integer", description: "Optional time limit in ms (5000-120000). Default 120000." },
            },
            required: ["code"],
          },
          capabilities: ["dangerous"],
          requires_confirm: true,
        },
        {
          name: "minecraft_mine_block",
          description: "Have the Minecraft bot find, walk to, and mine blocks of one type, collecting the drops.",
          schema: {
            type: "object",
            properties: {
              name:  { type: "string",  description: "Block id, e.g. oak_log, stone, iron_ore." },
              count: { type: "integer", description: "How many blocks to mine (1-64). Default 1." },
            },
            required: ["name"],
          },
          capabilities: ["write"],
          requires_confirm: false,
        },
        {
          name: "minecraft_craft_item",
          description: "Have the Minecraft bot craft an item from its inventory. 3x3 recipes need a crafting table within 32 blocks.",
          schema: {
            type: "object",
            properties: {
              name:  { type: "string",  description: "Item id, e.g. oak_planks, stick, wooden_pickaxe." },
              count: { type: "integer", description: "How many times to run the recipe (1-64). Default 1." },
            },
            required: ["name"],
          },
          capabilities: ["write"],
          requires_confirm: false,
        },
        {
          name: "minecraft_smelt_item",
          description: "Have the Minecraft bot smelt an item in a furnace within 32 blocks.",
          schema: {
            type: "object",
            properties: {
              name:  { type: "string",  description: "Item to smelt, e.g. raw_iron." },
              fuel:  { type: "string",  description: "Fuel item id. Default coal." },
              count: { type: "integer", description: "How many to smelt (1-64). Default 1." },
            },
            required: ["name"],
          },
          capabilities: ["write"],
          requires_confirm: false,
        },
        {
          name: "minecraft_place_item",
          description: "Have the Minecraft bot place a block from its inventory, next to itself unless coordinates are given.",
          schema: {
            type: "object",
            properties: {
              name: { type: "string", description: "Item id, e.g. crafting_table, furnace." },
              x:    { type: "integer", description: "Optional target X." },
              y:    { type: "integer", description: "Optional target Y." },
              z:    { type: "integer", description: "Optional target Z." },
            },
            required: ["name"],
          },
          capabilities: ["write"],
          requires_confirm: false,
        },
        {
          name: "minecraft_kill_mob",
          description: "Have the Minecraft bot hunt and kill the nearest mob of a type, then collect its drops.",
          schema: {
            type: "object",
            properties: {
              name: { type: "string", description: "Mob id, e.g. zombie, cow, pig." },
            },
            required: ["name"],
          },
          capabilities: ["write"],
          requires_confirm: false,
        },
        {
          name: "minecraft_defend",
          description: "Fight off hostile mobs within 8 blocks of the Minecraft bot until none are left (up to 30 s). Equips the best weapon first.",
          schema: { type: "object", properties: {} },
          capabilities: ["write"],
          requires_confirm: false,
        },
        {
          name: "minecraft_attack_entity",
          description: "Chase and fight one mob until it dies (up to 30 s). Targets the nearest hostile unless a mob id is given.",
          schema: {
            type: "object",
            properties: {
              name: { type: "string", description: "Optional mob id, e.g. zombie, cow. Nearest hostile if omitted." },
            },
          },
          capabilities: ["write"],
          requires_confirm: false,
        },
        {
          name: "minecraft_eat_food",
          description: "Eat food from the Minecraft bot's inventory. Picks the most filling food unless one is named.",
          schema: {
            type: "object",
            properties: {
              name: { type: "string", description: "Optional food item id, e.g. bread, cooked_beef." },
            },
          },
          capabilities: ["write"],
          requires_confirm: false,
        },
        {
          name: "minecraft_place_block",
          description: "Place a block from the Minecraft bot's inventory at x,y,z (or beside the bot) using plain mineflayer placement.",
          schema: {
            type: "object",
            properties: {
              name: { type: "string",  description: "Block item id, e.g. dirt, cobblestone, torch." },
              x:    { type: "integer", description: "Optional target X." },
              y:    { type: "integer", description: "Optional target Y." },
              z:    { type: "integer", description: "Optional target Z." },
            },
            required: ["name"],
          },
          capabilities: ["write"],
          requires_confirm: false,
        },
        {
          name: "minecraft_return_to_player",
          description: "Walk back to the player you're playing near (the owner). If stuck for 30 s, tries /tp (needs cheats).",
          schema: {
            type: "object",
            properties: {
              range: { type: "integer", description: "Stop within this many blocks. Default 4." },
            },
          },
          capabilities: ["write"],
          requires_confirm: false,
        },
        {
          name: "minecraft_mine_branch_tunnel",
          description: "Dig a safe staircase down to a depth, then a 1x2 tunnel, mining any ores seen and placing torches every 5 blocks. Resumable: run again to continue.",
          schema: {
            type: "object",
            properties: {
              depth:  { type: "integer", description: "Target Y level. Default 12 (use -54 for diamonds in 1.18+)." },
              length: { type: "integer", description: "Tunnel length in blocks (1-64). Default 20." },
            },
          },
          capabilities: ["write"],
          requires_confirm: false,
        },
        {
          name: "minecraft_build_house",
          description: "Build a 7x7 house (floor, 3-high walls, door, windows, flat roof, torches) next to the bot. Resumable. Fails with the missing materials if there aren't enough.",
          schema: {
            type: "object",
            properties: {
              material: { type: "string", description: "cobblestone or planks. Default: whichever there's more of." },
            },
          },
          capabilities: ["write"],
          requires_confirm: false,
        },
        {
          name: "minecraft_craft_chest",
          description: "Craft chests from planks (or logs), placing a crafting table if needed.",
          schema: { type: "object", properties: { count: { type: "integer", description: "How many (1-8). Default 1." } } },
          capabilities: ["write"],
          requires_confirm: false,
        },
        {
          name: "minecraft_place_chest",
          description: "Place a chest from the inventory, next to the bot unless coordinates are given.",
          schema: {
            type: "object",
            properties: {
              x: { type: "integer" }, y: { type: "integer" }, z: { type: "integer" },
            },
          },
          capabilities: ["write"],
          requires_confirm: false,
        },
        {
          name: "minecraft_deposit_items",
          description: "Put items into a chest (at x,y,z, or the nearest one within 16 blocks).",
          schema: {
            type: "object",
            properties: {
              item_name: { type: "string", description: "Item id, e.g. cobblestone." },
              count:     { type: "integer", description: "How many. Default: all of that item." },
              x: { type: "integer" }, y: { type: "integer" }, z: { type: "integer" },
            },
            required: ["item_name"],
          },
          capabilities: ["write"],
          requires_confirm: false,
        },
        {
          name: "minecraft_sort_inventory",
          description: "Put excess bulk items (stone, dirt, extra wood, junk) into the nearest chest, keeping tools, food, torches and a working stock.",
          schema: {
            type: "object",
            properties: {
              place_if_missing: { type: "boolean", description: "Craft and place a chest if none is within 16 blocks." },
            },
          },
          capabilities: ["write"],
          requires_confirm: false,
        },
        {
          name: "minecraft_drop_item",
          description: "Drop (toss) items from the Minecraft bot's inventory on the ground.",
          schema: {
            type: "object",
            properties: {
              name:  { type: "string",  description: "Item id, e.g. dirt, rotten_flesh." },
              count: { type: "integer", description: "How many to drop. Default: all of that item." },
            },
            required: ["name"],
          },
          capabilities: ["write"],
          requires_confirm: false,
        },
      ],
    },
  });
}

// ─── Action handlers ────────────────────────────────────────────────────────

async function handleGetStatus(_args) {
  return { success: true, message: buildStatusText() };
}

async function handleChat(args) {
  const msg = String(args.message || "").trim();
  if (!msg) return { success: false, message: "message cannot be empty" };
  if (!botReady) return { success: false, message: "bot is not connected to Minecraft" };
  bot.chat(msg);
  return { success: true, message: `Sent: ${msg}` };
}

async function handleMoveTo(args) {
  if (!botReady) return { success: false, message: "bot is not connected to Minecraft" };
  const x = Number(args.x), y = Number(args.y), z = Number(args.z);
  const range = Number(args.range ?? 1);
  if ([x, y, z, range].some(isNaN)) return { success: false, message: "invalid coordinates" };

  try {
    await bot.pathfinder.goto(new goals.GoalNear(x, y, z, range));
    const p = bot.entity.position;
    return { success: true, message: `Reached (${p.x.toFixed(1)}, ${p.y.toFixed(1)}, ${p.z.toFixed(1)})` };
  } catch (e) {
    return { success: false, message: `Pathfinding failed: ${e.message}` };
  }
}

async function handleLookAt(args) {
  if (!botReady) return { success: false, message: "bot is not connected to Minecraft" };
  const x = Number(args.x), y = Number(args.y), z = Number(args.z);
  if ([x, y, z].some(isNaN)) return { success: false, message: "invalid coordinates" };
  await bot.lookAt(new Vec3(x, y, z));
  return { success: true, message: `Looking at (${x}, ${y}, ${z})` };
}

async function handleAttackNearest(args) {
  if (!botReady) return { success: false, message: "bot is not connected to Minecraft" };
  const filter = args.mob_type ? String(args.mob_type).toLowerCase() : null;
  const t = nearestMob(Infinity, filter);
  if (!t) return { success: false, message: `No ${filter || "hostile mob"} found nearby` };
  bot.attack(t.entity);
  return { success: true, message: `Attacking ${mobName(t.entity)} at distance ${t.dist.toFixed(1)}` };
}

// Fight one entity with mineflayer-pvp until it dies, leaves, or time runs out.
async function fightUntilDead(entity, timeoutMs = 30_000) {
  const name = mobName(entity);
  startDefend(entity);
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    await new Promise(r => setTimeout(r, 250));
    if (!botReady) return { success: false, message: `Died fighting the ${name}` };
    if (!bot.entities[entity.id] || entity.isValid === false) {
      return { success: true, message: `Killed the ${name}. Health ${fmtHp(bot.health)}/20` };
    }
    if (!defendTarget) startDefend(entity);  // pvp gave up (lost sight); re-engage
  }
  try { bot.pvp.stop(); } catch (_) {}
  defendTarget = null;
  return { success: false, message: `Gave up on the ${name} after ${timeoutMs / 1000} s` };
}

async function handleAttackEntity(args) {
  if (!botReady) return { success: false, message: "bot is not connected to Minecraft" };
  const filter = args.name ? String(args.name).toLowerCase() : null;
  if (filter && badId(filter)) return { success: false, message: "name must be a mob id like zombie" };
  const t = nearestMob(32, filter);
  if (!t) return { success: false, message: `No ${filter || "hostile mob"} within 32 blocks` };
  return fightUntilDead(t.entity);
}

async function handleDefend(_args) {
  if (!botReady) return { success: false, message: "bot is not connected to Minecraft" };
  const start = Date.now(), killed = [];
  while (Date.now() - start < 30_000) {
    const t = nearestHostile(8);
    if (!t) break;
    const r = await fightUntilDead(t.entity, 30_000 - (Date.now() - start));
    if (!r.success) return { success: false, message: [...killed.map(k => `killed ${k}`), r.message].join(", ") };
    killed.push(mobName(t.entity));
  }
  if (!killed.length) {
    const far = nearbyThreats()[0];
    return { success: true, message: far ? `No hostiles within 8 blocks (nearest: ${far.name} at ${far.distance})`
                                         : "No hostile mobs nearby" };
  }
  return { success: true, message: `Defended: killed ${killed.join(", ")}. Health ${fmtHp(bot.health)}/20` };
}

async function handleEatFood(args) {
  if (!botReady) return { success: false, message: "bot is not connected to Minecraft" };
  const wanted = args.name ? String(args.name) : null;
  if (wanted && badId(wanted)) return { success: false, message: "name must be a food id like bread" };
  if (bot.food >= 20 && wanted !== "golden_apple") return { success: true, message: "Not hungry (hunger 20/20)" };
  const food = pickFood(wanted);
  if (!food) return { success: false, message: wanted ? `No ${wanted} in inventory` : "No food in inventory" };
  try {
    await bot.equip(food, "hand");
    await bot.consume();
    return { success: true, message: `Ate ${food.name}. Hunger ${bot.food}/20, health ${fmtHp(bot.health)}/20` };
  } catch (e) {
    return { success: false, message: `Eating failed: ${e.message}` };
  }
}

const FACES = [[0, -1, 0], [0, 1, 0], [1, 0, 0], [-1, 0, 0], [0, 0, 1], [0, 0, -1]].map(f => new Vec3(...f));

// Plain mineflayer placement: find a solid neighbour of the target cell and
// place against it. Used directly and as the fallback for placeItem.
async function rawPlaceBlock(name, pos) {
  const item = bot.inventory.items().find(i => i.name === name);
  if (!item) return { success: false, message: `No ${name} in inventory` };
  const target = pos ? pos.floored() : bot.entity.position.floored().offset(1, 0, 0);
  const here = bot.blockAt(target);
  if (here && here.boundingBox !== "empty") {
    return { success: false, message: `(${target.x}, ${target.y}, ${target.z}) is occupied by ${here.name}` };
  }
  for (const f of FACES) {
    const ref = bot.blockAt(target.plus(f));
    if (!ref || ref.boundingBox !== "block") continue;
    try {
      if (bot.entity.position.distanceTo(target) > 4) {
        await bot.pathfinder.goto(new goals.GoalNear(target.x, target.y, target.z, 3));
      }
      await bot.equip(item, "hand");
      await bot.placeBlock(ref, f.scaled(-1));
      return { success: true, message: `Placed ${name} at (${target.x}, ${target.y}, ${target.z})` };
    } catch (e) {
      return { success: false, message: `Place failed: ${e.message}` };
    }
  }
  return { success: false, message: `Nothing solid next to (${target.x}, ${target.y}, ${target.z}) to place against` };
}

async function handlePlaceBlock(a) {
  if (!botReady) return { success: false, message: "bot is not connected to Minecraft" };
  if (badId(a.name)) return { success: false, message: "name must be a block id like dirt" };
  const xyz = [a.x, a.y, a.z].map(Number);
  return rawPlaceBlock(a.name, xyz.every(Number.isFinite) ? new Vec3(...xyz) : null);
}

async function handleDropItem(a) {
  if (!botReady) return { success: false, message: "bot is not connected to Minecraft" };
  if (badId(a.name)) return { success: false, message: "name must be an item id like dirt" };
  const have = bot.inventory.items().filter(i => i.name === a.name).reduce((s, i) => s + i.count, 0);
  if (!have) return { success: false, message: `No ${a.name} in inventory` };
  const n = a.count == null ? have : Math.min(Math.max(parseInt(a.count, 10) || 1, 1), have);
  try {
    await bot.toss(mcData.itemsByName[a.name].id, null, n);
    return { success: true, message: `Dropped ${n} ${a.name}` };
  } catch (e) {
    return { success: false, message: `Drop failed: ${e.message}` };
  }
}

// ─── Shared helpers for the plain-mineflayer skills ─────────────────────────

const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const countWhere = (pred) => bot.inventory.items().filter(i => pred(i.name)).reduce((s, i) => s + i.count, 0);
const countItem = (name) => countWhere(n => n === name);
const LOG_RE = /_log$/;
const plankTotal = () => countWhere(n => n.endsWith("_planks"));

// An empty cell on solid ground next to the bot.
function freeSpotNear() {
  const base = bot.entity.position.floored();
  for (const [dx, dz] of [[1,0],[-1,0],[0,1],[0,-1],[1,1],[-1,-1],[1,-1],[-1,1],[2,0],[0,2],[-2,0],[0,-2]]) {
    const p = base.offset(dx, 0, dz);
    const here = bot.blockAt(p), below = bot.blockAt(p.offset(0, -1, 0));
    if (here && here.boundingBox === "empty" && below && below.boundingBox === "block") return p;
  }
  return null;
}

// Walk within reach of a cell. False if the path failed.
async function reach(v, dist = 4) {
  if (bot.entity.position.distanceTo(v.offset(0.5, 0.5, 0.5)) <= dist + 0.3) return true;
  try {
    await bot.pathfinder.goto(new goals.GoalNear(v.x, v.y, v.z, Math.max(1, dist - 1)));
    return true;
  } catch (_) {
    return false;
  }
}

// Dig one cell with the best tool, retrying for falling gravel and sand.
async function digCell(v) {
  for (let i = 0; i < 6; i++) {
    const b = bot.blockAt(v);
    if (!b || b.boundingBox === "empty") return;
    if (b.name === "bedrock") throw new Error("hit bedrock");
    try { await bot.tool.equipForBlock(b, {}); } catch (_) {}
    await bot.dig(b, true);
    await sleep(150);   // let anything above fall in
  }
}

// Planks from whichever logs there are most of; 2x2, no table needed.
async function ensurePlanksRaw(n) {
  while (plankTotal() < n) {
    const log = bot.inventory.items().filter(i => LOG_RE.test(i.name)).sort((a, b) => b.count - a.count)[0];
    if (!log) return false;
    const r = await rawCraftItem(log.name.replace(LOG_RE, "_planks"), Math.min(log.count, Math.ceil((n - plankTotal()) / 4)));
    if (!r.success) return false;
  }
  return true;
}

async function ensureCraftingTable() {
  if (nearbyBlock("crafting_table")) return true;
  if (!countItem("crafting_table")) {
    if (!(await ensurePlanksRaw(4))) return false;
    if (!(await rawCraftItem("crafting_table", 1)).success) return false;
  }
  const spot = freeSpotNear();
  return Boolean(spot && (await rawPlaceBlock("crafting_table", spot)).success);
}

// Movements that never dig: while building, a path must not tunnel through
// the walls just placed.
function withoutDigging() {
  const moves = new Movements(bot, mcData);
  moves.canDig = false;
  moves.allow1by1towers = false;   // no scaffold pillars inside the house
  bot.pathfinder.setMovements(moves);
}

// ─── Return to the player ───────────────────────────────────────────────────

const STUCK_MS = 30_000;

async function handleReturnToPlayer(a) {
  if (!botReady) return { success: false, message: "bot is not connected to Minecraft" };
  const name = ownerName();
  if (!name) return { success: false, message: "No other player is online" };
  const range = Math.min(Math.max(parseInt(a.range ?? 4, 10) || 4, 2), 10);
  const deadline = Date.now() + 110_000;
  let best = Infinity, lastProgress = Date.now(), lastPlan = 0, triedTeleport = false;
  const ownerEntity = () => bot.players[name] && bot.players[name].entity;

  // Last resort: /tp works if cheats are on for the LAN world.
  const teleport = async () => {
    triedTeleport = true;
    stopAllActivity();
    bot.chat(`/tp ${bot.username} ${name}`);
    await sleep(2500);
    const ent = ownerEntity();
    return Boolean(ent && bot.entity.position.distanceTo(ent.position) <= range + 2);
  };

  const seq = interruptSeq;
  try {
    while (Date.now() < deadline) {
      if (interruptSeq !== seq) return { success: false, message: "interrupted by a new order" };
      const ent = ownerEntity();
      if (!ent) {
        // Out of tracking range: no position to walk to.
        if (!triedTeleport && (await teleport())) break;
        return { success: false, message: `${name} is too far away to see, and /tp didn't work (needs cheats on)` };
      }
      const d = bot.entity.position.distanceTo(ent.position);
      if (d <= range + 0.5) break;
      if (d < best - 1) { best = d; lastProgress = Date.now(); }
      // Re-plan every 10 s without progress; the pathfinder digs through
      // whatever is in the way (canDig is on).
      if (!bot.pathfinder.goal || (Date.now() - lastProgress > 10_000 && Date.now() - lastPlan > 10_000)) {
        bot.pathfinder.setGoal(new goals.GoalFollow(ent, range), true);
        lastPlan = Date.now();
      }
      if (Date.now() - lastProgress > STUCK_MS) {
        if (!triedTeleport && (await teleport())) break;
        return { success: false, message: `Stuck ${Math.round(d)} blocks from ${name} for ${STUCK_MS / 1000} s` };
      }
      await sleep(500);
    }
  } finally {
    stopAllActivity();
  }
  const ent = ownerEntity();
  if (!ent) return { success: false, message: `Lost track of ${name}` };
  const d = bot.entity.position.distanceTo(ent.position);
  if (d > range + 2) return { success: false, message: `Still ${Math.round(d)} blocks from ${name}` };
  ownerCalledAt = 0;
  bot.lookAt(ent.position.offset(0, ent.height ?? 1.62, 0)).catch(() => {});
  return { success: true, message: `Back next to ${name}${triedTeleport ? " (teleported)" : ""}` };
}

// ─── Branch mining ──────────────────────────────────────────────────────────

const ORE_NAMES = ["coal_ore", "iron_ore", "gold_ore", "diamond_ore", "lapis_ore", "redstone_ore",
                   "emerald_ore", "copper_ore"].flatMap(n => [n, `deepslate_${n}`]);
const FLUIDS = new Set(["lava", "water"]);
const SIDES = [[1,0,0],[-1,0,0],[0,1,0],[0,-1,0],[0,0,1],[0,0,-1]].map(f => new Vec3(...f));
let branchDir = null;   // kept between runs so a tunnel continues straight

function fluidNear(cells) {
  for (const c of cells) for (const f of [new Vec3(0, 0, 0), ...SIDES]) {
    const b = bot.blockAt(c.plus(f));
    if (b && FLUIDS.has(b.name)) return b.name;
  }
  return null;
}

async function mineOresNear(radius, found) {
  const ids = ORE_NAMES.map(n => mcData.blocksByName[n]).filter(Boolean).map(b => b.id);
  for (const p of bot.findBlocks({ matching: ids, maxDistance: radius, count: 12 })) {
    const b = bot.blockAt(p);
    if (!b || fluidNear([p])) continue;
    try { await bot.tool.equipForBlock(b, {}); } catch (_) {}
    // Wrong tier (e.g. diamond with a stone pickaxe) would destroy the drop.
    if (!b.canHarvest(bot.heldItem ? bot.heldItem.type : null)) continue;
    if (!(await reach(p, 4))) continue;
    try {
      await bot.dig(b, true);
      found[b.name] = (found[b.name] || 0) + 1;
    } catch (_) {}
  }
}

// A solid floor under `v`, patching a hole with a scaffold block.
async function ensureFloor(v) {
  const below = bot.blockAt(v.offset(0, -1, 0));
  if (below && below.boundingBox === "block" && !FLUIDS.has(below.name)) return true;
  const filler = ["cobblestone", "cobbled_deepslate", "dirt", "netherrack"].find(countItem);
  return Boolean(filler && (await rawPlaceBlock(filler, v.offset(0, -1, 0))).success);
}

async function placeTorchBehind(prev) {
  if (!countItem("torch")) return false;
  return (await rawPlaceBlock("torch", prev)).success;
}

async function handleBranchMine(a) {
  if (!botReady) return { success: false, message: "bot is not connected to Minecraft" };
  if (!bot.inventory.items().some(i => i.name.endsWith("_pickaxe"))) {
    return { success: false, message: "Need a pickaxe to branch mine" };
  }
  const depth = Number.isFinite(+a.depth) ? Math.round(+a.depth) : 12;
  const length = Math.min(Math.max(parseInt(a.length ?? 20, 10) || 20, 1), 64);
  const deadline = Date.now() + Math.min(Math.max(parseInt(a.max_seconds ?? 105, 10) || 105, 10), 110) * 1000;
  const hurtAtStart = lastHurtAt;
  const seq = interruptSeq;
  const found = {};
  let steps = 0, tunnel = 0, stopped = null;

  // Keep going the same way if we're already down in a tunnel.
  if (!branchDir || bot.entity.position.y > depth + 1.5) {
    const yaw = bot.entity.yaw;
    const fx = -Math.sin(yaw), fz = -Math.cos(yaw);
    branchDir = Math.abs(fx) > Math.abs(fz) ? new Vec3(Math.sign(fx), 0, 0) : new Vec3(0, 0, Math.sign(fz));
  }
  const dir = branchDir;

  const step = async (down) => {
    const feet = bot.entity.position.floored();
    const next = feet.plus(dir).offset(0, down ? -1 : 0, 0);
    const cells = down ? [next.offset(0, 2, 0), next.offset(0, 1, 0), next]
                       : [next.offset(0, 1, 0), next];
    const fluid = fluidNear(cells);
    if (fluid) throw new Error(`${fluid} ahead`);
    for (const c of cells) await digCell(c);
    if (!(await ensureFloor(next))) throw new Error("a drop ahead and nothing to fill it with");
    await bot.pathfinder.goto(new goals.GoalBlock(next.x, next.y, next.z));
    steps++;
    if (steps % 5 === 0) await placeTorchBehind(feet);
    await mineOresNear(down ? 2 : 3, found);
  };

  try {
    while (bot.entity.position.y > depth + 0.5) {
      if (Date.now() > deadline) { stopped = "time limit"; break; }
      if (lastHurtAt !== hurtAtStart) { stopped = "something attacked me"; break; }
      if (interruptSeq !== seq) { stopped = "interrupted by a new order"; break; }
      await step(true);
    }
    while (!stopped && tunnel < length) {
      if (Date.now() > deadline) { stopped = "time limit"; break; }
      if (lastHurtAt !== hurtAtStart) { stopped = "something attacked me"; break; }
      if (interruptSeq !== seq) { stopped = "interrupted by a new order"; break; }
      await step(false);
      tunnel++;
    }
  } catch (e) {
    stopped = e.message;
  } finally {
    stopAllActivity();
  }

  const ores = Object.entries(found).map(([n, c]) => `${n}×${c}`).join(", ") || "none";
  const y = Math.floor(bot.entity.position.y);
  const msg = `Branch mine at y=${y}: ${steps} blocks dug, ${tunnel} of ${length} tunnel. Ores: ${ores}`;
  if (stopped && steps === 0) return { success: false, message: `Couldn't dig: ${stopped}` };
  return { success: true, message: stopped ? `${msg}. Stopped: ${stopped}; run again to continue` : msg };
}

// ─── House building ─────────────────────────────────────────────────────────

let house = loadHouse();

function loadHouse() {
  try { return JSON.parse(fs.readFileSync(HOUSE_PATH, "utf8")); } catch (_) { return null; }
}
function saveHouse() {
  try {
    fs.mkdirSync(path.dirname(HOUSE_PATH), { recursive: true });
    fs.writeFileSync(HOUSE_PATH, JSON.stringify(house, null, 2));
  } catch (_) {}
}

const MATERIAL = {
  cobblestone: { label: "cobblestone", matches: n => n === "cobblestone" },
  planks:      { label: "planks",      matches: n => n.endsWith("_planks") },
};

function cellDone(cell, b, mat) {
  if (!b) return false;
  if (STRUCTURAL.has(cell.kind)) return MATERIAL[mat].matches(b.name);
  if (cell.kind === "air" || cell.kind === "door_top") return b.boundingBox === "empty" || b.name.endsWith("_door");
  if (cell.kind === "door") return b.name.endsWith("_door") || (b.boundingBox === "empty" && !countWhere(n => n.endsWith("_door")));
  if (cell.kind === "window") return b.name === "glass" || (b.boundingBox === "empty" && !countItem("glass"));
  if (cell.kind.startsWith("torch")) return b.name === "torch" || !countItem("torch");
  return true;
}

async function handleBuildHouse(a) {
  if (!botReady) return { success: false, message: "bot is not connected to Minecraft" };
  const wood = plankTotal() + countWhere(n => LOG_RE.test(n)) * 4;
  let mat = a.material === "cobblestone" ? "cobblestone"
          : (a.material === "planks" || a.material === "wood") ? "planks"
          : (house && !house.complete ? house.material
             : (countItem("cobblestone") >= wood ? "cobblestone" : "planks"));

  // Reuse an unfinished site nearby; otherwise start next to the bot.
  const far = house && bot.entity.position.distanceTo(new Vec3(...house.origin)) > 64;
  if (!house || far || (house.complete && a.new_site)) {
    const feet = bot.entity.position.floored();
    house = { origin: [feet.x + 2, feet.y - 1, feet.z - 3], material: mat, complete: false };
    saveHouse();
  }
  if (house.complete) {
    return { success: true, message: `House already built at (${house.origin.join(", ")})` };
  }
  mat = house.material;

  const [x0, y0, z0] = house.origin;
  const cells = houseBlueprint(x0, y0, z0);
  const todo = () => cells.filter(c => !cellDone(c, bot.blockAt(new Vec3(c.x, c.y, c.z)), mat));
  const needMat = todo().filter(c => STRUCTURAL.has(c.kind)).length;
  if (mat === "planks") await ensurePlanksRaw(needMat);
  const have = countWhere(MATERIAL[mat].matches);
  if (have < needMat) {
    return { success: false,
             message: `Missing materials: ${mat === "planks" ? "oak_planks" : mat}×${needMat - have} ` +
                      `(have ${have}, need ${needMat} of ${materialNeeded(cells)})` };
  }
  // A door if there's spare wood and none in the inventory.
  if (!countWhere(n => n.endsWith("_door")) && plankTotal() >= (mat === "planks" ? needMat + 6 : 6)) {
    const plank = bot.inventory.items().filter(i => i.name.endsWith("_planks")).sort((p, q) => q.count - p.count)[0];
    if (plank && (await ensureCraftingTable())) await rawCraftItem(plank.name.replace("_planks", "_door"), 1);
  }

  const deadline = Date.now() + 105_000;
  const blockItem = () => bot.inventory.items().filter(i => MATERIAL[mat].matches(i.name))
                                              .sort((p, q) => q.count - p.count)[0];
  const seq = interruptSeq;
  withoutDigging();
  try {
    for (const c of cells) {
      if (Date.now() > deadline || interruptSeq !== seq) break;
      let v = new Vec3(c.x, c.y, c.z);
      if (c.kind === "torch_outside") {
        // Highest air cell above solid ground, near the floor's height.
        for (let dy = 2; dy >= -2; dy--) {
          const p = new Vec3(c.x, y0 + dy, c.z);
          const here = bot.blockAt(p), below = bot.blockAt(p.offset(0, -1, 0));
          if (here && here.boundingBox === "empty" && below && below.boundingBox === "block") { v = p; break; }
        }
      }
      const b = bot.blockAt(v);
      if (!b || cellDone(c, b, mat)) continue;
      const feet = bot.entity.position.floored();
      if (v.equals(feet) || v.equals(feet.offset(0, 1, 0))) continue;   // standing there; next run
      if (!(await reach(v))) continue;
      try {
        if (b.boundingBox !== "empty" && !(c.kind === "window" && b.name === "glass")) await digCell(v);
        if (STRUCTURAL.has(c.kind)) {
          const item = blockItem();
          if (!item) break;
          await rawPlaceBlock(item.name, v);
        } else if (c.kind === "door") {
          const door = bot.inventory.items().find(i => i.name.endsWith("_door"));
          if (door) await rawPlaceBlock(door.name, v);
        } else if (c.kind === "window" && countItem("glass")) {
          await rawPlaceBlock("glass", v);
        } else if (c.kind.startsWith("torch") && countItem("torch")) {
          await rawPlaceBlock("torch", v);
        }
      } catch (_) {
        // A cell that won't place (mob in the way, no support yet) is
        // retried on the next run.
      }
    }
  } finally {
    configurePathfinder();
  }

  const left = todo().filter(c => c.kind !== "torch_outside");
  const structural = cells.filter(c => STRUCTURAL.has(c.kind)).length;
  const doneCount = structural - left.filter(c => STRUCTURAL.has(c.kind)).length;
  if (left.length === 0) {
    house.complete = true;
    saveHouse();
    return { success: true, message: `Built a ${mat} house at (${house.origin.join(", ")})` };
  }
  return { success: true,
           message: `House in progress: ${doneCount}/${structural} blocks placed; run again to continue` };
}

// ─── Chests ─────────────────────────────────────────────────────────────────

// What sort_inventory keeps: tools, weapons, armor, food, torches and
// anything not listed are kept in full. Bulk blocks keep a working stock
// (the agent still crafts and builds with them); the rest goes in the chest.
const KEEP_BULK = {
  cobblestone: 32, dirt: 32, cobbled_deepslate: 0, gravel: 0, sand: 0, andesite: 0,
  diorite: 0, granite: 0, tuff: 0, deepslate: 0, stone: 0, netherrack: 0, flint: 0,
  rotten_flesh: 0, bone: 0, string: 0, spider_eye: 0, wheat_seeds: 0, calcite: 0,
};
const KEEP_GROUPS = [
  { re: /_log$/, keep: 16 },
  { re: /_planks$/, keep: 32 },
  { re: /_sapling$|_leaves$/, keep: 0 },
];

let lastChestPos = null;

function nearestChest(maxDistance = 16) {
  const id = mcData.blocksByName.chest.id;
  return bot.findBlock({ matching: id, maxDistance });
}

async function handleCraftChest(a) {
  if (!botReady) return { success: false, message: "bot is not connected to Minecraft" };
  const n = Math.min(Math.max(parseInt(a.count ?? 1, 10) || 1, 1), 8);
  if (!(await ensurePlanksRaw(8 * n))) return { success: false, message: `Need ${8 * n} planks (or logs) for ${n} chest(s)` };
  if (!(await ensureCraftingTable())) return { success: false, message: "Couldn't get a crafting table nearby" };
  return rawCraftItem("chest", n);
}

async function handlePlaceChest(a) {
  if (!botReady) return { success: false, message: "bot is not connected to Minecraft" };
  if (!countItem("chest")) return { success: false, message: "No chest in inventory (craft one first)" };
  const xyz = [a.x, a.y, a.z].map(Number);
  const pos = xyz.every(Number.isFinite) ? new Vec3(...xyz) : freeSpotNear();
  if (!pos) return { success: false, message: "No free spot next to me" };
  const r = await rawPlaceBlock("chest", pos);
  if (r.success) lastChestPos = pos.floored();
  return r;
}

async function openChest(pos) {
  const block = pos ? bot.blockAt(pos) : nearestChest();
  if (!block || block.name !== "chest") throw new Error(pos ? `No chest at (${pos.x}, ${pos.y}, ${pos.z})` : "No chest within 16 blocks");
  if (!(await reach(block.position, 3))) throw new Error("Can't reach the chest");
  return { chest: await bot.openContainer(block), at: block.position };
}

async function handleDepositItems(a) {
  if (!botReady) return { success: false, message: "bot is not connected to Minecraft" };
  if (badId(a.item_name)) return { success: false, message: "item_name must be an item id like cobblestone" };
  const have = countItem(a.item_name);
  if (!have) return { success: false, message: `No ${a.item_name} in inventory` };
  const xyz = [a.x, a.y, a.z].map(Number);
  const n = a.count == null ? have : Math.min(Math.max(parseInt(a.count, 10) || 1, 1), have);
  let chest;
  try {
    ({ chest } = await openChest(xyz.every(Number.isFinite) ? new Vec3(...xyz) : null));
    await chest.deposit(mcData.itemsByName[a.item_name].id, null, n);
    return { success: true, message: `Stored ${n} ${a.item_name}` };
  } catch (e) {
    return { success: false, message: `Deposit failed: ${e.message}` };
  } finally {
    if (chest) chest.close();
  }
}

function excessOf(name, totals) {
  if (name in KEEP_BULK) return Math.max(0, totals[name] - KEEP_BULK[name]);
  const g = KEEP_GROUPS.find(g => g.re.test(name));
  return g ? null : 0;   // null: decided per group below
}

async function handleSortInventory(a) {
  if (!botReady) return { success: false, message: "bot is not connected to Minecraft" };
  if (!nearestChest() && a.place_if_missing) {
    if (!countItem("chest")) {
      const r = await handleCraftChest({ count: 1 });
      if (!r.success) return { success: false, message: `No chest nearby and couldn't craft one: ${r.message}` };
    }
    const r = await handlePlaceChest({});
    if (!r.success) return { success: false, message: `Couldn't place a chest: ${r.message}` };
  }
  const totals = {};
  for (const i of bot.inventory.items()) totals[i.name] = (totals[i.name] || 0) + i.count;
  const plan = {};
  for (const name of Object.keys(totals)) {
    const n = excessOf(name, totals);
    if (n) plan[name] = n;
  }
  for (const g of KEEP_GROUPS) {
    let keep = g.keep;
    for (const name of Object.keys(totals).filter(n => g.re.test(n)).sort((p, q) => totals[q] - totals[p])) {
      const k = Math.min(keep, totals[name]);
      keep -= k;
      if (totals[name] - k > 0) plan[name] = totals[name] - k;
    }
  }
  if (!Object.keys(plan).length) return { success: true, message: "Nothing to put away" };

  let chest, at;
  const stored = [];
  try {
    ({ chest, at } = await openChest(null));
    for (const [name, n] of Object.entries(plan)) {
      try {
        await chest.deposit(mcData.itemsByName[name].id, null, n);
        stored.push(`${name}×${n}`);
      } catch (e) {
        if (/full/i.test(e.message)) break;
      }
    }
  } catch (e) {
    return { success: false, message: `Sorting failed: ${e.message}` };
  } finally {
    if (chest) chest.close();
  }
  lastChestPos = at;
  return { success: stored.length > 0,
           message: stored.length ? `Put away ${stored.join(", ")} in the chest at (${at.x}, ${at.y}, ${at.z})`
                                  : "The chest is full" };
}

// ─── Raw mineflayer fallbacks for the Voyager wrappers ──────────────────────

async function rawMineBlock(name, n) {
  const block = mcData.blocksByName[name];
  if (!block) return { success: false, message: `Unknown block ${name}` };
  const found = bot.findBlocks({ matching: block.id, maxDistance: 32, count: n });
  if (!found.length) return { success: false, message: `No ${name} within 32 blocks, explore first` };
  try {
    await bot.collectBlock.collect(found.map(p => bot.blockAt(p)).filter(Boolean));
    return { success: true, message: `Mined ${found.length} ${name} (mineflayer fallback)` };
  } catch (e) {
    return { success: false, message: `Mining failed: ${e.message}` };
  }
}

async function rawCraftItem(name, n) {
  const item = mcData.itemsByName[name];
  if (!item) return { success: false, message: `Unknown item ${name}` };
  const tableId = mcData.blocksByName.crafting_table.id;
  const table   = bot.findBlock({ matching: tableId, maxDistance: 32 });
  const recipe  = bot.recipesFor(item.id, null, 1, table)[0];
  if (!recipe) {
    return { success: false, message: `Can't craft ${name}: missing materials${table ? "" : " or a crafting table nearby"}` };
  }
  try {
    if (recipe.requiresTable && table && bot.entity.position.distanceTo(table.position) > 3) {
      await bot.pathfinder.goto(new goals.GoalNear(table.position.x, table.position.y, table.position.z, 2));
    }
    await bot.craft(recipe, n, recipe.requiresTable ? table : null);
    return { success: true, message: `Crafted ${name}×${n} (mineflayer fallback)` };
  } catch (e) {
    return { success: false, message: `Crafting failed: ${e.message}` };
  }
}

async function rawKillMob(name) {
  const t = nearestMob(32, name);
  if (!t) return { success: false, message: `No ${name} within 32 blocks` };
  return fightUntilDead(t.entity, 60_000);
}

// Run the Voyager primitive; if the primitives aren't loaded or it errored
// (other than timing out), retry with plain mineflayer.
async function voyagerOr(code, label, fallback) {
  if (!botReady) return { success: false, message: "bot is not connected to Minecraft" };
  if (!voyagerPrograms) return fallback();
  const r = await runVoyager(code, label);
  if (r.success || /timed out/.test(r.message)) return r;
  console.log(`[voyager] ${label} failed, trying mineflayer fallback`);
  const fb = await fallback();
  return fb.success ? fb : { success: false, message: `${r.message} | fallback: ${fb.message}` };
}

async function handleStop(_args) {
  if (!botReady) return { success: false, message: "bot is not connected to Minecraft" };
  bot.pathfinder.setGoal(null);
  return { success: true, message: "Stopped movement" };
}

async function handleDig(args) {
  if (!botReady) return { success: false, message: "bot is not connected to Minecraft" };
  const x = Math.floor(Number(args.x));
  const y = Math.floor(Number(args.y));
  const z = Math.floor(Number(args.z));
  if ([x, y, z].some(isNaN)) return { success: false, message: "invalid coordinates" };

  const block = bot.blockAt(new Vec3(x, y, z));
  if (!block || block.name === "air") return { success: false, message: `No block at (${x}, ${y}, ${z})` };

  try {
    await bot.dig(block);
    return { success: true, message: `Dug ${block.name} at (${x}, ${y}, ${z})` };
  } catch (e) {
    return { success: false, message: `Dig failed: ${e.message}` };
  }
}

const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;

// Same names Voyager's env/mineflayer/index.js puts in scope for skill code.
const GOAL_NAMES = [
  "Goal", "GoalBlock", "GoalNear", "GoalXZ", "GoalNearXZ", "GoalY",
  "GoalGetToBlock", "GoalLookAtBlock", "GoalBreakBlock", "GoalCompositeAny",
  "GoalCompositeAll", "GoalInvert", "GoalFollow", "GoalPlaceBlock",
];
const FAIL_COUNTERS =
  "let _craftItemFailCount = 0, _killMobFailCount = 0, _mineBlockFailCount = 0, " +
  "_placeItemFailCount = 0, _smeltItemFailCount = 0;\n";

// Runs skill code the way Voyager does: primitives + code in one async body.
// The primitives report progress through bot.chat, so those lines become the
// result message Lumi (or minecraft_bot.py) sees.
async function runVoyager(code, label, timeoutMs = SKILL_TIMEOUT_MS) {
  if (!botReady) return { success: false, message: "bot is not connected to Minecraft" };
  if (!voyagerPrograms) return { success: false, message: "Voyager primitives not loaded" };

  const mcData = require("minecraft-data")(bot.version);
  mcData.blocksByName.lapis_lazuli_ore ??= mcData.blocksByName.lapis_ore;
  mcData.itemsByName.lapis_lazuli_ore  ??= mcData.itemsByName.lapis_ore;

  const chatLog = [];
  const onChat  = (who, msg) => { if (who === "bot") chatLog.push(msg); };
  bot.on("chatEvent", onChat);
  console.log(`[voyager] running ${label}`);
  _logEv("skill", `run ${label}`);

  let timer, watch;
  const seq = interruptSeq;
  try {
    const fn = new AsyncFunction(
      "bot", "mcData", "Vec3", "Movements", "goals", ...GOAL_NAMES,
      FAIL_COUNTERS + voyagerPrograms + "\n" + code,
    );
    const result = await Promise.race([
      fn(bot, mcData, Vec3, Movements, goals, ...GOAL_NAMES.map(n => goals[n])),
      new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error(`timed out after ${timeoutMs / 1000} s`)),
                           timeoutMs);
      }),
      new Promise((_, reject) => {
        watch = setInterval(() => {
          if (interruptSeq !== seq) reject(new Error("interrupted by a new order"));
        }, 250);
      }),
    ]);
    const parts = [result !== undefined ? String(result) : "Skill completed.", ...chatLog.slice(-8)];
    return { success: true, message: parts.join(" | ") };
  } catch (e) {
    stopAllActivity();
    const feedback = chatLog.length ? " | " + chatLog.slice(-8).join(" | ") : "";
    return { success: false, message: `Skill error: ${e.message}${feedback}` };
  } finally {
    clearTimeout(timer);
    clearInterval(watch);
    bot.removeListener("chatEvent", onChat);
  }
}

async function handleExecuteSkill(args) {
  const code = String(args.code || "").trim();
  if (!code) return { success: false, message: "code cannot be empty" };
  // Optional shorter limit, e.g. 60 s for chopping trees, so a stuck
  // pathfinder fails fast and the agent can pick something else.
  const t = parseInt(args.timeout_ms, 10);
  const timeoutMs = Number.isFinite(t) ? Math.min(Math.max(t, 5000), SKILL_TIMEOUT_MS) : SKILL_TIMEOUT_MS;
  return runVoyager(code, args.description || "custom skill", timeoutMs);
}

// Item/block/mob names are interpolated into skill code, so restrict them to
// Minecraft identifier characters.
const ID_RE = /^[a-z0-9_]+$/;
const badId = (v) => typeof v !== "string" || !ID_RE.test(v);
const count = (v, def = 1) => {
  const n = parseInt(v ?? def, 10);
  return Number.isNaN(n) ? def : Math.min(Math.max(n, 1), 64);
};

async function handleMineBlock(a) {
  if (badId(a.name)) return { success: false, message: "name must be a block id like oak_log" };
  const n = count(a.count);
  return voyagerOr(`await mineBlock(bot, "${a.name}", ${n});`, `mineBlock ${a.name}`,
                   () => rawMineBlock(a.name, n));
}

async function handleCraftItem(a) {
  if (badId(a.name)) return { success: false, message: "name must be an item id like wooden_pickaxe" };
  const n = count(a.count);
  return voyagerOr(`await craftItem(bot, "${a.name}", ${n});`, `craftItem ${a.name}`,
                   () => rawCraftItem(a.name, n));
}

async function handleSmeltItem(a) {
  const fuel = a.fuel || "coal";
  if (badId(a.name) || badId(fuel)) return { success: false, message: "name and fuel must be item ids" };
  // No plain-mineflayer smelting fallback: furnace windows need Voyager's primitive.
  return runVoyager(`await smeltItem(bot, "${a.name}", "${fuel}", ${count(a.count)});`, `smeltItem ${a.name}`);
}

async function handlePlaceItem(a) {
  if (badId(a.name)) return { success: false, message: "name must be an item id like crafting_table" };
  const xyz = [a.x, a.y, a.z].map(Number);
  const has = xyz.every(n => Number.isFinite(n));
  const pos = has ? `new Vec3(${xyz.map(Math.floor).join(", ")})` : "bot.entity.position.offset(1, 0, 0)";
  return voyagerOr(`await placeItem(bot, "${a.name}", ${pos});`, `placeItem ${a.name}`,
                   () => rawPlaceBlock(a.name, has ? new Vec3(...xyz) : null));
}

async function handleKillMob(a) {
  if (badId(a.name)) return { success: false, message: "name must be a mob id like zombie" };
  return voyagerOr(`await killMob(bot, "${a.name}", 60);`, `killMob ${a.name}`,
                   () => rawKillMob(a.name));
}

// ─── Dispatch ────────────────────────────────────────────────────────────────

const HANDLERS = {
  minecraft_get_status:    handleGetStatus,
  minecraft_chat:          handleChat,
  minecraft_move_to:       handleMoveTo,
  minecraft_look_at:       handleLookAt,
  minecraft_attack_nearest: handleAttackNearest,
  minecraft_stop:          handleStop,
  minecraft_dig:           handleDig,
  minecraft_execute_skill: handleExecuteSkill,
  minecraft_mine_block:    handleMineBlock,
  minecraft_craft_item:    handleCraftItem,
  minecraft_smelt_item:    handleSmeltItem,
  minecraft_place_item:    handlePlaceItem,
  minecraft_kill_mob:      handleKillMob,
  minecraft_defend:        handleDefend,
  minecraft_attack_entity: handleAttackEntity,
  minecraft_eat_food:      handleEatFood,
  minecraft_place_block:   handlePlaceBlock,
  minecraft_drop_item:     handleDropItem,
  minecraft_return_to_player:   handleReturnToPlayer,
  minecraft_mine_branch_tunnel: handleBranchMine,
  minecraft_build_house:        handleBuildHouse,
  minecraft_craft_chest:        handleCraftChest,
  minecraft_place_chest:        handlePlaceChest,
  minecraft_deposit_items:      handleDepositItems,
  minecraft_sort_inventory:     handleSortInventory,
};

// Every handler call goes through here so auto-defend knows when the bot
// is busy with something it shouldn't interrupt.
async function runHandler(handler, args) {
  activeActions++;
  try {
    return await handler(args);
  } finally {
    activeActions--;
  }
}

// ─── Local control API (127.0.0.1:BOT_CONTROL_PORT) ─────────────────────────
// Used by minecraft_bot.py for Voyager-style autonomous control.
//   GET  /status  → buildStatusJson()
//   POST /action  → { action: "chat"|"move_to"|…, args: {} }
//   GET  /events  → last 100 log entries

const _controlServer = http.createServer((req, res) => {
  const url = (req.url || "").split("?")[0];
  res.setHeader("Content-Type", "application/json");
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "GET, POST, OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type");

  // Browsers preflight JSON POSTs from the :5002 dashboard.
  if (req.method === "OPTIONS") {
    res.writeHead(204);
    res.end();

  } else if (req.method === "GET" && url === "/status") {
    res.writeHead(200);
    res.end(JSON.stringify(buildStatusJson()));

  } else if (req.method === "GET" && url === "/events") {
    res.writeHead(200);
    res.end(JSON.stringify(_evLog.slice(-100)));

  } else if (req.method === "GET" && url === "/config") {
    res.writeHead(200);
    res.end(JSON.stringify({ host: MC_HOST, port: MC_PORT, username: MC_USERNAME, auth: MC_AUTH,
                             viewer_port: viewerRunning ? VIEWER_PORT : null,
                             skin_port: SKIN_PORT || null }));

  } else if (req.method === "POST" && url === "/config") {
    let body = "";
    req.on("data", c => body += c);
    req.on("end", () => {
      let payload;
      try { payload = JSON.parse(body); } catch (_) {
        res.writeHead(400);
        res.end(JSON.stringify({ success: false, message: "bad JSON" }));
        return;
      }
      const newHost = String(payload.host || "").trim();
      const newPort = parseInt(payload.port, 10);
      if (!newHost || isNaN(newPort) || newPort < 1 || newPort > 65535) {
        res.writeHead(422);
        res.end(JSON.stringify({ success: false, message: "host and a valid port (1–65535) are required" }));
        return;
      }
      const changed = newHost !== MC_HOST || newPort !== MC_PORT;
      MC_HOST = newHost;
      MC_PORT = newPort;
      _logEv("config", `server changed to ${MC_HOST}:${MC_PORT}`);
      res.writeHead(200);
      res.end(JSON.stringify({ success: true, message: `server set to ${MC_HOST}:${MC_PORT}` }));
      if (changed) {
        // Reconnect the mineflayer bot with the new address.
        console.log(`[mc] server updated to ${MC_HOST}:${MC_PORT} — reconnecting`);
        createBot();
      }
    });

  } else if (req.method === "GET" && url === "/directive") {
    // ?take=1 hands the order over and clears it (the game agent).
    const take = /[?&]take=1\b/.test(req.url || "");
    const d = pendingDirective;
    if (take) pendingDirective = null;
    res.writeHead(200);
    res.end(JSON.stringify({ directive: d }));

  } else if (req.method === "POST" && url === "/directive") {
    let body = "";
    req.on("data", c => { body += c; if (body.length > 4096) req.destroy(); });
    req.on("end", () => {
      let payload;
      try { payload = JSON.parse(body); } catch (_) {
        res.writeHead(400);
        res.end(JSON.stringify({ success: false, message: "bad JSON" }));
        return;
      }
      if (typeof payload.text !== "string") {
        res.writeHead(422);
        res.end(JSON.stringify({ success: false, message: "text must be a string" }));
        return;
      }
      const source = ["web", "chat", "lumi"].includes(payload.source) ? payload.source : "web";
      const r = setDirective(payload.text, source, payload.from ? cleanDirective(payload.from).slice(0, 32) : null);
      res.writeHead(r.success ? 200 : 422);
      res.end(JSON.stringify({ success: r.success, message: r.message, directive: r.directive || null }));
    });

  } else if (req.method === "POST" && url === "/action") {
    let body = "";
    req.on("data", c => body += c);
    req.on("end", async () => {
      let payload;
      try { payload = JSON.parse(body); } catch (_) {
        res.writeHead(400);
        res.end(JSON.stringify({ success: false, message: "bad JSON" }));
        return;
      }
      const key     = "minecraft_" + (payload.action || "");
      const handler = HANDLERS[key];
      _logEv("relay", `ctrl → ${key}`);
      if (!handler) {
        res.writeHead(404);
        res.end(JSON.stringify({ success: false, message: `unknown action: ${payload.action}` }));
        return;
      }
      try {
        const result  = await runHandler(handler, payload.args || {});
        const success = typeof result === "object" && result !== null
          ? result.success !== false : Boolean(result);
        const message = typeof result === "object" && result !== null
          ? String(result.message || result.error || "") : String(result ?? "");
        _logEv("relay", `ctrl ← ${key}: ${success ? "ok" : "fail"} ${message.slice(0, 80)}`);
        res.writeHead(200);
        res.end(JSON.stringify({ success, message }));
      } catch (e) {
        _logEv("error", `ctrl ${key}: ${e.message}`);
        res.writeHead(500);
        res.end(JSON.stringify({ success: false, message: e.message }));
      }
    });

  } else {
    res.writeHead(404);
    res.end("{}");
  }
});

_controlServer.listen(BOT_CONTROL_PORT, "127.0.0.1", () =>
  console.log(`[ctrl] control API on 127.0.0.1:${BOT_CONTROL_PORT}`)
);

// ─── Dispatch ────────────────────────────────────────────────────────────────

async function dispatchAction(invId, name, rawData) {
  _logEv("sdk", `← action: ${name}`);
  let args;
  try {
    args = typeof rawData === "string" ? JSON.parse(rawData) : (rawData || {});
  } catch (_) {
    sendJson({ command: "action/result", game: GAME_NAME,
               data: { id: invId, success: false, message: "invalid JSON in arguments" } });
    return;
  }

  const handler = HANDLERS[name];
  if (!handler) {
    sendJson({ command: "action/result", game: GAME_NAME,
               data: { id: invId, success: false, message: `no handler for ${name}` } });
    return;
  }

  let result;
  try {
    result = await runHandler(handler, args);
  } catch (e) {
    result = { success: false, message: `handler error: ${e.message}` };
  }

  const success = typeof result === "object" && result !== null
    ? result.success !== false
    : Boolean(result);
  const message = typeof result === "object" && result !== null
    ? String(result.message || result.error || "")
    : String(result ?? "");

  _logEv(success ? "result" : "error", `→ ${name}: ${message.slice(0, 100)}`);
  sendJson({ command: "action/result", game: GAME_NAME,
             data: { id: invId, success, message } });

  if (name !== "minecraft_get_status") pushBotContext();
}

// ─── SDK connect loop ────────────────────────────────────────────────────────

function connect() {
  if (stopping) return;

  let token;
  try {
    token = loadToken();
  } catch (e) {
    console.error("[sdk]", e.message);
    process.exit(1);
  }

  const uri = `ws://${LUMI_HOST}:${LUMI_PORT}/ws`;
  if (!sdkOffline) console.log(`[sdk] connecting to ${uri}`);
  ws = new WebSocket(uri);

  ws.on("open", () => {
    sendJson({
      command: "hello",
      game: GAME_NAME,
      client_name: GAME_NAME,
      client_version: CLIENT_VERSION,
      auth_token: token,
    });
  });

  ws.on("message", (raw) => {
    let msg;
    try { msg = JSON.parse(raw.toString()); } catch (_) { return; }

    switch (msg.command) {
      case "startup": {
        const sess  = (msg.data || {}).session || {};
        const proto = (msg.data || {}).protocolVersion || "?";
        console.log(`[sdk] welcome — session=${sess.sessionId} protocol=${proto}`);
        _logEv("sdk", `connected session=${sess.sessionId} proto=${proto}`);
        sdkOffline = false;
        registerActions();
        pushBotContext();
        break;
      }
      case "actions/registered":
        console.log(`[sdk] registered: ${(msg.data || {}).action_name}`);
        _logEv("sdk", `registered: ${(msg.data || {}).action_name}`);
        break;
      case "action":
        dispatchAction((msg.data || {}).id, (msg.data || {}).name, (msg.data || {}).data);
        break;
      case "speech_finished":
        // Optionally relay Lumi's replies to in-game chat:
        // if (botReady && (msg.data || {}).isFinal && msg.data.message)
        //   bot.chat(`[Lumi] ${msg.data.message}`);
        break;
      case "ping":
        sendJson({ command: "pong" });
        break;
      case "error":
        console.error("[sdk] server error:", (msg.data || {}).message);
        break;
    }
  });

  ws.on("close", () => {
    ws = null;
    if (!stopping) {
      if (!sdkOffline) console.log(`[sdk] disconnected — reconnecting in ${RECONNECT_MS / 1000} s`);
      reconnectTimer = setTimeout(connect, RECONNECT_MS);
    }
  });

  ws.on("error", (err) => {
    if (err.code === "ECONNREFUSED") {
      if (!sdkOffline) {
        console.log("[sdk] Lumi is not running (127.0.0.1:5056) — the bot still works; " +
                    "retrying quietly every 5 s until Lumi starts");
        _logEv("sdk", "Lumi SDK offline — retrying quietly");
      }
      sdkOffline = true;
    } else {
      console.error("[sdk] ws error:", err.message);
    }
  });
}

// Push a fresh status snapshot every 30 s so Lumi stays up-to-date.
setInterval(() => { if (botReady) pushBotContext(); }, 30_000);

process.on("SIGINT", () => {
  stopping = true;
  clearTimeout(reconnectTimer);
  if (ws)  ws.close();
  if (bot) try { bot.end(); } catch (_) {}
  process.exit(0);
});

startSkinServer();
createBot();
connect();
