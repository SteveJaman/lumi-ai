"""
Long-term memory module.

ChromaDB-backed. Every ~10 messages, a background reflection pass asks
the LLM to extract facts from the recent conversation. Each fact is
stored with metadata: timestamp, importance (0-1), emotional_valence,
session_id, and user_id.

Retrieval reweights results by importance and recency, not just semantic
similarity. Older memories naturally fade; important recent ones surface.

Phase 10.15 — per-user isolation
--------------------------------
Every stored fact carries a `user_id` matching HOST.user_id at write
time. Every read path (get_prompt_injection, query_temporal, list_all,
get_by_id, export_all, update_memory, delete_memory) filters by the
current user. A migration pass on init backfills legacy rows (created
before this phase) with user_id="anthony" so the default host keeps
seeing their existing history.
"""

from modules.module import Module
from constants import (
    AI_NAME, HOST, MEMORY_PROMPT, LLM_ENDPOINT, MODEL_NAME,
    MEMORY_QUERY_MESSAGE_COUNT, MEMORY_RECALL_COUNT,
    FAST_MODEL_NAME,
)
from modules.llm_providers import chat_sync, LLMResponse
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


REFLECTION_PROMPT = """Analyze the conversation excerpt below and extract key personal facts, preferences, or context about the user that would be useful to remember long-term.

For each fact, output ONE JSON object on its own line with exactly these keys:
  {"fact": "short statement of the fact", "importance": 0.0-1.0, "valence": "one-word emotion or null"}

Guidance:
  - fact:      a concise, standalone statement with NO subject name.
               Bad:  "he said X."
               Bad:  "Anthony prefers Python over JavaScript."
               Good: "Prefers Python over JavaScript."
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


W_SIMILARITY = 0.50
W_IMPORTANCE = 0.30
W_RECENCY    = 0.20
RECENCY_HALFLIFE_DAYS = 30.0
RETRIEVAL_BUFFER = 3

DEFAULT_IMPORTANCE = 0.5
LEGACY_USER_ID = "anthony"

_BAD_VALENCE_STRINGS = {"", "null", "none", "nil", "undefined", "n/a"}


_TEMPORAL_PATTERNS = [
    (re.compile(r"\b(yesterday)\b", re.IGNORECASE), 1),
    (re.compile(r"\b(today)\b", re.IGNORECASE), 0),
    (re.compile(r"\b(last|this)\s+week\b", re.IGNORECASE), 7),
    (re.compile(r"\b(last|this)\s+month\b", re.IGNORECASE), 30),
    (re.compile(r"\b(\d+)\s+days?\s+ago\b", re.IGNORECASE), None),
]


def _parse_temporal_hint(text):
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


def _clean_valence(raw):
    if raw is None:
        return None
    if not isinstance(raw, str):
        return None
    v = raw.strip().lower()
    if v in _BAD_VALENCE_STRINGS:
        return None
    if not re.fullmatch(r"[a-z\-]{2,20}", v):
        return None
    return v


def _current_user_id():
    """The currently-active user_id. Every memory written or read is
    scoped to this. Host switching via web login updates this live."""
    try:
        return HOST.user_id or LEGACY_USER_ID
    except Exception:
        return LEGACY_USER_ID


class Memory(Module):

    def __init__(self, signals, enabled=True):
        super().__init__(signals, enabled)

        self.API = self.API(self)
        self.prompt_injection.text = ""
        self.prompt_injection.priority = 60

        self.processed_count = 0

        self.session_id = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")

        self.chroma_client = chromadb.PersistentClient(
            path="./memories/chroma.db",
            settings=Settings(anonymized_telemetry=False),
        )
        self.collection = self.chroma_client.get_or_create_collection(
            name="neuro_collection"
        )
        count = self.collection.count()
        print(f"MEMORY: Loaded {count} memories from database.")
        if count == 0:
            print("MEMORY: Empty database. Importing from memoryinit.json")
            self.API.import_json(path="./memories/memoryinit.json")

        self._migrate_user_ids()
        self._normalize_all_metadata()

        signals.memory_module = self

    def clear_database(self):
        self.API.wipe()

    # ---------------------------------------------------------------
    # Per-user migration (Phase 10.15)
    # ---------------------------------------------------------------
    def _migrate_user_ids(self):
        """Assign user_id=LEGACY_USER_ID to any memory created before
        per-user isolation existed. Idempotent; silent when there's
        nothing to do."""
        try:
            total = self.collection.count()
            if total == 0:
                return
            results = self.collection.get()
        except Exception as e:
            print(f"\033[91mMEMORY: user_id migration read failed: {e}\033[0m")
            return

        ids = results.get("ids") or []
        metas = results.get("metadatas") or []
        if not ids:
            return

        to_update_ids = []
        to_update_metas = []
        for mem_id, meta in zip(ids, metas):
            meta = dict(meta or {})
            if not meta.get("user_id"):
                meta["user_id"] = LEGACY_USER_ID
                to_update_ids.append(mem_id)
                to_update_metas.append(meta)

        if not to_update_ids:
            return

        try:
            self.collection.update(
                ids=to_update_ids, metadatas=to_update_metas,
            )
            print(
                f"\033[90mMEMORY: Backfilled user_id on "
                f"{len(to_update_ids)} legacy memories "
                f"(assigned to '{LEGACY_USER_ID}').\033[0m"
            )
        except Exception as e:
            print(f"\033[91mMEMORY: user_id migration update failed: {e}\033[0m")

    # ---------------------------------------------------------------
    # Metadata migration
    # ---------------------------------------------------------------
    def _normalize_all_metadata(self):
        try:
            total = self.collection.count()
            if total == 0:
                return
            results = self.collection.get()
        except Exception as e:
            print(f"\033[91mMEMORY: normalize read failed: {e}\033[0m")
            return

        ids = results.get("ids") or []
        metas = results.get("metadatas") or []
        if not ids:
            return

        now_iso = datetime.datetime.now().isoformat(timespec="seconds")
        fixed = 0

        for id_, meta in zip(ids, metas):
            meta = dict(meta or {})
            changed = False

            ts = meta.get("timestamp")
            if not ts or not isinstance(ts, str) or not ts.strip():
                meta["timestamp"] = now_iso
                changed = True

            imp = meta.get("importance")
            try:
                f = float(imp)
                if f != f or f < 0.0 or f > 1.0:
                    raise ValueError
            except (TypeError, ValueError):
                meta["importance"] = DEFAULT_IMPORTANCE
                changed = True

            cleaned_val = _clean_valence(meta.get("valence"))
            if cleaned_val != meta.get("valence"):
                meta["valence"] = cleaned_val
                changed = True

            sid = meta.get("session_id")
            if not sid or not isinstance(sid, str):
                meta["session_id"] = "import"
                changed = True

            if not meta.get("user_id"):
                meta["user_id"] = LEGACY_USER_ID
                changed = True

            if changed:
                try:
                    self.collection.update(ids=[id_], metadatas=[meta])
                    fixed += 1
                except Exception as e:
                    print(
                        f"\033[91mMEMORY: normalize update failed "
                        f"for {id_}: {e}\033[0m"
                    )

        if fixed:
            print(
                f"\033[90mMEMORY: Normalized metadata on {fixed} "
                f"memories.\033[0m"
            )

    # ---------------------------------------------------------------
    # Retrieval scoring
    # ---------------------------------------------------------------
    def _score_memory(self, doc, meta, distance, now):
        similarity = 1.0 - float(distance) if distance is not None else 0.5
        similarity = max(0.0, min(1.0, similarity))

        importance = float(meta.get("importance", DEFAULT_IMPORTANCE))
        importance = max(0.0, min(1.0, importance))

        ts_str = meta.get("timestamp")
        if ts_str:
            try:
                ts = datetime.datetime.fromisoformat(ts_str)
                age_days = (now - ts).total_seconds() / 86400.0
                recency = math.exp(-age_days / RECENCY_HALFLIFE_DAYS)
            except Exception:
                recency = 0.5
        else:
            recency = 0.5

        return (
            W_SIMILARITY * similarity
            + W_IMPORTANCE * importance
            + W_RECENCY * recency
        )

    def get_prompt_injection(self):
        query = ""

        for message in self.signals.history[-MEMORY_QUERY_MESSAGE_COUNT:]:
            if message.get("role") == "user" and message.get("content"):
                query += HOST.name + ": " + str(message["content"]) + "\n"
            elif message.get("role") == "assistant" and message.get("content"):
                query += AI_NAME + ": " + str(message["content"]) + "\n"

        if not query.strip():
            self.prompt_injection.text = ""
            return self.prompt_injection

        fetch_n = max(MEMORY_RECALL_COUNT * RETRIEVAL_BUFFER, MEMORY_RECALL_COUNT + 3)
        total = self.collection.count()
        if total == 0:
            self.prompt_injection.text = ""
            return self.prompt_injection
        fetch_n = min(fetch_n, total)

        cutoff = _parse_temporal_hint(query)

        try:
            results = self.collection.query(
                query_texts=query,
                n_results=fetch_n,
                where={"user_id": _current_user_id()},
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
            if self.processed_count >= len(self.signals.history):
                self.processed_count = 0

            if len(self.signals.history) - self.processed_count >= 10:
                if getattr(self.signals, 'new_message', False) or \
                   getattr(self.signals, 'AI_thinking', False):
                    await asyncio.sleep(2)
                    continue

                messages = copy.deepcopy(
                    self.signals.history[-(len(self.signals.history) - self.processed_count):]
                )

                chat_section = ""
                for message in messages:
                    role = HOST.name if message.get("role") == "user" else AI_NAME
                    chat_section += f"{role}: {message.get('content', '')}\n"

                prompt = REFLECTION_PROMPT.replace("<<CHAT_SECTION>>", chat_section)

                try:
                    resp: LLMResponse = chat_sync(
                        FAST_MODEL_NAME,
                        [{"role": "user", "content": prompt}],
                        think=False,
                        keep_alive="30s",
                        options={
                            "temperature": 0.3,
                            "num_predict": 400,
                            "num_ctx": 8192,
                        },
                    )
                    if resp.content:
                        self._store_extracted_memories(resp.content)
                except Exception as e:
                    print(f"MEMORY: Error during reflection query: {e}")

                self.processed_count = len(self.signals.history)

            await asyncio.sleep(5)

    def _store_extracted_memories(self, raw_text):
        if not raw_text:
            return

        now_iso = datetime.datetime.now().isoformat(timespec="seconds")
        user_id = _current_user_id()

        stored = 0
        for line in raw_text.splitlines():
            line = line.strip()
            if not line or line.startswith("```"):
                continue
            line = line.rstrip(",;")

            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
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

            valence = _clean_valence(obj.get("valence"))

            metadata = {
                "type": "short-term",
                "timestamp": now_iso,
                "importance": importance,
                "valence": valence,
                "session_id": self.session_id,
                "user_id": user_id,
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
            try:
                from modules.log_hub import emit as hub_emit
                hub_emit(
                    self.signals, "memory",
                    f"stored {stored} new memories for '{user_id}' "
                    f"(session {self.session_id})"
                )
            except Exception:
                pass

    # ===============================================================
    # Public API
    # ===============================================================
    class API:
        def __init__(self, outer):
            self.outer = outer

        def _is_owned_by(self, mem_id, user_id):
            try:
                res = self.outer.collection.get(ids=[mem_id])
            except Exception:
                return False
            if not res or not res.get("ids"):
                return False
            meta = (res.get("metadatas") or [{}])[0] or {}
            return meta.get("user_id") == user_id

        # -----------------------------------------------------------
        # Mutations
        # -----------------------------------------------------------
        def create_memory(self, data, importance=0.9, valence=None):
            now_iso = datetime.datetime.now().isoformat(timespec="seconds")
            new_id = str(uuid.uuid4())
            try:
                imp = float(importance)
            except (TypeError, ValueError):
                imp = 0.9
            imp = max(0.0, min(1.0, imp))
            val = _clean_valence(valence)

            self.outer.collection.upsert(
                ids=[new_id],
                documents=[data],
                metadatas=[{
                    "type": "manual",
                    "timestamp": now_iso,
                    "importance": imp,
                    "valence": val,
                    "session_id": self.outer.session_id,
                    "user_id": _current_user_id(),
                }],
            )
            return new_id

        def delete_memory(self, mem_id):
            user_id = _current_user_id()
            if not self._is_owned_by(mem_id, user_id):
                return False
            try:
                self.outer.collection.delete(ids=[mem_id])
                return True
            except Exception as e:
                print(f"\033[91mMEMORY: delete failed: {e}\033[0m")
                return False

        def wipe(self):
            """Wipe only the current user's memories."""
            user_id = _current_user_id()
            try:
                res = self.outer.collection.get(
                    where={"user_id": user_id}
                )
                ids = res.get("ids") or []
                if ids:
                    self.outer.collection.delete(ids=ids)
                print(f"MEMORY: Wiped {len(ids)} memories for '{user_id}'.")
            except Exception as e:
                print(f"\033[91mMEMORY: wipe failed: {e}\033[0m")

        def wipe_all_users(self):
            """Full collection wipe. Used by Settings > Advanced."""
            try:
                self.outer.chroma_client.delete_collection(name="neuro_collection")
            except Exception:
                pass
            self.outer.collection = (
                self.outer.chroma_client.get_or_create_collection(
                    name="neuro_collection"
                )
            )
            print("MEMORY: Full collection wiped and reset.")

        def update_memory(self, mem_id, document=None,
                          importance=None, valence=None):
            if not mem_id:
                return False, "Missing id."
            user_id = _current_user_id()
            if not self._is_owned_by(mem_id, user_id):
                return False, "Memory not found."

            try:
                existing = self.outer.collection.get(ids=[mem_id])
            except Exception as e:
                return False, f"Lookup failed: {e}"

            if not existing or not existing.get("ids"):
                return False, "Memory not found."

            old_doc = (existing.get("documents") or [""])[0] or ""
            old_meta = dict((existing.get("metadatas") or [{}])[0] or {})

            new_doc = document if document is not None else old_doc
            new_meta = dict(old_meta)

            if importance is not None:
                try:
                    imp = float(importance)
                except (TypeError, ValueError):
                    return False, "Importance must be a number."
                new_meta["importance"] = max(0.0, min(1.0, imp))

            if valence is not None:
                new_meta["valence"] = _clean_valence(valence)

            try:
                self.outer.collection.update(
                    ids=[mem_id],
                    documents=[new_doc],
                    metadatas=[new_meta],
                )
            except Exception as e:
                return False, f"Update failed: {e}"
            return True, None

        # -----------------------------------------------------------
        # Reads
        # -----------------------------------------------------------
        def count(self):
            try:
                return int(self.outer.collection.count())
            except Exception:
                return 0

        def count_for_user(self, user_id=None):
            user_id = user_id or _current_user_id()
            try:
                res = self.outer.collection.get(
                    where={"user_id": user_id}
                )
                return len(res.get("ids") or [])
            except Exception:
                return 0

        def _row_from_parts(self, mem_id, doc, meta):
            meta = meta or {}
            return {
                "id": mem_id,
                "fact": doc or "",
                "timestamp": meta.get("timestamp") or "",
                "importance": meta.get("importance", DEFAULT_IMPORTANCE),
                "valence": meta.get("valence"),
                "session_id": meta.get("session_id") or "",
                "type": meta.get("type") or "short-term",
                "user_id": meta.get("user_id") or "",
            }

        def list_all(self, limit=200, offset=0, sort="recent"):
            user_id = _current_user_id()
            try:
                results = self.outer.collection.get(
                    where={"user_id": user_id}
                )
            except Exception as e:
                print(f"\033[91mMEMORY: list_all failed: {e}\033[0m")
                return {"total": 0, "memories": []}

            ids = results.get("ids") or []
            docs = results.get("documents") or []
            metas = results.get("metadatas") or []
            total = len(ids)

            rows = [
                self._row_from_parts(i, d, m)
                for i, d, m in zip(ids, docs, metas)
            ]

            if sort == "oldest":
                rows.sort(key=lambda r: r["timestamp"] or "")
            elif sort == "importance":
                rows.sort(key=lambda r: -(float(r["importance"] or 0.0)))
            else:
                rows.sort(key=lambda r: r["timestamp"] or "", reverse=True)

            return {"total": total, "memories": rows[offset:offset + limit]}

        def get_by_id(self, mem_id):
            user_id = _current_user_id()
            try:
                res = self.outer.collection.get(ids=[mem_id])
            except Exception:
                return None
            if not res or not res.get("ids"):
                return None
            doc = (res.get("documents") or [""])[0]
            meta = (res.get("metadatas") or [{}])[0] or {}
            if meta.get("user_id") != user_id:
                return None
            return self._row_from_parts(res["ids"][0], doc, meta)

        def export_all(self):
            user_id = _current_user_id()
            try:
                res = self.outer.collection.get(
                    where={"user_id": user_id}
                )
            except Exception:
                return []
            ids = res.get("ids") or []
            docs = res.get("documents") or []
            metas = res.get("metadatas") or []
            return [
                {
                    "id": i,
                    "fact": d or "",
                    "metadata": m or {},
                }
                for i, d, m in zip(ids, docs, metas)
            ]

        # -----------------------------------------------------------
        # Semantic + temporal query
        # -----------------------------------------------------------
        def query_temporal(self, query, since=None, limit=10):
            user_id = _current_user_id()

            if not query or not query.strip():
                if not since:
                    return []

            try:
                res = self.outer.collection.get(
                    where={"user_id": user_id}
                )
            except Exception as e:
                print(f"\033[91mMEMORY: query_temporal read failed: {e}\033[0m")
                return []
            total = len(res.get("ids") or [])
            if total == 0:
                return []

            cutoff = None
            if since:
                cutoff = _parse_temporal_hint(since)
            if cutoff is None and query:
                cutoff = _parse_temporal_hint(query)

            fetch_n = min(max(limit * 4, 20), total)
            effective_query = query.strip() if query and query.strip() else (since or "memory")

            try:
                results = self.outer.collection.query(
                    query_texts=effective_query,
                    n_results=fetch_n,
                    where={"user_id": user_id},
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

            hits.sort(
                key=lambda h: h["timestamp"] or "9999-12-31T23:59:59"
            )

            return hits[:limit]

        def format_recall(self, hits):
            if not hits:
                return "No memories matched."

            lines = []
            for h in hits:
                ts_display = ""
                if h["timestamp"]:
                    try:
                        ts = datetime.datetime.fromisoformat(h["timestamp"])
                        now = datetime.datetime.now()
                        if ts.date() == now.date():
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
                meta = dict(memory.get("metadata", {}) or {})

                ts = meta.get("timestamp")
                if not ts or not isinstance(ts, str) or not ts.strip():
                    meta["timestamp"] = datetime.datetime.now().isoformat(
                        timespec="seconds"
                    )

                imp = meta.get("importance")
                try:
                    f = float(imp)
                    if f != f or f < 0.0 or f > 1.0:
                        raise ValueError
                except (TypeError, ValueError):
                    meta["importance"] = DEFAULT_IMPORTANCE

                meta["valence"] = _clean_valence(meta.get("valence"))

                sid = meta.get("session_id")
                if not sid or not isinstance(sid, str):
                    meta["session_id"] = "import"

                if not meta.get("user_id"):
                    meta["user_id"] = LEGACY_USER_ID

                self.outer.collection.upsert(
                    ids=[str(memory["id"])],
                    documents=[memory["document"]],
                    metadatas=[meta],
                )