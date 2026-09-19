import os
import sys
import logging
import asyncio
import itertools
import io
import wave
import struct
from dotenv import load_dotenv
import discord
from discord.ext import commands, voice_recv
from faster_whisper import WhisperModel

from modules.module import Module

if sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

load_dotenv()
logger = logging.getLogger(__name__)


class LocalAudioSink(voice_recv.AudioSink):
    """Custom audio sink capturing incoming real-time PCM voice packets, tracking sample-based VAD silence, and transcribing via faster-whisper."""
    def __init__(self, signals, loop):
        super().__init__()
        self.signals = signals
        self.loop = loop
        self.user_buffers = {}
        self.user_silence_counts = {}

        print("\033[90mSTT Engine: Initializing local faster-whisper model...\033[0m")
        self.whisper_model = WhisperModel("base", device="cpu", compute_type="int8")
        print("\033[90mSTT Engine: faster-whisper loaded successfully.\033[0m")

        # VAD Parameters for 16kHz, mono, 16-bit PCM (20ms frames = 320 samples)
        self.sample_rate = 16000
        self.frame_duration_ms = 20
        self.amplitude_threshold = 500
        
        # 800ms silence threshold translated into frame counts
        self.silence_threshold_ms = 800
        self.max_silence_frames = int(self.silence_threshold_ms / self.frame_duration_ms)

    def wants_opus(self) -> bool:
        return False  # Request decoded PCM frames (16kHz, mono, 16-bit)

    def write(self, user, data):
        if not user or not data.pcm:
            return

        user_id = user.id
        pcm_data = data.pcm

        # Safe dictionary initialization
        if user_id not in self.user_buffers:
            self.user_buffers[user_id] = bytearray()
            self.user_silence_counts[user_id] = 0

        # Always extend buffer with incoming frame to preserve natural pauses and word tails
        self.user_buffers[user_id].extend(pcm_data)

        try:
            count = len(pcm_data) // 2
            if count > 0:
                shorts = struct.unpack(f"<{count}h", pcm_data)
                max_amp = max(abs(s) for s in shorts)
            else:
                max_amp = 0
        except Exception:
            max_amp = 0

        if max_amp < self.amplitude_threshold:
            self.user_silence_counts[user_id] += 1
            
            if self.user_silence_counts[user_id] >= self.max_silence_frames:
                if len(self.user_buffers[user_id]) > (self.sample_rate * 2 * 0.5):  # Min 0.5s of speech
                    raw_audio = bytes(self.user_buffers[user_id])
                    self.user_buffers[user_id].clear()
                    self.user_silence_counts[user_id] = 0

                    asyncio.run_coroutine_threadsafe(
                        self._process_audio(user, raw_audio), self.loop
                    )
                else:
                    self.user_buffers[user_id].clear()
                    self.user_silence_counts[user_id] = 0
        else:
            self.user_silence_counts[user_id] = 0

    async def _process_audio(self, user, pcm_bytes):
        try:
            wav_io = io.BytesIO()
            with wave.open(wav_io, 'wb') as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(self.sample_rate)
                wf.writeframes(pcm_bytes)
            wav_io.seek(0)

            segments, _ = await asyncio.get_running_loop().run_in_executor(
                None, lambda: self.whisper_model.transcribe(wav_io, beam_size=5)
            )

            transcribed_text = " ".join([segment.text for segment in segments]).strip()
            if transcribed_text:
                print(f"\n[Voice STT] {user.display_name}: {transcribed_text}")
                self.signals.add_message("user", f"{user.display_name}: {transcribed_text}")
                self.signals.new_message = True
        except Exception as e:
            print(f"\033[91mSTT Processing Error: {e}\033[0m")

    def cleanup(self):
        self.user_buffers.clear()
        self.user_silence_counts.clear()


