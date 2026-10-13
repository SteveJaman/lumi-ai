/**
 * Lumi Playground — client-side inference + personality showcase.
 *
 * Runs entirely in the visitor's browser via WebGPU. Nothing is
 * sent to any server. Uses @mlc-ai/web-llm directly through esm.run.
 */

import * as webllm from "https://esm.run/@mlc-ai/web-llm";

// ================================================================
// UI elements
// ================================================================
const chatForm          = document.getElementById("chat-form");
const userInput         = document.getElementById("user-input");
const messagesContainer = document.getElementById("messages");
const sendButton        = document.getElementById("send-button");
const modelStatusEl     = document.getElementById("model-status");
const newChatBtn        = document.getElementById("new-chat");
const loaderEl          = document.getElementById("loader");
const loaderTextEl      = document.getElementById("loader-text");
const flowerEl          = document.querySelector(".flower");
const appEl             = document.getElementById("app");
const emotionDisplayEl  = document.getElementById("emotion-display");
const emotionEmojiEl    = document.getElementById("emotion-emoji");
const emotionNameEl     = document.getElementById("emotion-name");
const welcomeTextEl     = document.getElementById("welcome-text");

// ================================================================
// Config
// ================================================================
const MODEL_ID = "Llama-3.2-3B-Instruct-q4f16_1-MLC";

const SYSTEM_PROMPT = `You are Lumi. Short, casual, playful. You talk like a friend, not an assistant.

Rules:
- 1-2 sentences per reply. Never longer unless asked.
- Never say "as an AI", "I'd be happy to", "let me know", or "how can I help".
- No bullet points. No lists.
- Match the user's energy. Short message = short reply.
- Ask follow-up questions sometimes.
- Never use the word "Lumi" in third person.
- Never explain what you're about to do — just do it.

When your mood shifts, prefix your reply with one tag: [happy], [sad], [excited], [confused], [angry], [surprised], or [calm].`;

const MAX_INPUT_CHARS = 500;

// ================================================================
// Emotion definitions
// ================================================================
const EMOTIONS = {
    happy: {
        emoji: "🌸",
        color: "#ffd97a",
        glow:  "255, 217, 122",
        hue:   45,          // warm yellow
        bpm:   14,          // faster heartbeat
    },
    excited: {
        emoji: "⭐",
        color: "#ffb3d1",
        glow:  "255, 179, 209",
        hue:   330,         // hot pink
        bpm:   11,          // fast
    },
    sad: {
        emoji: "🌧️",
        color: "#8bb4e6",
        glow:  "139, 180, 230",
        hue:   215,         // cold blue
        bpm:   20,          // slow
    },
    angry: {
        emoji: "🔥",
        color: "#ff7a8a",
        glow:  "255, 122, 138",
        hue:   0,           // red
        bpm:   8,           // very fast
    },
    confused: {
        emoji: "💭",
        color: "#c4a5ff",
        glow:  "196, 165, 255",
        hue:   275,         // lavender
        bpm:   17,
    },
    surprised: {
        emoji: "✨",
        color: "#b8f0e6",
        glow:  "184, 240, 230",
        hue:   180,         // cyan
        bpm:   12,          // quick
    },
    calm: {
        emoji: "🌙",
        color: "#7ee0d4",
        glow:  "126, 224, 212",
        hue:   172,         // Lumi's default
        bpm:   22,          // very slow
    },
};

const DEFAULT_MOOD = {
    color: "#7ee0d4",
    glow:  "126, 224, 212",
    hue:   172,
    bpm:   16,
};

let emotionResetTimer = null;
let lastEmotionTag = null;

