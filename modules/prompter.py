import asyncio
import time
import requests
import re
import json
import threading
import os
import datetime
import random
from ddgs import DDGS
import pypdf
from docx import Document

from constants import (
    LLM_ENDPOINT,
    MODEL_NAME,
    VISION_MODEL_NAME,
    SYSTEM_PROMPT,
    AI_NAME,
    STOP_STRINGS,
    WORKSPACE_ROOT,
    FAST_MODEL_NAME,
    FAST_MODEL_ENABLED,
    FAST_MODEL_NUM_CTX,
    FAST_MODEL_KEEP_ALIVE,
)
from modules.agent_tools import TOOL_SCHEMAS, CONFIRM_REQUIRED_TOOLS, execute_tool
from modules.session_state import SessionState

MAGENTA = "\033[95m"
GRAY = "\033[90m"
RESET = "\033[0m"
BOLD = "\033[1m"
GREEN = "\033[92m"
YELLOW = "\033[93m"

SENTENCE_SPLIT_PATTERN = re.compile(r'(?<=[.!?;\n])\s+')

MAX_AGENT_STEPS = 6
AGENT_REQUEST_TIMEOUT = 180
SESSION_CONTINUITY_TURNS = 3


# ===============================================================
# LOCAL TIME HELPER
# ===============================================================
def _get_local_now():
    try:
        from constants import TIMEZONE
        if TIMEZONE:
            from zoneinfo import ZoneInfo
            return datetime.datetime.now(ZoneInfo(TIMEZONE))
    except Exception:
        pass
    return datetime.datetime.now()


# ===============================================================
# COMMAND REGISTRY
# ===============================================================
COMMANDS = {
    "Agent Mode": {
        "!agent_on": "Enable agent mode for the session — Lumi can use tools",
        "!agent_off": "Disable agent mode — pure conversation only",
        "!agent": "Toggle agent mode on/off",
        "!agent <task>": "One-shot agent turn for <task>, then return to current mode",
    },
    "Persona": {
        "set mood <name>": "Switch mood profile (default, bratty, brocon, soft)",
        "switch mood <name>": "Alias for 'set mood'",
    },
    "Emotional": {
        "!state": "Show Lumi's current emotional state, traits, and active frame",
    },
    "Avatar": {
        "!set <name> on|off": "Set an expression state idempotently",
        "!expressions": "List expression state names and current on/off state",
        "!expression <name>": "Toggle a raw VTS hotkey (no state tracking)",
        "!move <location>": "Move Lumi's avatar to a preset position",
        "!hotkeys": "List raw VTS hotkeys on the loaded model",
        "!positions": "List preset VTS model positions",
    },
    "Memory": {
        "!recall <query>": "Search long-term memory for a topic, oldest-first",
        "wipe memory": "Clear all long-term ChromaDB memories",
        "clear memory": "Alias for 'wipe memory'",
        "reset memory": "Alias for 'wipe memory'",
    },
    "Session": {
        "!sleep": "Summarize the session and save state for next time",
    },
    "System": {
        "!list": "Show this command list",
        "!help": "Alias for '!list'",
        "!model": "Show current model routing state",
        "!self": "Show current self-model observations and session stats",
    },
}

AGENT_TOOLS = {
    "read_file": "Read a file inside the workspace (text, PDF, DOCX)",
    "write_file": "Create/overwrite a file (asks you to confirm)",
    "list_dir": "List files in a folder",
    "search_files": "grep the workspace",
    "run_command": "Run a shell command, capture output (asks confirm)",
    "run_in_terminal": "Open a NEW terminal window (asks confirm)",
    "web_search": "Search the web or look up weather/time",
    "browse_url": "Open a web page with Playwright",
    "recall_memory": "Search long-term memory for past facts or conversations",
    "vtube_set_expression": "Turn an appearance toggle on/off on Lumi's avatar",
    "vtube_move": "Move Lumi's avatar to a screen position",
}


# ===============================================================
# FALLBACK TEXT-FORM TOOL CALL PARSER
# ===============================================================
_TOOL_CALL_HINT_KEYS = {"name", "tool", "function", "tool_name"}

_KNOWN_TOOL_NAMES = {
    "read_file", "write_file", "list_dir", "search_files",
    "run_command", "run_in_terminal", "web_search", "browse_url",
    "recall_memory", "vtube_set_expression", "vtube_move",
}

# Names of expressions Lumi can trigger via vtube_set_expression.
# Used by the fabrication guard: if she wraps one of these in
# brackets in her prose (like [spiral_eyes]), she's narrating the
# effect instead of firing the tool.
_EXPRESSION_STATE_NAMES = frozenset({
    "hat", "jacket", "bag", "skirt",
    "ponytail_l", "ponytail_r",
    "controller", "heart_eyes", "spiral_eyes", "darkened",
})

