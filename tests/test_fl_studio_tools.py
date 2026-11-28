import asyncio
import types

import pytest

from modules import agent_tools, fl_studio_tools as fl
from modules import mcp_client as mc
from modules.everyday_common import ToolContext


class FakeMCP:
    """Stands in for signals.mcp (MCPRegistry): records every call to the FL Studio server."""

    def __init__(self, state="connected", port_ok=True):
        self.servers = {fl.SERVER_ID: types.SimpleNamespace(state=state)}
        self.calls = []
        self.port_ok = port_ok

    async def call_async(self, sid, tool, arguments):
        self.calls.append((sid, tool, arguments))
        if tool == "fl_connect":             # what the real server answers (server.py fl_connect)
            return ("Successfully connected to FL Studio via MIDI!" if self.port_ok
                    else "Connection failed: No MIDI port matching 'FLStudioMCP' was found.")
        return "ok"

    def actions(self):
        return [c for c in self.calls if c[1] != "fl_connect"]


PIN = ("fl-studio", "fl_connect", {"port_name": "FLStudioMCP"})


def _ctx(mcp):
    return ToolContext(signals=types.SimpleNamespace(mcp=mcp))


def _run(name, args, mcp):
    return asyncio.run(agent_tools.execute_everyday_tool(name, args, _ctx(mcp)))


def test_set_tempo_sends_fl_set_tempo_to_the_fl_studio_server():
    mcp = FakeMCP()
    assert _run("music_set_tempo", {"bpm": 128}, mcp) == "ok"
    assert mcp.calls == [PIN, ("fl-studio", "fl_set_tempo", {"bpm": 128.0})]


@pytest.mark.parametrize("bpm", [5, 1200, "fast", None])
def test_bad_tempos_are_refused_before_anything_is_sent(bpm):
    mcp = FakeMCP()
    assert _run("music_set_tempo", {"bpm": bpm}, mcp).startswith("Error")
    assert mcp.calls == []


def test_pattern_chord_and_melody_map_to_the_servers_tools():
    mcp = FakeMCP()
    _run("music_create_pattern", {"name": "Verse"}, mcp)
    _run("music_add_chord", {"scale": "C major", "chord_name": "vi", "octave": 4, "at": 4}, mcp)
    _run("music_add_melody", {"notes": "C4 E4 r G4:2", "duration": 0.5}, mcp)
    assert mcp.calls.count(PIN) == 1 and mcp.calls[0] == PIN     # pinned once, before the first action
    acts = mcp.actions()
    assert acts[0] == ("fl-studio", "fl_create_pattern", {"name": "Verse"})
    assert acts[1] == ("fl-studio", "fl_send_chord", {"midi_notes": [69, 72, 76], "time": 4.0, "duration": 1.0})
    assert acts[2] == ("fl-studio", "fl_send_notes", {"mode": "add", "notes": [
        {"midi": 60, "duration": 0.5, "time": 0.0},
        {"midi": 64, "duration": 0.5, "time": 0.5},
        {"midi": 67, "duration": 2.0, "time": 1.5}]})


@pytest.mark.parametrize("chord,scale,octave,want", [
    ("C", "", 4, [60, 64, 67]),
    ("Am7", "", 3, [57, 60, 64, 67]),
    ("V7", "C major", 4, [67, 71, 74, 77]),        # G B D F
    ("i", "A minor", 3, [57, 60, 64]),
    ("F#m", "", 4, [66, 69, 73]),
    ("Bbmaj7", "", 3, [58, 62, 65, 69]),
])
def test_chords_become_the_right_midi_notes(chord, scale, octave, want):
    assert fl.chord_to_midi(chord, scale, octave) == want


def test_bad_music_input_is_explained_not_sent():
    mcp = FakeMCP()
    assert "chord" in _run("music_add_chord", {"chord_name": "Xyz"}, mcp)
    assert "key" in _run("music_add_chord", {"chord_name": "vi"}, mcp)        # numeral needs a key
    assert "note" in _run("music_add_melody", {"notes": "H9"}, mcp)
    assert mcp.calls == []


def test_a_missing_midi_port_is_reported_and_nothing_is_sent():
    mcp = FakeMCP(port_ok=False)
    out = _run("music_set_tempo", {"bpm": 120}, mcp)
    assert out.startswith("Error") and "FLStudioMCP" in out and "loopMIDI" in out
    assert mcp.actions() == []                                   # never falls back to another port
    mcp.port_ok = True                                           # the user starts loopMIDI
    assert _run("music_set_tempo", {"bpm": 120}, mcp) == "ok"    # tried again, then sent
    assert mcp.calls.count(PIN) == 2


