"""
FL Studio for Lumi: four music tools that drive the FL Studio MCP server
(Zierax/fl-studio-mcp, id "fl-studio" in config/mcp_servers.json).

That server has about 160 tools of its own. They are kept out of the model's tool list
("expose_tools": false), and these wrappers call the few that matter, so the small local
model's context pays for four short schemas instead:

    music_set_tempo(bpm)                  -> fl_set_tempo
    music_create_pattern(name)            -> fl_create_pattern
    music_add_chord(scale, chord_name, octave, at) -> fl_send_chord (MIDI notes worked out here)
    music_add_melody(notes, duration, at) -> fl_send_notes

config/mcp_servers.json (per machine, not committed) needs this entry, with your folder:

    {"id": "fl-studio", "transport": "stdio", "enabled": true, "expose_tools": false,
     "command": "uv", "args": ["run", "--directory", "C:/path/to/fl-studio-mcp", "fl-studio-mcp"]}

What FL Studio needs (the server's docs/SETUP.md):
  * a loopMIDI port (e.g. FLStudioMCP) and the FL Studio MCP Controller script
    (fl_controller/device_FLStudioMCP.py in Documents\\Image-Line\\FL Studio\\Settings\\Hardware\\
    FLStudioMCP\\), chosen in Options > MIDI Settings. Tempo and patterns need only this.
  * for chords and melodies, also the ComposeWithLLM piano roll script
    (scripts/ComposeWithLLM.pyscript copied to ...\\Settings\\Piano roll scripts\\) bound to
    Ctrl+Alt+Y. fl_send_chord / fl_send_notes write mcp_request.json in that folder and press
    Ctrl+Alt+Y in the FL Studio window, so the notes land in the piano roll that is open.

Notes use MIDI numbers with 60 = C4 (the server's own convention). Times and lengths are
in quarter notes. These only edit the open FL Studio project, so they don't ask for approval;
the server's raw tools, if exposed, still go through the MCP approval gate.
"""

from __future__ import annotations

import re
from typing import List, Optional

SERVER_ID = "fl-studio"
AGENT = "operate"
CONFIRM_TOOLS: set = set()
TASKS = {"music_set_tempo": "Setting the tempo", "music_create_pattern": "Making a pattern",
         "music_add_chord": "Adding a chord", "music_add_melody": "Writing a melody"}

TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "music_set_tempo", "description": "Set the FL Studio project tempo.",
        "parameters": {"type": "object", "properties": {
            "bpm": {"type": "integer", "description": "20-999"}}, "required": ["bpm"]}}},
    {"type": "function", "function": {
        "name": "music_create_pattern", "description": "Create a new FL Studio pattern and make it active.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"}}, "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "music_add_chord",
        "description": "Add a chord to the piano roll, by numeral in a key (vi, V7) or by name (Am7).",
        "parameters": {"type": "object", "properties": {
            "scale": {"type": "string", "description": "key, e.g. C major"},
            "chord_name": {"type": "string", "description": "e.g. vi, V7, Am7"},
            "octave": {"type": "integer", "description": "root octave, 4 = middle"},
            "at": {"type": "number", "description": "start beat"}},
            "required": ["chord_name"]}}},
    {"type": "function", "function": {
        "name": "music_add_melody",
        "description": "Add a melody to the piano roll from note names; r is a rest.",
        "parameters": {"type": "object", "properties": {
            "notes": {"type": "string", "description": "e.g. C4 E4 G4 r C5"},
            "duration": {"type": "number", "description": "beats per note"},
            "at": {"type": "number", "description": "start beat"}},
            "required": ["notes"]}}},
]

# ── music theory: names to MIDI numbers ──────────────────────────────────

