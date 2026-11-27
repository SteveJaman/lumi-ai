"""
Lumi Island bridge: the island's mini-apps (Home, Chat, Calendar) over the
same authenticated socket as tool approvals (modules/hitl_server.py).

Client -> server (after the hello handshake), each with an optional "id"
that is echoed back:
    get_home                       -> home {snapshot, state, prefs}
    get_weather {refresh}          -> weather {weather} | {error, code}
    get_calendar {start, end}      -> calendar {events, at} | {error, code}
    get_chat {limit}               -> chat {messages: [{n, role, content}]}
    chat_send {text}               -> chat_ack {ok} | error
    quick_capture {kind, text|url} -> quick_capture {ok, kind, name} | {ok: false, error}
                                      kind "text": a long-term memory (memory.API.create_memory,
                                      as the web client's "create memory"); kind "link": a small
                                      note naming the URL, saved to data/library/ and indexed
                                      through knowledge.ingest_files (the page is not fetched).
                                      Files go through knowledge_upload_* like a drag-and-drop.
    heartbeat_set_paused {paused, minutes, perception}
                                   -> heartbeat_paused {paused, until}
                                      Focus Mode: pauses the heartbeat (and, unless
                                      perception=false, the ambient sampler) until `until`,
                                      at most MAX_PAUSE_MIN, so a closed island can't
                                      leave Lumi silent forever.
    focus_summary {minutes}        -> focus_summary {summary | null}
                                      one sentence from the ambient sampler's recent
                                      observations, written by the local fast model
    get_shortcuts                  -> shortcuts {shortcuts: [{id, title, url}]}
    shortcut_add {title, url}      -> shortcuts {ok, shortcut, shortcuts} | {ok: false, error}
    shortcut_delete {sid}          -> shortcuts {ok, shortcuts} | {ok: false, error}
                                      Hub launcher (modules/shortcuts_manager.py); the island
                                      opens a link itself, in the default browser
    media_get                      -> media_state {state}       (keeps the bridge polling ~25 s)
    media_cmd {action, value?}     -> media_state {state, message} | {ok: false, error}
                                      toggle/play/pause/next/previous/shuffle/repeat/like,
                                      volume (0-100 or +5/-5), play_uri, queue_uri, transfer, play_liked
    media_library {q?}             -> media_library {playlists, liked, recent, shows, albums} | {results}
    media_library {shelf, offset}  -> media_library {shelf, items, total, offset, next_offset}   (one page)
    media_tracks {kind, id, offset}-> media_tracks {items, total, offset, next_offset}
                                      kind: playlist (yours only, Spotify's rule) | album | liked
    media_next                     -> media_next {current, queue, devices}
                                      Media tab (modules/media_bridge.py: Spotify only)
    quick_note {text, kind?}       -> quick_note {ok, kind} | {ok: false, error}
                                      Hub's idea / homework / grocery box: a long-term memory
                                      labelled with its kind (guessed from the text if absent)

Server -> client (pushed, never polled):
    lumi_status {state}            idle / thinking / speaking
    lumi_mode {gaming}             Gaming Mode toggled
    chat_message {message}         a message was added to the chat
    chat_reset                     the chat was wiped or rewritten
    snapshot {snapshot}            next event + unread changed
    prefs {prefs}                  12/24 h, units or weather city changed in the web client
    shortcuts_update {shortcuts}   a shortcut was added or removed (any island, the web API)
    media_state {state}            now playing changed; "art" is left out when the track hasn't
                                   changed since the last push (the island keeps the last one)

Everything the island can do is something the signed-in owner can already do
in the web client; the socket is loopback-only, origin-checked, and (when
Lumi launches the island) needs the per-launch secret.
"""

import asyncio
import datetime
import re
import time
from urllib.parse import urlparse

from modules import hitl_server, home_info
from modules.workspace_hub import NotConnected, ProviderError

MAX_CHAT_CHARS = 4000
MAX_MESSAGE_CHARS = 2000        # per message sent to the island
DEFAULT_CHAT_LIMIT = 40
MIN_SEND_GAP_S = 0.4
MAX_CAPTURE_CHARS = 2000        # one quick-capture memory
MAX_URL_CHARS = 2000
MAX_PAUSE_MIN = 180             # a focus pause expires on its own after this
FOCUS_LOOKBACK_S = 600          # observations this long before the block count too
_URL_RE = re.compile(r"^https?://\S+$", re.I)
MAX_NOTE_CHARS = 1000
NOTE_KINDS = {"idea": "Idea", "homework": "Homework", "grocery": "Grocery list", "note": "Note"}
_NOTE_PREFIX_RE = re.compile(r"^\s*(idea|hw|homework|todo|grocery|groceries|buy|shopping)\s*[:\-]\s*", re.I)
_HOMEWORK_RE = re.compile(r"\b(homework|hw|assignment|essay|quiz|exam|midterm|final|lab \d+|due|canvas|"
                          r"chapter|problem set|pset|study|project)\b", re.I)
