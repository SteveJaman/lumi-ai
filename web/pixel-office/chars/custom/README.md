# Custom Lumi sprites

Drop hand-drawn PNGs here to replace any pose of any Pipeline agent. They're picked up on the
next page load; no code change. Then run `python scripts/sync_themes.py` so Lumi Island gets them too.

## Format

- Frames are **48 px wide and 96 px tall**, laid side by side; any number of frames (they loop).
- **3 rows**: facing down, facing up, facing right. Facing left is the right row mirrored.
- So a 4-frame walk is 192 x 288 px. Transparent background, feet on the bottom pixel row.
- Her look (assets/branding/lumi_png_samples/Lumi_Character_Sheet.png): split silver/teal hair,
  white cat ears with pink insides, a teal halo, white jacket with the "ALLIUM" print, teal tie,
  black shorts, thigh-high socks, black boots with mint soles, the long black ribbons.

A pose without a PNG uses the baked sheet (`../<agent>.png`, 16 frames of 48x96 from
scripts/build_pixel_office.py and scripts/lumi_hd.py); without that sheet, a placeholder drawn
in her palette. The browser console lists which poses use the baked sheet.

## Files (36)

### Main: standard uniform: white jacket, teal tie, twin tails

- `lumi-main-idle.png`: standing still
- `lumi-main-walk.png`: walk cycle (4 frames looks right)
- `lumi-main-sit.png`: seated
- `lumi-main-type.png`: seated, typing
- `lumi-main-read.png`: seated, reading
- `lumi-main-stretch.png`: standing, arms up
- `lumi-main-drink.png`: seated with a coffee mug
- `lumi-main-eat.png`: seated, eating a rice ball
- `lumi-main-sleep.png`: slumped asleep at the desk

### Minecraft: backpack, pickaxe in hand, green tie

- `lumi-minecraft-idle.png`: standing still
- `lumi-minecraft-walk.png`: walk cycle (4 frames looks right)
- `lumi-minecraft-sit.png`: seated
- `lumi-minecraft-type.png`: seated, typing
- `lumi-minecraft-read.png`: seated, reading
- `lumi-minecraft-stretch.png`: standing, arms up
- `lumi-minecraft-drink.png`: seated with a coffee mug
- `lumi-minecraft-eat.png`: seated, eating a rice ball
- `lumi-minecraft-sleep.png`: slumped asleep at the desk

### Office: shirt, thin violet glasses, lanyard badge

- `lumi-office-idle.png`: standing still
- `lumi-office-walk.png`: walk cycle (4 frames looks right)
- `lumi-office-sit.png`: seated
- `lumi-office-type.png`: seated, typing
- `lumi-office-read.png`: seated, reading
- `lumi-office-stretch.png`: standing, arms up
- `lumi-office-drink.png`: seated with a coffee mug
- `lumi-office-eat.png`: seated, eating a rice ball
- `lumi-office-sleep.png`: slumped asleep at the desk

### System: the mint cat-ear hoodie (the sheet's 4th pose), rose tie

- `lumi-system-idle.png`: standing still
- `lumi-system-walk.png`: walk cycle (4 frames looks right)
- `lumi-system-sit.png`: seated
- `lumi-system-type.png`: seated, typing
- `lumi-system-read.png`: seated, reading
- `lumi-system-stretch.png`: standing, arms up
- `lumi-system-drink.png`: seated with a coffee mug
- `lumi-system-eat.png`: seated, eating a rice ball
- `lumi-system-sleep.png`: slumped asleep at the desk
