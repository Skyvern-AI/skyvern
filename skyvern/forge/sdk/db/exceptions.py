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


class NotFoundError(Exception):
    pass


class DuplicateCopilotTurnError(Exception):
    def __init__(self, turn_id: str) -> None:
        super().__init__(f"Copilot turn {turn_id} already owns this idempotency key")
        self.turn_id = turn_id


class ScheduleLimitExceededError(Exception):
    """Raised when attempting to create a schedule that would exceed the org-wide tier limit."""

    def __init__(self, organization_id: str, current_count: int, max_allowed: int):
        self.organization_id = organization_id
        self.current_count = current_count
        self.max_allowed = max_allowed
        super().__init__(f"Schedule limit {max_allowed} reached (current: {current_count})")
