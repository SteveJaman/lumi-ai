"""
Lumi at 48x96 for the Pipeline (used by scripts/build_pixel_office.py).

The 16x32 Lumis (Pixel Agents poses + her look, painted at 1x) are scaled up 3x,
then repainted at the new resolution after her character sheet
(assets/branding/lumi_png_samples/Lumi_Character_Sheet.png): split silver/teal hair
with strands and a sheen, real eyes (lash line, teal iris, pupil, highlight), blush,
pink-lined cat ears, an elliptical halo, the teal hood lining at the collar, the
"ALLIUM" print down the jacket, the plaid hem of her shorts, a thigh garter, boot
glints, and the two long black ribbons with diamond tips that sway as she walks.

Sheet layout: 16 frames of 48x96 across, 3 rows (facing down, up, right):
    0-2 walk, 3-4 type, 5-6 read, 7 seated,
    8-9 stretch (arms up, then up on her toes), 10-11 coffee (mug at her chest, then
    sipping), 12-13 rice ball (holding it, then a bite out of it), 14-15 asleep
    (slumped forward, arms folded, eyes shut; 15 breathes).
"""

from __future__ import annotations

from typing import Dict, List, Optional, Set, Tuple

from PIL import Image

S = 3                         # 16x32 -> 48x96
FW, FH = 16 * S, 32 * S
COLS = 16
POSES = {"walk": [0, 1, 2, 1], "type": [3, 4], "read": [5, 6], "seated": 7,
         "stretch": [8, 9], "drink": [10, 11], "eat": [12, 13], "sleep": [14, 15]}
ALPHA_MIN = 2

RGB = Tuple[int, int, int]


def hexrgb(h: str) -> RGB:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


INK = hexrgb("1d1f2c")
LASH = hexrgb("2a2238")
IRIS_L, IRIS_D, PUPIL = hexrgb("74e0d0"), hexrgb("2f9c90"), hexrgb("173a40")
BLUSH, MOUTH = hexrgb("f6b8c8"), hexrgb("b5707e")
EAR_IN = hexrgb("f4a7bd")
PRINT = hexrgb("4a5068")
LINING = hexrgb("8fd8cf")
PLAID_L, PLAID_D = hexrgb("6b7084"), hexrgb("2e3240")
RIBBON, RIBBON_H = hexrgb("16161c"), hexrgb("3b3f52")
WHITE, LAV, SHADE = hexrgb("f4f6fb"), hexrgb("d5d6e0"), hexrgb("b9bccb")
SKIN_FALLBACK = hexrgb("f3d6c8")
MUG, MUG_SH, COFFEE = hexrgb("f7f8fc"), hexrgb("c9ccd8"), hexrgb("6a4630")
MUG_TEAL, MUG_TEAL_D = hexrgb("4fb3a8"), hexrgb("2b6f6c")
RICE, RICE_SH, NORI = hexrgb("fbfbf6"), hexrgb("d8dbe4"), hexrgb("1b2a22")

# 3x5 letters for the sleeve print, drawn turned a quarter (reading down the jacket).
GLYPHS = {
    "A": [".#.", "#.#", "###", "#.#", "#.#"], "L": ["#..", "#..", "#..", "#..", "###"],
    "I": ["###", ".#.", ".#.", ".#.", "###"], "U": ["#.#", "#.#", "#.#", "#.#", "###"],
    "M": ["#.#", "###", "###", "#.#", "#.#"],
}


# ─── small helpers ───────────────────────────────────────────────────

def lbox(layer: Optional[Image.Image], f: int, r: int) -> Optional[Tuple[int, int, int, int]]:
    """Bounding box of a 1x layer inside frame (f, r), in frame-local 1x pixels."""
    if layer is None:
        return None
    px = layer.load()
    pts = [(x, y) for y in range(32) for x in range(16) if px[f * 16 + x, r * 32 + y][3] >= ALPHA_MIN]
    if not pts:
        return None
    return min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts)


