"""Game moments move Lumi's emotional state (clamped), and game milestones
become memories (sdk/server.py context extras)."""

import threading
import types

from modules.emotional_state import GAME_LIMITS, EmotionalState
from sdk.server import SDKServer


def _state():
    s = EmotionalState.__new__(EmotionalState)
    s.valence, s.arousal, s.frustration, s.curiosity, s.boredom = 0.0, 0.4, 0.0, 0.5, 0.2
    s._lock = threading.Lock()
    return s


def test_death_hurts_and_diamonds_help():
    s = _state()
    changed = s.note_game_event("death")
    assert changed["frustration"] > 0 and changed["valence"] < 0
    s2 = _state()
    s2.note_game_event("collect:diamond")
    assert s2.valence > 0.2 and s2.arousal > 0.4


def test_same_moment_counts_once_per_cooldown():
    s = _state()
    s.note_game_event("hurt:mob")
    after_first = s.frustration
    assert s.note_game_event("hurt:fall") == {}      # same kind, too soon
    assert s.frustration == after_first


def test_game_events_alone_stay_in_limits():
    s = _state()
    for _ in range(20):
        s._game_event_at = {}                        # skip the cooldown
        s.note_game_event("death")
    lo, hi = GAME_LIMITS["frustration"]
    assert s.frustration <= hi
    assert s.valence >= GAME_LIMITS["valence"][0]


def test_unknown_kind_does_nothing():
    s = _state()
    assert s.note_game_event("weather:rain") == {}
    assert (s.valence, s.frustration) == (0.0, 0.0)


class _Client:
    approved = True
    allow_reactions = True
    game_name = "MinecraftAgent"
    context_text = "standing status"
    last_reaction = 0
    pending_reaction = None


def _server():
    felt, stored = [], []
    server = SDKServer.__new__(SDKServer)
    server.signals = types.SimpleNamespace(
        emotional_state=types.SimpleNamespace(note_game_event=lambda k: felt.append(k) or {"valence": 0.1}),
        memory=types.SimpleNamespace(API=types.SimpleNamespace(
            create_memory=lambda text, **kw: stored.append((text, kw)))),
        history=[], new_message=False, AI_thinking=False, AI_speaking=False, log_hub=None,
    )
    return server, felt, stored


def test_event_only_moves_mood_without_a_turn_or_context():
    server, felt, _ = _server()
    client = _Client()
    server._handle_context(client, {"data": {"message": "", "silent": True,
                                             "event_kind": "death", "event_only": True}})
    assert felt == ["death"]
    assert client.context_text == "standing status"
    assert server.signals.history == []


def test_remember_stores_a_game_memory_once():
    server, _, stored = _server()
    client = _Client()
    msg = {"data": {"message": "Built a planks house near 120, 64, -300.", "remember": True}}
    server._handle_context(client, msg)
    server._handle_context(client, msg)
    assert len(stored) == 1
    text, kw = stored[0]
    assert "Built a planks house" in text and kw["memory_type"] == "game"
    assert client.context_text == "standing status"


def test_unapproved_clients_cant_move_mood_or_memory():
    server, felt, stored = _server()
    client = _Client()
    client.approved = False
    server._handle_context(client, {"data": {"message": "x", "event_kind": "death", "remember": True}})
    assert felt == [] and stored == []
