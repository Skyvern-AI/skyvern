"""A form submission a page-level interceptor held and released, as the task loop reads it."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class SubmissionOutcome(StrEnum):
    HELD = "held"
    # The page received a response. Says nothing about whether the site accepted the submission.
    DELIVERED = "delivered"
    # The page received the site's own typed machine rejection, after the interceptor's recovery ran out.
    REJECTED_BY_SITE = "rejected_by_site"
    # The interceptor failed, was canceled, or could not see the response the page got.
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class InterceptedSubmission:
    # Unique within the process and increasing in the order the interceptor saw the requests.
    record_id: int
    outcome: SubmissionOutcome
    # time.monotonic() readings.
    started_at: float
    released_at: float | None = None
    delivered_at: float | None = None
    # The site's machine error code, set only on REJECTED_BY_SITE.
    site_error_type: str | None = None

    @property
    def effect_at(self) -> float | None:
        """When the page could last have changed because of this submission; None while it is still held."""
        return self.delivered_at if self.delivered_at is not None else self.released_at
