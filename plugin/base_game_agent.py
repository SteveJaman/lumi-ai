"""
BaseGameAgent: the shape every background game agent shares.

A game agent is Lumi's "other half" in a game: its own process, its own
loop, connected to Lumi's SDK server as a client. It runs whenever the
game's switch on the Games page is on (data/minecraft/autoplay.json, keyed
by game id), whatever Lumi's agent mode is set to, and talks to Lumi only
through the SDK:

    send_context(text)              silent standing status (what it's doing)
    narrate(text, event_kind=...)   a moment Lumi reacts to out loud
    feel(event_kind)                just the emotional nudge of a moment
    remember(text)                  a milestone kept as a long-term memory

Subclasses set GAME_ID and GAME_NAME and implement tick(), one step of
play. MinecraftAgent (plugin/minecraft_bot.py) runs its own richer loop by
overriding loop(); plugin/game_agent_mock.py is the smallest possible
agent. New games also need an entry in modules/game_manager.py's registry
so the Games page can start and stop them.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Optional

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from sdk import LumiClient

DATA_ROOT     = os.path.join(_ROOT, "data")
AUTOPLAY_PATH = os.path.join(DATA_ROOT, "minecraft", "autoplay.json")   # all games share it
TOKEN_PATH    = os.path.join(DATA_ROOT, "memories", "sdk_token.txt")


class BaseGameAgent(LumiClient):
    GAME_ID = ""              # key in autoplay.json and in the game registry
    GAME_NAME = ""            # SDK client name, e.g. "MinecraftAgent"
    EVENT_TAG = "GAME EVENT"  # narrations are sent as "[EVENT_TAG] text"
    LOOP_INTERVAL = 4.0       # seconds between ticks

    def __init__(self, token_path: str = TOKEN_PATH, reconnect_delay: float = 5.0, **kw) -> None:
        if not self.GAME_ID or not self.GAME_NAME:
            raise TypeError(f"{type(self).__name__} must set GAME_ID and GAME_NAME")
        super().__init__(game=self.GAME_NAME, token_path=token_path,
                         reconnect_delay=reconnect_delay, **kw)
        self._paused = False
        self._loop_task: Optional[asyncio.Task] = None

    # ─── The switch ───────────────────────────────────────────

    def autoplay_allowed(self) -> bool:
        """The game's Games page switch. A missing file or entry counts as
        on, so an agent started by hand plays."""
        try:
            with open(AUTOPLAY_PATH, encoding="utf-8") as f:
                return bool(json.load(f).get(self.GAME_ID, True))
        except Exception:
            return True

    # ─── The loop ─────────────────────────────────────────────

    def start_loop(self) -> None:
        if self._loop_task is None or self._loop_task.done():
            self._loop_task = asyncio.create_task(self.loop())

    async def loop(self) -> None:
        """tick() every LOOP_INTERVAL while the switch is on and the agent
        isn't paused. Runs whether or not Lumi is connected."""
        while not self._stop:
            await asyncio.sleep(self.LOOP_INTERVAL)
            if self._paused or not self.autoplay_allowed():
                continue
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[{self.GAME_ID}-agent] tick failed: {e}")

    async def tick(self) -> None:
        """One step of play."""
        raise NotImplementedError

    # ─── Talking to Lumi ──────────────────────────────────────

    async def narrate(self, text: str, event_kind: Optional[str] = None) -> None:
        """A moment Lumi reacts to out loud (one turn of her chat model)."""
        await self.send_context(f"[{self.EVENT_TAG}] {text}", silent=False, event_kind=event_kind)
