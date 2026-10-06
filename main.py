import asyncio
import os
import threading
import multiprocessing
import time
import signal

os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"

from signals import Signals
from constants import WORKSPACE_ROOT

from modules.memory import Memory
from modules.library_indexer import LibraryIndexer
from modules.multimodal import MultiModal
from modules.local_input import LocalInput
from modules.console_input import ConsoleInput
from modules.discordClient import DiscordClient
from modules.vtubeStudio import VtubeStudio
from modules.mic_input import MicInput
from modules.browser_controller import BrowserController
from modules.prompter import Prompter
from modules.socketIO import SocketIOServer
from modules.heartbeat import Heartbeat
from modules.emotional_state import EmotionalStateModule
from modules.ambient_perception import AmbientPerception
from modules.log_hub import LogHub
from modules.self_model import SelfModel

from sdk.server import SDKServer

try:
    from modules.tts import TTS
except ImportError:
    from tts import TTS


_global_signals = None
_global_prompter = None

_shutdown_lock = threading.Lock()
_shutdown_started = False


def handle_exit(sig, frame):
    global _shutdown_started

    if multiprocessing.current_process().name != "MainProcess":
        return

    with _shutdown_lock:
        if _shutdown_started:
            return
        _shutdown_started = True

    print("\n\033[93m[System] Interrupt received, shutting down cleanly...\033[0m")

    if _global_prompter:
        try:
            if not getattr(_global_prompter, "_session_saved_this_run", False):
                _global_prompter.save_session_state()
        except Exception as e:
            print(f"\033[91mSESSION: Shutdown save failed: {e}\033[0m")

    if _global_signals:
        _global_signals.terminate = True

    time.sleep(3.0)
    os._exit(0)


signal.signal(signal.SIGINT, handle_exit)


def choose_audio_mode():
    print("\n=======================================================")
    print("  Lumi Audio Routing Setup")
    print("=======================================================")
    print("1. Local PC Only")
    print("2. Discord Voice Channel Only")
    print("3. Both (PC + Discord)")

    while True:
        choice = input("\nSelect audio routing (1/2/3): ").strip()
        if choice == "1":
            return "local"
        elif choice == "2":
            return "discord"
        elif choice == "3":
            return "both"
        print("Invalid choice. Please enter 1, 2, or 3.")


def _build_module_map(modules_list):
    """SocketIOServer expects a dict keyed by short names."""
    mapping = {}
    for m in modules_list:
        cls = m.__class__.__name__
        if cls == "VtubeStudio":
            mapping["vtube_studio"] = m
        elif cls == "AudioPlayer":
            mapping["audio_player"] = m
        elif cls == "MultiModal":
            mapping["multimodal"] = m
        elif cls == "Memory":
            mapping["memory"] = m
    return mapping


def main():
    global _global_signals, _global_prompter
    selected_audio_mode = choose_audio_mode()

    signals = Signals()

    signals.log_hub = LogHub()
    signals.log_hub.start()

    _global_signals = signals

    # MicInput spins up its own local Whisper model (~1 GB RAM, several
    # seconds of startup). Skip it entirely when audio is routed to
    # Discord only.
    mic_enabled = selected_audio_mode in ("local", "both")

    modules = [
        Memory(signals, enabled=True),
        LibraryIndexer(signals, enabled=True),
        MultiModal(signals, enabled=True),
        LocalInput(signals, enabled=True),
        ConsoleInput(signals, enabled=True),
        MicInput(signals, enabled=mic_enabled),
        DiscordClient(signals, enabled=True, audio_mode=selected_audio_mode),
        VtubeStudio(signals, enabled=True),
        BrowserController(signals, enabled=True),
        Heartbeat(signals, enabled=True),
        EmotionalStateModule(signals, enabled=True),
        AmbientPerception(signals, enabled=False),
        SelfModel(signals, enabled=True),
        SDKServer(signals, enabled=True),
    ]

    tts_module = TTS(signals, enabled=True, modules=modules,
                     audio_mode=selected_audio_mode)
    modules.append(tts_module)

    threads = []
    for module in modules:
        t = threading.Thread(target=module.init_event_loop, daemon=True)
        t.start()
        threads.append(t)

    prompter = Prompter(signals, modules)
    _global_prompter = prompter

    sio_server = SocketIOServer(
        signals,
        stt=None,
        tts=tts_module,
        llmWrapper=None,
        prompter=prompter,
        modules=_build_module_map(modules),
    )
    threading.Thread(target=sio_server.run, daemon=True).start()

    time.sleep(1.5)

    print("\n=======================================================")
    print(f"  Lumi AI Ready — Audio routed to: {selected_audio_mode.upper()}")
    print(f"  Mic: active (press \\ to toggle mute)")
    print(f"  Web client:  http://127.0.0.1:5000/")
    print(f"  Diagnostics: http://127.0.0.1:5001/")
    print(f"  SDK:         ws://127.0.0.1:5056/ws")
    print("=======================================================\n")

    signals.system_ready = True

    signals.log_hub.emit("system", f"Lumi booting — audio mode = {selected_audio_mode}")

    try:
        asyncio.run(prompter.run())
    except KeyboardInterrupt:
        print("\nShutting down companion engine...")
        try:
            if not getattr(prompter, "_session_saved_this_run", False):
                prompter.save_session_state()
        except Exception as e:
            print(f"\033[91mSESSION: Shutdown save failed: {e}\033[0m")
        signals.terminate = True
        time.sleep(3.0)
        os._exit(0)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()