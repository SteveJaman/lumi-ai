"""Minecraft agent: action governor, action schema, per-game model choice,
cloud-error pause and resume, refusal feedback. No game, no network."""

import asyncio
import json
import os
import re
import time

import pytest

import constants
from modules import minecraft_governor as gov
from modules import model_router as mr
from modules.minecraft_actions import ACTIONS, render_actions_prompt

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = {"x": 100, "y": 64, "z": -50}


def state(**kw):
    s = {"position": HERE, "inventory": {}, "placed": [], "players": ["Steve"],
         "food": ["bread", "cooked_beef"]}
    s.update(kw)
    return s


# ─── governor rules ───────────────────────────────────────────────

def test_admits_a_normal_mining_action():
    assert gov.check("mine_block", {"name": "stone", "count": 8}, state()) == {"admit": True, "reason": ""}


def test_refuses_coordinates_outside_the_box():
    v = gov.check("move_to", {"x": 100 + 129, "y": 64, "z": -50}, state())
    assert not v["admit"] and "outside" in v["reason"]
    assert not gov.check("move_to", {"x": 100, "y": 64 + 65, "z": -50}, state())["admit"]
    assert gov.check("move_to", {"x": 100 + 128, "y": 64 + 64, "z": -50}, state())["admit"]


def test_box_exempts_following_and_going_home():
    far = {"x": 5000, "y": 64, "z": 5000}
    assert gov.check("move_to", far, state(), intent="go_home")["admit"]
    assert gov.check("move_to", far, state(), intent="return_home")["admit"]
    assert gov.check("move_to", far, state(), intent="follow_player")["admit"]


def test_box_refuses_when_position_unknown():
    assert not gov.check("move_to", {"x": 1, "y": 2, "z": 3}, state(position=None))["admit"]


def test_self_grief_guard():
    now = 1000.0
    placed = gov.remember_placement([], "place_block", {"name": "dirt", "x": 101, "y": 64, "z": -50}, now=now)
    v = gov.check("dig", {"x": 101, "y": 64, "z": -50}, state(placed=placed), now=now + 60)
    assert not v["admit"] and "placed" in v["reason"]
    assert gov.check("dig", {"x": 101, "y": 64, "z": -50}, state(placed=placed), now=now + 301)["admit"]


def test_last_tool_and_last_food():
    inv = {"stone_pickaxe": 1, "dirt": 10, "bread": 2}
    assert not gov.check("drop_item", {"name": "stone_pickaxe"}, state(inventory=inv))["admit"]
    assert not gov.check("drop_item", {"name": "bread", "count": 2}, state(inventory=inv))["admit"]
    assert gov.check("drop_item", {"name": "bread", "count": 1}, state(inventory=inv))["admit"]
    assert gov.check("drop_item", {"name": "dirt"}, state(inventory=inv))["admit"]
    two = dict(inv, wooden_axe=1)
    assert gov.check("drop_item", {"name": "stone_pickaxe"}, state(inventory=two))["admit"]


def test_never_attacks_a_player_by_default():
    assert not gov.check("kill_mob", {"name": "Steve"}, state())["admit"]
    assert not gov.check("attack_entity", {"name": "player"}, state())["admit"]
    assert gov.check("kill_mob", {"name": "zombie"}, state())["admit"]
    cfg = gov.Config(attack_allowlist=("steve",))
    assert gov.check("kill_mob", {"name": "Steve"}, state(), cfg=cfg)["admit"]


@pytest.mark.parametrize("text", ["join http://evil.example/x", "go to www.site.org", "visit evil.com now",
                                  "mail me at a.b@example.com"])
def test_refuses_chat_with_a_url_or_email(text):
    assert not gov.check("chat", {"message": text}, state())["admit"]
    assert not gov.check("chat_send", {"content": text}, state())["admit"]


def test_plain_chat_is_fine():
    assert gov.check("chat", {"message": "Got it. Getting wood, e.g. oak."}, state())["admit"]


