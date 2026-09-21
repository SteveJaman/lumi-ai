"""
Terminal companion interface with prompt-at-bottom rendering.

The prompt ("Anthony: ") stays pinned to the bottom of the terminal
at all times, like a modern chat UI. All background output — Lumi's
replies, heartbeat announcements, memory diagnostics — is rendered
above the prompt line.

How it works:
  - sys.stdout is replaced with an interceptor that routes every
    print() through a thread-safe queue.
  - The input loop uses msvcrt on Windows to read characters one at
    a time, so it can redraw the prompt whenever new output arrives
    without losing partially-typed input.
  - Non-Windows fallback uses plain input() with no prompt-pinning.
"""

import asyncio
import queue
import sys
import time
import threading

from modules.module import Module

CYAN = "\033[96m"
MAGENTA = "\033[95m"
GRAY = "\033[90m"
RESET = "\033[0m"
BOLD = "\033[1m"

try:
    import msvcrt
    HAS_MSVCRT = True
except ImportError:
    HAS_MSVCRT = False


class _StdoutInterceptor:
    """Replaces sys.stdout so every print() lands in a queue instead of
    directly at the cursor. Buffers until a newline so multi-arg prints
    arrive as one message."""

    def __init__(self, q, original):
        self._queue = q
        self._original = original
        self._buf = ""
        self._lock = threading.Lock()

    def write(self, text):
        if not text:
            return 0
        with self._lock:
            self._buf += str(text)
            while "\n" in self._buf:
                idx = self._buf.index("\n") + 1
                line = self._buf[:idx]
                self._buf = self._buf[idx:]
                self._queue.put(line)
        return len(text)

    def flush(self):
        with self._lock:
            if self._buf:
                self._queue.put(self._buf)
                self._buf = ""

    def isatty(self):
        return True

    def fileno(self):
        return self._original.fileno()

    def __getattr__(self, name):
        return getattr(self._original, name)


class ConsoleInput(Module):
    def __init__(self, signals, enabled=True):
        super().__init__(signals, enabled)
        self._output_queue = queue.Queue()
        self._original_stdout = None

    # ---------------------------------------------------------------
    # Rendering
    # ---------------------------------------------------------------
    def _render(self, text, buf, prompt_active=True):
        """Clear the prompt line, write `text` above, then redraw the
        prompt with the current input buffer."""
        out = self._original_stdout
        out.write("\r\033[K")  # return to start, clear line

        # Only write a newline-terminated text block when there is
        # actually something to write. Skipping this on empty text is
        # what prevents backspace from accumulating blank lines.
        if text:
            if not text.endswith("\n"):
                text = text + "\n"
            out.write(text)

        if prompt_active:
            out.write(f"{BOLD}{CYAN}Anthony:{RESET} " + "".join(buf))
        out.flush()

    def _drain(self, buf, prompt_active=True):
        """Render every queued message. Returns True if anything was
        rendered."""
        chunks = []
        while True:
            try:
                chunks.append(self._output_queue.get_nowait())
            except queue.Empty:
                break
        if not chunks:
            return False
        self._render("".join(chunks), buf, prompt_active=prompt_active)
        return True

    # ---------------------------------------------------------------
    # Windows: character-level input
    # ---------------------------------------------------------------
    def _read_line_windows(self):
        buf = []
        out = self._original_stdout

        # Draw the prompt via _render so it clears the current line
        # first. Writing it directly would double up with the prompt
        # that _render already left on the line after the last turn's
        # output finished rendering.
        self._render("", buf, prompt_active=True)

        while not self.signals.terminate:
            self._drain(buf, prompt_active=True)

            if msvcrt.kbhit():
                ch = msvcrt.getwch()

                if ch in ("\r", "\n"):
                    out.write("\n")
                    out.flush()
                    return "".join(buf)

                if ch == "\x03":  # Ctrl+C
                    raise KeyboardInterrupt

                if ch == "\b":  # backspace
                    if buf:
                        buf.pop()
                        self._render("", buf, prompt_active=True)
                    continue

                if ch in ("\x00", "\xe0"):
                    # Extended key (arrows, F-keys) — consume and ignore.
                    msvcrt.getwch()
                    continue

                if ch == "\t":
                    buf.append("    ")
                    out.write("    ")
                    out.flush()
                    continue

                buf.append(ch)
                out.write(ch)
                out.flush()
            else:
                time.sleep(0.02)

        return ""

    # ---------------------------------------------------------------
    # Non-Windows fallback
    # ---------------------------------------------------------------
    def _read_line_fallback(self):
        # Bare-bones: no prompt pinning, uses plain input().
        prompt = f"{BOLD}{CYAN}Anthony:{RESET} "
        try:
            return input(prompt)
        except (EOFError, KeyboardInterrupt):
            return ""

    # ---------------------------------------------------------------
    # Main
    # ---------------------------------------------------------------
    async def run(self):
        # Wait for main.py to finish its banner.
        while not getattr(self.signals, "system_ready", False) and \
                not self.signals.terminate:
            await asyncio.sleep(0.1)
        if self.signals.terminate:
            return

        # Header banner (still using the real stdout — interceptor goes
        # in *after* this so the header prints normally).
        print("\n" + f"{GRAY}=" * 55 + f"{RESET}")
        print(f"  {BOLD}{MAGENTA}Lumi AI{RESET} {GRAY}— Terminal Companion Interface{RESET}")
        print(f"{GRAY}" + "=" * 55 + f"{RESET}\n")

        # Install interceptor.
        self._original_stdout = sys.stdout
        sys.stdout = _StdoutInterceptor(self._output_queue, self._original_stdout)

        loop = asyncio.get_running_loop()

        try:
            while not self.signals.terminate:
                try:
                    if HAS_MSVCRT:
                        user_text = await loop.run_in_executor(
                            None, self._read_line_windows
                        )
                    else:
                        user_text = await loop.run_in_executor(
                            None, self._read_line_fallback
                        )
                except (EOFError, KeyboardInterrupt):
                    await asyncio.sleep(0.5)
                    continue

                cleaned = (user_text or "").strip()
                if not cleaned:
                    continue

                self.signals.history.append(
                    {"role": "user", "content": cleaned}
                )
                self.signals.new_message = True

                # Wait for the prompter to consume this turn, draining
                # output above the (currently blank) prompt line so
                # Lumi's reply streams into view.
                while self.signals.new_message and not self.signals.terminate:
                    self._drain([], prompt_active=True)
                    await asyncio.sleep(0.02)
                while self.signals.AI_thinking and not self.signals.terminate:
                    self._drain([], prompt_active=True)
                    await asyncio.sleep(0.02)
        finally:
            if self._original_stdout is not None:
                sys.stdout = self._original_stdout