"""Backchannels, barge-in during generation, speculative turns. No mic,
speakers, Fish Audio or Ollama: audio, TTS and the model are stubbed."""

import asyncio
import io
import os
import struct
import subprocess
import types
import wave
from types import SimpleNamespace

import pytest

import constants
from modules import backchannel as bc
from modules import speculative as spec

ROOT = os.path.join(os.path.dirname(__file__), "..")
CHUNK_S = 512 / 16000                     # RealtimeSTT chunk: 512 samples at 16 kHz


def wav(amplitude=8000, n=1600, rate=16000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(struct.pack(f"<{n}h", *([amplitude, -amplitude] * (n // 2))))
    return buf.getvalue()


def loud(n=512):
    return struct.pack(f"<{n}h", *([3000, -3000] * (n // 2)))


def quiet(n=512):
    return b"\x00\x00" * n


class FakeTTS:
    def __init__(self):
        self.fetched, self.played = [], []
        self.is_playing = False
        self._fetching = False
        self.last_audio_end = 0.0
        self.audio_queue = SimpleNamespace(empty=lambda: True)

    def fetch_audio(self, text):
        self.fetched.append(text)
        return wav()

    def play_audio(self, message, audio_bytes, volume=1.0, backchannel=False):
        self.played.append((message, volume, backchannel))


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(bc, "CACHE_ROOT", str(tmp_path / "backchannels"))
    monkeypatch.setenv("FISH_REFERENCE_ID", "voice123")
    return tmp_path / "backchannels"


@pytest.fixture
def chan(cache, monkeypatch):
    sent = []
    s = SimpleNamespace(history=[], terminate=False, AI_thinking=False, AI_speaking=False,
                        broadcast_event=lambda ev, p: sent.append((ev, p)))
    tts = FakeTTS()
    b = bc.Backchannel(s, tts=tts)
    b.ensure_clips()
    monkeypatch.setattr(bc.threading, "Thread", lambda target, args=(), kwargs=None, **k: SimpleNamespace(
        start=lambda: target(*args, **(kwargs or {}))))
    b.sent = sent
    return b


def talk(b, seconds, t0, chunk=loud, partial=None):
    fired = []
    n = int(seconds / CHUNK_S)
    for i in range(n):
        if partial and i == 10:
            b.on_partial(partial)
        now = t0 + i * CHUNK_S
        p = b.on_chunk(chunk(), now=now)
        if p:
            fired.append((p, now))
    return fired


# ── clips ─────────────────────────────────────────────────────
def test_six_clips_cached_per_voice_and_made_only_once(cache):
    tts = FakeTTS()
    b = bc.Backchannel(SimpleNamespace(), tts=tts)
    assert len(bc.BACKCHANNEL_PHRASES) == 6
    assert b.ensure_clips() == 6 and len(tts.fetched) == 6
    files = sorted(os.listdir(cache / "voice123"))
    assert len(files) == 6 and all(f.endswith(".wav") for f in files)
    assert bc.clip_path("Mm-hm.", "voice123") != bc.clip_path("Mm-hm.", "other")     # content-addressed
    tts2 = FakeTTS()
    assert bc.Backchannel(SimpleNamespace(), tts=tts2).ensure_clips() == 6 and tts2.fetched == []
    os.remove(cache / "voice123" / files[0])
    assert bc.Backchannel(SimpleNamespace(), tts=tts2).ensure_clips() == 6 and len(tts2.fetched) == 1


def test_no_tts_means_no_clips_and_no_backchannel(cache):
    b = bc.Backchannel(SimpleNamespace(AI_thinking=False, AI_speaking=False), tts=None)
    assert b.ensure_clips() == 0
    assert bc.decide(b.snapshot()) == (False, "no clips")


# ── when ──────────────────────────────────────────────────────
def test_long_talk_gets_at_most_two_spaced_quieter_backchannels(chan):
    fired = talk(chan, 12.0, 1000.0)
    assert len(fired) == 2
    assert fired[0][1] - 1000.0 >= constants.BACKCHANNEL_MIN_SPEECH_MS / 1000
    assert fired[1][1] - fired[0][1] >= constants.BACKCHANNEL_MIN_GAP_MS / 1000
    assert [(v, b) for _, v, b in chan.tts.played] == [(0.6, True), (0.6, True)]
    assert [ev for ev, _ in chan.sent] == ["backchannel_spoken"] * 2
    ev, payload = chan.sent[0]
    assert set(payload) == {"phrase", "at"} and payload["phrase"] in bc.BACKCHANNEL_PHRASES


def test_short_phrase_gets_none(chan):
    assert talk(chan, 0.6, 1000.0) == []


def test_pauses_reset_the_run(chan):
    t = 1000.0
    for _ in range(6):                       # 0.5 s talk, 0.5 s quiet, repeated
        assert talk(chan, 0.5, t) == []
        talk(chan, 0.5, t + 0.5, chunk=quiet)
        t += 1.0
    assert chan.tts.played == []


def test_venting_gets_none_for_the_whole_utterance(chan):
    chan.expect_partials = True
    assert talk(chan, 10.0, 1000.0, partial="I'm so frustrated with my lab partner, he never") == []
    chan.on_utterance_start()                 # a new utterance starts clean
    assert len(talk(chan, 3.0, 2000.0, partial="anyway we went to the store")) == 1


def test_never_while_she_is_replying_or_soon_after_she_spoke(chan):
    chan.signals.AI_thinking = True
    assert talk(chan, 5.0, 1000.0) == []
    chan.signals.AI_thinking = False
    chan.tts.last_audio_end = 3000.0
    assert talk(chan, 3.0, 3000.5) == []                  # within 4 s of her speech
    assert len(talk(chan, 3.0, 3005.0)) == 1


def test_waits_for_the_first_partial_when_partials_are_on(chan):
    chan.expect_partials = True
    assert talk(chan, 3.0, 1000.0) == []
    chan.on_utterance_start()
    assert len(talk(chan, 3.0, 2000.0, partial="so we drove to the coast")) == 1


def test_disabled(chan, monkeypatch):
    monkeypatch.setattr(constants, "BACKCHANNEL_ENABLED", False)
    assert talk(chan, 5.0, 1000.0) == []


@pytest.mark.parametrize("text, venting", [
    ("I'm so frustrated with this", True), ("this is the worst day", True),
    ("I went to the store and bought eggs", False), ("he's not frustratingly", False)])
def test_venting_keywords(text, venting):
    assert bc.is_venting(text) is venting


def test_should_backchannel_is_the_async_face_of_decide():
    ok = {"enabled": True, "have_clips": True, "speech_ms": 1500, "since_last_ms": 9000, "count": 0,
          "lumi_busy": False, "vented": False, "awaiting_partial": False}
    assert asyncio.run(bc.should_backchannel(ok)) is True
    assert asyncio.run(bc.should_backchannel({**ok, "count": 2})) is False


# ── playback through TTS.play_audio ───────────────────────────
@pytest.fixture
def real_tts(monkeypatch):
    from modules import tts as tts_mod
    writes = []

    class Stream:
        def write(self, data): writes.append(data)
        def stop_stream(self): pass
        def close(self): pass

    class PA:
        def get_format_from_width(self, w): return 8
        def open(self, **kw):
            writes.append(("open", kw.get("output_device_index")))
            return Stream()
        def terminate(self): pass

    monkeypatch.setattr(tts_mod, "HAS_PYAUDIO", True)
    monkeypatch.setattr(tts_mod, "pyaudio", SimpleNamespace(PyAudio=PA), raising=False)
    monkeypatch.setattr(tts_mod, "OUTPUT_DEVICE_INDEX", [3, 5])
    s = SimpleNamespace(history=[], terminate=False, AI_speaking=False, sio_queue=SimpleNamespace(put=lambda x: writes.append(("sio", x))))
    t = tts_mod.TTS(s, enabled=False, audio_mode="local")
    t.subtitles = SimpleNamespace(show=lambda *a: writes.append("subs"), clear=lambda: None)
    return t, writes


def test_backchannel_uses_the_same_output_path_quieter_and_isnt_her_speaking(real_tts):
    t, writes = real_tts
    seen = []
    t.signals.broadcast_tts_audio = lambda b: seen.append("browser")
    t.play_audio("Mm-hm.", wav(8000), volume=0.6, backchannel=True)
    assert [w for w in writes if isinstance(w, tuple) and w[0] == "open"] == [("open", 3), ("open", 5)]
    assert "subs" not in writes and seen == [] and not any(isinstance(w, tuple) and w[0] == "sio" for w in writes)
    assert t.signals.AI_speaking is False and t.is_playing is False and t.last_audio_end == 0.0
    pcm = b"".join(w for w in writes if isinstance(w, bytes))
    assert max(abs(x) for x in struct.unpack(f"<{len(pcm) // 2}h", pcm)) == 4800


def test_barge_in_stop_does_not_cut_a_backchannel(real_tts):
    t, writes = real_tts
    t.is_playing = False                    # what stop() leaves behind
    t.play_audio("Yeah.", wav(8000, n=16000), backchannel=True)
    assert len([w for w in writes if isinstance(w, bytes)]) > 2      # played to the end anyway


def test_normal_speech_path_unchanged(real_tts):
    t, writes = real_tts
    t.play_audio("Hello there.", wav(8000))
    assert "subs" in writes and t.signals.AI_speaking is False and t.last_audio_end > 0
    pcm = b"".join(w for w in writes if isinstance(w, bytes))
    assert max(abs(x) for x in struct.unpack(f"<{len(pcm) // 2}h", pcm)) == 8000


def test_backchannel_spoken_reaches_a_second_client(live_lumi):
    import socketio
    from yarl import URL
    from tests.conftest import LOCAL

    async def scenario(live):
        await live.register()
        live.srv._loop = asyncio.get_running_loop()      # set by _on_startup in production
        cookies = live.local.cookie_jar.filter_cookies(URL(live.local_url))
        cookie = "; ".join(f"{k}={v.value}" for k, v in cookies.items())
        got = {"a": [], "b": []}
        clients = []
        for name in ("a", "b"):
            c = socketio.AsyncClient()
            c.on("backchannel_spoken", (lambda n: (lambda d: got[n].append(d)))(name))
            await c.connect(live.local_url, transports=["polling"], headers={"Origin": LOCAL, "Cookie": cookie})
            clients.append(c)
        await asyncio.sleep(0.2)
        live.srv.signals.broadcast_event("backchannel_spoken", {"phrase": "Mm-hm.", "at": 123.0})
        for _ in range(30):
            if got["a"] and got["b"]:
                break
            await asyncio.sleep(0.1)
        for c in clients:
            await c.disconnect()
        assert got["a"] == got["b"] == [{"phrase": "Mm-hm.", "at": 123.0}]
    live_lumi(scenario)


# ── barge-in during generation (mic_input) ────────────────────
@pytest.fixture
def mic(monkeypatch, cache):
    from modules import mic_input
    calls = []
    s = SimpleNamespace(history=[], terminate=False, AI_thinking=False, AI_speaking=False,
                        tts_instance=SimpleNamespace(interrupt=lambda source: calls.append(source) or True))
    m = mic_input.MicInput(s, enabled=False)
    m.calls = calls
    return m


def test_instant_barge_in_during_generation_when_confirm_is_off(mic, capsys, monkeypatch):
    monkeypatch.setattr(constants, "BARGE_IN_CONFIRM_MS", 0)
    mic.signals.AI_thinking = True
    mic._on_vad_start()
    assert mic.calls == ["mic, during generation"]
    assert "barge-in during generation: cancelled" in capsys.readouterr().out


def test_instant_barge_in_during_playback_when_confirm_is_off(mic, capsys, monkeypatch):
    monkeypatch.setattr(constants, "BARGE_IN_CONFIRM_MS", 0)
    mic.signals.AI_speaking = True
    mic._on_vad_start()
    assert mic.calls == ["mic"]
    assert "Interrupting — you started talking" in capsys.readouterr().out
    mic._on_vad_start()                                  # same 1 s cooldown as before
    assert mic.calls == ["mic"]


def test_no_barge_in_when_idle(mic):
    mic._on_vad_start()
    assert mic.calls == [] and mic._recording is True


def test_interrupt_during_generation_stops_the_stream_and_records_it():
    """The real TTS.interrupt + the prompter's _SpokenStream: the reply being
    written stops, queued speech is dropped, and interrupt_pending is set."""
    import queue
    from modules import tts as tts_mod
    from modules.prompter import _SpokenStream
    s = SimpleNamespace(history=[], terminate=False, AI_thinking=True, AI_speaking=False,
                        tts_queue=queue.Queue())
    t = tts_mod.TTS(s, enabled=False, audio_mode="local")
    t.subtitles = SimpleNamespace(show=lambda *a: None, clear=lambda: None)
    pushed = []
    stream = _SpokenStream(SimpleNamespace(signals=s, _push_to_tts=pushed.append))
    assert stream.feed("Well, the thing is. ")
    s.tts_queue.put("Well, the thing is.")
    assert t.interrupt(source="mic, during generation") is True
    assert stream.feed("more words") is False and stream.interrupted
    assert s.tts_queue.empty() and s.interrupt_pending is True


def test_cooldown_and_recording_minimum_unchanged(mic):
    assert mic._interrupt_cooldown == 1.0
    src = open(os.path.join(ROOT, "modules", "mic_input.py"), encoding="utf-8").read()
    assert "min_length_of_recording=1.0" in src
    assert "if getattr(constants, \"SMART_TURN_ENABLED\", False) else 2.2" in src   # off = the old 2.2 s


# ── stable partials (mic_input) ───────────────────────────────
def test_stable_partial_needs_six_words_and_a_small_delta(mic, monkeypatch):
    got = []
    monkeypatch.setattr(mic, "_emit_stable_partial", got.append)
    mic._on_partial("so I was thinking")
    mic._on_partial("so I was thinking about")
    assert got == []
    mic._on_partial("so I was thinking about the trip")
    mic._on_partial("so I was thinking about the trip")
    assert got == ["so I was thinking about the trip"]
    mic._on_partial("completely different words now said here")
    assert len(got) == 1                                  # big change: not stable yet


# ── speculative turn ──────────────────────────────────────────
class FakePrompter:
    def __init__(self):
        self.requests = []

    def conversational_request(self, text, pending_user=None):
        self.requests.append((text, pending_user))
        return {"model": "dolphin3:8b", "label": "fast", "think": False, "keep_alive": "30m",
                "messages": [{"role": "system", "content": "sys"}, {"role": "user", "content": pending_user}],
                "options": {"num_predict": 200}, "reply_cap": 200, "max_sentences": 2}


def fake_chat(pieces, done=None):
    def chat(model, messages, think=None, keep_alive=None, options=None, on_text=None):
        out = ""
        for p in pieces:
            if done is not None:
                done.wait(5)
            if on_text and on_text(p) is False:
                break
            out += p
        return SimpleNamespace(content=out, raw={"done_reason": "stop", "eval_count": 5})
    return chat


@pytest.fixture
def specturn(monkeypatch):
    monkeypatch.setattr(constants, "SPECULATIVE_TURN_ENABLED", True)
    s = SimpleNamespace(history=[], terminate=False, AI_thinking=False, AI_speaking=False, prompter=FakePrompter())
    return s


def test_flag_is_on_and_when_off_nothing_runs(monkeypatch):
    assert constants.SPECULATIVE_TURN_ENABLED is True
    assert constants.SPECULATIVE_MIN_WORDS == 6 and constants.SPECULATIVE_MATCH_RATIO == 0.8
    monkeypatch.setattr(constants, "SPECULATIVE_TURN_ENABLED", False)
    s = SimpleNamespace(prompter=FakePrompter(), AI_thinking=False, AI_speaking=False)
    t = spec.SpeculativeTurn(s, chat=lambda *a, **k: pytest.fail("no model call when off"))
    assert t.on_stable_partial("what should I cook for dinner tonight") is None
    assert t.on_final("what should I cook for dinner tonight") is None and t.claim("x") is None
    assert s.prompter.requests == []


def test_accept_when_the_final_matches(specturn, capsys):
    t = spec.SpeculativeTurn(specturn, chat=fake_chat(["[happy] Pasta. ", "Definitely pasta."]))
    run = t.on_stable_partial("what should I cook for dinner tonight")
    run.done.wait(5)
    assert run.finished and run.text == "[happy] Pasta. Definitely pasta."
    assert specturn.prompter.requests == [("what should I cook for dinner tonight",) * 2]
    assert t.on_final("What should I cook for dinner tonight?") is True
    assert "speculative accept (ratio=1.00)" in capsys.readouterr().out
    assert t.claim("What should I cook for dinner tonight?") is run and t.claim("x") is None


def test_reject_when_the_final_diverges(specturn, capsys):
    t = spec.SpeculativeTurn(specturn, chat=fake_chat(["Sure."]))
    run = t.on_stable_partial("can you remind me to call my mom")
    assert t.on_final("can you remind me to email my professor about the lab report") is False
    out = capsys.readouterr().out
    assert "speculative reject (ratio=" in out
    assert run.cancel.is_set() and t.claim("anything") is None


def test_altered_last_word_still_accepts_but_a_different_sentence_rejects():
    base = "what should I cook for dinner tonight"
    assert spec.similarity(base, "what should I cook for dinner tomorrow") >= 0.8
    assert spec.similarity(base, "where did I leave my car keys yesterday") < 0.8


def test_nothing_starts_while_she_is_busy(specturn):
    specturn.AI_speaking = True
    t = spec.SpeculativeTurn(specturn, chat=lambda *a, **k: pytest.fail("busy"))
    assert t.on_stable_partial("what should I cook for dinner tonight") is None


def _stub_prompter(signals):
    from modules import prompter as pr
    pushed = []
    obj = SimpleNamespace(signals=signals, _push_to_tts=pushed.append, _live_stream=None)
    obj._finish_speculative = types.MethodType(pr.Prompter._finish_speculative, obj)
    obj._note_route = lambda req: obj.routes.append(req.get("tier"))
    obj.routes = []
    obj.pushed = pushed
    return obj


def test_accepted_run_continues_from_where_it_was_with_the_final_text(specturn, monkeypatch):
    import time as _t
    from modules import prompter as pr

    def slow(model, messages, think=None, keep_alive=None, options=None, on_text=None):
        on_text("[happy] Pasta ")
        while on_text(""):                     # still writing... until it's frozen
            _t.sleep(0.01)
        return SimpleNamespace(content="[happy] Pasta ", raw={})
    t = spec.SpeculativeTurn(specturn, chat=slow)
    t.on_stable_partial("what should I cook for dinner tonite")
    seen = {}

    def cont(model, messages, think=None, keep_alive=None, options=None, on_text=None):
        seen["messages"] = messages
        on_text("with garlic. Trust me.")
        return SimpleNamespace(content="with garlic. Trust me.", raw={"done_reason": "stop", "eval_count": 9})
    monkeypatch.setattr(pr, "chat_sync", cont)
    assert t.on_final("what should I cook for dinner tonight") is True       # one word differs
    claimed = t.claim("what should I cook for dinner tonight")
    obj = _stub_prompter(specturn)
    reply = asyncio.run(obj._finish_speculative(claimed, "what should I cook for dinner tonight"))
    assert not claimed.finished and claimed.cancel.is_set()
    assert seen["messages"][-2] == {"role": "user", "content": "what should I cook for dinner tonight"}
    assert seen["messages"][-1] == {"role": "assistant", "content": "[happy] Pasta "}   # prefill: continue from here
    assert reply == "[happy] Pasta with garlic. Trust me."


def test_a_run_that_finished_early_is_held_then_used_without_a_new_call(specturn, monkeypatch):
    from modules import prompter as pr
    t = spec.SpeculativeTurn(specturn, chat=fake_chat(["[happy] Pasta. ", "Definitely."]))
    run = t.on_stable_partial("what should I cook for dinner tonight")
    run.done.wait(5)
    obj = _stub_prompter(specturn)
    assert obj.pushed == []                               # nothing spoken before the final
    monkeypatch.setattr(pr, "chat_sync", lambda *a, **k: pytest.fail("no second call"))
    t.on_final("what should I cook for dinner tonight")
    reply = asyncio.run(obj._finish_speculative(t.claim("what should I cook for dinner tonight"),
                                                "what should I cook for dinner tonight"))
    assert reply == "[happy] Pasta. Definitely." and obj.pushed


def test_run_conversational_takes_the_flag_path_only_when_on():
    import inspect
    from modules import prompter as pr
    src = inspect.getsource(pr.Prompter.run_conversational)
    first = src.split("req = self.conversational_request(")[0]
    assert "if speculative.enabled():" in first and first.count("\n") < 12


# ── hygiene ───────────────────────────────────────────────────
def test_no_vts_in_backchannel_and_no_new_requirements():
    src = open(os.path.join(ROOT, "modules", "backchannel.py"), encoding="utf-8").read().lower()
    assert "vts" not in src and "vtube" not in src
    diff = subprocess.run(["git", "diff", "--", "requirements.txt"], cwd=ROOT, capture_output=True, text=True).stdout
    assert diff == ""


def test_no_new_audio_stream_code():
    for name in ("backchannel.py", "speculative.py"):
        src = open(os.path.join(ROOT, "modules", name), encoding="utf-8").read()
        for lib in ("pyaudio", "sounddevice", "simpleaudio", "AudioToTextRecorder"):
            assert lib not in src, (name, lib)
