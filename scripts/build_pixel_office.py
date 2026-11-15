"""
Bake Lumi's pixel office for the Pipeline view (web/pipeline.js, island/src/pipeline.js).

Built on Pixel Agents (MIT, https://github.com/pixel-agents-hq/pixel-agents, cloned
into third_party/pixel-agents; its character art is based on JIK-A-4's Metro City
pack): its character bodies, eyes and animation poses, wall and floor tiles, and
some furniture. On top of that, in Lumi's own design
(assets/branding/lumi_png_samples/Lumi_Character_Sheet.png):

  * four Lumi variants, one per agent: split silver/teal hair, white cat ears with
    pink insides, a floating halo, white jacket or shirt with a tie, black shorts,
    thigh-high socks, black boots with mint soles; each agent's colour as the
    accent (tie, halo, hoodie)
  * her cyberpunk room: dark navy floor with a neon grid and light pools, violet
    walls with neon trim, a night-city window, a "LUMI" neon sign, server racks,
    holo monitors, a neon rug lounge, her lilies (Allium triquetrum) and a cat bed

Outputs (written to web/pixel-office/ and island/src/pixel-office/):
    floor.png      the floor plan (floors, trim, rug, light pools)
    atlas.png      every wall piece and furniture sprite, animation frames included
    office.js      window.LUMI_OFFICE = { drawables, walk grid, seats, agent stations,
                   animation timing } (a script, not JSON: the island's CSP blocks fetch)
    chars/<agent>.png, <agent>_blink.png   128x96 sheets: 8 frames (walk1-3, type1-2,
                   read1-2, seated) x 3 rows (down, up, right; left = mirrored right)
    bubble-permission.png, bubble-waiting.png, LICENSE-pixel-agents.txt, CREDITS.md

Nothing from the clone is executed: the bits of its renderer that turn data into
pixels (furniture z-order and mirroring, wall auto-tiling, colorize, the character
layer stack and tint) are re-implemented here.

Usage:  python scripts/build_pixel_office.py
"""

from __future__ import annotations

import json
import math
import os
import random
import shutil
import sys
from typing import Dict, List, Optional, Tuple

from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PA = os.path.join(ROOT, "third_party", "pixel-agents")
ASSETS = os.path.join(PA, "webview-ui", "public", "assets")
ART = os.path.join(PA, "webview-ui", "src", "characters", "art")
OUTS = [os.path.join(ROOT, "web", "pixel-office"), os.path.join(ROOT, "island", "src", "pixel-office")]

TILE = 16
WALL, VOID = 0, 255
ALPHA_MIN = 2
SHEET_W, SHEET_H, FRAME_W, FRAME_H = 128, 96, 16, 32


def hexrgb(h: str) -> Tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


# Lumi's palette (character sheet swatches) + cyberpunk neons.
P = {
    "mint": hexrgb("8fd8cf"), "cyan": hexrgb("c6f6f8"), "blush": hexrgb("ead9d2"), "lav": hexrgb("d5d6e0"),
    "teal_d": hexrgb("2b6f6c"), "teal_m": hexrgb("4fb3a8"), "teal_l": hexrgb("8fd8cf"), "teal_h": hexrgb("c6f6f8"),
    "silv_d": hexrgb("7d8196"), "silv_m": hexrgb("b9bccb"), "silv_l": hexrgb("dfe1ea"), "silv_h": hexrgb("f7f8fc"),
    "pink": hexrgb("f4a7bd"), "pink_d": hexrgb("c9708c"),
    "neon_c": hexrgb("5cf2d2"), "neon_p": hexrgb("ff4fd8"), "neon_v": hexrgb("9b6bff"), "neon_y": hexrgb("ffd23f"),
    "ink": hexrgb("0c0e18"), "navy": hexrgb("141a2c"), "navy2": hexrgb("1b2238"), "steel": hexrgb("2b3150"),
    "steel2": hexrgb("3a4268"), "white": hexrgb("f4f6fb"), "black": hexrgb("16161c"),
}
ACCENTS = {"main": "5cf2d2", "minecraft": "5bd16a", "office": "b48cff", "system": "ff5a7a"}


# ─── colour maths (office/colorize.ts, characters/color.ts) ───────────

def lum(r, g, b):
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255


def rgb_to_hsl(r, g, b):
    rf, gf, bf = r / 255, g / 255, b / 255
    mx, mn = max(rf, gf, bf), min(rf, gf, bf)
    l = (mx + mn) / 2
    if mx == mn:
        return 0.0, 0.0, l
    d = mx - mn
    s = d / (2 - mx - mn) if l > 0.5 else d / (mx + mn)
    if mx == rf:
        h = ((gf - bf) / d + (6 if gf < bf else 0)) * 60
    elif mx == gf:
        h = ((bf - rf) / d + 2) * 60
    else:
        h = ((rf - gf) / d + 4) * 60
    return h, s, l


def hsl_deg_to_rgb(h, s, l):
    c = (1 - abs(2 * l - 1)) * s
    hp = (h % 360) / 60
    x = c * (1 - abs((hp % 2) - 1))
    r1, g1, b1 = ((c, x, 0) if hp < 1 else (x, c, 0) if hp < 2 else (0, c, x) if hp < 3 else
                  (0, x, c) if hp < 4 else (x, 0, c) if hp < 5 else (c, 0, x))
    m = l - c / 2
    return tuple(max(0, min(255, round((v + m) * 255))) for v in (r1, g1, b1))


def hsl01_to_rgb(h, s, l):
    if s == 0:
        v = round(l * 255)
        return v, v, v
    q = l * (1 + s) if l < 0.5 else l + s - l * s
    p = 2 * l - q

    def f(t):
        t = (t + 1) % 1
        if t < 1 / 6:
            return p + (q - p) * 6 * t
        if t < 1 / 2:
            return q
        if t < 2 / 3:
            return p + (q - p) * (2 / 3 - t) * 6
        return p
    return round(f(h + 1 / 3) * 255), round(f(h) * 255), round(f(h - 1 / 3) * 255)


def colorize(img: Image.Image, h, s, b=0, c=0) -> Image.Image:
    """Photoshop-style colorize (office/colorize.ts colorizeSprite)."""
    out = img.convert("RGBA").copy()
    px = out.load()
    for y in range(out.height):
        for x in range(out.width):
            r, g, bb, a = px[x, y]
            if a < ALPHA_MIN:
                px[x, y] = (0, 0, 0, 0)
                continue
            light = lum(r, g, bb)
            if c:
                light = 0.5 + (light - 0.5) * (100 + c) / 100
            light = max(0.0, min(1.0, light + b / 200))
            px[x, y] = (*hsl_deg_to_rgb(h, s / 100, light), a)
    return out


