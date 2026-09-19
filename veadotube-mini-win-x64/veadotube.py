import time
import re
from pynput.keyboard import Controller, Key
from modules.module import Module

class VeadotubeController(Module):
    def __init__(self, signals, enabled=True):
        super().__init__(signals, enabled)
        self.keyboard = Controller()
        
        # Map keywords or emotion tags to Veadotube Mini hotkeys
        # (Configure these keys to match whatever shortcuts you set up in Veadotube Mini settings)
        self.emotion_hotkeys = {
            "smug": Key.f1,
            "angry": Key.f2,
            "thinking": Key.f3,
            "laugh": Key.f4,
            "default": Key.f5
        }

    def trigger_expression(self, emotion):
        """Simulates pressing a hotkey to change Veadotube Mini's active state."""
        if not self.enabled:
            return
            
        key = self.emotion_hotkeys.get(emotion.lower(), self.emotion_hotkeys["default"])
        try:
            self.keyboard.press(key)
            self.keyboard.release(key)
            print(f"\033[90m[Veadotube: Triggered expression '{emotion}']\033[0m")
        except Exception as e:
            print(f"\033[91m[Veadotube Error: Could not press hotkey: {e}]\033[0m")

    def inspect_and_trigger(self, text):
        """Scans assistant replies for emotional markers or tone to switch expressions."""
        lowered = text.lower()
        if any(w in lowered for w in ["smug", "well, well", "clueless", "please"]):
            self.trigger_expression("smug")
        elif any(w in lowered for w in ["brat", "annoying", "idiot"]):
            self.trigger_expression("angry")
        elif any(w in lowered for w in ["hmm", "let me check", "searching"]):
            self.trigger_expression("thinking")
        else:
            self.trigger_expression("default")

    async def run(self):
        while not self.signals.terminate:
            await asyncio.sleep(0.1)