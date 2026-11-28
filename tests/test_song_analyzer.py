import asyncio

import numpy as np
import pytest

from modules import agent_tools, song_analyzer as sa, song_composer as sc
from modules.everyday_common import ToolContext

SR = 22050
BPM = 120
CHORDS = [("C", [60, 64, 67]), ("F", [65, 69, 72]), ("G", [67, 71, 74]), ("Am", [69, 72, 76])]
BARS = 16


@pytest.fixture(scope="module")
def song(tmp_path_factory):
    """32 s at 120 BPM: a kick on every beat, C F G Am one bar each, the second half louder."""
    import soundfile as sf
    beat = 60 / BPM
    n = int(SR * beat * 4 * BARS)
    y = np.zeros(n)
    t = np.arange(int(SR * beat * 4)) / SR
    for bar in range(BARS):
        name, notes = CHORDS[bar % 4]
        tone = sum(np.sin(2 * np.pi * 440 * 2 ** ((m - 69) / 12) * t) for m in notes + [notes[0] - 12])
        gain = 0.08 if bar < BARS // 2 else 0.2
        start = int(bar * SR * beat * 4)
        y[start:start + len(t)] += gain * tone / 4
    kick_t = np.arange(int(SR * 0.08)) / SR
    kick = np.sin(2 * np.pi * 60 * kick_t) * np.exp(-kick_t * 40)
    for b in range(BARS * 4):
        s = int(b * SR * beat)
        y[s:s + len(kick)] += 0.6 * kick
    path = tmp_path_factory.mktemp("audio") / "test_song.wav"
    sf.write(str(path), y / np.max(np.abs(y)) * 0.9, SR)
    return str(path), sa.analyze(str(path))


def test_tempo_is_found(song):
    _, a = song
    assert min(abs(a["bpm"] - x) for x in (60, 120, 240)) <= 3


def test_key_is_found(song):
    _, a = song
    assert a["key"] in ("C major", "A minor")


def test_chords_are_found_bar_by_bar(song):
    _, a = song
    found = [c for c, _ in a["chords"]]
    assert set(found) <= {"C", "Dm", "Em", "F", "G", "Am"}        # only the key's own chords
    assert {"C", "F", "G", "Am"} <= set(found)


def test_the_louder_half_is_a_chorus(song):
    _, a = song
    assert len(a["sections"]) >= 2 and a["sections"][-1]["name"] == "chorus"
    assert sum(s["bars"] for s in a["sections"]) == a["bars"]


def test_the_plan_is_one_music_compose_accepts(song):
    _, a = song
    sc.parse_sections(a["plan"], "major", None, a["bpm"])          # raises if it isn't


def test_key_triads_are_the_keys_own_chords():
    names = [n for n, _ in sa.key_triads(3, "major")]               # E-flat (D#) major
    assert names == ["D#", "Fm", "Gm", "G#", "A#", "Cm"]


def test_long_sections_are_split_for_music_compose():
    plan = sa.plan_string([{"name": "chorus", "bars": 130, "loop": ["C", "G"]}])
    assert plan == "chorus:44:C-G chorus:43:C-G chorus:43:C-G"


@pytest.mark.parametrize("path,word", [("notes.txt", "audio file"), ("C:/nope/missing.mp3", "No file")])
def test_bad_paths_are_refused(path, word):
    out = asyncio.run(agent_tools.execute_everyday_tool("music_analyze", {"path": path}, ToolContext()))
    assert out.startswith("Error") and word in out


def test_music_analyze_reports_and_recreates(song, monkeypatch, tmp_path):
    path, _ = song
    launched = []
    monkeypatch.setattr(sc, "song_dir", lambda: str(tmp_path))
    monkeypatch.setattr(sc, "fl_studio_exe", lambda: r"C:\FL\FL64.exe")
    monkeypatch.setattr(sc.subprocess, "Popen", lambda args, **kw: launched.append(args))
    out = asyncio.run(agent_tools.execute_everyday_tool(
        "music_analyze", {"path": path, "recreate": True, "style": "pop"}, ToolContext()))
    assert "BPM" in out and "To recreate: music_compose" in out and "Composed test_song (Lumi recreation)" in out
    made = tmp_path / "test_song (Lumi recreation).mid"
    assert made.exists() and launched == [[r"C:\FL\FL64.exe", str(made)]]


def test_loudness_cuts_find_the_jump_on_the_exact_bar():
    energy = [0.05] * 7 + [0.1] * 9                    # the chorus comes in at bar 7, off the 4-bar grid
    assert sa.loudness_cuts(energy) == [7]
    assert sa.loudness_cuts([0.05] * 16) == []         # no change, no cut


def test_merge_cuts_keeps_sections_at_least_four_bars():
    assert sa.merge_cuts([7], {4, 8, 12}, 16) == [0, 7, 12, 16]
