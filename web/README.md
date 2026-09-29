# Lumi Web Client

The local web client for Lumi, served at `http://127.0.0.1:5000`. It is plain HTML, CSS, and JavaScript: no framework, no bundler, no `package.json`. **There is no build step.** Edit a file, reload the tab, and the change is live. You do not need npm to work in this folder.

## Quick start

With Lumi running, open `http://127.0.0.1:5000`. On first run the server redirects you to `/setup` (the four-step wizard), and afterward to `/login` until you have a session. Once signed in, `/` serves the SPA.

To change something, edit the file in `web/` and reload. There is nothing to compile or restart for frontend changes.

<!-- VERIFY: confirm that static files under web/ are not cached by the server; if they are, add a hard-reload note here. -->

## Contents

```text
web/
├── index.html      SPA shell only (sidebar, #main-content); auth lives on /login
├── app.js          SPA router, shared helpers, and all seven pages (~1500 lines)
├── style.css       Design system: tokens and components
├── login.html      Standalone login/register page
├── login.js        Form handling for login.html
├── setup.html      First-run wizard markup
├── setup.js        Four-step wizard logic
└── README.md       This file
```

`style.css` is linked by every HTML file. `app.js` is the only script the SPA loads, so nearly every frontend change happens there or in `style.css`.

<!-- VERIFY: fill in approximate line counts for style.css, login.*, setup.* (run wc -l web/*). -->

## Architecture

```text
   Browser (127.0.0.1:5000)                        Lumi process
  ┌───────────────────────────┐                ┌──────────────────────────┐
  │  index.html + app.js      │  GET /, /web/* │  modules/socketIO.py     │
  │    hash router            │◄══════════════►│    route gating          │
  │    #main-content          │  /api/history  │    /api/* endpoints      │
  │    state, api() helper    │     every 1.5s │    /auth/login           │
  │                           │  /api/status   │            │             │
  │                           │     every 2s   │            ▼             │
  └───────────────────────────┘                │    signals.history       │
                                               │      ▲                   │
                                               │      └── terminal input  │
                                               └──────────────────────────┘
```

**Server-side routing.** `/` serves `index.html` to authenticated users and redirects everyone else to `/login` or `/setup`. This gating happens in `modules/socketIO.py`, not in client-side JS, so do not add redirect logic to `app.js`.

**SPA routing.** Routes are hash-based. The `navigate()` function in `app.js` updates the hash, and `render()` swaps the contents of `#main-content` for the matching page. Shared state lives in the `state` object near the top of `app.js`; read it before changing a page, since views read from it rather than keeping their own copies.

**Data flow.** Every page fetches from `/api/*` endpoints defined in `modules/socketIO.py`. Two polling loops matter for understanding how the client stays in sync:

- Chat polls `/api/history` every 1.5 seconds.
- Status polls `/api/status` every 2 seconds.

This is why text typed at the terminal appears in the web chat. Both write to and read from the same `signals.history` on the backend, and the browser picks up new entries on its next poll.

**Static assets.** Served from `/web/*`. The full list of API routes is in `modules/socketIO.py` and is not duplicated here.

## Pages

| Route | View function in `app.js` | What it does |
|---|---|---|
| `#/chat` | `renderChatView()` | Conversation with Lumi, message input, voice controls |
| `#/dashboard` | `renderDashboard()` | Live status overview |
| `#/memory` | `renderMemory()` | Browse and manage stored memories |
| `#/persona` | `renderPersona()` | Edit the persona |
| `#/models` | `renderModels()` | Manage models and LLM providers |
| `#/integrations` | `renderIntegrations()` | Configure external integrations |
| `#/settings` | `renderSettings()` | Application settings |

<!-- VERIFY: the "what it does" column is inferred from function names; confirm against app.js. -->

## Authentication

The session cookie is set server-side by `/auth/login`, and every API call sends it automatically via `credentials: 'same-origin'`. State-changing requests also add an `X-CSRF-Token` header populated from `state.csrf`. The `api()` helper in `app.js` handles both, so use it instead of calling `fetch` directly.

<!-- VERIFY: confirm state.csrf and the api() helper name against app.js. -->

## Design system

`style.css` is a single plain-CSS file. **No preprocessor**: do not introduce Sass, PostCSS, or Tailwind.

| Convention | Detail |
|---|---|
| Tokens | Colors, spacing, and typography are CSS custom properties in the `:root { ... }` block at the top (`--bg`, `--accent`, and so on) |
| Palette | GitHub-dark |
| Class names | BEM-ish but flat: `.page-header`, `.msg-bubble`, `.memory-row` |

Scan the token block before adding styles and reuse a token instead of hardcoding a value. Name new classes after the component, not the page.

## Adding a page

1. Add an entry to the `NAV` array in `app.js`.
2. Add a `case` for the new route in `render()`.
3. Write a `renderYourPage(main)` function that fills `#main-content` (the `main` argument).
4. If the page needs data, add a `/api/your-thing` route in `modules/socketIO.py` and fetch it through `api()`.

Reload the tab and the new nav entry appears.

## Voice in browser

Both features are opt-in.

| Feature | Behavior |
|---|---|
| TTS playback | A "Voice: ON/OFF" chip. When on, the tab joins a socket.io room and plays incoming audio. |
| Hold to talk | Records with `MediaRecorder` while the button is held, then POSTs the audio to `/api/voice_message`. The server transcribes it and injects the text as a user message. |

## Debugging

**Redirected to `/login` or `/setup`.** That is server-side gating working as intended. Finish setup or sign in; do not try to bypass it in JS.

**Terminal messages do not show in the web chat.** Check the browser's network tab for `/api/history` requests every 1.5 seconds. If they are failing, the session may have expired; reload and sign in again.

**A POST fails with a permission error.** The `X-CSRF-Token` header is likely missing. Route the request through `api()` so the token and cookie are attached.

**New nav entry appears but the page is blank.** Check that the `case` in `render()` matches the route string in `NAV` exactly, and that your `renderYourPage(main)` writes into `main` rather than a fresh element.

**Styles look wrong after an edit.** Reload without cache, then check whether a token in `:root` already covers what you are setting.

## See also

- `../docs/architecture.md`: where the web client sits in the turn flow
- `../modules/socketIO.py`: the backend that serves this folder and defines every route
- `../modules/README.md`: the backend files (auth, persona editor, model manager, integrations)