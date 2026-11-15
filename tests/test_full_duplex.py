"""Full-duplex-style voice: smart end of turn, barge-in that ignores
backchannels, the echo guard, streaming TTS, and speculative drafts that
don't use up the turn's context. No mic, speakers, Fish Audio or Ollama."""

import queue
import struct
import threading
import time
from types import SimpleNamespace

import pytest

import constants
from modules import duplex
from modules import tts as tts_mod

CHUNK = 512 * 2                      # one RealtimeSTT chunk: 512 samples, 16-bit
CHUNK_MS = 32.0


def pcm(level, n=512):
    return struct.pack(f"<{n}h", *([level, -level] * (n // 2)))


# ── 1. end of turn ────────────────────────────────────────────
@pytest.mark.parametrize("text, which", [
    ("", "DEFAULT"),
    ("What time is my meeting tomorrow?", "FAST"),
    ("I fixed the bug in the parser.", "FAST"),
    ("So I was thinking that", "SLOW"),
    ("and then we went to the store and", "SLOW"),
    ("Let me think, um", "SLOW"),
    ("I wanted to say,", "SLOW"),
    ("Okay.", "DEFAULT"),                      # too short to be sure
    ("hey lumi", "DEFAULT"),
])
def test_end_of_turn_silence(text, which):
    assert duplex.end_of_turn_silence(text) == getattr(constants, f"TURN_SILENCE_{which}")


def test_end_of_turn_is_faster_than_the_old_fixed_wait():
    assert constants.TURN_SILENCE_FAST < constants.TURN_SILENCE_DEFAULT < constants.TURN_SILENCE_SLOW < 2.2


# ── word classes ──────────────────────────────────────────────
@pytest.mark.parametrize("text, bc", [
    ("yeah", True), ("Mm-hm.", True), ("uh-huh", True), ("oh wow", True), ("Right, right.", True),
    ("got it", True), ("okay sure", True), ("yeah but what about the cat", False),
    ("wait", False), ("what do you mean", False), ("", False),
])
def test_is_backchannel(text, bc):
    assert duplex.is_backchannel(text) is bc


@pytest.mark.parametrize("text, hit", [
    ("wait, that's wrong", True), ("hold on a sec", True), ("stop", True), ("actually no", True),
    ("oh no", False), ("yeah totally", False), ("waiter", False)])
def test_interrupt_words(text, hit):
    assert duplex.has_interrupt_word(text) is hit


# ── 3. barge-in state machine ─────────────────────────────────
def feed(b, ms, voice=True):
    hits = 0
    for _ in range(int(ms / CHUNK_MS)):
        hits += b.on_chunk(voice, CHUNK_MS)
    return hits


def test_a_backchannel_over_her_never_confirms():
    b = duplex.BargeIn()
    b.arm("speaking")
    assert b.on_partial("yeah") is False
    assert feed(b, 900) == 0                       # long "yeeeah" still isn't an interruption
    assert b.resolve_final("Yeah.") == "drop"


def test_real_words_confirm_after_the_confirm_window():
    b = duplex.BargeIn()
    b.arm("speaking")
    assert feed(b, 300) == 0
    b.on_partial("can you also check")
    assert feed(b, 300) == 1 and b.confirmed       # 600 ms >= 500 ms with real words
    assert b.resolve_final("can you also check the weather") is None   # already handled


def test_wait_confirms_at_once_and_force_confirms_eventually():
    b = duplex.BargeIn()
    b.arm("speaking")
    assert b.on_partial("wait") is True
    b.arm("generating")
    assert feed(b, constants.BARGE_IN_FORCE_MS + 40) == 1     # no transcript, but long enough


def test_silence_or_echo_chunks_dont_count():
    b = duplex.BargeIn()
    b.arm("speaking")
    b.on_partial("so the thing is")
    assert feed(b, 2000, voice=False) == 0


def test_a_short_real_utterance_interrupts_at_the_final_transcript():
    b = duplex.BargeIn()
    b.arm("speaking")
    feed(b, 200)
    assert b.resolve_final("what?") == "interrupt"


# ── 2. echo guard ─────────────────────────────────────────────
def test_echo_guard_learns_the_echo_and_still_hears_you():
    g = duplex.EchoGuard()
    for _ in range(80):                             # she talks through speakers, you're quiet
        g.observe(mic_rms=1200, play_rms=3000, user_active=False)
    assert g.gain == pytest.approx(0.4, rel=0.05)
    assert not g.is_user_voice(1300, 3000)          # just her echo
    assert g.is_user_voice(4000, 3000)              # you, clearly louder than the echo
    assert g.is_user_voice(500, 0)                  # she's silent: normal speech threshold


def test_headphones_mean_no_extra_threshold():
    g = duplex.EchoGuard()
    for _ in range(50):
        g.observe(mic_rms=20, play_rms=3000, user_active=False)
    assert g.threshold(3000) == duplex.SPEECH_RMS


def test_your_voice_doesnt_teach_the_echo_guard():
    g = duplex.EchoGuard()
    g.observe(1200, 3000, user_active=False)
    g.observe(9000, 3000, user_active=True)         # someone's talking over her: not learned
    assert g.gain == pytest.approx(0.4)


def test_transcript_of_her_own_words_is_echo():
    now = time.time()
    recent = [(now - 5, "[happy] Pasta with garlic is the move tonight."),
              (now - 2, "Trust me, it never fails.")]
    assert duplex.is_echo("pasta with garlic is the move tonight", recent, now)
    assert duplex.is_echo("trust me it never fails", recent, now)
    assert not duplex.is_echo("what about pizza instead", recent, now)
    assert not duplex.is_echo("pasta with garlic is the move", [(now - 60, "pasta with garlic is the move")], now)


def test_echo_guard_off(monkeypatch):
    monkeypatch.setattr(constants, "ECHO_GUARD_ENABLED", False)
    now = time.time()
    assert not duplex.is_echo("trust me it never fails", [(now, "Trust me, it never fails.")], now)
    assert duplex.EchoGuard().threshold(5000) == duplex.SPEECH_RMS


# ── mic_input wiring ──────────────────────────────────────────
@pytest.fixture
def mic(monkeypatch, tmp_path):
    from modules import backchannel, mic_input
    monkeypatch.setattr(backchannel, "CACHE_ROOT", str(tmp_path))
    calls = []
    tts = SimpleNamespace(interrupt=lambda source: calls.append(source) or True,
                          playback_level=duplex.PlaybackLevel(), recent_spoken=[])
    s = SimpleNamespace(history=[], terminate=False, AI_thinking=False, AI_speaking=False, tts_instance=tts)
    m = mic_input.MicInput(s, enabled=False)
    m.recorder = SimpleNamespace(post_speech_silence_duration=2.2)
    m.calls = calls
    return m


def test_smart_turn_sets_the_recorders_silence_live(mic):
    mic._on_vad_start()
    assert mic.recorder.post_speech_silence_duration == constants.TURN_SILENCE_DEFAULT
    mic._on_partial_update("so I was thinking about")
    assert mic.recorder.post_speech_silence_duration == constants.TURN_SILENCE_SLOW
    mic._on_partial_update("so I was thinking about pizza tonight.")
    assert mic.recorder.post_speech_silence_duration == constants.TURN_SILENCE_FAST


def test_saying_yeah_while_she_talks_doesnt_stop_her(mic):
    mic.signals.AI_speaking = True
    mic._on_vad_start()
    assert mic.calls == [] and mic.barge.pending
    for _ in range(20):
        mic._on_chunk(pcm(3000))
    mic._on_partial_update("yeah")
    assert mic.calls == []
    assert mic._screen_transcript("Yeah.") is True              # not sent as a message
    assert mic.calls == [] and not mic.barge.pending


def test_talking_over_her_stops_her_once_confirmed(mic, capsys):
    mic.signals.AI_speaking = True
    mic._on_vad_start()
    mic._on_partial_update("could you also")
    for _ in range(20):
        mic._on_chunk(pcm(3000))
    assert mic.calls == ["mic"]
    assert "Interrupting" in capsys.readouterr().out
    assert mic._screen_transcript("could you also check the weather") is False


def test_wait_stops_her_immediately(mic):
    mic.signals.AI_speaking = True
    mic._on_vad_start()
    mic._on_partial_update("wait")
    assert mic.calls == ["mic"]


def test_during_generation_a_real_question_cancels_and_logs(mic, capsys):
    mic.signals.AI_thinking = True
    mic._on_vad_start()
    mic._on_partial_update("actually make it shorter")
    assert mic.calls == ["mic, during generation"]
    assert "barge-in during generation: cancelled" in capsys.readouterr().out


def test_her_echo_through_speakers_doesnt_interrupt_or_become_a_turn(mic):
    mic.signals.AI_speaking = True
    tts = mic.signals.tts_instance
    for _ in range(60):                                         # learning: she plays, you're quiet
        tts.playback_level.note(3000)
        mic._on_chunk(pcm(1200))
    mic._on_vad_start()                                         # the VAD fires on her echo
    for _ in range(60):
        tts.playback_level.note(3000)
        mic._on_chunk(pcm(1300))
    assert mic.calls == []                                      # never confirmed
    tts.recent_spoken.append((time.time(), "Honestly the ramen place downtown is great."))
    assert mic._screen_transcript("honestly the ramen place downtown is great") is True
    assert mic.calls == []


# ── 4. streaming TTS ──────────────────────────────────────────
class FakeResponse:
    def __init__(self, pieces, gap=0.0, gate=None):
        self.pieces, self.gap, self.gate, self.closed = pieces, gap, gate, False

    def iter_content(self, n):
        for p in self.pieces:
            if self.gate is not None:
                self.gate.wait(5)
            if self.closed:
                return
            time.sleep(self.gap)
            yield p

    def close(self):
        self.closed = True


@pytest.fixture
def fake_audio(monkeypatch):
    log = []

    class Stream:
        def write(self, data):
            log.append(("write", time.time(), len(data)))
        def stop_stream(self): pass
        def close(self): pass

    class PA:
        def get_format_from_width(self, w): return 8
        def open(self, **kw):
            log.append(("open", kw.get("rate"), kw.get("output_device_index")))
            return Stream()
        def terminate(self): pass

    monkeypatch.setattr(tts_mod, "HAS_PYAUDIO", True)
    monkeypatch.setattr(tts_mod, "pyaudio", SimpleNamespace(PyAudio=PA), raising=False)
    monkeypatch.setattr(tts_mod, "OUTPUT_DEVICE_INDEX", [3])
    monkeypatch.setattr(constants, "TTS_PREBUFFER_MS", 20)
    return log


@pytest.fixture
def tts(fake_audio, monkeypatch):
    monkeypatch.setattr(tts_mod, "FISH_API_KEY", "test-key")
    monkeypatch.setattr(tts_mod, "VOICE_ENGINE", "fish")
    s = SimpleNamespace(history=[], terminate=False, AI_speaking=False, AI_thinking=False,
                        sio_queue=queue.Queue(), tts_queue=queue.Queue(), speech_epoch=0)
    t = tts_mod.TTS(s, enabled=False, audio_mode="local")
    t.subtitles = SimpleNamespace(show=lambda *a: None, clear=lambda: None)
    return t


def test_streaming_audio_buffers_and_reads_frame_aligned():
    st = tts_mod.StreamingAudio(FakeResponse([b"\x01\x00" * 3 + b"\x02", b"\x00" * 5]), 44100)
    st.pump()
    assert st.done and len(st.buf) == 12
    assert st.read(0, 7) == bytes(st.buf[:6])                  # never half a sample
    assert st.finished_at(12) and not st.finished_at(6)
    assert st.wav_bytes()[:4] == b"RIFF"


def test_playback_starts_before_the_download_finishes(tts, fake_audio):
    slow = FakeResponse([pcm(2000, 4410)] * 10, gap=0.05)       # ~1 s of audio over ~0.5 s
    st = tts_mod.StreamingAudio(slow, 44100)
    pump = threading.Thread(target=st.pump)
    pump.start()
    tts.play_stream("A long streamed sentence.", st)
    pump.join()
    writes = [t for kind, t, *_ in fake_audio if kind == "write"]
    assert ("open", 44100, 3) in fake_audio
    assert writes and writes[0] < writes[-1] and writes[0] < time.time()
    first_write_before_done = writes[0] <= st.started + 0.45
    assert first_write_before_done                                # didn't wait for all 10 pieces
    assert sum(n for kind, _, n in fake_audio if kind == "write") == len(st.buf)
    assert tts.signals.AI_speaking is False and tts.last_audio_end > 0
    assert len(tts.playback_level._levels) > 0                   # the echo guard saw her level
    assert tts.recent_spoken[-1][1] == "A long streamed sentence."


def test_barge_in_cancels_a_stream_mid_download(tts):
    gate = threading.Event()
    resp = FakeResponse([pcm(2000, 4410)] * 50, gate=gate)
    st = tts_mod.StreamingAudio(resp, 44100)
    with tts._streams_lock:
        tts._streams.add(st)
    gate.set()
    t = threading.Thread(target=st.pump)
    t.start()
    tts.stop()
    t.join(3)
    assert st.cancelled and st.done and resp.closed and not t.is_alive()


def test_download_loop_streams_queues_in_order_and_sends_tabs_the_whole_clip(tts, monkeypatch):
    sent = []
    tts.signals.broadcast_tts_audio = sent.append
    monkeypatch.setattr(tts_mod.requests, "post",
                        lambda *a, **k: SimpleNamespace(raise_for_status=lambda: None,
                                                        **{"iter_content": FakeResponse([pcm(1000, 2204)] * 2).iter_content,
                                                           "close": lambda: None}))
    assert tts._stream_sentence("First sentence.", 0) is True
    item = tts.audio_queue.get_nowait()
    assert item[0] == "First sentence." and isinstance(item[1], tts_mod.StreamingAudio) and item[1].done
    assert len(sent) == 1 and sent[0][:4] == b"RIFF"


@pytest.mark.parametrize("change, streams", [
    ({}, True),
    ({"TTS_STREAMING": False}, False),
    ({"VOICE_ENGINE": "local_fast"}, False),
    ({"voice_context": {"active": True}, "audio_mode": "both"}, False),
    ({"audio_mode": "discord"}, False),
    ({"voice_context": {"active": False}, "audio_mode": "both"}, True),
])
def test_when_it_streams(tts, monkeypatch, change, streams):
    for k, v in change.items():
        if k == "TTS_STREAMING":
            monkeypatch.setattr(constants, k, v)
        elif k == "VOICE_ENGINE":
            monkeypatch.setattr(tts_mod, k, v)
        elif k == "voice_context":
            tts.signals.voice_context = v
        elif k == "audio_mode":
            tts.audio_mode = v
    assert tts._can_stream() is streams


def test_a_failed_stream_falls_back_to_a_whole_clip(tts, monkeypatch):
    def boom(*a, **k):
        raise ConnectionError("down")
    monkeypatch.setattr(tts_mod.requests, "post", boom)
    assert tts._stream_sentence("Hello.", 0) is False
    assert tts.audio_queue.empty()


def test_stale_stream_is_cancelled_not_played(tts, monkeypatch):
    played = []
    monkeypatch.setattr(tts, "play_stream", lambda *a: played.append(a))
    st = tts_mod.StreamingAudio(FakeResponse([]), 44100)
    tts.audio_queue.put(("Old.", st, 0))
    tts.signals.speech_epoch = 1                                # a barge-in happened
    tts.signals.terminate = False
    t = threading.Thread(target=tts.audio_playback_loop, daemon=True)
    t.start()
    time.sleep(0.3)
    tts.signals.terminate = True
    t.join(2)
    assert played == [] and st.cancelled


# ── 5. speculative drafts don't use up the turn ───────────────
def test_draft_injections_are_not_consumed_until_the_real_turn():
    from modules.injection import Injection
    from modules.prompter import Prompter
    s = SimpleNamespace(history=[], terminate=False, AI_thinking=False, AI_speaking=False)
    mod = SimpleNamespace(enabled=True, prompt_injection=Injection("ONE-SHOT NOTE", 1))
    mod.get_prompt_injection = lambda: mod.prompt_injection
    p = Prompter(s, modules=[mod])
    p._backstory_turns = 0
    start = p._turns_since_start
    draft = p.gather_injections("tell me your backstory", agent_context=False, consume=False)
    assert "ONE-SHOT NOTE" in draft
    assert p._turns_since_start == start and p._backstory_turns == 0 and mod.prompt_injection.text
    real = p.gather_injections("tell me your backstory", agent_context=False)
    assert "ONE-SHOT NOTE" in real
    assert p._turns_since_start == start + 1 and p._backstory_turns > 0 and not mod.prompt_injection.text


def test_speculative_is_on_and_everything_has_an_off_switch():
    assert constants.SPECULATIVE_TURN_ENABLED is True
    for name in ("SMART_TURN_ENABLED", "ECHO_GUARD_ENABLED", "TTS_STREAMING"):
        assert getattr(constants, name) is True
    assert constants.BARGE_IN_CONFIRM_MS > 0


def test_no_speculative_drafts_in_gaming_mode(monkeypatch):
    from modules import speculative
    monkeypatch.setattr(constants, "SPECULATIVE_TURN_ENABLED", True)
    monkeypatch.setattr(constants, "GAMING_MODE", False, raising=False)
    assert speculative.enabled()
    monkeypatch.setattr(constants, "GAMING_MODE", True, raising=False)
    assert not speculative.enabled()
