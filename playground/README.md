# Lumi Playground

A public, feature-limited chat preview of Lumi that runs entirely in
the visitor's browser.

**No server, no API keys, no usage costs.** The AI model is downloaded
once by the visitor and then runs locally on their hardware using
WebGPU. You pay nothing to host it, and neither you nor the visitor
sends any conversation data to a server.

---

## What this is

A stripped-down version of [Lumi](../) — Lumi's persona, Lumi's voice,
Lumi's general vibe — packaged as a static site you can hand to anyone
with a URL.

| Feature | Playground | Full Lumi |
|---|---|---|
| Persona-accurate chat | ✅ | ✅ |
| Runs without install | ✅ | ❌ |
| Public / shareable URL | ✅ | ❌ |
| VTube Studio avatar | ❌ | ✅ |
| Microphone input (Whisper) | ❌ | ✅ |
| Fish Audio TTS | ❌ | ✅ (uses browser TTS here) |
| Long-term memory | ❌ | ✅ |
| Self-model, emotional state | ❌ | ✅ |
| Agent tools, web search, SDK | ❌ | ✅ |
| Discord integration | ❌ | ✅ |

The playground is meant to be a *taste* of Lumi — a way for people to
meet her before deciding to install the full thing.

---

## How it works

This is important to understand because it's not a normal web app.

### The AI runs on the visitor's computer

When someone opens the playground:

1. Their browser downloads a small language model (~350 MB by default)
   from a CDN, cached afterward.
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

The tradeoff:

- **First-load delay.** The visitor waits for the model to download
  once. After that it's instant.
- **Requires WebGPU.** Browsers without it (older Safari, Firefox
  pre-2025) can't run the model. Users see a clear error message.
- **Performance varies.** A gaming PC runs circles around a 2015
  laptop. Response speed depends entirely on the visitor's hardware.

---

## Project structure

```
playground/
├── public/
│   ├── index.html       # Chat UI
│   ├── style.css        # All styling
│   └── chat.js          # Client-side logic (AI SDK + WebLLM)
├── package.json         # Dependencies (ai + @built-in-ai/web-llm)
├── vercel.json          # Static-site config
└── README.md            # This file
```

**There is no `api/` folder and no `.env` file.** If you see either,
something has gone wrong — that would be the older serverless design,
which we deliberately abandoned.

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
2. Import the `lumi-ai` repository (same repo, not a fork)
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
You should see:

1. The model download progress bar (~30 s–3 min depending on
   connection).
2. "Model ready. You can start chatting!"
3. Type "hey, who are you" — Lumi responds in her own voice.

---

## Customizing Lumi

### Persona

The system prompt lives in `public/chat.js`:

```javascript
const SYSTEM_PROMPT = `You are Lumi, a hyper-expressive, casual AI
companion. Your responses should sound like a friend talking, not an
AI assistant. Use fragments, natural pauses, and avoid lists or formal
language. Keep your answers relatively short.`;
```

Edit this string to change how Lumi behaves. Whatever you put here is
what the model sees as its identity for every conversation.

To get the *real* Lumi persona, copy the `personality` block from
`config/lumi.yaml` and paste it here. Keep it under ~1,500 characters
— small models don't have huge context windows.

### Model choice

Set at the top of `public/chat.js`:

```javascript
const MODEL_ID = 'Qwen3-0.6B-q4f16_1-MLC';
```

Sizes and tradeoffs:

| Model ID | Size | Best for |
|---|---|---|
| `Qwen3-0.6B-q4f16_1-MLC` | ~350 MB | Fast download, works on anything |
| `Llama-3.2-1B-Instruct-q4f16_1-MLC` | ~700 MB | Better quality, still light |
| `Llama-3.2-3B-Instruct-q4f16_1-MLC` | ~1.8 GB | Noticeably better, needs a decent GPU |
| `Phi-3.5-mini-instruct-q4f16_1-MLC` | ~2.2 GB | Strong reasoning, slow first load |

Full list: https://github.com/mlc-ai/web-llm#supported-models

Start with the 0.6B model. If you find it too limited, bump up one
step. The download time doubles each tier.

### Styling

`public/style.css` uses CSS custom properties at the top:

