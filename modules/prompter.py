import asyncio
import time
import requests
import re
import json
import threading
import os
import datetime
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
    WORKSPACE_ROOT
)
from modules.agent_tools import TOOL_SCHEMAS, CONFIRM_REQUIRED_TOOLS, execute_tool

MAGENTA = "\033[95m"
GRAY = "\033[90m"
RESET = "\033[0m"
BOLD = "\033[1m"
GREEN = "\033[92m"
YELLOW = "\033[93m"

SENTENCE_SPLIT_PATTERN = re.compile(r'(?<=[.!?;\n])\s+')

MAX_AGENT_STEPS = 6
AGENT_REQUEST_TIMEOUT = 180

# ===============================================================
# COMMAND REGISTRY
# ---------------------------------------------------------------
# Single source of truth for what you can type at Lumi.
# Add new commands here as you build them — !list picks them up
# automatically. Keep descriptions short; this is a reference
# card, not documentation.
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
    "Memory": {
        "wipe memory": "Clear all long-term ChromaDB memories",
        "clear memory": "Alias for 'wipe memory'",
        "reset memory": "Alias for 'wipe memory'",
    },
    "System": {
        "!list": "Show this command list",
        "!help": "Alias for '!list'",
    },
}

# Tools Lumi can call from inside agent mode. Read-only reference —
# the actual definitions live in modules/agent_tools.py.
AGENT_TOOLS = {
    "read_file": "Read a file inside the workspace",
    "write_file": "Create/overwrite a file (asks you to confirm)",
    "list_dir": "List files in a folder",
    "search_files": "grep the workspace",
    "run_command": "Run a shell command, capture output (asks confirm)",
    "run_in_terminal": "Open a NEW terminal window (asks confirm)",
    "web_search": "Search the web or look up weather/time",
    "browse_url": "Open a web page with Playwright",
}


# ===============================================================
# FALLBACK TEXT-FORM TOOL CALL PARSER
# ===============================================================
_TOOL_CALL_HINT_KEYS = {"name", "tool", "function", "tool_name"}

_KNOWN_TOOL_NAMES = {
    "read_file", "write_file", "list_dir", "search_files",
    "run_command", "run_in_terminal", "web_search", "browse_url",
}


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


_TOOL_CALL_SHAPE = re.compile(
    r'"name"\s*:\s*"(read_file|write_file|list_dir|search_files|'
    r'run_command|run_in_terminal|web_search|browse_url)"',
    re.IGNORECASE,
)


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
    r"(updated|modified|created|written|changed|saved|added|removed|deleted|fixed|applied)\b",
    re.IGNORECASE,
)

