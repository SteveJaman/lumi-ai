import os
import re
import time
import queue
import io
import wave
import requests
import threading
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

FISH_API_KEY = os.getenv("FISH_API_KEY")
FISH_REFERENCE_ID = os.getenv("FISH_REFERENCE_ID")

# Attempt to load routing configuration
try:
    from constants import OUTPUT_DEVICE_INDEX, AUDIO_OUTPUT_MODE, FISH_REFERENCE_IDS
except ImportError:
    OUTPUT_DEVICE_INDEX = None
    AUDIO_OUTPUT_MODE = "both"
    FISH_REFERENCE_IDS = {}

# Attempt to load PyAudio for hardware output device routing support
try:
    import pyaudio
    HAS_PYAUDIO = True
except ImportError:
    HAS_PYAUDIO = False
    import simpleaudio as sa


# ===============================================================
# LANGUAGE DETECTION
# ---------------------------------------------------------------
# Lightweight, character-range based. Not perfect, but for the
# handful of languages Lumi actually speaks, it's fast and reliable.
# Order matters: Japanese must come before Chinese-range checks
# because kanji shares codepoints with CJK unified ideographs.
# ===============================================================
_LANG_PATTERNS = [
    ("ja", re.compile(r"[\u3040-\u309F\u30A0-\u30FF]")),
    ("ko", re.compile(r"[\uAC00-\uD7AF]")),
    ("vi", re.compile(r"[ăâđêôơưĂÂĐÊÔƠƯ"
                      r"\u00C0-\u00FF\u0102\u0103\u0110\u0111\u0128\u0129"
                      r"\u0168\u0169\u01A0\u01A1\u01AF\u01B0\u1EA0-\u1EF9]")),
    ("es", re.compile(r"[ñÑ¿¡]")),
    ("ru", re.compile(r"[\u0400-\u04FF]")),
    ("zh", re.compile(r"[\u4E00-\u9FFF]")),
]


def _detect_language(text: str) -> str:
    """Return an ISO 639-1 code, defaulting to 'en'."""
    if not text:
        return "en"
    for code, pattern in _LANG_PATTERNS:
        if pattern.search(text):
            return code
    return "en"


class APIHandler:
    """Exposes status and control methods to main.py via tts.API"""
    def __init__(self, outer):
        self.outer = outer

    def set_TTS_status(self, status):
        self.outer.enabled = status
        if not status:
            self.outer.stop()
        self.outer.signals.sio_queue.put(('TTS_status', status))

    def get_TTS_status(self):
        return self.outer.enabled

    def abort_current(self):
        self.outer.stop()


