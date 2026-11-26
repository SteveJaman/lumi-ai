"""
Model router: sort a prompt into a tier before Lumi answers it.

    route_model(prompt) -> "haiku" | "sonnet" | "opus"

    haiku   simple chat, greetings, small talk
    sonnet  writing code, editing files, ordinary requests      (the default)
    opus    hard problems: architecture, debugging, deep analysis

A local Ollama model (ROUTER_MODEL: constants.GAME_AGENT_MODEL, qwen3.5:9b, the
agent's daily model) does the sorting, but only when it's already loaded: on the
11 GB card it can't sit next to the chat model, so loading it for every chat
message would evict chat and back again. Then, and in Gaming Mode, the prompt
gets the default tier at once. Obvious small talk is recognised without asking,
and anything that goes wrong (Ollama down, model missing, slow, nonsense answer)
falls back to "sonnet".

Agent turns also get a TASK tier (task_tier, TurnRouter, bottom of this file):
daily (chat, web, media, light tools: a model that fits in VRAM, 16K context) or
coding (the coder, 4K context), from LUMI.md ## Model. That picks the local model;
the haiku/sonnet/opus tier above still drives the Pipeline card, Claude Code's
model and cloud escalation.

What a tier RUNS (see TIER_INFO): Lumi's chat is local, so haiku means her fast
local model and sonnet / opus her main local model. The names matter for real
when she delegates to Claude Code: that run uses `--model haiku|sonnet|opus`
on your Claude plan. The Pipeline view shows both the tier and the real model.
"""

from __future__ import annotations

import os
import re
import threading
import time
from typing import Dict, Optional

import requests

import constants

TIERS = ("haiku", "sonnet", "opus")
DEFAULT_TIER = "sonnet"
ROUTER_MODEL = getattr(constants, "ROUTER_MODEL", None) or getattr(constants, "GAME_AGENT_MODEL", "qwen3.5:9b")
LOADED_TTL_S = 10.0            # how long a /api/ps answer is trusted
TIMEOUT_S = 4.0                # never hold up a reply longer than this
COOLDOWN_S = 60.0              # after a failure, don't ask again for a minute (no 4 s wait per message)
MAX_PROMPT_CHARS = 1500        # the router only needs the gist

# Small talk the router doesn't need a model for.
_SMALL_TALK = re.compile(
    r"^\s*(hi+|hey+|hello+|yo|sup|gm|gn|good (morning|night|evening|afternoon)|thanks?( you)?|thx|ty|"
    r"ok(ay)?|cool|nice|lol|lmao|haha+|bye|see (you|ya)|how are you|what'?s up|wyd|love you|ily)"
    r"[\s!.?~,:)(<3]*$", re.I)

_SYSTEM = (
    "You sort a user's message for an AI assistant into one of three tiers. Reply with ONE word.\n"
    "haiku: greetings, small talk, feelings, quick facts, casual questions.\n"
    "sonnet: writing or changing code, editing files, normal tasks, how-to questions.\n"
    "opus: hard problems: system architecture, tricky bugs, debugging failures, deep multi-step analysis.\n"
    "Examples:\n"
    "\"hey lumi how was your day\" -> haiku\n"
    "\"write a python script that renames my photos by date\" -> sonnet\n"
    "\"fix the typo in notes.txt\" -> sonnet\n"
    "\"my websocket reconnect loop leaks memory and crashes after an hour, find out why\" -> opus\n"
    "\"design the database schema and service boundaries for my app\" -> opus\n"
    "Answer with exactly one of: haiku, sonnet, opus."
)

_TIER_WORD = re.compile(r"\b(haiku|sonnet|opus)\b", re.I)

_last_error_log = {"t": 0.0}
_last_reason = {"text": ""}
_down_until = {"t": 0.0}
_lock = threading.Lock()


def _gaming() -> bool:
    return bool(getattr(constants, "GAMING_MODE", False))


def last_reason() -> str:
    """How route_model decided its last answer."""
    return _last_reason["text"]


def parse_tier(text: str) -> str:
    """The first tier word in the model's answer, or the default."""
    m = _TIER_WORD.search(text or "")
    return m.group(1).lower() if m else DEFAULT_TIER


