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
const os   = require("os");
const dgram = require("dgram");

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
const DATA_DIR         = path.dirname(HOUSE_PATH);
// Where the bot last died, for getting its items back.
const DEATH_PATH       = path.join(DATA_DIR, "death_location.json");
// The server picked on the Games page; overrides MC_HOST/MC_PORT at startup.
const SERVER_PATH      = path.join(DATA_DIR, "server.json");
// Listen for "Open to LAN" announcements on the local network.
const LAN_DISCOVERY    = (process.env.LAN_DISCOVERY || "true").toLowerCase() !== "false";
// Dropped items despawn after 5 minutes; give up a little before that.
const ITEM_DESPAWN_S   = 290;

try {
  const saved = JSON.parse(fs.readFileSync(SERVER_PATH, "utf8"));
  if (saved.host && Number.isInteger(saved.port)) {
    MC_HOST = saved.host;
    MC_PORT = saved.port;
    console.log(`[mc] using the server picked on the Games page: ${MC_HOST}:${MC_PORT}`);
  }
} catch (_) {}
// First-person 3D view of what the bot sees (prismarine-viewer). 0 disables.
const VIEWER_PORT      = parseInt(process.env.VIEWER_PORT || "3000", 10);
// Chunks around the bot sent to each viewer browser. Each chunk is
// serialized per browser, so this is the viewer's main memory cost.
const VIEWER_DISTANCE  = parseInt(process.env.VIEWER_DISTANCE || "4", 10);
// Pause the live viewer when the heap passes this (MB); see memoryWatch().
const VIEWER_HEAP_LIMIT_MB = parseInt(process.env.VIEWER_HEAP_LIMIT_MB || "1536", 10);
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

