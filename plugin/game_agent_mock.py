"""
A game agent that plays nothing: the smallest BaseGameAgent, to show the
multi-game setup works without a real game. It connects to Lumi as
"MockAgent", pushes a silent status line, and counts its ticks.

The Games page lists it only when LUMI_DEV_GAMES includes "mock"
(modules/game_manager.py). Run it by hand with:
    python plugin/game_agent_mock.py
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plugin.base_game_agent import BaseGameAgent


class MockAgent(BaseGameAgent):
    GAME_ID = "mock"
    GAME_NAME = "MockAgent"
    EVENT_TAG = "MOCK EVENT"
    LOOP_INTERVAL = 5.0

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.ticks = 0

    async def setup(self) -> None:
        await self.send_context("A mock game agent is connected. It doesn't play anything.")

    async def tick(self) -> None:
        self.ticks += 1
        if self.ticks % 6 == 0:
            await self.send_context(f"The mock game has ticked {self.ticks} times. Nothing happened.")


async def _main() -> None:
    agent = MockAgent()
    agent.start_loop()
    await agent.run()


if __name__ == "__main__":
    asyncio.run(_main())
