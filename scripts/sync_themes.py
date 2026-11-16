"""
Copy the shared front-end files from web/ into the island so both ship the same:

    web/themes/            -> island/src/themes/
    web/theme-engine.js    -> island/src/theme-engine.js
    web/pipeline.js        -> island/src/pipeline.js
    web/pixel-office/lumi-sprites.js, chars/*-sheet.png  -> island/src/pixel-office/ (Lumi's
                           sprite layout and her sheets; the room files are kept the same by hand)

web/ is the source of truth. Run after editing a theme, the engine or the Pipeline:
    python scripts/sync_themes.py
tests/test_themes.py fails while the copies differ.
"""

import os
import shutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB, ISLAND = os.path.join(ROOT, "web"), os.path.join(ROOT, "island", "src")


def main():
    dst = os.path.join(ISLAND, "themes")
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    shutil.copytree(os.path.join(WEB, "themes"), dst)
    for f in ("theme-engine.js", "pipeline.js"):
        shutil.copyfile(os.path.join(WEB, f), os.path.join(ISLAND, f))
    office_src, office_dst = os.path.join(WEB, "pixel-office"), os.path.join(ISLAND, "pixel-office")
    shutil.copyfile(os.path.join(office_src, "lumi-sprites.js"), os.path.join(office_dst, "lumi-sprites.js"))
    os.makedirs(os.path.join(office_dst, "chars"), exist_ok=True)
    for f in os.listdir(os.path.join(office_src, "chars")):
        if f.endswith("-sheet.png"):
            shutil.copyfile(os.path.join(office_src, "chars", f), os.path.join(office_dst, "chars", f))
    print("synced web/themes, theme-engine.js, pipeline.js and the sprite manifest into island/src")


if __name__ == "__main__":
    main()
