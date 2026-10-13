/**
 * Lumi Playground — client-side inference.
 *
 * Runs entirely in the visitor's browser via WebGPU. Nothing is sent
 * to any server. Uses @mlc-ai/web-llm directly through esm.run.
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
const progressContainer = document.getElementById("progress-container");
const progressBar       = document.getElementById("progress-bar");
const progressText      = document.getElementById("progress-text");
const newChatBtn        = document.getElementById("new-chat");

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
    if (tag === lastEmotionTag) return;       // no-op if unchanged
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
                addMessage(m.role, m.content);
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
// Status helper
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
// Strip Qwen3 <think> blocks and [emotion] tags from visible text
// ================================================================
function stripThink(text) {
    // Complete <think>...</think> blocks
    let out = text.replace(/<think\b[^>]*>[\s\S]*?<\/think>/gi, "");
    // Complete [emotion] tags
    out = out.replace(/\[\w+\]/g, "");
    // Unclosed <think> still streaming
    const openThink = out.search(/<think\b/i);
    if (openThink !== -1) out = out.slice(0, openThink);
    // Unclosed [emotion tag being typed
    out = out.replace(/\[[\w]*$/, "");
    return out.replace(/^\s+|\s+$/g, "");
}

// ================================================================
// Model initialization
// ================================================================
async function initializeModel() {
    if (!navigator.gpu) {
        setStatus("Needs Chrome or Edge (WebGPU required)", "error");
        userInput.disabled = true;
        sendButton.disabled = true;
        return;
    }

    try {
        const adapter = await navigator.gpu.requestAdapter();
        if (!adapter) {
            setStatus("WebGPU unavailable — try Chrome or Edge", "error");
            userInput.disabled = true;
            sendButton.disabled = true;
            return;
        }
    } catch {
        setStatus("WebGPU initialization failed", "error");
        userInput.disabled = true;
        sendButton.disabled = true;
        return;
    }

    setStatus("Preparing AI model…", "loading");

    try {
        let cached = false;
        try {
            cached = await webllm.hasModelInCache(MODEL_ID);
        } catch {
            cached = false;
        }

        if (!cached) {
            progressContainer.classList.remove("hidden");
            progressText.textContent = "downloading model…";
            progressBar.style.width = "0%";
        }

        engine = await webllm.CreateMLCEngine(MODEL_ID, {
            initProgressCallback: (progress) => {
                progressContainer.classList.remove("hidden");
                const pct = Math.round((progress.progress || 0) * 100);
                progressBar.style.width = `${pct}%`;
                progressText.textContent = progress.text || `loading: ${pct}%`;
            },
        });

        progressContainer.classList.add("hidden");
        setStatus("Model ready. Start chatting!", "ready");
        userInput.disabled = false;
        sendButton.disabled = false;
        userInput.focus();
    } catch (error) {
        console.error("Model initialization failed:", error);
        setStatus(`Error: ${error.message || "unknown"}`, "error");
        progressContainer.classList.add("hidden");

        const retry = document.createElement("button");
        retry.textContent = "Retry";
        retry.className = "icon-btn";
        retry.style.marginTop = "8px";
        retry.onclick = () => location.reload();
        modelStatusEl.appendChild(retry);
    }
}

// ================================================================
// Chat submit handler
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

    // Empty assistant bubble with typing dots
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

            // Detect emotion in the raw text before stripping
            applyEmotion(accumulated);

            const visible = stripThink(accumulated);

            if (visible) {
                assistantBubble.classList.remove("typing");
                assistantBubble.classList.add("streaming");
                assistantText.textContent = visible;
                scrollToBottom();
            }
        }

        // Final pass
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
// Suggestion chips
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
/**
 * Append a message to the container.
 * Returns { bubble, textSpan } — bubble is the styled box,
 * textSpan is the streaming target so we don't wipe the copy button.
 */
function addMessage(role, text) {
    const wrap = document.createElement("div");
    wrap.classList.add("message", role);

    const bubble = document.createElement("div");
    bubble.classList.add("bubble");

    const textSpan = document.createElement("span");
    textSpan.classList.add("text");
    textSpan.textContent = text || "";
    bubble.appendChild(textSpan);

    // Copy button (assistant only)
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