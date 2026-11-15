"""
Speculative turn (SPECULATIVE_TURN_ENABLED, off by default).

While you're still talking, mic_input.py watches RealtimeSTT's stabilized
partial transcript. Once it has SPECULATIVE_MIN_WORDS words and has stopped
changing much (a "stable partial"), her reply is started from it in the
background: the same request a real turn would make (prompter.
conversational_request), marked speculative, and nothing is spoken.

When the final transcript arrives:
  - similarity (difflib.SequenceMatcher on normalized text) >=
    SPECULATIVE_MATCH_RATIO: accept. The prompter's turn takes the run over:
    what was written so far is kept and generation continues from that point
    (an assistant prefill) with the final transcript in place of the partial.
    If the run had already finished, its reply is used as it is.
  - otherwise: reject. The run is cancelled and the turn starts fresh.
Both are logged at INFO: "speculative accept (ratio=0.93)" /
"speculative reject (ratio=0.41)".

A finished run is never spoken on its own: it waits for the final transcript
and the match check. With the flag off nothing here runs (one `if`).
"""

from __future__ import annotations

import difflib
import logging
import re
import threading
import time
from typing import Any, Dict, Optional

import constants
from modules.log_hub import emit as hub_emit

log = logging.getLogger("lumi.speculative")


def _cfg(name, default):
    return getattr(constants, name, default)


def enabled() -> bool:
    # Not in Gaming Mode: models unload after every call there, so a draft
    # would load the chat model only for the real turn to load it again.
    return bool(_cfg("SPECULATIVE_TURN_ENABLED", False)) and not _cfg("GAMING_MODE", False)


def normalize(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9' ]+", " ", (text or "").lower()).split())


def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, normalize(a), normalize(b)).ratio()


class SpeculativeRun:
    def __init__(self, partial: str, build):
        self.partial = partial
        self._build = build            # () -> request dict; runs on the run's own thread
        self.request: Optional[Dict[str, Any]] = None
        self.text = ""
        self.cancel = threading.Event()
        self.done = threading.Event()
        self.finished = False          # the model finished on its own (not cancelled)
        self.truncated = False
        self.error: Optional[str] = None
        self.final: Optional[str] = None
        self.ratio: Optional[float] = None
        self.started = time.time()
        self.thread: Optional[threading.Thread] = None

    def _collect(self, piece: str) -> bool:
        if self.cancel.is_set():
            return False
        self.text += piece
        return True

    def run(self, chat) -> None:
        try:
            # Built here, not on the mic thread: it asks the model router, which blocks.
            req = self.request = self._build()
            if self.cancel.is_set():
                return
            resp = chat(req["model"], req["messages"], think=req["think"], keep_alive=req["keep_alive"],
                        options=req["options"], on_text=self._collect)
            if not self.cancel.is_set():
                self.finished = True
                raw = resp.raw if isinstance(getattr(resp, "raw", None), dict) else {}
                self.truncated = (raw.get("done_reason") == "length"
                                  or (raw.get("eval_count") or 0) >= req["options"].get("num_predict", 10 ** 9))
                self.text = resp.content or self.text
        except Exception as e:
            self.error = str(e)
        finally:
            self.done.set()

    def stop(self, timeout: float = 10.0) -> None:
        """Freeze it where it is (it ends at the next token)."""
        self.cancel.set()
        self.done.wait(timeout)


class SpeculativeTurn:
    def __init__(self, signals, chat=None):
        self.signals = signals
        self._chat = chat
        self._lock = threading.Lock()
        self.run: Optional[SpeculativeRun] = None
        signals.speculative = self

    def _chat_fn(self):
        if self._chat is None:
            from modules.llm_providers import chat_sync
            self._chat = chat_sync
        return self._chat

    def _busy(self) -> bool:
        s = self.signals
        return bool(getattr(s, "AI_thinking", False) or getattr(s, "AI_speaking", False))

    def on_stable_partial(self, text: str) -> Optional[SpeculativeRun]:
        """mic_input: a stable partial. Starts a run if the flag is on and
        she's free. Returns the run (or None)."""
        if not enabled():
            return None
        prompter = getattr(self.signals, "prompter", None)
        if prompter is None or self._busy() or not hasattr(prompter, "conversational_request"):
            return None
        with self._lock:
            if self.run is not None and similarity(self.run.partial, text) >= 0.95:
                return self.run                       # already running on (nearly) this
            old, self.run = self.run, None
        if old is not None:
            old.cancel.set()
        run = SpeculativeRun(text, lambda: prompter.conversational_request(text, pending_user=text))
        run.thread = threading.Thread(target=run.run, args=(self._chat_fn(),), daemon=True,
                                      name="SpeculativeTurn")
        with self._lock:
            self.run = run
        run.thread.start()
        hub_emit(self.signals, "voice", f"speculative turn started from: {text[:80]}")
        return run

    def on_final(self, text: str) -> Optional[bool]:
        """mic_input: the final transcript. True = accepted (the turn will
        take it over), False = rejected and cancelled, None = no run."""
        if not enabled():
            return None
        with self._lock:
            run = self.run
        if run is None:
            return None
        ratio = similarity(run.partial, text)
        run.ratio = ratio
        if ratio >= float(_cfg("SPECULATIVE_MATCH_RATIO", 0.8)):
            run.final = text
            log.info("speculative accept (ratio=%.2f)", ratio)
            print(f"\033[90m[Speculative] speculative accept (ratio={ratio:.2f})\033[0m")
            hub_emit(self.signals, "voice", f"speculative accept (ratio={ratio:.2f})")
            return True
        with self._lock:
            if self.run is run:
                self.run = None
        run.cancel.set()
        log.info("speculative reject (ratio=%.2f)", ratio)
        print(f"\033[90m[Speculative] speculative reject (ratio={ratio:.2f})\033[0m")
        hub_emit(self.signals, "voice", f"speculative reject (ratio={ratio:.2f})")
        return False

    def claim(self, final_text: str) -> Optional[SpeculativeRun]:
        """prompter: the accepted run for this turn's message, or None. Any
        other run left over is cancelled."""
        if not enabled():
            return None
        with self._lock:
            run, self.run = self.run, None
        if run is None:
            return None
        if run.final is not None and normalize(run.final) == normalize(final_text):
            if run.request is not None:           # not built yet (router still answering) or it failed:
                return run                        # take the normal path instead
            run.cancel.set()
            return None
        run.cancel.set()
        return None
