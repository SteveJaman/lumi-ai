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

# A partial transcript is "stable" (speculative turn) once it has
# SPECULATIVE_MIN_WORDS words and the last re-decode changed at most this many.
STABLE_MAX_WORD_DELTA = 1


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

    def _wants_partials(self):
        return bool(getattr(constants, "BACKCHANNEL_ENABLED", True)
                    or getattr(constants, "SPECULATIVE_TURN_ENABLED", False))

    def _on_chunk(self, data):
        """Every mic chunk (RealtimeSTT on_recorded_chunk). Runs in the
        recorder's worker, so it stays cheap."""
        if not self._recording or not self.is_listening:
            return
        try:
            self.backchannel.on_chunk(data)
        except Exception:
            pass

    def _on_recording_stop(self):
        self._recording = False
        self.backchannel.on_utterance_end()

    def _on_partial(self, text):
        """Stabilized realtime transcript: the backchannel's venting check,
        and a stable_partial for the speculative turn."""
        text = (text or "").strip()
        self.backchannel.on_partial(text)
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
        """Fires the instant RealtimeSTT's VAD detects the start of speech —
        BEFORE transcription happens. This is what makes barge-in feel
        instant: we don't wait to hear what you said, just that you
        started talking, and cut Lumi off right then."""
        if not self.is_listening:
            return
        self._recording = True
        self._last_partial = self._stable_sent = ""
        channel = getattr(self, "backchannel", None)
        if channel is not None:
            channel.on_utterance_start()

        speaking = getattr(self.signals, 'AI_speaking', False)
        # Also while she's writing a reply (before any audio): the same
        # interrupt cancels the generation (speech_epoch), drops speech queued
        # from it, and records that she was cut off (interrupt_pending).
        generating = getattr(self.signals, 'AI_thinking', False)
        if not speaking and not generating:
            return  # she's not talking, nothing to interrupt

        now = time.time()
        if now - self._last_interrupt_time < self._interrupt_cooldown:
            return
        self._last_interrupt_time = now

        tts = getattr(self.signals, 'tts_instance', None)
        if tts:
            if speaking:
                print("\n\033[93m[Mic Input] Interrupting — you started talking\033[0m")
                tts.interrupt(source="mic")
            else:
                tts.interrupt(source="mic, during generation")
                print("\033[93m[Mic Input] barge-in during generation: cancelled\033[0m")

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
                post_speech_silence_duration=2.2,
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
                    "on_realtime_transcription_stabilized": self._on_partial}
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