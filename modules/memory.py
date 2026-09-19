"""
Long-term memory module.

ChromaDB-backed. Every ~10 messages, a background reflection pass asks the
LLM to extract facts from the recent conversation. Each fact is stored with
metadata: timestamp, importance (0-1), emotional_valence, and session_id.

Retrieval reweights results by importance and recency, not just semantic
similarity. Older memories naturally fade; important recent ones surface.
"""

from modules.module import Module
from constants import (
    AI_NAME, HOST_NAME, MEMORY_PROMPT, LLM_ENDPOINT, MODEL_NAME,
    MEMORY_QUERY_MESSAGE_COUNT, MEMORY_RECALL_COUNT,
)
from chromadb.config import Settings
import chromadb
import requests
import json
import uuid
import asyncio
import copy
import os
import math
import datetime
import re


# ===============================================================
# REFLECTION PROMPT
# ---------------------------------------------------------------
# Rewritten to produce structured JSON, not free text. Each fact
# gets an importance score and an emotional-valence tag so
# retrieval can reweight later.
#
# NOTE: This is a .replace() template, NOT a .format() template.
# The JSON example below contains literal braces, which would break
# str.format(). The chat text is injected via the <<CHAT_SECTION>>
# sentinel instead.
# ===============================================================
REFLECTION_PROMPT = """Analyze the conversation excerpt below and extract key personal facts, preferences, or context about the user that would be useful to remember long-term.

For each fact, output ONE JSON object on its own line with exactly these keys:
  {"fact": "short statement of the fact", "importance": 0.0-1.0, "valence": "one-word emotion or null"}

Guidance:
  - fact:      a concise, standalone statement. Bad: "he said X." Good: "Anthony prefers Python over JavaScript."
  - importance: 0.9+ for durable facts (name, preferences, major life events),
                0.5-0.8 for useful context (ongoing projects, current goals),
                0.3-0.5 for transient details (mood today, what he ate).
  - valence:   one word describing the emotional tone when the fact was
               mentioned, or null if neutral. Examples: "frustrated",
               "excited", "tired", "satisfied", "curious", "concerned".

Output ONLY the JSON lines. No headers, no prose, no markdown fences.

CONVERSATION:
<<CHAT_SECTION>>
"""


# ===============================================================
# RETRIEVAL WEIGHTS
# ===============================================================
W_SIMILARITY = 0.50
W_IMPORTANCE = 0.30
W_RECENCY    = 0.20
RECENCY_HALFLIFE_DAYS = 30.0      # score drops to 0.5 after this many days
RETRIEVAL_BUFFER = 3              # fetch 3× more than we need, then rerank

# Default importance for memories that predate this schema change
DEFAULT_IMPORTANCE = 0.5


# ===============================================================
# TEMPORAL QUERY DETECTION
# ===============================================================
_TEMPORAL_PATTERNS = [
    (re.compile(r"\b(yesterday)\b", re.IGNORECASE), 1),
    (re.compile(r"\b(today)\b", re.IGNORECASE), 0),
    (re.compile(r"\b(last|this)\s+week\b", re.IGNORECASE), 7),
    (re.compile(r"\b(last|this)\s+month\b", re.IGNORECASE), 30),
    (re.compile(r"\b(\d+)\s+days?\s+ago\b", re.IGNORECASE), None),
]


def _parse_temporal_hint(text):
    """Return a datetime cutoff if the text looks like a temporal query,
    or None if it isn't one."""
    if not text:
        return None
    now = datetime.datetime.now()
    for pattern, days in _TEMPORAL_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        if days is None:
            try:
                days = int(m.group(1))
            except (ValueError, IndexError):
                continue
        return now - datetime.timedelta(days=days)
    return None


def _format_age_hint(ts, now):
    """Human-readable age like '(2d ago)' or '(3w ago)'. Empty if no ts."""
    if ts is None:
        return ""
    age = now - ts
    if age.days == 0:
        hours = age.seconds // 3600
        if hours >= 1:
            return f"({hours}h ago)"
        return "(just now)"
    if age.days == 1:
        return "(yesterday)"
    if age.days < 7:
        return f"({age.days}d ago)"
    if age.days < 30:
        return f"({age.days // 7}w ago)"
    return f"({age.days // 30}mo ago)"


