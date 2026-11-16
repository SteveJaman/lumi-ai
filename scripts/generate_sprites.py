"""
Generate Lumi's Pipeline sprite sheets with an image model, then cut them into
clean RPG-Maker-style walk sheets.

    python scripts/generate_sprites.py                      # all four agents, OpenAI
    python scripts/generate_sprites.py --agents main office
    python scripts/generate_sprites.py --backend sd         # local Stable Diffusion (A1111 API)
    python scripts/generate_sprites.py --reslice            # re-cut the saved raw images, no API call
    python scripts/generate_sprites.py --from-image my.png --agents main   # cut an image you made elsewhere

Backends
  openai  OPENAI_API_KEY (environment or .env), optional OPENAI_BASE_URL.
          Tries dall-e-3 (1792x1024, hd) and falls back to gpt-image-1 if the
          account no longer serves dall-e-3. --model forces one.
  sd      SD_API_URL (default http://127.0.0.1:7860): AUTOMATIC1111 / Forge,
          started with --api. POST /sdapi/v1/txt2img.

Every raw generation is kept in assets/sprites_raw/<agent>.png, so the cut can be
re-tuned (--reslice, --rows, --tolerance) without paying for another image.

The cut (Pillow + numpy + scipy, all already in the lumi env):
  1. the white background is flood-filled away from the image border only, so her
     white jacket, socks and cat ears (inside the black outline) stay opaque;
  2. each figure is found as a connected blob (a floating halo joins its owner), the
     blobs are grouped into 3 rows by height and read left to right;
  3. every figure is scaled by ONE factor (she doesn't change size between frames)
     to fit a CELL_W x CELL_H cell, premultiplied so no white fringe survives, alpha
     snapped to 0/255 and the colours quantised to a small palette, so it reads as
     pixel art at any integer zoom;
  4. each figure is pasted bottom-centre: her feet sit on the cell's last row, which
     is the point pipeline.js puts on the centre of her tile.

Output: web/pixel-office/chars/<agent>-sheet.png and the island copy, 4 columns
(walk cycle) x 3 rows (facing down, up, right; left is right mirrored).
"""

import argparse
import base64
import io
import json
import os
import shutil
import sys
import time

import numpy as np
import requests
from PIL import Image
from scipy import ndimage

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW_DIR = os.path.join(ROOT, "assets", "sprites_raw")
OUT_DIRS = [os.path.join(ROOT, "web", "pixel-office", "chars"),
            os.path.join(ROOT, "island", "src", "pixel-office", "chars")]

AGENTS = ("main", "minecraft", "office", "system")
CELL_W, CELL_H = 40, 56          # same cell as scripts/lumi_pixel_art.py: her body ~one 32px room tile wide
COLS, ROWS = 4, 3
ROW_NAMES = ("down", "up", "right")
PALETTE_COLORS = 48

PROMPT = ("A high quality 32-bit pixel art sprite sheet of an anime girl with split silver and teal hair, "
          "white cat ears, and a {halo} halo. She wears a white jacket, {tie} tie, and dark shorts{extra}. "
          "The sprite sheet must be perfectly aligned on a grid, showing 3 rows (facing down, up, and right) "
          "and 4 columns (walking animation frames). Solid white background. Crisp black outlines. "
          "Chibi proportions: a big head with large shiny eyes and a small body, soft pastel shading.")
LOOKS = {   # main: halo cyan, tie mint; the others swap her accent colour and add one prop
    "main":      {"halo": "cyan", "tie": "mint", "extra": ""},
    "minecraft": {"halo": "green", "tie": "green", "extra": ", and carries a small brown backpack"},
    "office":    {"halo": "lavender", "tie": "purple", "extra": ", and wears thin round glasses"},
    "system":    {"halo": "red", "tie": "red", "extra": ", with a mint hood on the jacket"},
}
NEGATIVE = "blurry, 3d render, photo, text, watermark, labels, numbers, grid lines, multiple characters, cropped"


def prompt_for(agent):
    return PROMPT.format(**LOOKS[agent])


# ── generation ────────────────────────────────────────────────────

