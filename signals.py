import os
import json
import queue
import requests
from constants import LLM_ENDPOINT, MODEL_NAME, DATA_ROOT
from modules.log_hub import emit as hub_emit


# Flags whose changes are pushed to listeners (the HITL server forwards
# them to the Island as lumi_status, so it never has to poll).
_WATCHED_FLAGS = ("AI_thinking", "AI_speaking")


class _History(list):
    """The chat history. A list, but it tells `signals.on_history_change`
    when messages are added, so the Island can show a reply the moment it
    lands instead of polling. Messages are appended from many modules and
    threads; this is the one place that sees all of them.

    Listener: fn("append", index_of_first_new, [new messages]) or fn("reset", 0, [])."""

    def __init__(self, items=(), owner=None):
        super().__init__(items)
        self._owner = owner

    def _tell(self, kind, start, msgs):
        owner = self._owner
        listener = owner.__dict__.get("on_history_change") if owner is not None else None
        if listener is not None:
            try:
                listener(kind, start, msgs)
            except Exception:
                pass

    def append(self, item):
        super().append(item)
        self._tell("append", len(self) - 1, [item])

    def extend(self, items):
        items = list(items)
        start = len(self)
        super().extend(items)
        if items:
            self._tell("append", start, items)

    def insert(self, index, item):
        super().insert(index, item)
        self._tell("reset", 0, [])

    def pop(self, *args):
        out = super().pop(*args)
        self._tell("reset", 0, [])
        return out

    def clear(self):
        super().clear()
        self._tell("reset", 0, [])

    def __delitem__(self, key):
        super().__delitem__(key)
        self._tell("reset", 0, [])


class Signals:
    def __setattr__(self, name, value):
        if name == "history" and not isinstance(value, _History):
            value = _History(value, self)
        object.__setattr__(self, name, value)
        if name in _WATCHED_FLAGS:
            listener = self.__dict__.get("on_state_change")
            if listener is not None:
                try:
                    listener(self)
                except Exception:
                    pass
        elif name == "history":
            listener = self.__dict__.get("on_history_change")
            if listener is not None:
                try:
                    listener("reset", 0, [])
                except Exception:
                    pass

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

    def add_message(self, role, content, speaker=None, source=None, from_host=False):
        msg = {"role": role, "content": content}
        if speaker:
            msg["speaker"] = speaker
        if source:
            msg["source"] = source
        if from_host:
            msg["from_host"] = True     # may give Minecraft orders (game_orders.py)
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