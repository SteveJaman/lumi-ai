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


def test_offered_only_with_an_editor_and_a_voice(monkeypatch):
    from modules import synthv_project as sv
    monkeypatch.setattr(sv, "editor_path", lambda: None)          # no Synthesizer V either
    monkeypatch.setattr(vt, "editor_path", lambda: None)
    assert not vt.available(ToolContext())
    monkeypatch.setattr(vt, "editor_path", lambda: "x.exe")
    monkeypatch.setattr(vt, "voices", lambda: [])
    assert not vt.available(ToolContext())
    monkeypatch.setattr(vt, "voices", lambda: [MEGPOID])
    assert vt.available(ToolContext())
    monkeypatch.setattr(vt, "editor_path", lambda: None)          # Synthesizer V alone is enough
    monkeypatch.setattr(sv, "editor_path", lambda: "sv.exe")
    monkeypatch.setattr(sv, "voices", lambda root=None: ["Kasane Teto"])
    assert vt.available(ToolContext())


def test_schema_stays_compact():
    assert len(json.dumps(vt.TOOL_SCHEMAS)) < 700
    assert len(vt.TOOL_SCHEMAS[0]["function"]["description"]) < 100


# ── Synthesizer V (app "synthv": SharpV or Synthesizer V Studio) ─────────

from modules import synthv_project as sv


@pytest.fixture
def synthv(monkeypatch, tmp_path):
    launched = []
    db = tmp_path / "databases"
    for d in ("Kasane_Teto", "Koharu_Rikka_(Lite)", "Natsuki_Karin_AI", "tmp"):
        (db / d).mkdir(parents=True)
    exe = tmp_path / "synthv-sharp.exe"
    exe.write_bytes(b"")
    songs = tmp_path / "songs"
    monkeypatch.setattr(sv, "voices", lambda root=None: _real_voices(str(db)))
    monkeypatch.setenv("SYNTHV_EDITOR", str(exe))
    monkeypatch.delenv("SYNTHV_VOICE", raising=False)
    monkeypatch.setattr(vt, "song_dir", lambda: str(songs))
    monkeypatch.setattr(vt.subprocess, "Popen", lambda args, **kw: launched.append(args))
    return launched, songs, exe


_real_voices = sv.voices


def test_voices_are_the_database_folders_named_as_projects_name_them(tmp_path):
    for d in ("Kasane_Teto", "Koharu_Rikka_(Lite)", "tmp"):
        (tmp_path / d).mkdir()
    assert sv.voices(str(tmp_path)) == ["Kasane Teto", "Koharu Rikka (Lite)"]


def test_the_svp_matches_what_synthesizer_v_saves():
    p = sv.build_project("Twinkle", vt.parse_notes("C4:twin G4:star:2"), 100, "Kasane Teto", 1)
    assert set(p) == {"version", "time", "library", "tracks", "renderConfig", "instantModeEnabled"}
    assert p["time"]["tempo"] == [{"position": 0, "bpm": 100.0}]
    track = p["tracks"][0]
    assert set(track) == {"name", "dispColor", "dispOrder", "renderEnabled", "mixer", "mainGroup", "mainRef", "groups"}
    assert track["mainRef"]["groupID"] == track["mainGroup"]["uuid"]
    assert track["mainRef"]["database"]["name"] == "Kasane Teto"
    assert (track["mainRef"]["database"]["language"], track["mainRef"]["database"]["phoneset"]) == ("english", "arpabet")
    assert track["mainGroup"]["notes"] == [
        {"onset": 0, "duration": 705_600_000, "pitch": 60, "lyrics": "twin", "phonemes": "", "attributes": {}},
        {"onset": 705_600_000, "duration": 1_411_200_000, "pitch": 67, "lyrics": "star", "phonemes": "", "attributes": {}}]


def test_sing_song_synthv_writes_an_svp_and_opens_it_in_the_configured_editor(synthv):
    launched, songs, exe = synthv
    out = _run({"app": "synthv", "title": "Teto", "notes": "C4:la D4:la", "voice": "teto", "lang": "ja"})
    path = songs / "Teto.svp"
    assert "Wrote 2 notes for Kasane Teto" in out and path.exists()
    assert launched == [[str(exe), str(path)]]
    db = json.loads(path.read_text(encoding="utf-8"))["tracks"][0]["mainRef"]["database"]
    assert (db["name"], db["language"], db["phoneset"]) == ("Kasane Teto", "japanese", "romaji")


def test_synthv_default_voice_is_from_env_else_the_first_full_ai_voice(synthv, monkeypatch):
    installed = ["Kasane Teto", "Koharu Rikka (Lite)", "Natsuki Karin AI"]
    assert vt.pick_synthv_voice("", installed) == "Natsuki Karin AI"
    monkeypatch.setenv("SYNTHV_VOICE", "rikka")
    assert vt.pick_synthv_voice("", installed) == "Koharu Rikka (Lite)"
    with pytest.raises(vt.SongError, match="No Synthesizer V voice called"):
        vt.pick_synthv_voice("Nobody", installed)


def test_a_missing_synthv_editor_is_reported_and_nothing_is_written(synthv, monkeypatch, tmp_path):
    launched, songs, _ = synthv
    monkeypatch.setenv("SYNTHV_EDITOR", str(tmp_path / "gone.exe"))
    out = _run({"app": "synthv", "notes": "C4:la"})
    assert out.startswith("Error") and "SYNTHV_EDITOR" in out
    assert launched == [] and not songs.exists()


def test_an_unknown_app_is_refused(pc):
    assert _run({"app": "utau", "notes": "C4:la"}) == "Error: app must be vocaloid or synthv."


# ── phrases on one note (what the local model actually sends) ────────────

LOG_INPUT = ("C4:Late C5:nights G4:Coding D4:on F3:Dreams E4:The code A3:Commits B3:Look cool "
             "C4:Bullshit D4:Nobody cares G3:Till 3 AM when I'm typing")


def test_extra_words_are_sung_on_the_previous_pitch():
    notes = vt.parse_notes("E4:The code G3:Till 3 AM r C4:hi")
    assert [(n["lyric"], n["number"], n["pos"]) for n in notes] == [
        ("The", 64, 0), ("code", 64, 480), ("Till", 55, 960), ("3", 55, 1440), ("AM", 55, 1920),
        ("hi", 60, 2880)]                                      # the rest kept its beat


def test_the_input_from_the_failed_run_now_sings(synthv):
    launched, songs, _ = synthv
    out = _run({"app": "synthv", "notes": LOG_INPUT, "bpm": 95, "lang": "en", "voice": ""})
    assert out.startswith("Wrote 19 notes") and len(launched) == 1    # one per word


@pytest.mark.parametrize("notes", ["hello C4:la", "C4:la r world"])
def test_words_without_a_note_say_how_to_write_it(notes):
    with pytest.raises(vt.SongError, match="one syllable per note"):
        vt.parse_notes(notes)
