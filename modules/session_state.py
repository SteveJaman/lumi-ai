"""
Session state persistence for Lumi.

At the end of a session, asks the LLM to summarize the recent conversation
into a small JSON blob and writes it to memories/session_state.json. On the
next startup, Prompter reads it and injects it into the first few turns so
Lumi can open with continuity instead of a blank slate.

Phase 10.1 note: This module is host-agnostic. It uses generic "User" /
"Lumi" role labels in transcripts and refers to "the user" in injected
continuity text — never a hardcoded name. No sweep needed here for the
identity refactor. Per-user isolation (Phase 10.15) will later split the
state file per user_id, but that is out of scope for 10.1.
"""

import os
import json
import datetime
import re
import requests

from constants import LLM_ENDPOINT, MODEL_NAME, FAST_MODEL_NAME
from modules.llm_providers import chat_sync, LLMResponse

SESSION_STATE_PATH = os.path.join("memories", "session_state.json")

SUMMARY_PROMPT = """Summarize the conversation session below as strict JSON with exactly these keys:

{{
  "current_focus": "one short sentence about what the user was working on, OR null if the conversation was too short or purely casual",
  "unresolved": "anything left hanging, OR null",
  "running_bits": ["array of 0-3 short recurring jokes, topics, or themes, or empty array"]
}}

If the conversation was just greetings or small talk with no substantive topic, set current_focus to null and running_bits to an empty array. Do not invent a topic.

Respond ONLY with the JSON object. No prose. No markdown fences. No explanations.

CONVERSATION:
{transcript}
"""


class SessionState:
    def __init__(self, path=SESSION_STATE_PATH):
        self.path = path
        self.data = self._load()

    def _load(self):
        if not os.path.exists(self.path):
            return None
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"\033[90mSESSION: Could not load previous state: {e}\033[0m")
            return None

    def save(self, state_dict):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(state_dict, f, indent=2, ensure_ascii=False)
            self.data = state_dict
            return True
        except Exception as e:
            print(f"\033[91mSESSION: Failed to write state file: {e}\033[0m")
            return False

    def summarize_session(self, history, mood_at_end="default"):
        """Ask the LLM to summarize the session, then build the state dict.
        Returns the state dict or None on failure."""
        recent = history[-20:]
        transcript_lines = []
        for m in recent:
            role = m.get("role")
            content = (m.get("content") or "").strip()
            if not content:
                continue
            if role == "user":
                transcript_lines.append(f"User: {content}")
            elif role == "assistant":
                transcript_lines.append(f"Lumi: {content}")

        if len(transcript_lines) < 2:
            return None

        transcript = "\n".join(transcript_lines)
        prompt = SUMMARY_PROMPT.format(transcript=transcript)

        # Summarization is a structured extraction task. The fast model
        # handles it fine and frees the main model for user-facing turns.
        try:
            resp: LLMResponse = chat_sync(
                FAST_MODEL_NAME,
                [{"role": "user", "content": prompt}],
                keep_alive="30s",
                options={"temperature": 0.3, "num_ctx": 8192},
            )
            raw = (resp.content or "").strip()
        except Exception as e:
            print(f"\033[90mSESSION: Summary call failed: {e}\033[0m")
            return None

        # Strip code fences if the model wrapped it
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", raw, re.DOTALL)
            if not match:
                return None
            try:
                parsed = json.loads(match.group(0))
            except json.JSONDecodeError:
                return None

        state = {
            "last_session": datetime.datetime.now().isoformat(timespec="seconds"),
            "current_focus": (parsed.get("current_focus") or None),
            "unresolved": (parsed.get("unresolved") or None),
            "running_bits": parsed.get("running_bits") or [],
            "mood_at_end": mood_at_end,
        }
        # Trim whitespace on strings but keep None
        if isinstance(state["current_focus"], str):
            state["current_focus"] = state["current_focus"].strip() or None
        if isinstance(state["unresolved"], str):
            state["unresolved"] = state["unresolved"].strip() or None

        return state

    def format_injection(self):
        """Build a system prompt injection from the saved state."""
        if not self.data:
            return None

        parts = ["[Continuity — Last Session]"]

        last = self.data.get("last_session")
        if last:
            try:
                dt = datetime.datetime.fromisoformat(last)
                delta = datetime.datetime.now() - dt
                if delta.days == 0:
                    hours = delta.seconds // 3600
                    if hours == 0:
                        mins = max(1, delta.seconds // 60)
                        when = f"{mins} minute{'s' if mins != 1 else ''} ago"
                    else:
                        when = f"{hours} hour{'s' if hours != 1 else ''} ago"
                elif delta.days == 1:
                    when = "yesterday"
                else:
                    when = f"{delta.days} days ago"
                parts.append(f"You last spoke with the user {when}.")
            except Exception:
                pass

        focus = self.data.get("current_focus")
        if focus:
            parts.append(f"What they were focused on: {focus}")

        unresolved = self.data.get("unresolved")
        if unresolved:
            parts.append(f"Left hanging from last time: {unresolved}")

        bits = self.data.get("running_bits") or []
        if bits:
            parts.append(f"Recurring topics/jokes: {', '.join(bits)}")

        mood = self.data.get("mood_at_end")
        if mood and mood != "default":
            parts.append(f"Your mood at the end of that session: {mood}")

        parts.append(
            "Reference these details naturally if they come up in the conversation. "
            "Do NOT announce them upfront or list them mechanically. "
            "Only mention something from this list when it's contextually relevant."
        )

        return "\n".join(parts)