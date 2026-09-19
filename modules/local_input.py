from modules.module import Module  #[cite: 14]
import asyncio
import time

try:
    from pynput import keyboard
    HAS_PYNPUT = True
except ImportError:
    HAS_PYNPUT = False

class LocalInput(Module):  #[cite: 14]
    def __init__(self, signals, enabled=True):  #[cite: 14]
        super().__init__(signals, enabled)  #[cite: 14]
        self.prompt_injection.priority = 100  #[cite: 12]

    def on_shortcut_triggered(self):
        print("LOCAL_INPUT: Global hotkey triggered. Capturing context...")
        self.signals.hotkey_pressed = True
        self.signals.new_message = True
        self.signals.last_message_time = time.time()

    async def run(self):  #[cite: 14]
        if not self.enabled or not HAS_PYNPUT:
            return

        # Listen for Ctrl+Shift+Space as global hotkey
        hotkey = keyboard.GlobalHotKeys({
            '<ctrl>+<shift>+<space>': self.on_shortcut_triggered
        })
        hotkey.start()

        while not self.signals.terminate:  #[cite: 14]
            await asyncio.sleep(0.1)  #[cite: 14]
            
        hotkey.stop()