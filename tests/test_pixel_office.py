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


def test_character_sheets_are_48x96_lumis(meta):
    assert meta["char"]["w"] == 48 and meta["char"]["h"] == 96
    for a in AGENTS:
        for suffix in ("", "_blink"):
            im = Image.open(os.path.join(WEB, "chars", f"{a}{suffix}.png"))
            assert im.size == (48 * 16, 96 * 3) and im.mode == "RGBA"


def test_every_activity_frame_is_drawn():
    """Stretch, coffee, rice ball and asleep are real frames, different from the poses
    they start from (seated / standing), in every direction."""
    for a in AGENTS:
        im = Image.open(os.path.join(WEB, "chars", f"{a}.png")).convert("RGBA")
        cell = lambda c, r: im.crop((c * 48, r * 96, c * 48 + 48, r * 96 + 96)).tobytes()
        for r in range(3):
            assert cell(8, r) != cell(1, r), (a, "stretch", r)
            for col in (14, 15):
                assert cell(col, r) != cell(7, r), (a, "asleep", r)
            if r != 1:                                   # props show unless she faces away
                for col in (10, 11, 12, 13):
                    assert cell(col, r) != cell(7, r), (a, col, r)


def test_the_mug_and_the_rice_ball_are_big_enough_to_see():
    """At least ~60 pixels of teal mug / white rice ball differ from the seated frame."""
    for a in AGENTS:
        im = Image.open(os.path.join(WEB, "chars", f"{a}.png")).convert("RGBA")
        seat = im.crop((7 * 48, 0, 8 * 48, 96)).load()
        for col in (10, 12):
            f = im.crop((col * 48, 0, col * 48 + 48, 96)).load()
            diff = sum(1 for y in range(96) for x in range(48) if f[x, y] != seat[x, y])
            assert diff >= 60, (a, col, diff)


def test_web_and_island_ship_the_same_files():
    for rel in ["floor.png", "atlas.png", "office.js", "bubble-permission.png", "bubble-waiting.png",
                "lumi-sprites.js", "chars/custom/README.md",
                *[f"chars/{a}{s}.png" for a in AGENTS for s in ("", "_blink")]]:
        assert filecmp.cmp(os.path.join(WEB, rel), os.path.join(ISLAND, rel), shallow=False), rel
    assert filecmp.cmp(os.path.join(ROOT, "web", "pipeline.js"), os.path.join(ROOT, "island", "src", "pipeline.js"),
                       shallow=False)


def test_credits_ship_with_the_art():
    for d in (WEB, ISLAND):
        assert "MIT" in open(os.path.join(d, "LICENSE-pixel-agents.txt"), encoding="utf-8").read()
        assert "Pixel Agents" in open(os.path.join(d, "CREDITS.md"), encoding="utf-8").read()


# ── Lumi's sprite manifest and the ambient layer ──────────────────

def test_the_four_agents_are_main_minecraft_office_system(meta):
    assert set(meta["agents"]) == set(AGENTS)
    for d in (WEB, ISLAND):
        assert not os.path.exists(os.path.join(d, "chars", "planner.png"))


def test_ambient_spots_are_walkable_and_reachable(meta):
    walk = meta["walk"]
    kinds = meta["spots"]
    assert set(kinds) >= {"window", "coffee", "rack", "catbed"}
    for kind, spots in kinds.items():
        for s in spots:
            assert walk[s["row"]][s["col"]] == "1", (kind, s)
            for a in AGENTS:
                rest = meta["agents"][a]["rest"]
                assert reachable(walk, (rest["col"], rest["row"]), (s["col"], s["row"])), (a, kind)


def test_the_manifest_names_every_pose_of_every_agent():
    import re
    src = open(os.path.join(WEB, "lumi-sprites.js"), encoding="utf-8").read()
    agents = re.search(r"const AGENTS = \[(.*?)\]", src).group(1)
    poses = re.search(r"const POSES = \[(.*?)\]", src).group(1)
    assert re.findall(r"'(\w+)'", agents) == list(AGENTS)
    pose_list = re.findall(r"'(\w+)'", poses)
    assert {"idle", "walk", "sit", "type", "read", "stretch", "drink", "eat", "sleep"} <= set(pose_list)
    readme = open(os.path.join(WEB, "chars", "custom", "README.md"), encoding="utf-8").read()
    for a in AGENTS:                                   # the README lists every file a user can drop in
        for pz in pose_list:
            assert f"lumi-{a}-{pz}.png" in readme


def test_no_vtube_references_in_the_pipeline():
    import re
    for rel in ("web/pipeline.js", "web/pixel-office/lumi-sprites.js"):
        assert not re.search(r"vts|vtube", open(os.path.join(ROOT, rel), encoding="utf-8").read(), re.I)