_PC = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
_SCALES = {"major": [0, 2, 4, 5, 7, 9, 11], "minor": [0, 2, 3, 5, 7, 8, 10]}
_QUALITIES = {
    "": [0, 4, 7], "maj": [0, 4, 7], "m": [0, 3, 7], "min": [0, 3, 7], "dim": [0, 3, 6],
    "aug": [0, 4, 8], "sus2": [0, 2, 7], "sus4": [0, 5, 7], "6": [0, 4, 7, 9], "m6": [0, 3, 7, 9],
    "7": [0, 4, 7, 10], "maj7": [0, 4, 7, 11], "m7": [0, 3, 7, 10], "dim7": [0, 3, 6, 9],
    "m7b5": [0, 3, 6, 10], "9": [0, 4, 7, 10, 14], "add9": [0, 4, 7, 14],
}
_NUMERALS = ["I", "II", "III", "IV", "V", "VI", "VII"]
_ROMAN = re.compile(r"^(VII|VI|V|IV|III|II|I)(7)?$", re.I)
_NOTE = re.compile(r"^([A-G])([#b]?)(-?\d)$")
_ROOT = re.compile(r"^([A-G])([#b]?)(.*)$")


class MusicError(ValueError):
    """Bad musical input, said in words the model can fix."""


def _pc(letter: str, accidental: str) -> int:
    return (_PC[letter] + (1 if accidental == "#" else -1 if accidental == "b" else 0)) % 12


def note_to_midi(name: str) -> int:
    """C4 -> 60, F#3 -> 54, Bb4 -> 70."""
    m = _NOTE.match(name.strip().capitalize() if name else "")
    if not m:
        raise MusicError(f"{name!r} isn't a note like C4, F#3 or Bb4.")
    letter, acc, octave = m.group(1), m.group(2), int(m.group(3))
    midi = 12 * (octave + 1) + _PC[letter] + (1 if acc == "#" else -1 if acc == "b" else 0)
    if not 0 <= midi <= 127:
        raise MusicError(f"{name} is outside the MIDI range.")
    return midi


def _key(scale: str):
    """'C major' / 'F# minor' / 'Am' -> (tonic pitch class, scale steps)."""
    parts = (scale or "").replace("-", " ").split()
    if not parts:
        raise MusicError("Give the key for a numeral chord, like C major or A minor.")
    m = _ROOT.match(parts[0][:1].upper() + parts[0][1:])
    if not m:
        raise MusicError(f"{scale!r} isn't a key like C major or A minor.")
    mode = (parts[1] if len(parts) > 1 else ("minor" if m.group(3).lower() in ("m", "min") else "major")).lower()
    if mode not in _SCALES:
        raise MusicError(f"Only major and minor keys are supported, not {mode!r}.")
    return _pc(m.group(1), m.group(2)), _SCALES[mode]


