"""
Discord voice bridge.

Receive: DAVE MLS decryption → Opus decode → RMS VAD → faster-whisper.
Send:    Fish Audio WAV → FFmpeg → discord.VoiceClient.

Discord enforces DAVE end-to-end encryption on all non-Stage voice
channels (2026-03-02 onward). discord.py implements DAVE for the send
path only; discord-ext-voice-recv predates enforcement and has no
receive-side DAVE support. This module bridges that gap.

Critical operational rule: never disconnect+reconnect the voice client
at runtime. Reconnects leave the DAVE session out of sync with the
sink, and the sink goes deaf without error. If the connection drops,
exit and restart. Do not add "reconnect" helpers.
"""

import os
import sys
import logging
import asyncio
import itertools
import struct
from dotenv import load_dotenv
import discord
from discord.ext import commands, voice_recv
from faster_whisper import WhisperModel

from modules.module import Module
from modules.log_hub import emit as hub_emit

if sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

load_dotenv()
logger = logging.getLogger(__name__)

try:
    import numpy as np
    HAVE_NUMPY = True
except ImportError:
    HAVE_NUMPY = False

# ─── DAVE ─── Discord E2EE. discord.py 2.7+ implements DAVE for the send
# path only. discord-ext-voice-recv predates enforcement and has no
# receive path. We bridge that gap here.
try:
    import davey
    HAVE_DAVEY = True
except ImportError:
    davey = None
    HAVE_DAVEY = False


def _dave_preflight():
    if not HAVE_DAVEY:
        raise RuntimeError(
            "The 'davey' module is not installed. Run: "
            "pip install 'discord.py>=2.7' davey"
        )
    for name in ("DaveSession", "MediaType"):
        if not hasattr(davey, name):
            raise RuntimeError(
                f"davey has no '{name}'; expected davey >= 0.1.6"
            )
    if not hasattr(davey.DaveSession, "decrypt"):
        raise RuntimeError("davey.DaveSession has no decrypt()")


def _generate_tone_wav(path, freq_hz=440.0, duration_s=2.0, sample_rate=48000):
    """Write a simple sine tone — used by the !test_play diagnostic."""
    import wave, math
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        frames = []
        total = int(sample_rate * duration_s)
        for i in range(total):
            v = int(32767 * 0.4 * math.sin(2 * math.pi * freq_hz * i / sample_rate))
            frames.append(struct.pack("<h", v))
        w.writeframes(b"".join(frames))


class DaveDecryptor:
    """Decrypts received Opus frames via the connection's DaveSession.

    Lazy — at sink __init__ time the voice client hasn't been attached
    yet, so we resolve the session on first decrypt().
    """
    def __init__(self, voice_client):
        self._vc = voice_client
        self._session = None
        self._session_checked = False
        self._warned_missing = False
        self.decrypted = 0
        self.passthrough = 0
        self.dropped = 0

    def _get_session(self):
        if self._session_checked:
            return self._session
        self._session_checked = True
        conn = getattr(self._vc, "_connection", None)
        if conn is None:
            if not self._warned_missing:
                logger.error(
                    "voice client has no _connection; DAVE decryption "
                    "disabled — received audio will be unusable."
                )
                self._warned_missing = True
            return None
        self._session = getattr(conn, "dave_session", None)
        return self._session

    @property
    def status(self) -> str:
        if not HAVE_DAVEY:
            return "unavailable (davey not installed)"
        session = self._get_session()
        if session is None:
            return "no session (passthrough)"
        try:
            ready = bool(session.ready)
            version = int(session.protocol_version)
            return f"active (v{version})" if ready else "pending"
        except Exception as e:
            return f"unreadable ({e})"

    def decrypt(self, user_id: int, payload: bytes):
        if not payload:
            return None
        session = self._get_session()
        if session is None:
            self.passthrough += 1
            return payload
        try:
            if not session.ready:
                self.passthrough += 1
                return payload
            plaintext = session.decrypt(
                user_id, davey.MediaType.audio, payload
            )
            self.decrypted += 1
            return plaintext
        except Exception as e:
            self.dropped += 1
            if self.dropped <= 3 or self.dropped % 100 == 0:
                logger.debug(
                    "DAVE decrypt failed for user %s (%d so far): %s",
                    user_id, self.dropped, e
                )
            return None


