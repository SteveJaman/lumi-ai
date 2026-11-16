"""
Lumi as chibi pixel art, placed by hand pixel by pixel (the grids below), after her
character sheet (assets/branding/lumi_png_samples/Lumi_Character_Sheet.png): split
silver / teal hair with side tails, pink-lined cat ears, halo, X hair clip, white
jacket with teal hood lining and grey plaid hem, tie, black shorts, thigh garter,
white socks, grey leg warmers, teal-soled boots, the black tail strings.

Writes the same sheets as scripts/generate_sprites.py (an image model), so either can
be used and the last one run wins:

    web/pixel-office/chars/<agent>-sheet.png   (+ the island copy)
    4 columns (walk cycle) x 3 rows (facing down, up, right), 40x56 cells,
    feet on each cell's bottom row.

    python scripts/lumi_pixel_art.py            # all four
    python scripts/lumi_pixel_art.py --preview out.png   # also an 8x preview

Front and back are drawn as the left half and mirrored (silver half -> teal half);
the side view is drawn whole. Legs are one template, moved per walk frame.
"""

import argparse
import os

from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIRS = [os.path.join(ROOT, "web", "pixel-office", "chars"),
            os.path.join(ROOT, "island", "src", "pixel-office", "chars")]
AGENTS = ("main", "minecraft", "office", "system")
GW, GH = 36, 50                 # the drawing
CELL_W, CELL_H = 40, 56         # the sheet cell (drawing + outline + room for the bob)

# ── palette ───────────────────────────────────────────────────────
BASE = {
    "S": "eef0f7", "s": "c4c8d8", "z": "8c91a8",          # silver hair: light, mid, dark
    "T": "a6ece2", "t": "5ec6b9", "y": "2e8a86",          # teal hair
    "W": "ffffff",                                         # shine
    "P": "f6b8c9",                                         # inner ear
    "K": "fff0e8", "k": "f3cfc5",                          # skin
    "E": "3fb7a9", "e": "1d4a55", "A": "9ff0e2", "U": "1f8580",   # eye: iris, lash, glint, deep iris
    "I": "ffffff", "V": "dcfbf6",                          # hair shine (silver side, teal side)
    "B": "f8a9b8", "M": "c8687c",                          # blush, mouth
    "w": "f7f9fd", "v": "d4d9e6", "c": "aab1c4",           # white cloth
    "L": "8e92a3", "l": "b9bccb", "d": "6c7084",           # grey plaid
    "D": "25252e", "X": "17171d", "j": "34303f",           # shorts, black (clip, garter), tail strings
    "F": "cdd0da", "f": "9ca1b2",                          # leg warmers
    "N": "3d3d4c", "h": "6a6a7e",                         # boots, their shine
    "R": "8a5a3c", "r": "5e3b27",                          # backpack (Minecraft)
    "o": "2a2336",                                         # outline
    "O": "a9e4da", "Y": "6fc3b6", "Z": "3f8f88", "a": "e3f8f4",   # cat hood: light, mid, dark, inner ear
    "x": "f5f3ee",                                         # the bandage on the hood's ear
    "m": "34406a", "b": "5868a0", "C": "8494c8",           # shoulder bag: strap, body, shine
    "i": "d8fdff",                                         # ice-crystal wing (with E and U)
}
TEAL = {"G": "6fd6c8", "g": "3f9f95", "Q": "7fe0d3", "H": "9cf0e6", "n": "a6eee6"}   # tie, lining, halo, soles
ACCENTS = {a: TEAL for a in ("main", "minecraft", "office", "system")}   # her sheet: teal on every outfit
# Each agent wears one of the four outfits on her character sheet.
OUTFIT = {"office": "ponytail", "main": "twintails", "minecraft": "shirt", "system": "hood"}
SHADE = {"S": "s", "s": "z", "T": "t", "t": "y", "w": "v", "v": "c", "K": "k", "F": "f", "L": "d",
         "l": "L", "G": "g", "Q": "G", "R": "r", "O": "Y", "Y": "Z", "b": "m", "x": "c"}
HAIR = set("SszTtyOYZ")
HOOD = {"S": "O", "T": "O", "I": "a", "V": "a", "W": "a", "s": "Y", "t": "Y", "z": "Z", "y": "Z", "P": "a"}
MIRROR = {"S": "T", "s": "t", "z": "y", "I": "V"}
BACK_SWAP = {"S": "T", "s": "t", "z": "y", "T": "S", "t": "s", "y": "z", "I": "V", "V": "I"}

