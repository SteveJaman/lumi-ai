import io
import numpy as np
from discord.sinks.core import Filters, Sink, default_filters, AudioData

class StreamingSink(Sink):
    """A custom sink that converts incoming voice packets into 16kHz mono PCM for STT."""

    def __init__(self, signals, stt, filters=None):
        if filters is None:
            filters = default_filters
        self.filters = filters
        Filters.__init__(self, **self.filters)

        self.encoding = "pcm"
        self.vc = None
        self.audio_data = {}

        self.signals = signals
        self.stt = stt

    @Filters.container
    def write(self, data, user):
        if not data:
            return

        print("Receiving voice packet...")
        if user not in self.audio_data:
            file = io.BytesIO()
            self.audio_data.update({user: AudioData(file)})

        file = self.audio_data[user]
        file.write(data)

        try:
            # Convert raw 48kHz stereo PCM bytes from Discord to a numpy array (16-bit signed integer)
            arr = np.frombuffer(data, dtype=np.int16)
            if arr.size > 0:
                # Handle stereo to mono downmixing by averaging channels
                usable_len = (arr.size // 2) * 2
                arr = arr[:usable_len]
                mono = arr.reshape(-1, 2).astype(np.float32).mean(axis=1)

                # Simple resampling from 48kHz down to 16kHz (taking every 3rd sample)
                resampled = mono[::3].astype(np.int16)

                # Feed the processed 16kHz mono PCM raw bytes directly into your STT engine
                if getattr(self.signals, 'stt_ready', True) and self.stt:
                    if hasattr(self.stt, 'feed_audio'):
                        self.stt.feed_audio(resampled.tobytes())
                        print("FEEDING AUDIO TO STT")
        except Exception as e:
            print(f"Audio processing error in StreamingSink: {e}")

    def format_audio(self, audio):
        return