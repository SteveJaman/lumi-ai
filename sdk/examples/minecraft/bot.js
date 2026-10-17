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
const fs = require("fs");
const path = require("path");

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

const MC_HOST     = process.env.MC_HOST     || "localhost";
const MC_PORT     = parseInt(process.env.MC_PORT || "25565", 10);
const MC_USERNAME = process.env.MC_USERNAME || "LumiBot";
const MC_AUTH     = process.env.MC_AUTH     || "offline";
const MC_VERSION  = process.env.MC_VERSION  || undefined;

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
    try { bot.quit("reconnecting"); } catch (_) {}
    bot      = null;
    botReady = false;
  }

  console.log(`[mc] connecting to ${MC_HOST}:${MC_PORT} as ${MC_USERNAME}`);
  const opts = { host: MC_HOST, port: MC_PORT, username: MC_USERNAME, auth: MC_AUTH };
  if (MC_VERSION) opts.version = MC_VERSION;
  bot = mineflayer.createBot(opts);
  bot.loadPlugin(pathfinder);

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

  bot.on("error", (err) => console.error("[mc] error:", err.message));

  bot.on("end", (reason) => {
    botReady = false;
    console.log("[mc] disconnected:", reason);
    setTimeout(createBot, 10000);
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

// ─── Lumi SDK WebSocket ─────────────────────────────────────────────────────

let ws             = null;
let stopping       = false;
let reconnectTimer = null;

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
          description: "Execute a Voyager-compatible async JavaScript skill on the mineflayer bot. The code runs as an async function body and receives `bot`, `goals`, `Movements`, and `Vec3` in scope. Returns the skill's return value as a string.",
          schema: {
            type: "object",
            properties: {
              code:        { type: "string", description: "Async JavaScript code. Runs as the body of `async function(bot, goals, Movements, Vec3) { <code> }`." },
              description: { type: "string", description: "Human-readable description of what the skill does (logged, not executed)." },
            },
            required: ["code"],
          },
          capabilities: ["dangerous"],
          requires_confirm: true,
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

async function handleExecuteSkill(args) {
  if (!botReady) return { success: false, message: "bot is not connected to Minecraft" };
  const code = String(args.code || "").trim();
  if (!code) return { success: false, message: "code cannot be empty" };

  const desc = args.description ? ` (${args.description})` : "";
  console.log(`[mc] executing Voyager skill${desc}`);

  try {
    // Voyager convention: async skill body receives bot and helpers.
    const skillFn = new AsyncFunction("bot", "goals", "Movements", "Vec3", code);
    const result = await Promise.race([
      skillFn(bot, goals, Movements, Vec3),
      new Promise((_, reject) =>
        setTimeout(() => reject(new Error("skill timed out after 30 s")), 30_000)
      ),
    ]);
    return { success: true, message: result !== undefined ? String(result) : "Skill completed." };
  } catch (e) {
    return { success: false, message: `Skill error: ${e.message}` };
  }
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
};

async function dispatchAction(invId, name, rawData) {
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
  console.log(`[sdk] connecting to ${uri}`);
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
        registerActions();
        pushBotContext();
        break;
      }
      case "actions/registered":
        console.log(`[sdk] registered: ${(msg.data || {}).action_name}`);
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
      console.log(`[sdk] disconnected — reconnecting in ${RECONNECT_MS / 1000} s`);
      reconnectTimer = setTimeout(connect, RECONNECT_MS);
    }
  });

  ws.on("error", (err) => console.error("[sdk] ws error:", err.message));
}

// Push a fresh status snapshot every 30 s so Lumi stays up-to-date.
setInterval(() => { if (botReady) pushBotContext(); }, 30_000);

process.on("SIGINT", () => {
  stopping = true;
  clearTimeout(reconnectTimer);
  if (ws)  ws.close();
  if (bot) bot.quit();
  process.exit(0);
});

createBot();
connect();
