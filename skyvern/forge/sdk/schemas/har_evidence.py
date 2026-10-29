from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_validator

MAX_EVIDENCE_VISITS = 20
MAX_EVIDENCE_EVENTS = 50
EVENT_HOST_CLASSES = frozenset({"site", "third_party", "vendor_cdn", "vendor_api", "unknown"})


class HarEvidenceAvailability(StrEnum):
    # Parsed; an empty visit list then means the recording held nothing the extractor recognizes.
    available = "available"
    not_collected = "not_collected"
    har_missing = "har_missing"
    out_of_scope = "out_of_scope"
    too_large = "too_large"
    capacity = "capacity"
    parse_error = "parse_error"
    timeout = "timeout"


class HarEvidenceEvent(BaseModel):
    """One anti-bot event, carrying only class-level fields: never a URL, header, cookie, query or body value."""

    model_config = ConfigDict(frozen=True)

    event_type: str
    event_at: datetime | None = None
    vendor: str | None = None
    challenge_type: str | None = None
    signature_ids: tuple[str, ...] = ()
    http_status: int | None = None
    method: str | None = None
    accept_class: str | None = None
    url_class: str | None = None
    surface_class: str | None = None
    rate_limit_kind: str | None = None
    retry_after_s: int | None = None
    resource_fail_class: str | None = None
    vendor_path_class: str | None = None
    # The event's host relative to the visited site; an observation, not proof of who blocked the page.
    host_class: str = "unknown"
    # Set only by the deployment's extractor for an event it qualifies as enforcement (a block, not a widget or asset
    # load), with the service that event identifies, or None when it names none the reporting knows.
    failure_signal: str | None = None
    service: str | None = None

    @field_validator("host_class", mode="before")
    @classmethod
    def _known_host_class(cls, value: object) -> str:
        return value if isinstance(value, str) and value in EVENT_HOST_CLASSES else "unknown"


class HarEvidenceVisit(BaseModel):
    model_config = ConfigDict(frozen=True)

    tab_ref: str
    visit_n: int
    domain: str | None
    started_at: datetime | None
    ended_at: datetime | None
    vendors_seen: tuple[str, ...]
    clearance_effective: str
    events: tuple[HarEvidenceEvent, ...] = ()


class HarEvidence(BaseModel):
    """What a run's final HAR recording says about anti-bot activity, or why it says nothing. Vendor presence and
    events are observations, not proof that they caused the run's failure."""

    model_config = ConfigDict(frozen=True)

    availability: HarEvidenceAvailability
    extractor_version: str | None = None
    parse_notes: tuple[str, ...] = ()
    har_entry_count: int = 0
    visits: tuple[HarEvidenceVisit, ...] = ()
    # Older visits and events past the caps are dropped first, so what remains is nearest the run's end.
    truncated: bool = False

    @classmethod
    def unavailable(cls, availability: HarEvidenceAvailability) -> HarEvidence:
        return cls(availability=availability)