def test_governor_is_pure_and_cheap():
    s = state(placed=[(1, 2, 3, 0.0)])
    before = json.dumps(s, sort_keys=True)
    t0 = time.perf_counter()
    for _ in range(2000):
        gov.check("move_to", {"x": 1, "y": 2, "z": 3}, s)
    assert time.perf_counter() - t0 < 1.0
    assert json.dumps(s, sort_keys=True) == before


# ─── action schema ────────────────────────────────────────────────

def test_actions_match_bot_js():
    with open(os.path.join(ROOT, "sdk", "examples", "minecraft", "bot.js"), encoding="utf-8") as f:
        js = f.read()
    handlers = set(re.findall(r"^\s+minecraft_([a-z_]+):\s+handle", js, re.M))
    names = [a["name"] for a in ACTIONS]
    assert len(names) == len(set(names)) and 18 <= len(names) <= 26
    assert set(names) <= handlers, set(names) - handlers


def test_prompt_is_stable_and_compact():
    goals = [("chop_trees", "Chop trees"), ("explore", "Explore")]
    a, b = render_actions_prompt(goals), render_actions_prompt(goals)
    assert a == b and a.startswith("What you can do") and "- chop_trees: Chop trees" in a
    assert "Your body's actions" not in a and "move_to(" in render_actions_prompt(goals, detail=True)


def test_no_vtube_references():
    for name in ("minecraft_governor.py", "minecraft_actions.py"):
        with open(os.path.join(ROOT, "modules", name), encoding="utf-8") as f:
            assert not re.search(r"vts|vtube", f.read(), re.I)


# ─── per-game model choice ────────────────────────────────────────

@pytest.fixture
def yaml_copy(tmp_path, monkeypatch):
    path = tmp_path / "lumi.yaml"
    path.write_text("# persona\nname: Lumi  # keep me\nhost: <host>\n", encoding="utf-8")
    monkeypatch.setattr(constants, "YAML_PATH", str(path))
    mr._choice_cache.update(mtime=None, data={})
    return path


def test_default_is_local(yaml_copy):
    assert mr.agent_model_choice("minecraft") == "local"
    assert mr.agent_model_spec() == constants.GAME_AGENT_MODEL
    assert mr.agent_model_spec(model_override="local") == constants.GAME_AGENT_MODEL


def test_override_routes_to_the_provider(monkeypatch):
    monkeypatch.setattr(mr, "provider_has_key", lambda c: True)
    assert mr.agent_model_spec(model_override="claude") == constants.MINECRAFT_AGENT_CLOUD_MODELS["claude"]


def test_missing_key_raises(monkeypatch):
    monkeypatch.setattr(mr, "provider_has_key", lambda c: False)
    with pytest.raises(mr.CloudKeyMissing, match="Claude key not set"):
        mr.agent_model_spec(model_override="claude")


def test_set_agent_model_persists_to_lumi_yaml(yaml_copy, tmp_path, monkeypatch):
    import modules.game_manager as gm
    monkeypatch.setattr(gm, "CONTROL_PATH", str(tmp_path / "agent_control.json"))
    monkeypatch.setattr(gm, "MC_DIR", str(tmp_path))
    monkeypatch.setattr(mr, "provider_has_key", lambda c: c == "claude")
    monkeypatch.setattr(gm, "hub_emit", lambda *a, **k: None)
    manager = gm.GameManager.__new__(gm.GameManager)
    manager._children, manager.signals = {}, None

    out = manager.set_agent_model("minecraft", "claude")
    text = yaml_copy.read_text(encoding="utf-8")
    assert "games:" in text and "agent_model: claude" in text and "# keep me" in text
    assert out["agent_model"] == "claude" and out["available"] == ["local", "claude"]
    assert mr.agent_model_choice("minecraft") == "claude"
    assert json.loads((tmp_path / "agent_control.json").read_text())["seq"] == 1

    with pytest.raises(LookupError, match="OpenAI key not set"):
        manager.set_agent_model("minecraft", "openai")
    assert "agent_model: claude" in yaml_copy.read_text(encoding="utf-8")   # unchanged
    with pytest.raises(ValueError):
        manager.set_agent_model("minecraft", "grok")


