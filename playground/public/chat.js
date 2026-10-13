/**
 * Lumi Playground — client-side inference.
 *
 * Uses @mlc-ai/web-llm directly (no AI SDK wrapper) because the
 * provider packages have bundling issues when loaded via CDN.
 */

import * as webllm from "https://esm.run/@mlc-ai/web-llm";

// --------------------------------------------------------------------
// UI elements
// --------------------------------------------------------------------
const chatForm = document.getElementById("chat-form");
const userInput = document.getElementById("user-input");
const messagesContainer = document.getElementById("messages");
const sendButton = document.getElementById("send-button");
const modelStatus = document.getElementById("model-status");
const progressContainer = document.getElementById("progress-container");
const progressBar = document.getElementById("progress-bar");
const progressText = document.getElementById("progress-text");

// --------------------------------------------------------------------
// Config
// --------------------------------------------------------------------
const MODEL_ID = "Qwen3-0.6B-q4f16_1-MLC";

const SYSTEM_PROMPT = `You are Lumi, a hyper-expressive, casual AI companion. Your responses should sound like a friend talking, not an AI assistant. Use fragments, natural pauses, and avoid lists or formal language. Keep your answers relatively short.`;

const MAX_INPUT_CHARS = 500;

// --------------------------------------------------------------------
// State
// --------------------------------------------------------------------
let engine = null;
let history = [{ role: "system", content: SYSTEM_PROMPT }];
let generating = false;

// Restore history from localStorage
try {
    const saved = localStorage.getItem("lumi-history");
    if (saved) {
        const parsed = JSON.parse(saved);
        if (Array.isArray(parsed) && parsed.length) {
            // Drop the system message if it was saved (we add it fresh)
            history = [history[0], ...parsed.filter(m => m.role !== "system")];
            for (const m of parsed.filter(m => m.role !== "system")) {
                addMessage(m.role, m.content);
            }
        }
    }
} catch {}

function persistHistory() {
    try {
        const trimmed = history.filter(m => m.role !== "system").slice(-40);
        localStorage.setItem("lumi-history", JSON.stringify(trimmed));
    } catch {}
}

// --------------------------------------------------------------------
// Model initialization
// --------------------------------------------------------------------
async function initializeModel() {
    // WebGPU check
    if (!navigator.gpu) {
        modelStatus.textContent =
            "Your browser doesn't support WebGPU. Try Chrome or Edge.";
        userInput.disabled = true;
        sendButton.disabled = true;
        return;
    }

    modelStatus.textContent = "Preparing AI model…";

    try {
        // Detect if the model is already cached
        const cached = await webllm.hasModelInCache(MODEL_ID);
        if (!cached) {
            progressContainer.classList.remove("hidden");
            progressText.textContent = "Downloading model…";
        }

        engine = await webllm.CreateMLCEngine(MODEL_ID, {
            initProgressCallback: (progress) => {
                progressContainer.classList.remove("hidden");
                const pct = Math.round((progress.progress || 0) * 100);
                progressBar.style.width = `${pct}%`;
                progressText.textContent =
                    progress.text || `Loading: ${pct}%`;
            },
        });

        progressContainer.classList.add("hidden");
        modelStatus.textContent = "Model ready. Start chatting!";
        userInput.disabled = false;
        sendButton.disabled = false;
        userInput.focus();
    } catch (error) {
        console.error("Model initialization failed:", error);
        modelStatus.textContent = `Error: ${error.message}. `;
        const retry = document.createElement("button");
        retry.textContent = "Retry";
        retry.onclick = () => location.reload();
        modelStatus.appendChild(retry);
    }
}

// --------------------------------------------------------------------
// Chat
// --------------------------------------------------------------------
chatForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (generating || !engine) return;

    const userMessage = userInput.value.trim().slice(0, MAX_INPUT_CHARS);
    if (!userMessage) return;

    addMessage("user", userMessage);
    history.push({ role: "user", content: userMessage });
    persistHistory();
    userInput.value = "";
    generating = true;
    sendButton.disabled = true;
    sendButton.textContent = "…";

    const assistantEl = addMessage("assistant", "");
    assistantEl.classList.add("typing");

    let accumulated = "";

    try {
        const chunks = await engine.chat.completions.create({
            messages: history,
            temperature: 0.85,
            max_tokens: 400,
            stream: true,
        });

        for await (const chunk of chunks) {
            const delta = chunk.choices?.[0]?.delta?.content || "";
            if (delta) {
                accumulated += delta;
                assistantEl.textContent = accumulated;
                assistantEl.classList.remove("typing");
                scrollToBottom();
            }
        }

        history.push({ role: "assistant", content: accumulated });
        persistHistory();
    } catch (error) {
        console.error("Generation error:", error);
        assistantEl.textContent = `⚠ ${error.message}`;
        assistantEl.classList.remove("typing");
    } finally {
        generating = false;
        sendButton.disabled = false;
        sendButton.textContent = "Send";
        userInput.focus();
    }
});

// --------------------------------------------------------------------
// UI helpers
// --------------------------------------------------------------------
function addMessage(role, text) {
    const div = document.createElement("div");
    div.classList.add("message", role);
    div.textContent = text;
    messagesContainer.appendChild(div);
    scrollToBottom();
    return div;
}

function scrollToBottom() {
    messagesContainer.scrollTop = messagesContainer.scrollHeight;
}

// --------------------------------------------------------------------
// Boot
// --------------------------------------------------------------------
initializeModel();