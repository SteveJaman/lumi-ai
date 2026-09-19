import asyncio
import time
from modules.module import Module

CYAN = "\033[96m"
MAGENTA = "\033[95m"
GRAY = "\033[90m"
RESET = "\033[0m"
BOLD = "\033[1m"

class ConsoleInput(Module):
    def __init__(self, signals, enabled=True):
        super().__init__(signals, enabled)

    async def run(self):
        # Synchronously hold execution in this thread until main.py sets system_ready = True
        while not getattr(self.signals, 'system_ready', False) and not self.signals.terminate:
            await asyncio.sleep(0.1)

        if self.signals.terminate:
            return

        print("\n" + f"{GRAY}=" * 55 + f"{RESET}")
        print(f"  {BOLD}{MAGENTA}Lumi AI{RESET} {GRAY}— Terminal Companion Interface{RESET}")
        print(f"{GRAY}" + "=" * 55 + f"{RESET}\n")

        loop = asyncio.get_running_loop()
        while not self.signals.terminate:
            try:
                user_text = await loop.run_in_executor(None, input, f"{BOLD}{CYAN}Anthony:{RESET} ")
            except (EOFError, KeyboardInterrupt):
                user_text = ""
                await asyncio.sleep(0.5)
                continue

            cleaned_text = user_text.strip()
            if not cleaned_text:
                continue

            current_history = self.signals.history
            current_history.append({"role": "user", "content": cleaned_text})
            self.signals.history = current_history
            self.signals.new_message = True

            while self.signals.new_message and not self.signals.terminate:
                await asyncio.sleep(0.02)
            while self.signals.AI_thinking and not self.signals.terminate:
                await asyncio.sleep(0.02)