"""
Model router: sort a prompt into a tier before Lumi answers it.

    route_model(prompt) -> "haiku" | "sonnet" | "opus"

    haiku   simple chat, greetings, small talk
    sonnet  writing code, editing files, ordinary requests      (the default)
    opus    hard problems: architecture, debugging, deep analysis

A small local Ollama model (ROUTER_MODEL, qwen2.5:3b by default) does the
sorting. It's the same model the Minecraft agent uses, so it is often already
loaded. Obvious small talk is recognised without asking it, and anything that
goes wrong (Ollama down, model missing, slow, nonsense answer) falls back to
"sonnet". In Gaming Mode the model isn't asked at all, so nothing extra is
loaded into VRAM while you play.

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
ROUTER_MODEL = getattr(constants, "ROUTER_MODEL", None) or getattr(constants, "GAME_AGENT_MODEL", "qwen2.5:3b")
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
_down_until = {"t": 0.0}
_lock = threading.Lock()


def _gaming() -> bool:
    return bool(getattr(constants, "GAMING_MODE", False))


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
        "options": {"temperature": 0, "num_predict": 4, "num_ctx": 2048},
    }
    r = requests.post(f"{constants.LLM_ENDPOINT.rstrip('/')}/api/chat", json=payload, timeout=TIMEOUT_S)
    r.raise_for_status()
    return ((r.json() or {}).get("message") or {}).get("content", "")


def route_model(prompt: str) -> str:
    """Return "haiku", "sonnet" or "opus" for this prompt. Never raises."""
    try:
        quick = heuristic_tier(prompt)
        if quick is not None:
            return quick
        if _gaming():
            return DEFAULT_TIER                       # don't load the router model while gaming
        if time.time() < _down_until["t"]:
            return DEFAULT_TIER                       # it failed a moment ago: don't make every message wait
        return parse_tier(_ask_model(str(prompt)))
    except Exception as e:                            # Ollama down, model missing, timeout, bad JSON
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
    """The local model game agents use: MINECRAFT_AGENT_LOCAL_MODEL, else
    GAME_AGENT_MODEL."""
    return (getattr(constants, "MINECRAFT_AGENT_LOCAL_MODEL", None)
            or getattr(constants, "GAME_AGENT_MODEL", "qwen2.5:3b"))


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
