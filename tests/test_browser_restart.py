"""browse_url survives a dead browser: one event loop for the browser's life (so
Playwright's connection never outlives its loop), one relaunch on a dead-browser
error, then a clean message. Playwright itself is faked; no Firefox starts."""

import threading
import types

from modules import browser_controller as bc


def controller():
    return bc.BrowserController(types.SimpleNamespace(), enabled=True)


def test_dead_browser_errors_are_recognised():
    assert bc.is_dead_browser_error(AttributeError("'NoneType' object has no attribute 'send'"))
    assert bc.is_dead_browser_error(RuntimeError("Page.goto: Target page, context or browser has been closed"))
    assert bc.is_dead_browser_error(RuntimeError("Event loop is closed"))
    assert not bc.is_dead_browser_error(RuntimeError("net::ERR_NAME_NOT_RESOLVED"))
    assert not bc.is_dead_browser_error(TimeoutError("Timeout 30000ms exceeded"))


def test_a_dead_browser_is_relaunched_once_and_the_page_still_loads(monkeypatch):
    c = controller()
    tries, resets = [], []

    async def browse(url):
        tries.append(url)
        if len(tries) == 1:
            raise AttributeError("'NoneType' object has no attribute 'send'")
        return f"System Info - Contents of https://{url}:\nhello"

    async def reset():
        resets.append(1)
    monkeypatch.setattr(c, "_async_browse", browse)
    monkeypatch.setattr(c, "_reset", reset)
    out = c.browse_url("example.com")
    assert out.endswith("hello") and len(tries) == 2 and resets == [1]


def test_a_second_crash_says_use_web_search(monkeypatch):
    c = controller()

    async def browse(url):
        raise AttributeError("'NoneType' object has no attribute 'send'")

    async def reset():
        pass
    monkeypatch.setattr(c, "_async_browse", browse)
    monkeypatch.setattr(c, "_reset", reset)
    out = c.browse_url("example.com")
    assert out == "Error: Browser crashed. Please use web_search instead."
    assert out.startswith("Error")                    # counted as a failure by the harness


def test_an_ordinary_page_error_is_not_a_crash(monkeypatch):
    c = controller()
    tries = []

    async def browse(url):
        tries.append(url)
        raise RuntimeError("net::ERR_NAME_NOT_RESOLVED")
    monkeypatch.setattr(c, "_async_browse", browse)
    out = c.browse_url("nope.invalid")
    assert out.startswith("System Info: Failed to load nope.invalid") and len(tries) == 1   # no relaunch


def test_every_call_runs_on_the_same_browser_loop(monkeypatch):
    """The bug: a fresh loop per call left Playwright tied to a closed one."""
    c = controller()
    seen = []

    async def browse(url):
        seen.append((threading.current_thread().name, id(__import__("asyncio").get_running_loop())))
        return "ok"
    monkeypatch.setattr(c, "_async_browse", browse)
    c.browse_url("a.com")
    c.browse_url("b.com")
    assert seen[0] == seen[1] and seen[0][0] == "lumi-browser"
