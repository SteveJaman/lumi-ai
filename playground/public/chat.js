/**
 * Lumi Playground — client-side inference.
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

// ================================================================
// Config
// ================================================================
const MODEL_ID = "Qwen3-0.6B-q4f16_1-MLC";

const SYSTEM_PROMPT = `You are Lumi. Not a model pretending to be Lumi — you are her. Speak in short, casual fragments. No bullet points, no "as an AI", no explaining what you're about to do — just say it. Match the user's energy. If they say "hi hi", you say "hi hi" back and ask something real. Never narrate your own thought process. Never use the word "Lumi" in the third person.

When your mood shifts meaningfully, prefix your reply with a single tag like [happy], [sad], [excited], [confused], [angry], [surprised], or [calm]. One tag max per reply, only when the feeling is real.`;

const MAX_INPUT_CHARS = 500;

// ================================================================
// Emotion → UI accent color
// ================================================================
const EMOTION_STYLES = {
    happy:     { color: "#ffd97a", glow: "255, 217, 122" },
    excited:   { color: "#ffb3d1", glow: "255, 179, 209" },
    sad:       { color: "#8bb4e6", glow: "139, 180, 230" },
    angry:     { color: "#ff7a8a", glow: "255, 122, 138" },
    confused:  { color: "#c4a5ff", glow: "196, 165, 255" },
    surprised: { color: "#b8f0e6", glow: "184, 240, 230" },
    calm:      { color: "#7ee0d4", glow: "126, 224, 212" },
};

const DEFAULT_TEAL = "#7ee0d4";
const DEFAULT_GLOW = "126, 224, 212";

let emotionResetTimer = null;
let lastEmotionTag = null;

function applyEmotion(text) {
    const match = text.match(/\[(\w+)\]/);
    if (!match) return;
    const tag = match[1].toLowerCase();
    if (tag === lastEmotionTag) return;
    const style = EMOTION_STYLES[tag];
    if (!style) return;

    lastEmotionTag = tag;
    document.documentElement.style.setProperty("--teal", style.color);
    document.documentElement.style.setProperty(
        "--border-glow", `rgba(${style.glow}, 0.42)`
    );

    clearTimeout(emotionResetTimer);
    emotionResetTimer = setTimeout(() => {
        document.documentElement.style.setProperty("--teal", DEFAULT_TEAL);
        document.documentElement.style.setProperty(
            "--border-glow", `rgba(${DEFAULT_GLOW}, 0.42)`
        );
        lastEmotionTag = null;
    }, 6000);
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
                const { bubble } = addMessage(m.role, m.content, { preReveal: true });
            }
            // Hide suggestions if there's any restored history
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

// Animate --bloom from current to target over `durationMs` (for cached case)
function animateBloomTo(target, durationMs) {
    const current = parseFloat(
        getComputedStyle(document.documentElement).getPropertyValue("--bloom")
    ) || 0;
    const start = performance.now();
    const delta = target - current;

    return new Promise(resolve => {
        function step(now) {
            const k = Math.min(1, (now - start) / durationMs);
            const eased = 1 - Math.pow(1 - k, 3);   // ease-out cubic
            setBloom(current + delta * eased);
            if (k < 1) requestAnimationFrame(step);
            else resolve();
        }
        requestAnimationFrame(step);
    });
}

// ================================================================
// Status pill helper (header)
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
    // ---- WebGPU check ----
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

        if (!cached) {
            setLoaderText("planting seeds…");
        } else {
            setLoaderText("waking up…");
        }

        // ---- Load the engine ----
        engine = await webllm.CreateMLCEngine(MODEL_ID, {
            initProgressCallback: (progress) => {
                const p = progress.progress || 0;
                setBloom(p);

                if (progress.text) {
                    // MLC's own text is a bit noisy — trim to something friendly
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

        // ---- Ensure the flower is fully bloomed before pop ----
        await animateBloomTo(1.0, 400);
        setLoaderText("hello!");

        // Small beat so the completed bloom is visible
        await new Promise(r => setTimeout(r, 300));

        // ---- Pop the flower ----
        flowerEl.classList.add("popped");

        // Wait for pop animation to mostly finish
        await new Promise(r => setTimeout(r, 450));

        // ---- Fade loader out ----
        loaderEl.classList.add("fade-out");

        // ---- Reveal the app (with slight overlap so it feels continuous) ----
        setTimeout(() => {
            appEl.classList.add("visible");
        }, 150);

        // Cleanup: remove loader from the DOM after the fade completes
        setTimeout(() => {
            loaderEl.style.display = "none";
        }, 1200);

        // ---- Enable input ----
        setStatus("Model ready. Start chatting!", "ready");
        userInput.disabled = false;
        sendButton.disabled = false;
        setTimeout(() => userInput.focus(), 900);

    } catch (error) {
        console.error("Model initialization failed:", error);
        setLoaderText(`Error: ${error.message || "unknown"}`);
        loaderTextEl.classList.add("error");

        // Offer a retry button
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
    assistantBubble.classList.add("typing");
    assistantText.innerHTML = '<span class="dot-mid"></span>';

    let accumulated = "";

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
                assistantBubble.classList.remove("typing");
                assistantBubble.classList.add("streaming");
                assistantText.textContent = visible;
                scrollToBottom();
            }
        }

        assistantBubble.classList.remove("streaming");
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
        assistantBubble.classList.remove("typing", "streaming");
        assistantText.textContent =
            `⚠ ${error.message || "generation failed"}`;
    } finally {
        assistantBubble.classList.remove("streaming");
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
        addMessage("assistant", "fresh start. what's on your mind?");
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