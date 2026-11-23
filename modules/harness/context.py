"""Context management for the agent harness: tool-output truncation and compaction.

Truncation runs first, on every tool result before it enters history: at most
MAX_TOOL_CHARS characters (run_command keeps only its last RUN_COMMAND_TAIL_LINES
lines first), with the full output saved under data/tasks/tool_outputs/.

Compaction runs before each model call: when the estimated history size reaches
COMPACT_THRESHOLD of the model's context window, the full transcript is archived
to data/tasks/transcripts/<session>.jsonl first, then older turns are replaced by
a structured summary and LUMI.md's rules are re-injected.
"""

import json
import os
import re
import time

from modules.harness import config

MAX_TOOL_CHARS = 4000
RUN_COMMAND_TAIL_LINES = 200
KEEP_RECENT_MESSAGES = 6          # newest messages kept verbatim after compaction
TARGET_AFTER_COMPACTION = 0.6     # of the compaction limit, so it doesn't re-fire every turn
SUMMARY_TAG = "[HARNESS CONTEXT SUMMARY]"
RULES_TAG = "[LUMI.md RULES]"


# ===============================================================
# Token estimate
# ===============================================================
def estimate_tokens(messages):
    """Rough token count for a message list (≈4 chars per token, plus overhead)."""
    total = 0
    for m in messages:
        total += 4
        c = m.get("content")
        total += len(c if isinstance(c, str) else json.dumps(c, default=str)) // 4
        if m.get("tool_calls"):
            total += len(json.dumps(m["tool_calls"], default=str)) // 4
    return total


# ===============================================================
# Truncation
# ===============================================================
class ToolOutputStore:
    """Saves full tool outputs per session and truncates what the model sees."""

    def __init__(self, session_id, base_dir=None):
        self.session_id = session_id
        self.dir = os.path.join(base_dir or os.path.join(config.TASKS_DIR, "tool_outputs"), session_id)
        self._n = 0

    def _save(self, tool, text):
        self._n += 1
        os.makedirs(self.dir, exist_ok=True)
        safe = re.sub(r"[^\w.-]", "_", tool or "tool")
        path = os.path.join(self.dir, f"{safe}_{self._n}.log")
        with open(path, "w", encoding="utf-8", errors="replace") as f:
            f.write(text)
        return path

    def truncate(self, tool, text):
        """Return (text_for_history, saved_path_or_None)."""
        text = "" if text is None else str(text)
        out = text
        if tool == "run_command":
            lines = out.splitlines()
            if len(lines) > RUN_COMMAND_TAIL_LINES:
                out = "\n".join(lines[-RUN_COMMAND_TAIL_LINES:])
        if len(out) > MAX_TOOL_CHARS:
            out = out[:MAX_TOOL_CHARS] if tool != "run_command" else out[-MAX_TOOL_CHARS:]
        if out == text:
            return text, None
        path = self._save(tool, text)
        omitted = len(text) - len(out)
        try:
            rel = os.path.relpath(path, config.WORKSPACE_ROOT)
        except ValueError:               # different drive on Windows
            rel = path
        return f"{out}\n[truncated; {omitted} chars omitted]\n[full output: {rel}]", path


def truncate_text(text, limit=MAX_TOOL_CHARS):
    """Plain truncation with the standard marker (no file)."""
    text = str(text or "")
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n[truncated; {len(text) - limit} chars omitted]"


# ===============================================================
# Compaction
# ===============================================================
def rules_message(rules=None):
    rules = config.rules_text() if rules is None else rules
    return {"role": "system", "content": f"{RULES_TAG}\n{rules}".strip()}


def archive(messages, session_id, transcripts_dir=None, reason="compaction"):
    """Append the full transcript to transcripts/<session>.jsonl. Returns the path."""
    d = transcripts_dir or os.path.join(config.TASKS_DIR, "transcripts")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{session_id}.jsonl")
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"type": "archive", "reason": reason, "time": time.time(),
                            "message_count": len(messages)}) + "\n")
        for m in messages:
            f.write(json.dumps(m, ensure_ascii=False, default=str) + "\n")
        f.flush()
        os.fsync(f.fileno())
    return path


