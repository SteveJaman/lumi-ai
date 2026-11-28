import asyncio
import json

import pytest

from modules import agent_tools, song_composer as sc
from modules.everyday_common import ToolContext


def _bars(song, name, energy_names=None):
    """(start beat, end beat) of every section called `name`."""
    out, t = [], 0.0
    for n, bars, _ in song["sections"]:
        if n == name:
            out.append((t, t + bars * 4))
        t += bars * 4
    return out


@pytest.mark.parametrize("minutes,bpm", [(1, 120), (3, 132), (5, 140)])
def test_the_song_is_as_long_as_asked(minutes, bpm):
    song = sc.compose("A minor", bpm, "pop", minutes=minutes, seed=3)
    assert song["beats"] * 60 / bpm == pytest.approx(minutes * 60)


def test_custom_sections_and_progressions_are_used():
    song = sc.compose("C major", 120, "pop", "intro:4 chorus:8:vi-IV-I-V", seed=1)
    assert song["sections"] == [("intro", 4, "I V vi IV"), ("chorus", 8, "vi IV I V")]
    (start, end), = _bars(song, "chorus")
    first_bar = {n % 12 for s, ln, n, v in song["tracks"]["Chords"] if start <= s < start + 4}
    assert first_bar == {9, 0, 4}                                     # A minor = vi in C


def test_chords_are_voice_led_and_in_range():
    song = sc.compose("A minor", 128, "pop", "verse:16", seed=2)
    by_bar = {}
    for s, ln, n, v in song["tracks"]["Chords"]:
        by_bar.setdefault(int(s // 4), set()).add(n)
    voicings = [sorted(by_bar[b]) for b in sorted(by_bar)]
    assert all(52 <= c[0] and c[-1] <= 79 for c in voicings)
    moves = [sum(abs(a - b) for a, b in zip(x, y)) for x, y in zip(voicings, voicings[1:])]
    assert max(moves) <= 9                                            # no leaps between chords


def test_lead_lands_on_chord_tones_on_strong_beats():
    song = sc.compose("C major", 120, "pop", "chorus:8:vi-IV-I-V", seed=5)
    chords = {}
    for s, ln, n, v in song["tracks"]["Chords"]:
        chords.setdefault(int(s // 4), set()).add(n % 12)
    strong = [(s, n) for s, ln, n, v in song["tracks"]["Lead"] if (s % 4) in (0, 2)]
    assert strong and all(n % 12 in chords[int(s // 4)] for s, n in strong)


def test_energy_decides_which_parts_play():
    song = sc.compose("A minor", 128, "edm", "intro:4 verse:8 drop:8", seed=1)
    (i0, i1), = _bars(song, "intro")
    (d0, d1), = _bars(song, "drop")
    within = lambda part, a, b: [x for x in song["tracks"].get(part, []) if a <= x[0] < b]
    assert not within("Lead", i0, i1) and not within("Arp", i0, i1)
    assert within("Arp", d0, d1) and within("Pad", d0, d1) and within("Lead Double", d0, d1)
    assert all(n[2] == lead[2] + 12 for n, lead in zip(within("Lead Double", d0, d1), within("Lead", d0, d1)))


def test_drums_use_general_midi_keys_and_fill_into_a_bigger_section():
    song = sc.compose("A minor", 128, "pop", "verse:4 chorus:4", seed=1)
    keys = {n for s, ln, n, v in song["tracks"]["Drums"]}
    assert keys <= {sc.KICK, sc.SNARE, sc.CLAP, sc.HAT, sc.OPEN_HAT, sc.CRASH}
    fill = [s for s, ln, n, v in song["tracks"]["Drums"] if n == sc.SNARE and 14 <= s < 16]
    assert len(fill) == 8                                             # 16th snare roll into the chorus
    assert any(n == sc.CRASH and s == 16 for s, ln, n, v in song["tracks"]["Drums"])


def test_the_midi_file_round_trips_exactly(tmp_path):
    song = sc.compose("F# minor", 132, "trap", "verse:8 chorus:8", seed=9)
    path = sc.write_midi(str(tmp_path / "t.mid"), song, "ロウワー test")
    back = sc.read_midi(path)
    assert back["ppq"] == 96 and back["tempo_bpm"] == pytest.approx(132, abs=0.01)
    assert set(back["tracks"]) == set(song["tracks"])
    for name, notes in song["tracks"].items():
        got = back["tracks"][name]
        assert len(got) == len(notes)
        for (s, ln, n, v), (s2, ln2, n2, v2) in zip(sorted(notes), got):
            assert (n, v) == (n2, v2) and s2 == pytest.approx(s, abs=1 / 96) and ln2 == pytest.approx(ln, abs=1 / 96)


def test_the_same_seed_gives_the_same_song():
    a = sc.compose("A minor", 128, "pop", "verse:8", seed=11)
    b = sc.compose("A minor", 128, "pop", "verse:8", seed=11)
    assert a == b


@pytest.fixture
def pc(monkeypatch, tmp_path):
    launched = []
    monkeypatch.setattr(sc, "song_dir", lambda: str(tmp_path))
    monkeypatch.setattr(sc, "fl_studio_exe", lambda: r"C:\FL\FL64.exe")
    monkeypatch.setattr(sc.subprocess, "Popen", lambda args, **kw: launched.append(args))
    return launched, tmp_path


def _run(args):
    return asyncio.run(agent_tools.execute_everyday_tool("music_compose", args, ToolContext()))


def test_music_compose_writes_the_song_and_opens_it_in_fl_studio(pc):
    launched, folder = pc
    out = _run({"title": "Lumi Song", "key": "A minor", "bpm": 120, "style": "pop", "minutes": 1, "seed": 4})
    path = folder / "Lumi Song.mid"
    assert path.exists() and launched == [[r"C:\FL\FL64.exe", str(path)]]
    assert "120 BPM" in out and "(1:00)" in out and "Opened it in FL Studio" in out
    assert len(sc.read_midi(str(path))["tracks"]) >= 5


@pytest.mark.parametrize("args,word", [
    ({"style": "polka"}, "style"),
    ({"key": "H major"}, "key"),
    ({"bpm": 400}, "bpm"),
    ({"minutes": 30}, "minutes"),
    ({"sections": "verse:999"}, "bars"),
    ({"sections": "chorus:8:vi-Xyz"}, "chorus"),
])
def test_bad_plans_are_explained_and_nothing_is_written(pc, args, word):
    launched, folder = pc
    out = _run(args)
    assert out.startswith("Error") and word in out
    assert launched == [] and list(folder.iterdir()) == []


def test_the_newest_fl_studio_is_picked():
    names = ["FL Studio 20", "FL Studio 21", "FL Studio 2024", "FL Studio 2026", "FL Studio 2025"]
    assert max(names, key=sc._fl_year) == "FL Studio 2026"


def test_schema_stays_compact():
    assert len(json.dumps(sc.TOOL_SCHEMAS)) < 800
    assert len(sc.TOOL_SCHEMAS[0]["function"]["description"]) < 110


@pytest.mark.parametrize("style", sc.STYLES)
def test_every_style_survives_the_midi_round_trip(tmp_path, style):
    # Hat rolls over 16th hats used to make same-pitch notes overlap, which MIDI can't hold.
    for seed in range(4):
        song = sc.compose("A minor", 140, style, minutes=1, seed=seed)
        back = sc.read_midi(sc.write_midi(str(tmp_path / f"{style}{seed}.mid"), song))["tracks"]
        assert {k: len(v) for k, v in back.items()} == {k: len(v) for k, v in song["tracks"].items()}
