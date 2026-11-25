"""
Claude smart router: send each request to the cheapest Claude tier that can do it.

    classify_complexity(prompt, context) -> "haiku" | "sonnet" | "opus"
    await call_claude(messages, system_prompt, tools=None) -> LLMResponse
    call_claude_sync(...)                                  same, for worker threads
    usage_summary()                                        what data/claude_usage.json holds

Routing
  1. A local heuristic (length, code blocks, keywords such as "architecture",
     "debug", "refactor") settles the clear cases for free.
  2. Only an ambiguous prompt costs a micro-call: the haiku model answers with one
     word (thinking off, 8 output tokens).
  3. A turn that offers tools never runs below sonnet; small models that misfire on
     tool calls just loop.
  4. Auto-escalation: if haiku or sonnet errors, or returns a malformed tool call
     (unknown tool, arguments that aren't an object, or a reply cut off mid-call),
     the same request is retried once on the next tier up.

Model ids and prices come from constants.CLAUDE_ROUTER_MODELS / CLAUDE_ROUTER_PRICES.
Calls go through llm_providers.AnthropicClient, so the API key is the one already
saved in Settings -> Models -> Providers.

Every call adds its tokens to data/claude_usage.json (per tier, plus what the same
tokens would have cost on opus) and tells the Model Pipeline view in the workspace
pane: claude_model_switch when a tier is picked, claude_token_stream after each
reply. The view is fed after the fact, so a failure there never affects the call.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import constants
from constants import DATA_ROOT

TIERS = ("haiku", "sonnet", "opus")
DEFAULT_TIER = "sonnet"
TOOL_FLOOR = "sonnet"

DEFAULT_MODELS = {"haiku": "claude-haiku-5-5", "sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5"}
DEFAULT_PRICES = {"haiku": (0.10, 0.50), "sonnet": (2.00, 10.00), "opus": (4.00, 20.00)}
TIER_COST = {"haiku": "low", "sonnet": "medium", "opus": "high"}

USAGE_PATH = os.path.join(DATA_ROOT, "claude_usage.json")
MAX_OUTPUT_TOKENS = 16000          # non-streaming: room to finish without hitting HTTP timeouts
CLASSIFIER_MAX_TOKENS = 8
CLASSIFIER_PROMPT_CHARS = 2000

_usage_lock = threading.Lock()
_ACCOUNT_ERROR_RE = re.compile(r"credit balance|billing|\((401|403)\)|api[ _-]?key|permission|unauthori[sz]ed",
                               re.I)

# ─── heuristics ──────────────────────────────────────────────────────

_SMALL_TALK = re.compile(
    r"^\s*(hi+|hey+|hello+|yo|sup|gm|gn|good (morning|night|evening|afternoon)|thanks?( you)?|thx|ty|"
    r"ok(ay)?|cool|nice|lol|lmao|haha+|bye|see (you|ya)|how are you|what'?s up|wyd|love you|ily)"
    r"[\s!.?~,:)(<3]*$", re.I)

_OPUS_WORDS = re.compile(
    r"\b(architect(ure|ural)?|system design|design (the|a|an) (system|schema|database|api)|refactor(ing)?|"
    r"debug(ging)?|root cause|race condition|deadlock|memory leak|segfault|stack trace|traceback|"
    r"concurren(cy|t)|distributed|scal(e|ability)|migrat(e|ion)|security (audit|review)|threat model|"
    r"trade-?offs?|prove|proof|optimi[sz]e (the )?(algorithm|performance)|why (does|is) .* (fail|crash|break))\b",
    re.I)

_SONNET_WORDS = re.compile(
    r"\b(code|function|class|script|implement|write|fix|bug|error|test|regex|sql|query|api|endpoint|json|"
    r"yaml|html|css|javascript|typescript|python|file|edit|convert|parse|summari[sz]e|explain|compare)\b",
    re.I)

_CODE_LINE = re.compile(r"^\s*(def |class |import |from \S+ import|function |const |let |var |#include|"
                        r"public |private |return |if \(|for \(|\}|\{)", re.M)


def _score(prompt: str) -> Tuple[int, List[str]]:
    text = prompt or ""
    score, why = 0, []
    n = len(text)
    if n > 2500:
        score, why = score + 2, why + ["long prompt"]
    elif n > 600:
        score, why = score + 1, why + ["medium-length prompt"]
    elif n < 120:
        score, why = score - 1, why + ["short prompt"]
    if "```" in text or len(_CODE_LINE.findall(text)) >= 3:
        score, why = score + 1, why + ["code block"]
    opus_hits = {m.group(0).lower() for m in _OPUS_WORDS.finditer(text)}
    if opus_hits:
        score += min(3 * len(opus_hits), 5)
        why.append("keywords: " + ", ".join(sorted(opus_hits)[:3]))
    elif _SONNET_WORDS.search(text):
        score, why = score + 2, why + ["coding/task words"]
    return score, why


def heuristic(prompt: str) -> Tuple[Optional[str], str]:
    """(tier, reason) when the answer is clear without a model, else (None, reason)."""
    text = (prompt or "").strip()
    if not text:
        return "haiku", "empty prompt"
    if len(text) <= 60 and _SMALL_TALK.match(text):
        return "haiku", "small talk"
    score, why = _score(text)
    reason = ", ".join(why) or "plain question"
    if score <= -1:
        return "haiku", reason
    if score >= 4:
        return "opus", reason
    if score in (1, 2):
        return "sonnet", reason
    return None, reason                      # 0 or 3: could go either way


# ─── config ──────────────────────────────────────────────────────────

def models() -> Dict[str, str]:
    cfg = getattr(constants, "CLAUDE_ROUTER_MODELS", None) or {}
    return {t: str(cfg.get(t) or DEFAULT_MODELS[t]) for t in TIERS}


def prices() -> Dict[str, Tuple[float, float]]:
    cfg = getattr(constants, "CLAUDE_ROUTER_PRICES", None) or {}
    out = {}
    for t in TIERS:
        try:
            i, o = cfg.get(t) or DEFAULT_PRICES[t]
            out[t] = (float(i), float(o))
        except (TypeError, ValueError):
            out[t] = DEFAULT_PRICES[t]
    return out


def next_tier(tier: str) -> Optional[str]:
    i = TIERS.index(tier) if tier in TIERS else TIERS.index(DEFAULT_TIER)
    return TIERS[i + 1] if i + 1 < len(TIERS) else None


def max_tier(a: str, b: str) -> str:
    rank = {t: i for i, t in enumerate(TIERS)}
    a = a if a in rank else DEFAULT_TIER
    b = b if b in rank else DEFAULT_TIER
    return a if rank[a] >= rank[b] else b


def _client():
    from modules.llm_providers import get_client
    return get_client("anthropic")


# ─── classification ─────────────────────────────────────────────────

_CLASSIFIER_SYSTEM = (
    "Sort the user's request for an AI assistant into a model tier. Reply with ONE word.\n"
    "haiku: small talk, quick facts, simple rewording or lookups.\n"
    "sonnet: writing or changing code, tool use, normal multi-step tasks, explanations.\n"
    "opus: hard problems: architecture, tricky debugging, deep multi-step analysis or proofs.\n"
    "Answer with exactly one of: haiku, sonnet, opus."
)
_TIER_WORD = re.compile(r"\b(haiku|sonnet|opus)\b", re.I)


def _context_text(context: Any) -> str:
    if not context:
        return ""
    if isinstance(context, str):
        return context
    try:
        return json.dumps(context, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(context)


def _classify(prompt: str, context: Any = None, client=None) -> Tuple[str, str]:
    """(tier, reason). Never raises: an unreachable classifier means sonnet."""
    tier, reason = heuristic(prompt)
    if tier is not None:
        return tier, reason
    cl = client or _client()
    if not cl.is_configured():
        return DEFAULT_TIER, "ambiguous; no API key for the classifier"
    ctx = _context_text(context)
    user = (prompt or "")[:CLASSIFIER_PROMPT_CHARS]
    if ctx:
        user = f"Context: {ctx[:500]}\n\nRequest: {user}"
    hk = models()["haiku"]
    try:
        resp = cl.chat([{"role": "system", "content": _CLASSIFIER_SYSTEM}, {"role": "user", "content": user}],
                       options={"num_predict": CLASSIFIER_MAX_TOKENS,
                                "anthropic_payload": {"thinking": {"type": "disabled"}}},
                       model=hk)
    except Exception as e:
        return DEFAULT_TIER, f"ambiguous; classifier failed ({type(e).__name__})"
    _record("haiku", hk, _usage_of(resp), classifier=True)
    m = _TIER_WORD.search(resp.content or "")
    if not m:
        return DEFAULT_TIER, "ambiguous; classifier gave no tier"
    return m.group(1).lower(), f"classified by {hk} ({reason})"


def classify_complexity(prompt: str, context: Any = None) -> str:
    """"haiku", "sonnet" or "opus" for this prompt: a free heuristic first, and a
    one-word haiku micro-call only when the heuristic can't tell."""
    return _classify(prompt, context)[0]


