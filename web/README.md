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
├── app.js          SPA router, shared helpers, and all seven pages (~1700 lines)
├── style.css       Design system: tokens and components
├── login.html      Standalone login/register page
├── login.js        Form handling for login.html
├── setup.html      First-run wizard markup
├── setup.js        Four-step wizard logic
└── README.md       This file
```

`style.css` is linked by every HTML file. `app.js` is the only script the SPA
loads, so nearly every frontend change happens there or in `style.css`.

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

**SPA routing.** Routes are hash-based. The `navigate()` function in `app.js`
updates the hash, and `render()` swaps the contents of `#main-content` for the
matching page. Shared state lives in the `state` object near the top of
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

## Pages

| Route | View function in `app.js` | What it does |
|---|---|---|
| `#/chat` | `renderChatView()` | Message list with Fish Audio tag chips, agent-mode toggle chip, voice controls (TTS chip and hold-to-talk), file upload by button, drag-and-drop, or paste, and an attachment chip strip above the input |
| `#/dashboard` | `renderDashboard()` | Stat cards plus a live event stream from the log hub at `127.0.0.1:5001`, filterable by category chip |
| `#/memory` | `renderMemory()` | Table of stored facts with semantic search, sort (newest, oldest, importance), expandable rows showing full metadata, inline edit, delete, add through a modal, and export to JSON |
| `#/persona` | `renderPersona()` | Form-based editor for `config/lumi.yaml` with a section sidebar, trait sliders, list and map editors, raw YAML mode, preview, revert, and save |
| `#/models` | `renderModels()` | Four sections: currently loaded models with unload; installed models with filter and load/delete, plus pull progress; a provider registry (OpenAI, Anthropic, Gemini, Groq, OpenRouter) with API key config; and a routing form (main model, fast model, context sizes, keep-alive) |
| `#/integrations` | `renderIntegrations()` | Cards for Ollama, VTube Studio, Discord, Fish Audio, and Browser, each with a status pill, a Configure modal for env-backed fields, a Test button, and a module toggle where applicable |
| `#/settings` | `renderSettings()` | Four sub-tabs: General (timezone, Ollama endpoint), Security (change password, active session list with revoke), Advanced (five reset targets: `history`, `session`, `self_model`, `logs`, `memory`), and About (version, uptime, modules loaded) |

### Agent mode toggle

The agent mode toggle is a shared status chip in the chat status strip. It
reflects a boolean held on the backend prompter. Clicking it POSTs to
`/api/agent/toggle`. The chip state is polled from `/api/status` every 2 seconds,
so toggling from the terminal (`!agent_on`) also flips the chip within a couple
of seconds.

### Thinking indicator

While Lumi is generating a reply, an assistant-styled bubble with three pulsing
dots appears at the bottom of the chat log. It is created by
`updateTypingIndicator()` in `app.js`, driven by `state.status.ai_thinking` from
the 2-second status poll, and it is removed when the next assistant message
lands in `/api/history`. The indicator is a client-only affordance; the backend
does not emit a "thinking" message into `signals.history`.

If the indicator does not appear, check that `updateTypingIndicator()` is
tracking the last message's `role` (not just the history length) when deciding
whether a reply has arrived. A user message grows history too, and can be
mistaken for the assistant's reply if the check only compares lengths.

### Theme toggle

The light/dark toggle lives at the bottom of the sidebar
(`#theme-toggle`). The choice is persisted to `localStorage` under
`lumi-theme`. On first load the theme follows the OS via
`prefers-color-scheme`; after that, the user's explicit choice wins.

The theme attribute (`data-theme="light|dark"`) is applied to
`document.documentElement` by `initTheme()` in `app.js`. To avoid a
flash-of-wrong-theme on load, `index.html` runs a small inline script in the
`<head>` that sets the attribute before `style.css` parses. Do not move that
script; `initTheme()` is still needed after the DOM is ready to keep the toggle
button's icon in sync.

Colors are defined as CSS custom properties under
`:root, html[data-theme="light"]` and `html[data-theme="dark"]` in `style.css`.
Add new colors as tokens in both blocks rather than hardcoding them in
components.

## Authentication

