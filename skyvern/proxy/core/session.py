from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum


def _require_non_empty(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


class UpstreamClosedError(Exception):
    """Raised by upstream connections when the browser side is gone."""


@dataclass(frozen=True)
class Principal:
    """Authenticated caller identity; opaque to the core beyond these fields."""

    principal_id: str
    organization_id: str | None = None

    def __post_init__(self) -> None:
        _require_non_empty(self.principal_id, "principal_id")
        if self.organization_id is not None:
            _require_non_empty(self.organization_id, "organization_id")


class SessionResolutionStatus(Enum):
    ACTIVE = "active"
    UNKNOWN = "unknown"
    PENDING = "pending"
    CLOSED = "closed"
    EXPIRED = "expired"


@dataclass(frozen=True)
class ResolvedSession:
    """Connect-time routing for one session: which adapter dials which endpoint.

    upstream_ws_url and connect_headers are excluded from repr — the URL's path
    and query can carry session tokens and headers can carry credentials, so
    neither may reach logs or clients.
    """

    session_id: str
    upstream_adapter: str
    upstream_ws_url: str = field(repr=False)
    organization_id: str | None = None
    connect_headers: Mapping[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        _require_non_empty(self.session_id, "session_id")
        _require_non_empty(self.upstream_adapter, "upstream_adapter")
        _require_non_empty(self.upstream_ws_url, "upstream_ws_url")
        if self.organization_id is not None:
            _require_non_empty(self.organization_id, "organization_id")
        if not isinstance(self.connect_headers, Mapping):
            raise ValueError("connect_headers must be a mapping")


@dataclass(frozen=True)
class SessionResolution:
    """Outcome of resolving a session id; `session` is set exactly when ACTIVE.

    Negative statuses are explicit so callers reject the client before any
    upstream dial ever happens. organization_id names the owning organization
    when the registry knows it — including on negatives, so callers can hide
    lifecycle state from non-owners. expires_in_seconds is an optional hint of
    how long an ACTIVE resolution stays valid (caches must not serve it longer).
    """

    status: SessionResolutionStatus
    session: ResolvedSession | None = None
    organization_id: str | None = None
    expires_in_seconds: float | None = None

    def __post_init__(self) -> None:
        if (self.status is SessionResolutionStatus.ACTIVE) != (self.session is not None):
            raise ValueError("session must be set exactly when status is ACTIVE")
        if self.session is not None and self.organization_id != self.session.organization_id:
            raise ValueError("organization_id must match the resolved session's organization")
        if self.expires_in_seconds is not None:
            if self.status is not SessionResolutionStatus.ACTIVE:
                raise ValueError("expires_in_seconds is only valid on ACTIVE resolutions")
            if self.expires_in_seconds <= 0:
                raise ValueError("expires_in_seconds must be positive")

    @classmethod
    def active(cls, session: ResolvedSession, expires_in_seconds: float | None = None) -> SessionResolution:
        return cls(SessionResolutionStatus.ACTIVE, session, session.organization_id, expires_in_seconds)

    @classmethod
    def unknown(cls) -> SessionResolution:
        return cls(SessionResolutionStatus.UNKNOWN)

    @classmethod
    def pending(cls, organization_id: str | None = None) -> SessionResolution:
        """The session exists but has not published a dialable upstream yet."""
        return cls(SessionResolutionStatus.PENDING, organization_id=organization_id)

    @classmethod
    def closed(cls, organization_id: str | None = None) -> SessionResolution:
        return cls(SessionResolutionStatus.CLOSED, organization_id=organization_id)

    @classmethod
    def expired(cls, organization_id: str | None = None) -> SessionResolution:
        return cls(SessionResolutionStatus.EXPIRED, organization_id=organization_id)


def principal_owns_resolution(principal: Principal, resolution: SessionResolution) -> bool:
    """The org-vs-session authz rule shared by every AuthPort adapter.

    False for both an unknown session and one owned by another organization, so
    callers emit a single uniform rejection: a foreign session is
    indistinguishable from a nonexistent one, leaking neither existence nor
    lifecycle state across organizations. A resolution with no owning
    organization is dev-only and open to any principal.
    """
    if resolution.status is SessionResolutionStatus.UNKNOWN:
        return False
    owner = resolution.organization_id
    return owner is None or principal.organization_id == owner


@dataclass
class ProxySession:
    """Routable browser session state owned by the proxy core.

    upstream_ws_url and connect_headers are excluded from repr for the same
    reason as on ResolvedSession: they can carry session tokens and credentials.
    """

    session_id: str
    upstream_ws_url: str = field(repr=False)
    principal: Principal | None = None
    attached_cdp_session_ids: set[str] = field(default_factory=set)
    connect_headers: Mapping[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        _require_non_empty(self.session_id, "session_id")
        _require_non_empty(self.upstream_ws_url, "upstream_ws_url")
        if not isinstance(self.attached_cdp_session_ids, set):
            raise ValueError("attached_cdp_session_ids must be a set")
        for cdp_session_id in self.attached_cdp_session_ids:
            _require_non_empty(cdp_session_id, "cdp_session_id")
        if not isinstance(self.connect_headers, Mapping):
            raise ValueError("connect_headers must be a mapping")

    def allows_principal(self, principal: Principal) -> bool:
        return self.principal is None or self.principal == principal

    def attach_cdp_session(self, cdp_session_id: str) -> bool:
        _require_non_empty(cdp_session_id, "cdp_session_id")
        if cdp_session_id in self.attached_cdp_session_ids:
            return False
        self.attached_cdp_session_ids.add(cdp_session_id)
        return True

    def detach_cdp_session(self, cdp_session_id: str) -> bool:
        _require_non_empty(cdp_session_id, "cdp_session_id")
        if cdp_session_id not in self.attached_cdp_session_ids:
            return False
        self.attached_cdp_session_ids.remove(cdp_session_id)
        return True

    def has_cdp_session(self, cdp_session_id: str) -> bool:
        _require_non_empty(cdp_session_id, "cdp_session_id")
        return cdp_session_id in self.attached_cdp_session_ids
