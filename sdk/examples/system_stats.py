"""
Reference SDK client — system statistics and a desktop notification.

    python -m sdk.examples.system_stats

Requires Lumi running and `pip install websockets psutil`.

This file is a reference implementation. It deliberately does not
import from Lumi's internals — it only uses the standalone SDK
(sdk/client.py, sdk/protocol.py, sdk/actions.py, sdk/errors.py). If you
copy those four files into your own project, this file works there too.

Token discovery:
  1. env var LUMI_SDK_TOKEN (path to the token file)
  2. env var LUMI_SDK_TOKEN_PATH (alias)
  3. default: <workspace_root>/data/memories/sdk_token.txt

The default path only applies when this file runs inside the Lumi
repo. When running standalone, set LUMI_SDK_TOKEN to point at wherever
Lumi's sdk_token.txt lives.
"""

import asyncio
import os
import platform

try:
    import psutil
except ImportError:
    psutil = None

from sdk import LumiClient, action


def _default_token_path():
    """Best-effort default token location inside the Lumi workspace.
    Used only when the env var override is not set."""
    # sdk/examples/system_stats.py → sdk/examples → sdk → workspace root
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(os.path.dirname(here))
    return os.path.join(root, "data", "memories", "sdk_token.txt")


def _resolve_token_path():
    """Env override first, then the workspace default."""
    for var in ("LUMI_SDK_TOKEN", "LUMI_SDK_TOKEN_PATH"):
        val = os.environ.get(var)
        if val:
            return val
    return _default_token_path()


class SystemStatsClient(LumiClient):
    async def setup(self):
        self.register_action(
            action("system_stats",
                   "Return current CPU, memory, and disk usage on the user's machine.")
                .capabilities("read")
                .build(),
            self.handle_stats,
        )
        self.register_action(
            action("notify", "Show a desktop notification.")
                .param("title", "string", description="Notification title")
                .param("message", "string",
                       description="Notification body",
                       required=True)
                .capabilities("write")
                .requires_confirm()
                .build(),
            self.handle_notify,
        )

        await self.send_context(
            f"Reference SDK client running on {platform.node()}.",
            silent=True,
        )

    async def handle_stats(self, args):
        if psutil is None:
            return False, "psutil not installed (pip install psutil)"
        cpu = psutil.cpu_percent(interval=0.3)
        mem = psutil.virtual_memory()
        root = "C:\\" if os.name == "nt" else "/"
        disk = psutil.disk_usage(root)
        return True, (
            f"CPU {cpu:.1f}% | "
            f"RAM {mem.percent:.1f}% "
            f"({mem.used / (1024 ** 3):.1f}/{mem.total / (1024 ** 3):.1f} GB) | "
            f"Disk {disk.percent:.1f}%"
        )

    async def handle_notify(self, args):
        title = args.get("title") or "Lumi"
        body = args.get("message") or ""
        print(f"\a[{title}] {body}")
        return True, f"notified: {title}"


async def _main():
    token_path = _resolve_token_path()

    if not os.path.isfile(token_path):
        print(f"[sdk] token not found at {token_path}")
        print(f"[sdk] set LUMI_SDK_TOKEN=/path/to/sdk_token.txt "
              f"to override.")
        return

    client = SystemStatsClient(
        game="System Stats",
        token_path=token_path,
        on_lumi_message=lambda msg, role: print(f"[lumi] {msg}"),
    )
    try:
        await client.run()
    except KeyboardInterrupt:
        client.stop()
        print("\n[sdk] bye")


if __name__ == "__main__":
    asyncio.run(_main())