from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

MAX_EVIDENCE_VISITS = 20
MAX_EVIDENCE_EVENTS = 50
EVENT_HOST_CLASSES = frozenset({"site", "third_party", "vendor_cdn", "vendor_api", "unknown"})
# The final closed-recording user-error match's claim and result. It has no category: it is never a failure label,
# whatever its position, and it is held in failure_category only because that is the row's diagnostic carrier.
FINAL_HAR_USER_MATCH_REASON_CODE = "final_har_user_match"


def is_final_har_user_match_entry(category: object) -> bool:
    return isinstance(category, dict) and category.get("reason_code") == FINAL_HAR_USER_MATCH_REASON_CODE


def without_final_har_user_match(categories: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    """``categories`` as readers and serializers see them: the final match's bookkeeping is never a category."""
    if not categories or not any(is_final_har_user_match_entry(c) for c in categories):
        return categories
    return [c for c in categories if not is_final_har_user_match_entry(c)] or None


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


HTTP_FAILURE_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "OTHER"})
HTTP_FAILURE_URL_CLASSES = frozenset({"document", "api", "static", "websocket", "other"})


class HarHttpFailure(BaseModel):
    """An ordinary request answered with an HTTP error: class-level shape only, never its URL or content."""

    model_config = ConfigDict(frozen=True)

    at: datetime | None = None
    status: int
    method: str
    url_class: str

    @field_validator("method", mode="before")
    @classmethod
    def _known_method(cls, value: object) -> str:
        return value if isinstance(value, str) and value in HTTP_FAILURE_METHODS else "OTHER"

    @field_validator("url_class", mode="before")
    @classmethod
    def _known_url_class(cls, value: object) -> str:
        return value if isinstance(value, str) and value in HTTP_FAILURE_URL_CLASSES else "other"


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
    """What a run's final HAR recording says about anti-bot activity and ordinary HTTP failures, or why it says
    nothing. Vendor presence, events and failures are observations, not proof that they caused the run's failure."""

    model_config = ConfigDict(frozen=True)

    availability: HarEvidenceAvailability
    extractor_version: str | None = None
    parse_notes: tuple[str, ...] = ()
    har_entry_count: int = 0
    visits: tuple[HarEvidenceVisit, ...] = ()
    # The latest MAX_EVIDENCE_EVENTS of them.
    http_failures: tuple[HarHttpFailure, ...] = ()
    # Older visits and events past the caps are dropped first, so what remains is nearest the run's end.
    truncated: bool = False

    @classmethod
    def unavailable(cls, availability: HarEvidenceAvailability) -> HarEvidence:
        return cls(availability=availability)