def heuristic_tier(prompt: str) -> Optional[str]:
    """A tier when the answer is obvious without a model, else None."""
    text = (prompt or "").strip()
    if not text:
        return "haiku"
    if len(text) <= 60 and _SMALL_TALK.match(text):
        return "haiku"
    return None


def _ask_model(prompt: str) -> str:
    payload = {
        "model": ROUTER_MODEL,
        "messages": [{"role": "system", "content": _SYSTEM},
                     {"role": "user", "content": prompt[:MAX_PROMPT_CHARS]}],
        "stream": False,
        "think": False,
        "keep_alive": "30m",
        "options": {"temperature": 0, "num_predict": 4, "num_ctx": loaded_context(ROUTER_MODEL) or 2048},
    }
    r = requests.post(f"{constants.LLM_ENDPOINT.rstrip('/')}/api/chat", json=payload, timeout=TIMEOUT_S)
    r.raise_for_status()
    return ((r.json() or {}).get("message") or {}).get("content", "")


_loaded_cache: Dict[str, object] = {"t": 0.0, "models": None}


def _loaded() -> Dict[str, Optional[int]]:
    """{name: context_length} of the models Ollama has in memory (/api/ps), cached
    for LOADED_TTL_S. Empty when Ollama can't be reached."""
    now = time.time()
    with _lock:
        if _loaded_cache["models"] is not None and now - float(_loaded_cache["t"]) < LOADED_TTL_S:
            return dict(_loaded_cache["models"])
    try:
        r = requests.get(f"{constants.LLM_ENDPOINT.rstrip('/')}/api/ps", timeout=2)
        r.raise_for_status()
        models = {(m.get("name") or m.get("model")): m.get("context_length")
                  for m in (r.json() or {}).get("models", []) if m.get("name") or m.get("model")}
    except Exception:
        models = {}
    with _lock:
        _loaded_cache.update(t=now, models=models)
    return dict(models)


def loaded_models() -> list:
    return list(_loaded())


def is_loaded(model: str) -> bool:
    return any(_same_model(n, model) for n in _loaded())


def loaded_context(model: str) -> Optional[int]:
    """The context window a loaded model is running with. Asking for a different
    num_ctx makes Ollama reload it, which takes far longer than the router waits."""
    return next((ctx for n, ctx in _loaded().items() if _same_model(n, model)), None)


def route_model(prompt: str) -> str:
    """Return "haiku", "sonnet" or "opus" for this prompt. Never raises.
    last_reason() says how it was decided (the Models tab shows it)."""
    try:
        quick = heuristic_tier(prompt)
        if quick is not None:
            _last_reason["text"] = "small talk" if (prompt or "").strip() else "empty prompt"
            return quick
        if _gaming():
            _last_reason["text"] = "Gaming Mode: default tier"
            return DEFAULT_TIER                       # don't load the router model while gaming
        if time.time() < _down_until["t"]:
            _last_reason["text"] = "local router unavailable: default tier"
            return DEFAULT_TIER                       # it failed a moment ago: don't make every message wait
        if not is_loaded(ROUTER_MODEL):
            _last_reason["text"] = f"{ROUTER_MODEL} not loaded: default tier (keeps chat in VRAM)"
            return DEFAULT_TIER                       # loading it would evict the chat model
        tier = parse_tier(_ask_model(str(prompt)))
        _last_reason["text"] = f"sorted by {ROUTER_MODEL}"
        return tier
    except Exception as e:                            # Ollama down, model missing, timeout, bad JSON
        _last_reason["text"] = f"local router failed ({type(e).__name__}): fell back to sonnet"
        now = time.time()
        with _lock:
            _down_until["t"] = now + COOLDOWN_S
            if now - _last_error_log["t"] > 300:      # say it once every 5 minutes, not every turn
                _last_error_log["t"] = now
                print(f"\033[90m[router] falling back to sonnet ({type(e).__name__}); "
                      f"is `{ROUTER_MODEL}` pulled in Ollama?\033[0m")
        return DEFAULT_TIER


