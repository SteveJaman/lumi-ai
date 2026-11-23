"""PreToolUse hook: deny destructive shell commands, even in YOLO mode.

Covers run_command and run_in_terminal (and any tool with a "command" argument).
Patterns: rm -rf / rm -fr, del /s, rd|rmdir /s, format <drive>, mkfs, Remove-Item
-Recurse, diskpart, dd to a device, and shutdown/ format-volume style wipes.
"""

import re

_PATTERNS = [
    (r"\brm\s+(-[a-z]*r[a-z]*f[a-z]*|-[a-z]*f[a-z]*r[a-z]*|-r\s+-f|-f\s+-r|--recursive\s+--force|--force\s+--recursive)\b", "rm -rf"),
    (r"\bdel(ete)?\b[^\n]*\s/s\b", "del /s"),
    (r"\b(rd|rmdir)\b[^\n]*\s/s\b", "rmdir /s"),
    (r"(^|[\s;&|(])format(\.com)?\s+[a-z]:", "format <drive>"),
    (r"\bformat-volume\b", "Format-Volume"),
    (r"\bmkfs(\.\w+)?\b", "mkfs"),
    (r"\bremove-item\b[^\n]*-recurse", "Remove-Item -Recurse"),
    (r"\bdiskpart\b", "diskpart"),
    (r"\bdd\b[^\n]*\bof=/dev/", "dd to a device"),
    (r"\bcipher\s+/w\b", "cipher /w"),
]
_COMPILED = [(re.compile(p, re.I), label) for p, label in _PATTERNS]


def match(command):
    """The label of the first destructive pattern in `command`, or None."""
    text = str(command or "")
    for rx, label in _COMPILED:
        if rx.search(text):
            return label
    return None


def pre_tool_use(event):
    args = event.get("args") or {}
    command = args.get("command")
    if command is None:
        return None
    label = match(command)
    if label:
        return {"deny": True,
                "reason": (f"Blocked by hook block_rm_rf: the command matches the destructive "
                           f"pattern '{label}'. Destructive commands are never run by the agent. "
                           f"Choose a non-destructive approach or call task_fail.")}
    return None