// prismarine-viewer serializes every chunk for every connected browser, and
// socket.io buffers what a slow or background tab hasn't read yet, with no
// limit. A run that ended in "JavaScript heap out of memory" after 14 minutes
// pointed here. The watchdog pauses the viewer above VIEWER_HEAP_LIMIT_MB
// (closing its sockets frees the buffers) and brings it back later. It also
// logs the heap every 5 minutes, so if memory keeps growing with the viewer
// off, the log shows the leak is elsewhere.
let viewerPausedUntil = 0;
let lastMemLog = 0;
function memoryWatch() {
  const heapMb = Math.round(process.memoryUsage().heapUsed / 1048576);
  if (Date.now() - lastMemLog > 300_000) {
    lastMemLog = Date.now();
    console.log(`[mem] heap ${heapMb} MB, live viewer ${viewerRunning ? "on" : "off"}`);
  }
  if (viewerRunning && heapMb > VIEWER_HEAP_LIMIT_MB) {
    console.warn(`[mem] heap ${heapMb} MB is over ${VIEWER_HEAP_LIMIT_MB} MB: pausing the live viewer for 10 min`);
    _logEv("error", `heap ${heapMb} MB: live viewer paused`);
    stopViewer(bot);
    viewerPausedUntil = Date.now() + 600_000;
  } else if (!viewerRunning && viewerPausedUntil && Date.now() > viewerPausedUntil) {
    if (heapMb < VIEWER_HEAP_LIMIT_MB / 2 && botReady) {
      viewerPausedUntil = 0;
      console.log(`[mem] heap ${heapMb} MB: restarting the live viewer`);
      startViewer(bot);
    } else {
      console.warn(`[mem] heap still ${heapMb} MB with the viewer off; keeping it paused`);
      viewerPausedUntil = Date.now() + 600_000;
    }
  }
}
setInterval(memoryWatch, 15_000);

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

  lastHealth = lastFood = defendTarget = lastFoughtId = null;
  lastHurtAt = 0;
  announcedThreats.clear();
  recentHit = lastDamage = attention = null;
  landing = { airborne: false, peak: 0, landedAt: 0, landedPeak: 0 };
  gestures.clear();
  lastSeen.clear();

  bot.once("spawn", () => {
    mcData = require("minecraft-data")(bot.version);
    configurePathfinder();
    botReady   = true;
    lastHealth = bot.health ?? null;
    lastFood   = bot.food ?? null;
    console.log("[mc] bot spawned and ready");
    startViewer(bot);
    pushBotContext();
    // Health and hunger arrive in their own packet, often just after spawn.
    const joined = () => pushEvent("spawn", `I just joined the world. Health ${fmtHp(bot.health)}/20, ` +
                                            `hunger ${bot.food}/20, carrying: ${inventorySummary()}.`);
    if (bot.food === undefined) bot.once("health", joined);
    else joined();
  });

  bot.on("death", () => {
    rememberDeath();
    endEmergency();
    botReady = false;
    defendTarget = null;
    console.log("[mc] bot died");
    pushBotContext();
    pushEvent("death", `I just died!${deathCause()} I dropped everything I was carrying.`, true);
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
    if (lastHealth !== null && hp < lastHealth - 0.01) {
      lastHurtAt = Date.now();
      const d = damageCause();
      lastDamage = { ...d, at: lastHurtAt };
      if (hp > 0) {
        const urgency = hp <= 6 ? " I'm about to die!" : "";
        // Being hit by a player is social, so it's worth a reaction even
        // with EVENT_REACTIONS=major.
        pushEvent(`hurt:${d.cause}`, `${describeDamage(d)} Health is ${fmtHp(hp)}/20.${urgency}`,
                  d.cause === "player");
        maybeAutoDefend(d);
      }
    }
    if (lastFood !== null && food <= 6 && lastFood > 6) {
      pushEvent("hunger", `I'm getting really hungry (hunger ${food}/20).` +
                          (pickFood() ? "" : " And I have no food on me."));
    }
    lastHealth = hp;
    lastFood   = food;
  });

  bot.on("entityHurt", (entity, source) => recordHit(entity, source));
  bot.on("entityCrouch", onPlayerCrouch);
  bot.on("entitySwingArm", onPlayerSwing);

  // Respawn point: confirmed by the server's "Respawn point set", and lost
  // when the bed is gone or blocked at respawn.
  bot.on("message", (msg) => {
    if (msg.translate === "block.minecraft.set_spawn" || /respawn point set/i.test(msg.toString())) {
      spawnHeardAt = Date.now();
    }
  });
  bot.on("spawnReset", () => {
    const mem = readBed();
    if (mem && mem.spawn) {
      saveBed({ ...mem, spawn: false });
      _logEv("bed", "respawn point lost: the bed is gone or blocked");
    }
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
  bot.on("physicsTick", onPhysicsTick);

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
const PATH_THINK_MS = parseInt(process.env.PATH_THINK_MS || "10000", 10);

// Movement rules for staying alive. The pathfinder already avoids lava and
// fire; on top of that: never plan a drop of more than 3 blocks (4+ hurts),
// don't treat any fall into water as safe, make water paths expensive (it
// still swims when that's the only way, which is better than no path), and
// keep off magma, berry bushes and powder snow.
function survivalMovements() {
  const moves = new Movements(bot, mcData);
  moves.canDig = true;            // dig out of holes and through walls
  moves.allow1by1towers = true;   // pillar up out of caves and to reach logs
  moves.allowParkour = true;
  moves.allowSprinting = true;
  moves.maxDropDown = 3;
  moves.infiniteLiquidDropdownDistance = false;
  moves.liquidCost = 40;
  for (const name of ["magma_block", "sweet_berry_bush", "powder_snow", "campfire", "soul_campfire",
                      "soul_fire", "wither_rose", "cactus"]) {
    const b = mcData.blocksByName[name];
    if (b) moves.blocksToAvoid.add(b.id);
  }
  return moves;
}

function configurePathfinder() {
  bot.pathfinder.setMovements(survivalMovements());
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
let interruptSeq = 0;          // bumped by each order or emergency; running skills watch it
let interruptReason = "a new order";

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
function interruptCurrent(reason = "a new order") {
  interruptSeq++;
  interruptReason = reason;
  if (!defendTarget) stopAllActivity();
}

// ─── The player Lumi plays near ─────────────────────────────────────────────

// "come here", "follow me", "lumi come back", ... in chat from the owner.
const CALL_RE = /\b(come( here| back)?|follow( me)?|over here|where are you|get over here)\b/i;
let ownerCalledAt = 0;

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

// ─── HUD data (the Games page's live inventory) ─────────────────────────────

// Armor points per piece, as the game's armor bar counts them.
const ARMOR_POINTS = {
  leather: [1, 3, 2, 1], golden: [2, 5, 3, 1], chainmail: [2, 5, 4, 1],
  iron: [2, 6, 5, 2], diamond: [3, 8, 6, 3], netherite: [3, 8, 6, 3],
};
const ARMOR_PIECES = ["helmet", "chestplate", "leggings", "boots"];   // window slots 5-8

function armorPoints() {
  let total = 0;
  for (let i = 0; i < 4; i++) {
    const item = bot.inventory.slots[5 + i];
    if (!item) continue;
    if (item.name === "turtle_helmet") { total += 2; continue; }
    const [material, piece] = item.name.split("_");
    if (piece === ARMOR_PIECES[i] && ARMOR_POINTS[material]) total += ARMOR_POINTS[material][i];
  }
  return total;
}

function slotJson(index, item) {
  if (!item) return null;
  const out = { slot: index, name: item.name, display_name: item.displayName, count: item.count };
  if (item.maxDurability) {
    out.max_durability = item.maxDurability;
    out.durability = item.maxDurability - (item.durabilityUsed || 0);
  }
  return out;
}

// 36 entries: 0-8 the hotbar (window slots 36-44), 9-35 the main
// inventory (window slots 9-35). null for an empty slot.
function inventorySlots() {
  const slots = [];
  for (let i = 0; i < 9; i++) slots.push(slotJson(i, bot.inventory.slots[36 + i]));
  for (let i = 9; i < 36; i++) slots.push(slotJson(i, bot.inventory.slots[i]));
  return slots;
}

function buildStatusJson() {
  if (!bot || !botReady) {
    return { ready: false, username: MC_USERNAME, server: `${MC_HOST}:${MC_PORT}`, auth: MC_AUTH,
             death: deathJson() };
  }
  const pos = bot.entity.position;
  return {
    ready:     true,
    username:  bot.username,
    server:    `${MC_HOST}:${MC_PORT}`,
    position:  { x: +pos.x.toFixed(2), y: +pos.y.toFixed(2), z: +pos.z.toFixed(2) },
    health:    bot.health   ?? null,
    food:      bot.food     ?? null,
    inventory: bot.inventory.items().map(i => ({ name: i.name, count: i.count })),
    // For the HUD. `inventory` above stays the flat list the agent reads.
    slots:     inventorySlots(),
    held_slot: bot.quickBarSlot ?? 0,
    armor:     armorPoints(),
    auth:      MC_AUTH,
    version:   bot.version,
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
    environment: environmentJson(),
    emergency: emergency ? emergency.kind : null,
    death:     deathJson(),
    bed:       bedJson(),
    sleeping:  Boolean(bot.isSleeping),
    can_sleep: canSleepNow(),
    heap_mb:   Math.round(process.memoryUsage().heapUsed / 1048576),
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
const EVENT_COOLDOWN_MS = { hurt: 20_000, threat: 30_000, hunger: 120_000, collect: 15_000, kill: 10_000,
                            social: 45_000 };
const _lastEventAt = {};
// On top of the cooldowns: at most 3 of the same kind in any 60 s, so a
// zombie that keeps wandering in and out doesn't get "not again" five times.
const EVENT_BURST = 3, EVENT_BURST_WINDOW_MS = 60_000;
const _recentEvents = {};

// silent:false makes Lumi take a turn and react out loud. Only `major`
// events are sent unless EVENT_REACTIONS=all; the rest are just logged.
function pushEvent(kind, text, major = false) {
  _logEv("event", text);
  if (EVENT_REACTIONS === "off" || (EVENT_REACTIONS !== "all" && !major)) return;
  const now = Date.now();
  const cooldown = EVENT_COOLDOWN_MS[kind.split(":")[0]] ?? 0;
  if (now - (_lastEventAt[kind] || 0) < cooldown) return;
  const burst = (_recentEvents[kind] || []).filter(t => now - t < EVENT_BURST_WINDOW_MS);
  if (burst.length >= EVENT_BURST) return;
  _recentEvents[kind] = [...burst, now];
  _lastEventAt[kind] = now;
  sendJson({ command: "context", game: GAME_NAME,
             data: { message: `[MINECRAFT EVENT] ${text}`, silent: false } });
}

// ─── Perception and reflexes ───────────────────────────────────────────────
// Adapted from AIRI's Minecraft integration (github.com/moeru-ai/airi,
// integrations/minecraft, MIT License, Copyright (c) 2024-PRESENT Neko
// Ayaka): the attacker, fall and damage-taken perception events, the
// punch/teabag rules, and the idle-gaze and auto-eat reflexes. None of it
// calls a model; it runs on mineflayer events and the 1 s/500 ms ticks.

// Who or what hurt her. On 1.20+ servers entityHurt carries the real
// source (the skeleton that shot, the player who hit) a few ticks before
// the health packet; older servers fall back to the nearest hostile.
const ATTACKER_RECENCY_MS = 600;
const FALL_RECENCY_MS = 400;
const FALL_VELOCITY = -0.45;      // blocks/tick: faster than a jump or a step down
let recentHit = null;             // { entity, at }
let landing = { airborne: false, peak: 0, landedAt: 0, landedPeak: 0 };
let lastDamage = null;            // { cause, name, at } for the death message

function recordHit(victim, source) {
  if (bot && victim && victim === bot.entity && source) recentHit = { entity: source, at: Date.now() };
}

// Called every physics tick: remembers how fast she was falling just
// before landing, because by the time the health packet arrives she's on
// the ground with no velocity left.
function trackFall(e) {
  const vy = e.velocity ? e.velocity.y : 0;
  if (!e.onGround) {
    if (!landing.airborne) { landing.airborne = true; landing.peak = 0; }
    if (vy < landing.peak) landing.peak = vy;
    return;
  }
  if (landing.airborne) { landing.landedAt = Date.now(); landing.landedPeak = landing.peak; }
  landing.airborne = false;
  landing.peak = 0;
}

function fellJustNow(now = Date.now()) {
  if (landing.airborne && landing.peak < FALL_VELOCITY) return true;
  return now - landing.landedAt <= FALL_RECENCY_MS && landing.landedPeak < FALL_VELOCITY;
}

const onFire = (e) => typeof (e.metadata && e.metadata[0]) === "number" && (e.metadata[0] & 0x01) !== 0;

// { cause, name, entity } for damage she just took. cause is one of lava,
// fire, drowning, fall, player, mob, projectile, explosion, starving,
// unknown.
function damageCause(now = Date.now()) {
  const e = bot.entity;
  if (e.isInLava) return { cause: "lava" };
  if (e.isInWater && (bot.oxygenLevel ?? 20) <= 0) return { cause: "drowning" };
  const hit = recentHit && now - recentHit.at <= ATTACKER_RECENCY_MS ? recentHit.entity : null;
  if (hit) {
    if (hit.type === "player") return { cause: "player", name: hit.username || hit.name, entity: hit };
    const n = mobName(hit);
    if (/creeper|tnt|explosion|fireball/.test(n)) return { cause: "explosion", name: n, entity: hit };
    if (/arrow|trident|snowball|potion/.test(n)) return { cause: "projectile", name: n };
    return { cause: "mob", name: n, entity: hit };
  }
  if (fellJustNow(now)) return { cause: "fall" };
  if (onFire(e)) return { cause: "fire" };
  if (bot.food === 0) return { cause: "starving" };
  const t = nearestHostile(DEFEND_RADIUS + 3);
  if (t) return { cause: "mob", name: mobName(t.entity), entity: t.entity, guessed: true };
  return { cause: "unknown" };
}

function describeDamage(d) {
  const owner = d.name && d.name === ownerName();
  switch (d.cause) {
    case "lava":       return "I'm burning in lava!";
    case "fire":       return "I'm on fire!";
    case "drowning":   return "I'm drowning!";
    case "fall":       return "I fell and hurt myself.";
    case "starving":   return "I'm starving, and it's costing me health.";
    case "player":     return `${d.name} just hit me!${owner ? " My own teammate." : ""}`;
    case "explosion":  return `Something blew up right next to me${d.name === "creeper" ? " (a creeper)" : ""}!`;
    case "projectile": return `Something shot me with a ${d.name.replace(/_/g, " ")}!`;
    case "mob":        return d.guessed ? `I just took damage! A ${d.name} is right next to me.`
                                        : `A ${d.name.replace(/_/g, " ")} hit me!`;
    default:           return "I just took damage!";
  }
}

function deathCause() {
  if (!lastDamage || Date.now() - lastDamage.at > 10_000) return "";
  switch (lastDamage.cause) {
    case "lava":      return " I burned in lava.";
    case "fire":      return " I burned to death.";
    case "drowning":  return " I drowned.";
    case "fall":      return " I fell to my death.";
    case "starving":  return " I starved.";
    case "player":    return ` ${lastDamage.name} killed me.`;
    case "explosion": return " Something blew me up.";
    case "mob": case "projectile":
      return lastDamage.name ? ` A ${lastDamage.name.replace(/_/g, " ")} got me.` : "";
    default:          return "";
  }
}

// Players around her: a sliding-window count of crouches and arm swings
// (AIRI's teabag and punch rules). Someone who just gestured at her gets
// her attention, so idle gaze looks at them first.
const SOCIAL_RANGE = 8;
const CROUCH_SPAM = { count: 3, windowMs: 1_500 };   // 3 crouches = 6 toggles
const SWING_SPAM  = { count: 5, windowMs: 2_000 };
const gestures = new Map();      // "crouch:Name" -> [timestamps]
let attention = null;            // { entity, at }

function nearPlayer(e, range) {
  return e && e.type === "player" && e !== bot.entity && e.position && bot.entity &&
         bot.entity.position.distanceTo(e.position) <= range;
}

function countGesture(key, { count, windowMs }, now = Date.now()) {
  const recent = (gestures.get(key) || []).filter(t => now - t < windowMs);
  recent.push(now);
  if (recent.length >= count) { gestures.delete(key); return true; }
  gestures.set(key, recent);
  return false;
}

function onPlayerCrouch(e) {
  if (!botReady || !nearPlayer(e, SOCIAL_RANGE)) return;
  attention = { entity: e, at: Date.now() };
  if (!countGesture(`crouch:${e.username}`, CROUCH_SPAM)) return;
  _logEv("social", `${e.username} is crouch-spamming`);
  pushEvent("social:teabag", `${e.username} is crouch-spamming at me.`, true);
  crouchBack(e);
}

function onPlayerSwing(e) {
  if (!botReady || !nearPlayer(e, 10)) return;
  attention = { entity: e, at: Date.now() };
  if (countGesture(`swing:${e.username}`, SWING_SPAM)) _logEv("social", `${e.username} is punching the air near me`);
}

// Crouch back at them, three times, when nothing else needs her body.
let crouching = false;
async function crouchBack(e) {
  if (crouching || activeActions > 0 || defendTarget || emergency) return;
  crouching = true;
  try {
    await bot.lookAt(e.position.offset(0, e.height ?? 1.62, 0), true).catch(() => {});
    for (let i = 0; i < 3; i++) {
      bot.setControlState("sneak", true);  await sleep(180);
      bot.setControlState("sneak", false); await sleep(180);
    }
  } finally {
    bot.setControlState("sneak", false);
    crouching = false;
  }
}

// While idle, look at whoever's around: someone who just crouched or
// swung at her first, then the closest player who's moving (the owner
// wins a tie). Like AIRI's idle gaze, she skips about a third of the
// glances, doesn't turn for tiny angles, and turns smoothly.
const GAZE_RANGE = 16;
const GAZE_COOLDOWN_MS = 2_000;
const GAZE_SKIP = 0.3;
const GAZE_DEADZONE = 0.07;      // radians
let lastGazeAt = 0;
const lastSeen = new Map();      // username -> position at the last gaze tick

function lookDelta(target) {
  const p = bot.entity.position;
  const dx = target.x - p.x, dz = target.z - p.z;
  const dy = target.y - (p.y + (bot.entity.height || 1.62));
  let yaw = Math.atan2(-dx, -dz) - bot.entity.yaw;
  while (yaw > Math.PI) yaw -= 2 * Math.PI;
  while (yaw < -Math.PI) yaw += 2 * Math.PI;
  const pitch = Math.atan2(dy, Math.hypot(dx, dz)) - bot.entity.pitch;
  return Math.max(Math.abs(yaw), Math.abs(pitch));
}

function gazeTarget(now) {
  if (attention && now - attention.at <= GAZE_COOLDOWN_MS && nearPlayer(attention.entity, GAZE_RANGE)) {
    return { entity: attention.entity, attention: true };
  }
  const owner = ownerName();
  let best = null;
  for (const [name, pl] of Object.entries(bot.players)) {
    const e = pl && pl.entity;
    if (name === bot.username || !nearPlayer(e, GAZE_RANGE)) continue;
    const before = lastSeen.get(name);
    lastSeen.set(name, e.position.clone());
    if (!before || before.distanceTo(e.position) < 0.3) continue;   // standing still: don't stare
    const dist = bot.entity.position.distanceTo(e.position) - (name === owner ? 2 : 0);
    if (!best || dist < best.dist) best = { entity: e, dist };
  }
  return best && { entity: best.entity, attention: false };
}

function idleGaze() {
  if (!bot.entity || activeActions > 0 || defendTarget || emergency || crouching || bot.pathfinder.isMoving()) return;
  const now = Date.now();
  if (now - lastGazeAt < GAZE_COOLDOWN_MS) return;
  const t = gazeTarget(now);
  if (!t) return;
  if (!t.attention && Math.random() < GAZE_SKIP) return;
  const head = t.entity.position.offset(0, (t.entity.height || 1.8) * 0.85, 0);
  if (lookDelta(head) < GAZE_DEADZONE) return;
  lastGazeAt = now;
  bot.lookAt(head, false).catch(() => {});
}

// Critical health: eat right away instead of waiting for the game agent's
// next turn. Only at 6 HP or less, and only below 18 hunger (above that,
// health comes back by itself). Never mid-fight: eating swaps her weapon
// out, and the fight decides whether she lives.
const REFLEX_EAT_HP = 6;
let eatingNow = false;
let lastReflexEat = 0;

async function autoEatReflex() {
  if (!bot || !botReady || eatingNow || defendTarget || emergency) return;
  if (bot.health > REFLEX_EAT_HP || bot.food >= 18 || Date.now() - lastReflexEat < 3_000) return;
  const food = pickFood();
  if (!food) return;
  eatingNow = true;
  lastReflexEat = Date.now();
  try {
    await bot.equip(food, "hand");
    await bot.consume();
    _logEv("survival", `ate ${food.name} at ${fmtHp(bot.health)} HP`);
  } catch (_) {
  } finally {
    eatingNow = false;
  }
}
setInterval(autoEatReflex, 1000);

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

// Called when the bot is hurt. Fights back against the mob that hit her
// (a hostile within 12 blocks), or the nearest hostile if the server
// didn't say who it was. Never a player, and not after a fall, fire or
// drowning. Only interrupts a running skill when health is getting low.
function maybeAutoDefend(d = null) {
  if (!AUTO_DEFEND || !botReady || defendTarget) return;
  if (d && !["mob", "projectile", "explosion"].includes(d.cause)) return;
  let target = null;
  if (d && d.entity && d.entity.type !== "player" && HOSTILE_MOBS.has(mobName(d.entity)) && d.entity.position &&
      bot.entity.position.distanceTo(d.entity.position) <= 12) {
    target = d.entity;
  } else {
    const t = nearestHostile(DEFEND_RADIUS);
    target = t && t.entity;
  }
  if (!target) return;
  if (activeActions > 0 && bot.health > DEFEND_INTERRUPT_HEALTH) return;
  startDefend(target);
}

// Once a second: announce new hostiles and keep an eye on close ones.
function threatTick() {
  if (!bot || !botReady) return;
  for (const id of announcedThreats) if (!bot.entities[id]) announcedThreats.delete(id);

  const t = nearestHostile(THREAT_RADIUS);
  if (!t) { idleGaze(); return; }
  if (!announcedThreats.has(t.entity.id)) {
    announcedThreats.add(t.entity.id);
    const name = mobName(t.entity);
    const warn = name === "creeper" ? " It's going to blow up!" : "";
    pushEvent("threat", `A ${name} is ${t.dist.toFixed(0)} blocks away from me.${warn}`);
  }
  if (t.dist > DEFEND_RADIUS || defendTarget || activeActions > 0) {
    if (t.dist > DEFEND_RADIUS) idleGaze();
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
          name: "minecraft_scan_environment",
          description: "Danger sense: whether you're in a cave, the time of day, oxygen, and hazards nearby (hostile mobs within 8 blocks, lava, water).",
          schema: { type: "object", properties: {} },
          capabilities: ["read"],
          requires_confirm: false,
        },
        {
          name: "minecraft_retrieve_items",
          description: "Go back to where the bot last died and pick up the dropped items (they despawn 5 minutes after death).",
          schema: { type: "object", properties: {} },
          capabilities: ["write"],
          requires_confirm: false,
        },
        {
          name: "minecraft_use_bed",
          description: "Use a bed: the nearest one, or the one in the inventory placed down. mode 'sleep' (default) sleeps at night, fighting off monsters that are too close first; mode 'spawn' sets the respawn point at the bed, by day too.",
          schema: {
            type: "object",
            properties: { mode: { type: "string", enum: ["sleep", "spawn"], description: "sleep (default) or spawn." } },
          },
          capabilities: ["write"],
          requires_confirm: false,
        },
        {
          name: "minecraft_give_item",
          description: "Walk to a player and toss them an item from the inventory. item_name can be loose, e.g. 'pickaxe' (gives the best one) or 'diamonds'.",
          schema: {
            type: "object",
            properties: {
              item_name: { type: "string", description: "Item, e.g. stone_pickaxe, pickaxe, diamond." },
              count:     { type: "integer", description: "How many. Default 1." },
              username:  { type: "string", description: "Player to give it to. Default: the owner." },
            },
            required: ["item_name"],
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

// ─── Survival overrides ─────────────────────────────────────────────────────
// Checked every 500 ms (falls every physics tick). An emergency interrupts
// whatever skill is running, reports itself in /status (the game agent
// waits it out), and ends when the danger is gone. Fights are left alone.

let emergency = null;          // { kind: "drowning" | "lava", since }
let escaping = false;
let fall = null;               // { startY, warned, bucketUsed }

const isSolid = (b) => b && b.boundingBox === "block";
const headPos = () => bot.entity.position.offset(0, bot.entity.height || 1.62, 0).floored();

function startEmergency(kind, why) {
  if (emergency && emergency.kind === kind) return;
  emergency = { kind, since: Date.now() };
  console.log(`[survival] ${why}`);
  _logEv("survival", why);
  interruptCurrent(kind === "drowning" ? "drowning" : "a lava escape");
}

function endEmergency() {
  if (!emergency) return;
  _logEv("survival", `${emergency.kind} over`);
  emergency = null;
  escaping = false;
  try { bot.setControlState("jump", false); } catch (_) {}
}

// Lava (or fire) the bot could actually touch: the block it's in, or lava
// within 2.5 blocks that has open air next to it. Lava sealed behind stone
// doesn't count.
function lavaDanger() {
  const feet = bot.entity.position.floored();
  for (const p of [feet, feet.offset(0, 1, 0)]) {
    const b = bot.blockAt(p);
    if (b && (b.name === "lava" || b.name === "fire" || b.name === "soul_fire")) return p;
  }
  const lava = mcData.blocksByName.lava.id;
  for (const p of bot.findBlocks({ matching: lava, maxDistance: 3, count: 12 })) {
    if (p.offset(0.5, 0.5, 0.5).distanceTo(bot.entity.position) > 2.5) continue;
    if (SIDES.some(f => { const n = bot.blockAt(p.plus(f)); return n && n.boundingBox === "empty" && n.name !== "lava"; })) return p;
  }
  return null;
}

// The closest standable spot within 6 blocks with no lava within 3.
function safeSpotAwayFrom(danger) {
  const lava = mcData.blocksByName.lava.id;
  const base = bot.entity.position.floored();
  let best = null;
  for (let dx = -6; dx <= 6; dx++) for (let dz = -6; dz <= 6; dz++) for (let dy = -2; dy <= 2; dy++) {
    const p = base.offset(dx, dy, dz);
    if (p.distanceTo(danger) < 3) continue;
    const here = bot.blockAt(p), head = bot.blockAt(p.offset(0, 1, 0)), below = bot.blockAt(p.offset(0, -1, 0));
    if (!here || here.boundingBox !== "empty" || here.name === "lava" || here.name === "water") continue;
    if (!head || head.boundingBox !== "empty" || !isSolid(below) || below.name === "magma_block") continue;
    if (bot.findBlocks({ point: p, matching: lava, maxDistance: 3, count: 1 }).length) continue;
    const d = p.distanceTo(base);
    if (!best || d < best.d) best = { p, d };
  }
  return best && best.p;
}

async function escapeLava(danger) {
  if (escaping) return;
  escaping = true;
  try {
    const spot = safeSpotAwayFrom(danger);
    if (!spot) {
      // Nowhere mapped as safe: back straight away from it.
      const away = bot.entity.position.minus(danger);
      await bot.lookAt(bot.entity.position.plus(away), true);
      bot.setControlState("forward", true);
      bot.setControlState("jump", true);
      await sleep(1200);
      bot.clearControlStates();
      return;
    }
    await Promise.race([bot.pathfinder.goto(new goals.GoalBlock(spot.x, spot.y, spot.z)), sleep(8000)]);
  } catch (_) {
  } finally {
    stopAllActivity();
    escaping = false;
  }
}

function survivalTick() {
  if (!bot || !botReady || !bot.entity) return;
  const head = bot.blockAt(headPos());
  const headInWater = head && head.name === "water";
  const oxygen = bot.oxygenLevel ?? 20;

  if (headInWater && oxygen < 10) {
    startEmergency("drowning", `Drowning! Oxygen ${oxygen}/20: swimming up`);
  }
  if (emergency && emergency.kind === "drowning") {
    if (!headInWater || oxygen >= 18) { endEmergency(); return; }
    bot.setControlState("jump", true);              // swim straight up
    bot.look(bot.entity.yaw, Math.PI / 2, true).catch(() => {});
    const above = bot.blockAt(headPos().offset(0, 1, 0));
    if (isSolid(above) && above.name !== "bedrock" && !bot.targetDigBlock) {
      bot.dig(above, true).catch(() => {});         // a ceiling: dig through it
    }
    return;
  }

  const danger = lavaDanger();
  if (danger) {
    startEmergency("lava", `Lava next to me at ${danger.x}, ${danger.y}, ${danger.z}: getting away`);
    escapeLava(danger);
  } else if (emergency && emergency.kind === "lava" && !escaping) {
    endEmergency();
  }
}
setInterval(survivalTick, 500);

// ─── Moving like a player: sprint, swim, fly ────────────────────────────────
// Sprinting: the pathfinder sprints on straight stretches when allowed; this
// decides when it's allowed. Not at 6 hunger or less (the game won't let a
// player sprint then), and not for the last few blocks when catching up to
// the player, so she doesn't run past them.
// Swimming: while following, water is cheap to path through, so she swims
// after the player instead of refusing to cross a river. Standing in deep
// water with nowhere to go, she treads water at the surface.
// Flying: in creative she flies to the player when they're far or up in the
// air. With an elytra on, she opens it when she falls off something high,
// and boosts with fireworks toward the player when they're far away.

const SPRINT_MIN_FOOD = 7;
const WALK_NEAR_OWNER = 5;
const FLY_FOLLOW_DIST = 20;
const GLIDE_MIN_DROP = 6;
const ROCKET_EVERY_MS = 2500;
const ROCKET_OWNER_DIST = 24;
let following = false;        // inside return_to_player
let flyingNow = false;        // creative flight (gravity off)
let treading = false;
let lastRocket = 0;

function canCreativeFly() {
  return bot.game && bot.game.gameMode === "creative";
}

function ownerEnt() {
  const name = ownerName();
  return name && bot.players[name] && bot.players[name].entity;
}

// Open air under a point, counted in blocks (up to `max`).
function airBelow(pos, max) {
  const p = pos.floored();
  for (let i = 1; i <= max; i++) {
    const b = bot.blockAt(p.offset(0, -i, 0));
    if (!b || b.boundingBox !== "empty" || b.name === "water" || b.name === "lava") return i - 1;
  }
  return max;
}

// The player is in the air (flying, or falling), not standing on something.
function ownerAirborne(ent) {
  return airBelow(ent.position, 2) >= 2;
}

// A straight line the bot's body fits through, for creative flight (which
// moves through blocks otherwise and gets rubber-banded by the server).
function clearLine(from, to) {
  const delta = to.minus(from), len = delta.norm();
  if (len < 0.01) return true;
  const step = delta.scaled(0.5 / len);
  for (let p = from.clone(), t = 0; t <= len; t += 0.5, p = p.plus(step)) {
    for (const dy of [0, 1]) {
      const b = bot.blockAt(p.offset(0, dy, 0));
      if (b && b.boundingBox !== "empty") return false;
    }
  }
  return true;
}

function stopCreativeFlight() {
  if (!flyingNow) return;
  flyingNow = false;
  try { bot.creative.stopFlying(); } catch (_) {}
}

// Fly next to the player in creative: straight there when the line is
// clear, else straight up first and across from there. False if neither
// is clear.
async function flyNear(ent) {
  const me = bot.entity.position;
  const dir = me.minus(ent.position); dir.y = 0;
  const off = dir.norm() > 0.1 ? dir.scaled(2 / dir.norm()) : new Vec3(2, 0, 0);
  const dest = ent.position.plus(off).offset(0, ownerAirborne(ent) ? 0 : 0.5, 0);
  let legs = null;
  if (clearLine(me, dest)) legs = [dest];
  else {
    for (let up = 2; up <= 10 && !legs; up += 2) {
      const top = me.offset(0, up, 0), over = dest.offset(0, up, 0);
      if (clearLine(me, top) && clearLine(top, over) && clearLine(over, dest)) legs = [top, over, dest];
    }
  }
  if (!legs) return false;
  stopAllActivity();
  flyingNow = true;
  try {
    for (const leg of legs) {
      await Promise.race([bot.creative.flyTo(leg), sleep(15_000)]);
    }
  } catch (_) {
    return false;
  }
  return true;
}

function followMovements() {
  const moves = survivalMovements();
  moves.liquidCost = 3;   // swim after the player rather than refuse to cross
  return moves;
}

async function glideAndBoost(e) {
  const torso = bot.inventory.slots[bot.getEquipmentDestSlot("torso")];
  if (!torso || torso.name !== "elytra") return;
  if (!e.elytraFlying) {
    // Open it on a real fall: dropping fast with a long way to go.
    if (!e.onGround && !e.isInWater && e.velocity.y < -0.5 && airBelow(e.position, GLIDE_MIN_DROP) >= GLIDE_MIN_DROP) {
      bot.elytraFly().then(() => _logEv("move", "opened the elytra")).catch(() => {});
    }
    return;
  }
  // Gliding: head for the player if there is one, nose slightly down.
  const ent = ownerEnt();
  if (!ent) return;
  const to = ent.position.minus(e.position);
  const yaw = Math.atan2(-to.x, -to.z);
  const far = Math.hypot(to.x, to.z) > ROCKET_OWNER_DIST;
  bot.look(yaw, far ? 0.2 : -0.25, true).catch(() => {});
  const rocket = bot.inventory.items().find(i => i.name === "firework_rocket");
  if (far && rocket && Date.now() - lastRocket > ROCKET_EVERY_MS) {
    lastRocket = Date.now();
    try { await bot.equip(rocket, "hand"); bot.activateItem(); } catch (_) {}
  }
}

function movementTick() {
  if (!bot || !botReady || !bot.entity || !bot.pathfinder || !bot.pathfinder.movements) return;
  const e = bot.entity;
  const ent = ownerEnt();
  const ownerD = ent ? e.position.distanceTo(ent.position) : Infinity;

  bot.pathfinder.movements.allowSprinting =
    (bot.food ?? 20) >= SPRINT_MIN_FOOD && !(following && ownerD < WALK_NEAR_OWNER);

  // Tread water when nothing else is steering.
  const head = bot.blockAt(headPos());
  const shouldTread = Boolean(head && head.name === "water") && !emergency && !bot.pathfinder.isMoving();
  try {
    if (shouldTread && !bot.getControlState("jump")) bot.setControlState("jump", true);
    else if (!shouldTread && treading) bot.setControlState("jump", false);
  } catch (_) {}
  treading = shouldTread;

  // Creative flight ends once the player is back on the ground.
  if (flyingNow && !following && (!ent || !ownerAirborne(ent))) stopCreativeFlight();

  glideAndBoost(e).catch(() => {});
}
setInterval(movementTick, 250);

function movementState() {
  const e = bot.entity;
  if (!e) return "still";
  if (e.elytraFlying) return "gliding";
  if (flyingNow) return "flying";
  const head = bot.blockAt(headPos());
  if (e.isInWater || (head && head.name === "water")) return "swimming";
  if (bot.getControlState && bot.getControlState("sprint") && bot.getControlState("forward")) return "sprinting";
  return bot.pathfinder && bot.pathfinder.isMoving() ? "walking" : "still";
}

// Falling: log it, and with a water bucket, catch the landing ("MLG").
function onPhysicsTick() {
  if (!bot || !botReady || !bot.entity) return;
  const e = bot.entity;
  trackFall(e);
  if (e.onGround || (e.velocity && e.velocity.y > -0.4)) {
    if (fall && fall.bucketUsed) pickUpWater();
    fall = null;
    return;
  }
  fall = fall || { startY: e.position.y, warned: false, bucketUsed: false };
  let ground = 0;
  const feet = e.position.floored();
  while (ground < 40 && !isSolid(bot.blockAt(feet.offset(0, -ground - 1, 0)))) ground++;
  const below = bot.blockAt(feet.offset(0, -ground, 0));
  const total = fall.startY - e.position.y + ground;
  if (total <= 3 || (below && below.name === "water")) return;
  if (!fall.warned) {
    fall.warned = true;
    console.log(`[survival] Falling! about ${Math.round(total)} blocks`);
    _logEv("survival", `Falling! about ${Math.round(total)} blocks`);
  }
  const bucket = bot.inventory.items().find(i => i.name === "water_bucket");
  if (bucket && !fall.bucketUsed && ground <= 3) {
    fall.bucketUsed = true;
    bot.equip(bucket, "hand")
      .then(() => bot.look(e.yaw, -Math.PI / 2, true))
      .then(() => bot.activateItem())
      .catch(() => {});
  }
}

// After a water-bucket landing, scoop the water back up.
function pickUpWater() {
  setTimeout(() => {
    const water = bot.findBlock({ matching: mcData.blocksByName.water.id, maxDistance: 3 });
    const bucket = bot.inventory.items().find(i => i.name === "bucket");
    if (!water || !bucket) return;
    bot.equip(bucket, "hand").then(() => bot.lookAt(water.position.offset(0.5, 0.5, 0.5), true))
      .then(() => bot.activateItem()).catch(() => {});
  }, 300);
}

// ─── Environment (danger sense) ─────────────────────────────────────────────

const SEE_THROUGH = /leaves|glass|^air$|vine|torch/;

function environmentJson() {
  const head = headPos();
  let covered = false;
  for (let dy = 1; dy <= 24 && !covered; dy++) {
    const b = bot.blockAt(head.offset(0, dy, 0));
    if (b && b.boundingBox === "block" && !SEE_THROUGH.test(b.name)) covered = true;
  }
  const headBlock = bot.blockAt(head);
  const sky = headBlock && typeof headBlock.skyLight === "number" ? headBlock.skyLight : null;
  const inCave = covered && (sky === null ? bot.entity.position.y < 56 : sky <= 4);
  const hazards = nearbyThreats(8).map(t => `${t.name} at ${Math.round(t.distance)} blocks`);
  const lava = bot.findBlocks({ matching: mcData.blocksByName.lava.id, maxDistance: 6, count: 1 })[0];
  if (lava) hazards.push(`lava ${Math.round(lava.distanceTo(bot.entity.position))} blocks away`);
  if (headBlock && headBlock.name === "water") hazards.push("underwater");
  const tod = bot.time ? bot.time.timeOfDay : null;
  return {
    in_cave: inCave,
    covered,
    sky_light: sky,
    in_water: Boolean(headBlock && headBlock.name === "water"),
    oxygen: bot.oxygenLevel ?? 20,
    is_day: bot.time ? bot.time.isDay !== false : true,
    time_of_day: tod,
    hazards,
    emergency: emergency ? emergency.kind : null,
    moving: movementState(),
  };
}

function environmentText(env) {
  const parts = [env.in_cave ? "You are in a cave." : env.covered ? "You are under cover." : "You are outdoors."];
  parts.push(env.is_day ? "It's day." : "It's night.");
  for (const h of env.hazards) parts.push(`${h[0].toUpperCase()}${h.slice(1)}.`);
  if (env.oxygen < 20) parts.push(`Oxygen is ${env.oxygen}/20.`);
  if (env.moving && !["still", "walking"].includes(env.moving)) parts.push(`You are ${env.moving}.`);
  if (env.in_cave) parts.push("Pathfinding may be difficult.");
  if (env.emergency) parts.push(`EMERGENCY: ${env.emergency}.`);
  return `Environment: ${parts.join(" ")}`;
}

async function handleScanEnvironment(_args) {
  if (!botReady) return { success: false, message: "bot is not connected to Minecraft" };
  return { success: true, message: environmentText(environmentJson()) };
}

// ─── Beds: respawn point and sleeping ───────────────────────────────────────
// Using a bed sets the respawn point, by day too on Java 1.15+; the server
// confirms with "Respawn point set". Sleeping needs night (or a
// thunderstorm) and no monsters within about 8 blocks. When there are some,
// she fights them and tries again. Sleeping sets the respawn point as well.
// The bed she uses is remembered in data/minecraft/bed.json.

const BED_PATH = path.join(DATA_DIR, "bed.json");
const BED_RE = /_bed$/;
const SLEEP_MAX_KILLS = 5;
const SLEEP_MAX_MS = 10 * 60_000;        // a whole night is about 7 minutes
const WAKE_OWNER_DIST = 32;              // get up if the player wanders off
let spawnHeardAt = 0;                    // when the server last confirmed a respawn point

function readBed() {
  try { return JSON.parse(fs.readFileSync(BED_PATH, "utf8")); } catch (_) { return null; }
}

function saveBed(b) {
  try { fs.mkdirSync(DATA_DIR, { recursive: true }); fs.writeFileSync(BED_PATH, JSON.stringify(b)); } catch (_) {}
}

function forgetBed() {
  try { fs.unlinkSync(BED_PATH); } catch (_) {}
}

function canSleepNow() {
  if (!bot.time) return false;
  const t = bot.time.timeOfDay;
  return Boolean(bot.isRaining && bot.thunderState > 0) || (t >= 12541 && t <= 23458);
}

const bedIds = () => Object.values(mcData.blocksByName).filter(b => BED_RE.test(b.name)).map(b => b.id);
const bedItem = () => bot.inventory.items().find(i => BED_RE.test(i.name));

// The remembered bed if it's still there and within range, else the
// nearest one in range.
function findBed(maxDistance = 32) {
  const mem = readBed();
  const memPos = mem && new Vec3(mem.x, mem.y, mem.z);
  if (mem && (!mem.dimension || !bot.game || mem.dimension === bot.game.dimension)
      && bot.entity.position.distanceTo(memPos) <= maxDistance) {
    const b = bot.blockAt(memPos);
    if (b && BED_RE.test(b.name)) return b;
    if (b) forgetBed();                  // loaded, and the bed is gone
  }
  return bot.findBlock({ matching: bedIds(), maxDistance });
}

function rememberBed(pos, spawn) {
  saveBed({ x: pos.x, y: pos.y, z: pos.z, dimension: bot.game && bot.game.dimension, spawn: Boolean(spawn) });
}

function bedJson() {
  const mem = readBed();
  const near = botReady && bot.entity ? findBed(32) : null;
  return {
    spawn_set: Boolean(mem && mem.spawn),
    remembered: mem ? { x: mem.x, y: mem.y, z: mem.z } : null,
    nearby: Boolean(near),
    in_inventory: Boolean(botReady && bedItem()),
  };
}

// Put the bed from the inventory down next to her: the foot one block
// away, the head one further, both on solid ground. A bed points the way
// the player placing it faces.
async function placeBedNearby() {
  const item = bedItem();
  if (!item) return null;
  const base = bot.entity.position.floored();
  for (const [dx, dz] of [[1, 0], [-1, 0], [0, 1], [0, -1]]) {
    const foot = base.offset(dx, 0, dz), head = base.offset(2 * dx, 0, 2 * dz);
    const fits = [foot, head].every(p => {
      const here = bot.blockAt(p);
      return here && here.boundingBox === "empty" && here.name !== "water" && isSolid(bot.blockAt(p.offset(0, -1, 0)));
    });
    if (!fits) continue;
    try {
      await bot.look(Math.atan2(-dx, -dz), 0, true);
      await bot.equip(item, "hand");
      await bot.placeBlock(bot.blockAt(foot.offset(0, -1, 0)), new Vec3(0, 1, 0));
    } catch (_) {
      continue;
    }
    await sleep(300);
    const b = bot.blockAt(foot);
    if (b && BED_RE.test(b.name)) return b;
  }
  return null;
}

// Right-click the bed and wait for the server to confirm the respawn point.
async function setSpawnAt(bed) {
  const before = spawnHeardAt;
  try { await bot.activateBlock(bed); } catch (_) { return false; }
  for (let i = 0; i < 25 && spawnHeardAt === before && !bot.isSleeping; i++) await sleep(100);
  return spawnHeardAt !== before || bot.isSleeping;
}

// Runs in the background once she's asleep, so the agent isn't blocked all
// night: she stays in bed until the server wakes her (morning), or gets up
// when an order or emergency comes in, something hurts her, or the player
// wanders off.
async function watchSleep(seq) {
  const start = Date.now(), hp = bot.health;
  while (bot && bot.isSleeping && Date.now() - start < SLEEP_MAX_MS) {
    await sleep(500);
    let why = null;
    if (interruptSeq !== seq) why = `interrupted by ${interruptReason}`;
    else if (bot.health < hp) why = "something hurt me";
    else {
      const ent = ownerEnt();
      if (ownerName() && (!ent || bot.entity.position.distanceTo(ent.position) > WAKE_OWNER_DIST)) {
        why = `${ownerName()} wandered off`;
      }
    }
    if (why) {
      _logEv("bed", `got up: ${why}`);
      try { await bot.wake(); } catch (_) {}
      return;
    }
  }
  if (bot && bot.isSleeping) { try { await bot.wake(); } catch (_) {} }
  else _logEv("bed", "woke up");
}

async function handleUseBed(a) {
  if (!botReady) return { success: false, message: "bot is not connected to Minecraft" };
  const mode = a.mode === "spawn" ? "spawn" : "sleep";
  if (mode === "sleep" && !canSleepNow()) {
    return { success: false, message: "It isn't night. Beds only work at night or in a thunderstorm" };
  }
  if (bot.isSleeping) return { success: true, message: "Already in bed" };
  const seq = interruptSeq;
  let bed = findBed();
  if (!bed) bed = await placeBedNearby();
  if (!bed) {
    return { success: false, message: bedItem() ? "Couldn't find a flat spot to put my bed down"
                                                 : "No bed nearby and none in my inventory" };
  }
  const pos = bed.position;
  const mem = readBed();
  const sameBed = mem && mem.x === pos.x && mem.y === pos.y && mem.z === pos.z;
  rememberBed(pos, sameBed && mem.spawn);
  if (!(await reach(pos, 2))) return { success: false, message: "Couldn't get to the bed" };
  if (interruptSeq !== seq) return { success: false, message: `interrupted by ${interruptReason}` };

  if (mode === "spawn") {
    if (!(await setSpawnAt(bot.blockAt(pos)))) {
      return { success: false, message: "The game didn't confirm a respawn point (older versions only set it by sleeping at night)" };
    }
    rememberBed(pos, true);
    if (bot.isSleeping) {
      watchSleep(seq).catch(() => {});
      return { success: true, message: "Set my respawn point at the bed and went to sleep" };
    }
    return { success: true, message: `Set my respawn point at the bed (${pos.x}, ${pos.y}, ${pos.z})` };
  }

  // Sleep, clearing out any monsters the game says are too close.
  const killed = [];
  let lastError = "";
  for (let tries = 0; tries < SLEEP_MAX_KILLS + 3 && !bot.isSleeping; tries++) {
    if (interruptSeq !== seq) return { success: false, message: `interrupted by ${interruptReason}` };
    try {
      await bot.sleep(bot.blockAt(pos));
    } catch (e) {
      lastError = e.message;
      if (/monsters nearby/.test(e.message)) {
        if (killed.length >= SLEEP_MAX_KILLS) break;
        const t = nearestHostile(12);
        if (!t) { await sleep(1500); continue; }   // the server's box can see one we can't reach
        const r = await fightUntilDead(t.entity, 25_000);
        if (!r.success) return { success: false, message: `Couldn't sleep, monsters nearby: ${r.message}` };
        killed.push(mobName(t.entity));
        await reach(pos, 2);
      } else if (/too far/.test(e.message)) {
        await reach(pos, 1);
      } else if (/occupied/.test(e.message)) {
        return { success: false, message: "Someone else is in that bed" };
      } else if (!canSleepNow()) {
        return { success: false, message: "It's morning already" };
      } else {
        await sleep(1000);
      }
    }
  }
  const fought = killed.length ? `Killed ${killed.map(k => `a ${k.replace(/_/g, " ")}`).join(", ")}, then ` : "";
  if (!bot.isSleeping) {
    return { success: false, message: `${fought}couldn't get to sleep: ${lastError || "the game didn't let me"}` };
  }
  rememberBed(pos, true);                   // sleeping sets the respawn point
  _logEv("bed", "sleeping");
  watchSleep(seq).catch(() => {});
  return { success: true, message: `${fought ? fought + "went to sleep" : "Went to sleep"}. My respawn point is here now` };
}

// ─── Death memory and getting items back ────────────────────────────────────

function rememberDeath() {
  try {
    const p = bot.entity.position;
    fs.mkdirSync(DATA_DIR, { recursive: true });
    fs.writeFileSync(DEATH_PATH, JSON.stringify({
      x: +p.x.toFixed(1), y: +p.y.toFixed(1), z: +p.z.toFixed(1),
      dimension: bot.game && bot.game.dimension, at: Date.now() / 1000,
    }));
  } catch (_) {}
}

function readDeath() {
  try { return JSON.parse(fs.readFileSync(DEATH_PATH, "utf8")); } catch (_) { return null; }
}

function forgetDeath() {
  try { fs.unlinkSync(DEATH_PATH); } catch (_) {}
}

function deathJson() {
  const d = readDeath();
  if (!d) return null;
  const age = Math.round(Date.now() / 1000 - d.at);
  return { x: d.x, y: d.y, z: d.z, age_s: age, recoverable: age < ITEM_DESPAWN_S };
}

const isItemEntity = (e) => e && (e.name === "item" || e.objectType === "Item" || e.displayName === "Item");

async function handleRetrieveItems(_args) {
  if (!botReady) return { success: false, message: "bot is not connected to Minecraft" };
  const d = readDeath();
  if (!d) return { success: false, message: "I don't remember dying anywhere" };
  const age = Date.now() / 1000 - d.at;
  if (age > ITEM_DESPAWN_S) {
    forgetDeath();
    return { success: false, message: `My items have despawned (I died ${Math.round(age / 60)} minutes ago)` };
  }
  if (d.dimension && bot.game && bot.game.dimension && d.dimension !== bot.game.dimension) {
    return { success: false, message: "I died in another dimension" };
  }
  const spot = new Vec3(d.x, d.y, d.z);
  const at = bot.blockAt(spot.floored());
  if (at && (at.name === "lava" || at.name === "fire")) {
    forgetDeath();
    return { success: false, message: "I died in lava; those items are gone" };
  }
  const seq = interruptSeq;
  const deadline = Date.now() + 100_000;
  let picked = 0;
  const onCollect = (collector) => { if (collector === bot.entity) picked++; };
  bot.on("playerCollect", onCollect);
  try {
    if (bot.entity.position.distanceTo(spot) > 10) {
      await Promise.race([bot.pathfinder.goto(new goals.GoalNear(spot.x, spot.y, spot.z, 8)), sleep(60_000)]);
    }
    if (bot.entity.position.distanceTo(spot) > 12) {
      forgetDeath();
      return { success: false, message: "I couldn't get back to where I died" };
    }
    while (Date.now() < deadline && interruptSeq === seq) {
      const item = bot.nearestEntity(e => isItemEntity(e) && e.position.distanceTo(spot) < 14);
      if (!item) break;
      const target = item.position.floored();
      try {
        await Promise.race([bot.pathfinder.goto(new goals.GoalBlock(target.x, target.y, target.z)), sleep(10_000)]);
      } catch (_) {}
      await sleep(400);
      if (bot.entities[item.id]) {
        delete bot.entities[item.id];   // couldn't reach it; don't loop on it
      }
    }
  } catch (e) {
    return { success: false, message: `Couldn't get my items: ${e.message}` };
  } finally {
    bot.removeListener("playerCollect", onCollect);
    stopAllActivity();
  }
  if (interruptSeq !== seq) return { success: false, message: `interrupted by ${interruptReason}` };
  forgetDeath();
  return picked
    ? { success: true, message: `Got my stuff back: picked up ${picked} stack${picked === 1 ? "" : "s"} where I died` }
    : { success: false, message: "Nothing left where I died (despawned or picked up)" };
}

// ─── Server: reconnect and LAN discovery ────────────────────────────────────

const HOST_RE = /^[A-Za-z0-9.:\[\]-]{1,253}$/;

// Ask the server for its status. Resolves to {motd, version, players} or
// throws a readable error.
async function probeServer(host, port) {
  const mc = require("minecraft-protocol");
  try {
    const r = await Promise.race([
      mc.ping({ host, port, closeTimeout: 4000 }),
      sleep(4500).then(() => { throw new Error("timeout"); }),
    ]);
    const motd = typeof r.description === "string" ? r.description
               : (r.description && (r.description.text || "")) || "";
    return { motd: motd.replace(/§./g, "").trim(), version: r.version && r.version.name,
             players: r.players ? `${r.players.online}/${r.players.max}` : null };
  } catch (e) {
    // A refused connection on a dual-stack machine is an AggregateError
    // with an empty message; the code is on the inner errors.
    const code = [e.code, ...(e.errors || []).map(x => x.code), e.message].filter(Boolean).join(" ");
    const why = /ECONNREFUSED/.test(code) ? "nothing is listening there"
              : /ENOTFOUND|EAI_AGAIN/.test(code) ? "that host name doesn't resolve"
              : /timeout|ETIMEDOUT/.test(code) ? "it didn't answer in time"
              : code || "unknown error";
    throw new Error(`Couldn't reach a Minecraft world at ${host}:${port}: ${why}. ` +
                    "Open the world to LAN (or check the port) and try again.");
  }
}

// Switch servers: check the new one answers first, so a typo leaves the
// current connection alone. Remembered in data/minecraft/server.json.
async function reconnectTo(hostIn, portIn) {
  const host = String(hostIn || "").trim();
  const port = parseInt(portIn, 10);
  if (!HOST_RE.test(host) || !(port >= 1 && port <= 65535)) {
    return { status: 422, success: false, message: "host and a valid port (1-65535) are required" };
  }
  let info;
  try {
    info = await probeServer(host, port);
  } catch (e) {
    _logEv("config", e.message);
    return { status: 502, success: false, message: e.message };
  }
  MC_HOST = host;
  MC_PORT = port;
  try {
    fs.mkdirSync(DATA_DIR, { recursive: true });
    fs.writeFileSync(SERVER_PATH, JSON.stringify({ host, port }));
  } catch (_) {}
  _logEv("config", `server changed to ${host}:${port}`);
  console.log(`[mc] reconnecting to ${host}:${port}`);
  if (bot) {
    stopViewer(bot);
    try { bot.quit("switching servers"); } catch (_) {}
  }
  setTimeout(createBot, 2000);
  const label = [info.motd && `"${info.motd}"`, info.version, info.players && `${info.players} players`]
    .filter(Boolean).join(", ");
  return { status: 200, success: true, message: `Reconnecting to ${host}:${port}${label ? ` (${label})` : ""}`,
           server: { host, port, ...info } };
}

// Minecraft announces "Open to LAN" worlds every 1.5 s on 224.0.2.60:4445
// as "[MOTD]name[/MOTD][AD]port[/AD]". Keep what was heard in the last 10 s.
const lanWorlds = new Map();

function localAddresses() {
  return new Set(Object.values(os.networkInterfaces()).flat().filter(Boolean).map(i => i.address));
}

function startLanDiscovery() {
  if (!LAN_DISCOVERY) return;
  const sock = dgram.createSocket({ type: "udp4", reuseAddr: true });
  sock.on("error", (e) => {
    console.warn(`[lan] LAN world discovery off (${e.code || e.message})`);
    try { sock.close(); } catch (_) {}
  });
  sock.on("message", (msg, rinfo) => {
    const text = msg.toString("utf8");
    const port = /\[AD\](\d{1,5})\[\/AD\]/.exec(text);
    if (!port) return;
    const motd = /\[MOTD\](.*?)\[\/MOTD\]/.exec(text);
    const host = localAddresses().has(rinfo.address) ? "localhost" : rinfo.address;
    lanWorlds.set(`${host}:${port[1]}`, {
      name: motd ? motd[1].replace(/§./g, "") : "LAN world", host, port: +port[1], seen: Date.now(),
    });
  });
  sock.bind(4445, () => {
    try {
      sock.addMembership("224.0.2.60");
      console.log("[lan] listening for Open to LAN worlds");
    } catch (e) {
      console.warn(`[lan] LAN world discovery off (${e.message})`);
    }
  });
}

function lanWorldsJson() {
  const now = Date.now();
  for (const [k, w] of lanWorlds) if (now - w.seen > 10_000) lanWorlds.delete(k);
  return [...lanWorlds.values()].map(({ name, host, port }) => ({
    name, host, port, current: host === MC_HOST && port === MC_PORT,
  }));
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
  const moves = survivalMovements();
  moves.canDig = false;
  moves.allow1by1towers = false;   // no scaffold pillars inside the house
  bot.pathfinder.setMovements(moves);
}

// ─── Return to the player ───────────────────────────────────────────────────

const STUCK_MS = 30_000;

// While going to the player she swims (water is cheap to path through) and,
// in creative, flies; see "Moving like a player".
async function handleReturnToPlayer(a) {
  if (!botReady) return { success: false, message: "bot is not connected to Minecraft" };
  following = true;
  bot.pathfinder.setMovements(followMovements());
  try {
    return await returnToPlayer(a);
  } finally {
    following = false;
    bot.pathfinder.setMovements(survivalMovements());
    const ent = ownerEnt();
    if (flyingNow && (!ent || !ownerAirborne(ent))) stopCreativeFlight();
  }
}

async function returnToPlayer(a) {
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
      if (interruptSeq !== seq) return { success: false, message: `interrupted by ${interruptReason}` };
      const ent = ownerEntity();
      if (!ent) {
        // Out of tracking range: no position to walk to.
        if (!triedTeleport && (await teleport())) break;
        return { success: false, message: `${name} is too far away to see, and /tp didn't work (needs cheats on)` };
      }
      const d = bot.entity.position.distanceTo(ent.position);
      if (d <= range + 0.5) break;
      if (d < best - 1) { best = d; lastProgress = Date.now(); }
      // Creative: fly when they're far or up in the air (walking can't
      // reach someone flying).
      if (canCreativeFly() && (d > FLY_FOLLOW_DIST || ownerAirborne(ent)) && (await flyNear(ent))) {
        lastPlan = 0;
        await sleep(200);
        continue;
      }
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
      if (interruptSeq !== seq) { stopped = `interrupted by ${interruptReason}`; break; }
      await step(true);
    }
    while (!stopped && tunnel < length) {
      if (Date.now() > deadline) { stopped = "time limit"; break; }
      if (lastHurtAt !== hurtAtStart) { stopped = "something attacked me"; break; }
      if (interruptSeq !== seq) { stopped = `interrupted by ${interruptReason}`; break; }
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

// ─── Handing items to a player ──────────────────────────────────────────────

const TOOL_WORDS = /^(pickaxe|axe|sword|shovel|hoe|helmet|chestplate|leggings|boots)$/;
const MATERIAL_RANK = ["netherite", "diamond", "iron", "stone", "golden", "wooden", "leather"];
const rankOf = (name) => {
  const i = MATERIAL_RANK.findIndex(m => name.startsWith(m + "_"));
  return i < 0 ? 99 : i;
};

// The inventory item a loose name like "pickaxe", "diamonds" or "iron
// pickaxe" refers to. Tools pick the best material; anything else the
// stack there's most of.
function resolveItem(term) {
  const t = String(term || "").toLowerCase().trim().replace(/\s+/g, "_");
  const items = bot.inventory.items();
  for (const cand of [t, t.replace(/s$/, ""), t.replace(/es$/, "")]) {
    if (items.some(i => i.name === cand)) return cand;
  }
  const word = t.replace(/s$/, "");
  const names = [...new Set(items.map(i => i.name).filter(n => n.includes(word)))];
  if (!names.length) return null;
  if (TOOL_WORDS.test(word)) names.sort((a, b) => rankOf(a) - rankOf(b));
  else names.sort((a, b) => countItem(b) - countItem(a));
  return names[0];
}

async function handleGiveItem(a) {
  if (!botReady) return { success: false, message: "bot is not connected to Minecraft" };
  const term = String(a.item_name || "").trim();
  if (!/^[a-z0-9_ ]{2,40}$/i.test(term)) return { success: false, message: "item_name must be an item like stone_pickaxe or diamond" };
  const name = a.username ? String(a.username) : ownerName();
  if (!name || !bot.players[name]) return { success: false, message: `${name || "No player"} isn't online` };
  const item = resolveItem(term);
  if (!item) return { success: false, message: `I don't have any ${term}` };
  const have = countItem(item);
  const n = Math.min(Math.max(parseInt(a.count ?? 1, 10) || 1, 1), have);

  const ent = bot.players[name].entity;
  if (!ent) return { success: false, message: `I can't see ${name} from here` };
  try {
    if (bot.entity.position.distanceTo(ent.position) > 3) {
      await Promise.race([
        bot.pathfinder.goto(new goals.GoalFollow(ent, 2)),
        sleep(30_000).then(() => { throw new Error("couldn't reach them in 30 s"); }),
      ]);
    }
  } catch (e) {
    stopAllActivity();
    if (bot.entity.position.distanceTo(ent.position) > 4) return { success: false, message: `Couldn't get to ${name}: ${e.message}` };
  }
  stopAllActivity();
  try {
    // Items are thrown where the bot looks, so look at them first.
    await bot.lookAt(ent.position.offset(0, 1.2, 0), true);
    await bot.toss(mcData.itemsByName[item].id, null, n);
    return { success: true, message: `Gave ${n} ${item} to ${name}` };
  } catch (e) {
    return { success: false, message: `Couldn't hand it over: ${e.message}` };
  }
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
          if (interruptSeq !== seq) reject(new Error(`interrupted by ${interruptReason}`));
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
  minecraft_give_item:          handleGiveItem,
  minecraft_scan_environment:   handleScanEnvironment,
  minecraft_retrieve_items:     handleRetrieveItems,
  minecraft_use_bed:            handleUseBed,
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
      // Same as /reconnect: check the new server answers before switching.
      reconnectTo(payload.host, payload.port).then(({ status, ...r }) => {
        res.writeHead(status);
        res.end(JSON.stringify(r));
      });
    });

  } else if (req.method === "POST" && url === "/reconnect") {
    let body = "";
    req.on("data", c => { body += c; if (body.length > 4096) req.destroy(); });
    req.on("end", () => {
      let payload;
      try { payload = JSON.parse(body); } catch (_) {
        res.writeHead(400);
        res.end(JSON.stringify({ success: false, message: "bad JSON" }));
        return;
      }
      reconnectTo(payload.host, payload.port).then(({ status, ...r }) => {
        res.writeHead(status);
        res.end(JSON.stringify(r));
      });
    });

  } else if (req.method === "GET" && url === "/lan") {
    res.writeHead(200);
    res.end(JSON.stringify({ worlds: lanWorldsJson(), current: { host: MC_HOST, port: MC_PORT } }));

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
startLanDiscovery();
createBot();
connect();
