"""The web app's layout rules: one home for connections, and a sidebar that fits the window."""

import glob
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(*parts):
    return open(os.path.join(ROOT, *parts), encoding="utf-8").read()


def test_accounts_live_on_the_integrations_page_only():
    app = read("web", "app.js")
    assert "workspace.renderAccounts($('#accounts-holder'))" in app
    for section in ('id="int-accounts"', 'id="int-apps"', 'id="int-mcp"'):
        assert section in app
    tabs = app[app.index("const SETTINGS_TABS = ["):app.index("const _SETTINGS_ALIASES")]
    assert "'connections'" not in tabs                              # no second home in Settings
    assert "return navigate('integrations')" in app                 # old links still land somewhere
    assert "renderConnectionsTab" not in read("web", "workspace.js")


def test_every_message_points_at_integrations():
    stale = ("Settings → Connections", "Settings -> Connections", "Settings, then Connections")
    for path in glob.glob(os.path.join(ROOT, "modules", "*.py")) + glob.glob(os.path.join(ROOT, "web", "*.js")):
        text = open(path, encoding="utf-8").read()
        for s in stale:
            assert s not in text, (os.path.basename(path), s)


def test_sidebar_fits_the_window():
    css = read("web", "style.css")
    block = css[css.index("Sidebar that fits the window"):]
    assert ".app > .sidebar { overflow: hidden;" in block            # the page itself never scrolls
    assert ".sidebar .nav { flex: 1 1 auto; min-height: 0; overflow-y: auto;" in block
    assert ".sidebar .sidebar-footer { flex: none;" in block          # player and you stay pinned
