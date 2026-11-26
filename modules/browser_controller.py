import os
import asyncio
import threading
from playwright.async_api import async_playwright

from modules.module import Module
from constants import DATA_ROOT


# Firefox persistent profile lives under data/browser_profile/ after
# the folder reorg.
BROWSER_PROFILE_DIR = os.path.join(DATA_ROOT, "browser_profile")

BROWSE_TIMEOUT_S = 90            # a whole browse_url call, launch included
CRASHED = "Browser crashed. Please use web_search instead."

# Errors that mean the browser (or Playwright's connection to it) is gone, not
# that the page was bad: the page object is still there but its pipe is dead.
_DEAD_BROWSER = ("'NoneType' object has no attribute 'send'", "Target page, context or browser has been closed",
                 "Target closed", "Browser has been closed", "Connection closed", "has been disconnected",
                 "Event loop is closed", "browser has disconnected")


def is_dead_browser_error(err) -> bool:
    text = f"{type(err).__name__}: {err}"
    return isinstance(err, AttributeError) and "NoneType" in text or any(s in text for s in _DEAD_BROWSER)


class BrowserController(Module):
    """Lumi's Firefox (a persistent profile, so her sign-ins stay).

    Playwright objects belong to the event loop that created them. They used
    to be created on a fresh loop per call and kept after that loop closed, so
    the next call failed with "'NoneType' object has no attribute 'send'". Now
    one loop runs on its own thread for the life of the browser, and a dead
    browser is relaunched once before giving up."""

    def __init__(self, signals, enabled=True):
        super().__init__(signals, enabled)
        self.context = None
        self.page = None
        self.playwright = None
        self.prompt_injection.text = ""
        self._loop = None
        self._loop_thread = None
        self._loop_lock = threading.Lock()

    # ── the browser's own event loop ───────────────────────────────
    def _ensure_loop(self):
        with self._loop_lock:
            if self._loop is not None and self._loop_thread is not None and self._loop_thread.is_alive():
                return self._loop
            loop = asyncio.new_event_loop()
            ready = threading.Event()

            def run():
                asyncio.set_event_loop(loop)
                ready.set()
                loop.run_forever()

            t = threading.Thread(target=run, name="lumi-browser", daemon=True)
            t.start()
            ready.wait(5)
            self._loop, self._loop_thread = loop, t
            return loop

    def _run(self, coro, timeout=BROWSE_TIMEOUT_S):
        fut = asyncio.run_coroutine_threadsafe(coro, self._ensure_loop())
        return fut.result(timeout)

    # ── Playwright ─────────────────────────────────────────────────
    async def _open(self):
        if not self.playwright:
            self.playwright = await async_playwright().start()
        if not self.context:
            self.context = await self.playwright.firefox.launch_persistent_context(
                user_data_dir=BROWSER_PROFILE_DIR,
                headless=False
            )
            pages = self.context.pages
            self.page = pages[0] if pages else await self.context.new_page()
        if self.page is None or self.page.is_closed():
            self.page = await self.context.new_page()

    async def _reset(self):
        """Forget a dead browser so the next _open launches a fresh one."""
        for closer in (lambda: self.context.close() if self.context else None,
                       lambda: self.playwright.stop() if self.playwright else None):
            try:
                result = closer()
                if asyncio.iscoroutine(result):
                    await asyncio.wait_for(result, 10)
            except Exception:
                pass
        self.context = self.page = self.playwright = None

    async def _async_browse(self, url):
        """Load a page and return its text. Raises on failure."""
        await self._open()

        # Ensure she always hits the actual tier-list page if asked for trends/tier 1
        if "top-decks" in url:
            url = "https://www.masterduelmeta.com/tier-list"

        full_url = url if url.startswith("http") else f"https://{url}"

        print(f"\n\033[93m[Browser - Firefox Profile] Lumi navigating to: {full_url}\033[0m")
        await self.page.goto(full_url, timeout=30000)

        try:
            await self.page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass

        await asyncio.sleep(4)

        # Target the specific tier containers and deck card titles on Master Duel Meta
        page_text = await self.page.evaluate("""
            () => {
                const tierSections = Array.from(document.querySelectorAll('h2, h3, [class*="tier"], [class*="deck"], [class*="power"]'));
                if (tierSections.length > 0) {
                    return tierSections
                        .map(el => el.innerText.trim())
                        .filter(text => text.length > 0 && text.length < 80)
                        .join('\\n');
                }
                return document.body.innerText;
            }
        """)

        if len(page_text) > 4000:
            page_text = page_text[:4000] + "\n... [Page content truncated due to length]"

        return f"System Info - Contents of {full_url}:\n{page_text}"

    async def _browse_with_restart(self, url):
        try:
            return await self._async_browse(url)
        except Exception as e:
            if not is_dead_browser_error(e):
                return f"System Info: Failed to load {url} - {e}"
            print(f"\033[93m[Browser] the browser connection died ({e}); relaunching once\033[0m")
            await self._reset()
        try:
            return await self._async_browse(url)
        except Exception as e:
            print(f"\033[93m[Browser] relaunch failed too ({e})\033[0m")
            await self._reset()
            return f"Error: {CRASHED}"

    def browse_url(self, url):
        """Thread-safe: runs on the browser's own event loop and waits for it."""
        try:
            return self._run(self._browse_with_restart(url))
        except TimeoutError:
            return "System Info: Browser navigation timed out."
        except Exception as e:                       # the loop itself is broken: start over next time
            print(f"\033[93m[Browser] {type(e).__name__}: {e}\033[0m")
            with self._loop_lock:
                self._loop = None
            self.context = self.page = self.playwright = None
            return f"Error: {CRASHED}"