# ── front, left half (cols 0-17; mirrored to 18-35) ───────────────
FRONT = [
    "..............HHHH",   # 0  halo
    "...........HHH....",
    "..............HHHH",
    ".......S..........",   # 3  ears
    "......SPS.........",
    "......SPPS........",
    ".....SPPPSs.ss.sss",   # messy top
    "....sSPPSSSsSSsSSS",
    "...ssSSSSSSSSSSSSS",
    "..zsSSIIISSSSSSSSS",   # the shine ring
    ".zsSSSSSSIIISSsSSS",   # 10
    "zsSoSSSsSSSSIIsSSS",   # side tail (cols 0-2) parts from the head at col 3
    "zsSosSSsSSSsSSSsSS",
    "zsSosSzsKsSzKsSzKs",   # bangs, darker strand tips
    "zsSosSsKKKzKKKKsKK",
    "zsSosSsKKKKKKKKKKK",
    "zsSosSseeeeeeeKKKK",   # eyes: lash with a wing
    "zsSosSsKeUUWWeKKKK",
    "zsSosSsKeUEWWeKKKK",
    "zsSosSsKeEEEUeKKKK",
    "zsSosSsKeEAAEeKKKK",   # 20  lower glint
    ".zSosSsKKeeeeKKKKK",
    ".zSosSkBBBKKKKKKMK",   # blush, a small smile
    ".zSo.sSkkKKKKKKKKM",   # side locks frame the face
    ".zSo.sS.kkkKKKKKKK",
    ".zSo.sS...kkkkkkkk",
    "..zS.zs........kKK",   # neck
    "..zS..s..vwwwwQQwG",   # collar, hood lining, tie
    "..zS....vwwwwQwwwG",
    "...zS..vwvwwwQwwgG",   # sleeve | body
    "...zS..vwvwwwQwwgG",   # 30
    "....z..vwvcccQwwwg",
    ".......vwvddddvvvv",   # jacket darkens, white belt
    ".......LlvLLlLDDDD",   # plaid cuffs and hem
    ".......KKvLlLLdDDD",   # hands
    ".......kK.LLlLDDDD",
    "..........LLLLDDD.",   # 36
]
BACK = FRONT[:3] + [
    ".......S..........",
    "......SSS.........",
    "......SsSS........",
    ".....SSsSSssssssss",
    "....sSSSSSSSSSSSSS",
    "...ssSSSSSSSSSSSSS",
    "..zsSSIIISSSSSSSSS",
    ".zsSSSSSSIIISSsSSS",
    "zsSoSSSsSSSSIIsSSS",
    "zsSosSSsSSSsSSSsSS",
    "zsSosSSsSSSSsSSSSS",
    "zsSosSsSSSSsSSSSsS",
    "zsSosSsSSSSsSSSSsS",
    "zsSosSsSSSsSSSSsSS",
    "zsSosSsSSSsSSSSsSS",
    "zsSosSSsSSsSSSsSSS",
    "zsSosSSsSSsSSSsSSS",
    "zsSosSSsSSSsSSsSSS",
    ".zSosSSSsSSsSSSsSS",
    ".zSosSSsSSSsSSSsSs",
    ".zSo.sSzsSSsSSsSss",
    ".zSo.sS.zzssszssss",
    ".zSo.sS...kkkkkkkk",
    "..zS.zs........kKK",
    "..zS..s..vwwwwQQQQ",
    "..zS....vwwwwwQQQQ",
    "...zS..vwvwwwwwwww",
    "...zS..vwvwwwwwwww",
    "....z..vwvccccccww",
    ".......vwvdddddddd",
    ".......LlvLLlLLlLL",
    ".......KKvLlLLlLLL",
    ".......kK.LLlLLlLL",
    "..........DDDDDDD.",
]
# ── side, facing right, whole width ───────────────────────────────
SIDE = [
    "................HHHHHH..............",
    "..............HH......HH............",
    "................HHHHHH..............",
    "....................T...............",
    "...................TPT..............",
    "..........t.t.....TPPT..............",
    "..........tTtttttTPPTtt.............",
    ".........tTTTTTTTTTTTTTTt...........",
    "........tTTTTTTTTTTTTTTTTt..........",
    ".......tTTTVVVTTTTTTTTTTTTt.........",
    ".......ytTTTTTVVVTTTTTTTTTTt........",
    "......yytTTTTTTTTTTTTTTtTTTTt.......",
    "......ytTTTTTTTTTTTTTTtTTtTTtTt.....",
    "......ytTTTTTTTTTTTTTtKKtTKtTKt.....",
    "......ytTTTTTTTTTTTtKKKKKKyKKKt.....",
    "......ytTTTTTTTTTTtKKKKKKKKKKKK.....",
    "......ytTTTTTTTTTTtKKKKKeeeeeKK.....",
    "......ytTTTTTTTTTTtKKKKKeUWWeKK.....",
    "......ytTTTTTTTTTTtKKKKKeUEWeKK.....",
    "......ytTTTTTTTTTTtKKKKKeEEUeKKK....",
    "......ytTTTTTTTTTtKKKKKKeEAEeKK.....",
    "......ytTTTTTTTTtKKKKKKKKeeeKK......",
    "......yytTTTTTTtkKKKKKBBBKKKM.......",
    ".......ytTTTTTtkkKKKKKKKKKK.........",
    ".......yytTTTt..kkkkKKKKK...........",
    "........ytTTt.....kkkkk.............",
    "........ytTt........KKK.............",
    "........ytTt..vwwwwwwwQG............",
    "........ytTt.vwwwwwwwwwQG...........",
    ".........ytt.vwwwvvwwwwwG...........",
    ".........yt..vwwwvwwwwwwG...........",
    "..........y..vwwwvwwwwwwg...........",
    ".............vLLLvwwwDDDD...........",
    ".............vLlLvwwwDDDD...........",
    ".............LLLLLKKDDDDD...........",
    ".............LlLLLkKDDDDD...........",
    ".............LLLLLLDDDDDD...........",
]
LEG = [                 # drawn from row 35 down, under the shorts
    ".KKKKK..",
    ".KKKKK..",
    ".KKKKK..",
    ".KKKKK..",
    ".KKKKk..",
    ".wwwwv..",
    ".wwwwv..",
    ".wwwwv..",
    "FFFFFf..",
    "FfFFFf..",
    "FFFfFf..",
    "fFFFFf..",
    "NhNNNN..",
    "NhNNNNN.",
    "nnnnnnn.",
]
SIDE_LEG = LEG[:12] + ["NNNhNNN..", "NNNNhNNN.", "nnnnnnnn."]
LEG_Y = 35


