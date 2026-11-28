"""
Lumi listens to a song: music_analyze reads an audio file and reports its tempo, key, the chord
loop of each section and the section layout, as a plan music_compose can play back. With
recreate=true it composes that recreation in the same call.

What it measures (librosa, read-only):
    tempo     beat tracking on the percussive part
    key       the song's average pitch profile against the Krumhansl key profiles
    chords    one per bar (4 beats), matched against the key's own major and minor triads only,
              so detection noise can't produce off-key chords
    sections  change points in the timbre and harmony, snapped to 4-bar phrases, labelled by
              loudness (quiet start = intro, loud = chorus, quiet end = outro)

What it doesn't: the melody (it can't be pulled reliably from a full mix; music_compose writes
its own over the chords), sounds, or mixing. Tempo may come out at double or half speed.
"""

from __future__ import annotations

import asyncio
import collections
import os
from typing import List, Optional

AGENT = "operate"
CONFIRM_TOOLS: set = set()
TASKS = {"music_analyze": "Listening to a song"}
AUDIO_EXT = (".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aiff", ".aif")
MAX_BYTES = 300 * 1024 * 1024
MAX_SECONDS = 15 * 60
NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
MAJOR = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
MINOR = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]

TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "music_analyze",
        "description": "Listen to a song file and report its tempo, key, chords and sections; recreate=true also composes it.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "audio file (mp3, wav, flac...)"},
            "recreate": {"type": "boolean"},
            "style": {"type": "string", "description": "for recreate: pop, edm, lofi or trap"}},
            "required": ["path"]}}},
]


class AnalysisError(ValueError):
    """A file Lumi can't analyze, said plainly."""


def check_path(path: str) -> str:
    path = os.path.abspath(os.path.expanduser(str(path or "").strip().strip('"')))
    if not path.lower().endswith(AUDIO_EXT):
        raise AnalysisError(f"That isn't an audio file Lumi can read ({', '.join(AUDIO_EXT)}).")
    if not os.path.isfile(path):
        raise AnalysisError(f"No file at {path}.")
    if os.path.getsize(path) > MAX_BYTES:
        raise AnalysisError("That file is too big to analyze (over 300 MB).")
    return path


# ── the measurements ─────────────────────────────────────────────────────

def detect_key(profile) -> tuple:
    """(tonic pitch class, 'major'|'minor', correlation) for a 12-bin pitch profile."""
    import numpy as np
    best = max([(float(np.corrcoef(profile, np.roll(MAJOR, k))[0, 1]), k, "major") for k in range(12)] +
               [(float(np.corrcoef(profile, np.roll(MINOR, k))[0, 1]), k, "minor") for k in range(12)])
    return best[1], best[2], best[0]


def key_triads(tonic: int, mode: str) -> List[tuple]:
    """The key's own major and minor triads: [(name, [pitch classes])] (the diminished one left out)."""
    steps = [0, 2, 4, 5, 7, 9, 11] if mode == "major" else [0, 2, 3, 5, 7, 8, 10]
    out = []
    for d in range(7):
        root, third, fifth = ((tonic + steps[(d + i) % 7]) % 12 for i in (0, 2, 4))
        if (fifth - root) % 12 == 7:
            out.append((NAMES[root] + ("m" if (third - root) % 12 == 3 else ""), [root, third, fifth]))
    return out


