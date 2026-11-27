from modules.module import Module
import time
import os
import threading
import warnings

# Suppress noisy Hugging Face & PyTorch warnings before importing libraries
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=UserWarning)

import keyboard
from RealtimeSTT import AudioToTextRecorder
import constants
from modules.log_hub import emit as hub_emit
from modules.backchannel import Backchannel
from modules.speculative import SpeculativeTurn
from modules import duplex

# A partial transcript is "stable" (speculative turn) once it has
# SPECULATIVE_MIN_WORDS words and the last re-decode changed at most this many.
STABLE_MAX_WORD_DELTA = 1


def duplex_rms(data):
    """RMS of a 16-bit mono mic chunk (a sample of it: this runs per chunk)."""
    import array as _array
    n = len(data) // 2
    if n == 0:
        return 0.0
    samples = _array.array("h")
    samples.frombytes(data[:n * 2])
    picked = samples[::max(1, n // 256)]
    return (sum(v * v for v in picked) / len(picked)) ** 0.5


class MicInput(Module):
    def __init__(self, signals, enabled=True):
        super().__init__(signals, enabled)
        self.recorder = None
        self.is_listening = True
        self.prompt_injection.text = ""

        # --- Hotkey debounce state ---
        self._toggle_lock = threading.Lock()
        self._last_toggle_time = 0.0
        self._toggle_cooldown = 0.4  # seconds

        # --- Barge-in debounce state ---
        self._last_interrupt_time = 0.0
        self._interrupt_cooldown = 1.0  # don't re-trigger for 1s after an interrupt

        # --- Shutdown watchdog ---
        # RealtimeSTT's text() call blocks indefinitely. Without a way to
        # unblock it, the process gets killed mid-read at shutdown and its
        # worker threads raise BrokenPipeError on the way out. The watchdog
        # watches signals.terminate and calls recorder.shutdown() to unblock.
        self._watchdog_thread = None

        # --- Backchannels and speculative turns ---
        # Both ride this recorder's callbacks: no second mic stream.
        self.backchannel = Backchannel(signals)
        self.speculative = SpeculativeTurn(signals)
        self._recording = False
        self._last_partial = ""
        self._stable_sent = ""

        # --- Full-duplex turn taking (modules/duplex.py) ---
        self.barge = duplex.BargeIn()
        self.echo = duplex.EchoGuard()

    def _wants_partials(self):
        return bool(getattr(constants, "BACKCHANNEL_ENABLED", True)
                    or getattr(constants, "SPECULATIVE_TURN_ENABLED", False)
                    or getattr(constants, "SMART_TURN_ENABLED", False)
                    or getattr(constants, "BARGE_IN_CONFIRM_MS", 0) > 0)

    def _her_level(self):
        tts = getattr(self.signals, 'tts_instance', None)
        level = getattr(tts, "playback_level", None)
        return level.current() if level is not None else 0.0

    def _on_chunk(self, data):
        """Every mic chunk (RealtimeSTT on_recorded_chunk). Runs in the
        recorder's worker, so it stays cheap."""
        if not self.is_listening:
            return
        barge = getattr(self, "barge", None)
        echo = getattr(self, "echo", None)
        if barge is not None and echo is not None:
            try:
                rms = duplex_rms(data)
                play = self._her_level()
                # Learn how loud her echo is while nobody talks over her.
                echo.observe(rms, play, user_active=barge.pending)
                if self._recording and barge.on_chunk(echo.is_user_voice(rms, play),
                                                      len(data) / 2 / 16000 * 1000):
                    self._fire_barge_in("you kept talking")
            except Exception:
                pass
        if not self._recording:
            return
        try:
            self.backchannel.on_chunk(data)
        except Exception:
            pass

    def _on_partial_update(self, text):
        """Every realtime transcript update: the end-of-turn silence for what
        you've said so far, and whether a pending barge-in is real words or
        just a backchannel."""
        text = (text or "").strip()
        if getattr(constants, "SMART_TURN_ENABLED", False) and self.recorder is not None:
            self.recorder.post_speech_silence_duration = duplex.end_of_turn_silence(text)
        barge = getattr(self, "barge", None)
        if barge is not None and barge.on_partial(text):
            self._fire_barge_in(f"heard: {text[:40]}")

    def _on_recording_stop(self):
        self._recording = False
        self.backchannel.on_utterance_end()

    def _on_partial(self, text):
        """Stabilized realtime transcript: the backchannel's venting check,
        and a stable_partial for the speculative turn."""
        text = (text or "").strip()
        self.backchannel.on_partial(text)
        self._on_partial_update(text)
        words = text.split()
        prev = self._last_partial.split()
        self._last_partial = text
        if len(words) < getattr(constants, "SPECULATIVE_MIN_WORDS", 6):
            return
        common = sum(1 for a, b in zip(prev, words) if a.lower() == b.lower())
        delta = max(len(prev), len(words)) - common
        if delta <= STABLE_MAX_WORD_DELTA and text != self._stable_sent:
            self._stable_sent = text
            self._emit_stable_partial(text)

    def _emit_stable_partial(self, text):
        hub_emit(self.signals, "mic", f"stable_partial: {text[:80]}")
        try:
            self.speculative.on_stable_partial(text)
        except Exception as e:
            hub_emit(self.signals, "mic", f"speculative turn failed: {e}", level="warn")

    def toggle_mic(self):
        """Toggles the microphone listening state when '\\' is pressed (debounced)."""
        now = time.time()
        with self._toggle_lock:
            if now - self._last_toggle_time < self._toggle_cooldown:
                return
            self._last_toggle_time = now

        self.is_listening = not self.is_listening
        state = "unmuted" if self.is_listening else "muted"
        hub_emit(self.signals, "mic", f"Microphone {state}")

    def _on_vad_start(self):
        """Fires when RealtimeSTT's VAD starts a recording, before any
        transcription. If she's talking (or writing a reply), this arms the
        barge-in: it's confirmed by what you say next (modules/duplex.py), so
        a "yeah" or "mm-hm" doesn't stop her. BARGE_IN_CONFIRM_MS = 0 stops
        her right here, like before."""
        if not self.is_listening:
            return
        self._recording = True
        self._last_partial = self._stable_sent = ""
        channel = getattr(self, "backchannel", None)
        if channel is not None:
            channel.on_utterance_start()
        if getattr(constants, "SMART_TURN_ENABLED", False) and getattr(self, "recorder", None) is not None:
            self.recorder.post_speech_silence_duration = duplex.end_of_turn_silence("")

        speaking = getattr(self.signals, 'AI_speaking', False)
        # Also while she's writing a reply (before any audio): the same
        # interrupt cancels the generation (speech_epoch), drops speech queued
        # from it, and records that she was cut off (interrupt_pending).
        generating = getattr(self.signals, 'AI_thinking', False)
        if not speaking and not generating:
            return  # she's not talking, nothing to interrupt

        barge = getattr(self, "barge", None)
        if barge is not None and getattr(constants, "BARGE_IN_CONFIRM_MS", 0) > 0:
            barge.arm("speaking" if speaking else "generating")
            return
        self._fire_barge_in("you started talking")

    def _fire_barge_in(self, why=""):
        """Stop her: playback, queued speech and the reply being written."""
        from modules import mood_engine
        mood_engine.record("barge_in")
        now = time.time()
        if now - self._last_interrupt_time < self._interrupt_cooldown:
            return
        self._last_interrupt_time = now
        speaking = getattr(self.signals, 'AI_speaking', False)
        tts = getattr(self.signals, 'tts_instance', None)
        if tts:
            if speaking or not getattr(self.signals, 'AI_thinking', False):
                print(f"\n\033[93m[Mic Input] Interrupting — {why or 'you started talking'}\033[0m")
                tts.interrupt(source="mic")
            else:
                tts.interrupt(source="mic, during generation")
                print("\033[93m[Mic Input] barge-in during generation: cancelled\033[0m")

    def _screen_transcript(self, text):
        """Before a transcript becomes a message: her own echo is dropped,
        and so is a backchannel ("yeah") said over her, which she talks
        through. Returns True if the transcript should be dropped."""
        tts = getattr(self.signals, 'tts_instance', None)
        recent = list(getattr(tts, "recent_spoken", []) or [])
        barge = getattr(self, "barge", None)
        try:
            if duplex.is_echo(text, recent):
                print(f"\033[90m[Mic Input] ignored her own voice: {text[:60]}\033[0m")
                hub_emit(self.signals, "mic", f"echo dropped: {text[:60]}")
                return True
            outcome = barge.resolve_final(text) if barge is not None else None
            if outcome == "drop":
                print(f"\033[90m[Mic Input] '{text}' while she talks: she keeps going\033[0m")
                hub_emit(self.signals, "mic", f"user backchannel, not a turn: {text[:40]}")
                return True
            if outcome == "interrupt":
                self._fire_barge_in("you said something")
            return False
        finally:
            if barge is not None:
                barge.reset()

    def _shutdown_watchdog(self):
        """Watches signals.terminate and shuts down the recorder to unblock
        the blocking recorder.text() call. Without this, the main loop can't
        exit cleanly and the process gets killed mid-read, causing
        BrokenPipeError from RealtimeSTT's worker threads."""
        while not getattr(self.signals, 'terminate', False):
            time.sleep(0.2)

        # Terminate was set — unblock the recorder so text() returns.
        if self.recorder:
            try:
                self.recorder.shutdown()
            except Exception:
                pass

    def init_event_loop(self):
        """Background thread loop that listens to the microphone continuously."""
        if not self.enabled:
            return

        print("\033[90mMic Input: Initializing local Whisper speech-to-text recorder...\033[0m")

        try:
            self.recorder = AudioToTextRecorder(
                model="tiny.en",
                language="en",
                spinner=False,
                device="cpu",
                compute_type="int8",
                input_device_index=1,  # Updated to Focusrite USB Audio index
                # Smart end of turn (SMART_TURN_ENABLED) changes this per utterance.
                post_speech_silence_duration=(duplex.end_of_turn_silence("")
                                              if getattr(constants, "SMART_TURN_ENABLED", False) else 2.2),
                min_length_of_recording=1.0,
                # Speech actually starting a recording. (on_vad_detect_start
                # fires when the recorder starts *waiting* for speech, right
                # after the last transcription, which cut her off late.)
                on_recording_start=self._on_vad_start,
                on_recording_stop=self._on_recording_stop,
                # Backchannels watch the same audio (no second stream).
                on_recorded_chunk=self._on_chunk,
                # Partial transcripts (RealtimeSTT's rolling re-decode) for
                # the venting check and the speculative turn.
                **({"enable_realtime_transcription": True,
                    "realtime_model_type": "tiny.en",
                    "on_realtime_transcription_stabilized": self._on_partial,
                    "on_realtime_transcription_update": self._on_partial_update}
                   if self._wants_partials() else {}),
            )
            self.backchannel.expect_partials = self._wants_partials()
            if getattr(constants, "BACKCHANNEL_ENABLED", True):
                self.backchannel.warm_up_async()
            # The terminal already announces mic status in the startup
            # banner (main.py). Route the detailed line to the hub so it
            # doesn't land between the "Anthony:" prompt and your input.
            hub_emit(self.signals, "mic",
                     "Microphone active — press \\ to toggle mute/unmute")
            
            self.signals.mic_ready = True

        except Exception as e:
            print(f"\033[91mMic Input Error: Failed to initialize microphone: {e}\033[0m")
            return

        # Start the shutdown watchdog thread. It will call recorder.shutdown()
        # the moment signals.terminate flips, which unblocks text() below.
        self._watchdog_thread = threading.Thread(
            target=self._shutdown_watchdog,
            daemon=True,
            name="MicInputShutdownWatchdog",
        )
        self._watchdog_thread.start()

        try:
            keyboard.add_hotkey('\\', self.toggle_mic)
        except Exception as e:
            print(f"\033[93mWarning: Could not register global hotkey: {e}\033[0m")

        # Main listening loop
        try:
            while not getattr(self.signals, 'terminate', False):
                try:
                    if not self.is_listening:
                        time.sleep(0.2)
                        continue

                    # text() blocks until speech is transcribed OR the
                    # recorder is shut down by the watchdog. If shutdown
                    # happened, it raises — we catch that below.
                    spoken_text = self.recorder.text()

                    if not self.is_listening:
                        continue

                    if spoken_text and spoken_text.strip():
                        cleaned_text = spoken_text.strip()
                        if self._screen_transcript(cleaned_text):
                            continue
                        print(f"\nAnthony (Voice): {cleaned_text}")

                        # --- INSTANT STOP INTERCEPTOR ---
                        lowered_speech = cleaned_text.lower()
                        stop_phrases = ["stop", "shut up", "halt", "quiet", "enough"]

                        # Check if the user said just "stop" or requested an immediate halt
                        if any(phrase == lowered_speech or lowered_speech.startswith(phrase + " ") for phrase in stop_phrases):
                            print("\n\033[93m[Mic Input] Intercepted stop command — halting Lumi\033[0m")

                            # 1. Kill active TTS audio immediately
                            tts = getattr(self.signals, 'tts_instance', None)
                            if tts:
                                tts.stop()

                            # 2. Clear any pending LLM thoughts or generation flags
                            self.signals.AI_thinking = False
                            self.signals.AI_speaking = False

                            # 3. Force a quick "Ok" response straight to TTS without hitting the LLM
                            print("\n\033[1m\033[95mLumi Voidwright:\033[0m Ok")
                            if tts and hasattr(tts, 'fetch_audio'):
                                # Push a quick pre-fetched "Ok" or emotional acknowledgement
                                ok_audio = tts.fetch_audio("[sigh] Ok.")
                                if ok_audio:
                                    tts.audio_queue.put(("[sigh] Ok.", ok_audio))

                            # Save the exchange to history cleanly
                            self.signals.add_message("user", cleaned_text)
                            self.signals.add_message("assistant", "[sigh] Ok.")
                            continue
                        # --------------------------------

                        # Speculative turn: accept or reject the run started
                        # from the partial (no-op unless the flag is on).
                        self.speculative.on_final(cleaned_text)

                        while getattr(self.signals, 'AI_thinking', False):
                            time.sleep(0.1)

                        # The local mic is the host: spoken Minecraft
                        # orders go through the same gate as typed ones.
                        self.signals.add_message("user", cleaned_text, from_host=True)
                        self.signals.new_message = True

                except Exception as e:
                    # If terminate is set, the watchdog shut the recorder down.
                    # That's the clean-exit path — break without printing.
                    if getattr(self.signals, 'terminate', False):
                        break
                    print(f"\033[91mMic Input Loop Error: {e}\033[0m")
                    time.sleep(1)
        finally:
            # Ensure the recorder is shut down no matter how we exit.
            if self.recorder:
                try:
                    self.recorder.shutdown()
                except Exception:
                    pass

    def run(self):
        pass