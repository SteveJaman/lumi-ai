import os
import json
import queue
import requests
from constants import LLM_ENDPOINT, MODEL_NAME, DATA_ROOT
from modules.log_hub import emit as hub_emit


class Signals:
    def __init__(self):
        # Engine State Flags
        self.terminate = False
        self.new_message = False
        self.AI_thinking = False
        self.AI_speaking = False
        self.system_ready = False

        self.last_message_time = 0
        self.latest_image_base64 = None

        # Queues
        self.sio_queue = queue.Queue()
        self.vtube_queue = queue.Queue()
        self.tts_queue = queue.Queue()

        # Directory & File Setup — resolved under data/logs/
        self.log_dir = os.path.join(DATA_ROOT, "logs")
        self.log_file = os.path.join(self.log_dir, "chat_history.json")
        os.makedirs(self.log_dir, exist_ok=True)

        # Load history and force-initialize file on disk
        self.history = self.load_history(limit=30)
        self.save_history()

        # Phase 7.3-7.5 — voice-channel context.
        self.voice_context = None
        self.last_voice_speaker = None

    def load_history(self, limit=30):
        """Loads previous conversation turns from disk on startup."""
        if os.path.exists(self.log_file):
            try:
                with open(self.log_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    # Older builds stored full image base64 on each
                    # attachment; nothing reads it, so drop it on load.
                    for msg in data:
                        for att in msg.get("attachments") or []:
                            if isinstance(att, dict):
                                att.pop("image_b64", None)
                    hub_emit(self, "system",
                             f"history loaded ({len(data)} messages)")
                    return data[-limit:]
            except Exception as e:
                print(f"\033[90mHISTORY: Failed to load log file: {e}\033[0m")
        return []

    def quantize_history(self):
        """Compresses older messages into a summary block when history exceeds limits."""
        if len(self.history) <= 20:
            return

        older_messages = self.history[:-10]
        recent_messages = self.history[-10:]

        transcript_text = "\n".join(
            [f"{msg['role'].upper()}: {msg['content']}" for msg in older_messages]
        )

        prompt = (
            "Summarize the following conversation history concisely into a 2-3 sentence paragraph "
            "highlighting key topics discussed, inside jokes, or decisions made:\n\n"
            f"{transcript_text}"
        )

        try:
            payload = {
                "model": MODEL_NAME,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3
            }
            response = requests.post(f"{LLM_ENDPOINT}/v1/chat/completions",
                                     json=payload, timeout=20)
            if response.status_code == 200:
                summary_text = response.json()['choices'][0]['message']['content'].strip()
                quantized_entry = {
                    "role": "system",
                    "content": f"[Quantized History Summary]: {summary_text}"
                }
                self.history = [quantized_entry] + recent_messages
                print("\033[90mHISTORY: Quantized older chat logs into a compact summary block.\033[0m")
        except Exception as e:
            print(f"\033[90mHISTORY: Quantization skipped due to connection error: {e}\033[0m")

    def add_message(self, role, content, speaker=None, source=None):
        msg = {"role": role, "content": content}
        if speaker:
            msg["speaker"] = speaker
        if source:
            msg["source"] = source
        self.history.append(msg)

        if len(self.history) > 25:
            self.quantize_history()

        self.save_history()

    def save_history(self):
        """Saves current active history to JSON on disk."""
        try:
            os.makedirs(self.log_dir, exist_ok=True)
            with open(self.log_file, "w", encoding="utf-8") as f:
                json.dump(self.history, f, indent=2, ensure_ascii=False)
            hub_emit(self, "system",
                     f"history saved ({len(self.history)} messages)")
        except Exception as e:
            print(f"\033[90mHISTORY: Failed to save log file: {e}\033[0m")