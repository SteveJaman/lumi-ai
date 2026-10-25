"""
Install Lumi's Minecraft skin so LumiBot stops showing up as Steve.

A vanilla LAN world runs in offline mode, and an offline-mode bot can't
send a skin to the server, so everyone sees the default skin. The
CustomSkinLoader mod fixes that on your client: it loads a player's skin
from a local file. This script puts the skin where the mod looks and
configures the mod to look there first:

  1. Takes a 64x64 skin (by default the one already built at
     sdk/examples/minecraft/lumi_skin.png).
  2. Copies it to sdk/examples/minecraft/lumi_skin.png (served by
     bot.js on :3001) and to
     %APPDATA%\\.minecraft\\CustomSkinLoader\\LocalSkin\\skins\\<name>.png,
     where <name> is MC_USERNAME from sdk/examples/minecraft/.env.
  3. Puts the LocalSkin loader at the top of CustomSkinLoader.json's
     loadlist (before Mojang, which could otherwise return a real
     account's skin for the same name), creating the file if needed and
     backing up the old one.
  4. Clears the mod's cache, so it doesn't keep a cached Steve.

The image must be a real skin (64x64, 64x32, or a square HD skin like
128x128). The reference sheet in assets/branding/ is NOT a skin layout,
so cropping a corner of it would give a scrap of ear; --from-sheet
rebuilds the skin from it with hand-mapped crops instead.

You still need the CustomSkinLoader mod (Fabric build for your
Minecraft version, plus Fabric API) in your mods folder. The script
warns if it can't find it.

Usage (from the workspace root):
    python scripts/setup_skin.py --dry-run
    python scripts/setup_skin.py
    python scripts/setup_skin.py --source path/to/skin.png
    python scripts/setup_skin.py --from-sheet
    python scripts/setup_skin.py --minecraft-dir "D:\\Prism\\instances\\1.19\\.minecraft"
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time

try:
    from PIL import Image, ImageEnhance
except ImportError:
    sys.exit("Pillow is required: pip install pillow")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOT_DIR = os.path.join(ROOT, "sdk", "examples", "minecraft")
BOT_SKIN = os.path.join(BOT_DIR, "lumi_skin.png")
SHEET = os.path.join(ROOT, "assets", "branding", "lumi_png_samples",
                     "Anime Teal-White Minecraft Skin Atlas.png")
MOD_PAGE = "https://modrinth.com/mod/customskinloader"

# CustomSkinLoader's loader entries. LocalSkin reads
# <.minecraft>/CustomSkinLoader/LocalSkin/skins/<USERNAME>.png.
LOCAL_SKIN = {
    "name": "LocalSkin",
    "type": "Legacy",
    "checkPNG": False,
    "skin": "LocalSkin/skins/{USERNAME}.png",
    "model": "auto",
    "cape": "LocalSkin/capes/{USERNAME}.png",
    "elytra": "LocalSkin/elytras/{USERNAME}.png",
}
MOJANG = {
    "name": "Mojang",
    "type": "MojangAPI",
    "apiRoot": "https://api.mojang.com/",
    "sessionRoot": "https://sessionserver.mojang.com/",
}


# ─── The skin image ──────────────────────────────────────────────────────────

def load_skin(path: str) -> Image.Image:
    """A real skin image, or exit with why not."""
    img = Image.open(path)
    w, h = img.size
    if (w, h) in ((64, 64), (64, 32)) or (w == h and w % 64 == 0 and w <= 1024):
        return img.convert("RGBA")
    sys.exit(
        f"{path} is {w}x{h}, which isn't a skin layout (64x64, 64x32, or a square HD skin).\n"
        "A character sheet or render can't be cropped into a skin. Use --from-sheet to rebuild "
        "Lumi's skin from her reference sheet, or pass a real skin with --source."
    )


def build_from_sheet(path: str) -> Image.Image:
    """Lumi's 64x64 skin (classic 4 px arms) from her reference sheet:
    each skin face is box-downsampled from a hand-picked region of the
    sheet's front, back and side views. The face is hand-pixelled, since
    a downsampled anime face loses its eyes. The crop boxes fit this one
    sheet (1536x1024)."""
    sheet = Image.open(path).convert("RGB")
    if sheet.size != (1536, 1024):
        sys.exit(f"--from-sheet expects Lumi's 1536x1024 reference sheet; {path} is "
                 f"{sheet.size[0]}x{sheet.size[1]}.")
    sheet = ImageEnhance.Color(sheet).enhance(1.15)
    skin = Image.new("RGBA", (64, 64), (0, 0, 0, 0))

    def part(box, w, h):
        return sheet.crop(box).resize((w, h), Image.BOX)

    def avg(box):
        return part(box, 1, 1).getpixel((0, 0))

    def solid(color, w, h):
        return Image.new("RGB", (w, h), color)

    def put(img, x, y):
        skin.paste(img.convert("RGBA"), (x, y))

    # Head (8x8 faces)
    hair_white = avg((130, 70, 190, 110))
    hair_teal = tuple(int(c * 0.82) for c in avg((560, 120, 620, 220)))
    skin_tone = avg((170, 255, 220, 275))
    face_rows = ["WWWWTTTT", "WWWWTTTT", "WWWsTTTT", "WsssssTT",
                 "WLLssLLT", "WEesseET", "sbssssbs", "ssssssss"]
    palette = {"W": hair_white, "T": hair_teal, "s": skin_tone, "L": (34, 40, 56),
               "E": avg((205, 205, 225, 225)), "e": (200, 245, 245), "b": (245, 190, 190)}
    face = Image.new("RGB", (8, 8))
    for y, row in enumerate(face_rows):
        for x, c in enumerate(row):
            face.putpixel((x, y), palette[c])
    put(face, 8, 8)                                      # front
    put(part((420, 70, 525, 260), 8, 8), 0, 8)          # right side: white hair
    put(part((525, 70, 630, 260), 8, 8), 16, 8)         # left side: teal hair
    put(part((700, 60, 890, 250), 8, 8), 24, 8)         # back
    put(part((700, 60, 890, 120), 8, 8), 8, 0)          # top
    put(solid(skin_tone, 8, 8), 16, 0)                  # bottom

    # Body (8 wide, 12 tall, 4 deep)
    put(part((100, 285, 295, 600), 8, 12), 20, 20)      # front: jacket, tie, skirt
    put(part((705, 285, 870, 600), 8, 12), 32, 20)      # back
    side = part((1020, 290, 1080, 600), 4, 12)
    put(side, 16, 20)
    put(side, 28, 20)
    put(part((100, 285, 295, 320), 8, 4), 20, 16)       # top (shoulders)
    put(part((100, 560, 295, 600), 8, 4), 28, 16)       # bottom (skirt hem)

    # Arms (4 wide, 12 tall, 4 deep)
    def arm(bx, by, front, back):
        put(part(front, 4, 12), bx + 4, by + 4)
        put(part(back, 4, 12), bx + 12, by + 4)
        s = part((900, 295, 965, 590), 4, 12)
        put(s, bx, by + 4)
        put(s, bx + 8, by + 4)
        put(part((900, 295, 965, 330), 4, 4), bx + 4, by)
        put(solid(skin_tone, 4, 4), bx + 8, by)
    arm(40, 16, (10, 290, 95, 590), (1335, 300, 1395, 620))     # right arm
    arm(32, 48, (600, 295, 680, 590), (1410, 300, 1470, 620))   # left arm

    # Legs (4 wide, 12 tall, 4 deep)
    boot = avg((110, 920, 170, 960))

    def leg(bx, by, front, back, side_box):
        put(part(front, 4, 12), bx + 4, by + 4)
        put(part(back, 4, 12), bx + 12, by + 4)
        s = part(side_box, 4, 12)
        put(s, bx, by + 4)
        put(s, bx + 8, by + 4)
        put(solid(skin_tone, 4, 4), bx + 4, by)
        put(solid(boot, 4, 4), bx + 8, by)
    leg(0, 16, (100, 600, 180, 1000), (725, 600, 780, 1000), (900, 650, 980, 1000))
    leg(16, 48, (200, 600, 275, 1000), (790, 600, 845, 1000), (1020, 650, 1095, 1000))
    return skin


# ─── Minecraft side ──────────────────────────────────────────────────────────

def default_minecraft_dir() -> str:
    if os.name == "nt":
        return os.path.join(os.environ.get("APPDATA", ""), ".minecraft")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/minecraft")
    return os.path.expanduser("~/.minecraft")


def bot_username() -> str:
    """MC_USERNAME from the bot's .env, else LumiBot."""
    for name in (".env", ".env.example"):
        try:
            with open(os.path.join(BOT_DIR, name), encoding="utf-8") as f:
                for line in f:
                    key, sep, value = line.strip().partition("=")
                    if sep and key.strip() == "MC_USERNAME" and value.strip():
                        return value.strip().strip('"').strip("'")
        except OSError:
            pass
    return "LumiBot"