def tint_pixels(img: Image.Image, cv: dict, swaps: Optional[dict]) -> Image.Image:
    """characters/color.ts tintPixels."""
    swap = (swaps or {}).get(cv["palette"]) if cv.get("palette") else None
    swap = {k.lower(): hexrgb(v) for k, v in swap.items()} if swap else None
    shifts = cv.get("colorize") or cv.get("h") or cv.get("s") or cv.get("b") or cv.get("c")
    factor = (100 + cv.get("c", 0)) / 100
    out = Image.new("RGBA", img.size)
    src, dst = img.load(), out.load()
    for y in range(img.height):
        for x in range(img.width):
            r0, g0, b0, a = src[x, y]
            if a < ALPHA_MIN:
                continue
            if swap:
                hit = swap.get(f"#{r0:02x}{g0:02x}{b0:02x}")
                if hit:
                    r0, g0, b0 = hit
            if not shifts:
                dst[x, y] = (r0, g0, b0, a)
                continue
            if cv.get("colorize"):
                h, s, l = cv["h"] / 360, cv["s"] / 100, lum(r0, g0, b0)
            else:
                oh, os_, ol = rgb_to_hsl(r0, g0, b0)
                h = ((oh + cv.get("h", 0)) % 360 + 360) % 360 / 360
                s = max(0.0, min(1.0, os_ + cv.get("s", 0) / 100))
                l = ol
            if cv.get("c"):
                l = 0.5 + (l - 0.5) * factor
            l = max(0.0, min(1.0, l + cv.get("b", 0) / 200))
            dst[x, y] = (*hsl01_to_rgb(h, s, l), a)
    return out


def over(dst: Image.Image, src: Image.Image, behind: Optional[Image.Image] = None) -> None:
    d, s = dst.load(), src.load()
    b = behind.load() if behind else None
    for y in range(src.height):
        back_row = FRAME_H <= y < 2 * FRAME_H
        for x in range(src.width):
            r, g, bb, a = s[x, y]
            if a < ALPHA_MIN:
                continue
            if b and back_row and b[x, y][3] >= ALPHA_MIN:
                continue
            if a == 255:
                d[x, y] = (r, g, bb, 255)
                continue
            dr, dg, db, da = d[x, y]
            ao, as_ = da / 255, a / 255
            ar = as_ + ao * (1 - as_)
            d[x, y] = tuple(int((sv * as_ + dv * ao * (1 - as_)) / ar) for sv, dv in
                            ((r, dr), (g, dg), (bb, db))) + (int(ar * 255),)


def ramp(gray_l: float, colors: List[Tuple[int, int, int]]) -> Tuple[int, int, int]:
    """Map a 0-1 lightness onto a dark→light colour ramp."""
    i = min(len(colors) - 1, max(0, int(gray_l * len(colors) * 1.15)))
    return colors[i]


# ─── Pixel Agents character stack (characters/slots.ts, renderer.ts) ───

LAYERS = [
    ("body", "part", "body", "skin", None, None), ("under", "part", "body", "under", "under", None),
    ("utop", "part", "body", "utop", "utop", None), ("whites", "eye", None, None, None, None),
    ("iris", "eye", None, "eyes", None, None), ("lashes", "eye", None, "lashes", None, None),
    ("pants", "part", "pants", "pants", None, None), ("top", "part", "top", "top", None, None),
    ("full", "part", "full", "full", None, None), ("tie", "part", "full", "tie", None, None),
    ("jacket", "part", "jacket", "jacket", None, None), ("lanyard", "part", "lanyard", "lanyard", None, None),
    ("necklace", "part", "necklace", "necklace", None, None), ("watch", "part", "watch", "watch", None, None),
    ("shoes", "part", "shoes", "shoes", None, None), ("hair", "part", "hair", "hair", None, None),
    ("earrings", "part", "earrings", "earrings", None, "hair"), ("beard", "part", "beard", "beard", None, None),
    ("shades", "part", "shades", "shades", None, None), ("hairshades", "part", "hairshades", "hairshades", None, None),
    ("hat", "part", "hat", "hat", None, None),
]
EYE_SHAPES = {"plain": {}, "lashes": {}, "lidded": {"bare": True}, "stern": {"bare": True, "blink_lashes": True},
              "closed": {}}
BODY_EYES = {"flat": "plain", "bust": "lashes"}

with open(os.path.join(ART, "wardrobe.json"), encoding="utf-8") as _f:
    WARDROBE = json.load(_f)
SWAPS: Dict[str, dict] = {f"{cat}/{p['id']}": p["swaps"]
                          for cat, parts in WARDROBE["parts"].items() for p in parts if p.get("swaps")}


def art(path: str) -> Optional[Image.Image]:
    f = os.path.join(ART, *path.split("/")) + ".png"
    return Image.open(f).convert("RGBA") if os.path.isfile(f) else None


def build_layers(p: dict, blink: bool = False) -> Dict[str, Image.Image]:
    """Each worn piece, tinted, as its own 128x96 layer (draw order kept)."""
    shape = p.get("eyeShape") or BODY_EYES.get(p["body"], "plain")
    shown_eyes = "closed" if blink else shape
    worn_under = {"utop": bool(p.get("utop")) and not p.get("top") and not p.get("full"),
                  "under": bool(p.get("under")) and not p.get("pants") and not p.get("full")}
    out: Dict[str, Image.Image] = {}
    for pix, kind, sel, color, when, behind in LAYERS:
        if when and not worn_under[when]:
            continue
        if kind == "eye":
            folder = f"closed-{shape}" if (blink and pix == "lashes" and EYE_SHAPES[shape].get("blink_lashes")) \
                else shown_eyes
            path = f"eyes/{folder}/{pix}" + (f".{p.get('eyeRows', 'bottom')}" if pix == "iris" else "")
        else:
            part = p.get(sel)
            if not part:
                continue
            path = f"{pix}/{part}"
        img = art(path)
        if img is None:
            continue
        cv = p.get("colors", {}).get(color) if color else None
        if cv:
            img = tint_pixels(img, cv, SWAPS.get(f"{pix}/{p.get(sel)}") if kind == "part" else None)
        out[pix] = img
    if not EYE_SHAPES[shape].get("bare") and p.get("props", True):
        out["glasses_prop"] = art("props/reading-glasses")
    out["paper_prop"] = art("props/paper")
    return out


