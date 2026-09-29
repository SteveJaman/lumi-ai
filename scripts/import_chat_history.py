"""
One-shot importer: reads data/logs/chat_history.json, runs the
reflection prompt over chunks of it, and stores the extracted facts
into the same ChromaDB collection the runtime Memory module uses.

Lives under scripts/ but runs as if it were at the workspace root —
the bootstrap below adds the workspace root to sys.path and chdir's
into it, so `from constants import ...` works.

Run from anywhere:

    python scripts\\import_chat_history.py --dry-run
    python -m scripts.import_chat_history --dry-run

Safe to re-run — it skips facts already stored (exact text match).
Pass --wipe to start clean.

Note: ChromaDB takes an exclusive lock on data/memories/chroma.db.
Close Lumi (main.py) before running this script.
"""

import os
import sys

# ---- Path bootstrap ---------------------------------------------------
# This file lives in scripts/. Everything the rest of the workspace
# expects (constants.py, data/, config/) sits one directory up.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.chdir(_ROOT)
# -----------------------------------------------------------------------

import json
import re
import uuid
import argparse
import datetime

import requests
import chromadb
from chromadb.config import Settings

from constants import (
    LLM_ENDPOINT,
    FAST_MODEL_NAME,
    MODEL_NAME,
    HOST,
    AI_NAME,
    DATA_ROOT,
)


# Runtime storage locations — under data/ after the folder reorg.
HISTORY_PATH = os.path.join(DATA_ROOT, "logs", "chat_history.json")
CHROMA_PATH  = os.path.join(DATA_ROOT, "memories", "chroma.db")
COLLECTION   = "neuro_collection"

CHUNK_SIZE = 12
MAX_CHUNK_CHARS = 6000
PREFERRED_MODEL = FAST_MODEL_NAME


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


_BAD_VALENCE_STRINGS = {"", "null", "none", "nil", "undefined", "n/a"}


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


def _pick_model():
    try:
        r = requests.get(f"{LLM_ENDPOINT}/api/tags", timeout=5)
        r.raise_for_status()
        names = [m.get("name", "") for m in r.json().get("models", [])]
    except Exception as e:
        print(f"\033[91mERROR: cannot reach Ollama at {LLM_ENDPOINT} — {e}\033[0m")
        sys.exit(1)

    if not names:
        print("\033[91mERROR: Ollama returned zero models.\033[0m")
        sys.exit(1)

    base_preferred = PREFERRED_MODEL.split(":")[0].lower()
    for n in names:
        if base_preferred in n.lower():
            return n

    base_main = MODEL_NAME.split(":")[0].lower()
    for n in names:
        if base_main in n.lower():
            return n

    print(f"\033[93mWARNING: neither '{PREFERRED_MODEL}' nor "
          f"'{MODEL_NAME}' found. Using '{names[0]}'.\033[0m")
    return names[0]


def _load_history(path):
    if not os.path.exists(path):
        print(f"\033[91mERROR: {path} not found.\033[0m")
        print(f"\033[90m  (cwd is {os.getcwd()})\033[0m")
        sys.exit(1)
    with open(path, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as e:
            print(f"\033[91mERROR: chat_history.json is not valid JSON — {e}\033[0m")
            sys.exit(1)
    if not isinstance(data, list):
        print("\033[91mERROR: expected a JSON list at the top level.\033[0m")
        sys.exit(1)
    msgs = [
        m for m in data
        if isinstance(m, dict)
        and m.get("role") in ("user", "assistant")
        and (m.get("content") or "").strip()
    ]
    return msgs


def _chunk(messages, size):
    for i in range(0, len(messages), size):
        yield messages[i:i + size]


def _render_chunk(chunk):
    lines = []
    for m in chunk:
        role = HOST.name if m["role"] == "user" else AI_NAME
        content = str(m.get("content", "")).strip()
        lines.append(f"{role}: {content}")
    text = "\n".join(lines)
    if len(text) > MAX_CHUNK_CHARS:
        text = text[:MAX_CHUNK_CHARS] + "\n... [chunk truncated]"
    return text


def _call_llm(model, chat_section):
    prompt = REFLECTION_PROMPT.replace("<<CHAT_SECTION>>", chat_section)
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "think": False,           # <-- Qwen3: skip reasoning, write to content
        "keep_alive": "30s",
        "options": {
            "temperature": 0.3,
            "num_predict": 600,
            "num_ctx": 8192,
        },
    }
    try:
        r = requests.post(f"{LLM_ENDPOINT}/api/chat",
                          json=payload, timeout=90)
        r.raise_for_status()
        data = r.json()
        msg = data.get("message", {}) or {}
        content = (msg.get("content") or "").strip()

        # If content is empty but there's a thinking field, the model
        # went into reasoning mode anyway. Log it so we know.
        if not content:
            thinking = (msg.get("thinking") or "").strip()
            print(f"\033[93m  model returned empty content.\033[0m")
            if thinking:
                print(f"\033[90m  thinking field was {len(thinking)} chars — "
                      f"model is in reasoning mode despite think=False.\033[0m")
                print(f"\033[90m  thinking preview: {thinking[:200]!r}\033[0m")
            else:
                print(f"\033[90m  raw response keys: {list(data.keys())}\033[0m")
                print(f"\033[90m  message keys: {list(msg.keys())}\033[0m")
                print(f"\033[90m  full response preview: "
                      f"{str(data)[:400]}\033[0m")
        return content
    except Exception as e:
        print(f"\033[91m  LLM call failed: {type(e).__name__}: {e}\033[0m")
        return ""


