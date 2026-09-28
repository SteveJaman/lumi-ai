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
"""

from __future__ import annotations

import os
import re
import json
import time
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import requests


# ===============================================================
# Provider registry
# ===============================================================
# Presets for known providers. All "kind": "openai" entries speak the
# OpenAI chat-completions protocol, so one client class handles them all.
# Users can add arbitrary providers by dropping a custom entry into
# constants.LLM_PROVIDERS_CUSTOM.
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
        "kind":        "openai",   # Gemini exposes an OpenAI-compatible endpoint
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
                line = line.rstrip("\n")
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
    and ordering; writes atomically. On POSIX tightens perms to 0600."""
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
            # else: drop the line (removing the var)
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
    'ollama:lumi-qwen3'    → ('ollama', 'lumi-qwen3')"""
    s = (spec or "").strip()
    if not s:
        return ("ollama", "")
    if ":" in s:
        head, rest = s.split(":", 1)
        if head.lower() in PROVIDER_PRESETS:
            return (head.lower(), rest)
    return ("ollama", s)


def make_model_spec(provider: str, model: str) -> str:
    """Inverse of parse_model_spec. Ollama models stay bare for
    backwards compat with existing constants."""
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

    def as_ollama(self) -> Dict[str, Any]:
        return {"function": {"name": self.name, "arguments": self.arguments}}


@dataclass
class LLMResponse:
    content: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)
    provider: str = "ollama"
    model: str = ""
    latency_ms: int = 0

    def as_ollama_json(self) -> Dict[str, Any]:
        """Shape compatible with what the old code read directly."""
        msg: Dict[str, Any] = {"content": self.content}
        if self.tool_calls:
            msg["tool_calls"] = [tc.as_ollama() for tc in self.tool_calls]
        return {"message": msg}


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

    def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        think: Optional[bool] = None,
        options: Optional[Dict[str, Any]] = None,
        keep_alive: Optional[str] = None,
        model: str = "",
    ) -> LLMResponse:
        raise NotImplementedError

    def is_configured(self) -> bool:
        return True

    def list_models(self) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """Returns (models, error). Override per provider."""
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
                tcs.append(ToolCall(name=name, arguments=args or {}))

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
            out.append({
                "name": mid,
                "size": 0,
                "family": "",
                "parameter_size": "",
                "quantization_level": "",
                "provider": "openai",
            })
        out.sort(key=lambda x: x["name"])
        return out, None

    # -- message / tool conversion from Ollama shape --
    def _convert_messages(self, messages):
        """Ollama already speaks the OpenAI message protocol almost
        verbatim. We just drop None keys and normalize tool_calls."""
        out = []
        for m in messages:
            role = m.get("role", "user")
            entry: Dict[str, Any] = {"role": role}
            if m.get("content") is not None:
                entry["content"] = m.get("content")
            if role == "tool":
                # OpenAI requires tool_call_id; synthesize from name
                entry["tool_call_id"] = m.get("tool_call_id") or m.get("name", "tool")
                if m.get("name"):
                    entry["name"] = m["name"]
            if m.get("tool_calls"):
                entry["tool_calls"] = m["tool_calls"]
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

        # Translate Ollama's options block to OpenAI's flat fields.
        if "temperature" in opts:
            payload["temperature"] = opts["temperature"]
        if "top_p" in opts:
            payload["top_p"] = opts["top_p"]
        if "num_predict" in opts:
            payload["max_tokens"] = opts["num_predict"]
        # num_ctx has no OpenAI equivalent — ignore silently.

        t0 = time.time()
        r = requests.post(f"{self.base_url}/chat/completions",
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
                tcs.append(ToolCall(name=name, arguments=args or {}))

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
                "name": mid,
                "size": 0,
                "family": "",
                "parameter_size": "",
                "quantization_level": "",
                "provider": "anthropic",
            })
        out.sort(key=lambda x: x["name"])
        return out, None

    # -- message / tool conversion from Ollama shape --
    def _convert_messages(self, messages):
        """Returns (system_text, anthropic_messages).
        Anthropic takes system as a top-level param, not a message."""
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
                # Anthropic tool results are user messages with a
                # tool_result content block.
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

            # Plain user / assistant
            out.append({"role": role, "content": content})

        # Anthropic requires alternating user/assistant and the first
        # message to be from the user. Merge consecutive same-role turns.
        merged: List[Dict[str, Any]] = []
        for m in out:
            if merged and merged[-1]["role"] == m["role"]:
                prev = merged[-1]["content"]
                cur = m["content"]
                if isinstance(prev, str) and isinstance(cur, str):
                    merged[-1]["content"] = prev + "\n\n" + cur
                else:
                    # Coerce to block list
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
        r = requests.post(f"{self.base_url}/v1/messages",
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
    """Merge preset + .env overrides for one provider."""
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
    """provider_or_spec is either a provider id ('openai') or a full
    model spec ('openai:gpt-4o-mini' — provider portion is used)."""
    provider_id, _ = parse_model_spec(provider_or_spec)

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
            client.provider = provider_id  # for accurate error messages
        elif kind == "anthropic":
            client = AnthropicClient(cfg["base_url"], cfg["api_key"])
        else:
            raise RuntimeError(f"Unknown provider kind: {kind}")

        _CLIENT_CACHE[cache_key] = client
        return client


def invalidate_client_cache():
    """Call after the user saves a new API key so the next request
    rebuilds the client with fresh credentials."""
    with _CACHE_LOCK:
        _CLIENT_CACHE.clear()


# ===============================================================
# Public entry point
# ===============================================================
def chat_sync(
    model_spec: str,
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
    think: Optional[bool] = None,
    options: Optional[Dict[str, Any]] = None,
    keep_alive: Optional[str] = None,
) -> LLMResponse:
    """Synchronous dispatch. Wraps client construction, provider id
    extraction, and the model-name split into one call.

    Example:
        resp = chat_sync("openai:gpt-4o-mini", messages)
        print(resp.content)

        resp = chat_sync("lumi-qwen3", messages, tools=TOOL_SCHEMAS)
        for tc in resp.tool_calls:
            print(tc.name, tc.arguments)
    """
    provider_id, model = parse_model_spec(model_spec)
    client = get_client(provider_id)
    return client.chat(
        messages,
        tools=tools,
        think=think,
        options=options,
        keep_alive=keep_alive,
        model=model,
    )


# ===============================================================
# Provider listing (for the UI)
# ===============================================================
def list_providers() -> List[Dict[str, Any]]:
    """Return a UI-facing list of providers with masked keys + config."""
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


def save_provider_config(provider_id: str, api_key: Optional[str],
                         base_url: Optional[str]) -> Tuple[bool, Optional[str]]:
    """Persist provider config to .env. api_key=None leaves it alone;
    api_key='' removes it. base_url=None leaves it alone;
    base_url='' resets to the preset default."""
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
            # Empty string → reset to preset default (remove override).
            write_env_var(base_env, val if val else None)

        invalidate_client_cache()
        return True, None
    except Exception as e:
        return False, str(e)


def test_provider(provider_id: str) -> Tuple[bool, str]:
    """Ping the provider with a tiny models call. Returns (ok, message)."""
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