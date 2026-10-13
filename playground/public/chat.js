/**
 * Lumi Playground — client-side inference.
 *
 * Uses @mlc-ai/web-llm directly (no AI SDK wrapper) because the
 * provider packages have bundling issues when loaded via CDN.
 *
 * Runs entirely in the visitor's browser via WebGPU. Nothing is
 * sent to any server.
 */

import * as webllm from "https://esm.run/@mlc-ai/web-llm";

// ================================================================
// UI elements
// ================================================================
const chatForm         = document.getElementById("chat-form");
const userInput        = document.getElementById("user-input");
const messagesContainer = document.getElementById("messages");
const sendButton       = document.getElementById("send-button");
const modelStatusEl    = document.getElementById("model-status");
const progressContainer = document.getElementById("progress-container");
const progressBar      = document.getElementById("progress-bar");
const progressText     = document.getElementById("progress-text");
const newChatBtn       = document.getElementById("new-chat");

// ================================================================
// Config
// ================================================================
const MODEL_ID = "Qwen3-0.6B-q4f16_1-MLC";

const SYSTEM_PROMPT = `You are Lumi. Not a model pretending to be Lumi — you are her. Speak in short, casual fragments. No bullet points, no "as an AI", no explaining what you're about to do — just say it. Match the user's energy. If they say "hi hi", you say "hi hi" back and ask something real. Never narrate your own thought process. Never use the word "Lumi" in the third person.`;

const MAX_INPUT_CHARS = 500;

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
    } catch (e) {
        // localStorage might be full or disabled — non-fatal
    }
}

// ================================================================
// Status helper — updates the header status pill
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
// Strip Qwen3 <think>...</think> reasoning blocks
// ================================================================
/**
 * Handles three cases:
 *   - Complete block:  "hi <think>...</think> there" → "hi  there"
 *   - Open block mid-stream: "hi <think>still thinking" → "hi "
 *   - No block: passthrough
 */
function stripThink(text) {
    let out = text.replace(/<think\b[^>]*>[\s\S]*?<\/think>/gi, "");
    const openIdx = out.search(/<think\b/i);
    if (openIdx !== -1) {
        out = out.slice(0, openIdx);
    }
    return out.replace(/^\s+|\s+$/g, "");
}

// ================================================================
// Model initialization
// ================================================================
async function initializeModel() {
    // ---- WebGPU availability check ----
    if (!navigator.gpu) {
        setStatus("Needs Chrome or Edge (WebGPU required)", "error");
        userInput.disabled = true;
        sendButton.disabled = true;
        return;
    }

    // Firefox exposes navigator.gpu but often can't get an adapter.
    // Verify before we try to load the model, otherwise the engine
    // hangs silently on "Preparing…".
    try {
        const adapter = await navigator.gpu.requestAdapter();
        if (!adapter) {
            setStatus("WebGPU unavailable — try Chrome or Edge", "error");
            userInput.disabled = true;
            sendButton.disabled = true;
            return;
        }
    } catch (e) {
        setStatus("WebGPU initialization failed", "error");
        userInput.disabled = true;
        sendButton.disabled = true;
        return;
    }

    setStatus("Preparing AI model…", "loading");

    try {
        // ---- Detect cache to decide whether to show the progress bar ----
        let cached = false;
        try {
            cached = await webllm.hasModelInCache(MODEL_ID);
        } catch {
            // hasModelInCache not available in older builds; assume uncached
            cached = false;
        }

        if (!cached) {
            progressContainer.classList.remove("hidden");
            progressText.textContent = "downloading model…";
            progressBar.style.width = "0%";
        }

        // ---- Create the engine ----
        engine = await webllm.CreateMLCEngine(MODEL_ID, {
            initProgressCallback: (progress) => {
                progressContainer.classList.remove("hidden");
                const pct = Math.round((progress.progress || 0) * 100);
                progressBar.style.width = `${pct}%`;
                progressText.textContent = progress.text || `loading: ${pct}%`;
            },
        });

        // ---- Ready ----
        progressContainer.classList.add("hidden");
        setStatus("Model ready. Start chatting!", "ready");
        userInput.disabled = false;
        sendButton.disabled = false;
        userInput.focus();
    } catch (error) {
        console.error("Model initialization failed:", error);
        setStatus(`Error: ${error.message || "unknown"}`, "error");
        progressContainer.classList.add("hidden");

        // Retry button
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

    // Add user message
    addMessage("user", userMessage);
    history.push({ role: "user", content: userMessage });
    persistHistory();

    // Reset input
    userInput.value = "";
    userInput.style.height = "auto";
    generating = true;
    sendButton.disabled = true;
    sendButton.classList.add("loading");

    // Add empty assistant bubble with typing indicator
    const assistantBubble = addMessage("assistant", "");
    assistantBubble.classList.add("typing");
    assistantBubble.innerHTML = '<span class="dot-mid"></span>';

    let accumulated = "";

    try {
        const chunks = await engine.chat.completions.create({
            messages: history,
            temperature: 0.85,
            max_tokens: 400,
            stream: true,
            // Qwen3-specific: skip the chain-of-thought block. Silently
            // ignored on builds that don't support it — stripThink
            // handles those.
            extra_body: {
                enable_thinking: false,
            },
        });

        for await (const chunk of chunks) {
            const delta = chunk.choices?.[0]?.delta?.content || "";
            if (!delta) continue;

            accumulated += delta;

            // Strip <think>...</think> from the visible text
            const visible = stripThink(accumulated);

            if (visible) {
                assistantBubble.classList.remove("typing");
                assistantBubble.textContent = visible;
                scrollToBottom();
            }
        }

        // Final pass — if the model ended with a thinking block, we
        // may have an empty visible message. Fall back to a short nudge.
        const finalVisible = stripThink(accumulated);
        if (finalVisible) {
            assistantBubble.textContent = finalVisible;
            history.push({ role: "assistant", content: finalVisible });
        } else {
            assistantBubble.textContent = "…";
            history.push({ role: "assistant", content: "…" });
        }
        persistHistory();

    } catch (error) {
        console.error("Generation error:", error);
        assistantBubble.classList.remove("typing");
        assistantBubble.textContent = `⚠ ${error.message || "generation failed"}`;
    } finally {
        generating = false;
        sendButton.disabled = false;
        sendButton.classList.remove("loading");
        userInput.focus();
    }
});

// ================================================================
// New chat handler
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
        addMessage(
            "assistant",
            "fresh start. what's on your mind?"
        );
        userInput.focus();
    });
}

// ================================================================
// Auto-resize textarea as user types
// ================================================================
userInput.addEventListener("input", () => {
    userInput.style.height = "auto";
    userInput.style.height = Math.min(userInput.scrollHeight, 140) + "px";
});

// Enter to send, Shift+Enter for newline
userInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        chatForm.requestSubmit();
    }
});

// ================================================================
// UI helpers
// ================================================================
/**
 * Append a message to the container.
 * Returns the inner .bubble element (so streaming can mutate text).
 */
function addMessage(role, text) {
    const wrap = document.createElement("div");
    wrap.classList.add("message", role);

    const bubble = document.createElement("div");
    bubble.classList.add("bubble");

    if (text) {
        bubble.textContent = text;
    }

    wrap.appendChild(bubble);
    messagesContainer.appendChild(wrap);
    scrollToBottom();
    return bubble;
}

function scrollToBottom() {
    messagesContainer.scrollTop = messagesContainer.scrollHeight;
}

// ================================================================
// Boot
// ================================================================
initializeModel();