def find_mod(mc_dir: str):
    mods = os.path.join(mc_dir, "mods")
    try:
        return next((f for f in os.listdir(mods) if "customskinloader" in f.lower()), None)
    except OSError:
        return None


def configure_loader(config_path: str, dry_run: bool) -> str:
    """Put LocalSkin first in the loadlist. Returns what was done."""
    if not os.path.exists(config_path):
        if not dry_run:
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump({"loadlist": [LOCAL_SKIN, MOJANG]}, f, indent=2)
        return "created with LocalSkin first (the mod adds its other settings on first run)"
    try:
        with open(config_path, encoding="utf-8") as f:
            config = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        sys.exit(f"{config_path} isn't valid JSON ({e}). Fix or delete it, then run this again.")
    loadlist = config.get("loadlist")
    if not isinstance(loadlist, list):
        loadlist = []
    local = next((e for e in loadlist if isinstance(e, dict) and e.get("name") == "LocalSkin"), None)
    if loadlist and loadlist[0] is local:
        return "already has LocalSkin first; left unchanged"
    rest = [e for e in loadlist if e is not local]
    config["loadlist"] = [local or dict(LOCAL_SKIN)] + rest
    if not dry_run:
        shutil.copyfile(config_path, config_path + ".bak")
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
    return ("moved LocalSkin to the top" if local else "added LocalSkin at the top") + \
           " (old file kept as CustomSkinLoader.json.bak)"


