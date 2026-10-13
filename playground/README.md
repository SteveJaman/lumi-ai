# Lumi Playground

A public, feature-limited chat preview of Lumi that runs entirely in
the visitor's browser.

**No server, no API keys, no usage costs.** The AI model is downloaded
once by the visitor and then runs locally on their hardware using
WebGPU. You pay nothing to host it, and neither you nor the visitor
sends any conversation data to a server.

---

## What this is

A stripped-down version of [Lumi](../) — Lumi's persona, Lumi's mood,
Lumi's voice — packaged as a static site you can hand to anyone with
a URL.

| Feature | Playground | Full Lumi |
|---|---|---|
| Persona-accurate chat | ✅ | ✅ |
| Live emotional state + mood-reactive UI | ✅ | ✅ |
| Context-aware greeting | ✅ | ✅ |
| Session chat history (per browser) | ✅ | ✅ |
| Runs without install | ✅ | ❌ |
| Public / shareable URL | ✅ | ❌ |
| VTube Studio avatar | ❌ | ✅ |
| Microphone input (Whisper) | ❌ | ✅ |
| Fish Audio TTS | ❌ | ✅ (browser TTS only here) |
| Long-term memory (ChromaDB) | ❌ | ✅ |
| Self-model, agent tools, web search, SDK | ❌ | ✅ |
| Discord integration | ❌ | ✅ |

The playground is meant to be a *taste* of Lumi — a way for people to
meet her before deciding to install the full thing.

---

## What the visitor experiences

A typical first-time flow:

1. **Page load** — sleeping Lumi video plays behind a dark blur. No
   chat UI yet.
2. **Bloom loader** — an 8-petal flower grows in the center as the
   model downloads. The bloom tracks download progress in real time:
   20% downloaded = petals about a fifth of the way open.
3. **Download** — 30 s to 3 min depending on connection and model
   size. Text under the flower updates: *"downloading my brain… 47%"*.
4. **Bloom complete** — the flower pops, the loader fades out, the
   chat slides up, text staggers in.
