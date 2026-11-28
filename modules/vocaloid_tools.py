"""
Lumi sings, through the VOCALOID6 Editor or Synthesizer V: one tool, sing_song, writes the melody
and lyrics as a project with an installed voice and opens it in the editor, where you press play
or export. app "vocaloid" (default) writes a VOCALOID6 .vpr; app "synthv" writes a Synthesizer V
.svp (modules/synthv_project.py), opened in SYNTHV_EDITOR from .env (e.g. SharpV) or Synthesizer V
Studio.

VOCALOID6 has no scripting, command line or network interface to drive, so Lumi writes the
project file instead. The format is the one VOCALOID6 6.1 saves: a zip holding
Project/sequence.json, 480 ticks per quarter note, tempo as BPM x 100, notes as MIDI numbers
(60 = C4) with a lyric and a language. Phonemes are left for the editor to fill in.

Voices come from the registry (HKLM\\SOFTWARE\\VOCALOID6\\Voice\\Components: compID, name,
language bits); the editor from HKLM\\SOFTWARE\\VOCALOID6\\Application\\Components (Path).
Songs are saved in Documents\\Lumi Songs (constants.VOCALOID_SONG_DIR overrides it). The tool
only writes a new file there and opens the editor, so it doesn't ask for approval.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import zipfile
from typing import Dict, List, Optional

AGENT = "operate"
CONFIRM_TOOLS: set = set()
TASKS = {"sing_song": "Writing a song to sing"}

TPQ = 480                      # VOCALOID ticks per quarter note
BAR = 4 * TPQ
MAX_NOTES = 512
LANGS = {"ja": 0, "japanese": 0, "en": 1, "english": 1, "zh": 4, "chinese": 4}
NO_EFFECT_STYLE = ("No Effect", "0c29827a-4289-495d-94d2-e23602d346c6")   # as VOCALOID6 saves it

TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "sing_song",
        "description": "Write a melody with lyrics as a VOCALOID6 song and open it in the editor to sing.",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string"},
            "notes": {"type": "string", "description": "one syllable per note: note:syllable[:beats], r = rest, e.g. C4:twin C4:kle G4:star:2"},
            "bpm": {"type": "integer"},
            "lang": {"type": "string", "description": "en or ja"},
            "voice": {"type": "string", "description": "voice name, blank = default"},
            "app": {"type": "string", "description": "vocaloid or synthv"}},
            "required": ["notes"]}}},
]

_NOTE = re.compile(r"^([A-Ga-g])([#b]?)(-?\d)$")
_PC = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}


class SongError(ValueError):
    """Bad song input, said in words the model can fix."""


# ── the song ──────────────────────────────────────────────────────────────

def _midi(name: str) -> int:
    m = _NOTE.match(name.strip())
    if not m:
        raise SongError(f"{name!r} isn't a note like C4, F#3 or Bb4.")
    midi = 12 * (int(m.group(3)) + 1) + _PC[m.group(1).upper()] + {"#": 1, "b": -1, "": 0}[m.group(2)]
    if not 0 <= midi <= 127:
        raise SongError(f"{name} is outside the MIDI range.")
    return midi


_RESTS = ("r", "rest", "-")
FORMAT_HINT = "write note:syllable, one syllable per note, e.g. C4:hel D4:lo E4:world:2 r G4:bye"


def parse_notes(notes: str, beats: float = 1.0) -> List[dict]:
    """'C4:twin C4:kle G4:star:2 r:1' -> [{number, lyric, pos, duration}] in ticks.

    Small local models often put a whole phrase on one note ('E4:The code', 'G3:Till 3 AM ...').
    A word that isn't a note or a rest is sung as one more note at the previous note's pitch,
    so the phrase is sung instead of the song failing."""
    out, t, last = [], 0, None
    for token in (notes or "").replace(",", " ").split():
        parts = token.split(":")
        head = parts[0]
        if not (_NOTE.match(head) or head.lower() in _RESTS):
            if last is None:                                  # a word with no note before it
                raise SongError(f"{token!r} isn't a note; {FORMAT_HINT}.")
            ticks = max(1, round(beats * TPQ))
            out.append({"number": last, "lyric": token.replace(":", "")[:32], "pos": t, "duration": ticks})
            t += ticks
            continue
        length = beats
        if len(parts) > 1 and re.fullmatch(r"\d+(\.\d+)?", parts[-1]):
            length = float(parts.pop())
        if length <= 0:
            raise SongError(f"{token}: a note needs a length above 0.")
        ticks = max(1, round(length * TPQ))
        if head.lower() in _RESTS:
            last = None                                       # words after a rest need a new note
        else:
            lyric = (parts[1] if len(parts) > 1 and parts[1] else "la")[:32]
            last = _midi(head)
            out.append({"number": last, "lyric": lyric, "pos": t, "duration": ticks})
        t += ticks
    if not out:
        raise SongError(f"No notes given; {FORMAT_HINT}.")
    if len(out) > MAX_NOTES:
        raise SongError(f"Too many notes ({len(out)}); keep a song under {MAX_NOTES}.")
    return out


def _note(n: dict, lang: int) -> dict:
    # Field defaults as VOCALOID6 6.1 writes them for an AI voice.
    return {"lyric": n["lyric"], "phoneme": "", "langID": lang, "isProtected": False,
            "pos": n["pos"], "duration": n["duration"], "number": n["number"], "velocity": 64,
            "exp": {"accent": 50, "decay": 50, "bendDepth": 0, "bendLength": 0, "opening": 127},
            "aiExp": {k: 0.5 for k in ("pitchFine", "pitchDriftStart", "pitchDriftEnd", "pitchScalingCenter",
                                       "pitchScalingOrigin", "pitchTransitionStart", "pitchTransitionEnd",
                                       "amplitudeWhole", "amplitudeStart", "amplitudeEnd",
                                       "formantWhole", "formantStart", "formantEnd")},
            "singingSkill": {"duration": 20, "weight": {"pre": 64, "post": 64}},
            "vibrato": {"type": 0, "duration": 0}}


def build_sequence(title: str, notes: List[dict], bpm: float, voice: dict, lang: int) -> dict:
    """The Project/sequence.json of a one-track VOCALOID6 project."""
    end = max(n["pos"] + n["duration"] for n in notes)
    length = -(-end // BAR) * BAR                       # whole bars
    langs = [{"langID": i} for i in voice["langs"]] or [{"langID": lang}]
    return {
        "version": {"major": 6, "minor": 1, "revision": 0},
        "vender": "Yamaha Corporation",
        "title": title,
        "masterTrack": {
            "samplingRate": 44100,
            "loop": {"isEnabled": False, "begin": 0, "end": length},
            "tempo": {"isFolded": False, "height": 0.0, "global": {"isEnabled": False, "value": 12000},
                      "ara": {"isEnabled": True}, "events": [{"pos": 0, "value": int(round(bpm * 100))}]},
            "timeSig": {"isFolded": False, "events": [{"bar": 0, "numer": 4, "denom": 4}]},
            "volume": {"isFolded": False, "height": 0.0, "events": [{"pos": 0, "value": 0}]}},
        "voices": [{"compID": voice["compID"], "name": voice["bank"]}],
        "tracks": [{
            "type": 0, "name": title[:40] or "Lumi", "color": 0, "busNo": 0, "isFolded": False, "height": 0.0,
            "volume": {"isFolded": True, "height": 0.0, "events": [{"pos": 0, "value": 0}]},
            "panpot": {"isFolded": True, "height": 0.0, "events": [{"pos": 0, "value": 0}]},
            "isMuted": False, "isSoloMode": False, "lastScrollPositionNoteNumber": max(n["number"] for n in notes),
            "parts": [{
                "pos": 0, "duration": length,
                "styleName": NO_EFFECT_STYLE[0], "stylePresetID": NO_EFFECT_STYLE[1],
                "voice": {"compID": voice["compID"], "langID": lang},
                "aiVoice": {"compID": voice["compID"], "langIDs": langs},
                "notes": [_note(n, lang) for n in notes]}]}],
    }


def write_vpr(path: str, sequence: dict) -> str:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as z:     # VOCALOID6 stores it uncompressed
        z.writestr("Project/Audio/", b"")
        z.writestr("Project/sequence.json", json.dumps(sequence, ensure_ascii=False))
    return path


# ── this PC: voices, editor, song folder ─────────────────────────────────

def _reg_subkeys(path: str) -> Dict[str, Dict[str, str]]:
    try:
        import winreg
    except ImportError:
        return {}
    out = {}
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as root:
            i = 0
            while True:
                try:
                    name = winreg.EnumKey(root, i)
                except OSError:
                    break
                i += 1
                values = {}
                with winreg.OpenKey(root, name) as k:
                    j = 0
                    while True:
                        try:
                            vn, vv, _ = winreg.EnumValue(k, j)
                        except OSError:
                            break
                        values[vn] = str(vv)
                        j += 1
                out[name] = values
    except OSError:
        return {}
    return out


def voices() -> List[dict]:
    """Installed VOCALOID6 voices: [{compID, name, bank, langs}] (langs from the Languages bits)."""
    out = []
    for comp, v in _reg_subkeys(r"SOFTWARE\VOCALOID6\Voice\Components").items():
        try:
            bits = int(v.get("Languages", "0"))
        except ValueError:
            bits = 0
        out.append({"compID": comp, "name": v.get("GroupName") or v.get("BankName") or comp,
                    "bank": v.get("BankName") or comp, "langs": [i for i in range(8) if bits >> i & 1]})
    return sorted(out, key=lambda x: x["name"].lower())


def editor_path() -> Optional[str]:
    for v in _reg_subkeys(r"SOFTWARE\VOCALOID6\Application\Components").values():
        exe = os.path.join(v.get("Path", ""), "VOCALOID6.exe")
        if os.path.isfile(exe):
            return exe
    return None


def song_dir() -> str:
    import constants
    custom = getattr(constants, "VOCALOID_SONG_DIR", "")
    if custom:
        return custom
    docs = os.path.join(os.path.expanduser("~"), "Documents")
    try:                                                  # the real Documents (it may be on OneDrive)
        import ctypes
        buf = ctypes.create_unicode_buffer(260)
        if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf) == 0:
            docs = buf.value
    except Exception:
        pass
    return os.path.join(docs, "Lumi Songs")


def pick_voice(name: str, lang: int, installed: List[dict]) -> dict:
    if not installed:
        raise SongError("No VOCALOID6 voice is installed.")
    if name:
        squash = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())       # "po uta" finds "Po-uta"
        want = squash(name)
        for v in installed:
            if want and want in squash(v["name"] + " " + v["bank"]):
                break
        else:
            raise SongError(f"No VOCALOID6 voice called {name!r}. Installed: {', '.join(v['name'] for v in installed)}.")
    else:
        v = next((v for v in installed if lang in v["langs"]), installed[0])
    if v["langs"] and lang not in v["langs"]:
        raise SongError(f"{v['name']} doesn't sing that language.")
    return v


def _safe_title(title: str) -> str:
    return (re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "", title or "").strip().rstrip(".") or "Lumi song")[:60]


# ── the tool ──────────────────────────────────────────────────────────────

def available(ctx) -> bool:
    from modules import synthv_project as sv
    return bool(editor_path() and voices()) or bool(sv.editor_path() and sv.voices())


def pick_synthv_voice(name: str, installed: List[str]) -> str:
    """By name ('teto' finds 'Kasane Teto'), else SYNTHV_VOICE from .env, else the first AI voice
    (AI voices sing English, Japanese and Mandarin)."""
    if not installed:
        raise SongError("No Synthesizer V voice is installed.")
    squash = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())
    want = squash(name or os.environ.get("SYNTHV_VOICE", ""))
    if want:
        for v in installed:
            if want in squash(v):
                return v
        if name:
            raise SongError(f"No Synthesizer V voice called {name!r}. Some installed: {', '.join(installed[:12])}.")
    return next((v for v in installed if " AI" in v and "(Lite)" not in v), installed[0])


def _new_path(folder: str, title: str, ext: str) -> str:
    os.makedirs(folder, exist_ok=True)
    path, n = os.path.join(folder, f"{title}{ext}"), 2
    while os.path.exists(path):                           # never overwrite an earlier song
        path = os.path.join(folder, f"{title} ({n}){ext}")
        n += 1
    return path


async def run_tool(name: str, args, ctx) -> str:
    if name != "sing_song":
        return f"Error: unknown singing tool {name}."
    args = args if isinstance(args, dict) else {}
    app = str(args.get("app") or "vocaloid").strip().lower().replace(" ", "")
    if app in ("synthv", "synthesizerv", "sv", "sharpv"):
        return _sing_synthv(args)
    if app != "vocaloid":
        return "Error: app must be vocaloid or synthv."
    try:
        lang, bpm, notes = _song_input(args)
        voice = pick_voice(str(args.get("voice") or ""), lang, voices())
    except SongError as e:
        return f"Error: {e}"
    exe = editor_path()
    if not exe:
        return "Error: the VOCALOID6 Editor isn't installed."
    title = _safe_title(str(args.get("title") or ""))
    path = _new_path(song_dir(), title, ".vpr")
    write_vpr(path, build_sequence(title, notes, bpm, voice, lang))
    subprocess.Popen([exe, path], close_fds=True)
    return (f"Wrote {len(notes)} notes for {voice['name']} at {bpm:g} BPM to {path} and opened it in "
            f"VOCALOID6. Press play there to hear it, or export it as audio.")


def _song_input(args) -> tuple:
    """(language id, bpm, notes) from the tool arguments. Raises SongError."""
    lang_key = str(args.get("lang") or "en").strip().lower()
    if lang_key not in LANGS:
        raise SongError("lang must be en or ja.")
    try:
        bpm = float(args.get("bpm") or 120)
    except (TypeError, ValueError):
        raise SongError("bpm must be a number.")
    if not 20 <= bpm <= 300:
        raise SongError("bpm must be between 20 and 300.")
    return LANGS[lang_key], bpm, parse_notes(str(args.get("notes") or ""))


def _sing_synthv(args) -> str:
    """The same song as a Synthesizer V project (modules/synthv_project.py), opened in its editor."""
    from modules import synthv_project as sv
    try:
        lang, bpm, notes = _song_input(args)
        voice = pick_synthv_voice(str(args.get("voice") or ""), sv.voices())
    except SongError as e:
        return f"Error: {e}"
    exe = sv.editor_path()
    if not exe:
        return "Error: no Synthesizer V editor found (set SYNTHV_EDITOR in .env)."
    title = _safe_title(str(args.get("title") or ""))
    path = _new_path(song_dir(), title, ".svp")
    sv.write_svp(path, sv.build_project(title, notes, bpm, voice, lang, TPQ))
    subprocess.Popen([exe, path], close_fds=True)
    return (f"Wrote {len(notes)} notes for {voice} at {bpm:g} BPM to {path} and opened it in "
            f"Synthesizer V. Press play there to hear it, or export it as audio.")
