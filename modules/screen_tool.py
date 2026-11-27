"""
The screen look as an agent tool (Phase 5). The tool is in CONFIRM_TOOLS, so every
call asks you for approval first (the same gate as write_file and run_command).
The screen is only read when you say yes.
"""

from __future__ import annotations

import asyncio

from modules import screen_context

AGENT = "operate"
TOOL_NAME = "capture_screen_context"
CONFIRM_TOOLS = {TOOL_NAME}

TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": TOOL_NAME,
        "description": "Look at the user's screen once and describe it, with the question they asked, if any. Needs their approval.",
        "parameters": {"type": "object", "properties": {
            "question": {"type": "string", "description": "What to look for, e.g. 'why is this error here?'"}}}}},
]


def available(ctx) -> bool:
    return True


async def run_tool(name: str, args, ctx) -> str:
    if name != TOOL_NAME:
        return f"Error: unknown screen tool {name}."
    args = args if isinstance(args, dict) else {}
    question = str(args.get("question") or "")
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(None, screen_context.capture_screen_context, question)
    except Exception as e:
        return f"Error: couldn't look at the screen ({type(e).__name__})."