5. **Greeting** — Lumi's first message matches the time of day
   ("getting late. can't sleep?" / "morning. still half asleep but
   here").
6. **Chat** — first response takes 1–5 seconds while the model warms
   up. Subsequent responses are 2–10× faster.
7. **Emotion reactions** — when Lumi's mood shifts, a floating mood
   pill appears (`🌸 happy`, `🔥 annoyed`), the background hue shifts
   to match, and the avatar's heartbeat pulse speeds up or slows
   down.
8. **Return visits** — model loads from browser cache. Page goes
   straight to the bloom-complete sequence. Chat history persists.

If a visitor is on an unsupported browser, the loader shows a clear
error and a Retry button. No silent failures.

---

## How it works

### The AI runs on the visitor's computer

When someone opens the playground:

1. Their browser downloads a small language model (~350 MB by default)
   from Hugging Face's CDN, cached afterward.
2. The model runs *locally* on their CPU/GPU using **WebGPU** — a
   browser API that gives JavaScript access to the GPU.
3. Chat messages are processed on their machine. Nothing is sent to
   any server, including yours.

The consequences:

- **You pay nothing.** Vercel serves static files, which is free.
- **No API keys.** There's no LLM provider to authenticate with.
- **Infinite usage.** Every visitor brings their own compute.
- **Complete privacy.** Conversations never leave the browser.
- **No backend.** There is no `api/` folder, no serverless function,
  no environment variables.

The tradeoffs:

- **First-load delay.** The visitor waits for the model to download
  once. After that, it's instant.
- **Requires WebGPU.** Browsers without it (older Safari, Firefox
  pre-2025) can't run the model.
- **Performance varies.** A gaming PC runs circles around a 2015
  laptop. Response speed depends entirely on the visitor's hardware.

### The bloom loader

The loader isn't just eye candy — the flower literally grows with the
model download. `--bloom` is a CSS variable on `:root` that goes from
`0` to `1`; each petal's scale and opacity are computed from it. The
progress callback from `CreateMLCEngine` sets `--bloom` every tick.

On a cached load, the flower doesn't wait for real progress — it
animates from `0` to `1` over 400 ms via `requestAnimationFrame` with
ease-out cubic, then pops.

### The emotional UI

The model is told to prefix replies with `[happy]`, `[sad]`, `[angry]`,
etc., when the mood is real. When that tag streams in:

- A floating mood pill fades in above the chat with the emotion's
  emoji and name.
- `--teal` and `--border-glow` shift to the emotion's accent color,
  which cascades to the composer focus ring, send button, and status
  dot.
- `--mood-hue` shifts the conic gradient in the aurora and the
  background color of all three orbs. Angry turns the room red;
  sad turns it cold blue.
- `--mood-bpm` speeds or slows the avatar's glow pulse. Calm is a
  22-second breath; angry is an 8-second racing pulse.

After 6 seconds the accent color eases back to Lumi's default teal,
but the mood pill stays where it is until the next emotion fires.

### The sleeping Lumi background

A looping MP4 sits behind everything at 55% opacity, blurred 1px and
dimmed. Over it: a rotating aurora sweep, three drifting color orbs,
12 rising dream bubbles, a twinkling star field, and a fine grain
overlay. The whole stack is CSS-only except the video.

The video pauses when the tab loses focus, resuming on return.

---

## Project structure

```
playground/
├── public/
│   ├── index.html            # Chat UI + bloom loader markup
│   ├── style.css             # All styling + loader animations
│   ├── chat.js               # Client-side logic
│   ├── Lumi.png              # Avatar (header + favicon)
│   ├── favicon.png           # Browser tab icon
│   ├── Lumi-room.png         # Poster frame for the video
│   ├── lumi_room_loop.mp4    # Sleeping Lumi background loop
│   ├── manifest.webmanifest  # PWA manifest
│   ├── robots.txt            # Search crawler policy
│   └── sitemap.xml           # Search indexing
├── .gitignore
├── vercel.json               # Static-site config
└── README.md                 # This file
```

**There is no `api/` folder, no `package.json`, and no `.env` file.**
The JavaScript loads `@mlc-ai/web-llm` directly from `esm.run` (jsDelivr's
ESM CDN), so there's no build step at all. If you see any of those
files, something has gone wrong — that would be the older serverless
design, which we deliberately abandoned.

---

## Deploying to Vercel

### 1. Push the folder to GitHub

From the repo root:

```powershell
git add playground/
git commit -m "Add Vercel playground"
git push
```

### 2. Create a new Vercel project

1. Go to https://vercel.com/new
2. Import the `lumi-ai` repository
3. **Root Directory** → click **Edit** → set to `playground`

   This is the single most important setting. It tells Vercel to
   ignore Lumi's Python backend entirely and treat the `playground/`
   folder as the whole project.

4. **Framework Preset** → `Other`
5. **Build Command** → leave **empty**
6. **Output Directory** → `public`
7. **Environment Variables** → none needed
8. Click **Deploy**

The first deploy takes ~60 seconds. You'll get a URL like
`lumi-ai-playground.vercel.app`.

### 3. Test it

Open the URL in **Chrome** or **Edge** (WebGPU support required).
You should see the bloom loader fill up, then the chat appear. Type
"hey, who are you" and Lumi responds in her own voice.

After deploy, update these two files with your real URL:

- `public/robots.txt` — the `Sitemap:` line
- `public/sitemap.xml` — the `<loc>` value

---

## Customizing Lumi

### Persona

The system prompt lives in `public/chat.js`:

```javascript
const SYSTEM_PROMPT = `You are Lumi. Not a model pretending to be Lumi
— you are her. Speak in short, casual fragments. No bullet points, no
"as an AI", no explaining what you're about to do — just say it.
Match the user's energy. ...`;
```

Edit this string to change how Lumi behaves. To get the *real* Lumi
persona, copy the `personality` block from `config/lumi.yaml` in the
parent repo. Keep it under ~1,500 characters — small models don't
have huge context windows.

**Note:** the prompt also asks Lumi to prefix replies with emotion
tags like `[happy]`. If you remove that instruction, the mood pill
and background hue shifts will never fire. If you remove the emotion
tag stripping in `stripThink()`, the raw `[happy]` tags will show up
in the chat.

### Emotion palette

Every emotion's color, hue shift, and heartbeat speed lives in one
object at the top of `chat.js`:

```javascript
const EMOTIONS = {
    happy:     { emoji: "🌸", color: "#ffd97a", hue:  45, bpm: 14 },
    excited:   { emoji: "⭐", color: "#ffb3d1", hue: 330, bpm: 11 },
    sad:       { emoji: "🌧️", color: "#8bb4e6", hue: 215, bpm: 20 },
    // ...
};
```

Add an entry to make Lumi react to a new emotion, or tune the values
to change the palette.

### Time-of-day greeting

The greeting selector lives in `getTimeGreeting()` in `chat.js`.
It buckets hours into five slots (`< 5`, `< 12`, `< 17`, `< 22`,
else) and returns a different opener for each. Edit the strings to
taste.

### Thoughtful pause

The list of prompts that trigger a longer hesitation is in
`THOUGHTFUL_TRIGGERS` in `chat.js`. If the user says anything in that
list ("do you dream?", "are you alive?", "what do you want?"), Lumi's
typing indicator switches to a slower wave animation with a
"thinking…" label and she holds for 900 ms before generating.

### Model choice

Set at the top of `chat.js`:

```javascript
const MODEL_ID = "Qwen3-0.6B-q4f16_1-MLC";
```

Sizes and tradeoffs:

| Model ID | Size | Best for |
|---|---|---|
| `Qwen3-0.6B-q4f16_1-MLC` | ~350 MB | Fast download, works on anything. Current default. |
| `Llama-3.2-1B-Instruct-q4f16_1-MLC` | ~700 MB | Better quality, no `<think>` blocks, still light |
| `Llama-3.2-3B-Instruct-q4f16_1-MLC` | ~1.8 GB | Noticeably better, needs a decent GPU |
| `Phi-3.5-mini-instruct-q4f16_1-MLC` | ~2.2 GB | Strong reasoning, slow first load |

Full list: https://github.com/mlc-ai/web-llm#supported-models

**Note on Qwen3:** it's a reasoning model, so it emits `<think>...</think>`
blocks before every reply. The playground strips these client-side,
but they still consume generation tokens and slow the response. If
that bothers you, swap to the 1B Llama — the quality is comparable and
there's no reasoning overhead.

### Styling

`public/style.css` uses CSS custom properties at the top:

```css
:root {
    --bg-0: #171426;
    --fg:   #f6f1ff;
    --teal: #7ee0d4;
    --mood-hue: 172;   /* live-mutated by emotion system */
    --mood-bpm: 16;    /* live-mutated by emotion system */
    /* ... */
}
```

Change these to re-theme the whole page in one place. The `--mood-hue`
and `--mood-bpm` variables are written by JavaScript at runtime — if
you hardcode them, the emotion system will overwrite your values.

### Background video

Replace `public/lumi_room_loop.mp4` with your own 5–10 second loop.
Keep it under 10 MB for quick loading. `public/Lumi-room.png` is the
poster frame shown before the video starts.

If your replacement has a visible loop seam, trim it with ffmpeg
before shipping:

```powershell
ffmpeg -i input.mp4 -ss 0.2 -t 5.1 -c copy lumi_room_loop.mp4
```

That crops from 0.2s to 5.3s with no re-encode.

---

## Local testing

Because there's no build step, you can serve the folder with any
static server:

```powershell
cd playground/public
npx serve
# or
python -m http.server 3000
```

Open `http://localhost:3000` in Chrome.

WebGPU requires a secure context. `localhost` counts as secure, so
this works. If you want to test from another device on your network,
you'll need an HTTPS tunnel (ngrok, Cloudflare Tunnel) — plain
`http://192.168.x.x` won't work.

---

## Troubleshooting

### "Error: Your browser does not support WebGPU"

The visitor's browser is too old, or WebGPU isn't functional. Ask
them to try:

- Chrome 113+ (any platform)
- Edge 113+
- Safari 18+ (macOS Sonoma or later)
- Firefox: not supported as of early 2026

Firefox specifically exposes `navigator.gpu` but often can't get an
adapter — the code checks for this and shows a clearer message.

### Bloom loader stuck at 0%

The model download isn't starting. Usually network. Check DevTools
Network tab for a stalled request to `huggingface.co` or
`cas-bridge.xethub.hf.co` (Hugging Face's CDN).

### "Failed to resolve module specifier"

`chat.js` is importing bare package names without a URL. Verify the
first two lines are:

```javascript
import * as webllm from "https://esm.run/@mlc-ai/web-llm";
```

Not `from "@mlc-ai/web-llm"`.

### "Cannot find module '@mlc-ai/web-llm'" or a 404 from esm.run

`esm.run` (jsDelivr) is blocked by an ad blocker, corporate network,
or regional restriction. Test in incognito with extensions disabled.
If it's a network policy, swap the import to a different CDN:

```javascript
import * as webllm from "https://esm.sh/@mlc-ai/web-llm";
```

### Chat works but responses are slow

Expected on older hardware. WebGPU inference speed scales with the
GPU. A GTX 1060 vs an RTX 4090 is a 50× difference. Consider
offering the 0.6B model as a fallback in the model picker.

### Lumi narrates her own thinking

Two possible causes:

1. **The `<think>` stripper isn't running.** Check `stripThink()` is
   being called in the streaming loop and the system prompt is being
   sent correctly.
2. **The model is over-explaining.** The current system prompt includes
   *"Never narrate your own thought process"*, which helps. If the
   model still does it, try the Llama swap — it's less prone to this.

### Emotion pill never appears

The model isn't emitting emotion tags. This is a small-model
unreliability issue — Qwen3-0.6B doesn't always follow the "prefix
with `[emotion]`" instruction. The 1B Llama is noticeably better at
it. Or add more examples to the system prompt.

### Deploy fails with "Found main.py but it does not export..."

You didn't set the **Root Directory** to `playground` in Vercel's
project settings. Without that, Vercel scans the entire repo, finds
Lumi's Python `main.py`, and tries to interpret it as a serverless
function.

Fix: Project Settings → General → Root Directory → set to
`playground`. Save. Redeploy (uncheck "Use existing build cache").

### Video background doesn't play

Two possible causes:

1. **The video is too big.** Vercel's edge may return a 413 or the
   browser may give up. Compress to under 10 MB.
2. **The poster path is wrong.** `Lumi-room.png` must exist in
   `public/`. If it doesn't, the browser shows a black rectangle for
   a split second before the video loads.

### Chat history doesn't persist

Check DevTools → Application → Local Storage → look for
`lumi-history`. If it's missing, the browser has localStorage
disabled (private browsing, cookie settings). This is graceful —
the chat still works, just doesn't remember between visits.

---

## Design notes

### Why no serverless function?

The original design used a Vercel Python function to proxy requests
to Groq or OpenAI. It worked, but it had three problems:

1. **Cost scales with usage.** Every visitor's chat hit your API key.
2. **Rate limits.** Both Groq and OpenAI throttle aggressively on
   free tiers. A popular link could exhaust your quota in hours.
3. **Privacy.** Conversations went through a third party.

Client-side inference solves all three. The cost is the download
delay and the WebGPU requirement, which is a good trade for a
public playground.

### Why no build step?

The playground imports `@mlc-ai/web-llm` from a CDN, so there's
nothing to bundle. This means:

- `git push` is the entire deploy pipeline
- No `package.json` to maintain
- No framework churn to keep up with
- Works offline once the model is cached

The tradeoff is a hard dependency on `esm.run` being up and
unblocked. That's a small risk for a demo site.

### Why not a smaller model?

The 0.6B model at ~350 MB is close to the floor for a model that can
hold a coherent conversation. Going smaller (e.g. TinyLlama at 128M)
gives visible degradation — the persona falls apart, responses become
incoherent. The download feels long, but it's a one-time cost.

### Why no cloud fallback?

Some playgrounds offer a "cloud fallback" for WebGPU-less visitors.
We don't, because:

- It reintroduces the API-key / cost problem for exactly the users
  least likely to be genuine (bots, old browsers, drive-by traffic).
- It complicates the codebase significantly.
- The honest message ("this needs a modern browser") is fine. The
  people who care will open Chrome.

### Why a bloom loader instead of a progress bar?

The download is a 30 s – 3 min wait on first visit. That's long
enough to lose people. The flower gives the wait a *purpose* —
something to watch, something that tracks progress visually, and a
satisfying payoff at the end. It also makes the first impression
feel intentional rather than empty.

---

## Updating

Because there's no build step, deployment is trivial:

1. Edit any file in `playground/public/`.
2. Commit and push.
3. Vercel redeploys automatically in ~30 seconds.

No cache invalidation needed. Existing visitors get the new version
on next page load.

**Two things to know about caching:**

- **Code and CSS** — Vercel busts the cache automatically on deploy.
- **The model** — cached by the browser keyed to the model ID.
  Changing `MODEL_ID` triggers a re-download for every visitor.
  Changing anything else does not.

If you need to force a hard refresh for a specific user, tell them to
press `Ctrl+Shift+R`.

---

## License

Same as the parent project. See `../LICENSE`.