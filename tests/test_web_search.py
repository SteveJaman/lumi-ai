"""Lumi's web search: Bing in her own Firefox profile (modules/web_search.py).
No network: Playwright is replaced by a fake."""

import base64

import pytest

from modules import web_search as ws


def bing_link(url):
    return "https://www.bing.com/ck/a?!&&p=abc&u=a1" + base64.urlsafe_b64encode(url.encode()).decode().rstrip("=") + "&ntb=1"


def test_bing_redirects_are_decoded_to_the_real_page():
    assert ws.real_url(bing_link("https://www.kennesaw.edu/ccse/")) == "https://www.kennesaw.edu/ccse/"
    assert ws.real_url("https://example.com/a") == "https://example.com/a"
    assert ws.real_url("https://www.bing.com/ck/a?u=a1!!!") .startswith("https://www.bing.com/")


def test_parse_keeps_web_links_dedupes_and_trims():
    items = [{"title": "  KSU  CS ", "href": bing_link("https://ksu.edu/cs"), "body": "x " * 500},
             {"title": "dupe", "href": "https://ksu.edu/cs", "body": ""},
             {"title": "", "href": "https://no-title.example", "body": ""},
             {"title": "js", "href": "javascript:alert(1)", "body": ""},
             {"title": "Two", "href": "https://two.example", "body": "b"}]
    out = ws.parse_results(items, 5)
    assert [r["title"] for r in out] == ["KSU CS", "Two"]
    assert out[0]["href"] == "https://ksu.edu/cs" and len(out[0]["body"]) <= ws.MAX_BODY_CHARS


def test_search_url_is_bing():
    assert ws.search_url("  egg   deals ") == "https://www.bing.com/search?q=egg+deals&setlang=en"


class FakePage:
    def __init__(self, items, text="", url="https://www.bing.com/search?q=x"):
        self.items, self.text, self.url = items, text, url
    def goto(self, *a, **k): pass
    def wait_for_selector(self, *a, **k): pass
    def evaluate(self, js): return self.items
    def query_selector(self, s): return True
    def inner_text(self, s): return self.text


def fake_playwright(monkeypatch, page, launched):
    class Ctx:
        pages = [page]
        def close(self): launched.append("closed")

    class PW:
        firefox = type("F", (), {"launch_persistent_context": staticmethod(
            lambda d, **k: (launched.append((d, k["headless"])), Ctx())[1])})()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    import playwright.sync_api
    monkeypatch.setattr(playwright.sync_api, "sync_playwright", lambda: PW())


def test_search_uses_lumis_own_headless_profile(monkeypatch, tmp_path):
    monkeypatch.setattr(ws, "PROFILE_DIR", str(tmp_path / "prof"))
    launched = []
    fake_playwright(monkeypatch, FakePage([{"title": "A", "href": "https://a.example", "body": "s"}]), launched)
    assert ws.search("hello world", 3) == [{"title": "A", "href": "https://a.example", "body": "s"}]
    assert launched[0] == (str(tmp_path / "prof"), True) and launched[-1] == "closed"


def test_a_bot_check_fails_the_search_and_says_what_to_do(monkeypatch, tmp_path):
    monkeypatch.setattr(ws, "PROFILE_DIR", str(tmp_path / "prof"))
    fake_playwright(monkeypatch, FakePage([], text="Please verify you are a human"), [])
    with pytest.raises(ws.SearchBlocked, match="!searchbrowser"):
        ws.search("anything")
    fake_playwright(monkeypatch, FakePage([], text="No results for that"), [])
    assert ws.search("zxqv nothing") == []


def test_searches_wait_while_your_window_is_open(monkeypatch):
    monkeypatch.setitem(ws._user_window, "open", True)
    with pytest.raises(ws.SearchError, match="Close that window"):
        ws.search("x")
    assert ws.open_for_user() == "Lumi's search browser is already open."


def test_no_duckduckgo_left_in_lumis_code():
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1] / "modules"
    hits = [p.name for p in root.glob("*.py") if "duckduckgo.com" in p.read_text(encoding="utf-8", errors="ignore")
            or "from ddgs import" in p.read_text(encoding="utf-8", errors="ignore")]
    assert hits == []
