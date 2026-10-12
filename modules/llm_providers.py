"""
Multi-provider LLM client abstraction.

Dispatches on a model spec of the form "provider:model" or bare "model"
(which defaults to ollama). Normalizes request shape, response shape,
and tool-call format across three client classes:

    OllamaClient      — local Ollama, /api/chat
    OpenAICompatClient — /v1/chat/completions (OpenAI, Gemini, Groq,
                        OpenRouter, Together, vLLM, LM Studio...)
    AnthropicClient   — /v1/messages

The public entry point is chat_sync(spec, messages, ...). It returns an
LLMResponse with .content (str) and .tool_calls (list of the Ollama-shaped
dicts {"function": {"name": ..., "arguments": {...}}}), so existing call
sites that read response["message"]["content"] and .get("tool_calls") map
trivially onto it.

Provider config is read from .env. Keys are never returned to the UI
unmasked — see mask_key().

.env keys used:
    OPENAI_API_KEY          OPENAI_BASE_URL (opt)
    ANTHROPIC_API_KEY       ANTHROPIC_BASE_URL (opt)
    GEMINI_API_KEY
    GROQ_API_KEY
    OPENROUTER_API_KEY

Optional free-tier throttling overrides (per provider, no code edit):
    GEMINI_RPM / GEMINI_TPM / GEMINI_RPD     (same for OPENAI_, GROQ_, ...)
    GEMINI_RATE_LIMIT=off                    (disable throttling)
    LLM_MAX_TOOL_CHARS=6000                  (cap on each tool result sent)
"""

from __future__ import annotations

import os
import re
import json
import time
import threading
from collections import OrderedDict, deque
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import requests


# ===============================================================
# Provider registry
# ===============================================================
PROVIDER_PRESETS: Dict[str, Dict[str, Any]] = {
    "ollama": {
        "label":       "Ollama (local)",
        "kind":        "ollama",
        "base_url":    "http://localhost:11434",
        "key_env":     None,
        "doc_url":     "https://ollama.com",
    },
    "openai": {
        "label":       "OpenAI",
        "kind":        "openai",
        "base_url":    "https://api.openai.com/v1",
        "key_env":     "OPENAI_API_KEY",
        "base_url_env":"OPENAI_BASE_URL",
        "doc_url":     "https://platform.openai.com/api-keys",
    },
    "anthropic": {
        "label":       "Anthropic (Claude)",
        "kind":        "anthropic",
        "base_url":    "https://api.anthropic.com",
        "key_env":     "ANTHROPIC_API_KEY",
        "base_url_env":"ANTHROPIC_BASE_URL",
        "doc_url":     "https://console.anthropic.com/settings/keys",
    },
    "gemini": {
        "label":       "Google Gemini",
        "kind":        "openai",
        "base_url":    "https://generativelanguage.googleapis.com/v1beta/openai",
        "key_env":     "GEMINI_API_KEY",
        "base_url_env":"GEMINI_BASE_URL",
        "doc_url":     "https://aistudio.google.com/apikey",
    },
    "groq": {
        "label":       "Groq",
        "kind":        "openai",
        "base_url":    "https://api.groq.com/openai/v1",
        "key_env":     "GROQ_API_KEY",
        "base_url_env":"GROQ_BASE_URL",
        "doc_url":     "https://console.groq.com/keys",
    },
    "openrouter": {
        "label":       "OpenRouter",
        "kind":        "openai",
        "base_url":    "https://openrouter.ai/api/v1",
        "key_env":     "OPENROUTER_API_KEY",
        "base_url_env":"OPENROUTER_BASE_URL",
        "doc_url":     "https://openrouter.ai/keys",
    },
}


