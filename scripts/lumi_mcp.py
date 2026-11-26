"""
Lumi as a tool for Claude Desktop (and Claude Code): a small MCP server over stdio.

Claude Desktop starts this script. Claude then runs on your Claude plan, and when
it wants Lumi it calls these tools, which forward to Lumi's bridge on this PC
(modules/claude_bridge.py). No Anthropic API key is used and no login is copied.

Tools
  ask_lumi(text)            say something to Lumi as you; returns her reply
  lumi_status()             whether she's thinking or speaking
  lumi_recent_chat(n)       the last few messages
  lumi_search_memory(query) search her memories

Setup (see docs/claude-bridge.md): run it with the lumi env python. Claude Desktop
config (%APPDATA%\\Claude\\claude_desktop_config.json) gets an entry under mcpServers
that starts this file. Standard library only.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = os.environ.get("LUMI_URL", "http://127.0.0.1:5000").rstrip("/")
TOKEN_PATH = os.path.join(ROOT, "data", "credentials", "claude_bridge.token")
PROTOCOL = "2024-11-05"
ASK_TIMEOUT = 200  # Lumi's own reply wait is 180 s


def _token() -> str:
    try:
        with open(TOKEN_PATH, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _call(method: str, path: str, body=None, timeout: float = 15):
    tok = _token()
    if not tok:
        raise RuntimeError("Lumi's bridge token isn't set up yet. Start Lumi once, then try again.")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method, headers={
        "Authorization": f"Bearer {tok}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read() or b"{}").get("error") or e.reason
        except Exception:
            msg = e.reason
        raise RuntimeError(f"Lumi said no ({e.code}): {msg}")
    except urllib.error.URLError:
        raise RuntimeError("Lumi isn't running on this PC (couldn't reach her web server).")


TOOLS = [
    {
        "name": "ask_lumi",
        "description": "Say something to Lumi, as the user, and get her reply. Use it to hand her a request or ask her a question.",
        "inputSchema": {"type": "object", "properties": {
            "text": {"type": "string", "description": "What to say to Lumi, written as the user would say it."}},
            "required": ["text"]},
    },
    {
        "name": "lumi_status",
        "description": "Check whether Lumi is thinking or speaking right now.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "lumi_recent_chat",
        "description": "Get the last few messages in Lumi's chat, for context.",
        "inputSchema": {"type": "object", "properties": {
            "n": {"type": "integer", "description": "How many messages, 1 to 50.", "default": 20}}},
    },
    {
        "name": "lumi_search_memory",
        "description": "Search Lumi's memories of the user, such as preferences and past events.",
        "inputSchema": {"type": "object", "properties": {
            "query": {"type": "string", "description": "What to look for."}},
            "required": ["query"]},
    },
]


def run_tool(name: str, args: dict) -> str:
    if name == "ask_lumi":
        text = str(args.get("text", "")).strip()
        if not text:
            raise ValueError("text is empty")
        out = _call("POST", "/api/bridge/chat", {"text": text}, timeout=ASK_TIMEOUT)
        return out.get("reply") or "(Lumi didn't reply)"
    if name == "lumi_status":
        return json.dumps(_call("GET", "/api/bridge/status"))
    if name == "lumi_recent_chat":
        n = max(1, min(int(args.get("n", 20) or 20), 50))
        rows = _call("GET", f"/api/bridge/history?n={n}").get("messages", [])
        return "\n".join(f"{m['role']}: {m['content']}" for m in rows) or "(no messages yet)"
    if name == "lumi_search_memory":
        from urllib.parse import quote
        q = str(args.get("query", "")).strip()
        rows = _call("GET", f"/api/bridge/memories?q={quote(q)}").get("memories", [])
        return "\n".join(f"- {m['fact']} ({m['when']})" for m in rows) or "(nothing found)"
    raise ValueError(f"unknown tool {name}")


def _reply(msg_id, result=None, error=None):
    out = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        out["error"] = error
    else:
        out["result"] = result
    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()


def handle(msg: dict):
    method = msg.get("method")
    msg_id = msg.get("id")
    if msg_id is None:            # notifications get no reply
        return
    if method == "initialize":
        return _reply(msg_id, {
            "protocolVersion": PROTOCOL,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "lumi", "version": "0.1.0"},
        })
    if method == "tools/list":
        return _reply(msg_id, {"tools": TOOLS})
    if method == "tools/call":
        params = msg.get("params") or {}
        try:
            text = run_tool(params.get("name", ""), params.get("arguments") or {})
            return _reply(msg_id, {"content": [{"type": "text", "text": text}], "isError": False})
        except Exception as e:  # tool errors go back to the model, not the protocol
            return _reply(msg_id, {"content": [{"type": "text", "text": str(e)}], "isError": True})
    if method == "ping":
        return _reply(msg_id, {})
    return _reply(msg_id, error={"code": -32601, "message": f"unknown method {method}"})


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        handle(msg)


if __name__ == "__main__":
    main()