# What each tier actually runs. Lumi's chat is local; Claude Code is the one
# place the Claude model names are used for real (on your plan, not an API key).
TIER_INFO: Dict[str, Dict[str, str]] = {
    "haiku":  {"local": "fast", "claude_code": "haiku"},
    "sonnet": {"local": "main", "claude_code": "sonnet"},
    "opus":   {"local": "main", "claude_code": "opus"},
}


def claude_code_model(tier: Optional[str]) -> Optional[str]:
    return TIER_INFO.get(tier or "", {}).get("claude_code")


# ── tools ────────────────────────────────────────────────────────
# The prompt picks the tier before Lumi answers; a tool she then calls can
# raise it for the rest of the turn (the Pipeline card, and Claude Code's
# model if she delegates afterwards). MCP tools (mcp__<server>__<tool>,
# modules/mcp_client.py) default to sonnet; ones that write, delete, send or
# run something are opus.
_HEAVY_TOOL_WORDS = frozenset(
    "write edit create delete remove rm move rename send run exec execute update set put post patch "
    "insert drop kill install uninstall upload push commit modify append replace save deploy publish "
    "restart shutdown reset purge truncate pay buy order".split())


def tool_tier(tool_name: str) -> str:
    """The tier a tool call needs: "opus" for MCP tools that change, send or
    run things, "sonnet" for other MCP tools and everything else."""
    name = tool_name or ""
    if not name.startswith("mcp__"):
        return DEFAULT_TIER
    tool = name.split("__", 2)[-1]
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", tool)
    words = [w for w in re.split(r"[^a-z0-9]+", spaced.lower()) if w]
    return "opus" if any(w in _HEAVY_TOOL_WORDS for w in words) else "sonnet"


def max_tier(a: Optional[str], b: Optional[str]) -> str:
    """The higher of two tiers (haiku < sonnet < opus); unknown counts as the default."""
    rank = {t: i for i, t in enumerate(TIERS)}
    a = a if a in rank else DEFAULT_TIER
    b = b if b in rank else DEFAULT_TIER
    return a if rank[a] >= rank[b] else b


# ── game agents: a per-call model override ───────────────────────
# A game agent (plugin/minecraft_bot.py) picks its goals with the local game
# model unless the Games page chose a cloud provider for it. The choice is
# saved per game in config/lumi.yaml:  games: {minecraft: {agent_model: claude}}
AGENT_MODEL_CHOICES = ("local", "claude", "openai", "gemini")
AGENT_MODEL_LABELS = {"local": "Local", "claude": "Claude", "openai": "OpenAI", "gemini": "Gemini"}
# Choice -> llm_providers provider id (whose API key it needs).
AGENT_PROVIDERS = {"claude": "anthropic", "openai": "openai", "gemini": "gemini"}


class CloudKeyMissing(RuntimeError):
    """The chosen cloud provider has no API key. The game loop pauses on it."""

    def __init__(self, choice: str):
        self.choice = choice
        self.label = AGENT_MODEL_LABELS.get(choice, choice)
        super().__init__(f"{self.label} key not set")


def local_agent_model() -> str:
    """The local model game agents use: MINECRAFT_AGENT_LOCAL_MODEL, else LUMI.md
    `game_model`, else GAME_AGENT_MODEL."""
    override = getattr(constants, "MINECRAFT_AGENT_LOCAL_MODEL", None)
    if override:
        return override
    try:
        from modules.harness import config as hcfg
        configured = hcfg.game_model()
    except Exception:
        configured = None
    return configured or getattr(constants, "GAME_AGENT_MODEL", "qwen3.5:9b")


def provider_has_key(choice: str) -> bool:
    """Whether a cloud choice's provider has an API key in .env."""
    provider = AGENT_PROVIDERS.get(choice)
    if provider is None:
        return False
    from modules.llm_providers import _resolve_provider_config
    cfg = _resolve_provider_config(provider) or {}
    return bool(cfg.get("api_key"))


def available_agent_models() -> list:
    """"local", then every cloud choice whose provider has a key."""
    out = ["local"]
    for choice in AGENT_MODEL_CHOICES[1:]:
        try:
            if provider_has_key(choice):
                out.append(choice)
        except Exception:
            pass
    return out


