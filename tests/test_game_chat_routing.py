"""Minecraft and VTube Studio connection errors are handled quietly, chat
routes Minecraft orders to the bot, and the bot answers orders in game in
Lumi's own words."""

import asyncio
import os
import queue
import socket
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run(coro):
    return asyncio.run(coro)


def dead_url():
    """An http:// address nothing listens on (a refused connection)."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


# ── Task 1: Minecraft bot offline ─────────────────────────────────

def test_the_agent_reports_an_offline_bot_once_and_returns_a_clean_result(monkeypatch):
    mb = pytest.importorskip("plugin.minecraft_bot")
    monkeypatch.setattr(mb, "BOT_API", dead_url())
    agent = mb.MinecraftAgent.__new__(mb.MinecraftAgent)
    logged = []
    agent._record = lambda cat, msg: logged.append((cat, msg))
    for _ in range(3):
        r = run(agent._bot_action_raw("stop", {}))
        assert r == {"success": False, "error": "offline", "message": "Minecraft bot is offline"}
    assert logged == [("warn", "Minecraft bot is offline, cannot execute action")]
    assert not any("WinError" in m or "urlopen" in m for _, m in logged)


def test_the_games_page_hud_action_gets_a_clean_offline_result(monkeypatch, capsys):
    from modules import game_manager as gm
    monkeypatch.setattr(gm, "BOT_API", dead_url())
    games = gm.GameManager.__new__(gm.GameManager)
    games.signals = types.SimpleNamespace()
    r = games.hud_action("minecraft", next(iter(gm.HUD_ACTIONS)), {"name": "dirt", "count": 1})
    assert r == {"success": False, "error": "offline",
                 "message": "Minecraft bot is offline, cannot execute action"}
    out = capsys.readouterr().out
    assert "Minecraft bot is offline" in out and "WinError" not in out and "Traceback" not in out


# ── Task 1: VTube Studio closed ───────────────────────────────────

def test_a_refused_vts_connection_is_recognised_however_it_is_wrapped():
    from modules import vtubeStudio as vs
    win = OSError(10061, "refused")
    win.winerror = 1225
    assert vs.vts_refused(ConnectionRefusedError(10061, "No connection could be made"))
    assert vs.vts_refused(win)
    try:
        try:
            raise ConnectionRefusedError("x")
        except ConnectionRefusedError as inner:
            raise RuntimeError("websocket failed") from inner
    except RuntimeError as wrapped:
        assert vs.vts_refused(wrapped)
    assert not vs.vts_refused(ValueError("bad token"))


def test_closed_vts_logs_one_clean_line(capsys):
    from modules import vtubeStudio as vs

    class ClosedVTS:
        async def connect(self):
            e = OSError(1225, "[WinError 1225] The remote computer refused the network connection")
            e.winerror = 1225
            raise e

    signals = types.SimpleNamespace(terminate=False, AI_speaking=False, sio_queue=queue.SimpleQueue(),
                                    last_message_time=0.0)
    v = vs.VtubeStudio(signals, enabled=True)
    v.vts = ClosedVTS()
    for _ in range(3):                       # the first try and two reconnects
        assert run(v.connect_and_auth()) is False
    out = capsys.readouterr().out
    assert out.count("VTube Studio is closed. Avatar features disabled.") == 1
    assert "1225" not in out and "WinError" not in out and "Traceback" not in out
    assert signals.vts_detail == "VTube Studio is closed"


# ── Task 2: web chat → Minecraft ──────────────────────────────────

def test_web_chat_orders_with_her_name_go_to_the_bot():
    from modules.game_orders import extract_order
    assert extract_order("Lumi, follow me") == "follow me"
    assert extract_order("Lumi, mine some wood") == "mine some wood"
    assert extract_order("Lumi, how do I make a stone axe?") is None


def test_minecraft_talk_without_her_name_gets_a_turn_with_the_tool():
    from modules.prompter import mentions_minecraft
    for t in ("in minecraft, follow me", "follow me", "can you mine some wood?", "stay here", "chop 5 logs"):
        assert mentions_minecraft(t), t
    for t in ("how is your day", "give me a hug", "build me a website", "dig into this bug",
              "I'll follow up tomorrow", "collect my thoughts"):
        assert not mentions_minecraft(t), t


def test_the_game_tool_is_offered_whenever_the_game_agent_is_on():
    from modules import prompter as pm
    p = pm.Prompter.__new__(pm.Prompter)
    p.signals = types.SimpleNamespace(game_manager=types.SimpleNamespace(is_active=lambda g: True), sdk=None)
    assert p._minecraft_enabled() and not p._minecraft_connected()
    p.signals.game_manager = types.SimpleNamespace(is_active=lambda g: False)
    assert not p._minecraft_enabled()
    src = open(os.path.join(ROOT, "modules", "prompter.py"), encoding="utf-8").read()
    assert "if self._minecraft_enabled():           # always, while the game agent is on\n" \
           "            all_tools = all_tools + [GAME_COMMAND_TOOL_SCHEMA]" in src
    assert ("If the user talks to you about Minecraft (e.g., 'follow me', 'stop', 'mine this'), "
            "use the Minecraft tool to send the command directly to the bot.") in pm.MINECRAFT_GAMER_NOTE


# ── Task 3: natural in-game answers ───────────────────────────────

def test_model_lines_are_cleaned_and_robotic_ones_rejected():
    mb = pytest.importorskip("plugin.minecraft_bot")
    assert mb.clean_ack('"Right behind you!"') == "Right behind you!"
    assert mb.clean_ack("Lumi: On my way") == "On my way!"
    assert mb.clean_ack("Executing action: follow_player") is None
    assert mb.clean_ack("Sure thing, I will now begin to follow you around the whole world") is None
    assert mb.clean_ack("") is None
    for kind in ("follow", "come", "stay", "collect", "once"):
        line = mb.stock_ack(kind)
        assert len(line.split()) < 10 and not mb._ROBOTIC.search(line)


def _agent(mb, reply):
    from modules import minecraft_social as social
    agent = mb.MinecraftAgent.__new__(mb.MinecraftAgent)
    agent._cloud_pause = None
    agent._model_choice = "local"
    agent._social = social.SocialMemory()
    agent._persona = social.load_persona()
    agent._persona_text = social.persona_prompt(agent._persona)
    agent._last_status = {"players": [{"name": "Steve", "distance": 4.0}], "owner": {"name": "Steve"}}
    agent._record = lambda *a, **k: None
    agent._model_ready = lambda: True
    said, asked = [], []

    async def ask(system, prompt, options):
        asked.append((system, prompt))
        if isinstance(reply, Exception):
            raise reply
        return reply

    async def act(name, args, intent=None):
        said.append((name, args["message"]))
        return {"success": True}
    agent._ask_model = ask
    agent._bot_action = act
    return agent, said, asked


def test_an_order_is_answered_in_game_with_the_models_line():
    mb = pytest.importorskip("plugin.minecraft_bot")
    agent, said, asked = _agent(mb, "Right behind you, Steve!")
    run(agent._ack_in_game("follow me", "follow", "Steve"))
    assert said == [("chat", "Right behind you, Steve!")]
    system, prompt = asked[0]
    assert "Lumi" in system and '"follow me"' in prompt and "under 10 words" in prompt


def test_a_model_failure_still_gets_a_natural_line():
    mb = pytest.importorskip("plugin.minecraft_bot")
    for reply in (RuntimeError("ollama down"), mb.CloudModelError("Claude", "timeout"),
                  "Executing action: follow_player"):
        agent, said, _ = _agent(mb, reply)
        run(agent._ack_in_game("follow me", "follow", "Steve"))
        assert said[0][1] in mb._STOCK_ACKS["follow"], reply
        assert agent._cloud_pause is None                    # a chat line never pauses the agent


def test_bot_js_no_longer_answers_got_it_in_game():
    src = open(os.path.join(ROOT, "sdk", "examples", "minecraft", "bot.js"), encoding="utf-8").read()
    assert "Got it. ${r.directive.text}" not in src
    assert "ORDER_ANSWER_WAIT_MS" in src