def _check():
    for name, rows, w in (("FRONT", FRONT, 18), ("BACK", BACK, 18), ("SIDE", SIDE, GW)):
        assert len(rows) == 37, (name, len(rows))
        for i, r in enumerate(rows):
            assert len(r) == w, (name, i, len(r), r)


def _blank():
    return [["."] * GW for _ in range(GH)]


def _put(g, rows, x0, y0, dark=False, flip=False):
    for dy, row in enumerate(rows):
        row = row[::-1] if flip else row
        for dx, ch in enumerate(row):
            x, y = x0 + dx, y0 + dy
            if ch != "." and 0 <= x < GW and 0 <= y < GH:
                g[y][x] = ch.lower() if dark and ch in "KF" else (SHADE.get(ch, ch) if dark else ch)


def _mirror(half, swap):
    return [h + "".join(swap.get(c, c) for c in reversed(h)) for h in half]


def frame(view, step, agent):
    """One 36x50 letter grid. step 0-3: 0 and 2 mid-stride, 1 and 3 legs together."""
    g = _blank()
    bob = -1 if step in (1, 3) else 0
    if view in ("down", "up"):
        lift_l = 2 if step == 0 else 0
        lift_r = 2 if step == 2 else 0
        _put(g, LEG, 11, LEG_Y - lift_l)
        _put(g, LEG, GW - 11 - 8, LEG_Y - lift_r, flip=True)
        if view == "down":
            upper = _mirror(FRONT, MIRROR)
        else:
            upper = [r.translate(str.maketrans(BACK_SWAP)) for r in _mirror(BACK, MIRROR)]
        _put(g, upper, 0, bob)
        for x in (6, GW - 7):                                   # the tail strings and their arrows
            for y in range(37, 45):
                g[y][x] = "j"
            g[45][x - 1] = g[45][x] = g[45][x + 1] = "j"
            g[46][x] = "j"
    else:
        near, far = {0: (3, -3), 1: (0, 0), 2: (-3, 3), 3: (0, 0)}[step]
        _put(g, SIDE_LEG, 15 + far, LEG_Y - (1 if step == 1 else 0), dark=True)
        _put(g, SIDE_LEG, 15 + near, LEG_Y - (1 if step == 3 else 0))
        for y in range(37, 45):
            g[y][12] = "j"
        g[45][11] = g[45][12] = g[45][13] = "j"
        g[46][12] = "j"
        _put(g, SIDE, 0, bob)
    _details(g, view, agent, bob)
    return g