The session cookie is set server-side by `/auth/login`, and every API call sends
it automatically via `credentials: 'same-origin'`. The cookie is `HttpOnly` and
`SameSite=Lax`. It survives a browser restart for up to 30 days (sliding TTL).

The CSRF cookie is separate (`lumi_csrf`). It is readable by JS (not
`HttpOnly`), and the client echoes its value back in the `X-CSRF-Token` header
on every state-changing request, populated from `state.csrf`. The `api()` helper
in `app.js` handles both, so use it instead of calling `fetch` directly.

Multipart uploads (`/api/chat/upload`, `/api/voice_message`) still send
`X-CSRF-Token` but skip the JSON body path. The `api()` helper does not cover
them; see the debugging note below.

## Design system

`style.css` is a single plain-CSS file. **No preprocessor**: do not introduce
Sass, PostCSS, or Tailwind.

| Convention | Detail |
|---|---|
| Tokens | Colors, spacing, and typography are CSS custom properties in the `:root { ... }` block at the top (`--bg`, `--accent`, and so on) |
| Palette | Character-sheet pastel: mint green `#81D4C6`, ice blue `#B9F1FA`, pale peach `#F3D6CF`, lavender-gray `#D9D9DE`. Light mode uses the raw palette with accents darkened for text contrast; dark mode uses the raw mint and ice, with backgrounds on a cool ice-blue cast |
| Semantic tokens | Status surfaces (`.model-badge-loaded`, `.integration-status.state-ok`, `.settings-danger-note`, `.btn-danger`, and similar) read from semantic surface tokens (`--success-bg/-border/-text`, `--warn-*`, `--error-*`) rather than hardcoded hex values. Add new status components the same way |
| Class names | BEM-ish but flat: `.page-header`, `.msg-bubble`, `.memory-row` |
| Chat bubbles | `.msg-*` classes, with role-specific background (`.msg.user` uses the accent background, `.msg.assistant` uses the elevated surface). Attachment badges inside bubbles use `.msg-attachment-badge`. |

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

Reload the tab and the new nav entry appears.

## Voice in browser

Both features are opt-in.

| Feature | Behavior |
|---|---|
| TTS playback | A "Voice: ON/OFF" chip. When on, the tab joins a socket.io room and plays incoming audio. |
| Hold to talk | Records with `MediaRecorder` while the button is held, then POSTs the audio to `/api/voice_message`. The server transcribes it and injects the text as a user message. |

The voice socket is opt-in per tab and per browser session. Turning the Voice
chip off does not close the socket; it stops playing audio, and the tab still
receives `tts_audio` events.

The socket is used only for TTS playback. Chat messages are still delivered by
polling `/api/history` every 1.5 seconds. The socket does not carry chat.

## File uploads

There are three ways to attach a file in the chat view: the paperclip button,
drag-and-drop anywhere over the chat area, and paste from the clipboard
(screenshots land here).

**Accepted files.** The server accepts these extensions, up to 25 MB per file:

| Kind | Extensions |
|---|---|
| Documents | PDF, DOCX |
| Images | PNG, JPG, GIF, WebP, BMP |
| Text-like | TXT, MD, JSON, YAML, CSV, LOG |

**Server behavior.** The file is saved into
`data/library/{documents,images,text}/` with a timestamp prefix. A `.meta.md`
sidecar is written next to it, and the sidecar is indexed into ChromaDB
immediately so Lumi can retrieve it on later turns. See
[`../data/README.md`](../data/README.md) for the layout.

**Duplicates.** If a byte-identical file already exists, the server returns
HTTP 409 and the client shows a Replace/Cancel modal. **Replace** deletes the old
file, its sidecar, and its ChromaDB chunks, then saves the new one.

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
`--text-mute` on the ice-blue hue (`#D7EAF0`, `#8AB0BC`, `#5E7A85`) rather than
a neutral slate. If they have been reverted to neutral grays, the ice-blue tint
is gone.

## See also

- `../docs/architecture.md`: where the web client sits in the turn flow
- `../modules/socketIO.py`: the backend that serves this folder and defines
  every route
- `../modules/README.md`: the backend file map (auth, persona editor, model
  manager, integrations)