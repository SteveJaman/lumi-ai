"""Read-only deal finder (modules/deal_finder.py) and the split-view
workspace backend (modules/workspace.py). No browser is launched: the
Playwright run is replaced where a test needs a result."""

import asyncio
import os
import sys
import subprocess

import pytest

from modules import agent_tools, deal_finder, workspace
from modules.deal_finder import (DealFinder, DealFinderError, ForbiddenUrl, SearchResult,
                                 extract_deals, format_table, is_forbidden_url, source_urls)
from modules.everyday_common import ToolContext


def run(coro):
    return asyncio.run(coro)


def ctx_with(approve):
    asked = []

    async def confirm(name, preview):
        asked.append((name, preview))
        return approve
    return ToolContext(confirm=confirm), asked


# ─── URL guard ───────────────────────────────────────────────────────

@pytest.mark.parametrize("url", [
    "https://shop.example.com/cart",
    "https://shop.example.com/cart?id=1",
    "https://shop.example.com/p/1/add-to-cart",
    "https://shop.example.com/addToCart?sku=1",
    "https://shop.example.com/checkout/step1",
    "https://shop.example.com/login",
    "https://shop.example.com/x?next=/checkout",
    "https://accounts.example.com/signin",
    "https://www.example.com/sign-in",
    "https://www.example.com/myaccount",
    "https://www.example.com/oauth2/authorize",
    "https://www.example.com/ShoppingCart",
])
def test_forbidden_urls_are_caught(url):
    assert is_forbidden_url(url)


@pytest.mark.parametrize("url", [
    "https://www.bing.com/search?q=egg+carton+grocery+deals&setlang=en",
    "https://www.example.com/author/jane",
    "https://flipp.com/en-us/search/milk?postal_code=30144",
    "https://www.example.com/cartoon-network",
])
def test_ordinary_urls_pass(url):
    assert not is_forbidden_url(url)


def test_source_urls_are_fixed_hosts_and_refuse_forbidden_queries():
    urls = source_urls("eggs", "groceries")
    assert len(urls) == 1 and urls[0].startswith("https://www.bing.com/search?q=eggs")
    urls = source_urls("eggs", "groceries", postal_code="30144")
    assert urls[0].startswith("https://flipp.com/") and "postal_code=30144" in urls[0]
    assert source_urls("gas", "gas", postal_code="30144")[0].startswith("https://www.bing.com/search?")
    with pytest.raises(ForbiddenUrl):
        source_urls("checkout page", "retail")


def test_unknown_category_is_rejected():
    with pytest.raises(DealFinderError):
        deal_finder.normalize_category("crypto")
    assert deal_finder.normalize_category("Grocery") == "groceries"


class _Req:
    def __init__(self, url, nav=True, method="GET"):
        self.url, self._nav, self.method = url, nav, method

    def is_navigation_request(self):
        return self._nav


class _Route:
    def __init__(self):
        self.action = None

    async def abort(self):
        self.action = "abort"

    async def continue_(self):
        self.action = "continue"


def test_route_guard_halts_on_cart_navigation_and_form_posts():
    f = DealFinder(postal_code="")
    r = _Route()
    run(f._guard_route(r, _Req("https://example.com/search?q=eggs")))
    assert r.action == "continue" and f._halted is None
    r = _Route()
    run(f._guard_route(r, _Req("https://example.com/cart")))
    assert r.action == "abort" and "cart" in f._halted
    r = _Route()                                       # everything after a halt is aborted
    run(f._guard_route(r, _Req("https://example.com/img.png", nav=False)))
    assert r.action == "abort"

    f = DealFinder(postal_code="")
    r = _Route()
    run(f._guard_route(r, _Req("https://example.com/search", method="POST")))
    assert r.action == "abort" and "form" in f._halted


def test_finder_never_clicks_or_types():
    import inspect
    src = inspect.getsource(deal_finder)
    for verb in (".click(", ".fill(", ".type(", ".press(", ".check(", ".select_option(", ".tap("):
        assert verb not in src


# ─── extraction ──────────────────────────────────────────────────────

def test_extract_deals_finds_prices_sorts_and_dedupes():
    text = "\n".join([
        "Kroger Large Eggs 12ct $3.49",
        "Publix",
        "$2.99",
        "Kroger Large Eggs 12ct $3.49",
        "Free shipping on orders over $35",   # no cents: not a $N.NN price
        "Organic eggs | 18ct — $6.19",
    ])
    rows = extract_deals(text, "https://example.com/s")
    assert [r["price"] for r in rows] == ["$2.99", "$3.49", "$6.19"]
    assert rows[0]["item"] == "Publix"
    table = format_table(rows, "eggs", "groceries")
    assert "| Item | Price | Source |" in table
    assert "Organic eggs / 18ct" in table              # pipes escaped
    assert "example.com" in table


def test_format_table_empty():
    assert "No prices found" in format_table([], "eggs", "groceries")


# ─── tool: HITL gate ─────────────────────────────────────────────────

def test_tool_is_registered_gated_and_marked_as_web():
    assert "find_local_deals" in agent_tools.EVERYDAY_TOOL_NAMES
    assert "find_local_deals" in agent_tools.CONFIRM_REQUIRED_TOOLS
    assert agent_tools.PROVENANCE_MARKERS["find_local_deals"] == "FROM WEB"
    schema = deal_finder.TOOL_SCHEMAS[0]["function"]
    assert schema["parameters"]["required"] == ["search_query", "category"]


