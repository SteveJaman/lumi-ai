"""
Agent tools: real, structured actions the model can call — as opposed to the
old approach of regex-scanning free text for [READ: ...] style tags. Every
tool call comes back from Ollama as a proper tool_calls object, so there's no
ambiguity between "the model used a tool" and "the model quoted some text
that looked like a tool call" (that ambiguity was the bug we fixed earlier).

Every filesystem tool is restricted to WORKSPACE_ROOT — nothing can read,
write, or list outside the project folder. write_file, run_command, and
run_in_terminal are still gated behind a confirmation prompt in prompter.py
before they're ever actually called.
"""

import os
import re
import shutil
import subprocess

from constants import WORKSPACE_ROOT

SKIP_DIRS = {"venv", ".venv", ".git", "__pycache__", "node_modules"}

PROTECTED_FILES = {
    "main.py",
    "constants.py",
    ".env",
    "Modelfile",
}

MAX_FILE_READ_CHARS = 40000
MAX_COMMAND_OUTPUT_CHARS = 4000
COMMAND_TIMEOUT_SECONDS = 30
MAX_SEARCH_MATCHES = 40

# Commands that are clearly Linux-only and will always fail on Windows.
# Rejected before the user sees a confirm prompt — the model has a strong
# Linux bias in its training data and reaches for these automatically.
_LINUX_ONLY_COMMAND = re.compile(
    r"^\s*(?:sudo\s+)?(?:"
    r"apt|apt-get|yum|dnf|pacman|systemctl|service|"
    r"ls|cat|grep|find|chmod|chown|ps|kill|pkill|"
    r"ifconfig|ip\s+addr|netstat|"
    r"which|whereis|touch|rm\b|mv\b|cp\b"
    r")\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------
# Tool schemas — this is what actually replaces the old bracket-tag
# system. The model sees these definitions and returns a structured
# tool_calls object; it no longer has to be told a text syntax to type out.
# ---------------------------------------------------------------
TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read the contents of a file inside the project workspace. "
                "Always use this before editing a file you haven't already "
                "seen in this conversation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to the file, relative to the workspace root, e.g. 'prompter.py' or 'modules/memory.py'.",
                    }
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Create a new file or overwrite an existing file's contents "
                "inside the project workspace. This always requires the "
                "user's explicit confirmation before it takes effect."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file, relative to the workspace root."},
                    "content": {"type": "string", "description": "The FULL new contents of the file, not a diff."},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List files and folders at a given path inside the project workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relative to the workspace root. Use '.' for the root."}
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": (
                "Search for a text string or regex pattern across files in "
                "the project workspace, like grep. Use this to find where "
                "something is defined or used before editing it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Text or regex pattern to search for."},
                    "path": {"type": "string", "description": "Subfolder to search in, relative to workspace root. Defaults to the whole workspace."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": (
                "Run a shell command inside the workspace and CAPTURE its "
                "output (stdout + stderr) back to you. Use this whenever "
                "you need to see what a command produced — running tests, "
                "checking output, debugging, verifying a script works. The "
                "result comes back to you so you can read it and continue. "
                "This always requires the user's explicit confirmation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The shell command to run."}
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_in_terminal",
            "description": (
                "Open a NEW visible terminal window and run a command in it. "
                "Use this ONLY when the user explicitly wants a window they "
                "can interact with — e.g. 'open a terminal', 'launch a GUI', "
                "'start a server', 'run it so I can see it'. Do NOT use this "
                "when the user asks for output or results — use run_command "
                "for that. Output is NOT captured back to you; the window "
                "stays open for the user. This always requires confirmation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to run in the new window, e.g. 'python hello_world.py'.",
                    }
                },
                "required": ["command"],
            },
        },
    },
]

# Tool names that must never run without the user explicitly saying yes.
CONFIRM_REQUIRED_TOOLS = {"write_file", "run_command", "run_in_terminal"}


class ToolError(Exception):
    pass


def _safe_path(relative_path):
    """Resolves a relative path against WORKSPACE_ROOT and refuses to leave it."""
    relative_path = (relative_path or "").strip().strip("'\"")
    relative_path = relative_path.lstrip("/\\")
    if not relative_path:
        relative_path = "."
    target = os.path.abspath(os.path.join(WORKSPACE_ROOT, relative_path))
    root = os.path.abspath(WORKSPACE_ROOT)
    if not (target == root or target.startswith(root + os.sep)):
        raise ToolError(f"Refusing to access '{relative_path}' — it's outside the project workspace.")
    return target