// ================================================================
// Emotion application — drives the whole personality showcase
// ================================================================
function applyEmotion(text) {
    const match = text.match(/\[(\w+)\]/);
    if (!match) return;

    const tag = match[1].toLowerCase();
    if (tag === lastEmotionTag) return;

    const mood = EMOTIONS[tag];
    if (!mood) return;

    lastEmotionTag = tag;

    // ---- Update accent color vars ----
    document.documentElement.style.setProperty("--teal", mood.color);
    document.documentElement.style.setProperty(
        "--border-glow", `rgba(${mood.glow}, 0.42)`
    );

    // ---- Update mood-reactive background ----
    document.documentElement.style.setProperty("--mood-hue", mood.hue);

    // ---- Update heartbeat speed ----
    document.documentElement.style.setProperty("--mood-bpm", mood.bpm);

    // ---- Show the emotion meter ----
    if (emotionEmojiEl) emotionEmojiEl.textContent = mood.emoji;
    if (emotionNameEl)  emotionNameEl.textContent  = tag;
    if (emotionDisplayEl) {
        emotionDisplayEl.style.borderColor = `rgba(${mood.glow}, 0.55)`;
        emotionDisplayEl.style.boxShadow =
            `0 4px 18px -8px rgba(${mood.glow}, 0.6), ` +
            `inset 0 0 12px -6px rgba(${mood.glow}, 0.5)`;
        emotionDisplayEl.classList.add("active");
    }

    // ---- Reset after 6s ----
    clearTimeout(emotionResetTimer);
    emotionResetTimer = setTimeout(() => {
        document.documentElement.style.setProperty("--teal", DEFAULT_MOOD.color);
        document.documentElement.style.setProperty(
            "--border-glow", `rgba(${DEFAULT_MOOD.glow}, 0.42)`
        );
        document.documentElement.style.setProperty("--mood-hue", DEFAULT_MOOD.hue);
        document.documentElement.style.setProperty("--mood-bpm", DEFAULT_MOOD.bpm);

        if (emotionDisplayEl) emotionDisplayEl.classList.remove("active");
        lastEmotionTag = null;
    }, 6000);
}

// ================================================================
// Context-aware greeting — changes by time of day
// ================================================================
function getTimeGreeting() {
    const h = new Date().getHours();
    if (h < 5)  return "you're up late too, huh. what's keeping you awake?";
    if (h < 12) return "morning. still half asleep but here — what's up?";
    if (h < 17) return "hey. what's on your mind?";
    if (h < 22) return "evening. how was your day?";
    return "getting late. can't sleep?";
}

function setTimeGreeting() {
    if (welcomeTextEl) {
        welcomeTextEl.textContent = getTimeGreeting();
    }
}

// ================================================================
// Thoughtful pause — extra beat before personal/philosophical replies
// ================================================================
const THOUGHTFUL_TRIGGERS = [
    "who are you", "what do you think", "do you feel", "do you dream",
    "are you alive", "are you real", "what do you want",
    "what makes you happy", "are you conscious", "do you remember",
    "what are you", "how do you feel",
];

function isThoughtfulPrompt(text) {
    const lower = text.toLowerCase();
    return THOUGHTFUL_TRIGGERS.some(t => lower.includes(t));
}

// ================================================================
// State
// ================================================================
let engine = null;
let history = [{ role: "system", content: SYSTEM_PROMPT }];
let generating = false;

// ---- Restore history from localStorage ----
try {
    const saved = localStorage.getItem("lumi-history");
    if (saved) {
        const parsed = JSON.parse(saved);
        if (Array.isArray(parsed) && parsed.length) {
            const restored = parsed.filter(m => m.role !== "system");
            history = [history[0], ...restored];
            for (const m of restored) {
                addMessage(m.role, m.content, { preReveal: true });
            }
            if (restored.length > 0) {
                const s = document.getElementById("suggestions");
                if (s) s.remove();
            }
        }
    }
} catch (e) {
    console.warn("Could not restore history:", e);
}

function persistHistory() {
    try {
        const trimmed = history.filter(m => m.role !== "system").slice(-40);
        localStorage.setItem("lumi-history", JSON.stringify(trimmed));
    } catch {}
}

// ================================================================
// Loader helpers
// ================================================================
function setLoaderText(text) {
    if (loaderTextEl) loaderTextEl.textContent = text;
}

function setBloom(p) {
    const clamped = Math.max(0, Math.min(1, p));
    document.documentElement.style.setProperty("--bloom", clamped.toFixed(3));
}

function animateBloomTo(target, durationMs) {
    const current = parseFloat(
        getComputedStyle(document.documentElement).getPropertyValue("--bloom")
    ) || 0;
    const start = performance.now();
    const delta = target - current;

    return new Promise(resolve => {
        function step(now) {
            const k = Math.min(1, (now - start) / durationMs);
            const eased = 1 - Math.pow(1 - k, 3);
            setBloom(current + delta * eased);
            if (k < 1) requestAnimationFrame(step);
            else resolve();
        }
        requestAnimationFrame(step);
    });
}

// ================================================================
// Status pill helper
// ================================================================
function setStatus(text, mode = "ready") {
    if (!modelStatusEl) return;
    modelStatusEl.classList.remove("loading", "ready", "error");
    modelStatusEl.classList.add(mode);
    const textEl = modelStatusEl.querySelector(".status-text");
    if (textEl) textEl.textContent = text;
    else modelStatusEl.textContent = text;
}