def load_dotenv():
    path = os.path.join(ROOT, ".env")
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def gen_openai(prompt, model=None):
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        sys.exit("OPENAI_API_KEY isn't set (environment or .env). Set it, or use --backend sd / --from-image.")
    url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/") + "/images/generations"
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    bodies = {
        "dall-e-3": {"model": "dall-e-3", "prompt": prompt, "size": "1792x1024", "quality": "hd",
                     "n": 1, "response_format": "b64_json"},
        "gpt-image-1": {"model": "gpt-image-1", "prompt": prompt, "size": "1536x1024", "quality": "high", "n": 1},
    }
    order = [model] if model else ["dall-e-3", "gpt-image-1"]
    last = None
    for m in order:
        body = bodies.get(m, {"model": m, "prompt": prompt, "n": 1})
        r = requests.post(url, headers=headers, json=body, timeout=300)
        if r.status_code == 200:
            item = r.json()["data"][0]
            if item.get("revised_prompt"):
                print(f"    {m} revised the prompt to: {item['revised_prompt']}")
            raw = base64.b64decode(item["b64_json"]) if item.get("b64_json") else requests.get(item["url"], timeout=120).content
            return Image.open(io.BytesIO(raw)).convert("RGBA")
        last = f"{m}: HTTP {r.status_code} {r.text[:300]}"
        # only fall through to the next model when this one isn't available
        if r.status_code not in (400, 403, 404) or "model" not in r.text.lower():
            break
        print(f"    {m} unavailable, trying the next model")
    sys.exit(f"image generation failed: {last}")


def gen_sd(prompt):
    url = os.environ.get("SD_API_URL", "http://127.0.0.1:7860").rstrip("/") + "/sdapi/v1/txt2img"
    body = {"prompt": prompt, "negative_prompt": NEGATIVE, "width": int(os.environ.get("SD_WIDTH", 1344)),
            "height": int(os.environ.get("SD_HEIGHT", 768)), "steps": 30, "cfg_scale": 7, "sampler_name": "Euler a"}
    try:
        r = requests.post(url, json=body, timeout=600)
    except requests.RequestException as e:
        sys.exit(f"couldn't reach Stable Diffusion at {url} ({e}). Start it with --api or set SD_API_URL.")
    if r.status_code != 200:
        sys.exit(f"Stable Diffusion failed: HTTP {r.status_code} {r.text[:300]}")
    return Image.open(io.BytesIO(base64.b64decode(r.json()["images"][0]))).convert("RGBA")


# ── cutting ───────────────────────────────────────────────────────

def remove_background(im, tolerance=28):
    """Transparent wherever the near-white background reaches from the border."""
    a = np.asarray(im.convert("RGBA")).copy()
    rgb = a[..., :3].astype(np.int16)
    light = (rgb.min(axis=2) >= 255 - tolerance) & ((rgb.max(axis=2) - rgb.min(axis=2)) <= tolerance)
    light &= a[..., 3] > 0
    labels, _ = ndimage.label(light)
    border = np.unique(np.concatenate([labels[0], labels[-1], labels[:, 0], labels[:, -1]]))
    bg = np.isin(labels, border[border > 0]) | (a[..., 3] == 0)
    a[bg] = 0
    return Image.fromarray(a, "RGBA")


