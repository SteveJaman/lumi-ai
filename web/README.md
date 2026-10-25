# Lumi Web Client

The local web client for Lumi, served at `http://127.0.0.1:5000`. It is plain
HTML, CSS, and JavaScript: no framework, no bundler, no `package.json`.
**There is no build step.** Edit a file, reload the tab, and the change is live.
You do not need npm to work in this folder.

## Quick start

With Lumi running, open `http://127.0.0.1:5000`. On first run the server
redirects you to `/setup`, which shows a single-screen register form. A four-step
progress indicator sits at the top of that form, but it is decorative for now:
steps 2 to 4 are placeholders. After you register, the server redirects to
`/login` until you have a session. Once signed in, `/` serves the SPA.

To change something, edit the file in `web/` and reload. There is nothing to
compile or restart for frontend changes.

## Contents

```text
web/
├── index.html      SPA shell only (sidebar, #main-content); auth lives on /login
├── app.js          SPA router, shared helpers, and all seven pages (~4900 lines)
├── style.css       Design system: tokens, components, responsive and motion rules
├── theme-init.js   Blocking <head> script that applies the theme before first paint
├── login.html      Standalone login/register page
├── login.js        Form handling for login.html
├── setup.html      First-run wizard markup
├── setup.js        Four-step wizard logic
├── Lumi.png        Avatar used in the sidebar and on assistant messages
└── README.md       This file
```

`style.css` and `theme-init.js` are linked by every HTML file. `app.js` is the
only module script the SPA loads, so nearly every frontend change happens there
or in `style.css`.

## Architecture

```text
   Browser (127.0.0.1:5000)                        Lumi process
  ┌───────────────────────────┐                ┌──────────────────────────────┐
  │  index.html + app.js      │  GET /, /web/* │  modules/socketIO.py         │
  │    hash router            │◄══════════════►│    route gating              │
  │    #main-content          │  /api/history  │    /api/* endpoints          │
  │    state, api() helper    │     every 1.5s │      POST /api/chat/upload   │
  │                           │  /api/status   │      POST /api/voice_message │
  │                           │     every 2s   │      POST /api/agent/toggle  │
  │                           │                │    /auth/login               │
  │  socket.io (TTS audio     │  tts_audio     │            │                 │
  │   playback only)          │◄═══════════════│            ▼                 │
  └───────────────────────────┘                │    signals.history           │
                                               │      ▲                       │
                                               │      └── terminal input      │
                                               └──────────────────────────────┘
```

**Server-side routing.** `/` serves `index.html` to authenticated users and
redirects everyone else to `/login` or `/setup`. This gating happens in
`modules/socketIO.py`, not in client-side JS, so do not add redirect logic to
`app.js`.

**SPA routing.** Routes are hash-based. Sidebar entries are real
`<a href="#/view">` links, so the browser handles keyboard focus and
middle-click; the `hashchange` listener calls `navigate()`, and `render()`
swaps the contents of `#main-content` for the matching page. On load,
`viewFromHash()` opens whatever page the URL names (a reload on `#/memory`
stays on Memory), and an unknown hash falls back to chat. The active link gets
`aria-current="page"`. Shared state lives in the `state` object near the top of
`app.js`; read it before changing a page, since views read from it rather than
keeping their own copies.

**Data flow.** Every page fetches from `/api/*` endpoints defined in
`modules/socketIO.py`. Two polling loops matter for understanding how the client
stays in sync:

- Chat polls `/api/history` every 1.5 seconds.
- Status polls `/api/status` every 2 seconds.

This is why text typed at the terminal appears in the web chat. Both write to and
read from the same `signals.history` on the backend, and the browser picks up new
entries on its next poll.

**Socket.io.** `app.js` also opens a socket.io connection, but only for TTS audio
playback. It does not carry chat, which is polling-only. This is a common source
of confusion when reading the code.

**Static assets.** Served from `/web/*`. The full list of API routes is in
`modules/socketIO.py` and is not duplicated here. The three routes above are the
ones that matter most for understanding what the SPA does.

