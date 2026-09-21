import asyncio
import os
import threading
import multiprocessing
import time
import signal

# Prevents a common Windows DLL conflict error with PyTorch
os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"

from signals import Signals
from constants import WORKSPACE_ROOT

from modules.memory import Memory
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
from modules.ambient_perception import AmbientPerception
from modules.log_hub import LogHub
from modules.self_model import SelfModel

# TTS may live at root OR in modules/ depending on how you've organized files.
try:
    from modules.tts import TTS
except ImportError:
    from tts import TTS


# Global references so the OS signal handler can reach them
_global_signals = None
_global_prompter = None

# Shutdown guard. Windows delivers SIGINT to the whole process group at once,
# and RealtimeSTT's spawned children re-import main.py on Windows — so without
# a guard, every process would print its own shutdown message. The lock
# serializes within a process; the process-name check in handle_exit limits
# the actual work to the parent process only.
_shutdown_lock = threading.Lock()
_shutdown_started = False


def handle_exit(sig, frame):
    """Triggers clean shutdown across all modules when Ctrl+C is pressed.
    Saves session state before terminating so Lumi remembers where we left off.

    Only the main process handles this. RealtimeSTT spawns children that
    re-import main.py on Windows, so without the process check each child
    would register its own SIGINT handler and print its own shutdown line."""
    global _shutdown_started

    # Ignore the signal in multiprocessing children.
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

    # Give spawned children a moment to notice terminate and exit cleanly
    # before we hard-kill the process tree.
    time.sleep(3.0)
    os._exit(0)


signal.signal(signal.SIGINT, handle_exit)


def choose_audio_mode():
    """Interactive terminal menu to select audio routing on startup."""
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


def main():
    global _global_signals, _global_prompter
    selected_audio_mode = choose_audio_mode()

    signals = Signals()

    # Diagnostics hub — receives structured events from all modules and
    # serves them at http://127.0.0.1:5001/. Keeps high-frequency loop
    # output (ambient skips, heartbeat ticks) out of the terminal.
    signals.log_hub = LogHub()
    signals.log_hub.start()

    _global_signals = signals

    modules = [
        Memory(signals, enabled=True),
        MultiModal(signals, enabled=True),
        LocalInput(signals, enabled=True),
        ConsoleInput(signals, enabled=True),
        MicInput(signals, enabled=True),
        DiscordClient(signals, enabled=True, audio_mode=selected_audio_mode),
        VtubeStudio(signals, enabled=False),
        BrowserController(signals, enabled=True),
        Heartbeat(signals, enabled=True),
        EmotionalStateModule(signals, enabled=True),
        AmbientPerception(signals, enabled=True),
        SelfModel(signals, enabled=True),
    ]

    tts_module = TTS(signals, enabled=True, modules=modules, audio_mode=selected_audio_mode)
    modules.append(tts_module)

    threads = []
    for module in modules:
        t = threading.Thread(target=module.init_event_loop, daemon=True)
        t.start()
        threads.append(t)

    prompter = Prompter(signals, modules)
    _global_prompter = prompter

    sio_server = SocketIOServer(signals)
    threading.Thread(target=sio_server.run, daemon=True).start()

    time.sleep(1.5)

    print("\n=======================================================")
    print(f"  Lumi AI Ready — Audio routed to: {selected_audio_mode.upper()}")
    print(f"  Mic: active (press \\ to toggle mute)")
    print(f"  Diagnostics: http://127.0.0.1:{signals.log_hub.port}/")
    print("=======================================================\n")

    signals.system_ready = True

    signals.log_hub.start()
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