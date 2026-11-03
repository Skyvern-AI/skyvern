from typing import Literal

try:
    import psycopg
except ImportError:  # psycopg ships with the server extra only
    psycopg = None  # type: ignore[assignment]

# SQLSTATE class 08 is "connection exception"; 57P0x is the server going away (admin or crash
# shutdown, cannot_connect_now, idle-session timeout); 53300 is too_many_connections, a saturated
# pooler refusing the connect. No SQLSTATE at all means psycopg never got a server response: a
# refused connect or a socket that dropped mid-request.
_CONNECTION_SQLSTATE_PREFIXES = ("08", "57P", "53300")


def is_connection_failure(dbapi_error: BaseException) -> bool:
    if psycopg is None or not isinstance(dbapi_error, psycopg.OperationalError):
        return False
    sqlstate = dbapi_error.sqlstate
    return sqlstate is None or sqlstate.startswith(_CONNECTION_SQLSTATE_PREFIXES)


class DatabaseConnectionUnavailableError(Exception):
    """A read could not reach the database after bounded reconnection attempts."""

    def __init__(self, operation: str, attempts: int) -> None:
        super().__init__(f"{operation} could not reach the database after {attempts} connection attempts")
        self.operation = operation
        self.attempts = attempts


class NotFoundError(Exception):
    pass


class RunsListSearchCanceled(Exception):
    """A statement timeout cancelled the statement carrying a runs-list search filter.

    Raised for that statement only, so a cancellation anywhere else in the same request keeps its
    own error and its own logging. ``statement_timeout_seconds`` is the budget that applied, or
    None when the read ran under the connection's own timeout.
    """

    def __init__(self, *, indexed_search: bool, statement_timeout_seconds: float | None) -> None:
        super().__init__("A runs list search statement was cancelled")
        self.indexed_search = indexed_search
        self.statement_timeout_seconds = statement_timeout_seconds


class DuplicateCopilotTurnError(Exception):
    def __init__(self, turn_id: str) -> None:
        super().__init__(f"Copilot turn {turn_id} already owns this idempotency key")
        self.turn_id = turn_id


CopilotProposalConflictReason = Literal[
    "token_changed",
    "candidate_missing",
    "metadata_invalid",
    "being_accepted",
    "bytes_changed",
    "content_changed",
    "claim_changed",
    "disposition_changed",
]


class CopilotProposalConflictError(Exception):
    """The candidate changed after the caller read its owner/revision token."""

    def __init__(self, message: str, *, reason: CopilotProposalConflictReason) -> None:
        super().__init__(message)
        self.reason = reason


class ScheduleLimitExceededError(Exception):
    """Raised when attempting to create a schedule that would exceed the org-wide tier limit."""

    def __init__(self, organization_id: str, current_count: int, max_allowed: int):
        self.organization_id = organization_id
        self.current_count = current_count
        self.max_allowed = max_allowed
        super().__init__(f"Schedule limit {max_allowed} reached (current: {current_count})")
