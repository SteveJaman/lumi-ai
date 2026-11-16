"""Lumi's pixel office: the room data is consistent, her sprite sheets are in the
4x3 walk layout pipeline.js expects (scripts/lumi_pixel_art.py, scripts/generate_sprites.py),
and both front ends ship the same files."""

import filecmp
import json
import os
from collections import deque

import sys

import pytest
from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "web", "pixel-office")
ISLAND = os.path.join(ROOT, "island", "src", "pixel-office")
AGENTS = ("main", "minecraft", "office", "system")
sys.path.insert(0, os.path.join(ROOT, "scripts"))
CW, CH = 40, 56


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


def _cell(im, c, r):
    return im.crop((c * CW, r * CH, c * CW + CW, r * CH + CH))


def test_sheets_are_4x3_walk_cycles_with_feet_on_the_bottom_row():
    for a in AGENTS:
        im = Image.open(os.path.join(WEB, "chars", f"{a}-sheet.png"))
        assert im.mode == "RGBA" and im.size == (CW * 4, CH * 3), a
        for r in range(3):
            cells = [_cell(im, c, r) for c in range(4)]
            for c, cell in enumerate(cells):
                box = cell.getbbox()
                assert box and box[3] == CH, (a, r, c, box)          # feet on the last row: no floating
                assert 20 <= box[2] - box[0] <= CW, (a, r, c, box)   # about one 32px tile wide
            assert cells[0].tobytes() != cells[1].tobytes() != cells[2].tobytes(), (a, r)   # she walks


def test_the_pixel_art_script_reproduces_the_shipped_sheets():
    import lumi_pixel_art as art
    art._check()
    for a in AGENTS:
        shipped = Image.open(os.path.join(WEB, "chars", f"{a}-sheet.png")).convert("RGBA")
        assert art.sheet(a).tobytes() == shipped.tobytes(), a


def test_each_agent_wears_a_different_outfit():
    sheets = [Image.open(os.path.join(WEB, "chars", f"{a}-sheet.png")).tobytes() for a in AGENTS]
    assert len(set(sheets)) == len(AGENTS)


def test_the_slicer_keeps_her_white_jacket_and_finds_a_rough_grid():
    import random
    import generate_sprites as gs
    random.seed(3)
    im = Image.new("RGB", (1792, 1024), (251, 251, 249))
    d = ImageDraw.Draw(im)
    for r in range(3):
        for c in range(4):
            x, y = 160 + c * 420 + random.randint(-30, 30), 50 + r * 330 + random.randint(-15, 15)
            d.ellipse((x + 40, y, x + 160, y + 20), outline=(0, 0, 0), fill=(80, 240, 240), width=4)   # halo, apart
            d.rectangle((x + 50, y + 30, x + 150, y + 120), outline=(0, 0, 0), fill=(200, 205, 215), width=6)
            d.rectangle((x + 40, y + 120, x + 160, y + 230), outline=(0, 0, 0), fill=(255, 255, 255), width=6)
            d.rectangle((x + 55 + c * 5, y + 230, x + 90, y + 280), outline=(0, 0, 0), fill=(255, 255, 255), width=6)
            d.rectangle((x + 110, y + 230, x + 145, y + 280), outline=(0, 0, 0), fill=(255, 255, 255), width=6)
    sheet = gs.slice_sheet(im.convert("RGBA"))
    assert sheet.size == (gs.CELL_W * 4, gs.CELL_H * 3)
    for r in range(3):
        for c in range(4):
            cell = sheet.crop((c * gs.CELL_W, r * gs.CELL_H, (c + 1) * gs.CELL_W, (r + 1) * gs.CELL_H))
            box = cell.getbbox()
            assert box and box[3] == gs.CELL_H, (r, c, box)
            px = list(cell.getdata())
            white = sum(1 for p in px if p[3] and min(p[:3]) > 230)
            assert white > 40, (r, c, white)                         # inside the outline: kept opaque
            assert all(p[3] in (0, 255) for p in px)                 # crisp alpha


def test_the_prompts_describe_her():
    import generate_sprites as gs
    p = gs.prompt_for("main")
    for words in ("split silver and teal hair", "white cat ears", "cyan halo", "white jacket, mint tie",
                  "3 rows (facing down, up, and right)", "4 columns", "Solid white background"):
        assert words in p


def test_web_and_island_ship_the_same_files():
    for rel in ["floor.png", "atlas.png", "office.js", "bubble-permission.png", "bubble-waiting.png",
                "lumi-sprites.js", *[f"chars/{a}-sheet.png" for a in AGENTS]]:
        assert filecmp.cmp(os.path.join(WEB, rel), os.path.join(ISLAND, rel), shallow=False), rel
    assert filecmp.cmp(os.path.join(ROOT, "web", "pipeline.js"), os.path.join(ROOT, "island", "src", "pipeline.js"),
                       shallow=False)


def test_the_old_procedural_art_is_gone():
    assert not os.path.exists(os.path.join(ROOT, "scripts", "build_pixel_office.py"))
    assert not os.path.exists(os.path.join(ROOT, "scripts", "lumi_hd.py"))
    for d in (WEB, ISLAND):
        for a in AGENTS:
            assert not os.path.exists(os.path.join(d, "chars", f"{a}.png"))
            assert not os.path.exists(os.path.join(d, "chars", f"{a}_blink.png"))


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


def test_the_manifest_and_the_renderer_use_the_sheets():
    import re
    src = open(os.path.join(WEB, "lumi-sprites.js"), encoding="utf-8").read()
    agents = re.search(r"const AGENTS = \[(.*?)\]", src).group(1)
    poses = re.findall(r"'(\w+)'", re.search(r"const POSES = \[(.*?)\]", src).group(1))
    assert re.findall(r"'(\w+)'", agents) == list(AGENTS)
    assert {"idle", "walk", "sit", "type", "read", "stretch", "drink", "eat", "sleep"} <= set(poses)
    assert "walk: [0, 1, 2, 3]" in src and "down: 0, up: 1, right: 2, left: 2" in src
    pl = open(os.path.join(ROOT, "web", "pipeline.js"), encoding="utf-8").read()
    assert "chars/${a}-sheet.png" in pl
    assert "ctx.imageSmoothingEnabled = false" in pl
    assert "top = fy - CH + bob" in pl                                # bottom-centre anchor


def test_no_vtube_references_in_the_pipeline():
    import re
    for rel in ("web/pipeline.js", "web/pixel-office/lumi-sprites.js"):
        assert not re.search(r"vts|vtube", open(os.path.join(ROOT, rel), encoding="utf-8").read(), re.I)