def test_declined_approval_never_starts_the_browser(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("browser started without approval")
    monkeypatch.setattr(deal_finder, "_run_in_own_loop", boom)
    ctx, asked = ctx_with(False)
    out = run(deal_finder.run_tool("find_local_deals", {"search_query": "eggs", "category": "groceries"}, ctx))
    assert out == "The user declined to allow this action."
    assert asked and asked[0][0] == "find_local_deals"
    assert "bing.com" in asked[0][1] and "no clicks" in asked[0][1]


def test_forbidden_query_is_refused_before_asking(monkeypatch):
    ctx, asked = ctx_with(True)
    out = run(deal_finder.run_tool("find_local_deals", {"search_query": "login", "category": "retail"}, ctx))
    assert out.startswith("Error: Halted") and not asked


def test_approved_search_returns_table(monkeypatch):
    rows = extract_deals("Eggs $3.49\nMilk $2.59", "https://www.bing.com/search?q=eggs")
    monkeypatch.setattr(deal_finder, "_run_in_own_loop",
                        lambda factory: SearchResult(rows=rows, visited=[], notes=["x showed a bot check"]))
    ctx, asked = ctx_with(True)
    out = run(deal_finder.run_tool("find_local_deals", {"search_query": "milk", "category": "groceries"}, ctx))
    assert "| Milk | $2.59 |" in out and "bot check" in out and len(asked) == 1


def test_halt_during_run_is_reported(monkeypatch):
    def halted(factory):
        raise ForbiddenUrl("Halted: the page tried to open https://x.com/checkout")
    monkeypatch.setattr(deal_finder, "_run_in_own_loop", halted)
    ctx, _ = ctx_with(True)
    out = run(deal_finder.run_tool("find_local_deals", {"search_query": "tv", "category": "retail"}, ctx))
    assert out.startswith("Error: Halted")


# ─── workspace: file sandbox ─────────────────────────────────────────

@pytest.fixture
def data_root(tmp_path, monkeypatch):
    root = tmp_path / "data"
    (root / "tasks").mkdir(parents=True)
    (root / "tasks" / "lumi_tasks.json").write_text('{"tasks": []}', encoding="utf-8")
    (root / "credentials").mkdir()
    (root / "credentials" / "google_token.json").write_text("secret", encoding="utf-8")
    (root / "memories").mkdir()
    (root / "memories" / "users.db").write_bytes(b"x")
    (root / "notes.txt").write_text("hello", encoding="utf-8")
    (root / "api_token.txt").write_text("tok", encoding="utf-8")
    (root / "blob.bin").write_bytes(b"\x00\x01\x02")
    (tmp_path / "outside.txt").write_text("nope", encoding="utf-8")
    monkeypatch.setattr(workspace, "DATA_ROOT", str(root))
    return root


def test_tree_lists_data_and_hides_private_entries(data_root):
    tree = workspace.build_tree()
    names = {n["name"] for n in tree["children"]}
    assert {"tasks", "notes.txt", "blob.bin"} <= names
    assert not names & {"credentials", "memories", "api_token.txt"}
    tasks = next(n for n in tree["children"] if n["name"] == "tasks")
    assert tasks["children"][0]["path"] == "tasks/lumi_tasks.json"


@pytest.mark.parametrize("bad", [
    "../outside.txt", "tasks/../../outside.txt", "..\\outside.txt", "/etc/passwd",
    "C:/Windows/win.ini", "credentials/google_token.json", "memories/users.db",
    "api_token.txt", "", "notes.txt\x00.png",
])
def test_resolve_rejects_traversal_and_private(data_root, bad):
    with pytest.raises(ValueError):
        workspace.resolve_data_path(bad)


def test_read_file_ok_binary_refused_missing_404(data_root):
    body = workspace.read_data_file("tasks/lumi_tasks.json")
    assert body["content"] == '{"tasks": []}' and not body["truncated"]
    with pytest.raises(ValueError):
        workspace.read_data_file("blob.bin")
    with pytest.raises(FileNotFoundError):
        workspace.read_data_file("tasks/none.json")


# ─── workspace: emitters + streaming ─────────────────────────────────

def test_emitters_are_noops_without_viewers(monkeypatch):
    monkeypatch.setattr(workspace, "_server", None)
    workspace.emit_terminal("hi")
    workspace.emit_browser_frame("AAAA", "https://x")
    assert workspace._last_frame["data"] == "AAAA"


def test_run_streaming_captures_and_emits(monkeypatch):
    seen = []
    monkeypatch.setattr(workspace, "emit_terminal", lambda text, stream="stdout": seen.append((stream, text)))
    code, out, err = workspace.run_streaming(
        [sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"],
        timeout=30)
    assert code == 3 and out.strip() == "out" and err.strip() == "err"
    assert ("stdout", "out\n") in [(s, t.replace("\r", "")) for s, t in seen]
    assert any(s == "stderr" for s, _ in seen) and seen[-1] == ("info", "[exit 3]\n")


def test_run_streaming_times_out(monkeypatch):
    monkeypatch.setattr(workspace, "emit_terminal", lambda *a, **k: None)
    with pytest.raises(subprocess.TimeoutExpired):
        workspace.run_streaming([sys.executable, "-c", "import time; time.sleep(10)"], timeout=0.5)


def test_run_command_still_returns_exit_code_and_output():
    out = agent_tools.run_command("python --version")
    assert out.startswith("Exit code 0") and "Python" in out