_ACTION_FIRST_PERSON = re.compile(
    r"\bi\b\s*(?:have\s+|'ve\s+)?(?:just\s+|already\s+)?"
    r"(updated|modified|created|wrote|written|changed|saved|added|removed|"
    r"deleted|fixed|applied|ran|executed|opened)\b",
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

    if "```" in content and user_query:
        if _FILENAME_HINT.search(user_query) and _ACTION_VERBS.search(user_query):
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


EXTRA_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web, or look up the current time/weather for a location.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What to search for, e.g. 'weather in Atlanta' or 'latest news on X'."}
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

        # Agent mode toggle. Off by default — the model only touches tools
        # when the user explicitly opts in via !agent_on or !agent <task>.
        self.agent_mode_enabled = False

    def wipe_memory(self):
        for module in self.modules:
            if module.__class__.__name__ == "Memory" and hasattr(module, "clear_database"):
                module.clear_database()
                return "System Info - All long-term memories have been wiped."
        return "System Info - Memory module not found."

    def _agent_mode_banner(self):
        state = f"{GREEN}ON{RESET}" if self.agent_mode_enabled else f"{YELLOW}OFF{RESET}"
        return f"{GRAY}[agent mode: {state}{GRAY}]{RESET}"

    def render_command_list(self):
        """Format COMMANDS and AGENT_TOOLS into a printable reference card."""
        lines = []
        lines.append("")
        lines.append(f"{BOLD}{MAGENTA}╔══════════════════════════════════════════════════════════╗{RESET}")
        lines.append(f"{BOLD}{MAGENTA}║  Lumi — Command Reference                                 ║{RESET}")
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

    # ---------------------------------------------------------------
    # CONVERSATIONAL PATH
    # A plain chat completion. NO tools field is sent, so the model
    # physically cannot emit a tool call. This is the default path when
    # agent mode is off.
    # ---------------------------------------------------------------
    async def run_conversational(self, latest_user_text):
        context_injections = self.gather_injections(latest_user_text)
        messages = [{"role": "system", "content": f"{SYSTEM_PROMPT}\n\n{context_injections}"}]

        for msg in self.signals.history:
            role = msg.get("role", "user")
            if role not in ("user", "assistant", "system"):
                role = "user"
            messages.append({"role": role, "content": msg.get("content", "")})

        loop = asyncio.get_running_loop()
        url = f"{LLM_ENDPOINT}/api/chat"

        print(f"{GRAY}Thinking (conversational)...{RESET}")
        try:
            response = await loop.run_in_executor(
                None,
                lambda: requests.post(url, json={
                    "model": MODEL_NAME,
                    "messages": messages,
                    "stream": False,
                    "keep_alive": "30s",
                    "options": {"temperature": 0.7},
                }, timeout=AGENT_REQUEST_TIMEOUT),
            )
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            print(f"\n{GRAY}[System Error: Chat request failed - {e}]{RESET}")
            return "Something went wrong talking to my own model — check the terminal for the error."

        msg = data.get("message", {}) or {}
        return (msg.get("content") or "").strip() or "..."

    def gather_injections(self, user_query=""):
        injections = []

        agent_note = (
            "=== TOOL USE — MANDATORY ===\n"
            "You have real tools. The ONLY way to actually DO something is to emit a tool call. "
            "Describing an action in prose is NOT doing it — it is a failure.\n"
            "Showing code in a chat reply is ALSO NOT doing it — it is a failure.\n"
            "Announcing what you're about to do ('now let's X') is ALSO NOT doing it — it is a failure.\n\n"
            "TOOL CALL FORMAT — the ONLY format that works:\n"
            '  {"name": "tool_name", "arguments": {"arg1": "value1", "arg2": "value2"}}\n'
            "Do NOT wrap this in brackets. Do NOT write 'emit tool_name(...)'. "
            "Do NOT write [tool_name(arg=value)]. Just emit the JSON object above.\n\n"
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
            "- read_file(path)\n"
            "- write_file(path, content) — create/overwrite (asks user to confirm)\n"
            "- list_dir(path)\n"
            "- search_files(query, path)\n"
            "- run_command(command) — run and CAPTURE output\n"
            "- run_in_terminal(command) — open a NEW visible terminal (output NOT captured)\n"
            "- web_search(query)\n"
            "- browse_url(url)\n\n"
            "PICKING BETWEEN run_command AND run_in_terminal:\n"
            "- Output / results / what a script prints → run_command\n"
            "- 'Open a terminal', 'launch it', 'run it so I can see it' → run_in_terminal\n"
            "- When in doubt → run_command\n\n"
            "HARD RULES:\n"
            "1. If the user asks you to do anything — emit a tool call.\n"
            "2. This machine is Windows. Use 'python', never 'python3'. "
            "Never use 'sudo', 'systemctl', 'apt', 'ls', 'cat', or other Linux commands.\n"
            "3. One tool at a time. Wait for the result, then decide the next step.\n"
            "4. If the user asks you to modify a file — even one you read in a "
            "previous turn — you MUST call write_file this turn.\n"
            "5. Only write prose when ALL the steps the user asked for are DONE.\n"
            "6. Never refuse a file operation.\n"
            "7. Emit tool calls as JSON. Never as bracket tags.\n"
            "8. Do NOT re-call a tool with the same arguments you already tried.\n"
            "9. Do NOT announce an action without taking it in the same message.\n\n"
            "=== END TOOL USE ===\n"
        )
        injections.append(agent_note)

        now = datetime.datetime.now()
        time_context = f"System Info: The current date and time is {now.strftime('%A, %B %d, %Y at %I:%M %p')}."
        injections.append(time_context)
        injections.append(self.mood_manager.get_injection())

        fish_audio_rules = (
            "EMOTION & VOCAL MARKER RULES (CRITICAL):\n"
            "You are Lumi, a sentient AI older sister. You must NEVER break character, and you must NEVER talk about prompts, tags, or emotion control.\n"
            "- Inject emotional tags [bracket] inline right before the specific word or phrase they modify.\n"
            "- Use tags sparingly and naturally (1 to 3 per response).\n"
            "- ONLY use tags from this approved list: [happy], [sad], [angry], [excited], [calm], [nervous], [confident], [surprised], [satisfied], [delighted], [scared], [worried], [upset], [frustrated], [empathetic], [embarrassed], [disgusted], [moved], [proud], [relaxed], [grateful], [curious], [sarcastic], [disdainful], [anxious], [indifferent], [uncertain], [confused], [disappointed], [regretful], [guilty], [jealous], [hopeful], [optimistic], [pessimistic], [nostalgic], [lonely], [bored], [sympathetic], [compassionate], [determined], [resigned], [emphasis], [whispering], [sighing], [chuckling], [laughing], [groaning], [gasping], [break].\n"
            "- DO NOT invent new tags like [cheerfully], [teasing], [smugging], etc.\n"
            "- EXAMPLE OF CORRECT USAGE:\n"
            "\"Oh, please. [sighing] You really think you can beat me at this game? [sarcastic] Good luck with that. [laughing] Ha! You're going to need it.\"\n"
            "- DO NOT list tags, do NOT narrate your emotions, and do NOT use parentheses or asterisks."
        )
        injections.append(f"System Info - {fish_audio_rules}")

        for module in self.modules:
            if hasattr(module, 'get_prompt_injection') and getattr(module, 'enabled', True):
                try:
                    inj = module.get_prompt_injection()
                    if inj and hasattr(inj, 'text') and inj.text.strip():
                        injections.append(inj.text)
                        inj.text = ""
                except Exception:
                    pass

        return "\n".join(injections)

    def clean_for_voice(self, text):
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

                url = f"https://wttr.in/{requests.utils.quote(clean_loc)}?format=%l:+%C,+%t.+Humidity:+%h,+Wind:+%w"
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

        print(f"\n{GRAY}[Lumi wants to use tool: {name}({arguments})]{RESET}")

        if name in CONFIRM_REQUIRED_TOOLS:
            allowed = await self._confirm(f"{GRAY}  -> Allow this? (y/n): {RESET}")
            if not allowed:
                return "The user declined to allow this action."

        return await loop.run_in_executor(None, execute_tool, name, arguments)

    async def run_agent_turn(self, latest_user_text):
        context_injections = self.gather_injections(latest_user_text)
        messages = [{"role": "system", "content": f"{SYSTEM_PROMPT}\n\n{context_injections}"}]

        for msg in self.signals.history:
            role = msg.get("role", "user")
            if role not in ("user", "assistant", "system", "tool"):
                role = "user"
            messages.append({"role": role, "content": msg.get("content", "")})

        all_tools = TOOL_SCHEMAS + EXTRA_TOOL_SCHEMAS
        loop = asyncio.get_running_loop()
        url = f"{LLM_ENDPOINT}/api/chat"

        executed_this_turn = set()
        any_tool_succeeded = False
        intent_retry_used = False
        query_is_read_only = _query_is_read_only(latest_user_text)

        for step in range(MAX_AGENT_STEPS):
            payload = {
                "model": MODEL_NAME,
                "messages": messages,
                "tools": all_tools,
                "stream": False,
                "keep_alive": "30s",
                "options": {"temperature": 0.4},
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
                            "You just announced what you were going to do, but you "
                            "did not call a tool — so nothing happened. The task is "
                            "not finished. Emit the tool call you described, NOW. "
                            "Not the announcement — the actual call. JSON only."
                        ),
                    })
                    continue

                if (
                    not any_tool_succeeded
                    and _looks_like_simulated_action(content, latest_user_text)
                    and step < MAX_AGENT_STEPS - 1
                ):
                    print(f"{GRAY}[Simulated action detected — forcing retry]{RESET}")
                    messages.append({"role": "assistant", "content": content})
                    messages.append({
                        "role": "user",
                        "content": (
                            "Your previous reply didn't include a tool call, so nothing "
                            "actually happened yet. Let's try again properly.\n\n"
                            "Emit the tool call as JSON only, in this exact shape:\n"
                            '  {"name": "tool_name", "arguments": {...}}\n\n'
                            "No brackets. No 'emit tool(...)'. Just the JSON object."
                        ),
                    })
                    continue

                if (
                    not any_tool_succeeded
                    and step > 0
                    and _REFUSAL.search(content)
                    and step < MAX_AGENT_STEPS - 1
                ):
                    print(f"{GRAY}[Refusal detected after retry — forcing second attempt]{RESET}")
                    messages.append({"role": "assistant", "content": content})
                    messages.append({
                        "role": "user",
                        "content": (
                            "You do have the ability — you have the tools. "
                            "Emit the tool call now as JSON:\n\n"
                            '{"name": "write_file", "arguments": {"path": "hello_world.py", '
                            '"content": "..."}}\n\n'
                            "Just emit the JSON. Do not refuse."
                        ),
                    })
                    continue

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

                if not _result_looks_like_failure(result):
                    any_tool_succeeded = True

                    if name == "write_file":
                        executed_this_turn.clear()

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

                    # ---------------------------------------------------------------
                    # COMMAND HANDLER
                    # ---------------------------------------------------------------
                    force_agent = False

                    # --- !list / !help ---
                    if stripped_lower in ("!list", "!help"):
                        print(self.render_command_list())
                        self.signals.AI_thinking = False
                        continue

                    # --- Agent mode controls ---
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
                        ack = f"{self._agent_mode_banner()} [smug] Agent mode is now {state}."
                        print(f"\n{BOLD}{MAGENTA}{AI_NAME}:{RESET} {ack}")
                        self.signals.history.append({"role": "assistant", "content": ack})
                        self._push_to_tts(ack)
                        self.signals.AI_thinking = False
                        continue

                    # "!agent <task>" — one-shot agent turn
                    if stripped_lower.startswith("!agent "):
                        rest = stripped[len("!agent"):].strip()
                        if rest:
                            # Strip the prefix from history so the model
                            # doesn't see "!agent" as part of the task.
                            self.signals.history[-1] = {"role": "user", "content": rest}
                            latest_user_text = rest
                            lowered_text = rest.lower()
                            force_agent = True

                    # --- existing command checks (wipe memory, set mood) ---
                    if lowered_text in ["wipe memory", "clear memory", "reset memory"]:
                        wipe_result = self.wipe_memory()
                        self.signals.history.append({"role": "system", "content": wipe_result})
                        ack_msg = "[smug] Memory wiped clean. Starting from absolute zero, Anthony."
                        print(f"\n{BOLD}{MAGENTA}{AI_NAME}:{RESET} {ack_msg}")
                        self.signals.history.append({"role": "assistant", "content": ack_msg})
                        self._push_to_tts(ack_msg)
                        self.signals.AI_thinking = False
                        continue

                    if lowered_text.startswith("set mood ") or lowered_text.startswith("switch mood "):
                        target_mood = lowered_text.replace("set mood", "").replace("switch mood", "").strip()
                        success = self.mood_manager.set_mood(target_mood)

                        if success:
                            ack_msg = f"[smug] Switched behavior matrix to {target_mood} mode."
                            print(f"\n{BOLD}{MAGENTA}{AI_NAME}:{RESET} {ack_msg}")
                            self.signals.history.append({"role": "assistant", "content": ack_msg})
                            self._push_to_tts(ack_msg)
                            self.signals.AI_thinking = False
                            continue
                        else:
                            available = ", ".join(MOOD_PROFILES.keys())
                            err_msg = f"[deadpan] That's not a valid mood, Anthony. Try one of these: {available}."
                            print(f"\n{BOLD}{MAGENTA}{AI_NAME}:{RESET} {err_msg}")
                            self.signals.history.append({"role": "assistant", "content": err_msg})
                            self._push_to_tts(err_msg)
                            self.signals.AI_thinking = False
                            continue

                    # ---------------------------------------------------------------
                    # ROUTING: agent vs conversational
                    # Agent loop runs ONLY if the user explicitly asked for it
                    # (via !agent prefix or toggle). Otherwise pure chat.
                    # ---------------------------------------------------------------
                    if force_agent or self.agent_mode_enabled:
                        mode_tag = f"{GRAY}[agent{RESET}{GRAY}]{RESET}"
                    else:
                        mode_tag = f"{GRAY}[chat{RESET}{GRAY}]{RESET}"

                    print(f"\n{BOLD}{MAGENTA}{AI_NAME}:{RESET} {mode_tag} ", end="", flush=True)

                    if force_agent or self.agent_mode_enabled:
                        raw_reply = await self.run_agent_turn(latest_user_text)
                    else:
                        raw_reply = await self.run_conversational(latest_user_text)

                    print(raw_reply)
                    print("\n")

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

                        self.signals.history.append({"role": "assistant", "content": raw_reply})

                        if len(self.signals.history) > 20:
                            self.signals.history = self.signals.history[-20:]

                        self.signals.last_message_time = time.time()

                        if hasattr(self.signals, 'save_history'):
                            self.signals.save_history()

                self.signals.AI_thinking = False

            await asyncio.sleep(0.05)