# ===============================================================
# Static model catalogs (fallback / authoritative list)
# ===============================================================
PROVIDER_MODEL_OVERRIDES: Dict[str, List[str]] = {
    "gemini": [
        # NOTE (new API key): the older Gemini 2.5 and 2.0 models
        # (gemini-2.5-flash, gemini-2.5-pro, gemini-2.0-flash,
        # gemini-2.0-flash-lite) were REMOVED from this list. They
        # return HTTP 404 "no longer available to new users" for new
        # keys. The TTS / live / image / transcribe variants were also
        # removed because they can't do normal chat completions.
        # Keep newest-first.
        "gemini-3.8-flash",
        "gemini-3.7-flash",
        "gemini-3.6-flash",
        "gemini-3.5-flash",
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
        # Preview models — may change or disappear without notice.
        "gemini-3.1-pro-preview",
        "gemini-3-flash-preview",
    ],
    "openai": [
        "gpt-6-astra", "gpt-6.1-sol", "gpt-6-luna",
        "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
        "gpt-5.5", "gpt-5.5-pro", "gpt-5.4", "gpt-5.4-mini",
        "gpt-5.3-codex", "gpt-5.2", "gpt-5-chat-latest",
        "gpt-4o", "gpt-4o-mini",
        "gpt-4.1", "gpt-4.1-mini", "gpt-4.1-nano",
        "o1", "o1-mini", "o1-pro",
        "o3", "o3-mini", "o3-pro",
        "o4-mini",
    ],
    "anthropic": [
        "claude-opus-5.5", "claude-sonnet-5.5", "claude-haiku-5.5",
        "claude-fable-5.1", "claude-mythos-5.1",
        "claude-fable-5", "claude-opus-5", "claude-sonnet-5",
        "claude-opus-4.8", "claude-opus-4.7", "claude-opus-4.6",
        "claude-sonnet-4.6", "claude-haiku-4.5",
        "claude-sonnet-4", "claude-opus-4",
        "claude-3-5-sonnet-latest", "claude-3-5-haiku-latest",
        "claude-3-opus-latest", "claude-3-haiku-20240307",
    ],
    "groq": [
        "llama-3.3-70b-versatile",
        "llama-3.1-8b-instant",
        "meta-llama/llama-4-scout-17b-16e-instruct",
        "meta-llama/llama-4-maverick-17b-128e-instruct",
        "openai/gpt-oss-120b",
        "openai/gpt-oss-20b",
        "qwen/qwen3.8-27b",
        "qwen/qwen3-32b",
        "deepseek-r1-distill-llama-70b",
        "mistral-saba-24b",
        "mixtral-8x7b-32768",
    ],
    "openrouter": [
        "anthropic/claude-opus-5.5",
        "anthropic/claude-sonnet-5.5",
        "anthropic/claude-fable-5.1",
        "anthropic/claude-mythos-5.1",
        "anthropic/claude-opus-4.8",
        "anthropic/claude-sonnet-4.6",
        "anthropic/claude-haiku-4.5",
        "openai/gpt-6-astra",
        "openai/gpt-6.1-sol",
        "openai/gpt-5.5",
        "openai/gpt-5.5-pro",
        "openai/gpt-5.4",
        "openai/gpt-4o",
        "openai/gpt-4o-mini",
        "openai/o3",
        "openai/o4-mini",
        "google/gemini-3.8-flash",
        "google/gemini-3.5-flash",
        "meta-llama/llama-4-scout",
        "meta-llama/llama-4-maverick",
        "deepseek/deepseek-r1",
        "qwen/qwen3.8-27b",
        "qwen/qwen3-coder",
        "x-ai/grok-4.5",
        "x-ai/grok-4",
        "mistralai/mistral-large",
        "cohere/command-r-plus",
    ],
}


# ===============================================================
# .env helpers
# ===============================================================
_ENV_PATH = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), ".env")