def agent_model_spec(*, model_override: Optional[str] = None, local_model: Optional[str] = None) -> str:
    """The provider:model spec a game agent's next call runs on.
    model_override None or "local" -> the local model (local_model, else
    local_agent_model()); "claude" / "openai" / "gemini" -> that provider's
    model from MINECRAFT_AGENT_CLOUD_MODELS. Raises CloudKeyMissing when that
    provider has no key (never falls back to local), ValueError for an
    unknown choice."""
    if model_override in (None, "", "local"):
        return local_model or local_agent_model()
    if model_override not in AGENT_PROVIDERS:
        raise ValueError(f"unknown agent model: {model_override}")
    if not provider_has_key(model_override):
        raise CloudKeyMissing(model_override)
    return cloud_agent_model(model_override)


def cloud_agent_model(choice: str) -> str:
    models = getattr(constants, "MINECRAFT_AGENT_CLOUD_MODELS", {}) or {}
    return models.get(choice) or {
        "claude": "anthropic:claude-sonnet-5-5", "openai": "openai:gpt-5.5",
        "gemini": "gemini:gemini-3.5-flash"}[choice]


_choice_cache: Dict[str, object] = {"mtime": None, "data": {}}


def agent_model_choice(game: str = "minecraft") -> str:
    """The saved choice for a game (config/lumi.yaml games.<game>.agent_model),
    else MINECRAFT_AGENT_MODEL. Re-read only when the file changes, so a game
    loop can call it before every decision."""
    default = getattr(constants, "MINECRAFT_AGENT_MODEL", "local") if game == "minecraft" else "local"
    path = getattr(constants, "YAML_PATH", None)
    try:
        mtime = os.path.getmtime(path)
    except (OSError, TypeError):
        return default
    if mtime != _choice_cache["mtime"]:
        try:
            import yaml
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f.read().replace("<host>", "host")) or {}
        except Exception:
            data = {}
        _choice_cache.update(mtime=mtime, data=data if isinstance(data, dict) else {})
    games = _choice_cache["data"].get("games") or {}
    value = ((games.get(game) or {}) if isinstance(games, dict) else {}).get("agent_model")
    return value if value in AGENT_MODEL_CHOICES else default


def agent_model_info(choice: str, local_model: Optional[str] = None) -> Dict[str, str]:
    """{choice, label, model} for the Games page: "Claude (claude-sonnet-5-5)"."""
    spec = (local_model or local_agent_model()) if choice in (None, "", "local") else cloud_agent_model(choice)
    model = spec.split(":", 1)[1] if spec.split(":", 1)[0] in ("ollama", "anthropic", "openai", "gemini",
                                                                 "groq", "openrouter") else spec
    return {"choice": choice or "local", "label": AGENT_MODEL_LABELS.get(choice or "local", choice),
            "model": model}


# ─── tiered agent models ───────────────────────────────────────────────
# Agent-mode turns pick a local model by tier: sonnet / opus (and !auto tasks)
# run the PRIMARY chain, haiku the FAST chain. Each chain comes from LUMI.md
# (## Model) and is tried in order; the first model that is installed in Ollama
# and fits in VRAM wins. Chat mode is untouched (MODEL_NAME / FAST_MODEL_NAME).

INSTALLED_TTL_S = 60.0
_installed_cache: Dict[str, object] = {"t": 0.0, "models": None}
_vram_cache: Dict[str, object] = {"done": False, "gb": None}


def installed_models() -> Dict[str, float]:
    """{model name: size in GB} from Ollama's /api/tags, cached for a minute.
    Empty when Ollama can't be reached."""
    now = time.time()
    with _lock:
        if _installed_cache["models"] is not None and now - float(_installed_cache["t"]) < INSTALLED_TTL_S:
            return dict(_installed_cache["models"])
    try:
        r = requests.get(f"{constants.LLM_ENDPOINT.rstrip('/')}/api/tags", timeout=3)
        r.raise_for_status()
        models = {m["name"]: round(float(m.get("size") or 0) / 1e9, 2)
                  for m in (r.json() or {}).get("models", []) if m.get("name")}
    except Exception:
        models = {}
    with _lock:
        _installed_cache.update(t=now, models=models)
    return dict(models)