def chord_to_midi(chord_name: str, scale: str = "", octave: int = 4) -> List[int]:
    """vi in C major, octave 4 -> [69, 72, 76]; Am7, octave 3 -> [57, 60, 64, 67]."""
    name = (chord_name or "").strip()
    base = 12 * (int(octave) + 1)
    roman = _ROMAN.match(name)
    if roman:
        tonic, steps = _key(scale)
        degree = _NUMERALS.index(roman.group(1).upper())
        size = 4 if roman.group(2) else 3                 # stack thirds within the key
        offsets, root_offset = [], None
        for i in range(size):
            d = degree + 2 * i
            step = steps[d % 7] + 12 * (d // 7)
            root_offset = step if root_offset is None else root_offset
            offsets.append(step - root_offset)
        root = base + (tonic + steps[degree]) % 12
    else:
        m = _ROOT.match(name[:1].upper() + name[1:])
        if not m or m.group(3) not in _QUALITIES:
            raise MusicError(f"{chord_name!r} isn't a chord I know (try C, Am, G7, Fmaj7 or a numeral like vi).")
        root, offsets = base + _pc(m.group(1), m.group(2)), _QUALITIES[m.group(3)]
    notes = [root + o for o in offsets]
    if notes[0] < 0 or notes[-1] > 127:
        raise MusicError("That chord is outside the MIDI range; try another octave.")
    return notes


def melody_to_notes(notes: str, duration: float = 1.0, at: float = 0.0) -> List[dict]:
    """'C4 E4 r G4:2' -> fl_send_notes notes, one after another; ':2' sets that note's length."""
    if duration <= 0:
        raise MusicError("duration must be more than 0 beats.")
    out, t = [], float(at)
    for token in (notes or "").replace(",", " ").split():
        name, _, length = token.partition(":")
        beats = float(length) if length else duration
        if beats <= 0:
            raise MusicError(f"{token}: a note needs a length above 0.")
        if name.lower() not in ("r", "rest", "-"):
            out.append({"midi": note_to_midi(name), "duration": beats, "time": t})
        t += beats
    if not out:
        raise MusicError("No notes given (for example: C4 E4 G4).")
    return out


# ── the tools ─────────────────────────────────────────────────────────────

def _mcp(ctx):
    return getattr(getattr(ctx, "signals", None), "mcp", None)


def available(ctx) -> bool:
    """Only offered while the FL Studio server is connected."""
    mcp = _mcp(ctx)
    server = getattr(mcp, "servers", {}).get(SERVER_ID) if mcp is not None else None
    return getattr(server, "state", None) == "connected"


def _num(args, key, default: Optional[float] = None) -> Optional[float]:
    value = args.get(key, default)
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        raise MusicError(f"{key} must be a number.")


def midi_port() -> str:
    """The loopMIDI port FL Studio listens on (constants.FL_STUDIO_MIDI_PORT, else FLStudioMCP)."""
    import constants
    return str(getattr(constants, "FL_STUDIO_MIDI_PORT", "") or "FLStudioMCP")


async def _pin_port(mcp) -> Optional[str]:
    """Point the server at FL Studio's port before its first command, once per server process.
    Left alone it scores every MIDI port and can fall back to a hardware keyboard; with a port
    name it uses only that port, or fails. Returns an error for the model, or None."""
    server = getattr(mcp, "servers", {}).get(SERVER_ID)
    port = midi_port()
    if server is None or getattr(server, "fl_midi_port", None) == port:
        return None
    out = str(await mcp.call_async(SERVER_ID, "fl_connect", {"port_name": port}))
    if not out.startswith("Successfully"):
        return (f"Error: FL Studio's MIDI port {port!r} isn't open. Start loopMIDI with that port and "
                f"pick it in FL Studio's MIDI settings. ({out[:160]})")
    server.fl_midi_port = port       # a reconnect makes a new server object, which pins again
    return None


def _request(name: str, args: dict) -> tuple:
    """Check the input and turn it into (fl tool, arguments). Raises MusicError."""
    if name == "music_set_tempo":
        bpm = _num(args, "bpm")
        if bpm is None or not 20 <= bpm <= 999:
            raise MusicError("bpm must be between 20 and 999.")
        # fl_set_tempo (transport.setTempo) and fl_tempo_set (tempo.set) both end in the
        # controller's mixer.setCurrentTempo(BPM * 1000); this one also checks 20-999 itself.
        return "fl_set_tempo", {"bpm": float(bpm)}
    if name == "music_create_pattern":
        return "fl_create_pattern", {"name": str(args.get("name") or "").strip()}
    if name == "music_add_chord":
        octave = int(_num(args, "octave", 4))
        notes = chord_to_midi(str(args.get("chord_name") or ""), str(args.get("scale") or ""), octave)
        return "fl_send_chord", {"midi_notes": notes, "time": _num(args, "at", 0.0), "duration": 1.0}
    if name == "music_add_melody":
        notes = melody_to_notes(str(args.get("notes") or ""), _num(args, "duration", 1.0), _num(args, "at", 0.0))
        return "fl_send_notes", {"notes": notes, "mode": "add"}
    raise MusicError(f"unknown music tool {name}.")


async def run_tool(name: str, args, ctx) -> str:
    mcp = _mcp(ctx)
    if mcp is None:
        return "Error: FL Studio isn't connected (MCP isn't running)."
    try:
        tool, arguments = _request(name, args if isinstance(args, dict) else {})
    except MusicError as e:
        return f"Error: {e}"
    err = await _pin_port(mcp)                  # only after the input is known to be good
    if err:
        return err
    return await mcp.call_async(SERVER_ID, tool, arguments)
