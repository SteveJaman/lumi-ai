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

    def toggle_mic(self):
        """Toggles the microphone listening state when '\' is pressed (debounced)."""
        now = time.time()
        with self._toggle_lock:
            if now - self._last_toggle_time < self._toggle_cooldown:
                return
            self._last_toggle_time = now

        self.is_listening = not self.is_listening
        if self.is_listening:
            print("\n\033[92m[Mic Input: UNMUTED - Listening active]\033[0m")
        else:
            print("\n\033[91m[Mic Input: MUTED - Press '\\' to unmute]\033[0m")

    def _on_vad_start(self):
        """Fires the instant RealtimeSTT's VAD detects the start of speech —
        BEFORE transcription happens. This is what makes barge-in feel
        instant: we don't wait to hear what you said, just that you
        started talking, and cut Lumi off right then."""
        if not self.is_listening:
            return
        if not getattr(self.signals, 'AI_speaking', False):
            return  # she's not talking, nothing to interrupt

        now = time.time()
        if now - self._last_interrupt_time < self._interrupt_cooldown:
            return
        self._last_interrupt_time = now

        tts = getattr(self.signals, 'tts_instance', None)
        if tts:
            print("\n\033[93m[Mic Input] Interrupting — you started talking\033[0m")
            tts.stop()

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
                on_vad_detect_start=self._on_vad_start,
            )
            print("\033[92mMic Input: Microphone is active. Press '\\' to toggle mute/unmute.\033[0m")
        except Exception as e:
            print(f"\033[91mMic Input Error: Failed to initialize microphone: {e}\033[0m")
            return

        try:
            keyboard.add_hotkey('\\', self.toggle_mic)
        except Exception as e:
            print(f"\033[93mWarning: Could not register global hotkey: {e}\033[0m")

        # Main listening loop
        while not getattr(self.signals, 'terminate', False):
            try:
                if not self.is_listening:
                    time.sleep(0.2)
                    continue

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
                        print(f"\n\033[1\033[95mLumi Voidwright:\033[0m Ok")
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

                    while getattr(self.signals, 'AI_thinking', False):
                        time.sleep(0.1)

                    self.signals.add_message("user", cleaned_text)
                    self.signals.new_message = True

            except Exception as e:
                print(f"\033[91mMic Input Loop Error: {e}\033[0m")
                time.sleep(1)

        if self.recorder:
            try:
                self.recorder.shutdown()
            except Exception:
                pass

    def run(self):
        pass