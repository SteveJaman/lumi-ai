"""
Lumi External SDK.

External apps connect to Lumi over websocket, register actions, push
context, and receive invocations. Protocol is modeled after Vedal's
Neuro SDK for familiarity.

    from sdk import LumiClient, action, ActionSpec

See `sdk/README.md` for the wire protocol spec.
"""

from sdk.protocol import (
    SDK_PROTOCOL_VERSION,
    ClientCommand,
    ServerCommand,
    Priority,
    ActionSpec,
    Session,
)
from sdk.actions import (
    action,
    param,
    merge_schemas,
    ActionBuilder,
)
from sdk.client import LumiClient
from sdk.errors import (
    SDKError,
    AuthError,
    ProtocolError,
    ActionError,
    ClientDisconnected,
)

__all__ = [
    "SDK_PROTOCOL_VERSION",
    "ClientCommand",
    "ServerCommand",
    "Priority",
    "ActionSpec",
    "Session",
    "action",
    "param",
    "merge_schemas",
    "ActionBuilder",
    "LumiClient",
    "SDKError",
    "AuthError",
    "ProtocolError",
    "ActionError",
    "ClientDisconnected",
]