import time
from aiohttp import web
import socketio
from constants import PATIENCE

class SocketIOServer:
    def __init__(self, signals, stt=None, tts=None, llmWrapper=None, prompter=None, modules=None):
        self.signals = signals
        self.stt = stt
        self.tts = tts
        self.llmWrapper = llmWrapper
        self.prompter = prompter
        self.modules = modules if modules is not None else {}

    def run(self):
        """Entry point for running in a separate thread."""
        self.start_server()

    def start_server(self):
        print("Starting Socket.io server...")
        sio = socketio.AsyncServer(async_mode='aiohttp', cors_allowed_origins='*')
        app = web.Application()
        sio.attach(app)

        # --- LLM Controls ---
        @sio.event
        async def get_blacklist(sid):
            if self.llmWrapper:
                await sio.emit('get_blacklist', self.llmWrapper.API.get_blacklist())

        @sio.event
        async def set_blacklist(sid, message):
            if self.llmWrapper:
                self.llmWrapper.API.set_blacklist(message)

        @sio.event
        async def disable_LLM(sid):
            if self.llmWrapper:
                self.llmWrapper.API.set_LLM_status(False)

        @sio.event
        async def enable_LLM(sid):
            if self.llmWrapper:
                self.llmWrapper.API.set_LLM_status(True)

        @sio.event
        async def cancel_next_message(sid):
            if self.llmWrapper:
                self.llmWrapper.API.cancel_next()

        # --- Audio & Speech Controls ---
        @sio.event
        async def disable_TTS(sid):
            if self.tts:
                self.tts.API.set_TTS_status(False)

        @sio.event
        async def enable_TTS(sid):
            if self.tts:
                self.tts.API.set_TTS_status(True)

        @sio.event
        async def abort_current_message(sid):
            if self.tts:
                self.tts.API.abort_current()

        @sio.event
        async def disable_STT(sid):
            if self.stt:
                self.stt.API.set_STT_status(False)

        @sio.event
        async def enable_STT(sid):
            if self.stt:
                self.stt.API.set_STT_status(True)

        # --- Avatar / VTube Studio Controls ---
        @sio.event
        async def disable_movement(sid):
            if "vtube_studio" in self.modules:
                self.modules["vtube_studio"].API.set_movement_status(False)

        @sio.event
        async def enable_movement(sid):
            if "vtube_studio" in self.modules:
                self.modules["vtube_studio"].API.set_movement_status(True)

        @sio.event
        async def get_hotkeys(sid):
            if "vtube_studio" in self.modules:
                self.modules["vtube_studio"].API.get_hotkeys()

        @sio.event
        async def send_hotkey(sid, hotkey):
            if "vtube_studio" in self.modules:
                self.modules["vtube_studio"].API.send_hotkey(hotkey)

        @sio.event
        async def trigger_prop(sid, prop_action):
            if "vtube_studio" in self.modules:
                self.modules["vtube_studio"].API.trigger_prop(prop_action)

        @sio.event
        async def move_model(sid, mode):
            if "vtube_studio" in self.modules:
                self.modules["vtube_studio"].API.move_model(mode)

        # --- Multimodal Controls ---
        @sio.event
        async def disable_multimodal(sid):
            if "multimodal" in self.modules:
                self.modules["multimodal"].API.set_multimodal_status(False)

        @sio.event
        async def enable_multimodal(sid):
            if "multimodal" in self.modules:
                self.modules["multimodal"].API.set_multimodal_status(True)

        # --- Conversation & Prompt Controls ---
        @sio.event
        async def fun_fact(sid):
            current_hist = self.signals.history
            current_hist.append({"role": "user", "content": "Let's move on. Can you share a quick interesting fact?"})
            self.signals.history = current_hist
            self.signals.new_message = True

        @sio.event
        async def new_topic(sid, message):
            current_hist = self.signals.history
            current_hist.append({"role": "user", "content": message})
            self.signals.history = current_hist
            self.signals.new_message = True

        @sio.event
        async def nuke_history(sid):
            self.signals.history = []

        # --- Audio Player Controls ---
        @sio.event
        async def play_audio(sid, file_name):
            if "audio_player" in self.modules:
                self.modules["audio_player"].API.play_audio(file_name)

        @sio.event
        async def pause_audio(sid):
            if "audio_player" in self.modules:
                self.modules["audio_player"].API.pause_audio()

        @sio.event
        async def resume_audio(sid):
            if "audio_player" in self.modules:
                self.modules["audio_player"].API.resume_audio()

        @sio.event
        async def abort_audio(sid):
            if "audio_player" in self.modules:
                self.modules["audio_player"].API.stop_playing()

        # --- Long-term Memory Controls ---
        @sio.event
        async def clear_short_term(sid):
            if "memory" in self.modules:
                self.modules["memory"].API.clear_short_term()
                await sio.emit("get_memories", self.modules["memory"].API.get_memories())

        @sio.event
        async def import_json(sid):
            if "memory" in self.modules:
                self.modules["memory"].API.import_json()

        @sio.event
        async def export_json(sid):
            if "memory" in self.modules:
                self.modules["memory"].API.export_json()

        @sio.event
        async def delete_memory(sid, data):
            if "memory" in self.modules:
                self.modules["memory"].API.delete_memory(data)
                await sio.emit("get_memories", self.modules["memory"].API.get_memories())

        @sio.event
        async def get_memories(sid, data=""):
            if "memory" in self.modules:
                await sio.emit("get_memories", self.modules["memory"].API.get_memories(data))

        @sio.event
        async def create_memory(sid, data):
            if "memory" in self.modules:
                self.modules["memory"].API.create_memory(data)
                await sio.emit("get_memories", self.modules["memory"].API.get_memories())

        # --- Connection State Handlers ---
        @sio.event
        async def connect(sid, environ):
            # Sync history and timer state
            await sio.emit("history", self.signals.history)
            await sio.emit("patience_update", {
                "crr_time": time.time() - self.signals.last_message_time,
                "total_time": PATIENCE
            })

            if self.llmWrapper:
                await sio.emit('get_blacklist', self.llmWrapper.API.get_blacklist())
                await sio.emit('LLM_status', self.llmWrapper.API.get_LLM_status())
            if self.tts:
                await sio.emit('TTS_status', self.tts.API.get_TTS_status())
            if self.stt:
                await sio.emit('STT_status', self.stt.API.get_STT_status())

            if "audio_player" in self.modules:
                await sio.emit('audio_list', self.modules["audio_player"].API.get_audio_list())
            if "vtube_studio" in self.modules:
                await sio.emit('movement_status', self.modules["vtube_studio"].API.get_movement_status())
            if "multimodal" in self.modules:
                await sio.emit('multimodal_status', self.modules["multimodal"].API.get_multimodal_status())

        @sio.event
        def disconnect(sid):
            print(f'Socket.IO Client disconnected: {sid}')

        async def send_messages():
            # Plain return on terminate instead of raising GracefulExit.
            # aiohttp's GracefulExit inherits from SystemExit, not Exception,
            # so the old "except Exception" guard couldn't catch it — it
            # propagated up and asyncio logged it as an unretrieved task
            # exception on every shutdown. Returning is equivalent and clean.
            try:
                while not self.signals.terminate:
                    while not self.signals.sio_queue.empty():
                        event, data = self.signals.sio_queue.get()
                        await sio.emit(event, data)
                    await sio.sleep(0.1)
            except Exception as e:
                print(f"\033[91msocketIO send_messages error: {e}\033[0m")

        async def init_app():
            sio.start_background_task(send_messages)
            return app

        web.run_app(init_app(), port=5000, print=None)