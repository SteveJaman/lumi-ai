"""
Console input module.

Reads a line of text from the terminal, pins a prompt at the bottom of
the screen so background prints from other modules don't clobber what
the user is typing, and pushes complete lines into signals for the
prompter to consume.

The prompt label reads HOST.name live, so a login switch updates the
visible prompt without restarting.

Terminal input and web input share the same path: append to
signals.history as a user message, set new_message=True, and the
prompter loop picks it up on its next tick.
"""

import sys
import time
import asyncio
import threading

from modules.module import Module
from constants import HOST


TICK_SECONDS = 0.03

_CLEAR_LINE = "\033[2K"
_HIDE_CURSOR = "\033[?25l"
_SHOW_CURSOR = "\033[?25h"


class ConsoleInput(Module):

    def __init__(self, signals, enabled=True):
        super().__init__(signals, enabled)

        self._buffer = []
        self._lock = threading.Lock()

        # Other modules can call this to repaint the prompt after
        # printing background output.
        self.signals.console_redraw = self.redraw

    # ---------------------------------------------------------------
    # Prompt drawing
    # ---------------------------------------------------------------
    def _prompt_label(self):
        return f"{HOST.name}: "

    def redraw(self):
        with self._lock:
            try:
                sys.stdout.write(
                    "\r" + _CLEAR_LINE
                    + self._prompt_label()
                    + "".join(self._buffer)
                )
                sys.stdout.flush()
            except Exception:
                pass

    def _clear_line(self):
        try:
            sys.stdout.write("\r" + _CLEAR_LINE)
            sys.stdout.flush()
        except Exception:
            pass

    # ---------------------------------------------------------------
    # Line reader
    # ---------------------------------------------------------------
    def _read_line(self):
        try:
            import msvcrt
        except ImportError:
            try:
                return input(self._prompt_label())
            except (EOFError, KeyboardInterrupt):
                return None

        buf = []
        with self._lock:
            self._buffer = buf
        self.redraw()

        while True:
            if self.signals.terminate:
                return None

            # Yield stdin during a y/n confirmation so the prompter's
            # input() call gets the keystroke instead of us.
            if getattr(self.signals, "input_paused", False):
                time.sleep(0.05)
                continue

            if not msvcrt.kbhit():
                time.sleep(TICK_SECONDS)
                continue

            ch = msvcrt.getwch()

            if ch in ("\r", "\n"):
                sys.stdout.write("\n")
                sys.stdout.flush()
                with self._lock:
                    self._buffer = []
                return "".join(buf)

            if ch == "\x03":
                raise KeyboardInterrupt

            if ch == "\x08":
                if buf:
                    buf.pop()
                    self.redraw()
                continue

            if ch in ("\x00", "\xe0"):
                msvcrt.getwch()
                continue

            if ch == "\x1b":
                continue

            if ch >= " ":
                buf.append(ch)
                try:
                    sys.stdout.write(ch)
                    sys.stdout.flush()
                except Exception:
                    pass

    # ---------------------------------------------------------------
    # Thread entry
    # ---------------------------------------------------------------
    def _input_loop(self):
        try:
            sys.stdout.write(_HIDE_CURSOR)
            sys.stdout.flush()
        except Exception:
            pass

        try:
            while not self.signals.terminate:
                try:
                    line = self._read_line()
                except KeyboardInterrupt:
                    self.signals.terminate = True
                    break

                if line is None:
                    break

                line = line.strip()
                if not line:
                    continue

                # Route into the shared signals — the same path the
                # web client uses. The prompter reads history[-1] when
                # new_message flips True, so this is all that's needed.
                # Terminal commands (!agent_on, !list, etc.) also flow
                # through here and are picked up by the prompter's
                # command router on the same tick.
                try:
                    self.signals.history.append(
                        {"role": "user", "content": line}
                    )
                    self.signals.new_message = True
                    self.signals.last_message_time = time.time()
                    if hasattr(self.signals, "save_history"):
                        self.signals.save_history()
                except Exception as e:
                    print(f"\033[91mCONSOLE: failed to route input — {e}\033[0m")
        finally:
            try:
                sys.stdout.write(_SHOW_CURSOR)
                sys.stdout.flush()
            except Exception:
                pass

    def init_event_loop(self):
        # NOTE: Do NOT call super().init_event_loop() here. The base class
        # does asyncio.run(self.run()) which is an infinite sleep loop —
        # the reader thread below would never start. ConsoleInput has no
        # useful async work, so spawn the reader thread and return.
        if not self.enabled:
            return
        t = threading.Thread(
            target=self._input_loop,
            name="ConsoleInput",
            daemon=True,
        )
        t.start()

    async def run(self):
        while not self.signals.terminate:
            await asyncio.sleep(1)