"""
Action declaration helpers.

    from sdk import action

    walk = (
        action("walk", "Walk to a coordinate.")
            .param("x", "number", required=True)
            .param("z", "number", required=True)
            .param("y", "number", description="Optional; surface if omitted.")
            .capabilities("write")
            .build()
    )
"""

from __future__ import annotations

from typing import Any, Dict, List

from sdk.protocol import ActionSpec


def param(
    name: str,
    type_: str,
    *,
    description: str = "",
    required: bool = False,
    enum: List[Any] | None = None,
    default: Any = None,
    **extra: Any,
) -> Dict[str, Any]:
    """Build a single-property JSON schema."""
    prop: Dict[str, Any] = {"type": type_}
    if description:
        prop["description"] = description
    if enum is not None:
        prop["enum"] = enum
    if default is not None:
        prop["default"] = default
    prop.update(extra)
    return {
        "type": "object",
        "properties": {name: prop},
        "required": [name] if required else [],
    }


def merge_schemas(*schemas: Dict[str, Any]) -> Dict[str, Any]:
    """Combine several `param(...)` results into one object schema.
    `required` arrays are unioned and deduplicated."""
    props: Dict[str, Any] = {}
    required: List[str] = []
    for s in schemas:
        if not s:
            continue
        props.update(s.get("properties", {}))
        for r in s.get("required", []):
            if r not in required:
                required.append(r)
    out: Dict[str, Any] = {"type": "object", "properties": props}
    if required:
        out["required"] = required
    return out


class ActionBuilder:
    def __init__(self, name: str, description: str):
        self._name = name
        self._description = description
        self._params: List[Dict[str, Any]] = []
        self._caps: List[str] = ["read"]
        self._confirm = False

    def param(self, *args, **kwargs) -> "ActionBuilder":
        self._params.append(param(*args, **kwargs))
        return self

    def capabilities(self, *caps: str) -> "ActionBuilder":
        self._caps = list(caps)
        return self

    def requires_confirm(self, value: bool = True) -> "ActionBuilder":
        self._confirm = value
        return self

    def build(self) -> ActionSpec:
        schema = (
            merge_schemas(*self._params)
            if self._params
            else {"type": "object", "properties": {}}
        )
        return ActionSpec(
            name=self._name,
            description=self._description,
            schema=schema,
            capabilities=self._caps,
            requires_confirm=self._confirm,
        )


def action(name: str, description: str) -> ActionBuilder:
    """Start a fluent action declaration."""
    return ActionBuilder(name, description)