// ================================================================
// Strip Qwen3 <think> blocks and [emotion] tags
// ================================================================
function stripThink(text) {
    let out = text.replace(/<think\b[^>]*>[\s\S]*?<\/think>/gi, "");
    out = out.replace(/\[\w+\]/g, "");
    const openThink = out.search(/<think\b/i);
    if (openThink !== -1) out = out.slice(0, openThink);
    out = out.replace(/\[[\w]*$/, "");
    return out.replace(/^\s+|\s+$/g, "");
}

// ================================================================
// Model initialization + bloom reveal
// ================================================================
async function initializeModel() {
    if (!navigator.gpu) {
        setLoaderText("Needs Chrome or Edge (WebGPU required)");
        loaderTextEl.classList.add("error");
        return;
    }

    try {
        const adapter = await navigator.gpu.requestAdapter();
        if (!adapter) {
            setLoaderText("WebGPU unavailable — try Chrome or Edge");
            loaderTextEl.classList.add("error");
            return;
        }
    } catch {
        setLoaderText("WebGPU initialization failed");
        loaderTextEl.classList.add("error");
        return;
    }

    try {
        const cached = await webllm.hasModelInCache(MODEL_ID);
        setLoaderText(cached ? "waking up…" : "planting seeds…");

        engine = await webllm.CreateMLCEngine(MODEL_ID, {
            initProgressCallback: (progress) => {
                const p = progress.progress || 0;
                setBloom(p);

                if (progress.text) {
                    const t = progress.text.toLowerCase();
                    if (t.includes("fetch") || t.includes("download")) {
                        const pct = Math.round(p * 100);
                        setLoaderText(`downloading my brain… ${pct}%`);
                    } else if (t.includes("load") || t.includes("shader")) {
                        setLoaderText("waking up…");
                    } else {
                        setLoaderText(progress.text);
                    }
                }
            },
        });

        await animateBloomTo(1.0, 400);
        setLoaderText("hello!");
        await new Promise(r => setTimeout(r, 300));

        flowerEl.classList.add("popped");
        await new Promise(r => setTimeout(r, 450));

        loaderEl.classList.add("fade-out");

        setTimeout(() => {
            appEl.classList.add("visible");
            setTimeGreeting();
        }, 150);

        setTimeout(() => {
            loaderEl.style.display = "none";
        }, 1200);

        setStatus("Model ready. Start chatting!", "ready");
        userInput.disabled = false;
        sendButton.disabled = false;
        setTimeout(() => userInput.focus(), 900);

    } catch (error) {
        console.error("Model initialization failed:", error);
        setLoaderText(`Error: ${error.message || "unknown"}`);
        loaderTextEl.classList.add("error");

        const retry = document.createElement("button");
        retry.textContent = "Retry";
        retry.className = "icon-btn";
        retry.style.marginTop = "12px";
        retry.onclick = () => location.reload();
        loaderEl.appendChild(retry);
    }
}

// ================================================================
// Chat submit
// ================================================================
chatForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (generating || !engine) return;

    const userMessage = userInput.value.trim().slice(0, MAX_INPUT_CHARS);
    if (!userMessage) return;

    hideSuggestions();

    addMessage("user", userMessage);
    history.push({ role: "user", content: userMessage });
    persistHistory();

    userInput.value = "";
    userInput.style.height = "auto";
    generating = true;
    sendButton.disabled = true;

    const { bubble: assistantBubble, textSpan: assistantText } =
        addMessage("assistant", "");

    // ---- Show "thinking" or plain "typing" ----
    const thoughtful = isThoughtfulPrompt(userMessage);
    if (thoughtful) {
        assistantBubble.classList.add("thinking");
        assistantText.innerHTML =
            '<span class="thinking-dots">' +
            '<span></span><span></span><span></span>' +
            '</span><span class="thinking-label">thinking…</span>';
    } else {
        assistantBubble.classList.add("typing");
        assistantText.innerHTML = '<span class="dot-mid"></span>';
    }

    // ---- Artificial hesitation for personal questions ----
    if (thoughtful) {
        await new Promise(r => setTimeout(r, 900));
    }

    let accumulated = "";
    let firstTokenReceived = false;

    try {
        const chunks = await engine.chat.completions.create({
            messages: history,
            temperature: 0.85,
            max_tokens: 400,
            stream: true,
            extra_body: { enable_thinking: false },
        });

        for await (const chunk of chunks) {
            const delta = chunk.choices?.[0]?.delta?.content || "";
            if (!delta) continue;

            accumulated += delta;
            applyEmotion(accumulated);

            const visible = stripThink(accumulated);

            if (visible) {
                if (!firstTokenReceived) {
                    firstTokenReceived = true;
                    assistantBubble.classList.remove("typing", "thinking");
                    assistantBubble.classList.add("streaming");
                }
                assistantText.textContent = visible;
                scrollToBottom();
            }
        }

        assistantBubble.classList.remove("streaming", "thinking");
        const finalVisible = stripThink(accumulated);

        if (finalVisible) {
            assistantText.textContent = finalVisible;
            history.push({ role: "assistant", content: finalVisible });
        } else {
            assistantText.textContent =
                "(blank stare) ...sorry, my brain glitched. say that again?";
            history.push({
                role: "assistant",
                content: "sorry, my brain glitched. say that again?",
            });
        }
        persistHistory();
    } catch (error) {
        console.error("Generation error:", error);
        assistantBubble.classList.remove("typing", "thinking", "streaming");
        assistantText.textContent =
            `⚠ ${error.message || "generation failed"}`;
    } finally {
        assistantBubble.classList.remove("streaming", "thinking");
        generating = false;
        sendButton.disabled = false;
        userInput.focus();
    }
});

