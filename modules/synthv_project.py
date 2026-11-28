"""
Synthesizer V songs for sing_song (modules/vocaloid_tools.py, app "synthv"): the .svp project
file, the installed voices, and the editor to open it in.

The format is the one Synthesizer V saves (a JSON file; the field layout follows a project it
wrote itself): positions in blicks, 705,600,000 per quarter note; notes are pitch (MIDI, 60 = C4),
lyrics, onset, duration; the voice is mainRef.database {name, language, phoneset}. Phonemes and
the voice's backend are left blank for the editor to fill in.

Voices are the folders in Documents\\Dreamtonics\\Synthesizer V Studio\\databases (folder
"Kasane_Teto" -> voice "Kasane Teto", as projects name it). The editor is SYNTHV_EDITOR from
.env when set (a per-machine path, never committed), else an installed Synthesizer V Studio.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import List, Optional

BLICKS_PER_QUARTER = 705_600_000
LANGS = {0: ("japanese", "romaji"), 1: ("english", "arpabet"), 4: ("mandarin", "xsampa")}
STUDIO_EXES = (r"C:\Program Files\Synthesizer V Studio Pro\synthv-studio.exe",
               r"C:\Program Files\Synthesizer V Studio 2 Pro\synthv-studio.exe",
               r"C:\Program Files\Synthesizer V Studio Basic\synthv-studio.exe")


def _documents() -> str:
    docs = os.path.join(os.path.expanduser("~"), "Documents")
    try:                                                  # the real Documents (it may be on OneDrive)
        import ctypes
        buf = ctypes.create_unicode_buffer(260)
        if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf) == 0:
            docs = buf.value
    except Exception:
        pass
    return docs


def voices(root: Optional[str] = None) -> List[str]:
    """Installed voice names, e.g. 'Kasane Teto', 'Koharu Rikka (Lite)'."""
    root = root or os.path.join(_documents(), "Dreamtonics", "Synthesizer V Studio", "databases")
    try:
        names = [d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))]
    except OSError:
        return []
    return sorted(n.replace("_", " ") for n in names if n.lower() != "tmp")


def editor_path() -> Optional[str]:
    custom = os.environ.get("SYNTHV_EDITOR", "").strip().strip('"')
    if custom:
        return custom if os.path.isfile(custom) else None
    return next((p for p in STUDIO_EXES if os.path.isfile(p)), None)


def _curve() -> dict:
    return {"mode": "cubic", "points": []}


def build_project(title: str, notes: List[dict], bpm: float, voice: str, lang: int, tpq: int = 480) -> dict:
    """notes are sing_song's [{number, lyric, pos, duration}] in `tpq` ticks per quarter."""
    language, phoneset = LANGS[lang]
    scale = BLICKS_PER_QUARTER // tpq
    group = str(uuid.uuid4())
    return {
        "version": 153,
        "time": {"meter": [{"index": 0, "numerator": 4, "denominator": 4}],
                 "tempo": [{"position": 0, "bpm": float(bpm)}]},
        "library": [],
        "tracks": [{
            "name": title[:40] or "Lumi",
            "dispColor": "ff7db235", "dispOrder": 0, "renderEnabled": False,
            "mixer": {"gainDecibel": 0.0, "pan": 0.0, "mute": False, "solo": False, "display": True},
            "mainGroup": {
                "name": "main", "uuid": group,
                "parameters": {k: _curve() for k in ("pitchDelta", "vibratoEnv", "loudness", "tension",
                                                     "breathiness", "voicing", "gender", "toneShift")},
                "notes": [{"onset": n["pos"] * scale, "duration": n["duration"] * scale, "pitch": n["number"],
                           "lyrics": n["lyric"], "phonemes": "", "attributes": {}} for n in notes]},
            "mainRef": {
                "groupID": group, "blickOffset": 0, "pitchOffset": 0, "isInstrumental": False,
                "systemPitchDelta": _curve(),
                "database": {"name": voice, "language": language, "phoneset": phoneset,
                             "languageOverride": "", "phonesetOverride": "", "backendType": ""},
                "dictionary": "",
                "voice": {"vocalModeInherited": True, "vocalModePreset": "", "vocalModeParams": {}},
                "pitchTakes": {"activeTakeId": 0, "takes": [{"id": 0, "expr": 1.0, "liked": False}]},
                "timbreTakes": {"activeTakeId": 0, "takes": [{"id": 0, "expr": 1.0, "liked": False}]}},
            "groups": []}],
        "renderConfig": {"destination": "./", "filename": title[:60] or "untitled", "numChannels": 1,
                         "aspirationFormat": "noAspiration", "bitDepth": 16, "sampleRate": 44100,
                         "exportMixDown": True},
        "instantModeEnabled": True,
    }


def write_svp(path: str, project: dict) -> str:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(project, f, ensure_ascii=False)
    return path
