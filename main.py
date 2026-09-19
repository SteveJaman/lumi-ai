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

# TTS may live at root OR in modules/ depending on how you've organized files.
try:
    from modules.tts import TTS
except ImportError:
    from tts import TTS


# Global reference so the OS signal handler can reach it
_global_signals = None


def handle_exit(sig, frame):
    """Triggers clean shutdown across all modules when Ctrl+C is pressed."""
    print("\n\033[93m[System] Interrupt received, shutting down cleanly...\033[0m")
    if _global_signals:
        _global_signals.terminate = True
    time.sleep(1.5)  # Gives Discord a second to drop voice connection
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
    global _global_signals
    selected_audio_mode = choose_audio_mode()

    signals = Signals()
    _global_signals = signals  # Bind to global scope for signal handler

    # ------------------------------------------------------------------
    # Module roster. Flip `enabled=` to False to disable any module
    # without removing it. MicInput is the biggest CPU saver if you
    # mostly type; DiscordClient only needed for Discord voice.
    # ------------------------------------------------------------------
    modules = [
        Memory(signals, enabled=True),
        MultiModal(signals, enabled=True),
        LocalInput(signals, enabled=True),
        ConsoleInput(signals, enabled=True),
        MicInput(signals, enabled=True),
        DiscordClient(signals, enabled=True, audio_mode=selected_audio_mode),
        VtubeStudio(signals, enabled=False),
        BrowserController(signals, enabled=True),
    ]

    # Instantiate TTS, pass the modules reference AND the selected audio mode
    tts_module = TTS(signals, enabled=True, modules=modules, audio_mode=selected_audio_mode)
    modules.append(tts_module)

    # Spawn a daemon thread for each service module's event loop
    threads = []
    for module in modules:
        t = threading.Thread(target=module.init_event_loop, daemon=True)
        t.start()
        threads.append(t)

    prompter = Prompter(signals, modules)
    sio_server = SocketIOServer(signals)

    threading.Thread(target=sio_server.run, daemon=True).start()

    time.sleep(1.5)

    print("\n=======================================================")
    print(f"  Lumi AI Ready — Audio routed to: {selected_audio_mode.upper()}")
    print("=======================================================\n")

    signals.system_ready = True

    try:
        asyncio.run(prompter.run())
    except KeyboardInterrupt:
        print("\nShutting down companion engine...")
        signals.terminate = True
        time.sleep(1.5)
        os._exit(0)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()