"""
Lumi composes whole songs: music_compose turns a short song plan into a multi-track MIDI
arrangement and opens it in FL Studio.

A small local model can write a plan reliably but not thousands of notes, so the split is:

    the model     -> key, tempo, style, sections ("intro:8 verse:16 chorus:16:vi-IV-I-V")
    this module   -> drums, bass, chords, pad, arpeggio, lead (+ a lead double), as notes

Each section has an energy level (intro 1 ... drop 5) that decides which parts play and how
busy they are: drums build from a sparse groove to full kits with fills and crashes, chords
go from held to rhythmic, a pad and an arpeggio come in, and the lead is doubled an octave up
in the biggest sections. Chords are voice-led (each voicing stays close to the last). The lead
is built from motifs: a rhythm for the phrase, chord tones on strong beats, steps between,
and the phrase repeated over the next chords so it sounds composed, not random.

Output: a type-1 Standard MIDI File (96 ticks per quarter, like FL Studio), one named track
per part with a General MIDI program (drums on channel 10). FL Studio opens it as a project
with a channel per track. Songs go to Documents\\Lumi Songs and are never overwritten.
"""

from __future__ import annotations

import os
import random
import re
import struct
import subprocess
from typing import Dict, List, Optional, Tuple

from modules.fl_studio_tools import chord_to_midi, MusicError

PPQ = 96
MAX_BARS = 256
MAX_SECTIONS = 24
STYLES = ("pop", "edm", "lofi", "trap")
AGENT = "operate"
CONFIRM_TOOLS: set = set()
TASKS = {"music_compose": "Composing a song"}

TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "music_compose",
        "description": "Compose a full multi-track song (drums, bass, chords, pad, arp, lead) and open it in FL Studio.",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string"},
            "key": {"type": "string", "description": "e.g. A minor, C major"},
            "bpm": {"type": "integer"},
            "style": {"type": "string", "description": "pop, edm, lofi or trap"},
            "sections": {"type": "string", "description": "name:bars[:chords], e.g. intro:8 verse:16 chorus:16:vi-IV-I-V"},
            "minutes": {"type": "number", "description": "length, if no sections given"}},
            "required": []}}},
]

ENERGY = {"intro": 1, "verse": 2, "pre": 3, "prechorus": 3, "build": 3, "chorus": 4, "hook": 4,
          "drop": 5, "bridge": 2, "break": 1, "breakdown": 1, "outro": 1}
DEFAULT_SECTIONS = [("intro", 8), ("verse", 16), ("pre", 8), ("chorus", 16), ("verse", 16), ("pre", 8),
                    ("chorus", 16), ("bridge", 8), ("chorus", 16), ("outro", 8)]
PROGRESSIONS = {
    "major": {"intro": "I V vi IV", "verse": "I V vi IV", "pre": "IV V iii vi", "chorus": "vi IV I V",
              "drop": "vi IV I V", "bridge": "IV V vi vi", "outro": "I V vi IV"},
    "minor": {"intro": "i VI III VII", "verse": "i VI III VII", "pre": "iv v VI VII", "chorus": "VI VII i i",
              "drop": "VI VII i v", "bridge": "iv VI v v", "outro": "i VI III VII"},
}

# General MIDI programs (0-based) and channels; drums are channel 10 (index 9).
PARTS = {"Drums": (9, 0), "Bass": (1, 38), "Chords": (2, 4), "Pad": (3, 89), "Arp": (4, 81),
         "Lead": (5, 80), "Lead Double": (6, 81)}
KICK, SNARE, CLAP, HAT, OPEN_HAT, CRASH = 36, 38, 39, 42, 46, 49


class SongError(ValueError):
    """Bad song input, said in words the model can fix."""


# ── the plan ──────────────────────────────────────────────────────────────

def parse_key(key: str) -> Tuple[str, str]:
    """'a minor' / 'F# major' / 'Am' -> ('A minor', 'minor'); bad input raises SongError."""
    text = (key or "A minor").strip()
    m = re.match(r"^([A-Ga-g])([#b]?)\s*(major|minor|maj|min|m)?$", text.replace("  ", " "), re.I)
    if not m:
        raise SongError(f"{key!r} isn't a key like A minor or C major.")
    mode = "minor" if (m.group(3) or "major").lower() in ("minor", "min", "m") else "major"
    return f"{m.group(1).upper()}{m.group(2)} {mode}", mode


