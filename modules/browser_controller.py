import os
import asyncio
import threading
from playwright.async_api import async_playwright

from modules.module import Module
from constants import DATA_ROOT


# Firefox persistent profile lives under data/browser_profile/ after
# the folder reorg.
BROWSER_PROFILE_DIR = os.path.join(DATA_ROOT, "browser_profile")


class BrowserController(Module):
    def __init__(self, signals, enabled=True):
        super().__init__(signals, enabled)
        self.context = None
        self.page = None
        self.playwright = None
        self.prompt_injection.text = ""

    async def _async_browse(self, url):
        """Internal async Playwright worker using a persistent Firefox user profile."""
        try:
            if not self.playwright:
                self.playwright = await async_playwright().start()

            if not self.context:
                self.context = await self.playwright.firefox.launch_persistent_context(
                    user_data_dir=BROWSER_PROFILE_DIR,
                    headless=False
                )
                pages = self.context.pages
                self.page = pages[0] if pages else await self.context.new_page()

            # Ensure she always hits the actual tier-list page if asked for trends/tier 1
            if "top-decks" in url:
                url = "https://www.masterduelmeta.com/tier-list"

            if not url.startswith("http"):
                full_url = f"https://{url}"
            else:
                full_url = url

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
        except Exception as e:
            return f"System Info: Failed to load {url} - {e}"

    def browse_url(self, url):
        """Thread-safe runner that isolates Playwright's event loop."""
        result_container = []

        def worker():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            res = loop.run_until_complete(self._async_browse(url))
            result_container.append(res)
            loop.close()

        t = threading.Thread(target=worker)
        t.start()
        t.join()

        return result_container[0] if result_container else "System Info: Browser navigation timed out."