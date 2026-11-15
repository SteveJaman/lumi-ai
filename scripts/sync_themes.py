"""
Copy the shared front-end files from web/ into the island so both ship the same:

    web/themes/            -> island/src/themes/
    web/theme-engine.js    -> island/src/theme-engine.js
    web/pipeline.js        -> island/src/pipeline.js

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
    print("synced web/themes, theme-engine.js and pipeline.js into island/src")


if __name__ == "__main__":
    main()
