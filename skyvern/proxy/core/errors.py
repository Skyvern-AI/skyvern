"""Shared error taxonomy for upstream browser connections.

Pure exception classes only: adapters translate transport- and vendor-specific
failures into these categories so the core and its callers never branch on
framework exception types. Messages must never contain raw upstream URLs —
their paths and query strings can carry session tokens.
"""

from __future__ import annotations


class UpstreamConnectError(Exception):
    """Establishing a connection to the upstream browser failed."""


class TransientConnectionError(UpstreamConnectError):
    """Network-level failure worth retrying: refused, reset, timeout, DNS."""


class ProtocolConfigurationError(UpstreamConnectError):
    """The endpoint is reachable but is not a usable CDP websocket endpoint."""


class LaunchEnvironmentError(UpstreamConnectError):
    """A locally launched browser could not start in this environment."""


class LaunchTimeoutError(UpstreamConnectError):
    """A locally launched browser started but never became connectable in time."""


class VendorRateLimitError(UpstreamConnectError):
    """A hosted-browser provider rejected the connection due to rate limiting."""


class VendorAuthError(UpstreamConnectError):
    """A hosted-browser provider rejected the credentials attached by the proxy."""