def _summarize(older, current_task, max_chars=None):
    decisions, questions, tool_outputs = [], [], []
    for m in older:
        role = m.get("role")
        content = m.get("content") if isinstance(m.get("content"), str) else ""
        if role == "assistant":
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function", {}) or {}
                args = fn.get("arguments")
                try:
                    a = json.dumps(args, ensure_ascii=False, default=str)
                except (TypeError, ValueError):
                    a = str(args)
                decisions.append(f"called {fn.get('name')}({a[:160]})")
            text = content.strip()
            if text:
                for line in text.splitlines():
                    line = line.strip()
                    if line.endswith("?"):
                        questions.append(line[:200])
                decisions.append(f"said: {text[:200]}")
        elif role == "tool":
            tool_outputs.append((m.get("name") or "tool", content))
        elif role == "user" and content and not content.startswith("[System"):
            decisions.append(f"user/harness note: {content[:200]}")

    def build(n_decisions, out_chars):
        lines = [SUMMARY_TAG,
                 "Older turns were compacted. The full transcript is archived; the task store is the source of truth."]
        if current_task:
            lines.append(f"Current task: #{current_task.get('id')} {current_task.get('description')}"
                         f" (acceptance: {current_task.get('acceptance_criteria') or 'n/a'})")
        else:
            lines.append("Current task: none in progress — call task_next.")
        lines.append("Decisions so far:")
        lines += [f"- {d}" for d in (decisions[-n_decisions:] if n_decisions else [])] or ["- (none)"]
        lines.append("Open questions:")
        lines += [f"- {q}" for q in questions[-5:]] or ["- (none)"]
        lines.append("Last 3 tool outputs:")
        for name, out in tool_outputs[-3:]:
            lines.append(f"--- {name} ---\n{truncate_text(out, out_chars)}")
        if not tool_outputs:
            lines.append("- (none)")
        return "\n".join(lines)

    # Shrink to the budget: fewer decisions and shorter tool outputs first.
    n, out_chars = 25, 1500
    text = build(n, out_chars)
    while max_chars and len(text) > max_chars and (n > 3 or out_chars > 200):
        n = max(3, n // 2)
        out_chars = max(200, out_chars // 2)
        text = build(n, out_chars)
    if max_chars and len(text) > max_chars:
        text = truncate_text(text, max_chars)
    return text


class Compactor:
    def __init__(self, session_id, num_ctx=None, threshold=config.DEFAULT_COMPACT_THRESHOLD,
                 transcripts_dir=None, rules=None, keep_recent=KEEP_RECENT_MESSAGES):
        self.session_id = session_id
        self.num_ctx = num_ctx or config.num_ctx()
        self.threshold = threshold
        self.transcripts_dir = transcripts_dir
        self.rules = rules
        self.keep_recent = keep_recent
        self.compactions = 0
        self.last_archive = None

    def limit(self):
        return int(self.num_ctx * self.threshold)

    def needs_compaction(self, messages):
        return estimate_tokens(messages) >= self.limit()

    def compact(self, messages, current_task=None):
        """Archive, then return a compacted message list:
        [head system prompt, summary, LUMI.md rules, recent messages...]."""
        self.last_archive = archive(messages, self.session_id, self.transcripts_dir)
        head = [m for m in messages[:1] if m.get("role") == "system"]
        body = messages[len(head):]
        # Drop earlier summaries/rules: they are regenerated.
        body = [m for m in body if not (m.get("role") == "system" and isinstance(m.get("content"), str)
                                        and m["content"].startswith((SUMMARY_TAG, RULES_TAG)))]
        cut = max(0, len(body) - self.keep_recent)
        # Never start the kept tail on a tool message whose call was cut off.
        while cut < len(body) and body[cut].get("role") == "tool":
            cut += 1
        older, recent = body[:cut], body[cut:]

        # Aim well under the trigger so compaction doesn't fire again next turn.
        target = int(self.limit() * TARGET_AFTER_COMPACTION)
        rules = rules_message(self.rules)
        fixed = estimate_tokens(head + [rules])
        if fixed > target // 2 and head:                  # an oversized system prompt
            keep_chars = max(2000, (target // 2 - estimate_tokens([rules])) * 4)
            head = [dict(head[0], content=truncate_text(head[0].get("content"), keep_chars))]
            fixed = estimate_tokens(head + [rules])
        summary_chars = max(800, int((target - fixed) * 0.5 * 4))
        summary = {"role": "system", "content": _summarize(older, current_task, summary_chars)}

        remaining = target - fixed - estimate_tokens([summary])
        if estimate_tokens(recent) > remaining and recent:
            per_msg = max(200, (remaining * 4) // len(recent))
            recent = [dict(m, content=truncate_text(m.get("content"), per_msg))
                      if isinstance(m.get("content"), str) else m for m in recent]
        # Still over: drop the oldest kept messages (never leaving an orphaned tool result).
        while recent and estimate_tokens(recent) > max(remaining, 0):
            recent.pop(0)
            while recent and recent[0].get("role") == "tool":
                recent.pop(0)

        out = head + [summary, rules] + recent
        self.compactions += 1
        return out
