"""Lumi's pixel office (scripts/build_pixel_office.py output + pipeline.js):
the baked data is consistent and both front ends ship the same files."""

import filecmp
import json
import os
from collections import deque

import pytest
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "web", "pixel-office")
ISLAND = os.path.join(ROOT, "island", "src", "pixel-office")
AGENTS = ("main", "minecraft", "office", "system")


@pytest.fixture(scope="module")
def meta():
    src = open(os.path.join(WEB, "office.js"), encoding="utf-8").read()
    return json.loads(src[src.index("=") + 1:].strip().rstrip(";"))


def reachable(walk, a, b):
    rows, cols = len(walk), len(walk[0])
    ok = lambda c, r: 0 <= r < rows and 0 <= c < cols and (walk[r][c] == "1" or (c, r) == b)
    seen, q = {a}, deque([a])
    while q:
        c, r = q.popleft()
        if (c, r) == b:
            return True
        for dc, dr in ((0, -1), (1, 0), (0, 1), (-1, 0)):
            n = (c + dc, r + dr)
            if n not in seen and ok(*n):
                seen.add(n)
                q.append(n)
    return False


def test_every_agent_can_walk_between_its_rest_and_work_spots(meta):
    walk = meta["walk"]
    assert len(walk) == meta["rows"] and all(len(r) == meta["cols"] for r in walk)
    for a in AGENTS:
        st = meta["agents"][a]
        w, r = (st["work"]["col"], st["work"]["row"]), (st["rest"]["col"], st["rest"]["row"])
        assert walk[w[1]][w[0]] == "1" and walk[r[1]][r[0]] == "1", a
        assert reachable(walk, r, w), a
        assert st["work"]["face"] in ("up", "down", "left", "right")


def test_drawables_and_screens_point_at_real_art(meta):
    atlas = Image.open(os.path.join(WEB, "atlas.png"))
    for rx, ry, rw, rh in meta["rects"]:
        assert rx + rw <= atlas.width and ry + rh <= atlas.height
    n = len(meta["rects"])
    for d in meta["drawables"]:
        for idx in [d["a"], *d.get("frames", []), *d.get("on", []), *d.get("flicker", [])]:
            assert 0 <= idx < n
    for a in AGENTS:
        for i in meta["agents"][a]["screens"]:
            assert "on" in meta["drawables"][i], a


def test_character_sheets_are_pixel_agents_shaped():
    for a in AGENTS:
        for suffix in ("", "_blink"):
            im = Image.open(os.path.join(WEB, "chars", f"{a}{suffix}.png"))
            assert im.size == (128, 96) and im.mode == "RGBA"


def test_web_and_island_ship_the_same_files():
    for rel in ["floor.png", "atlas.png", "office.js", "bubble-permission.png", "bubble-waiting.png",
                *[f"chars/{a}{s}.png" for a in AGENTS for s in ("", "_blink")]]:
        assert filecmp.cmp(os.path.join(WEB, rel), os.path.join(ISLAND, rel), shallow=False), rel
    assert filecmp.cmp(os.path.join(ROOT, "web", "pipeline.js"), os.path.join(ROOT, "island", "src", "pipeline.js"),
                       shallow=False)


def test_credits_ship_with_the_art():
    for d in (WEB, ISLAND):
        assert "MIT" in open(os.path.join(d, "LICENSE-pixel-agents.txt"), encoding="utf-8").read()
        assert "Pixel Agents" in open(os.path.join(d, "CREDITS.md"), encoding="utf-8").read()