def read_file(path):
    target = _safe_path(path)
    if not os.path.isfile(target):
        raise ToolError(f"No such file: {path}")
    with open(target, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()
    truncated = len(content) > MAX_FILE_READ_CHARS
    if truncated:
        content = content[:MAX_FILE_READ_CHARS] + "\n... [truncated]"
    rel = os.path.relpath(target, WORKSPACE_ROOT)
    note = "\n(truncated — ask to see more if needed)" if truncated else ""
    return f"Contents of {rel}:\n```\n{content}\n```{note}"


def write_file(path, content):
    target = _safe_path(path)
    rel = os.path.relpath(target, WORKSPACE_ROOT).replace("\\", "/")

    if rel in PROTECTED_FILES:
        raise ToolError(f"Refusing to overwrite protected file: {rel}")

    existed = os.path.isfile(target)

    # Only make a backup when there's an existing file to back up.
    if existed:
        shutil.copy2(target, target + ".bak")

    os.makedirs(os.path.dirname(target) or WORKSPACE_ROOT, exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        f.write(content or "")

    action = "Updated" if existed else "Created"
    backup_note = f" Backup: {rel}.bak" if existed else ""
    return f"{action} {rel} ({len(content or '')} chars written).{backup_note}"


def list_dir(path="."):
    target = _safe_path(path)
    if not os.path.isdir(target):
        raise ToolError(f"No such directory: {path}")
    entries = []
    for name in sorted(os.listdir(target)):
        if name in SKIP_DIRS:
            continue
        full = os.path.join(target, name)
        marker = "[dir] " if os.path.isdir(full) else "      "
        entries.append(f"{marker}{name}")
    rel = os.path.relpath(target, WORKSPACE_ROOT) or "."
    if not entries:
        return f"{rel} is empty."
    return f"Contents of {rel}:\n" + "\n".join(entries)


def search_files(query, path="."):
    target = _safe_path(path)
    try:
        pattern = re.compile(query, re.IGNORECASE)
    except re.error:
        pattern = re.compile(re.escape(query), re.IGNORECASE)

    matches = []
    for root, dirs, files in os.walk(target):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for fname in files:
            fpath = os.path.join(root, fname)
            try:
                with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                    for i, line in enumerate(f, start=1):
                        if pattern.search(line):
                            rel = os.path.relpath(fpath, WORKSPACE_ROOT)
                            matches.append(f"{rel}:{i}: {line.strip()[:200]}")
                            if len(matches) >= MAX_SEARCH_MATCHES:
                                break
            except (UnicodeDecodeError, PermissionError, OSError):
                continue
            if len(matches) >= MAX_SEARCH_MATCHES:
                break
        if len(matches) >= MAX_SEARCH_MATCHES:
            break

    if not matches:
        return f"No matches for '{query}'."
    return f"Matches for '{query}':\n" + "\n".join(matches)


def run_command(command):
    if not command or not command.strip():
        return "No command given."

    # Reject Linux-only commands on Windows before anything else.
    if _LINUX_ONLY_COMMAND.match(command.strip()):
        return (
            "Error: this command is Linux-only and will fail on Windows. "
            "Use the Windows equivalent — 'dir' instead of 'ls', "
            "'type' instead of 'cat', 'tasklist' instead of 'ps', "
            "'Get-Service' instead of 'systemctl', 'del' instead of 'rm'. "
            "Do NOT use 'sudo' — Windows has no sudo."
        )

    try:
        result = subprocess.run(
            command,
            cwd=WORKSPACE_ROOT,
            shell=True,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return f"Command timed out after {COMMAND_TIMEOUT_SECONDS}s."
    except Exception as e:
        return f"Error running command: {e}"

    output = (result.stdout or "") + (result.stderr or "")
    if len(output) > MAX_COMMAND_OUTPUT_CHARS:
        output = output[:MAX_COMMAND_OUTPUT_CHARS] + "\n... [truncated]"
    return f"Exit code {result.returncode}\n{output.strip()}"


def run_in_terminal(command):
    """Spawns a new visible Windows terminal window running `command`,
    inside WORKSPACE_ROOT. Returns immediately — this does NOT capture
    output; the window stays open so the user can interact with it."""
    if not command or not command.strip():
        return "No command given."

    # Same Linux-only guard as run_command.
    if _LINUX_ONLY_COMMAND.match(command.strip()):
        return (
            "Error: this command is Linux-only and will fail on Windows. "
            "Use the Windows equivalent."
        )

    command = command.strip()

    try:
        # `start` is a cmd.exe builtin. The leading "" is the window-title
        # placeholder — without it, `start` treats the first quoted token
        # as a title and eats it. PowerShell -NoExit keeps the window
        # open after the command finishes so errors remain visible.
        subprocess.Popen(
            ["cmd", "/c", "start", "", "powershell", "-NoExit", "-Command", command],
            cwd=WORKSPACE_ROOT,
        )
        return (
            f"Opened a new terminal window in the workspace, running: {command}\n"
            f"(Output is not captured — the window stays open for the user to interact with.)"
        )
    except Exception as e:
        return f"Failed to open terminal window: {e}"


_DISPATCH = {
    "read_file": lambda args: read_file(args.get("path", ".")),
    "write_file": lambda args: write_file(args.get("path", ""), args.get("content", "")),
    "list_dir": lambda args: list_dir(args.get("path", ".")),
    "search_files": lambda args: search_files(args.get("query", ""), args.get("path", ".")),
    "run_command": lambda args: run_command(args.get("command", "")),
    "run_in_terminal": lambda args: run_in_terminal(args.get("command", "")),
}


def execute_tool(name, arguments):
    """Executes one of the tools above by name. Callers are responsible for
    confirmation gating on CONFIRM_REQUIRED_TOOLS before calling this."""
    fn = _DISPATCH.get(name)
    if not fn:
        return f"Unknown tool: {name}"
    try:
        return fn(arguments or {})
    except ToolError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error running {name}: {e}"