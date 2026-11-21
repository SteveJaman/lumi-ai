"""Lumi's Minecraft social side (modules/minecraft_social.py and its wiring in
plugin/minecraft_bot.py), and bot.js's moving-target / stuck / protocol-error
handling, run in node against a simulated world."""

import asyncio
import json
import os
import random
import re
import shutil
import subprocess

import pytest

from modules import minecraft_social as social

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOT_JS = os.path.join(ROOT, "sdk", "examples", "minecraft", "bot.js")
NODE = shutil.which("node")


def run(coro):
    return asyncio.run(coro)


STATUS = {
    "ready": True, "health": 20, "food": 20, "is_day": True, "busy": False,
    "owner": {"name": "SteveJaman", "distance": 5.0},
    "players": [{"name": "Alex", "distance": 30.5}, {"name": "SteveJaman", "distance": 5.0}],
    "threats": [],
}


# ── persona ───────────────────────────────────────────────────────

def test_persona_comes_from_lumi_yaml_traits():
    p = social.load_persona()
    assert p["name"] == "Lumi"
    for k in ("playfulness", "intelligence", "chaos", "empathy", "sarcasm"):
        assert k in p["traits"]
    text = social.persona_prompt(p)
    assert "sarcasm 0.75: be dry and sarcastic" in text
    assert "patience 0.35: complain about tedious work like mining or chopping" in text
    assert "Never mention being an AI" in text


# ── nearby players + social memory ────────────────────────────────

def test_nearby_players_block_lists_names_within_32_blocks_with_memory():
    mem = social.SocialMemory()
    mem.seen(["SteveJaman", "Alex"])
    assert mem.chat("SteveJaman", "Hi Lumi!") is True          # a first greeting
    assert mem.chat("SteveJaman", "hi again") is False         # remembered already
    mem.gift("SteveJaman", "oak_log", 16)
    far = dict(STATUS, players=STATUS["players"] + [{"name": "Faraway", "distance": 80}])
    block = social.nearby_players_block(far, mem)
    assert block.startswith("[NEARBY PLAYERS]") and block.endswith("[END NEARBY PLAYERS]")
    lines = block.splitlines()[1:-1]
    assert lines[0].startswith("- SteveJaman (5 blocks, your player)")   # nearest first
    assert "said hi to you" in lines[0] and "you gave them 16 oak log" in lines[0]
    assert lines[1].startswith("- Alex (30 blocks)")
    assert "Faraway" not in block
    assert "nobody within 32 blocks" in social.nearby_players_block({"players": []})


def test_gift_messages_from_bot_js_parse():
    m = social.GIFT_RE.match("Gave 16 oak_log to SteveJaman")
    assert m.groups() == ("16", "oak_log", "SteveJaman")


# ── banter rules ──────────────────────────────────────────────────

def test_she_answers_her_name_and_only_sometimes_otherwise():
    rng = random.Random(1)
    kw = dict(near=True, now=100.0, last_reply=0.0, last_reply_to={})
    named = sum(social.wants_reply("Steve", "Lumi, you're doing great", rng=rng, **kw) for _ in range(200))
    other = sum(social.wants_reply("Steve", "nice house", rng=rng, **kw) for _ in range(200))
    assert named > 160 and 20 < other < 100
    assert not social.wants_reply("Steve", "Lumi you're crazy", near=True, now=100.0, last_reply=95.0,
                                  last_reply_to={})                     # global cooldown
    assert not social.wants_reply("Steve", "nice house", near=False, now=100.0, last_reply=0.0,
                                  last_reply_to={}, rng=random.Random(0))  # far away, not named
    assert not social.wants_reply("Steve", "/gamemode creative", near=True, now=100.0, last_reply=0.0,
                                  last_reply_to={})


def test_observations_pick_the_most_pressing_thing():
    assert social.observe(STATUS) is None
    assert social.observe(dict(STATUS, food=6))[0] == "hungry"
    assert social.observe(dict(STATUS, health=5, food=6))[0] == "hurt"
    key, fact = social.observe(dict(STATUS, threats=[{"name": "zombie", "distance": 7}], food=6))
    assert key == "mob:zombie" and "zombie" in fact
    assert social.observe(dict(STATUS, is_day=False))[0] == "night"


def test_clean_line_strips_tags_and_rejects_robotic_or_long_lines():
    assert social.clean_line("[sarcastic] Ugh, fine, I'll chop the tree") == "Ugh, fine, I'll chop the tree."
    assert social.clean_line("Executing action: follow") is None
    assert social.clean_line("one two three four five six seven eight nine ten") is None


# ── the agent: banter, observations, gifts, prompts ───────────────