def test_routes_are_host_only():
    from modules.socketIO import _REMOTE_API_ALLOW
    assert not any("agent_model" in p for _, p in _REMOTE_API_ALLOW)


# ─── the agent: pause, resume, refusal feedback ───────────────────

@pytest.fixture
def agent(tmp_path, monkeypatch, yaml_copy):
    mb = pytest.importorskip("plugin.minecraft_bot")
    for name in ("TELEMETRY_PATH", "EVENTS_PATH", "CONTROL_PATH"):
        monkeypatch.setattr(mb, name, str(tmp_path / os.path.basename(getattr(mb, name))))
    monkeypatch.setattr(mb, "AUTOPLAY_PATH", str(tmp_path / "autoplay.json"))
    a = mb.MinecraftAgent()
    a._last_status = {"position": HERE, "inventory": [{"name": "stone_pickaxe", "count": 1}],
                      "owner": {"name": "Steve"}}
    sent = []

    async def raw(name, args):
        sent.append((name, args))
        return {"success": True, "message": "ok"}
    a._bot_action_raw = raw
    a._sent = sent
    return mb, a


# Past the tech tree (stone kit, a bed remembered), so the model picks.
KIT = {"is_day": True, "inventory": [{"name": "stone_pickaxe", "count": 1},
                                     {"name": "stone_axe", "count": 1},
                                     {"name": "stone_sword", "count": 1}],
       "nearby": {}, "position": HERE, "bed": {"remembered": True}}


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def test_refused_actions_never_reach_bot_js(agent):
    mb, a = agent
    r = run(a._bot_action("chat", {"message": "free stuff at http://x.example"}))
    assert not r["success"] and r["message"].startswith("action refused: ")
    assert a._sent == []
    assert a._tel["last_action"]["admitted"] is False and "URL" in a._tel["last_action"]["reason"]
    r = run(a._bot_action("mine_block", {"name": "stone", "count": 3}))
    assert r["success"] and a._sent == [("mine_block", {"name": "stone", "count": 3})]
    assert a._tel["last_action"] == dict(a._tel["last_action"], name="mine_block", admitted=True)


def test_local_model_prompt_is_unchanged_but_for_the_preamble(agent, monkeypatch):
    mb, a = agent
    calls = []

    def fake_chat(spec, messages, **kw):
        calls.append((spec, messages, kw))
        return type("R", (), {"content": '{"goal": "explore", "reason": "x"}'})()
    monkeypatch.setattr(mb, "chat_sync", fake_chat)
    a._refresh_model_info()
    picked = run(a._llm_pick({"is_day": True}, {"explore": "Explore", "chop_trees": "Chop"}))
    assert picked[0] == "explore"
    spec, messages, kw = calls[0]
    assert spec == mb.GOAL_MODEL and kw["keep_alive"] == mb.GOAL_KEEP_ALIVE
    assert messages[0]["content"] == mb.PICKER_SYSTEM            # no frontier directive
    assert "What you can do" in messages[1]["content"] and "Your body's actions" not in messages[1]["content"]
    assert len(calls) == 1                                       # one call, no second LLM


def test_cloud_uses_override_and_directive(agent, monkeypatch, tmp_path):
    mb, a = agent
    monkeypatch.setattr(mr, "agent_model_choice", lambda g="minecraft": "claude")
    monkeypatch.setattr(mr, "provider_has_key", lambda c: True)
    calls = []

    def fake_chat(spec, messages, **kw):
        calls.append((spec, messages))
        return type("R", (), {"content": '{"goal": "chop_trees", "reason": "wood"}'})()
    monkeypatch.setattr(mb, "chat_sync", fake_chat)
    a._refresh_model_info()
    assert a._tel["agent_model"]["label"] == "Claude"
    picked = run(a._llm_pick({"is_day": True}, {"explore": "Explore", "chop_trees": "Chop"}))
    assert picked[0] == "chop_trees"
    assert calls[0][0] == constants.MINECRAFT_AGENT_CLOUD_MODELS["claude"]
    assert mb.FRONTIER_DIRECTIVE in calls[0][1][0]["content"] and len(mb.FRONTIER_DIRECTIVE) < 400


