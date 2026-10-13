import { streamText } from 'https://esm.sh/ai@5';
import { webLLM } from 'https://esm.sh/@built-in-ai/web-llm@1';

// --- UI Elements ---
const chatForm = document.getElementById('chat-form');
const userInput = document.getElementById('user-input');
const messagesContainer = document.getElementById('messages');
const sendButton = document.getElementById('send-button');
const modelStatus = document.getElementById('model-status');
const progressContainer = document.getElementById('progress-container');
const progressBar = document.getElementById('progress-bar');
const progressText = document.getElementById('progress-text');

// --- Configuration ---
const MODEL_ID = 'Qwen3-0.6B-q4f16_1-MLC'; // A small, fast model. See notes below.
const SYSTEM_PROMPT = `You are Lumi, a hyper-expressive, casual AI companion. Your responses should sound like a friend talking, not an AI assistant. Use fragments, natural pauses, and avoid lists or formal language. Keep your answers relatively short.`;

// --- Chat Logic ---
const history = [];

async function initializeModel() {
    modelStatus.textContent = 'Checking model availability...';
    const model = webLLM(MODEL_ID);

    const availability = await model.availability();
    if (availability === 'unavailable') {
        modelStatus.textContent = 'Error: Your browser does not support WebGPU, which is required for this AI model.';
        userInput.disabled = true;
        sendButton.disabled = true;
        return;
    }

    if (availability === 'downloadable') {
        modelStatus.textContent = 'Preparing AI model for download...';
        progressContainer.classList.remove('hidden');
        
        try {
            await model.createSessionWithProgress((progress) => {
                const percentage = Math.round(progress * 100);
                progressBar.style.width = `${percentage}%`;
                progressText.textContent = `Downloading AI model: ${percentage}%`;
            });
            progressContainer.classList.add('hidden');
            modelStatus.textContent = 'Model ready. You can start chatting!';
        } catch (error) {
            console.error('Error during model download:', error);
            modelStatus.textContent = 'Error: Could not download the AI model.';
            userInput.disabled = true;
            sendButton.disabled = true;
            progressContainer.classList.add('hidden');
            return;
        }
    } else {
        modelStatus.textContent = 'Model ready (loaded from cache). You can start chatting!';
    }

    // Once ready, enable the input
    userInput.focus();
}

// --- Event Listeners ---
chatForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    const userMessage = userInput.value.trim();
    if (!userMessage) return;

    // 1. Display user message
    addMessage('user', userMessage);
    history.push({ role: 'user', content: userMessage });
    userInput.value = '';
    sendButton.disabled = true; // Disable while generating

    // 2. Add a placeholder for the assistant's response
    const assistantMessageElement = addMessage('assistant', '');

    // 3. Generate the response with streaming
    try {
        const result = streamText({
            model: webLLM(MODEL_ID), // Re-creates a lightweight wrapper, uses cached engine
            messages: [
                { role: 'system', content: SYSTEM_PROMPT },
                ...history
            ],
        });

        let fullResponse = '';
        for await (const chunk of result.textStream) {
            fullResponse += chunk;
            assistantMessageElement.textContent = fullResponse;
        }

        history.push({ role: 'assistant', content: fullResponse });
    } catch (error) {
        console.error('Error generating response:', error);
        assistantMessageElement.textContent = 'Sorry, something went wrong. Please try again.';
    } finally {
        sendButton.disabled = false;
        userInput.focus();
    }
});

function addMessage(role, text) {
    const messageElement = document.createElement('div');
    messageElement.classList.add('message', role);
    messageElement.textContent = text;
    messagesContainer.appendChild(messageElement);
    messagesContainer.scrollTop = messagesContainer.scrollHeight;
    return messageElement;
}

// --- Start the app ---
initializeModel();