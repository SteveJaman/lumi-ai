"""Tests for the SDK wire protocol — no external services required."""

import json
import pytest

from sdk.protocol import (
    ActionSpec,
    Session,
    hello_message,
    context_message,
    context_clear_message,
    register_actions_message,
    unregister_actions_message,
    force_action_message,
    action_result_message,
    startup_message,
    action_message,
    speech_finished_message,
    error_message,
    SDK_PROTOCOL_VERSION,
)


def test_action_spec_roundtrip():
    spec = ActionSpec(
        name="walk",
        description="Walk to a coordinate.",
        schema={"type": "object", "properties": {"x": {"type": "number"}}},
        capabilities=["write"],
        requires_confirm=True,
    )
    wire = spec.to_wire()
    assert wire["name"] == "walk"
    assert wire["capabilities"] == ["write"]
    assert wire["requires_confirm"] is True

    back = ActionSpec.from_wire(wire)
    assert back.name == spec.name
    assert back.description == spec.description
    assert back.schema == spec.schema
    assert back.capabilities == spec.capabilities
    assert back.requires_confirm == spec.requires_confirm


def test_action_spec_defaults_when_empty():
    spec = ActionSpec.from_wire({})
    assert spec.name == ""
    assert spec.capabilities == ["read"]
    assert spec.requires_confirm is False
    assert spec.schema == {"type": "object", "properties": {}}


def test_session_wire_shape():
    s = Session(session_id="abc", character_id="lumi", display_name="Lumi V")
    w = s.to_wire()
    assert w == {
        "sessionId": "abc",
        "characterId": "lumi",
        "displayName": "Lumi V",
    }


def test_hello_message_contains_auth_and_game():
    msg = hello_message("TestGame", "secret-token", client_version="1.2.3")
    assert msg["command"] == "hello"
    assert msg["game"] == "TestGame"
    assert msg["auth_token"] == "secret-token"
    assert msg["client_version"] == "1.2.3"


def test_context_and_clear():
    ctx = context_message("G", "hello world", silent=True)
    assert ctx["command"] == "context"
    assert ctx["data"]["message"] == "hello world"
    assert ctx["data"]["silent"] is True

    clear = context_clear_message("G")
    assert clear["command"] == "context/clear"
    assert clear["game"] == "G"


def test_register_actions_serializes_specs():
    specs = [
        ActionSpec(name="a", description="A"),
        ActionSpec(name="b", description="B", capabilities=["dangerous"]),
    ]
    msg = register_actions_message("G", specs)
    assert msg["command"] == "actions/register"
    assert len(msg["data"]["actions"]) == 2
    assert msg["data"]["actions"][0]["name"] == "a"


def test_unregister_actions_carries_names():
    msg = unregister_actions_message("G", ["a", "b"])
    assert msg["command"] == "actions/unregister"
    assert msg["data"]["action_names"] == ["a", "b"]


def test_force_action_priority_validation():
    msg = force_action_message("G", "do it", ["a"], priority="bogus")
    # Unknown priority falls back to "low" rather than being rejected
    assert msg["data"]["priority"] == "low"

    msg2 = force_action_message("G", "do it", ["a"], priority="high")
    assert msg2["data"]["priority"] == "high"


def test_action_result_omits_message_when_none():
    msg = action_result_message("G", "id-1", True)
    assert msg["data"] == {"id": "id-1", "success": True}

    msg2 = action_result_message("G", "id-2", False, "boom")
    assert msg2["data"]["success"] is False
    assert msg2["data"]["message"] == "boom"


def test_startup_carries_protocol_version():
    s = Session(session_id="sid")
    msg = startup_message(s)
    assert msg["command"] == "startup"
    assert msg["data"]["protocolVersion"] == SDK_PROTOCOL_VERSION
    assert msg["data"]["session"]["sessionId"] == "sid"


def test_action_message_encodes_args_as_json_string():
    msg = action_message("inv-1", "walk", {"x": 1, "z": 2})
    assert msg["command"] == "action"
    # Neuro convention: arguments are a JSON string, not an object
    assert isinstance(msg["data"]["data"], str)
    parsed = json.loads(msg["data"]["data"])
    assert parsed == {"x": 1, "z": 2}


def test_speech_finished_shape():
    msg = speech_finished_message("hi", role="assistant")
    assert msg["command"] == "speech_finished"
    assert msg["data"]["message"] == "hi"
    assert msg["data"]["isFinal"] is True
    assert msg["data"]["cancelled"] is False


def test_error_message_shape():
    msg = error_message("bad thing")
    assert msg["command"] == "error"
    assert msg["data"]["message"] == "bad thing"