@pytest.fixture
def agent():
    mb = pytest.importorskip("plugin.minecraft_bot")
    a = mb.MinecraftAgent.__new__(mb.MinecraftAgent)
    a._persona = social.load_persona()
    a._persona_text = social.persona_prompt(a._persona)
    a._social = social.SocialMemory()
    a._last_reply, a._last_reply_to, a._last_observe, a._observed = 0.0, {}, 0.0, {}
    a._cloud_pause, a._model_choice, a._order, a._gaming = None, "local", None, False
    a._override_goal = a._done_order = None
    a._last_status = STATUS
    a._tel = {"goal_description": "Chop down some trees for wood"}
    a._model_ready = lambda: True
    a._order_text = lambda: ""
    a.records, a.said, a.prompts = [], [], []
    a._record = lambda cat, msg: a.records.append((cat, msg))

    async def act(name, args, intent=None):
        a.said.append((name, args))
        return {"success": True, "message": "Gave 16 oak_log to SteveJaman" if name == "give_item" else "ok"}

    async def ask(system, prompt, options):
        a.prompts.append((system, prompt))
        return a.reply
    a._bot_action, a._ask_model, a.reply = act, ask, "Ugh, fine. You're welcome."
    return mb, a


def test_a_player_talking_to_her_gets_an_in_character_reply(agent, monkeypatch):
    mb, a = agent
    monkeypatch.setattr(social.random, "random", lambda: 0.0)
    run(a._hear({"from": "SteveJaman", "text": "Lumi, you're doing great"}, STATUS, ["SteveJaman", "Alex"]))
    assert a.said == [("chat", {"message": "Ugh, fine. You're welcome."})]
    system, prompt = a.prompts[0]
    assert "sarcasm" in system and "[NEARBY PLAYERS]" in prompt
    assert '"Lumi, you\'re doing great"' in prompt and "Chop down some trees" in prompt


def test_a_greeting_is_remembered_and_answered_even_without_a_model(agent, monkeypatch):
    mb, a = agent
    monkeypatch.setattr(social.random, "random", lambda: 0.0)
    a._model_ready = lambda: False
    run(a._hear({"from": "Alex", "text": "hi lumi"}, STATUS, ["SteveJaman", "Alex"]))
    assert a.said == [("chat", {"message": "Hey Alex."})]
    assert a._social.people["Alex"]["greeted"]
    assert ("social", "Alex said hi; remembering them this session") in a.records


def test_she_remarks_on_hunger_when_idle_near_players_but_not_twice(agent):
    mb, a = agent
    a.reply = "I'm starving over here."
    hungry = dict(STATUS, food=5)
    run(a._maybe_observe(hungry, ["SteveJaman"]))
    run(a._maybe_observe(hungry, ["SteveJaman"]))                # same thing within 5 min: quiet
    assert a.said == [("chat", {"message": "I'm starving over here."})]
    assert "getting hungry" in a.prompts[0][1]
    a.said.clear()
    run(a._maybe_observe(dict(hungry, busy=True), ["SteveJaman"]))   # busy: quiet
    run(a._maybe_observe(dict(STATUS, health=4), []))                # nobody around: quiet
    assert a.said == []


def test_an_order_ack_sees_who_is_around_and_she_logs_gifts(agent):
    mb, a = agent
    a.reply = "Fine. Logs incoming."
    run(a._ack_in_game("get me some wood", "collect", "SteveJaman"))
    assert a.said[-1] == ("chat", {"message": "Fine. Logs incoming."})
    assert "[NEARBY PLAYERS]" in a.prompts[-1][1] and a._social.people["SteveJaman"]["orders"] == 1
    a._order = {"give": {"item_name": "oak_log", "count": 16}}
    ok, _ = run(a._execute_goal("give_item", STATUS))
    assert ok and a._social.people["SteveJaman"]["gifts"] == [("oak_log", 16)]


def test_goal_timeouts_are_120_s():
    mb = pytest.importorskip("plugin.minecraft_bot")
    assert mb.GOAL_TIMEOUT_S["chop_trees"] == 120
    assert all(v == 120 for v in mb.GOAL_TIMEOUT_S.values())


def test_the_main_agent_is_told_to_mention_what_she_is_doing_in_minecraft():
    from modules import prompter
    assert "I'm busy mining, tell them I'll be there soon." in prompter.MINECRAFT_GAMER_NOTE


# ── bot.js in node, against a simulated world ─────────────────────

def _helpers_js():
    src = open(BOT_JS, encoding="utf-8").read()
    a = src.index("// Stuck watchdog for anything that walks")
    b = src.index("// mineflayer/protodef print a full stack")
    return src[a:b].replace("const STUCK_CHECK_MS = 5000;", "const STUCK_CHECK_MS = 300;")


