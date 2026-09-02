from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from skyvern.config import settings


class BrowserOperation(StrEnum):
    """Browser-protocol operations whose deadline is generous enough that a timeout means the
    browser stopped answering, rather than that one selector or one animation was slow."""

    EVALUATE = "evaluate"
    SCREENSHOT = "screenshot"
    RELOAD = "reload"


# Requiring more than one KIND of stuck operation is what separates a browser that has stopped
# answering from a single deadline being too tight for one heavy page.
MIN_DISTINCT_STUCK_OPERATIONS = 2


@dataclass
class BrowserHealth:
    """Browser-protocol timeouts for one run that no success has interrupted.

    Consecutive rather than cumulative: a browser that answers anything at all is still usable, and
    the retry ladders above are built to ride out an ordinary slow page.
    """

    consecutive_timeouts: int = 0
    stuck_operations: set[BrowserOperation] = field(default_factory=set)

    def record_success(self) -> None:
        self.consecutive_timeouts = 0
        self.stuck_operations.clear()

    def record_timeout(self, operation: BrowserOperation) -> None:
        self.consecutive_timeouts += 1
        self.stuck_operations.add(operation)

    def record_recovery(self, operation: BrowserOperation) -> None:
        """A second route answered the operation that just timed out, so that strike was a tight
        deadline rather than a dead browser; strikes held by other operations stand. A failure that
        never earned a strike (a non-timeout error) has nothing to give back."""
        if operation not in self.stuck_operations:
            return
        self.consecutive_timeouts = max(0, self.consecutive_timeouts - 1)
        self.stuck_operations.discard(operation)

    @property
    def is_degraded(self) -> bool:
        strikes = settings.BROWSER_DEGRADED_TIMEOUT_STRIKES
        if strikes <= 0:
            return False
        return self.consecutive_timeouts >= strikes and len(self.stuck_operations) >= MIN_DISTINCT_STUCK_OPERATIONS

    def describe_stuck_operations(self) -> str:
        return ", ".join(sorted(self.stuck_operations))