def parse_sections(sections: str, mode: str, minutes: Optional[float], bpm: float) -> List[dict]:
    """'intro:8 verse:16 chorus:16:vi-IV-I-V' -> [{name, bars, chords, energy}]."""
    out = []
    if sections and sections.strip():
        for token in sections.replace(",", " ").split():
            parts = token.split(":")
            name = re.sub(r"[^a-z]", "", parts[0].lower()) or "verse"
            try:
                bars = int(parts[1]) if len(parts) > 1 else 8
            except ValueError:
                raise SongError(f"{token}: bars must be a whole number, e.g. verse:16.")
            if not 1 <= bars <= 64:
                raise SongError(f"{token}: a section is 1 to 64 bars.")
            chords = parts[2].replace("-", " ").split() if len(parts) > 2 and parts[2] else None
            out.append({"name": name, "bars": bars, "chords": chords})
    else:
        plan = list(DEFAULT_SECTIONS)
        if minutes:
            target = max(8, round(minutes * bpm / 4))          # bars in 4/4
            total = sum(b for _, b in plan)
            plan = [(n, max(4, int(round(b * target / total / 4)) * 4)) for n, b in plan]
            while sum(b for _, b in plan) > target and len(plan) > 2:   # trim from the middle
                plan.pop(len(plan) // 2)
            diff = target - sum(b for _, b in plan)                     # land on the exact length
            i = len(plan) - 1                                           # the ending absorbs it: an odd
            plan[i] = (plan[i][0], min(64, max(4, plan[i][1] + diff)))  # outro length sounds natural
        out = [{"name": n, "bars": b, "chords": None} for n, b in plan]
    if not out or len(out) > MAX_SECTIONS:
        raise SongError(f"Give 1 to {MAX_SECTIONS} sections.")
    if sum(s["bars"] for s in out) > MAX_BARS:
        raise SongError(f"Keep the song under {MAX_BARS} bars.")
    for s in out:
        base = next((k for k in ENERGY if s["name"].startswith(k)), "verse")
        s["energy"] = ENERGY[base]
        s["chords"] = s["chords"] or PROGRESSIONS[mode].get(base if base in PROGRESSIONS[mode] else "verse",
                                                           PROGRESSIONS[mode]["verse"]).split()
    return out


# ── harmony ───────────────────────────────────────────────────────────────

def voice_lead(chord: List[int], previous: Optional[List[int]], low: int = 52, high: int = 79) -> List[int]:
    """The inversion of `chord` closest to `previous`, kept between low and high."""
    pcs = sorted({n % 12 for n in chord}, key=lambda p: (p - chord[0] % 12) % 12)
    candidates = []
    for rot in range(len(pcs)):
        order = pcs[rot:] + pcs[:rot]
        for base in range(low - 12, high):
            if base % 12 != order[0]:
                continue
            notes, last = [], base - 1
            for pc in order:
                n = last + 1 + (pc - (last + 1)) % 12
                notes.append(n)
                last = n
            if notes[0] >= low and notes[-1] <= high:
                candidates.append(notes)
    if not candidates:
        return sorted(chord)
    if previous is None:
        target = (low + high) / 2
        return min(candidates, key=lambda c: abs(sum(c) / len(c) - target))
    return min(candidates, key=lambda c: sum(abs(a - b) for a, b in zip(sorted(c), sorted(previous)))
               + abs(len(c) - len(previous)) * 6)


def _scale_notes(key: str) -> List[int]:
    tonic = chord_to_midi(key.split()[0], "", 0)[0] % 12
    steps = [0, 2, 4, 5, 7, 9, 11] if key.endswith("major") else [0, 2, 3, 5, 7, 8, 10]
    return sorted(n for n in range(128) if (n - tonic) % 12 in steps)


# ── the parts ─────────────────────────────────────────────────────────────

Note = Tuple[float, float, int, int]          # (start beat, length in beats, MIDI note, velocity)

DRUMS = {   # 16 steps per bar
    "pop":  {"kick": [0, 8], "kick+": [0, 6, 8], "snare": [4, 12], "hat": list(range(0, 16, 2))},
    "edm":  {"kick": [0, 4, 8, 12], "kick+": [0, 4, 8, 12], "snare": [4, 12], "hat": [2, 6, 10, 14]},
    "lofi": {"kick": [0, 7, 10], "kick+": [0, 7, 10], "snare": [4, 12], "hat": list(range(0, 16, 2))},
    "trap": {"kick": [0, 10], "kick+": [0, 7, 10], "snare": [8], "hat": list(range(0, 16, 2))},
}
BASS = {   # (start, length) in beats per bar
    "pop":  [(0, 1), (1, 1), (2, 1), (3, 1)], "pop+": [(i / 2, 0.5) for i in range(8)],
    "edm":  [(0.5, 0.5), (1.5, 0.5), (2.5, 0.5), (3.5, 0.5)], "edm+": [(i / 2 + 0.5, 0.5) for i in range(4)],
    "lofi": [(0, 1.5), (2, 1), (3, 1)], "lofi+": [(0, 1.5), (1.5, 0.5), (2, 1), (3, 1)],
    "trap": [(0, 2.5), (2.75, 1.25)], "trap+": [(0, 1.5), (1.75, 0.75), (2.75, 1.25)],
}
CHORD_RHYTHM = {1: [(0, 4)], 2: [(0, 4)], 3: [(0, 1.5), (1.5, 1), (2.5, 1.5)],
                4: [(0, 0.75), (1, 0.5), (1.5, 1), (2.5, 0.5), (3, 1)], 5: [(0, 0.75), (1, 0.5), (1.5, 1), (2.5, 0.5), (3, 1)]}
MELODY_CELLS = {
    "calm": [[(0, 1), (1, 1), (2, 2)], [(0, 1.5), (1.5, 0.5), (2, 2)], [(0, 2), (2, 1), (3, 1)],
             [(0.5, 0.5), (1, 1), (2, 1.5), (3.5, 0.5)]],
    "busy": [[(0, 0.5), (0.5, 0.5), (1, 1), (2, 1), (3, 1)], [(0, 0.75), (0.75, 0.75), (1.5, 0.5), (2, 1), (3, 0.5), (3.5, 0.5)],
             [(0, 0.5), (0.5, 0.5), (1, 0.5), (1.5, 0.5), (2, 1.5), (3.5, 0.5)], [(0, 1), (1, 0.5), (1.5, 0.5), (2, 2)]],
}


def _vel(rng, base: int) -> int:
    return max(1, min(127, base + rng.randint(-6, 6)))


def drums(style: str, energy: int, bars: int, start: float, fill: bool, crash: bool, rng) -> List[Note]:
    d, out = DRUMS[style], []
    for bar in range(bars):
        t0 = start + bar * 4
        kicks = d["kick+"] if energy >= 4 else d["kick"]
        if energy == 1:
            kicks = [0]
        for s in kicks:
            out.append((t0 + s / 4, 0.25, KICK, _vel(rng, 110)))
        if energy >= 2:
            for s in d["snare"]:
                out.append((t0 + s / 4, 0.25, CLAP if style in ("edm", "trap") else SNARE, _vel(rng, 104)))
        hats = list(range(16)) if (energy >= 4 and style in ("pop", "edm", "trap")) else d["hat"]
        for s in hats:
            accent = 88 if s % 4 == 0 else 70
            out.append((t0 + s / 4, 0.2, OPEN_HAT if (style == "edm" and s in (2, 6, 10, 14)) else HAT, _vel(rng, accent)))
        if style == "trap" and energy >= 4 and bar % 2 == 1:          # hat roll
            out += [(t0 + 3.5 + i / 8, 0.1, HAT, _vel(rng, 60 + i * 6)) for i in range(4)]
    if crash:
        out.append((start, 1.0, CRASH, 100))
    if fill and bars:
        t0 = start + (bars - 1) * 4
        out = [n for n in out if not (n[0] >= t0 + 2 and n[2] in (SNARE, CLAP))]
        out += [(t0 + 2 + i / 4, 0.2, SNARE, 60 + i * 7) for i in range(8)]
    return out


def bass(style: str, energy: int, chord_roots: List[int], start: float, rng) -> List[Note]:
    rhythm = BASS[style + "+"] if energy >= 4 else BASS[style]
    out = []
    for bar, root in enumerate(chord_roots):
        note = 36 + root % 12
        note = note - 12 if note > 43 else note
        for s, ln in (rhythm if energy >= 2 else [(0, 4)]):
            out.append((start + bar * 4 + s, ln, note, _vel(rng, 100 if s % 1 == 0 else 88)))
    return out


def chords_part(voicings: List[List[int]], energy: int, start: float, rng) -> List[Note]:
    out = []
    for bar, chord in enumerate(voicings):
        for s, ln in CHORD_RHYTHM[energy]:
            out += [(start + bar * 4 + s, ln, n, _vel(rng, 78 if s % 1 == 0 else 70)) for n in chord]
    return out


def pad_part(voicings: List[List[int]], start: float, rng) -> List[Note]:
    return [(start + bar * 4, 4, n - 12, _vel(rng, 58)) for bar, ch in enumerate(voicings) for n in ch]


def arp_part(voicings: List[List[int]], start: float, rng) -> List[Note]:
    out = []
    for bar, ch in enumerate(voicings):
        tones = [n + 12 for n in ch] + [ch[0] + 24]
        for i in range(16):
            out.append((start + bar * 4 + i / 4, 0.25, tones[i % len(tones)], _vel(rng, 72 if i % 4 == 0 else 60)))
    return out


def melody(voicings: List[List[int]], energy: int, start: float, scale: List[int], rng) -> List[Note]:
    """A motif-based lead: one phrase rhythm per 4 bars, chord tones on strong beats."""
    low, high = (64, 79) if energy >= 4 else (60, 74)
    pool = [n for n in scale if low <= n <= high]
    cells = MELODY_CELLS["busy" if energy >= 4 else "calm"]
    phrase = [rng.choice(cells) for _ in range(3)]
    phrase.append([(0, 1), (1, 3)])                                   # each phrase ends on a held note
    out, prev = [], pool[len(pool) // 2]
    for bar, chord in enumerate(voicings):
        tones = {n % 12 for n in chord}
        for s, ln in phrase[bar % 4]:
            strong = s in (0, 2)
            if strong or (bar % 4 == 3 and ln >= 2):
                options = [n for n in pool if n % 12 in tones] or pool
            else:
                options = [n for n in pool if 0 < abs(n - prev) <= 4] or pool
            note = min(options, key=lambda n: abs(n - prev) + rng.random() * 3)
            out.append((start + bar * 4 + s, ln * 0.95, note, _vel(rng, 96 if strong else 84)))
            prev = note
    return out


# ── the song ──────────────────────────────────────────────────────────────

def _dedupe(notes: List[Note]) -> List[Note]:
    """One note per pitch per tick (e.g. a hat roll landing on a 16th hat): the louder, longer one.
    Two identical notes at once can't both exist in MIDI."""
    best: Dict[Tuple[int, int], Note] = {}
    for n in notes:
        k = (int(round(n[0] * PPQ)), n[2])
        if k not in best or (n[3], n[1]) > (best[k][3], best[k][1]):
            best[k] = n
    # Same pitch can't overlap either: a note ends where the next one on that pitch starts.
    out, by_pitch = [], {}
    for n in sorted(best.values()):
        by_pitch.setdefault(n[2], []).append(n)
    for seq in by_pitch.values():
        for a, b in zip(seq, seq[1:] + [None]):
            length = a[1] if b is None else min(a[1], b[0] - a[0])
            out.append((a[0], max(length, 1 / PPQ), a[2], a[3]))
    return sorted(out)


def compose(key: str = "A minor", bpm: float = 128, style: str = "pop", sections: str = "",
            minutes: Optional[float] = None, seed: Optional[int] = None) -> dict:
    """The whole arrangement: {bpm, key, style, sections, tracks: {name: [notes]}}."""
    style = (style or "pop").strip().lower()
    if style not in STYLES:
        raise SongError(f"style must be one of {', '.join(STYLES)}.")
    if not 40 <= bpm <= 220:
        raise SongError("bpm must be between 40 and 220.")
    if minutes is not None and not 0.25 <= minutes <= 10:
        raise SongError("minutes must be between 0.25 and 10.")
    key, mode = parse_key(key)
    plan = parse_sections(sections, mode, minutes, bpm)
    rng = random.Random(seed)
    scale = _scale_notes(key)
    tracks: Dict[str, List[Note]] = {name: [] for name in PARTS}
    t, prev = 0.0, None
    for i, sec in enumerate(plan):
        try:
            raw = [chord_to_midi(c, key, 4) for c in sec["chords"]]
        except MusicError as e:
            raise SongError(f"{sec['name']}: {e}")
        voicings = []
        for bar in range(sec["bars"]):
            prev = voice_lead(raw[bar % len(raw)], prev)
            voicings.append(prev)
        roots = [raw[bar % len(raw)][0] for bar in range(sec["bars"])]
        e = sec["energy"]
        nxt = plan[i + 1]["energy"] if i + 1 < len(plan) else 0
        tracks["Drums"] += drums(style, e, sec["bars"], t, fill=nxt > e and e >= 2, crash=e >= 3, rng=rng)
        tracks["Bass"] += bass(style, e, roots, t, rng)
        tracks["Chords"] += chords_part(voicings, e, t, rng)
        if e >= 3:
            tracks["Pad"] += pad_part(voicings, t, rng)
        if e >= 4:
            tracks["Arp"] += arp_part(voicings, t, rng)
        if e >= 2:
            lead = melody(voicings, e, t, scale, rng)
            tracks["Lead"] += lead
            if e >= 4:
                tracks["Lead Double"] += [(s, ln, n + 12, max(1, v - 20)) for s, ln, n, v in lead]
        t += sec["bars"] * 4
    tracks = {k: _dedupe(v) for k, v in tracks.items() if v}
    return {"bpm": float(bpm), "key": key, "style": style, "beats": t,
            "sections": [(s["name"], s["bars"], " ".join(s["chords"])) for s in plan], "tracks": tracks}


# ── MIDI files ────────────────────────────────────────────────────────────

def _varlen(n: int) -> bytes:
    out = [n & 0x7F]
    while n > 0x7F:
        n >>= 7
        out.insert(0, (n & 0x7F) | 0x80)
    return bytes(out)


def _track(events: List[Tuple[int, int, bytes]]) -> bytes:
    """events: (tick, order, bytes) -> an MTrk chunk with delta times and end-of-track."""
    body, last = bytearray(), 0
    for tick, _, data in sorted(events, key=lambda e: (e[0], e[1])):
        body += _varlen(tick - last) + data
        last = tick
    body += b"\x00\xff\x2f\x00"
    return b"MTrk" + struct.pack(">I", len(body)) + bytes(body)


def write_midi(path: str, song: dict, title: str = "") -> str:
    tempo = int(round(60_000_000 / song["bpm"]))
    meta = [(0, 0, b"\xff\x03" + _varlen(len(title.encode())) + title.encode()),
            (0, 1, b"\xff\x51\x03" + tempo.to_bytes(3, "big")),
            (0, 2, b"\xff\x58\x04\x04\x02\x18\x08")]
    chunks = [_track(meta)]
    for name, notes in song["tracks"].items():
        ch, program = PARTS[name]
        ev = [(0, 0, b"\xff\x03" + _varlen(len(name.encode())) + name.encode()),
              (0, 1, bytes([0xC0 | ch, program]))]
        for start, length, note, vel in notes:
            on, off = int(round(start * PPQ)), int(round((start + length) * PPQ))
            ev.append((on, 3, bytes([0x90 | ch, note, vel])))
            ev.append((max(off, on + 1), 2, bytes([0x80 | ch, note, 0])))     # offs before ons at a tick
        chunks.append(_track(ev))
    header = b"MThd" + struct.pack(">IHHH", 6, 1, len(chunks), PPQ)
    with open(path, "wb") as f:
        f.write(header + b"".join(chunks))
    return path


def read_midi(path: str) -> dict:
    """{ppq, tempo_bpm, tracks: {name: [(start beat, length beat, note, velocity)]}} (for checks)."""
    data = open(path, "rb").read()
    assert data[:4] == b"MThd"
    _, fmt, ntrk, ppq = struct.unpack(">IHHH", data[4:14])
    i, out, bpm = 14, {}, None
    for _ in range(ntrk):
        assert data[i:i + 4] == b"MTrk"
        ln = struct.unpack(">I", data[i + 4:i + 8])[0]
        j, end, tick, name, status, open_, notes = i + 8, i + 8 + ln, 0, "", 0, {}, []
        while j < end:
            delta = 0
            while True:
                b = data[j]; j += 1
                delta = (delta << 7) | (b & 0x7F)
                if not b & 0x80:
                    break
            tick += delta
            if data[j] == 0xFF:
                kind, j = data[j + 1], j + 2
                n = 0
                while True:
                    b = data[j]; j += 1
                    n = (n << 7) | (b & 0x7F)
                    if not b & 0x80:
                        break
                payload, j = data[j:j + n], j + n
                if kind == 0x03:
                    name = payload.decode("utf-8", "replace")
                elif kind == 0x51:
                    bpm = 60_000_000 / int.from_bytes(payload, "big")
                continue
            if data[j] & 0x80:
                status, j = data[j], j + 1
            hi = status & 0xF0
            if hi in (0xC0, 0xD0):
                j += 1
                continue
            a, b2, j = data[j], data[j + 1], j + 2
            if hi == 0x90 and b2 > 0:
                open_[a] = (tick, b2)
            elif hi in (0x80, 0x90) and a in open_:
                t0, v = open_.pop(a)
                notes.append((t0 / ppq, (tick - t0) / ppq, a, v))
        if notes or name:
            out[name or f"track{len(out)}"] = sorted(notes)
        i = end
    return {"ppq": ppq, "tempo_bpm": bpm, "tracks": {k: v for k, v in out.items() if v}}


# ── this PC: where songs go, which FL Studio opens them ──────────────────

def song_dir() -> str:
    from modules import vocaloid_tools
    return vocaloid_tools.song_dir()


def fl_studio_exe() -> Optional[str]:
    """constants.FL_STUDIO_EXE, else the newest FL Studio install (by year/version in its folder name)."""
    import constants
    custom = getattr(constants, "FL_STUDIO_EXE", "")
    if custom:
        return custom if os.path.isfile(custom) else None
    root = r"C:\Program Files\Image-Line"
    try:
        folders = [d for d in os.listdir(root) if d.startswith("FL Studio ") and
                   os.path.isfile(os.path.join(root, d, "FL64.exe"))]
    except OSError:
        return None
    return os.path.join(root, max(folders, key=_fl_year), "FL64.exe") if folders else None


def _fl_year(folder: str) -> int:
    """'FL Studio 2026' -> 2026, 'FL Studio 21' -> 2021 (FL 20 and 21 predate year names)."""
    n = int(re.sub(r"\D", "", folder) or 0)
    return n if n >= 1000 else 2000 + n


# ── the tool ──────────────────────────────────────────────────────────────

def available(ctx) -> bool:
    return True                                     # writing a MIDI file needs nothing installed


async def run_tool(name: str, args, ctx) -> str:
    if name != "music_compose":
        return f"Error: unknown composing tool {name}."
    args = args if isinstance(args, dict) else {}
    try:
        try:
            bpm = float(args.get("bpm") or 128)
            minutes = float(args["minutes"]) if args.get("minutes") not in (None, "") else None
        except (TypeError, ValueError):
            raise SongError("bpm and minutes must be numbers.")
        song = compose(str(args.get("key") or "A minor"), bpm, str(args.get("style") or "pop"),
                       str(args.get("sections") or ""), minutes, seed=args.get("seed"))
    except SongError as e:
        return f"Error: {e}"
    from modules.vocaloid_tools import _safe_title, _new_path
    title = _safe_title(str(args.get("title") or ""))
    path = _new_path(song_dir(), title, ".mid")
    write_midi(path, song, title)
    seconds = song["beats"] * 60 / song["bpm"]
    notes = sum(len(v) for v in song["tracks"].values())
    shape = ", ".join(f"{n} {b}" for n, b, _ in song["sections"])
    opened = ""
    exe = fl_studio_exe()
    if exe and args.get("open", True):
        subprocess.Popen([exe, path], close_fds=True)
        opened = " Opened it in FL Studio."
    return (f"Composed {title}: {song['key']}, {song['bpm']:g} BPM, {song['style']}, "
            f"{int(song['beats'] // 4)} bars ({int(seconds // 60)}:{int(seconds % 60):02d}), "
            f"{len(song['tracks'])} parts, {notes} notes. Sections: {shape}. Saved to {path}.{opened}")
