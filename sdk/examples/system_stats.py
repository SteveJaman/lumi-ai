"""
Reference SDK client — system statistics and a desktop notification.

    python -m sdk.examples.system_stats

Requires Lumi running and `pip install websockets psutil`.
"""

import asyncio
import os
import platform

try:
    import psutil
except ImportError:
    psutil = None

from sdk import LumiClient, action


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
    token_path = os.path.join("memories", "sdk_token.txt")
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