_GROCERY_RE = re.compile(r"\b(buy|grocer(y|ies)|milk|eggs|bread|rice|chicken|beef|fruit|veggies|vegetables|"
                         r"cheese|butter|coffee|snacks|toilet paper|shopping list|pick up)\b", re.I)


def note_kind(text: str) -> tuple:
    """(kind, text without a "hw:" / "buy:" / "idea:" prefix). Kind is one of
    NOTE_KINDS; a plain thought is an idea."""
    t = str(text or "").strip()
    m = _NOTE_PREFIX_RE.match(t)
    if m:
        word = m.group(1).lower()
        kind = ("idea" if word == "idea" else "homework" if word in ("hw", "homework", "todo")
                else "grocery")
        return kind, t[m.end():].strip() or t
    if _HOMEWORK_RE.search(t):
        return "homework", t
    if _GROCERY_RE.search(t):
        return "grocery", t
    return "idea", t


def link_note(url: str, now: datetime.datetime) -> str:
    """The library note for a captured link: the URL and when, nothing fetched."""
    p = urlparse(url)
    return (f"# Saved link: {p.netloc}{p.path if p.path not in ('', '/') else ''}\n\n"
            f"{url}\n\nSaved from Lumi Island Quick Capture on {now:%Y-%m-%d %H:%M}.\n")


def link_label(url: str) -> str:
    p = urlparse(url)
    label = p.netloc + (p.path if p.path not in ("", "/") else "")
    return label if len(label) <= 60 else label[:57] + "…"


def _paused(signals, name: str) -> bool:
    return time.time() < float(getattr(signals, name, 0) or 0)


def heartbeat_paused(signals) -> bool:
    """Focus Mode's pause, read by modules/heartbeat.py."""
    return _paused(signals, "heartbeat_paused_until")


def perception_paused(signals) -> bool:
    """Focus Mode's pause, read by modules/ambient_perception.py."""
    return _paused(signals, "perception_paused_until")

_HIDDEN_SOURCES = ("sdk_event", "command", "watchdog_event", "announce_event")


def visible(msg) -> bool:
    """The messages a person would see in the chat (the web client's rule)."""
    return (isinstance(msg, dict)
            and msg.get("role") in ("user", "assistant")
            and not msg.get("hidden")
            and msg.get("source") not in _HIDDEN_SOURCES
            and isinstance(msg.get("content"), str)
            and bool(msg["content"].strip()))


def shape(n: int, msg: dict) -> dict:
    return {"n": n, "role": msg["role"], "content": msg["content"][:MAX_MESSAGE_CHARS]}


def _first_sentence(text: str, limit: int = 160) -> str:
    text = " ".join(str(text or "").split())
    m = re.match(r"(.+?[.!?])(\s|$)", text)
    out = (m.group(1) if m else text)[:limit].rstrip(" ,;:")
    return out[:1].lower() + out[1:] if out else out


def summarize_focus(seen) -> str:
    """One short clause for "You were working on: …", from (window, description)
    pairs. The local fast model writes it; if that fails, the newest
    description's first sentence. Screen notes never leave this PC."""
    notes = "\n".join(f"- {w or 'unknown window'}: {d}" for w, d in seen[-5:])
    try:
        from constants import FAST_MODEL_NAME, MODEL_NAME, FAST_MODEL_ENABLED, FAST_MODEL_KEEP_ALIVE
        from modules import gaming_mode
        from modules.llm_providers import chat_sync
        model = FAST_MODEL_NAME if FAST_MODEL_ENABLED else MODEL_NAME
        resp = chat_sync(model, [
            {"role": "system", "content": "You summarize what someone was working on from short notes "
                                          "about their screen. Reply with one short phrase, no more than "
                                          "15 words, that completes \"You were working on: \". No preamble."},
            {"role": "user", "content": notes}],
            think=False, keep_alive=gaming_mode.keep_alive(FAST_MODEL_KEEP_ALIVE),
            options={"temperature": 0.2, "num_predict": 40})
        text = _first_sentence((resp.content or "").strip().strip('"').removeprefix("You were working on:").strip())
        if text:
            return text
    except Exception:
        pass
    return _first_sentence(seen[-1][1])


