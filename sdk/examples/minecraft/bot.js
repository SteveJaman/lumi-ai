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
const { plugin: toolPlugin }   = require("mineflayer-tool");
const { plugin: collectBlock } = require("mineflayer-collectblock");
const { plugin: pvpPlugin }    = require("mineflayer-pvp");
const fs   = require("fs");
const path = require("path");
const http = require("http");

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

function createBot() {
  if (bot) {
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

  bot.once("spawn", () => {
    const mcData = require("minecraft-data")(bot.version);
    const moves  = new Movements(bot, mcData);
    bot.pathfinder.setMovements(moves);
    botReady = true;
    console.log("[mc] bot spawned and ready");
    pushBotContext();
  });

  bot.on("death", () => {
    botReady = false;
    console.log("[mc] bot died");
    pushBotContext();
  });

  bot.on("respawn", () => {
    const mcData = require("minecraft-data")(bot.version);
    const moves  = new Movements(bot, mcData);
    bot.pathfinder.setMovements(moves);
    botReady = true;
    console.log("[mc] bot respawned");
    pushBotContext();
  });

  bot.on("chat", (username, message) => {
    if (username === bot.username) return;
    pushBotContext(`[chat] ${username}: ${message}`);
  });

  bot.on("error", (err) => console.error("[mc] error:", err.code || err.message || err));

  const thisBot = bot;
  bot.on("end", (reason) => {
    // Ignore ends from a bot already replaced by a /config reconnect.
    if (bot !== thisBot) return;
    botReady = false;
    console.log(`[mc] disconnected from ${MC_HOST}:${MC_PORT}:`, reason);
    setTimeout(() => { if (bot === thisBot) createBot(); }, 10000);
  });
}

function buildStatusText() {
  if (!bot || !botReady) return "[MinecraftBot] Not connected to Minecraft server.";
  const pos   = bot.entity.position;
  const items = bot.inventory.items().map(i => `${i.name}×${i.count}`).join(", ") || "empty";
  return [
    `[MinecraftBot] ${bot.username} on ${MC_HOST}:${MC_PORT}`,
    `Position: ${pos.x.toFixed(1)}, ${pos.y.toFixed(1)}, ${pos.z.toFixed(1)}`,
    `Health: ${(bot.health ?? "?")}/20  Food: ${(bot.food ?? "?")}/20`,
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

function registerActions() {
  sendJson({
    command: "actions/register",
    game: GAME_NAME,
    data: {
      actions: [
        {
          name: "minecraft_get_status",
          description: "Return the Minecraft bot's current position, health, food level, and inventory.",
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

const HOSTILE_MOBS = new Set([
  "zombie", "skeleton", "creeper", "spider", "enderman", "witch",
  "slime", "cave_spider", "blaze", "ghast", "zombie_pigman",
  "wither_skeleton", "phantom", "drowned", "husk", "stray", "pillager",
  "vindicator", "ravager", "hoglin", "zoglin", "piglin_brute",
]);

async function handleAttackNearest(args) {
  if (!botReady) return { success: false, message: "bot is not connected to Minecraft" };
  const filter = args.mob_type ? String(args.mob_type).toLowerCase() : null;

  let nearest = null, minDist = Infinity;
  for (const entity of Object.values(bot.entities)) {
    if (entity === bot.entity || entity.type !== "mob") continue;
    const name = (entity.name || entity.mobType || "").toLowerCase().replace(/\s+/g, "_");
    if (filter ? name !== filter : !HOSTILE_MOBS.has(name)) continue;
    const dist = bot.entity.position.distanceTo(entity.position);
    if (dist < minDist) { minDist = dist; nearest = entity; }
  }

  if (!nearest) return { success: false, message: `No ${filter || "hostile mob"} found nearby` };
  bot.attack(nearest);
  return { success: true, message: `Attacking ${nearest.name || nearest.mobType} at distance ${minDist.toFixed(1)}` };
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
async function runVoyager(code, label) {
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

  let timer;
  try {
    const fn = new AsyncFunction(
      "bot", "mcData", "Vec3", "Movements", "goals", ...GOAL_NAMES,
      FAIL_COUNTERS + voyagerPrograms + "\n" + code,
    );
    const result = await Promise.race([
      fn(bot, mcData, Vec3, Movements, goals, ...GOAL_NAMES.map(n => goals[n])),
      new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error(`timed out after ${SKILL_TIMEOUT_MS / 1000} s`)),
                           SKILL_TIMEOUT_MS);
      }),
    ]);
    const parts = [result !== undefined ? String(result) : "Skill completed.", ...chatLog.slice(-8)];
    return { success: true, message: parts.join(" | ") };
  } catch (e) {
    try { bot.pathfinder.stop(); bot.pvp.stop(); } catch (_) {}
    const feedback = chatLog.length ? " | " + chatLog.slice(-8).join(" | ") : "";
    return { success: false, message: `Skill error: ${e.message}${feedback}` };
  } finally {
    clearTimeout(timer);
    bot.removeListener("chatEvent", onChat);
  }
}

async function handleExecuteSkill(args) {
  const code = String(args.code || "").trim();
  if (!code) return { success: false, message: "code cannot be empty" };
  return runVoyager(code, args.description || "custom skill");
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
  return runVoyager(`await mineBlock(bot, "${a.name}", ${count(a.count)});`, `mineBlock ${a.name}`);
}

async function handleCraftItem(a) {
  if (badId(a.name)) return { success: false, message: "name must be an item id like wooden_pickaxe" };
  return runVoyager(`await craftItem(bot, "${a.name}", ${count(a.count)});`, `craftItem ${a.name}`);
}

async function handleSmeltItem(a) {
  const fuel = a.fuel || "coal";
  if (badId(a.name) || badId(fuel)) return { success: false, message: "name and fuel must be item ids" };
  return runVoyager(`await smeltItem(bot, "${a.name}", "${fuel}", ${count(a.count)});`, `smeltItem ${a.name}`);
}

async function handlePlaceItem(a) {
  if (badId(a.name)) return { success: false, message: "name must be an item id like crafting_table" };
  const xyz = [a.x, a.y, a.z].map(Number);
  const pos = xyz.every(n => Number.isFinite(n))
    ? `new Vec3(${xyz.map(Math.floor).join(", ")})`
    : "bot.entity.position.offset(1, 0, 0)";
  return runVoyager(`await placeItem(bot, "${a.name}", ${pos});`, `placeItem ${a.name}`);
}

async function handleKillMob(a) {
  if (badId(a.name)) return { success: false, message: "name must be a mob id like zombie" };
  return runVoyager(`await killMob(bot, "${a.name}", 60);`, `killMob ${a.name}`);
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
};

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
    res.end(JSON.stringify({ host: MC_HOST, port: MC_PORT, username: MC_USERNAME, auth: MC_AUTH }));

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
        const result  = await handler(payload.args || {});
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
    result = await handler(args);
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

createBot();
connect();
