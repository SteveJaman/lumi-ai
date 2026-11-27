"""
Nearby places as agent tools (Phase 5). Read-only, so no approval gate; the tools only
read from Google Places when GOOGLE_MAPS_API_KEY is set (see modules/places.py).
"""

from __future__ import annotations

import asyncio

from modules import places

AGENT = "research"
CONFIRM_TOOLS: set = set()

TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "search_nearby_places",
        "description": "Find open places near the user, such as restaurants, cafes or stores, with ratings and distance.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "What to look for, e.g. 'ramen' or 'late-night cafe'."},
            "category": {"type": "string"},
            "open_now": {"type": "boolean", "description": "Default true."},
            "radius_meters": {"type": "integer", "description": "Default 8000."}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "get_place_details",
        "description": "Get hours, phone and website for one place from a search result id.",
        "parameters": {"type": "object", "properties": {
            "place_id": {"type": "string"}}, "required": ["place_id"]}}},
]


def available(ctx) -> bool:
    return places._config() is not None


async def run_tool(name: str, args, ctx) -> str:
    args = args if isinstance(args, dict) else {}
    loop = asyncio.get_running_loop()
    if name == "search_nearby_places":
        return await loop.run_in_executor(
            None, lambda: places.search_nearby_places(
                args.get("query", ""), args.get("category", ""),
                bool(args.get("open_now", True)), int(args.get("radius_meters") or 8000)))
    if name == "get_place_details":
        return await loop.run_in_executor(None, places.get_place_details, args.get("place_id", ""))
    return f"Error: unknown places tool {name}."