def chords_per_bar(beat_chroma, triads) -> List[str]:
    import numpy as np
    templates = []
    for _, (r, t, f) in triads:
        v = np.zeros(12)
        v[[r, t, f]] = [1.0, 0.8, 0.8]                 # the root counts most
        templates.append(v / np.linalg.norm(v))
    templates = np.array(templates)
    out = []
    for b in range(beat_chroma.shape[1] // 4):
        v = beat_chroma[:, b * 4:(b + 1) * 4].mean(axis=1)
        out.append(triads[int(np.argmax(templates @ (v / (np.linalg.norm(v) + 1e-9))))][0])
    return out


def loudness_cuts(bar_energy: List[float], window: int = 4, min_db: float = 2.5) -> List[int]:
    """Bars where the level jumps: the 4 bars after are min_db louder or quieter than the 4 before.
    Exact bars, not a grid, since the first detected beat isn't always a phrase start."""
    import numpy as np
    db = 20 * np.log10(np.maximum(np.asarray(bar_energy, dtype=float), 1e-6))
    jumps = {c: abs(db[c:c + window].mean() - db[c - window:c].mean())
             for c in range(window, len(db) - window + 1)}
    cuts = []
    for c, j in sorted(jumps.items(), key=lambda kv: -kv[1]):      # biggest jumps first
        if j >= min_db and all(abs(c - o) >= window for o in cuts):
            cuts.append(c)
    return sorted(cuts)


def merge_cuts(loud: List[int], timbre, nbars: int, min_gap: int = 4) -> List[int]:
    """Section starts: loudness cuts win, timbre cuts fill in where they are min_gap away."""
    cuts = sorted(set(loud))
    for c in sorted(timbre):
        if 0 < c < nbars and all(abs(c - o) >= min_gap for o in cuts):
            cuts.append(c)
    cuts = sorted(c for c in cuts if min_gap <= c <= nbars - min_gap)
    return [0] + cuts + [nbars]


def label_sections(cuts: List[int], bar_chords: List[str], bar_energy) -> List[dict]:
    """Sections between cut bars, named by loudness, each with its most repeated 4-bar chord loop."""
    import numpy as np
    segs = [{"start": a, "bars": b - a, "energy": float(np.mean(bar_energy[a:b])), "chords": bar_chords[a:b]}
            for a, b in zip(cuts, cuts[1:]) if b > a]
    lo, hi = np.quantile([s["energy"] for s in segs], [0.34, 0.67]) if len(segs) > 1 else (0, 0)
    for i, s in enumerate(segs):
        s["name"] = "verse" if s["energy"] < lo else "pre" if s["energy"] < hi else "chorus"
        if i == 0 and s["energy"] < hi and len(segs) > 1:
            s["name"] = "intro"
        if i == len(segs) - 1 and s["energy"] < lo and len(segs) > 1:
            s["name"] = "outro"
        windows = [tuple(s["chords"][j:j + 4]) for j in range(0, len(s["chords"]) - 3, 4)]
        s["loop"] = list(collections.Counter(windows).most_common(1)[0][0]) if windows else s["chords"][:4]
    return segs


def analyze(path: str) -> dict:
    """The whole analysis of one audio file (slow: run it off the event loop)."""
    import numpy as np
    import librosa
    path = check_path(path)
    y, sr = librosa.load(path, sr=22050, mono=True, duration=MAX_SECONDS)
    seconds = len(y) / sr
    if seconds < 8:
        raise AnalysisError("That recording is too short to analyze (under 8 seconds).")
    harmonic, percussive = librosa.effects.hpss(y)
    tempo, beats = librosa.beat.beat_track(y=percussive, sr=sr, units="frames")
    bpm = float(np.atleast_1d(tempo)[0])
    if len(beats) < 16 or bpm <= 0:
        raise AnalysisError("No steady beat found, so bars and chords can't be measured.")
    chroma = librosa.feature.chroma_cqt(y=harmonic, sr=sr)
    tonic, mode, fit = detect_key(chroma.mean(axis=1))
    beat_chroma = librosa.util.sync(chroma, beats, aggregate=np.median)
    bar_chords = chords_per_bar(beat_chroma, key_triads(tonic, mode))
    nbars = len(bar_chords)
    mfcc = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=13)
    rms = librosa.util.sync(librosa.feature.rms(y=y)[0][None, :], beats)[0]
    # Loudness is the clearest section change (a chorus hits harder), so it is weighted in.
    loud = np.log(rms + 1e-6)[None, :].repeat(6, axis=0)
    feat = librosa.util.normalize(np.vstack([librosa.util.sync(mfcc, beats)[:, :len(rms)],
                                             beat_chroma[:, :len(rms)], loud]), axis=1)
    k = max(2, min(10, nbars // 8))
    bounds = librosa.segment.agglomerative(feat, k) if feat.shape[1] > k else []
    bar_energy = [float(rms[b * 4:(b + 1) * 4].mean()) for b in range(nbars)]
    loud_cuts = loudness_cuts(bar_energy)
    timbre_cuts = {int(round(b / 16)) * 4 for b in bounds}                       # 4-bar phrases
    cuts = merge_cuts(loud_cuts, timbre_cuts, nbars)
    segs = label_sections(cuts, bar_chords, bar_energy)
    key = f"{NAMES[tonic]} {mode}"
    return {
        "file": os.path.basename(path), "seconds": seconds, "bpm": round(bpm), "key": key, "key_fit": fit,
        "bars": nbars, "chords": collections.Counter(bar_chords).most_common(),
        "sections": [{k2: s[k2] for k2 in ("name", "start", "bars", "loop", "energy")} for s in segs],
        "plan": plan_string(segs),
    }


def plan_string(segs: List[dict], max_bars: int = 64) -> str:
    """music_compose sections; a section longer than max_bars is split into equal parts."""
    out = []
    for s in segs:
        parts = -(-s["bars"] // max_bars)
        size, extra = divmod(s["bars"], parts)
        for i in range(parts):
            out.append(f"{s['name']}:{size + (1 if i < extra else 0)}:{'-'.join(s['loop'])}")
    return " ".join(out)


def summary(a: dict) -> str:
    m, s = divmod(int(round(a["seconds"])), 60)
    chords = ", ".join(c for c, _ in a["chords"][:6])
    shape = "; ".join(f"{s2['name']} {s2['bars']} bars ({' '.join(s2['loop'])})" for s2 in a["sections"])
    return (f"{a['file']}: {m}:{s:02d}, {a['bpm']} BPM, {a['key']} (fit {a['key_fit']:.2f}), {a['bars']} bars. "
            f"Main chords: {chords}. Sections: {shape}. To recreate: music_compose with key \"{a['key']}\", "
            f"bpm {a['bpm']}, sections \"{a['plan']}\". (Melody isn't measured; tempo may be double or half.)")


# ── the tool ──────────────────────────────────────────────────────────────

def available(ctx) -> bool:
    import importlib.util
    return importlib.util.find_spec("librosa") is not None


async def run_tool(name: str, args, ctx) -> str:
    if name != "music_analyze":
        return f"Error: unknown listening tool {name}."
    args = args if isinstance(args, dict) else {}
    try:
        path = check_path(args.get("path"))
        a = await asyncio.get_running_loop().run_in_executor(None, analyze, path)
    except AnalysisError as e:
        return f"Error: {e}"
    except Exception as e:                                  # a decoder or librosa failure on a bad file
        return f"Error: couldn't analyze that file ({type(e).__name__}: {str(e)[:160]})."
    out = summary(a)
    if args.get("recreate"):
        from modules import song_composer
        title = f"{os.path.splitext(a['file'])[0]} (Lumi recreation)"
        out += " " + await song_composer.run_tool("music_compose", {
            "title": title, "key": a["key"], "bpm": a["bpm"], "style": args.get("style") or "pop",
            "sections": a["plan"]}, ctx)
    return out
