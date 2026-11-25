"""
Hand a hard task to Claude Code when the Claude API can't take it.

Normal turns run on local Ollama. A hard (opus-tier) turn would escalate to the
Claude router while Cloud Escalation is on; when that can't happen (no
ANTHROPIC_API_KEY, or the API refused the account, e.g. no credit) and the turn
is a coding or reasoning task, the prompter asks you whether to hand it to
Claude Code instead. Claude Code is the official CLI running on your own Claude
login (modules/claude_code.py: read-only tools, Lumi's private files denied).
Decline, and the turn is answered locally as usual.

    offer_reason(tier, text) -> str | None   why to offer the handoff, or None
    looks_like_task(text) -> bool            coding / reasoning work, not chat
    wants_edits(text) -> bool                the message asks for changes to files
    modes_to_offer(text) -> [mode, ...]      edit first (then read-only) when it does
    build_task(text) -> str                  what Claude Code receives
    format_reply(answer, mode) -> str        what Lumi shows in the chat
    SPOKEN[mode]                             the one line she says out loud

Approvals are approve / decline everywhere (web, island, terminal), so the edit
option is a second prompt rather than a third button: a request for changes is
offered with edits first; decline it and you're asked about read-only.
"""

from __future__ import annotations

import re
from typing import Optional

from modules import claude_code, claude_router, hybrid_mode

MODE = "read_only"
EDIT_MODE = "edit"
MODEL = "opus"                     # Claude Code's own alias; claude_code.run falls back to its default
MIN_TASK_CHARS = 25

_TASK_RE = re.compile(
    r"\b(code|function|class|script|implement|refactor|debug|bug|error|exception|traceback|stack trace|"
    r"fix|test|regex|sql|query|api|endpoint|algorithm|complexity|architect(ure)?|design|schema|optimi[sz]e|"
    r"memory leak|race condition|deadlock|security|prove|proof|derive|analy[sz]e|explain why|step by step|"
    r"python|javascript|typescript|rust|java|c\+\+|c#|golang|repo|module|file|compile|build)\b", re.I)


_EDIT_RE = re.compile(
    r"\b(refactor|fix|change|implement|add|write|update|rename|remove|delete|edit|create|modify|rewrite|"
    r"patch|apply|replace|move|convert|migrate|clean ?up)\b", re.I)

# What Lumi says out loud after a handoff. The full answer (often long, with code)
# stays in the chat: reading it all through the voice would take minutes and,
# with Fish Audio, spend voice credit.
SPOKEN = {
    MODE: "I handed that to Claude Code. It's done, and the full answer is in the chat.",
    EDIT_MODE: "I handed that to Claude Code and let it make the changes. The details are in the chat.",
}

_REDUNDANT_RE = re.compile(r"\.?\s*Hard tasks are offered to Claude Code instead\.?\s*$", re.I)


def wants_edits(text: str) -> bool:
    return bool(_EDIT_RE.search(text or ""))


def modes_to_offer(text: str) -> list:
    """Edit mode first for a request that changes things, then read-only."""
    return [EDIT_MODE, MODE] if wants_edits(text) else [MODE]


def clean_reason(reason: str) -> str:
    """The reason without the "offered to Claude Code" tail (the prompt says that itself)."""
    return _REDUNDANT_RE.sub("", str(reason or "")).strip().rstrip(".")


def looks_like_task(text: str) -> bool:
    t = (text or "").strip()
    if len(t) < MIN_TASK_CHARS:
        return False
    return "```" in t or bool(_TASK_RE.search(t))


def offer_reason(tier: Optional[str], text: str) -> Optional[str]:
    """Why this turn should be offered to Claude Code, or None to stay local.
    Offered only when it would have escalated to Claude, can't, Claude Code is
    installed, and the message is real work rather than chat."""
    blocked = hybrid_mode.escalation_blocked(tier)
    if not blocked or not claude_code.available() or not looks_like_task(text):
        return None
    return clean_reason(blocked) or "the Claude API isn't available"


def build_task(text: str) -> str:
    """The user's message as Claude Code's task, within its size limit."""
    t = (text or "").strip()
    limit = claude_code.MAX_TASK_CHARS
    if len(t) > limit:
        t = t[: limit - 40].rstrip() + "\n[message shortened to fit]"
    return t


def format_reply(answer: str, mode: str = MODE) -> str:
    answer = (answer or "").strip() or "(Claude Code finished without an answer.)"
    how = "with permission to edit files" if mode == EDIT_MODE else "read-only"
    return f"I handed that one to Claude Code ({how}). Here's what it came back with:\n\n{answer}"


def api_reason() -> str:
    """Short reason for logs and the Models tab."""
    ok, why = claude_router.api_status()
    return "" if ok else why
