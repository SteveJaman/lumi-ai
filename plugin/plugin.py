"""
Lumi SDK reference plugin.

Two actions that no built-in tool and no shell command can replicate:

  - screenshot_primary_monitor()  — captures the primary display to a PNG
                                    (read capability, auto-approved)
  - log_note(text)                — appends a timestamped line to notes.txt
                                    (write capability, auto-approved)

Both were chosen deliberately to avoid vocabulary collisions with
existing tools and shell commands. Small-model tool selection routes
by token overlap, so an action named "read_clipboard" loses to
`run_command clip`, and an action named "open_notepad" loses to
`run_in_terminal`. The two actions below have no competitor.

Run from anywhere:
    python plugin/plugin.py

Requires Lumi running with the SDK module enabled, and:
    pip install mss
"""

import asyncio
import datetime
import os
import sys

# ----------------------------------------------------------------------
# Path bootstrap
# ----------------------------------------------------------------------
# The plugin lives inside Lumi-master/, so we add the workspace root to
# sys.path so `import sdk` resolves to Lumi's SDK package. If you later
# vendor a copy of sdk/ into a standalone project, delete this block.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    import mss
except ImportError:
    print("Missing dependency: pip install mss")
    raise

from sdk import LumiClient, action


# ----------------------------------------------------------------------
# Paths and config
# ----------------------------------------------------------------------
NOTES_PATH = os.path.join(_ROOT, "notes.txt")
SCREENSHOT_DIR = os.path.join(_ROOT, "logs", "screenshots")


class LumiPlugin(LumiClient):
    """Reference plugin. Subclass LumiClient, declare actions in setup()."""

    async def setup(self):
        """Called once after the SDK handshake. Register every action
        here — the plugin library sends them all in one batch."""

        # ─── Screenshot ───────────────────────────────────────────
        self.register_action(
            action(
                "screenshot_primary_monitor",
                "Capture the user's primary monitor and save it to a PNG "
                "file. Use this when the user asks for a screenshot or "
                "wants you to look at their screen.",
            )
                .capabilities("read")
                .build(),
            self.handle_screenshot,
        )

        # ─── Log a note ───────────────────────────────────────────
        self.register_action(
            action(
                "log_note",
                "Append a timestamped line to the user's notes file. Use "
                "this when the user wants to capture a thought, TODO, or "
                "reminder.",
            )
                .param(
                    "text", "string",
                    description="The note text",
                    required=True,
                )
                .capabilities("write")
                .build(),
            self.handle_log_note,
        )

        # ─── Capability summary ───────────────────────────────────
        # Free-text context that Lumi sees on every turn. Deliberately
        # does NOT name the tools — small models echo literal tool
        # names as prose when they appear in injected context.
        await self.send_context(
            "A local plugin is loaded. It can capture screenshots of the "
            "primary monitor and append short notes to a timestamped file.",
            silent=True,
        )

    # ──────────────────────────────────────────────────────────────
    # Handlers
    # ──────────────────────────────────────────────────────────────

    async def handle_screenshot(self, args):
        try:
            os.makedirs(SCREENSHOT_DIR, exist_ok=True)
            stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            path = os.path.join(SCREENSHOT_DIR, f"screen_{stamp}.png")
            with mss.mss() as sct:
                # monitors[0] is the virtual bounding box of every
                # monitor combined; monitors[1] is the primary.
                sct.shot(mon=1, output=path)
            return True, f"saved screenshot to {path}"
        except Exception as e:
            return False, f"screenshot failed: {e}"

    async def handle_log_note(self, args):
        text = str(args.get("text") or "").strip()
        if not text:
            return False, "no text provided"
        try:
            stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            line = f"[{stamp}] {text}\n"
            with open(NOTES_PATH, "a", encoding="utf-8") as f:
                f.write(line)
            return True, f"logged: {text}"
        except Exception as e:
            return False, f"failed to write note: {e}"


# ----------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------

async def _main():
    token_path = os.path.join(_ROOT, "memories", "sdk_token.txt")

    client = LumiPlugin(
        game="Lumi Plugin",
        token_path=token_path,
        on_lumi_message=lambda msg, role: print(f"[lumi] {msg}"),
    )

    try:
        await client.run()
    except KeyboardInterrupt:
        client.stop()
        print("\n[plugin] bye")


if __name__ == "__main__":
    asyncio.run(_main())