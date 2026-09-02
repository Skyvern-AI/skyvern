"""A leaf module with no Skyvern imports: copilot and workflow-service modules name a close reason
from inside the import cycle that ``persistent_browser_sessions`` reaches through ``db.utils``."""

from enum import StrEnum


class BrowserSessionCloseReason(StrEnum):
    """Persisted on the session row, so every member stays low-cardinality and free of session,
    run, or customer identifiers."""

    user_requested = "user_requested"
    expired = "expired"
    orphaned = "orphaned"
    shutdown = "shutdown"
    aborted = "aborted"