def stack(layers: Dict[str, Image.Image]) -> Image.Image:
    sheet = Image.new("RGBA", (SHEET_W, SHEET_H))
    order = [l[0] for l in LAYERS] + ["glasses_prop", "paper_prop"]
    behinds = {l[0]: l[5] for l in LAYERS}
    for pix in order:
        if pix in layers:
            over(sheet, layers[pix], layers.get(behinds.get(pix) or ""))
    return sheet


def C(h, s, b=0, c=0):
    return {"h": h, "s": s, "b": b, "c": c, "colorize": True}


# ─── Lumi's look, painted over the Pixel Agents poses ────────────────

SILVER = [P["silv_d"], P["silv_m"], P["silv_l"], P["silv_h"]]
TEAL = [P["teal_d"], P["teal_m"], P["teal_l"], P["teal_h"]]


def shade(rgb, k):
    return tuple(max(0, min(255, int(v * k))) for v in rgb)


def bbox(img: Image.Image, cell: Tuple[int, int]) -> Optional[Tuple[int, int, int, int]]:
    f, r = cell
    px = img.load()
    pts = [(x, y) for y in range(r * FRAME_H, (r + 1) * FRAME_H) for x in range(f * FRAME_W, (f + 1) * FRAME_W)
           if px[x, y][3] >= ALPHA_MIN]
    if not pts:
        return None
    return min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts)


def put(px, x, y, rgb, cell_box):
    x0, y0, x1, y1 = cell_box
    if x0 <= x <= x1 and y0 <= y <= y1:
        px[x, y] = (*rgb, 255)


def lumify(base: Image.Image, layers: Dict[str, Image.Image], look: dict) -> Image.Image:
    """Paint Lumi's features onto a stacked sheet, frame by frame."""
    out = base.copy()
    px = out.load()
    hair = layers.get("hair")
    hpx = hair.load() if hair else None
    accent = hexrgb(look["accent"])
    for r in range(3):                               # 0 down, 1 up, 2 right
        for f in range(8):
            cell = (f * FRAME_W, r * FRAME_H, f * FRAME_W + FRAME_W - 1, r * FRAME_H + FRAME_H - 1)
            hb = bbox(hair, (f, r)) if hair else None
            eb = bbox(layers["whites"], (f, r)) if "whites" in layers else None
            if not hb:
                continue
            hx0, hy0, hx1, hy1 = hb
            cx = (hx0 + hx1) / 2
            # 1) Split hair: her right side silver, her left side teal (or the hood colour).
            for y in range(hy0, hy1 + 1):
                for x in range(hx0, hx1 + 1):
                    if hpx[x, y][3] < ALPHA_MIN or px[x, y][:3] != hpx[x, y][:3]:
                        continue                     # not hair, or covered (hat, hood)
                    gl = lum(*hpx[x, y][:3])
                    if look.get("hood"):
                        fringe = eb and y >= eb[1] - 3 and r == 0 and hx0 + 2 <= x <= hx1 - 2
                        if not fringe:
                            px[x, y] = (*ramp(gl, look["hood"]), 255)
                            continue
                    if r == 0:
                        side_silver = x < cx
                    elif r == 1:
                        side_silver = x >= cx
                    else:
                        side_silver = x < hx0 + 2      # side view: her left (teal), a silver streak at the back
                    px[x, y] = (*ramp(gl, SILVER if side_silver else TEAL), 255)
            hood = look.get("hood")
            # 2) Twin tails / side tail: from the head's lower edge down beside the arms,
            #    in the free 2-pixel columns at the frame's sides.
            tails = look.get("tails", ())
            sway = 1 if (f in (0, 2)) else 0
            ty = hy1 - 3
            def tail(x_col, outward, colours):
                for k in range(11):
                    drift = outward * (1 if 2 <= k <= 6 else 0) + (sway * outward if k > 7 else 0)
                    xx = x_col + drift
                    put(px, xx, ty + k, colours[2] if k % 3 == 0 else colours[1], cell)
                    put(px, xx - outward, ty + k, colours[0] if k > 8 else colours[1], cell)
                put(px, x_col - outward, ty - 1, colours[2], cell)
            if r in (0, 1):
                for side in tails:                    # "right" = her right
                    viewer_left = (side == "right") == (r == 0)
                    colours = SILVER if side == "right" else TEAL
                    tail(cell[0] + 1 if viewer_left else cell[2] - 1, -1 if viewer_left else 1, colours)
            elif r == 2 and tails:
                tail(cell[0] + 2, -1, TEAL if "left" in tails else SILVER)
            # 3) Cat ears: two rows, white outside, pink inside (the hood's colour when it's up).
            ear_y = max(cell[1] + 1, hy0 - 2)
            def ear(x_outer, inner_dir, outline):
                body = look["hood"][2] if hood else P["silv_h"]
                put(px, x_outer, ear_y, outline, cell)
                put(px, x_outer, ear_y + 1, outline, cell)
                put(px, x_outer + inner_dir, ear_y + 1, P["pink"] if (r != 1 and not hood) else body, cell)
                put(px, x_outer + inner_dir * 2, ear_y + 1, outline, cell)
            if r in (0, 1):
                left_outline = (look["hood"][0] if hood else (SILVER[0] if r == 0 else TEAL[0]))
                right_outline = (look["hood"][0] if hood else (TEAL[0] if r == 0 else SILVER[0]))
                ear(hx0 + 1, 1, left_outline)
                ear(hx1 - 1, -1, right_outline)
            else:
                ear(int(cx) - 1, 1, look["hood"][0] if hood else TEAL[0])
            # 4) Halo: a two-row ring floating above the head, between the ears.
            hcol = hexrgb(look["halo"])
            hl = shade(hcol, 0.6)
            yh = max(cell[1], ear_y - 1 - (1 if f >= 3 and f % 2 else 0))
            hc = int(round(cx))
            for dx in range(-2, 2):
                put(px, hc + dx, yh, hcol, cell)
                put(px, hc + dx, yh + 1, hl, cell)
            put(px, hc - 3, yh + 1, hcol, cell)
            put(px, hc + 2, yh + 1, hcol, cell)
            # 5) Tie (front view), and suspenders / bag strap.
            if eb and r == 0:
                ex0, ey0, ex1, _ = eb
                tx = (ex0 + ex1) // 2
                neck = ey0 + 5
                if look.get("tie", True) and f not in (5, 6):      # not over the paper she reads
                    put(px, tx, neck, shade(accent, 0.7), cell)
                    put(px, tx + 1, neck, shade(accent, 0.7), cell)
                    for k in range(1, 5):
                        put(px, tx if k % 2 else tx + 1, neck + k, accent if k < 4 else shade(accent, 0.7), cell)
                if look.get("suspenders"):
                    for k in range(1, 6):
                        put(px, ex0 - 1, neck + k, P["black"], cell)
                        put(px, ex1 + 1, neck + k, P["black"], cell)
                if look.get("strap"):
                    for k in range(6):
                        put(px, ex1 + 1 - k, neck + k, P["steel"], cell)
            # 6) Thigh-high socks over the legs, mint soles on the boots.
            body_l, pants_l, shoes_l = layers.get("body"), layers.get("pants"), layers.get("shoes")
            if body_l and pants_l and shoes_l:
                pb, sb = bbox(pants_l, (f, r)), bbox(shoes_l, (f, r))
                if pb and sb:
                    bpx = body_l.load()
                    for y in range(pb[3] + 1, sb[1]):
                        for x in range(cell[0], cell[2] + 1):
                            if bpx[x, y][3] >= ALPHA_MIN and px[x, y][:3] == bpx[x, y][:3]:
                                gl = lum(*bpx[x, y][:3])
                                px[x, y] = (*(P["white"] if gl > 0.55 else P["lav"]), 255)
                    garter_y = pb[3] + 2
                    for x in range(cell[0], int(cx)):
                        if px[x, garter_y][3] >= ALPHA_MIN and px[x, garter_y][:3] in (P["white"], P["lav"]):
                            px[x, garter_y] = (*P["black"], 255)
                    spx = shoes_l.load()
                    for x in range(cell[0], cell[2] + 1):
                        for y in range(sb[3], sb[1] - 1, -1):
                            if spx[x, y][3] >= ALPHA_MIN:
                                px[x, y] = (*P["mint"], 255)
                                break
    return out


