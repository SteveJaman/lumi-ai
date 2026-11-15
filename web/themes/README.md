# Lumi themes

Each folder here is a theme for the web app **and** Lumi Island. `web/themes/` is the
source; `python scripts/sync_themes.py` copies it (and `theme-engine.js`,
`pipeline.js`) into `island/src/` so the island bundles the same files.

```
themes/
  index.json            the list the pickers show
  engine.css            canvas placement, loaded before any theme
  <id>/
    theme.json          manifest (below)
    style.css           the look
    background.js       optional animated canvas
    assets/             optional images
```

## theme.json

| key | type | meaning |
|---|---|---|
| `id` | string | folder name |
| `name`, `description`, `author`, `version` | string | shown in the pickers |
| `dark` | bool | the theme is designed on a dark base |
| `palette` | object | named colours, set as `--lt-<name>` on `<html>`. Use `primary`, `secondary`, `accent`, `glow`, `bg`, `surface`, `text`, `muted`, `danger` (more are fine) |
| `fonts` | object | `display`, `body`, `mono` CSS font stacks (local fonts only: the island's CSP blocks web fonts) |
| `style` | string | stylesheet file, usually `style.css` (omit for none) |
| `background` | object or null | `{ "script": "background.js", "fps": 30, "maxDpr": 1.5, "options": {...} }`; fps is capped at 30 |
| `cursor` | `"sparkle"` or null | cursor effect |
| `room` | object | Pipeline lighting: `{ "tint": "#ff7ac8", "alpha": 0.16, "blend": "soft-light", "glow": "#ffd1ea" }` |

## style.css

The engine sets `data-lumi-theme="<id>"` and `data-surface="web" | "island"` on `<html>`,
so scope every rule, e.g. `html[data-lumi-theme="sakura"][data-surface="web"] .card { ... }`.
The web app's colours are CSS variables (`--bg`, `--bg-elev`, `--accent`, `--text`, ... in
`web/style.css`); the island's are `--bg`, `--panel`, `--line`, `--teal`, `--pink`, ... in
`island/src/island.css`. Map `--lt-*` onto those and most of the UI follows.

## background.js

```js
LumiThemes.registerBackground('<id>', (canvas, ctx, { palette, options, surface }) => ({
  frame(t, dt, w, h) { /* draw; w/h are device pixels */ },
  resize(w, h) {},           // optional
  destroy() {},              // optional
}));
```

The engine calls `frame` at most 30 times a second, never while the window is hidden
or minimised, and once only for `prefers-reduced-motion`.