HARNESS = r"""
const Vec3 = require(%(vec3)s);
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
let interruptSeq = 0, pendingDirective = null, stopped = 0, jumps = 0, repaths = 0;
const log = []; const _logEv = (k, m) => log.push(k + ":" + m);
const goals = { GoalFollow: class { constructor(e, r) { this.entity = e; this.range = r; } } };
const survivalMovements = () => ({});
const player = { position: new Vec3(10, 64, 0) };
let goal = null, blocked = %(blocked)s;
const bot = {
  entity: { position: new Vec3(0, 64, 0) },
  players: { Steve: { entity: player } },
  pathfinder: {
    get goal() { return goal; }, movements: {},
    setGoal(g) { goal = g; }, isMoving() { return !!goal; }, isMining() { return false; },
    isBuilding() { return false; }, setMovements() { repaths++; },
  },
  getControlState() { return false; },
  setControlState(c, v) { if (c === "jump" && v) jumps++; },
};
function stopAllActivity() { stopped++; goal = null; }
%(helpers)s
// the world: Steve walks away for 1.5 s, then stops; Lumi walks toward her goal
const t0 = Date.now();
setInterval(() => {
  if (Date.now() - t0 < 1500) player.position = player.position.offset(0.25, 0, 0);
  if (goal && !blocked) {
    const d = goal.entity.position.minus(bot.entity.position);
    const n = Math.hypot(d.x, d.z) || 1;
    bot.entity.position = bot.entity.position.offset(0.45 * d.x / n, 0, 0.45 * d.z / n);
  }
}, 50);
(async () => {
  %(scenario)s
})().then(r => { console.log(JSON.stringify(Object.assign({ jumps, repaths, stopped }, r))); process.exit(0); });
"""


def _node(scenario, blocked=False):
    vec3 = os.path.join(ROOT, "sdk", "examples", "minecraft", "node_modules", "vec3")
    if not os.path.isdir(vec3):
        pytest.skip("bot.js node_modules not installed")
    js = HARNESS % {"vec3": json.dumps(vec3), "helpers": _helpers_js(), "scenario": scenario,
                    "blocked": "true" if blocked else "false"}
    out = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_give_chases_a_player_who_keeps_walking():
    r = _node("const g = await approachPlayer('Steve', 3, 10000);"
              "return { ok: g.ok, why: g.why || '', dist: bot.entity.position.distanceTo(player.position),"
              " moved: player.position.x - 10 };")
    assert r["ok"] and r["dist"] <= 3 and r["moved"] > 5          # Steve moved, she still got there
    assert r["stopped"] == 1                                      # the goal is cleared after


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_a_follow_order_on_the_way_does_not_cancel_the_delivery_but_stop_does():
    follow = _node("setTimeout(() => { interruptSeq++; pendingDirective = { text: 'follow me' }; goal = null; }, 400);"
                   "const g = await approachPlayer('Steve', 3, 10000); return { ok: g.ok, why: g.why || '' };")
    assert follow["ok"], follow
    stop = _node("setTimeout(() => { interruptSeq++; pendingDirective = { text: 'stop' }; }, 400);"
                 "const g = await approachPlayer('Steve', 3, 10000); return { ok: g.ok, why: g.why || '' };")
    assert not stop["ok"] and stop["why"] == "interrupted by a new order: stop"


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_stuck_she_jumps_then_repaths():
    r = _node("const g = await approachPlayer('Steve', 3, 1800); return { ok: g.ok, why: g.why || '' };",
              blocked=True)
    assert not r["ok"] and "couldn't reach them" in r["why"]
    assert r["jumps"] >= 1 and r["repaths"] >= 1


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_partial_read_errors_become_one_clean_line():
    src = open(BOT_JS, encoding="utf-8").read()
    a = src.index("let MINEFLAYER_VERSION")
    b = src.index('process.on("uncaughtException"')
    js = (src[a:b] +
          "for (let i = 0; i < 5; i++) console.log('PartialReadError: Read error for undefined : Missing characters "
          "in string, found size is 77 expected size was 1865\\n    at new ExtendableError (utils.js:63:13)');\n"
          "console.log('normal line still prints');\n")
    out = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=20)
    text = out.stdout + out.stderr
    assert text.count("skipped") == 1 and "Harmless" in text
    assert "ExtendableError" not in text and "normal line still prints" in text


def test_bot_js_routes_banter_and_reports_nearby_players():
    src = open(BOT_JS, encoding="utf-8").read()
    assert "queueChat(username, message);" in src
    assert 'url === "/chat"' in src and "players:   nearbyPlayers(32)" in src
    assert "if (order && !ORDER_VERB_RE.test(order)) order = null;" in src
    assert 'bot.on("kicked"' in src and "isProtocolReadError(err)" in src