LUMIS = {
    # Main: the sheet's twin-tail look. White jacket, mint tie, mint halo.
    "main": {"profile": {"body": "bust", "eyeShape": "lashes", "eyeRows": "both", "under": "basic", "utop": "basic",
                         "hair": "side_swept", "top": "long_sleeved_shirt", "jacket": "bomber", "pants": "shorts",
                         "shoes": "boots", "props": False,
                         "colors": {"top": C(220, 8, 82, 30), "jacket": C(225, 10, 78, 35), "pants": C(232, 12, -72, -10),
                                    "shoes": C(230, 10, -70, 10), "eyes": C(172, 60, 5, 10)}},
             "look": {"accent": "5cf2d2", "halo": "8fd8cf", "tails": ("right", "left")}},
    # Office (Claude Code): the shirt-and-suspenders look, glasses on, violet tie and halo.
    "office": {"profile": {"body": "bust", "eyeShape": "lashes", "eyeRows": "both", "under": "basic", "utop": "basic",
                           "hair": "mop", "top": "long_sleeved_shirt", "pants": "shorts", "shoes": "boots",
                           "shades": "glasses",
                           "colors": {"top": C(220, 8, 84, 30), "pants": C(232, 12, -72, -10),
                                      "shoes": C(230, 10, -70, 10), "eyes": C(172, 60, 5, 10),
                                      "shades": C(265, 40, -55, 0)}},
               "look": {"accent": "b48cff", "halo": "c9b4ff", "suspenders": True}},
    # Minecraft: the cat-ear hoodie look, hood up, in green.
    "minecraft": {"profile": {"body": "bust", "eyeShape": "lashes", "eyeRows": "both", "under": "basic", "utop": "basic",
                              "hair": "bob", "top": "long_sleeved_shirt", "jacket": "bomber", "pants": "shorts",
                              "shoes": "boots", "props": False,
                              "colors": {"top": C(220, 8, 82, 30), "jacket": C(135, 45, 2, 10), "pants": C(232, 12, -72, -10),
                                         "shoes": C(230, 10, -70, 10), "eyes": C(172, 60, 5, 10)}},
                  "look": {"accent": "5bd16a", "halo": "a6f0b5", "tie": True,
                           "hood": [hexrgb("2f7a45"), hexrgb("4fae63"), hexrgb("7fd68e"), hexrgb("b8f0c2")]}},
    # System: the side-ponytail look with the bag strap, rose tie and halo.
    "system": {"profile": {"body": "bust", "eyeShape": "lashes", "eyeRows": "both", "under": "basic", "utop": "basic",
                           "hair": "side_swept", "top": "long_sleeved_shirt", "jacket": "bomber", "pants": "shorts",
                           "shoes": "boots", "props": False,
                           "colors": {"top": C(220, 8, 82, 30), "jacket": C(225, 10, 74, 35), "pants": C(232, 12, -72, -10),
                                      "shoes": C(230, 10, -70, 10), "eyes": C(172, 60, 5, 10)}},
               "look": {"accent": "ff5a7a", "halo": "ffb3c4", "tails": ("right",), "strap": True}},
}


def build_lumi(agent: str, blink: bool) -> Image.Image:
    spec = LUMIS[agent]
    layers = build_layers(spec["profile"], blink)
    return lumify(stack(layers), layers, spec["look"])


# ─── furniture catalog (core/src/assets/manifestUtils.ts) ─────────────

def load_catalog() -> Dict[str, dict]:
    cat: Dict[str, dict] = {}
    root = os.path.join(ASSETS, "furniture")
    for folder in sorted(os.listdir(root)):
        mf = os.path.join(root, folder, "manifest.json")
        if not os.path.isfile(mf):
            continue
        with open(mf, encoding="utf-8") as f:
            m = json.load(f)

        def walk(node, inh):
            inh = dict(inh)
            for k in ("category", "orientation", "state", "backgroundTiles", "canPlaceOnSurfaces", "mirrorSide"):
                if k in node:
                    inh[k] = node[k]
            if node.get("type") == "asset":
                cat[node["id"]] = dict(inh, id=node["id"],
                                       file=os.path.join(root, folder, node.get("file") or f"{node['id']}.png"),
                                       w=node["width"], h=node["height"],
                                       fw=node.get("footprintW", 1), fh=node.get("footprintH", 1))
                return
            for ch in node.get("members", []):
                walk(ch, inh)
        walk(m, {})
    for k, e in list(cat.items()):
        if e.get("mirrorSide") and e.get("orientation") == "side":
            cat[f"{k}:left"] = dict(e, id=f"{k}:left", orientation="left", mirrored=True)
    for e in cat.values():
        e["isDesk"] = e.get("category") == "desks"
    return cat


# ─── custom cyberpunk props, drawn here ───────────────────────────────

def img(w, h):
    i = Image.new("RGBA", (w, h))
    return i, ImageDraw.Draw(i)