def test_a_reconnected_server_is_pinned_again():
    mcp = FakeMCP()
    _run("music_set_tempo", {"bpm": 100}, mcp)
    mcp.servers[fl.SERVER_ID] = types.SimpleNamespace(state="connected")   # Reconnect: new process
    _run("music_set_tempo", {"bpm": 110}, mcp)
    assert mcp.calls.count(PIN) == 2


def test_the_port_name_can_be_changed_in_constants(monkeypatch):
    import constants
    monkeypatch.setattr(constants, "FL_STUDIO_MIDI_PORT", "loopMIDI Port 1", raising=False)
    mcp = FakeMCP()
    _run("music_create_pattern", {"name": "A"}, mcp)
    assert mcp.calls[0] == ("fl-studio", "fl_connect", {"port_name": "loopMIDI Port 1"})


def test_tools_are_offered_only_while_the_server_is_connected():
    assert fl.available(_ctx(FakeMCP()))
    assert not fl.available(_ctx(FakeMCP(state="errored")))
    assert not fl.available(_ctx(None))
    offered = {s["function"]["name"] for s in agent_tools.everyday_tool_schemas(_ctx(FakeMCP()))}
    assert {"music_set_tempo", "music_add_melody"} <= offered


def test_schemas_stay_compact():
    import json
    assert len(json.dumps(fl.TOOL_SCHEMAS)) < 1800      # four tools, well under 500 tokens
    assert all(len(s["function"]["description"]) < 100 for s in fl.TOOL_SCHEMAS)


def test_the_fl_studio_server_entry_validates_as_hidden():
    # The registry behaviour (hidden from the model, kept through web-form edits) is covered in
    # tests/test_mcp_client.py::test_expose_tools_false_round_trips_through_web_form_edits.
    cfg = mc.validate_server({"id": fl.SERVER_ID, "command": "uv", "expose_tools": False,
                              "args": ["run", "--directory", "C:/x", "fl-studio-mcp"]})
    assert cfg["expose_tools"] is False and cfg["id"] == "fl-studio"
    assert "expose_tools" not in mc.validate_server({"id": "x", "command": "uv"})


# ── chord length and progressions ────────────────────────────────────────

def test_a_single_chord_takes_its_length():
    mcp = FakeMCP()
    _run("music_add_chord", {"chord_name": "IV", "scale": "C major", "beats": 4, "at": 8}, mcp)
    assert mcp.actions() == [("fl-studio", "fl_send_chord", {"midi_notes": [65, 69, 72], "time": 8.0, "duration": 4.0})]


def test_a_progression_is_one_request_with_every_chord_in_place():
    mcp = FakeMCP()
    _run("music_add_chord", {"chord_name": "vi IV I V", "scale": "C major", "beats": 4, "repeat": 2}, mcp)
    [(sid, tool, args)] = mcp.actions()                       # one call: one piano roll script run
    assert tool == "fl_send_notes" and args["mode"] == "add"
    starts = sorted({n["time"] for n in args["notes"]})
    assert starts == [0.0, 4.0, 8.0, 12.0, 16.0, 20.0, 24.0, 28.0]
    first = [n["midi"] for n in args["notes"] if n["time"] == 0.0]
    assert first == [69, 72, 76] and all(n["duration"] == 4.0 for n in args["notes"])
    assert [n["midi"] for n in args["notes"] if n["time"] == 28.0] == [67, 71, 74]   # V, second time round


@pytest.mark.parametrize("args", [
    {"chord_name": "vi IV Xyz", "scale": "C major"},            # one bad chord: nothing is sent
    {"chord_name": "C", "beats": 0},
    {"chord_name": "C", "repeat": 0},
    {"chord_name": "C G Am F", "repeat": 64},                  # 256 chords, over the limit
])
def test_bad_progressions_are_refused_before_anything_is_sent(args):
    mcp = FakeMCP()
    assert _run("music_add_chord", args, mcp).startswith("Error")
    assert mcp.calls == []


def test_piano_roll_replies_say_how_to_apply_the_notes_by_hand():
    mcp = FakeMCP()
    assert "ComposeWithLLM" in _run("music_add_melody", {"notes": "C4 E4"}, mcp)
    assert "ComposeWithLLM" in _run("music_add_chord", {"chord_name": "C"}, mcp)
    assert "ComposeWithLLM" not in _run("music_set_tempo", {"bpm": 100}, mcp)