# ===============================================================
# MODULE
# ===============================================================
class Memory(Module):

    def __init__(self, signals, enabled=True):
        super().__init__(signals, enabled)

        self.API = self.API(self)
        self.prompt_injection.text = ""
        self.prompt_injection.priority = 60

        self.processed_count = 0

        # Unique id for this process run, so memories made this session
        # can be grouped and (later) pruned if needed.
        self.session_id = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")

        self.chroma_client = chromadb.PersistentClient(
            path="./memories/chroma.db",
            settings=Settings(anonymized_telemetry=False),
        )
        self.collection = self.chroma_client.get_or_create_collection(
            name="neuro_collection"
        )
        print(f"MEMORY: Loaded {self.collection.count()} memories from database.")
        if self.collection.count() == 0:
            print("MEMORY: No memories found in database. Importing from memoryinit.json")
            self.API.import_json(path="./memories/memoryinit.json")

        # Expose ourselves on signals so other modules (prompter, agent tools)
        # can reach the temporal query API without importing this file.
        signals.memory_module = self

    def clear_database(self):
        """Bridge method called by prompter to wipe ChromaDB collection."""
        self.API.wipe()

    # ---------------------------------------------------------------
    # Retrieval
    # ---------------------------------------------------------------
    def _score_memory(self, doc, meta, distance, now):
        """Combined score = similarity + importance + recency."""
        # ChromaDB returns distance (lower = better). Convert to similarity.
        similarity = 1.0 - float(distance) if distance is not None else 0.5
        similarity = max(0.0, min(1.0, similarity))

        importance = float(meta.get("importance", DEFAULT_IMPORTANCE))
        importance = max(0.0, min(1.0, importance))

        # Recency: exponential decay by half-life
        ts_str = meta.get("timestamp")
        if ts_str:
            try:
                ts = datetime.datetime.fromisoformat(ts_str)
                age_days = (now - ts).total_seconds() / 86400.0
                recency = math.exp(-age_days / RECENCY_HALFLIFE_DAYS)
            except Exception:
                recency = 0.5
        else:
            # Old memories without a timestamp get a neutral recency
            recency = 0.5

        return (
            W_SIMILARITY * similarity
            + W_IMPORTANCE * importance
            + W_RECENCY * recency
        )

    def get_prompt_injection(self):
        query = ""

        # Build a query from recent history
        for message in self.signals.history[-MEMORY_QUERY_MESSAGE_COUNT:]:
            if message.get("role") == "user" and message.get("content"):
                query += HOST_NAME + ": " + str(message["content"]) + "\n"
            elif message.get("role") == "assistant" and message.get("content"):
                query += AI_NAME + ": " + str(message["content"]) + "\n"

        if not query.strip():
            self.prompt_injection.text = ""
            return self.prompt_injection

        # How many to over-fetch before rerank
        fetch_n = max(MEMORY_RECALL_COUNT * RETRIEVAL_BUFFER, MEMORY_RECALL_COUNT + 3)
        total = self.collection.count()
        if total == 0:
            self.prompt_injection.text = ""
            return self.prompt_injection
        fetch_n = min(fetch_n, total)

        # Detect temporal hint so we can filter
        cutoff = _parse_temporal_hint(query)

        try:
            results = self.collection.query(
                query_texts=query,
                n_results=fetch_n,
            )
        except Exception as e:
            print(f"\033[91mMEMORY: query failed: {e}\033[0m")
            self.prompt_injection.text = ""
            return self.prompt_injection

        if not results or not results.get("ids") or not results["ids"][0]:
            self.prompt_injection.text = ""
            return self.prompt_injection

        ids = results["ids"][0]
        docs = results["documents"][0]
        metas = results.get("metadatas", [[]])[0] or [{} for _ in ids]
        dists = results.get("distances", [[]])[0] or [None for _ in ids]

        now = datetime.datetime.now()

        scored = []
        for doc, meta, dist in zip(docs, metas, dists):
            meta = meta or {}

            # If a temporal hint was present, filter out older memories
            if cutoff is not None:
                ts_str = meta.get("timestamp")
                if ts_str:
                    try:
                        ts = datetime.datetime.fromisoformat(ts_str)
                        if ts < cutoff:
                            continue
                    except Exception:
                        pass

            score = self._score_memory(doc, meta, dist, now)
            scored.append((score, doc, meta))

        scored.sort(key=lambda x: x[0], reverse=True)
        top = scored[:MEMORY_RECALL_COUNT]

        if not top:
            self.prompt_injection.text = ""
            return self.prompt_injection

        # Format for the prompt, including age hints
        lines = [f"{AI_NAME} remembers these relevant details:"]
        for score, doc, meta in top:
            ts_str = meta.get("timestamp")
            age_hint = ""
            if ts_str:
                try:
                    ts = datetime.datetime.fromisoformat(ts_str)
                    age_hint = " " + _format_age_hint(ts, now)
                except Exception:
                    pass

            lines.append(f"- {doc}{age_hint}")

        lines.append("End of memory recall.")
        self.prompt_injection.text = "\n".join(lines)
        return self.prompt_injection

    # ---------------------------------------------------------------
    # Reflection loop
    # ---------------------------------------------------------------
    async def run(self):
        while not self.signals.terminate:
            if self.processed_count > len(self.signals.history):
                self.processed_count = 0

            if len(self.signals.history) - self.processed_count >= 10:
                if getattr(self.signals, 'new_message', False) or \
                   getattr(self.signals, 'AI_thinking', False):
                    # Skip this cycle so we don't collide with active turns
                    await asyncio.sleep(2)
                    continue

                messages = copy.deepcopy(
                    self.signals.history[-(len(self.signals.history) - self.processed_count):]
                )

                chat_section = ""
                for message in messages:
                    role = HOST_NAME if message.get("role") == "user" else AI_NAME
                    chat_section += f"{role}: {message.get('content', '')}\n"

                # NOTE: .replace() not .format() — the prompt body contains
                # literal JSON braces that would confuse str.format().
                prompt = REFLECTION_PROMPT.replace("<<CHAT_SECTION>>", chat_section)

                data = {
                    "model": MODEL_NAME,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                    "options": {"temperature": 0.3, "num_predict": 400},
                }

                try:
                    response = requests.post(
                        f"{LLM_ENDPOINT}/api/chat",
                        json=data,
                        timeout=45,
                    )
                    if response.status_code == 200:
                        raw = response.json()["message"]["content"]
                        self._store_extracted_memories(raw)
                except requests.exceptions.Timeout:
                    print("MEMORY: Reflection query timed out. Skipping this turn.")
                except Exception as e:
                    print(f"MEMORY: Error during reflection query: {e}")

                self.processed_count = len(self.signals.history)

            await asyncio.sleep(5)

    # ---------------------------------------------------------------
    # Parse the JSON lines and upsert with metadata
    # ---------------------------------------------------------------
    def _store_extracted_memories(self, raw_text):
        if not raw_text:
            return

        now_iso = datetime.datetime.now().isoformat(timespec="seconds")

        stored = 0
        for line in raw_text.splitlines():
            line = line.strip()
            if not line or line.startswith("```"):
                continue
            # Strip trailing commas, semicolons that some models add
            line = line.rstrip(",;")

            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                # Fall back to regex-scan for a JSON object inside the line
                match = re.search(r"\{.*\}", line)
                if not match:
                    continue
                try:
                    obj = json.loads(match.group(0))
                except json.JSONDecodeError:
                    continue

            fact = (obj.get("fact") or obj.get("content") or "").strip()
            if not fact:
                continue

            try:
                importance = float(obj.get("importance", DEFAULT_IMPORTANCE))
            except (TypeError, ValueError):
                importance = DEFAULT_IMPORTANCE
            importance = max(0.0, min(1.0, importance))

            valence = obj.get("valence")
            if isinstance(valence, str):
                valence = valence.strip().lower() or None
            else:
                valence = None

            metadata = {
                "type": "short-term",
                "timestamp": now_iso,
                "importance": importance,
                "valence": valence,
                "session_id": self.session_id,
            }

            try:
                self.collection.upsert(
                    ids=[str(uuid.uuid4())],
                    documents=[fact],
                    metadatas=[metadata],
                )
                stored += 1
            except Exception as e:
                print(f"MEMORY: upsert failed: {e}")

        if stored:
            print(f"\033[90mMEMORY: Stored {stored} new memories "
                  f"(session {self.session_id}).\033[0m")

    # ---------------------------------------------------------------
    # Public API
    # ---------------------------------------------------------------
    class API:
        def __init__(self, outer):
            self.outer = outer

        def create_memory(self, data):
            """Manual memory insertion — used by !remember in prompter."""
            now_iso = datetime.datetime.now().isoformat(timespec="seconds")
            self.outer.collection.upsert(
                ids=[str(uuid.uuid4())],
                documents=[data],
                metadatas=[{
                    "type": "manual",
                    "timestamp": now_iso,
                    "importance": 0.9,
                    "valence": None,
                    "session_id": self.outer.session_id,
                }],
            )

        def delete_memory(self, id):
            self.outer.collection.delete(id)

        def wipe(self):
            try:
                self.outer.chroma_client.delete_collection(name="neuro_collection")
            except Exception:
                pass
            self.outer.collection = self.outer.chroma_client.get_or_create_collection(
                name="neuro_collection"
            )
            print("MEMORY: Database successfully wiped and reset.")

        def query_temporal(self, query, since=None, limit=10):
            """Query memories with temporal semantics.

            Unlike get_prompt_injection(), this does NOT reweight by
            recency — temporal queries are about the past, so sorting
            oldest-first is the point.

            Args:
                query: free-text semantic query
                since: optional natural-language time hint
                       ("yesterday", "last week", "3 days ago")
                limit: max results to return

            Returns:
                List of dicts: {fact, timestamp, importance, valence, age_hint}
                sorted ascending by timestamp (oldest first).
            """
            if not query or not query.strip():
                # Allow a pure temporal query with no text — the user
                # asked "what did we do last week" and we still want hits.
                if not since:
                    return []

            total = self.outer.collection.count()
            if total == 0:
                return []

            # Resolve temporal cutoff if a hint was given.
            cutoff = None
            if since:
                cutoff = _parse_temporal_hint(since)
            if cutoff is None and query:
                # Also try to parse a temporal hint out of the query itself.
                cutoff = _parse_temporal_hint(query)

            # Over-fetch to survive the cutoff filter.
            fetch_n = min(max(limit * 4, 20), total)

            # ChromaDB needs a non-empty string for query_texts.
            effective_query = query.strip() if query and query.strip() else (since or "memory")

            try:
                results = self.outer.collection.query(
                    query_texts=effective_query,
                    n_results=fetch_n,
                )
            except Exception as e:
                print(f"\033[91mMEMORY: query_temporal failed: {e}\033[0m")
                return []

            if not results or not results.get("ids") or not results["ids"][0]:
                return []

            docs = results["documents"][0]
            metas = results.get("metadatas", [[]])[0] or [{} for _ in docs]

            now = datetime.datetime.now()
            hits = []

            for doc, meta in zip(docs, metas):
                meta = meta or {}

                ts_str = meta.get("timestamp")
                ts = None
                if ts_str:
                    try:
                        ts = datetime.datetime.fromisoformat(ts_str)
                    except Exception:
                        ts = None

                if cutoff is not None and ts is not None:
                    if ts < cutoff:
                        continue

                try:
                    importance = float(meta.get("importance", DEFAULT_IMPORTANCE))
                except (TypeError, ValueError):
                    importance = DEFAULT_IMPORTANCE

                hits.append({
                    "fact": doc,
                    "timestamp": ts_str or "",
                    "importance": importance,
                    "valence": meta.get("valence"),
                    "age_hint": _format_age_hint(ts, now),
                })

            # Sort ascending by time so the narrative reads chronologically.
            # Memories without timestamps go last (they can't be placed).
            hits.sort(
                key=lambda h: h["timestamp"] or "9999-12-31T23:59:59"
            )

            return hits[:limit]

        def format_recall(self, hits):
            """Render query_temporal output as a readable block.

            Used both by the !recall command (terminal output) and by the
            recall_memory tool (as the tool-result string fed back to the
            model)."""
            if not hits:
                return "No memories matched."

            lines = []
            for h in hits:
                # Trim to a friendly date; keep the time if it's today.
                ts_display = ""
                if h["timestamp"]:
                    try:
                        ts = datetime.datetime.fromisoformat(h["timestamp"])
                        now = datetime.datetime.now()
                        if ts.date() == now.date():
                            # %#I is Windows strftime for "no leading zero"
                            try:
                                ts_display = ts.strftime("%#I:%M %p")
                            except ValueError:
                                ts_display = ts.strftime("%I:%M %p")
                        else:
                            ts_display = ts.strftime("%Y-%m-%d %H:%M")
                    except Exception:
                        ts_display = h["timestamp"][:16]

                importance_marker = ""
                if h["importance"] >= 0.85:
                    importance_marker = " ★"
                elif h["importance"] >= 0.6:
                    importance_marker = " ·"

                valence = f" [{h['valence']}]" if h.get("valence") else ""
                header = f"[{ts_display or 'unknown'}]"
                lines.append(f"{header}{importance_marker}{valence} {h['fact']}")

            return "\n".join(lines)

        def import_json(self, path="./memories/memoryinit.json"):
            if not os.path.exists(path):
                print(f"MEMORY: No initialization file found at {path}. "
                      f"Starting with empty collection.")
                return

            with open(path, "r", encoding="utf-8") as file:
                try:
                    data = json.load(file)
                except json.JSONDecodeError:
                    return

            for memory in data.get("memories", []):
                meta = memory.get("metadata", {}) or {}
                # Backfill required fields so scoring works on old data
                meta.setdefault("timestamp", datetime.datetime.now().isoformat(timespec="seconds"))
                meta.setdefault("importance", DEFAULT_IMPORTANCE)
                meta.setdefault("valence", None)
                meta.setdefault("session_id", "import")

                self.outer.collection.upsert(
                    memory["id"],
                    documents=memory["document"],
                    metadatas=meta,
                )