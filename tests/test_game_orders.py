"""Minecraft orders spotted in what the host says to Lumi (modules/game_orders.py),
and the gate that only sends them while the game is on."""

import json
import os
import time
import types

import pytest

import modules.game_manager as gm
from modules.game_orders import extract_order, is_explicit
from plugin.minecraft_bot import _parse_order


@pytest.mark.parametrize("said, order", [
    ("Lumi, follow me.", "follow me"),
    ("hey lumi can you please make a stone axe?", "make a stone axe"),
    ("Lumi, could you get me 5 diamonds", "get me 5 diamonds"),
    ("Lumi, collect wood", "collect wood"),
    ("Lumi, go to sleep", "go to sleep"),
    ("Lumi, stay here", "stay here"),
    ("Lumi, stop", "stop"),
    ("Lumi, in minecraft, build a house", "build a house"),
    ("Lumi, construct a house", "construct a house"),
    ("Lumi, drop the dirt", "drop the dirt"),
    ("Lumi, do your own thing", "do your own thing"),
    ("chop some trees, Lumi", "chop some trees"),
    ("!mc chop some trees", "chop some trees"),
])
def test_orders_addressed_to_her_are_spotted(said, order):
    assert extract_order(said) == order


@pytest.mark.parametrize("said", [
    "follow me",                       # not addressed to her
    "collect wood",
    "Lumi, wait, what did you say?",
    "Lumi, stop it lol",
    "Lumi, how do I make a stone axe?",
    "Lumi, what are you doing in minecraft?",
    "Lumi, tell me a story",
    "Lumi, get me a coffee",
    "Lumi, do you like cats?",
    "I told Lumi to collect wood yesterday",
    "wait, what did you say?",
])
def test_conversation_is_left_alone(said):
    assert extract_order(said) is None


def test_explicit_command():
    assert is_explicit("!mc follow me")
    assert not is_explicit("Lumi, follow me")


def test_drop_orders_parse():
    assert _parse_order("drop the dirt") == {"kind": "drop", "item": "dirt", "count": None}
    assert _parse_order("drop 10 cobblestone") == {"kind": "drop", "item": "cobblestone", "count": 10}


# ─── The gate ───────────────────────────────────────────────────────────────

class _Agent:
    running = True


def _game(tmp_path, monkeypatch, switch_on, agent=False, fresh_telemetry=False):
    monkeypatch.setattr(gm, "AUTOPLAY_PATH", str(tmp_path / "autoplay.json"))
    monkeypatch.setattr(gm, "TELEMETRY_PATH", str(tmp_path / "telemetry.json"))
    (tmp_path / "autoplay.json").write_text(json.dumps({"minecraft": switch_on}))
    if fresh_telemetry:
        (tmp_path / "telemetry.json").write_text("{}")
    mgr = gm.GameManager.__new__(gm.GameManager)
    mgr._children = {"minecraft:agent": _Agent()} if agent else {}
    return mgr


def test_game_is_active_only_with_the_switch_on_and_the_agent_running(tmp_path, monkeypatch):
    assert _game(tmp_path, monkeypatch, True, agent=True).is_active("minecraft")
    assert not _game(tmp_path, monkeypatch, False, agent=True).is_active("minecraft")
    assert not _game(tmp_path, monkeypatch, True).is_active("minecraft")


def test_hand_started_agent_counts_while_its_telemetry_is_fresh(tmp_path, monkeypatch):
    mgr = _game(tmp_path, monkeypatch, True, fresh_telemetry=True)
    assert mgr.is_active("minecraft")
    old = time.time() - gm.TELEMETRY_FRESH_S - 5
    os.utime(tmp_path / "telemetry.json", (old, old))
    assert not mgr.is_active("minecraft")


class _AnyModule(types.ModuleType):
    """Stands in for an optional import the test environment lacks."""
    def __getattr__(self, name):
        return type(name, (), {"__init__": lambda self, *a, **k: None})


def _prompter_with(active):
    import importlib
    import sys
    for name in ("ddgs", "pypdf", "docx"):
        try:
            importlib.import_module(name)
        except Exception:
            sys.modules[name] = _AnyModule(name)
    prompter = pytest.importorskip("modules.prompter")
    sent = []
    games = types.SimpleNamespace(
        is_active=lambda game: active,
        send_directive=lambda game, text, source="web": sent.append((text, source)))
    p = prompter.Prompter.__new__(prompter.Prompter)
    p.signals = types.SimpleNamespace(game_manager=games)
    return p, sent


def test_order_is_sent_while_the_game_is_on():
    p, sent = _prompter_with(True)
    entry = {"role": "user", "content": "Lumi, collect wood", "from_host": True}
    note = p._forward_game_order("Lumi, collect wood", entry)
    assert sent == [("collect wood", "talk")]
    assert '[Game Order Dispatched: "collect wood"]' in note
    assert entry["game_order"] == "collect wood"


def test_nothing_is_sent_while_the_game_is_off():
    p, sent = _prompter_with(False)
    note = p._forward_game_order("Lumi, collect wood", {"role": "user", "content": "x"})
    assert sent == []
    assert "the game agent is offline" in note and "Games page" in note


def test_plain_chat_gets_no_note_either_way():
    for active in (True, False):
        p, sent = _prompter_with(active)
        assert p._forward_game_order("Lumi, how was your day?") is None and sent == []


def test_spoken_messages_from_the_mic_count_as_the_host():
    src = open(os.path.join(os.path.dirname(gm.__file__), "mic_input.py"), encoding="utf-8").read()
    assert 'add_message("user", cleaned_text, from_host=True)' in src