class TTS:
    def __init__(self, signals, enabled=True, modules=None, audio_mode="both"):
        self.signals = signals
        self.enabled = enabled
        self.modules = modules if modules is not None else []
        self.audio_mode = audio_mode
        self.is_playing = False
        self._current_sa_obj = None

        # DOUBLE BUFFERING QUEUE
        self.audio_queue = queue.Queue()

        # Attach the API helper for external control calls
        self.API = APIHandler(self)

        self.signals.tts_instance = self

        if self.enabled:
            self.init_engine()

    def init_engine(self):
        """Validates API environment variables and marks TTS as ready."""
        try:
            if not FISH_API_KEY or not FISH_REFERENCE_ID:
                print("\033[91mTTS Error: Missing FISH_API_KEY or "
                      "FISH_REFERENCE_ID in .env file!\033[0m")
            else:
                self.signals.tts_ready = True
                configured = [
                    code for code, ref in (FISH_REFERENCE_IDS or {}).items()
                    if ref
                ]
                if configured:
                    print(f"\033[90mTTS: Fish Audio ready "
                          f"(multi-language: {', '.join(configured)}).\033[0m")
                else:
                    print(f"\033[90mTTS: Fish Audio ready "
                          f"(single reference).\033[0m")
        except Exception as e:
            print(f"\033[91mTTS Load Error: {e}\033[0m")

    def _reference_for(self, text: str) -> str:
        """Pick the reference_id best matching the text's language.
        Falls back to the .env default if no per-language ref is set."""
        lang = _detect_language(text)
        ref = (FISH_REFERENCE_IDS or {}).get(lang)
        return ref or FISH_REFERENCE_ID

    def fetch_audio(self, message):
        """Fetches WAV bytes from Fish Audio."""
        clean_message = re.sub(r'\*.*?\*', '', message).strip()
        if not clean_message:
            return None

        reference_id = self._reference_for(clean_message)

        try:
            response = requests.post(
                "https://api.fish.audio/v1/tts",
                headers={
                    "Authorization": f"Bearer {FISH_API_KEY}",
                    "Content-Type": "application/json",
                    "model": "s2.1-pro-free",
                },
                json={
                    "text": clean_message,
                    "reference_id": reference_id,
                    "format": "wav",
                    "prosody": {
                        "speed": 1.0,
                        "temperature": 0.7
                    }
                },
                timeout=30
            )
            response.raise_for_status()
            return response.content
        except Exception as e:
            print(f"\033[91mTTS API Download Error: {e}\033[0m")
            return None

    def play_audio(self, message, audio_bytes):
        """Physically plays the bytes and routes to Discord."""
        self.signals.sio_queue.put(("current_message", message))

        # Discord Routing
        if self.audio_mode in ["discord", "both"]:
            for module in self.modules:
                if module.__class__.__name__ == "DiscordClient":
                    module.play_audio_in_voice(audio_bytes)

        # Local Playback
        if self.audio_mode in ["local", "both"]:
            self.is_playing = True
            self.audio_started()

            try:
                if HAS_PYAUDIO:
                    wf = wave.open(io.BytesIO(audio_bytes), 'rb')
                    p = pyaudio.PyAudio()

                    device_indices = []
                    if OUTPUT_DEVICE_INDEX is not None:
                        if isinstance(OUTPUT_DEVICE_INDEX, (list, tuple)):
                            device_indices = OUTPUT_DEVICE_INDEX
                        else:
                            device_indices = [OUTPUT_DEVICE_INDEX]
                    else:
                        device_indices = [None]

                    format_val = p.get_format_from_width(wf.getsampwidth())
                    channels_val = wf.getnchannels()
                    rate_val = wf.getframerate()

                    all_data = wf.readframes(wf.getnframes())
                    bytes_per_chunk = 1024 * channels_val * wf.getsampwidth()

                    threads = []

                    def device_worker(dev_idx):
                        """Independent thread for writing to a specific hardware device."""
                        try:
                            kwargs = {}
                            if dev_idx is not None:
                                kwargs['output_device_index'] = dev_idx

                            st = p.open(
                                format=format_val,
                                channels=channels_val,
                                rate=rate_val,
                                output=True,
                                **kwargs
                            )

                            for i in range(0, len(all_data), bytes_per_chunk):
                                if not self.is_playing:
                                    break
                                st.write(all_data[i:i+bytes_per_chunk])

                            st.stop_stream()
                            st.close()
                        except Exception as e:
                            print(f"\033[91mTTS PyAudio Device "
                                  f"{dev_idx} Error: {e}\033[0m")

                    for dev_idx in device_indices:
                        t = threading.Thread(target=device_worker, args=(dev_idx,))
                        threads.append(t)
                        t.start()

                    for t in threads:
                        t.join()

                    p.terminate()

                else:
                    wave_obj = sa.WaveObject.from_wave_file(io.BytesIO(audio_bytes))
                    self._current_sa_obj = wave_obj.play()
                    while self._current_sa_obj.is_playing() and self.is_playing:
                        time.sleep(0.05)
                    if not self.is_playing:
                        self._current_sa_obj.stop()
                    self._current_sa_obj = None

            except Exception as e:
                print(f"\033[91mTTS Playback Error: {e}\033[0m")
            finally:
                self.is_playing = False
                self.audio_ended()
        else:
            self.audio_ended()

    def stop(self):
        """Aborts playback and completely clears all queued speech."""
        self.is_playing = False

        if hasattr(self.signals, 'tts_queue'):
            with self.signals.tts_queue.mutex:
                self.signals.tts_queue.queue.clear()

        with self.audio_queue.mutex:
            self.audio_queue.queue.clear()

        if not HAS_PYAUDIO and self._current_sa_obj:
            try:
                self._current_sa_obj.stop()
            except Exception:
                pass

        self.signals.AI_speaking = False

    def audio_started(self):
        self.signals.AI_speaking = True

    def audio_ended(self):
        self.signals.last_message_time = time.time()
        self.signals.AI_speaking = False

    def audio_playback_loop(self):
        """BACKGROUND WORKER 2: Continuously plays ready-to-go audio files."""
        while not getattr(self.signals, 'terminate', False):
            try:
                message, audio_bytes = self.audio_queue.get(timeout=0.05)
                if message and audio_bytes and not getattr(self.signals, 'terminate', False):
                    self.play_audio(message, audio_bytes)
                self.audio_queue.task_done()
            except queue.Empty:
                pass
            except Exception as e:
                print(f"\033[91mTTS Playback Loop Error: {e}\033[0m")

    def init_event_loop(self):
        """BACKGROUND WORKER 1: Consumes text and downloads audio
        sequentially to preserve order."""
        if not self.enabled:
            return

        threading.Thread(target=self.audio_playback_loop, daemon=True).start()

        while not getattr(self.signals, 'terminate', False):
            try:
                if hasattr(self.signals, 'tts_queue'):
                    message = self.signals.tts_queue.get(timeout=0.05)
                    if message:
                        audio_bytes = self.fetch_audio(message)
                        if audio_bytes:
                            self.audio_queue.put((message, audio_bytes))

                    self.signals.tts_queue.task_done()
            except queue.Empty:
                pass
            except Exception as e:
                print(f"\033[91mTTS Download Loop Error: {e}\033[0m")