def _read_env_file() -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not os.path.exists(_ENV_PATH):
        return out
    try:
        with open(_ENV_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\r\n")
                if not line or line.lstrip().startswith("#"):
                    continue
                if "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    return out


def write_env_var(name: str, value: Optional[str]) -> bool:
    """Upsert or remove (value=None) a key in .env. Preserves comments
    and ordering. On POSIX tightens perms to 0600."""
    lines: List[str] = []
    if os.path.exists(_ENV_PATH):
        try:
            with open(_ENV_PATH, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()
        except Exception:
            lines = []

    pattern = re.compile(r"^\s*" + re.escape(name) + r"\s*=")
    new_line = f"{name}={value}" if value is not None else None
    replaced = False
    new_lines: List[str] = []
    for line in lines:
        if pattern.match(line):
            if not replaced:
                if new_line is not None:
                    new_lines.append(new_line)
                replaced = True
        else:
            new_lines.append(line)

    if not replaced and new_line is not None:
        new_lines.append(new_line)

    text = "\n".join(new_lines)
    if text and not text.endswith("\n"):
        text += "\n"

    try:
        with open(_ENV_PATH, "w", encoding="utf-8") as f:
            f.write(text)
        try:
            os.chmod(_ENV_PATH, 0o600)
        except Exception:
            pass
        return True
    except Exception:
        return False


def mask_key(key: Optional[str]) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "•" * len(key)
    return f"{key[:4]}…{key[-4:]}"


# ===============================================================
# Model spec parsing
# ===============================================================
def parse_model_spec(spec: str) -> Tuple[str, str]:
    """'openai:gpt-4o-mini' → ('openai', 'gpt-4o-mini')
    'lumi-qwen3'           → ('ollama', 'lumi-qwen3')
    'ollama:lumi-qwen3'    → ('ollama', 'lumi-qwen3')

    Bare strings that exactly match a known provider id are also
    recognized as that provider with an empty model name — this
    matters for get_client("gemini") being called with just the
    provider id.
    """
    s = (spec or "").strip()
    if not s:
        return ("ollama", "")
    if ":" in s:
        head, rest = s.split(":", 1)
        if head.lower() in PROVIDER_PRESETS:
            return (head.lower(), rest)
    if s.lower() in PROVIDER_PRESETS:
        return (s.lower(), "")
    return ("ollama", s)


def make_model_spec(provider: str, model: str) -> str:
    provider = (provider or "ollama").lower()
    if provider == "ollama":
        return model
    return f"{provider}:{model}"


# ===============================================================
# Response normalization
# ===============================================================
@dataclass
class ToolCall:
    name: str
    arguments: Dict[str, Any]
    id: str = ""
    # Provider-specific extras that must be echoed back on the next
    # request. For Gemini 3.x this is
    # {"google": {"thought_signature": "..."}}.
    extra_content: Optional[Dict[str, Any]] = None

    def as_ollama(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "function": {"name": self.name, "arguments": self.arguments},
        }
        if self.id:
            out["id"] = self.id
        if self.extra_content:
            out["extra_content"] = self.extra_content
        return out


@dataclass
class LLMResponse:
    content: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)
    provider: str = "ollama"
    model: str = ""
    latency_ms: int = 0

    def as_ollama_json(self) -> Dict[str, Any]:
        msg: Dict[str, Any] = {"content": self.content}
        if self.tool_calls:
            msg["tool_calls"] = [tc.as_ollama() for tc in self.tool_calls]
        return {"message": msg}


# ---------------------------------------------------------------
# Tool-call memory.
#
# Gemini 3.x returns a "thought_signature" with every function call and
# REQUIRES it to be sent back with that call on the next request,
# otherwise it answers HTTP 400 "Function call is missing a
# thought_signature". If the agent loop rebuilds the assistant message
# and drops the extra fields, we restore the id + signature from here,
# keyed by tool name + arguments.
# ---------------------------------------------------------------
_TOOL_CALL_MEMORY: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
_TOOL_CALL_MEMORY_MAX = 500
_TOOL_MEMORY_LOCK = threading.Lock()

# Documented placeholder that Gemini accepts when a real signature is
# not available (e.g. history that wasn't produced by Gemini).
_GEMINI_DUMMY_SIGNATURE: Dict[str, Any] = {
    "google": {"thought_signature": "skip_thought_signature_validator"},
}


def _tool_key(name: str, args: Any) -> str:
    try:
        a = json.dumps(args, sort_keys=True, default=str)
    except Exception:
        a = str(args)
    return f"{name}|{a}"


def _remember_tool_call(tc: ToolCall) -> None:
    if not (tc.id or tc.extra_content):
        return
    with _TOOL_MEMORY_LOCK:
        key = _tool_key(tc.name, tc.arguments)
        _TOOL_CALL_MEMORY[key] = {
            "id": tc.id, "extra_content": tc.extra_content}
        _TOOL_CALL_MEMORY.move_to_end(key)
        while len(_TOOL_CALL_MEMORY) > _TOOL_CALL_MEMORY_MAX:
            _TOOL_CALL_MEMORY.popitem(last=False)


def _recall_tool_call(name: str, args: Any) -> Dict[str, Any]:
    with _TOOL_MEMORY_LOCK:
        return dict(_TOOL_CALL_MEMORY.get(_tool_key(name, args)) or {})


# ---------------------------------------------------------------
# Free-tier rate limiting (RPM / TPM / RPD), tracked per provider+model.
#
# Fill in the real numbers from https://aistudio.google.com/rate-limit
# (your project's active limits). The values below are CONSERVATIVE
# PLACEHOLDERS, not Google's published numbers. Limits vary by model and
# change over time. Use None for "no limit". Resolution order:
#   .env  (GEMINI_RPM ...)  >  per-model entry  >  provider "default"
# ---------------------------------------------------------------
PROVIDER_RATE_LIMITS: Dict[str, Dict[str, Dict[str, Optional[int]]]] = {
    "gemini": {
        "default": {"rpm": 5, "tpm": 200_000, "rpd": 100},
        # Per-model overrides, e.g.:
        # "gemini-3.5-flash-lite": {"rpm": 10, "tpm": 250_000, "rpd": 500},
    },
}

_USAGE_PATH = os.path.join(os.path.dirname(_ENV_PATH), ".rate_usage.json")
_MAX_TOOL_CHARS_DEFAULT = 6000


class RateLimitExhausted(RuntimeError):
    """Daily quota used up, or the wait for a free slot was too long."""


def _pacific_day() -> str:
    """RPD quotas reset at midnight Pacific time."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")
    except Exception:
        return (datetime.now(timezone.utc) - timedelta(hours=8)).strftime("%Y-%m-%d")


def _limits_for(provider: str, model: str) -> Dict[str, Optional[int]]:
    env = _read_env_file()
    pu = provider.upper()
    if (env.get(f"{pu}_RATE_LIMIT") or "").strip().lower() in ("off", "0", "false"):
        return {}
    table = PROVIDER_RATE_LIMITS.get(provider) or {}
    base = dict(table.get("default") or {})
    base.update(table.get(model) or {})
    for key in ("rpm", "tpm", "rpd"):
        raw = (env.get(f"{pu}_{key.upper()}") or "").strip()
        if raw.isdigit():
            base[key] = int(raw)
        elif raw.lower() in ("none", "unlimited"):
            base[key] = None
    return {k: v for k, v in base.items() if v}


def _estimate_tokens(payload: Dict[str, Any]) -> int:
    try:
        body = json.dumps(
            {"m": payload.get("messages"), "t": payload.get("tools")},
            default=str)
    except Exception:
        body = str(payload)
    return max(1, len(body) // 4)


def _max_tool_chars() -> int:
    raw = (_read_env_file().get("LLM_MAX_TOOL_CHARS") or "").strip()
    return int(raw) if raw.isdigit() else _MAX_TOOL_CHARS_DEFAULT


class _RateLimiter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: Dict[str, deque] = {}
        self._daily: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        try:
            with open(_USAGE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._daily = data
        except Exception:
            self._daily = {}

    def _save(self) -> None:
        try:
            with open(_USAGE_PATH, "w", encoding="utf-8") as f:
                json.dump(self._daily, f)
        except Exception:
            pass

    def acquire(self, provider: str, model: str, est_tokens: int,
                max_wait: float = 90.0) -> Optional[list]:
        lim = _limits_for(provider, model)
        if not lim:
            return None
        key = f"{provider}:{model}"
        rpm, tpm, rpd = lim.get("rpm"), lim.get("tpm"), lim.get("rpd")
        deadline = time.time() + max_wait

        while True:
            wait = 0.0
            with self._lock:
                now = time.time()
                ev = self._events.setdefault(key, deque())
                while ev and now - ev[0][0] >= 60.0:
                    ev.popleft()

                day = _pacific_day()
                d = self._daily.get(key)
                if not d or d.get("day") != day:
                    d = {"day": day, "count": 0}
                    self._daily[key] = d
                if rpd and d["count"] >= rpd:
                    raise RateLimitExhausted(
                        f"Daily limit reached for {key} ({rpd} requests/day). "
                        f"It resets at midnight Pacific time. Switch the "
                        f"model in Models > Routing, or wait.")

                used_tokens = sum(t for _, t in ev)
                fits_rpm = (not rpm) or len(ev) < rpm
                fits_tpm = (not tpm) or used_tokens + est_tokens <= tpm \
                    or not ev   # a single oversized request must still run
                if fits_rpm and fits_tpm:
                    entry = [now, est_tokens]
                    ev.append(entry)
                    d["count"] += 1
                    self._save()
                    return entry
                wait = (ev[0][0] + 60.0 - now) if ev else 1.0

            if time.time() + wait > deadline:
                raise RateLimitExhausted(
                    f"Rate limit wait too long for {key}; try again in "
                    f"about {int(max(wait, 1))}s or use a lighter model.")
            time.sleep(min(max(wait, 0.25), 5.0))

    def adjust(self, entry: Optional[list], actual_tokens: Optional[int]) -> None:
        if entry is None or not actual_tokens:
            return
        with self._lock:
            entry[1] = int(actual_tokens)


_LIMITER = _RateLimiter()


def get_usage_snapshot() -> Dict[str, Any]:
    """Requests used today per provider:model (for a future UI badge)."""
    with _LIMITER._lock:
        return {k: dict(v) for k, v in _LIMITER._daily.items()}


# ---------------------------------------------------------------
# Retry helper for transient provider errors (overload / rate limit).
# Gemini in particular returns HTTP 503 "high demand" in short spikes.
# ---------------------------------------------------------------
_RETRY_STATUSES = (429, 500, 502, 503, 504)
_RETRY_DELAYS = (2.0, 5.0, 10.0)   # seconds; up to 3 retries (4 tries)


def _post_with_retry(url: str, before_attempt=None,
                     **kwargs) -> "requests.Response":
    last_exc: Optional[Exception] = None
    for attempt in range(len(_RETRY_DELAYS) + 1):
        if before_attempt is not None:
            before_attempt()   # every attempt counts toward RPM/TPM/RPD
        try:
            r = requests.post(url, **kwargs)
        except (requests.ConnectionError, requests.Timeout) as e:
            last_exc = e
            r = None
        if r is not None and r.status_code not in _RETRY_STATUSES:
            return r
        if r is not None and r.status_code == 429 and re.search(
                r"per ?day|daily", r.text or "", re.I):
            return r          # daily quota: retrying won't help
        if attempt >= len(_RETRY_DELAYS):
            if r is not None:
                return r          # caller turns the final error into text
            raise last_exc        # type: ignore[misc]
        delay = _RETRY_DELAYS[attempt]
        if r is not None:
            ra = r.headers.get("Retry-After", "")
            if ra.isdigit():
                delay = min(float(ra), 30.0)
        time.sleep(delay)
    raise RuntimeError("unreachable")


# ===============================================================
# Base client
# ===============================================================
class LLMClient:
    provider = "abstract"
    kind = "abstract"

    def __init__(self, base_url: str, api_key: Optional[str] = None,
                 timeout: int = 180):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.timeout = timeout

    def chat(self, messages, tools=None, think=None, options=None,
             keep_alive=None, model=""):
        raise NotImplementedError

    def is_configured(self) -> bool:
        return True

    def list_models(self) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        return ([], "Model listing not supported.")


# ===============================================================
# Ollama
# ===============================================================
class OllamaClient(LLMClient):
    provider = "ollama"
    kind = "ollama"

    def is_configured(self) -> bool:
        try:
            r = requests.get(f"{self.base_url}/api/tags", timeout=3)
            return r.status_code == 200
        except Exception:
            return False

    def list_models(self):
        try:
            r = requests.get(f"{self.base_url}/api/tags", timeout=5)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            return [], str(e)
        out = []
        for m in data.get("models", []) or []:
            d = m.get("details") or {}
            out.append({
                "name": m.get("name") or m.get("model") or "",
                "size": int(m.get("size") or 0),
                "family": d.get("family") or "",
                "parameter_size": d.get("parameter_size") or "",
                "quantization_level": d.get("quantization_level") or "",
                "provider": "ollama",
            })
        return out, None

    def chat(self, messages, tools=None, think=None, options=None,
             keep_alive=None, model=""):
        payload: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
        if think is not None:
            payload["think"] = think
        if keep_alive is not None:
            payload["keep_alive"] = keep_alive
        if options:
            payload["options"] = options

        t0 = time.time()
        r = requests.post(f"{self.base_url}/api/chat",
                          json=payload, timeout=self.timeout)
        r.raise_for_status()
        data = r.json()
        latency = int((time.time() - t0) * 1000)

        msg = data.get("message", {}) or {}
        content = (msg.get("content") or "").strip()
        raw_tool_calls = msg.get("tool_calls") or []

        tcs: List[ToolCall] = []
        for c in raw_tool_calls:
            fn = c.get("function") or {}
            name = fn.get("name")
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {}
            if name:
                tcs.append(ToolCall(
                    name=name,
                    arguments=args or {},
                    id=c.get("id") or "",
                ))

        return LLMResponse(
            content=content, tool_calls=tcs, raw=data,
            provider="ollama", model=model, latency_ms=latency,
        )


# ===============================================================
# OpenAI-compatible (OpenAI, Gemini, Groq, OpenRouter, Together...)
# ===============================================================
class OpenAICompatClient(LLMClient):
    provider = "openai"
    kind = "openai"

    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def list_models(self):
        # Static override wins when present.
        override = PROVIDER_MODEL_OVERRIDES.get(self.provider)
        if override:
            return (
                [{"name": m, "size": 0, "family": self.provider,
                  "parameter_size": "", "quantization_level": "",
                  "provider": self.provider} for m in override],
                None,
            )

        if not self.api_key:
            return [], "API key not set."
        try:
            r = requests.get(f"{self.base_url}/models",
                             headers=self._headers(), timeout=10)
            if r.status_code != 200:
                return [], f"HTTP {r.status_code}: {r.text[:200]}"
            data = r.json()
        except Exception as e:
            return [], str(e)

        out = []
        for m in data.get("data", []) or []:
            mid = m.get("id") or ""
            if not mid:
                continue
            if mid.startswith("models/"):
                mid = mid[len("models/"):]
            out.append({
                "name": mid, "size": 0, "family": "",
                "parameter_size": "", "quantization_level": "",
                "provider": self.provider,
            })
        out.sort(key=lambda x: x["name"])
        return out, None

    def _convert_messages(self, messages):
        """Convert Ollama-shaped messages to OpenAI's chat-completions
        format. Beyond dropping None keys, this enforces things that
        Ollama ignores but OpenAI (and Gemini's compat layer) validate
        strictly:

          1. Every tool_call in an assistant message has an `id`,
             and `type` is set to "function".
          2. Every tool message has a `tool_call_id` that matches a
             preceding assistant tool_call.
          3. `function.arguments` is a JSON string, not a JSON object.
          4. Gemini 3.x: every function call carries its
             `extra_content.google.thought_signature`.
        """
        out: List[Dict[str, Any]] = []
        # Queue of tool_call IDs to consume as matching tool results
        # arrive. Keyed by function name so parallel calls to the same
        # tool are matched in order.
        pending_ids: Dict[str, List[str]] = {}
        synth_counter = 0

        for m in messages:
            role = m.get("role", "user")

            # ── Assistant message carrying tool_calls ─────────────
            if role == "assistant" and m.get("tool_calls"):
                pending_ids = {}
                converted: List[Dict[str, Any]] = []
                for tc in m["tool_calls"]:
                    fn = tc.get("function") or {}
                    name = fn.get("name") or ""
                    args = fn.get("arguments")

                    # Parsed args are needed to look up remembered
                    # id/signature for calls the agent loop rebuilt.
                    if isinstance(args, str):
                        try:
                            parsed_args = json.loads(args)
                        except Exception:
                            parsed_args = {}
                    else:
                        parsed_args = args if args is not None else {}
                    mem = _recall_tool_call(name, parsed_args)

                    call_id = tc.get("id") or mem.get("id") or ""
                    if not call_id:
                        synth_counter += 1
                        call_id = f"call_{name}_{synth_counter}"

                    extra = tc.get("extra_content") or mem.get("extra_content")
                    if not extra and self.provider == "gemini":
                        extra = _GEMINI_DUMMY_SIGNATURE

                    if not isinstance(args, str):
                        args = json.dumps(parsed_args)

                    call: Dict[str, Any] = {
                        "id": call_id,
                        "type": "function",
                        "function": {"name": name, "arguments": args},
                    }
                    if extra:
                        call["extra_content"] = extra
                    converted.append(call)
                    pending_ids.setdefault(name, []).append(call_id)

                entry: Dict[str, Any] = {"role": "assistant"}
                if m.get("content"):
                    entry["content"] = m["content"]
                entry["tool_calls"] = converted
                out.append(entry)
                continue

            # ── Tool result message ──────────────────────────────
            if role == "tool":
                name = m.get("name") or ""
                call_id = m.get("tool_call_id") or ""
                if not call_id:
                    queue = pending_ids.get(name) or []
                    if queue:
                        call_id = queue.pop(0)
                    else:
                        synth_counter += 1
                        call_id = f"call_{name}_{synth_counter}"

                tool_text = m.get("content") or ""
                if (isinstance(tool_text, str)
                        and _limits_for(self.provider, "")):
                    cap = _max_tool_chars()
                    if len(tool_text) > cap:
                        tool_text = (tool_text[:cap]
                                     + f"\n...[truncated {len(tool_text) - cap}"
                                       f" chars to save tokens]")
                entry = {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": tool_text,
                }
                if name:
                    entry["name"] = name
                out.append(entry)
                continue

            # ── User / assistant / system ────────────────────────
            entry = {"role": role}
            if m.get("content") is not None:
                entry["content"] = m.get("content")
            out.append(entry)

        return out

    def _convert_tools(self, tools):
        # Ollama and OpenAI use the same function-schema shape.
        return tools or None

    def chat(self, messages, tools=None, think=None, options=None,
             keep_alive=None, model=""):
        opts = options or {}
        payload: Dict[str, Any] = {
            "model": model,
            "messages": self._convert_messages(messages),
            "stream": False,
        }
        if tools:
            payload["tools"] = self._convert_tools(tools)

        if "temperature" in opts:
            payload["temperature"] = opts["temperature"]
        if "top_p" in opts:
            payload["top_p"] = opts["top_p"]
        if "num_predict" in opts:
            payload["max_tokens"] = opts["num_predict"]

        est = _estimate_tokens(payload)
        slot: Dict[str, Any] = {}

        def _before():
            slot["ev"] = _LIMITER.acquire(self.provider, model, est)

        t0 = time.time()
        r = _post_with_retry(f"{self.base_url}/chat/completions",
                             before_attempt=_before,
                             headers=self._headers(),
                             json=payload, timeout=self.timeout)
        latency = int((time.time() - t0) * 1000)
        if r.status_code >= 400:
            try:
                err = r.json().get("error", {})
                msg = err.get("message") or r.text[:300]
            except Exception:
                msg = r.text[:300]
            raise RuntimeError(f"{self.provider} API error "
                               f"({r.status_code}): {msg}")

        data = r.json()
        _LIMITER.adjust(slot.get("ev"),
                        (data.get("usage") or {}).get("total_tokens"))
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message", {}) or {}
        content = (msg.get("content") or "").strip()
        raw_tool_calls = msg.get("tool_calls") or []

        tcs: List[ToolCall] = []
        for c in raw_tool_calls:
            fn = c.get("function") or {}
            name = fn.get("name")
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {}
            if name:
                tc = ToolCall(
                    name=name,
                    arguments=args or {},
                    id=c.get("id") or "",
                    # Gemini 3.x thought_signature lives here.
                    extra_content=c.get("extra_content") or None,
                )
                _remember_tool_call(tc)
                tcs.append(tc)

        return LLMResponse(
            content=content, tool_calls=tcs, raw=data,
            provider=self.provider, model=model, latency_ms=latency,
        )


# ===============================================================
# Anthropic (Claude)
# ===============================================================
class AnthropicClient(LLMClient):
    provider = "anthropic"
    kind = "anthropic"

    ANTHROPIC_VERSION = "2023-06-01"

    def _headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "x-api-key": self.api_key or "",
            "anthropic-version": self.ANTHROPIC_VERSION,
        }

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def list_models(self):
        override = PROVIDER_MODEL_OVERRIDES.get(self.provider)
        if override:
            return (
                [{"name": m, "size": 0, "family": self.provider,
                  "parameter_size": "", "quantization_level": "",
                  "provider": self.provider} for m in override],
                None,
            )

        if not self.api_key:
            return [], "API key not set."
        try:
            r = requests.get(f"{self.base_url}/v1/models",
                             headers=self._headers(), timeout=10)
            if r.status_code != 200:
                return [], f"HTTP {r.status_code}: {r.text[:200]}"
            data = r.json()
        except Exception as e:
            return [], str(e)

        out = []
        for m in data.get("data", []) or []:
            mid = m.get("id") or ""
            if not mid:
                continue
            out.append({
                "name": mid, "size": 0, "family": "",
                "parameter_size": "", "quantization_level": "",
                "provider": "anthropic",
            })
        out.sort(key=lambda x: x["name"])
        return out, None

    def _convert_messages(self, messages):
        system_parts: List[str] = []
        out: List[Dict[str, Any]] = []

        for m in messages:
            role = m.get("role", "user")
            content = m.get("content") or ""

            if role == "system":
                if content:
                    system_parts.append(content)
                continue

            if role == "tool":
                out.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": m.get("tool_call_id") or m.get("name", "tool"),
                        "content": content if isinstance(content, str)
                                   else json.dumps(content),
                    }],
                })
                continue

            if role == "assistant" and m.get("tool_calls"):
                blocks: List[Dict[str, Any]] = []
                if content:
                    blocks.append({"type": "text", "text": content})
                for c in m["tool_calls"]:
                    fn = c.get("function") or {}
                    args = fn.get("arguments") or {}
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except Exception:
                            args = {}
                    blocks.append({
                        "type": "tool_use",
                        "id": c.get("id") or f"toolu_{int(time.time()*1000)}",
                        "name": fn.get("name") or "",
                        "input": args or {},
                    })
                out.append({"role": "assistant", "content": blocks})
                continue

            out.append({"role": role, "content": content})

        merged: List[Dict[str, Any]] = []
        for m in out:
            if merged and merged[-1]["role"] == m["role"]:
                prev = merged[-1]["content"]
                cur = m["content"]
                if isinstance(prev, str) and isinstance(cur, str):
                    merged[-1]["content"] = prev + "\n\n" + cur
                else:
                    def _to_blocks(x):
                        if isinstance(x, str):
                            return [{"type": "text", "text": x}]
                        return x if isinstance(x, list) else []
                    merged[-1]["content"] = _to_blocks(prev) + _to_blocks(cur)
            else:
                merged.append(m)

        if merged and merged[0]["role"] == "assistant":
            merged.insert(0, {"role": "user", "content": "(continuing)"})

        return "\n\n".join(system_parts).strip(), merged

    def _convert_tools(self, tools):
        out = []
        for t in tools or []:
            fn = t.get("function") or {}
            out.append({
                "name": fn.get("name") or "",
                "description": fn.get("description") or "",
                "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
            })
        return out or None

    def chat(self, messages, tools=None, think=None, options=None,
             keep_alive=None, model=""):
        opts = options or {}
        system_text, amsg = self._convert_messages(messages)

        payload: Dict[str, Any] = {
            "model": model,
            "messages": amsg,
            "max_tokens": opts.get("num_predict", 4096),
        }
        if system_text:
            payload["system"] = system_text
        if "temperature" in opts:
            payload["temperature"] = opts["temperature"]
        if "top_p" in opts:
            payload["top_p"] = opts["top_p"]
        conv_tools = self._convert_tools(tools)
        if conv_tools:
            payload["tools"] = conv_tools

        t0 = time.time()
        r = _post_with_retry(f"{self.base_url}/v1/messages",
                             headers=self._headers(),
                             json=payload, timeout=self.timeout)
        latency = int((time.time() - t0) * 1000)
        if r.status_code >= 400:
            try:
                err = r.json().get("error", {})
                msg = err.get("message") or r.text[:300]
            except Exception:
                msg = r.text[:300]
            raise RuntimeError(f"Anthropic API error "
                               f"({r.status_code}): {msg}")

        data = r.json()
        blocks = data.get("content") or []

        text_parts: List[str] = []
        tcs: List[ToolCall] = []
        for b in blocks:
            btype = b.get("type")
            if btype == "text":
                text_parts.append(b.get("text") or "")
            elif btype == "tool_use":
                tcs.append(ToolCall(
                    name=b.get("name") or "",
                    arguments=b.get("input") or {},
                    id=b.get("id") or "",
                ))

        return LLMResponse(
            content="\n".join(text_parts).strip(),
            tool_calls=tcs, raw=data,
            provider="anthropic", model=model, latency_ms=latency,
        )


# ===============================================================
# Registry / factory
# ===============================================================
_CLIENT_CACHE: Dict[str, LLMClient] = {}
_CACHE_LOCK = threading.Lock()


def _resolve_provider_config(provider_id: str) -> Optional[Dict[str, Any]]:
    preset = PROVIDER_PRESETS.get(provider_id)
    if not preset:
        return None
    env = _read_env_file()
    key_env = preset.get("key_env")
    base_env = preset.get("base_url_env")

    api_key = env.get(key_env) if key_env else None
    base_url = (env.get(base_env) if base_env else None) or preset["base_url"]

    return {
        "id": provider_id,
        "label": preset["label"],
        "kind": preset["kind"],
        "base_url": base_url,
        "api_key": api_key,
        "key_env": key_env,
        "base_url_env": base_env,
        "doc_url": preset.get("doc_url"),
        "requires_key": key_env is not None,
    }


def get_client(provider_or_spec: str) -> LLMClient:
    """Accepts either a bare provider id ("gemini") or a full spec
    ("gemini:gemini-3.8-flash"). Bare ids matching a known provider
    route to that provider — this is the fix for the bug where
    get_client("gemini") fell through to ollama."""
    raw = (provider_or_spec or "").strip()

    if raw.lower() in PROVIDER_PRESETS:
        provider_id = raw.lower()
    else:
        provider_id, _ = parse_model_spec(raw)

    with _CACHE_LOCK:
        cfg = _resolve_provider_config(provider_id)
        if cfg is None:
            raise RuntimeError(f"Unknown provider: {provider_id}")

        cache_key = f"{provider_id}:{cfg['base_url']}:{bool(cfg['api_key'])}"
        cached = _CLIENT_CACHE.get(cache_key)
        if cached is not None:
            return cached

        kind = cfg["kind"]
        if kind == "ollama":
            client: LLMClient = OllamaClient(cfg["base_url"])
        elif kind == "openai":
            client = OpenAICompatClient(cfg["base_url"], cfg["api_key"])
            client.provider = provider_id
        elif kind == "anthropic":
            client = AnthropicClient(cfg["base_url"], cfg["api_key"])
        else:
            raise RuntimeError(f"Unknown provider kind: {kind}")

        _CLIENT_CACHE[cache_key] = client
        return client


def invalidate_client_cache():
    with _CACHE_LOCK:
        _CLIENT_CACHE.clear()


# ===============================================================
# Public entry point
# ===============================================================
def chat_sync(model_spec, messages, tools=None, think=None,
              options=None, keep_alive=None) -> LLMResponse:
    provider_id, model = parse_model_spec(model_spec)
    client = get_client(provider_id)
    return client.chat(
        messages, tools=tools, think=think,
        options=options, keep_alive=keep_alive, model=model,
    )


# ===============================================================
# Provider listing (for the UI)
# ===============================================================
def list_providers() -> List[Dict[str, Any]]:
    out = []
    for pid, preset in PROVIDER_PRESETS.items():
        cfg = _resolve_provider_config(pid)
        if cfg is None:
            continue
        out.append({
            "id": pid,
            "label": cfg["label"],
            "kind": cfg["kind"],
            "base_url": cfg["base_url"],
            "requires_key": cfg["requires_key"],
            "configured": (not cfg["requires_key"]) or bool(cfg["api_key"]),
            "api_key_masked": mask_key(cfg["api_key"]),
            "key_env": cfg["key_env"],
            "base_url_env": cfg["base_url_env"],
            "doc_url": cfg["doc_url"],
        })
    return out


def save_provider_config(provider_id, api_key, base_url):
    pid = (provider_id or "").lower().strip()
    preset = PROVIDER_PRESETS.get(pid)
    if preset is None:
        return False, f"Unknown provider: {provider_id}"

    key_env = preset.get("key_env")
    base_env = preset.get("base_url_env")

    try:
        if key_env is not None and api_key is not None:
            val = api_key.strip()
            write_env_var(key_env, val if val else None)

        if base_env is not None and base_url is not None:
            val = base_url.strip()
            write_env_var(base_env, val if val else None)

        invalidate_client_cache()
        return True, None
    except Exception as e:
        return False, str(e)


def test_provider(provider_id: str) -> Tuple[bool, str]:
    try:
        client = get_client(provider_id)
    except Exception as e:
        return False, str(e)

    if not client.is_configured():
        return False, "API key not configured."

    models, err = client.list_models()
    if err:
        return False, err
    return True, f"Connected — {len(models)} model(s) available."