from datetime import UTC, datetime
from email.utils import parsedate_to_datetime


class ProviderAPIError(RuntimeError):
    def __init__(self, *, status: int, code: str | None, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def retry_after_seconds(retry_after: str | None) -> float | None:
    """Seconds to wait from a numeric or HTTP-date ``Retry-After`` value, or None when it gives no wait."""
    if not retry_after:
        return None
    value = retry_after.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        target = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if target.tzinfo is None:
        target = target.replace(tzinfo=UTC)
    return max(0.0, (target - datetime.now(UTC)).total_seconds())


def compute_backoff(
    attempt: int,
    retry_after: str | None,
    *,
    base_seconds: float,
    factor: float,
    max_seconds: float | None = None,
) -> float:
    delay = retry_after_seconds(retry_after)
    if delay is None:
        delay = base_seconds * (factor ** (attempt - 1))
    return delay if max_seconds is None else min(delay, max_seconds)