def test_cloud_timeout_pauses_then_resume_retries_once(agent, monkeypatch):
    mb, a = agent
    monkeypatch.setattr(mr, "agent_model_choice", lambda g="minecraft": "claude")
    monkeypatch.setattr(mr, "provider_has_key", lambda c: True)
    monkeypatch.setattr(mb, "_CLOUD_TIMEOUT_S", 0.05)
    calls = []

    def slow_chat(spec, messages, **kw):
        calls.append(spec)
        time.sleep(0.3)
        return type("R", (), {"content": ""})()
    monkeypatch.setattr(mb, "chat_sync", slow_chat)
    a._refresh_model_info()
    status = KIT
    monkeypatch.setattr(a, "_should_return", lambda st: None)
    assert run(a._pick_goal(status)) is None
    assert a._cloud_pause and "timed out" in a._cloud_pause["error"]
    assert a._tel["loop_state"] == "paused_cloud_error" and len(calls) == 1

    # Resume: a new control seq lifts the pause; the next decision tries once.
    with open(mb.CONTROL_PATH, "w") as f:
        json.dump({"seq": 1}, f)
    a._check_control()                       # startup read
    with open(mb.CONTROL_PATH, "w") as f:
        json.dump({"seq": 2}, f)
    a._check_control()
    assert a._cloud_pause is None
    run(a._pick_goal(status))
    assert a._cloud_pause and len(calls) == 2  # tried once more, paused again


def test_missing_key_pauses_with_a_clear_message(agent, monkeypatch):
    mb, a = agent
    monkeypatch.setattr(mr, "agent_model_choice", lambda g="minecraft": "openai")
    monkeypatch.setattr(mr, "provider_has_key", lambda c: False)
    monkeypatch.setattr(mb, "chat_sync", lambda *x, **k: pytest.fail("no call without a key"))
    a._refresh_model_info()
    monkeypatch.setattr(a, "_should_return", lambda st: None)
    run(a._pick_goal(KIT))
    assert a._cloud_pause["error"] == "OpenAI key not set"
    assert "Minecraft agent paused: OpenAI key not set" in a._tel["current_thought"]


def test_refused_pick_is_fed_back_and_replaced(agent, monkeypatch):
    mb, a = agent
    far = {"desc": "Walk somewhere far", "when": lambda inv, st: True,
           "action": ("move_to", lambda st: {"x": 9000, "y": 64, "z": 9000})}
    monkeypatch.setitem(mb.FREE_GOALS, "visit_far", far)
    prompts = []

    def fake_chat(spec, messages, **kw):
        prompts.append(messages[1]["content"])
        goal = "visit_far" if len(prompts) == 1 else "explore"
        return type("R", (), {"content": json.dumps({"goal": goal, "reason": "r"})})()
    monkeypatch.setattr(mb, "chat_sync", fake_chat)
    a._refresh_model_info()
    monkeypatch.setattr(a, "_should_return", lambda st: None)
    status = {"is_day": True, "inventory": [{"name": "stone_pickaxe", "count": 1},
                                             {"name": "stone_axe", "count": 1},
                                             {"name": "stone_sword", "count": 1}],
              "nearby": {}, "position": HERE, "bed": {"remembered": True}}
    a._last_status = status
    goal, source, _ = run(a._pick_goal(status))
    assert (goal, source) == ("explore", "llm")
    assert "Observation: action refused:" in prompts[1] and "visit_far" not in prompts[1].split("Goals you can pick:")[1]
    assert a._tel["last_action"]["name"] == "move_to" and not a._tel["last_action"]["admitted"]