```css
:root {
    --bg: #0e0d12;
    --fg: #eae6f0;
    --accent: #b39ddb;
    /* ... */
}
```

Change these to re-theme the whole page in one place.

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

Open `http://localhost:3000` in Chrome. Note that `npx serve` and
`python -m http.server` serve the `public/` folder directly — they
don't handle Vercel's routing, but for a static site there's nothing
to route.

WebGPU requires a secure context. `localhost` counts as secure, so
this works. If you want to test from another device on your network,
you'll need an HTTPS tunnel (ngrok, Cloudflare Tunnel) — plain
`http://192.168.x.x` won't work.

---

## What the visitor sees

A typical first-time flow:

1. **Load** — landing page is instant, static files are small.
2. **First message attempt** — the download progress bar appears.
3. **Download** — 30 s to 3 min depending on connection and model
   size. The progress bar shows percentage.
4. **Ready** — "Model ready. You can start chatting!"
5. **Chat** — first response takes 1–5 seconds (model warms up in
   RAM). Subsequent responses are 2–10× faster.
6. **Return visits** — model loads from browser cache. No download,
   instant readiness.

If a visitor is on an unsupported browser, they see:

> Error: Your browser does not support WebGPU, which is required for
> this AI model.

That message is honest and correct. There's no fallback — running the
model on a server would reintroduce the cost problem we're solving.

---

## Troubleshooting

### "Error: Your browser does not support WebGPU"

The visitor's browser is too old. Ask them to try:

- Chrome 113+ (any platform)
- Edge 113+
- Safari 18+ (macOS Sonoma or later)
- Firefox: not supported as of early 2026

### Model download stalls or fails

Usually a network issue on the visitor's end. The model comes from
Hugging Face's CDN, which is globally distributed but can be slow
on some ISPs. Nothing you can do server-side — the download is
peer-to-peer from HF.

### Chat works but responses are slow

Expected on older hardware. WebGPU inference speed scales with the
GPU. A GTX 1060 vs an RTX 4090 is a 50× difference. If the visitor
has integrated graphics only, consider offering a smaller model as
a fallback.

### "Cannot find module '@built-in-ai/web-llm'"

You're missing a dependency. Check that `package.json` in the
`playground/` folder contains both `ai` and `@built-in-ai/web-llm`,
then push again. Vercel will reinstall on the next deploy.

### Deploy fails with "Found main.py but it does not export..."

You didn't set the **Root Directory** to `playground` in Vercel's
project settings. Without that, Vercel scans the entire repo, finds
Lumi's Python `main.py`, and tries to interpret it as a serverless
function. Fix: Project Settings → General → Root Directory → set
to `playground`. Redeploy.

---

## Design notes

### Why not a serverless function?

The original design used a Vercel Python function to proxy requests
to Groq or OpenAI. It worked, but it had three problems:

1. **Cost scales with usage.** Every visitor's chat hit your API key.
2. **Rate limits.** Both Groq and OpenAI throttle aggressively on
   free tiers. A popular link could exhaust your quota in hours.
3. **Privacy.** Conversations went through a third party.

Client-side inference solves all three. The cost is the download
delay and the WebGPU requirement, which is a good trade for a
public playground.

### Why not a smaller download?

The 0.6B model at ~350 MB is close to the floor for a model that can
hold a coherent conversation. Going smaller (e.g. TinyLlama at 128M)
gives visible degradation — the persona falls apart, responses become
incoherent. The download feels long, but it's a one-time cost.

### Why no fallback?

Some playgrounds offer a "cloud fallback" for WebGPU-less visitors.
We don't, because:

- It reintroduces the API-key / cost problem for exactly the users
  least likely to be genuine (bots, old browsers, drive-by traffic).
- It complicates the codebase significantly.
- The honest message ("this needs a modern browser") is fine. The
  people who care will open Chrome.

---

## Updating

Because there's no build step, deployment is trivial:

1. Edit any file in `playground/public/`.
2. Commit and push.
3. Vercel redeploys automatically in ~30 seconds.

No cache invalidation needed — Vercel handles it. Existing visitors
get the new version on next page load. The AI model itself is
cached by the browser keyed to the model ID, so changing
`MODEL_ID` triggers a re-download for everyone; changing anything
else does not.

---

## License

Same as the parent project. See `../LICENSE`.