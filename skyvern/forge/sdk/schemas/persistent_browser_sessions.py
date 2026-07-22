from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_validator

from skyvern.forge.sdk.db.utils import deserialize_proxy_location
from skyvern.schemas.proxy_pinning import validate_proxy_session_id
from skyvern.schemas.runs import ProxyLocationInput


class PersistentBrowserSessionStatus(StrEnum):
    created = "created"
    running = "running"
    failed = "failed"
    completed = "completed"
    timeout = "timeout"
    retry = "retry"


FINAL_STATUSES = (
    PersistentBrowserSessionStatus.completed,
    PersistentBrowserSessionStatus.failed,
    PersistentBrowserSessionStatus.timeout,
)


def is_final_status(status: str | None) -> bool:
    return status in FINAL_STATUSES


def export_profile_storage_id(
    *,
    session_id: str,
    browser_profile_id: str | None,
    generate_browser_profile: bool,
) -> str:
    """Pure (cloud-dep-free) routing helper mapping a session to the storage id its profile archive is exported
    to and read from (pass the loaded profile id, or None when no profile loaded)."""
    if generate_browser_profile:
        return session_id
    return browser_profile_id or session_id


class PersistentBrowserType(StrEnum):
    MSEdge = "msedge"
    Chrome = "chrome"
    StealthChromium = "stealth-chromium"

    @classmethod
    def from_source_browser_type(cls, value: str) -> "PersistentBrowserType | None":
        try:
            return cls(value)
        except ValueError:
            return None


class Extensions(StrEnum):
    AdBlocker = "ad-blocker"
    CaptchaSolver = "captcha-solver"


FORCED_WORKFLOW_SESSION_RUNNABLE_TYPE = "forced_workflow_run"


class PersistentBrowserSession(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    persistent_browser_session_id: str
    organization_id: str
    runnable_type: str | None = None
    runnable_id: str | None = None
    browser_address: str | None = None
    ip_address: str | None = None
    # Server-side only: the upstream CDP endpoint and the adapter that dials it. browser_address
    # remains the client-facing proxy URL. These must never reach a client or a log —
    # BrowserSessionResponse.from_browser_session is the allowlist that enforces the former.
    upstream_cdp_url: str | None = None
    browser_vendor: str | None = None
    status: str | None = None
    timeout_minutes: int | None = None
    proxy_location: ProxyLocationInput = None
    proxy_session_id: str | None = None
    instance_type: str | None = None
    vcpu_millicores: int | None = None
    memory_mb: int | None = None
    duration_ms: int | None = None
    compute_cost: Decimal | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime
    modified_at: datetime
    deleted_at: datetime | None = None
    extensions: list[Extensions] | None = None
    browser_type: PersistentBrowserType | None = None
    browser_profile_id: str | None = None
    generate_browser_profile: bool = False
    # False once a requested browser_profile_id failed to load at launch (fell back to a fresh profile),
    # so teardown exported under the session id rather than the bp_ id.
    browser_profile_loaded: bool = True

    @field_validator("proxy_location", mode="before")
    @classmethod
    def deserialize_proxy_location_field(cls, value: object) -> object:
        if isinstance(value, str):
            return deserialize_proxy_location(value, raise_on_invalid_geo_target=True)
        return value

    @field_validator("proxy_session_id")
    @classmethod
    def validate_proxy_session_id_field(cls, value: str | None) -> str | None:
        return validate_proxy_session_id(value)

    def should_export_profile(self) -> bool:
        """A session persists its profile at teardown only when it opted in or is reusing a saved profile.

        A reuse session (browser_profile_id set) must always re-export so the updated session-cookie
        sidecar survives; gating it off would silently log the profile out on the next reuse.
        """
        return bool(self.generate_browser_profile or self.browser_profile_id)


class AddressablePersistentBrowserSession(PersistentBrowserSession):
    browser_address: str