def _parse_facts(raw):
    facts = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("```"):
            continue
        line = line.rstrip(",;")

        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", line)
            if not m:
                continue
            try:
                obj = json.loads(m.group(0))
            except json.JSONDecodeError:
                continue

        fact = (obj.get("fact") or obj.get("content") or "").strip()
        if not fact:
            continue

        try:
            imp = float(obj.get("importance", 0.5))
        except (TypeError, ValueError):
            imp = 0.5
        imp = max(0.0, min(1.0, imp))

        facts.append({
            "fact": fact,
            "importance": imp,
            "valence": _clean_valence(obj.get("valence")),
        })
    return facts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wipe", action="store_true",
                    help="Delete the entire collection before importing.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Show what would be stored without writing anything.")
    ap.add_argument("--chunk-size", type=int, default=CHUNK_SIZE,
                    help=f"Messages per reflection chunk (default {CHUNK_SIZE}).")
    args = ap.parse_args()

    print(f"\033[96m=== Lumi chat history importer ===\033[0m")
    print(f"  cwd:     {os.getcwd()}")
    print(f"  history: {HISTORY_PATH}")
    print(f"  chroma:  {CHROMA_PATH}")
    print(f"  chunk:   {args.chunk_size} messages")
    print(f"  dry-run: {args.dry_run}")
    print(f"  wipe:    {args.wipe}")
    print()

    messages = _load_history(HISTORY_PATH)
    print(f"\033[90mLoaded {len(messages)} user/assistant messages.\033[0m")

    if not messages:
        print("\033[93mNothing to do.\033[0m")
        return

    model = _pick_model()
    print(f"\033[90mUsing model: {model}\033[0m")
    print()

    client = chromadb.PersistentClient(
        path=CHROMA_PATH,
        settings=Settings(anonymized_telemetry=False),
    )

    if args.wipe and not args.dry_run:
        try:
            client.delete_collection(COLLECTION)
            print("\033[93mWiped existing collection.\033[0m")
        except Exception:
            pass

    collection = client.get_or_create_collection(COLLECTION)
    before_count = collection.count()
    print(f"\033[90mCollection has {before_count} memories before import.\033[0m")

    existing_texts = set()
    if before_count:
        existing = collection.get()
        for d in (existing.get("documents") or []):
            existing_texts.add((d or "").strip().lower())

    session_id = datetime.datetime.now().strftime("import-%Y%m%d-%H%M%S")

    chunks = list(_chunk(messages, args.chunk_size))
    print(f"\033[90mSplit into {len(chunks)} chunk(s).\033[0m")
    print()

    total_facts = 0
    total_new = 0
    total_dup = 0

    for idx, chunk in enumerate(chunks, 1):
        print(f"\033[96m[{idx}/{len(chunks)}] Processing "
              f"{len(chunk)}-message chunk...\033[0m")
        chat_section = _render_chunk(chunk)
        raw = _call_llm(model, chat_section)

        if not raw:
            print(f"\033[91m  no response — skipping chunk\033[0m")
            continue

        facts = _parse_facts(raw)
        total_facts += len(facts)
        print(f"  extracted {len(facts)} fact(s)")

        for f in facts:
            key = f["fact"].strip().lower()
            if key in existing_texts:
                total_dup += 1
                print(f"\033[90m  skip dup: {f['fact']!r}\033[0m")
                continue

            if args.dry_run:
                print(f"\033[92m  would store: {f['fact']!r} "
                      f"(imp={f['importance']:.2f}, val={f['valence']})\033[0m")
                existing_texts.add(key)
                total_new += 1
                continue

            now_iso = datetime.datetime.now().isoformat(timespec="seconds")
            metadata = {
                "type": "import",
                "timestamp": now_iso,
                "importance": f["importance"],
                "valence": f["valence"],
                "session_id": session_id,
            }
            try:
                collection.upsert(
                    ids=[str(uuid.uuid4())],
                    documents=[f["fact"]],
                    metadatas=[metadata],
                )
                existing_texts.add(key)
                total_new += 1
                print(f"\033[92m  + {f['fact']!r} "
                      f"(imp={f['importance']:.2f}, val={f['valence']})\033[0m")
            except Exception as e:
                print(f"\033[91m  upsert failed: {e}\033[0m")

    after_count = collection.count()
    print()
    print(f"\033[96m=== Summary ===\033[0m")
    print(f"  facts extracted: {total_facts}")
    print(f"  new facts stored: {total_new}")
    print(f"  duplicates skipped: {total_dup}")
    print(f"  collection size: {before_count} → {after_count}")
    if args.dry_run:
        print(f"\n\033[93mDRY RUN — nothing was written. "
              f"Re-run without --dry-run to commit.\033[0m")


if __name__ == "__main__":
    main()