"""
Web search for Lumi: Bing, loaded in a headless Playwright Firefox.

    search(query, max_results=6) -> [{"title", "href", "body"}]
    search_url(query) -> str        the Bing results page (deal_finder loads it itself)

Results come back in the shape the old DuckDuckGo library used, so callers
(prompter.web_search, finance_agent) only swapped the function they call.

Searches run headless in Lumi's own persistent Firefox profile
(data/browser_search_profile), on a worker thread so Playwright's event loop
never meets Lumi's. The profile keeps cookies and consent between searches,
so Bing sees a returning visitor and rarely shows a bot check. It is NOT your
personal Firefox profile: an automated browser that reads web pages never
gets your other sign-ins.

If Bing does answer with a bot check, the search fails with SearchBlocked;
Lumi never tries to get past one. open_for_user() (the !searchbrowser
command) opens the same profile in a visible window where you can complete
the check, or sign in to Bing, yourself.
Bing wraps result links in a redirect (bing.com/ck/a?...&u=a1<base64>); the
real address is decoded from it, so Lumi sees where a result goes.
"""

from __future__ import annotations

import base64
import re
import threading
from typing import Dict, List
from urllib.parse import parse_qs, urlencode, urlparse

BING = "https://www.bing.com/search"
TIMEOUT_MS = 20000
MAX_BODY_CHARS = 400

from constants import DATA_ROOT  # noqa: E402
import os  # noqa: E402

PROFILE_DIR = os.path.join(DATA_ROOT, "browser_search_profile")

_one_at_a_time = threading.Lock()                  # one Firefox per profile at a time
_user_window = {"open": False}
_BLOCKED_RE = re.compile(r"captcha|verify you are (a )?human|unusual traffic|are you a robot", re.I)


class SearchError(Exception):
    """A message safe to show the user."""


class SearchBlocked(SearchError):
    pass


def search_url(query: str) -> str:
    return BING + "?" + urlencode({"q": " ".join(str(query or "").split())[:300], "setlang": "en"})


def real_url(href: str) -> str:
    """Bing's click-tracking redirect -> the page it points at."""
    try:
        p = urlparse(href)
        if (p.hostname or "").endswith("bing.com") and p.path.startswith("/ck/"):
            u = parse_qs(p.query).get("u", [""])[0]
            if u.startswith("a1"):
                raw = u[2:]
                decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8", "replace")
                if decoded.startswith(("http://", "https://")):
                    return decoded
    except (ValueError, UnicodeError):
        pass
    return href


_EXTRACT_JS = """
() => [...document.querySelectorAll('#b_results > li.b_algo')].map(li => {
  const a = li.querySelector('h2 a');
  const body = li.querySelector('.b_caption p, p.b_lineclamp2, p.b_lineclamp3, p.b_lineclamp4, .b_algoSlug, p');
  return { title: a ? a.innerText.trim() : '', href: a ? a.href : '', body: body ? body.innerText.trim() : '' };
})
"""


def parse_results(items: List[Dict[str, str]], max_results: int) -> List[Dict[str, str]]:
    out, seen = [], set()
    for it in items or []:
        title = " ".join(str(it.get("title") or "").split())
        href = real_url(str(it.get("href") or ""))
        if not title or not href.startswith(("http://", "https://")) or href in seen:
            continue
        seen.add(href)
        out.append({"title": title[:200], "href": href,
                    "body": " ".join(str(it.get("body") or "").split())[:MAX_BODY_CHARS]})
        if len(out) >= max_results:
            break
    return out


def _launch(pw, headless: bool):
    os.makedirs(PROFILE_DIR, exist_ok=True)
    try:
        return pw.firefox.launch_persistent_context(PROFILE_DIR, headless=headless, locale="en-US")
    except Exception as e:
        raise SearchError("Firefox for Playwright isn't installed, or Lumi's search profile is in use. "
                          f"Run `playwright install firefox` in the lumi environment. ({type(e).__name__})")


def _run(query: str, max_results: int) -> List[Dict[str, str]]:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = _launch(pw, headless=True)
        try:
            page = browser.pages[0] if browser.pages else browser.new_page()
            page.goto(search_url(query), timeout=TIMEOUT_MS, wait_until="domcontentloaded")
            try:
                page.wait_for_selector("#b_results", timeout=8000)
            except Exception:
                pass
            items = page.evaluate(_EXTRACT_JS)
            results = parse_results(items, max_results)
            if not results:
                text = page.inner_text("body")[:5000] if page.query_selector("body") else ""
                if _BLOCKED_RE.search(text) or "/challenge" in page.url:
                    raise SearchBlocked("Bing asked to confirm a person is searching, so this search didn't run. "
                                        "Type !searchbrowser, complete the check in the window that opens, "
                                        "close it, and ask again.")
            return results
        finally:
            browser.close()


def search(query: str, max_results: int = 6) -> List[Dict[str, str]]:
    """Blocking. Bing results via headless Firefox; [] when nothing matched."""
    query = " ".join(str(query or "").split())
    if not query:
        return []
    box: Dict[str, object] = {}

    if _user_window["open"]:
        raise SearchError("Lumi's search browser is open for you. Close that window, then ask again.")

    def work():
        with _one_at_a_time:
            try:
                box["out"] = _run(query, max(1, min(int(max_results), 20)))
            except Exception as e:                      # handed back to the caller's thread
                box["err"] = e

    t = threading.Thread(target=work, name="lumi-web-search", daemon=True)
    t.start()
    t.join(TIMEOUT_MS / 1000 + 25)
    if t.is_alive():
        raise SearchError("The search took too long.")
    if "err" in box:
        err = box["err"]
        if isinstance(err, SearchError):
            raise err
        raise SearchError(f"The search failed ({type(err).__name__}).")
    return box.get("out") or []


def open_for_user(url: str = "https://www.bing.com/") -> str:
    """Show Lumi's search profile in a visible Firefox window for YOU to use
    (pass a bot check, sign in to Bing). Lumi searches wait until it closes.
    Returns a line for the chat; the window runs on its own thread."""
    if _user_window["open"]:
        return "Lumi's search browser is already open."
    if not _one_at_a_time.acquire(timeout=30):
        return "A search is running; try again in a moment."
    _user_window["open"] = True

    def run():
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                ctx = _launch(pw, headless=False)
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                try:
                    page.goto(url, timeout=TIMEOUT_MS)
                except Exception:
                    pass
                # Wait until you close the window (or all its tabs).
                while ctx.pages:
                    try:
                        ctx.pages[0].wait_for_event("close", timeout=0)
                    except Exception:
                        break
                try:
                    ctx.close()
                except Exception:
                    pass
        except Exception as e:
            print(f"[web_search] couldn't open the search browser: {type(e).__name__}: {e}")
        finally:
            _user_window["open"] = False
            _one_at_a_time.release()

    threading.Thread(target=run, name="lumi-search-browser", daemon=True).start()
    return ("I opened my search browser on Bing. If it asks whether you're human, answer it there; you can "
            "also sign in to Bing. Close the window when you're done and I'll search with it again.")
