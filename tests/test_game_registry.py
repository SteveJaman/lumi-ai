"""The multi-game setup: BaseGameAgent, the mock agent, and game_manager's
registry, proven with a game that plays nothing."""

import asyncio
import json
import types

import pytest

import modules.game_manager as gm
import plugin.base_game_agent as base
from plugin.base_game_agent import BaseGameAgent
from plugin.game_agent_mock import MockAgent
from plugin.minecraft_bot import MinecraftAgent


def test_minecraft_agent_is_a_base_game_agent():
    assert issubclass(MinecraftAgent, BaseGameAgent)
    assert (MinecraftAgent.GAME_ID, MinecraftAgent.GAME_NAME) == ("minecraft", "MinecraftAgent")


def test_an_agent_must_name_its_game():
    class Nameless(BaseGameAgent):
        pass
    with pytest.raises(TypeError):
        Nameless(token="t")


def test_mock_agent_ticks_only_while_its_switch_is_on(tmp_path, monkeypatch):
    path = tmp_path / "autoplay.json"
    monkeypatch.setattr(base, "AUTOPLAY_PATH", str(path))
    agent = MockAgent(token="t")
    agent.LOOP_INTERVAL = 0.01
    sent = []

    async def send(text, silent=True, event_kind=None):
        sent.append(text)
    agent.send_context = send

    async def run_for(seconds):
        agent.start_loop()
        await asyncio.sleep(seconds)
        agent._stop = True
        await asyncio.sleep(0.03)

    path.write_text(json.dumps({"mock": False, "minecraft": True}))
    asyncio.run(run_for(0.1))
    assert agent.ticks == 0
    agent._stop, agent._loop_task = False, None
    path.write_text(json.dumps({"mock": True}))
    asyncio.run(run_for(0.15))
    assert agent.ticks > 0


def _manager(monkeypatch, tmp_path):
    monkeypatch.setattr(gm, "AUTOPLAY_PATH", str(tmp_path / "autoplay.json"))
    monkeypatch.setattr(gm, "MC_DIR", str(tmp_path))
    monkeypatch.setitem(gm.GAME_REGISTRY, "mock", {"label": "Mock game", "processes": ["agent"],
                                                   "agent": gm.MOCK_SCRIPT})
    mgr = gm.GameManager.__new__(gm.GameManager)
    mgr.signals = types.SimpleNamespace(log_hub=None)
    mgr._lock = gm.threading.Lock()
    mgr._children = {}
    spawned = []

    class _Running:
        running = True
        def info(self):
            return {"running": True}

    def fake_spawn(game, name, cmd, cwd):
        spawned.append((game, name, cmd[-1]))
        mgr._children[f"{game}:{name}"] = _Running()
    mgr._spawn = fake_spawn
    return mgr, spawned


def test_registry_starts_a_second_game_on_its_own(monkeypatch, tmp_path):
    mgr, spawned = _manager(monkeypatch, tmp_path)
    mgr.set_enabled("mock", True)
    assert spawned == [("mock", "agent", gm.MOCK_SCRIPT)]
    assert gm.read_autoplay() == {"mock": True}
    games = {g["game"]: g for g in mgr.list_games()}
    assert games["mock"]["running"] and games["mock"]["enabled"] and not games["mock"]["detailed"]
    assert games["minecraft"]["detailed"] and not games["minecraft"]["running"]


def test_only_minecraft_takes_orders(monkeypatch, tmp_path):
    mgr, _ = _manager(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="doesn't take orders"):
        mgr.send_directive("mock", "follow me")
    with pytest.raises(ValueError, match="unknown game"):
        mgr.send_directive("valheim", "follow me")


def test_unknown_game_is_refused(monkeypatch, tmp_path):
    mgr, _ = _manager(monkeypatch, tmp_path)
    with pytest.raises(ValueError):
        mgr.set_enabled("valheim", True)


@pytest.mark.parametrize("script", ["minecraft_bot.py", "game_agent_mock.py"])
def test_agents_import_when_run_as_scripts(script):
    """Run as a script, Python puts plugin/ first on the path, where
    plugin/plugin.py used to shadow the plugin package
    ("cannot import name 'game_vision' from 'plugin'")."""
    import os
    import subprocess
    import sys
    plugin_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "plugin")
    code = ("import runpy, sys; sys.path.insert(0, sys.argv[1]); "
            "runpy.run_path(sys.argv[2], run_name='script_import_check')")
    r = subprocess.run([sys.executable, "-c", code, plugin_dir, os.path.join(plugin_dir, script)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr[-800:]
