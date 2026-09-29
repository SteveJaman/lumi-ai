# Models

Lumi operates as a localized, multi-model agent. Rather than relying on a single monolithic cloud API, she orchestrates several specialized local models for text, voice, hearing, and memory.

## Large Language Model (LLM)

* **Model:** Qwen3
* **Configuration:** `config/Modelfile-qwen3`


Lumi's core reasoning and conversational intelligence run on Qwen3. The custom `Modelfile` defines her system prompt, parameter tuning (like temperature and context window), and stop tokens.

## Text-to-Speech (TTS)

* **Model:** XTTS v2.0.2
* **Path:** `models/v2.0.2/` (contains `model.pth`, `speakers_xtts.pth`, `vocab.json`)


* **Voice References:** `voices/` directory (e.g., `delta.wav`, `evilsama.wav`, `Soft_Onee_San.wav`)
Speech synthesis is handled locally. By providing a short reference audio file in the `voices` folder, XTTS clones the target voice characteristics and generates the output to `outputs/tts_output.wav`.



## Speech-to-Text (STT)

* **Model:** `faster-whisper`
* **Usage:** Local microphone (`modules/mic_input.py`) and Discord voice (`modules/discordClient.py`)
Handles real-time transcription. For Discord, the audio pipeline downmixes incoming Opus frames to mono at 48 kHz, runs VAD (Voice Activity Detection), and resamples to 16 kHz before passing the audio buffer to the transcription model.



## Embeddings & Memory (RAG)

* **Model:** `all-MiniLM-L6-v2` (SentenceTransformers)
* **Database:** ChromaDB (`data/memories/chroma.db`)
Lumi's long-term memory uses ChromaDB's default embedding model to vectorize conversation history. Retrieval relies on a custom scoring algorithm that balances semantic similarity, metadata tagging, and Ebbinghaus exponential time-decay.



## Vision & Ambient Perception

* **Components:** `modules/multimodal.py` and `modules/ambient_perception.py`

* **Storage:** `data/logs/images/`


Lumi continuously perceives the user's screen state. Screenshots are captured periodically (e.g., `screen_2026-09-27_16-09-48.jpg`), analyzed by a local vision model, and stored in the logs to provide ambient contextual awareness.