def find_frames(cut, rows=ROWS):
    """[[(x0, y0, x1, y1), ...] per row], in reading order, from a background-free image.

    Each figure is one connected blob; small leftovers (a halo or an ear floating off
    her head) join the nearest figure, and a blob that is two figures stacked is split. Blobs are grouped into rows at the biggest jumps in their centre height,
    so the generation doesn't have to sit on an exact grid."""
    alpha = np.asarray(cut)[..., 3] > 0
    alpha = ndimage.binary_opening(alpha, iterations=1)          # drop specks of leftover background
    h, w = alpha.shape
    labels, n = ndimage.label(ndimage.binary_dilation(alpha, iterations=1))
    labels[~alpha] = 0
    if n == 0:
        raise ValueError("nothing left after removing the background (try a lower --tolerance)")
    idx = np.arange(1, n + 1)
    area = ndimage.sum(alpha, labels, idx)
    slices = ndimage.find_objects(labels)
    blobs = [{"box": [sl[1].start, sl[0].start, sl[1].stop, sl[0].stop], "area": ar}
             for sl, ar in zip(slices, area) if sl is not None and ar > 0]
    big_area = sorted(b["area"] for b in blobs)[-min(len(blobs), rows * COLS):]
    cut_off = 0.15 * float(np.median(big_area))
    figs = [b for b in blobs if b["area"] >= cut_off]
    # Two figures touching top-to-bottom (her shadow on the next row's halo): split at the
    # thinnest row near the middle of anything far taller than a typical figure.
    med_h = float(np.median([f["box"][3] - f["box"][1] for f in figs]))
    for f in list(figs):
        x0, y0, x1, y1 = f["box"]
        if y1 - y0 <= 1.6 * med_h:
            continue
        prof = alpha[y0:y1, x0:x1].sum(axis=1)
        lo, hi = int((y1 - y0) * 0.3), int((y1 - y0) * 0.7)
        cut_y = y0 + lo + int(np.argmin(prof[lo:hi]))
        figs.remove(f)
        for a, b in ((y0, cut_y), (cut_y, y1)):
            yy, xx = np.nonzero(alpha[a:b, x0:x1])
            if len(yy):
                figs.append({"box": [x0 + xx.min(), a + yy.min(), x0 + xx.max() + 1, a + yy.max() + 1],
                             "area": len(yy)})
    for b in blobs:                                              # a stray halo/ear: give it to the nearest figure
        if b["area"] >= cut_off or b["area"] < 0.01 * cut_off:
            continue
        def gap(f, b=b):
            dx = max(f["box"][0] - b["box"][2], b["box"][0] - f["box"][2], 0)
            dy = max(f["box"][1] - b["box"][3], b["box"][1] - f["box"][3], 0)
            return dx * dx + dy * dy
        f = min(figs, key=gap)
        if gap(f) <= (h // 15) ** 2:
            fb, bb = f["box"], b["box"]
            f["box"] = [min(fb[0], bb[0]), min(fb[1], bb[1]), max(fb[2], bb[2]), max(fb[3], bb[3])]
    figs.sort(key=lambda f: (f["box"][1] + f["box"][3]) / 2)
    cy = [(f["box"][1] + f["box"][3]) / 2 for f in figs]
    splits = sorted(sorted(range(1, len(figs)), key=lambda i: cy[i - 1] - cy[i])[:rows - 1])
    groups = [figs[a:b] for a, b in zip([0, *splits], [*splits, len(figs)])]
    out = []
    for g in groups:
        if len(g) > COLS:                                        # extra blobs: keep the biggest, in order
            g = sorted(g, key=lambda f: -f["area"])[:COLS]
        out.append([tuple(f["box"]) for f in sorted(g, key=lambda f: f["box"][0])])
    return out


def _cycle(boxes):
    """Exactly COLS frames: 4 as drawn, 3 as RPG Maker's 0-1-2-1, fewer repeated."""
    if len(boxes) >= COLS:
        return boxes[:COLS]
    if len(boxes) == 3:
        return [boxes[0], boxes[1], boxes[2], boxes[1]]
    return [boxes[i % len(boxes)] for i in range(COLS)] if boxes else []


def _shrink(fig, scale):
    w, h = max(1, round(fig.width * scale)), max(1, round(fig.height * scale))
    small = fig.convert("RGBa").resize((w, h), Image.LANCZOS).convert("RGBA")   # premultiplied: no fringe
    a = np.asarray(small).copy()
    a[..., 3] = np.where(a[..., 3] >= 110, 255, 0)
    a[a[..., 3] == 0] = 0
    return Image.fromarray(a, "RGBA")


def quantize(sheet, colors=PALETTE_COLORS):
    a = np.asarray(sheet).copy()
    opaque = a[..., 3] > 0
    if not opaque.any():
        return sheet
    rgb = Image.fromarray(a[..., :3], "RGB").quantize(colors=colors, method=Image.Quantize.MEDIANCUT,
                                                       dither=Image.Dither.NONE).convert("RGB")
    a[..., :3] = np.where(opaque[..., None], np.asarray(rgb), 0)
    return Image.fromarray(a, "RGBA")


def slice_sheet(raw, row_order=ROW_NAMES, tolerance=28):
    """A raw generation -> a COLS x ROWS sheet of CELL_W x CELL_H cells (down, up, right)."""
    cut = remove_background(raw, tolerance)
    found = find_frames(cut, rows=len(row_order))
    by_dir = {}
    for name, boxes in zip(row_order, found):
        frames = [cut.crop(b) for b in _cycle(boxes)]
        if name == "left":                                       # we store right; left is drawn mirrored
            name, frames = "right", [f.transpose(Image.FLIP_LEFT_RIGHT) for f in frames]
        by_dir[name] = frames
    for need in ROW_NAMES:                                       # a missing direction borrows the nearest one
        if need not in by_dir:
            by_dir[need] = by_dir.get("down") or next(iter(by_dir.values()))
    figs = [f for d in ROW_NAMES for f in by_dir[d]]
    scale = min(CELL_W / max(f.width for f in figs), CELL_H / max(f.height for f in figs))
    sheet = Image.new("RGBA", (CELL_W * COLS, CELL_H * ROWS), (0, 0, 0, 0))
    for r, d in enumerate(ROW_NAMES):
        for c, fig in enumerate(by_dir[d]):
            s = _shrink(fig, scale)
            bbox = s.getbbox()
            if bbox:
                s = s.crop(bbox)
            x = c * CELL_W + (CELL_W - s.width) // 2
            y = r * CELL_H + CELL_H - s.height                    # feet on the cell's bottom row
            sheet.alpha_composite(s, (x, y))
    return quantize(sheet)


def save_sheet(agent, sheet):
    for d in OUT_DIRS:
        os.makedirs(d, exist_ok=True)
        sheet.save(os.path.join(d, f"{agent}-sheet.png"))
    print(f"    wrote chars/{agent}-sheet.png ({sheet.width}x{sheet.height}) to web and island")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--agents", nargs="+", choices=AGENTS, default=list(AGENTS))
    p.add_argument("--backend", choices=("openai", "sd"), default=os.environ.get("SPRITE_BACKEND", "openai"))
    p.add_argument("--model", help="force one OpenAI image model (default: dall-e-3, then gpt-image-1)")
    p.add_argument("--reslice", action="store_true", help="re-cut assets/sprites_raw/<agent>.png, no generation")
    p.add_argument("--from-image", help="cut this image instead of generating (use with one --agents)")
    p.add_argument("--rows", default=",".join(ROW_NAMES),
                   help="what the generated rows face, top to bottom, e.g. 'down,left,up' (left is mirrored)")
    p.add_argument("--tolerance", type=int, default=28, help="how far from pure white still counts as background")
    p.add_argument("--print-prompts", action="store_true")
    args = p.parse_args(argv)
    rows = tuple(s.strip() for s in args.rows.split(","))
    if args.print_prompts:
        for a in args.agents:
            print(f"{a}: {prompt_for(a)}")
        return
    if args.from_image and len(args.agents) != 1:
        sys.exit("--from-image cuts one image: pass exactly one --agents")
    load_dotenv()
    os.makedirs(RAW_DIR, exist_ok=True)
    for agent in args.agents:
        raw_path = os.path.join(RAW_DIR, f"{agent}.png")
        print(f"{agent}:")
        if args.from_image:
            raw = Image.open(args.from_image).convert("RGBA")
            raw.save(raw_path)
        elif args.reslice:
            if not os.path.exists(raw_path):
                print(f"    no {os.path.relpath(raw_path, ROOT)} yet, skipped")
                continue
            raw = Image.open(raw_path).convert("RGBA")
        else:
            t = time.time()
            raw = gen_sd(prompt_for(agent)) if args.backend == "sd" else gen_openai(prompt_for(agent), args.model)
            raw.save(raw_path)
            print(f"    generated {raw.width}x{raw.height} in {time.time() - t:.0f}s -> {os.path.relpath(raw_path, ROOT)}")
        save_sheet(agent, slice_sheet(raw, rows, args.tolerance))
    meta = {"cell": [CELL_W, CELL_H], "cols": COLS, "rows": list(ROW_NAMES)}
    print(json.dumps(meta))


if __name__ == "__main__":
    main()
