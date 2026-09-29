"""
Lumi SDK — reference client.

Demonstrates the full protocol:
  - connecting with auth token
  - registering actions
  - handling invoke requests
  - sending context that Lumi sees every turn
  - receiving Lumi's replies

Run with:
    python sdk_example.py

Requires Lumi to be running. The SDK module generates a token on
first boot and writes it to memories/sdk_token.txt.
"""

import asyncio
import json
import os
import platform

try:
    import psutil
except ImportError:
    psutil = None

try:
    import websockets
except ImportError:
    print("Missing dependency. Run: pip install websockets")
    raise

LUMI_HOST = "127.0.0.1"
LUMI_PORT = 5056
WORKSPACE = os.path.dirname(os.path.abspath(__file__))
TOKEN_PATH = os.path.join(WORKSPACE, "memories", "sdk_token.txt")


# ─── Action implementations ─────────────────────────────────────

def action_system_stats(arguments):
    """Return CPU, memory, and disk usage. Read-only."""
    if psutil is None:
        return {"success": False,
                "error": "psutil not installed. Run: pip install psutil"}
    try:
        cpu = psutil.cpu_percent(interval=0.3)
        mem = psutil.virtual_memory()
        root = "C:\\" if os.name == "nt" else "/"
        disk = psutil.disk_usage(root)
        return {
            "success": True,
            "result": (
                f"CPU {cpu:.1f}% | "
                f"RAM {mem.percent:.1f}% "
                f"({mem.used / (1024**3):.1f}/{mem.total / (1024**3):.1f} GB) | "
                f"Disk {disk.percent:.1f}%"
            ),
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def action_notify(arguments):
    """Show a desktop notification. Requires confirmation."""
    title = str(arguments.get("title") or "Lumi")
    message = str(arguments.get("message") or "")
    if not message:
        return {"success": False, "error": "message is required"}
    try:
        if os.name == "nt":
            print(f"\a[{title}] {message}")
        else:
            print(f"\a[{title}] {message}")
        return {"success": True, "result": f"notified: {title}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


ACTIONS = {
    "system_stats": {
        "impl": action_system_stats,
        "spec": {
            "name": "system_stats",
            "description": (
                "Return current CPU, memory, and disk usage on the "
                "user's machine."
            ),
            "parameters": {"type": "object", "properties": {}},
            "capabilities": ["read"],
            "requires_confirm": False,
        },
    },
    "notify": {
        "impl": action_notify,
        "spec": {
            "name": "notify",
            "description": "Show a desktop notification to the user.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string",
                              "description": "Notification title"},
                    "message": {"type": "string",
                                "description": "Notification body"},
                },
                "required": ["message"],
            },
            "capabilities": ["write"],
            "requires_confirm": True,
        },
    },
}


# ─── Client ─────────────────────────────────────────────────────

async def handle_invoke(msg, ws):
    inv_id = msg.get("invocation_id")
    action_name = msg.get("action_name")
    arguments = msg.get("arguments") or {}

    entry = ACTIONS.get(action_name)
    if entry is None:
        await ws.send(json.dumps({
            "type": "invoke_result",
            "invocation_id": inv_id,
            "success": False,
            "error": f"unknown action: {action_name}",
        }))
        return

    try:
        result = entry["impl"](arguments)
    except Exception as e:
        result = {"success": False, "error": str(e)}

    await ws.send(json.dumps({
        "type": "invoke_result",
        "invocation_id": inv_id,
        "success": result.get("success", True),
        "result": result.get("result"),
        "error": result.get("error"),
    }))
    print(f"[sdk] <- {action_name}({arguments}) -> {result}")


async def main():
    if not os.path.exists(TOKEN_PATH):
        print(f"Token file not found at {TOKEN_PATH}")
        print("Start Lumi first — the SDK module generates the token "
              "on first boot.")
        return
    with open(TOKEN_PATH, "r", encoding="utf-8") as f:
        token = f.read().strip()

    uri = f"ws://{LUMI_HOST}:{LUMI_PORT}/ws"
    print(f"Connecting to {uri}...")

    async with websockets.connect(uri) as ws:
        await ws.send(json.dumps({
            "type": "hello",
            "client_name": "sdk_example",
            "client_version": "0.1.0",
            "auth_token": token,
        }))
        welcome = json.loads(await ws.recv())
        print(f"Welcome: {welcome}")
        if welcome.get("type") != "welcome":
            print("Handshake failed.")
            return

        for name, entry in ACTIONS.items():
            await ws.send(json.dumps({
                "type": "register_action",
                "action": entry["spec"],
            }))
            ack = json.loads(await ws.recv())
            print(f"Register {name}: {ack.get('type')}")

        await ws.send(json.dumps({
            "type": "context",
            "text": f"Reference client is running on {platform.node()}.",
            "priority": 150,
        }))

        print("\nReady. Lumi can now call system_stats and notify.")
        print("Try in the web chat: 'what's my CPU at?' or 'notify me'.")
        print("Ctrl+C to exit.\n")

        try:
            async for raw in ws:
                msg = json.loads(raw)
                mtype = msg.get("type")
                if mtype == "invoke":
                    await handle_invoke(msg, ws)
                elif mtype == "lumi_message":
                    print(f"[lumi] {msg.get('content')}")
                elif mtype == "ping":
                    await ws.send(json.dumps({"type": "pong"}))
                elif mtype == "error":
                    print(f"[error] {msg.get('message')}")
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBye.")