def gpu_vram_gb() -> Optional[float]:
    """Total memory of the first NVIDIA GPU in GB (nvidia-smi), detected once.
    None when there is no NVIDIA GPU or the tool is missing."""
    if _vram_cache["done"]:
        return _vram_cache["gb"]
    gb = None
    try:
        import subprocess
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        first = (out.stdout or "").strip().splitlines()[0]
        gb = round(float(first) / 1024, 1)
    except Exception:
        gb = None
    _vram_cache.update(done=True, gb=gb)
    return gb


def _same_model(a: str, b: str) -> bool:
    norm = lambda n: n if ":" in n else n + ":latest"
    return norm(a.strip().lower()) == norm(b.strip().lower())


def pick_model(chain, installed: Dict[str, float], vram_gb: Optional[float],
               headroom_gb: float) -> Optional[str]:
    """The first chain entry that is installed and fits in vram - headroom;
    else the first installed entry (it will spill to the CPU); else None.
    Unknown VRAM counts as 'fits'."""
    present = []
    for want in chain:
        name = next((n for n in installed if _same_model(n, want)), None)
        if name is not None:
            present.append(name)
    if not present:
        return None
    if vram_gb is None:
        return present[0]
    budget = vram_gb - headroom_gb
    for name in present:
        if installed.get(name, 0.0) <= budget:
            return name
    return present[0]


def agent_model_for_tier(tier: Optional[str]) -> str:
    """The local model for an agent-mode turn of this tier. Never raises:
    falls back to LUMI.md `agent_model` when no chain entry is installed."""
    from modules.harness import config as hcfg
    fallback = hcfg.agent_model()
    try:
        fast = (tier == "haiku") or _gaming()          # games need the VRAM
        chain = hcfg.fast_agent_models() if fast else hcfg.primary_agent_models()
        vram = hcfg.vram_gb()
        if vram is None:
            vram = gpu_vram_gb()
        picked = pick_model(chain, installed_models(), vram, hcfg.vram_headroom_gb())
        return picked or fallback
    except Exception:
        return fallback


# ─── task tiers: daily vs coding (LUMI.md ## Model) ────────────────────
# The 11 GB card can't hold the 14B coder AND a big context: the coder spills to
# system RAM, and a scraped web page in its KV cache makes it crawl. So every
# agent turn is sorted by the work it asks for:
#   daily   conversation, web, memory, media, games, light tools -> a model that
#           fits in VRAM, with a big context (daily_num_ctx, 16K)
#   coding  writing / fixing code -> the coder, with a small context (4K)
# and a coding turn that calls a web or media tool finishes on the daily tier.

TASK_TIERS = ("daily", "coding")

# Words that mean "go and look / play something": checked first, so "look up how
# to fix X" stays on the big-context model.
_DAILY_WORDS = re.compile(
    r"\b(search(es|ed|ing)?|browse|browsing|look\s*(it\s*)?up|google|web\s*(site|page)?s?|website|url|link|"
    r"online|internet|news|weather|play(ing)?|pause|skip|spotify|song|music|playlist|album|podcast|"
    r"queue|volume|shuffle|minecraft)\b", re.I)
_CODE_WORDS = re.compile(
    r"\b(code|coding|script|function|bug|bugs|debug(ging)?|stack\s*trace|traceback|exception|"
    r"refactor|compile[sd]?|unit\s*tests?|pytest|regex|python|javascript|typescript|java|rust|golang|c\+\+|"
    r"c#|sql|html|css|json|yaml|api|endpoint|repo|git|commit|syntax|segfault|null\s*pointer|implement)\b"
    r"|\.(py|js|ts|tsx|jsx|rs|go|java|cpp|cc|c|h|cs|rb|php|sh|ps1|bat|sql|html|css|scss|json|ya?ml|toml|ini)\b",
    re.I)

# Tools whose output is big or whose work is "daily": the turn moves to the daily tier.
WEB_MEDIA_TOOLS = frozenset({"web_search", "browse_url", "recall_memory", "search_library",
                             "dispatch_game_command", "look_at_minecraft",
                             "vtube_set_expression", "vtube_move"})
WEB_MEDIA_PREFIXES = ("spotify_", "media_", "sdk__")