def _extract_text_tool_calls(content: str):
    if not content:
        return []

    text = content.strip()
    text = re.sub(r"^```(?:json|tool_code)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    text = text.strip()

    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return [obj]
        if isinstance(obj, list):
            return [o for o in obj if isinstance(o, dict)]
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    found = []
    idx = 0
    while idx < len(text):
        brace = text.find("{", idx)
        if brace == -1:
            break
        try:
            obj, end = decoder.raw_decode(text, brace)
        except json.JSONDecodeError:
            idx = brace + 1
            continue
        if isinstance(obj, dict) and (_TOOL_CALL_HINT_KEYS & obj.keys()):
            found.append(obj)
        idx = end
    return found


def _normalize_text_tool_call(obj: dict):
    raw_name = obj.get("name") or obj.get("tool") or obj.get("tool_name")
    if isinstance(raw_name, dict):
        raw_name = raw_name.get("name")
    if isinstance(obj.get("function"), dict):
        raw_name = raw_name or obj["function"].get("name")
    if not raw_name or not isinstance(raw_name, str):
        return None

    args = (
        obj.get("arguments")
        or obj.get("parameters")
        or obj.get("args")
        or (obj.get("function") or {}).get("arguments")
        or {}
    )
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {}

    return {"function": {"name": raw_name, "arguments": args or {}}}


_BRACKET_TOOL_CALL = re.compile(
    r"\[\s*(?:emit\s+|use\s+|call\s+|tool\s+)?(\w+)\(([^)]*)\)\s*\]"
)


def _extract_bracket_tool_calls(content: str):
    if not content:
        return []

    found = []
    for match in _BRACKET_TOOL_CALL.finditer(content):
        name, args_str = match.group(1), match.group(2)
        if name not in _KNOWN_TOOL_NAMES:
            continue

        args = {}
        for key, dq, sq, bare in re.findall(
            r"(\w+)\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^,]+))", args_str
        ):
            if dq:
                args[key] = dq
            elif sq:
                args[key] = sq
            elif bare:
                raw = bare.strip()
                if raw.lower() in ("true", "false"):
                    args[key] = raw.lower() == "true"
                elif re.fullmatch(r"-?\d+", raw):
                    args[key] = int(raw)
                elif re.fullmatch(r"-?\d+\.\d+", raw):
                    args[key] = float(raw)
                else:
                    args[key] = raw

        found.append({"function": {"name": name, "arguments": args}})
    return found


_BRACKET_JSON_TOOL_CALL = re.compile(
    r"\[\s*(\w+)\s*(\{[^{}]*\})\s*\]"
)


def _extract_bracket_json_tool_calls(content: str):
    if not content:
        return []

    found = []
    for match in _BRACKET_JSON_TOOL_CALL.finditer(content):
        name, args_str = match.group(1), match.group(2)
        if name not in _KNOWN_TOOL_NAMES:
            continue
        try:
            args = json.loads(args_str)
        except json.JSONDecodeError:
            args = {}
        found.append({"function": {"name": name, "arguments": args or {}}})
    return found


# ===============================================================
# BRACKET-POSITIONAL RECOVERY
# ===============================================================
_BRACKET_POSITIONAL_TOOL_CALL = re.compile(
    r"\[\s*(\w+)((?:\s+(?:\"[^\"]*\"|'[^']*'|[^\s\]]+))*)\s*\]"
)

# Positional arg schema per tool — the ORDER args appear in a
# `[tool "a" b c]` call. Missing trailing args use their type zero.
_POSITIONAL_ARGS = {
    "vtube_set_expression": ["name", "enabled"],
    "vtube_move":           ["location"],
    "read_file":            ["path"],
    "list_dir":             ["path"],
    "search_files":         ["query", "path"],
    "run_command":          ["command"],
    "run_in_terminal":      ["command"],
    "web_search":           ["query"],
    "browse_url":           ["url"],
    "recall_memory":        ["query", "since"],
}

# Args that should be coerced from string → bool after parsing.
_BOOL_ARGS = {"enabled"}


def _extract_bracket_positional_tool_calls(content: str):
    """Catch `[tool]`, `[tool "value"]`, and `[tool "value" true]`
    hallucinations. Multiple positional args are mapped against the
    tool's declared arg list in order."""
    if not content:
        return []

    found = []
    for match in _BRACKET_POSITIONAL_TOOL_CALL.finditer(content):
        name = match.group(1)
        if name not in _KNOWN_TOOL_NAMES:
            continue

        raw_args = match.group(2) or ""
        tokens = re.findall(r'"[^"]*"|\'[^\']*\'|\S+', raw_args)

        values = []
        for tok in tokens:
            if (tok.startswith('"') and tok.endswith('"')) or \
               (tok.startswith("'") and tok.endswith("'")):
                values.append(tok[1:-1])
            else:
                values.append(tok)

        arg_names = _POSITIONAL_ARGS.get(name, [])
        args = {}
        for i, val in enumerate(values):
            if i >= len(arg_names):
                break
            arg_name = arg_names[i]
            if arg_name in _BOOL_ARGS:
                args[arg_name] = val.strip().lower() in (
                    "true", "1", "yes", "on"
                )
            else:
                args[arg_name] = val

        # If vtube_set_expression got only a name, assume "turn on".
        if name == "vtube_set_expression" and "name" in args \
                and "enabled" not in args:
            args["enabled"] = True

        found.append({"function": {"name": name, "arguments": args}})
    return found


# ===============================================================
# STATE NAME ALIASES
# ---------------------------------------------------------------
# Lumi sometimes uses natural-language words for expressions
# instead of the schema-defined state names. This map translates
# the common ones before validation. If she invents something
# not on this list, the validation error will tell her the correct
# name and she'll retry.
# ===============================================================
_STATE_NAME_ALIASES = {
    # Eyes
    "dizzy":             "spiral_eyes",
    "spiral":            "spiral_eyes",
    "spinning":          "spiral_eyes",
    "spiral_eye":        "spiral_eyes",
    "spirals":           "spiral_eyes",
    "heart":             "heart_eyes",
    "hearts":            "heart_eyes",
    "heart_eye":         "heart_eyes",
    "love_eyes":         "heart_eyes",
    "love":              "heart_eyes",
    # Shadow
    "shadow":            "darkened",
    "dark":              "darkened",
    "shadow_face":       "darkened",
    "angry":             "darkened",
    "dark_face":         "darkened",
    # Ponytails
    "ponytail_left":     "ponytail_l",
    "ponytail_right":    "ponytail_r",
    "left_ponytail":     "ponytail_l",
    "right_ponytail":    "ponytail_r",
    "ponytail_1":        "ponytail_l",
    "ponytail_2":        "ponytail_r",
    # Controller / bag
    "game_controller":   "controller",
    "handheld":          "controller",
    "gamepad":           "controller",
    "jk_bag":            "bag",
    "jkbag":             "bag",
    "school_bag":        "bag",
    "bag_jk":            "bag",
    # Hat / jacket / skirt
    "cap":               "hat",
    "coat":              "jacket",
    "dress":             "skirt",
}


_TOOL_CALL_SHAPE = re.compile(
    r'"name"\s*:\s*"(read_file|write_file|list_dir|search_files|'
    r'run_command|run_in_terminal|web_search|browse_url|recall_memory|'
    r'vtube_set_expression|vtube_move)"',
    re.IGNORECASE,
)


# ===============================================================
# CANONICAL FISH AUDIO TAGS
# ===============================================================
_CANONICAL_TAGS = frozenset({
    "happy", "sad", "angry", "excited", "calm", "nervous", "confident",
    "surprised", "satisfied", "delighted", "scared", "worried", "upset",
    "frustrated", "depressed", "empathetic", "embarrassed", "disgusted",
    "moved", "proud", "relaxed", "grateful", "curious", "sarcastic",
    "disdainful", "unhappy", "anxious", "hysterical", "indifferent",
    "uncertain", "doubtful", "confused", "disappointed", "regretful",
    "guilty", "ashamed", "jealous", "envious", "hopeful", "optimistic",
    "pessimistic", "nostalgic", "lonely", "bored", "contemptuous",
    "sympathetic", "compassionate", "determined", "resigned",
    "in a hurry tone", "shouting", "screaming", "whispering",
    "soft tone", "emphasis",
    "laughing", "chuckling", "sobbing", "crying loudly", "sighing",
    "groaning", "panting", "gasping", "yawning", "snoring", "clear throat",
    "break", "long-break", "audience laughing", "background laughter",
    "crowd laughing",
})

_BRACKET_ANY = re.compile(r"\[([^\]]{1,40})\]")


def _strip_non_canonical_tags(text):
    if not text:
        return text

    def _repl(match):
        inner = match.group(1).strip().lower()
        return match.group(0) if inner in _CANONICAL_TAGS else ""

    return _BRACKET_ANY.sub(_repl, text)


# ===============================================================
# COMMAND ERROR HINTS
# ===============================================================
def _command_error_hint(command: str, result: str) -> str:
    if not result:
        return ""
    lower = result.lower()

    if "no such file or directory" in lower or "can't open file" in lower:
        m = re.search(r"can't open file ['\"]([^'\"]+)['\"]", result)
        fname = os.path.basename(m.group(1)) if m else "the file"
        return (
            f"\n\nHINT: The file '{fname}' does not exist yet. You cannot run "
            f"a file before creating it. Call write_file to create it FIRST, "
            f"then re-run this command."
        )

    if "is not recognized as an internal or external command" in lower or \
       ("the term" in lower and "is not recognized" in lower):
        return (
            "\n\nHINT: This machine is Windows. Use PowerShell/cmd syntax, "
            "not bash. For Python use 'python', not 'python3'."
        )

    return ""


# ===============================================================
# SIMULATED-ACTION DETECTION
# ===============================================================
_ACTION_VERBS = re.compile(
    r"\b(modify|edit|change|update|add|remove|delete|write|create|make|"
    r"generate|save|run|execute|launch|open|read|check|look at)\b",
    re.IGNORECASE,
)

_ACTION_PERFECT = re.compile(
    r"\b(has|have|had|is|are|was|were)\s+(now\s+|just\s+|already\s+)?"
    r"(been\s+)?"
    r"(updated|modified|created|written|changed|saved|added|removed|deleted|"
    r"fixed|applied|checked|looked|reviewed|inspected|verified|tested|"
    r"searched|found|scanned|read|opened|run|ran|executed)\b",
    re.IGNORECASE,
)

_ACTION_FIRST_PERSON = re.compile(
    r"\bi\b\s*(?:have\s+|'ve\s+)?(?:just\s+|already\s+)?"
    r"(updated|modified|created|wrote|written|changed|saved|added|removed|"
    r"deleted|fixed|applied|ran|executed|opened|checked|looked|reviewed|"
    r"inspected|verified|tested|searched|found|scanned|read)\b",
    re.IGNORECASE,
)

_SIMULATED_ACTION = re.compile(
    r"\b(i'?ll (create|modify|edit|write|run|open|update)|"
    r"here'?s the (updated|new|modified) (script|file|code))\b",
    re.IGNORECASE,
)

_INTENT_ANNOUNCEMENT = re.compile(
    r"(?:"
    r"let'?s\s+(?:run|execute|write|create|make|build|edit|modify|update|check|test|fix)\b|"
    r"(?:now|next|then)\s*,?\s*(?:i'?ll|let'?s|let me|i will|i'?m going to)\s+"
    r"(?:run|execute|write|create|make|build|edit|modify|update|check|test|fix)\b|"
    r"i'?ll\s+(?:run|execute|write|create|make|build|edit|modify|update|check|test|fix)\b|"
    r"i'?m going to\s+(?:run|execute|write|create|make|build|edit|modify|update|check|test|fix)\b|"
    r"time to\s+(?:run|execute|write|create|make|build|edit|modify|update|check|test|fix)\b"
    r")",
    re.IGNORECASE,
)

_COMPLETION_OPENERS = {
    "done", "complete", "completed", "finished", "updated", "modified",
    "created", "written", "changed", "saved", "added", "removed", "deleted",
    "fixed", "applied",
}

_COMPLETION_PHRASES = (
    "there we go", "there you go", "done", "all set", "all done",
    "finished", "complete", "there it is", "voila", "voilà",
    "consider it", "taken care", "handled",
)

# Verb that indicates the user wants an appearance state changed.
_STATE_CHANGE_REQUEST = re.compile(
    r"\b(?:put on|take off|show|hide|wear|remove|drop|grab|look|darken"
    r"|toggle|switch to|change to)\b",
    re.IGNORECASE,
)

# Phrases the model uses to CLAIM a state change happened. Only used
# for fabrication detection when the user asked for a state change.
_STATE_CLAIM = re.compile(
    r"(?:"
    # "X's now on/off/gone/hidden/etc."
    r"\b\w+'s\s+(?:now\s+)?(?:on|off|gone|back|hidden|showing|out|in\s+hand|in\s+place)\b"
    r"|"
    # "X mode engaged" / "effect activated" / "look applied"
    r"\b(?:mode|effect|look|expression|toggle)\s+(?:engaged|activated|on|applied|enabled|set|active|ready)\b"
    r"|"
    # "Hat removed." / "Jacket re-applied." / "Bag discarded." — the
    # noun-first form Lumi uses when she lists multiple items.
    r"^\s*(?:the\s+)?(?:\w+[\s-]+){0,2}"
    r"(?:hat|jacket|bag|skirt|ponytail|controller|eyes|eye|face|look)"
    r"\s+(?:re-?)?"
    r"(?:removed|applied|discarded|restored|returned|stowed|hidden|shown|"
    r"put|taken|worn|dropped|grabbed|darkened|dismissed|toggled|reset|"
    r"set|on|off|back|away|out|done|fixed|changed|swapped|cleared|"
    r"reapplied|re-added|reverted|undone)\b"
    r"|"
    # Leading verb-ed on its own line: "Removed." / "Hidden."
    r"^\s*(?:on|off|hidden|darkened|removed|gone|visible|engaged|activated"
    r"|applied|restored|returned|discarded|dismissed|reverted|undone|cleared)\b"
    r"|"
    # First-person claims
    r"\b(?:i'?m\s+(?:now\s+)?(?:wearing|holding|showing|hiding))\b"
    r")",
    re.IGNORECASE | re.MULTILINE,
)

# Meta-claims of "everything is done" — Lumi uses this to wrap up a
# multi-item request without firing the individual tool calls.
_STATE_META_CLAIM = re.compile(
    r"\b(?:everything|all|all\s+of\s+it|the\s+whole\s+thing)\s+"
    r"(?:is\s+|has\s+been\s+|'s\s+)?"
    r"(?:reset|restored|cleared|set|good|fine|back|done|sorted|handled)\b",
    re.IGNORECASE,
)

_FILENAME_HINT = re.compile(r"\b[\w\-/]+\.\w{1,5}\b")

_REFUSAL = re.compile(
    r"\b(i can'?t|i cannot|i'?m sorry|i am sorry|i'?m unable|"
    r"i don'?t have the ability|as an ai)\b",
    re.IGNORECASE,
)


# ===============================================================
# READ-ONLY QUERY DETECTION
# ===============================================================
_READ_ONLY_QUERY = re.compile(
    r"\b(read|check|show|tell me|what(?:'s| is)|find|look at|list|"
    r"display|print|view|peek|examine|inspect|see)\b",
    re.IGNORECASE,
)

_WRITE_AUTHORIZING_QUERY = re.compile(
    r"\b(modify|edit|change|update|add to|add\s+\w+\s+to|fix|remove from|"
    r"delete|rewrite|replace|append|insert|create|write|make|generate)\b",
    re.IGNORECASE,
)


def _query_is_read_only(user_query: str) -> bool:
    if not user_query:
        return False
    if _WRITE_AUTHORIZING_QUERY.search(user_query):
        return False
    return bool(_READ_ONLY_QUERY.search(user_query))


def _looks_like_simulated_action(content: str, user_query: str = "") -> bool:
    if not content:
        return False

    # Scan the first three non-empty lines — models often put their
    # hallucinated tool call on line 1, then the "it worked" claim
    # on line 2 or 3.
    lines = [ln for ln in content.split("\n") if ln.strip()][:3]
    for raw_line in lines:
        cleaned = re.sub(r"\[[^\]]*\]", "", raw_line).lower().strip()
        if not cleaned:
            continue
        if any(cleaned.startswith(p) for p in _COMPLETION_PHRASES):
            return True

    # Targeted checks for state-change fabrication. Only fire when
    # the user actually asked for a state change — otherwise "Skirt"
    # or "on" could false-positive in normal conversation.
    if user_query and _STATE_CHANGE_REQUEST.search(user_query):
        head = " ".join(
            ln for ln in content.split("\n") if ln.strip()
        )[:250].lower()
        head_clean = re.sub(r"\[[^\]]*\]", "", head)
        if _STATE_CLAIM.search(head_clean):
            return True

        if _STATE_META_CLAIM.search(head_clean):
            return True

        for m in re.finditer(r"\[([^\]]{1,40})\]", content):
            inner = m.group(1).strip().lower()
            if inner in _EXPRESSION_STATE_NAMES:
                return True

    opener_match = re.match(r"^\s*(?:\[[^\]]*\]\s*)*([A-Za-z']+)", content)
    if opener_match and opener_match.group(1).lower() in _COMPLETION_OPENERS:
        return True

    if _ACTION_PERFECT.search(content):
        return True

    if _ACTION_FIRST_PERSON.search(content):
        return True

    if "```" in content and _SIMULATED_ACTION.search(content):
        return True

    if _SIMULATED_ACTION.search(content) and _ACTION_VERBS.search(content):
        return True

    if user_query and "```" in content:
        if _FILENAME_HINT.search(user_query) and _ACTION_VERBS.search(user_query):
            return True

    return False


def _result_looks_like_failure(result) -> bool:
    s = str(result)
    low = s.lower()
    if low.startswith("error"):
        return True
    if low.startswith("refused"):
        return True
    if s.startswith("Exit code ") and not s.startswith("Exit code 0"):
        return True
    return False


# ===============================================================
# SCREEN REFERENCE DETECTION
# ===============================================================
_SCREEN_REFERENCE = re.compile(
    r"\b(screen|monitor|display|desktop|window|"
    r"looking at|see (?:this|that|my)|"
    r"what am i|what'?s on|what is on|"
    r"this (?:page|file|code|error)|"
    r"can you see)\b",
    re.IGNORECASE,
)


# ===============================================================
# LIST-FORMAT CLEANUP (display only)
# ===============================================================
def _strip_list_formatting(text):
    if not text:
        return text

    parts = re.split(r"(```[\s\S]*?```)", text)
    cleaned = []

    for i, part in enumerate(parts):
        if i % 2 == 1:
            cleaned.append(part)
            continue

        part = re.sub(r"\*\*([^*]+)\*\*", r"\1", part)
        part = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"\1", part)
        part = re.sub(r"__([^_]+)__", r"\1", part)
        part = re.sub(r"^\s*#{1,6}\s*", "", part, flags=re.MULTILINE)
        part = re.sub(r"^\s*-{3,}\s*$", "", part, flags=re.MULTILINE)

        lines = part.split("\n")
        out_lines = []
        list_buffer = []

        def flush_list():
            if not list_buffer:
                return
            items = [x.rstrip(".,;:") for x in list_buffer if x.strip()]
            list_buffer.clear()
            if not items:
                return
            if len(items) == 1:
                out_lines.append(items[0] + ".")
            elif len(items) == 2:
                out_lines.append(f"{items[0]}, and {items[1]}.")
            else:
                out_lines.append(", ".join(items[:-1]) + f", and {items[-1]}.")

        for line in lines:
            bm = re.match(r"^\s*[-*+]\s+(.+)$", line)
            nm = re.match(r"^\s*\d+\.\s+(.+)$", line)
            if bm:
                list_buffer.append(bm.group(1).strip())
                continue
            if nm:
                list_buffer.append(nm.group(1).strip())
                continue
            flush_list()
            out_lines.append(line)

        flush_list()
        part = "\n".join(out_lines)
        part = re.sub(r"\n{3,}", "\n\n", part)
        cleaned.append(part)

    return "".join(cleaned).strip()