class DiscordClient(Module):
    def __init__(self, signals, stt=None, enabled=True, audio_mode="local"):
        super().__init__(signals, enabled)
        self.audio_mode = audio_mode
        self.stt = stt
        self.bot = None
        self.connections = {}
        self.voice_channel_id = int(os.getenv("DISCORD_VOICE_CHANNEL_ID", 0))

        self._tts_queue = asyncio.Queue()
        self._tts_file_counter = itertools.count()

    async def run(self):
        # Silence noisy discord gateway/client/http loggers
        logging.getLogger("discord.gateway").setLevel(logging.CRITICAL)
        logging.getLogger("discord.player").setLevel(logging.CRITICAL)
        logging.getLogger("discord.client").setLevel(logging.CRITICAL)
        logging.getLogger("discord.voice_client").setLevel(logging.CRITICAL)
        logging.getLogger("discord.http").setLevel(logging.WARNING)
        
        # ADD THIS LINE TO HIDE OPUS STREAM DECODE NOISE:
        logging.getLogger("discord.ext.voice_recv").setLevel(logging.CRITICAL)
        
        if self.audio_mode not in ("discord", "both"):
            print("\033[90mDISCORD: Skipped (Local PC Only mode selected)\033[0m")
            while not self.signals.terminate:
                await asyncio.sleep(1.0)
            return

        intents = discord.Intents.default()
        intents.message_content = True
        intents.voice_states = True
        intents.guilds = True
        
        self.bot = commands.Bot(command_prefix="!", intents=intents)

        @self.bot.event
        async def on_ready():
            print(f"\033[92mDISCORD: {self.bot.user} is online and ready.\033[0m")
            
            self.bot.loop.create_task(self._tts_playback_loop())
            self.bot.loop.create_task(self._tts_listener_loop())
            self.bot.loop.create_task(self._shutdown_watchdog())

            if self.voice_channel_id:
                try:
                    v_channel = self.bot.get_channel(self.voice_channel_id)
                    if not v_channel:
                        v_channel = await self.bot.fetch_channel(self.voice_channel_id)

                    if v_channel and isinstance(v_channel, discord.VoiceChannel):
                        vc = await v_channel.connect(cls=voice_recv.VoiceRecvClient)
                        self.connections[v_channel.guild.id] = vc
                        vc.listen(LocalAudioSink(self.signals, self.bot.loop))
                        print(f"\033[92mDISCORD: Auto-connected to voice channel '{v_channel.name}' with LocalAudioSink active.\033[0m")
                except Exception as e:
                    print(f"\033[91mDISCORD Auto-Voice Connection Error: {e}\033[0m")

        @self.bot.event
        async def on_message(message):
            if message.author == self.bot.user:
                return
            await self.bot.process_commands(message)

            channel_id = int(os.getenv("DISCORD_CHANNEL_ID", 0))
            if channel_id and message.channel.id == channel_id:
                user_name = message.author.display_name
                content = message.content.strip()
                if content:
                    print(f"\n[Discord Text] {user_name}: {content}")
                    self.signals.last_discord_user_id = message.author.id
                    self.signals.last_discord_message_id = message.id
                    self.signals.add_message("user", f"{user_name}: {content}")
                    self.signals.new_message = True

        @self.bot.command(name="ping")
        async def ping(ctx):
            await ctx.send(f"Pong! {round(self.bot.latency * 1000)}ms")

        @self.bot.command(name="join")
        async def join(ctx):
            if not ctx.author.voice:
                return await ctx.send("You're not in a voice channel right now.")
            v_channel = ctx.author.voice.channel
            vc = await v_channel.connect(cls=voice_recv.VoiceRecvClient)
            self.connections[ctx.guild.id] = vc
            vc.listen(LocalAudioSink(self.signals, self.bot.loop))
            await ctx.send(f"Joined {v_channel.name} and started listening!")

        @self.bot.command(name="leave")
        async def leave(ctx):
            if ctx.guild.id in self.connections:
                vc = self.connections[ctx.guild.id]
                await vc.disconnect()
                del self.connections[ctx.guild.id]
                await ctx.send("Left the voice channel.")
            else:
                await ctx.send("Not connected in this server.")

        token = os.getenv('DISCORD_BOT_TOKEN') or os.getenv('DISCORD_TOKEN')
        try:
            await self.bot.start(token)
        except RuntimeError as e:
            if "Session is closed" not in str(e):
                print(f"\033[91mDISCORD: {e}\033[0m")
        except Exception:
            pass

    async def _shutdown_watchdog(self):
        while not getattr(self.signals, 'terminate', False):
            await asyncio.sleep(0.1)
            
        if self.connections:
            print("\033[93mDISCORD: Terminate signal received. Disconnecting from voice channels...\033[0m")
            for vc in list(self.connections.values()):
                if vc and vc.is_connected():
                    try:
                        await vc.disconnect()
                    except Exception:
                        pass
            self.connections.clear()
            
        if self.bot and not self.bot.is_closed():
            await self.bot.close()

    def send_to_discord(self, text, user_id=None):
        if not self.bot or not text:
            return

        channel_id = int(os.getenv("DISCORD_CHANNEL_ID", 0))
        if not channel_id:
            return

        payload_text = str(text)

        async def _send():
            try:
                channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
                if channel:
                    chunks = []
                    current_text = payload_text
                    while len(current_text) > 1900:
                        split_idx = current_text[:1900].rfind('\n')
                        if split_idx == -1:
                            split_idx = 1900
                        chunks.append(current_text[:split_idx])
                        current_text = current_text[split_idx:].strip()
                    if current_text:
                        chunks.append(current_text)

                    message_id = getattr(self.signals, 'last_discord_message_id', None)
                    
                    for i, chunk in enumerate(chunks):
                        if i == 0 and message_id:
                            try:
                                msg = await channel.fetch_message(message_id)
                                await msg.reply(chunk)
                                continue
                            except Exception:
                                pass
                        
                        message_content = f"<@{user_id}> {chunk}" if (i == 0 and user_id) else chunk
                        await channel.send(message_content)
            except Exception as e:
                print(f"\033[91mDISCORD Send Error: {e}\033[0m")

        if self.bot and self.bot.loop:
            asyncio.run_coroutine_threadsafe(_send(), self.bot.loop)

    def play_audio_in_voice(self, audio_bytes):
        if not self.bot:
            return

        async def _enqueue():
            await self._tts_queue.put(audio_bytes)

        asyncio.run_coroutine_threadsafe(_enqueue(), self.bot.loop)

    async def _tts_listener_loop(self):
        while not self.signals.terminate:
            if getattr(self.signals, 'discord_tts_ready', False):
                audio_bytes = getattr(self.signals, 'discord_tts_bytes', None)
                if audio_bytes:
                    self.play_audio_in_voice(audio_bytes)
                self.signals.discord_tts_ready = False
            await asyncio.sleep(0.1)

    async def _tts_playback_loop(self):
        while not self.signals.terminate:
            audio_bytes = await self._tts_queue.get()

            if not self.connections:
                continue
            
            vc = list(self.connections.values())[0]
            if not vc or not vc.is_connected():
                continue

            idx = next(self._tts_file_counter) % 2
            filename = f"temp_discord_audio_{idx}.wav"

            try:
                with open(filename, "wb") as f:
                    f.write(audio_bytes)
            except Exception as e:
                continue

            finished = asyncio.Event()

            def _after(error, _finished=finished):
                self.bot.loop.call_soon_threadsafe(_finished.set)

            try:
                vc.play(discord.FFmpegPCMAudio(filename), after=_after)
            except Exception as e:
                continue

            await finished.wait()