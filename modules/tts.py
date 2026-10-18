import os
import re
import time
import queue
import io
import wave
import requests
import threading
import array  # fallback RMS computation when numpy is absent
from dotenv import load_dotenv

from modules.subtitles import SubtitleWriter

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

# Numpy speeds up envelope computation by ~10x. Not required.
try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False


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


def _wav_duration(audio_bytes, text):
    """Seconds of audio, for pacing subtitles. Falls back to a
    speaking-rate estimate if the bytes aren't a readable WAV."""
    try:
        with wave.open(io.BytesIO(audio_bytes), 'rb') as wf:
            return wf.getnframes() / float(wf.getframerate())
    except Exception:
        return max(1.0, len(text or "") / 15.0)


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

        obs = next((m for m in self.modules
                    if m.__class__.__name__ == "OBSControl"), None)
        self.subtitles = SubtitleWriter(obs=obs)

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

    # ---------------------------------------------------------------
    # VTS integration helpers
    # ---------------------------------------------------------------
    def _find_vts(self):
        """Locate the VtubeStudio module if it's loaded and enabled.
        Called once per playback turn — the result is cached for the
        duration of the utterance so we don't scan the module list on
        every audio chunk."""
        for m in self.modules:
            if m.__class__.__name__ == "VtubeStudio":
                if getattr(m, "enabled", False) and hasattr(m, "API"):
                    return m
        return None

    def _audio_to_mouth_envelope(self, audio_bytes, samples_per_chunk=1024):
        """Decode WAV bytes and return a list of normalized volume
        values, one per playback chunk. Used to drive VTube Studio's
        MouthOpen parameter in sync with playback.

        The returned list is aligned 1:1 with the device_worker chunk
        loop — envelope[i] corresponds to the i-th chunk written.

        Returns a list of floats in [0.0, 1.0]. If decoding fails or
        the format isn't 16-bit PCM, returns an empty list and the
        caller skips lip-sync for this utterance (audio still plays).
        """
        try:
            wf = wave.open(io.BytesIO(audio_bytes), 'rb')
            n_channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            n_frames = wf.getnframes()
            raw = wf.readframes(n_frames)
            wf.close()

            if sampwidth != 2:
                # Only handle 16-bit PCM. Fish Audio returns 16-bit,
                # so this is a defensive branch that shouldn't fire.
                return []

            # Convert to mono float samples
            if HAS_NUMPY:
                samples = np.frombuffer(raw, dtype=np.int16)
                if n_channels == 2:
                    samples = samples.reshape(-1, 2).mean(axis=1)
                samples = samples.astype(np.float32)
            else:
                samples = array.array('h')
                samples.frombytes(raw)
                if n_channels == 2:
                    # Manual stereo → mono downmix
                    samples = array.array(
                        'f',
                        [(samples[i] + samples[i + 1]) * 0.5
                         for i in range(0, len(samples) - 1, 2)]
                    )
                else:
                    samples = array.array('f', [float(s) for s in samples])

            # Compute per-chunk RMS
            rms_values = []
            step = samples_per_chunk
            for i in range(0, len(samples), step):
                chunk = samples[i:i + step]
                if len(chunk) == 0:
                    continue
                if HAS_NUMPY:
                    rms = float(np.sqrt(np.mean(chunk ** 2)))
                else:
                    rms = (sum(float(s) * float(s) for s in chunk) / len(chunk)) ** 0.5
                rms_values.append(rms)

            if not rms_values:
                return []

            # Normalize to [0, 1]. Apply a mild gamma so quiet syllables
            # still move the mouth — without it, soft consonants get
            # flattened and the avatar looks stiff.
            peak = max(rms_values) or 1.0
            envelope = []
            for rms in rms_values:
                norm = rms / peak
                envelope.append(min(1.0, norm ** 0.6))

            return envelope
        except Exception as e:
            print(f"\033[90mTTS: envelope decode failed — {e}\033[0m")
            return []

    # ---------------------------------------------------------------
    # Playback
    # ---------------------------------------------------------------
    def play_audio(self, message, audio_bytes):
        """Physically plays the bytes and routes to Discord."""
        self.signals.sio_queue.put(("current_message", message))
        self.subtitles.show(message, _wav_duration(audio_bytes, message))

        try:
            broadcast = getattr(self.signals, "broadcast_tts_audio", None)
            if broadcast is not None:
                broadcast(audio_bytes)
        except Exception as e:
            print(f"\033[90mTTS: browser broadcast failed — {e}\033[0m")
        
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
                    wf.close()

                    bytes_per_chunk = 1024 * channels_val * wf.getsampwidth()

                    vts = self._find_vts()
                    envelope = self._audio_to_mouth_envelope(audio_bytes) if vts else []
                    mouth_driver_idx = device_indices[0] if device_indices else None

                    threads = []

                    def device_worker(dev_idx):
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

                            is_mouth_driver = (
                                vts is not None
                                and envelope
                                and dev_idx == mouth_driver_idx
                            )
                            mouth_tick = 0

                            for i in range(0, len(all_data), bytes_per_chunk):
                                if not self.is_playing:
                                    break
                                st.write(all_data[i:i + bytes_per_chunk])

                                if is_mouth_driver and mouth_tick < len(envelope):
                                    try:
                                        vts.API.set_mouth_open(envelope[mouth_tick])
                                    except Exception:
                                        pass
                                    mouth_tick += 1

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
        self.subtitles.clear()

        # Close the mouth so a barge-in doesn't leave the avatar
        # frozen mid-syllable.
        vts = self._find_vts()
        if vts:
            try:
                vts.API.set_mouth_open(0.0)
            except Exception:
                pass

    def audio_started(self):
        self.signals.AI_speaking = True

    def audio_ended(self):
        self.signals.last_message_time = time.time()
        self.signals.AI_speaking = False

        # Smoothly close the mouth. The VTS loop will decay the target
        # to zero over the next few frames.
        vts = self._find_vts()
        if vts:
            try:
                vts.API.set_mouth_open(0.0)
            except Exception:
                pass

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