"""
Read-only local deal finder.

One agent tool, ``find_local_deals(search_query, category)``. It opens a
headless Playwright browser on a fixed search page, reads the page text,
pulls out ``$N.NN`` prices with a regex and returns a Markdown table.

Safety model (each rule is enforced in code, not left to the prompt):

* Read-only. The browser never clicks, types, fills or submits anything:
  the only page calls are ``goto``, ``inner_text`` and ``screenshot``.
* Forbidden URLs. Any URL that looks like a cart, checkout, login or
  account page is refused before navigation, and every navigation the page
  itself attempts (redirects, scripts) is checked by a route handler. A hit
  aborts the request and halts the whole run.
* HITL. The tool is in CONFIRM_TOOLS, and ``run_tool`` asks
  ``ctx.confirm`` (the HITL approval server) with the exact URLs before
  Playwright is even started. Declined means nothing runs.
* Fresh context. A new, empty browser context per run: no cookies, no
  saved sign-ins, downloads refused. Bot checks (CAPTCHAs) are reported,
  never bypassed.

While a search runs, the page is screenshotted at ~5 fps and pushed to the
web client's Browser tab through ``modules.workspace.emit_browser_frame``.

Interface: the "everyday tool" module shape (see everyday_common.py), so
agent_tools.py and the prompter wire it up like the finance tools.
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
import sys
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional
from urllib.parse import quote, quote_plus, urlparse

from modules.everyday_common import ToolContext, one_line

AGENT = "system"
TOOL_NAME = "find_local_deals"
TASKS = {TOOL_NAME: "Looking for local deals"}

CATEGORIES = ("groceries", "gas", "restaurants", "retail")

PRICE_RE = re.compile(r"\$\d+\.\d{2}")

# Substrings that mark a page Lumi must never open: carts, checkouts,
# sign-ins, accounts, payments. Matched case-insensitively against the
# whole URL (path and query), so "?next=/checkout" is caught too. Letter
# boundaries keep "carton" or "author" from matching.
FORBIDDEN_URL_RE = re.compile(
    r"(?<![a-z])(?:my|shopping|add[-_]?to[-_]?)?"
    r"(?:cart|carts|basket|checkout|check[-_]out|log[-_]?in|log[-_]?on|sign[-_]?in|sign[-_]?up"
    r"|register|accounts?|o?auth|payments?|billing|wallet|purchase|order[-_]?confirm\w*)"
    r"(?![a-z])",
    re.IGNORECASE,
)

# Text that means the site put up a bot check. We stop instead of trying
# to get past it.
_BOT_CHECK_RE = re.compile(
    r"captcha|unusual traffic|are you a robot|verify you are human|access denied"
    r"|security verification|not a bot",
    re.IGNORECASE,
)

FRAME_INTERVAL_S = 0.2           # 5 fps
FRAME_QUALITY = 55               # JPEG quality, keeps frames ~40-80 KB
VIEWPORT = {"width": 1280, "height": 800}
NAV_TIMEOUT_MS = 20_000
SETTLE_S = 2.0                   # let JS-rendered results paint
MAX_PAGE_CHARS = 60_000
MAX_ROWS = 15
RUN_TIMEOUT_S = 90

POSTAL_CODE_ENV = "LUMI_DEALS_POSTAL_CODE"

_CATEGORY_TERMS = {
    "groceries": "grocery deals weekly ad",
    "gas": "gas prices near me",
    "restaurants": "restaurant deals specials",
    "retail": "sale price deal",
}


class DealFinderError(Exception):
    """Plain-words failure the model should hear about."""


class ForbiddenUrl(DealFinderError):
    """A cart / checkout / login URL came up; the run was halted."""


def is_forbidden_url(url: str) -> bool:
    return bool(FORBIDDEN_URL_RE.search(url or ""))


def _check_url(url: str) -> str:
    parsed = urlparse(url or "")
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise DealFinderError(f"Refusing non-web URL: {url!r}")
    if is_forbidden_url(url):
        raise ForbiddenUrl(f"Halted: {url} looks like a cart, checkout or sign-in page.")
    return url


def normalize_category(category: Any) -> str:
    c = str(category or "").strip().lower()
    aliases = {"grocery": "groceries", "food": "groceries", "fuel": "gas", "gasoline": "gas",
               "restaurant": "restaurants", "dining": "restaurants", "shopping": "retail",
               "store": "retail", "stores": "retail"}
    c = aliases.get(c, c)
    if c not in CATEGORIES:
        raise DealFinderError(f"Unknown category {category!r}; use one of: {', '.join(CATEGORIES)}.")
    return c


def source_urls(query: str, category: str, postal_code: Optional[str] = None) -> List[str]:
    """Search pages to try, in order. Fixed hosts; only the query varies."""
    if is_forbidden_url(query):
        raise ForbiddenUrl("Halted: the search words name a cart, checkout or sign-in page; "
                           "this tool only reads public prices.")
    urls = []
    if postal_code and category in ("groceries", "retail"):
        urls.append(f"https://flipp.com/en-us/search/{quote(query)}?postal_code={quote_plus(postal_code)}")
    terms = f"{query} {_CATEGORY_TERMS[category]}"
    if postal_code:
        terms += f" {postal_code}"
    urls.append(f"https://html.duckduckgo.com/html/?q={quote_plus(terms)}")
    return [_check_url(u) for u in urls]


# ─── extraction ──────────────────────────────────────────────────────

def _price_value(p: str) -> float:
    return float(p.lstrip("$"))


def extract_deals(text: str, source: str, limit: int = MAX_ROWS) -> List[Dict[str, Any]]:
    """Lines with a ``$N.NN`` price -> rows. When a price sits alone on its
    line (common on listing pages), the nearest text line above names it."""
    rows, seen = [], set()
    last_text = ""
    for raw in (text or "").splitlines():
        line = " ".join(raw.split())
        if not line:
            continue
        prices = PRICE_RE.findall(line)
        if not prices:
            if len(line) >= 3:
                last_text = line
            continue
        item = PRICE_RE.sub("", line).strip(" -–:|•·,")
        if len(item) < 3:
            item = last_text
        item = one_line(item, 90)
        if not item:
            continue
        price = prices[0]
        key = (item.lower(), price)
        if key in seen:
            continue
        seen.add(key)
        rows.append({"item": item, "price": price, "value": _price_value(price), "source": source})
    rows.sort(key=lambda r: r["value"])
    return rows[:limit]


def _cell(text: Any, limit: int = 90) -> str:
    return one_line(text, limit).replace("|", "/") or "-"


def format_table(rows: List[Dict[str, Any]], query: str, category: str) -> str:
    if not rows:
        return f"No prices found for '{query}' ({category})."
    lines = [f"Deals for '{query}' ({category}), cheapest first:", "",
             "| Item | Price | Source |", "|---|---|---|"]
    for r in rows:
        lines.append(f"| {_cell(r['item'])} | {r['price']} | {_cell(urlparse(r['source']).hostname, 40)} |")
    lines.append("")
    lines.append("Prices were read from search pages and may be stale; check the store before buying.")
    return "\n".join(lines)


# ─── the browser ─────────────────────────────────────────────────────

FrameSink = Callable[[str, str], None]          # (base64 jpeg, current url)


@dataclass
class SearchResult:
    rows: List[Dict[str, Any]]
    visited: List[str]
    notes: List[str]


class DealFinder:
    """Headless, read-only Playwright session for one search."""

    def __init__(self, on_frame: Optional[FrameSink] = None,
                 on_status: Optional[Callable[[str], None]] = None,
                 postal_code: Optional[str] = None):
        self.on_frame = on_frame
        self.on_status = on_status or (lambda msg: None)
        self.postal_code = postal_code if postal_code is not None else os.environ.get(POSTAL_CODE_ENV, "").strip() or None
        self._halted: Optional[str] = None

    def plan(self, query: str, category: str) -> List[str]:
        return source_urls(query, normalize_category(category), self.postal_code)

    async def search(self, query: str, category: str) -> SearchResult:
        query = " ".join(str(query or "").split())[:120]
        if not query:
            raise DealFinderError("No search query given.")
        category = normalize_category(category)
        urls = source_urls(query, category, self.postal_code)

        from playwright.async_api import async_playwright

        rows: List[Dict[str, Any]] = []
        visited: List[str] = []
        notes: List[str] = []
        async with async_playwright() as pw:
            browser = await self._launch(pw)
            try:
                context = await browser.new_context(
                    viewport=VIEWPORT, accept_downloads=False, java_script_enabled=True,
                    service_workers="block",
                )
                await context.route("**/*", self._guard_route)
                page = await context.new_page()
                page.on("popup", lambda p: asyncio.ensure_future(p.close()))
                page.on("dialog", lambda d: asyncio.ensure_future(d.dismiss()))
                streamer = asyncio.create_task(self._stream_frames(page))
                try:
                    for url in urls:
                        found = await self._read_one(page, url, visited, notes)
                        rows.extend(found)
                        if self._halted:
                            break
                        if found:
                            break
                finally:
                    streamer.cancel()
                    try:
                        await streamer
                    except (asyncio.CancelledError, Exception):
                        pass
                await context.close()
            finally:
                await browser.close()

        if self._halted:
            raise ForbiddenUrl(self._halted)
        rows.sort(key=lambda r: r["value"])
        return SearchResult(rows=rows[:MAX_ROWS], visited=visited, notes=notes)

    async def _launch(self, pw):
        try:
            return await pw.chromium.launch(headless=True)
        except Exception:
            return await pw.firefox.launch(headless=True)

    async def _guard_route(self, route, request):
        """Every request passes here. A navigation to a forbidden URL is
        aborted and halts the run; so is anything after a halt."""
        if self._halted:
            await route.abort()
            return
        if request.is_navigation_request() and is_forbidden_url(request.url):
            self._halted = f"Halted: the page tried to open {request.url}, which looks like a cart, checkout or sign-in page."
            self.on_status(self._halted)
            await route.abort()
            return
        if request.is_navigation_request() and request.method != "GET":
            self._halted = "Halted: the page tried to submit a form; this tool is read-only."
            self.on_status(self._halted)
            await route.abort()
            return
        await route.continue_()

    async def _read_one(self, page, url: str, visited: List[str], notes: List[str]) -> List[Dict[str, Any]]:
        _check_url(url)
        self.on_status(f"Opening {url}")
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        except Exception as e:
            if self._halted:
                return []
            notes.append(f"{urlparse(url).hostname}: {type(e).__name__}")
            return []
        await asyncio.sleep(SETTLE_S)
        if self._halted:
            return []
        if is_forbidden_url(page.url):
            self._halted = f"Halted: ended up on {page.url}, which looks like a cart, checkout or sign-in page."
            return []
        visited.append(page.url)
        try:
            text = (await page.inner_text("body", timeout=5_000))[:MAX_PAGE_CHARS]
        except Exception as e:
            notes.append(f"{urlparse(url).hostname}: couldn't read the page ({type(e).__name__})")
            return []
        if _BOT_CHECK_RE.search(text[:3000]) and not PRICE_RE.search(text):
            notes.append(f"{urlparse(url).hostname} showed a bot check; not bypassing it.")
            return []
        if len(text.strip()) < 400 and not PRICE_RE.search(text):
            notes.append(f"{urlparse(url).hostname} returned an almost empty page "
                         "(it likely blocks automated browsers).")
            return []
        return extract_deals(text, page.url)

    async def _stream_frames(self, page) -> None:
        if self.on_frame is None:
            return
        while True:
            started = time.monotonic()
            try:
                shot = await page.screenshot(type="jpeg", quality=FRAME_QUALITY, timeout=2_000)
                self.on_frame(base64.b64encode(shot).decode("ascii"), page.url)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass                                  # mid-navigation; next tick
            await asyncio.sleep(max(0.0, FRAME_INTERVAL_S - (time.monotonic() - started)))


def _run_in_own_loop(coro_factory: Callable[[], Awaitable[Any]]) -> Any:
    """Playwright needs subprocess support (a Proactor loop on Windows), and
    the caller's loop may be a selector loop. Run on a private loop in a
    worker thread instead."""
    if sys.platform == "win32":
        loop = asyncio.ProactorEventLoop()
    else:
        loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(asyncio.wait_for(coro_factory(), RUN_TIMEOUT_S))
    finally:
        loop.close()


# ─── everyday-tool interface ─────────────────────────────────────────

TOOL_SCHEMAS = [{
    "type": "function",
    "function": {
        "name": TOOL_NAME,
        "description": "Read-only web search for local prices on groceries, gas, restaurants or retail, returned as a price table (needs the user's approval).",
        "parameters": {
            "type": "object",
            "properties": {
                "search_query": {"type": "string", "description": "What to look for, e.g. 'eggs' or 'pizza'."},
                "category": {"type": "string", "enum": list(CATEGORIES),
                             "description": "One of groceries, gas, restaurants, retail."},
            },
            "required": ["search_query", "category"],
        },
    },
}]

CONFIRM_TOOLS = {TOOL_NAME}


def available(ctx: ToolContext) -> bool:
    try:
        import playwright.async_api  # noqa: F401
    except Exception:
        return False
    return True


def _workspace():
    try:
        from modules import workspace
        return workspace
    except Exception:
        return None


async def find_local_deals(ctx: ToolContext, search_query: Any, category: Any) -> str:
    query = " ".join(str(search_query or "").split())[:120]
    if not query:
        return "Error: no search query given."
    ws = _workspace()
    finder = DealFinder(
        on_frame=ws.emit_browser_frame if ws else None,
        on_status=(lambda msg: (ws.emit_browser_status(msg) if ws else None, ctx.log(msg))),
    )
    try:
        cat = normalize_category(category)
        urls = finder.plan(query, cat)
    except DealFinderError as e:
        return f"Error: {e}"

    preview = (
        f"Find local deals: '{query}' ({cat})\n\n"
        "Lumi will open a headless browser and READ these pages (no clicks, no sign-in, "
        "no cart; it halts on any cart/checkout/login URL):\n"
        + "\n".join(f"  - {u}" for u in urls)
    )
    if not await ctx.confirm(TOOL_NAME, preview):
        return "The user declined to allow this action."

    if ws:
        ws.emit_browser_status(f"Searching deals for '{query}' ({cat})", active=True)
    try:
        result = await asyncio.to_thread(_run_in_own_loop, lambda: finder.search(query, cat))
    except ForbiddenUrl as e:
        return f"Error: {e}"
    except DealFinderError as e:
        return f"Error: {e}"
    except asyncio.TimeoutError:
        return f"Error: the deal search took longer than {RUN_TIMEOUT_S}s and was stopped."
    except Exception as e:
        return f"Error: the browser failed ({type(e).__name__}: {one_line(str(e), 160)})."
    finally:
        if ws:
            ws.emit_browser_status("Browser idle", active=False)

    out = format_table(result.rows, query, cat)
    if result.notes:
        out += "\n\nNotes: " + "; ".join(result.notes)
    return out


async def run_tool(name: str, args: Dict[str, Any], ctx: ToolContext) -> str:
    if name == TOOL_NAME:
        return await find_local_deals(ctx, args.get("search_query") or args.get("query"), args.get("category"))
    return f"Unknown deal-finder tool: {name}"