def _details(g, view, agent, bob):
    """Dress her in her outfit from the sheet, then the small things every outfit has."""
    outfit = OUTFIT[agent]

    def at(x, y):
        y += bob
        return g[y][x] if 0 <= x < GW and 0 <= y < GH else "."

    def px(x, y, ch, only_empty=False):
        y += bob
        if 0 <= x < GW and 0 <= y < GH and (not only_empty or g[y][x] == "."):
            g[y][x] = ch

    def drop_tail(side):
        """side: 'L' or 'R' as seen; the tails are cols 0-3 / 32-35 (0-4 / 31-35 low down)."""
        for y in range(11, 32):
            reach = 4 if y >= 26 else 3
            xs = range(0, reach + 1) if side == "L" else range(GW - 1 - reach, GW)
            for x in xs:
                if at(x, y) in HAIR or at(x, y) == "o":
                    px(x, y, ".")

    def wing(x0, y0, flip=False):          # her little ice crystal, behind her shoulder
        shape = ["....i", "...iE", "..iEU", ".iEU.", "iEU..", ".U..."]
        for dy, row in enumerate(shape):
            for dx, ch in enumerate(row[::-1] if flip else row):
                if ch != ".":
                    px(x0 + dx, y0 + dy, ch, only_empty=True)

    if view in ("down", "up"):
        keep = {"twintails": "LR", "ponytail": "L" if view == "down" else "R"}.get(outfit, "")
        for side in "LR":
            if side not in keep:
                drop_tail(side)
    else:                                   # facing right we see her left side; her right tail is behind
        if outfit in ("shirt", "hood"):
            for y in range(23, 32):
                for x in range(0, 14):
                    if at(x, y) in HAIR:
                        px(x, y, ".")
        elif outfit == "ponytail":          # only her right (silver) tail, peeking out behind
            for y in range(23, 32):
                for x in range(0, 14):
                    ch = at(x, y)
                    if ch in "Tty":
                        px(x, y, {"T": "S", "t": "s", "y": "z"}[ch])

    if outfit == "hood":
        for y in range(0, 27):
            for x in range(GW):
                ch = at(x, y)
                inner = view == "down" and 6 <= x <= 29 and y >= 11     # her bangs stay out of the hood
                inner = inner or (view == "right" and x >= 20 and y >= 12)
                if ch in HOOD and not inner and not (ch == "W" and view != "up"):
                    px(x, y, HOOD[ch])
        if view in ("down", "up"):          # the hood frames her face down to the shoulders
            for y in range(11, 27):
                for x in (3, 4, 5, GW - 6, GW - 5, GW - 4):
                    px(x, y, "Y" if x in (3, GW - 4) else "O")
        if view == "down":                  # a darker rim around the face opening
            for x in range(6, GW - 6):
                px(x, 10, "Z")
            for y in range(11, 26):
                px(6, y, "Z"); px(GW - 7, y, "Z")
        else:
            for y in range(12, 27):
                for x in range(6, 12):
                    if at(x, y) == "." and at(x + 1, y) != ".":
                        px(x, y, "Y")
        ear = (26, 30) if view == "down" else (6, 10) if view == "up" else (18, 23)
        for x in range(*ear):               # the bandage on one of the hood's ears
            for y in (4, 5, 6, 7):
                if at(x, y) in "OYZa" and (x + y) % 3:
                    px(x, y, "x")

    if outfit == "shirt":                   # no jacket: white shirt, black suspenders, plain shorts
        for y in range(26, 37):
            for x in range(GW):
                ch = at(x, y)
                if ch == "Q":
                    px(x, y, "w")
                elif ch in "Lld" and (view != "down" or 10 <= x <= 25):
                    px(x, y, "D")
                elif ch in "Lld":
                    px(x, y, "w")
                elif ch == "c" and y < 33:
                    px(x, y, "w")
        for x in ((12, 23) if view != "right" else (18,)):
            for y in range(27, 32):
                px(x, y, "X")

    if view == "down":
        for x, y, ch in ((25, 10, "X"), (24, 11, "X"), (25, 11, "W"), (26, 11, "X"), (25, 12, "X")):   # X clip
            if outfit != "hood" or True:
                px(x, y, ch)
        if outfit in ("twintails", "ponytail"):
            for x, y in ((1, 10), (3, 10), (2, 11)):                                                  # ribbon
                px(x, y, "Q")
        px(5, 21, "G"); px(6, 22, "G"); px(7, 22, "Q")                                                # headset mic
        if outfit != "shirt":
            for y in (28, 29, 31):                                                                    # ALLIUM
                px(24, y, "X")
            px(24, 30, "c")
            wing(30, 24)
        for x in range(19, 24):                                                                       # garter
            if g[39][x] in "Kk":
                g[39][x] = "X"
        if outfit == "ponytail":                                                                      # shoulder bag
            for y in range(27, 31):
                px(9, y, "m")
            for y in range(30, 35):
                for x in range(4, 9):
                    px(x, y, "C" if (y == 30 and x < 8) else "b" if y < 34 else "m")
    elif view == "up":
        if outfit != "shirt":
            wing(1, 24, flip=True)
    else:
        if outfit != "hood":
            px(23, 10, "X"); px(22, 11, "X"); px(23, 11, "W"); px(24, 11, "X"); px(23, 12, "X")
        if outfit != "shirt":
            wing(7, 25, flip=True)