def lmask(layer: Optional[Image.Image], f: int, r: int) -> Set[Tuple[int, int]]:
    if layer is None:
        return set()
    px = layer.load()
    return {(x, y) for y in range(32) for x in range(16) if px[f * 16 + x, r * 32 + y][3] >= ALPHA_MIN}


def components(pts: Set[Tuple[int, int]]) -> List[Tuple[int, int, int, int]]:
    left, out = set(pts), []
    while left:
        stack = [left.pop()]
        comp = list(stack)
        while stack:
            x, y = stack.pop()
            for n in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                if n in left:
                    left.discard(n)
                    stack.append(n)
                    comp.append(n)
        out.append((min(p[0] for p in comp), min(p[1] for p in comp), max(p[0] for p in comp), max(p[1] for p in comp)))
    return sorted(out)


def union(*boxes):
    bs = [b for b in boxes if b]
    if not bs:
        return None
    return min(b[0] for b in bs), min(b[1] for b in bs), max(b[2] for b in bs), max(b[3] for b in bs)


class Canvas:
    """One 48x96 frame being painted."""

    def __init__(self, img: Image.Image):
        self.img = img
        self.px = img.load()

    def get(self, x, y):
        if 0 <= x < FW and 0 <= y < FH:
            return self.px[x, y]
        return (0, 0, 0, 0)

    def put(self, x, y, rgb, only_on=None):
        x, y = int(round(x)), int(round(y))
        if not (0 <= x < FW and 0 <= y < FH):
            return
        cur = self.px[x, y]
        if only_on == "clear" and cur[3] >= ALPHA_MIN:
            return
        if only_on == "solid" and cur[3] < ALPHA_MIN:
            return
        self.px[x, y] = (*rgb, 255)

    def clear(self, x, y):
        if 0 <= x < FW and 0 <= y < FH:
            self.px[x, y] = (0, 0, 0, 0)

    def rect(self, x0, y0, w, h, rgb, only_on=None):
        for y in range(int(y0), int(y0 + h)):
            for x in range(int(x0), int(x0 + w)):
                self.put(x, y, rgb, only_on)


def lum(rgb) -> float:
    return (0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]) / 255


def whiteish(c) -> bool:
    return c[3] >= ALPHA_MIN and lum(c) > 0.72 and max(c[:3]) - min(c[:3]) < 40


def skin_of(cv: Canvas, x, y) -> RGB:
    c = cv.get(int(x), int(y))
    if c[3] >= ALPHA_MIN and c[0] > c[2] + 15 and lum(c) > 0.55:
        return c[:3]
    return SKIN_FALLBACK


# ─── the repaint ─────────────────────────────────────────────────────

class Frame:
    """1x measurements of one frame, scaled to 48x96 coordinates."""

    def __init__(self, layers: Dict[str, Image.Image], f: int, r: int):
        self.f, self.r = f, r
        hb = lbox(layers.get("hair"), f, r)
        self.hair = tuple(v * S for v in hb) if hb else None
        eye_pts = lmask(layers.get("whites"), f, r) | lmask(layers.get("iris"), f, r)
        self.eyes = [(x0 * S, y0 * S, x1 * S + S - 1, y1 * S + S - 1) for x0, y0, x1, y1 in components(eye_pts)]
        tb = union(lbox(layers.get("top"), f, r), lbox(layers.get("jacket"), f, r))
        self.top = (tb[0] * S, tb[1] * S, tb[2] * S + S - 1, tb[3] * S + S - 1) if tb else None
        pb = lbox(layers.get("pants"), f, r)
        self.pants = (pb[0] * S, pb[1] * S, pb[2] * S + S - 1, pb[3] * S + S - 1) if pb else None
        sb = lbox(layers.get("shoes"), f, r)
        self.shoes = (sb[0] * S, sb[1] * S, sb[2] * S + S - 1, sb[3] * S + S - 1) if sb else None

    @property
    def cx(self):
        if self.hair:
            return (self.hair[0] + self.hair[2]) / 2
        return FW / 2

    @property
    def eye_y(self):
        return min(e[1] for e in self.eyes) if self.eyes else (self.hair[3] - 9 if self.hair else 30)


