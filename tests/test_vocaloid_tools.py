import asyncio
import json
import zipfile

import pytest

from modules import agent_tools, vocaloid_tools as vt
from modules.everyday_common import ToolContext

MEGPOID = {"compID": "BMBEENH2NEYK3M94", "name": "AI_Megpoid", "bank": "AI_Megpoid", "langs": [0, 1]}
POUTA = {"compID": "BESXB4Z8K82SYED9", "name": "Po-uta", "bank": "Po-uta", "langs": [0, 1, 4]}


@pytest.fixture
def pc(monkeypatch, tmp_path):
    """This PC as the tool sees it: two voices, the editor, a song folder, no real launch."""
    launched = []
    monkeypatch.setattr(vt, "voices", lambda: [MEGPOID, POUTA])
    monkeypatch.setattr(vt, "editor_path", lambda: r"D:\Vocaloid\Editor\VOCALOID6.exe")
    monkeypatch.setattr(vt, "song_dir", lambda: str(tmp_path))
    monkeypatch.setattr(vt.subprocess, "Popen", lambda args, **kw: launched.append(args))
    return launched, tmp_path


def _run(args):
    return asyncio.run(agent_tools.execute_everyday_tool("sing_song", args, ToolContext()))


def _read(path):
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        return names, json.loads(z.read("Project/sequence.json"))


def test_notes_and_lyrics_become_vocaloid_ticks():
    notes = vt.parse_notes("C4:twin C4:kle r G4:star:2 A4")
    assert [(n["number"], n["lyric"], n["pos"], n["duration"]) for n in notes] == [
        (60, "twin", 0, 480), (60, "kle", 480, 480), (67, "star", 1440, 960), (69, "la", 2400, 480)]


def test_sing_song_writes_a_vocaloid6_project_and_opens_it(pc):
    launched, folder = pc
    out = _run({"title": "Twinkle", "notes": "C4:twin C4:kle G4:star:2", "bpm": 100, "lang": "en"})
    path = folder / "Twinkle.vpr"
    assert "Wrote 3 notes for AI_Megpoid at 100 BPM" in out and path.exists()
    assert launched == [[r"D:\Vocaloid\Editor\VOCALOID6.exe", str(path)]]
    names, seq = _read(path)
    assert names == ["Project/Audio/", "Project/sequence.json"]
    assert seq["version"] == {"major": 6, "minor": 1, "revision": 0}
    assert seq["masterTrack"]["tempo"]["events"] == [{"pos": 0, "value": 10000}]
    assert seq["voices"] == [{"compID": "BMBEENH2NEYK3M94", "name": "AI_Megpoid"}]
    part = seq["tracks"][0]["parts"][0]
    assert part["voice"] == {"compID": "BMBEENH2NEYK3M94", "langID": 1}
    assert part["aiVoice"] == {"compID": "BMBEENH2NEYK3M94", "langIDs": [{"langID": 0}, {"langID": 1}]}
    assert part["duration"] == 1920                                      # one whole bar
    assert [(n["lyric"], n["number"], n["pos"], n["duration"], n["langID"]) for n in part["notes"]] == [
        ("twin", 60, 0, 480, 1), ("kle", 60, 480, 480, 1), ("star", 67, 960, 960, 1)]


def test_the_note_fields_match_what_vocaloid6_saves():
    seq = vt.build_sequence("t", vt.parse_notes("C4:la"), 120, MEGPOID, 1)
    note = seq["tracks"][0]["parts"][0]["notes"][0]
    assert set(note) == {"lyric", "phoneme", "langID", "isProtected", "pos", "duration", "number",
                         "velocity", "exp", "aiExp", "singingSkill", "vibrato"}      # as in a 6.1 .vpr
    assert set(seq) == {"version", "vender", "title", "masterTrack", "voices", "tracks"}


def test_a_voice_is_picked_by_name_or_by_language(pc):
    _run({"notes": "C4:ni", "lang": "ja", "voice": "po uta"})
    _, seq = _read(pc[1] / "Lumi song.vpr")
    assert seq["voices"][0]["name"] == "Po-uta"
    assert vt.pick_voice("", 4, [MEGPOID, POUTA]) is POUTA                # only Po-uta sings Chinese


def test_an_earlier_song_is_never_overwritten(pc):
    _run({"title": "Song", "notes": "C4:la"})
    _run({"title": "Song", "notes": "D4:la"})
    assert sorted(p.name for p in pc[1].iterdir()) == ["Song (2).vpr", "Song.vpr"]


@pytest.mark.parametrize("args,word", [
    ({"notes": ""}, "No notes"),
    ({"notes": "H9:la"}, "note"),
    ({"notes": "C4:la", "bpm": 900}, "bpm"),
    ({"notes": "C4:la", "lang": "fr"}, "lang"),
    ({"notes": "C4:la", "voice": "Nobody"}, "Installed: AI_Megpoid, Po-uta"),
    ({"notes": "C4:la", "voice": "megpoid", "lang": "zh"}, "doesn't sing"),
])
def test_bad_input_is_explained_and_nothing_is_written(pc, args, word):
    launched, folder = pc
    out = _run(args)
    assert out.startswith("Error") and word in out
    assert launched == [] and list(folder.iterdir()) == []


def test_titles_cannot_escape_the_song_folder(pc):
    _run({"title": r"..\..\evil:name", "notes": "C4:la"})
    assert [p.name for p in pc[1].iterdir()] == ["....evilname.vpr"]


def test_offered_only_with_the_editor_and_a_voice(monkeypatch):
    monkeypatch.setattr(vt, "editor_path", lambda: None)
    assert not vt.available(ToolContext())
    monkeypatch.setattr(vt, "editor_path", lambda: "x.exe")
    monkeypatch.setattr(vt, "voices", lambda: [])
    assert not vt.available(ToolContext())
    monkeypatch.setattr(vt, "voices", lambda: [MEGPOID])
    assert vt.available(ToolContext())


def test_schema_stays_compact():
    assert len(json.dumps(vt.TOOL_SCHEMAS)) < 700
    assert len(vt.TOOL_SCHEMAS[0]["function"]["description"]) < 100