def shade_and_outline(g):
    """Rim shade toward the light's far side, shade skin under the bangs, then a 1px outline."""
    h, w = len(g), len(g[0])
    out = [row[:] for row in g]
    for y in range(h):
        for x in range(w):
            ch = g[y][x]
            if ch in ".Hoj":
                continue
            right = g[y][x + 1] if x + 1 < w else "."
            below = g[y + 1][x] if y + 1 < h else "."
            if (right == "." or below == ".") and ch in SHADE:
                out[y][x] = SHADE[ch]
            if ch == "K" and y > 0 and g[y - 1][x] in HAIR:
                out[y][x] = "k"
    big = [["."] * (w + 2) for _ in range(h + 2)]
    for y in range(h):
        for x in range(w):
            big[y + 1][x + 1] = out[y][x]
    res = [row[:] for row in big]
    for y in range(h + 2):
        for x in range(w + 2):
            if big[y][x] != ".":
                continue
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nx, ny = x + dx, y + dy
                if 0 <= nx < w + 2 and 0 <= ny < h + 2 and big[ny][nx] not in ".Hj":
                    res[y][x] = "@" + big[ny][nx][-1]
                    break
    return res


def render(g, agent):
    pal = {**BASE, **ACCENTS[agent]}
    im = Image.new("RGBA", (len(g[0]), len(g)), (0, 0, 0, 0))
    px = im.load()
    for y, row in enumerate(g):
        for x, ch in enumerate(row):
            if ch == ".":
                continue
            if ch[0] == "@":                         # selout: half plum, half the neighbour at 40%
                o, n = pal["o"], pal[ch[1]]
                px[x, y] = tuple(int((int(o[i:i + 2], 16) + int(n[i:i + 2], 16) * 0.4) / 2 + 0.5)
                                 for i in (0, 2, 4)) + (255,)
            else:
                c = pal[ch]
                px[x, y] = (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16), 255)
    return im


def sheet(agent):
    out = Image.new("RGBA", (CELL_W * 4, CELL_H * 3), (0, 0, 0, 0))
    for r, view in enumerate(("down", "up", "right")):
        for c in range(4):
            fig = render(shade_and_outline(frame(view, c, agent)), agent)    # 38 x 52
            out.alpha_composite(fig, (c * CELL_W + (CELL_W - fig.width) // 2, r * CELL_H + CELL_H - fig.height))
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--agents", nargs="+", choices=AGENTS, default=list(AGENTS))
    p.add_argument("--preview", help="also write an 8x preview of every sheet, side by side")
    args = p.parse_args(argv)
    _check()
    sheets = []
    for a in args.agents:
        s = sheet(a)
        sheets.append(s)
        for d in OUT_DIRS:
            os.makedirs(d, exist_ok=True)
            s.save(os.path.join(d, f"{a}-sheet.png"))
        print(f"wrote chars/{a}-sheet.png ({s.width}x{s.height})")
    if args.preview:
        pv = Image.new("RGBA", (sum(s.width for s in sheets) + 8 * (len(sheets) - 1), sheets[0].height),
                       (60, 64, 84, 255))
        x = 0
        for s in sheets:
            pv.alpha_composite(s, (x, 0))
            x += s.width + 8
        pv.resize((pv.width * 8, pv.height * 8), Image.NEAREST).save(args.preview)


if __name__ == "__main__":
    main()