def clear_cache(csl_dir: str, dry_run: bool) -> int:
    """Empty the mod's cache folders. Returns how many files were there."""
    count = 0
    for name in ("cache", "caches"):
        path = os.path.join(csl_dir, name)
        if not os.path.isdir(path):
            continue
        for _, _, files in os.walk(path):
            count += len(files)
        if not dry_run:
            shutil.rmtree(path, ignore_errors=True)
    return count


# ─── Main ────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Install Lumi's Minecraft skin for CustomSkinLoader.")
    ap.add_argument("--source", default=BOT_SKIN,
                    help="Skin PNG to install (default: sdk/examples/minecraft/lumi_skin.png)")
    ap.add_argument("--from-sheet", action="store_true",
                    help="Rebuild the skin from Lumi's reference sheet in assets/branding/")
    ap.add_argument("--minecraft-dir", default=default_minecraft_dir(),
                    help="Your .minecraft folder (default: %%APPDATA%%\\.minecraft)")
    ap.add_argument("--username", default=None,
                    help="In-game name the skin is for (default: MC_USERNAME from the bot's .env)")
    ap.add_argument("--dry-run", action="store_true", help="Show what would change, change nothing")
    args = ap.parse_args()

    username = args.username or bot_username()
    skin = build_from_sheet(SHEET) if args.from_sheet else load_skin(args.source)
    source_label = "reference sheet (rebuilt)" if args.from_sheet else os.path.relpath(args.source, ROOT)
    mc_dir = args.minecraft_dir
    csl_dir = os.path.join(mc_dir, "CustomSkinLoader")
    local_skin = os.path.join(csl_dir, "LocalSkin", "skins", f"{username}.png")
    prefix = "[dry run] would " if args.dry_run else ""

    print(f"Skin: {source_label}, {skin.size[0]}x{skin.size[1]}, for player {username}")
    if not os.path.isdir(mc_dir):
        sys.exit(f"No Minecraft folder at {mc_dir}. Pass --minecraft-dir for a launcher instance.")
    config_path = os.path.join(csl_dir, "CustomSkinLoader.json")
    if os.path.exists(config_path):
        # Check it before writing anything, so a bad config changes nothing.
        try:
            with open(config_path, encoding="utf-8") as f:
                json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            sys.exit(f"{config_path} isn't valid JSON ({e}). Fix or delete it, then run this again.")

    # 1. bot.js's copy, served on :3001.
    if os.path.abspath(args.source) != os.path.abspath(BOT_SKIN) or args.from_sheet:
        if not args.dry_run:
            if os.path.exists(BOT_SKIN):
                shutil.copyfile(BOT_SKIN, os.path.join(BOT_DIR, "lumi_skin.bak.png"))
            skin.save(BOT_SKIN)
        print(f"  {prefix}write {os.path.relpath(BOT_SKIN, ROOT)} (bot.js serves it on :3001)")
    else:
        print(f"  {os.path.relpath(BOT_SKIN, ROOT)} is the source; left as is")

    # 2. CustomSkinLoader's local copy.
    if not args.dry_run:
        os.makedirs(os.path.dirname(local_skin), exist_ok=True)
        skin.save(local_skin)
    print(f"  {prefix}write {local_skin}")

    # 3. Loader order.
    if not args.dry_run:
        os.makedirs(csl_dir, exist_ok=True)
    print(f"  {prefix}configure {config_path}: {configure_loader(config_path, args.dry_run)}")

    # 4. Cache.
    cached = clear_cache(csl_dir, args.dry_run)
    print(f"  {prefix}clear the mod's cache ({cached} file{'s' if cached != 1 else ''})")

    mod = find_mod(mc_dir)
    if mod:
        print(f"\nCustomSkinLoader found: {mod}")
        print(f"Done. Restart Minecraft (or press F3+T in game) to see {username} with Lumi's skin."
              if not args.dry_run else "\nDry run only: nothing was changed.")
    else:
        print("\nCustomSkinLoader is NOT installed in "
              f"{os.path.join(mc_dir, 'mods')}. The skin won't show until it is:")
        print(f"  1. Download the Fabric build for your Minecraft version from {MOD_PAGE}")
        print("     (it needs Fabric API, which you already need for Fabric mods).")
        print("  2. Put the .jar in the mods folder and start Minecraft with the Fabric profile.")
        print("The files above are already in place for it." if not args.dry_run
              else "Dry run only: nothing was changed.")


if __name__ == "__main__":
    main()