# ─── calling ────────────────────────────────────────────────────────

def _last_user_text(messages: List[Dict[str, Any]]) -> str:
    for m in reversed(messages or []):
        if m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            return " ".join(str(b.get("text") or "") for b in c if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _tool_names(tools) -> set:
    return {((t.get("function") or {}).get("name") or t.get("name")) for t in (tools or []) if isinstance(t, dict)}


def malformed_tool_call(resp, tools) -> Optional[str]:
    """Why this reply's tool calls can't be run, or None when they're fine."""
    calls = list(getattr(resp, "tool_calls", None) or [])
    raw = getattr(resp, "raw", None) or {}
    if calls and raw.get("stop_reason") == "max_tokens":
        return "the tool call was cut off"
    names = _tool_names(tools)
    for c in calls:
        if not c.name:
            return "a tool call has no name"
        if names and c.name not in names:
            return f"called an unknown tool '{c.name}'"
        if not isinstance(c.arguments, dict):
            return f"'{c.name}' arguments aren't an object"
    return None


def _usage_of(resp) -> Dict[str, int]:
    u = (getattr(resp, "raw", None) or {}).get("usage") or {}

    def num(k):
        try:
            return int(u.get(k) or 0)
        except (TypeError, ValueError):
            return 0
    return {"input_tokens": num("input_tokens") + num("cache_read_input_tokens") + num("cache_creation_input_tokens"),
            "output_tokens": num("output_tokens")}


def _call_sync(messages, system_prompt, tools=None, *, tier: Optional[str] = None,
               context: Any = None, client=None):
    cl = client or _client()
    if not cl.is_configured():
        raise RuntimeError("No Anthropic API key. Add one in Settings -> Models -> Providers.")
    msgs = list(messages or [])
    if system_prompt:
        msgs = [{"role": "system", "content": str(system_prompt)}] + msgs

    if tier in TIERS:
        reason = "chosen by caller"
    else:
        tier, reason = _classify(_last_user_text(msgs), context, client=cl)
    if tools and TIERS.index(tier) < TIERS.index(TOOL_FLOOR):
        tier, reason = TOOL_FLOOR, f"tool-calling turn (never below {TOOL_FLOOR}); was {reason}"

    escalated = False
    while True:
        model = models()[tier]
        _emit_switch(tier, model, reason)
        problem, err = None, None
        try:
            resp = cl.chat(msgs, tools=tools or None, options={"num_predict": MAX_OUTPUT_TOKENS}, model=model)
        except Exception as e:
            resp, err = None, e
            problem = f"{type(e).__name__}: {str(e)[:160]}"
        if resp is not None:
            _record(tier, model, _usage_of(resp))
            if tools:
                problem = malformed_tool_call(resp, tools)
        up = next_tier(tier)
        # Billing, key and permission errors hit every tier alike: don't spend a retry on them.
        account_problem = err is not None and bool(_ACCOUNT_ERROR_RE.search(str(err)))
        if problem:
            _emit_failure(tier, model, problem, None if (escalated or account_problem) else up)
        if problem and not escalated and up is not None and not account_problem:
            escalated = True
            reason = f"auto-escalation from {tier}: {problem}"
            tier = up
            continue
        if err is not None:
            raise err
        if resp is not None:
            try:
                resp.raw = dict(resp.raw or {}, lumi_tier=tier, lumi_escalated=escalated)
            except Exception:
                pass
        return resp


def call_claude_sync(messages, system_prompt, tools=None, *, tier: Optional[str] = None,
                     context: Any = None, client=None):
    """call_claude for code that already runs in a worker thread."""
    return _call_sync(messages, system_prompt, tools, tier=tier, context=context, client=client)


async def call_claude(messages, system_prompt, tools=None, *, tier: Optional[str] = None,
                      context: Any = None, client=None):
    """Route the request to haiku / sonnet / opus and return the LLMResponse.
    Tool-calling turns run on sonnet or higher; an error or malformed tool call on
    haiku or sonnet is retried once on the next tier up."""
    return await asyncio.to_thread(_call_sync, messages, system_prompt, tools,
                                   tier=tier, context=context, client=client)


# ─── usage ──────────────────────────────────────────────────────────

def _empty_usage() -> Dict[str, Any]:
    return {"tiers": {t: {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0} for t in TIERS},
            "classifier_calls": 0, "updated": None}


def _cost(tier: str, inp: int, out: int) -> float:
    pi, po = prices()[tier]
    return (inp * pi + out * po) / 1_000_000


def _load_usage(path: str) -> Dict[str, Any]:
    data = _empty_usage()
    try:
        with open(path, encoding="utf-8") as f:
            saved = json.load(f)
    except (OSError, ValueError):
        return data
    if isinstance(saved, dict):
        for t in TIERS:
            row = (saved.get("tiers") or {}).get(t)
            if isinstance(row, dict):
                for k in ("calls", "input_tokens", "output_tokens"):
                    try:
                        data["tiers"][t][k] = int(row.get(k) or 0)
                    except (TypeError, ValueError):
                        pass
        try:
            data["classifier_calls"] = int(saved.get("classifier_calls") or 0)
        except (TypeError, ValueError):
            pass
    return data


def _with_totals(data: Dict[str, Any]) -> Dict[str, Any]:
    tot_in = tot_out = calls = 0
    cost = opus_equiv = 0.0
    for t in TIERS:
        row = data["tiers"][t]
        row["cost_usd"] = round(_cost(t, row["input_tokens"], row["output_tokens"]), 6)
        tot_in += row["input_tokens"]
        tot_out += row["output_tokens"]
        calls += row["calls"]
        cost += row["cost_usd"]
        opus_equiv += _cost("opus", row["input_tokens"], row["output_tokens"])
    data["totals"] = {"calls": calls, "input_tokens": tot_in, "output_tokens": tot_out,
                      "tokens": tot_in + tot_out, "cost_usd": round(cost, 6),
                      "opus_equivalent_usd": round(opus_equiv, 6),
                      "saved_usd": round(max(opus_equiv - cost, 0.0), 6)}
    return data


def _record(tier: str, model: str, usage: Dict[str, int], classifier: bool = False,
            path: Optional[str] = None) -> Dict[str, Any]:
    path = path or USAGE_PATH
    with _usage_lock:
        data = _load_usage(path)
        row = data["tiers"][tier]
        row["calls"] += 1
        row["input_tokens"] += usage.get("input_tokens", 0)
        row["output_tokens"] += usage.get("output_tokens", 0)
        if classifier:
            data["classifier_calls"] += 1
        data["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        data = _with_totals(data)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, path)
        except OSError:
            pass
    totals = dict(data["totals"], calls_by_tier={t: data["tiers"][t]["calls"] for t in TIERS})
    _emit_tokens(tier, model, usage, totals, classifier)
    return data


def usage_summary(path: Optional[str] = None) -> Dict[str, Any]:
    with _usage_lock:
        return _with_totals(_load_usage(path or USAGE_PATH))


# ─── Model Pipeline view (workspace pane) ───────────────────────────

def _emit_switch(tier: str, model: str, reason: str) -> None:
    try:
        from modules import workspace
        workspace.emit_model_switch(tier, model, reason, TIER_COST[tier])
    except Exception:
        pass


def _emit_failure(tier: str, model: str, problem: str, retry_tier: Optional[str]) -> None:
    try:
        from modules import workspace
        workspace.emit_claude_failure(tier, model, problem, retry_tier)
    except Exception:
        pass


def _emit_tokens(tier: str, model: str, usage: Dict[str, int], totals: Dict[str, Any], classifier: bool) -> None:
    try:
        from modules import workspace
        workspace.emit_token_usage(tier, model, usage.get("input_tokens", 0), usage.get("output_tokens", 0),
                                   totals, classifier=classifier)
    except Exception:
        pass