class OpusDecoderPool:
    """One Opus decoder per SSRC — Opus decoders carry inter-frame state,
    so mixing speakers through one decoder corrupts both."""
    def __init__(self):
        self._decoders = {}
        self.decoded = 0
        self.failed = 0

    def decode(self, ssrc: int, payload: bytes) -> bytes:
        decoder = self._decoders.get(ssrc)
        if decoder is None:
            decoder = discord.opus.Decoder()
            self._decoders[ssrc] = decoder
        try:
            pcm = decoder.decode(payload)
            self.decoded += 1
            return pcm
        except discord.opus.OpusError:
            self.failed += 1
            return b""

    def clear(self):
        self._decoders.clear()


class LocalAudioSink(voice_recv.AudioSink):
    """DAVE-aware audio sink with RMS-based VAD.

    Pipeline: transport-decrypted Opus (from extension) → DAVE MLS
    decrypt → Opus decode → downmix → RMS VAD → buffer → Whisper.

    VAD design
    ----------
    RMS with hysteresis (start threshold higher than stop threshold).
    Peak-amplitude VAD is too spiky — a single loud sample trips it, so
    breath sounds and background noise hold the buffer open between
    words. RMS is smoother and matches what the local mic path feels
    like.

    silence_ms=500 mirrors typical conversational VAD (300-500ms range).
    The old 800ms window is why "Hello... Hello." merged into one
    transcript.
    """
    def __init__(self, signals, loop):
        super().__init__()
        self.signals = signals
        self.loop = loop

        self._decryptor = None
        self.decoders = OpusDecoderPool()

        self.user_buffers = {}
        self.user_silence_counts = {}
        self.user_is_speaking = {}
        self.user_ssrc = {}

        self.sample_rate = 48000
        self.frame_duration_ms = 20

        # ─── VAD thresholds ───
        # RMS in normalised float [0, 1]. Speech is roughly 0.03-0.15,
        # quiet room ~0.001-0.003, breath ~0.005-0.010.
        self.rms_start = 0.020        # user has begun speaking
        self.rms_stop  = 0.010        # hysteresis — must drop here to end

        self.min_speech_ms = 250      # discard utterances shorter than this
        self.silence_ms = 500         # end-of-utterance after this much quiet

        self.min_speech_frames = max(1, self.min_speech_ms // self.frame_duration_ms)
        self.max_silence_frames = max(1, self.silence_ms // self.frame_duration_ms)

        self._frames_in = 0
        self._decoded = 0
        self._warned_broken = False

        print("\033[90mSTT Engine: Initializing local faster-whisper model...\033[0m")
        self.whisper_model = WhisperModel("base", device="cpu", compute_type="int8")
        print("\033[90mSTT Engine: faster-whisper loaded successfully.\033[0m")

    def wants_opus(self) -> bool:
        return True

    def _ensure_decryptor(self):
        if self._decryptor is not None:
            return self._decryptor
        vc = self.voice_client
        if vc is None:
            return None
        self._decryptor = DaveDecryptor(vc)
        print(f"\033[90mDAVE: {self._decryptor.status}\033[0m")
        return self._decryptor

    def _pcm_to_mono_float(self, pcm_bytes):
        """Stereo s16le → mono float32 in [-1, 1]."""
        if HAVE_NUMPY:
            arr = np.frombuffer(pcm_bytes, dtype=np.int16)
            if arr.size < 2:
                return None, None
            if arr.size % 2:
                arr = arr[:-1]
            stereo = arr.reshape(-1, 2).astype(np.float32)
            mono_f = stereo.mean(axis=1) / 32768.0
            mono_i16 = (mono_f * 32767.0).astype(np.int16)
            return mono_i16.tobytes(), mono_f
        else:
            count = len(pcm_bytes) // 4
            if count <= 0:
                return None, None
            samples = struct.unpack(f"<{count * 2}h", pcm_bytes[:count * 4])
            mono_i = [(samples[i] + samples[i + 1]) // 2
                      for i in range(0, len(samples), 2)]
            mono_bytes = struct.pack(f"<{len(mono_i)}h", *mono_i)
            mono_f = [s / 32768.0 for s in mono_i]
            return mono_bytes, mono_f

    def _rms(self, mono_f):
        if mono_f is None or len(mono_f) == 0:
            return 0.0
        if HAVE_NUMPY:
            return float(np.sqrt(np.mean(mono_f ** 2)))
        s = sum(x * x for x in mono_f) / len(mono_f)
        return s ** 0.5

    def write(self, user, data):
        if not user or not getattr(data, "opus", None):
            return

        user_id = user.id
        opus_payload = data.opus

        # ─── DAVE MLS decrypt ───
        decryptor = self._ensure_decryptor()
        if decryptor is not None:
            plaintext = decryptor.decrypt(user_id, opus_payload)
            if plaintext is None:
                return
            opus_payload = plaintext

        packet = getattr(data, "packet", None)
        ssrc = getattr(packet, "ssrc", user_id) if packet is not None else user_id
        self.user_ssrc[user_id] = ssrc

        pcm_bytes = self.decoders.decode(ssrc, opus_payload)
        if not pcm_bytes:
            self._frames_in += 1
            self._maybe_warn_broken()
            return
        self._frames_in += 1
        self._decoded += 1

        mono_bytes, mono_f = self._pcm_to_mono_float(pcm_bytes)
        if mono_bytes is None:
            return

        rms = self._rms(mono_f)

        if user_id not in self.user_buffers:
            self.user_buffers[user_id] = bytearray()
            self.user_silence_counts[user_id] = 0
            self.user_is_speaking[user_id] = False

        speaking = self.user_is_speaking[user_id]

        # ─── VAD state machine with hysteresis ───
        if not speaking and rms >= self.rms_start:
            # Start of utterance.
            speaking = True
            self.user_is_speaking[user_id] = True
            self.user_silence_counts[user_id] = 0
            # Barge-in: stop Lumi mid-sentence.
            vc = self.voice_client
            if vc is not None and vc.is_playing():
                self.loop.call_soon_threadsafe(vc.stop)
        elif speaking and rms < self.rms_stop:
            # Candidate silence — increment counter, keep capturing the tail.
            self.user_silence_counts[user_id] += 1
        elif speaking and rms >= self.rms_stop:
            # Still in the hysteresis band; treat as ongoing speech.
            self.user_silence_counts[user_id] = 0

        if not speaking:
            return

        # Append the current frame (both speech and trailing silence).
        self.user_buffers[user_id].extend(mono_bytes)

        # End of utterance?
        if self.user_silence_counts[user_id] >= self.max_silence_frames:
            self.user_is_speaking[user_id] = False
            buffer_len = len(self.user_buffers[user_id])
            # Require at least min_speech_ms of audio.
            min_bytes = int(self.sample_rate * 2 * (self.min_speech_ms / 1000.0))
            if buffer_len >= min_bytes:
                raw_audio = bytes(self.user_buffers[user_id])
                asyncio.run_coroutine_threadsafe(
                    self._process_audio(user, raw_audio), self.loop
                )
            self.user_buffers[user_id].clear()
            self.user_silence_counts[user_id] = 0

    def _maybe_warn_broken(self):
        if self._warned_broken:
            return
        if self._frames_in > 250 and self._decoded == 0:
            self._warned_broken = True
            d = self._decryptor
            hub_emit(
                self.signals, "mic",
                f"frames arriving but nothing decodes. "
                f"DAVE status: {d.status if d else 'n/a'}, "
                f"decrypt drops: {d.dropped if d else 0}.",
                level="warn",
            )

    async def _process_audio(self, user, pcm_bytes):
        try:
            count = len(pcm_bytes) // 2
            if count == 0:
                return

            if HAVE_NUMPY:
                arr = np.frombuffer(pcm_bytes, dtype=np.int16)
                downsampled = arr[::3].astype(np.float32) / 32768.0
            else:
                samples = struct.unpack(f"<{count}h", pcm_bytes)
                downsampled = samples[::3]
                downsampled = [s / 32768.0 for s in downsampled]

            segments, _ = await asyncio.get_running_loop().run_in_executor(
                None, lambda: self.whisper_model.transcribe(
                    downsampled, beam_size=5
                )
            )
            transcribed_text = " ".join(
                segment.text for segment in segments
            ).strip()
            if transcribed_text:
                self.signals.last_voice_speaker = user.display_name
                hub_emit(
                    self.signals, "mic",
                    f"[STT] {user.display_name}: {transcribed_text}"
                )
                self.signals.add_message(
                    "user",
                    f"{user.display_name}: {transcribed_text}",
                    speaker=user.display_name,
                    source="discord_voice",
                )
                self.signals.new_message = True
        except Exception as e:
            hub_emit(
                self.signals, "mic",
                f"STT processing error: {e}", level="error"
            )

    def stats(self):
        return {
            "frames_in": self._frames_in,
            "decoded": self._decoded,
            "decrypt_ok": self._decryptor.decrypted if self._decryptor else 0,
            "decrypt_dropped": self._decryptor.dropped if self._decryptor else 0,
            "passthrough": self._decryptor.passthrough if self._decryptor else 0,
            "opus_failed": self.decoders.failed,
            "speaking_now": sum(
                1 for v in self.user_is_speaking.values() if v
            ),
            "active_users": list(self.user_buffers.keys()),
        }

    def cleanup(self):
        for attr in ("user_buffers", "user_silence_counts",
                     "user_is_speaking", "user_ssrc"):
            d = getattr(self, attr, None)
            if d is not None:
                d.clear()
        decoders = getattr(self, "decoders", None)
        if decoders is not None:
            decoders.clear()


class DiscordClient(Module):
    def __init__(self, signals, stt=None, enabled=True, audio_mode="local"):
        super().__init__(signals, enabled)
        self.audio_mode = audio_mode
        self.stt = stt
        self.bot = None
        self.connections = {}
        self.voice_channel_id = int(os.getenv("DISCORD_VOICE_CHANNEL_ID", 0))

        self._sinks = {}
        
        self._roster = {}  # guild_id → list of {id, display_name, is_bot}

        self._tts_queue = asyncio.Queue()
        self._tts_file_counter = itertools.count()

    async def _clear_server_voice_flags(self, vc, tag=""):
        """Clear server-mute and server-deafen that would silently drop
        outgoing audio."""
        try:
            guild = vc.guild
            me = guild.me
            if me is None:
                try:
                    me = await guild.fetch_member(self.bot.user.id)
                except Exception as e:
                    hub_emit(self.signals, "discord",
                             f"couldn't fetch own member ({tag}): {e}",
                             level="warn")
                    return
            state = me.voice
            if state is None:
                return
            if state.mute or state.deaf:
                hub_emit(
                    self.signals, "discord",
                    f"bot was server-"
                    f"{'muted' if state.mute else 'deafened'} ({tag}); "
                    f"clearing...", level="warn"
                )
                try:
                    await me.edit(mute=False, deafen=False)
                    hub_emit(self.signals, "discord",
                             "cleared server mute/deafen")
                except Exception as e:
                    hub_emit(self.signals, "discord",
                             f"failed to clear mute/deafen: {e}",
                             level="error")
        except Exception as e:
            hub_emit(self.signals, "discord",
                     f"voice flag check failed ({tag}): {e}", level="warn")

    def _dump_voice_state(self, vc, tag=""):
        """Return a multiline string of the bot's current voice state."""
        try:
            guild = vc.guild
            me = guild.me
            vstate = me.voice if me else None
            ssrc = getattr(vc, "ssrc", None)
            conn = getattr(vc, "_connection", None)
            lines = [
                f"─── voice state ({tag}) ───",
                f"  channel:     {vc.channel}",
                f"  connected:   {vc.is_connected()}",
                f"  playing:     {vc.is_playing()}",
                f"  paused:      {vc.is_paused()}",
                f"  ssrc:        {ssrc}",
            ]
            if vstate is not None:
                lines += [
                    f"  self_mute:   {vstate.self_mute}",
                    f"  self_deaf:   {vstate.self_deaf}",
                    f"  server_mute: {vstate.mute}",
                    f"  server_deaf: {vstate.deaf}",
                    f"  suppress:    {vstate.suppress}",
                ]
            dave_status = "n/a"
            if conn is not None:
                sess = getattr(conn, "dave_session", None)
                if sess is not None:
                    try:
                        dave_status = (
                            f"ready={sess.ready} v{sess.protocol_version}"
                        )
                    except Exception as e:
                        dave_status = f"unreadable ({e})"
            lines.append(f"  dave:        {dave_status}")
            return "\n".join(lines)
        except Exception as e:
            return f"[voice_debug] dump failed: {e}"

    def _refresh_roster(self, vc):
        """Rebuild the roster for the given voice client and push it to
        signals.voice_context. Called on connect and on every
        voice_state_update event."""
        try:
            members = []
            for m in vc.channel.members:
                members.append({
                    "id": str(m.id),
                    "display_name": m.display_name,
                    "is_bot": bool(m.bot),
                })
            members.sort(key=lambda x: (x["is_bot"], x["display_name"].lower()))
            self._roster[vc.guild.id] = members
            self._push_voice_context(vc)
        except Exception as e:
            hub_emit(self.signals, "discord",
                     f"roster refresh failed: {e}", level="warn")

    def _push_voice_context(self, vc):
        """Write the current voice context to signals. Always assigns a
        fresh dict — never mutates in place — so the prompter thread
        reads a consistent snapshot."""
        try:
            members = self._roster.get(vc.guild.id, [])
            humans = [m for m in members if not m["is_bot"]]
            self.signals.voice_context = {
                "active": True,
                "guild_name": vc.guild.name,
                "guild_id": str(vc.guild.id),
                "channel_name": vc.channel.name,
                "roster": members,
                "human_count": len(humans),
                "mode": "group" if len(humans) > 1 else "solo",
            }
            names = ", ".join(m["display_name"] for m in members) or "(nobody)"
            hub_emit(self.signals, "discord",
                     f"voice roster: {len(humans)} human(s) — {names}")
        except Exception as e:
            hub_emit(self.signals, "discord",
                     f"voice context push failed: {e}", level="warn")

    def _clear_voice_context(self):
        self.signals.voice_context = None
        self.signals.last_voice_speaker = None

    async def run(self):
        logging.getLogger("discord.gateway").setLevel(logging.CRITICAL)
        logging.getLogger("discord.player").setLevel(logging.CRITICAL)
        logging.getLogger("discord.client").setLevel(logging.CRITICAL)
        logging.getLogger("discord.voice_client").setLevel(logging.CRITICAL)
        logging.getLogger("discord.http").setLevel(logging.WARNING)
        logging.getLogger("discord.ext.voice_recv").setLevel(logging.CRITICAL)

        if self.audio_mode not in ("discord", "both"):
            print("\033[90mDISCORD: Skipped (Local PC Only mode selected)\033[0m")
            while not self.signals.terminate:
                await asyncio.sleep(1.0)
            return

        try:
            _dave_preflight()
            hub_emit(self.signals, "discord",
                     "preflight OK — receive path is wired")
        except RuntimeError as e:
            hub_emit(self.signals, "discord",
                     f"PREFLIGHT FAILED: {e}", level="error")
            print(f"\033[91mDAVE PREFLIGHT FAILED: {e}\033[0m")

        intents = discord.Intents.default()
        intents.message_content = True
        intents.voice_states = True
        intents.guilds = True

        self.bot = commands.Bot(command_prefix="!", intents=intents)

        @self.bot.event
        async def on_ready():
            print(f"\033[92mDISCORD: {self.bot.user} is online and ready.\033[0m")
            hub_emit(self.signals, "discord",
                     f"{self.bot.user} is online and ready")

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
                        sink = LocalAudioSink(self.signals, self.bot.loop)
                        vc.listen(sink)
                        self._sinks[v_channel.guild.id] = sink
                        hub_emit(self.signals, "discord",
                                 f"auto-connected to '{v_channel.name}'")
                        await asyncio.sleep(0.5)
                        await self._clear_server_voice_flags(vc, tag="auto-join")
                        self._refresh_roster(vc)
                except Exception as e:
                    hub_emit(self.signals, "discord",
                             f"auto-voice connection error: {e}",
                             level="error")

        @self.bot.event
        async def on_message(message):
            if message.author == self.bot.user:
                return

            if message.content.startswith(self.bot.command_prefix):
                await self.bot.process_commands(message)
                return

            channel_id = int(os.getenv("DISCORD_CHANNEL_ID", 0))
            if channel_id and message.channel.id == channel_id:
                user_name = message.author.display_name
                content = message.content.strip()
                if content:
                    hub_emit(self.signals, "discord",
                             f"[text] {user_name}: {content}")
                    self.signals.last_discord_user_id = message.author.id
                    self.signals.last_discord_message_id = message.id
                    self.signals.add_message("user", f"{user_name}: {content}")
                    self.signals.new_message = True
                    
        @self.bot.event
        async def on_voice_state_update(member, before, after):
            # Refresh rosters when anyone joins/leaves/moves in a guild
            # we're actively connected to.
            for vc in list(self.connections.values()):
                if not vc.is_connected():
                    continue
                if member.guild.id != vc.guild.id:
                    continue
                self._refresh_roster(vc)

        # ─── Voice commands ────────────────────────────────────────

        @self.bot.command(name="join")
        async def join(ctx):
            if not ctx.author.voice:
                return await ctx.send("You're not in a voice channel right now.")
            if ctx.guild.id in self.connections:
                return await ctx.send("Already connected. Use !leave first.")
            v_channel = ctx.author.voice.channel
            vc = await v_channel.connect(cls=voice_recv.VoiceRecvClient)
            self.connections[ctx.guild.id] = vc
            sink = LocalAudioSink(self.signals, self.bot.loop)
            vc.listen(sink)
            self._sinks[ctx.guild.id] = sink
            await self._clear_server_voice_flags(vc, tag="!join")
            self._refresh_roster(vc)
            await ctx.send(f"Joined {v_channel.name} and started listening!")

        @self.bot.command(name="leave")
        async def leave(ctx):
            if ctx.guild.id in self.connections:
                vc = self.connections[ctx.guild.id]
                await vc.disconnect()
                del self.connections[ctx.guild.id]
                self._sinks.pop(ctx.guild.id, None)
                self._clear_voice_context()
                await ctx.send("Left the voice channel.")
            else:
                await ctx.send("Not connected in this server.")

        # ─── Diagnostics ───────────────────────────────────────────

        @self.bot.command(name="ping")
        async def ping(ctx):
            await ctx.send(f"Pong! {round(self.bot.latency * 1000)}ms")

        @self.bot.command(name="voice_debug")
        async def voice_debug(ctx):
            if ctx.guild.id not in self.connections:
                return await ctx.send("Not connected to a voice channel.")
            vc = self.connections[ctx.guild.id]
            state_dump = self._dump_voice_state(vc, tag="!voice_debug")

            me = ctx.guild.me
            st = me.voice
            lines = [
                f"ssrc={getattr(vc, 'ssrc', None)} playing={vc.is_playing()}",
                f"self_mute={st.self_mute} self_deaf={st.self_deaf}",
                f"server_mute={st.mute} server_deaf={st.deaf} suppress={st.suppress}",
            ]
            sink = self._sinks.get(ctx.guild.id)
            if sink is not None:
                s = sink.stats()
                lines.append(
                    f"frames_in={s['frames_in']} decoded={s['decoded']} "
                    f"dave_ok={s['decrypt_ok']} dave_dropped={s['decrypt_dropped']} "
                    f"passthrough={s['passthrough']} opus_failed={s['opus_failed']}"
                )
                lines.append(
                    f"speaking_now={s['speaking_now']} "
                    f"users={len(s['active_users'])}"
                )
            await ctx.send("```" + "\n".join(lines) + "```")

        @self.bot.command(name="voice_stats")
        async def voice_stats(ctx):
            sink = self._sinks.get(ctx.guild.id)
            if sink is None:
                return await ctx.send("No sink attached.")
            s = sink.stats()
            text = (
                f"frames_in={s['frames_in']}  decoded={s['decoded']}  "
                f"dave_ok={s['decrypt_ok']}  dave_drop={s['decrypt_dropped']}  "
                f"opus_fail={s['opus_failed']}  speaking={s['speaking_now']}"
            )
            await ctx.send(f"```{text}```")

        @self.bot.command(name="test_play")
        async def test_play(ctx):
            if ctx.guild.id not in self.connections:
                return await ctx.send("Use !join first.")
            vc = self.connections[ctx.guild.id]
            if not vc.is_connected():
                return await ctx.send("Not connected.")

            path = "test_tone.wav"
            _generate_tone_wav(path, freq_hz=440.0, duration_s=2.0)

            if vc.is_playing():
                vc.stop()

            finished = asyncio.Event()

            def _after(error):
                if error:
                    hub_emit(self.signals, "discord",
                             f"test_play error: {error}", level="error")
                self.bot.loop.call_soon_threadsafe(finished.set)

            vc.play(discord.FFmpegPCMAudio(path), after=_after)
            await ctx.send("Playing test tone. Hear it?")
            await finished.wait()

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
            hub_emit(self.signals, "discord",
                     "terminate signal — disconnecting from voice")
            for vc in list(self.connections.values()):
                if vc and vc.is_connected():
                    try:
                        await vc.disconnect()
                    except Exception:
                        pass
            self.connections.clear()
            self._sinks.clear()
            self._clear_voice_context()

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
                hub_emit(self.signals, "discord",
                         f"send error: {e}", level="error")

        if self.bot and self.bot.loop:
            asyncio.run_coroutine_threadsafe(_send(), self.bot.loop)

    def play_audio_in_voice(self, audio_bytes):
        if not self.bot:
            return

        async def _enqueue():
            await self._tts_queue.put(audio_bytes)

        try:
            asyncio.run_coroutine_threadsafe(_enqueue(), self.bot.loop)
        except Exception as e:
            hub_emit(self.signals, "discord",
                     f"enqueue failed: {type(e).__name__}: {e}",
                     level="error")

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
            try:
                audio_bytes = await self._tts_queue.get()
            except Exception:
                continue

            if not self.connections:
                continue

            vc = list(self.connections.values())[0]
            if not vc or not vc.is_connected():
                continue

            if vc.is_playing():
                vc.stop()

            idx = next(self._tts_file_counter) % 2
            filename = f"temp_discord_audio_{idx}.wav"

            try:
                with open(filename, "wb") as f:
                    f.write(audio_bytes)
            except Exception as e:
                hub_emit(self.signals, "discord",
                         f"file write failed: {e}", level="error")
                continue

            finished = asyncio.Event()

            def _after(error, _finished=finished):
                if error:
                    hub_emit(self.signals, "discord",
                             f"playback error: {error}", level="error")
                self.bot.loop.call_soon_threadsafe(_finished.set)

            try:
                vc.play(discord.FFmpegPCMAudio(filename), after=_after)
            except Exception as e:
                hub_emit(self.signals, "discord",
                         f"vc.play() raised: {type(e).__name__}: {e}",
                         level="error")
                continue

            await finished.wait()