**Errors.** Every `/api/*` and `/auth/*` response is JSON, including failures:
the server's error middleware returns `{"error": "..."}` with the HTTP status
even when a handler crashes. `api()` throws an `Error` whose message is that
`error` string and whose `.status` is the HTTP status, so callers can show
the message directly.

**History payload.** `/api/history` returns attachments as `{name, kind}`
only. The extracted text stays server-side, where the prompter reads it; the
chat only needs it for the badge. It also leaves out game events (entries with
`source: "sdk_event"`) and anything marked `hidden`: they stay in Lumi's
history for the model, but the chat shows only her reaction. `visibleHistory()`
in `app.js` applies the same filter as a backstop.

## Pages

| Route | View function in `app.js` | What it does |
|---|---|---|
| `#/chat` | `renderChatView()` | Message list with Fish Audio tag chips, mood chip, thinking indicator, "↓ Latest" jump button, agent-mode toggle chip, voice controls (TTS chip and hold-to-talk), file upload by button, drag-and-drop, or paste, and an attachment chip strip above the input |
| `#/dashboard` | `renderDashboard()` | Stat cards plus the event stream from `GET /api/log_events` (a session-authenticated proxy of the log hub; refreshed by the Refresh button), filterable by category chip. Remote sessions see the stats and a "Log events are local-only" note |
| `#/games` | `renderGames()` | Host-only. The **Minecraft AI** card: a switch (`POST /api/game/toggle`), the game agent's current thought, order and stats, an **AI directive / nudge** box (`POST /api/games/minecraft/directive`, cleared after sending), the bot's live view, and the **HUD** (`renderHud()`). The HUD has a connection line, pixel-art hearts, hunger and armor (inline SVG sprites generated once into CSS variables), the 27-slot inventory and hotbar with icons from `GET /api/games/minecraft/item/{name}.png` (falling back to the item's name), a game-style tooltip, a Drop 1 / Drop stack menu (`POST /api/games/minecraft/action`), and skin upload (`POST /api/games/minecraft/skin`). Above them, **Server connection**: host and port with **Reconnect** and a button per detected LAN world (`POST /api/games/minecraft/server`; the fields are filled from the current server but never while you type). Polled from `GET /api/game/status` every 2 s while the page is open; the HUD re-renders only when its data changes (`fingerprint()`) |
| `#/memory` | `renderMemory()` | Table of stored facts with semantic search, sort (newest, oldest, importance), rows that expand to full metadata by click or Enter/Space, inline edit, delete, add through a modal, and export to JSON |
| `#/persona` | `renderPersona()` | Form-based editor for `config/lumi.yaml` with a section sidebar, trait sliders, list and map editors, raw YAML mode, preview, revert, and save |
| `#/models` | `renderModels()` | Four sections: currently loaded models with unload; installed models with filter and load/delete, plus pull progress; a provider registry (OpenAI, Anthropic, Gemini, Groq, OpenRouter) with API key config; and a routing form (main model, fast model, context sizes, keep-alive) |
| `#/integrations` | `renderIntegrations()` | Cards for Ollama, VTube Studio, Discord, Fish Audio, Browser, and OBS Studio, each with a status pill, a Configure modal for env-backed fields, a Test button, and a module toggle where applicable |
| `#/stream` | `renderStream()` | **Hidden unless `STREAMING_ENABLED`.** Optional stream player embed, OBS card (status, scene buttons, Go live / Stop with confirmation), subtitle setup, and remote-access status. Polls `/api/stream/status` every 3 s while open; `stopStreamPolling()` runs on navigation |
| `#/settings` | `renderSettings()` | Four sub-tabs: General (timezone, Ollama endpoint), Security (change password, active session list with revoke), Advanced (five reset targets: `history`, `session`, `self_model`, `logs`, `memory`), and About (version, uptime, modules loaded) |

### Agent mode toggle

The agent mode toggle is a shared status chip in the chat status strip. It
reflects a boolean held on the backend prompter. Clicking it POSTs to
`/api/agent/toggle`. The chip state is polled from `/api/status` every 2 seconds,
so toggling from the terminal (`!agent_on`) also flips the chip within a couple
of seconds.

### Chat rendering and scrolling

`refreshHistory()` polls `/api/history?limit=100` and picks the cheapest update
that keeps the DOM in sync:

| Case | What happens |
|---|---|
| Nothing changed (same fingerprint) | No DOM work |
| New messages after an unchanged prefix | `appendNewMessages()` appends them with the pop-in animation |
| Same length, only the newest entry changed in place | `patchLastMessage()` replaces that one bubble, without animation. The backend does not stream replies into history today; this path is ready for it |
| Anything else | `renderChat()` rebuilds the list off-DOM and swaps it in; redrawn rows get `.msg-static` so they don't re-animate |

Scroll rules:

- If you were within 80px of the bottom, the view stays pinned to the bottom.
  Pinning is instant, not smooth, so it never reads as "scrolled away".
- If you had scrolled up, a full redraw restores your position instead of
  jumping to the top.
- While you are scrolled up, a floating **↓ Latest** button appears. It becomes
  **↓ New messages** when something arrived meanwhile, and clicking it scrolls
  smoothly to the bottom.
- Your own sent or spoken message is appended immediately through
  `appendLocalMessage()`, in the same shape `/api/history` returns, and always
  scrolls into view. The next poll then sees a matching prefix and appends
  rather than redraws.

`.chat-scroll` deliberately has no `scroll-behavior: smooth`; programmatic
jumps would otherwise animate through the whole history.

### Thinking indicator

While Lumi is generating a reply, an assistant-styled bubble with three pulsing
dots sits at the bottom of the chat log. `updateTypingIndicator()` creates it
when `state.status.ai_thinking` is true. When thinking starts, it remembers a
fingerprint of the newest history entry (`_thinkingAnchor`). The bubble is
removed once the newest entry is an assistant message that differs from that
anchor, or when the status poll reports thinking has stopped. Anchoring on
content rather than history length keeps this working when the 100-message
window is full.

The function runs on every status tick and after every chat render, so the two
polls can't drift apart. If messages arrive while thinking, the bubble is moved
back below them. The indicator is client-only; the backend never writes a
"thinking" message into `signals.history`.

### Mood accent

When the newest assistant message carries a canonical emotion tag
(`[happy]`, `[calm]`, …), `applyMoodAccent()` tints the app with that mood's
colour for 8 seconds, then `resetMoodAccent()` hands back to the theme.

- **Per theme.** `setAccentForMood()` uses the pastel as-is on dark. On light it
  mixes the pastel 55% toward the text colour (`color-mix`) so it stays legible
  on white.
- **Reset.** Reset removes the inline `--accent`/`--accent-bg` overrides
  rather than writing a hardcoded colour, so each theme's own accent returns.
- **Transitions.** `--accent` and `--accent-bg` are registered with
  `@property` as `<color>`, so mood changes, theme toggles, and the gradient
  tint behind the chat fade over 1.2 s. Browsers without `@property` switch
  instantly.
- **Mood chip.** The chip in the status strip pops in on every mood change,
  takes the mood's tint, and fades out on reset.
- **Once per message.** A mood is triggered once per message (tracked by
  fingerprint), so a later redraw of the same message does not bring back a
  mood that has already faded.

### Dialogs

All dialogs use `openModal({ title, wide, onClose })`, which returns
`{ overlay, card, close() }`. Append your content to `card`.

- Dialogs stack instead of replacing each other, and Escape closes the
  topmost one.
- `onClose` runs exactly once, however the dialog was dismissed: close
  button, Escape, backdrop click, or your own `close()`.
- Focus returns to whatever opened the dialog.
- Only a click that starts and ends on the backdrop dismisses it, so dragging
  a text selection out of a field won't close it.

Do not build overlays by hand or remove `.modal-overlay` elements globally.
That was how an open duplicate-upload prompt used to vanish without
answering.

### Theme toggle

The light/dark toggle lives at the bottom of the sidebar (`#theme-toggle`).

- **First paint.** `theme-init.js` is a plain blocking `<script>` in the
  `<head>` of `index.html`, `login.html`, and `setup.html`. It sets
  `data-theme` on `<html>` from `localStorage['lumi-theme']`, or from
  `prefers-color-scheme` when nothing is saved, before the page first paints.
  Keep it blocking and ahead of `style.css`.
- **Saving.** Only an explicit toggle writes `lumi-theme`. Until the user picks
  a theme, `initTheme()` keeps following live OS changes.
- **Button icon.** `initTheme()` and `applyTheme()` keep the toggle button's
  icon and title in sync.

Colors are defined as CSS custom properties under
`:root, html[data-theme="light"]` and `html[data-theme="dark"]` in `style.css`.
Add new colors as tokens in both blocks rather than hardcoding them in
components or in `app.js`.

### Narrow screens and motion

At `max-width: 760px`:

- **Sidebar.** It becomes a compact top bar with icon-only navigation; link
  titles carry the labels. The brand block, status pills, GitHub link, and
  user name are hidden.
- **Memory rows** wrap to two lines.
- **Dashboard event rows** stack.
- **Dialogs** scroll within the viewport.
- **Chat input** uses a shorter placeholder.

The shell uses `100dvh` so mobile browser chrome doesn't hide the input.

Under `prefers-reduced-motion: reduce`, message pop-ins, the mood chip and
its emoji, the jump button, the starfield, and the accent fade are all
disabled.

## Authentication

The session cookie is set server-side by `/auth/login`, and every API call sends
it automatically via `credentials: 'same-origin'`. The cookie is `HttpOnly` and
`SameSite=Lax`. It survives a browser restart for up to 30 days (sliding TTL).

The CSRF cookie is separate (`lumi_csrf`). It is readable by JS (not
`HttpOnly`), and the client echoes its value back in the `X-CSRF-Token` header
on every state-changing request, populated from `state.csrf`. The `api()` helper
in `app.js` handles both, so use it instead of calling `fetch` directly.

Multipart uploads (`/api/chat/upload`, `/api/voice_message`) still send
`X-CSRF-Token` but skip the JSON body path. The server checks the header only
for these two routes. The `api()` helper does not cover them; see the
debugging note below.

`/auth/logout` currently has no CSRF or origin check, so a cross-site request
can sign you out (it cannot do anything else).

## Design system

`style.css` is a single plain-CSS file. **No preprocessor**: do not introduce
Sass, PostCSS, or Tailwind.

| Convention | Detail |
|---|---|
| Tokens | Colors, spacing, and typography are CSS custom properties in the `:root { ... }` block at the top (`--bg`, `--accent`, and so on) |
| Palette | Character-sheet pastel: mint green `#81D4C6`, ice blue `#B9F1FA`, pale peach `#F3D6CF`, lavender-gray `#D9D9DE`. Light mode uses the raw palette with accents darkened for text contrast; dark mode uses the raw mint and ice, with backgrounds on a cool ice-blue cast |
| Semantic tokens | Status surfaces (`.model-badge-loaded`, `.integration-status.state-ok`, `.settings-danger-note`, `.btn-danger`, and similar) read from semantic surface tokens (`--success-bg/-border/-text`, `--warn-*`, `--error-*`) rather than hardcoded hex values. Add new status components the same way |
| Class names | BEM-ish but flat: `.page-header`, `.msg-bubble`, `.memory-row` |
| Chat bubbles | `.msg-*` classes, with role-specific background (`.msg.user` uses the accent background, `.msg.assistant` uses the elevated surface). Attachment badges inside bubbles use `.msg-attachment-badge`. `.msg-static` turns off the pop-in for redrawn rows. `.msg` is reserved for chat rows; the dashboard uses `.ev-msg` so it doesn't inherit chat layout and animation |
| Escaping | Untrusted text goes in via `textContent`, or through `escapeHtml()` when building an HTML string. That includes host names, log hub fields, file names, and anything else from the server or the user |
| Hidden state | The global `.hidden` is `display: none !important`. Elements that fade in and out (such as `.chat-jump.is-away`) use their own class so they can transition |
| Accessibility | Interactive elements are real `<a>`/`<button>` or carry `tabIndex` plus a key handler; `:focus-visible` rings use `--accent` |

`style.css` contains the component rules for the entire app: memory table,
persona editor, models and providers, integrations, settings, setup wizard,
chat, and dashboard. They are organized with section-header comments, so scan
for the relevant section before adding new rules.

Scan the token block before adding styles and reuse a token instead of hardcoding
a value. Name new classes after the component, not the page.

## Adding a page

1. Add an entry to the `NAV` array in `app.js`.
2. Add a `case` for the new route in `render()`.
3. Write a `renderYourPage(main)` function that fills `#main-content` (the
   `main` argument).
4. If the page needs data, add a `/api/your-thing` route in
   `modules/socketIO.py` and fetch it through `api()`.
5. If the page needs to change shared state on the backend (the agent toggle, a
   settings value, a memory edit), send the CSRF token via
   `api(..., { csrf: true })`. Forgetting this returns 403 from the server.
6. Use `openModal()` for any dialog and `escapeHtml()` for any server or user
   text you put into an HTML string.
7. Check the page at phone width (≤ 760px). Fixed-width grid columns are the
   usual thing that needs a rule in the narrow-screen media query.

Reload the tab and the new nav entry appears.

## Voice in browser

Both features are opt-in.

| Feature | Behavior |
|---|---|
| TTS playback | A "Voice: ON/OFF" chip, remembered in `localStorage['lumi_voice']`. The tab sends `web_voice_optin {enabled}` on every toggle and on every (re)connect; the server only sends `tts_audio` to opted-in sockets. |
| Hold to talk | Records with `MediaRecorder` while the button is held, then POSTs the clip to `/api/voice_message` with `X-CSRF-Token`. The server transcribes it with faster-whisper and injects the text as a user message. |

**One socket per page load.** `ensureVoiceSocket()` creates the socket.io
connection once and never opens a second one. socket.io handles reconnects,
and the `connect` handler re-sends the opt-in. An earlier version opened a
new socket whenever the old one was still connecting, so every extra socket
played each clip again.

**Recording format.** `pickRecorderMime()` asks `MediaRecorder.isTypeSupported()`
for the first of `audio/webm;codecs=opus`, `audio/webm`, `audio/ogg;codecs=opus`,
`audio/mp4`. The blob is then labelled with `recorder.mimeType`, meaning what
the browser actually produced, and the upload filename carries the matching
extension (`clip.webm`, `clip.ogg`, `clip.mp4`, …). Safari records mp4;
Chrome and Firefox record webm or ogg. The server keeps that extension for the
temp file it hands to faster-whisper.

**Press and release.**

- The button uses pointer events with pointer capture, so it works with mouse,
  touch, and pen, and sliding a finger off it still ends the recording.
- `touch-action: none` on `#mic-toggle` stops a touch drag from turning into a
  scroll, which would cancel the recording.
- If you release before the microphone permission or device open finishes,
  the stream is released and no recording starts.
- In browsers without `MediaRecorder`, the button is disabled with an
  explanatory tooltip.

**Limits.** Clips over 10 MB are rejected with HTTP 413. Clips under ~1 KB are
dropped client-side as accidental taps.

The socket is used only for TTS playback. Chat messages are still delivered by
polling `/api/history` every 1.5 seconds. The socket does not carry chat.

## File uploads

There are three ways to attach a file in the chat view: the paperclip button,
drag-and-drop anywhere over the chat area, and paste from the clipboard
(screenshots land here).

**Accepted files.** The server accepts these extensions, up to 25 MB per file:

| Kind | Extensions |
|---|---|
| Documents | PDF, DOCX, DOC |
| Images | PNG, JPG, GIF, WebP, BMP |
| Text-like | TXT, MD, JSON, YAML, CSV, LOG |

**Server behavior.** The file is saved into
`data/library/{documents,images,text}/` with a timestamp prefix. A `.meta.md`
sidecar is written next to it, and the sidecar is indexed into ChromaDB
immediately so Lumi can retrieve it on later turns. See
[`../data/README.md`](../data/README.md) for the layout.

**What comes back.** The upload response carries the extracted text (VLM report
for images, parsed text for documents, capped at 15,000 characters), not the
image bytes. `sendChat()` forwards each attachment as `{name, stored_path, kind,
text}`. Because no base64 image data is sent back, image attachments stay well
under the server's 1 MB JSON body limit.

**Duplicates.** If a byte-identical file already exists, the server returns
HTTP 409 and the client shows a Replace/Cancel dialog naming both files. Cancel
has default focus, so Enter keeps the original. **Replace** deletes the old
file, its sidecar, and its ChromaDB chunks, then saves the new one.
Duplicate prompts are queued: dropping a second batch while one is open waits
its turn instead of replacing it, and every prompt resolves, so no chip is left
stuck on "uploading…".

**Attachment chips.** The chip strip above the input shows each file's name,
target subfolder, and size. Failed uploads render as red chips that stay visible
until removed.

## Debugging

**Redirected to `/login` or `/setup`.** That is server-side gating working as
intended. Finish setup or sign in; do not try to bypass it in JS.

**Terminal messages do not show in the web chat.** Check the browser's network
tab for `/api/history` requests every 1.5 seconds. If they are failing, the
session may have expired; reload and sign in again.

**A POST fails with a permission error.** The `X-CSRF-Token` header is likely
missing. Route the request through `api()` so the token and cookie are attached.

**New nav entry appears but the page is blank.** Check that the `case` in
`render()` matches the route string in `NAV` exactly, and that your
`renderYourPage(main)` writes into `main` rather than a fresh element.

**Styles look wrong after an edit.** Reload without cache, then check whether a
token in `:root` already covers what you are setting.

**Uploads fail with a red chip.** Check the browser network tab for
`POST /api/chat/upload`. A 409 means a duplicate; use the Replace flow. A 400
means an unsupported extension or the size cap. A 500 usually means the VLM is
unreachable; check the diagnostics dashboard at `127.0.0.1:5001` for `VLM:` lines.

**Lumi does not reference an uploaded file on the next turn.** The upload landed
in ChromaDB, but the passive library injection has a distance threshold (1.0 on
Chroma distance). If your phrasing does not land close enough to the file's
content, the match is filtered. Try asking with words from the file itself, or
turn on agent mode and let Lumi call `search_library` explicitly. There is no
threshold on that path.

**A multipart request fails CSRF.** The `api()` helper does not handle multipart
bodies. Manual `fetch` calls for `/api/chat/upload` and `/api/voice_message`
must send `X-CSRF-Token: state.csrf` in the headers directly. `handleFiles()`
and `startMicRecording()` in `app.js` already do this; copy the pattern.

**The chat text looks neutral-gray in dark mode.** Check that
`html[data-theme="dark"]` in `style.css` defines `--text`, `--text-dim`, and
`--text-mute` on the ice-blue hue (`#CFE9F5`, `#7EA8BF`, `#4D7186`) rather than
a neutral slate. If they have been reverted to neutral grays, the ice-blue tint
is gone.

**The light theme's accent turned mint or stayed a mood colour.** Something is
writing `--accent` inline on `<html>` and not removing it. Mood code must go
through `setAccentForMood()` and `resetMoodAccent()`, which remove the inline
property instead of setting a default.

**Voice audio plays twice.** More than one socket is connected. Check that
nothing other than `ensureVoiceSocket()` calls `io()`.

**Hold-to-talk fails with 403 or 413.** 403 means the `X-CSRF-Token` header is
missing from the `fetch`. 413 means the clip is over 10 MB.

**An error message says only "HTTP 500".** The response wasn't JSON,
which shouldn't happen on `/api/*`. Check the Lumi console for the traceback
the error middleware prints.

**Testing layout without the backend.** The frontend only needs `/auth/me`,
`/api/status`, `/api/history`, and the routes of the page you are on. A
throwaway aiohttp app that serves `web/` and returns canned JSON for those
routes is enough to screenshot both themes at desktop and phone widths with
Playwright.

## See also

- `../docs/architecture.md`: where the web client sits in the turn flow
- `../modules/socketIO.py`: the backend that serves this folder and defines
  every route
- `../modules/README.md`: the backend file map (auth, persona editor, model
  manager, integrations)