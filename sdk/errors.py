"""SDK exceptions — shared by server and client."""


class SDKError(Exception):
    """Base class for all SDK errors."""


class AuthError(SDKError):
    """Token missing, malformed, or rejected."""


class ProtocolError(SDKError):
    """A message did not match the wire format."""


class ActionError(SDKError):
    """An action invocation returned an error."""


class ClientDisconnected(SDKError):
    """The connected client went away mid-invocation."""