"""Minecraft orders spotted in what the host says to Lumi (modules/game_orders.py)."""

import pytest

from modules.game_orders import extract_order, is_explicit


@pytest.mark.parametrize("said, order", [
    ("follow me", "follow me"),
    ("Lumi, follow me.", "follow me"),
    ("hey lumi can you please make a stone axe?", "make a stone axe"),
    ("Could you get me 5 diamonds", "get me 5 diamonds"),
    ("go to sleep", "go to sleep"),
    ("stay here", "stay here"),
    ("in minecraft, build a house", "build a house"),
    ("tell your other half to come back", "come back"),
    ("!mc chop some trees", "chop some trees"),
    ("do your own thing", "do your own thing"),
])
def test_orders_are_spotted(said, order):
    assert extract_order(said) == order


@pytest.mark.parametrize("said", [
    "wait, what did you say?",
    "stop",
    "stop it lol",
    "what are you doing in minecraft?",
    "how do I make a stone axe?",
    "tell me a story",
    "make me laugh",
    "get me a coffee",
    "set a timer for 5 minutes",
    "can you help me with my homework?",
    "come on, that's not fair",
])
def test_conversation_is_left_alone(said):
    assert extract_order(said) is None


def test_explicit_command():
    assert is_explicit("!mc follow me")
    assert not is_explicit("follow me")