# The only tools a 4K coding turn is offered (plus the harness task tools).
CODING_TOOLS = frozenset({"read_file", "write_file", "list_dir", "search_files", "run_command",
                          "run_in_terminal", "task_list", "task_add", "task_next", "task_complete",
                          "task_fail"})


def task_tier(prompt: str) -> str:
    """'daily' or 'coding' for a request. Web and media words win; code words
    (or a source file name) make it coding; everything else is daily."""
    text = prompt or ""
    if _DAILY_WORDS.search(text):
        return "daily"
    if _CODE_WORDS.search(text):
        return "coding"
    return "daily"


def is_web_media_tool(name: str) -> bool:
    name = name or ""
    return name in WEB_MEDIA_TOOLS or name.startswith(WEB_MEDIA_PREFIXES)


def tier_for_tools(names) -> Optional[str]:
    """'daily' when any of these tools is a web or media tool, else None (no change)."""
    return "daily" if any(is_web_media_tool(n) for n in names or ()) else None


def tools_used(messages) -> list:
    """Tool names already called in a conversation (assistant tool_calls and tool results)."""
    names = []
    for m in messages or ():
        if not isinstance(m, dict):
            continue
        if m.get("role") == "tool" and m.get("name"):
            names.append(str(m["name"]))
        for tc in m.get("tool_calls") or ():
            fn = (tc.get("function") or {}) if isinstance(tc, dict) else {}
            if fn.get("name"):
                names.append(str(fn["name"]))
    return names


def model_for_task(task: str) -> str:
    """The local model for a task tier. Daily must fit in VRAM (falls back to the
    first installed); coding takes the first installed entry even if it spills
    (its context is capped instead). Gaming Mode always uses daily. Never raises."""
    from modules.harness import config as hcfg
    fallback = hcfg.agent_model()
    try:
        if _gaming():
            task = "daily"
        installed = installed_models()
        if task == "coding":
            chain = hcfg.coding_models()
            picked = next((n for want in chain for n in installed if _same_model(n, want)), None)
            return picked or fallback
        vram = hcfg.vram_gb()
        if vram is None:
            vram = gpu_vram_gb()
        return pick_model(hcfg.daily_models(), installed, vram, hcfg.vram_headroom_gb()) or fallback
    except Exception:
        return fallback


def num_ctx_for(task: str) -> int:
    from modules.harness import config as hcfg
    return hcfg.tier_num_ctx("daily" if _gaming() else task)


_caps_cache: Dict[str, list] = {}


def model_supports_tools(model: str) -> bool:
    """Whether Ollama says this model takes structured tool calls (cached; True
    when Ollama can't be asked or the model is a cloud spec)."""
    if not model or model.split(":", 1)[0] in ("anthropic", "openai", "gemini", "groq", "openrouter"):
        return True                                   # a cloud model: tools are native
    if model not in _caps_cache:
        try:
            r = requests.post(f"{constants.LLM_ENDPOINT.rstrip('/')}/api/show", json={"model": model}, timeout=3)
            r.raise_for_status()
            _caps_cache[model] = list((r.json() or {}).get("capabilities") or ["tools"])
        except Exception:
            return True
    return "tools" in _caps_cache[model]


TEXT_TOOL_HINT = (
    "To use a tool, reply with ONLY a JSON object on its own, no other text: "
    '{"name": "<tool name>", "arguments": {<arguments>}}. One tool per reply. '
    "Tools you can use: {names}.")


class TurnRouter:
    """The model and context window for each model call of one agent turn.
    Starts on task_tier(prompt); a coding turn that has called a web or media
    tool moves to daily for the rest of the turn (never back)."""

    def __init__(self, prompt: str):
        self.start = "daily" if _gaming() else task_tier(prompt)
        self.task = self.start
        self.model = model_for_task(self.task)
        self.num_ctx = num_ctx_for(self.task)

    def route(self, messages) -> str:
        """The tier for the next call, given the conversation so far."""
        if self.task == "coding" and tier_for_tools(tools_used(messages)) == "daily":
            self.task = "daily"
            self.model = model_for_task("daily")
            self.num_ctx = num_ctx_for("daily")
        return self.task

    @staticmethod
    def coding_tools(tools) -> list:
        return [t for t in tools or () if ((t.get("function") or {}).get("name") in CODING_TOOLS)]
