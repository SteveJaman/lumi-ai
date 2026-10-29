"""Barge-in: talking over Lumi stops her everywhere, drops what she hadn't
said yet, and tells her next turn she was cut off.

Sequence under test: tts.play_audio() → the mic's VAD fires → queues are
cleared and playback stops → the interrupt note is in history."""

import importlib
import queue
import sys
import threading
import time
import types

import pytest


def _stub(name, **attrs):
    if name in sys.modules:
        return
    try:
        importlib.import_module(name)
    except Exception:
        sys.modules[name] = types.SimpleNamespace(**attrs)


for _name, _attrs in (("keyboard", {"add_hotkey": lambda *a, **k: None}),
                      ("RealtimeSTT", {"AudioToTextRecorder": object}),
                      ("pyaudio", {}), ("simpleaudio", {})):
    _stub(_name, **_attrs)

import modules.tts as tts_mod  # noqa: E402
from modules.mic_input import MicInput  # noqa: E402
from modules.tts import INTERRUPT_NOTE, TTS  # noqa: E402


class _Playing:
    """A simpleaudio play object that plays until stopped."""
    def __init__(self):
        self.stopped = False

    def is_playing(self):
        return not self.stopped

    def stop(self):
        self.stopped = True


class _Wave:
    last = None

    @classmethod
    def from_wave_file(cls, _f):
        return cls()

    def play(self):
        _Wave.last = _Playing()
        return _Wave.last


class _Vts:
    def __init__(self):
        self.mouth = []
        self.API = types.SimpleNamespace(set_mouth_open=self.mouth.append)


class _Discord:
    def __init__(self):
        self.stopped = 0

    def stop_voice(self):
        self.stopped += 1


_Discord.__name__ = "DiscordClient"


def _tts(monkeypatch):
    monkeypatch.setattr(tts_mod, "HAS_PYAUDIO", False)
    monkeypatch.setattr(tts_mod, "sa", types.SimpleNamespace(WaveObject=_Wave), raising=False)
    signals = types.SimpleNamespace(
        tts_queue=queue.Queue(), history=[], AI_speaking=False, AI_thinking=False,
        sio_queue=queue.Queue(), terminate=False, last_message_time=0)
    t = TTS.__new__(TTS)
    t.signals, t.enabled, t.audio_mode = signals, True, "local"
    t.is_playing, t._current_sa_obj, t._fetching = False, None, False
    t.audio_queue = queue.Queue()
    t.subtitles = types.SimpleNamespace(show=lambda *a: None, clear=lambda: None)
    vts = _Vts()
    t._find_vts = lambda: vts
    discord = _Discord()
    t.modules = [discord]
    signals.speech_epoch = 0
    signals.tts_instance = t
    stopped_tabs = []
    signals.broadcast_tts_stop = lambda: stopped_tabs.append(True)
    return t, signals, vts, discord, stopped_tabs


def _mic(signals):
    mic = MicInput.__new__(MicInput)
    mic.signals, mic.is_listening = signals, True
    mic._last_interrupt_time, mic._interrupt_cooldown = 0.0, 1.0
    return mic


def _wait(cond, timeout=2.0):
    end = time.time() + timeout
    while time.time() < end and not cond():
        time.sleep(0.01)
    return cond()


def test_talking_over_her_stops_everything_and_notes_it(monkeypatch):
    t, signals, vts, discord, tabs = _tts(monkeypatch)
    signals.tts_queue.put("And another thing I was going to say.")
    t.audio_queue.put(("A sentence already downloaded.", b"RIFF", 0))

    player = threading.Thread(target=t.play_audio, args=("I was saying something long", b"RIFF"))
    player.start()
    assert _wait(lambda: signals.AI_speaking and _Wave.last is not None)

    _mic(signals)._on_vad_start()                    # you start talking

    player.join(timeout=2)
    assert not player.is_alive()
    assert _Wave.last.stopped                        # playback cut
    assert signals.tts_queue.empty() and t.audio_queue.empty()
    assert vts.mouth and vts.mouth[-1] == 0.0        # mouth closed
    assert discord.stopped == 1 and tabs == [True]   # Discord and browser tabs too
    assert signals.AI_speaking is False
    assert signals.speech_epoch == 1
    note = signals.history[-1]
    assert note["content"] == INTERRUPT_NOTE and note["role"] == "system" and note["hidden"]


def test_vad_does_nothing_while_she_is_quiet(monkeypatch):
    t, signals, *_ = _tts(monkeypatch)
    _mic(signals)._on_vad_start()
    assert signals.speech_epoch == 0 and signals.history == []


def test_mid_turn_interrupt_leaves_the_note_to_the_prompter(monkeypatch):
    t, signals, *_ = _tts(monkeypatch)
    signals.AI_thinking = True                       # reply still being written
    assert t.interrupt(source="web")
    assert signals.history == [] and signals.interrupt_pending is True


def test_fish_audio_answer_after_a_barge_in_is_dropped(monkeypatch):
    t, signals, *_ = _tts(monkeypatch)

    def slow_fetch(message):
        t.interrupt(source="mic")                    # you cut in while Fish is answering
        return b"RIFF-late"
    t.fetch_audio = slow_fetch
    monkeypatch.setattr(t, "audio_playback_loop", lambda: None)
    signals.tts_queue.put("A sentence nobody should hear.")
    loop = threading.Thread(target=t.init_event_loop, daemon=True)
    loop.start()
    assert _wait(lambda: signals.speech_epoch == 1)
    time.sleep(0.1)
    signals.terminate = True
    loop.join(timeout=2)
    assert t.audio_queue.empty()


def test_stale_audio_in_the_queue_is_not_played(monkeypatch):
    t, signals, *_ = _tts(monkeypatch)
    played = []
    t.play_audio = lambda m, a: played.append(m)
    signals.speech_epoch = 2
    t.audio_queue.put(("old", b"RIFF", 1))
    t.audio_queue.put(("new", b"RIFF", 2))
    loop = threading.Thread(target=t.audio_playback_loop, daemon=True)
    loop.start()
    assert _wait(lambda: played == ["new"])
    signals.terminate = True
    loop.join(timeout=2)


class _AnyModule(types.ModuleType):
    """Stands in for an optional import the test environment lacks."""
    def __getattr__(self, name):
        return type(name, (), {"__init__": lambda self, *a, **k: None})


def test_the_reply_being_written_stops_too():
    for name in ("ddgs", "pypdf", "docx"):
        try:
            importlib.import_module(name)
        except Exception:
            sys.modules[name] = _AnyModule(name)
    prompter = pytest.importorskip("modules.prompter")
    pushed = []
    host = types.SimpleNamespace(signals=types.SimpleNamespace(history=[], speech_epoch=0),
                                 _push_to_tts=pushed.append)
    stream = prompter._SpokenStream(host)
    assert stream.feed("She was mid-way through a thought. ") is True
    host.signals.speech_epoch = 1                    # barge-in
    assert stream.feed("And she kept going. ") is False
    assert stream.interrupted and pushed == ["She was mid-way through a thought."]