def install(signals, server=None, info=None) -> None:
    """Register the handlers and the push hooks. Safe to call once, at boot,
    after the approval server exists. `server` is for tests; production uses
    the module singleton."""
    reg = server.register_handler if server else hitl_server.register_handler
    send = server.send if server else hitl_server.send
    push = server.broadcast_threadsafe if server else hitl_server.broadcast_threadsafe
    state = {"last_send": 0.0}
    prefs_src = info if info is not None else home_info.instance()

    def hub():
        return getattr(signals, "workspace_hub", None)

    async def home(ws, data):
        h = hub()
        await send(ws, {
            "type": "home", "id": data.get("id"),
            "snapshot": h.get_compressed_snapshot() if h is not None else None,
            "state": hitl_server.HITLServer._status_of(signals),
            "prefs": prefs_src.get_prefs(),
        })

    async def weather(ws, data):
        base = {"type": "weather", "id": data.get("id")}
        loop = asyncio.get_running_loop()
        try:
            wx = await loop.run_in_executor(None, prefs_src.get_weather, bool(data.get("refresh")))
        except home_info.NotConfigured as e:
            return await send(ws, {**base, "weather": None, "error": str(e), "code": "not_configured"})
        except home_info.HomeInfoError as e:
            return await send(ws, {**base, "weather": None, "error": str(e), "code": "network"})
        await send(ws, {**base, "weather": wx})

    async def calendar(ws, data):
        base = {"type": "calendar", "id": data.get("id")}
        h = hub()
        if h is None:
            return await send(ws, {**base, "events": [], "error": "Workspace isn't available.", "code": "unavailable"})
        loop = asyncio.get_running_loop()
        try:
            res = await loop.run_in_executor(None, h.get_events_range, data.get("start"), data.get("end"),
                                             bool(data.get("refresh")))
        except NotConnected as e:
            return await send(ws, {**base, "events": [], "error": str(e), "code": "not_connected"})
        except ValueError as e:                       # InvalidInput is one
            return await send(ws, {**base, "events": [], "error": str(e), "code": "bad_request"})
        except ProviderError as e:
            return await send(ws, {**base, "events": [], "error": str(e), "code": "provider"})
        await send(ws, {**base, "events": res["events"], "at": res["at"]})

    async def chat(ws, data):
        try:
            limit = max(1, min(int(data.get("limit") or DEFAULT_CHAT_LIMIT), 200))
        except (TypeError, ValueError):
            limit = DEFAULT_CHAT_LIMIT
        hist = list(signals.history)
        shown = [shape(i, m) for i, m in enumerate(hist) if visible(m)][-limit:]
        await send(ws, {"type": "chat", "id": data.get("id"), "messages": shown})

    async def chat_send(ws, data):
        text = str(data.get("text") or "").strip()
        base = {"type": "chat_ack", "id": data.get("id")}
        if not text:
            return await send(ws, {**base, "ok": False, "error": "Nothing to send."})
        now = time.time()
        if now - state["last_send"] < MIN_SEND_GAP_S:
            return await send(ws, {**base, "ok": False, "error": "Slow down a little."})
        state["last_send"] = now
        # Same as typing in the web client on this PC: a host message.
        signals.history.append({"role": "user", "content": text[:MAX_CHAT_CHARS], "from_host": True})
        signals.new_message = True
        signals.last_message_time = now
        if hasattr(signals, "save_history"):
            signals.save_history()
        await send(ws, {**base, "ok": True})

    # ── Quick Capture ─────────────────────────────────────────────
    # Nothing here logs the captured text or URL.
    async def quick_capture(ws, data):
        kind = data.get("kind")
        base = {"type": "quick_capture", "id": data.get("id"), "kind": kind}
        loop = asyncio.get_running_loop()
        if kind == "text":
            text = str(data.get("text") or "").strip()
            if not text:
                return await send(ws, {**base, "ok": False, "error": "Nothing to save."})
            memory = getattr(signals, "memory", None)
            api = getattr(memory, "API", None)
            if api is None:
                return await send(ws, {**base, "ok": False, "error": "Memory isn't loaded."})
            try:
                await loop.run_in_executor(None, lambda: api.create_memory(
                    text[:MAX_CAPTURE_CHARS], importance=0.8, memory_type="manual"))
            except Exception as e:
                return await send(ws, {**base, "ok": False, "error": f"memory: {type(e).__name__}"})
            return await send(ws, {**base, "ok": True, "name": "memory"})
        if kind == "link":
            url = str(data.get("url") or "").strip()
            if len(url) > MAX_URL_CHARS or not _URL_RE.match(url) or not urlparse(url).netloc:
                return await send(ws, {**base, "ok": False, "error": "That isn't an http(s) link."})
            from modules import knowledge
            now = datetime.datetime.now()
            try:
                # A generic file name, so the URL never shows up in the ingest log.
                path = knowledge.save_bytes(f"Quick capture link {now:%Y-%m-%d %H%M%S}.md",
                                            link_note(url, now).encode("utf-8"))
                res = (await loop.run_in_executor(None, knowledge.ingest_files, signals, [path], "island"))[0]
            except Exception as e:
                return await send(ws, {**base, "ok": False, "error": str(e) or type(e).__name__})
            if not res["ok"]:
                return await send(ws, {**base, "ok": False, "error": res["error"] or "couldn't index it"})
            return await send(ws, {**base, "ok": True, "name": link_label(url)})
        await send(ws, {**base, "ok": False, "error": "Unknown capture kind."})

    # ── Hub: shortcuts and the quick-note box ─────────────────────
    from modules import shortcuts_manager

    async def shortcuts(ws, data):
        items = await asyncio.get_running_loop().run_in_executor(None, shortcuts_manager.list_shortcuts)
        await send(ws, {"type": "shortcuts", "id": data.get("id"), "shortcuts": items})

    async def shortcut_add(ws, data):
        base = {"type": "shortcuts", "id": data.get("id")}
        loop = asyncio.get_running_loop()
        try:
            item = await loop.run_in_executor(None, shortcuts_manager.add_shortcut, data.get("title"), data.get("url"))
        except shortcuts_manager.ShortcutError as e:
            return await send(ws, {**base, "ok": False, "error": str(e)})
        except OSError:
            return await send(ws, {**base, "ok": False, "error": "Couldn't save the shortcut."})
        items = await loop.run_in_executor(None, shortcuts_manager.list_shortcuts)
        await send(ws, {**base, "ok": True, "shortcut": item, "shortcuts": items})

    async def shortcut_delete(ws, data):
        base = {"type": "shortcuts", "id": data.get("id")}
        loop = asyncio.get_running_loop()
        try:
            ok = await loop.run_in_executor(None, shortcuts_manager.remove_shortcut, data.get("sid"))
        except OSError:
            return await send(ws, {**base, "ok": False, "error": "Couldn't save the change."})
        items = await loop.run_in_executor(None, shortcuts_manager.list_shortcuts)
        if not ok:
            return await send(ws, {**base, "ok": False, "error": "That shortcut is already gone.", "shortcuts": items})
        await send(ws, {**base, "ok": True, "shortcuts": items})

    # Like Quick Capture, nothing here logs the note's text.
    async def quick_note(ws, data):
        base = {"type": "quick_note", "id": data.get("id")}
        raw = str(data.get("text") or "").strip()
        if not raw:
            return await send(ws, {**base, "ok": False, "error": "Nothing to save."})
        kind, text = note_kind(raw)
        if data.get("kind") in NOTE_KINDS:
            kind = data["kind"]
        api = getattr(getattr(signals, "memory", None), "API", None)
        if api is None:
            return await send(ws, {**base, "ok": False, "error": "Memory isn't loaded."})
        entry = f"{NOTE_KINDS[kind]}: {text[:MAX_NOTE_CHARS]}"
        try:
            await asyncio.get_running_loop().run_in_executor(None, lambda: api.create_memory(
                entry, importance=0.8, memory_type="manual"))
        except Exception as e:
            return await send(ws, {**base, "ok": False, "error": f"memory: {type(e).__name__}"})
        await send(ws, {**base, "ok": True, "kind": kind})

    # ── Media tab ─────────────────────────────────────────────────
    from modules import media_bridge

    def media(fn):
        async def handler(ws, data):
            kind = {"media_library": "media_library", "media_next": "media_next",
                    "media_tracks": "media_tracks"}.get(data.get("type"), "media_state")
            base = {"type": kind, "id": data.get("id")}
            try:
                out = await asyncio.get_running_loop().run_in_executor(None, fn, data)
            except ValueError as e:
                return await send(ws, {**base, "ok": False, "error": str(e)})
            except Exception as e:
                return await send(ws, {**base, "ok": False, "error": str(e) or type(e).__name__})
            await send(ws, {**base, "ok": True, **out})
        return handler

    def _cmd(data):
        msg, st = media_bridge.bridge().command(str(data.get("action") or ""), data.get("value"))
        return {"state": st, "message": msg}

    reg("media_get", media(lambda d: {"state": media_bridge.bridge().state()}))
    reg("media_cmd", media(_cmd))
    def _library(d):
        if d.get("shelf"):
            return media_bridge.bridge().library_page(str(d["shelf"]), int(d.get("offset") or 0),
                                                      int(d.get("limit") or 20))
        return media_bridge.bridge().library(str(d.get("q") or ""))

    reg("media_library", media(_library))
    reg("media_tracks", media(lambda d: media_bridge.bridge().tracks(
        str(d.get("kind") or ""), str(d.get("item_id") or d.get("pid") or ""), int(d.get("offset") or 0),
        int(d.get("limit") or 50))))
    reg("media_next", media(lambda d: media_bridge.bridge().up_next()))

    last_art = {"key": None}

    def push_media(st):
        out = dict(st)
        if out.get("key") == last_art["key"]:
            out.pop("art", None)                        # same track: the island already has it
        else:
            last_art["key"] = out.get("key")
        push({"type": "media_state", "state": out})
    media_bridge.bridge().add_listener(push_media)

    # ── Focus Mode ────────────────────────────────────────────────
    async def set_paused(ws, data):
        paused = bool(data.get("paused"))
        try:
            minutes = max(1.0, min(float(data.get("minutes") or 60), MAX_PAUSE_MIN))
        except (TypeError, ValueError):
            minutes = 60.0
        until = time.time() + minutes * 60 if paused else 0.0
        signals.heartbeat_paused_until = until
        signals.perception_paused_until = until if data.get("perception", True) else 0.0
        await send(ws, {"type": "heartbeat_paused", "id": data.get("id"), "paused": paused, "until": until})

    async def focus_summary(ws, data):
        base = {"type": "focus_summary", "id": data.get("id")}
        try:
            minutes = max(0.0, min(float(data.get("minutes") or 0), MAX_PAUSE_MIN))
        except (TypeError, ValueError):
            minutes = 0.0
        amb = getattr(signals, "ambient", None)
        since = time.time() - minutes * 60 - FOCUS_LOOKBACK_S
        try:
            seen = [(w, d) for t, w, d in (amb.get_recent_history(5) if amb else []) if t >= since and d]
        except Exception:
            seen = []
        if not seen:
            return await send(ws, {**base, "summary": None})
        summary = await asyncio.get_running_loop().run_in_executor(None, summarize_focus, seen)
        await send(ws, {**base, "summary": summary})

    reg("get_home", home)
    reg("get_weather", weather)
    reg("get_calendar", calendar)
    reg("get_chat", chat)
    reg("chat_send", chat_send)
    reg("quick_capture", quick_capture)
    reg("heartbeat_set_paused", set_paused)
    reg("focus_summary", focus_summary)
    reg("get_shortcuts", shortcuts)
    reg("shortcut_add", shortcut_add)
    reg("shortcut_delete", shortcut_delete)
    async def stage_manifest(ws, data):
        # The Island's Stage: what to fetch (modules/island_stage.py). Nothing is read here but names and sizes.
        from modules import island_stage
        out = await asyncio.get_running_loop().run_in_executor(None, island_stage.manifest)
        await send(ws, {"type": "stage_manifest", "id": data.get("id"), "ok": True, **out})

    async def stage_chunk(ws, data):
        from modules import island_stage
        base = {"type": "stage_chunk", "id": data.get("id")}
        chunk = await asyncio.get_running_loop().run_in_executor(
            None, island_stage.read_chunk, data.get("key"), data.get("offset", 0))
        if chunk is None:
            return await send(ws, {**base, "ok": False, "error": "That file isn't part of the Stage."})
        await send(ws, {**base, "ok": True, **chunk})

    reg("stage_manifest", stage_manifest)
    reg("stage_chunk", stage_chunk)
    reg("quick_note", quick_note)

    def on_history(kind, start, msgs):
        if kind == "reset":
            push({"type": "chat_reset"})
            return
        for offset, m in enumerate(msgs):
            if visible(m):
                push({"type": "chat_message", "message": shape(start + offset, m)})

    signals.on_history_change = on_history

    prefs_src.add_listener(lambda prefs: push({"type": "prefs", "prefs": prefs}))
    shortcuts_manager.add_listener(lambda items: push({"type": "shortcuts_update", "shortcuts": items}))

    h = hub()
    if h is not None:
        h.add_listener(lambda snap: push({"type": "snapshot", "snapshot": snap}))
