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