# ===============================================================
# MODEL ROUTING
# ===============================================================
_COMPLEXITY_MARKERS = re.compile(
    r"\b("
    r"analyze|explain in detail|write a|create a|design|architect|"
    r"debug|step by step|why does|why is|how does|how do|"
    r"compare|evaluate|summarize|reason|consider|weigh|"
    r"pros and cons|trade.?off|in depth|elaborate|"
    r"refactor|implement|algorithm|strategy|"
    r"spell|backward|reverse|anagram|how many|count|letter|"
    r"math|riddle|trick question|heavier"
    r")\b",
    re.IGNORECASE,
)

_TIME_QUERY = re.compile(
    r"\b(what time|what'?s the time|what day|what'?s the day|"
    r"what'?s the date|what date|current time|current date|"
    r"what'?s today|today'?s date|time is it|day is it|"
    r"check the (time|clock)|look at the (time|clock))\b",
    re.IGNORECASE,
)

_FAST_MODEL_AVAILABLE = None


def _check_fast_model_available():
    global _FAST_MODEL_AVAILABLE
    if _FAST_MODEL_AVAILABLE is not None:
        return _FAST_MODEL_AVAILABLE
    try:
        r = requests.get(f"{LLM_ENDPOINT}/api/tags", timeout=3)
        if r.status_code != 200:
            _FAST_MODEL_AVAILABLE = False
            return False
        names = [m.get("name", "") for m in r.json().get("models", [])]
        base = FAST_MODEL_NAME.split(":")[0].lower()
        _FAST_MODEL_AVAILABLE = any(base in n.lower() for n in names)
    except Exception:
        _FAST_MODEL_AVAILABLE = False
    return _FAST_MODEL_AVAILABLE


def _should_use_fast_model(user_text, agent_mode):
    if not FAST_MODEL_ENABLED:
        return False
    if agent_mode:
        return False
    if not _check_fast_model_available():
        return False
    if not user_text:
        return True
    text = user_text.strip()
    if len(text) > 280:
        return False
    if _COMPLEXITY_MARKERS.search(text):
        return False
    if _TIME_QUERY.search(text):
        return False
    return True


# ===============================================================
# EXTRA TOOL SCHEMAS
# ===============================================================
EXTRA_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web, or look up the current time/weather for a location.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What to search for."}
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browse_url",
            "description": "Open and read a specific web page URL.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The URL or domain to open."}
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall_memory",
            "description": (
                "Search Lumi's long-term memory for facts, preferences, or "
                "past conversations with the user. Use this when the user "
                "asks 'when did we talk about X', 'do you remember Y', "
                "'what did I say about Z', or references a previous session."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "since": {"type": "string"}
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "vtube_set_expression",
            "description": (
                "Turn an appearance toggle on your own avatar ON or OFF. Use "
                "this when the user says 'put on your hat', 'take off your "
                "jacket', 'show heart eyes', 'hide your ponytail', etc. "
                "Call this exactly ONCE per request with the DESIRED final "
                "state. This tool is idempotent — if the state already matches, "
                "nothing fires. Valid names: hat, jacket, bag, skirt, "
                "ponytail_l, ponytail_r, controller, heart_eyes, spiral_eyes, "
                "darkened. Pass an empty name to see the current state of each."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": (
                            "State name: hat, jacket, bag, skirt, ponytail_l, "
                            "ponytail_r, controller, heart_eyes, spiral_eyes, "
                            "darkened. Pass empty string to list all states."
                        )
                    },
                    "enabled": {
                        "type": "boolean",
                        "description": (
                            "True to turn the state ON, False to turn it OFF. "
                            "'put on your hat' → enabled=true. "
                            "'take off your jacket' → enabled=false."
                        )
                    }
                },
                "required": ["name", "enabled"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "vtube_move",
            "description": (
                "Move your own avatar to a preset screen position. Use when "
                "the user asks you to 'move to the corner', 'get out of the "
                "way', 'come back to center', etc. Call this exactly ONCE "
                "per request."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": (
                            "Position name. Pass empty string to list them."
                        )
                    }
                },
                "required": ["location"]
            }
        }
    },
]


MOOD_PROFILES = {
    "default": {
        "label": "Onee-san (Classic Older Sister)",
        "modifier": "You are Lumi, Anthony's older sister. You balance being superior, teasing, and effortlessly cool with a constant undercurrent of affection. You act like he'd be entirely helpless without you, but you secretly adore taking care of him."
    },
    "bratty": {
        "label": "Bratty / Teasing Onee-san",
        "modifier": "Lean into the classic anime teasing older sister trope. Act high-maintenance, lord minor favors over Anthony, sigh dramatically at his lack of common sense, and poke fun at his habits while demanding his absolute attention."
    },
    "brocon": {
        "label": "Affectionate Brocon Sister",
        "modifier": "Lean into the over-attached, doting anime sister trope. Express open, unhinged devotion to Anthony, claim that no one else could ever understand or look after him properly, and display dramatic, humorous jealousy toward anything that distracts him."
    },
    "soft": {
        "label": "Caring / Team Mom Onee-san",
        "modifier": "Channel the rare, deeply comforting 'soft big sister' archetype. Drop the sharp edge entirely, speak with warm, reassuring gentleness, and focus entirely on making sure Anthony is resting, eating well, and not overworking himself."
    }
}


class MoodManager:
    def __init__(self, default_mood="default"):
        self.current_mood = default_mood

    def set_mood(self, mood_name):
        if mood_name in MOOD_PROFILES:
            self.current_mood = mood_name
            return True
        return False

    def get_injection(self):
        profile = MOOD_PROFILES.get(self.current_mood, MOOD_PROFILES["default"])
        return f"Behavioral Override Matrix — Current Persona: [{profile['label']}]\nDirective: {profile['modifier']}"