// ================================================================
// Suggestions
// ================================================================
function hideSuggestions() {
    const el = document.getElementById("suggestions");
    if (el) el.remove();
}

document.querySelectorAll(".suggestions button").forEach((btn) => {
    btn.addEventListener("click", () => {
        userInput.value = btn.dataset.prompt || btn.textContent;
        chatForm.requestSubmit();
    });
});

// ================================================================
// New chat
// ================================================================
if (newChatBtn) {
    newChatBtn.addEventListener("click", () => {
        if (history.length > 1) {
            const ok = confirm("Start a new chat? Current history will be cleared.");
            if (!ok) return;
        }
        history = [{ role: "system", content: SYSTEM_PROMPT }];
        localStorage.removeItem("lumi-history");
        messagesContainer.innerHTML = "";
        addMessage("assistant", getTimeGreeting());
        userInput.focus();
    });
}

// ================================================================
// Input ergonomics
// ================================================================
userInput.addEventListener("input", () => {
    userInput.style.height = "auto";
    userInput.style.height = Math.min(userInput.scrollHeight, 140) + "px";
});

userInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        chatForm.requestSubmit();
    }
});

// ================================================================
// Message rendering
// ================================================================
function addMessage(role, text, opts = {}) {
    const wrap = document.createElement("div");
    wrap.classList.add("message", role);
    if (opts.preReveal) wrap.classList.add("pre-reveal");

    const bubble = document.createElement("div");
    bubble.classList.add("bubble");

    const textSpan = document.createElement("span");
    textSpan.classList.add("text");
    textSpan.textContent = text || "";
    bubble.appendChild(textSpan);

    if (role === "assistant") {
        const copyBtn = document.createElement("button");
        copyBtn.type = "button";
        copyBtn.className = "copy-btn";
        copyBtn.title = "Copy";
        copyBtn.setAttribute("aria-label", "Copy message");
        copyBtn.innerHTML = `
            <svg viewBox="0 0 24 24" width="12" height="12"
                 fill="none" stroke="currentColor" stroke-width="2"
                 stroke-linecap="round" stroke-linejoin="round">
                <rect x="9" y="9" width="13" height="13" rx="2"/>
                <path d="M5 15V5a2 2 0 0 1 2-2h10"/>
            </svg>`;
        copyBtn.addEventListener("click", (e) => {
            e.stopPropagation();
            navigator.clipboard.writeText(textSpan.textContent).catch(() => {});
            copyBtn.classList.add("copied");
            setTimeout(() => copyBtn.classList.remove("copied"), 1200);
        });
        bubble.appendChild(copyBtn);
    }

    wrap.appendChild(bubble);
    messagesContainer.appendChild(wrap);
    scrollToBottom();
    return { bubble, textSpan };
}

function scrollToBottom() {
    messagesContainer.scrollTop = messagesContainer.scrollHeight;
}

// ================================================================
// Pause background video when tab is hidden
// ================================================================
document.addEventListener("visibilitychange", () => {
    const vid = document.querySelector(".bg-video");
    if (!vid) return;
    if (document.hidden) vid.pause();
    else vid.play().catch(() => {});
});

// ================================================================
// Boot
// ================================================================
initializeModel();