def city_window(seed: int) -> List[Image.Image]:
    rng = random.Random(seed)
    w, h = 48, 32
    frames = []
    buildings = []
    x = 2
    while x < w - 2:
        bw = rng.randint(4, 8)
        bh = rng.randint(8, 20)
        buildings.append((x, bw, bh, rng.random()))
        x += bw + rng.choice((0, 1))
    lights = [(bx + 1 + rng.randint(0, max(0, bw - 3)), h - 3 - rng.randint(2, bh - 2), rng.choice(
        [P["neon_c"], P["neon_p"], P["neon_y"], P["cyan"]])) for bx, bw, bh, _ in buildings for _ in range(bw // 2 + 1)]
    for fr in range(3):
        im, d = img(w, h)
        d.rectangle([0, 0, w - 1, h - 1], fill=(*P["steel"], 255))
        for y in range(2, h - 2):                       # dusk → night sky
            t = (y - 2) / (h - 4)
            col = tuple(int(a + (b - a) * t) for a, b in zip(hexrgb("120a2e"), hexrgb("4a1f63")))
            d.line([2, y, w - 3, y], fill=(*col, 255))
        d.ellipse([w - 12, 4, w - 7, 9], fill=(*P["cyan"], 255))            # moon
        d.point([(w - 10, 6)], fill=(*P["teal_l"], 255))
        for bx, bw, bh, tone in buildings:
            base = hexrgb("0d0f1f") if tone < 0.5 else hexrgb("161b35")
            d.rectangle([bx, h - 3 - bh, min(w - 3, bx + bw - 1), h - 3], fill=(*base, 255))
        for i, (lx, ly, col) in enumerate(lights):
            if 2 <= lx <= w - 3 and (i + fr) % 4 != 0:
                d.point([(lx, ly)], fill=(*col, 255))
        # a little flying car crossing
        cx = 4 + fr * 14
        d.point([(cx, 10), (cx + 1, 10)], fill=(*P["neon_p"], 255))
        d.rectangle([w // 2 - 1, 2, w // 2, h - 3], fill=(*P["steel2"], 255))      # mullion
        d.rectangle([0, 0, w - 1, 1], fill=(*P["steel2"], 255))
        d.rectangle([0, h - 2, w - 1, h - 1], fill=(*P["steel2"], 255))
        d.line([2, h - 3, w - 3, h - 3], fill=(*P["neon_c"], 255))                 # sill glow
        frames.append(im)
    return frames


FONT = {  # 5x7 pixel letters
    "L": ["1....", "1....", "1....", "1....", "1....", "1....", "11111"],
    "U": ["1...1", "1...1", "1...1", "1...1", "1...1", "1...1", ".111."],
    "M": ["1...1", "11.11", "1.1.1", "1.1.1", "1...1", "1...1", "1...1"],
    "I": ["11111", "..1..", "..1..", "..1..", "..1..", "..1..", "11111"],
}


def neon_sign() -> List[Image.Image]:
    frames = []
    for fr in range(3):
        im, d = img(48, 16)
        d.rounded_rectangle([0, 0, 47, 15], radius=2, fill=(*P["ink"], 230), outline=(*P["steel2"], 255))
        glow = P["neon_c"] if fr != 2 else shade(P["neon_c"], 0.55)       # frame 2: flicker
        x = 6
        for ch in "LUMI":
            for yy, row in enumerate(FONT[ch]):
                for xx, v in enumerate(row):
                    if v == "1":
                        d.point([(x + xx, 4 + yy)], fill=(*glow, 255))
            x += 7 if ch != "I" else 6
        # a cat-ear heart in pink beside the name
        hx = 40
        for dx, dy in [(0, 6), (1, 5), (2, 6), (3, 5), (4, 6), (1, 7), (2, 8), (3, 7), (0, 5), (4, 5)]:
            d.point([(hx + dx - 1, dy)], fill=(*(P["neon_p"] if fr != 1 else shade(P["neon_p"], 0.75)), 255))
        frames.append(im)
    return frames


def holo_poster() -> Image.Image:
    im, d = img(16, 32)
    d.rectangle([1, 4, 14, 27], fill=(*P["steel"], 255), outline=(*P["neon_v"], 255))
    for y in range(5, 27):
        t = (y - 5) / 22
        d.line([2, y, 13, y], fill=(*tuple(int(a + (b - a) * t) for a, b in zip(P["teal_d"], hexrgb("25163f"))), 255))
    # Lumi silhouette: halo, cat ears, head, split hair
    d.ellipse([5, 7, 10, 9], outline=(*P["mint"], 255))
    d.point([(5, 12), (10, 12), (5, 11), (10, 11)], fill=(*P["silv_h"], 255))
    d.rectangle([5, 12, 10, 17], fill=(*P["blush"], 255))
    d.rectangle([5, 12, 7, 13], fill=(*P["silv_l"], 255))
    d.rectangle([8, 12, 10, 13], fill=(*P["teal_l"], 255))
    d.point([(6, 15), (9, 15)], fill=(*P["teal_d"], 255))
    d.rectangle([4, 18, 11, 24], fill=(*P["white"], 255))
    d.line([7, 18, 7, 22], fill=(*P["mint"], 255))
    return im


def server_rack() -> List[Image.Image]:
    frames = []
    rng = random.Random(7)
    pattern = [[rng.random() for _ in range(3)] for _ in range(9)]
    for fr in range(4):
        im, d = img(16, 40)
        d.rectangle([1, 1, 14, 39], fill=(*P["ink"], 255), outline=(*P["steel2"], 255))
        for i in range(9):
            y = 3 + i * 4
            d.rectangle([3, y, 12, y + 2], fill=(*P["navy2"], 255))
            for c in range(3):
                on = (pattern[i][c] + fr * 0.27) % 1 > 0.4
                col = [P["neon_c"], P["neon_y"], P["neon_p"]][(i + c) % 3] if on else hexrgb("1d3a3a")
                d.point([(4 + c * 2, y + 1)], fill=(*col, 255))
            d.line([10, y + 1, 11, y + 1], fill=(*P["steel2"], 255))
        d.line([1, 39, 14, 39], fill=(*P["neon_c"], 255))
        frames.append(im)
    return frames


def holo_monitor(kind: str) -> Tuple[Image.Image, List[Image.Image]]:
    """16x32 like a Pixel Agents PC (1x2, background row, sits on a desk): off + on frames."""
    def base():
        im, d = img(16, 32)
        d.rectangle([7, 24, 8, 29], fill=(*P["steel2"], 255))              # stand
        d.rectangle([4, 29, 11, 30], fill=(*P["steel"], 255))
        d.rectangle([1, 12, 14, 24], fill=(*P["ink"], 255), outline=(*P["steel2"], 255))
        return im, d
    off, d = base()
    d.rectangle([2, 13, 13, 23], fill=(*hexrgb("0b1420"), 255))
    on = []
    for fr in range(3):
        im, d = base()
        if kind == "minecraft":
            d.rectangle([2, 13, 13, 23], fill=(*hexrgb("6ec3ff"), 255))
            for x in range(2, 14):
                h = 19 + ((x * 7 + fr) % 3 == 0)
                d.line([x, h, x, 23], fill=(*hexrgb("8b5a2b"), 255))
                d.point([(x, h)], fill=(*hexrgb("5bbf5b"), 255))
            d.rectangle([3 + fr * 3, 14, 5 + fr * 3, 15], fill=(*P["white"], 255))   # cloud
        else:
            d.rectangle([2, 13, 13, 23], fill=(*hexrgb("0e2a36"), 255))
            for i in range(5):
                ln = 2 + ((i * 5 + fr * 3) % 9)
                d.line([3, 14 + i * 2, min(12, 3 + ln), 14 + i * 2],
                       fill=(*(P["neon_c"] if (i + fr) % 3 else P["neon_p"]), 255))
        d.line([1, 24, 14, 24], fill=(*P["neon_c"], 255))
        on.append(im)
    return off, on


def lily_pot() -> Image.Image:
    im, d = img(16, 32)
    d.polygon([(4, 24), (11, 24), (10, 30), (5, 30)], fill=(*P["navy2"], 255))
    d.line([4, 24, 11, 24], fill=(*P["neon_c"], 255))
    for sx, top in ((7, 12), (9, 14), (5, 15), (8, 10)):
        d.line([sx, top, 7 + (sx > 7), 24], fill=(*hexrgb("3f9a52"), 255))
    for fx, fy in ((7, 11), (9, 13), (5, 14), (8, 9), (11, 16)):
        for dx, dy in ((0, -1), (-1, 0), (1, 0), (0, 1), (-1, -1), (1, 1)):
            d.point([(fx + dx, fy + dy)], fill=(*P["white"], 255))
        d.point([(fx, fy)], fill=(*hexrgb("e8f27a"), 255))
    d.line([3, 20, 6, 22], fill=(*hexrgb("4fb563"), 255))
    d.line([12, 19, 9, 22], fill=(*hexrgb("4fb563"), 255))
    return im


def cat_bed() -> Image.Image:
    im, d = img(16, 16)
    d.ellipse([1, 6, 14, 14], fill=(*P["teal_m"], 255), outline=(*P["teal_d"], 255))
    d.ellipse([4, 8, 11, 12], fill=(*P["cyan"], 255))
    d.point([(6, 9), (9, 9), (7, 11), (8, 11)], fill=(*P["pink"], 255))
    return im


def neon_rug(w, h) -> Image.Image:
    im, d = img(w, h)
    d.rectangle([0, 0, w - 1, h - 1], fill=(*hexrgb("221a3d"), 235), outline=(*P["neon_v"], 255))
    d.rectangle([2, 2, w - 3, h - 3], outline=(*P["neon_c"], 160))
    for x in range(6, w - 6, 8):
        for y in range(6, h - 6, 8):
            d.point([(x, y)], fill=(*P["neon_p"], 150))
    return im


# ─── the room ─────────────────────────────────────────────────────────

COLS, ROWS = 20, 12

# Reused Pixel Agents furniture, recoloured: (type, col, row, colorize hsbc or None)
DARK = (232, 22, -48, 12)
FURNITURE = [
    ("DESK_FRONT", 2, 3, DARK), ("DESK_FRONT", 7, 3, DARK), ("DESK_FRONT", 12, 3, DARK),
    ("CUSHIONED_CHAIR_BACK", 3, 5, (135, 40, -20, 5)),     # Minecraft (green)
    ("CUSHIONED_CHAIR_BACK", 8, 5, (172, 45, -18, 5)),     # Main (mint)
    ("CUSHIONED_CHAIR_BACK", 13, 5, (265, 35, -22, 5)),    # Office (violet)
    ("SOFA_FRONT", 9, 7, (300, 30, -28, 5)), ("SOFA_BACK", 9, 10, (300, 30, -28, 5)),
    ("SOFA_SIDE", 8, 8, (300, 30, -28, 5)), ("SOFA_SIDE:left", 11, 8, (300, 30, -28, 5)),
    ("COFFEE_TABLE", 9, 8, DARK), ("COFFEE", 10, 9, None),
    ("SMALL_TABLE_FRONT", 16, 9, DARK), ("COFFEE", 16, 9, None),
    ("LARGE_PLANT", 17, 6, None), ("BIN", 18, 10, (232, 15, -40, 10)),
    ("HANGING_PLANT", 15, 1, None), ("CLOCK", 11, 1, (180, 40, -10, 10)),
]
# Custom pieces: (name, col, row, footprint w, h, background rows, surface?, wall?)
CUSTOM = [
    ("window", 2, 1, 3, 2, 0, False, True), ("window2", 5, 1, 3, 2, 0, False, True),
    ("sign", 8, 1, 3, 1, 0, False, True), ("poster", 14, 1, 1, 2, 0, False, True),
    ("rack", 16, 2, 1, 2, 0, False, False), ("rack", 17, 2, 1, 2, 0, False, False), ("rack", 18, 2, 1, 2, 0, False, False),
    ("mon_mc", 3, 3, 1, 2, 1, True, False), ("mon", 8, 3, 1, 2, 1, True, False), ("mon2", 9, 3, 1, 2, 1, True, False),
    ("mon", 13, 3, 1, 2, 1, True, False),
    ("lily", 1, 9, 1, 2, 1, False, False), ("lily", 5, 9, 1, 2, 1, False, False), ("lily", 14, 8, 1, 2, 1, False, False),
    ("catbed", 2, 10, 1, 1, 0, False, False),
]
# Where each agent works and rests: (col, row, facing, sits?, monitor names that light up)
STATIONS = {
    "main":      {"work": (8, 5, "up", True), "rest": (8, 5, "up", True), "screens": ["mon@8,3", "mon2@9,3"]},
    "office":    {"work": (13, 5, "up", True), "rest": (8, 9, "right", True), "screens": ["mon@13,3"]},
    "minecraft": {"work": (3, 5, "up", True), "rest": (9, 7, "down", True), "screens": ["mon_mc@3,3"]},
    "system":    {"work": (17, 4, "up", False), "rest": (11, 9, "left", True), "screens": []},
}


class Atlas:
    def __init__(self):
        self.items: List[Image.Image] = []
        self.keys: Dict[str, int] = {}

    def add(self, key: str, im: Image.Image) -> int:
        if key not in self.keys:
            self.keys[key] = len(self.items)
            self.items.append(im)
        return self.keys[key]

    def pack(self, width=256):
        x = y = row_h = 0
        rects = []
        for im in self.items:
            if x + im.width > width:
                x, y, row_h = 0, y + row_h + 1, 0
            rects.append([x, y, im.width, im.height])
            x += im.width + 1
            row_h = max(row_h, im.height)
        sheet = Image.new("RGBA", (width, y + row_h))
        for im, (rx, ry, _, _) in zip(self.items, rects):
            sheet.paste(im, (rx, ry))
        return sheet, rects


def build_room():
    cat = load_catalog()
    tiles = [[VOID] * COLS for _ in range(ROWS)]
    for r in range(1, ROWS):
        for c in range(COLS):
            edge = r == 1 or c == 0 or c == COLS - 1          # open at the front: no bottom wall
            tiles[r][c] = WALL if edge else 1
    tile = lambda c, r: tiles[r][c] if 0 <= c < COLS and 0 <= r < ROWS else VOID
    W, H = COLS * TILE, ROWS * TILE

    # Floor: Pixel Agents floor pattern, colorized deep navy, plus a neon grid.
    floor_pat = colorize(Image.open(os.path.join(ASSETS, "floors", "floor_1.png")), 232, 40, -72, -78)
    floor = Image.new("RGBA", (W, H))
    fd = ImageDraw.Draw(floor)
    for r in range(ROWS):
        for c in range(COLS):
            if tile(c, r) == WALL:
                fd.rectangle([c * TILE, r * TILE, c * TILE + 15, r * TILE + 15], fill=(*hexrgb("1a1530"), 255))
            elif tile(c, r) != VOID:
                floor.alpha_composite(floor_pat, (c * TILE, r * TILE))
                fd.line([c * TILE, r * TILE, c * TILE + 15, r * TILE], fill=(*P["neon_v"], 30))
                fd.line([c * TILE, r * TILE, c * TILE, r * TILE + 15], fill=(*P["neon_v"], 30))
    floor.alpha_composite(neon_rug(7 * TILE, 5 * TILE), (7 * TILE - 8, 7 * TILE - 6))
    fd.line([TILE, H - 2, W - TILE - 1, H - 2], fill=(*P["neon_c"], 255))       # the room's open front edge
    fd.line([TILE, H - 1, W - TILE - 1, H - 1], fill=(*P["neon_p"], 200))
    # Light pools: screens (cyan), the sign (pink), the racks (cyan), the window (violet).
    glow = Image.new("RGBA", (W, H))
    gd = ImageDraw.Draw(glow)
    for (gx, gy, rad, col, a) in ((8.5, 3.5, 30, P["neon_c"], 46), (3.5, 3.5, 24, hexrgb("5bd16a"), 40),
                                  (13.5, 3.5, 24, P["neon_v"], 42), (9.5, 2.2, 34, P["neon_p"], 34),
                                  (17.5, 3.5, 30, P["neon_c"], 40), (4.5, 2.2, 40, P["neon_v"], 30)):
        for k in range(rad, 0, -3):
            gd.ellipse([gx * TILE - k, gy * TILE + 8 - k * 0.6, gx * TILE + k, gy * TILE + 8 + k * 0.6],
                       fill=(*col, int(a * (1 - k / rad) ** 1.4)))
    floor.alpha_composite(glow)

    # Walls: Pixel Agents auto-tile pieces, colorized violet, with a neon trim line.
    wall_sheet = Image.open(os.path.join(ASSETS, "walls", "wall_0.png")).convert("RGBA")
    pieces = []
    for m in range(16):
        pc = colorize(wall_sheet.crop(((m % 4) * 16, (m // 4) * 32, (m % 4) * 16 + 16, (m // 4) * 32 + 32)),
                      258, 34, -100, -30)
        ppx = pc.load()
        for x in range(16):                           # neon strip along the wall face's foot
            for y in (28, 29):
                if ppx[x, y][3] >= ALPHA_MIN and ppx[x, y - 6][3] >= ALPHA_MIN:
                    ppx[x, y] = (*(P["neon_c"] if y == 28 else shade(P["neon_c"], 0.5)), 255)
            if not (m & 1) and ppx[x, 2][3] >= ALPHA_MIN:     # pink only along the top edge
                ppx[x, 2] = (*P["neon_p"], 255)
        pieces.append(pc)

    atlas = Atlas()
    drawables = []
    for r in range(ROWS):
        for c in range(COLS):
            if tile(c, r) != WALL:
                continue
            mask = ((tile(c, r - 1) == WALL) * 1 | (tile(c + 1, r) == WALL) * 2 |
                    (tile(c, r + 1) == WALL) * 4 | (tile(c - 1, r) == WALL) * 8)
            spr = pieces[mask]
            drawables.append({"a": atlas.add(f"wall{mask}", spr), "x": c * TILE, "y": r * TILE + TILE - spr.height,
                              "z": (r + 1) * TILE})

    # Desk z per tile so things on desks sort in front of them.
    desk_z: Dict[Tuple[int, int], float] = {}
    blocked = set()
    chairs = []
    for t, c, r, col in FURNITURE:
        e = cat[t]
        if e["isDesk"]:
            for dr in range(e["fh"]):
                for dc in range(e["fw"]):
                    desk_z[(c + dc, r + dr)] = max(desk_z.get((c + dc, r + dr), -1), r * TILE + e["h"])
    for t, c, r, col in FURNITURE:
        e = cat[t]
        base = Image.open(cat[t.split(":")[0]]["file"]).convert("RGBA")
        spr = colorize(base, *col) if col else base
        x, y = c * TILE, r * TILE
        z = y + e["h"]
        if e.get("category") == "wall":
            y = (r + 1) * TILE - e["h"]
            z = (r + 1) * TILE + 0.2                  # just in front of its wall
        if e.get("category") == "chairs":
            z = (r + e["fh"]) * TILE + 1 if e.get("orientation") == "back" else (r + 1) * TILE
            chairs.append((t, c, r, e))
        if e.get("canPlaceOnSurfaces") or t == "COFFEE":
            for dr in range(e["fh"]):
                for dc in range(e["fw"]):
                    dz = desk_z.get((c + dc, r + dr))
                    if dz is not None and dz + 0.5 > z:
                        z = dz + 0.5
        d = {"a": atlas.add(f"{t}{col}", spr), "x": x, "y": y, "z": z}
        if e.get("mirrored"):
            d["flip"] = 1
        drawables.append(d)
        if e.get("category") not in ("wall", "chairs") and t != "COFFEE":
            for dr in range(e.get("backgroundTiles") or 0, e["fh"]):
                for dc in range(e["fw"]):
                    blocked.add((c + dc, r + dr))

    win1, win2 = city_window(3), city_window(11)
    sign = neon_sign()
    rack = server_rack()
    mon_off, mon_on = holo_monitor("code")
    mc_off, mc_on = holo_monitor("minecraft")
    statics = {"poster": holo_poster(), "lily": lily_pot(), "catbed": cat_bed()}
    screens = {}
    for name, c, r, fw, fh, bg, surface, wall in CUSTOM:
        if name in ("window", "window2"):
            frames, fps = (win1 if name == "window" else win2), 1.2
        elif name == "sign":
            frames, fps = sign, 0
        elif name == "rack":
            frames, fps = rack, 5
        elif name in ("mon", "mon2", "mon_mc"):
            frames, fps = [mc_off if name == "mon_mc" else mon_off], 0
        else:
            frames, fps = [statics[name]], 0
        h = frames[0].height
        x = c * TILE
        y = (r + fh) * TILE - h if not wall else (r + 1) * TILE - h if name != "sign" else r * TILE - 4
        z = y + h if not wall else (r + 1) * TILE + 0.2
        if surface:
            z = max(z, desk_z.get((c, r), -1) + 0.5)
        d = {"a": atlas.add(f"{name}0", frames[0]), "x": x, "y": y, "z": z}
        if len(frames) > 1:
            d["frames"] = [atlas.add(f"{name}{i}", fr) for i, fr in enumerate(frames)]
            d["fps"] = fps
        if name == "sign":
            d["flicker"] = [atlas.add(f"{name}{i}", fr) for i, fr in enumerate(sign)]
        if name in ("mon", "mon2", "mon_mc"):
            d["on"] = [atlas.add(f"{name}on{i}", fr) for i, fr in enumerate(mc_on if name == "mon_mc" else mon_on)]
            screens[f"{name}@{c},{r}"] = len(drawables)
        drawables.append(d)
        if not wall and not surface:
            for dr in range(bg, fh):
                for dc in range(fw):
                    blocked.add((c + dc, r + dr))

    seat_tiles = {(st[name][0], st[name][1]) for st in STATIONS.values() for name in ("work", "rest")}
    walk = ["".join("1" if tile(c, r) not in (WALL, VOID) and ((c, r) not in blocked or (c, r) in seat_tiles)
                    else "0" for c in range(COLS)) for r in range(ROWS)]
    sheet, rects = atlas.pack()
    agents = {}
    for a, st in STATIONS.items():
        agents[a] = {k: {"col": st[k][0], "row": st[k][1], "face": st[k][2], "sit": st[k][3]} for k in ("work", "rest")}
        agents[a]["screens"] = [screens[s] for s in st["screens"]]
        agents[a]["accent"] = "#" + ACCENTS[a]
    return {"W": W, "H": H, "floor": floor, "atlas": sheet, "rects": rects, "drawables": drawables,
            "walk": walk, "agents": agents}


def bubble(name: str) -> Image.Image:
    with open(os.path.join(PA, "webview-ui", "src", "office", "sprites", f"bubble-{name}.json"), encoding="utf-8") as f:
        b = json.load(f)
    im = Image.new("RGBA", (b["width"], b["height"]))
    for y, row in enumerate(b["pixels"]):
        for x, k in enumerate(row):
            hv = b["palette"].get(k, "")
            if hv:
                im.putpixel((x, y), (*hexrgb(hv), 255))
    return im


def main():
    if not os.path.isdir(PA):
        sys.exit("third_party/pixel-agents is missing: git clone https://github.com/pixel-agents-hq/pixel-agents "
                 "third_party/pixel-agents")
    room = build_room()
    meta = {
        "W": room["W"], "H": room["H"], "tile": TILE, "cols": COLS, "rows": ROWS,
        "rects": room["rects"], "drawables": room["drawables"], "walk": room["walk"], "agents": room["agents"],
        "frame": {"w": FRAME_W, "h": FRAME_H, "walk": [0, 1, 2, 1], "type": [3, 4], "read": [5, 6], "seated": 7,
                  "rows": {"down": 0, "up": 1, "right": 2, "left": 2}},
        "timing": {"walk_px_s": 48, "walk_frame_s": 0.15, "type_frame_s": 0.3, "sit_offset": 6},
    }
    chars = {(a, b): build_lumi(a, b) for a in LUMIS for b in (False, True)}
    for out in OUTS:
        os.makedirs(os.path.join(out, "chars"), exist_ok=True)
        room["floor"].save(os.path.join(out, "floor.png"))
        room["atlas"].save(os.path.join(out, "atlas.png"))
        for name in ("permission", "waiting"):
            bubble(name).save(os.path.join(out, f"bubble-{name}.png"))
        with open(os.path.join(out, "office.js"), "w", encoding="utf-8") as f:
            f.write("/* Generated by scripts/build_pixel_office.py. Do not edit. */\n"
                    f"window.LUMI_OFFICE = {json.dumps(meta, separators=(',', ':'))};\n")
        for (a, b), sheet in chars.items():
            sheet.save(os.path.join(out, "chars", f"{a}{'_blink' if b else ''}.png"))
        shutil.copyfile(os.path.join(PA, "LICENSE"), os.path.join(out, "LICENSE-pixel-agents.txt"))
        with open(os.path.join(out, "CREDITS.md"), "w", encoding="utf-8") as f:
            f.write("# Credits\n\nCharacter bodies, eyes and animation poses, wall and floor tiles, some furniture "
                    "and the speech bubbles are from [Pixel Agents](https://github.com/pixel-agents-hq/pixel-agents) "
                    "by Pablo De Lucca, MIT License (LICENSE-pixel-agents.txt). Its pixel art is based on "
                    "[JIK-A-4, Metro City](https://jik-a-4.itch.io/metrocity-free-topdown-character-pack).\n\n"
                    "Lumi's look (split hair, cat ears, halo, outfits) and her cyberpunk room are drawn over and "
                    "around that by scripts/build_pixel_office.py, after her character sheet "
                    "(assets/branding/lumi_png_samples/Lumi_Character_Sheet.png). Re-run it after changing either.\n")
    print(f"baked {room['W']}x{room['H']} room, {len(room['drawables'])} drawables, {len(LUMIS)} Lumis "
          f"-> {', '.join(os.path.relpath(o, ROOT) for o in OUTS)}")


if __name__ == "__main__":
    main()