SKIN_L, SKIN_M, SKIN_D = hexrgb("fae6dd"), hexrgb("f2cfc3"), hexrgb("dcaea2")


def is_skin(c) -> bool:
    r, g, b = c[:3]
    return c[3] >= ALPHA_MIN and r > g > b - 10 and r - b > 28 and 0.35 < lum(c) < 0.9


def paint_skin(cv: Canvas, fr: Frame):
    """Her skin is pale peach (the sheet's swatch), not the base's tan; the base's dark
    brown head outline becomes a soft shadow instead of chunky blocks at 3x."""
    neck = fr.top[1] if fr.top else FH // 2
    for y in range(FH):
        for x in range(FW):
            c = cv.get(x, y)
            if c[3] < ALPHA_MIN:
                continue
            if is_skin(c):
                l = lum(c)
                cv.put(x, y, SKIN_L if l > 0.7 else SKIN_M if l > 0.55 else SKIN_D)
            elif y < neck + 2 and c[0] > c[2] + 12 and lum(c) < 0.42 and c[0] < 150:
                cv.put(x, y, SKIN_D)


def paint_hair(cv: Canvas, fr: Frame, ramps: List[List[RGB]]):
    if not fr.hair:
        return
    where = {}
    for ramp in ramps:
        for i, c in enumerate(ramp):
            where[tuple(c)] = (ramp, i)
    x0, y0, x1, y1 = fr.hair
    for y in range(y0, y1 + 1):
        for x in range(x0, x1 + 1):
            c = cv.get(x, y)
            hit = where.get(tuple(c[:3])) if c[3] >= ALPHA_MIN else None
            if not hit:
                continue
            ramp, i = hit
            if x % 3 == 2 and y > y0 + 2:                        # strands
                cv.put(x, y, ramp[max(0, i - 1)])
            elif y0 + 5 <= y <= y0 + 6 and (x // 2) % 2 == 0:     # the sheen ring
                cv.put(x, y, ramp[min(len(ramp) - 1, i + 1)])


BROW = hexrgb("8a8195")
RIM = hexrgb("3b2f5c")


def paint_eyes(cv: Canvas, fr: Frame, closed: bool, dy: int = 0, dx: int = 0):
    if fr.r == 1:
        return
    if fr.eyes:                                                   # the base's lash bars / glasses at 3x
        band0, band1 = min(e[1] for e in fr.eyes) - 5, max(e[3] for e in fr.eyes) + 2
        fx0 = min(e[0] for e in fr.eyes) - 4
        fx1 = max(e[2] for e in fr.eyes) + 4
        for y in range(band0, band1 + 1):
            for x in range(fx0, fx1 + 1):
                c = cv.get(x + dx, y + dy)
                if c[3] >= ALPHA_MIN and (lum(c) < 0.25 or is_skin(c)):
                    cv.put(x + dx, y + dy, SKIN_L)
    for x0, y0, x1, y1 in fr.eyes:
        w = 4 if fr.r == 0 else 3
        ex = int(round((x0 + x1) / 2 - w / 2)) + dx
        ey = y0 - 1 + dy
        if fr.r == 0 or fr.r == 2:                                # a thin, soft brow
            for k in range(w + 1):
                cv.put(ex - 1 + k, ey - 3 + (1 if k in (0, w) else 0), BROW, "solid")
        if getattr(fr, "glasses", False):                         # thin violet rims (Office)
            for k in range(-2, w + 2):
                cv.put(ex + k, ey - 1, RIM); cv.put(ex + k, ey + 6, RIM)
            for k in range(-1, 7):
                cv.put(ex - 2, ey + k, RIM); cv.put(ex + w + 1, ey + k, RIM)
        if closed:
            cv.put(ex - 1, ey + 3, LASH)
            for k in range(w):
                cv.put(ex + k, ey + 4, LASH)
            cv.put(ex + w, ey + 3, LASH)
            continue
        for k in range(-1, w + 1):
            cv.put(ex + k, ey, LASH)
        for row in range(1, 5):
            for k in range(w):
                cv.put(ex + k, ey + row, IRIS_L if row <= 2 else IRIS_D)
        cv.put(ex + 1, ey + 2, PUPIL); cv.put(ex + 1, ey + 3, PUPIL)
        if w == 4:
            cv.put(ex + 2, ey + 2, PUPIL); cv.put(ex + 2, ey + 3, PUPIL)
        cv.put(ex, ey + 1, WHITE)
        for k in range(w):
            cv.put(ex + k, ey + 5, SKIN_D)
    if fr.r == 0 and len(fr.eyes) >= 2:
        ys = fr.eyes[0][1] + dy
        for x0, _, x1, _ in fr.eyes:
            mid = (x0 + x1) // 2 + dx
            cv.put(mid - 2, ys + 7, BLUSH, "solid"); cv.put(mid - 1, ys + 7, BLUSH, "solid")
            cv.put(mid + 1, ys + 7, BLUSH, "solid"); cv.put(mid + 2, ys + 7, BLUSH, "solid")
        mx = (fr.eyes[0][2] + fr.eyes[-1][0]) // 2 + dx
        cv.put(mx, ys + 10, MOUTH, "solid"); cv.put(mx + 1, ys + 10, MOUTH, "solid")


def paint_ears_halo(cv: Canvas, fr: Frame, look: dict, halo: RGB, hood: Optional[List[RGB]]):
    if not fr.hair:
        return
    x0, top, x1, _ = fr.hair
    for y in range(0, top):                                       # drop the 1x ears and halo
        for x in range(FW):
            cv.clear(x, y)
    outer = hood[2] if hood else WHITE
    edge = hood[0] if hood else SHADE

    def hair_top_at(x):
        for y in range(top, top + 24):
            if cv.get(int(x), y)[3] >= ALPHA_MIN:
                return y
        return top

    def ear(apex_x, mirror):
        base_y = hair_top_at(apex_x) + 2                      # sits into the hair
        for dy in range(0, 9):
            y = base_y - 8 + dy
            hw = dy * 0.5
            for x in range(int(round(apex_x - hw)), int(round(apex_x + hw)) + 1):
                is_edge = abs(x - apex_x) >= hw - 0.6
                cv.put(x, y, edge if is_edge else outer)
            if fr.r != 1 and not hood and dy >= 3:
                ihw = (dy - 3) * 0.4
                for x in range(int(round(apex_x - ihw)), int(round(apex_x + ihw)) + 1):
                    cv.put(x + (1 if mirror else -1) * 0, y, EAR_IN)

    if fr.r in (0, 1):
        ear(x0 + 5, False)
        ear(x1 - 5, True)
    else:
        ear(fr.cx - 1, False)
    # The halo: an ellipse floating just above her head.
    hcx, hcy, rx, ry = fr.cx, max(2.5, hair_top_at(fr.cx) - 6.5), 8.5, 2.2
    lite = tuple(min(255, int(v * 1.15 + 20)) for v in halo)
    dark = tuple(int(v * 0.62) for v in halo)
    for y in range(int(hcy - ry - 2), int(hcy + ry + 3)):
        for x in range(int(hcx - rx - 2), int(hcx + rx + 3)):
            d = ((x + 0.5 - hcx) / rx) ** 2 + ((y + 0.5 - hcy) / ry) ** 2
            if 0.45 <= d <= 1.3:
                cv.put(x, y, lite if y < hcy - 0.5 else (halo if y < hcy + 1 else dark))


def paint_outfit(cv: Canvas, fr: Frame, look: dict, walk_phase: int):
    if not fr.top:
        return
    tx0, ty0, tx1, ty1 = fr.top
    center = (fr.eyes[0][2] + fr.eyes[-1][0]) / 2 if (fr.r == 0 and len(fr.eyes) >= 2) else (tx0 + tx1) / 2
    # Folds: a soft shade down both sides of the white top.
    for y in range(ty0 + 2, ty1 + 1):
        for x in (tx0 + 1, tx1 - 1):
            if whiteish(cv.get(x, y)):
                cv.put(x, y, LAV)
    # Teal hood lining at the collar (front).
    if fr.r == 0 and not look.get("hood_up"):
        for k in range(4):
            for x in (int(center) - 6 + k // 2, int(center) + 5 - k // 2):
                if whiteish(cv.get(x, ty0 + k)):
                    cv.put(x, ty0 + k, LINING)
    # "ALLIUM", turned a quarter, down her left front panel (or along the sleeve, side view).
    if fr.r in (0, 2) and fr.f not in (3, 4, 5, 6):
        sx = tx1 - 7 if fr.r == 0 else int((tx0 + tx1) / 2) - 2
        sy = ty0 + 3
        for letter in "ALLIUM":
            g = GLYPHS[letter]
            for gy, row in enumerate(g):
                for gx, ch in enumerate(row):
                    if ch == "#":
                        x, y = sx + (4 - gy), sy + gx
                        if whiteish(cv.get(x, y)) or cv.get(x, y)[:3] == LAV:
                            cv.put(x, y, PRINT)
            sy += 4
            if sy > ty1 + 4:
                break
    # Plaid hem of the shorts.
    if fr.pants:
        px0, py0, px1, py1 = fr.pants
        for y in range(py1 - 2, py1 + 1):
            for x in range(px0, px1 + 1):
                c = cv.get(x, y)
                if c[3] >= ALPHA_MIN and lum(c) < 0.35:
                    cv.put(x, y, PLAID_L if (x + y) % 2 == 0 else PLAID_D)
        # The thigh garter on her right leg (viewer's left in the front view).
        gy = py1 + 5
        for y in (gy, gy + 1):
            for x in range(px0, int(center) if fr.r == 0 else px1 + 1):
                c = cv.get(x, y)
                if c[3] >= ALPHA_MIN and lum(c) > 0.6:
                    cv.put(x, y, RIBBON)
    # Boot glints.
    if fr.shoes:
        sx0, sy0, sx1, _ = fr.shoes
        for x in (sx0 + 2, sx1 - 3):
            if cv.get(x, sy0 + 2)[3] >= ALPHA_MIN and lum(cv.get(x, sy0 + 2)) < 0.3:
                cv.put(x, sy0 + 2, hexrgb("7d8196"))
    # The two long ribbons from the back of her waist, with diamond tips.
    if fr.pants:
        py0, py1 = fr.pants[1], fr.pants[3]
        length = (py1 - py0) + (12 if fr.f == 7 else 24)
        sway = [0, 1, 0, -1][walk_phase % 4] if fr.f <= 2 else 0
        xs = [tx0 - 2, tx1 + 2] if fr.r in (0, 1) else [tx0 - 1]
        for i, x_base in enumerate(xs):
            out = -1 if i == 0 else 1
            for k in range(length):
                drift = (sway * out) if k > length // 2 else 0
                x = x_base + drift
                cv.put(x + out, py0 + 2 + k, RIBBON, "clear")
                cv.put(x, py0 + 2 + k, RIBBON, "clear")
                cv.put(x - out, py0 + 2 + k, RIBBON_H, "clear")
            tx, ty = x_base + drift, py0 + 2 + length
            for dyy, hw in enumerate((0, 1, 2, 3, 2, 1, 0)):
                for dxx in range(-hw, hw + 1):
                    cv.put(tx + dxx, ty + dyy, RIBBON_H if abs(dxx) == hw else RIBBON, "clear")
            cv.put(tx, ty + 3, WHITE); cv.put(tx, ty + 2, LAV)


def repaint(img: Image.Image, fr: Frame, look: dict, halo: RGB, ramps, hood, closed: bool) -> Image.Image:
    cv = Canvas(img)
    paint_skin(cv, fr)
    paint_hair(cv, fr, ramps)
    paint_eyes(cv, fr, closed)
    paint_outfit(cv, fr, look, fr.f)
    paint_ears_halo(cv, fr, look, halo, hood)
    return cv.img


# ─── the activity poses ──────────────────────────────────────────────

def _erase_hanging_arms(cv: Canvas, fr: Frame):
    if not fr.top:
        return
    tx0, ty0, tx1, ty1 = fr.top
    for y in range(ty0 + 3, ty1 + 13):
        for x in list(range(tx0 - 3, tx0 + 3)) + list(range(tx1 - 2, tx1 + 4)):
            c = cv.get(x, y)
            if c[3] >= ALPHA_MIN and (whiteish(c) or c[:3] == LAV or (c[0] > c[2] + 15 and lum(c) > 0.55)):
                cv.clear(x, y)


def _clasp_overhead(cv: Canvas, cx: float, hair_top: int, lift: int):
    """Arms up in a wide V from the sides of her head, sleeves outlined, hands open."""
    cx = int(round(cx))
    top = max(4, hair_top - 8 - lift)
    for side in (-1, 1):
        x_bot, y_bot = cx + side * 17, hair_top + 10
        x_top, y_top = cx + side * 10, top + 3
        steps = y_bot - y_top
        for k in range(steps + 1):
            t = k / max(1, steps)
            x = round(x_bot + (x_top - x_bot) * t)
            y = y_bot - k
            for w in range(-2, 3):
                cv.put(x + w, y, INK if abs(w) == 2 else (LAV if w == side else WHITE))
        for hy in range(4):                                      # the hand
            for hx in range(-2, 2):
                cv.put(x_top + hx + (0 if side < 0 else 1), top - 1 + hy, INK if hy == 0 or hx in (-2, 1) else SKIN_L)


def stretch(base: Image.Image, fr: Frame, up_more: bool) -> Image.Image:
    img = base.copy()
    cv = Canvas(img)
    _erase_hanging_arms(cv, fr)
    if fr.hair:
        top = next((y for y in range(fr.hair[1], FH) if cv.get(int(fr.cx), y)[3] >= ALPHA_MIN), fr.hair[1])
        _clasp_overhead(cv, fr.cx, max(top, 12), 2 if up_more else 0)
    if up_more:                                                   # up on her toes
        lifted = Image.new("RGBA", img.size)
        lifted.paste(img, (0, -2))
        return lifted
    return img


def _mug(cv: Canvas, x, y, handle_right=True):
    """A 9x10 mug in her teal with a white band, dark-outlined, coffee showing."""
    cv.rect(x - 1, y - 1, 11, 12, INK)
    cv.rect(x, y, 9, 10, MUG_TEAL)
    cv.rect(x + 6, y, 3, 10, MUG_TEAL_D)
    cv.rect(x, y + 4, 9, 2, MUG)
    cv.rect(x + 1, y, 7, 2, COFFEE)
    hx = x + 9 if handle_right else x - 4
    cv.rect(hx, y + 2, 4, 6, INK)
    cv.rect(hx + 1, y + 3, 2, 4, MUG_TEAL_D)
    cv.rect(hx + (0 if handle_right else 2), y + 4, 1, 2, INK)


def _rice_ball(cv: Canvas, x, y, bitten):
    """An 11x10 onigiri, dark-outlined, nori wrap; a bite out of its corner when bitten."""
    for dy in range(-1, 11):
        hw = (dy + 1) * 0.55 + 0.5
        for dx in range(int(round(-hw)), int(round(hw)) + 1):
            cv.put(x + 5 + dx, y + dy, INK)
    for dy in range(10):
        hw = dy * 0.55 + 0.4
        for dx in range(int(round(-hw)), int(round(hw)) + 1):
            cv.put(x + 5 + dx, y + dy, RICE if abs(dx) < hw - 0.6 else RICE_SH)
    cv.rect(x + 2, y + 6, 7, 4, NORI)
    cv.rect(x + 3, y + 6, 1, 4, hexrgb("2f4a3b"))
    if bitten:
        for (bx, by) in ((6, 2), (7, 2), (6, 3), (7, 3), (8, 3), (7, 4), (8, 4), (9, 4)):
            cv.clear(x + bx, y + by)
        for (bx, by) in ((5, 2), (6, 4), (7, 5)):
            cv.put(x + bx, y + by, RICE_SH)


def hold(base: Image.Image, fr: Frame, item: str, raised: bool) -> Image.Image:
    """Seated, holding the mug or the rice ball: at her chest, or up at her mouth."""
    img = base.copy()
    cv = Canvas(img)
    if not fr.top:
        return img
    tx0, ty0, tx1, _ = fr.top
    skin = skin_of(cv, fr.cx, fr.eye_y + 9)
    mouth_y = fr.eye_y + 11
    if fr.r == 1:                                                 # back view: just the elbow when she sips
        if raised:
            cv.rect(tx1 - 1, ty0 + 2, 4, 6, WHITE); cv.rect(tx1 + 2, ty0 + 2, 1, 6, LAV)
        return img
    cx = int((tx0 + tx1) / 2) if fr.r == 0 else min(FW - 12, tx1 - 2)
    y = (mouth_y - 4) if raised else (ty0 + 7)
    # her forearm, outlined so it shows against the jacket, up to the hand
    arm_y = y + 6
    for x in range(min(cx + 4, tx1), tx1 + 2):
        cv.put(x, arm_y - 1, INK); cv.put(x, arm_y, WHITE); cv.put(x, arm_y + 1, WHITE)
        cv.put(x, arm_y + 2, LAV); cv.put(x, arm_y + 3, INK)
    if item == "mug":
        _mug(cv, cx - 5, y, handle_right=False)
    else:
        _rice_ball(cv, cx - 5, y, bitten=raised)
    cv.rect(cx + 3, arm_y - 1, 4, 5, INK)
    cv.rect(cx + 4, arm_y, 2, 3, SKIN_M)
    return img


def asleep(base: Image.Image, fr: Frame, breathe: bool, closed_eyes_frame: Frame) -> Image.Image:
    """Slumped forward in her chair, arms folded, head down, eyes shut."""
    img = base.copy()
    cv = Canvas(img)
    if not fr.top:
        return img
    tx0, ty0, tx1, _ = fr.top
    neck = ty0
    drop = 8 if breathe else 7
    fwd = 3 if fr.r == 2 else 0
    head = img.crop((0, 0, FW, neck))
    for y in range(0, neck):
        for x in range(FW):
            cv.clear(x, y)
    # folded arms across her lap / the desk edge
    if fr.r in (0, 2):
        for y in range(ty0 + 8, ty0 + 15):
            for x in range(tx0, tx1 + 1):
                edge = y in (ty0 + 8, ty0 + 14) or x in (tx0, tx1)
                cv.put(x, y, INK if edge else (LAV if y == ty0 + 13 else WHITE), "solid")
    img.alpha_composite(head, (fwd, drop))
    cv = Canvas(img)
    if fr.r != 1:
        paint_eyes(cv, closed_eyes_frame, closed=True, dy=drop, dx=fwd)
    return img


# ─── the sheet ───────────────────────────────────────────────────────

def hd_sheet(base1x: Image.Image, layers: Dict[str, Image.Image], look: dict, ramps, hood,
             closed: bool) -> Image.Image:
    """The 768x288 sheet (16 frames x 3 rows of 48x96) from a lumified 1x sheet."""
    sheet = Image.new("RGBA", (FW * COLS, FH * 3))
    halo = hexrgb(look["halo"])
    look = dict(look, hood_up=bool(hood))
    for r in range(3):
        frames = {}
        for f in range(8):
            cell = base1x.crop((f * 16, r * 32, f * 16 + 16, r * 32 + 32)).resize((FW, FH), Image.NEAREST)
            fr = Frame(layers, f, r)
            fr.glasses = bool(look.get("glasses"))
            frames[f] = (repaint(cell, fr, look, halo, ramps, hood, closed), fr)
            sheet.alpha_composite(frames[f][0], (f * FW, r * FH))
        stand, fs = frames[1]
        seat, fseat = frames[7]
        extra = {
            8: stretch(stand, fs, False), 9: stretch(stand, fs, True),
            10: hold(seat, fseat, "mug", False), 11: hold(seat, fseat, "mug", True),
            12: hold(seat, fseat, "rice", False), 13: hold(seat, fseat, "rice", True),
            14: asleep(seat, fseat, False, fseat), 15: asleep(seat, fseat, True, fseat),
        }
        for col, im in extra.items():
            sheet.alpha_composite(im, (col * FW, r * FH))
    return sheet