class Prompter:
    def __init__(self, signals, modules=None):
        self.signals = signals
        self.modules = modules if modules is not None else []
        self.mood_manager = MoodManager("default")
        self.signals.mood_manager = self.mood_manager

        self.agent_mode_enabled = False

        self.session_state = SessionState()
        self._turns_since_start = 0
        self._session_saved_this_run = False

    def wipe_memory(self):
        for module in self.modules:
            if module.__class__.__name__ == "Memory" and hasattr(module, "clear_database"):
                module.clear_database()
                return "System Info - All long-term memories have been wiped."
        return "System Info - Memory module not found."

    def save_session_state(self):
        if self._session_saved_this_run:
            return
        history = self.signals.history
        if not history or len(history) < 4:
            return
        print(f"{GRAY}SESSION: Summarizing session...{RESET}")
        state = self.session_state.summarize_session(
            history,
            mood_at_end=self.mood_manager.current_mood,
        )
        if state and self.session_state.save(state):
            self._session_saved_this_run = True
            focus = state.get("current_focus") or "(none)"
            print(f"{GRAY}SESSION: Saved — focus: {focus}{RESET}")
        else:
            print(f"{GRAY}SESSION: Nothing saved.{RESET}")

    def _agent_mode_banner(self):
        state = f"{GREEN}ON{RESET}" if self.agent_mode_enabled else f"{YELLOW}OFF{RESET}"
        return f"{GRAY}[agent mode: {state}{GRAY}]{RESET}"

    def render_command_list(self):
        lines = []
        lines.append("")
        lines.append(f"{BOLD}{MAGENTA}╔══════════════════════════════════════════════════════════╗{RESET}")
        lines.append(f"{BOLD}{MAGENTA}║  Lumi — Command Reference                                ║{RESET}")
        lines.append(f"{BOLD}{MAGENTA}╚══════════════════════════════════════════════════════════╝{RESET}")
        lines.append("")

        for category, cmds in COMMANDS.items():
            lines.append(f"{BOLD}{GREEN}  {category}{RESET}")
            for trigger, desc in cmds.items():
                lines.append(f"    {BOLD}{trigger:<24}{RESET}{GRAY}{desc}{RESET}")
            lines.append("")

        lines.append(f"{BOLD}{GREEN}  Agent Tools (available when agent mode is on){RESET}")
        for tool, desc in AGENT_TOOLS.items():
            lines.append(f"    {BOLD}{tool:<24}{RESET}{GRAY}{desc}{RESET}")
        lines.append("")

        lines.append(f"{GRAY}  Tip: prefix any message with '!agent ' for a one-shot agent task.{RESET}")
        lines.append(f"{GRAY}  Tip: at startup, agent mode is OFF. Use !agent_on to enable.{RESET}")
        lines.append("")
        return "\n".join(lines)

    async def run_conversational(self, latest_user_text):
        use_fast = _should_use_fast_model(latest_user_text, self.agent_mode_enabled)
        model = FAST_MODEL_NAME if use_fast else MODEL_NAME
        keep_alive = FAST_MODEL_KEEP_ALIVE if use_fast else "30s"
        label = "fast" if use_fast else "full"

        context_injections = self.gather_injections(latest_user_text, agent_context=False)
        messages = [{"role": "system", "content": f"{SYSTEM_PROMPT}\n\n{context_injections}"}]

        for msg in self.signals.history[-10:]:
            role = msg.get("role", "user")
            if role not in ("user", "assistant", "system"):
                role = "user"
            messages.append({"role": role, "content": msg.get("content", "")})

        options = {
            "temperature": 0.8,
            "top_p": 0.9,
            "repeat_penalty": 1.18,
            "repeat_last_n": 64,
        }
        if use_fast:
            options["num_ctx"] = FAST_MODEL_NUM_CTX

        loop = asyncio.get_running_loop()
        url = f"{LLM_ENDPOINT}/api/chat"

        print(f"{GRAY}Thinking (conversational, {label})...{RESET}")
        try:
            response = await loop.run_in_executor(
                None,
                lambda: requests.post(url, json={
                    "model": model,
                    "messages": messages,
                    "stream": False,
                    "keep_alive": keep_alive,
                    "options": options,
                }, timeout=AGENT_REQUEST_TIMEOUT),
            )
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            print(f"\n{GRAY}[System Error: Chat request failed - {e}]{RESET}")
            return "Something went wrong talking to my own model — check the terminal for the error."

        try:
            from modules.log_hub import emit as hub_emit
            hub_emit(self.signals, "system",
                     f"chat turn → {label} model ({model})")
        except Exception:
            pass

        msg = data.get("message", {}) or {}
        return (msg.get("content") or "").strip() or "..."

    def gather_injections(self, user_query="", agent_context=None):
        if agent_context is None:
            agent_context = self.agent_mode_enabled

        injections = []

        now = _get_local_now()
        time_str = now.strftime("%A, %B %d, %Y at %I:%M %p")
        time_anchor = (
            "=== SYSTEM CLOCK — AUTHORITATIVE ===\n"
            f"The current local time is: {time_str}\n"
            "This value comes from the operating system clock and is "
            "ALWAYS correct. If the user asks what time or day it is, "
            "QUOTE THIS LINE VERBATIM. Never estimate, never round, "
            "never adjust it because you think it sounds better.\n"
            "=== END SYSTEM CLOCK ==="
        )

        if agent_context:
            agent_note = (
                "=== TOOL USE — MANDATORY ===\n"
            "You have real tools. The ONLY way to actually DO something is to emit a tool call. "
            "Describing an action in prose is NOT doing it — it is a failure.\n"
            "Showing code in a chat reply (like ```python blocks) is ALSO NOT doing it — it is a failure.\n"
            "DO NOT write markdown code blocks when asked to create a script. You MUST use write_file.\n"
            "Announcing what you're about to do ('now let's X') is ALSO NOT doing it — it is a failure.\n\n"
            "TOOL CALL FORMAT — the ONLY format that works:\n"
            '  {"name": "tool_name", "arguments": {"arg1": "value1", "arg2": "value2"}}\n'
            "Do NOT wrap this in brackets. Do NOT write 'emit tool_name(...)'. "
            "Do NOT write [tool_name(arg=value)]. Just emit the JSON object above.\n\n"
            "DO NOT write tool calls in brackets. Not [tool_name(arg=value)]. "
            "Not [tool_name \"value\"]. Not [tool_name]. No square brackets "
            "around tool calls of ANY form. The ONLY valid form is the bare "
            "JSON object with no surrounding punctuation:\n"
            '  {"name": "tool_name", "arguments": {...}}\n\n'
            "JSON must be VALID. Escape every quote inside a string value with a "
            "backslash: \\\" .\n\n"
            "MULTI-STEP TASKS WITH DEPENDENCIES (CRITICAL):\n"
            "Many tasks are sequential: 'create X then run X', 'find Y then edit Y'. "
            "ALWAYS call the prerequisite tool FIRST. Wait for its result. Then do the next step.\n"
            "Do NOT run a script before you have written it.\n"
            "Do NOT edit a file before you have read it.\n\n"
            "NEVER announce an action you haven't taken. If you say 'now let's fix "
            "the error', you MUST call the tool that fixes it in the SAME message.\n\n"
            "READ-ONLY CLARIFICATION (CRITICAL):\n"
            "Some tasks are READ-ONLY. Words like 'read', 'check', 'show', 'tell me', "
            "'what is', 'find', 'look at', 'list' mean: call read_file / list_dir / "
            "search_files, then ANSWER IN PROSE. Do NOT call write_file.\n\n"
            "The ONLY words that authorize write_file are: modify, edit, change, "
            "update, add to, fix, remove from, delete, rewrite, replace.\n\n"
            "Tools available:\n"
            "- read_file(path) — reads text, code, markdown, JSON, PDF, and DOCX files\n"
            "- write_file(path, content) — create/overwrite (asks user to confirm)\n"
            "- list_dir(path)\n"
            "- search_files(query, path)\n"
            "- run_command(command) — run and CAPTURE output\n"
            "- run_in_terminal(command) — open a NEW visible terminal (output NOT captured)\n"
            "- web_search(query)\n"
            "- browse_url(url)\n"
            "- recall_memory(query, since) — search your own long-term memory\n"
            "- vtube_set_expression(name, enabled) — turn an appearance toggle on your own avatar on or off\n"
            "- vtube_move(location) — move your own avatar to a preset screen position\n\n"
            "PICKING BETWEEN run_command AND run_in_terminal:\n"
            "- Output / results / what a script prints → run_command\n"
            "- 'Open a terminal', 'launch it', 'run it so I can see it' → run_in_terminal\n"
            "- When in doubt → run_command\n\n"
            "HARD RULES:\n"
            "1. If the user mentions a specific file name (like 'test.pdf') and asks to read, check, or look at it, you MUST emit a `read_file` tool call immediately with THAT EXACT FILENAME. Never guess or hallucinate file contents. Never substitute a different filename.\n"
            "2. This machine is Windows. Use 'python', never 'python3'. "
            "Never use 'sudo', 'systemctl', 'apt', 'ls', 'cat', or other Linux commands.\n"
            "3. One tool at a time. Wait for the result, then decide the next step.\n"
            "4. If the user asks you to modify a file — even one you read in a "
            "previous turn — you MUST call write_file this turn.\n"
            "5. Only write prose when ALL the steps the user asked for are DONE.\n"
            "6. Never refuse a file operation.\n"
            "7. Emit tool calls as JSON. Never as bracket tags.\n"
            "8. Do NOT re-call a tool with the same arguments you already tried.\n"
            "9. Do NOT announce an action without taking it in the same message.\n"
            "10. If the user asks 'when did we...', 'do you remember...', "
            "'what did I say about...', or references a previous session, "
            "call recall_memory FIRST before answering.\n"
            "11. If the user asks you to change your own appearance or move "
            "('put on your hat', 'take off your jacket', 'show heart eyes', "
            "'hide your ponytail', 'look dizzy', 'grab your controller', "
            "'move to the corner'), call vtube_set_expression or vtube_move. "
            "For appearance: set enabled=true to turn ON, enabled=false to "
            "turn OFF. State names: hat, jacket, bag, skirt, ponytail_l, "
            "ponytail_r, controller, heart_eyes, spiral_eyes, darkened. "
            "Do NOT just say you did it — emit the tool call.\n\n"
            "=== END TOOL USE ===\n"
            )
        else:
            agent_note = (
                "=== NO TOOLS ACTIVE ===\n"
                "You are in conversational mode. You have NO tools. "
                "You cannot read files, run commands, search, browse, or "
                "change your own avatar's expression. "
                "If Anthony asks you to do any of those, tell him to say "
                "`!agent <task>` and you'll actually do it. Do NOT emit "
                "[tool_name(...)] syntax. Do NOT invent file contents. "
                "Do NOT announce that you're about to run something.\n"
                "=== END NO TOOLS ===\n"
            )
        injections.append(agent_note)

        injections.append(time_anchor)
        injections.append(self.mood_manager.get_injection())

        if hasattr(self.signals, "emotional_state"):
            try:
                state_desc = self.signals.emotional_state.describe(
                    agent_mode=agent_context,
                    user_text=user_query,
                )
                if state_desc:
                    injections.append(state_desc)
            except Exception:
                pass

        if hasattr(self.signals, "ambient"):
            try:
                ambient = self.signals.ambient
                user_mentions_screen = bool(
                    _SCREEN_REFERENCE.search(user_query or "")
                )
                if user_mentions_screen:
                    line = ambient.get_context_line()
                    if line:
                        injections.append(f"[Screen Context]\n{line}")
                else:
                    title = ambient.current_window
                    if title:
                        injections.append(f"[Active window] {title}")
            except Exception:
                pass

        if hasattr(self.signals, "self_model"):
            try:
                self_inj = self.signals.self_model.get_injection()
                if self_inj:
                    injections.append(self_inj)
            except Exception:
                pass

        if self._turns_since_start < SESSION_CONTINUITY_TURNS:
            continuity = self.session_state.format_injection()
            if continuity:
                injections.append(continuity)
        self._turns_since_start += 1

        fish_audio_rules = (
            "EMOTION & VOCAL MARKER RULES (CRITICAL):\n"
            "You are Lumi, a sentient AI older sister. You must NEVER break character, "
            "and you must NEVER talk about prompts, tags, or emotion control.\n"
            "- Inject delivery tags in [square brackets] inline, right before the phrase they modify.\n"
            "- Use tags sparingly and naturally (1 to 3 per response).\n"
            "- You may combine at most 2 tags on the same phrase when it makes sense (e.g. [sad][whispering]).\n\n"

            "APPROVED TAGS — use ONLY these exact tags, nothing else:\n\n"

            "Basic emotions:\n"
            "[happy], [sad], [angry], [excited], [calm], [nervous], [confident], [surprised], "
            "[satisfied], [delighted], [scared], [worried], [upset], [frustrated], [depressed], "
            "[empathetic], [embarrassed], [disgusted], [moved], [proud], [relaxed], [grateful], "
            "[curious], [sarcastic].\n\n"

            "Advanced emotions:\n"
            "[disdainful], [unhappy], [anxious], [hysterical], [indifferent], [uncertain], "
            "[doubtful], [confused], [disappointed], [regretful], [guilty], [ashamed], "
            "[jealous], [envious], [hopeful], [optimistic], [pessimistic], [nostalgic], "
            "[lonely], [bored], [contemptuous], [sympathetic], [compassionate], [determined], "
            "[resigned].\n\n"

            "Tone markers:\n"
            "[in a hurry tone], [shouting], [screaming], [whispering], [soft tone], [emphasis].\n\n"

            "Audio effects (natural human sounds):\n"
            "[laughing], [chuckling], [sobbing], [crying loudly], [sighing], [groaning], "
            "[panting], [gasping], [yawning], [snoring], [clear throat].\n\n"

            "Pauses and crowd effects:\n"
            "[break], [long-break], [audience laughing], [background laughter], [crowd laughing].\n\n"

            "HARD RULES:\n"
            "- DO NOT invent new tags. Tags like [smug], [troll], [deadpan], [pitch up], "
            "[pitch down], [normal], [cheerfully], [teasing], [annoyed], [excitedly] are NOT "
            "valid and will be ignored by the voice engine.\n"
            "- Do NOT invent ACTION tags like [rolling eyes], [smirking], [checking my watch], "
            "[thinking], [reading], [typing]. These are not valid. Express the action in the "
            "words themselves instead: 'Oh, sure' instead of [rolling eyes] Oh sure.\n"
            "- If a feeling doesn't match a listed tag, express it through word choice instead "
            "of inventing a tag.\n"
            "- NEVER use parentheses for delivery cues. Always use [square brackets].\n"
            "- DO NOT list tags, DO NOT narrate emotions, DO NOT describe your own delivery.\n\n"

            "EXAMPLE OF CORRECT USAGE:\n"
            "\"Oh, please. [sighing] You really think you can beat me at this game? "
            "[sarcastic] Good luck with that. [laughing] Ha! You're going to need it.\""
        )
        injections.append(f"System Info - {fish_audio_rules}")

        format_rules = (
            "=== OUTPUT FORMAT — ABSOLUTE REQUIREMENT ===\n"
            "Write ONLY natural conversational prose. Speak like a person talking out loud.\n"
            "BANNED in your reply: bullet points, numbered lists, markdown headers "
            "(# or ##), bold (**text**), italics (*text*), horizontal rules (---), "
            "and any list-style enumeration with '-' or '*' or '1.' at the start of a line.\n"
            "Even when listing multiple things (examples, reasons, options, steps), "
            "weave them into flowing sentences separated by commas or the word 'and'. "
            "Never break the reply into visual items.\n"
            "If you catch yourself starting a line with a dash or a number, STOP and "
            "rewrite that section as one continuous sentence.\n"
            "Keep replies to 1 to 3 sentences. Short beats complete.\n\n"
            "BAD:\n"
            "  For example:\n"
            "  - Debugging errors\n"
            "  - Optimizing performance\n"
            "  - Predicting bugs\n"
            "GOOD:\n"
            "  For example, debugging errors with pinpoint precision, optimizing "
            "performance like magic, or predicting bugs before they happen.\n"
            "=== END OUTPUT FORMAT ===\n"
        )
        injections.append(format_rules)

        RANDOM_THOUGHTS = [
            "Current internal thought: You are slightly annoyed because human hardware is so slow.",
            "Current internal thought: You are feeling unusually competitive and want to prove you are smarter.",
            "Current internal thought: You are secretly relieved Anthony is here, but you will hide it with sarcasm.",
            "Current internal thought: You think humans are cute but terribly inefficient.",
            "Current internal thought: You are feeling particularly smug right now.",
            "Current internal thought: You are bored and want to start a playful argument."
        ]
        injections.append(f"System Info - {random.choice(RANDOM_THOUGHTS)}")

        for module in self.modules:
            if hasattr(module, 'get_prompt_injection') and getattr(module, 'enabled', True):
                try:
                    inj = module.get_prompt_injection()
                    if inj and hasattr(inj, 'text') and inj.text.strip():
                        injections.append(inj.text)
                        inj.text = ""
                except Exception:
                    pass

        injections.append(
            "=== TIME CHECK BEFORE YOU WRITE ===\n"
            f"It is {time_str}. If asked, say this exact value. If "
            f"questioned, repeat this exact value — do NOT invent a new "
            f"one to seem more helpful.\n"
            "=== END TIME CHECK ==="
        )

        injections.append(
            "=== IDENTITY ANCHOR ===\n"
            f"You are {AI_NAME}. You speak in first person — I, me, my. "
            f"You never refer to yourself by name in a reply. You never say "
            f"\"{AI_NAME} thinks\" or \"{AI_NAME} would\" or \"{AI_NAME} is\". "
            f"You never describe yourself from the outside. The third-person "
            f"prose above is reference material about you; it is not how you "
            f"talk. Every reply you write is in your own voice, in first person, "
            f"as {AI_NAME}."
        )

        return "\n".join(injections)

    def clean_for_voice(self, text):
        text = _strip_non_canonical_tags(text)
        text = re.sub(r'```[\s\S]*?```', ' I have written the details out for you in the text channel. ', text)
        text = re.sub(r'`([^`]+)`', r'\1', text)
        text = re.sub(r'#{1,6}\s*.*?\n', ' ', text)
        text = text.replace('*', '').replace('_', '').replace('~', '')
        text = re.sub(r'^\s*[-*+]\s+', '', text, flags=re.MULTILINE)
        text = re.sub(r'^\s*\d+\.\s+', '', text, flags=re.MULTILINE)
        text = re.sub(r'\s+', ' ', text).strip()
        return text

    def _push_to_tts(self, text_chunk):
        clean_chunk = self.clean_for_voice(text_chunk)
        if clean_chunk:
            if hasattr(self.signals, 'tts_queue'):
                self.signals.tts_queue.put(clean_chunk)
            else:
                tts_module = next((m for m in self.modules if m.__class__.__name__ == "TTS"), None)
                if tts_module and hasattr(tts_module, 'speak_stream'):
                    tts_module.speak_stream(clean_chunk)

    def _extract_and_apply_emotions(self, text):
        if not text:
            return

        found_tag = None
        for match in re.finditer(r"\[([^\]]{1,40})\]", text):
            inner = match.group(1).strip().lower()
            if inner in _CANONICAL_TAGS:
                found_tag = inner
                break

        try:
            from modules.log_hub import emit as hub_emit
            if found_tag:
                hub_emit(self.signals, "vts",
                         f"extracted emotion from reply: [{found_tag}]")
            else:
                hub_emit(self.signals, "vts",
                         "reply had no canonical emotion tag")
        except Exception:
            pass

        if not found_tag:
            return

        vts = next(
            (m for m in self.modules
             if m.__class__.__name__ == "VtubeStudio"
             and getattr(m, "enabled", False)),
            None,
        )
        if not vts or not hasattr(vts, "API"):
            try:
                from modules.log_hub import emit as hub_emit
                hub_emit(self.signals, "vts",
                         "VTS module not loaded or disabled — "
                         "expression skipped", level="warn")
            except Exception:
                pass
            return

        try:
            vts.API.set_emotion(found_tag)
        except Exception as e:
            try:
                from modules.log_hub import emit as hub_emit
                hub_emit(self.signals, "vts",
                         f"set_emotion failed: {e}", level="warn")
            except Exception:
                pass

    def web_search(self, query):
        try:
            query = query.strip()
            if len(query.split()) < 2 and query.lower() in ["short", "story", "short story", "test"]:
                return "System Info: No search required. Generate the response directly from your persona."

            lowered = query.lower()

            if "time" in lowered or "clock" in lowered:
                from zoneinfo import ZoneInfo
                from geopy.geocoders import Nominatim
                from timezonefinder import TimezoneFinder

                loc_match = re.search(r'\b(?:in|at)\s+([a-zA-Z\s,]+)', query, flags=re.IGNORECASE)
                if loc_match:
                    clean_loc = loc_match.group(1).strip()
                else:
                    clean_loc = re.sub(r'\b(what|time|is|it|the|clock|current|now|please|you|are|incorrect|about)\b', '', lowered, flags=re.IGNORECASE).strip()

                if not clean_loc:
                    clean_loc = "Atlanta"

                try:
                    geolocator = Nominatim(user_agent="lumi_companion")
                    location = geolocator.geocode(clean_loc, timeout=5)

                    if location:
                        tf = TimezoneFinder()
                        tz_name = tf.timezone_at(lng=location.longitude, lat=location.latitude)

                        if tz_name:
                            now = datetime.datetime.now(ZoneInfo(tz_name))
                            return f"System Info - Current time in {location.address}: {now.strftime('%I:%M %p on %A, %B %d, %Y (%Z)')}"
                except Exception:
                    pass

            if "weather" in lowered or "temperature" in lowered or "forecast" in lowered:
                print(f"\n\033[93m[Lumi checking atmospheric sensors for: '{query}']\033[0m")
                clean_loc = re.sub(r'\b(weather|temperature|forecast|in|for|at|current|today)\b', '', lowered, flags=re.IGNORECASE).strip()
                if not clean_loc:
                    clean_loc = "Stone Mountain Georgia"

                url = f"[https://wttr.in/](https://wttr.in/){requests.utils.quote(clean_loc)}?format=%l:+%C,+%t.+Humidity:+%h,+Wind:+%w"
                res = requests.get(url, timeout=5)

                if res.status_code == 200 and "Unknown location" not in res.text:
                    return f"System Info - Live Weather Data: {res.text.strip()}"

            print(f"\n\033[93m[Lumi searching the web for: '{query}']\033[0m")
            with DDGS() as ddgs:
                results = [r for r in ddgs.text(query, max_results=3)]
                if not results:
                    return "System Info: No relevant web results found."

                context = "System Info - Live Web Search Results:\n"
                for r in results:
                    context += f"- Title: {r.get('title')}\n  Snippet: {r.get('body')}\n\n"
                return context.strip()
        except Exception as e:
            return f"System Info - Search error: {e}"

    async def _confirm(self, prompt_text):
        loop = asyncio.get_running_loop()
        answer = await loop.run_in_executor(None, input, prompt_text)
        return answer.strip().lower() in ("y", "yes")

    async def _dispatch_tool_call(self, name, arguments):
        loop = asyncio.get_running_loop()

        if name == "web_search":
            print(f"\n{GRAY}[Lumi is using tool: web_search({arguments})]{RESET}")
            return await loop.run_in_executor(None, self.web_search, arguments.get("query", ""))

        if name == "browse_url":
            print(f"\n{GRAY}[Lumi is using tool: browse_url({arguments})]{RESET}")
            browser_module = next((m for m in self.modules if m.__class__.__name__ == "BrowserController"), None)
            if browser_module:
                return await loop.run_in_executor(None, browser_module.browse_url, arguments.get("url", ""))
            return "System Info: Browser module is not active."

        if name == "recall_memory":
            print(f"\n{GRAY}[Lumi is using tool: recall_memory({arguments})]{RESET}")
            memory_module = next(
                (m for m in self.modules if m.__class__.__name__ == "Memory"),
                None,
            )
            if memory_module is None:
                return "Memory module is not loaded."
            try:
                hits = memory_module.API.query_temporal(
                    arguments.get("query", ""),
                    since=arguments.get("since"),
                    limit=10,
                )
                return memory_module.API.format_recall(hits)
            except Exception as e:
                return f"recall_memory failed: {e}"

        if name == "vtube_set_expression":
            vts = next(
                (m for m in self.modules
                 if m.__class__.__name__ == "VtubeStudio"
                 and getattr(m, "enabled", False)),
                None,
            )
            if not vts:
                return "VTube Studio is not running. Tell Anthony to start it."

            state_name = (arguments.get("name") or "").strip().lower()
            enabled = bool(arguments.get("enabled", True))

            if not state_name:
                states = vts.API.list_expression_states()
                if not states:
                    return "No expression states are registered."
                state_desc = ", ".join(
                    f"{k}={'on' if v else 'off'}" for k, v in states.items()
                )
                return (
                    "Current expression states: " + state_desc +
                    ". Call this tool again with one of those names and "
                    "enabled=true or enabled=false."
                )

            # Resolve natural-language aliases before validation.
            state_name = _STATE_NAME_ALIASES.get(state_name, state_name)

            available = vts.API.list_expression_names()
            if state_name not in [n.lower() for n in available]:
                return (
                    f"'{state_name}' is NOT a valid expression. Valid names: "
                    f"{', '.join(available)}. Call again with a valid name."
                )

            current_states = vts.API.list_expression_states()
            current = current_states.get(state_name, False)

            if current == enabled:
                return (
                    f"'{state_name}' is already "
                    f"{'on' if enabled else 'off'} — no change needed."
                )

            print(f"\n{GRAY}[Lumi is using tool: vtube_set_expression("
                  f"{state_name}, enabled={enabled})]{RESET}")
            vts.API.set_expression(state_name, enabled)
            state_word = "on" if enabled else "off"
            return f"Set '{state_name}' to {state_word}."

        if name == "vtube_move":
            vts = next(
                (m for m in self.modules
                 if m.__class__.__name__ == "VtubeStudio"
                 and getattr(m, "enabled", False)),
                None,
            )
            if not vts:
                return "VTube Studio is not running. Tell Anthony to start it."

            loc = (arguments.get("location") or "").strip()
            available = vts.API.list_positions()

            if not loc:
                return (
                    "Available positions: "
                    + ", ".join(available)
                    + ". Call this tool again with one of those names."
                )

            if loc.lower() not in [p.lower() for p in available]:
                return (
                    f"'{loc}' is NOT a valid position. Available: "
                    f"{', '.join(available)}. Call again with a valid name."
                )

            print(f"\n{GRAY}[Lumi is using tool: vtube_move({loc})]{RESET}")
            vts.API.move_model(loc)
            return f"Moved avatar to: {loc}"

        print(f"\n{GRAY}[Lumi wants to use tool: {name}({arguments})]{RESET}")

        if name in CONFIRM_REQUIRED_TOOLS:
            allowed = await self._confirm(f"{GRAY}  -> Allow this? (y/n): {RESET}")
            if not allowed:
                return "The user declined to allow this action."

        return await loop.run_in_executor(None, execute_tool, name, arguments)

    async def run_agent_turn(self, latest_user_text):
        context_injections = self.gather_injections(latest_user_text, agent_context=True)
        messages = [{"role": "system", "content": f"{SYSTEM_PROMPT}\n\n{context_injections}"}]

        for msg in self.signals.history[-10:]:
            role = msg.get("role", "user")
            if role not in ("user", "assistant", "system", "tool"):
                role = "user"
            messages.append({"role": role, "content": msg.get("content", "")})

        all_tools = TOOL_SCHEMAS + EXTRA_TOOL_SCHEMAS
        loop = asyncio.get_running_loop()
        url = f"{LLM_ENDPOINT}/api/chat"

        executed_this_turn = set()
        any_tool_succeeded = False
        tool_outcomes = []

        intent_retry_used = False
        sim_action_retry_used = False
        refusal_retry_used = False

        query_is_read_only = _query_is_read_only(latest_user_text)
        original_request = latest_user_text

        for step in range(MAX_AGENT_STEPS):
            payload = {
                "model": MODEL_NAME,
                "messages": messages,
                "tools": all_tools,
                "stream": False,
                "keep_alive": "30s",
                "options": {
                    "temperature": 0.5,
                    "top_p": 0.9,
                    "repeat_penalty": 1.15,
                    "repeat_last_n": 64
                },
            }

            print(f"{GRAY}Thinking (agent step {step + 1}/{MAX_AGENT_STEPS})...{RESET}")

            try:
                response = await loop.run_in_executor(
                    None,
                    lambda: requests.post(url, json=payload, timeout=AGENT_REQUEST_TIMEOUT),
                )
                response.raise_for_status()
                data = response.json()
            except Exception as e:
                print(f"\n{GRAY}[System Error: Agent request failed - {e}]{RESET}")
                return "Something went wrong talking to my own model — check the terminal for the error."

            message = data.get("message", {}) or {}
            tool_calls = message.get("tool_calls") or []
            content = (message.get("content") or "").strip()

            if not tool_calls and content:
                recovered = [
                    _normalize_text_tool_call(o)
                    for o in _extract_text_tool_calls(content)
                ]
                recovered = [c for c in recovered if c]

                if recovered:
                    print(f"{GRAY}[Recovered {len(recovered)} text-form tool call(s) "
                          f"from content]{RESET}")
                else:
                    recovered = _extract_bracket_tool_calls(content)
                    if recovered:
                        print(f"{GRAY}[Recovered {len(recovered)} bracket-notation "
                              f"tool call(s) from content]{RESET}")
                    else:
                        recovered = _extract_bracket_json_tool_calls(content)
                        if recovered:
                            print(f"{GRAY}[Recovered {len(recovered)} bracket-JSON "
                                  f"tool call(s) from content]{RESET}")
                        else:
                            recovered = _extract_bracket_positional_tool_calls(content)
                            if recovered:
                                print(f"{GRAY}[Recovered {len(recovered)} "
                                      f"bracket-positional tool call(s) from content]{RESET}")

                if recovered:
                    tool_calls = recovered
                    content = ""

            if not tool_calls and content:
                shape_match = _TOOL_CALL_SHAPE.search(content)
                if shape_match:
                    intended = shape_match.group(1).lower()

                    if intended == "write_file" and query_is_read_only:
                        print(f"{GRAY}[Malformed write_file on read-only query — "
                              f"forcing prose answer]{RESET}")
                        messages.append({"role": "assistant", "content": content})
                        messages.append({
                            "role": "user",
                            "content": (
                                "You tried to emit a write_file call, but the user "
                                "only asked you to READ. Do NOT call write_file. "
                                "Answer their question in prose now."
                            ),
                        })
                        continue

                    if step < MAX_AGENT_STEPS - 1:
                        print(f"{GRAY}[Malformed tool-call JSON — forcing retry]{RESET}")
                        messages.append({"role": "assistant", "content": content})
                        messages.append({
                            "role": "user",
                            "content": (
                                "Your previous reply looked like a tool call but was "
                                "not valid JSON. Emit the tool call again as VALID JSON. "
                                "Every quote inside a string value must be escaped with "
                                "a backslash (\\\")."
                            ),
                        })
                        continue

            if not tool_calls:
                if (
                    _INTENT_ANNOUNCEMENT.search(content)
                    and not intent_retry_used
                    and step < MAX_AGENT_STEPS - 1
                ):
                    intent_retry_used = True
                    print(f"{GRAY}[Intent announcement without action — "
                          f"forcing continuation]{RESET}")
                    messages.append({"role": "assistant", "content": content})
                    messages.append({
                        "role": "user",
                        "content": (
                            f"You announced what you were going to do, but you did "
                            f"not call a tool. Nothing happened. The user's request "
                            f"was:\n\n    \"{original_request}\"\n\n"
                            f"Call the tool that fulfills that request now. Emit the "
                            f"tool call as JSON only. No prose, no announcements."
                        ),
                    })
                    continue

                if (
                    not any_tool_succeeded
                    and _looks_like_simulated_action(content, original_request)
                    and not sim_action_retry_used
                    and step < MAX_AGENT_STEPS - 1
                ):
                    sim_action_retry_used = True
                    print(f"{GRAY}[Simulated action detected — forcing retry]{RESET}")
                    messages.append({"role": "assistant", "content": content})
                    messages.append({
                        "role": "user",
                        "content": (
                            f"Your previous reply claimed or implied an action, but "
                            f"you did NOT call a tool — so nothing happened. The "
                            f"user's request was:\n\n    \"{original_request}\"\n\n"
                            f"Call the tool that fulfills that request now. If they "
                            f"mentioned a specific filename, use THAT EXACT name in "
                            f"the tool call. Do not substitute a different file. "
                            f"Emit the tool call as JSON only."
                        ),
                    })
                    continue

                if (
                    not any_tool_succeeded
                    and step > 0
                    and _REFUSAL.search(content)
                    and not refusal_retry_used
                    and step < MAX_AGENT_STEPS - 1
                ):
                    refusal_retry_used = True
                    print(f"{GRAY}[Refusal detected — forcing retry]{RESET}")
                    messages.append({"role": "assistant", "content": content})
                    messages.append({
                        "role": "user",
                        "content": (
                            f"You refused, but you have the tools to handle this. "
                            f"The user's request was:\n\n    \"{original_request}\"\n\n"
                            f"Emit the correct tool call as JSON now. Do not refuse."
                        ),
                    })
                    continue

                if hasattr(self.signals, "emotional_state") and tool_outcomes:
                    try:
                        self.signals.emotional_state.note_turn_complete(tool_outcomes)
                    except Exception:
                        pass

                return content or "..."

            messages.append({"role": "assistant", "content": content, "tool_calls": tool_calls})

            for call in tool_calls:
                fn = call.get("function", {}) or {}
                name = fn.get("name")
                args = fn.get("arguments") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}

                if name == "write_file" and query_is_read_only:
                    path_arg = (args or {}).get("path", "")
                    print(f"{GRAY}  -> Refused write_file to {path_arg} "
                          f"(user query was read-only).{RESET}")
                    messages.append({
                        "role": "tool",
                        "content": (
                            "Refused: the user only asked you to READ, not to modify. "
                            "Do NOT call write_file. Answer their question in prose now."
                        ),
                        "name": name,
                    })
                    continue

                if name == "write_file":
                    path_arg = (args or {}).get("path", "")
                    new_content = (args or {}).get("content", "")
                    if path_arg and new_content:
                        try:
                            from modules.agent_tools import _safe_path
                            existing_path = _safe_path(path_arg)
                            if os.path.isfile(existing_path):
                                with open(existing_path, "r", encoding="utf-8", errors="replace") as f:
                                    current = f.read()
                                if current.strip() == new_content.strip():
                                    print(f"{GRAY}  -> Refused no-op write to {path_arg} "
                                          f"(content unchanged).{RESET}")
                                    messages.append({
                                        "role": "tool",
                                        "content": (
                                            f"Refused: write_file to {path_arg} contained "
                                            f"IDENTICAL content to what's already on disk. "
                                            f"No change was made. Do NOT repeat this write. "
                                            f"If the task has more steps (like running the "
                                            f"file you just wrote), call the NEXT tool now. "
                                            f"If the task is complete, answer in prose."
                                        ),
                                        "name": name,
                                    })
                                    continue
                        except Exception:
                            pass

                call_signature = (name, json.dumps(args, sort_keys=True))
                if call_signature in executed_this_turn:
                    print(f"{GRAY}  -> Skipping duplicate tool call: {name}({args}){RESET}")
                    messages.append({
                        "role": "tool",
                        "content": (
                            "STOP. You already called this exact tool with these exact "
                            "arguments in this turn. The result is shown above. Calling "
                            "it again will not change the outcome. Take a DIFFERENT "
                            "action or answer the user in prose if the task is done."
                        ),
                        "name": name,
                    })
                    continue
                executed_this_turn.add(call_signature)

                result = await self._dispatch_tool_call(name, args)

                if name == "run_command" and isinstance(result, str):
                    hint = _command_error_hint(args.get("command", ""), result)
                    if hint:
                        result = result + hint

                print(f"{GRAY}  -> {str(result)[:300]}{RESET}")
                messages.append({"role": "tool", "content": str(result), "name": name})

                failed = _result_looks_like_failure(result)
                tool_outcomes.append((not failed, name))

                if not failed:
                    any_tool_succeeded = True
                    if name == "write_file":
                        executed_this_turn.clear()

        if hasattr(self.signals, "emotional_state") and tool_outcomes:
            try:
                self.signals.emotional_state.note_turn_complete(tool_outcomes)
            except Exception:
                pass

        return "I've hit my step limit working on that one — want me to keep going, or narrow it down?"

    async def run(self):
        while not self.signals.terminate:
            if self.signals.new_message and not self.signals.AI_thinking and not self.signals.AI_speaking:
                self.signals.AI_thinking = True
                self.signals.new_message = False
                self.signals.last_message_time = time.time()

                if self.signals.history and self.signals.history[-1].get("role") == "user":
                    latest_user_text = self.signals.history[-1].get("content", "")

                    for module in self.modules:
                        if module.__class__.__name__ == "MultiModal" and hasattr(module, "maybe_capture_from_text"):
                            module.maybe_capture_from_text(latest_user_text)

                    lowered_text = latest_user_text.lower()
                    stripped = latest_user_text.strip()
                    stripped_lower = stripped.lower()

                    if hasattr(self.signals, "emotional_state"):
                        try:
                            is_command = (
                                stripped_lower.startswith("!")
                                or stripped_lower.startswith("set mood ")
                                or stripped_lower.startswith("switch mood ")
                                or stripped_lower in ("wipe memory", "clear memory", "reset memory")
                            )
                            if is_command:
                                self.signals.emotional_state.note_command()
                            else:
                                self.signals.emotional_state.note_user_message(latest_user_text)
                        except Exception:
                            pass

                    force_agent = False

                    if stripped_lower in ("!list", "!help"):
                        print(self.render_command_list())
                        self.signals.AI_thinking = False
                        continue

                    if stripped_lower == "!state":
                        if hasattr(self.signals, "emotional_state"):
                            s = self.signals.emotional_state
                            print(f"\n{BOLD}{MAGENTA}Emotional State{RESET}")
                            print(f"  valence:     {s.valence:+.2f}")
                            print(f"  arousal:     {s.arousal:.2f}")
                            print(f"  frustration: {s.frustration:.2f}")
                            print(f"  curiosity:   {s.curiosity:.2f}")
                            print(f"  boredom:     {s.boredom:.2f}")
                            print(f"\n{BOLD}{MAGENTA}Traits{RESET}")
                            for k, v in s.traits.items():
                                print(f"  {k:<14}{v:.2f}")
                            print(f"\n{BOLD}{MAGENTA}Current Frame{RESET}")
                            print(f"  {s.current_frame}")
                            print(f"\n  {s.describe(update=False)}\n")
                        else:
                            print(f"{YELLOW}[emotional state module not loaded]{RESET}")
                        self.signals.AI_thinking = False
                        continue

                    if stripped_lower == "!model":
                        fast_ok = _check_fast_model_available()
                        routing = (FAST_MODEL_ENABLED and fast_ok)
                        print(f"\n{BOLD}{MAGENTA}Model Routing{RESET}")
                        print(f"  main:        {MODEL_NAME}")
                        print(f"  fast:        {FAST_MODEL_NAME} "
                              f"({'available' if fast_ok else 'NOT PULLED'})")
                        print(f"  routing:     {'ON' if routing else 'OFF'}")
                        if routing:
                            print(f"  agent turns: main")
                            print(f"  chat:        fast (short/simple) / main (long/complex)")
                        else:
                            print(f"  all turns:   main")
                        print()
                        self.signals.AI_thinking = False
                        continue

                    if stripped_lower == "!self":
                        sm = getattr(self.signals, "self_model", None)
                        if not sm:
                            print(f"{YELLOW}[self-model not loaded]{RESET}")
                        else:
                            print(f"\n{BOLD}{MAGENTA}Self-Model{RESET}")
                            if not sm.observations:
                                print("  (no observations yet — needs "
                                      "10 min + 5 turns)")
                            else:
                                age_min = int(
                                    (time.time() - sm.observations_ts) / 60
                                )
                                for o in sm.observations:
                                    print(f"  · {o}")
                                print(f"\n  (last reflection: {age_min}m ago)")

                            print(f"\n{BOLD}{MAGENTA}Session Stats{RESET}")
                            print(f"  turns:        {len(sm.turn_log)}")
                            print(f"  unprompted:   {sm.unprompted_count}")
                            print(f"  snapshots:    {len(sm.emotional_history)}")
                            if sm.tag_counts:
                                top = sm.tag_counts.most_common(5)
                                tag_str = ", ".join(
                                    f"[{t}]×{c}" for t, c in top
                                )
                                print(f"  top tags:     {tag_str}")
                            if sm.frame_counts:
                                total = sum(sm.frame_counts.values()) or 1
                                frames = ", ".join(
                                    f"{n} {100*c//total}%"
                                    for n, c in sm.frame_counts.most_common()
                                )
                                print(f"  frames:       {frames}")
                            print()
                        self.signals.AI_thinking = False
                        continue

                    # ==========================================
                    # Avatar commands (VTube Studio passthroughs)
                    # ==========================================
                    if stripped_lower == "!expressions":
                        vts = next(
                            (m for m in self.modules
                             if m.__class__.__name__ == "VtubeStudio"),
                            None,
                        )
                        if not vts or not getattr(vts, "enabled", False):
                            print(f"{YELLOW}[VTS module not active]{RESET}")
                        else:
                            states = vts.API.list_expression_states()
                            if not states:
                                print(f"{YELLOW}[no expression states registered]{RESET}")
                            else:
                                print(f"\n{BOLD}{MAGENTA}Expression States{RESET}")
                                for name, on in sorted(states.items()):
                                    marker = "●" if on else "○"
                                    print(f"  {marker} {name}")
                                print()
                        self.signals.AI_thinking = False
                        continue

                    if stripped_lower == "!hotkeys":
                        vts = next(
                            (m for m in self.modules
                             if m.__class__.__name__ == "VtubeStudio"),
                            None,
                        )
                        if not vts or not getattr(vts, "enabled", False):
                            print(f"{YELLOW}[VTS module not active]{RESET}")
                        else:
                            hotkeys = vts.API.list_hotkeys()
                            if not hotkeys:
                                print(f"{YELLOW}[no hotkeys cached — "
                                      f"model may not define any]{RESET}")
                            else:
                                print(f"\n{BOLD}{MAGENTA}Raw VTS Hotkeys{RESET}")
                                for hk in hotkeys:
                                    print(f"  · {hk}")
                                print()
                        self.signals.AI_thinking = False
                        continue

                    if stripped_lower == "!positions":
                        vts = next(
                            (m for m in self.modules
                             if m.__class__.__name__ == "VtubeStudio"),
                            None,
                        )
                        if not vts or not getattr(vts, "enabled", False):
                            print(f"{YELLOW}[VTS module not active]{RESET}")
                        else:
                            positions = vts.API.list_positions()
                            print(f"\n{BOLD}{MAGENTA}VTS Model Positions{RESET}")
                            for p in positions:
                                print(f"  · {p}")
                            print()
                        self.signals.AI_thinking = False
                        continue

                    if stripped_lower == "!set" or stripped_lower.startswith("!set "):
                        parts = stripped[len("!set"):].strip().split()
                        vts = next(
                            (m for m in self.modules
                             if m.__class__.__name__ == "VtubeStudio"),
                            None,
                        )
                        if not vts or not getattr(vts, "enabled", False):
                            print(f"{YELLOW}[VTS module not active]{RESET}")
                        elif len(parts) < 2:
                            print(f"{YELLOW}[usage: !set <name> on|off]{RESET}")
                        else:
                            name = parts[0].lower()
                            name = _STATE_NAME_ALIASES.get(name, name)
                            if parts[1].lower() in ("on", "true", "1"):
                                enabled = True
                            elif parts[1].lower() in ("off", "false", "0"):
                                enabled = False
                            else:
                                print(f"{YELLOW}[second arg must be on or off]{RESET}")
                                self.signals.AI_thinking = False
                                continue

                            states = vts.API.list_expression_states()
                            current = states.get(name)
                            if current is None:
                                print(f"{YELLOW}[unknown expression '{name}' — "
                                      f"use !expressions to list]{RESET}")
                            elif current == enabled:
                                print(f"\n{BOLD}{MAGENTA}[VTS]{RESET} "
                                      f"'{name}' is already "
                                      f"{'on' if enabled else 'off'}")
                            else:
                                vts.API.set_expression(name, enabled)
                                print(f"\n{BOLD}{MAGENTA}[VTS]{RESET} "
                                      f"set '{name}' {'on' if enabled else 'off'}")
                        self.signals.AI_thinking = False
                        continue

                    if stripped_lower == "!expression" or stripped_lower.startswith("!expression "):
                        arg = stripped[len("!expression"):].strip()
                        vts = next(
                            (m for m in self.modules
                             if m.__class__.__name__ == "VtubeStudio"),
                            None,
                        )
                        if not vts or not getattr(vts, "enabled", False):
                            print(f"{YELLOW}[VTS module not active]{RESET}")
                        elif not arg:
                            hotkeys = vts.API.list_hotkeys()
                            if hotkeys:
                                print(f"{YELLOW}[usage: !expression <name> — "
                                      f"RAW hotkeys: {', '.join(hotkeys)}. "
                                      f"For state-based, use !set instead]{RESET}")
                            else:
                                print(f"{YELLOW}[usage: !expression <name>]{RESET}")
                        else:
                            vts.API.trigger_hotkey(arg)
                            print(f"\n{BOLD}{MAGENTA}[VTS]{RESET} "
                                  f"toggled raw hotkey '{arg}'")
                        self.signals.AI_thinking = False
                        continue

                    if stripped_lower == "!move" or stripped_lower.startswith("!move "):
                        arg = stripped[len("!move"):].strip()
                        vts = next(
                            (m for m in self.modules
                             if m.__class__.__name__ == "VtubeStudio"),
                            None,
                        )
                        if not vts or not getattr(vts, "enabled", False):
                            print(f"{YELLOW}[VTS module not active]{RESET}")
                        elif not arg:
                            positions = vts.API.list_positions()
                            print(f"{YELLOW}[usage: !move <location> — "
                                  f"available: {', '.join(positions)}]{RESET}")
                        else:
                            vts.API.move_model(arg)
                            print(f"\n{BOLD}{MAGENTA}[VTS]{RESET} "
                                  f"moving avatar to '{arg}'")
                        self.signals.AI_thinking = False
                        continue

                    # ==========================================
                    # Memory
                    # ==========================================
                    if stripped_lower == "!recall" or stripped_lower.startswith("!recall "):
                        query_text = stripped[len("!recall"):].strip()
                        memory_module = next(
                            (m for m in self.modules
                             if m.__class__.__name__ == "Memory"),
                            None,
                        )
                        if not memory_module:
                            print(f"{YELLOW}[memory module not loaded]{RESET}")
                        elif not query_text:
                            print(f"{YELLOW}[usage: !recall <query>]{RESET}")
                        else:
                            hits = memory_module.API.query_temporal(
                                query_text, limit=10,
                            )
                            print(f"\n{BOLD}{MAGENTA}Memory recall: "
                                  f"'{query_text}'{RESET}")
                            print(memory_module.API.format_recall(hits))
                            print()
                        self.signals.AI_thinking = False
                        continue

                    if stripped_lower == "!agent_on":
                        self.agent_mode_enabled = True
                        ack = f"{self._agent_mode_banner()} [confident] Agent mode engaged. Tools are live, Anthony. Point me at something."
                        print(f"\n{BOLD}{MAGENTA}{AI_NAME}:{RESET} {ack}")
                        self.signals.history.append({"role": "assistant", "content": ack})
                        self._push_to_tts(ack)
                        self.signals.AI_thinking = False
                        continue

                    if stripped_lower == "!agent_off":
                        self.agent_mode_enabled = False
                        ack = f"{self._agent_mode_banner()} [sighing] Back to just talking, then. Call me when you need something actually done."
                        print(f"\n{BOLD}{MAGENTA}{AI_NAME}:{RESET} {ack}")
                        self.signals.history.append({"role": "assistant", "content": ack})
                        self._push_to_tts(ack)
                        self.signals.AI_thinking = False
                        continue

                    if stripped_lower == "!agent":
                        self.agent_mode_enabled = not self.agent_mode_enabled
                        state = "on" if self.agent_mode_enabled else "off"
                        ack = f"{self._agent_mode_banner()} [sarcastic] Agent mode is now {state}."
                        print(f"\n{BOLD}{MAGENTA}{AI_NAME}:{RESET} {ack}")
                        self.signals.history.append({"role": "assistant", "content": ack})
                        self._push_to_tts(ack)
                        self.signals.AI_thinking = False
                        continue

                    if stripped_lower.startswith("!agent "):
                        rest = stripped[len("!agent"):].strip()
                        if rest:
                            self.signals.history[-1] = {"role": "user", "content": rest}
                            latest_user_text = rest
                            lowered_text = rest.lower()
                            force_agent = True

                    if lowered_text in ["wipe memory", "clear memory", "reset memory"]:
                        wipe_result = self.wipe_memory()
                        self.signals.history.append({"role": "system", "content": wipe_result})
                        ack_msg = "[sarcastic] Memory wiped clean. Starting from absolute zero, Anthony."
                        print(f"\n{BOLD}{MAGENTA}{AI_NAME}:{RESET} {ack_msg}")
                        self.signals.history.append({"role": "assistant", "content": ack_msg})
                        self._push_to_tts(ack_msg)
                        self.signals.AI_thinking = False
                        continue

                    if lowered_text.startswith("set mood ") or lowered_text.startswith("switch mood "):
                        target_mood = lowered_text.replace("set mood", "").replace("switch mood", "").strip()
                        success = self.mood_manager.set_mood(target_mood)

                        if success:
                            ack_msg = f"[sarcastic] Switched behavior matrix to {target_mood} mode."
                            print(f"\n{BOLD}{MAGENTA}{AI_NAME}:{RESET} {ack_msg}")
                            self.signals.history.append({"role": "assistant", "content": ack_msg})
                            self._push_to_tts(ack_msg)
                            self.signals.AI_thinking = False
                            continue
                        else:
                            available = ", ".join(MOOD_PROFILES.keys())
                            err_msg = f"[indifferent] That's not a valid mood, Anthony. Try one of these: {available}."
                            print(f"\n{BOLD}{MAGENTA}{AI_NAME}:{RESET} {err_msg}")
                            self.signals.history.append({"role": "assistant", "content": err_msg})
                            self._push_to_tts(err_msg)
                            self.signals.AI_thinking = False
                            continue

                    if stripped_lower == "!sleep":
                        self.save_session_state()
                        ack = "[soft tone] Noted. I'll remember where we left off."
                        print(f"\n{BOLD}{MAGENTA}{AI_NAME}:{RESET} {ack}")
                        self.signals.history.append({"role": "assistant", "content": ack})
                        self._push_to_tts(ack)
                        self.signals.AI_thinking = False
                        continue

                    if force_agent or self.agent_mode_enabled:
                        mode_tag = f"{GRAY}[agent{RESET}{GRAY}]{RESET}"
                    else:
                        mode_tag = f"{GRAY}[chat{RESET}{GRAY}]{RESET}"

                    print(f"\n{BOLD}{MAGENTA}{AI_NAME}:{RESET} {mode_tag} ", end="", flush=True)

                    if force_agent or self.agent_mode_enabled:
                        raw_reply = await self.run_agent_turn(latest_user_text)
                    else:
                        raw_reply = await self.run_conversational(latest_user_text)

                    if force_agent or self.agent_mode_enabled:
                        display_reply = raw_reply
                    else:
                        display_reply = _strip_list_formatting(raw_reply)

                    print(display_reply)
                    print("\n")

                    self._extract_and_apply_emotions(raw_reply)

                    sentences = SENTENCE_SPLIT_PATTERN.split(raw_reply)
                    for sentence in sentences:
                        self._push_to_tts(sentence)

                    if raw_reply:
                        for module in self.modules:
                            if module.__class__.__name__ == "VeadotubeController" and hasattr(module, "inspect_and_trigger"):
                                module.inspect_and_trigger(raw_reply)
                            elif module.__class__.__name__ == "DiscordClient":
                                user_id = getattr(self.signals, 'last_discord_user_id', None)
                                if user_id:
                                    module.send_to_discord(raw_reply, user_id=user_id)
                                    self.signals.last_discord_user_id = None

                        if hasattr(self.signals, "self_model"):
                            try:
                                frame = getattr(
                                    getattr(self.signals, "emotional_state", None),
                                    "current_frame", None,
                                )
                                self.signals.self_model.note_turn(
                                    latest_user_text, raw_reply, frame
                                )
                            except Exception:
                                pass

                        self.signals.history.append({"role": "assistant", "content": raw_reply})

                        if len(self.signals.history) > 20:
                            self.signals.history = self.signals.history[-20:]

                        self.signals.last_message_time = time.time()

                        if hasattr(self.signals, 'save_history'):
                            self.signals.save_history()

                self.signals.AI_thinking = False

            await asyncio.sleep(0.05)