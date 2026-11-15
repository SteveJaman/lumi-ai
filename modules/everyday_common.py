"""
Shared plumbing for the "Everyday Life" tool branches
(email_agent, finance_agent, meeting_agent, windows_agent).

Each of those modules exposes the same small interface, which
agent_tools.py collects and the prompter dispatches:

    AGENT          which Pipeline card runs it ("system" or "main")
    TOOL_SCHEMAS   Ollama tool schemas
    CONFIRM_TOOLS  tools that must get the user's approval (HITL) first
    available(ctx) -> bool, whether to offer the tools this turn
    async run_tool(name, args, ctx) -> str

Confirm tools call `await ctx.confirm(tool_name, preview)` themselves, at
the point where they know exactly what they're about to do (a drafted
reply, a resolved button), so the approval shows the real action rather
than the raw arguments.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

import constants

LLM_TIMEOUT_S = 180


class EverydayError(Exception):
    """A tool failed in a way the model should hear about in plain words."""


@dataclass
class ToolContext:
    """What an everyday tool may touch. Built by the prompter per call."""
    signals: Any = None
    # async (tool_name, preview) -> bool. Defaults to "declined" so a tool
    # wired up without an approval path can never act on its own.
    confirm: Callable[[str, Any], Awaitable[bool]] = None
    # sync url -> page text (BrowserController.browse_url), or None.
    browse: Optional[Callable[[str], str]] = None
    log: Callable[[str], None] = field(default=lambda msg: None)

    def __post_init__(self):
        if self.confirm is None:
            async def _decline(_name, _preview):
                return False
            self.confirm = _decline

    @property
    def hub(self):
        return getattr(self.signals, "workspace_hub", None)


# ─── LLM ──────────────────────────────────────────────────────────────

_THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)


def _model_spec() -> str:
    return getattr(constants, "AGENT_MODEL_NAME", None) or constants.MODEL_NAME


async def ask_llm(system: str, user: str, *, json_mode: bool = False,
                  max_tokens: int = 900, temperature: float = 0.2,
                  num_ctx: Optional[int] = None) -> str:
    """One-shot completion on Lumi's agent model. Local Ollama is called
    over aiohttp so the event loop never blocks; any other configured
    provider goes through llm_providers.chat_sync in a worker thread."""
    from modules.llm_providers import parse_model_spec

    spec = _model_spec()
    provider, model = parse_model_spec(spec)
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    options = {"temperature": temperature, "num_predict": max_tokens,
               "num_ctx": num_ctx or getattr(constants, "MAIN_NUM_CTX", 8192)}

    if provider == "ollama":
        import aiohttp
        payload = {"model": model, "messages": messages, "stream": False,
                   "think": False, "options": options, "keep_alive": "30m"}
        if json_mode:
            payload["format"] = "json"
        url = f"{constants.LLM_ENDPOINT.rstrip('/')}/api/chat"
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=LLM_TIMEOUT_S)) as s:
                async with s.post(url, json=payload) as r:
                    if r.status == 404:
                        raise EverydayError(f"{model} isn't pulled in Ollama.")
                    if r.status >= 400:
                        raise EverydayError(f"Ollama answered {r.status}: {(await r.text())[:200]}")
                    data = await r.json()
        except asyncio.TimeoutError:
            raise EverydayError(f"{model} took longer than {LLM_TIMEOUT_S}s.")
        except aiohttp.ClientError as e:
            raise EverydayError(f"couldn't reach Ollama ({type(e).__name__}).")
        text = ((data or {}).get("message") or {}).get("content", "")
    else:
        from modules.llm_providers import chat_sync
        if json_mode:
            messages[0]["content"] += "\nReply with a single JSON object and nothing else."
        resp = await asyncio.to_thread(chat_sync, spec, messages, None, False, options)
        text = resp.content

    return _THINK_RE.sub("", text or "").strip()


def parse_json(text: str) -> Any:
    """Parse model output that should be JSON, tolerating code fences and
    chatter around the object. Raises EverydayError if there's none."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip())
    try:
        return json.loads(text)
    except ValueError:
        pass
    for opener in ("{", "["):
        i = text.find(opener)
        if i == -1:
            continue
        try:
            obj, _ = json.JSONDecoder().raw_decode(text, i)
            return obj
        except ValueError:
            continue
    raise EverydayError("the model didn't return valid JSON.")


def clip(text: Any, limit: int) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[:limit] + "\n... [truncated]"


def one_line(text: Any, limit: int = 120) -> str:
    return " ".join(str(text or "").split())[:limit]
